#!/usr/bin/env python3
"""Build a zero-model-call, auditable shared candidate pool for frozen R4 tasks.

The pool combines fastest Dijkstra references, local graph references, and any
already completed historical R4 route. It never mutates the frozen batch and
does not claim that historical R4 routes saw these references at planning time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from route_experiment.costs import evaluate_route_candidate
from route_experiment.decision import DECISION_PROFILES, score_route_candidates
from route_experiment.models import (
    Edge,
    EvaluationConfig,
    Node,
    RoadMap,
    RoadsideFeature,
    RouteCandidate,
    VehicleOrderTask,
)
from route_experiment.r4_suffix_witness import build_waypoint_suffix_witness
from route_experiment.routing import run_traditional_route


DEFAULT_PARENT = Path(
    "[LOCAL_HOME]/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/"
    "r4_codex_formal_batch_20261027_v2/manifest.json"
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def load_map(path: Path) -> RoadMap:
    value = json.loads(path.read_text(encoding="utf-8"))
    return RoadMap(
        map_id=value["map_id"],
        version=value["version"],
        size=value["size"],
        nodes=[Node(**item) for item in value["nodes"]],
        edges=[Edge(**item) for item in value["edges"]],
        boundary=value["boundary"],
        road_state_version=value.get("road_state_version", "road_state_v1"),
        roadside_features=[RoadsideFeature(**item) for item in value.get("roadside_features", [])],
    )


def edge_sequence_valid(
    route: RouteCandidate,
    road_map: RoadMap,
    start: str,
    pickup: str,
    dropoff: str,
    blocked: Iterable[str],
) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
    blocked_set = set(blocked)
    edges = []
    for edge_id in route.edge_sequence:
        edge = edge_by_id.get(edge_id)
        if edge is None:
            errors.append("edge_missing:" + edge_id)
        elif edge_id in blocked_set:
            errors.append("edge_blocked:" + edge_id)
        else:
            edges.append(edge)
    if len(edges) != len(route.edge_sequence):
        return False, errors
    nodes = list(route.node_sequence)
    if not nodes:
        nodes = [edges[0].from_node] + [edge.to_node for edge in edges] if edges else [start]
    if not edges:
        errors.append("empty_route")
        return False, errors
    if len(nodes) != len(edges) + 1:
        errors.append("node_edge_count_mismatch")
        return False, errors
    for index, edge in enumerate(edges):
        if edge.from_node != nodes[index] or edge.to_node != nodes[index + 1]:
            errors.append("edge_node_discontinuity:" + str(index))
            break
    if nodes[0] != start:
        errors.append("wrong_start")
    if pickup not in nodes or dropoff not in nodes:
        errors.append("endpoint_missing")
    elif nodes.index(pickup) >= nodes.index(dropoff):
        errors.append("pickup_dropoff_order")
    if len(nodes) != len(set(nodes)):
        errors.append("repeated_node")
    return not errors, errors


def _merge_candidate(by_route_hash: Dict[str, Dict[str, Any]], row: Dict[str, Any]) -> None:
    route_hash = row["edge_sequence_sha256"]
    prior = by_route_hash.get(route_hash)
    if prior is None:
        by_route_hash[route_hash] = row
        return
    prior["method_sources"] = sorted(set(prior["method_sources"]) | set(row["method_sources"]))
    prior["candidate_id"] = "+".join(prior["method_sources"])


def candidate_row(
    route: RouteCandidate,
    method: str,
    road_map: RoadMap,
    task_id: str,
    start: str,
    pickup: str,
    dropoff: str,
    blocked: List[str],
    evaluation: EvaluationConfig,
) -> Dict[str, Any]:
    feasible, errors = edge_sequence_valid(route, road_map, start, pickup, dropoff, blocked)
    metrics = None
    if feasible:
        try:
            metrics = evaluate_route_candidate(road_map, route, evaluation)
        except Exception as exc:  # Keep the candidate visible with an explicit blocker.
            feasible = False
            errors.append("common_evaluator_error:" + type(exc).__name__ + ":" + str(exc))
    edge_bytes = json.dumps(route.edge_sequence, separators=(",", ":")).encode()
    row: Dict[str, Any] = {
        "candidate_id": method,
        "method_sources": [method],
        "task_id": task_id,
        "feasible": feasible,
        "validation_errors": errors,
        "edge_sequence": list(route.edge_sequence),
        "node_sequence": list(route.node_sequence),
        "edge_sequence_sha256": sha256_bytes(edge_bytes),
        "route_distance_m": route.route_distance_m,
        "source_route_duration_sec": route.route_duration_sec,
        "travel_time_sec": metrics.total_travel_time_sec if metrics else None,
        "energy_kwh": metrics.energy_total_kwh if metrics else None,
        "energy_model_version": metrics.energy_model_version if metrics else None,
        "energy_profile_sha256": metrics.energy_profile_sha256 if metrics else None,
        "money_cost": None,
        "money_cost_status": "blocked_missing_frozen_tariff_fare_and_method_cost_rates",
    }
    return row


def load_historical_route(task: Dict[str, Any]) -> Tuple[List[str], List[str], str]:
    output_dir = task.get("output_dir")
    if not output_dir:
        return [], [], "historical_route_not_available"
    out = Path(output_dir)
    audit_path = out / "final_audit.json"
    if not audit_path.is_file():
        return [], [], "historical_route_not_available"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    edges = list(audit.get("accepted_route_edges") or [])
    nodes = list(audit.get("accepted_route_nodes") or [])
    if audit.get("status") != "completed_route" or not edges:
        return [], [], "historical_route_not_complete"
    return edges, nodes, "historical_r4_complete"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-manifest", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    parent_path = args.parent_manifest.expanduser().resolve()
    output = args.output_dir.expanduser()
    if not output.is_absolute():
        output = ROOT / output
    output = output.resolve()
    if output.exists():
        raise SystemExit("refusing_to_overwrite:" + str(output))
    parent_bytes = parent_path.read_bytes()
    parent = json.loads(parent_bytes)
    evaluation = EvaluationConfig()
    counters: Counter = Counter()
    pool_rows: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []
    pool_methods = ("full_map_dijkstra", "local_dijkstra", "local_a_star")

    for item in parent.get("tasks", []):
        task_id = str(item["task_id"])
        preflight_path = Path(item["preflight_path"]).expanduser().resolve()
        preflight_bytes = preflight_path.read_bytes()
        preflight = json.loads(preflight_bytes)
        map_ref = preflight["map_ref"]
        map_path = Path(map_ref["path"])
        if not map_path.is_absolute():
            map_path = ROOT / map_path
        map_path = map_path.resolve()
        if sha256_file(map_path) != map_ref["sha256"]:
            raise SystemExit("map_hash_mismatch:" + task_id)
        road_map = load_map(map_path)
        messages = preflight["route_search"].get("messages", [])
        user_message = next((entry for entry in messages if entry.get("role") == "user"), None)
        if not user_message:
            summary_rows.append({"task_id": task_id, "status": "blocked_no_frozen_route_prompt"})
            counters["blocked_no_frozen_route_prompt"] += 1
            continue
        prompt_payload = json.loads(user_message["content"])
        q = prompt_payload.get("q", {})
        start, pickup, dropoff = q.get("s"), q.get("p"), q.get("d")
        if not all((start, pickup, dropoff)):
            summary_rows.append({"task_id": task_id, "status": "blocked_endpoint_not_frozen"})
            counters["blocked_endpoint_not_frozen"] += 1
            continue
        blocked = list(preflight["route_search"].get("blocked_edge_ids") or [])
        public = preflight.get("task_public", {})
        task = VehicleOrderTask(
            task_id=task_id,
            vehicle_id=str(public.get("vehicle_id") or "vehicle-" + task_id),
            order_id=str(public.get("order_id") or task_id),
            start_node_id=str(start),
            pickup_node_id=str(pickup),
            dropoff_node_id=str(dropoff),
            pickup_text=public.get("pickup_text"),
            dropoff_text=public.get("dropoff_text"),
            task_type="exact_node",
        )
        candidates: Dict[str, Dict[str, Any]] = {}
        method_status: Dict[str, str] = {}
        for method in pool_methods:
            result = run_traditional_route(road_map, task, method, blocked_edge_ids=blocked)
            method_status[method] = result.status
            if result.status != "completed" or not result.routes:
                counters["traditional_candidate_failed"] += 1
                continue
            route = result.routes[0]
            row = candidate_row(route, method, road_map, task_id, str(start), str(pickup), str(dropoff), blocked, evaluation)
            row["compute_time_ms"] = result.metadata.get("compute_time_ms")
            _merge_candidate(candidates, row)
        historical_edges, historical_nodes, historical_status = load_historical_route(item)
        if historical_edges:
            historical = RouteCandidate(
                route_id="historical-r4-" + task_id,
                edge_sequence=historical_edges,
                node_sequence=historical_nodes,
                route_source="historical_r4_complete",
                route_reachability=True,
                validation_status="valid",
            )
            row = candidate_row(historical, "historical_r4_complete", road_map, task_id, str(start), str(pickup), str(dropoff), blocked, evaluation)
            _merge_candidate(candidates, row)

        # The independent two-leg baselines can revisit a node when their
        # start->pickup and pickup->dropoff paths overlap. Add a single simple
        # path witness across both waypoints so every task has a legal shared
        # fallback before any future method starts. This search is offline and
        # makes no model calls.
        witness = build_waypoint_suffix_witness(
            road_map.edges,
            str(start),
            str(pickup),
            str(dropoff),
            blocked_edge_ids=blocked,
            max_first_leg_attempts=max(64, len(road_map.nodes) * 2),
        )
        witness_status = "joint_simple_path_witness_not_found"
        if witness is not None:
            edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
            witness_route = RouteCandidate(
                route_id="joint-simple-witness-" + task_id,
                edge_sequence=list(witness.edge_sequence),
                node_sequence=list(witness.node_sequence),
                route_source="joint_simple_path_witness",
                route_reachability=True,
                route_distance_m=sum(edge_by_id[edge_id].length_m for edge_id in witness.edge_sequence),
                validation_status="valid",
            )
            witness_row = candidate_row(
                witness_route,
                "joint_simple_path_witness",
                road_map,
                task_id,
                str(start),
                str(pickup),
                str(dropoff),
                blocked,
                evaluation,
            )
            _merge_candidate(candidates, witness_row)
            witness_status = "completed" if witness_row["feasible"] else "rejected:" + ",".join(witness_row["validation_errors"])
        rows = list(candidates.values())
        rows.sort(key=lambda row: (row["candidate_id"], row["edge_sequence_sha256"]))
        for row in rows:
            row["route_id"] = row["candidate_id"] + ":" + row["edge_sequence_sha256"][:12]
            pool_rows.append(row)
        profile_status = {}
        for profile_name, profile in DECISION_PROFILES.items():
            score_input = [
                {
                    "candidate_id": row["route_id"],
                    "feasible": row["feasible"],
                    "travel_time_sec": row["travel_time_sec"],
                    "money_cost": row["money_cost"],
                }
                for row in rows
            ]
            scored = score_route_candidates(score_input, profile, currency="CNY")
            profile_status[profile_name] = {
                "status": scored["status"],
                "candidate_count": scored["candidate_count"],
                "incomplete_candidates": scored["incomplete_candidates"],
                "selected_candidate_id": scored["selected_candidate_id"],
            }
        # Invalid outputs are retained for audit, but do not invalidate an
        # otherwise complete pool when the certified fallback is present.
        pool_ready = witness_status == "completed" and all(
            method_status.get(method) == "completed" for method in pool_methods
        )
        status = "pool_ready_with_rejected_candidates_money_decision_blocked" if pool_ready and any(not row["feasible"] for row in rows) else (
            "pool_ready_money_decision_blocked" if pool_ready else "pool_partial_or_candidate_validation_error"
        )
        summary_rows.append({
            "task_id": task_id,
            "status": status,
            "seed": item.get("seed"),
            "sample_group": item.get("sample_group"),
            "source_task_status": item.get("status"),
            "source_preflight_sha256": sha256_bytes(preflight_bytes),
            "source_map_sha256": map_ref["sha256"],
            "start_node": start,
            "pickup_node": pickup,
            "dropoff_node": dropoff,
            "blocked_edge_count": len(blocked),
            "traditional_method_status": method_status,
            "historical_route_status": historical_status,
            "joint_simple_path_witness_status": witness_status,
            "candidate_count": len(rows),
            "feasible_candidate_count": sum(row["feasible"] for row in rows),
            "rejected_candidate_count": sum(not row["feasible"] for row in rows),
            "time_money_profiles": profile_status,
            "model_calls": 0,
        })
        counters[status] += 1

    output.mkdir(parents=True)
    pool_path = output / "candidate_pools.jsonl"
    with pool_path.open("w", encoding="utf-8") as handle:
        for row in pool_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path = output / "task_summary.jsonl"
    with summary_path.open("w", encoding="utf-8") as handle:
        for row in summary_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "schema_version": "r4_shared_candidate_pool_v2",
        "status": "completed_offline_pool_build",
        "created_at_local": __import__("datetime").datetime.now().astimezone().isoformat(),
        "model_calls": 0,
        "source_parent_manifest": str(parent_path),
        "source_parent_manifest_sha256": sha256_bytes(parent_bytes),
        "source_task_count": len(parent.get("tasks", [])),
        "task_summary_count": len(summary_rows),
        "candidate_row_count": len(pool_rows),
        "candidate_methods": list(pool_methods) + ["historical_r4_complete", "joint_simple_path_witness"],
        "energy_model_version": evaluation.energy_model_version,
        "currency": "CNY",
        "money_decision_status": "blocked_missing_frozen_tariff_fare_and_per_method_cost_rate; money_cost kept null, both v5 profiles preserve the block",
        "candidate_pools_path": pool_path.name,
        "candidate_pools_sha256": sha256_file(pool_path),
        "task_summary_path": summary_path.name,
        "task_summary_sha256": sha256_file(summary_path),
        "status_counts": dict(sorted(counters.items())),
        "limitations": [
            "Traditional route candidates are generated offline from the same frozen directed grid map, task endpoints, and blocked edges.",
            "Historical R4 routes did not receive these candidate paths before planning; they are included only as post-hoc candidates and are not evidence that the original run followed the new protocol.",
            "The pool contains available algorithm routes, not every mathematically possible simple path.",
            "A joint simple-path witness is included for every task when found; algorithm outputs rejected by the shared legality validator remain in the pool with feasible=false and explicit rejection reasons.",
            "Travel time and EV2 energy are common evaluator estimates over frozen edge conditions, not dynamic SUMO execution results.",
            "Monetary decision is blocked because frozen electricity tariff, fare basis, and complete per-method cost rates are not attached; no zero values are substituted.",
            "Only tasks present in the frozen formal R4 manifest are included; unavailable or incomplete model routes remain explicitly absent.",
        ],
    }
    write_json(output / "manifest.json", manifest)
    print(json.dumps({k: manifest[k] for k in ["status", "source_task_count", "task_summary_count", "candidate_row_count", "status_counts", "candidate_pools_sha256", "task_summary_sha256"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
