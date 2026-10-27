#!/usr/bin/env python3
"""Spend only the five remaining approved R4 progress-v2 model requests.

Starts from the offline-validated call-20 response without replaying it. Any
new CLI/model error ends this continuation; there are no retries or fallbacks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_cli_support import estimate_codex_usage_cost
from preflight_r4_codex_segment_task9 import MAX_FEEDBACK_REQUESTS, MAX_SEGMENT_ACTIONS, MAX_TOTAL_ACTIONS, PROGRESS_MULTIPLIER
from render_r4_segment_replay import render_replay
from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import EvaluationConfig, RouteCandidate
from route_experiment.r4_segment import run_r4_segment_route_search
from run_r4_codex_formal_checkpoint import prepare
from run_r4_codex_segment_task9 import MODEL, _load_gate, _sha256, _source_feature_context


ROOT_RUN = ROOT / "outputs/route_layer/r4_codex_segment_task9_progress_v2_continuation_20261027_v2"
OUTPUT_RUN = ROOT / "outputs/route_layer/r4_codex_segment_task9_progress_v2_continuation_20261027_v3"
PRICING_CONFIG = ROOT / "configs/codex_cli_pricing_2026-10-22.json"


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with temp.open("wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected_json_object:" + str(path))
    return value


def _build_seed(parent_dir: Path) -> Dict[str, Any]:
    parent_dir = parent_dir.resolve()
    audit_path = parent_dir / "offline_reconciled_final_audit.json"
    audit = _read_json(audit_path)
    manifest_path = parent_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if audit.get("experiment", {}).get("model_turns_completed") != 20:
        raise ValueError("parent_turn_count_mismatch")
    if audit.get("offline_recovered_response_replayed") is not False:
        raise ValueError("call20_was_replayed")
    if audit.get("route", {}).get("last_node_id") != "node_63_32":
        raise ValueError("call20_offline_prefix_node_mismatch")
    route = audit["route"]
    state = {
        "current_node_id": route["last_node_id"],
        "visited_node_ids": list(route["node_sequence"]),
        "pickup_seen": False,
        "direction_sequence": list(route["direction_sequence"]),
        "edge_sequence": list(route["edge_sequence"]),
    }
    if not (len(state["direction_sequence"]) == len(state["edge_sequence"]) == len(state["visited_node_ids"]) - 1 == 17):
        raise ValueError("offline_recovered_route_prefix_shape_invalid")
    if int(audit.get("rejected_segment_count", -1)) != MAX_FEEDBACK_REQUESTS:
        raise ValueError("feedback_budget_state_mismatch")
    session_id = manifest.get("codex_session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("persistent_codex_session_id_missing")
    return {
        "audit": audit,
        "manifest": manifest,
        "route_state": state,
        "session_id": session_id,
        "source_hashes": {
            "parent_manifest_sha256": _sha256(manifest_path.read_bytes()),
            "parent_offline_audit_sha256": _sha256(audit_path.read_bytes()),
            "call20_offline_audit_sha256": _sha256((parent_dir / "call_0020_offline_reconciliation.json").read_bytes()),
        },
    }


def run(output_dir: Path, parent_dir: Path, authorization_note: str) -> Dict[str, Any]:
    output_dir = output_dir.resolve()
    parent_dir = parent_dir.resolve()
    if output_dir.exists():
        raise FileExistsError("output_exists_no_replay_or_overwrite")
    if not authorization_note.strip():
        raise ValueError("current_user_approval_note_required")
    parent = _build_seed(parent_dir)
    loaded = _load_gate()
    item = loaded["item"]
    task = item["task"]
    if len(parent["route_state"]["visited_node_ids"]) == 0 or parent["route_state"]["visited_node_ids"][0] != task.start_node_id:
        raise ValueError("resumed_prefix_start_node_mismatch")

    request_limit = 25 - int(parent["audit"]["experiment"]["model_turns_completed"])
    if request_limit != 5:
        raise ValueError("approved_remaining_request_budget_mismatch")
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
    client.trace_index = 20

    manifest = {
        "schema_version": "r4_codex_progress_v2_final_budget_continuation_manifest_v1",
        "experiment_id": "r4_codex_segment_task9_progress_v2_continuation_20261027_v3",
        "status": "ready_for_execution",
        "authorization_note": authorization_note.strip(),
        "parent_run_path": str(parent_dir),
        "parent_evidence_hashes": parent["source_hashes"],
        "cumulative_model_requests_completed_before_start": 20,
        "cumulative_model_request_cap": 25,
        "new_request_limit": request_limit,
        "request_number_offset": 20,
        "initial_rejected_segments": MAX_FEEDBACK_REQUESTS,
        "remaining_rejected_segment_feedback_budget": 0,
        "route_state_includes_offline_validated_call20_without_replay": parent["route_state"],
        "model": MODEL,
        "reasoning_effort": "low",
        "codex_session_id": parent["session_id"],
        "session_mode": "same_persistent_per_task_session",
        "network_retries": 0,
        "traditional_fallback": False,
        "tool_events_allowed": False,
        "progress_limit": 218,
        "frozen_input_hashes": loaded["observed_hashes"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(output_dir / "manifest.json", manifest)

    state: Dict[str, Any] = {
        "experiment_id": manifest["experiment_id"],
        "status": "ready",
        "model_requests_started": 20,
        "model_requests_completed": 20,
        "new_model_requests_started": 0,
        "new_model_requests_completed": 0,
        "accepted_action_count": 17,
        "current_node_id": parent["route_state"]["current_node_id"],
        "pickup_seen": False,
        "cumulative_request_cap": 25,
        "effective_request_limit": 5,
        "rejected_segments": MAX_FEEDBACK_REQUESTS,
        "network_retries": 0,
        "parent_runner_state_is_immutable": True,
        "unresolved_cli_invocation": None,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    state_path = output_dir / "state.json"
    _write_json(state_path, state)

    def on_start(row: Dict[str, Any]) -> None:
        number = int(row["request_number"])
        attempt = {**row, "status": "request_in_progress", "delivery_status": "unknown_until_receipt", "started_at_utc": datetime.now(timezone.utc).isoformat()}
        _write_json(output_dir / "attempts" / f"attempt_{number:03d}.json", attempt)
        state.update({
            "status": "request_in_progress",
            "model_requests_started": number,
            "new_model_requests_started": number - 20,
            "request_in_progress": attempt,
            "unresolved_cli_invocation": {"request_number": number, "status": "delivery_and_usage_unknown_until_receipt", "messages_sha256": row["messages_sha256"]},
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        _write_json(state_path, state)

    def on_attempt(row: Dict[str, Any]) -> None:
        number = int(row["request_number"])
        path = output_dir / "attempts" / f"attempt_{number:03d}.json"
        previous = _read_json(path)
        status = row.get("status")
        attempt = {**previous, **row, "provider_audit": client.last_record, "completed_at_utc": datetime.now(timezone.utc).isoformat(), "delivery_status": "response_received" if status == "response_received" else "error_no_retry"}
        _write_json(path, attempt)
        audit = row.get("audit") or {}
        if audit.get("accepted"):
            state["accepted_action_count"] += len(audit.get("committed_direction_sequence", []))
            state["current_node_id"] = audit.get("current_node_after_segment", state["current_node_id"])
            state["pickup_seen"] = audit.get("pickup_seen_after_segment", state["pickup_seen"])
        elif status == "response_received" and audit.get("status") != "model_gave_up":
            state["rejected_segments"] += 1
        state.update({
            "status": "response_recorded" if status == "response_received" else "cli_or_model_error_no_retry",
            "model_requests_completed": number,
            "new_model_requests_completed": number - 20,
            "last_request_status": status,
            "last_request_usage": row.get("usage"),
            "unresolved_cli_invocation": None if status == "response_received" else {"request_number": number, "status": "cli_or_model_error_reconciled_separately", "provider_audit": client.last_record},
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        _write_json(state_path, state)

    result = run_r4_segment_route_search(
        client, item["package"], item["road_map"], task, task.pickup_node_id, task.dropoff_node_id,
        max_api_calls=request_limit,
        max_segment_actions=MAX_SEGMENT_ACTIONS,
        max_total_actions=MAX_TOTAL_ACTIONS,
        max_progress_multiplier=PROGRESS_MULTIPLIER,
        max_feedback_requests=MAX_FEEDBACK_REQUESTS,
        route_feature_context=_source_feature_context(),
        on_request_start=on_start,
        on_attempt=on_attempt,
        initial_route_state=parent["route_state"],
        progress_limit_override=218,
        initial_rejected_segments=MAX_FEEDBACK_REQUESTS,
        request_number_offset=20,
    )
    new_attempts = [_read_json(p) for p in sorted((output_dir / "attempts").glob("attempt_*.json"))]
    route = result["route"]
    if route["edge_sequence"]:
        candidate = RouteCandidate(route_id="r4-progress-v2-final-continuation", node_sequence=route["node_sequence"], edge_sequence=route["edge_sequence"], route_source="r4_codex_segment_progress_v2", route_reachability=True, validation_status="valid_prefix")
        ev = evaluate_route_candidate(item["road_map"], candidate, EvaluationConfig())
        energy = {"scope": "complete_model_route" if result["route_complete"] else "accepted_legal_prefix_only", "distance_m": ev.route_distance_m, "travel_time_sec": ev.total_travel_time_sec, "energy_kwh": ev.energy_total_kwh, "energy_model_version": ev.energy_model_version, "simulation_estimate": True}
    else:
        energy = {"scope": "not_estimable_no_accepted_edges", "distance_m": None, "travel_time_sec": None, "energy_kwh": None, "simulation_estimate": True}
    prior_usage = parent["audit"]["usage"]
    all_rows = list(prior_usage["per_request_rows"])
    for attempt in new_attempts:
        usage = attempt.get("usage") or {}
        all_rows.append({"request_number": attempt.get("request_number"), "status": attempt.get("status"), **usage})
    known_input = sum(r.get("input_tokens", 0) for r in all_rows if isinstance(r.get("input_tokens"), int))
    known_cached = sum(r.get("cached_input_tokens", 0) for r in all_rows if isinstance(r.get("cached_input_tokens"), int))
    known_output = sum(r.get("output_tokens", 0) for r in all_rows if isinstance(r.get("output_tokens"), int))
    usage_complete = len(all_rows) == 20 + len(new_attempts) and all(all(isinstance(r.get(k), int) for k in ("input_tokens", "cached_input_tokens", "output_tokens")) for r in all_rows)
    cost = estimate_codex_usage_cost(MODEL, known_input if usage_complete else None, known_cached if usage_complete else None, known_output if usage_complete else None, pricing_config_path=PRICING_CONFIG)
    final = {
        "schema_version": "r4_codex_progress_v2_cumulative_final_audit_v1",
        "experiment_id": manifest["experiment_id"],
        "parent_audit_path": str(parent_dir / "offline_reconciled_final_audit.json"),
        "parent_evidence_hashes": parent["source_hashes"],
        "status": result["status"],
        "formal_result": False,
        "route_complete": result["route_complete"],
        "route": route,
        "energy": energy,
        "parent_model_requests": 20,
        "new_model_requests": result["api_request_count"],
        "cumulative_model_requests": 20 + result["api_request_count"],
        "cumulative_model_request_cap": 25,
        "rejected_segments_total": result["rejected_segment_count"],
        "rejected_segment_limit": MAX_FEEDBACK_REQUESTS,
        "attempts_after_parent": new_attempts,
        "usage": {"input_tokens": known_input if usage_complete else None, "cached_input_tokens": known_cached if usage_complete else None, "output_tokens": known_output if usage_complete else None, "usage_complete": usage_complete, "account_billing": "not_reported_by_codex_cli", "rate_card_estimate": cost, "per_request_rows": all_rows},
        "limitations": ["Single task9 pilot only; no cross-seed or performance inference.", "The route is complete only if both fixed pickup and dropoff are reached.", "No request is retried; a CLI error must be reconciled offline.", "Rate-card estimate is not account billing."],
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(output_dir / "search_result.json", result)
    _write_json(output_dir / "cumulative_usage_ledger.json", final["usage"])
    _write_json(output_dir / "final_audit.json", final)
    state.update({"status": result["status"], "route_complete": result["route_complete"], "cumulative_model_requests": final["cumulative_model_requests"], "finished_at_utc": final["finished_at_utc"]})
    _write_json(state_path, state)
    render_replay(item["map_data"], {"task_id": task.task_id, "start_node_id": task.start_node_id, "pickup_node_id": task.pickup_node_id, "dropoff_node_id": task.dropoff_node_id}, final, output_dir / "replay.html")
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--parent", type=Path, default=ROOT_RUN)
    parser.add_argument("--output", type=Path, default=OUTPUT_RUN)
    parser.add_argument("--authorization-note", default="")
    args = parser.parse_args()
    if not args.execute:
        seed = _build_seed(args.parent)
        print(json.dumps({"status": "local_preflight_only_no_model_requests", "cumulative_requests_used": 20, "remaining_requests": 5, "current_node_id": seed["route_state"]["current_node_id"], "accepted_actions": len(seed["route_state"]["direction_sequence"]), "rejected_segments": MAX_FEEDBACK_REQUESTS, "output_available": not args.output.exists()}, ensure_ascii=False, indent=2))
        return
    if not args.authorization_note.strip():
        raise SystemExit("--execute requires the current approved 25-request budget")
    final = run(args.output, args.parent, args.authorization_note)
    print(json.dumps(final, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
