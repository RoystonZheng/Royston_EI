#!/usr/bin/env python3
"""Freeze v3.2 inputs using connection-level passenger legality."""

from __future__ import annotations

import csv
import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import networkx as nx
import sumolib

import preflight_fixed_orders_sumo_v2 as pf
from strict_passenger_graph import build_passenger_graph


PACKAGE = Path(__file__).resolve().parents[1]
SOURCE_SCENARIOS = PACKAGE / "derived/scenarios/v3_1"
PREFLIGHT_DIR = PACKAGE / "derived/scenarios/v3_2/preflight_strict_v1"
NETWORK = PACKAGE / "derived/sumo/v1.28.0/porto.net.xml"
FROZEN_DIR = PACKAGE / "derived/scenarios/v3_2/frozen_inputs_v1_2"
TRUTH_SOURCE = SOURCE_SCENARIOS / "frozen_inputs_v1_1/validator_only"
TRUTH_DIR = FROZEN_DIR / "validator_only"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def points_by_id(order_file: dict[str, Any]) -> dict[str, dict[str, Any]]:
    points = {v["initial_position"]["point_id"]: v["initial_position"] for v in order_file["vehicles"]}
    for order in order_file["orders"]:
        for role in ("pickup", "dropoff"):
            points[order[role]["point_id"]] = order[role]
    return points


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="3_2")
    parser.add_argument("--preflight-dir", type=Path, default=PREFLIGHT_DIR)
    parser.add_argument("--frozen-dir", type=Path, default=FROZEN_DIR)
    args = parser.parse_args()
    version = args.version
    preflight_dir = args.preflight_dir
    frozen_dir = args.frozen_dir
    truth_dir = frozen_dir / "validator_only"
    truth_source_dir = SOURCE_SCENARIOS / "frozen_inputs_v1_1/validator_only"

    preflight = json.loads((preflight_dir / "summary.json").read_text(encoding="utf-8"))
    if preflight["network_sha256"] != sha256(NETWORK):
        raise RuntimeError("strict preflight network hash differs from the frozen network")
    if len(preflight["seeds"]) != 10 or any(s["status"] != "passed" or s["reachable_route_legs"] != 200 for s in preflight["seeds"]):
        raise RuntimeError("all ten seeds must pass strict passenger-connection preflight before freeze")

    net = sumolib.net.readNet(str(NETWORK), withInternal=False)
    graph, full_allowed, turns = build_passenger_graph(net, NETWORK, pf)
    core = set(max(nx.strongly_connected_components(graph), key=len))
    core_graph = graph.subgraph(core).copy()
    allowed = {edge_id: full_allowed[edge_id] for edge_id in core}
    frozen_dir.mkdir(parents=True, exist_ok=False)
    truth_dir.mkdir(parents=True)

    expected_mix = {"candidate_point_to_candidate_point": 70, "fuzzy_single_endpoint": 15, "fuzzy_both_endpoints": 15}
    seed_records = []
    for row in preflight["seeds"]:
        seed = int(row["seed"])
        source_path = SOURCE_SCENARIOS / f"orders_seed_{seed}_draft.json"
        orders = json.loads(source_path.read_text(encoding="utf-8"))
        if len(orders["vehicles"]) != 10 or len(orders["orders"]) != 100 or len(orders["route_legs"]) != 200:
            raise RuntimeError(f"seed {seed}: unexpected fleet/order/task count")
        counts = {kind: sum(o["order_kind"] == kind for o in orders["orders"]) for kind in expected_mix}
        if counts != expected_mix:
            raise RuntimeError(f"seed {seed}: endpoint mix changed: {counts}")
        points = points_by_id(orders)
        raw = json.loads((preflight_dir / f"endpoint_matches_seed_{seed}.json").read_text(encoding="utf-8"))
        candidates = raw["endpoint_matches"]
        bindings = {}
        for point_id in points:
            options = candidates.get(point_id, [])
            options = [m for m in options if m["edge_id"] in core]
            if not options:
                raise RuntimeError(f"seed {seed}: endpoint {point_id} has no legal core match")
            # Freeze one nearest, deterministic link match shared by all methods.
            chosen = dict(options[0])
            chosen.pop("candidate_rank", None)
            bindings[point_id] = [chosen]

        # Validate all 200 tasks against the exact single match that will be
        # passed to every method, not the looser multi-match preflight result.
        fixed_matches = {point_id: matches for point_id, matches in bindings.items()}
        failures = []
        source_cache: dict[str, tuple[dict[str, float], dict[str, list[str]]]] = {}
        alternative_cache: dict[tuple[str, str], list[str] | None] = {}
        for task in orders["route_legs"]:
            result = pf.route_between_points(
                core_graph, allowed, fixed_matches, task["from_point_id"], task["to_point_id"],
                source_cache, alternative_cache,
            )
            if result["status"] != "reachable":
                failures.append(task["route_task_id"])
        if failures:
            raise RuntimeError(f"seed {seed}: single frozen bindings break route tasks: {failures[:10]}")

        for vehicle in orders["vehicles"]:
            if len(vehicle["fixed_order_sequence"]) != 10:
                raise RuntimeError(f"seed {seed}: non-fixed sequence for {vehicle['vehicle_id']}")
        # Keep planner-facing coordinates, IDs and fixed orders as generated;
        # remove hidden fuzzy OSM truth from that input as in v3.1.
        def strip_truth(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: strip_truth(v) for k, v in value.items() if k not in {"validation_truth", "fuzzy_endpoint_truth"}}
            if isinstance(value, list):
                return [strip_truth(item) for item in value]
            return value

        frozen = strip_truth(orders)
        frozen["scenario_id"] = f"porto_fixed_fleet_route_v{version}_seed_{seed}"
        frozen["status"] = "frozen_strict_passenger_preflight_passed_dynamic_simulation_pending"
        frozen["route_preflight"] = {
            "status": "passed",
            "route_legs_checked": 200,
            "reachable_route_legs": 200,
            "network_sha256": sha256(NETWORK),
            "method": "static directed route validation on the largest passenger-legal SCC, including SUMO connection allow/disallow and custom valid via-way restrictions",
        }
        for order in frozen["orders"]:
            order["route_validation_status"] = "passed_static_passenger_connection_reachability"
        for task in frozen["route_legs"]:
            task["traditional_reachability_status"] = "passed_static_passenger_connection_reachability"

        frozen_path = frozen_dir / f"orders_seed_{seed}_frozen.json"
        binding_path = frozen_dir / f"shared_endpoint_bindings_seed_{seed}.json"
        truth_source = truth_source_dir / f"endpoint_truth_seed_{seed}.json"
        truth_path = truth_dir / truth_source.name
        write_json(frozen_path, frozen)
        write_json(binding_path, {
            "scenario_id": frozen["scenario_id"],
            "network_sha256": sha256(NETWORK),
            "status": "frozen_single_passenger_legal_snap_per_endpoint",
            "shared_with_all_methods": True,
            "binding_selection": "nearest deterministic match among passenger-legal core edges",
            "endpoints": bindings,
        })
        write_json(truth_path, json.loads(truth_source.read_text(encoding="utf-8")))
        seed_records.append({
            "seed": seed,
            "frozen_orders_path": frozen_path.name,
            "frozen_orders_sha256": sha256(frozen_path),
            "shared_endpoint_bindings_path": binding_path.name,
            "shared_endpoint_bindings_sha256": sha256(binding_path),
            "validator_truth_path": f"validator_only/{truth_path.name}",
            "validator_truth_sha256": sha256(truth_path),
            "orders": 100,
            "vehicles": 10,
            "route_legs_static_reachable": 200,
            "endpoint_mix": counts,
        })

    edge_rows = []
    for edge_id in sorted(core):
        edge = allowed[edge_id]
        edge_rows.append({
            "edge_id": edge_id,
            "from_node": edge.getFromNode().getID(),
            "to_node": edge.getToNode().getID(),
            "length_m": round(edge.getLength(), 3),
            "speed_limit_mps": round(edge.getSpeed(), 3),
            "lane_count": len(edge.getLanes()),
            "highway_type": edge.getType(),
        })
    edge_csv = frozen_dir / "routeable_core_edges.csv"
    with edge_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(edge_rows[0]))
        writer.writeheader(); writer.writerows(edge_rows)
    connection_rows = [{"from_edge": a, "to_edge": b} for a in sorted(core) for b in sorted(core_graph.successors(a))]
    connection_csv = frozen_dir / "routeable_core_connections.csv"
    with connection_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["from_edge", "to_edge"])
        writer.writeheader(); writer.writerows(connection_rows)
    legal_turns_in_core = sum(len(rows) for (a, b), rows in turns.items() if a in core and b in core)
    config = {
        "scenario_id": f"porto_fixed_fleet_route_v{version}",
        "status": "inputs_frozen_strict_passenger_preflight_passed_dynamic_simulation_pending",
        "supersedes": "v3.1 frozen inputs for route execution; v3.1 files remain unchanged for audit",
        "data_lineage": {
            "order_heat_source": "local Porto taxi heat/OD aggregates; proxies for completed taxi trips, not online ride-hailing requests",
            "road_network_source": "fixed OSM snapshot 2026-10-22T04:01:05Z converted by SUMO 1.28.0",
            "traffic_and_signal_status": "simulated in SUMO; not observed Porto real-time traffic or measured signal phases",
        },
        "map": {
            "fence_wgs84": {"west": -8.635, "south": 41.138, "east": -8.595, "north": 41.162},
            "network_directed_edge_count": len(net.getEdges()),
            "fenced_passenger_edge_count": graph.number_of_nodes(),
            "fenced_passenger_connection_count": graph.number_of_edges(),
            "routeable_strongly_connected_core_edge_count": len(core),
            "routeable_core_connection_count": len(connection_rows),
            "legal_passenger_connection_instances_in_core": legal_turns_in_core,
            "route_permission_rule": "XML connection allow/disallow and internal via-lane allow/disallow must permit vClass=passenger; restricted movements are excluded",
            "base_map_keeps_outside_core_roads": True,
            "network_sha256": sha256(NETWORK),
        },
        "fleet_and_orders": {
            "vehicles": 10,
            "orders_per_vehicle": 10,
            "orders_per_seed": 100,
            "seeds": list(range(10)),
            "fixed_vehicle_order_sequences": True,
            "dispatch_or_reassignment": False,
            "endpoint_mix_per_seed": expected_mix,
            "generated_order_endpoints_visible_in_base_map": False,
            "map_match_is_frozen_and_shared": True,
            "route_legs_per_seed_static_reachability_gate": 200,
            "static_reachability_gate": "passed 2000/2000 using exact single frozen endpoint bindings and passenger-legal connections",
        },
        "traffic_simulation": {
            "engine": "Eclipse SUMO 1.28.0 with TraCI",
            "step_length_s": 0.1,
            "signal_program_count": sum(1 for _ in ET.parse(NETWORK).getroot().findall("tlLogic")),
            "signal_programs": "netconvert-generated fixed-time signal programs; simulated assumptions, not measured Porto timing",
            "background_demand": "synthetic 90 vehicles/hour for 2 hours, OD weights from historical heat cells; stress rate not calibrated to Porto traffic",
            "background_demand_status": "explicit pilot assumption; low/high sensitivity remains pending",
            "all_active_vehicles_share_network_and_queue_model": True,
            "signals_continue_while_planner_computes": True,
            "planner_delay_counts_in_completion_time": True,
        },
        "methods": [
            {"id": "R1_TD_DIJKSTRA", "status": "ready_to_run", "description": "time-dependent Dijkstra with live traffic snapshot and predicted signal wait"},
            {"id": "R2_PURE_LLM_CODEX_CLI", "status": "one_call_pilot_only", "description": "one request only after candidate route input is frozen; no retries or route fallback"},
            {"id": "R3_AGENT_OPTIMIZER", "status": "deferred", "description": "not in this execution batch"},
        ],
        "objectives": ["fastest_total_elapsed_time", "lowest_simulated_electric_energy"],
        "llm_budget_gate": {
            "formal_request_count_estimate": 2000,
            "currently_authorized_requests": 1,
            "retry_policy": "zero retry; stop on any failed or invalid request",
            "model": "gpt-6-luna",
            "reasoning_effort": "xhigh",
            "no_batch_requests_until_separate_budget_gate": True,
        },
        "seeds": seed_records,
    }
    config_path = frozen_dir / f"scenario_config_v{version}.json"
    write_json(config_path, config)
    manifest = {
        "manifest_version": "1.0.0",
        "scenario_id": config["scenario_id"],
        "status": config["status"],
        "network_sha256": sha256(NETWORK),
        "strict_preflight_summary_sha256": sha256(preflight_dir / "summary.json"),
        "frozen_orders": seed_records,
        "core_edge_file": {"path": edge_csv.name, "sha256": sha256(edge_csv), "rows": len(edge_rows)},
        "core_connection_file": {"path": connection_csv.name, "sha256": sha256(connection_csv), "rows": len(connection_rows)},
        "scenario_config": {"path": config_path.name, "sha256": sha256(config_path)},
        "evidence_boundary": "strict static route reachability; dynamic R1 result pending",
    }
    manifest_path = frozen_dir / f"manifest_v{version}.json"
    write_json(manifest_path, manifest)
    print(json.dumps({
        "status": manifest["status"],
        "frozen_seeds": len(seed_records),
        "strict_static_reachability": "2000/2000",
        "routeable_core_edges": len(edge_rows),
        "core_connections": len(connection_rows),
        "manifest": str(manifest_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
