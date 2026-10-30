#!/usr/bin/env python3
"""Reconcile an interrupted legacy R4 request at a no-replay boundary."""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

APP = Path("[LOCAL_HOME]/Library/Application Support/CodexR4Formal20261027")
BATCH = APP / "outputs/route_layer/r4_codex_formal_batch_20261027_v2"
WORKER = BATCH / "parallel_workers/worker_00"
MANIFEST = WORKER / "manifest.json"
sys.path.insert(0, str(APP / "scripts"))
from run_r4_formal_chunk_driver import defer_unknown_inflight


def apply_no_project_cap(manifest, usage_percent, at):
    """Record an account snapshot without turning it into a project stop gate."""
    manifest["halt_no_more_model_requests"] = False
    manifest["halt_status"] = None
    manifest["usage_stop_percent"] = None
    manifest["authorized_stop_threshold_percent"] = None
    manifest["current_authorization_status"] = "user_authorized_no_project_level_manual_usage_cap_zero_retry"
    guard = manifest.get("usage_guard")
    if isinstance(guard, dict):
        guard["stop_threshold_percent"] = None
        guard["last_snapshot_percent"] = usage_percent
        guard["last_snapshot_at_utc"] = at
        guard["user_authorized_no_project_level_stop"] = True
    checkpoint = manifest.get("quota_control_checkpoint")
    if isinstance(checkpoint, dict):
        checkpoint.update({
            "authorization_source": "latest_direct_user_reply_current_conversation",
            "branch_status": "cleared_no_project_level_usage_stop",
            "halt_no_more_model_requests": False,
            "stop_threshold_percent": None,
            "cleared_at_utc": at,
            "usage_snapshot_percent": usage_percent,
            "user_authorization_quote": "按照最新方案，你自己去做选择，我需要符合要求的最高效率，不设置额度上限",
        })
    manifest.setdefault("authorization_history", []).append({
        "recorded_at_utc": at,
        "no_project_level_cumulative_request_cap": True,
        "no_artificial_usage_percentage_stop": True,
        "source": "latest direct user instruction in active task",
        "user_quote": "按照最新方案，你自己去做选择，我需要符合要求的最高效率，不设置额度上限",
        "provider_hard_limits_and_retry_after_still_apply": True,
    })
    manifest.setdefault("events", []).append({
        "event": "unknown_request_deferred_without_project_usage_stop",
        "at_utc": at,
        "usage_snapshot_percent": usage_percent,
        "usage_snapshot_is_observation_only": True,
        "unknown_in_flight_deferred_without_replay": True,
        "no_project_level_request_or_usage_cap": True,
        "provider_hard_limits_and_retry_after_still_apply": True,
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--usage-percent", type=int, required=True)
    args = parser.parse_args()
    if not 0 <= args.usage_percent <= 100:
        raise SystemExit("usage percent must be between 0 and 100")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not manifest.get("in_flight"):
        raise SystemExit("no in-flight request to reconcile")
    changed = defer_unknown_inflight(WORKER, MANIFEST, manifest, WORKER / "chunk_driver.jsonl")
    if not changed:
        raise SystemExit("request is not safely classifiable; no retry permitted")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    at = datetime.now(timezone.utc).isoformat()
    apply_no_project_cap(manifest, args.usage_percent, at)
    manifest["updated_at_utc"] = at
    temporary = MANIFEST.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(MANIFEST)
    print(json.dumps({
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "requests": manifest.get("model_requests_used"),
        "route_complete": sum(t.get("status") == "route_complete" for t in manifest.get("tasks", [])),
        "failed": sum(t.get("status") == "failed" for t in manifest.get("tasks", [])),
        "unknown": sum(t.get("status") == "outcome_unknown" for t in manifest.get("tasks", [])),
        "in_flight": manifest.get("in_flight"),
        "halt": manifest.get("halt_no_more_model_requests"),
        "usage_stop_percent": manifest.get("usage_stop_percent"),
        "usage_snapshot_percent": args.usage_percent,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
