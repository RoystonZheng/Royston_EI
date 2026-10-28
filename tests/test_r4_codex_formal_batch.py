import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_r4_codex_formal_batch import (
    _natural_task_request_limit,
    _validate_pilot_audit,
    execute_batch,
    prepare_batch,
)


class R4CodexFormalBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.preflight_path = self.root / "preflight.json"
        self.preflight_path.write_text(json.dumps({
            "status": "offline_task_preflight_passed_no_model_request",
            "model_requests": 0,
        }), encoding="utf-8")
        self.source_manifest = self.root / "source_manifest.json"
        self.source_manifest.write_text(json.dumps({
            "status": "offline_preflight_ready_no_model_requests",
            "model_requests": 0,
            "task_count": 1,
            "routeable_task_count": 1,
            "skipped_task_count": 0,
            "minimum_model_request_lower_bound": 2,
            "skipped_tasks": [],
            "tasks": [{
                "seed": 0,
                "sample_group": "orders",
                "task_id": "task-test",
                "preflight_path": str(self.preflight_path),
                "minimum_route_actions_lower_bound": 2,
            }],
        }), encoding="utf-8")
        self.pilot_audit_path = self.root / "pilot_audit.json"
        self.pilot_audit_path.write_text(json.dumps({
            "schema_version": "r4_codex_stepwise_single_task_audit_v1",
            "experiment_id": "r4_codex_stepwise_formal_task9_pilot_v1",
            "map_id": "grid-100x100-seed-0",
            "task_id": "task-0000-0009",
            "start_node_id": "node_69_41",
            "pickup_node_id": "node_25_66",
            "dropoff_node_id": "node_42_43",
            "status": "completed_route",
            "route_complete": True,
            "requests": 111,
            "accepted_legal_steps": 111,
            "illegal_actions_accepted": 0,
            "retries": 0,
            "tool_events": 0,
            "usage": {"input_tokens": 1000, "output_tokens": 100, "latency_ms": 1000, "new_requests": 111},
            "accepted_prefix_metrics": {
                "accepted_prefix_distance_m": 1000.0,
                "accepted_prefix_duration_sec": 500.0,
                "accepted_prefix_energy_kwh": 2.0,
            },
            "account_usage_guard": {
                "baseline_weekly_used_percent": 12,
                "max_weekly_usage_increase_percentage_points": 1,
                "stop_threshold_percent": 13,
                "observed_weekly_used_percent_after_request": 13,
                "credits_balance_changed": False,
            },
        }), encoding="utf-8")

        self.pilot_manifest_path = self.root / "manifest.json"
        self.pilot_manifest_path.write_text(json.dumps({
            "schema_version": "r4_codex_pilot_continuation_manifest_v2",
            "experiment_id": "r4_codex_stepwise_formal_task9_pilot_v1",
            "codex_cli_session_mode": "ephemeral_per_request",
            "route_input_protocol": "r4-step-v3",
            "actual_model_requests": 111,
        }), encoding="utf-8")
        self.output = self.root / "batch"

    def test_unbounded_task_limit_comes_from_frozen_graph_node_count(self):
        preflight = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"
        task = {"preflight_path": str(preflight), "minimum_route_actions_lower_bound": 109}

        limit = _natural_task_request_limit(task)

        self.assertGreater(limit, task["minimum_route_actions_lower_bound"])
        self.assertLessEqual(limit, 10000)

    def tearDown(self):
        self.temp.cleanup()

    def _prepare(self):
        return prepare_batch(self.source_manifest, self.output)

    @staticmethod
    def _arg(args, key):
        return Path(args[args.index(key) + 1])

    def _write_v3_pilot_audit(self, *, pickup_reached):
        self.pilot_audit_path.write_text(json.dumps({
            "schema_version": "r4_codex_pilot_final_audit_v3",
            "experiment_id": "r4_codex_stepwise_formal_task9_pilot_v1",
            "status": "completed_route",
            "map_id": "grid-100x100-seed-0",
            "task_id": "task-0000-0009",
            "start_node": "node_69_41",
            "pickup_node": "node_25_66",
            "dropoff_node": "node_42_43",
            "pickup_reached": pickup_reached,
            "dropoff_reached": True,
            "repeated_nodes": False,
            "route_continuity_violations": [],
            "actual_model_requests_this_continuation": 111,
            "accepted_actions_this_run": 111,
            "invalid_or_stopped_actions": [],
            "retries": 0,
            "tool_events": 0,
            "input_tokens_this_continuation": 1000,
            "output_tokens_this_continuation": 100,
            "accepted_prefix_distance_m": 1000.0,
            "accepted_prefix_duration_sec": 500.0,
            "accepted_prefix_energy_kwh": 2.0,
        }), encoding="utf-8")
        (self.root / "state.json").write_text(json.dumps({
            "usage_totals": {
                "new_requests": 111,
                "input_tokens": 1000,
                "output_tokens": 100,
                "latency_ms": 99000.0,
            },
        }), encoding="utf-8")
        (self.root / "quota_guard.json").write_text(json.dumps({
            "schema_version": "codex_account_usage_guard_v1",
            "baseline_weekly_used_percent": 12,
            "max_weekly_usage_increase_percentage_points": 1,
            "stop_threshold_percent": 13,
            "observed_weekly_used_percent_after_request": 13,
            "credits_balance_changed": False,
            "requests_used_when_stopped": 111,
            "accepted_route_steps_when_stopped": 111,
        }), encoding="utf-8")

    def test_prepare_is_zero_request_plan_with_visual_index(self):
        manifest = self._prepare()
        self.assertEqual(manifest["status"], "offline_batch_preflight_ready_no_model_requests")
        self.assertEqual(manifest["model_requests_used"], 0)
        self.assertTrue((self.output / "index.html").is_file())
        index = (self.output / "index.html").read_text(encoding="utf-8")
        self.assertIn("原地图", index)
        self.assertIn("冻结样本：1", index)

    def test_usage_gate_stops_before_any_child_process(self):
        self._prepare()
        calls = []
        manifest = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=19,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: calls.append(args),
        )
        self.assertEqual(manifest["status"], "stopped_before_request_usage_at_or_above_stop_line")
        self.assertEqual(calls, [])
        self.assertEqual(manifest["model_requests_used"], 0)
        self.assertTrue(manifest.get("updated_at_utc"))

    def test_usage_gate_stops_when_baseline_is_at_the_stop_line(self):
        self._prepare()
        calls = []
        manifest = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=13,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: calls.append(args),
        )
        self.assertEqual(manifest["status"], "stopped_before_request_usage_at_or_above_stop_line")
        self.assertEqual(calls, [])
        self.assertEqual(manifest["model_requests_used"], 0)

    def test_request_cap_below_pending_lower_bound_does_not_run_child(self):
        self._prepare()
        calls = []
        manifest = execute_batch(
            self.output,
            authorized_request_limit=1,
            usage_baseline_percent=0,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: calls.append(args),
        )
        self.assertEqual(manifest["status"], "stopped_before_request_insufficient_budget_for_remaining_minimum")
        self.assertEqual(calls, [])

    def test_incomplete_bounded_pilot_blocks_formal_batch_before_any_child_process(self):
        self._prepare()
        audit = json.loads(self.pilot_audit_path.read_text(encoding="utf-8"))
        audit.update({"status": "partial_route_incomplete", "route_complete": False})
        self.pilot_audit_path.write_text(json.dumps(audit), encoding="utf-8")
        calls = []
        manifest = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=0,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: calls.append(args),
        )
        self.assertEqual(manifest["status"], "stopped_before_request_pilot_incomplete")
        self.assertEqual(calls, [])
        self.assertEqual(manifest["model_requests_used"], 0)

    def test_pilot_audit_must_use_the_frozen_task_endpoints(self):
        self._prepare()
        audit = json.loads(self.pilot_audit_path.read_text(encoding="utf-8"))
        audit["pickup_node_id"] = "node_25_65"
        self.pilot_audit_path.write_text(json.dumps(audit), encoding="utf-8")
        calls = []
        manifest = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=0,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: calls.append(args),
        )
        self.assertEqual(manifest["status"], "stopped_before_request_pilot_gate_failed")
        self.assertIn("pilot_pickup_node_mismatch", manifest["pilot_gate"]["errors"])
        self.assertEqual(calls, [])

    def test_pilot_session_mode_must_match_the_formal_batch(self):
        self._prepare()
        pilot_manifest = json.loads(self.pilot_manifest_path.read_text(encoding="utf-8"))
        pilot_manifest["codex_cli_session_mode"] = "persistent_per_task"
        self.pilot_manifest_path.write_text(json.dumps(pilot_manifest), encoding="utf-8")
        calls = []
        manifest = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=0,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: (
                calls.append(args), SimpleNamespace(returncode=0, stdout="", stderr="")
            )[1],
        )
        self.assertEqual(manifest["status"], "stopped_before_request_pilot_gate_failed")
        self.assertIn("pilot_session_mode_mismatch", manifest["pilot_gate"]["errors"])
        self.assertEqual(calls, [])

    def test_pilot_route_input_protocol_must_match_the_formal_batch(self):
        self._prepare()
        pilot_manifest = json.loads(self.pilot_manifest_path.read_text(encoding="utf-8"))
        pilot_manifest["route_input_protocol"] = "r4-step-v2"
        self.pilot_manifest_path.write_text(json.dumps(pilot_manifest), encoding="utf-8")
        calls = []
        manifest = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=0,
            usage_stop_percent=100,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: calls.append(args),
        )
        self.assertEqual(manifest["status"], "stopped_before_request_pilot_gate_failed")
        self.assertIn("pilot_route_input_protocol_mismatch", manifest["pilot_gate"]["errors"])
        self.assertEqual(calls, [])

    def test_a_passing_pilot_session_manifest_is_hash_locked_for_resume(self):
        self._prepare()
        first = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=13,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: self.fail("usage gate should stop before child process"),
        )
        self.assertEqual(first["status"], "stopped_before_request_usage_at_or_above_stop_line")
        pilot_manifest = json.loads(self.pilot_manifest_path.read_text(encoding="utf-8"))
        pilot_manifest["codex_cli_session_mode"] = "persistent_per_task"
        self.pilot_manifest_path.write_text(json.dumps(pilot_manifest), encoding="utf-8")
        calls = []
        resumed = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=0,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: (
                calls.append(args), SimpleNamespace(returncode=0, stdout="", stderr="")
            )[1],
        )
        self.assertEqual(resumed["status"], "stopped_before_request_pilot_gate_failed")
        self.assertEqual(resumed["pilot_gate"]["reason"], "pilot_evidence_changed_after_gate_passed")
        self.assertEqual(calls, [])

    def test_finalizer_v3_pilot_audit_can_pass_the_gate_with_quota_guard(self):
        self._prepare()
        self._write_v3_pilot_audit(pickup_reached=True)
        calls = []
        manifest = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=13,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: calls.append(args),
        )
        self.assertEqual(manifest["status"], "stopped_before_request_usage_at_or_above_stop_line")
        self.assertTrue(manifest["pilot_gate"]["passed"])
        self.assertEqual(manifest["pilot_gate"]["session_mode"], "ephemeral_per_request")
        self.assertEqual(manifest["pilot_gate"]["start_node_id"], "node_69_41")
        self.assertEqual(manifest["pilot_gate"]["pickup_node_id"], "node_25_66")
        self.assertEqual(manifest["pilot_gate"]["dropoff_node_id"], "node_42_43")
        self.assertEqual(manifest["pilot_gate"]["route_energy_kwh"], 2.0)
        self.assertEqual(calls, [])

    def test_finalizer_v3_pilot_without_pickup_is_rejected(self):
        self._prepare()
        self._write_v3_pilot_audit(pickup_reached=False)
        calls = []
        manifest = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=13,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: calls.append(args),
        )
        self.assertEqual(manifest["status"], "stopped_before_request_pilot_incomplete")
        self.assertFalse(manifest["pilot_gate"]["passed"])
        self.assertIn("pilot_route_incomplete", manifest["pilot_gate"]["errors"])
        self.assertEqual(calls, [])

    def test_unexecuted_stopped_action_is_not_counted_as_accepted_illegal(self):
        self._write_v3_pilot_audit(pickup_reached=False)
        audit = json.loads(self.pilot_audit_path.read_text(encoding="utf-8"))
        audit["status"] = "stopped_illegal_or_invalid_action"
        audit["dropoff_reached"] = False
        audit["invalid_or_stopped_actions"] = [{
            "action_executed": False,
            "deterministic_local_check_passed": True,
            "status": "stopped_illegal_or_invalid_action",
        }]
        self.pilot_audit_path.write_text(json.dumps(audit), encoding="utf-8")

        gate = _validate_pilot_audit(self.pilot_audit_path)

        self.assertFalse(gate["passed"])
        self.assertIn("pilot_route_incomplete", gate["errors"])
        self.assertNotIn("pilot_contains_accepted_illegal_action", gate["errors"])

    def test_executed_action_that_failed_local_validation_is_rejected(self):
        self._write_v3_pilot_audit(pickup_reached=True)
        audit = json.loads(self.pilot_audit_path.read_text(encoding="utf-8"))
        audit["invalid_or_stopped_actions"] = [{
            "action_executed": True,
            "deterministic_local_check_passed": False,
            "status": "accepted_illegal_action",
        }]
        self.pilot_audit_path.write_text(json.dumps(audit), encoding="utf-8")

        gate = _validate_pilot_audit(self.pilot_audit_path)

        self.assertFalse(gate["passed"])
        self.assertIn("pilot_contains_accepted_illegal_action", gate["errors"])

    def test_fake_child_completes_task_and_finalizes_without_real_model(self):
        self._prepare()
        calls = []

        def fake_runner(args, **kwargs):
            calls.append(args)
            if str(args[1]).endswith("run_r4_codex_pilot_continuation.py"):
                output_dir = self._arg(args, "--output-dir")
                state_path = output_dir / "state.json"
                if "--preflight-only" in args:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    (output_dir / "replay.html").write_text("fake replay", encoding="utf-8")
                    state_path.write_text(json.dumps({"status": "ready", "new_requests_used": 0, "attempts": []}), encoding="utf-8")
                else:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state.update({"status": "route_complete", "new_requests_used": 1})
                    state_path.write_text(json.dumps(state), encoding="utf-8")
            elif str(args[1]).endswith("finalize_r4_codex_pilot_continuation.py"):
                run_dir = self._arg(args, "--run-dir")
                (run_dir / "final_audit.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        manifest = execute_batch(
            self.output,
            authorized_request_limit=2,
            usage_baseline_percent=0,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            runner=fake_runner,
        )
        self.assertEqual(manifest["status"], "completed_with_audited_task_outcomes")
        self.assertEqual(manifest["model_requests_used"], 1)
        task = manifest["tasks"][0]
        self.assertEqual(task["status"], "route_complete")
        self.assertTrue((self.output / task["replay_path"]).is_file())
        self.assertTrue((self.output / task["final_audit_path"]).is_file())
        self.assertEqual(sum("--one-step" in args for args in calls), 1)

    def test_no_project_cap_disables_legacy_external_usage_stop_gate(self):
        manifest = self._prepare()
        manifest["require_external_live_account_usage_gate"] = True
        manifest["external_live_account_usage_checkpoint"] = {
            "active": False,
            "stop_threshold_percent": 100,
        }
        manifest["halt_no_more_model_requests"] = True
        manifest["halt_status"] = "stopped_before_request_live_usage_gate_missing_or_stale"
        manifest["usage_stop_percent"] = 100
        manifest["authorized_stop_threshold_percent"] = 100
        (self.output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        calls = []

        def fake_runner(args, **kwargs):
            calls.append(args)
            if str(args[1]).endswith("run_r4_codex_pilot_continuation.py"):
                output_dir = self._arg(args, "--output-dir")
                state_path = output_dir / "state.json"
                if "--preflight-only" in args:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    (output_dir / "replay.html").write_text("fake replay", encoding="utf-8")
                    state_path.write_text(json.dumps({"status": "ready", "new_requests_used": 0, "attempts": []}), encoding="utf-8")
                else:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state.update({"status": "route_complete", "new_requests_used": 1})
                    state_path.write_text(json.dumps(state), encoding="utf-8")
            elif str(args[1]).endswith("finalize_r4_codex_pilot_continuation.py"):
                run_dir = self._arg(args, "--run-dir")
                (run_dir / "final_audit.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        result = execute_batch(
            self.output,
            authorized_request_limit=None,
            usage_baseline_percent=5,
            usage_stop_percent=None,
            pilot_audit_path=self.pilot_audit_path,
            max_new_requests_per_invocation=1,
            runner=fake_runner,
        )

        self.assertEqual(result["status"], "completed_with_audited_task_outcomes")
        self.assertFalse(result["require_external_live_account_usage_gate"])
        self.assertFalse(result["halt_no_more_model_requests"])
        self.assertIsNone(result["halt_status"])
        self.assertIsNone(result["usage_stop_percent"])
        self.assertIsNone(result["authorized_stop_threshold_percent"])
        self.assertIsNone(result["usage_guard"]["stop_threshold_percent"])
        self.assertTrue(any(e.get("event") == "external_usage_stop_gate_disabled_by_latest_direct_user_authorization" for e in result["events"]))
        self.assertEqual(sum("--one-step" in args for args in calls), 1)

    def test_no_project_cap_preserves_non_quota_safety_halt(self):
        manifest = self._prepare()
        manifest.update({
            "require_external_live_account_usage_gate": True,
            "usage_stop_percent": 100,
            "authorized_stop_threshold_percent": 100,
            "halt_no_more_model_requests": True,
            "halt_status": "stopped_task_preflight_hash_mismatch",
            "status": "stopped_task_preflight_hash_mismatch",
        })
        (self.output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        calls = []

        result = execute_batch(
            self.output,
            authorized_request_limit=None,
            usage_baseline_percent=5,
            usage_stop_percent=None,
            pilot_audit_path=self.pilot_audit_path,
            runner=lambda *args, **kwargs: calls.append(args),
        )

        self.assertEqual(result["status"], "stopped_task_preflight_hash_mismatch")
        self.assertTrue(result["halt_no_more_model_requests"])
        self.assertEqual(result["halt_status"], "stopped_task_preflight_hash_mismatch")
        self.assertFalse(result["require_external_live_account_usage_gate"])
        self.assertIsNone(result["usage_stop_percent"])
        self.assertEqual(calls, [])

    def test_request_chunk_pauses_after_one_response_and_resumes_same_task(self):
        self._prepare()
        calls = []
        step_calls = 0

        def fake_runner(args, **kwargs):
            nonlocal step_calls
            calls.append(args)
            if str(args[1]).endswith("run_r4_codex_pilot_continuation.py"):
                output_dir = self._arg(args, "--output-dir")
                state_path = output_dir / "state.json"
                if "--preflight-only" in args:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    (output_dir / "replay.html").write_text("fake replay", encoding="utf-8")
                    state_path.write_text(json.dumps({"status": "ready", "new_requests_used": 0, "attempts": []}), encoding="utf-8")
                else:
                    step_calls += 1
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state.update({
                        "status": "route_complete" if step_calls == 2 else "ready",
                        "new_requests_used": step_calls,
                    })
                    state_path.write_text(json.dumps(state), encoding="utf-8")
            elif str(args[1]).endswith("finalize_r4_codex_pilot_continuation.py"):
                run_dir = self._arg(args, "--run-dir")
                (run_dir / "final_audit.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        first = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=0,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            max_new_requests_per_invocation=1,
            runner=fake_runner,
        )
        self.assertEqual(first["status"], "paused_at_request_chunk_limit")
        self.assertEqual(first["model_requests_used"], 1)
        self.assertEqual(first["tasks"][0]["status"], "ready")
        self.assertEqual(sum("--one-step" in args for args in calls), 1)

        second = execute_batch(
            self.output,
            authorized_request_limit=10,
            usage_baseline_percent=0,
            usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path,
            max_new_requests_per_invocation=1,
            runner=fake_runner,
        )
        self.assertEqual(second["status"], "completed_with_audited_task_outcomes")
        self.assertEqual(second["model_requests_used"], 2)
        self.assertEqual(second["tasks"][0]["status"], "route_complete")
        self.assertEqual(sum("--preflight-only" in args for args in calls), 1)
        self.assertEqual(sum("--one-step" in args for args in calls), 2)

    def test_unknown_in_flight_outcome_is_never_retried(self):
        self._prepare()
        one_step_calls = []

        def fake_runner(args, **kwargs):
            if str(args[1]).endswith("run_r4_codex_pilot_continuation.py"):
                output_dir = self._arg(args, "--output-dir")
                state_path = output_dir / "state.json"
                if "--preflight-only" in args:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    state_path.write_text(json.dumps({"status": "ready", "new_requests_used": 0, "attempts": []}), encoding="utf-8")
                else:
                    one_step_calls.append(args)
                    state_path.write_text(json.dumps({"status": "request_in_progress", "new_requests_used": 0, "attempts": []}), encoding="utf-8")
                    raise RuntimeError("simulated interruption")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        first = execute_batch(
            self.output, authorized_request_limit=2,
            usage_baseline_percent=0, usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path, runner=fake_runner,
        )
        second = execute_batch(
            self.output, authorized_request_limit=2,
            usage_baseline_percent=0, usage_stop_percent=13,
            pilot_audit_path=self.pilot_audit_path, runner=fake_runner,
        )
        self.assertEqual(first["status"], "stopped_outcome_unknown_no_retry")
        self.assertEqual(second["status"], "stopped_outcome_unknown_no_retry")
        self.assertEqual(len(one_step_calls), 1)


if __name__ == "__main__":
    unittest.main()
