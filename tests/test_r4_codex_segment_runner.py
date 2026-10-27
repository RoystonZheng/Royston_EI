import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_r4_codex_segment_task9 as segment_runner
import preflight_r4_codex_segment_task9 as segment_preflight


class R4CodexSegmentRunnerTests(unittest.TestCase):
    def test_recovery_feedback_comes_only_from_the_latest_attempt(self):
        rejected = {"request_number": 4, "audit": {"violations": [{"kind": "one_way_direction"}]}}
        accepted = {"request_number": 5, "audit": {"accepted": True, "violations": []}}
        latest_rejected = {"request_number": 6, "audit": {"violations": [{"kind": "boundary"}]}}

        self.assertIsNone(segment_runner._latest_feedback_attempt([rejected, accepted]))
        self.assertEqual(
            segment_runner._latest_feedback_attempt([rejected, accepted, latest_rejected]),
            latest_rejected,
        )

    def test_recovery_context_preserves_distinct_rejected_prefixes_at_same_state(self):
        loaded = segment_runner._load_gate(
            ROOT / "outputs/route_layer/r4_codex_segment_task9_preflight_v14"
        )
        recovery = segment_runner._build_recovery_context(
            ROOT / "outputs/route_layer/r4_codex_segment_task9_run_v6_reconciled_checkpoint_20261027",
            loaded,
        )

        constraints = recovery["initial_feedback"]["recent_rejected_prefix_constraints"]
        by_prefix = {tuple(item["prefix_directions"]): item for item in constraints}
        self.assertEqual(by_prefix[("N", "N")]["safe_next_directions"], ["E"])
        self.assertEqual(
            by_prefix[("N", "N", "E", "E")]["safe_next_directions"], ["S"]
        )

    def test_execute_cli_dispatches_request_limit_from_frozen_gate(self):
        argv = [
            "run_r4_codex_segment_task9.py",
            "--execute",
            "--request-limit",
            "28",
            "--authorization-note",
            "user-approved route pilot",
            "--preflight-dir",
            "preflight-v9",
            "--prior-failure-dir",
            "prior-run",
            "--output",
            "run-v1",
        ]
        with patch.object(sys, "argv", argv), patch.object(
            segment_runner, "run", return_value={"status": "completed_route"}
        ) as run_mock, redirect_stdout(StringIO()):
            segment_runner.main()

        run_mock.assert_called_once_with(
            Path("run-v1"),
            28,
            "user-approved route pilot",
            Path("preflight-v9"),
            Path("prior-run"),
        )

    def test_preflight_freezes_stateless_full_segments_with_no_feedback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "v7-preflight"
            record = segment_preflight.build_preflight(
                output_dir,
                session_mode="ephemeral_per_request",
                max_feedback_requests=0,
                require_full_segments=True,
            )

            schema = json.loads((output_dir / "route_schema.json").read_text(encoding="utf-8"))
            fixture = json.loads((output_dir / "offline_full_runner_fixture.json").read_text(encoding="utf-8"))
            self.assertEqual(record["experiment_id"], "r4_codex_segment_task9_preflight_v7")
            self.assertEqual(record["session_mode"], "ephemeral_per_request")
            self.assertEqual(record["segment_protocol"], "r4-segment-v5")
            self.assertTrue(record["require_full_segments"])
            self.assertEqual(record["max_feedback_requests"], 0)
            self.assertEqual(record["model_requests"], 0)
            self.assertEqual(record["maximum_model_requests"], 28)
            self.assertEqual(schema["properties"]["actions"]["minItems"], 8)
            self.assertEqual(schema["properties"]["actions"]["maxItems"], 8)
            self.assertTrue(fixture["route_complete"])

    def test_persistent_preflight_reuses_the_compact_map_after_the_first_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "r4-preflight-v15"
            record = segment_preflight.build_preflight(
                output_dir,
                session_mode="persistent_per_task",
                max_feedback_requests=218,
                require_full_segments=True,
            )

        fixture = record["offline_full_runner_fixture"]
        self.assertEqual(record["segment_protocol"], "r4-segment-v5")
        self.assertEqual(record["session_mode"], "persistent_per_task")
        self.assertTrue(record["map_representation"]["equivalence_passed"])
        self.assertTrue(record["map_representation"]["map_sent_once_per_persistent_session"])
        self.assertGreater(fixture["first_rendered_prompt_bytes"], 10000)
        self.assertLess(fixture["second_rendered_prompt_bytes"], 2000)

    def test_recovery_context_uses_validator_directions_and_records_attempted_energy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            preflight_dir = Path(temp_dir) / "r4_codex_segment_task9_preflight_v13"
            preflight_record = segment_preflight.build_preflight(
                preflight_dir,
                session_mode="ephemeral_per_request",
                max_feedback_requests=218,
                require_full_segments=True,
            )
            self.assertEqual(preflight_record["segment_protocol"], "r4-segment-v5")
            prompt_record = json.loads((preflight_dir / "route_messages.json").read_text(encoding="utf-8"))
            self.assertIn("每节点严格占连续4个字符", prompt_record["messages"][0]["content"])
            loaded = segment_runner._load_gate(preflight_dir)
            recovery = segment_runner._build_recovery_context(
                ROOT / "outputs/route_layer/r4_codex_segment_task9_run_v2_20261027",
                loaded,
            )

        self.assertEqual(recovery["initial_rejected_segments"], 1)
        self.assertEqual(recovery["initial_feedback"]["allowed_directions_at_failure_state"], ["N", "S"])
        self.assertEqual(recovery["prior_provider_audit"]["backend"], "codex_cli_trace_reconstructed")
        self.assertEqual(recovery["prior_provider_audit"]["event_audit"]["turn_started_count"], 1)
        self.assertFalse(recovery["prior_provider_audit"]["event_audit"]["tool_usage_detected"])
        self.assertEqual(recovery["prior_attempted_prefix_energy"][0]["legal_prefix_edge_count"], 1)
        self.assertGreater(recovery["prior_attempted_prefix_energy"][0]["energy_kwh"], 0)

        progressed_recovery = segment_runner._build_recovery_context(
            ROOT / "outputs/route_layer/r4_codex_segment_task9_run_v3_20261027",
            loaded,
        )
        self.assertEqual(progressed_recovery["initial_rejected_segments"], 4)
        self.assertEqual(progressed_recovery["prior_request_count"], 8)
        self.assertEqual(progressed_recovery["initial_route_state"]["current_node_id"], "node_51_35")
        self.assertEqual(len(progressed_recovery["initial_route_state"]["edge_sequence"]), 32)
        self.assertTrue(progressed_recovery["initial_feedback"]["allowed_directions_at_failure_state"])
        self.assertEqual(len(progressed_recovery["prior_attempted_prefix_energy"]), 4)

    def test_online_gate_accepts_explicit_stateless_segment_preflight(self):
        source = ROOT / "outputs/route_layer/r4_codex_segment_task9_preflight_v6/preflight.json"
        gate = json.loads(source.read_text(encoding="utf-8"))
        gate["session_mode"] = "ephemeral_per_request"
        gate["require_full_segments"] = True
        gate["max_feedback_requests"] = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            preflight_dir = Path(temp_dir)
            (preflight_dir / "preflight.json").write_text(
                json.dumps(gate), encoding="utf-8"
            )
            with patch.object(segment_runner, "PREFLIGHT_DIR", preflight_dir):
                loaded = segment_runner._load_gate()
        self.assertEqual(loaded["gate"]["session_mode"], "ephemeral_per_request")

    def test_pilot_runner_uses_formal_persistent_per_task_session(self):
        fixture_path = ROOT / "outputs/route_layer/r4_codex_segment_task9_preflight_v6/offline_full_runner_fixture.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        proposals = [item["proposal"] for item in fixture["attempts"]]
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.retries = 0
                self.cli_version = "fake-cli"
                self.route_constraint = None
                self.index = 0

            def chat(self, messages):
                proposal = proposals[self.index]
                self.index += 1
                return SimpleNamespace(
                    content=json.dumps(proposal),
                    model="fake-model",
                    request_id=f"fake-request-{self.index}",
                    finish_reason="turn.completed",
                    input_tokens=1,
                    cached_input_tokens=0,
                    output_tokens=1,
                    latency_ms=1.0,
                    retries=0,
                    provider_audit={"backend": "fake", "session_mode": "persistent_per_task"},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "segment-run"
            with patch.object(segment_runner, "CodexCliRouteStepClient", FakeClient):
                audit = segment_runner.run(output_dir, 25, "offline test fixture only")

            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(captured["persist_session"])
            self.assertEqual(captured["session_id"], None)
            self.assertEqual(captured["working_directory"], output_dir / "codex_empty_workspace")
            self.assertEqual(manifest["session_mode"], "persistent_per_task")
            self.assertEqual(manifest["codex_cli_session_mode"], "persistent_per_task")
            self.assertTrue(audit["route_complete"])
            self.assertEqual(audit["api_request_count"], 12)

    def test_runner_uses_ephemeral_sessions_for_compact_independent_segments(self):
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.retries = 0
                self.cli_version = "fake-cli"
                self.route_constraint = None
                self.index = 0

            def chat(self, messages):
                proposal = proposals[self.index]
                self.index += 1
                return SimpleNamespace(
                    content=json.dumps(proposal),
                    model="fake-model",
                    request_id=f"fake-request-{self.index}",
                    finish_reason="turn.completed",
                    input_tokens=1,
                    cached_input_tokens=0,
                    output_tokens=1,
                    latency_ms=1.0,
                    retries=0,
                    provider_audit={"backend": "fake", "session_mode": "ephemeral_per_request"},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            preflight_dir = Path(temp_dir) / "preflight"
            segment_preflight.build_preflight(
                preflight_dir,
                session_mode="ephemeral_per_request",
                max_feedback_requests=0,
                require_full_segments=True,
            )
            fixture = json.loads(
                (preflight_dir / "offline_full_runner_fixture.json").read_text(encoding="utf-8")
            )
            proposals = [item["proposal"] for item in fixture["attempts"]]
            output_dir = Path(temp_dir) / "segment-run"
            with patch.object(segment_runner, "PREFLIGHT_DIR", preflight_dir), patch.object(
                segment_runner, "CodexCliRouteStepClient", FakeClient
            ):
                audit = segment_runner.run(output_dir, 28, "offline test fixture only")

            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(captured["persist_session"])
            self.assertIsNone(captured["working_directory"])
            self.assertEqual(manifest["session_mode"], "ephemeral_per_request")
            self.assertEqual(manifest["max_feedback_requests"], 0)
            self.assertTrue(audit["route_complete"])
            self.assertEqual(audit["api_request_count"], 14)
            self.assertEqual(audit["attempts"][0]["provider_audit"]["backend"], "fake")


if __name__ == "__main__":
    unittest.main()
