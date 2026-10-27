#!/usr/bin/env python3
"""Run one fixed-order R1 dynamic Porto SUMO scenario.

This runner has no dispatch/reassignment layer. It plans both legs of the
current fixed order, accounts for measured planning delay, and executes those
legs in SUMO while signals and all other vehicles keep moving.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import os
import random
import statistics
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import networkx as nx
import sumolib
import traci

import preflight_fixed_orders_sumo_v2 as pf
from strict_passenger_graph import build_passenger_graph


PACKAGE = Path(__file__).resolve().parents[1]
SCENARIO_DIR = PACKAGE / "derived/scenarios/v3_3"
FROZEN_DIR = SCENARIO_DIR / "frozen_inputs_v1_3"
NETWORK = PACKAGE / "derived/sumo/v1.28.0/porto.net.xml"
PROTOCOL_FILE = PACKAGE / "derived/experiments/protocols/r1_dynamic_sumo_protocol_v3.json"
SUMO_BIN = Path(os.environ.get("PORTO_SUMO_BIN", ""))
DT = 0.1
FORBIDDEN_WAY_SEQUENCES = tuple(tuple(x) for x in pf.FORBIDDEN_WAY_SEQUENCES)

EV_PARAMS = {
    "type_id": "PORTO_EV_KIA_SOUL_2020_PROXY",
    "minGap": "2.50",
    "maxSpeed": "29.06",
    "accel": "2.0",
    "decel": "4.5",
    "sigma": "0.0",
    "emissionClass": "Energy/unknown",
    "mass_kg": 1830.0,
    "front_surface_area_m2": 2.6,
    "air_drag_coefficient": 0.35,
    "rotating_mass_kg": 40.0,
    "radial_drag_coefficient": 0.1,
    "roll_drag_coefficient": 0.01,
    "constant_power_intake_w": 100.0,
    "propulsion_efficiency": 0.98,
    "recuperation_efficiency": 0.96,
    "battery_capacity_wh": 64000.0,
    "maximum_power_w": 150000.0,
    "stopping_threshold_mps": 0.1,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ev_types(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    attrs = " ".join(
        f'{key}="{EV_PARAMS[param]}"'
        for key, param in (
            ("id", "type_id"),
            ("minGap", "minGap"),
            ("maxSpeed", "maxSpeed"),
            ("accel", "accel"),
            ("decel", "decel"),
            ("sigma", "sigma"),
            ("emissionClass", "emissionClass"),
        )
    )
    params = {
        "has.battery.device": "true",
        "airDragCoefficient": EV_PARAMS["air_drag_coefficient"],
        "constantPowerIntake": EV_PARAMS["constant_power_intake_w"],
        "frontSurfaceArea": EV_PARAMS["front_surface_area_m2"],
        "rotatingMass": EV_PARAMS["rotating_mass_kg"],
        "device.battery.capacity": EV_PARAMS["battery_capacity_wh"],
        "maximumPower": EV_PARAMS["maximum_power_w"],
        "propulsionEfficiency": EV_PARAMS["propulsion_efficiency"],
        "radialDragCoefficient": EV_PARAMS["radial_drag_coefficient"],
        "recuperationEfficiency": EV_PARAMS["recuperation_efficiency"],
        "rollDragCoefficient": EV_PARAMS["roll_drag_coefficient"],
        "stoppingThreshold": EV_PARAMS["stopping_threshold_mps"],
    }
    param_xml = "".join(f'<param key="{key}" value="{value}"/>' for key, value in params.items())
    path.write_text(f"<routes><vType {attrs} vClass=\"passenger\" mass=\"{EV_PARAMS['mass_kg']}\" length=\"4.5\">{param_xml}</vType></routes>\n", encoding="utf-8")


def network_core(net: Any) -> tuple[nx.DiGraph, dict[str, Any], set[str], dict[tuple[str, str], list[tuple[str, int]]], dict[str, list[dict[str, Any]]]]:
    graph, allowed_all, legal_turns = build_passenger_graph(net, NETWORK, pf)
    components = sorted(nx.strongly_connected_components(graph), key=len, reverse=True)
    core = set(components[0])
    graph = graph.subgraph(core).copy()
    allowed = {edge_id: allowed_all[edge_id] for edge_id in core}
    root = ET.parse(NETWORK).getroot()
    tls_by_turn: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    for (source, target), connections in legal_turns.items():
        if source not in core or target not in core:
            continue
        for connection in connections:
            tls_id = connection["tls_id"]
            link_index = connection["link_index"]
            if tls_id and link_index >= 0:
                item = (str(tls_id), int(link_index))
                if item not in tls_by_turn[(source, target)]:
                    tls_by_turn[(source, target)].append(item)

    programs = {}
    for logic in root.findall("tlLogic"):
        phases = [{"duration": float(p.get("duration", "0")), "state": p.get("state", "")} for p in logic.findall("phase")]
        if phases:
            programs[logic.get("id", "")] = phases
    return graph, allowed, core, tls_by_turn, programs


def point_bindings(seed: int) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    orders_path = FROZEN_DIR / f"orders_seed_{seed}_frozen.json"
    match_path = FROZEN_DIR / f"shared_endpoint_bindings_seed_{seed}.json"
    orders = json.loads(orders_path.read_text(encoding="utf-8"))
    matches = json.loads(match_path.read_text(encoding="utf-8"))["endpoints"]
    points: dict[str, dict[str, Any]] = {}
    for vehicle in orders["vehicles"]:
        p = vehicle["initial_position"]
        points[p["point_id"]] = p
    for order in orders["orders"]:
        points[order["pickup"]["point_id"]] = order["pickup"]
        points[order["dropoff"]["point_id"]] = order["dropoff"]
    for point_id in points:
        if point_id not in matches or len(matches[point_id]) != 1:
            raise RuntimeError(f"endpoint {point_id} lacks exactly one frozen link binding")
    return points, {"orders": orders, "matches": matches, "orders_path": orders_path, "binding_path": match_path}


def phase_at_time(phases: list[dict[str, Any]], current_phase: int, next_switch: float, now: float, target_time: float) -> tuple[int, float]:
    """Return active phase and its ending time for a frozen cyclic TLS program."""
    if not phases:
        return 0, target_time
    phase = max(0, min(current_phase, len(phases) - 1))
    switch = max(now, next_switch)
    if target_time < switch:
        return phase, switch
    cursor = switch
    phase = (phase + 1) % len(phases)
    guard = 0
    while guard < len(phases) * 4:
        duration = max(0.1, phases[phase]["duration"])
        end = cursor + duration
        if target_time < end:
            return phase, end
        cursor = end
        phase = (phase + 1) % len(phases)
        guard += 1
    return phase, target_time + max(0.1, phases[phase]["duration"])


class DynamicRouter:
    def __init__(self, graph: nx.DiGraph, allowed: dict[str, Any], tls_by_turn: dict[tuple[str, str], list[tuple[str, int]]], programs: dict[str, list[dict[str, Any]]]):
        self.graph = graph
        self.allowed = allowed
        self.tls_by_turn = tls_by_turn
        self.programs = programs

    def live_traffic_snapshot(self, now: float) -> dict[str, Any]:
        speeds: dict[str, list[float]] = defaultdict(list)
        stopped: Counter[str] = Counter()
        for vehicle_id in traci.vehicle.getIDList():
            try:
                edge_id = traci.vehicle.getRoadID(vehicle_id)
                if edge_id not in self.allowed:
                    continue
                speed = max(0.0, float(traci.vehicle.getSpeed(vehicle_id)))
                speeds[edge_id].append(speed)
                if speed < 0.3:
                    stopped[edge_id] += 1
            except traci.TraCIException:
                continue
        edge_state = {}
        for edge_id, values in speeds.items():
            edge_state[edge_id] = {"vehicle_count": len(values), "mean_speed_mps": sum(values) / len(values), "halting_count": stopped[edge_id]}
        return {
            "sim_time_s": round(now, 3),
            "occupied_edge_count": len(edge_state),
            "active_vehicle_count": sum(x["vehicle_count"] for x in edge_state.values()),
            "halting_vehicle_count": sum(x["halting_count"] for x in edge_state.values()),
            "edge_state": edge_state,
        }

    def _tls_snapshot(self, edge_path_hint: set[str] | None = None) -> dict[str, Any]:
        used = set()
        if edge_path_hint:
            for source in edge_path_hint:
                for target in self.graph.successors(source):
                    used.update(tls for tls, _ in self.tls_by_turn.get((source, target), []))
        else:
            for values in self.tls_by_turn.values():
                used.update(tls for tls, _ in values)
        output = {}
        for tls_id in used:
            try:
                output[tls_id] = {"phase": int(traci.trafficlight.getPhase(tls_id)), "next_switch_s": float(traci.trafficlight.getNextSwitch(tls_id))}
            except traci.TraCIException:
                continue
        return output

    def _wait_for_green(self, tls_id: str, link_index: int, eta: float, now: float, tls_snapshot: dict[str, Any]) -> float:
        phases = self.programs.get(tls_id)
        current = tls_snapshot.get(tls_id)
        if not phases or not current or not 0 <= link_index < max(len(p["state"]) for p in phases):
            return 0.0
        phase, phase_end = phase_at_time(phases, current["phase"], current["next_switch_s"], now, eta)
        state = phases[phase]["state"]
        if link_index < len(state) and state[link_index] in "gG":
            return 0.0
        elapsed = max(0.0, phase_end - eta)
        next_phase = (phase + 1) % len(phases)
        for _ in range(len(phases)):
            next_state = phases[next_phase]["state"]
            if link_index < len(next_state) and next_state[link_index] in "gG":
                return elapsed
            elapsed += max(0.1, phases[next_phase]["duration"])
            next_phase = (next_phase + 1) % len(phases)
        return 0.0

    def _turn_wait(self, from_edge: str, to_edge: str, eta: float, now: float, tls_snapshot: dict[str, Any]) -> float:
        waits = [self._wait_for_green(tls, index, eta, now, tls_snapshot) for tls, index in self.tls_by_turn.get((from_edge, to_edge), [])]
        return min(waits) if waits else 0.0

    def _edge_time_and_energy(self, edge_id: str, distance_m: float, eta: float, now: float, traffic: dict[str, Any]) -> tuple[float, float]:
        if distance_m <= 0.001:
            return 0.0, 0.0
        edge = self.allowed[edge_id]
        limit = max(1.0, float(edge.getSpeed()))
        state = traffic["edge_state"].get(edge_id)
        elapsed = max(0.0, eta - now)
        if state:
            decay = math.exp(-elapsed / 120.0)
            count = state["vehicle_count"] * decay
            halted = state["halting_count"] * decay
            observed = state["mean_speed_mps"]
            blended = 0.7 * limit + 0.3 * max(0.0, observed)
            capacity = max(1.0, edge.getLength() * max(1, len(edge.getLanes())) / 35.0)
            density_penalty = max(0.0, count - capacity) * 1.5 * decay
            speed = max(0.35 * limit, min(limit, blended / (1.0 + 0.18 * max(0.0, count - 1.0))))
            queue_penalty = 2.5 * halted + density_penalty
        else:
            speed = limit
            queue_penalty = 0.0
        drive_s = distance_m / max(0.1, speed)
        travel_s = drive_s + queue_penalty

        mass = EV_PARAMS["mass_kg"]
        gravity = 9.80665
        efficiency = EV_PARAMS["propulsion_efficiency"]
        rolling_wh = mass * gravity * EV_PARAMS["roll_drag_coefficient"] * distance_m / (3600.0 * efficiency)
        rho = 1.225
        aero_force = 0.5 * rho * EV_PARAMS["air_drag_coefficient"] * EV_PARAMS["front_surface_area_m2"] * speed * speed
        aero_wh = aero_force * distance_m / (3600.0 * efficiency)
        auxiliary_wh = EV_PARAMS["constant_power_intake_w"] * travel_s / 3600.0
        stop_wh = 0.0
        if state and state["halting_count"] > 0:
            kinetic_wh = 0.5 * mass * speed * speed / (3600.0 * efficiency)
            stop_wh = min(8.0, kinetic_wh * (1.0 - EV_PARAMS["recuperation_efficiency"]) * min(2.0, state["halting_count"]))
        return travel_s, rolling_wh + aero_wh + auxiliary_wh + stop_wh

    @staticmethod
    def _append_way_suffix(suffix: tuple[str, ...], edge_id: str) -> tuple[str, ...] | None:
        way = pf.osm_way_id(edge_id)
        if suffix and suffix[-1] == way:
            return suffix
        expanded = suffix + (way,)
        for forbidden in FORBIDDEN_WAY_SEQUENCES:
            if len(expanded) >= len(forbidden) and expanded[-len(forbidden):] == forbidden:
                return None
        return expanded[-3:]

    def route(self, from_match: dict[str, Any], to_match: dict[str, Any], objective: str, now: float, traffic: dict[str, Any], tls_snapshot: dict[str, Any], start_time_offset_s: float = 0.0) -> dict[str, Any]:
        source, target = from_match["edge_id"], to_match["edge_id"]
        start_pos, end_pos = float(from_match["edge_pos_m"]), float(to_match["edge_pos_m"])
        start_eta = now + start_time_offset_s
        if source == target and end_pos >= start_pos:
            travel_s, energy_wh = self._edge_time_and_energy(source, end_pos - start_pos, start_eta, now, traffic)
            return {"status": "ok", "path": [source], "predicted_travel_time_s": travel_s, "predicted_energy_wh": energy_wh, "signal_wait_s": 0.0, "distance_m": end_pos - start_pos, "expanded_states": 0}

        start_edge = self.allowed[source]
        initial_distance = max(0.0, start_edge.getLength() - start_pos)
        initial_time_s, initial_energy = self._edge_time_and_energy(source, initial_distance, start_eta, now, traffic)
        initial_suffix = (pf.osm_way_id(source),)
        start_state = (source, False, initial_suffix)
        # Heap key uses the requested primary objective; elapsed time is the tie breaker.
        first_metric = initial_time_s if objective == "FASTEST" else initial_energy
        heap = [(first_metric, initial_time_s, 0.0, start_state, (source,))]
        best = {start_state: (first_metric, initial_time_s)}
        expanded = 0
        while heap:
            metric, elapsed, signal_sum, state, path = heapq.heappop(heap)
            if best.get(state) != (metric, elapsed):
                continue
            edge_id, left_source, suffix = state
            expanded += 1
            if edge_id == target and (source != target or left_source):
                target_time, target_energy = self._edge_time_and_energy(target, end_pos, now + start_time_offset_s + elapsed, now, traffic)
                if len(path) == 1 and source == target:
                    target_time, target_energy = self._edge_time_and_energy(target, max(0.0, end_pos - start_pos), now + start_time_offset_s, now, traffic)
                    total_time, total_energy = target_time, target_energy
                else:
                    # The source segment was already included. For target entries,
                    # the current edge was added as a partial edge in expansion.
                    total_time = elapsed
                    total_energy = metric if objective == "LOWEST_ENERGY" else 0.0
                    if objective == "FASTEST":
                        total_energy = 0.0
                        cursor_eta = now + start_time_offset_s
                        for index, edge in enumerate(path):
                            distance = initial_distance if index == 0 else (end_pos if index == len(path) - 1 else self.allowed[edge].getLength())
                            _, energy_piece = self._edge_time_and_energy(edge, distance, cursor_eta, now, traffic)
                            total_energy += energy_piece
                            cursor_eta += distance / max(0.1, self.allowed[edge].getSpeed())
                    elif path:
                        # Metric is cumulative energy; signal idle energy is included on each transition.
                        total_energy = metric
                distance = max(0.0, initial_distance) + sum(self.allowed[e].getLength() for e in path[1:-1]) + (end_pos if len(path) > 1 else max(0.0, end_pos - start_pos))
                return {"status": "ok", "path": list(path), "predicted_travel_time_s": total_time, "predicted_energy_wh": total_energy, "signal_wait_s": signal_sum, "distance_m": distance, "expanded_states": expanded}

            for next_edge in self.graph.successors(edge_id):
                next_edge = str(next_edge)
                new_suffix = self._append_way_suffix(suffix, next_edge)
                if new_suffix is None:
                    continue
                eta = now + start_time_offset_s + elapsed
                wait = self._turn_wait(edge_id, next_edge, eta, now, tls_snapshot)
                distance = end_pos if next_edge == target else self.allowed[next_edge].getLength()
                travel_s, energy_wh = self._edge_time_and_energy(next_edge, distance, eta + wait, now, traffic)
                candidate_elapsed = elapsed + wait + travel_s
                candidate_energy = (metric if objective == "LOWEST_ENERGY" else initial_energy) + EV_PARAMS["constant_power_intake_w"] * wait / 3600.0 + energy_wh
                candidate_metric = candidate_elapsed if objective == "FASTEST" else candidate_energy
                candidate_state = (next_edge, True, new_suffix)
                old = best.get(candidate_state)
                if old is not None and (candidate_metric > old[0] + 1e-9 or (abs(candidate_metric - old[0]) <= 1e-9 and candidate_elapsed >= old[1])):
                    continue
                best[candidate_state] = (candidate_metric, candidate_elapsed)
                heapq.heappush(heap, (candidate_metric, candidate_elapsed, signal_sum + wait, candidate_state, path + (next_edge,)))

        return {"status": "unreachable", "path": [], "predicted_travel_time_s": None, "predicted_energy_wh": None, "signal_wait_s": 0.0, "distance_m": 0.0, "expanded_states": expanded}


class SimulationRun:
    def __init__(self, seed: int, objective: str, output_dir: Path, traffic_rate_per_hour: int, traffic_hours: float, planning_latency_trace: dict[str, float], planning_latency_trace_sha256: str):
        self.seed = seed
        self.objective = objective
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.traffic_rate_per_hour = traffic_rate_per_hour
        self.traffic_hours = traffic_hours
        self.planning_latency_trace = planning_latency_trace
        self.planning_latency_trace_sha256 = planning_latency_trace_sha256
        self.background_vehicle_count = 0
        self.net = sumolib.net.readNet(str(NETWORK), withInternal=False)
        self.graph, self.allowed, self.core, self.tls_by_turn, self.programs = network_core(self.net)
        self.router = DynamicRouter(self.graph, self.allowed, self.tls_by_turn, self.programs)
        self.points, self.inputs = point_bindings(seed)
        self.orders_file = self.inputs["orders"]
        self.match = self.inputs["matches"]
        self.orders_by_vehicle = {v["vehicle_id"]: sorted([o for o in self.orders_file["orders"] if o["vehicle_id"] == v["vehicle_id"]], key=lambda x: x["sequence_index"]) for v in self.orders_file["vehicles"]}
        self.vehicle_info = {v["vehicle_id"]: v for v in self.orders_file["vehicles"]}
        self.next_index = {v["vehicle_id"]: 0 for v in self.orders_file["vehicles"]}
        self.current_point = {v["vehicle_id"]: v["initial_position"]["point_id"] for v in self.orders_file["vehicles"]}
        self.ready: list[tuple[float, str]] = [(0.0, v["vehicle_id"]) for v in self.orders_file["vehicles"]]
        heapq.heapify(self.ready)
        self.pending_planner_s = 0.0
        self.planning_total_s = 0.0
        self.completed_orders = 0
        self.completed_by_vehicle: Counter[str] = Counter()
        self.exp_trip_counter = 0
        self.route_rows: list[dict[str, Any]] = []
        self.plan_rows: list[dict[str, Any]] = []
        self.trip_meta: dict[str, dict[str, Any]] = {}
        self.active_trip_meta: dict[str, dict[str, Any]] = {}
        self.edge_entries: dict[str, tuple[str, float]] = {}
        self.edge_pass_rows: list[dict[str, Any]] = []
        self.signal_touch_rows: list[dict[str, Any]] = []
        self.order_rows: list[dict[str, Any]] = []
        self.noop_leg_count = 0
        self.sim_start = 0.0
        self.run_start_wall = 0.0
        self.types_file = self.output_dir / "ev_type.rou.xml"
        write_ev_types(self.types_file)

    def _traffic_snapshot(self) -> dict[str, Any]:
        return self.router.live_traffic_snapshot(float(traci.simulation.getTime()))

    def _add_experiment_leg(self, vehicle_id: str, order: dict[str, Any], role: str, route_result: dict[str, Any], start_point: str, end_point: str) -> None:
        if route_result["status"] != "ok":
            raise RuntimeError(f"planner returned {route_result['status']} for {order['order_id']} {role}")
        path = route_result["path"]
        if not path or not set(path).issubset(self.core) or not pf.is_legal_sequence(path):
            raise RuntimeError(f"route failed edge/restriction validation: {order['order_id']} {role}")
        start_match = self.match[start_point][0]
        end_match = self.match[end_point][0]
        distance = float(route_result["distance_m"])
        if distance < 0.05:
            self.noop_leg_count += 1
            self.trip_meta[f"noop_{order['order_id']}_{role}_{self.noop_leg_count}"] = {
                "logical_vehicle_id": vehicle_id, "order_id": order["order_id"], "leg_role": role,
                "route_edges": path, "from_point_id": start_point, "to_point_id": end_point,
                "distance_m": 0.0, "predicted_travel_time_s": 0.0, "predicted_energy_wh": 0.0,
                "actual_noop": True,
            }
            if role == "vehicle_to_pickup":
                self._add_experiment_leg(vehicle_id, order, "pickup_to_dropoff", order["_planned_passenger"], order["pickup"]["point_id"], order["dropoff"]["point_id"])
            else:
                self._complete_order(vehicle_id, order)
            return

        trip_id = f"exp_{vehicle_id}_{order['order_id']}_{'deadhead' if role == 'vehicle_to_pickup' else 'passenger'}"
        route_id = f"route_{trip_id}"
        traci.route.add(route_id, path)
        departure = float(traci.simulation.getTime()) + self.pending_planner_s
        traci.vehicle.add(
            trip_id,
            route_id,
            typeID=EV_PARAMS["type_id"],
            depart=f"{departure:.3f}",
            departLane="best",
            departPos=f"{float(start_match['edge_pos_m']):.3f}",
            departSpeed="max",
            arrivalLane="current",
            arrivalPos=f"{float(end_match['edge_pos_m']):.3f}",
            arrivalSpeed="current",
        )
        meta = {
            "logical_vehicle_id": vehicle_id,
            "order_id": order["order_id"],
            "sequence_index": order["sequence_index"],
            "leg_role": role,
            "from_point_id": start_point,
            "to_point_id": end_point,
            "route_edges": path,
            "planned_distance_m": distance,
            "predicted_travel_time_s": route_result["predicted_travel_time_s"],
            "predicted_energy_wh": route_result["predicted_energy_wh"],
            "predicted_signal_wait_s": route_result["signal_wait_s"],
            "actual_noop": False,
        }
        self.trip_meta[trip_id] = meta
        self.active_trip_meta[trip_id] = meta
        self.edge_entries[trip_id] = (path[0], departure)

    def _close_edge(self, trip_id: str, at_time: float, next_edge: str | None = None) -> None:
        current = self.edge_entries.get(trip_id)
        if current is None:
            return
        edge_id, enter = current
        if edge_id.startswith(":"):
            if next_edge and not next_edge.startswith(":"):
                self.edge_entries[trip_id] = (next_edge, at_time)
            return
        if at_time >= enter:
            meta = self.trip_meta.get(trip_id, {})
            self.edge_pass_rows.append({
                "trip_id": trip_id,
                "logical_vehicle_id": meta.get("logical_vehicle_id"),
                "order_id": meta.get("order_id"),
                "leg_role": meta.get("leg_role"),
                "edge_id": edge_id,
                "entry_time_s": round(enter, 3),
                "exit_time_s": round(at_time, 3),
                "actual_edge_time_s": round(at_time - enter, 3),
                "next_edge_id": next_edge,
            })
        if next_edge:
            self.edge_entries[trip_id] = (next_edge, at_time)
        else:
            self.edge_entries.pop(trip_id, None)

    def _track_vehicle_edges(self) -> None:
        now = float(traci.simulation.getTime())
        active = set(traci.vehicle.getIDList())
        for trip_id in list(self.active_trip_meta):
            if trip_id not in active:
                continue
            try:
                edge_id = traci.vehicle.getRoadID(trip_id)
            except traci.TraCIException:
                continue
            if not edge_id:
                continue
            current = self.edge_entries.get(trip_id)
            if current is None:
                self.edge_entries[trip_id] = (edge_id, now)
            elif edge_id != current[0]:
                self._close_edge(trip_id, now, edge_id)

    def _complete_order(self, vehicle_id: str, order: dict[str, Any]) -> None:
        self.completed_orders += 1
        self.completed_by_vehicle[vehicle_id] += 1
        finish = float(traci.simulation.getTime())
        self.order_rows.append({
            "logical_vehicle_id": vehicle_id,
            "order_id": order["order_id"],
            "sequence_index": order["sequence_index"],
            "pickup_point_id": order["pickup"]["point_id"],
            "dropoff_point_id": order["dropoff"]["point_id"],
            "order_finish_time_s": round(finish, 3),
            "status": "completed",
        })
        self.next_index[vehicle_id] += 1
        if self.next_index[vehicle_id] < len(self.orders_by_vehicle[vehicle_id]):
            next_order = self.orders_by_vehicle[vehicle_id][self.next_index[vehicle_id]]
            self.current_point[vehicle_id] = order["dropoff"]["point_id"]
            heapq.heappush(self.ready, (finish, vehicle_id))
        else:
            self.current_point[vehicle_id] = order["dropoff"]["point_id"]

    def _process_arrivals(self) -> None:
        now = float(traci.simulation.getTime())
        arrived = list(traci.simulation.getArrivedIDList())
        for trip_id in arrived:
            meta = self.active_trip_meta.pop(trip_id, None)
            if meta is None:
                continue
            self._close_edge(trip_id, now)
            order = next(x for x in self.orders_by_vehicle[meta["logical_vehicle_id"]] if x["order_id"] == meta["order_id"])
            if meta["leg_role"] == "vehicle_to_pickup":
                self._add_experiment_leg(meta["logical_vehicle_id"], order, "pickup_to_dropoff", order["_planned_passenger"], order["pickup"]["point_id"], order["dropoff"]["point_id"])
            else:
                self._complete_order(meta["logical_vehicle_id"], order)

    def _simulation_tick(self) -> None:
        traci.simulationStep()
        self._track_vehicle_edges()
        self._process_arrivals()

    def _advance_planner_time(self, seconds: float) -> None:
        self.planning_total_s += seconds
        self.pending_planner_s += seconds
        while self.pending_planner_s + 1e-9 >= DT:
            self.pending_planner_s -= DT
            self._simulation_tick()

    def _background_trips(self, seed: int) -> list[dict[str, Any]]:
        # Historical endpoint counts shape the OD draws; the 90 veh/h flow is a
        # synthetic stress level, not an observed Porto traffic volume.
        points = []
        for p in self.orders_file["candidate_hotspots"]:
            matches = pf.map_match_point(self.net, p, self.allowed)
            if matches:
                points.append({**p, "binding": matches[0]})
        if len(points) < 10:
            raise RuntimeError("not enough heat cells could be matched for background traffic")
        rng = random.Random(4_200_000 + seed)
        origins = [p for p in points if int(p.get("heat_proxy", {}).get("pickup_count", 0)) > 0]
        destinations = [p for p in points if int(p.get("heat_proxy", {}).get("dropoff_count", 0)) > 0]
        origin_weights = [int(p["heat_proxy"]["pickup_count"]) for p in origins]
        destination_weights = [int(p["heat_proxy"]["dropoff_count"]) for p in destinations]
        route_cache: dict[tuple[str, str], list[str]] = {}
        trips = []
        count = self.traffic_rate_per_hour * int(self.traffic_hours)
        for index in range(count):
            for attempt in range(80):
                origin = rng.choices(origins, weights=origin_weights, k=1)[0]
                destination = rng.choices(destinations, weights=destination_weights, k=1)[0]
                if origin["point_id"] == destination["point_id"]:
                    continue
                key = (origin["binding"]["edge_id"], destination["binding"]["edge_id"])
                if key not in route_cache:
                    try:
                        path = nx.shortest_path(self.graph, key[0], key[1], weight=lambda a, b, _: self.allowed[b].getLength() / max(0.1, self.allowed[b].getSpeed()))
                        if not pf.is_legal_sequence(path):
                            alt = None
                            for i, candidate in enumerate(nx.shortest_simple_paths(self.graph, key[0], key[1], weight=lambda a, b, _: self.allowed[b].getLength() / max(0.1, self.allowed[b].getSpeed()))):
                                if i >= 40:
                                    break
                                if pf.is_legal_sequence(candidate):
                                    alt = candidate
                                    break
                            path = alt
                        route_cache[key] = list(path) if path else []
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        route_cache[key] = []
                path = route_cache[key]
                if path:
                    break
            else:
                raise RuntimeError(f"could not draw reachable background OD for trip {index}")
            depart = (index + rng.random()) * (3600.0 / self.traffic_rate_per_hour)
            trips.append({
                "vehicle_id": f"bg_s{seed}_{index:04d}",
                "origin_point_id": origin["point_id"],
                "destination_point_id": destination["point_id"],
                "start_edge_id": origin["binding"]["edge_id"],
                "start_pos_m": round(float(origin["binding"]["edge_pos_m"]), 3),
                "end_edge_id": destination["binding"]["edge_id"],
                "end_pos_m": round(float(destination["binding"]["edge_pos_m"]), 3),
                "depart_time_s": round(depart, 3),
                "route_edges": path,
            })
        return trips

    def _launch_background(self, trips: list[dict[str, Any]]) -> None:
        for item in trips:
            route_id = f"route_{item['vehicle_id']}"
            traci.route.add(route_id, item["route_edges"])
            traci.vehicle.add(
                item["vehicle_id"], route_id,
                depart=f"{item['depart_time_s']:.3f}",
                departLane="best",
                departPos=f"{item['start_pos_m']:.3f}",
                departSpeed="max",
                arrivalLane="current",
                arrivalPos=f"{item['end_pos_m']:.3f}",
                arrivalSpeed="current",
            )

    def _plan_order(self, ready_time: float, vehicle_id: str) -> None:
        index = self.next_index[vehicle_id]
        order = self.orders_by_vehicle[vehicle_id][index]
        from_point = self.current_point[vehicle_id]
        pickup_id, dropoff_id = order["pickup"]["point_id"], order["dropoff"]["point_id"]
        now = float(traci.simulation.getTime()) + self.pending_planner_s
        traffic = self._traffic_snapshot()
        tls_snapshot = self.router._tls_snapshot()
        snapshot_hash = canonical_json_hash({"traffic": traffic, "tls": tls_snapshot, "sim_time": now})

        started = time.perf_counter()
        first = self.router.route(self.match[from_point][0], self.match[pickup_id][0], self.objective, now, traffic, tls_snapshot)
        if first["status"] != "ok":
            raise RuntimeError(f"R1 route failed for {order['order_id']} deadhead")
        second = self.router.route(self.match[pickup_id][0], self.match[dropoff_id][0], self.objective, now, traffic, tls_snapshot, start_time_offset_s=float(first["predicted_travel_time_s"]))
        if second["status"] != "ok":
            raise RuntimeError(f"R1 route failed for {order['order_id']} passenger leg")
        planner_wall = time.perf_counter() - started
        if order["order_id"] not in self.planning_latency_trace:
            raise RuntimeError(f"planning latency trace missing order {order['order_id']}")
        planning_delay_applied = float(self.planning_latency_trace[order["order_id"]])
        order["_planned_passenger"] = second
        self.route_rows.extend([
            {"order_id": order["order_id"], "logical_vehicle_id": vehicle_id, "leg_role": "vehicle_to_pickup", **first},
            {"order_id": order["order_id"], "logical_vehicle_id": vehicle_id, "leg_role": "pickup_to_dropoff", **second},
        ])
        plan_record = {
            "vehicle_id": vehicle_id,
            "order_id": order["order_id"],
            "sequence_index": order["sequence_index"],
            "planning_ready_time_s": round(ready_time, 3),
            "plan_start_sim_time_s": round(now, 3),
            "planner_wall_time_s": round(planner_wall, 6),
            "planning_delay_applied_s": round(planning_delay_applied, 6),
            "planning_latency_trace_sha256": self.planning_latency_trace_sha256,
            "objective": self.objective,
            "traffic_state_hash": snapshot_hash,
            "active_vehicle_count": traffic["active_vehicle_count"],
            "occupied_edge_count": traffic["occupied_edge_count"],
            "halting_vehicle_count": traffic["halting_vehicle_count"],
            "deadhead_edges": first["path"],
            "deadhead_predicted_time_s": round(first["predicted_travel_time_s"], 3),
            "deadhead_predicted_energy_wh": round(first["predicted_energy_wh"], 4),
            "passenger_edges": second["path"],
            "passenger_predicted_time_s": round(second["predicted_travel_time_s"], 3),
            "passenger_predicted_energy_wh": round(second["predicted_energy_wh"], 4),
            "predicted_signal_wait_s": round(first["signal_wait_s"] + second["signal_wait_s"], 3),
            "signal_controller_count_in_snapshot": len(tls_snapshot),
        }
        self.plan_rows.append(plan_record)
        self._advance_planner_time(planning_delay_applied)
        self._add_experiment_leg(vehicle_id, order, "vehicle_to_pickup", first, from_point, pickup_id)

    def _start_sumo(self, background: list[dict[str, Any]]) -> None:
        sumo_bin = SUMO_BIN if SUMO_BIN.is_file() else Path("/Users/didi/Library/Application Support/Porto SUMO/1.28.0/Versions/1.28.0/EclipseSUMO/bin/sumo")
        args = [
            str(sumo_bin),
            "--net-file", str(NETWORK),
            "--route-files", str(self.types_file),
            "--step-length", str(DT),
            "--no-step-log", "true",
            "--duration-log.disable", "true",
            "--time-to-teleport", "-1",
            "--seed", str(self.seed),
            "--tripinfo-output", str(self.output_dir / "tripinfo.xml"),
            "--tripinfo-output.write-unfinished", "true",
            "--device.battery.probability", "1",
            "--start",
        ]
        traci.start(args, label=f"porto_r1_{self.seed}_{self.objective.lower()}_{time.time_ns()}", verbose=False)
        self.sim_start = float(traci.simulation.getTime())
        self._launch_background(background)

    def run(self) -> dict[str, Any]:
        background = self._background_trips(self.seed)
        self.background_vehicle_count = len(background)
        write_json(self.output_dir / "background_demand.json", {
            "source": "synthetic traffic drawn from the fixed 30 heat-weighted candidate cells",
            "traffic_rate_per_hour": self.traffic_rate_per_hour,
            "scheduled_hours": self.traffic_hours,
            "vehicles": background,
            "warning": "flow rate is a frozen simulation assumption, not measured Porto vehicle volume",
        })
        self._start_sumo(background)
        self.run_start_wall = time.perf_counter()
        max_sim_time = 24 * 3600
        try:
            while self.completed_orders < len(self.orders_file["orders"]):
                if self.ready:
                    ready_time, vehicle_id = heapq.heappop(self.ready)
                    if self.next_index[vehicle_id] >= len(self.orders_by_vehicle[vehicle_id]):
                        continue
                    self._plan_order(ready_time, vehicle_id)
                else:
                    if float(traci.simulation.getTime()) >= max_sim_time:
                        raise RuntimeError("SUMO run exceeded 24 simulated hours")
                    self._simulation_tick()
            sim_finish = float(traci.simulation.getTime())
            self._track_vehicle_edges()
        finally:
            traci.close()
        # SUMO finalizes XML output roots when TraCI closes; parse only afterward.
        result = self._summarize(sim_finish)
        return result

    def _summarize(self, sim_finish: float) -> dict[str, Any]:
        tripinfo_path = self.output_dir / "tripinfo.xml"
        root = ET.parse(tripinfo_path).getroot()
        trip_info = {x.get("id", ""): x for x in root.findall("tripinfo")}
        experimental_ids = set(self.trip_meta)
        trip_rows = []
        net_energy = gross_energy = regen_energy = total_distance = total_operating_s = total_wait_s = 0.0
        missing_tripinfo = []
        for trip_id, meta in self.trip_meta.items():
            if meta.get("actual_noop"):
                trip_rows.append({**meta, "trip_id": trip_id, "actual_duration_s": 0.0, "actual_distance_m": 0.0, "actual_energy_gross_wh": 0.0, "actual_energy_regenerated_wh": 0.0, "actual_energy_net_wh": 0.0, "status": "zero_distance_completed"})
                continue
            item = trip_info.get(trip_id)
            if item is None:
                missing_tripinfo.append(trip_id)
                continue
            battery = item.find("battery")
            gross = float(battery.get("totalEnergyConsumed", 0.0)) if battery is not None else 0.0
            regen = float(battery.get("totalEnergyRegenerated", 0.0)) if battery is not None else 0.0
            net = gross - regen
            duration = float(item.get("duration", 0.0))
            distance = float(item.get("routeLength", 0.0))
            waiting = float(item.get("waitingTime", 0.0))
            gross_energy += gross
            regen_energy += regen
            net_energy += net
            total_distance += distance
            total_operating_s += duration
            total_wait_s += waiting
            trip_rows.append({
                **meta,
                "trip_id": trip_id,
                "actual_duration_s": duration,
                "actual_distance_m": distance,
                "actual_waiting_s": waiting,
                "actual_time_loss_s": float(item.get("timeLoss", 0.0)),
                "actual_energy_gross_wh": gross,
                "actual_energy_regenerated_wh": regen,
                "actual_energy_net_wh": net,
                "status": "completed",
            })
        background_trip_infos = [item for trip_id, item in trip_info.items() if trip_id.startswith("bg_s")]
        background_arrived = sum(1 for item in background_trip_infos if item.get("arrival") not in (None, "", "-1"))
        if missing_tripinfo:
            raise RuntimeError(f"SUMO missing tripinfo for {len(missing_tripinfo)} experimental trips: {missing_tripinfo[:5]}")

        order_csv = self.output_dir / "order_execution.csv"
        if self.order_rows:
            with order_csv.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(self.order_rows[0]))
                writer.writeheader(); writer.writerows(self.order_rows)
        legs_path = self.output_dir / "route_legs_actual.json"
        write_json(legs_path, trip_rows)
        plan_path = self.output_dir / "route_plan_log.jsonl"
        plan_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.plan_rows), encoding="utf-8")
        pass_path = self.output_dir / "edge_pass_times.csv"
        if self.edge_pass_rows:
            with pass_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(self.edge_pass_rows[0]))
                writer.writeheader(); writer.writerows(self.edge_pass_rows)

        sim_elapsed = sim_finish - self.sim_start
        physical_elapsed = sim_elapsed + self.pending_planner_s
        summary = {
            "scenario_id": self.orders_file["scenario_id"],
            "status": "completed" if self.completed_orders == 100 else "incomplete",
            "method": "R1_TD_DIJKSTRA",
            "objective": self.objective,
            "seed": self.seed,
            "frozen_order_sha256": sha256(self.inputs["orders_path"]),
            "binding_sha256": sha256(self.inputs["binding_path"]),
            "network_sha256": sha256(NETWORK),
            "fleet_size": len(self.orders_file["vehicles"]),
            "orders_completed": self.completed_orders,
            "expected_orders": len(self.orders_file["orders"]),
            "route_requests": len(self.plan_rows),
            "route_legs_planned": len(self.route_rows),
            "zero_distance_legs": self.noop_leg_count,
            "all_vehicle_sequences_fixed": True,
            "dispatch_or_reassignment": False,
            "traffic_simulated": True,
            "sumo_version": "1.28.0",
            "experiment_protocol_sha256": sha256(PROTOCOL_FILE),
            "planning_latency_mode": "frozen_measured_per_order_replay",
            "planning_latency_trace_sha256": self.planning_latency_trace_sha256,
            "simulation_step_s": DT,
            "traffic_rate_per_hour_synthetic": self.traffic_rate_per_hour,
            "traffic_duration_hours_synthetic": self.traffic_hours,
            "synthetic_background_vehicles_scheduled": self.background_vehicle_count,
            "synthetic_background_vehicles_departed_by_finish": len(background_trip_infos),
            "synthetic_background_vehicles_arrived_by_finish": background_arrived,
            "elapsed_completion_time_s": round(physical_elapsed, 3),
            "sum_route_planning_wall_time_s": round(sum(float(p["planner_wall_time_s"]) for p in self.plan_rows), 6),
            "sum_planning_delay_applied_s": round(self.planning_total_s, 6),
            "sum_experimental_vehicle_operating_time_s": round(total_operating_s, 3),
            "sum_actual_waiting_time_s": round(total_wait_s, 3),
            "sum_actual_distance_km": round(total_distance / 1000.0, 3),
            "sum_actual_energy_gross_kwh": round(gross_energy / 1000.0, 5),
            "sum_actual_energy_regenerated_kwh": round(regen_energy / 1000.0, 5),
            "sum_actual_energy_net_kwh": round(net_energy / 1000.0, 5),
            "energy_model": "SUMO Energy/unknown with frozen Kia Soul EV 2020 proxy parameter set; not Porto vehicle calibration",
            "energy_vehicle_parameters": EV_PARAMS,
            "simultaneous_fleet_limit": len(self.orders_file["vehicles"]),
            "vehicle_representation": "one logical fixed vehicle per vehicle_id; SUMO moving vehicle IDs are per leg and never overlap legs for the same logical vehicle",
            "route_plan_count": len(self.plan_rows),
            "edge_pass_events": len(self.edge_pass_rows),
            "mean_planner_wall_time_ms": round(statistics.mean([p["planner_wall_time_s"] for p in self.plan_rows]) * 1000.0, 3) if self.plan_rows else 0,
            "maximum_active_traffic_vehicles_seen_in_planning_snapshot": max([p["active_vehicle_count"] for p in self.plan_rows], default=0),
            "maximum_halting_traffic_vehicles_seen_in_planning_snapshot": max([p["halting_vehicle_count"] for p in self.plan_rows], default=0),
            "total_predicted_signal_wait_s": round(sum(p["predicted_signal_wait_s"] for p in self.plan_rows), 3),
            "route_leg_artifact": legs_path.name,
            "edge_pass_artifact": pass_path.name,
            "route_plan_artifact": plan_path.name,
            "wall_execution_seconds": round(time.perf_counter() - self.run_start_wall, 3),
            "evidence_scope": "dynamic SUMO experiment for one objective/seed; simulated traffic and energy only",
        }
        write_json(self.output_dir / "summary.json", summary)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True, choices=range(10))
    parser.add_argument("--objective", choices=("FASTEST", "LOWEST_ENERGY"), default="FASTEST")
    parser.add_argument("--traffic-rate-per-hour", type=int, default=90)
    parser.add_argument("--traffic-hours", type=float, default=2.0)
    parser.add_argument("--planning-latency-trace", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.traffic_rate_per_hour <= 0 or args.traffic_hours <= 0:
        parser.error("traffic rate and hours must be positive")
    if not args.planning_latency_trace.is_file():
        parser.error("planning latency trace must exist")
    trace_payload = json.loads(args.planning_latency_trace.read_text(encoding="utf-8"))
    if trace_payload.get("seed") != args.seed or trace_payload.get("objective") != args.objective:
        parser.error("planning latency trace seed/objective does not match this run")
    trace_rows = trace_payload.get("requests", [])
    planning_latency_trace = {str(row["order_id"]): float(row["planning_delay_s"]) for row in trace_rows}
    if len(planning_latency_trace) != 100 or any(value < 0 for value in planning_latency_trace.values()):
        parser.error("planning latency trace must provide 100 nonnegative per-order delays")
    out = args.output_dir or PACKAGE / "derived/experiments/r1_dynamic_sumo_v1" / f"seed_{args.seed}" / args.objective.lower()
    output = out
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"refusing to overwrite existing run output: {output}")
    runner = SimulationRun(
        args.seed,
        args.objective,
        output,
        args.traffic_rate_per_hour,
        args.traffic_hours,
        planning_latency_trace,
        sha256(args.planning_latency_trace),
    )
    result = runner.run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
