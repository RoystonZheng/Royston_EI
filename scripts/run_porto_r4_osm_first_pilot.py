#!/usr/bin/env python3
"""Run an isolated, one-order Porto OSM R4 route pilot after a zero-call gate."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import importlib.util
import json
import math
from collections import deque
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "data/real_map_dispatch/porto_hybrid_v1"
R2_ROOT = ROOT / "data/real_map_dispatch/.worktrees/porto_r2_execute_20261025"
R2_RUNNER_PATH = R2_ROOT / "scripts/run_porto_r2_signednet_ev2_v4.py"
ROUTE_PROBLEM_PATH = R2_ROOT / "scripts/porto_r2_route_problem_signednet_ev2_v3.py"
CODEX_ADAPTER_PATH = R2_ROOT / "scripts/porto_r2_codex_adapter_v1.py"
PROTOCOL_PATH = R2_ROOT / "derived/experiments/protocols/r2_dynamic_sumo_protocol_v1_14_signednet_ev2_matched.json"
SHARED_PREFLIGHT = R2_ROOT / "derived/experiments/r2_dynamic_sumo_v1/porto_r2_ev2_signed_net_preflight_20261027T1801CST"
SHARED_GRAPH_PATH = SHARED_PREFLIGHT / "preflight/shared_candidate_link_graph.json"
SHARED_PREFLIGHT_SUMMARY = SHARED_PREFLIGHT / "preflight/summary.json"
SHARED_PREFLIGHT_MANIFEST = SHARED_PREFLIGHT / "batch_manifest.json"
ENERGY_MODULE_PATH = ROOT / "route_experiment/energy_model.py"
DECISION_MODULE_PATH = ROOT / "route_experiment/decision.py"
COSTS_MODULE_PATH = ROOT / "route_experiment/costs.py"
ORDER_ID = "ord_0001"
SEED = 0
OBJECTIVE = "FASTEST"
PROVIDER_CONTEXT_LIMIT = 1_050_000
OUTPUT_RESERVE_TOKENS = 8_192
PROVIDER_INPUT_GATE = PROVIDER_CONTEXT_LIMIT - OUTPUT_RESERVE_TOKENS
SOFT_INPUT_TARGET = 75_000
PILOT_CALL_SCOPE = 1
FARE_PER_PASSENGER_KM_CNY = 3.5
ELECTRICITY_PRICE_EUR_PER_KWH = 0.30
ECB_EUR_CNY = 7.4217
ECB_USD_PER_EUR = 1.1133
ELECTRICITY_PRICE_CNY_PER_KWH = ELECTRICITY_PRICE_EUR_PER_KWH * ECB_EUR_CNY
USD_TO_CNY = ECB_EUR_CNY / ECB_USD_PER_EUR
V5_PROFILES = (
    "money_priority_0_4_0_6",  # user shorthand 4:6 = time 0.4, money 0.6
    "time_priority_0_6_0_4",   # user shorthand 6:4 = time 0.6, money 0.4
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module_load_failed:{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_runtime():
    runner = load_module("porto_r4_pilot_r2_runner", R2_RUNNER_PATH)
    runtime = runner.load_runtime(SOURCE_ROOT)
    original_fallback_path = runner.candidate_fallback_path

    def candidate_fallback_path_with_self_pair(graph, start_id, end_id):
        # The shared all-pairs encoding omits self-pairs. For a vehicle already
        # at its pickup point, the exact same-point directed Link is still a
        # valid zero-intermediate-link route and must remain in the candidate
        # pool as the documented fallback.
        if graph and str(start_id) == str(end_id):
            for point in graph.get("p", []):
                if str(point[0]) == str(start_id):
                    return [str(point[1])]
            return None
        return original_fallback_path(graph, start_id, end_id)

    runner.candidate_fallback_path = candidate_fallback_path_with_self_pair
    return runner, runtime


def decode_base36(value: str) -> int:
    return int(value, 36)


def encode_base36(number: int) -> str:
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if number == 0:
        return "0"
    result = ""
    while number:
        number, remainder = divmod(number, 36)
        result = digits[remainder] + result
    return result


def endpoint_projection(shared_graph: dict[str, Any], point_ids: list[str]) -> dict[str, Any]:
    """Losslessly project the frozen all-pairs graph to this request's endpoints."""
    points = shared_graph["p"]
    old_index = {str(row[0]): index for index, row in enumerate(points)}
    if any(point_id not in old_index for point_id in point_ids):
        missing = [point_id for point_id in point_ids if point_id not in old_index]
        raise RuntimeError("shared_candidate_pair_missing:" + ",".join(missing))
    chosen_ids = set(point_ids)
    selected_points = [row for row in points if str(row[0]) in chosen_ids]
    selected_old_indexes = [old_index[str(row[0])] for row in selected_points]
    old_links = shared_graph["l"]
    selected_pairs: dict[tuple[int, int], Any] = {}
    needed_link_indexes: set[int] = set()
    for source in selected_old_indexes:
        targets = [index for index in range(len(points)) if index != source]
        row = shared_graph["r"][source]
        for target in selected_old_indexes:
            if target == source:
                continue
            entry = row[targets.index(target)]
            selected_pairs[(source, target)] = entry
            if entry is not None:
                needed_link_indexes.update(decode_base36(token) for token in entry[0].split(".") if token)
    ordered_link_indexes = sorted(needed_link_indexes)
    new_link_index = {old: index for index, old in enumerate(ordered_link_indexes)}
    projected_rows = []
    unreachable = []
    point_new_index = {old: new for new, old in enumerate(selected_old_indexes)}
    for source in selected_old_indexes:
        row = []
        for target in selected_old_indexes:
            if target == source:
                continue
            entry = selected_pairs[(source, target)]
            if entry is None:
                row.append(None)
                unreachable.append([point_new_index[source], point_new_index[target], "unreachable_in_frozen_shared_graph"])
                continue
            path = [decode_base36(token) for token in entry[0].split(".") if token]
            if any(index not in new_link_index for index in path):
                raise RuntimeError("candidate_projection_link_remap_failed")
            encoded = ".".join(encode_base36(new_link_index[index]) for index in path)
            row.append([encoded, entry[1], entry[2]])
        projected_rows.append(row)
    return {
        "v": shared_graph.get("v", 1),
        "sha256": shared_graph.get("sha256"),
        "metric": shared_graph.get("metric"),
        "role": "task_local_projection_of_the_frozen_shared_dijkstra_reference_only",
        "scope": "all_ordered_pairs_among_this_request_vehicle_pickup_dropoff_endpoints",
        "p": selected_points,
        "l": [old_links[index] for index in ordered_link_indexes],
        "r": projected_rows,
        "u": unreachable,
    }


def augment_shared_graph_for_frozen_endpoints(runner, runtime, context, raw_graph, endpoint_ids):
    """Extend the immutable hotspot reference with exact frozen order endpoints."""
    graph = copy.deepcopy(raw_graph)
    points = graph["candidate_points"]
    point_index = {str(row["point_id"]): index for index, row in enumerate(points)}
    missing = [str(point_id) for point_id in endpoint_ids if str(point_id) not in point_index]
    if not missing:
        return graph, {"added_point_ids": [], "new_ordered_pair_count": 0}

    r1 = runtime[0]
    base_graph_sha = json_hash(raw_graph)
    source_matches = {
        str(row["point_id"]): {
            "edge_id": str(row["edge_id"]),
            "edge_pos_m": float(row["edge_pos_m"]),
            "snap_distance_m": float(row["snap_distance_m"]),
            "candidate_rank": int(row.get("candidate_rank", 1)),
        }
        for row in points
    }
    for point_id in missing:
        match = context.match(point_id)
        points.append({
            "point_id": point_id,
            "edge_id": str(match["edge_id"]),
            "edge_pos_m": round(float(match["edge_pos_m"]), 3),
            "snap_distance_m": round(float(match["snap_distance_m"]), 3),
            "candidate_rank": int(match.get("candidate_rank", 1)),
        })
    point_index = {str(row["point_id"]): index for index, row in enumerate(points)}
    existing_pairs = {(int(row["from"]), int(row["to"])) for row in graph["ordered_pairs"]}
    link_index = {str(edge_id): index for index, edge_id in enumerate(graph["links"])}
    new_pair_count = 0
    for source_id in missing:
        source_index = point_index[source_id]
        for target_index, target in enumerate(points):
            if source_index == target_index:
                continue
            target_id = str(target["point_id"])
            for from_index, to_index, from_id, to_id in (
                (source_index, target_index, source_id, target_id),
                (target_index, source_index, target_id, source_id),
            ):
                key = (from_index, to_index)
                if key in existing_pairs:
                    continue
                from_match = source_matches.get(from_id) or context.match(from_id)
                to_match = source_matches.get(to_id) or context.match(to_id)
                route = runner.static_free_flow_dijkstra(r1, context, from_match, to_match)
                if route.get("status") != "ok":
                    graph["ordered_pairs"].append({
                        "from": from_index, "to": to_index, "status": "unreachable",
                        "reason": route.get("status", "unknown"),
                    })
                else:
                    path_indices = []
                    for edge_id in route["path"]:
                        edge_id = str(edge_id)
                        if edge_id not in link_index:
                            link_index[edge_id] = len(graph["links"])
                            graph["links"].append(edge_id)
                        path_indices.append(link_index[edge_id])
                    graph["ordered_pairs"].append({
                        "from": from_index, "to": to_index, "status": "ok",
                        "links": path_indices,
                        "distance_m": round(float(route["distance_m"]), 3),
                        "free_flow_time_s": round(float(route["predicted_travel_time_s"]), 3),
                    })
                existing_pairs.add(key)
                new_pair_count += 1
    graph["ordered_pairs"].sort(key=lambda row: (int(row["from"]), int(row["to"])))
    graph["pair_count"] = len(graph["ordered_pairs"])
    graph["pair_count_expected"] = len(points) * (len(points) - 1)
    graph["reachable_pair_count"] = sum(row["status"] == "ok" for row in graph["ordered_pairs"])
    graph["unreachable_pair_count"] = graph["pair_count"] - graph["reachable_pair_count"]
    graph["task_endpoint_augmentation"] = {
        "base_shared_candidate_graph_content_sha256": base_graph_sha,
        "added_frozen_endpoint_ids": missing,
        "method": "exact_frozen_endpoint_bindings_plus_same_frozen_network_directed_Dijkstra",
    }
    return graph, {"added_point_ids": missing, "new_ordered_pair_count": new_pair_count}


def compact_request(runner, runtime, legs, task, prompt, shared_graph, endpoint_ids):
    projected = endpoint_projection(shared_graph, endpoint_ids)
    task = copy.deepcopy(task)
    task["h"] = projected
    instructions, _old_task = prompt.rsplit("\n", 1)
    start = instructions.find("h is the same frozen all-candidate-pair free-flow Dijkstra Link graph")
    end_marker = "h.u lists unreachable pairs."
    end = instructions.find(end_marker, start)
    if start < 0 or end < 0:
        raise RuntimeError("shared_graph_prompt_contract_not_found")
    end += len(end_marker)
    replacement = (
        "h is a task-local endpoint projection of the same immutable shared candidate Link graph. "
        "The original 30-point/870-pair graph, its SHA-256, and all other pair routes are retained in the run artifacts; "
        "this request carries only the ordered pairs among its vehicle, pickup, and dropoff points. "
        "h.p is the ordered task endpoint table [point_id,edge_id,edge_position_m,snap_distance_m]; h.l is the projected common Link dictionary; "
        "h.r row i corresponds to source h.p[i], and entries correspond to other projected endpoints in order. "
        "Each reachable entry is [path,distance_m,free_flow_time_s], where path is dot-separated base36 indices into h.l; h.u lists unreachable projected pairs."
    )
    instructions = instructions[:start] + replacement + instructions[end:]
    endpoint_contract = []
    for role, key in (("vehicle_to_pickup", "vp"), ("pickup_to_dropoff", "pd")):
        leg = legs[role]
        endpoint_contract.append(
            f"{role}: fixed start Link alias {leg.edge_alias[leg.start['edge_id']]}; "
            f"fixed end Link alias {leg.edge_alias[leg.end['edge_id']]}."
        )
    instructions += (
        "\nEndpoint alias rule: the evaluator inserts both endpoint Links. "
        "intermediate_edge_ids must contain only the Links strictly between them; "
        "never include either endpoint alias. The route must follow listed directed successors "
        "continuously and must not repeat a Link.\n" + "\n".join(endpoint_contract)
    )
    instructions += (
        "\nOne-way boundary: every Link alias is a directed road Link, and each row's successor list is the only legal next-Link set. "
        "A reverse Link or reverse turn is never implied by the current Link; use it only if that exact directed successor is listed. "
        "Treat each consecutive Link pair as a separate route segment: check the successor relation before extending the route. "
        "If a segment is illegal, discard that segment and the unverified suffix, return to the last legal Link, and search a legal continuation. "
        "A proposed path is not executable until every consecutive pair has passed the directed-turn check."
    )
    prompt = instructions + "\n" + json.dumps(task, ensure_ascii=False, separators=(",", ":"))
    return task, prompt


def build_candidate_fallback_response(runner, shared_graph, legs, route_endpoints):
    """Create endpoint-anchored fallback routes, including a coincident zero-length leg.

    The shared all-pairs graph intentionally has no self-pair entries. When a
    vehicle already occupies the pickup point, the vehicle-to-pickup leg can
    still be valid: both endpoints bind to the same directed Link and no
    intermediate Links are needed. Keep the normal frozen Link-Dijkstra path
    for all non-coincident endpoint pairs.
    """
    rows = []
    for role, leg in legs.items():
        start_id, end_id = route_endpoints[role]
        if start_id == end_id and str(leg.start["edge_id"]) == str(leg.end["edge_id"]):
            path = [str(leg.start["edge_id"])]
        else:
            path = runner.candidate_fallback_path(shared_graph, start_id, end_id)
        if (
            not path
            or path[0] != str(leg.start["edge_id"])
            or path[-1] != str(leg.end["edge_id"])
            or any(edge_id not in leg.edge_alias for edge_id in path)
        ):
            return None
        rows.append({
            "leg_role": role,
            "intermediate_edge_ids": [leg.edge_alias[edge_id] for edge_id in path[1:-1]],
        })
    return {"routes": rows}


def minimum_intermediate_link_count(leg: Any) -> int:
    """Return the minimum number of intermediate Links on any directed path."""
    start = leg.start["edge_id"]
    end = leg.end["edge_id"]
    if start == end:
        return 0
    queue = deque([(start, 0)])
    seen = {start}
    while queue:
        node, transitions = queue.popleft()
        for target in leg.crop.graph.successors(node):
            if target == end:
                return max(0, transitions)
            if target not in seen:
                seen.add(target)
                queue.append((target, transitions + 1))
    raise RuntimeError("endpoint_route_has_no_directed_path")


def constrain_route_schema_for_endpoints(schema: dict[str, Any], task: dict[str, Any], legs: dict[str, Any]) -> dict[str, Any]:
    """Require a complete directed path and describe its fixed endpoint aliases."""
    role_to_task_key = {
        "vehicle_to_pickup": "vp",
        "pickup_to_dropoff": "pd",
    }
    branches = schema["properties"]["routes"]["items"].get("anyOf", [])
    seen_roles = set()
    for branch in branches:
        role_values = branch.get("properties", {}).get("leg_role", {}).get("enum", [])
        if len(role_values) != 1 or role_values[0] not in role_to_task_key:
            raise RuntimeError("route_schema_leg_role_contract_mismatch")
        role = role_values[0]
        task_leg = task["g"][role_to_task_key[role]]
        leg = legs[role]
        prefix = "v" if role == "vehicle_to_pickup" else "p"
        task_endpoint_aliases = {
            prefix + str(index).upper().rjust(4, "0")
            for index in (task_leg["a"][0], task_leg["b"][0])
        }
        middle = branch["properties"]["intermediate_edge_ids"]
        minimum = minimum_intermediate_link_count(leg)
        if minimum > middle.get("maxItems", 510):
            raise RuntimeError("minimum_route_exceeds_schema_cap")
        middle["minItems"] = minimum
        # Endpoint-anchored requests have the fixed start/end Link inserted by
        # the evaluator. Exclude those aliases from the model's intermediate
        # list at schema level. Keep the compact alias regex: the provider
        # rejects regex lookarounds, and a full per-edge enum bloats output.
        endpoint_aliases = {
            leg.edge_alias[leg.start["edge_id"]],
            leg.edge_alias[leg.end["edge_id"]],
        }
        if task_endpoint_aliases != endpoint_aliases:
            raise RuntimeError("route_schema_endpoint_alias_mapping_mismatch")
        # Bound the answer to a plausible path length. This keeps the model
        # from returning the entire local alias namespace as a route.
        middle["maxItems"] = min(middle.get("maxItems", 510), minimum + 32)
        middle["items"]["description"] = (
            "List only Links strictly between the fixed start and end Links. "
            "Never include endpoint aliases " + ", ".join(sorted(endpoint_aliases)) + ". "
            "The evaluator inserts both endpoints automatically. Each item must be a known directed Link alias; its direction is not reversible unless a separate alias exists."
        )
        seen_roles.add(role)
    if seen_roles != set(role_to_task_key):
        raise RuntimeError("route_schema_missing_leg_role")
    return schema


def _decode_endpoint_anchored_paths(response: dict[str, Any], legs: dict[str, Any]) -> dict[str, list[str]]:
    if not isinstance(response, dict) or set(response) != {"routes"}:
        raise ValueError("response_shape_invalid")
    rows = response.get("routes")
    if not isinstance(rows, list) or len(rows) != len(legs):
        raise ValueError("route_count_invalid")
    decoded = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"leg_role", "intermediate_edge_ids"}:
            raise ValueError("route_row_shape_invalid")
        role = row.get("leg_role")
        aliases = row.get("intermediate_edge_ids")
        if role not in legs or role in decoded or not isinstance(aliases, list):
            raise ValueError("route_role_or_length_invalid")
        leg = legs[role]
        endpoint_aliases = {leg.edge_alias[leg.start["edge_id"]], leg.edge_alias[leg.end["edge_id"]]}
        if any(not isinstance(alias, str) or alias not in leg.alias_to_edge for alias in aliases):
            raise ValueError("unknown_link_alias")
        if any(alias in endpoint_aliases for alias in aliases):
            raise ValueError("endpoint_alias_in_intermediate_route")
        if len(set(aliases)) != len(aliases):
            raise ValueError("repeated_link_in_route")
        start_edge = str(leg.start["edge_id"])
        end_edge = str(leg.end["edge_id"])
        if start_edge == end_edge:
            if aliases:
                raise ValueError("same_link_route_must_have_no_intermediate_links")
            decoded[role] = [start_edge]
        else:
            decoded[role] = [
                start_edge,
                *[str(leg.alias_to_edge[alias]) for alias in aliases],
                end_edge,
            ]
    if set(decoded) != set(legs):
        raise ValueError("required_leg_role_missing")
    return decoded


def make_synthetic_reverse_turn_fixture(runner, fallback_response: dict[str, Any], legs: dict[str, Any]):
    """Insert one known reverse-only transition to exercise rollback and repair."""
    response = copy.deepcopy(fallback_response)
    for row in response.get("routes", []):
        role = row.get("leg_role")
        leg = legs[role]
        path = [
            str(leg.start["edge_id"]),
            *[str(leg.alias_to_edge[alias]) for alias in row["intermediate_edge_ids"]],
            str(leg.end["edge_id"]),
        ]
        route_set = set(path)
        for source_index, source in enumerate(path[:-1]):
            reverse_only = sorted(
                str(candidate) for candidate in leg.crop.graph.predecessors(source)
                if str(candidate) not in route_set
                and not leg.crop.graph.has_edge(source, candidate)
            )
            if not reverse_only:
                continue
            wrong_way_edge = reverse_only[0]
            alias = leg.edge_alias.get(wrong_way_edge)
            if alias is None:
                continue
            row["intermediate_edge_ids"].insert(source_index, alias)
            return response, {
                "fixture_type": "synthetic_reverse_only_directed_transition",
                "map_type": "real OSM-derived Porto SUMO network",
                "leg_role": role,
                "accepted_prefix_link_ids": path[:source_index + 1],
                "last_verified_link_id": source,
                "proposed_wrong_way_link_id": wrong_way_edge,
                "reverse_transition_exists": bool(leg.crop.graph.has_edge(wrong_way_edge, source)),
                "forward_transition_exists": bool(leg.crop.graph.has_edge(source, wrong_way_edge)),
                "proposal_status": "synthetic_fixture_only_no_model_response_or_usage",
            }
    raise RuntimeError("no_reverse_only_transition_available_for_fixture")


def _first_directed_segment_failure(r1, leg, path: list[str]) -> dict[str, Any] | None:
    if not path or path[0] != leg.start["edge_id"] or path[-1] != leg.end["edge_id"]:
        return {"segment_index": 0, "reason": "endpoint_link_mismatch"}
    accepted = [path[0]]
    if accepted[0] not in leg.crop.edge_ids:
        return {"segment_index": 0, "reason": "start_link_outside_local_crop"}
    for index, target in enumerate(path[1:], start=1):
        source = accepted[-1]
        if target not in leg.crop.edge_ids:
            return {"segment_index": index, "from_link": source, "to_link": target, "reason": "link_outside_local_crop"}
        if target in accepted:
            return {"segment_index": index, "from_link": source, "to_link": target, "reason": "repeated_link_in_route"}
        if not leg.crop.graph.has_edge(source, target):
            return {"segment_index": index, "from_link": source, "to_link": target, "reason": "illegal_directed_turn"}
        if not r1.pf.is_legal_sequence(accepted + [target]):
            return {"segment_index": index, "from_link": source, "to_link": target, "reason": "forbidden_directed_way_sequence"}
        accepted.append(target)
    return None


def _path_passes_directed_guard(r1, leg, path: list[str], score_path, objective, now, traffic, tls_snapshot) -> tuple[bool, str | None]:
    failure = _first_directed_segment_failure(r1, leg, path)
    if failure:
        return False, str(failure["reason"])
    if len(set(path)) != len(path):
        return False, "repeated_link_in_route"
    try:
        score_path(r1, leg, path, objective, now, traffic, tls_snapshot)
    except (ValueError, TypeError, KeyError) as error:
        return False, f"{type(error).__name__}:{error}"
    return True, None


def validate_routes_incrementally(
    runner, r1, original_validator, score_path_fn, response, legs, objective, now, traffic, tls_snapshot,
    shared_graph, route_endpoints, context,
):
    """Check every directed segment; reroute the tail from the last legal Link on failure."""
    validation_error = None
    try:
        original_audit, original_routes = original_validator(
            r1, response, legs, objective, now, traffic, tls_snapshot,
            endpoint_anchored=True,
        )
        decoded = _decode_endpoint_anchored_paths(response, legs)
        segment_audit = {}
        for role, path in decoded.items():
            failure = _first_directed_segment_failure(r1, legs[role], path)
            if failure:
                raise ValueError("validator_passed_path_but_segment_guard_failed")
            segment_audit[role] = {
                "proposal_status": "passed_every_directed_segment",
                "proposed_link_count": len(path),
                "proposed_route_sha256": json_hash(path),
                "verified_segment_count": max(0, len(path) - 1),
                "illegal_segment_count": 0,
                "repaired_path": None,
            }
        return original_audit, original_routes, segment_audit
    except (ValueError, TypeError, KeyError) as error:
        validation_error = error

    if validation_error is None:
        return original_audit, original_routes, segment_audit

    try:
        proposed_paths = _decode_endpoint_anchored_paths(response, legs)
    except (ValueError, TypeError, KeyError):
        raise validation_error

    repaired_response = copy.deepcopy(response)
    repaired_rows = {row["leg_role"]: row for row in repaired_response["routes"]}
    segment_audit = {}
    repaired_paths = {}
    for role, proposed_path in proposed_paths.items():
        leg = legs[role]
        failure = _first_directed_segment_failure(r1, leg, proposed_path)
        if failure is None:
            repaired_paths[role] = proposed_path
            segment_audit[role] = {
                "proposal_status": "passed_every_directed_segment",
                "proposed_link_count": len(proposed_path),
                "proposed_route_sha256": json_hash(proposed_path),
                "verified_segment_count": max(0, len(proposed_path) - 1),
                "illegal_segment_count": 0,
                "repaired_path": None,
            }
            continue

        accepted_prefix = proposed_path[:int(failure["segment_index"])]
        if not accepted_prefix:
            accepted_prefix = [str(leg.start["edge_id"])]
        rollback_link = accepted_prefix[-1]
        if rollback_link == leg.start["edge_id"] and len(accepted_prefix) == 1:
            from_match = leg.start
        else:
            from_match = {
                "edge_id": rollback_link,
                "edge_pos_m": float(leg.crop.allowed[rollback_link].getLength()),
            }
        excluded_prefix_links = set(accepted_prefix[:-1])
        repair_edge_ids = set(leg.crop.edge_ids) - excluded_prefix_links
        repair_graph = leg.crop.graph.subgraph(repair_edge_ids).copy()
        repair_allowed = {edge_id: leg.crop.allowed[edge_id] for edge_id in repair_graph.nodes}
        tail = runner.static_free_flow_dijkstra(
            r1, context, from_match, leg.end, graph=repair_graph, allowed=repair_allowed,
        )
        repair_source = "directed_dijkstra_from_last_legal_link"
        repaired_path = None
        if tail.get("status") == "ok" and tail.get("path"):
            candidate = accepted_prefix + [str(edge_id) for edge_id in tail["path"][1:]]
            valid, _reason = _path_passes_directed_guard(
                r1, leg, candidate, score_path_fn, objective, now, traffic, tls_snapshot,
            )
            if valid:
                repaired_path = candidate
        if repaired_path is None:
            fallback_path = runner.candidate_fallback_path(
                shared_graph, *route_endpoints[role],
            )
            if fallback_path:
                candidate = [str(edge_id) for edge_id in fallback_path]
                valid, _reason = _path_passes_directed_guard(
                    r1, leg, candidate, score_path_fn, objective, now, traffic, tls_snapshot,
                )
                if valid:
                    repaired_path = candidate
                    repair_source = "full_leg_shared_directed_dijkstra_fallback"
        if repaired_path is None:
            raise validation_error

        row = repaired_rows[role]
        row["intermediate_edge_ids"] = [
            leg.edge_alias[edge_id] for edge_id in repaired_path[1:-1]
        ]
        repaired_paths[role] = repaired_path
        segment_audit[role] = {
            "proposal_status": "rejected_at_directed_segment_and_researched",
            "proposed_link_count": len(proposed_path),
            "proposed_route_sha256": json_hash(proposed_path),
            "verified_model_prefix_segment_count": max(0, len(accepted_prefix) - 1),
            "rejected_segment": failure,
            "discarded_unverified_suffix_link_count": max(0, len(proposed_path) - len(accepted_prefix) - 1),
            "rollback_link": rollback_link,
            "repair_source": repair_source,
            "repaired_link_count": len(repaired_path),
            "repaired_route_sha256": json_hash(repaired_path),
            "repair_segment_count": max(0, len(repaired_path) - len(accepted_prefix)),
            "repair_segments_validated_before_execution": True,
            "illegal_model_segment_executed": False,
        }

    try:
        audit, routes = original_validator(
            r1, repaired_response, legs, objective, now, traffic, tls_snapshot,
            endpoint_anchored=True,
        )
    except (ValueError, TypeError, KeyError):
        raise validation_error
    for role, route in routes.items():
        valid, reason = _path_passes_directed_guard(
            r1, legs[role], [str(edge_id) for edge_id in route["path"]],
            score_path_fn, objective, now, traffic, tls_snapshot,
        )
        if not valid:
            raise ValueError("repaired_route_failed_final_directed_guard:" + str(reason))
        segment_audit[role]["final_route_segment_count"] = max(0, len(route["path"]) - 1)
        segment_audit[role]["final_route_sha256"] = json_hash(route["path"])
    return audit, routes, segment_audit


def provenance(runner, runtime, source_preflight: dict[str, Any], source_manifest: dict[str, Any]) -> dict[str, Any]:
    r1 = runtime[0]
    price_path = Path(runner.PRICE_PATH)
    return {
        "r1_runner_sha256": sha256_file(Path(r1.__file__)),
        "r1_runner_expected_sha256": runner.EXPECTED_R1_SHA256,
        "route_problem_sha256": sha256_file(ROUTE_PROBLEM_PATH),
        "r2_runner_sha256": sha256_file(R2_RUNNER_PATH),
        "codex_adapter_sha256": sha256_file(CODEX_ADAPTER_PATH),
        "protocol_sha256": sha256_file(PROTOCOL_PATH),
        "source_preflight_manifest_sha256": sha256_file(SHARED_PREFLIGHT_MANIFEST),
        "source_preflight_report_sha256": sha256_file(SHARED_PREFLIGHT_SUMMARY),
        "shared_candidate_link_graph_sha256": source_preflight["shared_candidate_link_graph"]["sha256"],
        "frozen_input_manifest_sha256": source_preflight["frozen_input_manifest_sha256"],
        "network_sha256": source_preflight["network_sha256"],
        "core_edge_sha256": source_preflight["core_edge_sha256"],
        "core_connection_sha256": source_preflight["core_connection_sha256"],
        "source_preflight_run_id": source_manifest["run_id"],
        "energy_model_module_sha256": sha256_file(ENERGY_MODULE_PATH),
        "energy_profile_id": r1.ENERGY_PROFILE.profile_id,
        "energy_profile_sha256": r1.ENERGY_PROFILE.fingerprint(),
        "decision_module_sha256": sha256_file(DECISION_MODULE_PATH),
        "costs_module_sha256": sha256_file(COSTS_MODULE_PATH),
        "api_rate_card_sha256": sha256_file(price_path),
        "pilot_runner_sha256": sha256_file(Path(__file__)),
    }


def prepare_base(run_dir: Path, runner, runtime):
    if run_dir.exists():
        if any(run_dir.iterdir()):
            raise RuntimeError(f"refusing_to_overwrite:{run_dir}")
    else:
        run_dir.mkdir(parents=True)
    source_preflight = json.loads(SHARED_PREFLIGHT_SUMMARY.read_text(encoding="utf-8"))
    source_manifest = json.loads(SHARED_PREFLIGHT_MANIFEST.read_text(encoding="utf-8"))
    if source_preflight.get("run_status") != "completed_zero_model_calls" or source_preflight.get("actual_model_call_count") != 0:
        raise RuntimeError("shared_source_zero_call_preflight_not_complete")
    if source_preflight.get("route_leg_failed") != 0 or source_preflight.get("requests_failed") != 0:
        raise RuntimeError("shared_source_preflight_has_failed_inputs")
    frozen = runtime[5](runtime[0], list(range(10)))
    if frozen["network_sha256"] != source_preflight["network_sha256"]:
        raise RuntimeError("current_frozen_network_hash_differs_from_latest_protocol")
    if frozen["core_edge_sha256"] != source_preflight["core_edge_sha256"]:
        raise RuntimeError("current_frozen_core_edge_hash_differs_from_latest_protocol")
    if frozen["core_connection_sha256"] != source_preflight["core_connection_sha256"]:
        raise RuntimeError("current_frozen_core_connection_hash_differs_from_latest_protocol")
    if sha256_file(SHARED_GRAPH_PATH) != source_preflight["shared_candidate_link_graph"]["sha256"]:
        raise RuntimeError("shared_candidate_graph_source_hash_mismatch")
    if sha256_file(Path(runtime[0].__file__)) != runner.EXPECTED_R1_SHA256:
        raise RuntimeError("r1_runner_hash_mismatch")
    return source_preflight, source_manifest, frozen


def build_static_preflight(run_dir: Path) -> dict[str, Any]:
    runner, runtime = load_runtime()
    source_preflight, source_manifest, frozen = prepare_base(run_dir, runner, runtime)
    r1, NetworkContext, RouteProblemBuilder, _score_path, validate, _verify, estimate, codex_preflight, _call_once, build_schema, serialize_schema, _output_reserve = runtime
    raw_graph = json.loads(SHARED_GRAPH_PATH.read_text(encoding="utf-8"))
    context = NetworkContext.load(r1, SEED)
    orders, _route_lookup = runner.orders_and_route_pairs(context)
    order = next(row for row in orders if row["order_id"] == ORDER_ID)
    vehicle_id = order["vehicle_id"]
    initial = {row["vehicle_id"]: row["initial_position"]["point_id"] for row in context.orders_file["vehicles"]}
    from_point_id = initial[vehicle_id]
    endpoint_ids = [from_point_id, order["pickup"]["point_id"], order["dropoff"]["point_id"]]
    task_raw_graph, graph_augmentation = augment_shared_graph_for_frozen_endpoints(
        runner, runtime, context, raw_graph, endpoint_ids,
    )
    shared_graph = runner.compact_candidate_graph_for_prompt(task_raw_graph)
    endpoints = {
        "vehicle_to_pickup": (from_point_id, order["pickup"]["point_id"]),
        "pickup_to_dropoff": (order["pickup"]["point_id"], order["dropoff"]["point_id"]),
    }
    traffic = {"sim_time_s": 0.0, "occupied_edge_count": 0, "active_vehicle_count": 0, "halting_vehicle_count": 0, "edge_state": {}}
    tls_snapshot: dict[str, Any] = {}
    request_key = f"s{SEED}:{OBJECTIVE}:{ORDER_ID}"
    builder = RouteProblemBuilder(context)
    legs, task, prompt = builder.build_order_request(
        order, from_point_id, OBJECTIVE, 0.0, traffic, tls_snapshot, request_key,
        shared_candidate_link_graph=shared_graph,
    )
    task, prompt = compact_request(runner, runtime, legs, task, prompt, shared_graph, endpoint_ids)
    base_schema_path = runner.PACKAGE / runner.SCHEMA_RELATIVE
    base_schema = json.loads(base_schema_path.read_text(encoding="utf-8"))
    schema = constrain_route_schema_for_endpoints(build_schema(base_schema, legs), task, legs)
    token_estimate = int(estimate(prompt, schema))
    fallback_response = build_candidate_fallback_response(runner, shared_graph, legs, endpoints)
    if fallback_response is None:
        raise RuntimeError("shared_candidate_fallback_not_covered_by_task_graph")
    fallback_audit, fallback_routes = validate(r1, fallback_response, legs, OBJECTIVE, 0.0, traffic, tls_snapshot, endpoint_anchored=True)
    cli_info = codex_preflight()
    prices_snapshot = json.loads(runner.PRICE_PATH.read_text(encoding="utf-8"))
    source_provenance = provenance(runner, runtime, source_preflight, source_manifest)
    api_card = prices_snapshot["api_standard_usd_per_million_tokens"]
    preflight_capture = {
        "_context": context,
        "_score_path": runtime[3],
        "_shared_graph": shared_graph,
        "legs": legs,
        "now": 0.0,
        "traffic": traffic,
        "tls_snapshot": tls_snapshot,
        "route_endpoints": endpoints,
        "pre_sumo_verified_routes": {},
        "source_provenance": source_provenance,
        "api_rate_card_sha256": source_provenance["api_rate_card_sha256"],
    }
    v5_zero_call_candidates = build_v5_candidate_evaluation(
        runner, runtime, preflight_capture, {}, False,
        {
            "input_usd_per_million": float(api_card["input"]),
            "output_usd_per_million": float(api_card["output"]),
        },
    )
    feasible_pair_hashes = {
        json_hash(row.get("route_paths", {}))
        for row in v5_zero_call_candidates.get("candidates", [])
        if row.get("feasible") is True
    }
    v5_zero_call_candidates["distinct_feasible_route_pair_count"] = len(feasible_pair_hashes)
    v5_zero_call_candidates["shared_link_candidate_present"] = any(
        row.get("method") == "shared_candidate_link_dijkstra" and row.get("feasible") is True
        for row in v5_zero_call_candidates.get("candidates", [])
    )
    v5_zero_call_candidates["preflight_status"] = (
        "passed" if v5_zero_call_candidates.get("status") == "selected_both_profiles"
        and v5_zero_call_candidates["distinct_feasible_route_pair_count"] >= 2
        and v5_zero_call_candidates["shared_link_candidate_present"]
        else "blocked_missing_multiple_feasible_routes_or_shared_link"
    )
    report = {
        "run_id": run_dir.name,
        "experiment_family": "R4_real_OSM_route_layer",
        "map_type": "real OSM-derived Porto SUMO network",
        "layer": "route planning; fixed vehicle/order assignment, no dispatch optimization",
        "pilot_scope": {"seed": SEED, "order_id": ORDER_ID, "vehicle_id": vehicle_id, "objective": OBJECTIVE, "model_requests": 1, "route_legs": ["vehicle_to_pickup", "pickup_to_dropoff"]},
        "run_status": "completed_zero_model_calls" if token_estimate <= PROVIDER_INPUT_GATE and cli_info.get("available") and v5_zero_call_candidates["preflight_status"] == "passed" else "blocked_preflight",
        "actual_model_call_count": 0,
        "source_provenance": source_provenance,
        "shared_graph_scope": {
            "full_graph_available_in_run_artifacts": True,
            "source_shared_candidate_count": source_preflight["shared_candidate_link_graph"]["candidate_count"],
            "source_shared_ordered_pair_count": source_preflight["shared_candidate_link_graph"]["ordered_pair_count"],
            "full_graph_sha256": source_preflight["shared_candidate_link_graph"]["sha256"],
            "task_reference_candidate_count": len(task_raw_graph["candidate_points"]),
            "task_reference_ordered_pair_count": task_raw_graph["pair_count"],
            "task_endpoint_augmentation": graph_augmentation,
            "prompt_projection_endpoints": endpoint_ids,
            "projection_route_pair_count": 6,
            "fallback_validation_status": "passed",
            "fallback_routes": {
                role: {
                    "status": fallback_audit["status"],
                    "link_count": len(fallback_routes[role]["path"]),
                    "path_sha256": json_hash(fallback_routes[role]["path"]),
                } for role in fallback_routes
            },
        },
        "request_gate": {
            "estimated_input_tokens_including_schema_and_cli_overhead": token_estimate,
            "provider_context_window_tokens": PROVIDER_CONTEXT_LIMIT,
            "provider_max_output_tokens": 128_000,
            "output_reserve_tokens": OUTPUT_RESERVE_TOKENS,
            "provider_context_gate_status": "passed" if token_estimate <= PROVIDER_INPUT_GATE else "blocked",
            "project_soft_input_target_tokens": SOFT_INPUT_TARGET,
            "soft_target_status": "within_target" if token_estimate <= SOFT_INPUT_TARGET else "above_efficiency_target_continue_if_provider_allows",
            "prompt_bytes": len(prompt.encode("utf-8")),
            "schema_bytes": len(serialize_schema(schema)),
            "estimator_is_not_actual_usage": True,
        },
        "route_schema_policy": "intermediate_edge_ids must contain at least the minimum directed-hop path length minus fixed endpoint Links; endpoint aliases are named in each field description and forbidden by the validator",
        "codex_cli_preflight": cli_info,
        "road_turn_information": {
            "all_links_in_each_leg_crop_retained": True,
            "all_induced_legal_directed_turns_retained": True,
            "current_snapshot_signal_and_traffic_fields_retained": True,
            "removed_from_prompt": "unrelated shared candidate-pair routes; complete source graph retained by SHA in artifacts",
        },
        "decision_function": {
            "online_route_objective": "route_time_money_segment_v5_signed_fare_audit over a shared, complete, directed-legal candidate pool",
            "time_money_v5_version": "route_time_money_segment_v5_signed_fare_audit",
            "time_money_v5_profiles": {
                "4:6": {"profile_id": "money_priority_0_4_0_6", "time_weight": 0.4, "money_weight": 0.6},
                "6:4": {"profile_id": "time_priority_0_6_0_4", "time_weight": 0.6, "money_weight": 0.4},
            },
            "time_money_v5_status": v5_zero_call_candidates["preflight_status"],
            "preflight_distinct_feasible_route_pair_count": v5_zero_call_candidates["distinct_feasible_route_pair_count"],
            "preflight_shared_link_candidate_present": v5_zero_call_candidates["shared_link_candidate_present"],
            "fare_assumption": "3.5 CNY/passenger-km, scenario assumption only; not observed Porto fare",
            "electricity_price_assumption": f"EUR {ELECTRICITY_PRICE_EUR_PER_KWH}/kWh converted to CNY using ECB 2026-10-28 EUR/CNY {ECB_EUR_CNY}",
            "method_cost_scope": "API-standard-equivalent token expense; compute expense excluded; unknown in-scope cost blocks both profile rankings",
        },
        "energy_module": {
            "version": "ev_longitudinal_v2",
            "profile_id": "generic_reference_ev_v1",
            "profile_sha256": r1.ENERGY_PROFILE.fingerprint(),
            "source_sha256": sha256_file(ENERGY_MODULE_PATH),
            "application": "candidate preselection from signed-net energy estimates and post-run reconciliation from SUMO speed traces; segment bill clips only after signed-net aggregation",
        },
        "artifacts": {},
    }
    preflight_dir = run_dir / "preflight"
    requests_dir = preflight_dir / "requests"
    requests_dir.mkdir(parents=True)
    write_json(preflight_dir / "decision_v5_zero_call_candidate_pool.json", v5_zero_call_candidates)
    (requests_dir / "request_0001.prompt.txt").write_text(prompt, encoding="utf-8")
    write_json(requests_dir / "request_0001.schema.json", schema)
    write_json(requests_dir / "request_0001.task.json", task)
    shutil.copy2(SHARED_GRAPH_PATH, preflight_dir / "shared_candidate_link_graph.json")
    task_graph_path = preflight_dir / "task_candidate_link_graph.json"
    write_json(task_graph_path, task_raw_graph)
    report["artifacts"] = {
        "prompt_sha256": sha256_file(requests_dir / "request_0001.prompt.txt"),
        "schema_sha256": sha256_file(requests_dir / "request_0001.schema.json"),
        "task_sha256": sha256_file(requests_dir / "request_0001.task.json"),
        "full_shared_graph_copy_sha256": sha256_file(preflight_dir / "shared_candidate_link_graph.json"),
        "task_candidate_link_graph_sha256": sha256_file(task_graph_path),
        "decision_v5_zero_call_candidate_pool_sha256": sha256_file(preflight_dir / "decision_v5_zero_call_candidate_pool.json"),
    }
    write_json(preflight_dir / "summary.json", report)
    base_manifest = {
        "run_id": run_dir.name,
        "status": "preflight_only_complete" if report["run_status"] == "completed_zero_model_calls" else "preflight_blocked",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "experiment_family": report["experiment_family"],
        "map_type": report["map_type"],
        "layer": report["layer"],
        "pilot_scope": report["pilot_scope"],
        "preflight_report_sha256": sha256_file(preflight_dir / "summary.json"),
        "model_calls": 0,
        "authorization": "user directly authorized immediate zero-call preflight and the first model trial",
        "source_provenance": report["source_provenance"],
    }
    write_json(run_dir / "manifest.json", base_manifest)
    return report


def run_pilot(run_dir: Path) -> dict[str, Any]:
    preflight_path = run_dir / "preflight/summary.json"
    if not preflight_path.is_file():
        raise RuntimeError("fresh_zero_call_preflight_required")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "preflight_only_complete" or preflight.get("run_status") != "completed_zero_model_calls":
        raise RuntimeError("zero_call_preflight_not_passed")
    if manifest.get("preflight_report_sha256") != sha256_file(preflight_path):
        raise RuntimeError("preflight_report_hash_mismatch")
    if preflight.get("pilot_scope", {}).get("order_id") != ORDER_ID:
        raise RuntimeError("preflight_order_id_mismatch")
    if preflight.get("source_provenance", {}).get("pilot_runner_sha256") != sha256_file(Path(__file__)):
        raise RuntimeError("pilot_runner_changed_after_zero_call_preflight")

    runner, runtime = load_runtime()
    r1, NetworkContext, RouteProblemBuilder, score_path, validate, _verify, estimate, codex_preflight, call_once, build_schema, serialize_schema, default_output_reserve = runtime
    source_preflight, source_manifest, frozen = prepare_runtime_verification(run_dir, runner, runtime, preflight)
    shared_raw_path = run_dir / "preflight/task_candidate_link_graph.json"
    if not shared_raw_path.is_file() or sha256_file(shared_raw_path) != preflight.get("artifacts", {}).get("task_candidate_link_graph_sha256"):
        raise RuntimeError("task_candidate_graph_hash_mismatch_after_zero_call_preflight")
    shared_raw = json.loads(shared_raw_path.read_text(encoding="utf-8"))
    shared_graph = runner.compact_candidate_graph_for_prompt(shared_raw)
    context = NetworkContext.load(r1, SEED)
    orders, _route_lookup = runner.orders_and_route_pairs(context)
    selected_order = next(row for row in orders if row["order_id"] == ORDER_ID)
    endpoints = {
        "vehicle_to_pickup": (context.orders_file["vehicles"][0]["initial_position"]["point_id"], selected_order["pickup"]["point_id"]),
        "pickup_to_dropoff": (selected_order["pickup"]["point_id"], selected_order["dropoff"]["point_id"]),
    }
    initial_by_vehicle = {row["vehicle_id"]: row["initial_position"]["point_id"] for row in context.orders_file["vehicles"]}
    endpoints["vehicle_to_pickup"] = (initial_by_vehicle[selected_order["vehicle_id"]], selected_order["pickup"]["point_id"])
    endpoint_ids = [endpoints["vehicle_to_pickup"][0], endpoints["vehicle_to_pickup"][1], endpoints["pickup_to_dropoff"][1]]
    captured: dict[str, Any] = {}
    original_build = RouteProblemBuilder.build_order_request
    original_score_path = score_path
    original_candidate_fallback_path = runner.candidate_fallback_path
    captured.update({
        "_context": context,
        "_score_path": score_path,
        "_shared_graph": shared_graph,
        "source_provenance": copy.deepcopy(preflight.get("source_provenance", {})),
    })

    def candidate_fallback_path_for_online_selector(graph, start_id, end_id):
        # The shared-Link path remains in the prevalidated candidate pool. Once
        # v5 has selected a complete route pair, disable the legacy FASTEST-only
        # post-selection override in the R2 runner for this one online order.
        if captured.get("v5_online_selector_applied"):
            return None
        return original_candidate_fallback_path(graph, start_id, end_id)

    runner.candidate_fallback_path = candidate_fallback_path_for_online_selector

    def score_path_on_model_arrival(r1_arg, leg, path, objective, now, traffic, tls_snapshot, start_offset_s=0.0):
        if leg.role == "pickup_to_dropoff" and captured.get("model_vehicle_to_pickup_time_s") is not None:
            start_offset_s = float(captured["model_vehicle_to_pickup_time_s"])
        return original_score_path(r1_arg, leg, path, objective, now, traffic, tls_snapshot, start_offset_s=start_offset_s)

    original_validator = validate

    def validate_and_capture_model_arrival(*args, **kwargs):
        r1_arg, response, legs, objective, now, traffic, tls_snapshot = args[:7]
        audit, routes, segment_audit = validate_routes_incrementally(
            runner, r1_arg, original_validator, score_path_on_model_arrival, response, legs, objective, now,
            traffic, tls_snapshot, shared_graph, captured.get("route_endpoints", endpoints), context,
        )
        captured["model_segment_validation"] = segment_audit
        captured["pre_sumo_verified_routes"] = copy.deepcopy(routes)
        captured["model_proposal_verified_routes"] = copy.deepcopy(routes)
        final_response_path = run_dir / "codex_requests/call_0000.final.json"
        ledger_path = run_dir / "llm_call_ledger.jsonl"
        captured["model_response_sha256"] = sha256_file(final_response_path) if final_response_path.is_file() else None
        captured["usage_ledger_sha256"] = sha256_file(ledger_path) if ledger_path.is_file() else None
        usage_ledger = first_call_ledger(run_dir) or {}
        usage_available = bool(usage_ledger.get("usage_available")) and isinstance(usage_ledger.get("input_tokens"), int) and isinstance(usage_ledger.get("output_tokens"), int)
        usage_snapshot = {
            "input_tokens": usage_ledger.get("input_tokens"),
            "output_tokens": usage_ledger.get("output_tokens"),
        }
        api_rates = captured.get("api_rates", {})
        candidate_evaluation = build_v5_candidate_evaluation(
            runner, runtime, captured, usage_snapshot, usage_available, api_rates,
        )
        captured["v5_candidate_evaluation"] = candidate_evaluation
        write_json(run_dir / "decision_v5_candidate_pool_and_rankings.json", candidate_evaluation)
        if candidate_evaluation.get("status") != "selected_both_profiles":
            raise ValueError("v5_common_candidate_pool_blocked:" + candidate_evaluation.get("status", "unknown"))

        online_profile_id = V5_PROFILES[0]
        online_candidate_id = candidate_evaluation["selected_candidate_id_by_profile"][online_profile_id]
        online_candidate = next(
            row for row in candidate_evaluation["candidates"]
            if row["candidate_id"] == online_candidate_id
        )
        selected_paths = online_candidate["route_paths"]
        selected_routes = {}
        offset = 0.0
        for role in ("vehicle_to_pickup", "pickup_to_dropoff"):
            selected = score_path_on_model_arrival(
                r1_arg, legs[role], selected_paths[role], objective, now,
                traffic, tls_snapshot, start_offset_s=offset,
            )
            selected_routes[role] = selected
            if role == "vehicle_to_pickup":
                offset = float(selected["predicted_travel_time_s"])
        captured["v5_online_selector_applied"] = True
        captured["v5_online_profile_id"] = online_profile_id
        captured["v5_online_candidate_id"] = online_candidate_id
        captured["v5_online_selected_routes"] = copy.deepcopy(selected_routes)
        audit = copy.deepcopy(audit)
        for role, route in selected_routes.items():
            audit["routes"][role].update({
                "route_sha256": json_hash(route["path"]),
                "link_count": len(route["path"]),
                "selected_candidate_id": online_candidate_id,
                "selected_by_profile_id": online_profile_id,
            })
        write_json(run_dir / "pre_sumo_oneway_segment_audit.json", {
            "schema_version": "r4_porto_osm_incremental_directed_route_audit_v1",
            "map_type": "real OSM-derived Porto SUMO network",
            "layer": "route planning; route validation occurs before SUMO order execution",
            "request_key": captured.get("request_key"),
            "model_proposal_and_repair_checks": segment_audit,
            "v5_selected_candidate_id": online_candidate_id,
            "v5_selected_profile_id": online_profile_id,
            "selected_route_segments_guarded_before_sumo": True,
            "source_network_sha256": preflight.get("source_provenance", {}).get("network_sha256"),
        })
        vp = routes.get("vehicle_to_pickup")
        if vp is not None:
            captured["model_vehicle_to_pickup_time_s"] = float(vp["predicted_travel_time_s"])
        return audit, selected_routes

    def build_order_request_with_scoped_h(self, order, from_point_id, objective, now, traffic, tls_snapshot, request_key, **kwargs):
        legs, task, prompt = original_build(
            self, order, from_point_id, objective, now, traffic, tls_snapshot, request_key, **kwargs,
        )
        task, prompt = compact_request(runner, runtime, legs, task, prompt, shared_graph, endpoint_ids)
        schema_path = runner.PACKAGE / runner.SCHEMA_RELATIVE
        schema = constrain_route_schema_for_endpoints(
            build_schema(json.loads(schema_path.read_text(encoding="utf-8")), legs), task, legs,
        )
        estimated = int(estimate(prompt, schema))
        route_endpoints = {
            "vehicle_to_pickup": (from_point_id, order["pickup"]["point_id"]),
            "pickup_to_dropoff": (order["pickup"]["point_id"], order["dropoff"]["point_id"]),
        }
        fallback_response = build_candidate_fallback_response(runner, shared_graph, legs, route_endpoints)
        fallback_check = fallback_response is not None
        live_row = {
            "status": "passed" if estimated <= PROVIDER_INPUT_GATE and fallback_check else "blocked",
            "request_key": request_key,
            "sim_time_s": round(float(now), 6),
            "traffic_state_sha256": json_hash({"traffic": traffic, "tls": tls_snapshot, "sim_time_s": now}),
            "estimated_input_tokens_including_schema_and_cli_overhead": estimated,
            "provider_context_window_tokens": PROVIDER_CONTEXT_LIMIT,
            "output_reserve_tokens": OUTPUT_RESERVE_TOKENS,
            "provider_input_gate_tokens": PROVIDER_INPUT_GATE,
            "project_soft_input_target_tokens": SOFT_INPUT_TARGET,
            "soft_target_is_not_a_stop_line": True,
            "fallback_in_same_leg_graph": fallback_check,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "schema_sha256": hashlib.sha256(serialize_schema(schema)).hexdigest(),
            "actual_model_calls_at_gate": 0,
        }
        if "request_key" in captured:
            live_row["status"] = "blocked_duplicate_runtime_request_in_single_order_pilot"
        write_json(run_dir / "preflight/runtime_request.json", live_row)
        if live_row["status"] != "passed":
            raise RuntimeError("runtime_zero_call_gate_blocked:" + json.dumps(live_row, sort_keys=True))
        captured.update({
            "legs": legs, "task": task, "prompt": prompt, "schema": schema,
            "now": float(now), "traffic": copy.deepcopy(traffic), "tls_snapshot": copy.deepcopy(tls_snapshot),
            "route_endpoints": route_endpoints, "request_key": request_key,
        })
        return legs, task, prompt

    def build_response_schema_for_current_task(base_schema, legs, max_route_items=None):
        task = captured.get("task")
        if not isinstance(task, dict):
            raise RuntimeError("endpoint_task_missing_before_response_schema_build")
        schema = build_schema(base_schema, legs, max_route_items=max_route_items)
        schema = constrain_route_schema_for_endpoints(schema, task, captured["legs"])
        branch_by_role = {
            branch["properties"]["leg_role"]["enum"][0]: branch
            for branch in schema["properties"]["routes"]["items"]["anyOf"]
        }
        for role, task_key in (("vehicle_to_pickup", "vp"), ("pickup_to_dropoff", "pd")):
            required_min_items = minimum_intermediate_link_count(captured["legs"][role])
            actual_min_items = branch_by_role[role]["properties"]["intermediate_edge_ids"].get("minItems", 0)
            if actual_min_items != required_min_items:
                raise RuntimeError(f"endpoint_route_schema_min_items_mismatch:{role}:{actual_min_items}:{required_min_items}")
        return schema

    RouteProblemBuilder.build_order_request = build_order_request_with_scoped_h
    original_point_bindings = r1.point_bindings

    def one_order_point_bindings(seed: int):
        points, inputs = original_point_bindings(seed)
        if seed != SEED:
            return points, inputs
        subset = copy.deepcopy(inputs)
        order_doc = subset["orders"]
        order_doc["orders"] = [row for row in order_doc["orders"] if row["order_id"] == ORDER_ID]
        order_doc["route_legs"] = [row for row in order_doc["route_legs"] if row["order_id"] == ORDER_ID]
        if len(order_doc["orders"]) != 1 or len(order_doc["route_legs"]) != 2:
            raise RuntimeError("single_order_frozen_input_slice_invalid")
        return points, subset

    r1.point_bindings = one_order_point_bindings
    prices_snapshot = json.loads(runner.PRICE_PATH.read_text(encoding="utf-8"))
    codex_rates = prices_snapshot["codex_standard_credits_per_million_tokens"]
    api_rates = prices_snapshot["api_standard_usd_per_million_tokens"]
    prices = {
        "codex_input_credits_per_million": float(codex_rates["input"]),
        "codex_cached_input_credits_per_million": float(codex_rates["cached_input"]),
        "codex_output_credits_per_million": float(codex_rates["output"]),
        "api_input_usd_per_million": float(api_rates["input"]),
        "api_output_usd_per_million": float(api_rates["output"]),
    }
    captured["api_rates"] = {
        "input_usd_per_million": prices["api_input_usd_per_million"],
        "output_usd_per_million": prices["api_output_usd_per_million"],
    }
    captured["api_rate_card_sha256"] = sha256_file(Path(runner.PRICE_PATH))
    cli_info = codex_preflight()
    if not cli_info.get("available"):
        raise RuntimeError("codex_cli_preflight_unavailable_before_model_request")
    # The adapter accepts numeric compatibility fields. Only the first order is in scope;
    # these large sentinels are not project or account stop-lines.
    token_sentinel = 2**63 - 1
    credit_sentinel = 1e30
    call_ledger = run_dir / "llm_call_ledger.jsonl"
    requests_dir = run_dir / "codex_requests"
    model_budget = {
        "max_model_calls": PILOT_CALL_SCOPE,
        "cumulative_token_budget": token_sentinel,
        "cumulative_credit_budget": credit_sentinel,
        "max_estimated_input_tokens": PROVIDER_INPUT_GATE,
        "call_slots_used": 0,
        "actual_model_calls": 0,
        "usage_so_far": {"tokens": 0, "credits": 0.0},
        "request_namespace": "",
        "stop_reason": None,
        "call_once": call_once,
        "estimate_input_tokens": estimate,
        "build_response_schema": build_response_schema_for_current_task,
        "output_reserve_tokens": OUTPUT_RESERVE_TOKENS,
        "pilot_route_item_cap": None,
        "validate_model_routes": validate,
        "schema": json.loads((runner.PACKAGE / runner.SCHEMA_RELATIVE).read_text(encoding="utf-8")),
        "prices": prices,
        "cli_info": cli_info,
        "requests_dir": requests_dir,
        "call_ledger_path": call_ledger,
        "manifest_path": manifest_path,
    }
    model_budget["validate_model_routes"] = validate_and_capture_model_arrival
    manifest.update({"status": "running_first_model_trial", "run_started_utc": datetime.now(timezone.utc).isoformat(), "request_scope": "one frozen order, two route legs", "technical_adapter_call_scope": PILOT_CALL_SCOPE})
    write_json(manifest_path, manifest)
    try:
        dynamic_run = runner.make_r2_run_class(r1)
        experiment = dynamic_run(
            r1, context, RouteProblemBuilder(context), score_path_on_model_arrival, SEED, OBJECTIVE,
            run_dir / "sumo_trial", 90, 2.0,
            planner_mode="codex_cli", model_budget=model_budget,
            shared_candidate_link_graph=shared_graph,
        )
        summary = experiment.run()
        summary.update({
            "experiment_id": run_dir.name,
            "method": "R4_PORTO_OSM_CODEX_ROUTE_PILOT",
            "map_type": "real OSM-derived Porto SUMO network",
            "layer": "route planning; no dispatch optimization",
            "pilot_scope": {"seed": SEED, "order_id": ORDER_ID, "vehicle_id": selected_order["vehicle_id"], "objective": OBJECTIVE, "orders_in_scope": 1},
            "formal_matrix_result": False,
        })
        write_json(run_dir / "sumo_trial/summary.json", summary)
        # The first SUMO run is selected online by the 4:6 profile inside the
        # model validator callback. Restore shared-route lookup before the
        # second profile replay so its prevalidated Link candidate remains in
        # the scenario graph.
        runner.candidate_fallback_path = original_candidate_fallback_path
        comparison = write_route_comparison(runner, runtime, run_dir, captured, shared_graph, endpoints, summary)
        ev2 = audit_energy_traces(r1, run_dir)
        candidate_evaluation = captured.get("v5_candidate_evaluation", {})
        profile_replay = None
        second_profile_ev2 = None
        profile_ids = candidate_evaluation.get("selected_candidate_id_by_profile", {})
        first_candidate_id = profile_ids.get(V5_PROFILES[0])
        second_candidate_id = profile_ids.get(V5_PROFILES[1])
        if candidate_evaluation.get("status") == "selected_both_profiles" and first_candidate_id == second_candidate_id:
            reuse_audit = {
                "status": "passed" if comparison.get("status") == "v5_online_selected_route_executed_and_oneway_verified" else "failed_or_incomplete",
                "profile_id": V5_PROFILES[1],
                "candidate_id": second_candidate_id,
                "execution_legs": comparison.get("legs", {}),
                "summary_sha256": sha256_file(run_dir / "sumo_trial/summary.json"),
                "model_calls": 0,
                "reused_same_selected_route": True,
            }
            profile_replay = {
                "run_dir": str(run_dir / "sumo_trial"),
                "execution_audit": reuse_audit,
                "reused_same_route": True,
            }
            second_profile_ev2 = ev2
        elif candidate_evaluation.get("status") == "selected_both_profiles" and second_candidate_id:
            second_candidate = next(
                row for row in candidate_evaluation["candidates"]
                if row["candidate_id"] == second_candidate_id
            )
            # This is a deterministic SUMO replay after the single model response.
            # Bypass the live-request wrapper so the replay cannot be mistaken for
            # a duplicate model request.
            RouteProblemBuilder.build_order_request = original_build
            try:
                profile_replay = execute_fixed_v5_profile(
                    runner, runtime, run_dir, V5_PROFILES[1], second_candidate,
                    shared_graph, captured,
                )
            finally:
                RouteProblemBuilder.build_order_request = build_order_request_with_scoped_h
            profile_dir = Path(profile_replay["run_dir"])
            second_profile_ev2 = audit_energy_traces(r1, run_dir, profile_dir)
            profile_replay["execution_audit"]["ev2_audit_status"] = second_profile_ev2.get("status")
            profile_replay["execution_audit"]["ev2_audit_sha256"] = sha256_file(profile_dir / "ev2_trace_audit.json") if (profile_dir / "ev2_trace_audit.json").is_file() else None
            write_json(profile_dir / "v5_profile_execution_audit.json", profile_replay["execution_audit"])
        decision = audit_v5_gate(
            run_dir, candidate_evaluation, comparison, profile_replay, ev2, second_profile_ev2,
        )
        terminal = summary.get("terminal_order_count") == 1 and summary.get("orders_completed") == 1
        route_guard_passed = decision.get("oneway_full_flow_gate_passed") is True
        model_calls = int(model_budget.get("actual_model_calls", 0))
        if model_calls > 1:
            raise RuntimeError("pilot_call_scope_exceeded")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ledger = first_call_ledger(run_dir) or {}
        final_response_exists = (run_dir / "codex_requests/call_0000.final.json").is_file()
        usage_available = bool(ledger.get("usage_available")) and isinstance(ledger.get("input_tokens"), int) and isinstance(ledger.get("output_tokens"), int)
        model_candidate_id = "model_proposal_or_repaired"
        selected_model_legs = (
            2 if first_candidate_id == model_candidate_id else 0
        )
        model_segment_validation = captured.get("model_segment_validation", {})
        valid_model_legs = sum(
            row.get("proposal_status") == "passed_every_directed_segment"
            for row in model_segment_validation.values()
        )
        if decision.get("status") == "passed_both_profiles_full_flow_no_oneway_violation":
            model_outcome = "same_model_response_scored_by_both_v5_profiles"
            simulation_outcome = "completed_both_v5_profile_routes"
        elif final_response_exists:
            model_outcome = "model_response_received_and_routes_guarded"
            simulation_outcome = "completed_with_profile_or_energy_gate_incomplete"
        elif model_calls:
            request_status = ledger.get("status")
            if request_status == "provider_rejected_invalid_json_schema":
                model_outcome = "provider_rejected_invalid_json_schema_no_model_response"
            elif request_status == "incomplete_response_max_output_tokens":
                model_outcome = "incomplete_response_max_output_tokens_no_model_response"
            else:
                model_outcome = "cli_request_failed_without_model_response"
            simulation_outcome = "not_completed"
        else:
            model_outcome = "no_model_route_result"
            simulation_outcome = "not_completed"
        direct_oneway_violation = (
            comparison.get("executed_route_oneway_guard_status") == "failed"
            or any(
                row.get("execution_audit", {}).get("status") == "failed_oneway_guard"
                or any(
                    leg.get("oneway_guard_status") == "failed"
                    and leg.get("oneway_failure") is not None
                    and leg.get("oneway_failure", {}).get("reason") != "executed_route_missing"
                    for leg in row.get("execution_audit", {}).get("execution_legs", {}).values()
                )
                for row in decision.get("profile_execution", {}).values()
            )
        )
        manifest.update({
            "status": (
                "pilot_route_complete" if terminal and route_guard_passed and decision.get("status") == "passed_both_profiles_full_flow_no_oneway_violation"
                else "pilot_route_guard_failed" if direct_oneway_violation
                else "pilot_v5_decision_blocked_or_profile_replay_incomplete" if final_response_exists
                else "pilot_provider_or_cli_request_failed_no_route" if model_calls and not final_response_exists
                else "pilot_model_response_received_simulation_not_completed" if model_calls
                else "pilot_blocked_before_model_call"
            ),
            "model_route_outcome": model_outcome,
            "simulation_outcome": simulation_outcome,
            "model_routes_valid_count": valid_model_legs,
            "model_proposal_routes_valid_count": sum(
                row.get("proposal_status") == "passed_every_directed_segment"
                for row in model_segment_validation.values()
            ),
            "model_proposal_routes_repaired_count": sum(
                row.get("proposal_status") == "rejected_at_directed_segment_and_researched"
                for row in model_segment_validation.values()
            ),
            "model_routes_selected_count": selected_model_legs,
            "model_calls": model_calls,
            "model_call_attempts": model_calls,
            "accepted_model_response_count": int(final_response_exists and ledger.get("status") == "accepted"),
            "provider_request_status": ledger.get("status"),
            "usage_available": usage_available,
            "model_request_scope": f"one seed-{SEED} Porto OSM order {ORDER_ID}, shared route candidate set and two v5 weight profiles",
            "codex_usage_tokens_observed": int(model_budget["usage_so_far"].get("tokens", 0)) if usage_available else None,
            "codex_standard_credits_estimate": round(float(model_budget["usage_so_far"].get("credits", 0.0)), 8) if usage_available else None,
            "codex_account_billing": "not reported by Codex CLI",
            "route_comparison_status": comparison.get("status"),
            "pre_sumo_oneway_guard_status": comparison.get("pre_sumo_guard_status"),
            "executed_route_oneway_guard_status": "passed" if route_guard_passed else "failed_or_incomplete",
            "route_guard_passed": route_guard_passed,
            "pilot_full_flow_gate_passed": bool(terminal and route_guard_passed and decision.get("status") == "passed_both_profiles_full_flow_no_oneway_violation"),
            "ev2_audit_status": ev2.get("status"),
            "time_money_v5_status": decision.get("status"),
            "v5_profile_selected_candidate_ids": profile_ids,
            "sumo_trial_summary_sha256": sha256_file(run_dir / "sumo_trial/summary.json"),
            "runtime_request_preflight_sha256": sha256_file(run_dir / "preflight/runtime_request.json") if (run_dir / "preflight/runtime_request.json").exists() else None,
        })
        write_json(manifest_path, manifest)
        return {"manifest": manifest, "summary": summary, "route_comparison": comparison, "ev2": ev2, "decision_v5": decision, "profile_replay": profile_replay, "profile_ev2": second_profile_ev2}
    finally:
        RouteProblemBuilder.build_order_request = original_build
        runner.candidate_fallback_path = original_candidate_fallback_path
        r1.point_bindings = original_point_bindings


def prepare_runtime_verification(run_dir, runner, runtime, preflight):
    source_preflight = json.loads(SHARED_PREFLIGHT_SUMMARY.read_text(encoding="utf-8"))
    source_manifest = json.loads(SHARED_PREFLIGHT_MANIFEST.read_text(encoding="utf-8"))
    frozen = runtime[5](runtime[0], list(range(10)))
    expected = preflight["source_provenance"]
    if sha256_file(Path(runtime[0].__file__)) != expected["r1_runner_sha256"]:
        raise RuntimeError("r1_runner_changed_after_zero_call_preflight")
    if sha256_file(SHARED_GRAPH_PATH) != expected["shared_candidate_link_graph_sha256"]:
        raise RuntimeError("shared_candidate_graph_changed_after_zero_call_preflight")
    if frozen["network_sha256"] != expected["network_sha256"]:
        raise RuntimeError("network_changed_after_zero_call_preflight")
    return source_preflight, source_manifest, frozen


def first_call_ledger(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "llm_call_ledger.jsonl"
    if not path.is_file():
        return None
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(rows[0]) if rows else None


def build_v5_candidate_evaluation(runner, runtime, captured, usage, usage_available, api_rates):
    """Build one complete, same-snapshot order pool and score both user profiles."""
    decision_module = load_module("porto_r4_pilot_decision", DECISION_MODULE_PATH)
    r1 = runtime[0]
    context = captured["_context"]
    legs = captured["legs"]
    now = float(captured["now"])
    traffic = captured["traffic"]
    tls_snapshot = captured["tls_snapshot"]
    score_path = captured["_score_path"]
    shared_graph = captured["_shared_graph"]
    route_endpoints = captured["route_endpoints"]
    candidate_builder = runtime[2](context)

    model_method_cost_cny = None
    model_api_equivalent = None
    if usage_available:
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        api_cost_usd = (
            input_tokens * float(api_rates["input_usd_per_million"])
            + output_tokens * float(api_rates["output_usd_per_million"])
        ) / 1_000_000.0
        model_method_cost_cny = api_cost_usd * USD_TO_CNY
        model_api_equivalent = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "api_standard_equivalent_usd": api_cost_usd,
            "api_standard_equivalent_cny": model_method_cost_cny,
            "rate_basis": "frozen published API-standard uncached token rate card; not Codex account billing",
        }

    model_routes = captured.get("pre_sumo_verified_routes", {})
    methods = (
        "model_proposal_or_repaired",
        "dynamic_fastest",
        "signed_net_ev2",
        "static_free_flow_dijkstra",
        "shared_candidate_link_dijkstra",
    )

    def build_path(method, role, start_offset_s):
        leg = legs[role]
        payload = float(r1.ENERGY_PROFILE.passenger_load_kg) if role == "pickup_to_dropoff" else 0.0
        if method == "model_proposal_or_repaired":
            row = model_routes.get(role)
            return None if row is None else [str(edge_id) for edge_id in row.get("path", [])]
        if method == "dynamic_fastest":
            route = leg.crop.router.route(
                leg.start, leg.end, "FASTEST", now, traffic, tls_snapshot,
                start_time_offset_s=start_offset_s, payload_mass_kg=payload,
            )
            return [str(edge_id) for edge_id in route.get("path", [])] if route.get("status") == "ok" else None
        if method == "signed_net_ev2":
            route = candidate_builder.route_signed_net_ev2(
                leg, now, traffic, tls_snapshot,
                start_time_offset_s=start_offset_s, payload_mass_kg=payload,
            )
            return [str(edge_id) for edge_id in route.get("path", [])] if route.get("status") == "ok" else None
        if method == "static_free_flow_dijkstra":
            return [str(edge_id) for edge_id in leg.crop.static_path] if leg.crop.static_path else None
        if method == "shared_candidate_link_dijkstra":
            start_id, end_id = route_endpoints[role]
            route = runner.candidate_fallback_path(shared_graph, start_id, end_id)
            return [str(edge_id) for edge_id in route] if route else None
        raise ValueError("unknown_candidate_method:" + method)

    candidate_details = []
    for method in methods:
        route_paths = {}
        route_scores = {}
        route_failure = None
        offset = 0.0
        for role in ("vehicle_to_pickup", "pickup_to_dropoff"):
            try:
                path = build_path(method, role, offset)
                if not path:
                    route_failure = {"leg_role": role, "reason": "candidate_route_unavailable"}
                    break
                valid, reason = _path_passes_directed_guard(
                    r1, legs[role], path, score_path, OBJECTIVE, now, traffic, tls_snapshot,
                )
                if not valid:
                    route_failure = {"leg_role": role, "reason": reason or "directed_guard_failed"}
                    break
                scored = score_path(
                    r1, legs[role], path, OBJECTIVE, now, traffic, tls_snapshot,
                    start_offset_s=offset,
                )
                route_paths[role] = path
                route_scores[role] = scored
                if role == "vehicle_to_pickup":
                    offset = float(scored["predicted_travel_time_s"])
            except (ValueError, TypeError, KeyError, RuntimeError) as error:
                route_failure = {"leg_role": role, "reason": f"{type(error).__name__}:{error}"}
                break

        candidate_id = method
        if route_failure is not None:
            candidate_details.append({
                "candidate_id": candidate_id,
                "method": method,
                "feasible": False,
                "failure": route_failure,
                "travel_time_sec": None,
                "money_cost": None,
                "route_paths": route_paths,
            })
            continue

        segment_rows = [
            {
                "segment_id": role,
                "segment_type": "deadhead" if role == "vehicle_to_pickup" else "passenger",
                "distance_m": float(route_scores[role]["distance_m"]),
                "energy_kwh": float(route_scores[role]["predicted_energy_wh"]) / 1000.0,
                "fare_per_km": FARE_PER_PASSENGER_KM_CNY if role == "pickup_to_dropoff" else None,
                "fare_basis": "assumed_scenario_revenue_3_5_CNY_per_passenger_km_not_observed_fare",
                "price_basis": "ECB_reference_2026-10-28_EUR_CNY_7.4217; electricity tariff assumed EUR_0.30_per_kWh",
            }
            for role in ("vehicle_to_pickup", "pickup_to_dropoff")
        ]
        economics = decision_module.aggregate_route_segment_economics(
            segment_rows,
            currency="CNY",
            electricity_price_per_kwh=ELECTRICITY_PRICE_CNY_PER_KWH,
            fare_per_km=FARE_PER_PASSENGER_KM_CNY,
            price_basis="EUR_0.30_per_kWh converted using ECB 2026-10-28 EUR/CNY 7.4217",
            fare_basis="assumed 3.5 CNY per passenger-km; scenario assumption, not observed fare",
        )
        method_cost = model_method_cost_cny if method == "model_proposal_or_repaired" else 0.0
        money_cost = (
            None if economics.get("net_economic_cost") is None or method_cost is None
            else float(economics["net_economic_cost"]) + float(method_cost)
        )
        travel_time = sum(float(route_scores[role]["predicted_travel_time_s"]) for role in route_scores)
        candidate_details.append({
            "candidate_id": candidate_id,
            "method": method,
            "feasible": True,
            "travel_time_sec": travel_time,
            "money_cost": money_cost,
            "route_net_economic_cost_before_method": economics.get("net_economic_cost"),
            "method_api_token_cost_cny": method_cost,
            "method_api_token_cost_basis": (
                "actual observed token usage priced at frozen API-standard equivalent"
                if method == "model_proposal_or_repaired" and usage_available
                else "known zero API/token usage for deterministic candidate method"
                if method != "model_proposal_or_repaired"
                else "null: model token usage/rate unavailable"
            ),
            "money_cost_currency": "CNY",
            "route_paths": route_paths,
            "path_sha256_by_leg": {role: json_hash(path) for role, path in route_paths.items()},
            "leg_scores": {
                role: {
                    "travel_time_sec": route_scores[role]["predicted_travel_time_s"],
                    "signed_net_energy_kwh": float(route_scores[role]["predicted_energy_wh"]) / 1000.0,
                    "distance_m": route_scores[role]["distance_m"],
                }
                for role in route_scores
            },
            "segment_economics": economics,
        })

    scorer_inputs = [
        {
            "candidate_id": row["candidate_id"],
            "feasible": row["feasible"],
            "travel_time_sec": row.get("travel_time_sec"),
            "money_cost": row.get("money_cost"),
        }
        for row in candidate_details
    ]
    rankings = {
        profile: decision_module.score_route_candidates(scorer_inputs, profile, currency="CNY")
        for profile in V5_PROFILES
    }
    selected = {
        profile: ranking.get("selected_candidate_id")
        for profile, ranking in rankings.items()
    }
    snapshot_sha = json_hash({"traffic": traffic, "tls": tls_snapshot, "sim_time_s": now})
    status = "selected_both_profiles" if all(selected.values()) else "blocked_incomplete_candidate_data"
    return {
        "status": status,
        "decision_model_version": decision_module.DECISION_MODEL_VERSION,
        "source_sha256": sha256_file(DECISION_MODULE_PATH),
        "source_provenance": {
            **captured.get("source_provenance", {}),
            "decision_module_sha256": sha256_file(DECISION_MODULE_PATH),
            "energy_model_module_sha256": sha256_file(ENERGY_MODULE_PATH),
            "energy_profile_id": r1.ENERGY_PROFILE.profile_id,
            "energy_profile_sha256": r1.ENERGY_PROFILE.fingerprint(),
            "api_rate_card_sha256": captured.get("api_rate_card_sha256"),
            "model_response_sha256": captured.get("model_response_sha256"),
            "usage_ledger_sha256": captured.get("usage_ledger_sha256"),
        },
        "same_frozen_snapshot_sha256": snapshot_sha,
        "candidate_pool_shared_across_profiles": True,
        "candidate_methods": list(methods),
        "candidates": candidate_details,
        "scorer_inputs": scorer_inputs,
        "profiles": rankings,
        "selected_candidate_id_by_profile": selected,
        "profile_order_mapping": {
            "4:6": {"time_weight": 0.4, "money_weight": 0.6, "profile_id": "money_priority_0_4_0_6"},
            "6:4": {"time_weight": 0.6, "money_weight": 0.4, "profile_id": "time_priority_0_6_0_4"},
        },
        "fare_assumption": {
            "fare_per_passenger_km_cny": FARE_PER_PASSENGER_KM_CNY,
            "basis": "explicit scenario assumption authorized by user; not observed Porto fare",
        },
        "electricity_assumption": {
            "eur_per_kwh": ELECTRICITY_PRICE_EUR_PER_KWH,
            "eur_cny_reference_rate": ECB_EUR_CNY,
            "cny_per_kwh": ELECTRICITY_PRICE_CNY_PER_KWH,
            "fx_source": "ECB 2026-10-28 euro reference rate",
        },
        "method_cost_scope": "API-standard-equivalent token expense only; local compute expense explicitly excluded; unknown in-scope token cost remains null and blocks both profiles",
        "model_method_api_equivalent": model_api_equivalent,
        "actual_codex_account_billing": "not reported by Codex CLI",
        "snapshot_scope": "one frozen order with vehicle-to-pickup and passenger-service segments; shared time/traffic/signal snapshot; passenger segment time evaluated after each candidate pickup leg",
        "oneway_guard_applied_to_every_feasible_candidate_before_scoring": True,
        "signed_energy_billing": "sum signed EV2 link energy within each journey segment, then max(segment_net_kWh, 0) times tariff",
    }


def execute_fixed_v5_profile(runner, runtime, run_dir, profile_id, candidate, shared_graph, captured):
    """Run a no-model SUMO replay using the route pair selected by one v5 profile."""
    r1, NetworkContext, RouteProblemBuilder, score_path = runtime[:4]
    profile_dir = run_dir / "sumo_profiles" / profile_id
    if profile_dir.exists():
        raise RuntimeError("profile_sumo_directory_already_exists:" + profile_id)
    profile_context = NetworkContext.load(r1, SEED)
    original_build = RouteProblemBuilder.build_order_request
    selected_paths = candidate["route_paths"]

    def build_order_request_with_fixed_candidate(self, order, from_point_id, objective, now, traffic, tls_snapshot, request_key, **kwargs):
        legs, task, prompt = original_build(
            self, order, from_point_id, objective, now, traffic, tls_snapshot, request_key, **kwargs,
        )
        if order.get("order_id") != ORDER_ID:
            raise RuntimeError("fixed_v5_profile_received_unexpected_order")
        for role, leg in legs.items():
            path = [str(edge_id) for edge_id in selected_paths[role]]

            def fixed_route(start, end, route_objective, route_now, route_traffic, route_tls, _path=path, _leg=leg, _role=role, **route_kwargs):
                if _path[0] != str(_leg.start["edge_id"]) or _path[-1] != str(_leg.end["edge_id"]):
                    raise RuntimeError("fixed_v5_route_endpoint_mismatch:" + _role)
                return {"status": "ok", "path": _path}

            leg.crop.router.route = fixed_route
        return legs, task, prompt

    RouteProblemBuilder.build_order_request = build_order_request_with_fixed_candidate
    try:
        experiment = runner.make_r2_run_class(r1)(
            r1, profile_context, RouteProblemBuilder(profile_context), score_path,
            SEED, OBJECTIVE, profile_dir, 90, 2.0,
            planner_mode="traditional", model_budget=None,
            shared_candidate_link_graph=shared_graph,
        )
        summary = experiment.run()
    finally:
        RouteProblemBuilder.build_order_request = original_build

    summary.update({
        "experiment_id": run_dir.name + "__" + profile_id,
        "method": "R4_PORTO_OSM_V5_SELECTED_ROUTE_REPLAY",
        "map_type": "real OSM-derived Porto SUMO network",
        "layer": "route planning; no dispatch optimization",
        "decision_model_version": "route_time_money_segment_v5_signed_fare_audit",
        "decision_profile_id": profile_id,
        "selected_candidate_id": candidate["candidate_id"],
        "selected_path_sha256_by_leg": candidate["path_sha256_by_leg"],
        "model_calls": 0,
        "formal_matrix_result": False,
    })
    write_json(profile_dir / "summary.json", summary)

    actual_path = profile_dir / "route_legs_actual.json"
    executed_rows = json.loads(actual_path.read_text(encoding="utf-8")) if actual_path.is_file() else []
    by_role = {row.get("leg_role"): row for row in executed_rows if row.get("order_id") == ORDER_ID}
    execution_legs = {}
    for role in ("vehicle_to_pickup", "pickup_to_dropoff"):
        row = by_role.get(role)
        path = [str(edge_id) for edge_id in (row or {}).get("route_edges", [])]
        expected = [str(edge_id) for edge_id in selected_paths[role]]
        directed_failure = (
            _first_directed_segment_failure(r1, captured["legs"][role], path)
            if path else {"reason": "executed_route_missing"}
        )
        execution_legs[role] = {
            "status": "passed" if row and path == expected and directed_failure is None else "failed_or_incomplete",
            "selected_path_sha256": json_hash(expected),
            "executed_path_sha256": json_hash(path) if path else None,
            "executed_matches_selected_candidate": bool(row and path == expected),
            "oneway_guard_status": "passed" if directed_failure is None else "failed",
            "oneway_failure": directed_failure,
        }
    terminal = summary.get("terminal_order_count") == 1 and summary.get("orders_completed") == 1
    report = {
        "status": "passed" if terminal and all(row["status"] == "passed" for row in execution_legs.values()) else "failed_or_incomplete",
        "profile_id": profile_id,
        "candidate_id": candidate["candidate_id"],
        "terminal_order_count": summary.get("terminal_order_count"),
        "orders_completed": summary.get("orders_completed"),
        "execution_legs": execution_legs,
        "summary_sha256": sha256_file(profile_dir / "summary.json"),
        "route_legs_actual_sha256": sha256_file(actual_path) if actual_path.is_file() else None,
        "model_calls": 0,
    }
    write_json(profile_dir / "v5_profile_execution_audit.json", report)
    return {"summary": summary, "execution_audit": report, "run_dir": str(profile_dir)}


def write_route_comparison(runner, runtime, run_dir, captured, shared_graph, endpoints, summary):
    if not captured:
        report = {"status": "blocked_no_live_request_capture", "model_call_or_route_claim": False}
        write_json(run_dir / "same_snapshot_route_comparison.json", report)
        return report
    r1 = runtime[0]
    response_path = run_dir / "codex_requests/call_0000.final.json"
    if not response_path.is_file():
        ledger = first_call_ledger(run_dir) or {}
        report = {
            "status": "no_final_model_response",
            "request_status": ledger.get("status"),
            "cli_exit_code": ledger.get("cli_exit_code"),
            "usage_available": ledger.get("usage_available", False),
            "model_response_received": False,
            "model_call_or_route_claim": False,
        }
        write_json(run_dir / "same_snapshot_route_comparison.json", report)
        return report
    response = json.loads(response_path.read_text(encoding="utf-8"))
    evaluation = captured.get("v5_candidate_evaluation", {})
    candidate_by_id = {row["candidate_id"]: row for row in evaluation.get("candidates", [])}
    selected_candidate_id = captured.get("v5_online_candidate_id")
    selected_candidate = candidate_by_id.get(selected_candidate_id, {})
    selected_paths = selected_candidate.get("route_paths", {})
    segment_audit = captured.get("model_segment_validation", {})
    comparison = {
        "status": "blocked_v5_candidate_selection_missing",
        "decision_model_version": evaluation.get("decision_model_version"),
        "online_profile_id": captured.get("v5_online_profile_id"),
        "selected_candidate_id": selected_candidate_id,
        "snapshot_sha256": json_hash({"traffic": captured["traffic"], "tls": captured["tls_snapshot"], "sim_time_s": captured["now"]}),
        "model_response_sha256": json_hash(response),
        "pre_sumo_guard_status": "passed" if selected_paths and set(selected_paths) == set(captured["legs"]) and evaluation.get("status") == "selected_both_profiles" else "blocked_missing_verified_v5_selected_routes",
        "candidate_pool_shared_across_profiles": evaluation.get("candidate_pool_shared_across_profiles"),
        "legs": {},
    }
    executed_rows_path = run_dir / "sumo_trial/route_legs_actual.json"
    executed_roles = set()
    if executed_rows_path.exists():
        executed_rows = json.loads(executed_rows_path.read_text(encoding="utf-8"))
        by_role = {row.get("leg_role"): row for row in executed_rows if row.get("order_id") == ORDER_ID}
        for role, row in by_role.items():
            if role not in captured["legs"]:
                continue
            executed = [str(edge_id) for edge_id in row.get("route_edges", [])]
            failure = _first_directed_segment_failure(r1, captured["legs"][role], executed)
            expected = [str(edge_id) for edge_id in selected_paths.get(role, [])]
            comparison["legs"][role] = {
                "selected_candidate_id": selected_candidate_id,
                "selected_route_source": selected_candidate.get("method"),
                "selected_path": expected,
                "selected_path_sha256": json_hash(expected) if expected else None,
                "executed_path_sha256": json_hash(executed) if executed else None,
                "executed_matches_selected_policy": bool(expected and executed == expected),
                "executed_route_oneway_guard_status": "passed" if failure is None else "failed",
                "executed_route_oneway_failure": failure,
                "model_proposal_segment_audit": segment_audit.get(role),
            }
            executed_roles.add(role)
    comparison["executed_route_oneway_guard_status"] = (
        "passed" if executed_roles == set(captured["legs"])
        and all(row.get("executed_route_oneway_guard_status") == "passed" and row.get("executed_matches_selected_policy") is True for row in comparison["legs"].values())
        else "failed_or_incomplete"
    )
    if comparison["pre_sumo_guard_status"] == "passed" and comparison["executed_route_oneway_guard_status"] == "passed":
        comparison["status"] = "v5_online_selected_route_executed_and_oneway_verified"
    else:
        comparison["status"] = "blocked_v5_selection_or_oneway_route_gate"
    comparison["selection_rule"] = "route_time_money_segment_v5_signed_fare_audit shared-pool ranking; this run executes the 4:6 profile (time 0.4, money 0.6)"
    write_json(run_dir / "same_snapshot_route_comparison.json", comparison)
    return comparison


def audit_energy_traces(r1, run_dir: Path, simulation_dir: Path | None = None) -> dict[str, Any]:
    simulation_dir = simulation_dir or (run_dir / "sumo_trial")
    trace_path = simulation_dir / "sumo_motion_traces_v2.json"
    actual_path = simulation_dir / "route_legs_actual.json"
    if not trace_path.is_file() or not actual_path.is_file():
        report = {
            "status": "not_estimable_no_executed_route_trace",
            "signed_net_kwh": None,
            "reason": "SUMO motion trace or actual route legs missing; do not treat empty distance or absent trace as zero energy.",
        }
        write_json(simulation_dir / "ev2_trace_audit.json", report)
        return report
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    actual = json.loads(actual_path.read_text(encoding="utf-8"))
    rows = []
    for leg in actual:
        if leg.get("order_id") != ORDER_ID:
            continue
        trip_id = leg["trip_id"]
        steps = [r1.MotionStep(**step) for step in trace["trips"].get(trip_id, [])]
        payload = r1.ENERGY_PROFILE.passenger_load_kg if leg.get("leg_role") == "pickup_to_dropoff" else 0.0
        energy = r1.evaluate_motion_trace(
            steps, r1.ENERGY_PROFILE, payload_mass_kg=payload,
            trace_fidelity="observed_sumo_speed_trace_grade_missing",
        )
        observed_wh = leg.get("actual_energy_net_wh")
        recalculated_wh = energy.net_kwh * 1000.0
        rows.append({
            "trip_id": trip_id,
            "leg_role": leg.get("leg_role"),
            "model_version": energy.model_version,
            "profile_id": energy.profile_id,
            "profile_sha256": energy.profile_sha256,
            "trace_samples": len(steps),
            "propulsion_draw_kwh": energy.propulsion_draw_kwh,
            "regenerated_kwh": energy.regenerated_kwh,
            "auxiliary_kwh": energy.auxiliary_kwh,
            "signed_net_kwh": energy.net_kwh,
            "billable_kwh": max(0.0, energy.net_kwh),
            "source_route_ledger_net_wh": observed_wh,
            "module_recalculated_net_wh": recalculated_wh,
            "matches_source_within_1e-8_wh": observed_wh is not None and math.isclose(float(observed_wh), recalculated_wh, rel_tol=0.0, abs_tol=1e-8),
            "missing_grade_steps": energy.missing_grade_steps,
            "trace_fidelity": energy.trace_fidelity,
        })
    report = {
        "status": "passed" if rows and all(row["matches_source_within_1e-8_wh"] for row in rows) else "mismatch_or_incomplete",
        "energy_model_version": "ev_longitudinal_v2",
        "energy_model_module_sha256": sha256_file(ENERGY_MODULE_PATH),
        "profile_sha256": r1.ENERGY_PROFILE.fingerprint(),
        "route_leg_count": len(rows),
        "legs": rows,
        "limitation": "SUMO simulated motion; missing grade treated as flat; not measured production vehicle energy",
    }
    write_json(simulation_dir / "ev2_trace_audit.json", report)
    return report


def audit_v5_gate(run_dir: Path, evaluation: dict[str, Any], comparison: dict[str, Any], profile_replay: dict[str, Any] | None, first_profile_ev2: dict[str, Any], second_profile_ev2: dict[str, Any] | None) -> dict[str, Any]:
    decision_module = load_module("porto_r4_pilot_decision", DECISION_MODULE_PATH)
    profile_execution = {}
    first_profile = V5_PROFILES[0]
    second_profile = V5_PROFILES[1]
    first_passed = (
        comparison.get("status") == "v5_online_selected_route_executed_and_oneway_verified"
        and first_profile_ev2.get("status") == "passed"
    )
    profile_execution[first_profile] = {
        "status": "passed" if first_passed else "failed_or_incomplete",
        "execution_source": "live_model_response_then_v5_selection_before_sumo",
        "candidate_id": evaluation.get("selected_candidate_id_by_profile", {}).get(first_profile),
        "route_comparison_status": comparison.get("status"),
        "oneway_status": comparison.get("executed_route_oneway_guard_status"),
        "ev2_audit_status": first_profile_ev2.get("status"),
        "simulation_dir": str(run_dir / "sumo_trial"),
        "simulation_summary_sha256": sha256_file(run_dir / "sumo_trial/summary.json") if (run_dir / "sumo_trial/summary.json").is_file() else None,
        "ev2_audit_sha256": sha256_file(run_dir / "ev2_trace_audit.json") if (run_dir / "ev2_trace_audit.json").is_file() else None,
        "executed_legs": comparison.get("legs", {}),
    }
    if profile_replay is None:
        second_status = "failed_or_incomplete"
        second_run_dir = None
        second_execution_audit = None
    else:
        second_run_dir = Path(profile_replay["run_dir"])
        second_execution_audit = profile_replay["execution_audit"]
        second_status = (
            "passed" if second_execution_audit.get("status") == "passed"
            and second_profile_ev2 is not None and second_profile_ev2.get("status") == "passed"
            else "failed_or_incomplete"
        )
    profile_execution[second_profile] = {
        "status": second_status,
        "execution_source": "same_candidate_reused_without_duplicate_sumo" if profile_replay and profile_replay.get("reused_same_route") else "deterministic_sumo_replay_of_v5_selected_route",
        "candidate_id": evaluation.get("selected_candidate_id_by_profile", {}).get(second_profile),
        "oneway_status": "passed" if second_execution_audit and second_status == "passed" else "failed_or_incomplete",
        "simulation_dir": str(second_run_dir) if second_run_dir else None,
        "simulation_summary_sha256": (
            second_execution_audit.get("summary_sha256") if second_execution_audit
            else (sha256_file(run_dir / "sumo_trial/summary.json") if profile_replay and profile_replay.get("reused_same_route") else None)
        ),
        "ev2_audit_status": second_profile_ev2.get("status") if second_profile_ev2 else None,
        "ev2_audit_sha256": (
            sha256_file(second_run_dir / "ev2_trace_audit.json")
            if second_run_dir and (second_run_dir / "ev2_trace_audit.json").is_file()
            else (sha256_file(run_dir / "ev2_trace_audit.json") if profile_replay and profile_replay.get("reused_same_route") and (run_dir / "ev2_trace_audit.json").is_file() else None)
        ),
        "execution_audit": second_execution_audit,
    }
    both_profiles_passed = (
        evaluation.get("status") == "selected_both_profiles"
        and all(row.get("status") == "passed" for row in profile_execution.values())
    )
    report = {
        **evaluation,
        "status": "passed_both_profiles_full_flow_no_oneway_violation" if both_profiles_passed else "blocked_or_failed_full_flow_gate",
        "decision_model_version": decision_module.DECISION_MODEL_VERSION,
        "applied_to_online_route_choice": bool(comparison.get("selected_candidate_id")),
        "profile_execution": profile_execution,
        "oneway_full_flow_gate_passed": both_profiles_passed,
        "source_sha256": sha256_file(DECISION_MODULE_PATH),
        "reason": None if both_profiles_passed else "Both profile rankings and SUMO route replays must pass complete candidate pricing, per-Link directed validation, exact selected-path matching, and EV2 trace reconciliation.",
    }
    write_json(run_dir / "decision_v5_candidate_gate.json", report)
    return report


def main() -> int:
    global ORDER_ID
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--order-id", default=ORDER_ID)
    args = parser.parse_args()
    ORDER_ID = str(args.order_id)
    run_dir = args.run_dir if args.run_dir.is_absolute() else (ROOT / args.run_dir)
    run_dir = run_dir.resolve()
    if args.mode == "preflight":
        report = build_static_preflight(run_dir)
        print(json.dumps({"run_dir": str(run_dir), "status": report["run_status"], "model_calls": 0, "estimated_input_tokens": report["request_gate"]["estimated_input_tokens_including_schema_and_cli_overhead"]}, ensure_ascii=False))
        return 0 if report["run_status"] == "completed_zero_model_calls" else 2
    result = run_pilot(run_dir)
    print(json.dumps({
        "run_dir": str(run_dir),
        "status": result["manifest"]["status"],
        "model_calls": result["manifest"].get("model_calls"),
        "orders_completed": result["summary"].get("orders_completed"),
        "route_comparison": result["route_comparison"].get("status"),
        "ev2": result["ev2"].get("status"),
        "decision_v5": result["decision_v5"].get("status"),
    }, ensure_ascii=False))
    return 0 if result["manifest"]["status"] == "pilot_route_complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
