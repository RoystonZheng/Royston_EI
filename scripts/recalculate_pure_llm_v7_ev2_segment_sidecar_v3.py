#!/usr/bin/env python3
"""Recalculate EV2 economics for legal prefixes in saved pure-LLM v7 traces.

This is an offline, versioned sidecar calculation. It never calls a model,
reruns route search, or modifies the original experiment or v2 audit.
Incomplete/failed v7 proposals have no observed full-route energy; their
``actual_route_ev2`` remains null. Exact legal prefixes are reported as
simulation estimates by pickup/ride phase. Illegal full proposals and proxy
edges are retained only as separately labelled v2 counterfactuals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.costs import evaluate_route_candidate
from route_experiment.energy_model import EnergyModelConfig, model_metadata
from route_experiment.failure_audit import recover_partial_move_sequence
from route_experiment.llm_adapter import replay_move_sequence
from route_experiment.models import Edge, EvaluationConfig, Node, RoadMap, RouteCandidate
from scripts.recalculate_pure_llm_grid_energy_v2 import (
    move_sequence_from_nodes,
    recover_node_sequence,
)

DEFAULT_AUDIT = ROOT / (
    "outputs/route_layer/pure_llm_grid_energy_v2_recalculation_20261029T0630UTC/"
    "pure_llm_experiment_v7/failure_energy_audit_v2.json"
)
DEFAULT_OUTPUT = ROOT / (
    "outputs/route_layer/pure_llm_grid_energy_v3_segment_recalculation_20261030_v2/"
    "pure_llm_experiment_v7"
)
ENERGY_MODULE = ROOT / "route_experiment/energy_model.py"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def load_road_map(path: Path) -> RoadMap:
    payload = read_json(path)
    return RoadMap(
        map_id=payload["map_id"],
        version=payload["version"],
        size=payload["size"],
        nodes=[Node(**node) for node in payload["nodes"]],
        edges=[Edge(**edge) for edge in payload["edges"]],
        boundary=payload["boundary"],
        road_state_version=payload.get("road_state_version", "road_state_v1"),
        roadside_features=payload.get("roadside_features", []),
    )


def extract_sequence(method: dict[str, Any], task: dict[str, Any], v2_record: dict[str, Any]):
    """Reproduce the stored v2 path selection for an independent trace check."""
    metadata = method.get("metadata") or {}
    raw_output = method.get("raw_output") or {}
    reason = str(method.get("failure_reason") or "unknown_failure")
    sequence = metadata.get("move_sequence") or raw_output.get("move_sequence") or ""
    start_node = task.get("start_node_id")
    recovered = False
    recovered_nodes = False

    if not sequence and reason == "llm_output_parse_failed":
        sequence = metadata.get("recovered_partial_move_sequence") or recover_partial_move_sequence(raw_output)
        recovered = bool(sequence)
    if not sequence:
        recovered_path, recovered_nodes = recover_node_sequence(raw_output)
        node_moves = move_sequence_from_nodes(recovered_path)
        if node_moves:
            sequence = node_moves
            start_node = recovered_path[0]
            recovered = recovered or recovered_nodes

    stored_audit = v2_record["proposal_energy_audit_v2"]
    stored_start = stored_audit.get("proposal_path_start_node_id")
    if stored_start and sequence and start_node != stored_start:
        raise ValueError(
            f"v2_start_node_mismatch:{v2_record['task_id']}:{start_node}:{stored_start}"
        )
    if not sequence:
        return "", None, recovered, recovered_nodes
    return str(sequence).strip().upper(), str(start_node or ""), recovered, recovered_nodes


def map_v2_sources(audit: dict[str, Any], root: Path):
    """Index and hash-check every immutable source listed by the v2 audit."""
    by_seed: dict[int, dict[str, list[dict[str, Any]]]] = {}
    source_rows = []
    for entry in audit.get("source_files", []):
        path = root / entry["path"]
        if not path.is_file():
            raise FileNotFoundError(f"v2_source_missing:{path}")
        actual = sha256(path)
        if actual != entry.get("sha256"):
            raise ValueError(f"v2_source_hash_mismatch:{path}:{entry.get('sha256')}:{actual}")
        seed = int(entry["seed"])
        verified_entry = {**entry, "verified_sha256": actual}
        by_seed.setdefault(seed, {}).setdefault(entry["kind"], []).append(verified_entry)
        source_rows.append(verified_entry)

    for seed, kinds in by_seed.items():
        if len(kinds.get("map", [])) != 1:
            raise ValueError(f"expected_one_map_for_seed:{seed}")
        route_scopes = {row.get("scope") for row in kinds.get("saved_routes", [])}
        if route_scopes != {"orders", "formal"}:
            raise ValueError(f"saved_route_scopes_incomplete:{seed}:{route_scopes}")
        task_names = {Path(row["path"]).name for row in kinds.get("saved_tasks", [])}
        if not any(name.startswith("order_tasks_") for name in task_names):
            raise ValueError(f"order_task_source_missing:{seed}")
        if not any(name.startswith("formal_scenarios_") for name in task_names):
            raise ValueError(f"formal_task_source_missing:{seed}")
    return by_seed, source_rows


def verify_hash_rows(rows: list[dict[str, Any]], root: Path, label: str) -> None:
    """Recheck immutable source hashes after calculation, before publishing."""
    for row in rows:
        path = root / row["path"]
        expected = row.get("sha256") or row.get("verified_sha256")
        if not path.is_file():
            raise FileNotFoundError(f"{label}_missing_after_calculation:{path}")
        actual = sha256(path)
        if actual != expected:
            raise ValueError(f"{label}_hash_changed_after_calculation:{path}:{expected}:{actual}")


def load_seed(seed: int, entries: dict[str, list[dict[str, Any]]], root: Path):
    map_entry = entries["map"][0]
    road_map = load_road_map(root / map_entry["path"])
    node_by_id = {node.node_id: node for node in road_map.nodes}
    edge_by_id = {edge.edge_id: edge for edge in road_map.edges}

    order_entry = next(
        row for row in entries["saved_tasks"]
        if Path(row["path"]).name.startswith("order_tasks_")
    )
    formal_entry = next(
        row for row in entries["saved_tasks"]
        if Path(row["path"]).name.startswith("formal_scenarios_")
    )
    order_payload = read_json(root / order_entry["path"])
    formal_payload = read_json(root / formal_entry["path"])
    tasks: dict[str, dict[str, Any]] = {}
    truth: dict[str, dict[str, Any]] = {}
    scenario_by_task: dict[str, str] = {}
    for task in order_payload.get("tasks", []):
        tasks[task["task_id"]] = {**task, "scope": "orders"}
    for row in order_payload.get("ground_truth", []):
        truth[row["task_id"]] = row
    for scenario in formal_payload.get("scenarios", []):
        for task in scenario.get("tasks", []):
            tasks[task["task_id"]] = {
                **task,
                "scope": "formal",
                "blocked_edge_ids": list(scenario.get("blocked_edge_ids", [])),
                "scenario_type": scenario.get("scenario_type"),
                "scenario_id": scenario.get("scenario_id"),
            }
            scenario_by_task[task["task_id"]] = scenario.get("scenario_id")

    routes_by_scope = {}
    route_entries = {}
    for row in entries["saved_routes"]:
        route_entries[row["scope"]] = row
        payload = read_json(root / row["path"])
        routes_by_scope[row["scope"]] = {
            result["task_id"]: result for result in payload.get("results", [])
        }
    return {
        "road_map": road_map,
        "node_by_id": node_by_id,
        "edge_by_id": edge_by_id,
        "tasks": tasks,
        "truth": truth,
        "scenario_by_task": scenario_by_task,
        "routes_by_scope": routes_by_scope,
        "route_entries": route_entries,
        "order_entry": order_entry,
        "formal_entry": formal_entry,
        "map_entry": map_entry,
    }


def evaluate_prefix_segment(
    road_map: RoadMap,
    task_id: str,
    segment_type: str,
    nodes: list[str],
    edge_ids: list[str],
    payload_mass_kg: float,
    config: EvaluationConfig,
) -> dict[str, Any]:
    if not edge_ids:
        return {
            "segment_type": segment_type,
            "status": "zero_length_phase",
            "payload_mass_kg": payload_mass_kg,
            "edge_count": 0,
            "edge_ids": [],
            "distance_m": 0.0,
            "signed_net_energy_kwh": 0.0,
            "billable_energy_kwh": 0.0,
            "simulation_estimate": True,
            "note": "Boundary exists but this prefix contains no movement in the phase.",
        }
    if len(nodes) != len(edge_ids) + 1:
        raise ValueError(f"segment_node_edge_mismatch:{task_id}:{segment_type}")
    candidate = RouteCandidate(
        route_id=f"{task_id}:{segment_type}:legal_prefix",
        node_sequence=nodes,
        edge_sequence=edge_ids,
        route_source="saved_v7_legal_prefix_ev2_v3_sidecar",
        route_reachability=True,
        validation_status="legal_prefix_edges",
        payload_mass_kg=payload_mass_kg,
    )
    result = evaluate_route_candidate(road_map, candidate, config)
    breakdown = list(result.energy_segments)
    summed_link_energy = sum(float(item.get("energy_kwh", 0.0) or 0.0) for item in breakdown)
    links_reconcile = abs(summed_link_energy - result.energy_total_kwh) <= 1e-9
    if not links_reconcile:
        raise ValueError(f"segment_link_energy_mismatch:{task_id}:{segment_type}")
    return {
        "segment_type": segment_type,
        "status": "legal_prefix_simulation_estimate",
        "payload_mass_kg": payload_mass_kg,
        "edge_count": len(edge_ids),
        "edge_ids": edge_ids,
        "edge_sequence_sha256": hashlib.sha256(
            json.dumps(edge_ids, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "node_sequence": nodes,
        "distance_m": result.route_distance_m,
        "travel_time_sec": result.total_travel_time_sec,
        "signed_net_energy_kwh": result.energy_total_kwh,
        "billable_energy_kwh": max(result.energy_total_kwh, 0.0),
        "propulsion_draw_kwh": result.energy_moving_kwh,
        "regenerated_kwh": result.energy_regenerated_kwh,
        "auxiliary_kwh": result.energy_auxiliary_kwh,
        "grade_wheel_kwh": result.energy_grade_wheel_kwh,
        "energy_grade_missing": result.energy_grade_missing,
        "energy_model_version": result.energy_model_version,
        "energy_profile_id": result.energy_profile_id,
        "energy_profile_sha256": result.energy_profile_sha256,
        "trace_fidelity": result.energy_trace_fidelity,
        "road_link_breakdown_reconciles": links_reconcile,
        "road_link_signed_net_energy_kwh": summed_link_energy,
        "road_links": breakdown,
        "initial_speed_mps": 0.0,
        "terminal_stop": True,
        "simulation_estimate": True,
        "not_observed_vehicle_energy": True,
    }


def build_record(
    v2_record: dict[str, Any],
    seed_data: dict[str, Any],
    seed: int,
    scope: str,
    task_id: str,
    source_root: Path,
    config: EvaluationConfig,
):
    task = seed_data["tasks"].get(task_id)
    if task is None:
        raise ValueError(f"task_missing:{seed}:{task_id}")
    task_truth = seed_data["truth"].get(task_id, {})
    pickup_id = task.get("pickup_node_id") or task.get("true_pickup_node_id") or task_truth.get("true_pickup_node_id")
    dropoff_id = task.get("dropoff_node_id") or task.get("true_dropoff_node_id") or task_truth.get("true_dropoff_node_id")
    if not pickup_id or not dropoff_id:
        raise ValueError(f"frozen_pickup_dropoff_boundary_missing:{task_id}")

    result = seed_data["routes_by_scope"][scope].get(task_id)
    if result is None:
        raise ValueError(f"saved_route_result_missing:{seed}:{scope}:{task_id}")
    method = next(
        (item for item in result.get("methods", []) if item.get("method_name") == v2_record.get("method_name")),
        None,
    )
    if method is None:
        raise ValueError(f"saved_method_result_missing:{task_id}")
    sequence, start_node, recovered, recovered_nodes = extract_sequence(method, task, v2_record)

    if scope == "orders":
        trace_stem = task_id
        trace_folder = "orders"
    else:
        trace_stem = seed_data["scenario_by_task"].get(task_id)
        trace_folder = "formal_scenarios"
    if not trace_stem:
        raise ValueError(f"formal_scenario_id_missing:{task_id}")
    trace_path = (
        source_root / "seeds" / f"seed_{seed}" / "visualizations" / trace_folder
        / f"{trace_stem}__{v2_record.get('method_name')}.json"
    )
    if not trace_path.is_file():
        raise FileNotFoundError(f"saved_trace_missing:{trace_path}")
    trace_document = read_json(trace_path)
    trace = trace_document.get("trace") or {}
    trace_sha = sha256(trace_path)
    if trace_document.get("task_id") != task_id:
        raise ValueError(f"trace_task_id_mismatch:{task_id}:{trace_document.get('task_id')}")

    replay = None
    if sequence:
        if not start_node:
            raise ValueError(f"proposal_start_node_missing:{task_id}")
        replay = replay_move_sequence(
            seed_data["road_map"],
            start_node,
            sequence,
            blocked_edge_ids=task.get("blocked_edge_ids", []),
        )
    prefix_nodes = list((replay or {}).get("validated_prefix_node_sequence", []))
    prefix_edges = list((replay or {}).get("validated_prefix_edge_sequence", []))
    trace_nodes = list(trace.get("validated_prefix_node_sequence") or [])
    trace_edges = list(trace.get("validated_prefix_edge_sequence") or [])
    if prefix_nodes != trace_nodes or prefix_edges != trace_edges:
        raise ValueError(f"saved_trace_replay_mismatch:{task_id}")

    prior_energy = v2_record["proposal_energy_audit_v2"]
    if len(prefix_edges) != int(prior_energy.get("validated_prefix_move_count", 0) or 0):
        raise ValueError(f"v2_prefix_count_mismatch:{task_id}")
    if sequence and len(sequence) != int(prior_energy.get("proposed_move_count", 0) or 0):
        raise ValueError(f"v2_proposal_count_mismatch:{task_id}")

    task_start = task.get("start_node_id")
    origin_matches = bool(start_node == task_start) if start_node else False
    if prefix_nodes and prefix_nodes[0] != task_start:
        origin_matches = False
    pickup_node = seed_data["node_by_id"].get(pickup_id)
    dropoff_node = seed_data["node_by_id"].get(dropoff_id)
    if pickup_node is None or dropoff_node is None:
        raise ValueError(f"frozen_endpoint_not_in_map:{task_id}")
    pickup_index = prefix_nodes.index(pickup_id) if pickup_id in prefix_nodes else None
    pickup_reached = pickup_index is not None
    pickup_legal = bool(pickup_node.legal_pickup)
    dropoff_legal = bool(dropoff_node.legal_dropoff)

    segments = []
    prefix_status = "no_legal_prefix_edges"
    phase_boundary_status = "pickup_not_reached_in_legal_prefix"
    if prefix_edges and origin_matches:
        if pickup_reached:
            phase_boundary_status = "pickup_node_reached"
            deadhead_edges = prefix_edges[:pickup_index]
            deadhead_nodes = prefix_nodes[: pickup_index + 1]
            segments.append(
                evaluate_prefix_segment(
                    seed_data["road_map"], task_id, "deadhead_prefix",
                    deadhead_nodes, deadhead_edges, 0.0, config,
                )
            )
            if pickup_legal:
                ride_edges = prefix_edges[pickup_index:]
                ride_nodes = prefix_nodes[pickup_index:]
                if ride_edges:
                    segments.append(
                        evaluate_prefix_segment(
                            seed_data["road_map"], task_id, "ride_prefix",
                            ride_nodes, ride_edges, config.energy_profile.passenger_load_kg, config,
                        )
                    )
                else:
                    phase_boundary_status = "pickup_reached_no_ride_edge_in_prefix"
            else:
                phase_boundary_status = "pickup_node_reached_but_not_legal_for_service"
            prefix_status = "pickup_reached_legal_prefix_simulation"
        else:
            segments.append(
                evaluate_prefix_segment(
                    seed_data["road_map"], task_id, "deadhead_prefix",
                    prefix_nodes, prefix_edges, 0.0, config,
                )
            )
            prefix_status = "pre_pickup_legal_prefix_simulation"
    elif prefix_edges:
        prefix_status = "origin_mismatch_prefix_not_task_executable"
        phase_boundary_status = "not_applied_origin_mismatch"

    counterfactual_status = prior_energy.get("status")
    counterfactual_energy = prior_energy.get("route_energy_estimate_kwh")
    if counterfactual_status == "no_route_proposed":
        counterfactual_energy = None
    actual_reason = (
        "v7_attempt_failed_or_incomplete; no completed accepted route or observed vehicle trajectory"
    )

    return {
        "schema_version": "pure_llm_v7_ev2_segment_failure_audit_v3_record",
        "task_id": task_id,
        "source_task_id": v2_record.get("source_task_id"),
        "seed": seed,
        "scope": scope,
        "task_type": task.get("task_type"),
        "scenario_type": task.get("scenario_type"),
        "method_name": v2_record.get("method_name"),
        "source_status": v2_record.get("status"),
        "failure_reason": v2_record.get("failure_reason"),
        "response_received": bool(v2_record.get("response_received")),
        "source_trace": {
            "path": str(trace_path.relative_to(ROOT)),
            "sha256": trace_sha,
            "v2_audit_sha256": None,
            "route_result_path": str(seed_data["route_entries"][scope]["path"]),
            "route_result_sha256": seed_data["route_entries"][scope]["verified_sha256"],
            "map_path": str(seed_data["map_entry"]["path"]),
            "map_sha256": seed_data["map_entry"]["verified_sha256"],
        },
        "proposal_trace": {
            "path_basis": prior_energy.get("proposal_path_basis"),
            "proposal_origin_node_id": start_node,
            "task_start_node_id": task_start,
            "origin_matches_task_start": origin_matches,
            "attempted_move_count": len(sequence),
            "legal_prefix_edge_count": len(prefix_edges),
            "legal_prefix_move_count_v2": prior_energy.get("validated_prefix_move_count"),
            "first_violation_step": (replay or {}).get("first_violation_step"),
            "violation_count": len((replay or {}).get("move_violations", [])),
            "sequence_recovered": recovered,
            "node_sequence_recovered": recovered_nodes,
            "trace_matches_independent_replay": True,
        },
        "frozen_trip_boundaries": {
            "pickup_node_id": pickup_id,
            "dropoff_node_id": dropoff_id,
            "pickup_node_legal": pickup_legal,
            "dropoff_node_legal": dropoff_legal,
            "pickup_index_in_legal_prefix_nodes": pickup_index,
            "pickup_occurrences_in_legal_prefix": prefix_nodes.count(pickup_id),
            "dropoff_reached_after_pickup_in_legal_prefix": bool(
                pickup_reached and dropoff_id in prefix_nodes[pickup_index:]
            ),
            "phase_boundary_status": phase_boundary_status,
            "boundary_source": "frozen_task_or_hidden_evaluation_ground_truth",
        },
        "legal_prefix_segment_simulation": {
            "status": prefix_status,
            "interpretation": (
                "Energy simulated from the saved exact legal prefix; the prefix was not a completed/observed vehicle trip."
            ),
            "segments": segments,
            "signed_net_energy_kwh_sum": sum(
                float(segment.get("signed_net_energy_kwh", 0.0) or 0.0)
                for segment in segments
            ) if segments else None,
            "billable_energy_kwh_sum_by_frozen_segment": sum(
                float(segment.get("billable_energy_kwh", 0.0) or 0.0)
                for segment in segments
            ) if segments else None,
            "actual_route_ev2": None,
            "actual_route_ev2_null_reason": actual_reason,
        },
        "counterfactual_proposal_energy_v2": {
            "status": counterfactual_status,
            "hypothetical_net_energy_kwh": counterfactual_energy,
            "billable_energy_kwh": None,
            "exact_edge_moves": prior_energy.get("exact_edge_moves"),
            "reverse_edge_inferred_moves": prior_energy.get("reverse_edge_inferred_moves"),
            "proxy_edge_moves": prior_energy.get("proxy_edge_moves"),
            "trace_fidelity": prior_energy.get("energy_trace_fidelity"),
            "interpretation": (
                "Counterfactual only; invalid/recovered proposal and inferred reverse/proxy edges are not actual travel and are not billed."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-v2", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    audit_path = args.audit_v2.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise SystemExit(f"refusing_to_overwrite:{output_dir}")
    if not audit_path.is_file():
        raise SystemExit(f"v2_audit_missing:{audit_path}")

    audit = read_json(audit_path)
    v2_audit_sha = sha256(audit_path)
    if audit.get("schema_version") != "pure_llm_grid_energy_audit_ev_longitudinal_v2":
        raise SystemExit("unexpected_v2_audit_schema")
    source_root = (ROOT / audit["source_experiment"]).resolve()
    source_manifest = source_root / "manifest.json"
    if not source_manifest.is_file():
        raise SystemExit(f"source_manifest_missing:{source_manifest}")
    source_manifest_sha = sha256(source_manifest)
    if source_manifest_sha != audit.get("source_manifest_sha256"):
        raise SystemExit("source_manifest_hash_mismatch_v2_audit")

    profile = EnergyModelConfig()
    config = EvaluationConfig(energy_profile=profile)
    module_sha = sha256(ENERGY_MODULE)
    if module_sha != audit.get("energy_model_module_sha256"):
        raise SystemExit("energy_model_module_changed_since_v2_audit")
    profile_sha = profile.fingerprint()
    if profile_sha != audit.get("energy_model", {}).get("profile_sha256"):
        raise SystemExit("energy_profile_changed_since_v2_audit")

    by_seed, verified_source_files = map_v2_sources(audit, ROOT)
    seed_cache = {
        seed: load_seed(seed, entries, ROOT)
        for seed, entries in sorted(by_seed.items())
    }
    seen: set[tuple[int, str, str]] = set()
    counters: Counter[str] = Counter()
    output_records = []
    trace_sources = []
    prefix_signed_total = 0.0
    prefix_billable_total = 0.0

    for v2_record in audit.get("records", []):
        seed = int(v2_record["seed"])
        scope = str(v2_record["scope"])
        task_id = str(v2_record["task_id"])
        key = (seed, scope, task_id)
        if key in seen:
            raise SystemExit(f"duplicate_v2_record:{key}")
        seen.add(key)
        record = build_record(
            v2_record,
            seed_cache[seed],
            seed,
            scope,
            task_id,
            source_root,
            config,
        )
        record["source_trace"]["v2_audit_sha256"] = v2_audit_sha
        trace_sources.append({
            "path": record["source_trace"]["path"],
            "sha256": record["source_trace"]["sha256"],
            "seed": seed,
            "scope": scope,
            "task_id": task_id,
        })
        counters["records"] += 1
        counters["actual_route_ev2_null"] += record["legal_prefix_segment_simulation"]["actual_route_ev2"] is None
        counters["legal_prefix_records_with_energy"] += bool(record["legal_prefix_segment_simulation"]["segments"])
        counters["legal_prefix_edges"] += record["proposal_trace"]["legal_prefix_edge_count"]
        counters["pickup_reached_in_legal_prefix"] += bool(
            record["frozen_trip_boundaries"]["pickup_index_in_legal_prefix_nodes"] is not None
        )
        counters["pickup_legal"] += record["frozen_trip_boundaries"]["pickup_node_legal"]
        counters["counterfactual_records"] += record["counterfactual_proposal_energy_v2"]["hypothetical_net_energy_kwh"] is not None
        counters["counterfactual_reverse_edge_moves"] += int(
            record["counterfactual_proposal_energy_v2"].get("reverse_edge_inferred_moves") or 0
        )
        counters["counterfactual_proxy_moves"] += int(
            record["counterfactual_proposal_energy_v2"].get("proxy_edge_moves") or 0
        )
        for segment in record["legal_prefix_segment_simulation"]["segments"]:
            counters[f"{segment['segment_type']}_segment_count"] += 1
        signed = record["legal_prefix_segment_simulation"]["signed_net_energy_kwh_sum"]
        billable = record["legal_prefix_segment_simulation"]["billable_energy_kwh_sum_by_frozen_segment"]
        if signed is not None:
            prefix_signed_total += float(signed)
        if billable is not None:
            prefix_billable_total += float(billable)
        output_records.append(record)

    if len(output_records) != int(audit.get("coverage", {}).get("records", -1)):
        raise SystemExit("v2_record_denominator_mismatch")
    if len(trace_sources) != len(output_records):
        raise SystemExit("trace_source_coverage_mismatch")

    # Confirm that inputs remained byte-identical throughout this offline pass.
    verify_hash_rows(verified_source_files, ROOT, "source_file")
    verify_hash_rows(trace_sources, ROOT, "source_trace")
    if sha256(source_manifest) != source_manifest_sha:
        raise SystemExit("source_manifest_changed_during_recalculation")
    if sha256(audit_path) != v2_audit_sha:
        raise SystemExit("v2_audit_changed_during_recalculation")
    if sha256(ENERGY_MODULE) != module_sha:
        raise SystemExit("energy_model_module_changed_during_recalculation")

    # Preserve v2 counters but clarify that proposal energy remains hypothetical.
    coverage = {
        **dict(counters),
        "legal_prefix_signed_net_energy_kwh_total": prefix_signed_total,
        "legal_prefix_billable_energy_kwh_sum_by_frozen_segment": prefix_billable_total,
        "failed_source_records": sum(row.get("source_status") == "failed" for row in output_records),
        "v2_counterfactual_full_proposal_energy_kwh_total": audit.get("coverage", {}).get("hypothetical_full_proposal_energy_kwh"),
        "v2_counterfactual_partial_proposal_energy_kwh_total": audit.get("coverage", {}).get("hypothetical_partial_proposal_energy_kwh"),
        "v2_no_route_proposed_count": audit.get("coverage", {}).get("not_estimable"),
    }
    output_payload = {
        "schema_version": "pure_llm_v7_ev2_segment_failure_audit_v3",
        "source_experiment": audit.get("source_experiment"),
        "source_experiment_status": audit.get("source_experiment_status"),
        "source_manifest_sha256": source_manifest_sha,
        "source_v2_audit_path": str(audit_path.relative_to(ROOT)),
        "source_v2_audit_sha256": v2_audit_sha,
        "calculation_scope": "offline EV2 phase recalculation from saved v7 result files, maps, task truth, and validated-prefix traces; no model calls or route reruns",
        "energy_model": model_metadata(profile),
        "energy_model_module_sha256": module_sha,
        "calculation_script_sha256": sha256(Path(__file__).resolve()),
        "coverage": coverage,
        "rules": {
            "deadhead_payload_mass_kg": 0.0,
            "ride_payload_mass_kg": profile.passenger_load_kg,
            "segment_signed_net_energy": "propulsion_draw - regenerated_energy + auxiliary_energy",
            "segment_billable_energy": "max(segment_signed_net_energy_kwh, 0)",
            "actual_route_ev2": "null for failed/incomplete v7 records; no completed accepted route or observed vehicle trajectory",
            "legal_prefix_energy": "offline simulation estimate only; never presented as observed vehicle consumption",
            "illegal_proposal_energy": "retained from v2 only as counterfactual; reverse/proxy edge counts remain explicit; billable energy is null",
            "per_phase_evaluator_boundary": "each phase is evaluated from initial_speed_mps=0 and terminal_stop=True; pickup stationary dwell is not added",
        },
        "source_files": verified_source_files,
        "source_trace_files": trace_sources,
        "limitations": [
            "All 150 v7 source task records failed; no complete route actual EV2 result is estimable.",
            "Legal-prefix values are deterministic simulation estimates of the exact legal edge prefix, not a physical execution trace.",
            "A prefix that does not reach pickup has only a deadhead-prefix segment; ride energy remains null.",
            "Pickup dwell and corresponding auxiliary energy are not modeled because no common frozen dwell value is available.",
            "Invalid full proposals, reverse-edge inferences, and proxy edges are counterfactual only and are not billed.",
            "Missing grade follows the EV2 module's flat-grade fallback and remains flagged per edge.",
        ],
        "records": output_records,
    }

    output_path = output_dir / "failure_energy_segment_audit_v3.json"
    write_json(output_path, output_payload)
    manifest = {
        "schema_version": "pure_llm_v7_ev2_segment_recalculation_manifest_v1",
        "status": "completed_offline_segment_recalculation",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_v2_audit_path": str(audit_path.relative_to(ROOT)),
        "source_v2_audit_sha256": v2_audit_sha,
        "source_manifest_sha256": source_manifest_sha,
        "energy_model_module_sha256": module_sha,
        "energy_profile_sha256": profile_sha,
        "calculation_script_sha256": sha256(Path(__file__).resolve()),
        "output": {
            "path": str(output_path.relative_to(ROOT)),
            "sha256": sha256(output_path),
            "records": len(output_records),
        },
        "coverage": coverage,
        "model_calls": 0,
        "route_reruns": 0,
        "source_results_modified": False,
        "source_hashes_reverified_after_recalculation": True,
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps({
        "status": manifest["status"],
        "manifest": str(manifest_path.relative_to(ROOT)),
        "manifest_sha256": sha256(manifest_path),
        "audit": str(output_path.relative_to(ROOT)),
        "audit_sha256": sha256(output_path),
        "coverage": coverage,
        "model_calls": 0,
        "route_reruns": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
