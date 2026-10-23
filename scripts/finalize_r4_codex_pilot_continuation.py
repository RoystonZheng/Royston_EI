#!/usr/bin/env python3
"""Finalize an offline audit and rate-card estimate for a Codex R4 pilot run."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_cli_support import estimate_codex_usage_cost  # noqa: E402
from route_experiment.costs import evaluate_route_candidate  # noqa: E402
from route_experiment.models import Edge, Node, RoadMap, RoadsideFeature, RouteCandidate  # noqa: E402
from route_experiment.r4_agent import replay_directed_edge_sequence  # noqa: E402
from route_experiment.r4_stepwise import _model_self_check_mismatch_fields  # noqa: E402


PREFLIGHT = ROOT / "outputs/route_layer/r4_codex_offline_preflight_20261021/seed0_semantic_road_feature.json"
DEFAULT_MODEL = "gpt-5.6-luna"


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_map(preflight_path: Optional[Path] = None) -> RoadMap:
    map_data = read_json(preflight_path or PREFLIGHT)["map"]
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


def _collect_self_check_discrepancies(attempts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    discrepancies = []
    for attempt in attempts:
        validation = attempt.get("validation", {})
        local = validation.get("deterministic_local_check", {})
        model_self_check = attempt.get("response", {}).get("self_check", {})
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

    trace_paths = sorted((run_dir / "traces").glob("r4_step_*.jsonl"))
    traces = [_trace_usage(path) for path in trace_paths]
    attempts = state.get("attempts", [])
    actual_requests, cli_invocations = _validate_attempt_trace_alignment(
        attempts, traces, state.get("new_requests_used", 0)
    )
    with_usage = sum(int(row["turn_completed_usage_count"]) > 0 for row in traces)
    model_turn_traces = [row for row in traces if int(row.get("turn_started_count", 0)) > 0]
    input_complete = bool(model_turn_traces) and all(
        isinstance(row.get("input_tokens"), int) for row in model_turn_traces
    )
    output_complete = bool(model_turn_traces) and all(
        isinstance(row.get("output_tokens"), int) for row in model_turn_traces
    )
    cached_complete = bool(model_turn_traces) and all(
        isinstance(row.get("cached_input_tokens"), int) for row in model_turn_traces
    )
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
            "proposed_direction": attempt.get("response", {}).get("direction"),
            "proposed_edge_id": attempt.get("allowed_edge_ids", {}).get(
                attempt.get("response", {}).get("direction")
            ),
            "proposed_target_node_id": attempt.get("validation", {})
            .get("deterministic_local_check", {})
            .get("expected_target_node_id"),
            "model_self_check_false_fields": sorted(
                key for key, value in attempt.get("response", {}).get("self_check", {}).items()
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
    ]
    self_check_discrepancies = _collect_self_check_discrepancies(attempts)
    tool_events = sum(bool(row.get("tool_item_types")) for row in traces)
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
    cumulative_input = None
    if input_complete:
        if isinstance(parent_cumulative_input, int):
            cumulative_input = int(parent_cumulative_input) + input_tokens
        else:
            bootstrap_input = (
                int(bootstrap_usage["input_tokens"])
                if bootstrap_included and isinstance(bootstrap_usage.get("input_tokens"), int)
                else 0
            )
            cumulative_input = bootstrap_input + (parent_input if parent_run else 0) + input_tokens
    cumulative_output = None
    if output_complete:
        if isinstance(parent_cumulative_output, int):
            cumulative_output = int(parent_cumulative_output) + output_tokens
        else:
            bootstrap_output = (
                int(bootstrap_usage["output_tokens"])
                if bootstrap_included and isinstance(bootstrap_usage.get("output_tokens"), int)
                else 0
            )
            cumulative_output = bootstrap_output + (parent_output if parent_run else 0) + output_tokens
    cumulative_credits = None
    cumulative_api_usd = None
    if usage_costs:
        cumulative_credits = (
            float(parent_cumulative_credits)
            if parent_cumulative_credits is not None
            else (
                float(bootstrap_cost.get("codex_standard_credit_equivalent", 0) or 0)
                if bootstrap_included else 0.0
            ) + parent_credits
        ) + credits
        cumulative_api_usd = (
            float(parent_cumulative_api_usd)
            if parent_cumulative_api_usd is not None
            else (
                float(bootstrap_cost.get("api_equivalent_usd", 0) or 0)
                if bootstrap_included else 0.0
            ) + parent_api_usd
        ) + api_usd
    status = "completed_route" if state.get("status") == "route_complete" else "partial_legal_prefix_not_complete"
    manifest_preflight_status = manifest.get("preflight_status", manifest.get("status"))
    manifest.update({
        "preflight_status": manifest_preflight_status,
        "status": status,
        "stop_status": state.get("status"),
        "actual_model_requests": actual_requests,
        "codex_cli_invocations": cli_invocations,
        "authorized_request_limit": state.get("authorized_new_request_limit"),
        "accepted_legal_actions": legal_attempts,
        "model_self_check_discrepancy_count": len(self_check_discrepancies),
        "retries": int(state.get("retry_count", 0) or 0),
        "tool_events": tool_events,
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
    evaluation = evaluate_route_candidate(road_map, candidate)
    prefix = {
        "accepted_prefix_distance_m": evaluation.route_distance_m,
        "accepted_prefix_duration_sec": evaluation.total_travel_time_sec,
        "accepted_prefix_energy_kwh": evaluation.energy_total_kwh,
        "energy_model_version": evaluation.energy_model_version,
    }
    for diagnostic, attempt in zip(
        invalid_attempts,
        [row for row in attempts if row.get("status") != "accepted_legal_step"],
    ):
        if not diagnostic["deterministic_local_check_passed"]:
            diagnostic["hypothetical_step_energy_kwh"] = None
            diagnostic["hypothetical_energy_status"] = "not_estimated_invalid_transition"
            continue
        edge_id = diagnostic.get("proposed_edge_id")
        target_node = diagnostic.get("proposed_target_node_id")
        if not edge_id or not target_node:
            diagnostic["hypothetical_step_energy_kwh"] = None
            diagnostic["hypothetical_energy_status"] = "not_estimated_missing_transition"
            continue
        hypothetical_candidate = RouteCandidate(
            route_id="codex-independent-pilot-proposal-" + str(attempt.get("request_number")),
            node_sequence=list(state.get("accepted_nodes", [])) + [target_node],
            edge_sequence=list(state.get("accepted_edges", [])) + [edge_id],
            route_source="codex_cli_unexecuted_proposal",
            route_reachability=True,
            validation_status="partial_prefix",
        )
        hypothetical_evaluation = evaluate_route_candidate(road_map, hypothetical_candidate)
        diagnostic["hypothetical_step_energy_kwh"] = round(
            max(0.0, hypothetical_evaluation.energy_total_kwh - evaluation.energy_total_kwh), 9
        )
        diagnostic["hypothetical_energy_status"] = "estimate_only_not_consumed"

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
        "minimum_remaining_moves_at_stop": last_validation.get("remaining_minimum_moves_after_move"),
        "stop_diagnostics": state.get("stop_diagnostics"),
        "actual_model_requests_this_continuation": actual_requests,
        "codex_cli_invocations_this_continuation": cli_invocations,
        "cli_invocations_without_model_turn": cli_invocations - actual_requests,
        "pre_request_failure_count": len(state.get("pre_request_failures", [])),
        "pre_request_failures": state.get("pre_request_failures", []),
        "authorized_new_request_limit": state.get("authorized_new_request_limit"),
        "accepted_legal_actions_this_continuation": legal_attempts,
        "invalid_or_stopped_actions": invalid_attempts,
        "model_self_check_discrepancy_count": len(self_check_discrepancies),
        "model_self_check_discrepancies": self_check_discrepancies,
        "retries": int(state.get("retry_count", 0) or 0),
        "tool_events": tool_events,
        "input_tokens_this_continuation": input_tokens if input_complete else None,
        "cached_input_tokens_this_continuation": cached_tokens if cached_complete else None,
        "output_tokens_this_continuation": output_tokens if output_complete else None,
        "known_total_tokens_this_continuation": input_tokens + output_tokens if input_complete and output_complete else None,
        "complete_total_tokens_this_continuation": input_tokens + output_tokens if input_complete and output_complete and with_usage == actual_requests else None,
        "codex_standard_credit_equivalent_this_continuation": credits if usage_costs else None,
        "api_equivalent_usd_this_continuation": api_usd if usage_costs else None,
        "account_cash_cost": None,
        "account_cash_cost_status": "not_reported_by_codex_cli; rate-card equivalents are not invoice amounts",
        "cumulative_requests_including_bootstrap_and_parent": cumulative_requests,
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
        "trace_count": len(trace_paths),
        "trace_evidence": [
            {"path": str(path.relative_to(run_dir)), **row}
            for path, row in zip(trace_paths, traces)
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
        "pricing_config": "configs/codex_cli_pricing_2026-10-22.json",
        "requests": [
            {"trace_path": str(path.relative_to(run_dir)), **row,
             "cost_estimate": estimate_codex_usage_cost(
                 model, row.get("input_tokens"), row.get("cached_input_tokens"), row.get("output_tokens")
             ) if isinstance(row.get("input_tokens"), int) and isinstance(row.get("output_tokens"), int) else None}
            for path, row in zip(trace_paths, traces)
        ],
        "totals": {
            "request_count": actual_requests,
            "input_tokens": input_tokens if input_complete else None,
            "cached_input_tokens": cached_tokens if cached_complete else None,
            "output_tokens": output_tokens if output_complete else None,
            "known_total_tokens": input_tokens + output_tokens if input_complete and output_complete else None,
            "codex_standard_credit_equivalent": credits if usage_costs else None,
            "api_equivalent_usd": api_usd if usage_costs else None,
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
