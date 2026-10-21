"""Deterministic global assignment and output validation."""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

from .models import Assignment, Candidate, Order, Vehicle


@dataclass
class _FlowEdge:
    to: int
    reverse: int
    capacity: int
    cost: int
    candidate_id: str = ""


class DeterministicGlobalOptimizer:
    """Minimum-cost bipartite flow with an explicit unserved penalty.

    The large unserved penalty implements the documented lexicographic order:
    maximize served orders first, then minimize wait, distance and energy.
    """

    def __init__(self, method_name: str = "global_min_cost_flow_v1"):
        self.method_name = method_name

    @staticmethod
    def _cost(candidate: Candidate) -> int:
        wait = int(round(candidate.predicted_wait_sec * 1000.0))
        distance = int(round(candidate.empty_distance_m * 100.0))
        energy = int(round(candidate.energy_kwh * 100.0))
        return wait * 1_000_000 + distance + energy

    def optimize(
        self,
        candidates: Sequence[Candidate],
        vehicles: Sequence[Vehicle],
        orders: Sequence[Order],
    ) -> Tuple[List[Candidate], str]:
        valid = [candidate for candidate in candidates if candidate.hard_constraint_ok]
        eligible_vehicle_ids = sorted({candidate.vehicle_id for candidate in valid})
        if not eligible_vehicle_ids:
            return [], "infeasible"
        order_ids = sorted({candidate.order_id for candidate in valid})
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
            # A vehicle can remain unused; this is much cheaper than violating
            # a hard rule but much more expensive than any feasible order.
            add_edge(vehicle_index[vehicle_id], sink, 1, 10**18)
        for order_id in order_ids:
            add_edge(order_index[order_id], sink, 1, 0)

        refs: List[Tuple[int, int, Candidate]] = []
        for candidate in sorted(valid, key=lambda item: item.candidate_id):
            idx = add_edge(
                vehicle_index[candidate.vehicle_id],
                order_index[candidate.order_id],
                1,
                self._cost(candidate),
                candidate.candidate_id,
            )
            refs.append((vehicle_index[candidate.vehicle_id], idx, candidate))

        flow_target = len(eligible_vehicle_ids)
        flow = 0
        while flow < flow_target:
            distances = [10**30] * node_count
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
                    candidate_distance = distances[current] + edge.cost
                    if candidate_distance >= distances[edge.to]:
                        continue
                    distances[edge.to] = candidate_distance
                    prev[edge.to] = (current, edge_index)
                    if not in_queue[edge.to]:
                        queue.append(edge.to)
                        in_queue[edge.to] = True
            if distances[sink] >= 10**30:
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

