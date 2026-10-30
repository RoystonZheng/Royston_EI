#!/usr/bin/env python3
"""Prepare or run one bounded Codex CLI route pilot on the frozen Porto graph.

The pilot makes at most one model invocation, never retries, uses anonymized
edge/signal aliases in the prompt, validates locally, and runs one fixed order
in a fresh SUMO copy if both proposed legs pass the hard validator.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = PACKAGE / "scripts"
SUMO_HOME = Path("/Users/didi/Library/Application Support/Porto SUMO/1.28.0/Versions/1.28.0/EclipseSUMO")
os.environ["SUMO_HOME"] = str(SUMO_HOME / "share/sumo")
sys.path.insert(0, str(SUMO_HOME / "share/sumo/tools"))
os.environ["PORTO_SUMO_BIN"] = str(SUMO_HOME / "bin/sumo")
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS))

import traci  # noqa: E402
import run_porto_r1_dynamic_sumo_v3 as r1  # noqa: E402
from route_experiment.codex_cli_adapter import CodexCliRouteStepClient  # noqa: E402


OUT = PACKAGE / "derived/experiments/r2_codex_cli_v1/pilot_seed0_order0001_fastest_v3"
FROZEN = PACKAGE / "derived/scenarios/v3_3/frozen_inputs_v1_3"
NETWORK = PACKAGE / "derived/sumo/v1.28.0/porto.net.xml"
SCHEMA_PATH = PACKAGE / "derived/experiments/protocols/porto_r2_codex_route_response_v1.json"
CORE_EDGE_CSV = FROZEN / "routeable_core_edges.csv"
SCENARIO_MANIFEST = FROZEN / "manifest_v3_3.json"
BACKGROUND_DEMAND = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/formal_v3_fixed_delay/seed_0/FASTEST/background_demand.json"
MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "low"
MAX_ROUGH_INPUT_TOKENS = 100_000
MAX_ROUTE_ITEMS_PER_LEG = 512
PILOT_MAX_SIM_TIME_S = 7_200
DT = r1.DT


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_path(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def install_frozen_core_scope():
    """Use the already-frozen core edge file instead of requiring pyproj here."""
    manifest = json.loads(SCENARIO_MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["core_edge_file"]
    if digest_path(CORE_EDGE_CSV) != expected["sha256"]:
        raise RuntimeError("frozen_core_edge_file_hash_mismatch")
    with CORE_EDGE_CSV.open(encoding="utf-8-sig", newline="") as stream:
        edge_ids = {row["edge_id"] for row in csv.DictReader(stream)}
    if len(edge_ids) != int(expected["rows"]):
        raise RuntimeError("frozen_core_edge_row_count_mismatch")
    r1.pf.edge_is_inside_fence = lambda _net, edge: edge.getID() in edge_ids
    return edge_ids


def score_route(router, allowed, path, start_match, end_match, objective, now, traffic, tls):
    if not path:
        raise ValueError("empty_route")
    source = start_match["edge_id"]
    target = end_match["edge_id"]
    if path[0] != source or path[-1] != target:
        raise ValueError("endpoint_link_mismatch")
    if len(path) == 1:
        distance = float(end_match["edge_pos_m"]) - float(start_match["edge_pos_m"])
        if distance < -1e-6:
            raise ValueError("reverse_same_link_requires_loop")
        travel, energy = router._edge_time_and_energy(path[0], max(0.0, distance), now, now, traffic)
        return {"status": "ok", "path": path, "predicted_travel_time_s": travel,
                "predicted_energy_wh": energy, "signal_wait_s": 0.0,
                "distance_m": max(0.0, distance)}

    elapsed = 0.0
    signal_wait = 0.0
    energy_total = 0.0
    distance_total = 0.0
    for index, edge_id in enumerate(path):
        edge = allowed[edge_id]
        if index == 0:
            distance = max(0.0, float(edge.getLength()) - float(start_match["edge_pos_m"]))
        elif index == len(path) - 1:
            distance = max(0.0, float(end_match["edge_pos_m"]))
        else:
            distance = float(edge.getLength())
        if index:
            wait = router._turn_wait(path[index - 1], edge_id, now + elapsed, now, tls)
            signal_wait += wait
            energy_total += r1.EV_PARAMS["constant_power_intake_w"] * wait / 3600.0
            elapsed += wait
        travel, energy = router._edge_time_and_energy(
            edge_id, distance, now + elapsed, now, traffic
        )
        elapsed += travel
        energy_total += energy
        distance_total += distance
    return {"status": "ok", "path": path, "predicted_travel_time_s": elapsed,
            "predicted_energy_wh": energy_total, "signal_wait_s": signal_wait,
            "distance_m": distance_total}


def build_request(runner):
    orders = runner.orders_file["orders"]
    vehicles = {v["vehicle_id"]: v for v in runner.orders_file["vehicles"]}
    order = min(orders, key=lambda x: (x["sequence_index"], x["vehicle_id"]))
    vehicle = vehicles[order["vehicle_id"]]
    start_point = vehicle["initial_position"]["point_id"]
    start_match = runner.match[start_point][0]
    pickup_match = runner.match[order["pickup"]["point_id"]][0]
    dropoff_match = runner.match[order["dropoff"]["point_id"]][0]

    raw_edges = sorted(runner.core)
    alias = {edge_id: f"e{index:04d}" for index, edge_id in enumerate(raw_edges)}
    signal_ids = sorted({
        signal_id
        for values in runner.tls_by_turn.values()
        for signal_id, _ in values
        if signal_id in runner.programs
    })
    signal_alias = {signal_id: f"s{index:04d}" for index, signal_id in enumerate(signal_ids)}

    traffic = runner._traffic_snapshot()
    tls_snapshot = runner.router._tls_snapshot()
    sim_now = float(traci.simulation.getTime())

    links = []
    way_to_aliases = {}
    for edge_id in raw_edges:
        edge = runner.allowed[edge_id]
        length = float(edge.getLength())
        speed = max(0.1, float(edge.getSpeed()))
        time_s, energy_wh = runner.router._edge_time_and_energy(
            edge_id, length, sim_now, sim_now, traffic
        )
        way_id = r1.pf.osm_way_id(edge_id)
        way_to_aliases.setdefault(way_id, []).append(alias[edge_id])
        links.append([alias[edge_id], round(length, 2), round(speed, 3), round(time_s, 3), round(energy_wh, 4)])

    turns = []
    for source, target in sorted(runner.graph.edges):
        signal_refs = [
            [signal_alias[sig], int(link_index)]
            for sig, link_index in runner.tls_by_turn.get((source, target), [])
            if sig in signal_alias
        ]
        turns.append([alias[source], alias[target], signal_refs])

    signals = []
    for signal_id in signal_ids:
        phases = runner.programs.get(signal_id, [])
        live = tls_snapshot.get(signal_id)
        if not live or not phases:
            continue
        signals.append([
            signal_alias[signal_id], int(live["phase"]),
            round(max(0.0, float(live["next_switch_s"]) - sim_now), 3),
            [[round(float(phase["duration"]), 3),
              [i for i, state in enumerate(phase["state"]) if state in "gG"]]
             for phase in phases],
        ])

    forbidden_rules = []
    for rule in r1.FORBIDDEN_WAY_SEQUENCES:
        grouped = [way_to_aliases.get(way, []) for way in rule]
        if all(grouped):
            forbidden_rules.append(grouped)

    def endpoint(match):
        edge_id = match["edge_id"]
        return {"edge_id": alias[edge_id], "position_m_from_link_start": round(float(match["edge_pos_m"]), 3)}

    payload = {
        "task": {
            "scenario": runner.orders_file["scenario_id"],
            "seed": 0,
            "order_id": order["order_id"],
            "vehicle_id": order["vehicle_id"],
            "objective": "FASTEST",
            "request_start_sim_time_s": round(sim_now, 3),
            "max_route_links": MAX_ROUTE_ITEMS_PER_LEG,
        },
        "legs": [
            {"leg_role": "vehicle_to_pickup", "from": endpoint(start_match), "to": endpoint(pickup_match)},
            {"leg_role": "pickup_to_dropoff", "from": endpoint(pickup_match), "to": endpoint(dropoff_match)},
        ],
        "network": {
            "directed_links": links,
            "legal_turns": turns,
            "signal_programs": signals,
            "forbidden_link_group_sequences": forbidden_rules,
        },
        "traffic_snapshot": {
            "sim_time_s": round(sim_now, 3),
            "occupied_link_count": len(traffic["edge_state"]),
            "active_vehicle_count": traffic["active_vehicle_count"],
            "halting_vehicle_count": traffic["halting_vehicle_count"],
            "occupied_links": [
                [alias[edge_id], item["vehicle_count"], round(item["mean_speed_mps"], 3), item["halting_count"]]
                for edge_id, item in sorted(traffic["edge_state"].items()) if edge_id in alias
            ],
        },
    }
    return payload, alias, signal_alias, order, vehicle, start_match, pickup_match, dropoff_match, traffic, tls_snapshot


def make_prompt(payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        "You are the route planner in a fixed-order Porto road-network experiment.\n"
        "Return the two route link sequences required by the JSON schema, in the same leg order. "
        "Use only the supplied directed_links and legal_turns. Never invent links or transitions. "
        "Each path must start and end on the specified directed links. Stay within the supplied graph. "
        "Respect forbidden_link_group_sequences: a path may not traverse one alias from each successive group in a listed rule.\n"
        "Choose FASTEST using predicted link travel time and signal waiting. Link rows are "
        "[alias,length_m,speed_limit_mps,predicted_full_link_time_s,predicted_full_link_energy_wh]. "
        "Turn rows are [from_alias,to_alias,signal_refs], where each signal ref is [signal_alias,link_index]. "
        "Signal rows are [signal_alias,current_phase_index,seconds_until_next_phase,phases]; each phase is "
        "[duration_s,green_link_indices]. Estimate arrival time cumulatively along each proposed path and wait "
        "until a listed movement is green before entering the next link. If a turn has several signal refs, use "
        "the earliest allowed movement. Current traffic snapshot is shared with the traditional baseline. For a "
        "partial first or last link, scale the full-link travel time by the fraction of length traversed.\n"
        "The environment keeps advancing while route planning runs. This request is one diagnostic order only; "
        "do not dispatch vehicles, change the order sequence, or use tools. Return only schema-conforming JSON.\n\n"
        "INPUT_JSON:\n" + data
    )


def make_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def prepare():
    if OUT.exists():
        if (OUT / "preflight.json").is_file():
            return json.loads((OUT / "preflight.json").read_text(encoding="utf-8"))
        raise SystemExit(f"refusing to reuse incomplete output directory: {OUT}")
    OUT.mkdir(parents=True)
    frozen_core = install_frozen_core_scope()
    init_dir = OUT / "state_snapshot"
    trace_hash = digest_bytes(b"porto_r2_pilot_initial_snapshot_only")
    runner = r1.SimulationRun(0, "FASTEST", init_dir, 90, 2.0, {}, trace_hash)
    try:
        background = json.loads(BACKGROUND_DEMAND.read_text(encoding="utf-8"))["vehicles"]
        runner.background_vehicle_count = len(background)
        if runner.core != frozen_core:
            raise RuntimeError("sumo_graph_does_not_match_frozen_core_edge_file")
        runner._start_sumo(background)
        payload, alias, signal_alias, order, vehicle, start_match, pickup_match, dropoff_match, traffic, tls = build_request(runner)
    finally:
        try:
            traci.close()
        except Exception:
            pass

    prompt = make_prompt(payload)
    prompt_bytes = prompt.encode("utf-8")
    estimated_tokens = math.ceil(len(prompt_bytes) / 3.0)
    if estimated_tokens > MAX_ROUGH_INPUT_TOKENS:
        status = "blocked_input_budget_preflight"
        reason = "rough character-based estimate exceeds frozen 100000-token ceiling; no model call made"
    else:
        status = "ready_zero_model_calls"
        reason = "one-call diagnostic cap; no model call has been made during preflight"
    frozen_path = FROZEN / "orders_seed_0_frozen.json"
    binding_path = FROZEN / "shared_endpoint_bindings_seed_0.json"
    manifest = {
        "pilot_id": "porto_r2_codex_cli_seed0_order0001_fastest_v1",
        "status": status,
        "reason": reason,
        "formal_matrix": False,
        "model_call_cap": 1,
        "retry_cap": 0,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "codex_cli_version": CodexCliRouteStepClient(model=MODEL, trace_dir=OUT / "codex_trace", reasoning_effort=REASONING_EFFORT).cli_version,
        "max_rough_input_tokens": MAX_ROUGH_INPUT_TOKENS,
        "rough_input_token_estimate_characters_div_3": estimated_tokens,
        "prompt_bytes": len(prompt_bytes),
        "prompt_sha256": digest_bytes(prompt_bytes),
        "request_payload_sha256": digest_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
        "schema_sha256": digest_path(SCHEMA_PATH),
        "network_sha256": digest_path(NETWORK),
        "frozen_core_edge_file_sha256": digest_path(CORE_EDGE_CSV),
        "background_demand_sha256": digest_path(BACKGROUND_DEMAND),
        "frozen_orders_sha256": digest_path(frozen_path),
        "endpoint_bindings_sha256": digest_path(binding_path),
        "pilot_runner_sha256": digest_path(Path(__file__).resolve()),
        "r1_runner_sha256": digest_path(Path(r1.__file__).resolve()),
        "codex_adapter_sha256": digest_path(REPO_ROOT / "route_experiment/codex_cli_adapter.py"),
        "core_directed_link_count": len(alias),
        "legal_turn_count": len(payload["network"]["legal_turns"]),
        "signal_program_count": len(payload["network"]["signal_programs"]),
        "objective": "FASTEST",
        "seed": 0,
        "order_id": order["order_id"],
        "vehicle_id": vehicle["vehicle_id"],
        "experimental_vehicle_count_in_pilot": 1,
        "fixed_fleet_size_in_source_scenario": 10,
        "traffic_rate_per_hour_synthetic": 90,
        "traffic_duration_hours_synthetic": 2,
        "traffic_snapshot": {
            "sim_time_s": payload["traffic_snapshot"]["sim_time_s"],
            "active_vehicle_count": traffic["active_vehicle_count"],
            "occupied_link_count": len(traffic["edge_state"]),
            "signal_controller_count": len(tls),
        },
        "ids_sent_to_model": "deterministic per-request aliases; source OSM/SUMO identifiers are not in the prompt or route output",
        "cost_reporting": "Codex CLI JSONL usage and standard-rate equivalent only; account billing is not reported by the CLI",
        "limitation": "one-order diagnostic only; 90 veh/h synthetic background and SUMO signals; not a formal R2 matrix or real Porto traffic result",
    }
    (OUT / "request_prompt.txt").write_bytes(prompt_bytes)
    write_json(OUT / "request_payload.json", payload)
    write_json(OUT / "preflight.json", manifest)
    return manifest


def validate_routes(response, alias_to_raw, runner, order, vehicle, start_match, pickup_match, dropoff_match, traffic, tls):
    if not isinstance(response, dict) or not isinstance(response.get("routes"), list) or len(response["routes"]) != 2:
        return {"status": "failed", "reason": "schema_shape_invalid"}, None
    expected = ["vehicle_to_pickup", "pickup_to_dropoff"]
    by_role = {row.get("leg_role"): row for row in response["routes"] if isinstance(row, dict)}
    if len(by_role) != 2 or any(role not in by_role for role in expected):
        return {"status": "failed", "reason": "route_roles_invalid"}, None
    matches = {
        "vehicle_to_pickup": (start_match, pickup_match),
        "pickup_to_dropoff": (pickup_match, dropoff_match),
    }
    raw_paths = {}
    scored = {}
    for role in expected:
        aliases = by_role[role].get("edge_ids")
        if not isinstance(aliases, list) or not aliases or len(aliases) > MAX_ROUTE_ITEMS_PER_LEG:
            return {"status": "failed", "reason": f"route_length_invalid:{role}"}, None
        if any(item not in alias_to_raw for item in aliases):
            return {"status": "failed", "reason": f"unknown_link_alias:{role}"}, None
        path = [alias_to_raw[item] for item in aliases]
        start, end = matches[role]
        if path[0] != start["edge_id"] or path[-1] != end["edge_id"]:
            return {"status": "failed", "reason": f"endpoint_link_mismatch:{role}"}, None
        if any(not runner.graph.has_edge(a, b) for a, b in zip(path, path[1:])):
            return {"status": "failed", "reason": f"illegal_turn_or_disconnected:{role}"}, None
        if not set(path).issubset(runner.core) or not r1.pf.is_legal_sequence(path):
            return {"status": "failed", "reason": f"hard_route_validator_rejected:{role}"}, None
        raw_paths[role] = path
    first = score_route(runner.router, runner.allowed, raw_paths[expected[0]], start_match, pickup_match, "FASTEST", 0.0, traffic, tls)
    second = score_route(runner.router, runner.allowed, raw_paths[expected[1]], pickup_match, dropoff_match, "FASTEST", first["predicted_travel_time_s"], traffic, tls)
    scored[expected[0]] = first
    scored[expected[1]] = second
    validation = {
        "status": "passed",
        "local_hard_validator": "SUMO passenger graph membership + legal edge-to-edge connection + forbidden OSM way-sequence validator",
        "route_lengths": {role: len(raw_paths[role]) for role in expected},
        "predicted_costs": {
            role: {"travel_time_s": scored[role]["predicted_travel_time_s"], "signal_wait_s": scored[role]["signal_wait_s"], "distance_m": scored[role]["distance_m"]}
            for role in expected
        },
    }
    return validation, (raw_paths, scored)


def run_pilot():
    preflight_path = OUT / "preflight.json"
    if not preflight_path.is_file():
        raise SystemExit("zero-call preflight is missing; run preparation first")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if preflight["status"] != "ready_zero_model_calls":
        raise SystemExit("preflight did not pass; no model call permitted")
    marker = OUT / "model_call_started.json"
    try:
        with marker.open("x", encoding="utf-8") as f:
            json.dump({"call_cap": 1, "retry_cap": 0, "started_utc_epoch": time.time()}, f)
    except FileExistsError:
        raise SystemExit("model call marker exists; retry prohibited")

    prompt = (OUT / "request_prompt.txt").read_text(encoding="utf-8")
    schema = make_schema()
    client = CodexCliRouteStepClient(
        model=MODEL,
        trace_dir=OUT / "codex_trace",
        reasoning_effort=REASONING_EFFORT,
        timeout_sec=300,
        route_constraint=SimpleNamespace(schema=schema),
        trace_stem="r2_route",
    )
    try:
        response = client.chat([{"role": "user", "content": prompt}])
    except Exception as exc:
        write_json(OUT / "model_call_failure.json", {
            "status": "failed_no_retry",
            "error_type": type(exc).__name__,
            "error_code": str(exc),
            "provider_audit": client.last_record,
        })
        raise

    response_path = OUT / "model_response.json"
    response_payload = json.loads(response.content)
    write_json(response_path, response_payload)
    frozen_core = install_frozen_core_scope()
    # The validator runner is local-only; it uses the frozen network and bindings and emits no raw IDs.
    validator_runner = r1.SimulationRun(0, "FASTEST", OUT / "validator_snapshot", 90, 2.0, {}, "validator_only")
    if validator_runner.core != frozen_core:
        raise RuntimeError("sumo_graph_does_not_match_frozen_core_edge_file")
    alias_to_raw = {f"e{index:04d}": edge_id for index, edge_id in enumerate(sorted(validator_runner.core))}
    order = min(validator_runner.orders_file["orders"], key=lambda x: (x["sequence_index"], x["vehicle_id"]))
    vehicle = {v["vehicle_id"]: v for v in validator_runner.orders_file["vehicles"]}[order["vehicle_id"]]
    start_match = validator_runner.match[vehicle["initial_position"]["point_id"]][0]
    pickup_match = validator_runner.match[order["pickup"]["point_id"]][0]
    dropoff_match = validator_runner.match[order["dropoff"]["point_id"]][0]
    # Recreate the frozen t=0 signal/traffic snapshot in a new SUMO instance for hard validation and replay.
    background = json.loads(BACKGROUND_DEMAND.read_text(encoding="utf-8"))["vehicles"]
    validator_runner.background_vehicle_count = len(background)
    validator_runner._start_sumo(background)
    try:
        traffic = validator_runner._traffic_snapshot()
        tls = validator_runner.router._tls_snapshot()
        validation, validated = validate_routes(response_payload, alias_to_raw, validator_runner, order, vehicle, start_match, pickup_match, dropoff_match, traffic, tls)
    finally:
        traci.close()
    write_json(OUT / "route_validation.json", validation)
    write_json(OUT / "codex_usage_audit.json", response.provider_audit)
    if validation["status"] != "passed":
        write_json(OUT / "pilot_summary.json", {
            "status": "model_route_rejected_no_retry",
            "model_calls": 1,
            "retry_count": 0,
            "validation": validation,
            "usage": response.provider_audit.get("event_audit"),
            "limitation": preflight["limitation"],
        })
        return

    raw_paths, scored = validated
    execution = r1.SimulationRun(0, "FASTEST", OUT / "sumo_execution", 90, 2.0, {}, "r2_codex_actual_latency")
    background = json.loads(BACKGROUND_DEMAND.read_text(encoding="utf-8"))["vehicles"]
    execution.background_vehicle_count = len(background)
    execution._start_sumo(background)
    execution.run_start_wall = time.perf_counter()
    try:
        # Replay the measured single-call planner latency as simulated planning delay.
        planner_delay_s = response.latency_ms / 1000.0
        execution._advance_planner_time(planner_delay_s)
        execution.planning_total_s = planner_delay_s
        execution.orders_by_vehicle[order["vehicle_id"]][0]["_planned_passenger"] = scored["pickup_to_dropoff"]
        execution._add_experiment_leg(
            order["vehicle_id"], execution.orders_by_vehicle[order["vehicle_id"]][0],
            "vehicle_to_pickup", scored["vehicle_to_pickup"],
            vehicle["initial_position"]["point_id"], order["pickup"]["point_id"],
        )
        deadline = float(traci.simulation.getTime()) + PILOT_MAX_SIM_TIME_S
        while execution.completed_orders < 1 and float(traci.simulation.getTime()) < deadline:
            execution._simulation_tick()
        sim_finish = float(traci.simulation.getTime())
        execution._track_vehicle_edges()
        completed = execution.completed_orders == 1
    finally:
        traci.close()

    trip_root = r1.ET.parse(execution.output_dir / "tripinfo.xml").getroot()
    trip_info = {item.get("id", ""): item for item in trip_root.findall("tripinfo")}
    alias_to_raw_reverse = {edge_id: f"e{index:04d}" for index, edge_id in enumerate(sorted(execution.core))}
    actual_legs = []
    for trip_id, metadata in execution.trip_meta.items():
        item = trip_info.get(trip_id)
        if item is None:
            continue
        battery = item.find("battery")
        gross = float(battery.get("totalEnergyConsumed", 0.0)) if battery is not None else 0.0
        regen = float(battery.get("totalEnergyRegenerated", 0.0)) if battery is not None else 0.0
        actual_legs.append({
            "order_id": metadata["order_id"],
            "leg_role": metadata["leg_role"],
            "route_edge_aliases": [alias_to_raw_reverse[e] for e in metadata["route_edges"]],
            "actual_duration_s": float(item.get("duration", 0.0)),
            "actual_waiting_s": float(item.get("waitingTime", 0.0)),
            "actual_distance_m": float(item.get("routeLength", 0.0)),
            "actual_energy_gross_wh": gross,
            "actual_energy_regenerated_wh": regen,
            "actual_energy_net_wh": gross - regen,
        })
    summary = {
        "pilot_id": preflight["pilot_id"],
        "status": "one_order_completed" if completed else "one_order_incomplete_at_2h_cap",
        "formal_matrix": False,
        "method": "R2_PURE_LLM_CODEX_CLI",
        "objective": "FASTEST",
        "seed": 0,
        "vehicle_id": order["vehicle_id"],
        "order_id": order["order_id"],
        "fixed_fleet_size_in_source_scenario": 10,
        "experimental_vehicles_in_this_diagnostic_pilot": 1,
        "dispatch_or_reassignment": False,
        "model": MODEL,
        "codex_cli_version": preflight["codex_cli_version"],
        "model_calls": 1,
        "retry_count": 0,
        "planner_latency_ms": response.latency_ms,
        "planner_latency_added_to_simulation_clock_s": response.latency_ms / 1000.0,
        "simulation_completion_time_from_t0_s": sim_finish + execution.pending_planner_s,
        "orders_completed": int(completed),
        "actual_legs": actual_legs,
        "actual_energy_net_kwh": sum(x["actual_energy_net_wh"] for x in actual_legs) / 1000.0,
        "actual_distance_km": sum(x["actual_distance_m"] for x in actual_legs) / 1000.0,
        "actual_waiting_s": sum(x["actual_waiting_s"] for x in actual_legs),
        "sumo_version": "1.28.0",
        "background_traffic": "90 vehicles/hour synthetic, two hours; only one experimental vehicle is active in this pilot",
        "validation": validation,
        "codex_usage": response.provider_audit.get("event_audit"),
        "rate_card_estimate": response.provider_audit.get("cost_estimate"),
        "account_billing": "not reported by Codex CLI",
        "limitation": preflight["limitation"],
    }
    write_json(OUT / "actual_legs.json", actual_legs)
    write_json(OUT / "pilot_summary.json", summary)
    write_json(OUT / "execution_manifest.json", {
        "prompt_sha256": preflight["prompt_sha256"],
        "model_response_sha256": digest_path(response_path),
        "response_schema_sha256": preflight["schema_sha256"],
        "network_sha256": preflight["network_sha256"],
        "frozen_orders_sha256": preflight["frozen_orders_sha256"],
        "endpoint_bindings_sha256": preflight["endpoint_bindings_sha256"],
        "usage_trace_sha256": response.provider_audit.get("trace_jsonl_sha256"),
        "model": MODEL,
        "cli_version": preflight["codex_cli_version"],
        "one_call_no_retry": True,
    })
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-model-call", action="store_true", help="make the single pre-registered Codex call and execute its route if valid")
    args = parser.parse_args()
    if args.execute_model_call:
        run_pilot()
    else:
        print(json.dumps(prepare(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
