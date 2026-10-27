#!/usr/bin/env python3
"""Add EV v2 sidecars for R4 route states absent from the existing manifest."""

from __future__ import annotations

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
from scripts.recalculate_r4_energy_v2 import load_road_map


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    route_root = ROOT / "outputs/route_layer"
    old_manifest_path = route_root / "r4_energy_v2_recalculation_manifest.json"
    old_manifest = read_json(old_manifest_path)
    already_present = {row["run"] for row in old_manifest.get("runs", [])}
    output = route_root / "r4_energy_v2_incremental_reconciliation_20261027_v1"
    if output.exists():
        raise SystemExit(f"refusing_to_overwrite:{output}")

    profile = EnergyModelConfig()
    config = EvaluationConfig(energy_profile=profile)
    rows = []
    for state_path in sorted(route_root.glob("r4_*/state.json")):
        run_name = state_path.parent.name
        state = read_json(state_path)
        edge_ids = list(state.get("accepted_edges") or [])
        if not edge_ids or run_name in already_present:
            continue
        source_relative = state.get("source_preflight")
        source_path = ROOT / str(source_relative) if source_relative else None
        if source_path is None or not source_path.is_file():
            rows.append({"run": run_name, "status": "not_recalculated_source_map_missing", "edge_count": len(edge_ids)})
            continue
        source = read_json(source_path)
        map_data = source.get("map") or source.get("road_map")
        if not isinstance(map_data, dict):
            rows.append({"run": run_name, "status": "not_recalculated_map_missing", "edge_count": len(edge_ids)})
            continue

        try:
            road_map = load_road_map(map_data)
            nodes = list(state.get("accepted_nodes") or [])
            if len(nodes) != len(edge_ids) + 1:
                nodes = []
            candidate = RouteCandidate(
                route_id=state.get("task_id") or run_name,
                node_sequence=nodes,
                edge_sequence=edge_ids,
                route_source="r4_saved_legal_prefix",
                route_reachability=True,
                validation_status="valid",
            )
            result = evaluate_route_candidate(road_map, candidate, config)
            final_audit = state_path.parent / "final_audit.json"
            is_complete = state.get("status") == "route_complete"
            status = "recalculated_complete_route" if is_complete else "recalculated_legal_prefix"
            row = {
                "run": run_name,
                "status": status,
                "route_status": state.get("status"),
                "is_complete_route": is_complete,
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
                "final_audit_sha256": sha256(final_audit) if final_audit.is_file() else None,
            }
            destination = output / run_name / "energy_v2_recalculation.json"
            destination.parent.mkdir(parents=True, exist_ok=False)
            destination.write_text(
                json.dumps({"energy_model": model_metadata(profile), "result": row}, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            row["sidecar"] = str(destination.relative_to(ROOT))
            row["sidecar_sha256"] = sha256(destination)
            rows.append(row)
        except Exception as exc:
            rows.append({"run": run_name, "status": "not_recalculated_route_validation_failed", "edge_count": len(edge_ids), "reason": str(exc)})

    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "r4_grid_energy_recalculation_v2_incremental",
        "status": "completed_offline_reconciliation",
        "base_manifest": str(old_manifest_path.relative_to(ROOT)),
        "base_manifest_sha256": sha256(old_manifest_path),
        "energy_model": model_metadata(profile),
        "route_algorithm_rerun": False,
        "source_run_files_preserved": True,
        "scope_note": "Sidecars extend the existing EV v2 audit only; non-complete tasks remain valid prefixes or unknown/stopped states, not complete-route results.",
        "completed_routes": sum(row.get("is_complete_route") is True for row in rows),
        "legal_prefixes": sum(row.get("status") == "recalculated_legal_prefix" for row in rows),
        "not_recalculated": sum(str(row.get("status", "")).startswith("not_recalculated") for row in rows),
        "runs": rows,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "manifest": str(manifest_path),
        "runs": len(rows),
        "completed_routes": manifest["completed_routes"],
        "legal_prefixes": manifest["legal_prefixes"],
        "not_recalculated": manifest["not_recalculated"],
    }, ensure_ascii=False, indent=2))
    return 0 if manifest["not_recalculated"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
