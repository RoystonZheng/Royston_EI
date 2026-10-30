#!/usr/bin/env python3
"""Zero-call preflight audit that binds shared candidate Links into R4 prompts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_r4_codex_pilot_continuation import (  # noqa: E402
    _load_frozen_task,
    _objective_progress_guidance,
)
from route_experiment.r4_constraints import build_r4_route_output_constraint  # noqa: E402
from route_experiment.r4_stepwise import build_r4_stepwise_messages  # noqa: E402
from route_experiment.r4_stepwise import build_r4_step_output_constraint  # noqa: E402

PARENT_DEFAULT = Path("[R4_APP_SUPPORT_OUTPUT_ROOT]/manifest.json")
POOL_DEFAULT = ROOT / "outputs/route_layer/r4_shared_candidate_pool_v2_20261030T1230CST/candidate_pools.jsonl"
RUNNER = ROOT / "scripts/run_r4_codex_pilot_continuation.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-manifest", type=Path, default=PARENT_DEFAULT)
    parser.add_argument("--candidate-pool", type=Path, default=POOL_DEFAULT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    parent_path = args.parent_manifest.expanduser().resolve()
    pool_path = args.candidate_pool.expanduser().resolve()
    output = args.output_dir.resolve() if args.output_dir.is_absolute() else (ROOT / args.output_dir).resolve()
    if output.exists():
        raise SystemExit("refusing_to_overwrite_existing_output:" + str(output))

    parent = read_json(parent_path)
    pool_manifest_path = pool_path.parent / "manifest.json"
    pool_manifest = read_json(pool_manifest_path)
    if pool_manifest.get("schema_version") != "r4_shared_candidate_pool_v2":
        raise SystemExit("candidate_pool_schema_mismatch")
    if sha256(pool_path) != pool_manifest.get("candidate_pools_sha256"):
        raise SystemExit("candidate_pool_hash_mismatch")
    pool_tasks_path = pool_path.parent / str(pool_manifest.get("task_summary_path", "task_summary.jsonl"))
    pool_tasks = {}
    for line in pool_tasks_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        pool_tasks[str(row["task_id"])] = row
    task_rows = list(parent.get("tasks", []))
    output.mkdir(parents=True)
    summary = {
        "schema_version": "r4_shared_link_prompt_preflight_audit_v1",
        "status": "running",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "parent_manifest": str(parent_path),
        "parent_manifest_sha256": sha256(parent_path),
        "candidate_pool": str(pool_path),
        "candidate_pool_sha256": sha256(pool_path),
        "candidate_pool_manifest_sha256": sha256(pool_manifest_path),
        "candidate_pool_task_summary_sha256": sha256(pool_tasks_path),
        "runner_script": str(RUNNER),
        "runner_script_sha256": sha256(RUNNER),
        "stepwise_module": str(ROOT / "route_experiment/r4_stepwise.py"),
        "stepwise_module_sha256": sha256(ROOT / "route_experiment/r4_stepwise.py"),
        "task_count": len(task_rows),
        "model_calls": 0,
        "tasks": [],
    }
    manifest_path = output / "manifest.json"
    write_json(manifest_path, summary)
    counts = Counter()

    for source_task in task_rows:
        task_id = str(source_task["task_id"])
        task_out = output / "tasks" / task_id
        task_out.mkdir(parents=True)
        task_preflight = Path(str(source_task["preflight_path"])).expanduser()
        if not task_preflight.is_absolute():
            task_preflight = ROOT / task_preflight
        task_preflight = task_preflight.resolve()
        expected_preflight_hash = source_task.get("preflight_sha256")
        pool_summary = pool_tasks.get(task_id)
        row: Dict[str, Any] = {
            "task_id": task_id,
            "prior_parent_status": source_task.get("status"),
            "preflight_path": str(task_preflight),
            "preflight_sha256": sha256(task_preflight) if task_preflight.is_file() else None,
            "pool_task_summary": pool_summary,
            "status": "pending",
        }
        if not task_preflight.is_file():
            row.update(status="blocked_missing_preflight", reason="frozen_preflight_missing")
        elif expected_preflight_hash and sha256(task_preflight) != expected_preflight_hash:
            row.update(status="blocked_preflight_hash_mismatch", reason="parent_preflight_sha_mismatch")
        elif pool_summary is None:
            row.update(status="blocked_pool_task_missing", reason="candidate_pool_has_no_task_summary")
        elif pool_summary.get("source_preflight_sha256") != sha256(task_preflight):
            row.update(status="blocked_pool_preflight_hash_mismatch", reason="candidate_pool_uses_different_frozen_preflight")
        else:
            command = [
                sys.executable, str(RUNNER), "--preflight-only",
                "--preflight-path", str(task_preflight),
                "--output-dir", str(task_out / "r4_link_preflight"),
                "--experiment-id", "r4_link_preflight_" + task_id,
                "--route-input-protocol", "r4-step-v4-link-fallback",
                "--shared-candidate-pool", str(pool_path),
                "--record-user-authorized-formal-batch-pilot",
            ]
            proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
            (task_out / "stdout.json").write_text(proc.stdout, encoding="utf-8")
            (task_out / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
            row["preflight_returncode"] = proc.returncode
            row["preflight_command"] = command
            state_path = task_out / "r4_link_preflight" / "state.json"
            if proc.returncode != 0 or not state_path.is_file():
                row.update(status="blocked_r4_link_preflight", reason=proc.stderr[-2000:])
            else:
                state = read_json(state_path)
                source, road_map, task, package = _load_frozen_task(task_preflight)
                full_constraint = build_r4_route_output_constraint(
                    package, task.pickup_node_id, task.dropoff_node_id, allow_failed=False
                )
                initial_budget = min(
                    int(state["route_action_budget"]),
                    int(state["authorized_new_request_limit"]),
                )
                initial_constraint = build_r4_step_output_constraint(
                    package,
                    full_constraint,
                    current_node_id=state["start_node_id"],
                    pickup_seen=state["start_node_id"] == state["pickup_node_id"],
                    visited_node_ids=[state["start_node_id"]],
                    remaining_action_budget=initial_budget,
                    include_objective_cost_guidance=bool(state.get("enable_progress_guidance")),
                    require_suffix_witness=True,
                )
                messages = build_r4_stepwise_messages(
                    package,
                    state["start_node_id"], state["pickup_node_id"], state["dropoff_node_id"],
                    current_node_id=state["start_node_id"],
                    visited_node_ids=[state["start_node_id"]],
                    pickup_seen=state["start_node_id"] == state["pickup_node_id"],
                    accepted_directions=[],
                    remaining_action_budget=initial_budget,
                    route_feature_context=state.get("route_feature_context"),
                    compact_visited_state=bool(state.get("compact_visited_state")),
                    progress_guidance=(
                        _objective_progress_guidance(initial_constraint)
                        if state.get("enable_progress_guidance")
                        else None
                    ),
                    shared_candidate_routes=state.get("shared_candidate_routes"),
                )
                prompt_payload = json.loads(messages[-1]["content"])
                link_payload = prompt_payload.get("l") or {}
                route_list = link_payload.get("routes") or []
                state_routes = state.get("shared_candidate_routes") or []
                route_ids = [r.get("route_id") for r in route_list]
                state_route_ids = [r.get("route_id") for r in state_routes]
                prompt_sha = hashlib.sha256(messages[-1]["content"].encode("utf-8")).hexdigest()
                ok = bool(
                    state.get("route_input_protocol") == "r4-step-v4-link-fallback"
                    and state.get("new_requests_used") == 0
                    and state.get("shared_candidate_pool_sha256") == sha256(pool_path)
                    and link_payload.get("v") == "shared-candidate-links-v1"
                    and link_payload.get("policy") == "fallback_only"
                    and state_routes == route_list
                    and route_ids == state_route_ids
                    and len(route_list) == int(state.get("initial_prompt_shared_link_route_count", -1))
                    and canonical_sha256(route_list) == state.get("initial_prompt_shared_link_routes_sha256")
                    and prompt_sha == state.get("initial_prompt_sha256")
                )
                row.update({
                    "status": "passed_links_visible_in_first_prompt" if ok else "blocked_prompt_link_audit_mismatch",
                    "state_path": str(state_path),
                    "state_sha256": sha256(state_path),
                    "route_input_protocol": state.get("route_input_protocol"),
                    "shared_candidate_pool_sha256": state.get("shared_candidate_pool_sha256"),
                    "shared_candidate_route_count": len(route_list),
                    "shared_candidate_route_ids": route_ids,
                    "initial_prompt_sha256": prompt_sha,
                    "initial_prompt_link_policy": link_payload.get("policy"),
                    "initial_prompt_link_routes_sha256": canonical_sha256(route_list),
                    "map_equivalence_passed": state.get("map_equivalence_audit", {}).get("passed"),
                    "zero_model_requests": state.get("new_requests_used") == 0,
                    "money_cost_status": link_payload.get("money_cost_status"),
                })
        summary["tasks"].append(row)
        counts[row["status"]] += 1
        write_json(manifest_path, summary)

    summary["status"] = "passed_all_prompt_link_checks" if counts.get("passed_links_visible_in_first_prompt") == len(task_rows) else "completed_with_blocks"
    summary["status_counts"] = dict(counts)
    summary["model_calls"] = 0
    summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(manifest_path, summary)
    print(json.dumps({"status": summary["status"], "task_count": len(task_rows), "status_counts": summary["status_counts"], "manifest": str(manifest_path), "manifest_sha256": sha256(manifest_path)}, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "passed_all_prompt_link_checks" else 2


if __name__ == "__main__":
    raise SystemExit(main())
