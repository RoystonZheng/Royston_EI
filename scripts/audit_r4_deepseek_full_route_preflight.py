#!/usr/bin/env python3
"""Zero-request sizing audit for the current DeepSeek full-route R4 protocol.

This only reconstructs frozen inputs and calls local serializers/validators. It
does not instantiate a model client, read API keys, or send network requests.
"""

import hashlib
import json
import math
import sys
from collections import defaultdict, deque
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from route_experiment.llm_adapter import build_r4_route_search_messages  # noqa: E402
from route_experiment.models import (  # noqa: E402
    Edge,
    Node,
    PackageConfig,
    RoadMap,
    RoadsideFeature,
    VehicleOrderTask,
)
from route_experiment.r4_constraints import build_r4_route_output_constraint  # noqa: E402
from route_experiment.routing import (  # noqa: E402
    build_route_input_package,
    route_chain_reachability,
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_map(path):
    raw = read_json(path)
    return RoadMap(
        map_id=raw["map_id"],
        version=raw["version"],
        size=raw["size"],
        nodes=[Node(**item) for item in raw["nodes"]],
        edges=[Edge(**item) for item in raw["edges"]],
        boundary=raw["boundary"],
        road_state_version=raw["road_state_version"],
        roadside_features=[RoadsideFeature(**item) for item in raw["roadside_features"]],
    )


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)]


def summary(values):
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "sum": sum(ordered),
        "min": min(ordered),
        "median": percentile(ordered, 0.5),
        "p90": percentile(ordered, 0.9),
        "p95": percentile(ordered, 0.95),
        "max": max(ordered),
        "mean": (sum(ordered) / len(ordered)) if ordered else None,
    }


def directed_hops(edges, start, goal):
    if start == goal:
        return 0
    outgoing = defaultdict(list)
    for edge in edges:
        outgoing[edge.from_node].append(edge.to_node)
    queue = deque([(start, 0)])
    visited = {start}
    while queue:
        node, distance = queue.popleft()
        for neighbor in outgoing.get(node, ()):
            if neighbor == goal:
                return distance + 1
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, distance + 1))
    return None


def canonical_bytes(value):
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def build_direction_candidate(messages, pickup_node_id, dropoff_node_id, max_edges):
    """Build an unadopted size-only direction-string request candidate."""

    candidate_messages = [dict(message) for message in messages]
    system = candidate_messages[0]["content"]
    system = system.replace(
        "槽码是大写 base36 而不是十六进制，例如槽位 35=00Z、36=010、255=073；",
        "路线用 E/W/N/S 表示方向，每条有向边一个字符；不得由坐标推断缺失或反向出边。",
    )
    block_start = system.index("completed 时必须调用 submit_r4_route 工具")
    block_end = system.index("边序列仍须连续", block_start)
    replacement = (
        "completed 时必须调用 submit_r4_route 工具，并用 move_sequence 返回路线：每一步一个 E/W/N/S 字符，不加分隔符；"
        "step_check_sequence 每条边一个字符 '1'。方向字符本身不保证当前节点存在该有向边；本地程序会逐边检查实际出边、"
        "连续性、边界、上车/下车顺序和重复节点，任何检查失败都拒绝整条提议，不使用传统算法兜底。"
    )
    system = system[:block_start] + replacement + system[block_end:]
    candidate_messages[0]["content"] = system

    context = json.loads(candidate_messages[1]["content"])
    completed = context["output_contract"]["completed"]
    completed.pop("edge_slot_sequence", None)
    completed["move_sequence"] = "one E/W/N/S direction character per edge, without separators"
    context["output_contract"].pop("edge_slot_encoding", None)
    candidate_messages[1]["content"] = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    parameters = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["completed"]},
            "pickup_node_id": {"type": "string", "enum": [pickup_node_id]},
            "dropoff_node_id": {"type": "string", "enum": [dropoff_node_id]},
            "move_sequence": {
                "type": "string",
                "pattern": r"^[EWNS]{{1,{0}}}$".format(max_edges),
            },
            "step_check_sequence": {
                "type": "string",
                "pattern": r"^1{{1,{0}}}$".format(max_edges),
            },
            "failure_reason_code": {"type": "string", "enum": ["none"]},
        },
        "required": [
            "status",
            "pickup_node_id",
            "dropoff_node_id",
            "move_sequence",
            "step_check_sequence",
            "failure_reason_code",
        ],
        "additionalProperties": False,
    }
    tool_definition = {
        "type": "function",
        "function": {
            "name": "submit_r4_route",
            "strict": True,
            "description": "Submit one complete route as one compass direction per directed edge.",
            "parameters": parameters,
        },
    }
    return candidate_messages, tool_definition


def task_candidates(seed_dir, seed):
    order_data = read_json(seed_dir / "order_tasks_seed_{0}.json".format(seed))
    truth = {row["task_id"]: row for row in order_data["ground_truth"]}
    candidates = []
    for raw in order_data["tasks"]:
        resolved = dict(raw)
        resolved.update(truth[raw["task_id"]])
        resolved["pickup_node_id"] = truth[raw["task_id"]]["true_pickup_node_id"]
        resolved["dropoff_node_id"] = truth[raw["task_id"]]["true_dropoff_node_id"]
        candidates.append((VehicleOrderTask(**resolved), (), "order"))

    scenario_data = read_json(seed_dir / "formal_scenarios_seed_{0}.json".format(seed))
    for scenario in scenario_data["scenarios"]:
        blocked = tuple(scenario.get("blocked_edge_ids", ()))
        for raw in scenario["tasks"]:
            candidates.append((VehicleOrderTask(**raw), blocked, scenario["scenario_type"]))
    return candidates


def full_map_baseline_lengths(seed_dir, seed):
    lengths = {}
    for filename in (
        "traditional_order_routes_costs_seed_{0}.json".format(seed),
        "traditional_formal_routes_costs_seed_{0}.json".format(seed),
    ):
        data = read_json(seed_dir / filename)
        for row in data["results"]:
            for method in row["methods"]:
                if method["method_name"] != "full_map_dijkstra":
                    continue
                if method["status"] == "completed" and method.get("routes"):
                    route = next(
                        (
                            item
                            for item in method["routes"]
                            if item["route_id"] == method.get("best_route_id")
                        ),
                        method["routes"][0],
                    )
                    lengths[row["task_id"]] = len(route.get("edge_sequence", ()))
                break
    return lengths


def audit(seed_root, output_path):
    rows = []
    excluded = []
    per_seed = {}

    for seed in range(10):
        seed_dir = seed_root / "seed_{0}".format(seed)
        road_map = load_map(seed_dir / "map_100x100_seed_{0}.json".format(seed))
        baseline_lengths = full_map_baseline_lengths(seed_dir, seed)
        seed_count = 0
        for task, blocked_edge_ids, source_type in task_candidates(seed_dir, seed):
            if source_type == "illegal_endpoint":
                excluded.append({"seed": seed, "task_id": task.task_id, "reason": source_type})
                continue
            package = build_route_input_package(
                road_map,
                task,
                PackageConfig(),
                blocked_edge_ids=blocked_edge_ids,
            )
            reachability = route_chain_reachability(
                package.edges,
                task.start_node_id,
                task.pickup_node_id,
                task.dropoff_node_id,
                package.blocked_edge_ids,
            )
            if not reachability["chain_reachable"]:
                excluded.append({"seed": seed, "task_id": task.task_id, "reason": "unreachable"})
                continue

            # Match the live runner's public route task: evaluator-only truth
            # fields are removed before package/messages are serialized.
            public_task = replace(
                task,
                true_pickup_node_id=None,
                true_dropoff_node_id=None,
            )
            package = build_route_input_package(
                road_map,
                public_task,
                PackageConfig(),
                blocked_edge_ids=blocked_edge_ids,
            )
            messages = build_r4_route_search_messages(
                package,
                task.start_node_id,
                task.pickup_node_id,
                task.dropoff_node_id,
                require_completed_route=True,
            )
            route_constraint = build_r4_route_output_constraint(
                package,
                task.pickup_node_id,
                task.dropoff_node_id,
                allow_failed=False,
            )
            message_bytes = sum(len(message["content"].encode("utf-8")) for message in messages)
            tool_definition = route_constraint.tool_definition()
            request_body = {
                "model": "deepseek-v4-flash",
                "messages": messages,
                "temperature": 0,
                "stream": False,
                "reasoning_effort": "none",
                "max_tokens": 1024,
                "tools": [tool_definition],
                "tool_choice": route_constraint.tool_choice,
            }
            request_body_bytes = len(
                json.dumps(request_body, ensure_ascii=False).encode("utf-8")
            )
            direction_messages, direction_tool = build_direction_candidate(
                messages,
                task.pickup_node_id,
                task.dropoff_node_id,
                route_constraint.max_route_edges,
            )
            direction_message_bytes = sum(
                len(message["content"].encode("utf-8")) for message in direction_messages
            )
            direction_request_body = dict(request_body)
            direction_request_body["messages"] = direction_messages
            direction_request_body["tools"] = [direction_tool]
            direction_request_body_bytes = len(
                json.dumps(direction_request_body, ensure_ascii=False).encode("utf-8")
            )

            first_code = min(route_constraint.edge_by_slot_code)
            lower_bound_hops = (
                directed_hops(package.edges, task.start_node_id, task.pickup_node_id)
                + directed_hops(package.edges, task.pickup_node_id, task.dropoff_node_id)
            )
            baseline_hops = baseline_lengths.get(task.task_id)
            if baseline_hops is None:
                raise RuntimeError("missing_completed_full_map_baseline:{0}".format(task.task_id))
            # This is a size-only fixture: the repeated edge code passes the
            # schema's alphabet/count checks but is not claimed to be a route.
            size_fixture = {
                "status": "completed",
                "pickup_node_id": task.pickup_node_id,
                "dropoff_node_id": task.dropoff_node_id,
                "edge_slot_sequence": first_code * lower_bound_hops,
                "step_check_sequence": "1" * lower_bound_hops,
                "failure_reason_code": "none",
            }
            normalized_fixture = route_constraint.normalize_payload(size_fixture)
            edge_json_bytes = canonical_bytes(size_fixture)
            move_json_bytes = canonical_bytes(
                {
                    "status": "completed",
                    "pickup_node_id": task.pickup_node_id,
                    "dropoff_node_id": task.dropoff_node_id,
                    "move_sequence": "E" * lower_bound_hops,
                    "step_check_sequence": "1" * lower_bound_hops,
                    "failure_reason_code": "none",
                }
            )
            baseline_edge_json_bytes = canonical_bytes(
                {
                    "status": "completed",
                    "pickup_node_id": task.pickup_node_id,
                    "dropoff_node_id": task.dropoff_node_id,
                    "edge_slot_sequence": first_code * baseline_hops,
                    "step_check_sequence": "1" * baseline_hops,
                    "failure_reason_code": "none",
                }
            )
            baseline_move_json_bytes = canonical_bytes(
                {
                    "status": "completed",
                    "pickup_node_id": task.pickup_node_id,
                    "dropoff_node_id": task.dropoff_node_id,
                    "move_sequence": "E" * baseline_hops,
                    "step_check_sequence": "1" * baseline_hops,
                    "failure_reason_code": "none",
                }
            )

            messages_json = json.dumps(messages, ensure_ascii=False)
            if any(
                field in messages_json
                for field in ("true_pickup_node_id", "true_dropoff_node_id", "ground_truth")
            ):
                raise RuntimeError("evaluator_truth_leaked_into_route_prompt")
            rows.append(
                {
                    "seed": seed,
                    "task_id": task.task_id,
                    "source_type": source_type,
                    "package_nodes": len(package.nodes),
                    "package_directed_edges": len(package.edges),
                    "package_expansion_count": package.expansion_count,
                    "message_bytes": message_bytes,
                    "tool_schema_bytes": route_constraint.schema_bytes,
                    "request_body_bytes": request_body_bytes,
                    "direction_candidate_message_bytes": direction_message_bytes,
                    "direction_candidate_schema_bytes": canonical_bytes(direction_tool),
                    "direction_candidate_request_body_bytes": direction_request_body_bytes,
                    "schema_sha256": route_constraint.schema_sha256,
                    "schema_max_route_edges": route_constraint.max_route_edges,
                    "shortest_stage_hops_lower_bound": lower_bound_hops,
                    "full_map_dijkstra_edge_count_for_output_sizing_only": baseline_hops,
                    "edge_slot_output_json_bytes_at_lower_bound": edge_json_bytes,
                    "direction_output_json_bytes_at_lower_bound_size_only": move_json_bytes,
                    "edge_slot_output_json_bytes_at_full_map_dijkstra_length": baseline_edge_json_bytes,
                    "direction_output_json_bytes_at_full_map_dijkstra_length_size_only_alternative": baseline_move_json_bytes,
                    "edge_slot_fixture_schema_normalized": normalized_fixture["status"] == "completed",
                    "output_cap_tokens": 1024,
                    "output_fit_status": "not_proven_tokenizer_unavailable_and_path_may_be_longer",
                }
            )
            seed_count += 1
        per_seed[str(seed)] = seed_count
        print("seed {0}: sized {1} reachable tasks".format(seed, seed_count), flush=True)

    if len(rows) != 130:
        raise RuntimeError("expected_130_reachable_tasks_got_{0}".format(len(rows)))

    smoke_path = ROOT / "outputs/route_layer/r4_smoke_v17_api/seed0_semantic_road_feature.json"
    smoke = read_json(smoke_path)
    smoke_messages = smoke["route_search"]["messages"]
    smoke_schema_bytes = smoke["deepseek"]["constraint_schema_bytes"]
    smoke_input_tokens = smoke["deepseek"]["usage"]["input_tokens"]
    smoke_message_bytes = sum(len(item["content"].encode("utf-8")) for item in smoke_messages)
    smoke_chars = len(smoke["deepseek"]["attempts"][0].get("raw_output", ""))
    smoke_output_tokens = smoke["deepseek"]["attempts"][0].get("output_tokens")

    # The 8x8 smoke is only a rough scale reference; no 100x100 tokenizer
    # measurement is available.  Keep the two observed input byte/token ratios
    # separate rather than treating either as a provider quote.
    input_ratio_smoke = smoke_input_tokens / float(smoke_message_bytes + smoke_schema_bytes)
    input_ratio_stepwise = 40270 / float(77463)  # prior 8x8 R4 stepwise smoke
    request_sizes = [row["request_body_bytes"] for row in rows]
    direction_request_sizes = [row["direction_candidate_request_body_bytes"] for row in rows]
    direction_schema_sizes = [row["direction_candidate_schema_bytes"] for row in rows]
    direction_message_sizes = [row["direction_candidate_message_bytes"] for row in rows]
    schema_sizes = [row["tool_schema_bytes"] for row in rows]
    message_sizes = [row["message_bytes"] for row in rows]
    hop_sizes = [row["shortest_stage_hops_lower_bound"] for row in rows]
    baseline_hop_sizes = [row["full_map_dijkstra_edge_count_for_output_sizing_only"] for row in rows]
    output_sizes = [row["edge_slot_output_json_bytes_at_lower_bound"] for row in rows]
    move_output_sizes = [row["direction_output_json_bytes_at_lower_bound_size_only"] for row in rows]
    baseline_output_sizes = [row["edge_slot_output_json_bytes_at_full_map_dijkstra_length"] for row in rows]
    baseline_move_output_sizes = [row["direction_output_json_bytes_at_full_map_dijkstra_length_size_only_alternative"] for row in rows]
    schema_max_edges = max(row["schema_max_route_edges"] for row in rows)
    schema_max_output_example_bytes = canonical_bytes(
        {
            "status": "completed",
            "pickup_node_id": "node_99_99",
            "dropoff_node_id": "node_99_98",
            "edge_slot_sequence": "000" * schema_max_edges,
            "step_check_sequence": "1" * schema_max_edges,
            "failure_reason_code": "none",
        }
    )

    pricing = read_json(ROOT / "configs/route_layer/price_smoke_v1.json")
    price_per_token = float(pricing["api_price_per_token"])
    projected_message_schema_bytes = sum(
        row["message_bytes"] + row["tool_schema_bytes"] for row in rows
    )
    direction_candidate_message_schema_bytes = sum(
        row["direction_candidate_message_bytes"] + row["direction_candidate_schema_bytes"]
        for row in rows
    )
    estimate_low_tokens = math.ceil(projected_message_schema_bytes * input_ratio_smoke)
    estimate_high_tokens = math.ceil(projected_message_schema_bytes * input_ratio_stepwise)
    direction_estimate_low_tokens = math.ceil(direction_candidate_message_schema_bytes * input_ratio_smoke)
    direction_estimate_high_tokens = math.ceil(direction_candidate_message_schema_bytes * input_ratio_stepwise)
    result = {
        "schema_version": "r4_deepseek_full_route_offline_preflight_v1",
        "status": "offline_only_no_model_requests",
        "model_requests_made": 0,
        "api_key_read": False,
        "formal_batch_started": False,
        "protocol_under_audit": "current DeepSeek full-route strict directed-edge slots with one per-edge check marker",
        "input_source": "traditional_experiment_v1 frozen 100x100 seeds 0-9",
        "routeable_task_count": len(rows),
        "tasks_per_seed": per_seed,
        "excluded_count": len(excluded),
        "excluded_tasks": excluded,
        "truth_field_name_scan": "no evaluator-only truth field names in any serialized route prompt",
        "semantic_endpoint_size_basis": "frozen evaluator node mapping used only as the conditional already-resolved endpoint for sizing; this is not an endpoint-parsing result",
        "request_retry_policy": 0,
        "planning_attempt_cap_used_for_budget": 3,
        "output_token_cap_used_for_size_review": 1024,
        "deepseek_smoke_reference": {
            "source": str(smoke_path.relative_to(ROOT)),
            "status": smoke["status"],
            "input_tokens": smoke_input_tokens,
            "message_bytes": smoke_message_bytes,
            "tool_schema_bytes": smoke_schema_bytes,
            "observed_input_tokens_per_message_plus_schema_byte": input_ratio_smoke,
            "output_tokens": smoke_output_tokens,
            "raw_tool_argument_characters": smoke_chars,
            "note": "one failed 8x8 call; scale proxy only, not a validated 100x100 tokenizer estimate",
        },
        "comparison_proxy": {
            "prior_stepwise_input_tokens_per_message_byte": input_ratio_stepwise,
            "note": "upper planning proxy from a different 8x8 request pattern; not a provider quote",
        },
        "size_summaries_per_single_method_group": {
            "message_bytes": summary(message_sizes),
            "tool_schema_bytes": summary(schema_sizes),
            "request_body_bytes_without_authorization_header": summary(request_sizes),
            "direction_candidate_message_bytes_unadopted": summary(direction_message_sizes),
            "direction_candidate_schema_bytes_unadopted": summary(direction_schema_sizes),
            "direction_candidate_request_body_bytes_unadopted": summary(direction_request_sizes),
            "direction_candidate_input_byte_reduction_percent": round(
                (projected_message_schema_bytes - direction_candidate_message_schema_bytes)
                / projected_message_schema_bytes * 100.0, 2
            ),
            "max_route_edges_allowed_by_current_schema": summary(
                [row["schema_max_route_edges"] for row in rows]
            ),
            "minimum_route_hops_lower_bound": summary(hop_sizes),
            "current_edge_slot_output_json_bytes_at_shortest_stage_lower_bound": summary(output_sizes),
            "one_char_direction_output_json_bytes_size_only_alternative": summary(move_output_sizes),
            "full_map_dijkstra_route_edge_count_for_output_sizing_only": summary(baseline_hop_sizes),
            "current_edge_slot_output_json_bytes_at_full_map_dijkstra_route_length": summary(baseline_output_sizes),
            "one_char_direction_output_json_bytes_at_full_map_dijkstra_route_length_size_only_alternative": summary(baseline_move_output_sizes),
            "edge_slot_output_json_bytes_at_max_schema_route_length_size_example": schema_max_output_example_bytes,
        },
        "input_cost_proxy_per_single_method_group": {
            "configured_price_cny_per_token": price_per_token,
            "message_plus_schema_bytes": projected_message_schema_bytes,
            "estimated_tokens_low_from_failed_full_route_smoke_ratio": estimate_low_tokens,
            "estimated_tokens_high_from_prior_stepwise_ratio": estimate_high_tokens,
            "estimated_input_cost_cny_low": round(estimate_low_tokens * price_per_token, 6),
            "estimated_input_cost_cny_high": round(estimate_high_tokens * price_per_token, 6),
            "three_attempt_upper_input_cost_proxy_cny_low": round(3 * estimate_low_tokens * price_per_token, 6),
            "three_attempt_upper_input_cost_proxy_cny_high": round(3 * estimate_high_tokens * price_per_token, 6),
            "three_method_three_attempt_upper_input_cost_proxy_cny_high": round(
                9 * estimate_high_tokens * price_per_token, 6
            ),
            "direction_candidate_message_plus_schema_bytes_unadopted": direction_candidate_message_schema_bytes,
            "direction_candidate_estimated_tokens_low_unadopted": direction_estimate_low_tokens,
            "direction_candidate_estimated_tokens_high_unadopted": direction_estimate_high_tokens,
            "direction_candidate_input_cost_cny_low_unadopted": round(direction_estimate_low_tokens * price_per_token, 6),
            "direction_candidate_input_cost_cny_high_unadopted": round(direction_estimate_high_tokens * price_per_token, 6),
            "excludes": [
                "output token charges",
                "Qwen local compute charges",
                "semantic endpoint failures before route call",
                "per-task retry feedback byte growth",
                "actual DeepSeek tokenization and current provider tariff",
            ],
            "interpretation": "byte-ratio budget proxy only; not invoice, quote, or hard ceiling",
        },
        "task_rows": rows,
        "limitations": [
            "For semantic-order sizing, frozen evaluator mapping stands in for a correct upstream Qwen selection; only selected endpoint IDs enter the route query, while truth fields are removed. This does not evaluate semantic parsing.",
            "Shortest stage-hop counts are lower bounds, not model route lengths; detours can be longer.",
            "Saved full-map Dijkstra edge counts are used only for output-size comparison and are never included in prompts; the model route length remains unknown.",
            "Current schema permits up to package_nodes-1 edge slots, which is much larger than the 1024-token output cap can safely guarantee.",
            "Direction-string schema and prompt were constructed only in memory for byte measurement; they were not applied to the route runner.",
            "One 8x8 failed API smoke is insufficient to predict 100x100 tokenization, route quality, or cost.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_out = {
        key: result[key]
        for key in (
            "routeable_task_count",
            "excluded_count",
            "size_summaries_per_single_method_group",
            "input_cost_proxy_per_single_method_group",
        )
    }
    print(json.dumps(summary_out, ensure_ascii=False, indent=2))
    print("wrote {0}".format(output_path))


def main():
    seed_root = ROOT / "outputs/route_layer/traditional_experiment_v1/seeds"
    output_path = ROOT / "outputs/route_layer/r4_preflight/r4_deepseek_full_route_preflight_20261022.json"
    audit(seed_root, output_path)


if __name__ == "__main__":
    main()
