import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reconcile_r4_codex_segment_rejected_energy import (
    evaluate_by_occupancy,
    rejected_proposal_energy,
)
from route_experiment.models import Edge, Node, RoadMap


class RejectedProposalEnergyTests(unittest.TestCase):
    def setUp(self):
        self.road_map = RoadMap(
            map_id="mini",
            version="1",
            size=3,
            nodes=[
                Node(node_id="n0", x=0, y=0),
                Node(node_id="n1", x=1, y=0),
                Node(node_id="n2", x=2, y=0),
            ],
            edges=[
                Edge(
                    edge_id="e01",
                    from_node="n0",
                    to_node="n1",
                    length_m=100,
                    base_speed_mps=8,
                    congestion_level=0,
                ),
                Edge(
                    edge_id="e12",
                    from_node="n1",
                    to_node="n2",
                    length_m=100,
                    base_speed_mps=8,
                    congestion_level=0,
                ),
            ],
            boundary={"min_x": 0, "max_x": 2, "min_y": 0, "max_y": 0},
        )

    def test_estimates_first_rejected_action_when_it_is_a_route_edge(self):
        attempt = {
            "request_number": 7,
            "current_node_id": "n0",
            "proposal": {"actions": [{"direction": "E"}]},
            "audit": {
                "first_violation_step": 1,
                "violations": [{"kind": "node_revisited", "step": 1}],
            },
        }

        result = rejected_proposal_energy(
            self.road_map, self.road_map.edges[:1], [], attempt
        )

        self.assertEqual(result["status"], "estimated")
        self.assertTrue(result["includes_first_rejected_action"])
        self.assertTrue(result["first_rejected_action_energy_estimable"])
        self.assertGreater(result["first_rejected_action"]["energy_kwh"], 0)
        self.assertGreater(result["energy_kwh"], 0)
        self.assertEqual(result["failure_reason"], "node_revisited")

    def test_one_way_violation_estimates_only_the_legal_route_prefix(self):
        attempt = {
            "request_number": 8,
            "current_node_id": "n0",
            "proposal": {"actions": [{"direction": "E"}, {"direction": "W"}]},
            "audit": {
                "first_violation_step": 2,
                "violations": [{"kind": "one_way_direction", "step": 2}],
            },
        }

        result = rejected_proposal_energy(
            self.road_map, self.road_map.edges[:1], [], attempt
        )

        self.assertEqual(result["status"], "estimated")
        self.assertFalse(result["includes_first_rejected_action"])
        self.assertIsNone(result["first_rejected_action"]["energy_kwh"])
        self.assertEqual(
            result["first_rejected_action"]["non_estimable_reason"],
            "no_directed_edge_in_map",
        )
        self.assertEqual(result["estimated_edge_count"], 1)
        self.assertGreater(result["energy_kwh"], 0)
        self.assertEqual(
            result["truncated_at"]["reason"], "route_input_directed_edge_missing"
        )

    def test_missing_route_edge_keeps_energy_null_instead_of_zero(self):
        attempt = {
            "request_number": 9,
            "current_node_id": "n0",
            "proposal": {"actions": [{"direction": "E"}]},
            "audit": {
                "first_violation_step": 1,
                "violations": [{"kind": "one_way_direction", "step": 1}],
            },
        }

        result = rejected_proposal_energy(self.road_map, [], [], attempt)

        self.assertEqual(result["status"], "not_estimable")
        self.assertIsNone(result["energy_kwh"])
        self.assertEqual(result["estimated_edge_count"], 0)

    def test_route_energy_splits_empty_and_occupied_load_at_pickup(self):
        result = evaluate_by_occupancy(
            self.road_map,
            ["n0", "n1", "n2"],
            ["e01", "e12"],
            "n1",
            False,
        )

        self.assertEqual([leg["leg_type"] for leg in result["legs"]], ["empty", "occupied"])
        self.assertEqual([leg["payload_mass_kg"] for leg in result["legs"]], [0.0, 75.0])
        self.assertTrue(result["pickup_stop_and_restart_assumed"])
        self.assertAlmostEqual(
            result["energy_kwh"],
            sum(leg["energy_kwh"] for leg in result["legs"]),
        )


if __name__ == "__main__":
    unittest.main()
