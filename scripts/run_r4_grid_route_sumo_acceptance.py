#!/usr/bin/env python3
"""Replay a completed R4 grid route through a SUMO network projected from its map.

This is a zero-model-call safety acceptance. The road graph and route are copied
from frozen JSON artifacts; every directed link is preserved as a directed SUMO
edge, and the completed vehicle route is checked against the network output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def require_binary(value: str | None, name: str) -> str:
    found = value or shutil.which(name)
    if not found or not Path(found).is_file():
        raise RuntimeError(f"{name} binary not found; pass --{name.replace('_', '-')}-bin")
    return str(Path(found).resolve())


def run_logged(argv: list[str], log_path: Path) -> None:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    log_path.write_text(
        "COMMAND: " + json.dumps(argv, ensure_ascii=False) + "\n"
        + f"EXIT_CODE: {result.returncode}\n"
        + "--- STDOUT ---\n" + result.stdout
        + "\n--- STDERR ---\n" + result.stderr,
        encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}); see {log_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", required=True, type=Path)
    parser.add_argument("--pilot-audit", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--sumo-bin")
    parser.add_argument("--netconvert-bin")
    args = parser.parse_args()

    map_path = args.map.resolve()
    audit_path = args.pilot_audit.resolve()
    project_root = args.project_root.resolve()
    out = args.output_dir.resolve()
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    sumo = require_binary(args.sumo_bin or os.environ.get("SUMO_BIN"), "sumo")
    netconvert = require_binary(args.netconvert_bin or os.environ.get("NETCONVERT_BIN"), "netconvert")

    road_map = json.loads(map_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if road_map.get("map_id") != audit.get("map_id"):
        raise RuntimeError("frozen map ID does not match the completed pilot audit")
    preflight_path = Path(audit["source_preflight"])
    if not preflight_path.is_absolute():
        preflight_path = project_root / preflight_path
    preflight_path = preflight_path.resolve()
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    expected_map_sha = (preflight.get("map_ref") or {}).get("sha256")
    if not expected_map_sha or expected_map_sha != sha256(map_path):
        raise RuntimeError("map does not match the completed pilot's frozen preflight SHA")
    nodes = {node["node_id"]: node for node in road_map["nodes"]}
    edges = {edge["edge_id"]: edge for edge in road_map["edges"]}
    route_nodes = list(audit["accepted_route_nodes"])
    route_edges = list(audit["accepted_route_edges"])
    if len(route_nodes) != len(route_edges) + 1:
        raise RuntimeError("pilot route node/edge count mismatch")
    if route_nodes[0] != audit["start_node"] or route_nodes[-1] != audit["dropoff_node"]:
        raise RuntimeError("pilot route endpoints do not match its audit")

    edge_errors: list[dict[str, Any]] = []
    for i, edge_id in enumerate(route_edges):
        edge = edges.get(edge_id)
        if edge is None:
            edge_errors.append({"step": i + 1, "edge_id": edge_id, "reason": "edge_missing"})
        elif edge.get("from_node") != route_nodes[i] or edge.get("to_node") != route_nodes[i + 1]:
            edge_errors.append({
                "step": i + 1,
                "edge_id": edge_id,
                "reason": "directed_endpoint_mismatch",
                "route_edge": [route_nodes[i], route_nodes[i + 1]],
                "map_edge": [edge.get("from_node"), edge.get("to_node")],
            })
    if edge_errors:
        raise RuntimeError("pilot route failed directed-edge audit")

    reverse_probe = next((
        edge for edge in road_map["edges"]
        if edge.get("one_way")
        and (edge.get("to_node"), edge.get("from_node")) not in {
            (item.get("from_node"), item.get("to_node")) for item in road_map["edges"]
        }
    ), None)
    if reverse_probe is None:
        raise RuntimeError("frozen map has no one-way reverse probe")

    nodes_xml = out / "grid.nod.xml"
    edges_xml = out / "grid.edg.xml"
    net_xml = out / "grid.net.xml"
    routes_xml = out / "pilot_route.rou.xml"
    trips_xml = out / "tripinfo.xml"
    vehroute_xml = out / "vehroute.xml"
    summary_xml = out / "sumo_summary.xml"
    write_xml(nodes_xml, ET.Element("nodes"))
    # Populate node and edge files explicitly after root construction.
    root_nodes = ET.Element("nodes")
    for node in road_map["nodes"]:
        ET.SubElement(root_nodes, "node", {
            "id": node["node_id"],
            "x": str(float(node["x"]) * 100.0),
            "y": str(float(node["y"]) * 100.0),
            "type": "priority",
        })
    write_xml(nodes_xml, root_nodes)

    root_edges = ET.Element("edges")
    for edge in road_map["edges"]:
        attrs = {
            "id": edge["edge_id"],
            "from": edge["from_node"],
            "to": edge["to_node"],
            "numLanes": "1",
            "speed": str(max(0.1, float(edge.get("base_speed_mps", 8.0)))),
            "priority": "1",
        }
        ET.SubElement(root_edges, "edge", attrs)
    write_xml(edges_xml, root_edges)

    run_logged([
        netconvert,
        "--node-files", str(nodes_xml),
        "--edge-files", str(edges_xml),
        "--output-file", str(net_xml),
        "--no-turnarounds", "true",
    ], out / "netconvert.log")

    net_root = ET.parse(net_xml).getroot()
    network_edges = {
        item.get("id"): {
            "from": item.get("from"),
            "to": item.get("to"),
            "lane_length": float(item.find("lane").get("length")) if item.find("lane") is not None else None,
        }
        for item in net_root.findall("edge")
        if item.get("function") != "internal"
    }
    missing_network_edges = sorted(set(edges) - set(network_edges))
    if missing_network_edges:
        raise RuntimeError(f"SUMO network omitted {len(missing_network_edges)} frozen directed edges")
    unexpected_reverse_edges = []
    for edge in road_map["edges"]:
        if edge.get("one_way"):
            reverse_id = f"edge_{edge['to_node']}_{edge['from_node']}"
            if reverse_id in network_edges:
                unexpected_reverse_edges.append(reverse_id)
    if unexpected_reverse_edges:
        raise RuntimeError("SUMO network contains forbidden reverse one-way edges")

    route_root = ET.Element("routes")
    ET.SubElement(route_root, "vType", {
        "id": "r4_ev", "accel": "2.6", "decel": "4.5", "sigma": "0",
        "length": "4.5", "maxSpeed": "20", "speedFactor": "1",
    })
    ET.SubElement(route_root, "route", {"id": "r4_pilot_route", "edges": " ".join(route_edges)})
    vehicle = ET.SubElement(route_root, "vehicle", {
        "id": "r4_pilot_vehicle", "type": "r4_ev", "route": "r4_pilot_route",
        "depart": "0", "departLane": "best", "departSpeed": "max",
    })
    pickup_index = route_nodes.index(audit["pickup_node"])
    if pickup_index == 0:
        raise RuntimeError("pickup must follow at least one routed edge")
    pickup_in_edge = edges[route_edges[pickup_index - 1]]
    pickup_lane_length = network_edges[route_edges[pickup_index - 1]]["lane_length"]
    if pickup_lane_length is None or pickup_lane_length <= 2:
        raise RuntimeError("SUMO pickup edge has no usable lane length")
    pickup_pos = min(float(pickup_in_edge.get("length_m", 100.0)) - 1.0, pickup_lane_length - 1.0)
    dropoff_in_edge = edges[route_edges[-1]]
    dropoff_lane_length = network_edges[route_edges[-1]]["lane_length"]
    if dropoff_lane_length is None or dropoff_lane_length <= 2:
        raise RuntimeError("SUMO dropoff edge has no usable lane length")
    dropoff_pos = min(float(dropoff_in_edge.get("length_m", 100.0)) - 1.0, dropoff_lane_length - 1.0)
    ET.SubElement(vehicle, "stop", {
        "edge": pickup_in_edge["edge_id"], "endPos": str(pickup_pos), "duration": "10",
    })
    ET.SubElement(vehicle, "stop", {
        "edge": dropoff_in_edge["edge_id"], "endPos": str(dropoff_pos), "duration": "10",
    })
    write_xml(routes_xml, route_root)

    run_logged([
        sumo,
        "--net-file", str(net_xml),
        "--route-files", str(routes_xml),
        "--tripinfo-output", str(trips_xml),
        "--vehroute-output", str(vehroute_xml),
        "--summary-output", str(summary_xml),
        "--step-length", "0.5",
        "--end", "5000",
        "--time-to-teleport", "-1",
        "--no-step-log", "true",
        "--duration-log.disable", "true",
        "--seed", "0",
    ], out / "sumo.log")

    trip_root = ET.parse(trips_xml).getroot()
    trip = next((item for item in trip_root.findall("tripinfo") if item.get("id") == "r4_pilot_vehicle"), None)
    if trip is None:
        raise RuntimeError("SUMO did not produce a terminal tripinfo record")
    vehicle_root = ET.parse(vehroute_xml).getroot()
    veh = next((item for item in vehicle_root.findall("vehicle") if item.get("id") == "r4_pilot_vehicle"), None)
    route_out = veh.find("route") if veh is not None else None
    travelled = (route_out.get("edges") or "").split() if route_out is not None else []
    if travelled != route_edges:
        raise RuntimeError("SUMO vehicle route differs from the frozen R4 directed-edge sequence")

    copied_map = out / "frozen_map.json"
    copied_audit = out / "pilot_final_audit.json"
    copied_preflight = out / "pilot_preflight.json"
    shutil.copy2(map_path, copied_map)
    shutil.copy2(audit_path, copied_audit)
    shutil.copy2(preflight_path, copied_preflight)
    source_files = [
        Path(__file__).resolve(),
        Path("route_experiment/r4_stepwise.py").resolve(),
        Path("scripts/run_r4_codex_pilot_continuation.py").resolve(),
        Path("tests/test_r4_codex_pilot_acceptance.py").resolve(),
    ]
    manifest = {
        "schema_version": "r4_grid_route_sumo_acceptance_v1",
        "status": "passed",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "zero-model-call SUMO replay of one archived, completed 100x100 grid R4 pilot route; not a new model experiment",
        "map_id": road_map["map_id"],
        "pilot_task_id": audit["task_id"],
        "pilot_status": audit["status"],
        "model_calls_during_acceptance": 0,
        "map": {"path": str(map_path), "sha256": sha256(map_path), "node_count": len(nodes), "directed_edge_count": len(edges), "matched_preflight_sha256": expected_map_sha},
        "preflight": {"path": str(preflight_path), "sha256": sha256(preflight_path)},
        "pilot_audit": {"path": str(audit_path), "sha256": sha256(audit_path), "route_edge_count": len(route_edges), "route_node_count": len(route_nodes), "route_edge_sequence_sha256": hashlib.sha256(json.dumps(route_edges, separators=(",", ":")).encode()).hexdigest(), "start_node": route_nodes[0], "pickup_node": audit["pickup_node"], "dropoff_node": route_nodes[-1]},
        "directed_validation": {"status": "passed", "edge_errors": edge_errors, "one_way_edges_on_route": sum(bool(edges[eid].get("one_way")) for eid in route_edges), "one_way_violations": 0, "reverse_probe": {"edge_id": reverse_probe["edge_id"], "from_node": reverse_probe["from_node"], "to_node": reverse_probe["to_node"], "reverse_edge_exists": False, "acceptance_test": "tests/test_r4_codex_pilot_acceptance.py::R4CodexPilotAcceptanceTests.test_formal_online_step_path_rejects_oneway_reverse_backtracks_once_and_reroutes"}},
        "sumo": {"sumo_binary": sumo, "sumo_version": subprocess.run([sumo, "--version"], capture_output=True, text=True, check=True).stdout.splitlines()[0], "netconvert_binary": netconvert, "net_sha256": sha256(net_xml), "route_file_sha256": sha256(routes_xml), "tripinfo_sha256": sha256(trips_xml), "vehroute_sha256": sha256(vehroute_xml), "vehicle_completed": True, "route_edge_sequence_matched": travelled == route_edges, "simulated_route_length_m": float(trip.get("routeLength", "nan")), "duration_s": float(trip.get("duration", "nan")), "waiting_time_s": float(trip.get("waitingTime", "nan")), "pickup_stop_count": 1, "dropoff_stop_count": 1, "pickup_stop_end_position_m": pickup_pos, "dropoff_stop_end_position_m": dropoff_pos},
        "source_sha256": {str(path): sha256(path) for path in source_files if path.is_file()},
        "artifacts": {name: sha256(out / name) for name in ("grid.nod.xml", "grid.edg.xml", "grid.net.xml", "pilot_route.rou.xml", "netconvert.log", "sumo.log", "tripinfo.xml", "vehroute.xml", "sumo_summary.xml", "frozen_map.json", "pilot_final_audit.json", "pilot_preflight.json")},
        "limitations": ["SUMO replay validates an archived completed pilot route against a network projected from the same synthetic directed map.", "The model's online step path is covered by the zero-call mocked-client acceptance test; no new model request was issued.", "SUMO replay is a gate check, not an additional route-performance sample."],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
