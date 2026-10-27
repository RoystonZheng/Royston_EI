#!/usr/bin/env python3
"""Continue the explicitly approved progress-v2 R4 task9 pilot.

The stopped parent run is immutable. This continuation carries forward only
responses that have complete local validation evidence, resumes the same
Codex CLI conversation, and applies the human-approved cumulative call cap.
Any unresolved invocation stops the run; it is never replayed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_cli_support import estimate_codex_usage_cost
from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import EvaluationConfig, RouteCandidate
from route_experiment.r4_segment import run_r4_segment_route_search
from run_r4_codex_formal_checkpoint import prepare
from preflight_r4_codex_segment_task9 import (
    MAX_FEEDBACK_REQUESTS,
    MAX_SEGMENT_ACTIONS,
    MAX_TOTAL_ACTIONS,
    PROGRESS_MULTIPLIER,
)
from render_r4_segment_replay import render_replay
from run_r4_codex_segment_task9 import MODEL, _load_gate, _sha256, _source_feature_context


DEFAULT_PARENT = ROOT / "outputs/route_layer/r4_codex_segment_task9_progress_v2_pilot_20261027_v1"
DEFAULT_PRIOR_SETUP_FAILURE = ROOT / "outputs/route_layer/r4_codex_segment_task9_progress_v2_continuation_20261027_v1"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_codex_segment_task9_progress_v2_continuation_20261027_v2"
MAX_CUMULATIVE_REQUESTS = 25
PRICING_CONFIG = ROOT / "configs/codex_cli_pricing_2026-10-22.json"


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


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected_json_object:" + str(path))
    return value


def _trace_thread_ids(path: Path) -> List[str]:
    ids: List[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                ids.append(event["thread_id"])
    return ids


def _audit_prior_local_setup_failure(run_dir: Path) -> Dict[str, Any]:
    run_dir = run_dir.resolve()
    state_path = run_dir / "state.json"
    state = _read_json(state_path)
    invocation = state.get("unresolved_cli_invocation") or {}
    provider = invocation.get("provider_audit") or {}
    event_audit = provider.get("event_audit") or {}
    if (
        invocation.get("request_number") != 7
        or provider.get("cli_exit_code") != 1
        or provider.get("usage_available") is not False
        or event_audit.get("turn_started_count") != 0
        or event_audit.get("usage_event_count") != 0
        or event_audit.get("event_types") != []
        or "Failed to read output schema file" not in str(provider.get("stderr_tail", ""))
    ):
        raise ValueError("prior_continuation_failure_is_not_a_pre_model_local_schema_error")
    trace_rel = provider.get("trace_jsonl_path")
    if not isinstance(trace_rel, str):
        raise ValueError("prior_setup_failure_trace_path_missing")
    trace_path = Path(trace_rel)
    if not trace_path.is_absolute():
        trace_path = ROOT / trace_path
    if not trace_path.is_file() or trace_path.stat().st_size != 0:
        raise ValueError("prior_setup_failure_trace_not_empty_or_missing")
    return {
        "run_path": str(run_dir),
        "state_path": str(state_path),
        "state_sha256": _sha256(state_path.read_bytes()),
        "trace_path": str(trace_path),
        "trace_sha256": _sha256(trace_path.read_bytes()),
        "request_number_in_failed_cli_attempt": 7,
        "turn_started_count": 0,
        "usage_event_count": 0,
        "cli_exit_code": 1,
        "failure_classification": "local_schema_path_error_before_model_turn",
        "counted_as_model_request": False,
        "retry_or_response_replay": False,
    }


def _load_parent_seed(parent_dir: Path, task: Any, map_package: Any) -> Dict[str, Any]:
    manifest_path = parent_dir / "manifest.json"
    state_path = parent_dir / "state.json"
    stop_path = parent_dir / "user_stop_reconciliation_20261027.json"
    offline_path = parent_dir / "offline_validation_audit_call_0006.json"
    manifest = _read_json(manifest_path)
    state = _read_json(state_path)
    stop = _read_json(stop_path)
    offline = _read_json(offline_path)

    if state.get("status") != "stopped_by_user_after_call_reconciliation":
        raise ValueError("parent_run_is_not_the_audited_stopped_progress_v2_run")
    if state.get("model_requests_started") != 6 or state.get("model_requests_completed") != 6:
        raise ValueError("parent_request_count_changed")
    if state.get("network_retries") != 0 or manifest.get("network_retries") != 0:
        raise ValueError("parent_retry_policy_changed")
    if state.get("current_node_id") != "node_68_39" or state.get("accepted_action_count") != 3:
        raise ValueError("parent_committed_state_changed")
    if offline.get("request_number") != 6 or offline.get("validator", {}).get("accepted") is not True:
        raise ValueError("request_6_offline_validation_not_accepted")
    if stop.get("request_6_reconciliation", {}).get("committed_to_route_state") is not False:
        raise ValueError("request_6_was_already_committed_in_parent")
    if stop.get("request_6_reconciliation", {}).get("replayed") is not False:
        raise ValueError("request_6_replay_status_invalid")

    attempt_dir = parent_dir / "attempts"
    trace_dir = parent_dir / "traces"
    prefix_nodes = [task.start_node_id]
    prefix_directions: List[str] = []
    prefix_edges: List[str] = []
    rejected_segments = 0
    parent_attempts: List[Dict[str, Any]] = []
    usage_rows: List[Dict[str, Any]] = []
    session_ids = set()

    for number in range(1, 7):
        attempt_path = attempt_dir / f"attempt_{number:03d}.json"
        trace_path = trace_dir / f"r4_segment_{number:04d}.jsonl"
        attempt = _read_json(attempt_path)
        if not trace_path.is_file():
            raise ValueError("parent_trace_missing:" + str(trace_path))
        ids = _trace_thread_ids(trace_path)
        if len(set(ids)) != 1:
            raise ValueError("parent_codex_session_id_missing_or_ambiguous")
        session_ids.update(ids)

        if number < 6:
            if attempt.get("status") != "response_received":
                raise ValueError("parent_response_not_complete_no_replay:" + str(number))
            audit = attempt.get("audit") or {}
            if audit.get("accepted"):
                steps = audit.get("steps") or []
                if steps and prefix_nodes[-1] != steps[0].get("from_node_id"):
                    raise ValueError("parent_route_prefix_discontinuity:" + str(number))
                prefix_directions.extend(audit.get("committed_direction_sequence") or [])
                prefix_edges.extend(audit.get("committed_edge_sequence") or [])
                prefix_nodes.extend(audit.get("committed_node_sequence") or [])
            else:
                rejected_segments += 1
            usage = attempt.get("usage") or {}
            if not all(isinstance(usage.get(key), int) for key in ("input_tokens", "cached_input_tokens", "output_tokens")):
                raise ValueError("parent_usage_incomplete_no_replay:" + str(number))
            usage_rows.append({"request_number": number, "status": attempt.get("status"), **usage})
            parent_attempts.append(attempt)
        else:
            if attempt.get("status") != "response_received_unvalidated":
                raise ValueError("parent_request_6_status_changed")
            raw_trace = trace_path.read_bytes()
            raw_final = (trace_dir / "r4_segment_0006.final.json").read_bytes()
            if _sha256(raw_trace) != offline.get("raw_trace_sha256"):
                raise ValueError("parent_request_6_trace_hash_mismatch")
            if _sha256(raw_final) != offline.get("final_json_sha256"):
                raise ValueError("parent_request_6_final_hash_mismatch")
            audit = offline["validator"]
            if not audit.get("committed_direction_sequence"):
                raise ValueError("parent_request_6_has_no_validated_action")
            prefix_directions.extend(audit.get("committed_direction_sequence") or [])
            prefix_edges.extend(audit.get("committed_edge_sequence") or [])
            prefix_nodes.extend(audit.get("committed_node_sequence") or [])
            usage = offline.get("usage") or {}
            if not all(isinstance(usage.get(key), int) for key in ("input_tokens", "cached_input_tokens", "output_tokens")):
                raise ValueError("parent_request_6_usage_incomplete")
            usage_rows.append({"request_number": number, "status": "response_received_offline_validator_passed", **usage})
            parent_attempts.append({
                **attempt,
                "effective_status": "response_received_offline_validator_passed_not_committed_in_parent",
                "offline_validation_audit_path": str(offline_path),
            })

    if len(session_ids) != 1:
        raise ValueError("parent_codex_session_changed_between_requests")
    if len(prefix_directions) != 4 or len(prefix_edges) != 4 or len(prefix_nodes) != 5:
        raise ValueError("parent_validated_prefix_does_not_match_expected_call_reconciliation")
    if prefix_nodes[0] != task.start_node_id or prefix_nodes[-1] != "node_68_38":
        raise ValueError("parent_validated_prefix_endpoints_mismatch")
    if len(set(prefix_nodes)) != len(prefix_nodes):
        raise ValueError("parent_validated_prefix_revisits_node")

    route_state = {
        "current_node_id": prefix_nodes[-1],
        "visited_node_ids": prefix_nodes,
        "pickup_seen": task.pickup_node_id in prefix_nodes,
        "direction_sequence": prefix_directions,
        "edge_sequence": prefix_edges,
    }
    if route_state["pickup_seen"]:
        raise ValueError("task9_prefix_unexpectedly_reached_pickup")
    return {
        "manifest": manifest,
        "state": state,
        "stop": stop,
        "offline_audit": offline,
        "session_id": next(iter(session_ids)),
        "route_state": route_state,
        "rejected_segments": rejected_segments,
        "parent_attempts": parent_attempts,
        "usage_rows": usage_rows,
        "parent_request_count": 6,
        "progress_limit": int(stop["prior_state_snapshot"]["request_in_progress"]["progress_limit"]),
        "source_hashes": {
            "parent_manifest_sha256": _sha256(manifest_path.read_bytes()),
            "parent_state_sha256": _sha256(state_path.read_bytes()),
            "parent_stop_audit_sha256": _sha256(stop_path.read_bytes()),
            "parent_call6_offline_audit_sha256": _sha256(offline_path.read_bytes()),
        },
    }


def run_continuation(
    *,
    parent_dir: Path,
    output_dir: Path,
    cumulative_request_cap: int,
    authorization_note: str,
    prior_setup_failure_dir: Path = DEFAULT_PRIOR_SETUP_FAILURE,
) -> Dict[str, Any]:
    parent_dir = parent_dir.resolve()
    output_dir = output_dir.resolve()
    prior_setup_failure = _audit_prior_local_setup_failure(prior_setup_failure_dir)
    if output_dir.exists():
        raise FileExistsError("continuation_output_exists_no_replay_or_overwrite")
    if not 1 <= int(cumulative_request_cap) <= MAX_CUMULATIVE_REQUESTS:
        raise ValueError("cumulative_request_cap_must_be_1_to_25")
    if not authorization_note.strip():
        raise ValueError("current_user_authorization_note_required")

    loaded = _load_gate()
    gate, item = loaded["gate"], loaded["item"]
    task = item["task"]
    parent = _load_parent_seed(parent_dir, task, item["package"])
    if int(cumulative_request_cap) <= parent["parent_request_count"]:
        raise ValueError("cumulative_request_cap_has_no_remaining_calls")
    request_limit = int(cumulative_request_cap) - parent["parent_request_count"]
    if request_limit > int(gate["maximum_model_requests"]) - parent["parent_request_count"]:
        raise ValueError("requested_cap_exceeds_frozen_preflight_upper_bound")
    if parent["progress_limit"] != 218 or parent["manifest"].get("maximum_model_requests") != 25:
        raise ValueError("parent_progress_v2_protocol_limits_changed")

    output_dir.mkdir(parents=True)
    client = CodexCliRouteStepClient(
        model=MODEL,
        trace_dir=output_dir / "traces",
        reasoning_effort="low",
        timeout_sec=300,
        persist_session=True,
        session_id=parent["session_id"],
        working_directory=output_dir / "codex_empty_workspace",
        trace_stem="r4_segment",
    )
    client.trace_index = parent["parent_request_count"]
    if client.retries != 0:
        raise RuntimeError("r4_progress_v2_network_retries_must_be_zero")

    manifest = {
        "schema_version": "r4_codex_segment_progress_v2_continuation_manifest_v1",
        "experiment_id": "r4_codex_segment_task9_progress_v2_continuation_20261027_v2",
        "status": "ready_for_explicit_execution",
        "authorization_note": authorization_note.strip(),
        "authorization_source": "current_user_direct_reply_2026-10-27",
        "authorization_scope": "continue only the frozen progress-v2 task9 pilot; no formal batch",
        "parent_run_path": str(parent_dir),
        "prior_local_cli_setup_failure": prior_setup_failure,
        "prior_cli_setup_failures_not_counted_as_model_requests": 1,
        "parent_request_count": parent["parent_request_count"],
        "cumulative_request_cap": int(cumulative_request_cap),
        "request_number_offset": parent["parent_request_count"],
        "requests_authorized_in_this_continuation": request_limit,
        "effective_request_limit": request_limit,
        "cumulative_requests_left_at_start": request_limit,
        "parent_rejected_segments": parent["rejected_segments"],
        "maximum_rejected_segments_total": MAX_FEEDBACK_REQUESTS,
        "remaining_rejected_segment_feedback_budget": MAX_FEEDBACK_REQUESTS - parent["rejected_segments"],
        "model": MODEL,
        "reasoning_effort": "low",
        "session_mode": "persistent_per_task_resumed_from_parent_session",
        "codex_session_id": parent["session_id"],
        "codex_cli_version": client.cli_version,
        "network_retries": 0,
        "traditional_fallback": False,
        "tool_events_allowed": False,
        "max_segment_actions": MAX_SEGMENT_ACTIONS,
        "progress_limit": parent["progress_limit"],
        "initial_validated_route_prefix": parent["route_state"],
        "frozen_input_hashes": loaded["observed_hashes"],
        "preflight_sha256": _sha256((ROOT / "outputs/route_layer/r4_codex_segment_task9_preflight_v6/preflight.json").read_bytes()),
        "parent_evidence_hashes": parent["source_hashes"],
        "account_billing": "not_reported_by_codex_cli",
        "formal_batch_started": False,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_json_durable(output_dir / "manifest.json", manifest)
    seed = {
        "schema_version": "r4_progress_v2_continuation_seed_v1",
        "parent_run_path": str(parent_dir),
        "parent_evidence_hashes": parent["source_hashes"],
        "route_state_after_offline_validated_call_6": parent["route_state"],
        "prior_rejected_segment_count": parent["rejected_segments"],
        "prior_usage_rows": parent["usage_rows"],
        "prior_attempt_references": [
            {"request_number": i, "attempt_path": str(parent_dir / "attempts" / f"attempt_{i:03d}.json"),
             "trace_path": str(parent_dir / "traces" / f"r4_segment_{i:04d}.jsonl")}
            for i in range(1, 7)
        ],
        "session_id": parent["session_id"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_json_durable(output_dir / "continuation_seed.json", seed)

    state: Dict[str, Any] = {
        "experiment_id": manifest["experiment_id"],
        "status": "ready",
        "model_requests_started": parent["parent_request_count"],
        "model_requests_completed": parent["parent_request_count"],
        "new_model_requests_started": 0,
        "new_model_requests_completed": 0,
        "accepted_action_count": len(parent["route_state"]["direction_sequence"]),
        "current_node_id": parent["route_state"]["current_node_id"],
        "pickup_seen": parent["route_state"]["pickup_seen"],
        "effective_request_limit": request_limit,
        "cumulative_request_cap": int(cumulative_request_cap),
        "request_number_offset": parent["parent_request_count"],
        "network_retries": 0,
        "unresolved_cli_invocation": None,
        "parent_state_is_immutable": True,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    state_path = output_dir / "state.json"
    _write_json_durable(state_path, state)

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
            "new_model_requests_started": number - parent["parent_request_count"],
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
        previous = _read_json(attempt_path)
        finalized = {
            **previous,
            **attempt,
            "provider_audit": client.last_record,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "delivery_status": "response_received" if attempt.get("status") == "response_received" else "unknown_or_error",
        }
        _write_json_durable(attempt_path, finalized)
        audit = attempt.get("audit") or {}
        if audit.get("accepted"):
            state["accepted_action_count"] = int(state.get("accepted_action_count", 0)) + len(audit.get("committed_direction_sequence", []))
            state["current_node_id"] = audit.get("current_node_after_segment", state["current_node_id"])
            state["pickup_seen"] = audit.get("pickup_seen_after_segment", state["pickup_seen"])
        status = attempt.get("status")
        state.update({
            "status": "response_recorded" if status == "response_received" else "request_error_no_retry",
            "model_requests_completed": number,
            "new_model_requests_completed": number - parent["parent_request_count"],
            "last_request_status": status,
            "last_request_usage": attempt.get("usage"),
            "unresolved_cli_invocation": None if status == "response_received" else {
                "request_number": number,
                "status": "delivery_and_usage_unknown_or_error",
                "provider_audit": client.last_record,
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
        max_api_calls=request_limit,
        max_segment_actions=MAX_SEGMENT_ACTIONS,
        max_total_actions=MAX_TOTAL_ACTIONS,
        max_progress_multiplier=PROGRESS_MULTIPLIER,
        max_feedback_requests=MAX_FEEDBACK_REQUESTS,
        route_feature_context=_source_feature_context(),
        on_request_start=on_request_start,
        on_attempt=on_attempt,
        initial_route_state=parent["route_state"],
        progress_limit_override=parent["progress_limit"],
        initial_rejected_segments=parent["rejected_segments"],
        request_number_offset=parent["parent_request_count"],
    )

    all_attempts = parent["parent_attempts"] + [
        _read_json(path) for path in sorted((output_dir / "attempts").glob("attempt_*.json"))
    ]
    all_usage_rows = list(parent["usage_rows"])
    for attempt in all_attempts[6:]:
        usage = attempt.get("usage") or {}
        all_usage_rows.append({"request_number": attempt.get("request_number"), "status": attempt.get("status"), **usage})
    known_input = sum(row["input_tokens"] for row in all_usage_rows if isinstance(row.get("input_tokens"), int))
    known_cached = sum(row["cached_input_tokens"] for row in all_usage_rows if isinstance(row.get("cached_input_tokens"), int))
    known_output = sum(row["output_tokens"] for row in all_usage_rows if isinstance(row.get("output_tokens"), int))
    expected_call_count = parent["parent_request_count"] + search["api_request_count"]
    usage_unknown_requests = [
        row.get("request_number") for row in all_usage_rows
        if not all(isinstance(row.get(key), int) for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
    ]
    usage_complete = len(all_usage_rows) == expected_call_count and not usage_unknown_requests
    cost_estimate = estimate_codex_usage_cost(
        MODEL,
        known_input if usage_complete else None,
        known_cached if usage_complete else None,
        known_output if usage_complete else None,
        pricing_config_path=PRICING_CONFIG,
    )
    usage_total = {
        "call_count": expected_call_count,
        "known_input_tokens": known_input,
        "known_cached_input_tokens": known_cached,
        "known_output_tokens": known_output,
        "cached_input_is_subset_of_input": True,
        "usage_complete": usage_complete,
        "usage_unknown_request_numbers": usage_unknown_requests,
        "account_billing": "not_reported_by_codex_cli",
        "rate_card_estimate": cost_estimate,
        "per_request_rows": all_usage_rows,
    }

    route = search["route"]
    if route["edge_sequence"]:
        candidate = RouteCandidate(
            route_id="r4-progress-v2-continuation-" + task.task_id,
            node_sequence=route["node_sequence"],
            edge_sequence=route["edge_sequence"],
            route_source="r4_codex_segment_progress_v2",
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
            "note": "共享确定性评估器对累计已接纳合法路线段的估算；不是实车测量，也不含被拒绝提议。",
        }
    else:
        energy = {"scope": "not_estimable_no_accepted_edges", "energy_kwh": None, "distance_m": None, "travel_time_sec": None, "simulation_estimate": True}

    final_audit = {
        "schema_version": "r4_codex_segment_progress_v2_continuation_final_audit_v1",
        "experiment_id": manifest["experiment_id"],
        "parent_run_path": str(parent_dir),
        "parent_evidence_hashes": parent["source_hashes"],
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": search["status"],
        "model": MODEL,
        "session_id": parent["session_id"],
        "task_id": task.task_id,
        "map_id": item["road_map"].map_id,
        **loaded["observed_hashes"],
        "route_complete": bool(search["route_complete"]),
        "formal_batch_result": False,
        "route": route,
        "energy": energy,
        "parent_request_count": parent["parent_request_count"],
        "continuation_request_count": search["api_request_count"],
        "cumulative_request_count": expected_call_count,
        "cumulative_request_cap": int(cumulative_request_cap),
        "remaining_request_budget": max(0, int(cumulative_request_cap) - expected_call_count),
        "rejected_segment_count_total": search["rejected_segment_count"],
        "rejected_segment_limit": MAX_FEEDBACK_REQUESTS,
        "attempts": all_attempts,
        "usage": usage_total,
        "unresolved_cli_invocation": state.get("unresolved_cli_invocation"),
        "comparison_status": "single_task_progress_v2_pilot_not_formal_group_comparison",
        "limitations": [
            "Single frozen seed-0 task9 pilot; no cross-seed statistical inference.",
            "Only validator-accepted route segments are included in route and energy metrics.",
            "A partial route is not a completed route result; its energy is a simulated prefix estimate.",
            "Codex CLI does not report account cash billing; rate-card equivalent is not an invoice.",
            "No traditional fallback or network retry is used.",
        ],
    }
    _write_json_durable(output_dir / "search_result.json", search)
    _write_json_durable(output_dir / "cumulative_usage_ledger.json", usage_total)
    _write_json_durable(output_dir / "final_audit.json", final_audit)
    _write_json_durable(output_dir / "state.json", {
        **state,
        "status": search["status"],
        "route_complete": search["route_complete"],
        "cumulative_request_count": expected_call_count,
        "finished_at_utc": final_audit["finished_at_utc"],
        "unresolved_cli_invocation": state.get("unresolved_cli_invocation"),
    })
    render_replay(
        item["map_data"],
        {"task_id": task.task_id, "start_node_id": task.start_node_id,
         "pickup_node_id": task.pickup_node_id, "dropoff_node_id": task.dropoff_node_id},
        final_audit,
        output_dir / "replay.html",
    )
    index = """<!doctype html><html lang=zh-CN><meta charset=utf-8><title>R4 progress-v2续跑</title><h1>R4 progress-v2 task9续跑</h1><p>状态：__STATUS__。累计调用不超过25次，无传统回退、无重试。</p><ul><li><a href='replay.html'>地图与逐段回放</a></li><li><a href='final_audit.json'>最终审计</a></li><li><a href='cumulative_usage_ledger.json'>累计用量账本</a></li><li><a href='manifest.json'>运行清单</a></li></ul></html>""".replace("__STATUS__", str(search["status"]))
    (output_dir / "index.html").write_text(index, encoding="utf-8")
    return final_audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Send up to the explicitly approved remaining requests")
    parser.add_argument("--parent", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--prior-cli-setup-failure-dir", type=Path, default=DEFAULT_PRIOR_SETUP_FAILURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cumulative-request-cap", type=int, default=25)
    parser.add_argument("--authorization-note", default="")
    args = parser.parse_args()
    if not args.execute:
        if args.output.exists():
            raise SystemExit("continuation output already exists; no overwrite or replay")
        loaded = _load_gate()
        parent = _load_parent_seed(args.parent, loaded["item"]["task"], loaded["item"]["package"])
        setup_failure = _audit_prior_local_setup_failure(args.prior_cli_setup_failure_dir)
        parent_calls = parent["parent_request_count"]
        if not parent_calls < args.cumulative_request_cap <= MAX_CUMULATIVE_REQUESTS:
            raise SystemExit("cumulative cap must leave at least one request and may not exceed 25")
        print(json.dumps({
            "status": "local_resume_preflight_only_no_model_requests",
            "parent_calls_already_used": parent_calls,
            "remaining_requests_at_cap": max(0, args.cumulative_request_cap - parent_calls),
            "maximum_cumulative_requests": MAX_CUMULATIVE_REQUESTS,
            "validated_prefix_actions": len(parent["route_state"]["direction_sequence"]),
            "current_node_id": parent["route_state"]["current_node_id"],
            "prior_rejected_segments": parent["rejected_segments"],
            "prior_local_cli_setup_failure": setup_failure["failure_classification"],
            "prior_setup_failure_model_request_counted": setup_failure["counted_as_model_request"],
            "frozen_task_id": loaded["gate"]["task_id"],
            "output_directory_available": True,
            "next_action": "The current user approval will be recorded in the execution manifest; --execute sends only the remaining calls.",
        }, ensure_ascii=False, indent=2))
        return
    if not args.authorization_note.strip():
        raise SystemExit("--execute requires the current user approval in --authorization-note")
    result = run_continuation(
        parent_dir=args.parent,
        output_dir=args.output,
        cumulative_request_cap=args.cumulative_request_cap,
        authorization_note=args.authorization_note,
        prior_setup_failure_dir=args.prior_cli_setup_failure_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
