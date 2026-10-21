#!/usr/bin/env python3
"""Run a bounded D0/D1 mechanism preflight on the 44-order NYC TLC C3 case.

This is a controlled zone-level simulation, not an NYC fleet-performance
estimate. It rebuilds untrimmed January training-period OD medians and allows
empty pickup only when vehicle and request share a zone.
Requires DuckDB (available in the workspace audit runtime).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA_ROOT = ROOT / "data/dispatch/nyc_tlc_hvfhv_2024"
SOURCE_PARQUET = DATA_ROOT / "raw/fhvhv_tripdata_2024-01.parquet"
DATASET_MANIFEST = DATA_ROOT / "manifest.json"
DEFAULT_SCENARIO_DIR = (
    ROOT
    / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/multi_vehicle_competition_scenarios_v1"
    / "C3_manhattan_multizone_44_order_burst"
)
DEFAULT_OUTPUT_DIR = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/c3_mechanism_preflight_v1"
TRAIN_START = "2024-01-01 00:00:00"
TRAIN_END = "2024-01-16 00:00:00"


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
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_untrimmed_zone_od_matrix(output_path: Path) -> Dict[str, Any]:
    """Aggregate observed directed OD medians without SCC clipping."""

    import duckdb

    if not SOURCE_PARQUET.is_file():
        raise FileNotFoundError(SOURCE_PARQUET)
    source_sql = SOURCE_PARQUET.resolve().as_posix().replace("'", "''")
    connection = duckdb.connect()
    connection.execute("SET threads=4")
    connection.execute("SET memory_limit='4GB'")
    connection.execute("SET temp_directory='/tmp/nyc_tlc_duckdb_tmp'")
    try:
        rows = connection.execute(
            f"""
            SELECT PULocationID, DOLocationID,
                   median(trip_time) AS median_trip_time_s,
                   median(trip_miles) AS median_trip_miles,
                   count(*) AS source_trip_count
            FROM read_parquet('{source_sql}')
            WHERE pickup_datetime >= TIMESTAMP '{TRAIN_START}'
              AND pickup_datetime < TIMESTAMP '{TRAIN_END}'
              AND shared_match_flag = 'N'
              AND request_datetime <= pickup_datetime
              AND pickup_datetime < dropoff_datetime
              AND trip_time > 0 AND trip_miles > 0
              AND PULocationID IS NOT NULL AND DOLocationID IS NOT NULL
              AND PULocationID <> DOLocationID
            GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise RuntimeError("training-period zone OD aggregation returned no rows")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(filename=str(output_path), mode="wb", mtime=0) as binary_stream:
        with io.TextIOWrapper(binary_stream, encoding="utf-8", newline="") as text_stream:
            writer = csv.writer(text_stream)
            writer.writerow(
                [
                    "pickup_zone_id",
                    "dropoff_zone_id",
                    "median_trip_time_s",
                    "median_trip_miles",
                    "source_trip_count",
                ]
            )
            writer.writerows(rows)
    return {
        "duckdb_version": duckdb.__version__,
        "training_window_pickup_datetime": [TRAIN_START, TRAIN_END],
        "aggregation": "median trip_time and trip_miles per directed OD; completed single-match records",
        "filters": [
            "shared_match_flag = N",
            "request_datetime <= pickup_datetime < dropoff_datetime",
            "trip_time > 0 and trip_miles > 0",
            "non-null zones and pickup_zone_id != dropoff_zone_id",
        ],
        "component_policy": "no SCC clipping; retain every observed qualifying directed OD pair",
        "directed_od_pair_count": len(rows),
        "training_trip_rows_in_matrix": sum(int(row[4]) for row in rows),
        "matrix_sha256": sha256_file(output_path),
        "matrix_bytes": output_path.stat().st_size,
    }


def summarize_rounds(rounds, burst_end_sec: float) -> Dict[str, Any]:
    contested_orders = set()
    conflict_round_count = 0
    invalid_rounds = []
    candidate_count = 0
    feasible_candidate_count = 0
    selected_assignment_count = 0
    burst_rounds = []
    one_to_one_violations = []
    for record in rounds:
        candidates = record.candidates
        candidate_count += len(candidates)
        feasible = [candidate for candidate in candidates if candidate.hard_constraint_ok]
        feasible_candidate_count += len(feasible)
        by_order: Dict[str, set] = {}
        for candidate in feasible:
            by_order.setdefault(candidate.order_id, set()).add(candidate.vehicle_id)
        round_contested = {order_id for order_id, vehicle_ids in by_order.items() if len(vehicle_ids) > 1}
        if round_contested:
            conflict_round_count += 1
            contested_orders.update(round_contested)
        selected = record.output.assignments
        selected_assignment_count += len(selected)
        selected_vehicle_ids = [assignment.vehicle_id for assignment in selected]
        selected_order_ids = [assignment.order_id for assignment in selected]
        if len(set(selected_vehicle_ids)) != len(selected) or len(set(selected_order_ids)) != len(selected):
            one_to_one_violations.append(record.dispatch_round_id)
        if record.output.validator_status != "valid" or record.output.hard_constraint_errors:
            invalid_rounds.append(record.dispatch_round_id)
        if record.snapshot_time_sec <= burst_end_sec + 1e-9:
            burst_rounds.append(
                {
                    "dispatch_round_id": record.dispatch_round_id,
                    "time_sec": record.snapshot_time_sec,
                    "candidate_count": len(candidates),
                    "hard_feasible_candidate_count": len(feasible),
                    "waiting_order_count": len({c.order_id for c in candidates}),
                    "contested_order_count": len(round_contested),
                    "selected_assignment_count": len(selected),
                    "validator_status": record.output.validator_status,
                }
            )
    return {
        "dispatch_round_count": len(rounds),
        "candidate_count_total": candidate_count,
        "hard_feasible_candidate_count_total": feasible_candidate_count,
        "rounds_with_same_order_feasible_for_multiple_vehicles": conflict_round_count,
        "distinct_orders_observed_with_multi_vehicle_competition": len(contested_orders),
        "selected_assignment_count_across_rounds": selected_assignment_count,
        "one_to_one_violations": one_to_one_violations,
        "invalid_rounds": invalid_rounds,
        "rounds_during_request_burst": burst_rounds,
    }


def run_method(inputs: dict, method_name: str, seed: int, output_dir: Path) -> Dict[str, Any]:
    from dispatch_experiment.models import DispatchConfig
    from dispatch_experiment.nyc_tlc_scenario import NYCTLCZoneRouteOracle
    from dispatch_experiment.runner import DispatchSimulation

    scenario = inputs["scenario"]
    controls = scenario["dispatch_controls"]
    orders = inputs["orders"]
    vehicles = inputs["vehicles"]
    road_map = inputs["road_map"]
    route_oracle = NYCTLCZoneRouteOracle(road_map, inputs["od_costs"])
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
        route_oracle=route_oracle,
    )
    summary, rounds = simulation.run()
    if not rounds:
        raise RuntimeError(f"{method_name}: simulation produced no dispatch rounds")
    round_audit = summarize_rounds(rounds, max(order.arrival_time_sec for order in orders))
    if round_audit["invalid_rounds"] or round_audit["one_to_one_violations"]:
        raise RuntimeError(f"{method_name}: at least one dispatch round failed output validation")

    result = {
        "schema_version": config.schema_version,
        "experiment_type": "multi_round_matching_mechanism_preflight",
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
            "cost_matrix_sha256": inputs["cost_matrix_sha256"],
            "cost_model": (
                "direct observed training-period median trip time and distance for occupied ride legs; "
                "cross-zone empty pickup is unreachable, and same-zone pickup is zero-cost by scenario design."
            ),
            "required_direct_od_costs": {
                f"{origin}->{destination}": inputs["od_costs"][(origin, destination)]
                for origin, destination in sorted(inputs["required_direct_od"])
            },
        },
        "round_audit": round_audit,
        "summary": summary.to_dict(),
        "orders_after_simulation": [order.to_dict() for order in simulation.orders],
        "vehicles_after_simulation": [vehicle.to_dict() for vehicle in simulation.vehicles],
        "rounds": [record.to_dict() for record in rounds],
    }
    (output_dir / f"{method_name}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "method_name": method_name,
        "scenario_fingerprint_sha256": fingerprint,
        "round_audit": round_audit,
        "final_status_counts": summary.status_counts,
        "result_file": f"{method_name}.json",
    }


def main() -> None:
    import argparse

    from dispatch_experiment.nyc_tlc_scenario import load_dispatch_scenario

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-dir", type=Path, default=DEFAULT_SCENARIO_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite C3 preflight output: {output_dir}")
    if not DATASET_MANIFEST.is_file():
        raise FileNotFoundError(DATASET_MANIFEST)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.building-", dir=output_dir.parent))
    try:
        matrix_path = stage / "zone_cost_matrix_train_2024-01-01_15_full_observed_od.csv.gz"
        matrix_audit = build_untrimmed_zone_od_matrix(matrix_path)
        inputs = load_dispatch_scenario(args.scenario_dir, matrix_path)
        scenario = inputs["scenario"]
        if scenario["scenario_id"] != "C3_manhattan_multizone_44_order_burst":
            raise ValueError("this runner only accepts the 44-order C3 burst scenario")
        if len(inputs["orders"]) != 44 or len(inputs["vehicles"]) != 18:
            raise ValueError("C3 preflight requires exactly 44 orders and 18 vehicles")
        if len(inputs["required_direct_od"]) != 42:
            raise ValueError("C3 preflight expected 42 distinct direct order OD pairs")

        dataset_manifest = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
        source_entry = next(
            item for item in dataset_manifest["files"]
            if item["file"] == "raw/fhvhv_tripdata_2024-01.parquet"
        )
        if sha256_file(SOURCE_PARQUET) != source_entry["sha256"]:
            raise RuntimeError("January source parquet hash differs from the downloaded-data manifest")
        if len(inputs["required_direct_od"]) > len(inputs["od_costs"]):
            raise RuntimeError("the cost matrix has fewer OD pairs than the C3 order set")

        method_results = []
        fingerprints = set()
        for method_name in ("D0_greedy", "D1_optimizer"):
            result = run_method(inputs, method_name, args.seed, stage)
            method_results.append(result)
            fingerprints.add(result["scenario_fingerprint_sha256"])
        if len(fingerprints) != 1:
            raise RuntimeError("D0 and D1 used different scenario fingerprints")

        manifest = {
            "experiment_id": "nyc_tlc_c3_multiround_mechanism_preflight_v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "completed_mechanism_preflight",
            "performance_claim": False,
            "scenario_id": scenario["scenario_id"],
            "scenario_fingerprint_sha256": next(iter(fingerprints)),
            "source_scenario_dir": inputs["scenario_dir"].relative_to(ROOT).as_posix(),
            "source_request_window": scenario["source_request_window"],
            "source_dataset": dataset_manifest["dataset_id"],
            "source_monthly_file": source_entry,
            "dataset_manifest_sha256": sha256_file(DATASET_MANIFEST),
            "experiment_script_sha256": sha256_file(Path(__file__).resolve()),
            "cost_matrix": {
                "file": matrix_path.name,
                **matrix_audit,
                "direct_c3_order_od_pair_count": len(inputs["required_direct_od"]),
                "all_c3_direct_order_od_pairs_present": all(
                    pair in inputs["od_costs"] for pair in inputs["required_direct_od"]
                ),
            },
            "pickup_cost_policy": (
                "Same-zone vehicle-to-pickup legs have zero time/distance by scenario design. "
                "All cross-zone empty pickup routes are explicitly unreachable because occupied TLC trips "
                "do not provide empty pickup ETAs."
            ),
            "ride_cost_policy": (
                "Use only the direct observed median time/distance for each requested OD; no SCC clipping, "
                "no shortest-path composition, and no street-level route claim."
            ),
            "methods": method_results,
            "interpretation": (
                "Checks multi-round candidate generation, same-zone multi-vehicle competition, one-to-one "
                "assignment validation, and D0/D1 execution on identical inputs. Synthetic vehicle positions, "
                "zero within-zone pickup cost, already-dispatched TLC trips, and occupied-trip OD proxies mean "
                "these results are not NYC service, wait-time, or algorithm-performance estimates."
            ),
        }
        (stage / "preflight_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (stage / "README.md").write_text(
            "# C3 multi-round mechanism preflight\n\n"
            "This package runs D0 and D1 on the 44-order, 18-vehicle C3 burst using identical inputs. "
            "It rebuilds an untrimmed observed zone OD training matrix from January 1–15 source trips. "
            "Pickup cost is zero only for same-zone vehicles; cross-zone empty pickup is unavailable. "
            "The matrix stores direct occupied-trip medians and is not a road network. Results validate "
            "mechanism execution and are not NYC performance estimates. See `preflight_manifest.json` "
            "and the two per-method logs.\n",
            encoding="utf-8",
        )
        stage.rename(output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    print(json.dumps({"output_dir": str(output_dir), "methods": method_results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
