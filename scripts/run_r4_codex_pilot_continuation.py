#!/usr/bin/env python3
"""Continue the separately authorized one-step Codex R4 pilot, one request at a time.

This runner never repairs a model action or retries a request. Run --preflight-only
before any model request, then invoke --one-step once per authorized request.
"""

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient  # noqa: E402
from route_experiment.costs import evaluate_route_candidate  # noqa: E402
from route_experiment.models import (  # noqa: E402
    Edge,
    Node,
    PackageConfig,
    RoadMap,
    RoadsideFeature,
    RouteCandidate,
    VehicleOrderTask,
)
from route_experiment.r4_agent import replay_directed_edge_sequence  # noqa: E402
from route_experiment.r4_constraints import build_r4_route_output_constraint  # noqa: E402
from route_experiment.r4_stepwise import (  # noqa: E402
    _local_check,
    _model_self_check_mismatch_fields,
    audit_r4_stepwise_map_equivalence,
    build_r4_step_output_constraint,
    build_r4_stepwise_messages,
)
from route_experiment.routing import build_route_input_package  # noqa: E402


PREVIOUS_DIR = ROOT / "outputs/route_layer/r4_codex_smoke_v5"
PREFLIGHT_PATH = ROOT / "outputs/route_layer/r4_codex_offline_preflight_20261021/seed0_semantic_road_feature.json"
OUTPUT_DIR = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v1"
DEFAULT_OUTPUT_DIR = OUTPUT_DIR
MAX_NEW_CALLS = 8
ROUTE_ACTION_BUDGET = 32
MODEL = "gpt-5.6-luna"


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_frozen_task(preflight_path: Optional[Path] = None):
    source = _read_json(preflight_path or PREFLIGHT_PATH)
    map_data = source["map"]
    road_map = RoadMap(
        map_id=map_data["map_id"],
        version=map_data["version"],
        size=int(map_data["size"]),
        nodes=[Node(**item) for item in map_data["nodes"]],
        edges=[Edge(**item) for item in map_data["edges"]],
        boundary=dict(map_data["boundary"]),
        road_state_version=map_data["road_state_version"],
        roadside_features=[RoadsideFeature(**item) for item in map_data["roadside_features"]],
    )
    query = source["route_search"]["messages"][-1]["content"]
    query_data = json.loads(query)["q"]
    task_public = source["task_public"]
    task = VehicleOrderTask(
        task_id=task_public["task_id"],
        vehicle_id="vehicle-0000",
        order_id="order-0009",
        start_node_id=task_public["start_node_id"],
        pickup_node_id=query_data["p"],
        dropoff_node_id=query_data["d"],
        pickup_text=task_public["pickup_text"],
        dropoff_text=task_public["dropoff_text"],
        task_type=task_public["task_type"],
        true_pickup_node_id=query_data["p"],
        true_dropoff_node_id=query_data["d"],
    )
    package = build_route_input_package(road_map, task, PackageConfig())
    return source, road_map, task, package


def _initial_state(
    experiment_id: str = "r4_codex_independent_8x8_pilot_v1",
    authorized_request_limit: int = MAX_NEW_CALLS,
) -> Dict[str, Any]:
    authorized_request_limit = int(authorized_request_limit)
    if authorized_request_limit < 1 or authorized_request_limit > MAX_NEW_CALLS:
        raise ValueError("authorized_request_limit_out_of_range")
    source, road_map, task, package = _load_frozen_task()
    if road_map.map_id != "grid-8x8-seed-0" or len(road_map.nodes) != 64 or len(road_map.edges) != 205:
        raise RuntimeError("frozen_map_identity_mismatch")
    node_ids = {node.node_id for node in package.nodes}
    if task.start_node_id not in node_ids:
        raise RuntimeError("frozen_start_endpoint_mismatch")
    full_constraint = build_r4_route_output_constraint(
        package, task.pickup_node_id, task.dropoff_node_id, allow_failed=False
    )
    pickup_seen = task.start_node_id == task.pickup_node_id
    initial_completion_budget = min(ROUTE_ACTION_BUDGET, authorized_request_limit)
    initial_constraint = build_r4_step_output_constraint(
        package,
        full_constraint,
        current_node_id=task.start_node_id,
        pickup_seen=pickup_seen,
        visited_node_ids=[task.start_node_id],
        remaining_action_budget=initial_completion_budget,
    )
    initial_stop_reason = _authorized_call_budget_stop_reason(
        initial_constraint.remaining_minimum_moves, authorized_request_limit
    )
    preflight_messages = build_r4_stepwise_messages(
        package,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        current_node_id=task.start_node_id,
        visited_node_ids=[task.start_node_id],
        pickup_seen=pickup_seen,
        accepted_directions=[],
        remaining_action_budget=initial_completion_budget,
    )
    compact_map = json.loads(preflight_messages[-1]["content"])["m"]
    map_audit = audit_r4_stepwise_map_equivalence(package, compact_map)
    if not map_audit["passed"] or map_audit["expected_directed_edge_count"] != len(package.edges):
        raise RuntimeError("frozen_map_equivalence_failed")
    if not initial_constraint.edge_by_direction and not initial_stop_reason:
        raise RuntimeError("no_legal_continuation_directions")
    return {
        "schema_version": "r4_codex_pilot_continuation_state_v2",
        "experiment_id": experiment_id,
        "status": initial_stop_reason or "ready",
        "stop_diagnostics": (
            {
                "minimum_remaining_moves": initial_constraint.remaining_minimum_moves,
                "unused_authorized_calls": authorized_request_limit,
                "reason": initial_stop_reason,
            }
            if initial_stop_reason
            else None
        ),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_preflight": str(PREFLIGHT_PATH.relative_to(ROOT)) if PREFLIGHT_PATH.is_relative_to(ROOT) else str(PREFLIGHT_PATH),
        "source_preflight_sha256": hashlib.sha256(PREFLIGHT_PATH.read_bytes()).hexdigest(),
        "task_adjustment": source.get("route_search", {}).get("task_adjustment"),
        "map_id": road_map.map_id,
        "map_version": road_map.version,
        "task_id": task.task_id,
        "start_node_id": task.start_node_id,
        "pickup_node_id": task.pickup_node_id,
        "dropoff_node_id": task.dropoff_node_id,
        "model": MODEL,
        "reasoning_effort": "low",
        "authorized_new_request_limit": authorized_request_limit,
        "new_requests_used": 0,
        "codex_session_id": None,
        "codex_session_mode": "ephemeral_per_request",
        "retry_count": 0,
        "accepted_nodes": [task.start_node_id],
        "accepted_edges": [],
        "accepted_directions": [],
        "pickup_seen": pickup_seen,
        "route_action_budget": ROUTE_ACTION_BUDGET,
        "attempts": [],
        "map_equivalence_audit": map_audit,
        "initial_continuation_allowed_directions": sorted(initial_constraint.edge_by_direction),
        "initial_remaining_minimum_moves": initial_constraint.remaining_minimum_moves,
        "scope": "Fresh independent Codex 8x8 pilot; no inherited smoke action; separate from formal DeepSeek R4; no local route repair.",
    }


def _safe_unexecuted_self_check_mismatch_parent(
    parent: Dict[str, Any], parent_audit: Dict[str, Any]
) -> bool:
    """Allow resuming only after a locally valid move was stopped by model self-check."""
    attempts = parent.get("attempts", [])
    if (
        parent.get("status") != "stopped_illegal_or_invalid_action"
        or parent.get("retry_count") != 0
        or parent.get("new_requests_used") != len(attempts)
        or not attempts
        or parent_audit.get("stop_status") != "stopped_illegal_or_invalid_action"
        or parent_audit.get("actual_model_requests_this_continuation") != len(attempts)
        or parent_audit.get("tool_events") != 0
        or parent_audit.get("retries") != 0
    ):
        return False

    prior = attempts[:-1]
    last = attempts[-1]
    if any(
        item.get("status") != "accepted_legal_step"
        or item.get("tool_item_types")
        or item.get("retry_count") != 0
        for item in prior
    ):
        return False

    response = last.get("response", {})
    self_check = response.get("self_check", {})
    validation = last.get("validation", {})
    local = validation.get("deterministic_local_check", {})
    expected_old_fields = {
        "allowed_directed_edge",
        "starts_at_current",
        "inside_boundary",
        "pickup_order_ok",
        "not_revisited",
    }
    expected_current_fields = {
        "allowed_directed_edge",
        "starts_at_current",
        "inside_boundary",
        "pickup_order_ok",
        "target_node_is_new",
    }
    if set(self_check) == expected_old_fields:
        normalized_self_check = dict(self_check)
        normalized_self_check["target_node_is_new"] = self_check.get("not_revisited")
    elif set(self_check) == expected_current_fields:
        normalized_self_check = self_check
    else:
        return False
    mismatch_fields = _model_self_check_mismatch_fields(
        normalized_self_check, local.get("flags", {})
    )
    stopped_rows = parent_audit.get("invalid_or_stopped_actions", [])
    false_fields = sorted(key for key, value in self_check.items() if value is False)
    return bool(
        last.get("status") == "stopped_illegal_or_invalid_action"
        and not last.get("tool_item_types")
        and last.get("retry_count") == 0
        and validation.get("exact_fields") is True
        and validation.get("move_action") is True
        and validation.get("direction_in_dynamic_schema") is True
        and validation.get("failure_code_none") is True
        and validation.get("model_self_check_well_formed", True) is True
        and validation.get("all_model_self_checks_true") is False
        and bool(mismatch_fields)
        and false_fields
        and local.get("passed") is True
        and local.get("errors") == []
        and local.get("flags", {}).get("not_revisited") is True
        and local.get("expected_target_node_id") not in parent.get("accepted_nodes", [])
        and last.get("current_node_id") == parent.get("accepted_nodes", [None])[-1]
        and last.get("response", {}).get("direction") in last.get("allowed_directions", [])
        and len(stopped_rows) == 1
        and stopped_rows[0].get("request_number") == last.get("request_number")
        and stopped_rows[0].get("action_executed") is False
        and stopped_rows[0].get("deterministic_local_check_passed") is True
        and stopped_rows[0].get("model_self_check_false_fields") == false_fields
    )


def _resume_state(
    parent_dir: Path,
    allow_unexecuted_self_check_mismatch: bool = False,
) -> Dict[str, Any]:
    """Start a separately budgeted continuation from a completed pilot prefix."""
    parent_dir = Path(parent_dir).resolve()
    parent_state_path = parent_dir / "state.json"
    parent_audit_path = parent_dir / "final_audit.json"
    if not parent_state_path.is_file() or not parent_audit_path.is_file():
        raise RuntimeError("resume_parent_artifacts_missing")
    parent = _read_json(parent_state_path)
    parent_audit = _read_json(parent_audit_path)
    safe_self_check_resume = (
        allow_unexecuted_self_check_mismatch
        and _safe_unexecuted_self_check_mismatch_parent(parent, parent_audit)
    )
    clean_budget_stop = parent.get("status") in {
        "authorized_call_limit_exhausted",
        "insufficient_authorized_budget_for_remaining_route",
    }
    if not clean_budget_stop and not safe_self_check_resume:
        if allow_unexecuted_self_check_mismatch:
            raise RuntimeError("resume_self_check_mismatch_parent_not_safe")
        raise RuntimeError("resume_parent_not_cleanly_capped")
    if parent.get("retry_count") != 0:
        raise RuntimeError("resume_parent_call_contract_mismatch")
    if (
        not safe_self_check_resume
        and parent.get("status") == "authorized_call_limit_exhausted"
        and parent.get("new_requests_used") != MAX_NEW_CALLS
    ):
        raise RuntimeError("resume_parent_call_contract_mismatch")
    accepted_attempts = [
        item for item in parent.get("attempts", [])
        if item.get("status") == "accepted_legal_step"
    ]
    expected_accepted_attempts = int(parent.get("new_requests_used", 0)) - int(safe_self_check_resume)
    if len(accepted_attempts) != expected_accepted_attempts or any(
        item.get("tool_item_types") or item.get("retry_count") != 0
        for item in accepted_attempts
    ):
        raise RuntimeError("resume_parent_contains_invalid_or_tool_action")
    if parent_audit.get("actual_model_requests_this_continuation") != parent.get("new_requests_used"):
        raise RuntimeError("resume_parent_audit_call_count_mismatch")
    if parent_audit.get("tool_events") != 0 or parent_audit.get("retries") != 0:
        raise RuntimeError("resume_parent_audit_tool_or_retry_mismatch")

    _, road_map, task, package = _load_frozen_task()
    if (
        parent.get("map_id") != road_map.map_id
        or parent.get("task_id") != task.task_id
        or parent.get("pickup_node_id") != package.pickup_node_id
        or parent.get("dropoff_node_id") != package.dropoff_node_id
        or parent_audit.get("map_id") != road_map.map_id
    ):
        raise RuntimeError("resume_parent_task_or_map_mismatch")

    current = parent["accepted_nodes"][-1]
    full_constraint = build_r4_route_output_constraint(
        package, parent["pickup_node_id"], parent["dropoff_node_id"], allow_failed=False
    )
    route_action_budget = ROUTE_ACTION_BUDGET - len(parent["accepted_edges"])
    resume_completion_budget = min(MAX_NEW_CALLS, route_action_budget)
    step_constraint = build_r4_step_output_constraint(
        package,
        full_constraint,
        current_node_id=current,
        pickup_seen=bool(parent["pickup_seen"]),
        visited_node_ids=parent["accepted_nodes"],
        remaining_action_budget=resume_completion_budget,
    )
    resume_stop_reason = _authorized_call_budget_stop_reason(
        step_constraint.remaining_minimum_moves, MAX_NEW_CALLS
    )
    if not step_constraint.edge_by_direction and not resume_stop_reason:
        raise RuntimeError("resume_parent_has_no_legal_continuation")
    messages = build_r4_stepwise_messages(
        package,
        parent["start_node_id"],
        parent["pickup_node_id"],
        parent["dropoff_node_id"],
        current_node_id=current,
        visited_node_ids=parent["accepted_nodes"],
        pickup_seen=bool(parent["pickup_seen"]),
        accepted_directions=parent["accepted_directions"],
        remaining_action_budget=resume_completion_budget,
    )
    compact_map = json.loads(messages[-1]["content"])["m"]
    map_audit = audit_r4_stepwise_map_equivalence(package, compact_map)
    if not map_audit["passed"] or map_audit["expected_directed_edge_count"] != 205:
        raise RuntimeError("resume_map_equivalence_failed")

    state = copy.deepcopy(parent)
    parent_usage = parent.get("usage_totals", {})
    parent_experiment_id = str(parent.get("experiment_id", ""))
    parent_prefix, version_separator, parent_version = parent_experiment_id.rpartition("_v")
    if version_separator and parent_version.isdigit():
        resumed_experiment_id = "{0}_v{1}".format(
            parent_prefix, int(parent_version) + 1
        )
    else:
        resumed_experiment_id = parent_experiment_id + "_continued"
    # This continuation is specified as one isolated CLI process per step.
    # A thread ID returned by an ephemeral invocation is only audit metadata;
    # it may not be resumed by a later process. Always start the next step with
    # the complete map/state prompt and keep the session ID out of continuation
    # state, even if a legacy parent stored one.
    state.update({
        "schema_version": "r4_codex_pilot_continuation_state_v2",
        "experiment_id": resumed_experiment_id,
        "status": resume_stop_reason or "ready",
        "stop_diagnostics": (
            {
                "minimum_remaining_moves": step_constraint.remaining_minimum_moves,
                "unused_authorized_calls": MAX_NEW_CALLS,
                "reason": resume_stop_reason,
            }
            if resume_stop_reason
            else None
        ),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "parent_run": str(parent_dir.relative_to(ROOT)) if parent_dir.is_relative_to(ROOT) else str(parent_dir),
        "parent_state_sha256": hashlib.sha256(parent_state_path.read_bytes()).hexdigest(),
        "parent_final_audit_sha256": hashlib.sha256(parent_audit_path.read_bytes()).hexdigest(),
        "parent_requests": int(parent.get("new_requests_used", 0)),
        "parent_input_tokens": parent_usage.get("input_tokens"),
        "parent_output_tokens": parent_usage.get("output_tokens"),
        "parent_latency_ms": parent_usage.get("latency_ms"),
        "authorized_new_request_limit": MAX_NEW_CALLS,
        "new_requests_used": 0,
        "retry_count": 0,
        "codex_session_id": None,
        "codex_session_mode": "ephemeral_per_request",
        "attempts": [],
        "map_equivalence_audit": map_audit,
        "initial_continuation_allowed_directions": sorted(step_constraint.edge_by_direction),
        "initial_remaining_minimum_moves": step_constraint.remaining_minimum_moves,
        "scope": "Continuation of the independent Codex 8x8 pilot; up to 8 new one-step requests; separate from formal route comparison.",
        **(
            {
                "resume_reason": (
                    "unexecuted_false_model_self_check_with_local_pass"
                    if str(parent.get("experiment_id", "")).endswith("_v5")
                    else "unexecuted_false_not_revisited_self_check_with_local_pass"
                ),
                "rejected_parent_action_replayed": False,
            }
            if safe_self_check_resume
            else {}
        ),
        "usage_totals": {
            "new_requests": 0,
            "retry_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": 0.0,
            "account_cost": "not_reported_by_cli",
        },
    })
    state["route_metrics"] = _route_summary(state, road_map)
    return state


def preflight_only(
    resume_from: Path = None,
    allow_unexecuted_self_check_mismatch: bool = False,
    experiment_id: str = None,
    authorized_request_limit: int = MAX_NEW_CALLS,
) -> None:
    if OUTPUT_DIR.exists():
        raise FileExistsError("continuation_output_already_exists")
    if resume_from and experiment_id:
        raise ValueError("experiment_id_override_only_supported_for_fresh_run")
    if resume_from and authorized_request_limit != MAX_NEW_CALLS:
        raise ValueError("request_limit_override_only_supported_for_fresh_run")
    state = (
        _resume_state(resume_from, allow_unexecuted_self_check_mismatch)
        if resume_from
        else _initial_state(
            experiment_id or "r4_codex_independent_8x8_pilot_v1",
            authorized_request_limit,
        )
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=False)
    (OUTPUT_DIR / "traces").mkdir()
    _write_json(OUTPUT_DIR / "state.json", state)
    manifest = {
        "schema_version": "r4_codex_pilot_continuation_manifest_v2",
        "experiment_id": state["experiment_id"],
        "status": "offline_preflight_passed_no_model_request",
        "authorization": (
            "up to {0} sequential one-step requests; zero retry; stop on tool event, illegal action, give_up, or call error; stop early if the shortest remaining route exceeds unused authorized calls"
        ).format(state["authorized_new_request_limit"]),
        "authorized_request_limit": state["authorized_new_request_limit"],
        "model": MODEL,
        "reasoning_effort": "low",
        "route_action_budget": ROUTE_ACTION_BUDGET,
        "route_action_budget_is_independent_of_authorized_request_limit": True,
        "authorization_request_limit_is_separate_from_route_action_budget": True,
        "api_calls_made_by_this_preflight": 0,
        "retry_count": 0,
        "codex_cli_session_mode": state.get("codex_session_mode", "ephemeral_per_request"),
        "codex_cli_session_storage": (
            "one isolated Codex CLI conversation per route task; session ID stored in local state"
            if state.get("codex_session_mode") == "persistent_per_task"
            else "one ephemeral Codex CLI process per request; no conversation session is persisted"
        ),
        "per_request_route_schema": "dynamic direction enum reapplied on every initial or resumed CLI invocation",
        "direction_schema_completion_budget": "minimum of remaining route action budget and remaining authorized model calls",
        "per_step_map_payload": (
            "full compact map only on initial request; later requests send q/s state and current dynamic directions"
            if state.get("codex_session_mode") == "persistent_per_task"
            else "full compact map and current q/s state are sent on every isolated request"
        ),
        "initial_state": {
            "current_node": state["accepted_nodes"][-1],
            "visited_nodes": state["accepted_nodes"],
            "pickup_node": state["pickup_node_id"],
            "dropoff_node": state["dropoff_node_id"],
            "allowed_directions": state["initial_continuation_allowed_directions"],
            "minimum_remaining_moves": state["initial_remaining_minimum_moves"],
            "route_action_budget_remaining": ROUTE_ACTION_BUDGET - len(state["accepted_edges"]),
            "requests_authorized_in_this_continuation": state["authorized_new_request_limit"],
            "can_complete_with_authorized_requests": (
                state["initial_remaining_minimum_moves"] <= state["authorized_new_request_limit"]
            ),
            "early_stop_if_remaining_route_exceeds_unused_authorized_calls": True,
        },
        "parent_run": state.get("parent_run"),
        "parent_requests_already_spent": state.get("parent_requests", 0),
        "resume_reason": state.get("resume_reason"),
        "rejected_parent_action_replayed": state.get("rejected_parent_action_replayed", False),
        "map_equivalence_audit": state["map_equivalence_audit"],
        "task_adjustment": state.get("task_adjustment"),
        "source_preflight_sha256": state["source_preflight_sha256"],
        "prior_smoke_result_sha256": state.get("prior_smoke_result_sha256"),
    }
    _write_json(OUTPUT_DIR / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def _route_summary(state: Dict[str, Any], road_map: RoadMap) -> Dict[str, Any]:
    edges = state["accepted_edges"]
    if not edges:
        return {"accepted_prefix_energy_kwh": None, "accepted_prefix_duration_sec": None}
    candidate = RouteCandidate(
        route_id="codex-independent-pilot-" + state["task_id"],
        node_sequence=list(state["accepted_nodes"]),
        edge_sequence=list(edges),
        route_source="codex_cli_independent_pilot",
        route_reachability=True,
        validation_status=("valid" if state["status"] == "route_complete" else "partial_legal_prefix"),
    )
    evaluation = evaluate_route_candidate(road_map, candidate)
    return {
        "accepted_prefix_energy_kwh": evaluation.energy_total_kwh,
        "accepted_prefix_duration_sec": evaluation.total_travel_time_sec,
        "accepted_prefix_distance_m": evaluation.route_distance_m,
        "energy_basis": "shared simulated route energy model; legal accepted prefix only",
        **(
            {
                "completed_route_energy_kwh": evaluation.energy_total_kwh,
                "completed_route_duration_sec": evaluation.total_travel_time_sec,
                "completed_route_distance_m": evaluation.route_distance_m,
                "energy_model_version": evaluation.energy_model_version,
            }
            if state["status"] == "route_complete"
            else {}
        ),
    }


def _model_self_check_is_well_formed(payload: Dict[str, Any]) -> bool:
    self_check = payload.get("self_check")
    expected = {
        "allowed_directed_edge",
        "starts_at_current",
        "inside_boundary",
        "pickup_order_ok",
        "target_node_is_new",
    }
    return (
        isinstance(self_check, dict)
        and set(self_check) == expected
        and all(isinstance(value, bool) for value in self_check.values())
    )


def _model_self_check_all_true(payload: Dict[str, Any]) -> bool:
    return _model_self_check_is_well_formed(payload) and all(
        payload["self_check"].values()
    )


def _authorized_call_budget_stop_reason(
    minimum_remaining_moves: Optional[int], remaining_authorized_calls: int
) -> Optional[str]:
    """Return a deterministic stop reason when this request budget cannot finish the task."""
    if minimum_remaining_moves is None:
        return "remaining_route_unreachable"
    if minimum_remaining_moves > max(0, int(remaining_authorized_calls)):
        return "insufficient_authorized_budget_for_remaining_route"
    return None


def one_step() -> None:
    if not (OUTPUT_DIR / "state.json").exists():
        raise RuntimeError("run_preflight_first")
    state = _read_json(OUTPUT_DIR / "state.json")
    if state["status"] != "ready":
        raise RuntimeError("pilot_stopped_status:" + str(state["status"]))
    authorized_limit = int(state.get("authorized_new_request_limit", MAX_NEW_CALLS))
    if state["new_requests_used"] >= authorized_limit:
        state["status"] = "authorized_call_limit_exhausted"
        _write_json(OUTPUT_DIR / "state.json", state)
        raise RuntimeError("authorized_call_limit_exhausted")

    _, road_map, task, package = _load_frozen_task()
    current = state["accepted_nodes"][-1]
    route_action_budget = ROUTE_ACTION_BUDGET - len(state["accepted_edges"])
    remaining_authorized_calls = max(
        0,
        int(state.get("authorized_new_request_limit", MAX_NEW_CALLS))
        - int(state["new_requests_used"]),
    )
    effective_completion_budget = min(route_action_budget, remaining_authorized_calls)
    full_constraint = build_r4_route_output_constraint(
        package, state["pickup_node_id"], state["dropoff_node_id"], allow_failed=False
    )
    constraint = build_r4_step_output_constraint(
        package,
        full_constraint,
        current_node_id=current,
        pickup_seen=bool(state["pickup_seen"]),
        visited_node_ids=state["accepted_nodes"],
        remaining_action_budget=effective_completion_budget,
    )
    budget_stop_reason = _authorized_call_budget_stop_reason(
        constraint.remaining_minimum_moves, remaining_authorized_calls
    )
    if budget_stop_reason:
        state["status"] = budget_stop_reason
        state["stop_diagnostics"] = {
            "minimum_remaining_moves": constraint.remaining_minimum_moves,
            "unused_authorized_calls": remaining_authorized_calls,
            "reason": budget_stop_reason,
        }
        _write_json(OUTPUT_DIR / "state.json", state)
        raise RuntimeError(budget_stop_reason)
    if not constraint.edge_by_direction:
        state["status"] = "local_pre_request_no_allowed_action"
        _write_json(OUTPUT_DIR / "state.json", state)
        raise RuntimeError("local_pre_request_no_allowed_action")
    messages = build_r4_stepwise_messages(
        package,
        state["start_node_id"],
        state["pickup_node_id"],
        state["dropoff_node_id"],
        current_node_id=current,
        visited_node_ids=state["accepted_nodes"],
        pickup_seen=bool(state["pickup_seen"]),
        accepted_directions=state["accepted_directions"],
        remaining_action_budget=effective_completion_budget,
    )
    call_number = int(state["new_requests_used"]) + 1
    attempt_log_index = len(state["attempts"]) + 1
    client = CodexCliRouteStepClient(
        model=MODEL,
        trace_dir=OUTPUT_DIR / "traces",
        reasoning_effort="low",
        timeout_sec=300,
        route_constraint=constraint,
        persist_session=state.get("codex_session_mode") == "persistent_per_task",
        session_id=state.get("codex_session_id"),
        working_directory=(
            OUTPUT_DIR / "codex_empty_workspace"
            if state.get("codex_session_mode") == "persistent_per_task"
            else None
        ),
    )
    # The client object is recreated for each one-request process invocation;
    # continue trace numbering from actual model requests already made.
    client.call_count = int(state["new_requests_used"])
    attempt: Dict[str, Any] = {
        "request_number": call_number,
        "cumulative_request_number": int(state.get("parent_requests", 0)) + call_number,
        "step_number": len(state["accepted_edges"]) + 1,
        "current_node_id": current,
        "pickup_seen_before": bool(state["pickup_seen"]),
        "remaining_action_budget": route_action_budget,
        "remaining_authorized_calls": remaining_authorized_calls,
        "effective_completion_budget": effective_completion_budget,
        "allowed_directions": sorted(constraint.edge_by_direction),
        "allowed_edge_ids": {
            direction: edge.edge_id for direction, edge in constraint.edge_by_direction.items()
        },
        "messages_sha256": hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "schema_sha256": constraint.schema_sha256,
        "request_started_at_utc": datetime.now(timezone.utc).isoformat(),
        "retry_count": 0,
    }
    state["status"] = "request_in_progress"
    _write_json(OUTPUT_DIR / "state.json", state)
    try:
        response = client.chat(messages)
        attempt["provider_record"] = response.provider_audit
        payload = json.loads(response.content)
    except Exception as error:
        provider_record = dict(client.last_record)
        attempt["provider_record"] = provider_record
        if (
            state.get("codex_session_mode") == "persistent_per_task"
            and provider_record.get("session_id")
        ):
            state["codex_session_id"] = provider_record["session_id"]
        event_audit = provider_record.get("event_audit", {})
        attempt["usage"] = {
            "input_tokens": event_audit.get("input_tokens"),
            "cached_input_tokens": event_audit.get("cached_input_tokens"),
            "output_tokens": event_audit.get("output_tokens"),
            "latency_ms": provider_record.get("latency_ms"),
            "account_cost": "not_reported_by_cli",
        }
        attempt["error_type"] = type(error).__name__
        attempt["error"] = CodexCliRouteStepClient._redact_diagnostics(str(error))[:500]
        tool_events = event_audit.get("tool_item_types", [])
        if provider_record.get("status") == "not_called":
            failure = dict(attempt)
            failure["status"] = "not_called_preflight_rejection"
            failure["model_request_started"] = False
            state.setdefault("pre_request_failures", []).append(failure)
            state["status"] = "stopped_pre_request_error"
            state["usage_totals"] = {
                "new_requests": state["new_requests_used"],
                "retry_count": 0,
                "input_tokens": sum(int(item.get("usage", {}).get("input_tokens") or 0) for item in state["attempts"]),
                "cached_input_tokens": sum(int(item.get("usage", {}).get("cached_input_tokens") or 0) for item in state["attempts"]),
                "output_tokens": sum(int(item.get("usage", {}).get("output_tokens") or 0) for item in state["attempts"]),
                "latency_ms": round(sum(float(item.get("usage", {}).get("latency_ms") or 0.0) for item in state["attempts"]), 3),
                "account_cost": "not_reported_by_cli",
            }
            failure_index = len(state["pre_request_failures"])
            _write_json(
                OUTPUT_DIR / "pre_request_failures" / ("pre_request_{0:02d}.json".format(failure_index)),
                failure,
            )
            _write_json(OUTPUT_DIR / "state.json", state)
            print(json.dumps(failure, ensure_ascii=False, indent=2))
            return
        attempt["status"] = "stopped_tool_event" if tool_events else "stopped_request_error"
        attempt["tool_item_types"] = tool_events
        state["new_requests_used"] += 1 if provider_record.get("status") != "not_called" else 0
        state["attempts"].append(attempt)
        state["status"] = attempt["status"]
        state["usage_totals"] = {
            "new_requests": state["new_requests_used"],
            "retry_count": 0,
            "input_tokens": sum(int(item.get("usage", {}).get("input_tokens") or 0) for item in state["attempts"]),
            "cached_input_tokens": sum(int(item.get("usage", {}).get("cached_input_tokens") or 0) for item in state["attempts"]),
            "output_tokens": sum(int(item.get("usage", {}).get("output_tokens") or 0) for item in state["attempts"]),
            "latency_ms": round(sum(float(item.get("usage", {}).get("latency_ms") or 0.0) for item in state["attempts"]), 3),
            "account_cost": "not_reported_by_cli",
        }
        _write_json(OUTPUT_DIR / ("attempt_{0:02d}.json".format(attempt_log_index)), attempt)
        _write_json(OUTPUT_DIR / "state.json", state)
        print(json.dumps(attempt, ensure_ascii=False, indent=2))
        return

    state["new_requests_used"] += 1
    if state.get("codex_session_mode") == "persistent_per_task":
        state["codex_session_id"] = response.session_id
    attempt["codex_session_id"] = response.session_id
    attempt["response"] = payload
    attempt["usage"] = {
        "input_tokens": response.input_tokens,
        "cached_input_tokens": response.cached_input_tokens,
        "output_tokens": response.output_tokens,
        "latency_ms": response.latency_ms,
        "account_cost": "not_reported_by_cli",
    }
    attempt["tool_item_types"] = response.provider_audit.get("event_audit", {}).get("tool_item_types", [])
    if attempt["tool_item_types"]:
        attempt["status"] = "stopped_tool_event"
        state["status"] = attempt["status"]
    elif payload.get("action") == "give_up":
        attempt["status"] = "stopped_model_gave_up"
        state["status"] = attempt["status"]
    else:
        fields_ok = set(payload) == {"action", "direction", "self_check", "failure_reason_code"}
        self_check_well_formed = _model_self_check_is_well_formed(payload)
        self_check_all_true = _model_self_check_all_true(payload)
        direction = payload.get("direction")
        local_check = _local_check(
            direction if isinstance(direction, str) else "",
            current,
            bool(state["pickup_seen"]),
            list(state["accepted_nodes"]),
            {node.node_id: node for node in package.nodes},
            {(edge.from_node, edge.to_node): edge for edge in package.edges},
            set(package.blocked_edge_ids),
            full_constraint,
            remaining_action_budget=effective_completion_budget,
        )
        self_check_mismatch_fields = (
            _model_self_check_mismatch_fields(
                payload["self_check"], local_check["flags"]
            )
            if self_check_well_formed
            else []
        )
        validation = {
            "exact_fields": fields_ok,
            "move_action": payload.get("action") == "move",
            "direction_in_dynamic_schema": direction in constraint.edge_by_direction,
            "failure_code_none": payload.get("failure_reason_code") == "none",
            "model_self_check_well_formed": self_check_well_formed,
            "all_model_self_checks_true": self_check_all_true,
            "model_self_check_mismatch_fields": self_check_mismatch_fields,
            "deterministic_local_check": {
                key: value for key, value in local_check.items() if key != "edge"
            },
        }
        accepted = (
            fields_ok
            and payload.get("action") == "move"
            and direction in constraint.edge_by_direction
            and payload.get("failure_reason_code") == "none"
            and self_check_well_formed
            and local_check["passed"]
        )
        attempt["validation"] = validation
        if not accepted:
            attempt["status"] = "stopped_illegal_or_invalid_action"
            state["status"] = attempt["status"]
        else:
            edge = constraint.edge_by_direction[direction]
            attempt["selected_edge_id"] = edge.edge_id
            attempt["target_node_id"] = edge.to_node
            attempt["status"] = "accepted_legal_step"
            state["accepted_edges"].append(edge.edge_id)
            state["accepted_directions"].append(direction)
            state["accepted_nodes"].append(edge.to_node)
            state["pickup_seen"] = bool(state["pickup_seen"] or edge.to_node == state["pickup_node_id"])
            if edge.to_node == state["dropoff_node_id"]:
                if not state["pickup_seen"]:
                    attempt["status"] = "stopped_illegal_endpoint_order"
                    state["status"] = attempt["status"]
                else:
                    attempt["status"] = "route_completed"
                    state["status"] = "route_complete"
            else:
                remaining_authorized_calls = max(
                    0,
                    int(state.get("authorized_new_request_limit", MAX_NEW_CALLS))
                    - int(state["new_requests_used"]),
                )
                minimum_remaining_moves = local_check.get(
                    "remaining_minimum_moves_after_move"
                )
                budget_stop_reason = _authorized_call_budget_stop_reason(
                    minimum_remaining_moves, remaining_authorized_calls
                )
                if budget_stop_reason:
                    state["status"] = budget_stop_reason
                    state["stop_diagnostics"] = {
                        "minimum_remaining_moves": minimum_remaining_moves,
                        "unused_authorized_calls": remaining_authorized_calls,
                        "reason": budget_stop_reason,
                    }
                    attempt["stop_reason"] = budget_stop_reason
                else:
                    state["status"] = "ready"
    if state["status"] == "ready" and state["new_requests_used"] >= authorized_limit:
        state["status"] = "authorized_call_limit_exhausted"
    attempt["request_completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    state["attempts"].append(attempt)
    state["usage_totals"] = {
        "new_requests": state["new_requests_used"],
        "retry_count": 0,
        "input_tokens": sum(int(item.get("usage", {}).get("input_tokens", 0) or 0) for item in state["attempts"]),
        "cached_input_tokens": sum(int(item.get("usage", {}).get("cached_input_tokens", 0) or 0) for item in state["attempts"]),
        "output_tokens": sum(int(item.get("usage", {}).get("output_tokens", 0) or 0) for item in state["attempts"]),
        "latency_ms": round(sum(float(item.get("usage", {}).get("latency_ms", 0.0) or 0.0) for item in state["attempts"]), 3),
        "account_cost": "not_reported_by_cli",
    }
    state["route_metrics"] = _route_summary(state, road_map)
    _write_json(OUTPUT_DIR / ("attempt_{0:02d}.json".format(attempt_log_index)), attempt)
    _write_json(OUTPUT_DIR / "state.json", state)
    print(
        json.dumps(
            {
                "status": state["status"],
                "request_number": call_number,
                "current_node_id": attempt["current_node_id"],
                "allowed_directions": attempt["allowed_directions"],
                "response": attempt.get("response"),
                "validation": attempt.get("validation"),
                "selected_edge_id": attempt.get("selected_edge_id"),
                "target_node_id": attempt.get("target_node_id"),
                "usage": attempt.get("usage"),
                "tool_item_types": attempt.get("tool_item_types"),
                "new_requests_used": state["new_requests_used"],
                "authorized_new_request_limit": authorized_limit,
                "stop_diagnostics": state.get("stop_diagnostics"),
                "next_call": "permitted" if state["status"] == "ready" else "stopped",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    global OUTPUT_DIR, PREFLIGHT_PATH
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight-only", action="store_true")
    group.add_argument("--one-step", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--preflight-path",
        type=Path,
        default=PREFLIGHT_PATH,
        help="Frozen offline task/map preflight JSON for this independent pilot",
    )
    parser.add_argument(
        "--experiment-id",
        default=None,
        help="Unique identifier for a fresh pilot; may not override a resumed parent's experiment chain",
    )
    parser.add_argument(
        "--request-limit",
        type=int,
        default=MAX_NEW_CALLS,
        help="Authorized maximum for this fresh pilot (1-8); cannot exceed the hard-coded safety cap",
    )
    parser.add_argument("--resume-from", type=Path, default=None,
                        help="Parent pilot directory; use only with --preflight-only to start a new bounded continuation")
    parser.add_argument(
        "--allow-unexecuted-self-check-mismatch",
        action="store_true",
        help="Allow resuming only from the v3 not_revisited=false proposal that passed deterministic local validation and was not executed",
    )
    args = parser.parse_args()
    OUTPUT_DIR = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    PREFLIGHT_PATH = args.preflight_path if args.preflight_path.is_absolute() else ROOT / args.preflight_path
    if args.resume_from and not args.preflight_only:
        parser.error("--resume-from can only be used with --preflight-only")
    if args.allow_unexecuted_self_check_mismatch and not args.resume_from:
        parser.error("--allow-unexecuted-self-check-mismatch requires --resume-from")
    if args.resume_from and args.experiment_id:
        parser.error("--experiment-id is only supported for a fresh pilot")
    if args.request_limit < 1 or args.request_limit > MAX_NEW_CALLS:
        parser.error("--request-limit must be between 1 and {0}".format(MAX_NEW_CALLS))
    if args.preflight_only:
        preflight_only(
            args.resume_from or None,
            args.allow_unexecuted_self_check_mismatch,
            args.experiment_id,
            args.request_limit,
        )
    else:
        one_step()


if __name__ == "__main__":
    main()
