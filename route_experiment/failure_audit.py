"""Helpers for auditing failed pure-LLM route proposals.

Energy values produced here are map-based estimates of a model proposal. They
must not be confused with measured energy from an executed, valid route.
"""

import json
import re
import statistics
from typing import Any, Dict, Optional

from .models import EvaluationConfig, Edge, RoadMap


_NODE_ID = re.compile(r"^node_(-?\d+)_(-?\d+)$")

_FAILURE_LABELS = {
    "llm_move_sequence_invalid": "模型返回了路线，但路线包含违反道路限制的移动",
    "llm_route_invalid": "模型返回了路线，但路线校验未通过",
    "llm_output_parse_failed": "已收到模型响应，但输出格式无法解析",
    "model_confirmed_no_directed_path": "模型报告有向图中至少一段不可达；该判断未经独立可达性验证",
    "model_reported_no_directed_path": "模型报告无有向路径，但本地可达性预检确认任务链可达",
    "model_unable_to_construct_route": "模型未构造出完整路线，但没有报告已确认路网无路可走",
    "no_route": "模型返回了失败结果：未找到路线",
    "no_route_found": "模型返回了失败结果：未找到路线",
    "unreachable": "模型返回了失败结果：判断起终点不可达",
    "illegal_endpoint": "模型返回了失败结果：判断场景端点不合法",
    "pickup_not_found": "模型未能从任务描述中解析上车点",
    "pickup_unresolved": "模型未能把上车点解析为地图节点",
    "pickup_unreachable": "模型判断无法抵达上车点",
    "no_legal_pickup": "模型没有找到合法上车点",
    "no_legal_pickup_near_landmark": "模型没有找到地标附近的合法上车点",
    "ambiguous_pickup": "模型认为上车点表达存在歧义",
    "dropoff_not_found": "模型未能从任务描述中解析下车点",
    "dropoff_unresolved": "模型未能把下车点解析为地图节点",
    "dropoff_unreachable": "模型判断无法抵达下车点",
    "no_legal_dropoff": "模型没有找到合法下车点",
    "ambiguous_dropoff": "模型认为下车点表达存在歧义",
}

_VIOLATION_LABELS = {
    "one_way_direction": "单行方向相反",
    "missing_directed_edge": "该方向没有道路边",
    "outside_boundary": "越过地图边界",
    "blocked_edge": "经过场景封闭道路",
}


def describe_failure_zh(method_result: Dict[str, Any], trace: Optional[Dict[str, Any]] = None) -> str:
    """Return a concise Chinese explanation while preserving the raw code."""

    reason = str(method_result.get("failure_reason") or "unknown_failure")
    if reason == "llm_move_sequence_invalid":
        violations = (trace or {}).get("move_violations", [])
        counts: Dict[str, int] = {}
        for violation in violations:
            kind = str(violation.get("kind") or "unknown")
            counts[kind] = counts.get(kind, 0) + 1
        details = "、".join(
            "{0} {1} 步".format(_VIOLATION_LABELS.get(kind, kind), count)
            for kind, count in sorted(counts.items())
        )
        suffix = "：" + details if details else ""
        return "模型已返回路线，但路线校验发现非法移动" + suffix + "。"
    if reason == "llm_output_parse_failed":
        violations = (trace or {}).get("move_violations", [])
        if violations:
            counts: Dict[str, int] = {}
            for violation in violations:
                kind = str(violation.get("kind") or "unknown")
                counts[kind] = counts.get(kind, 0) + 1
            details = "、".join(
                "{0} {1} 步".format(_VIOLATION_LABELS.get(kind, kind), count)
                for kind, count in sorted(counts.items())
            )
            return (
                "已收到模型响应，但 JSON 被截断；从响应前缀恢复了部分路线，"
                "其中发现非法移动：{0}。耗电只估算可恢复的部分路线。"
            ).format(details)
        return "已收到模型响应，但输出格式无法解析；无法恢复足够路线步骤进行完整能耗估算。"
    if reason.startswith(("deepseek_request_failed", "missing_deepseek", "llm_request_failed")):
        return "API 请求未正常完成，没有取得可用于路线回放的模型结果。原始错误码：{0}".format(reason)
    label = _FAILURE_LABELS.get(reason)
    if label:
        if reason in {"illegal_endpoint", "unreachable"}:
            label += "（是否为预期失败需结合场景设计判断）"
        return label + "。原始原因：{0}".format(reason)
    return "方法未提供可验证路线。原始原因：{0}".format(reason)


def recover_partial_move_sequence(raw_output: Any) -> str:
    """Recover only complete move-run objects from a truncated JSON response."""

    if not isinstance(raw_output, dict):
        return ""
    content = raw_output.get("content")
    if not isinstance(content, str):
        return ""
    match = re.search(r'"move_runs"\s*:\s*\[', content)
    if not match:
        return ""
    remaining = content[match.end():]
    moves = []
    total_steps = 0
    for obj_match in re.finditer(r"\{[^{}]*\}", remaining):
        try:
            run = json.loads(obj_match.group(0))
        except json.JSONDecodeError:
            continue
        direction = str(run.get("direction", "")).upper()
        steps = run.get("steps")
        if direction not in {"E", "W", "N", "S"}:
            continue
        if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
            continue
        if total_steps + steps > 200_000:
            break
        moves.append(direction * steps)
        total_steps += steps
    return "".join(moves)


def recover_partial_edge_slot_sequence(raw_output: Any) -> str:
    """Recover complete three-character edge slots from truncated tool JSON."""

    if not isinstance(raw_output, dict):
        return ""
    content = raw_output.get("content")
    if not isinstance(content, str):
        return ""
    match = re.search(r'"edge_slot_sequence"\s*:\s*"([0-9A-Z]*)', content)
    if not match:
        return ""
    sequence = match.group(1)
    return sequence[: (len(sequence) // 3) * 3]


def _node_xy(node_id: str):
    match = _NODE_ID.match(str(node_id or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _edge_energy(edge: Edge, config: EvaluationConfig) -> float:
    distance_km = edge.length_m / 1000.0
    return (
        distance_km * config.moving_kwh_per_km
        + edge.signal_delay_sec * config.signal_idle_kwh_per_sec
        + distance_km * edge.congestion_level * config.congestion_extra_kwh_per_km_level
    )


def estimate_route_proposal_energy(
    road_map: RoadMap,
    replay: Dict[str, Any],
    config: Optional[EvaluationConfig] = None,
) -> Dict[str, Any]:
    """Estimate energy for the full proposed sequence and legal prefix.

    Existing directed edges use their exact map attributes. A proposed reverse
    traversal uses the opposite edge as a segment proxy. Moves with no edge in
    either direction use the map median edge length/congestion/signal delay.
    The latter two cases are explicitly counted as inferred, since an invalid
    move has no directly traversable edge record.
    """

    config = config or EvaluationConfig()
    edges = list(road_map.edges)
    edge_by_id = {edge.edge_id: edge for edge in edges}
    fallback_length_m = statistics.median([edge.length_m for edge in edges]) if edges else 100.0
    fallback_congestion = statistics.median([edge.congestion_level for edge in edges]) if edges else 0
    fallback_signal_delay = statistics.median([edge.signal_delay_sec for edge in edges]) if edges else 0.0

    attempted_nodes = list(replay.get("attempted_route_node_sequence", []))
    directed_edge_mode = replay.get("route_representation") == "directed_edge_slots"
    proposed_edge_ids = list(replay.get("attempted_route_edge_sequence", []))
    proposed_move_count = (
        len(proposed_edge_ids)
        if directed_edge_mode
        else max(0, len(attempted_nodes) - 1)
    )
    total_energy = 0.0
    distance_m = 0.0
    exact_edge_moves = 0
    reverse_edge_inferred_moves = 0
    proxy_edge_moves = 0

    if directed_edge_mode:
        for edge_id in proposed_edge_ids:
            edge = edge_by_id.get(edge_id)
            if edge is not None:
                total_energy += _edge_energy(edge, config)
                distance_m += edge.length_m
                exact_edge_moves += 1
            else:
                segment_km = fallback_length_m / 1000.0
                total_energy += (
                    segment_km * config.moving_kwh_per_km
                    + fallback_signal_delay * config.signal_idle_kwh_per_sec
                    + segment_km * fallback_congestion * config.congestion_extra_kwh_per_km_level
                )
                distance_m += fallback_length_m
                proxy_edge_moves += 1
    else:
        for source_id, target_id in zip(attempted_nodes, attempted_nodes[1:]):
            edge_id = "edge_{0}_{1}".format(source_id, target_id)
            edge = edge_by_id.get(edge_id)
            if edge is not None:
                total_energy += _edge_energy(edge, config)
                distance_m += edge.length_m
                exact_edge_moves += 1
                continue

            reverse_id = "edge_{0}_{1}".format(target_id, source_id)
            reverse_edge = edge_by_id.get(reverse_id)
            if reverse_edge is not None:
                total_energy += _edge_energy(reverse_edge, config)
                distance_m += reverse_edge.length_m
                reverse_edge_inferred_moves += 1
                continue

            source_xy, target_xy = _node_xy(source_id), _node_xy(target_id)
            if source_xy is not None and target_xy is not None:
                grid_steps = abs(source_xy[0] - target_xy[0]) + abs(source_xy[1] - target_xy[1])
                segment_length_m = fallback_length_m * max(1, grid_steps)
            else:
                segment_length_m = fallback_length_m
            segment_km = segment_length_m / 1000.0
            total_energy += (
                segment_km * config.moving_kwh_per_km
                + fallback_signal_delay * config.signal_idle_kwh_per_sec
                + segment_km * fallback_congestion * config.congestion_extra_kwh_per_km_level
            )
            distance_m += segment_length_m
            proxy_edge_moves += 1

    prefix_energy = 0.0
    prefix_edges = list(replay.get("validated_prefix_edge_sequence", []))
    for edge_id in prefix_edges:
        edge = edge_by_id.get(edge_id)
        if edge is not None:
            prefix_energy += _edge_energy(edge, config)

    return {
        "status": "estimated" if proposed_move_count else "no_route_proposed",
        "route_energy_estimate_kwh": total_energy if proposed_move_count else None,
        "validated_prefix_energy_estimate_kwh": prefix_energy if proposed_move_count else None,
        "estimated_route_distance_m": distance_m if proposed_move_count else None,
        "proposed_move_count": proposed_move_count,
        "validated_prefix_move_count": len(prefix_edges),
        "exact_edge_moves": exact_edge_moves,
        "reverse_edge_inferred_moves": reverse_edge_inferred_moves,
        "proxy_edge_moves": proxy_edge_moves,
        "energy_model_version": config.energy_model_version,
        "fallback_length_m": fallback_length_m,
        "fallback_congestion_level": fallback_congestion,
        "fallback_signal_delay_sec": fallback_signal_delay,
        "interpretation": (
            "not actual measured consumption; hypothetical sum for model-proposed directed edges. "
            "Disconnected edge sequences are not executable routes."
            if directed_edge_mode
            else "not actual measured consumption; hypothetical estimate for the model-proposed path. "
            "Valid-prefix estimate stops before the first illegal move."
        ),
    }
