#!/usr/bin/env python3
"""Prepare and run a separate R4 recovery shard for audited provider rejections.

Only the last CLI attempt of each parent run is continued, and only when the
existing R4 verifier proves that it was a zero-retry HTTP 429/capacity rejection
with no model response. The parent trace is preserved and never replayed.
"""

import argparse
import copy
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import r4_parallel_formal_batch as parallel  # noqa: E402
import run_r4_codex_formal_batch as batch  # noqa: E402
import run_r4_codex_pilot_continuation as continuation  # noqa: E402
from r4_execution_lock import acquire_execution_lock, release_execution_lock  # noqa: E402

DEFAULT_BATCH_ROOT = Path(
    "/Users/didi/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/"
    "r4_codex_formal_batch_20261027_v2"
)
DEFAULT_SOURCE = Path(
    "/Users/didi/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/"
    "r4_codex_batch_preflights_20261024_v1/manifest.json"
)
DEFAULT_PILOT = ROOT / "outputs/route_layer/r4_codex_formal_task9_pilot_20261027_v3/final_audit.json"
WORKER_ID = "worker_recovery_429"
RELATIVE_ROOT = "parallel_workers/" + WORKER_ID
SERVICE_LABEL = "local.codex.r4-formal-20261027-" + WORKER_ID
STEP_RUNNER = ROOT / "scripts/run_r4_codex_pilot_continuation.py"
FINALIZER = ROOT / "scripts/finalize_r4_codex_pilot_continuation.py"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    batch.write_json(Path(path), value)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _absolute(root, value):
    path = Path(value)
    return path if path.is_absolute() else root / path


def _valid_zero_call_continuation_preflight(state, task_id, parent_state_sha256, parent_audit_sha256, request_bound):
    """Accept only a ready, authorized, untouched continuation of the frozen parent."""
    return bool(
        state.get("status") == "ready"
        and state.get("additional_model_requests_authorized") is True
        and state.get("route_input_protocol") == "r4-step-v3"
        and state.get("task_id") == task_id
        and state.get("parent_state_sha256") == parent_state_sha256
        and state.get("parent_final_audit_sha256") == parent_audit_sha256
        and int(state.get("new_requests_used", -1)) == 0
        and not state.get("attempts")
        and int(state.get("authorized_new_request_limit", 0)) == request_bound
        and int(state.get("effective_authorized_new_request_limit", 0)) == request_bound
    )


def _deduplicate_prepared_worker(batch_root):
    """Remove tasks already assigned to another recovery shard before requests."""
    batch_root = Path(batch_root).resolve()
    parallel_root = batch_root / "parallel_workers"
    worker_root = parallel_root / WORKER_ID
    manifest_path = worker_root / "manifest.json"
    manifest = _read(manifest_path)
    plan_path = parallel_root / "parallel_execution_plan.json"
    plan = _read(plan_path)
    recovery_worker_ids = {
        row.get("worker_id")
        for row in plan.get("workers", [])
        if row.get("aggregation_mode") == "append_recovery_run"
    }
    master_path = batch_root / "manifest.json"
    if master_path.is_file():
        embedded = (_read(master_path).get("parallel_execution") or {}).get("workers", [])
        recovery_worker_ids.update(
            row.get("worker_id") for row in embedded
            if row.get("aggregation_mode") == "append_recovery_run"
        )
    assigned_elsewhere = {}
    for candidate in sorted(parallel_root.glob("*/manifest.json")):
        if candidate.parent == worker_root:
            continue
        try:
            other = _read(candidate)
        except (OSError, json.JSONDecodeError):
            continue
        is_recovery = (
            other.get("recovery_mode") == "append_recovery_run"
            or candidate.parent.name in recovery_worker_ids
            or candidate.parent.name.startswith("worker_recovery")
            or "recovery" in str(other.get("experiment_id", "")).casefold()
        )
        if not is_recovery:
            continue
        owner = other.get("experiment_id") or candidate.parent.name
        for row in other.get("tasks", []):
            assigned_elsewhere.setdefault(row.get("task_id"), set()).add(owner)

    original_tasks = list(manifest.get("tasks", []))
    excluded = [
        {"task_id": row["task_id"], "already_assigned_to": sorted(assigned_elsewhere[row["task_id"]])}
        for row in original_tasks if row.get("task_id") in assigned_elsewhere
    ]
    kept = [row for row in original_tasks if row.get("task_id") not in assigned_elsewhere]
    if not excluded:
        return {"excluded_count": 0, "kept_count": len(kept)}
    if not kept:
        raise RuntimeError("all_provider_rejection_tasks_already_assigned_elsewhere")

    source_path = parallel_root / (WORKER_ID + "_source_manifest.json")
    source = _read(source_path)
    kept_ids = {row["task_id"] for row in kept}
    source["tasks"] = [row for row in source.get("tasks", []) if row.get("task_id") in kept_ids]
    lower_bound = sum(int(row.get("minimum_route_actions_lower_bound", 0) or 0) for row in kept)
    source.update({
        "task_count": len(kept),
        "routeable_task_count": len(kept),
        "skipped_task_count": 0,
        "minimum_model_request_lower_bound": lower_bound,
    })
    _write(source_path, source)
    manifest["tasks"] = kept
    manifest.update({
        "frozen_task_count": len(kept),
        "routeable_task_count": len(kept),
        "minimum_model_request_lower_bound": lower_bound,
        "source_manifest_sha256": _sha(source_path),
    })
    manifest.setdefault("events", []).append({
        "event": "provider_rejection_recovery_deduplicated_before_any_new_request",
        "excluded_task_ids": [row["task_id"] for row in excluded],
        "excluded_count": len(excluded),
        "kept_count": len(kept),
        "at_utc": datetime.now(timezone.utc).isoformat(),
    })
    _save_worker(worker_root, manifest)
    _write(worker_root / "dedup_exclusions.json", {
        "schema_version": "r4_provider_recovery_dedup_audit_v1",
        "worker_id": WORKER_ID,
        "excluded_count": len(excluded),
        "kept_count": len(kept),
        "excluded_tasks": excluded,
        "source_evidence_all_candidates_preserved": str(worker_root / "provider_rejection_evidence.json"),
        "model_requests": 0,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    })

    plan = _read(plan_path)
    worker = next((row for row in plan.get("workers", []) if row.get("worker_id") == WORKER_ID), None)
    if worker is None:
        raise RuntimeError("prepared_recovery_worker_not_registered")
    worker.update({
        "task_ids": [row["task_id"] for row in kept],
        "task_count": len(kept),
        "minimum_route_actions_lower_bound": lower_bound,
        "source_manifest_sha256": _sha(source_path),
    })
    worker.setdefault("events", []).append({
        "event": "deduplicated_prior_recovery_assignments_before_requests",
        "excluded_count": len(excluded),
        "kept_count": len(kept),
    })
    parallel._write(plan_path, plan)
    return {"excluded_count": len(excluded), "kept_count": len(kept), "excluded_tasks": excluded}


def _parent_for_task(batch_root, task):
    for run in reversed(task.get("runs", [])):
        out = _absolute(batch_root, run.get("output_dir")) if run.get("output_dir") else None
        if out and (out / "state.json").is_file() and (out / "final_audit.json").is_file():
            return out
    out = Path(task.get("output_dir", "")) if task.get("output_dir") else None
    if out and (out / "state.json").is_file() and (out / "final_audit.json").is_file():
        return out
    return None


def _confirmed_rejection(batch_root, task):
    if task.get("status") != "failed":
        return None
    parent = _parent_for_task(batch_root, task)
    if not parent:
        return None
    state = _read(parent / "state.json")
    audit = _read(parent / "final_audit.json")
    rejected = continuation._safe_provider_rejection_resume_parent(state, audit)
    if len(rejected) != 1:
        return None
    attempt = state["attempts"][rejected[0] - 1]
    provider = attempt.get("provider_record") or {}
    trace = Path(provider["trace_jsonl_path"])
    return {
        "task_id": task["task_id"],
        "seed": int(task["seed"]),
        "parent_run": str(parent),
        "parent_state_sha256": _sha(parent / "state.json"),
        "parent_final_audit_sha256": _sha(parent / "final_audit.json"),
        "parent_trace_sha256": provider["trace_jsonl_sha256"],
        "rejected_request_number": int(rejected[0]),
        "accepted_prefix_moves": len(state.get("accepted_edges", [])),
        "accepted_prefix_node": state.get("accepted_nodes", [None])[-1],
        "provider_error_messages": [
            row.get("message") if row.get("type") == "error"
            else (row.get("error") or {}).get("message")
            for row in (
                json.loads(line)
                for line in trace.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if row.get("type") in {"error", "turn.failed"}
        ],
        "source_preflight_path": task["preflight_path"],
        "source_preflight_sha256": task["preflight_sha256"],
        "map_id": state.get("map_id"),
        "route_input_protocol": state.get("route_input_protocol"),
        "codex_session_mode": state.get("codex_session_mode"),
        "unknown_parent_request_numbers_not_replayed": state.get(
            "unresolved_parent_request_numbers_not_replayed", []
        ),
        "provider_rejected_parent_request_numbers_not_replayed": rejected,
        "recovery_authorization": (
            "direct user authorization in the active conversation: complete the R4 experiment "
            "efficiently, without an artificial quota; resume only verified provider rejections"
        ),
    }


def prepare(batch_root, source_path):
    batch_root = Path(batch_root).resolve()
    source_path = Path(source_path).resolve()
    parent_path = batch_root / "manifest.json"
    plan_path = batch_root / "parallel_workers/parallel_execution_plan.json"
    worker_root = batch_root / RELATIVE_ROOT
    source_out = batch_root / "parallel_workers" / (WORKER_ID + "_source_manifest.json")
    if worker_root.exists():
        raise FileExistsError("recovery_worker_already_exists; preserve it and inspect before retry")
    if source_out.exists():
        archived = source_out.with_name(
            source_out.stem + ".incomplete_prepare_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + source_out.suffix
        )
        source_out.replace(archived)

    parent = _read(parent_path)
    plan = _read(plan_path)
    plan_hash_before = _sha(plan_path)
    frozen_source = _read(source_path)
    if _sha(source_path) != parent.get("source_manifest_sha256"):
        raise RuntimeError("frozen_source_manifest_hash_mismatch")
    frozen_by_id = {row["task_id"]: row for row in frozen_source["tasks"]}
    candidates = []
    evidence = []
    for row in parent["tasks"]:
        verified = _confirmed_rejection(batch_root, row)
        if not verified:
            continue
        source_row = frozen_by_id.get(row["task_id"])
        if not source_row:
            raise RuntimeError("provider_rejection_task_not_in_frozen_source:" + row["task_id"])
        source_preflight = Path(source_row["preflight_path"])
        if not source_preflight.is_absolute():
            source_preflight = ROOT / source_preflight
        if _sha(source_preflight) != row.get("preflight_sha256"):
            raise RuntimeError("provider_rejection_preflight_hash_mismatch:" + row["task_id"])
        source_row["preflight_path"] = str(source_preflight)
        source_row["preflight_sha256"] = _sha(source_preflight)
        candidates.append(copy.deepcopy(source_row))
        evidence.append(verified)
    if not candidates:
        raise RuntimeError("no_trace_verified_provider_rejections_to_recover")

    source_copy = copy.deepcopy(frozen_source)
    source_copy.update({
        "status": "offline_preflight_ready_no_model_requests",
        "formal_batch_started": False,
        "formal_result": False,
        "model_requests": 0,
        "task_count": len(candidates),
        "routeable_task_count": len(candidates),
        "skipped_task_count": 0,
        "skipped_tasks": [],
        "minimum_model_request_lower_bound": sum(
            int(row.get("minimum_route_actions_lower_bound", 0)) for row in candidates
        ),
        "tasks": candidates,
    })
    _write(source_out, source_copy)
    manifest = batch.prepare_batch(source_out, worker_root)
    by_id = {row["task_id"]: row for row in manifest["tasks"]}
    for row, recovery in zip(candidates, evidence):
        task = by_id[row["task_id"]]
        task["recovery_authorization"] = recovery
        task["recovery_type"] = "verified_provider_rejection_continuation"
    manifest.update({
        "experiment_id": "r4_codex_formal_provider_rejection_recovery_v1",
        "recovery_mode": "append_recovery_run",
        "recovery_authorization": "direct_user_authorized_no_artificial_project_or_usage_cap",
        "status": "offline_preflight_ready_no_model_requests",
        "events": [{
            "event": "35_provider_rejections_prepared_for_separate_prefix_continuations",
            "task_ids": [row["task_id"] for row in candidates],
            "original_rejected_calls_replayed": 0,
            "model_requests_during_prepare": 0,
            "at_utc": datetime.now(timezone.utc).isoformat(),
        }],
    })
    _write(worker_root / "manifest.json", manifest)
    _write(worker_root / "provider_rejection_evidence.json", {
        "schema_version": "r4_provider_rejection_recovery_evidence_v1",
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "parent_manifest_sha256_at_prepare": _sha(parent_path),
        "frozen_source_manifest_sha256": _sha(source_path),
        "recovery_task_count": len(evidence),
        "original_rejected_calls_replayed": 0,
        "tasks": evidence,
    })

    # Re-read before atomically extending the plan; the live aggregator does
    # not write this file, but a changed plan must never be overwritten.
    current_plan = _read(plan_path)
    if _sha(plan_path) != plan_hash_before:
        raise RuntimeError("parallel_plan_changed_during_recovery_prepare; worker remains offline")
    if any(row.get("worker_id") == WORKER_ID for row in current_plan.get("workers", [])):
        raise RuntimeError("recovery_worker_already_registered")
    worker = {
        "worker_id": WORKER_ID,
        "relative_root": RELATIVE_ROOT,
        "source_manifest": str(source_out),
        "source_manifest_sha256": _sha(source_out),
        "task_ids": [row["task_id"] for row in candidates],
        "task_count": len(candidates),
        "minimum_route_actions_lower_bound": sum(
            int(row.get("minimum_route_actions_lower_bound", 0)) for row in candidates
        ),
        "request_limit": None,
        "request_limit_policy": "no_project_level_cumulative_cap_user_authorized",
        "per_task_resumable_slice_limit": "frozen_graph_simple_path_bound",
        "aggregation_mode": "append_recovery_run",
        "service_label": SERVICE_LABEL,
        "status": "offline_preflight_ready_no_model_requests",
        "pid": None,
    }
    current_plan["workers"].append(worker)
    current_plan["worker_count"] = len(current_plan["workers"])
    current_plan.setdefault("events", []).append({
        "event": "verified_provider_rejection_recovery_worker_prepared",
        "worker_id": WORKER_ID,
        "task_count": len(candidates),
        "source_manifest_sha256": _sha(source_out),
        "model_requests_during_prepare": 0,
        "frozen_denominator_changed": False,
        "at_utc": datetime.now(timezone.utc).isoformat(),
    })
    parallel._write(plan_path, current_plan)
    return {
        "status": "offline_preflight_ready_no_model_requests",
        "worker_id": WORKER_ID,
        "task_count": len(candidates),
        "original_rejected_calls_replayed": 0,
        "worker_root": str(worker_root),
        "source_manifest_sha256": _sha(source_out),
        "plan_sha256": _sha(plan_path),
        "task_ids": [row["task_id"] for row in candidates],
    }


def _save_worker(worker_root, manifest):
    manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    _write(worker_root / "manifest.json", manifest)
    batch._render_index(manifest, worker_root)


def _append_log(worker_root, event):
    event["at_utc"] = datetime.now(timezone.utc).isoformat()
    with (worker_root / "recovery_driver.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _finalize_task(worker_root, manifest, task, output, parent_run):
    command = [sys.executable, str(FINALIZER), "--run-dir", str(output), "--parent-run", str(parent_run)]
    result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, check=False)
    state = _read(output / "state.json")
    task["requests_used"] = int(state.get("new_requests_used", 0) or 0)
    task["active_run_requests_recorded"] = task["requests_used"]
    if task.get("runs"):
        task["runs"][-1]["requests_used"] = task["requests_used"]
    if result.returncode != 0:
        task["status"] = "failed"
        task["error"] = "finalizer_failed:" + (result.stderr or "unknown")[:500]
    else:
        audit = _read(output / "final_audit.json")
        task["final_audit_path"] = str((output / "final_audit.json").relative_to(worker_root))
        task["replay_path"] = str((output / "replay.html").relative_to(worker_root))
        if audit.get("status") == "completed_route":
            task["status"] = "route_complete"
            task["error"] = None
        elif state.get("status") in {"stopped_outcome_unknown_no_retry", "request_in_progress"}:
            task["status"] = "outcome_unknown"
            task["error"] = state.get("error") or state.get("status")
        else:
            task["status"] = "failed"
            task["error"] = state.get("status") or audit.get("status")
    manifest.pop("in_flight", None)
    manifest["model_requests_used"] = sum(int(row.get("requests_used", 0) or 0) for row in manifest["tasks"])
    _append_log(worker_root, {
        "event": "provider_rejection_continuation_finalized",
        "task_id": task["task_id"],
        "status": task["status"],
        "requests_used": task["requests_used"],
        "finalizer_returncode": result.returncode,
    })
    _save_worker(worker_root, manifest)


def _mark_unknown(worker_root, manifest, task, output, request_number):
    state = _read(output / "state.json")
    trace = output / "traces" / ("r4_step_{0:04d}.jsonl".format(request_number))
    trace_sha = _sha(trace) if trace.is_file() else None
    unknown = {
        "task_id": task["task_id"],
        "expected_request_number": request_number,
        "status": "outcome_unknown_no_retry",
        "trace_path": str(trace.relative_to(worker_root)) if trace.is_file() else None,
        "trace_sha256": trace_sha,
        "worker_id": WORKER_ID,
        "provider_usage_available": False,
    }
    manifest.setdefault("unresolved_in_flight", []).append(unknown)
    manifest["requests_with_unknown_delivery"] = len(manifest["unresolved_in_flight"])
    task["status"] = "outcome_unknown"
    task["error"] = "recovery_request_outcome_unknown_preserved_without_replay"
    task["requests_used"] = int(state.get("new_requests_used", 0) or 0)
    task["active_run_requests_recorded"] = task["requests_used"]
    if task.get("runs"):
        task["runs"][-1]["requests_used"] = task["requests_used"]
    manifest.pop("in_flight", None)
    manifest["model_requests_used"] = sum(int(row.get("requests_used", 0) or 0) for row in manifest["tasks"])
    _append_log(worker_root, {"event": "recovery_delivery_unknown_preserved", **unknown})
    _save_worker(worker_root, manifest)


def preflight_worker(batch_root):
    """Preflight every eligible continuation before the recovery worker can call a model."""
    batch_root = Path(batch_root).resolve()
    worker_root = batch_root / RELATIVE_ROOT
    manifest_path = worker_root / "manifest.json"
    manifest = _read(manifest_path)
    if manifest.get("model_requests_used", 0):
        raise RuntimeError("recovery_worker_already_has_model_requests")
    dedup = _deduplicate_prepared_worker(batch_root)
    manifest = _read(manifest_path)
    evidence_doc = _read(worker_root / "provider_rejection_evidence.json")
    evidence_by_id = {row["task_id"]: row for row in evidence_doc.get("tasks", [])}
    rows = []
    for task in manifest["tasks"]:
        auth = task.get("recovery_authorization") or {}
        parent = Path(auth.get("parent_run", ""))
        result_row = {"task_id": task["task_id"], "status": "blocked", "reason": None}
        try:
            evidence = evidence_by_id.get(task["task_id"])
            if not evidence or evidence != auth:
                raise RuntimeError("provider_rejection_evidence_mismatch")
            if (
                _sha(parent / "state.json") != auth.get("parent_state_sha256")
                or _sha(parent / "final_audit.json") != auth.get("parent_final_audit_sha256")
            ):
                raise RuntimeError("provider_rejection_parent_changed_after_prepare")
            parent_state = _read(parent / "state.json")
            parent_audit = _read(parent / "final_audit.json")
            if continuation._safe_provider_rejection_resume_parent(parent_state, parent_audit) != [
                auth.get("rejected_request_number")
            ]:
                raise RuntimeError("provider_rejection_parent_no_longer_eligible")
            preflight = _absolute(ROOT, task["preflight_path"]).resolve()
            if _sha(preflight) != task["preflight_sha256"]:
                raise RuntimeError("frozen_task_preflight_changed")
            _, road_map, frozen_task, package = continuation._load_frozen_task(preflight)
            if frozen_task.task_id != task["task_id"] or road_map.map_id != parent_state.get("map_id"):
                raise RuntimeError("recovery_task_or_map_mismatch")
            if (
                parent_state.get("route_input_protocol") != "r4-step-v3"
                or parent_state.get("codex_session_mode") != "ephemeral_per_request"
            ):
                raise RuntimeError("frozen_protocol_or_session_mode_mismatch")
            request_bound = continuation._natural_simple_path_limit(package)
            output = worker_root / "tasks" / ("seed_" + str(task["seed"])) / task["task_id"] / "run_000"
            if task.get("output_dir"):
                output = Path(task["output_dir"])
            state_path = output / "state.json"
            if not state_path.is_file():
                command = [
                    sys.executable, str(STEP_RUNNER), "--preflight-only",
                    "--output-dir", str(output),
                    "--resume-from", str(parent),
                    "--preflight-path", str(preflight),
                    "--request-limit", str(request_bound),
                    "--codex-session-mode", "ephemeral_per_request",
                    "--route-input-protocol", "r4-step-v3",
                    "--user-authorized-formal-task-resume",
                ]
                completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, check=False)
                if completed.returncode != 0 or not state_path.is_file():
                    raise RuntimeError("continuation_preflight_exit_" + str(completed.returncode) + ":" + (completed.stderr or "")[:300])
            state = _read(state_path)
            if not _valid_zero_call_continuation_preflight(
                state,
                task["task_id"],
                auth["parent_state_sha256"],
                auth["parent_final_audit_sha256"],
                request_bound,
            ):
                raise RuntimeError("continuation_preflight_not_ready_authorized_zero_call_or_frozen")
            task["output_dir"] = str(output)
            task["status"] = "ready"
            task["error"] = None
            task["requests_used"] = 0
            task["runs"] = [{
                "output_dir": str(output),
                "parent_run": str(parent),
                "request_limit": request_bound,
                "requests_used": 0,
                "run_type": "trace_verified_provider_rejection_continuation",
                "provider_rejected_parent_requests_not_replayed": auth["provider_rejected_parent_request_numbers_not_replayed"],
            }]
            task["active_run_requests_recorded"] = 0
            task["replay_path"] = str((output / "replay.html").relative_to(worker_root))
            result_row.update({
                "status": "passed",
                "request_bound": request_bound,
                "output_dir": str(output),
                "state_sha256": _sha(state_path),
                "parent_rejected_request_not_replayed": auth["rejected_request_number"],
                "model_requests": 0,
            })
            _append_log(worker_root, {
                "event": "recovery_preflight_passed_no_model_call",
                "task_id": task["task_id"],
                "parent_accepted_prefix_moves": auth["accepted_prefix_moves"],
                "request_bound_graph_derived": request_bound,
                "parent_rejected_request_not_replayed": auth["rejected_request_number"],
            })
        except Exception as error:
            task["status"] = "preflight_blocked"
            task["error"] = "zero_call_recovery_preflight_failed:" + str(error)[:500]
            result_row["reason"] = str(error)[:500]
            _append_log(worker_root, {
                "event": "recovery_preflight_failed_no_model_call",
                "task_id": task["task_id"],
                "error_type": type(error).__name__,
                "error": str(error)[:300],
            })
        rows.append(result_row)
        manifest["model_requests_used"] = 0
        _save_worker(worker_root, manifest)

    passed = sum(row["status"] == "passed" for row in rows)
    blocked = len(rows) - passed
    audit = {
        "schema_version": "r4_provider_rejection_continuation_preflight_audit_v1",
        "status": "all_preflights_passed" if blocked == 0 and passed else "preflight_blocked",
        "worker_id": WORKER_ID,
        "worker_manifest_path": str(manifest_path),
        "worker_manifest_sha256": _sha(manifest_path),
        "frozen_source_manifest_sha256": evidence_doc.get("frozen_source_manifest_sha256"),
        "task_count": len(rows),
        "passed_count": passed,
        "blocked_count": blocked,
        "model_requests_during_preflight": 0,
        "retries_during_preflight": 0,
        "original_rejected_calls_replayed": 0,
        "tasks": rows,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    audit_path = worker_root / "resume_preflight_audit.json"
    _write(audit_path, audit)
    return {
        "status": audit["status"],
        "worker_id": WORKER_ID,
        "task_count": len(rows),
        "passed_count": passed,
        "blocked_count": blocked,
        "deduplicated_excluded_count": dedup["excluded_count"],
        "model_requests": 0,
        "audit_path": str(audit_path),
    }


def _register_worker_in_parent_manifest(batch_root, worker):
    """Expose a dynamically prepared recovery shard to the live root aggregator."""
    batch_root = Path(batch_root).resolve()
    parent_path = batch_root / "manifest.json"
    master = _read(parent_path)
    execution = master.get("parallel_execution")
    if not isinstance(execution, dict):
        raise RuntimeError("parent_parallel_execution_plan_missing")
    workers = execution.setdefault("workers", [])
    existing = next((row for row in workers if row.get("worker_id") == WORKER_ID), None)
    if existing is None:
        workers.append(copy.deepcopy(worker))
    else:
        existing.update(copy.deepcopy(worker))
    execution["worker_count"] = len(workers)
    execution.setdefault("events", []).append({
        "event": "provider_rejection_recovery_worker_registered_for_live_aggregation",
        "worker_id": WORKER_ID,
        "task_count": worker.get("task_count"),
        "at_utc": datetime.now(timezone.utc).isoformat(),
    })
    master.setdefault("events", []).append({
        "event": "provider_rejection_recovery_worker_registered_for_live_aggregation",
        "worker_id": WORKER_ID,
        "task_count": worker.get("task_count"),
        "at_utc": datetime.now(timezone.utc).isoformat(),
    })
    batch.write_json(parent_path, master)
    batch._render_index(master, batch_root)
    reread = _read(parent_path)
    registered = next((row for row in reread.get("parallel_execution", {}).get("workers", []) if row.get("worker_id") == WORKER_ID), None)
    if not registered or registered.get("task_ids") != worker.get("task_ids"):
        raise RuntimeError("parent_aggregator_registration_not_persisted")


def _running_worker_pid(batch_root):
    script_path = str(Path(__file__).resolve())
    batch_root_text = str(Path(batch_root).resolve())
    process_table = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False
    ).stdout
    for line in process_table.splitlines():
        if script_path in line and "--run-worker" in line and batch_root_text in line:
            try:
                return int(line.strip().split(None, 1)[0])
            except (IndexError, ValueError):
                continue
    return None


def _record_worker_process(batch_root, worker, pid):
    batch_root = Path(batch_root).resolve()
    plan_path = batch_root / "parallel_workers/parallel_execution_plan.json"
    plan = _read(plan_path)
    row = next((item for item in plan.get("workers", []) if item.get("worker_id") == WORKER_ID), None)
    if row is None:
        raise RuntimeError("prepared_recovery_worker_not_registered")
    row.update({"pid": int(pid), "status": "running", "execution_mode": "detached_caffeinated_process"})
    parallel._write(plan_path, plan)
    master_path = batch_root / "manifest.json"
    master = _read(master_path)
    embedded = next((item for item in master.get("parallel_execution", {}).get("workers", []) if item.get("worker_id") == WORKER_ID), None)
    if embedded is None:
        raise RuntimeError("recovery_worker_missing_from_parent_aggregator_plan")
    embedded.update({"pid": int(pid), "status": "running", "execution_mode": "detached_caffeinated_process"})
    batch.write_json(master_path, master)
    batch._render_index(master, batch_root)


def _run_one_task(worker_root, manifest, task):
    auth = task["recovery_authorization"]
    parent = Path(auth["parent_run"])
    if _sha(parent / "state.json") != auth["parent_state_sha256"] or _sha(parent / "final_audit.json") != auth["parent_final_audit_sha256"]:
        raise RuntimeError("provider_rejection_parent_changed_after_prepare:" + task["task_id"])
    parent_state = _read(parent / "state.json")
    parent_audit = _read(parent / "final_audit.json")
    if continuation._safe_provider_rejection_resume_parent(parent_state, parent_audit) != [auth["rejected_request_number"]]:
        raise RuntimeError("provider_rejection_parent_no_longer_eligible:" + task["task_id"])
    preflight = _absolute(ROOT, task["preflight_path"]).resolve()
    if _sha(preflight) != task["preflight_sha256"]:
        raise RuntimeError("frozen_task_preflight_changed:" + task["task_id"])
    _, road_map, frozen_task, package = continuation._load_frozen_task(preflight)
    if frozen_task.task_id != task["task_id"] or road_map.map_id != parent_state.get("map_id"):
        raise RuntimeError("recovery_task_or_map_mismatch:" + task["task_id"])
    request_bound = continuation._natural_simple_path_limit(package)
    output = worker_root / "tasks" / ("seed_" + str(task["seed"])) / task["task_id"] / "run_000"
    if task.get("output_dir"):
        output = Path(task["output_dir"])
    if not (output / "state.json").is_file():
        command = [
            sys.executable, str(STEP_RUNNER), "--preflight-only",
            "--output-dir", str(output),
            "--resume-from", str(parent),
            "--preflight-path", str(preflight),
            "--request-limit", str(request_bound),
            "--codex-session-mode", str(auth.get("codex_session_mode") or "ephemeral_per_request"),
            "--route-input-protocol", str(auth.get("route_input_protocol") or "r4-step-v3"),
            "--user-authorized-formal-task-resume",
        ]
        result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, check=False)
        if result.returncode != 0 or not (output / "state.json").is_file():
            task["status"] = "failed"
            task["error"] = "zero_call_recovery_preflight_failed:" + (result.stderr or "unknown")[:500]
            _append_log(worker_root, {"event": "recovery_preflight_failed_no_model_call", "task_id": task["task_id"], "returncode": result.returncode})
            _save_worker(worker_root, manifest)
            return
        state = _read(output / "state.json")
        if state.get("status") != "ready" or state.get("additional_model_requests_authorized") is not True:
            task["status"] = "failed"
            task["error"] = "recovery_preflight_not_authorized_or_not_ready"
            _append_log(worker_root, {"event": "recovery_preflight_gate_failed_no_model_call", "task_id": task["task_id"], "state_status": state.get("status")})
            _save_worker(worker_root, manifest)
            return
        task["output_dir"] = str(output)
        task["status"] = "active"
        task["requests_used"] = 0
        task["runs"] = [{
            "output_dir": str(output),
            "parent_run": str(parent),
            "request_limit": request_bound,
            "requests_used": 0,
            "run_type": "trace_verified_provider_rejection_continuation",
            "provider_rejected_parent_requests_not_replayed": auth["provider_rejected_parent_request_numbers_not_replayed"],
        }]
        task["active_run_requests_recorded"] = 0
        task["replay_path"] = str((output / "replay.html").relative_to(worker_root))
        _append_log(worker_root, {
            "event": "recovery_preflight_passed_no_model_call",
            "task_id": task["task_id"],
            "parent_accepted_prefix_moves": auth["accepted_prefix_moves"],
            "request_bound_graph_derived": request_bound,
            "parent_rejected_request_not_replayed": auth["rejected_request_number"],
        })
        _save_worker(worker_root, manifest)

    state_path = output / "state.json"
    state = _read(state_path)
    if state.get("status") == "request_in_progress":
        _mark_unknown(worker_root, manifest, task, output, int(state.get("new_requests_used", 0)) + 1)
        return
    if not task.get("runs"):
        task["runs"] = [{"output_dir": str(output), "parent_run": str(parent), "request_limit": request_bound, "requests_used": int(state.get("new_requests_used", 0)), "run_type": "trace_verified_provider_rejection_continuation"}]
        task["output_dir"] = str(output)
        task["status"] = "active"

    while state.get("status") == "ready":
        before = int(state.get("new_requests_used", 0) or 0)
        manifest["current_task_id"] = task["task_id"]
        manifest["in_flight"] = {
            "task_id": task["task_id"],
            "expected_request_number": before + 1,
            "task_requests_used_before": before,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        _save_worker(worker_root, manifest)
        result = subprocess.run(
            [sys.executable, str(STEP_RUNNER), "--one-step", "--output-dir", str(output)],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
        )
        state = _read(state_path)
        after = int(state.get("new_requests_used", 0) or 0)
        if state.get("status") == "request_in_progress":
            _mark_unknown(worker_root, manifest, task, output, before + 1)
            return
        if after < before or (state.get("status") == "ready" and after == before):
            # A child that exited without a durable terminal state is not safe
            # to repeat. Preserve it as unknown and let other tasks proceed.
            if state.get("pre_request_failures") and after == before:
                manifest.pop("in_flight", None)
                state["status"] = "local_pre_request_no_allowed_action"
                batch.write_json(state_path, state)
            else:
                _mark_unknown(worker_root, manifest, task, output, before + 1)
                return
        manifest.pop("in_flight", None)
        task["requests_used"] = after
        task["active_run_requests_recorded"] = after
        task["runs"][-1]["requests_used"] = after
        manifest["model_requests_used"] = sum(int(row.get("requests_used", 0) or 0) for row in manifest["tasks"])
        _append_log(worker_root, {
            "event": "recovery_direction_request_finished",
            "task_id": task["task_id"],
            "request_number": after if after > before else None,
            "returncode": result.returncode,
            "state_status": state.get("status"),
            "accepted_moves": len(state.get("accepted_edges", [])),
            "current_node": (state.get("accepted_nodes") or [None])[-1],
        })
        _save_worker(worker_root, manifest)

    if state.get("status") == "request_in_progress":
        _mark_unknown(worker_root, manifest, task, output, int(state.get("new_requests_used", 0)) + 1)
    else:
        _finalize_task(worker_root, manifest, task, output, parent)


def run_worker(batch_root):
    batch_root = Path(batch_root).resolve()
    worker_root = batch_root / RELATIVE_ROOT
    lock = acquire_execution_lock(worker_root)
    try:
        manifest_path = worker_root / "manifest.json"
        manifest = _read(manifest_path)
        if manifest.get("status") == "completed_with_audited_task_outcomes":
            return 0
        manifest["status"] = "running"
        manifest.setdefault("events", []).append({"event": "provider_rejection_recovery_worker_started", "at_utc": datetime.now(timezone.utc).isoformat()})
        _save_worker(worker_root, manifest)
        for task in manifest["tasks"]:
            if task.get("status") in {"route_complete", "failed", "preflight_blocked", "outcome_unknown"}:
                continue
            try:
                _run_one_task(worker_root, manifest, task)
            except Exception as error:
                task["status"] = "failed"
                task["error"] = "recovery_driver_error:" + str(error)[:500]
                manifest.pop("in_flight", None)
                _append_log(worker_root, {"event": "recovery_driver_error", "task_id": task.get("task_id"), "error_type": type(error).__name__, "error": str(error)[:300]})
                _save_worker(worker_root, manifest)
        all_terminal = all(row.get("status") in {"route_complete", "failed", "preflight_blocked", "outcome_unknown"} for row in manifest["tasks"])
        manifest["status"] = "completed_with_audited_task_outcomes" if all_terminal else "paused_at_checkpoint"
        manifest.setdefault("events", []).append({"event": "provider_rejection_recovery_worker_checkpoint", "all_tasks_terminal": all_terminal, "at_utc": datetime.now(timezone.utc).isoformat()})
        _save_worker(worker_root, manifest)
        return 0
    finally:
        release_execution_lock(lock)


def start_worker(batch_root, pilot_audit):
    batch_root = Path(batch_root).resolve()
    worker_root = batch_root / RELATIVE_ROOT
    plan_path = batch_root / "parallel_workers/parallel_execution_plan.json"
    plan = _read(plan_path)
    worker = next((row for row in plan.get("workers", []) if row.get("worker_id") == WORKER_ID), None)
    if not worker:
        raise RuntimeError("prepared_recovery_worker_not_registered")
    worker_manifest = _read(worker_root / "manifest.json")
    preflight_path = worker_root / "resume_preflight_audit.json"
    if not preflight_path.is_file():
        raise RuntimeError("all_task_zero_call_preflights_required_before_launch")
    preflight_audit = _read(preflight_path)
    expected_ids = {row["task_id"] for row in worker_manifest.get("tasks", [])}
    passed_ids = {row["task_id"] for row in preflight_audit.get("tasks", []) if row.get("status") == "passed"}
    if (
        preflight_audit.get("status") != "all_preflights_passed"
        or preflight_audit.get("blocked_count") != 0
        or preflight_audit.get("passed_count") != len(expected_ids)
        or passed_ids != expected_ids
        or preflight_audit.get("model_requests_during_preflight") != 0
        or preflight_audit.get("retries_during_preflight") != 0
        or preflight_audit.get("original_rejected_calls_replayed") != 0
        or preflight_audit.get("worker_manifest_sha256") != _sha(worker_root / "manifest.json")
    ):
        raise RuntimeError("recovery_zero_call_preflight_audit_incomplete_or_changed")
    source_manifest_path = _absolute(batch_root, worker.get("source_manifest", ""))
    if _sha(source_manifest_path) != worker.get("source_manifest_sha256"):
        raise RuntimeError("recovery_source_manifest_changed_before_launch")
    for task in worker_manifest.get("tasks", []):
        auth = task.get("recovery_authorization") or {}
        parent = Path(auth.get("parent_run", ""))
        if (
            task.get("status") != "ready"
            or not task.get("output_dir")
            or _sha(Path(task["output_dir"]) / "state.json") != next(
                row["state_sha256"] for row in preflight_audit["tasks"] if row["task_id"] == task["task_id"]
            )
            or _sha(parent / "state.json") != auth.get("parent_state_sha256")
            or _sha(parent / "final_audit.json") != auth.get("parent_final_audit_sha256")
        ):
            raise RuntimeError("recovery_preflight_or_parent_changed_before_launch:" + task["task_id"])
        state = _read(Path(task["output_dir"]) / "state.json")
        if not _valid_zero_call_continuation_preflight(
            state,
            task["task_id"],
            auth["parent_state_sha256"],
            auth["parent_final_audit_sha256"],
            int(next(row["request_bound"] for row in preflight_audit["tasks"] if row["task_id"] == task["task_id"])),
        ):
            raise RuntimeError("recovery_preflight_state_invalid_before_launch:" + task["task_id"])
    _register_worker_in_parent_manifest(batch_root, worker)
    if not Path(pilot_audit).is_file() or not _read(pilot_audit).get("route_complete"):
        # The frozen formal batch pilot gate must remain passed and unchanged.
        audit = _read(pilot_audit)
        if audit.get("status") != "completed_route":
            raise RuntimeError("formal_batch_pilot_gate_not_complete")
    stdout_path = batch_root / "parallel_workers/logs" / (WORKER_ID + ".log")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    existing_pid = _running_worker_pid(batch_root)
    if existing_pid:
        _record_worker_process(batch_root, worker, existing_pid)
        return {"status": "already_running", "worker_id": WORKER_ID, "pid": existing_pid, "log_path": str(stdout_path)}

    # A LaunchAgent launched directly by launchd cannot read this user's
    # Documents folder on this machine (TCC returns EPERM). Disable only the
    # failed service created for this recovery shard, then start from the
    # authorized current process and detach with caffeinate.
    domain = "gui/" + str(os.getuid())
    loaded, _ = parallel._launchd_print(SERVICE_LABEL)
    if loaded:
        subprocess.run(["launchctl", "bootout", domain + "/" + SERVICE_LABEL], check=False, capture_output=True, text=True)
    subprocess.run(["launchctl", "disable", domain + "/" + SERVICE_LABEL], check=False, capture_output=True, text=True)
    with stdout_path.open("ab", buffering=0) as log_handle:
        process = subprocess.Popen(
            [
                "/usr/bin/caffeinate", "-i", sys.executable, str(Path(__file__).resolve()),
                "--run-worker", "--batch-root", str(batch_root),
            ],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    time.sleep(0.5)
    if process.poll() is not None:
        raise RuntimeError("detached_recovery_worker_exited_at_start; inspect:" + str(stdout_path))
    _record_worker_process(batch_root, worker, process.pid)
    return {"status": "running", "worker_id": WORKER_ID, "pid": process.pid, "execution_mode": "detached_caffeinated_process", "log_path": str(stdout_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    parser.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--pilot-audit", type=Path, default=DEFAULT_PILOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--preflight-worker", action="store_true")
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--run-worker", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        result = prepare(args.batch_root, args.source_manifest)
    elif args.preflight_worker:
        result = preflight_worker(args.batch_root)
    elif args.start:
        result = start_worker(args.batch_root, args.pilot_audit)
    else:
        return run_worker(args.batch_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
