#!/usr/bin/env python3
"""Attach shared traffic, energy and monetary costs to route results."""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.costs import enrich_route_method_result
from route_experiment.models import (
    Edge,
    EvaluationConfig,
    EvaluationResult,
    Node,
    PriceConfig,
    RoadMap,
    RouteCandidate,
    RouteMethodResult,
    ResourceUsage,
    MonetaryCost,
    ValidationResult,
)


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
    )


def _parse_method(payload):
    routes = [RouteCandidate(**route) for route in payload.get("routes", [])]
    validation = payload.get("validation")
    if validation is not None:
        validation = ValidationResult(**validation)
    evaluation = payload.get("evaluation")
    if evaluation is not None:
        evaluation = EvaluationResult(**evaluation)
    resources = ResourceUsage(**payload.get("resources", {}))
    money = MonetaryCost(**payload.get("monetary_cost", {}))
    return RouteMethodResult(
        task_id=payload["task_id"],
        method_name=payload["method_name"],
        status=payload.get("status", "completed"),
        routes=routes,
        best_route_id=payload.get("best_route_id"),
        valid_route_count=payload.get("valid_route_count", 0),
        invalid_route_count=payload.get("invalid_route_count", 0),
        failure_reason=payload.get("failure_reason"),
        validation=validation,
        evaluation=evaluation,
        resources=resources,
        monetary_cost=money,
        metadata=payload.get("metadata", {}),
        raw_output=payload.get("raw_output"),
    )


def _summary(results):
    summary = defaultdict(
        lambda: {
            "evaluated_routes": 0,
            "not_evaluated": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_llm_calls": 0,
            "total_llm_latency_ms": 0.0,
            "total_compute_time_ms": 0.0,
            "total_energy_kwh": 0.0,
            "total_api_cost": 0.0,
            "total_compute_cost": 0.0,
            "total_electricity_cost": 0.0,
            "total_monetary_cost": 0.0,
            "currency": "CNY",
        }
    )
    for item in results:
        for method in item["methods"]:
            result = method
            bucket = summary[result.method_name]
            bucket["total_input_tokens"] += result.resources.input_tokens
            bucket["total_output_tokens"] += result.resources.output_tokens
            bucket["total_llm_calls"] += result.resources.llm_calls
            bucket["total_llm_latency_ms"] += result.resources.llm_latency_ms
            bucket["total_compute_time_ms"] += result.resources.compute_time_ms
            if result.metadata.get("cost_status") == "evaluated":
                bucket["evaluated_routes"] += 1
                bucket["total_energy_kwh"] += result.resources.energy_kwh
                bucket["total_api_cost"] += result.monetary_cost.api_cost_amount
                bucket["total_compute_cost"] += result.monetary_cost.compute_cost_amount
                bucket["total_electricity_cost"] += (
                    result.monetary_cost.electricity_cost_amount
                )
                bucket["total_monetary_cost"] += result.monetary_cost.total_monetary_cost
                bucket["currency"] = result.monetary_cost.currency
            else:
                bucket["not_evaluated"] += 1
    return dict(summary)


def _load_price_config(path: Path, args) -> PriceConfig:
    """Load a named price profile, with CLI values overriding its rates."""

    payload = {}
    if path is not None:
        payload = json.loads(path.read_text(encoding="utf-8"))
    return PriceConfig(
        api_price_per_token=(
            args.api_price_per_token
            if args.api_price_per_token is not None
            else payload.get("api_price_per_token", 0.0)
        ),
        compute_price_per_ms=(
            args.compute_price_per_ms
            if args.compute_price_per_ms is not None
            else payload.get("compute_price_per_ms", 0.0)
        ),
        electricity_price_per_kwh=(
            args.electricity_price_per_kwh
            if args.electricity_price_per_kwh is not None
            else payload.get("electricity_price_per_kwh", 0.0)
        ),
        currency=args.currency or payload.get("currency", "CNY"),
        pricing_version=args.pricing_version or payload.get(
            "pricing_version", "price_v1"
        ),
        billing_period=args.billing_period or payload.get(
            "billing_period", "simulation"
        ),
        pricing_status=args.pricing_status or payload.get(
            "pricing_status", "unconfirmed"
        ),
        price_source=args.price_source or payload.get(
            "price_source", "unconfirmed"
        ),
        price_source_url=args.price_source_url or payload.get(
            "price_source_url", ""
        ),
        reference_station=args.reference_station or payload.get(
            "reference_station", ""
        ),
        reference_rate_type=args.reference_rate_type or payload.get(
            "reference_rate_type", ""
        ),
        observed_at=args.observed_at or payload.get("observed_at", ""),
        price_basis=args.price_basis or payload.get("price_basis", ""),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-json", required=True)
    parser.add_argument("--routes-json", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--price-config-json")
    parser.add_argument("--api-price-per-token", type=float)
    parser.add_argument("--compute-price-per-ms", type=float)
    parser.add_argument("--electricity-price-per-kwh", type=float)
    parser.add_argument("--currency")
    parser.add_argument("--pricing-version")
    parser.add_argument("--billing-period")
    parser.add_argument("--pricing-status")
    parser.add_argument("--price-source")
    parser.add_argument("--price-source-url")
    parser.add_argument("--reference-station")
    parser.add_argument("--reference-rate-type")
    parser.add_argument("--observed-at")
    parser.add_argument("--price-basis")
    parser.add_argument("--input-tokens", type=int, default=0)
    parser.add_argument("--output-tokens", type=int, default=0)
    parser.add_argument("--llm-calls", type=int, default=0)
    parser.add_argument("--llm-latency-ms", type=float, default=0.0)
    parser.add_argument("--moving-kwh-per-km", type=float, default=0.18)
    parser.add_argument("--signal-idle-kwh-per-sec", type=float, default=0.00003)
    parser.add_argument("--congestion-extra-kwh-per-km-level", type=float, default=0.01)
    parser.add_argument("--energy-model-version", default="energy_v1")
    args = parser.parse_args()

    road_map = load_road_map(Path(args.map_json))
    input_payload = json.loads(Path(args.routes_json).read_text(encoding="utf-8"))
    evaluation_config = EvaluationConfig(
        moving_kwh_per_km=args.moving_kwh_per_km,
        signal_idle_kwh_per_sec=args.signal_idle_kwh_per_sec,
        congestion_extra_kwh_per_km_level=args.congestion_extra_kwh_per_km_level,
        energy_model_version=args.energy_model_version,
    )
    price_config_path = (
        Path(args.price_config_json) if args.price_config_json else None
    )
    price_config = _load_price_config(price_config_path, args)

    output = dict(input_payload)
    enriched_items = []
    for item in input_payload.get("results", []):
        enriched_methods = []
        for method_payload in item.get("methods", []):
            result = _parse_method(method_payload)
            enrich_route_method_result(
                road_map,
                result,
                evaluation_config=evaluation_config,
                price_config=price_config,
                input_tokens=args.input_tokens,
                output_tokens=args.output_tokens,
                llm_calls=args.llm_calls,
                llm_latency_ms=args.llm_latency_ms,
            )
            enriched_methods.append(result)
        enriched_item = dict(item)
        enriched_item["methods"] = enriched_methods
        enriched_items.append(enriched_item)

    output["results"] = []
    for item in enriched_items:
        serialized_item = dict(item)
        serialized_item["methods"] = [method.to_dict() for method in item["methods"]]
        output["results"].append(serialized_item)
    output["cost_config"] = {
        "evaluation": evaluation_config.to_dict(),
        "pricing": price_config.to_dict(),
    }
    output["summary"] = _summary(enriched_items)
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
                "output_path": str(output_path),
                "summary": output["summary"],
                "pricing": price_config.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
