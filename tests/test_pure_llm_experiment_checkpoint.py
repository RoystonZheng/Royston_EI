import unittest
import json
import tempfile
from pathlib import Path

from route_experiment.models import PriceConfig, ResourceUsage, RouteMethodResult, RoadMap, VehicleOrderTask
from scripts.run_pure_llm_experiment import (
    _write_index,
    _run_seed,
    _task_results_payload,
    _tasks_requiring_attempt,
)


class _RecordedResult:
    def __init__(self, task_id):
        self.task_id = task_id

    def to_dict(self):
        return {"task_id": self.task_id, "status": "failed"}


class PureLLMExperimentCheckpointTests(unittest.TestCase):
    def test_experiment_index_links_original_map_and_seed_dashboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "failure_energy_audit.json").write_text("{}", encoding="utf-8")
            _write_index(output_dir, [{
                "seed": 0,
                "seed_dashboard_index": "seeds/seed_0/index.html",
                "order_visual_index": "seeds/seed_0/visualizations/orders/index.html",
                "formal_visual_index": "seeds/seed_0/visualizations/formal_scenarios/index.html",
                "order_trace_count": 10,
                "formal_trace_count": 5,
            }])
            page = (output_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("seeds/seed_0/index.html", page)
            self.assertIn("seeds/seed_0/map_preview.html", page)
            self.assertNotIn("failure_energy_audit.json", page)

    def test_payload_can_checkpoint_a_partial_order_batch(self):
        road_map = RoadMap(
            map_id="grid-2x2-seed-0",
            version="v1",
            size=2,
            nodes=[],
            edges=[],
            boundary={"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1},
        )
        tasks = [
            VehicleOrderTask(
                task_id="task-1",
                vehicle_id="vehicle-1",
                order_id="order-1",
                start_node_id="node-0-0",
                pickup_node_id="node-1-0",
                dropoff_node_id="node-1-1",
            ),
            VehicleOrderTask(
                task_id="task-2",
                vehicle_id="vehicle-2",
                order_id="order-2",
                start_node_id="node-0-1",
                pickup_node_id="node-1-1",
                dropoff_node_id="node-0-0",
            ),
        ]

        payload = _task_results_payload(
            road_map,
            tasks,
            {"task-1": _RecordedResult("task-1")},
            {},
            "order_batch",
            0,
        )

        self.assertEqual([item["task_id"] for item in payload["results"]], ["task-1"])

    def test_resume_retries_infrastructure_errors_and_missing_but_keeps_model_results(self):
        tasks = [
            VehicleOrderTask("task-1", "vehicle-1", "order-1", "a", "b", "c"),
            VehicleOrderTask("task-2", "vehicle-2", "order-2", "a", "b", "c"),
            VehicleOrderTask("task-3", "vehicle-3", "order-3", "a", "b", "c"),
            VehicleOrderTask("task-4", "vehicle-4", "order-4", "a", "b", "c"),
        ]
        prior_records = {
            "task-1": {"status": "completed", "valid_route_count": 1},
            "task-2": {
                "status": "failed",
                "valid_route_count": 0,
                "failure_reason": "llm_route_invalid",
                "resources": {"llm_calls": 1},
            },
            "task-3": {
                "status": "failed",
                "valid_route_count": 0,
                "failure_reason": "deepseek_request_failed:deepseek_http_status:400",
                "resources": {"llm_calls": 1},
            },
        }

        remaining = _tasks_requiring_attempt(tasks, prior_records)

        self.assertEqual([task.task_id for task in remaining], ["task-3", "task-4"])

    def test_output_limit_hit_is_checkpointed_and_batch_continues(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            seed_dir = output_dir / "seeds" / "seed_0"
            routes_path = seed_dir / "pure_llm_order_routes_costs_seed_0.json"

            class LimitedPlanner:
                client = type("Client", (), {"max_tokens": 1})()
                calls = 0

                def plan(self, road_map, task, blocked_edge_ids=None):
                    self.calls += 1
                    return RouteMethodResult(
                        task_id=task.task_id,
                        method_name="pure_llm_deepseek",
                        status="failed",
                        failure_reason="llm_output_parse_failed",
                        resources=ResourceUsage(llm_calls=1, output_tokens=1),
                        raw_output={"content": "truncated"},
                    )

            planner = LimitedPlanner()
            _run_seed(
                seed=0,
                size=20,
                output_dir=output_dir,
                planner=planner,
                price_config=PriceConfig(),
                price_profile={},
                limit_tasks=2,
                limit_formal=0,
            )

            self.assertEqual(planner.calls, 2)
            payload = json.loads(routes_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["results"]), 2)
            for item in payload["results"]:
                method = item["methods"][0]
                self.assertTrue(method["metadata"]["output_token_limit_reached"])

    def test_each_route_result_is_saved_before_the_next_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            seed_dir = output_dir / "seeds" / "seed_0"
            routes_path = seed_dir / "pure_llm_order_routes_costs_seed_0.json"

            class ObservingPlanner:
                calls = 0

                def plan(self, road_map, task, blocked_edge_ids=None):
                    if self.calls == 0:
                        self.assert_checkpoint_count(0)
                    else:
                        self.assert_checkpoint_count(1)
                    self.calls += 1
                    return RouteMethodResult(
                        task_id=task.task_id,
                        method_name="pure_llm_deepseek",
                        status="failed",
                        failure_reason="test_route_failure",
                        resources=ResourceUsage(llm_calls=1),
                        raw_output={"status": "failed", "reason": "test"},
                    )

                def assert_checkpoint_count(self, expected):
                    self_outer.assertTrue(routes_path.exists())
                    payload = json.loads(routes_path.read_text(encoding="utf-8"))
                    self_outer.assertEqual(len(payload["results"]), expected)

            self_outer = self
            planner = ObservingPlanner()
            price_config = PriceConfig(
                api_price_per_token=0,
                compute_price_per_ms=0,
                electricity_price_per_kwh=0,
            )

            _run_seed(
                seed=0,
                size=20,
                output_dir=output_dir,
                planner=planner,
                price_config=price_config,
                price_profile={},
                limit_tasks=2,
                limit_formal=0,
            )

            self.assertEqual(planner.calls, 2)
            payload = json.loads(routes_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["results"]), 2)


if __name__ == "__main__":
    unittest.main()
