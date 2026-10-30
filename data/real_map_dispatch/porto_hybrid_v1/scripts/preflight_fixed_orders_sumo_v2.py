#!/usr/bin/env python3
"""Preflight fixed Porto order legs against the frozen SUMO directed network.

This is a route-validity gate only. It does not run traffic, dispatch vehicles,
or report experimental performance. OSM via-way restrictions that netconvert
cannot represent are checked explicitly against the returned edge sequence.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

import networkx as nx
import sumolib


PACKAGE = Path(__file__).resolve().parents[1]
SCENARIOS = PACKAGE / "derived/scenarios/v2"
NETWORK = PACKAGE / "derived/sumo/v1.28.0/porto.net.xml"
BBOX = (-8.635, 41.138, -8.595, 41.162)
FUZZY_MAX_SNAP_M = 50.0
CANDIDATE_MAX_SNAP_M = 300.0
MAX_MATCHES_PER_POINT = 8
MAX_MATCH_DISTANCE_SPREAD_M = 12.0

# netconvert reports that these two valid, passenger-routable via-way
# restrictions cannot be applied. The OSM member order is a directed sequence.
FORBIDDEN_WAY_SEQUENCES = (
    ("95843585", "676985051", "676370076", "676370075"),  # no_left_turn
    ("1165465725", "210669783", "95843585", "1165463267"),  # no_straight_on
)


def osm_way_id(edge_id: str) -> str:
    return edge_id.lstrip("-").split("#", 1)[0]


def edge_is_inside_fence(net: Any, edge: Any) -> bool:
    shape = edge.getShape()
    if not shape:
        return False
    west, south, east, north = BBOX
    for x, y in shape:
        lon, lat = net.convertXY2LonLat(x, y)
        if not (west - 1e-7 <= lon <= east + 1e-7 and south - 1e-7 <= lat <= north + 1e-7):
            return False
    return True


def build_route_graph(net: Any) -> tuple[nx.DiGraph, dict[str, Any]]:
    allowed = {
        edge.getID(): edge
        for edge in net.getEdges()
        if edge.allows("passenger") and edge_is_inside_fence(net, edge)
    }
    graph = nx.DiGraph()
    for edge_id, edge in allowed.items():
        speed = max(0.1, edge.getSpeed())
        graph.add_node(edge_id, length_m=edge.getLength(), speed_mps=speed)

    for edge_id, edge in allowed.items():
        for next_edge in edge.getOutgoing():
            next_id = next_edge.getID()
            if next_id not in allowed:
                continue
            graph.add_edge(
                edge_id,
                next_id,
                weight=allowed[next_id].getLength() / max(0.1, allowed[next_id].getSpeed()),
            )
    return graph, allowed


def is_legal_sequence(path: list[str]) -> bool:
    compressed: list[str] = []
    for edge_id in path:
        way_id = osm_way_id(edge_id)
        if not compressed or way_id != compressed[-1]:
            compressed.append(way_id)
    for forbidden in FORBIDDEN_WAY_SEQUENCES:
        width = len(forbidden)
        if any(tuple(compressed[i : i + width]) == forbidden for i in range(len(compressed) - width + 1)):
            return False
    return True


def map_match_point(net: Any, point: dict[str, Any], allowed_edges: dict[str, Any]) -> list[dict[str, Any]]:
    point_id = point["point_id"]
    lon, lat = float(point["lon"]), float(point["lat"])
    xy = net.convertLonLat2XY(lon, lat)
    is_fuzzy = point.get("point_kind") == "fuzzy_road_feature_sample"
    radius = FUZZY_MAX_SNAP_M if is_fuzzy else CANDIDATE_MAX_SNAP_M
    matches: list[dict[str, Any]] = []
    for edge, _shape_distance in net.getNeighboringEdges(*xy, r=radius, includeJunctions=False):
        edge_id = edge.getID()
        if edge_id not in allowed_edges:
            continue
        lane_index, lane_pos, snap_distance = edge.getClosestLanePosDist(xy)
        if snap_distance > radius:
            continue
        matches.append({
            "edge_id": edge_id,
            "lane_index": int(lane_index),
            "edge_pos_m": round(float(lane_pos), 3),
            "snap_distance_m": round(float(snap_distance), 3),
        })
    matches.sort(key=lambda row: (row["snap_distance_m"], row["edge_id"], row["lane_index"]))
    if not matches:
        return []
    cutoff = matches[0]["snap_distance_m"] + MAX_MATCH_DISTANCE_SPREAD_M
    kept = [m for m in matches if m["snap_distance_m"] <= cutoff][:MAX_MATCHES_PER_POINT]
    for idx, match in enumerate(kept):
        match["candidate_rank"] = idx + 1
    return kept


def free_flow_cost(path: list[str], start_match: dict[str, Any], end_match: dict[str, Any], allowed: dict[str, Any]) -> float | None:
    if not path:
        return None
    if len(path) == 1:
        if end_match["edge_pos_m"] < start_match["edge_pos_m"]:
            return None
        speed = max(0.1, allowed[path[0]].getSpeed())
        return (end_match["edge_pos_m"] - start_match["edge_pos_m"]) / speed
    first = allowed[path[0]]
    last = allowed[path[-1]]
    total = (first.getLength() - start_match["edge_pos_m"]) / max(0.1, first.getSpeed())
    for edge_id in path[1:-1]:
        edge = allowed[edge_id]
        total += edge.getLength() / max(0.1, edge.getSpeed())
    total += end_match["edge_pos_m"] / max(0.1, last.getSpeed())
    return total


def find_legal_path(
    graph: nx.DiGraph,
    start_match: dict[str, Any],
    end_match: dict[str, Any],
    allowed: dict[str, Any],
) -> tuple[list[str] | None, float | None]:
    source, target = start_match["edge_id"], end_match["edge_id"]
    if source == target:
        direct = free_flow_cost([source], start_match, end_match, allowed)
        if direct is not None:
            return [source], direct
        if not graph.has_edge(source, source):
            # A loop may still return to the same directed edge. Search a cycle
            # through its outgoing links without reusing the trivial zero path.
            best: tuple[list[str], float] | None = None
            for neighbor in graph.successors(source):
                try:
                    tail = nx.shortest_path(graph, neighbor, source, weight="weight")
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                path = [source] + tail
                if not is_legal_sequence(path):
                    continue
                cost = free_flow_cost(path, start_match, end_match, allowed)
                if cost is not None and (best is None or cost < best[1]):
                    best = (path, cost)
            return best if best is not None else (None, None)

    try:
        paths = nx.shortest_simple_paths(graph, source, target, weight="weight")
        for index, path in enumerate(paths):
            if index >= 100:
                break
            if not is_legal_sequence(path):
                continue
            cost = free_flow_cost(path, start_match, end_match, allowed)
            if cost is not None:
                return path, cost
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None, None
    return None, None


def route_between_points(
    graph: nx.DiGraph,
    allowed: dict[str, Any],
    matches_by_point: dict[str, list[dict[str, Any]]],
    from_point: str,
    to_point: str,
    source_cache: dict[str, tuple[dict[str, float], dict[str, list[str]]]],
    alternative_cache: dict[tuple[str, str], list[str] | None],
) -> dict[str, Any]:
    starts, ends = matches_by_point.get(from_point, []), matches_by_point.get(to_point, [])
    best: dict[str, Any] | None = None
    for start_match in starts:
        source = start_match["edge_id"]
        if source not in source_cache:
            source_cache[source] = nx.single_source_dijkstra(graph, source, weight="weight")
        distances, paths = source_cache[source]
        for end_match in ends:
            target = end_match["edge_id"]
            path = paths.get(target)
            if path is not None and not is_legal_sequence(path):
                pair = (source, target)
                if pair not in alternative_cache:
                    legal_alternative: list[str] | None = None
                    try:
                        alternatives = nx.shortest_simple_paths(graph, source, target, weight="weight")
                        for index, candidate in enumerate(alternatives):
                            if index >= 100:
                                break
                            if is_legal_sequence(candidate):
                                legal_alternative = candidate
                                break
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        pass
                    alternative_cache[pair] = legal_alternative
                path = alternative_cache[pair]
            if source == target and end_match["edge_pos_m"] < start_match["edge_pos_m"]:
                path = None
                # Returning to an earlier position on the same directed edge
                # requires a cycle; do not accept the zero-length self path.
                for neighbor in graph.successors(source):
                    try:
                        tail = nx.shortest_path(graph, neighbor, source, weight="weight")
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        continue
                    candidate = [source] + tail
                    if is_legal_sequence(candidate):
                        candidate_cost = free_flow_cost(candidate, start_match, end_match, allowed)
                        if candidate_cost is not None and (path is None or candidate_cost < path[1]):
                            path = (candidate, candidate_cost)
                if isinstance(path, tuple):
                    route_path, travel_s = path
                else:
                    continue
            else:
                route_path = path
                travel_s = free_flow_cost(route_path, start_match, end_match, allowed) if route_path else None
            if path is None or travel_s is None:
                continue
            score = travel_s + (start_match["snap_distance_m"] + end_match["snap_distance_m"]) / 100.0
            if best is None or score < best["selection_score"]:
                distance = 0.0
                if len(route_path) == 1:
                    distance = max(0.0, end_match["edge_pos_m"] - start_match["edge_pos_m"])
                else:
                    distance = allowed[route_path[0]].getLength() - start_match["edge_pos_m"]
                    distance += sum(allowed[eid].getLength() for eid in route_path[1:-1])
                    distance += end_match["edge_pos_m"]
                best = {
                    "status": "reachable",
                    "start_match": start_match,
                    "end_match": end_match,
                    "path": route_path,
                    "free_flow_time_s": round(travel_s, 3),
                    "distance_m": round(distance, 3),
                    "selection_score": score,
                }
    return best or {"status": "unreachable", "start_match": None, "end_match": None, "path": []}


def point_registry(order_file: dict[str, Any]) -> dict[str, dict[str, Any]]:
    points: dict[str, dict[str, Any]] = {}
    for vehicle in order_file["vehicles"]:
        p = vehicle["initial_position"]
        points[p["point_id"]] = p
    for order in order_file["orders"]:
        for key in ("pickup", "dropoff"):
            p = order[key]
            points[p["point_id"]] = p
    return points


def run_seed(seed: int, net: Any, graph: nx.DiGraph, allowed: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    order_path = SCENARIOS / f"orders_seed_{seed}_draft.json"
    order_file = json.loads(order_path.read_text(encoding="utf-8"))
    points = point_registry(order_file)
    matches_by_point = {point_id: map_match_point(net, point, allowed) for point_id, point in points.items()}
    match_failures = [point_id for point_id, matches in matches_by_point.items() if not matches]

    task_rows: list[dict[str, Any]] = []
    source_cache: dict[str, tuple[dict[str, float], dict[str, list[str]]]] = {}
    alternative_cache: dict[tuple[str, str], list[str] | None] = {}
    for task in order_file["route_legs"]:
        path_result = route_between_points(
            graph, allowed, matches_by_point, task["from_point_id"], task["to_point_id"],
            source_cache, alternative_cache,
        )
        task_rows.append({
            "route_task_id": task["route_task_id"],
            "vehicle_id": task["vehicle_id"],
            "order_id": task["order_id"],
            "leg_role": task["leg_role"],
            "from_point_id": task["from_point_id"],
            "to_point_id": task["to_point_id"],
            "status": path_result["status"],
            "start_edge_id": (path_result["start_match"] or {}).get("edge_id"),
            "end_edge_id": (path_result["end_match"] or {}).get("edge_id"),
            "start_snap_distance_m": (path_result["start_match"] or {}).get("snap_distance_m"),
            "end_snap_distance_m": (path_result["end_match"] or {}).get("snap_distance_m"),
            "distance_m": path_result.get("distance_m"),
            "free_flow_time_s": path_result.get("free_flow_time_s"),
            "validated_link_sequence": " ".join(path_result.get("path", [])),
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"route_legs_seed_{seed}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(task_rows[0]))
        writer.writeheader()
        writer.writerows(task_rows)

    matches_payload = {
        "scenario_id": order_file["scenario_id"],
        "status": "preflight_match_candidates",
        "point_count": len(points),
        "max_match_count_per_point": MAX_MATCHES_PER_POINT,
        "candidate_snap_radius_m": CANDIDATE_MAX_SNAP_M,
        "fuzzy_snap_radius_m": FUZZY_MAX_SNAP_M,
        "endpoint_matches": matches_by_point,
    }
    (out_dir / f"endpoint_matches_seed_{seed}.json").write_text(
        json.dumps(matches_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    reachable = sum(row["status"] == "reachable" for row in task_rows)
    summary = {
        "seed": seed,
        "order_file_sha256": __import__("hashlib").sha256(order_path.read_bytes()).hexdigest(),
        "point_count": len(points),
        "point_match_failures": match_failures,
        "route_legs": len(task_rows),
        "reachable_route_legs": reachable,
        "unreachable_route_legs": len(task_rows) - reachable,
        "status": "passed" if not match_failures and reachable == len(task_rows) else "failed",
        "scope": "static reachability only; no SUMO traffic simulation or performance result",
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, action="append", help="seed to preflight; repeat to select multiple")
    parser.add_argument("--all-seeds", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=SCENARIOS / "preflight_v2")
    parser.add_argument("--network", type=Path, default=NETWORK)
    args = parser.parse_args()
    seeds = list(range(10)) if args.all_seeds else (args.seed or [0])

    net = sumolib.net.readNet(str(args.network), withInternal=False)
    graph, allowed = build_route_graph(net)
    summaries = [run_seed(seed, net, graph, allowed, args.output_dir) for seed in seeds]
    result = {
        "network": str(args.network),
        "network_edge_count": len(net.getEdges()),
        "fenced_passenger_edge_count": graph.number_of_nodes(),
        "fenced_connection_count": graph.number_of_edges(),
        "custom_via_way_restrictions": [list(x) for x in FORBIDDEN_WAY_SEQUENCES],
        "seeds": summaries,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(row["status"] == "passed" for row in summaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
