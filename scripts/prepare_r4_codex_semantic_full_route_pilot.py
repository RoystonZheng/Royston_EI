#!/usr/bin/env python3
"""Prepare (without sending) one bounded R4 full-route Codex pilot request."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for path in (ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.models import PackageConfig, RouteCandidate, RouteInputPackage, VehicleOrderTask
from route_experiment.r4_compact_route import (
    build_r4_compact_route_messages,
    make_r4_compact_route_constraint,
    validate_r4_compact_route_proposal,
)
from route_experiment.r4_stepwise import (
    _compact_map_from_package,
    audit_r4_stepwise_map_equivalence,
)
from route_experiment.routing import (
    _search_path,
    build_route_input_package,
    route_chain_reachability,
)
from run_r4_codex_formal_checkpoint import load_map
from run_route_r4_smoke import _road_feature_protocol, _validate_protocol_references
from preflight_r4_codex_all_frozen_tasks import (
    DEFAULT_INTENT_ARTIFACT,
    DEFAULT_INTENT_ROOT,
    DEFAULT_PURE_LLM_ROOT,
    DEFAULT_SEED_ROOT,
    _feature_context,
    _intent_source_audit,
    read_json,
    sha256_file,
)


DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_codex_semantic_full_route_pilot_preflight_20261023_v3"
DEFAULT_TASK_ID = "task-0008-0003"
DEFAULT_SEED = 8
MAX_ROUTE_EDGES = 512


def _route_nodes_to_moves(node_sequence: list[str]) -> str:
    """Encode a known directed node path using the compact route alphabet."""
    directions = {(1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S"}
    moves = []
    for current, target in zip(node_sequence, node_sequence[1:]):
        current_xy = tuple(map(int, current.removeprefix("node_").split("_")))
        target_xy = tuple(map(int, target.removeprefix("node_").split("_")))
        delta = (target_xy[0] - current_xy[0], target_xy[1] - current_xy[1])
        if delta not in directions:
            raise RuntimeError("pilot_reference_route_has_non_adjacent_nodes")
        moves.append(directions[delta])
    return "".join(moves)


def _find_no_revisit_reference_route(
    package: RouteInputPackage,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
) -> RouteCandidate | None:
    """Build a local feasibility witness without exposing it to the model.

    The first leg is the shortest path that does not prematurely visit the
    dropoff. The second leg is the shortest path after removing all previously
    visited nodes except the pickup. This is a deterministic witness strategy,
    not a globally optimal solver for the joint simple-path problem.
    """

    blocked = set(package.blocked_edge_ids)
    first_edges = [edge for edge in package.edges if edge.to_node != dropoff_node_id]
    first_leg, _ = _search_path(
        package.nodes,
        first_edges,
        start_node_id,
        pickup_node_id,
        "dijkstra",
        blocked,
    )
    if first_leg is None:
        return None

    previously_visited = set(first_leg.node_sequence[:-1])
    second_edges = [
        edge
        for edge in package.edges
        if edge.from_node not in previously_visited
        and edge.to_node not in previously_visited
    ]
    second_leg, _ = _search_path(
        package.nodes,
        second_edges,
        pickup_node_id,
        dropoff_node_id,
        "dijkstra",
        blocked,
    )
    if second_leg is None:
        return None

    node_sequence = list(first_leg.node_sequence) + list(second_leg.node_sequence[1:])
    if len(node_sequence) != len(set(node_sequence)):
        return None
    return RouteCandidate(
        route_id="no-revisit-feasibility-witness",
        node_sequence=node_sequence,
        edge_sequence=list(first_leg.edge_sequence) + list(second_leg.edge_sequence),
        route_source="sequential_dijkstra_no_revisit_witness",
        route_reachability=True,
        route_distance_m=(first_leg.route_distance_m or 0.0)
        + (second_leg.route_distance_m or 0.0),
        route_duration_sec=(first_leg.route_duration_sec or 0.0)
        + (second_leg.route_duration_sec or 0.0),
        validation_status="valid",
    )


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def prepare(
    seed_root: Path,
    pure_llm_root: Path,
    intent_root: Path,
    intent_artifact_path: Path,
    seed: int,
    task_id: str,
) -> dict[str, Any]:
    seed_dir = seed_root / f"seed_{seed}"
    pure_dir = pure_llm_root / f"seed_{seed}"
    map_path = seed_dir / f"map_100x100_seed_{seed}.json"
    order_path = seed_dir / f"order_tasks_seed_{seed}.json"
    pure_map_path = pure_dir / map_path.name
    pure_order_path = pure_dir / order_path.name
    if sha256_file(map_path) != sha256_file(pure_map_path):
        raise RuntimeError("pilot_map_differs_from_pure_llm_frozen_map")
    if sha256_file(order_path) != sha256_file(pure_order_path):
        raise RuntimeError("pilot_order_differs_from_pure_llm_frozen_order")

    order_data = read_json(order_path)
    raw_task = next(item for item in order_data["tasks"] if item["task_id"] == task_id)
    intent_data = read_json(intent_artifact_path)
    if intent_data.get("contains_evaluator_truth") is not False or intent_data.get("api_requests") != 0:
        raise RuntimeError("pilot_intent_artifact_truth_or_scope_gate_failed")
    intent = next(
        item for item in intent_data["records"]
        if int(item["seed"]) == seed
        and item["task_id"] == task_id
        and item["map_input_mode"] == "road_feature_protocol_v1"
    )
    source_audit = _intent_source_audit(intent, raw_task, intent_root)
    if not source_audit.get("passed") or intent.get("uses_evaluator_truth") is not False:
        raise RuntimeError("pilot_qwen_source_integrity_or_truth_gate_failed")
    if intent.get("status") != "resolved" or not intent.get("route_eligible"):
        raise RuntimeError("pilot_semantic_endpoint_not_route_eligible")
    route_intent = intent.get("route_intent")
    if not route_intent:
        raise RuntimeError("pilot_route_intent_missing")

    road_map = load_map(map_path)
    task = VehicleOrderTask(
        task_id=raw_task["task_id"],
        vehicle_id=raw_task.get("vehicle_id", "r4-pilot"),
        order_id=raw_task.get("order_id", task_id),
        start_node_id=raw_task["start_node_id"],
        pickup_node_id=intent["pickup_node_id"],
        dropoff_node_id=intent["dropoff_node_id"],
        pickup_text=raw_task.get("pickup_text"),
        dropoff_text=raw_task.get("dropoff_text"),
        task_type=raw_task.get("task_type", "semantic_place"),
    )
    protocol = _road_feature_protocol(road_map, task)
    protocol.context["random_seed"] = seed
    protocol_audit = _validate_protocol_references(protocol)
    if not protocol_audit.get("passed"):
        raise RuntimeError("pilot_road_feature_protocol_audit_failed")
    feature_context = _feature_context(protocol, route_intent)

    package = build_route_input_package(road_map, task, PackageConfig())
    reachability = route_chain_reachability(
        package.edges,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        package.blocked_edge_ids,
    )
    if not reachability.get("chain_reachable"):
        raise RuntimeError("pilot_route_chain_not_reachable")
    messages = build_r4_compact_route_messages(
        package,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        route_intent=route_intent,
        route_feature_context=feature_context,
    )
    prompt = CodexCliRouteStepClient._render_prompt(messages)
    user_payload = json.loads(messages[1]["content"])
    if user_payload.get("i") != feature_context:
        raise RuntimeError("pilot_route_feature_context_missing")
    if any(marker in prompt for marker in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")):
        raise RuntimeError("pilot_evaluator_truth_leaked_to_prompt")
    compact_map = _compact_map_from_package(
        package, task.start_node_id, task.pickup_node_id, task.dropoff_node_id
    )
    map_audit = audit_r4_stepwise_map_equivalence(package, compact_map)
    if not map_audit.get("passed"):
        raise RuntimeError("pilot_map_edge_cost_equivalence_failed")

    reference_route = _find_no_revisit_reference_route(
        package,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
    )
    if reference_route is None:
        raise RuntimeError("pilot_reference_route_missing")
    reference_nodes = list(reference_route.node_sequence)
    reference_repeat_count = len(reference_nodes) - len(set(reference_nodes))
    if reference_repeat_count:
        raise RuntimeError("pilot_reference_route_does_not_meet_confirmed_no_revisit_rule")
    reference_moves = _route_nodes_to_moves(reference_nodes)
    reference_validation = validate_r4_compact_route_proposal(
        road_map,
        start_node_id=task.start_node_id,
        pickup_node_id=task.pickup_node_id,
        dropoff_node_id=task.dropoff_node_id,
        blocked_edge_ids=package.blocked_edge_ids,
        payload={
            "status": "completed",
            "move_sequence": reference_moves,
            "step_check_sequence": "1" * len(reference_moves),
            "failure_reason_code": "none",
        },
    )
    if not reference_validation.get("accepted"):
        raise RuntimeError("pilot_reference_route_rejected_by_full_route_validator")

    constraint = make_r4_compact_route_constraint(MAX_ROUTE_EDGES)
    schema_json = json.dumps(constraint.schema, ensure_ascii=False, sort_keys=True, indent=2)
    return {
        "schema_version": "r4_codex_semantic_full_route_pilot_preflight_v2",
        "status": "ready_for_user_authorization_no_model_request_sent",
        "candidate_is_formal_result": False,
        "model_request_sent": False,
        "retry_count": 0,
        "fallback_to_traditional_route": False,
        "seed": seed,
        "task_id": task_id,
        "task_type": task.task_type,
        "map_id": road_map.map_id,
        "start_node_id": task.start_node_id,
        "pickup_node_id": task.pickup_node_id,
        "dropoff_node_id": task.dropoff_node_id,
        "semantic_feature_context_included": True,
        "qwen_intent_source_audit": source_audit,
        "map_order_same_as_pure_llm": True,
        "map_sha256": sha256_file(map_path),
        "order_file_sha256": sha256_file(order_path),
        "task_public_fields_sha256": sha256_bytes(
            json.dumps(raw_task, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ),
        "intent_record_route_eligible": bool(intent.get("route_eligible")),
        "route_chain_reachable": bool(reachability["chain_reachable"]),
        "local_directed_map_equivalence": map_audit,
        "evaluator_truth_absent_from_prompt": True,
        "route_action_limit": MAX_ROUTE_EDGES,
        "prompt_bytes": len(prompt.encode("utf-8")),
        "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
        "schema_bytes": len(schema_json.encode("utf-8")),
        "schema_sha256": sha256_bytes(schema_json.encode("utf-8")),
        "independent_reference_audit_not_sent_to_model": {
            "method": "sequential_dijkstra_no_revisit_feasibility_witness",
            "status": "witness_found",
            "edge_count": len(reference_route.edge_sequence),
            "no_revisit_passed": reference_repeat_count == 0,
            "full_route_validator_accepted": reference_validation["accepted"],
            "full_route_validator_violations": len(reference_validation["violations"]),
            "route_sequence_sha256": sha256_bytes(
                json.dumps(reference_nodes, separators=(",", ":")).encode("utf-8")
            ),
        },
        "limitations": [
            "本产物只准备一个路线请求，未调用 Codex CLI 或其他模型。",
            "这是路线提示与本地校验 smoke，不是性能样本；模型失败或违规时零重试并保留失败。",
            "Codex CLI 实际输出上限/账户扣减在本次未测量；prompt/schema 字节不换算 token 或费用。",
            "离线见证路线只证明该端点任务存在一条合法不重访路线，不进入模型输入，也不是全局最优保证。",
        ],
        "messages": messages,
        "schema": constraint.schema,
    }


def render(preflight: dict[str, Any]) -> str:
    fields = [
        ("状态", preflight["status"]),
        ("种子 / 任务", f"{preflight['seed']} / {preflight['task_id']}"),
        ("端点", f"{preflight['start_node_id']} → {preflight['pickup_node_id']} → {preflight['dropoff_node_id']}"),
        ("模型请求", "0（待授权）"),
        ("道路特征上下文", "已加入"),
        ("模型提示", f"{preflight['prompt_bytes']:,} bytes"),
        ("输出边数上限", preflight["route_action_limit"]),
        ("本地参考长度", f"{preflight['independent_reference_audit_not_sent_to_model']['edge_count']} edges；不重访"),
        (
            "参考路线全程校验",
            "通过；0 项违规"
            if preflight["independent_reference_audit_not_sent_to_model"]["full_route_validator_accepted"]
            else "失败",
        ),
    ]
    table = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in fields
    )
    limitations = "".join(f"<li>{html.escape(item)}</li>" for item in preflight["limitations"])
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>R4 单次语义整路线 smoke 预检</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:28px;color:#172033;background:#f5f7fb}}main{{max-width:900px;margin:auto;background:white;border:1px solid #dce3ef;border-radius:12px;padding:24px}}h1{{margin-top:0}}.notice{{background:#fff7e6;border:1px solid #f0d49b;padding:14px;border-radius:8px}}table{{border-collapse:collapse;width:100%;margin:18px 0}}th,td{{text-align:left;border-bottom:1px solid #e5e9f0;padding:10px}}th{{width:190px;color:#59677e}}li{{margin:8px 0}}code{{background:#eef2f8;padding:2px 4px}}</style></head><body><main><h1>R4 单条语义整路线 smoke：请求前预检</h1><p class="notice">本页对应一个冻结的 100×100 语义任务。地图/订单一致、RoadFeatureInputProtocol 上下文已加入、模型请求未发送；完整路线仍需 Codex 返回并由本地逐边、不重访校验器审核。</p><table>{table}</table><p>模型提示：<a href="prompt.json">prompt.json</a>；响应 schema：<a href="schema.json">schema.json</a>；机器可读门禁：<a href="preflight.json">preflight.json</a>。</p><h2>限制</h2><ul>{limitations}</ul></main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-root", type=Path, default=DEFAULT_SEED_ROOT)
    parser.add_argument("--pure-llm-root", type=Path, default=DEFAULT_PURE_LLM_ROOT)
    parser.add_argument("--intent-root", type=Path, default=DEFAULT_INTENT_ROOT)
    parser.add_argument("--intent-artifact", type=Path, default=DEFAULT_INTENT_ARTIFACT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit("refusing_to_overwrite_existing_pilot_preflight")
    result = prepare(
        args.seed_root,
        args.pure_llm_root,
        args.intent_root,
        args.intent_artifact,
        args.seed,
        args.task_id,
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "prompt.json").write_text(
        json.dumps(result["messages"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "schema.json").write_text(
        json.dumps(result["schema"], ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    public = {key: value for key, value in result.items() if key not in ("messages", "schema")}
    (args.output_dir / "preflight.json").write_text(
        json.dumps(public, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "index.html").write_text(render(public), encoding="utf-8")
    print(json.dumps(public, ensure_ascii=False, indent=2))
    print(f"output_dir={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
