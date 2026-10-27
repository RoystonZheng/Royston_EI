#!/usr/bin/env python3
"""Offline-only preflight for R4's compact-state/progress-feedback prompt v3."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for import_path in (ROOT, SCRIPTS):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from route_experiment.models import PackageConfig, VehicleOrderTask
from route_experiment.r4_constraints import build_r4_route_output_constraint
from route_experiment.r4_stepwise import (
    _compact_map_from_package,
    audit_r4_stepwise_map_equivalence,
    build_r4_step_output_constraint,
    build_r4_stepwise_messages,
)
from route_experiment.routing import build_route_input_package
from run_r4_codex_formal_checkpoint import load_map


DEFAULT_STATE = ROOT / "outputs/route_layer/r4_codex_task9_stateless_run_20261026_v6/state.json"
DEFAULT_PREFLIGHT = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"
DEFAULT_SEED_ROOT = ROOT / "outputs/route_layer/traditional_experiment_v1/seeds"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_progress_feedback_v3_preflight_20261026_v1"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decode_visited_slots(payload: dict[str, Any]) -> list[str]:
    compact_map = payload["m"]
    state = payload["s"]
    encoded = state["v"]
    width = int(state["vl"])
    if state.get("vf") != "grid_slot_base36" or len(encoded) % width:
        raise ValueError("visited_slot_encoding_invalid")
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = []
    for offset in range(0, len(encoded), width):
        slot = 0
        for char in encoded[offset : offset + width]:
            slot = slot * 36 + chars.index(char)
        x = int(compact_map["x0"]) + slot % int(compact_map["w"])
        y = int(compact_map["y0"]) + slot // int(compact_map["w"])
        result.append(f"node_{x}_{y}")
    return result


def run_preflight(
    state_path: Path,
    preflight_path: Path,
    seed_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    state = read_json(state_path)
    source_preflight = read_json(preflight_path)
    task_id = state["task_id"]
    task_public = source_preflight["task_public"]
    if task_public["task_id"] != task_id:
        raise RuntimeError("source_task_id_mismatch")

    seed = 0
    seed_dir = seed_root / f"seed_{seed}"
    map_path = next(seed_dir.glob(f"map_*_seed_{seed}.json"))
    orders = read_json(next(seed_dir.glob(f"order_tasks_seed_{seed}.json")))
    raw_task = next(row for row in orders["tasks"] if row["task_id"] == task_id)
    road_map = load_map(map_path)
    task = VehicleOrderTask(
        task_id=task_id,
        vehicle_id=raw_task.get("vehicle_id", "r4-preflight"),
        order_id=raw_task.get("order_id", task_id),
        start_node_id=state["start_node_id"],
        pickup_node_id=state["pickup_node_id"],
        dropoff_node_id=state["dropoff_node_id"],
        pickup_text=raw_task.get("pickup_text"),
        dropoff_text=raw_task.get("dropoff_text"),
        task_type=raw_task.get("task_type", "exact_node"),
    )
    package = build_route_input_package(road_map, task, PackageConfig())

    source_payload = json.loads(source_preflight["route_search"]["messages"][1]["content"])
    if any(
        source_payload["q"][key] != state[value]
        for key, value in (("s", "start_node_id"), ("p", "pickup_node_id"), ("d", "dropoff_node_id"))
    ):
        raise RuntimeError("source_endpoint_mismatch")
    if package.map_id != state["map_id"] or package.start_node_id != state["start_node_id"]:
        raise RuntimeError("source_map_or_start_mismatch")

    accepted_nodes = list(state["accepted_nodes"])
    accepted_edges = list(state["accepted_edges"])
    if not accepted_nodes:
        raise RuntimeError("accepted_prefix_empty")
    current_node_id = accepted_nodes[-1]
    edge_by_id = {edge.edge_id: edge for edge in package.edges}
    if len(accepted_nodes) != len(accepted_edges) + 1 or accepted_nodes[0] != task.start_node_id:
        raise RuntimeError("accepted_prefix_shape_invalid")
    if len(accepted_nodes) != len(set(accepted_nodes)):
        raise RuntimeError("accepted_prefix_revisits_node")
    for index, edge_id in enumerate(accepted_edges):
        edge = edge_by_id.get(edge_id)
        if edge is None or (edge.from_node, edge.to_node) != (
            accepted_nodes[index], accepted_nodes[index + 1]
        ):
            raise RuntimeError("accepted_prefix_edge_mismatch")
        if edge.to_node == task.dropoff_node_id and task.pickup_node_id not in accepted_nodes[: index + 2]:
            raise RuntimeError("accepted_prefix_dropoff_before_pickup")
    compact_map = _compact_map_from_package(
        package, task.start_node_id, task.pickup_node_id, task.dropoff_node_id
    )
    map_audit = audit_r4_stepwise_map_equivalence(package, compact_map)
    if not map_audit.get("passed"):
        raise RuntimeError("compact_map_equivalence_failed")
    full_constraint = build_r4_route_output_constraint(
        package, task.pickup_node_id, task.dropoff_node_id, allow_failed=False
    )
    remaining_requests = max(
        1,
        int(state["authorized_new_request_limit"]) - int(state["new_requests_used"]),
    )
    remaining_actions = int(state["route_action_budget"]) - len(accepted_edges)
    remaining_budget = min(remaining_requests, remaining_actions)
    step_constraint = build_r4_step_output_constraint(
        package,
        full_constraint,
        current_node_id=current_node_id,
        pickup_seen=bool(state["pickup_seen"]),
        visited_node_ids=accepted_nodes,
        blocked_edge_ids=package.blocked_edge_ids,
        remaining_action_budget=remaining_budget,
        include_objective_cost_guidance=True,
    )
    progress_guidance = {
        "objective": "travel_time_then_distance",
        "current_lower_bound": step_constraint.remaining_objective_cost_lower_bound,
        "by_direction": {
            direction: step_constraint.objective_cost_lower_bound_by_direction[direction]
            for direction in ("E", "W", "N", "S")
            if direction in step_constraint.objective_cost_lower_bound_by_direction
        },
    }

    common = {
        "current_node_id": current_node_id,
        "visited_node_ids": accepted_nodes,
        "pickup_seen": bool(state["pickup_seen"]),
        "accepted_directions": list(state["accepted_directions"]),
        "remaining_action_budget": remaining_budget,
        "route_feature_context": state.get("route_feature_context"),
    }
    v2_messages = build_r4_stepwise_messages(
        package, task.start_node_id, task.pickup_node_id, task.dropoff_node_id,
        **common,
    )
    v3_messages = build_r4_stepwise_messages(
        package, task.start_node_id, task.pickup_node_id, task.dropoff_node_id,
        **common,
        compact_visited_state=True,
        progress_guidance=progress_guidance,
    )
    v2_payload = json.loads(v2_messages[1]["content"])
    v3_payload = json.loads(v3_messages[1]["content"])
    decoded_visited = _decode_visited_slots(v3_payload)
    expected_visited = [
        node_id for node_id in accepted_nodes
        if node_id not in {task.start_node_id, current_node_id}
    ]
    if decoded_visited != expected_visited:
        raise RuntimeError("compact_visited_state_round_trip_failed")
    if v2_payload["q"] != v3_payload["q"] or v2_payload["m"] != v3_payload["m"]:
        raise RuntimeError("v3_changed_map_or_task_payload")
    if set(step_constraint.edge_by_direction) != set(progress_guidance["by_direction"]):
        raise RuntimeError("progress_guidance_candidate_mismatch")
    if any(marker in json.dumps(v3_payload).lower() for marker in ("ground_truth", "true_pickup_node_id", "true_dropoff_node_id")):
        raise RuntimeError("evaluator_truth_leaked_to_v3_prompt")

    v2_bytes = len(v2_messages[1]["content"].encode("utf-8"))
    v3_bytes = len(v3_messages[1]["content"].encode("utf-8"))
    schema = step_constraint.tool_definition()
    result = {
        "schema_version": "r4_progress_feedback_v3_offline_preflight_v1",
        "status": "offline_preflight_passed_no_model_request",
        "model_requests_sent": 0,
        "task_id": task_id,
        "map_id": road_map.map_id,
        "map_file_sha256": sha256_file(map_path),
        "source_state_sha256": sha256_file(state_path),
        "source_preflight_sha256": sha256_file(preflight_path),
        "route_prefix": {
            "accepted_edges": len(accepted_edges),
            "current_node_id": current_node_id,
            "pickup_reached": bool(state["pickup_seen"]),
            "is_complete": False,
            "prefix_revalidated_against_frozen_map": True,
        },
        "endpoints": {
            "start_node_id": task.start_node_id,
            "pickup_node_id": task.pickup_node_id,
            "dropoff_node_id": task.dropoff_node_id,
            "same_as_source_preflight": True,
        },
        "map_equivalence_audit": map_audit,
        "route_quality_guidance": {
            "objective": "travel_time_then_distance",
            "current_optimistic_lower_bound": step_constraint.remaining_objective_cost_lower_bound,
            "candidate_direction_guidance": progress_guidance["by_direction"],
            "schema_allowed_directions_unchanged": list(step_constraint.edge_by_direction),
            "schema_sha256": step_constraint.schema_sha256,
            "schema_bytes": len(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()),
            "lower_bound_is_advisory_only": True,
            "validator_remains_authoritative": True,
        },
        "visited_state_compaction": {
            "encoding": v3_payload["s"]["vf"],
            "slot_width": v3_payload["s"]["vl"],
            "visited_middle_nodes": len(expected_visited),
            "v2_state_bytes": len(json.dumps(v2_payload["s"], ensure_ascii=False, separators=(",", ":")).encode()),
            "v3_state_bytes": len(json.dumps(v3_payload["s"], ensure_ascii=False, separators=(",", ":")).encode()),
            "round_trip_exact": decoded_visited == expected_visited,
        },
        "prompt_bytes": {
            "v2_user": v2_bytes,
            "v3_user": v3_bytes,
            "reduction_bytes": v2_bytes - v3_bytes,
            "reduction_percent": round((v2_bytes - v3_bytes) / v2_bytes * 100, 4) if v2_bytes else 0.0,
            "token_savings_measured": False,
        },
        "limitations": [
            "No model was called; no route-quality effect is measured.",
            "Objective-cost lower bounds relax overlap between the pickup and dropoff legs and are advisory, not an optimality proof.",
            "The v6 trace remains unchanged and its fourth invocation delivery/usage remains unknown.",
            "This candidate must be treated as a separately versioned protocol and pre-registered before a live comparison.",
        ],
    }
    write_json(output_dir / "preflight.json", result)
    write_json(output_dir / "prompt_v3_payload.json", v3_payload)
    (output_dir / "prompt_v3_system.txt").write_text(v3_messages[0]["content"] + "\n", encoding="utf-8")
    result["artifacts"] = {
        "preflight_json": "preflight.json",
        "prompt_v3_payload": "prompt_v3_payload.json",
        "prompt_v3_system": "prompt_v3_system.txt",
    }
    write_json(output_dir / "preflight.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--preflight", type=Path, default=DEFAULT_PREFLIGHT)
    parser.add_argument("--seed-root", type=Path, default=DEFAULT_SEED_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_preflight(args.state, args.preflight, args.seed_root, args.output_dir)
    print(json.dumps({
        "status": result["status"],
        "model_requests_sent": result["model_requests_sent"],
        "route_prefix": result["route_prefix"],
        "route_quality_guidance": result["route_quality_guidance"],
        "prompt_bytes": result["prompt_bytes"],
        "visited_state_compaction": result["visited_state_compaction"],
        "output_dir": str(args.output_dir),
    }, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
