#!/usr/bin/env python3
"""Continue a frozen R4 worker after terminal request errors without replay.

Each child invocation uses the existing official batch runner. Failed and
outcome-unknown tasks remain terminal; after a clean checkpoint, this wrapper
starts only the next pending/ready task. A fixed provider cooldown separates
invocations after a CLI/provider failure when Retry-After is unavailable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT / "scripts"))

from run_r4_formal_chunk_driver import defer_unknown_inflight, route_safety_gate_block_reason  # noqa: E402

RUNNER = PROJECT / "scripts" / "run_r4_codex_formal_batch.py"
DRIVER = PROJECT / "scripts" / "run_r4_formal_chunk_driver.py"
RUNNABLE = {"pending", "ready", "active", "budget_paused"}
COOLDOWN = 60


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def log(path: Path, event: dict[str, Any]) -> None:
    event["at_utc"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def has_live_runner(batch_dir: Path) -> bool:
    rows = subprocess.run(
        ["ps", "-axo", "pid=,stat=,command="], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return any(
        str(batch_dir) in row
        and any(name in row for name in ("run_r4_formal_chunk_driver.py", "run_r4_codex_formal_batch.py"))
        and "run_r4_resilient_chunk_supervisor.py" not in row
        and "python3 - <<" not in row
        for row in rows
    )


def run(args: argparse.Namespace) -> int:
    batch_dir = args.batch_dir.resolve()
    project_root = args.project_root.resolve()
    manifest_path = batch_dir / "manifest.json"
    parent_path = batch_dir.parent.parent / "manifest.json"
    log_path = batch_dir / "resume_supervisor.jsonl"
    route_gate = route_safety_gate_block_reason(batch_dir)
    if route_gate:
        log(log_path, {"event": "supervisor_blocked_route_safety_gate", "reason": route_gate})
        return 2

    while True:
        parent = read(parent_path)
        if parent.get("halt_no_more_model_requests") is True:
            log(log_path, {"event": "supervisor_blocked_parent_halt", "halt_status": parent.get("halt_status")})
            return 2
        route_gate = route_safety_gate_block_reason(batch_dir)
        if route_gate:
            log(log_path, {"event": "supervisor_blocked_route_safety_gate", "reason": route_gate})
            return 2

        manifest = read(manifest_path)
        if manifest.get("in_flight"):
            if has_live_runner(batch_dir):
                time.sleep(10)
                continue
            if defer_unknown_inflight(batch_dir, manifest_path, manifest, log_path):
                continue
            log(log_path, {
                "event": "supervisor_blocked_unreconciled_in_flight",
                "in_flight": manifest.get("in_flight"),
            })
            return 3

        runnable_count = sum(task.get("status") in RUNNABLE for task in manifest.get("tasks", []))
        if runnable_count == 0:
            log(log_path, {"event": "supervisor_completed_no_runnable_tasks", "status": manifest.get("status")})
            return 0
        if has_live_runner(batch_dir):
            log(log_path, {"event": "supervisor_blocked_duplicate_runner"})
            return 4

        prior_requests = int(manifest.get("model_requests_used", 0) or 0)
        prior_statuses = [task.get("status") for task in manifest.get("tasks", [])]
        log(log_path, {
            "event": "official_chunk_driver_invoked",
            "worker_status": manifest.get("status"),
            "runnable_tasks": runnable_count,
            "requests_before": prior_requests,
            "unknown_tasks_are_terminal_and_not_replayed": True,
            "request_limit": "none",
            "usage_stop_percent": "none",
            "max_new_requests_per_invocation": args.chunk_size,
        })
        command = [
            sys.executable,
            str(RUNNER),
            "--execute",
            "--output-dir",
            str(batch_dir),
            "--request-limit",
            "none",
            "--usage-baseline-percent",
            str(args.usage_baseline_percent),
            "--usage-stop-percent",
            "none",
            "--pilot-audit",
            str(args.pilot_audit.resolve()),
            "--max-new-requests-per-invocation",
            str(args.chunk_size),
        ]
        with (batch_dir / "resilient_runner.log").open("a", encoding="utf-8") as output:
            child = subprocess.Popen(command, cwd=str(project_root), stdout=output, stderr=subprocess.STDOUT)
            while child.poll() is None:
                current = read(manifest_path)
                print(
                    "R4 resilient worker heartbeat; requests={0}; task={1}".format(
                        current.get("model_requests_used"), current.get("current_task_id")
                    ),
                    flush=True,
                )
                time.sleep(8)
            returncode = child.returncode

        after = read(manifest_path)
        if after.get("in_flight"):
            if defer_unknown_inflight(batch_dir, manifest_path, after, log_path):
                after = read(manifest_path)
            else:
                log(log_path, {
                    "event": "supervisor_blocked_unresolved_in_flight_after_child",
                    "returncode": returncode,
                    "in_flight": after.get("in_flight"),
                })
                return 3

        after_requests = int(after.get("model_requests_used", prior_requests) or 0)
        after_statuses = [task.get("status") for task in after.get("tasks", [])]
        after_runnable = sum(task.get("status") in RUNNABLE for task in after.get("tasks", []))
        status = after.get("status")
        log(log_path, {
            "event": "official_chunk_driver_finished",
            "returncode": returncode,
            "worker_status": status,
            "requests_before": prior_requests,
            "requests_after": after_requests,
            "runnable_tasks_after": after_runnable,
            "unknown_tasks_replayed": False,
        })
        if after_runnable == 0:
            continue
        made_progress = after_requests > prior_requests or after_statuses != prior_statuses
        if not made_progress:
            log(log_path, {"event": "supervisor_stopped_without_progress", "returncode": returncode, "worker_status": status})
            return returncode or 5

        if status in {"stopped_provider_or_tool_contract_failure", "stopped_before_request_provider_or_tool_contract_failure"}:
            log(log_path, {"event": "safe_next_task_cooldown", "seconds": COOLDOWN,
                           "failed_and_unknown_tasks_will_not_be_replayed": True})
            time.sleep(COOLDOWN)
        else:
            time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT)
    parser.add_argument("--pilot-audit", type=Path, required=True)
    parser.add_argument("--usage-baseline-percent", type=int, required=True)
    parser.add_argument("--chunk-size", type=int, default=512)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
