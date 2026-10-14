"""Deterministic graph-only semantic endpoint resolution for baselines.

This resolver is intentionally weaker than the future road-feature protocol.
It can use landmark names, aliases, coordinates and legal endpoint flags already
present in the graph, but it does not inspect roadside-feature entities.  When
the request refers to a sign or utility pole, it chooses a generic geometric
fallback so the traditional route algorithm still runs and can be compared on
the resulting detour or opposite-side error.
"""

from typing import Dict, List, Optional, Sequence, Tuple

from .models import RoadMap, VehicleOrderTask


_DIRECTION_WORDS = {
    "东": "east",
    "东侧": "east",
    "西": "west",
    "西侧": "west",
    "南": "south",
    "南侧": "south",
    "北": "north",
    "北侧": "north",
}


def _resolution_metadata(**values: object) -> Dict[str, object]:
    """Return the shared audit fields for a semantic resolver decision."""

    metadata: Dict[str, object] = {
        "semantic_resolution_method": "graph_only",
        "baseline_visibility": "graph_only",
        "semantic_baseline_visibility": "graph_only",
        "semantic_reads_roadside_features": False,
        "reads_roadside_features": False,
        "semantic_uses_evaluator_truth": False,
        "uses_evaluator_truth": False,
    }
    metadata.update(values)
    return metadata


def _nearest(candidates: Sequence[object], anchor: object) -> Optional[object]:
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda node: (
            abs(node.x - anchor.x) + abs(node.y - anchor.y),
            node.node_id,
        ),
    )


def _match_landmark(text: Optional[str], landmarks: Sequence[object]) -> Optional[object]:
    if not text:
        return None
    matches = []
    for landmark in landmarks:
        names = [landmark.landmark_name] + list(landmark.landmark_aliases)
        if any(name and name in text for name in names):
            matches.append(landmark)
    if not matches:
        return None
    return sorted(matches, key=lambda node: (-len(node.landmark_name or ""), node.node_id))[0]


def _direction(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for label, direction in sorted(_DIRECTION_WORDS.items(), key=lambda item: -len(item[0])):
        if label in text:
            return direction
    return None


def _directional_candidates(
    candidates: Sequence[object], anchor: object, direction: Optional[str]
) -> List[object]:
    if direction is None:
        return []
    if direction == "east":
        return [node for node in candidates if node.x > anchor.x]
    if direction == "west":
        return [node for node in candidates if node.x < anchor.x]
    if direction == "north":
        return [node for node in candidates if node.y > anchor.y]
    return [node for node in candidates if node.y < anchor.y]


def resolve_graph_only_semantic_task(
    road_map: RoadMap, task: VehicleOrderTask
) -> Tuple[Optional[str], Optional[str], Dict[str, object]]:
    """Resolve semantic endpoints with a conventional graph-only baseline.

    The returned IDs are baseline selections, not evaluator truth.  This lets
    traditional algorithms produce a valid route for every semantic task while
    preserving the distinction between the selected candidate and the hidden
    ground-truth endpoint.
    """

    nodes = {node.node_id: node for node in road_map.nodes}
    legal_pickups = sorted(
        [node for node in road_map.nodes if node.legal_pickup],
        key=lambda node: node.node_id,
    )
    legal_dropoffs = sorted(
        [node for node in road_map.nodes if node.legal_dropoff],
        key=lambda node: node.node_id,
    )
    landmarks = [node for node in road_map.nodes if node.landmark_name]
    start = nodes.get(task.start_node_id)
    if start is None:
        return None, None, _resolution_metadata(
            resolved=False,
            semantic_resolution_status="failed",
            reason="start_node_missing",
        )
    if not legal_pickups or not legal_dropoffs:
        return None, None, _resolution_metadata(
            resolved=False,
            semantic_resolution_status="failed",
            reason="legal_endpoint_missing",
        )

    pickup_anchor = _match_landmark(task.pickup_text, landmarks)
    pickup_strategy = "nearest_legal_pickup_to_start_fallback"
    pickup_candidates = legal_pickups
    if pickup_anchor is not None:
        pickup_strategy = "nearest_legal_pickup_to_landmark"
        if task.task_type == "semantic_landmark_relative":
            directional = _directional_candidates(
                legal_pickups, pickup_anchor, _direction(task.pickup_text)
            )
            if directional:
                pickup_candidates = directional
                pickup_strategy = "nearest_legal_pickup_in_graph_direction"
        pickup = _nearest(pickup_candidates, pickup_anchor)
    else:
        # In particular, roadside-feature text is intentionally not matched
        # against RoadMap.roadside_features in the graph-only baseline.
        pickup = _nearest(pickup_candidates, start)

    dropoff_anchor = _match_landmark(task.dropoff_text, landmarks)
    if dropoff_anchor is not None:
        dropoff = _nearest(legal_dropoffs, dropoff_anchor)
        dropoff_strategy = "nearest_legal_dropoff_to_landmark"
    else:
        dropoff = _nearest(legal_dropoffs, pickup or start)
        dropoff_strategy = "nearest_legal_dropoff_to_pickup_fallback"

    if pickup is None or dropoff is None:
        return None, None, _resolution_metadata(
            resolved=False,
            semantic_resolution_status="failed",
            reason="semantic_candidate_missing",
        )
    if dropoff.node_id == pickup.node_id:
        alternatives = [node for node in legal_dropoffs if node.node_id != pickup.node_id]
        dropoff = _nearest(alternatives, pickup)
        if dropoff is None:
            return None, None, _resolution_metadata(
                resolved=False,
                semantic_resolution_status="failed",
                reason="distinct_dropoff_missing",
            )

    status = (
        "fallback"
        if "fallback" in pickup_strategy or "fallback" in dropoff_strategy
        else "resolved"
    )
    return pickup.node_id, dropoff.node_id, _resolution_metadata(
        resolved=True,
        semantic_resolution_status=status,
        pickup_node_id=pickup.node_id,
        dropoff_node_id=dropoff.node_id,
        semantic_candidate_pickup_node_id=pickup.node_id,
        semantic_candidate_dropoff_node_id=dropoff.node_id,
        pickup_strategy=pickup_strategy,
        dropoff_strategy=dropoff_strategy,
        semantic_resolution_strategy="{0};{1}".format(
            pickup_strategy, dropoff_strategy
        ),
    )
