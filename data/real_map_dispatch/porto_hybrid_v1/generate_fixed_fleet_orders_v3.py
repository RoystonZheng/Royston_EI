#!/usr/bin/env python3
"""Create v3 fixed orders whose endpoints lie on the routable SUMO core.

v2 files remain untouched. v3 keeps the same 70/15/15 order mix and candidate
heat cells, but draws fuzzy road-feature points only from locations that can be
matched to the largest strongly connected passenger-road component.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import networkx as nx
import sumolib


PACKAGE = Path(__file__).resolve().parent
V2_SCRIPT = PACKAGE / "generate_fixed_fleet_orders_v2.py"
PREFLIGHT_DIR = PACKAGE / "scripts"
NETWORK = PACKAGE / "derived/sumo/v1.28.0/porto.net.xml"
SCENARIOS = PACKAGE / "derived/scenarios/v3_1"
FUZZY_MAX_SNAP_M = 50.0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_v2_module():
    sys.path.insert(0, str(PACKAGE))
    import generate_fixed_fleet_orders_v2 as v2  # type: ignore
    return v2


def load_preflight_module():
    sys.path.insert(0, str(PREFLIGHT_DIR))
    import preflight_fixed_orders_sumo_v2 as pf  # type: ignore
    return pf


def core_matches(net: Any, point: dict[str, Any], core_edges: dict[str, Any]) -> list[dict[str, Any]]:
    xy = net.convertLonLat2XY(float(point["lon"]), float(point["lat"]))
    is_fuzzy = point.get("point_kind") == "fuzzy_road_feature_sample"
    radius = FUZZY_MAX_SNAP_M if is_fuzzy else 300.0
    found = []
    for edge, _ in net.getNeighboringEdges(*xy, r=radius, includeJunctions=False):
        if edge.getID() not in core_edges:
            continue
        lane_index, edge_pos, snap_distance = edge.getClosestLanePosDist(xy)
        if snap_distance <= radius:
            found.append({"edge_id": edge.getID(), "lane_index": lane_index, "edge_pos_m": edge_pos, "snap_distance_m": snap_distance})
    found.sort(key=lambda m: (m["snap_distance_m"], m["edge_id"], m["lane_index"]))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fleet-size", type=int, default=10)
    parser.add_argument("--orders-per-vehicle", type=int, default=10)
    parser.add_argument("--candidate-count", type=int, default=30)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--network", type=Path, default=NETWORK)
    args = parser.parse_args()

    v2 = load_v2_module()
    pf = load_preflight_module()
    net = sumolib.net.readNet(str(args.network), withInternal=False)
    full_graph, full_allowed = pf.build_route_graph(net)
    components = sorted(nx.strongly_connected_components(full_graph), key=len, reverse=True)
    core_ids = set(components[0])
    core_allowed = {edge_id: full_allowed[edge_id] for edge_id in core_ids}
    if len(core_ids) < 1000:
        raise RuntimeError(f"unexpectedly small routeable core: {len(core_ids)} edges")

    output = args.output or SCENARIOS / f"orders_seed_{args.seed}_draft.json"
    payload = v2.build(SimpleNamespace(
        seed=args.seed,
        fleet_size=args.fleet_size,
        orders_per_vehicle=args.orders_per_vehicle,
        candidate_count=args.candidate_count,
        osm=PACKAGE / "raw/osm/porto_candidate.osm",
        heat=PACKAGE / "derived/heat/porto_hotspot_cells_200m.csv",
        output=output,
    ))

    # Fail closed if any of the fixed heat-based candidates lacks a core-road snap.
    candidate_failures = [p["point_id"] for p in payload["candidate_hotspots"] if not core_matches(net, p, core_allowed)]
    if candidate_failures:
        raise RuntimeError(f"candidate heat cells cannot match the routeable core: {candidate_failures}")
    for vehicle in payload["vehicles"]:
        if not core_matches(net, vehicle["initial_position"], core_allowed):
            raise RuntimeError(f"vehicle initial position cannot match core: {vehicle['vehicle_id']}")

    # Sample only real OSM signal/shared-vertex features adjacent to a road in
    # the SCC, then retain the first seed-stable point whose core snap is valid.
    features = v2.parse_osm_features(PACKAGE / "raw/osm/porto_candidate.osm")
    core_way_ids = {pf.osm_way_id(edge_id) for edge_id in core_ids}
    eligible_features = []
    for feature in features:
        segments = [segment for segment in feature["adjacent_segments"] if segment["way_id"] in core_way_ids]
        if segments:
            eligible_features.append({**feature, "adjacent_segments": segments})
    if len(eligible_features) < 20:
        raise RuntimeError(f"only {len(eligible_features)} OSM features touch a core road")
    rng = random.Random(args.seed + 9_000_000)

    endpoint_roles = []
    for order in payload["orders"]:
        kind = order["order_kind"]
        if kind == "fuzzy_single_endpoint":
            endpoint_roles.append((order, "pickup" if order["pickup"]["point_kind"] == "fuzzy_road_feature_sample" else "dropoff"))
        elif kind == "fuzzy_both_endpoints":
            endpoint_roles.extend(((order, "pickup"), (order, "dropoff")))

    feature_audit = []
    for order, role in endpoint_roles:
        matched = None
        for attempt in range(100):
            feature = eligible_features[rng.randrange(len(eligible_features))]
            sample = v2.sample_fuzzy_point(rng, feature, f"sample_{order['order_id']}_{role}_{attempt}")
            if core_matches(net, sample, core_allowed):
                matched = (feature, sample)
                break
        if matched is None:
            raise RuntimeError(f"failed to sample a core-matched fuzzy point for {order['order_id']} {role}")
        feature, sample = matched
        point_id = f"fuzzy_{order['order_id']}_{role}"
        sample["point_id"] = point_id
        sample["validation_truth"]["map_match_status"] = "preflight_candidate_routeable_core"
        order[role] = sample
        feature_audit.append({
            "order_id": order["order_id"],
            "endpoint_role": role,
            "feature_osm_node_id": feature["node_id"],
            "feature_type": feature["feature_type"],
        })

    # Endpoint resampling changes point IDs; regenerate the sequential route
    # task table from the final fixed orders so no task references a stale v2 ID.
    order_by_vehicle: dict[str, list[dict[str, Any]]] = {}
    for order in payload["orders"]:
        order_by_vehicle.setdefault(order["vehicle_id"], []).append(order)
    route_legs = []
    for vehicle in payload["vehicles"]:
        vehicle_orders = sorted(order_by_vehicle[vehicle["vehicle_id"]], key=lambda row: row["sequence_index"])
        current_point_id = vehicle["initial_position"]["point_id"]
        for order in vehicle_orders:
            route_legs.append({
                "route_task_id": f"task_{order['order_id']}_deadhead",
                "vehicle_id": vehicle["vehicle_id"],
                "order_id": order["order_id"],
                "leg_role": "vehicle_to_pickup",
                "from_point_id": current_point_id,
                "to_point_id": order["pickup"]["point_id"],
                "traditional_reachability_status": "pending",
            })
            route_legs.append({
                "route_task_id": f"task_{order['order_id']}_passenger",
                "vehicle_id": vehicle["vehicle_id"],
                "order_id": order["order_id"],
                "leg_role": "pickup_to_dropoff",
                "from_point_id": order["pickup"]["point_id"],
                "to_point_id": order["dropoff"]["point_id"],
                "traditional_reachability_status": "pending",
            })
            current_point_id = order["dropoff"]["point_id"]
    payload["route_legs"] = route_legs

    # Rebuild the feature truth list from the replaced endpoints. The original
    # generator's feature_records are not copied to the public planner input.
    payload["scenario_id"] = f"porto_fixed_fleet_route_v3_1_seed_{args.seed}"
    payload["generator_version"] = "0.3.1"
    payload["status"] = "draft_routeable_core_preflight_pending"
    payload["source_hashes"]["sumo_network_sha256"] = sha256(args.network)
    payload["routeability_policy"] = {
        "network_edge_scope": "largest strongly connected passenger-road component fully inside the frozen Porto fence",
        "core_directed_edge_count": len(core_ids),
        "full_fenced_passenger_edge_count": full_graph.number_of_nodes(),
        "candidate_heat_points_preserved": True,
        "fuzzy_points_sampled_near_real_osm_signal_or_shared_road_vertices": True,
        "fuzzy_snap_limit_m": FUZZY_MAX_SNAP_M,
        "candidate_snap_limit_m": 300.0,
        "purpose": "exclude endpoint pairs that are structurally unreachable before comparing route methods; raw map roads remain preserved outside this experiment routing core",
    }
    payload["dataset_semantics"]["routeable_core"] = "only the largest strongly connected passenger-road component is used for this experiment; outside-core OSM roads remain in the base map and are not route links"
    payload["route_preflight"]["status"] = "pending"
    payload["route_preflight"]["unreachable_task_policy"] = "no silent endpoint replacement; any failure blocks freeze and is recorded for review"
    payload["fuzzy_endpoint_truth"] = {
        "visibility": "validator_only",
        "records": feature_audit,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "sha256": sha256(output),
        "candidate_hotspots": len(payload["candidate_hotspots"]),
        "fuzzy_endpoints": len(feature_audit),
        "eligible_core_features": len(eligible_features),
        "routeable_core_edges": len(core_ids),
        "fenced_passenger_edges": full_graph.number_of_nodes(),
        "candidate_snap_failures": candidate_failures,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
