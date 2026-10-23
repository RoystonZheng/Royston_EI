import unittest

from route_experiment.models import Edge, Node, RoadMap
from route_experiment.r4_compact_route import (
    build_r4_compact_route_schema,
    build_r4_compact_route_messages,
    build_r4_compact_route_feedback,
    validate_r4_compact_route_proposal,
)
from route_experiment.models import VehicleOrderTask
from route_experiment.routing import build_route_input_package


class R4CompactRouteTests(unittest.TestCase):
    def setUp(self):
        self.road_map = RoadMap(
            map_id="compact-route-test-map-0",
            version="v1",
            size=2,
            nodes=[
                Node("node_0_0", 0, 0),
                Node("node_1_0", 1, 0),
                Node("node_0_1", 0, 1, legal_pickup=True),
                Node("node_1_1", 1, 1, legal_dropoff=True),
            ],
            edges=[
                Edge("edge_north", "node_0_0", "node_0_1", 100.0, 10.0),
                Edge("edge_east", "node_0_1", "node_1_1", 100.0, 10.0),
                Edge("edge_back", "node_0_1", "node_0_0", 100.0, 10.0),
                Edge("edge_oneway", "node_0_0", "node_1_0", 100.0, 10.0, one_way=True),
                Edge("edge_up", "node_1_0", "node_1_1", 100.0, 10.0),
            ],
            boundary={"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1},
        )

    def test_schema_keeps_long_route_contract_compact(self):
        schema = build_r4_compact_route_schema(max_route_edges=9999)

        self.assertEqual(schema["type"], "object")
        self.assertIn("move_sequence", schema["properties"])
        self.assertIn("step_check_sequence", schema["properties"])
        self.assertLess(len(str(schema).encode("utf-8")), 2000)

    def test_messages_keep_feedback_inside_json_and_exclude_evaluator_truth(self):
        task = VehicleOrderTask(
            task_id="compact-task",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
        )
        package = build_route_input_package(self.road_map, task)
        messages = build_r4_compact_route_messages(
            package,
            task.start_node_id,
            task.pickup_node_id,
            task.dropoff_node_id,
            feedback={"violations": [{"step": 1, "kind": "one_way_direction"}]},
        )
        user_payload = __import__("json").loads(messages[1]["content"])

        self.assertIn("r4_validation_feedback", user_payload)
        self.assertIn("one_way_direction", messages[1]["content"])
        self.assertNotIn("true_pickup_node_id", messages[1]["content"])
        self.assertIn("step_check_sequence", messages[0]["content"])

    def test_messages_include_truth_free_road_feature_context(self):
        task = VehicleOrderTask(
            task_id="compact-semantic-task",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
            task_type="semantic_place",
        )
        package = build_route_input_package(self.road_map, task)
        context = {
            "protocol": {"name": "RoadFeatureInputProtocol", "version": "v1"},
            "pickup_reference": {"name": "north gate", "anchor_node_id": "node_0_1"},
            "intent": {"pickup_relation": "at"},
        }
        messages = build_r4_compact_route_messages(
            package,
            task.start_node_id,
            task.pickup_node_id,
            task.dropoff_node_id,
            route_feature_context=context,
        )
        user_payload = __import__("json").loads(messages[1]["content"])

        self.assertEqual(user_payload["i"], context)
        self.assertIn("RoadFeatureInputProtocol v1", messages[0]["content"])
        self.assertNotIn("ground_truth", messages[1]["content"])

    def test_messages_reject_evaluator_truth_in_road_feature_context(self):
        task = VehicleOrderTask(
            task_id="compact-semantic-task",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
        )
        package = build_route_input_package(self.road_map, task)

        with self.assertRaisesRegex(ValueError, "feature_context_contains_evaluator_truth"):
            build_r4_compact_route_messages(
                package,
                task.start_node_id,
                task.pickup_node_id,
                task.dropoff_node_id,
                route_feature_context={"ground_truth": {"pickup": "node_0_1"}},
            )

    def test_feedback_contains_rejection_location_without_truth_fields(self):
        audit = validate_r4_compact_route_proposal(
            self.road_map,
            start_node_id="node_1_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
            payload={
                "status": "completed",
                "move_sequence": "WNE",
                "step_check_sequence": "111",
            },
        )

        feedback = build_r4_compact_route_feedback(audit)

        self.assertEqual(feedback["first_violation_step"], 1)
        self.assertEqual(feedback["violations"][0]["kind"], "one_way_direction")
        self.assertNotIn("true_pickup_node_id", str(feedback))

    def test_validator_accepts_complete_directed_route_with_per_step_checks(self):
        result = validate_r4_compact_route_proposal(
            self.road_map,
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
            payload={
                "status": "completed",
                "move_sequence": "NE",
                "step_check_sequence": "11",
            },
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["route_node_sequence"], ["node_0_0", "node_0_1", "node_1_1"])
        self.assertEqual(result["route_edge_sequence"], ["edge_north", "edge_east"])
        self.assertEqual([step["local_directed_edge_check"] for step in result["steps"]], ["PASS", "PASS"])

    def test_validator_rejects_reverse_one_way_move_and_keeps_step_location(self):
        result = validate_r4_compact_route_proposal(
            self.road_map,
            start_node_id="node_1_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
            payload={
                "status": "completed",
                "move_sequence": "WNE",
                "step_check_sequence": "111",
            },
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["violations"][0]["kind"], "one_way_direction")
        self.assertEqual(result["violations"][0]["step"], 1)
        self.assertEqual(result["steps"][0]["from_node_id"], "node_1_0")
        self.assertEqual(result["steps"][0]["to_node_id"], "node_0_0")

    def test_validator_rejects_model_self_check_count_mismatch(self):
        result = validate_r4_compact_route_proposal(
            self.road_map,
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
            payload={
                "status": "completed",
                "move_sequence": "NE",
                "step_check_sequence": "1",
            },
        )

        self.assertFalse(result["accepted"])
        self.assertIn("self_check_count_mismatch", [item["kind"] for item in result["violations"]])

    def test_validator_rejects_blocked_edge_and_never_accepts_partial_route(self):
        result = validate_r4_compact_route_proposal(
            self.road_map,
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
            payload={
                "status": "completed",
                "move_sequence": "NE",
                "step_check_sequence": "11",
            },
            blocked_edge_ids={"edge_east"},
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["violations"][0]["kind"], "blocked_edge")
        self.assertEqual(result["validated_prefix_edge_sequence"], ["edge_north"])


if __name__ == "__main__":
    unittest.main()
