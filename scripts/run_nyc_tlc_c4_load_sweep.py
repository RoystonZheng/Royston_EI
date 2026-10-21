#!/usr/bin/env python3
"""Run compact D0/D1 C4 load-sweep simulations for 12/24/48 synthetic cars.

The runner keeps per-round validation and aggregate candidate counts but does
not persist every candidate snapshot for this 790-order workload.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA_ROOT = ROOT / "data/dispatch/nyc_tlc_hvfhv_2024"
SOURCE_PARQUET = DATA_ROOT / "raw/fhvhv_tripdata_2024-01.parquet"
DATASET_MANIFEST = DATA_ROOT / "manifest.json"
DEFAULT_SCENARIO_DIR = (
    ROOT
    / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/multi_vehicle_competition_scenarios_v1"
    / "C4_manhattan_790_order_five_minute_load_sweep"
)
DEFAULT_OUTPUT_DIR = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/c4_load_sweep_mechanism_v1"
MATRIX_BUILDER_IMPORT = "scripts.run_nyc_tlc_c3_mechanism_preflight"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scenario_fingerprint(road_map, orders, vehicles) -> str:
    identity = {
        "map": road_map.to_dict(),
        "orders": [
            [
                order.order_id,
                order.arrival_time_sec,
                order.pickup_node_id,
                order.dropoff_node_id,
                order.passenger_no_show,
            ]
            for order in orders
        ],
        "vehicles": [
            [vehicle.vehicle_id, vehicle.initial_position_node_id]
            for vehicle in vehicles
        ],
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def run_fleet_method(
    *, scenario_dir: Path, matrix_path: Path, fleet_id: str, method_name: str, seed: int, output_dir: Path
) -> Dict[str, Any]:
    from dispatch_experiment.models import DispatchConfig
    from dispatch_experiment.nyc_tlc_scenario import NYCTLCZoneRouteOracle, load_dispatch_scenario
    from dispatch_experiment.runner import DispatchSimulation

    inputs = load_dispatch_scenario(scenario_dir, matrix_path, fleet_id=fleet_id)
    scenario = inputs["scenario"]
    controls = scenario["dispatch_controls"]
    orders = inputs["orders"]
    vehicles = inputs["vehicles"]
    road_map = inputs["road_map"]
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
        route_oracle=NYCTLCZoneRouteOracle(road_map, inputs["od_costs"]),
        retain_round_records=False,
    )
    summary, round_records = simulation.run()
    if round_records or not simulation.round_audits:
        raise RuntimeError(f"{fleet_id}/{method_name}: compact trace mode did not produce round audits")
    if simulation.round_validation_errors:
        raise RuntimeError(
            f"{fleet_id}/{method_name}: one or more rounds failed validation: "
            f"{simulation.round_validation_errors[:3]}"
        )

    rejected = Counter()
    competing_orders = set()
    competition_round_count = 0
    for audit in simulation.round_audits:
        rejected.update(audit["rejected_candidate_decision_counts"])
        if audit["multi_vehicle_competition_order_count"]:
            competition_round_count += 1
            competing_orders.update(audit["multi_vehicle_competition_order_ids"])
    assigned_map = {
        order.order_id: order.assigned_vehicle_id
        for order in simulation.orders
        if order.assigned_vehicle_id
    }
    result = {
        "schema_version": config.schema_version,
        "experiment_type": "synthetic_load_sweep_mechanism",
        "performance_claim": False,
        "scenario_id": scenario["scenario_id"],
        "fleet_id": fleet_id,
        "method_name": method_name,
        "seed": seed,
        "scenario_fingerprint_sha256": fingerprint,
        "config": config.to_dict(),
        "input_provenance": {
            "scenario_metadata_sha256": inputs["scenario_metadata_sha256"],
            "orders_json_sha256": inputs["orders_json_sha256"],
            "cost_matrix_sha256": inputs["cost_matrix_sha256"],
            "direct_order_od_pair_count": len(inputs["required_direct_od"]),
            "all_direct_order_od_pairs_present": all(
                pair in inputs["od_costs"] for pair in inputs["required_direct_od"]
            ),
            "cost_model": (
                "direct observed January 1-15 median trip time/distance for occupied legs, including "
                "same-zone trips; same-zone empty pickup is zero by scenario design, cross-zone empty pickup is unavailable."
            ),
        },
        "summary": summary.to_dict(),
        "candidate_audit": {
            "dispatch_round_count": len(simulation.round_audits),
            "candidate_decision_count": sum(
                audit["candidate_decision_count"] for audit in simulation.round_audits
            ),
            "retained_candidate_count": sum(
                audit["retained_candidate_count"] for audit in simulation.round_audits
            ),
            "rejected_decision_counts_by_reason": dict(sorted(rejected.items())),
            "rounds_with_multi_vehicle_order_competition": competition_round_count,
            "distinct_orders_with_multi_vehicle_competition": len(competing_orders),
            "selected_assignment_count": sum(
                audit["selected_assignment_count"] for audit in simulation.round_audits
            ),
            "validation_error_count": len(simulation.round_validation_errors),
        },
        "round_audits": simulation.round_audits,
        "assigned_order_vehicle_map": assigned_map,
        "orders_after_simulation": [order.to_dict() for order in simulation.orders],
        "vehicles_after_simulation": [vehicle.to_dict() for vehicle in simulation.vehicles],
    }
    filename = f"{method_name}_{fleet_id}.json"
    (output_dir / filename).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "fleet_id": fleet_id,
        "vehicle_count": len(vehicles),
        "method_name": method_name,
        "scenario_fingerprint_sha256": fingerprint,
        "summary": summary.to_dict(),
        "candidate_audit": result["candidate_audit"],
        "result_file": filename,
    }


def main() -> None:
    import argparse
    from importlib import import_module

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-dir", type=Path, default=DEFAULT_SCENARIO_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    scenario_dir = args.scenario_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite C4 output: {output_dir}")
    if not SOURCE_PARQUET.is_file() or not DATASET_MANIFEST.is_file():
        raise FileNotFoundError("NYC TLC January source or dataset manifest is missing")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.building-", dir=output_dir.parent))
    try:
        helper = import_module(MATRIX_BUILDER_IMPORT)
        matrix_path = stage / "zone_cost_matrix_train_2024-01-01_15_full_observed_od_including_same_zone.csv.gz"
        matrix_audit = helper.build_untrimmed_zone_od_matrix(matrix_path, include_same_zone=True)
        source_manifest = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
        source_entry = next(
            item for item in source_manifest["files"]
            if item["file"] == "raw/fhvhv_tripdata_2024-01.parquet"
        )
        if sha256_file(SOURCE_PARQUET) != source_entry["sha256"]:
            raise RuntimeError("January source parquet hash differs from downloaded-data manifest")
        scenario = json.loads((scenario_dir / "scenario.json").read_text(encoding="utf-8"))
        if scenario["scenario_id"] != "C4_manhattan_790_order_five_minute_load_sweep":
            raise ValueError("this runner only accepts the C4 790-order load-sweep scenario")
        if scenario["request_trace_row_count"] != 790:
            raise ValueError("C4 load sweep requires exactly 790 orders")
        expected_fleets = [variant["fleet_id"] for variant in scenario["fleet_variants"]]
        if len(expected_fleets) != 3:
            raise ValueError("C4 preflight requires three fleet scaling variants")

        method_results = []
        pair_fingerprints: Dict[str, set] = {fleet_id: set() for fleet_id in expected_fleets}
        for fleet_id in expected_fleets:
            for method_name in ("D0_greedy", "D1_optimizer"):
                result = run_fleet_method(
                    scenario_dir=scenario_dir,
                    matrix_path=matrix_path,
                    fleet_id=fleet_id,
                    method_name=method_name,
                    seed=args.seed,
                    output_dir=stage,
                )
                method_results.append(result)
                pair_fingerprints[fleet_id].add(result["scenario_fingerprint_sha256"])
            if len(pair_fingerprints[fleet_id]) != 1:
                raise RuntimeError(f"D0 and D1 used different inputs for {fleet_id}")

        dataset_entry = source_entry
        manifest = {
            "experiment_id": "nyc_tlc_c4_load_sweep_mechanism_v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "completed_mechanism_preflight",
            "performance_claim": False,
            "scenario_id": scenario["scenario_id"],
            "scenario_metadata_sha256": sha256_file(scenario_dir / "scenario.json"),
            "orders_json_sha256": sha256_file(scenario_dir / "orders.json"),
            "dataset_manifest_sha256": sha256_file(DATASET_MANIFEST),
            "source_monthly_file": dataset_entry,
            "experiment_script_sha256": sha256_file(Path(__file__).resolve()),
            "cost_matrix": {
                "file": matrix_path.name,
                **matrix_audit,
            },
            "fleet_scenario_fingerprints": {
                fleet_id: next(iter(fingerprints))
                for fleet_id, fingerprints in pair_fingerprints.items()
            },
            "methods": method_results,
            "pickup_cost_policy": (
                "Same-zone empty pickup is zero by synthetic placement; cross-zone empty pickup is explicitly unavailable."
            ),
            "ride_cost_policy": (
                "Use each order's direct observed January 1-15 OD median time and distance, including same-zone trips; "
                "do not compose zone OD medians into road routes."
            ),
            "trace_policy": (
                "Compact per-round validation/candidate audit only; full candidate and state snapshots are not stored for C4. "
                "Final orders, vehicle states, assignments, input hashes and aggregate decision counts are retained."
            ),
            "interpretation": (
                "Compares load and synthetic fleet-size scenarios for mechanism execution only. The source slice is "
                "already-dispatched/completed demand, initial fleet positions are invented, within-zone pickup is zero, "
                "and cross-zone pickup is disabled; results are not NYC service-rate or dispatch-performance estimates."
            ),
        }
        c4_orders = json.loads((scenario_dir / "orders.json").read_text(encoding="utf-8"))["orders"]
        same_zone_pair_count = len({
            (row["pickup_node_id"], row["dropoff_node_id"])
            for row in c4_orders
            if row["pickup_node_id"] == row["dropoff_node_id"]
        })
        manifest["cost_matrix"]["same_zone_order_od_pair_count"] = same_zone_pair_count
        (stage / "preflight_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (stage / "README.md").write_text(
            "# C4 load-sweep mechanism preflight\n\n"
            "This package runs D0 and D1 against the same 790-order, five-minute C4 trace for three synthetic "
            "fleet sizes. The training cost matrix includes direct observed same-zone ride medians. Empty pickup "
            "is zero only for same-zone vehicle placement; cross-zone empty pickup is unavailable. To keep this "
            "790-order run bounded, the package stores per-round validation/candidate summaries rather than full "
            "candidate snapshots. This is a mechanism/sensitivity preflight, not an NYC performance estimate.\n",
            encoding="utf-8",
        )
        stage.rename(output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    compact = [
        {
            "fleet_id": item["fleet_id"],
            "vehicle_count": item["vehicle_count"],
            "method": item["method_name"],
            "completed": item["summary"]["served_orders"],
            "unserved": item["summary"]["unserved_orders"],
            "rounds": item["candidate_audit"]["dispatch_round_count"],
            "validations": item["candidate_audit"]["validation_error_count"],
            "file": item["result_file"],
        }
        for item in method_results
    ]
    print(json.dumps({"output_dir": str(output_dir), "results": compact}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
