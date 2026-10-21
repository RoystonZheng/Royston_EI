#!/usr/bin/env python3
"""Audit one NYC TLC HVFHV month and export a causal request-time trace.

Requires DuckDB (`python -m pip install duckdb`). The trace contains only
request time and zone endpoints plus source-row provenance. Historical pickup
outcomes are deliberately not included in the scheduler input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = ROOT / "data/dispatch/nyc_tlc_hvfhv_2024"
DEFAULT_OUTPUT_ROOT = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024"


def sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def iso(value: Any) -> Any:
    return value.isoformat(sep=" ") if isinstance(value, datetime) else value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--month", type=int, default=1)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()
    if not 1 <= args.month <= 12:
        parser.error("--month must be in 1..12")

    raw = args.data_root / "raw"
    month_file = raw / f"fhvhv_tripdata_{args.year}-{args.month:02d}.parquet"
    year_glob = raw / f"fhvhv_tripdata_{args.year}-*.parquet"
    zone_lookup = raw / "taxi_zone_lookup.csv"
    manifest_path = args.data_root / "manifest.json"
    for required in (month_file, zone_lookup, manifest_path):
        if not required.is_file():
            raise FileNotFoundError(required)

    start = datetime(args.year, args.month, 1)
    if args.month == 12:
        end = datetime(args.year + 1, 1, 1)
    else:
        end = datetime(args.year, args.month + 1, 1)
    start_sql = start.strftime("%Y-%m-%d %H:%M:%S")
    end_sql = end.strftime("%Y-%m-%d %H:%M:%S")
    out_dir = args.output_root / f"audit_{args.year}-{args.month:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / f"request_trace_{args.year}-{args.month:02d}.parquet"
    audit_path = out_dir / f"audit_{args.year}-{args.month:02d}.json"
    if trace_path.exists() or audit_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing audit output: {out_dir}")

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")
    con.execute("SET temp_directory='/tmp/nyc_tlc_duckdb_tmp'")
    month_sql = sql_path(month_file)
    lookup_sql = sql_path(zone_lookup)
    glob_sql = sql_path(year_glob)
    con.execute(
        f"CREATE VIEW month_trips AS SELECT * FROM read_parquet('{month_sql}')"
    )
    schema = [
        {"column": row[0], "type": row[1]}
        for row in con.execute("DESCRIBE month_trips").fetchall()
    ]

    month_row = con.execute(
        f"""
        WITH trips AS (
            SELECT *,
                   date_diff('second', request_datetime, pickup_datetime) AS wait_s,
                   date_diff('second', pickup_datetime, dropoff_datetime) AS observed_trip_s
            FROM month_trips
        ), zones AS (
            SELECT LocationID FROM read_csv_auto('{lookup_sql}')
        )
        SELECT
            count(*) AS row_count,
            min(pickup_datetime) AS pickup_min,
            max(pickup_datetime) AS pickup_max,
            min(request_datetime) AS request_min,
            max(request_datetime) AS request_max,
            count(*) FILTER (WHERE request_datetime IS NULL) AS request_null,
            count(*) FILTER (WHERE pickup_datetime IS NULL) AS pickup_null,
            count(*) FILTER (WHERE dropoff_datetime IS NULL) AS dropoff_null,
            count(*) FILTER (WHERE PULocationID IS NULL) AS pu_zone_null,
            count(*) FILTER (WHERE DOLocationID IS NULL) AS do_zone_null,
            count(*) FILTER (WHERE trip_time IS NULL) AS trip_time_null,
            count(*) FILTER (WHERE trip_miles IS NULL) AS trip_miles_null,
            count(*) FILTER (WHERE shared_match_flag IS NULL) AS shared_match_null,
            count(*) FILTER (WHERE request_datetime > pickup_datetime) AS request_after_pickup,
            max(date_diff('second', pickup_datetime, request_datetime))
                FILTER (WHERE request_datetime > pickup_datetime) AS max_request_after_pickup_s,
            count(*) FILTER (WHERE pickup_datetime >= dropoff_datetime) AS nonpositive_trip_interval,
            count(*) FILTER (WHERE trip_time <= 0) AS nonpositive_trip_time,
            count(*) FILTER (WHERE trip_miles < 0) AS negative_trip_miles,
            count(*) FILTER (WHERE abs(observed_trip_s - trip_time) > 60) AS trip_time_mismatch_over_60s,
            count(*) FILTER (WHERE PULocationID IS NOT NULL AND pu.LocationID IS NULL) AS pu_zone_unmatched,
            count(*) FILTER (WHERE DOLocationID IS NOT NULL AND dz.LocationID IS NULL) AS do_zone_unmatched,
            count(*) FILTER (WHERE shared_match_flag = 'Y') AS shared_match_y,
            count(*) FILTER (WHERE shared_match_flag = 'N') AS shared_match_n,
            count(*) FILTER (WHERE shared_match_flag NOT IN ('Y', 'N')) AS shared_match_other,
            count(*) FILTER (WHERE request_datetime < TIMESTAMP '{start_sql}') AS request_before_month,
            count(*) FILTER (WHERE request_datetime >= TIMESTAMP '{end_sql}') AS request_after_month,
            quantile_cont(wait_s, 0.5) FILTER (WHERE wait_s >= 0) AS request_to_pickup_median_s,
            quantile_cont(wait_s, 0.95) FILTER (WHERE wait_s >= 0) AS request_to_pickup_p95_s,
            max(wait_s) FILTER (WHERE wait_s >= 0) AS request_to_pickup_max_s
        FROM trips
        LEFT JOIN zones pu ON trips.PULocationID = pu.LocationID
        LEFT JOIN zones dz ON trips.DOLocationID = dz.LocationID
        """
    ).fetchone()
    metric_names = [
        "row_count", "pickup_min", "pickup_max", "request_min", "request_max",
        "request_null", "pickup_null", "dropoff_null", "pu_zone_null", "do_zone_null",
        "trip_time_null", "trip_miles_null", "shared_match_null", "request_after_pickup",
        "max_request_after_pickup_s", "nonpositive_trip_interval", "nonpositive_trip_time",
        "negative_trip_miles", "trip_time_mismatch_over_60s", "pu_zone_unmatched",
        "do_zone_unmatched", "shared_match_y", "shared_match_n", "shared_match_other",
        "request_before_month", "request_after_month", "request_to_pickup_median_s",
        "request_to_pickup_p95_s", "request_to_pickup_max_s",
    ]
    month_metrics = {name: iso(value) for name, value in zip(metric_names, month_row)}

    columns = (
        "hvfhs_license_num, dispatching_base_num, originating_base_num, request_datetime, "
        "on_scene_datetime, pickup_datetime, dropoff_datetime, PULocationID, DOLocationID, "
        "trip_miles, trip_time, base_passenger_fare, tolls, bcf, sales_tax, "
        "congestion_surcharge, airport_fee, tips, driver_pay, shared_request_flag, "
        "shared_match_flag, access_a_ride_flag, wav_request_flag, wav_match_flag"
    )
    duplicate_fingerprint_rows = con.execute(
        f"SELECT count(*) - count(DISTINCT hash({columns})) FROM month_trips"
    ).fetchone()[0]

    cohort_source = f"read_parquet('{glob_sql}', filename=true, file_row_number=true)"
    source_file_expr = "'raw/' || regexp_extract(filename, '([^/]+)$', 1)"
    cohort_where = (
        f"request_datetime >= TIMESTAMP '{start_sql}' "
        f"AND request_datetime < TIMESTAMP '{end_sql}'"
    )
    candidate_where = f"""
        {cohort_where}
        AND shared_match_flag = 'N'
        AND request_datetime <= pickup_datetime
        AND pickup_datetime < dropoff_datetime
        AND trip_time > 0 AND trip_miles >= 0
        AND PULocationID IS NOT NULL AND DOLocationID IS NOT NULL
        AND pu_zone.LocationID IS NOT NULL AND do_zone.LocationID IS NOT NULL
    """
    zone_cte = f"""WITH zone_ids AS (
        SELECT LocationID FROM read_csv_auto('{lookup_sql}')
    )"""
    cohort_row = con.execute(
        f"""
        {zone_cte}, cohort AS (
            SELECT trips.* FROM {cohort_source} AS trips
            WHERE {cohort_where}
        )
        SELECT
            count(*) AS row_count,
            count(*) FILTER (WHERE shared_match_flag = 'Y') AS shared_match_y,
            count(*) FILTER (WHERE shared_match_flag = 'N') AS shared_match_n,
            count(*) FILTER (WHERE shared_match_flag NOT IN ('Y', 'N')) AS shared_match_other,
            count(*) FILTER (WHERE request_datetime > pickup_datetime) AS request_after_pickup,
            count(*) FILTER (WHERE pickup_datetime >= dropoff_datetime) AS nonpositive_trip_interval,
            count(*) FILTER (WHERE trip_time <= 0) AS nonpositive_trip_time,
            count(*) FILTER (WHERE trip_miles < 0) AS negative_trip_miles,
            count(*) FILTER (WHERE PULocationID IS NOT NULL AND pu_zone.LocationID IS NULL) AS pu_zone_unmatched,
            count(*) FILTER (WHERE DOLocationID IS NOT NULL AND do_zone.LocationID IS NULL) AS do_zone_unmatched,
            count(*) FILTER (WHERE {candidate_where}) AS candidate_rows,
            min(request_datetime) FILTER (WHERE {candidate_where}) AS candidate_request_min,
            max(request_datetime) FILTER (WHERE {candidate_where}) AS candidate_request_max,
            quantile_cont(date_diff('second', request_datetime, pickup_datetime), 0.5)
                FILTER (WHERE {candidate_where}) AS request_to_pickup_median_s,
            quantile_cont(date_diff('second', request_datetime, pickup_datetime), 0.95)
                FILTER (WHERE {candidate_where}) AS request_to_pickup_p95_s,
            max(date_diff('second', request_datetime, pickup_datetime))
                FILTER (WHERE {candidate_where}) AS request_to_pickup_max_s,
            count(DISTINCT PULocationID) FILTER (WHERE {candidate_where}) AS distinct_pickup_zones,
            count(DISTINCT DOLocationID) FILTER (WHERE {candidate_where}) AS distinct_dropoff_zones,
            count(DISTINCT (PULocationID, DOLocationID)) FILTER (WHERE {candidate_where}) AS distinct_od_pairs
        FROM cohort
        LEFT JOIN zone_ids pu_zone ON cohort.PULocationID = pu_zone.LocationID
        LEFT JOIN zone_ids do_zone ON cohort.DOLocationID = do_zone.LocationID
        """
    ).fetchone()
    cohort_names = [
        "row_count", "shared_match_y", "shared_match_n", "shared_match_other",
        "request_after_pickup", "nonpositive_trip_interval", "nonpositive_trip_time",
        "negative_trip_miles", "pu_zone_unmatched", "do_zone_unmatched", "candidate_rows",
        "candidate_request_min", "candidate_request_max", "request_to_pickup_median_s",
        "request_to_pickup_p95_s", "request_to_pickup_max_s", "distinct_pickup_zones",
        "distinct_dropoff_zones", "distinct_od_pairs",
    ]
    cohort_metrics = {name: iso(value) for name, value in zip(cohort_names, cohort_row)}

    source_files = [
        row[0]
        for row in con.execute(
            f"SELECT DISTINCT {source_file_expr} FROM {cohort_source} WHERE {cohort_where} ORDER BY 1"
        ).fetchall()
    ]
    hourly_counts = [
        {"hour": row[0], "events": row[1]}
        for row in con.execute(
            f"""{zone_cte}, cohort AS (SELECT trips.* FROM {cohort_source} AS trips)
            SELECT hour(request_datetime), count(*)
            FROM cohort
            LEFT JOIN zone_ids pu_zone ON cohort.PULocationID = pu_zone.LocationID
            LEFT JOIN zone_ids do_zone ON cohort.DOLocationID = do_zone.LocationID
            WHERE {candidate_where}
            GROUP BY 1 ORDER BY 1"""
        ).fetchall()
    ]
    top_od = [
        {"pickup_zone_id": row[0], "dropoff_zone_id": row[1], "events": row[2]}
        for row in con.execute(
            f"""{zone_cte}, cohort AS (SELECT trips.* FROM {cohort_source} AS trips)
            SELECT PULocationID, DOLocationID, count(*) AS n
            FROM cohort
            LEFT JOIN zone_ids pu_zone ON cohort.PULocationID = pu_zone.LocationID
            LEFT JOIN zone_ids do_zone ON cohort.DOLocationID = do_zone.LocationID
            WHERE {candidate_where}
            GROUP BY 1, 2 ORDER BY n DESC, PULocationID, DOLocationID LIMIT 20"""
        ).fetchall()
    ]

    con.execute(
        f"""
        COPY (
            {zone_cte}
            SELECT
                {source_file_expr} || '#' || CAST(file_row_number AS VARCHAR) AS source_record_id,
                {source_file_expr} AS source_file,
                file_row_number AS source_row_number,
                request_datetime,
                date_diff('second', TIMESTAMP '{start_sql}', request_datetime) AS arrival_time_sec,
                PULocationID AS pickup_zone_id,
                DOLocationID AS dropoff_zone_id
            FROM {cohort_source} AS trips
            LEFT JOIN zone_ids pu_zone ON trips.PULocationID = pu_zone.LocationID
            LEFT JOIN zone_ids do_zone ON trips.DOLocationID = do_zone.LocationID
            WHERE {candidate_where}
            ORDER BY request_datetime, filename, file_row_number
        ) TO '{sql_path(trace_path)}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_sha = {
        item["file"].split("/")[-1]: item["sha256"]
        for item in manifest["files"]
    }
    source_sha.update(
        {
            item["file"].split("/")[-1]: item["sha256"]
            for item in manifest.get("supporting_files", [])
        }
    )
    audit = {
        "audit_version": "nyc-tlc-hvfhv-month-audit-v1",
        "audited_request_month": f"{args.year:04d}-{args.month:02d}",
        "source_dataset": manifest["dataset"],
        "dataset_id": manifest["dataset_id"],
        "source_page": manifest["source_page"],
        "publisher_download_page": manifest["publisher_download_page"],
        "duckdb_version": duckdb.__version__,
        "source_month_file": str(month_file.relative_to(ROOT)),
        "source_month_sha256": source_sha.get(month_file.name),
        "source_month_file_audit": month_metrics,
        "source_month_full_row_hash_duplicate_fingerprints": duplicate_fingerprint_rows,
        "request_month_cohort_source_files": source_files,
        "request_month_cohort": cohort_metrics,
        "hourly_request_counts_in_trace": hourly_counts,
        "top_20_od_pairs_in_trace": top_od,
        "trace_file": str(trace_path.relative_to(ROOT)),
        "trace_bytes": trace_path.stat().st_size,
        "trace_sha256": sha256_file(trace_path),
        "trace_columns": [
            "source_record_id", "source_file", "source_row_number", "request_datetime",
            "arrival_time_sec", "pickup_zone_id", "dropoff_zone_id",
        ],
        "selection_rules": [
            f"request_datetime in [{start_sql}, {end_sql}) across all {args.year} monthly files",
            "shared_match_flag = N",
            "request_datetime <= pickup_datetime < dropoff_datetime",
            "trip_time > 0 and trip_miles >= 0",
            "pickup and dropoff zone IDs both present in taxi_zone_lookup.csv",
        ],
        "interpretation_limits": [
            "The source records dispatched/completed trips, not all attempted or unserved requests.",
            "The trace contains no actual pickup/dropoff outcome fields; those remain outside scheduler input.",
            "The filter uses ex-post shared-match and chronology fields, so this is a curated historical trace, not an unbiased population of all demand.",
            "Zone IDs are not graph node IDs; a NYC road network and explicit fleet-state assumptions are still required before running the existing simulator.",
            "Timestamps are retained as provided by the source; no timezone conversion is applied.",
        ],
        "schema": schema,
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "audit_path": str(audit_path),
        "trace_path": str(trace_path),
        "source_month_rows": month_metrics["row_count"],
        "candidate_trace_rows": cohort_metrics["candidate_rows"],
        "trace_bytes": audit["trace_bytes"],
        "duplicate_fingerprints": duplicate_fingerprint_rows,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
