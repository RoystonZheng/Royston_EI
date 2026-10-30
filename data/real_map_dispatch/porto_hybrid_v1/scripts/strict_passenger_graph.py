#!/usr/bin/env python3
"""Build an edge graph that respects SUMO connection-level vClass permissions."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

import networkx as nx


def connection_allows(connection: ET.Element, vclass: str = "passenger") -> bool:
    allow = set(connection.get("allow", "").split())
    disallow = set(connection.get("disallow", "").split())
    if allow:
        return vclass in allow or "all" in allow
    return vclass not in disallow


def lane_allows(lane: ET.Element | None, vclass: str = "passenger") -> bool:
    if lane is None:
        return False
    allow = set(lane.get("allow", "").split())
    disallow = set(lane.get("disallow", "").split())
    if allow:
        return vclass in allow or "all" in allow
    return vclass not in disallow


def build_passenger_graph(net: Any, network_path: Path, pf: Any) -> tuple[nx.DiGraph, dict[str, Any], dict[tuple[str, str], list[dict[str, Any]]]]:
    """Return passenger-allowed edges and only movement connections legal to cars.

    `sumolib.Edge.getOutgoing()` reports edge adjacency but does not expose the
    `<connection allow/disallow>` vClass rule. Parse the frozen net XML for that
    rule so bus-only or other restricted movements cannot enter the route graph.
    """
    allowed = {
        edge.getID(): edge
        for edge in net.getEdges()
        if edge.allows("passenger") and pf.edge_is_inside_fence(net, edge)
    }
    graph = nx.DiGraph()
    for edge_id, edge in allowed.items():
        graph.add_node(edge_id, length_m=edge.getLength(), speed_mps=max(0.1, edge.getSpeed()))

    turns: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    root = ET.parse(network_path).getroot()
    lanes_by_id = {
        lane.get("id", ""): lane
        for edge in root.findall("edge")
        for lane in edge.findall("lane")
        if lane.get("id")
    }
    for connection in root.findall("connection"):
        source, target = connection.get("from"), connection.get("to")
        if source not in allowed or target not in allowed or not connection_allows(connection):
            continue
        via_lane_id = connection.get("via")
        if via_lane_id and not lane_allows(lanes_by_id.get(via_lane_id)):
            # SUMO serializes some movement restrictions on the internal via
            # lane instead of the external from/to connection.
            continue
        graph.add_edge(source, target, weight=allowed[target].getLength() / max(0.1, allowed[target].getSpeed()))
        turn = {
            "tls_id": connection.get("tl"),
            "link_index": int(connection.get("linkIndex", "-1")),
            "from_lane": int(connection.get("fromLane", "0")),
            "to_lane": int(connection.get("toLane", "0")),
            "allow": connection.get("allow"),
            "disallow": connection.get("disallow"),
        }
        turns[(source, target)].append(turn)
    return graph, allowed, turns
