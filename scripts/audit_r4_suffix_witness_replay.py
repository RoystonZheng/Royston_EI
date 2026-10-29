#!/usr/bin/env python3
"""Replay one saved R4 run against a bounded no-revisit suffix gate, offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.models import PackageConfig, VehicleOrderTask
from route_experiment.r4_compact_route import validate_r4_compact_route_proposal
from route_experiment.r4_suffix_witness import audit_next_edges
from route_experiment.routing import build_route_input_package
from scripts.run_r4_codex_formal_checkpoint import load_map


DIRECTIONS = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _target(node_id: str, direction: str, node_by_id: dict[str, Any]) -> str:
    node = node_by_id[node_id]
    dx, dy = DIRECTIONS[direction]
    return "node_{0}_{1}".format(node.x + dx, node.y + dy)


def audit(preflight_path: Path, final_audit_path: Path, output_path: Path) -> dict[str, Any]:
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    final_audit = json.loads(final_audit_path.read_text(encoding="utf-8"))
    public = preflight["task_public"]
    truth = preflight["evaluation_truth"]
    task = VehicleOrderTask(
        task_id=public["task_id"],
        vehicle_id=public["vehicle_id"],
        order_id=public["order_id"],
        start_node_id=public["start_node_id"],
        pickup_node_id=truth["true_pickup_node_id"],
        dropoff_node_id=truth["true_dropoff_node_id"],
        task_type=public["task_type"],
    )
    map_path = (ROOT / preflight["map_ref"]["path"]).resolve()
    if _sha256(map_path) != preflight["map_ref"].get("sha256"):
        raise RuntimeError("frozen_map_sha256_mismatch")
    road_map = load_map(map_path)
    package = build_route_input_package(
        road_map,
        task,
        PackageConfig(),
        blocked_edge_ids=preflight["route_search"].get("blocked_edge_ids", ()),
    )
    edge_by_id = {edge.edge_id: edge for edge in package.edges}
    node_by_id = {node.node_id: node for node in package.nodes}
    directions = final_audit["accepted_directions"]
    if final_audit.get("task_id") not in (None, task.task_id):
        raise RuntimeError("saved_audit_task_id_mismatch")
    current = task.start_node_id
    visited = {current}
    unsafe_action = None
    safe_candidates = []
    filtered_candidate_reasons = {}
    analyzed_actions = 0

    for action_number, proposed_direction in enumerate(directions, start=1):
        candidates = []
        filtered_candidate_reasons = {}
        remaining_action_budget = int(preflight["route_search"]["max_route_actions"]) - (
            action_number - 1
        )
        certification = audit_next_edges(
            package.edges,
            current,
            task.pickup_node_id,
            task.dropoff_node_id,
            visited_node_ids=visited,
            blocked_edge_ids=package.blocked_edge_ids,
            remaining_action_budget=remaining_action_budget,
        )
        for certified in certification.certified_edges:
            source = node_by_id[certified.edge.from_node]
            target = node_by_id[certified.edge.to_node]
            delta = (target.x - source.x, target.y - source.y)
            direction = next(
                name for name, vector in DIRECTIONS.items() if vector == delta
            )
            candidates.append(
                {
                    "direction": direction,
                    "target_node_id": certified.edge.to_node,
                    "witness": certified.suffix_witness,
                }
            )
        for edge_id, reason in certification.filtered_edge_reasons.items():
            edge = edge_by_id[edge_id]
            source, target = node_by_id[edge.from_node], node_by_id[edge.to_node]
            delta = (target.x - source.x, target.y - source.y)
            direction = next(
                name for name, vector in DIRECTIONS.items() if vector == delta
            )
            filtered_candidate_reasons[direction] = reason

        analyzed_actions += 1
        selected = next(
            (candidate for candidate in candidates if candidate["direction"] == proposed_direction),
            None,
        )
        if selected is None:
            unsafe_action = action_number
            safe_candidates = candidates
            break

        current = _target(current, proposed_direction, node_by_id)
        visited.add(current)

    result: dict[str, Any] = {
        "schema_version": "r4_suffix_witness_replay_v1",
        "model_calls": 0,
        "task_id": task.task_id,
        "seed": preflight["seed"],
        "map_id": road_map.map_id,
        "map_sha256": _sha256(map_path),
        "preflight_sha256": _sha256(preflight_path),
        "saved_final_audit_sha256": _sha256(final_audit_path),
        "package_node_count": len(package.nodes),
        "package_directed_edge_count": len(package.edges),
        "historical_accepted_actions": len(directions),
        "historical_model_requests": final_audit.get(
            "actual_model_requests_this_continuation"
        ),
        "historical_input_tokens": final_audit.get(
            "input_tokens_this_continuation"
        ),
        "historical_output_tokens": final_audit.get(
            "output_tokens_this_continuation"
        ),
        "historical_accepted_prefix_energy_kwh": final_audit.get(
            "accepted_prefix_energy_kwh"
        ),
        "full_route_energy_kwh": final_audit.get("completed_route_energy_kwh"),
        "historical_status": final_audit.get("status"),
        "historical_stop_status": final_audit.get("stop_status"),
        "first_action_without_certified_suffix": unsafe_action,
        "current_node_before_that_action": current,
        "historical_direction": directions[unsafe_action - 1] if unsafe_action else None,
        "certified_alternative_directions": [
            candidate["direction"] for candidate in safe_candidates
        ],
        "filtered_direction_reasons_before_that_action": filtered_candidate_reasons,
        "analyzed_historical_actions": analyzed_actions,
        "certified_replay": None,
    }

    if unsafe_action is not None and safe_candidates:
        chosen = safe_candidates[0]
        prefix_directions = directions[: unsafe_action - 1]
        replay_directions = prefix_directions + [chosen["direction"]]
        replay_nodes = [task.start_node_id]
        position = task.start_node_id
        for direction in replay_directions:
            position = _target(position, direction, node_by_id)
            replay_nodes.append(position)
        replay_nodes.extend(chosen["witness"].node_sequence[1:])
        replay_moves = []
        for source_id, target_id in zip(replay_nodes, replay_nodes[1:]):
            source, target = node_by_id[source_id], node_by_id[target_id]
            delta = (target.x - source.x, target.y - source.y)
            replay_moves.append(
                next(direction for direction, step in DIRECTIONS.items() if step == delta)
            )
        validation = validate_r4_compact_route_proposal(
            road_map,
            start_node_id=task.start_node_id,
            pickup_node_id=task.pickup_node_id,
            dropoff_node_id=task.dropoff_node_id,
            blocked_edge_ids=package.blocked_edge_ids,
            payload={
                "status": "completed",
                "move_sequence": "".join(replay_moves),
                "step_check_sequence": "1" * len(replay_moves),
                "failure_reason_code": "none",
            },
        )
        result["certified_replay"] = {
            "substituted_direction_at_first_unsafe_action": chosen["direction"],
            "witness_edges_after_substitution": len(chosen["witness"].edge_sequence),
            "total_route_steps": len(replay_moves),
            "fits_frozen_action_budget": len(replay_moves)
            <= int(preflight["route_search"]["max_route_actions"]),
            "pickup_step": replay_nodes.index(task.pickup_node_id),
            "dropoff_step": replay_nodes.index(task.dropoff_node_id),
            "unique_nodes": len(replay_nodes) == len(set(replay_nodes)),
            "final_node_id": replay_nodes[-1],
            "move_sequence": "".join(replay_moves),
            "move_sequence_sha256": hashlib.sha256(
                "".join(replay_moves).encode("ascii")
            ).hexdigest(),
            "validator_accepted": bool(validation.get("accepted")),
            "validator_status": validation.get("status"),
            "validator_violations": validation.get("violations", []),
            "counts_as_llm_route_result": False,
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--final-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.preflight, args.final_audit, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("certified_replay", {}).get("validator_accepted") else 2


if __name__ == "__main__":
    raise SystemExit(main())
