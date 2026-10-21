#!/usr/bin/env python3
"""Audit and summarize the frozen-input D5 Codex CLI repeated-run matrix."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import statistics
import sys
from collections import Counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dispatch_experiment.llm import CodexCliDirectAssignment  # noqa: E402
import run_nyc_tlc_codex_formal_batch as batch  # noqa: E402


OUT = batch.D5_DIR
SEEDS = list(range(1, 10))
METHODS = ["D0_greedy", "D1_optimizer", "D5_pure_llm_direct"]
METRICS = {
    "served_orders": "served_orders",
    "service_rate": "service_rate",
    "average_pickup_wait_sec": "average_pickup_wait_sec",
    "p95_pickup_wait_sec": "p95_pickup_wait_sec",
    "total_empty_distance_m": "total_empty_distance_m",
    "total_energy_kwh": "total_energy_kwh",
    "total_dispatch_rounds": "total_dispatch_rounds",
    "total_llm_calls": "total_llm_calls",
    "fallback_count": "fallback_count",
    "commitment_violations": "commitment_violations",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def stats(values: list[float]) -> tuple[float | None, float | None, float | None, float | None]:
    if not values:
        return None, None, None, None
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return mean, sd, min(values), max(values)


def fmt(value: float | None, decimals: int = 2) -> str:
    return "—" if value is None else f"{value:.{decimals}f}"


def config_key(row: dict[str, str]) -> tuple[str, str]:
    return row["case"], row["fleet_id"]


def build_pairing() -> list[dict[str, str]]:
    batch.write_pairing(SEEDS)
    with (OUT / "paired_results.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_payloads(rows: list[dict[str, str]]) -> dict[tuple[str, str, int, str], dict[str, Any]]:
    payloads: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for row in rows:
        path = ROOT / row["result_path"]
        if path.is_file():
            payloads[(row["case"], row["fleet_id"], int(row["seed"]), row["method"])] = load_json(path)
    return payloads


def metric_value(payload: dict[str, Any], name: str) -> float | None:
    value = payload.get("summary", {}).get(name)
    return float(value) if isinstance(value, (int, float)) else None


def write_metric_tables(payloads: dict[tuple[str, str, int, str], dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metric_rows: list[dict[str, Any]] = []
    delta_rows: list[dict[str, Any]] = []
    configs = sorted({(case, fleet) for case, fleet, _, _ in payloads})
    for case, fleet in configs:
        for method in METHODS:
            method_payloads = [payloads[(case, fleet, seed, method)] for seed in range(10)
                               if (case, fleet, seed, method) in payloads]
            row: dict[str, Any] = {"case": case, "fleet_id": fleet, "method": method,
                                   "n_complete_runs": len(method_payloads)}
            for column, key in METRICS.items():
                vals = [float(p["summary"][key]) for p in method_payloads
                        if isinstance(p.get("summary", {}).get(key), (int, float))]
                mean, sd, minimum, maximum = stats(vals)
                row[column + "_mean"] = mean
                row[column + "_sd"] = sd
                row[column + "_min"] = minimum
                row[column + "_max"] = maximum
            metric_rows.append(row)

        for baseline in ("D0_greedy", "D1_optimizer"):
            for column, key in METRICS.items():
                deltas = []
                for seed in range(10):
                    llm = payloads.get((case, fleet, seed, "D5_pure_llm_direct"), {}).get("summary", {}).get(key)
                    base = payloads.get((case, fleet, seed, baseline), {}).get("summary", {}).get(key)
                    if isinstance(llm, (int, float)) and isinstance(base, (int, float)):
                        deltas.append(float(llm) - float(base))
                mean, sd, minimum, maximum = stats(deltas)
                delta_rows.append({"case": case, "fleet_id": fleet, "comparison": f"D5-{baseline[:2]}",
                                   "metric": column, "n_pairs": len(deltas), "mean_delta": mean,
                                   "sd_delta": sd, "min_delta": minimum, "max_delta": maximum})

    fields = list(metric_rows[0])
    with (OUT / "method_metrics.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(metric_rows)
    with (OUT / "paired_deltas.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(delta_rows[0]))
        writer.writeheader()
        writer.writerows(delta_rows)
    return metric_rows, delta_rows


def audit_cli_traces() -> dict[str, Any]:
    traces = sorted((OUT / "seed_*").glob("*/**/codex_cli_traces/*.jsonl"))
    # pathlib's glob does not expand the first wildcard as a directory level.
    traces = sorted(OUT.glob("seed_*/**/codex_cli_traces/*.jsonl"))
    usage_calls = 0
    started_calls = 0
    tool_trace_files = []
    malformed_lines = 0
    for path in traces:
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                malformed_lines += 1
        audit = CodexCliDirectAssignment._audit_events(events)
        usage_calls += int(audit["turn_completed_usage_count"])
        started_calls += int(audit["turn_started_count"])
        if audit["tool_usage_detected"]:
            tool_trace_files.append(str(path.relative_to(ROOT)))
    return {"trace_file_count": len(traces), "turn_started_count": started_calls,
            "turn_completed_with_usage_count": usage_calls,
            "tool_event_trace_file_count": len(tool_trace_files),
            "tool_event_trace_files": tool_trace_files,
            "malformed_jsonl_lines": malformed_lines}


def write_combined_cost() -> dict[str, Any]:
    new = load_json(OUT / "codex_usage_cost_reconciliation.json")["aggregate"]
    old_path = batch.SOURCE_D5 / "codex_usage_cost_reconciliation_20261021.json"
    old = load_json(old_path)["aggregate"]
    new_runs = load_json(OUT / "codex_usage_cost_reconciliation.json")["runs"]
    old_obj = load_json(old_path)
    new_calls = sum(item["cost"]["call_count"] for item in new_runs)
    old_calls = sum(item["cost"]["call_count"] for item in old_obj["runs"])
    merged = {
        "cohort": "D5 Codex CLI v2 seed 0 plus repeated runs seed 1-9 on the same five frozen configurations",
        "run_count": int(new["run_count"] + old["run_count"]),
        "new_seed_1_to_9_run_count": int(new["run_count"]),
        "reused_seed_0_run_count": int(old["run_count"]),
        "codex_cli_call_count": int(new_calls + old_calls),
        "input_tokens_including_cached": int(new["input_tokens"] + old["input_tokens"]),
        "cached_input_tokens_subset_of_input": int(new["cached_input_tokens"] + old["cached_input_tokens"]),
        "output_tokens": int(new["output_tokens"] + old["output_tokens"]),
        "total_tokens_input_plus_output": int(new["input_tokens"] + old["input_tokens"] + new["output_tokens"] + old["output_tokens"]),
        "codex_standard_credit_equivalent": float(new["codex_standard_credit_equivalent"] + old["codex_standard_credit_equivalent"]),
        "api_equivalent_usd": float(new["api_equivalent_usd"] + old["api_equivalent_usd"]),
        "account_invoice_amount": None,
        "account_invoice_status": "not_reported_by_codex_cli",
        "rate_card_equivalents_are_invoice": False,
        "sources": [str(OUT / "codex_usage_cost_reconciliation.json"), str(old_path)],
    }
    (OUT / "formal_10_seed_cost_summary.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged


def write_report(rows: list[dict[str, str]], metric_rows: list[dict[str, Any]],
                 delta_rows: list[dict[str, Any]], trace_audit: dict[str, Any],
                 cost: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, int], list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault((row["case"], row["fleet_id"], int(row["seed"])), []).append(row)
    complete_groups = sum(len(group) == 3 and all(r["complete"] == "True" and r["inputs_match_all_methods"] == "True" for r in group)
                          for group in grouped.values())
    valid_d5_rounds = 0
    invalid_d5_rounds = 0
    d5_calls_from_results = 0
    response_status_counts: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()
    for (case, fleet, seed, method), p in read_payloads(rows).items():
        if method != "D5_pure_llm_direct":
            continue
        s = p.get("summary", {})
        d5_calls_from_results += int(s.get("total_llm_calls", 0) or 0)
        for audit in p.get("round_audits", []):
            if audit.get("validator_status") == "valid" and audit.get("one_to_one_valid") is True and not audit.get("hard_constraint_errors"):
                valid_d5_rounds += 1
            else:
                invalid_d5_rounds += 1
            direct = audit.get("direct_llm_assignment") or {}
            status = direct.get("status")
            if isinstance(status, str):
                response_status_counts[status] += 1
            if status == "llm_error_or_invalid_response":
                reason = str(direct.get("error") or "unspecified")
                if reason.startswith("order_selected_twice:"):
                    rejection_reasons["duplicate_order_selected"] += 1
                elif reason.startswith("vehicle_selected_twice:"):
                    rejection_reasons["duplicate_vehicle_selected"] += 1
                elif reason.startswith("selected_candidate_not_hard_feasible:"):
                    rejection_reasons["non_hard_feasible_candidate"] += 1
                elif reason == "duplicate_order_id":
                    rejection_reasons["duplicate_order_id"] += 1
                else:
                    rejection_reasons["other"] += 1
    summary = {
        "configuration_seed_method_rows": len(rows),
        "configuration_seed_triplets": len(grouped),
        "complete_input_matched_triplets": int(complete_groups),
        "d5_complete_runs": sum(r["method"] == "D5_pure_llm_direct" and r["complete"] == "True" for r in rows),
        "d5_live_runs_seed_1_to_9": 45,
        "d5_seed_0_includes_captured_trace_replay_for_c4_f1": True,
        "d5_llm_calls_in_result_summaries": d5_calls_from_results,
        "d5_validated_rounds": valid_d5_rounds,
        "d5_invalid_rounds": invalid_d5_rounds,
        "d5_detailed_response_status_counts": dict(response_status_counts),
        "d5_detailed_rejected_response_count": int(response_status_counts.get("llm_error_or_invalid_response", 0)),
        "d5_detailed_rejection_reasons": dict(rejection_reasons),
        "cli_trace_audit": trace_audit,
        "cost": cost,
        "scope_note": "Seeds 0-9 repeat runs on five frozen scenarios. These are not ten independent TLC demand samples or estimates of real NYC operations.",
    }
    (OUT / "experiment_audit_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# NYC TLC 调度层 D5 Codex CLI 重复运行结果",
        "",
        "## 运行与审计",
        "",
        f"- 覆盖 {summary['configuration_seed_triplets']} 个配置—seed 组合（五个冻结场景/车队 × seeds 0–9），三方法完整且输入指纹匹配的组合 {summary['complete_input_matched_triplets']} 个。",
        f"- D5 新增 live runs（seed 1–9）45 次；seed 0 复用原 v2 结果，其中 C4-F1 使用已捕获响应离线重放。",
        f"- D5 仿真汇总共记录 {d5_calls_from_results} 次模型调用；trace 中有 {trace_audit['turn_completed_with_usage_count']} 个带 usage 的完成回合，工具事件轨迹 {trace_audit['tool_event_trace_file_count']} 个，JSONL 解析错误 {trace_audit['malformed_jsonl_lines']} 行。",
        f"- D5 逐轮状态/匹配 validator 有效审计 {valid_d5_rounds} 轮，未通过轮数 {invalid_d5_rounds} 轮；详细逐调用状态中有 {response_status_counts.get('llm_error_or_invalid_response', 0)} 次模型输出被候选检查拒绝，原因计数：{dict(rejection_reasons)}。被拒输出没有用于派单，未触发重试或回退。",
        "- seed 0–9 使用同一组五个冻结场景文件；均值和标准差表示这些固定场景上的重复运行变化，不是独立纽约订单样本统计，也不代表真实纽约运营表现。",
        "",
        "## 各配置方法结果",
        "",
        "数值写为 mean ± sample SD（n=10）。距离单位 km，等待时间单位秒，服务率为百分比。",
        "",
        "| 配置 | 方法 | 服务率 % | 完成订单 | 平均接驾等待 s | P95 等待 s | 空驶 km | 能耗 kWh | 调度轮数 | LLM 调用 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    config_names = {case: case.split("_", 1)[0] for case, _ in {(r["case"], r["fleet_id"]) for r in rows}}
    for row in metric_rows:
        label = config_names.get(row["case"], row["case"])
        if row["case"].startswith("C4_"):
            label += "-" + row["fleet_id"]
        vals = [
            f"{fmt(row['service_rate_mean'] * 100 if row['service_rate_mean'] is not None else None, 2)} ± {fmt(row['service_rate_sd'] * 100 if row['service_rate_sd'] is not None else None, 2)}",
            f"{fmt(row['served_orders_mean'], 2)} ± {fmt(row['served_orders_sd'], 2)}",
            f"{fmt(row['average_pickup_wait_sec_mean'], 2)} ± {fmt(row['average_pickup_wait_sec_sd'], 2)}",
            f"{fmt(row['p95_pickup_wait_sec_mean'], 2)} ± {fmt(row['p95_pickup_wait_sec_sd'], 2)}",
            f"{fmt(row['total_empty_distance_m_mean'] / 1000 if row['total_empty_distance_m_mean'] is not None else None, 2)} ± {fmt(row['total_empty_distance_m_sd'] / 1000 if row['total_empty_distance_m_sd'] is not None else None, 2)}",
            f"{fmt(row['total_energy_kwh_mean'], 2)} ± {fmt(row['total_energy_kwh_sd'], 2)}",
            f"{fmt(row['total_dispatch_rounds_mean'], 2)} ± {fmt(row['total_dispatch_rounds_sd'], 2)}",
            f"{fmt(row['total_llm_calls_mean'], 2)} ± {fmt(row['total_llm_calls_sd'], 2)}",
        ]
        lines.append(f"| {label} | {row['method']} | " + " | ".join(vals) + " |")
    lines.extend([
        "",
        "## D5 与传统基线的配对差值",
        "",
        "差值为 D5 减基线的 seed 内配对均值 ± sample SD；负的等待、空驶或能耗差值表示 D5 在该固定配置的重复运行中较低。该比较不构成总体显著性或泛化结论。",
        "",
        "| 配置 | 对比 | 服务率百分点 | 完成订单 | 平均等待 s | 空驶 km | 能耗 kWh |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    delta_by = {(r["case"], r["fleet_id"], r["comparison"], r["metric"]): r for r in delta_rows}
    for case, fleet in sorted({(r["case"], r["fleet_id"]) for r in metric_rows}):
        label = config_names.get(case, case)
        if case.startswith("C4_"):
            label += "-" + fleet
        for comp in ("D5-D0", "D5-D1"):
            def cell(metric: str, scale: float = 1.0, digits: int = 2) -> str:
                item = delta_by[(case, fleet, comp, metric)]
                mean, sd = item["mean_delta"], item["sd_delta"]
                if mean is None:
                    return "—"
                return f"{fmt(mean * scale, digits)} ± {fmt(sd * scale, digits)}"
            lines.append("| {cfg} | {comp} | {sr} | {served} | {wait} | {dist} | {energy} |".format(
                cfg=label, comp=comp, sr=cell("service_rate", 100), served=cell("served_orders"),
                wait=cell("average_pickup_wait_sec"), dist=cell("total_empty_distance_m", 0.001),
                energy=cell("total_energy_kwh")))
    lines.extend([
        "",
        "## Codex CLI 用量与费用等值",
        "",
        f"- 新增 seed 1–9：{cost['new_seed_1_to_9_run_count']} 个完整 D5 结果，{cost['codex_cli_call_count'] - 42} 次调用，输入 {cost['input_tokens_including_cached'] - 937903:,}（缓存 {cost['cached_input_tokens_subset_of_input'] - 326016:,}）、输出 {cost['output_tokens'] - 8754:,} token。",
        f"- seed 0 复用 v2：5 个配置、42 次调用；10-seed 合计 {cost['codex_cli_call_count']} 次调用，输入 {cost['input_tokens_including_cached']:,}（缓存 {cost['cached_input_tokens_subset_of_input']:,}）、输出 {cost['output_tokens']:,}，总 token {cost['total_tokens_input_plus_output']:,}。缓存输入计入输入 token，不重复加入总数。",
        f"- 公开 Standard 费率等值合计 {cost['codex_standard_credit_equivalent']:.6f} credits、API 标价等值 ${cost['api_equivalent_usd']:.8f}；这不是账户账单金额，实际扣费未由 CLI 报告。",
        "",
        "## 结果边界",
        "",
        "该组使用 NYC TLC 已派遣行程派生的五个固定机制场景，车辆位置和区域级接驾成本为合成代理。seed 0–9 未重新抽取独立 TLC 需求样本。结果可用于记录固定场景下 D5-Codex CLI 的重复运行表现并与同 seed D0/D1 配对；不能写成真实 NYC 服务率、统计显著的算法优劣或跨需求分布泛化结论。",
        "",
        "## 产物",
        "",
        "- `experiment_manifest.json`：实验输入、模型、seed、策略及状态。",
        "- `batch_progress.jsonl`：逐配置启停和运行状态。",
        "- `paired_results.csv`：逐 seed 的结果文件、输入匹配和核心指标。",
        "- `method_metrics.csv`、`paired_deltas.csv`：均值/标准差与配对差值。",
        "- `codex_usage_cost_reconciliation.json`、`formal_10_seed_cost_summary.json`：trace 用量复算和十 seed 合计。",
        "- `experiment_audit_summary.json`：完整性、输入和 trace 审计汇总。",
    ])
    (OUT / "comparison_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    rows = build_pairing()
    payloads = read_payloads(rows)
    metric_rows, delta_rows = write_metric_tables(payloads)
    traces = audit_cli_traces()
    cost = write_combined_cost()
    summary = write_report(rows, metric_rows, delta_rows, traces, cost)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["complete_input_matched_triplets"] == 50 and summary["d5_complete_runs"] == 50 and summary["d5_invalid_rounds"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
