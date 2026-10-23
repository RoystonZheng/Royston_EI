"""Compact whole-route response contract and deterministic R4 replay.

The model returns one direction per move and one self-check marker per move.
No proposal is accepted until every step has been checked against the frozen
directed map, scenario closures, boundary, visit order, and no-revisit rule.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from .models import RoadMap


@dataclass(frozen=True)
class R4CompactRouteConstraint:
    schema: Dict[str, Any]
    schema_sha256: str
    schema_bytes: int


def make_r4_compact_route_constraint(max_route_edges: int) -> R4CompactRouteConstraint:
    schema = build_r4_compact_route_schema(max_route_edges)
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return R4CompactRouteConstraint(
        schema=schema,
        schema_sha256=hashlib.sha256(encoded).hexdigest(),
        schema_bytes=len(encoded),
    )


def build_r4_compact_route_messages(
    package,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    *,
    feedback: Optional[Dict[str, Any]] = None,
    route_intent: Optional[Dict[str, Any]] = None,
    route_feature_context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Build one compact whole-route request with a check marker per move."""

    from .llm_adapter import build_r4_route_search_messages

    messages = build_r4_route_search_messages(
        package,
        start_node_id,
        pickup_node_id,
        dropoff_node_id,
        require_completed_route=True,
    )
    payload = json.loads(messages[1]["content"])
    payload["output_contract"] = {
        "schema_version": "r4_compact_route_output_v1",
        "status": ["completed", "failed"],
        "move_sequence": "one E/W/N/S character per proposed edge; no separators",
        "step_check_sequence": "one 1 per move, aligned by position",
        "failure_reason_code": ["none", "unable_to_construct_route", "no_directed_path"],
        "rule": "completed is only a proposal; the independent program decides acceptance",
    }
    payload["route_query"]["step_check_requirement"] = (
        "For every move, first check the current node's directed slot, destination boundary, "
        "pickup/dropoff order, and no-revisit rule; append one self-check marker only after checking."
    )
    if route_intent:
        payload["route_intent"] = route_intent
    if route_feature_context is not None:
        feature_context_json = json.dumps(
            route_feature_context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if any(
            marker in feature_context_json.lower()
            for marker in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")
        ):
            raise ValueError("r4_compact_route_feature_context_contains_evaluator_truth")
        payload["i"] = route_feature_context
    if feedback:
        payload["r4_validation_feedback"] = feedback
    messages[0] = {
        "role": "system",
        "content": (
            "你只执行给定有向网格地图上的单车路线检索。不得调用工具、外部算法或读取其他文件。"
            "只能使用 directional_cost_codes_by_y 中实际列出的出边；点号表示该方向无路，禁止推断反向边。"
            "从 start_node_id 出发，按顺序到 pickup_node_id、dropoff_node_id，到达下车点立即停止。"
            "路径不得越界、经过封闭边或重复节点。目标是总通行时间最小，同时间再比较总距离；"
            "不要输出长篇推理。对候选路线的每一步依次核对当前有向出边、边界、上/下车顺序和未访问状态，"
            "并用 step_check_sequence 中的一个字符 1 对应每一步已做自检。完成后调用输出 schema，"
            "move_sequence 的每个字符代表从当前点移动一个方向。独立程序会从第一步开始逐边复核，"
            "任何一项失败都会拒绝整条提议；自检码不能替代程序校验。"
            "若消息含 r4_validation_feedback，它是本地校验器对上一条提议的错误摘要，不是地图事实；"
            "请从原起点重新规划，不要只续写前缀。"
            "若输入含 i，它是已通过白名单校验的 RoadFeatureInputProtocol v1 地点特征与关系片段；"
            "只可用来理解固定 q 端点的语义依据，不得修改 q 的端点，也不能把未列出的边当成存在。"
        ),
    }
    messages[1] = {
        "role": "user",
        "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }
    return messages


_DIRECTIONS = {
    "E": (1, 0),
    "W": (-1, 0),
    "N": (0, 1),
    "S": (0, -1),
}


def build_r4_compact_route_schema(max_route_edges: int) -> Dict[str, Any]:
    """Build a small strict schema for a route and its aligned self-checks."""

    maximum = int(max_route_edges)
    if maximum < 1:
        raise ValueError("r4_compact_route_max_edges_invalid")
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["completed", "failed"]},
            "move_sequence": {
                "type": "string",
                "pattern": "^[EWNS]{0," + str(maximum) + "}$",
            },
            "step_check_sequence": {
                "type": "string",
                "pattern": "^1{0," + str(maximum) + "}$",
                "description": "One 1 per move, in route order, meaning the model performed its step check.",
            },
            "failure_reason_code": {
                "type": "string",
                "enum": ["none", "unable_to_construct_route", "no_directed_path"],
            },
        },
        "required": [
            "status",
            "move_sequence",
            "step_check_sequence",
            "failure_reason_code",
        ],
        "additionalProperties": False,
    }


def validate_r4_compact_route_proposal(
    road_map: RoadMap,
    *,
    start_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    payload: Any,
    blocked_edge_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    """Audit a compact route proposal and accept only a fully legal route."""

    if not isinstance(payload, dict):
        return _invalid_payload("r4_route_output_not_object")
    status = payload.get("status")
    moves = payload.get("move_sequence")
    checks = payload.get("step_check_sequence")
    if status == "failed":
        return {
            "schema_version": "r4_compact_route_validation_v1",
            "accepted": False,
            "status": "model_failed",
            "failure_reason_code": payload.get("failure_reason_code"),
            "route_node_sequence": [start_node_id],
            "route_edge_sequence": [],
            "validated_prefix_node_sequence": [start_node_id],
            "validated_prefix_edge_sequence": [],
            "steps": [],
            "violations": [],
        }
    if status != "completed" or not isinstance(moves, str) or not isinstance(checks, str):
        return _invalid_payload("r4_route_output_shape_invalid")

    violations: List[Dict[str, Any]] = []
    if len(moves) != len(checks):
        violations.append(
            {
                "step": None,
                "kind": "self_check_count_mismatch",
                "move_count": len(moves),
                "self_check_count": len(checks),
            }
        )
    if not moves:
        violations.append({"step": None, "kind": "route_sequence_empty"})

    node_by_id = {node.node_id: node for node in road_map.nodes}
    if pickup_node_id not in node_by_id or not node_by_id[pickup_node_id].legal_pickup:
        violations.append({"step": None, "kind": "pickup_endpoint_not_legal", "to_node_id": pickup_node_id})
    if dropoff_node_id not in node_by_id or not node_by_id[dropoff_node_id].legal_dropoff:
        violations.append({"step": None, "kind": "dropoff_endpoint_not_legal", "to_node_id": dropoff_node_id})
    start = node_by_id.get(start_node_id)
    if start is None:
        return _invalid_payload("r4_start_node_missing")
    edge_by_pair = {(edge.from_node, edge.to_node): edge for edge in road_map.edges}
    blocked: Set[str] = set(blocked_edge_ids)
    min_x = int(road_map.boundary.get("min_x", min(node.x for node in road_map.nodes)))
    max_x = int(road_map.boundary.get("max_x", max(node.x for node in road_map.nodes)))
    min_y = int(road_map.boundary.get("min_y", min(node.y for node in road_map.nodes)))
    max_y = int(road_map.boundary.get("max_y", max(node.y for node in road_map.nodes)))
    current_x, current_y = start.x, start.y
    current_id = start_node_id
    visited = {start_node_id}
    pickup_seen = current_id == pickup_node_id
    dropoff_seen = current_id == dropoff_node_id
    route_nodes = [start_node_id]
    proposed_edges: List[Optional[str]] = []
    accepted_prefix_nodes = [start_node_id]
    accepted_prefix_edges: List[str] = []
    prefix_open = True
    steps: List[Dict[str, Any]] = []

    for step_index, direction in enumerate(moves, 1):
        from_id = current_id
        from_x, from_y = current_x, current_y
        model_check = (
            "UNALIGNABLE_COUNT_MISMATCH"
            if len(moves) != len(checks)
            else "PASS" if checks[step_index - 1] == "1" else "FAIL"
        )
        if direction not in _DIRECTIONS:
            violation = {
                "step": step_index,
                "kind": "invalid_direction",
                "from_node_id": from_id,
                "direction": direction,
                "to_node_id": None,
            }
            violations.append(violation)
            steps.append(
                {
                    **violation,
                    "edge_id": None,
                    "model_self_check": model_check,
                    "local_directed_edge_check": "FAIL",
                    "local_boundary_check": "NOT_EVALUATED",
                    "local_endpoint_order_check": "NOT_EVALUATED",
                    "local_no_revisit_check": "NOT_EVALUATED",
                    "accepted_step": False,
                }
            )
            proposed_edges.append(None)
            prefix_open = False
            continue

        dx, dy = _DIRECTIONS[direction]
        target_x, target_y = from_x + dx, from_y + dy
        target_id = "node_{0}_{1}".format(target_x, target_y)
        target_node = node_by_id.get(target_id)
        within_boundary = min_x <= target_x <= max_x and min_y <= target_y <= max_y
        edge = edge_by_pair.get((from_id, target_id))
        reverse = edge_by_pair.get((target_id, from_id))
        step_violations: List[Dict[str, Any]] = []
        if not within_boundary:
            kind = "outside_boundary"
        elif target_node is None:
            kind = "target_node_missing"
        elif edge is None and reverse is not None and reverse.one_way:
            kind = "one_way_direction"
        elif edge is None:
            kind = "missing_directed_edge"
        elif edge.edge_id in blocked:
            kind = "blocked_edge"
        else:
            kind = None
        if kind:
            step_violations.append(
                {
                    "step": step_index,
                    "kind": kind,
                    "from_node_id": from_id,
                    "to_node_id": target_id,
                    "direction": direction,
                    "edge_id": edge.edge_id if edge else None,
                }
            )

        local_order_ok = not dropoff_seen and not (
            target_id == dropoff_node_id and not pickup_seen
        )
        if not local_order_ok:
            step_violations.append(
                {
                    "step": step_index,
                    "kind": "route_endpoint_order_invalid",
                    "from_node_id": from_id,
                    "to_node_id": target_id,
                    "direction": direction,
                    "edge_id": edge.edge_id if edge else None,
                }
            )
        is_revisit = target_id in visited
        if is_revisit:
            step_violations.append(
                {
                    "step": step_index,
                    "kind": "node_revisited",
                    "from_node_id": from_id,
                    "to_node_id": target_id,
                    "direction": direction,
                    "edge_id": edge.edge_id if edge else None,
                }
            )
        if model_check == "FAIL":
            step_violations.append(
                {
                    "step": step_index,
                    "kind": "model_self_check_failed",
                    "from_node_id": from_id,
                    "to_node_id": target_id,
                    "direction": direction,
                    "edge_id": edge.edge_id if edge else None,
                }
            )

        accepted_step = not step_violations and prefix_open
        for violation in step_violations:
            violation["local_position"] = {"x": target_x, "y": target_y}
            violations.append(violation)
        if accepted_step and edge is not None:
            accepted_prefix_edges.append(edge.edge_id)
            accepted_prefix_nodes.append(target_id)
        elif step_violations:
            prefix_open = False

        steps.append(
            {
                "step": step_index,
                "from_node_id": from_id,
                "to_node_id": target_id,
                "from_x": from_x,
                "from_y": from_y,
                "to_x": target_x,
                "to_y": target_y,
                "direction": direction,
                "edge_id": edge.edge_id if edge else None,
                "model_self_check": model_check,
                "local_directed_edge_check": (
                    "PASS" if edge is not None and edge.edge_id not in blocked else "FAIL"
                ),
                "local_boundary_check": "PASS" if within_boundary else "FAIL",
                "local_endpoint_order_check": "PASS" if local_order_ok else "FAIL",
                "local_no_revisit_check": "FAIL" if is_revisit else "PASS",
                "accepted_step": accepted_step,
                "violation_kinds": [item["kind"] for item in step_violations],
            }
        )
        proposed_edges.append(edge.edge_id if edge else None)
        route_nodes.append(target_id)
        current_x, current_y, current_id = target_x, target_y, target_id
        if target_id == pickup_node_id:
            pickup_seen = True
        if target_id == dropoff_node_id:
            dropoff_seen = True
        if target_node is not None:
            visited.add(target_id)

    if moves and current_id != dropoff_node_id:
        violations.append(
            {
                "step": len(moves),
                "kind": "route_does_not_end_at_dropoff",
                "from_node_id": current_id,
                "to_node_id": dropoff_node_id,
            }
        )
    if not pickup_seen:
        violations.append(
            {
                "step": len(moves),
                "kind": "pickup_not_visited",
                "from_node_id": start_node_id,
                "to_node_id": pickup_node_id,
            }
        )

    accepted = not violations and bool(moves) and len(proposed_edges) == len(moves)
    return {
        "schema_version": "r4_compact_route_validation_v1",
        "accepted": accepted,
        "status": "completed" if accepted else "proposal_rejected",
        "failure_reason_code": None if accepted else "route_proposal_invalid",
        "route_node_sequence": route_nodes,
        "route_edge_sequence": [edge_id for edge_id in proposed_edges if edge_id],
        "proposed_edge_sequence": proposed_edges,
        "validated_prefix_node_sequence": accepted_prefix_nodes,
        "validated_prefix_edge_sequence": accepted_prefix_edges,
        "move_count": len(moves),
        "steps": steps,
        "violations": violations,
        "first_violation_step": next(
            (item.get("step") for item in violations if item.get("step") is not None), None
        ),
        "model_self_check_count": len(checks),
        "model_self_check_all_pass": len(moves) == len(checks)
        and all(check == "1" for check in checks),
    }


def _invalid_payload(kind: str) -> Dict[str, Any]:
    return {
        "schema_version": "r4_compact_route_validation_v1",
        "accepted": False,
        "status": "proposal_rejected",
        "failure_reason_code": kind,
        "route_node_sequence": [],
        "route_edge_sequence": [],
        "validated_prefix_node_sequence": [],
        "validated_prefix_edge_sequence": [],
        "steps": [],
        "violations": [{"step": None, "kind": kind}],
        "first_violation_step": None,
    }


def build_r4_compact_route_feedback(audit: Dict[str, Any]) -> Dict[str, Any]:
    """Return bounded validator feedback without evaluator-only route truth."""

    violations = []
    for item in audit.get("violations", [])[:12]:
        violations.append(
            {
                key: item[key]
                for key in ("step", "kind", "from_node_id", "to_node_id", "direction")
                if key in item
            }
        )
    return {
        "schema_version": "r4_validation_feedback_v1",
        "proposal_rejected": True,
        "violation_count": len(audit.get("violations", [])),
        "first_violation_step": audit.get("first_violation_step"),
        "violations": violations,
        "instruction": "Discard the previous proposal and search again from the original start node.",
    }
