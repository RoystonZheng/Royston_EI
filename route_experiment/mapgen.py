"""Seeded discrete road-map generation for the route-layer experiment."""

import random
from typing import Dict, List, Tuple

from .models import Edge, MapConfig, Node, RoadMap, RoadsideFeature


LANDMARKS: List[Tuple[str, List[str]]] = [
    ("体育馆东门", ["体育馆东侧入口"]),
    ("火车站", ["车站"]),
    ("市民广场", ["广场"]),
    ("科技园", ["科技园区"]),
    ("医院南门", ["医院南侧入口"]),
    ("图书馆", ["图书馆入口"]),
    ("会展中心", ["展览中心"]),
    ("大学城", ["高校区"]),
    ("商业街", ["步行街"]),
    ("机场接驳点", ["机场站"]),
    ("公园北门", ["公园入口"]),
    ("客运中心", ["长途站"]),
]

ROAD_SIGN_LABELS: List[Tuple[str, List[str]]] = [
    ("体育馆方向路牌", ["体育馆路牌", "体育馆方向"]),
    ("北侧入口指示牌", ["北门指示牌", "北侧入口"]),
    ("公交站路牌", ["公交站牌", "站牌"]),
    ("辅路入口路牌", ["辅路指示牌", "辅路入口"]),
    ("掉头口提示牌", ["掉头提示", "掉头口"]),
]

UTILITY_POLE_LABELS: List[Tuple[str, List[str]]] = [
    ("电线杆A", ["A号电线杆", "电杆A"]),
    ("电线杆B", ["B号电线杆", "电杆B"]),
    ("电线杆C", ["C号电线杆", "电杆C"]),
    ("电线杆D", ["D号电线杆", "电杆D"]),
    ("电线杆E", ["E号电线杆", "电杆E"]),
]


def _node_id(x: int, y: int) -> str:
    return "node_{0}_{1}".format(x, y)


def _edge_id(from_node: str, to_node: str) -> str:
    return "edge_{0}_{1}".format(from_node, to_node)


def _congestion_level(rng: random.Random) -> int:
    value = rng.random()
    if value < 0.60:
        return 0
    if value < 0.90:
        return 1
    return 2


def _road_state(rng: random.Random, config: MapConfig) -> Tuple[float, int, float]:
    base_speed = rng.choice((8.0, 10.0, 12.0))
    congestion = _congestion_level(rng)
    if rng.random() < config.signal_probability:
        signal_delay = float(rng.choice((10, 15, 20)))
    else:
        signal_delay = 0.0
    return base_speed, congestion, signal_delay


def _make_edge(
    from_node: str,
    to_node: str,
    base_speed: float,
    congestion: int,
    signal_delay: float,
    one_way: bool,
) -> Edge:
    return Edge(
        edge_id=_edge_id(from_node, to_node),
        from_node=from_node,
        to_node=to_node,
        length_m=100.0,
        base_speed_mps=base_speed,
        one_way=one_way,
        congestion_level=congestion,
        signal_delay_sec=signal_delay,
    )


def _side_and_orientation(node: Node, paired: Node) -> Tuple[str, str]:
    dx = paired.x - node.x
    dy = paired.y - node.y
    if dx > 0:
        return "west_side", "toward_east"
    if dx < 0:
        return "east_side", "toward_west"
    if dy > 0:
        return "south_side", "toward_north"
    return "north_side", "toward_south"


def _make_roadside_features(
    nodes: List[Node], edges: List[Edge], rng: random.Random, config: MapConfig
) -> List[RoadsideFeature]:
    """Place semantic roadside objects on deterministic two-sided road pairs."""

    node_by_id = {node.node_id: node for node in nodes}
    directed_pairs = {(edge.from_node, edge.to_node) for edge in edges}
    pairs: List[Tuple[str, str]] = []
    for from_node, to_node in sorted(directed_pairs):
        if (to_node, from_node) not in directed_pairs:
            continue
        pair = tuple(sorted((from_node, to_node)))
        if pair not in pairs:
            pairs.append(pair)
    rng.shuffle(pairs)

    features: List[RoadsideFeature] = []
    used_nodes = set()

    def add_features(
        feature_type: str,
        labels: List[Tuple[str, List[str]]],
        requested_count: int,
        role: str,
        prefix: str,
    ) -> None:
        for index in range(max(0, requested_count)):
            available = [pair for pair in pairs if pair[0] not in used_nodes and pair[1] not in used_nodes]
            if not available:
                break
            legal_available = [
                pair
                for pair in available
                if node_by_id[pair[0]].legal_pickup and node_by_id[pair[1]].legal_pickup
            ]
            chosen_pool = legal_available or available
            node_id, paired_node_id = chosen_pool[index % len(chosen_pool)]
            node = node_by_id[node_id]
            paired = node_by_id[paired_node_id]
            # Showcase candidates must be valid pickup points on both sides;
            # this lets a later route run isolate semantic side selection from
            # an unrelated illegal-endpoint failure.
            if not legal_available:
                node.legal_pickup = True
                paired.legal_pickup = True
            label, aliases = labels[index % len(labels)]
            side, orientation = _side_and_orientation(node, paired)
            features.append(
                RoadsideFeature(
                    feature_id="{0}_{1:02d}".format(prefix, index + 1),
                    feature_type=feature_type,
                    node_id=node_id,
                    label=label,
                    aliases=list(aliases),
                    side_of_road=side,
                    orientation=orientation,
                    semantic_role=role,
                    paired_node_id=paired_node_id,
                )
            )
            used_nodes.add(node_id)
            used_nodes.add(paired_node_id)

    add_features(
        "road_sign",
        ROAD_SIGN_LABELS,
        config.road_sign_count,
        "semantic_pickup_reference",
        "sign",
    )
    add_features(
        "utility_pole",
        UTILITY_POLE_LABELS,
        config.utility_pole_count,
        "semantic_side_reference",
        "pole",
    )
    return features


def generate_map(seed: int, size: int, config: MapConfig) -> RoadMap:
    """Generate a deterministic square grid road map without wrap-around edges."""

    if size < 2:
        raise ValueError("size must be at least 2")
    if config.size != size:
        raise ValueError("config.size must match size")
    if not 0.0 <= config.one_way_probability <= 1.0:
        raise ValueError("one_way_probability must be between 0 and 1")
    if not 0.0 <= config.signal_probability <= 1.0:
        raise ValueError("signal_probability must be between 0 and 1")
    if config.road_sign_count < 0 or config.utility_pole_count < 0:
        raise ValueError("roadside feature counts must be non-negative")

    rng = random.Random(seed)
    landmark_count = min(len(LANDMARKS), max(1, size // 8))
    landmark_positions = rng.sample(
        [(x, y) for x in range(size) for y in range(size)], landmark_count
    )
    landmark_by_position: Dict[Tuple[int, int], Tuple[str, List[str]]] = {
        position: LANDMARKS[index]
        for index, position in enumerate(landmark_positions)
    }

    nodes: List[Node] = []
    for x in range(size):
        for y in range(size):
            landmark = landmark_by_position.get((x, y))
            nodes.append(
                Node(
                    node_id=_node_id(x, y),
                    x=x,
                    y=y,
                    landmark_name=landmark[0] if landmark else None,
                    landmark_aliases=list(landmark[1]) if landmark else [],
                    legal_pickup=rng.random() < 0.25,
                    legal_dropoff=rng.random() < 0.25,
                )
            )

    # Each undirected grid neighbor pair is sampled once. A pair becomes either
    # one directed road or two opposing roads, so boundary edges never wrap.
    edges: List[Edge] = []
    for x in range(size):
        for y in range(size):
            source = _node_id(x, y)
            for target_x, target_y in ((x + 1, y), (x, y + 1)):
                if target_x >= size or target_y >= size:
                    continue
                target = _node_id(target_x, target_y)
                base_speed, congestion, signal_delay = _road_state(rng, config)
                if rng.random() < config.one_way_probability:
                    if rng.random() < 0.5:
                        edges.append(
                            _make_edge(
                                source,
                                target,
                                base_speed,
                                congestion,
                                signal_delay,
                                True,
                            )
                        )
                    else:
                        edges.append(
                            _make_edge(
                                target,
                                source,
                                base_speed,
                                congestion,
                                signal_delay,
                                True,
                            )
                        )
                else:
                    edges.append(
                        _make_edge(
                            source,
                            target,
                            base_speed,
                            congestion,
                            signal_delay,
                            False,
                        )
                    )
                    edges.append(
                        _make_edge(
                            target,
                            source,
                            base_speed,
                            congestion,
                            signal_delay,
                            False,
                        )
                    )

    # Ensure every generated map has both legal endpoint classes even for a
    # deliberately tiny test map or an unusual random seed.
    if not any(node.legal_pickup for node in nodes):
        nodes[0].legal_pickup = True
    if not any(node.legal_dropoff for node in nodes):
        nodes[-1].legal_dropoff = True

    roadside_features = _make_roadside_features(nodes, edges, rng, config)

    return RoadMap(
        map_id="grid-{0}x{0}-seed-{1}".format(size, seed),
        version="grid-v1-{0}".format(size),
        size=size,
        nodes=nodes,
        edges=edges,
        boundary={"min_x": 0, "max_x": size - 1, "min_y": 0, "max_y": size - 1},
        road_state_version="road-state-v1-seed-{0}".format(seed),
        roadside_features=roadside_features,
    )
