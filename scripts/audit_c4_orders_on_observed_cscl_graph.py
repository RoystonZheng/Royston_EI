#!/usr/bin/env python3
"""Optimistic C4 zone-to-zone reachability screen on observed-cost arcs."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from audit_ezpass_cscl_match import CSCL_PATH, project, distance  # noqa: E402

OVERLAY_DIR = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_cost_overlay_preflight_v1"
ZONE_PATH = ROOT / "data/dispatch/nyc_dot_ezpass_2021_2024/c4_zone_subset_2026-10-20/taxi_zones.geojson"
ORDERS_PATH = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/multi_vehicle_competition_scenarios_v1/C4_manhattan_790_order_five_minute_load_sweep/orders.json"
OUTPUT = OVERLAY_DIR / "c4_zone_observed_graph_reachability.json"


class DSU:
    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def point_on_segment(p, a, b):
    cross = (p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])
    if abs(cross) > 1e-5:
        return False
    dot = (p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])
    if dot < 0:
        return False
    return dot <= (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2


def point_in_ring(p, ring):
    inside = False
    x, y = p
    for a, b in zip(ring, ring[1:]):
        if point_on_segment(p, a, b):
            return True
        x1, y1 = a
        x2, y2 = b
        if (y1 > y) != (y2 > y):
            x_cross = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < x_cross:
                inside = not inside
    return inside


def orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a, b, c, d):
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if ((o1 > 0 and o2 < 0) or (o1 < 0 and o2 > 0)) and ((o3 > 0 and o4 < 0) or (o3 < 0 and o4 > 0)):
        return True
    return (abs(o1) < 1e-5 and point_on_segment(c, a, b)) or (abs(o2) < 1e-5 and point_on_segment(d, a, b)) or (abs(o3) < 1e-5 and point_on_segment(a, c, d)) or (abs(o4) < 1e-5 and point_on_segment(b, c, d))


def point_in_polygon(p, polygon):
    if not polygon or not point_in_ring(p, polygon[0]):
        return False
    return not any(point_in_ring(p, hole) for hole in polygon[1:])


def line_segment_intersects_polygon(a, b, polygons):
    for polygon in polygons:
        if point_in_polygon(a, polygon) or point_in_polygon(b, polygon):
            return True
        for ring in polygon:
            for c, d in zip(ring, ring[1:]):
                if segments_intersect(a, b, c, d):
                    return True
    return False


def main():
    cscl = json.loads(CSCL_PATH.read_text())
    geometry = {}
    for row in cscl:
        fid = str(row.get("physicalid"))
        geometry[fid] = [[project((p[0], p[1])) for p in part] for part in row.get("the_geom", {}).get("coordinates", [])]
    features = json.loads(ZONE_PATH.read_text())["features"]
    zones = {}
    for feature in features:
        props = feature["properties"]
        geom = feature["geometry"]
        if geom["type"] == "Polygon":
            raw_polygons = [geom["coordinates"]]
        else:
            raw_polygons = geom["coordinates"]
        polygons = []
        for polygon in raw_polygons:
            polygons.append([[project((p[0], p[1])) for p in ring] for ring in polygon])
        zones[str(props["locationid"])] = {"zone": props["zone"], "borough": props["borough"], "polygons": polygons}

    arc_map = {}
    with (OVERLAY_DIR / "directed_cost_candidates.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if float(row["primitive_coverage_fraction_for_this_sensor"]) < 0.95:
                continue
            fid = row["physicalid"]
            part_idx, seg_idx = int(row["part_index"]), int(row["primitive_index"])
            direction = row["travel_direction_on_cscl"]
            key = (fid, part_idx, seg_idx, direction)
            if key in arc_map:
                continue
            a, b = geometry[fid][part_idx][seg_idx:seg_idx + 2]
            if direction == "TF":
                a, b = b, a
            arc_map[key] = {"key": key, "start": a, "end": b, "length_m": distance(a, b), "sensor_sid": row["sensor_sid"]}
    arcs = list(arc_map.values())

    # Cluster observed full-coverage arc endpoints at the prior 10m optimistic
    # upper-bound tolerance and build the directed graph.
    endpoints = [arc["start"] for arc in arcs] + [arc["end"] for arc in arcs]
    split = len(arcs)
    dsu = DSU(len(endpoints))
    for i, p in enumerate(endpoints):
        for j in range(i + 1, len(endpoints)):
            if distance(p, endpoints[j]) <= 10.0:
                dsu.union(i, j)
    roots = [dsu.find(i) for i in range(len(endpoints))]
    node_id = {root: ix for ix, root in enumerate(sorted(set(roots)))}
    starts = [node_id[roots[i]] for i in range(split)]
    ends = [node_id[roots[i + split]] for i in range(split)]
    outgoing = defaultdict(set)
    reverse = defaultdict(set)
    weak = defaultdict(set)
    for u, v in zip(starts, ends):
        outgoing[u].add(v)
        reverse[v].add(u)
        weak[u].add(v)
        weak[v].add(u)
    for n in node_id.values():
        outgoing.setdefault(n, set())
        reverse.setdefault(n, set())
        weak.setdefault(n, set())

    zone_nodes = defaultdict(set)
    zone_arcs = defaultdict(set)
    for arc_idx, arc in enumerate(arcs):
        for zid, z in zones.items():
            if line_segment_intersects_polygon(arc["start"], arc["end"], z["polygons"]):
                zone_arcs[zid].add(arc_idx)
                zone_nodes[zid].add(starts[arc_idx])
                zone_nodes[zid].add(ends[arc_idx])

    orders_data = json.loads(ORDERS_PATH.read_text())
    orders = orders_data["orders"]
    requested_zone_ids = sorted({
        str(o[key]).removeprefix("zone_")
        for o in orders for key in ("pickup_node_id", "dropoff_node_id")
    }, key=int)
    missing_polygon_zone_ids = sorted(set(requested_zone_ids) - set(zones), key=int)
    counts = defaultdict(int)
    samples = {}
    for order in orders:
        pu = str(order["pickup_node_id"]).removeprefix("zone_")
        do = str(order["dropoff_node_id"]).removeprefix("zone_")
        counts[(pu, do)] += 1
        samples.setdefault((pu, do), order["order_id"])

    zone_stats = {}
    for zid, z in sorted(zones.items(), key=lambda item: int(item[0])):
        z_arcs = zone_arcs[zid]
        zone_stats[zid] = {
            "zone": z["zone"],
            "borough": z["borough"],
            "full_observed_directed_arcs_intersecting_polygon": len(z_arcs),
            "attached_graph_nodes_optimistic": len(zone_nodes[zid]),
            "c4_pickup_order_count": sum(n for (pu, _), n in counts.items() if pu == zid),
            "c4_destination_order_count": sum(n for (_, do), n in counts.items() if do == zid),
        }

    # Optimistic route upper bound: any cost-covered road arc intersecting a
    # zone can be an origin/destination attachment, with no within-zone access cost.
    pair_results = []
    routed_order_total = 0
    eligible_order_total = 0
    both_attach_total = 0
    same_weak_component_total = 0
    for (pu, do), order_count in sorted(counts.items(), key=lambda x: (-x[1], int(x[0][0]), int(x[0][1]))):
        src = zone_nodes.get(pu, set())
        dst = zone_nodes.get(do, set())
        both_attach = bool(src) and bool(dst)
        reached = set(src)
        q = deque(src)
        while q:
            node = q.popleft()
            for nxt in outgoing[node]:
                if nxt not in reached:
                    reached.add(nxt)
                    q.append(nxt)
        directed_reachable = bool(reached.intersection(dst)) if both_attach else False
        if both_attach:
            both_attach_total += order_count
        if directed_reachable:
            routed_order_total += order_count
        pair_results.append({
            "pickup_zone_id": pu,
            "pickup_zone_name": zones.get(pu, {}).get("zone"),
            "dropoff_zone_id": do,
            "dropoff_zone_name": zones.get(do, {}).get("zone"),
            "order_count": order_count,
            "pickup_zone_has_observed_graph_attachment": bool(src),
            "dropoff_zone_has_observed_graph_attachment": bool(dst),
            "directed_path_exists_under_optimistic_zone_attachment": directed_reachable,
            "representative_order_id": samples[(pu, do)],
        })

    summary = {
        "classification": "optimistic C4 route-reachability upper-bound screen on full-observed-cost Manhattan arcs; not a C4 route or dispatch run",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs_sha256": {
            "cscl_manhattan": hashlib.sha256(CSCL_PATH.read_bytes()).hexdigest(),
            "taxi_zone_subset": hashlib.sha256(ZONE_PATH.read_bytes()).hexdigest(),
            "c4_orders": hashlib.sha256(ORDERS_PATH.read_bytes()).hexdigest(),
            "cost_candidate_csv": hashlib.sha256((OVERLAY_DIR / "directed_cost_candidates.csv").read_bytes()).hexdigest(),
        },
        "full_observed_directed_arcs": len(arcs),
        "endpoint_snap_tolerance_m": 10,
        "c4_orders": len(orders),
        "distinct_c4_od_zone_pairs": len(counts),
        "c4_od_zone_ids_requested": len(requested_zone_ids),
        "c4_od_zone_ids_with_current_tlc_polygon": len(set(requested_zone_ids).intersection(zones)),
        "c4_zone_ids_missing_current_tlc_polygon": missing_polygon_zone_ids,
        "c4_orders_with_missing_dropoff_polygon": sum(
            1 for o in orders if str(o["dropoff_node_id"]).removeprefix("zone_") in missing_polygon_zone_ids
        ),
        "c4_pickup_zones_without_full_observed_arc_intersection": [
            zid for zid, z in sorted(zones.items(), key=lambda item: int(item[0]))
            if any(str(o["pickup_node_id"]).removeprefix("zone_") == zid for o in orders)
            and not zone_arcs[zid]
        ],
        "c4_orders_picked_up_in_zones_without_full_observed_arc": sum(
            1 for o in orders
            if str(o["pickup_node_id"]).removeprefix("zone_") in {
                zid for zid, z in zones.items()
                if any(str(order["pickup_node_id"]).removeprefix("zone_") == zid for order in orders)
                and not zone_arcs[zid]
            }
        ),
        "c4_orders_with_both_zone_polygons_attached_to_observed_graph": both_attach_total,
        "c4_orders_with_directed_path_under_optimistic_attachment": routed_order_total,
        "optimistic_directed_route_reachability_rate": round(routed_order_total / len(orders), 5) if orders else 0,
        "c4_pickup_zone_stats": zone_stats,
        "limitations": [
            "A zone attaches if any full-observation road primitive intersects its polygon; this grants optimistic free access to any node in the zone.",
            "No precise vehicle/pickup point, within-zone access distance, turn restrictions, grade separation, or exact endpoint snap is represented.",
            "Only a directed arc primitive at least 95% covered by one E-ZPass sensor is included; partial observed arcs are omitted.",
            "The graph covers only Manhattan CSCL and observed subnetwork fragments; destination zones outside the selected Manhattan polygons cannot attach.",
            "A reachable pair is an upper-bound screening result, not a validated shortest route or travel-time estimate.",
        ],
    }
    result = {"summary": summary, "od_pairs": pair_results}
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    compact = {k: v for k, v in summary.items() if k not in {"c4_pickup_zone_stats", "limitations", "inputs_sha256"}}
    print(json.dumps(compact, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
