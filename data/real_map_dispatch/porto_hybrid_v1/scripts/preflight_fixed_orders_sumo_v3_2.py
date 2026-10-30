#!/usr/bin/env python3
"""Strict v3.2 preflight including connection-level passenger permissions."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import networkx as nx
import sumolib

import preflight_fixed_orders_sumo_v2 as pf
from strict_passenger_graph import build_passenger_graph


PACKAGE = Path(__file__).resolve().parents[1]
NETWORK = PACKAGE / "derived/sumo/v1.28.0/porto.net.xml"
SOURCE_SCENARIOS = PACKAGE / "derived/scenarios/v3_1"
DEFAULT_OUTPUT = PACKAGE / "derived/scenarios/v3_2/preflight_strict_v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--all-seeds", action="store_true")
    parser.add_argument("--source-dir", type=Path, default=SOURCE_SCENARIOS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--network", type=Path, default=NETWORK)
    args = parser.parse_args()
    seeds = list(range(10)) if args.all_seeds else (args.seed or [0])

    pf.SCENARIOS = args.source_dir
    net = sumolib.net.readNet(str(args.network), withInternal=False)
    full_graph, full_allowed, turns = build_passenger_graph(net, args.network, pf)
    components = sorted(nx.strongly_connected_components(full_graph), key=len, reverse=True)
    if not components:
        raise RuntimeError("strict passenger route graph is empty")
    core = set(components[0])
    graph = full_graph.subgraph(core).copy()
    allowed = {edge_id: full_allowed[edge_id] for edge_id in core}
    summaries = [pf.run_seed(seed, net, graph, allowed, args.output_dir) for seed in seeds]
    result = {
        "network": str(args.network),
        "network_sha256": sha256(args.network),
        "network_edge_count": len(net.getEdges()),
        "fenced_passenger_edge_count_before_core": full_graph.number_of_nodes(),
        "fenced_passenger_connection_count_before_core": full_graph.number_of_edges(),
        "routeable_core_edge_count": graph.number_of_nodes(),
        "routeable_core_connection_count": graph.number_of_edges(),
        "connection_permissions": "only SUMO XML connections permitted to vClass=passenger are routeable",
        "passenger_connection_count_in_network": sum(1 for (a, b), rows in turns.items() for _ in rows),
        "custom_via_way_restrictions": [list(x) for x in pf.FORBIDDEN_WAY_SEQUENCES],
        "seeds": summaries,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.output_dir / "summary.json"
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(row["status"] == "passed" for row in summaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
