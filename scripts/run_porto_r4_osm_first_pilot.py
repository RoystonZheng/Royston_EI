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
    prompt = instructions + "\n" + json.dumps(task, ensure_ascii=False, separators=(",", ":"))
    return task, prompt


def provenance(runner, runtime, source_preflight: dict[str, Any], source_manifest: dict[str, Any]) -> dict[str, Any]:
    r1 = runtime[0]
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
        "decision_module_sha256": sha256_file(DECISION_MODULE_PATH),
        "costs_module_sha256": sha256_file(COSTS_MODULE_PATH),
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
    shared_graph = runner.compact_candidate_graph_for_prompt(raw_graph)
    context = NetworkContext.load(r1, SEED)
    orders, _route_lookup = runner.orders_and_route_pairs(context)
    order = next(row for row in orders if row["order_id"] == ORDER_ID)
    vehicle_id = order["vehicle_id"]
    initial = {row["vehicle_id"]: row["initial_position"]["point_id"] for row in context.orders_file["vehicles"]}
    from_point_id = initial[vehicle_id]
    endpoint_ids = [from_point_id, order["pickup"]["point_id"], order["dropoff"]["point_id"]]
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
    schema = build_schema(base_schema, legs)
    token_estimate = int(estimate(prompt, schema))
    fallback_response = runner.candidate_fallback_response(shared_graph, legs, endpoints)
    if fallback_response is None:
        raise RuntimeError("shared_candidate_fallback_not_covered_by_task_graph")
    fallback_audit, fallback_routes = validate(r1, fallback_response, legs, OBJECTIVE, 0.0, traffic, tls_snapshot, endpoint_anchored=True)
    cli_info = codex_preflight()
    report = {
        "run_id": run_dir.name,
        "experiment_family": "R4_real_OSM_route_layer",
        "map_type": "real OSM-derived Porto SUMO network",
        "layer": "route planning; fixed vehicle/order assignment, no dispatch optimization",
        "pilot_scope": {"seed": SEED, "order_id": ORDER_ID, "vehicle_id": vehicle_id, "objective": OBJECTIVE, "model_requests": 1, "route_legs": ["vehicle_to_pickup", "pickup_to_dropoff"]},
        "run_status": "completed_zero_model_calls" if token_estimate <= PROVIDER_INPUT_GATE and cli_info.get("available") else "blocked_preflight",
        "actual_model_call_count": 0,
        "source_provenance": provenance(runner, runtime, source_preflight, source_manifest),
        "shared_graph_scope": {
            "full_graph_available_in_run_artifacts": True,
            "candidate_count": source_preflight["shared_candidate_link_graph"]["candidate_count"],
            "ordered_pair_count": source_preflight["shared_candidate_link_graph"]["ordered_pair_count"],
            "full_graph_sha256": source_preflight["shared_candidate_link_graph"]["sha256"],
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
        "codex_cli_preflight": cli_info,
        "road_turn_information": {
            "all_links_in_each_leg_crop_retained": True,
            "all_induced_legal_directed_turns_retained": True,
            "current_snapshot_signal_and_traffic_fields_retained": True,
            "removed_from_prompt": "unrelated shared candidate-pair routes; complete source graph retained by SHA in artifacts",
        },
        "decision_function": {
            "online_route_objective": "FASTEST; keep model route only when legal and strictly faster than shared Dijkstra on the same snapshot",
            "time_money_v5_version": "route_time_money_segment_v5_signed_fare_audit",
            "time_money_v5_status": "not_used_for_route_choice; economic ranking remains blocked when candidate money inputs or passenger fare are unknown",
        },
        "energy_module": {
            "version": "ev_longitudinal_v2",
            "profile_id": "generic_reference_ev_v1",
            "profile_sha256": r1.ENERGY_PROFILE.fingerprint(),
            "source_sha256": sha256_file(ENERGY_MODULE_PATH),
            "application": "post-run, from observed SUMO speed traces; signed net energy, with regen and auxiliary load",
        },
        "artifacts": {},
    }
    preflight_dir = run_dir / "preflight"
    requests_dir = preflight_dir / "requests"
    requests_dir.mkdir(parents=True)
    (requests_dir / "request_0001.prompt.txt").write_text(prompt, encoding="utf-8")
    write_json(requests_dir / "request_0001.schema.json", schema)
    write_json(requests_dir / "request_0001.task.json", task)
    shutil.copy2(SHARED_GRAPH_PATH, preflight_dir / "shared_candidate_link_graph.json")
    report["artifacts"] = {
        "prompt_sha256": sha256_file(requests_dir / "request_0001.prompt.txt"),
        "schema_sha256": sha256_file(requests_dir / "request_0001.schema.json"),
        "task_sha256": sha256_file(requests_dir / "request_0001.task.json"),
        "full_shared_graph_copy_sha256": sha256_file(preflight_dir / "shared_candidate_link_graph.json"),
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

    runner, runtime = load_runtime()
    r1, NetworkContext, RouteProblemBuilder, score_path, validate, _verify, estimate, codex_preflight, call_once, build_schema, serialize_schema, default_output_reserve = runtime
    source_preflight, source_manifest, frozen = prepare_runtime_verification(run_dir, runner, runtime, preflight)
    shared_raw = json.loads(SHARED_GRAPH_PATH.read_text(encoding="utf-8"))
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

    def score_path_on_model_arrival(r1_arg, leg, path, objective, now, traffic, tls_snapshot, start_offset_s=0.0):
        if leg.role == "pickup_to_dropoff" and captured.get("model_vehicle_to_pickup_time_s") is not None:
            start_offset_s = float(captured["model_vehicle_to_pickup_time_s"])
        return original_score_path(r1_arg, leg, path, objective, now, traffic, tls_snapshot, start_offset_s=start_offset_s)

    original_validator = validate

    def validate_and_capture_model_arrival(*args, **kwargs):
        audit, routes = original_validator(*args, **kwargs)
        vp = routes.get("vehicle_to_pickup")
        if vp is not None:
            captured["model_vehicle_to_pickup_time_s"] = float(vp["predicted_travel_time_s"])
        return audit, routes

    def build_order_request_with_scoped_h(self, order, from_point_id, objective, now, traffic, tls_snapshot, request_key, **kwargs):
        legs, task, prompt = original_build(
            self, order, from_point_id, objective, now, traffic, tls_snapshot, request_key, **kwargs,
        )
        task, prompt = compact_request(runner, runtime, legs, task, prompt, shared_graph, endpoint_ids)
        schema_path = runner.PACKAGE / runner.SCHEMA_RELATIVE
        schema = build_schema(json.loads(schema_path.read_text(encoding="utf-8")), legs)
        estimated = int(estimate(prompt, schema))
        route_endpoints = {
            "vehicle_to_pickup": (from_point_id, order["pickup"]["point_id"]),
            "pickup_to_dropoff": (order["pickup"]["point_id"], order["dropoff"]["point_id"]),
        }
        fallback_response = runner.candidate_fallback_response(shared_graph, legs, route_endpoints)
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
        if captured:
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
        "build_response_schema": build_schema,
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
        comparison = write_route_comparison(runner, runtime, run_dir, captured, shared_graph, endpoints, summary)
        ev2 = audit_energy_traces(r1, run_dir)
        decision = audit_v5_gate(run_dir, comparison)
        terminal = summary.get("terminal_order_count") == 1 and summary.get("orders_completed") == 1
        model_calls = int(model_budget.get("actual_model_calls", 0))
        if model_calls > 1:
            raise RuntimeError("pilot_call_scope_exceeded")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update({
            "status": "pilot_route_complete" if terminal else ("pilot_model_called_route_not_completed" if model_calls else "pilot_blocked_before_model_call"),
            "model_calls": model_calls,
            "model_request_scope": "one seed-0 FASTEST order ord_0001, with pickup and passenger legs in one request",
            "codex_usage_tokens_observed": int(model_budget["usage_so_far"].get("tokens", 0)),
            "codex_standard_credits_estimate": round(float(model_budget["usage_so_far"].get("credits", 0.0)), 8),
            "codex_account_billing": "not reported by Codex CLI",
            "route_comparison_status": comparison.get("status"),
            "ev2_audit_status": ev2.get("status"),
            "time_money_v5_status": decision.get("status"),
            "sumo_trial_summary_sha256": sha256_file(run_dir / "sumo_trial/summary.json"),
            "runtime_request_preflight_sha256": sha256_file(run_dir / "preflight/runtime_request.json") if (run_dir / "preflight/runtime_request.json").exists() else None,
        })
        write_json(manifest_path, manifest)
        return {"manifest": manifest, "summary": summary, "route_comparison": comparison, "ev2": ev2, "decision_v5": decision}
    finally:
        RouteProblemBuilder.build_order_request = original_build
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


def write_route_comparison(runner, runtime, run_dir, captured, shared_graph, endpoints, summary):
    if not captured:
        report = {"status": "blocked_no_live_request_capture", "model_call_or_route_claim": False}
        write_json(run_dir / "same_snapshot_route_comparison.json", report)
        return report
    r1, _ctx, _builder_type, score_path, validate = runtime[:5]
    response_path = run_dir / "codex_requests/call_0000.final.json"
    if not response_path.is_file():
        report = {"status": "model_response_missing_or_not_delivered", "model_call_or_route_claim": False}
        write_json(run_dir / "same_snapshot_route_comparison.json", report)
        return report
    response = json.loads(response_path.read_text(encoding="utf-8"))
    model_routes: dict[str, Any] = {}
    validation_error = None
    try:
        _audit, model_routes = validate(
            r1, response, captured["legs"], OBJECTIVE, captured["now"],
            captured["traffic"], captured["tls_snapshot"], endpoint_anchored=True,
        )
    except (ValueError, TypeError, KeyError) as error:
        validation_error = f"{type(error).__name__}:{error}"
    comparison = {"status": "completed", "objective": OBJECTIVE, "snapshot_sha256": json_hash({"traffic": captured["traffic"], "tls": captured["tls_snapshot"], "sim_time_s": captured["now"]}), "model_validation_error": validation_error, "legs": {}}
    model_vp_time = (
        float(model_routes["vehicle_to_pickup"]["predicted_travel_time_s"])
        if validation_error is None and "vehicle_to_pickup" in model_routes
        else None
    )
    baseline_vp_time = None
    for role, leg in captured["legs"].items():
        start_id, end_id = captured["route_endpoints"][role]
        baseline = runner.candidate_fallback_path(shared_graph, start_id, end_id)
        if not baseline:
            comparison["legs"][role] = {"status": "blocked_shared_fallback_missing"}
            continue
        start_offset = 0.0
        if role == "pickup_to_dropoff":
            start_offset = model_vp_time if model_vp_time is not None else float(baseline_vp_time or 0.0)
        baseline_score = score_path(r1, leg, baseline, OBJECTIVE, captured["now"], captured["traffic"], captured["tls_snapshot"], start_offset_s=start_offset)
        model_score = None
        if validation_error is None and role in model_routes:
            model_score = model_routes[role]
        selected_source = "shared_link_fallback"
        if model_score is not None and float(model_score["predicted_travel_time_s"]) < float(baseline_score["predicted_travel_time_s"]) - 1e-6:
            selected_source = "model"
        chosen = model_score if selected_source == "model" else baseline_score
        comparison["legs"][role] = {
            "status": "compared_same_snapshot",
            "model_route_valid": model_score is not None,
            "model_route": None if model_score is None else {"link_count": len(model_score["path"]), "path_sha256": json_hash(model_score["path"]), "predicted_travel_time_s": round(float(model_score["predicted_travel_time_s"]), 6), "predicted_ev2_signed_net_wh": round(float(model_score["predicted_energy_wh"]), 6)},
            "shared_dijkstra_route": {"link_count": len(baseline), "path_sha256": json_hash(baseline), "predicted_travel_time_s": round(float(baseline_score["predicted_travel_time_s"]), 6), "predicted_ev2_signed_net_wh": round(float(baseline_score["predicted_energy_wh"]), 6)},
            "selected_route_source": selected_source,
            "selection_rule": "legal model route only when strictly faster on the same frozen dynamic snapshot; otherwise shared Dijkstra fallback",
        }
        if role == "vehicle_to_pickup":
            baseline_vp_time = float(baseline_score["predicted_travel_time_s"])
    executed_rows_path = run_dir / "sumo_trial/route_legs_actual.json"
    if executed_rows_path.exists():
        executed_rows = json.loads(executed_rows_path.read_text(encoding="utf-8"))
        by_role = {row.get("leg_role"): row for row in executed_rows if row.get("order_id") == ORDER_ID}
        for role, row in by_role.items():
            if role in comparison["legs"]:
                comparison["legs"][role]["executed_path_sha256"] = json_hash(row.get("route_edges", []))
                comparison["legs"][role]["executed_matches_selected_policy"] = row.get("route_edges") == (
                    model_routes[role]["path"] if comparison["legs"][role]["selected_route_source"] == "model" and model_routes else runner.candidate_fallback_path(shared_graph, *captured["route_endpoints"][role])
                )
    write_json(run_dir / "same_snapshot_route_comparison.json", comparison)
    return comparison


def audit_energy_traces(r1, run_dir: Path) -> dict[str, Any]:
    trace_path = run_dir / "sumo_trial/sumo_motion_traces_v2.json"
    actual_path = run_dir / "sumo_trial/route_legs_actual.json"
    if not trace_path.is_file() or not actual_path.is_file():
        report = {"status": "not_observed", "reason": "SUMO motion trace or actual route legs missing"}
        write_json(run_dir / "ev2_trace_audit.json", report)
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
    write_json(run_dir / "ev2_trace_audit.json", report)
    return report


def audit_v5_gate(run_dir: Path, comparison: dict[str, Any]) -> dict[str, Any]:
    decision_module = load_module("porto_r4_pilot_decision", DECISION_MODULE_PATH)
    results = {}
    for role, leg in comparison.get("legs", {}).items():
        candidates = []
        model = leg.get("model_route")
        baseline = leg.get("shared_dijkstra_route")
        if model:
            candidates.append({"candidate_id": "model", "feasible": bool(leg.get("model_route_valid")), "travel_time_sec": model.get("predicted_travel_time_s"), "money_cost": None})
        if baseline:
            candidates.append({"candidate_id": "shared_dijkstra", "feasible": True, "travel_time_sec": baseline.get("predicted_travel_time_s"), "money_cost": None})
        results[role] = decision_module.score_route_candidates(candidates, "time_priority_0_6_0_4", currency="EUR")
    status = "blocked_incomplete_candidate_data" if any(row.get("status") == "blocked_incomplete_candidate_data" for row in results.values()) else "not_applicable"
    report = {
        "status": status,
        "decision_model_version": decision_module.DECISION_MODEL_VERSION,
        "applied_to_online_route_choice": False,
        "per_leg_candidate_checks": results,
        "reason": "This pilot is the frozen FASTEST route objective. The time-money v5 evaluator requires known monetary costs; passenger-fare and complete price inputs are not supplied, so it correctly emits no economic ranking.",
        "source_sha256": sha256_file(DECISION_MODULE_PATH),
    }
    write_json(run_dir / "decision_v5_candidate_gate.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
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
