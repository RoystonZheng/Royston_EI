#!/usr/bin/env python3
"""Run a bounded D0/D1 pilot on a NYC TLC request trace and zone-cost proxy.

This is not an actual fleet replay: vehicle states are synthetic and observed
occupied trip costs proxy zone-to-zone driving costs. The sample is suitable
only for checking whether the dispatch workflow and competition structure run.
Requires DuckDB (`python -m pip install duckdb`).
"""

from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import json
import random
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dispatch_experiment.models import DispatchConfig, Order, RouteLeg, Vehicle
from dispatch_experiment.runner import DispatchSimulation
from dispatch_experiment.route_oracle import GraphRouteOracle
from route_experiment.models import Edge, Node, RoadMap


DATA_ROOT = ROOT / "data/dispatch/nyc_tlc_hvfhv_2024"
TRACE_PATH = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/audit_2024-01/request_trace_2024-01.parquet"
OUTPUT_DIR = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/zone_proxy_pilot_2024-01-16_18-19"
TRAIN_START = "2024-01-01 00:00:00"
TRAIN_END = "2024-01-16 00:00:00"
TEST_START = "2024-01-16 18:00:00"
TEST_END = "2024-01-16 19:00:00"
SAMPLE_SIZE = 100
VEHICLE_COUNT = 10
SEED = 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def largest_strong_component(edges: Sequence[Tuple[int, int]]) -> Set[int]:
    adjacency: Dict[int, List[int]] = {}
    reverse: Dict[int, List[int]] = {}
    nodes: Set[int] = set()
    for source, target in edges:
        nodes.update((source, target))
        adjacency.setdefault(source, []).append(target)
        reverse.setdefault(target, []).append(source)

    seen: Set[int] = set()
    order: List[int] = []
    for root in sorted(nodes):
        if root in seen:
            continue
        stack = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                order.append(node)
                continue
            if node in seen:
                continue
            seen.add(node)
            stack.append((node, True))
            for neighbor in adjacency.get(node, ()):
                if neighbor not in seen:
                    stack.append((neighbor, False))

    seen.clear()
    components: List[Set[int]] = []
    for root in reversed(order):
        if root in seen:
            continue
        component: Set[int] = set()
        stack = [root]
        seen.add(root)
        while stack:
            node = stack.pop()
            component.add(node)
            for neighbor in reverse.get(node, ()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(component)
    return max(components, key=len)


class DirectZoneCostOracle(GraphRouteOracle):
    """Use observed direct zone medians; use the graph only for missing pairs."""

    def __init__(self, road_map: RoadMap):
        super().__init__(road_map)
        self._direct_edges = {
            (edge.from_node, edge.to_node): edge for edge in road_map.edges
        }

    def route(self, source_node_id: str, target_node_id: str) -> RouteLeg:
        key = (source_node_id, target_node_id)
        if key in self._cache:
            return self._cache[key]
        edge = self._direct_edges.get(key)
        if edge is None:
            return super().route(source_node_id, target_node_id)
        leg = RouteLeg(
            reachable=True,
            distance_m=edge.length_m,
            duration_sec=edge.length_m / edge.base_speed_mps,
            node_sequence=[source_node_id, target_node_id],
            edge_sequence=[edge.edge_id],
        )
        self._cache[key] = leg
        return leg


def main() -> None:
    if OUTPUT_DIR.exists():
        raise FileExistsError(f"Refusing to overwrite pilot output: {OUTPUT_DIR}")
    if not TRACE_PATH.is_file():
        raise FileNotFoundError(TRACE_PATH)
    raw_glob = DATA_ROOT / "raw/fhvhv_tripdata_2024-*.parquet"
    raw_glob_sql = raw_glob.resolve().as_posix().replace("'", "''")
    trace_sql = TRACE_PATH.resolve().as_posix().replace("'", "''")
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")
    con.execute("SET temp_directory='/tmp/nyc_tlc_duckdb_tmp'")
    cost_rows = con.execute(
        f"""
        SELECT PULocationID, DOLocationID,
               median(trip_time) AS median_trip_time_s,
               median(trip_miles) AS median_trip_miles,
               count(*) AS source_trip_count
        FROM read_parquet('{raw_glob_sql}')
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
    component = largest_strong_component([(row[0], row[1]) for row in cost_rows])
    core_rows = [row for row in cost_rows if row[0] in component and row[1] in component]

    max_zone_id = max(component)
    nodes = [
        Node(
            node_id=f"zone_{zone_id}",
            x=zone_id,
            y=0,
            legal_pickup=True,
            legal_dropoff=True,
        )
        for zone_id in sorted(component)
    ]
    edges: List[Edge] = []
    for origin, destination, median_time_s, median_miles, _count in core_rows:
        distance_m = float(median_miles) * 1609.344
        edges.append(
            Edge(
                edge_id=f"od_{origin}_{destination}",
                from_node=f"zone_{origin}",
                to_node=f"zone_{destination}",
                length_m=distance_m,
                base_speed_mps=distance_m / float(median_time_s),
                one_way=True,
            )
        )
    zone_map = RoadMap(
        map_id="nyc_hvfhv_empirical_zone_cost_graph_v1",
        version="training pickup dates 2024-01-01 through 2024-01-15",
        size=max_zone_id + 1,
        nodes=nodes,
        edges=edges,
        boundary={"x_min": 0, "x_max": max_zone_id, "y_min": 0, "y_max": 0},
        road_state_version="static_zone_od_medians_v1",
    )

    zone_ids_sql = ",".join(str(zone_id) for zone_id in sorted(component))
    window_total_count, window_core_count, window_core_cross_zone_count = con.execute(
        f"""
        SELECT count(*),
               count(*) FILTER (WHERE pickup_zone_id IN ({zone_ids_sql}) AND dropoff_zone_id IN ({zone_ids_sql})),
               count(*) FILTER (WHERE pickup_zone_id IN ({zone_ids_sql}) AND dropoff_zone_id IN ({zone_ids_sql})
                                  AND pickup_zone_id <> dropoff_zone_id)
        FROM read_parquet('{trace_sql}')
        WHERE request_datetime >= TIMESTAMP '{TEST_START}'
          AND request_datetime < TIMESTAMP '{TEST_END}'
        """
    ).fetchone()
    sample_rows = con.execute(
        f"""
        SELECT source_record_id, source_file, source_row_number, request_datetime,
               arrival_time_sec, pickup_zone_id, dropoff_zone_id
        FROM read_parquet('{trace_sql}')
        WHERE request_datetime >= TIMESTAMP '{TEST_START}'
          AND request_datetime < TIMESTAMP '{TEST_END}'
          AND pickup_zone_id IN ({zone_ids_sql})
          AND dropoff_zone_id IN ({zone_ids_sql})
          AND pickup_zone_id <> dropoff_zone_id
        ORDER BY md5(source_record_id)
        LIMIT {SAMPLE_SIZE}
        """
    ).fetchall()
    if len(sample_rows) != SAMPLE_SIZE:
        raise RuntimeError(f"expected {SAMPLE_SIZE} test orders, got {len(sample_rows)}")

    orders: List[Order] = []
    for row in sample_rows:
        orders.append(
            Order(
                order_id=row[0],
                arrival_time_sec=float(
                    (row[3] - datetime.fromisoformat(TEST_START)).total_seconds()
                ),
                pickup_node_id=f"zone_{row[5]}",
                dropoff_node_id=f"zone_{row[6]}",
                hotspot_id="nyc_taxi_zone_sample",
                passenger_no_show=False,
            )
        )
    orders.sort(key=lambda order: (order.arrival_time_sec, order.order_id))

    start_weights = con.execute(
        f"""
        SELECT PULocationID, count(*) AS n
        FROM read_parquet('{raw_glob_sql}')
        WHERE pickup_datetime >= TIMESTAMP '{TRAIN_START}'
          AND pickup_datetime < TIMESTAMP '{TRAIN_END}'
          AND shared_match_flag = 'N'
          AND request_datetime <= pickup_datetime
          AND pickup_datetime < dropoff_datetime
          AND trip_time > 0 AND trip_miles > 0
          AND PULocationID IN ({zone_ids_sql})
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    rng = random.Random(SEED)
    start_zones = rng.choices(
        [row[0] for row in start_weights],
        weights=[row[1] for row in start_weights],
        k=VEHICLE_COUNT,
    )
    initial_vehicles = [
        Vehicle(
            vehicle_id=f"vehicle-{index:02d}",
            initial_position_node_id=f"zone_{zone_id}",
            position_node_id=f"zone_{zone_id}",
        )
        for index, zone_id in enumerate(start_zones)
    ]
    initial_positions = [vehicle.initial_position_node_id for vehicle in initial_vehicles]

    OUTPUT_DIR.mkdir(parents=True)
    cost_csv = OUTPUT_DIR / "zone_cost_matrix_train_2024-01-01_15.csv.gz"
    with gzip.GzipFile(filename=str(cost_csv), mode="wb", mtime=0) as binary_stream:
        import io

        text_stream = io.TextIOWrapper(binary_stream, encoding="utf-8", newline="")
        writer = csv.writer(text_stream)
        writer.writerow(
            ["pickup_zone_id", "dropoff_zone_id", "median_trip_time_s", "median_trip_miles", "source_trip_count"]
        )
        for row in core_rows:
            writer.writerow(row)
        text_stream.flush()
        text_stream.detach()

    shared_fingerprint_payload = {
        "map_id": zone_map.map_id,
        "core_zones": sorted(component),
        "training_window": [TRAIN_START, TRAIN_END],
        "cost_rows": core_rows,
        "test_window": [TEST_START, TEST_END],
        "sample_order_ids": [order.order_id for order in orders],
        "sample_orders": [
            [order.arrival_time_sec, order.pickup_node_id, order.dropoff_node_id]
            for order in orders
        ],
        "initial_vehicle_positions": initial_positions,
    }
    shared_fingerprint = hashlib.sha256(
        json.dumps(shared_fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    sample_event_file = OUTPUT_DIR / "sampled_test_orders.json"
    sample_event_file.write_text(
        json.dumps(
            {
                "scenario_fingerprint_sha256": shared_fingerprint,
                "request_sample_count": len(orders),
                "window_population": window_core_cross_zone_count,
                "window_population_total": window_total_count,
                "window_population_in_zone_core": window_core_count,
                "sample_fraction": len(orders) / window_core_cross_zone_count,
                "orders": [order.to_dict() for order in orders],
                "synthetic_vehicle_initial_positions": initial_positions,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    results = []
    for method_name in ("D0_greedy", "D1_optimizer"):
        config = DispatchConfig(
            seed=SEED,
            method_name=method_name,
            map_size=6,
            vehicle_count=VEHICLE_COUNT,
            order_count=SAMPLE_SIZE,
            horizon_sec=3600,
            max_pickup_wait_sec=300.0,
            post_arrival_boarding_timeout_sec=180.0,
            passenger_no_show_rate=0.0,
            feedback_mode="none",
        )
        simulation = DispatchSimulation(config)
        simulation.config.map_size = max_zone_id + 1
        simulation.road_map = zone_map
        simulation.oracle = DirectZoneCostOracle(zone_map)
        simulation.hotspots = []
        simulation.orders = copy.deepcopy(orders)
        simulation.vehicles = copy.deepcopy(initial_vehicles)
        simulation.order_by_id = {order.order_id: order for order in simulation.orders}
        simulation.vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in simulation.vehicles}
        summary, rounds = simulation.run()
        conflict_orders = set()
        conflict_rounds = 0
        for record in rounds:
            feasible_by_order: Dict[str, Set[str]] = {}
            for candidate in record.candidates:
                if candidate.hard_constraint_ok:
                    feasible_by_order.setdefault(candidate.order_id, set()).add(candidate.vehicle_id)
            if any(len(vehicle_ids) > 1 for vehicle_ids in feasible_by_order.values()):
                conflict_rounds += 1
                conflict_orders.update(
                    order_id for order_id, vehicle_ids in feasible_by_order.items() if len(vehicle_ids) > 1
                )
        payload = {
            "experiment_label": "exploratory_zone_proxy_pilot_not_formal_result",
            "method": method_name,
            "scenario_fingerprint_sha256": shared_fingerprint,
            "config": config.to_dict(),
            "summary": summary.to_dict(),
            "candidate_conflict_audit": {
                "rounds_with_order_feasible_for_multiple_vehicles": conflict_rounds,
                "orders_with_multi_vehicle_competition": len(conflict_orders),
            },
            "orders_input": [order.to_dict() for order in orders],
            "orders_final": [order.to_dict() for order in simulation.orders],
            "vehicles_initial": [vehicle.to_dict() for vehicle in initial_vehicles],
            "vehicles_final": [vehicle.to_dict() for vehicle in simulation.vehicles],
            "rounds": [record.to_dict() for record in rounds],
        }
        result_path = OUTPUT_DIR / f"{method_name}_pilot.json"
        result_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        results.append(
            {
                "method": method_name,
                "result_file": result_path.name,
                "summary": summary.to_dict(),
                "candidate_conflict_audit": payload["candidate_conflict_audit"],
            }
        )

    manifest = json.loads((DATA_ROOT / "manifest.json").read_text(encoding="utf-8"))
    audit_manifest = json.loads(
        (TRACE_PATH.parent / "audit_2024-01.json").read_text(encoding="utf-8")
    )
    run_manifest = {
        "experiment_label": "exploratory_zone_proxy_pilot_not_formal_result",
        "created_date": date.today().isoformat(),
        "pilot_script_sha256": sha256_file(Path(__file__).resolve()),
        "duckdb_version": duckdb.__version__,
        "source_dataset_id": manifest["dataset_id"],
        "source_monthly_file_sha256": {
            "fhvhv_tripdata_2024-01.parquet": next(
                item["sha256"] for item in manifest["files"] if item["file"].endswith("2024-01.parquet")
            ),
            "fhvhv_tripdata_2024-02.parquet": next(
                item["sha256"] for item in manifest["files"] if item["file"].endswith("2024-02.parquet")
            ),
        },
        "request_trace_sha256": audit_manifest["trace_sha256"],
        "cost_model": {
            "source_fields": ["trip_time", "trip_miles"],
            "training_pickup_window": [TRAIN_START, TRAIN_END],
            "aggregation": "directed taxi-zone OD medians over completed single-match trips",
            "empty_vehicle_travel_assumption": "occupied trip OD medians proxy empty reposition and ride-leg costs",
            "missing_direct_pair": "Dijkstra over observed directed zone OD proxy edges",
            "component_policy": "largest strongly connected component of training OD graph",
            "zone_count": len(component),
            "directed_cost_pair_count": len(core_rows),
            "coordinate_note": "zone_id is stored in the x field only as a stable placeholder; map coordinates are not geographic and are not used for routing",
            "cost_matrix_file": cost_csv.name,
            "cost_matrix_sha256": sha256_file(cost_csv),
        },
        "test_sample": {
            "request_time_window": [TEST_START, TEST_END],
            "window_total_events": window_total_count,
            "window_events_with_both_zones_in_component": window_core_count,
            "window_cross_zone_events_within_component": window_core_cross_zone_count,
            "sample_size": SAMPLE_SIZE,
            "sample_fraction": SAMPLE_SIZE / window_core_cross_zone_count,
            "sample_method": "deterministic lowest MD5(source_record_id) sample; original request timestamps retained",
            "same_zone_orders_excluded": True,
            "vehicle_count": VEHICLE_COUNT,
            "vehicle_initial_positions": initial_positions,
            "vehicle_initialization": "synthetic, sampled with replacement by Jan 1-15 pickup-zone frequency",
            "no_show_rate": 0.0,
        },
        "method_results": results,
        "limitations": [
            "Only completed/dispatched source trips are present; unserved requests are absent.",
            "The cost graph is a taxi-zone proxy, not a physical road graph or route-layer experiment.",
            "Occupied trip time/distance do not equal empty pickup-leg costs.",
            "The 100-order sample is heavily downsampled from the citywide hour and is not an NYC service-rate estimate.",
            "Vehicle states and no-show behavior are assumptions; no actual fleet identifiers or states are available.",
            "This is one fixed-window pilot, not a multi-seed formal comparison.",
        ],
    }
    manifest_path = OUTPUT_DIR / "pilot_manifest.json"
    manifest_path.write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output_dir": str(OUTPUT_DIR),
                "zone_count": len(component),
                "zone_cost_pairs": len(core_rows),
                "test_window_total_population": window_total_count,
                "test_window_core_population": window_core_count,
                "test_window_core_cross_zone_population": window_core_cross_zone_count,
                "sample_fraction": SAMPLE_SIZE / window_core_cross_zone_count,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
