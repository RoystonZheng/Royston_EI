#!/usr/bin/env python3
"""Run the pure DeepSeek route-layer experiment on the fixed seed inputs.

The runner reuses the map/order/formal-scenario generators from the traditional
first round, but writes to a separate output tree.  A route failure remains a
measured model outcome; infrastructure errors are recorded separately in the
manifest.  Prompts and credentials are never persisted.
"""

import argparse
import html
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.costs import enrich_route_method_result, price_costs
from route_experiment.llm_adapter import DeepSeekClient, LLMRoutePlanner
from route_experiment.mapgen import generate_map
from route_experiment.models import MapConfig, PriceConfig, ResourceUsage, RoadMap, VehicleOrderTask
from route_experiment.scenario import generate_formal_scenario_set
from route_experiment.taskgen import TaskGenerationConfig, generate_order_tasks
from scripts.render_seed_dashboard import render_seed_dashboard
from scripts.render_traditional_traces import render


LLM_METHOD = LLMRoutePlanner.method_name
PROMPT_SCHEMA_VERSION = "pure_llm_route_input_v2"
MAP_INPUT_ENCODING = "grid-directed-mask-v2"
SEMANTIC_POLICY = {
    "resolver": "deepseek_route_planner_with_graph_only_landmarks",
    "baseline_visibility": "graph_only",
    "reads_roadside_features": False,
    "uses_evaluator_truth": False,
    "semantic_tasks_remain_routable": True,
}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without adding a dotenv dependency.

    Existing process environment variables take precedence. Values from this
    file are never written to experiment artifacts or printed by the runner.
    """

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _load_price_profile(path: Path) -> Tuple[PriceConfig, Dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    config = PriceConfig(
        api_price_per_token=float(payload.get("api_price_per_token", 0.0)),
        compute_price_per_ms=float(payload.get("compute_price_per_ms", 0.0)),
        electricity_price_per_kwh=float(payload.get("electricity_price_per_kwh", 0.0)),
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


def _enrich(result, road_map: RoadMap, price_config: PriceConfig) -> None:
    input_tokens = result.resources.input_tokens
    output_tokens = result.resources.output_tokens
    llm_calls = result.resources.llm_calls
    latency = result.resources.llm_latency_ms
    enrich_route_method_result(
        road_map,
        result,
        price_config=price_config,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        llm_calls=llm_calls,
        llm_latency_ms=latency,
    )
    # A failed model route has no vehicle-energy evaluation, but its API and
    # compute resources are still an observed cost of the experiment.
    if result.metadata.get("cost_status") != "evaluated":
        result.monetary_cost = price_costs(result.resources, price_config)
        result.metadata["cost_status"] = "not_evaluated_route_api_recorded"
        result.metadata["pricing_version"] = price_config.pricing_version
        result.metadata["pricing_status"] = price_config.pricing_status
        result.metadata["price_source"] = price_config.price_source
        result.metadata["price_source_url"] = price_config.price_source_url
        result.metadata["reference_station"] = price_config.reference_station
        result.metadata["reference_rate_type"] = price_config.reference_rate_type
        result.metadata["observed_at"] = price_config.observed_at
        result.metadata["price_basis"] = price_config.price_basis


def _summary(results: Iterable[object]) -> Dict[str, object]:
    values = {
        "task_count": 0,
        "completed": 0,
        "failed": 0,
        "valid_routes": 0,
        "evaluated_routes": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_llm_calls": 0,
        "total_llm_latency_ms": 0.0,
        "total_energy_kwh": 0.0,
        "total_monetary_cost": 0.0,
    }
    for result in results:
        values["task_count"] += 1
        values[result.status] += 1
        values["valid_routes"] += result.valid_route_count
        values["evaluated_routes"] += int(result.metadata.get("cost_status") == "evaluated")
        values["total_input_tokens"] += result.resources.input_tokens
        values["total_output_tokens"] += result.resources.output_tokens
        values["total_llm_calls"] += result.resources.llm_calls
        values["total_llm_latency_ms"] += result.resources.llm_latency_ms
        values["total_energy_kwh"] += result.resources.energy_kwh
        values["total_monetary_cost"] += result.monetary_cost.total_monetary_cost
    return values


def _task_results_payload(
    road_map: RoadMap,
    tasks: Iterable[VehicleOrderTask],
    results: Dict[str, object],
    price_profile: Dict[str, object],
    input_type: str,
    seed: int,
    scenario_set=None,
) -> Dict[str, object]:
    items = []
    if scenario_set is None:
        for task in tasks:
            if task.task_id not in results:
                continue
            result = results[task.task_id]
            items.append(
                {
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "methods": [result if isinstance(result, dict) else result.to_dict()],
                }
            )
    else:
        for scenario in scenario_set.scenarios:
            task = scenario.tasks[0]
            if task.task_id not in results:
                continue
            items.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "scenario_type": scenario.scenario_type,
                    "task_id": task.task_id,
                    "methods":[
                        results[task.task_id]
                        if isinstance(results[task.task_id], dict)
                        else results[task.task_id].to_dict()
                    ],
                }
            )
    return {
        "schema_version": "pure_llm_experiment_routes_v1",
        "run_type": "live_deepseek_pure_llm_experiment",
        "input_type": input_type,
        "random_seed": seed,
        "map_id": road_map.map_id,
        "map_version": road_map.version,
        "methods": [LLM_METHOD],
        "semantic_policy": SEMANTIC_POLICY,
        "price_profile": price_profile,
        "results": items,
    }


def _tasks_requiring_attempt(tasks, prior_records):
    """Retry tasks without a model result; keep route outcomes as observations."""

    def needs_retry(record):
        if not isinstance(record, dict):
            return True
        reason = str(record.get("failure_reason") or "")
        if reason.startswith("deepseek_request_failed") or "missing_deepseek_api_key" in reason:
            return True
        # Invalid, unreachable, and unparsable model outputs are measured
        # outcomes. Retry only when no usable provider response was obtained.
        if record.get("status") in ("completed", "failed"):
            return False
        resources = record.get("resources") or {}
        return int(resources.get("llm_calls") or 0) == 0

    return [task for task in tasks if needs_retry(prior_records.get(task.task_id))]


def _serialize_result_record(result):
    return result if isinstance(result, dict) else result.to_dict()


def _load_method_records(path: Path) -> Dict[str, Dict[str, object]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = {}
    for item in payload.get("results", []):
        methods = item.get("methods") or []
        if item.get("task_id") and methods and isinstance(methods[0], dict):
            record = methods[0]
            resources = record.get("resources") or {}
            metadata = record.get("metadata") or {}
            cap = int(metadata.get("max_output_tokens_requested") or 0)
            used = int(resources.get("output_tokens") or 0)
            if cap and used >= cap:
                metadata.setdefault("output_token_limit_reached", True)
                record["metadata"] = metadata
            records[str(item["task_id"])] = record
    return records


def _load_attempt_counts(path: Path) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    if not path.exists():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        task_id = record.get("task_id")
        if task_id:
            counts[str(task_id)] += 1
    return counts


def _append_attempt(path: Path, task_id: str, attempt_number: int, result) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": task_id,
        "attempt_number": attempt_number,
        "method_result": _serialize_result_record(result),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def _summary_from_records(results) -> Dict[str, object]:
    summary = _empty_payload_summary()
    for result in results.values():
        _summarize_result_payload(_serialize_result_record(result), summary)
    return summary


def _write_index(output_dir: Path, details: List[Dict[str, object]]) -> None:
    rows = []
    for detail in details:
        map_preview = Path(str(detail["seed_dashboard_index"])).with_name(
            "map_preview.html"
        ).as_posix()
        rows.append(
            '<tr><td><a href="{seed}">种子总览</a><br>{number}</td>'
            '<td><a href="{map_preview}">原始地图</a></td>'
            '<td>{orders}</td><td>{formal}</td>'
            '<td><a href="{order}">订单轨迹</a></td>'
            '<td><a href="{formal_index}">正式场景轨迹</a></td></tr>'.format(
                seed=html.escape(str(detail["seed_dashboard_index"])),
                map_preview=html.escape(map_preview),
                number=detail["seed"],
                orders=detail["order_trace_count"],
                formal=detail["formal_trace_count"],
                order=html.escape(str(detail["order_visual_index"])),
                formal_index=html.escape(str(detail["formal_visual_index"])),
            )
        )
    output_dir.joinpath("index.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<title>纯大模型路线层实验轨迹</title><style>body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;margin:28px}"
        "table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #ddd;padding:8px;text-align:left}th{background:#eef2ff}</style>"
        "<h1>纯大模型路线层实验轨迹</h1><p>结构化路线规划；点击种子总览查看原地图、算法结果和单条回放。原始 JSON 和 HTML 回放均保留。</p>"
        + "<table><thead><tr><th>种子</th><th>原始地图</th><th>订单记录</th><th>正式场景记录</th><th>订单索引</th><th>正式场景索引</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></html>",
        encoding="utf-8",
    )


def _summarize_result_payload(method: Dict[str, object], summary: Dict[str, object]) -> None:
    resources = method.get("resources") or {}
    money = method.get("monetary_cost") or {}
    summary["task_count"] += 1
    status = method.get("status") or "failed"
    summary[status] = summary.get(status, 0) + 1
    summary["valid_routes"] += int(method.get("valid_route_count") or 0)
    metadata = method.get("metadata") or {}
    summary["evaluated_routes"] += int(metadata.get("cost_status") == "evaluated")
    summary["total_input_tokens"] += int(resources.get("input_tokens") or 0)
    summary["total_output_tokens"] += int(resources.get("output_tokens") or 0)
    summary["total_llm_calls"] += int(resources.get("llm_calls") or 0)
    summary["total_llm_latency_ms"] += float(resources.get("llm_latency_ms") or 0.0)
    summary["total_energy_kwh"] += float(resources.get("energy_kwh") or 0.0)
    summary["total_monetary_cost"] += float(money.get("total_monetary_cost") or 0.0)


def _empty_payload_summary() -> Dict[str, object]:
    return {
        "task_count": 0,
        "completed": 0,
        "failed": 0,
        "valid_routes": 0,
        "evaluated_routes": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_llm_calls": 0,
        "total_llm_latency_ms": 0.0,
        "total_energy_kwh": 0.0,
        "total_monetary_cost": 0.0,
    }


def _consolidate_existing_output(
    output_dir: Path,
    model_requested: str = "deepseek-v4-flash",
    api_base_url: str = "https://api.deepseek.com",
    api_key_env: str = "LLM_API_KEY",
    map_size: int = 100,
) -> Dict[str, object]:
    """Rebuild the root manifest/index from every completed seed directory."""

    details: List[Dict[str, object]] = []
    all_errors: List[str] = []
    models = set()
    for seed_dir in sorted((output_dir / "seeds").glob("seed_*")):
        try:
            seed = int(seed_dir.name.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        order_paths = sorted(seed_dir.glob("*order_routes_costs_seed_*.json"))
        formal_paths = sorted(seed_dir.glob("*formal_routes_costs_seed_*.json"))
        if not order_paths or not formal_paths:
            continue
        order_payload = json.loads(order_paths[0].read_text(encoding="utf-8"))
        formal_payload = json.loads(formal_paths[0].read_text(encoding="utf-8"))
        order_summary = _empty_payload_summary()
        formal_summary = _empty_payload_summary()
        for item in order_payload.get("results", []):
            for method in item.get("methods", []):
                _summarize_result_payload(method, order_summary)
                model = (method.get("metadata") or {}).get("llm_model")
                if model:
                    models.add(model)
                reason = method.get("failure_reason") or ""
                if str(reason).startswith("deepseek_request_failed"):
                    all_errors.append("seed_{0}:{1}".format(seed, reason))
        for item in formal_payload.get("results", []):
            for method in item.get("methods", []):
                _summarize_result_payload(method, formal_summary)
                model = (method.get("metadata") or {}).get("llm_model")
                if model:
                    models.add(model)
                reason = method.get("failure_reason") or ""
                if str(reason).startswith("deepseek_request_failed"):
                    all_errors.append("seed_{0}:{1}".format(seed, reason))
        order_manifest_path = seed_dir / "visualizations" / "orders" / "manifest.json"
        formal_manifest_path = seed_dir / "visualizations" / "formal_scenarios" / "manifest.json"
        order_manifest = json.loads(order_manifest_path.read_text(encoding="utf-8")) if order_manifest_path.exists() else {}
        formal_manifest = json.loads(formal_manifest_path.read_text(encoding="utf-8")) if formal_manifest_path.exists() else {}
        details.append(
            {
                "seed": seed,
                "map_id": order_payload.get("map_id"),
                "order_count": len(order_payload.get("results", [])),
                "formal_count": len(formal_payload.get("results", [])),
                "order_summary": order_summary,
                "formal_summary": formal_summary,
                "order_trace_count": order_manifest.get("trace_count", 0),
                "formal_trace_count": formal_manifest.get("trace_count", 0),
                "order_visual_index": str((seed_dir / "visualizations" / "orders" / "index.html").relative_to(output_dir)),
                "formal_visual_index": str((seed_dir / "visualizations" / "formal_scenarios" / "index.html").relative_to(output_dir)),
                "order_routes_costs": str(order_paths[0].relative_to(output_dir)),
                "formal_routes_costs": str(formal_paths[0].relative_to(output_dir)),
                "seed_dashboard_index": str((seed_dir / "index.html").relative_to(output_dir)),
                "map_json": str(next(seed_dir.glob("map_*_seed_*.json")).relative_to(output_dir)),
                "errors": [
                    str(method.get("failure_reason"))
                    for payload in (order_payload, formal_payload)
                    for item in payload.get("results", [])
                    for method in item.get("methods", [])
                    if str(method.get("failure_reason") or "").startswith("deepseek_request_failed")
                ],
                "result_status": "api_error" if any(
                    str(method.get("failure_reason") or "").startswith("deepseek_request_failed")
                    for payload in (order_payload, formal_payload)
                    for item in payload.get("results", [])
                    for method in item.get("methods", [])
                ) else "completed",
            }
        )
    details.sort(key=lambda item: item["seed"])
    manifest = {
        "schema_version": "pure_llm_experiment_manifest_v1",
        "run_type": "live_deepseek_pure_llm_experiment",
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "map_input_encoding": MAP_INPUT_ENCODING,
        "map_input_mode": "graph_only",
        "roadside_features_provided_to_model": False,
        "route_repair": "none",
        "route_validation": "post_hoc_only",
        "map_size": map_size,
        "seeds": [detail["seed"] for detail in details],
        "methods": [LLM_METHOD],
        "model_requested": model_requested,
        "models_observed": sorted(models),
        "api_base_url": api_base_url,
        "api_key_env": api_key_env,
        "semantic_policy": SEMANTIC_POLICY,
        "status": "api_error" if all_errors else "completed",
        "artifacts_complete": all(
            detail["order_count"] == 10 and detail["formal_count"] == 5
            and detail["order_trace_count"] == 10
            and detail["formal_trace_count"] == 5
            for detail in details
        ) and len(details) == 10,
        "errors": all_errors,
        "total_trace_count": sum(
            detail["order_trace_count"] + detail["formal_trace_count"] for detail in details
        ),
        "seeds_detail": details,
        "index_html": "index.html",
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_index(output_dir, details)
    return manifest


def _checkpoint_seed_results(
    output_dir: Path,
    seed_dir: Path,
    map_path: Path,
    road_map: RoadMap,
    order_tasks: List[VehicleOrderTask],
    order_results: Dict[str, object],
    formal_scenarios: List[object],
    formal_results: Dict[str, object],
    scenario_set,
    price_profile: Dict[str, object],
    seed: int,
) -> None:
    """Persist JSON and replay views after each route request."""

    order_routes_path = seed_dir / "pure_llm_order_routes_costs_seed_{0}.json".format(seed)
    formal_routes_path = seed_dir / "pure_llm_formal_routes_costs_seed_{0}.json".format(seed)
    _write_json(
        order_routes_path,
        _task_results_payload(road_map, order_tasks, order_results, price_profile, "order_batch", seed),
    )
    selected_formal = [scenario.tasks[0] for scenario in formal_scenarios]
    _write_json(
        formal_routes_path,
        _task_results_payload(
            road_map,
            selected_formal,
            formal_results,
            price_profile,
            "formal_scenario_set",
            seed,
            scenario_set=scenario_set,
        ),
    )
    order_visual_dir = seed_dir / "visualizations" / "orders"
    formal_visual_dir = seed_dir / "visualizations" / "formal_scenarios"
    render(map_path, order_routes_path, order_visual_dir)
    render(map_path, formal_routes_path, formal_visual_dir)
    render_seed_dashboard(seed_dir)
    _consolidate_existing_output(
        output_dir,
        model_requested=os.environ.get("LLM_MODEL", "deepseek-flash"),
        api_base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        map_size=road_map.size,
    )


def _run_seed(
    seed: int,
    size: int,
    output_dir: Path,
    planner: LLMRoutePlanner,
    price_config: PriceConfig,
    price_profile: Dict[str, object],
    limit_tasks: Optional[int],
    limit_formal: Optional[int],
    resume: bool = False,
) -> Dict[str, object]:
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
    _write_json(
        scenario_path,
        {
            "set_id": scenario_set.set_id,
            "random_seed": scenario_set.random_seed,
            "map_id": scenario_set.road_map.map_id,
            "map_version": scenario_set.road_map.version,
            "coverage": scenario_set.coverage.to_dict(),
            "metadata": scenario_set.metadata,
            "scenarios": [
                {
                    **scenario.to_dict(),
                    "road_map": None,
                }
                for scenario in scenario_set.scenarios
            ],
        },
    )
    order_tasks = list(batch.tasks)
    formal_scenarios = list(scenario_set.scenarios)
    if limit_tasks is not None:
        order_tasks = order_tasks[: max(0, limit_tasks)]
    if limit_formal is not None:
        formal_scenarios = formal_scenarios[: max(0, limit_formal)]
    order_routes_path = seed_dir / "pure_llm_order_routes_costs_seed_{0}.json".format(seed)
    formal_routes_path = seed_dir / "pure_llm_formal_routes_costs_seed_{0}.json".format(seed)
    order_visual_dir = seed_dir / "visualizations" / "orders"
    formal_visual_dir = seed_dir / "visualizations" / "formal_scenarios"
    attempts_path = seed_dir / "pure_llm_attempts_seed_{0}.jsonl".format(seed)
    existing_order = _load_method_records(order_routes_path) if resume else {}
    existing_formal = _load_method_records(formal_routes_path) if resume else {}
    order_results = {
        task_id: record
        for task_id, record in existing_order.items()
        if task_id not in {task.task_id for task in _tasks_requiring_attempt(order_tasks, existing_order)}
    }
    formal_tasks = [scenario.tasks[0] for scenario in formal_scenarios]
    formal_results = {
        task_id: record
        for task_id, record in existing_formal.items()
        if task_id not in {task.task_id for task in _tasks_requiring_attempt(formal_tasks, existing_formal)}
    }
    attempt_counts = _load_attempt_counts(attempts_path)
    _checkpoint_seed_results(
        output_dir, seed_dir, map_path, road_map, order_tasks, order_results,
        formal_scenarios, formal_results, scenario_set, price_profile, seed,
    )
    for task in _tasks_requiring_attempt(order_tasks, order_results):
        result = planner.plan(road_map, task)
        _enrich(result, road_map, price_config)
        client = getattr(planner, "client", None)
        result.metadata["max_output_tokens_requested"] = getattr(client, "max_tokens", None)
        result.metadata["reasoning_effort_requested"] = getattr(client, "reasoning_effort", None)
        max_tokens = getattr(client, "max_tokens", None)
        output_limit_reached = bool(
            max_tokens and result.resources.output_tokens >= max_tokens
        )
        if output_limit_reached:
            result.metadata["output_token_limit_reached"] = True
        attempt_counts[task.task_id] += 1
        result.metadata["attempt_number"] = attempt_counts[task.task_id]
        _append_attempt(attempts_path, task.task_id, attempt_counts[task.task_id], result)
        order_results[task.task_id] = result
        _checkpoint_seed_results(
            output_dir, seed_dir, map_path, road_map, order_tasks, order_results,
            formal_scenarios, formal_results, scenario_set, price_profile, seed,
        )
        if output_limit_reached:
            print(
                "output_token_limit_reached seed={0} task={1} cap={2}; checkpoint saved; continue batch".format(
                    seed, task.task_id, max_tokens
                ),
                flush=True,
            )
        print(
            "seed={0} scope=order task={1} status={2} model={3} input_tokens={4} output_tokens={5}".format(
                seed,
                task.task_id,
                result.status,
                result.metadata.get("llm_model", ""),
                result.resources.input_tokens,
                result.resources.output_tokens,
            ),
            flush=True,
        )
    for scenario in formal_scenarios:
        task = scenario.tasks[0]
        if task.task_id in formal_results:
            continue
        result = planner.plan(road_map, task, blocked_edge_ids=scenario.blocked_edge_ids)
        _enrich(result, road_map, price_config)
        client = getattr(planner, "client", None)
        result.metadata["max_output_tokens_requested"] = getattr(client, "max_tokens", None)
        result.metadata["reasoning_effort_requested"] = getattr(client, "reasoning_effort", None)
        max_tokens = getattr(client, "max_tokens", None)
        output_limit_reached = bool(
            max_tokens and result.resources.output_tokens >= max_tokens
        )
        if output_limit_reached:
            result.metadata["output_token_limit_reached"] = True
        attempt_counts[task.task_id] += 1
        result.metadata["attempt_number"] = attempt_counts[task.task_id]
        _append_attempt(attempts_path, task.task_id, attempt_counts[task.task_id], result)
        formal_results[task.task_id] = result
        _checkpoint_seed_results(
            output_dir, seed_dir, map_path, road_map, order_tasks, order_results,
            formal_scenarios, formal_results, scenario_set, price_profile, seed,
        )
        if output_limit_reached:
            print(
                "output_token_limit_reached seed={0} task={1} cap={2}; checkpoint saved; continue batch".format(
                    seed, task.task_id, max_tokens
                ),
                flush=True,
            )
        print(
            "seed={0} scope=formal scenario={1} status={2} model={3} input_tokens={4} output_tokens={5}".format(
                seed,
                scenario.scenario_type,
                result.status,
                result.metadata.get("llm_model", ""),
                result.resources.input_tokens,
                result.resources.output_tokens,
            ),
            flush=True,
        )
    _checkpoint_seed_results(
        output_dir, seed_dir, map_path, road_map, order_tasks, order_results,
        formal_scenarios, formal_results, scenario_set, price_profile, seed,
    )
    order_manifest = json.loads(
        (seed_dir / "visualizations" / "orders" / "manifest.json").read_text(encoding="utf-8")
    )
    formal_manifest = json.loads(
        (seed_dir / "visualizations" / "formal_scenarios" / "manifest.json").read_text(encoding="utf-8")
    )
    all_results = list(order_results.values()) + list(formal_results.values())
    infrastructure_errors = [
        record.get("failure_reason")
        for result in all_results
        for record in [_serialize_result_record(result)]
        if record.get("failure_reason")
        and str(record["failure_reason"]).startswith(("missing_deepseek", "deepseek_request_failed"))
    ]
    return {
        "seed": seed,
        "map_id": road_map.map_id,
        "order_count": len(order_tasks),
        "formal_count": len(formal_scenarios),
        "order_summary": _summary_from_records(order_results),
        "formal_summary": _summary_from_records(formal_results),
        "order_trace_count": order_manifest["trace_count"],
        "formal_trace_count": formal_manifest["trace_count"],
        "order_visual_index": str((order_visual_dir / "index.html").relative_to(output_dir)),
        "formal_visual_index": str((formal_visual_dir / "index.html").relative_to(output_dir)),
        "order_routes_costs": str(order_routes_path.relative_to(output_dir)),
        "formal_routes_costs": str(formal_routes_path.relative_to(output_dir)),
        "seed_dashboard_index": str((seed_dir / "index.html").relative_to(output_dir)),
        "map_json": str(map_path.relative_to(output_dir)),
        "errors": infrastructure_errors,
        "result_status": "api_error" if infrastructure_errors else "completed",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--seeds", type=int, nargs="+")
    group.add_argument("--seed-count", type=int, default=10)
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--output-dir", default="outputs/route_layer/pure_llm_experiment_v1")
    parser.add_argument("--price-config-json", default="configs/route_layer/price_smoke_v1.json")
    parser.add_argument(
        "--env-file",
        default="configs/route_layer/llm_api.env",
        help="local KEY=VALUE file; credentials are never written to artifacts",
    )
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env", default="LLM_API_KEY")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--limit-tasks", type=int)
    parser.add_argument("--limit-formal", type=int)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse valid completed routes in the output directory and retry failed or missing tasks",
    )
    parser.add_argument("--consolidate-only", action="store_true")
    args = parser.parse_args()
    _load_env_file(Path(args.env_file))
    model = args.model or os.environ.get("LLM_MODEL", "deepseek-v4-flash")
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
    max_output_tokens = int(
        args.max_output_tokens
        or os.environ.get("LLM_MAX_OUTPUT_TOKENS", "4096")
    )
    reasoning_effort = args.reasoning_effort or os.environ.get(
        "LLM_REASONING_EFFORT", "none"
    )
    if args.consolidate_only:
        manifest = _consolidate_existing_output(
            Path(args.output_dir),
            model_requested=model,
            api_base_url=base_url,
            api_key_env=args.api_key_env,
        )
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "artifacts_complete": manifest["artifacts_complete"],
                    "seeds": manifest["seeds"],
                    "total_trace_count": manifest["total_trace_count"],
                    "manifest": str(Path(args.output_dir) / "manifest.json"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    seeds = args.seeds or list(range(args.seed_start, args.seed_start + args.seed_count))
    if not seeds or len(set(seeds)) != len(seeds):
        raise SystemExit("seeds must be non-empty and unique")
    if args.size <= 1:
        raise SystemExit("--size must be greater than 1")
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise SystemExit("missing environment variable: " + args.api_key_env)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    price_config, price_profile = _load_price_profile(Path(args.price_config_json))
    client = DeepSeekClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=args.timeout,
        retries=args.retries,
        max_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
    )
    planner = LLMRoutePlanner(client)
    details = [
        _run_seed(
            seed,
            args.size,
            output_dir,
            planner,
            price_config,
            price_profile,
            args.limit_tasks,
            args.limit_formal,
            resume=args.resume,
        )
        for seed in seeds
    ]
    errors = [
        "seed_{0}:{1}".format(detail["seed"], error)
        for detail in details
        for error in detail["errors"]
    ]
    manifest = {
        "schema_version": "pure_llm_experiment_manifest_v1",
        "run_type": "live_deepseek_pure_llm_experiment",
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "map_input_encoding": MAP_INPUT_ENCODING,
        "map_input_mode": "graph_only",
        "roadside_features_provided_to_model": False,
        "route_repair": "none",
        "route_validation": "post_hoc_only",
        "map_size": args.size,
        "seeds": list(seeds),
        "methods": [LLM_METHOD],
        "model_requested": model,
        "api_base_url": base_url,
        "api_key_env": args.api_key_env,
        "semantic_policy": SEMANTIC_POLICY,
        "price_profile": price_profile,
        "max_output_tokens": max_output_tokens,
        "reasoning_effort": reasoning_effort,
        "retries": args.retries,
        "resumed_existing_output": args.resume,
        "status": "api_error" if errors else "completed",
        "errors": errors,
        "artifacts_complete": all(
            detail["order_count"] == 10 and detail["formal_count"] == 5
            and detail["order_trace_count"] == 10
            and detail["formal_trace_count"] == 5
            for detail in details
        ) and len(details) == 10,
        "total_trace_count": sum(
            detail["order_trace_count"] + detail["formal_trace_count"] for detail in details
        ),
        "seeds_detail": details,
        "index_html": "index.html",
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_index(output_dir, details)
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
