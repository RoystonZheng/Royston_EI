#!/usr/bin/env python3
"""Audit the first Codex policy + vehicle-agent + optimizer dispatch pilot."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1"
RESULTS = PACKAGE / "results"
CASE = "C3_manhattan_multizone_44_order_burst"
FLEET = "single_fleet"
D6_DIR = RESULTS / "hybrid_agent_codex_nn_v1" / CASE / FLEET
D6_PATH = D6_DIR / "D6_agent_codex_context_optimizer_seed_0.json"
TRACE_DIR = D6_DIR / "codex_context_policy_traces"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def method_paths() -> dict[str, Path]:
    return {
        "D0_greedy": RESULTS / "traditional_baselines_complete_v1" / CASE / FLEET / "D0_greedy_seed_0.json",
        "D1_optimizer": RESULTS / "traditional_baselines_complete_v1" / CASE / FLEET / "D1_optimizer_seed_0.json",
        "D2_agent_optimizer": RESULTS / CASE / FLEET / "D2_agent_optimizer_seed_0.json",
        "D5_pure_llm_direct": RESULTS / "pure_llm_codex_cli_feasible_edges_v2" / CASE / FLEET / "D5_pure_llm_direct_seed_0.json",
        "D6_agent_codex_context_optimizer": D6_PATH,
    }


def main() -> None:
    methods = {name: load(path) for name, path in method_paths().items()}
    d6 = methods["D6_agent_codex_context_optimizer"]
    d6_summary = d6["summary"]
    hashes = {name: payload["input_hashes"] for name, payload in methods.items()}
    fingerprints = {name: payload["scenario_fingerprint_sha256"] for name, payload in methods.items()}
    same_inputs = len({json.dumps(value, sort_keys=True) for value in hashes.values()}) == 1
    same_scenario = len(set(fingerprints.values())) == 1
    validator_states = sorted({round_record["output"]["validator_status"] for round_record in d6["rounds"]})
    hard_errors = sum(len(round_record["output"].get("hard_constraint_errors", [])) for round_record in d6["rounds"])
    policy_records = [
        round_record["output"].get("audit", {}).get("codex_context_policy")
        for round_record in d6["rounds"]
        if round_record["output"].get("audit", {}).get("codex_context_policy")
    ]
    if len(policy_records) != 1:
        raise SystemExit("expected exactly one D6 policy-call audit record")
    policy = policy_records[0]
    trace_path = Path(policy["trace_jsonl_path"])
    final_path = Path(policy["final_json_path"])
    prompt_path = Path(policy["prompt_path"])
    complete = d6.get("run_mode") == "complete_simulation" and not d6.get("partial_run")
    if not same_inputs or not same_scenario:
        raise SystemExit("input mismatch across methods")
    if not complete or validator_states != ["valid"] or hard_errors != 0:
        raise SystemExit("D6 completeness/validator audit failed")
    if not policy.get("replayed_from_existing_call") or policy.get("new_invocation_made") is not False:
        raise SystemExit("D6 pilot must reuse the one captured Codex response")
    if policy.get("tool_usage_detected") or policy.get("status") != "accepted":
        raise SystemExit("D6 policy response was not accepted without tools")

    rows = []
    for name, payload in methods.items():
        summary = payload["summary"]
        rows.append({
            "method": name,
            "served_orders": summary["served_orders"],
            "service_rate": summary["service_rate"],
            "average_pickup_wait_sec": summary["average_pickup_wait_sec"],
            "p95_pickup_wait_sec": summary["p95_pickup_wait_sec"],
            "total_empty_distance_m": summary["total_empty_distance_m"],
            "total_energy_kwh": summary["total_energy_kwh"],
            "total_dispatch_rounds": summary["total_dispatch_rounds"],
            "total_llm_calls": summary["total_llm_calls"],
            "total_llm_tokens": summary.get("total_llm_tokens"),
            "total_decision_time_ms": summary["total_decision_time_ms"],
            "decision_time_includes_cli_latency": name != "D6_agent_codex_context_optimizer",
        })
    with (D6_DIR / "pilot_comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    reference = methods["D1_optimizer"]["summary"]
    round_count = len(d6["rounds"])
    d6_vs_d1 = {
        "served_orders_delta": d6_summary["served_orders"] - reference["served_orders"],
        "average_wait_sec_delta": d6_summary["average_pickup_wait_sec"] - reference["average_pickup_wait_sec"],
        "p95_wait_sec_delta": d6_summary["p95_pickup_wait_sec"] - reference["p95_pickup_wait_sec"],
        "empty_distance_m_delta": d6_summary["total_empty_distance_m"] - reference["total_empty_distance_m"],
        "energy_kwh_delta": d6_summary["total_energy_kwh"] - reference["total_energy_kwh"],
    }
    audit = {
        "schema_version": "hybrid_dispatch_pilot_audit_v1",
        "created_date": "2026-10-22",
        "case": CASE,
        "fleet_id": FLEET,
        "seed": 0,
        "status": "complete_single_seed_mechanism_pilot_not_formal_performance_result",
        "all_methods_share_identical_input_hashes": same_inputs,
        "all_methods_share_identical_scenario_fingerprint": same_scenario,
        "input_hashes": hashes,
        "scenario_fingerprints": fingerprints,
        "d6_run_mode": d6["run_mode"],
        "d6_round_count": len(d6["rounds"]),
        "d6_validator_states": validator_states,
        "d6_hard_constraint_error_count": hard_errors,
        "d6_commitment_violations": d6_summary["commitment_violations"],
        "d6_context_filter_candidate_counts": {
            "before": d6_summary["candidate_count_before_filter"],
            "after": d6_summary["candidate_count_after_filter"],
        },
        "d6_fallback_count": d6_summary["fallback_count"],
        "codex_policy": policy,
        "codex_policy_artifact_hashes": {
            "jsonl_sha256": sha256(trace_path),
            "final_json_sha256": sha256(final_path),
            "prompt_sha256": sha256(prompt_path),
        },
        "model_call_accounting": {
            "new_codex_requests_for_completed_simulation": 0,
            "original_codex_requests_reused": 1,
            "input_tokens": d6_summary["total_llm_input_tokens"],
            "cached_input_tokens": d6_summary["total_llm_cached_input_tokens"],
            "output_tokens": d6_summary["total_llm_output_tokens"],
            "total_tokens": d6_summary["total_llm_tokens"],
            "codex_standard_credit_equivalent": d6_summary["total_codex_standard_credit_equivalent"],
            "api_rate_card_equivalent_usd": d6_summary["total_api_equivalent_usd"],
            "actual_account_charge_usd": None,
            "codex_cli_latency_ms": None,
            "codex_cli_latency_status": "not_captured_before_initial_local_interruption",
        },
        "d6_vs_d1_single_seed_delta": d6_vs_d1,
        "input_cost_limitations": d6["cost_semantics"],
        "initial_execution_note": "The first local run stopped after the Codex response due to a missing CandidateDecision import. The preserved response and trace were replayed with byte-identical prompt content; no second model request was made. The initial incomplete local result is preserved as D6_agent_codex_context_optimizer_seed_0_replay_accounting_pre_fix.json.",
        "benchmark_manifest_sha256": sha256(PACKAGE / "benchmark_manifest.json"),
    }
    (D6_DIR / "pilot_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    comparison_lines = [
        "# D6 Codex + Agent + 优化器首轮 pilot",
        "",
        "状态：单场景 seed 0 的完整仿真 pilot；用于检查组合链路和记账，不是正式性能结论。",
        "",
        f"- 场景：{CASE}，18 辆车、44 个订单，seed 0。",
        f"- D0/D1/D2/D5/D6 的场景指纹和三项输入 SHA-256 一致：{same_inputs and same_scenario}。",
        f"- D6：{round_count} 个调度轮次；validator 全部通过；硬约束错误 {hard_errors}；承诺违约 {d6_summary['commitment_violations']}；候选过滤回退 {d6_summary['fallback_count']} 次。",
        f"- Codex：一次原始请求，gpt-5.6-luna / low；返回参数 base={policy['policy_parameters']['base']}、weights={policy['policy_parameters']['weights']}；无工具事件、零重试。",
        f"- 用量：输入 {d6_summary['total_llm_input_tokens']:,}、缓存输入 {d6_summary['total_llm_cached_input_tokens']:,}、输出 {d6_summary['total_llm_output_tokens']:,}、合计 {d6_summary['total_llm_tokens']:,} token。标准费率等值 {d6_summary['total_codex_standard_credit_equivalent']:.6f} credits / ${d6_summary['total_api_equivalent_usd']:.8f}；不是账户账单。CLI 实际耗时未保存在首次中断记录中。",
        "",
        "## 对照",
        "",
        "| 方法 | 完成/44 | 平均接驾等待（秒） | P95（秒） | 空驶距离（米） | 能耗代理（kWh） | Codex/LLM 调用 | token |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        tokens = f"{row['total_llm_tokens']:,}" if isinstance(row["total_llm_tokens"], int) else "—"
        comparison_lines.append(
            f"| {row['method']} | {row['served_orders']}/44 ({row['service_rate']:.1%}) | "
            f"{row['average_pickup_wait_sec']:.2f} | {row['p95_pickup_wait_sec']:.2f} | "
            f"{row['total_empty_distance_m']:.1f} | {row['total_energy_kwh']:.3f} | "
            f"{row['total_llm_calls']} | {tokens} |"
        )
    comparison_lines.extend([
        "",
        "相对 D1，D6 的完成订单数变化为 {served_orders_delta:+d}；平均等待变化 {average_wait_sec_delta:+.2f} 秒，P95 变化 {p95_wait_sec_delta:+.2f} 秒，空驶变化 {empty_distance_m_delta:+.1f} 米，能耗代理变化 {energy_kwh_delta:+.3f} kWh。".format(**d6_vs_d1),
        "",
        "单个固定案例 seed 不能支持显著性、跨场景泛化或 NYC 实际运营结论。Codex CLI 延迟未记录，因此 D6 的本地 `total_decision_time_ms` 不与其他方法的端到端时间比较；TLC 订单、车辆和接驾成本均为历史行程/合成状态/区域级代理。",
        "",
        "原始结果、prompt、最终响应和 JSONL 均在本目录的 `codex_context_policy_traces/`；逐项核验见 `pilot_audit.json` 和 `pilot_comparison.csv`。",
    ])
    (D6_DIR / "pilot_report.md").write_text("\n".join(comparison_lines) + "\n", encoding="utf-8")
    print(json.dumps({"audit": str(D6_DIR / "pilot_audit.json"), "report": str(D6_DIR / "pilot_report.md"), "same_inputs": same_inputs, "validator_states": validator_states, "token_total": d6_summary["total_llm_tokens"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
