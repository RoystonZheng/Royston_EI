#!/usr/bin/env python3
"""Run a bounded C2 D0/D1 matching-mechanism preflight on the NYC TLC slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_SCENARIO_DIR = (
    ROOT
    / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/multi_vehicle_competition_scenarios_v1"
    / "C2_four_same_second_orders_three_vehicles"
)
DEFAULT_COST_MATRIX = (
    ROOT
    / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/zone_proxy_pilot_2024-01-16_18-19"
    / "zone_cost_matrix_train_2024-01-01_15.csv.gz"
)
DEFAULT_OUTPUT_DIR = (
    ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/c2_mechanism_preflight_v1"
)


def scenario_fingerprint(road_map, orders, vehicles) -> str:
    identity = {
        "map": road_map.to_dict(),
        "orders": [
            {
                "order_id": order.order_id,
                "arrival_time_sec": order.arrival_time_sec,
                "pickup_node_id": order.pickup_node_id,
                "dropoff_node_id": order.dropoff_node_id,
                "hotspot_id": order.hotspot_id,
                "passenger_no_show": order.passenger_no_show,
            }
            for order in orders
        ],
        "vehicles": [
            {
                "vehicle_id": vehicle.vehicle_id,
                "initial_position_node_id": vehicle.initial_position_node_id,
            }
            for vehicle in vehicles
        ],
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def serialize_method_result(inputs: dict, method_name: str, seed: int) -> Dict[str, Any]:
    from dispatch_experiment.models import DispatchConfig
    from dispatch_experiment.runner import DispatchSimulation

    scenario = inputs["scenario"]
    orders = inputs["orders"]
    vehicles = inputs["vehicles"]
    road_map = inputs["road_map"]
    controls = scenario["dispatch_controls"]
    config = DispatchConfig(
        map_size=road_map.size,
        vehicle_count=len(vehicles),
        order_count=len(orders),
        horizon_sec=int(controls["horizon_sec"]),
        max_pickup_wait_sec=float(controls["max_pickup_wait_sec"]),
        passenger_no_show_rate=float(controls["passenger_no_show_rate"]),
        method_name=method_name,
        feedback_mode="none",
        seed=seed,
    )
    fingerprint = scenario_fingerprint(road_map, orders, vehicles)
    simulation = DispatchSimulation(
        config,
        road_map=road_map,
        orders=orders,
        vehicles=vehicles,
    )
    summary, rounds = simulation.run()
    if not rounds:
        raise RuntimeError(f"{method_name}: simulation produced no dispatch rounds")

    first_round = rounds[0]
    candidates = first_round.candidates
    selected = first_round.output.assignments
    candidate_vehicle_ids = {candidate.vehicle_id for candidate in candidates}
    candidate_order_ids = {candidate.order_id for candidate in candidates}
    selected_vehicle_ids = [assignment.vehicle_id for assignment in selected]
    selected_order_ids = [assignment.order_id for assignment in selected]
    first_round_audit = {
        "dispatch_round_id": first_round.dispatch_round_id,
        "snapshot_time_sec": first_round.snapshot_time_sec,
        "trigger_type": first_round.trigger_type,
        "candidate_count": len(candidates),
        "hard_feasible_candidate_count": sum(
            1 for candidate in candidates if candidate.hard_constraint_ok
        ),
        "all_candidates_route_reachable": all(
            candidate.route_reachability for candidate in candidates
        ),
        "selected_assignment_count": len(selected),
        "selected_distinct_vehicle_count": len(set(selected_vehicle_ids)),
        "selected_distinct_order_count": len(set(selected_order_ids)),
        "waiting_order_count_at_decision": len(candidate_order_ids),
        "candidate_vehicle_count": len(candidate_vehicle_ids),
        "selected_vehicle_ids": selected_vehicle_ids,
        "selected_order_ids": selected_order_ids,
        "validator_status": first_round.output.validator_status,
        "hard_constraint_errors": first_round.output.hard_constraint_errors,
    }

    expected_candidates = len(orders) * len(vehicles)
    if first_round.snapshot_time_sec != 0.0:
        raise RuntimeError("C2 preflight expected all four orders in the initial time-zero round")
    if first_round_audit["candidate_count"] != expected_candidates:
        raise RuntimeError(
            f"expected {expected_candidates} initial candidates, got {first_round_audit['candidate_count']}"
        )
    if first_round_audit["hard_feasible_candidate_count"] != expected_candidates:
        raise RuntimeError("C2 expected all co-located vehicle/order pairs to be feasible")
    if len(selected) != len(vehicles):
        raise RuntimeError(f"expected {len(vehicles)} assignments in the initial round, got {len(selected)}")
    if len(set(selected_vehicle_ids)) != len(selected) or len(set(selected_order_ids)) != len(selected):
        raise RuntimeError("initial assignments violate one-to-one vehicle/order constraints")
    if first_round.output.validator_status != "valid" or first_round.output.hard_constraint_errors:
        raise RuntimeError("initial dispatch round failed the shared output validator")

    return {
        "schema_version": config.schema_version,
        "experiment_type": "one_round_matching_mechanism_preflight",
        "performance_claim": False,
        "scenario_id": scenario["scenario_id"],
        "fleet_id": inputs["fleet"]["fleet_id"],
        "method_name": method_name,
        "seed": seed,
        "scenario_fingerprint_sha256": fingerprint,
        "config": config.to_dict(),
        "input_provenance": {
            "scenario_metadata_sha256": inputs["scenario_metadata_sha256"],
            "orders_json_sha256": inputs["orders_json_sha256"],
            "cost_matrix_path": inputs["cost_matrix_path"].relative_to(ROOT).as_posix(),
            "cost_matrix_sha256": inputs["cost_matrix_sha256"],
            "cost_model": (
                "training-period median zone OD trip time and distance; used for occupied ride legs. "
                "Vehicle starts are co-located with pickup zones, so pickup legs are zero by scenario design. "
                "This is not a road network or validated empty-pickup ETA."
            ),
            "required_direct_od_costs": {
                f"{origin}->{destination}": inputs["od_costs"][(origin, destination)]
                for origin, destination in sorted(inputs["required_direct_od"])
            },
        },
        "initial_round_audit": first_round_audit,
        "summary": summary.to_dict(),
        "orders_after_simulation": [order.to_dict() for order in simulation.orders],
        "vehicles_after_simulation": [vehicle.to_dict() for vehicle in simulation.vehicles],
        "rounds": [record.to_dict() for record in rounds],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-dir", type=Path, default=DEFAULT_SCENARIO_DIR)
    parser.add_argument("--cost-matrix", type=Path, default=DEFAULT_COST_MATRIX)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    from dispatch_experiment.nyc_tlc_scenario import load_dispatch_scenario

    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite preflight output: {output_dir}")
    inputs = load_dispatch_scenario(args.scenario_dir, args.cost_matrix)
    scenario = inputs["scenario"]
    if scenario["scenario_id"] != "C2_four_same_second_orders_three_vehicles":
        raise ValueError("this bounded runner only accepts the C2 four-order/three-vehicle scenario")
    if len(inputs["orders"]) != 4 or len(inputs["vehicles"]) != 3:
        raise ValueError("C2 preflight requires exactly four orders and three vehicles")
    if any(order.arrival_time_sec != 0.0 for order in inputs["orders"]):
        raise ValueError("C2 preflight expects all orders to have the same timestamp")
    if any(vehicle.position_node_id != "zone_79" for vehicle in inputs["vehicles"]):
        raise ValueError("C2 preflight expects all three vehicles at Zone 79")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.building-", dir=output_dir.parent))
    try:
        results = []
        fingerprints = set()
        for method_name in ("D0_greedy", "D1_optimizer"):
            result = serialize_method_result(inputs, method_name, args.seed)
            (stage / f"{method_name}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            fingerprints.add(result["scenario_fingerprint_sha256"])
            results.append(
                {
                    "method_name": method_name,
                    "scenario_fingerprint_sha256": result["scenario_fingerprint_sha256"],
                    "initial_round_audit": result["initial_round_audit"],
                    "final_status_counts": result["summary"]["status_counts"],
                    "result_file": f"{method_name}.json",
                }
            )
        if len(fingerprints) != 1:
            raise RuntimeError("D0 and D1 did not use identical scenario fingerprints")

        manifest = {
            "experiment_id": "nyc_tlc_c2_matching_mechanism_preflight_v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "completed_mechanism_preflight",
            "performance_claim": False,
            "scenario_id": scenario["scenario_id"],
            "scenario_fingerprint_sha256": next(iter(fingerprints)),
            "source_scenario_dir": inputs["scenario_dir"].relative_to(ROOT).as_posix(),
            "methods": results,
            "interpretation": (
                "Checks initial candidate generation, one-to-one feasibility, maximum initial assignment count, "
                "and D0/D1 use of the same inputs. Results are not NYC performance estimates: vehicle positions "
                "are synthetic, pickup legs are zero by design, and ride costs are training-period zone OD medians."
            ),
        }
        (stage / "preflight_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (stage / "README.md").write_text(
            "# C2 matching mechanism preflight\n\n"
            "This output validates the first dispatch round for the four-order, three-vehicle C2 scenario. "
            "It is not a performance result. D0 and D1 use the same scenario fingerprint. "
            "The training-period zone OD medians represent occupied ride legs; all vehicles start in Zone 79, "
            "so pickup legs are zero by design. See `preflight_manifest.json` and the full per-method JSON files.\n",
            encoding="utf-8",
        )
        stage.rename(output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    print(json.dumps({"output_dir": str(output_dir), "methods": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
