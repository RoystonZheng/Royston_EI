"""Traditional graph-routing baselines for the route-layer experiment.

The module contains the offline R0/R1/R2 baselines only:

* ``full_map_dijkstra`` — R0, reference search on the complete map;
* ``local_dijkstra`` — R1, Dijkstra on the neutral local input package;
* ``local_a_star`` — R2, A* on the same local input package.

Semantic order tasks use a deterministic graph-only endpoint resolver.  It can
use landmark names and geometry already present in the graph, but it does not
read roadside-feature entities or evaluator truth.  This keeps semantic tasks
runnable while leaving road-feature interpretation as the Agent-readable
comparison variable.
"""

import heapq
import json
import math
import time
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import (
    Edge,
    PackageConfig,
    RoadMap,
    RouteCandidate,
    RouteInputPackage,
    RouteMethodResult,
    ValidationResult,
    VehicleOrderTask,
)
from .semantic import resolve_graph_only_semantic_task


TRADITIONAL_METHODS = (
    "full_map_dijkstra",
    "local_dijkstra",
    "local_a_star",
)
DEFAULT_CONGESTION_SPEED_MULTIPLIERS = {0: 1.0, 1: 0.8, 2: 0.6}


def edge_travel_time_sec(
    edge: Edge,
    congestion_speed_multipliers: Optional[Dict[int, float]] = None,
) -> float:
    """Return the deterministic travel-time cost used by all baselines."""

    multipliers = congestion_speed_multipliers or DEFAULT_CONGESTION_SPEED_MULTIPLIERS
    multiplier = multipliers.get(edge.congestion_level, 1.0)
    if edge.base_speed_mps <= 0 or multiplier <= 0:
        raise ValueError("edge speed and congestion multiplier must be positive")
    return edge.length_m / (edge.base_speed_mps * multiplier) + edge.signal_delay_sec


def _node_lookup(road_map: RoadMap) -> Dict[str, object]:
    return {node.node_id: node for node in road_map.nodes}


def _edge_lookup(edges: Sequence[Edge]) -> Dict[str, Edge]:
    return {edge.edge_id: edge for edge in edges}


def _adjacency(
    edges: Sequence[Edge], blocked_edge_ids: Set[str]
) -> Dict[str, List[Edge]]:
    adjacency: Dict[str, List[Edge]] = {}
    for edge in edges:
        if edge.edge_id in blocked_edge_ids:
            continue
        adjacency.setdefault(edge.from_node, []).append(edge)
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda edge: edge.edge_id)
    return adjacency


def directed_path_exists(
    edges: Sequence[Edge],
    source_node_id: str,
    target_node_id: str,
    blocked_edge_ids: Iterable[str] = (),
) -> bool:
    """Check directed reachability without constructing or returning a route."""

    adjacency = _adjacency(edges, set(blocked_edge_ids))
    pending = [source_node_id]
    visited = {source_node_id}
    while pending:
        current_id = pending.pop()
        if current_id == target_node_id:
            return True
        for edge in adjacency.get(current_id, []):
            if edge.to_node not in visited:
                visited.add(edge.to_node)
                pending.append(edge.to_node)
    return False


def route_chain_reachability(
    edges: Sequence[Edge],
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    blocked_edge_ids: Iterable[str] = (),
) -> Dict[str, bool]:
    """Return feasibility of the two directed legs, without a path proposal."""

    blocked = set(blocked_edge_ids)
    start_to_pickup = directed_path_exists(
        edges, start_node_id, pickup_node_id, blocked
    )
    pickup_to_dropoff = directed_path_exists(
        edges, pickup_node_id, dropoff_node_id, blocked
    )
    return {
        "start_to_pickup": start_to_pickup,
        "pickup_to_dropoff": pickup_to_dropoff,
        "chain_reachable": start_to_pickup and pickup_to_dropoff,
    }


def _heuristic_time_sec(node_id: str, target_id: str, nodes: Dict[str, object], max_speed: float) -> float:
    node = nodes[node_id]
    target = nodes[target_id]
    distance_m = (abs(node.x - target.x) + abs(node.y - target.y)) * 100.0
    return distance_m / max_speed


def _search_path(
    nodes: Sequence[object],
    edges: Sequence[Edge],
    source_node_id: str,
    target_node_id: str,
    algorithm: str,
    blocked_edge_ids: Set[str],
    congestion_speed_multipliers: Optional[Dict[int, float]] = None,
) -> Tuple[Optional[RouteCandidate], Dict[str, object]]:
    """Search one directed leg and retain a deterministic replay trace.

    The trace is intentionally independent of any renderer.  It records the
    order in which nodes were expanded plus the costs/frontier size observed
    at each expansion, so later visualizations can replay the exact search
    without rerunning the algorithm.
    """

    trace: Dict[str, object] = {
        "schema_version": "route_search_leg_trace_v1",
        "algorithm": algorithm,
        "source_node_id": source_node_id,
        "target_node_id": target_node_id,
        "status": "not_started",
        "expanded_node_ids": [],
        "expanded_events": [],
        "route_node_sequence": [],
        "route_edge_sequence": [],
        "route_duration_sec": None,
        "route_distance_m": None,
    }
    node_by_id = {node.node_id: node for node in nodes}
    if source_node_id not in node_by_id or target_node_id not in node_by_id:
        trace["status"] = "invalid_endpoint"
        return None, trace
    if algorithm not in ("dijkstra", "a_star"):
        raise ValueError("unknown search algorithm: {0}".format(algorithm))

    trace["status"] = "running"

    edge_by_id = _edge_lookup(edges)
    adjacency = _adjacency(edges, blocked_edge_ids)
    max_speed = max(
        (edge.base_speed_mps for edge in edges if edge.edge_id not in blocked_edge_ids),
        default=1.0,
    )
    distances = {source_node_id: 0.0}
    predecessors: Dict[str, Tuple[str, str]] = {}
    queue = [(0.0, 0.0, source_node_id)]
    while queue:
        priority, current_distance, current_id = heapq.heappop(queue)
        if current_distance != distances.get(current_id):
            continue
        expanded_node_ids = trace["expanded_node_ids"]
        expanded_events = trace["expanded_events"]
        expanded_node_ids.append(current_id)
        expanded_events.append(
            {
                "step": len(expanded_node_ids),
                "node_id": current_id,
                "g_cost_sec": current_distance,
                "priority_sec": priority,
                "frontier_size": len(queue),
            }
        )
        if current_id == target_node_id:
            break
        for edge in adjacency.get(current_id, []):
            edge_cost = edge_travel_time_sec(
                edge, congestion_speed_multipliers=congestion_speed_multipliers
            )
            candidate_distance = current_distance + edge_cost
            if candidate_distance >= distances.get(edge.to_node, float("inf")):
                continue
            distances[edge.to_node] = candidate_distance
            predecessors[edge.to_node] = (current_id, edge.edge_id)
            heuristic = (
                _heuristic_time_sec(
                    edge.to_node, target_node_id, node_by_id, max_speed
                )
                if algorithm == "a_star"
                else 0.0
            )
            heapq.heappush(
                queue,
                (candidate_distance + heuristic, candidate_distance, edge.to_node),
            )

    if target_node_id not in distances:
        trace["status"] = "unreachable"
        return None, trace

    node_sequence = [target_node_id]
    edge_sequence: List[str] = []
    current_id = target_node_id
    while current_id != source_node_id:
        previous_id, edge_id = predecessors[current_id]
        node_sequence.append(previous_id)
        edge_sequence.append(edge_id)
        current_id = previous_id
    node_sequence.reverse()
    edge_sequence.reverse()

    route_distance = sum(edge_by_id[edge_id].length_m for edge_id in edge_sequence)
    route_duration = distances[target_node_id]
    route = RouteCandidate(
            route_id="path-{0}-{1}".format(source_node_id, target_node_id),
            node_sequence=node_sequence,
            edge_sequence=edge_sequence,
            route_reachability=True,
            route_distance_m=route_distance,
            route_duration_sec=route_duration,
        )
    trace["status"] = "completed"
    trace["route_node_sequence"] = list(node_sequence)
    trace["route_edge_sequence"] = list(edge_sequence)
    trace["route_duration_sec"] = route_duration
    trace["route_distance_m"] = route_distance
    return route, trace


def _not_started_search_trace(method_name: str, reason: str) -> Dict[str, object]:
    algorithm = "a_star" if method_name == "local_a_star" else "dijkstra"
    return {
        "schema_version": "route_search_trace_v1",
        "method_name": method_name,
        "algorithm": algorithm,
        "status": "not_started",
        "reason": reason,
        "legs": [],
        "expanded_nodes": 0,
        "route_node_sequence": [],
        "route_edge_sequence": [],
    }


def _combine_search_trace(
    method_name: str,
    algorithm: str,
    first_trace: Dict[str, object],
    second_trace: Dict[str, object],
    route: Optional[RouteCandidate] = None,
    status: str = "completed",
) -> Dict[str, object]:
    expanded_nodes = len(first_trace.get("expanded_node_ids", [])) + len(
        second_trace.get("expanded_node_ids", [])
    )
    return {
        "schema_version": "route_search_trace_v1",
        "method_name": method_name,
        "algorithm": algorithm,
        "status": status,
        "legs": [first_trace, second_trace],
        "expanded_nodes": expanded_nodes,
        "route_node_sequence": list(route.node_sequence) if route else [],
        "route_edge_sequence": list(route.edge_sequence) if route else [],
    }


def _chain_reachable(
    nodes: Sequence[object],
    edges: Sequence[Edge],
    task: VehicleOrderTask,
    blocked_edge_ids: Set[str],
) -> bool:
    """Check directed reachability for start → pickup → dropoff."""

    return route_chain_reachability(
        edges,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        blocked_edge_ids,
    )["chain_reachable"]


def _region_for_margin(road_map: RoadMap, task: VehicleOrderTask, margin: int):
    lookup = _node_lookup(road_map)
    points = [
        lookup[task.start_node_id],
        lookup[task.pickup_node_id],
        lookup[task.dropoff_node_id],
    ]
    return {
        "min_x": max(road_map.boundary["min_x"], min(point.x for point in points) - margin),
        "max_x": min(road_map.boundary["max_x"], max(point.x for point in points) + margin),
        "min_y": max(road_map.boundary["min_y"], min(point.y for point in points) - margin),
        "max_y": min(road_map.boundary["max_y"], max(point.y for point in points) + margin),
    }


def _inside_region(node: object, region: Dict[str, int]) -> bool:
    return (
        region["min_x"] <= node.x <= region["max_x"]
        and region["min_y"] <= node.y <= region["max_y"]
    )


def build_route_input_package(
    road_map: RoadMap,
    task: VehicleOrderTask,
    config: Optional[PackageConfig] = None,
    blocked_edge_ids: Iterable[str] = (),
) -> RouteInputPackage:
    """Extract the neutral local package using the fixed expansion rule."""

    if task.pickup_node_id is None or task.dropoff_node_id is None:
        raise ValueError("semantic_endpoint_unresolved")
    config = config or PackageConfig()
    blocked = set(blocked_edge_ids)
    node_by_id = _node_lookup(road_map)
    required_ids = {
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
    }
    if not required_ids.issubset(node_by_id):
        raise ValueError("task_endpoint_missing_from_map")

    selected_nodes: List[object] = []
    selected_edges: List[Edge] = []
    selected_region: Dict[str, int] = road_map.boundary
    expansion_count = 0
    for expansion_count in range(config.max_expansions + 1):
        margin = config.initial_margin + expansion_count * config.expansion_step
        selected_region = _region_for_margin(road_map, task, margin)
        selected_nodes = [node for node in road_map.nodes if _inside_region(node, selected_region)]
        selected_ids = {node.node_id for node in selected_nodes}
        selected_edges = [
            edge
            for edge in road_map.edges
            if edge.from_node in selected_ids
            and edge.to_node in selected_ids
            and edge.edge_id not in blocked
        ]
        if _chain_reachable(selected_nodes, selected_edges, task, set()):
            break

    feature_ids = {node.node_id for node in selected_nodes}
    feature_subset = [
        feature
        for feature in road_map.roadside_features
        if feature.node_id in feature_ids
        or (feature.paired_node_id and feature.paired_node_id in feature_ids)
    ]
    payload = {
        "map_id": road_map.map_id,
        "map_version": road_map.version,
        "road_state_version": road_map.road_state_version,
        "start_node_id": task.start_node_id,
        "pickup_node_id": task.pickup_node_id,
        "dropoff_node_id": task.dropoff_node_id,
        "nodes": [node.to_dict() for node in selected_nodes],
        "edges": [edge.to_dict() for edge in selected_edges],
        "roadside_features": [feature.to_dict() for feature in feature_subset],
    }
    serialized_token_estimate = int(
        math.ceil(
            len(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            / 4.0
            * config.tokens_per_four_chars
        )
    )
    return RouteInputPackage(
        package_id="package-{0}-{1}".format(road_map.map_id, task.task_id),
        map_id=road_map.map_id,
        map_version=road_map.version,
        road_state_version=road_map.road_state_version,
        random_seed=int(road_map.map_id.rsplit("-", 1)[-1]),
        start_node_id=task.start_node_id,
        pickup_node_id=task.pickup_node_id,
        dropoff_node_id=task.dropoff_node_id,
        task_type=task.task_type,
        pickup_text=task.pickup_text,
        dropoff_text=task.dropoff_text,
        nodes=selected_nodes,
        edges=selected_edges,
        expansion_count=expansion_count,
        serialized_token_estimate=serialized_token_estimate,
        boundary=road_map.boundary,
        blocked_edge_ids=sorted(blocked),
        roadside_features=feature_subset,
    )


def _failed_result(
    task: VehicleOrderTask,
    method_name: str,
    reason: str,
    errors: Optional[List[str]] = None,
    validation: Optional[ValidationResult] = None,
    metadata: Optional[Dict[str, object]] = None,
) -> RouteMethodResult:
    result_metadata = dict(metadata or {})
    result_metadata.setdefault(
        "search_trace", _not_started_search_trace(method_name, reason)
    )
    return RouteMethodResult(
        task_id=task.task_id,
        method_name=method_name,
        status="failed",
        failure_reason=reason,
        validation=validation,
        metadata=result_metadata,
        raw_output={"errors": errors or [reason]},
    )


def run_traditional_route(
    road_map: RoadMap,
    task: VehicleOrderTask,
    method_name: str,
    package_config: Optional[PackageConfig] = None,
    blocked_edge_ids: Iterable[str] = (),
) -> RouteMethodResult:
    """Run one R0/R1/R2 baseline for one vehicle–order task."""

    if method_name not in TRADITIONAL_METHODS:
        raise ValueError("unknown traditional method: {0}".format(method_name))
    started = time.perf_counter()
    blocked = set(blocked_edge_ids)
    semantic_resolution = None
    route_task = task
    if task.pickup_node_id is None or task.dropoff_node_id is None:
        pickup_node_id, dropoff_node_id, semantic_resolution = (
            resolve_graph_only_semantic_task(road_map, task)
        )
        if not semantic_resolution.get("resolved"):
            return _failed_result(
                task,
                method_name,
                "semantic_endpoint_unresolved",
                errors=[semantic_resolution.get("reason", "semantic_candidate_missing")],
                validation=ValidationResult(
                    valid=False,
                    status="semantic_endpoint_unresolved",
                    errors=[semantic_resolution.get("reason", "semantic_candidate_missing")],
                ),
                metadata={"semantic_resolution": semantic_resolution},
            )
        route_task = replace(
            task,
            pickup_node_id=pickup_node_id,
            dropoff_node_id=dropoff_node_id,
        )

    node_by_id = _node_lookup(road_map)
    missing = [
        node_id
        for node_id in (
            route_task.start_node_id,
            route_task.pickup_node_id,
            route_task.dropoff_node_id,
        )
        if node_id not in node_by_id
    ]
    if missing:
        return _failed_result(
            task,
            method_name,
            "task_endpoint_missing_from_map",
            errors=["missing:" + node_id for node_id in missing],
        )
    pickup_legal = bool(node_by_id[route_task.pickup_node_id].legal_pickup)
    dropoff_legal = bool(node_by_id[route_task.dropoff_node_id].legal_dropoff)
    if not pickup_legal:
        return _failed_result(
            task,
            method_name,
            "illegal_pickup_endpoint",
            validation=ValidationResult(
                valid=False,
                status="illegal_endpoint",
                errors=["pickup_not_legal"],
                legal_pickup=False,
                legal_dropoff=dropoff_legal,
            ),
        )
    if not dropoff_legal:
        return _failed_result(
            task,
            method_name,
            "illegal_dropoff_endpoint",
            validation=ValidationResult(
                valid=False,
                status="illegal_endpoint",
                errors=["dropoff_not_legal"],
                legal_pickup=True,
                legal_dropoff=False,
            ),
        )

    package = None
    if method_name == "full_map_dijkstra":
        nodes = road_map.nodes
        edges = road_map.edges
        algorithm = "dijkstra"
    else:
        try:
            package = build_route_input_package(
                road_map,
                route_task,
                config=package_config,
                blocked_edge_ids=blocked,
            )
        except ValueError as error:
            return _failed_result(
                task,
                method_name,
                str(error),
                metadata={"compute_time_ms": (time.perf_counter() - started) * 1000},
            )
        nodes = package.nodes
        edges = package.edges
        algorithm = "a_star" if method_name == "local_a_star" else "dijkstra"

    first_leg, first_trace = _search_path(
        nodes,
        edges,
        route_task.start_node_id,
        route_task.pickup_node_id,
        algorithm,
        blocked,
    )
    second_leg, second_trace = _search_path(
        nodes,
        edges,
        route_task.pickup_node_id,
        route_task.dropoff_node_id,
        algorithm,
        blocked,
    )
    search_trace = _combine_search_trace(
        method_name,
        algorithm,
        first_trace,
        second_trace,
        status="completed"
        if first_leg is not None and second_leg is not None
        else "unreachable",
    )
    expanded_nodes = search_trace["expanded_nodes"]
    elapsed_ms = (time.perf_counter() - started) * 1000
    if first_leg is None or second_leg is None:
        return _failed_result(
            task,
            method_name,
            "unreachable",
            validation=ValidationResult(
                valid=False,
                status="unreachable",
                errors=["no_directed_path_for_start_pickup_dropoff_chain"],
                legal_pickup=True,
                legal_dropoff=True,
            ),
            metadata={
                "expanded_nodes": expanded_nodes,
                "search_trace": search_trace,
                "compute_time_ms": elapsed_ms,
                "package_expansion_count": (
                    package.expansion_count if package is not None else 0
                ),
                **(
                    {"semantic_resolution": semantic_resolution}
                    if semantic_resolution is not None
                    else {}
                ),
            },
        )

    route = RouteCandidate(
        route_id="{0}-{1}".format(method_name, task.task_id),
        node_sequence=first_leg.node_sequence + second_leg.node_sequence[1:],
        edge_sequence=first_leg.edge_sequence + second_leg.edge_sequence,
        route_source=method_name,
        route_reachability=True,
        route_distance_m=(first_leg.route_distance_m or 0.0)
        + (second_leg.route_distance_m or 0.0),
        route_duration_sec=(first_leg.route_duration_sec or 0.0)
        + (second_leg.route_duration_sec or 0.0),
        validation_status="valid",
    )
    search_trace = _combine_search_trace(
        method_name,
        algorithm,
        first_trace,
        second_trace,
        route=route,
        status="completed",
    )
    return RouteMethodResult(
        task_id=task.task_id,
        method_name=method_name,
        status="completed",
        routes=[route],
        best_route_id=route.route_id,
        valid_route_count=1,
        validation=ValidationResult(
            valid=True,
            status="valid",
            pickup_exact_match=task.task_type == "exact_node",
            dropoff_exact_match=task.task_type == "exact_node",
            legal_pickup=True,
            legal_dropoff=True,
            direction_valid=True,
        ),
        metadata={
            "expanded_nodes": expanded_nodes,
            "search_trace": search_trace,
            "compute_time_ms": elapsed_ms,
            "package_expansion_count": (
                package.expansion_count if package is not None else 0
            ),
            "package_node_count": len(package.nodes) if package is not None else len(nodes),
            "package_edge_count": len(package.edges) if package is not None else len(edges),
            **(
                {"semantic_resolution": semantic_resolution}
                if semantic_resolution is not None
                else {}
            ),
        },
    )


def run_traditional_methods(
    road_map: RoadMap,
    tasks: Sequence[VehicleOrderTask],
    method_names: Sequence[str] = TRADITIONAL_METHODS,
    blocked_edge_ids_by_task: Optional[Dict[str, Iterable[str]]] = None,
) -> Dict[str, List[RouteMethodResult]]:
    """Run a fixed set of traditional methods over a shared task batch."""

    blocked_by_task = blocked_edge_ids_by_task or {}
    return {
        task.task_id: [
            run_traditional_route(
                road_map,
                task,
                method_name,
                blocked_edge_ids=blocked_by_task.get(task.task_id, ()),
            )
            for method_name in method_names
        ]
        for task in tasks
    }
