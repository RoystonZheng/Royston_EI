#!/usr/bin/env python3
"""Offline EV v2 energy audit for saved pure-LLM grid runs.

Reads saved grid maps, tasks, and model responses only. It preserves all source
artifacts and creates versioned sidecars; it does not call a model or rerun a
route planner.
"""

from __future__ import annotations

import copy
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.energy_model import EnergyModelConfig, model_metadata
from route_experiment.failure_audit import estimate_route_proposal_energy, recover_partial_move_sequence
from route_experiment.llm_adapter import replay_move_sequence
from route_experiment.costs import evaluate_route_candidate
from route_experiment.models import Edge, EvaluationConfig, Node, RoadMap, RouteCandidate
from scripts.audit_pure_llm_failures import _route_paths, _task_lookup

DEFAULT_SOURCES = (
    ROOT / "outputs/route_layer/pure_llm_experiment_v1",
    ROOT / "outputs/route_layer/pure_llm_experiment_v4",
    ROOT / "outputs/route_layer/pure_llm_experiment_v5",
    ROOT / "outputs/route_layer/pure_llm_experiment_v6",
    ROOT / "outputs/route_layer/pure_llm_experiment_v7",
    ROOT / "outputs/route_layer/pure_llm_experiment_v8",
)
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/pure_llm_grid_energy_v2_recalculation_20261026"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def recover_node_sequence(raw_output: Any) -> tuple[list[str], bool]:
    """Read a returned node path or recover complete node IDs from truncated JSON."""

    if not isinstance(raw_output, dict):
        return [], False
    nodes = raw_output.get("node_sequence")
    if isinstance(nodes, list):
        return [str(node) for node in nodes], False
    content = raw_output.get("content")
    if not isinstance(content, str):
        return [], False
    match = re.search(r'"node_sequence"\s*:\s*\[', content)
    if not match:
        return [], False
    node_array = content[match.end():].split("]", 1)[0]
    recovered = re.findall(r'"(node_-?\d+_-?\d+)"', node_array)
    return recovered, bool(recovered)


def move_sequence_from_nodes(nodes: list[str]) -> str:
    """Convert a complete cardinal grid-node chain into direction letters."""

    if len(nodes) < 2:
        return ""
    coordinates = []
    for node_id in nodes:
        match = re.fullmatch(r"node_(-?\d+)_(-?\d+)", node_id)
        if not match:
            return ""
        coordinates.append((int(match.group(1)), int(match.group(2))))
    directions = {(1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S"}
    moves = []
    for left, right in zip(coordinates, coordinates[1:]):
        direction = directions.get((right[0] - left[0], right[1] - left[1]))
        if direction is None:
            return ""
        moves.append(direction)
    return "".join(moves)


def load_map(path: Path) -> RoadMap:
    payload = read_json(path)
    return RoadMap(
        map_id=payload["map_id"],
        version=payload["version"],
        size=payload["size"],
        nodes=[Node(**node) for node in payload["nodes"]],
        edges=[Edge(**edge) for edge in payload["edges"]],
        boundary=payload["boundary"],
        road_state_version=payload.get("road_state_version", "road_state_v1"),
        roadside_features=payload.get("roadside_features", []),
    )


def audit_method(method: dict[str, Any], task: dict[str, Any], road_map: RoadMap,
                 config: EvaluationConfig) -> dict[str, Any]:
    if method.get("status") == "completed" and method.get("routes"):
        routes = method["routes"]
        selected = next(
            (route for route in routes if route.get("route_id") == method.get("best_route_id")),
            next((route for route in routes if route.get("validation_status") in (None, "", "valid")), None),
        )
        if selected is not None:
            evaluation = evaluate_route_candidate(road_map, RouteCandidate(**selected), config)
            return {
                "task_id": method.get("task_id"),
                "method_name": method.get("method_name"),
                "status": method.get("status"),
                "failure_reason": None,
                "response_received": True,
                "valid_route_energy_v2": evaluation.to_dict(),
            }

    metadata = method.get("metadata") or {}
    raw_output = method.get("raw_output") or {}
    reason = str(method.get("failure_reason") or "unknown_failure")
    sequence = metadata.get("move_sequence") or raw_output.get("move_sequence") or ""
    recovered = False
    recovered_nodes = False
    sequence_start_node = task.get("start_node_id")
    path_basis = "move_sequence"
    if not sequence and reason == "llm_output_parse_failed":
        sequence = metadata.get("recovered_partial_move_sequence") or recover_partial_move_sequence(raw_output)
        recovered = bool(sequence)
        if recovered:
            path_basis = "recovered_move_runs"

    if not sequence:
        proposed_nodes, recovered_nodes = recover_node_sequence(raw_output)
        node_moves = move_sequence_from_nodes(proposed_nodes)
        if node_moves:
            sequence = node_moves
            sequence_start_node = proposed_nodes[0]
            path_basis = "recovered_node_sequence_prefix" if recovered_nodes else "returned_node_sequence"
            recovered = recovered or recovered_nodes

    audit: dict[str, Any]
    if sequence and sequence_start_node:
        replay = replay_move_sequence(
            road_map,
            sequence_start_node,
            sequence,
            blocked_edge_ids=task.get("blocked_edge_ids", []),
        )
        audit = estimate_route_proposal_energy(road_map, replay, config)
        if recovered:
            audit["status"] = "partial_estimated"
            if path_basis == "recovered_node_sequence_prefix":
                audit["interpretation"] = (
                    "Partial hypothetical estimate from complete node IDs recovered before response truncation; "
                    "not the full route and not actual vehicle consumption."
                )
            else:
                audit["interpretation"] = (
                    "Partial hypothetical estimate from complete move_runs recovered before response truncation; "
                    "not the full route and not actual vehicle consumption."
                )
        else:
            audit["interpretation"] = (
                "Hypothetical estimate for the model-proposed sequence, not actual vehicle consumption; "
                "legal-prefix energy is reported separately and inferred edges remain explicit."
            )
        audit["recovered_move_sequence"] = recovered
        audit["proposal_path_basis"] = path_basis
        audit["proposal_path_start_node_id"] = sequence_start_node
        audit["task_start_node_id"] = task.get("start_node_id")
        audit["proposal_origin_matches_task_start"] = sequence_start_node == task.get("start_node_id")
        audit["recovered_from_truncated_node_sequence"] = recovered_nodes
        if sequence_start_node != task.get("start_node_id"):
            audit["interpretation"] += (
                " The returned path origin differs from the task vehicle start; its energy is only for the "
                "reported path segment and its prefix is not evidence of an executable task route."
            )
    else:
        audit = {
            "status": "no_route_proposed",
            "route_energy_estimate_kwh": None,
            "validated_prefix_energy_estimate_kwh": None,
            "estimated_route_distance_m": None,
            "proposed_move_count": 0,
            "validated_prefix_move_count": 0,
            "exact_edge_moves": 0,
            "reverse_edge_inferred_moves": 0,
            "proxy_edge_moves": 0,
            "energy_model_version": config.energy_model_version,
            "energy_profile_id": config.energy_profile.profile_id,
            "energy_profile_sha256": config.energy_profile.fingerprint(),
            "interpretation": "No route steps were returned or recoverable; energy is not estimable, not zero.",
            "recovered_move_sequence": False,
            "proposal_path_basis": "no_recoverable_path",
            "proposal_path_start_node_id": None,
            "task_start_node_id": task.get("start_node_id"),
            "proposal_origin_matches_task_start": None,
            "recovered_from_truncated_node_sequence": False,
        }
    return {
        "task_id": method.get("task_id"),
        "method_name": method.get("method_name"),
        "status": method.get("status"),
        "failure_reason": reason,
        "response_received": bool(metadata.get("llm_response_received", raw_output is not None)),
        "validation": method.get("validation"),
        "proposal_energy_audit_v2": audit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        action="append",
        help="Source experiment directory; may be repeated. Defaults to the saved formal grid runs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Versioned sidecar output directory.",
    )
    args = parser.parse_args()
    source_roots = tuple(path.resolve() for path in (args.source or DEFAULT_SOURCES))
    output_root = args.output.resolve()
    config = EvaluationConfig(energy_profile=EnergyModelConfig())
    calculation_script_sha256 = sha256(Path(__file__).resolve())
    summaries = []
    all_records = []

    for source_root in source_roots:
        source_manifest_path = source_root / "manifest.json"
        source_manifest = read_json(source_manifest_path) if source_manifest_path.is_file() else {}
        source_manifest_sha256 = sha256(source_manifest_path) if source_manifest_path.is_file() else None
        per_run_records = []
        source_files = []
        counters: Counter[str] = Counter()

        for seed_dir in sorted(
            (source_root / "seeds").glob("seed_*"),
            key=lambda path: int(path.name.split("_", 1)[1]),
        ):
            map_path = next(seed_dir.glob("map_*_seed_*.json"), None)
            if map_path is None:
                continue
            road_map = load_map(map_path)
            tasks = _task_lookup(seed_dir)
            source_files.append({
                "path": str(map_path.relative_to(ROOT)),
                "sha256": sha256(map_path),
                "kind": "map",
                "seed": int(seed_dir.name.split("_", 1)[1]),
            })
            for task_path in sorted(seed_dir.glob("*_tasks_seed_*.json")) + sorted(
                seed_dir.glob("formal_scenarios_seed_*.json")
            ):
                source_files.append({
                    "path": str(task_path.relative_to(ROOT)),
                    "sha256": sha256(task_path),
                    "kind": "saved_tasks",
                    "seed": int(seed_dir.name.split("_", 1)[1]),
                })

            for scope, routes_path in _route_paths(seed_dir):
                if routes_path is None:
                    continue
                routes_payload = read_json(routes_path)
                source_files.append({
                    "path": str(routes_path.relative_to(ROOT)),
                    "sha256": sha256(routes_path),
                    "kind": "saved_routes",
                    "seed": int(seed_dir.name.split("_", 1)[1]),
                    "scope": scope,
                })
                for item in routes_payload.get("results", []):
                    task = tasks.get(item.get("task_id"), {})
                    methods = item.get("methods", [])
                    for method in methods:
                        if method.get("method_name") != "pure_llm_deepseek":
                            continue
                        record = audit_method(copy.deepcopy(method), task, road_map, config)
                        record.update({
                            "seed": int(seed_dir.name.split("_", 1)[1]),
                            "scope": scope,
                            "source_task_id": item.get("task_id"),
                        })
                        counters["records"] += 1
                        if "valid_route_energy_v2" in record:
                            counters["valid_route_recalculated"] += 1
                            counters["valid_route_energy_kwh_total"] += float(
                                record["valid_route_energy_v2"].get("energy_total_kwh", 0.0) or 0.0
                            )
                        else:
                            audit = record["proposal_energy_audit_v2"]
                            counters["energy_estimated"] += audit.get("status") == "estimated"
                            counters["energy_partially_estimated"] += audit.get("status") == "partial_estimated"
                            counters["not_estimable"] += audit.get("status") == "no_route_proposed"
                            counters["proposal_steps"] += int(audit.get("proposed_move_count", 0) or 0)
                            counters["legal_prefix_steps"] += int(audit.get("validated_prefix_move_count", 0) or 0)
                            if audit.get("status") == "estimated":
                                counters["hypothetical_full_proposal_energy_kwh"] += float(
                                    audit.get("route_energy_estimate_kwh") or 0.0
                                )
                            elif audit.get("status") == "partial_estimated":
                                counters["hypothetical_partial_proposal_energy_kwh"] += float(
                                    audit.get("route_energy_estimate_kwh") or 0.0
                                )
                            counters["legal_prefix_energy_kwh"] += float(
                                audit.get("validated_prefix_energy_estimate_kwh") or 0.0
                            )
                        per_run_records.append(record)
                        all_records.append(record)

        run_id = source_root.name
        output_path = output_root / run_id / "failure_energy_audit_v2.json"
        profile_info = model_metadata(config.energy_profile)
        payload = {
            "schema_version": "pure_llm_grid_energy_audit_ev_longitudinal_v2",
            "source_experiment": str(source_root.relative_to(ROOT)),
            "source_experiment_status": source_manifest.get("status", "no_source_manifest"),
            "source_manifest_sha256": source_manifest_sha256,
            "calculation_scope": "offline energy recalculation from saved maps, tasks, and responses; no model calls or route reruns",
            "energy_model": profile_info,
            "energy_model_module_sha256": sha256(ROOT / "route_experiment/energy_model.py"),
            "calculation_script_sha256": calculation_script_sha256,
            "source_files": source_files,
            "coverage": dict(counters),
            "limitations": [
                "Proposal energy is hypothetical and is not measured vehicle consumption.",
                "Illegal moves use explicit reverse-edge or median-map proxies; keep those counts visible.",
                "Legal-prefix energy stops before the first illegal move and is distinct from full proposal energy.",
                "A missing response or unrecoverable route is not estimable, not zero energy.",
                "Missing grade is treated as flat and marked in per-record model fidelity metadata where available.",
            ],
            "records": per_run_records,
        }
        write_json(output_path, payload)
        summaries.append({
            "source_experiment": str(source_root.relative_to(ROOT)),
            "source_manifest_sha256": source_manifest_sha256,
            "source_status": source_manifest.get("status", "no_source_manifest"),
            "output": str(output_path.relative_to(ROOT)),
            "output_sha256": sha256(output_path),
            "coverage": dict(counters),
            "source_file_count": len(source_files),
        })

    master = {
        "schema_version": "pure_llm_grid_energy_v2_recalculation_manifest_v1",
        "status": "completed_offline_recalculation",
        "created_date": date.today().isoformat(),
        "energy_model": model_metadata(config.energy_profile),
        "energy_model_module_sha256": sha256(ROOT / "route_experiment/energy_model.py"),
        "calculation_script_sha256": calculation_script_sha256,
        "runs": summaries,
        "record_count": len(all_records),
        "limitations": [
            "Failed model proposals do not represent executable or observed vehicle routes.",
            "Energy estimates for invalid proposals are hypothetical and rely on recorded or explicitly inferred edges.",
            "No route choices were rerun or changed by this post-hoc accounting.",
        ],
    }
    write_json(output_root / "manifest.json", master)
    print(json.dumps(master, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
