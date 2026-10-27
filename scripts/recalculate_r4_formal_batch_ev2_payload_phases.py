#!/usr/bin/env python3
"""Create immutable EV v2 payload-phase sidecars for completed R4 batch routes.

The script reads completed route states from an R4 formal batch and does not
rerun routing or call a model. Empty-leg edges end at pickup; passenger-loaded
edges start at pickup. Inputs and source artifacts are hash-recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from route_experiment.costs import evaluate_route_candidate
from route_experiment.energy_model import EnergyModelConfig, model_metadata
from route_experiment.models import EvaluationConfig, RouteCandidate


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_path(value: str, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--existing-sidecar-manifest", type=Path, action="append", default=[])
    args = parser.parse_args()

    batch_root = args.batch_root.resolve()
    output_dir = args.output_dir.resolve()
    manifest_path = batch_root / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"missing_batch_manifest:{manifest_path}")
    if output_dir.exists():
        raise SystemExit(f"refusing_to_overwrite:{output_dir}")

    source_manifest_bytes = manifest_path.read_bytes()
    source_manifest = json.loads(source_manifest_bytes)
    covered: set[str] = set()
    prior_manifests = []
    for prior_path in args.existing_sidecar_manifest:
        prior_path = prior_path.resolve()
        prior = read_json(prior_path)
        prior_manifests.append({"path": str(prior_path), "sha256": sha256(prior_path)})
        for row in prior.get("runs", []):
            source = row.get("source_run_dir") or row.get("run")
            if source:
                covered.add(str(Path(source).resolve()))

    profile = EnergyModelConfig()
    evaluation_config = EvaluationConfig(energy_profile=profile)
    rows = []
    for task in source_manifest.get("tasks", []):
        if task.get("status") != "route_complete":
            continue
        complete_runs = []
        for run in task.get("runs", []):
            out = run.get("output_dir")
            if not out:
                continue
            run_dir = resolve_path(out, batch_root)
            audit_path = run_dir / "final_audit.json"
            if audit_path.is_file():
                audit = read_json(audit_path)
                if audit.get("status") == "completed_route":
                    complete_runs.append((run_dir, audit_path, audit))
        if not complete_runs:
            run_dir = resolve_path(task["output_dir"], batch_root)
            audit_path = resolve_path(task.get("final_audit_path", str(run_dir / "final_audit.json")), batch_root)
            if audit_path.is_file() and read_json(audit_path).get("status") == "completed_route":
                complete_runs.append((run_dir, audit_path, read_json(audit_path)))

        for run_dir, audit_path, audit in complete_runs:
            run_key = str(run_dir.resolve())
            if run_key in covered:
                continue
            state_path = run_dir / "state.json"
            if not state_path.is_file():
                rows.append({"task_id": task.get("task_id"), "source_run_dir": run_key,
                             "status": "not_recalculated_state_missing"})
                continue
            state = read_json(state_path)
            if state.get("status") != "route_complete" or not audit.get("pickup_reached") or not audit.get("dropoff_reached"):
                rows.append({"task_id": task.get("task_id"), "source_run_dir": run_key,
                             "status": "not_recalculated_completion_evidence_mismatch"})
                continue

            nodes = list(state.get("accepted_nodes") or [])
            edge_ids = list(state.get("accepted_edges") or [])
            pickup = state.get("pickup_node_id") or audit.get("pickup_node")
            dropoff = state.get("dropoff_node_id") or audit.get("dropoff_node")
            if len(nodes) != len(edge_ids) + 1 or pickup not in nodes or nodes[-1] != dropoff:
                rows.append({"task_id": task.get("task_id"), "source_run_dir": run_key,
                             "status": "not_recalculated_endpoint_or_sequence_mismatch",
                             "node_count": len(nodes), "edge_count": len(edge_ids),
                             "pickup_node": pickup, "dropoff_node": dropoff})
                continue

            preflight_path = resolve_path(str(state.get("source_preflight", "")), ROOT)
            if not preflight_path.is_file():
                rows.append({"task_id": task.get("task_id"), "source_run_dir": run_key,
                             "status": "not_recalculated_preflight_missing"})
                continue
            preflight = read_json(preflight_path)
            map_data = preflight.get("map") or preflight.get("road_map")
            map_source_path = None
            map_source_sha256 = None
            map_ref = preflight.get("map_ref")
            if not isinstance(map_data, dict) and isinstance(map_ref, dict) and map_ref.get("path"):
                map_source_path = resolve_path(str(map_ref["path"]), ROOT)
                if not map_source_path.is_file():
                    rows.append({"task_id": task.get("task_id"), "source_run_dir": run_key,
                                 "status": "not_recalculated_map_reference_missing",
                                 "map_ref_path": str(map_source_path),
                                 "expected_map_sha256": map_ref.get("sha256")})
                    continue
                map_source_sha256 = sha256(map_source_path)
                expected_map_sha256 = map_ref.get("sha256")
                if expected_map_sha256 and map_source_sha256 != expected_map_sha256:
                    rows.append({"task_id": task.get("task_id"), "source_run_dir": run_key,
                                 "status": "not_recalculated_map_reference_hash_mismatch",
                                 "map_ref_path": str(map_source_path),
                                 "expected_map_sha256": expected_map_sha256,
                                 "actual_map_sha256": map_source_sha256})
                    continue
                map_document = read_json(map_source_path)
                map_data = map_document.get("map") or map_document.get("road_map") or map_document
            if not isinstance(map_data, dict):
                rows.append({"task_id": task.get("task_id"), "source_run_dir": run_key,
                             "status": "not_recalculated_map_missing",
                             "preflight_path": str(preflight_path),
                             "map_ref": map_ref})
                continue

            pickup_index = nodes.index(pickup)
            edge_by_id = {str(edge["edge_id"]): edge for edge in map_data.get("edges", [])}
            try:
                missing_edges = [edge for edge in edge_ids if edge not in edge_by_id]
                if missing_edges:
                    raise ValueError("route_edges_missing_from_frozen_map:" + ",".join(missing_edges[:5]))
                empty_edge_ids = edge_ids[:pickup_index]
                passenger_edge_ids = edge_ids[pickup_index:]
                if not empty_edge_ids or not passenger_edge_ids:
                    raise ValueError("pickup_phase_must_have_nonempty_edges")

                empty_route = RouteCandidate(
                    route_id=str(task["task_id"]) + ":empty",
                    node_sequence=nodes[:pickup_index + 1], edge_sequence=empty_edge_ids,
                    route_source="saved_completed_r4_route_ev2_sidecar",
                    route_reachability=True, validation_status="valid", payload_mass_kg=0.0,
                )
                passenger_route = RouteCandidate(
                    route_id=str(task["task_id"]) + ":passenger",
                    node_sequence=nodes[pickup_index:], edge_sequence=passenger_edge_ids,
                    route_source="saved_completed_r4_route_ev2_sidecar",
                    route_reachability=True, validation_status="valid",
                    payload_mass_kg=profile.passenger_load_kg,
                )
                empty = evaluate_route_candidate(map_data_to_roadmap(map_data), empty_route, evaluation_config)
                passenger = evaluate_route_candidate(map_data_to_roadmap(map_data), passenger_route, evaluation_config)
            except Exception as exc:
                rows.append({"task_id": task.get("task_id"), "source_run_dir": run_key,
                             "status": "not_recalculated_route_validation_failed", "reason": str(exc)})
                continue

            result = {
                "schema_version": "r4_ev_v2_payload_phase_recalculation_v1",
                "status": "offline_recalculated_completed_route",
                "task_id": task.get("task_id"), "seed": task.get("seed"),
                "sample_group": task.get("sample_group"), "source_run_dir": run_key,
                "route_status": state.get("status"),
                "route_selection_objective": "travel_time_then_distance",
                "route_algorithm_rerun": False, "model_calls": 0,
                "energy_model": model_metadata(profile),
                "energy_profile_sha256": profile.fingerprint(),
                "payload_phase_definition": {
                    "empty_leg_payload_mass_kg": 0.0,
                    "passenger_leg_payload_mass_kg": profile.passenger_load_kg,
                    "split_node_id": pickup,
                    "empty_edge_count": len(empty_edge_ids),
                    "passenger_edge_count": len(passenger_edge_ids),
                    "boundary_convention": "edges ending at pickup are empty; edges leaving pickup are passenger-loaded",
                },
                "metrics": {
                    "empty_leg_signed_net_energy_kwh": empty.energy_total_kwh,
                    "passenger_leg_signed_net_energy_kwh": passenger.energy_total_kwh,
                    "total_signed_net_energy_kwh": empty.energy_total_kwh + passenger.energy_total_kwh,
                    "empty_leg_billable_energy_kwh": max(empty.energy_total_kwh, 0.0),
                    "passenger_leg_billable_energy_kwh": max(passenger.energy_total_kwh, 0.0),
                    "total_billable_energy_kwh": max(empty.energy_total_kwh, 0.0) + max(passenger.energy_total_kwh, 0.0),
                    "empty_propulsion_draw_kwh": empty.energy_moving_kwh,
                    "passenger_propulsion_draw_kwh": passenger.energy_moving_kwh,
                    "empty_regenerated_kwh": empty.energy_regenerated_kwh,
                    "passenger_regenerated_kwh": passenger.energy_regenerated_kwh,
                    "empty_auxiliary_kwh": empty.energy_auxiliary_kwh,
                    "passenger_auxiliary_kwh": passenger.energy_auxiliary_kwh,
                    "empty_leg_duration_sec": empty.total_travel_time_sec,
                    "passenger_leg_duration_sec": passenger.total_travel_time_sec,
                    "total_duration_sec": empty.total_travel_time_sec + passenger.total_travel_time_sec,
                    "prior_full_route_passenger_payload_energy_kwh": audit.get("completed_route_energy_kwh"),
                    "account_cash_cost": None, "electricity_tariff": None,
                },
                "evidence": {
                    "state_path": str(state_path), "state_sha256": sha256(state_path),
                    "final_audit_path": str(audit_path), "final_audit_sha256": sha256(audit_path),
                    "preflight_path": str(preflight_path), "preflight_sha256": sha256(preflight_path),
                    "map_source_path": str(map_source_path) if map_source_path else None,
                    "map_source_sha256": map_source_sha256,
                    "preflight_map_ref_sha256": map_ref.get("sha256") if isinstance(map_ref, dict) else None,
                    "map_id": map_data.get("map_id"), "map_version": map_data.get("version"),
                    "accepted_nodes_sha256": hashlib.sha256(json.dumps(nodes, separators=(",", ":")).encode()).hexdigest(),
                    "accepted_edges_sha256": hashlib.sha256(json.dumps(edge_ids, separators=(",", ":")).encode()).hexdigest(),
                    "source_manifest_sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
                },
            }
            target = output_dir / f"seed_{task.get('seed')}" / str(task.get("task_id")) / (run_dir.name + ".json")
            target.parent.mkdir(parents=True, exist_ok=False)
            target.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            rows.append({"task_id": task.get("task_id"), "seed": task.get("seed"),
                         "sample_group": task.get("sample_group"), "source_run_dir": run_key,
                         "source_state_sha256": sha256(state_path), "output": str(target),
                         "output_sha256": sha256(target), "status": result["status"]})
            covered.add(run_key)

    output_dir.mkdir(parents=True, exist_ok=True)
    missing = [row for row in rows if row.get("status", "").startswith("not_recalculated")]
    manifest = {
        "schema_version": "r4_formal_batch_ev2_payload_phase_sidecar_v1",
        "status": "passed" if not missing else "completed_with_gaps",
        "created_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "batch_root": str(batch_root),
        "batch_manifest_sha256_at_scan": hashlib.sha256(source_manifest_bytes).hexdigest(),
        "previous_sidecar_manifests": prior_manifests,
        "frozen_sample_denominator_changed": False,
        "route_algorithm_rerun": False,
        "model_calls": 0,
        "energy_model": model_metadata(profile),
        "route_complete_source_run_count_scanned": len([r for r in rows if r.get("status") == "offline_recalculated_completed_route"]),
        "recalculated_route_count": len([r for r in rows if r.get("status") == "offline_recalculated_completed_route"]),
        "error_count": len(missing),
        "runs": rows,
        "errors": missing,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), "status": manifest["status"],
                      "recalculated_route_count": manifest["recalculated_route_count"],
                      "error_count": manifest["error_count"], "model_calls": 0}, ensure_ascii=False))
    return 0 if not missing else 2


def map_data_to_roadmap(map_data: dict[str, Any]):
    from route_experiment.models import Edge, Node, RoadMap
    return RoadMap(
        map_id=map_data["map_id"], version=map_data["version"], size=map_data["size"],
        nodes=[Node(**n) for n in map_data["nodes"]],
        edges=[Edge(**e) for e in map_data["edges"]],
        boundary=map_data["boundary"],
        road_state_version=map_data.get("road_state_version", "road_state_v1"),
        roadside_features=map_data.get("roadside_features", []),
    )


if __name__ == "__main__":
    raise SystemExit(main())
