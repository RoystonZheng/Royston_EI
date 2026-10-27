#!/usr/bin/env python3
"""Prepare a frozen multi-month NYC TLC dispatch train/evaluation package.

The package is a controlled simulation input derived from observed, already-
matched FHV trips. Synthetic fleets and a zone-level ride-cost proxy are
explicitly recorded; this is not a replay of actual vehicle availability.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import shutil
import tempfile
import io
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data/dispatch/nyc_tlc_hvfhv_2024"
RAW_ROOT = DATA_ROOT / "raw"
DATASET_MANIFEST = DATA_ROOT / "manifest.json"
BASE_PACKAGE = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1"
BASE_MATRIX = BASE_PACKAGE / "cases/C4_manhattan_790_order_five_minute_load_sweep/zone_od_cost_matrix.csv.gz"
BASE_CENTROIDS = BASE_PACKAGE / "zone_centroids.json"
BASE_PICKUP_POLICY = BASE_PACKAGE / "pickup_cost_policy.json"
DEFAULT_OUTPUT = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_formal_multimonth_v1"
FOCUS_ZONES = (79, 125, 148, 161, 162, 163, 164, 170, 186, 230, 231, 234)
TRAIN_QUANTILES = (0.10, 0.30, 0.50, 0.70, 0.90)
EVAL_QUANTILES = (0.25, 0.50, 0.75)
WINDOW_SECONDS = 300


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_record(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def iso(value: datetime) -> str:
    return value.isoformat(sep=" ")


def sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def verify_sources(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    verified: dict[str, dict[str, Any]] = {}
    for record in [*dataset.get("files", []), *dataset.get("supporting_files", [])]:
        relative = record["file"]
        path = DATA_ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = file_record(path)
        if actual["bytes"] != record["bytes"] or actual["sha256"] != record["sha256"]:
            raise ValueError(f"source file differs from dataset manifest: {relative}")
        verified[relative] = actual
    return verified


def quantile_index(length: int, q: float) -> int:
    if length <= 0:
        raise ValueError("cannot select from an empty sample")
    return int(math.floor(q * (length - 1) + 0.5))


def derive_windows(connection: duckdb.DuckDBPyConnection, raw_glob: str, lookup_path: str, spatial_zone_ids: set[int]) -> list[dict[str, Any]]:
    zone_csv = ",".join(str(zone) for zone in FOCUS_ZONES)
    spatial_zone_csv = ",".join(map(str, sorted(spatial_zone_ids)))
    query = f"""
        WITH zone_ids AS (
            SELECT LocationID FROM read_csv_auto('{lookup_path}')
        ), valid AS (
            SELECT request_datetime, PULocationID AS pickup_zone_id,
                   DOLocationID AS dropoff_zone_id
            FROM read_parquet('{raw_glob}', filename=true, file_row_number=true) AS trips
            JOIN zone_ids AS pu_zone ON trips.PULocationID = pu_zone.LocationID
            JOIN zone_ids AS do_zone ON trips.DOLocationID = do_zone.LocationID
            WHERE shared_match_flag = 'N'
              AND request_datetime <= pickup_datetime
              AND pickup_datetime < dropoff_datetime
              AND trip_time > 0 AND trip_miles >= 0
              AND request_datetime >= TIMESTAMP '2024-01-01 00:00:00'
              AND request_datetime < TIMESTAMP '2025-01-01 00:00:00'
              AND PULocationID IN ({zone_csv})
              AND PULocationID IN ({spatial_zone_csv})
              AND DOLocationID IN ({spatial_zone_csv})
              AND hour(request_datetime) BETWEEN 20 AND 23
        ), bins AS (
            SELECT date_trunc('day', request_datetime)
                     + floor(date_part('hour', request_datetime) * 12
                             + date_part('minute', request_datetime) / 5)
                       * interval '5 minutes' AS window_start,
                   count(*) AS order_count,
                   count(DISTINCT pickup_zone_id) AS origin_zone_count,
                   count(DISTINCT dropoff_zone_id) AS destination_zone_count
            FROM valid
            GROUP BY 1
        ), daily_peaks AS (
            SELECT *, row_number() OVER (
                PARTITION BY CAST(window_start AS DATE)
                ORDER BY order_count DESC, window_start ASC
            ) AS day_rank
            FROM bins
            WHERE origin_zone_count = {len(FOCUS_ZONES)}
        )
        SELECT month(window_start) AS month_num,
               CAST(window_start AS DATE) AS service_date,
               window_start, order_count, origin_zone_count, destination_zone_count
        FROM daily_peaks
        WHERE day_rank = 1 AND order_count >= 250
        ORDER BY month_num, order_count, service_date, window_start
    """
    rows = connection.execute(query).fetchall()
    by_month: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for month_num, service_date, start, count, origins, destinations in rows:
        by_month[int(month_num)].append({
            "month": int(month_num),
            "service_date": service_date.isoformat(),
            "window_start": start,
            "window_end": start + timedelta(seconds=WINDOW_SECONDS),
            "expected_order_count": int(count),
            "origin_zone_count": int(origins),
            "destination_zone_count": int(destinations),
        })

    selected: list[dict[str, Any]] = []
    selections = [
        (month_num, "training", TRAIN_QUANTILES)
        for month_num in (1, 2)
    ] + [
        (month_num, "evaluation", EVAL_QUANTILES)
        for month_num in range(3, 13)
    ]
    for month_num, cohort, quantiles in selections:
        candidates = by_month.get(month_num, [])
        if len(candidates) < len(quantiles):
            raise ValueError(f"not enough independent daily peaks for month {month_num}")
        for q in quantiles:
            item = dict(candidates[quantile_index(len(candidates), q)])
            minute_stamp = item["window_start"].strftime("%H%M%S")
            q_label = int(round(q * 100))
            prefix = "TR" if cohort == "training" else "EV"
            item.update({
                "cohort": cohort,
                "selection_quantile": q,
                "sample_stratum": f"q{q_label:02d}",
                "case_id": f"{prefix}_{item['service_date'].replace('-', '')}_{minute_stamp}_q{q_label:02d}",
                "candidate_daily_peak_count": len(candidates),
            })
            selected.append(item)

    if len({item["service_date"] for item in selected}) != len(selected):
        raise ValueError("selection reused a calendar date; windows must be date-independent")
    training_dates = {item["service_date"] for item in selected if item["cohort"] == "training"}
    evaluation_dates = {item["service_date"] for item in selected if item["cohort"] == "evaluation"}
    if training_dates & evaluation_dates:
        raise ValueError("training and evaluation dates overlap")
    return selected


def extract_events(
    connection: duckdb.DuckDBPyConnection,
    raw_glob: str,
    lookup_path: str,
    windows: list[dict[str, Any]],
    spatial_zone_ids: set[int],
) -> dict[str, list[dict[str, Any]]]:
    connection.execute("DROP TABLE IF EXISTS selected_windows")
    connection.execute("""CREATE TEMP TABLE selected_windows (
        case_id VARCHAR, window_start TIMESTAMP, window_end TIMESTAMP
    )""")
    connection.executemany(
        "INSERT INTO selected_windows VALUES (?, ?, ?)",
        [(item["case_id"], item["window_start"], item["window_end"]) for item in windows],
    )
    zone_csv = ",".join(str(zone) for zone in FOCUS_ZONES)
    spatial_zone_csv = ",".join(map(str, sorted(spatial_zone_ids)))
    query = f"""
        WITH zone_ids AS (SELECT LocationID FROM read_csv_auto('{lookup_path}')
        )
        SELECT selected.case_id,
               'raw/' || regexp_extract(trips.filename, '([^/]+)$', 1)
                   || '#' || CAST(trips.file_row_number AS VARCHAR) AS source_record_id,
               'raw/' || regexp_extract(trips.filename, '([^/]+)$', 1) AS source_file,
               trips.file_row_number AS source_row_number,
               trips.request_datetime,
               trips.PULocationID AS pickup_zone_id,
               trips.DOLocationID AS dropoff_zone_id
        FROM read_parquet('{raw_glob}', filename=true, file_row_number=true) AS trips
        JOIN selected_windows AS selected
          ON trips.request_datetime >= selected.window_start
         AND trips.request_datetime < selected.window_end
        JOIN zone_ids AS pu_zone ON trips.PULocationID = pu_zone.LocationID
        JOIN zone_ids AS do_zone ON trips.DOLocationID = do_zone.LocationID
        WHERE trips.shared_match_flag = 'N'
          AND trips.request_datetime <= trips.pickup_datetime
          AND trips.pickup_datetime < trips.dropoff_datetime
          AND trips.trip_time > 0 AND trips.trip_miles >= 0
          AND trips.request_datetime >= TIMESTAMP '2024-01-01 00:00:00'
          AND trips.request_datetime < TIMESTAMP '2025-01-01 00:00:00'
          AND trips.PULocationID IN ({zone_csv})
          AND trips.PULocationID IN ({spatial_zone_csv})
          AND trips.DOLocationID IN ({spatial_zone_csv})
        ORDER BY selected.case_id, trips.request_datetime, source_record_id
    """
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case_id, record_id, source_file, row_number, request_time, pickup, dropoff in connection.execute(query).fetchall():
        start = next(item["window_start"] for item in windows if item["case_id"] == case_id)
        output[case_id].append({
            "order_id": record_id,
            "source_record_id": record_id,
            "source_file": source_file,
            "source_row_number": int(row_number),
            "request_datetime": request_time,
            "arrival_time_sec": round((request_time - start).total_seconds(), 6),
            "pickup_zone_id": int(pickup),
            "dropoff_zone_id": int(dropoff),
        })
    return output


def read_centroids(path: Path) -> dict[int, tuple[float, float]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(zone): (float(point["lon"]), float(point["lat"]))
        for zone, point in payload.items()
    }


def official_polygon_zone_ids(zones_zip: Path) -> set[int]:
    """Return location IDs with polygon geometry in the official TLC zip."""
    try:
        import shapefile
    except ImportError as error:
        raise RuntimeError("reading official taxi-zone polygons requires pyshp") from error
    with zipfile.ZipFile(zones_zip) as archive:
        stem = "taxi_zones/taxi_zones"
        reader = shapefile.Reader(
            shp=io.BytesIO(archive.read(stem + ".shp")),
            shx=io.BytesIO(archive.read(stem + ".shx")),
            dbf=io.BytesIO(archive.read(stem + ".dbf")),
        )
        fields = [field[0] for field in reader.fields[1:]]
        return {
            int(dict(zip(fields, feature.record))["LocationID"])
            for feature in reader.iterShapeRecords()
        }


def audit_unmapped_selected_trips(
    connection: duckdb.DuckDBPyConnection,
    raw_glob: str,
    lookup_path: str,
    windows: list[dict[str, Any]],
    spatial_zone_ids: set[int],
) -> dict[str, int]:
    """Count eligible selected-window records without official polygon zones."""
    connection.execute("DROP TABLE IF EXISTS selected_windows")
    connection.execute("CREATE TEMP TABLE selected_windows (case_id VARCHAR, window_start TIMESTAMP, window_end TIMESTAMP)")
    connection.executemany(
        "INSERT INTO selected_windows VALUES (?, ?, ?)",
        [(item["case_id"], item["window_start"], item["window_end"]) for item in windows],
    )
    zone_csv = ",".join(str(zone) for zone in FOCUS_ZONES)
    spatial_zone_csv = ",".join(map(str, sorted(spatial_zone_ids)))
    lookup = sql_path(Path(lookup_path))
    rows = connection.execute(f"""
        WITH zone_ids AS (SELECT LocationID FROM read_csv_auto('{lookup}'))
        SELECT selected.case_id, count(*)
        FROM read_parquet('{raw_glob}', filename=true, file_row_number=true) AS trips
        JOIN selected_windows AS selected
          ON trips.request_datetime >= selected.window_start
         AND trips.request_datetime < selected.window_end
        JOIN zone_ids AS pu_zone ON trips.PULocationID = pu_zone.LocationID
        JOIN zone_ids AS do_zone ON trips.DOLocationID = do_zone.LocationID
        WHERE trips.shared_match_flag = 'N'
          AND trips.request_datetime <= trips.pickup_datetime
          AND trips.pickup_datetime < trips.dropoff_datetime
          AND trips.trip_time > 0 AND trips.trip_miles >= 0
          AND trips.request_datetime >= TIMESTAMP '2024-01-01 00:00:00'
          AND trips.request_datetime < TIMESTAMP '2025-01-01 00:00:00'
          AND trips.PULocationID IN ({zone_csv})
          AND (trips.PULocationID NOT IN ({spatial_zone_csv})
               OR trips.DOLocationID NOT IN ({spatial_zone_csv}))
        GROUP BY selected.case_id
    """).fetchall()
    return {str(case_id): int(count) for case_id, count in rows}


def fill_missing_official_centroids(
    centroids: dict[int, tuple[float, float]],
    needed_zone_ids: set[int],
    zones_zip: Path,
) -> dict[str, Any]:
    """Fill absent zone centroids from the official TLC polygon shapefile.

    The base benchmark centroid file is retained for existing zones. Missing
    destinations are derived from polygon centroids in the source shapefile's
    projected CRS and transformed to WGS84 for the shared haversine policy.
    """
    missing = sorted(needed_zone_ids - centroids.keys())
    if not missing:
        return {"filled_zone_ids": [], "source_sha256": sha256_file(zones_zip)}
    try:
        import shapefile
        from pyproj import CRS, Transformer
        from shapely.geometry import shape as make_shape
    except ImportError as error:
        raise RuntimeError(
            "filling missing official taxi-zone centroids requires pyshp, pyproj, and shapely"
        ) from error

    with zipfile.ZipFile(zones_zip) as archive:
        stem = "taxi_zones/taxi_zones"
        reader = shapefile.Reader(
            shp=io.BytesIO(archive.read(stem + ".shp")),
            shx=io.BytesIO(archive.read(stem + ".shx")),
            dbf=io.BytesIO(archive.read(stem + ".dbf")),
        )
        fields = [field[0] for field in reader.fields[1:]]
        source_crs = CRS.from_wkt(archive.read(stem + ".prj").decode("utf-8"))
        transformer = Transformer.from_crs(source_crs, CRS.from_epsg(4326), always_xy=True)
        missing_set = set(missing)
        for feature in reader.iterShapeRecords():
            record = dict(zip(fields, feature.record))
            zone_id = int(record["LocationID"])
            if zone_id not in missing_set:
                continue
            polygon_centroid = make_shape(feature.shape.__geo_interface__).centroid
            longitude, latitude = transformer.transform(polygon_centroid.x, polygon_centroid.y)
            if not math.isfinite(longitude) or not math.isfinite(latitude):
                raise ValueError(f"non-finite polygon centroid for taxi zone {zone_id}")
            centroids[zone_id] = (longitude, latitude)
            missing_set.remove(zone_id)
        if missing_set:
            raise ValueError("official taxi-zone shapefile has no polygons for zones: " + ", ".join(map(str, sorted(missing_set))))
    return {
        "filled_zone_ids": missing,
        "source_file": "raw/taxi_zones.zip",
        "source_sha256": sha256_file(zones_zip),
        "method": "polygon centroid in shapefile CRS transformed to EPSG:4326 using pyproj; shapefile coordinates use official TLC Taxi Zones polygons",
    }


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(min(1.0, value)))


def make_augmented_cost_matrix(
    output_path: Path,
    base_matrix: Path,
    windows: list[dict[str, Any]],
    events_by_case: dict[str, list[dict[str, Any]]],
    centroids: dict[int, tuple[float, float]],
    pickup_policy: dict[str, Any],
) -> dict[str, Any]:
    required_pairs = {
        (row["pickup_zone_id"], row["dropoff_zone_id"])
        for rows in events_by_case.values() for row in rows
    }
    observed: dict[tuple[int, int], dict[str, str]] = {}
    with gzip.open(base_matrix, "rt", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        fields = list(reader.fieldnames or [])
        for row in reader:
            pair = (int(row["pickup_zone_id"]), int(row["dropoff_zone_id"]))
            observed[pair] = row
    missing = sorted(required_pairs - observed.keys())
    fallbacks = []
    detour = float(pickup_policy["detour_factor"])
    speed = float(pickup_policy["speed_mps"])
    minimum_m = float(pickup_policy["minimum_intrazonal_distance_m"])
    for origin, destination in missing:
        if origin not in centroids or destination not in centroids:
            raise ValueError(f"no centroid available for missing zone OD pair {origin}->{destination}")
        distance_m = minimum_m if origin == destination else haversine_m(centroids[origin], centroids[destination]) * detour
        duration_s = distance_m / speed
        fallback = {
            "pickup_zone_id": str(origin),
            "dropoff_zone_id": str(destination),
            "median_trip_time_s": f"{duration_s:.6f}",
            "median_trip_miles": f"{distance_m / 1609.344:.6f}",
            "source_trip_count": "0",
            "cost_source": "taxi_zone_centroid_fallback_v1",
        }
        observed[(origin, destination)] = fallback
        fallbacks.append({
            "pickup_zone_id": origin,
            "dropoff_zone_id": destination,
            "distance_m": round(distance_m, 6),
            "duration_sec": round(duration_s, 6),
            "cost_source": fallback["cost_source"],
        })

    fields = [*fields, "cost_source"] if "cost_source" not in fields else fields
    with gzip.open(output_path, "wt", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for pair in sorted(observed):
            row = dict(observed[pair])
            row["cost_source"] = row.get("cost_source") or "observed_training_period_median"
            writer.writerow(row)
    return {
        "source_matrix_sha256": sha256_file(base_matrix),
        "augmented_matrix_sha256": sha256_file(output_path),
        "required_order_od_pair_count": len(required_pairs),
        "observed_order_od_pair_count": len(required_pairs) - len(missing),
        "centroid_fallback_pair_count": len(fallbacks),
        "centroid_fallback_pairs": fallbacks,
        "fallback_policy": {
            "cost_source": "taxi_zone_centroid_fallback_v1",
            "distance": "haversine between official taxi-zone polygon area centroids multiplied by the shared detour factor; minimum intrazonal distance applies within a zone",
            "speed_mps": speed,
            "detour_factor": detour,
            "minimum_intrazonal_distance_m": minimum_m,
            "source_trip_count": 0,
        },
    }


def fleet_variants(case_id: str) -> list[dict[str, Any]]:
    output = []
    for fleet_id, copies_per_zone in (("F1", 1), ("F2", 2), ("F4", 4)):
        zones = [zone for zone in FOCUS_ZONES for _ in range(copies_per_zone)]
        output.append({
            "fleet_id": fleet_id,
            "vehicle_count": len(zones),
            "positions_by_zone": {str(zone): copies_per_zone for zone in FOCUS_ZONES},
            "vehicles": [
                {
                    "vehicle_id": f"{case_id}-{fleet_id}-{index:03d}",
                    "initial_position_node_id": f"zone_{zone}",
                    "position_node_id": f"zone_{zone}",
                }
                for index, zone in enumerate(zones, start=1)
            ],
            "initial_same_zone_candidate_count_per_order": "count of synthetic vehicles placed in that order's pickup zone",
        })
    return output


def write_parquet(connection: duckdb.DuckDBPyConnection, path: Path, case_id: str, rows: list[dict[str, Any]]) -> None:
    connection.execute("DROP TABLE IF EXISTS prepared_orders")
    connection.execute("""CREATE TEMP TABLE prepared_orders (
        scenario_id VARCHAR, order_id VARCHAR, source_record_id VARCHAR,
        source_file VARCHAR, source_row_number BIGINT, request_datetime TIMESTAMP,
        arrival_time_sec DOUBLE, pickup_zone_id INTEGER, dropoff_zone_id INTEGER
    )""")
    values = [(
        case_id, row["order_id"], row["source_record_id"], row["source_file"],
        row["source_row_number"], row["request_datetime"], row["arrival_time_sec"],
        row["pickup_zone_id"], row["dropoff_zone_id"],
    ) for row in rows]
    connection.executemany("INSERT INTO prepared_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    connection.execute(f"COPY prepared_orders TO '{sql_path(path)}' (FORMAT PARQUET, COMPRESSION ZSTD)")


def build_package(output_dir: Path, windows: list[dict[str, Any]], events_by_case: dict[str, list[dict[str, Any]]], connection: duckdb.DuckDBPyConnection, source_records: dict[str, dict[str, Any]], dataset: dict[str, Any]) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing package: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.building-", dir=output_dir.parent))
    try:
        centroids = read_centroids(BASE_CENTROIDS)
        needed_zone_ids = {
            zone_id
            for rows in events_by_case.values()
            for row in rows
            for zone_id in (row["pickup_zone_id"], row["dropoff_zone_id"])
        }
        centroid_audit = fill_missing_official_centroids(
            centroids, needed_zone_ids, RAW_ROOT / "taxi_zones.zip"
        )
        pickup_policy = json.loads(BASE_PICKUP_POLICY.read_text(encoding="utf-8"))
        matrix_audit = make_augmented_cost_matrix(
            stage / "zone_od_cost_matrix.csv.gz", BASE_MATRIX, windows,
            events_by_case, centroids, pickup_policy,
        )
        (stage / "zone_centroids.json").write_text(
            json.dumps({str(zone): {"lon": point[0], "lat": point[1]} for zone, point in sorted(centroids.items())}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        shutil.copy2(BASE_PICKUP_POLICY, stage / "pickup_cost_policy.json")

        cases: dict[str, Any] = {}
        all_order_ids: set[str] = set()
        cohort_order_ids: dict[str, set[str]] = {"training": set(), "evaluation": set()}
        for window in windows:
            case_id = window["case_id"]
            orders = events_by_case.get(case_id, [])
            if len(orders) != window["expected_order_count"]:
                raise ValueError(
                    f"{case_id} row count mismatch: expected {window['expected_order_count']}, got {len(orders)}"
                )
            ids = [row["order_id"] for row in orders]
            if len(ids) != len(set(ids)) or all_order_ids.intersection(ids):
                raise ValueError(f"duplicate order ID in selected window: {case_id}")
            all_order_ids.update(ids)
            cohort_order_ids[window["cohort"]].update(ids)
            case_dir = stage / "cases" / case_id
            case_dir.mkdir(parents=True)
            order_rows = [
                {
                    "order_id": row["order_id"],
                    "arrival_time_sec": row["arrival_time_sec"],
                    "pickup_node_id": f"zone_{row['pickup_zone_id']}",
                    "dropoff_node_id": f"zone_{row['dropoff_zone_id']}",
                    "hotspot_id": case_id,
                    "passenger_no_show": False,
                }
                for row in orders
            ]
            order_json = case_dir / "orders.json"
            order_payload = {
                "scenario_id": case_id,
                "scenario_title": f"{window['cohort']} {window['service_date']} evening peak {window['sample_stratum']}",
                "source_request_window": [iso(window["window_start"]), iso(window["window_end"])],
                "order_count": len(order_rows),
                "orders": order_rows,
            }
            order_json.write_text(json.dumps(order_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            parquet_path = case_dir / "orders.parquet"
            write_parquet(connection, parquet_path, case_id, orders)
            matrix_path = case_dir / "zone_od_cost_matrix.csv.gz"
            shutil.copy2(stage / "zone_od_cost_matrix.csv.gz", matrix_path)
            source_month = int(window["window_start"].month)
            source_file = f"raw/fhvhv_tripdata_2024-{source_month:02d}.parquet"
            scenario = {
                "scenario_id": case_id,
                "title": order_payload["scenario_title"],
                "source_dataset": "NYC TLC 2024 High-Volume FHV, matched trip request proxy",
                "source_request_window": [iso(window["window_start"]), iso(window["window_end"])],
                "request_trace_row_count": len(order_rows),
                "origin_zone_counts": {
                    str(zone): sum(1 for row in orders if row["pickup_zone_id"] == zone)
                    for zone in FOCUS_ZONES
                },
                "distinct_origin_zone_count": len({row["pickup_zone_id"] for row in orders}),
                "distinct_destination_zone_count": len({row["dropoff_zone_id"] for row in orders}),
                "fleet_variants": fleet_variants(case_id),
                "dispatch_controls": {
                    "max_pickup_wait_sec": 300.0,
                    "passenger_no_show_rate": 0.0,
                    "one_to_one_assignment": True,
                    "time_origin": iso(window["window_start"]),
                    "horizon_sec": WINDOW_SECONDS,
                },
                "sampling": {
                    "cohort": window["cohort"],
                    "month": source_month,
                    "service_date": window["service_date"],
                    "sample_stratum": window["sample_stratum"],
                    "daily_peak_quantile": window["selection_quantile"],
                    "daily_peak_candidate_count": window["candidate_daily_peak_count"],
                    "selection_rule": "one maximum-count five-minute window per service date, 20:00-23:59, exactly 12 focus pickup zones, at least 250 requests; quantile rank is round(q*(n-1)) after sorting daily peaks by count/date/time",
                    "expected_order_count_from_window_preflight": window["expected_order_count"],
                },
                "source_file_sha256": source_records[source_file]["sha256"],
                "cost_matrix_policy": "January 1-15 observed zone OD medians; missing selected evaluation OD pairs are explicitly tagged centroid fallback rows",
                "limitations": [
                    "TLC records represent already matched/completed trips, not all attempted or unserved requests.",
                    "Vehicle IDs, fleet sizes, and initial positions are synthetic; each vehicle starts in one of the 12 focus pickup zones.",
                    "The zone OD matrix is a zone-level cost proxy, not a road network or observed empty-vehicle ETA.",
                    "Fallback cost rows are centroid estimates, not observed trip costs; their source is recorded per row.",
                    "Results support controlled comparative simulation only, not NYC-wide service or deployment claims.",
                ],
                "input_files": {},
            }
            scenario_path = case_dir / "scenario.json"
            scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            # A file cannot contain a stable hash of itself. Record the other
            # immutable inputs here; scenario.json is hashed in the package manifest.
            for name, path in (("orders.json", order_json), ("orders.parquet", parquet_path),
                               ("zone_od_cost_matrix.csv.gz", matrix_path)):
                scenario["input_files"][name] = {"file": name, **file_record(path)}
            scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            cases[case_id] = {
                "path": f"cases/{case_id}",
                "scenario_id": case_id,
                "request_count": len(order_rows),
                "fleet_ids": ["F1", "F2", "F4"],
                "cohort": window["cohort"],
                "selection_quantile": window["selection_quantile"],
                "source_request_window": scenario["source_request_window"],
                "files": {
                    name: {"path": name, **file_record(case_dir / name)}
                    for name in ("scenario.json", "orders.json", "orders.parquet", "zone_od_cost_matrix.csv.gz")
                },
            }
        if cohort_order_ids["training"] & cohort_order_ids["evaluation"]:
            raise ValueError("training/evaluation order IDs overlap")

        base_manifest = json.loads((BASE_PACKAGE / "benchmark_manifest.json").read_text(encoding="utf-8"))
        manifest = {
            "schema_version": "nyc-tlc-shared-dispatch-input-v1",
            "package_kind": "frozen_multi_month_dispatch_training_evaluation_package",
            "created_date": "2026-10-24",
            "experiment_role": "30 independent date-level evaluation windows with 10 earlier training windows",
            "source_dataset": dataset["dataset"],
            "source_dataset_id": dataset["dataset_id"],
            "source_dataset_manifest_sha256": sha256_file(DATASET_MANIFEST),
            "source_manifest_files_verified": source_records,
            "zone_centroids": {
                "path": "zone_centroids.json",
                **file_record(stage / "zone_centroids.json"),
            },
            "pickup_cost_policy": {
                "path": "pickup_cost_policy.json",
                **file_record(stage / "pickup_cost_policy.json"),
            },
            "raw_cost_matrix_augmentation": matrix_audit,
            "official_centroid_augmentation": centroid_audit,
            "spatially_unlocated_trip_exclusions": {
                "reason": "eligible records with an endpoint lacking an official TLC taxi-zone polygon are excluded before window selection because no reproducible centroid or OD cost exists",
                "selected_window_counts": {item["case_id"]: item.get("unmapped_zone_excluded_count", 0) for item in windows},
                "selected_window_total": sum(item.get("unmapped_zone_excluded_count", 0) for item in windows),
            },
            "common_runtime_contract": base_manifest.get("common_runtime_contract"),
            "pickup_cost_proxy": base_manifest.get("pickup_cost_proxy"),
            "method_matrix": [
                "D0_greedy", "D1_optimizer", "D2_agent_optimizer",
                "D5_pure_llm_direct", "D6_agent_codex_context_optimizer",
                "D7_agent_codex_capacity_shield_optimizer",
                "D8_agent_codex_nn_capacity_shield_optimizer",
            ],
            "sampling": {
                "focus_pickup_zones": list(FOCUS_ZONES),
                "time_of_day_local": "20:00-23:59",
                "window_duration_sec": WINDOW_SECONDS,
                "daily_window_rule": "maximum request count among five-minute windows per date, tied windows choose earliest start; require all 12 focus origins and at least 250 requests",
                "training_months": [1, 2],
                "training_quantiles_per_month": list(TRAIN_QUANTILES),
                "evaluation_months": list(range(3, 13)),
                "evaluation_quantiles_per_month": list(EVAL_QUANTILES),
                "evaluation_unit": "calendar service date; exactly one window per selected date",
                "date_split": "training Jan-Feb 2024, evaluation Mar-Dec 2024",
                "quantile_rule": "nearest rank index round(q*(n-1)) on monthly eligible daily peak counts sorted by count then date and start time",
            },
            "cases": cases,
            "training_case_ids": [item["case_id"] for item in windows if item["cohort"] == "training"],
            "evaluation_case_ids": [item["case_id"] for item in windows if item["cohort"] == "evaluation"],
            "training_order_count": len(cohort_order_ids["training"]),
            "evaluation_order_count": len(cohort_order_ids["evaluation"]),
            "training_evaluation_order_id_overlap": 0,
            "interpretation": "This package supports a controlled, date-separated comparative simulation using a selected Manhattan evening-peak cohort. It is not a random sample of all NYC trips and cannot establish real citywide service performance.",
            "license_note": dataset.get("license_status"),
        }
        manifest_path = stage / "benchmark_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / "window_selection.json").write_text(
            json.dumps(windows, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        stage.rename(output_dir)
        return {
            "package_path": str(output_dir),
            "manifest_path": str(output_dir / "benchmark_manifest.json"),
            "window_count": len(windows),
            "training_window_count": len(manifest["training_case_ids"]),
            "evaluation_window_count": len(manifest["evaluation_case_ids"]),
            "training_order_count": manifest["training_order_count"],
            "evaluation_order_count": manifest["evaluation_order_count"],
            "od_matrix": matrix_audit,
            "training_evaluation_order_id_overlap": 0,
            "manifest_sha256": sha256_file(output_dir / "benchmark_manifest.json"),
        }
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing package: {output_dir}")
    dataset = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
    source_records = verify_sources(dataset)
    raw_glob = sql_path(RAW_ROOT / "fhvhv_tripdata_2024-*.parquet")
    lookup_path = sql_path(RAW_ROOT / "taxi_zone_lookup.csv")
    connection = duckdb.connect()
    connection.execute("SET threads=4")
    connection.execute("SET memory_limit='4GB'")
    connection.execute("SET temp_directory='/tmp/nyc_tlc_duckdb_tmp'")
    spatial_zone_ids = official_polygon_zone_ids(RAW_ROOT / "taxi_zones.zip")
    windows = derive_windows(connection, raw_glob, lookup_path, spatial_zone_ids)
    excluded_counts = audit_unmapped_selected_trips(
        connection, raw_glob, lookup_path, windows, spatial_zone_ids
    )
    for window in windows:
        window["unmapped_zone_excluded_count"] = excluded_counts.get(window["case_id"], 0)
    events_by_case = extract_events(connection, raw_glob, lookup_path, windows, spatial_zone_ids)
    if set(events_by_case) != {item["case_id"] for item in windows}:
        missing = sorted({item["case_id"] for item in windows} - set(events_by_case))
        raise ValueError("selected cases have no extracted events: " + ", ".join(missing))
    result = build_package(output_dir.resolve(), windows, events_by_case, connection, source_records, dataset)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
