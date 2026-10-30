#!/usr/bin/env python3
"""Run the frozen R4 link-fallback matrix sequentially, without replaying tasks."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARENT_DEFAULT = Path(
    "[LOCAL_HOME]/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/"
    "r4_codex_formal_batch_20261027_v2/manifest.json"
)
POOL_DEFAULT = ROOT / "outputs/route_layer/r4_shared_candidate_pool_v2_20261030T1230CST/candidate_pools.jsonl"
TASK_RUNNER = ROOT / "scripts/run_r4_codex_pilot_continuation.py"
DRIVER = ROOT / "scripts/run_r4_link_fallback_task.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--parent-manifest", type=Path, default=PARENT_DEFAULT)
    parser.add_argument("--candidate-pool", type=Path, default=POOL_DEFAULT)
    parser.add_argument("--skip-task-ids", nargs="*", default=["task-0000-0001", "task-0000-0002"])
    args = parser.parse_args()

    out = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    out = out.resolve()
    parent_path = args.parent_manifest.expanduser().resolve()
    pool_path = args.candidate_pool.expanduser()
    if not pool_path.is_absolute():
        pool_path = ROOT / pool_path
    pool_path = pool_path.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit("refusing_to_reuse_nonempty_batch_output:" + str(out))
    out.mkdir(parents=True, exist_ok=True)
    lock_path = out / "RUNNER.lock"
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SystemExit("r4_link_fallback_batch_runner_already_locked")
    os.write(lock_fd, str(os.getpid()).encode())
    os.close(lock_fd)

    parent_bytes = parent_path.read_bytes()
    parent = json.loads(parent_bytes)
    pool_manifest_path = pool_path.parent / "manifest.json"
    pool_manifest = read_json(pool_manifest_path)
    if pool_manifest.get("schema_version") != "r4_shared_candidate_pool_v2" or sha256(pool_path) != pool_manifest.get("candidate_pools_sha256"):
        raise SystemExit("shared_candidate_pool_integrity_failed")

    tasks = list(parent.get("tasks", []))
    task_rows = []
    skipped = set(args.skip_task_ids)
    for item in tasks:
        task_id = str(item["task_id"])
        task_rows.append({
            "task_id": task_id,
            "source_preflight_path": item.get("preflight_path"),
            "status": "skipped_prior_attempt_no_replay" if task_id in skipped else "pending",
            "output_dir": None,
            "new_requests_used": 0,
            "accepted_legal_actions": 0,
            "route_complete": False,
        })

    manifest = {
        "schema_version": "r4_shared_link_fallback_formal_batch_v1",
        "status": "running",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_parent_manifest": str(parent_path),
        "source_parent_manifest_sha256": hashlib.sha256(parent_bytes).hexdigest(),
        "shared_candidate_pool": str(pool_path),
        "shared_candidate_pool_sha256": sha256(pool_path),
        "energy_model_version": pool_manifest.get("energy_model_version"),
        "skipped_prior_attempt_task_ids": sorted(skipped),
        "retry_policy": "no application-level request replay; unknown/failed task is sealed and runner proceeds only to a different frozen task id",
        "batch_concurrency": 1,
        "model_calls_in_preflight": 0,
        "task_count": len(task_rows),
        "task_rows": task_rows,
    }
    write_json_atomic(out / "manifest.json", manifest)
    summary_path = out / "task_progress.jsonl"

    for index, row in enumerate(task_rows):
        if row["status"] != "pending":
            append_jsonl(summary_path, {"task_id": row["task_id"], "status": row["status"]})
            continue
        task_id = row["task_id"]
        preflight = Path(row["source_preflight_path"]).expanduser()
        if not preflight.is_absolute():
            preflight = ROOT / preflight
        task_out = out / "tasks" / task_id
        task_out.mkdir(parents=True, exist_ok=False)
        row["output_dir"] = str(task_out)
        row["status"] = "preflight_running"
        write_json_atomic(out / "manifest.json", manifest)
        preflight_cmd = [
            sys.executable, str(TASK_RUNNER), "--preflight-only",
            "--preflight-path", str(preflight),
            "--output-dir", str(task_out),
            "--experiment-id", "r4_link_fallback_formal_" + task_id,
            "--route-input-protocol", "r4-step-v4-link-fallback",
            "--shared-candidate-pool", str(pool_path),
            "--record-user-authorized-formal-batch-pilot",
        ]
        preflight_result = subprocess.run(preflight_cmd, cwd=ROOT, text=True, capture_output=True)
        (task_out / "batch_preflight.stdout.json").write_text(preflight_result.stdout, encoding="utf-8")
        (task_out / "batch_preflight.stderr.txt").write_text(preflight_result.stderr, encoding="utf-8")
        if preflight_result.returncode != 0:
            row["status"] = "preflight_failed"
            row["preflight_returncode"] = preflight_result.returncode
            row["preflight_error"] = preflight_result.stderr[-2000:]
            write_json_atomic(out / "manifest.json", manifest)
            append_jsonl(summary_path, {"task_id": task_id, "status": row["status"], "preflight_returncode": preflight_result.returncode})
            continue
        row["status"] = "running"
        write_json_atomic(out / "manifest.json", manifest)
        driver_cmd = [sys.executable, str(DRIVER), "--output-dir", str(task_out)]
        driver_result = subprocess.run(driver_cmd, cwd=ROOT, text=True, capture_output=True)
        (task_out / "batch_driver.stdout.json").write_text(driver_result.stdout, encoding="utf-8")
        (task_out / "batch_driver.stderr.txt").write_text(driver_result.stderr, encoding="utf-8")
        state_path = task_out / "state.json"
        if state_path.is_file():
            state = read_json(state_path)
            row["new_requests_used"] = int(state.get("new_requests_used", 0))
            row["accepted_legal_actions"] = sum(a.get("status") == "accepted_legal_step" for a in state.get("attempts", []))
            row["route_complete"] = state.get("status") == "completed_route"
            row["terminal_state_status"] = state.get("status")
            row["state_sha256"] = sha256(state_path)
            row["usage_unknown_attempts"] = sum(
                a.get("usage", {}).get("input_tokens") is None
                or a.get("usage", {}).get("output_tokens") is None
                for a in state.get("attempts", [])
            )
            row["status"] = "route_complete" if row["route_complete"] else "incomplete_no_replay"
        else:
            row["status"] = "preflight_failed_without_state"
        row["driver_returncode"] = driver_result.returncode
        write_json_atomic(out / "manifest.json", manifest)
        append_jsonl(summary_path, {k: v for k, v in row.items() if k != "source_preflight_path"})

    manifest["status"] = "finished_available_tasks"
    manifest["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest["status_counts"] = {}
    for row in task_rows:
        manifest["status_counts"][row["status"]] = manifest["status_counts"].get(row["status"], 0) + 1
    write_json_atomic(out / "manifest.json", manifest)
    lock_path.unlink(missing_ok=True)
    print(json.dumps({"status": manifest["status"], "task_count": len(task_rows), "status_counts": manifest["status_counts"], "output_dir": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
