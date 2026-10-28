#!/usr/bin/env python3
"""Recalculate saved grid-dispatch candidate legs with the shared EV v2 model.

The original dispatch JSON files are read-only inputs.  This writes a new
hash-bound sidecar containing edge-level EV estimates for every saved candidate
and a separate ledger for legs selected by the historical simulation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from route_experiment.energy_model import (  # noqa: E402
    EnergyModelConfig,
    evaluate_route_edges,
    model_metadata,
)
from route_experiment.models import EvaluationConfig  # noqa: E402


DEFAULT_MANIFEST = Path(
    "outputs/dispatch_layer/traditional_experiment_v1/dispatch_matrix_manifest.json"
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_line(handle, value: Dict[str, Any]) -> None:
    handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _route_energy(
    leg: Dict[str, Any],
    edge_by_id: Dict[str, Dict[str, Any]],
    evaluation_config: EvaluationConfig,
    profile: EnergyModelConfig,
    payload_mass_kg: float,
) -> Dict[str, Any]:
    edge_ids = list(leg.get("edge_sequence") or [])
    node_ids = list(leg.get("node_sequence") or [])
    distance_m = float(leg.get("distance_m") or 0.0)

    if not edge_ids:
        if distance_m == 0.0 and (not node_ids or len(node_ids) == 1):
            return {
                "energy_status": "estimable_zero_distance_noop",
                "trace_fidelity": "estimated_zero_distance_noop",
                "distance_m": 0.0,
                "drive_time_sec": 0.0,
                "signal_idle_time_sec": 0.0,
                "propulsion_draw_kwh": 0.0,
                "regenerated_kwh": 0.0,
                "auxiliary_kwh": 0.0,
                "signed_net_kwh": 0.0,
                "billable_energy_kwh": 0.0,
                "missing_grade_steps": 0,
                "regen_power_capped_steps": 0,
                "link_energy": [],
                "error": None,
            }
        return {
            "energy_status": "non_estimable_missing_edge_sequence",
            "trace_fidelity": None,
            "distance_m": distance_m,
            "signed_net_kwh": None,
            "billable_energy_kwh": None,
            "link_energy": [],
            "error": "positive_distance_without_edge_sequence",
        }

    missing = [edge_id for edge_id in edge_ids if edge_id not in edge_by_id]
    if missing:
        return {
            "energy_status": "non_estimable_edge_mapping_missing",
            "trace_fidelity": None,
            "distance_m": distance_m,
            "signed_net_kwh": None,
            "billable_energy_kwh": None,
            "link_energy": [],
            "error": "edge_ids_missing_from_source_map:" + ",".join(missing),
        }

    edges = [edge_by_id[edge_id] for edge_id in edge_ids]
    if node_ids:
        if len(node_ids) != len(edges) + 1:
            return {
                "energy_status": "non_estimable_route_shape_mismatch",
                "trace_fidelity": None,
                "distance_m": distance_m,
                "signed_net_kwh": None,
                "billable_energy_kwh": None,
                "link_energy": [],
                "error": "node_edge_count_mismatch",
            }
        if any(
            edge.get("from_node") != node_ids[index]
            or edge.get("to_node") != node_ids[index + 1]
            for index, edge in enumerate(edges)
        ):
            return {
                "energy_status": "non_estimable_route_discontinuity",
                "trace_fidelity": None,
                "distance_m": distance_m,
                "signed_net_kwh": None,
                "billable_energy_kwh": None,
                "link_energy": [],
                "error": "node_edge_sequence_discontinuity",
            }
    elif any(
        previous.get("to_node") != current.get("from_node")
        for previous, current in zip(edges, edges[1:])
    ):
        return {
            "energy_status": "non_estimable_route_discontinuity",
            "trace_fidelity": None,
            "distance_m": distance_m,
            "signed_net_kwh": None,
            "billable_energy_kwh": None,
            "link_energy": [],
            "error": "edge_sequence_discontinuity",
        }

    edge_distance = sum(float(edge.get("length_m") or 0.0) for edge in edges)
    if not math.isclose(edge_distance, distance_m, rel_tol=0.0, abs_tol=1e-6):
        return {
            "energy_status": "non_estimable_route_distance_mismatch",
            "trace_fidelity": None,
            "distance_m": distance_m,
            "signed_net_kwh": None,
            "billable_energy_kwh": None,
            "link_energy": [],
            "error": "edge_distance_does_not_match_saved_leg_distance",
        }

    try:
        result = evaluate_route_edges(
            edges,
            evaluation_config.congestion_speed_multipliers,
            profile,
            payload_mass_kg=payload_mass_kg,
            initial_speed_mps=0.0,
            terminal_stop=True,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return {
            "energy_status": "non_estimable_ev2_evaluation_error",
            "trace_fidelity": None,
            "distance_m": distance_m,
            "signed_net_kwh": None,
            "billable_energy_kwh": None,
            "link_energy": [],
            "error": type(exc).__name__ + ":" + str(exc),
        }

    return {
        "energy_status": "estimable_from_saved_edge_conditions",
        "trace_fidelity": result.trace_fidelity,
        "distance_m": result.distance_m,
        "drive_time_sec": result.drive_time_sec,
        "signal_idle_time_sec": result.signal_idle_time_sec,
        "propulsion_draw_kwh": result.propulsion_draw_kwh,
        "regenerated_kwh": result.regenerated_kwh,
        "auxiliary_kwh": result.auxiliary_kwh,
        "signed_net_kwh": result.net_kwh,
        "billable_energy_kwh": max(result.net_kwh, 0.0),
        "rolling_wheel_kwh": result.rolling_wheel_kwh,
        "aerodynamic_wheel_kwh": result.aerodynamic_wheel_kwh,
        "grade_wheel_kwh": result.grade_wheel_kwh,
        "acceleration_wheel_kwh": result.acceleration_wheel_kwh,
        "missing_grade_steps": result.missing_grade_steps,
        "regen_power_capped_steps": result.regen_power_capped_steps,
        "link_energy": result.edge_breakdown,
        "error": None,
    }


def _candidate_decision_map(output: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(row.get("candidate_id")): row
        for row in output.get("candidate_decisions", [])
        if row.get("candidate_id") is not None
    }


def recalculate(manifest_path: Path, output_dir: Path) -> Dict[str, Any]:
    manifest_bytes = manifest_path.read_bytes()
    source_manifest = json.loads(manifest_bytes)
    if source_manifest.get("schema_version") != "dispatch-matrix-v1":
        raise ValueError("unexpected_dispatch_matrix_manifest_schema")
    if output_dir.exists():
        raise FileExistsError("sidecar_output_directory_already_exists:" + str(output_dir))
    output_dir.mkdir(parents=True)

    evaluation_config = EvaluationConfig()
    profile = evaluation_config.energy_profile
    source_hashes: Dict[str, str] = {}
    counters = Counter()
    global_run_summaries = []

    candidate_path = output_dir / "candidate_leg_ev2.jsonl"
    execution_path = output_dir / "assigned_journey_segments_ev2.jsonl"
    summary_path = output_dir / "run_summary.jsonl"

    with candidate_path.open("w", encoding="utf-8") as candidate_out, \
         execution_path.open("w", encoding="utf-8") as execution_out, \
         summary_path.open("w", encoding="utf-8") as summary_out:
        for raw_path in source_manifest.get("result_paths", []):
            source_path = Path(raw_path)
            if not source_path.is_absolute():
                source_path = (Path.cwd() / source_path).resolve()
            if not source_path.is_file():
                raise FileNotFoundError("source_result_missing:" + str(source_path))
            source_bytes = source_path.read_bytes()
            source_hash = _sha256_bytes(source_bytes)
            source_hashes[str(source_path)] = source_hash
            payload = json.loads(source_bytes)
            run_summary = payload.get("summary") or {}
            method = str(run_summary.get("method_name"))
            seed = int(run_summary.get("seed"))
            run_id = str(run_summary.get("experiment_run_id"))
            expected_fingerprint = source_manifest.get("scenario_fingerprints_sha256", {}).get(
                method + "/seed_" + str(seed)
            )
            if payload.get("scenario_fingerprint_sha256") != expected_fingerprint:
                raise ValueError("scenario_fingerprint_mismatch:" + str(source_path))

            edge_by_id = {
                str(edge.get("edge_id")): edge
                for edge in payload.get("map", {}).get("edges", [])
                if edge.get("edge_id") is not None
            }
            order_by_id = {
                str(order.get("order_id")): order
                for order in payload.get("orders", [])
                if order.get("order_id") is not None
            }
            candidate_energy: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            assignments = []
            run_counters = Counter()
            legacy_config = payload.get("config") or {}
            old_rate = legacy_config.get("moving_kwh_per_km")

            for round_payload in payload.get("rounds", []):
                round_id = str(round_payload.get("dispatch_round_id"))
                output = round_payload.get("output") or {}
                decision_map = _candidate_decision_map(output)
                selected_ids = set(output.get("selected_candidate_ids") or [])
                candidates = round_payload.get("candidates") or []
                candidates_by_id = {
                    str(candidate.get("candidate_id")): candidate
                    for candidate in candidates
                    if candidate.get("candidate_id") is not None
                }
                for candidate in candidates:
                    candidate_id = str(candidate.get("candidate_id"))
                    decision = decision_map.get(candidate_id, {})
                    result_legs = {}
                    for leg_key, journey_segment, payload_mass in (
                        ("pickup_leg", "deadhead_pickup", 0.0),
                        ("ride_leg", "passenger_service", profile.passenger_load_kg),
                    ):
                        leg = candidate.get(leg_key) or {}
                        energy = _route_energy(
                            leg, edge_by_id, evaluation_config, profile, payload_mass
                        )
                        result_legs[leg_key] = energy
                        run_counters["candidate_leg_rows"] += 1
                        run_counters["candidate_leg_" + energy["energy_status"]] += 1
                        if energy.get("link_energy"):
                            run_counters["candidate_link_rows"] += len(energy["link_energy"])
                        candidate_energy[(round_id, candidate_id, leg_key)] = energy
                        legacy_leg_kwh = None
                        if old_rate is not None and energy.get("distance_m") is not None:
                            legacy_leg_kwh = (
                                float(energy["distance_m"]) / 1000.0 * float(old_rate)
                            )
                        row = {
                            "source_result_path": str(source_path),
                            "source_result_sha256": source_hash,
                            "source_manifest_sha256": _sha256_bytes(manifest_bytes),
                            "experiment_run_id": run_id,
                            "method_name": method,
                            "seed": seed,
                            "scenario_fingerprint_sha256": payload.get(
                                "scenario_fingerprint_sha256"
                            ),
                            "dispatch_round_id": round_id,
                            "snapshot_time_sec": round_payload.get("snapshot_time_sec"),
                            "candidate_id": candidate_id,
                            "vehicle_id": candidate.get("vehicle_id"),
                            "order_id": candidate.get("order_id"),
                            "candidate_selected": candidate_id in selected_ids,
                            "hard_constraint_pass": decision.get(
                                "hard_constraint_pass", candidate.get("hard_constraint_ok")
                            ),
                            "candidate_decision": decision.get("agent_decision"),
                            "candidate_rejection_reason": decision.get(
                                "reason", candidate.get("rejection_reason")
                            ),
                            "leg": journey_segment,
                            "payload_mass_kg": payload_mass,
                            "candidate_route_status": "selected_candidate"
                            if candidate_id in selected_ids
                            else "counterfactual_candidate_only",
                            "legacy_configured_kwh_per_km": old_rate,
                            "legacy_proxy_energy_kwh_for_leg": legacy_leg_kwh,
                            "fare_amount": None,
                            "electricity_price_source": None,
                            "electricity_cost_amount": None,
                            "energy_model_version": profile.model_version,
                            "energy_profile_id": profile.profile_id,
                            "energy_profile_sha256": profile.fingerprint(),
                            "data_basis": "saved_candidate_edge_sequence_and_frozen_map_edge_conditions",
                            "physical_or_stepwise_motion_trace_present": False,
                            **energy,
                        }
                        _json_line(candidate_out, row)
                        run_counters[
                            "candidate_legs_selected"
                            if candidate_id in selected_ids
                            else "candidate_legs_counterfactual"
                        ] += 1

                for assignment in output.get("assignments", []):
                    assignments.append((round_payload, assignment, candidates_by_id))

            executed_net_kwh = 0.0
            executed_billable_kwh = 0.0
            executed_propulsion_kwh = 0.0
            executed_regen_kwh = 0.0
            executed_auxiliary_kwh = 0.0
            selected_full_plan_net_kwh = 0.0
            assignment_status_counts = Counter()
            for round_payload, assignment, candidates_by_id in assignments:
                round_id = str(round_payload.get("dispatch_round_id"))
                candidate_id = str(assignment.get("candidate_id"))
                candidate = candidates_by_id.get(candidate_id)
                order_id = str(assignment.get("order_id"))
                order = order_by_id.get(order_id) or {}
                order_status = str(order.get("status") or "unknown")
                cancel_reason = order.get("cancel_reason")
                if candidate is None:
                    counters["assignment_candidate_missing"] += 1
                    continue
                pickup = candidate_energy.get((round_id, candidate_id, "pickup_leg"))
                ride = candidate_energy.get((round_id, candidate_id, "ride_leg"))
                if pickup and pickup.get("signed_net_kwh") is not None:
                    selected_full_plan_net_kwh += float(pickup["signed_net_kwh"])
                if ride and ride.get("signed_net_kwh") is not None:
                    selected_full_plan_net_kwh += float(ride["signed_net_kwh"])

                if order_status == "completed":
                    executed_leg_status = {
                        "pickup_leg": "simulated_leg_completed_estimate_only",
                        "ride_leg": "simulated_leg_completed_estimate_only",
                    }
                elif order_status == "cancelled" and cancel_reason == "passenger_no_show":
                    executed_leg_status = {
                        "pickup_leg": "simulated_pickup_completed_estimate_only",
                        "ride_leg": "not_executed_counterfactual_only",
                    }
                else:
                    executed_leg_status = {
                        "pickup_leg": "execution_extent_not_established",
                        "ride_leg": "execution_extent_not_established",
                    }

                for leg_key, journey_segment in (
                    ("pickup_leg", "deadhead_pickup"),
                    ("ride_leg", "passenger_service"),
                ):
                    energy = candidate_energy.get((round_id, candidate_id, leg_key)) or {}
                    execution_status = executed_leg_status[leg_key]
                    is_executed = execution_status in (
                        "simulated_leg_completed_estimate_only",
                        "simulated_pickup_completed_estimate_only",
                    )
                    model_estimate = energy.get("signed_net_kwh") if is_executed else None
                    if model_estimate is not None:
                        executed_net_kwh += float(model_estimate)
                        executed_billable_kwh += max(float(model_estimate), 0.0)
                        executed_propulsion_kwh += float(energy.get("propulsion_draw_kwh") or 0.0)
                        executed_regen_kwh += float(energy.get("regenerated_kwh") or 0.0)
                        executed_auxiliary_kwh += float(energy.get("auxiliary_kwh") or 0.0)
                    execution_row = {
                        "source_result_path": str(source_path),
                        "source_result_sha256": source_hash,
                        "experiment_run_id": run_id,
                        "method_name": method,
                        "seed": seed,
                        "dispatch_round_id": round_id,
                        "candidate_id": candidate_id,
                        "assignment_id": assignment.get("assignment_id"),
                        "assignment_type": assignment.get("assignment_type"),
                        "vehicle_id": assignment.get("vehicle_id"),
                        "order_id": order_id,
                        "order_final_status": order_status,
                        "cancel_reason": cancel_reason,
                        "pickup_arrival_time_sec": order.get("pickup_arrival_time_sec"),
                        "boarding_time_sec": order.get("boarding_time_sec"),
                        "dropoff_time_sec": order.get("dropoff_time_sec"),
                        "journey_segment": journey_segment,
                        "execution_status": execution_status,
                        "energy_model_version": profile.model_version,
                        "trace_fidelity": energy.get("trace_fidelity"),
                        "simulated_route_energy_estimate_kwh": model_estimate,
                        "counterfactual_candidate_energy_estimate_kwh": energy.get("signed_net_kwh")
                        if not is_executed
                        else None,
                        "observed_energy_kwh": None,
                        "billable_energy_kwh": max(float(model_estimate), 0.0)
                        if model_estimate is not None
                        else None,
                        "fare_amount": None,
                        "revenue_status": "missing_from_source_order_record",
                        "electricity_price_per_kwh": None,
                        "electricity_cost_amount": None,
                        "money_decision_status": "blocked_missing_order_fare_verified_tariff_and_method_cost",
                        "link_energy": energy.get("link_energy") if is_executed else None,
                    }
                    _json_line(execution_out, execution_row)
                    run_counters["assignment_segment_rows"] += 1
                    run_counters["assignment_segment_" + execution_status] += 1
                assignment_status_counts[order_status + (":" + str(cancel_reason) if cancel_reason else "")] += 1

            record = {
                "source_result_path": str(source_path),
                "source_result_sha256": source_hash,
                "experiment_run_id": run_id,
                "method_name": method,
                "seed": seed,
                "scenario_fingerprint_sha256": payload.get("scenario_fingerprint_sha256"),
                "legacy_total_energy_kwh": run_summary.get("total_energy_kwh"),
                "legacy_energy_rate_kwh_per_km": old_rate,
                "legacy_rate_source_status": "configured_in_legacy_run_without_authoritative_price_source",
                "assigned_order_status_counts": dict(assignment_status_counts),
                "selected_full_plan_ev2_signed_net_estimate_kwh": selected_full_plan_net_kwh,
                "simulated_completed_leg_ev2_signed_net_estimate_kwh": executed_net_kwh,
                "simulated_completed_leg_billable_energy_estimate_kwh": executed_billable_kwh,
                "simulated_completed_leg_propulsion_draw_estimate_kwh": executed_propulsion_kwh,
                "simulated_completed_leg_regenerated_estimate_kwh": executed_regen_kwh,
                "simulated_completed_leg_auxiliary_estimate_kwh": executed_auxiliary_kwh,
                "observed_energy_kwh": None,
                "source_result_unmodified": True,
                "sidecar_only": True,
                "runtime_selector_switched": False,
                "money_decision_status": "blocked_missing_order_fare_verified_tariff_and_method_cost",
                **dict(run_counters),
            }
            _json_line(summary_out, record)
            global_run_summaries.append(record)
            counters.update(run_counters)
            counters["source_runs"] += 1

    energy_model_path = PROJECT_ROOT / "route_experiment" / "energy_model.py"
    script_path = Path(__file__).resolve()
    output_hashes = {
        str(path): _sha256_file(path)
        for path in (candidate_path, execution_path, summary_path)
    }
    global_manifest = {
        "schema_version": "grid_dispatch_ev2_segment_sidecar_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_manifest_path": str(manifest_path.resolve()),
        "source_manifest_sha256": _sha256_bytes(manifest_bytes),
        "source_matrix_schema": source_manifest.get("schema_version"),
        "source_result_count": len(source_hashes),
        "source_results": source_hashes,
        "energy_model_version": profile.model_version,
        "energy_model_path": str(energy_model_path),
        "energy_model_sha256": _sha256_file(energy_model_path),
        "energy_profile": model_metadata(profile),
        "energy_profile_sha256": profile.fingerprint(),
        "congestion_speed_multipliers": evaluation_config.congestion_speed_multipliers,
        "calculation_script_path": str(script_path),
        "calculation_script_sha256": _sha256_file(script_path),
        "trace_fidelity": "estimated_from_route_edge_conditions",
        "physical_or_stepwise_motion_trace_available": False,
        "candidate_routes_are_counterfactual_unless_selected": True,
        "selected_route_energy_is_still_model_estimated_without_per_edge_motion_trace": True,
        "passenger_fares_available": False,
        "authoritative_electricity_tariff_available": False,
        "method_compute_price_available": False,
        "time_money_rankings_emitted": False,
        "money_decision_status": "blocked_missing_order_fare_verified_tariff_and_method_cost",
        "sidecar_only": True,
        "runtime_selector_switched": False,
        "source_results_modified": False,
        "new_model_calls": 0,
        "sumo_reruns": 0,
        "counts": dict(counters),
        "outputs": {
            path.name: {"path": str(path.resolve()), "sha256": digest}
            for path, digest in ((candidate_path, output_hashes[str(candidate_path)]),
                                 (execution_path, output_hashes[str(execution_path)]),
                                 (summary_path, output_hashes[str(summary_path)]))
        },
    }
    manifest_out = output_dir / "manifest.json"
    manifest_out.write_text(
        json.dumps(global_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return global_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    result = recalculate(args.manifest.resolve(), args.out_dir.resolve())
    print(json.dumps({
        "manifest_path": str(args.out_dir.resolve() / "manifest.json"),
        "source_result_count": result["source_result_count"],
        "counts": result["counts"],
        "source_results_modified": result["source_results_modified"],
        "new_model_calls": result["new_model_calls"],
        "sumo_reruns": result["sumo_reruns"],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
