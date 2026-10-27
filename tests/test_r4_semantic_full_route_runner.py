import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from route_experiment.models import Edge, Node, PackageConfig, RoadMap, VehicleOrderTask
from route_experiment.routing import build_route_input_package, run_traditional_route
from scripts.run_r4_codex_semantic_full_route_pilot import (
    DEFAULT_PREFLIGHT_DIR,
    _load_current_inputs,
    finalize_proposal,
    run,
)
from scripts.prepare_r4_codex_semantic_full_route_pilot import (
    _find_no_revisit_reference_route,
    _route_nodes_to_moves,
)
from scripts.run_r4_codex_formal_checkpoint import load_map
from route_experiment.r4_compact_route import validate_r4_compact_route_proposal


class R4SemanticFullRouteRunnerTests(unittest.TestCase):
    def setUp(self):
        self.road_map = RoadMap(
            map_id="semantic-pilot-runner-test",
            version="v1",
            size=2,
            nodes=[
                Node("node_0_0", 0, 0),
                Node("node_1_0", 1, 0),
                Node("node_0_1", 0, 1, legal_pickup=True),
                Node("node_1_1", 1, 1, legal_dropoff=True),
            ],
            edges=[
                Edge("edge_north", "node_0_0", "node_0_1", 100.0, 10.0),
                Edge("edge_east", "node_0_1", "node_1_1", 100.0, 10.0),
                Edge("edge_back", "node_0_1", "node_0_0", 100.0, 10.0),
            ],
            boundary={"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1},
        )
        self.task = VehicleOrderTask(
            task_id="runner-test-task",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
        )

    def test_finalize_scores_only_a_valid_complete_route(self):
        result = finalize_proposal(
            self.road_map,
            self.task,
            {
                "status": "completed",
                "move_sequence": "NE",
                "step_check_sequence": "11",
                "failure_reason_code": "none",
            },
        )

        self.assertTrue(result["validation_audit"]["accepted"])
        self.assertEqual(result["method_result"]["status"], "completed")
        self.assertGreater(result["method_result"]["resources"]["energy_kwh"], 0)
        self.assertIsNone(result["method_result"]["metadata"]["proposal_energy_audit"])

    def test_finalize_rejects_cross_leg_revisit_and_records_proposal_energy(self):
        result = finalize_proposal(
            self.road_map,
            self.task,
            {
                "status": "completed",
                "move_sequence": "NSNE",
                "step_check_sequence": "1111",
                "failure_reason_code": "none",
            },
        )

        method = result["method_result"]
        self.assertFalse(result["validation_audit"]["accepted"])
        self.assertEqual(method["failure_reason"], "llm_route_invalid")
        self.assertIn("node_revisited", method["validation"]["errors"])
        self.assertIsNotNone(method["metadata"]["proposal_energy_audit"])
        self.assertIsNone(method["resources"]["energy_kwh"])

    def test_seed0_task9_has_a_no_revisit_reference_witness(self):
        seed_dir = Path(__file__).resolve().parents[1] / "outputs/route_layer/traditional_experiment_v1/seeds/seed_0"
        road_map = load_map(seed_dir / "map_100x100_seed_0.json")
        task = VehicleOrderTask(
            task_id="task-0000-0009",
            vehicle_id="pilot",
            order_id="pilot",
            start_node_id="node_69_41",
            pickup_node_id="node_25_66",
            dropoff_node_id="node_42_43",
            task_type="semantic_place",
        )
        package = build_route_input_package(road_map, task, PackageConfig())

        route = _find_no_revisit_reference_route(
            package, task.start_node_id, task.pickup_node_id, task.dropoff_node_id
        )

        self.assertIsNotNone(route)
        self.assertEqual(len(route.node_sequence), len(set(route.node_sequence)))
        self.assertEqual(route.node_sequence[0], task.start_node_id)
        self.assertIn(task.pickup_node_id, route.node_sequence)
        self.assertEqual(route.node_sequence[-1], task.dropoff_node_id)
        validation = validate_r4_compact_route_proposal(
            road_map,
            start_node_id=task.start_node_id,
            pickup_node_id=task.pickup_node_id,
            dropoff_node_id=task.dropoff_node_id,
            blocked_edge_ids=package.blocked_edge_ids,
            payload={
                "status": "completed",
                "move_sequence": _route_nodes_to_moves(route.node_sequence),
                "step_check_sequence": "1" * len(route.edge_sequence),
                "failure_reason_code": "none",
            },
        )
        self.assertTrue(validation["accepted"])

    def test_default_runner_path_does_not_construct_a_model_client(self):
        def forbidden_client(**kwargs):
            raise AssertionError("offline run must not construct a model client")

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "pilot"
            result = run(
                preflight_dir=DEFAULT_PREFLIGHT_DIR,
                output_dir=output_dir,
                send_one_request=False,
                client_factory=forbidden_client,
            )

        self.assertFalse(result["model_request_sent"])
        self.assertEqual(result["retry_count"], 0)

    def test_one_attempt_fixture_renders_a_complete_auditable_result(self):
        item = _load_current_inputs(DEFAULT_PREFLIGHT_DIR)
        reference = run_traditional_route(
            item["road_map"], item["task"], "local_dijkstra", PackageConfig()
        )
        route = reference.routes[0]
        payload = {
            "status": "completed",
            "move_sequence": _route_nodes_to_moves(route.node_sequence),
            "step_check_sequence": "1" * len(route.edge_sequence),
            "failure_reason_code": "none",
        }

        class FixtureClient:
            last_record = {"status": "accepted", "backend": "test_fixture", "call_index": 1}

            def __init__(self, **kwargs):
                self.route_constraint = kwargs["route_constraint"]

            def chat(self, messages):
                self.assertions = messages
                return SimpleNamespace(
                    content=json.dumps(payload),
                    provider_audit=dict(self.last_record),
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    latency_ms=0.0,
                )

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "fixture-run"
            result = run(
                preflight_dir=DEFAULT_PREFLIGHT_DIR,
                output_dir=output_dir,
                send_one_request=True,
                client_factory=FixtureClient,
            )
            self.assertEqual(result["route_status"], "completed")
            self.assertEqual(result["model_request_calls_observed"], 1)
            self.assertTrue((output_dir / "attempt_01.json").is_file())
            self.assertTrue((output_dir / "result.json").is_file())
            replay_index = output_dir / result["replay_index"]
            self.assertTrue(replay_index.is_file())
            seed_dashboard = output_dir / result["seed_dashboard"]
            self.assertTrue(seed_dashboard.is_file())
            self.assertTrue((seed_dashboard.parent / "map_preview.html").is_file())


if __name__ == "__main__":
    unittest.main()
