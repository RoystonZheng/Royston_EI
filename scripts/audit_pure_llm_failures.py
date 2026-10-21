#!/usr/bin/env python3
"""Add a reproducible post-hoc failure and proposal-energy audit to v7 outputs.

This script reads saved maps, tasks, and model responses only. It never calls an
LLM or reruns a route planner. It preserves raw outputs and adds derived audit
metadata to each result before regenerating the local HTML/JSON views.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.failure_audit import (  # noqa: E402
    describe_failure_zh,
    estimate_route_proposal_energy,
    recover_partial_move_sequence,
)
from route_experiment.llm_adapter import replay_move_sequence  # noqa: E402
from route_experiment.models import EvaluationConfig  # noqa: E402
from scripts.render_seed_dashboard import render_seed_dashboard  # noqa: E402
from scripts.render_traditional_traces import load_road_map, render  # noqa: E402
from scripts.run_pure_llm_experiment import _consolidate_existing_output  # noqa: E402


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _task_lookup(seed_dir: Path):
    lookup = {}
    order_path = next(seed_dir.glob("order_tasks_seed_*.json"), None)
    formal_path = next(seed_dir.glob("formal_scenarios_seed_*.json"), None)
    if order_path:
        for task in _read_json(order_path).get("tasks", []):
            lookup[task.get("task_id")] = dict(task)
    if formal_path:
        for scenario in _read_json(formal_path).get("scenarios", []):
            for task in scenario.get("tasks", []):
                item = dict(task)
                item["blocked_edge_ids"] = list(scenario.get("blocked_edge_ids", []))
                item["scenario_type"] = scenario.get("scenario_type")
                lookup[item.get("task_id")] = item
    return lookup


def _route_paths(seed_dir: Path):
    return (
        ("orders", next(seed_dir.glob("*order_routes_costs_seed_*.json"), None)),
        ("formal", next(seed_dir.glob("*formal_routes_costs_seed_*.json"), None)),
    )


def _traditional_baseline_lookup(output_dir: Path, seed: int):
    seed_dir = output_dir.parent / "traditional_experiment_v1" / "seeds" / ("seed_{0}".format(seed))
    lookup = {}
    for scope, stem in (("orders", "order"), ("formal", "formal")):
        path = seed_dir / "traditional_{0}_routes_costs_seed_{1}.json".format(stem, seed)
        if not path.exists():
            continue
        payload = _read_json(path)
        for item in payload.get("results", []):
            methods = item.get("methods", [])
            lookup[(scope, item.get("task_id"))] = {
                "method_count": len(methods),
                "all_valid": bool(methods)
                and all(
                    method.get("status") == "completed"
                    and int(method.get("valid_route_count") or 0) > 0
                    for method in methods
                ),
                "all_failed": bool(methods)
                and all(method.get("status") == "failed" for method in methods),
            }
    return lookup


def _baseline_assessment(scope: str, task: dict, baseline: dict):
    if not baseline:
        return None
    scenario_type = task.get("scenario_type")
    if baseline.get("all_valid"):
        return (
            "同任务的 {0} 种传统算法均找到有效路线；因此这不是地图物理不可达，"
            "本次失败来自 LLM 的路线生成或地点解析。"
        ).format(baseline.get("method_count", 0))
    if (
        scope == "formal"
        and scenario_type in {"illegal_endpoint", "unreachable"}
        and baseline.get("all_failed")
    ):
        return "该正式场景预设为{0}，传统算法也按场景约束失败，属于设计中的不可行案例。".format(
            "非法端点" if scenario_type == "illegal_endpoint" else "不可达"
        )
    return "同任务传统算法结果未全部成功；需结合正式场景约束检查，不单凭 LLM 的失败码判断地图可达性。"


def _annotate_method(
    method: dict,
    task: dict,
    road_map,
    evaluation_config: EvaluationConfig,
    baseline: dict = None,
    scope: str = "",
):
    metadata = method.setdefault("metadata", {})
    raw_output = method.get("raw_output") or {}
    reason = str(method.get("failure_reason") or "unknown_failure")
    original_move_sequence = metadata.get("move_sequence") or raw_output.get("move_sequence") or ""
    move_sequence = original_move_sequence
    recovered_partial = False
    if not move_sequence and reason == "llm_output_parse_failed":
        move_sequence = (
            metadata.get("recovered_partial_move_sequence")
            or recover_partial_move_sequence(raw_output)
        )
        recovered_partial = bool(move_sequence)
        if recovered_partial:
            metadata["recovered_partial_move_sequence"] = move_sequence

    replay = None
    energy_audit = {
        "status": "no_route_proposed",
        "route_energy_estimate_kwh": None,
        "validated_prefix_energy_estimate_kwh": None,
        "estimated_route_distance_m": None,
        "proposed_move_count": 0,
        "validated_prefix_move_count": 0,
        "exact_edge_moves": 0,
        "reverse_edge_inferred_moves": 0,
        "proxy_edge_moves": 0,
        "energy_model_version": evaluation_config.energy_model_version,
        "interpretation": "No route steps were returned or recoverable; energy was not estimated.",
    }
    if move_sequence and task.get("start_node_id"):
        replay = replay_move_sequence(
            road_map,
            task["start_node_id"],
            move_sequence,
            blocked_edge_ids=task.get("blocked_edge_ids", []),
        )
        energy_audit = estimate_route_proposal_energy(road_map, replay, evaluation_config)
        if recovered_partial:
            energy_audit["status"] = "partial_estimated"
            energy_audit["interpretation"] = (
                "Partial estimate from complete move_runs recovered before the API response was truncated; "
                "not the full route and not actual measured consumption."
            )
            energy_audit["recovered_move_sequence"] = True
        else:
            energy_audit["recovered_move_sequence"] = False

    request_error = reason.startswith(("deepseek_request_failed", "missing_deepseek", "llm_request_failed"))
    response_received = not request_error and method.get("raw_output") is not None
    method["metadata"] = metadata
    metadata["llm_response_received"] = response_received
    metadata["failure_detail_zh"] = describe_failure_zh(method, replay)
    metadata["failure_reason_label_zh"] = metadata["failure_detail_zh"].split("。", 1)[0]
    baseline_detail = _baseline_assessment(scope, task, baseline or {})
    if baseline_detail:
        metadata["failure_detail_zh"] += " " + baseline_detail
    metadata["baseline_assessment_zh"] = baseline_detail
    metadata["proposal_energy_audit"] = energy_audit
    if replay is not None:
        metadata["proposal_move_violation_count"] = len(replay["move_violations"])
        metadata["proposal_move_violation_counts"] = dict(
            Counter(item.get("kind", "unknown") for item in replay["move_violations"])
        )
    else:
        metadata["proposal_move_violation_count"] = 0
        metadata["proposal_move_violation_counts"] = {}

    return {
        "task_id": method.get("task_id"),
        "method_name": method.get("method_name"),
        "status": method.get("status"),
        "response_received": response_received,
        "failure_reason": reason,
        "failure_detail_zh": metadata["failure_detail_zh"],
        "baseline_assessment_zh": baseline_detail,
        "move_violation_count": metadata["proposal_move_violation_count"],
        "move_violation_counts": metadata["proposal_move_violation_counts"],
        "proposal_energy_audit": energy_audit,
    }


def audit_experiment(output_dir: Path):
    seeds_summary = []
    all_records = []
    global_reasons = Counter()
    global_violations = Counter()
    complete_violations = Counter()
    partial_violations = Counter()
    global_response_states = Counter()
    complete_energy_total = 0.0
    partial_energy_total = 0.0
    full_sequence_count = 0
    partial_sequence_count = 0
    no_route_steps_count = 0
    for seed_dir in sorted(
        (output_dir / "seeds").glob("seed_*"),
        key=lambda path: int(path.name.split("_", 1)[1]),
    ):
        map_path = next(seed_dir.glob("map_*_seed_*.json"), None)
        if not map_path:
            continue
        road_map = load_road_map(map_path)
        tasks = _task_lookup(seed_dir)
        seed_records = []
        failure_counts = Counter()
        violation_counts = Counter()
        complete_seed_violations = Counter()
        partial_seed_violations = Counter()
        scope_counts = Counter()
        baseline_lookup = _traditional_baseline_lookup(
            output_dir, int(seed_dir.name.split("_", 1)[1])
        )
        for scope, routes_path in _route_paths(seed_dir):
            if not routes_path:
                continue
            routes_payload = _read_json(routes_path)
            for item in routes_payload.get("results", []):
                task = tasks.get(item.get("task_id"), {})
                for method in item.get("methods", []):
                    if method.get("method_name") != "pure_llm_deepseek":
                        continue
                    record = _annotate_method(
                        method,
                        task,
                        road_map,
                        EvaluationConfig(),
                        baseline=baseline_lookup.get((scope, item.get("task_id"))),
                        scope=scope,
                    )
                    record.update({"seed": int(seed_dir.name.split("_", 1)[1]), "scope": scope})
                    seed_records.append(record)
                    all_records.append(record)
                    failure_counts[record["failure_reason"]] += 1
                    scope_counts[(scope, record["failure_reason"])] += 1
                    violation_counts.update(record["move_violation_counts"])
                    global_reasons[record["failure_reason"]] += 1
                    global_violations.update(record["move_violation_counts"])
                    global_response_states["received" if record["response_received"] else "not_received"] += 1
                    audit = record["proposal_energy_audit"]
                    estimate = audit.get("route_energy_estimate_kwh") or 0.0
                    if audit.get("status") == "estimated":
                        full_sequence_count += 1
                        complete_energy_total += estimate
                        complete_violations.update(record["move_violation_counts"])
                        complete_seed_violations.update(record["move_violation_counts"])
                    elif audit.get("status") == "partial_estimated":
                        partial_sequence_count += 1
                        partial_energy_total += estimate
                        partial_violations.update(record["move_violation_counts"])
                        partial_seed_violations.update(record["move_violation_counts"])
                    else:
                        no_route_steps_count += 1
            _write_json(routes_path, routes_payload)

        for scope, routes_path in _route_paths(seed_dir):
            if routes_path:
                folder = "orders" if scope == "orders" else "formal_scenarios"
                render(map_path, routes_path, seed_dir / "visualizations" / folder)
        render_seed_dashboard(seed_dir)
        seeds_summary.append(
            {
                "seed": int(seed_dir.name.split("_", 1)[1]),
                "result_count": len(seed_records),
                "failure_reason_counts": dict(failure_counts),
                "scope_failure_reason_counts": {
                    "{0}:{1}".format(scope, reason): count
                    for (scope, reason), count in sorted(scope_counts.items())
                },
                "move_violation_counts": dict(violation_counts),
                "complete_sequence_violation_counts": dict(complete_seed_violations),
                "partial_response_violation_counts": dict(partial_seed_violations),
                "complete_proposal_energy_estimate_kwh": round(
                    sum(
                        (r["proposal_energy_audit"].get("route_energy_estimate_kwh") or 0.0)
                        for r in seed_records
                        if r["proposal_energy_audit"].get("status") == "estimated"
                    ),
                    9,
                ),
                "partial_recovered_prefix_energy_estimate_kwh": round(
                    sum(
                        (r["proposal_energy_audit"].get("route_energy_estimate_kwh") or 0.0)
                        for r in seed_records
                        if r["proposal_energy_audit"].get("status") == "partial_estimated"
                    ),
                    9,
                ),
                "partial_estimate_count": sum(
                    r["proposal_energy_audit"].get("status") == "partial_estimated"
                    for r in seed_records
                ),
                "no_route_proposed_count": sum(
                    r["proposal_energy_audit"].get("status") == "no_route_proposed"
                    for r in seed_records
                ),
                "baseline_routeable_failure_count": sum(
                    bool(r.get("baseline_assessment_zh"))
                    and "不是地图物理不可达" in r["baseline_assessment_zh"]
                    for r in seed_records
                ),
            }
        )

    report = {
        "schema_version": "pure_llm_failure_energy_audit_v1",
        "audit_date": "2026-10-18",
        "source_experiment": str(output_dir),
        "method": "post-hoc local analysis of saved responses; no API calls and no route planner reruns",
        "response_received_counts": dict(global_response_states),
        "failure_reason_counts": dict(global_reasons),
        "move_violation_counts": dict(global_violations),
        "complete_sequence_violation_counts": dict(complete_violations),
        "partial_response_violation_counts": dict(partial_violations),
        "proposal_sequence_counts": {
            "complete_sequences": full_sequence_count,
            "partial_sequences_recovered_from_parse_error": partial_sequence_count,
            "no_route_steps_returned": no_route_steps_count,
        },
        "proposal_energy_estimate_kwh": {
            "complete_proposals": round(complete_energy_total, 9),
            "partial_recovered_prefixes": round(partial_energy_total, 9),
            "note": "Keep these categories separate; the partial-response estimate is not a full-route estimate.",
        },
        "result_count": len(all_records),
        "seeds": seeds_summary,
        "records": all_records,
        "energy_estimate_note": (
            "The estimate is hypothetical, based on the model-proposed movement sequence, and is not measured "
            "vehicle consumption. Valid directed edges use recorded map attributes; prohibited reverse moves "
            "use the reverse edge as a proxy; missing/out-of-map moves use median map segment attributes. "
            "No-route responses have no energy estimate."
        ),
    }
    audit_path = output_dir / "failure_energy_audit.json"
    _write_json(audit_path, report)

    manifest = _consolidate_existing_output(output_dir, model_requested="deepseek-flash", map_size=100)
    manifest["failure_energy_audit"] = "failure_energy_audit.json"
    manifest["failure_reason_counts"] = dict(global_reasons)
    manifest["move_violation_counts"] = dict(global_violations)
    manifest["complete_sequence_violation_counts"] = dict(complete_violations)
    manifest["partial_response_violation_counts"] = dict(partial_violations)
    manifest["api_response_counts"] = dict(global_response_states)
    _write_json(output_dir / "manifest.json", manifest)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--experiment-dir",
        default="outputs/route_layer/pure_llm_experiment_v7",
    )
    args = parser.parse_args()
    report = audit_experiment(Path(args.experiment_dir))
    print(
        json.dumps(
            {
                "audit_json": str(Path(args.experiment_dir) / "failure_energy_audit.json"),
                "result_count": report["result_count"],
                "failure_reason_counts": report["failure_reason_counts"],
                "move_violation_counts": report["move_violation_counts"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
