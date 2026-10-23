import unittest

from route_experiment.models import Edge, Node, RoadMap, RoadsideFeature, VehicleOrderTask
from route_experiment.r4_intent import (
    build_intent_prompt,
    build_intent_query,
    build_road_feature_protocol,
    extract_intent_json,
    validate_route_intent,
)


class R4IntentTests(unittest.TestCase):
    def setUp(self):
        self.road_map = RoadMap(
            map_id="r4-intent-map-0",
            version="v1",
            size=3,
            nodes=[
                Node("node_0_0", 0, 0, landmark_name="图书馆", legal_pickup=True),
                Node("node_1_0", 1, 0, legal_pickup=True),
                Node("node_2_0", 2, 0, legal_dropoff=True),
                Node("node_0_1", 0, 1, landmark_name="体育馆", legal_dropoff=True),
            ],
            edges=[
                Edge("e0", "node_0_0", "node_1_0", 100, 10),
                Edge("e1", "node_1_0", "node_2_0", 100, 10),
                Edge("e2", "node_0_0", "node_0_1", 100, 10),
            ],
            boundary={"min_x": 0, "max_x": 2, "min_y": 0, "max_y": 1},
            roadside_features=[
                RoadsideFeature(
                    "sign_01", "road_sign", "node_0_0", "体育馆方向路牌",
                    aliases=["体育馆路牌"], side_of_road="east_side",
                    orientation="toward_east", paired_node_id="node_1_0",
                )
            ],
        )
        self.protocol = build_road_feature_protocol(self.road_map, seed=0)

    def test_protocol_is_versioned_and_contains_feature_relation_candidates(self):
        payload = self.protocol.to_dict()
        feature = next(item for item in payload["semantic_index"] if item["entry_id"] == "feature:sign_01")
        self.assertEqual(payload["protocol_version"], "v1")
        self.assertEqual(
            [item["node_id"] for item in feature["pickup_candidates"]],
            ["node_0_0", "node_1_0"],
        )
        self.assertFalse(payload["output_contract"]["uses_evaluator_truth"])

    def test_feature_query_contains_bounded_candidates_without_truth(self):
        task = VehicleOrderTask(
            "t1", "v", "o", "node_2_0", None, None,
            "请在体育馆路牌旁、东侧的合法上车点上车。",
            "请在体育馆附近下车。", "semantic_road_feature_position",
            true_pickup_node_id="node_0_0", true_dropoff_node_id="node_0_1",
        )
        query = build_intent_query(self.road_map, task, self.protocol)
        encoded = build_intent_prompt(query)
        self.assertEqual(query["allowed_feature_refs"], ["feature:sign_01"])
        self.assertIn("node_0_0", encoded)
        self.assertIn("node_1_0", encoded)
        self.assertNotIn("true_pickup_node_id", encoded)
        self.assertLessEqual(len(query["pickup_candidates"]), 24)

    def test_graph_only_query_hides_roadside_feature_and_uses_explicit_fallback(self):
        task = VehicleOrderTask(
            "t2", "v", "o", "node_2_0", None, None,
            "请在体育馆方向路牌旁、东侧的合法上车点上车。",
            "请在体育馆附近下车。", "semantic_road_feature_position",
        )
        query = build_intent_query(
            self.road_map, task, self.protocol, map_input_mode="graph_only"
        )
        self.assertEqual(query["allowed_feature_refs"], [])
        self.assertEqual(query["pickup_resolution_mode"], "graph_only_start_nearest_fallback")
        self.assertEqual(query["pickup_candidates"][0]["node_id"], "node_1_0")
        self.assertEqual(query["matched_pickup_entries"], [])
        self.assertNotIn("feature:sign_01", str(query))
        self.assertIn("体育馆方向路牌", query["pickup_text"])

    def test_intent_validation_rejects_invented_ids_and_accepts_whitelisted_ids(self):
        task = VehicleOrderTask(
            "t3", "v", "o", "node_2_0", None, None,
            "请在体育馆路牌旁、东侧上车。", "请在体育馆附近下车。",
            "semantic_road_feature_position",
        )
        query = build_intent_query(self.road_map, task, self.protocol)
        common = {
            "schema_version": "route_intent_v1",
            "status": "resolved",
            "pickup_anchor_ref": "feature:sign_01",
            "dropoff_anchor_ref": "landmark:node_0_1",
            "relation": "near",
            "side": "east",
            "feature_evidence_refs": ["feature:sign_01"],
            "confidence": 0.9,
        }
        accepted = validate_route_intent(
            {**common, "pickup_node_id": "node_0_0", "dropoff_node_id": "node_0_1"},
            query,
        )
        rejected = validate_route_intent(
            {**common, "pickup_node_id": "node_99_99", "dropoff_node_id": "node_0_1"},
            query,
        )
        self.assertTrue(accepted["valid"])
        self.assertFalse(rejected["valid"])
        self.assertEqual(rejected["reason"], "intent_pickup_not_in_candidate_whitelist")

    def test_extract_json_ignores_qwen_think_block(self):
        parsed = extract_intent_json(
            '<think>brief note</think>\n{"schema_version":"route_intent_v1","status":"resolved"}'
        )
        self.assertEqual(parsed["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
