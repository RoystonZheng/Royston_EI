"""Versioned semantic map protocol and bounded R4 intent queries.

The persisted protocol is derived only from public map facts. Query payloads
expose a shortlist of legal endpoint candidates, never evaluator truth.
"""

import json
import re
from typing import Any, Dict, Iterable, List, Optional

from .models import RoadFeatureInputProtocol, RoadMap, VehicleOrderTask


def _distance(a: Dict[str, Any], b: Dict[str, Any]) -> int:
    return abs(int(a["x"]) - int(b["x"])) + abs(int(a["y"]) - int(b["y"]))


def build_road_feature_protocol(road_map: RoadMap, seed: int) -> RoadFeatureInputProtocol:
    """Build the full per-map RoadFeatureInputProtocol v1 snapshot."""

    entities: List[Dict[str, Any]] = []
    features: List[Dict[str, Any]] = []
    relations: List[Dict[str, Any]] = []
    semantic_index: List[Dict[str, Any]] = []
    nodes = {node.node_id: node for node in road_map.nodes}

    for node in road_map.nodes:
        entities.append({
            "entity_id": node.node_id,
            "entity_type": "intersection",
            "coordinates": {"x": node.x, "y": node.y},
            "legal_pickup": node.legal_pickup,
            "legal_dropoff": node.legal_dropoff,
        })
        if node.landmark_name:
            landmark_id = "landmark:" + node.node_id
            entities.append({
                "entity_id": landmark_id,
                "entity_type": "landmark",
                "name": node.landmark_name,
                "aliases": list(node.landmark_aliases),
            })
            features.append({
                "feature_id": landmark_id,
                "feature_type": "landmark",
                "name": node.landmark_name,
                "aliases": list(node.landmark_aliases),
            })
            relations.append({
                "relation_type": "landmark_at_node",
                "source_id": landmark_id,
                "target_id": node.node_id,
            })

    for edge in road_map.edges:
        entities.append({
            "entity_id": edge.edge_id,
            "entity_type": "directed_road",
            "from_node_id": edge.from_node,
            "to_node_id": edge.to_node,
        })
        features.append({
            "feature_id": "road_state:" + edge.edge_id,
            "feature_type": "road_state",
            "length_m": edge.length_m,
            "base_speed_mps": edge.base_speed_mps,
            "congestion_level": edge.congestion_level,
            "signal_delay_sec": edge.signal_delay_sec,
        })
        relations.append({
            "relation_type": "directed_connects",
            "source_id": edge.from_node,
            "target_id": edge.to_node,
            "edge_id": edge.edge_id,
        })

    for feature in road_map.roadside_features:
        feature_ref = "feature:" + feature.feature_id
        entities.append({
            "entity_id": feature_ref,
            "entity_type": feature.feature_type,
            "name": feature.label,
            "aliases": list(feature.aliases),
        })
        features.append({
            "feature_id": feature_ref,
            "feature_type": feature.feature_type,
            "label": feature.label,
            "aliases": list(feature.aliases),
            "side_of_road": feature.side_of_road,
            "orientation": feature.orientation,
            "semantic_role": feature.semantic_role,
        })
        relations.append({
            "relation_type": "feature_located_at",
            "source_id": feature_ref,
            "target_id": feature.node_id,
        })
        candidates = []
        for node_id, relation in (
            (feature.node_id, "feature_side"),
            (feature.paired_node_id, "opposite_side"),
        ):
            node = nodes.get(node_id)
            if node_id and node:
                relations.append({
                    "relation_type": "opposite_side_candidate" if relation == "opposite_side" else "feature_side_candidate",
                    "source_id": feature_ref,
                    "target_id": node_id,
                })
                if node.legal_pickup:
                    candidates.append({
                        "node_id": node.node_id,
                        "coordinates": {"x": node.x, "y": node.y},
                        "legal_pickup": True,
                        "relation_to_feature": relation,
                    })
        semantic_index.append({
            "entry_id": feature_ref,
            "entry_type": feature.feature_type,
            "name": feature.label,
            "aliases": list(feature.aliases),
            "side_of_road": feature.side_of_road,
            "orientation": feature.orientation,
            "anchor_node_id": feature.node_id,
            "anchor_coordinates": {"x": nodes[feature.node_id].x, "y": nodes[feature.node_id].y},
            "pickup_candidates": candidates,
        })

    legal_pickups = [node for node in road_map.nodes if node.legal_pickup]
    legal_dropoffs = [node for node in road_map.nodes if node.legal_dropoff]
    for landmark in (node for node in road_map.nodes if node.landmark_name):
        anchor = {"x": landmark.x, "y": landmark.y}
        pickup_candidates = sorted(
            legal_pickups,
            key=lambda node: (_distance(anchor, {"x": node.x, "y": node.y}), node.node_id),
        )[:6]
        dropoff_candidates = sorted(
            legal_dropoffs,
            key=lambda node: (_distance(anchor, {"x": node.x, "y": node.y}), node.node_id),
        )[:6]
        directional_candidates: Dict[str, List[Dict[str, Any]]] = {}
        for side, predicate in (
            ("east", lambda n: n.x > landmark.x),
            ("west", lambda n: n.x < landmark.x),
            ("north", lambda n: n.y > landmark.y),
            ("south", lambda n: n.y < landmark.y),
        ):
            side_nodes = sorted(
                [node for node in legal_pickups if predicate(node)],
                key=lambda node: (_distance(anchor, {"x": node.x, "y": node.y}), node.node_id),
            )[:4]
            directional_candidates[side] = [
                {"node_id": node.node_id, "coordinates": {"x": node.x, "y": node.y}, "legal_pickup": True}
                for node in side_nodes
            ]
        semantic_index.append({
            "entry_id": "landmark:" + landmark.node_id,
            "entry_type": "landmark",
            "name": landmark.landmark_name,
            "aliases": list(landmark.landmark_aliases),
            "anchor_node_id": landmark.node_id,
            "anchor_coordinates": anchor,
            "pickup_candidates": [
                {"node_id": node.node_id, "coordinates": {"x": node.x, "y": node.y}, "legal_pickup": True}
                for node in pickup_candidates
            ],
            "directional_pickup_candidates": directional_candidates,
            "dropoff_candidates": [
                {"node_id": node.node_id, "coordinates": {"x": node.x, "y": node.y}, "legal_dropoff": True}
                for node in dropoff_candidates
            ],
        })

    return RoadFeatureInputProtocol(
        protocol_name="RoadFeatureInputProtocol",
        protocol_version="v1",
        units={"coordinates": "grid_cell", "road_length": "m", "speed": "m/s", "signal_delay": "s"},
        context={
            "map_id": road_map.map_id,
            "map_version": road_map.version,
            "road_state_version": road_map.road_state_version,
            "random_seed": int(seed),
            "source_time": "static_synthetic_map",
            "boundary": dict(road_map.boundary),
        },
        route_query={"query_scope": "task_specific_fragment_is_materialized_per_task"},
        entities=entities,
        features=features,
        relations=relations,
        constraints=[
            {"constraint_type": "inclusive_boundary", "value": dict(road_map.boundary)},
            {"constraint_type": "directed_edges_only", "source": "listed_directed_road_entities"},
            {"constraint_type": "legal_pickup_only", "source": "intersection.legal_pickup"},
            {"constraint_type": "legal_dropoff_only", "source": "intersection.legal_dropoff"},
        ],
        semantic_index=semantic_index,
        output_contract={
            "schema_version": "route_intent_v1",
            "status_values": ["resolved", "ambiguous", "unresolved"],
            "candidate_ids": "select only IDs from this task query's candidate lists",
            "uses_evaluator_truth": False,
        },
    )


def _matching_entries(text: Optional[str], entries: Iterable[Dict[str, Any]], types: Iterable[str]) -> List[Dict[str, Any]]:
    if not text:
        return []
    allowed = set(types)
    matches = []
    for entry in entries:
        if entry.get("entry_type") not in allowed:
            continue
        names = [entry.get("name"), *entry.get("aliases", [])]
        matched = [name for name in names if isinstance(name, str) and name and name in text]
        if matched:
            matches.append((max(len(name) for name in matched), entry))
    return [entry for _, entry in sorted(matches, key=lambda item: (-item[0], item[1]["entry_id"]))[:3]]


def _dedupe_candidates(groups: Iterable[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        for candidate in group:
            node_id = candidate.get("node_id")
            if isinstance(node_id, str):
                result.setdefault(node_id, candidate)
    return [result[node_id] for node_id in sorted(result)]


def build_intent_query(
    road_map: RoadMap,
    task: VehicleOrderTask,
    protocol: RoadFeatureInputProtocol,
    *,
    map_input_mode: str = "road_feature_protocol_v1",
    max_candidates: int = 24,
) -> Dict[str, Any]:
    """Build the bounded Qwen query from matched protocol entities only."""

    if map_input_mode not in {"road_feature_protocol_v1", "graph_only"}:
        raise ValueError("r4_map_input_mode_invalid")
    nodes = {node.node_id: node for node in road_map.nodes}
    if task.task_type == "exact_node":
        return {
            "schema_version": "route_intent_query_v1",
            "task_id": task.task_id,
            "map_id": road_map.map_id,
            "map_input_mode": map_input_mode,
            "status": "exact",
            "start_node_id": task.start_node_id,
            "pickup_node_id": task.pickup_node_id,
            "dropoff_node_id": task.dropoff_node_id,
            "uses_evaluator_truth": False,
        }

    entries = list(protocol.semantic_index)
    feature_matches = (
        _matching_entries(task.pickup_text, entries, {"road_sign", "utility_pole"})
        if map_input_mode == "road_feature_protocol_v1"
        else []
    )
    pickup_landmarks = _matching_entries(task.pickup_text, entries, {"landmark"})
    if map_input_mode == "graph_only" and task.task_type == "semantic_road_feature_position":
        # A venue name embedded in a road-sign label does not turn the request
        # into a landmark task after the roadside feature is ablated.
        feature_markers = ("路牌", "电线杆", "路灯", "公交站牌", "方向牌")
        if any(marker in (task.pickup_text or "") for marker in feature_markers):
            pickup_landmarks = []
    dropoff_landmarks = _matching_entries(task.dropoff_text, entries, {"landmark"})

    if feature_matches:
        pickup_candidates = _dedupe_candidates(
            entry.get("pickup_candidates", []) for entry in feature_matches
        )
        pickup_mode = "road_feature_relation"
    elif pickup_landmarks:
        landmark = pickup_landmarks[0]
        directional = landmark.get("directional_pickup_candidates", {})
        pickup_candidates = _dedupe_candidates([
            landmark.get("pickup_candidates", []),
            *directional.values(),
        ])
        pickup_mode = "landmark_graph_candidates"
    else:
        start = nodes.get(task.start_node_id)
        legal = [node for node in road_map.nodes if node.legal_pickup]
        if map_input_mode == "graph_only" and task.task_type == "semantic_road_feature_position" and start:
            legal = sorted(
                legal,
                key=lambda node: (abs(node.x - start.x) + abs(node.y - start.y), node.node_id),
            )[:8]
            pickup_mode = "graph_only_start_nearest_fallback"
        else:
            pickup_mode = "no_matching_pickup_anchor"
        pickup_candidates = [
            {"node_id": node.node_id, "coordinates": {"x": node.x, "y": node.y}, "legal_pickup": True}
            for node in legal
        ]

    if dropoff_landmarks:
        dropoff_candidates = _dedupe_candidates(
            [entry.get("dropoff_candidates", []) for entry in dropoff_landmarks]
        )
        dropoff_mode = "landmark_graph_candidates"
    else:
        dropoff_mode = "no_matching_dropoff_anchor"
        dropoff_candidates = []

    pickup_candidates = pickup_candidates[:max_candidates]
    dropoff_candidates = dropoff_candidates[:max_candidates]
    query = {
        "schema_version": "route_intent_query_v1",
        "task_id": task.task_id,
        "map_id": road_map.map_id,
        "map_version": road_map.version,
        "road_state_version": road_map.road_state_version,
        "map_input_mode": map_input_mode,
        "start_node_id": task.start_node_id,
        "pickup_text": task.pickup_text,
        "dropoff_text": task.dropoff_text,
        "pickup_resolution_mode": pickup_mode,
        "dropoff_resolution_mode": dropoff_mode,
        "matched_pickup_entries": feature_matches or pickup_landmarks,
        "matched_dropoff_entries": dropoff_landmarks,
        "pickup_candidates": pickup_candidates,
        "dropoff_candidates": dropoff_candidates,
        "allowed_feature_refs": [entry["entry_id"] for entry in feature_matches],
        "allowed_landmark_refs": list(dict.fromkeys(
            entry["entry_id"] for entry in pickup_landmarks + dropoff_landmarks
        )),
        "uses_evaluator_truth": False,
    }
    return query


def build_intent_prompt(query: Dict[str, Any]) -> str:
    """Render a small deterministic JSON-only Qwen prompt."""

    user = json.dumps(query, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        "你是路线任务的地点意图解析器。根据原始自然语言和给定的地图协议候选，"
        "只选择候选白名单里的上车点与下车点；不生成路线，不猜测未列出的节点，不调用工具。"
        "坐标 x 向东递增、y 向北递增。道路特征任务先选名称和侧别匹配的 feature；"
        "feature_side 表示路牌所在道路侧的候选点，opposite_side 表示道路另一侧。"
        "例如用户说在路牌旁并指定路牌位于道路西侧，应选该路牌的 feature_side 候选；"
        "不要把输入中的地标或道路特征名称直接当成节点。地标任务按附近或指定方位选择节点。"
        "如果 map_input_mode=graph_only 且没有匹配道路特征，遵循 pickup_resolution_mode 中给出的普通图候选规则。"
        "不得读取或推断评估真值。输入对象的 schema_version 是 route_intent_query_v1；"
        "输出对象必须使用 schema_version=route_intent_v1，status 只能为 resolved、ambiguous 或 unresolved。"
        "只输出一个 JSON 对象，字段必须完整，格式如下："
        '{"schema_version":"route_intent_v1","status":"resolved",'
        '"pickup_node_id":"候选节点ID","dropoff_node_id":"候选节点ID",'
        '"pickup_anchor_ref":"候选引用ID","dropoff_anchor_ref":"候选引用ID",'
        '"relation":"near|directional|feature_side|opposite_side|unknown",'
        '"side":"north|south|east|west|none|unknown",'
        '"feature_evidence_refs":[],"confidence":0.0}。'
        "status=resolved 时必须填白名单中的节点和引用；置信度为 0 到 1 的数字。\n"
        "输入：" + user
    )


def validate_route_intent(payload: Any, query: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce the candidate whitelist and endpoint legality without truth."""

    if not isinstance(payload, dict):
        return {"valid": False, "reason": "intent_output_not_object"}
    pickup_ids = {item["node_id"] for item in query.get("pickup_candidates", [])}
    dropoff_ids = {item["node_id"] for item in query.get("dropoff_candidates", [])}
    pickup_id = payload.get("pickup_node_id")
    dropoff_id = payload.get("dropoff_node_id")
    if payload.get("schema_version") != "route_intent_v1":
        return {"valid": False, "reason": "intent_schema_version_invalid"}
    if payload.get("status") != "resolved":
        return {"valid": False, "reason": "intent_unresolved", "model_status": payload.get("status")}
    if pickup_id not in pickup_ids:
        return {"valid": False, "reason": "intent_pickup_not_in_candidate_whitelist"}
    if dropoff_id not in dropoff_ids:
        return {"valid": False, "reason": "intent_dropoff_not_in_candidate_whitelist"}
    feature_refs = set(query.get("allowed_feature_refs", []))
    landmark_refs = set(query.get("allowed_landmark_refs", []))
    allowed_anchors = feature_refs | landmark_refs
    anchors = [payload.get("pickup_anchor_ref"), payload.get("dropoff_anchor_ref")]
    if any(anchor and anchor not in allowed_anchors for anchor in anchors):
        return {"valid": False, "reason": "intent_anchor_not_in_reference_whitelist"}
    evidence = payload.get("feature_evidence_refs", [])
    if not isinstance(evidence, list) or any(item not in feature_refs for item in evidence):
        return {"valid": False, "reason": "intent_feature_evidence_not_in_reference_whitelist"}
    return {
        "valid": True,
        "pickup_node_id": pickup_id,
        "dropoff_node_id": dropoff_id,
        "pickup_anchor_ref": payload.get("pickup_anchor_ref"),
        "dropoff_anchor_ref": payload.get("dropoff_anchor_ref"),
        "relation": payload.get("relation"),
        "side": payload.get("side"),
        "feature_evidence_refs": evidence,
        "confidence": payload.get("confidence"),
        "resolution_mode": query.get("pickup_resolution_mode"),
    }


def extract_intent_json(text: str) -> Dict[str, Any]:
    """Parse the first JSON object from a local model completion."""

    cleaned = re.sub(r"<think>.*?</think>", "", str(text), flags=re.S).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("intent_json_missing")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                value = json.loads(cleaned[start:index + 1])
                if not isinstance(value, dict):
                    raise ValueError("intent_json_not_object")
                return value
    raise ValueError("intent_json_incomplete")
