#!/usr/bin/env python3
"""Serially resume the authorized frozen R4 shards without artificial usage caps."""
import argparse
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path("/Users/didi/Documents/personalcodexfile/第二次课")
BATCH = Path("/Users/didi/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/r4_codex_formal_batch_20261027_v2")
PILOT = Path("/Users/didi/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/r4_codex_formal_task9_pilot_20261027_v3/final_audit.json")
APP_SUPPORT_SCRIPTS = Path("/Users/didi/Library/Application Support/CodexR4Formal20261027/scripts")
sys.path.insert(0, str(APP_SUPPORT_SCRIPTS))
from run_r4_formal_chunk_driver import confirmed_provider_429, defer_unknown_inflight, read_manifest

AUDIT = BATCH / "resume_audit_20261028T034707Z" / "r4_serial_checkpoint_resume_20261028T045500Z"
LOG = BATCH / "parallel_workers" / "serial_supervisor_userauth.jsonl"
LOCK = AUDIT / "serial_supervisor_userauth.lock"
DRIVER = APP_SUPPORT_SCRIPTS / "run_r4_formal_chunk_driver.py"
RUNNABLE = {"pending", "ready", "active", "budget_paused"}
FAIL_CLOSED = {
    "stopped_task_finalization_error",
    "stopped_task_preflight_error",
    "stopped_task_resume_preflight_error",
    "stopped_task_preflight_hash_mismatch",
    "stopped_resume_parent_audit_missing",
    "stopped_before_request_pilot_gate_failed",
    "stopped_before_request_pilot_incomplete",
    "stopped_outcome_unknown_no_retry",
}

def stamp():
    return datetime.now(timezone.utc).isoformat()

def log(event, **fields):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at_utc": stamp(), "event": event, **fields}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

def atomic_write(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)

def active_r4_drivers():
    try:
        rows = subprocess.check_output(["ps", "-axo", "pid=,comm=,args="]).decode("utf-8", "replace").splitlines()
    except Exception as error:
        log("process_scan_error", error=repr(error))
        return ["process_scan_error"]
    found = []
    for row in rows:
        try:
            pid, comm, args = row.strip().split(None, 2)
            argv = shlex.split(args)
        except Exception:
            continue
        if not Path(comm).name.lower().startswith("python"):
            continue
        names = {Path(value).name for value in argv}
        if not ({"run_r4_formal_chunk_driver.py", "run_r4_codex_formal_batch.py"} & names):
            continue
        if not any("r4_codex_formal_batch_20261027_v2" in value for value in argv):
            continue
        found.append(row.strip())
    return found

def worker_roots():
    plan_path = BATCH / "parallel_workers" / "parallel_execution_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    rows = []
    for worker in plan.get("workers", []):
        if not worker.get("relative_root"):
            continue
        root = BATCH / worker["relative_root"]
        if (root / "manifest.json").is_file():
            rows.append((worker["worker_id"], root))
    return rows

def runnable_tasks(manifest):
    return [row for row in manifest.get("tasks", []) if row.get("status") in RUNNABLE]

def authorize_worker_no_cap(manifest_path):
    manifest = read_manifest(manifest_path)
    changed = False
    if manifest.get("authorized_request_limit") is not None:
        manifest["authorized_request_limit"] = None
        changed = True
    if manifest.get("authorized_stop_threshold_percent") is not None:
        manifest["authorized_stop_threshold_percent"] = None
        changed = True
    if manifest.get("halt_no_more_model_requests") is True:
        manifest["halt_no_more_model_requests"] = False
        manifest["halt_status"] = None
        changed = True
    if manifest.get("pause_reason") == "legacy_no_quota_runner_terminated":
        manifest["pause_reason"] = None
        changed = True
    if changed:
        at = stamp()
        manifest["current_authorization_status"] = "no_project_level_request_or_usage_stop_user_authorized"
        manifest.setdefault("authorization_history", []).append({
            "recorded_at_utc": at,
            "no_project_level_cumulative_request_cap": True,
            "no_artificial_usage_percentage_stop": True,
            "source": "latest direct user instruction in active task",
        })
        manifest["updated_at_utc"] = at
        atomic_write(manifest_path, manifest)
        log("worker_user_authorization_applied", worker_id=manifest_path.parent.name,
            request_cap=None, usage_stop_percent=None,
            manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest())
    return manifest

def refresh_root():
    command = [sys.executable, str(PROJECT / "scripts" / "r4_parallel_formal_batch.py"),
               "--batch-root", str(BATCH), "--refresh-once"]
    result = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True)
    log("root_manifest_refreshed", returncode=result.returncode, stdout=result.stdout[-2000:],
        stderr=result.stderr[-2000:])
    return result.returncode == 0

def cooldown_remaining(evidence):
    path = Path(evidence.get("trace_path", ""))
    if not path.is_absolute():
        path = BATCH / path
    if not path.is_file():
        return 180
    age = max(0.0, time.time() - path.stat().st_mtime)
    return max(0, int(180 - age))

def run_worker(worker_id, worker_root, usage_baseline):
    manifest_path = worker_root / "manifest.json"
    manifest = authorize_worker_no_cap(manifest_path)
    pending = runnable_tasks(manifest)
    if not pending:
        log("worker_skipped_no_runnable_tasks", worker_id=worker_id, status=manifest.get("status"))
        return True

    if manifest.get("status") in FAIL_CLOSED:
        log("worker_stopped_for_integrity_review", worker_id=worker_id, status=manifest.get("status"))
        return False

    while True:
        other = active_r4_drivers()
        if other:
            log("waiting_for_existing_r4_driver", worker_id=worker_id, processes=other)
            time.sleep(10)
            continue

        command = [
            sys.executable, str(DRIVER),
            "--batch-dir", str(worker_root),
            "--project-root", str(PROJECT),
            "--request-limit", "none",
            "--usage-baseline-percent", str(usage_baseline),
            "--usage-stop-percent", "none",
            "--pilot-audit", str(PILOT),
            "--max-new-requests-per-invocation", "none",
        ]
        log("worker_driver_started", worker_id=worker_id,
            runnable_task_count=len(pending), request_cap=None, usage_stop_percent=None,
            argv=command)
        runner_log = worker_root / "serial_runner_userauth.log"
        started = time.monotonic()
        with runner_log.open("a", encoding="utf-8") as output:
            child = subprocess.Popen(command, cwd=PROJECT, stdout=output, stderr=subprocess.STDOUT)
            while child.poll() is None:
                current = read_manifest(manifest_path)
                print("R4 serial heartbeat; worker={0}; requests={1}; task={2}".format(
                    worker_id, current.get("model_requests_used"), current.get("current_task_id")), flush=True)
                time.sleep(30)
            returncode = child.returncode

        manifest = read_manifest(manifest_path)
        if manifest.get("in_flight"):
            task_id = manifest["in_flight"].get("task_id")
            if defer_unknown_inflight(worker_root, manifest_path, manifest,
                                      worker_root / "chunk_driver.jsonl"):
                log("interrupted_request_preserved_unknown_no_retry", worker_id=worker_id,
                    task_id=task_id, returncode=returncode)
                manifest = read_manifest(manifest_path)

        if not refresh_root():
            log("root_refresh_failed", worker_id=worker_id)
            return False

        pending = runnable_tasks(manifest)
        log("worker_driver_exited", worker_id=worker_id, returncode=returncode,
            status=manifest.get("status"), requests=manifest.get("model_requests_used"),
            runnable=len(pending), elapsed_sec=round(time.monotonic() - started, 2))
        if not pending:
            return True
        if manifest.get("status") in FAIL_CLOSED:
            log("worker_stopped_for_integrity_review", worker_id=worker_id, status=manifest.get("status"))
            return False

        if manifest.get("status") == "stopped_provider_or_tool_contract_failure":
            evidence = confirmed_provider_429(worker_root, manifest)
            if not evidence:
                log("worker_stopped_unclassified_terminal_error", worker_id=worker_id,
                    task_id=manifest.get("current_task_id"), returncode=returncode)
                return False
            wait_seconds = cooldown_remaining(evidence)
            log("provider_cooldown_before_independent_task", worker_id=worker_id,
                **evidence, cooldown_seconds=wait_seconds, failed_task_replayed=False)
            while wait_seconds > 0:
                print("Provider cooldown; preserving failed task, continuing independent tasks in {0}s.".format(
                    wait_seconds), flush=True)
                interval = min(30, wait_seconds)
                time.sleep(interval)
                wait_seconds -= interval
            continue

        if returncode != 0 and manifest.get("status") not in {
            "paused_at_request_chunk_limit",
            "paused_at_checkpoint_unknown_tasks_deferred",
            "ready_for_serial_resume",
        }:
            log("worker_runner_nonzero_exit_requires_review", worker_id=worker_id,
                status=manifest.get("status"), returncode=returncode)
            return False
        if manifest.get("status") in {"paused_at_request_chunk_limit", "paused_at_checkpoint_unknown_tasks_deferred"}:
            continue
        if manifest.get("status") in {"running", "ready_for_serial_resume", "paused_at_checkpoint_for_serial_resume"}:
            continue
        log("worker_stopped_unexpected_status", worker_id=worker_id, status=manifest.get("status"))
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--usage-baseline-percent", type=int, required=True,
                        help="read-only snapshot for audit only; no percentage stop is applied")
    args = parser.parse_args()
    if not (0 <= args.usage_baseline_percent <= 100):
        raise SystemExit("usage_baseline_percent must be between 0 and 100")

    workers = worker_roots()
    plan = []
    for worker_id, worker_root in workers:
        manifest = read_manifest(worker_root / "manifest.json")
        plan.append({"worker_id": worker_id, "status": manifest.get("status"),
                     "runnable_tasks": len(runnable_tasks(manifest)),
                     "model_requests_used": manifest.get("model_requests_used", 0)})
    if args.plan_only:
        print(json.dumps({"model_requests": 0, "request_cap": None, "usage_stop_percent": None,
                          "workers": plan}, ensure_ascii=False, indent=2))
        return 0

    LOG.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("second_serial_supervisor_blocked")
            return 73
        log("serial_supervisor_started", workers=plan, request_cap=None, usage_stop_percent=None,
            usage_baseline_percent=args.usage_baseline_percent)
        for worker_id, worker_root in workers:
            current = read_manifest(worker_root / "manifest.json")
            if not runnable_tasks(current):
                continue
            okay = run_worker(worker_id, worker_root, args.usage_baseline_percent)
            if not okay:
                log("serial_supervisor_halted_for_integrity_review", worker_id=worker_id)
                print("Supervisor paused for integrity review at worker {0}.".format(worker_id), flush=True)
                return 0
        refresh_root()
        final = json.loads((BATCH / "manifest.json").read_text(encoding="utf-8"))
        summary = {"status": final.get("status"), "model_requests_used": final.get("model_requests_used"),
                   "task_counts": {key: sum(row.get("status") == key for row in final.get("tasks", []))
                                   for key in ["route_complete","failed","ready","outcome_unknown","pending","preflight_blocked"]},
                   "unknown_delivery_count": final.get("requests_with_unknown_delivery")}
        log("serial_supervisor_all_shards_terminal_or_skipped", summary=summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
