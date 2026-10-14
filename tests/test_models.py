import json
import unittest

from route_experiment.models import (
    Edge,
    Node,
    RoadFeatureInputProtocol,
    RoadMap,
    RoadsideFeature,
    RouteCandidate,
    RouteInputPackage,
)


class ModelContractTests(unittest.TestCase):
    def test_node_and_edge_serialize_with_stable_fields(self):
        node = Node(
            node_id="node_0_0",
            x=0,
            y=0,
            landmark_name=None,
            landmark_aliases=[],
            legal_pickup=True,
            legal_dropoff=True,
        )
        edge = Edge(
            edge_id="edge_0_0_1_0",
            from_node="node_0_0",
            to_node="node_1_0",
            length_m=100.0,
            base_speed_mps=10.0,
            one_way=True,
            congestion_level=2,
            signal_delay_sec=15.0,
        )

        payload = {"node": node.to_dict(), "edge": edge.to_dict()}
        round_trip = json.loads(json.dumps(payload, sort_keys=True))

        self.assertIsNone(round_trip["node"]["landmark_name"])
        self.assertEqual(round_trip["node"]["landmark_aliases"], [])
        self.assertEqual(round_trip["edge"]["congestion_level"], 2)
        self.assertEqual(round_trip["edge"]["signal_delay_sec"], 15.0)

    def test_route_input_package_keeps_empty_alternatives_and_json_safe_metadata(self):
        road_map = RoadMap(
            map_id="map_1",
            version="v1",
            size=20,
            nodes=[],
            edges=[],
            boundary={"min_x": 0, "max_x": 19, "min_y": 0, "max_y": 19},
        )
        package = RouteInputPackage(
            package_id="pkg_1",
            map_id="map_1",
            map_version="v1",
            road_state_version="roads_v1",
            random_seed=1,
            start_node_id="node_0_0",
            pickup_node_id="node_1_0",
            dropoff_node_id="node_2_0",
            task_type="exact_node",
            pickup_text=None,
            dropoff_text=None,
            nodes=[],
            edges=[],
            expansion_count=0,
            serialized_token_estimate=12,
        )
        payload = package.to_dict()

        self.assertEqual(payload["alternative_route_refs"], [])
        self.assertIsNone(payload["pickup_text"])
        self.assertEqual(payload["serialized_token_estimate"], 12)

    def test_protocol_has_entities_features_relations_and_constraints(self):
        protocol = RoadFeatureInputProtocol(
            protocol_name="road_feature_input",
            protocol_version="1.0",
            units={"distance": "m", "time": "s"},
            context={"task_id": "task_1"},
            route_query={"start_entity": "node_0_0"},
            entities=[{"id": "node_0_0", "type": "intersection"}],
            features=[
                {
                    "feature_id": "f1",
                    "subject": "edge_1",
                    "name": "traffic.congestion_level",
                    "value": 2,
                    "unit": "level",
                }
            ],
            relations=[],
            constraints=[{"type": "one_way"}],
            semantic_index=[],
            output_contract={"must_not_invent_entity": True},
        )

        payload = protocol.to_dict()

        self.assertEqual(payload["protocol_version"], "1.0")
        self.assertEqual(payload["features"][0]["value"], 2)
        self.assertTrue(payload["output_contract"]["must_not_invent_entity"])

    def test_roadside_feature_keeps_semantic_side_relation(self):
        feature = RoadsideFeature(
            feature_id="sign_01",
            feature_type="road_sign",
            node_id="node_1_1",
            label="体育馆方向路牌",
            aliases=["体育馆路牌"],
            side_of_road="north_side",
            orientation="toward_south",
            paired_node_id="node_1_2",
        )

        payload = json.loads(json.dumps(feature.to_dict(), ensure_ascii=False))

        self.assertEqual(payload["feature_type"], "road_sign")
        self.assertEqual(payload["paired_node_id"], "node_1_2")
        self.assertEqual(payload["side_of_road"], "north_side")


if __name__ == "__main__":
    unittest.main()
