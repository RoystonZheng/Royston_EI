#!/usr/bin/env python3
"""Recalculate saved R4 grid routes and legal prefixes from their frozen maps."""

from __future__ import annotations

import hashlib
import json
import argparse
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from route_experiment.costs import evaluate_route_candidate
from route_experiment.energy_model import EnergyModelConfig, model_metadata
from route_experiment.models import Edge, EvaluationConfig, Node, RoadMap, RouteCandidate


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_road_map(value: Dict[str, Any]) -> RoadMap:
    return RoadMap(
        map_id=value["map_id"],
        version=value["version"],
        size=value["size"],
        nodes=[Node(**node) for node in value["nodes"]],
        edges=[Edge(**edge) for edge in value["edges"]],
        boundary=value["boundary"],
        road_state_version=value.get("road_state_version", "road_state_v1"),
        roadside_features=value.get("roadside_features", []),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recalculate saved local R4 routes and prefixes into a new immutable "
            "sidecar directory. Source run folders are never modified."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New output directory for per-run EV v2 JSON files and manifest; must not already exist.",
    )
    args = parser.parse_args()
    output_dir = args.output_dir.expanduser()
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise SystemExit(f"refusing_to_overwrite:{output_dir}")

    profile = EnergyModelConfig()
    config = EvaluationConfig(energy_profile=profile)
    rows = []
    sidecar_payloads = []
    for state_path in sorted((ROOT / "outputs/route_layer").glob("r4_codex_*/state.json")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        edge_ids = list(state.get("accepted_edges") or [])
        if not edge_ids:
            continue
        source_relative = state.get("source_preflight")
        source_path = ROOT / str(source_relative) if source_relative else None
        if source_path is None or not source_path.is_file():
            rows.append({"run": state_path.parent.name, "status": "not_recalculated_source_map_missing", "edge_count": len(edge_ids)})
            continue
        source = json.loads(source_path.read_text(encoding="utf-8"))
        map_data = source.get("map") or source.get("road_map")
        if not isinstance(map_data, dict):
            rows.append({"run": state_path.parent.name, "status": "not_recalculated_map_missing", "edge_count": len(edge_ids)})
            continue
        try:
            road_map = load_road_map(map_data)
            candidate = RouteCandidate(
                route_id=state.get("task_id") or state_path.parent.name,
                node_sequence=list(state.get("accepted_nodes") or []),
                edge_sequence=edge_ids,
                route_source="r4_saved_legal_prefix",
                route_reachability=True,
                validation_status="valid",
            )
            if len(candidate.node_sequence) != len(edge_ids) + 1:
                candidate.node_sequence = []
            result = evaluate_route_candidate(road_map, candidate, config)
            final_audit_path = state_path.parent / "final_audit.json"
            state_status = state.get("status")
            if state_status == "route_complete":
                recalculation_status = "recalculated_complete_route"
            elif state_status == "ready":
                recalculation_status = "recalculated_preflight_prefix"
            else:
                recalculation_status = "recalculated_legal_prefix"
            row = {
                "run": state_path.parent.name,
                "status": recalculation_status,
                "route_status": state_status,
                "is_complete_route": state_status == "route_complete",
                "edge_count": len(edge_ids),
                "distance_m": result.route_distance_m,
                "duration_sec": result.total_travel_time_sec,
                "energy_kwh": result.energy_total_kwh,
                "propulsion_draw_kwh": result.energy_moving_kwh,
                "regenerated_kwh": result.energy_regenerated_kwh,
                "auxiliary_kwh": result.energy_auxiliary_kwh,
                "components_wheel_kwh": {
                    "rolling": result.energy_rolling_wheel_kwh,
                    "aerodynamic": result.energy_aerodynamic_wheel_kwh,
                    "grade": result.energy_grade_wheel_kwh,
                    "acceleration": result.energy_acceleration_wheel_kwh,
                },
                "energy_model_version": result.energy_model_version,
                "energy_profile_id": result.energy_profile_id,
                "energy_profile_sha256": result.energy_profile_sha256,
                "energy_trace_fidelity": result.energy_trace_fidelity,
                "energy_grade_missing": result.energy_grade_missing,
                "state_sha256": sha256(state_path),
                "source_preflight": str(source_relative),
                "source_preflight_sha256": sha256(source_path),
                "final_audit_sha256": sha256(final_audit_path) if final_audit_path.is_file() else None,
            }
            sidecar_payloads.append((state_path.parent.name, {"energy_model": model_metadata(profile), "result": row}))
            rows.append(row)
        except Exception as exc:
            rows.append({
                "run": state_path.parent.name,
                "status": "not_recalculated_route_validation_failed",
                "edge_count": len(edge_ids),
                "reason": str(exc),
            })
    output_dir.mkdir(parents=True, exist_ok=False)
    for run_name, payload in sidecar_payloads:
        sidecar_path = output_dir / run_name / "energy_v2_recalculation.json"
        sidecar_path.parent.mkdir(parents=True, exist_ok=False)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        sidecar_path.write_text(encoded, encoding="utf-8")
        row = next(item for item in rows if item.get("run") == run_name and item.get("status", "").startswith("recalculated_"))
        row["sidecar_path"] = str(sidecar_path)
        row["sidecar_sha256"] = sha256(sidecar_path)

    output = output_dir / "manifest.json"
    output.write_text(
        json.dumps({
            "schema_version": "r4_grid_energy_recalculation_v2",
            "status": "completed_offline_recalculation",
            "energy_model": model_metadata(profile),
            "route_algorithm_rerun": False,
            "source_results_modified": False,
            "output_dir": str(output_dir),
            "scope_note": "Every reported non-complete result is a validated prefix or a preflight state; only route_complete is a completed route.",
            "completed_routes": sum(1 for row in rows if row.get("is_complete_route")),
            "legal_prefixes": sum(1 for row in rows if row.get("status") == "recalculated_legal_prefix"),
            "preflight_prefixes": sum(1 for row in rows if row.get("status") == "recalculated_preflight_prefix"),
            "not_recalculated": sum(1 for row in rows if row.get("status", "").startswith("not_recalculated")),
            "runs": rows,
        }, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "manifest": str(output),
        "runs": len(rows),
        "completed_routes": sum(1 for row in rows if row.get("is_complete_route")),
        "legal_prefixes": sum(1 for row in rows if row.get("status") == "recalculated_legal_prefix"),
        "preflight_prefixes": sum(1 for row in rows if row.get("status") == "recalculated_preflight_prefix"),
        "not_recalculated": sum(1 for row in rows if row.get("status", "").startswith("not_recalculated")),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
