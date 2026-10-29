#!/usr/bin/env python3
"""Offline, same-graph Dijkstra pairing for completed fixed-endpoint R4 runs.

Reads the formal parent manifest and its immutable completed-run artifacts,
recomputes local_dijkstra on the identical PackageConfig/map/endpoints, and
scores both paths with the shared route and EV2 evaluators. It makes no model
requests and never modifies source routes or experiment inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for item in (ROOT, ROOT / "scripts"):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import EvaluationConfig, PackageConfig, RouteCandidate, VehicleOrderTask
from route_experiment.routing import edge_travel_time_sec, run_traditional_route
from run_r4_codex_formal_checkpoint import load_map


FIXED_ENDPOINT_TYPES = {
    "exact_node",
    "feasible_exact",
    "boundary_exact",
    "detour_exact",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_under(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else root / path


def final_audit_path(batch_dir: Path, task: dict[str, Any]) -> Path:
    raw = task.get("final_audit_path")
    choices: list[Path] = []
    if raw:
        choices.append(resolve_under(batch_dir, str(raw)))
    if task.get("output_dir"):
        choices.append(Path(task["output_dir"]) / "final_audit.json")
    for path in choices:
        if path.is_file():
            return path
    raise FileNotFoundError(f"final_audit_not_found:{task.get('seed')}:{task.get('task_id')}:{choices}")


def endpoints_for(preflight: dict[str, Any]) -> tuple[str, str, str] | None:
    public = preflight.get("task_public") or {}
    task_type = str(public.get("task_type") or "")
    if task_type not in FIXED_ENDPOINT_TYPES:
        return None
    if public.get("pickup_text") or public.get("dropoff_text"):
        return None
    truth = preflight.get("evaluation_truth") or {}
    start = public.get("start_node_id")
    pickup = public.get("pickup_node_id") or truth.get("true_pickup_node_id")
    dropoff = public.get("dropoff_node_id") or truth.get("true_dropoff_node_id")
    if not all((start, pickup, dropoff)):
        return None

    # The generated prompt must expose these fixed endpoints while excluding
    # evaluator-only truth fields. This confirms the comparison isolates route
    # search rather than semantic endpoint resolution.
    route_search = preflight.get("route_search") or {}
    if route_search.get("evaluator_truth_in_prompt") is not False:
        raise ValueError(f"evaluator_truth_prompt_gate_failed:{public.get('task_id')}")
    messages = route_search.get("messages") or []
    if len(messages) < 2 or not isinstance(messages[1].get("content"), str):
        raise ValueError(f"preflight_route_prompt_missing:{public.get('task_id')}")
    payload = json.loads(messages[1]["content"])
    query = payload.get("q") or {}
    if (query.get("s"), query.get("p"), query.get("d")) != (start, pickup, dropoff):
        raise ValueError(f"prompt_fixed_endpoint_mismatch:{public.get('task_id')}")
    return str(start), str(pickup), str(dropoff)


def validate_path(
    nodes: list[str],
    edge_ids: list[str],
    edge_by_id: dict[str, Any],
    start: str,
    pickup: str,
    dropoff: str,
) -> dict[str, Any]:
    if not nodes or len(nodes) != len(edge_ids) + 1:
        raise ValueError("route_node_edge_length_mismatch")
    if nodes[0] != start or nodes[-1] != dropoff:
        raise ValueError("route_endpoints_mismatch")
    if pickup not in nodes[:-1]:
        raise ValueError("pickup_not_reached_before_dropoff")
    missing = [edge_id for edge_id in edge_ids if edge_id not in edge_by_id]
    if missing:
        raise ValueError("route_edge_missing:" + ",".join(missing[:5]))
    for index, edge_id in enumerate(edge_ids):
        edge = edge_by_id[edge_id]
        if edge.from_node != nodes[index] or edge.to_node != nodes[index + 1]:
            raise ValueError(f"route_edge_node_discontinuity:{index}:{edge_id}")
    return {
        "moves": len(edge_ids),
        "distance_m": sum(edge_by_id[item].length_m for item in edge_ids),
        "optimizer_objective_travel_time_sec": sum(
            edge_travel_time_sec(edge_by_id[item]) for item in edge_ids
        ),
        "repeated_nodes": len(nodes) != len(set(nodes)),
    }


def score_pair(
    road_map: Any,
    nodes: list[str],
    edge_ids: list[str],
    pickup: str,
    route_id: str,
) -> dict[str, Any]:
    profile = EvaluationConfig().energy_profile
    passenger_mass_kg = float(profile.passenger_load_kg)
    full_candidate = RouteCandidate(
        route_id=route_id,
        node_sequence=nodes,
        edge_sequence=edge_ids,
        route_reachability=True,
        payload_mass_kg=passenger_mass_kg,
    )
    full = evaluate_route_candidate(road_map, full_candidate, EvaluationConfig())
    pickup_index = nodes.index(pickup)
    deadhead_edges = edge_ids[:pickup_index]
    passenger_edges = edge_ids[pickup_index:]
    deadhead_energy = 0.0
    if deadhead_edges:
        deadhead = RouteCandidate(
            route_id=route_id + "-deadhead",
            node_sequence=nodes[: pickup_index + 1],
            edge_sequence=deadhead_edges,
            route_reachability=True,
            payload_mass_kg=0.0,
        )
        deadhead_energy = evaluate_route_candidate(road_map, deadhead, EvaluationConfig()).energy_total_kwh
    passenger_energy = 0.0
    if passenger_edges:
        passenger = RouteCandidate(
            route_id=route_id + "-passenger",
            node_sequence=nodes[pickup_index:],
            edge_sequence=passenger_edges,
            route_reachability=True,
            payload_mass_kg=passenger_mass_kg,
        )
        passenger_energy = evaluate_route_candidate(road_map, passenger, EvaluationConfig()).energy_total_kwh
    return {
        "ev2_time_sec": full.total_travel_time_sec,
        "ev2_energy_full_route_passenger_load_kwh": full.energy_total_kwh,
        "ev2_energy_deadhead_empty_kwh": deadhead_energy,
        "ev2_energy_passenger_leg_kwh": passenger_energy,
        "ev2_energy_segmented_total_kwh": deadhead_energy + passenger_energy,
        "ev2_energy_profile_id": profile.profile_id,
        "ev2_energy_profile_sha256": profile.fingerprint(),
    }


def pct_delta(model: float, baseline: float) -> float | None:
    return None if baseline == 0 else 100.0 * (model - baseline) / baseline


def violation_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return int(value)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [
        "moves",
        "distance_m",
        "optimizer_objective_travel_time_sec",
        "ev2_time_sec",
        "ev2_energy_segmented_total_kwh",
    ]
    output: dict[str, Any] = {
        "paired_count": len(records),
        "model_objective_time_better_count": 0,
        "model_objective_time_tie_count": 0,
        "model_objective_time_worse_count": 0,
        "metrics": {},
    }
    for record in records:
        delta = record["r4_over_baseline_percent"]["optimizer_objective_travel_time_sec"]
        if delta < -1e-9:
            output["model_objective_time_better_count"] += 1
        elif delta > 1e-9:
            output["model_objective_time_worse_count"] += 1
        else:
            output["model_objective_time_tie_count"] += 1
    for metric in metrics:
        values = [row["r4_over_baseline_percent"].get(metric) for row in records]
        values = [float(value) for value in values if value is not None and math.isfinite(value)]
        output["metrics"][metric] = {
            "median_percent_difference": statistics.median(values) if values else None,
            "mean_percent_difference": statistics.mean(values) if values else None,
        }
    return output


def audit(batch_dir: Path, project_root: Path, manifest_snapshot_path: Path) -> dict[str, Any]:
    manifest_path = batch_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_hash = sha256_bytes(manifest_bytes)
    if manifest_snapshot_path.exists():
        raise FileExistsError(f"refusing_to_overwrite_manifest_snapshot:{manifest_snapshot_path}")
    manifest_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_snapshot_path.write_bytes(manifest_bytes)
    tasks = manifest.get("tasks", [])
    status_counts = Counter(item.get("status") for item in tasks)
    records: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for task in tasks:
        if task.get("status") != "route_complete":
            continue
        task_id = task["task_id"]
        seed = int(task["seed"])
        preflight_path = Path(task["preflight_path"])
        preflight_bytes = preflight_path.read_bytes()
        preflight_hash = sha256_bytes(preflight_bytes)
        if task.get("preflight_sha256") and preflight_hash != task["preflight_sha256"]:
            raise ValueError(f"preflight_hash_mismatch:{seed}:{task_id}")
        preflight = json.loads(preflight_bytes)
        endpoints = endpoints_for(preflight)
        if endpoints is None:
            excluded.append({
                "seed": seed,
                "task_id": task_id,
                "task_type": (preflight.get("task_public") or {}).get("task_type"),
                "reason": "semantic_or_non_fixed_endpoint_condition_excluded",
                "preflight_sha256": preflight_hash,
            })
            continue
        start, pickup, dropoff = endpoints

        map_ref = preflight.get("map_ref") or {}
        map_path = resolve_under(project_root, str(map_ref["path"]))
        map_bytes = map_path.read_bytes()
        map_hash = sha256_bytes(map_bytes)
        if map_hash != map_ref.get("sha256"):
            raise ValueError(f"map_hash_mismatch:{seed}:{task_id}")
        map_audit = ((preflight.get("route_search") or {}).get("map_equivalence_audit") or {})
        if map_audit.get("passed") is not True:
            raise ValueError(f"r4_map_equivalence_gate_failed:{seed}:{task_id}")
        road_map = load_map(map_path)

        final_path = final_audit_path(batch_dir, task)
        final_hash = sha256_file(final_path)
        final = read_json(final_path)
        if final.get("status") != "completed_route":
            raise ValueError(f"completed_manifest_task_has_noncomplete_audit:{seed}:{task_id}")
        if final.get("source_preflight_sha256") != preflight_hash:
            raise ValueError(f"final_audit_preflight_hash_mismatch:{seed}:{task_id}")
        if final.get("pickup_reached") is not True or final.get("dropoff_reached") is not True:
            raise ValueError(f"final_audit_endpoint_not_reached:{seed}:{task_id}")
        if violation_count(final.get("invalid_or_stopped_actions")) != 0:
            raise ValueError(f"final_audit_invalid_action_count:{seed}:{task_id}")
        if violation_count(final.get("route_continuity_violations")) != 0:
            raise ValueError(f"final_audit_route_continuity_errors:{seed}:{task_id}")

        public = preflight["task_public"]
        vehicle_task = VehicleOrderTask(
            task_id=task_id,
            vehicle_id=str(public.get("vehicle_id") or "r4-audit"),
            order_id=str(public.get("order_id") or task_id),
            start_node_id=start,
            pickup_node_id=pickup,
            dropoff_node_id=dropoff,
            task_type=str(public.get("task_type") or "exact_node"),
        )
        blocked_edges = tuple(((preflight.get("route_search") or {}).get("blocked_edge_ids") or []))
        baseline_result = run_traditional_route(
            road_map,
            vehicle_task,
            "local_dijkstra",
            package_config=PackageConfig(),
            blocked_edge_ids=blocked_edges,
        )
        if baseline_result.status != "completed" or not baseline_result.routes:
            raise ValueError(f"same_package_dijkstra_failed:{seed}:{task_id}:{baseline_result.failure_reason}")
        baseline_route = baseline_result.routes[0]
        if baseline_route.validation_status != "valid":
            raise ValueError(f"dijkstra_route_validation_failed:{seed}:{task_id}")

        map_edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
        baseline_nodes = list(baseline_route.node_sequence)
        baseline_edges = list(baseline_route.edge_sequence)
        baseline_path_metrics = validate_path(
            baseline_nodes, baseline_edges, map_edge_by_id, start, pickup, dropoff
        )
        baseline_scored = score_pair(
            road_map, baseline_nodes, baseline_edges, pickup, f"dijkstra-{seed}-{task_id}"
        )

        r4_nodes = list(final.get("accepted_route_nodes") or [])
        r4_edges = list(final.get("accepted_route_edges") or [])
        r4_path_metrics = validate_path(r4_nodes, r4_edges, map_edge_by_id, start, pickup, dropoff)
        r4_scored = score_pair(road_map, r4_nodes, r4_edges, pickup, f"r4-{seed}-{task_id}")

        # Guard against mixing another energy or travel-time definition into
        # the pair: re-scored values must reconcile with the saved formal audit.
        for key, observed in (
            ("completed_route_distance_m", r4_path_metrics["distance_m"]),
            ("completed_route_duration_sec", r4_scored["ev2_time_sec"]),
            ("completed_route_energy_kwh", r4_scored["ev2_energy_full_route_passenger_load_kwh"]),
        ):
            saved = final.get(key)
            if saved is None or not math.isclose(float(saved), float(observed), rel_tol=1e-9, abs_tol=1e-8):
                raise ValueError(f"r4_metric_reconciliation_failed:{seed}:{task_id}:{key}:{saved}:{observed}")

        baseline_metrics = {**baseline_path_metrics, **baseline_scored}
        r4_metrics = {**r4_path_metrics, **r4_scored}
        deltas = {
            key: pct_delta(float(r4_metrics[key]), float(baseline_metrics[key]))
            for key in (
                "moves",
                "distance_m",
                "optimizer_objective_travel_time_sec",
                "ev2_time_sec",
                "ev2_energy_full_route_passenger_load_kwh",
                "ev2_energy_segmented_total_kwh",
            )
        }
        source_task = preflight.get("task_source") or {}
        source_path = resolve_under(project_root, str(source_task["path"]))
        source_hash = sha256_file(source_path)
        if source_hash != source_task.get("sha256"):
            raise ValueError(f"task_source_hash_mismatch:{seed}:{task_id}")
        records.append({
            "seed": seed,
            "sample_group": task.get("sample_group"),
            "task_id": task_id,
            "task_type": public.get("task_type"),
            "map_id": road_map.map_id,
            "map_sha256": map_hash,
            "task_source_sha256": source_hash,
            "preflight_sha256": preflight_hash,
            "final_audit_sha256": final_hash,
            "endpoints": {"start": start, "pickup": pickup, "dropoff": dropoff},
            "blocked_edge_ids_sha256": sha256_bytes(
                json.dumps(sorted(blocked_edges), separators=(",", ":")).encode()
            ),
            "package_config": PackageConfig().__dict__,
            "baseline_local_dijkstra": baseline_metrics,
            "r4_model": r4_metrics,
            "r4_over_baseline_percent": deltas,
            "model_route_valid": True,
            "baseline_route_valid": True,
            "model_requests": final.get("cumulative_requests_including_bootstrap_and_parent"),
            "input_tokens": final.get("cumulative_input_tokens_including_bootstrap_and_parent"),
            "output_tokens": final.get("cumulative_output_tokens_including_bootstrap_and_parent"),
        })

    prior_files = sorted(batch_dir.glob("resume_audit_*/r4_completed_fixed_endpoint_same_graph_dijkstra_audit_*.json"))
    previous_reconciliation: dict[str, Any] | None = None
    if prior_files:
        prior = read_json(prior_files[-1])
        current = {(row["seed"], row["task_id"]): row for row in records}
        matches = 0
        mismatches = []
        for old in prior.get("records", []):
            key = (int(old["seed"]), old["task_id"])
            new = current.get(key)
            if new is None:
                mismatches.append({"seed": key[0], "task_id": key[1], "reason": "prior_record_missing_from_current_parent"})
                continue
            metrics_ok = True
            for side, old_name, new_name in (
                ("baseline_local_dijkstra", "optimizer_objective_travel_time_sec", "optimizer_objective_travel_time_sec"),
                ("baseline_local_dijkstra", "distance_m", "distance_m"),
                ("baseline_local_dijkstra", "ev2_time_sec", "ev2_time_sec"),
                ("baseline_local_dijkstra", "ev2_energy_kwh", "ev2_energy_full_route_passenger_load_kwh"),
                ("r4_model", "optimizer_objective_travel_time_sec", "optimizer_objective_travel_time_sec"),
                ("r4_model", "distance_m", "distance_m"),
                ("r4_model", "ev2_time_sec", "ev2_time_sec"),
                ("r4_model", "ev2_energy_kwh", "ev2_energy_full_route_passenger_load_kwh"),
            ):
                left = old[side].get(old_name)
                right = new[side].get(new_name)
                if left is None or right is None or not math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-8):
                    metrics_ok = False
                    break
            if metrics_ok:
                matches += 1
            else:
                mismatches.append({"seed": key[0], "task_id": key[1], "reason": "saved_metric_changed_since_prior_snapshot"})
        previous_reconciliation = {
            "prior_audit_path": str(prior_files[-1]),
            "prior_audit_sha256": sha256_file(prior_files[-1]),
            "prior_record_count": len(prior.get("records", [])),
            "metric_exact_matches": matches,
            "mismatches": mismatches,
        }

    return {
        "schema_version": "r4_completed_fixed_endpoint_same_graph_dijkstra_audit_v2",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_requests_made_for_audit": 0,
        "original_inputs_or_routes_modified": False,
        "parent_manifest_path": str(manifest_path),
        "parent_manifest_sha256": manifest_hash,
        "parent_manifest_snapshot_path": str(manifest_snapshot_path),
        "parent_manifest_status_counts_at_snapshot": dict(status_counts),
        "parent_frozen_task_count": manifest.get("frozen_task_count"),
        "route_input_protocol": manifest.get("route_input_protocol"),
        "scope": (
            "Only parent-manifest completed tasks with fixed exact endpoints. "
            "Same frozen map and local PackageConfig graph are used for local_dijkstra and R4. "
            "Semantic endpoint tasks are excluded so endpoint understanding is not conflated with route-search quality."
        ),
        "route_objective": "travel_time_then_distance",
        "energy_reporting": (
            "Both full-route 75kg-reference energy (to reconcile the prior snapshot) and EV2 phase energy "
            "(empty deadhead to pickup plus passenger load from pickup to dropoff) are included."
        ),
        "package_config": PackageConfig().__dict__,
        "energy_profile": {
            "profile_id": EvaluationConfig().energy_profile.profile_id,
            "profile_sha256": EvaluationConfig().energy_profile.fingerprint(),
            "passenger_load_kg": EvaluationConfig().energy_profile.passenger_load_kg,
        },
        "paired_count": len(records),
        "excluded_completed_tasks": excluded,
        "previous_snapshot_reconciliation": previous_reconciliation,
        "summary": summarize(records),
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-snapshot", type=Path)
    args = parser.parse_args()
    snapshot_path = (
        args.manifest_snapshot.resolve()
        if args.manifest_snapshot
        else args.output.with_name(args.output.stem + "_parent_manifest_snapshot.json").resolve()
    )
    result = audit(args.batch_dir.resolve(), args.project_root.resolve(), snapshot_path)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({
        "audit": str(args.output.resolve()),
        "parent_manifest_snapshot": str(snapshot_path),
        "paired_count": result["paired_count"],
        "excluded_completed_tasks": len(result["excluded_completed_tasks"]),
        "summary": result["summary"],
        "parent_manifest_sha256": result["parent_manifest_sha256"],
        "model_requests_made_for_audit": 0,
    }, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
