#!/usr/bin/env python3
"""Run the first offline traditional route-layer experiment.

For every requested seed this runner creates the map, mixed order batch and
formal scenario set, runs R0/R1/R2, applies the shared cost evaluator, and
renders a standalone replay for every task-method search trace.
"""

import argparse
import hashlib
import html
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
from scripts.render_traditional_traces import render
from scripts.render_seed_dashboard import render_seed_dashboard


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


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _write_experiment_index(output_dir: Path, details: List[dict]) -> None:
    """Write the seed-level index from an experiment manifest payload."""
    index_rows = []
    for detail in details:
        map_preview = Path(detail["seed_dashboard_index"]).with_name(
            "map_preview.html"
        ).as_posix()
        index_rows.append(
            "<tr><td><a href=\"{seed_index}\">种子总览</a><br>{seed}</td>"
            "<td><a href=\"{map_preview}\">原始地图</a></td>"
            "<td>{orders}</td><td>{formal}</td>"
            "<td><a href=\"{order_index}\">订单轨迹</a></td>"
            "<td><a href=\"{formal_index}\">正式场景轨迹</a></td></tr>".format(
                seed=detail["seed"],
                orders=detail["order_trace_count"],
                formal=detail["formal_trace_count"],
                seed_index=html.escape(detail["seed_dashboard_index"]),
                map_preview=html.escape(map_preview),
                order_index=html.escape(detail["order_visual_index"]),
                formal_index=html.escape(detail["formal_visual_index"]),
            )
        )
    output_dir.joinpath("index.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<title>传统算法第一轮实验轨迹</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;"
        "margin:28px;color:#1f2937}table{border-collapse:collapse;width:100%}"
        "th,td{border-bottom:1px solid #ddd;padding:8px;text-align:left}"
        "th{background:#eef2ff}</style>"
        "<h1>传统算法第一轮实验轨迹</h1>"
        "<p>点击种子总览查看原地图、算法结果和单条检索回放。原始 JSON 和 HTML 回放均保留。</p>"
        "<table><thead><tr><th>种子</th><th>原始地图</th><th>订单记录</th><th>正式场景记录</th>"
        "<th>订单索引</th><th>正式场景索引</th></tr></thead><tbody>"
        + "".join(index_rows)
        + "</tbody></table></html>",
        encoding="utf-8",
    )


def _load_price_profile(path: Path) -> Tuple[PriceConfig, Dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return (
        PriceConfig(
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
        ),
        payload,
    )


def _enrich(results_by_task, road_map, price_config) -> None:
    for method_results in results_by_task.values():
        for result in method_results:
            enrich_route_method_result(
                road_map, result, price_config=price_config
            )


def _summary(results_by_task) -> Dict[str, Dict[str, object]]:
    summary = defaultdict(
        lambda: {
            "task_count": 0,
            "completed": 0,
            "failed": 0,
            "valid_routes": 0,
            "evaluated_routes": 0,
            "not_evaluated": 0,
            "total_energy_kwh": 0.0,
            "total_electricity_cost": 0.0,
            "total_monetary_cost": 0.0,
            "total_expanded_nodes": 0,
            "total_compute_time_ms": 0.0,
        }
    )
    for method_results in results_by_task.values():
        for result in method_results:
            item = summary[result.method_name]
            item["task_count"] += 1
            item[result.status] += 1
            item["valid_routes"] += result.valid_route_count
            item["total_expanded_nodes"] += int(
                result.metadata.get("expanded_nodes", 0)
            )
            item["total_compute_time_ms"] += float(
                result.metadata.get("compute_time_ms", 0.0)
            )
            if result.metadata.get("cost_status") == "evaluated":
                item["evaluated_routes"] += 1
                item["total_energy_kwh"] += result.resources.energy_kwh
                item["total_electricity_cost"] += (
                    result.monetary_cost.electricity_cost_amount
                )
                item["total_monetary_cost"] += (
                    result.monetary_cost.total_monetary_cost
                )
            else:
                item["not_evaluated"] += 1
    return {method: dict(values) for method, values in sorted(summary.items())}


def _scenario_payload(scenario_set) -> dict:
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


def _order_routes_payload(road_map, batch, results_by_task, price_profile):
    return {
        "schema_version": "traditional_experiment_routes_v1",
        "run_type": "offline_traditional_experiment",
        "input_type": "order_batch",
        "random_seed": batch.random_seed,
        "map_id": road_map.map_id,
        "map_version": road_map.version,
        "methods": list(TRADITIONAL_METHODS),
        "semantic_policy": SEMANTIC_POLICY,
        "price_profile": price_profile,
        "results": [
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "methods": [
                    result.to_dict()
                    for result in results_by_task[task.task_id]
                ],
            }
            for task in batch.tasks
        ],
    }


def _formal_routes_payload(road_map, scenario_set, results_by_type, price_profile):
    items = []
    for scenario in scenario_set.scenarios:
        task = scenario.tasks[0]
        items.append(
            {
                "scenario_id": scenario.scenario_id,
                "scenario_type": scenario.scenario_type,
                "task_id": task.task_id,
                "methods": [
                    result.to_dict()
                    for result in results_by_type[scenario.scenario_type]
                ],
            }
        )
    return {
        "schema_version": "traditional_experiment_formal_routes_v1",
        "run_type": "offline_traditional_experiment",
        "input_type": "formal_scenario_set",
        "random_seed": scenario_set.random_seed,
        "map_id": road_map.map_id,
        "map_version": road_map.version,
        "methods": list(TRADITIONAL_METHODS),
        "semantic_policy": SEMANTIC_POLICY,
        "price_profile": price_profile,
        "coverage": scenario_set.coverage.to_dict(),
        "results": items,
    }


def _run_seed(seed: int, size: int, output_dir: Path, price_config, price_profile):
    road_map = generate_map(seed=seed, size=size, config=MapConfig(size=size))
    batch = generate_order_tasks(
        road_map,
        seed=seed,
        config=TaskGenerationConfig(task_count=10, exact_ratio=0.7),
    )
    scenario_set = generate_formal_scenario_set(road_map, seed=seed)
    seed_dir = output_dir / "seeds" / "seed_{0}".format(seed)
    seed_dir.mkdir(parents=True, exist_ok=True)

    map_path = seed_dir / "map_{0}x{0}_seed_{1}.json".format(size, seed)
    order_path = seed_dir / "order_tasks_seed_{0}.json".format(seed)
    scenario_path = seed_dir / "formal_scenarios_seed_{0}.json".format(seed)
    _write_json(map_path, road_map.to_dict())
    _write_json(order_path, batch.to_dict())
    _write_json(scenario_path, _scenario_payload(scenario_set))

    order_results = run_traditional_methods(
        road_map, batch.tasks, method_names=TRADITIONAL_METHODS
    )
    _enrich(order_results, road_map, price_config)
    formal_results = {}
    for scenario in scenario_set.scenarios:
        task = scenario.tasks[0]
        result = run_traditional_methods(
            road_map,
            [task],
            method_names=TRADITIONAL_METHODS,
            blocked_edge_ids_by_task={task.task_id: scenario.blocked_edge_ids},
        )
        _enrich(result, road_map, price_config)
        formal_results[scenario.scenario_type] = result[task.task_id]

    order_routes_path = seed_dir / "traditional_order_routes_costs_seed_{0}.json".format(seed)
    formal_routes_path = seed_dir / "traditional_formal_routes_costs_seed_{0}.json".format(seed)
    _write_json(
        order_routes_path,
        _order_routes_payload(road_map, batch, order_results, price_profile),
    )
    _write_json(
        formal_routes_path,
        _formal_routes_payload(road_map, scenario_set, formal_results, price_profile),
    )

    order_visual_dir = seed_dir / "visualizations" / "orders"
    formal_visual_dir = seed_dir / "visualizations" / "formal_scenarios"
    order_visual_manifest = render(map_path, order_routes_path, order_visual_dir)
    formal_visual_manifest = render(
        map_path, formal_routes_path, formal_visual_dir
    )
    render_seed_dashboard(seed_dir)

    errors = []
    if batch.exact_task_count != 7 or batch.semantic_task_count != 3:
        errors.append("mixed_task_ratio_mismatch")
    for method, values in _summary(order_results).items():
        if values["completed"] != 10 or values["valid_routes"] != 10:
            errors.append("{0}_order_batch_incomplete".format(method))
        if values["evaluated_routes"] != 10:
            errors.append("{0}_order_cost_not_evaluated".format(method))
    expected_types = {"feasible", "boundary", "illegal_endpoint", "detour", "unreachable"}
    if set(formal_results) != expected_types:
        errors.append("formal_scenario_types_missing")
    for method in TRADITIONAL_METHODS:
        for scenario_type in {"feasible", "boundary", "detour"}:
            if any(
                item.status != "completed"
                for item in formal_results[scenario_type]
                if item.method_name == method
            ):
                errors.append("{0}_{1}_should_complete".format(method, scenario_type))
        for scenario_type in {"illegal_endpoint", "unreachable"}:
            if any(
                item.status != "failed"
                for item in formal_results[scenario_type]
                if item.method_name == method
            ):
                errors.append("{0}_{1}_should_fail".format(method, scenario_type))

    return {
        "seed": seed,
        "map_id": road_map.map_id,
        "task_count": batch.task_count,
        "exact_task_count": batch.exact_task_count,
        "semantic_task_count": batch.semantic_task_count,
        "formal_scenario_types": sorted(formal_results),
        "coverage": scenario_set.coverage.to_dict(),
        "order_summary": _summary(order_results),
        "formal_summary": _summary(formal_results),
        "order_trace_count": order_visual_manifest["trace_count"],
        "formal_trace_count": formal_visual_manifest["trace_count"],
        "order_routes_costs": str(order_routes_path.relative_to(output_dir)),
        "formal_routes_costs": str(formal_routes_path.relative_to(output_dir)),
        "order_visual_index": str(
            (order_visual_dir / "index.html").relative_to(output_dir)
        ),
        "formal_visual_index": str(
            (formal_visual_dir / "index.html").relative_to(output_dir)
        ),
        "seed_dashboard_index": str(
            (seed_dir / "index.html").relative_to(output_dir)
        ),
        "map_json": str(map_path.relative_to(output_dir)),
        "orders_json": str(order_path.relative_to(output_dir)),
        "formal_scenarios_json": str(scenario_path.relative_to(output_dir)),
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--seeds", type=int, nargs="+")
    group.add_argument("--seed-count", type=int, default=10)
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument(
        "--output-dir", default="outputs/route_layer/traditional_experiment_v1"
    )
    parser.add_argument(
        "--price-config-json", default="configs/route_layer/price_smoke_v1.json"
    )
    args = parser.parse_args()
    seeds = args.seeds or list(range(args.seed_start, args.seed_start + args.seed_count))
    if not seeds or len(set(seeds)) != len(seeds):
        raise SystemExit("seeds must be non-empty and unique")
    if args.size <= 1:
        raise SystemExit("--size must be greater than 1")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    price_config, price_profile = _load_price_profile(Path(args.price_config_json))
    details = [
        _run_seed(seed, args.size, output_dir, price_config, price_profile)
        for seed in seeds
    ]
    errors = [
        "seed_{0}:{1}".format(detail["seed"], error)
        for detail in details
        for error in detail["errors"]
    ]
    manifest = {
        "schema_version": "traditional_experiment_manifest_v1",
        "run_type": "offline_traditional_experiment",
        "map_size": args.size,
        "seeds": list(seeds),
        "methods": list(TRADITIONAL_METHODS),
        "semantic_policy": SEMANTIC_POLICY,
        "price_profile": price_profile,
        "status": "failed" if errors else "passed",
        "errors": errors,
        "total_trace_count": sum(
            detail["order_trace_count"] + detail["formal_trace_count"]
            for detail in details
        ),
        "seeds_detail": details,
        "index_html": "index.html",
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_experiment_index(output_dir, details)
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "seeds": list(seeds),
                "total_trace_count": manifest["total_trace_count"],
                "output_dir": str(output_dir),
                "manifest": str(output_dir / "manifest.json"),
                "errors": errors,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
