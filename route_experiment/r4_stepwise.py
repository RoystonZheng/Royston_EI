"""Stepwise R4 route generation with compact inputs and a local hard validator.

The language model proposes one directed edge at a time.  This module owns
the authoritative route state and advances it only after deterministic
validation succeeds.  Model self-check fields are retained for audit only.
"""

import hashlib
import heapq
import json
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .llm_adapter import build_r4_route_search_messages, validate_llm_route
from .models import RoadMap, RouteCandidate, RouteInputPackage, VehicleOrderTask
from .r4_agent import _safe_error
from .r4_constraints import R4RouteOutputConstraint, build_r4_route_output_constraint
from .routing import edge_travel_time_sec


_DIRECTION_ORDER = ("E", "W", "N", "S")
_BASE36_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIRECTION_DELTAS = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}
_SELF_CHECK_KEYS = (
    "allowed_directed_edge",
    "starts_at_current",
    "inside_boundary",
    "pickup_order_ok",
    "target_node_is_new",
)
_SELF_CHECK_LOCAL_FLAGS = {
    "allowed_directed_edge": "allowed_directed_edge",
    "starts_at_current": "starts_at_current",
    "inside_boundary": "inside_boundary",
    "pickup_order_ok": "pickup_order_ok",
    "target_node_is_new": "not_revisited",
}
_SELF_CHECK_DESCRIPTIONS = {
    "allowed_directed_edge": "本步的方向对应从当前位置出发且实际允许通行的有向道路边。",
    "starts_at_current": "本步有向道路边的起点就是当前节点 s.n。",
    "inside_boundary": "本步目标节点仍在地图边界内。",
    "pickup_order_ok": "本步不会在上车前到达下车点。",
    "target_node_is_new": "目标节点是首次访问的节点，尚未出现在 s.v 列表中。",
}


def _model_self_check_mismatch_fields(
    model_self_check: Dict[str, Any], local_flags: Dict[str, Any]
) -> List[str]:
    """Return self-check fields that disagree with deterministic local facts."""
    return sorted(
        key
        for key, local_key in _SELF_CHECK_LOCAL_FLAGS.items()
        if model_self_check.get(key) is not local_flags.get(local_key)
    )


def _shortest_path_edge_count(
    edges: Iterable[Any],
    source_node_id: str,
    target_node_id: str,
    forbidden_node_ids: Iterable[str],
    blocked_edge_ids: Iterable[str],
) -> Optional[int]:
    if source_node_id == target_node_id:
        return 0

    forbidden = set(forbidden_node_ids) - {source_node_id, target_node_id}
    blocked = set(blocked_edge_ids)
    adjacency: Dict[str, List[str]] = {}
    for edge in edges:
        if edge.edge_id in blocked:
            continue
        if edge.from_node in forbidden or edge.to_node in forbidden:
            continue
        adjacency.setdefault(edge.from_node, []).append(edge.to_node)

    queue = deque([(source_node_id, 0)])
    seen = {source_node_id}
    while queue:
        current, distance = queue.popleft()
        for neighbor in adjacency.get(current, ()):
            if neighbor == target_node_id:
                return distance + 1
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, distance + 1))
    return None


def _minimum_remaining_move_lower_bound(
    edges: Iterable[Any],
    current_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    pickup_seen: bool,
    visited_node_ids: Iterable[str],
    blocked_edge_ids: Iterable[str],
) -> Optional[int]:
    """Return a safe lower bound on remaining moves without exposing a route."""

    edge_list = list(edges)
    visited = set(visited_node_ids)

    if pickup_seen:
        return _shortest_path_edge_count(
            edge_list,
            current_node_id,
            dropoff_node_id,
            visited - {current_node_id},
            blocked_edge_ids,
        )

    first_leg_forbidden = (visited - {current_node_id, pickup_node_id}) | {
        dropoff_node_id
    }
    second_leg_forbidden = visited - {current_node_id, pickup_node_id}
    first_leg = _shortest_path_edge_count(
        edge_list,
        current_node_id,
        pickup_node_id,
        first_leg_forbidden,
        blocked_edge_ids,
    )
    second_leg = _shortest_path_edge_count(
        edge_list,
        pickup_node_id,
        dropoff_node_id,
        second_leg_forbidden,
        blocked_edge_ids,
    )
    if first_leg is None or second_leg is None:
        return None
    # The two legs are checked independently, so their sum is a lower bound.
    # A candidate is never rejected just because the legs' shortest witnesses
    # might overlap; the final full-route validator still owns that invariant.
    return first_leg + second_leg


def _shortest_path_objective_cost(
    edges: Iterable[Any],
    source_node_id: str,
    target_node_id: str,
    forbidden_node_ids: Iterable[str],
    blocked_edge_ids: Iterable[str],
) -> Optional[Tuple[float, float]]:
    """Return the lexicographic (travel-time, distance) path-cost lower bound."""

    if source_node_id == target_node_id:
        return 0.0, 0.0

    forbidden = set(forbidden_node_ids) - {source_node_id, target_node_id}
    blocked = set(blocked_edge_ids)
    adjacency: Dict[str, List[Any]] = {}
    for edge in edges:
        if edge.edge_id in blocked:
            continue
        if edge.from_node in forbidden or edge.to_node in forbidden:
            continue
        adjacency.setdefault(edge.from_node, []).append(edge)

    start_cost = (0.0, 0.0)
    best: Dict[str, Tuple[float, float]] = {source_node_id: start_cost}
    queue: List[Tuple[float, float, str]] = [(0.0, 0.0, source_node_id)]
    while queue:
        travel_time_sec, distance_m, current = heapq.heappop(queue)
        if best.get(current) != (travel_time_sec, distance_m):
            continue
        if current == target_node_id:
            return travel_time_sec, distance_m
        for edge in adjacency.get(current, ()):
            candidate = (
                travel_time_sec + edge_travel_time_sec(edge),
                distance_m + float(edge.length_m),
            )
            if candidate < best.get(edge.to_node, (float("inf"), float("inf"))):
                best[edge.to_node] = candidate
                heapq.heappush(queue, (*candidate, edge.to_node))
    return None


def _minimum_remaining_objective_cost_lower_bound(
    edges: Iterable[Any],
    current_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    pickup_seen: bool,
    visited_node_ids: Iterable[str],
    blocked_edge_ids: Iterable[str],
) -> Optional[Tuple[float, float]]:
    """Relax cross-leg node conflicts to produce an optimistic cost lower bound."""

    edge_list = list(edges)
    visited = set(visited_node_ids)
    if pickup_seen:
        return _shortest_path_objective_cost(
            edge_list,
            current_node_id,
            dropoff_node_id,
            visited - {current_node_id},
            blocked_edge_ids,
        )

    first_leg_forbidden = (visited - {current_node_id, pickup_node_id}) | {
        dropoff_node_id
    }
    second_leg_forbidden = visited - {current_node_id, pickup_node_id}
    first_leg = _shortest_path_objective_cost(
        edge_list,
        current_node_id,
        pickup_node_id,
        first_leg_forbidden,
        blocked_edge_ids,
    )
    second_leg = _shortest_path_objective_cost(
        edge_list,
        pickup_node_id,
        dropoff_node_id,
        second_leg_forbidden,
        blocked_edge_ids,
    )
    if first_leg is None or second_leg is None:
        return None
    return first_leg[0] + second_leg[0], first_leg[1] + second_leg[1]


def _remaining_route_reachable(
    edges: Iterable[Any],
    current_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    pickup_seen: bool,
    visited_node_ids: Iterable[str],
    blocked_edge_ids: Iterable[str],
) -> bool:
    return _minimum_remaining_move_lower_bound(
        edges,
        current_node_id,
        pickup_node_id,
        dropoff_node_id,
        pickup_seen,
        visited_node_ids,
        blocked_edge_ids,
    ) is not None


def _node_xy(node_id: str) -> Tuple[int, int]:
    match = re.fullmatch(r"node_(-?\d+)_(-?\d+)", str(node_id or ""))
    if match is None:
        raise ValueError("r4_stepwise_node_id_invalid")
    return int(match.group(1)), int(match.group(2))


def _fixed_width_base36(value: int, width: int) -> str:
    if value < 0 or width < 1 or value >= 36 ** width:
        raise ValueError("r4_stepwise_grid_slot_out_of_range")
    output = []
    for _ in range(width):
        output.append(_BASE36_DIGITS[value % 36])
        value //= 36
    return "".join(reversed(output))


def _encode_visited_grid_slots(
    package: RouteInputPackage,
    visited_node_ids: Iterable[str],
    *,
    start_node_id: str,
    current_node_id: str,
) -> Tuple[str, int]:
    min_x = min(node.x for node in package.nodes)
    min_y = min(node.y for node in package.nodes)
    width = max(node.x for node in package.nodes) - min_x + 1
    height = max(node.y for node in package.nodes) - min_y + 1
    slot_width = 1
    while 36 ** slot_width < width * height:
        slot_width += 1
    node_by_id = {node.node_id: node for node in package.nodes}
    slots = []
    for node_id in visited_node_ids:
        if node_id in {start_node_id, current_node_id}:
            continue
        node = node_by_id.get(node_id)
        if node is None:
            raise ValueError("r4_stepwise_visited_nodes_invalid")
        slot = (node.y - min_y) * width + (node.x - min_x)
        slots.append(_fixed_width_base36(slot, slot_width))
    return "".join(slots), slot_width


def _compact_map_from_package(
    package: RouteInputPackage,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
) -> Dict[str, Any]:
    legacy_messages = build_r4_route_search_messages(
        package,
        start_node_id,
        pickup_node_id,
        dropoff_node_id,
        require_completed_route=True,
    )
    legacy_user = json.loads(legacy_messages[1]["content"])
    original = legacy_user["map"]
    boundary = original["boundary_inclusive"]
    return {
        "x0": boundary["min_x"],
        "y0": boundary["min_y"],
        "w": original["width"],
        "h": original["height"],
        "g": original["directional_cost_codes_by_y"],
        "c": original["edge_cost_legend"],
    }


def audit_r4_stepwise_map_equivalence(
    package: RouteInputPackage, compact_map: Dict[str, Any]
) -> Dict[str, Any]:
    """Prove that compact rows preserve every directed edge and its costs."""

    nodes = {(node.x, node.y): node for node in package.nodes}
    edges_by_pair = {(edge.from_node, edge.to_node): edge for edge in package.edges}
    x0, y0, width, height = (
        int(compact_map["x0"]),
        int(compact_map["y0"]),
        int(compact_map["w"]),
        int(compact_map["h"]),
    )
    if width <= 0 or height <= 0 or len(compact_map.get("g", [])) != height:
        return {"passed": False, "reason": "compact_grid_dimensions_invalid"}
    expected_bounds = {
        "x0": min(node.x for node in package.nodes),
        "y0": min(node.y for node in package.nodes),
        "w": max(node.x for node in package.nodes) - min(node.x for node in package.nodes) + 1,
        "h": max(node.y for node in package.nodes) - min(node.y for node in package.nodes) + 1,
    }
    recovered_bounds = {"x0": x0, "y0": y0, "w": width, "h": height}
    if recovered_bounds != expected_bounds:
        return {
            "passed": False,
            "reason": "compact_grid_boundary_mismatch",
            "expected_bounds": expected_bounds,
            "recovered_bounds": recovered_bounds,
        }
    deltas = ((1, 0), (-1, 0), (0, 1), (0, -1))
    expected: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for edge in package.edges:
        key = (edge.from_node, edge.to_node)
        if key in expected:
            return {"passed": False, "reason": "duplicate_package_directed_edge"}
        expected[key] = (round(edge_travel_time_sec(edge), 6), float(edge.length_m))

    recovered: Dict[Tuple[str, str], Tuple[float, float]] = {}
    invalid_codes = []
    for y_index, row in enumerate(compact_map["g"]):
        if not isinstance(row, str) or len(row) != width * 4:
            return {"passed": False, "reason": "compact_grid_row_length_invalid"}
        for x_index in range(width):
            source_node = nodes.get((x0 + x_index, y0 + y_index))
            if source_node is None:
                return {"passed": False, "reason": "compact_grid_node_missing"}
            for direction_index, (dx, dy) in enumerate(deltas):
                code = row[x_index * 4 + direction_index]
                if code == ".":
                    continue
                target_node = nodes.get((x0 + x_index + dx, y0 + y_index + dy))
                if target_node is None:
                    invalid_codes.append(code)
                    continue
                cost = compact_map.get("c", {}).get(code)
                if not isinstance(cost, list) or len(cost) != 2:
                    invalid_codes.append(code)
                    continue
                recovered[(source_node.node_id, target_node.node_id)] = (
                    float(cost[0]), float(cost[1])
                )

    missing = sorted(set(expected) - set(recovered))
    unexpected = sorted(set(recovered) - set(expected))
    cost_mismatches = []
    for pair in set(expected) & set(recovered):
        expected_cost, recovered_cost = expected[pair], recovered[pair]
        if (
            abs(expected_cost[0] - recovered_cost[0]) > 1e-6
            or abs(expected_cost[1] - recovered_cost[1]) > 1e-6
        ):
            cost_mismatches.append(pair)

    def digest(records: Dict[Tuple[str, str], Tuple[float, float]]) -> str:
        payload = [
            [source, target, round(cost[0], 6), round(cost[1], 6)]
            for (source, target), cost in sorted(records.items())
        ]
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    return {
        "passed": not (missing or unexpected or cost_mismatches or invalid_codes),
        "expected_directed_edge_count": len(expected),
        "recovered_directed_edge_count": len(recovered),
        "missing_directed_edges": [list(pair) for pair in missing],
        "unexpected_directed_edges": [list(pair) for pair in unexpected],
        "cost_mismatches": [list(pair) for pair in sorted(cost_mismatches)],
        "invalid_codes": invalid_codes,
        "expected_semantics_sha256": digest(expected),
        "recovered_semantics_sha256": digest(recovered),
    }


def build_r4_stepwise_messages(
    package: RouteInputPackage,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    *,
    current_node_id: str,
    visited_node_ids: Iterable[str],
    pickup_seen: Optional[bool] = None,
    accepted_directions: Iterable[str] = (),
    feedback: Optional[Dict[str, Any]] = None,
    remaining_action_budget: Optional[int] = None,
    route_feature_context: Optional[Dict[str, Any]] = None,
    compact_visited_state: bool = False,
    progress_guidance: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Build the slim route request and current authoritative step state."""

    node_by_id = {node.node_id: node for node in package.nodes}
    if not {start_node_id, pickup_node_id, dropoff_node_id, current_node_id}.issubset(
        node_by_id
    ):
        raise ValueError("r4_stepwise_endpoint_missing_from_package")
    if not node_by_id[pickup_node_id].legal_pickup:
        raise ValueError("r4_stepwise_pickup_not_legal")
    if not node_by_id[dropoff_node_id].legal_dropoff:
        raise ValueError("r4_stepwise_dropoff_not_legal")

    visited = list(visited_node_ids)
    if not visited or visited[0] != start_node_id or visited[-1] != current_node_id:
        raise ValueError("r4_stepwise_visited_state_invalid")
    if len(visited) != len(set(visited)) or any(node_id not in node_by_id for node_id in visited):
        raise ValueError("r4_stepwise_visited_nodes_invalid")
    pickup_seen = pickup_node_id in visited if pickup_seen is None else bool(pickup_seen)
    if pickup_seen != (pickup_node_id in visited):
        raise ValueError("r4_stepwise_pickup_state_mismatch")
    if remaining_action_budget is not None and (
        isinstance(remaining_action_budget, bool)
        or int(remaining_action_budget) < 1
    ):
        raise ValueError("r4_stepwise_remaining_action_budget_invalid")

    compact_map = _compact_map_from_package(
        package, start_node_id, pickup_node_id, dropoff_node_id
    )
    equivalence = audit_r4_stepwise_map_equivalence(package, compact_map)
    if not equivalence["passed"]:
        raise ValueError("r4_stepwise_map_equivalence_failed")
    accepted = list(accepted_directions)
    if any(direction not in _DIRECTION_ORDER for direction in accepted):
        raise ValueError("r4_stepwise_accepted_directions_invalid")
    if len(accepted) != len(visited) - 1:
        raise ValueError("r4_stepwise_accepted_state_length_mismatch")
    for index, direction in enumerate(accepted):
        dx, dy = _DIRECTION_DELTAS[direction]
        source = node_by_id[visited[index]]
        target = node_by_id[visited[index + 1]]
        if (target.x - source.x, target.y - source.y) != (dx, dy):
            raise ValueError("r4_stepwise_accepted_state_route_mismatch")

    protocol_v3 = bool(compact_visited_state or progress_guidance is not None)
    user_payload: Dict[str, Any] = {
        "v": "r4-step-v3" if protocol_v3 else "r4-step-v2",
        "m": compact_map,
        "q": {
            "s": start_node_id,
            "p": pickup_node_id,
            "d": dropoff_node_id,
            "o": "travel_time_then_distance",
        },
        "s": {
            "n": current_node_id,
            "p": pickup_seen,
            "v": visited,
        },
    }
    if compact_visited_state:
        encoded_visited, slot_width = _encode_visited_grid_slots(
            package,
            visited,
            start_node_id=start_node_id,
            current_node_id=current_node_id,
        )
        user_payload["s"].update(
            {
                "v": encoded_visited,
                "vf": "grid_slot_base36",
                "vl": slot_width,
            }
        )
    if progress_guidance is not None:
        try:
            progress_json = json.dumps(
                progress_guidance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            normalized_progress_guidance = json.loads(progress_json)
        except (TypeError, ValueError) as error:
            raise ValueError("r4_stepwise_progress_guidance_invalid") from error
        user_payload["r"] = normalized_progress_guidance
    if remaining_action_budget is not None:
        user_payload["s"]["b"] = int(remaining_action_budget)
    if feedback is not None:
        user_payload["f"] = {
            key: feedback[key]
            for key in ("code", "direction")
            if key in feedback
        }
    if route_feature_context is not None:
        # Keep only JSON data and reject evaluator truth before serialization.
        feature_context_json = json.dumps(
            route_feature_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if any(
            marker in feature_context_json.lower()
            for marker in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")
        ):
            raise ValueError("r4_stepwise_route_feature_context_contains_evaluator_truth")
        user_payload["i"] = route_feature_context
    visited_state_description = (
        "s.v 是访问过节点的固定宽度 base36 网格索引串，s.vf 标明编码，s.vl 是每个索引的字符数；"
        "索引 k 解码为 x=m.x0+(k mod m.w)、y=m.y0+floor(k/m.w)。起点 q.s 与当前节点 s.n 已单独提供，"
        "因此编码串不重复包含它们；起点和当前节点也视为已访问。"
        if compact_visited_state
        else "s.v 是按访问顺序排列的完整 node_x_y 节点编号列表，包含起点和当前节点；"
        "判断是否重访时直接查目标编号是否在 s.v 中。"
    )
    progress_description = (
        "r 给出每个合法方向对应的完整剩余路线时间/距离乐观下界及其相对当前下界的超额；"
        "该估计放宽了两段路径的节点冲突，只作为路线质量提示，不是完整路线保证，也不改变本地准入规则。"
        "本地确定性校验仍是唯一准入依据。优先考虑超额较小且符合任务语义的方向，但不能仅按局部距离贪心。"
        if progress_guidance is not None
        else ""
    )
    system = (
        "你逐步为一辆车提出路线动作。用户消息的 m.g 是有向道路成本网格：坐标原点为 m.x0/m.y0，"
        "行按 y 递增、格点按 x 递增，每格连续四个字符按 E/W/N/S 表示出边；'.' 为无边，其他码在 m.c 中对应"
        "[行驶秒数, 米数]。节点编号为 node_x_y，边界为 x0..x0+w-1、y0..y0+h-1。"
        + visited_state_description
        + " "
        "只依据当前地图事实，从 s.n 提议一条下一步方向；工具 schema 的 direction 候选由本地程序过滤，"
        "仅包含真实允许的有向出边、仍保留到达剩余目标的路线且能放入剩余调用预算的方向。"
        "目标顺序是 q.s→q.p→q.d，首次在已上车后到达 q.d 即结束。不得重复访问节点，不得越界，不得逆单行。"
        "q.o 要求优先降低总行驶秒数，再降低距离。先规划完整的剩余路线，再只提交该路线的下一步；不要只按局部方向贪心。"
        "若 s.b 存在，它表示含本次请求在内的剩余动作调用预算，路线必须在预算耗尽前到达下车点。"
        "若有 i，它是已通过白名单校验的 RoadFeatureInputProtocol v1 相关语义片段和地点意图；"
        "它只说明 q 中固定端点的道路特征依据，不得据此修改 q 的端点，也不能把未列出的道路边当成存在。"
        "输出 submit_r4_route 工具参数：action=move 时只给一个 direction；"
        "action=give_up 时 direction 为空且不声称地图无路。"
        "self_check 必须逐项填写 allowed_directed_edge、starts_at_current、inside_boundary、pickup_order_ok、target_node_is_new；"
        "若目标节点编号首次出现在路线中，target_node_is_new 填 true；若已在 s.v 中，填 false。"
        "它们是你对本步的结构化声明，本地程序会独立校验并决定是否推进状态。"
        "若有 f，按该程序错误修正动作；失败动作没有改变 s 中的当前位置或已访问集合。"
        + progress_description
        + "不要输出长篇自由文本推理。"
    )
    user = json.dumps(user_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


@dataclass
class R4StepOutputConstraint:
    """Strict one-action schema for the current node's allowed directions."""

    current_node_id: str
    edge_by_direction: Dict[str, Any]
    edge_id_by_direction: Dict[str, str]
    filtered_directions: Dict[str, str]
    remaining_minimum_moves: Optional[int]
    remaining_objective_cost_lower_bound: Optional[Dict[str, float]]
    objective_cost_lower_bound_by_direction: Dict[str, Dict[str, Dict[str, float]]]
    remaining_action_budget: Optional[int]
    can_give_up: bool = True

    @property
    def schema(self) -> Dict[str, Any]:
        direction_values = ([""] if self.can_give_up else []) + [
            direction for direction in _DIRECTION_ORDER
            if direction in self.edge_by_direction
        ]
        action_values = (
            ["give_up"]
            if self.can_give_up and not self.edge_by_direction
            else ["move", "give_up"]
            if self.can_give_up
            else ["move"]
        )
        failure_reason_values = (
            ["none"]
            if action_values == ["move"]
            else ["unable_to_construct_route"]
            if action_values == ["give_up"]
            else ["none", "unable_to_construct_route"]
        )
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": action_values,
                },
                "direction": {"type": "string", "enum": direction_values},
                "self_check": {
                    "type": "object",
                    "properties": {
                        key: {
                            "type": "boolean",
                            "description": _SELF_CHECK_DESCRIPTIONS[key],
                        }
                        for key in _SELF_CHECK_KEYS
                    },
                    "required": list(_SELF_CHECK_KEYS),
                    "additionalProperties": False,
                },
                "failure_reason_code": {
                    "type": "string",
                    "enum": failure_reason_values,
                },
            },
            "required": [
                "action",
                "direction",
                "self_check",
                "failure_reason_code",
            ],
            "additionalProperties": False,
        }

    def tool_definition(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "submit_r4_route",
                "strict": True,
                "description": "Submit exactly one compass direction for deterministic local validation.",
                "parameters": self.schema,
            },
        }

    @property
    def tool_choice(self) -> Dict[str, Any]:
        return {"type": "function", "function": {"name": "submit_r4_route"}}

    @property
    def schema_sha256(self) -> str:
        raw = json.dumps(self.tool_definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def schema_bytes(self) -> int:
        raw = json.dumps(self.tool_definition(), sort_keys=True, separators=(",", ":"))
        return len(raw.encode("utf-8"))


def build_r4_step_output_constraint(
    package: RouteInputPackage,
    full_constraint: R4RouteOutputConstraint,
    *,
    current_node_id: str,
    pickup_seen: bool,
    visited_node_ids: Iterable[str],
    blocked_edge_ids: Iterable[str] = (),
    remaining_action_budget: Optional[int] = None,
    include_objective_cost_guidance: bool = False,
) -> R4StepOutputConstraint:
    node_by_id = {node.node_id: node for node in package.nodes}
    if current_node_id not in node_by_id:
        raise ValueError("r4_step_constraint_current_node_missing")
    visited = set(visited_node_ids)
    if pickup_seen != (full_constraint.pickup_node_id in visited):
        raise ValueError("r4_step_constraint_pickup_state_mismatch")
    blocked = set(blocked_edge_ids) | set(package.blocked_edge_ids)
    pickup_seen = bool(pickup_seen)
    if remaining_action_budget is not None and (
        isinstance(remaining_action_budget, bool)
        or int(remaining_action_budget) < 1
    ):
        raise ValueError("r4_step_remaining_action_budget_invalid")
    remaining_minimum_moves = _minimum_remaining_move_lower_bound(
        package.edges,
        current_node_id,
        full_constraint.pickup_node_id,
        full_constraint.dropoff_node_id,
        pickup_seen,
        visited,
        blocked,
    )
    remaining_objective_cost = (
        _minimum_remaining_objective_cost_lower_bound(
            package.edges,
            current_node_id,
            full_constraint.pickup_node_id,
            full_constraint.dropoff_node_id,
            pickup_seen,
            visited,
            blocked,
        )
        if include_objective_cost_guidance
        else None
    )
    can_give_up = remaining_minimum_moves is None or (
        remaining_action_budget is not None
        and remaining_minimum_moves > int(remaining_action_budget)
    )
    edge_by_direction: Dict[str, Any] = {}
    filtered_directions: Dict[str, str] = {}
    objective_cost_lower_bound_by_direction: Dict[
        str, Dict[str, Dict[str, float]]
    ] = {}
    for edge in package.edges:
        if edge.from_node != current_node_id or edge.edge_id in blocked:
            continue
        target = node_by_id.get(edge.to_node)
        if target is None or edge.to_node in visited:
            continue
        if edge.to_node == full_constraint.dropoff_node_id and not pickup_seen:
            continue
        source = node_by_id[current_node_id]
        delta = (target.x - source.x, target.y - source.y)
        direction = next(
            (name for name, expected in _DIRECTION_DELTAS.items() if expected == delta),
            None,
        )
        if direction is None:
            raise ValueError("r4_step_constraint_edge_not_orthogonal")
        if direction in edge_by_direction:
            raise ValueError("r4_step_constraint_duplicate_direction")
        next_visited = visited | {edge.to_node}
        next_pickup_seen = pickup_seen or edge.to_node == full_constraint.pickup_node_id
        next_minimum_moves = _minimum_remaining_move_lower_bound(
            package.edges,
            edge.to_node,
            full_constraint.pickup_node_id,
            full_constraint.dropoff_node_id,
            next_pickup_seen,
            next_visited,
            blocked,
        )
        if next_minimum_moves is None:
            filtered_directions[direction] = "remaining_route_unreachable"
            continue
        if (
            remaining_action_budget is not None
            and next_minimum_moves > int(remaining_action_budget) - 1
        ):
            filtered_directions[direction] = "remaining_action_budget_insufficient"
            continue
        edge_by_direction[direction] = edge
        if include_objective_cost_guidance:
            remaining_cost_after_move = _minimum_remaining_objective_cost_lower_bound(
                package.edges,
                edge.to_node,
                full_constraint.pickup_node_id,
                full_constraint.dropoff_node_id,
                next_pickup_seen,
                next_visited,
                blocked,
            )
            if remaining_cost_after_move is None or remaining_objective_cost is None:
                raise ValueError("r4_step_constraint_objective_bound_inconsistent")
            route_lower_bound_if_taken = {
                "travel_time_sec": edge_travel_time_sec(edge) + remaining_cost_after_move[0],
                "distance_m": float(edge.length_m) + remaining_cost_after_move[1],
            }
            current_lower_bound = {
                "travel_time_sec": remaining_objective_cost[0],
                "distance_m": remaining_objective_cost[1],
            }
            objective_cost_lower_bound_by_direction[direction] = {
                "route_lower_bound_if_taken": {
                    key: round(value, 6) for key, value in route_lower_bound_if_taken.items()
                },
                "excess_over_current_lower_bound": {
                    key: round(route_lower_bound_if_taken[key] - current_lower_bound[key], 6)
                    for key in current_lower_bound
                },
            }
    edge_id_by_direction = {
        direction: edge.edge_id for direction, edge in edge_by_direction.items()
    }
    can_give_up = can_give_up or not edge_by_direction
    return R4StepOutputConstraint(
        current_node_id=current_node_id,
        edge_by_direction=edge_by_direction,
        edge_id_by_direction=edge_id_by_direction,
        filtered_directions=filtered_directions,
        remaining_minimum_moves=remaining_minimum_moves,
        remaining_objective_cost_lower_bound=(
            {
                "travel_time_sec": round(remaining_objective_cost[0], 6),
                "distance_m": round(remaining_objective_cost[1], 6),
            }
            if remaining_objective_cost is not None
            else None
        ),
        objective_cost_lower_bound_by_direction=objective_cost_lower_bound_by_direction,
        remaining_action_budget=(
            int(remaining_action_budget)
            if remaining_action_budget is not None
            else None
        ),
        can_give_up=can_give_up,
    )


def _local_check(
    direction: str,
    current_node_id: str,
    pickup_seen: bool,
    visited_node_ids: List[str],
    node_by_id: Dict[str, Any],
    edge_by_pair: Dict[Tuple[str, str], Any],
    blocked_edge_ids: Set[str],
    full_constraint: R4RouteOutputConstraint,
    remaining_action_budget: Optional[int] = None,
) -> Dict[str, Any]:
    delta = _DIRECTION_DELTAS.get(direction)
    current_node = node_by_id.get(current_node_id)
    expected_target = None
    if delta and current_node:
        expected_target = next(
            (
                node.node_id for node in node_by_id.values()
                if (node.x, node.y) == (current_node.x + delta[0], current_node.y + delta[1])
            ),
            None,
        )
    global_edge = (
        edge_by_pair.get((current_node_id, expected_target))
        if expected_target is not None
        else None
    )
    edge_exists = delta is not None and global_edge is not None
    starts_at_current = bool(global_edge and global_edge.from_node == current_node_id)
    inside_boundary = bool(global_edge and global_edge.to_node in node_by_id)
    pickup_order_ok = bool(
        global_edge
        and not (
            global_edge.to_node == full_constraint.dropoff_node_id and not pickup_seen
        )
    )
    not_revisited = bool(global_edge and global_edge.to_node not in visited_node_ids)
    not_blocked = bool(global_edge and global_edge.edge_id not in blocked_edge_ids)
    stop_rule_ok = not (
        current_node_id == full_constraint.dropoff_node_id and pickup_seen
    )
    flags = {
        "edge_exists": edge_exists,
        "allowed_directed_edge": bool(
            edge_exists
            and starts_at_current
            and inside_boundary
            and pickup_order_ok
            and not_revisited
            and not_blocked
            and stop_rule_ok
        ),
        "starts_at_current": starts_at_current,
        "inside_boundary": inside_boundary,
        "pickup_order_ok": pickup_order_ok,
        "not_revisited": not_revisited,
        "not_blocked": not_blocked,
        "stop_rule_ok": stop_rule_ok,
    }
    errors = []
    if not edge_exists:
        errors.append("directed_edge_missing")
    elif not starts_at_current:
        errors.append("edge_not_allowed_from_current")
    elif not inside_boundary:
        errors.append("target_outside_map")
    elif not not_blocked:
        errors.append("blocked_edge")
    elif not pickup_order_ok:
        errors.append("dropoff_before_pickup")
    elif not not_revisited:
        errors.append("node_revisited")
    elif not stop_rule_ok:
        errors.append("move_after_dropoff")
    remaining_minimum_moves_after_move = None
    if not errors and global_edge is not None:
        next_pickup_seen = pickup_seen or global_edge.to_node == full_constraint.pickup_node_id
        remaining_minimum_moves_after_move = _minimum_remaining_move_lower_bound(
            edge_by_pair.values(),
            global_edge.to_node,
            full_constraint.pickup_node_id,
            full_constraint.dropoff_node_id,
            next_pickup_seen,
            set(visited_node_ids) | {global_edge.to_node},
            blocked_edge_ids,
        )
        if remaining_minimum_moves_after_move is None:
            errors.append("move_leaves_no_reachable_route")
        elif (
            remaining_action_budget is not None
            and remaining_minimum_moves_after_move > remaining_action_budget - 1
        ):
            errors.append("move_exceeds_remaining_action_budget")
    return {
        "passed": not errors,
        "flags": flags,
        "errors": errors,
        "edge": global_edge,
        "expected_target_node_id": expected_target,
        "remaining_minimum_moves_after_move": remaining_minimum_moves_after_move,
    }


def _attempt_replay(
    accepted_nodes: List[str],
    accepted_edges: List[str],
    edge: Optional[Any],
    direction: str,
    current_node_id: str,
    local_checks: Dict[str, Any],
    model_self_check: Dict[str, Any],
) -> Dict[str, Any]:
    pairs = []
    for index, edge_id in enumerate(accepted_edges):
        source, target = accepted_nodes[index], accepted_nodes[index + 1]
        pairs.append(
            {"step": index + 1, "from_node_id": source, "to_node_id": target, "edge_id": edge_id}
        )
    attempted_nodes = list(accepted_nodes)
    attempted_edges = list(accepted_edges)
    if edge is not None:
        source, target, edge_id = current_node_id, edge.to_node, edge.edge_id
    else:
        source = current_node_id
        target = local_checks.get("expected_target_node_id")
        edge_id = "invalid_direction_" + direction
    pairs.append(
        {
            "step": len(accepted_edges) + 1,
            "from_node_id": source,
            "to_node_id": target,
            "edge_id": edge_id if local_checks["passed"] else None,
            "direction": direction,
        }
    )
    if target is not None:
        attempted_nodes.append(target)
    attempted_edges.append(edge_id)
    records = [
        {
            "step": index + 1,
            "from_node_id": accepted_nodes[index],
            "to_node_id": accepted_nodes[index + 1],
            "edge_id": accepted_edges[index],
            "model_edge_check": "DECLARED",
            "local_directed_edge_check": "PASS",
            "local_continuity_check": "PASS",
            "local_endpoint_order_check": "PASS",
            "accepted_step": True,
        }
        for index in range(len(accepted_edges))
    ]
    records.append(
        {
            "step": len(accepted_edges) + 1,
            "from_node_id": source,
            "to_node_id": target,
            "edge_id": edge_id if local_checks["passed"] else None,
            "direction": direction,
            "model_edge_check": (
                "PASS"
                if all(model_self_check.values())
                else "SELF_CHECK_MISMATCH"
                if local_checks["passed"]
                else "FAIL_OR_INCOMPLETE"
            ),
            "model_self_check_mismatch_fields": _model_self_check_mismatch_fields(
                model_self_check, local_checks.get("flags", {})
            ),
            "local_directed_edge_check": "PASS" if local_checks["flags"]["allowed_directed_edge"] else "FAIL",
            "local_continuity_check": "PASS" if local_checks["flags"]["starts_at_current"] else "FAIL",
            "local_endpoint_order_check": "PASS" if local_checks["flags"]["pickup_order_ok"] else "FAIL",
            "accepted_step": bool(local_checks["passed"]),
        }
    )
    violations = [
        {
            "step": len(accepted_edges) + 1,
            "kind": error,
            "from_node_id": source,
            "to_node_id": target,
            "edge_id": edge_id,
            "direction": direction,
        }
        for error in local_checks["errors"]
    ]
    prefix_nodes = list(accepted_nodes)
    prefix_edges = list(accepted_edges)
    if local_checks["passed"] and target is not None:
        prefix_nodes.append(target)
        prefix_edges.append(edge_id)
    return {
        "schema_version": "r4_stepwise_route_trace_v1",
        "route_representation": "directed_edge_slots",
        "attempted_move_sequence": "",
        "attempted_route_node_sequence": attempted_nodes,
        "attempted_route_edge_sequence": attempted_edges,
        "proposed_edge_pairs": pairs,
        "validated_prefix_node_sequence": prefix_nodes,
        "validated_prefix_edge_sequence": prefix_edges,
        "move_violations": violations,
        "step_check_records": records,
        "move_count": len(accepted_edges) + 1,
        "first_violation_step": (
            len(accepted_edges) + 1 if violations else None
        ),
    }


def run_r4_stepwise_route_search(
    client: Any,
    package: RouteInputPackage,
    road_map: RoadMap,
    task: VehicleOrderTask,
    selected_pickup_node_id: str,
    selected_dropoff_node_id: str,
    *,
    max_api_calls: int,
    max_route_actions: Optional[int] = None,
    blocked_edge_ids: Iterable[str] = (),
    on_attempt: Optional[Callable[[Dict[str, Any]], None]] = None,
    route_feature_context: Optional[Dict[str, Any]] = None,
    enable_validation_feedback: bool = True,
    compact_visited_state: bool = False,
    enable_progress_guidance: bool = False,
) -> Dict[str, Any]:
    """Request and locally validate one route action per bounded API call."""

    if isinstance(max_api_calls, bool) or not 1 <= int(max_api_calls) <= 4096:
        raise ValueError("r4_stepwise_api_call_budget_must_be_between_1_and_4096")
    split_route_budget = max_route_actions is not None
    route_action_budget = (
        int(max_route_actions) if split_route_budget else int(max_api_calls)
    )
    if isinstance(max_route_actions, bool) or not 1 <= route_action_budget <= 4096:
        raise ValueError("r4_stepwise_route_action_budget_must_be_between_1_and_4096")
    if task.start_node_id == selected_dropoff_node_id:
        raise ValueError("r4_stepwise_start_equals_dropoff")
    full_constraint = build_r4_route_output_constraint(
        package,
        selected_pickup_node_id,
        selected_dropoff_node_id,
        allow_failed=False,
    )
    if getattr(client, "route_constraint", None) is None:
        raise RuntimeError("r4_stepwise_strict_tool_client_required")
    if int(getattr(client, "retries", 0) or 0) != 0:
        raise RuntimeError("r4_stepwise_network_retries_must_be_zero")

    node_by_id = {node.node_id: node for node in package.nodes}
    edge_by_pair = {(edge.from_node, edge.to_node): edge for edge in package.edges}
    blocked = set(blocked_edge_ids) | set(package.blocked_edge_ids)
    current = task.start_node_id
    visited = [current]
    pickup_seen = current == selected_pickup_node_id
    accepted_directions: List[str] = []
    accepted_edges: List[str] = []
    accepted_nodes = [current]
    attempts: List[Dict[str, Any]] = []
    step_records: List[Dict[str, Any]] = []
    final_status = "step_call_budget_exhausted"
    final_route = None
    final_validation = None
    total_input_bytes = 0
    feedback_for_next_request = None
    input_protocol = (
        "r4-step-v3"
        if compact_visited_state or enable_progress_guidance
        else "r4-step-v2"
    )

    def snapshot() -> Dict[str, Any]:
        usage_complete = all(
            "input_tokens" in item and "output_tokens" in item
            for item in attempts
        )
        return {
            "schema_version": "r4_stepwise_search_result_v1",
            "route_input_protocol": input_protocol,
            "status": final_status,
            "api_request_count": len(attempts),
            "api_call_limit": int(max_api_calls),
            "route_action_budget": route_action_budget,
            "route_action_budget_mode": (
                "separate_from_request_limit" if split_route_budget else "legacy_shared_limit"
            ),
            "validation_feedback_enabled": enable_validation_feedback,
            "attempts": attempts,
            "steps": step_records,
            "route": final_route,
            "replay": attempts[-1].get("replay") if attempts else None,
            "validation": final_validation,
            "usage": {
                "llm_calls": len(attempts),
                "usage_complete": usage_complete,
                "input_tokens": (
                    sum(int(item.get("input_tokens", 0) or 0) for item in attempts)
                    if usage_complete
                    else None
                ),
                "output_tokens": (
                    sum(int(item.get("output_tokens", 0) or 0) for item in attempts)
                    if usage_complete
                    else None
                ),
                "llm_latency_ms": (
                    round(sum(float(item.get("latency_ms", 0.0) or 0.0) for item in attempts), 3)
                    if all("latency_ms" in item for item in attempts)
                    else None
                ),
                "client_retries": sum(int(item.get("retries", 0) or 0) for item in attempts),
                "serialized_user_input_bytes": total_input_bytes,
                "serialized_messages_bytes": sum(int(item.get("messages_bytes", 0) or 0) for item in attempts),
            },
        }

    for call_number in range(1, int(max_api_calls) + 1):
        remaining_action_budget = (
            route_action_budget - len(accepted_edges)
            if split_route_budget
            else int(max_api_calls) - call_number + 1
        )
        constraint = build_r4_step_output_constraint(
            package,
            full_constraint,
            current_node_id=current,
            pickup_seen=pickup_seen,
            visited_node_ids=visited,
            blocked_edge_ids=blocked,
            remaining_action_budget=remaining_action_budget,
            include_objective_cost_guidance=enable_progress_guidance,
        )
        client.route_constraint = constraint
        progress_guidance = None
        if enable_progress_guidance:
            progress_guidance = {
                "objective": "travel_time_then_distance",
                "current_lower_bound": constraint.remaining_objective_cost_lower_bound,
                "by_direction": {
                    direction: constraint.objective_cost_lower_bound_by_direction[direction]
                    for direction in _DIRECTION_ORDER
                    if direction in constraint.objective_cost_lower_bound_by_direction
                },
            }
        messages = build_r4_stepwise_messages(
            package,
            task.start_node_id,
            selected_pickup_node_id,
            selected_dropoff_node_id,
            current_node_id=current,
            visited_node_ids=visited,
            pickup_seen=pickup_seen,
            accepted_directions=accepted_directions,
            feedback=feedback_for_next_request,
            remaining_action_budget=remaining_action_budget,
            route_feature_context=route_feature_context,
            compact_visited_state=compact_visited_state,
            progress_guidance=progress_guidance,
        )
        feedback_for_next_request = None
        user_bytes = len(messages[-1]["content"].encode("utf-8"))
        messages_bytes = sum(
            len(message["content"].encode("utf-8")) for message in messages
        )
        request_payload = json.loads(messages[-1]["content"])
        input_user_sha256 = hashlib.sha256(
            messages[-1]["content"].encode("utf-8")
        ).hexdigest()
        total_input_bytes += user_bytes
        try:
            response = client.chat(messages)
        except Exception as error:
            failed_attempt = {
                "attempt": call_number,
                "step": len(accepted_edges) + 1,
                "status": "api_error",
                "error": _safe_error(error, client),
                "input_bytes": user_bytes,
                "messages_bytes": messages_bytes,
                "input_user_sha256": input_user_sha256,
                "request_state": request_payload["s"],
                "request_feedback": request_payload.get("f"),
                "allowed_directions": [
                    direction for direction in _DIRECTION_ORDER
                    if direction in constraint.edge_by_direction
                ],
                "filtered_directions": dict(constraint.filtered_directions),
                "remaining_minimum_moves": constraint.remaining_minimum_moves,
                "remaining_action_budget": remaining_action_budget,
                "route_input_protocol": input_protocol,
                "route_progress_guidance": progress_guidance,
                "constraint_schema_sha256": constraint.schema_sha256,
                "constraint_schema_bytes": constraint.schema_bytes,
            }
            provider_audit = getattr(client, "last_record", None)
            if isinstance(provider_audit, dict) and provider_audit.get("backend"):
                failed_attempt["provider_audit"] = provider_audit
                for field in (
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "latency_ms",
                    "retry_count",
                ):
                    value = provider_audit.get(field)
                    if value is not None:
                        target = "retries" if field == "retry_count" else field
                        failed_attempt[target] = value
                if provider_audit.get("trace_jsonl_path"):
                    failed_attempt["request_id"] = provider_audit["trace_jsonl_path"]
            attempts.append(failed_attempt)
            final_status = "api_error"
            if on_attempt is not None:
                on_attempt(snapshot())
            break

        attempt: Dict[str, Any] = {
            "attempt": call_number,
            "step": len(accepted_edges) + 1,
            "status": "response_received",
            "input_bytes": user_bytes,
            "messages_bytes": messages_bytes,
            "input_user_sha256": input_user_sha256,
            "request_state": request_payload["s"],
            "request_feedback": request_payload.get("f"),
            "allowed_directions": [
                direction for direction in _DIRECTION_ORDER
                if direction in constraint.edge_by_direction
            ],
            "filtered_directions": dict(constraint.filtered_directions),
            "remaining_minimum_moves": constraint.remaining_minimum_moves,
            "remaining_action_budget": remaining_action_budget,
            "route_input_protocol": input_protocol,
            "route_progress_guidance": progress_guidance,
            "model": str(getattr(response, "model", "")),
            "request_id": getattr(response, "request_id", None),
            "finish_reason": str(getattr(response, "finish_reason", "") or ""),
            "input_tokens": int(getattr(response, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(response, "output_tokens", 0) or 0),
            "latency_ms": float(getattr(response, "latency_ms", 0.0) or 0.0),
            "retries": int(getattr(response, "retries", 0) or 0),
            "output_constraint_mode": getattr(response, "output_constraint_mode", None),
            "constraint_schema_sha256": constraint.schema_sha256,
            "constraint_schema_bytes": constraint.schema_bytes,
            "raw_output": str(getattr(response, "content", "")),
        }
        provider_audit = getattr(response, "provider_audit", None)
        if isinstance(provider_audit, dict):
            attempt["provider_audit"] = provider_audit
        attempts.append(attempt)
        try:
            try:
                payload = json.loads(attempt["raw_output"])
            except json.JSONDecodeError as error:
                raise ValueError("r4_step_output_json_invalid") from error
            if not isinstance(payload, dict):
                raise ValueError("r4_step_output_not_object")
            action = payload.get("action")
            direction = payload.get("direction", "")
            self_check = payload.get("self_check", {})
            if not isinstance(self_check, dict) or any(
                not isinstance(self_check.get(key), bool) for key in _SELF_CHECK_KEYS
            ):
                raise ValueError("r4_step_self_check_invalid")
            model_self_check = {key: self_check[key] for key in _SELF_CHECK_KEYS}
            if action == "give_up":
                if (
                    direction != ""
                    or payload.get("failure_reason_code") != "unable_to_construct_route"
                    or set(payload) != {"action", "direction", "self_check", "failure_reason_code"}
                ):
                    raise ValueError("r4_step_give_up_payload_invalid")
                remaining_minimum_moves = _minimum_remaining_move_lower_bound(
                    package.edges,
                    current,
                    selected_pickup_node_id,
                    selected_dropoff_node_id,
                    pickup_seen,
                    visited,
                    blocked,
                )
                remaining_route_reachable = remaining_minimum_moves is not None
                remaining_budget_insufficient = (
                    remaining_minimum_moves is not None
                    and remaining_minimum_moves > remaining_action_budget
                )
                attempt.update(
                    {
                        "parsed_output": payload,
                        "remaining_route_reachable": remaining_route_reachable,
                        "remaining_minimum_moves": remaining_minimum_moves,
                    }
                )
                if (
                    remaining_route_reachable
                    and not remaining_budget_insufficient
                    and constraint.edge_by_direction
                ):
                    feedback_code = "give_up_while_route_reachable"
                    attempt.update(
                        {
                            "status": "step_invalid",
                            "local_checks": {
                                "passed": False,
                                "errors": [feedback_code],
                                "remaining_route_reachable": True,
                                "remaining_minimum_moves": remaining_minimum_moves,
                                "remaining_action_budget": remaining_action_budget,
                            },
                            "feedback": {"code": feedback_code},
                        }
                    )
                    if enable_validation_feedback:
                        feedback_for_next_request = dict(attempt["feedback"])
                        final_status = "step_call_budget_exhausted"
                    else:
                        final_status = "validation_rejected_without_feedback"
                else:
                    termination_reason = (
                        "remaining_route_unreachable"
                        if not remaining_route_reachable
                        else "remaining_action_budget_insufficient"
                        if remaining_budget_insufficient
                        else "no_safe_continuation"
                    )
                    attempt.update(
                        {
                            "status": "model_gave_up",
                            "termination_reason": termination_reason,
                            "local_checks": {
                                "passed": False,
                                "errors": [termination_reason],
                                "remaining_route_reachable": remaining_route_reachable,
                                "remaining_minimum_moves": remaining_minimum_moves,
                                "remaining_action_budget": remaining_action_budget,
                            },
                        }
                    )
                    final_status = "model_gave_up"
                step_records.append(
                    {
                        "api_call": call_number,
                        "step": len(accepted_edges) + 1,
                        "action": "give_up",
                        "accepted_step": False,
                        "model_self_check": model_self_check,
                        "local_checks": attempt["local_checks"],
                        "feedback": attempt.get("feedback"),
                    }
                )
                attempt["replay"] = {
                    "schema_version": "r4_stepwise_route_trace_v1",
                    "route_representation": "directed_edge_slots",
                    "attempted_route_node_sequence": list(accepted_nodes),
                    "attempted_route_edge_sequence": list(accepted_edges),
                    "proposed_edge_pairs": [
                        {
                            "step": index + 1,
                            "from_node_id": accepted_nodes[index],
                            "to_node_id": accepted_nodes[index + 1],
                            "edge_id": accepted_edges[index],
                        }
                        for index in range(len(accepted_edges))
                    ],
                    "validated_prefix_node_sequence": list(accepted_nodes),
                    "validated_prefix_edge_sequence": list(accepted_edges),
                    "move_violations": [],
                    "step_check_records": [],
                    "move_count": len(accepted_edges),
                    "first_violation_step": None,
                }
            elif action == "move":
                if (
                    direction not in _DIRECTION_ORDER
                    or set(payload) != {"action", "direction", "self_check", "failure_reason_code"}
                ):
                    raise ValueError("r4_step_direction_invalid")
                local = _local_check(
                    direction,
                    current,
                    pickup_seen,
                    visited,
                    node_by_id,
                    edge_by_pair,
                    blocked,
                    full_constraint,
                    remaining_action_budget=remaining_action_budget,
                )
                local["flags"]["model_self_check_passed"] = all(model_self_check.values())
                model_self_check_mismatch_fields = _model_self_check_mismatch_fields(
                    model_self_check, local["flags"]
                )
                model_failure_code_overridden = (
                    payload.get("failure_reason_code") != "none"
                    and local["passed"] is True
                )
                attempt.update(
                    {
                        "parsed_output": payload,
                        "action": action,
                        "direction": direction,
                        "edge_slot_code": next(
                            (
                                code for code, candidate in full_constraint.edge_by_slot_code.items()
                                if candidate.from_node == current
                                and candidate.to_node == local["expected_target_node_id"]
                            ),
                            None,
                        ),
                        "model_self_check": model_self_check,
                        "model_self_check_mismatch_fields": model_self_check_mismatch_fields,
                        "model_failure_code_overridden_by_local_validation": model_failure_code_overridden,
                        "local_checks": {
                            "passed": local["passed"],
                            "flags": local["flags"],
                            "errors": local["errors"],
                            "remaining_minimum_moves_after_move": local[
                                "remaining_minimum_moves_after_move"
                            ],
                            "remaining_action_budget_after_move": max(
                                remaining_action_budget - 1, 0
                            ),
                        },
                        "replay": _attempt_replay(
                            accepted_nodes,
                            accepted_edges,
                            local["edge"],
                            direction,
                            current,
                            local,
                            model_self_check,
                        ),
                    }
                )
                if not local["passed"]:
                    attempt["status"] = "step_invalid"
                    feedback_code = local["errors"][0]
                    attempt["feedback"] = {
                        "code": feedback_code,
                        "direction": direction,
                    }
                    feedback_for_next_request = (
                        dict(attempt["feedback"]) if enable_validation_feedback else None
                    )
                    step_records.append(
                        {
                            "api_call": call_number,
                            "step": len(accepted_edges) + 1,
                            "action": action,
                            "direction": direction,
                            "accepted_step": False,
                            "model_self_check": model_self_check,
                            "model_self_check_mismatch_fields": model_self_check_mismatch_fields,
                            "model_failure_code_overridden_by_local_validation": model_failure_code_overridden,
                            "local_checks": attempt["local_checks"],
                            "feedback": attempt["feedback"],
                        }
                    )
                    final_status = (
                        "step_call_budget_exhausted"
                        if enable_validation_feedback
                        else "validation_rejected_without_feedback"
                    )
                else:
                    edge = local["edge"]
                    accepted_edges.append(edge.edge_id)
                    accepted_directions.append(direction)
                    current = edge.to_node
                    accepted_nodes.append(current)
                    visited.append(current)
                    pickup_seen = pickup_seen or current == selected_pickup_node_id
                    feedback_for_next_request = None
                    attempt["status"] = "step_accepted"
                    attempt["remaining_minimum_moves_after_move"] = local[
                        "remaining_minimum_moves_after_move"
                    ]
                    step_records.append(
                        {
                            "api_call": call_number,
                            "step": len(accepted_edges),
                            "action": action,
                            "direction": direction,
                            "from_node_id": edge.from_node,
                            "to_node_id": edge.to_node,
                            "accepted_step": True,
                            "model_self_check": model_self_check,
                            "model_self_check_mismatch_fields": model_self_check_mismatch_fields,
                            "model_failure_code_overridden_by_local_validation": model_failure_code_overridden,
                            "local_checks": attempt["local_checks"],
                            "remaining_minimum_moves_after_move": local[
                                "remaining_minimum_moves_after_move"
                            ],
                        }
                    )
                    if current == selected_dropoff_node_id and pickup_seen:
                        candidate = RouteCandidate(
                            route_id="r4-stepwise-" + task.task_id,
                            node_sequence=list(accepted_nodes),
                            edge_sequence=list(accepted_edges),
                            route_source="r4_stepwise_llm",
                            route_reachability=True,
                        )
                        validation = validate_llm_route(
                            road_map,
                            task,
                            candidate,
                            selected_pickup_node_id,
                            selected_dropoff_node_id,
                            blocked,
                        )
                        final_validation = validation.to_dict()
                        candidate.validation_status = validation.status
                        candidate.validation_errors = list(validation.errors)
                        if validation.valid:
                            final_route = candidate.to_dict()
                            final_status = "route_validated"
                        else:
                            final_status = "final_validation_failed"
                            attempt["final_validation"] = final_validation
                    else:
                        final_status = "step_call_budget_exhausted"
            else:
                raise ValueError("r4_step_action_invalid")
        except (json.JSONDecodeError, ValueError) as error:
            feedback_for_next_request = {
                "code": str(error).split(":", 1)[0],
            } if enable_validation_feedback else None
            attempt.update(
                {
                    "status": "step_output_invalid",
                    "parse_error": _safe_error(error, client),
                    "feedback": {"code": str(error).split(":", 1)[0]},
                }
            )
            step_records.append(
                {
                    "api_call": call_number,
                    "step": len(accepted_edges) + 1,
                    "action": "invalid_output",
                    "accepted_step": False,
                    "local_checks": {"passed": False, "errors": [attempt["feedback"]["code"]]},
                    "feedback": attempt["feedback"],
                }
            )
            final_status = (
                "step_call_budget_exhausted"
                if enable_validation_feedback
                else "validation_rejected_without_feedback"
            )

        if final_status in {
            "route_validated",
            "model_gave_up",
            "api_error",
            "final_validation_failed",
            "validation_rejected_without_feedback",
        }:
            if on_attempt is not None:
                on_attempt(snapshot())
            break
        if on_attempt is not None:
            on_attempt(snapshot())

    result = snapshot()
    if (
        split_route_budget
        and len(attempts) >= int(max_api_calls)
        and len(accepted_edges) < route_action_budget
        and final_status == "step_call_budget_exhausted"
    ):
        result["status"] = "request_limit_exhausted"
    return result
