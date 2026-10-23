#!/usr/bin/env python3
"""Generate a reproducible draft of fixed per-vehicle Porto route tasks.

The output fixes spatial endpoints and vehicle order sequences only. It is not
an executable SUMO scenario until endpoint matching and all 200 route legs have
passed the final directed-network validator.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
BBOX = {"west": -8.635, "south": 41.138, "east": -8.595, "north": 41.162}
ALLOWED_HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "road", "motorway_link", "trunk_link",
    "primary_link", "secondary_link", "tertiary_link",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inside(lon: float, lat: float) -> bool:
    return BBOX["west"] <= lon <= BBOX["east"] and BBOX["south"] <= lat <= BBOX["north"]


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    lat_mid = math.radians((lat1 + lat2) / 2)
    dx = (lon2 - lon1) * 111_320 * math.cos(lat_mid)
    dy = (lat2 - lat1) * 111_320
    return math.hypot(dx, dy)


def interpolate_m(a: tuple[float, float], b: tuple[float, float], distance: float) -> tuple[float, float]:
    length = distance_m(a, b)
    ratio = 0.0 if length <= 0 else min(1.0, max(0.0, distance / length))
    return (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio)


def weighted_choice(rng: random.Random, rows: list[dict[str, Any]], weight_key: str) -> dict[str, Any]:
    weights = [max(0, int(row[weight_key])) for row in rows]
    total = sum(weights)
    if total == 0:
        return rows[rng.randrange(len(rows))]
    pick = rng.randrange(total)
    for row, weight in zip(rows, weights):
        pick -= weight
        if pick < 0:
            return row
    return rows[-1]


def hotspot_point(row: dict[str, Any]) -> dict[str, Any]:
    point_id = f"hotspot_{row['cell_x']}_{row['cell_y']}"
    return {
        "point_id": point_id,
        "point_kind": "heat_cell_proxy",
        "lon": float(row["center_lon"]),
        "lat": float(row["center_lat"]),
        "uncertainty_radius_m": 0,
        "heat_proxy": {
            "pickup_count": int(row["pickup_count"]),
            "dropoff_count": int(row["dropoff_count"]),
            "source_semantics": "historical taxi endpoint cell; not an official ride-hailing station",
        },
        "map_match_status": "pending",
    }


def load_hotspots(path: Path, top_k: int) -> list[dict[str, Any]]:
    by_cell: dict[tuple[int, int], dict[str, Any]] = defaultdict(dict)
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            lon, lat = float(row["center_lon"]), float(row["center_lat"])
            if not inside(lon, lat):
                continue
            cell = (int(row["cell_x"]), int(row["cell_y"]))
            by_cell[cell][row["endpoint_role"]] = int(row["trip_count"])
            by_cell[cell]["center_lon"] = lon
            by_cell[cell]["center_lat"] = lat
    rows = []
    for (cell_x, cell_y), values in by_cell.items():
        pickup = values.get("pickup_proxy", 0)
        dropoff = values.get("dropoff_proxy", 0)
        if pickup <= 0 or dropoff <= 0:
            continue
        rows.append({
            "cell_x": cell_x,
            "cell_y": cell_y,
            "center_lon": values["center_lon"],
            "center_lat": values["center_lat"],
            "pickup_count": pickup,
            "dropoff_count": dropoff,
        })
    rows.sort(key=lambda row: (-row["pickup_count"], row["cell_y"], row["cell_x"]))
    if len(rows) < top_k:
        raise ValueError(f"need {top_k} in-fence cells with both endpoint roles; found {len(rows)}")
    return rows[:top_k]


def parse_osm_features(osm_path: Path) -> list[dict[str, Any]]:
    root = ET.parse(osm_path).getroot()
    nodes = {
        element.attrib["id"]: (float(element.attrib["lon"]), float(element.attrib["lat"]))
        for element in root.findall("node")
    }
    tags_by_node = {
        element.attrib["id"]: {t.attrib.get("k"): t.attrib.get("v") for t in element.findall("tag")}
        for element in root.findall("node")
    }
    ways_by_node: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    eligible_segments: list[dict[str, Any]] = []
    for way in root.findall("way"):
        tags = {t.attrib.get("k"): t.attrib.get("v") for t in way.findall("tag")}
        if tags.get("highway") not in ALLOWED_HIGHWAYS:
            continue
        if any(tags.get(key) in {"no", "private"} for key in ("access", "motor_vehicle", "motorcar")):
            continue
        refs = [nd.attrib["ref"] for nd in way.findall("nd")]
        ways_by_node_for_way = (way.attrib["id"], refs)
        for ref in refs:
            ways_by_node[ref].append(ways_by_node_for_way)
        for index, (first, second) in enumerate(zip(refs, refs[1:])):
            if first not in nodes or second not in nodes:
                continue
            a, b = nodes[first], nodes[second]
            if not inside(*a) or not inside(*b):
                continue
            if tags.get("highway") == "service":
                continue
            eligible_segments.append({
                "way_id": way.attrib["id"],
                "segment_index": index,
                "from_node_id": first,
                "to_node_id": second,
                "a": a,
                "b": b,
            })

    features: list[dict[str, Any]] = []
    for node_id, node_tags in tags_by_node.items():
        coord = nodes.get(node_id)
        if coord is None or not inside(*coord):
            continue
        highway_ways = ways_by_node.get(node_id, [])
        distinct_way_ids = sorted({way_id for way_id, _ in highway_ways})
        feature_type = None
        if node_tags.get("highway") == "traffic_signals":
            feature_type = "mapped_traffic_signal"
        elif len(distinct_way_ids) >= 2:
            feature_type = "shared_osm_road_vertex"
        if feature_type is None:
            continue

        adjacent = [segment for segment in eligible_segments if node_id in {segment["from_node_id"], segment["to_node_id"]}]
        if not adjacent:
            continue
        features.append({
            "node_id": node_id,
            "feature_type": feature_type,
            "anchor": coord,
            "adjacent_segments": adjacent,
        })
    features.sort(key=lambda feature: (feature["feature_type"], int(feature["node_id"])))
    if not features:
        raise ValueError("no in-fence road-feature points with adjacent routable OSM segments")
    return features


def sample_fuzzy_point(
    rng: random.Random,
    feature: dict[str, Any],
    point_id: str,
) -> dict[str, Any]:
    segment = feature["adjacent_segments"][rng.randrange(len(feature["adjacent_segments"]))]
    anchor = feature["anchor"]
    if segment["from_node_id"] == feature["node_id"]:
        other = segment["b"]
    elif segment["to_node_id"] == feature["node_id"]:
        other = segment["a"]
    else:
        raise ValueError("selected segment is not incident to its feature anchor")
    segment_length = distance_m(anchor, other)
    if segment_length < 1:
        lon, lat = anchor
    else:
        max_offset = min(35.0, segment_length)
        min_offset = min(3.0, max_offset)
        offset = rng.uniform(min_offset, max_offset)
        lon, lat = interpolate_m(anchor, other, offset)
    return {
        "point_id": point_id,
        "point_kind": "fuzzy_road_feature_sample",
        "lon": lon,
        "lat": lat,
        "uncertainty_radius_m": 35,
        "feature_hint": feature["feature_type"],
        "llm_visible": {
            "lon": round(lon, 7),
            "lat": round(lat, 7),
            "uncertainty_radius_m": 35,
            "feature_hint": feature["feature_type"],
        },
        "validation_truth": {
            "feature_osm_node_id": feature["node_id"],
            "source_way_id": segment["way_id"],
            "source_segment_index": segment["segment_index"],
            "source_segment_node_ids": [segment["from_node_id"], segment["to_node_id"]],
            "map_match_status": "pending",
            "visibility": "validator_only; hide source ids from all route planners",
        },
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    package = Path(__file__).parent
    heat_path = args.heat or package / "derived/heat/porto_hotspot_cells_200m.csv"
    osm_path = args.osm or package / "raw/osm/porto_candidate.osm"
    output_path = args.output or package / f"derived/scenarios/orders_seed_{args.seed}_draft.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    hotspots = load_hotspots(heat_path, args.candidate_count)
    features = parse_osm_features(osm_path)
    start_points: dict[str, dict[str, Any]] = {}
    for row in hotspots:
        point = hotspot_point(row)
        start_points[point["point_id"]] = point

    feature_records: list[dict[str, Any]] = []
    # Keep each car's task mix at exactly seven candidate-to-candidate and three fuzzy orders.
    role_by_sequence: dict[tuple[int, int], str] = {}
    global_fuzzy_roles = ["pickup"] * 15 + ["dropoff"] * 15
    rng.shuffle(global_fuzzy_roles)
    fuzzy_cursor = 0
    for vehicle_index in range(args.fleet_size):
        roles = ["exact"] * (args.orders_per_vehicle - 3)
        roles += ["fuzzy"] * 3
        rng.shuffle(roles)
        for sequence_index, order_kind in enumerate(roles):
            if order_kind == "exact":
                endpoint_role = "none"
            else:
                endpoint_role = global_fuzzy_roles[fuzzy_cursor]
                fuzzy_cursor += 1
            role_by_sequence[(vehicle_index, sequence_index)] = endpoint_role

    vehicles = []
    orders = []
    route_legs = []
    order_counter = 0
    for vehicle_index in range(args.fleet_size):
        vehicle_id = f"veh_{vehicle_index + 1:02d}"
        initial_hotspot = weighted_choice(rng, hotspots, "pickup_count")
        initial = hotspot_point(initial_hotspot)
        vehicles.append({
            "vehicle_id": vehicle_id,
            "initial_position": initial,
            "initial_map_match_status": "pending",
            "fixed_order_sequence": [f"ord_{vehicle_index * args.orders_per_vehicle + i + 1:04d}" for i in range(args.orders_per_vehicle)],
        })
        current_point_id = initial["point_id"]
        for sequence_index in range(args.orders_per_vehicle):
            order_counter += 1
            order_id = f"ord_{order_counter:04d}"
            endpoint_role = role_by_sequence[(vehicle_index, sequence_index)]
            pickup_base = weighted_choice(rng, hotspots, "pickup_count")
            dropoff_base = weighted_choice(rng, hotspots, "dropoff_count")
            for _ in range(25):
                if (pickup_base["cell_x"], pickup_base["cell_y"]) != (dropoff_base["cell_x"], dropoff_base["cell_y"]):
                    break
                dropoff_base = weighted_choice(rng, hotspots, "dropoff_count")
            pickup = hotspot_point(pickup_base)
            dropoff = hotspot_point(dropoff_base)
            if endpoint_role == "pickup":
                feature = features[rng.randrange(len(features))]
                pickup = sample_fuzzy_point(rng, feature, f"fuzzy_{order_id}_pickup")
                feature_records.append({"order_id": order_id, "endpoint_role": "pickup", "feature_osm_node_id": feature["node_id"], "feature_type": feature["feature_type"]})
            elif endpoint_role == "dropoff":
                feature = features[rng.randrange(len(features))]
                dropoff = sample_fuzzy_point(rng, feature, f"fuzzy_{order_id}_dropoff")
                feature_records.append({"order_id": order_id, "endpoint_role": "dropoff", "feature_osm_node_id": feature["node_id"], "feature_type": feature["feature_type"]})
            kind = "fuzzy_single_endpoint" if endpoint_role != "none" else "candidate_point_to_candidate_point"
            orders.append({
                "order_id": order_id,
                "vehicle_id": vehicle_id,
                "sequence_index": sequence_index,
                "order_kind": kind,
                "pickup": pickup,
                "dropoff": dropoff,
                "release_rule": "begin route planning immediately after this vehicle completes the previous order",
                "route_validation_status": "pending",
            })
            route_legs.append({
                "route_task_id": f"task_{order_id}_deadhead",
                "vehicle_id": vehicle_id,
                "order_id": order_id,
                "leg_role": "vehicle_to_pickup",
                "from_point_id": current_point_id,
                "to_point_id": pickup["point_id"],
                "traditional_reachability_status": "pending",
            })
            route_legs.append({
                "route_task_id": f"task_{order_id}_passenger",
                "vehicle_id": vehicle_id,
                "order_id": order_id,
                "leg_role": "pickup_to_dropoff",
                "from_point_id": pickup["point_id"],
                "to_point_id": dropoff["point_id"],
                "traditional_reachability_status": "pending",
            })
            current_point_id = dropoff["point_id"]

    exact_count = sum(order["order_kind"] == "candidate_point_to_candidate_point" for order in orders)
    fuzzy_count = len(orders) - exact_count
    if exact_count != args.orders_per_vehicle * args.fleet_size * 0.7 or fuzzy_count != args.orders_per_vehicle * args.fleet_size * 0.3:
        raise ValueError("the configured counts must yield exact 70/30 integer totals")

    payload = {
        "scenario_id": f"porto_fixed_fleet_route_v1_seed_{args.seed}",
        "status": "draft_preflight_pending",
        "generator_version": VERSION,
        "seed": args.seed,
        "source_hashes": {"osm_sha256": sha256(osm_path), "hotspot_csv_sha256": sha256(heat_path)},
        "candidate_fence_wgs84": BBOX,
        "dataset_semantics": {
            "historical_hotspots": "UCI/figshare Porto taxi endpoint-cell proxies; not active ride-hailing stations",
            "fuzzy_endpoint": "random point on a non-service OSM road segment near a mapped signal/shared road vertex; the sampled coordinate is fixed for this seed, not a fixed reusable station",
            "traffic": "not present in this order file; generated by the separate SUMO scenario",
        },
        "counts": {
            "fleet_size": args.fleet_size,
            "orders_per_vehicle": args.orders_per_vehicle,
            "total_orders": len(orders),
            "candidate_pool_size": len(hotspots),
            "exact_candidate_to_candidate_orders": exact_count,
            "fuzzy_single_endpoint_orders": fuzzy_count,
            "fuzzy_pickup_orders": sum(row["endpoint_role"] == "pickup" for row in feature_records),
            "fuzzy_dropoff_orders": sum(row["endpoint_role"] == "dropoff" for row in feature_records),
            "route_legs_for_preflight": len(route_legs),
        },
        "candidate_hotspots": [hotspot_point(row) for row in hotspots],
        "vehicles": vehicles,
        "orders": orders,
        "route_legs": route_legs,
        "route_preflight": {
            "status": "not_run",
            "required_before_experiment": True,
            "validator": "traditional time-dependent route search on the final directed SUMO network, including one-way, access, and turn restrictions",
            "routes_are_not_frozen_or_shared_with_other_methods": True,
            "unreachable_task_policy": "apply the predeclared deterministic regeneration rule, record replaced task IDs, then freeze a new scenario file and hash before comparing methods",
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output_path),
        "status": payload["status"],
        "counts": payload["counts"],
        "route_preflight": payload["route_preflight"]["status"],
        "sha256": sha256(output_path),
    }, ensure_ascii=False, indent=2))
    return payload


def main() -> int:
    package = Path(__file__).parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fleet-size", type=int, default=10)
    parser.add_argument("--orders-per-vehicle", type=int, default=10)
    parser.add_argument("--candidate-count", type=int, default=30)
    parser.add_argument("--osm", type=Path, default=package / "raw/osm/porto_candidate.osm")
    parser.add_argument("--heat", type=Path, default=package / "derived/heat/porto_hotspot_cells_200m.csv")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    build(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
