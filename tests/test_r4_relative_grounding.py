import unittest

from route_experiment.relative_grounding import (
    resolve_landmark_near_candidate,
    resolve_landmark_relative_candidate,
)


class RelativeGroundingTests(unittest.TestCase):
    def setUp(self):
        self.anchor_ref = "landmark:node_5_5"
        self.model_query = {
            "task_type": "semantic_landmark_relative",
            "pickup_text": "请在大学城西侧的合法上车点上车。",
            "matched_pickup_entries": [{
                "entry_id": self.anchor_ref,
                "entry_type": "landmark",
                "anchor_coordinates": {"x": 5, "y": 5},
            }],
            "pickup_candidates": [
                {"candidate_ref": "P00", "coordinates": {"x": 5, "y": 5}},
                {"candidate_ref": "P01", "coordinates": {"x": 4, "y": 5}},
                {"candidate_ref": "P02", "coordinates": {"x": 4, "y": 10}},
            ],
        }
        self.parsed_intent = {
            "schema_version": "route_intent_candidate_v2",
            "status": "resolved",
            "pickup_anchor_ref": self.anchor_ref,
            "pickup_candidate_ref": "P00",
            "relation": "near",
        }

    def test_explicit_direction_reselects_nearest_whitelisted_candidate(self):
        result = resolve_landmark_relative_candidate(
            self.parsed_intent, self.model_query
        )

        self.assertEqual("resolved", result["status"])
        self.assertEqual("P01", result["candidate_ref"])
        self.assertEqual("west", result["direction"])
        self.assertEqual(self.anchor_ref, result["anchor_ref"])

    def test_explicit_axis_direction_uses_sign_not_dominant_axis(self):
        self.model_query["pickup_candidates"] = [
            {"candidate_ref": "P00", "coordinates": {"x": 4, "y": 10}},
        ]

        result = resolve_landmark_relative_candidate(
            self.parsed_intent, self.model_query
        )

        self.assertEqual("resolved", result["status"])
        self.assertEqual("P00", result["candidate_ref"])

    def test_rejects_anchor_not_in_public_candidate_context(self):
        self.parsed_intent["pickup_anchor_ref"] = "landmark:node_8_8"

        result = resolve_landmark_relative_candidate(
            self.parsed_intent, self.model_query
        )

        self.assertEqual("unresolved", result["status"])
        self.assertEqual("pickup_anchor_not_in_query", result["reason"])

    def test_returns_unresolved_when_no_candidate_is_on_requested_side(self):
        self.model_query["pickup_candidates"] = [
            {"candidate_ref": "P00", "coordinates": {"x": 5, "y": 5}},
            {"candidate_ref": "P01", "coordinates": {"x": 6, "y": 5}},
        ]

        result = resolve_landmark_relative_candidate(
            self.parsed_intent, self.model_query
        )

        self.assertEqual("unresolved", result["status"])
        self.assertEqual("no_candidate_on_requested_side", result["reason"])

    def test_near_relation_selects_nearest_public_dropoff_candidate(self):
        self.model_query["matched_dropoff_entries"] = [{
            "entry_id": "landmark:node_20_20",
            "entry_type": "landmark",
            "anchor_coordinates": {"x": 20, "y": 20},
        }]
        self.model_query["dropoff_text"] = "请在机场附近下车。"
        self.model_query["dropoff_candidates"] = [
            {"candidate_ref": "D00", "coordinates": {"x": 23, "y": 20}},
            {"candidate_ref": "D01", "coordinates": {"x": 21, "y": 20}},
        ]
        parsed = {
            "schema_version": "route_intent_candidate_v2",
            "status": "resolved",
            "dropoff_anchor_ref": "landmark:node_20_20",
        }

        result = resolve_landmark_near_candidate(parsed, self.model_query, "dropoff")

        self.assertEqual("resolved", result["status"])
        self.assertEqual("D01", result["candidate_ref"])

    def test_nearby_pickup_excludes_landmark_anchor_node(self):
        self.model_query["pickup_text"] = "请在大学城附近的合法上车点上车。"
        self.model_query["pickup_candidates"] = [
            {"candidate_ref": "P00", "coordinates": {"x": 5, "y": 5}},
            {"candidate_ref": "P01", "coordinates": {"x": 4, "y": 5}},
        ]
        result = resolve_landmark_near_candidate(
            self.parsed_intent, self.model_query, "pickup"
        )

        self.assertEqual("resolved", result["status"])
        self.assertEqual("P01", result["candidate_ref"])

    def test_nearby_pickup_fails_if_only_anchor_node_is_listed(self):
        self.model_query["pickup_text"] = "请在大学城附近的合法上车点上车。"
        self.model_query["pickup_candidates"] = [
            {"candidate_ref": "P00", "coordinates": {"x": 5, "y": 5}},
        ]
        result = resolve_landmark_near_candidate(
            self.parsed_intent, self.model_query, "pickup"
        )

        self.assertEqual("unresolved", result["status"])
        self.assertEqual("no_legal_candidate_after_endpoint_exclusion", result["reason"])

    def test_near_relation_rejects_anchor_outside_query(self):
        self.model_query["matched_dropoff_entries"] = [{
            "entry_id": self.anchor_ref,
            "entry_type": "landmark",
            "anchor_coordinates": {"x": 5, "y": 5},
        }]
        self.model_query["dropoff_text"] = "请在机场附近下车。"
        self.model_query["dropoff_candidates"] = [
            {"candidate_ref": "D00", "coordinates": {"x": 5, "y": 6}},
        ]

        result = resolve_landmark_near_candidate(
            {
                "schema_version": "route_intent_candidate_v2",
                "status": "resolved",
                "dropoff_anchor_ref": "landmark:unknown",
            },
            self.model_query,
            "dropoff",
        )

        self.assertEqual("unresolved", result["status"])
        self.assertEqual("dropoff_anchor_not_in_query", result["reason"])


if __name__ == "__main__":
    unittest.main()
