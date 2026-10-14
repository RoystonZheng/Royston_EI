import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from route_experiment.mapgen import generate_map
from route_experiment.models import MapConfig
from route_experiment.scenario import (
    generate_feature_showcase_scenarios,
    generate_formal_scenario_batch,
    generate_formal_scenario_set,
)


class FeatureScenarioTests(unittest.TestCase):
    def test_showcase_scenarios_expose_graph_only_vs_agent_inputs(self):
        road_map = generate_map(seed=0, size=100, config=MapConfig(size=100))

        scenarios = generate_feature_showcase_scenarios(road_map)

        self.assertGreaterEqual(len(scenarios), 2)
        for scenario in scenarios:
            self.assertEqual(scenario.baseline_visibility, "graph_only")
            self.assertEqual(
                scenario.agent_visibility, "road_feature_input_protocol_v1"
            )
            self.assertEqual(len(scenario.pickup_candidate_node_ids), 2)
            self.assertNotEqual(
                scenario.baseline_selected_node_id,
                scenario.agent_selected_node_id,
            )
            self.assertTrue(scenario.feature_ids)
            self.assertTrue(scenario.semantic_query)
            self.assertTrue(scenario.expected_route_effect)

    def test_formal_scenario_set_covers_required_case_types(self):
        road_map = generate_map(seed=0, size=100, config=MapConfig(size=100))

        scenario_set = generate_formal_scenario_set(road_map, seed=0)

        self.assertTrue(scenario_set.coverage.feasible_case)
        self.assertTrue(scenario_set.coverage.boundary_case)
        self.assertTrue(scenario_set.coverage.detour_case)
        self.assertTrue(scenario_set.coverage.unreachable_case)
        self.assertTrue(scenario_set.coverage.illegal_endpoint_case)
        self.assertGreater(scenario_set.coverage.legal_pickup_count, 0)
        self.assertGreater(scenario_set.coverage.legal_dropoff_count, 0)

        task_types = {
            task.task_type
            for scenario in scenario_set.scenarios
            for task in scenario.tasks
        }
        self.assertTrue(
            {
                "feasible_exact",
                "boundary_exact",
                "illegal_endpoint",
                "detour_exact",
                "unreachable_exact",
            }.issubset(task_types)
        )

        nodes = {node.node_id: node for node in scenario_set.road_map.nodes}
        boundary_task = next(
            task
            for scenario in scenario_set.scenarios
            for task in scenario.tasks
            if task.task_type == "boundary_exact"
        )
        boundary_nodes = [
            nodes[boundary_task.start_node_id],
            nodes[boundary_task.true_pickup_node_id],
            nodes[boundary_task.true_dropoff_node_id],
        ]
        self.assertTrue(
            any(
                node.x in (0, 99) or node.y in (0, 99)
                for node in boundary_nodes
            )
        )

        illegal_task = next(
            task
            for scenario in scenario_set.scenarios
            for task in scenario.tasks
            if task.task_type == "illegal_endpoint"
        )
        self.assertTrue(
            not nodes[illegal_task.true_pickup_node_id].legal_pickup
            or not nodes[illegal_task.true_dropoff_node_id].legal_dropoff
        )

        detour_details = scenario_set.metadata["case_details"]["detour_exact"]
        self.assertGreater(
            detour_details["shortest_distance_m"],
            detour_details["geometric_lower_bound_m"],
        )

        unreachable_scenario = next(
            scenario
            for scenario in scenario_set.scenarios
            if scenario.scenario_type == "unreachable"
        )
        unreachable_task = unreachable_scenario.tasks[0]
        blocked = set(unreachable_scenario.blocked_edge_ids)
        self.assertTrue(blocked)
        adjacency = {}
        for edge in scenario_set.road_map.edges:
            if edge.edge_id in blocked:
                continue
            adjacency.setdefault(edge.from_node, []).append(edge.to_node)
        pending = [unreachable_task.start_node_id]
        seen = set(pending)
        while pending:
            current = pending.pop()
            for neighbor in adjacency.get(current, []):
                if neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
        self.assertNotIn(unreachable_task.true_dropoff_node_id, seen)

    def test_formal_scenario_batch_accepts_multiple_seeds_without_fixed_count(self):
        scenario_sets = generate_formal_scenario_batch(
            seeds=[0, 1], size=20, config=MapConfig(size=20)
        )

        self.assertEqual(len(scenario_sets), 2)
        self.assertEqual(
            [scenario_set.random_seed for scenario_set in scenario_sets],
            [0, 1],
        )
        self.assertEqual(
            len({scenario_set.road_map.map_id for scenario_set in scenario_sets}),
            2,
        )
        for scenario_set in scenario_sets:
            self.assertTrue(scenario_set.coverage.boundary_case)
            self.assertTrue(scenario_set.coverage.detour_case)
            self.assertTrue(scenario_set.coverage.unreachable_case)

    def test_formal_scenario_cli_writes_one_bundle_per_seed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "formal_scenarios"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/generate_formal_scenarios.py",
                    "--size",
                    "20",
                    "--seed-start",
                    "0",
                    "--seed-count",
                    "2",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["seeds"], [0, 1])
            self.assertEqual(len(manifest["bundles"]), 2)
            self.assertIn("formal-scenario-set-0000", result.stdout)
            for bundle in manifest["bundles"]:
                self.assertTrue((output_dir / bundle["map_file"]).exists())
                self.assertTrue((output_dir / bundle["order_file"]).exists())
                self.assertTrue((output_dir / bundle["scenario_file"]).exists())
                order_payload = json.loads(
                    (output_dir / bundle["order_file"]).read_text(encoding="utf-8")
                )
                self.assertEqual(order_payload["task_count"], 10)


if __name__ == "__main__":
    unittest.main()
