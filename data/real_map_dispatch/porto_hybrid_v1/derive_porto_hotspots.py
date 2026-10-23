#!/usr/bin/env python3
"""Aggregate Porto taxi trip endpoints into privacy-preserving spatial cells."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


CELL_SIZE_M = 200.0
REFERENCE_LON = -8.61
REFERENCE_LAT = 41.15
METERS_PER_DEGREE_LAT = 111_320.0


def point_to_cell(lon: float, lat: float) -> tuple[int, int]:
    x_m = (lon - REFERENCE_LON) * METERS_PER_DEGREE_LAT * math.cos(
        math.radians(REFERENCE_LAT)
    )
    y_m = (lat - REFERENCE_LAT) * METERS_PER_DEGREE_LAT
    return math.floor(x_m / CELL_SIZE_M), math.floor(y_m / CELL_SIZE_M)


def cell_center(cell_x: int, cell_y: int) -> tuple[float, float]:
    x_m = (cell_x + 0.5) * CELL_SIZE_M
    y_m = (cell_y + 0.5) * CELL_SIZE_M
    lon = REFERENCE_LON + x_m / (
        METERS_PER_DEGREE_LAT * math.cos(math.radians(REFERENCE_LAT))
    )
    lat = REFERENCE_LAT + y_m / METERS_PER_DEGREE_LAT
    return lon, lat


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parent / "raw/uci/train.csv.zip",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent / "derived/heat",
    )
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"dataset archive does not exist: {args.input}")

    endpoint_counts: Counter[tuple[int, int, str]] = Counter()
    od_counts: Counter[tuple[tuple[int, int], tuple[int, int]]] = Counter()
    totals = Counter()
    with zipfile.ZipFile(args.input) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            parser.error(f"expected exactly one CSV member, found {members}")
        with archive.open(members[0]) as raw_csv:
            text_csv = io.TextIOWrapper(raw_csv, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text_csv)
            required = {"POLYLINE", "MISSING_DATA", "TIMESTAMP"}
            if not required.issubset(reader.fieldnames or []):
                parser.error(f"missing required fields: {sorted(required - set(reader.fieldnames or []))}")
            for row in reader:
                totals["rows_seen"] += 1
                if str(row.get("MISSING_DATA", "")).strip().lower() == "true":
                    totals["missing_data_rows"] += 1
                    continue
                try:
                    path = json.loads(row["POLYLINE"])
                except (TypeError, json.JSONDecodeError):
                    totals["invalid_polyline_rows"] += 1
                    continue
                if not isinstance(path, list) or not path:
                    totals["empty_polyline_rows"] += 1
                    continue
                try:
                    start_lon, start_lat = map(float, path[0])
                    end_lon, end_lat = map(float, path[-1])
                    timestamp = int(row["TIMESTAMP"])
                    if not all(
                        math.isfinite(value)
                        for value in (start_lon, start_lat, end_lon, end_lat)
                    ):
                        raise ValueError("non-finite coordinate")
                    if not (-180 <= start_lon <= 180 and -90 <= start_lat <= 90):
                        raise ValueError("invalid start coordinate")
                    if not (-180 <= end_lon <= 180 and -90 <= end_lat <= 90):
                        raise ValueError("invalid end coordinate")
                except (TypeError, ValueError, IndexError, OverflowError):
                    totals["invalid_endpoint_rows"] += 1
                    continue

                start_cell = point_to_cell(start_lon, start_lat)
                end_cell = point_to_cell(end_lon, end_lat)
                endpoint_counts[(*start_cell, "pickup_proxy")] += 1
                endpoint_counts[(*end_cell, "dropoff_proxy")] += 1
                od_counts[(start_cell, end_cell)] += 1
                # Store coarse UTC hour only as a demand-profile proxy; no trip IDs or taxi IDs.
                hour = datetime.fromtimestamp(timestamp, tz=timezone.utc).hour
                totals[f"utc_hour_{hour:02d}"] += 1
                totals["trips_aggregated"] += 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    hotspot_path = args.output_dir / "porto_hotspot_cells_200m.csv"
    with hotspot_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["cell_x", "cell_y", "center_lon", "center_lat", "endpoint_role", "trip_count"])
        for (cell_x, cell_y, role), count in sorted(
            endpoint_counts.items(), key=lambda item: (item[0][1], item[0][0], item[0][2])
        ):
            lon, lat = cell_center(cell_x, cell_y)
            writer.writerow([cell_x, cell_y, f"{lon:.7f}", f"{lat:.7f}", role, count])

    od_path = args.output_dir / "porto_od_grid_counts_200m.csv"
    with od_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["origin_cell_x", "origin_cell_y", "destination_cell_x", "destination_cell_y", "trip_count"])
        for (origin, destination), count in sorted(
            od_counts.items(), key=lambda item: (item[0][0][1], item[0][0][0], item[0][1][1], item[0][1][0])
        ):
            writer.writerow([*origin, *destination, count])

    metadata = {
        "source_archive": args.input.name,
        "grid": {
            "projection": "local equirectangular approximation centered near Porto",
            "reference_lon": REFERENCE_LON,
            "reference_lat": REFERENCE_LAT,
            "cell_size_m": CELL_SIZE_M,
        },
        "endpoint_semantics": "trip start/end GPS points from completed taxi trajectories; proxies only",
        "time_semantics": "UTC hour bins derived from trip start timestamp; not local service-hour truth",
        "privacy": "trip and taxi identifiers are not written to derived outputs",
        "counts": dict(sorted(totals.items())),
        "outputs": [hotspot_path.name, od_path.name],
    }
    (args.output_dir / "aggregation_manifest.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
