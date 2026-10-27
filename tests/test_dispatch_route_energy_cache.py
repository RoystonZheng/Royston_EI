import unittest
from unittest.mock import patch

from dispatch_experiment.route_oracle import GraphRouteOracle
from route_experiment.energy_model import EnergyModelConfig, evaluate_route_edges
from route_experiment.models import Edge, Node, RoadMap


class DispatchRouteEnergyCacheTests(unittest.TestCase):
    def setUp(self):
        self.road_map = RoadMap(
            map_id="dispatch-energy-cache-test",
            version="cache-v1",
            size=2,
            nodes=[Node(node_id="n0", x=0, y=0), Node(node_id="n1", x=1, y=0)],
            edges=[
                Edge(
                    edge_id="e01",
                    from_node="n0",
                    to_node="n1",
                    length_m=250.0,
                    base_speed_mps=10.0,
                    congestion_level=1,
                    signal_delay_sec=8.0,
                    slope_rad=0.01,
                )
            ],
            boundary={"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 0},
        )
        self.oracle = GraphRouteOracle(self.road_map)
        self.multipliers = {0: 1.0, 1: 0.8, 2: 0.6}
        self.profile = EnergyModelConfig()

    def test_repeated_route_uses_cached_components_without_changing_values(self):
        edge = self.road_map.edges[0]
        expected = evaluate_route_edges(
            [edge], self.multipliers, self.profile, payload_mass_kg=0.0
        )
        with patch("dispatch_experiment.route_oracle.evaluate_route_edges", wraps=evaluate_route_edges) as evaluator:
            first = self.oracle.route_energy_summary(
                ["e01"], self.multipliers, self.profile, payload_mass_kg=0.0
            )
            second = self.oracle.route_energy_summary(
                ["e01"], self.multipliers, self.profile, payload_mass_kg=0.0
            )

        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first.net_kwh, expected.net_kwh)
        self.assertEqual(first.propulsion_draw_kwh, expected.propulsion_draw_kwh)
        self.assertEqual(first.regenerated_kwh, expected.regenerated_kwh)
        self.assertEqual(first.auxiliary_kwh, expected.auxiliary_kwh)

    def test_cache_key_separates_load_energy_profile_and_traffic_profile(self):
        with patch("dispatch_experiment.route_oracle.evaluate_route_edges", wraps=evaluate_route_edges) as evaluator:
            self.oracle.route_energy_summary(["e01"], self.multipliers, self.profile, payload_mass_kg=0.0)
            self.oracle.route_energy_summary(["e01"], self.multipliers, self.profile, payload_mass_kg=75.0)
            alternate_profile = EnergyModelConfig(auxiliary_power_w=900.0)
            self.oracle.route_energy_summary(["e01"], self.multipliers, alternate_profile, payload_mass_kg=0.0)
            alternate_multipliers = dict(self.multipliers)
            alternate_multipliers[1] = 0.7
            self.oracle.route_energy_summary(["e01"], alternate_multipliers, self.profile, payload_mass_kg=0.0)

        self.assertEqual(evaluator.call_count, 4)


if __name__ == "__main__":
    unittest.main()
