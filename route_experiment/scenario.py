"""Reproducible showcase and formal coverage scenarios."""

import heapq
import random
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .mapgen import generate_map
from .models import (
    CoverageReport,
    FormalScenarioSet,
    MapConfig,
    RoadMap,
    RoadsideFeature,
    RouteFeatureScenario,
    ScenarioRecord,
    VehicleOrderTask,
)


def _node_xy(node_id: str) -> Tuple[int, int]:
    _, x, y = node_id.split("_")
    return int(x), int(y)


def _farthest_node(road_map: RoadMap, source_ids: List[str], excluded: set) -> Optional[str]:
    source_points = [_node_xy(node_id) for node_id in source_ids]
    candidates = [node for node in road_map.nodes if node.node_id not in excluded]
    if not candidates or not source_points:
        return None
    return max(
        candidates,
        key=lambda node: min(
            abs(node.x - x) + abs(node.y - y) for x, y in source_points
        ),
    ).node_id


def _nearest_landmark(road_map: RoadMap, source_id: str) -> Optional[str]:
    source_x, source_y = _node_xy(source_id)
    landmarks = [node for node in road_map.nodes if node.landmark_name]
    if not landmarks:
        return None
    return min(
        landmarks,
        key=lambda node: abs(node.x - source_x) + abs(node.y - source_y),
    ).node_id


def _side_label(side: str) -> str:
    return {
        "north_side": "道路北侧",
        "south_side": "道路南侧",
        "east_side": "道路东侧",
        "west_side": "道路西侧",
    }.get(side, "道路一侧")


def _orientation_label(orientation: str) -> str:
    return {
        "toward_north": "北方",
        "toward_south": "南方",
        "toward_east": "东方",
        "toward_west": "西方",
    }.get(orientation, "指定方向")


def _scenario_for_feature(
    road_map: RoadMap,
    feature: RoadsideFeature,
    scenario_id: str,
    title: str,
    scenario_type: str,
    description: str,
    semantic_query: str,
    expected_route_effect: str,
) -> RouteFeatureScenario:
    paired_node_id = feature.paired_node_id or feature.node_id
    excluded = {feature.node_id, paired_node_id}
    start_node_id = _farthest_node(road_map, [feature.node_id, paired_node_id], excluded)
    if start_node_id is None:
        start_node_id = feature.node_id
    dropoff_node_id = _nearest_landmark(road_map, start_node_id)
    if dropoff_node_id in excluded:
        dropoff_node_id = _farthest_node(road_map, [start_node_id], excluded)
    return RouteFeatureScenario(
        scenario_id=scenario_id,
        title=title,
        scenario_type=scenario_type,
        description=description,
        start_node_id=start_node_id,
        pickup_candidate_node_ids=[feature.node_id, paired_node_id],
        baseline_selected_node_id=paired_node_id,
        agent_selected_node_id=feature.node_id,
        dropoff_node_id=dropoff_node_id,
        feature_ids=[feature.feature_id],
        semantic_query=semantic_query,
        expected_baseline_behavior=(
            "仅读取节点和道路边，忽略路边语义，可能把模糊定位映射到另一侧候选点，"
            "再按图搜索产生绕行。"
        ),
        expected_agent_behavior=(
            "读取 {0} 的名称、侧别、朝向和对应节点关系，选择更符合原话的近侧合法候选点，"
            "再生成路线。".format(feature.feature_type)
        ),
        expected_route_effect=expected_route_effect,
    )


def generate_feature_showcase_scenarios(road_map: RoadMap) -> List[RouteFeatureScenario]:
    """Create semantic-location cases from the generated map's own features.

    These records are intentionally expectations and controlled inputs, not
    route-quality results. The baseline is graph-only; the Agent-readable
    input additionally exposes the fixed road-feature protocol.
    """

    by_type: Dict[str, List[RoadsideFeature]] = {}
    for feature in road_map.roadside_features:
        by_type.setdefault(feature.feature_type, []).append(feature)

    scenarios: List[RouteFeatureScenario] = []
    signs = by_type.get("road_sign", [])
    poles = by_type.get("utility_pole", [])
    if signs:
        feature = signs[0]
        scenarios.append(
            _scenario_for_feature(
                road_map,
                feature,
                "feature-near-side-01",
                "路牌近侧定位",
                "ambiguous_near_side",
                "语义定位落在道路两侧候选点之间，路牌指向近侧合法上车点。",
                "请在{0}旁边、{1}安排上车点。".format(
                    feature.label, _side_label(feature.side_of_road)
                ),
                "修正定位后减少跨侧绕行，提升上车点精准匹配率。",
            )
        )
    if poles:
        feature = poles[0]
        scenarios.append(
            _scenario_for_feature(
                road_map,
                feature,
                "feature-two-way-01",
                "双向道路另一侧修正",
                "two_way_opposite_side",
                "电线杆位于双向道路一侧，模糊 GPS 可能把乘客放到对侧节点。",
                "请找{0}旁、朝向{1}的近侧合法上车点。".format(
                    feature.label, _orientation_label(feature.orientation)
                ),
                "避免先到错误一侧再掉头或绕行，降低接驾距离和时间。",
            )
        )
    if len(signs) > 1:
        feature = signs[1]
        scenarios.append(
            _scenario_for_feature(
                road_map,
                feature,
                "feature-semantic-anchor-01",
                "道路特征语义锚点",
                "semantic_anchor",
                "没有精确节点 ID 时，用路牌名称和道路侧别恢复可解释的上车位置。",
                "乘客说在{0}附近上车，请结合路牌和道路侧别判断具体节点。".format(
                    feature.label
                ),
                "把语义道路特征转换为候选节点，减少仅按名称检索造成的定位偏差。",
            )
        )
    return scenarios


def showcase_payload(road_map: RoadMap) -> Dict[str, object]:
    scenarios = generate_feature_showcase_scenarios(road_map)
    return {
        "map_id": road_map.map_id,
        "protocol": "RoadFeatureInputProtocol v1",
        "baseline_visibility": "graph_only",
        "agent_visibility": "road_feature_input_protocol_v1",
        "roadside_features": [feature.to_dict() for feature in road_map.roadside_features],
        "scenarios": [scenario.to_dict() for scenario in scenarios],
    }


def _ordered_nodes(road_map: RoadMap):
    return sorted(road_map.nodes, key=lambda node: (node.x, node.y, node.node_id))


def _pick_node(nodes: Sequence[Any], excluded: Set[str], rng: random.Random):
    candidates = [node for node in nodes if node.node_id not in excluded]
    if not candidates:
        raise ValueError("formal scenario generation ran out of distinct nodes")
    return candidates[rng.randrange(len(candidates))]


def _formal_task(
    seed: int,
    case_name: str,
    task_type: str,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
) -> VehicleOrderTask:
    suffix = "{0:04d}-{1}".format(seed, case_name)
    return VehicleOrderTask(
        task_id="formal-task-" + suffix,
        vehicle_id="formal-vehicle-" + suffix,
        order_id="formal-order-" + suffix,
        start_node_id=start_node_id,
        pickup_node_id=pickup_node_id,
        dropoff_node_id=dropoff_node_id,
        task_type=task_type,
        true_pickup_node_id=pickup_node_id,
        true_dropoff_node_id=dropoff_node_id,
    )


def _adjacency(road_map: RoadMap, blocked_edge_ids: Set[str]):
    adjacency: Dict[str, List[Tuple[str, float]]] = {}
    for edge in road_map.edges:
        if edge.edge_id in blocked_edge_ids:
            continue
        adjacency.setdefault(edge.from_node, []).append(
            (edge.to_node, edge.length_m)
        )
    return adjacency


def _shortest_distances(
    road_map: RoadMap, source_node_id: str, blocked_edge_ids: Set[str]
) -> Dict[str, float]:
    adjacency = _adjacency(road_map, blocked_edge_ids)
    distances = {source_node_id: 0.0}
    queue = [(0.0, source_node_id)]
    while queue:
        distance, node_id = heapq.heappop(queue)
        if distance != distances[node_id]:
            continue
        for neighbor_id, edge_length in adjacency.get(node_id, []):
            candidate = distance + edge_length
            if candidate < distances.get(neighbor_id, float("inf")):
                distances[neighbor_id] = candidate
                heapq.heappush(queue, (candidate, neighbor_id))
    return distances


def _find_detour_pair(
    road_map: RoadMap, legal_pickups: Sequence[Any]
) -> Tuple[Any, Any, float, float]:
    """Find a directed-graph leg longer than its geometric lower bound."""

    nodes = _ordered_nodes(road_map)
    targets = sorted(legal_pickups, key=lambda node: (node.x, node.y, node.node_id))
    for source in nodes:
        distances = _shortest_distances(road_map, source.node_id, set())
        for target in targets:
            if target.node_id == source.node_id:
                continue
            lower_bound = float(
                (abs(source.x - target.x) + abs(source.y - target.y)) * 100
            )
            shortest = distances.get(target.node_id)
            if shortest is not None and shortest > lower_bound:
                return source, target, shortest, lower_bound
    raise ValueError(
        "map does not contain a directed detour; use a larger map or regenerate"
    )


def _case_record(
    road_map: RoadMap,
    seed: int,
    scenario_type: str,
    task: VehicleOrderTask,
    coverage: CoverageReport,
    blocked_edge_ids: Optional[Sequence[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ScenarioRecord:
    return ScenarioRecord(
        scenario_id="formal-{0:04d}-{1}".format(seed, scenario_type),
        random_seed=seed,
        road_map=road_map,
        tasks=[task],
        coverage=coverage,
        scenario_type=scenario_type,
        blocked_edge_ids=list(blocked_edge_ids or []),
        metadata=dict(metadata or {}),
    )


def generate_formal_scenario_set(
    road_map: RoadMap, seed: int
) -> FormalScenarioSet:
    """Generate the required feasible, boundary, illegal, detour and unreachable cases.

    Each case is an independent ``ScenarioRecord``.  In particular, edge
    closures used to make the unreachable case do not affect the feasible or
    detour cases in the same seed's set.
    """

    rng = random.Random(seed)
    nodes = _ordered_nodes(road_map)
    legal_pickups = [node for node in nodes if node.legal_pickup]
    legal_dropoffs = [node for node in nodes if node.legal_dropoff]
    illegal_pickups = [node for node in nodes if not node.legal_pickup]
    illegal_dropoffs = [node for node in nodes if not node.legal_dropoff]
    if not legal_pickups or not legal_dropoffs:
        raise ValueError("formal scenarios require legal pickup and dropoff nodes")
    if not illegal_pickups or not illegal_dropoffs:
        raise ValueError("formal scenarios require illegal endpoint candidates")

    counts = {
        "legal_pickup_count": len(legal_pickups),
        "legal_dropoff_count": len(legal_dropoffs),
    }
    all_coverage = CoverageReport(
        feasible_case=True,
        detour_case=True,
        unreachable_case=True,
        boundary_case=True,
        illegal_endpoint_case=True,
        **counts
    )

    feasible_start = _pick_node(nodes, set(), rng)
    feasible_pickup = _pick_node(legal_pickups, {feasible_start.node_id}, rng)
    feasible_dropoff = _pick_node(
        legal_dropoffs, {feasible_start.node_id, feasible_pickup.node_id}, rng
    )
    feasible_task = _formal_task(
        seed,
        "feasible",
        "feasible_exact",
        feasible_start.node_id,
        feasible_pickup.node_id,
        feasible_dropoff.node_id,
    )

    boundary_nodes = [
        node
        for node in nodes
        if node.x in (road_map.boundary["min_x"], road_map.boundary["max_x"])
        or node.y in (road_map.boundary["min_y"], road_map.boundary["max_y"])
    ]
    boundary_start = _pick_node(boundary_nodes, set(), rng)
    boundary_pickup = _pick_node(legal_pickups, {boundary_start.node_id}, rng)
    boundary_dropoff = _pick_node(
        legal_dropoffs, {boundary_start.node_id, boundary_pickup.node_id}, rng
    )
    boundary_task = _formal_task(
        seed,
        "boundary",
        "boundary_exact",
        boundary_start.node_id,
        boundary_pickup.node_id,
        boundary_dropoff.node_id,
    )

    illegal_start = _pick_node(nodes, set(), rng)
    illegal_pickup = _pick_node(illegal_pickups, {illegal_start.node_id}, rng)
    illegal_dropoff = _pick_node(
        illegal_dropoffs, {illegal_start.node_id, illegal_pickup.node_id}, rng
    )
    illegal_task = _formal_task(
        seed,
        "illegal",
        "illegal_endpoint",
        illegal_start.node_id,
        illegal_pickup.node_id,
        illegal_dropoff.node_id,
    )

    detour_start, detour_pickup, shortest_distance, lower_bound = _find_detour_pair(
        road_map, legal_pickups
    )
    detour_dropoff = _pick_node(
        legal_dropoffs, {detour_start.node_id, detour_pickup.node_id}, rng
    )
    detour_task = _formal_task(
        seed,
        "detour",
        "detour_exact",
        detour_start.node_id,
        detour_pickup.node_id,
        detour_dropoff.node_id,
    )
    detour_metadata = {
        "leg": "start_to_pickup",
        "shortest_distance_m": shortest_distance,
        "geometric_lower_bound_m": lower_bound,
        "detour_reason": "directed_road_constraints",
    }

    unreachable_start = _pick_node(nodes, set(), rng)
    unreachable_dropoff = _pick_node(
        legal_dropoffs, {unreachable_start.node_id}, rng
    )
    unreachable_pickup = _pick_node(
        legal_pickups, {unreachable_start.node_id, unreachable_dropoff.node_id}, rng
    )
    blocked_edge_ids = [
        edge.edge_id
        for edge in road_map.edges
        if edge.from_node == unreachable_dropoff.node_id
        or edge.to_node == unreachable_dropoff.node_id
    ]
    if not blocked_edge_ids:
        raise ValueError("unreachable scenario requires incident edges to block")
    unreachable_task = _formal_task(
        seed,
        "unreachable",
        "unreachable_exact",
        unreachable_start.node_id,
        unreachable_pickup.node_id,
        unreachable_dropoff.node_id,
    )

    cases = [
        _case_record(
            road_map,
            seed,
            "feasible",
            feasible_task,
            CoverageReport(feasible_case=True, **counts),
        ),
        _case_record(
            road_map,
            seed,
            "boundary",
            boundary_task,
            CoverageReport(boundary_case=True, **counts),
        ),
        _case_record(
            road_map,
            seed,
            "illegal_endpoint",
            illegal_task,
            CoverageReport(illegal_endpoint_case=True, **counts),
        ),
        _case_record(
            road_map,
            seed,
            "detour",
            detour_task,
            CoverageReport(detour_case=True, **counts),
            metadata=detour_metadata,
        ),
        _case_record(
            road_map,
            seed,
            "unreachable",
            unreachable_task,
            CoverageReport(unreachable_case=True, **counts),
            blocked_edge_ids=blocked_edge_ids,
            metadata={
                "unreachable_reason": "incident_edges_closed",
                "blocked_edge_count": len(blocked_edge_ids),
            },
        ),
    ]
    return FormalScenarioSet(
        set_id="formal-scenario-set-{0:04d}".format(seed),
        random_seed=seed,
        road_map=road_map,
        scenarios=cases,
        coverage=all_coverage,
        metadata={
            "case_types": [case.scenario_type for case in cases],
            "case_details": {"detour_exact": detour_metadata},
        },
    )


def generate_formal_scenario_batch(
    seeds: Sequence[int],
    size: int = 100,
    config: Optional[MapConfig] = None,
) -> List[FormalScenarioSet]:
    """Prepare one formal scenario set per requested seed.

    The caller can pass ``list(range(10))`` later for the ten-seed run. This
    function does not execute any seeds until the caller supplies them.
    """

    seed_list = list(seeds)
    if not seed_list:
        raise ValueError("at least one seed is required")
    if len(set(seed_list)) != len(seed_list):
        raise ValueError("seeds must be unique")
    map_config = config or MapConfig(size=size)
    if map_config.size != size:
        raise ValueError("config.size must match size")
    return [
        generate_formal_scenario_set(
            generate_map(seed, size, map_config), seed=seed
        )
        for seed in seed_list
    ]
