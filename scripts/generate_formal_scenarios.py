#!/usr/bin/env python3
"""Generate formal route-layer coverage bundles for one or more map seeds."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.models import MapConfig
from route_experiment.scenario import generate_formal_scenario_batch
from route_experiment.taskgen import TaskGenerationConfig, generate_order_tasks


def _scenario_payload(scenario_set):
    scenarios = []
    for scenario in scenario_set.scenarios:
        payload = scenario.to_dict()
        payload.pop("road_map", None)
        scenarios.append(payload)
    return {
        "set_id": scenario_set.set_id,
        "random_seed": scenario_set.random_seed,
        "map_id": scenario_set.road_map.map_id,
        "map_version": scenario_set.road_map.version,
        "coverage": scenario_set.coverage.to_dict(),
        "metadata": scenario_set.metadata,
        "scenarios": scenarios,
    }


def main():
    parser = argparse.ArgumentParser()
    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument("--seeds", type=int, nargs="+")
    seed_group.add_argument("--seed-count", type=int, default=1)
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument(
        "--output-dir", default="outputs/route_layer/formal_scenarios"
    )
    args = parser.parse_args()

    if args.seeds is None and args.seed_count <= 0:
        raise SystemExit("--seed-count must be positive")
    seeds = args.seeds or list(range(args.seed_start, args.seed_start + args.seed_count))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    scenario_sets = generate_formal_scenario_batch(
        seeds=seeds,
        size=args.size,
        config=MapConfig(size=args.size),
    )

    bundles = []
    for scenario_set in scenario_sets:
        seed = scenario_set.random_seed
        map_file = "map_{0}x{0}_seed_{1}.json".format(args.size, seed)
        order_file = "order_tasks_seed_{0}.json".format(seed)
        scenario_file = "formal_scenarios_seed_{0}.json".format(seed)
        (output_dir / map_file).write_text(
            json.dumps(
                scenario_set.road_map.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        (output_dir / scenario_file).write_text(
            json.dumps(
                _scenario_payload(scenario_set),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        order_batch = generate_order_tasks(
            scenario_set.road_map,
            seed=seed,
            config=TaskGenerationConfig(task_count=10, exact_ratio=0.7),
        )
        (output_dir / order_file).write_text(
            json.dumps(
                order_batch.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        bundles.append(
            {
                "seed": seed,
                "set_id": scenario_set.set_id,
                "map_file": map_file,
                "order_file": order_file,
                "scenario_file": scenario_file,
                "scenario_count": len(scenario_set.scenarios),
            }
        )

    manifest = {
        "size": args.size,
        "seeds": list(seeds),
        "bundle_count": len(bundles),
        "bundles": bundles,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
