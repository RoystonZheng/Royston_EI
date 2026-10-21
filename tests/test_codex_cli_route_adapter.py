import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.models import Edge, Node, RouteInputPackage, VehicleOrderTask
from route_experiment.r4_constraints import build_r4_route_output_constraint
from route_experiment.r4_stepwise import (
    build_r4_step_output_constraint,
    run_r4_stepwise_route_search,
)


class CodexCliRouteAdapterTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [
            Node("node_0_0", 0, 0),
            Node("node_0_1", 0, 1, legal_pickup=True),
            Node("node_1_1", 1, 1, legal_dropoff=True),
            Node("node_1_0", 1, 0),
        ]
        self.edges = [
            Edge("edge_north", "node_0_0", "node_0_1", 100.0, 10.0),
            Edge("edge_east", "node_0_1", "node_1_1", 100.0, 10.0),
        ]
        self.package = RouteInputPackage(
            package_id="codex-cli-test-package",
            map_id="codex-cli-test-map",
            map_version="v1",
            road_state_version="v1",
            random_seed=0,
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
            task_type="exact_node",
            pickup_text=None,
            dropoff_text=None,
            nodes=self.nodes,
            edges=self.edges,
            expansion_count=4,
            serialized_token_estimate=0,
            boundary={"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1},
        )
        self.road_map = SimpleNamespace(nodes=self.nodes, edges=self.edges)
        self.task = VehicleOrderTask(
            task_id="codex-cli-test-task",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
        )

    @staticmethod
    def _model_response(direction):
        return {
            "action": "move",
            "direction": direction,
            "self_check": {
                "allowed_directed_edge": True,
                "starts_at_current": True,
                "inside_boundary": True,
                "pickup_order_ok": True,
                "not_revisited": True,
            },
            "failure_reason_code": "none",
        }

    def _fake_cli(self, directions):
        calls = {"count": 0}

        def run(command, **kwargs):
            call_index = calls["count"]
            calls["count"] += 1
            self.assertIn(["--disable", "shell_tool"], [command[index:index + 2] for index in range(len(command) - 1)])
            self.assertIn(["--disable", "unified_exec"], [command[index:index + 2] for index in range(len(command) - 1)])
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn("--ignore-user-config", command)
            for name in (
                "DEEPSEEK_API_KEY",
                "LLM_API_KEY",
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "CODEX_API_KEY",
            ):
                self.assertNotIn(name, kwargs["env"])
            schema_path = Path(command[command.index("--output-schema") + 1])
            schema = json.loads(schema_path.read_text())
            allowed = schema["properties"]["direction"]["enum"]
            direction = directions[call_index]
            self.assertEqual(allowed, [direction])
            final_path = Path(command[command.index("--output-last-message") + 1])
            response_text = json.dumps(self._model_response(direction))
            final_path.write_text(response_text)
            events = [
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": response_text},
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 7,
                        "reasoning_output_tokens": 3,
                    },
                },
            ]
            return SimpleNamespace(
                returncode=0,
                stdout="".join(json.dumps(item) + "\n" for item in events),
                stderr="",
            )

        return calls, run

    def test_codex_cli_can_complete_locally_validated_stepwise_route_without_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            calls, fake_run = self._fake_cli(["N", "E"])
            client = CodexCliRouteStepClient(
                model="gpt-5.6-luna",
                trace_dir=Path(temp_dir) / "traces",
            )
            full_constraint = build_r4_route_output_constraint(
                self.package, "node_0_1", "node_1_1", allow_failed=False
            )
            client.route_constraint = build_r4_step_output_constraint(
                self.package,
                full_constraint,
                current_node_id="node_0_0",
                pickup_seen=False,
                visited_node_ids=["node_0_0"],
                remaining_action_budget=2,
            )
            with patch("route_experiment.codex_cli_adapter.subprocess.run", side_effect=fake_run):
                result = run_r4_stepwise_route_search(
                    client,
                    self.package,
                    self.road_map,
                    self.task,
                    "node_0_1",
                    "node_1_1",
                    max_api_calls=2,
                )

            self.assertEqual(result["status"], "route_validated")
            self.assertEqual(
                result["route"]["edge_sequence"], ["edge_north", "edge_east"]
            )
            self.assertEqual(result["usage"]["llm_calls"], 2)
            self.assertEqual(calls["count"], 2)
            self.assertTrue(all(item["status"] == "step_accepted" for item in result["attempts"]))
            self.assertTrue(all(item["retries"] == 0 for item in result["attempts"]))
            self.assertEqual(
                result["attempts"][0]["provider_audit"]["backend"], "codex_cli"
            )

            first_trace = Path(result["attempts"][0]["request_id"])
            second_trace = Path(result["attempts"][1]["request_id"])
            self.assertTrue(first_trace.is_file())
            self.assertTrue(second_trace.is_file())
            first_events = [json.loads(line) for line in first_trace.read_text().splitlines()]
            self.assertFalse(any(
                item.get("type") in CodexCliRouteStepClient.TOOL_ITEM_TYPES
                for event in first_events
                for item in [event.get("item", {})]
            ))

    def test_cli_tool_event_invalidates_model_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            constraint = SimpleNamespace(
                schema={"type": "object", "properties": {"direction": {"enum": ["N"]}}},
                schema_sha256="schema-hash",
                schema_bytes=64,
            )
            response_text = json.dumps(self._model_response("N"))

            def run(command, **kwargs):
                Path(command[command.index("--output-last-message") + 1]).write_text(response_text)
                events = [
                    {"type": "turn.started"},
                    {"type": "item.completed", "item": {"type": "command_execution", "command": "pwd"}},
                    {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
                ]
                return SimpleNamespace(
                    returncode=0,
                    stdout="".join(json.dumps(item) + "\n" for item in events),
                    stderr="",
                )

            client = CodexCliRouteStepClient(
                model="gpt-5.6-luna",
                trace_dir=Path(temp_dir) / "traces",
            )
            client.route_constraint = constraint
            with patch("route_experiment.codex_cli_adapter.subprocess.run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "codex_cli_tool_event_detected"):
                    client.chat([{"role": "user", "content": "choose N"}])

    def test_route_search_records_tool_rejection_in_attempt_trace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            response_text = json.dumps(self._model_response("N"))

            def run(command, **kwargs):
                Path(command[command.index("--output-last-message") + 1]).write_text(response_text)
                events = [
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {"type": "command_execution", "command": "pwd"},
                    },
                    {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
                ]
                return SimpleNamespace(
                    returncode=0,
                    stdout="".join(json.dumps(item) + "\n" for item in events),
                    stderr="",
                )

            client = CodexCliRouteStepClient(
                model="gpt-5.6-luna",
                trace_dir=Path(temp_dir) / "traces",
            )
            client.route_constraint = build_r4_step_output_constraint(
                self.package,
                build_r4_route_output_constraint(
                    self.package, "node_0_1", "node_1_1", allow_failed=False
                ),
                current_node_id="node_0_0",
                pickup_seen=False,
                visited_node_ids=["node_0_0"],
                remaining_action_budget=2,
            )
            with patch("route_experiment.codex_cli_adapter.subprocess.run", side_effect=run):
                result = run_r4_stepwise_route_search(
                    client,
                    self.package,
                    self.road_map,
                    self.task,
                    "node_0_1",
                    "node_1_1",
                    max_api_calls=1,
                )

            self.assertEqual(result["status"], "api_error")
            self.assertEqual(
                result["attempts"][0]["provider_audit"]["status"],
                "tool_use_invalidated_response",
            )
            self.assertEqual(
                result["attempts"][0]["provider_audit"]["event_audit"]["tool_item_types"],
                ["command_execution"],
            )
            self.assertEqual(result["usage"]["input_tokens"], 5)
            self.assertEqual(result["usage"]["output_tokens"], 2)


if __name__ == "__main__":
    unittest.main()
