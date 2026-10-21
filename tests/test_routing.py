import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from route_experiment.mapgen import generate_map
from route_experiment.models import Edge, MapConfig, PackageConfig
from route_experiment.routing import (
    build_route_input_package,
    directed_path_exists,
    route_chain_reachability,
    run_traditional_route,
)
from route_experiment.scenario import generate_formal_scenario_set
from route_experiment.taskgen import TaskGenerationConfig, generate_order_tasks


class TraditionalRoutingTests(unittest.TestCase):
    def setUp(self):
        self.road_map = generate_map(seed=0, size=20, config=MapConfig(size=20))
        self.batch = generate_order_tasks(
            self.road_map,
            seed=0,
            config=TaskGenerationConfig(task_count=10, exact_ratio=0.7),
        )

    def test_route_chain_preflight_checks_directional_reachability_only(self):
        edges = [
            Edge("s-p", "start", "pickup", 100.0, 10.0, one_way=True),
            Edge("p-d", "pickup", "dropoff", 100.0, 10.0, one_way=True),
        ]

        self.assertEqual(
            route_chain_reachability(edges, "start", "pickup", "dropoff"),
            {
                "start_to_pickup": True,
                "pickup_to_dropoff": True,
                "chain_reachable": True,
            },
        )
        self.assertTrue(directed_path_exists(edges, "start", "dropoff"))
        self.assertFalse(directed_path_exists(edges, "dropoff", "start"))

    def test_full_and_local_traditional_methods_find_same_exact_route(self):
        task = next(task for task in self.batch.tasks if task.task_type == "exact_node")

        results = [
            run_traditional_route(self.road_map, task, "full_map_dijkstra"),
            run_traditional_route(self.road_map, task, "local_dijkstra"),
            run_traditional_route(self.road_map, task, "local_a_star"),
        ]

        for result in results:
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.valid_route_count, 1)
            self.assertEqual(len(result.routes), 1)
            route = result.routes[0]
            self.assertTrue(route.route_reachability)
            self.assertEqual(route.node_sequence[0], task.start_node_id)
            self.assertEqual(route.node_sequence[-1], task.dropoff_node_id)
            self.assertEqual(route.validation_status, "valid")
            self.assertGreater(result.metadata["expanded_nodes"], 0)

        self.assertAlmostEqual(
            results[0].routes[0].route_distance_m,
            results[1].routes[0].route_distance_m,
        )
        self.assertAlmostEqual(
            results[1].routes[0].route_duration_sec,
            results[2].routes[0].route_duration_sec,
        )

    def test_traditional_result_keeps_search_and_route_trace(self):
        task = next(task for task in self.batch.tasks if task.task_type == "exact_node")

        result = run_traditional_route(self.road_map, task, "local_a_star")

        trace = result.metadata["search_trace"]
        self.assertEqual(trace["schema_version"], "route_search_trace_v1")
        self.assertEqual(trace["algorithm"], "a_star")
        self.assertEqual(len(trace["legs"]), 2)
        self.assertGreater(len(trace["legs"][0]["expanded_node_ids"]), 0)
        self.assertGreater(len(trace["legs"][1]["expanded_node_ids"]), 0)
        self.assertEqual(
            trace["route_node_sequence"], result.routes[0].node_sequence
        )
        self.assertEqual(
            trace["route_edge_sequence"], result.routes[0].edge_sequence
        )
        self.assertEqual(
            trace["expanded_nodes"], result.metadata["expanded_nodes"]
        )

    def test_local_package_contains_only_the_expanded_map_region(self):
        task = next(task for task in self.batch.tasks if task.task_type == "exact_node")

        package = build_route_input_package(
            self.road_map,
            task,
            config=PackageConfig(initial_margin=2, expansion_step=2, max_expansions=2),
        )

        self.assertEqual(package.map_id, self.road_map.map_id)
        self.assertIn(task.start_node_id, {node.node_id for node in package.nodes})
        self.assertIn(task.pickup_node_id, {node.node_id for node in package.nodes})
        self.assertIn(task.dropoff_node_id, {node.node_id for node in package.nodes})
        self.assertGreaterEqual(package.expansion_count, 0)
        self.assertTrue(package.serialized_token_estimate > 0)

    def test_semantic_tasks_use_graph_only_resolution_for_traditional_baseline(self):
        semantic_tasks = [
            task for task in self.batch.tasks if task.task_type != "exact_node"
        ]

        for task in semantic_tasks:
            result = run_traditional_route(
                self.road_map, task, "full_map_dijkstra"
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.valid_route_count, 1)
            self.assertEqual(
                result.metadata["semantic_resolution"]["baseline_visibility"],
                "graph_only",
            )
            resolution = result.metadata["semantic_resolution"]
            self.assertEqual(resolution["semantic_resolution_method"], "graph_only")
            self.assertIn(
                resolution["semantic_resolution_status"], {"resolved", "fallback"}
            )
            self.assertFalse(resolution["semantic_reads_roadside_features"])
            self.assertFalse(resolution["semantic_uses_evaluator_truth"])
            self.assertTrue(result.metadata["semantic_resolution"]["resolved"])
            self.assertFalse(result.validation.pickup_exact_match)
            self.assertFalse(result.validation.dropoff_exact_match)

    def test_formal_illegal_and_unreachable_cases_are_recorded_as_failures(self):
        scenario_set = generate_formal_scenario_set(self.road_map, seed=0)

        illegal = next(
            scenario for scenario in scenario_set.scenarios
            if scenario.scenario_type == "illegal_endpoint"
        )
        illegal_result = run_traditional_route(
            self.road_map,
            illegal.tasks[0],
            "full_map_dijkstra",
            blocked_edge_ids=illegal.blocked_edge_ids,
        )
        self.assertEqual(illegal_result.status, "failed")
        self.assertIn(illegal_result.failure_reason, {
            "illegal_pickup_endpoint",
            "illegal_dropoff_endpoint",
        })

        unreachable = next(
            scenario for scenario in scenario_set.scenarios
            if scenario.scenario_type == "unreachable"
        )
        unreachable_result = run_traditional_route(
            self.road_map,
            unreachable.tasks[0],
            "full_map_dijkstra",
            blocked_edge_ids=unreachable.blocked_edge_ids,
        )
        self.assertEqual(unreachable_result.status, "failed")
        self.assertEqual(unreachable_result.failure_reason, "unreachable")

    def test_traditional_route_cli_processes_order_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            map_path = temp_path / "map.json"
            order_path = temp_path / "orders.json"
            result_path = temp_path / "routes.json"
            map_path.write_text(
                json.dumps(self.road_map.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )
            order_path.write_text(
                json.dumps(self.batch.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    "scripts/run_traditional_routes.py",
                    "--map-json",
                    str(map_path),
                    "--order-json",
                    str(order_path),
                    "--output-json",
                    str(result_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["task_count"], 10)
            self.assertEqual(payload["semantic_policy"]["resolver"], "graph_only")
            self.assertFalse(payload["semantic_policy"]["reads_roadside_features"])
            self.assertFalse(payload["semantic_policy"]["uses_evaluator_truth"])
            self.assertTrue(payload["semantic_policy"]["semantic_tasks_remain_routable"])
            self.assertEqual(
                set(payload["methods"]),
                {"full_map_dijkstra", "local_dijkstra", "local_a_star"},
            )
            self.assertEqual(len(payload["results"]), 10)
            semantic_results = [
                item for item in payload["results"]
                if item["task_type"] != "exact_node"
            ]
            self.assertEqual(len(semantic_results), 3)
            for semantic_result in semantic_results:
                for method_result in semantic_result["methods"]:
                    self.assertEqual(method_result["status"], "completed")
                    self.assertEqual(method_result["valid_route_count"], 1)
                    self.assertEqual(
                        method_result["metadata"]["semantic_resolution"][
                            "baseline_visibility"
                        ],
                        "graph_only",
                    )


if __name__ == "__main__":
    unittest.main()
