"""Plan-named compatibility interface for the shared route evaluator."""

from .costs import evaluate_route_candidate, price_costs


def evaluate_route(road_map, route, config=None):
    """Evaluate one route using the common traffic and energy model."""

    return evaluate_route_candidate(road_map, route, config=config)


__all__ = ["evaluate_route", "price_costs"]
