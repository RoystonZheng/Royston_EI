import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from route_experiment.mapgen import generate_map
from route_experiment.models import MapConfig
from route_experiment.taskgen import TaskGenerationConfig, generate_order_tasks


class OrderTaskGenerationTests(unittest.TestCase):
    def test_generates_reproducible_seventy_thirty_batch_without_mutating_map(self):
        road_map = generate_map(seed=0, size=100, config=MapConfig(size=100))
        map_before = road_map.to_dict()
        config = TaskGenerationConfig(task_count=10, exact_ratio=0.7)

        first = generate_order_tasks(road_map, seed=42, config=config)
        second = generate_order_tasks(road_map, seed=42, config=config)

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.task_count, 10)
        self.assertEqual(first.exact_task_count, 7)
        self.assertEqual(first.semantic_task_count, 3)
        self.assertEqual(first.semantic_type_counts, {
            "semantic_landmark_nearby": 1,
            "semantic_landmark_relative": 1,
            "semantic_road_feature_position": 1,
        })
        self.assertEqual(road_map.to_dict(), map_before)
        self.assertEqual(len({task.task_id for task in first.tasks}), 10)
        self.assertEqual(len({task.order_id for task in first.tasks}), 10)

        legal_pickups = {
            node.node_id for node in road_map.nodes if node.legal_pickup
        }
        legal_dropoffs = {
            node.node_id for node in road_map.nodes if node.legal_dropoff
        }
        for task in first.tasks:
            self.assertTrue(task.start_node_id)
            self.assertIn(task.true_pickup_node_id, legal_pickups)
            self.assertIn(task.true_dropoff_node_id, legal_dropoffs)
            if task.task_type == "exact_node":
                self.assertIn(task.pickup_node_id, legal_pickups)
                self.assertIn(task.dropoff_node_id, legal_dropoffs)
                self.assertIsNone(task.pickup_text)
                self.assertIsNone(task.dropoff_text)
            else:
                self.assertIsNone(task.pickup_node_id)
                self.assertIsNone(task.dropoff_node_id)
                self.assertTrue(task.pickup_text)
                self.assertTrue(task.dropoff_text)
                self.assertNotIn("node_", task.pickup_text)
                self.assertNotIn("node_", task.dropoff_text)

    def test_semantic_tasks_cover_all_configured_location_forms(self):
        road_map = generate_map(seed=0, size=100, config=MapConfig(size=100))

        batch = generate_order_tasks(
            road_map,
            seed=7,
            config=TaskGenerationConfig(task_count=12, exact_ratio=0.75),
        )

        semantic_types = {
            task.task_type for task in batch.tasks if task.task_type != "exact_node"
        }
        self.assertEqual(
            semantic_types,
            {
                "semantic_landmark_nearby",
                "semantic_landmark_relative",
                "semantic_road_feature_position",
            },
        )
        self.assertEqual(batch.exact_task_count, 9)
        self.assertEqual(batch.semantic_task_count, 3)

    def test_cli_reads_map_json_and_writes_tasks_outside_map_directory(self):
        road_map = generate_map(seed=0, size=20, config=MapConfig(size=20))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            map_path = temp_path / "map.json"
            output_path = temp_path / "tasks" / "order_tasks.json"
            map_path.write_text(
                json.dumps(road_map.to_dict(), ensure_ascii=False), encoding="utf-8"
            )

            subprocess.check_call(
                [
                    sys.executable,
                    "scripts/generate_order_tasks.py",
                    str(map_path),
                    str(output_path),
                    "--seed",
                    "5",
                    "--task-count",
                    "10",
                ]
            )

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["map_id"], road_map.map_id)
            self.assertEqual(payload["random_seed"], 5)
            self.assertEqual(payload["task_count"], 10)
            self.assertEqual(len(payload["tasks"]), 10)
            semantic_task = next(
                task for task in payload["tasks"] if task["task_type"] != "exact_node"
            )
            self.assertNotIn("true_pickup_node_id", semantic_task)
            self.assertNotIn("true_dropoff_node_id", semantic_task)
            self.assertEqual(len(payload["ground_truth"]), 10)
            self.assertTrue(payload["ground_truth"][0]["true_pickup_node_id"])


if __name__ == "__main__":
    unittest.main()
