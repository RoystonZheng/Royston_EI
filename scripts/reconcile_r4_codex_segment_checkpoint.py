#!/usr/bin/env python3
"""Reconcile a fully persisted Codex CLI response after its parent runner stopped.

This utility makes no model requests. It accepts only a single in-progress
attempt whose exact prompt, schema, completed CLI event stream, final response,
usage event, and deterministic route validation all agree. It then writes a
recoverable checkpoint that the regular R4 runner can continue from.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import EvaluationConfig, RouteCandidate
from route_experiment.r4_segment import make_r4_segment_constraint, validate_r4_segment_proposal
from run_r4_codex_segment_task9 import _attempted_prefix_energy, _load_gate, _write_json_durable


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_events(path: Path) -> List[Dict[str, Any]]:
    events = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"trace_json_parse_error_line_{line_number}") from error
        if not isinstance(event, dict):
            raise RuntimeError(f"trace_event_not_object_line_{line_number}")
        events.append(event)
    return events


def _find_matching_trace(run_dir: Path, started: Dict[str, Any]) -> Dict[str, Path]:
    trace_dir = run_dir / "traces"
    expected_prompt_hash = started.get("rendered_cli_prompt_sha256")
    matches = []
    for prompt_path in trace_dir.glob("*.prompt.txt"):
        if _sha256(prompt_path.read_bytes()) == expected_prompt_hash:
            stem = prompt_path.name.removesuffix(".prompt.txt")
            matches.append({
                "prompt": prompt_path,
                "schema": trace_dir / f"{stem}.schema.json",
                "jsonl": trace_dir / f"{stem}.jsonl",
                "final": trace_dir / f"{stem}.final.json",
            })
    if len(matches) != 1:
        raise RuntimeError(f"matching_persisted_prompt_count_{len(matches)}")
    paths = matches[0]
    if any(not path.is_file() for path in paths.values()):
        raise RuntimeError("matching_cli_trace_artifact_missing")
    return paths


def _reconstruct_attempt(run_dir: Path, preflight_dir: Path) -> Dict[str, Any] | None:
    run_dir = run_dir.resolve()
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    if state.get("unresolved_cli_invocation") is None and state.get("status") == "response_recorded":
        return None
    in_progress = state.get("request_in_progress")
    if not isinstance(in_progress, dict) or in_progress.get("status") != "request_in_progress":
        raise RuntimeError("no_single_request_in_progress_to_reconcile")
    number = int(in_progress["request_number"])
    attempt_path = run_dir / "attempts" / f"attempt_{number:03d}.json"
    original_attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    if original_attempt.get("status") != "request_in_progress":
        raise RuntimeError("attempt_record_not_in_progress")
    for key in (
        "request_number", "current_node_id", "visited_node_ids", "accepted_action_count",
        "pickup_seen", "progress_limit", "messages_sha256", "schema_sha256",
        "rendered_cli_prompt_sha256",
    ):
        if original_attempt.get(key) != in_progress.get(key):
            raise RuntimeError("in_progress_state_attempt_mismatch:" + key)

    trace = _find_matching_trace(run_dir, in_progress)
    prompt_bytes = trace["prompt"].read_bytes()
    schema_payload = json.loads(trace["schema"].read_text(encoding="utf-8"))
    max_actions = schema_payload.get("properties", {}).get("actions", {}).get("maxItems")
    min_actions = schema_payload.get("properties", {}).get("actions", {}).get("minItems", 0)
    constraint = make_r4_segment_constraint(max_actions, min_actions)
    if constraint.schema != schema_payload or constraint.schema_sha256 != in_progress.get("schema_sha256"):
        raise RuntimeError("persisted_schema_does_not_match_started_request")
    if _sha256(prompt_bytes) != in_progress.get("rendered_cli_prompt_sha256"):
        raise RuntimeError("persisted_prompt_hash_mismatch")

    events = _load_events(trace["jsonl"])
    event_audit = CodexCliRouteStepClient._audit_events(events)
    if (
        event_audit.get("turn_started_count") != 1
        or event_audit.get("usage_event_count") != 1
        or event_audit.get("tool_usage_detected") is not False
        or sum(event.get("type") == "turn.completed" for event in events) != 1
        or any(
            event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "error"
            for event in events
        )
    ):
        raise RuntimeError("persisted_cli_event_audit_incomplete_or_invalid")
    messages = [
        event["item"].get("text")
        for event in events
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "agent_message"
    ]
    final_raw = trace["final"].read_text(encoding="utf-8")
    if len(messages) != 1 or messages[0] != final_raw:
        raise RuntimeError("persisted_agent_message_and_final_artifact_mismatch")
    try:
        proposal = json.loads(final_raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("persisted_final_response_invalid_json") from error
    if not all(isinstance(event_audit.get(field), int) for field in ("input_tokens", "output_tokens")):
        raise RuntimeError("persisted_usage_event_missing_token_counts")

    loaded = _load_gate(preflight_dir)
    item = loaded["item"]
    validation = validate_r4_segment_proposal(
        item["road_map"],
        start_node_id=item["task"].start_node_id,
        pickup_node_id=item["task"].pickup_node_id,
        dropoff_node_id=item["task"].dropoff_node_id,
        current_node_id=in_progress["current_node_id"],
        pickup_seen=bool(in_progress["pickup_seen"]),
        visited_node_ids=in_progress["visited_node_ids"],
        payload=proposal,
        accepted_action_count=int(in_progress["accepted_action_count"]),
        progress_limit=int(in_progress["progress_limit"]),
        max_segment_actions=int(max_actions),
        min_segment_actions=int(min_actions),
        blocked_edge_ids=item["package"].blocked_edge_ids,
        allowed_edge_ids=(edge.edge_id for edge in item["package"].edges),
    )
    usage = {
        key: event_audit.get(key)
        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    }
    usage.update({"latency_ms": None, "retries": 0})
    provider_audit = {
        "backend": "codex_cli_trace_recovered_after_parent_termination",
        "status": "response_reconstructed_from_complete_persisted_trace",
        "model": json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")).get("model"),
        "cli_exit_code": None,
        "cli_exit_code_note": "Parent runner exited before recording the subprocess return code; complete turn, final JSON, and usage are present on disk.",
        "event_audit": event_audit,
        "trace_jsonl_path": str(trace["jsonl"].resolve()),
        "trace_jsonl_sha256": _sha256(trace["jsonl"].read_bytes()),
        "prompt_path": str(trace["prompt"].resolve()),
        "prompt_sha256": _sha256(prompt_bytes),
        "schema_path": str(trace["schema"].resolve()),
        "schema_sha256": _sha256(trace["schema"].read_bytes()),
        "final_json_path": str(trace["final"].resolve()),
        "final_json_sha256": _sha256(trace["final"].read_bytes()),
        "network_retries": 0,
        "tool_events_allowed": False,
        "account_billing": "not_reported_by_cli",
    }
    result = {
        **original_attempt,
        "status": "response_received",
        "delivery_status": "response_reconstructed_from_complete_persisted_trace",
        "reconciled_after_parent_exit_code": 143,
        "request_id": str(trace["jsonl"].resolve()),
        "model": provider_audit["model"],
        "finish_reason": "turn.completed",
        "usage": usage,
        "constraint_schema_sha256": constraint.schema_sha256,
        "constraint_schema_bytes": constraint.schema_bytes,
        "proposal": proposal,
        "audit": validation,
        "provider_audit": provider_audit,
        "reconciliation_evidence": {
            "prompt_hash_matched_started_attempt": True,
            "schema_hash_matched_started_attempt": True,
            "agent_message_matches_final_json": True,
            "complete_turn_and_usage_events": True,
            "deterministic_route_validation": validation.get("status"),
        },
    }
    return result


def _merge_route(base_route: Dict[str, Any], attempts: List[Dict[str, Any]]) -> Dict[str, Any]:
    route = copy.deepcopy(base_route)
    for attempt in attempts:
        audit = attempt.get("audit") or {}
        if not audit.get("accepted"):
            continue
        if attempt.get("current_node_id") != route["node_sequence"][-1]:
            raise RuntimeError("reconciled_attempt_route_prefix_mismatch")
        if list(attempt.get("visited_node_ids") or []) != route["node_sequence"]:
            raise RuntimeError("reconciled_attempt_visited_state_mismatch")
        route["node_sequence"].extend(audit.get("committed_node_sequence") or [])
        route["edge_sequence"].extend(audit.get("committed_edge_sequence") or [])
        route["direction_sequence"].extend(audit.get("committed_direction_sequence") or [])
    route["last_node_id"] = route["node_sequence"][-1]
    route["move_count"] = len(route["direction_sequence"])
    route["pickup_seen"] = route["pickup_node_id"] in route["node_sequence"]
    route["route_complete"] = route["pickup_seen"] and route["last_node_id"] == route["dropoff_node_id"]
    return route


def reconcile(run_dir: Path, prior_failure_dir: Path, preflight_dir: Path, checkpoint_dir: Path) -> Dict[str, Any]:
    run_dir = run_dir.resolve()
    prior_failure_dir = prior_failure_dir.resolve()
    checkpoint_dir = checkpoint_dir.resolve()
    if checkpoint_dir.exists():
        raise FileExistsError("checkpoint_output_exists")
    loaded = _load_gate(preflight_dir)
    gate = loaded["gate"]
    prior_audit = json.loads((prior_failure_dir / "final_audit.json").read_text(encoding="utf-8"))
    prior_manifest = json.loads((prior_failure_dir / "manifest.json").read_text(encoding="utf-8"))
    v4_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    recovered_attempt = _reconstruct_attempt(run_dir, preflight_dir)
    if recovered_attempt is not None:
        _write_json_durable(run_dir / "reconciliation.json", {
            "status": "reconstructed_from_complete_persisted_cli_trace",
            "request_number": recovered_attempt["request_number"],
            "attempt_file_preserved_as_original_interruption_marker": str((run_dir / "attempts" / f"attempt_{recovered_attempt['request_number']:03d}.json").resolve()),
            "reconstructed_attempt": recovered_attempt,
            "note": "No model request was resent. Original in-progress attempt remains unchanged; this sidecar preserves the reconciled response.",
        })
    else:
        _write_json_durable(run_dir / "reconciliation.json", {
            "status": "runner_stopped_between_audited_model_turns",
            "request_number": None,
            "note": "No model request was in progress at the stop point; all stored attempts remain as recorded.",
        })

    new_attempts = []
    attempt_paths = sorted((run_dir / "attempts").glob("attempt_*.json"))
    for attempt_path in attempt_paths:
        current = json.loads(attempt_path.read_text(encoding="utf-8"))
        if recovered_attempt is not None and current.get("request_number") == recovered_attempt["request_number"]:
            current = recovered_attempt
        if current.get("status") != "response_received":
            raise RuntimeError("run_contains_unresolved_or_unclosed_attempt")
        current = dict(current)
        current["source_run_dir"] = str(run_dir)
        current["attempted_prefix_energy"] = _attempted_prefix_energy(loaded["item"], current)
        new_attempts.append(current)

    attempts = [copy.deepcopy(attempt) for attempt in (prior_audit.get("attempts") or [])] + new_attempts
    if len(attempts) != int(prior_audit["api_request_count"]) + len(new_attempts):
        raise RuntimeError("checkpoint_attempt_count_mismatch")
    if [int(attempt.get("request_number", -1)) for attempt in attempts] != list(range(1, len(attempts) + 1)):
        raise RuntimeError("checkpoint_request_numbers_not_contiguous")
    for attempt in attempts:
        attempt.setdefault("source_run_dir", str(prior_failure_dir))
        if attempt.get("attempted_prefix_energy") is None:
            attempt["attempted_prefix_energy"] = _attempted_prefix_energy(loaded["item"], attempt)

    route = _merge_route(prior_audit["route"], new_attempts)
    interrupted_state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    expected_moves = int(interrupted_state["accepted_action_count"])
    if recovered_attempt is not None and recovered_attempt.get("audit", {}).get("accepted"):
        expected_moves += len(recovered_attempt["audit"].get("committed_direction_sequence") or [])
    if route["move_count"] != expected_moves:
        raise RuntimeError("checkpoint_route_and_runner_state_move_count_mismatch")
    token_complete = all(
        attempt.get("status") == "response_received"
        and isinstance(attempt.get("usage"), dict)
        and isinstance(attempt["usage"].get("input_tokens"), int)
        and isinstance(attempt["usage"].get("output_tokens"), int)
        for attempt in attempts
    )
    if not token_complete:
        raise RuntimeError("checkpoint_usage_incomplete")
    input_tokens = sum(attempt["usage"]["input_tokens"] for attempt in attempts)
    output_tokens = sum(attempt["usage"]["output_tokens"] for attempt in attempts)
    prior_search = json.loads((prior_failure_dir / "search_result.json").read_text(encoding="utf-8"))
    all_segment_audits = list(prior_audit.get("segment_audits") or []) + [
        attempt["audit"] for attempt in new_attempts
    ]
    rejected_count = sum(bool((attempt.get("audit") or {}).get("violations")) for attempt in attempts)
    now = datetime.now(timezone.utc).isoformat()
    status = "process_interrupted_reconciled_checkpoint"
    search_result = {
        **prior_search,
        "method_version": v4_manifest.get("segment_protocol", "r4-segment-v3"),
        "status": status,
        "route_complete": bool(route["route_complete"]),
        "api_call_limit": len(new_attempts),
        "api_request_count": len(attempts),
        "rejected_segment_count": rejected_count,
        "attempts": attempts,
        "segment_audits": all_segment_audits,
        "route": route,
        "usage": {
            "usage_complete": token_complete,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "serialized_user_input_bytes": None,
            "serialized_messages_bytes": None,
            "serialized_cli_prompt_bytes": None,
            "serialized_cli_prompt_scope": "Not reconstructed for the whole checkpoint; per-request prompt hashes and sizes remain in attempt records.",
            "client_retries": 0,
        },
    }
    energy = None
    if route["edge_sequence"]:
        evaluated = evaluate_route_candidate(
            loaded["item"]["road_map"],
            RouteCandidate(
                route_id="r4-reconciled-checkpoint-" + route["dropoff_node_id"],
                node_sequence=route["node_sequence"],
                edge_sequence=route["edge_sequence"],
                route_source="r4_codex_segment_reconciled_checkpoint",
                route_reachability=True,
                validation_status="valid_prefix",
            ),
            EvaluationConfig(),
        )
        energy = {
            "scope": "complete_model_route" if route["route_complete"] else "accepted_legal_prefix_only",
            "energy_kwh": evaluated.energy_total_kwh,
            "distance_m": evaluated.route_distance_m,
            "travel_time_sec": evaluated.total_travel_time_sec,
            "energy_model_version": evaluated.energy_model_version,
            "trace_fidelity": evaluated.energy_trace_fidelity,
            "simulation_estimate": True,
            "note": "Checkpoint estimate covers only deterministically accepted legal edges.",
        }
    usage = {
        "usage_complete": token_complete,
        "complete": token_complete,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "client_retries": 0,
        "account_billing": "not_reported_by_codex_cli",
    }
    cumulative_usage = {
        "usage_complete": token_complete,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model_turn_count": len(attempts),
        "account_billing": "not_reported_by_codex_cli",
        "scope": "all audited R4 pilot turns through the reconciled interruption checkpoint",
    }
    final_audit = {
        **prior_audit,
        "schema_version": "r4_codex_segment_task9_final_audit_v3",
        "experiment_id": v4_manifest.get("experiment_id"),
        "segment_protocol": v4_manifest.get("segment_protocol", "r4-segment-v3"),
        "continued_from_segment_protocol": v4_manifest.get("continued_from_segment_protocol"),
        "created_at_utc": v4_manifest.get("created_at_utc"),
        "finished_at_utc": now,
        "status": status,
        "route_complete": bool(route["route_complete"]),
        "route": route,
        "energy": energy,
        "attempted_prefix_energy": [
            attempt["attempted_prefix_energy"] for attempt in attempts
            if (attempt.get("audit") or {}).get("violations")
        ],
        "api_request_count": len(attempts),
        "current_run_api_request_count": len(new_attempts),
        "prior_failure_request_count": len(prior_audit.get("attempts") or []),
        "request_limit": len(new_attempts),
        "cumulative_request_ceiling": int(gate["maximum_model_requests"]),
        "rejected_segment_count": rejected_count,
        "attempts": attempts,
        "segment_audits": all_segment_audits,
        "usage": usage,
        "cumulative_usage": cumulative_usage,
        "unresolved_cli_invocation": None,
        "reconciliation": {
            "parent_runner_exit_code": 143,
            "request_number": recovered_attempt.get("request_number") if recovered_attempt else None,
            "trace_complete": True if recovered_attempt else None,
            "final_response_and_usage_recovered": True if recovered_attempt else None,
            "cli_exit_code_observed": False if recovered_attempt else None,
            "model_request_replayed": False,
            "original_interruption_attempt_preserved": recovered_attempt is not None,
            "active_invocation_at_stop": recovered_attempt is not None,
        },
        "comparison_status": "single_task_pilot_not_formal_group_comparison",
    }
    checkpoint_dir.mkdir(parents=True)
    checkpoint_manifest = {
        **v4_manifest,
        "status": status,
        "checkpoint_reconciled_from_run_dir": str(run_dir),
        "max_segment_actions": int(gate["max_segment_actions"]),
        "progress_limit": int(gate["progress_limit"]),
        "reconciled_at_utc": now,
    }
    _write_json_durable(checkpoint_dir / "manifest.json", checkpoint_manifest)
    _write_json_durable(checkpoint_dir / "search_result.json", search_result)
    _write_json_durable(checkpoint_dir / "final_audit.json", final_audit)
    _write_json_durable(checkpoint_dir / "recovery_report.json", {
        "status": status,
        "source_run_dir": str(run_dir),
        "continuation_source_dir": str(prior_failure_dir),
        "reconciled_request_number": recovered_attempt.get("request_number") if recovered_attempt else None,
        "all_attempts_auditable": True,
        "all_route_edges_accepted_by_deterministic_validator": True,
        "model_requests_sent_by_reconciler": 0,
        "accepted_route_moves": route["move_count"],
        "route_complete": route["route_complete"],
        "cumulative_input_tokens": input_tokens,
        "cumulative_output_tokens": output_tokens,
        "unknown_delivery_remaining": False,
        "account_billing": "not_reported_by_codex_cli",
    })
    _write_json_durable(checkpoint_dir / "state.json", {
        "status": status,
        "accepted_action_count": route["move_count"],
        "current_node_id": route["last_node_id"],
        "route_complete": route["route_complete"],
        "model_requests_started": len(attempts),
        "model_requests_completed": len(attempts),
        "unresolved_cli_invocation": None,
        "updated_at_utc": now,
    })
    return {
        "status": status,
        "checkpoint_dir": str(checkpoint_dir),
        "reconciled_request_number": recovered_attempt.get("request_number") if recovered_attempt else None,
        "accepted_route_moves": route["move_count"],
        "route_complete": route["route_complete"],
        "api_request_count": len(attempts),
        "rejected_segment_count": rejected_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reconciler_model_requests": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--prior-failure-dir", type=Path, required=True)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(reconcile(args.run_dir, args.prior_failure_dir, args.preflight_dir, args.checkpoint_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
