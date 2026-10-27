#!/usr/bin/env python3
"""Offline-check all frozen R1 OD legs, excluding the already-run sample."""

from __future__ import annotations

import collections
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parents[1]
RUNNER = PACKAGE / "scripts/run_porto_r1_dynamic_sumo_v5.py"
INPUT_ROOT = PACKAGE / "derived/scenarios/v3_3/frozen_inputs_v1_3"
FORMAL_ROOT = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/formal_v5_energy_v2"
SAMPLE_AUDIT = FORMAL_ROOT / "zero_call_route_preflight_v5.json"
OUTPUT = FORMAL_ROOT / "full_endpoint_preflight_v1"
OBJECTIVES = ("FASTEST", "LOWEST_ENERGY")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sample_pair_keys(module: Any) -> set[tuple[int, str, str, str, str, str]]:
    sampled = set()
    for seed in range(10):
        _points, inputs = module.point_bindings(seed)
        orders = inputs["orders"]["orders"]
        legs = []
        for vehicle in inputs["orders"]["vehicles"]:
            sequence = sorted(
                (row for row in orders if row["vehicle_id"] == vehicle["vehicle_id"]),
                key=lambda row: row["sequence_index"],
            )
            current = vehicle["initial_position"]["point_id"]
            for order in sequence:
                legs.append((vehicle["vehicle_id"], order["order_id"], "vehicle_to_pickup", current, order["pickup"]["point_id"]))
                legs.append((vehicle["vehicle_id"], order["order_id"], "pickup_to_dropoff", order["pickup"]["point_id"], order["dropoff"]["point_id"]))
                current = order["dropoff"]["point_id"]
        indexes = sorted({round(i * (len(legs) - 1) / 9) for i in range(10)})
        sampled.update((seed, *legs[index]) for index in indexes)
    return sampled


def main() -> int:
    if OUTPUT.exists():
        raise SystemExit(f"refusing_to_overwrite:{OUTPUT}")
    sample = json.loads(SAMPLE_AUDIT.read_text(encoding="utf-8"))
    if sample.get("status") != "passed" or sample.get("error_count") != 0:
        raise SystemExit("refusing_full_check_sample_preflight_not_passed")
    sys.path.insert(0, str(PACKAGE / "scripts"))
    spec = importlib.util.spec_from_file_location("porto_r1_v5_full_check", RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot_import_frozen_r1_runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if sha256(RUNNER) != sample.get("runner_sha256"):
        raise SystemExit("refusing_full_check_runner_hash_mismatch")
    sample_keys = sample_pair_keys(module)
    if len(sample_keys) != 100:
        raise RuntimeError(f"unexpected_sample_leg_identity_count:{len(sample_keys)}")
    bindings: dict[int, dict[str, Any]] = {}
    all_legs = []
    for seed in range(10):
        _points, inputs = module.point_bindings(seed)
        bindings[seed] = inputs["matches"]
        orders = inputs["orders"]["orders"]
        for vehicle in inputs["orders"]["vehicles"]:
            sequence = sorted(
                (row for row in orders if row["vehicle_id"] == vehicle["vehicle_id"]),
                key=lambda row: row["sequence_index"],
            )
            current = vehicle["initial_position"]["point_id"]
            for order in sequence:
                all_legs.append({"seed": seed, "vehicle_id": vehicle["vehicle_id"], "order_id": order["order_id"], "leg_role": "vehicle_to_pickup", "from_point_id": current, "to_point_id": order["pickup"]["point_id"]})
                all_legs.append({"seed": seed, "vehicle_id": vehicle["vehicle_id"], "order_id": order["order_id"], "leg_role": "pickup_to_dropoff", "from_point_id": order["pickup"]["point_id"], "to_point_id": order["dropoff"]["point_id"]})
                current = order["dropoff"]["point_id"]
    if len(all_legs) != 2000:
        raise RuntimeError(f"unexpected_frozen_leg_count:{len(all_legs)}")
    missing_sample = sample_keys - {(leg["seed"], leg["vehicle_id"], leg["order_id"], leg["leg_role"], leg["from_point_id"], leg["to_point_id"]) for leg in all_legs}
    if missing_sample:
        raise RuntimeError(f"sample_pairs_not_in_frozen_population:{len(missing_sample)}")

    graph, allowed, _core, tls_by_turn, programs = module.network_core(module.sumolib.net.readNet(str(module.NETWORK), withInternal=False))
    router = module.DynamicRouter(graph, allowed, tls_by_turn, programs)
    traffic = {"sim_time_s": 0.0, "occupied_edge_count": 0, "active_vehicle_count": 0, "halting_vehicle_count": 0, "edge_state": {}}
    for edge_id in list(graph.nodes)[::max(1, len(graph) // 80)][:80]:
        edge = allowed[edge_id]
        traffic["edge_state"][edge_id] = {"vehicle_count": 6, "mean_speed_mps": max(0.5, edge.getSpeed() * 0.35), "halting_count": 2}
    traffic.update({"occupied_edge_count": len(traffic["edge_state"]), "active_vehicle_count": 6 * len(traffic["edge_state"]), "halting_vehicle_count": 2 * len(traffic["edge_state"])})
    tls = {tls_id: {"phase": 0, "next_switch_s": 1_000_000.0} for tls_id in programs}

    OUTPUT.mkdir(parents=True)
    rows_path = OUTPUT / "remaining_route_checks.jsonl"
    counts = collections.Counter()
    errors = []
    durations = []
    started = time.perf_counter()
    checked = 0
    with rows_path.open("w", encoding="utf-8") as stream:
        for leg in all_legs:
            key = (leg["seed"], leg["vehicle_id"], leg["order_id"], leg["leg_role"], leg["from_point_id"], leg["to_point_id"])
            if key in sample_keys:
                continue
            for objective in OBJECTIVES:
                query_start = time.perf_counter()
                record = {**leg, "objective": objective}
                try:
                    route = router.route(
                        bindings[leg["seed"]][leg["from_point_id"]][0],
                        bindings[leg["seed"]][leg["to_point_id"]][0],
                        objective,
                        0.0,
                        traffic,
                        tls,
                        payload_mass_kg=module.ENERGY_PROFILE.passenger_load_kg if leg["leg_role"] == "pickup_to_dropoff" else 0.0,
                    )
                    record.update({key: route.get(key) for key in ("status", "distance_m", "predicted_travel_time_s", "predicted_energy_wh", "signal_wait_s", "expanded_states")})
                    path = [str(edge_id) for edge_id in route.get("path", [])]
                    record["path_sha256"] = hashlib.sha256("\n".join(path).encode()).hexdigest() if path else None
                    counts[(objective, route.get("status"))] += 1
                    if route.get("status") != "ok":
                        errors.append({"seed": leg["seed"], "order_id": leg["order_id"], "leg_role": leg["leg_role"], "objective": objective, "status": route.get("status")})
                    else:
                        broken = [(a, b) for a, b in zip(path, path[1:]) if not graph.has_edge(a, b)]
                        if not path or broken or not module.pf.is_legal_sequence(path):
                            record["path_edges_on_error"] = path
                            issue = "empty_path" if not path else "disconnected_path" if broken else "forbidden_way_sequence"
                            errors.append({"seed": leg["seed"], "order_id": leg["order_id"], "leg_role": leg["leg_role"], "objective": objective, "error": issue, "broken_edges": broken[:10]})
                        if objective == "LOWEST_ENERGY" and route.get("predicted_energy_wh") is not None and route["predicted_energy_wh"] < -1e-5:
                            errors.append({"seed": leg["seed"], "order_id": leg["order_id"], "leg_role": leg["leg_role"], "objective": objective, "error": "negative_signed_net_energy_wh", "wh": route["predicted_energy_wh"]})
                except Exception as exc:  # preserve a route-specific error instead of aborting the population audit
                    record.update({"status": "error", "error": f"{type(exc).__name__}:{exc}"})
                    counts[(objective, "error")] += 1
                    errors.append({"seed": leg["seed"], "order_id": leg["order_id"], "leg_role": leg["leg_role"], "objective": objective, "error": record["error"]})
                record["query_wall_s"] = round(time.perf_counter() - query_start, 6)
                durations.append(record["query_wall_s"])
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                checked += 1
            if checked % 200 == 0:
                print(json.dumps({"checked_queries": checked, "remaining_queries_expected": 3800, "elapsed_wall_s": round(time.perf_counter() - started, 2)}, ensure_ascii=False), flush=True)

    report = {
        "audit_id": "r1_v5_ev2_full_endpoint_preflight_v1",
        "status": "passed" if not errors and checked == 3800 else "failed",
        "zero_model_calls": True,
        "full_frozen_od_leg_count": len(all_legs),
        "previous_sampled_od_leg_count": len(sample_keys),
        "newly_checked_od_leg_count": len(all_legs) - len(sample_keys),
        "new_route_queries": checked,
        "previous_sample_route_queries": sample.get("route_queries"),
        "total_population_route_queries_including_sample": checked + int(sample.get("route_queries", 0)),
        "route_counts_for_remaining_population": {f"{objective}:{status}": count for (objective, status), count in counts.items()},
        "error_count_remaining_population": len(errors),
        "errors": errors,
        "mean_query_wall_s": statistics.mean(durations) if durations else None,
        "total_wall_s": round(time.perf_counter() - started, 3),
        "sample_preflight_path": str(SAMPLE_AUDIT),
        "sample_preflight_sha256": sha256(SAMPLE_AUDIT),
        "runner_sha256": sha256(RUNNER),
        "network_sha256": sha256(module.NETWORK),
        "scenario_manifest_sha256": sha256(INPUT_ROOT / "manifest_v3_3.json"),
        "traffic_snapshot": "same deterministic 80-edge synthetic queue snapshot used in sample preflight; it is a route-algorithm gate, not a SUMO performance outcome",
        "signal_snapshot": "phase 0 frozen; route reachability is checked offline and does not claim live signal prediction accuracy",
        "scope_note": "The previous 100-leg sample is reused from its passed audit and not queried again; this file stores every remaining leg/objective query.",
    }
    (OUTPUT / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "full_frozen_od_leg_count", "previous_sampled_od_leg_count", "newly_checked_od_leg_count", "new_route_queries", "error_count_remaining_population", "total_wall_s")}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    import statistics
    raise SystemExit(main())
