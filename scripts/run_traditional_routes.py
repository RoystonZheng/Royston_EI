#!/usr/bin/env python3
"""Run the offline R0/R1/R2 route baselines on generated inputs."""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.models import Edge, Node, RoadMap, RoadsideFeature, VehicleOrderTask
from route_experiment.routing import TRADITIONAL_METHODS, run_traditional_methods


def load_road_map(path: Path) -> RoadMap:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RoadMap(
        map_id=payload["map_id"],
        version=payload["version"],
        size=payload["size"],
        nodes=[Node(**node) for node in payload["nodes"]],
        edges=[Edge(**edge) for edge in payload["edges"]],
        boundary=payload["boundary"],
        road_state_version=payload.get("road_state_version", "road_state_v1"),
        roadside_features=[
            RoadsideFeature(**feature)
            for feature in payload.get("roadside_features", [])
        ],
    )


def _method_summary(results):
    summary = defaultdict(lambda: {"completed": 0, "failed": 0, "valid_routes": 0})
    for method_results in results.values():
        for result in method_results:
            item = summary[result.method_name]
            item[result.status] += 1
            item["valid_routes"] += result.valid_route_count
    return dict(summary)


def _parse_methods(values):
    methods = values or list(TRADITIONAL_METHODS)
    unknown = sorted(set(methods) - set(TRADITIONAL_METHODS))
    if unknown:
        raise SystemExit("unknown methods: " + ",".join(unknown))
    return methods


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-json", required=True)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--order-json")
    input_group.add_argument("--scenario-json")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--methods", nargs="+")
    args = parser.parse_args()

    road_map = load_road_map(Path(args.map_json))
    methods = _parse_methods(args.methods)
    output = {
        "map_id": road_map.map_id,
        "map_version": road_map.version,
        "methods": methods,
        "semantic_policy": {
            "resolver": "graph_only",
            "baseline_visibility": "graph_only",
            "reads_roadside_features": False,
            "uses_evaluator_truth": False,
            "semantic_tasks_remain_routable": True,
        },
    }

    if args.order_json:
        batch = json.loads(Path(args.order_json).read_text(encoding="utf-8"))
        tasks = [VehicleOrderTask(**task) for task in batch["tasks"]]
        results = run_traditional_methods(road_map, tasks, method_names=methods)
        output["input_type"] = "order_batch"
        output["random_seed"] = batch.get("random_seed")
        output["task_count"] = len(tasks)
        output["results"] = [
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "methods": [result.to_dict() for result in results[task.task_id]],
            }
            for task in tasks
        ]
    else:
        scenario_payload = json.loads(
            Path(args.scenario_json).read_text(encoding="utf-8")
        )
        scenarios = scenario_payload["scenarios"]
        tasks = [
            VehicleOrderTask(**scenario["tasks"][0]) for scenario in scenarios
        ]
        blocked_by_task = {
            task.task_id: scenario.get("blocked_edge_ids", [])
            for task, scenario in zip(tasks, scenarios)
        }
        results = run_traditional_methods(
            road_map,
            tasks,
            method_names=methods,
            blocked_edge_ids_by_task=blocked_by_task,
        )
        output["input_type"] = "formal_scenario_set"
        output["random_seed"] = scenario_payload.get("random_seed")
        output["scenario_count"] = len(scenarios)
        output["results"] = [
            {
                "scenario_id": scenario["scenario_id"],
                "scenario_type": scenario["scenario_type"],
                "task_id": task.task_id,
                "methods": [result.to_dict() for result in results[task.task_id]],
            }
            for scenario, task in zip(scenarios, tasks)
        ]

    output["summary"] = _method_summary(results)
    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "map_id": road_map.map_id,
                "input_type": output["input_type"],
                "methods": methods,
                "output_path": str(output_path),
                "summary": output["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
