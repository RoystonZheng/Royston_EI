#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare or resume the frozen R4 Codex stepwise batch.

The default workflow is zero-request preparation. Model requests require an
explicit --execute flag and --request-limit; pass "none" for no project-level
cumulative request cap. Each task uses the existing deterministic per-direction
validator and writes its own replay and final audit. No request is retried; an
interrupted in-flight call is marked outcome-unknown and is never replayed.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
from r4_execution_lock import acquire_execution_lock, release_execution_lock
from run_r4_codex_pilot_continuation import _load_frozen_task, _natural_simple_path_limit

DEFAULT_SOURCE = ROOT / "outputs/route_layer/r4_codex_batch_preflights_20261024_v1/manifest.json"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_codex_formal_batch_v2"
STEP_RUNNER = ROOT / "scripts/run_r4_codex_pilot_continuation.py"
FINALIZER = ROOT / "scripts/finalize_r4_codex_pilot_continuation.py"
ARGUMENT_NOT_SET = object()
_TASK_LIMIT_CACHE: Dict[str, int] = {}


def _natural_task_request_limit(task: Dict[str, Any]) -> int:
    preflight_path = Path(task["preflight_path"])
    if not preflight_path.is_absolute():
        preflight_path = ROOT / preflight_path
    key = str(preflight_path.resolve())
    if key not in _TASK_LIMIT_CACHE:
        try:
            _, _, _, package = _load_frozen_task(Path(key))
            _TASK_LIMIT_CACHE[key] = _natural_simple_path_limit(package)
        except RuntimeError as error:
            # A preflight without a map reference is rejected by the child
            # before any model request. Keep offline/fake-child fixtures
            # executable using their already-audited shortest-route lower bound.
            if str(error) != "frozen_map_reference_missing":
                raise
            lower_bound = int(task.get("minimum_route_actions_lower_bound", 0))
            if lower_bound < 1:
                raise
            _TASK_LIMIT_CACHE[key] = lower_bound
    return _TASK_LIMIT_CACHE[key]


def parse_optional_int(value: str) -> Optional[int]:
    if value.strip().lower() in {"none", "unlimited", "off", "no-limit"}:
        return None
    return int(value)
REQUIRED_PILOT_TASK_ID = "task-0000-0009"
REQUIRED_PILOT_MAP_ID = "grid-100x100-seed-0"
REQUIRED_PILOT_SCHEMA = "r4_codex_stepwise_single_task_audit_v1"
SUPPORTED_PILOT_SCHEMAS = {
    REQUIRED_PILOT_SCHEMA,
    "r4_codex_pilot_final_audit_v3",
}
REQUIRED_PILOT_SESSION_MODE = "ephemeral_per_request"
REQUIRED_ROUTE_INPUT_PROTOCOL = "r4-step-v3"
REQUIRED_PILOT_ENDPOINTS = {
    "start_node_id": "node_69_41",
    "pickup_node_id": "node_25_66",
    "dropoff_node_id": "node_42_43",
}


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _normalize_authorized_recovery_trace_slots(run_dir: Path) -> Dict[str, Any]:
    """Align recovered responses to their run-local request numbers.

    Interrupted parent deliveries are archived separately from the accepted
    attempts. A recovery response can therefore be written one slot later than
    its run-local request number. Preserve collisions and record the mapping.
    """
    run_dir = Path(run_dir).resolve()
    state_path = run_dir / "state.json"
    state = read_json(state_path)
    trace_dir = run_dir / "traces"
    archive_dir = run_dir / "recovery_audit" / "trace_slot_collisions"
    mappings = []
    for attempt in state.get("attempts", []):
        request_number = int(attempt.get("request_number", 0) or 0)
        if request_number < 1:
            continue
        if (trace_dir / f"r4_step_{request_number:04d}.jsonl").is_file():
            continue
        provider = attempt.get("provider_record", {})
        match = re.search(r"r4_step_(\d{4})\.final\.json$", str(provider.get("final_json_path", "")))
        if not match:
            continue
        actual_number = int(match.group(1))
        if actual_number <= request_number:
            continue
        if not (trace_dir / f"r4_step_{actual_number:04d}.jsonl").is_file():
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        moved = []
        for suffix in (".jsonl", ".final.json", ".prompt.txt", ".schema.json"):
            source = trace_dir / f"r4_step_{actual_number:04d}{suffix}"
            target = trace_dir / f"r4_step_{request_number:04d}{suffix}"
            if not source.is_file():
                continue
            if target.exists():
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                archived = archive_dir / f"{target.name}.pre_align_{digest[:12]}"
                if not archived.exists():
                    shutil.copy2(target, archived)
                target.unlink()
            source.replace(target)
            moved.append({"from": source.name, "to": target.name})
        for field in ("final_json_path", "prompt_path", "schema_path"):
            value = provider.get(field)
            if value:
                provider[field] = str(value).replace(
                    f"r4_step_{actual_number:04d}",
                    f"r4_step_{request_number:04d}",
                )
        mappings.append({"request_number": request_number, "previous_trace_slot": actual_number, "moved_artifacts": moved})
    if mappings:
        state.setdefault("recovery_trace_slot_normalizations", []).extend(mappings)
        write_json(state_path, state)
    return {"normalized_count": len(mappings), "mappings": mappings}


def sha256_file(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_tasks(source_manifest_path: Path) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    source_manifest_path = Path(source_manifest_path).resolve()
    source = read_json(source_manifest_path)
    if source.get("status") != "offline_preflight_ready_no_model_requests":
        raise RuntimeError("source_preflight_manifest_not_ready")
    if source.get("model_requests") != 0:
        raise RuntimeError("source_preflight_contains_model_requests")
    if source.get("routeable_task_count") != len(source.get("tasks", [])):
        raise RuntimeError("source_preflight_task_count_mismatch")
    tasks = []
    for row in source["tasks"]:
        preflight_path = Path(row["preflight_path"])
        if not preflight_path.is_absolute():
            preflight_path = ROOT / preflight_path
        if not preflight_path.is_file():
            raise RuntimeError("task_preflight_file_missing:" + row["task_id"])
        preflight = read_json(preflight_path)
        if preflight.get("status") != "offline_task_preflight_passed_no_model_request":
            raise RuntimeError("task_preflight_not_ready:" + row["task_id"])
        if preflight.get("model_requests") != 0:
            raise RuntimeError("task_preflight_has_model_requests:" + row["task_id"])
        tasks.append({
            **row,
            "preflight_path": str(preflight_path),
            "preflight_sha256": sha256_file(preflight_path),
            "status": "pending",
            "requests_used": 0,
            "output_dir": None,
            "final_audit_path": None,
            "replay_path": None,
            "error": None,
        })
    return source, tasks


def _render_index(manifest: Dict[str, Any], output_dir: Path) -> None:
    rows = []
    by_seed: Dict[int, List[Dict[str, Any]]] = {}
    for task in manifest["tasks"]:
        by_seed.setdefault(int(task["seed"]), []).append(task)
    for seed, tasks in sorted(by_seed.items()):
        rows.append(f"<h2>Seed {seed}</h2><table><thead><tr><th>任务</th><th>分组</th><th>场景</th><th>状态</th><th>请求</th><th>原地图 / 回放 / 审计</th></tr></thead><tbody>")
        for task in tasks:
            links = []
            map_preview = ROOT / "outputs/route_layer/traditional_experiment_v1" / f"seeds/seed_{int(task['seed'])}/map_preview.html"
            if map_preview.is_file():
                links.append(f'<a href="../traditional_experiment_v1/seeds/seed_{int(task["seed"])}/map_preview.html">原地图</a>')
            if task.get("replay_path"):
                links.append(f'<a href="{html.escape(task["replay_path"], quote=True)}">轨迹回放</a>')
            if task.get("final_audit_path"):
                links.append(f'<a href="{html.escape(task["final_audit_path"], quote=True)}">终审 JSON</a>')
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(task['task_id']))}</td>"
                f"<td>{html.escape(str(task['sample_group']))}</td>"
                f"<td>{html.escape(str(task.get('scenario_type') or 'order'))}</td>"
                f"<td>{html.escape(str(task['status']))}</td>"
                f"<td>{int(task.get('requests_used', 0))}</td>"
                f"<td>{' · '.join(links) or '尚未运行'}</td>"
                "</tr>"
            )
        rows.append("</tbody></table>")
    skipped_rows = []
    for task in manifest.get("skipped_tasks", []):
        skipped_rows.append(
            "<tr>"
            f"<td>{html.escape(str(task.get('seed')))}</td>"
            f"<td>{html.escape(str(task.get('task_id')))}</td>"
            f"<td>{html.escape(str(task.get('endpoint_gate')))}</td>"
            f"<td>{html.escape(str(task.get('route_search_gate')))}</td>"
            f"<td>{html.escape(', '.join(task.get('preflight_errors', [])))}</td>"
            "</tr>"
        )
    status = html.escape(str(manifest["status"]))
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>R4 Codex 路线批次</title>
<style>body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:24px;color:#172033;background:#f5f7fb}main{max-width:1500px;margin:auto}header,section{background:#fff;border:1px solid #dce3ee;border-radius:10px;padding:18px;margin:14px 0}header{background:#172554;color:white}table{border-collapse:collapse;width:100%;background:white;font-size:13px}th,td{padding:8px;border-bottom:1px solid #e2e8f0;text-align:left}th{background:#edf2fc;position:sticky;top:0}a{color:#1d4ed8} .wrap{overflow:auto}</style><main><header><h1>R4 Codex 路线批次</h1><div>状态：__STATUS__ · 请求数：__USED__/__LIMIT__ · 冻结样本：__FROZEN__ · 可路由：__COUNT__ · 跳过：__SKIPPED__ · 已终态：__DONE__/__COUNT__</div><p>每个方向由模型单独提议；本地确定性校验决定是否接纳。工具调用、非法动作和失败均保留审计。</p></header><section><h2>可路由任务</h2>__TABLES__</section><section><h2>预检跳过项</h2><table><thead><tr><th>Seed</th><th>任务</th><th>端点门禁</th><th>路线门禁</th><th>原因</th></tr></thead><tbody>__SKIPPED_ROWS__</tbody></table></section></main></html>"""
    done = sum(task["status"] in {"route_complete", "failed", "preflight_blocked"} for task in manifest["tasks"])
    document = document.replace("__STATUS__", status)
    document = document.replace("__USED__", str(manifest.get("model_requests_used", 0)))
    document = document.replace("__LIMIT__", str(manifest.get("request_limit_display") or manifest.get("authorized_request_limit") or "未授权"))
    document = document.replace("__FROZEN__", str(manifest.get("frozen_task_count", 0)))
    document = document.replace("__SKIPPED__", str(manifest.get("skipped_task_count", 0)))
    document = document.replace("__DONE__", str(done)).replace("__COUNT__", str(len(manifest["tasks"])))
    document = document.replace("__TABLES__", "\n".join(rows))
    document = document.replace("__SKIPPED_ROWS__", "\n".join(skipped_rows))
    (output_dir / "index.html").write_text(document, encoding="utf-8")


def prepare_batch(source_manifest_path: Path, output_dir: Path) -> Dict[str, Any]:
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError("batch_output_already_exists")
    source, tasks = _source_tasks(source_manifest_path)
    source_skipped = read_json(source_manifest_path).get("skipped_tasks", [])
    output_dir.mkdir(parents=True)
    manifest = {
        "schema_version": "r4_codex_formal_batch_v2",
        "experiment_id": "r4_codex_formal_batch_v2",
        "status": "offline_batch_preflight_ready_no_model_requests",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_manifest": str(Path(source_manifest_path).resolve()),
        "source_manifest_sha256": sha256_file(source_manifest_path),
        "frozen_task_count": source.get("task_count"),
        "routeable_task_count": len(tasks),
        "skipped_task_count": source.get("skipped_task_count"),
        "skipped_tasks": source_skipped,
        "minimum_model_request_lower_bound": int(source["minimum_model_request_lower_bound"]),
        "model_requests_used": 0,
        "authorized_request_limit": None,
        "authorization_history": [],
        "events": [],
        "retry_policy": "zero_retries; interrupted in-flight outcome is unknown and never automatically replayed",
        "codex_cli_session_mode": REQUIRED_PILOT_SESSION_MODE,
        "route_input_protocol": REQUIRED_ROUTE_INPUT_PROTOCOL,
        "session_policy": "one isolated Codex CLI request per route action; each prompt repeats the compact map, current route state, feature context, and v3 cost-to-go guidance",
        "request_budget_allocation": "per-task action/request ceiling is the unique frozen graph node count minus one (simple-path bound under no-revisit validation); no project-level cap when the user authorizes none; no response is automatically retried",
        "validation_policy": "local deterministic validator gates each movement; no traditional route fallback",
        "status_policy": "invalid task actions are not executed; task outcome is audited before the next independent task",
        "tasks": tasks,
    }
    write_json(output_dir / "manifest.json", manifest)
    _render_index(manifest, output_dir)
    return manifest


def _task_state(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    output_dir = task.get("output_dir")
    if not output_dir:
        return None
    state_path = Path(output_dir) / "state.json"
    return read_json(state_path) if state_path.is_file() else None


def _minimum_remaining_for_task(task: Dict[str, Any]) -> int:
    state = _task_state(task)
    if state:
        stop = state.get("stop_diagnostics") or {}
        if isinstance(stop.get("minimum_remaining_moves"), int):
            return int(stop["minimum_remaining_moves"])
        attempts = state.get("attempts", [])
        if attempts:
            local = attempts[-1].get("validation", {}).get("deterministic_local_check", {})
            value = local.get("remaining_minimum_moves_after_move")
            if isinstance(value, int):
                return value
        if state.get("status") == "route_complete":
            return 0
    return int(task["minimum_route_actions_lower_bound"])


def _command(args: List[str], cwd: Path, runner: Callable[..., Any]) -> Any:
    return runner(args, cwd=str(cwd), check=False, capture_output=True, text=True)


def _validate_pilot_audit(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {"passed": False, "reason": "pilot_audit_path_required"}
    path = Path(path).resolve()
    if not path.is_file():
        return {"passed": False, "reason": "pilot_audit_file_missing", "path": str(path)}
    try:
        audit = read_json(path)
    except (OSError, ValueError) as error:
        return {"passed": False, "reason": "pilot_audit_unreadable", "path": str(path), "detail": str(error)}

    manifest_path = path.parent / "manifest.json"
    pilot_manifest: Dict[str, Any] = {}
    if not manifest_path.is_file():
        pilot_manifest_error = "pilot_manifest_missing"
    else:
        try:
            pilot_manifest = read_json(manifest_path)
            pilot_manifest_error = None
        except (OSError, ValueError) as error:
            pilot_manifest_error = "pilot_manifest_unreadable:" + str(error)

    errors = []
    schema_version = audit.get("schema_version")
    is_finalizer_v3 = schema_version == "r4_codex_pilot_final_audit_v3"
    if schema_version not in SUPPORTED_PILOT_SCHEMAS:
        errors.append("pilot_audit_schema_mismatch")
    if audit.get("task_id") != REQUIRED_PILOT_TASK_ID:
        errors.append("pilot_task_id_mismatch")
    if audit.get("map_id") != REQUIRED_PILOT_MAP_ID:
        errors.append("pilot_map_id_mismatch")
    if pilot_manifest_error:
        errors.append(pilot_manifest_error)
    else:
        if pilot_manifest.get("experiment_id") != audit.get("experiment_id"):
            errors.append("pilot_manifest_experiment_id_mismatch")
        if pilot_manifest.get("codex_cli_session_mode") != REQUIRED_PILOT_SESSION_MODE:
            errors.append("pilot_session_mode_mismatch")
        if pilot_manifest.get("route_input_protocol") != REQUIRED_ROUTE_INPUT_PROTOCOL:
            errors.append("pilot_route_input_protocol_mismatch")
        manifest_requests = pilot_manifest.get("actual_model_requests")
        audit_requests = audit.get("actual_model_requests_this_continuation") if is_finalizer_v3 else audit.get("requests")
        if manifest_requests is not None and manifest_requests != audit_requests:
            errors.append("pilot_manifest_request_count_mismatch")
        if is_finalizer_v3 and (
            pilot_manifest.get("parent_run") is not None
            or int(pilot_manifest.get("parent_requests_already_spent", 0) or 0) != 0
        ):
            errors.append("pilot_parent_run_not_fresh")
    endpoint_aliases = {
        "start_node_id": "start_node" if is_finalizer_v3 else "start_node_id",
        "pickup_node_id": "pickup_node" if is_finalizer_v3 else "pickup_node_id",
        "dropoff_node_id": "dropoff_node" if is_finalizer_v3 else "dropoff_node_id",
    }
    for field, expected in REQUIRED_PILOT_ENDPOINTS.items():
        if audit.get(endpoint_aliases[field]) != expected:
            errors.append("pilot_" + field.removesuffix("_node_id") + "_node_mismatch")
    if is_finalizer_v3:
        route_complete = (
            audit.get("status") == "completed_route"
            and audit.get("pickup_reached") is True
            and audit.get("dropoff_reached") is True
            and audit.get("repeated_nodes") is False
            and audit.get("route_continuity_violations") == []
        )
    else:
        route_complete = audit.get("status") == "completed_route" and audit.get("route_complete") is True
    if not route_complete:
        errors.append("pilot_route_incomplete")
    request_count = audit.get("actual_model_requests_this_continuation") if is_finalizer_v3 else audit.get("requests", 0)
    accepted_step_count = audit.get("accepted_actions_this_run") if is_finalizer_v3 else audit.get("accepted_legal_steps", 0)
    if int(request_count or 0) < 1:
        errors.append("pilot_requests_missing")
    if int(accepted_step_count or 0) < 1:
        errors.append("pilot_legal_steps_missing")
    if is_finalizer_v3:
        stopped_actions = audit.get("invalid_or_stopped_actions", [])
        if not isinstance(stopped_actions, list):
            errors.append("pilot_stopped_action_audit_invalid")
            stopped_actions = []
        for stopped_action in stopped_actions:
            if not isinstance(stopped_action, dict) or type(
                stopped_action.get("action_executed")
            ) is not bool:
                errors.append("pilot_stopped_action_execution_state_incomplete")
                continue
            if stopped_action["action_executed"]:
                local_passed = stopped_action.get("deterministic_local_check_passed")
                if local_passed is False:
                    errors.append("pilot_contains_accepted_illegal_action")
                elif local_passed is not True:
                    errors.append("pilot_stopped_action_validation_state_incomplete")
        contains_accepted_illegal_action = False
    else:
        contains_accepted_illegal_action = (
            int(audit.get("illegal_actions_accepted", 0) or 0) != 0
        )
    if contains_accepted_illegal_action:
        errors.append("pilot_contains_accepted_illegal_action")
    if int(audit.get("retries", 0) or 0) != 0 or int(audit.get("tool_events", 0) or 0) != 0:
        errors.append("pilot_retry_or_tool_event_present")

    if is_finalizer_v3:
        state_path = path.parent / "state.json"
        state: Dict[str, Any] = {}
        if not state_path.is_file():
            errors.append("pilot_state_file_missing")
        else:
            try:
                state = read_json(state_path)
            except (OSError, ValueError):
                errors.append("pilot_state_file_unreadable")
        usage_totals = state.get("usage_totals") if isinstance(state.get("usage_totals"), dict) else {}
        usage = {
            "input_tokens": audit.get("input_tokens_this_continuation"),
            "output_tokens": audit.get("output_tokens_this_continuation"),
            "latency_ms": usage_totals.get("latency_ms"),
        }
        if usage_totals.get("new_requests") != request_count:
            errors.append("pilot_state_request_count_mismatch")
        metrics = {
            "accepted_prefix_distance_m": audit.get("accepted_prefix_distance_m"),
            "accepted_prefix_duration_sec": audit.get("accepted_prefix_duration_sec"),
            "accepted_prefix_energy_kwh": audit.get("accepted_prefix_energy_kwh"),
        }
    else:
        usage = audit.get("usage") if isinstance(audit.get("usage"), dict) else {}
        metrics = audit.get("accepted_prefix_metrics") if isinstance(audit.get("accepted_prefix_metrics"), dict) else {}
    for key in ("input_tokens", "output_tokens", "latency_ms"):
        if not isinstance(usage.get(key), (int, float)) or usage[key] < 0:
            errors.append("pilot_usage_incomplete:" + key)
    for key in ("accepted_prefix_distance_m", "accepted_prefix_duration_sec", "accepted_prefix_energy_kwh"):
        if not isinstance(metrics.get(key), (int, float)) or metrics[key] < 0:
            errors.append("pilot_route_metrics_incomplete:" + key)
    guard = audit.get("account_usage_guard") if isinstance(audit.get("account_usage_guard"), dict) else {}
    guard_path = path.parent / "quota_guard.json"
    if not guard and guard_path.is_file():
        try:
            guard = read_json(guard_path)
        except (OSError, ValueError):
            errors.append("pilot_account_usage_guard_unreadable")
    for key in ("baseline_weekly_used_percent", "observed_weekly_used_percent_after_request"):
        if not isinstance(guard.get(key), int):
            errors.append("pilot_account_usage_guard_incomplete:" + key)
    if guard.get("credits_balance_changed") is not False:
        errors.append("pilot_extra_credits_balance_changed_or_unverified")
    max_increase = guard.get("max_weekly_usage_increase_percentage_points")
    stop_threshold = guard.get("stop_threshold_percent")
    baseline = guard.get("baseline_weekly_used_percent")
    observed = guard.get("observed_weekly_used_percent_after_request")
    if not isinstance(max_increase, int) or not isinstance(stop_threshold, int):
        errors.append("pilot_account_usage_guard_limits_missing")
    elif isinstance(baseline, int) and isinstance(observed, int):
        if observed > stop_threshold or observed - baseline > max_increase:
            errors.append("pilot_account_usage_guard_limit_exceeded")

    return {
        "passed": not errors,
        "reason": None if not errors else errors[0],
        "errors": errors,
        "path": str(path),
        "sha256": sha256_file(path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path) if manifest_path.is_file() else None,
        "session_mode": pilot_manifest.get("codex_cli_session_mode"),
        "task_id": audit.get("task_id"),
        "map_id": audit.get("map_id"),
        "requests": request_count,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "latency_ms": usage.get("latency_ms"),
        "route_energy_kwh": metrics.get("accepted_prefix_energy_kwh"),
        "route_distance_m": metrics.get("accepted_prefix_distance_m"),
        "route_duration_sec": metrics.get("accepted_prefix_duration_sec"),
        "start_node_id": audit.get(endpoint_aliases["start_node_id"]),
        "pickup_node_id": audit.get(endpoint_aliases["pickup_node_id"]),
        "dropoff_node_id": audit.get(endpoint_aliases["dropoff_node_id"]),
        "account_usage_guard": guard,
    }


def execute_batch(
    output_dir: Path,
    *,
    authorized_request_limit: Optional[int],
    usage_baseline_percent: int,
    usage_stop_percent: Optional[int],
    pilot_audit_path: Optional[Path] = None,
    max_new_requests_per_invocation: Optional[int] = None,
    stop_after_task_id: Optional[str] = None,
    runner: Callable[..., Any] = subprocess.run,
) -> Dict[str, Any]:
    output_dir = Path(output_dir).resolve()
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("batch_preflight_required")
    manifest = read_json(manifest_path)
    if (
        manifest.get("schema_version") != "r4_codex_formal_batch_v2"
        or manifest.get("codex_cli_session_mode") != REQUIRED_PILOT_SESSION_MODE
        or manifest.get("route_input_protocol") != REQUIRED_ROUTE_INPUT_PROTOCOL
    ):
        raise RuntimeError("batch_method_version_mismatch")
    # Current direct user authorization permits R4 continuation without a
    # project-level request, account-usage, or fee cap. Keep usage as an
    # observation; provider limits and the no-replay policy remain in force.
    if authorized_request_limit is None and usage_stop_percent is None:
        prior_gate = manifest.get("external_live_account_usage_checkpoint")
        quota_halt_statuses = {
            "paused_for_fresh_live_usage_gate",
            "stopped_before_request_live_usage_gate_missing_or_stale",
            "stopped_before_request_usage_at_or_above_stop_line",
        }
        quota_pause_reasons = {
            "awaiting_next_fresh_usage_snapshot",
            "paused_for_fresh_live_usage_gate",
        }
        gate_was_active = (
            manifest.get("require_external_live_account_usage_gate") is True
            or manifest.get("usage_stop_percent") is not None
            or manifest.get("authorized_stop_threshold_percent") is not None
            or (isinstance(prior_gate, dict) and prior_gate.get("active") is True)
            or manifest.get("halt_status") in quota_halt_statuses
            or manifest.get("pause_reason") in quota_pause_reasons
            or str(manifest.get("current_authorization_status", "")).startswith(
                "user_authorized_stop_at_100_percent_account_usage"
            )
        )
        manifest["require_external_live_account_usage_gate"] = False
        manifest["usage_stop_percent"] = None
        manifest["authorized_stop_threshold_percent"] = None
        if isinstance(prior_gate, dict):
            prior_gate.update({
                "active": False,
                "stop_threshold_percent": None,
                "disabled_reason": "latest_direct_user_authorized_no_manual_usage_stop",
                "usage_snapshot_is_observation_only": True,
            })
        halt_status = manifest.get("halt_status")
        if halt_status in quota_halt_statuses or (
            manifest.get("halt_no_more_model_requests") is True
            and halt_status is None
            and gate_was_active
        ):
            manifest["halt_no_more_model_requests"] = False
            manifest["halt_status"] = None
        if manifest.get("pause_reason") in quota_pause_reasons:
            manifest["pause_reason"] = None
        if manifest.get("status") in quota_halt_statuses:
            manifest["status"] = "ready_for_serial_resume"
        checkpoint = manifest.get("quota_control_checkpoint")
        if isinstance(checkpoint, dict):
            checkpoint.update({
                "branch_status": "cleared_no_project_level_usage_stop",
                "halt_no_more_model_requests": False,
                "stop_threshold_percent": None,
                "provider_hard_limits_and_retry_after_still_apply": True,
            })
        if gate_was_active:
            manifest["current_authorization_status"] = "no_project_level_request_or_usage_stop_user_authorized"
        if gate_was_active:
            manifest.setdefault("events", []).append({
                "event": "external_usage_stop_gate_disabled_by_latest_direct_user_authorization",
                "authorization_source": "latest_direct_user_reply_in_active_conversation",
                "usage_snapshot_is_observation_only": True,
                "provider_hard_limits_and_retry_after_still_apply": True,
                "at_utc": datetime.now(timezone.utc).isoformat(),
            })
        if manifest.get("halt_no_more_model_requests") is True:
            # A provider, schema, hash, or finalization halt is not a quota
            # gate and must survive the no-cap authorization unchanged.
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            return manifest
    if stop_after_task_id is not None and not any(
        item.get("task_id") == stop_after_task_id for item in manifest.get("tasks", [])
    ):
        raise ValueError("stop_after_task_id_not_in_frozen_batch")
    if manifest.get("in_flight"):
        flight = manifest["in_flight"]
        in_flight_task = next((item for item in manifest["tasks"] if item["task_id"] == flight.get("task_id")), None)
        state = _task_state(in_flight_task) if in_flight_task else None
        if not state or state.get("status") == "request_in_progress":
            manifest["status"] = "stopped_outcome_unknown_no_retry"
            if in_flight_task:
                in_flight_task["status"] = "outcome_unknown"
                in_flight_task["error"] = "process_interrupted_during_or_before_request_state_commit"
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            return manifest
        # A completed child state is durable evidence that the step result was
        # applied. Reconcile it before continuing; never issue that step again.
        before = int(flight.get("task_requests_used_before", 0))
        after = int(state.get("new_requests_used", 0))
        delta = max(0, after - before)
        in_flight_task["requests_used"] = int(in_flight_task.get("requests_used", 0)) + delta
        if in_flight_task.get("runs"):
            in_flight_task["runs"][-1]["requests_used"] = after
        manifest["model_requests_used"] = sum(int(item.get("requests_used", 0)) for item in manifest["tasks"])
        manifest.setdefault("events", []).append({
            "event": "recovered_committed_step_after_orchestrator_interruption",
            "task_id": in_flight_task["task_id"],
            "requests_reconciled": delta,
            "at_utc": datetime.now(timezone.utc).isoformat(),
        })
        if state.get("status") == "ready":
            in_flight_task["status"] = "ready"
        elif state.get("status") == "route_complete":
            in_flight_task["status"] = "route_complete"
        elif state.get("status") in {
            "authorized_call_limit_exhausted",
            "insufficient_authorized_budget_for_remaining_route",
            "quota_cap_reached",
        }:
            in_flight_task["status"] = "budget_paused"
        else:
            in_flight_task["status"] = "failed"
            in_flight_task["error"] = state.get("status")
        if in_flight_task["status"] in {"route_complete", "budget_paused", "failed"}:
            output_path = Path(in_flight_task["output_dir"])
            finalizer_cmd = [sys.executable, str(FINALIZER), "--run-dir", str(output_path)]
            parent_run = in_flight_task.get("runs", [{}])[-1].get("parent_run") if in_flight_task.get("runs") else None
            if parent_run:
                finalizer_cmd += ["--parent-run", parent_run]
            finalizer_result = _command(finalizer_cmd, ROOT, runner)
            if finalizer_result.returncode == 0:
                in_flight_task["final_audit_path"] = str((output_path / "final_audit.json").relative_to(output_dir))
            else:
                manifest["status"] = "stopped_task_finalization_error"
                in_flight_task["error"] = (finalizer_result.stderr or "finalizer_failed")[0:1000]
                write_json(manifest_path, manifest)
                _render_index(manifest, output_dir)
                return manifest
        in_flight_task["replay_path"] = str((Path(in_flight_task["output_dir"]) / "replay.html").relative_to(output_dir))
        manifest.pop("in_flight", None)
    source_manifest_path = Path(manifest["source_manifest"])
    if sha256_file(source_manifest_path) != manifest["source_manifest_sha256"]:
        raise RuntimeError("batch_source_manifest_hash_changed")
    if authorized_request_limit is not None and authorized_request_limit < 1:
        raise ValueError("authorized_request_limit_must_be_positive")
    if not (0 <= usage_baseline_percent <= 100) or (
        usage_stop_percent is not None and not (0 <= usage_stop_percent <= 100)
    ):
        raise ValueError("usage_percent_out_of_range")
    if max_new_requests_per_invocation is not None and max_new_requests_per_invocation < 1:
        raise ValueError("max_new_requests_per_invocation_must_be_positive")
    pilot_gate = _validate_pilot_audit(pilot_audit_path)
    previous_pilot_gate = manifest.get("pilot_gate")
    if previous_pilot_gate and previous_pilot_gate.get("passed") is True:
        if (
            pilot_gate.get("passed") is not True
            or pilot_gate.get("sha256") != previous_pilot_gate.get("sha256")
            or pilot_gate.get("manifest_sha256") != previous_pilot_gate.get("manifest_sha256")
        ):
            pilot_gate = {
                **pilot_gate,
                "passed": False,
                "reason": "pilot_evidence_changed_after_gate_passed",
            }
    if not pilot_gate.get("passed"):
        checked_at = datetime.now(timezone.utc).isoformat()
        manifest["status"] = (
            "stopped_before_request_pilot_incomplete"
            if pilot_gate.get("reason") == "pilot_route_incomplete"
            else "stopped_before_request_pilot_gate_failed"
        )
        manifest["pilot_gate"] = {**pilot_gate, "checked_at_utc": checked_at}
        manifest["updated_at_utc"] = checked_at
        write_json(manifest_path, manifest)
        _render_index(manifest, output_dir)
        return manifest
    manifest["pilot_gate"] = {
        **pilot_gate,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if usage_stop_percent is not None and usage_baseline_percent >= usage_stop_percent:
        checked_at = datetime.now(timezone.utc).isoformat()
        manifest["status"] = "stopped_before_request_usage_at_or_above_stop_line"
        manifest["updated_at_utc"] = checked_at
        manifest["usage_guard"] = {
            "baseline_weekly_used_percent": usage_baseline_percent,
            "stop_threshold_percent": usage_stop_percent,
            "model_requests_started": 0,
            "checked_at_utc": checked_at,
        }
        write_json(manifest_path, manifest)
        _render_index(manifest, output_dir)
        return manifest

    used = int(manifest.get("model_requests_used", 0))
    if authorized_request_limit is not None and authorized_request_limit < used:
        raise ValueError("request_limit_below_already_used_requests")
    pending = [task for task in manifest["tasks"] if task["status"] in {"pending", "ready", "active", "budget_paused"}]
    pending_minimum = sum(_minimum_remaining_for_task(task) for task in pending)
    remaining = None if authorized_request_limit is None else authorized_request_limit - used
    if authorized_request_limit is not None and pending and remaining < pending_minimum:
        checked_at = datetime.now(timezone.utc).isoformat()
        manifest["status"] = "stopped_before_request_insufficient_budget_for_remaining_minimum"
        manifest["updated_at_utc"] = checked_at
        manifest["authorized_request_limit"] = authorized_request_limit
        manifest["minimum_requests_for_pending_tasks"] = pending_minimum
        manifest["usage_guard"] = {
            "baseline_weekly_used_percent": usage_baseline_percent,
            "stop_threshold_percent": usage_stop_percent,
            "model_requests_started": 0,
            "checked_at_utc": checked_at,
        }
        write_json(manifest_path, manifest)
        _render_index(manifest, output_dir)
        return manifest

    previous_limit = manifest.get("authorized_request_limit")
    manifest["authorized_request_limit"] = authorized_request_limit
    manifest["request_limit_policy"] = (
        "no_project_level_cumulative_cap_user_authorized"
        if authorized_request_limit is None else "explicit_project_level_cumulative_cap"
    )
    manifest["request_limit_display"] = (
        "无项目累计请求上限" if authorized_request_limit is None else str(authorized_request_limit)
    )
    if authorized_request_limit is None:
        manifest["request_budget_allocation"] = (
            "No project-level cumulative request cap. Each task's finite bound is derived from "
            "unique frozen graph nodes minus one; no response is automatically retried."
        )
    if previous_limit != authorized_request_limit:
        manifest.setdefault("authorization_history", []).append({
            "authorized_cumulative_request_limit": authorized_request_limit,
            "no_project_level_cumulative_cap": authorized_request_limit is None,
            "usage_baseline_percent": usage_baseline_percent,
            "usage_stop_percent": usage_stop_percent,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        })
    checked_at = datetime.now(timezone.utc).isoformat()
    manifest["usage_guard"] = {
        "baseline_weekly_used_percent": usage_baseline_percent,
        "stop_threshold_percent": usage_stop_percent,
        "mechanism": (
            "local account-usage stop disabled by user instruction; provider-enforced limits remain active"
            if usage_stop_percent is None else
            "caller supplies a read-only account snapshot; this process cannot query account usage itself"
        ),
        "max_new_requests_per_invocation": max_new_requests_per_invocation,
        "checked_at_utc": checked_at,
    }
    manifest["status"] = "running"
    write_json(manifest_path, manifest)
    _render_index(manifest, output_dir)

    if stop_after_task_id is not None:
        stop_task = next(item for item in manifest["tasks"] if item["task_id"] == stop_after_task_id)
        if stop_task.get("status") in {"route_complete", "failed", "preflight_blocked"}:
            manifest["status"] = "paused_at_requested_task_boundary"
            manifest["pause_reason"] = "requested_task_already_terminal"
            manifest["requested_stop_after_task_id"] = stop_after_task_id
            manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            return manifest

    new_requests_this_invocation = 0
    for task in manifest["tasks"]:
        if task["status"] not in {"pending", "ready", "active", "budget_paused"}:
            continue
        used = int(manifest.get("model_requests_used", 0))
        other_pending = [item for item in manifest["tasks"]
                         if item is not task and item["status"] in {"pending", "ready", "active", "budget_paused"}]
        reserve = sum(_minimum_remaining_for_task(item) for item in other_pending)
        current_minimum = _minimum_remaining_for_task(task)
        total_pending_minimum = current_minimum + reserve
        natural_task_limit = _natural_task_request_limit(task)
        if authorized_request_limit is None:
            task_limit = natural_task_limit
        else:
            remaining_slack = max(0, authorized_request_limit - used - total_pending_minimum)
            fair_slack = remaining_slack // (1 + len(other_pending))
            task_limit = min(natural_task_limit, current_minimum + fair_slack)
        if task_limit < current_minimum:
            manifest["status"] = "stopped_before_request_insufficient_budget_for_next_task"
            manifest["current_task_id"] = task["task_id"]
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            break

        task_root = output_dir / "tasks" / f"seed_{int(task['seed'])}" / task["task_id"]
        previous_task_output = Path(task["output_dir"]) if task.get("output_dir") else None
        is_continuation = task["status"] == "budget_paused"
        resume_existing_run = (
            task["status"] in {"ready", "active"}
            and previous_task_output is not None
            and (previous_task_output / "state.json").is_file()
        )
        run_number = len(task.get("runs", []))
        task_output = previous_task_output if resume_existing_run else task_root / f"run_{run_number:03d}"
        task["output_dir"] = str(task_output)
        task["active_run_requests_recorded"] = 0
        preflight_path = Path(task["preflight_path"])
        if sha256_file(preflight_path) != task["preflight_sha256"]:
            manifest["status"] = "stopped_task_preflight_hash_mismatch"
            manifest["current_task_id"] = task["task_id"]
            task["status"] = "preflight_blocked"
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            break

        state_path = task_output / "state.json"
        if is_continuation:
            if not previous_task_output or not (previous_task_output / "final_audit.json").is_file():
                manifest["status"] = "stopped_resume_parent_audit_missing"
                task["status"] = "outcome_unknown"
                write_json(manifest_path, manifest)
                _render_index(manifest, output_dir)
                break
            preflight_cmd = [
                sys.executable, str(STEP_RUNNER), "--preflight-only",
                "--output-dir", str(task_output), "--resume-from", str(previous_task_output),
                "--request-limit", str(task_limit),
                "--codex-session-mode", REQUIRED_PILOT_SESSION_MODE,
                "--route-input-protocol", REQUIRED_ROUTE_INPUT_PROTOCOL,
            ]
            preflight_result = _command(preflight_cmd, ROOT, runner)
            if preflight_result.returncode != 0 or not state_path.is_file():
                manifest["status"] = "stopped_task_resume_preflight_error"
                task["status"] = "outcome_unknown"
                task["error"] = (preflight_result.stderr or "")[0:1000]
                write_json(manifest_path, manifest)
                _render_index(manifest, output_dir)
                break
        elif not state_path.exists():
            if task_output.exists() and any(task_output.iterdir()):
                manifest["status"] = "stopped_incomplete_task_output_without_state"
                task["status"] = "outcome_unknown"
                write_json(manifest_path, manifest)
                _render_index(manifest, output_dir)
                break
            preflight_cmd = [
                sys.executable, str(STEP_RUNNER), "--preflight-only",
                "--output-dir", str(task_output),
                "--preflight-path", str(preflight_path),
                "--request-limit", str(task_limit),
                "--codex-session-mode", REQUIRED_PILOT_SESSION_MODE,
                "--route-input-protocol", REQUIRED_ROUTE_INPUT_PROTOCOL,
                "--experiment-id", f"r4-codex-formal-seed-{task['seed']}-{task['task_id']}",
                "--record-user-authorized-formal-batch-pilot",
            ]
            preflight_result = _command(preflight_cmd, ROOT, runner)
            if preflight_result.returncode != 0 or not state_path.is_file():
                manifest["status"] = "stopped_task_preflight_error"
                task["status"] = "preflight_blocked"
                task["error"] = (preflight_result.stderr or "")[0:1000]
                write_json(manifest_path, manifest)
                _render_index(manifest, output_dir)
                break
        state = read_json(state_path)
        if state.get("status") == "request_in_progress":
            manifest["status"] = "stopped_outcome_unknown_no_retry"
            task["status"] = "outcome_unknown"
            task["error"] = "task_state_has_in_flight_model_request"
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            return manifest
        if state.get("status") not in {"ready", "route_complete"}:
            task["status"] = "preflight_blocked"
            task["requests_used"] = int(state.get("new_requests_used", 0))
            task["error"] = state.get("status")
            task["replay_path"] = str((task_output / "replay.html").relative_to(output_dir))
            task["output_dir"] = str(task_output)
            if state.get("status") == "route_complete":
                task["status"] = "route_complete"
            else:
                task["status"] = "preflight_blocked"
            manifest["model_requests_used"] = sum(int(item.get("requests_used", 0)) for item in manifest["tasks"])
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            continue

        task["status"] = "active"
        if not resume_existing_run:
            task.setdefault("runs", []).append({
                "output_dir": str(task_output),
                "parent_run": str(previous_task_output) if is_continuation else None,
                "request_limit": task_limit,
                "requests_used": int(state.get("new_requests_used", 0)),
            })
        manifest["current_task_id"] = task["task_id"]
        write_json(manifest_path, manifest)
        _render_index(manifest, output_dir)

        request_chunk_reached = False
        while state.get("status") == "ready":
            before_requests = int(state.get("new_requests_used", 0))
            manifest["in_flight"] = {
                "task_id": task["task_id"],
                "expected_request_number": before_requests + 1,
                "task_requests_used_before": before_requests,
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            one_step_cmd = [
                sys.executable, str(STEP_RUNNER), "--one-step",
                "--output-dir", str(task_output),
            ]
            try:
                result = _command(one_step_cmd, ROOT, runner)
            except Exception as error:  # An interrupted child may already have sent a request.
                manifest["status"] = "stopped_outcome_unknown_no_retry"
                task["status"] = "outcome_unknown"
                task["error"] = f"{type(error).__name__}: {error}"
                write_json(manifest_path, manifest)
                _render_index(manifest, output_dir)
                return manifest
            state = read_json(state_path)
            after_requests = int(state.get("new_requests_used", 0))
            delta_requests = max(0, after_requests - before_requests)
            task["active_run_requests_recorded"] = int(task.get("active_run_requests_recorded", 0)) + delta_requests
            task["requests_used"] = int(task.get("requests_used", 0)) + delta_requests
            new_requests_this_invocation += delta_requests
            task["runs"][-1]["requests_used"] = int(state.get("new_requests_used", 0))
            manifest["model_requests_used"] = sum(int(item.get("requests_used", 0)) for item in manifest["tasks"])
            if state.get("status") == "request_in_progress" or after_requests < before_requests:
                manifest["status"] = "stopped_outcome_unknown_no_retry"
                task["status"] = "outcome_unknown"
                task["error"] = "child_interrupted_while_model_request_outcome_unknown"
                write_json(manifest_path, manifest)
                _render_index(manifest, output_dir)
                return manifest
            if after_requests > before_requests:
                manifest["events"].append({
                    "event": "request_recorded",
                    "task_id": task["task_id"],
                    "request_number": after_requests,
                    "task_status": state.get("status"),
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                })
            manifest.pop("in_flight", None)
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            if result.returncode != 0 and state.get("status") == "ready" and delta_requests == 0:
                manifest["status"] = "stopped_outcome_unknown_no_retry"
                task["status"] = "outcome_unknown"
                task["error"] = (result.stderr or "child_failed_without_terminal_state")[0:1000]
                write_json(manifest_path, manifest)
                _render_index(manifest, output_dir)
                return manifest
            if (
                max_new_requests_per_invocation is not None
                and new_requests_this_invocation >= max_new_requests_per_invocation
                and state.get("status") == "ready"
            ):
                request_chunk_reached = True
                break

        state = read_json(state_path)
        task["runs"][-1]["requests_used"] = int(state.get("new_requests_used", 0))
        task["status"] = "ready" if request_chunk_reached and state.get("status") == "ready" else (
            "route_complete" if state.get("status") == "route_complete" else (
            "budget_paused" if state.get("status") in {
                "authorized_call_limit_exhausted",
                "insufficient_authorized_budget_for_remaining_route",
                "quota_cap_reached",
            } else "failed"
        ))
        task["error"] = None if task["status"] in {"route_complete", "ready"} else state.get("status")
        task["replay_path"] = str((task_output / "replay.html").relative_to(output_dir))
        if task.get("recovery_authorization"):
            recovery_alignment = _normalize_authorized_recovery_trace_slots(task_output)
            if recovery_alignment["normalized_count"]:
                manifest.setdefault("events", []).append({
                    "event": "authorized_recovery_trace_slots_aligned_before_finalization",
                    "task_id": task["task_id"],
                    "mappings": recovery_alignment["mappings"],
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                })
        finalizer_cmd = [sys.executable, str(FINALIZER), "--run-dir", str(task_output)]
        run_parent = task.get("runs", [{}])[-1].get("parent_run") if task.get("runs") else None
        if run_parent:
            finalizer_cmd += ["--parent-run", str(run_parent)]
        finalizer_result = _command(finalizer_cmd, ROOT, runner)
        if finalizer_result.returncode != 0:
            manifest["status"] = "stopped_task_finalization_error"
            task["error"] = (finalizer_result.stderr or "finalizer_failed")[0:1000]
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            break
        task["final_audit_path"] = str((task_output / "final_audit.json").relative_to(output_dir))
        manifest["model_requests_used"] = sum(int(item.get("requests_used", 0)) for item in manifest["tasks"])
        manifest.pop("in_flight", None)
        write_json(manifest_path, manifest)
        _render_index(manifest, output_dir)
        if state.get("status") in {"stopped_request_error", "stopped_pre_request_error", "stopped_tool_event"}:
            manifest["status"] = "stopped_provider_or_tool_contract_failure"
            manifest["stop_reason"] = state.get("status")
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            break
        if (
            max_new_requests_per_invocation is not None
            and new_requests_this_invocation >= max_new_requests_per_invocation
            and any(item["status"] not in {"route_complete", "failed", "preflight_blocked"} for item in manifest["tasks"])
        ):
            manifest["status"] = "paused_at_request_chunk_limit"
            manifest["pause_reason"] = "max_new_requests_per_invocation_reached"
            manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            return manifest
        if task["task_id"] == stop_after_task_id:
            manifest["status"] = "paused_at_requested_task_boundary"
            manifest["pause_reason"] = "requested_task_completed"
            manifest["requested_stop_after_task_id"] = stop_after_task_id
            manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            write_json(manifest_path, manifest)
            _render_index(manifest, output_dir)
            return manifest

    if all(task["status"] in {"route_complete", "failed", "preflight_blocked"} for task in manifest["tasks"]):
        manifest["status"] = "completed_with_audited_task_outcomes"
    elif manifest.get("status") == "running":
        manifest["status"] = "paused_at_checkpoint"
    manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(manifest_path, manifest)
    _render_index(manifest, output_dir)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true", help="create a zero-request batch plan")
    mode.add_argument("--execute", action="store_true", help="send route requests with an explicit cumulative cap or none")
    parser.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--request-limit", type=parse_optional_int, default=ARGUMENT_NOT_SET, help="absolute cumulative request ceiling; use none for no project-level cap")
    parser.add_argument("--usage-baseline-percent", type=int, default=None, help="fresh read-only weekly usage snapshot before execution")
    parser.add_argument("--usage-stop-percent", type=parse_optional_int, default=ARGUMENT_NOT_SET, help="local weekly usage stop percentage; use none to disable this local stop")
    parser.add_argument("--pilot-audit", type=Path, default=None,
                        help="completed, zero-retry task-0000-0009 pilot audit required before the formal batch")
    parser.add_argument("--max-new-requests-per-invocation", type=int, default=None,
                        help="checkpoint after this many new model responses; this is resumable and not a cumulative cap")
    parser.add_argument("--stop-after-task-id", default=None,
                        help="stop cleanly once this frozen task has a terminal audited outcome")
    args = parser.parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    if args.preflight_only:
        manifest = prepare_batch(args.source_manifest, output_dir)
    else:
        if args.request_limit is ARGUMENT_NOT_SET or args.usage_baseline_percent is None or args.usage_stop_percent is ARGUMENT_NOT_SET or args.pilot_audit is None:
            parser.error("--execute requires --request-limit, --usage-baseline-percent, --usage-stop-percent, and --pilot-audit")
        try:
            lock_handle = acquire_execution_lock(output_dir)
        except RuntimeError:
            parser.error("batch_execution_already_running_for_output_dir")
        try:
            manifest = execute_batch(
                output_dir,
                authorized_request_limit=args.request_limit,
                usage_baseline_percent=args.usage_baseline_percent,
                usage_stop_percent=args.usage_stop_percent,
                pilot_audit_path=args.pilot_audit,
                max_new_requests_per_invocation=args.max_new_requests_per_invocation,
                stop_after_task_id=args.stop_after_task_id,
            )
        finally:
            release_execution_lock(lock_handle)
    print(json.dumps({key: manifest.get(key) for key in (
        "status", "frozen_task_count", "routeable_task_count", "model_requests_used",
        "authorized_request_limit", "minimum_model_request_lower_bound",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
