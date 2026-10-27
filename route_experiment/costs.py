"""Shared route evaluation and resource-cost calculations.

The evaluator is deliberately deterministic and offline.  It recomputes route
time and energy from the road edges so every planner is charged with the same
road-state model.  Resource quantities (tokens, milliseconds and kWh) remain
separate from monetary amounts; only monetary components are summed.
"""

from typing import Dict, Optional, Tuple

from .energy_model import EnergyModelConfig, evaluate_route_edges

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
    signal_delay = 0.0
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
        base_time += edge_base_time
        signal_delay += edge.signal_delay_sec
        distance_m += edge.length_m
    profile = config.energy_profile
    if isinstance(profile, dict):
        profile = EnergyModelConfig(**profile)
    energy = evaluate_route_edges(
        edges,
        config.congestion_speed_multipliers,
        profile,
        payload_mass_kg=route.payload_mass_kg,
    )
    congestion_delay = max(0.0, energy.drive_time_sec - base_time)
    signal_idle_energy = (
        profile.auxiliary_power_w * signal_delay / 3_600_000.0
    )
    return EvaluationResult(
        base_travel_time_sec=base_time,
        congestion_delay_sec=congestion_delay,
        signal_delay_sec=signal_delay,
        total_travel_time_sec=energy.drive_time_sec + signal_delay,
        energy_moving_kwh=energy.propulsion_draw_kwh,
        energy_idle_signal_kwh=signal_idle_energy,
        # Kept as a zero-valued compatibility field: congestion is represented
        # by speed changes and stop/start motion, never by a flat kWh surcharge.
        energy_congestion_kwh=0.0,
        energy_rolling_wheel_kwh=energy.rolling_wheel_kwh,
        energy_aerodynamic_wheel_kwh=energy.aerodynamic_wheel_kwh,
        energy_grade_wheel_kwh=energy.grade_wheel_kwh,
        energy_acceleration_wheel_kwh=energy.acceleration_wheel_kwh,
        energy_regenerated_kwh=energy.regenerated_kwh,
        energy_auxiliary_kwh=energy.auxiliary_kwh,
        energy_total_kwh=energy.net_kwh,
        route_distance_m=distance_m,
        simulation_estimate=True,
        energy_model_version=energy.model_version,
        energy_trace_fidelity=energy.trace_fidelity,
        energy_profile_id=energy.profile_id,
        energy_profile_sha256=energy.profile_sha256,
        energy_grade_missing=energy.missing_grade_steps > 0,
        energy_segments=energy.edge_breakdown,
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
    billable_energy_kwh = max(0.0, float(resources.energy_kwh))
    electricity_cost = billable_energy_kwh * prices.electricity_price_per_kwh
    return MonetaryCost(
        api_cost_amount=api_cost,
        compute_cost_amount=compute_cost,
        electricity_cost_amount=electricity_cost,
        billable_energy_kwh=billable_energy_kwh,
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
    method_result.metadata["energy_profile_id"] = evaluation.energy_profile_id
    method_result.metadata["energy_profile_sha256"] = evaluation.energy_profile_sha256
    method_result.metadata["energy_trace_fidelity"] = evaluation.energy_trace_fidelity
    method_result.metadata["energy_grade_missing"] = evaluation.energy_grade_missing
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
