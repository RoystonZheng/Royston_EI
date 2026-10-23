#!/usr/bin/env python3
"""Build a no-request R4 stepwise preflight from the frozen 100x100 task."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for candidate in (ROOT, SCRIPTS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.r4_constraints import build_r4_route_output_constraint
from route_experiment.r4_stepwise import (
    audit_r4_stepwise_map_equivalence,
    build_r4_step_output_constraint,
    build_r4_stepwise_messages,
)
from run_r4_codex_formal_checkpoint import INTENT_RESULT, prepare
from run_route_r4_smoke import _road_feature_protocol, _validate_protocol_references


DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2"
PURE_LLM_SEED = ROOT / "outputs/route_layer/pure_llm_experiment_v8/seeds/seed_0"
ROUTE_ACTION_BUDGET = 512
REQUEST_LIMIT = 1


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _feature_context(protocol, intent):
    entries = {row["entry_id"]: row for row in protocol.semantic_index}
    pickup = entries[intent["pickup_anchor_ref"]]
    dropoff = entries[intent["dropoff_anchor_ref"]]
    return {
        "protocol": {"name": protocol.protocol_name, "version": protocol.protocol_version},
        "pickup_reference": {
            key: pickup[key]
            for key in (
                "entry_id", "entry_type", "name", "aliases", "side_of_road",
                "orientation", "anchor_node_id", "pickup_candidates",
            )
            if key in pickup
        },
        "dropoff_reference": {
            key: dropoff[key]
            for key in (
                "entry_id", "entry_type", "name", "aliases", "anchor_node_id",
                "dropoff_candidates",
            )
            if key in dropoff
        },
        "intent": {
            "pickup_anchor_refs": [intent["pickup_anchor_ref"]],
            "dropoff_anchor_refs": [intent["dropoff_anchor_ref"]],
            "pickup_relation": intent.get("relation"),
            "pickup_side": intent.get("side"),
            "feature_evidence_refs": intent.get("feature_evidence_refs", []),
        },
    }


def build_preflight(output_dir: Path):
    item = prepare()
    road_map = item["road_map"]
    task = item["task"]
    package = item["package"]
    intent = item["intent_result"]["validation"]
    if not intent.get("valid"):
        raise RuntimeError("qwen_local_intent_validation_failed")

    protocol = _road_feature_protocol(road_map, task)
    protocol_audit = _validate_protocol_references(protocol)
    if not protocol_audit["passed"]:
        raise RuntimeError("road_feature_protocol_reference_validation_failed")
    feature_context = _feature_context(protocol, intent)
    full_constraint = build_r4_route_output_constraint(
        package, task.pickup_node_id, task.dropoff_node_id, allow_failed=False
    )
    route_constraint = build_r4_step_output_constraint(
        package,
        full_constraint,
        current_node_id=task.start_node_id,
        pickup_seen=task.start_node_id == task.pickup_node_id,
        visited_node_ids=[task.start_node_id],
        remaining_action_budget=ROUTE_ACTION_BUDGET,
    )
    messages = build_r4_stepwise_messages(
        package,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
        current_node_id=task.start_node_id,
        visited_node_ids=[task.start_node_id],
        remaining_action_budget=ROUTE_ACTION_BUDGET,
        route_feature_context=feature_context,
    )
    payload = json.loads(messages[-1]["content"])
    map_audit = audit_r4_stepwise_map_equivalence(package, payload["m"])
    prompt = CodexCliRouteStepClient._render_prompt(messages)
    prompt_bytes = prompt.encode("utf-8")
    prompt_has_truth = any(
        field in prompt
        for field in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")
    )
    if not map_audit["passed"]:
        raise RuntimeError("stepwise_map_equivalence_audit_failed")
    if prompt_has_truth:
        raise RuntimeError("evaluator_truth_leaked_to_route_prompt")

    traditional_map = item["map_path"]
    traditional_tasks = item["task_path"]
    shared_map = PURE_LLM_SEED / traditional_map.name
    shared_tasks = PURE_LLM_SEED / traditional_tasks.name
    shared_inputs = {
        "traditional_map_sha256": _sha256(traditional_map),
        "pure_llm_map_sha256": _sha256(shared_map),
        "map_byte_identical": _sha256(traditional_map) == _sha256(shared_map),
        "traditional_orders_sha256": _sha256(traditional_tasks),
        "pure_llm_orders_sha256": _sha256(shared_tasks),
        "orders_byte_identical": _sha256(traditional_tasks) == _sha256(shared_tasks),
    }

    parsed_intent = {
        "schema_version": "route_intent_v1",
        "status": "resolved",
        "pickup": {
            "anchor_refs": [intent["pickup_anchor_ref"]],
            "relation": intent.get("relation"),
            "side": intent.get("side"),
            "candidate_entity_refs": [task.pickup_node_id],
        },
        "dropoff": {
            "anchor_refs": [intent["dropoff_anchor_ref"]],
            "relation": "near",
            "side": "unknown",
            "candidate_entity_refs": [task.dropoff_node_id],
        },
        "feature_evidence_refs": intent.get("feature_evidence_refs", []),
        "confidence": intent.get("confidence"),
    }
    serialized_bytes = sum(len(row["content"].encode("utf-8")) for row in messages)
    schema_bytes = json.dumps(
        route_constraint.schema, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    gates = {
        "qwen_intent_valid": bool(intent.get("valid")),
        "protocol_references_valid": protocol_audit["passed"],
        "intent_pickup_is_legal_candidate": task.pickup_node_id in {
            row.node_id for row in package.nodes if row.legal_pickup
        },
        "intent_dropoff_is_legal_candidate": task.dropoff_node_id in {
            row.node_id for row in package.nodes if row.legal_dropoff
        },
        "route_chain_reachable": route_constraint.remaining_minimum_moves is not None,
        "minimum_route_fits_action_budget": (
            route_constraint.remaining_minimum_moves is not None
            and route_constraint.remaining_minimum_moves <= ROUTE_ACTION_BUDGET
        ),
        "compact_map_semantically_equivalent": map_audit["passed"],
        "protocol_context_in_step_payload": payload.get("i") == feature_context,
        "evaluator_truth_absent_from_route_prompt": not prompt_has_truth,
        "shared_map_matches_pure_llm": shared_inputs["map_byte_identical"],
        "shared_orders_match_pure_llm": shared_inputs["orders_byte_identical"],
    }
    result = {
        "schema_version": "route_r4_smoke_result_v2",
        "status": "local_gate_passed_api_not_requested" if all(gates.values()) else "local_gate_failed",
        "formal_result": False,
        "formal_batch_started": False,
        "api_request_count": 0,
        "model_invocation_count": 0,
        "route_model_backend": "codex_cli",
        "map": road_map.to_dict(),
        "task_public": {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "start_node_id": task.start_node_id,
            "pickup_text": task.pickup_text,
            "dropoff_text": task.dropoff_text,
        },
        "qwen": {
            "model": item["intent_result"].get("model"),
            "revision": item["intent_result"].get("revision"),
            "parsed_intent": parsed_intent,
            "checks": {
                "intent_valid": bool(intent.get("valid")),
                "protocol_references_valid": protocol_audit["passed"],
                "pickup_is_legal_candidate": gates["intent_pickup_is_legal_candidate"],
                "dropoff_is_legal_candidate": gates["intent_dropoff_is_legal_candidate"],
            },
        },
        "protocol_audit": protocol_audit,
        "shared_input_audit": shared_inputs,
        "route_search": {
            "generation_mode": "stepwise",
            "package_nodes": len(package.nodes),
            "package_edges": len(package.edges),
            "max_step_api_calls": REQUEST_LIMIT,
            "max_route_actions": ROUTE_ACTION_BUDGET,
            "serialized_prompt_bytes": serialized_bytes,
            "route_prompt_bytes": len(prompt_bytes),
            "route_prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
            "reachability_preflight": {
                "chain_reachable": gates["route_chain_reachable"],
                "minimum_remaining_moves": route_constraint.remaining_minimum_moves,
            },
            "map_equivalence_audit": map_audit,
            "output_constraint": {
                "mode": "codex_cli_json_schema_step_direction_schema",
                "initial_allowed_directions": list(route_constraint.edge_by_direction),
                "initial_filtered_directions": route_constraint.filtered_directions,
                "minimum_remaining_moves_lower_bound": route_constraint.remaining_minimum_moves,
                "initial_action_budget": route_constraint.remaining_action_budget,
                "schema_bytes": len(schema_bytes),
                "schema_sha256": route_constraint.schema_sha256,
            },
            "messages": messages,
            "rendered_prompt": prompt,
        },
        "codex_cli": {
            "status": "not_called",
            "max_step_api_calls": REQUEST_LIMIT,
            "attempts": [],
            "usage": {
                "usage_complete": True,
                "llm_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "serialized_user_input_bytes": 0,
                "serialized_messages_bytes": 0,
                "client_retries": 0,
            },
            "estimated_cost": {
                "route_electricity_status": "not_evaluated_no_validated_route",
                "electricity_cost_amount": None,
            },
        },
        "preflight_gates": gates,
        "cost_boundary": {
            "route_model_requests": 0,
            "route_model_token_usage": "not_applicable_no_request",
            "actual_codex_account_billing": "not_reported_by_cli",
            "route_electricity": "not_evaluated_no_model_route",
        },
        "limitations": [
            "offline input and validator preflight only; no Codex route request was sent",
            "one-request cap can produce only a partial route and is not a complete-route experiment",
            "this task checkpoint is not a formal multi-seed R4 result",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "preflight.json", result)
    _write_json(output_dir / "route_messages.json", {"messages": messages})
    _write_json(output_dir / "route_schema.json", route_constraint.schema)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit("refusing_to_overwrite_existing_preflight")
    result = build_preflight(args.output_dir)
    print(json.dumps({
        "status": result["status"],
        "formal_result": result["formal_result"],
        "api_request_count": result["api_request_count"],
        "map": result["map"]["map_id"],
        "task_id": result["task_public"]["task_id"],
        "route_actions": result["route_search"]["max_route_actions"],
        "request_limit": result["route_search"]["max_step_api_calls"],
        "minimum_route_moves": result["route_search"]["output_constraint"]["minimum_remaining_moves_lower_bound"],
        "gates": result["preflight_gates"],
        "output_dir": str(args.output_dir),
    }, ensure_ascii=False))
    return 0 if result["status"] == "local_gate_passed_api_not_requested" else 2


if __name__ == "__main__":
    raise SystemExit(main())
