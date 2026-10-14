"""Shared route evaluation and resource-cost calculations.

The evaluator is deliberately deterministic and offline.  It recomputes route
time and energy from the road edges so every planner is charged with the same
road-state model.  Resource quantities (tokens, milliseconds and kWh) remain
separate from monetary amounts; only monetary components are summed.
"""

from typing import Dict, Optional, Tuple

from .models import (
    Edge,
    EvaluationConfig,
    EvaluationResult,
    MonetaryCost,
    PriceConfig,
    RoadMap,
    RouteCandidate,
    RouteMethodResult,
    ResourceUsage,
)


def _edge_lookup(road_map: RoadMap) -> Dict[str, Edge]:
    return {edge.edge_id: edge for edge in road_map.edges}


def evaluate_route_candidate(
    road_map: RoadMap,
    route: RouteCandidate,
    config: Optional[EvaluationConfig] = None,
) -> EvaluationResult:
    """Recompute traffic time and simulated energy for one route.

    ``route_duration_sec`` and ``route_distance_m`` are intentionally not
    trusted here.  They are planner output fields; the common evaluator uses
    the shared map edges and ``EvaluationConfig`` instead.
    """

    config = config or EvaluationConfig()
    if not route.edge_sequence:
        raise ValueError("route_has_no_edges")

    edge_by_id = _edge_lookup(road_map)
    missing = [edge_id for edge_id in route.edge_sequence if edge_id not in edge_by_id]
    if missing:
        raise ValueError("route_edge_missing:" + ",".join(missing))

    edges = [edge_by_id[edge_id] for edge_id in route.edge_sequence]
    if route.node_sequence:
        if len(route.node_sequence) != len(edges) + 1:
            raise ValueError("route_node_edge_length_mismatch")
        for index, edge in enumerate(edges):
            if (
                edge.from_node != route.node_sequence[index]
                or edge.to_node != route.node_sequence[index + 1]
            ):
                raise ValueError("route_edge_node_discontinuity")
    else:
        # A route from an external planner may contain only edge IDs.  The
        # common evaluator can still validate and score its edge chain.
        for previous, current in zip(edges, edges[1:]):
            if previous.to_node != current.from_node:
                raise ValueError("route_edge_discontinuity")

    base_time = 0.0
    congestion_delay = 0.0
    signal_delay = 0.0
    moving_energy = 0.0
    idle_signal_energy = 0.0
    congestion_energy = 0.0
    distance_m = 0.0

    for edge in edges:
        if edge.base_speed_mps <= 0:
            raise ValueError("edge_speed_must_be_positive:" + edge.edge_id)
        multiplier = config.congestion_speed_multipliers.get(edge.congestion_level)
        if multiplier is None:
            raise ValueError("congestion_level_missing:" + str(edge.congestion_level))
        if multiplier <= 0:
            raise ValueError("congestion_multiplier_must_be_positive")

        edge_base_time = edge.length_m / edge.base_speed_mps
        effective_time = edge.length_m / (edge.base_speed_mps * multiplier)
        base_time += edge_base_time
        congestion_delay += effective_time - edge_base_time
        signal_delay += edge.signal_delay_sec
        distance_m += edge.length_m

        distance_km = edge.length_m / 1000.0
        moving_energy += distance_km * config.moving_kwh_per_km
        idle_signal_energy += edge.signal_delay_sec * config.signal_idle_kwh_per_sec
        congestion_energy += (
            distance_km
            * edge.congestion_level
            * config.congestion_extra_kwh_per_km_level
        )

    total_energy = moving_energy + idle_signal_energy + congestion_energy
    return EvaluationResult(
        base_travel_time_sec=base_time,
        congestion_delay_sec=congestion_delay,
        signal_delay_sec=signal_delay,
        total_travel_time_sec=base_time + congestion_delay + signal_delay,
        energy_moving_kwh=moving_energy,
        energy_idle_signal_kwh=idle_signal_energy,
        energy_congestion_kwh=congestion_energy,
        energy_total_kwh=total_energy,
        route_distance_m=distance_m,
        simulation_estimate=True,
        energy_model_version=config.energy_model_version,
    )


def _nonnegative(value: float, field_name: str) -> float:
    number = float(value)
    if number < 0:
        raise ValueError(field_name + "_must_be_nonnegative")
    return number


def calculate_resource_cost(
    method_result: RouteMethodResult,
    evaluation: EvaluationResult,
    price_config: Optional[PriceConfig] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_calls: int = 0,
    llm_latency_ms: float = 0.0,
    compute_time_ms: Optional[float] = None,
) -> Tuple[ResourceUsage, MonetaryCost]:
    """Convert resource quantities to monetary components for one result."""

    price_config = price_config or PriceConfig()
    if compute_time_ms is None:
        compute_time_ms = method_result.metadata.get("compute_time_ms", 0.0)

    input_tokens = int(_nonnegative(input_tokens, "input_tokens"))
    output_tokens = int(_nonnegative(output_tokens, "output_tokens"))
    llm_calls = int(_nonnegative(llm_calls, "llm_calls"))
    llm_latency_ms = _nonnegative(llm_latency_ms, "llm_latency_ms")
    compute_time_ms = _nonnegative(compute_time_ms, "compute_time_ms")

    for field_name, value in (
        ("api_price_per_token", price_config.api_price_per_token),
        ("compute_price_per_ms", price_config.compute_price_per_ms),
        ("electricity_price_per_kwh", price_config.electricity_price_per_kwh),
    ):
        _nonnegative(value, field_name)

    resources = ResourceUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        llm_calls=llm_calls,
        llm_latency_ms=llm_latency_ms,
        compute_time_ms=compute_time_ms,
        energy_kwh=evaluation.energy_total_kwh,
    )
    return resources, price_costs(resources, price_config)


def price_costs(resources: ResourceUsage, prices: PriceConfig) -> MonetaryCost:
    """Convert already recorded resource quantities into currency amounts."""

    for field_name, value in (
        ("api_price_per_token", prices.api_price_per_token),
        ("compute_price_per_ms", prices.compute_price_per_ms),
        ("electricity_price_per_kwh", prices.electricity_price_per_kwh),
    ):
        _nonnegative(value, field_name)
    api_cost = (resources.input_tokens + resources.output_tokens) * prices.api_price_per_token
    compute_cost = resources.compute_time_ms * prices.compute_price_per_ms
    electricity_cost = resources.energy_kwh * prices.electricity_price_per_kwh
    return MonetaryCost(
        api_cost_amount=api_cost,
        compute_cost_amount=compute_cost,
        electricity_cost_amount=electricity_cost,
        total_monetary_cost=api_cost + compute_cost + electricity_cost,
        currency=prices.currency,
    )


def _select_route(result: RouteMethodResult) -> Optional[RouteCandidate]:
    if result.best_route_id:
        for route in result.routes:
            if route.route_id == result.best_route_id:
                return route
    for route in result.routes:
        if route.route_reachability and route.validation_status in ("valid", ""):
            return route
    return None


def enrich_route_method_result(
    road_map: RoadMap,
    method_result: RouteMethodResult,
    evaluation_config: Optional[EvaluationConfig] = None,
    price_config: Optional[PriceConfig] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_calls: int = 0,
    llm_latency_ms: float = 0.0,
) -> RouteMethodResult:
    """Attach shared evaluation and costs to a route result in place."""

    evaluation_config = evaluation_config or EvaluationConfig()
    price_config = price_config or PriceConfig()
    route = _select_route(method_result) if method_result.status == "completed" else None
    if route is None:
        method_result.metadata["cost_status"] = "not_evaluated"
        return method_result

    evaluation = evaluate_route_candidate(road_map, route, evaluation_config)
    resources, money = calculate_resource_cost(
        method_result,
        evaluation,
        price_config=price_config,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        llm_calls=llm_calls,
        llm_latency_ms=llm_latency_ms,
    )
    method_result.evaluation = evaluation
    method_result.resources = resources
    method_result.monetary_cost = money
    method_result.metadata["cost_status"] = "evaluated"
    method_result.metadata["energy_model_version"] = evaluation.energy_model_version
    method_result.metadata["pricing_version"] = price_config.pricing_version
    method_result.metadata["billing_period"] = price_config.billing_period
    method_result.metadata["pricing_status"] = price_config.pricing_status
    method_result.metadata["price_source"] = price_config.price_source
    method_result.metadata["price_source_url"] = price_config.price_source_url
    method_result.metadata["reference_station"] = price_config.reference_station
    method_result.metadata["reference_rate_type"] = price_config.reference_rate_type
    method_result.metadata["observed_at"] = price_config.observed_at
    method_result.metadata["price_basis"] = price_config.price_basis
    return method_result


# Compatibility aliases for callers that use the terminology from the plan.
evaluate_route_cost = evaluate_route_candidate
calculate_cost = calculate_resource_cost
evaluate_route = evaluate_route_candidate
