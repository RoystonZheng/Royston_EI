#!/usr/bin/env python3
"""Prepare and run a one-shot Codex pure-LLM route-search control for R4.

Preparation is offline. Execution makes at most one Codex CLI invocation for
each of the 129 routeable frozen tasks, with no retries or route fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from r4_execution_lock import acquire_execution_lock, release_execution_lock

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import EvaluationConfig, RouteCandidate
from route_experiment.r4_compact_route import (
    make_r4_compact_route_constraint,
    validate_r4_compact_route_proposal,
)
from scripts.run_r4_codex_formal_checkpoint import load_map


SOURCE_ROOT = ROOT / "outputs/route_layer/r4_codex_batch_preflights_20261024_v1"
SOURCE_MANIFEST = SOURCE_ROOT / "manifest.json"
OUTPUT_ROOT = ROOT / "outputs/route_layer/r4_codex_pure_llm_matched_batch_20261027_v1"
MODEL = "gpt-5.6-luna"
MAX_ROUTE_EDGES = 512
PROTOCOL = "r4-codex-pure-llm-full-route-v1"
CONSTRAINT = make_r4_compact_route_constraint(MAX_ROUTE_EDGES)

SYSTEM_PROMPT = (
    "你只根据用户消息中的冻结有向网格图，为一辆车一次性构造完整路线。"
    "地图m.g的每行按y递增排列；每个节点按x递增排列，连续4个字符依次表示E、W、N、S出边；"
    "'.'表示没有出边，其他编码的行驶秒数和距离见m.c。只能走图中明确列出的有向边，"
    "不可根据相邻坐标推断反向边。起点是q.s，先到q.p上车，再到q.d下车并立即停止；"
    "路线不可越界、重复节点或违反接送顺序。s.v列出的节点已经访问，当前任务从初始起点开始。"
    "在完整路线中尽量最小化总行驶秒数，秒数相同时再最小化距离。先构造完整路线再优化，不要局部贪心。"
    "对每条路线边逐步核对当前有向出边、边界、接送顺序和节点未访问情况；"
    "move_sequence每个字符对应一条边，step_check_sequence每条边对应一个字符'1'。"
    "只有完整路线满足全部约束时才返回completed；无法构造完整路线时返回failed和空路线。"
    "独立程序会逐边复核；自检字符串不能代替程序验证。只返回输出schema要求的JSON，不输出解释。"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def task_dir(item: dict[str, Any]) -> Path:
    return OUTPUT_ROOT / "tasks" / f"seed_{int(item['seed'])}" / item["task_id"]


def _build_prompt(source_item: dict[str, Any]) -> dict[str, Any]:
    preflight_path = ROOT / source_item["preflight_path"]
    preflight_bytes = preflight_path.read_bytes()
    preflight = json.loads(preflight_bytes)
    search = preflight["route_search"]
    if search.get("evaluator_truth_in_prompt") is not False:
        raise ValueError("evaluator_truth_gate_not_false:" + source_item["task_id"])
    original_messages = search.get("messages")
    if not isinstance(original_messages, list) or len(original_messages) < 2:
        raise ValueError("frozen_prompt_missing_messages:" + source_item["task_id"])
    try:
        payload = json.loads(original_messages[-1]["content"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("frozen_prompt_payload_invalid:" + source_item["task_id"]) from error
    q = payload.get("q", {})
    state = payload.get("s", {})
    start, pickup, dropoff = q.get("s"), q.get("p"), q.get("d")
    if not all(isinstance(value, str) and value for value in (start, pickup, dropoff)):
        raise ValueError("frozen_endpoint_missing:" + source_item["task_id"])
    if state.get("n") != start or state.get("v") != [start]:
        raise ValueError("preflight_not_initial_route_state:" + source_item["task_id"])
    truth = preflight.get("evaluation_truth", {})
    endpoints_match_truth = (
        pickup == truth.get("true_pickup_node_id")
        and dropoff == truth.get("true_dropoff_node_id")
    )

    # Preserve the entire frozen compact map, task endpoints, and whitelisted
    # intent. Only add the full-route output contract; no evaluator fields are
    # copied into the model prompt.
    payload["output_contract"] = {
        "schema_version": "r4_compact_route_output_v1",
        "status": ["completed", "failed"],
        "move_sequence": "one E/W/N/S character per proposed edge; no separators",
        "step_check_sequence": "one 1 per move, aligned by position",
        "failure_reason_code": ["none", "unable_to_construct_route", "no_directed_path"],
        "rule": "completed is a proposal; the independent program decides acceptance",
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))},
    ]
    rendered = CodexCliRouteStepClient._render_prompt(messages).encode("utf-8")
    forbidden_markers = (b"true_pickup_node_id", b"true_dropoff_node_id", b"ground_truth")
    if any(marker in rendered for marker in forbidden_markers):
        raise ValueError("evaluator_truth_marker_in_rendered_prompt:" + source_item["task_id"])

    map_path = ROOT / preflight["map_ref"]["path"]
    task_source_path = ROOT / preflight["task_source"]["path"]
    if sha256_bytes(map_path.read_bytes()) != preflight["map_ref"].get("sha256"):
        raise ValueError("map_hash_mismatch:" + source_item["task_id"])
    if sha256_bytes(task_source_path.read_bytes()) != preflight["task_source"].get("sha256"):
        raise ValueError("task_source_hash_mismatch:" + source_item["task_id"])
    if preflight.get("map_ref", {}).get("map_id") and preflight["map_ref"]["map_id"] != payload.get("m", {}).get("id"):
        # Some older preflight records omit the compact-map id. The file hash
        # and map loader remain authoritative in that case.
        raise ValueError("map_id_mismatch:" + source_item["task_id"])

    map_bytes = map_path.read_bytes()
    road_map = load_map(map_path)
    if road_map.map_id != read_json(map_path).get("map_id"):
        raise ValueError("loaded_map_identity_mismatch:" + source_item["task_id"])
    return {
        "seed": int(source_item["seed"]),
        "task_id": source_item["task_id"],
        "sample_group": source_item["sample_group"],
        "task_type": preflight.get("task_public", {}).get("task_type"),
        "map_id": road_map.map_id,
        "start_node_id": start,
        "pickup_node_id": pickup,
        "dropoff_node_id": dropoff,
        "r4_endpoints_match_evaluation_truth": endpoints_match_truth,
        "blocked_edge_ids": list(search.get("blocked_edge_ids", [])),
        "preflight_path": str(preflight_path.relative_to(ROOT)),
        "preflight_sha256": sha256_bytes(preflight_bytes),
        "map_path": str(map_path.relative_to(ROOT)),
        "map_sha256": sha256_bytes(map_bytes),
        "task_source_path": str(task_source_path.relative_to(ROOT)),
        "task_source_sha256": sha256_bytes(task_source_path.read_bytes()),
        "prompt_sha256": sha256_bytes(rendered),
        "prompt_bytes": len(rendered),
        "schema_sha256": CONSTRAINT.schema_sha256,
        "schema_bytes": CONSTRAINT.schema_bytes,
        "messages": messages,
        "road_map": road_map,
    }


def prepare(output_dir: Path = OUTPUT_ROOT) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError("output_directory_exists_no_overwrite")
    staging_dir = output_dir.with_name(output_dir.name + ".preparing")
    if staging_dir.exists():
        raise FileExistsError("preflight_staging_directory_exists_no_overwrite")
    source_bytes = SOURCE_MANIFEST.read_bytes()
    source = json.loads(source_bytes)
    task_rows = source.get("tasks", [])
    if len(task_rows) != 129 or len(task_rows) + len(source.get("skipped_tasks", [])) != 150:
        raise RuntimeError("unexpected_frozen_task_denominator")

    staging_dir.mkdir(parents=True, exist_ok=False)
    prepared = []
    try:
        for item in task_rows:
            row = _build_prompt(item)
            td = staging_dir / "tasks" / f"seed_{row['seed']}" / row["task_id"]
            write_json(td / "request.json", {
                "protocol": PROTOCOL,
                "messages": row.pop("messages"),
                "schema": CONSTRAINT.schema,
                "schema_sha256": CONSTRAINT.schema_sha256,
                "prompt_sha256": row["prompt_sha256"],
                "prompt_bytes": row["prompt_bytes"],
            })
            row["task_dir"] = str(td.relative_to(staging_dir))
            row["status"] = "pending"
            row["model_requests_used"] = 0
            row.pop("road_map")
            prepared.append(row)

        skipped = [
            {key: item.get(key) for key in ("seed", "task_id", "sample_group", "endpoint_gate", "route_search_gate", "preflight_errors")}
            for item in source.get("skipped_tasks", [])
        ]
        manifest = {
            "schema_version": "r4_codex_pure_llm_matched_batch_v1",
            "experiment_id": "r4_codex_pure_llm_matched_batch_20261027_v1",
            "status": "offline_preflight_passed_no_model_requests",
            "created_at_utc": utc_now(),
            "authorization_note": None,
            "source_preflight_manifest": str(SOURCE_MANIFEST.relative_to(ROOT)),
            "source_preflight_manifest_sha256": sha256_bytes(source_bytes),
            "frozen_task_count": 150,
            "routeable_task_count": 129,
            "preflight_skipped_task_count": 21,
            "model": MODEL,
            "reasoning_effort": "low",
            "protocol": PROTOCOL,
            "session_mode": "ephemeral_per_request",
            "request_policy": {
                "one_model_invocation_per_routeable_task": True,
                "maximum_routeable_invocations": 129,
                "retries": 0,
                "route_repair": False,
                "traditional_fallback": False,
                "validator": "validate_r4_compact_route_proposal",
                "evaluation": "shared EV longitudinal v2 EvaluationConfig",
                "route_endpoints": "fixed to the same frozen R4 preflight endpoints; endpoint parsing is outside this route-search comparison",
                "account_usage_stop_percent": 100,
            },
            "route_outcome_denominator_includes_skips": True,
            "skipped_tasks": skipped,
            "model_requests_used": 0,
            "in_flight": None,
            "tasks": prepared,
            "events": [],
        }
        write_json(staging_dir / "manifest.json", manifest)
        write_json(staging_dir / "summary.json", {
            "status": manifest["status"],
            "frozen_task_count": 150,
            "routeable_task_count": 129,
            "skipped_task_count": 21,
            "model_requests_used": 0,
            "preflight_errors": [],
            "prompt_bytes_min": min(x["prompt_bytes"] for x in prepared),
            "prompt_bytes_max": max(x["prompt_bytes"] for x in prepared),
            "distinct_map_count": len({x["map_sha256"] for x in prepared}),
            "prompt_hashes": {x["task_id"]: x["prompt_sha256"] for x in prepared},
        })
        os.replace(staging_dir, output_dir)
    except Exception:
        import shutil
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return manifest


def _task_attempt_path(item: dict[str, Any], output_dir: Path = OUTPUT_ROOT) -> Path:
    return output_dir / item["task_dir"] / "attempt_001.json"


def _summarize(manifest: dict[str, Any], output_dir: Path = OUTPUT_ROOT) -> dict[str, Any]:
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "latency_ms": 0.0}
    usage_records = 0
    terminal = 0
    completed_routes = 0
    tool_events = 0
    retry_count = 0
    unknown = 0
    for item in manifest["tasks"]:
        attempt_path = _task_attempt_path(item, output_dir)
        if not attempt_path.is_file():
            continue
        attempt = read_json(attempt_path)
        if attempt.get("status") in {"request_in_progress", "outcome_unknown_no_retry"}:
            unknown += 1
        if attempt.get("status") in {"response_received", "request_error_no_retry", "outcome_unknown_no_retry"}:
            terminal += 1
        usage = attempt.get("usage", {})
        if isinstance(usage.get("input_tokens"), int):
            totals["input_tokens"] += usage["input_tokens"]
            totals["cached_input_tokens"] += int(usage.get("cached_input_tokens") or 0)
            totals["output_tokens"] += int(usage.get("output_tokens") or 0)
            totals["latency_ms"] += float(usage.get("latency_ms") or 0)
            usage_records += 1
        audit = attempt.get("provider_audit", {})
        tool_events += int(audit.get("tool_usage_detected") is True)
        retry_count += int(attempt.get("retry_count") or 0)
        completed_routes += int(bool((attempt.get("validator") or {}).get("accepted")))
    return {
        "status": manifest.get("status"),
        "frozen_task_count": manifest.get("frozen_task_count"),
        "routeable_task_count": manifest.get("routeable_task_count"),
        "preflight_skipped_task_count": manifest.get("preflight_skipped_task_count"),
        "model_requests_used": manifest.get("model_requests_used"),
        "terminal_invocation_count": terminal,
        "usage_complete_invocation_count": usage_records,
        "completed_valid_route_count": completed_routes,
        "request_outcome_unknown_count": unknown,
        "tool_usage_detected_count": tool_events,
        "retry_count": retry_count,
        "usage_totals": totals,
        "account_billing": "not_reported_by_codex_cli",
    }


def execute(output_dir: Path, *, max_new_requests: int, authorization_note: str) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("offline_preflight_manifest_required")
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != "r4_codex_pure_llm_matched_batch_v1":
        raise RuntimeError("unexpected_batch_schema")
    if not authorization_note.strip():
        raise ValueError("authorization_note_required")
    if max_new_requests < 1:
        raise ValueError("max_new_requests_must_be_positive")
    source_path = ROOT / manifest["source_preflight_manifest"]
    if sha256_bytes(source_path.read_bytes()) != manifest["source_preflight_manifest_sha256"]:
        raise RuntimeError("frozen_source_manifest_hash_changed")

    manifest["authorization_note"] = authorization_note
    if manifest.get("in_flight"):
        flight_id = manifest["in_flight"].get("task_id")
        flight = next((x for x in manifest["tasks"] if x["task_id"] == flight_id), None)
        attempt_path = _task_attempt_path(flight, output_dir) if flight else None
        attempt = read_json(attempt_path) if attempt_path and attempt_path.is_file() else None
        if attempt and attempt.get("delivery_status") == "response_received":
            flight["status"] = "complete_valid_route" if (attempt.get("validator") or {}).get("accepted") else "response_received_invalid_route"
        else:
            if flight:
                flight["status"] = "outcome_unknown_no_retry"
                if attempt is None:
                    attempt = {"status": "outcome_unknown_no_retry", "delivery_status": "outcome_unknown_no_retry", "retry_count": 0}
                    write_json(_task_attempt_path(flight, output_dir), attempt)
                else:
                    attempt["status"] = "outcome_unknown_no_retry"
                    attempt["delivery_status"] = "outcome_unknown_no_retry"
                    write_json(attempt_path, attempt)
        manifest["events"].append({"event": "interrupted_invocation_reconciled_no_retry", "task_id": flight_id, "at_utc": utc_now()})
        manifest["in_flight"] = None

    for item in manifest["tasks"]:
        if item.get("status") == "active":
            item["status"] = "outcome_unknown_no_retry"
            manifest["events"].append({"event": "orphan_active_task_marked_unknown_no_retry", "task_id": item["task_id"], "at_utc": utc_now()})

    manifest["status"] = "running"
    write_json(manifest_path, manifest)
    requests_before = int(manifest.get("model_requests_used", 0))
    started_this_invocation = 0
    for item in manifest["tasks"]:
        if item.get("status") != "pending":
            continue
        if int(manifest.get("model_requests_used", 0)) >= 129 or started_this_invocation >= max_new_requests:
            break
        request_path = output_dir / item["task_dir"] / "request.json"
        request = read_json(request_path)
        rendered = CodexCliRouteStepClient._render_prompt(request["messages"]).encode("utf-8")
        if sha256_bytes(rendered) != item["prompt_sha256"]:
            raise RuntimeError("prepared_prompt_hash_changed_no_model_request:" + item["task_id"])
        if request.get("schema_sha256") != CONSTRAINT.schema_sha256 or item.get("schema_sha256") != CONSTRAINT.schema_sha256:
            raise RuntimeError("prepared_schema_hash_changed_no_model_request:" + item["task_id"])
        if sha256_bytes((ROOT / item["map_path"]).read_bytes()) != item["map_sha256"]:
            raise RuntimeError("prepared_map_hash_changed_no_model_request:" + item["task_id"])
        map_path = ROOT / item["map_path"]
        road_map = load_map(map_path)
        task_output = request_path.parent
        attempt = {
            "schema_version": "r4_pure_llm_task_attempt_v1",
            "task_id": item["task_id"],
            "seed": item["seed"],
            "model": MODEL,
            "protocol": PROTOCOL,
            "request_number": int(manifest["model_requests_used"]) + 1,
            "prompt_sha256": item["prompt_sha256"],
            "schema_sha256": item["schema_sha256"],
            "retry_count": 0,
            "status": "request_in_progress",
            "delivery_status": "unknown_until_receipt",
            "started_at_utc": utc_now(),
        }
        write_json(task_output / "attempt_001.json", attempt)
        manifest["model_requests_used"] = int(manifest.get("model_requests_used", 0)) + 1
        started_this_invocation += 1
        item["model_requests_used"] = 1
        item["status"] = "active"
        manifest["in_flight"] = {"task_id": item["task_id"], "request_number": manifest["model_requests_used"], "started_at_utc": utc_now()}
        write_json(manifest_path, manifest)

        client = CodexCliRouteStepClient(
            model=MODEL,
            trace_dir=task_output / "traces",
            reasoning_effort="low",
            timeout_sec=300,
            route_constraint=CONSTRAINT,
            persist_session=False,
            working_directory=task_output / "empty_workspace",
            trace_stem="pure_llm_route",
        )
        try:
            response = client.chat(request["messages"])
            payload = json.loads(response.content)
            preflight = read_json(ROOT / item["preflight_path"])
            audit = validate_r4_compact_route_proposal(
                road_map,
                start_node_id=item["start_node_id"],
                pickup_node_id=item["pickup_node_id"],
                dropoff_node_id=item["dropoff_node_id"],
                payload=payload,
                blocked_edge_ids=preflight["route_search"].get("blocked_edge_ids", []),
            )
            metrics = None
            if audit.get("accepted"):
                candidate = RouteCandidate(
                    route_id="pure_llm_codex_" + item["task_id"],
                    node_sequence=list(audit["route_node_sequence"]),
                    edge_sequence=list(audit["route_edge_sequence"]),
                    route_source="pure_llm_codex_one_shot_full_route",
                    route_reachability=True,
                    validation_status="valid",
                )
                result = evaluate_route_candidate(road_map, candidate, EvaluationConfig())
                metrics = {
                    "edge_count": len(audit["route_edge_sequence"]),
                    "distance_m": result.route_distance_m,
                    "travel_time_sec": result.total_travel_time_sec,
                    "energy_kwh": result.energy_total_kwh,
                    "energy_model_version": result.energy_model_version,
                    "simulation_estimate": True,
                }
            attempt.update({
                "status": "response_received",
                "delivery_status": "response_received",
                "response": payload,
                "validator": audit,
                "metrics": metrics,
                "usage": {
                    "input_tokens": response.input_tokens,
                    "cached_input_tokens": response.cached_input_tokens,
                    "output_tokens": response.output_tokens,
                    "latency_ms": response.latency_ms,
                    "account_billing": "not_reported_by_codex_cli",
                },
                "provider_audit": response.provider_audit,
                "completed_at_utc": utc_now(),
            })
            item["status"] = "complete_valid_route" if audit.get("accepted") else "response_received_invalid_route"
        except Exception as error:
            attempt.update({
                "status": "request_error_no_retry",
                "delivery_status": "unknown_or_error",
                "error_type": type(error).__name__,
                "error": CodexCliRouteStepClient._redact_diagnostics(str(error))[:500],
                "provider_audit": dict(client.last_record),
                "completed_at_utc": utc_now(),
            })
            item["status"] = "request_error_no_retry"
        write_json(task_output / "attempt_001.json", attempt)
        item["completed_at_utc"] = utc_now()
        manifest["events"].append({
            "event": "task_invocation_finished",
            "task_id": item["task_id"],
            "task_status": item["status"],
            "request_number": attempt["request_number"],
            "at_utc": utc_now(),
        })
        manifest["in_flight"] = None
        write_json(manifest_path, manifest)
        write_json(output_dir / "summary.json", _summarize(manifest, output_dir))

    pending = sum(item.get("status") == "pending" for item in manifest["tasks"])
    manifest["status"] = "completed" if pending == 0 else "paused_at_request_chunk_boundary"
    manifest["updated_at_utc"] = utc_now()
    write_json(manifest_path, manifest)
    summary = _summarize(manifest, output_dir)
    summary["new_requests_started_this_invocation"] = int(manifest.get("model_requests_used", 0)) - requests_before
    summary["pending_routeable_tasks"] = pending
    write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="run offline prompt/hash preflight; no model requests")
    parser.add_argument("--execute", action="store_true", help="execute pending tasks from the prepared batch")
    parser.add_argument("--max-new-requests", type=int, default=10, help="checkpoint size before reading account usage again")
    parser.add_argument("--authorization-note", default="")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args()
    if args.prepare == args.execute:
        parser.error("choose exactly one of --prepare or --execute")
    if args.prepare:
        result = prepare(args.output_dir)
    else:
        output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
        output_dir = output_dir.resolve()
        try:
            lock_handle = acquire_execution_lock(output_dir)
        except RuntimeError:
            parser.error("batch_execution_already_running_for_output_dir")
        try:
            result = execute(output_dir, max_new_requests=args.max_new_requests, authorization_note=args.authorization_note)
        finally:
            release_execution_lock(lock_handle)
    print(json.dumps(result if args.execute else {
        "status": result["status"],
        "frozen_task_count": result["frozen_task_count"],
        "routeable_task_count": result["routeable_task_count"],
        "preflight_skipped_task_count": result["preflight_skipped_task_count"],
        "model_requests_used": result["model_requests_used"],
        "source_preflight_manifest_sha256": result["source_preflight_manifest_sha256"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
