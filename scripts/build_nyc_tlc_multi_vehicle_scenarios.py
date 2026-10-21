#!/usr/bin/env python3
"""Build derived NYC TLC request slices for controlled multi-vehicle scenarios.

The source trace and raw TLC files are read-only. Synthetic vehicles are placed
in pickup zones on purpose so the derived scenarios contain known same-zone
candidate overlap. This is a dispatch-mechanism input package, not real fleet
replay or a route-cost evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import duckdb

ROOT = Path(__file__).resolve().parents[1]
TRACE_PATH = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/audit_2024-01/request_trace_2024-01.parquet"
DATASET_MANIFEST_PATH = ROOT / "data/dispatch/nyc_tlc_hvfhv_2024/manifest.json"
DEFAULT_OUTPUT_DIR = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/multi_vehicle_competition_scenarios_v1"

MANHATTAN_FOCUS_ZONES = [79, 125, 148, 161, 162, 163, 164, 170, 186, 230, 231, 234]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def iso(value: datetime) -> str:
    return value.isoformat(sep=" ")


def read_events(
    con: duckdb.DuckDBPyConnection,
    trace_sql: str,
    start: datetime,
    end: datetime,
    *,
    pickup_zone: int | None = None,
    pickup_zones: Sequence[int] | None = None,
    exact_timestamp: datetime | None = None,
) -> List[Tuple[Any, ...]]:
    filters = [
        f"request_datetime >= TIMESTAMP '{iso(start)}'",
        f"request_datetime < TIMESTAMP '{iso(end)}'",
    ]
    if pickup_zone is not None:
        filters.append(f"pickup_zone_id = {int(pickup_zone)}")
    if pickup_zones is not None:
        zone_csv = ",".join(str(int(zone)) for zone in sorted(set(pickup_zones)))
        filters.append(f"pickup_zone_id IN ({zone_csv})")
    if exact_timestamp is not None:
        filters.append(f"request_datetime = TIMESTAMP '{iso(exact_timestamp)}'")
    where_sql = " AND ".join(filters)
    query = f"""
        SELECT source_record_id, source_file, source_row_number,
               request_datetime,
               date_diff('microseconds', TIMESTAMP '{iso(start)}', request_datetime) / 1000000.0
                   AS arrival_time_sec,
               pickup_zone_id, dropoff_zone_id
        FROM read_parquet('{trace_sql}')
        WHERE {where_sql}
        ORDER BY request_datetime, source_record_id
    """
    return con.execute(query).fetchall()


def make_orders(scenario_id: str, events: Sequence[Tuple[Any, ...]]) -> List[Dict[str, Any]]:
    return [
        {
            "order_id": row[0],
            "arrival_time_sec": round(float(row[4]), 6),
            "pickup_node_id": f"zone_{int(row[5])}",
            "dropoff_node_id": f"zone_{int(row[6])}",
            "hotspot_id": scenario_id,
            "passenger_no_show": False,
        }
        for row in events
    ]


def make_vehicles(vehicle_zones: Sequence[int], prefix: str) -> List[Dict[str, Any]]:
    return [
        {
            "vehicle_id": f"{prefix}-{index:03d}",
            "initial_position_node_id": f"zone_{int(zone)}",
            "position_node_id": f"zone_{int(zone)}",
        }
        for index, zone in enumerate(vehicle_zones, start=1)
    ]


def fleet_variant(label: str, vehicle_zones: Sequence[int], prefix: str) -> Dict[str, Any]:
    return {
        "fleet_id": label,
        "vehicle_count": len(vehicle_zones),
        "positions_by_zone": {
            str(zone): list(vehicle_zones).count(zone) for zone in sorted(set(vehicle_zones))
        },
        "vehicles": make_vehicles(vehicle_zones, prefix),
        "initial_same_zone_candidate_count_per_order": "count of synthetic vehicles placed in that order's pickup zone",
    }


def write_scenario(
    con: duckdb.DuckDBPyConnection,
    stage: Path,
    *,
    scenario_id: str,
    title: str,
    source_window: Tuple[datetime, datetime],
    events: Sequence[Tuple[Any, ...]],
    fleet_variants: Sequence[Dict[str, Any]],
    expected_order_count: int,
    intended_competition: str,
    limitation: str,
) -> Dict[str, Any]:
    if len(events) != expected_order_count:
        raise RuntimeError(f"{scenario_id}: expected {expected_order_count} rows, got {len(events)}")
    if not events:
        raise RuntimeError(f"{scenario_id}: no request rows found")

    scenario_dir = stage / scenario_id
    scenario_dir.mkdir(parents=True)
    orders = make_orders(scenario_id, events)
    order_json = scenario_dir / "orders.json"
    order_json.write_text(
        json.dumps(
            {
                "scenario_id": scenario_id,
                "scenario_title": title,
                "source_request_window": [iso(source_window[0]), iso(source_window[1])],
                "order_count": len(orders),
                "orders": orders,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # The JSON carries exact model-ready orders; this compact Parquet keeps the
    # order provenance and source columns available for analysis.
    parquet_path = scenario_dir / "orders.parquet"
    table_rows = [
        (
            scenario_id,
            row[0],
            row[0],
            row[1],
            int(row[2]),
            row[3],
            round(float(row[4]), 6),
            int(row[5]),
            int(row[6]),
        )
        for row in events
    ]
    con.execute("DROP TABLE IF EXISTS scenario_orders_build")
    con.execute(
        """CREATE TEMP TABLE scenario_orders_build (
               scenario_id VARCHAR,
               order_id VARCHAR,
               source_record_id VARCHAR,
               source_file VARCHAR,
               source_row_number BIGINT,
               request_datetime TIMESTAMP,
               arrival_time_sec DOUBLE,
               pickup_zone_id INTEGER,
               dropoff_zone_id INTEGER
           )"""
    )
    con.executemany("INSERT INTO scenario_orders_build VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", table_rows)
    con.execute(f"COPY scenario_orders_build TO '{sql_path(parquet_path)}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    scenario = {
        "scenario_id": scenario_id,
        "title": title,
        "source_dataset": "NYC TLC 2024 High-Volume FHV, single-match candidate request trace",
        "source_request_window": [iso(source_window[0]), iso(source_window[1])],
        "request_trace_row_count": len(events),
        "origin_zone_counts": {
            str(zone): sum(1 for row in events if int(row[5]) == zone)
            for zone in sorted({int(row[5]) for row in events})
        },
        "distinct_origin_zone_count": len({int(row[5]) for row in events}),
        "distinct_destination_zone_count": len({int(row[6]) for row in events}),
        "fleet_variants": list(fleet_variants),
        "dispatch_controls": {
            "max_pickup_wait_sec": 300.0,
            "passenger_no_show_rate": 0.0,
            "one_to_one_assignment": True,
            "time_origin": iso(source_window[0]),
            "horizon_sec": max(60, int((source_window[1] - source_window[0]).total_seconds())),
        },
        "intended_competition": intended_competition,
        "limitations": [
            limitation,
            "Synthetic vehicles are deliberately placed in request pickup zones; this creates zero zone-level pickup time and omits within-zone driving distance.",
            "NYC TLC rows are already-dispatched/completed trips; unserved demand and real vehicle availability are absent.",
            "The existing zone OD proxy from the earlier pilot is not included as a validated road-cost model.",
        ],
        "input_files": {
            "orders_json": {
                "file": "orders.json",
                "sha256": sha256_file(order_json),
                "bytes": order_json.stat().st_size,
            },
            "orders_parquet": {
                "file": "orders.parquet",
                "sha256": sha256_file(parquet_path),
                "bytes": parquet_path.stat().st_size,
            },
        },
    }
    (scenario_dir / "scenario.json").write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "scenario_id": scenario_id,
        "title": title,
        "order_count": len(events),
        "distinct_origin_zone_count": scenario["distinct_origin_zone_count"],
        "distinct_destination_zone_count": scenario["distinct_destination_zone_count"],
        "fleet_counts": [variant["vehicle_count"] for variant in fleet_variants],
        "source_request_window": scenario["source_request_window"],
        "intended_competition": intended_competition,
        "scenario_json_sha256": sha256_file(scenario_dir / "scenario.json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite derived scenario data: {output_dir}")
    if not TRACE_PATH.is_file():
        raise FileNotFoundError(TRACE_PATH)
    if not DATASET_MANIFEST_PATH.is_file():
        raise FileNotFoundError(DATASET_MANIFEST_PATH)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.building-", dir=output_dir.parent))
    try:
        con = duckdb.connect()
        con.execute("SET threads=4")
        con.execute("SET memory_limit='2GB'")
        trace_sql = sql_path(TRACE_PATH)

        c2_start = datetime(2024, 1, 20, 21, 54, 6)
        c2_end = datetime(2024, 1, 20, 21, 54, 7)
        c2_events = read_events(
            con, trace_sql, c2_start, c2_end, pickup_zone=79, exact_timestamp=c2_start
        )
        if len(c2_events) != 4:
            raise RuntimeError(f"Expected four same-second Zone 79 orders, got {len(c2_events)}")
        c1_events = c2_events[:1]

        c3_start = datetime(2024, 1, 17, 20, 0, 20)
        c3_end = datetime(2024, 1, 17, 20, 0, 30)
        c3_events = read_events(
            con,
            trace_sql,
            c3_start,
            c3_end,
            pickup_zones=MANHATTAN_FOCUS_ZONES,
        )
        c3_origins = sorted({int(row[5]) for row in c3_events})
        if len(c3_events) != 44 or len(c3_origins) != 9:
            raise RuntimeError(
                f"Expected 44 orders from 9 origins in C3, got {len(c3_events)} from {len(c3_origins)}"
            )

        c4_start = datetime(2024, 1, 20, 23, 20, 0)
        c4_end = datetime(2024, 1, 20, 23, 25, 0)
        c4_events = read_events(
            con,
            trace_sql,
            c4_start,
            c4_end,
            pickup_zones=MANHATTAN_FOCUS_ZONES,
        )
        if len(c4_events) != 790:
            raise RuntimeError(f"Expected 790 requests in C4, got {len(c4_events)}")

        c1_vehicles = [79, 79, 79]
        c2_vehicles = [79, 79, 79]
        c3_vehicles = [zone for zone in c3_origins for _ in range(2)]
        c4_variants = [
            fleet_variant(f"F{per_zone}", [zone for zone in MANHATTAN_FOCUS_ZONES for _ in range(per_zone)], f"C4-F{per_zone}")
            for per_zone in (1, 2, 4)
        ]

        scenarios = [
            write_scenario(
                con,
                stage,
                scenario_id="C1_one_order_three_vehicle_candidates",
                title="单订单多车候选机制检查",
                source_window=(c2_start, c2_end),
                events=c1_events,
                fleet_variants=[fleet_variant("F3_same_zone", c1_vehicles, "C1")],
                expected_order_count=1,
                intended_competition="One order has three initially co-located synthetic vehicles available.",
                limitation="Mechanism-only singleton; not a performance comparison.",
            ),
            write_scenario(
                con,
                stage,
                scenario_id="C2_four_same_second_orders_three_vehicles",
                title="同秒四单争抢三车",
                source_window=(c2_start, c2_end),
                events=c2_events,
                fleet_variants=[fleet_variant("F3_same_zone", c2_vehicles, "C2")],
                expected_order_count=4,
                intended_competition="Four orders arrive in the same recorded second at Zone 79; three vehicles create a 4-by-3 one-to-one matching instance.",
                limitation="All starting vehicles are co-located at Zone 79, so pickup costs tie at zone resolution; use distinct reachable starts after a road network is available for cost-sensitive comparison.",
            ),
            write_scenario(
                con,
                stage,
                scenario_id="C3_manhattan_multizone_44_order_burst",
                title="曼哈顿九起点区跨区突发",
                source_window=(c3_start, c3_end),
                events=c3_events,
                fleet_variants=[fleet_variant("F2_per_observed_origin", c3_vehicles, "C3")],
                expected_order_count=44,
                intended_competition="Forty-four requests over ten seconds across nine Manhattan pickup zones; two co-located synthetic vehicles per observed origin create same-zone multi-vehicle candidates and same-zone order contention.",
                limitation="This dense supply layout is controlled and does not estimate actual vehicle supply or cross-zone reachability.",
            ),
            write_scenario(
                con,
                stage,
                scenario_id="C4_manhattan_790_order_five_minute_load_sweep",
                title="曼哈顿十二关注区五分钟负载阶梯",
                source_window=(c4_start, c4_end),
                events=c4_events,
                fleet_variants=c4_variants,
                expected_order_count=790,
                intended_competition="Seven hundred ninety requests over five minutes across twelve Manhattan focus zones; fleet variants place one, two, or four synthetic vehicles in each zone.",
                limitation="Fleet sweep is a controlled load sensitivity analysis, not an inferred NYC fleet-size estimate.",
            ),
        ]

        dataset_manifest = json.loads(DATASET_MANIFEST_PATH.read_text(encoding="utf-8"))
        root_manifest = {
            "dataset_package_id": "nyc_tlc_2024_multivehicle_competition_scenarios_v1",
            "created_date": datetime.now().date().isoformat(),
            "source_dataset_id": dataset_manifest["dataset_id"],
            "source_trace": {
                "file": TRACE_PATH.relative_to(ROOT).as_posix(),
                "sha256": sha256_file(TRACE_PATH),
                "audit_file": "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/audit_2024-01/audit_2024-01.json",
            },
            "scenario_design": {
                "prior_pilot_training_window_not_used_by_this_builder": ["2024-01-01 00:00:00", "2024-01-16 00:00:00"],
                "event_windows_are_from_test_period": True,
                "focus_manhattan_zones": MANHATTAN_FOCUS_ZONES,
                "raw_source_and_existing_trace_modified": False,
                "vehicles_are_synthetic": True,
                "vehicle_positions_are_deliberately_co_located_with_pickup_zones": True,
                "same_zone_pickup_assumption": "zero zone-level pickup time; intra-zone movement omitted",
                "no_show_rate": 0.0,
                "max_pickup_wait_sec": 300.0,
                "formal_performance_result": False,
            },
            "scenarios": scenarios,
        }
        (stage / "scenario_manifest.json").write_text(
            json.dumps(root_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (stage / "README.md").write_text(
            "# NYC TLC multi-vehicle competition scenarios v1\n\n"
            "Derived scenario inputs built from the audited January request trace. The raw TLC files and source trace were not modified. "
            "Synthetic vehicles are deliberately colocated with observed pickup zones to create known candidate overlap at zone resolution. "
            "No NYC road ETA or real fleet state is implied. These are scenario inputs, not performance results.\n\n"
            "See `scenario_manifest.json` and each scenario's `scenario.json`. `orders.json` uses the dispatch experiment's order field names; `orders.parquet` preserves source provenance.\n",
            encoding="utf-8",
        )
        con.close()
        stage.rename(output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    print(json.dumps({"output_dir": str(output_dir), "scenarios": scenarios}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
