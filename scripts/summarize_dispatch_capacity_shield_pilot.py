#!/usr/bin/env python3
"""Audit the paired C4 D6/D7 capacity-shield pilot."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PILOT_DIR = (
    ROOT
    / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/"
    / "hybrid_agent_codex_capacity_shield_v1/C4_manhattan_790_order_five_minute_load_sweep/F1"
)
D6_PATH = PILOT_DIR / "D6_agent_codex_context_optimizer_seed_0.json"
D7_PATH = PILOT_DIR / "D7_agent_codex_capacity_shield_optimizer_seed_0.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def policy_record(payload: dict) -> dict:
    audits = payload.get("round_audits", [])
    records = [audit.get("codex_context_policy") for audit in audits]
    records = [record for record in records if record]
    if len(records) != 1:
        raise SystemExit("expected exactly one Codex context policy audit record")
    return records[0]


def main() -> None:
    d6 = load(D6_PATH)
    d7 = load(D7_PATH)
    d6_summary = d6["summary"]
    d7_summary = d7["summary"]
    d6_policy = policy_record(d6)
    d7_policy = policy_record(d7)
    same_inputs = (
        d6["input_hashes"] == d7["input_hashes"]
        and d6["scenario_fingerprint_sha256"] == d7["scenario_fingerprint_sha256"]
    )
    same_prompt = d6_policy.get("prompt_sha256") == d7_policy.get("prompt_sha256")
    same_parameters = d6_policy.get("policy_parameters") == d7_policy.get("policy_parameters")
    d6_replayed = (
        d6_policy.get("status") == "accepted"
        and d6_policy.get("replayed_from_existing_call") is True
        and d6_policy.get("new_invocation_made") is False
    )
    d7_called_once = (
        d7_policy.get("status") == "accepted"
        and d7_policy.get("invocation_made") is True
        and d7_policy.get("retry_count") == 0
    )
    d7_audits = d7["round_audits"]
    d6_audits = d6["round_audits"]
    capacity_records = [audit.get("max_cardinality_shield") for audit in d7_audits]
    if any(record is None for record in capacity_records):
        raise SystemExit("D7 is missing a per-round maximum-matching shield audit")
    activations = [
        {
            "dispatch_round_id": audit["dispatch_round_id"],
            "snapshot_time_sec": audit["snapshot_time_sec"],
            **audit["max_cardinality_shield"],
        }
        for audit in d7_audits
        if audit["max_cardinality_shield"]["restored_candidate_count"] > 0
    ]
    all_complete = all(
        payload.get("run_mode") == "complete_simulation"
        and payload.get("partial_run") is False
        for payload in (d6, d7)
    )
    validators_valid = all(
        audit.get("validator_status") == "valid"
        for audits in (d6_audits, d7_audits)
        for audit in audits
    )
    hard_errors = sum(
        len(audit.get("hard_constraint_errors", []))
        for audits in (d6_audits, d7_audits)
        for audit in audits
    )
    capacity_preserved = all(record.get("capacity_preserved") for record in capacity_records)
    unique_codex_requests = int(d7_policy.get("invocation_made", False)) + int(
        d6_policy.get("new_invocation_made", False)
    )
    checks = {
        "same_frozen_inputs": same_inputs,
        "same_codex_prompt_hash": same_prompt,
        "same_codex_parameters": same_parameters,
        "D6_reused_D7_response_without_new_call": d6_replayed,
        "D7_one_live_request_no_retry": d7_called_once,
        "both_complete_simulations": all_complete,
        "all_round_validators_valid": validators_valid,
        "no_hard_constraint_errors": hard_errors == 0,
        "D7_capacity_preserved_every_round": capacity_preserved,
        "capacity_shield_activated_at_least_once": bool(activations),
    }
    if not all(checks.values()):
        raise SystemExit("pilot audit failed: " + json.dumps(checks, ensure_ascii=False))

    metric_fields = [
        "served_orders",
        "service_rate",
        "average_pickup_wait_sec",
        "p95_pickup_wait_sec",
        "total_empty_distance_m",
        "total_energy_kwh",
        "total_dispatch_rounds",
        "candidate_count_before_filter",
        "candidate_count_after_filter",
        "fallback_count",
        "commitment_violations",
    ]
    comparison_rows = []
    for field in metric_fields:
        d6_value = d6_summary[field]
        d7_value = d7_summary[field]
        comparison_rows.append(
            {"metric": field, "D6": d6_value, "D7": d7_value, "D7_minus_D6": d7_value - d6_value}
        )
    comparison_rows.extend(
        [
            {"metric": "new_codex_requests_for_pair", "D6": 0, "D7": 1, "D7_minus_D6": 1},
            {
                "metric": "unique_codex_requests_for_paired_experiment",
                "D6": None,
                "D7": unique_codex_requests,
                "D7_minus_D6": None,
            },
            {
                "metric": "shared_codex_tokens_recorded_once",
                "D6": None,
                "D7": d7_summary["total_llm_tokens"],
                "D7_minus_D6": None,
            },
        ]
    )
    comparison_path = PILOT_DIR / "capacity_shield_pilot_comparison.csv"
    with comparison_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(comparison_rows[0]))
        writer.writeheader()
        writer.writerows(comparison_rows)

    trace_path = Path(d7_policy["trace_jsonl_path"])
    prompt_path = Path(d7_policy["prompt_path"])
    final_path = Path(d7_policy["final_json_path"])
    audit = {
        "schema_version": "dispatch_capacity_shield_pilot_audit_v1",
        "created_date": "2026-10-23",
        "case_id": d7["case_id"],
        "fleet_id": d7["fleet_id"],
        "seed": d7_summary["seed"],
        "status": "complete_single_seed_paired_mechanism_pilot_not_formal_performance_result",
        "checks": checks,
        "input_hashes": d7["input_hashes"],
        "scenario_fingerprint_sha256": d7["scenario_fingerprint_sha256"],
        "round_counts": {"D6": len(d6_audits), "D7": len(d7_audits)},
        "validator_invalid_rounds": {
            "D6": sum(audit.get("validator_status") != "valid" for audit in d6_audits),
            "D7": sum(audit.get("validator_status") != "valid" for audit in d7_audits),
        },
        "hard_constraint_error_count": hard_errors,
        "capacity_shield": {
            "audited_rounds": len(capacity_records),
            "capacity_loss_before_shield_rounds": sum(
                record["filtered_max_matching_before_shield"]
                < record["full_hard_feasible_max_matching"]
                for record in capacity_records
            ),
            "activated_rounds": len(activations),
            "total_restored_edges": sum(
                record["restored_candidate_count"] for record in capacity_records
            ),
            "activation_records": activations,
            "capacity_preserved_every_round": capacity_preserved,
            "scope": "per_dispatch_snapshot_maximum_matching_cardinality_only",
        },
        "codex": {
            "model": d7_policy["model"],
            "reasoning_effort": d7_policy["reasoning_effort"],
            "new_requests_for_D7": 1,
            "new_requests_for_D6_replay": 0,
            "unique_new_requests_for_pair": unique_codex_requests,
            "prompt_sha256_D6": d6_policy["prompt_sha256"],
            "prompt_sha256_D7": d7_policy["prompt_sha256"],
            "input_tokens": d7_summary["total_llm_input_tokens"],
            "cached_input_tokens": d7_summary["total_llm_cached_input_tokens"],
            "output_tokens": d7_summary["total_llm_output_tokens"],
            "total_tokens": d7_summary["total_llm_tokens"],
            "standard_credit_equivalent": d7_summary["total_codex_standard_credit_equivalent"],
            "api_rate_card_equivalent_usd": d7_summary["total_api_equivalent_usd"],
            "actual_account_charge_usd": None,
            "tool_usage_detected": d7_policy.get("tool_usage_detected"),
            "retry_count": d7_policy.get("retry_count"),
        },
        "D6_summary": d6_summary,
        "D7_summary": d7_summary,
        "artifacts": {
            "D6_result_sha256": sha256(D6_PATH),
            "D7_result_sha256": sha256(D7_PATH),
            "Codex_prompt_sha256": sha256(prompt_path),
            "Codex_trace_sha256": sha256(trace_path),
            "Codex_final_response_sha256": sha256(final_path),
        },
        "limitations": [
            "One fixed TLC request window, one synthetic fleet configuration, and one seed do not support a performance conclusion or generalization to NYC operations.",
            "The shield preserves maximum matching cardinality for each current dispatch snapshot; it does not guarantee end-to-end service over the full simulation horizon.",
            "Pickup distances, vehicle positions, and energy are proxies rather than observed NYC fleet operations.",
            "Codex JSONL usage supports a rate-card equivalent estimate, not proof of actual account billing.",
        ],
    }
    audit_path = PILOT_DIR / "capacity_shield_pilot_audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def fmt(value: float, digits: int = 2) -> str:
        return f"{value:,.{digits}f}"

    d6_mean = d6_summary["average_pickup_wait_sec"]
    d7_mean = d7_summary["average_pickup_wait_sec"]
    d6_empty = d6_summary["total_empty_distance_m"]
    d7_empty = d7_summary["total_empty_distance_m"]
    d6_energy = d6_summary["total_energy_kwh"]
    d7_energy = d7_summary["total_energy_kwh"]
    report = [
        "# 调度层容量保护机制配对 pilot",
        "",
        "状态：C4/F1/seed 0 完整配对仿真，用于验证 D7 容量保护机制是否被触发以及整条链路能否运行；不是正式性能结论。",
        "",
        f"- 场景：{d7['case_id']}，{d7_summary['order_count']} 单、{d7_summary['vehicle_count']} 车，seed {d7_summary['seed']}。",
        f"- D6 与 D7 使用相同冻结输入哈希、场景指纹和 Codex 提示 SHA-256：{same_inputs and same_prompt}；D6 逐字节提示校验后回放 D7 已捕获响应。",
        f"- 唯一的新 Codex CLI 请求：{unique_codex_requests} 次；gpt-5.6-luna / low，零重试、零工具；用量 {d7_summary['total_llm_input_tokens']:,} 输入 + {d7_summary['total_llm_output_tokens']:,} 输出 = {d7_summary['total_llm_tokens']:,} token。",
        f"- 标准费率等值 {d7_summary['total_codex_standard_credit_equivalent']:.6f} credits / ${d7_summary['total_api_equivalent_usd']:.8f}；不是 Codex 账户账单。",
        f"- D7 完成 {len(d7_audits)} 轮，D6 完成 {len(d6_audits)} 轮；逐轮 validator 全通过、硬约束错误 {hard_errors}、承诺违约 D6={d6_summary['commitment_violations']} / D7={d7_summary['commitment_violations']}。",
        f"- 容量保护在 {len(activations)} 轮触发、恢复 {sum(record['restored_candidate_count'] for record in capacity_records)} 条边，{len(capacity_records)} 轮的最大匹配容量均得到保留。",
        "",
        "## 配对结果（单 seed）",
        "",
        "| 指标 | D6 Agent+Codex+优化器 | D7 加容量保护 | D7−D6 |",
        "|---|---:|---:|---:|",
        f"| 完成订单 | {d6_summary['served_orders']}/790 | {d7_summary['served_orders']}/790 | {d7_summary['served_orders'] - d6_summary['served_orders']:+d} |",
        f"| 服务率 | {d6_summary['service_rate']:.2%} | {d7_summary['service_rate']:.2%} | {d7_summary['service_rate'] - d6_summary['service_rate']:+.2%} |",
        f"| 平均接驾等待 | {fmt(d6_mean)} 秒 | {fmt(d7_mean)} 秒 | {fmt(d7_mean-d6_mean, 3)} 秒 |",
        f"| P95 接驾等待 | {fmt(d6_summary['p95_pickup_wait_sec'])} 秒 | {fmt(d7_summary['p95_pickup_wait_sec'])} 秒 | {fmt(d7_summary['p95_pickup_wait_sec']-d6_summary['p95_pickup_wait_sec'], 3)} 秒 |",
        f"| 空驶距离 | {fmt(d6_empty)} 米 | {fmt(d7_empty)} 米 | {fmt(d7_empty-d6_empty, 3)} 米 |",
        f"| 能耗代理 | {fmt(d6_energy, 3)} kWh | {fmt(d7_energy, 3)} kWh | {fmt(d7_energy-d6_energy, 3)} kWh |",
        f"| 调度轮次 | {d6_summary['total_dispatch_rounds']} | {d7_summary['total_dispatch_rounds']} | {d7_summary['total_dispatch_rounds']-d6_summary['total_dispatch_rounds']:+d} |",
        "",
        "本 seed 中 D7 未增加完成订单；平均等待、空驶和能耗代理高于 D6。容量保护的已观测作用是恢复一轮中的理论最大匹配容量，不能据此称为改善端到端服务表现。",
        "",
        "## 机制触发记录",
        "",
    ]
    for event in activations:
        report.append(
            f"- {event['dispatch_round_id']}（t={event['snapshot_time_sec']:.1f}s）：完整可行图最大匹配 {event['full_hard_feasible_max_matching']}，筛选后为 {event['filtered_max_matching_before_shield']}；恢复 {event['restored_candidate_count']} 条边后为 {event['max_matching_after_shield']}。"
        )
    report.extend(
        [
            "",
            "## 结论边界与下一步",
            "",
            "这次运行证明 D7 可以接入并触发容量恢复，且没有破坏候选匹配容量或统一硬约束校验。它只覆盖一个历史需求窗口、F1 合成车队和一个 seed；不能证明服务率改善、统计显著性或真实 NYC 运营效果。TLC 数据没有真实空车位置、拒单请求和实测接驾 ETA。",
            "",
            "正式批次应扩大到多个相互独立的 TLC 时间窗口与车队配置；每个固定输入上先运行 D7 并记录唯一 Codex 响应，再将相同提示/响应回放给 D6，另运行 D2 规则 Agent 基线。结果按输入窗口配对，报告服务、等待、空驶、能耗、容量恢复、失败回退及 token。不要把同一固定窗口的重复 seed 当作独立需求样本。",
            "",
            "逐项审计见 `capacity_shield_pilot_audit.json`，指标表见 `capacity_shield_pilot_comparison.csv`；原始 D6/D7 JSON 和 Codex prompt、JSONL、最终响应均保存在本目录。",
        ]
    )
    report_path = PILOT_DIR / "capacity_shield_pilot_report.md"
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "audit": str(audit_path),
                "report": str(report_path),
                "comparison": str(comparison_path),
                "checks": checks,
                "capacity_activations": len(activations),
                "unique_codex_requests": unique_codex_requests,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
