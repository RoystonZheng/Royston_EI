#!/usr/bin/env python3
"""Create an immutable v5 segment-economics sidecar for completed R4 routes.

The script reads a frozen R4 batch snapshot and completed run artifacts. It
does not modify source results, rerun route selection, or call a model. Missing
tariffs, fares, or method-level usage prices remain null; route ranking stays
blocked unless a complete shared candidate pool is available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from route_experiment.costs import evaluate_route_candidate  # noqa: E402
from route_experiment.decision import (  # noqa: E402
    DECISION_MODEL_VERSION,
    DECISION_PROFILES,
    aggregate_route_segment_economics,
)
from route_experiment.energy_model import EnergyModelConfig, model_metadata  # noqa: E402
from route_experiment.models import (  # noqa: E402
    Edge,
    EvaluationConfig,
    Node,
    RoadMap,
    RouteCandidate,
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
            count += 1
    return count


def resolve_input(value: str | Path, batch_root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    candidates = [PROJECT_ROOT / path, batch_root / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def as_road_map(value: Dict[str, Any]) -> RoadMap:
    return RoadMap(
        map_id=value["map_id"],
        version=value["version"],
        size=value["size"],
        nodes=[Node(**row) for row in value["nodes"]],
        edges=[Edge(**row) for row in value["edges"]],
        boundary=value["boundary"],
        road_state_version=value.get("road_state_version", "road_state_v1"),
        roadside_features=value.get("roadside_features", []),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--currency", default="CNY")
    parser.add_argument("--electricity-price-per-kwh", type=float, default=None)
    parser.add_argument("--electricity-price-source", default=None)
    parser.add_argument("--fare-per-km", type=float, default=None)
    parser.add_argument("--fare-basis", default=None)
    args = parser.parse_args()

    if args.electricity_price_per_kwh is not None:
        if args.electricity_price_per_kwh < 0 or not args.electricity_price_source:
            raise SystemExit("explicit_electricity_price_requires_nonnegative_value_and_source")
    if args.fare_per_km is not None:
        if args.fare_per_km < 0 or not args.fare_basis:
            raise SystemExit("explicit_fare_rate_requires_nonnegative_value_and_basis")

    batch_root = args.batch_root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    manifest_path = batch_root / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"missing_batch_manifest:{manifest_path}")
    if output_dir.exists():
        raise SystemExit(f"refusing_to_overwrite:{output_dir}")

    source_manifest_raw = manifest_path.read_bytes()
    source_manifest_sha = sha256_bytes(source_manifest_raw)
    source_manifest = json.loads(source_manifest_raw)
    profile = EnergyModelConfig()
    evaluation_config = EvaluationConfig(energy_profile=profile)
    run_rows: list[Dict[str, Any]] = []
    segment_rows: list[Dict[str, Any]] = []
    source_hashes: Dict[str, Dict[str, str]] = {}
    seen_run_dirs: set[str] = set()
    counters: Counter[str] = Counter()

    for task in source_manifest.get("tasks", []):
        if task.get("status") != "route_complete":
            continue
        task_runs = list(task.get("runs", []))
        if not task_runs and task.get("output_dir"):
            task_runs = [{"output_dir": task["output_dir"]}]
        for run in task_runs:
            output_value = run.get("output_dir")
            if not output_value:
                continue
            run_dir = Path(output_value).expanduser()
            if not run_dir.is_absolute():
                run_dir = batch_root / run_dir
            run_dir = run_dir.resolve()
            run_key = str(run_dir)
            if run_key in seen_run_dirs:
                continue
            audit_path = run_dir / "final_audit.json"
            if not audit_path.is_file():
                continue
            audit = read_json(audit_path)
            if audit.get("status") != "completed_route":
                continue
            seen_run_dirs.add(run_key)

            state_path = run_dir / "state.json"
            if not state_path.is_file():
                raise SystemExit(f"completed_route_state_missing:{run_dir}")
            state = read_json(state_path)
            if state.get("status") != "route_complete":
                raise SystemExit(f"completed_route_state_status_mismatch:{run_dir}")
            if not audit.get("pickup_reached") or not audit.get("dropoff_reached"):
                raise SystemExit(f"completed_route_endpoint_audit_missing:{run_dir}")

            nodes = list(state.get("accepted_nodes") or [])
            edge_ids = list(state.get("accepted_edges") or [])
            pickup = state.get("pickup_node_id") or audit.get("pickup_node")
            dropoff = state.get("dropoff_node_id") or audit.get("dropoff_node")
            if len(nodes) != len(edge_ids) + 1 or pickup not in nodes or nodes[-1] != dropoff:
                raise SystemExit(f"completed_route_sequence_mismatch:{run_dir}")
            pickup_index = nodes.index(pickup)
            empty_edge_ids = edge_ids[:pickup_index]
            passenger_edge_ids = edge_ids[pickup_index:]
            if not empty_edge_ids or not passenger_edge_ids:
                raise SystemExit(f"completed_route_payload_split_invalid:{run_dir}")

            state_sha = sha256_file(state_path)
            audit_sha = sha256_file(audit_path)
            preflight_value = state.get("source_preflight")
            if not preflight_value:
                raise SystemExit(f"completed_route_preflight_reference_missing:{run_dir}")
            preflight_path = resolve_input(preflight_value, batch_root)
            if not preflight_path.is_file():
                raise SystemExit(f"completed_route_preflight_missing:{preflight_path}")
            preflight = read_json(preflight_path)
            preflight_sha = sha256_file(preflight_path)
            map_data = preflight.get("map") or preflight.get("road_map")
            map_path = None
            map_sha = None
            map_ref = preflight.get("map_ref")
            if not isinstance(map_data, dict) and isinstance(map_ref, dict) and map_ref.get("path"):
                map_path = resolve_input(map_ref["path"], batch_root)
                if not map_path.is_file():
                    raise SystemExit(f"completed_route_frozen_map_missing:{map_path}")
                map_sha = sha256_file(map_path)
                if map_ref.get("sha256") and map_sha != map_ref["sha256"]:
                    raise SystemExit(f"completed_route_frozen_map_hash_mismatch:{map_path}")
                map_document = read_json(map_path)
                map_data = map_document.get("map") or map_document.get("road_map") or map_document
            if not isinstance(map_data, dict):
                raise SystemExit(f"completed_route_map_unavailable:{run_dir}")

            road_map = as_road_map(map_data)
            edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
            missing_edges = [edge_id for edge_id in edge_ids if edge_id not in edge_by_id]
            if missing_edges:
                raise SystemExit(f"completed_route_edge_missing:{run_dir}:{missing_edges[:5]}")

            empty_candidate = RouteCandidate(
                route_id=f"{task.get('task_id')}:deadhead",
                node_sequence=nodes[: pickup_index + 1],
                edge_sequence=empty_edge_ids,
                route_source="saved_completed_r4_route_v5_economics_sidecar",
                route_reachability=True,
                validation_status="valid",
                payload_mass_kg=0.0,
            )
            passenger_candidate = RouteCandidate(
                route_id=f"{task.get('task_id')}:passenger",
                node_sequence=nodes[pickup_index:],
                edge_sequence=passenger_edge_ids,
                route_source="saved_completed_r4_route_v5_economics_sidecar",
                route_reachability=True,
                validation_status="valid",
                payload_mass_kg=profile.passenger_load_kg,
            )
            empty_eval = evaluate_route_candidate(road_map, empty_candidate, evaluation_config)
            passenger_eval = evaluate_route_candidate(road_map, passenger_candidate, evaluation_config)

            phase_specs = [
                ("deadhead", "deadhead", 0.0, empty_candidate, empty_eval),
                ("passenger", "passenger", profile.passenger_load_kg, passenger_candidate, passenger_eval),
            ]
            aggregate_inputs = []
            phase_meta = []
            for segment_id, segment_type, payload_mass, candidate, evaluation in phase_specs:
                links = [dict(row) for row in evaluation.energy_segments]
                aggregate_inputs.append(
                    {
                        "segment_id": segment_id,
                        "segment_type": segment_type,
                        "distance_m": evaluation.route_distance_m,
                        "energy_kwh": evaluation.energy_total_kwh,
                        "travel_time_sec": evaluation.total_travel_time_sec,
                        "road_links": links,
                        "fare_per_km": args.fare_per_km if segment_type == "passenger" else None,
                        "fare_basis": args.fare_basis if segment_type == "passenger" else "known_zero_for_deadhead",
                        "electricity_price_per_kwh": args.electricity_price_per_kwh,
                        "price_basis": args.electricity_price_source,
                    }
                )
                phase_meta.append(
                    {
                        "segment_id": segment_id,
                        "segment_type": segment_type,
                        "payload_mass_kg": payload_mass,
                        "edge_ids_sha256": sha256_bytes(json.dumps(candidate.edge_sequence, separators=(",", ":")).encode()),
                        "distance_m": evaluation.route_distance_m,
                        "travel_time_sec": evaluation.total_travel_time_sec,
                        "signed_net_energy_kwh": evaluation.energy_total_kwh,
                        "billable_energy_kwh": max(0.0, evaluation.energy_total_kwh),
                        "propulsion_draw_kwh": evaluation.energy_moving_kwh,
                        "regenerated_kwh": evaluation.energy_regenerated_kwh,
                        "auxiliary_kwh": evaluation.energy_auxiliary_kwh,
                        "energy_grade_missing": evaluation.energy_grade_missing,
                        "road_link_count": len(links),
                    }
                )

            economics = aggregate_route_segment_economics(
                aggregate_inputs,
                currency=args.currency,
                electricity_price_per_kwh=args.electricity_price_per_kwh,
                fare_per_km=args.fare_per_km,
                price_basis=args.electricity_price_source,
                fare_basis=args.fare_basis,
            )
            if any(not row.get("road_link_breakdown_reconciles") for row in economics["segments"]):
                raise SystemExit(f"segment_link_reconciliation_failed:{run_dir}")
            if abs(float(economics["energy_kwh"]) - sum(float(row["signed_net_energy_kwh"]) for row in phase_meta)) > 1e-9:
                raise SystemExit(f"segment_energy_aggregate_mismatch:{run_dir}")

            source_hashes[run_key] = {
                "state.json": state_sha,
                "final_audit.json": audit_sha,
                "preflight": preflight_sha,
                "map": map_sha or (map_ref.get("sha256") if isinstance(map_ref, dict) else "embedded_in_preflight"),
            }
            current_objective = state.get("route_selection_objective") or audit.get("route_selection_objective")
            blockers = [
                "no_complete_shared_feasible_candidate_pool_across_methods_for_same_task",
                "frozen_electricity_tariff_missing" if args.electricity_price_per_kwh is None else None,
                "passenger_fare_basis_missing" if args.fare_per_km is None else None,
                "per_run_method_usage_and_cost_rate_unavailable",
            ]
            blockers = [item for item in blockers if item]
            profile_gates = [
                {
                    "profile_id": decision_profile.profile_id,
                    "time_weight": decision_profile.time_weight,
                    "money_weight": decision_profile.money_weight,
                    "status": "blocked_no_complete_shared_route_candidate_pool",
                    "selected_candidate_id": None,
                    "ranked_candidates": [],
                }
                for decision_profile in DECISION_PROFILES.values()
            ]

            run_row = {
                "schema_version": "r4_formal_batch_time_money_v5_run_economics_v1",
                "task_id": task.get("task_id"),
                "seed": task.get("seed"),
                "sample_group": task.get("sample_group"),
                "source_run_dir": run_key,
                "route_status": state.get("status"),
                "historical_route_selection_objective": current_objective,
                "route_algorithm_rerun": False,
                "new_model_calls": 0,
                "decision_model_version": DECISION_MODEL_VERSION,
                "energy_model": model_metadata(profile),
                "energy_profile_sha256": profile.fingerprint(),
                "segments": phase_meta,
                "route_segment_economics_v5": economics,
                "method_costs_once_per_run": {
                    "accounting_scope": "method_level_once",
                    "currency": args.currency,
                    "provider_usage": None,
                    "compute_cost_amount": None,
                    "actual_account_charge_amount": None,
                    "status": "per_run_usage_or_rate_card_unavailable",
                    "null_reasons": ["per_run_provider_usage_not_attached_to_completed_route", "matching_method_cost_rate_not_frozen"],
                },
                "time_money_decision_profiles": profile_gates,
                "time_money_decision_evaluated": False,
                "time_money_decision_status": "blocked_incomplete_inputs_and_shared_candidate_pool",
                "missing_inputs": blockers,
                "evidence": {
                    "state_path": str(state_path),
                    "state_sha256": state_sha,
                    "final_audit_path": str(audit_path),
                    "final_audit_sha256": audit_sha,
                    "preflight_path": str(preflight_path),
                    "preflight_sha256": preflight_sha,
                    "map_path": str(map_path) if map_path else None,
                    "map_sha256": map_sha,
                    "map_id": road_map.map_id,
                    "map_version": road_map.version,
                    "accepted_nodes_sha256": sha256_bytes(json.dumps(nodes, separators=(",", ":")).encode()),
                    "accepted_edges_sha256": sha256_bytes(json.dumps(edge_ids, separators=(",", ":")).encode()),
                    "source_manifest_sha256": source_manifest_sha,
                },
            }
            run_rows.append(run_row)
            counters["decision_status:" + run_row["time_money_decision_status"]] += 1
            for segment in economics["segments"]:
                segment_rows.append(
                    {
                        "task_id": task.get("task_id"),
                        "seed": task.get("seed"),
                        "source_run_dir": run_key,
                        "segment_id": segment["segment_id"],
                        "segment_type": segment["segment_type"],
                        "energy_model_version": profile.model_version,
                        "energy_profile_sha256": profile.fingerprint(),
                        "source_energy_estimate": next(row for row in phase_meta if row["segment_id"] == segment["segment_id"]),
                        "v5_segment_economics": segment,
                        "method_costs_ref": "run_economics_v5.method_costs_once_per_run",
                    }
                )

    if not run_rows:
        raise SystemExit("no_completed_route_sources_found")
    if sha256_file(manifest_path) != source_manifest_sha:
        raise SystemExit("batch_manifest_changed_during_recalculation")
    for run_dir, hashes in source_hashes.items():
        source = Path(run_dir)
        checks = {
            "state.json": source / "state.json",
            "final_audit.json": source / "final_audit.json",
        }
        for key in ("state.json", "final_audit.json"):
            if sha256_file(checks[key]) != hashes[key]:
                raise SystemExit(f"source_changed_during_recalculation:{run_dir}:{key}")
        preflight_path = Path(next(row["evidence"]["preflight_path"] for row in run_rows if row["source_run_dir"] == run_dir))
        if sha256_file(preflight_path) != hashes["preflight"]:
            raise SystemExit(f"source_changed_during_recalculation:{run_dir}:preflight")
        map_path_value = next(row["evidence"]["map_path"] for row in run_rows if row["source_run_dir"] == run_dir)
        if map_path_value and sha256_file(Path(map_path_value)) != hashes["map"]:
            raise SystemExit(f"source_changed_during_recalculation:{run_dir}:map")

    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / "source_manifest_snapshot.json", source_manifest)
    run_path = output_dir / "run_economics_v5.jsonl"
    segment_path = output_dir / "segment_economics_v5.jsonl"
    run_count = write_jsonl(run_path, run_rows)
    segment_count = write_jsonl(segment_path, segment_rows)
    manifest = {
        "schema_version": "r4_formal_batch_time_money_v5_segment_sidecar_v1",
        "status": "completed_segment_economics_route_choice_blocked",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "batch_root": str(batch_root),
        "source_manifest_path": str(manifest_path),
        "source_manifest_sha256": source_manifest_sha,
        "source_manifest_snapshot_sha256": sha256_file(output_dir / "source_manifest_snapshot.json"),
        "source_manifest_updated_at_utc": source_manifest.get("updated_at_utc"),
        "decision_model_version": DECISION_MODEL_VERSION,
        "decision_module_path": str(PROJECT_ROOT / "route_experiment" / "decision.py"),
        "decision_module_sha256": sha256_file(PROJECT_ROOT / "route_experiment" / "decision.py"),
        "energy_model_path": str(PROJECT_ROOT / "route_experiment" / "energy_model.py"),
        "energy_model_sha256": sha256_file(PROJECT_ROOT / "route_experiment" / "energy_model.py"),
        "generator_path": str(Path(__file__).resolve()),
        "generator_sha256": sha256_file(Path(__file__).resolve()),
        "energy_model": model_metadata(profile),
        "currency": args.currency,
        "electricity_price_per_kwh": args.electricity_price_per_kwh,
        "electricity_price_source": args.electricity_price_source,
        "fare_per_km": args.fare_per_km,
        "fare_basis": args.fare_basis,
        "route_complete_task_count_in_source_manifest": sum(1 for task in source_manifest.get("tasks", []) if task.get("status") == "route_complete"),
        "completed_source_run_count": run_count,
        "segment_row_count": segment_count,
        "source_hashes_reverified_after_recalculation": True,
        "source_results_modified": False,
        "route_algorithm_rerun": False,
        "new_model_calls": 0,
        "shared_candidate_pool_available": False,
        "weighted_rankings_emitted": False,
        "dispatch_or_sumo_rerun": False,
        "decision_status_counts": {key.removeprefix("decision_status:"): value for key, value in counters.items()},
        "outputs": {
            "run_economics_v5.jsonl": {"path": str(run_path), "rows": run_count, "sha256": sha256_file(run_path)},
            "segment_economics_v5.jsonl": {"path": str(segment_path), "rows": segment_count, "sha256": sha256_file(segment_path)},
        },
    }
    write_json(output_dir / "manifest.json", manifest)
    print(json.dumps({"output_dir": str(output_dir), "completed_source_runs": run_count, "segments": segment_count, "status": manifest["status"], "source_results_modified": False, "new_model_calls": 0}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
