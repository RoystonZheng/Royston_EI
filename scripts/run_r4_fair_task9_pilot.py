#!/usr/bin/env python3
"""Run a same-endpoint traditional / pure-LLM / R4 route pilot.

The pilot fixes one frozen task and gives the two Codex methods the same map,
route endpoints, task intent, time-first objective, pickup order, and no-revisit
constraint. It is a controlled pilot, not a multi-seed result.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import EvaluationConfig, RouteCandidate
from route_experiment.r4_compact_route import validate_r4_compact_route_proposal
from route_experiment.routing import edge_travel_time_sec
from run_r4_codex_formal_checkpoint import INTENT_RESULT, prepare, sha256
from run_r4_codex_segment_task9 import _source_feature_context, run as run_segment


MODEL = "gpt-5.6-luna"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_fair_task9_pilot_20261027_v2"
MAX_ROUTE_EDGES = 512


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _shortest_path(
    item: Dict[str, Any], source: str, target: str,
    forbidden_nodes: Iterable[str] = (),
) -> Optional[Tuple[List[str], List[str]]]:
    """Lexicographic Dijkstra: simulated travel time, then distance."""
    forbidden = set(forbidden_nodes) - {source}
    blocked = set(item["package"].blocked_edge_ids)
    adjacency: Dict[str, List[Any]] = {}
    for edge in item["package"].edges:
        if edge.edge_id in blocked or edge.to_node in forbidden:
            continue
        adjacency.setdefault(edge.from_node, []).append(edge)
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda edge: edge.edge_id)

    best: Dict[str, Tuple[float, float]] = {source: (0.0, 0.0)}
    previous: Dict[str, Tuple[str, str]] = {}
    queue: List[Tuple[float, float, str]] = [(0.0, 0.0, source)]
    while queue:
        time_cost, distance, node_id = heapq.heappop(queue)
        if (time_cost, distance) != best.get(node_id):
            continue
        if node_id == target:
            break
        for edge in adjacency.get(node_id, []):
            candidate = (
                time_cost + edge_travel_time_sec(edge),
                distance + edge.length_m,
            )
            if candidate < best.get(edge.to_node, (float("inf"), float("inf"))):
                best[edge.to_node] = candidate
                previous[edge.to_node] = (node_id, edge.edge_id)
                heapq.heappush(queue, (*candidate, edge.to_node))
    if target not in best:
        return None
    nodes = [target]
    edges = []
    current = target
    while current != source:
        parent, edge_id = previous[current]
        edges.append(edge_id)
        nodes.append(parent)
        current = parent
    return list(reversed(nodes)), list(reversed(edges))


def _traditional_baseline(item: Dict[str, Any]) -> Dict[str, Any]:
    task = item["task"]
    first = _shortest_path(item, task.start_node_id, task.pickup_node_id)
    if first is None:
        raise RuntimeError("traditional_start_to_pickup_unreachable")
    first_nodes, first_edges = first
    # Reserve the first-leg interior vertices so the concatenated route obeys
    # the same no-revisit rule as both model protocols.
    second = _shortest_path(
        item, task.pickup_node_id, task.dropoff_node_id,
        forbidden_nodes=first_nodes[:-1],
    )
    if second is None:
        raise RuntimeError("traditional_no_revisit_second_leg_unreachable")
    second_nodes, second_edges = second
    nodes = first_nodes + second_nodes[1:]
    edges = first_edges + second_edges
    node_by_id = {node.node_id: node for node in item["road_map"].nodes}
    delta_to_direction = {(1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S"}
    directions = "".join(
        delta_to_direction[(
            node_by_id[nodes[index + 1]].x - node_by_id[nodes[index]].x,
            node_by_id[nodes[index + 1]].y - node_by_id[nodes[index]].y,
        )]
        for index in range(len(nodes) - 1)
    )
    audit = validate_r4_compact_route_proposal(
        item["road_map"],
        start_node_id=task.start_node_id,
        pickup_node_id=task.pickup_node_id,
        dropoff_node_id=task.dropoff_node_id,
        payload={
            "status": "completed",
            "move_sequence": directions,
            "step_check_sequence": "1" * len(directions),
            "failure_reason_code": "none",
        },
    )
    if not audit.get("accepted"):
        raise RuntimeError("traditional_common_validator_rejected:" + json.dumps(audit.get("violations", [])))
    candidate = RouteCandidate(
        route_id="traditional_time_distance_no_revisit_task9",
        node_sequence=nodes,
        edge_sequence=edges,
        route_source="traditional_two_leg_dijkstra_no_revisit",
        route_reachability=True,
        validation_status="valid",
    )
    evaluated = evaluate_route_candidate(item["road_map"], candidate, EvaluationConfig())
    return {
        "method": "traditional_two_leg_dijkstra_no_revisit",
        "status": "completed",
        "validator": audit,
        "route": {"node_sequence": nodes, "edge_sequence": edges, "move_sequence": directions},
        "metrics": {
            "edge_count": len(edges),
            "distance_m": evaluated.route_distance_m,
            "travel_time_sec": evaluated.total_travel_time_sec,
            "energy_kwh": evaluated.energy_total_kwh,
            "energy_model_version": evaluated.energy_model_version,
            "simulation_estimate": True,
        },
        "optimality_claim": "none; first-leg shortest path followed by a shortest second leg avoiding first-leg interior vertices",
    }


def _run_pure_llm(item: Dict[str, Any], feature_context: Dict[str, Any], output: Path) -> Dict[str, Any]:
    from route_experiment.r4_compact_route import build_r4_compact_route_messages, make_r4_compact_route_constraint

    task = item["task"]
    messages = build_r4_compact_route_messages(
        item["package"], task.start_node_id, task.pickup_node_id, task.dropoff_node_id,
        route_intent=item["route_intent"], route_feature_context=feature_context,
    )
    prompt = CodexCliRouteStepClient._render_prompt(messages).encode("utf-8")
    constraint = make_r4_compact_route_constraint(MAX_ROUTE_EDGES)
    if b"true_pickup_node_id" in prompt or b"true_dropoff_node_id" in prompt or b"ground_truth" in prompt:
        raise RuntimeError("evaluator_truth_leaked_to_codex_route_prompt")
    write_json(output / "request.json", {
        "messages": messages,
        "prompt_sha256": sha256(prompt),
        "schema": constraint.schema,
        "schema_sha256": constraint.schema_sha256,
        "prompt_bytes": len(prompt),
    })
    client = CodexCliRouteStepClient(
        model=MODEL,
        trace_dir=output / "traces",
        reasoning_effort="low",
        timeout_sec=300,
        route_constraint=constraint,
        persist_session=False,
        working_directory=output / "empty_workspace",
        trace_stem="pure_llm_route",
    )
    attempt: Dict[str, Any] = {
        "request_number": 1,
        "status": "request_in_progress",
        "delivery_status": "unknown_until_receipt",
        "model": MODEL,
        "task_id": task.task_id,
        "map_id": item["road_map"].map_id,
        "prompt_sha256": sha256(prompt),
        "schema_sha256": constraint.schema_sha256,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "retry_count": 0,
    }
    write_json(output / "attempt_001.json", attempt)
    write_json(output.parent / "state.json", {
        "status": "pure_llm_request_in_progress",
        "model_requests_started": 1,
        "unresolved_cli_invocation": {
            "method": "pure_llm_codex",
            "request_number": 1,
            "status": "delivery_and_usage_unknown_until_receipt",
            "prompt_sha256": sha256(prompt),
        },
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    try:
        response = client.chat(messages)
        payload = json.loads(response.content)
        audit = validate_r4_compact_route_proposal(
            item["road_map"],
            start_node_id=task.start_node_id,
            pickup_node_id=task.pickup_node_id,
            dropoff_node_id=task.dropoff_node_id,
            payload=payload,
        )
        metrics = None
        if audit.get("accepted"):
            candidate = RouteCandidate(
                route_id="pure_llm_codex_task9",
                node_sequence=list(audit["route_node_sequence"]),
                edge_sequence=list(audit["route_edge_sequence"]),
                route_source="pure_llm_codex_compact_route",
                route_reachability=True,
                validation_status="valid",
            )
            evaluated = evaluate_route_candidate(item["road_map"], candidate, EvaluationConfig())
            metrics = {
                "edge_count": len(audit["route_edge_sequence"]),
                "distance_m": evaluated.route_distance_m,
                "travel_time_sec": evaluated.total_travel_time_sec,
                "energy_kwh": evaluated.energy_total_kwh,
                "energy_model_version": evaluated.energy_model_version,
                "simulation_estimate": True,
            }
        attempt.update({
            "status": "response_received",
            "delivery_status": "response_received",
            "response": payload,
            "validator": audit,
            "metrics": metrics,
            "usage": {
                "input_tokens": response.input_tokens,
                "cached_input_tokens": response.cached_input_tokens,
                "output_tokens": response.output_tokens,
                "latency_ms": response.latency_ms,
                "account_billing": "not_reported_by_codex_cli",
            },
            "provider_audit": response.provider_audit,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as error:
        attempt.update({
            "status": "request_error_no_retry",
            "delivery_status": "unknown_or_error",
            "error_type": type(error).__name__,
            "error": CodexCliRouteStepClient._redact_diagnostics(str(error))[:500],
            "provider_audit": dict(client.last_record),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        })
    write_json(output / "attempt_001.json", attempt)
    return attempt


def run(output_dir: Path, authorization_note: str, execute: bool) -> Dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError("output_directory_exists_no_replay_or_retry")
    item = prepare()
    feature_context = _source_feature_context()
    task = item["task"]
    baseline = _traditional_baseline(item)
    root_map = item["map_path"].read_bytes()
    root_task = item["task_path"].read_bytes()
    intent_bytes = INTENT_RESULT.read_bytes()
    manifest = {
        "schema_version": "r4_fair_task9_same_endpoint_pilot_v1",
        "experiment_id": "r4_fair_task9_pilot_20261027_v2",
        "status": "preflight_complete_ready_for_codex" if execute else "preflight_complete_no_model_requests",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "authorization_note": authorization_note if execute else None,
        "formal_result": False,
        "study_scope": "one frozen task, route search only; endpoint parsing is held fixed and not evaluated",
        "map_id": item["road_map"].map_id,
        "task_id": task.task_id,
        "start_node_id": task.start_node_id,
        "pickup_node_id": task.pickup_node_id,
        "dropoff_node_id": task.dropoff_node_id,
        "map_sha256": sha256(root_map),
        "order_tasks_sha256": sha256(root_task),
        "qwen_intent_sha256": sha256(intent_bytes) if intent_bytes else None,
        "endpoint_source": "the same frozen Qwen RoadFeatureInputProtocol v1 resolution supplied to both Codex methods; exact endpoint ids are fixed for route-search comparison",
        "shared_constraints": {
            "directed_edges_only": True,
            "blocked_edges_forbidden": True,
            "pickup_before_dropoff": True,
            "stop_at_dropoff": True,
            "no_revisit": True,
            "objective": "total simulated travel time, then distance",
            "validator": "validate_r4_compact_route_proposal",
            "evaluation": "shared EV longitudinal v2 EvaluationConfig",
        },
        "methods": {
            "traditional": "two-leg lexicographic Dijkstra; second leg excludes first-leg interior nodes; feasibility baseline, not global optimum",
            "pure_llm": {"backend": "codex_cli", "model": MODEL, "reasoning_effort": "low", "request_limit": 1, "retries": 0},
            "r4": {"protocol": "segment-v1", "backend": "codex_cli", "model": MODEL, "reasoning_effort": "low", "request_limit": 25, "retries": 0, "segment_actions_max": 10, "persistent_per_task": True, "structured_feedback_max": 3, "traditional_fallback": False},
        },
        "limitations": [
            "Single seed-0 task9 pilot; no statistical inference or formal cross-seed claim.",
            "R4 segment-v1 is versioned separately from historical single-step R4 results.",
            "Travel time and EV energy are shared evaluator estimates, not road measurements or billed electricity.",
            "Codex CLI account billing is not reported by the runner; rate-card values are estimates only.",
        ],
    }
    output_dir.mkdir(parents=True)
    write_json(output_dir / "manifest.json", manifest)
    write_json(output_dir / "traditional_baseline.json", baseline)
    write_json(output_dir / "state.json", {
        "status": "ready_for_codex" if execute else "preflight_complete_no_model_requests",
        "model_requests_started": 0,
        "unresolved_cli_invocation": None,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    if not execute:
        return {"status": manifest["status"], "model_requests": 0, "output": str(output_dir), "traditional": baseline["metrics"]}
    if not authorization_note.strip():
        raise ValueError("authorization_note_required_for_execute_mode")

    pure_attempt = _run_pure_llm(item, feature_context, output_dir / "pure_llm")
    write_json(output_dir / "state.json", {
        "status": "pure_llm_complete_starting_r4",
        "model_requests_started": 1,
        "pure_llm_status": pure_attempt.get("status"),
        "unresolved_cli_invocation": None if pure_attempt.get("status") == "response_received" else pure_attempt.get("provider_audit"),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    segment_audit = run_segment(
        output_dir / "r4_segment",
        request_limit=25,
        authorization_note=authorization_note,
    )
    pure_metrics = pure_attempt.get("metrics")
    r4_metrics = segment_audit.get("energy")
    comparison = {
        "schema_version": "r4_fair_task9_comparison_v1",
        "status": "pilot_completed" if segment_audit.get("status") == "completed" and pure_attempt.get("status") == "response_received" else "pilot_partial_or_failed",
        "formal_result": False,
        "common_task": {"map_id": item["road_map"].map_id, "task_id": task.task_id, "start": task.start_node_id, "pickup": task.pickup_node_id, "dropoff": task.dropoff_node_id},
        "methods": {
            "traditional": {"status": "completed", **baseline["metrics"]},
            "pure_llm_codex": {"status": pure_attempt.get("status"), "validator_status": (pure_attempt.get("validator") or {}).get("status"), **(pure_metrics or {})},
            "r4_segment_v1_codex": {"status": segment_audit.get("status"), "route_complete": segment_audit.get("route_complete"), "validator_status": "accepted_segments_only", **(r4_metrics or {})},
        },
        "codex_usage": {
            "pure_llm": pure_attempt.get("usage"),
            "r4_segment": segment_audit.get("usage"),
            "account_billing": "not_reported_by_codex_cli",
        },
        "interpretation": "A single controlled route-search pilot. Incomplete paths report accepted legal prefixes only; no ranking or general performance claim.",
    }
    write_json(output_dir / "comparison.json", comparison)
    write_json(output_dir / "state.json", {
        "status": comparison["status"],
        "model_requests_started": 1 + int(segment_audit.get("api_request_count", 0) or 0),
        "pure_llm_status": pure_attempt.get("status"),
        "r4_segment_status": segment_audit.get("status"),
        "unresolved_cli_invocation": segment_audit.get("unresolved_cli_invocation"),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    return comparison


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorization-note", default="")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.execute and not args.authorization_note.strip():
        parser.error("--execute requires the user's experiment authorization note")
    result = run(args.output, args.authorization_note, args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
