#!/usr/bin/env python3
"""Run disjoint R4 route tasks in parallel and maintain the original live index.

This changes only task scheduling. Each worker uses the existing frozen
one-direction-per-request runner and its deterministic local validator.
"""

import argparse
import copy
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import run_r4_codex_formal_batch as batch  # noqa: E402

DEFAULT_ROOT = ROOT / "outputs/route_layer/r4_codex_formal_batch_20261027_v2"
DEFAULT_SOURCE = ROOT / "outputs/route_layer/r4_codex_batch_preflights_20261024_v1/manifest.json"
DEFAULT_PILOT = ROOT / "outputs/route_layer/r4_codex_formal_task9_pilot_20261027_v3/final_audit.json"
SHARD_DRIVER = ROOT / "scripts/run_r4_formal_chunk_driver.py"
MAX_TASK_REQUESTS = 512


def parse_optional_int(value: str):
    if value.strip().lower() in {"none", "unlimited", "off", "no-limit"}:
        return None
    return int(value)
TERMINAL = {"route_complete", "failed", "preflight_blocked"}


def _launchd_print(label: str) -> tuple[bool, str]:
    domain = f"gui/{os.getuid()}/{label}"
    result = subprocess.run(["launchctl", "print", domain], capture_output=True, text=True, check=False)
    return result.returncode == 0, result.stdout + result.stderr


def _launchd_active(label: str) -> bool:
    loaded, details = _launchd_print(label)
    if not loaded:
        return False
    return "state = running" in details or "state = spawn scheduled" in details


def _unknown_delivery_key(row: Dict[str, Any]) -> tuple[Any, ...]:
    """Identify one unknown delivery across repeated aggregate refreshes."""
    trace_sha = row.get("trace_sha256")
    if trace_sha:
        identity = ("sha256", str(trace_sha).lower())
    elif row.get("trace_path"):
        identity = ("path", Path(str(row["trace_path"])).as_posix())
    elif row.get("run_id"):
        identity = ("run", str(row["run_id"]))
    else:
        identity = ("owner", row.get("worker_id"))
    return (row.get("task_id"), row.get("expected_request_number"), identity)


def _resolved_path(root: Path, value: Any) -> Optional[Path]:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else Path(root) / path


def _reconciled_saved_response_evidence(
    worker_root: Path, row: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Verify an unknown ledger row was later accepted from its saved complete trace."""
    request_number = row.get("expected_request_number")
    if not isinstance(request_number, int) or request_number < 1:
        return None
    trace_path = _resolved_path(worker_root, row.get("trace_path"))
    if trace_path is None or not trace_path.is_file():
        return None
    try:
        trace_bytes = trace_path.read_bytes()
        trace_sha = hashlib.sha256(trace_bytes).hexdigest()
        if row.get("trace_sha256") and trace_sha != str(row["trace_sha256"]).lower():
            return None
        events = [
            json.loads(line)
            for line in trace_bytes.decode("utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    completions = [event for event in events if event.get("type") == "turn.completed"]
    if len(completions) != 1 or not isinstance(completions[0].get("usage"), dict):
        return None
    if any(
        event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and str(event["item"].get("type", "")).startswith("tool_")
        for event in events
    ):
        return None

    state_path = trace_path.parent.parent / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if row.get("task_id") and state.get("task_id") != row.get("task_id"):
        return None
    if request_number not in state.get("reconciled_saved_response_request_numbers", []):
        return None
    attempts = state.get("attempts", [])
    if request_number > len(attempts):
        return None
    attempt = attempts[request_number - 1]
    validation = attempt.get("validation") or {}
    local_check = validation.get("deterministic_local_check") or {}
    provider = attempt.get("provider_record") or {}
    usage = attempt.get("usage") or {}
    response = attempt.get("response") or {}
    direction = response.get("direction")
    if (
        attempt.get("request_number") != request_number
        or attempt.get("trace_sha256") != trace_sha
        or not attempt.get("reconciled_from_saved_complete_response")
        or attempt.get("status") != "accepted_legal_step"
        or not local_check.get("passed")
        or validation.get("direction_in_dynamic_schema") is not True
        or response.get("action") != "move"
        or direction not in (attempt.get("allowed_directions") or [])
        or attempt.get("selected_edge_id")
        != (attempt.get("allowed_edge_ids") or {}).get(direction)
        or provider.get("usage_available") is not True
        or attempt.get("tool_item_types")
    ):
        return None
    trace_usage = completions[0]["usage"]
    for key in ("input_tokens", "output_tokens"):
        if int(usage.get(key, -1)) != int(trace_usage.get(key, -2)):
            return None
    return {
        "request_number": request_number,
        "trace_path": str(trace_path),
        "trace_sha256": trace_sha,
        "input_tokens": int(trace_usage.get("input_tokens", 0)),
        "output_tokens": int(trace_usage.get("output_tokens", 0)),
        "direction": direction,
        "target_node_id": attempt.get("target_node_id"),
        "evidence": "state_attempt_reconciled_from_matching_complete_trace_and_local_validation",
    }


def _task_with_reconciled_saved_state(
    task: Dict[str, Any], worker_root: Path
) -> Dict[str, Any]:
    """Repair stale task status/counters from a quiescent, fully accepted run state."""
    if task.get("status") != "outcome_unknown":
        return task
    candidates = []
    if task.get("output_dir"):
        candidates.append(task.get("output_dir"))
    candidates.extend(
        run.get("output_dir")
        for run in reversed(task.get("runs", []) or [])
        if isinstance(run, dict) and run.get("output_dir")
    )
    run_dir = None
    state = None
    for candidate in candidates:
        resolved = _resolved_path(worker_root, candidate)
        if resolved is None or not (resolved / "state.json").is_file():
            continue
        try:
            candidate_state = json.loads((resolved / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        run_dir, state = resolved, candidate_state
        break
    if run_dir is None or state is None:
        return task
    attempts = state.get("attempts", [])
    new_requests = state.get("new_requests_used")
    if (
        state.get("task_id") != task.get("task_id")
        or state.get("status") not in {"ready", "route_complete"}
        or not isinstance(new_requests, int)
        or new_requests != len(attempts)
        or any(
            attempt.get("status") != "accepted_legal_step"
            or not (attempt.get("validation") or {}).get("deterministic_local_check", {}).get("passed")
            for attempt in attempts
        )
    ):
        return task
    if state.get("status") == "route_complete":
        try:
            final_audit = json.loads((run_dir / "final_audit.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return task
        if final_audit.get("status") != "completed_route":
            return task

    repaired = copy.deepcopy(task)
    repaired["status"] = state["status"]
    repaired["requests_used"] = new_requests
    repaired["active_run_requests_recorded"] = new_requests
    repaired["error"] = None
    for run in repaired.get("runs", []) or []:
        run_path = _resolved_path(worker_root, run.get("output_dir")) if isinstance(run, dict) else None
        if run_path == run_dir:
            run["requests_used"] = new_requests
            run["saved_state_reconciled"] = True
    return repaired


def _start_launch_agent(label: str, arguments: Sequence[str], stdout_path: Path, stderr_path: Path) -> Path:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / (label + ".plist")
    payload = {
        "Label": label,
        "ProgramArguments": list(arguments),
        "EnvironmentVariables": {"PATH": str(Path.home() / ".local/bin") + ":/usr/bin:/bin:/usr/sbin:/sbin"},
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    plist_path.write_bytes(plistlib.dumps(payload))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"], check=True)
    loaded, _ = _launchd_print(label)
    if loaded:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, capture_output=True, text=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=True)
    return plist_path


def _read(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, value: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def partition_tasks(tasks: Sequence[Dict[str, Any]], worker_count: int) -> List[List[Dict[str, Any]]]:
    """Greedily balance independent tasks by their frozen action lower bounds."""
    if worker_count < 1:
        raise ValueError("worker_count_must_be_positive")
    bins: List[List[Dict[str, Any]]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for task in sorted(
        tasks,
        key=lambda row: (-int(row.get("minimum_route_actions_lower_bound", 0)), str(row["task_id"])),
    ):
        index = min(range(worker_count), key=lambda item: (loads[item], len(bins[item]), item))
        bins[index].append(dict(task))
        loads[index] += int(task.get("minimum_route_actions_lower_bound", 0))
    return bins


def shard_source(source: Dict[str, Any], tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Create a zero-request source manifest for one disjoint worker shard."""
    result = copy.deepcopy(source)
    result.update({
        "status": "offline_preflight_ready_no_model_requests",
        "formal_batch_started": False,
        "formal_result": False,
        "model_requests": 0,
        "task_count": len(tasks),
        "routeable_task_count": len(tasks),
        "skipped_task_count": 0,
        "skipped_tasks": [],
        "minimum_model_request_lower_bound": sum(
            int(task.get("minimum_route_actions_lower_bound", 0)) for task in tasks
        ),
        "tasks": [dict(task) for task in tasks],
    })
    return result


def _assert_original_runner_stopped(batch_root: Path) -> None:
    result = subprocess.run(
        ["ps", "-axo", "pid=,stat=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    marker = f"--output-dir {batch_root}"
    for line in result.stdout.splitlines():
        if "run_r4_codex_formal_batch.py --execute" not in line or marker not in line:
            continue
        fields = line.strip().split(None, 2)
        if len(fields) >= 2 and "T" in fields[1]:
            raise RuntimeError("original_batch_parent_is_stopped_but_not_reconciled; terminate it safely first")
        raise RuntimeError("original_batch_runner_still_active")


def prepare_shards(
    batch_root: Path,
    source_path: Path,
    worker_count: int,
    exclude_task_ids: Sequence[str],
) -> Dict[str, Any]:
    batch_root = Path(batch_root).resolve()
    source_path = Path(source_path).resolve()
    master_path = batch_root / "manifest.json"
    master = _read(master_path)
    if master.get("in_flight"):
        raise RuntimeError("cannot_prepare_parallel_workers_with_in_flight_request")
    parallel_root = batch_root / "parallel_workers"
    if parallel_root.exists():
        raise FileExistsError("parallel_workers_directory_already_exists")

    source = _read(source_path)
    if source.get("status") != "offline_preflight_ready_no_model_requests" or source.get("model_requests") != 0:
        raise RuntimeError("frozen_source_manifest_not_zero_call_preflight")
    terminal_ids = {
        task["task_id"] for task in master.get("tasks", [])
        if task.get("status") in TERMINAL
    }
    excluded = set(exclude_task_ids)
    unknown_ids = {task["task_id"] for task in master.get("tasks", []) if task.get("status") == "outcome_unknown"}
    if not excluded.issubset(unknown_ids | terminal_ids):
        raise RuntimeError("only_terminal_or_outcome_unknown_tasks_may_be_excluded")
    remaining = [
        task for task in source.get("tasks", [])
        if task["task_id"] not in terminal_ids and task["task_id"] not in excluded
    ]
    if len(remaining) != int(master.get("routeable_task_count", 0)) - len(terminal_ids) - len(excluded - terminal_ids):
        raise RuntimeError("remaining_task_partition_count_mismatch")
    if not remaining:
        raise RuntimeError("no_remaining_tasks_to_parallelize")

    parallel_root.mkdir(parents=True)
    assignments = partition_tasks(remaining, worker_count)
    workers = []
    for index, assigned in enumerate(assignments):
        if not assigned:
            continue
        worker_name = f"worker_{index:02d}"
        worker_root = parallel_root / worker_name
        source_shard_path = parallel_root / f"{worker_name}_source_manifest.json"
        _write(source_shard_path, shard_source(source, assigned))
        batch.prepare_batch(source_shard_path, worker_root)
        # Preserve a safe, partially executed task by copying its exact state
        # into one worker shard. Unknown in-flight tasks stay explicitly
        # excluded; they are never silently replayed from their start node.
        worker_manifest_path = worker_root / "manifest.json"
        worker_manifest = _read(worker_manifest_path)
        source_by_id = {task["task_id"]: task for task in master["tasks"]}
        worker_by_id = {task["task_id"]: task for task in worker_manifest["tasks"]}
        for task_id in [task["task_id"] for task in assigned]:
            source_task = source_by_id[task_id]
            if source_task.get("status") != "ready" or not source_task.get("output_dir"):
                continue
            source_output = Path(source_task["output_dir"])
            source_state_path = source_output / "state.json"
            if not source_state_path.is_file():
                raise RuntimeError("ready_partial_task_state_missing:" + task_id)
            source_state = _read(source_state_path)
            if source_state.get("status") != "ready":
                raise RuntimeError("ready_partial_task_not_at_safe_checkpoint:" + task_id)
            worker_task_root = worker_root / "tasks" / f"seed_{int(source_task['seed'])}" / task_id
            target_output = worker_task_root / "run_000"
            if target_output.exists():
                raise FileExistsError("partial_task_worker_output_already_exists:" + task_id)
            shutil.copytree(source_output, target_output)
            worker_task = worker_by_id[task_id]
            requests_used = int(source_state.get("new_requests_used", source_task.get("requests_used", 0)) or 0)
            if requests_used != int(source_task.get("requests_used", requests_used) or 0):
                raise RuntimeError("partial_task_request_count_mismatch:" + task_id)
            worker_task.update({
                "status": "ready",
                "requests_used": requests_used,
                "output_dir": str(target_output),
                "runs": [{"output_dir": str(target_output), "parent_run": None,
                          "request_limit": MAX_TASK_REQUESTS, "requests_used": requests_used}],
                "active_run_requests_recorded": requests_used,
            })
        worker_manifest["model_requests_used"] = sum(int(task.get("requests_used", 0) or 0) for task in worker_manifest["tasks"])
        worker_manifest["events"].append({
            "event": "parallel_shard_prepared_with_partial_state_preserved",
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "partial_task_ids": [task["task_id"] for task in assigned if source_by_id[task["task_id"]].get("status") == "ready"],
        })
        _write(worker_manifest_path, worker_manifest)
        workers.append({
            "worker_id": worker_name,
            "relative_root": worker_root.relative_to(batch_root).as_posix(),
            "source_manifest": str(source_shard_path),
            "source_manifest_sha256": batch.sha256_file(source_shard_path),
            "task_ids": [task["task_id"] for task in assigned],
            "task_count": len(assigned),
            "minimum_route_actions_lower_bound": sum(
                int(task.get("minimum_route_actions_lower_bound", 0)) for task in assigned
            ),
            "request_limit": None,
            "request_limit_policy": "no_project_level_cumulative_cap",
            "per_task_resumable_slice_limit": MAX_TASK_REQUESTS,
            "status": "offline_preflight_ready_no_model_requests",
            "pid": None,
        })

    plan = {
        "schema_version": "r4_formal_parallel_execution_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "batch_root": str(batch_root),
        "parent_manifest_sha256_at_prepare": batch.sha256_file(master_path),
        "source_manifest": str(source_path),
        "source_manifest_sha256": batch.sha256_file(source_path),
        "route_input_protocol": master.get("route_input_protocol"),
        "session_policy": master.get("session_policy"),
        "worker_count": len(workers),
        "request_limit_policy": "no_project_level_cumulative_cap",
        "per_task_resumable_slice_limit": MAX_TASK_REQUESTS,
        "tasks_already_terminal": sorted(terminal_ids),
        "tasks_explicitly_excluded": sorted(excluded - terminal_ids),
        "excluded_unknown_task_ids": sorted((excluded - terminal_ids) & unknown_ids),
        "workers": workers,
        "scheduling_change_only": True,
        "model_requests_during_prepare": 0,
    }
    _write(parallel_root / "parallel_execution_plan.json", plan)
    return plan


def start_workers(
    batch_root: Path,
    usage_baseline_percent: int,
    usage_stop_percent: Optional[int],
    pilot_audit: Path,
    max_new_requests_per_invocation: int,
) -> Dict[str, Any]:
    batch_root = Path(batch_root).resolve()
    _assert_original_runner_stopped(batch_root)
    master_path = batch_root / "manifest.json"
    master = _read(master_path)
    if master.get("in_flight"):
        raise RuntimeError("master_manifest_has_in_flight_request")
    if usage_stop_percent is not None and usage_baseline_percent >= usage_stop_percent:
        raise RuntimeError("fresh_usage_snapshot_at_or_above_stop_line")
    if max_new_requests_per_invocation < 1:
        raise ValueError("max_new_requests_per_invocation_must_be_positive")
    plan_path = batch_root / "parallel_workers/parallel_execution_plan.json"
    plan = _read(plan_path)
    unknown_ids = {task["task_id"] for task in master.get("tasks", []) if task.get("status") == "outcome_unknown"}
    for task_id in plan.get("tasks_explicitly_excluded", []):
        task = next((row for row in master["tasks"] if row["task_id"] == task_id), None)
        if not task or (task.get("status") not in TERMINAL and not (task_id in unknown_ids and task.get("status") == "outcome_unknown")):
            raise RuntimeError("excluded_task_not_terminal:" + task_id)

    # No non-terminal task that has already spent model requests may be dropped.
    worker_task_ids = {task_id for worker in plan["workers"] for task_id in worker["task_ids"]}
    for task in master["tasks"]:
        if task.get("status") in TERMINAL or task["task_id"] in worker_task_ids:
            continue
        if task.get("status") == "outcome_unknown" and task["task_id"] in plan.get("excluded_unknown_task_ids", []):
            continue
        if int(task.get("requests_used", 0) or 0) > 0:
            raise RuntimeError("unassigned_partial_task:" + task["task_id"])

    runner = ROOT / "scripts/run_r4_codex_formal_batch.py"
    caffeinate = "/usr/bin/caffeinate"
    log_dir = batch_root / "parallel_workers/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    launched_workers = 0
    for worker in plan["workers"]:
        worker_root = batch_root / worker["relative_root"]
        worker_manifest = _read(worker_root / "manifest.json")
        if worker_manifest.get("in_flight"):
            raise RuntimeError("worker_has_in_flight_request:" + worker["worker_id"])
        if all(task.get("status") in TERMINAL for task in worker_manifest.get("tasks", [])):
            worker["status"] = "completed"
            continue
        if worker_manifest.get("status") not in {
            "offline_batch_preflight_ready_no_model_requests",
            "paused_at_request_chunk_limit",
            "paused_at_checkpoint",
            "paused_at_checkpoint_unknown_tasks_deferred",
            "stopped_before_request_insufficient_budget_for_remaining_minimum",
            "stopped_before_request_insufficient_budget_for_next_task",
        }:
            raise RuntimeError(
                "worker_not_at_safe_checkpoint:" + worker["worker_id"]
                + ":" + str(worker_manifest.get("status"))
            )
        label = worker.get("service_label") or f"local.codex.r4-formal-20261027-{worker['worker_id']}"
        worker["service_label"] = label
        if _launchd_active(label):
            worker["status"] = "running"
            continue
        worker_root_text = str(worker_root.resolve())
        process_table = subprocess.run(
            ["ps", "-axo", "pid=,stat=,command="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if any(
            "run_r4_formal_chunk_driver.py" in line
            and ("--batch-dir " + worker_root_text + " ") in line
            for line in process_table.splitlines()
        ):
            worker["status"] = "running"
            continue
        command = [caffeinate, "-i", sys.executable, str(SHARD_DRIVER),
                   "--batch-dir", str(worker_root), "--project-root", str(ROOT),
                   "--request-limit", str(worker["request_limit"]),
                   "--usage-baseline-percent", str(usage_baseline_percent),
                   "--usage-stop-percent", "none" if usage_stop_percent is None else str(usage_stop_percent),
                   "--pilot-audit", str(Path(pilot_audit).resolve())]
        log_path = log_dir / f"{worker['worker_id']}.log"
        plist_path = _start_launch_agent(label, command, log_path, log_path)
        worker["launch_agent_plist"] = str(plist_path)
        loaded, details = _launchd_print(label)
        if not loaded or not ("state = running" in details or "state = spawn scheduled" in details):
            # LaunchAgent state can lag its spawned process. Accept only a live
            # shard driver for this exact worker; otherwise preserve the error.
            process_table = subprocess.run(
                ["ps", "-axo", "pid=,stat=,command="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            driver_alive = any(
                "run_r4_formal_chunk_driver.py" in line
                and ("--batch-dir " + str(worker_root.resolve()) + " ") in line
                for line in process_table.splitlines()
            )
            if not driver_alive:
                raise RuntimeError("parallel_worker_launch_agent_not_active:" + worker["worker_id"])
        worker["pid"] = None
        worker["log_path"] = str(log_path)
        worker["status"] = "running"
        worker.setdefault("cycles", []).append({
            "cycle": len(worker["cycles"]) + 1,
            "service_label": label,
            "max_new_requests_per_invocation": max_new_requests_per_invocation,
            "usage_baseline_percent": usage_baseline_percent,
            "usage_stop_percent": usage_stop_percent,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        launched_workers += 1

    if launched_workers == 0 and not any(_launchd_active(worker.get("service_label", "")) for worker in plan["workers"]):
        raise RuntimeError("all_parallel_workers_already_terminal_or_unavailable")

    plan["started_at_utc"] = datetime.now(timezone.utc).isoformat()
    plan["last_cycle_max_new_requests_per_invocation"] = max_new_requests_per_invocation
    plan["usage_baseline_percent"] = usage_baseline_percent
    plan["usage_stop_percent"] = usage_stop_percent
    plan["pilot_audit_sha256"] = batch.sha256_file(Path(pilot_audit).resolve())
    _write(plan_path, plan)

    master["parallel_execution"] = plan
    master["parallel_execution"]["worker_count"] = len(plan["workers"])
    master.setdefault("events", []).append({
        "event": "independent_route_tasks_started_in_parallel",
        "worker_count": len(plan["workers"]),
        "route_input_protocol": master.get("route_input_protocol"),
        "at_utc": datetime.now(timezone.utc).isoformat(),
    })
    master.pop("in_flight", None)
    master["status"] = "running"
    master["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    batch.write_json(master_path, master)
    batch._render_index(master, batch_root)

    watcher_command = [sys.executable, str(Path(__file__).resolve()), "--watch", "--batch-root", str(batch_root)]
    watcher_log = log_dir / "master_aggregator.log"
    aggregator_label = plan.get("aggregator_service_label") or "local.codex.r4-formal-20261027-aggregator"
    plan["aggregator_service_label"] = aggregator_label
    if not _launchd_active(aggregator_label):
        aggregator_plist = _start_launch_agent(aggregator_label, watcher_command, watcher_log, watcher_log)
        plan["aggregator_plist"] = str(aggregator_plist)
    plan["aggregator_pid"] = None
    _write(plan_path, plan)
    return plan


def refresh_master(batch_root: Path) -> Dict[str, Any]:
    batch_root = Path(batch_root).resolve()
    manifest_path = batch_root / "manifest.json"
    master = _read(manifest_path)
    plan = master.get("parallel_execution")
    if not isinstance(plan, dict):
        raise RuntimeError("parallel_execution_plan_missing_from_master")
    by_id = {task["task_id"]: task for task in master.get("tasks", [])}
    worker_rows = []
    in_flight = []
    duplicate_suppressed_requests = 0
    resolved_unknown_keys = set()
    for worker in plan.get("workers", []):
        worker_manifest_path = batch_root / worker["relative_root"] / "manifest.json"
        if not worker_manifest_path.is_file():
            continue
        worker_manifest = _read(worker_manifest_path)
        for row in worker_manifest.get("unresolved_in_flight", []):
            if row.get("status") == "reconciled_complete_response_from_saved_trace":
                resolved_unknown_keys.add(
                    _unknown_delivery_key({**row, "worker_id": worker["worker_id"]})
                )
            elif _reconciled_saved_response_evidence(
                batch_root / worker["relative_root"], row
            ):
                resolved_unknown_keys.add(
                    _unknown_delivery_key({**row, "worker_id": worker["worker_id"]})
                )
    merged_unknowns = []
    unknown_keys = set()
    for existing_unknown in master.get("unresolved_in_flight", []):
        key = _unknown_delivery_key(existing_unknown)
        if key in resolved_unknown_keys:
            continue
        if key not in unknown_keys:
            merged_unknowns.append(copy.deepcopy(existing_unknown))
            unknown_keys.add(key)
    for worker in plan.get("workers", []):
        worker_root = batch_root / worker["relative_root"]
        worker_manifest_path = worker_root / "manifest.json"
        if not worker_manifest_path.is_file():
            worker_rows.append({"worker_id": worker["worker_id"], "status": "missing_manifest"})
            continue
        wm = _read(worker_manifest_path)
        worker["status"] = wm.get("status")
        worker["model_requests_used"] = wm.get("model_requests_used", 0)
        worker["current_task_id"] = wm.get("current_task_id")
        if wm.get("in_flight"):
            in_flight.append({"worker_id": worker["worker_id"], **wm["in_flight"]})
        prefix = worker["relative_root"].rstrip("/") + "/"
        for unknown in wm.get("unresolved_in_flight", []):
            key = _unknown_delivery_key({**unknown, "worker_id": worker["worker_id"]})
            if unknown.get("status") == "reconciled_complete_response_from_saved_trace":
                continue
            if key in resolved_unknown_keys:
                continue
            if key in unknown_keys:
                continue
            copied_unknown = copy.deepcopy(unknown)
            if copied_unknown.get("trace_path") and not Path(copied_unknown["trace_path"]).is_absolute():
                copied_unknown["trace_path"] = prefix + copied_unknown["trace_path"]
            copied_unknown["worker_id"] = worker["worker_id"]
            merged_unknowns.append(copied_unknown)
            unknown_keys.add(key)
        assigned_task_ids = set(worker.get("task_ids", []))
        append_recovery_run = worker.get("aggregation_mode") == "append_recovery_run"
        for source_task in wm.get("tasks", []):
            if assigned_task_ids and source_task["task_id"] not in assigned_task_ids:
                if source_task.get("status") == "duplicate_suppressed":
                    duplicate_suppressed_requests += int(source_task.get("requests_used", 0) or 0)
                continue
            source_task = _task_with_reconciled_saved_state(
                source_task, batch_root / worker["relative_root"]
            )
            target = by_id.get(source_task["task_id"])
            if target is None:
                raise RuntimeError("worker_task_not_in_parent_denominator:" + source_task["task_id"])
            prior_requests_used = int(target.get("requests_used", 0) or 0)
            prior_runs = copy.deepcopy(target.get("runs", []) or [])
            for key in (
                "status", "requests_used", "output_dir", "runs", "error",
                "active_run_requests_recorded", "final_audit_path", "replay_path",
            ):
                if append_recovery_run and key in {"requests_used", "runs"}:
                    continue
                if key in source_task:
                    target[key] = copy.deepcopy(source_task[key])
            if append_recovery_run:
                merged_runs = prior_runs
                run_indexes = {
                    row.get("output_dir"): index
                    for index, row in enumerate(merged_runs)
                    if isinstance(row, dict) and row.get("output_dir")
                }
                for run in source_task.get("runs", []) or []:
                    output_dir = run.get("output_dir")
                    if output_dir and output_dir in run_indexes:
                        merged_runs[run_indexes[output_dir]] = copy.deepcopy(run)
                    else:
                        merged_runs.append(copy.deepcopy(run))
                target["runs"] = merged_runs
            if target.get("final_audit_path") and not Path(target["final_audit_path"]).is_absolute():
                target["final_audit_path"] = prefix + target["final_audit_path"]
            if target.get("replay_path") and not Path(target["replay_path"]).is_absolute():
                target["replay_path"] = prefix + target["replay_path"]
        worker_rows.append({
            "worker_id": worker["worker_id"],
            "status": wm.get("status"),
            "model_requests_used": wm.get("model_requests_used", 0),
            "current_task_id": wm.get("current_task_id"),
            "pending_tasks": sum(row.get("status") == "pending" for row in wm.get("tasks", [])),
        })

    master["parallel_execution"]["duplicate_suppressed_request_count"] = duplicate_suppressed_requests
    for task in master["tasks"]:
        unique_runs = []
        run_indexes = {}
        for run in task.get("runs", []) or []:
            if not isinstance(run, dict):
                continue
            output_dir = run.get("output_dir")
            if output_dir and output_dir in run_indexes:
                unique_runs[run_indexes[output_dir]] = run
            else:
                if output_dir:
                    run_indexes[output_dir] = len(unique_runs)
                unique_runs.append(run)
        task["runs"] = unique_runs
        task["requests_used"] = sum(int(run.get("requests_used", 0) or 0) for run in unique_runs)
    master["model_requests_used"] = (
        sum(int(task.get("requests_used", 0) or 0) for task in master["tasks"])
        + duplicate_suppressed_requests
    )
    master["parallel_execution"]["worker_snapshots"] = worker_rows
    master["parallel_execution"]["in_flight"] = in_flight
    master["parallel_execution"]["snapshot_at_utc"] = datetime.now(timezone.utc).isoformat()
    master["unresolved_in_flight"] = merged_unknowns
    master["requests_with_unknown_delivery"] = len(merged_unknowns)
    active = [task for task in master["tasks"] if task.get("status") not in TERMINAL]
    master["current_task_id"] = active[0]["task_id"] if active else None
    all_terminal = not active
    jobs_alive = []
    for worker in plan.get("workers", []):
        label = worker.get("service_label")
        if label and _launchd_active(label):
            jobs_alive.append(label)
            continue
        # Screen-based workers are used when GUI LaunchAgents are unavailable
        # or fail to survive the calling session. Detect the detached driver
        # so the aggregate manifest does not incorrectly report a pause.
        worker_root = str((batch_root / worker.get("relative_root", "")).resolve())
        try:
            process_table = subprocess.run(
                ["ps", "-axo", "pid=,stat=,command="],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError:
            process_table = ""
        if any(
            "run_r4_formal_chunk_driver.py" in line and worker_root in line
            for line in process_table.splitlines()
        ):
            jobs_alive.append("screen:" + worker.get("worker_id", worker_root))
            continue
        pid = worker.get("pid")
        if not pid:
            continue
        try:
            os.kill(int(pid), 0)
            jobs_alive.append(str(pid))
        except OSError:
            pass
    aggregator_label = plan.get("aggregator_service_label")
    if all_terminal:
        master["status"] = "completed_with_audited_task_outcomes"
    elif jobs_alive:
        master["status"] = "running"
    else:
        master["status"] = "paused_at_parallel_checkpoint"
    master["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    batch.write_json(manifest_path, master)
    batch._render_index(master, batch_root)
    return master


def watch(batch_root: Path, interval_seconds: float = 20.0) -> None:
    while True:
        master = refresh_master(batch_root)
        if master.get("status") != "running":
            print(json.dumps({
                "status": master.get("status"),
                "model_requests_used": master.get("model_requests_used"),
                "counts": {
                    status: sum(task.get("status") == status for task in master["tasks"])
                    for status in ("route_complete", "failed", "preflight_blocked", "pending", "active")
                },
            }, ensure_ascii=False), flush=True)
            return
        time.sleep(interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", type=Path, default=DEFAULT_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true", help="create disjoint zero-request worker preflights")
    mode.add_argument("--start", action="store_true", help="start already-prepared workers and live index aggregation")
    mode.add_argument("--resume", action="store_true", help="resume workers at a persisted request-count checkpoint")
    mode.add_argument("--watch", action="store_true", help="refresh the original batch manifest and index")
    mode.add_argument("--refresh-once", action="store_true", help="refresh the original batch manifest and index once")
    parser.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--pilot-audit", type=Path, default=DEFAULT_PILOT)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--usage-baseline-percent", type=int)
    parser.add_argument("--usage-stop-percent", type=parse_optional_int, default=None)
    parser.add_argument("--max-new-requests-per-invocation", type=int, default=128)
    parser.add_argument("--exclude-task-id", action="append", default=[])
    args = parser.parse_args()
    root = args.batch_root if args.batch_root.is_absolute() else ROOT / args.batch_root
    if args.prepare:
        plan = prepare_shards(root, args.source_manifest, args.workers, args.exclude_task_id)
        print(json.dumps({
            "status": "parallel_workers_prepared_no_model_requests",
            "worker_count": plan["worker_count"],
            "routeable_remaining": sum(row["task_count"] for row in plan["workers"]),
            "minimum_action_lower_bound": sum(row["minimum_route_actions_lower_bound"] for row in plan["workers"]),
            "workers": [{key: row[key] for key in ("worker_id", "task_count", "minimum_route_actions_lower_bound", "request_limit")} for row in plan["workers"]],
        }, ensure_ascii=False, indent=2))
    elif args.start or args.resume:
        if args.usage_baseline_percent is None:
            parser.error("--start/--resume requires a fresh --usage-baseline-percent")
        plan = start_workers(
            root,
            args.usage_baseline_percent,
            args.usage_stop_percent,
            args.pilot_audit,
            args.max_new_requests_per_invocation,
        )
        print(json.dumps({
            "status": "parallel_workers_started",
            "worker_count": plan["worker_count"],
            "workers": [{key: row.get(key) for key in ("worker_id", "pid", "task_count", "request_limit")} for row in plan["workers"]],
            "aggregator_pid": plan.get("aggregator_pid"),
        }, ensure_ascii=False, indent=2))
    elif args.refresh_once:
        master = refresh_master(root)
        print(json.dumps({
            "status": master.get("status"),
            "model_requests_used": master.get("model_requests_used"),
            "counts": {status: sum(row.get("status") == status for row in master["tasks"]) for status in ("route_complete", "failed", "preflight_blocked", "pending", "active")},
        }, ensure_ascii=False, indent=2))
    else:
        watch(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
