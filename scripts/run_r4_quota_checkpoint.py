#!/usr/bin/env python3
"""Run a continuous serial R4 batch with usage snapshots recorded as observations.

Read the Codex usage tool before invoking this script, then read it again after
the batch returns and pass the second value to --record-post-usage. No project
request or usage cap is applied. Successful and delivery-unknown requests are
never replayed here.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
BATCH = Path("[R4_APP_SUPPORT_OUTPUT_ROOT]")
WORKER = BATCH / "parallel_workers" / "worker_00"
MANIFEST = WORKER / "manifest.json"
PILOT = Path("[LOCAL_HOME]/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/r4_codex_formal_task9_pilot_20261027_v3/final_audit.json")
RUNNER = PROJECT / "scripts" / "run_r4_codex_formal_batch.py"
PENDING = WORKER / "quota_checkpoint_pending.json"
LEDGER = BATCH / "quota_guarded_monitor_20261028.jsonl"
LOCK = WORKER / "quota_checkpoint.lock"


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def write_manifest(value):
    temp = MANIFEST.with_suffix(".json.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(MANIFEST)


def append_event(event):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at_utc": now(), **event}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def verify_no_other_runner():
    rows = subprocess.check_output(["ps", "-axo", "pid=,args="], text=True).splitlines()
    found = []
    own = os.getpid()
    for row in rows:
        try:
            pid_text, args = row.strip().split(None, 1)
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == own:
            continue
        if (
            "run_r4_codex_formal_batch.py" in args
            or "run_r4_formal_chunk_driver.py" in args
            or "run_r4_serial_formal_supervisor.py" in args
            or "serial_supervisor_userauth.py" in args
        ):
            if str(WORKER) in args or str(BATCH) in args:
                found.append(row.strip())
    if found:
        raise RuntimeError("another R4 driver/runner is active: " + " | ".join(found))


def record_post_usage(percent: int):
    if not 0 <= percent <= 100:
        raise SystemExit("post usage must be between 0 and 100")
    if not PENDING.is_file():
        raise SystemExit("no pending batch checkpoint to reconcile")
    pending = json.loads(PENDING.read_text(encoding="utf-8"))
    manifest = read_manifest()
    halt_status = manifest.get("halt_status")
    if manifest.get("halt_no_more_model_requests") is True and halt_status not in {
        "paused_for_fresh_live_usage_gate", "stopped_at_user_authorized_100_percent_usage_line"
    }:
        raise SystemExit("manifest has a non-quota safety halt; refusing to clear it")
    event = {**pending, "post_usage_percent": percent, "post_usage_recorded_at_utc": now()}
    append_event(event)
    manifest["halt_no_more_model_requests"] = False
    manifest["halt_status"] = None
    manifest["usage_stop_percent"] = None
    manifest["authorized_stop_threshold_percent"] = None
    manifest["current_authorization_status"] = "user_authorized_no_project_level_manual_usage_cap_zero_retry"
    gate = manifest.get("external_live_account_usage_checkpoint")
    if isinstance(gate, dict):
        gate["active"] = False
        gate["post_usage_percent"] = percent
        gate["post_usage_recorded_at_utc"] = now()
    manifest["updated_at_utc"] = now()
    write_manifest(manifest)
    PENDING.unlink()
    print(json.dumps({"post_usage_percent": percent, "halt": False,
                      "requests_before": pending.get("requests_before"),
                      "requests_after": pending.get("requests_after"),
                      "manifest_sha256": sha(MANIFEST)}, ensure_ascii=False))


def run_batch(pre_usage: int):
    if not 0 <= pre_usage <= 100:
        raise SystemExit("pre usage must be between 0 and 100")
    if PENDING.exists():
        raise SystemExit("previous batch lacks post-usage reconciliation; no new batch allowed")
    manifest = read_manifest()
    if manifest.get("in_flight"):
        raise SystemExit("manifest has an in-flight request; refusing to replay or overlap")
    if manifest.get("halt_no_more_model_requests") is True and manifest.get("halt_status") not in {
        "paused_for_fresh_live_usage_gate", "paused_for_live_quota_checkpoint_owner",
        "stopped_at_user_authorized_100_percent_usage_line"
    }:
        raise SystemExit("manifest has a non-quota safety halt; refusing to clear it")
    if not PILOT.is_file():
        raise SystemExit("frozen pilot audit is missing")
    source = Path(manifest["source_manifest"])
    if not source.is_absolute():
        source = PROJECT / source
    if not source.is_file() or sha(source) != manifest.get("source_manifest_sha256"):
        raise SystemExit("frozen source manifest hash mismatch")
    verify_no_other_runner()

    before = int(manifest.get("model_requests_used", 0))
    manifest["halt_no_more_model_requests"] = False
    manifest["halt_status"] = None
    manifest["usage_stop_percent"] = None
    manifest["authorized_stop_threshold_percent"] = None
    manifest["current_authorization_status"] = "user_authorized_no_project_level_manual_usage_cap_zero_retry"
    if manifest.get("require_external_live_account_usage_gate") is True:
        manifest["external_live_account_usage_checkpoint"] = {
            "active": True,
            "caller_pid": os.getpid(),
            "pre_usage_percent": pre_usage,
            "stop_threshold_percent": None,
            "usage_snapshot_is_observation_only": True,
            "no_project_level_usage_stop": True,
            "max_new_requests_per_invocation": None,
            "authorized_request_limit": None,
            "requests_before": before,
            "recorded_at_utc": now(),
        }
    manifest["updated_at_utc"] = now()
    write_manifest(manifest)
    command = [sys.executable, str(RUNNER), "--execute", "--output-dir", str(WORKER),
               "--request-limit", "none", "--usage-baseline-percent", str(pre_usage),
               "--usage-stop-percent", "none", "--pilot-audit", str(PILOT)]
    append_event({"event": "unbounded_serial_batch_started", "pre_usage_percent": pre_usage,
                  "requests_before": before, "source_manifest_sha256": manifest.get("source_manifest_sha256"),
                  "argv": command})
    result = subprocess.run(command, cwd=PROJECT, check=False)
    after_manifest = read_manifest()
    after = int(after_manifest.get("model_requests_used", before))
    delta = after - before
    pending = {"event": "serial_batch_finished_pending_post_usage",
               "pre_usage_percent": pre_usage, "requests_before": before, "requests_after": after,
               "new_requests": delta, "returncode": result.returncode,
               "in_flight": after_manifest.get("in_flight"), "status": after_manifest.get("status"),
               "worker_manifest_sha256": sha(MANIFEST), "source_manifest_sha256": manifest.get("source_manifest_sha256")}
    PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    append_event(pending)
    print(json.dumps(pending, ensure_ascii=False))
    if delta < 0:
        raise SystemExit("safety violation: request ledger decreased during batch")
    return result.returncode


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pre-usage", type=int)
    group.add_argument("--record-post-usage", type=int)
    args = parser.parse_args()
    WORKER.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another quota checkpoint is active")
        if args.record_post_usage is not None:
            record_post_usage(args.record_post_usage)
            return 0
        return run_batch(args.pre_usage)


if __name__ == "__main__":
    raise SystemExit(main())
