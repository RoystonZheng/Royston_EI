#!/usr/bin/env python3
"""Recalculate saved NYC dispatch assignments with ev_longitudinal_v2.

This is a post-hoc energy accounting pass. It reads completed result files,
reconstructs only the routes for assignments already recorded in each round
audit, and writes versioned outputs separately from the legacy results. It
never runs a dispatch policy or calls a model.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT))

from dispatch_experiment.nyc_tlc_scenario import (  # noqa: E402
    NYCTLCZoneCentroidPickupOracle,
    load_dispatch_scenario,
    sha256_file,
)
from route_experiment.energy_model import (  # noqa: E402
    EnergyModelConfig,
    evaluate_route_edges,
)
from route_experiment.models import EvaluationConfig  # noqa: E402


DEFAULT_PACKAGE = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_formal_multimonth_v1"
)
DEFAULT_RESULT_BASE = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_v1"
)
DEFAULT_REPLICATE_ROOT = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_replicates_v1"
)
DEFAULT_OUTPUT = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_energy_v2_offline_20261026"
)
EXPECTED_PROFILE_SHA256 = (
    "b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee"
)
RECALCULATION_METHOD = "fixed_assignment_route_reconstruction_v1_virtual_pickup_segment"
LEGACY_KWH_PER_KM = 0.18
DISPATCH_METHODS = [
    "D0_greedy",
    "D1_optimizer",
    "D2_agent_optimizer",
    "D5_pure_llm_direct",
    "D6_agent_codex_context_optimizer",
    "D7_agent_codex_capacity_shield_optimizer",
    "D8_agent_codex_nn_capacity_shield_optimizer",
]
MIXED_CELLS = {("3", "EV_20240521_210000_q50", "F4")}


def canonical_hash(payload: Any) -> str:
    body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


@dataclass
class VehicleState:
    position_node_id: str
    state: str = "idle"
    current_order_id: Optional[str] = None
    current_pickup_node_id: Optional[str] = None
    current_dropoff_node_id: Optional[str] = None
    planned_pickup_time_sec: Optional[float] = None
    planned_dropoff_time_sec: Optional[float] = None
    boarding_deadline_time_sec: Optional[float] = None
    reserved_order_id: Optional[str] = None
    reserved_candidate: Optional[Dict[str, Any]] = None
    available_time_sec: float = 0.0


def _selected_pairs(record: Dict[str, Any]) -> List[Tuple[float, str, str, str]]:
    """Return (time, round id, vehicle id, order id) from saved outputs."""

    rows = record.get("round_audits") or []
    selected: List[Tuple[float, str, str, str]] = []
    if rows:
        for row in rows:
            vehicles = row.get("selected_vehicle_ids") or []
            orders = row.get("selected_order_ids") or []
            count = int(row.get("selected_assignment_count", len(vehicles)))
            if len(vehicles) != len(orders) or len(vehicles) != count:
                raise ValueError(
                    "selected_vehicle_order_pairing_missing:" + str(row.get("dispatch_round_id"))
                )
            for vehicle_id, order_id in zip(vehicles, orders):
                selected.append(
                    (
                        float(row["snapshot_time_sec"]),
                        str(row["dispatch_round_id"]),
                        str(vehicle_id),
                        str(order_id),
                    )
                )
        return selected

    # Full-record outputs preserve assignment pairs directly.
    for row in record.get("rounds") or []:
        input_row = row.get("input") or {}
        output = row.get("output") or {}
        assignments = output.get("assignments") or []
        for assignment in assignments:
            selected.append(
                (
                    float(input_row["snapshot_time_sec"]),
                    str(output.get("dispatch_round_id", row.get("dispatch_round_id", ""))),
                    str(assignment["vehicle_id"]),
                    str(assignment["order_id"]),
                )
            )
    if not record.get("rounds"):
        raise ValueError("no_saved_round_audits_or_full_round_records")
    return selected


def _evaluate_assignment_energy(
    pickup_leg: Any,
    ride_leg: Any,
    road_map: Any,
    profile: EnergyModelConfig,
    evaluation_config: EvaluationConfig,
) -> Dict[str, Any]:
    edge_by_id = {edge.edge_id: edge for edge in road_map.edges}

    # TLC pickup legs are synthetic centroid distances and have no road-edge
    # sequence. Represent that saved route proxy as one flat virtual segment at
    # its frozen 5 m/s policy speed so its distance is not silently assigned
    # zero energy. This remains an estimate without a measured pickup trace.
    if pickup_leg.distance_m > 0:
        pickup_edges: Sequence[Any] = [
            {
                "edge_id": "synthetic_pickup_proxy",
                "length_m": float(pickup_leg.distance_m),
                "base_speed_mps": float(pickup_leg.distance_m)
                / max(float(pickup_leg.duration_sec), 1e-12),
                "congestion_level": 0,
                "signal_delay_sec": 0.0,
                "slope_rad": None,
            }
        ]
    else:
        pickup_edges = []
    ride_edges = []
    for edge_id in ride_leg.edge_sequence:
        edge = edge_by_id.get(edge_id)
        if edge is None:
            raise ValueError("saved_ride_edge_missing:" + str(edge_id))
        ride_edges.append(edge)

    pickup = evaluate_route_edges(
        pickup_edges,
        evaluation_config.congestion_speed_multipliers,
        profile,
        payload_mass_kg=0.0,
    )
    ride = evaluate_route_edges(
        ride_edges,
        evaluation_config.congestion_speed_multipliers,
        profile,
        payload_mass_kg=profile.passenger_load_kg,
    )
    return {
        "net_kwh": pickup.net_kwh + ride.net_kwh,
        "pickup": asdict(pickup),
        "ride": asdict(ride),
    }


def _activate(
    vehicle: VehicleState,
    order: Any,
    start_time: float,
    pickup_duration: float,
    ride_duration: float,
) -> None:
    vehicle.current_order_id = order.order_id
    vehicle.current_pickup_node_id = order.pickup_node_id
    vehicle.current_dropoff_node_id = order.dropoff_node_id
    vehicle.state = "pickup_in_progress"
    vehicle.planned_pickup_time_sec = start_time + pickup_duration
    vehicle.planned_dropoff_time_sec = start_time + pickup_duration + ride_duration
    vehicle.boarding_deadline_time_sec = None
    vehicle.available_time_sec = vehicle.planned_dropoff_time_sec


def _advance_vehicle_events(
    timestamp: float,
    vehicles: Dict[str, VehicleState],
    orders: Dict[str, Any],
    boarding_timeout_sec: float,
) -> None:
    """Mirror the simulator's deterministic vehicle event transitions."""

    for vehicle in vehicles.values():
        if (
            vehicle.state == "pickup_in_progress"
            and vehicle.planned_pickup_time_sec is not None
            and vehicle.planned_pickup_time_sec <= timestamp + 1e-9
        ):
            current_order = orders[vehicle.current_order_id]
            current_order.status = "arrived_waiting" if current_order.passenger_no_show else "boarded"
            if current_order.passenger_no_show:
                vehicle.state = "arrived_waiting"
                vehicle.boarding_deadline_time_sec = timestamp + boarding_timeout_sec
                vehicle.available_time_sec = vehicle.boarding_deadline_time_sec
            else:
                vehicle.state = "occupied"

        if (
            vehicle.state == "arrived_waiting"
            and vehicle.boarding_deadline_time_sec is not None
            and vehicle.boarding_deadline_time_sec <= timestamp + 1e-9
        ):
            order = orders[vehicle.current_order_id]
            order.status = "cancelled"
            vehicle.position_node_id = order.pickup_node_id
            vehicle.current_order_id = None
            vehicle.current_pickup_node_id = None
            vehicle.current_dropoff_node_id = None
            vehicle.planned_pickup_time_sec = None
            vehicle.planned_dropoff_time_sec = None
            vehicle.boarding_deadline_time_sec = None
            vehicle.available_time_sec = 0.0
            vehicle.state = "idle"

        if (
            vehicle.state == "occupied"
            and vehicle.planned_dropoff_time_sec is not None
            and vehicle.planned_dropoff_time_sec <= timestamp + 1e-9
        ):
            completed_order = orders[vehicle.current_order_id]
            completed_order.status = "completed"
            vehicle.position_node_id = completed_order.dropoff_node_id
            vehicle.current_order_id = None
            vehicle.current_pickup_node_id = None
            vehicle.current_dropoff_node_id = None
            vehicle.planned_pickup_time_sec = None
            vehicle.planned_dropoff_time_sec = None
            vehicle.boarding_deadline_time_sec = None
            vehicle.available_time_sec = timestamp
            if vehicle.reserved_order_id is not None:
                reserved = orders[vehicle.reserved_order_id]
                route = vehicle.reserved_candidate
                if route is None:
                    raise ValueError("reserved_order_missing_saved_route")
                vehicle.reserved_order_id = None
                vehicle.reserved_candidate = None
                reserved.status = "assigned"
                _activate(
                    vehicle,
                    reserved,
                    timestamp,
                    route["pickup_duration_sec"],
                    route["ride_duration_sec"],
                )
            else:
                vehicle.state = "idle"


def recalculate_one(
    result_path: Path,
    result_root: Path,
    package: Path,
    output_dir: Path,
    seed: int,
    profile: EnergyModelConfig,
    evaluation_config: EvaluationConfig,
    scenario_cache: Dict[Tuple[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    record = json.loads(result_path.read_text(encoding="utf-8"))
    if record.get("run_mode") != "complete_simulation" or record.get("partial_run"):
        raise ValueError("result_is_not_complete_simulation")
    if record.get("round_validation_errors"):
        raise ValueError("saved_round_validation_errors_present")
    if not record.get("summary") or record["summary"].get("total_dispatch_rounds", 0) <= 0:
        raise ValueError("summary_missing_or_has_no_dispatch_rounds")

    case_id = str(record["case_id"])
    fleet_id = str(record["fleet_id"])
    method = str(record["method_name"])
    cache_key = (case_id, fleet_id)
    loaded = scenario_cache.get(cache_key)
    if loaded is None:
        manifest = json.loads((package / "benchmark_manifest.json").read_text(encoding="utf-8"))
        case_entry = manifest["cases"].get(case_id)
        if case_entry is None:
            raise ValueError("case_missing_from_frozen_package:" + case_id)
        case_dir = package / case_entry["path"]
        loaded = load_dispatch_scenario(
            case_dir,
            case_dir / "zone_od_cost_matrix.csv.gz",
            fleet_id=fleet_id,
        )
        policy = json.loads((package / "pickup_cost_policy.json").read_text(encoding="utf-8"))
        centroid_payload = json.loads((package / "zone_centroids.json").read_text(encoding="utf-8"))
        loaded["oracle"] = NYCTLCZoneCentroidPickupOracle(
            loaded["road_map"],
            loaded["od_costs"],
            {
                int(zone_id): (float(point["lon"]), float(point["lat"]))
                for zone_id, point in centroid_payload.items()
            },
            speed_mps=float(policy["speed_mps"]),
            detour_factor=float(policy["detour_factor"]),
            minimum_intrazonal_distance_m=float(policy["minimum_intrazonal_distance_m"]),
        )
        loaded["cohort"] = case_entry.get("cohort", "unknown")
        scenario_cache[cache_key] = loaded

    expected_hashes = record.get("input_hashes") or {}
    actual_hashes = {
        "scenario_json": loaded["scenario_metadata_sha256"],
        "orders_json": loaded["orders_json_sha256"],
        "zone_od_cost_matrix": loaded["cost_matrix_sha256"],
    }
    if expected_hashes != actual_hashes:
        raise ValueError("frozen_input_hash_mismatch")

    # The cached scenario is shared across methods, while replay changes order
    # statuses. Each method needs its own pristine order objects.
    orders = {
        order.order_id: order
        for order in copy.deepcopy(loaded["orders"])
    }
    vehicles: Dict[str, VehicleState] = {
        vehicle.vehicle_id: VehicleState(position_node_id=vehicle.position_node_id)
        for vehicle in loaded["vehicles"]
    }
    max_wait = float(record.get("config", {}).get("max_pickup_wait_sec", 300.0))
    boarding_timeout = float(
        record.get("config", {}).get("post_arrival_boarding_timeout_sec", 180.0)
    )
    pairs = _selected_pairs(record)
    audit_rows = record.get("round_audits") or []
    if audit_rows and len(audit_rows) != int(record["summary"]["total_dispatch_rounds"]):
        raise ValueError("saved_round_audit_count_mismatch")
    times = [float(row["snapshot_time_sec"]) for row in audit_rows]
    if any(right + 1e-9 < left for left, right in zip(times, times[1:])):
        raise ValueError("saved_round_times_not_monotonic")

    detail_by_round: Dict[str, List[Tuple[str, str]]] = {}
    if audit_rows:
        for row in audit_rows:
            detail_by_round[str(row["dispatch_round_id"])] = list(
                zip(row.get("selected_vehicle_ids") or [], row.get("selected_order_ids") or [])
            )
        indexed_pairs = {
            (round_id, vehicle_id, order_id)
            for _, round_id, vehicle_id, order_id in pairs
        }
        expected_pairs = {
            (round_id, str(vehicle_id), str(order_id))
            for round_id, assignment_pairs in detail_by_round.items()
            for vehicle_id, order_id in assignment_pairs
        }
        if indexed_pairs != expected_pairs:
            raise ValueError("saved_assignment_pairing_reconciliation_failed")

    energy_totals = Counter()
    assignments: List[Dict[str, Any]] = []
    empty_distance_total = 0.0
    legacy_energy_total = 0.0
    assigned_order_ids = set()
    assigned_vehicle_rounds = set()

    if audit_rows:
        rows_by_round = {str(row["dispatch_round_id"]): row for row in audit_rows}
        dispatch_pairs = [
            (float(row["snapshot_time_sec"]), str(row["dispatch_round_id"]),
             str(vehicle_id), str(order_id))
            for row in audit_rows
            for vehicle_id, order_id in zip(
                row.get("selected_vehicle_ids") or [], row.get("selected_order_ids") or []
            )
        ]
    else:
        dispatch_pairs = pairs
        rows_by_round = {}

    if audit_rows:
        normalized_rounds = [
            (
                float(row["snapshot_time_sec"]),
                str(row["dispatch_round_id"]),
                [
                    (str(vehicle_id), str(order_id))
                    for vehicle_id, order_id in zip(
                        row.get("selected_vehicle_ids") or [],
                        row.get("selected_order_ids") or [],
                    )
                ],
            )
            for row in audit_rows
        ]
    else:
        normalized_rounds = []
        grouped: Dict[str, List[Tuple[float, str, str, str]]] = {}
        for pair in dispatch_pairs:
            grouped.setdefault(pair[1], []).append(pair)
        for round_id, round_pairs in grouped.items():
            timestamp = round_pairs[0][0]
            normalized_rounds.append(
                (timestamp, round_id, [(item[2], item[3]) for item in round_pairs])
            )

    for timestamp, round_id, assignment_pairs in normalized_rounds:
        # The original event loop releases arrivals, processes vehicle events,
        # expires waiting orders, and then dispatches at this timestamp.
        for order in orders.values():
            if order.status == "future" and order.arrival_time_sec <= timestamp + 1e-9:
                order.status = "waiting"
        _advance_vehicle_events(timestamp, vehicles, orders, boarding_timeout)
        for order in orders.values():
            if (
                order.status == "waiting"
                and timestamp >= order.arrival_time_sec + max_wait - 1e-9
            ):
                order.status = "unserved"

        for vehicle_id, order_id in assignment_pairs:
            if vehicle_id not in vehicles or order_id not in orders:
                raise ValueError("selected_vehicle_or_order_missing")
            vehicle = vehicles[vehicle_id]
            order = orders[order_id]
            if order.status != "waiting":
                raise ValueError(
                    "selected_order_not_waiting:" + order_id + ":" + str(order.status)
                )
            if vehicle.state not in ("idle", "occupied") or vehicle.reserved_order_id:
                raise ValueError("selected_vehicle_not_dispatchable:" + vehicle_id)
            if order_id in assigned_order_ids:
                raise ValueError("order_selected_more_than_once:" + order_id)
            if (round_id, vehicle_id) in assigned_vehicle_rounds:
                raise ValueError("vehicle_selected_more_than_once_in_round:" + vehicle_id)

            candidate_type = "immediate_pickup" if vehicle.state == "idle" else "next_order_handoff"
            source_node = (
                vehicle.position_node_id
                if vehicle.state == "idle"
                else str(vehicle.current_dropoff_node_id)
            )
            start_time = max(timestamp, vehicle.available_time_sec)
            pickup_leg = loaded["oracle"].pickup_route(source_node, order.pickup_node_id)
            ride_leg = loaded["oracle"].ride_route(order.pickup_node_id, order.dropoff_node_id)
            if not pickup_leg.reachable or not ride_leg.reachable:
                raise ValueError("selected_route_unreachable:" + order_id)
            energy = _evaluate_assignment_energy(
                pickup_leg,
                ride_leg,
                loaded["road_map"],
                profile,
                evaluation_config,
            )
            old_kwh = (pickup_leg.distance_m + ride_leg.distance_m) / 1000.0 * LEGACY_KWH_PER_KM
            legacy_energy_total += old_kwh
            empty_distance_total += pickup_leg.distance_m
            energy_totals["net_kwh"] += energy["net_kwh"]
            for leg_name in ("pickup", "ride"):
                leg = energy[leg_name]
                for name in (
                    "propulsion_draw_kwh",
                    "regenerated_kwh",
                    "auxiliary_kwh",
                    "rolling_wheel_kwh",
                    "aerodynamic_wheel_kwh",
                    "grade_wheel_kwh",
                    "acceleration_wheel_kwh",
                    "drive_time_sec",
                    "distance_m",
                ):
                    energy_totals[leg_name + "_" + name] += float(leg[name])
                energy_totals["missing_grade_steps"] += int(leg["missing_grade_steps"])
                energy_totals["regen_power_capped_steps"] += int(leg["regen_power_capped_steps"])

            assignment_row = {
                "dispatch_round_id": round_id,
                "snapshot_time_sec": timestamp,
                "vehicle_id": vehicle_id,
                "order_id": order_id,
                "candidate_type": candidate_type,
                "source_node_id": source_node,
                "pickup_route": {
                    "reason": pickup_leg.reason,
                    "distance_m": pickup_leg.distance_m,
                    "duration_sec": pickup_leg.duration_sec,
                    "node_sequence": pickup_leg.node_sequence,
                    "edge_sequence": pickup_leg.edge_sequence,
                    "energy_trace_basis": "synthetic_centroid_proxy_virtual_flat_edge",
                },
                "ride_route": {
                    "distance_m": ride_leg.distance_m,
                    "duration_sec": ride_leg.duration_sec,
                    "node_sequence": ride_leg.node_sequence,
                    "edge_sequence": ride_leg.edge_sequence,
                    "energy_trace_basis": "saved_training_period_zone_od_edge",
                },
                "legacy_energy_kwh_reconstructed": old_kwh,
                "ev_longitudinal_v2_energy": energy,
                "start_time_sec": start_time,
            }
            assignments.append(assignment_row)
            assigned_order_ids.add(order_id)
            assigned_vehicle_rounds.add((round_id, vehicle_id))
            order.status = "reserved" if candidate_type == "next_order_handoff" else "assigned"

            candidate = {
                "pickup_duration_sec": float(pickup_leg.duration_sec),
                "ride_duration_sec": float(ride_leg.duration_sec),
            }
            if candidate_type == "immediate_pickup":
                order.status = "assigned"
                _activate(
                    vehicle,
                    order,
                    timestamp,
                    candidate["pickup_duration_sec"],
                    candidate["ride_duration_sec"],
                )
            else:
                vehicle.reserved_order_id = order.order_id
                vehicle.reserved_candidate = candidate

    # Finish deterministic vehicle state transitions through the final saved
    # event time so the replay can be checked against completion statuses.
    if audit_rows:
        final_time = float(audit_rows[-1]["snapshot_time_sec"])
        for order in orders.values():
            if order.status == "future" and order.arrival_time_sec <= final_time + 1e-9:
                order.status = "waiting"
        _advance_vehicle_events(final_time, vehicles, orders, boarding_timeout)

    stored_energy = float(record["summary"].get("total_energy_kwh") or 0.0)
    stored_empty_distance = float(record["summary"].get("total_empty_distance_m") or 0.0)
    old_energy_delta = legacy_energy_total - stored_energy
    empty_distance_delta = empty_distance_total - stored_empty_distance
    reconciles = (
        abs(old_energy_delta) <= max(1e-6, abs(stored_energy) * 1e-8)
        and abs(empty_distance_delta) <= max(1e-5, abs(stored_empty_distance) * 1e-8)
    )
    if not reconciles:
        raise ValueError(
            "legacy_metric_reconciliation_failed:energy_delta={:.12g};empty_distance_delta={:.12g}".format(
                old_energy_delta, empty_distance_delta
            )
        )

    method_energy_dependency = {
        "D0_greedy": "assignment_rule_does_not_use_energy",
        "D1_optimizer": "optimizer_objective_contains_legacy_energy",
        "D2_agent_optimizer": "optimizer_objective_contains_legacy_energy",
        "D5_pure_llm_direct": "saved_model_input_contains_legacy_energy",
        "D6_agent_codex_context_optimizer": "optimizer_objective_contains_legacy_energy",
        "D7_agent_codex_capacity_shield_optimizer": "optimizer_objective_contains_legacy_energy",
        "D8_agent_codex_nn_capacity_shield_optimizer": "optimizer_objective_contains_legacy_energy",
    }.get(method, "method_requires_manual_dependency_audit")

    output_rel = result_path.relative_to(result_root)
    source_sha = sha256_file(result_path)
    output_record = {
        "schema_version": "nyc_dispatch_energy_recalculation_v1",
        "recalculation_method": RECALCULATION_METHOD,
        "recalculation_script_sha256": sha256_file(Path(__file__).resolve()),
        "status": "offline_recalculated_fixed_legacy_assignments",
        "source_result": str(result_path),
        "source_result_sha256": source_sha,
        "scenario_id": case_id,
        "cohort": loaded.get("cohort", "unknown"),
        "fleet_id": fleet_id,
        "seed": seed,
        "method_name": method,
        "assignment_count": len(assignments),
        "input_hashes": actual_hashes,
        "energy_model": {
            "version": profile.model_version,
            "profile_id": profile.profile_id,
            "profile_sha256": profile.fingerprint(),
            "trace_basis": "zone_od_medians plus synthetic centroid pickup proxy",
            "limitations": [
                "not calibrated to a production vehicle or measured EV telemetry",
                "NYC dataset has no road-level pickup trajectory or time-varying congestion trace",
                "grade and signal delay are missing from zone OD edges; grade is treated as flat",
                "selected dispatches remain the original decisions and may depend on legacy energy",
            ],
        },
        "decision_energy_dependency": method_energy_dependency,
        "legacy_reconciliation": {
            "status": "passed",
            "stored_total_energy_kwh": stored_energy,
            "reconstructed_total_energy_kwh": legacy_energy_total,
            "energy_delta_kwh": old_energy_delta,
            "stored_total_empty_distance_m": stored_empty_distance,
            "reconstructed_total_empty_distance_m": empty_distance_total,
            "empty_distance_delta_m": empty_distance_delta,
        },
        "metrics": {
            "legacy_total_energy_kwh": stored_energy,
            "ev_longitudinal_v2_total_energy_kwh": float(energy_totals["net_kwh"]),
            "ev_longitudinal_v2_energy_delta_vs_legacy_kwh": float(energy_totals["net_kwh"]) - stored_energy,
            "components": dict(energy_totals),
            "original_non_energy_metrics": record["summary"],
        },
        "assignments": assignments,
    }
    output_path = output_dir / f"seed_{seed}" / output_rel.parent / (
        output_rel.stem + ".energy_v2_offline.json"
    )
    write_json(output_path, output_record)
    return {
        "source": str(result_path),
        "source_sha256": source_sha,
        "output": str(output_path),
        "case_id": case_id,
        "fleet_id": fleet_id,
        "seed": seed,
        "method_name": method,
        "status": "recalculated",
        "assignment_count": len(assignments),
        "legacy_energy_kwh": stored_energy,
        "ev_longitudinal_v2_energy_kwh": float(energy_totals["net_kwh"]),
        "decision_energy_dependency": method_energy_dependency,
        "input_hashes": actual_hashes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE))
    parser.add_argument("--seed0-results", default=str(DEFAULT_RESULT_BASE))
    parser.add_argument("--replicate-root", default=str(DEFAULT_REPLICATE_ROOT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2, 3])
    parser.add_argument("--only-result", default=None, help="recalculate one saved result JSON")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    package = Path(args.package).resolve()
    output_dir = Path(args.output_dir).resolve()
    profile = EnergyModelConfig()
    if profile.fingerprint() != EXPECTED_PROFILE_SHA256:
        raise SystemExit("energy profile hash differs from the user-approved profile")
    evaluation_config = EvaluationConfig()

    sources: List[Tuple[int, Path, Path]] = []
    if args.only_result:
        result_path = Path(args.only_result).resolve()
        seed = 0
        for part in result_path.parts:
            if part.startswith("seed_") and part[5:].isdigit():
                seed = int(part[5:])
                break
        result_root = result_path.parent.parent.parent if "evaluation" in result_path.parts else Path(args.seed0_results).resolve()
        sources.append((seed, result_root, result_path))
    else:
        for seed in args.seeds:
            result_root = (
                Path(args.seed0_results).resolve()
                if seed == 0
                else Path(args.replicate_root).resolve() / f"seed_{seed}" / "evaluation"
            )
            if not result_root.exists():
                continue
            for result_path in sorted(result_root.rglob(f"*_seed_{seed}.json")):
                sources.append((seed, result_root, result_path))
    if args.limit is not None:
        sources = sources[: max(0, args.limit)]

    scenario_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
    entries = []
    excluded = []
    failures = []
    benchmark_manifest = json.loads(
        (package / "benchmark_manifest.json").read_text(encoding="utf-8")
    )
    case_cohorts = {
        case_id: item.get("cohort", "unknown")
        for case_id, item in benchmark_manifest.get("cases", {}).items()
    }
    for seed, result_root, result_path in sources:
        try:
            record = json.loads(result_path.read_text(encoding="utf-8"))
            case_id = str(record.get("case_id", ""))
            fleet_id = str(record.get("fleet_id", ""))
            cohort = case_cohorts.get(case_id, "unknown")
            if cohort != "evaluation":
                excluded.append({
                    "source": str(result_path),
                    "seed": seed,
                    "case_id": case_id,
                    "fleet_id": fleet_id,
                    "cohort": cohort,
                    "status": "excluded_non_evaluation_case",
                    "reason": "training cohort is not part of the held-out evaluation denominator",
                })
                continue
            if (str(seed), case_id, fleet_id) in MIXED_CELLS:
                excluded.append({
                    "source": str(result_path),
                    "seed": seed,
                    "case_id": case_id,
                    "fleet_id": fleet_id,
                    "status": "excluded_mixed_code_cell",
                    "reason": "seed 3 F4 cell contains old-code D0/D1/D2 and in-flight v2 D8",
                })
                continue
            output_rel = result_path.relative_to(result_root)
            target_path = output_dir / f"seed_{seed}" / output_rel.parent / (
                output_rel.stem + ".energy_v2_offline.json"
            )
            if target_path.is_file():
                prior = json.loads(target_path.read_text(encoding="utf-8"))
                if (
                    prior.get("source_result_sha256") == sha256_file(result_path)
                    and prior.get("legacy_reconciliation", {}).get("status") == "passed"
                    and prior.get("energy_model", {}).get("profile_sha256") == profile.fingerprint()
                    and prior.get("schema_version") == "nyc_dispatch_energy_recalculation_v1"
                    and all(
                        assignment.get("pickup_route", {}).get("energy_trace_basis")
                        == "synthetic_centroid_proxy_virtual_flat_edge"
                        for assignment in prior.get("assignments", [])
                    )
                ):
                    prior["recalculation_method"] = RECALCULATION_METHOD
                    prior["recalculation_script_sha256"] = sha256_file(Path(__file__).resolve())
                    write_json(target_path, prior)
                    entries.append({
                        "source": str(result_path),
                        "source_sha256": prior["source_result_sha256"],
                        "output": str(target_path),
                        "case_id": case_id,
                        "cohort": cohort,
                        "fleet_id": fleet_id,
                        "seed": seed,
                        "method_name": str(record.get("method_name", "")),
                        "status": "reused_verified_offline_recalculation",
                        "assignment_count": prior.get("assignment_count", 0),
                        "legacy_energy_kwh": prior.get("metrics", {}).get("legacy_total_energy_kwh"),
                        "ev_longitudinal_v2_energy_kwh": prior.get("metrics", {}).get("ev_longitudinal_v2_total_energy_kwh"),
                        "decision_energy_dependency": prior.get("decision_energy_dependency"),
                        "input_hashes": prior.get("input_hashes"),
                    })
                    continue
            entry = recalculate_one(
                result_path,
                result_root,
                package,
                output_dir,
                seed,
                profile,
                evaluation_config,
                scenario_cache,
            )
            entries.append(entry)
        except Exception as error:  # retain exact unrecomputable gaps
            failures.append({
                "source": str(result_path),
                "seed": seed,
                "status": "unrecomputable",
                "reason": str(error),
            })

    indexed_outputs = {
        str(Path(row["output"]).resolve())
        for row in entries
        if row.get("output")
    }
    all_output_files = sorted(output_dir.rglob("*.energy_v2_offline.json"))
    excluded_output_artifacts = []
    output_audit_errors = []
    for output_path in all_output_files:
        resolved_output = str(output_path.resolve())
        if resolved_output in indexed_outputs:
            continue
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            excluded_output_artifacts.append({
                "output": str(output_path),
                "output_sha256": sha256_file(output_path),
                "source_result": payload.get("source_result"),
                "source_result_sha256": payload.get("source_result_sha256"),
                "status": "excluded_preexisting_unindexed_artifact",
                "reason": "not emitted by the current held-out source scan; retained but excluded from recalculation summaries",
            })
        except Exception as error:
            output_audit_errors.append({
                "output": str(output_path),
                "status": "unreadable_unindexed_artifact",
                "reason": str(error),
            })

    seeds_in_scope = sorted(set(args.seeds)) if not args.only_result else sorted(set(row[0] for row in sources))
    evaluation_cases = {
        case_id: item
        for case_id, item in benchmark_manifest.get("cases", {}).items()
        if item.get("cohort") == "evaluation"
    }
    all_seed_summaries = []
    all_index_rows = []
    expected_missing_total = 0
    for seed in seeds_in_scope:
        seed_entries = [row for row in entries if int(row.get("seed", -1)) == seed]
        seed_excluded = [row for row in excluded if int(row.get("seed", -1)) == seed]
        seed_failures = [row for row in failures if int(row.get("seed", -1)) == seed]
        seed_excluded_outputs = [
            row for row in excluded_output_artifacts
            if f"/seed_{seed}/" in str(row.get("output", ""))
        ]
        expected_keys = set()
        expected_cells = set()
        for case_id, item in evaluation_cases.items():
            for fleet_id in item.get("fleet_ids", []):
                expected_cells.add((case_id, fleet_id))
                for method in DISPATCH_METHODS:
                    expected_keys.add((case_id, fleet_id, method))
        excluded_cells = {
            (row.get("case_id"), row.get("fleet_id"))
            for row in seed_excluded
            if row.get("status") == "excluded_mixed_code_cell"
        }
        eligible_keys = {
            key for key in expected_keys if (key[0], key[1]) not in excluded_cells
        }
        present_keys = {
            (row.get("case_id"), row.get("fleet_id"), row.get("method_name"))
            for row in seed_entries
        }
        missing_keys = sorted(eligible_keys - present_keys)
        expected_missing_total += len(missing_keys)
        method_counts = {
            method: sum(1 for row in seed_entries if row.get("method_name") == method)
            for method in DISPATCH_METHODS
        }
        paired_cells = 0
        for case_id, fleet_id in expected_cells - excluded_cells:
            if all((case_id, fleet_id, method) in present_keys for method in DISPATCH_METHODS):
                paired_cells += 1
        seed_output_dir = output_dir / f"seed_{seed}"
        seed_files = sorted(seed_output_dir.rglob("*.energy_v2_offline.json"))
        seed_indexed_paths = {
            str(Path(row["output"]).resolve())
            for row in seed_entries if row.get("output")
        }
        seed_excluded_paths = {
            str(Path(row["output"]).resolve())
            for row in seed_excluded_outputs if row.get("output")
        }
        seed_file_paths = {str(path.resolve()) for path in seed_files}
        seed_coverage_complete = (
            seed_indexed_paths | seed_excluded_paths
        ) == seed_file_paths
        seed_source_count = sum(1 for item in sources if item[0] == seed)
        seed_status = (
            "incomplete_with_expected_gaps"
            if missing_keys or seed_failures
            else ("complete_for_eligible_scope_with_exclusions" if seed_excluded else "complete")
        )
        seed_manifest = {
            "schema_version": "nyc_dispatch_energy_recalculation_seed_manifest_v1",
            "status": seed_status,
            "seed": seed,
            "created_at_local": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
            "scope": "held-out evaluation results for this seed only; no policy rerun or model calls",
            "energy_model": {
                "version": profile.model_version,
                "profile_id": profile.profile_id,
                "profile_sha256": profile.fingerprint(),
            },
            "recalculation_method": RECALCULATION_METHOD,
            "recalculation_script_sha256": sha256_file(Path(__file__).resolve()),
            "source_result_count": seed_source_count,
            "recalculated_output_count": len(seed_entries),
            "newly_recalculated_this_invocation_count": sum(
                1 for row in seed_entries if row.get("status") == "recalculated"
            ),
            "reused_verified_output_count": sum(
                1 for row in seed_entries
                if row.get("status") == "reused_verified_offline_recalculation"
            ),
            "evaluation_method_counts": method_counts,
            "expected_evaluation_cells": len(expected_cells),
            "excluded_mixed_cells": [
                {"case_id": case_id, "fleet_id": fleet_id}
                for case_id, fleet_id in sorted(excluded_cells)
            ],
            "eligible_evaluation_cells": len(expected_cells - excluded_cells),
            "paired_completed_cells": paired_cells,
            "eligible_expected_method_results": len(eligible_keys),
            "missing_expected_method_result_count": len(missing_keys),
            "missing_by_method": {
                method: sum(1 for key in missing_keys if key[2] == method)
                for method in DISPATCH_METHODS
            },
            "missing_expected_method_results": [
                {"case_id": case_id, "fleet_id": fleet_id, "method_name": method}
                for case_id, fleet_id, method in missing_keys
            ],
            "excluded_source_count": len(seed_excluded),
            "unrecomputable_source_count": len(seed_failures),
            "output_artifact_file_count": len(seed_files),
            "output_coverage_complete": seed_coverage_complete,
            "results": seed_entries,
            "excluded": seed_excluded,
            "unrecomputable": seed_failures,
            "excluded_preexisting_outputs": seed_excluded_outputs,
        }
        seed_manifest_path = seed_output_dir / "manifest.json"
        write_json(seed_manifest_path, seed_manifest)
        seed_index_rows = seed_entries + seed_excluded + seed_excluded_outputs + seed_failures
        seed_index_path = seed_output_dir / "recalculation_index.jsonl"
        seed_index_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in seed_index_rows),
            encoding="utf-8",
        )
        all_index_rows.extend(seed_index_rows)
        all_seed_summaries.append({
            "seed": seed,
            "status": seed_status,
            "source_result_count": seed_source_count,
            "recalculated_output_count": len(seed_entries),
            "paired_completed_cells": paired_cells,
            "eligible_evaluation_cells": len(expected_cells - excluded_cells),
            "missing_expected_method_result_count": len(missing_keys),
            "missing_by_method": seed_manifest["missing_by_method"],
            "manifest": str(seed_manifest_path),
            "output_coverage_complete": seed_coverage_complete,
        })

    overall_status = (
        "completed_with_expected_gaps"
        if expected_missing_total or failures or output_audit_errors
        else "complete_for_all_eligible_seed_results"
    )
    manifest = {
        "schema_version": "nyc_dispatch_energy_recalculation_manifest_v1",
        "status": overall_status,
        "created_at_local": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": "post-hoc energy accounting for saved, complete dispatch assignments; no policy rerun or model calls",
        "energy_model": {
            "version": profile.model_version,
            "profile_id": profile.profile_id,
            "profile_sha256": profile.fingerprint(),
        },
        "recalculation_method": RECALCULATION_METHOD,
        "recalculation_script": str(Path(__file__).resolve()),
        "recalculation_script_sha256": sha256_file(Path(__file__).resolve()),
        "input_result_count": len(sources),
        "recalculated_count": len(entries),
        "newly_recalculated_this_invocation_count": sum(
            1 for row in entries if row.get("status") == "recalculated"
        ),
        "reused_verified_count": sum(
            1 for row in entries if row.get("status") == "reused_verified_offline_recalculation"
        ),
        "included_result_count": len(entries),
        "excluded_count": len(excluded),
        "unrecomputable_count": len(failures),
        "expected_missing_result_count": expected_missing_total,
        "output_artifact_file_count": len(all_output_files),
        "indexed_result_output_count": len(indexed_outputs),
        "excluded_preexisting_output_count": len(excluded_output_artifacts),
        "output_coverage_complete": (
            len(indexed_outputs) + len(excluded_output_artifacts) == len(all_output_files)
            and not output_audit_errors
            and all(item["output_coverage_complete"] for item in all_seed_summaries)
        ),
        "seed_summaries": all_seed_summaries,
        "seed_manifest_paths": [item["manifest"] for item in all_seed_summaries],
        "excluded_preexisting_outputs": excluded_output_artifacts,
        "unreadable_unindexed_outputs": output_audit_errors,
        "results": entries,
        "excluded": excluded,
        "unrecomputable": failures,
        "limitations": [
            "recalculated energies apply to frozen legacy assignments and do not imply v2-aware decisions",
            "the synthetic centroid pickup proxy is converted to a virtual flat segment at the frozen proxy speed",
            "zone OD ride edges lack grade, signal, and time-varying congestion observations",
            "the generic reference EV profile is not calibrated to a production vehicle",
        ],
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)
    index_path = output_dir / "recalculation_index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in all_index_rows),
        encoding="utf-8",
    )
    print(json.dumps({
        "manifest": str(manifest_path),
        "input_result_count": len(sources),
        "recalculated_count": manifest["recalculated_count"],
        "newly_recalculated_this_invocation_count": manifest["newly_recalculated_this_invocation_count"],
        "reused_verified_count": manifest["reused_verified_count"],
        "included_result_count": manifest["included_result_count"],
        "excluded_count": len(excluded),
        "unrecomputable_count": len(failures),
        "expected_missing_result_count": expected_missing_total,
        "output_artifact_file_count": len(all_output_files),
        "output_coverage_complete": manifest["output_coverage_complete"],
        "seed_summaries": all_seed_summaries,
        "first_failures": failures[:10],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
