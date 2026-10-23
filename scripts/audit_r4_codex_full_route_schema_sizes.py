#!/usr/bin/env python3
"""Offline size audit for a candidate one-call Codex R4 route schema.

This script reconstructs the 130 routable frozen 100x100 tasks, builds each
compact route package, and measures the prompt plus the JSON Schema that the
Codex CLI adapter would serialize. It never invokes Codex or an API.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient  # noqa: E402
from route_experiment.llm_adapter import build_r4_route_search_messages  # noqa: E402
from route_experiment.models import (  # noqa: E402
    Edge,
    Node,
    PackageConfig,
    RoadMap,
    RoadsideFeature,
    VehicleOrderTask,
)
from route_experiment.r4_constraints import build_r4_route_output_constraint  # noqa: E402
from route_experiment.routing import (  # noqa: E402
    build_route_input_package,
    route_chain_reachability,
)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_map(path: Path) -> RoadMap:
    raw = _read_json(path)
    return RoadMap(
        map_id=raw["map_id"],
        version=raw["version"],
        size=raw["size"],
        nodes=[Node(**item) for item in raw["nodes"]],
        edges=[Edge(**item) for item in raw["edges"]],
        boundary=raw["boundary"],
        road_state_version=raw["road_state_version"],
        roadside_features=[RoadsideFeature(**item) for item in raw["roadside_features"]],
    )


def _summarize(values):
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "sum": sum(ordered),
        "min": min(ordered),
        "median": ordered[(len(ordered) - 1) // 2],
        "p90": ordered[round((len(ordered) - 1) * 0.9)],
        "max": max(ordered),
        "mean": sum(ordered) / len(ordered),
    }


def audit(seed_root: Path):
    rows = []
    skipped = []
    by_seed = {}
    for seed in range(10):
        seed_dir = seed_root / ("seed_{0}".format(seed))
        road_map = _load_map(seed_dir / ("map_100x100_seed_{0}.json".format(seed)))
        orders = _read_json(seed_dir / ("order_tasks_seed_{0}.json".format(seed)))
        truth_by_task = {item["task_id"]: item for item in orders["ground_truth"]}
        candidates = []
        for raw in orders["tasks"]:
            endpoints = truth_by_task[raw["task_id"]]
            resolved = dict(raw)
            resolved.update(endpoints)
            resolved["pickup_node_id"] = endpoints["true_pickup_node_id"]
            resolved["dropoff_node_id"] = endpoints["true_dropoff_node_id"]
            candidates.append((VehicleOrderTask(**resolved), (), "order"))

        scenarios = _read_json(seed_dir / ("formal_scenarios_seed_{0}.json".format(seed)))
        for scenario in scenarios["scenarios"]:
            for raw in scenario["tasks"]:
                candidates.append(
                    (
                        VehicleOrderTask(**raw),
                        tuple(scenario.get("blocked_edge_ids", [])),
                        scenario["scenario_type"],
                    )
                )

        seed_rows = 0
        for task, blocked_edge_ids, source_type in candidates:
            if source_type == "illegal_endpoint":
                skipped.append({"seed": seed, "task_id": task.task_id, "reason": "illegal_endpoint"})
                continue
            package = build_route_input_package(
                road_map,
                task,
                PackageConfig(),
                blocked_edge_ids=blocked_edge_ids,
            )
            reachability = route_chain_reachability(
                package.edges,
                task.start_node_id,
                task.pickup_node_id,
                task.dropoff_node_id,
                package.blocked_edge_ids,
            )
            if not reachability["chain_reachable"]:
                skipped.append({"seed": seed, "task_id": task.task_id, "reason": "unreachable"})
                continue

            messages = build_r4_route_search_messages(
                package,
                task.start_node_id,
                task.pickup_node_id,
                task.dropoff_node_id,
                require_completed_route=True,
            )
            codex_messages = copy.deepcopy(messages)
            codex_messages[0]["content"] = codex_messages[0]["content"].replace(
                "completed 时必须调用 submit_r4_route 工具",
                "completed 时必须按给定 JSON Schema 返回对象",
            )
            prompt = CodexCliRouteStepClient._render_prompt(codex_messages).encode("utf-8")
            constraint = build_r4_route_output_constraint(
                package,
                task.pickup_node_id,
                task.dropoff_node_id,
                allow_failed=False,
            )
            schema = constraint.tool_definition()["function"]["parameters"]
            schema_bytes = json.dumps(
                schema, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            rows.append(
                {
                    "seed": seed,
                    "task_id": task.task_id,
                    "source_type": source_type,
                    "package_nodes": len(package.nodes),
                    "package_directed_edges": len(package.edges),
                    "prompt_bytes": len(prompt),
                    "schema_bytes": len(schema_bytes),
                    "prompt_plus_schema_bytes": len(prompt) + len(schema_bytes),
                    "schema_sha256": constraint.schema_sha256,
                }
            )
            seed_rows += 1
        by_seed[str(seed)] = seed_rows
        print("seed {0}: {1} routable tasks sized".format(seed, seed_rows), flush=True)

    if len(rows) != 130:
        raise RuntimeError("expected_130_routable_tasks_got_{0}".format(len(rows)))
    return {
        "schema_version": "r4_codex_full_route_schema_size_audit_v1",
        "status": "offline_only_no_model_requests",
        "model_requests_made": 0,
        "method": "build the production full-route prompt, map-derived route schema, and Codex CLI prompt/schema serialization for each frozen routable task; exclude illegal-endpoint and unreachable formal cases",
        "task_count": len(rows),
        "tasks_per_seed": by_seed,
        "prompt_bytes_per_group": _summarize([item["prompt_bytes"] for item in rows]),
        "schema_bytes_per_group": _summarize([item["schema_bytes"] for item in rows]),
        "prompt_plus_schema_bytes_per_group": _summarize(
            [item["prompt_plus_schema_bytes"] for item in rows]
        ),
        "excluded_tasks": skipped,
        "task_rows": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seed-root",
        type=Path,
        default=ROOT / "outputs/route_layer/traditional_experiment_v1/seeds",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "outputs/route_layer/r4_preflight/codex_cli_full_route_schema_size_audit_20261021.json",
    )
    args = parser.parse_args()
    result = audit(args.seed_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("wrote {0}".format(args.output))
    print(json.dumps({key: result[key] for key in (
        "task_count", "tasks_per_seed", "prompt_bytes_per_group",
        "schema_bytes_per_group", "prompt_plus_schema_bytes_per_group"
    )}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
