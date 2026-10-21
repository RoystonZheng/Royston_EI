import tempfile
import unittest
from pathlib import Path

from route_experiment.codex_cli_adapter import CodexCliRouteStepClient
from route_experiment.route_model_factory import build_route_model_client


class RouteModelFactoryTests(unittest.TestCase):
    def test_codex_backend_needs_no_provider_api_key_for_stepwise_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            constraint = object()
            client = build_route_model_client(
                backend="codex_cli",
                generation_mode="stepwise",
                model="gpt-5.6-luna",
                trace_dir=Path(temp_dir) / "traces",
                api_key="",
                base_url="",
                reasoning_effort="low",
                route_constraint=constraint,
            )

        self.assertIsInstance(client, CodexCliRouteStepClient)
        self.assertEqual(client.model, "gpt-5.6-luna")
        self.assertEqual(client.retries, 0)
        self.assertIs(client.route_constraint, constraint)

    def test_codex_backend_rejects_whole_route_mode_before_any_invocation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "codex_cli_requires_stepwise_mode"):
                build_route_model_client(
                    backend="codex_cli",
                    generation_mode="whole_route",
                    model="gpt-5.6-luna",
                    trace_dir=Path(temp_dir) / "traces",
                    api_key="",
                    base_url="",
                    reasoning_effort="low",
                )


if __name__ == "__main__":
    unittest.main()
