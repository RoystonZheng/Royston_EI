#!/usr/bin/env python3
"""Run a formal R4 batch shard one request at a time.

Requests with no terminal response are recorded as unknown and never replayed;
other frozen tasks in the shard continue.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def parse_optional_int(value):
    if value.strip().lower() in {"none", "unlimited", "off", "no-limit"}:
        return None
    return int(value)


def read_manifest(path):
    return json.loads(path.read_text(encoding="utf-8"))


def log(path, event):
    event["at_utc"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def defer_unknown_inflight(batch_dir, manifest_path, manifest, log_path):
    flight = manifest.get("in_flight")
    if not flight:
        return False
    task = next((item for item in manifest["tasks"] if item["task_id"] == flight.get("task_id")), None)
    if task is None or not task.get("output_dir"):
        return False
    output = Path(task["output_dir"])
    state_path = output / "state.json"
    if not state_path.exists():
        return False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "request_in_progress":
        return False
    expected = int(flight.get("expected_request_number", int(state.get("new_requests_used", 0)) + 1))
    trace = output / "traces" / ("r4_step_{0:04d}.jsonl".format(expected))
    final = output / "traces" / ("r4_step_{0:04d}.final.json".format(expected))
    attempt = output / ("attempt_{0:02d}.json".format(len(state.get("attempts", [])) + 1))
    trace_sha = hashlib.sha256(trace.read_bytes()).hexdigest() if trace.exists() else None
    unknown = dict(flight)
    unknown.update({
        "status": "outcome_unknown_no_retry",
        "trace_path": str(trace.relative_to(batch_dir)) if trace.exists() else None,
        "trace_sha256": trace_sha,
        "final_artifact_exists_without_state_commit": final.exists(),
        "attempt_artifact_exists_without_state_commit": attempt.exists(),
        "recorded_successful_requests": manifest.get("model_requests_used", 0),
        "provider_usage_available": False,
    })
    prior = manifest.get("unresolved_in_flight")
    items = prior if isinstance(prior, list) else ([prior] if prior else [])
    items.append(unknown)
    manifest["unresolved_in_flight"] = items
    manifest["requests_with_unknown_delivery"] = len(items)
    manifest.pop("in_flight", None)
    task["status"] = "outcome_unknown"
    task["error"] = "request_interrupted_without_terminal_response_or_usage; preserved_without_automatic_replay"
    manifest["status"] = "paused_at_checkpoint_unknown_tasks_deferred"
    manifest.setdefault("events", []).append({
        "event": "deferred_unknown_task_to_continue_independent_tasks",
        "task_id": task["task_id"],
        "request_number": expected,
        "requests_reconciled": 0,
        "trace_sha256": trace_sha,
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "details": "No terminal response or usage; no replay. Frozen task denominator remains unchanged.",
    })
    manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(log_path, {"event": "unknown_request_deferred", "task_id": task["task_id"], "request_number": expected, "trace_sha256": trace_sha})
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--pilot-audit", type=Path, required=True)
    parser.add_argument("--request-limit", type=parse_optional_int, required=True, help="cumulative cap; use none for no project-level cap")
    parser.add_argument("--usage-baseline-percent", type=int, required=True)
    parser.add_argument("--usage-stop-percent", type=parse_optional_int, default=None)
    parser.add_argument(
        "--max-new-requests-per-invocation",
        type=parse_optional_int,
        default=1,
        help="resumable checkpoint size; use none to keep one driver alive across checkpoints",
    )
    args = parser.parse_args()
    batch_dir = args.batch_dir.resolve()
    project_root = args.project_root.resolve()
    manifest_path = batch_dir / "manifest.json"
    runner = project_root / "scripts" / "run_r4_codex_formal_batch.py"
    log_path = batch_dir / "chunk_driver.jsonl"
    log(log_path, {"event": "driver_started", "request_limit": args.request_limit,
                   "usage_baseline_percent": args.usage_baseline_percent,
                   "usage_stop_percent": args.usage_stop_percent,
                   "max_new_requests_per_invocation": args.max_new_requests_per_invocation})

    while True:
        manifest = read_manifest(manifest_path)
        runnable = [item for item in manifest["tasks"] if item.get("status") in {"pending", "ready", "active", "budget_paused"}]
        if not runnable:
            log(log_path, {"event": "driver_stopped_no_runnable_tasks", "status": manifest.get("status"),
                           "unknown_tasks": [item["task_id"] for item in manifest["tasks"] if item.get("status") == "outcome_unknown"]})
            return 0
        command = [sys.executable, str(runner), "--execute", "--output-dir", str(batch_dir),
                   "--request-limit", "none" if args.request_limit is None else str(args.request_limit),
                   "--usage-baseline-percent", str(args.usage_baseline_percent),
                   "--usage-stop-percent", "none" if args.usage_stop_percent is None else str(args.usage_stop_percent),
                   "--pilot-audit", str(args.pilot_audit.resolve())]
        if args.max_new_requests_per_invocation is not None:
            command.extend([
                "--max-new-requests-per-invocation",
                str(args.max_new_requests_per_invocation),
            ])
        started = time.monotonic()
        with (batch_dir / "chunk_runner.log").open("a", encoding="utf-8") as handle:
            process = subprocess.Popen(command, cwd=str(project_root), stdout=handle, stderr=subprocess.STDOUT)
            while process.poll() is None:
                current = read_manifest(manifest_path)
                print("R4 shard heartbeat; requests={0}; task={1}".format(
                    current.get("model_requests_used"), current.get("current_task_id")), flush=True)
                time.sleep(8)
            returncode = process.returncode
        manifest = read_manifest(manifest_path)
        if manifest.get("in_flight") and defer_unknown_inflight(batch_dir, manifest_path, manifest, log_path):
            continue
        status = manifest.get("status")
        log(log_path, {"event": "runner_chunk_finished", "returncode": returncode, "status": status,
                       "model_requests_used": manifest.get("model_requests_used"),
                       "route_complete_count": sum(item.get("status") == "route_complete" for item in manifest["tasks"]),
                       "current_task_id": manifest.get("current_task_id"),
                       "elapsed_sec": round(time.monotonic() - started, 3)})
        if returncode != 0:
            return returncode or 1
        if status == "paused_at_request_chunk_limit":
            time.sleep(1)
            continue
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
