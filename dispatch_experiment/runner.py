"""End-to-end offline dispatch simulation and JSON result writer."""

import copy
import hashlib
import json
import math
import random
import time
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from route_experiment.mapgen import generate_map
from route_experiment.models import MapConfig

from .agent import CandidateFeedback, ContextFunction, VehicleAgent, apply_feedback
from .models import (
    Assignment,
    Candidate,
    DispatchConfig,
    DispatchInput,
    DispatchOutput,
    DispatchRoundRecord,
    ExperimentSummary,
    Order,
    Vehicle,
)
from .optimizer import (
    DeterministicGlobalOptimizer,
    build_assignments,
    greedy_assign,
    validate_selection,
)
from .route_oracle import GraphRouteOracle


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(percentile * len(ordered))) - 1))
    return ordered[index]


def _hotspot_definitions(road_map) -> List[Dict[str, object]]:
    """Build three fixed, non-overlapping equal-probability pickup regions.

    Their areas increase from high to low density.  A sampled order first picks
    one region uniformly, then picks a legal pickup node uniformly within it.
    The 100 m node-cell footprint is the area convention recorded in outputs.
    """

    size = road_map.size
    if size < 6:
        raise ValueError("dispatch hotspot sampling requires a map of at least 6x6")
    high_width = max(1, size // 6)
    medium_width = max(high_width + 1, size // 3)
    low_width = size - high_width - medium_width
    if low_width <= medium_width:
        raise ValueError("map is too small to form three strictly increasing hotspot areas")

    definitions = []
    x_start = 0
    for hotspot_id, density_label, width in (
        ("hotspot-high", "high", high_width),
        ("hotspot-medium", "medium", medium_width),
        ("hotspot-low", "low", low_width),
    ):
        x_end = x_start + width - 1
        legal_node_ids = sorted(
            node.node_id
            for node in road_map.nodes
            if x_start <= node.x <= x_end and node.legal_pickup
        )
        if not legal_node_ids:
            raise ValueError("hotspot has no legal pickup nodes: " + hotspot_id)
        definitions.append(
            {
                "hotspot_id": hotspot_id,
                "density_label": density_label,
                "bounds_nodes": {
                    "x_min": x_start,
                    "x_max": x_end,
                    "y_min": 0,
                    "y_max": size - 1,
                },
                "area_size_m2": width * size * 100 * 100,
                "area_estimation": "node_cell_footprint_100m",
                "legal_pickup_node_count": len(legal_node_ids),
                "legal_pickup_node_ids": legal_node_ids,
                "sampling_probability": 1.0 / 3.0,
            }
        )
        x_start = x_end + 1
    return definitions


def generate_orders(
    road_map,
    config: DispatchConfig,
    rng: random.Random,
    hotspots: Sequence[Dict[str, object]],
) -> List[Order]:
    dropoffs = [node for node in road_map.nodes if node.legal_dropoff]
    if not hotspots or not dropoffs:
        raise ValueError("dispatch map must contain legal pickup and dropoff nodes")
    node_by_id = {node.node_id: node for node in road_map.nodes}
    arrival_times = sorted(
        rng.random() * max(1, config.horizon_sec - 1) for _ in range(config.order_count)
    )
    no_show_count = int(round(config.order_count * config.passenger_no_show_rate))
    no_show_indices = set(rng.sample(range(config.order_count), no_show_count))
    orders: List[Order] = []
    for index, arrival_time in enumerate(arrival_times):
        hotspot = rng.choice(hotspots)
        pickup = node_by_id[rng.choice(hotspot["legal_pickup_node_ids"])]
        dropoff = dropoffs[rng.randrange(len(dropoffs))]
        if dropoff.node_id == pickup.node_id and len(dropoffs) > 1:
            dropoff = dropoffs[(dropoffs.index(dropoff) + 1) % len(dropoffs)]
        orders.append(
            Order(
                order_id="order-{0:04d}".format(index),
                arrival_time_sec=round(arrival_time, 3),
                pickup_node_id=pickup.node_id,
                dropoff_node_id=dropoff.node_id,
                hotspot_id=hotspot["hotspot_id"],
                passenger_no_show=index in no_show_indices,
            )
        )
    return orders


def generate_vehicles(road_map, config: DispatchConfig, rng: random.Random) -> List[Vehicle]:
    vehicles: List[Vehicle] = []
    for index in range(config.vehicle_count):
        node = road_map.nodes[rng.randrange(len(road_map.nodes))]
        vehicles.append(
            Vehicle(
                vehicle_id="vehicle-{0:02d}".format(index),
                initial_position_node_id=node.node_id,
                position_node_id=node.node_id,
            )
        )
    return vehicles


def _method_options(method_name: str) -> Tuple[bool, bool, str]:
    """Return (use_context_function, use_feedback, audit label)."""

    if method_name == "D0_greedy":
        return False, False, "greedy_baseline"
    if method_name == "D1_optimizer":
        return False, False, "centralized_optimizer"
    if method_name == "D2_agent_optimizer":
        return True, False, "agent_manual_context"
    if method_name == "D3_agent_llm_context_optimizer":
        return True, False, "agent_offline_llm_context_surrogate"
    if method_name == "D4_agent_llm_feedback_optimizer":
        return True, True, "agent_offline_llm_feedback_surrogate"
    if method_name == "D5_pure_llm_direct":
        return False, False, "pure_llm_direct_matching"
    raise ValueError("unknown dispatch method: " + method_name)


class DispatchSimulation:
    def __init__(
        self,
        config: DispatchConfig,
        *,
        road_map=None,
        orders: Optional[Sequence[Order]] = None,
        vehicles: Optional[Sequence[Vehicle]] = None,
        route_oracle=None,
        retain_round_records: bool = True,
        llm_trace_dir: Optional[Path] = None,
    ):
        self.config = config
        self.retain_round_records = retain_round_records
        self.rng = random.Random(config.seed)
        supplied = (road_map is not None, orders is not None, vehicles is not None)
        if any(supplied) and not all(supplied):
            raise ValueError("custom dispatch scenarios require road_map, orders, and vehicles together")
        custom_scenario = all(supplied)
        if custom_scenario:
            self.road_map = road_map
            self.orders = copy.deepcopy(list(orders))
            self.vehicles = copy.deepcopy(list(vehicles))
            if len(self.orders) != config.order_count:
                raise ValueError("config.order_count must match supplied orders")
            if len(self.vehicles) != config.vehicle_count:
                raise ValueError("config.vehicle_count must match supplied vehicles")
            if self.road_map.size != config.map_size:
                raise ValueError("config.map_size must match the supplied road map")
            node_ids = {node.node_id for node in self.road_map.nodes}
            if len(node_ids) != len(self.road_map.nodes):
                raise ValueError("custom road map contains duplicate node IDs")
            if len({order.order_id for order in self.orders}) != len(self.orders):
                raise ValueError("custom scenario contains duplicate order IDs")
            if len({vehicle.vehicle_id for vehicle in self.vehicles}) != len(self.vehicles):
                raise ValueError("custom scenario contains duplicate vehicle IDs")
            if any(
                order.status != "future"
                or order.pickup_node_id not in node_ids
                or order.dropoff_node_id not in node_ids
                for order in self.orders
            ):
                raise ValueError("custom orders must be future orders with nodes present on the road map")
            if any(
                vehicle.state != "idle"
                or vehicle.initial_position_node_id not in node_ids
                or vehicle.position_node_id not in node_ids
                for vehicle in self.vehicles
            ):
                raise ValueError("custom vehicles must start idle at nodes present on the road map")
            self.hotspots = []
        else:
            self.road_map = generate_map(
                seed=config.seed,
                size=config.map_size,
                config=MapConfig(size=config.map_size),
            )
            self.hotspots = _hotspot_definitions(self.road_map)
            self.orders = generate_orders(self.road_map, config, self.rng, self.hotspots)
            self.vehicles = generate_vehicles(self.road_map, config, self.rng)
        self.oracle = route_oracle or GraphRouteOracle(self.road_map)
        self.order_by_id = {order.order_id: order for order in self.orders}
        self.vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in self.vehicles}
        self.method_use_context, self.method_use_feedback, self.audit_label = _method_options(config.method_name)
        function_mode = "manual_linear_clip"
        if config.method_name == "D3_agent_llm_context_optimizer" or config.method_name == "D4_agent_llm_feedback_optimizer":
            function_mode = "llm_linear_clip_surrogate"
        self.context_function = ContextFunction(mode=function_mode)
        if self.method_use_feedback and config.feedback_mode == "deepseek":
            from .llm import default_deepseek_feedback

            self.feedback = default_deepseek_feedback()
        else:
            self.feedback = CandidateFeedback(
                config.feedback_mode if self.method_use_feedback else "none"
            )
        self.direct_llm = None
        if config.method_name == "D5_pure_llm_direct":
            if config.llm_backend != "codex_cli":
                raise ValueError(
                    "D5_pure_llm_direct currently requires llm_backend='codex_cli'"
                )
            if llm_trace_dir is None:
                raise ValueError("D5_pure_llm_direct requires llm_trace_dir to preserve usage traces")
            from .llm import default_codex_cli_direct_assignment

            self.direct_llm = default_codex_cli_direct_assignment(
                model=config.llm_model,
                trace_dir=llm_trace_dir,
            )
        self.optimizer = DeterministicGlobalOptimizer()
        self.round_records: List[DispatchRoundRecord] = []
        self.round_audits: List[Dict[str, object]] = []
        self.round_validation_errors: List[Dict[str, object]] = []
        self.current_candidate_by_order: Dict[str, Candidate] = {}
        self.metrics = {
            "candidate_before": 0,
            "candidate_after": 0,
            "fallback_count": 0,
            "rounds": 0,
            "total_decision_ms": 0.0,
            "optimizer_ms": 0.0,
        }

    def _next_event_time(self, now: float) -> Optional[float]:
        events: List[float] = []
        events.extend(order.arrival_time_sec for order in self.orders if order.status == "future")
        for vehicle in self.vehicles:
            if vehicle.state == "pickup_in_progress" and vehicle.planned_pickup_time_sec is not None:
                events.append(vehicle.planned_pickup_time_sec)
            elif vehicle.state == "arrived_waiting" and vehicle.boarding_deadline_time_sec is not None:
                events.append(vehicle.boarding_deadline_time_sec)
            elif vehicle.state == "occupied" and vehicle.planned_dropoff_time_sec is not None:
                events.append(vehicle.planned_dropoff_time_sec)
        events.extend(
            order.arrival_time_sec + self.config.max_pickup_wait_sec
            for order in self.orders
            if order.status == "waiting"
        )
        future = [event for event in events if event >= now - 1e-9]
        return min(future) if future else None

    def _release_arrivals(self, now: float) -> int:
        count = 0
        for order in self.orders:
            if order.status == "future" and order.arrival_time_sec <= now + 1e-9:
                order.status = "waiting"
                count += 1
        return count

    def _start_order(self, vehicle: Vehicle, order: Order, candidate: Candidate, now: float) -> None:
        order.status = "assigned"
        order.assigned_vehicle_id = vehicle.vehicle_id
        order.assignment_time_sec = now
        vehicle.current_order_id = order.order_id
        vehicle.current_pickup_node_id = order.pickup_node_id
        vehicle.current_dropoff_node_id = order.dropoff_node_id
        vehicle.state = "pickup_in_progress"
        vehicle.planned_pickup_time_sec = candidate.predicted_pickup_time_sec
        vehicle.planned_dropoff_time_sec = candidate.predicted_dropoff_time_sec
        vehicle.boarding_deadline_time_sec = None
        vehicle.available_time_sec = candidate.predicted_dropoff_time_sec
        vehicle.last_assignment_time_sec = now
        vehicle.total_empty_distance_m += candidate.empty_distance_m
        vehicle.total_energy_kwh += candidate.energy_kwh
        vehicle.total_busy_sec += candidate.pickup_leg.duration_sec + candidate.ride_leg.duration_sec

    def _apply_assignments(self, selected: Sequence[Candidate], now: float) -> None:
        for candidate in selected:
            vehicle = self.vehicle_by_id[candidate.vehicle_id]
            order = self.order_by_id[candidate.order_id]
            if candidate.candidate_type == "immediate_pickup":
                self._start_order(vehicle, order, candidate, now)
            else:
                order.status = "reserved"
                order.assigned_vehicle_id = vehicle.vehicle_id
                order.assignment_time_sec = now
                vehicle.reserved_order_id = order.order_id
                self.current_candidate_by_order[order.order_id] = candidate

    def _process_vehicle_events(self, now: float) -> List[str]:
        triggers: List[str] = []
        for vehicle in self.vehicles:
            if vehicle.state == "pickup_in_progress" and vehicle.planned_pickup_time_sec is not None:
                if vehicle.planned_pickup_time_sec > now + 1e-9:
                    continue
                order = self.order_by_id[vehicle.current_order_id]
                order.pickup_arrival_time_sec = vehicle.planned_pickup_time_sec
                if order.passenger_no_show:
                    order.status = "arrived_waiting"
                    vehicle.state = "arrived_waiting"
                    vehicle.boarding_deadline_time_sec = (
                        now + self.config.post_arrival_boarding_timeout_sec
                    )
                    vehicle.available_time_sec = vehicle.boarding_deadline_time_sec
                    triggers.append("vehicle_arrived_waiting")
                else:
                    order.status = "boarded"
                    order.boarding_time_sec = now
                    vehicle.state = "occupied"
                    triggers.append("passenger_boarded")
            if vehicle.state == "arrived_waiting" and vehicle.boarding_deadline_time_sec is not None:
                if vehicle.boarding_deadline_time_sec > now + 1e-9:
                    continue
                order = self.order_by_id[vehicle.current_order_id]
                order.status = "cancelled"
                order.cancel_time_sec = now
                order.cancel_reason = "passenger_no_show"
                self._release_vehicle(vehicle, order.pickup_node_id)
                triggers.append("passenger_no_show_cancelled")
            if vehicle.state == "occupied" and vehicle.planned_dropoff_time_sec is not None:
                if vehicle.planned_dropoff_time_sec > now + 1e-9:
                    continue
                order = self.order_by_id[vehicle.current_order_id]
                order.status = "completed"
                order.dropoff_time_sec = now
                vehicle.position_node_id = order.dropoff_node_id
                current_order_id = order.order_id
                vehicle.current_order_id = None
                vehicle.current_pickup_node_id = None
                vehicle.current_dropoff_node_id = None
                vehicle.planned_pickup_time_sec = None
                vehicle.planned_dropoff_time_sec = None
                vehicle.boarding_deadline_time_sec = None
                vehicle.available_time_sec = now
                if vehicle.reserved_order_id:
                    reserved_order = self.order_by_id[vehicle.reserved_order_id]
                    candidate = self.current_candidate_by_order.pop(reserved_order.order_id)
                    vehicle.reserved_order_id = None
                    self._start_order(vehicle, reserved_order, candidate, now)
                else:
                    vehicle.state = "idle"
                triggers.append("current_order_completed:" + current_order_id)
        return triggers

    @staticmethod
    def _release_vehicle(vehicle: Vehicle, position_node_id: str) -> None:
        vehicle.position_node_id = position_node_id
        vehicle.current_order_id = None
        vehicle.current_pickup_node_id = None
        vehicle.current_dropoff_node_id = None
        vehicle.planned_pickup_time_sec = None
        vehicle.planned_dropoff_time_sec = None
        vehicle.boarding_deadline_time_sec = None
        vehicle.available_time_sec = 0.0
        vehicle.state = "idle"

    def _expire_waiting(self, now: float) -> List[str]:
        expired: List[str] = []
        for order in self.orders:
            if order.status == "waiting" and now >= order.arrival_time_sec + self.config.max_pickup_wait_sec:
                order.status = "unserved"
                order.cancel_time_sec = now
                order.cancel_reason = "timeout"
                expired.append(order.order_id)
        return expired

    def _run_dispatch(self, now: float, trigger_type: str) -> None:
        started = time.perf_counter()
        round_id = "round-{0:04d}".format(self.metrics["rounds"])
        raw_candidates: List[Candidate] = []
        all_candidates: List[Candidate] = []
        all_decisions = []
        for vehicle in self.vehicles:
            agent = VehicleAgent(vehicle.vehicle_id)
            vehicle_candidates, context_candidates, decisions = agent.propose(
                dispatch_round_id=round_id,
                now=now,
                vehicle=vehicle,
                orders=self.orders,
                vehicles=self.vehicles,
                oracle=self.oracle,
                config=self.config,
                context_function=self.context_function,
                use_context=self.method_use_context,
            )
            raw_candidates.extend(vehicle_candidates)
            all_candidates.extend(
                context_candidates if self.method_use_context else vehicle_candidates
            )
            all_decisions.extend(decisions)
        # Full audit mode snapshots the method-neutral candidate table before
        # context filtering or feedback mutates the method's working set.
        # Compact audit mode does not retain per-round tables, avoiding several
        # large deep copies for high-volume scenarios.
        shared_input_candidates: List[Candidate] = []
        if self.retain_round_records:
            shared_input_candidates = copy.deepcopy(raw_candidates)
            for candidate in shared_input_candidates:
                candidate.context_threshold_sec = None
                candidate.context_pass = True
                candidate.feedback_decision = None
                candidate.feedback_risk = None
                candidate.feedback_reason = None
                for field_name in (
                    "predicted_pickup_time_sec",
                    "predicted_dropoff_time_sec",
                    "predicted_wait_sec",
                ):
                    value = getattr(candidate, field_name)
                    if not math.isfinite(value):
                        setattr(candidate, field_name, None)
        before_count = len(raw_candidates)
        feedback_started = time.perf_counter()
        if self.method_use_feedback:
            all_candidates = apply_feedback(
                all_candidates, self.feedback, self.config.max_pickup_wait_sec
            )
        feedback_ms = (time.perf_counter() - feedback_started) * 1000.0
        after_count = len(all_candidates)
        optimizer_started = time.perf_counter()
        direct_llm_record = None
        if self.config.method_name == "D5_pure_llm_direct":
            selected, direct_llm_record = self.direct_llm.select(
                snapshot_time_sec=now,
                vehicles=self.vehicles,
                orders=self.orders,
                candidates=raw_candidates,
                max_wait_sec=self.config.max_pickup_wait_sec,
            )
            optimizer_status = direct_llm_record.get("status", "unknown")
        elif self.config.method_name == "D0_greedy":
            selected = greedy_assign(all_candidates, self.orders)
            optimizer_status = "not_used"
        else:
            selected, optimizer_status = self.optimizer.optimize(
                all_candidates, self.vehicles, self.orders
            )
        optimizer_ms = (
            0.0
            if self.config.method_name == "D5_pure_llm_direct"
            else (time.perf_counter() - optimizer_started) * 1000.0
        )
        errors = validate_selection(
            selected, all_candidates, self.config.max_pickup_wait_sec
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        selected_order_ids = {candidate.order_id for candidate in selected}
        feasible_by_order: Dict[str, set] = {}
        for candidate in raw_candidates:
            if candidate.hard_constraint_ok:
                feasible_by_order.setdefault(candidate.order_id, set()).add(candidate.vehicle_id)
        contested_order_ids = [
            order_id for order_id, vehicle_ids in feasible_by_order.items() if len(vehicle_ids) > 1
        ]
        decision_reasons: Dict[str, int] = {}
        for decision in all_decisions:
            if decision.agent_decision == "reject":
                reason = decision.reason or "unspecified"
                decision_reasons[reason] = decision_reasons.get(reason, 0) + 1
        selected_vehicle_ids = [candidate.vehicle_id for candidate in selected]
        selected_order_ids_list = [candidate.order_id for candidate in selected]
        one_to_one_valid = (
            len(set(selected_vehicle_ids)) == len(selected)
            and len(set(selected_order_ids_list)) == len(selected)
        )
        if errors or not one_to_one_valid:
            self.round_validation_errors.append(
                {
                    "dispatch_round_id": round_id,
                    "hard_constraint_errors": errors,
                    "one_to_one_valid": one_to_one_valid,
                }
            )
        round_audit = {
                "dispatch_round_id": round_id,
                "snapshot_time_sec": now,
                "trigger_type": trigger_type,
                "candidate_decision_count": len(all_decisions),
                "retained_candidate_count": len(all_candidates),
                "hard_feasible_candidate_count": sum(
                    1 for candidate in raw_candidates if candidate.hard_constraint_ok
                ),
                "rejected_candidate_decision_counts": decision_reasons,
                "multi_vehicle_competition_order_count": len(contested_order_ids),
                "multi_vehicle_competition_order_ids": sorted(contested_order_ids),
                "selected_assignment_count": len(selected),
                "selected_vehicle_ids": selected_vehicle_ids,
                "selected_order_ids": selected_order_ids_list,
                "validator_status": "invalid" if errors else "valid",
                "hard_constraint_errors": errors,
                "one_to_one_valid": one_to_one_valid,
                "direct_llm_assignment": direct_llm_record,
                "direct_llm_error_count": (
                    len(self.direct_llm.errors) if self.direct_llm is not None else 0
                ),
            }
        if not self.retain_round_records and self.metrics["rounds"] == 0:
            # Keep the complete first candidate table so compact D0/D1/D5
            # runs still expose an auditable common initial decision input.
            def json_safe(value):
                if isinstance(value, float) and not math.isfinite(value):
                    return None
                if isinstance(value, dict):
                    return {key: json_safe(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [json_safe(item) for item in value]
                return value

            first_round_candidates = []
            for candidate in raw_candidates:
                row = candidate.to_dict()
                row["context_threshold_sec"] = None
                row["context_pass"] = True
                row["feedback_decision"] = None
                row["feedback_risk"] = None
                row["feedback_reason"] = None
                first_round_candidates.append(json_safe(row))
            round_audit["common_candidate_table"] = first_round_candidates
            round_audit["common_candidate_table_sha256"] = hashlib.sha256(
                json.dumps(
                    first_round_candidates,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        self.round_audits.append(round_audit)
        output = DispatchOutput(
            decision_package_id="decision-" + round_id,
            dispatch_round_id=round_id,
            method_name=self.config.method_name,
            method_version="dispatch-agent-v1",
            assignments=build_assignments(selected),
            unserved_order_ids=[
                order.order_id
                for order in self.orders
                if order.status in ("waiting", "unserved")
                and order.order_id not in selected_order_ids
            ],
            selected_candidate_ids=[candidate.candidate_id for candidate in selected],
            candidate_decisions=(
                all_decisions
                if self.retain_round_records and self.config.method_name != "D5_pure_llm_direct"
                else []
            ),
            optimizer_status=optimizer_status,
            fallback_used=any(decision.fallback_used for decision in all_decisions),
            fallback_reason=(
                "no_candidate_after_context_filter"
                if any(decision.fallback_used for decision in all_decisions)
                else None
            ),
            validator_status="invalid" if errors else "valid",
            hard_constraint_errors=errors,
            function_time_ms=0.0,
            llm_time_ms=(
                direct_llm_record.get("latency_ms", 0.0)
                if self.direct_llm is not None and direct_llm_record
                else (
                    feedback_ms
                    if self.method_use_feedback and self.feedback.is_live_llm
                    else 0.0
                )
            ),
            optimizer_time_ms=(0.0 if self.config.method_name == "D5_pure_llm_direct" else optimizer_ms),
            total_decision_time_ms=elapsed_ms,
            llm_call_count=(
                int(bool(direct_llm_record.get("request_made")))
                if self.direct_llm is not None and direct_llm_record
                else (self.feedback.call_count if self.feedback.is_live_llm else 0)
            ),
            input_tokens=(
                direct_llm_record.get("input_tokens", 0)
                if self.direct_llm is not None and direct_llm_record
                else self.feedback.input_tokens
            ),
            output_tokens=(
                direct_llm_record.get("output_tokens", 0)
                if self.direct_llm is not None and direct_llm_record
                else self.feedback.output_tokens
            ),
            api_cost=(self.feedback.api_cost if self.direct_llm is None else None),
            api_cost_usd=(
                direct_llm_record.get("estimated_api_cost_usd")
                if self.direct_llm is not None and direct_llm_record
                else None
            ),
            audit={
                "context_function": self.context_function.to_record(),
                "feedback_mode": self.feedback.mode,
                "feedback_evaluation_count": self.feedback.call_count,
                "live_llm": self.feedback.is_live_llm or self.direct_llm is not None,
                "llm_error_count": (
                    len(self.direct_llm.errors)
                    if self.direct_llm is not None
                    else len(getattr(self.feedback, "errors", []))
                ),
                "audit_label": self.audit_label,
                "future_order_visibility": False,
                "direct_llm_assignment": direct_llm_record,
                "direct_llm_error_count": (
                    len(self.direct_llm.errors) if self.direct_llm is not None else 0
                ),
                "direct_llm_unknown_cost_count": (
                    self.direct_llm.unknown_cost_count if self.direct_llm is not None else 0
                ),
                "api_cost_currency": (
                    direct_llm_record.get("cost_currency")
                    if direct_llm_record is not None
                    else None
                ),
            },
        )
        if self.retain_round_records:
            snapshot = DispatchInput(
                dispatch_round_id=round_id,
                experiment_run_id="run-seed-{0}".format(self.config.seed),
                snapshot_time_sec=now,
                trigger_type=trigger_type,
                vehicles=copy.deepcopy(self.vehicles),
                orders=copy.deepcopy(
                    [order for order in self.orders if order.status != "future"]
                ),
                candidates=copy.deepcopy(shared_input_candidates),
                max_pickup_wait_sec=self.config.max_pickup_wait_sec,
                post_arrival_boarding_timeout_sec=self.config.post_arrival_boarding_timeout_sec,
                max_next_reservation_per_vehicle=self.config.max_next_reservation_per_vehicle,
                schema_version=self.config.schema_version,
            )
            self.round_records.append(
                DispatchRoundRecord(
                    dispatch_round_id=round_id,
                    trigger_type=trigger_type,
                    snapshot_time_sec=now,
                    input=snapshot,
                    output=copy.deepcopy(output),
                    candidates=copy.deepcopy(shared_input_candidates),
                )
            )
        self.metrics["candidate_before"] += before_count
        self.metrics["candidate_after"] += after_count
        self.metrics["fallback_count"] += int(output.fallback_used)
        self.metrics["rounds"] += 1
        self.metrics["total_decision_ms"] += elapsed_ms
        self.metrics["optimizer_ms"] += optimizer_ms
        self._apply_assignments(selected, now)

    def run(
        self, max_dispatch_rounds: Optional[int] = None
    ) -> Tuple[ExperimentSummary, List[DispatchRoundRecord]]:
        if max_dispatch_rounds is not None and max_dispatch_rounds <= 0:
            raise ValueError("max_dispatch_rounds must be positive")
        now = 0.0
        initial_trigger = "initial"
        while True:
            next_time = self._next_event_time(now)
            if next_time is None:
                break
            if next_time < now - 1e-9:
                raise RuntimeError("event clock moved backwards")
            now = next_time
            arrival_count = self._release_arrivals(now)
            vehicle_triggers = self._process_vehicle_events(now)
            expired = self._expire_waiting(now)
            triggers = []
            if arrival_count:
                triggers.append("new_order")
            triggers.extend(vehicle_triggers)
            if expired:
                triggers.append("pickup_timeout")
            if not triggers:
                triggers.append(initial_trigger)
            self._run_dispatch(now, "+".join(triggers))
            if max_dispatch_rounds is not None and self.metrics["rounds"] >= max_dispatch_rounds:
                break
            initial_trigger = "periodic"
            # Once the horizon has passed, no future order can arrive.  The
            # loop still drains active trips and waiting-order deadlines.
            if now > self.config.horizon_sec + self.config.max_pickup_wait_sec + 3600:
                break

        waits = [
            order.pickup_arrival_time_sec - order.arrival_time_sec
            for order in self.orders
            if order.status in ("boarded", "completed") and order.pickup_arrival_time_sec is not None
        ]
        status_counts: Dict[str, int] = {}
        for order in self.orders:
            status_counts[order.status] = status_counts.get(order.status, 0) + 1
        served = sum(1 for order in self.orders if order.status == "completed")
        unserved = sum(1 for order in self.orders if order.status == "unserved")
        cancelled = sum(1 for order in self.orders if order.status == "cancelled")
        total_empty = sum(vehicle.total_empty_distance_m for vehicle in self.vehicles)
        total_energy = sum(vehicle.total_energy_kwh for vehicle in self.vehicles)
        summary = ExperimentSummary(
            experiment_run_id="run-seed-{0}".format(self.config.seed),
            method_name=self.config.method_name,
            seed=self.config.seed,
            map_id=self.road_map.map_id,
            vehicle_count=len(self.vehicles),
            order_count=len(self.orders),
            served_orders=served,
            unserved_orders=unserved,
            cancelled_orders=cancelled,
            service_rate=served / max(1, len(self.orders)),
            average_pickup_wait_sec=mean(waits) if waits else None,
            p95_pickup_wait_sec=_percentile(waits, 0.95),
            max_pickup_wait_sec=max(waits) if waits else None,
            total_empty_distance_m=total_empty,
            total_energy_kwh=total_energy,
            total_dispatch_rounds=self.metrics["rounds"],
            total_llm_calls=(
                self.direct_llm.call_count
                if self.direct_llm is not None
                else (self.feedback.call_count if self.feedback.is_live_llm else 0)
            ),
            total_llm_input_tokens=(
                self.direct_llm.input_tokens
                if self.direct_llm is not None and self.direct_llm.input_usage_call_count == self.direct_llm.call_count
                else None
            ),
            total_llm_cached_input_tokens=(
                self.direct_llm.cached_input_tokens
                if self.direct_llm is not None and self.direct_llm.cached_input_usage_call_count == self.direct_llm.call_count
                else None
            ),
            total_llm_output_tokens=(
                self.direct_llm.output_tokens
                if self.direct_llm is not None and self.direct_llm.output_usage_call_count == self.direct_llm.call_count
                else None
            ),
            total_llm_tokens=(
                self.direct_llm.input_tokens + self.direct_llm.output_tokens
                if self.direct_llm is not None
                and self.direct_llm.input_usage_call_count == self.direct_llm.call_count
                and self.direct_llm.output_usage_call_count == self.direct_llm.call_count
                else None
            ),
            llm_usage_status=(
                "available_best_effort"
                if self.direct_llm is not None
                and self.direct_llm.usage_call_count == self.direct_llm.call_count
                and self.direct_llm.input_usage_call_count == self.direct_llm.call_count
                and self.direct_llm.output_usage_call_count == self.direct_llm.call_count
                and self.direct_llm.call_count
                else ("missing_or_partial" if self.direct_llm is not None and self.direct_llm.call_count else "not_recorded")
            ),
            total_codex_cli_invocations=(
                self.direct_llm.call_count
                if self.direct_llm is not None and getattr(self.direct_llm, "backend", "codex_cli") == "codex_cli"
                else 0
            ),
            total_api_cost=(self.feedback.api_cost if self.direct_llm is None else None),
            total_api_cost_usd=(
                (
                    None
                    if self.direct_llm.unknown_cost_count
                    else self.direct_llm.api_cost_usd
                )
                if self.direct_llm is not None
                else None
            ),
            total_codex_standard_credit_equivalent=(
                self.direct_llm.codex_standard_credit_equivalent
                if self.direct_llm is not None
                and self.direct_llm.cost_estimate_call_count == self.direct_llm.call_count
                and self.direct_llm.call_count
                else None
            ),
            total_api_equivalent_usd=(
                self.direct_llm.api_equivalent_usd
                if self.direct_llm is not None
                and self.direct_llm.cost_estimate_call_count == self.direct_llm.call_count
                and self.direct_llm.call_count
                else None
            ),
            codex_cost_estimate_status=(
                "estimated_from_official_standard_rate_card"
                if self.direct_llm is not None
                and self.direct_llm.cost_estimate_call_count == self.direct_llm.call_count
                and self.direct_llm.call_count
                else (
                    "missing_or_partial"
                    if self.direct_llm is not None and self.direct_llm.call_count
                    else "not_recorded"
                )
            ),
            total_decision_time_ms=self.metrics["total_decision_ms"],
            optimizer_time_ms=self.metrics["optimizer_ms"],
            candidate_count_before_filter=self.metrics["candidate_before"],
            candidate_count_after_filter=self.metrics["candidate_after"],
            fallback_count=self.metrics["fallback_count"],
            status_counts=status_counts,
        )
        return summary, self.round_records


def run_experiment(
    seed: int = 0,
    method_name: str = "D4_agent_llm_feedback_optimizer",
    map_size: int = 20,
    vehicle_count: int = 10,
    order_count: int = 100,
    feedback_mode: str = "offline_rule",
    llm_backend: str = "codex_cli",
    llm_model: str = "gpt-5.6-luna",
    output_dir: Optional[str] = None,
) -> Dict[str, object]:
    config = DispatchConfig(
        seed=seed,
        method_name=method_name,
        map_size=map_size,
        vehicle_count=vehicle_count,
        order_count=order_count,
        feedback_mode=feedback_mode,
        llm_backend=llm_backend,
        llm_model=llm_model,
    )
    trace_base = Path(output_dir) if output_dir else Path("outputs/dispatch_layer")
    simulation = DispatchSimulation(
        config,
        llm_trace_dir=(trace_base / "codex_cli_traces") if method_name == "D5_pure_llm_direct" else None,
    )
    summary, rounds = simulation.run()
    payload = {
        "schema_version": config.schema_version,
        "config": config.to_dict(),
        "scenario": {
            "hotspots": simulation.hotspots,
            "pickup_sampling_mode": config.pickup_sampling_mode,
            "road_state_change_enabled": False,
        },
        "map": simulation.road_map.to_dict(),
        "orders": [order.to_dict() for order in simulation.orders],
        "vehicles": [vehicle.to_dict() for vehicle in simulation.vehicles],
        "summary": summary.to_dict(),
        "rounds": [record.to_dict() for record in rounds],
    }
    scenario_identity = {
        "map": payload["map"],
        "hotspots": payload["scenario"]["hotspots"],
        "orders": [
            {
                "order_id": order.order_id,
                "arrival_time_sec": order.arrival_time_sec,
                "pickup_node_id": order.pickup_node_id,
                "dropoff_node_id": order.dropoff_node_id,
                "hotspot_id": order.hotspot_id,
                "passenger_no_show": order.passenger_no_show,
            }
            for order in simulation.orders
        ],
        "vehicle_initial_positions": [
            {
                "vehicle_id": vehicle.vehicle_id,
                "initial_position_node_id": vehicle.initial_position_node_id,
            }
            for vehicle in simulation.vehicles
        ],
    }
    payload["scenario_fingerprint_sha256"] = hashlib.sha256(
        json.dumps(scenario_identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if output_dir:
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        result_path = path / "{0}_seed_{1}.json".format(method_name, seed)
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["result_path"] = str(result_path)
    return payload

