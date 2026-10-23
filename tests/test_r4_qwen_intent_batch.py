import unittest

from scripts.run_r4_qwen_intent_local_batch import (
    _build_batch_prompt,
    _resolve_candidate_intent,
)


class R4QwenIntentBatchTests(unittest.TestCase):
    def setUp(self):
        self.query = {
            "task_id": "t1",
            "task_type": "semantic_road_feature_position",
            "map_id": "grid-100x100-seed-0",
            "map_version": "v1",
            "map_input_mode": "road_feature_protocol_v1",
            "start_node_id": "node_90_90",
            "pickup_text": "请在公交站路牌旁、西侧上车。",
            "dropoff_text": "请在体育馆附近下车。",
            "pickup_resolution_mode": "road_feature_relation",
            "dropoff_resolution_mode": "landmark_graph_candidates",
            "matched_pickup_entries": [{
                "entry_id": "feature:sign_1",
                "entry_type": "road_sign",
                "name": "公交站路牌",
                "aliases": ["站牌"],
                "side_of_road": "west_side",
                "orientation": "toward_east",
                "anchor_node_id": "node_1_1",
                "anchor_coordinates": {"x": 1, "y": 1},
                "pickup_candidates": [
                    {"node_id": "node_1_1", "coordinates": {"x": 1, "y": 1}, "relation_to_feature": "feature_side"},
                    {"node_id": "node_2_1", "coordinates": {"x": 2, "y": 1}, "relation_to_feature": "opposite_side"},
                ],
            }],
            "matched_dropoff_entries": [{
                "entry_id": "landmark:node_5_5",
                "entry_type": "landmark",
                "name": "体育馆",
                "aliases": [],
                "anchor_node_id": "node_5_5",
                "anchor_coordinates": {"x": 5, "y": 5},
                "dropoff_candidates": [
                    {"node_id": "node_5_4", "coordinates": {"x": 5, "y": 4}},
                ],
            }],
            "pickup_candidates": [
                {"node_id": "node_1_1", "coordinates": {"x": 1, "y": 1}, "relation_to_feature": "feature_side"},
                {"node_id": "node_2_1", "coordinates": {"x": 2, "y": 1}, "relation_to_feature": "opposite_side"},
            ],
            "dropoff_candidates": [
                {"node_id": "node_5_4", "coordinates": {"x": 5, "y": 4}},
            ],
            "allowed_feature_refs": ["feature:sign_1"],
            "allowed_landmark_refs": ["landmark:node_5_5"],
            "uses_evaluator_truth": False,
        }
        self.prompt, self.model_query, self.pickup_map, self.dropoff_map = _build_batch_prompt(self.query)

    def test_model_query_uses_opaque_candidate_refs_and_links_each_to_anchor(self):
        self.assertEqual(self.pickup_map, {"P00": "node_1_1", "P01": "node_2_1"})
        self.assertEqual(self.dropoff_map, {"D00": "node_5_4"})
        self.assertNotIn('"node_id":"node_1_1"', self.prompt)
        self.assertNotIn('"anchor_node_id":"node_1_1"', self.prompt)
        self.assertEqual(
            self.model_query["pickup_candidates"][0]["candidate_anchor_links"],
            [{
                "anchor_ref": "feature:sign_1",
                "entry_type": "road_sign",
                "name": "公交站路牌",
                "side_of_road": "west_side",
                "orientation": "toward_east",
                "relation_to_feature": "feature_side",
            }],
        )

    def test_candidate_must_belong_to_the_selected_anchor(self):
        parsed = {
            "schema_version": "route_intent_candidate_v2",
            "status": "resolved",
            "pickup_candidate_ref": "P01",
            "dropoff_candidate_ref": "D00",
            "pickup_anchor_ref": "feature:sign_1",
            "dropoff_anchor_ref": "landmark:node_5_5",
            "relation": "feature_side",
            "side": "west",
            "feature_evidence_refs": ["feature:sign_1"],
            "confidence": 0.9,
        }
        parsed["pickup_anchor_ref"] = "feature:other"
        _, validation = _resolve_candidate_intent(
            parsed, self.query, self.model_query, self.pickup_map, self.dropoff_map
        )
        self.assertFalse(validation["valid"])
        self.assertEqual(validation["reason"], "intent_pickup_candidate_anchor_mismatch")

        parsed["pickup_anchor_ref"] = "feature:sign_1"
        _, validation = _resolve_candidate_intent(
            parsed, self.query, self.model_query, self.pickup_map, self.dropoff_map
        )
        self.assertFalse(validation["valid"])
        self.assertEqual(validation["reason"], "intent_pickup_candidate_relation_mismatch")

        parsed["pickup_candidate_ref"] = "P00"
        parsed["relation"] = "feature_side"
        resolved, validation = _resolve_candidate_intent(
            parsed, self.query, self.model_query, self.pickup_map, self.dropoff_map
        )
        self.assertTrue(validation["valid"])
        self.assertEqual(resolved["pickup_node_id"], "node_1_1")

    def test_graph_only_feature_task_does_not_require_a_feature_anchor(self):
        query = dict(self.query)
        query.update({
            "map_input_mode": "graph_only",
            "pickup_resolution_mode": "graph_only_start_nearest_fallback",
            "matched_pickup_entries": [],
            "allowed_feature_refs": [],
            "pickup_candidates": [
                {"node_id": "node_1_1", "coordinates": {"x": 1, "y": 1}},
                {"node_id": "node_2_1", "coordinates": {"x": 2, "y": 1}},
            ],
        })
        _, model_query, pickup_map, dropoff_map = _build_batch_prompt(query)
        parsed = {
            "schema_version": "route_intent_candidate_v2",
            "status": "resolved",
            "pickup_candidate_ref": "P00",
            "dropoff_candidate_ref": "D00",
            "pickup_anchor_ref": None,
            "dropoff_anchor_ref": "landmark:node_5_5",
            "relation": "near",
            "side": "none",
            "feature_evidence_refs": [],
            "confidence": 0.8,
        }
        resolved, validation = _resolve_candidate_intent(
            parsed, query, model_query, pickup_map, dropoff_map
        )
        self.assertTrue(validation["valid"])
        self.assertEqual(resolved["pickup_node_id"], "node_1_1")


if __name__ == "__main__":
    unittest.main()
