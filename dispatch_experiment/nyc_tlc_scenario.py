"""Load an audited NYC TLC scenario with explicit zone-level cost proxies."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Dict, Tuple

from route_experiment.models import Edge, Node, RoadMap

from .models import Order, RouteLeg, Vehicle
from .route_oracle import GraphRouteOracle


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_zone_od_cost_map(cost_matrix_path: Path) -> Tuple[RoadMap, Dict[Tuple[int, int], dict]]:
    """Build a directed zone graph from training-period median trip costs.

    Each zone OD row is represented as a direct directed edge. This is a
    zone-level cost proxy, not a street network or an empty-pickup ETA model.
    """

    nodes_by_zone: Dict[int, Node] = {}
    edges = []
    costs: Dict[Tuple[int, int], dict] = {}
    with gzip.open(cost_matrix_path, "rt", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {
            "pickup_zone_id",
            "dropoff_zone_id",
            "median_trip_time_s",
            "median_trip_miles",
            "source_trip_count",
        }
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("zone OD cost matrix is missing required columns")
        for row in reader:
            origin = int(row["pickup_zone_id"])
            destination = int(row["dropoff_zone_id"])
            seconds = float(row["median_trip_time_s"])
            miles = float(row["median_trip_miles"])
            count = int(row["source_trip_count"])
            if seconds <= 0 or miles <= 0 or count <= 0:
                raise ValueError("zone OD cost rows must have positive time, distance, and count")
            nodes_by_zone.setdefault(
                origin,
                Node(
                    node_id=f"zone_{origin}",
                    x=origin,
                    y=0,
                    legal_pickup=True,
                    legal_dropoff=True,
                ),
            )
            nodes_by_zone.setdefault(
                destination,
                Node(
                    node_id=f"zone_{destination}",
                    x=destination,
                    y=0,
                    legal_pickup=True,
                    legal_dropoff=True,
                ),
            )
            distance_m = miles * 1609.344
            edges.append(
                Edge(
                    edge_id=f"od_{origin}_{destination}",
                    from_node=f"zone_{origin}",
                    to_node=f"zone_{destination}",
                    length_m=distance_m,
                    base_speed_mps=distance_m / seconds,
                    one_way=True,
                )
            )
            costs[(origin, destination)] = {
                "median_trip_time_s": seconds,
                "median_trip_miles": miles,
                "source_trip_count": count,
            }

    if not nodes_by_zone or not edges:
        raise ValueError("zone OD cost matrix is empty")
    zones = sorted(nodes_by_zone)
    max_zone = max(zones)
    road_map = RoadMap(
        map_id="nyc_tlc_zone_od_proxy_v1",
        version="training-period observed zone OD medians",
        size=max_zone + 1,
        nodes=[nodes_by_zone[zone] for zone in zones],
        edges=edges,
        boundary={"x_min": min(zones), "x_max": max_zone, "y_min": 0, "y_max": 0},
        road_state_version="static_zone_od_median_proxy_v1",
    )
    return road_map, costs


class NYCTLCZoneRouteOracle(GraphRouteOracle):
    """Use observed direct OD medians for rides and same-zone-only pickups.

    The TLC files contain completed occupied trips, not empty-vehicle ETAs.
    Therefore this bounded mechanism experiment permits zero-cost pickup legs
    only when the vehicle and pickup share a taxi zone. Cross-zone pickups are
    explicitly unreachable until an independent empty-pickup cost model exists.
    """

    def __init__(self, road_map: RoadMap, od_costs: Dict[Tuple[int, int], dict]):
        super().__init__(road_map)
        self._od_costs = od_costs
        self._direct_edges = {
            (edge.from_node, edge.to_node): edge for edge in road_map.edges
        }

    def pickup_route(self, source_node_id: str, target_node_id: str) -> RouteLeg:
        if source_node_id != target_node_id:
            return RouteLeg(reachable=False, reason="cross_zone_empty_pickup_cost_unavailable")
        if source_node_id not in self._nodes:
            return RouteLeg(reachable=False, reason="endpoint_missing")
        return RouteLeg(
            reachable=True,
            node_sequence=[source_node_id],
            edge_sequence=[],
        )

    def ride_route(self, source_node_id: str, target_node_id: str) -> RouteLeg:
        if source_node_id not in self._nodes or target_node_id not in self._nodes:
            return RouteLeg(reachable=False, reason="endpoint_missing")
        try:
            origin = int(source_node_id.removeprefix("zone_"))
            destination = int(target_node_id.removeprefix("zone_"))
        except ValueError:
            return RouteLeg(reachable=False, reason="invalid_zone_node_id")
        edge = self._direct_edges.get((source_node_id, target_node_id))
        if (origin, destination) not in self._od_costs or edge is None:
            return RouteLeg(reachable=False, reason="direct_training_period_od_cost_unavailable")
        return RouteLeg(
            reachable=True,
            distance_m=edge.length_m,
            duration_sec=edge.length_m / edge.base_speed_mps,
            node_sequence=[source_node_id, target_node_id],
            edge_sequence=[edge.edge_id],
        )


class NYCTLCZoneCentroidPickupOracle(NYCTLCZoneRouteOracle):
    """Use an explicitly synthetic centroid proxy for empty pickup legs.

    Loaded rides still use direct observed TLC OD medians. Pickup legs use
    polygon centroids, a fixed detour factor, a fixed speed, and a fixed
    intrazonal distance. These are scenario assumptions, not measured road
    ETAs or street routes.
    """

    def __init__(
        self,
        road_map: RoadMap,
        od_costs: Dict[Tuple[int, int], dict],
        zone_centroids: Dict[int, Tuple[float, float]],
        *,
        speed_mps: float = 5.0,
        detour_factor: float = 1.3,
        minimum_intrazonal_distance_m: float = 300.0,
    ):
        super().__init__(road_map, od_costs)
        if speed_mps <= 0 or detour_factor < 1 or minimum_intrazonal_distance_m <= 0:
            raise ValueError("pickup proxy parameters must be positive and detour_factor >= 1")
        self._zone_centroids = zone_centroids
        self._pickup_speed_mps = speed_mps
        self._detour_factor = detour_factor
        self._minimum_intrazonal_distance_m = minimum_intrazonal_distance_m

    @staticmethod
    def _zone_id(node_id: str) -> int:
        try:
            return int(node_id.removeprefix("zone_"))
        except ValueError as error:
            raise ValueError("invalid_zone_node_id:" + node_id) from error

    def pickup_route(self, source_node_id: str, target_node_id: str) -> RouteLeg:
        if source_node_id not in self._nodes or target_node_id not in self._nodes:
            return RouteLeg(reachable=False, reason="endpoint_missing")
        source_zone = self._zone_id(source_node_id)
        target_zone = self._zone_id(target_node_id)
        source = self._zone_centroids.get(source_zone)
        target = self._zone_centroids.get(target_zone)
        if source is None or target is None:
            return RouteLeg(reachable=False, reason="zone_centroid_missing")
        if source_zone == target_zone:
            distance_m = self._minimum_intrazonal_distance_m
        else:
            lon1, lat1 = map(math.radians, source)
            lon2, lat2 = map(math.radians, target)
            dlon = lon2 - lon1
            dlat = lat2 - lat1
            a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            straight_m = 2 * 6_371_000.0 * math.asin(math.sqrt(min(1.0, a)))
            distance_m = straight_m * self._detour_factor
        duration_sec = distance_m / self._pickup_speed_mps
        return RouteLeg(
            reachable=True,
            distance_m=distance_m,
            duration_sec=duration_sec,
            node_sequence=[source_node_id, target_node_id],
            edge_sequence=[],
            reason="synthetic_zone_centroid_pickup_proxy",
        )


def load_dispatch_scenario(
    scenario_dir: Path,
    cost_matrix_path: Path,
    *,
    fleet_id: str | None = None,
) -> dict:
    scenario_dir = scenario_dir.resolve()
    cost_matrix_path = cost_matrix_path.resolve()
    metadata_path = scenario_dir / "scenario.json"
    orders_path = scenario_dir / "orders.json"
    for path in (metadata_path, orders_path, cost_matrix_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    order_payload = json.loads(orders_path.read_text(encoding="utf-8"))
    if order_payload.get("scenario_id") != metadata.get("scenario_id"):
        raise ValueError("scenario ID differs between orders.json and scenario.json")
    order_rows = order_payload.get("orders")
    if not isinstance(order_rows, list) or len(order_rows) != metadata.get("request_trace_row_count"):
        raise ValueError("orders.json count differs from scenario.json")
    orders = [Order(**row) for row in order_rows]

    fleet_variants = metadata.get("fleet_variants") or []
    if fleet_id is None:
        if len(fleet_variants) != 1:
            raise ValueError("select fleet_id when a scenario contains multiple fleet variants")
        fleet = fleet_variants[0]
    else:
        matches = [variant for variant in fleet_variants if variant.get("fleet_id") == fleet_id]
        if len(matches) != 1:
            raise ValueError(f"unknown or duplicated fleet_id: {fleet_id}")
        fleet = matches[0]
    vehicles = [Vehicle(**row) for row in fleet["vehicles"]]
    if len(vehicles) != fleet.get("vehicle_count"):
        raise ValueError("fleet vehicle_count differs from vehicle records")

    road_map, od_costs = load_zone_od_cost_map(cost_matrix_path)
    node_ids = {node.node_id for node in road_map.nodes}
    required_zone_ids = {
        node_id
        for order in orders
        for node_id in (order.pickup_node_id, order.dropoff_node_id)
    }
    required_zone_ids.update(vehicle.position_node_id for vehicle in vehicles)
    missing = sorted(required_zone_ids - node_ids)
    if missing:
        raise ValueError(f"zone cost map has no node for scenario endpoints: {missing[:10]}")

    required_direct_od = {
        (int(order.pickup_node_id.removeprefix("zone_")), int(order.dropoff_node_id.removeprefix("zone_")))
        for order in orders
    }
    missing_od = sorted(pair for pair in required_direct_od if pair not in od_costs)
    if missing_od:
        raise ValueError(f"training-period OD matrix has no direct cost for scenario orders: {missing_od[:10]}")

    return {
        "scenario": metadata,
        "orders": orders,
        "vehicles": vehicles,
        "fleet": fleet,
        "road_map": road_map,
        "od_costs": od_costs,
        "scenario_dir": scenario_dir,
        "scenario_metadata_sha256": sha256_file(metadata_path),
        "orders_json_sha256": sha256_file(orders_path),
        "cost_matrix_path": cost_matrix_path,
        "cost_matrix_sha256": sha256_file(cost_matrix_path),
        "required_direct_od": required_direct_od,
    }
