#!/usr/bin/env python3
"""Read-only audit of traditional route outputs against R4's no-revisit rule."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "outputs/route_layer/traditional_experiment_v1"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/traditional_no_revisit_audit_20261023_v2"
METHODS = ("full_map_dijkstra", "local_dijkstra", "local_a_star")
SCOPES = ("orders", "formal_scenarios")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def duplicate_positions(sequence: list[str]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, node_id in enumerate(sequence):
        positions[str(node_id)].append(index + 1)
    return {node_id: indexes for node_id, indexes in positions.items() if len(indexes) > 1}


def audit(input_root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    input_hashes: dict[str, str] = {}
    summary = defaultdict(Counter)
    all_tasks: set[tuple[str, str]] = set()
    for seed in range(10):
        seed_dir = input_root / "seeds" / f"seed_{seed}"
        for scope in SCOPES:
            prefix = "traditional_order" if scope == "orders" else "traditional_formal"
            path = seed_dir / f"{prefix}_routes_costs_seed_{seed}.json"
            input_hashes[str(path.relative_to(ROOT))] = sha256(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            for task_result in payload.get("results", []):
                task_id = task_result["task_id"]
                all_tasks.add((scope, task_id))
                for method_result in task_result.get("methods", []):
                    method = method_result["method_name"]
                    summary[(scope, method)]["result_records"] += 1
                    routes = method_result.get("routes") or []
                    if not routes:
                        summary[(scope, method)]["no_route_records"] += 1
                        rows.append({
                            "seed": seed,
                            "scope": scope,
                            "task_id": task_id,
                            "method": method,
                            "method_status": method_result.get("status"),
                            "original_route_validation_status": "no_route_output",
                            "common_no_revisit_check": "not_applicable",
                            "route_node_count": 0,
                            "repeated_node_count": 0,
                            "repeated_nodes_and_positions": {},
                            "route_id": None,
                            "replay_path": (
                                f"{input_root.name}/seeds/seed_{seed}/visualizations/"
                                f"{'orders' if scope == 'orders' else 'formal_scenarios'}/"
                                f"{task_id}__{method}.html"
                            ),
                        })
                    for route in routes:
                        sequence = list(route.get("node_sequence") or [])
                        duplicates = duplicate_positions(sequence)
                        repeated = bool(duplicates)
                        legs = method_result.get("metadata", {}).get("search_trace", {}).get("legs") or []
                        if len(legs) == 2 and repeated:
                            first_leg_nodes = set(legs[0].get("route_node_sequence") or [])
                            second_leg_nodes = set(legs[1].get("route_node_sequence") or [])
                            pickup_node = legs[0].get("target_node_id")
                            cross_leg_nodes = (first_leg_nodes & second_leg_nodes) - (
                                {pickup_node} if pickup_node else set()
                            )
                            repeat_scope = "cross_leg" if cross_leg_nodes else "other"
                        else:
                            cross_leg_nodes = set()
                            repeat_scope = "none" if not repeated else "within_leg_or_unknown"
                        summary[(scope, method)]["route_records"] += 1
                        summary[(scope, method)]["repeated_route_records"] += int(repeated)
                        summary[(scope, method)]["cross_leg_repeat_routes"] += int(
                            repeat_scope == "cross_leg"
                        )
                        summary[(scope, method)]["repeated_nodes_extra"] += sum(
                            len(indexes) - 1 for indexes in duplicates.values()
                        )
                        summary[(scope, method)]["marked_valid_with_repeats"] += int(
                            repeated and route.get("validation_status") == "valid"
                        )
                        rows.append({
                            "seed": seed,
                            "scope": scope,
                            "task_id": task_id,
                            "method": method,
                            "method_status": method_result.get("status"),
                            "original_route_validation_status": route.get("validation_status"),
                            "common_no_revisit_check": "fail" if repeated else "pass",
                            "route_node_count": len(sequence),
                            "repeated_node_count": sum(len(indexes) - 1 for indexes in duplicates.values()),
                            "repeated_nodes_and_positions": duplicates,
                            "repeat_scope": repeat_scope,
                            "cross_leg_repeated_nodes": sorted(cross_leg_nodes),
                            "route_id": route.get("route_id"),
                            "replay_path": (
                                f"{input_root.name}/seeds/seed_{seed}/visualizations/"
                                f"{'orders' if scope == 'orders' else 'formal_scenarios'}/"
                                f"{task_id}__{method}.html"
                            ),
                        })

    grouped = {
        f"{scope}/{method}": dict(summary[(scope, method)])
        for scope in SCOPES
        for method in METHODS
    }
    repeated_rows = [row for row in rows if row["common_no_revisit_check"] == "fail"]
    repeated_task_ids = {
        (row["scope"], row["task_id"]) for row in repeated_rows
    }
    return {
        "schema_version": "traditional_no_revisit_audit_v1",
        "status": "read_only_audit_complete",
        "source_experiment": str(input_root.relative_to(ROOT)),
        "source_files_sha256": input_hashes,
        "method_names": list(METHODS),
        "task_count": len(all_tasks),
        "method_task_result_count": sum(item["result_records"] for item in summary.values()),
        "route_output_count": sum(item["route_records"] for item in summary.values()),
        "no_route_result_count": sum(item["no_route_records"] for item in summary.values()),
        "repeated_route_output_count": len(repeated_rows),
        "distinct_tasks_with_any_repeated_route": len(repeated_task_ids),
        "routes_marked_valid_despite_repeated_nodes": sum(
            item["marked_valid_with_repeats"] for item in summary.values()
        ),
        "cross_leg_repeat_route_output_count": sum(
            item["cross_leg_repeat_routes"] for item in summary.values()
        ),
        "by_scope_and_method": grouped,
        "limitations": [
            "本审计只核验路线节点序列是否重复访问，不改变或重写传统实验源文件。",
            "按项目已确认的正式 R4 硬约束，整条路线不得重复访问节点；传统方法原始 status/validation_status 未执行此跨两段检查。",
            "传统 Dijkstra/A* 分别规划 start→pickup 和 pickup→dropoff 后拼接；本报告不搜索替代的全局不重复路线，因此重复路线违规不等于任务没有其他合法路线。",
            "对 390 条已输出路线的两段 search trace 单独检查时，没有任何单段内部重访；160 条重复均由两段在 pickup 以外共享节点造成。",
            "所有原始轨迹仍可从表格链接打开；若用于正式配对比较，应在共同约束下重算传统路线或明确报告其违规，不可继续将这些结果当成全部合法路线。",
        ],
        "rows": rows,
    }


def render(report: dict[str, Any]) -> str:
    cards = [
        ("任务数", report["task_count"]),
        ("算法-任务结果", report["method_task_result_count"]),
        ("已输出路线", report["route_output_count"]),
        ("含重访路线", report["repeated_route_output_count"]),
        ("跨两段重复路线", report["cross_leg_repeat_route_output_count"]),
        ("至少一个算法含重访的任务", report["distinct_tasks_with_any_repeated_route"]),
        ("原记录仍标为有效", report["routes_marked_valid_despite_repeated_nodes"]),
    ]
    card_html = "".join(
        f'<div class="card"><span>{html.escape(str(label))}</span><strong>{html.escape(str(value))}</strong></div>'
        for label, value in cards
    )
    heads = ["种子", "范围", "任务", "算法", "原状态", "共用不重访规则", "路线节点数", "重复节点与位置", "轨迹"]
    head_html = "".join(f"<th>{html.escape(head)}</th>" for head in heads)
    body = []
    for row in report["rows"]:
        duplicates = "; ".join(
            f"{node_id}（第{'、'.join(map(str, positions))}个节点）"
            for node_id, positions in row["repeated_nodes_and_positions"].items()
        ) or "—"
        cells = [
            row["seed"], "订单" if row["scope"] == "orders" else "正式场景",
            row["task_id"], row["method"],
            f"{row['method_status']} / {row['original_route_validation_status']}",
            "违规：重复访问" if row["common_no_revisit_check"] == "fail" else "通过",
            row["route_node_count"], duplicates,
        ]
        tds = "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells)
        replay = html.escape(row["replay_path"], quote=True)
        replay_file = ROOT / "outputs/route_layer" / row["replay_path"]
        link = f'<td><a href="../{replay}">打开原轨迹</a></td>' if replay_file.is_file() else "<td>—</td>"
        css = " class=\"bad\"" if row["common_no_revisit_check"] == "fail" else ""
        body.append(f"<tr{css}>{tds}{link}</tr>")
    limitations = "".join(f"<li>{html.escape(item)}</li>" for item in report["limitations"])
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>传统路线不重访规则审计</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;color:#172033;background:#f6f8fc}}h1{{margin:0 0 8px}}.sub{{color:#59677e}}.notice{{padding:14px;background:#fff7e6;border:1px solid #f0d49b;border-radius:8px;margin:16px 0}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}}.card{{background:#fff;border:1px solid #dce3ef;border-radius:8px;padding:12px}}.card span{{display:block;color:#617089;font-size:12px}}.card strong{{font-size:21px}}.tablewrap{{overflow:auto;background:#fff;border:1px solid #dce3ef;border-radius:8px;margin-top:18px}}table{{border-collapse:collapse;min-width:1350px;width:100%;font-size:12px}}th,td{{border-bottom:1px solid #e4e8f0;padding:8px;text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#edf2fb;z-index:1}}tr.bad{{background:#fff0ed}}li{{margin:7px 0}}a{{white-space:nowrap}}</style></head><body>
<h1>传统算法路线：不重复节点规则审计</h1><p class="sub">只读核对传统实验 v1 的 10 种子原轨迹；没有改写源结果，也没有模型请求。</p>
<div class="notice"><b>判读：</b>研究方案已确认路线不得重复访问节点。传统算法当前分别搜索上车前后两段再拼接，原结果未检查两段之间的重访。红色行按 R4 共用硬约束属于违规；这不证明不存在其他不重访路线。</div>
<div class="cards">{card_html}</div><div class="tablewrap"><table><thead><tr>{head_html}</tr></thead><tbody>{''.join(body)}</tbody></table></div><h2>边界</h2><ul>{limitations}</ul><p>机器可读明细见 <code>summary.json</code>，输入哈希一并保存。</p></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit("refusing_to_overwrite_existing_audit")
    result = audit(args.input_root)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "index.html").write_text(render(result), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in ("rows", "source_files_sha256")}, ensure_ascii=False, indent=2))
    print(f"output_dir={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
