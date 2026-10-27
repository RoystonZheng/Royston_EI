#!/usr/bin/env python3
"""Offline audit of a one-request, full-route R4 candidate on frozen tasks.

This is an input/schema budget preflight only. It sends no model requests and
does not claim that a model can produce complete legal routes in one response.
"""

from __future__ import annotations

import argparse
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
from route_experiment.models import PackageConfig, VehicleOrderTask
from route_experiment.r4_compact_route import (
    build_r4_compact_route_messages,
    make_r4_compact_route_constraint,
)
from route_experiment.r4_stepwise import (
    _compact_map_from_package,
    audit_r4_stepwise_map_equivalence,
)
from route_experiment.routing import (
    build_route_input_package,
    route_chain_reachability,
    run_traditional_route,
)
from run_r4_codex_formal_checkpoint import load_map
from run_route_r4_smoke import _road_feature_protocol, _validate_protocol_references
from preflight_r4_codex_all_frozen_tasks import (
    DEFAULT_INTENT_ARTIFACT,
    DEFAULT_INTENT_ROOT,
    DEFAULT_OUTPUT as DEFAULT_STEPWISE_PREFLIGHT,
    DEFAULT_PURE_LLM_ROOT,
    DEFAULT_SEED_ROOT,
    _feature_context,
    _task_from_raw,
    audit as audit_stepwise,
    read_json,
)


DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_codex_full_route_candidate_preflight_20261023_v5"
MAX_ROUTE_EDGES = 512


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * p)]


def _raw_sources(seed_root: Path, seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
    seed_dir = seed_root / f"seed_{seed}"
    orders = read_json(seed_dir / f"order_tasks_seed_{seed}.json")
    scenarios = read_json(seed_dir / f"formal_scenarios_seed_{seed}.json")
    order_by_id = {task["task_id"]: task for task in orders["tasks"]}
    scenario_by_id = {}
    for scenario in scenarios["scenarios"]:
        for task in scenario["tasks"]:
            scenario_by_id[task["task_id"]] = (scenario, task)
    return order_by_id, scenario_by_id


def audit_candidate(
    seed_root: Path,
    pure_llm_root: Path,
    intent_root: Path,
    intent_artifact: Path,
    stepwise_preflight: Path,
) -> dict[str, Any]:
    baseline = read_json(stepwise_preflight / "summary.json")
    if baseline.get("status") != "offline_preflight_passed_no_model_requests":
        raise RuntimeError("stepwise_frozen_preflight_not_passed")
    if baseline.get("task_count") != 150 or baseline.get("route_chain_reachable_count") != 129:
        raise RuntimeError("stepwise_frozen_task_scope_changed")

    # Re-read the frozen sources and re-run the hash/intent truth gates.
    stepwise_audit = audit_stepwise(seed_root, pure_llm_root, intent_root, intent_artifact)
    if stepwise_audit.get("status") != "offline_preflight_passed_no_model_requests":
        raise RuntimeError("frozen_input_or_intent_audit_failed")

    intent_data = read_json(intent_artifact)
    intent_records = {
        (int(item["seed"]), item["task_id"], item["map_input_mode"]): item
        for item in intent_data["records"]
    }
    rows_by_seed = {
        seed: [row for row in stepwise_audit["task_rows"] if row["seed"] == seed]
        for seed in range(10)
    }
    result_rows = []
    map_audits = []
    no_model_requests = True

    for seed in range(10):
        seed_dir = seed_root / f"seed_{seed}"
        map_path = seed_dir / f"map_100x100_seed_{seed}.json"
        road_map = load_map(map_path)
        order_by_id, scenario_by_id = _raw_sources(seed_root, seed)
        protocol_cache: dict[str, Any] = {}
        for row in rows_by_seed[seed]:
            if row.get("route_chain_reachable") is not True:
                continue
            task_id = row["task_id"]
            if row["sample_group"] == "orders":
                raw = order_by_id[task_id]
                blocked_edge_ids: tuple[str, ...] = ()
            else:
                scenario, raw = scenario_by_id[task_id]
                blocked_edge_ids = tuple(scenario.get("blocked_edge_ids", []))

            task = _task_from_raw(raw, row["pickup_node_id"], row["dropoff_node_id"])
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
            if not reachability["chain_reachable"]:
                raise RuntimeError(f"reachable_task_changed_during_candidate_audit:{seed}:{task_id}")
            reference = run_traditional_route(
                road_map,
                task,
                "local_dijkstra",
                package_config=PackageConfig(),
                blocked_edge_ids=blocked_edge_ids,
            )
            reference_route = reference.routes[0] if reference.routes else None
            reference_edge_count = (
                len(reference_route.edge_sequence) if reference_route is not None else None
            )
            reference_route_is_simple = (
                len(reference_route.node_sequence) == len(set(reference_route.node_sequence))
                if reference_route is not None
                else None
            )

            route_intent = None
            feature_context = None
            if row.get("route_feature_context"):
                intent_record = intent_records[(seed, task_id, "road_feature_protocol_v1")]
                if intent_record.get("uses_evaluator_truth") is not False:
                    raise RuntimeError(f"semantic_intent_truth_gate_failed:{seed}:{task_id}")
                route_intent = intent_record.get("route_intent")
                if not route_intent:
                    raise RuntimeError(f"semantic_route_intent_missing:{seed}:{task_id}")
                protocol = protocol_cache.get(task_id)
                if protocol is None:
                    protocol = _road_feature_protocol(road_map, task)
                    protocol.context["random_seed"] = seed
                    protocol_audit = _validate_protocol_references(protocol)
                    if not protocol_audit.get("passed"):
                        raise RuntimeError(f"road_feature_protocol_audit_failed:{seed}:{task_id}")
                    protocol_cache[task_id] = protocol
                feature_context = _feature_context(protocol, route_intent)

            messages = build_r4_compact_route_messages(
                package,
                task.start_node_id,
                task.pickup_node_id,
                task.dropoff_node_id,
                route_intent=route_intent,
                route_feature_context=feature_context,
            )
            rendered = CodexCliRouteStepClient._render_prompt(messages)
            payload = json.loads(messages[1]["content"])
            compact_map = _compact_map_from_package(
                package, task.start_node_id, task.pickup_node_id, task.dropoff_node_id
            )
            map_audit = audit_r4_stepwise_map_equivalence(package, compact_map)
            truth_absent = not any(
                marker in rendered
                for marker in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")
            )
            feature_present = ("i" in payload) == (feature_context is not None)
            if not map_audit["passed"]:
                raise RuntimeError(f"candidate_map_equivalence_failed:{seed}:{task_id}")
            if not truth_absent:
                raise RuntimeError(f"evaluator_truth_leaked_to_candidate_prompt:{seed}:{task_id}")
            if not feature_present:
                raise RuntimeError(f"candidate_feature_context_presence_mismatch:{seed}:{task_id}")
            map_audits.append(map_audit)

            schema = make_r4_compact_route_constraint(MAX_ROUTE_EDGES)
            schema_bytes = len(
                json.dumps(schema.schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            prompt_bytes = len(rendered.encode("utf-8"))
            result_rows.append({
                "seed": seed,
                "sample_group": row["sample_group"],
                "scenario_type": row.get("scenario_type"),
                "task_id": task_id,
                "route_feature_context_included": feature_context is not None,
                "route_intent_included": route_intent is not None,
                "minimum_route_actions_lower_bound": row["minimum_route_actions_lower_bound"],
                "route_action_limit": MAX_ROUTE_EDGES,
                "local_dijkstra_reference_status": reference.status,
                "local_dijkstra_reference_edge_count": reference_edge_count,
                "local_dijkstra_reference_route_is_simple": reference_route_is_simple,
                "reference_route_fits_output_limit": (
                    reference_edge_count is not None and reference_edge_count <= MAX_ROUTE_EDGES
                ),
                "prompt_bytes": prompt_bytes,
                "schema_bytes": schema_bytes,
                "map_equivalence_passed": map_audit["passed"],
                "evaluator_truth_absent": truth_absent,
                "api_requests_assumed": 1,
                "model_request_sent": False,
            })

    prompt_sizes = [row["prompt_bytes"] for row in result_rows]
    schema_sizes = [row["schema_bytes"] for row in result_rows]
    reference_edges = [
        row["local_dijkstra_reference_edge_count"]
        for row in result_rows
        if row["local_dijkstra_reference_edge_count"] is not None
    ]
    stepwise_floor = int(baseline["minimum_stepwise_model_request_lower_bound"])
    one_shot_assumed_calls = len(result_rows)
    summary = {
        "schema_version": "r4_codex_full_route_candidate_preflight_v1",
        "status": "offline_candidate_preflight_passed_no_model_requests",
        "candidate_status": "UNVALIDATED_METHOD_CANDIDATE",
        "formal_r4_started": False,
        "model_requests_sent": 0,
        "frozen_task_count": 150,
        "chain_reachable_task_count": len(result_rows),
        "simple_route_reference_witness_count": sum(
            row["local_dijkstra_reference_route_is_simple"] is True for row in result_rows
        ),
        "chain_reachable_tasks_without_simple_reference_witness": sum(
            row["local_dijkstra_reference_route_is_simple"] is False for row in result_rows
        ),
        "not_sent_task_count": 150 - len(result_rows),
        "one_request_per_chain_reachable_task_assumed_calls": one_shot_assumed_calls,
        "stepwise_minimum_request_lower_bound": stepwise_floor,
        "assumed_request_reduction_percent": round(
            (1 - one_shot_assumed_calls / stepwise_floor) * 100, 2
        ),
        "request_count_is_model_success_guarantee": False,
        "retry_policy": "zero retries; a failed/invalid full-route proposal remains failed",
        "fallback_policy": "no Dijkstra/A* route fallback",
        "map_order_frozen_audit_passed": stepwise_audit["shared_input_audit"][
            "all_seed_map_order_and_scenario_comparisons_passed"
        ],
        "semantic_route_feature_context_count": sum(
            row["route_feature_context_included"] for row in result_rows
        ),
        "semantic_route_intent_count": sum(row["route_intent_included"] for row in result_rows),
        "compact_map_edge_and_cost_equivalence_passed": all(
            result["passed"] for result in map_audits
        ),
        "evaluator_truth_absent_from_all_prompts": all(
            row["evaluator_truth_absent"] for row in result_rows
        ),
        "route_action_limit": MAX_ROUTE_EDGES,
        "local_dijkstra_reference_used_as_model_input": False,
        "local_dijkstra_reference_routes_completed": sum(
            row["local_dijkstra_reference_status"] == "completed" for row in result_rows
        ),
        "local_dijkstra_reference_routes_fit_512_edge_limit": sum(
            row["reference_route_fits_output_limit"] for row in result_rows
        ),
        "local_dijkstra_reference_routes_are_simple": sum(
            row["local_dijkstra_reference_route_is_simple"] is True for row in result_rows
        ),
        "local_dijkstra_reference_routes_repeat_nodes": sum(
            row["local_dijkstra_reference_route_is_simple"] is False for row in result_rows
        ),
        "local_dijkstra_reference_edge_count_stats": {
            "count": len(reference_edges),
            "min": min(reference_edges) if reference_edges else None,
            "median": sorted(reference_edges)[(len(reference_edges) - 1) // 2] if reference_edges else None,
            "p90": _percentile(reference_edges, 0.90),
            "max": max(reference_edges) if reference_edges else None,
        },
        "prompt_byte_stats": {
            "count": len(prompt_sizes),
            "min": min(prompt_sizes) if prompt_sizes else None,
            "median": sorted(prompt_sizes)[(len(prompt_sizes) - 1) // 2] if prompt_sizes else None,
            "p90": _percentile(prompt_sizes, 0.90),
            "max": max(prompt_sizes) if prompt_sizes else None,
            "sum": sum(prompt_sizes),
        },
        "schema_byte_stats": {
            "count": len(schema_sizes),
            "min": min(schema_sizes) if schema_sizes else None,
            "median": sorted(schema_sizes)[(len(schema_sizes) - 1) // 2] if schema_sizes else None,
            "p90": _percentile(schema_sizes, 0.90),
            "max": max(schema_sizes) if schema_sizes else None,
        },
        "stepwise_reference": {
            "path": str((stepwise_preflight / "summary.json").relative_to(ROOT)),
            "minimum_model_request_lower_bound": stepwise_floor,
            "minimum_route_actions": baseline["minimum_action_lower_bound_stats_on_reachable_tasks"],
            "repeated_initial_prompt_bytes_floor": baseline["minimum_initial_prompt_bytes_repeated_floor"],
        },
        "limitations": [
            "本报告只验证冻结输入、语义上下文、紧凑地图等价、真值隔离和本地硬校验接口；没有调用模型。",
            "129 次是每条可达任务恰好一次请求的调用假设，不是模型能在一次响应中完成合法路线的证据。",
            "整路线方案与逐步方案的方法不同，不能将候选预检写成正式 R4 结果。",
            "输出方向串即使附带逐步自检码，也必须由本地程序逐边验证；任何违规都拒绝整条路线，且不回退到 Dijkstra/A*。",
            "Dijkstra 只在离线审计中测量 512 边输出上限的参考长度，不进入模型提示、不作为路线回退；参考长度不等同于满足无重访规则的证明。",
            "129 条任务仅通过分段有向可达门禁；其中 73 条传统 local Dijkstra 拼接参考路线本身不重访，可作为路线存在的见证。另 56 条当前参考路线跨腿重复，尚未搜索替代的不重访路线；这不证明它们无路。",
            "提示字节数不换算为 token 或费用；实际费用取决于调用后端、缓存与实际用量。",
            "10 个非法端点、10 个预设不可达任务和 1 个语义解析失败任务不发路线请求，失败分类保留在冻结任务审计中。",
        ],
        "task_rows": result_rows,
    }
    return summary


def _render(summary: dict[str, Any]) -> str:
    prompt = summary["prompt_byte_stats"]
    cards = [
        ("冻结任务", summary["frozen_task_count"]),
        ("分段有向可达任务", summary["chain_reachable_task_count"]),
        ("不重访参考路线见证", summary["simple_route_reference_witness_count"]),
        ("模型调用（本次）", summary["model_requests_sent"]),
        ("整路线一次请求假设", summary["one_request_per_chain_reachable_task_assumed_calls"]),
        ("逐步方案最低请求数", summary["stepwise_minimum_request_lower_bound"]),
        ("理论调用数减少", f"{summary['assumed_request_reduction_percent']}%"),
        ("语义特征上下文任务", summary["semantic_route_feature_context_count"]),
        ("最大提示字节", prompt["max"]),
    ]
    card_html = "".join(
        f'<div class="card"><span>{html.escape(str(label))}</span><strong>{html.escape(str(value))}</strong></div>'
        for label, value in cards
    )
    headers = ["种子", "任务", "样本组", "道路特征输入", "最短动作下界", "Dijkstra参考边数", "参考路线无重访", "提示字节", "Schema 字节", "门禁"]
    head_html = "".join(f"<th>{html.escape(label)}</th>" for label in headers)
    rows = []
    for row in summary["task_rows"]:
        group = "订单" if row["sample_group"] == "orders" else f"正式场景/{row.get('scenario_type') or ''}"
        gates = "通过：地图等价、无真值泄漏"
        feature = "已注入" if row["route_feature_context_included"] else "精确端点"
        cells = [
            row["seed"], row["task_id"], group, feature,
            row["minimum_route_actions_lower_bound"], row["local_dijkstra_reference_edge_count"],
            ("是" if row["local_dijkstra_reference_route_is_simple"] else "否；R4 可改路"),
            row["prompt_bytes"], row["schema_bytes"], gates,
        ]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells) + "</tr>")
    limitations = "".join(f"<li>{html.escape(text)}</li>" for text in summary["limitations"])
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>R4 整路线候选离线预检</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;color:#172033;background:#f6f8fc}}h1{{margin:0 0 8px}}.sub{{color:#546179}}.notice{{padding:14px;background:#fff7e6;border:1px solid #f3d18b;border-radius:8px;margin:16px 0}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}}.card{{background:white;border:1px solid #dae1ed;border-radius:8px;padding:12px}}.card span{{display:block;color:#617089;font-size:12px}}.card strong{{font-size:21px}}.tablewrap{{overflow:auto;background:white;border:1px solid #dce3ef;border-radius:8px;margin-top:18px}}table{{border-collapse:collapse;min-width:1050px;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #e4e8f0;padding:8px;text-align:left;white-space:nowrap}}th{{position:sticky;top:0;background:#edf2fb;z-index:1}}li{{margin:7px 0}}</style></head><body>
<h1>R4 整路线候选：十种子离线预检</h1><p class="sub">状态：候选方案本地门禁通过；模型请求 0；正式 R4 未启动。</p>
<div class="notice"><b>读数边界：</b>“129 次”是假设每个可达任务一条整路线请求、零重试时的调用数。模型是否能一次给出合法完整路线仍未知。非法路线由本地逐边校验并整条拒绝，不使用传统算法兜底。</div>
<div class="cards">{card_html}</div><h2>逐任务预检</h2><div class="tablewrap"><table><thead><tr>{head_html}</tr></thead><tbody>{''.join(rows)}</tbody></table></div><h2>限制</h2><ul>{limitations}</ul></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-root", type=Path, default=DEFAULT_SEED_ROOT)
    parser.add_argument("--pure-llm-root", type=Path, default=DEFAULT_PURE_LLM_ROOT)
    parser.add_argument("--intent-root", type=Path, default=DEFAULT_INTENT_ROOT)
    parser.add_argument("--intent-artifact", type=Path, default=DEFAULT_INTENT_ARTIFACT)
    parser.add_argument("--stepwise-preflight", type=Path, default=DEFAULT_STEPWISE_PREFLIGHT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit("refusing_to_overwrite_existing_candidate_preflight")
    result = audit_candidate(
        args.seed_root,
        args.pure_llm_root,
        args.intent_root,
        args.intent_artifact,
        args.stepwise_preflight,
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "index.html").write_text(_render(result), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "task_rows"}, ensure_ascii=False, indent=2))
    print(f"output_dir={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
