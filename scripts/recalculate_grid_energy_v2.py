#!/usr/bin/env python3
"""Recalculate saved 100x100 grid route energy without rerunning planners."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from route_experiment.costs import evaluate_route_candidate
from route_experiment.energy_model import EnergyModelConfig, model_metadata
from route_experiment.models import Edge, EvaluationConfig, Node, RoadMap, RouteCandidate


SOURCE_DIR = ROOT / "outputs/route_layer/traditional_experiment_v1"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/traditional_experiment_v1_energy_v2"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def route_structure_sha256(payload: Dict[str, Any]) -> str:
    structure = [
        {
            "task_id": item.get("task_id"),
            "methods": [
                {
                    "method_name": method.get("method_name"),
                    "status": method.get("status"),
                    "routes": [
                        {
                            "route_id": route.get("route_id"),
                            "node_sequence": route.get("node_sequence", []),
                            "edge_sequence": route.get("edge_sequence", []),
                            "validation_status": route.get("validation_status"),
                        }
                        for route in method.get("routes", [])
                    ],
                }
                for method in item.get("methods", [])
            ],
        }
        for item in payload.get("results", [])
    ]
    packed = json.dumps(structure, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def load_map(path: Path) -> RoadMap:
    payload = json.loads(path.read_text(encoding="utf-8"))
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


def select_route(method: Dict[str, Any]) -> Dict[str, Any] | None:
    routes = method.get("routes", [])
    best_id = method.get("best_route_id")
    if best_id:
        for route in routes:
            if route.get("route_id") == best_id:
                return route
    for route in routes:
        if route.get("route_reachability") and route.get("validation_status", "") in ("", "valid"):
            return route
    return None


def recalculate_file(
    source_path: Path,
    target_path: Path,
    road_map: RoadMap,
    prices: Dict[str, Any],
    config: EvaluationConfig,
) -> Dict[str, Any]:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    route_hash_before = route_structure_sha256(payload)
    methods_evaluated = 0
    methods_not_evaluated = 0
    by_method: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"evaluated_routes": 0, "not_evaluated": 0, "total_energy_kwh": 0.0,
                 "total_billable_energy_kwh": 0.0,
                 "total_electricity_cost": 0.0, "total_monetary_cost": 0.0}
    )
    for item in payload.get("results", []):
        for method in item.get("methods", []):
            name = method.get("method_name", "unknown")
            bucket = by_method[name]
            route = select_route(method) if method.get("status") == "completed" else None
            if route is None:
                method.setdefault("metadata", {})["cost_status"] = "not_evaluated"
                method["metadata"]["energy_model_version"] = config.energy_model_version
                method["metadata"]["energy_trace_fidelity"] = "not_evaluated_no_valid_route"
                bucket["not_evaluated"] += 1
                methods_not_evaluated += 1
                continue
            candidate = RouteCandidate(**route)
            evaluation = evaluate_route_candidate(road_map, candidate, config)
            energy_kwh = evaluation.energy_total_kwh
            monetary = method.setdefault("monetary_cost", {})
            api_cost = float(monetary.get("api_cost_amount", 0.0) or 0.0)
            compute_cost = float(monetary.get("compute_cost_amount", 0.0) or 0.0)
            price = float(prices.get("electricity_price_per_kwh", 0.0) or 0.0)
            billable_energy_kwh = max(0.0, energy_kwh)
            electricity_cost = billable_energy_kwh * price
            currency = prices.get("currency", monetary.get("currency", "CNY"))
            method["evaluation"] = evaluation.to_dict()
            method.setdefault("resources", {})["energy_kwh"] = energy_kwh
            method["monetary_cost"] = {
                **monetary,
                "api_cost_amount": api_cost,
                "compute_cost_amount": compute_cost,
                "billable_energy_kwh": billable_energy_kwh,
                "energy_cost_basis": "max(net_route_leg_energy_kwh, 0) * electricity_price_per_kwh",
                "electricity_cost_amount": electricity_cost,
                "total_monetary_cost": api_cost + compute_cost + electricity_cost,
                "currency": currency,
            }
            metadata = method.setdefault("metadata", {})
            metadata.update({
                "cost_status": "evaluated",
                "energy_model_version": evaluation.energy_model_version,
                "energy_profile_id": evaluation.energy_profile_id,
                "energy_profile_sha256": evaluation.energy_profile_sha256,
                "energy_trace_fidelity": evaluation.energy_trace_fidelity,
                "energy_grade_missing": evaluation.energy_grade_missing,
                "energy_recalculation_source": "saved_route_and_saved_map_no_route_rerun",
            })
            bucket["evaluated_routes"] += 1
            bucket["total_energy_kwh"] += energy_kwh
            bucket["total_billable_energy_kwh"] += billable_energy_kwh
            bucket["total_electricity_cost"] += electricity_cost
            bucket["total_monetary_cost"] += api_cost + compute_cost + electricity_cost
            methods_evaluated += 1
    payload["energy_recalculation"] = {
        "status": "completed_offline_recalculation",
        "source_file": str(source_path.relative_to(SOURCE_DIR)),
        "source_sha256": sha256(source_path),
        "recalculation_scope": "energy_and_electricity_cost_only; route_choices_unchanged",
        "electricity_cost_basis": "max(net_route_leg_energy_kwh, 0) * electricity_price_per_kwh",
        "route_structure_sha256_before": route_hash_before,
        "route_structure_sha256_after": route_structure_sha256(payload),
        "energy_model": model_metadata(config.energy_profile),
        "evaluated_routes": methods_evaluated,
        "not_evaluated": methods_not_evaluated,
    }
    payload["summary"] = {name: values for name, values in sorted(by_method.items())}
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "source": str(source_path.relative_to(SOURCE_DIR)),
        "source_sha256": sha256(source_path),
        "output": str(target_path.relative_to(ROOT)),
        "output_sha256": sha256(target_path),
        "route_structure_sha256_before": route_hash_before,
        "route_structure_sha256_after": route_structure_sha256(payload),
        "evaluated_routes": methods_evaluated,
        "not_evaluated": methods_not_evaluated,
        "summary": {name: values for name, values in sorted(by_method.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    source_root = args.source.resolve()
    output_root = args.output.resolve()
    source_manifest_path = source_root / "manifest.json"
    manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    profile = EnergyModelConfig()
    evaluation = EvaluationConfig(energy_profile=profile)
    aggregate: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"evaluated_routes": 0, "not_evaluated": 0, "total_energy_kwh": 0.0,
                 "total_billable_energy_kwh": 0.0,
                 "total_electricity_cost": 0.0, "total_monetary_cost": 0.0}
    )
    file_records: List[Dict[str, Any]] = []

    for seed_detail in manifest.get("seeds_detail", []):
        seed_map_path = source_root / seed_detail["map_json"]
        road_map = load_map(seed_map_path)
        for source_key, output_name in (
            ("order_routes_costs", "order_routes_energy_v2.json"),
            ("formal_routes_costs", "formal_routes_energy_v2.json"),
        ):
            relative = seed_detail.get(source_key)
            if not relative:
                continue
            source_file = source_root / relative
            if not source_file.is_file():
                continue
            target_file = output_root / "seeds" / ("seed_" + str(seed_detail["seed"])) / output_name
            record = recalculate_file(source_file, target_file, road_map, manifest.get("price_profile", {}), evaluation)
            record["seed"] = seed_detail["seed"]
            file_records.append(record)
            for method, numbers in record["summary"].items():
                bucket = aggregate[method]
                for key in bucket:
                    bucket[key] += numbers.get(key, 0.0)

    result_manifest = {
        "schema_version": "traditional_grid_energy_recalculation_v2",
        "status": "completed_offline_recalculation",
        "created_date": "2026-10-26",
        "source_experiment": str(source_root),
        "source_manifest_sha256": sha256(source_manifest_path),
        "source_run_type": manifest.get("run_type"),
        "map_size": manifest.get("map_size"),
        "seeds": manifest.get("seeds", []),
        "methods": manifest.get("methods", []),
        "route_algorithm_rerun": False,
        "route_choices_changed": False,
        "electricity_cost_basis": "max(net_route_leg_energy_kwh, 0) * configured_electricity_price_per_kwh",
        "energy_model": model_metadata(profile),
        "tariff_profile": manifest.get("price_profile", {}),
        "fidelity_limitations": [
            "No saved per-step vehicle speed trace was available; acceleration and deceleration were reconstructed from edge speed caps.",
            "Grid edge grades were absent and were treated as flat.",
            "Congestion used the saved congestion-level speed multipliers; no unobserved queue count or fixed congestion kWh surcharge was added.",
        ],
        "evaluated_route_method_pairs": sum(x["evaluated_routes"] for x in file_records),
        "not_evaluated_route_method_pairs": sum(x["not_evaluated"] for x in file_records),
        "files": file_records,
        "aggregate_summary": {name: values for name, values in sorted(aggregate.items())},
    }
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(result_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    rows = []
    for method, metrics in sorted(aggregate.items()):
        evaluated = int(metrics["evaluated_routes"])
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{:.3f}</td><td>{:.3f}</td><td>{:.3f}</td><td>{:.3f}</td></tr>".format(
                method, evaluated, metrics["total_energy_kwh"], metrics["total_billable_energy_kwh"],
                metrics["total_electricity_cost"], metrics["total_monetary_cost"],
            )
        )
    html = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>网格路网能耗 v2 重算</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;margin:32px;color:#1f2937}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:9px;text-align:left}}th{{background:#eef2ff}}.note{{max-width:1050px;line-height:1.65}}</style>
<h1>100×100 网格路线：能耗 v2 离线重算</h1><p class='note'>路线算法和路线选择未重跑；以下只替换能耗与电费估算。能耗由通用参考电动车纵向模型计算，拥堵通过速度倍率体现，信号灯按停车、怠速和重新起步估算。电费对每条路线腿按 max(净能耗, 0) × 配置电价计算。旧结果仍保存在 traditional_experiment_v1。</p>
<table><thead><tr><th>方法</th><th>可评估路线数</th><th>合计净能耗（kWh）</th><th>合计计费能耗（kWh）</th><th>电费（CNY）</th><th>总金额（CNY）</th></tr></thead><tbody>{}</tbody></table>
<p>模型版本：ev_longitudinal_v2；无逐秒速度记录，速度变化为重建估算；网格坡度缺失按平路处理。逐种子、逐路线明细见 manifest.json 与 seeds/。</p></html>""".format("\n".join(rows))
    (output_root / "index.html").write_text(html, encoding="utf-8")
    print(json.dumps({
        "output_root": str(output_root),
        "manifest_sha256": sha256(manifest_path),
        "files_written": len(file_records),
        "evaluated_route_method_pairs": result_manifest["evaluated_route_method_pairs"],
        "not_evaluated_route_method_pairs": result_manifest["not_evaluated_route_method_pairs"],
        "aggregate_summary": result_manifest["aggregate_summary"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
