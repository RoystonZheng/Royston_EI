"""Deterministic order-task generation on top of an existing road map.

The generator intentionally does not create or modify a map.  It consumes a
``RoadMap`` and produces route-layer vehicle/order tasks that can later be
converted into a shared ``RouteInputPackage``.
"""

import random
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import JsonModel, RoadMap, RoadsideFeature, VehicleOrderTask


SEMANTIC_TASK_TYPES = (
    "semantic_landmark_nearby",
    "semantic_landmark_relative",
    "semantic_road_feature_position",
)


@dataclass
class TaskGenerationConfig(JsonModel):
    """Configuration for one reproducible route-task batch."""

    task_count: int = 10
    exact_ratio: float = 0.7
    vehicle_prefix: str = "vehicle"
    order_prefix: str = "order"
    task_prefix: str = "task"
    require_all_semantic_types: bool = True


@dataclass
class OrderTaskBatch(JsonModel):
    """Tasks and audit metadata kept separate from the source road map.

    ``to_dict`` exposes a planner-safe task view and moves semantic truth
    nodes into a separate evaluator-only ``ground_truth`` list.
    """

    map_id: str
    map_version: str
    random_seed: int
    task_count: int
    exact_ratio: float
    exact_task_count: int
    semantic_task_count: int
    semantic_type_counts: Dict[str, int]
    tasks: List[VehicleOrderTask] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        payload = super().to_dict()
        ground_truth = []
        for task in payload["tasks"]:
            ground_truth.append(
                {
                    "task_id": task["task_id"],
                    "true_pickup_node_id": task.pop("true_pickup_node_id"),
                    "true_dropoff_node_id": task.pop("true_dropoff_node_id"),
                }
            )
        payload["ground_truth"] = ground_truth
        return payload


_DIRECTION_LABELS = {
    "north": "北侧",
    "south": "南侧",
    "east": "东侧",
    "west": "西侧",
}


def _validate_config(config: TaskGenerationConfig) -> None:
    if config.task_count <= 0:
        raise ValueError("task_count must be positive")
    if not 0.0 <= config.exact_ratio <= 1.0:
        raise ValueError("exact_ratio must be between 0 and 1")
    if config.require_all_semantic_types and config.task_count < len(SEMANTIC_TASK_TYPES):
        raise ValueError(
            "task_count must be at least {0} to cover all semantic task types".format(
                len(SEMANTIC_TASK_TYPES)
            )
        )


def _rounded_exact_count(task_count: int, exact_ratio: float) -> int:
    """Round to the nearest integer while keeping both classes non-empty."""

    exact_count = int(task_count * exact_ratio + 0.5)
    exact_count = max(0, min(task_count, exact_count))
    if task_count > 1 and exact_count == task_count:
        exact_count = task_count - 1
    if task_count > 1 and exact_count == 0:
        exact_count = 1
    return exact_count


def _node_lookup(road_map: RoadMap) -> Dict[str, object]:
    return {node.node_id: node for node in road_map.nodes}


def _pick_node(
    rng: random.Random,
    candidates: Sequence[object],
    excluded: Iterable[str] = (),
) -> object:
    excluded_set = set(excluded)
    available = [node for node in candidates if node.node_id not in excluded_set]
    if not available:
        raise ValueError("not enough distinct nodes for order task generation")
    return rng.choice(sorted(available, key=lambda node: node.node_id))


def _nearest_node(candidates: Sequence[object], x: int, y: int) -> object:
    if not candidates:
        raise ValueError("no candidate nodes available")
    return min(
        candidates,
        key=lambda node: (abs(node.x - x) + abs(node.y - y), node.node_id),
    )


def _relative_pickup(
    rng: random.Random,
    landmarks: Sequence[object],
    legal_pickups: Sequence[object],
    excluded: Iterable[str],
) -> Tuple[object, str]:
    landmark = _pick_node(rng, landmarks)
    excluded_set = set(excluded)
    directions = [
        ("east", lambda dx, dy: dx > 0),
        ("west", lambda dx, dy: dx < 0),
        ("north", lambda dx, dy: dy > 0),
        ("south", lambda dx, dy: dy < 0),
    ]
    rng.shuffle(directions)
    for direction, predicate in directions:
        candidates = [
            node
            for node in legal_pickups
            if node.node_id not in excluded_set
            and predicate(node.x - landmark.x, node.y - landmark.y)
        ]
        if candidates:
            pickup = _nearest_node(candidates, landmark.x, landmark.y)
            return pickup, "请在{0}{1}的合法上车点上车。".format(
                landmark.landmark_name, _DIRECTION_LABELS[direction]
            )

    pickup = _nearest_node(
        [node for node in legal_pickups if node.node_id not in excluded_set],
        landmark.x,
        landmark.y,
    )
    return pickup, "请在{0}附近的合法上车点上车。".format(landmark.landmark_name)


def _feature_pickup(
    rng: random.Random,
    features: Sequence[RoadsideFeature],
    node_lookup: Dict[str, object],
    excluded: Iterable[str],
) -> Tuple[object, str]:
    excluded_set = set(excluded)
    candidates = [
        feature
        for feature in features
        if feature.node_id in node_lookup
        and node_lookup[feature.node_id].legal_pickup
        and feature.node_id not in excluded_set
    ]
    if not candidates:
        raise ValueError("no usable roadside feature for semantic task")
    feature = rng.choice(sorted(candidates, key=lambda item: item.feature_id))
    side_label = _DIRECTION_LABELS.get(
        feature.side_of_road.replace("_side", ""), "道路一侧"
    )
    pickup = node_lookup[feature.node_id]
    return pickup, "请在{0}旁、{1}的合法上车点上车。".format(
        feature.label, side_label
    )


def _make_task(
    road_map: RoadMap,
    rng: random.Random,
    task_type: str,
    legal_pickups: Sequence[object],
    legal_dropoffs: Sequence[object],
    landmarks: Sequence[object],
    node_lookup: Dict[str, object],
) -> VehicleOrderTask:
    true_pickup = None
    pickup_text = None
    if task_type == "exact_node":
        true_pickup = _pick_node(rng, legal_pickups)
    elif task_type == "semantic_landmark_nearby":
        landmark = _pick_node(rng, landmarks)
        candidates = [
            node for node in legal_pickups if node.node_id != landmark.node_id
        ]
        true_pickup = _nearest_node(candidates, landmark.x, landmark.y)
        pickup_text = "请在{0}附近的合法上车点上车。".format(landmark.landmark_name)
    elif task_type == "semantic_landmark_relative":
        true_pickup, pickup_text = _relative_pickup(
            rng, landmarks, legal_pickups, ()
        )
    elif task_type == "semantic_road_feature_position":
        true_pickup, pickup_text = _feature_pickup(
            rng, road_map.roadside_features, node_lookup, ()
        )
    else:
        raise ValueError("unknown task type: {0}".format(task_type))

    true_dropoff = _pick_node(rng, legal_dropoffs, {true_pickup.node_id})
    if task_type == "exact_node":
        dropoff_text = None
    else:
        dropoff_landmark = _pick_node(rng, landmarks, {true_pickup.node_id})
        dropoff_candidates = [
            node
            for node in legal_dropoffs
            if node.node_id != true_pickup.node_id
        ]
        true_dropoff = _nearest_node(
            dropoff_candidates, dropoff_landmark.x, dropoff_landmark.y
        )
        dropoff_text = "请在{0}附近下车。".format(dropoff_landmark.landmark_name)

    start = _pick_node(
        rng,
        list(node_lookup.values()),
        {true_pickup.node_id, true_dropoff.node_id},
    )
    return VehicleOrderTask(
        task_id="",
        vehicle_id="",
        order_id="",
        start_node_id=start.node_id,
        pickup_node_id=true_pickup.node_id if task_type == "exact_node" else None,
        dropoff_node_id=true_dropoff.node_id if task_type == "exact_node" else None,
        pickup_text=pickup_text,
        dropoff_text=dropoff_text,
        task_type=task_type,
        true_pickup_node_id=true_pickup.node_id,
        true_dropoff_node_id=true_dropoff.node_id,
    )


def generate_order_tasks(
    road_map: RoadMap,
    seed: int,
    config: Optional[TaskGenerationConfig] = None,
) -> OrderTaskBatch:
    """Generate a reproducible mixed exact/semantic route-task batch.

    The map is treated as read-only.  Exact tasks expose endpoint IDs, while
    semantic tasks keep the true endpoint IDs only in audit fields and expose
    natural-language descriptions to downstream planners.
    """

    config = config or TaskGenerationConfig()
    _validate_config(config)
    node_lookup = _node_lookup(road_map)
    legal_pickups = sorted(
        [node for node in road_map.nodes if node.legal_pickup],
        key=lambda node: node.node_id,
    )
    legal_dropoffs = sorted(
        [node for node in road_map.nodes if node.legal_dropoff],
        key=lambda node: node.node_id,
    )
    landmarks = sorted(
        [node for node in road_map.nodes if node.landmark_name],
        key=lambda node: node.node_id,
    )
    if not legal_pickups or not legal_dropoffs:
        raise ValueError("road map must contain legal pickup and dropoff nodes")
    if config.require_all_semantic_types and not landmarks:
        raise ValueError("road map must contain landmarks for semantic tasks")
    if config.require_all_semantic_types and not road_map.roadside_features:
        raise ValueError("road map must contain roadside features for semantic tasks")

    exact_count = _rounded_exact_count(config.task_count, config.exact_ratio)
    semantic_count = config.task_count - exact_count
    if config.require_all_semantic_types and semantic_count < len(SEMANTIC_TASK_TYPES):
        raise ValueError(
            "exact_ratio leaves fewer than {0} semantic tasks".format(
                len(SEMANTIC_TASK_TYPES)
            )
        )

    rng = random.Random(seed)
    task_types = ["exact_node"] * exact_count
    semantic_types = list(SEMANTIC_TASK_TYPES)
    for index in range(semantic_count):
        task_types.append(semantic_types[index % len(semantic_types)])
    rng.shuffle(task_types)

    tasks: List[VehicleOrderTask] = []
    type_counts = {task_type: 0 for task_type in SEMANTIC_TASK_TYPES}
    for index, task_type in enumerate(task_types, start=1):
        task = _make_task(
            road_map,
            rng,
            task_type,
            legal_pickups,
            legal_dropoffs,
            landmarks,
            node_lookup,
        )
        task.task_id = "{0}-{1:04d}-{2:04d}".format(
            config.task_prefix, seed, index
        )
        task.vehicle_id = "{0}-{1:04d}-{2:04d}".format(
            config.vehicle_prefix, seed, index
        )
        task.order_id = "{0}-{1:04d}-{2:04d}".format(
            config.order_prefix, seed, index
        )
        tasks.append(task)
        if task_type != "exact_node":
            type_counts[task_type] += 1

    return OrderTaskBatch(
        map_id=road_map.map_id,
        map_version=road_map.version,
        random_seed=seed,
        task_count=len(tasks),
        exact_ratio=config.exact_ratio,
        exact_task_count=exact_count,
        semantic_task_count=semantic_count,
        semantic_type_counts=type_counts,
        tasks=tasks,
    )
