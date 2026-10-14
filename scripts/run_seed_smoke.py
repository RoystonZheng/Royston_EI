#!/usr/bin/env python3
"""Run a reproducible multi-seed smoke test for the offline route baselines.

The smoke run exercises the complete local chain for a small seed list:
map -> mixed order tasks -> R0/R1/R2 -> shared cost evaluator.  It also
generates the five formal scenario types and records their expected success or
failure statuses.  The default price profile is explicitly marked as
illustrative, so smoke amounts are never mistaken for confirmed prices.
"""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.costs import enrich_route_method_result
from route_experiment.mapgen import generate_map
from route_experiment.models import MapConfig, PriceConfig
from route_experiment.routing import TRADITIONAL_METHODS, run_traditional_methods
from route_experiment.scenario import generate_formal_scenario_set
from route_experiment.taskgen import TaskGenerationConfig, generate_order_tasks


SEMANTIC_POLICY = {
    "resolver": "graph_only",
    "baseline_visibility": "graph_only",
    "reads_roadside_features": False,
    "uses_evaluator_truth": False,
    "semantic_tasks_remain_routable": True,
}


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_price_profile(path: Path) -> Tuple[PriceConfig, Dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    config = PriceConfig(
        api_price_per_token=float(payload.get("api_price_per_token", 0.0)),
        compute_price_per_ms=float(payload.get("compute_price_per_ms", 0.0)),
        electricity_price_per_kwh=float(
            payload.get("electricity_price_per_kwh", 0.0)
        ),
        currency=payload.get("currency", "CNY"),
        pricing_version=payload.get("pricing_version", "price_v1"),
        billing_period=payload.get("billing_period", "simulation"),
        pricing_status=payload.get("pricing_status", "unconfirmed"),
        price_source=payload.get("price_source", "unconfirmed"),
        price_source_url=payload.get("price_source_url", ""),
        reference_station=payload.get("reference_station", ""),
        reference_rate_type=payload.get("reference_rate_type", ""),
        observed_at=payload.get("observed_at", ""),
        price_basis=payload.get("price_basis", ""),
    )
    return config, payload


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _enrich_results(road_map, results_by_task, price_config):
    for method_results in results_by_task.values():
        for result in method_results:
            enrich_route_method_result(
                road_map,
                result,
                price_config=price_config,
            )


def _summarize_results(results_by_task) -> Dict[str, Dict[str, object]]:
    summary = defaultdict(
        lambda: {
            "task_count": 0,
            "completed": 0,
            "failed": 0,
            "valid_routes": 0,
            "evaluated_routes": 0,
            "not_evaluated": 0,
            "total_energy_kwh": 0.0,
            "total_api_cost": 0.0,
            "total_compute_cost": 0.0,
            "total_electricity_cost": 0.0,
            "total_monetary_cost": 0.0,
        }
    )
    for method_results in results_by_task.values():
        for result in method_results:
            bucket = summary[result.method_name]
            bucket["task_count"] += 1
            bucket[result.status] = bucket.get(result.status, 0) + 1
            bucket["valid_routes"] += result.valid_route_count
            if result.metadata.get("cost_status") == "evaluated":
                bucket["evaluated_routes"] += 1
                bucket["total_energy_kwh"] += result.resources.energy_kwh
                bucket["total_api_cost"] += result.monetary_cost.api_cost_amount
                bucket["total_compute_cost"] += result.monetary_cost.compute_cost_amount
                bucket["total_electricity_cost"] += (
                    result.monetary_cost.electricity_cost_amount
                )
                bucket["total_monetary_cost"] += (
                    result.monetary_cost.total_monetary_cost
                )
            else:
                bucket["not_evaluated"] += 1
    return {method: dict(values) for method, values in sorted(summary.items())}


def _semantic_task_count(tasks) -> int:
    return sum(task.task_type != "exact_node" for task in tasks)


def _run_one_seed(
    seed: int,
    size: int,
    output_dir: Path,
    price_config: PriceConfig,
    price_profile: Dict[str, object],
) -> Dict[str, object]:
    road_map = generate_map(seed=seed, size=size, config=MapConfig(size=size))
    batch = generate_order_tasks(
        road_map,
        seed=seed,
        config=TaskGenerationConfig(task_count=10, exact_ratio=0.7),
    )
    scenario_set = generate_formal_scenario_set(road_map, seed=seed)

    map_payload = road_map.to_dict()
    order_payload = batch.to_dict()
    repeat_map = generate_map(seed=seed, size=size, config=MapConfig(size=size))
    repeat_batch = generate_order_tasks(
        repeat_map,
        seed=seed,
        config=TaskGenerationConfig(task_count=10, exact_ratio=0.7),
    )
    deterministic = {
        "map_hash": _stable_hash(map_payload),
        "repeat_map_hash": _stable_hash(repeat_map.to_dict()),
        "order_hash": _stable_hash(order_payload),
        "repeat_order_hash": _stable_hash(repeat_batch.to_dict()),
    }
    deterministic["passed"] = (
        deterministic["map_hash"] == deterministic["repeat_map_hash"]
        and deterministic["order_hash"] == deterministic["repeat_order_hash"]
    )

    mixed_results = run_traditional_methods(
        road_map, batch.tasks, method_names=TRADITIONAL_METHODS
    )
    _enrich_results(road_map, mixed_results, price_config)

    formal_results = {}
    for scenario in scenario_set.scenarios:
        task = scenario.tasks[0]
        results = run_traditional_methods(
            road_map,
            [task],
            method_names=TRADITIONAL_METHODS,
            blocked_edge_ids_by_task={task.task_id: scenario.blocked_edge_ids},
        )
        _enrich_results(road_map, results, price_config)
        formal_results[scenario.scenario_type] = results[task.task_id]

    mixed_summary = _summarize_results(mixed_results)
    formal_summary = _summarize_results(formal_results)
    errors: List[str] = []
    if not deterministic["passed"]:
        errors.append("seed_not_deterministic")
    if batch.exact_task_count != 7 or batch.semantic_task_count != 3:
        errors.append("mixed_task_ratio_mismatch")
    coverage = scenario_set.coverage.to_dict()
    for coverage_flag in (
        "feasible_case",
        "detour_case",
        "unreachable_case",
        "boundary_case",
        "illegal_endpoint_case",
    ):
        if not coverage.get(coverage_flag):
            errors.append("coverage_missing:{0}".format(coverage_flag))
    if set(formal_results) != {
        "feasible",
        "boundary",
        "illegal_endpoint",
        "detour",
        "unreachable",
    }:
        errors.append("formal_scenario_types_missing")
    success_cases = {"feasible", "boundary", "detour"}
    failure_cases = {"illegal_endpoint", "unreachable"}
    for method in TRADITIONAL_METHODS:
        for scenario_type in success_cases:
            if any(
                result.status != "completed"
                for result in formal_results.get(scenario_type, [])
                if result.method_name == method
            ):
                errors.append("{0}_{1}_should_complete".format(method, scenario_type))
        for scenario_type in failure_cases:
            if any(
                result.status != "failed"
                for result in formal_results.get(scenario_type, [])
                if result.method_name == method
            ):
                errors.append("{0}_{1}_should_fail".format(method, scenario_type))
    for method, values in mixed_summary.items():
        if values["completed"] != 10 or values["valid_routes"] != 10:
            errors.append("{0}_mixed_batch_not_complete".format(method))
        if values["evaluated_routes"] != 10:
            errors.append("{0}_mixed_cost_not_evaluated".format(method))

    seed_dir = output_dir / "seeds"
    map_path = seed_dir / "map_{0}x{0}_seed_{1}.json".format(size, seed)
    order_path = seed_dir / "order_tasks_seed_{0}.json".format(seed)
    route_path = seed_dir / "traditional_routes_seed_{0}_costs.json".format(seed)
    summary_path = seed_dir / "smoke_summary_seed_{0}.json".format(seed)
    _write_json(map_path, map_payload)
    _write_json(order_path, order_payload)
    _write_json(
        route_path,
        {
            "schema_version": "traditional_seed_smoke_v1",
            "run_type": "offline_seed_smoke",
            "random_seed": seed,
            "map_id": road_map.map_id,
            "map_version": road_map.version,
            "methods": list(TRADITIONAL_METHODS),
            "semantic_policy": SEMANTIC_POLICY,
            "price_profile": price_profile,
            "results": [
                {
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "methods": [result.to_dict() for result in mixed_results[task.task_id]],
                }
                for task in batch.tasks
            ],
        },
    )
    summary = {
        "schema_version": "traditional_seed_smoke_summary_v1",
        "run_type": "offline_seed_smoke",
        "random_seed": seed,
        "map_size": size,
        "map_id": road_map.map_id,
        "task_count": len(batch.tasks),
        "exact_task_count": batch.exact_task_count,
        "semantic_task_count": _semantic_task_count(batch.tasks),
        "formal_scenario_types": sorted(formal_results),
        "coverage": scenario_set.coverage.to_dict(),
        "deterministic_check": deterministic,
        "semantic_policy": SEMANTIC_POLICY,
        "price_profile": price_profile,
        "mixed_summary": mixed_summary,
        "formal_summary": formal_summary,
        "errors": errors,
        "artifacts": {
            "map": str(map_path),
            "orders": str(order_path),
            "routes_and_costs": str(route_path),
        },
    }
    summary["artifacts"]["summary"] = str(summary_path)
    _write_json(summary_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument("--seeds", type=int, nargs="+")
    seed_group.add_argument("--seed-count", type=int, default=2)
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument(
        "--output-dir", default="outputs/route_layer/traditional_seed_smoke"
    )
    parser.add_argument(
        "--price-config-json",
        default="configs/route_layer/price_smoke_v1.json",
    )
    args = parser.parse_args()
    if args.seeds is None and args.seed_count <= 0:
        raise SystemExit("--seed-count must be positive")
    seeds = args.seeds or list(
        range(args.seed_start, args.seed_start + args.seed_count)
    )
    if len(set(seeds)) != len(seeds):
        raise SystemExit("seeds must be unique")
    if args.size <= 1:
        raise SystemExit("--size must be greater than 1")

    price_config, price_profile = _load_price_profile(Path(args.price_config_json))
    output_dir = Path(args.output_dir)
    summaries = [
        _run_one_seed(seed, args.size, output_dir, price_config, price_profile)
        for seed in seeds
    ]
    errors = [
        "seed_{0}:{1}".format(summary["random_seed"], error)
        for summary in summaries
        for error in summary["errors"]
    ]
    manifest = {
        "schema_version": "traditional_seed_smoke_manifest_v1",
        "run_type": "offline_seed_smoke",
        "map_size": args.size,
        "seeds": list(seeds),
        "price_profile": price_profile,
        "semantic_policy": SEMANTIC_POLICY,
        "status": "failed" if errors else "passed",
        "errors": errors,
        "summaries": summaries,
    }
    manifest_path = output_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "seeds": list(seeds),
                "output_dir": str(output_dir),
                "manifest": str(manifest_path),
                "errors": errors,
                "price_profile": price_profile.get("pricing_version"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
