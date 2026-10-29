"""Conservative, offline witness search for an R4 simple route suffix.

This module is intentionally separate from the active R4 runner. A returned
witness is a concrete directed route that visits pickup before dropoff and
does not revisit any node in the accepted prefix or within the suffix. Failure
to find a witness is inconclusive: the bounded search does not prove global
unreachability.
"""

from collections import deque
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from .models import Edge


@dataclass(frozen=True)
class WaypointSuffixWitness:
    node_sequence: List[str]
    edge_sequence: List[str]


@dataclass(frozen=True)
class CertifiedNextEdge:
    edge: Edge
    suffix_witness: WaypointSuffixWitness
    total_moves_to_dropoff: int


@dataclass(frozen=True)
class NextEdgeCertification:
    certified_edges: List[CertifiedNextEdge]
    filtered_edge_reasons: dict[str, str]


def audit_next_edges(
    edges: Sequence[Edge],
    current_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    visited_node_ids: Iterable[str] = (),
    blocked_edge_ids: Iterable[str] = (),
    remaining_action_budget: Optional[int] = None,
    max_first_leg_attempts: int = 64,
) -> NextEdgeCertification:
    """Audit outgoing edges and certify only complete, budget-fitting suffixes.

    This is the interface a future dynamic direction schema can consume. It
    does not choose a route: the model can still select among certified edges.
    ``suffix_witness_not_found`` means this bounded search could not certify
    the edge; it is not proof that the complete graph has no route.
    """
    blocked = set(blocked_edge_ids)
    visited = set(visited_node_ids)
    visited.add(current_node_id)
    certified = []
    filtered = {}
    outgoing = sorted(
        (
            edge
            for edge in edges
            if edge.from_node == current_node_id and edge.edge_id not in blocked
        ),
        key=lambda edge: edge.edge_id,
    )
    for edge in outgoing:
        if edge.to_node in visited:
            filtered[edge.edge_id] = "target_already_visited"
            continue
        if edge.to_node == dropoff_node_id and pickup_node_id not in visited:
            filtered[edge.edge_id] = "pickup_not_seen"
            continue
        witness = build_waypoint_suffix_witness(
            edges,
            edge.to_node,
            pickup_node_id,
            dropoff_node_id,
            visited_node_ids=visited | {edge.to_node},
            blocked_edge_ids=blocked,
            max_first_leg_attempts=max_first_leg_attempts,
        )
        if witness is None:
            filtered[edge.edge_id] = "suffix_witness_not_found"
            continue
        total_moves = 1 + len(witness.edge_sequence)
        if remaining_action_budget is not None and total_moves > remaining_action_budget:
            filtered[edge.edge_id] = "action_budget_insufficient"
            continue
        certified.append(CertifiedNextEdge(edge, witness, total_moves))
    return NextEdgeCertification(certified, filtered)


def certified_next_edges(
    edges: Sequence[Edge],
    current_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    visited_node_ids: Iterable[str] = (),
    blocked_edge_ids: Iterable[str] = (),
    remaining_action_budget: Optional[int] = None,
    max_first_leg_attempts: int = 64,
) -> List[CertifiedNextEdge]:
    """Convenience wrapper returning only the witnessed next edges."""
    return audit_next_edges(
        edges,
        current_node_id,
        pickup_node_id,
        dropoff_node_id,
        visited_node_ids,
        blocked_edge_ids,
        remaining_action_budget,
        max_first_leg_attempts,
    ).certified_edges


def _shortest_path(
    edges: Sequence[Edge],
    start_node_id: str,
    target_node_id: str,
    forbidden_node_ids: Iterable[str],
    blocked_edge_ids: Iterable[str],
) -> Optional[WaypointSuffixWitness]:
    if start_node_id == target_node_id:
        return WaypointSuffixWitness([start_node_id], [])

    forbidden = set(forbidden_node_ids) - {start_node_id, target_node_id}
    blocked = set(blocked_edge_ids)
    adjacency = {}
    for edge in edges:
        if edge.edge_id in blocked or edge.from_node in forbidden or edge.to_node in forbidden:
            continue
        adjacency.setdefault(edge.from_node, []).append(edge)
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda edge: edge.edge_id)

    queue = deque([start_node_id])
    predecessor = {}
    seen = {start_node_id}
    while queue:
        current = queue.popleft()
        for edge in adjacency.get(current, ()):
            target = edge.to_node
            if target in seen:
                continue
            seen.add(target)
            predecessor[target] = (current, edge.edge_id)
            if target == target_node_id:
                nodes = [target_node_id]
                edge_ids = []
                while nodes[-1] != start_node_id:
                    previous, edge_id = predecessor[nodes[-1]]
                    edge_ids.append(edge_id)
                    nodes.append(previous)
                nodes.reverse()
                edge_ids.reverse()
                return WaypointSuffixWitness(nodes, edge_ids)
            queue.append(target)
    return None


def build_waypoint_suffix_witness(
    edges: Sequence[Edge],
    current_node_id: str,
    pickup_node_id: str,
    dropoff_node_id: str,
    visited_node_ids: Iterable[str] = (),
    blocked_edge_ids: Iterable[str] = (),
    max_first_leg_attempts: int = 64,
) -> Optional[WaypointSuffixWitness]:
    """Find a bounded, concrete, no-revisit suffix through pickup to dropoff.

    The first attempt uses the shortest first leg. If it blocks every known
    second leg, the search bans overlapping first-leg nodes and tries bounded
    alternatives. The accepted-prefix current node is the only visited node
    that may appear as the suffix start. A found witness certifies safety;
    ``None`` means only that this bounded search found no certificate.
    """
    if not current_node_id or not pickup_node_id or not dropoff_node_id:
        return None
    if pickup_node_id == dropoff_node_id or max_first_leg_attempts < 1:
        return None

    edge_list = list(edges)
    blocked = set(blocked_edge_ids)
    visited = set(visited_node_ids)
    visited.add(current_node_id)
    if dropoff_node_id in visited and current_node_id != dropoff_node_id:
        return None

    # If pickup is already in the accepted prefix, only the remaining dropoff
    # leg is needed. All earlier nodes remain forbidden.
    if pickup_node_id in visited:
        suffix = _shortest_path(
            edge_list,
            current_node_id,
            dropoff_node_id,
            visited - {current_node_id},
            blocked,
        )
        return suffix if _is_simple(suffix) else None

    prior_nodes = visited - {current_node_id}
    first_leg_forbidden = prior_nodes | {dropoff_node_id}
    unrestricted_second = _shortest_path(
        edge_list,
        pickup_node_id,
        dropoff_node_id,
        prior_nodes,
        blocked,
    )
    if unrestricted_second is None:
        return None

    pending_bans = deque([frozenset()])
    seen_bans = {frozenset()}
    attempts = 0
    while pending_bans and attempts < max_first_leg_attempts:
        extra_forbidden = pending_bans.popleft()
        first_leg = _shortest_path(
            edge_list,
            current_node_id,
            pickup_node_id,
            first_leg_forbidden | extra_forbidden,
            blocked,
        )
        attempts += 1
        if first_leg is None:
            continue

        first_leg_internal = set(first_leg.node_sequence[:-1]) - {current_node_id}
        second_leg = _shortest_path(
            edge_list,
            pickup_node_id,
            dropoff_node_id,
            prior_nodes | first_leg_internal,
            blocked,
        )
        if second_leg is not None:
            witness = WaypointSuffixWitness(
                list(first_leg.node_sequence) + list(second_leg.node_sequence[1:]),
                list(first_leg.edge_sequence) + list(second_leg.edge_sequence),
            )
            if _is_simple(witness):
                return witness

        # Branch only on nodes shared with a concrete pickup-to-dropoff path.
        # Any disjoint solution must change at least one such conflict.
        conflicts = [
            node_id
            for node_id in first_leg.node_sequence[1:-1]
            if node_id in unrestricted_second.node_sequence[1:-1]
        ]
        for node_id in conflicts:
            next_bans = frozenset(set(extra_forbidden) | {node_id})
            if next_bans not in seen_bans:
                seen_bans.add(next_bans)
                pending_bans.append(next_bans)

    return None


def _is_simple(witness: Optional[WaypointSuffixWitness]) -> bool:
    if witness is None or len(witness.node_sequence) != len(witness.edge_sequence) + 1:
        return False
    return len(witness.node_sequence) == len(set(witness.node_sequence))
