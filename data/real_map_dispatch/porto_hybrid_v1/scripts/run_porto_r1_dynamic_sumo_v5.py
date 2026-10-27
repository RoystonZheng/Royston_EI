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

_PROJECT_ROOT = next(
    (parent for parent in Path(__file__).resolve().parents if (parent / "route_experiment" / "energy_model.py").is_file()),
    None,
)
if _PROJECT_ROOT is None:
    raise RuntimeError("shared_ev_energy_model_not_found")
import sys
sys.path.insert(0, str(_PROJECT_ROOT))
from route_experiment.energy_model import EnergyModelConfig, MotionStep, evaluate_motion_trace, model_metadata

import preflight_fixed_orders_sumo_v2 as pf
from strict_passenger_graph import build_passenger_graph


PACKAGE = Path(__file__).resolve().parents[1]
SCENARIO_DIR = PACKAGE / "derived/scenarios/v3_3"
FROZEN_DIR = SCENARIO_DIR / "frozen_inputs_v1_3"
NETWORK = PACKAGE / "derived/sumo/v1.28.0/porto.net.xml"
PROTOCOL_FILE = PACKAGE / "derived/experiments/protocols/r1_dynamic_sumo_protocol_v5_energy_v2.json"
SUMO_BIN = Path(os.environ.get("PORTO_SUMO_BIN", ""))
DT = 0.1
FORBIDDEN_WAY_SEQUENCES = tuple(tuple(x) for x in pf.FORBIDDEN_WAY_SEQUENCES)

ENERGY_PROFILE = EnergyModelConfig()
EV_PARAMS = {
    "type_id": "GENERIC_REFERENCE_EV_V1",
    "minGap": "2.50",
    "maxSpeed": "29.06",
    "accel": str(ENERGY_PROFILE.max_acceleration_mps2),
    "decel": str(ENERGY_PROFILE.max_deceleration_mps2),
    "sigma": "0.0",
    "mass_kg": ENERGY_PROFILE.curb_mass_kg,
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
        )
    )
    path.write_text(f"<routes><vType {attrs} vClass=\"passenger\" mass=\"{EV_PARAMS['mass_kg']}\" length=\"4.5\"/></routes>\n", encoding="utf-8")


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

    def _edge_snapshot_travel_time(self, edge_id: str, distance_m: float, traffic: dict[str, Any]) -> float:
        """Return the frozen per-query edge time without doing unused energy work."""
        if distance_m <= 0.001:
            return 0.0
        edge = self.allowed[edge_id]
        limit = max(1.0, float(edge.getSpeed()))
        state = traffic["edge_state"].get(edge_id)
        if state:
            count = float(state["vehicle_count"])
            halted = float(state["halting_count"])
            observed = float(state["mean_speed_mps"])
            blended = 0.7 * limit + 0.3 * max(0.0, observed)
            capacity = max(1.0, edge.getLength() * max(1, len(edge.getLanes())) / 35.0)
            density_penalty = max(0.0, count - capacity) * 1.5
            speed = max(0.35 * limit, min(limit, blended / (1.0 + 0.18 * max(0.0, count - 1.0))))
            queue_penalty = 2.5 * halted + density_penalty
        else:
            speed = limit
            queue_penalty = 0.0
        return distance_m / max(0.1, speed) + queue_penalty

    @staticmethod
    def _kinetic_potential_kwh(speed_mps: float, payload_mass_kg: float = 0.0) -> float:
        effective_mass = (
            ENERGY_PROFILE.curb_mass_kg
            + payload_mass_kg
            + ENERGY_PROFILE.rotating_mass_kg
        )
        return (
            0.5 * effective_mass * speed_mps * speed_mps
            / ENERGY_PROFILE.propulsion_efficiency / 3_600_000.0
        )

    @staticmethod
    def _append_motion_phase(
        steps: list[MotionStep], distance_m: float, speed_start_mps: float,
        speed_end_mps: float, acceleration_mps2: float,
    ) -> tuple[float, float]:
        """Append a constant-acceleration phase in <=5 m intervals."""
        if distance_m <= 1e-9:
            return speed_start_mps, 0.0
        pieces = max(1, int(math.ceil(distance_m / 5.0)))
        dx = distance_m / pieces
        sign = 1.0 if speed_end_mps >= speed_start_mps else -1.0
        accel = abs(acceleration_mps2)
        duration = 0.0
        current_speed = speed_start_mps
        for index in range(pieces):
            ds = dx if index < pieces - 1 else distance_m - dx * (pieces - 1)
            next_speed = math.sqrt(max(0.0, current_speed * current_speed + 2.0 * sign * accel * ds))
            if index == pieces - 1:
                next_speed = speed_end_mps
            if current_speed + next_speed <= 1e-9:
                raise ValueError("zero_speed_motion_phase")
            dt = 2.0 * ds / (current_speed + next_speed)
            steps.append(MotionStep(dt, current_speed, next_speed, slope_rad=None))
            duration += dt
            current_speed = next_speed
        return current_speed, duration

    @classmethod
    def _continuous_edge_energy(
        cls, distance_m: float, speed_cap_mps: float,
        speed_start_mps: float, speed_end_mps: float,
        payload_mass_kg: float, idle_seconds: float = 0.0,
    ) -> tuple[float, float] | None:
        """Estimate signed EV v2 energy over one continuous edge segment.

        The boundary speeds are shared by adjacent links. Acceleration, cruise,
        and braking are integrated as one continuous speed profile; no edge is
        independently treated as a stop-start trip.
        """
        distance = max(0.0, float(distance_m))
        cap = max(0.0, float(speed_cap_mps))
        v0 = max(0.0, float(speed_start_mps))
        v1 = max(0.0, float(speed_end_mps))
        if idle_seconds < 0 or v0 > cap + 1e-7 or v1 > cap + 1e-7:
            return None
        if distance <= 1e-8:
            if abs(v0 - v1) > 1e-7:
                return None
            result = evaluate_motion_trace(
                [], ENERGY_PROFILE, payload_mass_kg=payload_mass_kg,
                idle_durations_sec=(idle_seconds,),
                trace_fidelity="estimated_from_snapshot_continuous_edge_v1",
            )
            return result.net_kwh, result.drive_time_sec + idle_seconds
        if cap <= 1e-8:
            return None
        accel = ENERGY_PROFILE.max_acceleration_mps2
        decel = ENERGY_PROFILE.max_deceleration_mps2
        if accel <= 0 or decel <= 0:
            raise ValueError("invalid_ev_acceleration_limits")
        minimum_transition_distance = (
            (v1 * v1 - v0 * v0) / (2.0 * accel)
            if v1 >= v0
            else (v0 * v0 - v1 * v1) / (2.0 * decel)
        )
        if minimum_transition_distance > distance + 1e-7:
            return None
        accel_to_cap = max(0.0, (cap * cap - v0 * v0) / (2.0 * accel))
        decel_from_cap = max(0.0, (cap * cap - v1 * v1) / (2.0 * decel))
        if accel_to_cap + decel_from_cap <= distance:
            peak = cap
        else:
            peak_sq = (
                2.0 * distance + v0 * v0 / accel + v1 * v1 / decel
            ) / (1.0 / accel + 1.0 / decel)
            peak = math.sqrt(max(v0 * v0, v1 * v1, peak_sq))
            if peak > cap + 1e-7:
                return None
        accel_distance = max(0.0, (peak * peak - v0 * v0) / (2.0 * accel))
        decel_distance = max(0.0, (peak * peak - v1 * v1) / (2.0 * decel))
        cruise_distance = max(0.0, distance - accel_distance - decel_distance)
        steps: list[MotionStep] = []
        duration = 0.0
        if accel_distance > 1e-8:
            _v, dt = cls._append_motion_phase(steps, accel_distance, v0, peak, accel)
            duration += dt
        if cruise_distance > 1e-8:
            dt = cruise_distance / max(1e-6, peak)
            steps.append(MotionStep(dt, peak, peak, slope_rad=None, acceleration_mps2=0.0))
            duration += dt
        if decel_distance > 1e-8:
            _v, dt = cls._append_motion_phase(steps, decel_distance, peak, v1, decel)
            duration += dt
        result = evaluate_motion_trace(
            steps, ENERGY_PROFILE, payload_mass_kg=payload_mass_kg,
            idle_durations_sec=(idle_seconds,),
            trace_fidelity="estimated_from_snapshot_continuous_edge_v1",
        )
        return result.net_kwh, duration + idle_seconds

    def _snapshot_edge_state(
        self, edge_id: str, traffic: dict[str, Any],
    ) -> tuple[float, float]:
        edge = self.allowed[edge_id]
        limit = max(1.0, float(edge.getSpeed()))
        state = traffic["edge_state"].get(edge_id)
        if not state:
            return limit, 0.0
        count = float(state["vehicle_count"])
        halted = float(state["halting_count"])
        observed = float(state["mean_speed_mps"])
        blended = 0.7 * limit + 0.3 * max(0.0, observed)
        capacity = max(1.0, edge.getLength() * max(1, len(edge.getLanes())) / 35.0)
        density_penalty = max(0.0, count - capacity) * 1.5
        speed = max(0.35 * limit, min(limit, blended / (1.0 + 0.18 * max(0.0, count - 1.0))))
        return speed, 2.5 * halted + density_penalty

    def _snapshot_turn_wait(
        self, from_edge: str, to_edge: str, now: float,
        tls_snapshot: dict[str, Any],
    ) -> float:
        return self._turn_wait(from_edge, to_edge, now, now, tls_snapshot)

    def _entry_boundary_speed(
        self, from_edge: str, to_edge: str, now: float,
        traffic: dict[str, Any], tls_snapshot: dict[str, Any],
        from_distance_m: float | None = None, to_distance_m: float | None = None,
    ) -> float:
        from_speed, from_queue = self._snapshot_edge_state(from_edge, traffic)
        to_speed, to_queue = self._snapshot_edge_state(to_edge, traffic)
        turn_wait = self._snapshot_turn_wait(from_edge, to_edge, now, tls_snapshot)
        if from_queue > 1e-9 or to_queue > 1e-9 or turn_wait > 1e-9:
            return 0.0
        from_distance = (
            float(from_distance_m) if from_distance_m is not None
            else float(self.allowed[from_edge].getLength())
        )
        to_distance = (
            float(to_distance_m) if to_distance_m is not None
            else float(self.allowed[to_edge].getLength())
        )
        # Bound the shared boundary speed so either adjacent link can reach it
        # from rest and brake back to rest within its available segment. This
        # keeps short endpoint clips and short urban links physically feasible.
        transition_distance = max(0.0, min(from_distance, to_distance))
        accel_bound = math.sqrt(2.0 * ENERGY_PROFILE.max_acceleration_mps2 * transition_distance)
        decel_bound = math.sqrt(2.0 * ENERGY_PROFILE.max_deceleration_mps2 * transition_distance)
        return min(from_speed, to_speed, accel_bound, decel_bound)

    def _route_signed_net_energy(
        self, from_match: dict[str, Any], to_match: dict[str, Any], now: float,
        traffic: dict[str, Any], tls_snapshot: dict[str, Any],
        start_time_offset_s: float = 0.0, payload_mass_kg: float = 0.0,
    ) -> dict[str, Any]:
        """Dijkstra over adjacent-link boundary-speed states.

        EV v2 signed-net energy can be negative on a braking segment. Reweight
        each edge transition by the kinetic-energy potential K/eta. For the
        flat-grade model, all reduced edge costs are nonnegative, while the
        potential telescopes to zero for a stopped vehicle-to-stopped vehicle
        route. Therefore Dijkstra minimizes the original signed-net energy.
        Traffic and signal weights are frozen at this route's live snapshot.
        """
        source, target = str(from_match["edge_id"]), str(to_match["edge_id"])
        start_pos, end_pos = float(from_match["edge_pos_m"]), float(to_match["edge_pos_m"])
        now_s = float(now + start_time_offset_s)
        if source == target and end_pos >= start_pos:
            distance = end_pos - start_pos
            speed, queue = self._snapshot_edge_state(source, traffic)
            motion = self._continuous_edge_energy(distance, speed, 0.0, 0.0, payload_mass_kg, queue)
            if motion is None:
                return {"status": "unreachable", "path": [], "predicted_travel_time_s": None, "predicted_energy_wh": None, "signal_wait_s": 0.0, "distance_m": 0.0, "expanded_states": 0}
            return {"status": "ok", "path": [source], "predicted_travel_time_s": motion[1], "predicted_energy_wh": motion[0] * 1000.0, "signal_wait_s": 0.0, "distance_m": distance, "expanded_states": 0}

        if source not in self.graph or target not in self.graph:
            return {"status": "unreachable", "path": [], "predicted_travel_time_s": None, "predicted_energy_wh": None, "signal_wait_s": 0.0, "distance_m": 0.0, "expanded_states": 0}

        def potential(speed: float) -> float:
            return self._kinetic_potential_kwh(speed, payload_mass_kg)

        def idle_after(edge_id: str, next_edge: str) -> tuple[float, float]:
            _speed, queue = self._snapshot_edge_state(edge_id, traffic)
            signal = self._snapshot_turn_wait(edge_id, next_edge, now_s, tls_snapshot)
            return queue + signal, signal

        start_way = pf.osm_way_id(source)
        initial_suffix = (start_way,)
        heap: list[tuple[float, float, tuple[Any, ...], tuple[str, ...], float]] = []
        best: dict[tuple[Any, ...], tuple[float, float]] = {}
        expanded = 0
        start_edge = self.allowed[source]
        initial_distance = max(0.0, float(start_edge.getLength()) - start_pos)
        for first_next in self.graph.successors(source):
            first_next = str(first_next)
            suffix = self._append_way_suffix(initial_suffix, first_next)
            if suffix is None:
                continue
            out_speed = self._entry_boundary_speed(
                source, first_next, now_s, traffic, tls_snapshot,
                from_distance_m=initial_distance,
            )
            idle, signal_wait = idle_after(source, first_next)
            speed, _queue = self._snapshot_edge_state(source, traffic)
            source_motion = self._continuous_edge_energy(initial_distance, speed, 0.0, out_speed, payload_mass_kg, idle)
            if source_motion is None:
                continue
            reduced = source_motion[0] + potential(0.0) - potential(out_speed)
            if reduced < -1e-8:
                raise ValueError(f"negative_energy_reweighted_source_arc:{source}:{first_next}:{reduced}")
            reduced = max(0.0, reduced)
            state = (source, first_next, suffix, True)
            elapsed = source_motion[1]
            prior = best.get(state)
            if prior is None or (reduced, elapsed) < prior:
                best[state] = (reduced, elapsed)
                heapq.heappush(heap, (reduced, elapsed, state, (source, first_next), signal_wait))

        final_cost = math.inf
        final_value: tuple[tuple[Any, ...], tuple[str, ...], float, float, float] | None = None
        while heap:
            reduced_cost, elapsed, state, path, signal_sum = heapq.heappop(heap)
            if reduced_cost > final_cost + 1e-10:
                break
            if best.get(state) != (reduced_cost, elapsed):
                continue
            previous, edge_id, suffix, left_source = state
            expanded += 1
            if edge_id == target and (source != target or left_source):
                entry_speed = self._entry_boundary_speed(
                    previous, edge_id, now_s, traffic, tls_snapshot,
                    from_distance_m=initial_distance if previous == source else None,
                    to_distance_m=end_pos if edge_id == target else None,
                )
                speed, _queue = self._snapshot_edge_state(edge_id, traffic)
                target_motion = self._continuous_edge_energy(end_pos, speed, entry_speed, 0.0, payload_mass_kg)
                if target_motion is not None:
                    terminal_reduced = target_motion[0] + potential(entry_speed) - potential(0.0)
                    if terminal_reduced < -1e-8:
                        raise ValueError(f"negative_energy_reweighted_target_arc:{previous}:{edge_id}:{terminal_reduced}")
                    total_reduced = reduced_cost + max(0.0, terminal_reduced)
                    total_time = elapsed + target_motion[1]
                    if (total_reduced, total_time) < (final_cost, math.inf if final_value is None else final_value[2]):
                        final_cost = total_reduced
                        final_value = (state, path, total_time, signal_sum, total_reduced)
                continue

            for next_edge in self.graph.successors(edge_id):
                next_edge = str(next_edge)
                new_suffix = self._append_way_suffix(suffix, next_edge)
                if new_suffix is None:
                    continue
                entry_speed = self._entry_boundary_speed(
                    previous, edge_id, now_s, traffic, tls_snapshot,
                    from_distance_m=initial_distance if previous == source else None,
                    to_distance_m=end_pos if edge_id == target else None,
                )
                exit_speed = self._entry_boundary_speed(
                    edge_id, next_edge, now_s, traffic, tls_snapshot,
                    to_distance_m=end_pos if next_edge == target else None,
                )
                idle, turn_signal_wait = idle_after(edge_id, next_edge)
                edge_speed, _edge_queue = self._snapshot_edge_state(edge_id, traffic)
                edge_motion = self._continuous_edge_energy(
                    float(self.allowed[edge_id].getLength()), edge_speed,
                    entry_speed, exit_speed, payload_mass_kg, idle,
                )
                if edge_motion is None:
                    continue
                arc_reduced = edge_motion[0] + potential(entry_speed) - potential(exit_speed)
                if arc_reduced < -1e-8:
                    raise ValueError(f"negative_energy_reweighted_arc:{previous}:{edge_id}:{next_edge}:{arc_reduced}")
                candidate_cost = reduced_cost + max(0.0, arc_reduced)
                candidate_time = elapsed + edge_motion[1]
                candidate_state = (edge_id, next_edge, new_suffix, True)
                old = best.get(candidate_state)
                if old is not None and (candidate_cost > old[0] + 1e-10 or (abs(candidate_cost - old[0]) <= 1e-10 and candidate_time >= old[1])):
                    continue
                best[candidate_state] = (candidate_cost, candidate_time)
                candidate_path = path + (next_edge,)
                heapq.heappush(heap, (candidate_cost, candidate_time, candidate_state, candidate_path, signal_sum + turn_signal_wait))

        if final_value is None:
            return {"status": "unreachable", "path": [], "predicted_travel_time_s": None, "predicted_energy_wh": None, "signal_wait_s": 0.0, "distance_m": 0.0, "expanded_states": expanded}
        _, best_path, predicted_time, signal_wait, predicted_energy_kwh = final_value
        distance = max(0.0, float(self.allowed[source].getLength()) - start_pos)
        distance += sum(float(self.allowed[edge].getLength()) for edge in best_path[1:-1])
        distance += max(0.0, end_pos)
        return {"status": "ok", "path": list(best_path), "predicted_travel_time_s": predicted_time, "predicted_energy_wh": predicted_energy_kwh * 1000.0, "signal_wait_s": signal_wait, "distance_m": distance, "expanded_states": expanded}

    def _evaluate_path_energy(
        self, path: list[str], from_match: dict[str, Any], to_match: dict[str, Any],
        now: float, traffic: dict[str, Any], tls_snapshot: dict[str, Any],
        start_time_offset_s: float = 0.0, payload_mass_kg: float = 0.0,
    ) -> float:
        """Evaluate one chosen path with a continuous, link-boundary EV v2 trace."""
        if not path:
            raise ValueError("cannot_evaluate_empty_path")
        start_pos = float(from_match["edge_pos_m"])
        end_pos = float(to_match["edge_pos_m"])
        now_s = float(now + start_time_offset_s)
        total_kwh = 0.0
        if len(path) == 1:
            edge = path[0]
            distance = max(0.0, end_pos - start_pos)
            speed, queue = self._snapshot_edge_state(edge, traffic)
            motion = self._continuous_edge_energy(distance, speed, 0.0, 0.0, payload_mass_kg, queue)
            if motion is None:
                raise ValueError(f"infeasible_continuous_energy_trace:{edge}")
            return motion[0]
        for index, edge_id in enumerate(path):
            edge_id = str(edge_id)
            distance = (
                max(0.0, float(self.allowed[edge_id].getLength()) - start_pos)
                if index == 0 else
                max(0.0, end_pos)
                if index == len(path) - 1 else
                float(self.allowed[edge_id].getLength())
            )
            entry_speed = 0.0 if index == 0 else self._entry_boundary_speed(
                str(path[index - 1]), edge_id, now_s, traffic, tls_snapshot,
                from_distance_m=max(0.0, float(self.allowed[str(path[index - 1])].getLength()) - start_pos) if index == 1 else None,
                to_distance_m=end_pos if index == len(path) - 1 else None,
            )
            exit_speed = 0.0 if index == len(path) - 1 else self._entry_boundary_speed(
                edge_id, str(path[index + 1]), now_s, traffic, tls_snapshot,
                from_distance_m=max(0.0, float(self.allowed[edge_id].getLength()) - start_pos) if index == 0 else None,
                to_distance_m=end_pos if index + 1 == len(path) - 1 else None,
            )
            idle = 0.0
            if index < len(path) - 1:
                _speed, queue = self._snapshot_edge_state(edge_id, traffic)
                idle = queue + self._snapshot_turn_wait(
                    edge_id, str(path[index + 1]), now_s, tls_snapshot,
                )
            speed, _queue = self._snapshot_edge_state(edge_id, traffic)
            motion = self._continuous_edge_energy(
                distance, speed, entry_speed, exit_speed, payload_mass_kg, idle,
            )
            if motion is None:
                raise ValueError(f"infeasible_continuous_energy_trace:{edge_id}")
            total_kwh += motion[0]
        return total_kwh

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

    def route(self, from_match: dict[str, Any], to_match: dict[str, Any], objective: str, now: float, traffic: dict[str, Any], tls_snapshot: dict[str, Any], start_time_offset_s: float = 0.0, payload_mass_kg: float = 0.0) -> dict[str, Any]:
        source, target = from_match["edge_id"], to_match["edge_id"]
        start_pos, end_pos = float(from_match["edge_pos_m"]), float(to_match["edge_pos_m"])
        start_eta = now + start_time_offset_s
        if objective == "LOWEST_ENERGY":
            return self._route_signed_net_energy(
                from_match, to_match, now, traffic, tls_snapshot,
                start_time_offset_s=start_time_offset_s,
                payload_mass_kg=payload_mass_kg,
            )
        if source == target and end_pos >= start_pos:
            speed, queue = self._snapshot_edge_state(source, traffic)
            motion = self._continuous_edge_energy(end_pos - start_pos, speed, 0.0, 0.0, payload_mass_kg, queue)
            if motion is None:
                return {"status": "unreachable", "path": [], "predicted_travel_time_s": None, "predicted_energy_wh": None, "signal_wait_s": 0.0, "distance_m": 0.0, "expanded_states": 0}
            travel_s = motion[1]
            energy_wh = motion[0] * 1000.0
            return {"status": "ok", "path": [source], "predicted_travel_time_s": travel_s, "predicted_energy_wh": energy_wh, "signal_wait_s": 0.0, "distance_m": end_pos - start_pos, "expanded_states": 0}

        start_edge = self.allowed[source]
        initial_distance = max(0.0, start_edge.getLength() - start_pos)
        initial_time_s = self._edge_snapshot_travel_time(source, initial_distance, traffic)
        initial_suffix = (pf.osm_way_id(source),)
        start_state = (source, False, initial_suffix)
        # Heap key uses the requested primary objective; elapsed time is the tie breaker.
        first_metric = initial_time_s
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
                total_time = elapsed
                distance = max(0.0, initial_distance) + sum(self.allowed[e].getLength() for e in path[1:-1]) + (end_pos if len(path) > 1 else max(0.0, end_pos - start_pos))
                total_energy = self._evaluate_path_energy(
                    list(path), from_match, to_match, now, traffic, tls_snapshot,
                    start_time_offset_s=start_time_offset_s,
                    payload_mass_kg=payload_mass_kg,
                ) * 1000.0
                return {"status": "ok", "path": list(path), "predicted_travel_time_s": total_time, "predicted_energy_wh": total_energy, "signal_wait_s": signal_sum, "distance_m": distance, "expanded_states": expanded}

            for next_edge in self.graph.successors(edge_id):
                next_edge = str(next_edge)
                new_suffix = self._append_way_suffix(suffix, next_edge)
                if new_suffix is None:
                    continue
                eta = now + start_time_offset_s + elapsed
                wait = self._turn_wait(edge_id, next_edge, eta, now, tls_snapshot)
                distance = end_pos if next_edge == target else self.allowed[next_edge].getLength()
                travel_s = self._edge_snapshot_travel_time(next_edge, distance, traffic)
                candidate_elapsed = elapsed + wait + travel_s
                candidate_metric = candidate_elapsed
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
        self.motion_trace_steps: dict[str, list[MotionStep]] = {}
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
            departSpeed="0",
            arrivalLane="current",
            arrivalPos=f"{float(end_match['edge_pos_m']):.3f}",
            arrivalSpeed="0",
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
        self.motion_trace_steps[trip_id] = []
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
        before_ids = set(traci.vehicle.getIDList())
        before_speeds = {}
        for trip_id in self.active_trip_meta:
            if trip_id in before_ids:
                try:
                    before_speeds[trip_id] = max(0.0, float(traci.vehicle.getSpeed(trip_id)))
                except traci.TraCIException:
                    pass
        traci.simulationStep()
        after_ids = set(traci.vehicle.getIDList())
        arrived_ids = set(traci.simulation.getArrivedIDList())
        for trip_id in list(self.active_trip_meta):
            if trip_id in after_ids:
                try:
                    speed_end = max(0.0, float(traci.vehicle.getSpeed(trip_id)))
                except traci.TraCIException:
                    continue
            elif trip_id in arrived_ids:
                # The frozen SUMO vehicle type explicitly requests zero arrival speed.
                speed_end = 0.0
            else:
                continue
            speed_start = before_speeds.get(trip_id, 0.0)
            self.motion_trace_steps.setdefault(trip_id, []).append(
                MotionStep(duration_sec=DT, speed_start_mps=speed_start, speed_end_mps=speed_end, slope_rad=None)
            )
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
        second = self.router.route(
            self.match[pickup_id][0], self.match[dropoff_id][0], self.objective,
            now, traffic, tls_snapshot,
            start_time_offset_s=float(first["predicted_travel_time_s"]),
            payload_mass_kg=ENERGY_PROFILE.passenger_load_kg,
        )
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
        energy_nonestimable_trip_count = 0
        missing_tripinfo = []
        for trip_id, meta in self.trip_meta.items():
            if meta.get("actual_noop"):
                trip_rows.append({**meta, "trip_id": trip_id, "actual_duration_s": 0.0, "actual_distance_m": 0.0, "actual_energy_gross_wh": 0.0, "actual_energy_regenerated_wh": 0.0, "actual_energy_net_wh": 0.0, "energy_trace_fidelity": "zero_distance_noop", "energy_model_version": ENERGY_PROFILE.model_version, "energy_profile_sha256": ENERGY_PROFILE.fingerprint(), "motion_trace_samples": 0, "status": "zero_distance_completed"})
                continue
            item = trip_info.get(trip_id)
            if item is None:
                missing_tripinfo.append(trip_id)
                continue
            duration = float(item.get("duration", 0.0))
            distance = float(item.get("routeLength", 0.0))
            waiting = float(item.get("waitingTime", 0.0))
            trace_steps = self.motion_trace_steps.get(trip_id, [])
            payload_mass = (
                ENERGY_PROFILE.passenger_load_kg
                if meta.get("leg_role") == "pickup_to_dropoff"
                else 0.0
            )
            if trace_steps:
                energy = evaluate_motion_trace(
                    trace_steps,
                    ENERGY_PROFILE,
                    payload_mass_kg=payload_mass,
                    trace_fidelity="observed_sumo_speed_trace_grade_missing",
                )
            else:
                energy = None
                energy_nonestimable_trip_count += 1
            gross = (energy.propulsion_draw_kwh + energy.auxiliary_kwh) * 1000.0 if energy else None
            regen = energy.regenerated_kwh * 1000.0 if energy else None
            net = energy.net_kwh * 1000.0 if energy else None
            if energy:
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
                "actual_energy_propulsion_draw_kwh": energy.propulsion_draw_kwh if energy else None,
                "actual_energy_rolling_wheel_kwh": energy.rolling_wheel_kwh if energy else None,
                "actual_energy_aerodynamic_wheel_kwh": energy.aerodynamic_wheel_kwh if energy else None,
                "actual_energy_grade_wheel_kwh": energy.grade_wheel_kwh if energy else None,
                "actual_energy_acceleration_wheel_kwh": energy.acceleration_wheel_kwh if energy else None,
                "actual_energy_auxiliary_kwh": energy.auxiliary_kwh if energy else None,
                "energy_trace_fidelity": energy.trace_fidelity if energy else "non_estimable_no_motion_trace",
                "energy_model_version": energy.model_version if energy else ENERGY_PROFILE.model_version,
                "energy_profile_sha256": energy.profile_sha256 if energy else ENERGY_PROFILE.fingerprint(),
                "energy_missing_grade_steps": energy.missing_grade_steps if energy else None,
                "motion_trace_samples": len(trace_steps),
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
        trace_path = self.output_dir / "sumo_motion_traces_v2.json"
        write_json(trace_path, {
            "schema_version": "sumo_motion_trace_v2",
            "sampling_interval_sec": DT,
            "source": "TraCI vehicle speed sampled around each SUMO simulation step",
            "slope_source": "unavailable_in_frozen_network; slope is null and model treats it as flat",
            "trips": {
                trip_id: [
                    {
                        "duration_sec": step.duration_sec,
                        "speed_start_mps": step.speed_start_mps,
                        "speed_end_mps": step.speed_end_mps,
                        "slope_rad": step.slope_rad,
                    }
                    for step in steps
                ]
                for trip_id, steps in sorted(self.motion_trace_steps.items())
            },
        })
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
            "sum_actual_energy_gross_kwh": round(gross_energy / 1000.0, 5) if energy_nonestimable_trip_count == 0 else None,
            "sum_actual_energy_regenerated_kwh": round(regen_energy / 1000.0, 5) if energy_nonestimable_trip_count == 0 else None,
            "sum_actual_energy_net_kwh": round(net_energy / 1000.0, 5) if energy_nonestimable_trip_count == 0 else None,
            "sum_estimable_actual_energy_gross_kwh": round(gross_energy / 1000.0, 5),
            "sum_estimable_actual_energy_regenerated_kwh": round(regen_energy / 1000.0, 5),
            "sum_estimable_actual_energy_net_kwh": round(net_energy / 1000.0, 5),
            "energy_nonestimable_trip_count": energy_nonestimable_trip_count,
            "energy_model": "ev_longitudinal_v2 generic reference EV; kWh recomputed from SUMO speed/acceleration trace, not SUMO Energy/unknown battery output",
            "energy_model_profile": model_metadata(ENERGY_PROFILE),
            "energy_trace_artifact": trace_path.name,
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
    out = args.output_dir or PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/formal_v5_energy_v2" / f"seed_{args.seed}" / args.objective.lower()
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
