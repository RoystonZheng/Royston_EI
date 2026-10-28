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


def confirmed_provider_429(batch_dir, manifest):
    """Return hash-bound evidence for a classified transient provider rejection."""
    task_id = manifest.get("current_task_id")
    task = next((item for item in manifest.get("tasks", []) if item.get("task_id") == task_id), None)
    if not task or task.get("status") != "failed":
        return None
    output = Path(task.get("output_dir", ""))
    if not output.is_absolute():
        output = batch_dir / output
    state_path = output / "state.json"
    if not state_path.is_file():
        return None
    try:
        state = read_manifest(state_path)
    except (OSError, json.JSONDecodeError):
        return None
    if state.get("status") != "stopped_request_error":
        return None
    attempts = state.get("attempts", [])
    if not attempts:
        return None
    attempt = attempts[-1]
    provider = attempt.get("provider_record", {})
    trace_value = provider.get("trace_jsonl_path") or provider.get("trace_path")
    if not trace_value:
        return None
    trace_path = Path(trace_value)
    if not trace_path.is_absolute():
        trace_path = output / trace_path
    if not trace_path.is_file():
        return None
    trace_bytes = trace_path.read_bytes()
    trace_sha = hashlib.sha256(trace_bytes).hexdigest()
    expected_sha = provider.get("trace_jsonl_sha256") or provider.get("trace_sha256")
    if not expected_sha or trace_sha != expected_sha:
        return None
    try:
        events = [json.loads(line) for line in trace_bytes.decode("utf-8", errors="replace").splitlines() if line.strip()]
    except json.JSONDecodeError:
        return None
    messages = [
        json.dumps(event, ensure_ascii=False).lower()
        for event in events
        if event.get("type") in {"error", "turn.failed"}
    ]
    joined = " ".join(messages)
    if "429" in joined and "too many requests" in joined:
        failure_type = "http_429"
    elif "selected model is at capacity" in joined:
        failure_type = "model_at_capacity"
    else:
        return None
    return {
        "task_id": task_id,
        "request_number": attempt.get("request_number"),
        "trace_sha256": trace_sha,
        "trace_path": str(trace_path),
        "usage_available": bool(provider.get("usage_available")),
        "failure_type": failure_type,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--pilot-audit", type=Path, required=True)
    parser.add_argument("--request-limit", type=parse_optional_int, required=True, help="cumulative cap; use none for no project-level cap")
    parser.add_argument("--usage-baseline-percent", type=int, required=True)
    parser.add_argument("--usage-stop-percent", type=parse_optional_int, default=None)
    args = parser.parse_args()
    batch_dir = args.batch_dir.resolve()
    project_root = args.project_root.resolve()
    manifest_path = batch_dir / "manifest.json"
    runner = project_root / "scripts" / "run_r4_codex_formal_batch.py"
    log_path = batch_dir / "chunk_driver.jsonl"
    log(log_path, {"event": "driver_started", "request_limit": args.request_limit,
                   "usage_baseline_percent": args.usage_baseline_percent,
                   "usage_stop_percent": args.usage_stop_percent})
    consecutive_429 = 0

    while True:
        manifest = read_manifest(manifest_path)
        if manifest.get("halt_no_more_model_requests") is True:
            manifest["status"] = manifest.get(
                "halt_status", "stopped_by_zero_retry_guard"
            )
            manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            log(log_path, {
                "event": "driver_stopped_by_persisted_halt_guard",
                "status": manifest["status"],
            })
            return 0
        runnable = [item for item in manifest["tasks"] if item.get("status") in {"pending", "ready", "active", "budget_paused"}]
        if not runnable:
            log(log_path, {"event": "driver_stopped_no_runnable_tasks", "status": manifest.get("status"),
                           "unknown_tasks": [item["task_id"] for item in manifest["tasks"] if item.get("status") == "outcome_unknown"]})
            return 0
        command = [sys.executable, str(runner), "--execute", "--output-dir", str(batch_dir),
                   "--request-limit", "none" if args.request_limit is None else str(args.request_limit),
                   "--usage-baseline-percent", str(args.usage_baseline_percent),
                   "--usage-stop-percent", "none" if args.usage_stop_percent is None else str(args.usage_stop_percent),
                   "--pilot-audit", str(args.pilot_audit.resolve()),
                   "--max-new-requests-per-invocation", "1"]
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
            consecutive_429 = 0
            time.sleep(1)
            continue
        if status == "stopped_provider_or_tool_contract_failure":
            evidence = confirmed_provider_429(batch_dir, manifest)
            if evidence:
                consecutive_429 += 1
                cooldown = min(180 * (2 ** (consecutive_429 - 1)), 900)
                event_name = (
                    "confirmed_429_cooldown_then_continue_other_tasks"
                    if evidence["failure_type"] == "http_429"
                    else "confirmed_capacity_cooldown_then_continue_other_tasks"
                )
                log(log_path, {
                    "event": event_name,
                    **evidence,
                    "cooldown_seconds": cooldown,
                    "failed_task_replayed": False,
                })
                print("Confirmed transient provider rejection ({0}) for {1}; preserving that task and cooling down {2}s before another pending task.".format(
                    evidence["failure_type"], evidence["task_id"], cooldown), flush=True)
                time.sleep(cooldown)
                continue
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
