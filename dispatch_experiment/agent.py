"""Vehicle-agent candidate generation, context filtering and feedback hooks."""

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Candidate, CandidateDecision, DispatchConfig, Order, Vehicle
from .route_oracle import GraphRouteOracle


FEATURE_NAMES = (
    "local_order_density",
    "local_idle_vehicle_density",
    "demand_supply_ratio",
    "commitment_slack",
    "candidate_eta",
    "time_context",
    "vehicle_state_code",
)


@dataclass
class ContextFunction:
    """Bounded, auditable threshold function.

    LLM implementations may supply the seven coefficients through the same
    interface, but they cannot execute arbitrary code or change hard rules.
    """

    mode: str = "manual_linear_clip"
    version: str = "context-v1"
    base: float = 240.0
    weights: Tuple[float, ...] = (-20.0, 30.0, -5.0, 15.0, -60.0, 0.0, 0.0)
    min_threshold_sec: float = 120.0
    max_threshold_sec: float = 300.0

    def set_parameters(self, base: float, weights: Sequence[float], *, mode: str, version: str) -> None:
        if len(weights) != len(FEATURE_NAMES):
            raise ValueError("context_weight_count_mismatch")
        self.base = float(base)
        self.weights = tuple(float(value) for value in weights)
        self.mode = mode
        self.version = version

    def threshold(self, features: Dict[str, float]) -> float:
        values = [features.get(name, 0.0) for name in FEATURE_NAMES]
        raw = self.base + sum(weight * value for weight, value in zip(self.weights, values))
        return max(self.min_threshold_sec, min(self.max_threshold_sec, raw))

    def to_record(self) -> Dict[str, object]:
        return {
            "function_id": "linear_clip",
            "function_mode": self.mode,
            "function_version": self.version,
            "feature_names": list(FEATURE_NAMES),
            "formula": "clip(base + sum(w_i * feature_i), min_threshold, max_threshold)",
            "parameters": {
                "base": self.base,
                "weights": list(self.weights),
            },
            "min_threshold_sec": self.min_threshold_sec,
            "max_threshold_sec": self.max_threshold_sec,
        }


class CandidateFeedback:
    """Structured feedback interface.

    ``offline_rule`` is the reproducible default.  A real LLM adapter can
    implement the same method and return only the fixed decision fields.
    """

    def __init__(self, mode: str = "offline_rule"):
        self.mode = mode
        self.is_live_llm = False
        self.call_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.api_cost = 0.0

    def evaluate(self, candidate: Candidate, max_wait_sec: float) -> Dict[str, object]:
        if self.mode in ("none", "disabled"):
            return {"decision": "retain", "risk": 0.0, "reason": "feedback_disabled"}
        self.call_count += 1
        # This is intentionally a transparent deterministic surrogate for an
        # LLM.  It gives the D4 pipeline a reproducible control path without
        # claiming that a model has been evaluated.
        ratio = candidate.predicted_wait_sec / max_wait_sec if max_wait_sec else 1.0
        risk = max(
            0.0,
            min(1.0, ratio * 0.75 + (0.25 if not candidate.current_commitment_ok else 0.0)),
        )
        if ratio > 0.95:
            decision = "review"
            reason = "near_wait_limit"
        elif candidate.empty_distance_m > 2500.0:
            decision = "review"
            reason = "long_empty_leg"
        else:
            decision = "retain"
            reason = "within_structured_limits"
        return {"decision": decision, "risk": risk, "reason": reason}


def _vehicle_state_code(vehicle: Vehicle) -> float:
    return {
        "idle": 0.0,
        "pickup_in_progress": 1.0,
        "occupied": 2.0,
        "reserved": 3.0,
        "exception": 4.0,
    }.get(vehicle.state, 5.0)


def _features(
    now: float,
    config: DispatchConfig,
    waiting_orders: Sequence[Order],
    vehicles: Sequence[Vehicle],
    vehicle: Vehicle,
    pickup_duration_sec: float,
) -> Dict[str, float]:
    waiting = len(waiting_orders)
    idle = sum(1 for item in vehicles if item.state == "idle")
    return {
        "local_order_density": min(10.0, waiting / 10.0),
        "local_idle_vehicle_density": idle / max(1.0, float(config.vehicle_count)),
        "demand_supply_ratio": min(10.0, waiting / max(1.0, float(idle))),
        "commitment_slack": max(
            0.0,
            min(1.0, (vehicle.available_time_sec - now) / config.max_pickup_wait_sec),
        ),
        "candidate_eta": max(0.0, min(2.0, pickup_duration_sec / config.max_pickup_wait_sec)),
        "time_context": max(0.0, min(1.0, now / config.horizon_sec)),
        "vehicle_state_code": _vehicle_state_code(vehicle),
    }


def _candidate_hard_reason(candidate: Candidate) -> Optional[str]:
    if not candidate.pickup_leg.reachable:
        return candidate.pickup_leg.reason or "pickup_route_unreachable"
    if not candidate.ride_leg.reachable:
        return candidate.ride_leg.reason or "ride_route_unreachable"
    if not candidate.max_wait_ok:
        return "max_pickup_wait_exceeded"
    if not candidate.current_commitment_ok:
        return "current_commitment_conflict"
    if not candidate.one_to_one_ok:
        return "one_to_one_conflict"
    if not candidate.time_non_overlap_ok:
        return "time_overlap"
    return None


class VehicleAgent:
    """One vehicle's local proposal loop."""

    def __init__(self, vehicle_id: str):
        self.vehicle_id = vehicle_id

    def propose(
        self,
        dispatch_round_id: str,
        now: float,
        vehicle: Vehicle,
        orders: Sequence[Order],
        vehicles: Sequence[Vehicle],
        oracle: GraphRouteOracle,
        config: DispatchConfig,
        context_function: Optional[ContextFunction],
        use_context: bool,
    ) -> Tuple[List[Candidate], List[Candidate], List[CandidateDecision]]:
        if vehicle.reserved_order_id:
            return [], [], []
        if vehicle.state not in ("idle", "occupied"):
            return [], [], []
        candidate_type = "immediate_pickup" if vehicle.state == "idle" else "next_order_handoff"
        source_node_id = vehicle.position_node_id
        start_time = max(now, vehicle.available_time_sec)
        if vehicle.state == "occupied" and vehicle.current_dropoff_node_id:
            source_node_id = vehicle.current_dropoff_node_id

        waiting = [order for order in orders if order.status == "waiting" and order.arrival_time_sec <= now]
        candidates: List[Candidate] = []
        decisions: List[CandidateDecision] = []
        hard_candidates: List[Candidate] = []
        filtered_candidates: List[Candidate] = []

        for order in waiting:
            pickup_route = getattr(oracle, "pickup_route", oracle.route)
            ride_route = getattr(oracle, "ride_route", oracle.route)
            pickup_leg = pickup_route(source_node_id, order.pickup_node_id)
            ride_leg = ride_route(order.pickup_node_id, order.dropoff_node_id)
            predicted_pickup = start_time + pickup_leg.duration_sec if pickup_leg.reachable else math.inf
            predicted_dropoff = predicted_pickup + ride_leg.duration_sec if ride_leg.reachable else math.inf
            wait = max(0.0, predicted_pickup - order.arrival_time_sec)
            route_ok = pickup_leg.reachable and ride_leg.reachable
            hard_ok = (
                route_ok
                and wait <= config.max_pickup_wait_sec
                and vehicle.reserved_order_id is None
                and vehicle.state in ("idle", "occupied")
            )
            candidate = Candidate(
                candidate_id="{0}__{1}__{2}".format(dispatch_round_id, vehicle.vehicle_id, order.order_id),
                dispatch_round_id=dispatch_round_id,
                vehicle_id=vehicle.vehicle_id,
                order_id=order.order_id,
                candidate_type=candidate_type,
                route_reachability=route_ok,
                pickup_leg=pickup_leg,
                ride_leg=ride_leg,
                vehicle_available_time_sec=start_time,
                predicted_pickup_time_sec=predicted_pickup,
                predicted_dropoff_time_sec=predicted_dropoff,
                predicted_wait_sec=wait,
                empty_distance_m=pickup_leg.distance_m,
                total_distance_m=pickup_leg.distance_m + ride_leg.distance_m,
                energy_kwh=(pickup_leg.distance_m + ride_leg.distance_m) / 1000.0 * config.moving_kwh_per_km,
                current_commitment_ok=True,
                one_to_one_ok=True,
                time_non_overlap_ok=True,
                max_wait_ok=wait <= config.max_pickup_wait_sec,
                hard_constraint_ok=hard_ok,
            )
            candidate.feature_vector = _features(
                now, config, waiting, vehicles, vehicle, pickup_leg.duration_sec
            )
            if use_context and context_function is not None:
                candidate.context_threshold_sec = context_function.threshold(candidate.feature_vector)
                candidate.context_pass = wait <= candidate.context_threshold_sec
            candidate.rejection_reason = _candidate_hard_reason(candidate)
            candidates.append(candidate)
            if hard_ok:
                hard_candidates.append(candidate)
                if (not use_context) or candidate.context_pass:
                    filtered_candidates.append(candidate)
                    decisions.append(
                        CandidateDecision(
                            candidate_id=candidate.candidate_id,
                            agent_id=self.vehicle_id,
                            filter_stage="after_function" if use_context else "after_hard_check",
                            agent_decision="retain",
                            hard_constraint_pass=True,
                            context_threshold_sec=candidate.context_threshold_sec,
                            context_pass=candidate.context_pass,
                            reason="passed_hard_and_context_checks",
                        )
                    )
                else:
                    decisions.append(
                        CandidateDecision(
                            candidate_id=candidate.candidate_id,
                            agent_id=self.vehicle_id,
                            filter_stage="after_function",
                            agent_decision="reject",
                            hard_constraint_pass=True,
                            context_threshold_sec=candidate.context_threshold_sec,
                            context_pass=False,
                            reason="context_threshold_rejected",
                        )
                    )
            else:
                decisions.append(
                    CandidateDecision(
                        candidate_id=candidate.candidate_id,
                        agent_id=self.vehicle_id,
                        filter_stage="after_hard_check",
                        agent_decision="reject",
                        hard_constraint_pass=False,
                        context_threshold_sec=candidate.context_threshold_sec,
                        context_pass=candidate.context_pass,
                        reason=candidate.rejection_reason or "hard_constraint_rejected",
                    )
                )

        if use_context and hard_candidates and not filtered_candidates:
            # The fallback expands only to candidates that already satisfy the
            # hard rules.  It never relaxes the service promise.
            filtered_candidates = list(hard_candidates)
            for candidate in filtered_candidates:
                decisions.append(
                    CandidateDecision(
                        candidate_id=candidate.candidate_id,
                        agent_id=self.vehicle_id,
                        filter_stage="fallback_retain",
                        agent_decision="fallback_retain",
                        hard_constraint_pass=True,
                        context_threshold_sec=candidate.context_threshold_sec,
                        context_pass=candidate.context_pass,
                        fallback_used=True,
                        fallback_reason="no_candidate_after_context_filter",
                    )
                )
        return candidates, filtered_candidates, decisions


def apply_feedback(
    candidates: Sequence[Candidate],
    feedback: CandidateFeedback,
    max_wait_sec: float,
) -> List[Candidate]:
    retained: List[Candidate] = []
    for candidate in candidates:
        result = feedback.evaluate(candidate, max_wait_sec)
        candidate.feedback_decision = str(result.get("decision", "retain"))
        candidate.feedback_risk = float(result.get("risk", 0.0))
        candidate.feedback_reason = str(result.get("reason", ""))
        # Review is retained and remains subject to the optimizer.  Feedback
        # may remove a candidate only when it explicitly rejects it.
        if candidate.feedback_decision != "reject":
            retained.append(candidate)
    return retained

