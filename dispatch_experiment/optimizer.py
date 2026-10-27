"""Deterministic global assignment and output validation."""

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Assignment, Candidate, Order, Vehicle


@dataclass
class _FlowEdge:
    to: int
    reverse: int
    capacity: int
    cost: int
    candidate_id: str = ""


class DeterministicGlobalOptimizer:
    """Minimum-cost bipartite flow with an exact, bounded lexicographic cost.

    The integer objective is ordered as: maximize served orders, minimize total
    pickup wait, minimize total empty distance, then minimize EV energy. The
    dynamic scales are derived from the current feasible candidate set, so no
    fixed penalty can let a lower-priority metric overturn a higher-priority
    one.
    """

    OBJECTIVE_VERSION = "global_min_cost_flow_lexicographic_v2"
    WAIT_UNITS_PER_SECOND = 1_000
    DISTANCE_UNITS_PER_METER = 1_000
    ENERGY_UNITS_PER_KWH = 1_000_000

    def __init__(self, method_name: str = OBJECTIVE_VERSION):
        self.method_name = method_name
        self.last_objective_metadata: Dict[str, object] = {
            "version": self.OBJECTIVE_VERSION,
            "status": "not_run",
        }

    @classmethod
    def _quantized_components(cls, candidate: Candidate) -> Tuple[int, int, int]:
        wait_s = float(candidate.predicted_wait_sec)
        distance_m = float(candidate.empty_distance_m)
        energy_kwh = float(candidate.energy_kwh)
        if not all(map(math.isfinite, (wait_s, distance_m, energy_kwh))):
            raise ValueError("optimizer_candidate_cost_must_be_finite")
        if wait_s < 0.0 or distance_m < 0.0:
            raise ValueError("optimizer_wait_and_distance_must_be_non_negative")
        return (
            int(round(wait_s * cls.WAIT_UNITS_PER_SECOND)),
            int(round(distance_m * cls.DISTANCE_UNITS_PER_METER)),
            int(round(energy_kwh * cls.ENERGY_UNITS_PER_KWH)),
        )

    def optimize(
        self,
        candidates: Sequence[Candidate],
        vehicles: Sequence[Vehicle],
        orders: Sequence[Order],
    ) -> Tuple[List[Candidate], str]:
        valid = [candidate for candidate in candidates if candidate.hard_constraint_ok]
        eligible_vehicle_ids = sorted({candidate.vehicle_id for candidate in valid})
        if not eligible_vehicle_ids:
            # Do not carry a previous round's integer scales into this audit.
            # No min-cost-flow problem was constructed for this round.
            self.last_objective_metadata = {
                "version": self.OBJECTIVE_VERSION,
                "status": "no_feasible_candidates",
                "priority_order": [
                    "maximize_served_orders",
                    "minimize_total_pickup_wait",
                    "minimize_total_empty_distance",
                    "minimize_total_ev_energy",
                ],
                "eligible_candidate_count": 0,
                "eligible_vehicle_count": 0,
                "max_assignments_for_bounds": 0,
            }
            return [], "infeasible"
        order_ids = sorted({candidate.order_id for candidate in valid})
        flow_target = len(eligible_vehicle_ids)
        max_assignments = min(flow_target, len(order_ids))
        quantized = {
            candidate.candidate_id: self._quantized_components(candidate)
            for candidate in valid
        }
        min_energy = min(value[2] for value in quantized.values())
        max_energy = max(value[2] for value in quantized.values())
        energy_span = max_energy - min_energy
        max_wait = max(value[0] for value in quantized.values())
        max_distance = max(value[1] for value in quantized.values())

        # A one-unit improvement in distance must exceed every possible
        # aggregate energy change. A one-unit improvement in wait must exceed
        # every possible aggregate distance and energy change.
        distance_weight = max_assignments * energy_span + 1
        wait_weight = (
            max_assignments * max_distance * distance_weight
            + max_assignments * energy_span
            + 1
        )
        max_candidate_cost = (
            max_wait * wait_weight
            + max_distance * distance_weight
            + energy_span
        )
        unserved_penalty = max_assignments * max_candidate_cost + 1
        self.last_objective_metadata = {
            "version": self.OBJECTIVE_VERSION,
            "status": "ready",
            "priority_order": [
                "maximize_served_orders",
                "minimize_total_pickup_wait",
                "minimize_total_empty_distance",
                "minimize_total_ev_energy",
            ],
            "quantization": {
                "wait": "1 millisecond",
                "empty_distance": "1 millimeter",
                "energy": "0.000001 kWh (0.001 Wh)",
            },
            "distance_weight": distance_weight,
            "wait_weight": wait_weight,
            "energy_offset_units": -min_energy,
            "unserved_vehicle_penalty": unserved_penalty,
            "max_assignments_for_bounds": max_assignments,
            "tie_break": "candidate edges inserted in ascending candidate_id; strict distance improvements only",
        }

        def candidate_cost(candidate: Candidate) -> int:
            wait_units, distance_units, energy_units = quantized[candidate.candidate_id]
            return (
                wait_units * wait_weight
                + distance_units * distance_weight
                + energy_units
                - min_energy
            )

        source = 0
        vehicle_start = 1
        order_start = vehicle_start + len(eligible_vehicle_ids)
        sink = order_start + len(order_ids)
        node_count = sink + 1
        graph: List[List[_FlowEdge]] = [[] for _ in range(node_count)]

        def add_edge(
            from_node: int,
            to_node: int,
            capacity: int,
            cost: int,
            candidate_id: str = "",
        ) -> int:
            forward_index = len(graph[from_node])
            graph[from_node].append(
                _FlowEdge(to_node, len(graph[to_node]), capacity, cost, candidate_id)
            )
            graph[to_node].append(
                _FlowEdge(from_node, forward_index, 0, -cost, "")
            )
            return forward_index

        vehicle_index = {vehicle_id: vehicle_start + i for i, vehicle_id in enumerate(eligible_vehicle_ids)}
        order_index = {order_id: order_start + i for i, order_id in enumerate(order_ids)}
        for vehicle_id in eligible_vehicle_ids:
            add_edge(source, vehicle_index[vehicle_id], 1, 0)
            # A vehicle can remain unused. The penalty exceeds the maximum
            # possible sum of all selected candidate costs, guaranteeing that
            # every feasible additional match wins before quality terms.
            add_edge(vehicle_index[vehicle_id], sink, 1, unserved_penalty)
        for order_id in order_ids:
            add_edge(order_index[order_id], sink, 1, 0)

        refs: List[Tuple[int, int, Candidate]] = []
        for candidate in sorted(valid, key=lambda item: item.candidate_id):
            idx = add_edge(
                vehicle_index[candidate.vehicle_id],
                order_index[candidate.order_id],
                1,
                candidate_cost(candidate),
                candidate.candidate_id,
            )
            refs.append((vehicle_index[candidate.vehicle_id], idx, candidate))

        flow = 0
        while flow < flow_target:
            distances: List[Optional[int]] = [None] * node_count
            prev: List[Tuple[int, int]] = [(-1, -1)] * node_count
            in_queue = [False] * node_count
            queue = [source]
            distances[source] = 0
            in_queue[source] = True
            cursor = 0
            while cursor < len(queue):
                current = queue[cursor]
                cursor += 1
                in_queue[current] = False
                for edge_index, edge in enumerate(graph[current]):
                    if edge.capacity <= 0:
                        continue
                    current_distance = distances[current]
                    if current_distance is None:
                        continue
                    candidate_distance = current_distance + edge.cost
                    known_distance = distances[edge.to]
                    if known_distance is not None and candidate_distance >= known_distance:
                        continue
                    distances[edge.to] = candidate_distance
                    prev[edge.to] = (current, edge_index)
                    if not in_queue[edge.to]:
                        queue.append(edge.to)
                        in_queue[edge.to] = True
            if distances[sink] is None:
                return [], "error"
            current = sink
            while current != source:
                previous, edge_index = prev[current]
                edge = graph[previous][edge_index]
                edge.capacity -= 1
                graph[current][edge.reverse].capacity += 1
                current = previous
            flow += 1

        selected: List[Candidate] = []
        for from_node, edge_index, candidate in refs:
            if graph[from_node][edge_index].capacity == 0:
                selected.append(candidate)
        selected.sort(key=lambda item: (item.order_id, item.vehicle_id, item.candidate_id))
        return selected, "optimal"


def greedy_assign(candidates: Sequence[Candidate], orders: Sequence[Order]) -> List[Candidate]:
    """D0: deterministic earliest-deadline, nearest-feasible assignment."""

    order_deadline = {order.order_id: order.arrival_time_sec for order in orders}
    selected: List[Candidate] = []
    used_vehicles = set()
    used_orders = set()
    for candidate in sorted(
        (item for item in candidates if item.hard_constraint_ok),
        key=lambda item: (
            order_deadline.get(item.order_id, 0.0),
            item.predicted_wait_sec,
            item.empty_distance_m,
            item.order_id,
            item.vehicle_id,
        ),
    ):
        if candidate.vehicle_id in used_vehicles or candidate.order_id in used_orders:
            continue
        selected.append(candidate)
        used_vehicles.add(candidate.vehicle_id)
        used_orders.add(candidate.order_id)
    return selected


def build_assignments(candidates: Sequence[Candidate]) -> List[Assignment]:
    return [
        Assignment(
            assignment_id="assignment-" + candidate.candidate_id,
            candidate_id=candidate.candidate_id,
            vehicle_id=candidate.vehicle_id,
            order_id=candidate.order_id,
            assignment_type=(
                "immediate" if candidate.candidate_type == "immediate_pickup" else "next_reservation"
            ),
            predicted_pickup_time_sec=candidate.predicted_pickup_time_sec,
            predicted_dropoff_time_sec=candidate.predicted_dropoff_time_sec,
            predicted_wait_sec=candidate.predicted_wait_sec,
        )
        for candidate in candidates
    ]


def validate_selection(
    selected: Sequence[Candidate],
    candidates: Sequence[Candidate],
    max_pickup_wait_sec: float,
) -> List[str]:
    errors: List[str] = []
    vehicle_ids = set()
    order_ids = set()
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    for candidate in selected:
        if candidate.candidate_id not in by_id:
            errors.append("selected_candidate_missing:" + candidate.candidate_id)
        if candidate.vehicle_id in vehicle_ids:
            errors.append("vehicle_assigned_twice:" + candidate.vehicle_id)
        if candidate.order_id in order_ids:
            errors.append("order_assigned_twice:" + candidate.order_id)
        vehicle_ids.add(candidate.vehicle_id)
        order_ids.add(candidate.order_id)
        if not candidate.hard_constraint_ok:
            errors.append("hard_constraint_failed:" + candidate.candidate_id)
        if candidate.predicted_wait_sec > max_pickup_wait_sec:
            errors.append("wait_limit_failed:" + candidate.candidate_id)
    return errors

