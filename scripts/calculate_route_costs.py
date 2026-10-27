#!/usr/bin/env python3
"""Attach shared traffic, energy and monetary costs to route results."""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.costs import enrich_route_method_result, evaluate_route_candidate
from route_experiment.decision import (
    DECISION_MODEL_VERSION,
    DECISION_PROFILES,
    aggregate_route_segment_economics,
    score_route_candidates,
)
from route_experiment.energy_model import EnergyModelConfig, model_metadata
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _route_decisions_for_item(
    item,
    road_map,
    evaluation_config,
    price_config,
    *,
    fare_per_km=None,
    segment_type="passenger",
):
    """Compare all feasible planner routes in one shared task-level pool."""

    electricity_price_available = (
        price_config.electricity_price_per_kwh is not None
        and price_config.electricity_price_per_kwh > 0
        and price_config.pricing_status != "unconfirmed"
        and price_config.price_source != "unconfirmed"
    )
    electricity_price = (
        price_config.electricity_price_per_kwh
        if electricity_price_available
        else None
    )
    observed_fare = item.get("fare_revenue_amount")
    observed_fare_currency = item.get("fare_currency")
    fare_currency_mismatch = (
        observed_fare is not None
        and observed_fare_currency != price_config.currency
    )

    method_decisions = {}
    candidate_catalog = []
    score_candidates = []
    feasible_count = 0
    blocked_candidates = []

    for method_payload in item.get("methods", []):
        method = _parse_method(method_payload)
        method_catalog = []
        method_candidate_ids = []
        method_feasible_count = 0
        method_scoreable_count = 0

        token_total = method.resources.input_tokens + method.resources.output_tokens
        usage_known = (
            method.resources.llm_calls > 0
            or token_total > 0
            or method.metadata.get("usage_accounting_status") == "reported"
            or method.metadata.get("cost_status") == "evaluated"
        )
        api_cost = None
        if usage_known and token_total == 0 and method.resources.llm_calls == 0:
            api_cost = 0.0
        elif (
            usage_known
            and price_config.api_price_per_token is not None
            and price_config.api_price_per_token > 0
        ):
            api_cost = token_total * price_config.api_price_per_token

        compute_ms = method.metadata.get("compute_time_ms")
        if compute_ms is None and method.resources.compute_time_ms > 0:
            compute_ms = method.resources.compute_time_ms
        compute_cost = None
        if compute_ms is not None:
            try:
                compute_ms = float(compute_ms)
            except (TypeError, ValueError):
                compute_ms = None
        if compute_ms == 0:
            compute_cost = 0.0
        elif (
            compute_ms is not None
            and compute_ms > 0
            and price_config.compute_price_per_ms is not None
            and price_config.compute_price_per_ms > 0
        ):
            compute_cost = compute_ms * price_config.compute_price_per_ms
        method_money_complete = api_cost is not None and compute_cost is not None
        method_costs = {
            "currency": price_config.currency,
            "api_cost_amount": api_cost,
            "compute_cost_amount": compute_cost,
            "known_method_cost_subtotal": (
                sum(value for value in (api_cost, compute_cost) if value is not None)
                if api_cost is not None or compute_cost is not None
                else None
            ),
            "complete_for_declared_scope": method_money_complete,
            "accounted_once_per_method": True,
            "null_reasons": {
                **({"api_cost_amount": "usage_or_matching_api_rate_missing"} if api_cost is None else {}),
                **({"compute_cost_amount": "compute_time_or_matching_compute_rate_missing"} if compute_cost is None else {}),
            },
        }

        for route in method.routes:
            candidate_id = f"{method.method_name}::{route.route_id}"
            method_candidate_ids.append(candidate_id)
            if not route.route_reachability or route.validation_status not in ("valid", ""):
                candidate = {
                    "candidate_id": candidate_id,
                    "method_name": method.method_name,
                    "route_id": route.route_id,
                    "hard_feasible": False,
                    "feasible": False,
                    "scoreable_for_time_money": False,
                    "eligible": False,
                    "exclusion_reason": "hard_constraint_failed_or_unvalidated",
                }
                method_catalog.append(candidate)
                candidate_catalog.append(candidate)
                continue

            try:
                evaluation = evaluate_route_candidate(road_map, route, evaluation_config)
            except (ValueError, KeyError) as error:
                candidate = {
                    "candidate_id": candidate_id,
                    "method_name": method.method_name,
                    "route_id": route.route_id,
                    "hard_feasible": False,
                    "feasible": False,
                    "scoreable_for_time_money": False,
                    "eligible": False,
                    "exclusion_reason": str(error),
                }
                method_catalog.append(candidate)
                candidate_catalog.append(candidate)
                continue

            method_feasible_count += 1
            feasible_count += 1
            fare_for_candidate = (
                float(observed_fare)
                if observed_fare is not None and not fare_currency_mismatch
                else None
            )
            economics = aggregate_route_segment_economics(
                [
                    {
                        "segment_id": route.route_id,
                        "distance_m": evaluation.route_distance_m,
                        "energy_kwh": evaluation.energy_total_kwh,
                        "segment_type": segment_type,
                        "road_links": evaluation.energy_segments,
                        "fare_revenue_amount": fare_for_candidate,
                        "fare_per_km": fare_per_km,
                        "price_basis": (
                            f"{price_config.pricing_version}:{price_config.price_source}"
                        ),
                        "fare_basis": (
                            item.get("fare_basis")
                            or (
                                "observed_task_fare_amount"
                                if fare_for_candidate is not None
                                else None
                            )
                        ),
                    }
                ],
                currency=price_config.currency,
                electricity_price_per_kwh=electricity_price,
                fare_per_km=fare_per_km,
                price_basis=f"{price_config.pricing_version}:{price_config.price_source}",
            )

            missing_money_inputs = []
            if economics.get("energy_kwh") is None:
                missing_money_inputs.append("route_energy")
            if economics.get("electricity_cost_amount") is None:
                missing_money_inputs.append("electricity_tariff")
            if economics.get("fare_revenue_amount") is None:
                missing_money_inputs.append(
                    "fare_currency_missing_or_mismatch"
                    if fare_currency_mismatch
                    else "passenger_fare"
                )
            if api_cost is None:
                missing_money_inputs.append("api_usage_or_rate")
            if compute_cost is None:
                missing_money_inputs.append("compute_time_or_rate")
            segment_net_cost = economics.get("net_economic_cost")
            if segment_net_cost is None:
                missing_money_inputs.append("segment_net_cost")

            scoreable = not missing_money_inputs
            money_cost = (
                segment_net_cost + api_cost + compute_cost
                if scoreable
                else None
            )
            candidate = {
                "candidate_id": candidate_id,
                "method_name": method.method_name,
                "route_id": route.route_id,
                "hard_feasible": True,
                "feasible": True,
                "scoreable_for_time_money": scoreable,
                "eligible": scoreable,
                "exclusion_reason": (
                    None
                    if scoreable
                    else "incomplete_economic_inputs:" + ",".join(missing_money_inputs)
                ),
                "missing_money_inputs": missing_money_inputs,
                "travel_time_sec": evaluation.total_travel_time_sec,
                "distance_m": evaluation.route_distance_m,
                "energy_kwh": evaluation.energy_total_kwh,
                "segment_net_economic_cost": segment_net_cost,
                "money_cost": money_cost,
                "method_cost_ref": method.method_name,
                "economics": economics,
            }
            method_catalog.append(candidate)
            candidate_catalog.append(candidate)
            if scoreable:
                method_scoreable_count += 1
                score_candidates.append({
                    "candidate_id": candidate_id,
                    "method_name": method.method_name,
                    "feasible": True,
                    "travel_time_sec": evaluation.total_travel_time_sec,
                    "money_cost": money_cost,
                })
            else:
                blocked_candidates.append({
                    "candidate_id": candidate_id,
                    "method_name": method.method_name,
                    "reason": candidate["exclusion_reason"],
                })

        if method_feasible_count == 0:
            method_status = "no_feasible_candidates"
        elif method_scoreable_count < method_feasible_count:
            method_status = "blocked_incomplete_economic_inputs"
        else:
            method_status = "candidate_data_complete"
        method_decisions[method.method_name] = {
            "status": method_status,
            "candidate_count": len(method_catalog),
            "hard_feasible_candidate_count": method_feasible_count,
            "money_scoreable_candidate_count": method_scoreable_count,
            "candidate_ids": method_candidate_ids,
            "method_costs": method_costs,
            "candidate_catalog": method_catalog,
        }

    profile_results = {}
    for profile_id, profile in DECISION_PROFILES.items():
        if feasible_count == 0:
            profile_results[profile_id] = score_route_candidates(
                [], profile, currency=price_config.currency
            )
            continue
        if blocked_candidates:
            profile_results[profile_id] = {
                "model_version": DECISION_MODEL_VERSION,
                "profile": {
                    "profile_id": profile.profile_id,
                    "time_weight": profile.time_weight,
                    "money_weight": profile.money_weight,
                },
                "normalization": "min_max_over_same_feasible_candidate_set_per_scenario",
                "candidate_pool_scope": "same_task_scenario_across_all_methods",
                "candidate_count": 0,
                "hard_feasible_candidate_count": feasible_count,
                "money_scoreable_candidate_count": len(score_candidates),
                "blocked_candidate_count": len(blocked_candidates),
                "currency": price_config.currency,
                "excluded_candidates": [],
                "incomplete_candidates": blocked_candidates,
                "candidates": [],
                "selected_candidate_id": None,
                "status": "blocked_incomplete_economic_inputs",
            }
        else:
            profile_results[profile_id] = score_route_candidates(
                score_candidates,
                profile,
                currency=price_config.currency,
            )
            profile_results[profile_id]["candidate_pool_scope"] = (
                "same_task_scenario_across_all_methods"
            )

    for method_name, decision in method_decisions.items():
        method_ids = set(decision["candidate_ids"])
        decision["profiles"] = {
            profile_id: {
                "status": profile_result["status"],
                "candidate_pool_scope": "same_task_scenario_across_all_methods",
                "selected_candidate_id": (
                    profile_result.get("selected_candidate_id")
                    if profile_result.get("selected_candidate_id") in method_ids
                    else None
                ),
                "selected_by_shared_pool": (
                    profile_result.get("selected_candidate_id") in method_ids
                ),
            }
            for profile_id, profile_result in profile_results.items()
        }

    if feasible_count == 0:
        overall_status = "no_feasible_candidates" if method_decisions else "no_methods"
    elif blocked_candidates:
        overall_status = "blocked_incomplete_economic_inputs"
    else:
        overall_status = "scored"

    return {
        "model_version": DECISION_MODEL_VERSION,
        "status": overall_status,
        "pricing_status": price_config.pricing_status,
        "price_source": price_config.price_source,
        "price_source_url": price_config.price_source_url,
        "pricing_version": price_config.pricing_version,
        "currency": price_config.currency,
        "fare_per_km_scenario": fare_per_km,
        "fare_currency_mismatch": fare_currency_mismatch,
        "fare_currency_status": (
            "not_applicable"
            if observed_fare is None
            else "matched"
            if observed_fare_currency == price_config.currency
            else "missing_or_mismatched"
        ),
        "segment_type": segment_type,
        "candidate_pool": {
            "scope": "same_task_scenario_across_all_methods",
            "candidate_count": feasible_count,
            "generated_route_count": len(candidate_catalog),
            "hard_feasible_candidate_count": feasible_count,
            "money_scoreable_candidate_count": len(score_candidates),
            "blocked_candidate_count": len(blocked_candidates),
            "candidate_ids": [
                candidate["candidate_id"]
                for candidate in candidate_catalog
                if candidate.get("hard_feasible")
            ],
            "shared_across_profiles": True,
        },
        "profiles": profile_results,
        "method_decisions": method_decisions,
        "accounting_limit": (
            "Route segment economics contain priced electricity minus known fare "
            "revenue. API/token and compute charges are held once at method level "
            "and added once to each alternative method-route decision. Missing "
            "inputs block shared-pool scoring. Full vehicle profitability is not "
            "established."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-json", required=True)
    parser.add_argument("--routes-json", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--price-config-json")
    parser.add_argument("--energy-profile-json")
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
    parser.add_argument(
        "--enable-route-decisions",
        action="store_true",
        help="write versioned time/money decisions as a sidecar field without changing original routes",
    )
    parser.add_argument("--fare-per-km", type=float)
    parser.add_argument(
        "--decision-segment-type",
        choices=("deadhead", "passenger"),
        default="passenger",
    )
    args = parser.parse_args()

    road_map = load_road_map(Path(args.map_json))
    input_payload = json.loads(Path(args.routes_json).read_text(encoding="utf-8"))
    energy_profile = EnergyModelConfig()
    if args.energy_profile_json:
        profile_payload = json.loads(Path(args.energy_profile_json).read_text(encoding="utf-8"))
        energy_profile = EnergyModelConfig(**profile_payload)
    evaluation_config = EvaluationConfig(energy_profile=energy_profile)
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
        if args.enable_route_decisions:
            enriched_item["route_decisions"] = _route_decisions_for_item(
                item,
                road_map,
                evaluation_config,
                price_config,
                fare_per_km=args.fare_per_km,
                segment_type=args.decision_segment_type,
            )
        enriched_items.append(enriched_item)

    output["results"] = []
    for item in enriched_items:
        serialized_item = dict(item)
        serialized_item["methods"] = [method.to_dict() for method in item["methods"]]
        output["results"].append(serialized_item)
    output["cost_config"] = {
        "evaluation": evaluation_config.to_dict(),
        "pricing": price_config.to_dict(),
        "energy_model": model_metadata(energy_profile),
    }
    if args.enable_route_decisions:
        output["route_decision_config"] = {
            "model_version": DECISION_MODEL_VERSION,
            "candidate_pool_scope": "same_task_scenario_across_all_methods",
            "candidate_pool_shared_across_profiles": True,
            "economic_input_completeness_required_for_selection": True,
            "profiles": {
                profile_id: {
                    "time_weight": profile.time_weight,
                    "money_weight": profile.money_weight,
                }
                for profile_id, profile in DECISION_PROFILES.items()
            },
            "fare_per_km_scenario": args.fare_per_km,
            "segment_type": args.decision_segment_type,
            "money_value": "priced journey-segment electricity minus known fare revenue plus method-level API/token and compute cost once",
            "unknown_economic_input_policy": "retain known subtotals and block candidate-pool scoring if any hard-feasible candidate lacks comparable monetary inputs",
            "segment_accounting": "ordered road-Link energy details; observed trip fare once per passenger leg; per-Link fare only for an explicit per-km schedule",
            "signed_fare_policy": "finite observed fare amounts may be negative; preserve the signed source amount and audit its meaning without clamping",
            "method_cost_accounting": "API/token and compute charges are stored once per method and added once to each alternative method-route score; they are not assigned to Links or summed across alternative candidates",
            "results_are_sidecar_only": True,
            "source_routes_sha256": _sha256(Path(args.routes_json)),
            "source_map_sha256": _sha256(Path(args.map_json)),
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
