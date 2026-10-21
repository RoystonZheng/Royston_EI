#!/usr/bin/env python3
"""Package immutable NYC TLC scenario inputs for shared dispatch comparisons."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NYC = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024"
SCENARIOS = NYC / "multi_vehicle_competition_scenarios_v1"
TARGET = NYC / "three_method_benchmark_v1"
ZONE_GEOJSON = ROOT / "data/dispatch/nyc_dot_ezpass_2021_2024/c4_zone_subset_2026-10-20/taxi_zones.geojson"
PICKUP_POLICY = {
    "policy_id": "synthetic_zone_centroid_pickup_proxy_v1",
    "distance_method": "haversine between official taxi-zone polygon area centroids, multiplied by detour_factor",
    "speed_mps": 5.0,
    "detour_factor": 1.3,
    "minimum_intrazonal_distance_m": 300.0,
    "interpretation": "synthetic zone-level pickup cost for controlled method comparison; not measured street distance or ETA",
}

CASES = {
    "C2_four_same_second_orders_three_vehicles": {
        "source_scenario": SCENARIOS / "C2_four_same_second_orders_three_vehicles",
        "source_cost_matrix": NYC / "c3_mechanism_preflight_v1/zone_cost_matrix_train_2024-01-01_15_full_observed_od.csv.gz",
        "training_manifest": NYC / "c3_mechanism_preflight_v1/preflight_manifest.json",
    },
    "C3_manhattan_multizone_44_order_burst": {
        "source_scenario": SCENARIOS / "C3_manhattan_multizone_44_order_burst",
        "source_cost_matrix": NYC / "c3_mechanism_preflight_v1/zone_cost_matrix_train_2024-01-01_15_full_observed_od.csv.gz",
        "training_manifest": NYC / "c3_mechanism_preflight_v1/preflight_manifest.json",
    },
    "C4_manhattan_790_order_five_minute_load_sweep": {
        "source_scenario": SCENARIOS / "C4_manhattan_790_order_five_minute_load_sweep",
        "source_cost_matrix": NYC / "c4_load_sweep_mechanism_v1/zone_cost_matrix_train_2024-01-01_15_full_observed_od_including_same_zone.csv.gz",
        "training_manifest": NYC / "c4_load_sweep_mechanism_v1/preflight_manifest.json",
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_file(source: Path, target: Path) -> dict:
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return {"path": target.name, "bytes": target.stat().st_size, "sha256": sha256(target)}


def ring_area_centroid(ring):
    cross_sum = 0.0
    x_sum = 0.0
    y_sum = 0.0
    for index in range(len(ring) - 1):
        x1, y1 = ring[index][:2]
        x2, y2 = ring[index + 1][:2]
        cross = x1 * y2 - x2 * y1
        cross_sum += cross
        x_sum += (x1 + x2) * cross
        y_sum += (y1 + y2) * cross
    if abs(cross_sum) < 1e-15:
        points = ring[:-1] or ring
        return 0.0, (sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points))
    area = abs(cross_sum / 2.0)
    centroid = (x_sum / (3.0 * cross_sum), y_sum / (3.0 * cross_sum))
    return area, centroid


def polygon_centroid(polygon):
    net_area = 0.0
    x_weight = 0.0
    y_weight = 0.0
    for ring_index, ring in enumerate(polygon):
        area, center = ring_area_centroid(ring)
        signed_area = area if ring_index == 0 else -area
        net_area += signed_area
        x_weight += center[0] * signed_area
        y_weight += center[1] * signed_area
    if net_area <= 0:
        raise ValueError("invalid taxi-zone polygon area")
    return net_area, (x_weight / net_area, y_weight / net_area)


def geojson_zone_centroids(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    centroids = {}
    for feature in document.get("features", []):
        zone_id = int(feature["properties"]["locationid"])
        geometry = feature["geometry"]
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        area_sum = 0.0
        lon_sum = 0.0
        lat_sum = 0.0
        for polygon in polygons:
            area, (lon, lat) = polygon_centroid(polygon)
            area_sum += area
            lon_sum += lon * area
            lat_sum += lat * area
        centroids[str(zone_id)] = {"lon": lon_sum / area_sum, "lat": lat_sum / area_sum}
    return centroids


def main() -> None:
    overwrite = "--overwrite" in sys.argv[1:]
    if TARGET.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing benchmark package: {TARGET}; pass --overwrite to rebuild its input files")
    if TARGET.exists() and overwrite and (TARGET / "results").exists():
        raise FileExistsError(
            f"refusing to rebuild inputs while results exist: {TARGET / 'results'}; "
            "archive results and choose a new versioned package path first"
        )
    TARGET.mkdir(parents=True, exist_ok=True)
    cases_root = TARGET / "cases"
    if cases_root.exists():
        shutil.rmtree(cases_root)
    cases_root.mkdir(parents=True)

    zone_centroids = geojson_zone_centroids(ZONE_GEOJSON)
    (TARGET / "zone_centroids.json").write_text(
        json.dumps(zone_centroids, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    copy_file(ZONE_GEOJSON, TARGET / "taxi_zones.geojson")
    (TARGET / "pickup_cost_policy.json").write_text(
        json.dumps(PICKUP_POLICY, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    case_records = {}
    for case_id, sources in CASES.items():
        case_source = sources["source_scenario"]
        case_target = TARGET / "cases" / case_id
        case_target.mkdir(parents=True, exist_ok=False)
        files = {}
        for name in ("scenario.json", "orders.json", "orders.parquet"):
            src = case_source / name
            if src.exists():
                files[name] = copy_file(src, case_target / name)
        cost_target = case_target / "zone_od_cost_matrix.csv.gz"
        files["zone_od_cost_matrix.csv.gz"] = copy_file(sources["source_cost_matrix"], cost_target)
        training_manifest = sources["training_manifest"]
        metadata = json.loads((case_target / "scenario.json").read_text(encoding="utf-8"))
        order_payload = json.loads((case_target / "orders.json").read_text(encoding="utf-8"))
        if len(order_payload.get("orders", [])) != metadata.get("request_trace_row_count"):
            raise ValueError(f"order count mismatch: {case_id}")
        required_pickup_zones = {
            int(row["pickup_node_id"].removeprefix("zone_"))
            for row in order_payload["orders"]
        }
        required_pickup_zones.update(
            int(row["position_node_id"].removeprefix("zone_"))
            for fleet in metadata.get("fleet_variants", [])
            for row in fleet["vehicles"]
        )
        missing_centroids = sorted(zone_id for zone_id in required_pickup_zones if str(zone_id) not in zone_centroids)
        if missing_centroids:
            raise ValueError(f"missing pickup-zone centroid(s) for {case_id}: {missing_centroids}")
        case_records[case_id] = {
            "path": f"cases/{case_id}",
            "scenario_id": metadata["scenario_id"],
            "request_count": len(order_payload["orders"]),
            "fleet_ids": [item["fleet_id"] for item in metadata.get("fleet_variants", [])],
            "source_scenario_path": str(case_source.relative_to(ROOT)),
            "source_cost_matrix_path": str(sources["source_cost_matrix"].relative_to(ROOT)),
            "training_preflight_manifest_sha256": sha256(training_manifest),
            "files": files,
            "limitations": metadata.get("limitations", []),
        }

    manifest = {
        "schema_version": "nyc-tlc-shared-dispatch-input-v1",
        "package_kind": "common-input-package_for_dispatch_method_comparison",
        "created_date": "2026-10-20",
        "performance_claim": False,
        "source_dataset": "NYC TLC 2024 High Volume FHV trip data, dataset bqxa-629f",
        "raw_source_manifest": {
            "path": "data/dispatch/nyc_tlc_hvfhv_2024/manifest.json",
            "sha256": sha256(ROOT / "data/dispatch/nyc_tlc_hvfhv_2024/manifest.json"),
        },
        "method_groups": {
            "traditional": ["D0_greedy", "D1_optimizer"],
            "pure_llm": ["D5_pure_llm_direct"],
            "research_agent_optimizer": [
                "D2_agent_optimizer",
                "D3_agent_llm_context_optimizer",
                "D4_agent_llm_feedback_optimizer",
            ],
        },
        "common_runtime_contract": "dispatch-agent-v1 DispatchInput/Candidate set; all methods use the same order trace, fleet variant, zone OD cost matrix, hard constraints, and independent output validator.",
        "pickup_cost_proxy": PICKUP_POLICY,
        "zone_centroids": {
            "path": "zone_centroids.json",
            "source_path": str(ZONE_GEOJSON.relative_to(ROOT)),
            "source_sha256": sha256(ZONE_GEOJSON),
            "zone_count": len(zone_centroids),
            "sha256": sha256(TARGET / "zone_centroids.json"),
        },
        "cases": case_records,
    }
    (TARGET / "benchmark_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(TARGET)
    print(json.dumps({key: value["request_count"] for key, value in case_records.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
