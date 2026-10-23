import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_r4_codex_pilot_continuation as pilot  # noqa: E402


class FakeStepClient:
    def __init__(self, **kwargs):
        self.call_count = 0

    def chat(self, messages):
        self.call_count += 1
        return SimpleNamespace(
            content=json.dumps(
                {
                    "action": "move",
                    "direction": "S",
                    "failure_reason_code": "none",
                    "self_check": {
                        "allowed_directed_edge": False,
                        "starts_at_current": True,
                        "inside_boundary": False,
                        "pickup_order_ok": True,
                        "target_node_is_new": True,
                    },
                }
            ),
            provider_audit={"event_audit": {"tool_item_types": []}},
            input_tokens=100,
            cached_input_tokens=0,
            output_tokens=20,
            latency_ms=1.0,
            session_id=None,
        )


class R4CodexPilotAcceptanceTests(unittest.TestCase):
    def test_fresh_pilot_starts_at_frozen_task_origin_without_inherited_smoke_move(self):
        state = pilot._initial_state("fresh-task-test")

        self.assertEqual(state["accepted_nodes"], [state["start_node_id"]])
        self.assertEqual(state["accepted_edges"], [])
        self.assertEqual(state["accepted_directions"], [])
        self.assertIsNone(state["codex_session_id"])
        self.assertEqual(state["codex_session_mode"], "ephemeral_per_request")

    def test_preflight_manifest_uses_the_authorized_request_limit(self):
        preflight = ROOT / "outputs/route_layer/r4_preflight/r4_codex_ephemeral_8x8_seed0_20261022.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "PREFLIGHT_PATH", preflight),
            ):
                pilot.preflight_only(
                    experiment_id="request-limit-manifest-test",
                    authorized_request_limit=4,
                )

            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(state["authorized_new_request_limit"], 4)
        self.assertEqual(manifest["authorized_request_limit"], 4)
        self.assertEqual(manifest["initial_state"]["requests_authorized_in_this_continuation"], 4)
        self.assertEqual(
            manifest["initial_state"]["can_complete_with_authorized_requests"],
            manifest["initial_state"]["minimum_remaining_moves"] <= 4,
        )

    def test_one_step_filters_directions_to_the_remaining_authorized_calls(self):
        preflight = ROOT / "outputs/route_layer/r4_preflight/r4_codex_ephemeral_fixed_smoke_seed0_20261022.json"
        client_args = []
        observed_budgets = []

        class BudgetAwareClient:
            def __init__(self, **kwargs):
                client_args.append(kwargs)
                self.last_record = {}

            def chat(self, messages):
                request = json.loads(messages[-1]["content"])
                observed_budgets.append(request["s"]["b"])
                return SimpleNamespace(
                    content=json.dumps({
                        "action": "move",
                        "direction": "N",
                        "failure_reason_code": "none",
                        "self_check": {
                            "allowed_directed_edge": True,
                            "starts_at_current": True,
                            "inside_boundary": True,
                            "pickup_order_ok": True,
                            "target_node_is_new": True,
                        },
                    }),
                    provider_audit={"event_audit": {"tool_item_types": []}},
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=5,
                    latency_ms=1,
                    session_id=None,
                )

            @staticmethod
            def _redact_diagnostics(text):
                return text

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            with (
                patch.object(pilot, "PREFLIGHT_PATH", preflight),
                patch.object(pilot, "OUTPUT_DIR", output_dir),
            ):
                state = pilot._initial_state("budget-aware-step", authorized_request_limit=2)
                (output_dir).mkdir(parents=True)
                (output_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
                with patch.object(pilot, "CodexCliRouteStepClient", BudgetAwareClient):
                    pilot.one_step()
                updated = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(client_args[0]["route_constraint"].edge_by_direction.keys(), {"N"})
        self.assertEqual(observed_budgets, [2])
        self.assertEqual(updated["accepted_directions"], ["N"])

    def test_skips_model_request_when_remaining_authorization_cannot_finish_route(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 7,
            "attempts": [],
        })

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient") as client_class,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "insufficient_authorized_budget_for_remaining_route"
                ):
                    pilot.one_step()
                client_class.assert_not_called()

            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(state["status"], "insufficient_authorized_budget_for_remaining_route")
        self.assertEqual(state["new_requests_used"], 0)
        self.assertEqual(state["stop_diagnostics"]["unused_authorized_calls"], 7)

    def test_locally_valid_move_is_accepted_and_false_model_checks_are_recorded(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 32,
            "attempts": [],
        })

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", FakeStepClient),
            ):
                pilot.one_step()

            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        attempt = state["attempts"][-1]
        self.assertEqual(attempt["status"], "accepted_legal_step")
        self.assertEqual(attempt["selected_edge_id"], "edge_node_4_7_node_4_6")
        self.assertEqual(state["accepted_nodes"][-1], "node_4_6")
        self.assertEqual(state["status"], "ready")
        self.assertTrue(attempt["validation"]["deterministic_local_check"]["passed"])
        self.assertFalse(attempt["validation"]["all_model_self_checks_true"])
        self.assertEqual(
            attempt["validation"]["model_self_check_mismatch_fields"],
            ["allowed_directed_edge", "inside_boundary"],
        )

    def test_one_step_reuses_and_persists_codex_session_id(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 32,
            "attempts": [],
            "codex_session_id": "existing-codex-session",
            "codex_session_mode": "persistent_per_task",
        })
        client_args = []

        class SessionStepClient(FakeStepClient):
            def __init__(self, **kwargs):
                client_args.append(kwargs)
                super().__init__(**kwargs)

            def chat(self, messages):
                response = super().chat(messages)
                response.session_id = "existing-codex-session"
                response.provider_audit["session_id"] = response.session_id
                return response

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", SessionStepClient),
            ):
                pilot.one_step()
            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(len(client_args), 1)
        self.assertTrue(client_args[0]["persist_session"])
        self.assertEqual(client_args[0]["session_id"], "existing-codex-session")
        self.assertEqual(state["codex_session_id"], "existing-codex-session")

    def test_ephemeral_one_step_does_not_persist_cli_session_id(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 32,
            "attempts": [],
            "codex_session_id": None,
            "codex_session_mode": "ephemeral_per_request",
        })
        client_args = []

        class EphemeralSessionStepClient(FakeStepClient):
            def __init__(self, **kwargs):
                client_args.append(kwargs)
                super().__init__(**kwargs)

            def chat(self, messages):
                response = super().chat(messages)
                response.session_id = "cli-thread-id-for-audit-only"
                response.provider_audit["session_id"] = response.session_id
                return response

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", EphemeralSessionStepClient),
            ):
                pilot.one_step()
            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(len(client_args), 1)
        self.assertFalse(client_args[0]["persist_session"])
        self.assertIsNone(client_args[0]["session_id"])
        self.assertIsNone(state["codex_session_id"])

    def test_preflight_rejection_is_recorded_without_counting_or_appending_a_request(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 32,
            "attempts": [],
        })

        class PreflightRejectedClient:
            last_record = {
                "backend": "codex_cli",
                "status": "not_called",
                "stop_reason": "persistent_working_directory_not_empty",
            }

            def __init__(self, **kwargs):
                pass

            @staticmethod
            def _redact_diagnostics(text):
                return text

            def chat(self, messages):
                raise RuntimeError("codex_cli_persistent_working_directory_not_empty")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", PreflightRejectedClient),
            ):
                pilot.one_step()
            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(state["new_requests_used"], 0)
        self.assertEqual(state["attempts"], [])
        self.assertEqual(state["status"], "stopped_pre_request_error")
        self.assertEqual(
            state["pre_request_failures"][0]["provider_record"]["stop_reason"],
            "persistent_working_directory_not_empty",
        )


if __name__ == "__main__":
    unittest.main()
