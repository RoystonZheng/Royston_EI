#!/usr/bin/env python3
"""Zero-request profile of endpoint-centered Porto map crops.

For each frozen route leg this measures how much of the common directed graph
is needed for a legal static path to remain reachable. It stores only counts
and hashes, never raw map identifiers or candidate paths, and makes no model
calls. This is a design feasibility profile, not an experiment result.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import statistics
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
SUMO_HOME = Path("/Users/didi/Library/Application Support/Porto SUMO/1.28.0/Versions/1.28.0/EclipseSUMO")
sys.path.insert(0, str(SUMO_HOME / "share/sumo/tools"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(PACKAGE / "scripts"))

import run_porto_r1_dynamic_sumo_v3 as r1  # noqa: E402

FROZEN = PACKAGE / "derived/scenarios/v3_3/frozen_inputs_v1_3"
OUT = PACKAGE / "derived/experiments/r2_context_profile_v1"
MARGINS_M = (250, 500, 750, 1000, 1500, 2000, 3000)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_core_scope():
    manifest = json.loads((FROZEN / "manifest_v3_3.json").read_text(encoding="utf-8"))
    core_file = FROZEN / "routeable_core_edges.csv"
    expected = manifest["core_edge_file"]
    if digest(core_file) != expected["sha256"]:
        raise RuntimeError("frozen_core_edge_file_hash_mismatch")
    with core_file.open(encoding="utf-8-sig", newline="") as stream:
        edge_ids = {row["edge_id"] for row in csv.DictReader(stream)}
    if len(edge_ids) != expected["rows"]:
        raise RuntimeError("frozen_core_edge_row_count_mismatch")
    r1.pf.edge_is_inside_fence = lambda _net, edge: edge.getID() in edge_ids
    return edge_ids, expected["sha256"]


def bbox_for(shape):
    xs = [float(point[0]) for point in shape]
    ys = [float(point[1]) for point in shape]
    return min(xs), min(ys), max(xs), max(ys)


def overlaps(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def build_compact_payload(runner, edge_ids, edge_alias, signal_alias, tls_by_turn, start, end):
    links = []
    for edge_id in sorted(edge_ids):
        edge = runner.allowed[edge_id]
        links.append([
            edge_alias[edge_id], round(float(edge.getLength()), 2),
            round(max(0.1, float(edge.getSpeed())), 3),
        ])
    turns = []
    for source, target in sorted(tls_by_turn):
        refs = [[signal_alias[sig], int(index)] for sig, index in tls_by_turn[(source, target)] if sig in signal_alias]
        turns.append([edge_alias[source], edge_alias[target], refs])
    signal_rows = []
    for signal_id, alias in sorted(signal_alias.items(), key=lambda item: item[1]):
        phases = runner.programs.get(signal_id, [])
        signal_rows.append([
            alias,
            [[round(float(phase["duration"]), 3), [i for i, c in enumerate(phase["state"]) if c in "gG"]] for phase in phases],
        ])
    return {
        "legs": [
            {"from": {"edge": edge_alias[start["edge_id"]], "pos_m": round(float(start["edge_pos_m"]), 2)},
             "to": {"edge": edge_alias[end["edge_id"]], "pos_m": round(float(end["edge_pos_m"]), 2)}}
        ],
        "links": links,
        "turns": turns,
        "signals": signal_rows,
    }


def legal_route_in_subgraph(runner, edge_ids, start, end):
    graph = runner.graph.subgraph(edge_ids).copy()
    if start["edge_id"] not in graph or end["edge_id"] not in graph:
        return False
    allowed = {key: runner.allowed[key] for key in graph.nodes}
    legal_turns = {
        key: value for key, value in runner.tls_by_turn.items()
        if key[0] in graph and key[1] in graph
    }
    router = r1.DynamicRouter(graph, allowed, legal_turns, runner.programs)
    traffic = {"sim_time_s": 0.0, "edge_state": {}, "active_vehicle_count": 0,
               "occupied_edge_count": 0, "halting_vehicle_count": 0}
    route = router.route(start, end, "FASTEST", 0.0, traffic, {})
    return route["status"] == "ok" and r1.pf.is_legal_sequence(route["path"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    output = args.output_dir
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing output directory: {output}")
    output.mkdir(parents=True)
    core_ids, core_sha = install_core_scope()
    rows = []
    seed_hashes = {}

    for seed in range(10):
        runner_dir = output / f"runtime_seed_{seed}"
        runner = r1.SimulationRun(seed, "FASTEST", runner_dir, 90, 2.0, {}, "offline_context_profile")
        if runner.core != core_ids:
            raise RuntimeError("sumo_graph_does_not_match_frozen_core_edge_file")
        orders_path = FROZEN / f"orders_seed_{seed}_frozen.json"
        binding_path = FROZEN / f"shared_endpoint_bindings_seed_{seed}.json"
        seed_hashes[str(seed)] = {"orders_sha256": digest(orders_path), "bindings_sha256": digest(binding_path)}
        bounds = {edge_id: bbox_for(runner.allowed[edge_id].getShape()) for edge_id in core_ids}
        endpoints = runner.match
        for leg in runner.orders_file["route_legs"]:
            start = endpoints[leg["from_point_id"]][0]
            end = endpoints[leg["to_point_id"]][0]
            start_bounds = bounds[start["edge_id"]]
            end_bounds = bounds[end["edge_id"]]
            base = (min(start_bounds[0], end_bounds[0]), min(start_bounds[1], end_bounds[1]),
                    max(start_bounds[2], end_bounds[2]), max(start_bounds[3], end_bounds[3]))
            chosen = None
            for margin in MARGINS_M:
                crop = (base[0] - margin, base[1] - margin, base[2] + margin, base[3] + margin)
                selected = {edge_id for edge_id, box in bounds.items() if overlaps(box, crop)}
                selected.add(start["edge_id"])
                selected.add(end["edge_id"])
                if legal_route_in_subgraph(runner, selected, start, end):
                    chosen = (margin, selected)
                    break
            if chosen is None:
                chosen = ("full_graph", set(core_ids))
            margin, selected = chosen
            local_alias = {edge_id: f"e{idx:04d}" for idx, edge_id in enumerate(sorted(selected))}
            relevant_turns = {
                key: values for key, values in runner.tls_by_turn.items()
                if key[0] in selected and key[1] in selected
            }
            signal_ids = sorted({sig for values in relevant_turns.values() for sig, _ in values if sig in runner.programs})
            local_signal_alias = {sig: f"s{idx:04d}" for idx, sig in enumerate(signal_ids)}
            compact = build_compact_payload(runner, selected, local_alias, local_signal_alias, relevant_turns, start, end)
            compact_bytes = len(json.dumps(compact, separators=(",", ":")).encode("utf-8"))
            rows.append({
                "seed": seed,
                "route_task_id": leg["route_task_id"],
                "leg_role": leg["leg_role"],
                "crop_margin_m": margin,
                "directed_link_count": len(selected),
                "legal_turn_count": len(relevant_turns),
                "signal_program_count": len(local_signal_alias),
                "compact_graph_json_bytes": compact_bytes,
                "rough_tokens_bytes_div_3": math.ceil(compact_bytes / 3.0),
                "static_legal_reachability": "passed",
            })
        print(json.dumps({"seed": seed, "route_legs_profiled": len(runner.orders_file["route_legs"]), "total": len(rows)}))

    margin_values = [row["crop_margin_m"] for row in rows if isinstance(row["crop_margin_m"], int)]
    counts = [row["directed_link_count"] for row in rows]
    sizes = [row["compact_graph_json_bytes"] for row in rows]
    summary = {
        "profile_id": "porto_r2_endpoint_crop_static_context_profile_v1",
        "status": "completed_zero_model_calls",
        "formal_experiment": False,
        "method": "endpoint-edge bounding rectangle expanded by the smallest listed margin that preserves a legal FASTEST static path; common graph and hard route validator",
        "margins_m": list(MARGINS_M),
        "route_legs_profiled": len(rows),
        "static_legal_reachability_count": sum(row["static_legal_reachability"] == "passed" for row in rows),
        "full_core_link_count": len(core_ids),
        "frozen_core_edge_sha256": core_sha,
        "profile_script_sha256": digest(Path(__file__).resolve()),
        "margin_usage_counts": {str(margin): sum(row["crop_margin_m"] == margin for row in rows) for margin in MARGINS_M},
        "full_graph_fallback_count": sum(row["crop_margin_m"] == "full_graph" for row in rows),
        "link_count_median": statistics.median(counts),
        "link_count_mean": statistics.mean(counts),
        "link_count_p95": sorted(counts)[math.ceil(0.95 * len(counts)) - 1],
        "link_count_max": max(counts),
        "compact_graph_bytes_median": statistics.median(sizes),
        "compact_graph_bytes_mean": statistics.mean(sizes),
        "compact_graph_bytes_p95": sorted(sizes)[math.ceil(0.95 * len(sizes)) - 1],
        "compact_graph_bytes_max": max(sizes),
        "input_order_and_binding_hashes": seed_hashes,
        "limitations": [
            "This profiles static reachability and input size only; it does not compare methods or run SUMO.",
            "The smallest feasible crop is selected using a traditional legality/reachability check; this map-query policy must be frozen and shared identically by all methods before any matched comparison.",
            "The byte-divided-by-three token estimate remains rough and must not be used as a billing or Codex usage limit.",
        ],
    }
    with (output / "route_context_profile.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output / "profile_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
