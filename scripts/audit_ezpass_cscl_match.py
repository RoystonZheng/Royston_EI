#!/usr/bin/env python3
"""Preflight geometry/name matching of NYC DOT E-ZPass links to Manhattan CSCL.

Uses only the Python standard library. This is a matching audit, not a router or
an assignment of observed travel times to CSCL edges.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EZPASS_PATH = ROOT / "data/dispatch/nyc_dot_ezpass_2021_2024/snapshots_2024-01_C4_v1/c4_2024-01-20_23-20-59.json"
CSCL_PATH = ROOT / "data/dispatch/nyc_dot_ezpass_2021_2024/cscl_manhattan_2026-10-20/manhattan_centerline.json"
OUT_DIR = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_match_preflight_v1"

LAT0 = 40.76
METERS_PER_DEGREE = 111_320.0
X_SCALE = METERS_PER_DEGREE * math.cos(math.radians(LAT0))
CELL_M = 100.0
SAMPLE_SPACING_M = 15.0
MATCH_RADIUS_M = 25.0

WORD_MAP = {
    "STREET": "ST", "AVENUE": "AVE", "DRIVE": "DR", "BOULEVARD": "BLVD",
    "PARKWAY": "PKWY", "ROAD": "RD", "PLACE": "PL", "TERRACE": "TER",
    "COURT": "CT", "LANE": "LN", "HIGHWAY": "HWY", "EXPRESSWAY": "EXPY",
    "TURNPIKE": "TPKE", "SQUARE": "SQ", "CIRCLE": "CIR", "BRIDGE": "BR",
    "TUNNEL": "TUN", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
ORDINAL_RE = re.compile(r"\b(\d+)(?:ST|ND|RD|TH)\b")
TOKEN_RE = re.compile(r"[A-Z0-9]+")
SENSOR_DIRECTION_SPLIT_RE = re.compile(
    r"\s*[-–—]\s*(?=(?:EASTBOUND|WESTBOUND|NORTHBOUND|SOUTHBOUND|EB|WB|NB|SB)\b)",
    re.IGNORECASE,
)


def normalize_name(value: str) -> str:
    value = ORDINAL_RE.sub(r"\1", (value or "").upper())
    value = value.replace("STREEET", "STREET")
    tokens = [WORD_MAP.get(tok, tok) for tok in TOKEN_RE.findall(value)]
    return " ".join(tokens)


def name_keys(value: str, sensor: bool = False) -> set[str]:
    # DOT sensor names append direction and cross streets after " - ".
    core = SENSOR_DIRECTION_SPLIT_RE.split(value, maxsplit=1)[0] if sensor else value
    if sensor:
        core = re.sub(r"\s+(?:NB|SB|EB|WB)\b.*$", "", core, flags=re.IGNORECASE)
        core = re.sub(r"\s+(?:EXIT|ENTRANCE)\s+\d+.*$", "", core, flags=re.IGNORECASE)
    key = normalize_name(core)
    keys = {key} if key else set()
    # Cardinal direction prefixes distinguish East/West blocks in CSCL. A DOT
    # link may omit that prefix (e.g. "42nd Street"), so retain a second key.
    parts = key.split()
    if len(parts) > 1 and parts[0] in {"N", "S", "E", "W"}:
        keys.add(" ".join(parts[1:]))
    if key in {"FDR DR", "FRANKLIN D ROOSEVELT DR", "FRANKLIN DELANO ROOSEVELT DR"}:
        keys.update({"FDR DR", "FRANKLIN D ROOSEVELT DR", "FRANKLIN DELANO ROOSEVELT DR"})
    if key == "6 AVE":
        keys.add("AVE OF THE AMERICAS")
    if key == "AVE OF THE AMERICAS":
        keys.add("6 AVE")
    if key == "9 AVE":
        keys.add("COLUMBUS AVE")
    return keys


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    values: list[int] = []
    i = 0
    while i < len(encoded):
        result = 0
        shift = 0
        while True:
            b = ord(encoded[i]) - 63
            i += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        values.append(~(result >> 1) if result & 1 else result >> 1)
    lat = lon = 0
    points = []
    for i in range(0, len(values), 2):
        lat += values[i]
        lon += values[i + 1]
        points.append((lon / 1e5, lat / 1e5))
    return points


def project(point: tuple[float, float]) -> tuple[float, float]:
    lon, lat = point
    return lon * X_SCALE, lat * METERS_PER_DEGREE


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def point_segment_distance(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom = dx * dx + dy * dy
    if denom == 0:
        return distance(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / denom))
    q = (a[0] + t * dx, a[1] + t * dy)
    return distance(p, q)


def densify(points: list[tuple[float, float]], spacing: float) -> list[tuple[float, float]]:
    if len(points) < 2:
        return points[:]
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        d = distance(a, b)
        n = max(1, math.ceil(d / spacing))
        for i in range(1, n + 1):
            t = i / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def cell(point: tuple[float, float]) -> tuple[int, int]:
    return math.floor(point[0] / CELL_M), math.floor(point[1] / CELL_M)


def percentile(values: list[float], pct: float) -> float | None:
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * pct
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def read_inputs():
    ezpass = json.loads(EZPASS_PATH.read_text())
    cscl = json.loads(CSCL_PATH.read_text())
    return ezpass, cscl


def build_index(cscl_rows):
    segments: list[dict] = []
    by_cell: dict[tuple[int, int], list[int]] = defaultdict(list)
    feature_by_id = {}
    for row in cscl_rows:
        # Exclude pedestrian-only and unclassified features from vehicle matching.
        if row.get("trafdir") not in {"FT", "TF", "TW"}:
            continue
        fid = str(row.get("physicalid", ""))
        keys = name_keys(row.get("full_street_name", ""))
        feature_by_id[fid] = row
        coords = row.get("the_geom", {}).get("coordinates", [])
        for part_idx, part in enumerate(coords):
            projected = [project((pt[0], pt[1])) for pt in part]
            for seg_idx, (a, b) in enumerate(zip(projected, projected[1:])):
                if distance(a, b) < 0.01:
                    continue
                rec = {"fid": fid, "keys": keys, "a": a, "b": b, "part": part_idx, "index": seg_idx}
                seg_id = len(segments)
                segments.append(rec)
                minx, maxx = sorted((a[0], b[0]))
                miny, maxy = sorted((a[1], b[1]))
                for cx in range(math.floor(minx / CELL_M), math.floor(maxx / CELL_M) + 1):
                    for cy in range(math.floor(miny / CELL_M), math.floor(maxy / CELL_M) + 1):
                        by_cell[(cx, cy)].append(seg_id)
    return segments, by_cell, feature_by_id


def nearby_segments(point, by_cell, radius_cells=1):
    cx, cy = cell(point)
    found = set()
    for x in range(cx - radius_cells, cx + radius_cells + 1):
        for y in range(cy - radius_cells, cy + radius_cells + 1):
            found.update(by_cell.get((x, y), ()))
    return found


def candidate_distances(point, keys, segments, ids) -> tuple[float, float]:
    same_name = float("inf")
    any_name = float("inf")
    for sid in ids:
        seg = segments[sid]
        d = point_segment_distance(point, seg["a"], seg["b"])
        if d < any_name:
            any_name = d
        if keys.intersection(seg["keys"]) and d < same_name:
            same_name = d
    return same_name, any_name


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ezpass_rows, cscl_rows = read_inputs()
    segments, by_cell, feature_by_id = build_index(cscl_rows)
    results = []
    matched_feature_ids_valid = set()
    valid_borough_counts = Counter()
    match_status_counts = Counter()
    trafdir_matched = Counter()

    for sensor in ezpass_rows:
        if sensor.get("borough") != "Manhattan":
            continue
        points = [project(p) for p in decode_polyline(sensor.get("polyline", ""))]
        samples = densify(points, SAMPLE_SPACING_M)
        keys = name_keys(sensor.get("link_name", ""), sensor=True)
        same_dists, any_dists = [], []
        per_sample_feature = []
        for p in samples:
            ids = nearby_segments(p, by_cell)
            same_d, any_d = candidate_distances(p, keys, segments, ids)
            same_dists.append(same_d)
            any_dists.append(any_d)
            best_fid = None
            best_dist = float("inf")
            best_segment = None
            for sid in ids:
                seg = segments[sid]
                if not keys.intersection(seg["keys"]):
                    continue
                d = point_segment_distance(p, seg["a"], seg["b"])
                if d < best_dist:
                    best_dist, best_fid, best_segment = d, seg["fid"], seg
            per_sample_feature.append((best_fid if best_dist <= MATCH_RADIUS_M else None, best_dist, best_segment))

        finite_same = [d for d in same_dists if math.isfinite(d)]
        finite_any = [d for d in any_dists if math.isfinite(d)]
        match_coverage = sum(d <= MATCH_RADIUS_M for d in same_dists) / max(1, len(same_dists))
        any_coverage = sum(d <= MATCH_RADIUS_M for d in any_dists) / max(1, len(any_dists))
        median_d = percentile(finite_same, 0.5)
        p90_d = percentile(finite_same, 0.9)
        valid_time = int(sensor.get("n_samples") or 0) > 0 and float(sensor.get("median_tt_sec") or 0) > 0
        if match_coverage >= 0.90 and (p90_d or 0) <= MATCH_RADIUS_M:
            status = "name_geometry_match"
        elif any_coverage >= 0.90:
            status = "geometry_only_name_mismatch"
        elif match_coverage > 0:
            status = "partial_name_geometry_match"
        else:
            status = "unmatched"
        match_status_counts[("valid" if valid_time else "no_cost", status)] += 1
        if valid_time:
            valid_borough_counts[sensor.get("borough", "")] += 1
        matched_fids = sorted({fid for fid, d, _ in per_sample_feature if fid and d <= MATCH_RADIUS_M})
        if valid_time and status == "name_geometry_match":
            matched_feature_ids_valid.update(matched_fids)
            for fid in matched_fids:
                trafdir_matched[feature_by_id[fid].get("trafdir", "UNKNOWN")] += 1

        comparable = compatible = incompatible = 0
        one_way_comparable = one_way_compatible = one_way_incompatible = two_way_comparable = 0
        for a, b in zip(samples, samples[1:]):
            midpoint = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            ids = nearby_segments(midpoint, by_cell)
            best_seg, best_dist = None, float("inf")
            for sid in ids:
                seg = segments[sid]
                if not keys.intersection(seg["keys"]):
                    continue
                d = point_segment_distance(midpoint, seg["a"], seg["b"])
                if d < best_dist:
                    best_seg, best_dist = seg, d
            if best_seg is None or best_dist > MATCH_RADIUS_M:
                continue
            trafdir = feature_by_id[best_seg["fid"]].get("trafdir")
            if trafdir == "TW":
                comparable += 1
                compatible += 1
                two_way_comparable += 1
                continue
            if trafdir not in {"FT", "TF"}:
                continue
            sensor_vec = (b[0] - a[0], b[1] - a[1])
            road_vec = (best_seg["b"][0] - best_seg["a"][0], best_seg["b"][1] - best_seg["a"][1])
            denom = math.hypot(*sensor_vec) * math.hypot(*road_vec)
            if denom == 0:
                continue
            cosine = (sensor_vec[0] * road_vec[0] + sensor_vec[1] * road_vec[1]) / denom
            if abs(cosine) < 0.5:
                continue
            comparable += 1
            one_way_comparable += 1
            allowed_same_sign = trafdir == "FT"
            is_compatible = (cosine > 0) == allowed_same_sign
            if is_compatible:
                compatible += 1
                one_way_compatible += 1
            else:
                incompatible += 1
                one_way_incompatible += 1
        results.append({
            "sensor_sid": sensor.get("sid"),
            "sensor_link_name": sensor.get("link_name"),
            "median_calculation_timestamp": sensor.get("median_calculation_timestamp"),
            "n_samples": int(sensor.get("n_samples") or 0),
            "median_tt_sec": float(sensor.get("median_tt_sec") or 0),
            "valid_cost": valid_time,
            "sensor_link_length_ft": float(sensor.get("link_length_ft") or 0),
            "sample_count": len(samples),
            "normalized_sensor_name_keys": sorted(keys),
            "same_name_coverage_within_25m": round(match_coverage, 4),
            "same_name_median_distance_m": round(median_d, 3) if median_d is not None else None,
            "same_name_p90_distance_m": round(p90_d, 3) if p90_d is not None else None,
            "any_name_coverage_within_25m": round(any_coverage, 4),
            "nearest_any_name_median_distance_m": round(percentile(finite_any, 0.5), 3) if finite_any else None,
            "match_status": status,
            "matched_cscl_physicalids": matched_fids,
            "direction_comparable_sample_intervals": comparable,
            "direction_compatible_sample_intervals": compatible,
            "direction_incompatible_sample_intervals": incompatible,
            "direction_compatibility_rate": round(compatible / comparable, 4) if comparable else None,
            "one_way_direction_comparable_intervals": one_way_comparable,
            "one_way_direction_compatible_intervals": one_way_compatible,
            "one_way_direction_incompatible_intervals": one_way_incompatible,
            "two_way_comparable_intervals": two_way_comparable,
        })

    all_vehicle_rows = [r for r in cscl_rows if r.get("trafdir") in {"FT", "TF", "TW"}]
    valid_links = [r for r in results if r["valid_cost"]]
    matched_valid = [r for r in valid_links if r["match_status"] == "name_geometry_match"]
    summary = {
        "classification": "matching preflight only; no route-cost assignment and no dispatch rerun",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs_sha256": {
            "ezpass_snapshot": hashlib.sha256(EZPASS_PATH.read_bytes()).hexdigest(),
            "cscl_manhattan_snapshot": hashlib.sha256(CSCL_PATH.read_bytes()).hexdigest(),
            "matching_script": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "coordinate_method": "local equirectangular projection at 40.76N; adequate for short-distance screening, not survey-grade measurement",
        "sampling_spacing_m": SAMPLE_SPACING_M,
        "match_radius_m": MATCH_RADIUS_M,
        "cscl_manhattan_features": len(cscl_rows),
        "cscl_vehicle_direction_features_ft_tf_tw": len(all_vehicle_rows),
        "cscl_geometry_segments_indexed": len(segments),
        "ezpass_manhattan_links": len(results),
        "ezpass_manhattan_links_with_valid_observed_time": len(valid_links),
        "valid_links_name_and_geometry_matched": len(matched_valid),
        "valid_link_match_rate": round(len(matched_valid) / max(1, len(valid_links)), 4),
        "unique_cscl_physicalids_associated_with_matched_valid_links": len(matched_feature_ids_valid),
        "direction_check_basis": "E-ZPass encoded polyline point order vs nearest same-name CSCL segment geometry; FT permits digitized direction, TF reverse, TW either; excludes |heading cosine| < 0.5. This is a screening check, not a traffic-direction ground-truth audit.",
        "valid_matched_link_direction_interval_counts": {
            "comparable": sum(r["direction_comparable_sample_intervals"] for r in matched_valid),
            "compatible": sum(r["direction_compatible_sample_intervals"] for r in matched_valid),
            "incompatible": sum(r["direction_incompatible_sample_intervals"] for r in matched_valid),
            "one_way_comparable": sum(r["one_way_direction_comparable_intervals"] for r in matched_valid),
            "one_way_compatible": sum(r["one_way_direction_compatible_intervals"] for r in matched_valid),
            "one_way_incompatible": sum(r["one_way_direction_incompatible_intervals"] for r in matched_valid),
            "two_way_comparable": sum(r["two_way_comparable_intervals"] for r in matched_valid),
        },
        "approx_feature_coverage_denominator_note": "This ratio is not reported as a route-cost coverage rate: sensor links can cover only part of a CSCL feature, and one sensor time has not yet been apportioned across features.",
        "matched_feature_trafdir_counts_nonexclusive": dict(trafdir_matched),
        "valid_time_link_match_status_counts": {k[1]: v for k, v in match_status_counts.items() if k[0] == "valid"},
        "all_link_match_status_counts": {
            status: sum(v for (availability, item_status), v in match_status_counts.items() if item_status == status)
            for status in sorted({item_status for _, item_status in match_status_counts})
        },
        "limitations": [
            "CSCL is the current 2026 street centerline while E-ZPass costs are a January 2024 snapshot.",
            "A name-and-geometry match identifies candidate roadway features only. Sampled one-way direction checks are reported separately; this does not prove exact sensor-to-edge overlap or cost allocation.",
            "No median travel time has been assigned to CSCL edges; no unobserved edge has been filled or treated as zero.",
            "No directed shortest paths, zone-to-zone cost matrix, D0/D1 rerun, or route-layer rerun were produced.",
            "The local projection and 15m sampling are screening approximations; finalists require exact projected geometry validation.",
        ],
        "per_sensor_records": len(results),
    }

    json_path = OUT_DIR / "sensor_cscl_match.json"
    summary_path = OUT_DIR / "summary.json"
    csv_path = OUT_DIR / "sensor_cscl_match.csv"
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    flat_fields = [
        "sensor_sid", "sensor_link_name", "median_calculation_timestamp", "n_samples", "median_tt_sec", "valid_cost",
        "sensor_link_length_ft", "sample_count", "same_name_coverage_within_25m", "same_name_median_distance_m",
        "same_name_p90_distance_m", "any_name_coverage_within_25m", "nearest_any_name_median_distance_m",
        "match_status", "matched_cscl_physicalids", "direction_comparable_sample_intervals",
        "direction_compatible_sample_intervals", "direction_incompatible_sample_intervals", "direction_compatibility_rate",
        "one_way_direction_comparable_intervals", "one_way_direction_compatible_intervals",
        "one_way_direction_incompatible_intervals", "two_way_comparable_intervals",
        "normalized_sensor_name_keys",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=flat_fields)
        writer.writeheader()
        for row in results:
            writer.writerow({**row,
                "matched_cscl_physicalids": ";".join(row["matched_cscl_physicalids"]),
                "normalized_sensor_name_keys": ";".join(row["normalized_sensor_name_keys"]),
            })
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
