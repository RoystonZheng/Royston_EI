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
LOADED_RUNNER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient  # noqa: E402
from r4_route_safety_gate import route_safety_gate_block_reason_for_path  # noqa: E402
from codex_cli_support import normalize_session_cumulative_usage  # noqa: E402
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
    accepted_prefix_sha256,
    audit_accepted_prefix_suffix_witnesses,
    audit_r4_stepwise_map_equivalence,
    build_r4_step_output_constraint,
    build_r4_stepwise_messages,
)
from route_experiment.routing import build_route_input_package  # noqa: E402
from render_r4_codex_pilot_replay import render as render_pilot_replay  # noqa: E402


PREVIOUS_DIR = ROOT / "outputs/route_layer/r4_codex_smoke_v5"
PREFLIGHT_PATH = ROOT / "outputs/route_layer/r4_codex_offline_preflight_20261021/seed0_semantic_road_feature.json"
OUTPUT_DIR = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v1"
DEFAULT_OUTPUT_DIR = OUTPUT_DIR
MAX_NEW_CALLS = 8
# Formal request/action ceilings are derived from the frozen task graph's
# unique nodes. With revisit forbidden, a simple path can use at most N - 1
# moves; this is a structural bound, not a quota.
MAX_FORMAL_STEPWISE_CALLS = 100 * 100 - 1
DEFAULT_NEW_CALLS = 8
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


def _refresh_replay() -> Path:
    """Regenerate the task replay from the saved state and attempts."""
    (OUTPUT_DIR / "traces").mkdir(parents=True, exist_ok=True)
    return render_pilot_replay(OUTPUT_DIR, OUTPUT_DIR / "replay.html")


def _attempt_usage_record(raw_usage: Dict[str, Any], state: Dict[str, Any], session_id: Optional[str]):
    """Normalize resumed-session cumulative usage before adding it to the ledger."""
    fields = ("input_tokens", "cached_input_tokens", "output_tokens")
    raw = {key: raw_usage.get(key) for key in fields}
    if state.get("codex_session_mode") != "persistent_per_task":
        return {
            **raw,
            "latency_ms": raw_usage.get("latency_ms"),
            "account_cost": "not_reported_by_cli",
            "accounting_basis": "reported_per_invocation",
        }

    previous = {}
    for prior_attempt in reversed(state.get("attempts", [])):
        prior_session_id = prior_attempt.get("codex_session_id") or prior_attempt.get(
            "provider_record", {}
        ).get("session_id")
        if prior_session_id != session_id:
            continue
        prior_usage = prior_attempt.get("usage", {})
        previous = prior_usage.get("session_cumulative") or {
            key: prior_usage.get(key) for key in fields
        }
        break
    normalized = normalize_session_cumulative_usage(raw, previous)
    return {
        **normalized,
        "latency_ms": raw_usage.get("latency_ms"),
        "account_cost": "not_reported_by_cli",
    }


def _rollback_rejected_action_one_node(
    state: Dict[str, Any],
    *,
    current_node_id: str,
    rejected_direction: Optional[str],
    rejected_target_node_id: Optional[str],
    rejection_code: str,
) -> Dict[str, Any]:
    """Reject a proposed edge, then backtrack exactly one accepted edge if possible."""
    nodes = state.get("accepted_nodes")
    edges = state.get("accepted_edges")
    directions = state.get("accepted_directions")
    if not isinstance(nodes, list) or not nodes or nodes[-1] != current_node_id:
        raise ValueError("r4_rejected_action_current_node_mismatch")
    if not isinstance(edges, list) or not isinstance(directions, list):
        raise ValueError("r4_rejected_action_prefix_malformed")
    if len(edges) != len(directions) or len(nodes) != len(edges) + 1:
        raise ValueError("r4_rejected_action_prefix_length_mismatch")

    rejected_by_node = state.setdefault("rejected_directions_by_node", {})
    rejected = rejected_by_node.setdefault(current_node_id, [])
    if rejected_direction in {"E", "W", "N", "S"} and rejected_direction not in rejected:
        rejected.append(rejected_direction)
        rejected.sort()

    rejected_edge = {
        "from_node_id": current_node_id,
        "to_node_id": rejected_target_node_id,
        "direction": rejected_direction,
        "reason": rejection_code,
    }
    recovery: Dict[str, Any] = {
        "event": "reject_candidate_backtrack_one_legal_node_and_research",
        "rejected_edge": rejected_edge,
        "rejected_edge_added_to_route": False,
        "rejection_code": rejection_code,
        "rollback_count_nodes": 0,
        "rollback_node_id": current_node_id,
        "removed_accepted_edge": None,
        "retry_from_node_id": current_node_id,
        "re_search_required": True,
    }

    if edges:
        predecessor = nodes[-2]
        removed_edge = {
            "from_node_id": predecessor,
            "to_node_id": current_node_id,
            "edge_id": edges[-1],
            "direction": directions[-1],
        }
        edges.pop()
        directions.pop()
        nodes.pop()
        backtracked = state.setdefault("backtracked_directions_by_node", {})
        at_checkpoint = backtracked.setdefault(predecessor, [])
        if removed_edge["direction"] not in at_checkpoint:
            at_checkpoint.append(removed_edge["direction"])
            at_checkpoint.sort()
        recovery.update({
            "rollback_count_nodes": 1,
            "rollback_node_id": predecessor,
            "removed_accepted_edge": removed_edge,
            "retry_from_node_id": predecessor,
            "backtracked_direction_at_checkpoint": removed_edge["direction"],
        })
        if "pickup_node_id" in state:
            state["pickup_seen"] = state["pickup_node_id"] in nodes

    state.setdefault("route_recovery_events", []).append(recovery)
    state["pending_route_research_feedback"] = {
        "code": "rejected_candidate_and_backtracked_one_node",
        "rejected_edge": rejected_edge,
        "rollback_node_id": recovery["rollback_node_id"],
        "retry_from_node_id": recovery["retry_from_node_id"],
        "backtracked_direction_at_checkpoint": recovery.get(
            "backtracked_direction_at_checkpoint"
        ),
    }
    return recovery


def _summarize_attempt_usage(attempts: List[Dict[str, Any]], new_requests: int) -> Dict[str, Any]:
    """Keep aggregate usage unknown when any invoked request lacks a report."""
    fields = ("input_tokens", "cached_input_tokens", "output_tokens")
    known: Dict[str, List[int]] = {field: [] for field in fields}
    missing_by_field: Dict[str, List[int]] = {field: [] for field in fields}
    for attempt in attempts:
        request_number = int(attempt.get("request_number", 0) or 0)
        usage = attempt.get("usage") if isinstance(attempt.get("usage"), dict) else {}
        for field in fields:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                known[field].append(value)
            else:
                missing_by_field[field].append(request_number)

    unknown_requests = sorted({
        request_number
        for field_requests in missing_by_field.values()
        for request_number in field_requests
        if request_number
    })
    totals: Dict[str, Any] = {
        "new_requests": int(new_requests),
        "retry_count": 0,
        "usage_complete": not unknown_requests,
        "unknown_usage_request_numbers": unknown_requests,
        "account_cost": "not_reported_by_cli",
    }
    for field in fields:
        observed = sum(known[field])
        totals[field] = observed if not missing_by_field[field] else None
        totals["known_" + field + "_sum"] = observed
        totals["unknown_" + field + "_request_numbers"] = missing_by_field[field]
    latency_values = [
        attempt.get("usage", {}).get("latency_ms")
        for attempt in attempts
        if isinstance(attempt.get("usage"), dict)
    ]
    known_latency = [
        float(value) for value in latency_values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    totals["latency_ms"] = (
        round(sum(known_latency), 3)
        if len(known_latency) == len(attempts)
        else None
    )
    totals["known_latency_ms_sum"] = round(sum(known_latency), 3)
    return totals


def _load_frozen_task(preflight_path: Optional[Path] = None):
    source = _read_json(preflight_path or PREFLIGHT_PATH)
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
        actual_map_sha256 = hashlib.sha256(map_path.read_bytes()).hexdigest()
        if actual_map_sha256 != map_ref.get("sha256"):
            raise RuntimeError("frozen_map_reference_hash_mismatch")
        map_data = _read_json(map_path)
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
    evaluation_truth = source.get("evaluation_truth", {})
    task = VehicleOrderTask(
        task_id=task_public["task_id"],
        vehicle_id=task_public.get("vehicle_id", "r4-preflight"),
        order_id=task_public.get("order_id", task_public["task_id"]),
        start_node_id=task_public["start_node_id"],
        pickup_node_id=query_data["p"],
        dropoff_node_id=query_data["d"],
        pickup_text=task_public["pickup_text"],
        dropoff_text=task_public["dropoff_text"],
        task_type=task_public["task_type"],
        true_pickup_node_id=evaluation_truth.get("true_pickup_node_id", query_data["p"]),
        true_dropoff_node_id=evaluation_truth.get("true_dropoff_node_id", query_data["d"]),
    )
    blocked_edge_ids = tuple(source.get("route_search", {}).get("blocked_edge_ids", []))
    package = build_route_input_package(
        road_map, task, PackageConfig(), blocked_edge_ids=blocked_edge_ids
    )
    return source, road_map, task, package


def _load_shared_candidate_routes(pool_path: Optional[Path], package: Any, task: VehicleOrderTask):
    if pool_path is None:
        return [], None
    pool_path = Path(pool_path).expanduser().resolve()
    manifest_path = pool_path.parent / "manifest.json"
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != "r4_shared_candidate_pool_v2":
        raise RuntimeError("shared_candidate_pool_schema_mismatch")
    if manifest.get("candidate_pools_path") != pool_path.name:
        raise RuntimeError("shared_candidate_pool_path_mismatch")
    pool_sha = hashlib.sha256(pool_path.read_bytes()).hexdigest()
    if pool_sha != manifest.get("candidate_pools_sha256"):
        raise RuntimeError("shared_candidate_pool_hash_mismatch")
    edge_by_id = {edge.edge_id: edge for edge in package.edges}
    blocked = set(package.blocked_edge_ids)
    links = []
    with pool_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("task_id") != task.task_id or row.get("feasible") is not True:
                continue
            edge_ids = list(row.get("edge_sequence") or [])
            nodes = list(row.get("node_sequence") or [])
            if not edge_ids or len(nodes) != len(edge_ids) + 1:
                raise RuntimeError("shared_candidate_route_shape_invalid:" + str(row.get("route_id")))
            if nodes[0] != task.start_node_id or task.pickup_node_id not in nodes or task.dropoff_node_id not in nodes:
                raise RuntimeError("shared_candidate_route_endpoint_mismatch:" + str(row.get("route_id")))
            if nodes.index(task.pickup_node_id) >= nodes.index(task.dropoff_node_id) or len(nodes) != len(set(nodes)):
                raise RuntimeError("shared_candidate_route_order_or_revisit_invalid:" + str(row.get("route_id")))
            for index, edge_id in enumerate(edge_ids):
                edge = edge_by_id.get(edge_id)
                if edge is None or edge_id in blocked or edge.from_node != nodes[index] or edge.to_node != nodes[index + 1]:
                    raise RuntimeError("shared_candidate_route_edge_invalid:" + str(row.get("route_id")))
            links.append({
                "route_id": row.get("route_id"),
                "method_sources": row.get("method_sources"),
                "edge_sequence_sha256": row.get("edge_sequence_sha256"),
                "node_sequence": nodes,
                "edge_sequence": edge_ids,
                "travel_time_sec": row.get("travel_time_sec"),
                "route_distance_m": row.get("route_distance_m"),
                "energy_kwh": row.get("energy_kwh"),
                "energy_model_version": row.get("energy_model_version"),
            })
    if not links:
        raise RuntimeError("shared_candidate_pool_has_no_feasible_route_for_task")
    return links, pool_sha


def _input_protocol_flags(input_protocol: str):
    if input_protocol not in {"r4-step-v2", "r4-step-v3", "r4-step-v4-link-fallback"}:
        raise ValueError("route_input_protocol_invalid")
    enabled = input_protocol in {"r4-step-v3", "r4-step-v4-link-fallback"}
    return enabled, enabled


def _natural_simple_path_limit(package: Any) -> int:
    node_ids = {node.node_id for node in package.nodes}
    return max(1, len(node_ids) - 1)


def _is_formal_batch_task_id(task_id: str) -> bool:
    """Recognize both order tasks and formal-scenario tasks in the frozen matrix."""
    return task_id.startswith(("task-", "formal-task-"))


def _has_explicit_unbounded_authorization(state: Dict[str, Any]) -> bool:
    if not state.get("additional_model_requests_authorized", False):
        return False
    quote = str(state.get("authorization_quote") or "").casefold()
    return any(marker in quote for marker in (
        "不设置上限", "不设置额度上限", "无限制", "no limit", "unlimited"
    ))


def _apply_unbounded_route_budget(state: Dict[str, Any], package: Any) -> bool:
    """Replace an old per-task quota with the finite no-revisit graph bound."""
    if not _has_explicit_unbounded_authorization(state):
        return False
    natural_limit = _natural_simple_path_limit(package)
    prior = state.get("unbounded_authorization_applied")
    if (
        isinstance(prior, dict)
        and prior.get("natural_simple_path_limit") == natural_limit
        and state.get("authorized_new_request_limit") == natural_limit
        and state.get("effective_authorized_new_request_limit") == natural_limit
        and state.get("route_action_budget") == natural_limit
        and prior.get("runner_sha256") == LOADED_RUNNER_SHA256
    ):
        return False
    state["authorization_budget_overrides"] = state.get("authorization_budget_overrides", []) + [{
        "reason": "explicit_user_no_limit_authorization",
        "previous_authorized_request_limit": state.get(
            "initial_candidate_request_limit", state.get("authorized_new_request_limit")
        ),
        "previous_effective_authorized_new_request_limit": state.get("effective_authorized_new_request_limit"),
        "previous_route_action_budget": state.get(
            "frozen_route_action_budget", state.get("route_action_budget")
        ),
        "applied_at_utc": datetime.now(timezone.utc).isoformat(),
    }]
    state["authorized_new_request_limit"] = natural_limit
    state["effective_authorized_new_request_limit"] = natural_limit
    state["route_action_budget"] = natural_limit
    state["unbounded_authorization_applied"] = {
        "user_authorized": True,
        "natural_simple_path_limit": natural_limit,
        "derivation": "unique frozen graph nodes minus one; validator continues to forbid revisits",
        "runner_sha256": LOADED_RUNNER_SHA256,
    }
    return True


def _objective_progress_guidance(constraint: Any) -> Dict[str, Any]:
    return {
        "objective": "travel_time_then_distance",
        "current_lower_bound": constraint.remaining_objective_cost_lower_bound,
        "by_direction": {
            direction: constraint.objective_cost_lower_bound_by_direction[direction]
            for direction in ("E", "W", "N", "S")
            if direction in constraint.objective_cost_lower_bound_by_direction
        },
    }


def _initial_state(
    experiment_id: str = "r4_codex_independent_8x8_pilot_v1",
    authorized_request_limit: int = DEFAULT_NEW_CALLS,
    preflight_path: Optional[Path] = None,
    codex_session_mode: str = "ephemeral_per_request",
    input_protocol: str = "r4-step-v2",
    user_authorized_task9_experiment: bool = False,
    user_authorized_formal_batch_pilot: bool = False,
    shared_candidate_pool_path: Optional[Path] = None,
) -> Dict[str, Any]:
    if codex_session_mode not in {"ephemeral_per_request", "persistent_per_task"}:
        raise ValueError("codex_session_mode_invalid")
    if shared_candidate_pool_path and input_protocol != "r4-step-v4-link-fallback":
        raise ValueError("shared_candidate_pool_requires_r4_step_v4_link_fallback")
    if input_protocol == "r4-step-v4-link-fallback" and not shared_candidate_pool_path:
        raise ValueError("r4_step_v4_requires_shared_candidate_pool")
    compact_visited_state, enable_progress_guidance = _input_protocol_flags(input_protocol)
    authorized_request_limit = int(authorized_request_limit)
    source_path = Path(preflight_path or PREFLIGHT_PATH).resolve()
    source, road_map, task, package = _load_frozen_task(source_path)
    shared_candidate_routes, shared_candidate_pool_sha256 = _load_shared_candidate_routes(
        shared_candidate_pool_path, package, task
    )
    route_search = source.get("route_search", {})
    if route_search.get("generation_mode", "stepwise") != "stepwise":
        raise RuntimeError("frozen_preflight_is_not_stepwise")
    route_action_budget = int(route_search.get("max_route_actions", ROUTE_ACTION_BUDGET))
    frozen_route_action_budget = route_action_budget
    requested_candidate_request_limit = authorized_request_limit
    if route_action_budget < 1:
        raise RuntimeError("frozen_route_action_budget_invalid")
    natural_limit = _natural_simple_path_limit(package)
    if user_authorized_formal_batch_pilot or user_authorized_task9_experiment:
        authorized_request_limit = natural_limit
        route_action_budget = natural_limit
    maximum_requests = (
        natural_limit if road_map.size >= 100 else MAX_NEW_CALLS
    )
    if authorized_request_limit < 1 or authorized_request_limit > maximum_requests:
        raise ValueError("authorized_request_limit_out_of_range")
    node_ids = {node.node_id for node in package.nodes}
    if task.start_node_id not in node_ids:
        raise RuntimeError("frozen_start_endpoint_mismatch")
    full_constraint = build_r4_route_output_constraint(
        package, task.pickup_node_id, task.dropoff_node_id, allow_failed=False
    )
    pickup_seen = task.start_node_id == task.pickup_node_id
    initial_completion_budget = min(route_action_budget, authorized_request_limit)
    initial_constraint = build_r4_step_output_constraint(
        package,
        full_constraint,
        current_node_id=task.start_node_id,
        pickup_seen=pickup_seen,
        visited_node_ids=[task.start_node_id],
        remaining_action_budget=initial_completion_budget,
        include_objective_cost_guidance=enable_progress_guidance,
        require_suffix_witness=True,
    )
    initial_stop_reason = _authorized_call_budget_stop_reason(
        initial_constraint.remaining_minimum_moves, authorized_request_limit
    )
    source_payload = json.loads(source["route_search"]["messages"][-1]["content"])
    route_feature_context = source_payload.get("i")
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
        route_feature_context=route_feature_context,
        compact_visited_state=compact_visited_state,
        progress_guidance=(
            _objective_progress_guidance(initial_constraint)
            if enable_progress_guidance
            else None
        ),
        shared_candidate_routes=shared_candidate_routes if shared_candidate_pool_path else None,
    )
    preflight_payload = json.loads(preflight_messages[-1]["content"])
    initial_shared_links = preflight_payload.get("l")
    initial_link_routes = (
        initial_shared_links.get("routes", [])
        if isinstance(initial_shared_links, dict)
        else []
    )
    initial_link_routes_sha256 = hashlib.sha256(
        json.dumps(
            initial_link_routes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    initial_user_message_sha256 = hashlib.sha256(
        preflight_messages[-1]["content"].encode("utf-8")
    ).hexdigest()
    compact_map = preflight_payload["m"]
    map_audit = audit_r4_stepwise_map_equivalence(package, compact_map)
    if not map_audit["passed"] or map_audit["expected_directed_edge_count"] != len(package.edges):
        raise RuntimeError("frozen_map_equivalence_failed")
    if not initial_constraint.edge_by_direction and not initial_stop_reason:
        raise RuntimeError("no_legal_continuation_directions")
    state = {
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
        "source_preflight": str(source_path.relative_to(ROOT)) if source_path.is_relative_to(ROOT) else str(source_path),
        "source_preflight_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
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
        "frozen_route_action_budget": frozen_route_action_budget,
        "initial_candidate_request_limit": requested_candidate_request_limit,
        # The candidate limit is a safety ceiling, not permission to spend it.
        # Only the explicit task9 authorization flag activates new requests.
        "effective_authorized_new_request_limit": (
            authorized_request_limit if (user_authorized_task9_experiment or user_authorized_formal_batch_pilot) else 0
        ),
        "additional_model_requests_authorized": bool(user_authorized_task9_experiment or user_authorized_formal_batch_pilot),
        "current_authorization_status": (
            "user_authorized_fresh_formal_batch_pilot"
            if user_authorized_formal_batch_pilot
            else "user_authorized_fresh_task9_run"
            if user_authorized_task9_experiment
            else "not_authorized"
        ),
        "new_requests_used": 0,
        "codex_session_id": None,
        "codex_session_mode": codex_session_mode,
        "route_input_protocol": input_protocol,
        "compact_visited_state": compact_visited_state,
        "enable_progress_guidance": enable_progress_guidance,
        "suffix_witness_gate_version": "r4-suffix-witness-gate-v1",
        "suffix_witness_gate_enabled": True,
        "rejected_directions_by_node": {},
        "suffix_witness_prefix_audit": {
            "passed": True,
            "accepted_action_count": 0,
            "prefix_sha256": accepted_prefix_sha256([task.start_node_id], []),
            "model_calls": 0,
        },
        "retry_count": 0,
        "accepted_nodes": [task.start_node_id],
        "accepted_edges": [],
        "accepted_directions": [],
        "pickup_seen": pickup_seen,
        "route_action_budget": route_action_budget,
        "route_feature_context": route_feature_context,
        "shared_candidate_routes": shared_candidate_routes,
        "shared_candidate_pool_sha256": shared_candidate_pool_sha256,
        "initial_prompt_sha256": initial_user_message_sha256,
        "initial_prompt_shared_link_policy": (
            initial_shared_links.get("policy")
            if isinstance(initial_shared_links, dict)
            else None
        ),
        "initial_prompt_shared_link_version": (
            initial_shared_links.get("v")
            if isinstance(initial_shared_links, dict)
            else None
        ),
        "initial_prompt_shared_link_route_count": len(initial_link_routes),
        "initial_prompt_shared_link_routes_sha256": initial_link_routes_sha256,
        "attempts": [],
        "map_equivalence_audit": map_audit,
        "initial_continuation_allowed_directions": sorted(initial_constraint.edge_by_direction),
        "initial_remaining_minimum_moves": initial_constraint.remaining_minimum_moves,
        "scope": "Fresh stepwise Codex task on the exact frozen preflight; shared candidate links are exposed before the first request as fallback-only references; one direction per request; invalid moves do not advance state and trigger re-search from the last legal checkpoint.",
    }
    if user_authorized_task9_experiment:
        if road_map.size != 100 or task.task_id != "task-0000-0009":
            raise ValueError("task9_user_authorization_scope_mismatch")
        state.update({
            "user_authorization_status": "granted_for_task9_continuation",
            "authorization_scope": "complete the task9 route experiment only; no ten-seed batch",
            "authorization_quote": "完成全部这部分实验无限制！",
            "authorization_source": "direct_user_reply_2026-10-26; continued under latest v3 improvement request",
        })
    if user_authorized_formal_batch_pilot:
        if road_map.size != 100 or not task.task_id:
            raise ValueError("formal_batch_pilot_authorization_scope_mismatch")
        state.update({
            "user_authorization_status": "granted_for_formal_r4_batch_pilot",
            "authorization_scope": "complete all experiment-plan-required R4 route-layer experiments; omit unnecessary experiments; includes the required task9 zero-retry pilot and the frozen formal batch",
            "authorization_quote": "按实验方案完成全部需要的实验，不必要可以省略；不设置上限，减少不必要测试，尽快完成实验。",
            "authorization_source": "direct_user_replies_2026-10-27_current_thread; formal R4 batch and quota follow-up",
        })
    _apply_unbounded_route_budget(state, package)
    return state


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


_PREFIX_SUFFIX_RECOVERABLE_REASONS = {
    "suffix_witness_not_found",
    "current_prefix_has_no_certified_continuation",
    "action_budget_insufficient",
    "remaining_action_budget_insufficient",
}


def _recover_resume_prefix_to_last_certified_checkpoint(
    package: Any,
    full_constraint: Any,
    *,
    accepted_nodes: List[str],
    accepted_edges: List[str],
    accepted_directions: List[str],
    route_action_budget: int,
) -> Dict[str, Any]:
    """Keep the latest old checkpoint from which a full suffix is certified.

    Only suffix-feasibility failures are recoverable. Structural problems in
    an accepted edge remain fatal. Parent state and call history are read-only;
    the caller records the removed suffix in the new continuation state.
    """
    original_audit = audit_accepted_prefix_suffix_witnesses(
        package,
        full_constraint,
        accepted_nodes=list(accepted_nodes),
        accepted_edges=list(accepted_edges),
        accepted_directions=list(accepted_directions),
        route_action_budget=route_action_budget,
    )
    if original_audit.get("passed"):
        return {
            "passed": True,
            "accepted_nodes": list(accepted_nodes),
            "accepted_edges": list(accepted_edges),
            "accepted_directions": list(accepted_directions),
            "prefix_audit": original_audit,
            "rollback": None,
        }

    failure_reason = original_audit.get("reason")
    if failure_reason not in _PREFIX_SUFFIX_RECOVERABLE_REASONS:
        return {
            "passed": False,
            "reason": failure_reason,
            "failure_audit": original_audit,
            "rollback": None,
        }

    failed_step = original_audit.get("step")
    checkpoint_edge_count = (
        int(failed_step) - 1
        if isinstance(failed_step, int) and not isinstance(failed_step, bool)
        else len(accepted_edges) - 1
    )
    while checkpoint_edge_count >= 0:
        candidate_nodes = list(accepted_nodes[: checkpoint_edge_count + 1])
        candidate_edges = list(accepted_edges[:checkpoint_edge_count])
        candidate_directions = list(accepted_directions[:checkpoint_edge_count])
        candidate_audit = audit_accepted_prefix_suffix_witnesses(
            package,
            full_constraint,
            accepted_nodes=candidate_nodes,
            accepted_edges=candidate_edges,
            accepted_directions=candidate_directions,
            route_action_budget=route_action_budget,
        )
        if candidate_audit.get("passed"):
            rollback = {
                "event": "rollback_to_last_certified_checkpoint",
                "reason": failure_reason,
                "failure_step": failed_step,
                "original_action_count": len(accepted_edges),
                "original_prefix_sha256": accepted_prefix_sha256(
                    accepted_nodes, accepted_edges
                ),
                "checkpoint_action_count": checkpoint_edge_count,
                "checkpoint_node_id": candidate_nodes[-1],
                "checkpoint_prefix_sha256": candidate_audit.get("prefix_sha256"),
                "removed_action_count": len(accepted_edges) - checkpoint_edge_count,
                "removed_node_sequence": list(accepted_nodes[checkpoint_edge_count + 1 :]),
                "removed_edge_sequence": list(accepted_edges[checkpoint_edge_count:]),
                "removed_direction_sequence": list(
                    accepted_directions[checkpoint_edge_count:]
                ),
                "rejected_parent_action_replayed": False,
                "model_calls": 0,
            }
            return {
                "passed": True,
                "accepted_nodes": candidate_nodes,
                "accepted_edges": candidate_edges,
                "accepted_directions": candidate_directions,
                "prefix_audit": candidate_audit,
                "failure_audit": original_audit,
                "rollback": rollback,
            }

        candidate_reason = candidate_audit.get("reason")
        if candidate_reason not in _PREFIX_SUFFIX_RECOVERABLE_REASONS:
            return {
                "passed": False,
                "reason": candidate_reason,
                "failure_audit": original_audit,
                "checkpoint_failure_audit": candidate_audit,
                "rollback": None,
            }
        earlier_failed_step = candidate_audit.get("step")
        next_checkpoint = (
            int(earlier_failed_step) - 1
            if isinstance(earlier_failed_step, int)
            and not isinstance(earlier_failed_step, bool)
            else checkpoint_edge_count - 1
        )
        checkpoint_edge_count = min(checkpoint_edge_count - 1, next_checkpoint)

    return {
        "passed": False,
        "reason": "no_certified_resume_checkpoint",
        "failure_audit": original_audit,
        "rollback": None,
    }


def _resume_state(
    parent_dir: Path,
    allow_unexecuted_self_check_mismatch: bool = False,
    authorized_request_limit: int = MAX_NEW_CALLS,
    preflight_path: Optional[Path] = None,
    codex_session_mode: str = "ephemeral_per_request",
    user_authorized_user_stop_resume: bool = False,
    input_protocol: Optional[str] = None,
    user_authorized_formal_task_resume: bool = False,
) -> Dict[str, Any]:
    """Start a separately budgeted continuation from a validated pilot prefix."""
    if codex_session_mode not in {"ephemeral_per_request", "persistent_per_task"}:
        raise ValueError("codex_session_mode_invalid")
    parent_dir = Path(parent_dir).resolve()
    parent_state_path = parent_dir / "state.json"
    parent_audit_path = parent_dir / "final_audit.json"
    if not parent_state_path.is_file() or not parent_audit_path.is_file():
        raise RuntimeError("resume_parent_artifacts_missing")
    parent = _read_json(parent_state_path)
    parent_audit = _read_json(parent_audit_path)
    if user_authorized_formal_task_resume and (
        parent.get("user_authorization_status") != "granted_for_formal_r4_batch_pilot"
    ):
        raise RuntimeError("resume_formal_batch_authorization_missing")
    if user_authorized_formal_task_resume and user_authorized_user_stop_resume:
        raise RuntimeError("resume_authorization_mode_conflict")
    parent_input_protocol = parent.get("route_input_protocol", "r4-step-v2")
    selected_input_protocol = input_protocol or parent_input_protocol
    if selected_input_protocol != parent_input_protocol:
        raise RuntimeError("resume_input_protocol_switch_forbidden")
    compact_visited_state, enable_progress_guidance = _input_protocol_flags(
        selected_input_protocol
    )
    parent_preflight = Path(parent.get("source_preflight") or PREFLIGHT_PATH)
    if not parent_preflight.is_absolute():
        parent_preflight = ROOT / parent_preflight
    selected_preflight = Path(preflight_path) if preflight_path else parent_preflight
    if not selected_preflight.is_absolute():
        selected_preflight = ROOT / selected_preflight
    if not selected_preflight.is_file():
        raise RuntimeError("resume_preflight_missing")
    if (
        parent.get("source_preflight_sha256")
        and hashlib.sha256(selected_preflight.read_bytes()).hexdigest()
        != parent["source_preflight_sha256"]
    ):
        raise RuntimeError("resume_preflight_hash_mismatch")
    safe_self_check_resume = (
        allow_unexecuted_self_check_mismatch
        and _safe_unexecuted_self_check_mismatch_parent(parent, parent_audit)
    )
    unresolved_parent_request_numbers = (
        _safe_unresolved_delivery_resume_parent(parent, parent_audit)
        if user_authorized_user_stop_resume or user_authorized_formal_task_resume
        else []
    )
    provider_rejected_parent_request_numbers = (
        _safe_provider_rejection_resume_parent(parent, parent_audit)
        if user_authorized_formal_task_resume
        else []
    )
    safe_unresolved_delivery_resume = bool(unresolved_parent_request_numbers)
    safe_provider_rejection_resume = bool(provider_rejected_parent_request_numbers)
    quota_guard = parent.get("account_usage_guard") or {}
    if not isinstance(quota_guard, dict):
        quota_guard = {}
    clean_quota_stop = (
        parent.get("status") == "quota_cap_reached"
        and bool(quota_guard.get("stop_reason"))
        and quota_guard.get("requests_used_when_stopped") == parent.get("new_requests_used")
        and quota_guard.get("accepted_route_steps_when_stopped") == len(parent.get("accepted_edges", []))
        and quota_guard.get("baseline_weekly_used_percent") is not None
        and quota_guard.get("observed_weekly_used_percent_after_request") is not None
        and not parent_audit.get("route_complete", False)
    )
    clean_budget_stop = parent.get("status") in {
        "authorized_call_limit_exhausted",
        "insufficient_authorized_budget_for_remaining_route",
    } or clean_quota_stop or (
        (user_authorized_user_stop_resume or user_authorized_formal_task_resume)
        and parent.get("status") == "stopped_by_user_instruction"
    ) or safe_unresolved_delivery_resume or safe_provider_rejection_resume
    if not clean_budget_stop and not safe_self_check_resume:
        if allow_unexecuted_self_check_mismatch:
            raise RuntimeError("resume_self_check_mismatch_parent_not_safe")
        raise RuntimeError("resume_parent_not_cleanly_capped")
    if parent.get("retry_count") != 0:
        raise RuntimeError("resume_parent_call_contract_mismatch")
    if (
        not safe_self_check_resume
        and parent.get("status") == "authorized_call_limit_exhausted"
        and parent.get("new_requests_used") != parent.get("authorized_new_request_limit")
    ):
        raise RuntimeError("resume_parent_call_contract_mismatch")
    accepted_attempts = [
        item for item in parent.get("attempts", [])
        if item.get("status") == "accepted_legal_step"
    ]
    if (user_authorized_user_stop_resume or user_authorized_formal_task_resume) and parent.get("status") in {
        "stopped_by_user_instruction", "stopped_outcome_unknown_no_retry"
    }:
        if (
            len(parent.get("attempts", [])) != int(parent.get("new_requests_used", 0))
            or any(item.get("status") == "request_in_progress" for item in parent.get("attempts", []))
        ):
            raise RuntimeError("resume_user_stopped_parent_has_inflight_or_unaccounted_request")
    expected_accepted_attempts = (
        int(parent.get("new_requests_used", 0))
        - int(safe_self_check_resume)
        - len(unresolved_parent_request_numbers)
        - len(provider_rejected_parent_request_numbers)
    )
    if len(accepted_attempts) != expected_accepted_attempts or any(
        item.get("tool_item_types") or item.get("retry_count") != 0
        for item in accepted_attempts
    ):
        raise RuntimeError("resume_parent_contains_invalid_or_tool_action")
    # `new_requests_used` counts CLI invocations. A pre-turn interruption is a
    # real invocation in the ledger but not an observed model turn, so compare
    # against the invocation count and keep model-turn counts separate.
    audited_request_count = parent_audit.get(
        "codex_cli_invocations_this_continuation",
        parent_audit.get("actual_model_requests_this_continuation", parent_audit.get("requests")),
    )
    if audited_request_count != parent.get("new_requests_used"):
        raise RuntimeError("resume_parent_audit_call_count_mismatch")
    if (
        parent_audit.get("retries") != 0
        or (
            parent_audit.get("tool_events") != 0
            and not (
                safe_unresolved_delivery_resume
                and parent_audit.get("tool_events") is None
                and parent_audit.get("tool_audit_complete") is False
                and set(parent_audit.get("tool_audit_unknown_request_numbers", []))
                == set(unresolved_parent_request_numbers)
            )
            and not (
                safe_provider_rejection_resume
                and parent_audit.get("tool_events") is None
                and parent_audit.get("tool_audit_complete") is False
            )
        )
    ):
        raise RuntimeError("resume_parent_audit_tool_or_retry_mismatch")

    _, road_map, task, package = _load_frozen_task(selected_preflight)
    natural_limit = _natural_simple_path_limit(package)
    unbounded_resume = _has_explicit_unbounded_authorization(parent)
    if unbounded_resume:
        authorized_request_limit = natural_limit
    maximum_requests = natural_limit if road_map.size >= 100 else MAX_NEW_CALLS
    if authorized_request_limit < 1 or authorized_request_limit > maximum_requests:
        raise ValueError("resume_request_limit_out_of_range")
    if (
        parent.get("map_id") != road_map.map_id
        or parent.get("task_id") != task.task_id
        or parent.get("pickup_node_id") != package.pickup_node_id
        or parent.get("dropoff_node_id") != package.dropoff_node_id
        or parent_audit.get("map_id") != road_map.map_id
    ):
        raise RuntimeError("resume_parent_task_or_map_mismatch")
    if user_authorized_user_stop_resume and parent.get("status") in {
        "stopped_by_user_instruction", "stopped_outcome_unknown_no_retry"
    }:
        if road_map.size != 100 or task.task_id != "task-0000-0009":
            raise RuntimeError("resume_user_stopped_parent_scope_mismatch")
    if user_authorized_formal_task_resume and (
        road_map.size != 100
        or not _is_formal_batch_task_id(task.task_id)
        or selected_input_protocol not in {"r4-step-v3", "r4-step-v4-link-fallback"}
    ):
        raise RuntimeError("resume_formal_batch_parent_scope_mismatch")

    resume_nodes = list(parent["accepted_nodes"])
    resume_edges = list(parent["accepted_edges"])
    resume_directions = list(parent["accepted_directions"])
    full_constraint = build_r4_route_output_constraint(
        package, parent["pickup_node_id"], parent["dropoff_node_id"], allow_failed=False
    )
    route_action_total = (
        natural_limit if unbounded_resume
        else int(parent.get("route_action_budget", ROUTE_ACTION_BUDGET))
    )
    prefix_recovery = _recover_resume_prefix_to_last_certified_checkpoint(
        package,
        full_constraint,
        accepted_nodes=resume_nodes,
        accepted_edges=resume_edges,
        accepted_directions=resume_directions,
        route_action_budget=route_action_total,
    )
    if not prefix_recovery.get("passed"):
        failure_audit = prefix_recovery.get("failure_audit") or {}
        raise RuntimeError(
            "resume_parent_suffix_witness_audit_failed:{0}".format(
                prefix_recovery.get("reason") or failure_audit.get("reason")
            )
        )
    resume_nodes = prefix_recovery["accepted_nodes"]
    resume_edges = prefix_recovery["accepted_edges"]
    resume_directions = prefix_recovery["accepted_directions"]
    rollback_event = prefix_recovery.get("rollback")
    current = resume_nodes[-1]
    route_action_budget = route_action_total - len(resume_edges)
    resume_completion_budget = min(authorized_request_limit, route_action_budget)
    parent_prefix_audit = prefix_recovery["prefix_audit"]
    rejected_directions_by_node = copy.deepcopy(
        parent.get("rejected_directions_by_node", {})
    )
    if rollback_event:
        rejected_directions_by_node.pop(current, None)
    step_constraint = build_r4_step_output_constraint(
        package,
        full_constraint,
        current_node_id=current,
        pickup_seen=package.pickup_node_id in resume_nodes,
        visited_node_ids=resume_nodes,
        remaining_action_budget=resume_completion_budget,
        include_objective_cost_guidance=enable_progress_guidance,
        require_suffix_witness=True,
        rejected_directions=rejected_directions_by_node.get(current, ()),
    )
    resume_stop_reason = _authorized_call_budget_stop_reason(
        step_constraint.remaining_minimum_moves, authorized_request_limit
    )
    if not step_constraint.edge_by_direction and not resume_stop_reason:
        no_action_reason = _no_action_stop_status(step_constraint)
        if no_action_reason != "local_pre_request_no_allowed_action":
            raise RuntimeError("resume_parent_" + no_action_reason)
        raise RuntimeError("resume_parent_has_no_legal_continuation")
    messages = build_r4_stepwise_messages(
        package,
        parent["start_node_id"],
        parent["pickup_node_id"],
        parent["dropoff_node_id"],
        current_node_id=current,
        visited_node_ids=resume_nodes,
        pickup_seen=package.pickup_node_id in resume_nodes,
        accepted_directions=resume_directions,
        remaining_action_budget=resume_completion_budget,
        route_feature_context=parent.get("route_feature_context"),
        compact_visited_state=compact_visited_state,
        progress_guidance=(
            _objective_progress_guidance(step_constraint)
            if enable_progress_guidance
            else None
        ),
        shared_candidate_routes=parent.get("shared_candidate_routes") if parent.get("shared_candidate_pool_sha256") else None,
    )
    compact_map = json.loads(messages[-1]["content"])["m"]
    map_audit = audit_r4_stepwise_map_equivalence(package, compact_map)
    if not map_audit["passed"]:
        raise RuntimeError("resume_map_equivalence_failed")

    state = copy.deepcopy(parent)
    state["accepted_nodes"] = list(resume_nodes)
    state["accepted_edges"] = list(resume_edges)
    state["accepted_directions"] = list(resume_directions)
    state["pickup_seen"] = package.pickup_node_id in resume_nodes
    state["suffix_witness_gate_version"] = "r4-suffix-witness-gate-v1"
    state["suffix_witness_gate_enabled"] = True
    state["rejected_directions_by_node"] = rejected_directions_by_node
    state["resume_prefix_rollbacks"] = copy.deepcopy(
        parent.get("resume_prefix_rollbacks", [])
    )
    if rollback_event:
        rollback_event["cleared_rejected_directions_at_checkpoint"] = copy.deepcopy(
            parent.get("rejected_directions_by_node", {}).get(current, [])
        )
        rollback_event["parent_state_sha256"] = hashlib.sha256(
            parent_state_path.read_bytes()
        ).hexdigest()
        state["resume_prefix_rollbacks"].append(rollback_event)
    parent_account_usage_guard = state.pop("account_usage_guard", None)
    parent_usage = parent.get("usage_totals", {})
    parent_experiment_id = str(parent.get("experiment_id", ""))
    parent_prefix, version_separator, parent_version = parent_experiment_id.rpartition("_v")
    if version_separator and parent_version.isdigit():
        resumed_experiment_id = "{0}_v{1}".format(
            parent_prefix, int(parent_version) + 1
        )
    else:
        resumed_experiment_id = parent_experiment_id + "_continued"
    # Never inherit a parent session into a new continuation. The default mode
    # uses one isolated CLI session per step; an explicitly selected persistent
    # mode starts its own fresh per-task session on the first step and records
    # that new session ID for subsequent state-only updates.
    state.update({
        "schema_version": "r4_codex_pilot_continuation_state_v2",
        "experiment_id": resumed_experiment_id,
        "status": resume_stop_reason or "ready",
        "stop_diagnostics": (
            {
                "minimum_remaining_moves": step_constraint.remaining_minimum_moves,
                "unused_authorized_calls": authorized_request_limit,
                "reason": resume_stop_reason,
            }
            if resume_stop_reason
            else None
        ),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "parent_run": str(parent_dir.relative_to(ROOT)) if parent_dir.is_relative_to(ROOT) else str(parent_dir),
        "parent_state_sha256": hashlib.sha256(parent_state_path.read_bytes()).hexdigest(),
        "parent_final_audit_sha256": hashlib.sha256(parent_audit_path.read_bytes()).hexdigest(),
        # Keep the immediate parent's own request count separate from the
        # cumulative chain count. The former is a run-local manifest field;
        # the latter labels new calls correctly across nested continuations.
        "parent_requests": int(parent.get("new_requests_used", 0)),
        "cumulative_parent_requests": int(
            parent_audit.get("cumulative_requests_including_bootstrap_and_parent")
            if isinstance(parent_audit.get("cumulative_requests_including_bootstrap_and_parent"), int)
            else parent.get("new_requests_used", 0)
        ),
        "cumulative_parent_cli_invocations": int(
            parent_audit.get(
                "cumulative_cli_invocations_including_bootstrap_and_parent",
                int(parent_audit.get("cumulative_requests_including_bootstrap_and_parent", 0) or 0)
                + max(
                    0,
                    int(parent_audit.get("codex_cli_invocations_this_continuation", 0) or 0)
                    - int(parent_audit.get("actual_model_requests_this_continuation", 0) or 0),
                ),
            )
        ),
        "parent_input_tokens": parent_usage.get("input_tokens"),
        "parent_output_tokens": parent_usage.get("output_tokens"),
        "parent_latency_ms": parent_usage.get("latency_ms"),
        "authorized_new_request_limit": authorized_request_limit,
        # Do not inherit effective permission from the parent. A resume gets
        # fresh authority only when the explicit user-stop-resume flag is set.
        "effective_authorized_new_request_limit": (
            authorized_request_limit
            if user_authorized_user_stop_resume or user_authorized_formal_task_resume
            else 0
        ),
        "route_action_budget": route_action_total,
        "additional_model_requests_authorized": bool(
            user_authorized_user_stop_resume or user_authorized_formal_task_resume
        ),
        "current_authorization_status": (
            "user_authorized_formal_task_resume"
            if user_authorized_formal_task_resume
            else "user_authorized_resume"
            if user_authorized_user_stop_resume
            else "not_authorized"
        ),
        "new_requests_used": 0,
        "retry_count": 0,
        "codex_session_id": None,
        "codex_session_mode": codex_session_mode,
        "route_input_protocol": selected_input_protocol,
        "compact_visited_state": compact_visited_state,
        "enable_progress_guidance": enable_progress_guidance,
        "suffix_witness_gate_version": "r4-suffix-witness-gate-v1",
        "suffix_witness_gate_enabled": True,
        "rejected_directions_by_node": rejected_directions_by_node,
        "suffix_witness_prefix_audit": parent_prefix_audit,
        "attempts": [],
        "map_equivalence_audit": map_audit,
        "initial_continuation_allowed_directions": sorted(step_constraint.edge_by_direction),
        "initial_remaining_minimum_moves": step_constraint.remaining_minimum_moves,
        "scope": (
            "Continuation of the frozen formal R4 task from its validator-accepted legal prefix; "
            "up to {0} new one-step requests; unresolved parent invocations remain separate and are not replayed."
        ).format(authorized_request_limit)
        if user_authorized_formal_task_resume
        else "Continuation of the independent Codex {0}x{0} pilot from its accepted legal prefix; up to {1} new one-step requests; separate from formal route comparison.".format(
            road_map.size, authorized_request_limit
        ),
        "source_preflight": str(selected_preflight.relative_to(ROOT)) if selected_preflight.is_relative_to(ROOT) else str(selected_preflight),
        "source_preflight_sha256": hashlib.sha256(selected_preflight.read_bytes()).hexdigest(),
        "parent_account_usage_guard": parent_account_usage_guard,
        "unresolved_parent_request_numbers_not_replayed": unresolved_parent_request_numbers,
        "provider_rejected_parent_request_numbers_not_replayed": provider_rejected_parent_request_numbers,
        "provider_rejected_parent_usage_unknown_request_numbers": provider_rejected_parent_request_numbers,
        **(
            {
                "user_authorization_status": "granted_for_task9_continuation",
                "authorization_scope": (
                    "continue only the frozen seed-0 task9 route from its accepted prefix; "
                    "do not replay unresolved parent calls; no ten-seed batch"
                ),
                "authorization_quote": "直接完成这个实验快一点，要符合实验依据要",
                "authorization_source": "direct_user_goal_continuation_2026-10-27",
            }
            if user_authorized_user_stop_resume
            else {}
        ),
        **(
            {
                "user_authorization_status": "granted_for_formal_r4_batch_pilot",
                "authorization_scope": (
                    "continue the frozen formal R4 task from its last validator-accepted prefix; "
                    "preserve unresolved parent invocations without replay; no project-level request cap"
                ),
                "authorization_quote": (
                    "直接完成这个实验快一点，要符合实验依据要；不设置额度上限"
                ),
                "authorization_source": "direct_user_goal_continuation_2026-10-27",
            }
            if user_authorized_formal_task_resume
            else {}
        ),
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
        **(
            {
                "resume_reason": "confirmed_provider_rate_limit_or_capacity_rejection",
                "rejected_parent_action_replayed": False,
                "provider_rejection_usage_remains_unknown": True,
            }
            if safe_provider_rejection_resume
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
    _apply_unbounded_route_budget(state, package)
    state["route_metrics"] = _route_summary(state, road_map)
    return state


def _safe_unresolved_delivery_resume_parent(
    parent: Dict[str, Any], parent_audit: Dict[str, Any]
) -> List[int]:
    """Return fully audited unknown invocations eligible for a fresh continuation.

    Unknown calls remain in their parent run and are never replayed. Only the
    validator-accepted route prefix is inherited by the new continuation.
    """
    if parent.get("status") not in {
        "stopped_outcome_unknown_no_retry",
        "stopped_by_user_instruction",
    }:
        return []
    attempts = parent.get("attempts", [])
    unknown_attempts = [
        item for item in attempts
        if item.get("status") == "delivery_and_usage_unknown"
    ]
    unknown_request_numbers = [item.get("request_number") for item in unknown_attempts]
    unknown_request_numbers = sorted(
        number for number in unknown_request_numbers if isinstance(number, int)
    )
    invocation_count = parent_audit.get(
        "codex_cli_invocations_this_continuation",
        parent_audit.get("actual_model_requests_this_continuation"),
    )
    if (
        not attempts
        or len(attempts) != int(parent.get("new_requests_used", 0) or 0)
        or not unknown_attempts
        or any(
            item.get("status") not in {"accepted_legal_step", "delivery_and_usage_unknown"}
            for item in attempts
        )
        or parent.get("retry_count") != 0
        or parent_audit.get("retries") != 0
        or invocation_count != parent.get("new_requests_used")
        or parent_audit.get("actual_model_requests_this_continuation", 0)
        > parent.get("new_requests_used", 0)
        or parent_audit.get("route_continuity_violations")
        or parent_audit.get("status") != "partial_legal_prefix_not_complete"
        or parent_audit.get("additional_model_requests_authorized") is not False
        or parent_audit.get("delivery_unknown_request_numbers") != unknown_request_numbers
        or parent_audit.get("usage_unknown_request_numbers") != unknown_request_numbers
        or parent_audit.get("tool_audit_unknown_request_numbers") != unknown_request_numbers
        or parent_audit.get("tool_events") is not None
        or parent_audit.get("tool_audit_complete") is not False
        or parent_audit.get("accepted_move_count_including_prior_smoke")
        != len(parent.get("accepted_edges", []))
        or parent_audit.get("current_node")
        != (parent.get("accepted_nodes") or [None])[-1]
    ):
        return []
    accepted_nodes = parent.get("accepted_nodes") or []
    for unresolved in unknown_attempts:
        request_number = unresolved.get("request_number")
        provider = unresolved.get("provider_record", {})
        current_node = unresolved.get("current_node_id")
        route_action_executed = unresolved.get(
            "route_action_executed", unresolved.get("action_executed")
        )
        if (
            not isinstance(request_number, int)
            or unresolved.get("response") is not None
            or route_action_executed is not False
            or unresolved.get("retry_count") != 0
            or not provider.get("delivery_and_usage_unknown_evidence")
            or provider.get("status") not in {
                "interrupted_before_turn_completion",
                "delivery_and_usage_unknown",
            }
            or current_node not in accepted_nodes
            or unresolved.get("step_number") != accepted_nodes.index(current_node) + 1
        ):
            return []
    return unknown_request_numbers


def _safe_provider_rejection_resume_parent(
    parent: Dict[str, Any], parent_audit: Dict[str, Any]
) -> List[int]:
    """Return a single, trace-verified provider rejection eligible for resuming.

    The rejected invocation stays in its original run and its usage remains
    unknown. A continuation may inherit only the already validator-accepted
    route prefix; the rejected invocation is never replayed.
    """
    if parent.get("status") != "stopped_request_error":
        return []
    attempts = parent.get("attempts", [])
    request_count = int(parent.get("new_requests_used", 0) or 0)
    if (
        not attempts
        or len(attempts) != request_count
        or parent.get("retry_count") != 0
        or parent_audit.get("retries") != 0
        or parent_audit.get("status") != "partial_legal_prefix_not_complete"
        or parent_audit.get("route_complete") is True
        or parent_audit.get("route_continuity_violations")
        or parent_audit.get("actual_model_requests_this_continuation") != request_count
        or parent_audit.get("codex_cli_invocations_this_continuation") != request_count
        or parent_audit.get("accepted_move_count_including_prior_smoke")
        != len(parent.get("accepted_edges", []))
        or parent_audit.get("current_node")
        != (parent.get("accepted_nodes") or [None])[-1]
    ):
        return []
    for prior in attempts[:-1]:
        provider = prior.get("provider_record") or {}
        event_audit = provider.get("event_audit") or {}
        if (
            prior.get("status") != "accepted_legal_step"
            or prior.get("retry_count") != 0
            or prior.get("tool_item_types") != []
            or event_audit.get("tool_usage_detected") is not False
            or event_audit.get("tool_item_types") != []
        ):
            return []

    failed = attempts[-1]
    provider = failed.get("provider_record") or {}
    event_audit = provider.get("event_audit") or {}
    if (
        failed.get("status") != "stopped_request_error"
        or failed.get("request_number") != request_count
        or failed.get("retry_count") != 0
        or failed.get("tool_item_types") != []
        or failed.get("response") is not None
        or failed.get("validation") is not None
        or any(failed.get("usage", {}).get(key) is not None for key in (
            "input_tokens", "cached_input_tokens", "output_tokens"
        ))
        or provider.get("backend") != "codex_cli"
        or provider.get("status") != "cli_error"
        or provider.get("cli_exit_code") != 1
        or provider.get("retry_count") != 0
        or provider.get("canonical_final_json_present") is not False
        or provider.get("agent_message_count") != 0
        or provider.get("usage_available") is not False
        or event_audit.get("tool_usage_detected") is not False
        or event_audit.get("tool_item_types") != []
        or event_audit.get("item_types") != []
        or event_audit.get("usage_event_count") != 0
    ):
        return []

    trace_path_value = provider.get("trace_jsonl_path")
    trace_sha256 = provider.get("trace_jsonl_sha256")
    if not trace_path_value or not trace_sha256:
        return []
    trace_path = Path(trace_path_value)
    try:
        trace_bytes = trace_path.read_bytes()
        if hashlib.sha256(trace_bytes).hexdigest() != trace_sha256:
            return []
        rows = [json.loads(line) for line in trace_bytes.decode("utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    event_types = [row.get("type") for row in rows]
    if event_types != ["thread.started", "turn.started", "error", "turn.failed"]:
        return []
    if event_audit.get("event_types") != event_types:
        return []
    error_messages = []
    for row in rows:
        if row.get("type") == "error":
            error_messages.append(row.get("message", ""))
        elif row.get("type") == "turn.failed":
            error = row.get("error") or {}
            error_messages.append(error.get("message", "") if isinstance(error, dict) else str(error))
    if not error_messages or not all(
        "429 Too Many Requests" in message
        or "Selected model is at capacity" in message
        for message in error_messages
    ):
        return []
    return [request_count]


def preflight_only(
    resume_from: Path = None,
    allow_unexecuted_self_check_mismatch: bool = False,
    experiment_id: str = None,
    authorized_request_limit: int = MAX_NEW_CALLS,
    preflight_path: Path = None,
    codex_session_mode: str = "ephemeral_per_request",
    user_authorized_user_stop_resume: bool = False,
    input_protocol: Optional[str] = None,
    user_authorized_task9_experiment: bool = False,
    user_authorized_formal_batch_pilot: bool = False,
    user_authorized_formal_task_resume: bool = False,
    shared_candidate_pool_path: Optional[Path] = None,
) -> None:
    if OUTPUT_DIR.exists():
        raise FileExistsError("continuation_output_already_exists")
    if user_authorized_formal_task_resume and not resume_from:
        raise ValueError("formal_task_resume_requires_resume_preflight")
    if resume_from and experiment_id:
        raise ValueError("experiment_id_override_only_supported_for_fresh_run")
    if resume_from and (user_authorized_task9_experiment or user_authorized_formal_batch_pilot):
        raise ValueError("fresh_authorization_flag_cannot_be_used_on_resume")
    if resume_from and shared_candidate_pool_path:
        raise ValueError("shared_candidate_pool_cannot_be_changed_on_resume")
    state = (
        _resume_state(
            resume_from,
            allow_unexecuted_self_check_mismatch,
            authorized_request_limit,
            preflight_path,
            codex_session_mode,
            user_authorized_user_stop_resume,
            input_protocol,
            user_authorized_formal_task_resume,
        )
        if resume_from
        else _initial_state(
            experiment_id or "r4_codex_independent_8x8_pilot_v1",
            authorized_request_limit,
            preflight_path,
            codex_session_mode,
            input_protocol or "r4-step-v2",
            user_authorized_task9_experiment,
            user_authorized_formal_batch_pilot,
            shared_candidate_pool_path,
        )
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=False)
    (OUTPUT_DIR / "traces").mkdir()
    _write_json(OUTPUT_DIR / "state.json", state)
    manifest = {
        "schema_version": "r4_codex_pilot_continuation_manifest_v3",
        "experiment_id": state["experiment_id"],
        "status": "offline_preflight_passed_no_model_request",
        "user_authorization_status": state.get("user_authorization_status", "not_granted"),
        "current_authorization_status": state.get("current_authorization_status", "not_authorized"),
        "additional_model_requests_authorized": state.get("additional_model_requests_authorized", False),
        "effective_authorized_new_request_limit": state.get("effective_authorized_new_request_limit", 0),
        "authorization_scope": state.get("authorization_scope"),
        "authorization_quote": state.get("authorization_quote"),
        "authorization_source": state.get("authorization_source"),
        "request_limit_semantics": (
            "user_authorized_unbounded_graph_derived_simple_path_bound"
            if state.get("unbounded_authorization_applied")
            else "user_authorized_formal_batch_pilot_candidate_safety_cap"
            if state.get("user_authorization_status") == "granted_for_formal_r4_batch_pilot"
            else
            "user_authorized_task9_candidate_safety_cap"
            if state.get("user_authorization_status") == "granted_for_task9_continuation"
            else "candidate_safety_cap_not_user_authorization"
        ),
        "candidate_run_limits": (
            "No artificial per-task request/action quota; maximum is unique frozen graph nodes minus one under no-revisit validation. Each offered direction requires a complete suffix witness. Invalid moves do not advance state and are re-searched from the last legal node with that direction excluded; zero CLI retries; stop on tool event, give_up, or call error."
            if state.get("unbounded_authorization_applied")
            else "formal-batch-authorized R4 task: at most {0} one-step requests under the protocol's per-task candidate ceiling; every offered direction needs a complete suffix witness; invalid moves are excluded and re-searched from the last legal node; zero CLI retries; stop on tool event, give_up, or call error"
            if state.get("user_authorization_status") == "granted_for_formal_r4_batch_pilot"
            else "user-authorized task9-only pilot: at most {0} one-step requests under this candidate safety cap; every offered direction needs a complete suffix witness; invalid moves are excluded and re-searched from the last legal node; zero CLI retries; stop on tool event, give_up, or call error"
            if state.get("user_authorization_status") == "granted_for_task9_continuation"
            else "if separately authorized: up to {0} sequential one-step requests; every offered direction needs a complete suffix witness; invalid moves are excluded and re-searched from the last legal node; zero CLI retries; stop on tool event, give_up, or call error"
        ).format(state["authorized_new_request_limit"]),
        "candidate_request_limit": state["authorized_new_request_limit"],
        "model": MODEL,
        "reasoning_effort": "low",
        "route_action_budget": state["route_action_budget"],
        "route_action_budget_is_independent_of_candidate_request_limit": True,
        "candidate_request_limit_is_separate_from_route_action_budget": True,
        "api_calls_made_by_this_preflight": 0,
        "retry_count": 0,
        "codex_cli_session_mode": state.get("codex_session_mode", "ephemeral_per_request"),
        "route_input_protocol": state.get("route_input_protocol", "r4-step-v2"),
        "shared_candidate_pool_sha256": state.get("shared_candidate_pool_sha256"),
        "shared_candidate_route_count": len(state.get("shared_candidate_routes") or []),
        "compact_visited_state": bool(state.get("compact_visited_state", False)),
        "objective_progress_guidance": bool(state.get("enable_progress_guidance", False)),
        "suffix_witness_gate_version": state.get("suffix_witness_gate_version"),
        "suffix_witness_gate_enabled": bool(state.get("suffix_witness_gate_enabled")),
        "suffix_witness_prefix_audit": state.get("suffix_witness_prefix_audit"),
        "codex_cli_session_storage": (
            "one isolated Codex CLI conversation per route task; session ID stored in local state"
            if state.get("codex_session_mode") == "persistent_per_task"
            else "one ephemeral Codex CLI process per request; no conversation session is persisted"
        ),
        "per_request_route_schema": "dynamic direction enum reapplied on every initial or resumed CLI invocation",
        "direction_schema_completion_budget": "minimum of remaining route action budget and remaining calls within the candidate limit",
        "per_step_map_payload": (
            "full compact map only on initial request; later requests send q/s state and current dynamic directions"
            if state.get("codex_session_mode") == "persistent_per_task"
            else "full compact map and current q/s state are sent on every isolated request"
        ),
        "per_step_resume_fields": (
            ["q", "s", "allowed_directions", "r", "f", "l"]
            if state.get("enable_progress_guidance")
            else ["q", "s", "allowed_directions", "f", "l"]
        ),
        "initial_state": {
            "current_node": state["accepted_nodes"][-1],
            "visited_nodes": state["accepted_nodes"],
            "pickup_node": state["pickup_node_id"],
            "dropoff_node": state["dropoff_node_id"],
            "allowed_directions": state["initial_continuation_allowed_directions"],
            "minimum_remaining_moves": state["initial_remaining_minimum_moves"],
            "route_action_budget_remaining": state["route_action_budget"] - len(state["accepted_edges"]),
            "candidate_requests_in_this_continuation": state["authorized_new_request_limit"],
            "can_complete_with_candidate_request_limit": (
                state["initial_remaining_minimum_moves"] <= state["authorized_new_request_limit"]
            ),
            "early_stop_if_remaining_route_exceeds_unused_candidate_calls": True,
            "suffix_witness_gate_version": state.get("suffix_witness_gate_version"),
        },
        "parent_run": state.get("parent_run"),
        "parent_requests": state.get("parent_requests", 0),
        "cumulative_parent_requests": state.get(
            "cumulative_parent_requests", state.get("parent_requests", 0)
        ),
        "cumulative_parent_cli_invocations": state.get(
            "cumulative_parent_cli_invocations",
            state.get("cumulative_parent_requests", state.get("parent_requests", 0)),
        ),
        "parent_requests_already_spent": state.get(
            "cumulative_parent_requests", state.get("parent_requests", 0)
        ),
        "resume_reason": state.get("resume_reason"),
        "rejected_parent_action_replayed": state.get("rejected_parent_action_replayed", False),
        "map_equivalence_audit": state["map_equivalence_audit"],
        "task_adjustment": state.get("task_adjustment"),
        "source_preflight_sha256": state["source_preflight_sha256"],
        "prior_smoke_result_sha256": state.get("prior_smoke_result_sha256"),
    }
    _write_json(OUTPUT_DIR / "manifest.json", manifest)
    _refresh_replay()
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
        "energy_basis": (
            "shared simulated route energy model; completed validated route"
            if state["status"] == "route_complete"
            else "shared simulated route energy model; legal accepted prefix only"
        ),
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


def _no_action_stop_status(constraint: Any) -> str:
    """Distinguish a request-budget stop from a true local dead end."""
    reasons = set((getattr(constraint, "filtered_directions", {}) or {}).values())
    budget_reasons = {
        "remaining_action_budget_insufficient",
        "action_budget_insufficient",
    }
    if reasons and reasons.issubset(budget_reasons):
        return "insufficient_authorized_budget_for_remaining_route"
    if reasons == {"remaining_route_unreachable"}:
        return "remaining_route_unreachable"
    return "local_pre_request_no_allowed_action"


def one_step() -> None:
    route_gate_block = route_safety_gate_block_reason_for_path(OUTPUT_DIR, ROOT)
    if route_gate_block:
        raise RuntimeError(route_gate_block)
    if not (OUTPUT_DIR / "state.json").exists():
        raise RuntimeError("run_preflight_first")
    state = _read_json(OUTPUT_DIR / "state.json")
    if state["status"] != "ready":
        raise RuntimeError("pilot_stopped_status:" + str(state["status"]))
    if not state.get("additional_model_requests_authorized", False):
        state["status"] = "awaiting_user_authorization"
        state["current_authorization_status"] = "not_authorized"
        _write_json(OUTPUT_DIR / "state.json", state)
        _refresh_replay()
        raise RuntimeError("model_request_not_authorized")

    preflight_path = Path(state["source_preflight"])
    if not preflight_path.is_absolute():
        preflight_path = ROOT / preflight_path
    if hashlib.sha256(preflight_path.read_bytes()).hexdigest() != state["source_preflight_sha256"]:
        raise RuntimeError("frozen_preflight_hash_changed")
    _, road_map, task, package = _load_frozen_task(preflight_path)
    if (
        road_map.map_id != state["map_id"]
        or task.task_id != state["task_id"]
        or task.start_node_id != state["start_node_id"]
        or package.pickup_node_id != state["pickup_node_id"]
        or package.dropoff_node_id != state["dropoff_node_id"]
    ):
        raise RuntimeError("frozen_preflight_task_mismatch")
    if _apply_unbounded_route_budget(state, package):
        _write_json(OUTPUT_DIR / "state.json", state)
        task_manifest_path = OUTPUT_DIR / "manifest.json"
        if task_manifest_path.is_file():
            task_manifest = _read_json(task_manifest_path)
            task_manifest.update({
                "candidate_request_limit": state["authorized_new_request_limit"],
                "effective_authorized_new_request_limit": state["effective_authorized_new_request_limit"],
                "route_action_budget": state["route_action_budget"],
                "request_limit_semantics": "user_authorized_unbounded; finite graph-derived simple-path bound",
                "candidate_run_limits": (
                    "No artificial per-task request/action quota; natural maximum is unique frozen graph nodes minus one. "
                    "Each offered direction requires a complete suffix witness. Invalid moves do not advance state and are re-searched from the last legal node with that direction excluded; zero CLI retries."
                ),
                "unbounded_authorization_applied": state["unbounded_authorization_applied"],
            })
            _write_json(task_manifest_path, task_manifest)
        _refresh_replay()
    effective_authorized_limit = int(state.get("effective_authorized_new_request_limit", 0))
    if effective_authorized_limit <= 0:
        state["status"] = "awaiting_user_authorization"
        state["current_authorization_status"] = "not_authorized"
        state["additional_model_requests_authorized"] = False
        state["effective_authorized_new_request_limit"] = 0
        _write_json(OUTPUT_DIR / "state.json", state)
        _refresh_replay()
        raise RuntimeError("model_request_not_authorized")
    authorized_limit = int(state.get("authorized_new_request_limit", DEFAULT_NEW_CALLS))
    if int(state.get("new_requests_used", 0)) >= min(effective_authorized_limit, authorized_limit):
        state["status"] = "authorized_call_limit_exhausted"
        _write_json(OUTPUT_DIR / "state.json", state)
        _refresh_replay()
        raise RuntimeError("authorized_call_limit_exhausted")
    current = state["accepted_nodes"][-1]
    route_action_budget = int(state.get("route_action_budget", ROUTE_ACTION_BUDGET)) - len(state["accepted_edges"])
    remaining_authorized_calls = max(
        0,
        int(state.get("authorized_new_request_limit", DEFAULT_NEW_CALLS))
        - int(state["new_requests_used"]),
    )
    effective_completion_budget = min(route_action_budget, remaining_authorized_calls)
    full_constraint = build_r4_route_output_constraint(
        package, state["pickup_node_id"], state["dropoff_node_id"], allow_failed=False
    )
    current_prefix_hash = accepted_prefix_sha256(
        state["accepted_nodes"], state["accepted_edges"]
    )
    stored_prefix_audit = state.get("suffix_witness_prefix_audit") or {}
    if (
        state.get("suffix_witness_gate_version") != "r4-suffix-witness-gate-v1"
        or not state.get("suffix_witness_gate_enabled")
        or stored_prefix_audit.get("prefix_sha256") != current_prefix_hash
    ):
        prefix_recovery = _recover_resume_prefix_to_last_certified_checkpoint(
            package,
            full_constraint,
            accepted_nodes=list(state["accepted_nodes"]),
            accepted_edges=list(state["accepted_edges"]),
            accepted_directions=list(state["accepted_directions"]),
            route_action_budget=int(state.get("route_action_budget", ROUTE_ACTION_BUDGET)),
        )
        if not prefix_recovery.get("passed"):
            prefix_audit = prefix_recovery.get("failure_audit") or {
                "passed": False,
                "reason": prefix_recovery.get("reason"),
            }
            state["status"] = "paused_suffix_witness_gate_failed"
            state["stop_diagnostics"] = prefix_audit
            _write_json(OUTPUT_DIR / "state.json", state)
            _refresh_replay()
            raise RuntimeError("accepted_prefix_suffix_witness_audit_failed")
        prefix_audit = prefix_recovery["prefix_audit"]
        rollback_event = prefix_recovery.get("rollback")
        if rollback_event:
            state["accepted_nodes"] = list(prefix_recovery["accepted_nodes"])
            state["accepted_edges"] = list(prefix_recovery["accepted_edges"])
            state["accepted_directions"] = list(prefix_recovery["accepted_directions"])
            state["pickup_seen"] = package.pickup_node_id in state["accepted_nodes"]
            current = state["accepted_nodes"][-1]
            route_action_budget = int(
                state.get("route_action_budget", ROUTE_ACTION_BUDGET)
            ) - len(state["accepted_edges"])
            effective_completion_budget = min(
                route_action_budget, remaining_authorized_calls
            )
            rejected = state.setdefault("rejected_directions_by_node", {})
            rollback_event["cleared_rejected_directions_at_checkpoint"] = copy.deepcopy(
                rejected.pop(current, [])
            )
            state.setdefault("resume_prefix_rollbacks", []).append(rollback_event)
            current_prefix_hash = rollback_event["checkpoint_prefix_sha256"]
        state["suffix_witness_gate_version"] = "r4-suffix-witness-gate-v1"
        state["suffix_witness_gate_enabled"] = True
        state.setdefault("rejected_directions_by_node", {})
        state["suffix_witness_prefix_audit"] = prefix_audit
        state.setdefault("offline_reconciliation_events", [])
        state["suffix_witness_gate_migrations"] = state.get(
            "suffix_witness_gate_migrations", []
        ) + [{
            "event": (
                "rollback_to_last_certified_checkpoint_and_enable_suffix_gate"
                if rollback_event
                else "suffix_witness_gate_enabled_after_zero_call_prefix_audit"
            ),
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "prefix_sha256": current_prefix_hash,
            "accepted_action_count": len(state["accepted_edges"]),
            "rollback": rollback_event,
            "model_calls": 0,
        }]
        _write_json(OUTPUT_DIR / "state.json", state)
        _refresh_replay()
    constraint = build_r4_step_output_constraint(
        package,
        full_constraint,
        current_node_id=current,
        pickup_seen=bool(state["pickup_seen"]),
        visited_node_ids=state["accepted_nodes"],
        remaining_action_budget=effective_completion_budget,
        include_objective_cost_guidance=bool(state.get("enable_progress_guidance", False)),
        require_suffix_witness=True,
        rejected_directions=sorted(
            set(state.get("rejected_directions_by_node", {}).get(current, ()))
            | set(state.get("backtracked_directions_by_node", {}).get(current, ()))
        ),
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
        _refresh_replay()
        raise RuntimeError(budget_stop_reason)
    if not constraint.edge_by_direction:
        failure_status = _no_action_stop_status(constraint)
        request_number = int(state.get("new_requests_used", 0)) + 1
        failure = {
            "request_number": request_number,
            "current_node_id": current,
            "status": failure_status,
            "model_request_started": False,
            "retry_count": 0,
            "allowed_directions": [],
            "filtered_directions": dict(constraint.filtered_directions),
            "remaining_minimum_moves": constraint.remaining_minimum_moves,
            "remaining_authorized_calls": remaining_authorized_calls,
            "route_action_budget_remaining": route_action_budget,
            "effective_completion_budget": effective_completion_budget,
            "pickup_node_id": state["pickup_node_id"],
            "dropoff_node_id": state["dropoff_node_id"],
        }
        if failure_status == "insufficient_authorized_budget_for_remaining_route":
            failure["reason"] = "every_available_next_direction_was_filtered_by_remaining_authorized_budget"
        elif failure_status == "remaining_route_unreachable":
            failure["reason"] = "every_available_next_direction_leaves_no_reachable_route"
        else:
            failure["reason"] = "no_dynamically_allowed_unvisited_direction"
        state["status"] = failure_status
        state.setdefault("pre_request_failures", []).append(failure)
        state["stop_diagnostics"] = dict(failure)
        failure_dir = OUTPUT_DIR / "pre_request_failures"
        _write_json(
            failure_dir / "pre_request_{0:02d}.json".format(len(state["pre_request_failures"])),
            failure,
        )
        _write_json(OUTPUT_DIR / "state.json", state)
        _refresh_replay()
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        raise RuntimeError(failure_status)
    messages = build_r4_stepwise_messages(
        package,
        state["start_node_id"],
        state["pickup_node_id"],
        state["dropoff_node_id"],
        current_node_id=current,
        visited_node_ids=state["accepted_nodes"],
        pickup_seen=bool(state["pickup_seen"]),
        accepted_directions=state["accepted_directions"],
        feedback=state.get("pending_route_research_feedback"),
        remaining_action_budget=effective_completion_budget,
        route_feature_context=state.get("route_feature_context"),
        compact_visited_state=bool(state.get("compact_visited_state", False)),
        progress_guidance=(
            _objective_progress_guidance(constraint)
            if state.get("enable_progress_guidance", False)
            else None
        ),
        shared_candidate_routes=state.get("shared_candidate_routes") if state.get("shared_candidate_pool_sha256") else None,
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
        "cumulative_request_number": int(
            state.get(
                "cumulative_parent_cli_invocations",
                state.get("cumulative_parent_requests", state.get("parent_requests", 0)),
            )
        ) + call_number,
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
        "suffix_witness_gate_version": "r4-suffix-witness-gate-v1",
        "suffix_witness_by_direction": constraint.suffix_witness_by_direction,
        "route_research_trigger": copy.deepcopy(
            state.get("pending_route_research_feedback")
        ),
        "rollback_checkpoint": {
            "node_id": current,
            "accepted_edge_count": len(state["accepted_edges"]),
            "accepted_prefix_sha256": hashlib.sha256(
                json.dumps(
                    {"nodes": state["accepted_nodes"], "edges": state["accepted_edges"]},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
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
        attempt["usage"] = _attempt_usage_record({
            "input_tokens": event_audit.get("input_tokens"),
            "cached_input_tokens": event_audit.get("cached_input_tokens"),
            "output_tokens": event_audit.get("output_tokens"),
            "latency_ms": provider_record.get("latency_ms"),
        }, state, provider_record.get("session_id") or state.get("codex_session_id"))
        attempt["error_type"] = type(error).__name__
        attempt["error"] = CodexCliRouteStepClient._redact_diagnostics(str(error))[:500]
        tool_events = event_audit.get("tool_item_types", [])
        if provider_record.get("status") == "not_called":
            failure = dict(attempt)
            failure["status"] = "not_called_preflight_rejection"
            failure["model_request_started"] = False
            state.setdefault("pre_request_failures", []).append(failure)
            state["status"] = "stopped_pre_request_error"
            state["usage_totals"] = _summarize_attempt_usage(
                state["attempts"], state["new_requests_used"]
            )
            failure_index = len(state["pre_request_failures"])
            _write_json(
                OUTPUT_DIR / "pre_request_failures" / ("pre_request_{0:02d}.json".format(failure_index)),
                failure,
            )
            _write_json(OUTPUT_DIR / "state.json", state)
            _refresh_replay()
            print(json.dumps(failure, ensure_ascii=False, indent=2))
            return
        attempt["status"] = "stopped_tool_event" if tool_events else "stopped_request_error"
        attempt["tool_item_types"] = tool_events
        state["new_requests_used"] += 1 if provider_record.get("status") != "not_called" else 0
        state["attempts"].append(attempt)
        state["status"] = attempt["status"]
        state["usage_totals"] = _summarize_attempt_usage(
            state["attempts"], state["new_requests_used"]
        )
        _write_json(OUTPUT_DIR / ("attempt_{0:02d}.json".format(attempt_log_index)), attempt)
        _write_json(OUTPUT_DIR / "state.json", state)
        _refresh_replay()
        print(json.dumps(attempt, ensure_ascii=False, indent=2))
        return

    state["new_requests_used"] += 1
    state["pending_route_research_feedback"] = None
    if state.get("codex_session_mode") == "persistent_per_task":
        state["codex_session_id"] = response.session_id
    attempt["codex_session_id"] = response.session_id
    attempt["response"] = payload
    attempt["usage"] = {
        **_attempt_usage_record({
            "input_tokens": response.input_tokens,
            "cached_input_tokens": response.cached_input_tokens,
            "output_tokens": response.output_tokens,
            "latency_ms": response.latency_ms,
        }, state, response.session_id),
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
            require_suffix_witness=True,
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
            "model_failure_code_overridden_by_local_validation": (
                payload.get("action") == "move"
                and payload.get("failure_reason_code") != "none"
                and local_check["passed"] is True
            ),
            "model_self_check_well_formed": self_check_well_formed,
            "all_model_self_checks_true": self_check_all_true,
            "model_self_check_mismatch_fields": self_check_mismatch_fields,
            "deterministic_local_check": {
                key: value for key, value in local_check.items() if key != "edge"
            },
            "suffix_witness_gate_passed": bool(local_check.get("suffix_witness")),
        }
        accepted = (
            fields_ok
            and payload.get("action") == "move"
            and direction in constraint.edge_by_direction
            and self_check_well_formed
            and local_check["passed"]
            and bool(local_check.get("suffix_witness"))
        )
        attempt["validation"] = validation
        if not accepted:
            attempt["status"] = "stopped_illegal_or_invalid_action"
            rejection_code = (
                local_check.get("errors", ["candidate_rejected"])[0]
                if local_check.get("errors")
                else "candidate_rejected"
            )
            recovery = _rollback_rejected_action_one_node(
                state,
                current_node_id=current,
                rejected_direction=(direction if isinstance(direction, str) else None),
                rejected_target_node_id=local_check.get("expected_target_node_id"),
                rejection_code=rejection_code,
            )
            attempt["route_recovery"] = recovery
            attempt["rollback_to_last_legal_checkpoint"] = True
            attempt["rejected_direction_at_checkpoint"] = (
                direction if direction in {"E", "W", "N", "S"} else None
            )
            attempt["status"] = "rejected_and_reroute_from_last_legal_node"
            attempt["reroute_checkpoint_prefix_sha256"] = accepted_prefix_sha256(
                state["accepted_nodes"], state["accepted_edges"]
            )
            recovery_prefix_audit = audit_accepted_prefix_suffix_witnesses(
                package,
                full_constraint,
                accepted_nodes=list(state["accepted_nodes"]),
                accepted_edges=list(state["accepted_edges"]),
                accepted_directions=list(state["accepted_directions"]),
                route_action_budget=int(
                    state.get("route_action_budget", ROUTE_ACTION_BUDGET)
                ),
            )
            state["suffix_witness_prefix_audit"] = recovery_prefix_audit
            if recovery_prefix_audit.get("passed"):
                state["status"] = "ready"
            else:
                state["status"] = "paused_suffix_witness_gate_failed"
                state["stop_diagnostics"] = recovery_prefix_audit
        else:
            edge = constraint.edge_by_direction[direction]
            attempt["selected_edge_id"] = edge.edge_id
            attempt["target_node_id"] = edge.to_node
            attempt["status"] = "accepted_legal_step"
            state["accepted_edges"].append(edge.edge_id)
            state["accepted_directions"].append(direction)
            state["accepted_nodes"].append(edge.to_node)
            state["pickup_seen"] = bool(state["pickup_seen"] or edge.to_node == state["pickup_node_id"])
            attempt["suffix_witness_after_selected_action"] = local_check.get("suffix_witness")
            state["suffix_witness_prefix_audit"] = {
                "passed": bool(local_check.get("suffix_witness")),
                "accepted_action_count": len(state["accepted_edges"]),
                "prefix_sha256": accepted_prefix_sha256(
                    state["accepted_nodes"], state["accepted_edges"]
                ),
                "last_step_witness": local_check.get("suffix_witness"),
                "model_calls": 0,
            }
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
    state["usage_totals"] = _summarize_attempt_usage(
        state["attempts"], state["new_requests_used"]
    )
    state["route_metrics"] = _route_summary(state, road_map)
    _write_json(OUTPUT_DIR / ("attempt_{0:02d}.json".format(attempt_log_index)), attempt)
    _write_json(OUTPUT_DIR / "state.json", state)
    _refresh_replay()
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
        default=None,
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
        default=DEFAULT_NEW_CALLS,
        help="Candidate request count for a separately authorized run; formal no-limit authorization expands to the frozen graph's simple-path bound",
    )
    parser.add_argument(
        "--codex-session-mode",
        choices=("ephemeral_per_request", "persistent_per_task"),
        default="ephemeral_per_request",
        help="Use one isolated Codex session per task to avoid repeating the compact map after the first step",
    )
    parser.add_argument(
        "--route-input-protocol",
        choices=("r4-step-v2", "r4-step-v3", "r4-step-v4-link-fallback"),
        default=None,
        help="Version the prompt protocol for a fresh pilot; a resumed route cannot switch protocols",
    )
    parser.add_argument(
        "--shared-candidate-pool",
        type=Path,
        default=None,
        help="Verified candidate_pools.jsonl from an r4_shared_candidate_pool_v2 artifact; required by r4-step-v4-link-fallback",
    )
    parser.add_argument(
        "--record-user-authorized-task9-experiment",
        action="store_true",
        help="Record the user's prior authorization for the task9-only experiment; makes no model request by itself",
    )
    parser.add_argument(
        "--record-user-authorized-formal-batch-pilot",
        action="store_true",
        help="Record the user's direct authorization to complete all plan-required R4 experiments; makes no model request by itself",
    )
    parser.add_argument("--resume-from", type=Path, default=None,
                        help="Parent pilot directory; use only with --preflight-only to start a new bounded continuation")
    parser.add_argument(
        "--allow-unexecuted-self-check-mismatch",
        action="store_true",
        help="Allow resuming only from the v3 not_revisited=false proposal that passed deterministic local validation and was not executed",
    )
    parser.add_argument(
        "--user-authorized-user-stop-resume",
        action="store_true",
        help="Record explicit user authorization to continue a stopped task9 route from its validated prefix",
    )
    parser.add_argument(
        "--user-authorized-formal-task-resume",
        action="store_true",
        help="Continue an already authorized formal R4 task from its validated prefix; unresolved requests remain unreplayed",
    )
    args = parser.parse_args()
    OUTPUT_DIR = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    if (OUTPUT_DIR / "USER_STOP_REQUESTED").exists():
        raise SystemExit("user_stop_sentinel_present: no R4 request may be started for this run")
    selected_preflight = args.preflight_path
    if selected_preflight and not selected_preflight.is_absolute():
        selected_preflight = ROOT / selected_preflight
    if selected_preflight:
        PREFLIGHT_PATH = selected_preflight
    if args.resume_from and not args.preflight_only:
        parser.error("--resume-from can only be used with --preflight-only")
    if args.allow_unexecuted_self_check_mismatch and not args.resume_from:
        parser.error("--allow-unexecuted-self-check-mismatch requires --resume-from")
    if args.user_authorized_user_stop_resume and (not args.resume_from or not args.preflight_only):
        parser.error("--user-authorized-user-stop-resume requires --resume-from and --preflight-only")
    if args.user_authorized_formal_task_resume and (not args.resume_from or not args.preflight_only):
        parser.error("--user-authorized-formal-task-resume requires --resume-from and --preflight-only")
    if args.user_authorized_formal_task_resume and args.user_authorized_user_stop_resume:
        parser.error("formal task resume and task9 user-stop resume are mutually exclusive")
    if args.resume_from and args.experiment_id:
        parser.error("--experiment-id is only supported for a fresh pilot")
    if args.shared_candidate_pool and not args.shared_candidate_pool.is_absolute():
        args.shared_candidate_pool = ROOT / args.shared_candidate_pool
    if args.request_limit < 1:
        parser.error("--request-limit must be a positive integer")
    if args.preflight_only:
        preflight_only(
            args.resume_from or None,
            args.allow_unexecuted_self_check_mismatch,
            args.experiment_id,
            args.request_limit,
            selected_preflight,
            args.codex_session_mode,
            args.user_authorized_user_stop_resume,
            args.route_input_protocol,
            args.record_user_authorized_task9_experiment,
            args.record_user_authorized_formal_batch_pilot,
            args.user_authorized_formal_task_resume,
            args.shared_candidate_pool,
        )
    else:
        one_step()


if __name__ == "__main__":
    main()
