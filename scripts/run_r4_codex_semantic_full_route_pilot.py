#!/usr/bin/env python3
"""Run at most one authorized Codex request for the frozen semantic R4 pilot.

Default mode is offline-only. A model call requires --send-one-request, and a
successful or failed attempt is terminal: the output directory prevents retry.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.costs import evaluate_route_candidate
from route_experiment.failure_audit import describe_failure_zh, estimate_route_proposal_energy
from route_experiment.models import (
    EvaluationConfig,
    PackageConfig,
    RouteCandidate,
    VehicleOrderTask,
)
from route_experiment.r4_compact_route import (
    make_r4_compact_route_constraint,
    validate_r4_compact_route_proposal,
)
from route_experiment.routing import build_route_input_package
from scripts.prepare_r4_codex_semantic_full_route_pilot import (
    DEFAULT_INTENT_ARTIFACT,
    DEFAULT_INTENT_ROOT,
    DEFAULT_PURE_LLM_ROOT,
    DEFAULT_SEED_ROOT,
    MAX_ROUTE_EDGES,
    _feature_context,
    _intent_source_audit,
    _road_feature_protocol,
    prepare,
    read_json,
    sha256_file,
)
from scripts.run_r4_codex_formal_checkpoint import load_map, sha256, write_json


DEFAULT_PREFLIGHT_DIR = ROOT / "outputs/route_layer/r4_codex_semantic_full_route_pilot_preflight_20261023_v3"
DEFAULT_OUTPUT_DIR = ROOT / "outputs/route_layer/r4_codex_semantic_full_route_pilot_v1"
MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "low"
ELECTRICITY_CNY_PER_KWH = 2.29


def _load_current_inputs(preflight_dir: Path) -> Dict[str, Any]:
    """Rebuild the frozen request and reject stale/modified preflight files."""
    saved = read_json(preflight_dir / "preflight.json")
    if saved.get("schema_version") != "r4_codex_semantic_full_route_pilot_preflight_v2":
        raise RuntimeError("pilot_preflight_schema_version_mismatch")
    if saved.get("model_request_sent") is not False or saved.get("retry_count") != 0:
        raise RuntimeError("pilot_preflight_already_used_or_retry_policy_invalid")

    rebuilt = prepare(
        DEFAULT_SEED_ROOT,
        DEFAULT_PURE_LLM_ROOT,
        DEFAULT_INTENT_ROOT,
        DEFAULT_INTENT_ARTIFACT,
        int(saved["seed"]),
        saved["task_id"],
    )
    for key in ("map_sha256", "order_file_sha256", "prompt_sha256", "schema_sha256"):
        if rebuilt.get(key) != saved.get(key):
            raise RuntimeError("pilot_preflight_stale_" + key)
    if rebuilt.get("evaluator_truth_absent_from_prompt") is not True:
        raise RuntimeError("pilot_prompt_truth_isolation_failed")
    if rebuilt["independent_reference_audit_not_sent_to_model"].get("full_route_validator_accepted") is not True:
        raise RuntimeError("pilot_reference_route_validator_gate_failed")

    seed = int(saved["seed"])
    seed_dir = DEFAULT_SEED_ROOT / f"seed_{seed}"
    map_path = seed_dir / f"map_100x100_seed_{seed}.json"
    order_path = seed_dir / f"order_tasks_seed_{seed}.json"
    order_data = read_json(order_path)
    raw_task = next(item for item in order_data["tasks"] if item["task_id"] == saved["task_id"])
    intent_data = read_json(DEFAULT_INTENT_ARTIFACT)
    intent = next(
        item for item in intent_data["records"]
        if int(item["seed"]) == seed
        and item["task_id"] == saved["task_id"]
        and item["map_input_mode"] == "road_feature_protocol_v1"
    )
    if not _intent_source_audit(intent, raw_task, DEFAULT_INTENT_ROOT).get("passed"):
        raise RuntimeError("pilot_qwen_intent_source_recheck_failed")
    if intent.get("uses_evaluator_truth") is not False or not intent.get("route_eligible"):
        raise RuntimeError("pilot_qwen_intent_not_route_eligible")

    task = VehicleOrderTask(
        task_id=raw_task["task_id"],
        vehicle_id=raw_task.get("vehicle_id", "r4-pilot"),
        order_id=raw_task.get("order_id", raw_task["task_id"]),
        start_node_id=raw_task["start_node_id"],
        pickup_node_id=intent["pickup_node_id"],
        dropoff_node_id=intent["dropoff_node_id"],
        pickup_text=raw_task.get("pickup_text"),
        dropoff_text=raw_task.get("dropoff_text"),
        task_type=raw_task.get("task_type", "semantic_place"),
    )
    road_map = load_map(map_path)
    package = build_route_input_package(road_map, task, PackageConfig())
    protocol = _road_feature_protocol(road_map, task)
    protocol.context["random_seed"] = seed
    feature_context = _feature_context(protocol, intent["route_intent"])
    constraint = make_r4_compact_route_constraint(MAX_ROUTE_EDGES)
    if constraint.schema != rebuilt["schema"]:
        raise RuntimeError("pilot_schema_rebuild_mismatch")
    return {
        "saved_preflight": saved,
        "messages": rebuilt["messages"],
        "schema": rebuilt["schema"],
        "constraint": constraint,
        "road_map": road_map,
        "package": package,
        "map_path": map_path,
        "order_path": order_path,
        "raw_task": raw_task,
        "task": task,
        "route_intent": intent["route_intent"],
        "feature_context": feature_context,
        "intent_record": intent,
    }


def finalize_proposal(
    road_map,
    task: VehicleOrderTask,
    payload: Any,
    *,
    blocked_edge_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    """Validate, score, and retain proposed-route energy separately."""
    validation = validate_r4_compact_route_proposal(
        road_map,
        start_node_id=task.start_node_id,
        pickup_node_id=task.pickup_node_id,
        dropoff_node_id=task.dropoff_node_id,
        payload=payload,
        blocked_edge_ids=blocked_edge_ids,
    )
    accepted = bool(validation.get("accepted"))
    candidate: Optional[RouteCandidate] = None
    evaluation = None
    proposal_energy = None
    if accepted:
        candidate = RouteCandidate(
            route_id="r4_codex_cli-" + task.task_id,
            node_sequence=list(validation["route_node_sequence"]),
            edge_sequence=list(validation["route_edge_sequence"]),
            route_source="r4_codex_cli",
            route_reachability=True,
            validation_status="valid",
        )
        evaluation = evaluate_route_candidate(road_map, candidate, EvaluationConfig())
    elif isinstance(payload, dict) and payload.get("move_sequence"):
        proposal_energy = estimate_route_proposal_energy(
            road_map,
            {
                "route_representation": "node_sequence",
                "attempted_route_node_sequence": validation.get("route_node_sequence", []),
                "attempted_route_edge_sequence": validation.get("proposed_edge_sequence", []),
                "validated_prefix_node_sequence": validation.get("validated_prefix_node_sequence", []),
                "validated_prefix_edge_sequence": validation.get("validated_prefix_edge_sequence", []),
            },
            EvaluationConfig(),
        )
    elif isinstance(payload, dict) and payload.get("status") == "failed":
        proposal_energy = estimate_route_proposal_energy(
            road_map,
            {
                "route_representation": "node_sequence",
                "attempted_route_node_sequence": [],
                "attempted_route_edge_sequence": [],
                "validated_prefix_node_sequence": [],
                "validated_prefix_edge_sequence": [],
            },
            EvaluationConfig(),
        )
        proposal_energy["interpretation"] = "模型未提出路线；行驶能耗未评估，不能解释为零能耗。"

    trace = _make_trace(task, payload, validation, accepted)
    failure_reason = None
    if not accepted:
        if isinstance(payload, dict) and payload.get("status") == "failed":
            failure_reason = {
                "no_directed_path": "model_reported_no_directed_path",
                "unable_to_construct_route": "model_unable_to_construct_route",
            }.get(payload.get("failure_reason_code"), "model_route_failed")
        elif isinstance(payload, dict) and payload.get("status") == "completed":
            failure_reason = "llm_route_invalid"
        else:
            failure_reason = "llm_output_parse_failed"

    energy = evaluation.energy_total_kwh if evaluation else None
    billable_energy = max(0.0, float(energy)) if isinstance(energy, (int, float)) else None
    electricity_cost = billable_energy * ELECTRICITY_CNY_PER_KWH if billable_energy is not None else None
    method_result = {
        "task_id": task.task_id,
        "method_name": "r4_codex_cli",
        "status": "completed" if accepted else "failed",
        "routes": [candidate.to_dict()] if candidate else [],
        "best_route_id": candidate.route_id if candidate else None,
        "valid_route_count": 1 if candidate else 0,
        "invalid_route_count": int(
            isinstance(payload, dict) and payload.get("status") == "completed" and not accepted
        ),
        "failure_reason": failure_reason,
        "validation": {
            "valid": accepted,
            "status": "valid" if accepted else validation.get("status", "not_evaluated"),
            "errors": [row.get("kind", "route_invalid") for row in validation.get("violations", [])],
        },
        "evaluation": evaluation.to_dict() if evaluation else None,
        "resources": {
            "input_tokens": None,
            "output_tokens": None,
            "llm_calls": 0,
            "llm_latency_ms": None,
            "compute_time_ms": None,
            "energy_kwh": energy,
        },
        "monetary_cost": {
            "api_cost_amount": None,
            "compute_cost_amount": None,
            "electricity_cost_amount": electricity_cost,
            "billable_energy_kwh": billable_energy,
            "energy_cost_basis": "max(net_route_leg_energy_kwh, 0) * configured_electricity_price_per_kwh",
            "total_monetary_cost": None,
            "currency": "CNY",
        },
        "metadata": {
            "compute_time_ms": None,
            "llm_response_received": False,
            "account_billing_status": "not_reported_by_codex_cli",
            "failure_detail_zh": "" if accepted else describe_failure_zh({"failure_reason": failure_reason}, trace),
            "route_intent": None,
            "route_proposal": payload,
            "proposal_energy_audit": proposal_energy,
            "search_trace": trace,
            "route_validator_audit": validation,
            "request_attempt": 1,
            "request_retry_count": 0,
            "route_backend": "Codex CLI account; no provider API key passed",
            "output_token_hard_limit": None,
            "output_size_bound": "strict schema: at most 512 moves plus 512 aligned self-check markers",
            "electricity_price_cny_per_kwh": ELECTRICITY_CNY_PER_KWH,
            "cost_status": "energy_evaluated_api_amount_unreported" if accepted else "route_energy_not_evaluated_or_proposal_estimate_only",
        },
        "raw_output": payload,
    }
    return {
        "validation_audit": validation,
        "route_candidate": candidate,
        "evaluation": evaluation,
        "proposal_energy_audit": proposal_energy,
        "trace": trace,
        "method_result": method_result,
    }


def _make_trace(task: VehicleOrderTask, payload: Any, audit: Dict[str, Any], accepted: bool) -> Dict[str, Any]:
    obj = payload if isinstance(payload, dict) else {}
    moves = obj.get("move_sequence", "") if isinstance(obj.get("move_sequence", ""), str) else ""
    no_route = obj.get("status") == "failed" or not moves
    attempted_nodes = list(audit.get("route_node_sequence", []))
    prefix_nodes = list(audit.get("validated_prefix_node_sequence", [task.start_node_id]))
    violations = []
    for row in audit.get("violations", []):
        step = row.get("step")
        violations.append({
            "step": step,
            "kind": row.get("kind", "route_invalid"),
            "from_node_id": row.get("from_node_id"),
            "to_node_id": row.get("to_node_id"),
            "move": moves[step - 1] if isinstance(step, int) and 1 <= step <= len(moves) else None,
            "edge_id": row.get("edge_id"),
        })
    return {
        "schema_version": "route_search_trace_v1",
        "method_name": "r4_codex_cli",
        "algorithm": "llm",
        "status": "completed" if accepted else ("failed" if no_route else "invalid"),
        "legs": [
            {"source_node_id": task.start_node_id, "target_node_id": task.pickup_node_id, "status": "completed" if accepted else "not_completed", "expanded_node_ids": [], "expanded_events": []},
            {"source_node_id": task.pickup_node_id, "target_node_id": task.dropoff_node_id, "status": "completed" if accepted else "not_completed", "expanded_node_ids": [], "expanded_events": []},
        ],
        "expanded_nodes": None,
        "route_node_sequence": attempted_nodes if accepted else ([] if no_route else prefix_nodes),
        "route_edge_sequence": list(audit.get("route_edge_sequence", [])) if accepted else list(audit.get("validated_prefix_edge_sequence", [])),
        "attempted_move_sequence": moves,
        "attempted_route_node_sequence": [] if no_route else attempted_nodes,
        "attempted_route_edge_sequence": [edge for edge in audit.get("proposed_edge_sequence", []) if edge],
        "validated_prefix_node_sequence": prefix_nodes,
        "validated_prefix_edge_sequence": list(audit.get("validated_prefix_edge_sequence", [])),
        "move_violations": violations,
        "move_count": audit.get("move_count", len(moves)),
        "r4_step_audit": list(audit.get("steps", [])),
        "first_violation_step": audit.get("first_violation_step"),
        "model_self_check_all_pass": audit.get("model_self_check_all_pass"),
        "failure_reason": None if accepted else ("llm_output_parse_failed" if not isinstance(payload, dict) else "llm_route_invalid"),
    }


def run(
    *,
    preflight_dir: Path,
    output_dir: Path,
    send_one_request: bool = False,
    client_factory: Callable[..., CodexCliRouteStepClient] = CodexCliRouteStepClient,
) -> Dict[str, Any]:
    if not send_one_request:
        item = _load_current_inputs(preflight_dir)
        return {
            "status": "offline_ready_no_model_request_sent",
            "model_request_sent": False,
            "retry_count": 0,
            "task_id": item["saved_preflight"]["task_id"],
            "prompt_sha256": item["saved_preflight"]["prompt_sha256"],
            "output_dir": str(output_dir),
            "next_step_requires_explicit_send_flag": True,
        }
    if output_dir.exists():
        raise RuntimeError("pilot_output_exists_no_retry_or_overwrite")
    item = _load_current_inputs(preflight_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    trace_dir = output_dir / "model_trace"
    client = client_factory(
        model=MODEL,
        trace_dir=trace_dir,
        reasoning_effort=REASONING_EFFORT,
        timeout_sec=300,
        route_constraint=item["constraint"],
        persist_session=False,
        session_id=None,
    )
    attempt: Dict[str, Any] = {
        "request_number": 1,
        "retry_count": 0,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": item["task"].task_id,
        "map_id": item["road_map"].map_id,
        "prompt_sha256": item["saved_preflight"]["prompt_sha256"],
        "schema_sha256": item["saved_preflight"]["schema_sha256"],
        "status": "request_in_progress",
        "model_request_started": True,
    }
    # Persist the one-request reservation before touching the provider.
    write_json(output_dir / "state.json", {
        "experiment_id": "r4_codex_semantic_full_route_pilot_v1",
        "status": "request_in_progress",
        "request_count_reserved": 1,
        "maximum_requests": 1,
        "retry_count": 0,
    })
    response_received = False
    payload: Any = None
    raw_content = None
    try:
        response = client.chat(item["messages"])
        response_received = True
        raw_content = response.content
        try:
            payload = json.loads(response.content)
            attempt["status"] = "response_json_received"
        except (TypeError, json.JSONDecodeError):
            payload = {"status": "unparseable"}
            attempt["status"] = "response_parse_failed"
        attempt.update({
            "response": payload,
            "raw_response_content": raw_content,
            "provider_record": response.provider_audit,
            "usage": {
                "input_tokens": response.input_tokens,
                "cached_input_tokens": response.cached_input_tokens,
                "output_tokens": response.output_tokens,
                "latency_ms": response.latency_ms,
                "account_billing": "not_reported_by_codex_cli",
            },
        })
    except Exception as error:
        provider = dict(client.last_record)
        attempt.update({
            "status": "request_error_no_retry",
            "error_type": type(error).__name__,
            "error": CodexCliRouteStepClient._redact_diagnostics(str(error))[:500],
            "provider_record": provider,
            "usage": {
                "input_tokens": provider.get("input_tokens"),
                "cached_input_tokens": provider.get("cached_input_tokens"),
                "output_tokens": provider.get("output_tokens"),
                "latency_ms": provider.get("latency_ms"),
                "account_billing": "not_reported_by_codex_cli",
            },
        })
        payload = {"status": "unparseable"}
    attempt["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(output_dir / "attempt_01.json", attempt)

    result = finalize_proposal(
        item["road_map"],
        item["task"],
        payload,
        blocked_edge_ids=item["package"].blocked_edge_ids,
    )
    method = result["method_result"]
    usage = attempt.get("usage", {})
    provider = attempt.get("provider_record", {})
    method["resources"].update({
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "llm_calls": int(provider.get("status") != "not_called"),
        "llm_latency_ms": usage.get("latency_ms"),
    })
    method["metadata"].update({
        "llm_response_received": response_received,
        "account_billing_status": "not_reported_by_codex_cli",
        "codex_usage_cost_estimate": provider.get("cost_estimate"),
        "route_intent": item["route_intent"],
        "route_proposal": payload if response_received else None,
        "provider_record": provider,
        "request_attempt": 1,
        "request_retry_count": 0,
    })
    route_results = {
        "schema_version": "r4_codex_semantic_full_route_pilot_results_v1",
        "map_id": item["road_map"].map_id,
        "map_version": item["road_map"].version,
        "random_seed": int(item["saved_preflight"]["seed"]),
        "methods": ["r4_codex_cli"],
        "run_type": "single_task_smoke_not_formal_batch",
        "results": [{
            "task_id": item["task"].task_id,
            "task_type": item["task"].task_type,
            "methods": [method],
        }],
    }
    seed_dir = output_dir / f"seeds/seed_{item['saved_preflight']['seed']}"
    seed_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(item["map_path"], seed_dir / item["map_path"].name)
    shutil.copy2(item["order_path"], seed_dir / item["order_path"].name)
    formal_input = DEFAULT_SEED_ROOT / f"seed_{item['saved_preflight']['seed']}" / f"formal_scenarios_seed_{item['saved_preflight']['seed']}.json"
    if formal_input.is_file():
        shutil.copy2(formal_input, seed_dir / formal_input.name)
    routes_path = seed_dir / "r4_codex_order_routes_costs_seed_8.json"
    write_json(routes_path, route_results)
    write_json(seed_dir / "r4_codex_formal_routes_costs_seed_8.json", {
        "schema_version": "r4_codex_semantic_full_route_pilot_formal_scope_v1",
        "map_id": item["road_map"].map_id,
        "random_seed": int(item["saved_preflight"]["seed"]),
        "methods": ["r4_codex_cli"],
        "run_type": "not_run_single_task_smoke",
        "results": [],
    })
    from scripts.render_traditional_traces import render as render_traces
    manifest = render_traces(
        seed_dir / item["map_path"].name,
        routes_path,
        seed_dir / "visualizations/orders",
    )
    from scripts.render_seed_dashboard import render_seed_dashboard
    dashboard_path = render_seed_dashboard(seed_dir)
    summary = {
        "experiment_id": "r4_codex_semantic_full_route_pilot_v1",
        "status": "completed_single_task_smoke" if method["status"] == "completed" else "failed_single_task_smoke",
        "candidate_is_formal_result": False,
        "model_request_attempted": bool(attempt.get("model_request_started")),
        "model_request_calls_observed": int("call_index" in provider),
        "model_response_received": response_received,
        "retry_count": 0,
        "fallback_to_traditional_route": False,
        "task_id": item["task"].task_id,
        "map_id": item["road_map"].map_id,
        "route_status": method["status"],
        "failure_reason": method["failure_reason"],
        "usage": attempt.get("usage"),
        "validation": method["metadata"]["route_validator_audit"],
        "energy_kwh": method["resources"]["energy_kwh"],
        "proposal_energy_audit": method["metadata"]["proposal_energy_audit"],
        "replay_index": "seeds/seed_8/visualizations/orders/index.html",
        "seed_dashboard": str(dashboard_path.relative_to(output_dir)),
        "trace_count": manifest["trace_count"],
        "limitations": [
            "单任务 smoke 不构成正式实验或跨种子成功率结论。",
            "Codex CLI 未报告账户实扣；能耗只由共同路线评估器复算。",
            "任何无效路线均整条拒绝；不重试、不使用 Dijkstra/A* 兜底。",
        ],
    }
    write_json(output_dir / "result.json", route_results)
    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "state.json", {
        "experiment_id": summary["experiment_id"],
        "status": summary["status"],
        "request_count_reserved": 1,
        "requests_used": int("call_index" in provider),
        "maximum_requests": 1,
        "retry_count": 0,
        "route_validated": method["status"] == "completed",
    })
    rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in summary.items()
        if key not in {"validation", "proposal_energy_audit", "limitations"}
    )
    index_html = (
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>R4 单任务路线 smoke</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:28px;color:#172033}"
        "table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #ddd;padding:9px;text-align:left}"
        "th{width:220px;color:#59677e}</style><h1>R4 单任务整路线 smoke</h1>"
        "<p>这是单任务诊断，不是正式批次结果。原始请求、validator 明细、尝试路线能耗审计及回放均已保存。</p>"
        f"<table>{rows}</table><h2>查看回放</h2>"
        f"<p><a href='{html.escape(summary['seed_dashboard'])}'>打开 seed 8 原地图与结果总览</a> · "
        f"<a href='{html.escape(summary['replay_index'])}'>订单路线轨迹索引</a></p>"
        f"<p><a href='summary.json'>summary.json</a> · <a href='result.json'>result.json</a> · "
        f"<a href='attempt_01.json'>attempt_01.json</a></p></html>"
    )
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--send-one-request",
        action="store_true",
        help="Send exactly one Codex CLI route request; zero retries; only run after explicit user authorization.",
    )
    args = parser.parse_args()
    result = run(
        preflight_dir=args.preflight_dir,
        output_dir=args.output_dir,
        send_one_request=args.send_one_request,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
