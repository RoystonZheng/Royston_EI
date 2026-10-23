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
    build_r4_stepwise_messages,
    build_r4_step_output_constraint,
    run_r4_stepwise_route_search,
)


class CodexCliRouteAdapterTests(unittest.TestCase):
    TOOL_FEATURES = (
        "apps",
        "auth_elicitation",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "chronicle",
        "code_mode",
        "code_mode_host",
        "computer_use",
        "goals",
        "hooks",
        "image_generation",
        "in_app_browser",
        "memories",
        "multi_agent",
        "plugins",
        "remote_plugin",
        "shell_snapshot",
        "shell_tool",
        "skill_mcp_dependency_install",
        "skill_search",
        "tool_call_mcp_elicitation",
        "tool_suggest",
        "unified_exec",
        "workspace_dependencies",
    )

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
                "target_node_is_new": True,
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
            disabled = {
                command[index + 1]
                for index, token in enumerate(command[:-1])
                if token == "--disable"
            }
            self.assertTrue(set(self.TOOL_FEATURES).issubset(disabled))
            self.assertEqual(len(disabled), len(self.TOOL_FEATURES))
            self.assertEqual(
                sum(token == "--disable" for token in command), len(self.TOOL_FEATURES)
            )
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn("--ignore-user-config", command)
            self.assertIn(
                ["--config", "skills.include_instructions=false"],
                [command[index:index + 2] for index in range(len(command) - 1)],
            )
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
            self.assertEqual(client.call_count, 2)
            self.assertTrue(all(item["status"] == "step_accepted" for item in result["attempts"]))
            self.assertTrue(all(item["retries"] == 0 for item in result["attempts"]))
            self.assertEqual(
                result["attempts"][0]["provider_audit"]["backend"], "codex_cli"
            )
            self.assertEqual(
                result["attempts"][0]["provider_audit"]["tool_policy"]["disabled_features"],
                list(CodexCliRouteStepClient.DISABLED_TOOL_FEATURES),
            )
            self.assertEqual(
                result["attempts"][0]["provider_audit"]["tool_policy"]["skill_instructions"],
                "disabled_via_cli_config",
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

    def test_ephemeral_call_ignores_a_previous_session_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, fake_run = self._fake_cli(["N"])
            client = CodexCliRouteStepClient(
                model="gpt-5.6-luna",
                trace_dir=Path(temp_dir) / "traces",
                route_constraint=SimpleNamespace(
                    schema={"type": "object", "properties": {"direction": {"enum": ["N"]}}},
                    schema_sha256="schema-hash",
                    schema_bytes=64,
                ),
                persist_session=False,
                session_id="stale-session-id",
            )

            def assert_fresh_ephemeral_exec(command, **kwargs):
                self.assertEqual(command[command.index("exec") + 1], "--json")
                self.assertNotIn("resume", command)
                self.assertIn("--ephemeral", command)
                return fake_run(command, **kwargs)

            with patch(
                "route_experiment.codex_cli_adapter.subprocess.run",
                side_effect=assert_fresh_ephemeral_exec,
            ):
                response = client.chat([{"role": "user", "content": "one route step"}])

        self.assertEqual(json.loads(response.content)["direction"], "N")
        self.assertFalse(client.persist_session)
        self.assertIsNone(client.session_id)

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
                    stderr=(
                        "Authorization: Bearer abcdef1234567890\n"
                        "api_key=sk-sensitive-secret-1234567890"
                    ),
                )

            client = CodexCliRouteStepClient(
                model="gpt-5.6-luna",
                trace_dir=Path(temp_dir) / "traces",
            )
            client.route_constraint = constraint
            with patch("route_experiment.codex_cli_adapter.subprocess.run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "codex_cli_tool_event_detected"):
                    client.chat([{"role": "user", "content": "choose N"}])
            self.assertNotIn("abcdef1234567890", client.last_record["stderr_tail"])
            self.assertNotIn("sk-sensitive-secret-1234567890", client.last_record["stderr_tail"])

    def test_new_client_resumes_trace_sequence_without_overwriting_prior_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_dir = Path(temp_dir) / "traces"
            calls, fake_run = self._fake_cli(["N", "N"])
            constraint = SimpleNamespace(
                schema={"type": "object", "properties": {"direction": {"enum": ["N"]}}},
                schema_sha256="schema-hash",
                schema_bytes=64,
            )
            first_client = CodexCliRouteStepClient(
                model="gpt-5.6-luna",
                trace_dir=trace_dir,
            )
            first_client.route_constraint = constraint
            second_client = CodexCliRouteStepClient(
                model="gpt-5.6-luna",
                trace_dir=trace_dir,
            )
            second_client.route_constraint = constraint

            with patch("route_experiment.codex_cli_adapter.subprocess.run", side_effect=fake_run):
                first_result = first_client.chat([{"role": "user", "content": "choose N"}])
                second_result = second_client.chat([{"role": "user", "content": "choose N again"}])

            self.assertEqual(calls["count"], 2)
            self.assertTrue(first_result.request_id.endswith("r4_step_0001.jsonl"))
            self.assertTrue(second_result.request_id.endswith("r4_step_0002.jsonl"))
            self.assertTrue((trace_dir / "r4_step_0001.prompt.txt").is_file())
            self.assertTrue((trace_dir / "r4_step_0002.prompt.txt").is_file())

    def test_codex_prompt_uses_json_contract_instead_of_disabled_tool_contract(self):
        messages = build_r4_stepwise_messages(
            self.package,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            visited_node_ids=[self.task.start_node_id],
        )
        rendered = CodexCliRouteStepClient._render_prompt(messages)
        self.assertIn("JSON Schema 的 direction 候选", rendered)
        self.assertIn("仅返回符合 JSON Schema 的 JSON 对象", rendered)
        self.assertNotIn("submit_r4_route 工具参数", rendered)
        self.assertIn("Do not call tools", rendered)

    def test_persistent_session_reuses_thread_and_sends_only_current_state_after_first_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            calls = []
            constraint = SimpleNamespace(
                schema={
                    "type": "object",
                    "properties": {"direction": {"type": "string", "enum": ["N"]}},
                    "required": ["direction"],
                    "additionalProperties": False,
                },
                schema_sha256="schema-hash",
                schema_bytes=96,
            )
            response_text = json.dumps(self._model_response("N"))
            initial_messages = [
                {"role": "system", "content": "Keep the map and routing rules for this task."},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "m": {"g": ["a" * 4000 for _ in range(8)]},
                            "q": {"s": "node_0_0", "p": "node_0_1", "d": "node_1_1"},
                            "s": {"n": "node_0_0", "v": ["node_0_0"], "p": False, "b": 2},
                        }
                    ),
                },
            ]

            def run(command, **kwargs):
                calls.append((list(command), kwargs["input"]))
                self.assertNotIn("--ephemeral", command)
                self.assertIn("--ignore-user-config", command)
                self.assertTrue(
                    set(self.TOOL_FEATURES).issubset(
                        {
                            command[index + 1]
                            for index, token in enumerate(command[:-1])
                            if token == "--disable"
                        }
                    )
                )
                schema_path = Path(command[command.index("--output-schema") + 1])
                self.assertEqual(json.loads(schema_path.read_text())["properties"]["direction"]["enum"], ["N"])
                final_path = Path(command[command.index("--output-last-message") + 1])
                final_path.write_text(response_text)
                events = [
                    {"type": "thread.started", "thread_id": "codex-session-123"},
                    {"type": "turn.started"},
                    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}},
                ]
                return SimpleNamespace(
                    returncode=0,
                    stdout="".join(json.dumps(item) + "\n" for item in events),
                    stderr="",
                )

            client = CodexCliRouteStepClient(
                model="gpt-5.6-luna",
                trace_dir=Path(temp_dir) / "traces",
                persist_session=True,
            )
            client.route_constraint = constraint
            with patch("route_experiment.codex_cli_adapter.subprocess.run", side_effect=run):
                first = client.chat(initial_messages)
                next_messages = [
                    {"role": "system", "content": "Keep the map and routing rules for this task."},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "m": {"g": ["b" * 4000 for _ in range(8)]},
                                "q": {"s": "node_0_0", "p": "node_0_1", "d": "node_1_1"},
                                "s": {"n": "node_0_1", "v": ["node_0_0", "node_0_1"], "p": True, "b": 1},
                            }
                        ),
                    },
                ]
                second = client.chat(next_messages)

            self.assertEqual(first.session_id, "codex-session-123")
            self.assertEqual(second.session_id, "codex-session-123")
            self.assertEqual(calls[0][0][calls[0][0].index("exec") + 1], "--json")
            self.assertEqual(calls[1][0][calls[1][0].index("exec") + 1], "resume")
            self.assertIn("codex-session-123", calls[1][0])
            self.assertIn("node_0_1", calls[1][1])
            self.assertNotIn('"m"', calls[1][1])
            self.assertLess(len(calls[1][1]), 1000)

    def test_nonempty_persistent_workspace_is_rejected_before_model_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workdir = root / "workspace"
            workdir.mkdir()
            (workdir / "unexpected.txt").write_text("occupied", encoding="utf-8")
            client = CodexCliRouteStepClient(
                model="gpt-5.6-luna",
                trace_dir=root / "traces",
                persist_session=True,
                working_directory=workdir,
                route_constraint=build_r4_step_output_constraint(
                    self.package,
                    build_r4_route_output_constraint(
                        self.package, "node_0_1", "node_1_1", allow_failed=False
                    ),
                    current_node_id="node_0_0",
                    pickup_seen=False,
                    visited_node_ids=["node_0_0"],
                    remaining_action_budget=2,
                ),
            )
            with patch("route_experiment.codex_cli_adapter.subprocess.run") as run:
                with self.assertRaisesRegex(
                    RuntimeError, "codex_cli_persistent_working_directory_not_empty"
                ):
                    client.chat([{"role": "user", "content": "{}"}])

        run.assert_not_called()
        self.assertEqual(client.last_record["status"], "not_called")
        self.assertEqual(
            client.last_record["stop_reason"], "persistent_working_directory_not_empty"
        )

    def test_unknown_cli_item_type_is_fail_closed_as_tool_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            response_text = json.dumps(self._model_response("N"))

            def run(command, **kwargs):
                Path(command[command.index("--output-last-message") + 1]).write_text(response_text)
                events = [
                    {"type": "turn.started"},
                    {"type": "item.completed", "item": {"type": "browser_action"}},
                    {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}},
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
            client.route_constraint = SimpleNamespace(
                schema={"type": "object", "properties": {"direction": {"enum": ["N"]}}},
                schema_sha256="schema-hash",
                schema_bytes=64,
            )
            with patch("route_experiment.codex_cli_adapter.subprocess.run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "codex_cli_tool_event_detected"):
                    client.chat([{"role": "user", "content": "choose N"}])
            self.assertEqual(
                client.last_record["event_audit"]["tool_item_types"], ["browser_action"]
            )

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
