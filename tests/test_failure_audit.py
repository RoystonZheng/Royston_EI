import unittest

from route_experiment.failure_audit import (
    describe_failure_zh,
    estimate_route_proposal_energy,
    recover_partial_edge_slot_sequence,
    recover_partial_move_sequence,
)
from route_experiment.llm_adapter import replay_move_sequence
from route_experiment.models import Edge, EvaluationConfig, Node, RoadMap


class FailureAuditTests(unittest.TestCase):
    def setUp(self):
        self.road_map = RoadMap(
            map_id="audit-test-map",
            version="grid-test",
            size=3,
            boundary={"min_x": 0, "max_x": 2, "min_y": 0, "max_y": 0},
            nodes=[
                Node(node_id="node_0_0", x=0, y=0),
                Node(node_id="node_1_0", x=1, y=0),
                Node(node_id="node_2_0", x=2, y=0),
            ],
            edges=[
                Edge(
                    edge_id="edge_node_0_0_node_1_0",
                    from_node="node_0_0",
                    to_node="node_1_0",
                    length_m=1000.0,
                    base_speed_mps=10.0,
                    congestion_level=1,
                    signal_delay_sec=20.0,
                    one_way=True,
                ),
                Edge(
                    edge_id="edge_node_1_0_node_2_0",
                    from_node="node_1_0",
                    to_node="node_2_0",
                    length_m=500.0,
                    base_speed_mps=10.0,
                    congestion_level=0,
                    signal_delay_sec=0.0,
                ),
            ],
        )

    def test_proposal_energy_separates_full_path_from_legal_prefix(self):
        replay = replay_move_sequence(self.road_map, "node_0_0", "EWE")

        estimate = estimate_route_proposal_energy(self.road_map, replay)

        self.assertEqual(estimate["status"], "estimated")
        self.assertEqual(estimate["proposed_move_count"], 3)
        self.assertEqual(estimate["validated_prefix_move_count"], 1)
        self.assertEqual(estimate["reverse_edge_inferred_moves"], 1)
        self.assertAlmostEqual(
            estimate["validated_prefix_energy_estimate_kwh"], 0.10225644973632048
        )
        self.assertAlmostEqual(
            estimate["route_energy_estimate_kwh"], 0.30676934920896143
        )
        self.assertEqual(estimate["energy_model_version"], "ev_longitudinal_v2")
        self.assertIn("not actual", estimate["interpretation"])

    def test_partial_move_runs_can_be_recovered_without_claiming_full_route(self):
        output = {
            "content": (
                '{"status":"completed","move_runs":['
                '{"direction":"E","steps":3},'
                '{"direction":"N","steps":2},'
                '{"direction":"W","steps":'
            )
        }

        self.assertEqual(recover_partial_move_sequence(output), "EEENN")

    def test_partial_edge_slot_sequence_keeps_complete_codes_only(self):
        output = {
            "content": (
                '{"edge_slot_sequence":"00C00D00'
            )
        }

        self.assertEqual(recover_partial_edge_slot_sequence(output), "00C00D")

    def test_directed_edge_slot_energy_sums_selected_edges_without_fabricating_gaps(self):
        self.road_map.edges.append(
            Edge(
                edge_id="edge_node_2_0_node_1_0",
                from_node="node_2_0",
                to_node="node_1_0",
                length_m=400.0,
                base_speed_mps=10.0,
            )
        )
        replay = {
            "route_representation": "directed_edge_slots",
            "attempted_route_node_sequence": [
                "node_0_0",
                "node_1_0",
                "node_1_0",
            ],
            "attempted_route_edge_sequence": [
                "edge_node_0_0_node_1_0",
                "edge_node_2_0_node_1_0",
            ],
            "validated_prefix_edge_sequence": ["edge_node_0_0_node_1_0"],
        }

        estimate = estimate_route_proposal_energy(self.road_map, replay)

        self.assertEqual(estimate["proposed_move_count"], 2)
        self.assertEqual(estimate["exact_edge_moves"], 2)
        self.assertEqual(estimate["validated_prefix_move_count"], 1)
        self.assertEqual(estimate["reverse_edge_inferred_moves"], 0)
        self.assertAlmostEqual(estimate["estimated_route_distance_m"], 1400.0)
        self.assertAlmostEqual(
            estimate["route_energy_estimate_kwh"], 0.15072780502660693
        )
        self.assertEqual(estimate["energy_model_version"], "ev_longitudinal_v2")
        self.assertIn("Disconnected", estimate["interpretation"])

    def test_failure_description_distinguishes_model_decline_and_route_violation(self):
        decline = describe_failure_zh({"failure_reason": "no_legal_pickup"})
        violation = describe_failure_zh(
            {"failure_reason": "llm_move_sequence_invalid"},
            {"move_violations": [{"kind": "one_way_direction"}, {"kind": "outside_boundary"}]},
        )
        unplanned = describe_failure_zh(
            {"failure_reason": "model_unable_to_construct_route"}
        )
        claimed_unreachable = describe_failure_zh(
            {"failure_reason": "model_confirmed_no_directed_path"}
        )

        self.assertIn("没有找到合法上车点", decline)
        self.assertIn("已返回路线", violation)
        self.assertIn("单行方向相反 1 步", violation)
        self.assertIn("越过地图边界 1 步", violation)
        self.assertIn("没有报告已确认路网无路可走", unplanned)
        self.assertIn("未经独立可达性验证", claimed_unreachable)


if __name__ == "__main__":
    unittest.main()
