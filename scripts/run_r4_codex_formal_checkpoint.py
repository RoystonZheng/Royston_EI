#!/usr/bin/env python3
"""Run one frozen R4 semantic order through local intent + one Codex route proposal.

The preflight command performs no model request. The execution command sends at
most one route proposal, makes no retry, and always records the deterministic
per-edge validator output and replay artifacts.
"""

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.costs import evaluate_route_candidate
from route_experiment.failure_audit import describe_failure_zh, estimate_route_proposal_energy
from route_experiment.models import (
    Edge,
    EvaluationConfig,
    Node,
    PackageConfig,
    RoadMap,
    RoadsideFeature,
    RouteCandidate,
    VehicleOrderTask,
)
from route_experiment.r4_compact_route import (
    build_r4_compact_route_messages,
    make_r4_compact_route_constraint,
    validate_r4_compact_route_proposal,
)
from route_experiment.routing import build_route_input_package, route_chain_reachability


SEED_DIR = ROOT / "outputs/route_layer/traditional_experiment_v1/seeds/seed_0"
INTENT_RESULT = ROOT / "outputs/route_layer/r4_qwen_intent_local_smoke_v1/seed_0/task-0000-0009/result.json"
OUTPUT_ROOT = ROOT / "outputs/route_layer/r4_codex_formal_checkpoint_v1"
SEED_OUTPUT = OUTPUT_ROOT / "seeds/seed_0"
TASK_ID = "task-0000-0009"
MODEL = "gpt-5.6-luna"
MAX_ROUTE_EDGES = 512
ELECTRICITY_CNY_PER_KWH = 2.29


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_map(path: Path) -> RoadMap:
    raw = read_json(path)
    return RoadMap(
        map_id=raw["map_id"],
        version=raw["version"],
        size=int(raw["size"]),
        nodes=[Node(**item) for item in raw["nodes"]],
        edges=[Edge(**item) for item in raw["edges"]],
        boundary=dict(raw["boundary"]),
        road_state_version=raw.get("road_state_version", "road_state_v1"),
        roadside_features=[RoadsideFeature(**item) for item in raw.get("roadside_features", [])],
    )


def prepare():
    map_path = next(SEED_DIR.glob("map_*_seed_*.json"))
    task_path = next(SEED_DIR.glob("order_tasks_seed_*.json"))
    map_data = read_json(map_path)
    task_file = read_json(task_path)
    raw_task = next(row for row in task_file["tasks"] if row["task_id"] == TASK_ID)
    intent_result = read_json(INTENT_RESULT)
    intent = intent_result.get("validation", {})
    if not intent.get("valid"):
        raise RuntimeError("qwen_local_intent_validation_failed")
    road_map = load_map(map_path)
    # Build the route task only from planner-visible task fields and the
    # already validated endpoint intent. Evaluator truth is never copied here.
    task = VehicleOrderTask(
        task_id=raw_task["task_id"],
        vehicle_id=raw_task["vehicle_id"],
        order_id=raw_task["order_id"],
        start_node_id=raw_task["start_node_id"],
        pickup_node_id=intent["pickup_node_id"],
        dropoff_node_id=intent["dropoff_node_id"],
        pickup_text=raw_task.get("pickup_text"),
        dropoff_text=raw_task.get("dropoff_text"),
        task_type=raw_task["task_type"],
    )
    route_intent = {
        key: intent.get(key)
        for key in (
            "pickup_anchor_ref",
            "dropoff_anchor_ref",
            "relation",
            "side",
            "feature_evidence_refs",
            "resolution_mode",
        )
    }
    package = build_route_input_package(road_map, task, PackageConfig())
    reachable = route_chain_reachability(
        package.edges,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        package.blocked_edge_ids,
    )
    if not reachable["chain_reachable"]:
        raise RuntimeError("r4_route_preflight_chain_unreachable")
    messages = build_r4_compact_route_messages(
        package,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        route_intent=route_intent,
    )
    prompt = CodexCliRouteStepClient._render_prompt(messages).encode("utf-8")
    if b"true_pickup_node_id" in prompt or b"true_dropoff_node_id" in prompt or b"ground_truth" in prompt:
        raise RuntimeError("evaluator_truth_leaked_to_codex_route_prompt")
    constraint = make_r4_compact_route_constraint(MAX_ROUTE_EDGES)
    return {
        "map_path": map_path,
        "task_path": task_path,
        "map_data": map_data,
        "road_map": road_map,
        "task": task,
        "intent_result": intent_result,
        "route_intent": route_intent,
        "package": package,
        "reachable": reachable,
        "messages": messages,
        "prompt_bytes": prompt,
        "constraint": constraint,
    }


def preflight() -> Dict[str, Any]:
    item = prepare()
    schema_bytes = json.dumps(
        item["constraint"].schema, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")
    record = {
        "schema_version": "r4_codex_formal_single_task_preflight_v1",
        "status": "offline_preflight_passed_no_route_request",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "formal_batch_started": False,
        "map_id": item["road_map"].map_id,
        "task_id": item["task"].task_id,
        "task_type": item["task"].task_type,
        "map_sha256": sha256(item["map_path"].read_bytes()),
        "order_tasks_sha256": sha256(item["task_path"].read_bytes()),
        "intent_result_sha256": sha256(INTENT_RESULT.read_bytes()),
        "route_input_package_id": item["package"].package_id,
        "route_input_nodes": len(item["package"].nodes),
        "route_input_directed_edges": len(item["package"].edges),
        "route_chain_reachability": item["reachable"],
        "route_intent": item["route_intent"],
        "route_prompt_bytes": len(item["prompt_bytes"]),
        "route_prompt_sha256": sha256(item["prompt_bytes"]),
        "route_schema_bytes": len(schema_bytes),
        "route_schema_sha256": item["constraint"].schema_sha256,
        "max_route_edges": MAX_ROUTE_EDGES,
        "maximum_model_requests": 1,
        "retry_count": 0,
        "model": MODEL,
        "reasoning_effort": "low",
        "tool_events_allowed": False,
        "validator": "validate every proposed move against the full frozen directed map; reject the entire route on any violation",
        "evaluator_truth_in_route_prompt": False,
        "result_scope": "one frozen semantic order checkpoint; not a multi-seed result",
    }
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    write_json(OUTPUT_ROOT / "preflight.json", record)
    write_json(OUTPUT_ROOT / "route_messages.json", {"messages": item["messages"]})
    write_json(OUTPUT_ROOT / "route_schema.json", item["constraint"].schema)
    write_json(OUTPUT_ROOT / "state.json", {
        "experiment_id": "r4_codex_formal_checkpoint_v1",
        "status": record["status"],
        "route_requests_used": 0,
        "maximum_route_requests": 1,
        "retry_count": 0,
        "map_id": record["map_id"],
        "task_id": TASK_ID,
    })
    return record


def _make_trace(item, payload, audit, accepted: bool) -> Dict[str, Any]:
    task = item["task"]
    no_route_proposed = payload.get("status") == "failed" or not payload.get("move_sequence")
    attempted_nodes = list(audit.get("route_node_sequence", [] if no_route_proposed else [task.start_node_id]))
    prefix_nodes = list(audit.get("validated_prefix_node_sequence", [] if no_route_proposed else [task.start_node_id]))
    route_nodes = attempted_nodes if accepted else ([] if no_route_proposed else prefix_nodes)
    route_edges = list(audit.get("route_edge_sequence", [])) if accepted else list(audit.get("validated_prefix_edge_sequence", []))
    steps = list(audit.get("steps", []))
    violations = []
    for row in audit.get("violations", []):
        step = row.get("step")
        move = payload.get("move_sequence", "")
        violations.append({
            "step": step,
            "kind": row.get("kind", "route_invalid"),
            "from_node_id": row.get("from_node_id"),
            "to_node_id": row.get("to_node_id"),
            "move": move[step - 1] if isinstance(step, int) and 1 <= step <= len(move) else None,
            "edge_id": row.get("edge_id"),
        })
    route_status = "completed" if accepted else ("failed" if no_route_proposed else "invalid")
    if accepted:
        failure_reason = None
    elif no_route_proposed:
        reason_code = payload.get("failure_reason_code")
        failure_reason = {
            "unable_to_construct_route": "model_unable_to_construct_route",
            "no_directed_path": "model_reported_no_directed_path",
        }.get(reason_code, "model_route_failed")
    else:
        failure_reason = "llm_route_invalid"
    return {
        "schema_version": "route_search_trace_v1",
        "method_name": "r4_codex_cli",
        "algorithm": "llm",
        "status": route_status,
        "legs": [
            {
                "source_node_id": task.start_node_id,
                "target_node_id": task.pickup_node_id,
                "status": "completed" if accepted else "not_completed",
                "expanded_node_ids": [],
                "expanded_events": [],
            },
            {
                "source_node_id": task.pickup_node_id,
                "target_node_id": task.dropoff_node_id,
                "status": "completed" if accepted else "not_completed",
                "expanded_node_ids": [],
                "expanded_events": [],
            },
        ],
        "expanded_nodes": None,
        "route_node_sequence": route_nodes,
        "route_edge_sequence": route_edges,
        "attempted_move_sequence": payload.get("move_sequence", ""),
        "attempted_route_node_sequence": [] if no_route_proposed else attempted_nodes,
        "attempted_route_edge_sequence": [edge for edge in audit.get("proposed_edge_sequence", []) if edge],
        "validated_prefix_node_sequence": prefix_nodes,
        "validated_prefix_edge_sequence": list(audit.get("validated_prefix_edge_sequence", [])),
        "move_violations": violations,
        "move_count": audit.get("move_count", len(payload.get("move_sequence", ""))),
        "r4_step_audit": steps,
        "first_violation_step": audit.get("first_violation_step"),
        "model_self_check_all_pass": audit.get("model_self_check_all_pass"),
        "failure_reason": failure_reason,
    }


def execute_once() -> Dict[str, Any]:
    state_path = OUTPUT_ROOT / "state.json"
    if not state_path.is_file():
        raise RuntimeError("run_offline_preflight_first")
    state = read_json(state_path)
    if state.get("status") != "offline_preflight_passed_no_route_request":
        raise RuntimeError("checkpoint_state_not_ready")
    if (OUTPUT_ROOT / "attempt_01.json").exists():
        raise RuntimeError("route_request_already_attempted_no_retry")

    item = prepare()
    client = CodexCliRouteStepClient(
        model=MODEL,
        trace_dir=OUTPUT_ROOT / "traces",
        reasoning_effort="low",
        timeout_sec=300,
        route_constraint=item["constraint"],
        persist_session=False,
        session_id=None,
    )
    attempt: Dict[str, Any] = {
        "request_number": 1,
        "retry_count": 0,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": TASK_ID,
        "map_id": item["road_map"].map_id,
        "route_prompt_sha256": sha256(item["prompt_bytes"]),
        "route_schema_sha256": item["constraint"].schema_sha256,
        "status": "request_in_progress",
    }
    state.update({"status": "request_in_progress", "route_requests_used": 1})
    write_json(state_path, state)
    response_received = False
    payload: Dict[str, Any] = {}
    try:
        response = client.chat(item["messages"])
        response_received = True
        payload = json.loads(response.content)
        attempt.update({
            "status": "response_received",
            "response": payload,
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
    attempt["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(OUTPUT_ROOT / "attempt_01.json", attempt)

    validation_audit = None
    route_candidate = None
    evaluation = None
    proposal_energy = None
    if response_received:
        validation_audit = validate_r4_compact_route_proposal(
            item["road_map"],
            start_node_id=item["task"].start_node_id,
            pickup_node_id=item["task"].pickup_node_id,
            dropoff_node_id=item["task"].dropoff_node_id,
            payload=payload,
        )
        if validation_audit.get("accepted"):
            route_candidate = RouteCandidate(
                route_id="r4_codex_cli-" + TASK_ID,
                node_sequence=list(validation_audit["route_node_sequence"]),
                edge_sequence=list(validation_audit["route_edge_sequence"]),
                route_source="r4_codex_cli",
                route_reachability=True,
                validation_status="valid",
            )
            evaluation = evaluate_route_candidate(item["road_map"], route_candidate, EvaluationConfig())
        elif payload.get("move_sequence"):
            proposal_energy = estimate_route_proposal_energy(
                item["road_map"],
                {
                    "route_representation": "node_sequence",
                    "attempted_route_node_sequence": validation_audit.get("route_node_sequence", []),
                    "attempted_route_edge_sequence": validation_audit.get("proposed_edge_sequence", []),
                    "validated_prefix_node_sequence": validation_audit.get("validated_prefix_node_sequence", []),
                    "validated_prefix_edge_sequence": validation_audit.get("validated_prefix_edge_sequence", []),
                },
                EvaluationConfig(),
            )
        elif payload.get("status") == "failed":
            proposal_energy = estimate_route_proposal_energy(
                item["road_map"],
                {
                    "route_representation": "node_sequence",
                    "attempted_route_node_sequence": [],
                    "attempted_route_edge_sequence": [],
                    "validated_prefix_node_sequence": [],
                    "validated_prefix_edge_sequence": [],
                },
                EvaluationConfig(),
            )
            proposal_energy["interpretation"] = (
                "模型返回失败且没有移动序列；行驶能耗未评估，不能解释为零能耗。"
            )

    trace = _make_trace(item, payload, validation_audit or {}, bool(validation_audit and validation_audit.get("accepted")))
    provider = attempt.get("provider_record", {})
    usage = attempt.get("usage", {})
    model_status = attempt["status"]
    completed = bool(validation_audit and validation_audit.get("accepted"))
    if completed:
        status = "completed"
        failure_reason = None
        validation = {"valid": True, "status": "valid", "errors": [], "direction_valid": True, "legal_pickup": True, "legal_dropoff": True}
    elif response_received:
        status = "failed"
        failure_reason = "llm_route_invalid" if payload.get("status") == "completed" else "model_unable_to_construct_route"
        validation = {
            "valid": False,
            "status": validation_audit.get("status", "invalid") if validation_audit else "invalid",
            "errors": [row.get("kind", "route_invalid") for row in (validation_audit or {}).get("violations", [])],
            "direction_valid": not any(row.get("kind") in {"one_way_direction", "missing_directed_edge", "blocked_edge"} for row in (validation_audit or {}).get("violations", [])),
        }
    else:
        status = "failed"
        failure_reason = "codex_cli_request_failed"
        validation = {"valid": False, "status": "not_evaluated", "errors": [attempt.get("error", "request_failed")]}

    energy = evaluation.energy_total_kwh if evaluation else None
    billable_energy = max(0.0, float(energy)) if isinstance(energy, (int, float)) else None
    electricity_cost = billable_energy * ELECTRICITY_CNY_PER_KWH if billable_energy is not None else None
    method_result = {
        "task_id": TASK_ID,
        "method_name": "r4_codex_cli",
        "status": status,
        "routes": [route_candidate.to_dict()] if route_candidate else [],
        "best_route_id": route_candidate.route_id if route_candidate else None,
        "valid_route_count": 1 if route_candidate else 0,
        "invalid_route_count": int(
            response_received and payload.get("status") == "completed" and not completed
        ),
        "failure_reason": failure_reason,
        "validation": validation,
        "evaluation": evaluation.to_dict() if evaluation else None,
        "resources": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "llm_calls": int(provider.get("status") != "not_called"),
            "llm_latency_ms": usage.get("latency_ms"),
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
            "llm_response_received": response_received,
            "account_billing_status": "not_reported_by_codex_cli",
            "codex_usage_cost_estimate": provider.get("cost_estimate"),
            "price_source": "Tesla China official Supercharger station reference",
            "price_source_url": "https://www.tesla.cn/findus/location/supercharger/cnsc9691?region=CN",
            "electricity_price_cny_per_kwh": ELECTRICITY_CNY_PER_KWH,
            "cost_status": "energy_evaluated_api_and_compute_amount_unreported" if completed else "route_energy_not_evaluated",
            "failure_detail_zh": (
                ""
                if completed
                else describe_failure_zh({"failure_reason": failure_reason})
                if response_received and payload.get("status") == "failed"
                else "模型输出路线提议，但本地逐边校验拒绝；路线与逐步违规信息见下方审计。"
                if response_received
                else "Codex CLI 未产生可校验模型响应；本次零重试停止。"
            ),
            "route_intent": item["route_intent"],
            "route_proposal": payload if response_received else None,
            "proposal_energy_audit": proposal_energy,
            "search_trace": trace,
            "route_validator_audit": validation_audit,
            "request_attempt": 1,
            "request_retry_count": 0,
            "output_token_hard_limit": None,
            "output_size_bound": "strict schema: at most 512 moves plus 512 aligned self-check markers",
            "qwen_intent_latency_ms": item["intent_result"].get("latency_ms"),
            "qwen_local_token_usage": "not_reported_by_local_runner",
            "route_backend": "Codex CLI account; no provider API key passed",
        },
        "raw_output": payload if response_received else {"error": attempt.get("error")},
    }
    order_payload = {
        "schema_version": "r4_codex_formal_single_task_results_v1",
        "map_id": item["road_map"].map_id,
        "map_version": item["road_map"].version,
        "random_seed": 0,
        "methods": ["r4_codex_cli"],
        "input_type": "frozen_traditional_experiment_v1_seed_0",
        "run_type": "single_formal_checkpoint_not_batch",
        "results": [{"task_id": TASK_ID, "task_type": item["task"].task_type, "methods": [method_result]}],
    }
    SEED_OUTPUT.mkdir(parents=True, exist_ok=True)
    for source in (
        item["map_path"],
        item["task_path"],
        SEED_DIR / "formal_scenarios_seed_0.json",
    ):
        destination = SEED_OUTPUT / source.name
        if not destination.exists():
            shutil.copy2(source, destination)
        elif sha256(destination.read_bytes()) != sha256(source.read_bytes()):
            raise RuntimeError("frozen_input_copy_hash_mismatch")
    routes_path = SEED_OUTPUT / "r4_codex_order_routes_costs_seed_0.json"
    write_json(routes_path, order_payload)
    write_json(SEED_OUTPUT / "r4_codex_formal_routes_costs_seed_0.json", {
        "schema_version": "r4_codex_formal_single_task_empty_formal_scope_v1",
        "map_id": item["road_map"].map_id,
        "random_seed": 0,
        "methods": ["r4_codex_cli"],
        "run_type": "not_run_single_order_checkpoint",
        "results": [],
    })

    from scripts.render_traditional_traces import render
    from scripts.render_seed_dashboard import render_seed_dashboard
    render(item["map_path"], routes_path, SEED_OUTPUT / "visualizations/orders")
    dashboard = render_seed_dashboard(SEED_OUTPUT)
    root_index = OUTPUT_ROOT / "index.html"
    root_index.write_text(
        '<!doctype html><meta charset="utf-8"><title>R4 Codex 单任务检查点</title>'
        '<h1>R4 Codex 单任务检查点</h1><p>只包含 seed 0 的一个冻结订单，不是正式批次结果。</p>'
        '<p><a href="seeds/seed_0/index.html">查看 seed 0 地图与路线结果</a> · '
        '<a href="seeds/seed_0/visualizations/orders/index.html">查看该订单逐步回放索引</a></p>',
        encoding="utf-8",
    )
    final_status = "completed_single_task_checkpoint" if completed else "failed_single_task_checkpoint"
    write_json(state_path, {
        "experiment_id": "r4_codex_formal_checkpoint_v1",
        "status": final_status,
        "route_requests_used": 1,
        "maximum_route_requests": 1,
        "retry_count": 0,
        "map_id": item["road_map"].map_id,
        "task_id": TASK_ID,
        "model_response_received": response_received,
        "route_validated": completed,
        "result_path": str(routes_path.relative_to(ROOT)),
        "replay_index": str((SEED_OUTPUT / "visualizations/orders/index.html").relative_to(ROOT)),
        "seed_dashboard": str(dashboard.relative_to(ROOT)),
        "root_index": str(root_index.relative_to(ROOT)),
    })
    return read_json(state_path)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight-only", action="store_true")
    group.add_argument("--execute-once", action="store_true")
    args = parser.parse_args()
    result = preflight() if args.preflight_only else execute_once()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
