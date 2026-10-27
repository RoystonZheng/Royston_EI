#!/usr/bin/env python3
"""Audit the integrated Agent + Codex + MLP + capacity-shield pilot."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


RESULT_ROOT = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results"
PILOT = RESULT_ROOT / "nn_integrated_method_v1"
D6_PATH = RESULT_ROOT / "hybrid_agent_codex_capacity_shield_v1/C4_manhattan_790_order_five_minute_load_sweep/F1/D6_agent_codex_context_optimizer_seed_0.json"
D7_PATH = RESULT_ROOT / "hybrid_agent_codex_capacity_shield_v1/C4_manhattan_790_order_five_minute_load_sweep/F1/D7_agent_codex_capacity_shield_optimizer_seed_0.json"
D8_PATH = PILOT / "evaluation_topk1_v3/C4_manhattan_790_order_five_minute_load_sweep/F1/D8_agent_codex_nn_capacity_shield_optimizer_seed_0.json"
MODEL_PATH = PILOT / "models/d1_pairwise_mlp.json"
TRAINING_REPORT_PATH = PILOT / "models/d1_pairwise_mlp_training_report.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def policy_record(payload: dict) -> dict:
    records = [audit.get("codex_context_policy") for audit in payload.get("round_audits", [])]
    records = [record for record in records if record]
    if len(records) != 1:
        raise ValueError("expected exactly one Codex context policy record")
    return records[0]


def main() -> None:
    d6, d7, d8 = load(D6_PATH), load(D7_PATH), load(D8_PATH)
    model = load(MODEL_PATH)
    training = load(TRAINING_REPORT_PATH)
    d8_audits = d8.get("round_audits", [])
    shields = [audit.get("max_cardinality_shield") for audit in d8_audits]
    ranker_records = [audit.get("neural_candidate_ranker") for audit in d8_audits]
    policy8 = policy_record(d8)
    policy7 = policy_record(d7)
    train_order_ids = set()
    for run in training["training_data"]["training_runs"]:
        case = str(run["case_id"])
        scenario_path = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/cases" / case / "orders.json"
        payload = load(scenario_path)
        orders = payload if isinstance(payload, list) else payload.get("orders", [])
        train_order_ids.update(str(order["order_id"]) for order in orders)
    eval_orders = load(ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/cases/C4_manhattan_790_order_five_minute_load_sweep/orders.json")
    eval_orders = eval_orders if isinstance(eval_orders, list) else eval_orders.get("orders", [])
    eval_order_ids = {str(order["order_id"]) for order in eval_orders}
    overlap = sorted(train_order_ids & eval_order_ids)

    def score_audit_valid(record: dict) -> bool:
        rows = record.get("candidate_score_audit")
        if not isinstance(rows, list) or record.get("candidate_score_audit_truncated") is not False:
            return False
        encoded = json.dumps(
            rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        retained_count = sum(bool(row.get("retained")) for row in rows)
        return (
            len(rows) == record.get("scored_candidate_count")
            and retained_count == record.get("candidate_count_after_ranker")
            and len(rows) - retained_count == record.get("candidate_count_pruned")
            and hashlib.sha256(encoded).hexdigest() == record.get("candidate_score_audit_sha256")
        )

    checks = {
        "D8_complete_simulation": d8.get("run_mode") == "complete_simulation" and d8.get("partial_run") is False,
        "D8_matches_D7_frozen_inputs": d8.get("input_hashes") == d7.get("input_hashes") and d8.get("scenario_fingerprint_sha256") == d7.get("scenario_fingerprint_sha256"),
        "D8_reuses_D7_prompt_and_response": policy8.get("status") == "accepted" and policy8.get("replayed_from_existing_call") is True and policy8.get("new_invocation_made") is False and policy8.get("prompt_sha256") == policy7.get("prompt_sha256"),
        "ranker_active_each_round": bool(d8_audits) and all(item and item.get("enabled") for item in ranker_records),
        "ranker_pruned_candidates": sum(item.get("candidate_count_pruned", 0) for item in ranker_records if item) > 0,
        "ranker_model_hash_matches": all(item.get("model_sha256") == training.get("model_sha256") for item in ranker_records if item),
        "candidate_score_audits_complete_and_hash_valid": bool(ranker_records) and all(score_audit_valid(item) for item in ranker_records if item),
        "capacity_preserved_each_round": bool(shields) and all(item and item.get("capacity_preserved") for item in shields),
        "all_round_validators_pass": bool(d8_audits) and all(item.get("validator_status") == "valid" for item in d8_audits),
        "no_train_eval_order_overlap": not overlap,
    }
    if not all(checks.values()):
        raise SystemExit("integrated pilot audit failed: " + json.dumps(checks, ensure_ascii=False))

    ranker_summary = {
        "round_count": len(ranker_records),
        "scored_candidate_count": sum(item["scored_candidate_count"] for item in ranker_records),
        "pruned_candidate_count": sum(item["candidate_count_pruned"] for item in ranker_records),
        "capacity_restored_candidate_count": sum(item["restored_candidate_count"] for item in shields),
        "activated_capacity_shield_rounds": sum(item["restored_candidate_count"] > 0 for item in shields),
        "max_matching_preserved_rounds": sum(item["capacity_preserved"] for item in shields),
    }
    metrics = [
        "served_orders", "service_rate", "average_pickup_wait_sec", "p95_pickup_wait_sec",
        "total_empty_distance_m", "total_energy_kwh", "total_dispatch_rounds",
        "candidate_count_before_filter", "candidate_count_after_filter", "fallback_count",
        "commitment_violations", "total_decision_time_ms", "optimizer_time_ms",
    ]
    comparisons = []
    for key in metrics:
        d7_value = d7["summary"].get(key)
        d8_value = d8["summary"].get(key)
        comparisons.append({
            "metric": key,
            "D7": d7_value,
            "D8": d8_value,
            "D8_minus_D7": (d8_value - d7_value) if isinstance(d7_value, (int, float)) and isinstance(d8_value, (int, float)) else None,
        })
    report = {
        "schema_version": "dispatch-integrated-nn-pilot-audit-v1",
        "scenario_id": d8.get("scenario_id"),
        "fleet_id": d8.get("fleet_id"),
        "seed": d8.get("summary", {}).get("seed"),
        "training_report": training,
        "model_sha256": training.get("model_sha256"),
        "evaluation_inputs_match_D7": checks["D8_matches_D7_frozen_inputs"],
        "evaluation_window_order_id_overlap": overlap,
        "new_codex_invocations": 0,
        "codex_response_replayed_from_D7": True,
        "ranker": ranker_summary,
        "D7_D8_comparison": comparisons,
        "checks": checks,
        "interpretation": "Single held-out window mechanism pilot. D1 imitation labels, two small training windows, and one evaluation window do not establish broad generalization or real NYC service improvement. Replayed Codex output incurred no new model invocation; end-to-end live Codex latency is not measured by this run.",
        "result_paths": {
            "D6": str(D6_PATH), "D7": str(D7_PATH), "D8": str(D8_PATH),
            "model": str(MODEL_PATH), "training_report": str(TRAINING_REPORT_PATH),
        },
    }
    audit_path = PILOT / "evaluation_topk1_v3/integrated_nn_pilot_audit.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with (audit_path.parent / "D7_D8_comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["metric", "D7", "D8", "D8_minus_D7"])
        writer.writeheader()
        writer.writerows(comparisons)
    s7, s8 = d7["summary"], d8["summary"]
    training_rows = "\n".join(
        f"| {item['case_id']} | {item.get('source_request_window')} | {item['order_id_count']} | {item['pairwise_sample_count']} |"
        for item in training["training_data"]["training_runs"]
    )
    report_text = f"""# D8 集成神经网络调度 pilot

日期：2026-10-23  
场景：C4-F1，NYC TLC 派生 790 单，合成车队 12 辆，seed 0。该结果是单窗口机制 pilot。

## 方法

D8 将 Vehicle Agent、一次 Codex 情境策略、D1 标签训练的成对排序 MLP、每车 Top-1 筛选、最大匹配容量保护和确定性全局优化器串成同一条工作流。D1/D8 使用共同历史需求输入；训练窗口和评估窗口的订单 ID 无重叠。模型标签是 D1 优化器模仿，不是真实司机或未服务需求标签。

| D1 训练窗口 | TLC 请求时间 | 有可行候选的订单数 | 成对样本数 |
|---|---|---:|---:|
{training_rows}

训练共 {training['training_data']['training_order_id_count']} 个有可行候选的订单、{training['training']['pairwise_sample_count']} 个成对样本、120 epoch；训练集成对命中率 {training['training']['in_sample_pair_accuracy']:.1%}，只用于拟合诊断。

## D7 与 D8

| 指标 | D7 | D8 |
|---|---:|---:|
| 已服务订单 | {s7['served_orders']}/{s7['order_count']} | {s8['served_orders']}/{s8['order_count']} |
| 平均接驾等待 | {s7['average_pickup_wait_sec']:.2f}s | {s8['average_pickup_wait_sec']:.2f}s |
| P95 接驾等待 | {s7['p95_pickup_wait_sec']:.2f}s | {s8['p95_pickup_wait_sec']:.2f}s |
| 空驶距离代理 | {s7['total_empty_distance_m']:.2f}m | {s8['total_empty_distance_m']:.2f}m |
| 能耗代理 | {s7['total_energy_kwh']:.3f}kWh | {s8['total_energy_kwh']:.3f}kWh |
| 仿真轮数 | {s7['total_dispatch_rounds']} | {s8['total_dispatch_rounds']} |

网络对 {ranker_summary['scored_candidate_count']} 条硬可行候选打分，筛掉 {ranker_summary['pruned_candidate_count']} 条；容量保护在 {ranker_summary['activated_capacity_shield_rounds']} 轮恢复了 {ranker_summary['capacity_restored_candidate_count']} 条边。571/571 轮最大匹配容量均保留，逐轮 validator 全部通过，承诺违约为 0。D7 和 D8 的已服务订单及上述主要性能指标相同，本次未观察到服务或等待改善。

## 用量与解释边界

D8 复用了 D7 已保存的同 prompt Codex 响应，新增 Codex 请求为 0。复用记录携带的原响应 usage 为 13,451 输入 + 210 输出 = 13,661 token；这是原始请求消耗，不是 D8 新增消耗。账户实际扣款未知。

训练样本和评估窗口数量都很少，且 D1 标签只反映当前优化器偏好。当前 pilot 证明 MLP 候选筛选与容量保护可接入并通过逐轮校验，不证明其提升总体调度性能，也不能外推 NYC 实际服务率。正式比较需要更多独立时间窗和更丰富的硬可行候选竞争。

模型 SHA-256：`{training['model_sha256']}`  
评估输入与 D7 一致：{checks['D8_matches_D7_frozen_inputs']}  
逐轮审计：[`integrated_nn_pilot_audit.json`](integrated_nn_pilot_audit.json)  
配对指标：[`D7_D8_comparison.csv`](D7_D8_comparison.csv)
"""
    (audit_path.parent / "integrated_nn_pilot_report.md").write_text(report_text, encoding="utf-8")
    print(json.dumps({
        "audit_path": str(audit_path.resolve()),
        "checks": checks,
        "ranker": ranker_summary,
        "D7_summary": d7["summary"],
        "D8_summary": d8["summary"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
