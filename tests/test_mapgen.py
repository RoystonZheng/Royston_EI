import json
import unittest

from route_experiment.mapgen import generate_map
from route_experiment.models import MapConfig


class MapGenerationTests(unittest.TestCase):
    def test_same_seed_generates_identical_100_by_100_map(self):
        config = MapConfig(size=100)
        first = generate_map(seed=7, size=100, config=config)
        second = generate_map(seed=7, size=100, config=config)

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(len(first.nodes), 10000)
        self.assertEqual(first.boundary, {"min_x": 0, "max_x": 99, "min_y": 0, "max_y": 99})

    def test_map_has_expected_road_features_and_no_wraparound_edges(self):
        road_map = generate_map(seed=11, size=100, config=MapConfig(size=100))
        nodes = {node.node_id: node for node in road_map.nodes}

        self.assertGreater(len(road_map.edges), 0)
        self.assertTrue(any(edge.one_way for edge in road_map.edges))
        self.assertTrue(any(not edge.one_way for edge in road_map.edges))
        self.assertTrue(any(edge.congestion_level == 2 for edge in road_map.edges))
        self.assertTrue(any(edge.signal_delay_sec > 0 for edge in road_map.edges))
        self.assertGreater(sum(node.legal_pickup for node in road_map.nodes), 0)
        self.assertGreater(sum(node.legal_dropoff for node in road_map.nodes), 0)
        self.assertGreater(sum(bool(node.landmark_name) for node in road_map.nodes), 0)
        self.assertGreaterEqual(
            sum(feature.feature_type == "road_sign" for feature in road_map.roadside_features),
            1,
        )
        self.assertGreaterEqual(
            sum(feature.feature_type == "utility_pole" for feature in road_map.roadside_features),
            1,
        )
        self.assertTrue(
            all(feature.paired_node_id for feature in road_map.roadside_features)
        )

        for edge in road_map.edges:
            source = nodes[edge.from_node]
            target = nodes[edge.to_node]
            self.assertLessEqual(abs(source.x - target.x), 1)
            self.assertLessEqual(abs(source.y - target.y), 1)
            self.assertNotEqual((source.x, source.y), (target.x, target.y))
            self.assertIn(edge.congestion_level, (0, 1, 2))
            self.assertGreater(edge.length_m, 0)
            self.assertGreater(edge.base_speed_mps, 0)

    def test_map_serialization_is_json_safe(self):
        road_map = generate_map(seed=3, size=20, config=MapConfig(size=20))
        encoded = json.dumps(road_map.to_dict(), sort_keys=True)
        self.assertIn('"size": 20', encoded)
        self.assertIn('"road_state_version"', encoded)
        self.assertIn('"roadside_features"', encoded)


if __name__ == "__main__":
    unittest.main()
