#!/usr/bin/env python3
"""Map accepted E-ZPass observations to CSCL directed geometry primitives.

This computes sampled, partial-length candidate coverage. It does not produce a
complete route graph, fill missing costs, or create an OD cost matrix.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from audit_ezpass_cscl_match import (  # noqa: E402
    CSCL_PATH,
    EZPASS_PATH,
    MATCH_RADIUS_M,
    OUT_DIR as MATCH_DIR,
    SAMPLE_SPACING_M,
    build_index,
    decode_polyline,
    densify,
    distance,
    name_keys,
    nearby_segments,
    point_segment_distance,
    project,
)

OUT_DIR = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_cost_overlay_preflight_v1"


def projection_parameter(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom = dx * dx + dy * dy
    if denom == 0:
        return 0.0
    return ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / denom


def merge_intervals(intervals):
    if not intervals:
        return []
    ordered = sorted((min(a, b), max(a, b)) for a, b in intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def interval_length(intervals):
    return sum(b - a for a, b in merge_intervals(intervals))


def compatible_direction(trafdir, cosine):
    if abs(cosine) < 0.5:
        return None
    if trafdir == "TW":
        return "FT" if cosine > 0 else "TF"
    if trafdir == "FT" and cosine > 0:
        return "FT"
    if trafdir == "TF" and cosine < 0:
        return "TF"
    return None


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ezpass = json.loads(EZPASS_PATH.read_text())
    cscl = json.loads(CSCL_PATH.read_text())
    match_rows = json.loads((MATCH_DIR / "sensor_cscl_match.json").read_text())
    accepted_ids = {
        str(r["sensor_sid"])
        for r in match_rows
        if r["valid_cost"] and r["match_status"] == "name_geometry_match"
    }
    segments, index, features = build_index(cscl)
    segment_by_key = {(s["fid"], s["part"], s["index"]): s for s in segments}
    feature_lines = {}
    for row in cscl:
        if row.get("trafdir") not in {"FT", "TF", "TW"}:
            continue
        fid = str(row.get("physicalid", ""))
        parts = []
        for part in row.get("the_geom", {}).get("coordinates", []):
            pts = [project((pt[0], pt[1])) for pt in part]
            if len(pts) >= 2:
                parts.append(pts)
        feature_lines[fid] = parts

    # Geometry coverage is tracked per primitive and per travel direction.
    # Cost candidates remain per sensor observation; overlapping sensors are
    # deliberately not averaged together.
    observed_intervals_by_arc = defaultdict(list)
    observation_by_sensor_arc = defaultdict(dict)
    link_summaries = []
    direction_rejected_intervals = 0
    unmapped_sensor_intervals = 0
    zero_projected_length_intervals = 0
    mapped_sample_intervals = 0
    sensor_sample_intervals = 0
    sensor_polyline_length_m = 0.0
    sensor_mapped_length_m = 0.0
    speed_rows = []

    for sensor in ezpass:
        if sensor.get("borough") != "Manhattan" or str(sensor.get("sid")) not in accepted_ids:
            continue
        sid = str(sensor["sid"])
        raw_points = [project(p) for p in decode_polyline(sensor.get("polyline", ""))]
        samples = densify(raw_points, 10.0)
        keys = name_keys(sensor.get("link_name", ""), sensor=True)
        polyline_length = sum(distance(a, b) for a, b in zip(samples, samples[1:]))
        sensor_polyline_length_m += polyline_length
        tt = float(sensor.get("median_tt_sec") or 0)
        link_length_ft = float(sensor.get("link_length_ft") or 0)
        reported_speed = float(sensor.get("median_speed_fps") or 0)
        derived_speed = link_length_ft / tt if link_length_ft > 0 and tt > 0 else 0.0
        speed = reported_speed if reported_speed > 0 else derived_speed
        if speed > 0 and derived_speed > 0:
            speed_rows.append({
                "sensor_sid": sid,
                "reported_speed_fps": reported_speed,
                "derived_speed_fps": derived_speed,
                "relative_difference": abs(reported_speed - derived_speed) / derived_speed if reported_speed > 0 else None,
            })

        mapped_sensor_length = 0.0
        per_arc_intervals = defaultdict(list)
        per_arc_nearest_m = defaultdict(list)
        for a, b in zip(samples, samples[1:]):
            sensor_sample_intervals += 1
            seg_len = distance(a, b)
            if seg_len <= 1e-6:
                continue
            midpoint = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            unit = ((b[0] - a[0]) / seg_len, (b[1] - a[1]) / seg_len)
            best = None
            best_dist = float("inf")
            ids = nearby_segments(midpoint, index)
            for idx in ids:
                candidate = segments[idx]
                if not keys.intersection(candidate["keys"]):
                    continue
                d = point_segment_distance(midpoint, candidate["a"], candidate["b"])
                if d > MATCH_RADIUS_M or d >= best_dist:
                    continue
                cx = candidate["b"][0] - candidate["a"][0]
                cy = candidate["b"][1] - candidate["a"][1]
                clen = math.hypot(cx, cy)
                if clen <= 1e-8:
                    continue
                cosine = (unit[0] * cx + unit[1] * cy) / clen
                trafdir = features[candidate["fid"]].get("trafdir")
                travel_dir = compatible_direction(trafdir, cosine)
                if travel_dir is None:
                    direction_rejected_intervals += 1
                    continue
                best, best_dist = (candidate, travel_dir), d
            if best is None:
                unmapped_sensor_intervals += 1
                continue

            candidate, travel_dir = best
            target_length = distance(candidate["a"], candidate["b"])
            t0 = max(0.0, min(1.0, projection_parameter(a, candidate["a"], candidate["b"])))
            t1 = max(0.0, min(1.0, projection_parameter(b, candidate["a"], candidate["b"])))
            lo, hi = sorted((t0 * target_length, t1 * target_length))
            if hi - lo <= 0.001:
                zero_projected_length_intervals += 1
                continue
            arc_key = (candidate["fid"], candidate["part"], candidate["index"], travel_dir)
            per_arc_intervals[arc_key].append((lo, hi))
            per_arc_nearest_m[arc_key].append(best_dist)
            mapped_sample_intervals += 1
            mapped_sensor_length += seg_len

        for arc_key, intervals in per_arc_intervals.items():
            covered_m = interval_length(intervals)
            fid, part_id, seg_id, travel_dir = arc_key
            row = features[fid]
            primitive = segment_by_key[(fid, part_id, seg_id)]
            primitive_length_m = distance(primitive["a"], primitive["b"])
            observed_intervals_by_arc[arc_key].extend(intervals)
            observation_by_sensor_arc[(sid, *arc_key)] = {
                "sensor_sid": sid,
                "sensor_link_name": sensor.get("link_name"),
                "median_calculation_timestamp": sensor.get("median_calculation_timestamp"),
                "n_samples": int(sensor.get("n_samples") or 0),
                "aggregation_period_sec": int(sensor.get("aggregation_period_sec") or 0),
                "median_tt_sec": tt,
                "sensor_link_length_ft": link_length_ft,
                "median_speed_fps": speed,
                "speed_basis": "reported_median_speed_fps" if reported_speed > 0 else "link_length_ft_div_median_tt_sec",
                "physicalid": fid,
                "full_street_name": row.get("full_street_name"),
                "trafdir": row.get("trafdir"),
                "part_index": part_id,
                "primitive_index": seg_id,
                "travel_direction_on_cscl": travel_dir,
                "primitive_length_m": primitive_length_m,
                "observed_covered_length_m": covered_m,
                "observed_covered_length_ft": covered_m * 3.280839895,
                "primitive_coverage_fraction_for_this_sensor": min(1.0, covered_m / primitive_length_m) if primitive_length_m else 0,
                "observed_time_estimate_for_covered_length_sec": covered_m * 3.280839895 / speed if speed > 0 else None,
                "median_nearest_distance_m": sum(per_arc_nearest_m[arc_key]) / len(per_arc_nearest_m[arc_key]),
            }
        sensor_mapped_length_m += mapped_sensor_length
        link_summaries.append({
            "sensor_sid": sid,
            "sensor_link_name": sensor.get("link_name"),
            "valid_cost": True,
            "sampled_polyline_length_m": round(polyline_length, 3),
            "matched_sensor_length_m": round(mapped_sensor_length, 3),
            "sampled_geometry_match_fraction": round(mapped_sensor_length / polyline_length, 4) if polyline_length else 0,
            "median_tt_sec": tt,
            "reported_median_speed_fps": reported_speed,
            "derived_link_speed_fps": round(derived_speed, 5),
            "cscl_directed_primitives_touched": len(per_arc_intervals),
        })

    # Total routeable directed geometry length in the Manhattan slice.
    directed_geometry_total_m = 0.0
    directed_geometry_observed_m = 0.0
    directed_primitives = 0
    observed_directed_primitives = set()
    for fid, parts in feature_lines.items():
        row = features[fid]
        dirs = ["FT", "TF"] if row.get("trafdir") == "TW" else [row.get("trafdir")]
        for part_id, part in enumerate(parts):
            for seg_id, (a, b) in enumerate(zip(part, part[1:])):
                length = distance(a, b)
                if length <= 0:
                    continue
                for direction in dirs:
                    directed_primitives += 1
                    directed_geometry_total_m += length
                    arc_key = (fid, part_id, seg_id, direction)
                    intervals = observed_intervals_by_arc.get(arc_key, [])
                    covered = min(length, interval_length(intervals))
                    directed_geometry_observed_m += covered
                    if covered > 0:
                        observed_directed_primitives.add(arc_key)

    cost_rows = []
    for key, row in observation_by_sensor_arc.items():
        sid, fid, part_id, seg_id, travel_dir = key
        row = dict(row)
        row["sensor_observation_key"] = f"{sid}:{fid}:{part_id}:{seg_id}:{travel_dir}"
        cost_rows.append(row)
    cost_rows.sort(key=lambda x: (int(x["sensor_sid"]), int(x["physicalid"]), x["part_index"], x["primitive_index"], x["travel_direction_on_cscl"]))

    rel_speed_diffs = [r["relative_difference"] for r in speed_rows if r["relative_difference"] is not None]
    summary = {
        "classification": "partial geometry-to-cost overlay preflight; not a complete route-cost graph",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs_sha256": {
            "ezpass_snapshot": hashlib.sha256(EZPASS_PATH.read_bytes()).hexdigest(),
            "cscl_manhattan_snapshot": hashlib.sha256(CSCL_PATH.read_bytes()).hexdigest(),
            "matching_audit": hashlib.sha256((MATCH_DIR / "sensor_cscl_match.json").read_bytes()).hexdigest(),
            "overlay_script": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "accepted_valid_sensor_links": len(accepted_ids),
        "accepted_sensor_observation_to_directed_primitive_rows": len(cost_rows),
        "sensor_polyline_sampled_length_km": round(sensor_polyline_length_m / 1000, 3),
        "sensor_polyline_sampled_length_mapped_km": round(sensor_mapped_length_m / 1000, 3),
        "sensor_polyline_mapped_fraction": round(sensor_mapped_length_m / sensor_polyline_length_m, 4) if sensor_polyline_length_m else 0,
        "sensor_geometry_sample_intervals": sensor_sample_intervals,
        "mapped_sensor_geometry_sample_intervals": mapped_sample_intervals,
        "unmapped_sensor_sample_intervals": unmapped_sensor_intervals,
        "zero_projected_length_sensor_intervals": zero_projected_length_intervals,
        "nearby_candidate_road_segments_rejected_by_direction": direction_rejected_intervals,
        "manhattan_cscl_routeable_directed_primitive_count": directed_primitives,
        "manhattan_cscl_routeable_directed_geometry_length_km": round(directed_geometry_total_m / 1000, 3),
        "manhattan_cscl_directed_geometry_length_with_observed_cost_candidate_km": round(directed_geometry_observed_m / 1000, 3),
        "manhattan_cscl_directed_geometry_length_coverage_fraction": round(directed_geometry_observed_m / directed_geometry_total_m, 5) if directed_geometry_total_m else 0,
        "directed_primitives_with_any_observed_cost_candidate": len(observed_directed_primitives),
        "directed_primitives_fully_covered_by_any_single_sensor": len({
            (row["physicalid"], row["part_index"], row["primitive_index"], row["travel_direction_on_cscl"])
            for row in cost_rows if row["primitive_coverage_fraction_for_this_sensor"] >= 0.95
        }),
        "source_speed_consistency": {
            "links_with_reported_and_derived_speed": len(rel_speed_diffs),
            "median_relative_difference": round(sorted(rel_speed_diffs)[len(rel_speed_diffs)//2], 5) if rel_speed_diffs else None,
            "max_relative_difference": round(max(rel_speed_diffs), 5) if rel_speed_diffs else None,
            "interpretation": "Reported median_speed_fps is used as the observed corridor speed. Derived link_length_ft / median_tt_sec is retained as a consistency check, not substituted when the reported speed is present.",
        },
        "missing_cost_policy": "No cost is assigned outside covered subsegment intervals; no zero fill or guessed speed is applied.",
        "limitations": [
            "The overlay uses local equirectangular coordinates and 10m sampled sensor intervals; it estimates subsegment coverage and is not survey-grade exact line intersection.",
            "A sensor speed over the sensor corridor is used to estimate time only on the mapped subsegment length; observations are not averaged across overlapping sensors.",
            "A partial interval does not make a whole CSCL primitive fully observed; route graph splitting at observation boundaries is still needed.",
            "No endpoint snapping, turn restrictions, grade separation, connected component, shortest path, C4 pickup ETA coverage, or OD cost matrix has been validated.",
            "CSCL topology is current 2026 while DOT speeds are a January 2024 snapshot.",
        ],
    }

    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (OUT_DIR / "sensor_link_mapping.json").write_text(json.dumps(link_summaries, indent=2, ensure_ascii=False) + "\n")
    with (OUT_DIR / "directed_cost_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(cost_rows[0].keys()) if cost_rows else [])
        if cost_rows:
            writer.writeheader()
            writer.writerows(cost_rows)
    (OUT_DIR / "speed_consistency.json").write_text(json.dumps(speed_rows, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
