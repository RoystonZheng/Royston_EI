#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a reproducible, static Dijkstra preflight for the Porto route study.

This is a structural pilot, not the SUMO dynamic experiment. It reads the
order-demand package, generates a fixed pilot sequence, builds a directed OSM
graph, enforces supported via-node turn restrictions, and saves route artifacts
to a new output directory without touching existing results.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import random
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/real_map_dispatch/porto_hybrid_v1"
DEFAULT_PACKAGE = DATA / "map_import/porto-map-order-package-v1.json"
DEFAULT_OSM = DATA / "raw/osm/porto_candidate.osm"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/porto_traditional_preflight_20261023_v7"
BBOX = {"west": -8.635, "south": 41.138, "east": -8.595, "north": 41.162}
ALLOWED_HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "road", "motorway_link", "trunk_link",
    "primary_link", "secondary_link", "tertiary_link",
}
FREE_FLOW_KMH = {
    "motorway": 70, "motorway_link": 45, "trunk": 60, "trunk_link": 40,
    "primary": 50, "primary_link": 35, "secondary": 40, "secondary_link": 30,
    "tertiary": 35, "tertiary_link": 28, "unclassified": 30,
    "residential": 30, "living_street": 15, "road": 25,
}
MAX_SNAP_M = 300.0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inside(lon: float, lat: float) -> bool:
    return BBOX["west"] <= lon <= BBOX["east"] and BBOX["south"] <= lat <= BBOX["north"]


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat_mid = math.radians((a[1] + b[1]) / 2.0)
    dx = (a[0] - b[0]) * 111_320.0 * math.cos(lat_mid)
    dy = (a[1] - b[1]) * 111_320.0
    return math.hypot(dx, dy)


def interpolate(a: tuple[float, float], b: tuple[float, float], meters: float) -> tuple[float, float]:
    length = distance_m(a, b)
    ratio = 0.0 if length < 0.01 else min(1.0, meters / length)
    return (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio)


def tags(element: ET.Element) -> dict[str, str]:
    return {child.attrib["k"]: child.attrib["v"] for child in element.findall("tag")}


def candidate_points(package: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
    rows = [p for p in package["pickup_points"] if inside(float(p["lon"]), float(p["lat"]))]
    rows.sort(key=lambda p: (-int(p["count"]), str(p["point_id"])))
    if len(rows) < top_k:
        raise ValueError(f"only {len(rows)} pickup points fall inside the candidate fence")
    return [dict(p, point_kind="candidate_pickup") for p in rows[:top_k]]


def parse_graph(osm_path: Path) -> tuple[dict[str, tuple[float, float]], dict[str, list[dict[str, Any]]], dict[str, Any], list[dict[str, Any]]]:
    root = ET.parse(osm_path).getroot()
    nodes = {
        node.attrib["id"]: (float(node.attrib["lon"]), float(node.attrib["lat"]))
        for node in root.findall("node")
    }
    node_tags = {node.attrib["id"]: tags(node) for node in root.findall("node")}
    adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ways_by_node: dict[str, set[str]] = defaultdict(set)
    eligible_segments: list[dict[str, Any]] = []
    eligible_way_ids: set[str] = set()
    road_way_counts: dict[str, int] = defaultdict(int)

    for way in root.findall("way"):
        way_tags = tags(way)
        highway = way_tags.get("highway")
        if highway is None:
            continue
        road_way_counts["highway_ways"] += 1
        if highway not in ALLOWED_HIGHWAYS:
            continue
        if any(way_tags.get(key, "").lower() in {"no", "private"} for key in ("access", "motor_vehicle", "motorcar")):
            road_way_counts["excluded_access_ways"] += 1
            continue
        refs = [nd.attrib["ref"] for nd in way.findall("nd")]
        if len(refs) < 2 or any(ref not in nodes for ref in refs):
            continue
        oneway = way_tags.get("oneway", "").lower()
        forward_only = oneway in {"yes", "1", "true"} or (not oneway and way_tags.get("junction") == "roundabout")
        reverse_only = oneway == "-1"
        eligible_way_ids.add(way.attrib["id"])
        road_way_counts["routable_ways"] += 1
        speed = FREE_FLOW_KMH.get(highway, 25)
        for index, (u, v) in enumerate(zip(refs, refs[1:])):
            a, b = nodes[u], nodes[v]
            length = distance_m(a, b)
            if length <= 0.05:
                continue
            segment = {"way_id": way.attrib["id"], "segment_index": index, "u": u, "v": v,
                       "a": a, "b": b, "length_m": length, "freeflow_s": length / (speed / 3.6),
                       "highway": highway}
            eligible_segments.append(segment)
            ways_by_node[u].add(way.attrib["id"])
            ways_by_node[v].add(way.attrib["id"])
            if reverse_only:
                adjacency[v].append(dict(segment, from_node=v, to_node=u, coordinates=[b, a]))
            elif forward_only:
                adjacency[u].append(dict(segment, from_node=u, to_node=v, coordinates=[a, b]))
            else:
                adjacency[u].append(dict(segment, from_node=u, to_node=v, coordinates=[a, b]))
                adjacency[v].append(dict(segment, from_node=v, to_node=u, coordinates=[b, a]))

    via_node_rules: list[dict[str, str]] = []
    via_way_relations = 0
    conditional_relations = 0
    ignored_mode_exceptions = 0
    restriction_relations = 0
    restriction_types: dict[str, int] = defaultdict(int)
    for relation in root.findall("relation"):
        relation_tags = tags(relation)
        if relation_tags.get("type") != "restriction":
            continue
        restriction_relations += 1
        restriction = relation_tags.get("restriction:motorcar") or relation_tags.get("restriction")
        restriction_types[restriction or "unspecified"] += 1
        if "restriction:conditional" in relation_tags or "restriction:motorcar:conditional" in relation_tags:
            conditional_relations += 1
        exceptions = {v.strip() for v in relation_tags.get("except", "").split(";") if v.strip()}
        if exceptions.intersection({"motorcar", "motor_vehicle", "vehicle"}):
            ignored_mode_exceptions += 1
            continue
        members = [(m.attrib.get("type"), m.attrib.get("ref"), m.attrib.get("role")) for m in relation.findall("member")]
        from_ids = [ref for typ, ref, role in members if role == "from" and typ == "way"]
        to_ids = [ref for typ, ref, role in members if role == "to" and typ == "way"]
        via_nodes = [ref for typ, ref, role in members if role == "via" and typ == "node"]
        via_ways = [ref for typ, ref, role in members if role == "via" and typ == "way"]
        if via_ways:
            via_way_relations += 1
            continue
        if len(from_ids) != 1 or len(to_ids) != 1 or len(via_nodes) != 1:
            continue
        if from_ids[0] not in eligible_way_ids or to_ids[0] not in eligible_way_ids or via_nodes[0] not in nodes:
            continue
        if not (restriction or "").startswith(("no_", "only_")):
            continue
        via_node_rules.append({"via_node": via_nodes[0], "from_way": from_ids[0], "to_way": to_ids[0], "restriction": restriction})

    signals = [n for n, t in node_tags.items() if t.get("highway") == "traffic_signals" and n in nodes and inside(*nodes[n])]
    signal_nodes_on_graph = [n for n in signals if adjacency.get(n)]
    feature_nodes = [n for n in ways_by_node if adjacency.get(n) and (node_tags.get(n, {}).get("highway") == "traffic_signals" or len(ways_by_node[n]) >= 2)]
    stats = {
        "osm_xml_nodes": len(nodes),
        "osm_highway_ways": road_way_counts["highway_ways"],
        "routable_ways_after_access_and_class_filter": road_way_counts["routable_ways"],
        "excluded_access_ways": road_way_counts["excluded_access_ways"],
        "directed_graph_nodes": len({edge["from_node"] for edges in adjacency.values() for edge in edges} | {edge["to_node"] for edges in adjacency.values() for edge in edges}),
        "directed_graph_arcs": sum(len(v) for v in adjacency.values()),
        "physical_segments_in_osm_extract": len(eligible_segments),
        "mapped_signal_nodes_in_fence": len(signals),
        "mapped_signal_nodes_on_routable_graph": len(signal_nodes_on_graph),
        "road_feature_nodes_for_fuzzy_sampling": len(feature_nodes),
        "turn_restriction_relations": restriction_relations,
        "supported_via_node_turn_restrictions": len(via_node_rules),
        "unsupported_via_way_restrictions": via_way_relations,
        "conditional_restriction_relations_not_applied": conditional_relations,
        "restriction_relations_exempting_motorcar": ignored_mode_exceptions,
        "restriction_types": dict(sorted(restriction_types.items())),
    }
    return nodes, adjacency, stats, eligible_segments


def build_fuzzy_features(nodes: dict[str, tuple[float, float]], segments: list[dict[str, Any]], osm_path: Path) -> list[dict[str, Any]]:
    root = ET.parse(osm_path).getroot()
    n_tags = {n.attrib["id"]: tags(n) for n in root.findall("node")}
    ways_by_node: dict[str, set[str]] = defaultdict(set)
    incident: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for seg in segments:
        ways_by_node[seg["u"]].add(seg["way_id"])
        ways_by_node[seg["v"]].add(seg["way_id"])
        incident[seg["u"]].append(seg)
        incident[seg["v"]].append(seg)
    features = []
    for node_id, node_ways in ways_by_node.items():
        if node_id not in nodes or not inside(*nodes[node_id]):
            continue
        if n_tags.get(node_id, {}).get("highway") == "traffic_signals":
            kind = "mapped_traffic_signal"
        elif len(node_ways) >= 2:
            kind = "shared_road_vertex"
        else:
            continue
        internal_segments = [s for s in incident[node_id] if inside(*s["a"]) and inside(*s["b"])]
        if internal_segments:
            features.append({"node_id": node_id, "coordinate": nodes[node_id], "kind": kind, "segments": internal_segments})
    return features


def create_fuzzy(rng: random.Random, features: list[dict[str, Any]], point_id: str) -> dict[str, Any]:
    feature = rng.choice(features)
    segment = rng.choice(feature["segments"])
    anchor = feature["coordinate"]
    other = segment["b"] if segment["u"] == feature["node_id"] else segment["a"]
    offset = rng.uniform(3.0, min(35.0, max(3.0, segment["length_m"])))
    lon, lat = interpolate(anchor, other, offset)
    return {"point_id": point_id, "point_kind": "fuzzy_road_feature", "lon": lon, "lat": lat,
            "uncertainty_radius_m": 35, "feature_hint": feature["kind"], "source_feature_node_id": feature["node_id"]}


def weighted_pick(rng: random.Random, points: list[dict[str, Any]]) -> dict[str, Any]:
    weights = [max(1, int(p["count"])) for p in points]
    return rng.choices(points, weights=weights, k=1)[0]


def make_pilot_orders(candidates: list[dict[str, Any]], features: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    candidate_by_id = {p["point_id"]: p for p in candidates}
    vehicles = []
    for i in range(10):
        init = weighted_pick(rng, candidates)
        vehicles.append({"vehicle_id": f"veh_{i + 1:02d}", "initial_position": init, "fixed_order_sequence": []})
    kinds = (["candidate_to_candidate"] * 70 + ["one_candidate_one_fuzzy"] * 15 + ["two_fuzzy"] * 15)
    rng.shuffle(kinds)
    orders = []
    fuzzy_counter = 0
    for i, kind in enumerate(kinds):
        vehicle = vehicles[i % 10]
        if kind == "candidate_to_candidate":
            pickup, dropoff = weighted_pick(rng, candidates), weighted_pick(rng, candidates)
        elif kind == "one_candidate_one_fuzzy":
            fuzzy_counter += 1
            fuzzy = create_fuzzy(rng, features, f"fuzzy_{fuzzy_counter:03d}")
            candidate = weighted_pick(rng, candidates)
            pickup, dropoff = (candidate, fuzzy) if rng.random() < 0.5 else (fuzzy, candidate)
        else:
            fuzzy_counter += 1
            pickup = create_fuzzy(rng, features, f"fuzzy_{fuzzy_counter:03d}")
            fuzzy_counter += 1
            dropoff = create_fuzzy(rng, features, f"fuzzy_{fuzzy_counter:03d}")
        order_id = f"ord_{i + 1:04d}"
        vehicle["fixed_order_sequence"].append(order_id)
        orders.append({"order_id": order_id, "vehicle_id": vehicle["vehicle_id"], "sequence_index": len(vehicle["fixed_order_sequence"]) - 1,
                       "order_kind": kind, "pickup": pickup, "dropoff": dropoff,
                       "release_rule": "start planning after this vehicle completes the previous order"})
    return {"scenario_id": f"porto_static_traditional_pilot_seed_{seed}", "status": "pilot_static_preflight_only",
            "formal_experiment": False, "seed": seed, "fleet_size": 10, "orders_per_vehicle": 10,
            "endpoint_mix": {"candidate_to_candidate": 70, "one_candidate_one_fuzzy": 15, "two_fuzzy": 15,
                             "note": "The 30% internal split is a pilot assumption; not frozen for formal runs."},
            "vehicle_order_policy": "fixed per vehicle; no assignment, dispatch, reassignment, or sequence optimization",
            "vehicles": vehicles, "orders": orders}


def replacement_endpoints(kind: str, rng: random.Random, candidates: list[dict[str, Any]],
                          features: list[dict[str, Any]], fuzzy_counter: int) -> tuple[dict[str, Any], dict[str, Any], int]:
    if kind == "candidate_to_candidate":
        return weighted_pick(rng, candidates), weighted_pick(rng, candidates), fuzzy_counter
    if kind == "one_candidate_one_fuzzy":
        fuzzy_counter += 1
        fuzzy = create_fuzzy(rng, features, f"fuzzy_{fuzzy_counter:03d}")
        candidate = weighted_pick(rng, candidates)
        return ((candidate, fuzzy) if rng.random() < 0.5 else (fuzzy, candidate)) + (fuzzy_counter,)
    fuzzy_counter += 1
    pickup = create_fuzzy(rng, features, f"fuzzy_{fuzzy_counter:03d}")
    fuzzy_counter += 1
    dropoff = create_fuzzy(rng, features, f"fuzzy_{fuzzy_counter:03d}")
    return pickup, dropoff, fuzzy_counter


def nearest_node(point: dict[str, Any], routable_nodes: list[str], nodes: dict[str, tuple[float, float]]) -> tuple[str | None, float]:
    coord = (float(point["lon"]), float(point["lat"]))
    # Fuzzy samples retain their source OSM feature node; prefer that anchor
    # over an unrelated nearby node when it is legal in both directions.
    feature_node = point.get("source_feature_node_id")
    if feature_node in set(routable_nodes):
        return feature_node, distance_m(coord, nodes[feature_node])
    node_id, distance = min(((n, distance_m(coord, nodes[n])) for n in routable_nodes), key=lambda x: x[1])
    return (node_id if distance <= MAX_SNAP_M else None), distance


def dijkstra(start: str, adjacency: dict[str, list[dict[str, Any]]], rules_by_key: dict[tuple[str, str], list[dict[str, str]]], metric: str) -> tuple[dict[tuple[str, str | None], float], dict[tuple[str, str | None], tuple[tuple[str, str | None], dict[str, Any]]]]:
    initial = (start, None)
    dist = {initial: 0.0}
    parent: dict[tuple[str, str | None], tuple[tuple[str, str | None], dict[str, Any]]] = {}
    heap = [(0.0, start, "")]
    while heap:
        cost, node, incoming_way = heapq.heappop(heap)
        state = (node, incoming_way or None)
        if cost != dist.get(state):
            continue
        rules = rules_by_key.get((node, incoming_way), []) if incoming_way else []
        no_ways = {r["to_way"] for r in rules if r["restriction"].startswith("no_")}
        only_ways = {r["to_way"] for r in rules if r["restriction"].startswith("only_")}
        for edge in adjacency.get(node, []):
            way = edge["way_id"]
            if way in no_ways or (only_ways and way not in only_ways):
                continue
            weight = edge["length_m"] if metric == "distance" else edge["freeflow_s"]
            next_state = (edge["to_node"], way)
            next_cost = cost + weight
            if next_cost < dist.get(next_state, float("inf")):
                dist[next_state] = next_cost
                parent[next_state] = (state, edge)
                heapq.heappush(heap, (next_cost, edge["to_node"], way))
    return dist, parent


def best_state(target: str, dist: dict[tuple[str, str | None], float]) -> tuple[str, str | None] | None:
    matches = [(cost, state) for state, cost in dist.items() if state[0] == target]
    return min(matches, default=(float("inf"), None), key=lambda x: x[0])[1]


def reconstruct(state: tuple[str, str | None], parent: dict[tuple[str, str | None], tuple[tuple[str, str | None], dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    while state in parent:
        prior, edge = parent[state]
        out.append(edge)
        state = prior
    out.reverse()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--osm", type=Path, default=DEFAULT_OSM)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=30)
    args = parser.parse_args()
    package = json.loads(args.package.read_text(encoding="utf-8"))
    nodes, adjacency, graph_stats, segments = parse_graph(args.osm)
    candidates = candidate_points(package, args.top_k)
    features = build_fuzzy_features(nodes, segments, args.osm)
    graph_stats["road_feature_nodes_for_fuzzy_sampling"] = len(features)
    scenario = make_pilot_orders(candidates, features, args.seed)

    incoming_nodes = {edge["to_node"] for edges in adjacency.values() for edge in edges}
    routable_nodes = sorted(node_id for node_id, edges in adjacency.items() if edges and node_id in incoming_nodes)
    points: dict[str, dict[str, Any]] = {}
    for p in candidates:
        points[p["point_id"]] = p
    for vehicle in scenario["vehicles"]:
        p = vehicle["initial_position"]
        points[p["point_id"]] = p
    for order in scenario["orders"]:
        points[order["pickup"]["point_id"]] = order["pickup"]
        points[order["dropoff"]["point_id"]] = order["dropoff"]
    matches = {pid: nearest_node(p, routable_nodes, nodes) for pid, p in points.items()}

    root = ET.parse(args.osm).getroot()
    rules_by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    total_restriction_relations = 0
    via_way_count = 0
    conditional_count = 0
    mode_exception_count = 0
    for relation in root.findall("relation"):
        rt = tags(relation)
        if rt.get("type") != "restriction":
            continue
        total_restriction_relations += 1
        restriction = rt.get("restriction:motorcar") or rt.get("restriction")
        if "restriction:conditional" in rt or "restriction:motorcar:conditional" in rt:
            conditional_count += 1
        exceptions = {x.strip() for x in rt.get("except", "").split(";") if x.strip()}
        if exceptions.intersection({"motorcar", "motor_vehicle", "vehicle"}):
            mode_exception_count += 1
            continue
        members = [(m.attrib.get("type"), m.attrib.get("ref"), m.attrib.get("role")) for m in relation.findall("member")]
        f = [ref for typ, ref, role in members if role == "from" and typ == "way"]
        t = [ref for typ, ref, role in members if role == "to" and typ == "way"]
        vnodes = [ref for typ, ref, role in members if role == "via" and typ == "node"]
        vways = [ref for typ, ref, role in members if role == "via" and typ == "way"]
        if vways:
            via_way_count += 1
            continue
        if len(f) == len(t) == len(vnodes) == 1 and f[0] in {e["way_id"] for es in adjacency.values() for e in es} and t[0] in {e["way_id"] for es in adjacency.values() for e in es} and restriction and restriction.startswith(("no_", "only_")):
            if vnodes[0] in nodes:
                rules_by_key[(vnodes[0], f[0])].append({"to_way": t[0], "restriction": restriction})

    for i, p in enumerate(points.values()):
        node_id, snap_distance = matches[p["point_id"]]
        p["map_match"] = {"status": "matched" if node_id else "outside_300m_snap_limit", "osm_node_id": node_id,
                           "distance_m": round(snap_distance, 2), "max_snap_m": MAX_SNAP_M}

    route_cache: dict[tuple[str, str], tuple[dict[tuple[str, str | None], float], dict[tuple[str, str | None], tuple[tuple[str, str | None], dict[str, Any]]]]] = {}

    def route_search_exists(start_point: dict[str, Any], end_point: dict[str, Any], metric: str = "time") -> bool:
        start_node = matches[start_point["point_id"]][0]
        end_node = matches[end_point["point_id"]][0]
        if not start_node or not end_node:
            return False
        key = (start_node, metric)
        if key not in route_cache:
            route_cache[key] = dijkstra(start_node, adjacency, rules_by_key, metric)
        return best_state(end_node, route_cache[key][0]) is not None

    def add_match(point: dict[str, Any]) -> None:
        points[point["point_id"]] = point
        node_id, snap_distance = nearest_node(point, routable_nodes, nodes)
        matches[point["point_id"]] = (node_id, snap_distance)
        point["map_match"] = {"status": "matched" if node_id else "outside_300m_snap_limit", "osm_node_id": node_id,
                              "distance_m": round(snap_distance, 2), "max_snap_m": MAX_SNAP_M}

    # Deterministically regenerate only infeasible pilot orders before the
    # sequence is frozen. The assigned vehicle and sequence index stay fixed.
    pilot_rng = random.Random(args.seed + 1_000_003)
    fuzzy_counter = max((int(pid.split("_")[-1]) for pid in points if pid.startswith("fuzzy_")), default=0)
    order_by_id = {order["order_id"]: order for order in scenario["orders"]}
    regeneration_records = []
    unresolved_regenerations = []
    for vehicle in scenario["vehicles"]:
        current = vehicle["initial_position"]
        for order_id in vehicle["fixed_order_sequence"]:
            order = order_by_id[order_id]
            old_pair = [order["pickup"]["point_id"], order["dropoff"]["point_id"]]
            first_leg_ok = route_search_exists(current, order["pickup"])
            passenger_leg_ok = route_search_exists(order["pickup"], order["dropoff"])
            if not (first_leg_ok and passenger_leg_ok):
                replacement = None
                for attempt in range(1, 501):
                    new_pickup, new_dropoff, fuzzy_counter = replacement_endpoints(order["order_kind"], pilot_rng, candidates, features, fuzzy_counter)
                    add_match(new_pickup)
                    add_match(new_dropoff)
                    if route_search_exists(current, new_pickup) and route_search_exists(new_pickup, new_dropoff):
                        replacement = (new_pickup, new_dropoff, attempt)
                        break
                if replacement is None:
                    unresolved_regenerations.append({"order_id": order_id, "old_endpoints": old_pair, "attempt_limit": 500})
                else:
                    order["pickup"], order["dropoff"], attempts = replacement
                    regeneration_records.append({"order_id": order_id, "vehicle_id": vehicle["vehicle_id"],
                                                 "sequence_index": order["sequence_index"], "old_endpoints": old_pair,
                                                 "new_endpoints": [order["pickup"]["point_id"], order["dropoff"]["point_id"]],
                                                 "rejected_draws_before_acceptance": attempts - 1,
                                                 "initial_failed_leg_roles": [name for name, ok in (("vehicle_to_pickup", first_leg_ok), ("pickup_to_dropoff", passenger_leg_ok)) if not ok]})
            current = order["dropoff"]

    scenario["status"] = "pilot_static_preflight_regenerated" if regeneration_records else "pilot_static_preflight_only"
    scenario["preflight_regeneration"] = {"policy": "keep vehicle assignment and sequence; resample only infeasible order endpoints from the same order category; first feasible deterministic draw is accepted",
                                           "rng_seed": args.seed + 1_000_003, "max_draws_per_order": 500,
                                           "replacement_count": len(regeneration_records), "unresolved_count": len(unresolved_regenerations),
                                           "records": regeneration_records, "unresolved": unresolved_regenerations}

    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "pilot_orders.json").write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Candidate-to-candidate links: shortest distance, same directed graph.
    candidate_rows = []
    candidate_features = []
    for source in candidates:
        source_node = matches[source["point_id"]][0]
        for target in candidates:
            target_node = matches[target["point_id"]][0]
            row = {"from_point_id": source["point_id"], "to_point_id": target["point_id"], "from_node": source_node, "to_node": target_node}
            if not source_node or not target_node:
                row.update(status="endpoint_snap_failed", distance_m=None, freeflow_time_s=None, road_link_count=0, way_ids=[])
            else:
                key = (source_node, "distance")
                if key not in route_cache:
                    route_cache[key] = dijkstra(source_node, adjacency, rules_by_key, "distance")
                dist, parent = route_cache[key]
                state = best_state(target_node, dist)
                if state is None:
                    row.update(status="unreachable_under_applied_via_node_restrictions", distance_m=None, freeflow_time_s=None, road_link_count=0, way_ids=[])
                else:
                    path = reconstruct(state, parent)
                    row.update(status="reachable_via_node_only", distance_m=round(sum(e["length_m"] for e in path), 2),
                               freeflow_time_s=round(sum(e["freeflow_s"] for e in path), 2), road_link_count=len(path),
                               way_ids=list(dict.fromkeys(e["way_id"] for e in path)))
                    coordinates = []
                    for edge in path:
                        if not coordinates:
                            coordinates.extend(edge["coordinates"])
                        elif coordinates[-1] == edge["coordinates"][0]:
                            coordinates.append(edge["coordinates"][1])
                        else:
                            coordinates.extend(edge["coordinates"])
                    if coordinates:
                        candidate_features.append({"type": "Feature", "properties": {k: row[k] for k in ("from_point_id", "to_point_id", "distance_m", "freeflow_time_s", "road_link_count", "status")},
                                                  "geometry": {"type": "LineString", "coordinates": coordinates}})
            candidate_rows.append(row)
    with (args.output / "candidate_pair_routes.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["from_point_id", "to_point_id", "from_node", "to_node", "status", "distance_m", "freeflow_time_s", "road_link_count", "way_ids"])
        writer.writeheader()
        for row in candidate_rows:
            row = dict(row, way_ids=json.dumps(row.get("way_ids", []), ensure_ascii=False))
            writer.writerow(row)
    (args.output / "candidate_dijkstra_links.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": candidate_features}, ensure_ascii=False), encoding="utf-8")

    # Fixed per-vehicle sequence: 2 legs per order, no cross-vehicle dispatch.
    vehicle_positions = {v["vehicle_id"]: v["initial_position"] for v in scenario["vehicles"]}
    route_rows = []
    for order in scenario["orders"]:
        vehicle_id = order["vehicle_id"]
        origin = vehicle_positions[vehicle_id]
        for leg_role, start_point, end_point in (("vehicle_to_pickup", origin, order["pickup"]), ("pickup_to_dropoff", order["pickup"], order["dropoff"])):
            start_node = matches[start_point["point_id"]][0]
            end_node = matches[end_point["point_id"]][0]
            row = {"vehicle_id": vehicle_id, "sequence_index": order["sequence_index"], "order_id": order["order_id"], "leg_role": leg_role,
                   "from_point_id": start_point["point_id"], "to_point_id": end_point["point_id"], "from_node": start_node, "to_node": end_node}
            if not start_node or not end_node:
                row.update(status="endpoint_snap_failed", distance_m=None, freeflow_time_s=None, road_link_count=0, way_ids=[])
            else:
                key = (start_node, "time")
                if key not in route_cache:
                    route_cache[key] = dijkstra(start_node, adjacency, rules_by_key, "time")
                dist, parent = route_cache[key]
                state = best_state(end_node, dist)
                if state is None:
                    row.update(status="unreachable_under_applied_via_node_restrictions", distance_m=None, freeflow_time_s=None, road_link_count=0, way_ids=[])
                else:
                    path = reconstruct(state, parent)
                    row.update(status="reachable_via_node_only", distance_m=round(sum(e["length_m"] for e in path), 2),
                               freeflow_time_s=round(sum(e["freeflow_s"] for e in path), 2), road_link_count=len(path),
                               way_ids=list(dict.fromkeys(e["way_id"] for e in path)))
            route_rows.append(row)
            if leg_role == "pickup_to_dropoff":
                vehicle_positions[vehicle_id] = order["dropoff"]
    with (args.output / "fixed_order_route_legs.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["vehicle_id", "sequence_index", "order_id", "leg_role", "from_point_id", "to_point_id", "from_node", "to_node", "status", "distance_m", "freeflow_time_s", "road_link_count", "way_ids"])
        writer.writeheader()
        for row in route_rows:
            writer.writerow(dict(row, way_ids=json.dumps(row.get("way_ids", []), ensure_ascii=False)))

    reachable_candidates = sum(row["status"] == "reachable_via_node_only" for row in candidate_rows)
    reachable_legs = sum(row["status"] == "reachable_via_node_only" for row in route_rows)
    snap_failures = sum(not matches[pid][0] for pid in matches)
    total_distance_m = sum(float(row["distance_m"]) for row in route_rows if row["distance_m"] is not None)
    total_freeflow_s = sum(float(row["freeflow_time_s"]) for row in route_rows if row["freeflow_time_s"] is not None)
    vehicle_freeflow_s: dict[str, float] = defaultdict(float)
    for row in route_rows:
        if row["freeflow_time_s"] is not None:
            vehicle_freeflow_s[row["vehicle_id"]] += float(row["freeflow_time_s"])
    summary = {
        "status": "static_traditional_dijkstra_preflight_pilot",
        "formal_experiment": False,
        "created_at_local_date": "2026-10-23",
        "algorithm": "directed Dijkstra with via-node no_*/only_* turn restrictions",
        "candidate_link_objective": "shortest OSM segment distance",
        "fixed_order_route_objective": "estimated free-flow time using fixed highway-class speeds",
        "fixed_orders": {"vehicles": 10, "orders": 100, "route_legs": 200, "reachable_route_legs": reachable_legs,
                         "unreachable_or_unmatched_route_legs": 200 - reachable_legs, "endpoint_snap_failures": snap_failures,
                         "endpoint_mix": scenario["endpoint_mix"],
                         "deterministic_endpoint_regenerations": len(regeneration_records),
                         "unresolved_regenerations": len(unresolved_regenerations),
                         "planned_route_distance_km_static": round(total_distance_m / 1000, 3),
                         "planned_freeflow_route_time_h_static": round(total_freeflow_s / 3600, 3),
                         "sum_vehicle_freeflow_route_time_h_static": round(sum(vehicle_freeflow_s.values()) / 3600, 3),
                         "fleet_makespan_freeflow_h_static": round(max(vehicle_freeflow_s.values(), default=0) / 3600, 3)},
        "candidate_links": {"candidate_points": len(candidates), "ordered_pairs_including_self": len(candidate_rows),
                            "nonself_ordered_pairs": len(candidate_rows) - len(candidates),
                            "reachable_nonself_under_applied_restrictions": reachable_candidates - len(candidates),
                            "reachable_including_self": reachable_candidates,
                            "unreachable_or_unmatched_including_self": len(candidate_rows) - reachable_candidates},
        "graph": graph_stats,
        "routing_restrictions": {"applied_via_node_relations": sum(len(v) for v in rules_by_key.values()),
                                 "unsupported_via_way_relations": via_way_count,
                                 "other_relations_not_applied_to_routable_graph": max(0, total_restriction_relations - sum(len(v) for v in rules_by_key.values()) - via_way_count - mode_exception_count),
                                 "conditional_relations_not_applied": conditional_count,
                                 "turn_restriction_relations_total": total_restriction_relations},
        "source_files": {"package": str(args.package), "package_sha256": sha256(args.package), "osm": str(args.osm), "osm_sha256": sha256(args.osm)},
        "provenance": {"demand_package_semantics": package["metadata"].get("source_type"),
                       "demand_period": package["metadata"].get("source_period"),
                       "orders_are_synthetic_fixed_pilot_draws_from_aggregate_heat": True,
                       "traffic_data_or_dynamic_simulation_used": False,
                       "sumo_or_traci_used": False},
        "limitations": [
            "This run is a static algorithm preflight, not the dynamic fixed-fleet experiment.",
            "Infeasible pilot orders are deterministically resampled offline within the same order category, preserving each vehicle assignment and sequence; replacement records are stored in pilot_orders.json.",
            "Order endpoints remain inside the candidate fence, while routes may use roads beyond the fence inside the downloaded OSM snapshot to avoid cutting boundary connections; point matching uses the nearest node with both incoming and outgoing arcs within 300 m.",
            "Via-node turn restrictions are applied, but via-way turn restrictions are not enforced; routes are not certified legal.",
            "Some OSM restriction relations reference roads excluded from this passenger-car graph and therefore cannot be applied here.",
            "Free-flow speed is an explicit highway-class estimate; mapped signal phase, congestion, vehicle interactions, planning delay, and actual pass-through time are absent.",
            "The 15/15 split of the 30% fuzzy orders is a pilot assumption because the formal internal split has not been frozen.",
            "The imported Porto package is historical aggregate taxi demand, not real-time ride-hailing orders.",
        ],
        "output_files": ["pilot_orders.json", "fixed_order_route_legs.csv", "candidate_pair_routes.csv", "candidate_dijkstra_links.geojson", "summary.json", "run_notes.md"],
    }
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notes = """# Porto 传统 Dijkstra 静态预检 Pilot\n\n本次从本地 Porto 需求包按围栏内上车热度选出 Top 30 候选点，生成 10 辆车、每车固定 10 单的 pilot 序列。所有车辆订单归属与顺序固定，没有派单、换单或跨车调度。候选点 Link 以道路长度最短为目标；固定订单的 200 条行程腿以道路等级自由流速度估计时间，并运行有向 Dijkstra。\n\n这是传统算法的静态路线与可达性预检，不是完整动态实验结果。路网遵守单行方向并应用可识别的经节点 `no_*` / `only_*` 转向限制。订单端点留在围栏内，车辆路线允许使用已下载 OSM 快照中围栏外的接续道路。点位按 300 米内最近且同时有入边和出边的路网节点吸附；OSM 快照仍有 2 条未支持的经道路限制。初始不可达订单按固定随机种子、保留订单类别/车辆/顺序的规则离线重采样，记录在订单文件中。信号相位、排队、背景车流、其他实验车造成的拥堵、规划耗时和车辆实际逐路段通行时间均未进入本次运行。\n\n需求包包含 2013–2014 年历史出租车轨迹聚合热度与 OD 需求，不含实际逐单订单。为使静态算法能运行，本 pilot 基于 Top 30 候选点和 OSM 道路特征点生成固定合成订单。30% 模糊订单暂按 15 单单侧模糊、15 单双侧模糊构造；这是 pilot 假设，不是已冻结的正式设置。\n\n逐项数量、哈希、预检状态和限制见 `summary.json`；所有候选点方向路线见 `candidate_pair_routes.csv` 与 `candidate_dijkstra_links.geojson`；固定订单和路线腿见 `pilot_orders.json` 与 `fixed_order_route_legs.csv`。\n"""
    (args.output / "run_notes.md").write_text(notes, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
