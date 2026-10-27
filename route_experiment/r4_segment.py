"""Bounded R4 route segments with atomic deterministic validation.

The model proposes at most ten structured moves per request.  Local code
replays every edge against the frozen directed map and route state.  A segment
is committed only when every move is legal and stays inside the frozen route
progress budget; no classical route is substituted for a rejected proposal.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import RoadMap, RouteInputPackage
from .r4_stepwise import (
    _DIRECTION_DELTAS,
    _DIRECTION_ORDER,
    _minimum_remaining_move_lower_bound,
    build_r4_stepwise_messages,
)
from .routing import edge_travel_time_sec


_SELF_CHECK_KEYS = (
    "allowed_directed_edge",
    "starts_at_current",
    "inside_boundary",
    "pickup_order_ok",
    "target_node_is_new",
)


@dataclass(frozen=True)
class R4SegmentConstraint:
    schema: Dict[str, Any]
    schema_sha256: str
    schema_bytes: int


def build_r4_segment_schema(
    max_segment_actions: int = 10,
    min_segment_actions: int = 0,
) -> Dict[str, Any]:
    maximum = int(max_segment_actions)
    if not 1 <= maximum <= 10:
        raise ValueError("r4_segment_action_limit_must_be_between_1_and_10")
    minimum = int(min_segment_actions)
    if not 0 <= minimum <= maximum:
        raise ValueError("r4_segment_minimum_action_limit_invalid")
    self_check = {
        "type": "object",
        "properties": {
            key: {"type": "boolean"}
            for key in _SELF_CHECK_KEYS
        },
        "required": list(_SELF_CHECK_KEYS),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "minItems": minimum,
                "maxItems": maximum,
                "items": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": list(_DIRECTION_ORDER)},
                        "self_check": self_check,
                    },
                    "required": ["direction", "self_check"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["actions"],
        "additionalProperties": False,
    }


def make_r4_segment_constraint(
    max_segment_actions: int = 10,
    min_segment_actions: int = 0,
) -> R4SegmentConstraint:
    schema = build_r4_segment_schema(max_segment_actions, min_segment_actions)
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return R4SegmentConstraint(
        schema=schema,
        schema_sha256=hashlib.sha256(encoded).hexdigest(),
        schema_bytes=len(encoded),
    )


def build_r4_segment_messages(
    package: RouteInputPackage,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    *,
    current_node_id: str,
    visited_node_ids: Iterable[str],
    accepted_directions: Iterable[str],
    pickup_seen: bool,
    max_segment_actions: int,
    min_segment_actions: int = 0,
    remaining_action_budget: int,
    accepted_action_count: int,
    progress_limit: int,
    route_feature_context: Optional[Dict[str, Any]] = None,
    feedback: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Build a compact-map segment snapshot for an isolated or resumed session."""

    constraint = build_r4_segment_schema(max_segment_actions, min_segment_actions)
    visited = list(visited_node_ids)
    remaining = _minimum_remaining_move_lower_bound(
        package.edges,
        current_node_id,
        pickup_node_id,
        dropoff_node_id,
        pickup_seen,
        visited,
        package.blocked_edge_ids,
    )
    if remaining is None:
        raise ValueError("r4_segment_current_state_has_no_remaining_route")
    guidance = {
        "objective": "travel_time_then_distance",
        "remaining_moves_lower_bound": remaining,
        "accepted_actions": int(accepted_action_count),
        "progress_limit": int(progress_limit),
        "progress_lower_bound": int(accepted_action_count) + int(remaining),
    }
    messages = build_r4_stepwise_messages(
        package,
        start_node_id,
        pickup_node_id,
        dropoff_node_id,
        current_node_id=current_node_id,
        visited_node_ids=visited,
        pickup_seen=pickup_seen,
        accepted_directions=list(accepted_directions),
        remaining_action_budget=max(1, int(remaining_action_budget)),
        route_feature_context=route_feature_context,
        compact_visited_state=True,
        progress_guidance=guidance,
    )
    payload = json.loads(messages[1]["content"])
    payload["v"] = "r4-segment-v5"
    payload["seg"] = {
        "max_actions": int(max_segment_actions),
        "min_actions": int(min_segment_actions),
        "accepted_actions": int(accepted_action_count),
        "remaining_action_budget": int(remaining_action_budget),
        "progress_limit": int(progress_limit),
    }
    if feedback is not None:
        payload["f"] = feedback
    messages[0] = {
        "role": "system",
        "content": (
            "你是单车路线检索器。只依据消息中的紧凑有向道路网和固定任务端点工作。"
            "地图编码定义：m.x0、m.y0 是左下坐标原点，m.w、m.h 是宽和高；m.g 有 m.h 行，按 y 从小到大排列；"
            "每行按 x 从小到大排列 m.w 个节点，每节点严格占连续4个字符，槽位依次是 E、W、N、S。"
            "对 node_x_y，先取 row=y-m.y0、cell=x-m.x0，再读取该行从 4*cell 开始的4个字符。"
            "槽位 '.' 表示没有这条有向边；其他单字符码表示该方向确有出边，成本在 m.c[码] 中，格式为[行驶秒数,米数]。"
            "方向位移固定为 E=(+1,0)、W=(-1,0)、N=(0,+1)、S=(0,-1)。只读取当前节点对应槽位；绝不能推断反向边。"
            "节点编号是 node_x_y，坐标边界为 x=m.x0..m.x0+m.w-1、y=m.y0..m.y0+m.h-1。"
            "s.vf 为 grid_slot_base36 时，s.v 是已访问坐标索引串；按 s.vl 个字符切块，每块用大写 base36 解码为 k，"
            "再算 x=m.x0+(k mod m.w)、y=m.y0+floor(k/m.w)，对应 node_x_y。q.s 与 s.n 单独给出，也都视为已访问。"
            "根据 s.n、s.p、s.v，从当前节点提出最多 seg.max_actions 个连续动作。"
            "输出 JSON 的 actions 数组；每项只含 direction 和 self_check。每一步都要按当前节点逐项检查："
            "该方向是否是实际允许的有向边、起点是否为当前节点、目标是否在地图边界内、"
            "是否满足先上车后下车、目标节点是否未访问；各项确认后才把对应布尔值填为 true。"
            "每次移动后都必须把当前节点更新为到达节点，再用新坐标重新定位 m.g 行和单元格后读取出边；"
            "不能因为上一步方向合法就重复使用该方向。"
            "不允许重复节点、越界、逆单行、先到下车点或下车后继续移动。"
            "整段还必须满足 seg.progress_limit 路线进度预算；r 是程序计算的乐观剩余步数下界，"
            "用于避免绕行，不是替代路线或路线答案。i 若存在，是已校验的 RoadFeatureInputProtocol v1 特征/意图，"
            "只能帮助理解固定 q 端点，不能改变端点或证明某条边存在。"
            "不要输出长篇自由文本推理。外部确定性程序会逐边复核整个动作段；只有整段全部通过才会原子接纳，"
            "否则整段不推进。若输入含 f，上一整段已被原子拒绝；必须从 s.n 和原访问状态重新生成整段。"
            "f.allowed_directions_at_failure_state 是校验器计算的局部合法出边；它不保证仍能到达上下车点，也不保证满足路线进度预算。"
            "若 f.progress_safe_directions_at_failure_state 存在且上次合法前缀完全相同，失败位置的下一步只能从该列表选择；"
            "该列表已由程序确认仍有未访问的接送路线并满足进度上限。若列表为空，必须改写更早的动作前缀。"
            "f.recent_rejected_prefix_constraints 是同一已接纳路线状态下此前被拒绝的前缀约束；"
            "每项 prefix_directions 到达 failure_node 后，下一步只能从 safe_next_directions 选择，不能重用 failed_direction。"
            "若 safe_next_directions 为空，必须在该前缀更早处改道。不得重复列表中的失败前缀。"
            "本次前缀不同，仍须按当前节点重新检查 m.g 和剩余路线下界。不要调用工具、传统算法或外部路线服务。"
        ),
    }
    messages[1] = {
        "role": "user",
        "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }
    return messages


def validate_r4_segment_proposal(
    road_map: RoadMap,
    *,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    current_node_id: str,
    pickup_seen: bool,
    visited_node_ids: Sequence[str],
    payload: Any,
    accepted_action_count: int,
    progress_limit: int,
    max_segment_actions: int = 10,
    min_segment_actions: int = 0,
    blocked_edge_ids: Iterable[str] = (),
    allowed_edge_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Validate a proposal against a temporary state; commit is all-or-nothing."""

    node_by_id = {node.node_id: node for node in road_map.nodes}
    node_by_xy = {(node.x, node.y): node for node in road_map.nodes}
    edge_by_pair = {(edge.from_node, edge.to_node): edge for edge in road_map.edges}
    blocked = set(blocked_edge_ids)
    allowed = set(allowed_edge_ids) if allowed_edge_ids is not None else None
    visited = list(visited_node_ids)
    if (
        current_node_id not in node_by_id
        or pickup_node_id not in node_by_id
        or dropoff_node_id not in node_by_id
        or not visited
        or visited[0] != start_node_id
        or visited[-1] != current_node_id
        or len(visited) != len(set(visited))
        or any(node_id not in node_by_id for node_id in visited)
        or pickup_seen != (pickup_node_id in visited)
    ):
        return _segment_rejection("segment_state_invalid")

    if not isinstance(payload, dict) or set(payload) != {"actions"}:
        return _segment_rejection("segment_payload_shape_invalid")
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return _segment_rejection("segment_actions_not_array")
    if len(actions) < int(min_segment_actions):
        result = _segment_rejection("segment_too_short")
        result["violations"][0].update({
            "provided_actions": len(actions),
            "required_actions": int(min_segment_actions),
        })
        return result
    if len(actions) > int(max_segment_actions):
        return _segment_rejection("segment_too_long")
    if not actions:
        return {
            "schema_version": "r4_segment_validation_v2",
            "accepted": False,
            "status": "model_gave_up",
            "route_complete": False,
            "violations": [],
            "first_violation_step": None,
            "steps": [],
            "committed_node_sequence": [],
            "committed_edge_sequence": [],
            "committed_direction_sequence": [],
            "attempted_node_sequence": [current_node_id],
            "attempted_edge_sequence": [],
        }

    min_x = int(road_map.boundary.get("min_x", min(node.x for node in road_map.nodes)))
    max_x = int(road_map.boundary.get("max_x", max(node.x for node in road_map.nodes)))
    min_y = int(road_map.boundary.get("min_y", min(node.y for node in road_map.nodes)))
    max_y = int(road_map.boundary.get("max_y", max(node.y for node in road_map.nodes)))
    trial_current = current_node_id
    trial_pickup_seen = bool(pickup_seen)
    trial_visited = set(visited)
    attempted_nodes: List[str] = []
    attempted_edges: List[str] = []
    attempted_directions: List[str] = []
    steps: List[Dict[str, Any]] = []
    violations: List[Dict[str, Any]] = []

    def reject(kind: str, step_index: int, **detail: Any) -> None:
        violations.append({"step": step_index, "kind": kind, **detail})

    for step_index, action in enumerate(actions, 1):
        if not isinstance(action, dict) or set(action) != {"direction", "self_check"}:
            reject("segment_action_shape_invalid", step_index)
            break
        direction = action.get("direction")
        self_check = action.get("self_check")
        if direction not in _DIRECTION_DELTAS:
            reject("invalid_direction", step_index, direction=direction)
            break
        if (
            not isinstance(self_check, dict)
            or set(self_check) != set(_SELF_CHECK_KEYS)
            or any(not isinstance(self_check.get(key), bool) for key in _SELF_CHECK_KEYS)
        ):
            reject("self_check_shape_invalid", step_index, direction=direction)
            break

        current_node = node_by_id[trial_current]
        dx, dy = _DIRECTION_DELTAS[direction]
        target_xy = (current_node.x + dx, current_node.y + dy)
        target = node_by_xy.get(target_xy)
        within_boundary = min_x <= target_xy[0] <= max_x and min_y <= target_xy[1] <= max_y
        edge = edge_by_pair.get((trial_current, target.node_id)) if target else None
        if edge is not None and allowed is not None and edge.edge_id not in allowed:
            edge = None
        not_blocked = bool(edge and edge.edge_id not in blocked)
        pickup_order_ok = bool(
            target
            and trial_current != dropoff_node_id
            and not (target.node_id == dropoff_node_id and not trial_pickup_seen)
        )
        target_is_new = bool(target and target.node_id not in trial_visited)
        local_flags = {
            "allowed_directed_edge": bool(
                edge and within_boundary and not_blocked and pickup_order_ok and target_is_new
            ),
            "starts_at_current": bool(edge and edge.from_node == trial_current),
            "inside_boundary": bool(target and within_boundary),
            "pickup_order_ok": pickup_order_ok,
            "target_node_is_new": target_is_new,
        }
        mismatches = [key for key in _SELF_CHECK_KEYS if self_check[key] != local_flags[key]]
        step_violations: List[str] = []
        diagnostic_kinds: List[str] = []
        if not within_boundary or target is None:
            step_violations.append("outside_boundary_or_missing_node")
        if edge is None:
            reverse = edge_by_pair.get((target.node_id, trial_current)) if target else None
            step_violations.append("one_way_direction" if reverse and reverse.one_way else "directed_edge_missing")
        elif not not_blocked:
            step_violations.append("blocked_edge")
        if target and not pickup_order_ok:
            step_violations.append(
                "move_after_dropoff" if trial_current == dropoff_node_id else "dropoff_before_pickup"
            )
        if target and not target_is_new:
            step_violations.append("node_revisited")
        if mismatches:
            # The model's self-check is evidence about its reasoning, not the
            # source of route legality. Record disagreement, but let the local
            # deterministic checks decide whether the proposed move is safe.
            diagnostic_kinds.append("model_self_check_mismatch")

        if target and target.node_id == dropoff_node_id and step_index != len(actions):
            step_violations.append("move_after_dropoff")

        if not step_violations and edge is not None and target is not None:
            next_pickup_seen = trial_pickup_seen or target.node_id == pickup_node_id
            next_visited = trial_visited | {target.node_id}
            remaining = _minimum_remaining_move_lower_bound(
                road_map.edges,
                target.node_id,
                pickup_node_id,
                dropoff_node_id,
                next_pickup_seen,
                next_visited,
                blocked,
            )
            if remaining is None:
                step_violations.append("remaining_route_unreachable")
            else:
                progress_lower_bound = int(accepted_action_count) + step_index + int(remaining)
                if progress_lower_bound > int(progress_limit):
                    step_violations.append("progress_budget_exceeded")

        steps.append(
            {
                "step": step_index,
                "from_node_id": trial_current,
                "to_node_id": target.node_id if target else None,
                "direction": direction,
                "edge_id": edge.edge_id if edge else None,
                "model_self_check": dict(self_check),
                "local_check_flags": local_flags,
                "self_check_mismatch_fields": mismatches,
                "diagnostic_kinds": diagnostic_kinds,
                "violation_kinds": list(step_violations),
                "accepted_in_temporary_state": not step_violations,
            }
        )
        if step_violations:
            for kind in step_violations:
                reject(
                    kind,
                    step_index,
                    from_node_id=trial_current,
                    to_node_id=target.node_id if target else None,
                    direction=direction,
                    edge_id=edge.edge_id if edge else None,
                )
            break

        attempted_nodes.append(target.node_id)
        attempted_edges.append(edge.edge_id)
        attempted_directions.append(direction)
        trial_current = target.node_id
        trial_visited.add(target.node_id)
        trial_pickup_seen = trial_pickup_seen or target.node_id == pickup_node_id

    accepted = not violations and len(steps) == len(actions)
    complete = accepted and trial_current == dropoff_node_id and trial_pickup_seen
    return {
        "schema_version": "r4_segment_validation_v2",
        "accepted": accepted,
        "status": "completed_route" if complete else "segment_accepted" if accepted else "segment_rejected",
        "route_complete": complete,
        "model_self_check_discrepancy_count": sum(
            1 for step in steps if step.get("self_check_mismatch_fields")
        ),
        "current_node_after_segment": trial_current,
        "pickup_seen_after_segment": trial_pickup_seen,
        "visited_after_segment": visited + attempted_nodes if accepted else list(visited),
        "violations": violations,
        "first_violation_step": violations[0].get("step") if violations else None,
        "steps": steps,
        "attempted_node_sequence": attempted_nodes,
        "attempted_edge_sequence": attempted_edges,
        "attempted_direction_sequence": attempted_directions,
        "committed_node_sequence": attempted_nodes if accepted else [],
        "committed_edge_sequence": attempted_edges if accepted else [],
        "committed_direction_sequence": attempted_directions if accepted else [],
    }


def _segment_rejection(kind: str) -> Dict[str, Any]:
    return {
        "schema_version": "r4_segment_validation_v2",
        "accepted": False,
        "status": "segment_rejected",
        "route_complete": False,
        "violations": [{"step": None, "kind": kind}],
        "first_violation_step": None,
        "steps": [],
        "attempted_node_sequence": [],
        "attempted_edge_sequence": [],
        "attempted_direction_sequence": [],
        "committed_node_sequence": [],
        "committed_edge_sequence": [],
        "committed_direction_sequence": [],
    }


def _rejection_prefix_constraint(feedback: Dict[str, Any]) -> Dict[str, Any]:
    """Keep one compact, deterministic constraint from a rejected prefix."""
    return {
        "prefix_directions": list(feedback.get("previous_valid_prefix_directions") or []),
        "failure_node": feedback.get("from_node_id"),
        "failed_direction": feedback.get("direction"),
        "safe_next_directions": list(
            feedback.get("progress_safe_directions_at_failure_state") or []
        ),
        "reason": feedback.get("code"),
    }


def _allowed_directions_at_node(
    road_map: RoadMap,
    current_node_id: str,
    visited_node_ids: Sequence[str],
    pickup_node_id: str,
    dropoff_node_id: str,
    pickup_seen: bool,
    blocked_edge_ids: Iterable[str],
) -> List[str]:
    """Return deterministic legal next directions for feedback context."""
    if current_node_id == dropoff_node_id:
        return []
    node_by_id = {node.node_id: node for node in road_map.nodes}
    source = node_by_id.get(current_node_id)
    if source is None:
        return []
    visited = set(visited_node_ids)
    blocked = set(blocked_edge_ids)
    edge_pairs = {
        (edge.from_node, edge.to_node)
        for edge in road_map.edges
        if edge.edge_id not in blocked
    }
    nodes_by_xy = {(node.x, node.y): node for node in road_map.nodes}
    allowed = []
    for direction in _DIRECTION_ORDER:
        dx, dy = _DIRECTION_DELTAS[direction]
        target = nodes_by_xy.get((source.x + dx, source.y + dy))
        if target is None or target.node_id in visited:
            continue
        if (current_node_id, target.node_id) not in edge_pairs:
            continue
        if target.node_id == dropoff_node_id and not pickup_seen:
            continue
        allowed.append(direction)
    return allowed


def _progress_safe_directions_at_node(
    road_map: RoadMap,
    *,
    current_node_id: str,
    visited_node_ids: Sequence[str],
    pickup_node_id: str,
    dropoff_node_id: str,
    pickup_seen: bool,
    blocked_edge_ids: Iterable[str],
    accepted_action_count: int,
    progress_limit: int,
) -> List[str]:
    """Filter locally legal moves to those retaining a feasible bounded route."""
    blocked = set(blocked_edge_ids)
    local = _allowed_directions_at_node(
        road_map,
        current_node_id,
        visited_node_ids,
        pickup_node_id,
        dropoff_node_id,
        pickup_seen,
        blocked,
    )
    node_by_id = {node.node_id: node for node in road_map.nodes}
    source = node_by_id.get(current_node_id)
    if source is None:
        return []
    node_by_xy = {(node.x, node.y): node for node in road_map.nodes}
    visited = set(visited_node_ids)
    direction_targets = {
        "E": (source.x + 1, source.y),
        "W": (source.x - 1, source.y),
        "N": (source.x, source.y + 1),
        "S": (source.x, source.y - 1),
    }
    viable = []
    for direction in local:
        target = node_by_xy.get(direction_targets[direction])
        if target is None or target.node_id in visited:
            continue
        next_pickup_seen = pickup_seen or target.node_id == pickup_node_id
        remaining = _minimum_remaining_move_lower_bound(
            road_map.edges,
            target.node_id,
            pickup_node_id,
            dropoff_node_id,
            next_pickup_seen,
            visited | {target.node_id},
            blocked,
        )
        if (
            remaining is not None
            and int(accepted_action_count) + 1 + int(remaining) <= int(progress_limit)
        ):
            viable.append(direction)
    return viable


def run_r4_segment_route_search(
    client: Any,
    package: RouteInputPackage,
    road_map: RoadMap,
    task: Any,
    selected_pickup_node_id: str,
    selected_dropoff_node_id: str,
    *,
    max_api_calls: int,
    max_segment_actions: int = 10,
    require_full_segments: bool = False,
    max_total_actions: int = 512,
    max_progress_multiplier: int = 2,
    max_feedback_requests: int = 3,
    blocked_edge_ids: Iterable[str] = (),
    route_feature_context: Optional[Dict[str, Any]] = None,
    on_request_start: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_attempt: Optional[Callable[[Dict[str, Any]], None]] = None,
    initial_route_state: Optional[Dict[str, Any]] = None,
    progress_limit_override: Optional[int] = None,
    initial_rejected_segments: int = 0,
    initial_feedback: Optional[Dict[str, Any]] = None,
    request_number_offset: int = 0,
) -> Dict[str, Any]:
    """Run the bounded segment protocol; never retries a network call."""

    if isinstance(max_api_calls, bool) or not 1 <= int(max_api_calls) <= 4096:
        raise ValueError("r4_segment_api_call_limit_invalid")
    if isinstance(max_feedback_requests, bool) or not 0 <= int(max_feedback_requests) <= int(max_total_actions):
        raise ValueError("r4_segment_feedback_limit_invalid")
    if isinstance(max_total_actions, bool) or not 1 <= int(max_total_actions) <= 4096:
        raise ValueError("r4_segment_total_action_limit_invalid")
    if isinstance(max_progress_multiplier, bool) or not 1 <= int(max_progress_multiplier) <= 10:
        raise ValueError("r4_segment_progress_multiplier_invalid")
    if isinstance(initial_rejected_segments, bool) or not 0 <= int(initial_rejected_segments) <= int(max_feedback_requests):
        raise ValueError("r4_segment_initial_rejection_count_invalid")
    if initial_feedback is not None and not isinstance(initial_feedback, dict):
        raise ValueError("r4_segment_initial_feedback_invalid")
    if isinstance(request_number_offset, bool) or int(request_number_offset) < 0:
        raise ValueError("r4_segment_request_number_offset_invalid")
    build_r4_segment_schema(max_segment_actions)
    if int(getattr(client, "retries", 0) or 0) != 0:
        raise RuntimeError("r4_segment_network_retries_must_be_zero")

    node_by_id = {node.node_id: node for node in road_map.nodes}
    package_node_by_id = {node.node_id: node for node in package.nodes}
    package_edge_by_id = {edge.edge_id: edge for edge in package.edges}
    map_edge_by_id = {edge.edge_id: edge for edge in road_map.edges}
    if package.boundary != road_map.boundary:
        raise ValueError("r4_segment_prompt_and_validator_boundaries_differ")
    for node_id, package_node in package_node_by_id.items():
        map_node = node_by_id.get(node_id)
        if map_node is None or (map_node.x, map_node.y) != (package_node.x, package_node.y):
            raise ValueError("r4_segment_prompt_and_validator_nodes_differ")
    for edge_id, package_edge in package_edge_by_id.items():
        map_edge = map_edge_by_id.get(edge_id)
        if map_edge is None or any(
            getattr(map_edge, field) != getattr(package_edge, field)
            for field in (
                "from_node", "to_node", "length_m", "base_speed_mps", "one_way",
                "congestion_level", "signal_delay_sec", "slope_rad",
            )
        ) or abs(edge_travel_time_sec(map_edge) - edge_travel_time_sec(package_edge)) > 1e-6:
            raise ValueError("r4_segment_prompt_and_validator_edges_differ")
    if initial_route_state is None:
        current = task.start_node_id
        visited = [current]
        pickup_seen = current == selected_pickup_node_id
        directions: List[str] = []
        edges: List[str] = []
    else:
        current = initial_route_state.get("current_node_id")
        visited = list(initial_route_state.get("visited_node_ids") or [])
        pickup_seen = initial_route_state.get("pickup_seen")
        directions = list(initial_route_state.get("direction_sequence") or [])
        edges = list(initial_route_state.get("edge_sequence") or [])
        if (
            not isinstance(current, str)
            or not isinstance(pickup_seen, bool)
            or not visited
            or visited[0] != task.start_node_id
            or visited[-1] != current
            or len(visited) != len(set(visited))
            or len(directions) != len(edges)
            or len(visited) != len(edges) + 1
            or any(node_id not in package_node_by_id for node_id in visited)
            or pickup_seen != (selected_pickup_node_id in visited)
            or (selected_dropoff_node_id in visited and visited[-1] != selected_dropoff_node_id)
        ):
            raise ValueError("r4_segment_initial_route_state_invalid")
        direction_by_delta = {delta: direction for direction, delta in _DIRECTION_DELTAS.items()}
        for index, edge_id in enumerate(edges):
            edge = package_edge_by_id.get(edge_id)
            if (
                edge is None
                or edge_id in set(blocked_edge_ids) | set(package.blocked_edge_ids)
                or edge.from_node != visited[index]
                or edge.to_node != visited[index + 1]
            ):
                raise ValueError("r4_segment_initial_route_edge_sequence_invalid")
            source = package_node_by_id[edge.from_node]
            target = package_node_by_id[edge.to_node]
            observed_direction = direction_by_delta.get((target.x - source.x, target.y - source.y))
            if observed_direction != directions[index]:
                raise ValueError("r4_segment_initial_route_direction_sequence_invalid")
        if selected_pickup_node_id in visited and selected_dropoff_node_id in visited:
            if visited.index(selected_pickup_node_id) > visited.index(selected_dropoff_node_id):
                raise ValueError("r4_segment_initial_route_pickup_order_invalid")
    blocked = set(blocked_edge_ids) | set(package.blocked_edge_ids)
    initial_lower_bound = _minimum_remaining_move_lower_bound(
        package.edges,
        current,
        selected_pickup_node_id,
        selected_dropoff_node_id,
        pickup_seen,
        visited,
        blocked,
    )
    if initial_lower_bound is None:
        raise ValueError("r4_segment_task_unreachable_at_start")
    computed_progress_limit = min(
        int(max_total_actions),
        int(initial_lower_bound) * int(max_progress_multiplier),
    )
    progress_limit = (
        int(progress_limit_override)
        if progress_limit_override is not None
        else computed_progress_limit
    )
    if (
        isinstance(progress_limit_override, bool)
        or progress_limit < len(directions)
        or progress_limit > int(max_total_actions)
    ):
        raise ValueError("r4_segment_progress_limit_override_invalid")
    if current == selected_dropoff_node_id and pickup_seen:
        progress_limit = 0
    remaining_action_budget_at_start = progress_limit - len(directions)
    request_upper_bound = (
        int(math.ceil(remaining_action_budget_at_start / float(max_segment_actions)))
        + int(max_feedback_requests)
        - int(initial_rejected_segments)
    )
    if int(max_api_calls) > request_upper_bound:
        raise ValueError("r4_segment_request_limit_exceeds_frozen_task_upper_bound")
    attempts: List[Dict[str, Any]] = []
    segment_audits: List[Dict[str, Any]] = []
    input_bytes = 0
    messages_bytes = 0
    rendered_cli_prompt_bytes_total = 0
    rendered_cli_prompt_sizes_known = True
    feedback = dict(initial_feedback) if initial_feedback is not None else None
    feedback_history = list(
        (feedback or {}).get("recent_rejected_prefix_constraints") or []
    )
    rejected_segments = int(initial_rejected_segments)
    final_status = "segment_request_budget_exhausted"

    for local_request_number in range(1, int(max_api_calls) + 1):
        request_number = int(request_number_offset) + local_request_number
        if current == selected_dropoff_node_id and pickup_seen:
            final_status = "completed_route"
            break
        remaining_action_budget = progress_limit - len(directions)
        if remaining_action_budget <= 0:
            final_status = "route_progress_budget_exhausted"
            break
        remaining_lower_bound = _minimum_remaining_move_lower_bound(
            package.edges,
            current,
            selected_pickup_node_id,
            selected_dropoff_node_id,
            pickup_seen,
            visited,
            blocked,
        )
        if remaining_lower_bound is None:
            final_status = "remaining_route_unreachable"
            break
        effective_max_segment_actions = min(max_segment_actions, remaining_action_budget)
        if require_full_segments:
            # Require a complete K-step segment, or the exact shortest remaining
            # suffix when fewer than K moves remain. This bounds calls by the
            # progress budget while making the final response terminate at the
            # requested destination.
            effective_max_segment_actions = min(
                effective_max_segment_actions, remaining_lower_bound
            )
        minimum_segment_actions = effective_max_segment_actions if require_full_segments else 0
        constraint = make_r4_segment_constraint(
            effective_max_segment_actions,
            minimum_segment_actions,
        )
        client.route_constraint = constraint
        messages = build_r4_segment_messages(
            package,
            task.start_node_id,
            selected_pickup_node_id,
            selected_dropoff_node_id,
            current_node_id=current,
            visited_node_ids=visited,
            accepted_directions=directions,
            pickup_seen=pickup_seen,
            max_segment_actions=effective_max_segment_actions,
            min_segment_actions=minimum_segment_actions,
            remaining_action_budget=remaining_action_budget,
            accepted_action_count=len(directions),
            progress_limit=progress_limit,
            route_feature_context=route_feature_context,
            feedback=feedback,
        )
        feedback_snapshot = dict(feedback) if feedback is not None else None
        feedback = None
        user_size = len(messages[-1]["content"].encode("utf-8"))
        all_messages_size = sum(len(message["content"].encode("utf-8")) for message in messages)
        prompt_renderer = getattr(client, "render_request_prompt", None)
        rendered_prompt = prompt_renderer(messages) if callable(prompt_renderer) else None
        rendered_prompt_bytes = (
            len(rendered_prompt.encode("utf-8"))
            if isinstance(rendered_prompt, str)
            else None
        )
        rendered_prompt_sha256 = (
            hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest()
            if isinstance(rendered_prompt, str)
            else None
        )
        input_bytes += user_size
        messages_bytes += all_messages_size
        if rendered_prompt_bytes is None:
            rendered_cli_prompt_sizes_known = False
        else:
            rendered_cli_prompt_bytes_total += rendered_prompt_bytes
        request_start = {
            "request_number": request_number,
            "current_node_id": current,
            "pickup_seen": pickup_seen,
            "visited_node_ids": list(visited),
            "accepted_action_count": len(directions),
            "remaining_action_budget": remaining_action_budget,
            "progress_limit": progress_limit,
            "messages_sha256": hashlib.sha256(
                "\n".join(message["content"] for message in messages).encode("utf-8")
            ).hexdigest(),
            "schema_sha256": constraint.schema_sha256,
            "input_bytes": user_size,
            "messages_bytes": all_messages_size,
            "rendered_cli_prompt_bytes": rendered_prompt_bytes,
            "rendered_cli_prompt_sha256": rendered_prompt_sha256,
            "feedback_snapshot": feedback_snapshot,
        }
        if on_request_start:
            on_request_start(request_start)
        try:
            response = client.chat(messages)
        except Exception as error:
            provider = getattr(client, "last_record", None)
            attempt = {
                "request_number": request_number,
                "status": "api_error",
                "error_type": type(error).__name__,
                "input_bytes": user_size,
                "messages_bytes": all_messages_size,
                "rendered_cli_prompt_bytes": rendered_prompt_bytes,
                "rendered_cli_prompt_sha256": rendered_prompt_sha256,
                "provider_audit": provider if isinstance(provider, dict) else None,
            }
            attempts.append(attempt)
            final_status = "api_error"
            if on_attempt:
                on_attempt(attempt)
            break

        content = getattr(response, "content", response)
        try:
            payload = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError):
            payload = None
        audit = validate_r4_segment_proposal(
            road_map,
            start_node_id=task.start_node_id,
            pickup_node_id=selected_pickup_node_id,
            dropoff_node_id=selected_dropoff_node_id,
            current_node_id=current,
            pickup_seen=pickup_seen,
            visited_node_ids=visited,
            payload=payload,
            accepted_action_count=len(directions),
            progress_limit=progress_limit,
            max_segment_actions=effective_max_segment_actions,
            min_segment_actions=minimum_segment_actions,
            blocked_edge_ids=blocked,
            allowed_edge_ids=(edge.edge_id for edge in package.edges),
        )
        usage = {
            key: getattr(response, key, None)
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "latency_ms", "retries")
        }
        attempt = {
            "request_number": request_number,
            "status": "response_received",
            "model": getattr(response, "model", None),
            "request_id": getattr(response, "request_id", None),
            "finish_reason": getattr(response, "finish_reason", None),
            "usage": usage,
            "input_bytes": user_size,
            "messages_bytes": all_messages_size,
            "rendered_cli_prompt_bytes": rendered_prompt_bytes,
            "rendered_cli_prompt_sha256": rendered_prompt_sha256,
            "constraint_schema_sha256": constraint.schema_sha256,
            "constraint_schema_bytes": constraint.schema_bytes,
            "provider_audit": getattr(response, "provider_audit", None),
            "proposal": payload,
            "audit": audit,
        }
        attempts.append(attempt)
        segment_audits.append(audit)
        if on_attempt:
            on_attempt(attempt)
        if audit["accepted"]:
            directions.extend(audit["committed_direction_sequence"])
            edges.extend(audit["committed_edge_sequence"])
            visited.extend(audit["committed_node_sequence"])
            current = audit["current_node_after_segment"]
            pickup_seen = audit["pickup_seen_after_segment"]
            feedback_history = []
            if audit["route_complete"]:
                final_status = "completed_route"
                break
            continue
        if audit["status"] == "model_gave_up":
            final_status = "model_gave_up"
            break

        rejected_segments += 1
        if rejected_segments > int(max_feedback_requests):
            final_status = "segment_rejection_limit_exhausted"
            break
        first = (audit.get("violations") or [{}])[0]
        prefix_nodes = list(audit.get("attempted_node_sequence") or [])
        prefix_directions = list(audit.get("attempted_direction_sequence") or [])
        failure_state_visited = list(visited) + prefix_nodes
        failure_state_pickup_seen = pickup_seen or selected_pickup_node_id in failure_state_visited
        failure_node = str(first.get("from_node_id", ""))
        local_allowed = _allowed_directions_at_node(
            road_map,
            failure_node,
            failure_state_visited,
            selected_pickup_node_id,
            selected_dropoff_node_id,
            failure_state_pickup_seen,
            blocked,
        )
        progress_safe = _progress_safe_directions_at_node(
            road_map,
            current_node_id=failure_node,
            visited_node_ids=failure_state_visited,
            pickup_node_id=selected_pickup_node_id,
            dropoff_node_id=selected_dropoff_node_id,
            pickup_seen=failure_state_pickup_seen,
            blocked_edge_ids=blocked,
            accepted_action_count=len(directions) + len(prefix_directions),
            progress_limit=progress_limit,
        )
        feedback = {
            "code": str(first.get("kind", "segment_rejected")),
            "step": first.get("step"),
            "direction": first.get("direction"),
            "from_node_id": first.get("from_node_id"),
            "to_node_id": first.get("to_node_id"),
            "previous_valid_prefix_directions": prefix_directions,
            "allowed_directions_at_failure_state": local_allowed,
            "progress_safe_directions_at_failure_state": progress_safe,
            "failure_state_route_progress_moves": len(directions) + len(prefix_directions),
            "progress_limit": progress_limit,
            "recovery_scope": "regenerate_the_entire_segment_from_the_original_current_node",
            "rejected_segment_is_atomic": True,
        }
        prefix_constraint = _rejection_prefix_constraint(feedback)
        if prefix_constraint not in feedback_history:
            feedback_history.append(prefix_constraint)
        feedback["recent_rejected_prefix_constraints"] = list(feedback_history)

    if current == selected_dropoff_node_id and pickup_seen:
        final_status = "completed_route"
    route_nodes = list(visited)
    usage_fields = ("input_tokens", "output_tokens")
    usage_complete = all(
        isinstance(attempt.get("usage"), dict)
        and all(isinstance(attempt["usage"].get(key), int) for key in usage_fields)
        for attempt in attempts
        if attempt.get("status") == "response_received"
    ) and all(attempt.get("status") == "response_received" for attempt in attempts)
    input_tokens = (
        sum(int(attempt["usage"]["input_tokens"]) for attempt in attempts)
        if usage_complete
        else None
    )
    output_tokens = (
        sum(int(attempt["usage"]["output_tokens"]) for attempt in attempts)
        if usage_complete
        else None
    )
    return {
        "schema_version": "r4_segment_search_result_v1",
        "method_version": "r4-segment-v5",
        "status": final_status,
        "route_complete": final_status == "completed_route",
        "initial_remaining_move_lower_bound": int(initial_lower_bound),
        "initial_route_move_count": len(initial_route_state.get("direction_sequence", [])) if initial_route_state else 0,
        "initial_rejected_segment_count": int(initial_rejected_segments),
        "request_number_offset": int(request_number_offset),
        "progress_limit": progress_limit,
        "max_segment_actions": int(max_segment_actions),
        "api_call_limit": int(max_api_calls),
        "api_request_count": len(attempts),
        "rejected_segment_count": rejected_segments,
        "recent_rejected_prefix_constraints": list(feedback_history),
        "attempts": attempts,
        "segment_audits": segment_audits,
        "route": {
            "start_node_id": task.start_node_id,
            "pickup_node_id": selected_pickup_node_id,
            "dropoff_node_id": selected_dropoff_node_id,
            "node_sequence": route_nodes,
            "edge_sequence": edges,
            "direction_sequence": directions,
            "move_count": len(directions),
            "last_node_id": current,
            "pickup_seen": pickup_seen,
            "route_complete": final_status == "completed_route",
        },
        "usage": {
            "usage_complete": usage_complete,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "serialized_user_input_bytes": input_bytes,
            "serialized_messages_bytes": messages_bytes,
            "serialized_cli_prompt_bytes": (
                rendered_cli_prompt_bytes_total
                if rendered_cli_prompt_sizes_known
                else None
            ),
            "serialized_cli_prompt_scope": (
                "current CLI stdin prompt only; excludes persistent conversation history"
            ),
            "client_retries": sum(
                int((attempt.get("usage") or {}).get("retries") or 0)
                for attempt in attempts
                if attempt.get("status") == "response_received"
            ),
        },
    }
