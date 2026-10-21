"""Deterministic route and ETA oracle used by the dispatch simulation."""

import heapq
from typing import Dict, List, Optional, Tuple

from route_experiment.models import Edge, RoadMap
from route_experiment.routing import edge_travel_time_sec

from .models import RouteLeg


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

