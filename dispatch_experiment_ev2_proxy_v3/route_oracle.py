"""Deterministic route and ETA oracle used by the dispatch simulation."""

import heapq
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from route_experiment.energy_model import EnergyModelConfig, evaluate_route_edges
from route_experiment.models import Edge, RoadMap
from route_experiment.routing import edge_travel_time_sec
from route_experiment.synthetic_segment_energy import evaluate_synthetic_start_stop_segment

from .models import RouteLeg


@dataclass(frozen=True)
class RouteEnergySummary:
    net_kwh: float
    propulsion_draw_kwh: float
    regenerated_kwh: float
    auxiliary_kwh: float
    trace_fidelity: str = "estimated_from_route_edge_conditions"


class GraphRouteOracle:
    """Shortest-path oracle with an in-memory source/target cache.

    The oracle is deliberately a route service, not a dispatch policy.  It
    exposes only route facts to the agent and records no future orders.
    """

    def __init__(self, road_map: RoadMap):
        self.road_map = road_map
        self._nodes = {node.node_id: node for node in road_map.nodes}
        self._edges = {edge.edge_id: edge for edge in road_map.edges}
        self._adjacency: Dict[str, List[Edge]] = {}
        for edge in road_map.edges:
            self._adjacency.setdefault(edge.from_node, []).append(edge)
        for edges in self._adjacency.values():
            edges.sort(key=lambda edge: edge.edge_id)
        self._cache: Dict[Tuple[str, str], RouteLeg] = {}
        self._energy_cache = OrderedDict()

    def route_energy_summary(
        self,
        edge_sequence: List[str],
        congestion_speed_multipliers: Dict[int, float],
        config: EnergyModelConfig,
        *,
        payload_mass_kg: Optional[float] = None,
    ) -> RouteEnergySummary:
        """Return candidate energy components, reusing exact route evaluations.

        The cache belongs to this oracle, so its fixed road map is part of the
        cache identity. Every setting that affects the evaluation is in the
        key. Cached values contain only the immutable totals consumed by
        dispatch candidates; callers cannot mutate a shared EnergyResult.
        """

        payload = config.passenger_load_kg if payload_mass_kg is None else float(payload_mass_kg)
        if payload < 0:
            raise ValueError("payload_mass_kg_must_be_nonnegative")
        multipliers_key = tuple(
            sorted((int(level), float(multiplier)) for level, multiplier in congestion_speed_multipliers.items())
        )
        key = (tuple(edge_sequence), multipliers_key, config.fingerprint(), payload)
        cached = self._energy_cache.get(key)
        if cached is not None:
            self._energy_cache.move_to_end(key)
            return cached

        edges = [self._edges[edge_id] for edge_id in edge_sequence if edge_id in self._edges]
        result = evaluate_route_edges(
            edges,
            congestion_speed_multipliers,
            config,
            payload_mass_kg=payload,
        )
        summary = RouteEnergySummary(
            net_kwh=result.net_kwh,
            propulsion_draw_kwh=result.propulsion_draw_kwh,
            regenerated_kwh=result.regenerated_kwh,
            auxiliary_kwh=result.auxiliary_kwh,
            trace_fidelity=result.trace_fidelity,
        )
        self._energy_cache[key] = summary
        if len(self._energy_cache) > 8192:
            self._energy_cache.popitem(last=False)
        return summary

    def route_leg_energy_summary(
        self,
        leg: RouteLeg,
        congestion_speed_multipliers: Dict[int, float],
        config: EnergyModelConfig,
        *,
        payload_mass_kg: Optional[float] = None,
    ) -> RouteEnergySummary:
        """Evaluate an explicit route leg without silently zeroing missing paths.

        Road-edge legs use the ordinary EV v2 route evaluator.  The only
        positive-distance edge-less leg accepted by this version is the
        versioned NYC zone-centroid pickup proxy; all other such legs fail
        closed so an absent path cannot become zero energy.
        """

        if leg.edge_sequence:
            missing = [edge_id for edge_id in leg.edge_sequence if edge_id not in self._edges]
            if missing:
                raise ValueError("route_leg_references_missing_energy_edges:" + ",".join(missing[:5]))
            return self.route_energy_summary(
                leg.edge_sequence,
                congestion_speed_multipliers,
                config,
                payload_mass_kg=payload_mass_kg,
            )

        distance = float(leg.distance_m)
        duration = float(leg.duration_sec)
        if distance == 0.0 and duration == 0.0:
            return RouteEnergySummary(
                net_kwh=0.0,
                propulsion_draw_kwh=0.0,
                regenerated_kwh=0.0,
                auxiliary_kwh=0.0,
                trace_fidelity="zero_distance_noop_or_unreachable_leg",
            )
        if leg.reason != "synthetic_zone_centroid_pickup_proxy":
            raise ValueError(
                "positive_route_leg_missing_edge_sequence:" + str(leg.reason or "reason_unavailable")
            )

        payload = config.passenger_load_kg if payload_mass_kg is None else float(payload_mass_kg)
        synthetic_key = (
            "synthetic_centroid_start_stop_ev2_v1",
            round(distance, 9),
            round(duration, 9),
            config.fingerprint(),
            payload,
        )
        cached = self._energy_cache.get(synthetic_key)
        if cached is not None:
            self._energy_cache.move_to_end(synthetic_key)
            return cached

        result = evaluate_synthetic_start_stop_segment(
            distance,
            duration,
            config,
            payload_mass_kg=payload,
        )
        summary = RouteEnergySummary(
            net_kwh=result.net_kwh,
            propulsion_draw_kwh=result.propulsion_draw_kwh,
            regenerated_kwh=result.regenerated_kwh,
            auxiliary_kwh=result.auxiliary_kwh,
            trace_fidelity=result.trace_fidelity,
        )
        self._energy_cache[synthetic_key] = summary
        if len(self._energy_cache) > 8192:
            self._energy_cache.popitem(last=False)
        return summary

    def route(self, source_node_id: str, target_node_id: str) -> RouteLeg:
        key = (source_node_id, target_node_id)
        if key in self._cache:
            return self._cache[key]
        if source_node_id not in self._nodes or target_node_id not in self._nodes:
            leg = RouteLeg(reachable=False, reason="endpoint_missing")
            self._cache[key] = leg
            return leg
        if source_node_id == target_node_id:
            leg = RouteLeg(
                reachable=True,
                node_sequence=[source_node_id],
                edge_sequence=[],
            )
            self._cache[key] = leg
            return leg

        distances = {source_node_id: 0.0}
        predecessor: Dict[str, Tuple[str, str]] = {}
        queue = [(0.0, source_node_id)]
        while queue:
            distance, current = heapq.heappop(queue)
            if distance != distances.get(current):
                continue
            if current == target_node_id:
                break
            for edge in self._adjacency.get(current, []):
                candidate = distance + edge_travel_time_sec(edge)
                if candidate >= distances.get(edge.to_node, float("inf")):
                    continue
                distances[edge.to_node] = candidate
                predecessor[edge.to_node] = (current, edge.edge_id)
                heapq.heappush(queue, (candidate, edge.to_node))

        if target_node_id not in distances:
            leg = RouteLeg(reachable=False, reason="unreachable")
            self._cache[key] = leg
            return leg

        node_sequence = [target_node_id]
        edge_sequence: List[str] = []
        current = target_node_id
        while current != source_node_id:
            previous, edge_id = predecessor[current]
            node_sequence.append(previous)
            edge_sequence.append(edge_id)
            current = previous
        node_sequence.reverse()
        edge_sequence.reverse()
        distance_m = sum(self._edges[edge_id].length_m for edge_id in edge_sequence)
        leg = RouteLeg(
            reachable=True,
            distance_m=distance_m,
            duration_sec=distances[target_node_id],
            node_sequence=node_sequence,
            edge_sequence=edge_sequence,
        )
        self._cache[key] = leg
        return leg

    def cache_size(self) -> int:
        return len(self._cache)

