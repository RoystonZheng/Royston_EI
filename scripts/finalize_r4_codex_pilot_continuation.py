#!/usr/bin/env python3
"""Finalize an offline audit and rate-card estimate for a Codex R4 pilot run."""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_cli_support import (  # noqa: E402
    estimate_codex_usage_cost,
    normalize_session_cumulative_usage,
)
from route_experiment.costs import evaluate_route_candidate  # noqa: E402
from route_experiment.failure_audit import estimate_route_proposal_energy  # noqa: E402
from route_experiment.models import Edge, Node, RoadMap, RoadsideFeature, RouteCandidate  # noqa: E402
from route_experiment.r4_agent import replay_directed_edge_sequence  # noqa: E402
from route_experiment.r4_stepwise import (  # noqa: E402
    _DIRECTION_DELTAS,
    _model_self_check_mismatch_fields,
)
from render_r4_codex_pilot_replay import render as render_pilot_replay  # noqa: E402


PREFLIGHT = ROOT / "outputs/route_layer/r4_codex_offline_preflight_20261021/seed0_semantic_road_feature.json"
DEFAULT_MODEL = "gpt-5.6-luna"


def _combine_cumulative_usage(
    current_total: float,
    parent_audit: Dict[str, Any],
    cumulative_field: str,
    *,
    has_parent: bool,
    fallback_prior: float = 0.0,
) -> Optional[float]:
    """Combine this run's usage without masking an incomplete parent ledger."""
    if has_parent and cumulative_field in parent_audit:
        parent_total = parent_audit.get(cumulative_field)
        if parent_total is None:
            return None
        return float(parent_total) + float(current_total)
    return float(fallback_prior) + float(current_total)


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_hash_matches(path: Path, expected_sha256: Optional[str], allow_terminal_newline: bool = False) -> bool:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() == expected_sha256:
        return True
    return bool(
        allow_terminal_newline
        and raw.endswith(b"\n")
        and hashlib.sha256(raw[:-1]).hexdigest() == expected_sha256
    )


def load_map(preflight_path: Optional[Path] = None) -> RoadMap:
    source = read_json(preflight_path or PREFLIGHT)
    if isinstance(source.get("map"), dict):
        map_data = source["map"]
    else:
        map_ref = source.get("map_ref")
        if not isinstance(map_ref, dict) or not map_ref.get("path"):
            raise RuntimeError("frozen_map_reference_missing")
        map_path = Path(map_ref["path"])
        if not map_path.is_absolute():
            map_path = ROOT / map_path
        if not map_path.is_file():
            raise RuntimeError("frozen_map_reference_missing_file")
        if hashlib.sha256(map_path.read_bytes()).hexdigest() != map_ref.get("sha256"):
            raise RuntimeError("frozen_map_reference_hash_mismatch")
        map_data = read_json(map_path)
    return RoadMap(
        map_id=map_data["map_id"],
        version=map_data["version"],
        size=int(map_data["size"]),
        nodes=[Node(**item) for item in map_data["nodes"]],
        edges=[Edge(**item) for item in map_data["edges"]],
        boundary=dict(map_data["boundary"]),
        road_state_version=map_data["road_state_version"],
        roadside_features=[RoadsideFeature(**item) for item in map_data["roadside_features"]],
    )


def _trace_usage(path: Path) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    parse_errors = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            parse_errors += 1
    completed = [
        event.get("usage") or event.get("turn", {}).get("usage")
        for event in events
        if event.get("type") == "turn.completed"
    ]
    completed = [row for row in completed if isinstance(row, dict)]
    input_tokens = sum(int(row.get("input_tokens", 0) or 0) for row in completed) if completed else None
    output_tokens = sum(int(row.get("output_tokens", 0) or 0) for row in completed) if completed else None
    cached = []
    for row in completed:
        value = row.get("cached_input_tokens")
        if value is None and isinstance(row.get("input_tokens_details"), dict):
            value = row["input_tokens_details"].get("cached_tokens")
        cached.append(value)
    cached_tokens = sum(int(value) for value in cached) if cached and all(isinstance(v, int) for v in cached) else None
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "trace_available": True,
        "turn_started_count": sum(event.get("type") == "turn.started" for event in events),
        "turn_completed_usage_count": len(completed),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "tool_item_types": sorted({
            str(event.get("item", {}).get("type"))
            for event in events
            if event.get("type") in {"item.started", "item.completed"}
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") not in {"agent_message", "reasoning", "error"}
        }),
        "parse_error_count": parse_errors,
    }


def _trace_provider_rejection_status(path: Path) -> Optional[int]:
    """Return a provider HTTP rejection code proven by terminal trace events."""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") not in {"error", "turn.failed"}:
            continue
        error = event.get("error")
        error = error if isinstance(error, dict) else {}
        status = error.get("status_code", error.get("status"))
        try:
            if int(status) == 429:
                return 429
        except (TypeError, ValueError):
            pass
        message = " ".join(
            str(value)
            for value in (event.get("message"), error.get("message"))
            if value is not None
        )
        if re.search(r"\b429\s+Too\s+Many\s+Requests\b", message, re.IGNORECASE):
            return 429
        if re.search(r"\bHTTP\s+429\b", message, re.IGNORECASE):
            return 429
    return None


def _collect_attempt_traces(
    run_dir: Path, attempts: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Align request evidence; a final-only response stays explicitly incomplete."""
    run_dir = Path(run_dir).resolve()
    trace_dir = run_dir / "traces"
    rows: List[Dict[str, Any]] = []
    evidence_paths: List[str] = []
    for attempt in attempts:
        request_number = int(attempt.get("request_number", 0) or 0)
        if request_number <= 0:
            raise RuntimeError("attempt_request_number_missing")
        trace_path = trace_dir / "r4_step_{0:04d}.jsonl".format(request_number)
        if trace_path.is_file():
            row = _trace_usage(trace_path)
            row["request_number"] = request_number
            if (
                int(row.get("turn_started_count", 0) or 0) > 0
                and int(row.get("turn_completed_usage_count", 0) or 0) == 0
            ):
                rejection_status = _trace_provider_rejection_status(trace_path)
                if rejection_status is not None:
                    row.update({
                        "delivery_and_usage_unknown": False,
                        "provider_rejection_confirmed": True,
                        "provider_rejection_status_code": rejection_status,
                        "delivery_unknown": False,
                        "delivery_status": "provider_rejected_without_model_response",
                        "usage_unknown": True,
                        "turn_started_status": "provider_rejected_without_completion",
                        "input_tokens": None,
                        "cached_input_tokens": None,
                        "output_tokens": None,
                        "usage_accounting_basis": "provider_rejection_usage_unknown",
                    })
                else:
                    row.update({
                        "delivery_and_usage_unknown": True,
                        "delivery_unknown": True,
                        "usage_unknown": True,
                        "turn_started_status": "started_without_completion",
                        "input_tokens": None,
                        "cached_input_tokens": None,
                        "output_tokens": None,
                        "tool_item_types": None,
                        "usage_accounting_basis": "turn_started_without_turn_completed",
                    })
            if (
                int(row.get("turn_started_count", 0) or 0) == 0
                and int(row.get("turn_completed_usage_count", 0) or 0) == 0
            ):
                provider = attempt.get("provider_record", {})
                artifact_paths = []
                for key, hash_key in (("prompt_path", "prompt_sha256"), ("schema_path", "schema_sha256")):
                    artifact_path = Path(provider.get(key, ""))
                    if not artifact_path.is_absolute():
                        artifact_path = run_dir / artifact_path
                    if not artifact_path.is_file():
                        raise RuntimeError("no_turn_invocation_artifact_missing")
                    if not _artifact_hash_matches(
                        artifact_path,
                        provider.get(hash_key),
                        allow_terminal_newline=(key == "schema_path"),
                    ):
                        raise RuntimeError("no_turn_invocation_artifact_hash_mismatch")
                    artifact_paths.append(str(artifact_path.resolve().relative_to(run_dir)))
                row.update({
                    "delivery_and_usage_unknown": True,
                    "turn_started_status": "not_observed",
                    "input_tokens": None,
                    "cached_input_tokens": None,
                    "output_tokens": None,
                    "tool_item_types": None,
                    "usage_accounting_basis": "delivery_and_usage_unknown_no_model_turn_observed",
                })
                evidence_paths.append(" + ".join([str(trace_path.relative_to(run_dir))] + artifact_paths))
                rows.append(row)
                continue
            rows.append(row)
            evidence_paths.append(str(trace_path.relative_to(run_dir)))
            continue

        provider = attempt.get("provider_record", {})
        if provider.get("delivery_and_usage_unknown_evidence"):
            artifact_paths = []
            for key, hash_key in (("prompt_path", "prompt_sha256"), ("schema_path", "schema_sha256")):
                artifact_path = Path(provider.get(key, ""))
                if not artifact_path.is_absolute():
                    artifact_path = run_dir / artifact_path
                if not artifact_path.is_file():
                    raise RuntimeError("delivery_unknown_request_artifact_missing")
                if not _artifact_hash_matches(
                    artifact_path,
                    provider.get(hash_key),
                    allow_terminal_newline=(key == "schema_path"),
                ):
                    raise RuntimeError("delivery_unknown_request_artifact_hash_mismatch")
                artifact_paths.append(str(artifact_path.resolve().relative_to(run_dir)))
            rows.append({
                "request_number": request_number,
                "sha256": None,
                "trace_available": False,
                "delivery_and_usage_unknown": True,
                "turn_started_count": 0,
                "turn_started_status": "unknown",
                "turn_completed_usage_count": 0,
                "input_tokens": None,
                "cached_input_tokens": None,
                "output_tokens": None,
                "tool_item_types": None,
                "parse_error_count": None,
                "usage_accounting_basis": "delivery_and_usage_unknown_after_interruption",
            })
            evidence_paths.append(" + ".join(artifact_paths))
            continue
        if not provider.get("partial_final_response_evidence"):
            raise RuntimeError("attempt_trace_missing_without_partial_response_evidence")
        final_path = Path(provider.get("final_json_path", ""))
        if not final_path.is_absolute():
            final_path = run_dir / final_path
        if not final_path.is_file():
            raise RuntimeError("partial_final_response_file_missing")
        digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
        if digest != provider.get("final_json_sha256"):
            raise RuntimeError("partial_final_response_hash_mismatch")
        try:
            final_payload = json.loads(final_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("partial_final_response_invalid_json") from error
        if final_payload != attempt.get("response"):
            raise RuntimeError("partial_final_response_attempt_mismatch")
        rows.append({
            "request_number": request_number,
            "sha256": None,
            "trace_available": False,
            "partial_final_response_evidence": True,
            "final_json_sha256": digest,
            "turn_started_count": 1,
            "turn_completed_usage_count": 0,
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "tool_item_types": None,
            "parse_error_count": None,
            "usage_accounting_basis": "missing_cli_jsonl_after_partial_response_write",
        })
        evidence_paths.append(str(final_path.resolve().relative_to(run_dir)))
    return rows, evidence_paths


def _collect_trace_outcome_request_numbers(
    traces: List[Dict[str, Any]],
) -> Dict[str, List[int]]:
    """Keep provider rejection, delivery uncertainty, and usage uncertainty distinct."""
    provider_rejected = []
    delivery_unknown = []
    usage_unknown = []
    for row in traces:
        request_number = row.get("request_number")
        if row.get("provider_rejection_confirmed"):
            provider_rejected.append(request_number)
        if row.get("delivery_and_usage_unknown") or row.get("delivery_unknown"):
            delivery_unknown.append(request_number)
        if (
            row.get("usage_unknown")
            or row.get("delivery_and_usage_unknown")
            or row.get("turn_started_status") == "unknown"
            or (
                int(row.get("turn_started_count", 0) or 0) > 0
                and (
                    not isinstance(row.get("input_tokens"), int)
                    or not isinstance(row.get("output_tokens"), int)
                )
            )
        ):
            usage_unknown.append(request_number)
    return {
        "provider_rejected_request_numbers": provider_rejected,
        "delivery_unknown_request_numbers": delivery_unknown,
        "usage_unknown_request_numbers": usage_unknown,
    }


def _reconcile_trace_usage(
    traces: List[Dict[str, Any]], attempts: List[Dict[str, Any]], persistent_session: bool
) -> List[Dict[str, Any]]:
    """Use per-invocation deltas for resumed Codex sessions; retain raw totals."""
    reconciled = []
    previous_by_session: Dict[str, Dict[str, Any]] = {}
    for index, trace in enumerate(traces):
        row = dict(trace)
        if row.get("trace_available") is False:
            row["usage_accounting_basis"] = "usage_missing_raw_cli_trace_missing"
            reconciled.append(row)
            continue
        if not persistent_session:
            row["usage_accounting_basis"] = "reported_per_invocation"
            reconciled.append(row)
            continue
        attempt = attempts[index] if index < len(attempts) else {}
        provider = attempt.get("provider_record", {})
        session_id = (
            attempt.get("codex_session_id")
            or provider.get("session_id")
            or "persistent_session_unknown_id"
        )
        raw = {
            "input_tokens": row.get("input_tokens"),
            "cached_input_tokens": row.get("cached_input_tokens"),
            "output_tokens": row.get("output_tokens"),
        }
        if int(row.get("turn_completed_usage_count", 0)) > 0:
            normalized = normalize_session_cumulative_usage(
                raw, previous_by_session.get(session_id, {})
            )
            previous_by_session[session_id] = raw
            row["reported_session_cumulative_usage"] = raw
            row["input_tokens"] = normalized["input_tokens"]
            row["cached_input_tokens"] = normalized["cached_input_tokens"]
            row["output_tokens"] = normalized["output_tokens"]
            row["usage_accounting_basis"] = normalized["accounting_basis"]
        else:
            row["usage_accounting_basis"] = "usage_missing"
        reconciled.append(row)
    return reconciled


def _collect_self_check_discrepancies(attempts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    discrepancies = []
    for attempt in attempts:
        validation = attempt.get("validation")
        if not isinstance(validation, dict):
            validation = {}
        local = validation.get("deterministic_local_check")
        if not isinstance(local, dict):
            local = {}
        model_self_check = (attempt.get("response") or {}).get("self_check", {})
        if not isinstance(model_self_check, dict) or not isinstance(local.get("flags"), dict):
            continue
        normalized = dict(model_self_check)
        if "target_node_is_new" not in normalized and "not_revisited" in normalized:
            normalized["target_node_is_new"] = normalized["not_revisited"]
        mismatch_fields = validation.get("model_self_check_mismatch_fields")
        if not isinstance(mismatch_fields, list):
            mismatch_fields = _model_self_check_mismatch_fields(normalized, local["flags"])
        if mismatch_fields:
            discrepancies.append({
                "request_number": attempt.get("request_number"),
                "status": attempt.get("status"),
                "model_self_check_mismatch_fields": sorted(mismatch_fields),
                "model_self_check": model_self_check,
                "deterministic_local_check_passed": bool(local.get("passed")),
                "action_executed": attempt.get("status") in {"accepted_legal_step", "route_completed"},
            })
    return discrepancies


def _collect_model_failure_code_overrides(attempts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    overrides = []
    for attempt in attempts:
        validation = attempt.get("validation")
        if not isinstance(validation, dict):
            validation = {}
        local = validation.get("deterministic_local_check")
        if not isinstance(local, dict):
            local = {}
        failure_code = (attempt.get("response") or {}).get("failure_reason_code")
        if (
            validation.get("model_failure_code_overridden_by_local_validation")
            and failure_code not in (None, "none")
            and local.get("passed") is True
        ):
            overrides.append({
                "request_number": attempt.get("request_number"),
                "failure_reason_code": failure_code,
                "deterministic_local_check_passed": True,
                "action_executed": attempt.get("status") in {"accepted_legal_step", "route_completed"},
            })
    return overrides


def _validate_attempt_trace_alignment(
    attempts: List[Dict[str, Any]], traces: List[Dict[str, Any]], new_requests_used: int
) -> Tuple[int, int]:
    """Separate CLI invocations from calls that actually started a model turn."""
    if len(attempts) != len(traces):
        raise RuntimeError("attempt_trace_request_count_mismatch")
    cli_invocations = sum(
        attempt.get("provider_record", {}).get("status") != "not_called"
        for attempt in attempts
    )
    actual_model_requests = sum(int(row.get("turn_started_count", 0)) > 0 for row in traces)
    if cli_invocations != int(new_requests_used) or actual_model_requests > cli_invocations:
        raise RuntimeError("attempt_trace_request_count_mismatch")
    return actual_model_requests, cli_invocations


def finalize(run_dir: Path, parent_run: Optional[Path], model: str) -> Dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    state = read_json(run_dir / "state.json")
    manifest = read_json(run_dir / "manifest.json")
    source_preflight = Path(state.get("source_preflight", PREFLIGHT))
    if not source_preflight.is_absolute():
        source_preflight = ROOT / source_preflight
    road_map = load_map(source_preflight)

    parent_audit: Dict[str, Any] = {}
    parent_cost: Dict[str, Any] = {}
    if parent_run:
        parent_run = Path(parent_run).resolve()
        parent_audit = read_json(parent_run / "final_audit.json")
        parent_cost_path = parent_run / "codex_usage_cost_reconciliation.json"
        if parent_cost_path.is_file():
            parent_cost = read_json(parent_cost_path)

    bootstrap_result = read_json(ROOT / "outputs/route_layer/r4_codex_smoke_v5/result.json")
    bootstrap_usage = bootstrap_result.get("usage", {})
    bootstrap_cost = estimate_codex_usage_cost(
        model,
        bootstrap_usage.get("input_tokens"),
        bootstrap_usage.get("cached_input_tokens"),
        bootstrap_usage.get("output_tokens"),
    )
    bootstrap_included = bool(
        state.get("prior_smoke_result") or state.get("prior_action_reconciliation")
    )

    attempts = state.get("attempts", [])
    traces, trace_evidence_paths = _collect_attempt_traces(run_dir, attempts)
    traces = _reconcile_trace_usage(
        traces,
        attempts,
        state.get("codex_session_mode") == "persistent_per_task",
    )
    actual_requests, cli_invocations = _validate_attempt_trace_alignment(
        attempts, traces, state.get("new_requests_used", 0)
    )
    with_usage = sum(int(row["turn_completed_usage_count"]) > 0 for row in traces)
    model_turn_traces = [row for row in traces if int(row.get("turn_started_count", 0)) > 0]
    has_unknown_delivery_or_usage = any(
        row.get("delivery_and_usage_unknown")
        or row.get("turn_started_status") == "unknown"
        for row in traces
    )
    input_complete = bool(model_turn_traces) and all(
        isinstance(row.get("input_tokens"), int) for row in model_turn_traces
    ) and not has_unknown_delivery_or_usage
    output_complete = bool(model_turn_traces) and all(
        isinstance(row.get("output_tokens"), int) for row in model_turn_traces
    ) and not has_unknown_delivery_or_usage
    cached_complete = bool(model_turn_traces) and all(
        isinstance(row.get("cached_input_tokens"), int) for row in model_turn_traces
    ) and not has_unknown_delivery_or_usage
    input_tokens = sum(int(row["input_tokens"]) for row in traces if isinstance(row.get("input_tokens"), int))
    output_tokens = sum(int(row["output_tokens"]) for row in traces if isinstance(row.get("output_tokens"), int))
    cached_tokens = sum(int(row["cached_input_tokens"]) for row in traces if isinstance(row.get("cached_input_tokens"), int))
    usage_costs = [
        estimate_codex_usage_cost(model, row["input_tokens"], row["cached_input_tokens"], row["output_tokens"])
        for row in traces
        if isinstance(row.get("input_tokens"), int) and isinstance(row.get("output_tokens"), int)
    ]
    credits = sum(float(item["codex_standard_credit_equivalent"]) for item in usage_costs)
    api_usd = sum(float(item["api_equivalent_usd"]) for item in usage_costs)

    replay = replay_directed_edge_sequence(
        road_map,
        state["start_node_id"],
        state["accepted_edges"],
        state["accepted_nodes"],
    )
    if replay.get("move_violations"):
        raise RuntimeError("accepted_prefix_replay_has_violations")
    trace_outcomes = _collect_trace_outcome_request_numbers(traces)
    provider_rejected_request_numbers = trace_outcomes["provider_rejected_request_numbers"]
    provider_rejected_set = set(provider_rejected_request_numbers)
    delivery_unknown_request_numbers = trace_outcomes["delivery_unknown_request_numbers"]
    delivery_unknown_set = set(delivery_unknown_request_numbers)
    accepted_action_statuses = {"accepted_legal_step", "route_completed"}
    last_validation = next((
        attempt.get("validation", {}).get("deterministic_local_check", {})
        for attempt in reversed(attempts)
        if attempt.get("status") in accepted_action_statuses
        and attempt.get("validation", {}).get("deterministic_local_check")
    ), {})
    legal_attempts = sum(attempt.get("status") in accepted_action_statuses for attempt in attempts)
    invalid_attempts = [
        {
            "request_number": attempt.get("request_number"),
            "status": attempt.get("status"),
            "proposed_direction": (attempt.get("response") or {}).get("direction"),
            "proposed_edge_id": attempt.get("allowed_edge_ids", {}).get(
                (attempt.get("response") or {}).get("direction")
            ),
            "proposed_target_node_id": attempt.get("validation", {})
            .get("deterministic_local_check", {})
            .get("expected_target_node_id"),
            "model_self_check_false_fields": sorted(
                key for key, value in (attempt.get("response") or {}).get("self_check", {}).items()
                if value is False
            ),
            "deterministic_local_check_passed": bool(
                attempt.get("validation", {}).get("deterministic_local_check", {}).get("passed")
            ),
            "action_executed": attempt.get("status") in accepted_action_statuses,
            "vehicle_energy_consumed_kwh": (
                0.0 if attempt.get("status") not in accepted_action_statuses else None
            ),
            "vehicle_energy_status": (
                "not_executed_after_validation_stop"
                if attempt.get("status") not in accepted_action_statuses
                else "executed"
            ),
            "errors": attempt.get("validation", {}).get("deterministic_local_check", {}).get("errors", []),
        }
        for attempt in attempts
        if attempt.get("status") not in accepted_action_statuses
        and attempt.get("request_number") not in delivery_unknown_set
        and attempt.get("request_number") not in provider_rejected_set
        and not attempt.get("provider_record", {}).get("delivery_and_usage_unknown_evidence")
    ]
    self_check_discrepancies = _collect_self_check_discrepancies(attempts)
    model_failure_code_overrides = _collect_model_failure_code_overrides(attempts)
    usage_unknown_request_numbers = trace_outcomes["usage_unknown_request_numbers"]
    cli_invocations_without_model_turn = max(
        0, cli_invocations - actual_requests
    )
    unresolved_cli_invocation = None
    if delivery_unknown_request_numbers:
        unresolved_request_number = delivery_unknown_request_numbers[-1]
        unresolved_attempt = next(
            item for item in attempts
            if item.get("request_number") == unresolved_request_number
        )
        unresolved_provider = unresolved_attempt.get("provider_record", {})
        exit_code = unresolved_provider.get("cli_exit_code")
        runner_returncode = unresolved_provider.get("runner_process_returncode")
        unresolved_trace = next(
            row for row in traces
            if row.get("request_number") == unresolved_request_number
        )
        turn_started = int(unresolved_trace.get("turn_started_count", 0) or 0) > 0
        unresolved_cli_invocation = {
            "request_number": unresolved_request_number,
            "status": (
                "model_turn_started_without_completion"
                if turn_started else "delivery_and_usage_unknown"
            ),
            "attempt_status": unresolved_attempt.get("status"),
            "error": unresolved_attempt.get("error"),
            "cli_exit_code": exit_code,
            "runner_process_returncode": runner_returncode,
            "process_signal": "SIGTERM" if exit_code == -15 or runner_returncode == -15 else None,
            "model_turn_observed": turn_started,
            "delivery_status": (
                "model_turn_started_but_response_completion_unknown"
                if turn_started else "unknown"
            ),
            "usage_status": "unknown",
            "tool_audit_status": "unknown",
            "action_executed": False,
            "vehicle_energy_consumed_kwh": 0.0,
            "energy_status": "no_model_direction_or_edge_was_returned; no_vehicle_move_executed",
            "retry_count": int(unresolved_attempt.get("retry_count", 0) or 0),
        }
    tool_audit_complete = all(row.get("tool_item_types") is not None for row in traces)
    tool_events_reported = sum(bool(row.get("tool_item_types")) for row in traces)
    tool_events = tool_events_reported if tool_audit_complete else None
    parent_requests_value = parent_audit.get(
        "actual_model_requests_this_continuation", parent_audit.get("actual_model_requests")
    )
    parent_input_value = parent_audit.get(
        "input_tokens_this_continuation", parent_audit.get("input_tokens")
    )
    parent_output_value = parent_audit.get(
        "output_tokens_this_continuation", parent_audit.get("output_tokens")
    )
    parent_requests = int(parent_requests_value or 0)
    parent_input = int(parent_input_value or 0)
    parent_output = int(parent_output_value or 0)
    parent_cost_totals = parent_cost.get("totals", {})
    parent_credits = float(parent_audit.get(
        "codex_standard_credit_equivalent_this_continuation",
        parent_audit.get(
            "codex_standard_credit_equivalent",
            parent_cost_totals.get("codex_standard_credit_equivalent", 0),
        ),
    ) or 0)
    parent_api_usd = float(parent_audit.get(
        "api_equivalent_usd_this_continuation",
        parent_audit.get("api_equivalent_usd", parent_cost_totals.get("api_equivalent_usd", 0)),
    ) or 0)
    parent_cumulative_requests = parent_audit.get(
        "cumulative_requests_including_bootstrap_and_parent"
    )
    parent_cumulative_cli_invocations = parent_audit.get(
        "cumulative_cli_invocations_including_bootstrap_and_parent"
    )
    parent_cumulative_input = parent_audit.get(
        "cumulative_input_tokens_including_bootstrap_and_parent"
    )
    parent_cumulative_output = parent_audit.get(
        "cumulative_output_tokens_including_bootstrap_and_parent"
    )
    parent_cumulative_credits = parent_audit.get(
        "cumulative_codex_standard_credit_equivalent_including_bootstrap_and_parent"
    )
    parent_cumulative_api_usd = parent_audit.get(
        "cumulative_api_equivalent_usd_including_bootstrap_and_parent"
    )
    cumulative_requests = (
        int(parent_cumulative_requests) + actual_requests
        if parent_cumulative_requests is not None
        else int(bootstrap_included) + parent_requests + actual_requests
    )
    if parent_cumulative_cli_invocations is not None:
        cumulative_cli_invocations = int(parent_cumulative_cli_invocations) + cli_invocations
    elif parent_run:
        prior_unknown_cli_invocations = max(
            0,
            int(parent_audit.get("codex_cli_invocations_this_continuation", parent_requests) or 0)
            - int(parent_audit.get("actual_model_requests_this_continuation", parent_requests) or 0),
        )
        cumulative_cli_invocations = (
            int(parent_cumulative_requests or 0)
            + prior_unknown_cli_invocations
            + cli_invocations
        )
    else:
        cumulative_cli_invocations = int(bootstrap_included) + cli_invocations
    cumulative_input = None
    if input_complete:
        bootstrap_input = (
            int(bootstrap_usage["input_tokens"])
            if bootstrap_included and isinstance(bootstrap_usage.get("input_tokens"), int)
            else 0
        )
        cumulative_input = _combine_cumulative_usage(
            input_tokens,
            parent_audit,
            "cumulative_input_tokens_including_bootstrap_and_parent",
            has_parent=bool(parent_run),
            fallback_prior=bootstrap_input + (parent_input if parent_run else 0),
        )
    cumulative_output = None
    if output_complete:
        bootstrap_output = (
            int(bootstrap_usage["output_tokens"])
            if bootstrap_included and isinstance(bootstrap_usage.get("output_tokens"), int)
            else 0
        )
        cumulative_output = _combine_cumulative_usage(
            output_tokens,
            parent_audit,
            "cumulative_output_tokens_including_bootstrap_and_parent",
            has_parent=bool(parent_run),
            fallback_prior=bootstrap_output + (parent_output if parent_run else 0),
        )
    usage_costs_complete = (
        len(usage_costs) == actual_requests
        and bool(usage_costs)
        and not has_unknown_delivery_or_usage
    )
    cumulative_credits = None
    cumulative_api_usd = None
    if usage_costs_complete:
        cumulative_credits = _combine_cumulative_usage(
            credits,
            parent_audit,
            "cumulative_codex_standard_credit_equivalent_including_bootstrap_and_parent",
            has_parent=bool(parent_run),
            fallback_prior=(
                float(bootstrap_cost.get("codex_standard_credit_equivalent", 0) or 0)
                if bootstrap_included else 0.0
            ) + parent_credits,
        )
        cumulative_api_usd = _combine_cumulative_usage(
            api_usd,
            parent_audit,
            "cumulative_api_equivalent_usd_including_bootstrap_and_parent",
            has_parent=bool(parent_run),
            fallback_prior=(
                float(bootstrap_cost.get("api_equivalent_usd", 0) or 0)
                if bootstrap_included else 0.0
            ) + parent_api_usd,
        )
    status = "completed_route" if state.get("status") == "route_complete" else "partial_legal_prefix_not_complete"
    manifest_preflight_status = manifest.get("preflight_status", manifest.get("status"))
    manifest.update({
        "preflight_status": manifest_preflight_status,
        "status": status,
        "stop_status": state.get("status"),
        "actual_model_requests": actual_requests,
        "codex_cli_invocations": cli_invocations,
        "provider_rejected_request_count": len(provider_rejected_request_numbers),
        "provider_rejected_request_numbers": provider_rejected_request_numbers,
        "delivery_unknown_request_count": len(delivery_unknown_request_numbers),
        "delivery_unknown_request_numbers": delivery_unknown_request_numbers,
        "usage_unknown_request_numbers": usage_unknown_request_numbers,
        "unresolved_cli_invocation": unresolved_cli_invocation,
        "authorized_request_limit": state.get(
            "effective_authorized_new_request_limit", state.get("authorized_new_request_limit")
        ),
        "candidate_request_limit": state.get("authorized_new_request_limit"),
        "current_authorization_status": state.get("current_authorization_status"),
        "additional_model_requests_authorized": state.get("additional_model_requests_authorized"),
        "accepted_legal_actions": legal_attempts,
        "model_self_check_discrepancy_count": len(self_check_discrepancies),
        "model_failure_code_override_count": len(model_failure_code_overrides),
        "retries": int(state.get("retry_count", 0) or 0),
        "tool_events": tool_events,
        "tool_events_reported_in_available_traces": tool_events_reported,
        "tool_audit_complete": tool_audit_complete,
        "tool_audit_unknown_request_numbers": [
            row.get("request_number") for row in traces
            if row.get("tool_item_types") is None
        ],
        "final_audit_path": "final_audit.json",
        "cost_reconciliation_path": "codex_usage_cost_reconciliation.json",
    })
    candidate = RouteCandidate(
        route_id="codex-independent-pilot-" + str(state.get("task_id")),
        node_sequence=list(state.get("accepted_nodes", [])),
        edge_sequence=list(state.get("accepted_edges", [])),
        route_source="codex_cli_independent_pilot",
        route_reachability=True,
        validation_status="valid" if status == "completed_route" else "partial_legal_prefix",
    )
    if candidate.edge_sequence:
        evaluation = evaluate_route_candidate(road_map, candidate)
        route_is_complete = status == "completed_route"
        prefix = {
            "accepted_prefix_distance_m": evaluation.route_distance_m,
            "accepted_prefix_duration_sec": evaluation.total_travel_time_sec,
            "accepted_prefix_energy_kwh": evaluation.energy_total_kwh,
            "energy_model_version": evaluation.energy_model_version,
            "energy_basis": (
                "shared simulated route energy model; completed validated route"
                if route_is_complete
                else "shared simulated route energy model; legal accepted prefix only"
            ),
            **(
                {
                    "completed_route_distance_m": evaluation.route_distance_m,
                    "completed_route_duration_sec": evaluation.total_travel_time_sec,
                    "completed_route_energy_kwh": evaluation.energy_total_kwh,
                }
                if route_is_complete
                else {}
            ),
        }
    else:
        # An untouched start state has no traveled prefix to evaluate. Keep
        # its distance, duration, and energy unavailable instead of inventing
        # a zero-length/zero-energy route result.
        prefix = {
            "accepted_prefix_distance_m": None,
            "accepted_prefix_duration_sec": None,
            "accepted_prefix_energy_kwh": None,
            "energy_model_version": None,
            "energy_basis": "no accepted route edge; energy not estimated",
        }
    for diagnostic, attempt in zip(
        invalid_attempts,
        [row for row in attempts if row.get("status") != "accepted_legal_step"],
    ):
        target_node = diagnostic.get("proposed_target_node_id")
        current_node = attempt.get("current_node_id")
        direction = diagnostic.get("proposed_direction")
        if not target_node and isinstance(current_node, str):
            pieces = current_node.split("_")
            delta = _DIRECTION_DELTAS.get(direction)
            if len(pieces) == 3 and pieces[0] == "node" and delta:
                try:
                    x, y = int(pieces[1]), int(pieces[2])
                except ValueError:
                    pass
                else:
                    target_node = "node_{0}_{1}".format(x + delta[0], y + delta[1])
                    diagnostic["proposed_target_node_id"] = target_node
        if not current_node or not target_node or current_node == target_node:
            diagnostic["hypothetical_step_energy_kwh"] = None
            diagnostic["hypothetical_energy_status"] = "not_estimated_missing_transition"
            continue
        proposal_energy = estimate_route_proposal_energy(
            road_map,
            {
                "route_representation": "node_sequence",
                "attempted_route_node_sequence": [current_node, target_node],
                "attempted_route_edge_sequence": [],
                "validated_prefix_node_sequence": [current_node],
                "validated_prefix_edge_sequence": [],
            },
        )
        if proposal_energy.get("status") != "estimated":
            diagnostic["hypothetical_step_energy_kwh"] = None
            diagnostic["hypothetical_energy_status"] = "not_estimated_missing_transition"
            continue
        diagnostic["hypothetical_step_energy_kwh"] = round(
            float(proposal_energy["route_energy_estimate_kwh"]), 9
        )
        diagnostic["hypothetical_energy_status"] = "estimate_only_not_consumed"
        diagnostic["hypothetical_step_distance_m"] = round(
            float(proposal_energy["estimated_route_distance_m"]), 3
        )
        diagnostic["hypothetical_energy_basis"] = {
            "exact_edge_moves": proposal_energy["exact_edge_moves"],
            "reverse_edge_inferred_moves": proposal_energy["reverse_edge_inferred_moves"],
            "proxy_edge_moves": proposal_energy["proxy_edge_moves"],
            "fallback_length_m": proposal_energy["fallback_length_m"],
            "fallback_congestion_level": proposal_energy["fallback_congestion_level"],
            "fallback_signal_delay_sec": proposal_energy["fallback_signal_delay_sec"],
        }

    audit = {
        "schema_version": "r4_codex_pilot_final_audit_v3",
        "experiment_id": state.get("experiment_id"),
        "status": status,
        "stop_status": state.get("status"),
        "map_id": road_map.map_id,
        "map_nodes": len(road_map.nodes),
        "map_directed_edges": len(road_map.edges),
        "source_preflight": str(source_preflight.relative_to(ROOT)) if source_preflight.is_relative_to(ROOT) else str(source_preflight),
        "source_preflight_sha256": state.get("source_preflight_sha256"),
        "task_adjustment": state.get("task_adjustment"),
        "task_id": state.get("task_id"),
        "start_node": state.get("start_node_id"),
        "pickup_node": state.get("pickup_node_id"),
        "dropoff_node": state.get("dropoff_node_id"),
        "pickup_reached": bool(state.get("pickup_seen")),
        "dropoff_reached": state.get("accepted_nodes", [None])[-1] == state.get("dropoff_node_id"),
        "accepted_route_nodes": state.get("accepted_nodes"),
        "accepted_route_edges": state.get("accepted_edges"),
        "accepted_directions": state.get("accepted_directions"),
        "accepted_move_count_including_prior_smoke": len(state.get("accepted_edges", [])),
        "accepted_actions_this_run": legal_attempts,
        "bootstrap_route_action_included": bootstrap_included,
        "current_node": state.get("accepted_nodes", [None])[-1],
        "route_continuity_violations": replay.get("move_violations", []),
        "repeated_nodes": len(state.get("accepted_nodes", [])) != len(set(state.get("accepted_nodes", []))),
        "accepted_prefix_distance_m": prefix.get("accepted_prefix_distance_m"),
        "accepted_prefix_duration_sec": prefix.get("accepted_prefix_duration_sec"),
        "accepted_prefix_energy_kwh": prefix.get("accepted_prefix_energy_kwh"),
        "energy_model_version": prefix.get("energy_model_version"),
        "energy_basis": prefix.get("energy_basis"),
        "completed_route_distance_m": prefix.get("completed_route_distance_m"),
        "completed_route_duration_sec": prefix.get("completed_route_duration_sec"),
        "completed_route_energy_kwh": prefix.get("completed_route_energy_kwh"),
        "minimum_remaining_moves_at_stop": last_validation.get("remaining_minimum_moves_after_move"),
        "stop_diagnostics": state.get("stop_diagnostics"),
        "actual_model_requests_this_continuation": actual_requests,
        "codex_cli_invocations_this_continuation": cli_invocations,
        "cli_invocations_without_model_turn": cli_invocations_without_model_turn,
        "cli_invocations_with_delivery_unknown_count": len(delivery_unknown_request_numbers),
        "provider_rejected_request_count": len(provider_rejected_request_numbers),
        "provider_rejected_request_numbers": provider_rejected_request_numbers,
        "delivery_unknown_request_numbers": delivery_unknown_request_numbers,
        "usage_unknown_request_numbers": usage_unknown_request_numbers,
        "unresolved_cli_invocation": unresolved_cli_invocation,
        "pre_request_failure_count": len(state.get("pre_request_failures", [])),
        "pre_request_failures": state.get("pre_request_failures", []),
        "authorized_new_request_limit": state.get(
            "effective_authorized_new_request_limit", state.get("authorized_new_request_limit")
        ),
        "candidate_request_limit": state.get("authorized_new_request_limit"),
        "current_authorization_status": state.get("current_authorization_status"),
        "additional_model_requests_authorized": state.get("additional_model_requests_authorized"),
        "account_usage_guard": state.get("account_usage_guard"),
        "parent_account_usage_guard": state.get("parent_account_usage_guard"),
        "accepted_legal_actions_this_continuation": legal_attempts,
        "invalid_or_stopped_actions": invalid_attempts,
        "model_self_check_discrepancy_count": len(self_check_discrepancies),
        "model_self_check_discrepancies": self_check_discrepancies,
        "model_failure_code_override_count": len(model_failure_code_overrides),
        "model_failure_code_overrides": model_failure_code_overrides,
        "retries": int(state.get("retry_count", 0) or 0),
        "tool_events": tool_events,
        "tool_audit_complete": tool_audit_complete,
        "tool_audit_unknown_request_numbers": [
            row.get("request_number") for row in traces
            if row.get("tool_item_types") is None
        ],
        "input_tokens_this_continuation": input_tokens if input_complete else None,
        "known_input_tokens_sum_this_continuation": input_tokens,
        "usage_accounting_basis": (
            "persistent_session_cumulative_delta"
            if state.get("codex_session_mode") == "persistent_per_task"
            else "reported_per_invocation"
        ),
        "cached_input_tokens_this_continuation": cached_tokens if cached_complete else None,
        "known_cached_input_tokens_sum_this_continuation": cached_tokens,
        "output_tokens_this_continuation": output_tokens if output_complete else None,
        "known_output_tokens_sum_this_continuation": output_tokens,
        "known_total_tokens_this_continuation": input_tokens + output_tokens if with_usage else None,
        "complete_total_tokens_this_continuation": input_tokens + output_tokens if input_complete and output_complete and with_usage == actual_requests else None,
        "codex_standard_credit_equivalent_this_continuation": credits if usage_costs_complete else None,
        "api_equivalent_usd_this_continuation": api_usd if usage_costs_complete else None,
        "known_codex_standard_credit_equivalent_this_continuation": credits,
        "known_api_equivalent_usd_this_continuation": api_usd,
        "account_cash_cost": None,
        "account_cash_cost_status": "not_reported_by_codex_cli; rate-card equivalents are not invoice amounts",
        "cumulative_requests_including_bootstrap_and_parent": cumulative_requests,
        "cumulative_cli_invocations_including_bootstrap_and_parent": cumulative_cli_invocations,
        "bootstrap_smoke_tokens": {
            "input_tokens": bootstrap_usage.get("input_tokens"),
            "cached_input_tokens": bootstrap_usage.get("cached_input_tokens"),
            "output_tokens": bootstrap_usage.get("output_tokens"),
            "included_in_cumulative": bootstrap_included,
        },
        "cumulative_input_tokens_including_bootstrap_and_parent": cumulative_input,
        "cumulative_output_tokens_including_bootstrap_and_parent": cumulative_output,
        "cumulative_codex_standard_credit_equivalent_including_bootstrap_and_parent": cumulative_credits,
        "cumulative_api_equivalent_usd_including_bootstrap_and_parent": cumulative_api_usd,
        "parent_run": str(parent_run.relative_to(ROOT)) if parent_run and parent_run.is_relative_to(ROOT) else (str(parent_run) if parent_run else None),
        "pricing_model_assumption": model,
        "pricing_config": "configs/codex_cli_pricing_2026-10-22.json",
        "pricing_basis": "official GPT-5.6 Luna Standard credit and API token rates; cached tokens are a subset of input tokens",
        "trace_count": sum(bool(row.get("trace_available")) for row in traces),
        "trace_evidence": [
            {"path": path, **row}
            for path, row in zip(trace_evidence_paths, traces)
        ],
        "manifest_status": manifest.get("status"),
        "manifest_preflight_status": manifest_preflight_status,
    }
    (run_dir / "final_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    reconciliation = {
        "schema_version": "codex_cli_run_cost_reconciliation_v1",
        "experiment_id": state.get("experiment_id"),
        "model_for_rate_card_assumption": model,
        "usage_accounting_basis": (
            "persistent_session_cumulative_delta"
            if state.get("codex_session_mode") == "persistent_per_task"
            else "reported_per_invocation"
        ),
        "pricing_config": "configs/codex_cli_pricing_2026-10-22.json",
        "requests": [
            {"trace_evidence_path": path, **row,
             "cost_estimate": estimate_codex_usage_cost(
                 model, row.get("input_tokens"), row.get("cached_input_tokens"), row.get("output_tokens")
             ) if isinstance(row.get("input_tokens"), int) and isinstance(row.get("output_tokens"), int) else None}
            for path, row in zip(trace_evidence_paths, traces)
        ],
        "totals": {
            "request_count": actual_requests,
            "input_tokens": input_tokens if input_complete else None,
            "known_input_tokens_sum": input_tokens,
            "cached_input_tokens": cached_tokens if cached_complete else None,
            "known_cached_input_tokens_sum": cached_tokens,
            "output_tokens": output_tokens if output_complete else None,
            "known_output_tokens_sum": output_tokens,
            "known_total_tokens": input_tokens + output_tokens if input_complete and output_complete else None,
            "codex_standard_credit_equivalent": credits if usage_costs_complete else None,
            "api_equivalent_usd": api_usd if usage_costs_complete else None,
            "known_codex_standard_credit_equivalent": credits,
            "known_api_equivalent_usd": api_usd,
            "account_cash_cost": None,
        },
    }
    (run_dir / "codex_usage_cost_reconciliation.json").write_text(
        json.dumps(reconciliation, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    render_pilot_replay(run_dir, run_dir / "replay.html", parent_run)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--parent-run", type=Path, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    audit = finalize(args.run_dir, args.parent_run, args.model)
    print(json.dumps({key: value for key, value in audit.items() if key != "trace_evidence"}, ensure_ascii=False, indent=2))
    print("final_audit_path=" + str(args.run_dir / "final_audit.json"))


if __name__ == "__main__":
    main()
