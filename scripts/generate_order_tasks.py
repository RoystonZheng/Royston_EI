#!/usr/bin/env python3
"""Generate route-layer order tasks from an existing map JSON artifact."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.models import Edge, Node, RoadMap, RoadsideFeature
from route_experiment.taskgen import TaskGenerationConfig, generate_order_tasks


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("map_json")
    parser.add_argument("output_json")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--task-count", type=int, default=10)
    parser.add_argument("--exact-ratio", type=float, default=0.7)
    args = parser.parse_args()

    road_map = load_road_map(Path(args.map_json))
    batch = generate_order_tasks(
        road_map,
        seed=args.seed,
        config=TaskGenerationConfig(
            task_count=args.task_count,
            exact_ratio=args.exact_ratio,
        ),
    )
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(batch.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "map_id": batch.map_id,
                "random_seed": batch.random_seed,
                "task_count": batch.task_count,
                "exact_task_count": batch.exact_task_count,
                "semantic_task_count": batch.semantic_task_count,
                "semantic_type_counts": batch.semantic_type_counts,
                "output_path": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
