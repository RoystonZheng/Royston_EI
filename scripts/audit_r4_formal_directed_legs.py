#!/usr/bin/env python3
"""Read-only per-leg directed-edge audit for the live R4 formal batch."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from run_r4_codex_pilot_continuation import _load_frozen_task
from route_experiment.r4_agent import replay_directed_edge_sequence


ROOT = Path(__file__).resolve().parents[1]
DIRECTION_BY_DELTA = {
    (1, 0): "E",
    (-1, 0): "W",
    (0, 1): "N",
    (0, -1): "S",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_state(task_row: dict[str, Any], state_path: Path) -> dict[str, Any]:
    state = _read_json(state_path)
    errors: list[str] = []
    preflight_path = Path(task_row["preflight_path"])
    expected_preflight_sha = task_row.get("preflight_sha256")
    if not preflight_path.is_absolute():
        preflight_path = ROOT / preflight_path
    if not preflight_path.is_file():
        return {
            "task_id": task_row["task_id"],
            "status": task_row.get("status"),
            "state_path": str(state_path),
            "passed": False,
            "errors": ["frozen_preflight_missing"],
            "accepted_edge_count": len(state.get("accepted_edges", [])),
            "legs": [],
        }
    actual_preflight_sha = _sha256(preflight_path)
    if expected_preflight_sha and actual_preflight_sha != expected_preflight_sha:
        errors.append("batch_preflight_sha256_mismatch")
    if state.get("source_preflight_sha256") != actual_preflight_sha:
        errors.append("state_preflight_sha256_mismatch")
    _, road_map, frozen_task, package = _load_frozen_task(preflight_path)
    if state.get("task_id") != frozen_task.task_id or task_row.get("task_id") != frozen_task.task_id:
        errors.append("frozen_task_id_mismatch")
    if state.get("map_id") != road_map.map_id or road_map.map_id != f"grid-100x100-seed-{task_row['seed']}":
        errors.append("frozen_map_id_mismatch")
    if state.get("route_input_protocol") != "r4-step-v3":
        errors.append("route_input_protocol_mismatch")

    nodes = list(state.get("accepted_nodes", []))
    edges = list(state.get("accepted_edges", []))
    directions = list(state.get("accepted_directions", []))
    start = state.get("start_node_id")
    pickup = state.get("pickup_node_id")
    dropoff = state.get("dropoff_node_id")
    if not nodes or nodes[0] != start:
        errors.append("accepted_prefix_start_mismatch")
    if len(nodes) != len(edges) + 1:
        errors.append("accepted_node_edge_length_mismatch")
    if len(directions) != len(edges):
        errors.append("accepted_direction_edge_length_mismatch")
    if len(set(nodes)) != len(nodes):
        errors.append("accepted_prefix_revisits_node")

    replay = replay_directed_edge_sequence(road_map, start, edges, nodes)
    if replay["move_violations"]:
        errors.append("directed_edge_replay_violation")
    if replay["validated_prefix_node_sequence"] != nodes:
        errors.append("replayed_nodes_do_not_match_accepted_prefix")

    edge_by_id = {edge.edge_id: edge for edge in package.edges}
    node_by_id = {node.node_id: node for node in package.nodes}
    blocked = set(package.blocked_edge_ids)
    for index, edge_id in enumerate(edges):
        edge = edge_by_id.get(edge_id)
        if edge is None:
            errors.append(f"step_{index + 1}:edge_missing_from_frozen_route_package")
            continue
        if edge_id in blocked:
            errors.append(f"step_{index + 1}:blocked_edge_used")
        if index + 1 >= len(nodes):
            continue
        if edge.from_node != nodes[index] or edge.to_node != nodes[index + 1]:
            errors.append(f"step_{index + 1}:edge_endpoints_do_not_match_node_sequence")
            continue
        source = node_by_id.get(nodes[index])
        target = node_by_id.get(nodes[index + 1])
        if source is None or target is None:
            errors.append(f"step_{index + 1}:node_missing_from_frozen_route_package")
            continue
        direction = DIRECTION_BY_DELTA.get((target.x - source.x, target.y - source.y))
        if direction != directions[index]:
            errors.append(f"step_{index + 1}:direction_label_mismatch")

    pickup_index = nodes.index(pickup) if pickup in nodes else None
    dropoff_index = nodes.index(dropoff) if dropoff in nodes else None
    if pickup_index is not None and dropoff_index is not None and pickup_index > dropoff_index:
        errors.append("dropoff_reached_before_pickup")
    if task_row.get("status") == "route_complete":
        if pickup_index is None or dropoff_index is None or dropoff_index != len(nodes) - 1:
            errors.append("completed_route_endpoint_or_stop_rule_mismatch")
        if state.get("status") != "route_complete":
            errors.append("completed_manifest_state_not_route_complete")

    legs: list[dict[str, Any]] = []
    if pickup_index is not None:
        pickup_leg = replay_directed_edge_sequence(
            road_map, start, edges[:pickup_index], nodes[: pickup_index + 1]
        )
        legs.append({
            "leg": "start_to_pickup",
            "start_node": start,
            "end_node": pickup,
            "edge_count": pickup_index,
            "passed": not pickup_leg["move_violations"],
            "violations": pickup_leg["move_violations"],
        })
        if dropoff_index is not None:
            dropoff_leg = replay_directed_edge_sequence(
                road_map,
                pickup,
                edges[pickup_index:dropoff_index],
                nodes[pickup_index : dropoff_index + 1],
            )
            legs.append({
                "leg": "pickup_to_dropoff",
                "start_node": pickup,
                "end_node": dropoff,
                "edge_count": dropoff_index - pickup_index,
                "passed": not dropoff_leg["move_violations"],
                "violations": dropoff_leg["move_violations"],
            })
        elif task_row.get("status") != "route_complete":
            partial_leg = replay_directed_edge_sequence(
                road_map,
                pickup,
                edges[pickup_index:],
                nodes[pickup_index:],
            )
            legs.append({
                "leg": "pickup_to_current_prefix",
                "start_node": pickup,
                "end_node": nodes[-1],
                "edge_count": len(edges) - pickup_index,
                "passed": not partial_leg["move_violations"],
                "violations": partial_leg["move_violations"],
            })
    else:
        prefix_leg = replay_directed_edge_sequence(road_map, start, edges, nodes)
        legs.append({
            "leg": "start_to_current_prefix",
            "start_node": start,
            "end_node": nodes[-1] if nodes else None,
            "edge_count": len(edges),
            "passed": not prefix_leg["move_violations"],
            "violations": prefix_leg["move_violations"],
        })

    return {
        "task_id": task_row["task_id"],
        "seed": task_row["seed"],
        "sample_group": task_row.get("sample_group"),
        "status": task_row.get("status"),
        "state_path": str(state_path),
        "map_id": road_map.map_id,
        "map_directed_edges": len(road_map.edges),
        "preflight_sha256": actual_preflight_sha,
        "frozen_task_id": frozen_task.task_id,
        "route_complete": task_row.get("status") == "route_complete",
        "pickup_reached": pickup_index is not None,
        "dropoff_reached": dropoff_index is not None,
        "accepted_edge_count": len(edges),
        "replayed_directed_edge_count": replay["move_count"],
        "legs": legs,
        "replay_violations": replay["move_violations"],
        "errors": errors,
        "passed": not errors,
    }


def audit_batch(manifest_path: Path) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    results = []
    skipped = []
    for task in manifest.get("tasks", []):
        output_dir = Path(task.get("output_dir") or "")
        state_path = output_dir / "state.json" if task.get("output_dir") else None
        if not state_path or not state_path.is_file():
            skipped.append({"task_id": task.get("task_id"), "status": task.get("status")})
            continue
        results.append(audit_state(task, state_path))

    status_counts = Counter(item["status"] for item in results)
    violations = [item for item in results if not item["passed"]]
    completed = [item for item in results if item["route_complete"]]
    leg_counts = Counter(leg["leg"] for item in results for leg in item["legs"])
    failed_legs = [
        {"task_id": item["task_id"], **leg}
        for item in results
        for leg in item["legs"]
        if not leg["passed"]
    ]
    return {
        "schema_version": "r4_formal_batch_per_segment_directed_edge_audit_v1",
        "status": "passed" if not violations and not failed_legs else "failed",
        "batch_manifest_path": str(manifest_path),
        "batch_manifest_sha256": _sha256(manifest_path),
        "batch_status_at_audit": manifest.get("status"),
        "batch_updated_at_utc": manifest.get("updated_at_utc"),
        "model_requests_used_at_audit": manifest.get("model_requests_used"),
        "routeable_task_denominator": manifest.get("routeable_task_count"),
        "task_status_counts_audited": dict(status_counts),
        "states_audited": len(results),
        "states_skipped_without_output": skipped,
        "completed_routes_audited": len(completed),
        "completed_routes_with_directed_edge_violation": sum(
            bool(item["replay_violations"]) for item in completed
        ),
        "accepted_edges_audited": sum(item["accepted_edge_count"] for item in results),
        "legs_audited_by_type": dict(leg_counts),
        "failed_legs": failed_legs,
        "violations": violations,
        "tasks": results,
        "note": (
            "Read-only replay against each task's frozen preflight map and blocked-edge set. "
            "A route is counted compliant only when every accepted segment exists in the directed graph, "
            "matches its node sequence and direction labels, obeys pickup/dropoff order, and has no revisits."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_batch(args.manifest.resolve())
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        key: report[key]
        for key in (
            "status", "batch_status_at_audit", "model_requests_used_at_audit",
            "routeable_task_denominator", "task_status_counts_audited", "states_audited",
            "completed_routes_audited", "accepted_edges_audited", "legs_audited_by_type",
            "completed_routes_with_directed_edge_violation", "failed_legs", "violations",
        )
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
