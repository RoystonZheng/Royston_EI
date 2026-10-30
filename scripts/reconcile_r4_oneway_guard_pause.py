#!/usr/bin/env python3
"""Release a quiescent R4 batch pause only after a frozen-map edge audit passes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _launch_agent_active(label: str) -> bool:
    domain = f"gui/{os.getuid()}/{label}"
    result = subprocess.run(
        ["launchctl", "print", domain], capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and (
        "state = running" in result.stdout or "state = spawn scheduled" in result.stdout
    )


def build_release_plan(batch_root: Path, directed_audit_path: Path) -> dict[str, Any]:
    manifest_path = batch_root / "manifest.json"
    plan_path = batch_root / "parallel_workers/parallel_execution_plan.json"
    pause_path = batch_root / "oneway_guard_pause_reconciliation.json"
    manifest = read_json(manifest_path)
    plan = read_json(plan_path)
    audit = read_json(directed_audit_path)
    pause = read_json(pause_path)
    errors: list[str] = []

    if audit.get("status") != "passed":
        errors.append("directed_edge_audit_not_passed")
    if audit.get("batch_manifest_sha256") != sha256(manifest_path):
        errors.append("directed_edge_audit_manifest_hash_stale")
    if audit.get("routeable_task_denominator") != manifest.get("routeable_task_count"):
        errors.append("directed_edge_audit_denominator_mismatch")
    if audit.get("completed_routes_with_directed_edge_violation") != 0:
        errors.append("completed_route_directed_edge_violation_present")
    if audit.get("failed_legs") or audit.get("violations"):
        errors.append("directed_edge_audit_has_violations")
    if manifest.get("in_flight"):
        errors.append("parent_manifest_has_in_flight_request")
    if not manifest.get("parallel_execution"):
        errors.append("parallel_execution_plan_missing_from_parent")
    if not pause.get("launch_agents_disabled") or pause.get("active_request_processes") != 0:
        errors.append("pause_reconciliation_did_not_confirm_quiescent_checkpoint")

    process_table = subprocess.run(
        ["ps", "-axo", "pid=,stat=,command="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    workers = []
    for worker in plan.get("workers", []):
        worker_root = batch_root / worker["relative_root"]
        worker_manifest_path = worker_root / "manifest.json"
        worker_manifest = read_json(worker_manifest_path)
        if worker_manifest.get("in_flight"):
            errors.append(f"{worker['worker_id']}:worker_manifest_has_in_flight_request")
        if worker_manifest.get("status") != "paused_for_r4_oneway_guard_repair_and_full_flow_gate":
            errors.append(f"{worker['worker_id']}:unexpected_worker_pause_status")
        if worker_manifest.get("halt_no_more_model_requests") is not True:
            errors.append(f"{worker['worker_id']}:expected_guard_halt_missing")
        label = worker.get("service_label") or f"local.codex.r4-formal-20261027-{worker['worker_id']}"
        if _launch_agent_active(label):
            errors.append(f"{worker['worker_id']}:launch_agent_still_active")
        worker_root_text = str(worker_root.resolve())
        if any(
            "run_r4_formal_chunk_driver.py" in line and worker_root_text in line
            for line in process_table.splitlines()
        ):
            errors.append(f"{worker['worker_id']}:chunk_driver_still_active")
        task_statuses = [task.get("status") for task in worker_manifest.get("tasks", [])]
        runnable = any(status in {"pending", "ready", "active", "budget_paused"} for status in task_statuses)
        has_unknown = "outcome_unknown" in task_statuses
        next_status = (
            "paused_at_checkpoint"
            if runnable or not has_unknown
            else "paused_at_checkpoint_unknown_tasks_deferred"
        )
        workers.append({
            "worker_id": worker["worker_id"],
            "relative_root": worker["relative_root"],
            "manifest_path": str(worker_manifest_path),
            "manifest_sha256_before": sha256(worker_manifest_path),
            "previous_status": worker_manifest.get("status"),
            "next_status": next_status,
            "runnable_task_count": sum(status in {"pending", "ready", "active", "budget_paused"} for status in task_statuses),
            "unknown_task_count_deferred": sum(status == "outcome_unknown" for status in task_statuses),
            "halt_will_be_cleared_after_audit": True,
        })

    if manifest.get("status") != "paused_at_parallel_checkpoint":
        errors.append("parent_not_at_parallel_checkpoint")
    if manifest.get("halt_no_more_model_requests") is True:
        errors.append("parent_halt_guard_still_set")

    return {
        "schema_version": "r4_oneway_guard_release_audit_v1",
        "status": "ready_to_release" if not errors else "blocked",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "batch_root": str(batch_root),
        "parent_manifest_path": str(manifest_path),
        "parent_manifest_sha256_before": sha256(manifest_path),
        "directed_edge_audit_path": str(directed_audit_path),
        "directed_edge_audit_sha256": sha256(directed_audit_path),
        "directed_edge_audit_status": audit.get("status"),
        "states_audited": audit.get("states_audited"),
        "completed_routes_audited": audit.get("completed_routes_audited"),
        "accepted_edges_audited": audit.get("accepted_edges_audited"),
        "directed_edge_violations": audit.get("completed_routes_with_directed_edge_violation"),
        "failed_legs": audit.get("failed_legs"),
        "other_audit_violations": audit.get("violations"),
        "parent_model_requests_at_checkpoint": manifest.get("model_requests_used"),
        "workers": workers,
        "errors": errors,
        "model_requests_during_reconciliation": 0,
    }


def apply_release(batch_root: Path, directed_audit_path: Path, output_path: Path) -> dict[str, Any]:
    release = build_release_plan(batch_root, directed_audit_path)
    if release["status"] != "ready_to_release":
        raise RuntimeError("oneway_guard_release_blocked:" + ",".join(release["errors"]))

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    manifest_path = batch_root / "manifest.json"
    plan_path = batch_root / "parallel_workers/parallel_execution_plan.json"
    pause_path = batch_root / "oneway_guard_pause_reconciliation.json"
    backup_paths = [manifest_path, plan_path, pause_path]
    backup_paths.extend(Path(worker["manifest_path"]) for worker in release["workers"])
    backups = []
    for source in backup_paths:
        backup = source.with_name(source.name + f".pre_guard_release_{stamp}")
        shutil.copy2(source, backup)
        backups.append({"path": str(backup), "sha256": sha256(backup)})

    manifest = read_json(manifest_path)
    plan = read_json(plan_path)
    worker_by_id = {worker["worker_id"]: worker for worker in release["workers"]}
    for plan_worker in plan.get("workers", []):
        record = worker_by_id[plan_worker["worker_id"]]
        worker_manifest_path = Path(record["manifest_path"])
        worker_manifest = read_json(worker_manifest_path)
        prior_status = worker_manifest.get("status")
        prior_halt = worker_manifest.get("halt_no_more_model_requests")
        worker_manifest.update({
            "status": record["next_status"],
            "halt_no_more_model_requests": False,
            "halt_status": None,
            "pause_reason": None,
            "stop_reason": None,
            "oneway_guard_release": {
                "released_at_utc": now.isoformat(),
                "previous_status": prior_status,
                "previous_halt_no_more_model_requests": prior_halt,
                "directed_edge_audit_path": release["directed_edge_audit_path"],
                "directed_edge_audit_sha256": release["directed_edge_audit_sha256"],
                "status": "passed",
                "model_requests_during_release": 0,
            },
        })
        worker_manifest.setdefault("events", []).append({
            "event": "oneway_guard_released_after_frozen_map_per_segment_audit",
            "at_utc": now.isoformat(),
            "directed_edge_audit_sha256": release["directed_edge_audit_sha256"],
            "previous_status": prior_status,
            "previous_halt_no_more_model_requests": prior_halt,
            "worker_resumed_from_saved_legal_prefix": True,
            "unknown_tasks_replayed": False,
            "model_requests_during_release": 0,
        })
        worker_manifest["updated_at_utc"] = now.isoformat()
        worker_manifest_path.write_text(
            json.dumps(worker_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        plan_worker["status"] = record["next_status"]
        plan_worker["guard_release_audit_sha256"] = release["directed_edge_audit_sha256"]

    plan["oneway_guard_release"] = {
        "released_at_utc": now.isoformat(),
        "directed_edge_audit_path": release["directed_edge_audit_path"],
        "directed_edge_audit_sha256": release["directed_edge_audit_sha256"],
        "model_requests_during_release": 0,
    }
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    manifest.setdefault("events", []).append({
        "event": "oneway_guard_released_after_frozen_map_per_segment_audit",
        "at_utc": now.isoformat(),
        "directed_edge_audit_path": release["directed_edge_audit_path"],
        "directed_edge_audit_sha256": release["directed_edge_audit_sha256"],
        "states_audited": release["states_audited"],
        "completed_routes_audited": release["completed_routes_audited"],
        "accepted_edges_audited": release["accepted_edges_audited"],
        "directed_edge_violations": 0,
        "failed_legs": [],
        "unknown_tasks_replayed": False,
        "model_requests_during_release": 0,
    })
    manifest["updated_at_utc"] = now.isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pause = read_json(pause_path)
    pause["guard_release"] = {
        "at_utc": now.isoformat(),
        "status": "passed",
        "directed_edge_audit_path": release["directed_edge_audit_path"],
        "directed_edge_audit_sha256": release["directed_edge_audit_sha256"],
        "states_audited": release["states_audited"],
        "completed_routes_audited": release["completed_routes_audited"],
        "accepted_edges_audited": release["accepted_edges_audited"],
        "directed_edge_violations": 0,
        "failed_legs": [],
        "workers_released": [worker["worker_id"] for worker in release["workers"]],
        "model_requests_during_release": 0,
    }
    pause_path.write_text(json.dumps(pause, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    release["status"] = "released_at_safe_checkpoint_no_model_request"
    release["backups"] = backups
    release["parent_manifest_sha256_after"] = sha256(manifest_path)
    release["created_at_utc"] = now.isoformat()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(release, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return release


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--directed-edge-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="clear the pause only after every release check passes")
    args = parser.parse_args()
    batch_root = args.batch_root.resolve()
    audit_path = args.directed_edge_audit.resolve()
    output_path = args.output.resolve()
    if args.apply:
        result = apply_release(batch_root, audit_path, output_path)
    else:
        result = build_release_plan(batch_root, audit_path)
    print(json.dumps({
        "status": result["status"],
        "states_audited": result.get("states_audited"),
        "completed_routes_audited": result.get("completed_routes_audited"),
        "accepted_edges_audited": result.get("accepted_edges_audited"),
        "directed_edge_violations": result.get("directed_edge_violations"),
        "worker_count": len(result.get("workers", [])),
        "model_requests_during_reconciliation": result.get("model_requests_during_reconciliation", 0),
        "errors": result.get("errors", []),
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"ready_to_release", "released_at_safe_checkpoint_no_model_request"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
