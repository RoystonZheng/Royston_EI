#!/usr/bin/env python3
"""Export restartable, zero-request task inputs for the frozen R4 batch."""

from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for item in (ROOT, SCRIPTS):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.models import PackageConfig
from route_experiment.r4_constraints import build_r4_route_output_constraint
from route_experiment.r4_stepwise import (
    audit_r4_stepwise_map_equivalence,
    build_r4_step_output_constraint,
    build_r4_stepwise_messages,
)
from route_experiment.routing import build_route_input_package
from preflight_r4_codex_all_frozen_tasks import (
    DEFAULT_INTENT_ARTIFACT,
    DEFAULT_INTENT_ROOT,
    DEFAULT_SEED_ROOT,
    _feature_context,
    _task_from_raw,
    canonical_json_sha256,
    read_json,
)
from run_r4_codex_formal_checkpoint import load_map
from run_route_r4_smoke import _road_feature_protocol, _validate_protocol_references


DEFAULT_SOURCE_PREFLIGHT = ROOT / "outputs/route_layer/r4_codex_all_frozen_tasks_recheck_20261024/summary.json"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_codex_batch_preflights_20261024_v1"
ROUTE_ACTION_BUDGET = 512
_ROAD_PROTOCOL_CACHE: Dict[str, Any] = {}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=16)
def _load_map_cached(path_text: str):
    return load_map(Path(path_text))


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _find_source_task(row: Dict[str, Any], seed_root: Path) -> Tuple[dict, Path, Path, List[str], dict]:
    seed = int(row["seed"])
    seed_dir = seed_root / f"seed_{seed}"
    map_path = seed_dir / f"map_100x100_seed_{seed}.json"
    if row["sample_group"] == "orders":
        source_path = seed_dir / f"order_tasks_seed_{seed}.json"
        payload = read_json(source_path)
        raw = next(item for item in payload["tasks"] if item["task_id"] == row["task_id"])
        truth = next(item for item in payload["ground_truth"] if item["task_id"] == row["task_id"])
        return raw, map_path, source_path, [], truth

    source_path = seed_dir / f"formal_scenarios_seed_{seed}.json"
    payload = read_json(source_path)
    for scenario in payload["scenarios"]:
        for raw in scenario["tasks"]:
            if raw["task_id"] == row["task_id"]:
                truth = {
                    "true_pickup_node_id": raw.get("true_pickup_node_id", raw.get("pickup_node_id")),
                    "true_dropoff_node_id": raw.get("true_dropoff_node_id", raw.get("dropoff_node_id")),
                }
                return raw, map_path, source_path, list(scenario.get("blocked_edge_ids", [])), truth
    raise KeyError("frozen_task_not_found:" + str(row["task_id"]))


def build_task_preflight(
    row: Dict[str, Any],
    *,
    seed_root: Path = DEFAULT_SEED_ROOT,
    intent_root: Path = DEFAULT_INTENT_ROOT,
    intent_artifact_path: Path = DEFAULT_INTENT_ARTIFACT,
) -> Dict[str, Any]:
    """Build one R4 model input using only the audited task row and frozen sources."""
    if row.get("endpoint_gate") != "passed" or row.get("route_chain_reachable") is not True:
        raise ValueError("task_is_not_routeable_preflight")
    raw, map_path, task_source_path, blocked_edge_ids, truth = _find_source_task(row, seed_root)
    map_path = map_path.resolve()
    road_map = _load_map_cached(str(map_path))
    intent = None
    if row["sample_group"] == "orders" and row.get("task_type") != "exact_node":
        intent_data = read_json(intent_artifact_path)
        record = next(
            item for item in intent_data["records"]
            if int(item["seed"]) == int(row["seed"])
            and item["task_id"] == row["task_id"]
            and item.get("map_input_mode") == "road_feature_protocol_v1"
        )
        if record.get("uses_evaluator_truth") is not False or record.get("status") != "resolved":
            raise RuntimeError("semantic_intent_source_gate_failed")
        intent = record.get("route_intent")
        if not intent:
            raise RuntimeError("semantic_route_intent_missing")
        if record.get("pickup_node_id") != row.get("pickup_node_id") or record.get("dropoff_node_id") != row.get("dropoff_node_id"):
            raise RuntimeError("semantic_intent_endpoint_does_not_match_frozen_preflight")

    task = _task_from_raw(raw, row.get("pickup_node_id"), row.get("dropoff_node_id"))
    package = build_route_input_package(
        road_map,
        task,
        PackageConfig(),
        blocked_edge_ids=tuple(blocked_edge_ids),
    )
    feature_context = None
    if intent is not None:
        # This protocol's semantic index is map-derived. Reuse it for all
        # semantic orders on the same frozen map rather than rebuilding the
        # 10k-edge entity/relation tables for every task.
        protocol = _ROAD_PROTOCOL_CACHE.get(road_map.map_id)
        if protocol is None:
            protocol = _road_feature_protocol(road_map, task)
            _ROAD_PROTOCOL_CACHE[road_map.map_id] = protocol
        protocol.context["random_seed"] = int(row["seed"])
        protocol_audit = _validate_protocol_references(protocol)
        if not protocol_audit["passed"]:
            raise RuntimeError("road_feature_protocol_reference_validation_failed")
        feature_context = _feature_context(protocol, intent)

    full_constraint = build_r4_route_output_constraint(
        package, task.pickup_node_id, task.dropoff_node_id, allow_failed=False
    )
    initial_constraint = build_r4_step_output_constraint(
        package,
        full_constraint,
        current_node_id=task.start_node_id,
        pickup_seen=task.start_node_id == task.pickup_node_id,
        visited_node_ids=[task.start_node_id],
        blocked_edge_ids=tuple(blocked_edge_ids),
        remaining_action_budget=ROUTE_ACTION_BUDGET,
    )
    if initial_constraint.remaining_minimum_moves != row.get("minimum_route_actions_lower_bound"):
        raise RuntimeError("frozen_preflight_action_lower_bound_mismatch")
    if not initial_constraint.edge_by_direction:
        raise RuntimeError("routeable_task_has_no_initial_legal_direction")

    messages = build_r4_stepwise_messages(
        package,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        current_node_id=task.start_node_id,
        visited_node_ids=[task.start_node_id],
        remaining_action_budget=ROUTE_ACTION_BUDGET,
        route_feature_context=feature_context,
    )
    payload = json.loads(messages[-1]["content"])
    map_audit = audit_r4_stepwise_map_equivalence(package, payload["m"])
    if not map_audit["passed"]:
        raise RuntimeError("batch_task_compact_map_equivalence_failed")
    rendered_prompt = CodexCliRouteStepClient._render_prompt(messages)
    truth_markers = ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")
    truth_in_prompt = any(marker in rendered_prompt for marker in truth_markers)
    if truth_in_prompt:
        raise RuntimeError("evaluator_truth_leaked_to_route_prompt")

    if map_path.is_relative_to(ROOT):
        map_ref = str(map_path.relative_to(ROOT))
    else:
        map_ref = str(map_path)
    route_search = {
        "generation_mode": "stepwise",
        "max_route_actions": ROUTE_ACTION_BUDGET,
        "blocked_edge_ids": list(blocked_edge_ids),
        "minimum_route_actions_lower_bound": initial_constraint.remaining_minimum_moves,
        "initial_allowed_directions": list(initial_constraint.edge_by_direction),
        "initial_filtered_directions": initial_constraint.filtered_directions,
        "output_schema": initial_constraint.schema,
        "output_schema_sha256": initial_constraint.schema_sha256,
        "map_equivalence_audit": map_audit,
        "evaluator_truth_in_prompt": False,
        "messages": messages,
        "rendered_prompt": rendered_prompt,
    }
    return {
        "schema_version": "r4_codex_stepwise_task_preflight_v1",
        "status": "offline_task_preflight_passed_no_model_request",
        "seed": int(row["seed"]),
        "sample_group": row["sample_group"],
        "scenario_type": row.get("scenario_type"),
        "map_ref": {"path": map_ref, "sha256": _sha256_file(map_path)},
        "task_source": {
            "path": str(task_source_path.resolve().relative_to(ROOT)),
            "sha256": _sha256_file(task_source_path),
        },
        "task_public": {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "scenario_type": row.get("scenario_type"),
            "start_node_id": task.start_node_id,
            "pickup_text": task.pickup_text,
            "dropoff_text": task.dropoff_text,
            "vehicle_id": task.vehicle_id,
            "order_id": task.order_id,
            "pickup_node_id": raw.get("pickup_node_id") if task.task_type == "exact_node" else None,
            "dropoff_node_id": raw.get("dropoff_node_id") if task.task_type == "exact_node" else None,
        },
        "route_search": route_search,
        "evaluation_truth": {
            "true_pickup_node_id": truth.get("true_pickup_node_id"),
            "true_dropoff_node_id": truth.get("true_dropoff_node_id"),
        },
        "qwen_route_intent": intent,
        "source_preflight_row": {
            "task_id": row["task_id"],
            "endpoint_gate": row["endpoint_gate"],
            "route_search_gate": row.get("route_search_gate"),
            "intent_source_audit": row.get("intent_source_audit"),
            "source_summary_row_sha256": canonical_json_sha256(row),
        },
        "model_requests": 0,
    }


def export_batch_preflights(summary: Dict[str, Any], output_dir: Path) -> Dict[str, Any]:
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("batch_preflight_output_already_exists")
    rows = summary.get("task_rows", [])
    if len(rows) != 150:
        raise RuntimeError("frozen_preflight_must_contain_150_tasks")
    routeable = [
        row for row in rows
        if row.get("endpoint_gate") == "passed" and row.get("route_chain_reachable") is True
    ]
    skipped = [
        {
            "seed": row["seed"],
            "sample_group": row["sample_group"],
            "task_id": row["task_id"],
            "endpoint_gate": row.get("endpoint_gate"),
            "route_search_gate": row.get("route_search_gate"),
            "route_chain_reachable": row.get("route_chain_reachable"),
            "preflight_errors": row.get("preflight_errors", []),
        }
        for row in rows
        if row not in routeable
    ]
    output_dir.mkdir(parents=True, exist_ok=False)
    task_entries: List[Dict[str, Any]] = []
    for row in routeable:
        preflight = build_task_preflight(row)
        target = output_dir / "tasks" / f"seed_{int(row['seed'])}" / row["task_id"] / "preflight.json"
        _atomic_json(target, preflight)
        task_entries.append({
            "seed": int(row["seed"]),
            "sample_group": row["sample_group"],
            "task_id": row["task_id"],
            "preflight_path": str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else str(target),
            "minimum_route_actions_lower_bound": row["minimum_route_actions_lower_bound"],
        })
    manifest = {
        "schema_version": "r4_codex_stepwise_batch_preflight_manifest_v1",
        "status": "offline_preflight_ready_no_model_requests",
        "formal_result": False,
        "formal_batch_started": False,
        "task_count": len(rows),
        "routeable_task_count": len(routeable),
        "skipped_task_count": len(skipped),
        "minimum_model_request_lower_bound": sum(
            int(row["minimum_route_actions_lower_bound"]) for row in routeable
        ),
        "model_requests": 0,
        "retry_policy": "zero_retries",
        "route_policy": "one_direction_per_request; deterministic local validator gates state advancement; no traditional fallback",
        "tasks": task_entries,
        "skipped_tasks": skipped,
    }
    _atomic_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-preflight", type=Path, default=DEFAULT_SOURCE_PREFLIGHT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    summary = read_json(args.source_preflight)
    manifest = export_batch_preflights(summary, args.output_dir)
    print(json.dumps({key: manifest[key] for key in (
        "status", "task_count", "routeable_task_count", "skipped_task_count",
        "minimum_model_request_lower_bound", "model_requests",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
