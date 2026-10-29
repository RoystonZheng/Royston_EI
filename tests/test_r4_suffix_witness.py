import unittest

from route_experiment.models import Edge
from route_experiment.r4_suffix_witness import (
    audit_next_edges,
    build_waypoint_suffix_witness,
    certified_next_edges,
)


class R4SuffixWitnessTests(unittest.TestCase):
    def test_shortest_first_leg_dead_end_does_not_hide_valid_longer_simple_route(self):
        # The shortest S->P leg is S-A-P, but the only P->D leg is P-A-D.
        # A valid complete route exists only if the gate finds S-X-P-A-D.
        edges = [
            Edge("s-a", "S", "A", 1, 1),
            Edge("a-p", "A", "P", 1, 1),
            Edge("s-x", "S", "X", 1, 1),
            Edge("x-p", "X", "P", 1, 1),
            Edge("p-a", "P", "A", 1, 1),
            Edge("a-d", "A", "D", 1, 1),
        ]

        witness = build_waypoint_suffix_witness(edges, "S", "P", "D")

        self.assertIsNotNone(witness)
        self.assertEqual(witness.node_sequence, ["S", "X", "P", "A", "D"])
        self.assertEqual(witness.edge_sequence, ["s-x", "x-p", "p-a", "a-d"])

    def test_witness_never_reuses_a_node_from_the_accepted_prefix(self):
        edges = [
            Edge("a-p", "A", "P", 1, 1),
            Edge("p-b", "P", "B", 1, 1),
            Edge("b-d", "B", "D", 1, 1),
            Edge("p-s", "P", "S", 1, 1),
            Edge("s-d", "S", "D", 1, 1),
        ]

        witness = build_waypoint_suffix_witness(
            edges, "A", "P", "D", visited_node_ids=["S", "A"]
        )

        self.assertIsNotNone(witness)
        self.assertEqual(witness.node_sequence, ["A", "P", "B", "D"])
        self.assertEqual(len(witness.node_sequence), len(set(witness.node_sequence)))

    def test_no_witness_is_reported_without_claiming_global_unreachability(self):
        edges = [
            Edge("s-p", "S", "P", 1, 1),
            Edge("p-s", "P", "S", 1, 1),
            Edge("p-d-via-s", "P", "S", 1, 1),
            Edge("s-d", "S", "D", 1, 1),
        ]

        witness = build_waypoint_suffix_witness(edges, "S", "P", "D")

        self.assertIsNone(witness)

    def test_dynamic_candidate_gate_removes_dead_end_edge_and_keeps_witnessed_edge(self):
        edges = [
            Edge("s-a", "S", "A", 1, 1),
            Edge("a-p", "A", "P", 1, 1),
            Edge("s-x", "S", "X", 1, 1),
            Edge("x-p", "X", "P", 1, 1),
            Edge("p-a", "P", "A", 1, 1),
            Edge("a-d", "A", "D", 1, 1),
        ]

        certification = audit_next_edges(
            edges, "S", "P", "D", visited_node_ids=["S"], remaining_action_budget=4
        )

        self.assertEqual(
            [item.edge.edge_id for item in certification.certified_edges], ["s-x"]
        )
        self.assertEqual(
            certification.filtered_edge_reasons["s-a"], "suffix_witness_not_found"
        )
        self.assertEqual(certification.certified_edges[0].total_moves_to_dropoff, 4)
        self.assertEqual(
            certification.certified_edges[0].suffix_witness.node_sequence,
            ["X", "P", "A", "D"],
        )

    def test_dynamic_candidate_gate_respects_remaining_action_budget(self):
        edges = [
            Edge("s-p", "S", "P", 1, 1),
            Edge("p-d", "P", "D", 1, 1),
        ]

        certification = audit_next_edges(
            edges, "S", "P", "D", visited_node_ids=["S"], remaining_action_budget=1
        )

        self.assertEqual(certification.certified_edges, [])
        self.assertEqual(
            certification.filtered_edge_reasons["s-p"], "action_budget_insufficient"
        )


if __name__ == "__main__":
    unittest.main()
