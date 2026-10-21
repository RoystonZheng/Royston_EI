#!/usr/bin/env python3
"""Connectivity diagnostic for CSCL primitive arcs fully covered by one sensor."""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict, deque
from pathlib import Path

from audit_ezpass_cscl_match import CSCL_PATH, project, distance

ROOT = Path(__file__).resolve().parents[1]
OVERLAY_DIR = ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_cost_overlay_preflight_v1"
OUTPUT = OVERLAY_DIR / "observed_full_primitive_graph_connectivity.json"


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


def components(adjacency):
    seen = set()
    output = []
    for start in adjacency:
        if start in seen:
            continue
        q = [start]
        seen.add(start)
        nodes = []
        while q:
            node = q.pop()
            nodes.append(node)
            for nxt in adjacency[node]:
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        output.append(nodes)
    return output


def strongly_connected_components(graph, reverse):
    seen = set()
    order = []
    for start in graph:
        if start in seen:
            continue
        stack = [(start, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                order.append(node)
                continue
            if node in seen:
                continue
            seen.add(node)
            stack.append((node, True))
            for nxt in graph[node]:
                if nxt not in seen:
                    stack.append((nxt, False))
    seen.clear()
    result = []
    for start in reversed(order):
        if start in seen:
            continue
        q = [start]
        seen.add(start)
        group = []
        while q:
            node = q.pop()
            group.append(node)
            for nxt in reverse[node]:
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        result.append(group)
    return result


def main():
    cscl_rows = json.loads(CSCL_PATH.read_text())
    lookup = {str(row.get("physicalid")): row for row in cscl_rows}
    geometry = {}
    for row in cscl_rows:
        fid = str(row.get("physicalid"))
        parts = []
        for part in row.get("the_geom", {}).get("coordinates", []):
            points = [project((p[0], p[1])) for p in part]
            parts.append(points)
        geometry[fid] = parts

    candidate_path = OVERLAY_DIR / "directed_cost_candidates.csv"
    arcs_by_key = {}
    with candidate_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if float(row["primitive_coverage_fraction_for_this_sensor"]) < 0.95:
                continue
            fid = row["physicalid"]
            part_idx = int(row["part_index"])
            seg_idx = int(row["primitive_index"])
            direction = row["travel_direction_on_cscl"]
            key = (fid, part_idx, seg_idx, direction)
            if key in arcs_by_key:
                continue
            pts = geometry[fid][part_idx]
            a, b = pts[seg_idx], pts[seg_idx + 1]
            if direction == "TF":
                a, b = b, a
            arcs_by_key[key] = {"key": key, "start": a, "end": b, "length_m": distance(a, b), "sensor_sid": row["sensor_sid"]}

    arcs = list(arcs_by_key.values())
    endpoints = [arc["start"] for arc in arcs] + [arc["end"] for arc in arcs]
    half = len(arcs)
    results = {}
    for tolerance in (0.5, 2.0, 5.0, 10.0):
        dsu = DSU(len(endpoints))
        for i, a in enumerate(endpoints):
            for j in range(i + 1, len(endpoints)):
                if distance(a, endpoints[j]) <= tolerance:
                    dsu.union(i, j)
        roots = [dsu.find(i) for i in range(len(endpoints))]
        node_ids = {root: ix for ix, root in enumerate(sorted(set(roots)))}
        start_nodes = [node_ids[roots[i]] for i in range(half)]
        end_nodes = [node_ids[roots[i + half]] for i in range(half)]
        directed = defaultdict(set)
        reverse = defaultdict(set)
        weak = defaultdict(set)
        for u, v in zip(start_nodes, end_nodes):
            directed[u].add(v)
            reverse[v].add(u)
            weak[u].add(v)
            weak[v].add(u)
            directed.setdefault(v, set())
            reverse.setdefault(u, set())
            weak.setdefault(u, set())
            weak.setdefault(v, set())
        wcc = components(weak)
        scc = strongly_connected_components(directed, reverse)
        largest_wcc = max(wcc, key=len) if wcc else []
        largest_scc = max(scc, key=len) if scc else []
        largest_wcc_edges = sum(1 for u, v in zip(start_nodes, end_nodes) if u in largest_wcc and v in largest_wcc)
        results[str(tolerance)] = {
            "endpoint_snap_tolerance_m": tolerance,
            "nodes": len(node_ids),
            "fully_observed_directed_arcs": len(arcs),
            "weak_component_count": len(wcc),
            "largest_weak_component_nodes": len(largest_wcc),
            "largest_weak_component_arcs": largest_wcc_edges,
            "strongly_connected_component_count": len(scc),
            "largest_strong_component_nodes": len(largest_scc),
            "total_full_observed_arc_length_km": round(sum(arc["length_m"] for arc in arcs) / 1000, 3),
            "largest_weak_component_sensor_ids": sorted({arc["sensor_sid"] for arc, u, v in zip(arcs, start_nodes, end_nodes) if u in largest_wcc and v in largest_wcc}),
        }
    result = {
        "classification": "conservative graph connectivity diagnostic using only primitive arcs at least 95% covered by one sensor",
        "input": str(candidate_path.relative_to(ROOT)),
        "full_observed_directed_arc_count": len(arcs),
        "snapping_sensitivity": results,
        "limitations": [
            "Only fully covered primitive arcs are included; partial observed intervals are omitted.",
            "Endpoint snapping ignores grade separation and turn restrictions; components are an optimistic topology screening, not route validation.",
            "The graph is not connected to vehicle locations, taxi-zone centroids, or requested pickup points.",
            "No OD costs, C4 feasibility, or dispatch performance were computed.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
