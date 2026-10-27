#!/usr/bin/env python3
"""Run one separately authorized R4 segment-v2 task9 pilot.

Without --execute this script only verifies the frozen local inputs. A model
request requires the explicit --execute switch and a fresh output directory.
The runner never resumes or retries a request-in-progress invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import EvaluationConfig, RouteCandidate
from route_experiment.r4_segment import (
    _allowed_directions_at_node,
    _progress_safe_directions_at_node,
    _rejection_prefix_constraint,
    run_r4_segment_route_search,
)
from run_r4_codex_formal_checkpoint import INTENT_RESULT, prepare
from preflight_r4_codex_segment_task9 import MAX_FEEDBACK_REQUESTS, MAX_SEGMENT_ACTIONS, MAX_TOTAL_ACTIONS, PROGRESS_MULTIPLIER
from render_r4_segment_replay import render_replay


PREFLIGHT_DIR = ROOT / "outputs/route_layer/r4_codex_segment_task9_preflight_v6"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_codex_segment_task9_run_v2"
MODEL = "gpt-5.6-luna"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json_durable(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def _source_feature_context() -> Dict[str, Any]:
    source_path = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    payload = json.loads(source["route_search"]["messages"][1]["content"])
    context = payload.get("i")
    if not isinstance(context, dict):
        raise RuntimeError("frozen_agent_readable_feature_context_missing")
    return context


def _load_gate(preflight_dir: Path | None = None) -> Dict[str, Any]:
    preflight_path = Path(preflight_dir or PREFLIGHT_DIR) / "preflight.json"
    if not preflight_path.is_file():
        raise RuntimeError("r4_segment_zero_call_preflight_missing")
    gate = json.loads(preflight_path.read_text(encoding="utf-8"))
    if gate.get("status") != "offline_segment_preflight_passed_no_model_requests":
        raise RuntimeError("r4_segment_zero_call_preflight_not_passed")
    if gate.get("formal_result") is not False or gate.get("model_requests") != 0:
        raise RuntimeError("r4_segment_preflight_scope_invalid")
    if gate.get("model") != MODEL or gate.get("network_retries") != 0:
        raise RuntimeError("r4_segment_model_or_retry_policy_changed")
    if gate.get("segment_protocol") not in {None, "r4-segment-v1", "r4-segment-v2", "r4-segment-v3", "r4-segment-v4", "r4-segment-v5"}:
        raise RuntimeError("r4_segment_protocol_version_invalid")
    if gate.get("session_mode") not in {"persistent_per_task", "ephemeral_per_request"}:
        raise RuntimeError("r4_segment_session_mode_mismatch")
    if not isinstance(gate.get("require_full_segments", False), bool):
        raise RuntimeError("r4_segment_full_segment_policy_invalid")
    max_feedback_requests = gate.get("max_feedback_requests", MAX_FEEDBACK_REQUESTS)
    if isinstance(max_feedback_requests, bool) or not isinstance(max_feedback_requests, int) or not 0 <= max_feedback_requests <= int(gate.get("max_total_actions", MAX_TOTAL_ACTIONS)):
        raise RuntimeError("r4_segment_feedback_policy_invalid")
    item = prepare()
    observed = {
        "map_sha256": _sha256(item["map_path"].read_bytes()),
        "orders_sha256": _sha256(item["task_path"].read_bytes()),
        "intent_result_sha256": _sha256(INTENT_RESULT.read_bytes()),
    }
    for key, value in observed.items():
        if gate.get(key) != value:
            raise RuntimeError("frozen_input_hash_changed:" + key)
    task = item["task"]
    frozen_task = (gate.get("start_node_id"), gate.get("pickup_node_id"), gate.get("dropoff_node_id"))
    if frozen_task != (task.start_node_id, task.pickup_node_id, task.dropoff_node_id):
        raise RuntimeError("frozen_route_endpoints_changed")
    if gate.get("route_input_directed_edges") != len(item["package"].edges):
        raise RuntimeError("frozen_route_package_edge_count_changed")
    return {"gate": gate, "item": item, "feature_context": _source_feature_context(), "observed_hashes": observed}


def _json_safe_provider(record: Any) -> Dict[str, Any]:
    return record if isinstance(record, dict) else {}


def _attempted_prefix_energy(item: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
    audit = attempt.get("audit") or {}
    edge_ids = list(audit.get("attempted_edge_sequence") or [])
    node_ids = list(audit.get("attempted_node_sequence") or [])
    current = attempt.get("current_node_id")
    base = {
        "request_number": attempt.get("request_number"),
        "first_violation_step": audit.get("first_violation_step"),
        "violations": audit.get("violations") or [],
        "legal_prefix_edge_count": len(edge_ids),
        "scope": "attempted_legal_prefix_before_first_violation",
        "simulation_estimate": True,
        "illegal_action_energy_estimable": False,
        "note": "估算仅覆盖违规前实际存在的合法有向边；非法边没有可定义的道路能耗，且这只是模型提案，不是车辆已行驶能耗。",
    }
    if not edge_ids:
        return {
            **base,
            "energy_kwh": None,
            "distance_m": None,
            "travel_time_sec": None,
            "note": "违规前没有合法边；非法动作本身没有可定义的道路能耗。",
        }
    if not isinstance(current, str) or len(node_ids) != len(edge_ids):
        return {
            **base,
            "status": "attempted_prefix_trace_inconsistent",
            "energy_kwh": None,
            "distance_m": None,
            "travel_time_sec": None,
        }
    candidate = RouteCandidate(
        route_id="r4-attempted-prefix-" + str(attempt.get("request_number")),
        node_sequence=[current] + node_ids,
        edge_sequence=edge_ids,
        route_source="r4_codex_segment_attempted_legal_prefix",
        route_reachability=True,
        validation_status="valid_prefix",
    )
    evaluated = evaluate_route_candidate(item["road_map"], candidate, EvaluationConfig())
    return {
        **base,
        "status": "estimated",
        "energy_kwh": evaluated.energy_total_kwh,
        "distance_m": evaluated.route_distance_m,
        "travel_time_sec": evaluated.total_travel_time_sec,
        "energy_model_version": evaluated.energy_model_version,
        "trace_fidelity": evaluated.energy_trace_fidelity,
    }


def _provider_audit_from_trace(run_dir: Path, attempt: Dict[str, Any]) -> Dict[str, Any]:
    trace_ref = attempt.get("request_id")
    if not isinstance(trace_ref, str) or not trace_ref:
        raise RuntimeError("prior_recovery_trace_reference_missing")
    trace_path = Path(trace_ref).resolve()
    resolved_run_dir = Path(run_dir).resolve()
    if not trace_path.is_relative_to(resolved_run_dir) or not trace_path.is_file():
        raise RuntimeError("prior_recovery_trace_path_invalid")
    events = []
    parse_errors = []
    for line_number, line in enumerate(trace_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            parse_errors.append(line_number)
    event_audit = CodexCliRouteStepClient._audit_events(events)
    completed_turns = sum(event.get("type") == "turn.completed" for event in events)
    usage = attempt.get("usage") or {}
    if (
        parse_errors
        or event_audit.get("turn_started_count") != 1
        or event_audit.get("usage_event_count") != 1
        or completed_turns != 1
        or event_audit.get("tool_usage_detected") is not False
        or event_audit.get("input_tokens") != usage.get("input_tokens")
        or event_audit.get("output_tokens") != usage.get("output_tokens")
    ):
        raise RuntimeError("prior_recovery_trace_audit_failed")
    messages = [
        event.get("item", {}).get("text")
        for event in events
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "agent_message"
    ]
    if len(messages) != 1:
        raise RuntimeError("prior_recovery_trace_agent_message_count_invalid")
    try:
        trace_payload = json.loads(messages[0])
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("prior_recovery_trace_agent_message_invalid") from error
    if trace_payload != attempt.get("proposal"):
        raise RuntimeError("prior_recovery_attempt_response_trace_mismatch")
    return {
        "backend": "codex_cli_trace_reconstructed",
        "trace_path": str(trace_path),
        "trace_sha256": _sha256(trace_path.read_bytes()),
        "event_audit": event_audit,
        "parse_errors": parse_errors,
    }


def _route_state_from_prior_audit(prior_audit: Dict[str, Any], loaded: Dict[str, Any]) -> Dict[str, Any]:
    item = loaded["item"]
    task = item["task"]
    route = prior_audit.get("route") or {}
    node_ids = list(route.get("node_sequence") or [])
    edge_ids = list(route.get("edge_sequence") or [])
    directions = list(route.get("direction_sequence") or [])
    package_edges = {edge.edge_id: edge for edge in item["package"].edges}
    package_nodes = {node.node_id: node for node in item["package"].nodes}
    blocked = set(item["package"].blocked_edge_ids)
    direction_by_delta = {(1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S"}
    if (
        prior_audit.get("route_complete")
        or route.get("start_node_id") != task.start_node_id
        or route.get("pickup_node_id") != task.pickup_node_id
        or route.get("dropoff_node_id") != task.dropoff_node_id
        or not node_ids
        or node_ids[0] != task.start_node_id
        or len(node_ids) != len(edge_ids) + 1
        or len(directions) != len(edge_ids)
        or route.get("move_count") != len(edge_ids)
        or len(node_ids) != len(set(node_ids))
        or any(node_id not in package_nodes for node_id in node_ids)
        or (task.dropoff_node_id in node_ids and node_ids[-1] != task.dropoff_node_id)
    ):
        raise RuntimeError("prior_recovery_route_state_shape_invalid")
    for index, edge_id in enumerate(edge_ids):
        edge = package_edges.get(edge_id)
        if (
            edge is None
            or edge_id in blocked
            or edge.from_node != node_ids[index]
            or edge.to_node != node_ids[index + 1]
        ):
            raise RuntimeError("prior_recovery_route_contains_illegal_edge")
        source = package_nodes[edge.from_node]
        target = package_nodes[edge.to_node]
        if direction_by_delta.get((target.x - source.x, target.y - source.y)) != directions[index]:
            raise RuntimeError("prior_recovery_route_direction_mismatch")
    if task.pickup_node_id in node_ids and task.dropoff_node_id in node_ids:
        if node_ids.index(task.pickup_node_id) > node_ids.index(task.dropoff_node_id):
            raise RuntimeError("prior_recovery_route_pickup_order_invalid")
    return {
        "current_node_id": node_ids[-1],
        "visited_node_ids": node_ids,
        "pickup_seen": task.pickup_node_id in node_ids,
        "direction_sequence": directions,
        "edge_sequence": edge_ids,
    }


def _feedback_from_rejected_attempt(attempt: Dict[str, Any], loaded: Dict[str, Any]) -> Dict[str, Any] | None:
    failure_audit = attempt.get("audit") or {}
    violations = list(failure_audit.get("violations") or [])
    if not violations:
        return None
    item = loaded["item"]
    violation = violations[0]
    prefix_nodes = list(attempt.get("visited_node_ids") or []) + list(
        failure_audit.get("attempted_node_sequence") or []
    )
    prefix_directions = list(failure_audit.get("attempted_direction_sequence") or [])
    failure_node = violation.get("from_node_id")
    if not isinstance(failure_node, str) or failure_node not in prefix_nodes:
        raise RuntimeError("prior_recovery_violation_prefix_inconsistent")
    pickup_seen = item["task"].pickup_node_id in prefix_nodes
    allowed = _allowed_directions_at_node(
        item["road_map"],
        failure_node,
        prefix_nodes,
        item["task"].pickup_node_id,
        item["task"].dropoff_node_id,
        pickup_seen,
        item["package"].blocked_edge_ids,
    )
    if not allowed:
        raise RuntimeError("prior_recovery_failure_state_has_no_legal_next_direction")
    progress_safe = _progress_safe_directions_at_node(
        item["road_map"],
        current_node_id=failure_node,
        visited_node_ids=prefix_nodes,
        pickup_node_id=item["task"].pickup_node_id,
        dropoff_node_id=item["task"].dropoff_node_id,
        pickup_seen=pickup_seen,
        blocked_edge_ids=item["package"].blocked_edge_ids,
        accepted_action_count=int(attempt.get("accepted_action_count", 0)) + len(prefix_directions),
        progress_limit=int(attempt.get("progress_limit", loaded["gate"]["progress_limit"])),
    )
    return {
        "code": str(violation.get("kind", "segment_rejected")),
        "step": violation.get("step"),
        "direction": violation.get("direction"),
        "from_node_id": failure_node,
        "to_node_id": violation.get("to_node_id"),
        "previous_valid_prefix_directions": prefix_directions,
        "allowed_directions_at_failure_state": allowed,
        "progress_safe_directions_at_failure_state": progress_safe,
        "failure_state_route_progress_moves": int(attempt.get("accepted_action_count", 0)) + len(prefix_directions),
        "progress_limit": int(attempt.get("progress_limit", loaded["gate"]["progress_limit"])),
        "recovery_scope": "regenerate_the_entire_segment_from_the_current_accepted_node",
        "rejected_segment_is_atomic": True,
    }


def _latest_feedback_attempt(attempts: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """Only reuse feedback when the immediately preceding proposal was rejected.

    Earlier rejected segments are already reflected in the accepted route state;
    replaying their location-specific advice after later accepted moves is stale.
    """
    if not attempts:
        return None
    latest = attempts[-1]
    return latest if (latest.get("audit") or {}).get("violations") else None


def _same_state_rejected_attempts(attempts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return consecutive rejected attempts from the latest unchanged route state."""
    latest = _latest_feedback_attempt(attempts)
    if latest is None:
        return []
    latest_signature = (
        latest.get("current_node_id"),
        latest.get("accepted_action_count"),
        tuple(latest.get("visited_node_ids") or []),
    )
    selected = []
    for attempt in reversed(attempts):
        audit = attempt.get("audit") or {}
        if audit.get("accepted") or not audit.get("violations"):
            break
        signature = (
            attempt.get("current_node_id"),
            attempt.get("accepted_action_count"),
            tuple(attempt.get("visited_node_ids") or []),
        )
        if signature != latest_signature:
            break
        selected.append(attempt)
    return list(reversed(selected))


def _build_recovery_context(prior_failure_dir: Path, loaded: Dict[str, Any]) -> Dict[str, Any]:
    prior_dir = Path(prior_failure_dir).resolve()
    manifest_path = prior_dir / "manifest.json"
    audit_path = prior_dir / "final_audit.json"
    search_path = prior_dir / "search_result.json"
    if not manifest_path.is_file() or not audit_path.is_file() or not search_path.is_file():
        raise RuntimeError("prior_recovery_artifacts_missing")
    prior_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prior_audit = json.loads(audit_path.read_text(encoding="utf-8"))
    prior_search = json.loads(search_path.read_text(encoding="utf-8"))
    item = loaded["item"]
    if prior_audit.get("route_complete") or prior_audit.get("status") == "completed_route":
        raise RuntimeError("prior_run_already_completed")
    if prior_audit.get("task_id") != item["task"].task_id or prior_audit.get("map_id") != item["road_map"].map_id:
        raise RuntimeError("prior_recovery_task_or_map_mismatch")
    for key, expected in loaded["observed_hashes"].items():
        if prior_audit.get(key) != expected or prior_manifest.get(key) != expected:
            raise RuntimeError("prior_recovery_frozen_input_hash_mismatch:" + key)
    if (
        prior_manifest.get("max_segment_actions") != int(loaded["gate"].get("max_segment_actions", MAX_SEGMENT_ACTIONS))
        or prior_manifest.get("progress_limit") != loaded["gate"].get("progress_limit")
        or prior_audit.get("unresolved_cli_invocation") is not None
    ):
        raise RuntimeError("prior_recovery_protocol_or_invocation_state_mismatch")
    prior_protocol = prior_search.get("method_version") or prior_manifest.get("segment_protocol")
    if prior_protocol not in {"r4-segment-v2", "r4-segment-v3", "r4-segment-v4", "r4-segment-v5"}:
        raise RuntimeError("prior_recovery_method_version_invalid")
    attempts = list(prior_audit.get("attempts") or [])
    request_count = prior_audit.get("api_request_count")
    prior_usage = prior_audit.get("cumulative_usage") or prior_audit.get("usage") or {}
    if (
        isinstance(request_count, bool)
        or not isinstance(request_count, int)
        or request_count != len(attempts)
        or not attempts
        or not prior_usage.get("usage_complete")
        or prior_audit.get("status") not in {
            "segment_rejection_limit_exhausted",
            "segment_request_budget_exhausted",
            "route_progress_budget_exhausted",
            "process_interrupted_reconciled_checkpoint",
        }
    ):
        raise RuntimeError("prior_recovery_run_not_closed_and_usage_complete")
    enriched_attempts = []
    for attempt in attempts:
        if attempt.get("status") != "response_received" or not isinstance(attempt.get("audit"), dict):
            raise RuntimeError("prior_recovery_attempt_not_auditable_model_response")
        source_dir = Path(attempt.get("source_run_dir") or prior_dir).resolve()
        provider = _provider_audit_from_trace(source_dir, attempt)
        enriched = dict(attempt)
        enriched["provider_audit"] = provider
        enriched["source_run_dir"] = str(source_dir)
        enriched_attempts.append(enriched)
    if prior_audit.get("usage", {}).get("usage_complete") is False:
        raise RuntimeError("prior_recovery_current_run_usage_incomplete")
    route_state = _route_state_from_prior_audit(prior_audit, loaded)
    rejected_count = prior_audit.get("rejected_segment_count")
    feedback_attempt = _latest_feedback_attempt(enriched_attempts)
    feedback = _feedback_from_rejected_attempt(feedback_attempt, loaded) if feedback_attempt else None
    same_state_attempts = _same_state_rejected_attempts(enriched_attempts)
    prefix_constraints = []
    for rejected_attempt in same_state_attempts:
        attempt_feedback = _feedback_from_rejected_attempt(rejected_attempt, loaded)
        if attempt_feedback is None:
            continue
        compact_constraint = _rejection_prefix_constraint(attempt_feedback)
        if compact_constraint not in prefix_constraints:
            prefix_constraints.append(compact_constraint)
    if feedback is not None:
        feedback["recent_rejected_prefix_constraints"] = prefix_constraints
    if (
        isinstance(rejected_count, bool)
        or not isinstance(rejected_count, int)
        or rejected_count < 0
        or rejected_count > int(loaded["gate"].get("max_feedback_requests", MAX_FEEDBACK_REQUESTS))
    ):
        raise RuntimeError("prior_recovery_rejection_count_exceeds_new_feedback_policy")
    return {
        "prior_run_dir": str(prior_dir),
        "prior_final_audit_sha256": _sha256(audit_path.read_bytes()),
        "prior_manifest_sha256": _sha256(manifest_path.read_bytes()),
        "prior_segment_protocol": prior_protocol,
        "prior_model_turn_count": request_count,
        "prior_request_count": request_count,
        "prior_usage": prior_usage,
        "initial_rejected_segments": rejected_count,
        "initial_feedback": feedback,
        "same_state_rejected_prefix_count": len(prefix_constraints),
        "initial_route_state": route_state,
        "progress_limit": int(prior_manifest["progress_limit"]),
        "prior_attempts": enriched_attempts,
        "prior_attempted_prefix_energy": [
            _attempted_prefix_energy(item, attempt)
            for attempt in enriched_attempts
            if (attempt.get("audit") or {}).get("violations")
        ],
        "prior_provider_audit": feedback_attempt.get("provider_audit") if feedback_attempt else None,
        "latest_feedback_request_number": feedback_attempt.get("request_number") if feedback_attempt else None,
    }


def run(
    output_dir: Path,
    request_limit: int,
    authorization_note: str,
    preflight_dir: Path | None = None,
    prior_failure_dir: Path | None = None,
) -> Dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError("output_directory_exists_no_replay_or_retry")
    selected_preflight_dir = Path(preflight_dir or PREFLIGHT_DIR)
    loaded = _load_gate(selected_preflight_dir)
    gate, item = loaded["gate"], loaded["item"]
    maximum = int(gate["maximum_model_requests"])
    recovery = _build_recovery_context(prior_failure_dir, loaded) if prior_failure_dir else None
    initial_rejected_segments = int(recovery["initial_rejected_segments"]) if recovery else 0
    initial_move_count = (
        len(recovery["initial_route_state"]["direction_sequence"]) if recovery else 0
    )
    remaining_request_bound = (
        int(math.ceil((int(gate["progress_limit"]) - initial_move_count) / float(gate.get("max_segment_actions", MAX_SEGMENT_ACTIONS))))
        + int(gate.get("max_feedback_requests", MAX_FEEDBACK_REQUESTS))
        - initial_rejected_segments
    )
    if isinstance(request_limit, bool) or not 1 <= int(request_limit) <= maximum:
        raise ValueError("request_limit_must_be_within_frozen_task_upper_bound")
    if int(request_limit) > remaining_request_bound:
        raise ValueError("request_limit_exceeds_remaining_progress_and_feedback_bound")
    if not authorization_note.strip():
        raise ValueError("authorization_note_required_for_execute_mode")

    client = CodexCliRouteStepClient(
        model=MODEL,
        trace_dir=output_dir / "traces",
        reasoning_effort="low",
        timeout_sec=300,
        persist_session=gate["session_mode"] == "persistent_per_task",
        session_id=None,
        working_directory=(
            output_dir / "codex_empty_workspace"
            if gate["session_mode"] == "persistent_per_task"
            else None
        ),
        trace_stem="r4_segment",
    )
    if client.retries != 0:
        raise RuntimeError("r4_segment_network_retries_must_be_zero")
    output_dir.mkdir(parents=True)
    task = item["task"]
    manifest = {
        "schema_version": "r4_codex_segment_task9_run_manifest_v3" if gate.get("require_full_segments") else "r4_codex_segment_task9_run_manifest_v2",
        "experiment_id": gate.get("experiment_id", "r4_codex_segment_task9_run_v2"),
        "status": "ready_for_explicit_execution",
        "authorization_note": authorization_note.strip(),
        "map_id": item["road_map"].map_id,
        "task_id": task.task_id,
        "start_node_id": task.start_node_id,
        "pickup_node_id": task.pickup_node_id,
        "dropoff_node_id": task.dropoff_node_id,
        **loaded["observed_hashes"],
        "preflight_sha256": _sha256((selected_preflight_dir / "preflight.json").read_bytes()),
        "model": MODEL,
        "reasoning_effort": "low",
        "session_mode": gate["session_mode"],
        "codex_cli_session_mode": gate["session_mode"],
        "maximum_model_requests": maximum,
        "effective_request_limit": int(request_limit),
        "max_segment_actions": int(gate.get("max_segment_actions", MAX_SEGMENT_ACTIONS)),
        "max_feedback_requests": int(gate.get("max_feedback_requests", MAX_FEEDBACK_REQUESTS)),
        "segment_protocol": gate.get("segment_protocol") or "r4-segment-v1",
        "continued_from_segment_protocol": recovery["prior_segment_protocol"] if recovery else None,
        "require_full_segments": bool(gate.get("require_full_segments", False)),
        "initial_rejected_segments": initial_rejected_segments,
        "initial_route_move_count": initial_move_count,
        "prior_failure_dir": recovery["prior_run_dir"] if recovery else None,
        "prior_final_audit_sha256": recovery["prior_final_audit_sha256"] if recovery else None,
        "prior_manifest_sha256": recovery["prior_manifest_sha256"] if recovery else None,
        "prior_model_turn_count": int(recovery["prior_model_turn_count"]) if recovery else 0,
        "initial_feedback_sha256": (
            _sha256(json.dumps(recovery["initial_feedback"], ensure_ascii=False, sort_keys=True).encode("utf-8"))
            if recovery else None
        ),
        "progress_limit": gate["progress_limit"],
        "network_retries": 0,
        "tool_events_allowed": False,
        "output_token_hard_limit": None,
        "codex_cli_version": client.cli_version,
        "account_billing": "not_reported_by_codex_cli",
        "formal_batch_started": False,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_json_durable(output_dir / "manifest.json", manifest)
    state: Dict[str, Any] = {
        "experiment_id": manifest["experiment_id"],
        "status": "ready",
        "model_requests_started": int(recovery["prior_request_count"]) if recovery else 0,
        "model_requests_completed": int(recovery["prior_request_count"]) if recovery else 0,
        "initial_rejected_segments": initial_rejected_segments,
        "current_node_id": recovery["initial_route_state"]["current_node_id"] if recovery else task.start_node_id,
        "visited_node_ids": recovery["initial_route_state"]["visited_node_ids"] if recovery else [task.start_node_id],
        "pickup_seen": recovery["initial_route_state"]["pickup_seen"] if recovery else task.start_node_id == task.pickup_node_id,
        "accepted_action_count": initial_move_count,
        "effective_request_limit": int(request_limit),
        "network_retries": 0,
        "unresolved_cli_invocation": None,
    }
    state_path = output_dir / "state.json"
    _write_json_durable(state_path, state)
    if recovery:
        _write_json_durable(output_dir / "recovery_context.json", recovery)
    started_at = datetime.now(timezone.utc).isoformat()

    def on_request_start(request_start: Dict[str, Any]) -> None:
        number = int(request_start["request_number"])
        in_progress = {
            **request_start,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "request_in_progress",
            "delivery_status": "unknown_until_receipt",
        }
        _write_json_durable(output_dir / "attempts" / f"attempt_{number:03d}.json", in_progress)
        state.update({
            "status": "request_in_progress",
            "model_requests_started": number,
            "request_in_progress": in_progress,
            "unresolved_cli_invocation": {
                "request_number": number,
                "status": "delivery_and_usage_unknown_until_receipt",
                "messages_sha256": request_start["messages_sha256"],
            },
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        _write_json_durable(state_path, state)

    def on_attempt(attempt: Dict[str, Any]) -> None:
        number = int(attempt["request_number"])
        attempt_path = output_dir / "attempts" / f"attempt_{number:03d}.json"
        previous = json.loads(attempt_path.read_text(encoding="utf-8"))
        finalized = {
            **previous,
            **attempt,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "delivery_status": "response_received" if attempt.get("status") == "response_received" else "unknown_or_error",
        }
        _write_json_durable(attempt_path, finalized)
        audit = attempt.get("audit") or {}
        if audit.get("accepted"):
            state["accepted_action_count"] = int(state.get("accepted_action_count", 0)) + len(audit.get("committed_direction_sequence", []))
            state["current_node_id"] = audit.get("current_node_after_segment", state["current_node_id"])
            state["pickup_seen"] = audit.get("pickup_seen_after_segment", state["pickup_seen"])
            state["visited_node_ids"] = list(state.get("visited_node_ids") or []) + list(
                audit.get("committed_node_sequence", [])
            )
        state.update({
            "status": "response_recorded" if attempt.get("status") == "response_received" else "request_error_no_retry",
            "model_requests_completed": number,
            "last_request_status": attempt.get("status"),
            "last_request_usage": attempt.get("usage"),
            "unresolved_cli_invocation": None if attempt.get("status") == "response_received" else {
                "request_number": number,
                "status": "delivery_and_usage_unknown",
                "provider_audit": _json_safe_provider(attempt.get("provider_audit")),
            },
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        _write_json_durable(state_path, state)

    search = run_r4_segment_route_search(
        client,
        item["package"],
        item["road_map"],
        task,
        task.pickup_node_id,
        task.dropoff_node_id,
        max_api_calls=int(request_limit),
        max_segment_actions=int(gate.get("max_segment_actions", MAX_SEGMENT_ACTIONS)),
        max_total_actions=int(gate.get("max_total_actions", MAX_TOTAL_ACTIONS)),
        max_progress_multiplier=int(gate.get("progress_multiplier", PROGRESS_MULTIPLIER)),
        max_feedback_requests=int(gate.get("max_feedback_requests", MAX_FEEDBACK_REQUESTS)),
        require_full_segments=bool(gate.get("require_full_segments", False)),
        initial_rejected_segments=initial_rejected_segments,
        initial_feedback=recovery["initial_feedback"] if recovery else None,
        request_number_offset=int(recovery["prior_model_turn_count"]) if recovery else 0,
        initial_route_state=recovery["initial_route_state"] if recovery else None,
        progress_limit_override=int(recovery["progress_limit"]) if recovery else int(gate["progress_limit"]),
        route_feature_context=loaded["feature_context"],
        on_request_start=on_request_start,
        on_attempt=on_attempt,
    )
    route = search["route"]
    energy = None
    if route["edge_sequence"]:
        candidate = RouteCandidate(
            route_id="r4-codex-segment-" + task.task_id,
            node_sequence=route["node_sequence"],
            edge_sequence=route["edge_sequence"],
            route_source="r4_codex_segment_v2",
            route_reachability=True,
            validation_status="valid_prefix",
        )
        evaluated = evaluate_route_candidate(item["road_map"], candidate, EvaluationConfig())
        energy = {
            "scope": "complete_model_route" if search["route_complete"] else "accepted_legal_prefix_only",
            "energy_kwh": evaluated.energy_total_kwh,
            "distance_m": evaluated.route_distance_m,
            "travel_time_sec": evaluated.total_travel_time_sec,
            "energy_model_version": evaluated.energy_model_version,
            "trace_fidelity": evaluated.energy_trace_fidelity,
            "simulation_estimate": True,
            "note": "共享确定性评估器对已接纳合法边的估算；不是实车测量，也不包括被拒绝路线段。",
        }
    else:
        energy = {
            "scope": "not_estimable_no_accepted_edges",
            "energy_kwh": None,
            "distance_m": None,
            "travel_time_sec": None,
            "simulation_estimate": True,
            "note": "没有已接纳路线边，能耗不可估；不填零。",
        }
    complete_usage = search["usage"]["usage_complete"]
    persisted_attempts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((output_dir / "attempts").glob("attempt_*.json"))
    ]
    for attempt in persisted_attempts:
        attempt["attempted_prefix_energy"] = _attempted_prefix_energy(item, attempt)
        attempt["source_run_dir"] = str(output_dir.resolve())
    prior_audit = None
    prior_attempts = []
    prior_segment_audits = []
    prior_request_count = 0
    prior_usage = None
    if recovery:
        prior_dir = Path(recovery["prior_run_dir"])
        prior_audit = json.loads((prior_dir / "final_audit.json").read_text(encoding="utf-8"))
        prior_attempts = [dict(attempt) for attempt in recovery["prior_attempts"]]
        for attempt in prior_attempts:
            attempt["attempted_prefix_energy"] = _attempted_prefix_energy(item, attempt)
            # Keep each trace's original source directory from the recovery audit.
        prior_segment_audits = list(prior_audit.get("segment_audits") or [])
        prior_request_count = int(recovery["prior_request_count"])
        prior_usage = recovery["prior_usage"]
    all_attempts = prior_attempts + persisted_attempts
    cumulative_usage_complete = bool(
        (not prior_attempts or prior_usage and prior_usage.get("usage_complete"))
        and search["usage"].get("usage_complete")
    )
    cumulative_usage = {
        "usage_complete": cumulative_usage_complete,
        "input_tokens": (
            int((prior_usage or {}).get("input_tokens") or 0) + int(search["usage"].get("input_tokens") or 0)
            if cumulative_usage_complete else None
        ),
        "output_tokens": (
            int((prior_usage or {}).get("output_tokens") or 0) + int(search["usage"].get("output_tokens") or 0)
            if cumulative_usage_complete else None
        ),
        "model_turn_count": prior_request_count + int(search["api_request_count"]),
        "account_billing": "not_reported_by_codex_cli",
        "scope": "prior rejected proposal plus continuation requests; Codex CLI token usage only",
    }
    final_audit = {
        "schema_version": "r4_codex_segment_task9_final_audit_v3",
        "experiment_id": manifest["experiment_id"],
        "segment_protocol": manifest["segment_protocol"],
        "continued_from_segment_protocol": manifest["continued_from_segment_protocol"],
        "created_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": search["status"],
        "model": MODEL,
        "session_mode": manifest["session_mode"],
        "task_id": task.task_id,
        "map_id": item["road_map"].map_id,
        "start_node_id": task.start_node_id,
        "pickup_node_id": task.pickup_node_id,
        "dropoff_node_id": task.dropoff_node_id,
        **loaded["observed_hashes"],
        "route_complete": bool(search["route_complete"]),
        "formal_batch_result": False,
        "route": route,
        "energy": energy,
        "attempted_prefix_energy": [
            attempt["attempted_prefix_energy"]
            for attempt in all_attempts
            if attempt.get("audit", {}).get("violations")
        ],
        "api_request_count": prior_request_count + search["api_request_count"],
        "current_run_api_request_count": search["api_request_count"],
        "prior_failure_request_count": prior_request_count,
        "request_limit": int(request_limit),
        "cumulative_request_ceiling": maximum,
        "rejected_segment_count": search["rejected_segment_count"],
        "prior_failure_context": recovery,
        "attempts": all_attempts,
        "segment_audits": prior_segment_audits + search["segment_audits"],
        "usage": {
            **search["usage"],
            "complete": complete_usage,
            "account_billing": "not_reported_by_codex_cli",
        },
        "cumulative_usage": cumulative_usage,
        "unresolved_cli_invocation": state.get("unresolved_cli_invocation"),
        "comparison_status": "single_task_pilot_not_formal_group_comparison",
        "limitations": [
            "This is one separately versioned R4 task pilot, not the ten-seed formal result.",
            "An incomplete route reports accepted-prefix estimates only; rejected proposals are not vehicle execution.",
            "Codex CLI does not expose a hard output-token ceiling or account cash billing in this runner.",
            "R4-A/R4-B group alignment and common endpoint metrics remain prerequisites for formal comparison.",
        ],
    }
    _write_json_durable(output_dir / "search_result.json", search)
    _write_json_durable(output_dir / "final_audit.json", final_audit)
    _write_json_durable(output_dir / "state.json", {
        **state,
        "status": search["status"],
        "route_complete": search["route_complete"],
        "finished_at_utc": final_audit["finished_at_utc"],
        "unresolved_cli_invocation": state.get("unresolved_cli_invocation"),
    })
    render_replay(
        item["map_data"],
        {
            "task_id": task.task_id,
            "start_node_id": task.start_node_id,
            "pickup_node_id": task.pickup_node_id,
            "dropoff_node_id": task.dropoff_node_id,
        },
        final_audit,
        output_dir / "replay.html",
    )
    index = """<!doctype html><html lang=zh-CN><meta charset=utf-8><title>R4 单任务分段试跑</title><h1>R4 __PROTOCOL__ task9 单任务试跑</h1><p>状态：__STATUS__。此页不是十种子正式比较。</p><ul><li><a href='replay.html'>打开地图与逐段回放</a></li><li><a href='final_audit.json'>最终审计</a></li><li><a href='manifest.json'>输入/运行清单</a></li><li><a href='state.json'>运行状态（含中断未决调用）</a></li></ul></html>""".replace("__STATUS__", str(search["status"])).replace("__PROTOCOL__", str(search["method_version"]))
    (output_dir / "index.html").write_text(index, encoding="utf-8")
    return final_audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Send model requests; omitted means local preflight only")
    parser.add_argument("--request-limit", type=int, default=0)
    parser.add_argument("--authorization-note", default="")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--preflight-dir", type=Path, default=PREFLIGHT_DIR)
    parser.add_argument("--prior-failure-dir", type=Path, help="Continue after an auditable rejected segment without replaying its completed model request")
    args = parser.parse_args()
    if not args.execute:
        loaded = _load_gate(args.preflight_dir)
        recovery = (
            _build_recovery_context(args.prior_failure_dir, loaded)
            if args.prior_failure_dir
            else None
        )
        remaining_request_bound = (
            int(math.ceil((int(loaded["gate"]["progress_limit"]) - (len(recovery["initial_route_state"]["direction_sequence"]) if recovery else 0)) / float(loaded["gate"].get("max_segment_actions", MAX_SEGMENT_ACTIONS))))
            + int(loaded["gate"].get("max_feedback_requests", MAX_FEEDBACK_REQUESTS))
            - (int(recovery["initial_rejected_segments"]) if recovery else 0)
        )
        print(json.dumps({
            "status": "local_gate_passed_no_model_requests",
            "model_requests": 0,
            "task_id": loaded["gate"]["task_id"],
            "request_upper_bound": loaded["gate"]["maximum_model_requests"],
            "remaining_request_bound": remaining_request_bound,
            "recovery_context": recovery,
            "output_token_hard_limit": None,
            "next_action": "The local gate made no model request; the already authorized pilot can execute within the frozen request bound.",
        }, ensure_ascii=False, indent=2))
        return
    if args.request_limit <= 0:
        raise SystemExit("--execute requires a positive --request-limit within the frozen task bound")
    if not args.authorization_note.strip():
        raise SystemExit("--execute requires a fresh human authorization note")
    print(json.dumps(
        run(
            args.output,
            args.request_limit,
            args.authorization_note,
            args.preflight_dir,
            args.prior_failure_dir,
        ),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
