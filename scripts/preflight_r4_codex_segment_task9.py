#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a zero-request R4 segmented-route protocol preflight for frozen seed-0 task9."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.r4_segment import (
    build_r4_segment_messages,
    make_r4_segment_constraint,
    run_r4_segment_route_search,
    validate_r4_segment_proposal,
)
from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import EvaluationConfig, RouteCandidate
from route_experiment.r4_stepwise import (
    _minimum_remaining_move_lower_bound,
    audit_r4_stepwise_map_equivalence,
)
from run_r4_codex_formal_checkpoint import INTENT_RESULT, prepare
from render_r4_segment_replay import render_replay


STEPWISE_PREFLIGHT = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"
WITNESS_AUDIT = ROOT / "outputs/route_layer/r4_segment_v1_proposal_offline_gate_20261026_v1/summary.json"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_codex_segment_task9_preflight_v6"
MAX_SEGMENT_ACTIONS = 8
MAX_TOTAL_ACTIONS = 512
PROGRESS_MULTIPLIER = 2
MAX_FEEDBACK_REQUESTS = MAX_TOTAL_ACTIONS


class OfflineWitnessClient:
    """Feed a local witness through the route runner without a model call."""

    retries = 0

    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.calls = []
        self.route_constraint = None
        self.session_id = None

    def render_request_prompt(self, messages):
        if self.session_id is None:
            prompt = CodexCliRouteStepClient._render_prompt(messages)
            self.session_id = "offline-witness-session"
            return prompt
        return CodexCliRouteStepClient._render_resume_prompt(self, messages)

    def chat(self, messages):
        self.calls.append(messages)
        proposal = self.proposals.pop(0)
        return SimpleNamespace(
            content=json.dumps(proposal, ensure_ascii=False),
            model="offline_witness_fixture",
            request_id=None,
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            latency_ms=None,
            retries=0,
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _feature_flags_for_witness(
    road_map,
    current_node_id: str,
    direction: str,
    visited,
    pickup_node_id: str,
    dropoff_node_id: str,
    pickup_seen: bool,
):
    dxdy = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}
    nodes_by_id = {node.node_id: node for node in road_map.nodes}
    nodes_by_xy = {(node.x, node.y): node for node in road_map.nodes}
    node = nodes_by_id[current_node_id]
    dx, dy = dxdy[direction]
    target = nodes_by_xy.get((node.x + dx, node.y + dy))
    edge = next(
        (edge for edge in road_map.edges if target and edge.from_node == current_node_id and edge.to_node == target.node_id),
        None,
    )
    pickup_order_ok = bool(target and current_node_id != dropoff_node_id and not (target.node_id == dropoff_node_id and not pickup_seen))
    target_new = bool(target and target.node_id not in visited)
    return {
        "allowed_directed_edge": bool(edge and pickup_order_ok and target_new),
        "starts_at_current": bool(edge and edge.from_node == current_node_id),
        "inside_boundary": bool(target),
        "pickup_order_ok": pickup_order_ok,
        "target_node_is_new": target_new,
    }


def _offline_witness_segment_audit(
    item: Dict[str, Any],
    witness: Dict[str, Any],
    progress_limit: int,
    require_full_segments: bool = False,
) -> Dict[str, Any]:
    road_map = item["road_map"]
    task = item["task"]
    moves = witness["witness_move_sequence"]
    current = task.start_node_id
    visited = [current]
    pickup_seen = current == task.pickup_node_id
    committed_edges = []
    segment_rows = []
    for offset in range(0, len(moves), MAX_SEGMENT_ACTIONS):
        chunk = moves[offset : offset + MAX_SEGMENT_ACTIONS]
        actions = []
        trial_current = current
        trial_visited = list(visited)
        trial_pickup_seen = pickup_seen
        for direction in chunk:
            flags = _feature_flags_for_witness(
                road_map,
                trial_current,
                direction,
                trial_visited,
                task.pickup_node_id,
                task.dropoff_node_id,
                trial_pickup_seen,
            )
            actions.append({"direction": direction, "self_check": flags})
            target_node_id = None
            source = next(node for node in road_map.nodes if node.node_id == trial_current)
            dxdy = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}
            dx, dy = dxdy[direction]
            target = next((node for node in road_map.nodes if (node.x, node.y) == (source.x + dx, source.y + dy)), None)
            if target:
                target_node_id = target.node_id
                trial_current = target_node_id
                trial_visited.append(target_node_id)
                trial_pickup_seen = trial_pickup_seen or target_node_id == task.pickup_node_id
        remaining_lower_bound = _minimum_remaining_move_lower_bound(
            item["package"].edges,
            current,
            task.pickup_node_id,
            task.dropoff_node_id,
            pickup_seen,
            visited,
            item["package"].blocked_edge_ids,
        )
        if remaining_lower_bound is None:
            raise RuntimeError("offline_witness_remaining_route_unreachable")
        effective_max_segment_actions = min(MAX_SEGMENT_ACTIONS, progress_limit - len(committed_edges))
        if require_full_segments:
            effective_max_segment_actions = min(effective_max_segment_actions, remaining_lower_bound)
        minimum_segment_actions = effective_max_segment_actions if require_full_segments else 0
        audit = validate_r4_segment_proposal(
            road_map,
            start_node_id=task.start_node_id,
            pickup_node_id=task.pickup_node_id,
            dropoff_node_id=task.dropoff_node_id,
            current_node_id=current,
            pickup_seen=pickup_seen,
            visited_node_ids=visited,
            payload={"actions": actions},
            accepted_action_count=len(committed_edges),
            progress_limit=progress_limit,
            max_segment_actions=effective_max_segment_actions,
            min_segment_actions=minimum_segment_actions,
            allowed_edge_ids=(edge.edge_id for edge in item["package"].edges),
        )
        segment_rows.append({
            "segment_number": len(segment_rows) + 1,
            "move_offset_start": offset,
            "move_count": len(chunk),
            "accepted": audit["accepted"],
            "route_complete": audit["route_complete"],
            "first_violation_step": audit["first_violation_step"],
            "violations": audit["violations"],
        })
        if not audit["accepted"]:
            return {
                "status": "offline_witness_segment_rejected",
                "accepted_segment_count": sum(row["accepted"] for row in segment_rows),
                "segment_count": len(segment_rows),
                "segments": segment_rows,
                "final_node_id": current,
                "committed_move_count": len(committed_edges),
                "witness_input_leaked_to_model": False,
            }
        current = audit["current_node_after_segment"]
        visited = audit["visited_after_segment"]
        pickup_seen = audit["pickup_seen_after_segment"]
        committed_edges.extend(audit["committed_edge_sequence"])
    return {
        "status": "offline_witness_passed" if current == task.dropoff_node_id and pickup_seen else "offline_witness_incomplete",
        "accepted_segment_count": sum(row["accepted"] for row in segment_rows),
        "segment_count": len(segment_rows),
        "segments": segment_rows,
        "final_node_id": current,
        "committed_move_count": len(committed_edges),
        "committed_edge_count": len(committed_edges),
        "witness_input_leaked_to_model": False,
        "scope": "Offline validator compatibility only; the local witness is not a model result and is never placed in prompt or schema.",
    }


def _build_witness_proposals(item: Dict[str, Any], witness: Dict[str, Any]):
    road_map = item["road_map"]
    task = item["task"]
    nodes_by_id = {node.node_id: node for node in road_map.nodes}
    nodes_by_xy = {(node.x, node.y): node for node in road_map.nodes}
    delta = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}
    current = task.start_node_id
    visited = [current]
    pickup_seen = current == task.pickup_node_id
    proposals = []
    moves = witness["witness_move_sequence"]
    for offset in range(0, len(moves), MAX_SEGMENT_ACTIONS):
        actions = []
        for direction in moves[offset : offset + MAX_SEGMENT_ACTIONS]:
            flags = _feature_flags_for_witness(
                road_map,
                current,
                direction,
                visited,
                task.pickup_node_id,
                task.dropoff_node_id,
                pickup_seen,
            )
            actions.append({"direction": direction, "self_check": flags})
            node = nodes_by_id[current]
            dx, dy = delta[direction]
            target = nodes_by_xy.get((node.x + dx, node.y + dy))
            if target is None:
                raise RuntimeError("offline_witness_contains_missing_node")
            current = target.node_id
            visited.append(current)
            pickup_seen = pickup_seen or current == task.pickup_node_id
        proposals.append({"actions": actions})
    return proposals


def _exercise_runner_offline(
    item,
    witness,
    feature_context,
    initial_lower_bound,
    progress_limit,
    max_requests,
    *,
    max_feedback_requests=MAX_FEEDBACK_REQUESTS,
    require_full_segments=False,
):
    """Exercise the full runner with local fixtures; never calls Codex."""
    client = OfflineWitnessClient(_build_witness_proposals(item, witness))
    result = run_r4_segment_route_search(
        client,
        item["package"],
        item["road_map"],
        item["task"],
        item["task"].pickup_node_id,
        item["task"].dropoff_node_id,
        max_api_calls=max_requests,
        max_segment_actions=MAX_SEGMENT_ACTIONS,
        max_total_actions=MAX_TOTAL_ACTIONS,
        max_progress_multiplier=PROGRESS_MULTIPLIER,
        max_feedback_requests=max_feedback_requests,
        require_full_segments=require_full_segments,
        route_feature_context=feature_context,
    )
    if (
        result["status"] != "completed_route"
        or result["route"]["move_count"] != int(witness["witness_edge_count"])
        or result["api_request_count"] != len(client.calls)
        or any(not row["accepted"] for row in result["segment_audits"])
    ):
        raise RuntimeError("offline_witness_full_runner_gate_failed")
    candidate = RouteCandidate(
        route_id="offline-witness-task9",
        node_sequence=result["route"]["node_sequence"],
        edge_sequence=result["route"]["edge_sequence"],
        route_source="offline_validator_fixture",
        route_reachability=True,
        validation_status="valid",
    )
    evaluation = evaluate_route_candidate(item["road_map"], candidate, EvaluationConfig())
    return {
        "scope": "offline_runner_fixture_only_not_model_result",
        "model_requests": 0,
        "simulated_response_count": len(client.calls),
        "status": result["status"],
        "route_complete": result["route_complete"],
        "initial_move_lower_bound": int(initial_lower_bound),
        "progress_limit": progress_limit,
        "move_count": result["route"]["move_count"],
        "distance_m": evaluation.route_distance_m,
        "travel_time_sec": evaluation.total_travel_time_sec,
        "energy_kwh": evaluation.energy_total_kwh,
        "energy_model_version": evaluation.energy_model_version,
        "segment_count": result["api_request_count"],
        "max_segment_actions": MAX_SEGMENT_ACTIONS,
        "usage": result["usage"],
        "route": result["route"],
        "attempts": result["attempts"],
        "segment_audits": result["segment_audits"],
    }


def build_preflight(
    output_dir: Path = DEFAULT_OUTPUT,
    *,
    session_mode: str = "persistent_per_task",
    max_feedback_requests: int = MAX_FEEDBACK_REQUESTS,
    require_full_segments: bool = False,
) -> Dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError("segment_preflight_output_already_exists")
    if session_mode not in {"persistent_per_task", "ephemeral_per_request"}:
        raise ValueError("r4_segment_session_mode_invalid")
    if isinstance(max_feedback_requests, bool) or not 0 <= int(max_feedback_requests) <= MAX_TOTAL_ACTIONS:
        raise ValueError("r4_segment_feedback_limit_invalid")
    source = json.loads(STEPWISE_PREFLIGHT.read_text(encoding="utf-8"))
    if source.get("status") != "local_gate_passed_api_not_requested":
        raise RuntimeError("source_r4_stepwise_preflight_not_ready")
    item = prepare()
    task = item["task"]
    road_map = item["road_map"]
    package = item["package"]
    source_map_sha = source.get("shared_input_audit", {}).get("pure_llm_map_sha256")
    if not source_map_sha or source_map_sha != _sha256(item["map_path"]):
        raise RuntimeError("checkpoint_map_differs_from_segment_source")
    source_payload = json.loads(source["route_search"]["messages"][1]["content"])
    feature_context = source_payload.get("i")
    if not isinstance(feature_context, dict):
        raise RuntimeError("frozen_agent_readable_feature_context_missing")
    if source_payload.get("q", {}).get("p") != item["task"].pickup_node_id or source_payload.get("q", {}).get("d") != item["task"].dropoff_node_id:
        raise RuntimeError("frozen_semantic_endpoints_mismatch")

    initial_lower_bound = _minimum_remaining_move_lower_bound(
        package.edges,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        task.start_node_id == task.pickup_node_id,
        [task.start_node_id],
        package.blocked_edge_ids,
    )
    if initial_lower_bound is None or initial_lower_bound != source["route_search"]["reachability_preflight"].get("minimum_remaining_moves"):
        raise RuntimeError("task9_start_route_lower_bound_mismatch")
    progress_limit = min(MAX_TOTAL_ACTIONS, int(initial_lower_bound) * PROGRESS_MULTIPLIER)
    max_requests = int(math.ceil(progress_limit / MAX_SEGMENT_ACTIONS)) + int(max_feedback_requests)
    initial_segment_actions = min(MAX_SEGMENT_ACTIONS, progress_limit, int(initial_lower_bound))
    minimum_segment_actions = initial_segment_actions if require_full_segments else 0
    messages = build_r4_segment_messages(
        package,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        current_node_id=task.start_node_id,
        visited_node_ids=[task.start_node_id],
        accepted_directions=[],
        pickup_seen=task.start_node_id == task.pickup_node_id,
        max_segment_actions=initial_segment_actions,
        min_segment_actions=minimum_segment_actions,
        remaining_action_budget=progress_limit,
        accepted_action_count=0,
        progress_limit=progress_limit,
        route_feature_context=feature_context,
    )
    payload = json.loads(messages[1]["content"])
    map_audit = audit_r4_stepwise_map_equivalence(package, payload["m"])
    if not map_audit.get("passed"):
        raise RuntimeError("segment_compact_map_equivalence_failed")
    schema = make_r4_segment_constraint(initial_segment_actions, minimum_segment_actions)
    prompt = "\n\n".join(
        message["role"].upper() + ":\n" + message["content"] for message in messages
    )
    if any(marker in prompt for marker in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")):
        raise RuntimeError("evaluator_truth_leaked_to_segment_prompt")

    witness_data = json.loads(WITNESS_AUDIT.read_text(encoding="utf-8"))
    witness = next(
        row for row in witness_data["task_rows"]
        if int(row["seed"]) == 0 and row["task_id"] == task.task_id
    )
    if witness.get("witness_status") != "validated_witness":
        raise RuntimeError("offline_route_witness_missing")
    witness_audit = _offline_witness_segment_audit(
        item, witness, progress_limit, require_full_segments=require_full_segments
    )
    if witness_audit["status"] != "offline_witness_passed":
        raise RuntimeError("offline_witness_segment_gate_failed")
    runner_fixture = _exercise_runner_offline(
        item,
        witness,
        feature_context,
        int(initial_lower_bound),
        progress_limit,
        max_requests,
        max_feedback_requests=int(max_feedback_requests),
        require_full_segments=require_full_segments,
    )

    schema_bytes = json.dumps(schema.schema, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    prompt_bytes = prompt.encode("utf-8")
    output_dir.mkdir(parents=True)
    version_match = re.search(r"(?:^|[_-])v(\d+)(?:[_-]|$)", output_dir.name)
    version = int(version_match.group(1)) if version_match else 8
    record = {
        "schema_version": "r4_codex_segment_preflight_v3" if version >= 7 else "r4_codex_segment_preflight_v2",
        "experiment_id": f"r4_codex_segment_task9_preflight_v{version}",
        "segment_protocol": "r4-segment-v5",
        "status": "offline_segment_preflight_passed_no_model_requests",
        "formal_batch_started": False,
        "model_requests": 0,
        "map_id": road_map.map_id,
        "task_id": task.task_id,
        "task_type": task.task_type,
        "map_sha256": _sha256(item["map_path"]),
        "orders_sha256": _sha256(item["task_path"]),
        "intent_result_sha256": _sha256(INTENT_RESULT),
        "source_stepwise_preflight": str(STEPWISE_PREFLIGHT.relative_to(ROOT)),
        "source_stepwise_preflight_sha256": _sha256(STEPWISE_PREFLIGHT),
        "route_input_package_id": package.package_id,
        "route_input_nodes": len(package.nodes),
        "route_input_directed_edges": len(package.edges),
        "map_semantics_equivalence": map_audit,
        "route_feature_context_included": True,
        "route_feature_context_sha256": hashlib.sha256(
            json.dumps(feature_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "route_feature_context_source": "frozen stepwise preflight; no new Qwen call",
        "start_node_id": task.start_node_id,
        "pickup_node_id": task.pickup_node_id,
        "dropoff_node_id": task.dropoff_node_id,
        "initial_move_lower_bound": int(initial_lower_bound),
        "progress_limit": progress_limit,
        "max_segment_actions": MAX_SEGMENT_ACTIONS,
        "min_segment_actions_at_start": minimum_segment_actions,
        "require_full_segments": bool(require_full_segments),
        "max_total_actions": MAX_TOTAL_ACTIONS,
        "progress_multiplier": PROGRESS_MULTIPLIER,
        "max_feedback_requests": int(max_feedback_requests),
        "maximum_model_requests": max_requests,
        "network_retries": 0,
        "session_mode": session_mode,
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "tool_events_allowed": False,
        "output_token_hard_limit": None,
        "account_billing": "not_reported_by_codex_cli",
        "prompt_bytes": len(prompt_bytes),
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "schema_bytes": len(schema_bytes),
        "schema_sha256": schema.schema_sha256,
        "offline_witness_gate": witness_audit,
        "offline_full_runner_fixture": {
            "status": runner_fixture["status"],
            "model_requests": runner_fixture["model_requests"],
            "simulated_response_count": runner_fixture["simulated_response_count"],
            "route_complete": runner_fixture["route_complete"],
            "move_count": runner_fixture["move_count"],
            "distance_m": runner_fixture["distance_m"],
            "travel_time_sec": runner_fixture["travel_time_sec"],
            "energy_kwh": runner_fixture["energy_kwh"],
            "segment_count": runner_fixture["segment_count"],
            "first_rendered_prompt_bytes": runner_fixture["attempts"][0].get("rendered_cli_prompt_bytes"),
            "second_rendered_prompt_bytes": (
                runner_fixture["attempts"][1].get("rendered_cli_prompt_bytes")
                if len(runner_fixture["attempts"]) > 1 else None
            ),
            "usage_is_model_usage": False,
        },
        "map_representation": {
            "encoding": "lossless_directional_cost_codes_by_y",
            "directed_edge_count": len(package.edges),
            "equivalence_passed": bool(map_audit.get("passed")),
            "map_sent_once_per_persistent_session": session_mode == "persistent_per_task",
            "note": "m.g and m.c are already a compact lossless representation; reuse the persistent session instead of retransmitting them on every request.",
        },
        "evaluator_truth_in_prompt": False,
        "formal_result": False,
        "limitations": [
            "Offline witness validates the validator/protocol path only; it is not a model proposal, quality result, token estimate, or cost result.",
            f"The K={MAX_SEGMENT_ACTIONS} segment protocol changes the single-direction request method and is a separately versioned R4 candidate.",
            "Full segments require exactly K moves while the shortest-path lower bound exceeds K; the final segment requires exactly the remaining shortest-path lower bound and still undergoes local legality validation.",
            "This preflight does not authorize or make any model request.",
            "The full-runner fixture feeds a local witness to a fake client; its route metrics are not model performance.",
        ],
    }
    _write_json(output_dir / "preflight.json", record)
    _write_json(output_dir / "route_messages.json", {"messages": messages})
    _write_json(output_dir / "route_schema.json", schema.schema)
    _write_json(output_dir / "offline_witness_segment_audit.json", witness_audit)
    _write_json(output_dir / "offline_full_runner_fixture.json", runner_fixture)
    _write_json(output_dir / "offline_witness_source_row.json", {
        key: witness.get(key)
        for key in ("seed", "task_id", "start_node_id", "pickup_node_id", "dropoff_node_id", "witness_status", "witness_edge_count", "witness_scope")
    })
    replay_audit = {
        "task_id": task.task_id,
        "status": "offline_witness_fixture_completed_not_model_result",
        "route_complete": True,
        "api_request_count": runner_fixture["simulated_response_count"],
        "rejected_segment_count": 0,
        "attempts": runner_fixture["attempts"],
        "route": runner_fixture["route"],
        "usage": runner_fixture["usage"],
        "energy": {
            "scope": "offline_witness_fixture_only",
            "energy_kwh": runner_fixture["energy_kwh"],
            "distance_m": runner_fixture["distance_m"],
            "travel_time_sec": runner_fixture["travel_time_sec"],
            "note": "离线见证 + 假客户端的运行器测试指标，不是模型路线或实车能耗。",
        },
    }
    render_replay(
        item["map_data"],
        {
            "task_id": task.task_id,
            "start_node_id": task.start_node_id,
            "pickup_node_id": task.pickup_node_id,
            "dropoff_node_id": task.dropoff_node_id,
        },
        replay_audit,
        output_dir / "offline_witness_replay.html",
    )
    _render_index(output_dir, record)
    return record


def _render_index(output_dir: Path, record: Dict[str, Any]) -> None:
    source_map_link = "../traditional_experiment_v1/seeds/seed_0/map_preview.html"
    match = re.search(r"_v(\d+)$", str(record.get("experiment_id", "")))
    version = int(match.group(1)) if match else 4
    details = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))}</td></tr>"
        for key, value in (
            ("状态", record["status"]),
            ("任务", record["task_id"]),
            ("端点", f'{record["start_node_id"]} → {record["pickup_node_id"]} → {record["dropoff_node_id"]}'),
            ("输入地图道路特征", "已包含冻结的 RoadFeatureInputProtocol 上下文"),
            ("初始下界 / 进度上限", f'{record["initial_move_lower_bound"]} / {record["progress_limit"]} 步'),
            ("每段上限 / 请求上限", f'{record["max_segment_actions"]} / {record["maximum_model_requests"]} 次'),
            ("会话模式", record["session_mode"]),
            ("离线见证分段校验", record["offline_witness_gate"]),
            ("完整运行器假响应测试", record["offline_full_runner_fixture"]),
            ("模型请求", record["model_requests"]),
            ("正式实验结果", record["formal_result"]),
        )
    )
    session_explanation = (
        "每次分段请求单独启动，不继承前序会话；每段重新发送同一冻结地图和当前路线状态。"
        if record["session_mode"] == "ephemeral_per_request"
        else "每个 task 使用一条持久会话：首轮输入包含压缩有向图和道路特征，后续输入携带最新状态与校验反馈。"
    )
    segment_explanation = (
        f"当最短剩余步数大于{record['max_segment_actions']}时，每段必须恰好提出{record['max_segment_actions']}步；剩余最短步数不超过该值时，最后一段必须恰好走完这段最短路径。"
        if record.get("require_full_segments")
        else f"模型每段最多提出{record['max_segment_actions']}步。"
    )
    page = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>R4 分段方案零调用预检</title>
<style>body{font:15px/1.6 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f3f6fc;color:#172033;margin:24px}main{max-width:1100px;margin:auto}header,section{background:white;padding:20px;margin:14px 0;border:1px solid #dce3ee;border-radius:12px}header{background:#172554;color:white}table{border-collapse:collapse;width:100%}th,td{padding:9px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:top}th{width:240px;background:#f7f9fc}a{color:#2563eb}.notice{color:#9a3412;background:#fff7ed;padding:12px;border-radius:8px}</style><main><header><h1>R4 segment-v__VERSION__ 零调用预检</h1><p>状态：offline_segment_preflight_passed_no_model_requests</p><p>此页面是协议与校验器的离线准入记录，不是模型实验结果。</p></header><section><h2>冻结任务与门禁</h2><table>__DETAILS__</table></section><section><h2>如何解读</h2><p class=notice>离线见证和假客户端只用于验证运行器、逐边校验、原子提交和进度限制；没有调用模型，也不是 R4 路线结果。</p><p>__SESSION_EXPLANATION__ __SEGMENT_EXPLANATION__程序逐边复核，整段通过才提交。模型自检与程序事实不一致只记录差异，路线合法性由程序判断。实际 token 用量以 CLI 回执为准。超过进度上限的整段拒绝，且没有传统算法回退。</p><p><a href='__MAP_LINK__'>打开 seed 0 原地图</a> · <a href='route_messages.json'>查看冻结模型输入</a> · <a href='route_schema.json'>查看输出约束</a> · <a href='offline_witness_segment_audit.json'>查看分段离线门禁</a> · <a href='offline_full_runner_fixture.json'>查看假客户端完整运行</a> · <a href='offline_witness_replay.html'>查看离线路线回放</a> · <a href='offline_verification_report.json'>查看离线代码与回放核验</a></p></section></main></html>"""
    page = (
        page.replace("__DETAILS__", details)
        .replace("__MAP_LINK__", source_map_link)
        .replace("__VERSION__", str(version))
        .replace("__SESSION_EXPLANATION__", session_explanation)
        .replace("__SEGMENT_EXPLANATION__", segment_explanation)
    )
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--session-mode", choices=("persistent_per_task", "ephemeral_per_request"), default="persistent_per_task")
    parser.add_argument("--max-feedback-requests", type=int, default=MAX_FEEDBACK_REQUESTS)
    parser.add_argument("--require-full-segments", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build_preflight(
        args.output,
        session_mode=args.session_mode,
        max_feedback_requests=args.max_feedback_requests,
        require_full_segments=args.require_full_segments,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
