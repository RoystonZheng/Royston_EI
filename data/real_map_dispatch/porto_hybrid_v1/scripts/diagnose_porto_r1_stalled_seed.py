#!/usr/bin/env python3
"""Diagnostic replay for an incomplete formal_v2 seed; never used for metrics."""

from __future__ import annotations

import csv
import importlib
import sys
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parents[1]
RUNNER_DIR = PACKAGE / "scripts"
sys.path.insert(0, str(RUNNER_DIR))
runner = importlib.import_module("run_porto_r1_dynamic_sumo")


class DiagnosticRun(runner.SimulationRun):
    def __init__(self, seed: int, objective: str, output_dir: Path, traffic_rate_per_hour: int, traffic_hours: float):
        super().__init__(seed, objective, output_dir, traffic_rate_per_hour, traffic_hours)
        self.diagnostic_path = self.output_dir / "active_vehicle_positions_60s.csv"
        self.next_diagnostic_time_s = 1.0
        self.active_vehicle_ids: set[str] = set()
        with self.diagnostic_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                "sim_time_s", "trip_id", "logical_vehicle_id", "order_id", "leg_role",
                "road_id", "lane_id", "lane_position_m", "speed_mps", "x", "y",
                "route_index", "next_edge_id", "next_tls_id", "next_tls_link_index",
                "next_tls_distance_m", "next_tls_state",
            ])
            writer.writeheader()

    def _simulation_tick(self) -> None:
        super()._simulation_tick()
        now = float(runner.traci.simulation.getTime())
        if now + 1e-9 < self.next_diagnostic_time_s:
            return
        self.next_diagnostic_time_s += 60.0
        active = set(runner.traci.vehicle.getIDList())
        records: list[dict[str, Any]] = []
        for trip_id, meta in self.active_trip_meta.items():
            if trip_id not in active:
                continue
            try:
                road_id = runner.traci.vehicle.getRoadID(trip_id)
                lane_id = runner.traci.vehicle.getLaneID(trip_id)
                lane_pos = float(runner.traci.vehicle.getLanePosition(trip_id))
                speed = float(runner.traci.vehicle.getSpeed(trip_id))
                x, y = runner.traci.vehicle.getPosition(trip_id)
                route = runner.traci.vehicle.getRoute(trip_id)
                route_index = int(runner.traci.vehicle.getRouteIndex(trip_id))
                next_edge = route[route_index + 1] if route_index + 1 < len(route) else ""
                next_tls = runner.traci.vehicle.getNextTLS(trip_id)
                tls = next_tls[0] if next_tls else ("", -1, None, "")
                records.append({
                    "sim_time_s": round(now, 1),
                    "trip_id": trip_id,
                    "logical_vehicle_id": meta.get("logical_vehicle_id", ""),
                    "order_id": meta.get("order_id", ""),
                    "leg_role": meta.get("leg_role", ""),
                    "road_id": road_id,
                    "lane_id": lane_id,
                    "lane_position_m": round(lane_pos, 3),
                    "speed_mps": round(speed, 3),
                    "x": round(float(x), 3),
                    "y": round(float(y), 3),
                    "route_index": route_index,
                    "next_edge_id": next_edge,
                    "next_tls_id": tls[0],
                    "next_tls_link_index": tls[1],
                    "next_tls_distance_m": round(float(tls[2]), 3) if tls[2] is not None else "",
                    "next_tls_state": tls[3],
                })
            except runner.traci.TraCIException:
                continue
        if records:
            with self.diagnostic_path.open("a", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(records[0]))
                writer.writerows(records)


def main() -> int:
    runner.SimulationRun = DiagnosticRun
    return runner.main()


if __name__ == "__main__":
    raise SystemExit(main())
