#!/usr/bin/env python3
"""Recalculate completed R4 routes with empty and passenger payload phases.

This is an offline accounting pass over saved, validator-complete routes. It
does not call a model, rerun route selection, or overwrite source run files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from route_experiment.energy_model import EnergyModelConfig, evaluate_route_edges, model_metadata  # noqa: E402
from route_experiment.models import EvaluationConfig  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_preflight(state: Dict[str, Any], project_root: Path) -> Path:
    source = state.get("source_preflight")
    if not source:
        raise ValueError("source_preflight_missing")
    path = Path(source)
    if not path.is_absolute():
        path = project_root / path
    if not path.is_file():
        raise ValueError("source_preflight_not_found:" + str(path))
    expected = state.get("source_preflight_sha256")
    if expected and sha256(path) != expected:
        raise ValueError("source_preflight_hash_mismatch:" + str(path))
    return path.resolve()


def map_from_preflight(preflight: Dict[str, Any], project_root: Path) -> Dict[str, Any]:
    road_map = preflight.get("map") or preflight.get("road_map")
    if isinstance(road_map, dict):
        return road_map
    ref = preflight.get("map_ref")
    if not isinstance(ref, dict) or not ref.get("path"):
        raise ValueError("map_data_missing")
    path = Path(ref["path"])
    if not path.is_absolute():
        path = project_root / path
    if not path.is_file():
        raise ValueError("map_ref_file_missing:" + str(path))
    if ref.get("sha256") and sha256(path) != ref["sha256"]:
        raise ValueError("map_ref_hash_mismatch:" + str(path))
    data = read_json(path)
    return data.get("map") or data.get("road_map") or data


def evaluate_one(task: Dict[str, Any], output_dir: Path, project_root: Path,
                 profile: EnergyModelConfig, evaluation: EvaluationConfig) -> Dict[str, Any]:
    run_dir = Path(task["output_dir"]).resolve()
    state_path = run_dir / "state.json"
    audit_path = run_dir / "final_audit.json"
    if not state_path.is_file() or not audit_path.is_file():
        raise ValueError("completed_route_state_or_final_audit_missing:" + str(run_dir))
    state = read_json(state_path)
    audit = read_json(audit_path)
    if state.get("status") != "route_complete" or audit.get("status") != "completed_route":
        raise ValueError("source_run_not_validator_complete:" + str(run_dir))

    nodes = list(state.get("accepted_nodes") or [])
    edge_ids = list(state.get("accepted_edges") or [])
    if len(nodes) != len(edge_ids) + 1 or not nodes:
        raise ValueError("accepted_node_edge_count_mismatch:" + str(run_dir))
    pickup = state.get("pickup_node_id")
    dropoff = state.get("dropoff_node_id")
    if pickup not in nodes:
        raise ValueError("pickup_not_reached_in_saved_route:" + str(run_dir))
    pickup_index = nodes.index(pickup)
    if nodes[-1] != dropoff:
        raise ValueError("dropoff_not_final_node:" + str(run_dir))

    preflight_path = resolve_preflight(state, project_root)
    preflight = read_json(preflight_path)
    map_data = map_from_preflight(preflight, project_root)
    edges_by_id = {str(edge["edge_id"]): edge for edge in map_data.get("edges", [])}
    ordered_edges: List[Dict[str, Any]] = []
    for index, edge_id in enumerate(edge_ids):
        edge = edges_by_id.get(str(edge_id))
        if edge is None:
            raise ValueError("accepted_edge_missing_from_frozen_map:" + str(edge_id))
        if edge.get("from_node") != nodes[index] or edge.get("to_node") != nodes[index + 1]:
            raise ValueError("accepted_edge_direction_or_sequence_mismatch:" + str(edge_id))
        ordered_edges.append(edge)

    empty_edges = ordered_edges[:pickup_index]
    passenger_edges = ordered_edges[pickup_index:]
    empty = evaluate_route_edges(
        empty_edges, evaluation.congestion_speed_multipliers, profile,
        payload_mass_kg=0.0,
    )
    passenger = evaluate_route_edges(
        passenger_edges, evaluation.congestion_speed_multipliers, profile,
        payload_mass_kg=float(profile.passenger_load_kg),
    )
    old_all_loaded = evaluate_route_edges(
        ordered_edges, evaluation.congestion_speed_multipliers, profile,
        payload_mass_kg=float(profile.passenger_load_kg),
    )
    signed_net = float(empty.net_kwh + passenger.net_kwh)
    billable = float(max(empty.net_kwh, 0.0) + max(passenger.net_kwh, 0.0))
    old_value = audit.get("completed_route_energy_kwh")
    if old_value is None:
        old_value = audit.get("accepted_prefix_energy_kwh")

    payload = {
        "schema_version": "r4_ev_v2_payload_phase_recalculation_v1",
        "status": "offline_recalculated_completed_route",
        "task_id": task["task_id"],
        "seed": task.get("seed"),
        "sample_group": task.get("sample_group"),
        "source_run_dir": str(run_dir),
        "route_status": state.get("status"),
        "route_selection_objective": state.get("route_selection_objective", "travel_time_then_distance"),
        "route_algorithm_rerun": False,
        "model_calls": 0,
        "energy_model": model_metadata(profile),
        "energy_profile_sha256": profile.fingerprint(),
        "payload_phase_definition": {
            "empty_leg_payload_mass_kg": 0.0,
            "passenger_leg_payload_mass_kg": float(profile.passenger_load_kg),
            "split_node_id": pickup,
            "empty_edge_count": len(empty_edges),
            "passenger_edge_count": len(passenger_edges),
            "boundary_convention": "edges ending at pickup are empty; edges leaving pickup are passenger-loaded",
        },
        "metrics": {
            "empty_leg_signed_net_energy_kwh": float(empty.net_kwh),
            "passenger_leg_signed_net_energy_kwh": float(passenger.net_kwh),
            "total_signed_net_energy_kwh": signed_net,
            "empty_leg_billable_energy_kwh": float(max(empty.net_kwh, 0.0)),
            "passenger_leg_billable_energy_kwh": float(max(passenger.net_kwh, 0.0)),
            "total_billable_energy_kwh": billable,
            "prior_full_route_passenger_payload_energy_kwh": float(old_value) if old_value is not None else None,
            "same_route_all_passenger_payload_recalculated_kwh": float(old_all_loaded.net_kwh),
            "payload_split_delta_vs_prior_final_audit_kwh": (
                signed_net - float(old_value) if old_value is not None else None
            ),
            "account_cash_cost": None,
            "electricity_tariff": None,
        },
        "evidence": {
            "state_path": str(state_path),
            "state_sha256": sha256(state_path),
            "final_audit_path": str(audit_path),
            "final_audit_sha256": sha256(audit_path),
            "preflight_path": str(preflight_path),
            "preflight_sha256": sha256(preflight_path),
            "map_id": map_data.get("map_id"),
            "map_version": map_data.get("version"),
            "map_content_sha256": canonical_sha256(map_data),
            "accepted_nodes_sha256": canonical_sha256(nodes),
            "accepted_edges_sha256": canonical_sha256(edge_ids),
        },
    }
    relative = Path(f"seed_{task.get('seed')}") / str(task["task_id"]) / (hashlib.sha256(str(run_dir).encode()).hexdigest()[:12] + ".json")
    target = output_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return {
        "task_id": task["task_id"], "seed": task.get("seed"),
        "source_run_dir": str(run_dir), "output": str(target),
        "output_sha256": sha256(target), "state_sha256": payload["evidence"]["state_sha256"],
        "final_audit_sha256": payload["evidence"]["final_audit_sha256"],
        "preflight_sha256": payload["evidence"]["preflight_sha256"],
        "energy_profile_sha256": profile.fingerprint(),
        "empty_leg_signed_net_energy_kwh": float(empty.net_kwh),
        "passenger_leg_signed_net_energy_kwh": float(passenger.net_kwh),
        "total_signed_net_energy_kwh": signed_net,
        "total_billable_energy_kwh": billable,
        "prior_full_route_passenger_payload_energy_kwh": float(old_value) if old_value is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", required=True, type=Path,
                        help="authoritative R4 batch directory containing manifest.json and parallel_workers")
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    batch_root = args.batch_root.resolve()
    project_root = args.project_root.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise SystemExit("refusing_to_overwrite_existing_output_dir:" + str(output_dir))
    output_dir.mkdir(parents=True)

    manifest_paths = [batch_root / "manifest.json"]
    workers = batch_root / "parallel_workers"
    if workers.is_dir():
        manifest_paths += sorted(p for p in workers.glob("worker*/manifest.json") if p.is_file())
    selected: Dict[str, Dict[str, Any]] = {}
    source_manifests = []
    for manifest_path in manifest_paths:
        if not manifest_path.is_file():
            continue
        manifest = read_json(manifest_path)
        source_manifests.append({"path": str(manifest_path), "sha256": sha256(manifest_path)})
        tasks = manifest.get("tasks", [])
        if isinstance(tasks, dict):
            tasks = list(tasks.values())
        for task in tasks:
            if not isinstance(task, dict) or not task.get("output_dir"):
                continue
            state_path = Path(task["output_dir"]).resolve() / "state.json"
            if not state_path.is_file():
                continue
            try:
                state = read_json(state_path)
            except Exception:
                continue
            if state.get("status") != "route_complete":
                continue
            selected[str(Path(task["output_dir"]).resolve())] = task

    profile = EnergyModelConfig()
    evaluation = EvaluationConfig(energy_profile=profile)
    rows = []
    errors = []
    for task in selected.values():
        try:
            rows.append(evaluate_one(task, output_dir, project_root, profile, evaluation))
        except Exception as error:
            errors.append({"task_id": task.get("task_id"), "source_run_dir": task.get("output_dir"),
                           "status": "recalculation_error", "reason": str(error)})

    manifest = {
        "schema_version": "r4_ev_v2_payload_phase_recalculation_manifest_v1",
        "status": "passed" if not errors else "completed_with_errors",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "batch_root": str(batch_root),
        "batch_manifest_sha256_at_scan": sha256(batch_root / "manifest.json"),
        "source_manifests": source_manifests,
        "frozen_sample_denominator_changed": False,
        "route_algorithm_rerun": False,
        "model_calls": 0,
        "energy_model": model_metadata(profile),
        "energy_profile_sha256": profile.fingerprint(),
        "route_complete_source_run_count": len(selected),
        "recalculated_route_count": len(rows),
        "error_count": len(errors),
        "runs": rows,
        "errors": errors,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), "status": manifest["status"],
                      "routes": len(rows), "errors": len(errors),
                      "energy_profile_sha256": profile.fingerprint()}, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
