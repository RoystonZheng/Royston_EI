#!/usr/bin/env python3
"""Run one gated R4 route-layer smoke task.

The local Qwen parser and candidate checks must pass before the selected route
model is called. Retries are disabled by design.
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_cli_support import aggregate_codex_usage_cost  # noqa: E402
from route_experiment.costs import calculate_resource_cost, evaluate_route_candidate  # noqa: E402
from route_experiment.failure_audit import (  # noqa: E402
    estimate_route_proposal_energy,
    recover_partial_move_sequence,
)
from route_experiment.llm_adapter import (  # noqa: E402
    build_r4_route_search_messages,
    replay_move_sequence,
)
from route_experiment.mapgen import generate_map  # noqa: E402
from route_experiment.models import (  # noqa: E402
    EvaluationConfig,
    MapConfig,
    PackageConfig,
    PriceConfig,
    RoadFeatureInputProtocol,
    RouteCandidate,
    RouteMethodResult,
)
from route_experiment.route_model_factory import build_route_model_client  # noqa: E402
from route_experiment.routing import (  # noqa: E402
    build_route_input_package,
    route_chain_reachability,
)
from route_experiment.r4_constraints import build_r4_route_output_constraint  # noqa: E402
from route_experiment.r4_agent import (  # noqa: E402
    audit_partial_edge_slot_output,
    needs_edge_slot_proposal_audit,
    run_r4_route_search,
)
from route_experiment.r4_stepwise import (  # noqa: E402
    audit_r4_stepwise_map_equivalence,
    build_r4_step_output_constraint,
    build_r4_stepwise_messages,
    run_r4_stepwise_route_search,
)
from route_experiment.taskgen import TaskGenerationConfig, generate_order_tasks  # noqa: E402


MODEL_DIR = Path(
    "/tmp/route-r4-mlx-cache/hub/models--mlx-community--Qwen3-4B-Instruct-2507-4bit"
    "/snapshots/50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"
)
QWEN_REVISION = "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"


def _load_env_file(path):
    values = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _road_feature_protocol(road_map, task):
    nodes = {node.node_id: node for node in road_map.nodes}
    entities = []
    features = []
    relations = []
    semantic_index = []

    for node in road_map.nodes:
        entities.append(
            {
                "entity_id": node.node_id,
                "entity_type": "intersection",
                "coordinates": {"x": node.x, "y": node.y},
                "legal_pickup": node.legal_pickup,
                "legal_dropoff": node.legal_dropoff,
            }
        )
        if node.landmark_name:
            entities.append(
                {
                    "entity_id": "landmark:" + node.node_id,
                    "entity_type": "landmark",
                    "name": node.landmark_name,
                    "aliases": list(node.landmark_aliases),
                }
            )
            features.append(
                {
                    "feature_id": "landmark:" + node.node_id,
                    "feature_type": "landmark",
                    "name": node.landmark_name,
                    "aliases": list(node.landmark_aliases),
                }
            )
            relations.append(
                {
                    "relation_type": "landmark_at_node",
                    "source_id": "landmark:" + node.node_id,
                    "target_id": node.node_id,
                }
            )

    for edge in road_map.edges:
        entities.append(
            {
                "entity_id": edge.edge_id,
                "entity_type": "directed_road",
                "from_node_id": edge.from_node,
                "to_node_id": edge.to_node,
            }
        )
        features.append(
            {
                "feature_id": "road_state:" + edge.edge_id,
                "feature_type": "road_state",
                "length_m": edge.length_m,
                "base_speed_mps": edge.base_speed_mps,
                "congestion_level": edge.congestion_level,
                "signal_delay_sec": edge.signal_delay_sec,
            }
        )
        relations.append(
            {
                "relation_type": "directed_connects",
                "source_id": edge.from_node,
                "target_id": edge.to_node,
                "edge_id": edge.edge_id,
            }
        )

    for feature in road_map.roadside_features:
        entities.append(
            {
                "entity_id": "feature:" + feature.feature_id,
                "entity_type": feature.feature_type,
                "name": feature.label,
                "aliases": list(feature.aliases),
            }
        )
        features.append(
            {
                "feature_id": "feature:" + feature.feature_id,
                "feature_type": feature.feature_type,
                "label": feature.label,
                "aliases": list(feature.aliases),
                "side_of_road": feature.side_of_road,
                "orientation": feature.orientation,
                "semantic_role": feature.semantic_role,
            }
        )
        relations.append(
            {
                "relation_type": "feature_located_at",
                "source_id": "feature:" + feature.feature_id,
                "target_id": feature.node_id,
            }
        )
        if feature.paired_node_id:
            relations.append(
                {
                    "relation_type": "opposite_side_candidate",
                    "source_id": "feature:" + feature.feature_id,
                    "target_id": feature.paired_node_id,
                }
            )
        pickup_candidates = []
        for node_id, relation in (
            (feature.node_id, "feature_side"),
            (feature.paired_node_id, "opposite_side"),
        ):
            if node_id and node_id in nodes:
                pickup_candidates.append(
                    {
                        "node_id": node_id,
                        "legal_pickup": nodes[node_id].legal_pickup,
                        "relation_to_feature": relation,
                    }
                )
        semantic_index.append(
            {
                "entry_id": "feature:" + feature.feature_id,
                "entry_type": feature.feature_type,
                "name": feature.label,
                "aliases": list(feature.aliases),
                "side_of_road": feature.side_of_road,
                "orientation": feature.orientation,
                "pickup_candidates": pickup_candidates,
            }
        )

    legal_dropoffs = [node for node in road_map.nodes if node.legal_dropoff]
    for landmark in (node for node in road_map.nodes if node.landmark_name):
        candidates = sorted(
            legal_dropoffs,
            key=lambda node: (
                abs(node.x - landmark.x) + abs(node.y - landmark.y),
                node.node_id,
            ),
        )[:6]
        semantic_index.append(
            {
                "entry_id": "landmark:" + landmark.node_id,
                "entry_type": "landmark",
                "name": landmark.landmark_name,
                "aliases": list(landmark.landmark_aliases),
                "dropoff_candidates": [
                    {
                        "node_id": node.node_id,
                        "legal_dropoff": node.legal_dropoff,
                        "grid_distance_to_landmark": abs(node.x - landmark.x)
                        + abs(node.y - landmark.y),
                    }
                    for node in candidates
                ],
            }
        )

    protocol = RoadFeatureInputProtocol(
        protocol_name="RoadFeatureInputProtocol",
        protocol_version="v1",
        units={"coordinates": "grid_cell", "road_length": "m", "speed": "m/s"},
        context={
            "map_id": road_map.map_id,
            "map_version": road_map.version,
            "road_state_version": road_map.road_state_version,
            "random_seed": 0,
            "task_id": task.task_id,
            "source_time": "static_synthetic_map",
            "boundary": dict(road_map.boundary),
        },
        route_query={
            "task_id": task.task_id,
            "task_type": task.task_type,
            "start_node_id": task.start_node_id,
            "pickup_text": task.pickup_text,
            "dropoff_text": task.dropoff_text,
        },
        entities=entities,
        features=features,
        relations=relations,
        constraints=[
            {"constraint_type": "inclusive_boundary", "value": dict(road_map.boundary)},
            {"constraint_type": "directed_edges_only", "source": "listed_road_edges"},
            {"constraint_type": "legal_pickup_only", "source": "node.legal_pickup"},
            {"constraint_type": "legal_dropoff_only", "source": "node.legal_dropoff"},
        ],
        semantic_index=semantic_index,
        output_contract={
            "schema_version": "route_intent_v1",
            "status_values": ["resolved", "ambiguous", "unresolved"],
            "candidate_ids": "must be selected from semantic_index; never invent IDs",
        },
    )
    return protocol


def _validate_protocol_references(protocol):
    entity_ids = {entity.get("entity_id") for entity in protocol.entities}
    missing = []
    for relation in protocol.relations:
        for field in ("source_id", "target_id"):
            entity_id = relation.get(field)
            if entity_id and entity_id not in entity_ids:
                missing.append(
                    {
                        "relation_type": relation.get("relation_type"),
                        "field": field,
                        "entity_id": entity_id,
                    }
                )
    for entry in protocol.semantic_index:
        for candidate in entry.get("pickup_candidates", []) + entry.get("dropoff_candidates", []):
            if candidate.get("node_id") not in entity_ids:
                missing.append(
                    {
                        "relation_type": "semantic_candidate",
                        "field": "node_id",
                        "entity_id": candidate.get("node_id"),
                    }
                )
    return {"passed": not missing, "missing_references": missing}


def _extract_json(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S)
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise ValueError("json_object_missing")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    return value


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-api",
        "--api-once",
        dest="api_once",
        action="store_true",
        help="allow live route-model calls after local gates pass; stepwise mode may call once per action, under its hard cap",
    )
    parser.add_argument(
        "--route-model-backend",
        choices=("deepseek", "codex_cli"),
        default="deepseek",
        help="route model provider; Codex CLI is supported for stepwise mode only",
    )
    parser.add_argument(
        "--codex-model",
        default="gpt-5.6-luna",
        help="Codex account model when --route-model-backend=codex_cli",
    )
    parser.add_argument(
        "--codex-reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
        default="low",
        help="reasoning effort for the Codex CLI route backend",
    )
    parser.add_argument(
        "--max-planning-attempts",
        type=int,
        choices=(1, 2, 3),
        default=1,
        help="maximum total route proposals, including the first request (default: 1)",
    )
    parser.add_argument(
        "--require-completed-route",
        action="store_true",
        help="for a locally preverified reachable task, require a nonempty completed route",
    )
    parser.add_argument(
        "--route-generation-mode",
        choices=("stepwise", "whole_route"),
        default="stepwise",
        help="stepwise proposals are checked locally before route state advances",
    )
    parser.add_argument(
        "--max-step-api-calls",
        type=int,
        default=32,
        help="hard cap on actual stepwise model requests for this run (default: 32)",
    )
    parser.add_argument(
        "--max-route-actions",
        type=int,
        default=None,
        help="maximum route moves allowed; defaults to --max-step-api-calls for backward compatibility",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        choices=(128, 256, 512, 1024),
        default=128,
        help="DeepSeek output-token cap (default: 128); Codex CLI has no hard output-token cap",
    )
    parser.add_argument("--output", default="outputs/route_layer/r4_stepwise_offline_v2/seed0_semantic_road_feature.json")
    args = parser.parse_args()
    if args.route_generation_mode == "stepwise" and not 1 <= args.max_step_api_calls <= 4096:
        parser.error("--max-step-api-calls must be between 1 and 4096")
    if args.max_route_actions is not None and not 1 <= args.max_route_actions <= 4096:
        parser.error("--max-route-actions must be between 1 and 4096")
    route_action_budget = args.max_route_actions or args.max_step_api_calls
    if args.route_model_backend == "codex_cli" and args.route_generation_mode != "stepwise":
        parser.error("--route-model-backend=codex_cli currently requires --route-generation-mode=stepwise")
    try:
        from mlx_lm import generate, load
        from mlx_lm.sample_utils import make_sampler
    except ImportError as error:
        raise SystemExit(
            "missing_local_qwen_runtime: install mlx_lm in the Python environment used to run this smoke"
        ) from error
    output_path = (ROOT / args.output).resolve()
    if output_path.exists():
        raise SystemExit("refusing_to_overwrite_existing_smoke_artifact")

    env_path = ROOT / "configs/route_layer/llm_api.env"
    if args.route_model_backend == "deepseek":
        env_values = _load_env_file(env_path)
        api_key = env_values.get("LLM_API_KEY", "")
        base_url = env_values.get("LLM_BASE_URL", "https://api.deepseek.com")
        model_name = env_values.get("LLM_MODEL", "deepseek-flash")
    else:
        # Deliberately do not read the API-key file when using the signed-in account.
        api_key = ""
        base_url = ""
        model_name = args.codex_model
    if args.api_once and args.route_model_backend == "deepseek" and not api_key:
        raise SystemExit("missing LLM_API_KEY in configured env file")

    road_map = generate_map(0, 8, MapConfig(size=8, road_sign_count=2, utility_pole_count=2))
    task_batch = generate_order_tasks(
        road_map, 0, TaskGenerationConfig(task_count=10, exact_ratio=0.7)
    )
    task = next(
        task for task in task_batch.tasks
        if task.task_type == "semantic_road_feature_position"
    )
    protocol = _road_feature_protocol(road_map, task)
    protocol_dict = protocol.to_dict()
    protocol_validation = _validate_protocol_references(protocol)
    protocol_json = json.dumps(protocol_dict, ensure_ascii=False, separators=(",", ":"))
    if any(secret_field in protocol_json for secret_field in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")):
        raise RuntimeError("protocol_contains_evaluator_truth")
    if not protocol_validation["passed"]:
        raise RuntimeError("protocol_reference_validation_failed")

    qwen_input = {
        "context": protocol.context,
        "route_query": protocol.route_query,
        "semantic_index": protocol.semantic_index,
        "output_contract": protocol.output_contract,
    }
    query_json = json.dumps(qwen_input, ensure_ascii=False, separators=(",", ":"))
    qwen_messages = [
        {
            "role": "system",
            "content": (
                "地点意图解析器。只输出 JSON。成功时 schema_version 必须为 route_intent_v1，status 必须为 resolved。"
                "pickup.anchor_refs 和 feature_evidence_refs 必须引用匹配道路特征的 entry_id；"
                "dropoff.anchor_refs 和 feature_evidence_refs 必须引用匹配地标的 entry_id。"
                "pickup/dropoff 各从对应候选表选择恰好一个 node_id；必须满足合法性并且两个端点不同。"
                "不得省略证据，不得发明 ID。"
            ),
        },
        {
            "role": "user",
            "content": (
                '按 JSON 结构输出：{"schema_version":"route_intent_v1","status":"resolved",'
                '"pickup":{"anchor_refs":["feature entry_id"],"relation":"near|at|relative_to",'
                '"side":"north|south|east|west|unknown","orientation":"north|south|east|west|unknown",'
                '"candidate_entity_refs":["node_id"]},"dropoff":{"anchor_refs":["landmark entry_id"],'
                '"relation":"near|at|relative_to","side":"unknown","orientation":"unknown",'
                '"candidate_entity_refs":["node_id"]},"route_preferences":["min_travel_time"],'
                '"feature_evidence_refs":["feature entry_id","landmark entry_id"],"confidence":0.0}\n输入：'
                + query_json
            ),
        },
    ]

    qwen_load_started = time.perf_counter()
    qwen_model, tokenizer = load(str(MODEL_DIR))
    qwen_load_ms = (time.perf_counter() - qwen_load_started) * 1000.0
    try:
        qwen_prompt = tokenizer.apply_chat_template(
            qwen_messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
    except TypeError:
        qwen_prompt = tokenizer.apply_chat_template(
            qwen_messages, tokenize=False, add_generation_prompt=True
        )
    qwen_started = time.perf_counter()
    qwen_raw = generate(
        qwen_model,
        tokenizer,
        qwen_prompt,
        max_tokens=220,
        sampler=make_sampler(temp=0.0),
        verbose=False,
    ).strip()
    qwen_elapsed_ms = (time.perf_counter() - qwen_started) * 1000.0
    qwen_result = _extract_json(qwen_raw)

    entry_by_id = {entry["entry_id"]: entry for entry in protocol.semantic_index}
    matched_feature = next(
        (
            entry for entry in protocol.semantic_index
            if entry["entry_type"] in ("road_sign", "utility_pole")
            and any(
                phrase and phrase in (task.pickup_text or "")
                for phrase in [entry["name"]] + entry.get("aliases", [])
            )
        ),
        None,
    )
    matched_landmark = next(
        (
            entry for entry in protocol.semantic_index
            if entry["entry_type"] == "landmark"
            and any(
                phrase and phrase in (task.dropoff_text or "")
                for phrase in [entry["name"]] + entry.get("aliases", [])
            )
        ),
        None,
    )
    if not matched_feature or not matched_landmark:
        raise RuntimeError("smoke_task_semantic_anchor_missing")
    pickup_candidates = matched_feature["pickup_candidates"]
    dropoff_candidates = matched_landmark["dropoff_candidates"]
    selected_pickup = qwen_result.get("pickup", {}).get("candidate_entity_refs", [])
    selected_dropoff = qwen_result.get("dropoff", {}).get("candidate_entity_refs", [])
    pickup_ids = {candidate["node_id"] for candidate in pickup_candidates if candidate["legal_pickup"]}
    dropoff_ids = {candidate["node_id"] for candidate in dropoff_candidates if candidate["legal_dropoff"]}
    evidence = qwen_result.get("feature_evidence_refs", [])
    checks = {
        "schema_version": qwen_result.get("schema_version") == "route_intent_v1",
        "status_resolved": qwen_result.get("status") == "resolved",
        "one_pickup_candidate": len(selected_pickup) == 1,
        "one_dropoff_candidate": len(selected_dropoff) == 1,
        "pickup_allowlisted_and_legal": len(selected_pickup) == 1 and selected_pickup[0] in pickup_ids,
        "dropoff_allowlisted_and_legal": len(selected_dropoff) == 1 and selected_dropoff[0] in dropoff_ids,
        "distinct_endpoints": len(selected_pickup) == 1 and len(selected_dropoff) == 1 and selected_pickup[0] != selected_dropoff[0],
        "feature_anchor_present": matched_feature["entry_id"] in qwen_result.get("pickup", {}).get("anchor_refs", []),
        "landmark_anchor_present": matched_landmark["entry_id"] in qwen_result.get("dropoff", {}).get("anchor_refs", []),
        "feature_evidence_present": matched_feature["entry_id"] in evidence,
        "landmark_evidence_present": matched_landmark["entry_id"] in evidence,
        "pickup_matches_evaluator": len(selected_pickup) == 1 and selected_pickup[0] == task.true_pickup_node_id,
        "dropoff_matches_evaluator": len(selected_dropoff) == 1 and selected_dropoff[0] == task.true_dropoff_node_id,
        "no_truth_fields_in_model_input": all(
            field not in query_json for field in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")
        ),
    }
    route_feature_context = {
        "protocol": {"name": protocol.protocol_name, "version": protocol.protocol_version},
        "pickup_reference": {
            key: matched_feature[key]
            for key in (
                "entry_id", "entry_type", "name", "aliases", "side_of_road",
                "orientation", "anchor_node_id", "pickup_candidates",
            )
            if key in matched_feature
        },
        "dropoff_reference": {
            key: matched_landmark[key]
            for key in (
                "entry_id", "entry_type", "name", "aliases", "anchor_node_id",
                "dropoff_candidates",
            )
            if key in matched_landmark
        },
        "intent": {
            "pickup_anchor_refs": qwen_result.get("pickup", {}).get("anchor_refs", []),
            "dropoff_anchor_refs": qwen_result.get("dropoff", {}).get("anchor_refs", []),
            "pickup_relation": qwen_result.get("pickup", {}).get("relation"),
            "pickup_side": qwen_result.get("pickup", {}).get("side"),
            "feature_evidence_refs": evidence,
        },
    }
    route_search = None
    route_constraint = None
    route_audit_constraint = None
    if all(checks.values()):
        try:
            pickup_id, dropoff_id = selected_pickup[0], selected_dropoff[0]
            resolved_task = replace(
                task,
                pickup_node_id=pickup_id,
                dropoff_node_id=dropoff_id,
                true_pickup_node_id=None,
                true_dropoff_node_id=None,
            )
            package = build_route_input_package(road_map, resolved_task, PackageConfig())
            reachability_preflight = route_chain_reachability(
                package.edges,
                task.start_node_id,
                pickup_id,
                dropoff_id,
                package.blocked_edge_ids,
            )
            require_completed_route = (
                args.require_completed_route
                or args.route_generation_mode == "stepwise"
            )
            if require_completed_route:
                checks["completed_route_preflight_reachable"] = reachability_preflight[
                    "chain_reachable"
                ]
            route_audit_constraint = build_r4_route_output_constraint(
                package,
                pickup_id,
                dropoff_id,
                allow_failed=not require_completed_route,
            )
            map_equivalence_audit = None
            legacy_route_messages = None
            if args.route_generation_mode == "stepwise":
                legacy_route_messages = build_r4_route_search_messages(
                    package,
                    task.start_node_id,
                    pickup_id,
                    dropoff_id,
                    require_completed_route=True,
                )
                route_messages = build_r4_stepwise_messages(
                    package,
                    task.start_node_id,
                    pickup_id,
                    dropoff_id,
                    current_node_id=task.start_node_id,
                    visited_node_ids=[task.start_node_id],
                    route_feature_context=route_feature_context,
                )
                stepwise_payload = json.loads(route_messages[-1]["content"])
                map_equivalence_audit = audit_r4_stepwise_map_equivalence(
                    package, stepwise_payload["m"]
                )
                if not map_equivalence_audit["passed"]:
                    raise RuntimeError("stepwise_map_equivalence_audit_failed")
                route_constraint = build_r4_step_output_constraint(
                    package,
                    route_audit_constraint,
                    current_node_id=task.start_node_id,
                    pickup_seen=task.start_node_id == pickup_id,
                    visited_node_ids=[task.start_node_id],
                    remaining_action_budget=route_action_budget,
                )
                checks["minimum_route_moves_fit_route_action_budget"] = (
                    route_constraint.remaining_minimum_moves is not None
                    and route_constraint.remaining_minimum_moves
                    <= route_action_budget
                )
            else:
                route_messages = build_r4_route_search_messages(
                    package,
                    task.start_node_id,
                    pickup_id,
                    dropoff_id,
                    require_completed_route=require_completed_route,
                )
                route_constraint = route_audit_constraint
            route_json = route_messages[1]["content"]
            serialized_prompt_bytes = sum(
                len(message["content"].encode("utf-8")) for message in route_messages
            )
            legacy_serialized_prompt_bytes = (
                sum(
                    len(message["content"].encode("utf-8"))
                    for message in legacy_route_messages
                )
                if legacy_route_messages is not None
                else None
            )
            if any(
                field in route_json
                for field in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")
            ):
                raise RuntimeError("route_input_contains_evaluator_truth")
            route_search = {
                "input_bytes": len(route_json.encode("utf-8")),
                "serialized_prompt_bytes": serialized_prompt_bytes,
                "legacy_serialized_prompt_bytes": legacy_serialized_prompt_bytes,
                "prompt_byte_reduction_percent": (
                    round(
                        (legacy_serialized_prompt_bytes - serialized_prompt_bytes)
                        / legacy_serialized_prompt_bytes
                        * 100.0,
                        2,
                    )
                    if legacy_serialized_prompt_bytes
                    else None
                ),
                "package_nodes": len(package.nodes),
                "package_edges": len(package.edges),
                "package_expansion_count": package.expansion_count,
                "reachability_preflight": reachability_preflight,
                "generation_mode": args.route_generation_mode,
                "encoding": "directional-cost-grid-step-v1" if args.route_generation_mode == "stepwise" else "directional-cost-grid-v1",
                "map_equivalence_audit": map_equivalence_audit,
                "output_constraint": {
                    "mode": (
                        "codex_cli_json_schema_step_direction_schema"
                        if args.route_model_backend == "codex_cli"
                        else "deepseek_beta_strict_step_direction_schema"
                        if args.route_generation_mode == "stepwise"
                        else "deepseek_beta_strict_directed_edge_schema"
                    ),
                    "allowed_directed_edge_count": len(
                        route_audit_constraint.edge_id_by_slot_code
                    ),
                    "initial_allowed_direction_count": (
                        len(route_constraint.edge_by_direction)
                        if args.route_generation_mode == "stepwise"
                        else None
                    ),
                    "initial_filtered_directions": (
                        route_constraint.filtered_directions
                        if args.route_generation_mode == "stepwise"
                        else None
                    ),
                    "minimum_remaining_moves_lower_bound": (
                        route_constraint.remaining_minimum_moves
                        if args.route_generation_mode == "stepwise"
                        else None
                    ),
                    "initial_action_budget": (
                        route_constraint.remaining_action_budget
                        if args.route_generation_mode == "stepwise"
                        else None
                    ),
                    "max_route_edges": route_audit_constraint.max_route_edges,
                    "schema_scope": "reachable_budget_fitting_current_directions" if args.route_generation_mode == "stepwise" else "complete_route",
                    "schema_bytes": route_constraint.schema_bytes,
                    "schema_sha256": route_constraint.schema_sha256,
                    "action_representation": (
                        "single_compass_direction" if args.route_generation_mode == "stepwise"
                        else "three_character_base36_edge_slot"
                    ),
                },
                "max_output_tokens": (
                    None if args.route_model_backend == "codex_cli" else args.max_output_tokens
                ),
                "output_token_hard_limit": (
                    None if args.route_model_backend == "codex_cli" else args.max_output_tokens
                ),
                "max_output_tokens_total_upper_bound": (
                    None
                    if args.route_model_backend == "codex_cli"
                    else args.max_output_tokens * args.max_step_api_calls
                    if args.route_generation_mode == "stepwise"
                    else args.max_output_tokens * args.max_planning_attempts
                ),
                "max_step_api_calls": args.max_step_api_calls if args.route_generation_mode == "stepwise" else None,
                "max_route_actions": route_action_budget if args.route_generation_mode == "stepwise" else None,
                "retries": 0,
                "messages": route_messages,
            }
            checks["explicit_route_input_built_without_truth"] = True
        except Exception as input_error:
            checks["explicit_route_input_built_without_truth"] = False
            route_search = {
                "status": "local_input_build_failed",
                "error": type(input_error).__name__ + ":" + str(input_error)[:200],
            }
    else:
        checks["explicit_route_input_built_without_truth"] = False
    local_gate_passed = all(checks.values())
    provider_key = args.route_model_backend
    provider_result = {
        "backend": provider_key,
        "status": "not_called_local_gate_failed" if not local_gate_passed else "not_called",
    }
    result = {
        "schema_version": "route_r4_smoke_result_v2",
        "status": "local_gate_failed" if not local_gate_passed else "local_gate_passed",
        "formal_result": False,
        "api_request_count": 0,
        "model_invocation_count": 0,
        "route_model_backend": provider_key,
        "map": road_map.to_dict(),
        "task_public": protocol.route_query,
        "protocol": protocol_dict,
        "model_input": qwen_input,
        "route_search": route_search,
        "qwen": {
            "model": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
            "revision": QWEN_REVISION,
            "input_bytes": len(query_json.encode("utf-8")),
            "output_tokens_local_estimate": len(tokenizer.encode(qwen_raw)),
            "model_load_ms": round(qwen_load_ms, 3),
            "inference_ms": round(qwen_elapsed_ms, 3),
            "raw_output": qwen_raw,
            "parsed_intent": qwen_result,
            "checks": checks,
        "protocol_validation": protocol_validation,
        },
        provider_key: provider_result,
    }
    _write_json(output_path, result)
    if not local_gate_passed:
        print(json.dumps({"status": "local_gate_failed", "checks": checks, "output": str(output_path)}, ensure_ascii=False))
        return 2
    if not args.api_once:
        print(json.dumps({"status": "local_gate_passed_api_not_requested", "checks": checks, "output": str(output_path)}, ensure_ascii=False))
        return 0

    pickup_id, dropoff_id = selected_pickup[0], selected_dropoff[0]
    base_host = urlparse(base_url).hostname if base_url else None
    if args.route_model_backend == "deepseek" and base_host:
        bypass = set(filter(None, os.environ.get("NO_PROXY", "").split(",")))
        bypass.add(base_host)
        os.environ["NO_PROXY"] = ",".join(sorted(bypass))
        os.environ["no_proxy"] = os.environ["NO_PROXY"]
    trace_dir = output_path.parent / (output_path.stem + "_" + provider_key + "_traces")
    client = build_route_model_client(
        backend=args.route_model_backend,
        generation_mode=args.route_generation_mode,
        model=model_name,
        trace_dir=trace_dir,
        api_key=api_key,
        base_url=base_url,
        reasoning_effort=args.codex_reasoning_effort,
        timeout_sec=300 if args.route_model_backend == "codex_cli" else 60,
        max_output_tokens=args.max_output_tokens,
        route_constraint=route_constraint,
    )
    price_data = json.loads(
        (ROOT / "configs/route_layer/price_smoke_v1.json").read_text()
    )
    prices = PriceConfig(
        **{
            key: value
            for key, value in price_data.items()
            if key in PriceConfig.__dataclass_fields__
        }
    )
    result["status"] = "route_model_request_started"
    result["api_request_count"] = 0
    result["model_invocation_count"] = 0
    result[provider_key] = {
        "backend": provider_key,
        "request_count": 0,
        "model_invocation_count": 0,
        "route_generation_mode": args.route_generation_mode,
        "max_planning_attempts": args.max_planning_attempts if args.route_generation_mode == "whole_route" else None,
        "max_step_api_calls": args.max_step_api_calls if args.route_generation_mode == "stepwise" else None,
        "max_route_actions": route_action_budget if args.route_generation_mode == "stepwise" else None,
        "model_requested": model_name,
        "base_host": base_host,
        "output_constraint_mode": route_search["output_constraint"]["mode"],
        "output_token_hard_limit": None if provider_key == "codex_cli" else args.max_output_tokens,
        "account_cost_status": (
            "Codex account usage is not priced by the CLI"
            if provider_key == "codex_cli"
            else "priced_by_configured_provider_rate"
        ),
        "require_completed_route": args.require_completed_route,
        "constraint_schema_sha256": route_constraint.schema_sha256,
        "constraint_schema_bytes": route_constraint.schema_bytes,
        "allowed_directed_edge_count": len(route_audit_constraint.edge_id_by_slot_code),
        "initial_allowed_direction_count": (
            len(route_constraint.edge_by_direction)
            if args.route_generation_mode == "stepwise"
            else None
        ),
        "status": "request_started",
        "attempts": [],
    }
    provider_result = result[provider_key]
    _write_json(output_path, result)

    def persist_attempts(snapshot):
        attempts = snapshot["attempts"]
        for attempt in attempts:
            if "api_cost_estimate_amount" not in attempt and "input_tokens" in attempt:
                if provider_key == "deepseek":
                    attempt["api_cost_estimate_amount"] = (
                        attempt.get("input_tokens", 0) + attempt.get("output_tokens", 0)
                    ) * prices.api_price_per_token
                else:
                    attempt["api_cost_estimate_amount"] = None
                    attempt["account_cost_status"] = "not_reported_by_codex_cli"
            if attempt.get("replay") and (
                attempt.get("status") == "route_proposal_invalid"
                or args.route_generation_mode == "stepwise"
            ):
                energy_estimate = estimate_route_proposal_energy(
                    road_map, attempt["replay"], EvaluationConfig()
                )
                energy_estimate["hypothetical_electricity_cost_amount"] = (
                    energy_estimate.get("route_energy_estimate_kwh", 0.0)
                    * prices.electricity_price_per_kwh
                )
                attempt["proposal_energy_estimate"] = energy_estimate
            if needs_edge_slot_proposal_audit(attempt.get("status", "")) and "partial_proposal_audit" not in attempt:
                if route_audit_constraint is not None:
                    partial_slot_audit = audit_partial_edge_slot_output(
                        road_map,
                        task.start_node_id,
                        pickup_id,
                        dropoff_id,
                        attempt.get("raw_output", ""),
                        route_audit_constraint,
                    )
                    if partial_slot_audit["complete_slot_count"]:
                        partial_slot_audit["energy_estimate"][
                            "hypothetical_electricity_cost_amount"
                        ] = (
                            partial_slot_audit["energy_estimate"].get(
                                "route_energy_estimate_kwh", 0.0
                            )
                            * prices.electricity_price_per_kwh
                        )
                        attempt["partial_proposal_audit"] = partial_slot_audit
                partial_moves = recover_partial_move_sequence(
                    {"content": attempt.get("raw_output", "")}
                )
                if partial_moves and "partial_proposal_audit" not in attempt:
                    partial_replay = replay_move_sequence(
                        road_map, task.start_node_id, partial_moves
                    )
                    partial_energy = estimate_route_proposal_energy(
                        road_map, partial_replay, EvaluationConfig()
                    )
                    partial_energy["hypothetical_electricity_cost_amount"] = (
                        partial_energy.get("route_energy_estimate_kwh", 0.0)
                        * prices.electricity_price_per_kwh
                    )
                    attempt["partial_proposal_audit"] = {
                        "recovered_move_sequence": partial_moves,
                        "replay": partial_replay,
                        "energy_estimate": partial_energy,
                        "interpretation": "partial model proposal only; not a valid route or measured energy",
                    }

        api_usage_complete = all(
            "input_tokens" in item and "output_tokens" in item
            for item in attempts
        )
        api_cost = (
            sum(
                (item.get("input_tokens", 0) + item.get("output_tokens", 0))
                * prices.api_price_per_token
                for item in attempts
            )
            if api_usage_complete and provider_key == "deepseek"
            else None
        )
        codex_rate_card_estimate = (
            aggregate_codex_usage_cost(attempts, model_name)
            if provider_key == "codex_cli"
            else None
        )
        result["status"] = snapshot["status"]
        result["model_invocation_count"] = snapshot["api_request_count"]
        result["api_request_count"] = (
            snapshot["api_request_count"] if provider_key == "deepseek" else 0
        )
        result["formal_result"] = False
        provider_result.update(
            {
                "status": snapshot["status"],
                "request_count": snapshot["api_request_count"],
                "model_invocation_count": snapshot["api_request_count"],
                "attempts": attempts,
                "usage": snapshot["usage"],
                "route": snapshot.get("route"),
                "replay": snapshot.get("replay"),
                "validation": snapshot.get("validation"),
                "steps": snapshot.get("steps", []),
                "api_call_limit": snapshot.get("api_call_limit"),
                "route_action_budget": snapshot.get("route_action_budget"),
                "route_action_budget_mode": snapshot.get("route_action_budget_mode"),
                "estimated_cost": {
                    "api_cost_amount": api_cost,
                    "codex_rate_card_estimate": codex_rate_card_estimate,
                    "account_cost_status": (
                        "estimated_by_configured_api_rate"
                        if provider_key == "deepseek"
                        else "actual_codex_account_invoice_or_allowance_not_reported_by_cli"
                    ),
                    "local_qwen_inference_compute_cost_amount": qwen_elapsed_ms
                    * prices.compute_price_per_ms,
                    "electricity_cost_amount": None,
                    "route_electricity_status": "not_evaluated_no_validated_route",
                    "currency": prices.currency,
                    "pricing_version": prices.pricing_version,
                    "note": "invalid proposals carry separate hypothetical energy estimates; route electricity cost is counted only after validation",
                },
            }
        )
        _write_json(output_path, result)

    try:
        if args.route_generation_mode == "stepwise":
            route_search_result = run_r4_stepwise_route_search(
                client,
                package,
                road_map,
                resolved_task,
                pickup_id,
                dropoff_id,
                max_api_calls=args.max_step_api_calls,
                max_route_actions=(
                    route_action_budget
                    if args.max_route_actions is not None
                    else None
                ),
                blocked_edge_ids=package.blocked_edge_ids,
                on_attempt=persist_attempts,
                route_feature_context=route_feature_context,
            )
        else:
            route_search_result = run_r4_route_search(
                client,
                route_messages,
                road_map,
                resolved_task,
                pickup_id,
                dropoff_id,
                max_attempts=args.max_planning_attempts,
                blocked_edge_ids=package.blocked_edge_ids,
                on_attempt=persist_attempts,
                route_constraint=route_constraint,
            )
        persist_attempts(route_search_result)
        if route_search_result["status"] == "route_validated":
            candidate = RouteCandidate(**route_search_result["route"])
            evaluation = evaluate_route_candidate(road_map, candidate, EvaluationConfig())
            method_result = RouteMethodResult(
                task_id=task.task_id,
                method_name="r4_smoke_" + provider_key,
                status="completed",
                routes=[candidate],
                best_route_id=candidate.route_id,
            )
            route_usage = route_search_result["usage"]
            usage, costs = calculate_resource_cost(
                method_result,
                evaluation,
                prices,
                input_tokens=route_usage["input_tokens"],
                output_tokens=route_usage["output_tokens"],
                llm_calls=route_usage["llm_calls"],
                llm_latency_ms=route_usage["llm_latency_ms"],
                compute_time_ms=qwen_elapsed_ms,
            )
            cost_dict = costs.to_dict()
            cost_dict["route_electricity_status"] = "calculated_from_validated_route"
            if provider_key == "codex_cli":
                cost_dict["api_cost_amount"] = None
                cost_dict["total_monetary_cost"] = None
                cost_dict["account_cost_status"] = (
                    "actual_codex_account_invoice_or_allowance_not_reported_by_cli"
                )
                cost_dict["codex_rate_card_estimate"] = aggregate_codex_usage_cost(
                    route_search_result.get("attempts", []), model_name
                )
                cost_dict["api_cost_amount_is_invoice"] = False
            provider_result.update(
                {
                    "evaluation": evaluation.to_dict(),
                    "resource_usage": usage.to_dict(),
                    "estimated_cost": cost_dict,
                }
            )
            _write_json(output_path, result)
    except Exception as error:
        detail = str(error).replace(api_key, "[redacted]") if api_key else str(error)
        provider_result.update(
            {"status": "local_route_workflow_error", "error": type(error).__name__ + ":" + detail[:300]}
        )
        result["status"] = "local_route_workflow_error"
        result["formal_result"] = False
        _write_json(output_path, result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "api_request_count": result["api_request_count"],
                "model_invocation_count": result["model_invocation_count"],
                "input_tokens": provider_result.get("usage", {}).get("input_tokens", 0),
                "output_tokens": provider_result.get("usage", {}).get("output_tokens", 0),
                "output": str(output_path),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "route_validated" else 3


if __name__ == "__main__":
    raise SystemExit(main())
