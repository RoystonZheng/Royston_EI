#!/usr/bin/env python3
"""Replay a deterministic relative-direction resolver on frozen Qwen outputs.

No model is loaded or called. Existing model-visible query/result files are
used for candidate selection; evaluator truth is read only to score afterward.
The output is a development audit, not an independent formal experiment.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.relative_grounding import (
    resolve_landmark_near_candidate,
    resolve_landmark_relative_candidate,
)

DEFAULT_SOURCE = ROOT / "outputs/route_layer/r4_qwen_intent_local_batch_v3"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_relative_grounding_postprocess_dev_v1"
MODES = ("road_feature_protocol_v1", "graph_only")


def _exact_mcnemar_pvalue(feature_only: int, graph_only: int) -> float:
    discordant = feature_only + graph_only
    if not discordant:
        return 1.0
    tail = min(feature_only, graph_only)
    return min(
        1.0,
        2.0 * sum(math.comb(discordant, k) for k in range(tail + 1))
        / (2**discordant),
    )


def _load_case(result_path: Path) -> dict[str, Any]:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    mode = result["map_input_mode"]
    seed = int(result["seed"])
    task_id = result["task_id"]
    query_path = result_path.with_name("query.json")
    query_file = json.loads(query_path.read_text(encoding="utf-8"))
    model_query = result["model_query"]
    if query_file != model_query:
        raise RuntimeError("saved_model_query_does_not_match_result_query")
    parsed = result.get("parsed_intent") or {}
    refs = result.get("candidate_ref_resolution") or {}
    pickup_ref = parsed.get("pickup_candidate_ref")
    dropoff_ref = parsed.get("dropoff_candidate_ref")
    raw_pickup = (refs.get("pickup") or {}).get(pickup_ref)
    raw_dropoff = (refs.get("dropoff") or {}).get(dropoff_ref)
    raw_valid = bool(result.get("validation", {}).get("valid"))
    raw_validation_reason = (result.get("validation") or {}).get("reason")
    task_type = result.get("task_type")
    mode = result.get("map_input_mode")

    # Keep the model-selected anchor. This development replay only maps that
    # anchor plus an explicit text relation to a legal public candidate.
    pickup_grounding = None
    if task_type == "semantic_landmark_nearby":
        pickup_grounding = resolve_landmark_near_candidate(parsed, model_query, "pickup")
        chosen_pickup_ref = (
            pickup_grounding.get("candidate_ref")
            if pickup_grounding.get("status") == "resolved"
            else None
        )
        pickup_resolved = chosen_pickup_ref in (refs.get("pickup") or {})
    elif task_type == "semantic_landmark_relative":
        pickup_grounding = resolve_landmark_relative_candidate(parsed, model_query)
        chosen_pickup_ref = (
            pickup_grounding.get("candidate_ref")
            if pickup_grounding.get("status") == "resolved"
            else None
        )
        pickup_resolved = chosen_pickup_ref in (refs.get("pickup") or {})
    else:
        chosen_pickup_ref = pickup_ref
        pickup_resolved = chosen_pickup_ref in (refs.get("pickup") or {})

    # Every semantic task in this frozen sample asks for a landmark-nearby
    # dropoff. An unresolved model anchor fails closed.
    dropoff_grounding = resolve_landmark_near_candidate(parsed, model_query, "dropoff")
    chosen_dropoff_ref = (
        dropoff_grounding.get("candidate_ref")
        if dropoff_grounding.get("status") == "resolved"
        else None
    )
    dropoff_resolved = chosen_dropoff_ref in (refs.get("dropoff") or {})
    final_pickup = (refs.get("pickup") or {}).get(chosen_pickup_ref)
    final_dropoff = (refs.get("dropoff") or {}).get(chosen_dropoff_ref)

    # Road-feature pickup keeps the original relation validator authoritative.
    # Graph-only feature tasks retain their legal model candidate, and its
    # semantic mismatch is measured against truth afterward.
    if task_type == "semantic_road_feature_position":
        pickup_valid = raw_valid
    else:
        pickup_valid = pickup_resolved
    post_valid = bool(pickup_valid and dropoff_resolved)
    if post_valid:
        post_valid_reason = "resolved_from_model_anchor_and_public_candidates"
    elif task_type == "semantic_road_feature_position" and not raw_valid:
        post_valid_reason = raw_validation_reason or "road_feature_relation_validator_rejected"
    elif pickup_grounding and pickup_grounding.get("status") != "resolved":
        post_valid_reason = pickup_grounding.get("reason") or "pickup_grounding_failed"
    elif dropoff_grounding.get("status") != "resolved":
        post_valid_reason = dropoff_grounding.get("reason") or "dropoff_grounding_failed"
    else:
        post_valid_reason = "candidate_resolution_failed"

    truth = result.get("evaluation_after_inference") or {}
    true_pickup = truth.get("true_pickup_node_id")
    true_dropoff = truth.get("true_dropoff_node_id")
    # Score endpoints independently, then require a valid pair for full exactness.
    pickup_exact = bool(final_pickup and final_pickup == true_pickup)
    dropoff_exact = bool(final_dropoff and final_dropoff == true_dropoff)
    post_exact = bool(post_valid and pickup_exact and dropoff_exact)
    raw_exact = bool(truth.get("both_endpoints_exact_match"))
    if task_type == "semantic_landmark_nearby":
        pickup_relation = "near"
        pickup_side = "unknown"
    elif task_type == "semantic_landmark_relative":
        pickup_relation = "relative_to"
        pickup_side = (pickup_grounding or {}).get("direction", "unknown")
    else:
        pickup_relation = parsed.get("relation", "unknown")
        pickup_side = parsed.get("side", "unknown")
    route_intent_record = {
        "seed": seed,
        "map_id": result.get("map_id"),
        "task_id": task_id,
        "task_type": task_type,
        "map_input_mode": mode,
        "status": "resolved" if post_valid else "unresolved",
        "resolution_reason": post_valid_reason,
        "route_eligible": post_valid,
        "start_node_id": model_query.get("start_node_id"),
        "pickup_node_id": final_pickup,
        "dropoff_node_id": final_dropoff,
        "route_intent": {
            "pickup_anchor_refs": [parsed.get("pickup_anchor_ref")]
            if parsed.get("pickup_anchor_ref") else [],
            "pickup_relation": pickup_relation,
            "pickup_side": pickup_side,
            "dropoff_anchor_refs": [parsed.get("dropoff_anchor_ref")]
            if parsed.get("dropoff_anchor_ref") else [],
            "dropoff_relation": "near",
            "feature_evidence_refs": parsed.get("feature_evidence_refs", []),
        },
        "source_query_sha256": hashlib.sha256(
            json.dumps(model_query, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "uses_evaluator_truth": False,
    }
    return {
        "seed": seed,
        "task_id": task_id,
        "task_type": task_type,
        "mode": mode,
        "raw_valid": raw_valid,
        "raw_validation_reason": raw_validation_reason,
        "raw_exact": raw_exact,
        "post_valid": post_valid,
        "post_valid_reason": post_valid_reason,
        "post_pickup_exact": pickup_exact,
        "post_dropoff_exact": dropoff_exact,
        "post_exact": post_exact,
        "raw_pickup_ref": pickup_ref,
        "resolved_pickup_ref": chosen_pickup_ref,
        "raw_dropoff_ref": dropoff_ref,
        "resolved_dropoff_ref": chosen_dropoff_ref,
        "raw_pickup_node": raw_pickup,
        "resolved_pickup_node": final_pickup,
        "raw_dropoff_node": raw_dropoff,
        "resolved_dropoff_node": final_dropoff,
        "true_pickup_node": true_pickup,
        "true_dropoff_node": true_dropoff,
        "pickup_grounding": pickup_grounding,
        "dropoff_grounding": dropoff_grounding,
        "route_intent_record": route_intent_record,
        "truth_read_after_candidate_selection": bool(truth.get("truth_loaded_after_generation")),
        "source_prompt_version": result.get("prompt_version"),
        "source_query_sha256": hashlib.sha256(
            json.dumps(model_query, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "source_query_file_sha256": hashlib.sha256(query_path.read_bytes()).hexdigest(),
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, Any] = {}
    for mode in MODES:
        mode_rows = [row for row in rows if row["mode"] == mode]
        by_type: dict[str, dict[str, int]] = {}
        for task_type in sorted({row["task_type"] for row in mode_rows}):
            selected = [row for row in mode_rows if row["task_type"] == task_type]
            by_type[task_type] = {
                "count": len(selected),
                "raw_exact": sum(row["raw_exact"] for row in selected),
                "postprocess_pickup_exact": sum(row["post_pickup_exact"] for row in selected),
                "postprocess_dropoff_exact": sum(row["post_dropoff_exact"] for row in selected),
                "postprocess_exact": sum(row["post_exact"] for row in selected),
                "postprocess_valid": sum(row["post_valid"] for row in selected),
            }
        by_mode[mode] = {
            "task_count": len(mode_rows),
            "raw_exact_match_count": sum(row["raw_exact"] for row in mode_rows),
            "postprocess_pickup_exact_count": sum(row["post_pickup_exact"] for row in mode_rows),
            "postprocess_dropoff_exact_count": sum(row["post_dropoff_exact"] for row in mode_rows),
            "postprocess_exact_match_count": sum(row["post_exact"] for row in mode_rows),
            "postprocess_valid_count": sum(row["post_valid"] for row in mode_rows),
            "by_task_type": by_type,
        }

    paired: dict[tuple[int, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        paired[(row["seed"], row["task_id"])][row["mode"]] = row
    feature_only = graph_only = both = neither = 0
    for pair in paired.values():
        a = pair["road_feature_protocol_v1"]["post_exact"]
        b = pair["graph_only"]["post_exact"]
        if a and b:
            both += 1
        elif a:
            feature_only += 1
        elif b:
            graph_only += 1
        else:
            neither += 1
    return {
        "schema_version": "r4_relative_grounding_postprocess_dev_summary_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "retrospective_local_postprocess_of_existing_qwen_outputs_no_route_generation",
        "api_requests": 0,
        "hosted_inference_requests": 0,
        "rows": len(rows),
        "paired_task_count": len(paired),
        "by_mode": by_mode,
        "paired_postprocess_exact_comparison": {
            "road_feature_only_exact": feature_only,
            "graph_only_only_exact": graph_only,
            "both_exact": both,
            "neither_exact": neither,
            "exact_two_sided_mcnemar_p_exploratory": _exact_mcnemar_pvalue(feature_only, graph_only),
        },
        "limitations": [
            "This is a post-hoc development replay on the same 30 semantic tasks already inspected; it is not an independent held-out evaluation.",
            "It changes endpoint resolution by using the model-selected, query-whitelisted landmark anchor and explicit Chinese nearby/cardinal-direction relations with public candidate geometry.",
            "Road-feature pickup resolution is not repaired: the original road-feature relation validator remains authoritative and its rejected sample remains a failure.",
            "The deterministic resolver covers only the explicit nearby/four-cardinal-direction templates represented in this synthetic task generator; it does not establish general natural-language understanding.",
            "No route was proposed, validated, costed, or energy-scored by this replay.",
        ],
    }


def _render_html(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    sections = []
    for mode in MODES:
        result = summary["by_mode"][mode]
        sections.append(
            f"<section><h2>{html.escape(mode)}</h2><p>原始精确命中：{result['raw_exact_match_count']}/{result['task_count']}；"
            f"关系规则后有效两端精确命中：{result['postprocess_exact_match_count']}/{result['task_count']}；"
            f"候选有效：{result['postprocess_valid_count']}/{result['task_count']}</p></section>"
        )
    body_rows = []
    for row in rows:
        pickup_g = row.get("pickup_grounding") or {}
        dropoff_g = row.get("dropoff_grounding") or {}
        body_rows.append(
            "<tr>"
            f"<td>{row['seed']}</td><td>{html.escape(row['task_id'])}</td>"
            f"<td>{html.escape(row['mode'])}</td><td>{html.escape(row['task_type'])}</td>"
            f"<td>{html.escape(str(row['raw_pickup_node']))}</td>"
            f"<td>{html.escape(str(row['resolved_pickup_node']))}</td>"
            f"<td>{html.escape(str(pickup_g.get('direction', pickup_g.get('relation', '—'))))}</td>"
            f"<td>{html.escape(str(row['raw_dropoff_node']))}</td>"
            f"<td>{html.escape(str(row['resolved_dropoff_node']))}</td>"
            f"<td>{html.escape(str(dropoff_g.get('relation', '—')))}</td>"
            f"<td>{html.escape(str(row['true_pickup_node']))}</td>"
            f"<td>{html.escape(str(row['true_dropoff_node']))}</td>"
            f"<td>{'是' if row['post_valid'] else '否'}：{html.escape(str(row['post_valid_reason']))}</td>"
            f"<td>{'是' if row['raw_exact'] else '否'}</td>"
            f"<td>{'是' if row['post_pickup_exact'] else '否'}</td>"
            f"<td>{'是' if row['post_dropoff_exact'] else '否'}</td>"
            f"<td>{'是' if row['post_exact'] else '否'}</td>"
            "</tr>"
        )
    paired = summary["paired_postprocess_exact_comparison"]
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>R4 相对方位 grounding 离线开发审计</title><style>
body{font:15px/1.55 system-ui,-apple-system,sans-serif;margin:28px;color:#1b2430;background:#f5f7fb}main{max-width:1500px;margin:auto}section,.note{background:white;padding:18px 22px;border:1px solid #dce3ee;border-radius:10px;margin:14px 0}h1{margin-bottom:5px}table{border-collapse:collapse;width:100%;background:white;font-size:13px}th,td{padding:8px;border-bottom:1px solid #e4e8ef;text-align:left}th{background:#edf2fc;position:sticky;top:0} .table-wrap{overflow:auto;max-height:75vh}.warning{color:#8a3c00;font-weight:600}
</style><main><h1>R4 相对方位端点解析：离线开发复核</h1>
<p class="warning">仅对既有 Qwen 输出做程序化后处理；没有新模型请求，也没有生成路线。不是独立留出集或正式路线实验结果。</p>
""" + "".join(sections) + f"""
<section><h2>配对结果</h2><p>道路特征输入独有命中 {paired['road_feature_only_exact']}；仅图结构独有命中 {paired['graph_only_only_exact']}；两种方式都命中 {paired['both_exact']}；两者均未命中 {paired['neither_exact']}。探索性双侧精确 McNemar p={paired['exact_two_sided_mcnemar_p_exploratory']:.6g}。</p><p>此统计使用同一批已检查任务，仅为开发诊断。</p></section>
<div class="note"><strong>处理规则：</strong>模型只负责从查询可见的地标条目中选择锚点；程序按原始文本明确写出的“附近”或东/西/南/北关系，在公开合法候选中按曼哈顿距离选点，距离相同时按冻结候选顺序裁决。道路特征上车点不被此回放覆盖，仍按原特征关系校验结果记录。</div>
<div class="table-wrap"><table><thead><tr><th>种子</th><th>任务</th><th>输入</th><th>类型</th><th>模型上车点</th><th>规则后上车点</th><th>上车关系</th><th>模型下车点</th><th>规则后下车点</th><th>下车关系</th><th>真值上车点（事后评分）</th><th>真值下车点（事后评分）</th><th>整体有效/原因</th><th>原始两端命中</th><th>规则后上车命中</th><th>规则后下车命中</th><th>规则后两端命中</th></tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>
</main></html>"""


def run(source: Path, output: Path) -> dict[str, Any]:
    summary_path = source / "summary.json"
    source_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if source_summary.get("completed_samples") != 60:
        raise RuntimeError("source_batch_not_complete_60_samples")
    input_audit = json.loads((source / "shared_input_audit.json").read_text(encoding="utf-8"))
    if not input_audit.get("passed"):
        raise RuntimeError("source_shared_input_audit_not_passed")
    rows = []
    for result_path in sorted(source.glob("seed_*/task-*/road_feature_protocol_v1/result.json")):
        rows.append(_load_case(result_path))
    for result_path in sorted(source.glob("seed_*/task-*/graph_only/result.json")):
        rows.append(_load_case(result_path))
    if len(rows) != 60:
        raise RuntimeError(f"source_row_count_expected_60_got_{len(rows)}")
    summary = _summarize(rows)
    summary["source"] = str(source.relative_to(ROOT))
    summary["source_summary_sha256"] = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    summary["source_shared_input_audit_passed"] = True
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "cases.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    route_intents = {
        "schema_version": "r4_qwen_resolved_route_intents_dev_v1",
        "scope": "retrospective_local_postprocess_same_30_semantic_tasks",
        "contains_evaluator_truth": False,
        "api_requests": 0,
        "records": [row["route_intent_record"] for row in rows],
    }
    (output / "route_intents.json").write_text(
        json.dumps(route_intents, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "index.html").write_text(_render_html(summary, rows), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run(args.source, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
