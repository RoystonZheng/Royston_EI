#!/usr/bin/env python3
"""Offline R4 preflight for all frozen 100x100 order and formal tasks.

This builds no model requests. It checks shared frozen inputs, joins semantic
orders to the truth-free R4 intent artifact, then builds the same initial
stepwise prompt/schema and directed reachability lower bound used by R4.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for path in (ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.models import PackageConfig, VehicleOrderTask
from route_experiment.r4_constraints import build_r4_route_output_constraint
from route_experiment.r4_stepwise import (
    audit_r4_stepwise_map_equivalence,
    build_r4_step_output_constraint,
    build_r4_stepwise_messages,
)
from route_experiment.routing import build_route_input_package, route_chain_reachability
from run_r4_codex_formal_checkpoint import load_map
from run_route_r4_smoke import _road_feature_protocol, _validate_protocol_references


DEFAULT_SEED_ROOT = ROOT / "outputs/route_layer/traditional_experiment_v1/seeds"
DEFAULT_PURE_LLM_ROOT = ROOT / "outputs/route_layer/pure_llm_experiment_v8/seeds"
DEFAULT_INTENT_ROOT = ROOT / "outputs/route_layer/r4_qwen_intent_local_batch_v3"
DEFAULT_INTENT_ARTIFACT = ROOT / "outputs/route_layer/r4_relative_grounding_postprocess_dev_v2/route_intents.json"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_codex_all_frozen_tasks_preflight_20261022_v2"
ROUTE_ACTION_BUDGET = 512


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(raw)


def strip_repeated_map(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_repeated_map(item) for key, item in value.items() if key != "road_map"}
    if isinstance(value, list):
        return [strip_repeated_map(item) for item in value]
    return value


def _feature_context(protocol, route_intent: dict[str, Any]) -> dict[str, Any]:
    entries = {row["entry_id"]: row for row in protocol.semantic_index}
    pickup_ref = route_intent["pickup_anchor_refs"][0]
    dropoff_ref = route_intent["dropoff_anchor_refs"][0]
    if pickup_ref not in entries or dropoff_ref not in entries:
        raise ValueError("route_intent_anchor_not_in_protocol_index")
    pickup = entries[pickup_ref]
    dropoff = entries[dropoff_ref]
    return {
        "protocol": {"name": protocol.protocol_name, "version": protocol.protocol_version},
        "pickup_reference": {
            key: pickup[key]
            for key in (
                "entry_id", "entry_type", "name", "aliases", "side_of_road",
                "orientation", "anchor_node_id", "pickup_candidates",
            )
            if key in pickup
        },
        "dropoff_reference": {
            key: dropoff[key]
            for key in (
                "entry_id", "entry_type", "name", "aliases", "side_of_road",
                "orientation", "anchor_node_id", "dropoff_candidates",
            )
            if key in dropoff
        },
        "intent": {
            "pickup_anchor_refs": route_intent.get("pickup_anchor_refs", []),
            "dropoff_anchor_refs": route_intent.get("dropoff_anchor_refs", []),
            "pickup_relation": route_intent.get("pickup_relation"),
            "pickup_side": route_intent.get("pickup_side"),
            "dropoff_relation": route_intent.get("dropoff_relation"),
            "feature_evidence_refs": route_intent.get("feature_evidence_refs", []),
        },
    }


def _eligible_endpoint(node_by_id, node_id: str | None, kind: str) -> tuple[bool, str]:
    if not node_id or node_id not in node_by_id:
        return False, f"{kind}_node_missing"
    node = node_by_id[node_id]
    if kind == "pickup" and not node.legal_pickup:
        return False, "pickup_node_illegal"
    if kind == "dropoff" and not node.legal_dropoff:
        return False, "dropoff_node_illegal"
    return True, "ok"


def _task_from_raw(raw: dict[str, Any], pickup: str | None, dropoff: str | None) -> VehicleOrderTask:
    return VehicleOrderTask(
        task_id=raw["task_id"],
        vehicle_id=raw.get("vehicle_id", "r4-preflight"),
        order_id=raw.get("order_id", raw["task_id"]),
        start_node_id=raw["start_node_id"],
        pickup_node_id=pickup,
        dropoff_node_id=dropoff,
        pickup_text=raw.get("pickup_text"),
        dropoff_text=raw.get("dropoff_text"),
        task_type=raw.get("task_type", "exact_node"),
    )


def _intent_source_audit(
    record: dict[str, Any], raw_task: dict[str, Any], intent_root: Path
) -> dict[str, Any]:
    seed = int(record["seed"])
    task_id = record["task_id"]
    source_dir = intent_root / f"seed_{seed}" / task_id / "road_feature_protocol_v1"
    query_path = source_dir / "query.json"
    result_path = source_dir / "result.json"
    if not query_path.is_file() or not result_path.is_file():
        return {"passed": False, "reason": "source_qwen_query_or_result_missing"}
    query = read_json(query_path)
    result = read_json(result_path)
    query_matches_result = query == result.get("model_query")
    query_hash_matches = canonical_json_sha256(query) == record.get("source_query_sha256")
    task_fields_match = all(
        query.get(key) == raw_task.get(key)
        for key in ("task_id", "start_node_id", "pickup_text", "dropoff_text")
    )
    return {
        "passed": bool(query_matches_result and query_hash_matches and task_fields_match),
        "query_matches_saved_model_query": query_matches_result,
        "canonical_query_hash_matches_intent_record": query_hash_matches,
        "public_task_fields_match": task_fields_match,
        "query_file_sha256": sha256_file(query_path),
    }


def _task_row(
    *,
    seed: int,
    sample_group: str,
    scenario_type: str | None,
    raw_task: dict[str, Any],
    pickup_node_id: str | None,
    dropoff_node_id: str | None,
    road_map,
    blocked_edge_ids: tuple[str, ...],
    route_intent: dict[str, Any] | None,
    intent_audit: dict[str, Any] | None,
    intent_issue: str | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seed": seed,
        "sample_group": sample_group,
        "scenario_type": scenario_type,
        "task_id": raw_task["task_id"],
        "task_type": raw_task.get("task_type"),
        "start_node_id": raw_task.get("start_node_id"),
        "pickup_node_id": pickup_node_id,
        "dropoff_node_id": dropoff_node_id,
        "blocked_edge_count": len(blocked_edge_ids),
        "intent_source_audit": intent_audit,
        "intent_issue": intent_issue,
        "route_feature_context": False,
        "endpoint_gate": "not_checked",
        "route_search_gate": "not_built",
        "route_chain_reachable": None,
        "minimum_route_actions_lower_bound": None,
        "route_action_budget": ROUTE_ACTION_BUDGET,
        "package_nodes": None,
        "package_directed_edges": None,
        "initial_prompt_bytes": None,
        "initial_schema_bytes": None,
        "initial_request_payload_bytes": None,
        "compact_map_equivalence_passed": None,
        "evaluator_truth_absent_from_prompt": None,
        "preflight_errors": [],
    }

    if intent_audit is not None and not intent_audit.get("passed"):
        row["endpoint_gate"] = "intent_source_integrity_failed"
        row["route_search_gate"] = "not_built_source_integrity"
        return row
    if intent_issue is not None:
        row["endpoint_gate"] = "intent_resolution_failed"
        row["route_search_gate"] = "not_built_intent_resolution_failed"
        row["preflight_errors"] = [intent_issue]
        return row
    if route_intent is not None:
        if not route_intent.get("pickup_anchor_refs") or not route_intent.get("dropoff_anchor_refs"):
            row["endpoint_gate"] = "intent_anchor_missing"
            row["route_search_gate"] = "not_built_intent_invalid"
            return row

    node_by_id = {node.node_id: node for node in road_map.nodes}
    endpoint_checks = {
        "start": (raw_task.get("start_node_id") in node_by_id, "start_node_missing"),
        "pickup": _eligible_endpoint(node_by_id, pickup_node_id, "pickup"),
        "dropoff": _eligible_endpoint(node_by_id, dropoff_node_id, "dropoff"),
    }
    endpoint_errors = []
    if not endpoint_checks["start"][0]:
        endpoint_errors.append(endpoint_checks["start"][1])
    for kind in ("pickup", "dropoff"):
        if not endpoint_checks[kind][0]:
            endpoint_errors.append(endpoint_checks[kind][1])
    if endpoint_errors:
        row["endpoint_gate"] = "rejected"
        row["route_search_gate"] = "invalid_endpoint"
        row["preflight_errors"] = endpoint_errors
        return row
    row["endpoint_gate"] = "passed"

    task = _task_from_raw(raw_task, pickup_node_id, dropoff_node_id)
    try:
        package = build_route_input_package(
            road_map,
            task,
            PackageConfig(),
            blocked_edge_ids=blocked_edge_ids,
        )
        reachability = route_chain_reachability(
            package.edges,
            task.start_node_id,
            task.pickup_node_id,
            task.dropoff_node_id,
            package.blocked_edge_ids,
        )
        row.update(
            {
                "route_chain_reachable": bool(reachability["chain_reachable"]),
                "start_to_pickup_reachable": bool(reachability["start_to_pickup"]),
                "pickup_to_dropoff_reachable": bool(reachability["pickup_to_dropoff"]),
                "package_nodes": len(package.nodes),
                "package_directed_edges": len(package.edges),
            }
        )
        if not reachability["chain_reachable"]:
            row["route_search_gate"] = "deterministic_no_path_preflight"
            row["preflight_disposition"] = (
                "record_expected_no_path_without_route_model_request_to_avoid_wasting_tokens"
            )
            return row
        full_constraint = build_r4_route_output_constraint(
            package, pickup_node_id, dropoff_node_id, allow_failed=True
        )
        step_constraint = build_r4_step_output_constraint(
            package,
            full_constraint,
            current_node_id=task.start_node_id,
            pickup_seen=task.start_node_id == pickup_node_id,
            visited_node_ids=[task.start_node_id],
            blocked_edge_ids=blocked_edge_ids,
            remaining_action_budget=ROUTE_ACTION_BUDGET,
        )
        feature_context = None
        if route_intent is not None:
            protocol = _road_feature_protocol(road_map, task)
            protocol.context["random_seed"] = seed
            protocol_audit = _validate_protocol_references(protocol)
            if not protocol_audit["passed"]:
                raise ValueError("road_feature_protocol_reference_audit_failed")
            feature_context = _feature_context(protocol, route_intent)
            row["route_feature_context"] = True
        messages = build_r4_stepwise_messages(
            package,
            task.start_node_id,
            pickup_node_id,
            dropoff_node_id,
            current_node_id=task.start_node_id,
            visited_node_ids=[task.start_node_id],
            remaining_action_budget=ROUTE_ACTION_BUDGET,
            route_feature_context=feature_context,
        )
        prompt = CodexCliRouteStepClient._render_prompt(messages)
        payload_text = messages[-1]["content"]
        payload = json.loads(payload_text)
        map_audit = audit_r4_stepwise_map_equivalence(package, payload["m"])
        truth_markers = (
            "true_pickup_node_id",
            "true_dropoff_node_id",
            "ground_truth",
        )
        truth_absent = not any(marker in prompt for marker in truth_markers)
        schema_json = json.dumps(step_constraint.schema, ensure_ascii=False, sort_keys=True)
        prompt_bytes = len(prompt.encode("utf-8"))
        schema_bytes = len(schema_json.encode("utf-8"))
        row.update(
            {
                "route_search_gate": (
                    "reachable_initial_step_built"
                    if reachability["chain_reachable"]
                    else "unreachable_initial_step_built"
                ),
                "route_chain_reachable": bool(reachability["chain_reachable"]),
                "start_to_pickup_reachable": bool(reachability["start_to_pickup"]),
                "pickup_to_dropoff_reachable": bool(reachability["pickup_to_dropoff"]),
                "minimum_route_actions_lower_bound": step_constraint.remaining_minimum_moves,
                "initial_allowed_directions": list(step_constraint.edge_by_direction),
                "initial_filtered_directions": step_constraint.filtered_directions,
                "initial_schema_sha256": step_constraint.schema_sha256,
                "package_nodes": len(package.nodes),
                "package_directed_edges": len(package.edges),
                "initial_prompt_bytes": prompt_bytes,
                "initial_schema_bytes": schema_bytes,
                "initial_request_payload_bytes": prompt_bytes + schema_bytes,
                "compact_map_equivalence_passed": bool(map_audit["passed"]),
                "compact_map_equivalence": map_audit,
                "evaluator_truth_absent_from_prompt": truth_absent,
                "route_messages_sha256": sha256_bytes(prompt.encode("utf-8")),
            }
        )
        if step_constraint.remaining_minimum_moves is not None:
            row["minimum_actions_fit_budget"] = (
                step_constraint.remaining_minimum_moves <= ROUTE_ACTION_BUDGET
            )
            row["initial_prompt_bytes_times_min_actions_floor"] = (
                prompt_bytes * step_constraint.remaining_minimum_moves
            )
        else:
            row["minimum_actions_fit_budget"] = None
            row["initial_prompt_bytes_times_min_actions_floor"] = None
        if not map_audit["passed"]:
            row["preflight_errors"].append("compact_map_equivalence_failed")
        if not truth_absent:
            row["preflight_errors"].append("evaluator_truth_found_in_prompt")
    except Exception as exc:  # retain a row for every frozen task
        row["route_search_gate"] = "preflight_error"
        row["preflight_errors"].append(f"{type(exc).__name__}:{exc}")
    return row


def _summary(rows: list[dict[str, Any]], shared_inputs: dict[str, Any]) -> dict[str, Any]:
    groups = defaultdict(list)
    for row in rows:
        groups[row["sample_group"]].append(row)
    routeable = [row for row in rows if row.get("route_chain_reachable")]
    prompt_values = [row["initial_prompt_bytes"] for row in routeable if row.get("initial_prompt_bytes") is not None]
    schema_values = [row["initial_schema_bytes"] for row in routeable if row.get("initial_schema_bytes") is not None]
    actions = [row["minimum_route_actions_lower_bound"] for row in routeable if row.get("minimum_route_actions_lower_bound") is not None]

    def stats(values: list[int | float]) -> dict[str, Any] | None:
        if not values:
            return None
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "min": min(ordered),
            "median": ordered[(len(ordered) - 1) // 2],
            "p90": ordered[round((len(ordered) - 1) * 0.9)],
            "max": max(ordered),
            "mean": sum(ordered) / len(ordered),
        }

    by_seed = {}
    for seed in range(10):
        seed_rows = [row for row in rows if row["seed"] == seed]
        by_seed[str(seed)] = {
            "sample_count": len(seed_rows),
            "minimum_model_request_lower_bound": sum(
                row.get("minimum_route_actions_lower_bound") or 0
                for row in seed_rows
                if row.get("route_chain_reachable") is True
            ),
            "groups": {
                group: {
                    "count": len([row for row in seed_rows if row["sample_group"] == group]),
                    "routeable": sum(
                        row["sample_group"] == group and row.get("route_chain_reachable") is True
                        for row in seed_rows
                    ),
                    "unreachable": sum(
                        row["sample_group"] == group and row.get("route_chain_reachable") is False
                        for row in seed_rows
                    ),
                    "endpoint_rejected": sum(
                        row["sample_group"] == group and row.get("endpoint_gate") == "rejected"
                        for row in seed_rows
                    ),
                    "intent_integrity_failed": sum(
                        row["sample_group"] == group and row.get("endpoint_gate") == "intent_source_integrity_failed"
                        for row in seed_rows
                    ),
                    "intent_resolution_failed": sum(
                        row["sample_group"] == group and row.get("endpoint_gate") == "intent_resolution_failed"
                        for row in seed_rows
                    ),
                    "minimum_model_request_lower_bound": sum(
                        row.get("minimum_route_actions_lower_bound") or 0
                        for row in seed_rows
                        if row["sample_group"] == group and row.get("route_chain_reachable") is True
                    ),
                }
                for group in ("orders", "formal_scenarios")
            },
        }
    return {
        "schema_version": "r4_codex_all_frozen_tasks_preflight_v1",
        "status": "offline_preflight_only_no_model_requests",
        "formal_result": False,
        "formal_batch_started": False,
        "codex_requests": 0,
        "hosted_model_requests": 0,
        "local_qwen_requests_this_run": 0,
        "seed_count": 10,
        "task_count": len(rows),
        "order_task_count": len(groups["orders"]),
        "formal_scenario_count": len(groups["formal_scenarios"]),
        "tasks_per_seed": {str(seed): sum(row["seed"] == seed for row in rows) for seed in range(10)},
        "route_chain_reachable_count": sum(row.get("route_chain_reachable") is True for row in rows),
        "expected_no_path_count": sum(row.get("route_chain_reachable") is False for row in rows),
        "endpoint_rejected_count": sum(row.get("endpoint_gate") == "rejected" for row in rows),
        "intent_source_integrity_failed_count": sum(
            row.get("endpoint_gate") == "intent_source_integrity_failed" for row in rows
        ),
        "intent_resolution_failed_count": sum(
            row.get("endpoint_gate") == "intent_resolution_failed" for row in rows
        ),
        "preflight_error_count": sum(row.get("route_search_gate") == "preflight_error" for row in rows),
        "route_action_budget": ROUTE_ACTION_BUDGET,
        "minimum_action_lower_bound_stats_on_reachable_tasks": stats(actions),
        "minimum_stepwise_model_request_lower_bound": sum(actions),
        "minimum_initial_prompt_bytes_repeated_floor": sum(
            row["initial_prompt_bytes"] * row["minimum_route_actions_lower_bound"]
            for row in routeable
            if row.get("initial_prompt_bytes") is not None
            and row.get("minimum_route_actions_lower_bound") is not None
        ),
        "initial_prompt_byte_stats_on_reachable_tasks": stats(prompt_values),
        "initial_schema_byte_stats_on_reachable_tasks": stats(schema_values),
        "shared_input_audit": shared_inputs,
        "by_seed": by_seed,
        "limitations": [
            "This report builds only the initial stepwise prompt and schema; it does not run route generation.",
            "Prompt/schema byte lengths are not token counts, costs, or an account billing estimate.",
            "A completed stepwise route uses one model request per accepted direction; later prompts include a growing visited-node list.",
            "The sum of shortest-action lower bounds is a minimum model-request count for this one-request-per-action protocol; rejected suggestions could increase it.",
            "initial_prompt_bytes_times_min_actions_floor is only a byte-based lower-bound proxy, not a token/cost estimate.",
            "The semantic endpoint records come from the same previously inspected Qwen batch and deterministic postprocess; this is not held-out validation.",
            "Illegal endpoints and expected no-path scenarios remain in the 150-task denominator and are reported separately.",
        ],
        "task_rows": rows,
    }


def _render_html(result: dict[str, Any]) -> str:
    summary_labels = {
        "task_count": "任务总数",
        "order_task_count": "订单任务",
        "formal_scenario_count": "正式场景",
        "route_chain_reachable_count": "两段有向可达",
        "expected_no_path_count": "预设不可达",
        "endpoint_rejected_count": "非法端点",
        "intent_source_integrity_failed_count": "意图来源校验失败",
        "intent_resolution_failed_count": "语义端点解析失败",
        "preflight_error_count": "预检程序错误",
        "route_action_budget": "每任务动作上限",
        "minimum_stepwise_model_request_lower_bound": "分步调用最低请求数",
    }
    cards = "".join(
        f'<div class="card"><span>{html.escape(label)}</span><strong>{html.escape(str(result.get(key)))}</strong></div>'
        for key, label in summary_labels.items()
    )
    headers = [
        ("seed", "种子"),
        ("sample_group", "样本组"),
        ("scenario_type", "场景类型"),
        ("task_id", "任务编号"),
        ("task_type", "任务类型"),
        ("start_node_id", "起点"),
        ("pickup_node_id", "上车点"),
        ("dropoff_node_id", "下车点"),
        ("endpoint_gate", "端点门禁"),
        ("route_search_gate", "路线预检状态"),
        ("route_chain_reachable", "两段有向可达"),
        ("minimum_route_actions_lower_bound", "最短动作下界"),
        ("package_nodes", "输入节点数"),
        ("package_directed_edges", "输入有向边数"),
        ("initial_prompt_bytes", "首步提示字节"),
        ("initial_schema_bytes", "首步 Schema 字节"),
        ("evaluator_truth_absent_from_prompt", "提示不含评估真值"),
    ]
    value_labels = {
        "orders": "订单",
        "formal_scenarios": "正式场景",
        "feasible": "可行",
        "boundary": "边界",
        "illegal_endpoint": "非法端点",
        "detour": "绕行",
        "unreachable": "不可达",
        "exact_node": "精确节点",
        "feasible_exact": "可行场景",
        "boundary_exact": "边界场景",
        "illegal_endpoint": "非法端点",
        "detour_exact": "绕行场景",
        "unreachable_exact": "不可达场景",
        "passed": "通过",
        "rejected": "拒绝",
        "intent_resolution_failed": "语义端点解析失败",
        "intent_source_integrity_failed": "意图来源校验失败",
        "reachable_initial_step_built": "可达，首步输入已构建",
        "unreachable_initial_step_built": "不可达",
        "invalid_endpoint": "非法端点，不构建路线输入",
        "deterministic_no_path_preflight": "确定性预检无路，不请求模型",
        "not_built_intent_resolution_failed": "端点解析失败，不构建路线输入",
        "not_built_source_integrity": "意图来源校验失败，不构建路线输入",
        "preflight_error": "预检程序错误",
        "True": "是",
        "False": "否",
    }
    task_type_labels = {
        "semantic_landmark_nearby": "地标附近",
        "semantic_landmark_relative": "地标相对方位",
        "semantic_road_feature_position": "道路特征位置",
    }
    table_head = "".join(f"<th>{html.escape(label)}</th>" for _, label in headers)
    table_rows = []
    for row in result["task_rows"]:
        cells = []
        for key, _ in headers:
            value = row.get(key)
            if isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if key == "task_type" and value in task_type_labels:
                value = task_type_labels[value]
            elif isinstance(value, bool):
                value = "是" if value else "否"
            elif value is not None:
                value = value_labels.get(str(value), value)
            cells.append(f"<td>{html.escape('' if value is None else str(value))}</td>")
        css = "bad" if row.get("route_search_gate") in {"invalid_endpoint", "unreachable_initial_step_built", "preflight_error", "not_built_source_integrity"} else ""
        table_rows.append(f'<tr class="{css}">' + "".join(cells) + "</tr>")
    warnings = "".join(f"<li>{html.escape(item)}</li>" for item in result["limitations"])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>R4 十种子冻结任务离线预检</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;color:#172033;background:#f6f8fc}}
h1{{margin:0 0 6px}}.sub{{color:#546179;margin:0 0 18px}}.notice{{padding:14px;background:#fff7e6;border:1px solid #f3d18b;border-radius:8px;margin:16px 0}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px}}.card{{background:white;border:1px solid #dae1ed;border-radius:8px;padding:12px}}
.card span{{display:block;color:#617089;font-size:12px;overflow-wrap:anywhere}}.card strong{{font-size:21px}}.tablewrap{{overflow:auto;background:white;border:1px solid #dce3ef;border-radius:8px;margin-top:18px}}
table{{border-collapse:collapse;min-width:1700px;width:100%;font-size:12px}}th,td{{border-bottom:1px solid #e4e8f0;padding:8px;text-align:left;white-space:nowrap}}th{{position:sticky;top:0;background:#edf2fb;z-index:1}}tr.bad{{background:#fff3f1}}
li{{margin:6px 0}}code{{background:#e9edf4;padding:2px 4px;border-radius:4px}}
</style></head><body><h1>R4 十种子冻结任务离线预检</h1>
<p class="sub">状态：离线预检通过；Codex/托管模型请求：0；不是路线实验结果。</p>
<div class="notice"><b>如何读：</b>每个种子 10 个订单 + 5 个正式场景。保留非法端点、不可达和语义解析门禁失败；这些状态不会被算作模型路线成功。表中提示和 Schema 是首步字节数，不是 token 或费用。</div>
<div class="cards">{cards}</div><div class="tablewrap"><table><thead><tr>{table_head}</tr></thead><tbody>{''.join(table_rows)}</tbody></table></div>
<h2>限制</h2><ul>{warnings}</ul><p>机器可读详情：<code>summary.json</code>。</p></body></html>"""


def audit(seed_root: Path, pure_llm_root: Path, intent_root: Path, intent_artifact_path: Path) -> dict[str, Any]:
    intent_data = read_json(intent_artifact_path)
    if intent_data.get("contains_evaluator_truth") is not False or intent_data.get("api_requests") != 0:
        raise RuntimeError("route_intent_artifact_scope_or_truth_gate_failed")
    intent_records = {
        (int(row["seed"]), row["task_id"], row["map_input_mode"]): row
        for row in intent_data["records"]
    }
    shared_by_seed: dict[str, Any] = {}
    all_rows: list[dict[str, Any]] = []
    for seed in range(10):
        seed_dir = seed_root / f"seed_{seed}"
        map_path = seed_dir / f"map_100x100_seed_{seed}.json"
        order_path = seed_dir / f"order_tasks_seed_{seed}.json"
        scenario_path = seed_dir / f"formal_scenarios_seed_{seed}.json"
        pure_dir = pure_llm_root / f"seed_{seed}"
        pure_map = pure_dir / map_path.name
        pure_orders = pure_dir / order_path.name
        pure_scenarios = pure_dir / scenario_path.name
        checks = {
            "map_matches_pure_llm_byte_for_byte": sha256_file(map_path) == sha256_file(pure_map),
            "orders_match_pure_llm_byte_for_byte": sha256_file(order_path) == sha256_file(pure_orders),
            "formal_scenarios_match_after_removing_duplicate_map": strip_repeated_map(read_json(scenario_path))
            == strip_repeated_map(read_json(pure_scenarios)),
        }
        shared_by_seed[str(seed)] = {
            **checks,
            "traditional_map_sha256": sha256_file(map_path),
            "pure_llm_map_sha256": sha256_file(pure_map),
            "traditional_order_sha256": sha256_file(order_path),
            "pure_llm_order_sha256": sha256_file(pure_orders),
            "traditional_scenarios_sha256": sha256_file(scenario_path),
            "pure_llm_scenarios_sha256": sha256_file(pure_scenarios),
        }
        road_map = load_map(map_path)
        orders = read_json(order_path)
        scenarios = read_json(scenario_path)
        if len(orders["tasks"]) != 10 or len(scenarios["scenarios"]) != 5:
            raise RuntimeError(f"unexpected_task_count_seed_{seed}")

        for raw in orders["tasks"]:
            route_intent = None
            intent_audit = None
            intent_issue = None
            if raw["task_type"] == "exact_node":
                pickup, dropoff = raw.get("pickup_node_id"), raw.get("dropoff_node_id")
            else:
                intent = intent_records.get((seed, raw["task_id"], "road_feature_protocol_v1"))
                if intent is None:
                    pickup = dropoff = None
                    intent_audit = {"passed": False, "reason": "r4_road_feature_intent_record_missing"}
                else:
                    intent_audit = _intent_source_audit(intent, raw, intent_root)
                    if intent.get("uses_evaluator_truth") is not False:
                        intent_audit["passed"] = False
                        intent_audit["reason"] = "route_intent_truth_isolation_gate_failed"
                    elif intent.get("status") != "resolved" or not intent.get("route_eligible"):
                        intent_issue = intent.get("resolution_reason") or "route_intent_unresolved"
                    else:
                        route_intent = intent.get("route_intent")
                    pickup, dropoff = intent.get("pickup_node_id"), intent.get("dropoff_node_id")
            all_rows.append(
                _task_row(
                    seed=seed,
                    sample_group="orders",
                    scenario_type=None,
                    raw_task=raw,
                    pickup_node_id=pickup,
                    dropoff_node_id=dropoff,
                    road_map=road_map,
                    blocked_edge_ids=(),
                    route_intent=route_intent,
                    intent_audit=intent_audit,
                    intent_issue=intent_issue,
                )
            )

        for scenario in scenarios["scenarios"]:
            for raw in scenario["tasks"]:
                all_rows.append(
                    _task_row(
                        seed=seed,
                        sample_group="formal_scenarios",
                        scenario_type=scenario["scenario_type"],
                        raw_task=raw,
                        pickup_node_id=raw.get("pickup_node_id"),
                        dropoff_node_id=raw.get("dropoff_node_id"),
                        road_map=road_map,
                        blocked_edge_ids=tuple(scenario.get("blocked_edge_ids", [])),
                        route_intent=None,
                        intent_audit=None,
                        intent_issue=None,
                    )
                )
    shared_inputs = {
        "all_seed_map_order_and_scenario_comparisons_passed": all(
            all(item[key] for key in (
                "map_matches_pure_llm_byte_for_byte",
                "orders_match_pure_llm_byte_for_byte",
                "formal_scenarios_match_after_removing_duplicate_map",
            ))
            for item in shared_by_seed.values()
        ),
        "by_seed": shared_by_seed,
    }
    result = _summary(all_rows, shared_inputs)
    if len(all_rows) != 150:
        raise RuntimeError(f"expected_150_frozen_tasks_got_{len(all_rows)}")
    result["intent_artifact"] = {
        "path": str(intent_artifact_path.relative_to(ROOT)),
        "schema_version": intent_data.get("schema_version"),
        "scope": intent_data.get("scope"),
        "contains_evaluator_truth": intent_data.get("contains_evaluator_truth"),
        "api_requests": intent_data.get("api_requests"),
        "r4_protocol_semantic_record_count": sum(
            row.get("map_input_mode") == "road_feature_protocol_v1" for row in intent_data["records"]
        ),
    }
    result["task_rows"] = all_rows
    result["routeable_task_ids_by_seed"] = {
        str(seed): [
            row["task_id"] for row in all_rows
            if row["seed"] == seed and row.get("route_chain_reachable") is True
        ]
        for seed in range(10)
    }
    result["status"] = (
        "offline_preflight_passed_no_model_requests"
        if shared_inputs["all_seed_map_order_and_scenario_comparisons_passed"]
        and result["preflight_error_count"] == 0
        and all(row.get("compact_map_equivalence_passed") in (None, True) for row in all_rows)
        and all(row.get("evaluator_truth_absent_from_prompt") in (None, True) for row in all_rows)
        else "offline_preflight_has_blockers_no_model_requests"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-root", type=Path, default=DEFAULT_SEED_ROOT)
    parser.add_argument("--pure-llm-root", type=Path, default=DEFAULT_PURE_LLM_ROOT)
    parser.add_argument("--intent-root", type=Path, default=DEFAULT_INTENT_ROOT)
    parser.add_argument("--intent-artifact", type=Path, default=DEFAULT_INTENT_ARTIFACT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit("refusing_to_overwrite_existing_preflight")
    result = audit(args.seed_root, args.pure_llm_root, args.intent_root, args.intent_artifact)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "index.html").write_text(_render_html(result), encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "status", "task_count", "order_task_count", "formal_scenario_count",
        "route_chain_reachable_count", "expected_no_path_count", "endpoint_rejected_count",
        "intent_source_integrity_failed_count", "intent_resolution_failed_count", "preflight_error_count",
        "minimum_stepwise_model_request_lower_bound",
        "minimum_initial_prompt_bytes_repeated_floor",
        "intent_resolution_failed_count",
        "minimum_action_lower_bound_stats_on_reachable_tasks",
        "initial_prompt_byte_stats_on_reachable_tasks",
        "initial_schema_byte_stats_on_reachable_tasks",
        "shared_input_audit",
    )}, ensure_ascii=False, indent=2))
    print(f"output_dir={args.output_dir}")
    return 0 if result["status"] == "offline_preflight_passed_no_model_requests" else 2


if __name__ == "__main__":
    raise SystemExit(main())
