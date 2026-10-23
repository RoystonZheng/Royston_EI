#!/usr/bin/env python3
"""Inspect a candidate OSM extract and preflight top heat-cell reachability."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import xml.etree.ElementTree as ET
from collections import Counter, deque
from pathlib import Path


ALLOWED_HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "road", "motorway_link",
    "trunk_link", "primary_link", "secondary_link", "tertiary_link",
}
BBOX = {"west": -8.635, "south": 41.138, "east": -8.595, "north": 41.162}


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def distance_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    lat_mid = math.radians((lat_a + lat_b) / 2.0)
    dx = (lon_a - lon_b) * 111_320.0 * math.cos(lat_mid)
    dy = (lat_a - lat_b) * 111_320.0
    return math.hypot(dx, dy)


def reachable(start: str, target: str, adjacency: dict[str, set[str]]) -> bool:
    if start == target:
        return True
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, ()):
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    package = Path(__file__).parent
    parser.add_argument("--osm", type=Path, default=package / "raw/osm/porto_candidate.osm")
    parser.add_argument(
        "--hotspots", type=Path,
        default=package / "derived/heat/porto_hotspot_cells_200m.csv",
    )
    parser.add_argument(
        "--output", type=Path,
        default=package / "derived/region/osm_preflight.json",
    )
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()
    if not args.osm.is_file() or not args.hotspots.is_file():
        parser.error("OSM extract and hotspot aggregation must exist first")

    root = ET.parse(args.osm).getroot()
    if root.tag != "osm":
        parser.error(f"expected OSM XML root; found {root.tag!r}")
    osm_base = next(
        (element.attrib.get("osm_base") for element in root if element.tag == "meta"), None
    )
    nodes = {
        element.attrib["id"]: (float(element.attrib["lon"]), float(element.attrib["lat"]))
        for element in root.findall("node")
    }
    traffic_signals = 0
    for node in root.findall("node"):
        tags = {tag.attrib.get("k"): tag.attrib.get("v") for tag in node.findall("tag")}
        traffic_signals += tags.get("highway") == "traffic_signals"

    highway_counts: Counter[str] = Counter()
    oneway_counts: Counter[str] = Counter()
    restriction_counts: Counter[str] = Counter()
    for relation in root.findall("relation"):
        tags = {tag.attrib.get("k"): tag.attrib.get("v") for tag in relation.findall("tag")}
        if tags.get("type") == "restriction":
            restriction_counts[tags.get("restriction", "unspecified")] += 1

    directed: dict[str, set[str]] = {}
    undirected: dict[str, set[str]] = {}
    highway_way_count = 0
    routable_way_count = 0
    for way in root.findall("way"):
        tags = {tag.attrib.get("k"): tag.attrib.get("v") for tag in way.findall("tag")}
        highway = tags.get("highway")
        if highway is None:
            continue
        highway_counts[highway] += 1
        highway_way_count += 1
        if highway not in ALLOWED_HIGHWAYS:
            continue
        if any(tags.get(key) in {"no", "private"} for key in ("access", "motor_vehicle", "motorcar")):
            continue
        refs = [node.attrib["ref"] for node in way.findall("nd")]
        if len(refs) < 2 or any(ref not in nodes for ref in refs):
            continue
        routable_way_count += 1
        oneway = tags.get("oneway", "").lower()
        if oneway:
            oneway_counts[oneway] += 1
        is_roundabout = tags.get("junction") == "roundabout"
        forward_only = oneway in {"yes", "1", "true"} or (not oneway and is_roundabout)
        reverse_only = oneway == "-1"
        for first, second in zip(refs, refs[1:]):
            directed.setdefault(first, set())
            directed.setdefault(second, set())
            undirected.setdefault(first, set()).add(second)
            undirected.setdefault(second, set()).add(first)
            if reverse_only:
                directed[second].add(first)
            elif forward_only:
                directed[first].add(second)
            else:
                directed[first].add(second)
                directed[second].add(first)

    components = 0
    largest_component = 0
    unseen = set(undirected)
    while unseen:
        components += 1
        seed = unseen.pop()
        component_size = 1
        queue = deque([seed])
        while queue:
            current = queue.popleft()
            for nxt in undirected[current]:
                if nxt in unseen:
                    unseen.remove(nxt)
                    component_size += 1
                    queue.append(nxt)
        largest_component = max(largest_component, component_size)

    with args.hotspots.open(encoding="utf-8", newline="") as source:
        heat_rows = [
            row for row in csv.DictReader(source)
            if row["endpoint_role"] == "pickup_proxy"
            and BBOX["west"] <= float(row["center_lon"]) <= BBOX["east"]
            and BBOX["south"] <= float(row["center_lat"]) <= BBOX["north"]
        ]
    heat_rows.sort(key=lambda row: (-int(row["trip_count"]), int(row["cell_y"]), int(row["cell_x"])))
    routable_node_ids = list(directed)
    snapped = []
    for row in heat_rows[: args.top_k]:
        lon, lat = float(row["center_lon"]), float(row["center_lat"])
        nearest_id, nearest_distance = min(
            (
                (node_id, distance_m(lon, lat, *nodes[node_id]))
                for node_id in routable_node_ids
            ),
            key=lambda item: item[1],
        )
        snapped.append({
            "cell_x": int(row["cell_x"]),
            "cell_y": int(row["cell_y"]),
            "heat_count": int(row["trip_count"]),
            "center_lon": lon,
            "center_lat": lat,
            "nearest_osm_node_id": nearest_id,
            "nearest_node_distance_m": round(nearest_distance, 2),
        })

    pair_checks = len(snapped) * len(snapped)
    reachable_pairs = sum(
        reachable(first["nearest_osm_node_id"], second["nearest_osm_node_id"], directed)
        for first in snapped for second in snapped
    )
    result = {
        "status": "structural_preflight_only",
        "candidate_fence": BBOX,
        "osm_base_timestamp": osm_base,
        "osm_sha256": sha256(args.osm),
        "counts": {
            "osm_nodes_in_extract": len(nodes),
            "osm_ways_in_extract": len(root.findall("way")),
            "highway_ways": highway_way_count,
            "routable_ways_after_simple_access_filter": routable_way_count,
            "routable_directed_graph_nodes": len(directed),
            "routable_directed_arcs": sum(map(len, directed.values())),
            "weak_components": components,
            "largest_weak_component_nodes": largest_component,
            "highway_traffic_signal_nodes": traffic_signals,
            "turn_restriction_relations": sum(restriction_counts.values()),
            "turn_restriction_types": dict(sorted(restriction_counts.items())),
            "oneway_tags_on_routable_ways": dict(sorted(oneway_counts.items())),
            "reachable_ordered_top_hotspot_pairs_before_turn_restrictions": reachable_pairs,
            "tested_ordered_top_hotspot_pairs": pair_checks,
        },
        "top_pickup_hotspot_node_preflight": snapped,
        "limitations": [
            "OSM turn-restriction relations are counted but not enforced in the connectivity graph.",
            "Hotspot cell centers are snapped to nearest routable OSM nodes for this preflight; this is not final point-to-directed-link binding.",
            "Access tags are filtered with a small fixed rule set; conditional access and time-dependent restrictions are not modeled.",
            "Signal nodes indicate mapped signal presence only; signal phases and queues are not present in the OSM snapshot.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
