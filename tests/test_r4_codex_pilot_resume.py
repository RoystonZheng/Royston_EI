import sys
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_r4_codex_pilot_continuation as r4_runner  # noqa: E402
from run_r4_provider_rejection_recoveries import _valid_zero_call_continuation_preflight  # noqa: E402
from run_r4_codex_pilot_continuation import (  # noqa: E402
    _authorized_call_budget_stop_reason,
    _load_frozen_task,
    _initial_state,
    _natural_simple_path_limit,
    _is_formal_batch_task_id,
    _summarize_attempt_usage,
    _model_self_check_all_true,
    _model_self_check_is_well_formed,
    preflight_only,
    _resume_state,
    _safe_unresolved_delivery_resume_parent,
    one_step,
)


class R4CodexPilotResumeTests(unittest.TestCase):
    def test_recovery_preflight_gate_requires_frozen_authorized_zero_call_state(self):
        state = {
            "status": "ready",
            "additional_model_requests_authorized": True,
            "route_input_protocol": "r4-step-v3",
            "task_id": "formal-task-0000-feasible",
            "parent_state_sha256": "a" * 64,
            "parent_final_audit_sha256": "b" * 64,
            "new_requests_used": 0,
            "attempts": [],
            "authorized_new_request_limit": 9999,
            "effective_authorized_new_request_limit": 9999,
        }
        args = (
            state,
            "formal-task-0000-feasible",
            "a" * 64,
            "b" * 64,
            9999,
        )
        self.assertTrue(_valid_zero_call_continuation_preflight(*args))

        invalid_variants = [
            {**state, "status": "failed"},
            {**state, "additional_model_requests_authorized": False},
            {**state, "route_input_protocol": "r4-step-v2"},
            {**state, "new_requests_used": 1},
            {**state, "attempts": [{"request_number": 1}]},
            {**state, "parent_state_sha256": "c" * 64},
            {**state, "authorized_new_request_limit": 128},
        ]
        for invalid in invalid_variants:
            with self.subTest(state=invalid):
                self.assertFalse(_valid_zero_call_continuation_preflight(
                    invalid,
                    "formal-task-0000-feasible",
                    "a" * 64,
                    "b" * 64,
                    9999,
                ))

    def test_formal_resume_accepts_both_frozen_matrix_task_id_families(self):
        self.assertTrue(_is_formal_batch_task_id("task-0002-0005"))
        self.assertTrue(_is_formal_batch_task_id("formal-task-0003-feasible"))
        self.assertFalse(_is_formal_batch_task_id("ad-hoc-task-0001"))

    def _provider_rejection_parent(self, directory, error_message):
        trace_path = Path(directory) / "provider-error.jsonl"
        rows = [
            {"type": "thread.started"},
            {"type": "turn.started"},
            {"type": "error", "message": error_message},
            {"type": "turn.failed", "error": {"message": error_message}},
        ]
        trace_path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        import hashlib

        trace_sha = hashlib.sha256(trace_path.read_bytes()).hexdigest()
        failed_attempt = {
            "request_number": 2,
            "status": "stopped_request_error",
            "retry_count": 0,
            "tool_item_types": [],
            "response": None,
            "validation": None,
            "provider_record": {
                "backend": "codex_cli",
                "status": "cli_error",
                "cli_exit_code": 1,
                "retry_count": 0,
                "canonical_final_json_present": False,
                "agent_message_count": 0,
                "usage_available": False,
                "trace_jsonl_path": str(trace_path),
                "trace_jsonl_sha256": trace_sha,
                "event_audit": {
                    "event_types": [
                        "thread.started", "turn.started", "error", "turn.failed"
                    ],
                    "tool_item_types": [],
                    "tool_usage_detected": False,
                    "usage_event_count": 0,
                    "item_types": [],
                },
            },
        }
        accepted_attempt = {
            "request_number": 1,
            "status": "accepted_legal_step",
            "retry_count": 0,
            "tool_item_types": [],
            "provider_record": {
                "event_audit": {
                    "tool_item_types": [],
                    "tool_usage_detected": False,
                }
            },
        }
        parent = {
            "status": "stopped_request_error",
            "new_requests_used": 2,
            "retry_count": 0,
            "accepted_nodes": ["node_0_0", "node_0_1"],
            "accepted_edges": ["edge_0_0_0_1"],
            "attempts": [accepted_attempt, failed_attempt],
        }
        audit = {
            "status": "partial_legal_prefix_not_complete",
            "retries": 0,
            "actual_model_requests_this_continuation": 2,
            "codex_cli_invocations_this_continuation": 2,
            "route_continuity_violations": [],
            "accepted_move_count_including_prior_smoke": 1,
            "current_node": "node_0_1",
            "tool_events": None,
            "tool_audit_complete": False,
        }
        return parent, audit, failed_attempt

    def test_confirmed_provider_rate_limit_can_resume_without_replaying_failed_request(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, audit, failed_attempt = self._provider_rejection_parent(
                directory, "exceeded retry limit, last status: 429 Too Many Requests"
            )

            rejected = r4_runner._safe_provider_rejection_resume_parent(parent, audit)

        self.assertEqual(rejected, [2])
        self.assertEqual(failed_attempt["status"], "stopped_request_error")

    def test_provider_capacity_rejection_can_resume_from_only_the_accepted_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, audit, _ = self._provider_rejection_parent(
                directory, "Selected model is at capacity. Please try a different model."
            )

            rejected = r4_runner._safe_provider_rejection_resume_parent(parent, audit)

        self.assertEqual(rejected, [2])

    def test_provider_error_resume_rejects_unknown_or_unhashed_terminal_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, audit, failed_attempt = self._provider_rejection_parent(
                directory, "network connection lost"
            )
            self.assertEqual(r4_runner._safe_provider_rejection_resume_parent(parent, audit), [])

            failed_attempt["provider_record"]["trace_jsonl_sha256"] = "0" * 64
            self.assertEqual(r4_runner._safe_provider_rejection_resume_parent(parent, audit), [])

    def test_provider_error_resume_rejects_tool_or_completed_turn_events(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, audit, failed_attempt = self._provider_rejection_parent(
                directory, "429 Too Many Requests"
            )
            trace_path = Path(failed_attempt["provider_record"]["trace_jsonl_path"])
            import hashlib

            rows = [
                {"type": "thread.started"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "shell"}},
                {"type": "error", "message": "429 Too Many Requests"},
                {"type": "turn.failed", "error": {"message": "429 Too Many Requests"}},
            ]
            trace_path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            failed_attempt["provider_record"]["trace_jsonl_sha256"] = hashlib.sha256(
                trace_path.read_bytes()
            ).hexdigest()

            self.assertEqual(r4_runner._safe_provider_rejection_resume_parent(parent, audit), [])

            rows[2] = {"type": "turn.completed", "usage": {"input_tokens": 1}}
            trace_path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            failed_attempt["provider_record"]["trace_jsonl_sha256"] = hashlib.sha256(
                trace_path.read_bytes()
            ).hexdigest()
            self.assertEqual(r4_runner._safe_provider_rejection_resume_parent(parent, audit), [])

    def test_formal_provider_rejection_resume_requires_authorization_and_keeps_usage_unknown(self):
        source = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v8"
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "formal-parent"
            shutil.copytree(source, parent)
            state_path = parent / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["user_authorization_status"] = "granted_for_formal_r4_batch_pilot"
            state["status"] = "stopped_request_error"
            failed = state["attempts"][-1]
            trace_path = parent / "traces" / "synthetic_provider_rejection.jsonl"
            rows = [
                {"type": "thread.started"},
                {"type": "turn.started"},
                {"type": "error", "message": "429 Too Many Requests"},
                {"type": "turn.failed", "error": {"message": "429 Too Many Requests"}},
            ]
            trace_path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            import hashlib

            failed.update({
                "status": "stopped_request_error",
                "retry_count": 0,
                "tool_item_types": [],
                "response": None,
                "validation": None,
                "usage": {
                    "input_tokens": None,
                    "cached_input_tokens": None,
                    "output_tokens": None,
                },
                "provider_record": {
                    "backend": "codex_cli",
                    "status": "cli_error",
                    "cli_exit_code": 1,
                    "retry_count": 0,
                    "canonical_final_json_present": False,
                    "agent_message_count": 0,
                    "usage_available": False,
                    "trace_jsonl_path": str(trace_path),
                    "trace_jsonl_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
                    "event_audit": {
                        "event_types": [
                            "thread.started", "turn.started", "error", "turn.failed"
                        ],
                        "tool_item_types": [],
                        "tool_usage_detected": False,
                        "usage_event_count": 0,
                        "item_types": [],
                    },
                },
            })
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "resume_parent_not_cleanly_capped"):
                _resume_state(parent, authorized_request_limit=512)

            resumed = _resume_state(
                parent,
                authorized_request_limit=512,
                user_authorized_formal_task_resume=True,
            )

        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["new_requests_used"], 0)
        self.assertEqual(resumed["unresolved_parent_request_numbers_not_replayed"], [])
        self.assertEqual(
            resumed["provider_rejected_parent_request_numbers_not_replayed"], [5]
        )
        self.assertEqual(
            resumed["provider_rejected_parent_usage_unknown_request_numbers"], [5]
        )
        self.assertEqual(resumed["resume_reason"], "confirmed_provider_rate_limit_or_capacity_rejection")
        self.assertFalse(resumed["rejected_parent_action_replayed"])

    def test_usage_summary_keeps_missing_request_usage_unknown(self):
        summary = _summarize_attempt_usage(
            [
                {
                    "request_number": 1,
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 10,
                        "output_tokens": 7,
                    },
                },
                {
                    "request_number": 2,
                    "usage": {
                        "input_tokens": None,
                        "cached_input_tokens": None,
                        "output_tokens": 5,
                    },
                },
            ],
            new_requests=2,
        )

        self.assertIsNone(summary["input_tokens"])
        self.assertEqual(summary["known_input_tokens_sum"], 100)
        self.assertEqual(summary["output_tokens"], 12)
        self.assertEqual(summary["unknown_usage_request_numbers"], [2])
        self.assertFalse(summary["usage_complete"])

    def test_authorized_call_budget_stops_when_shortest_remaining_route_cannot_fit(self):
        self.assertIsNone(_authorized_call_budget_stop_reason(5, 8))
        self.assertEqual(
            _authorized_call_budget_stop_reason(9, 1),
            "insufficient_authorized_budget_for_remaining_route",
        )
        self.assertEqual(
            _authorized_call_budget_stop_reason(None, 8),
            "remaining_route_unreachable",
        )

    def test_fresh_pilot_can_use_a_unique_experiment_id(self):
        state = _initial_state(experiment_id="r4_codex_independent_8x8_pilot_v5")

        self.assertEqual(state["experiment_id"], "r4_codex_independent_8x8_pilot_v5")
        self.assertEqual(state["accepted_nodes"], ["node_0_5"])
        self.assertEqual(state["accepted_edges"], [])
        self.assertEqual(state["accepted_directions"], [])
        self.assertEqual(state["effective_authorized_new_request_limit"], 0)
        self.assertFalse(state["additional_model_requests_authorized"])

    def test_fresh_pilot_honors_a_lower_authorized_request_limit(self):
        state = _initial_state(
            experiment_id="r4_codex_independent_8x8_pilot_v6",
            authorized_request_limit=6,
        )

        self.assertEqual(state["authorized_new_request_limit"], 6)
        self.assertEqual(state["status"], "insufficient_authorized_budget_for_remaining_route")
        with self.assertRaisesRegex(ValueError, "authorized_request_limit_out_of_range"):
            _initial_state(authorized_request_limit=9)

    def test_stepwise_pilot_separates_self_check_shape_from_truth(self):
        valid = {
            "self_check": {
                "allowed_directed_edge": True,
                "starts_at_current": True,
                "inside_boundary": True,
                "pickup_order_ok": True,
                "target_node_is_new": True,
            }
        }
        legacy = {
            "self_check": {
                **valid["self_check"],
                "target_node_is_new": None,
                "not_revisited": True,
            }
        }
        false_new_node_claim = {
            "self_check": {**valid["self_check"], "target_node_is_new": False}
        }

        self.assertTrue(_model_self_check_is_well_formed(valid))
        self.assertTrue(_model_self_check_all_true(valid))
        self.assertFalse(_model_self_check_is_well_formed(legacy))
        self.assertFalse(_model_self_check_all_true(legacy))
        self.assertTrue(_model_self_check_is_well_formed(false_new_node_claim))
        self.assertFalse(_model_self_check_all_true(false_new_node_claim))

    def test_resume_accepts_audit_request_count_field_from_v2_parent(self):
        parent = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v2"
        state = _resume_state(parent)

        self.assertEqual(state["status"], "insufficient_authorized_budget_for_remaining_route")
        self.assertEqual(state["experiment_id"], "r4_codex_independent_8x8_pilot_v3")
        self.assertEqual(state["new_requests_used"], 0)
        self.assertEqual(state["parent_requests"], 8)
        self.assertEqual(state["accepted_nodes"][-1], "node_4_4")
        self.assertEqual(state["initial_remaining_minimum_moves"], 10)

    def test_continuation_discards_persisted_thread_ids_for_ephemeral_steps(self):
        source = ROOT / "outputs/route_layer/r4_codex_independent_pilot_v6"
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "parent"
            shutil.copytree(source, parent)
            state_path = parent / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["codex_session_mode"] = "persistent_per_task"
            state["codex_session_id"] = "expired-thread-id-must-not-be-resumed"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            resumed = _resume_state(parent)

        self.assertEqual(resumed["codex_session_mode"], "ephemeral_per_request")
        self.assertIsNone(resumed["codex_session_id"])

    def test_resume_v3_after_unexecuted_self_check_mismatch_from_last_legal_prefix(self):
        parent = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3"
        with self.assertRaisesRegex(RuntimeError, "resume_parent_not_cleanly_capped"):
            _resume_state(parent)

        state = _resume_state(parent, allow_unexecuted_self_check_mismatch=True)

        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["experiment_id"], "r4_codex_independent_8x8_pilot_v4")
        self.assertEqual(state["accepted_nodes"][-1], "node_0_4")
        self.assertEqual(state["new_requests_used"], 0)
        self.assertEqual(state["parent_requests"], 7)
        self.assertEqual(state["parent_input_tokens"], 65626)
        self.assertEqual(state["parent_output_tokens"], 7931)
        self.assertEqual(state["initial_remaining_minimum_moves"], 8)
        self.assertEqual(state["resume_reason"], "unexecuted_false_not_revisited_self_check_with_local_pass")

    def test_resume_v5_after_unexecuted_false_self_check_from_last_legal_prefix(self):
        parent = ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5"

        state = _resume_state(parent, allow_unexecuted_self_check_mismatch=True)

        self.assertEqual(state["status"], "insufficient_authorized_budget_for_remaining_route")
        self.assertEqual(state["experiment_id"], "r4_codex_independent_8x8_pilot_v6")
        self.assertEqual(state["accepted_nodes"][-1], "node_4_7")
        self.assertEqual(state["new_requests_used"], 0)
        self.assertEqual(state["parent_requests"], 6)
        self.assertEqual(state["resume_reason"], "unexecuted_false_model_self_check_with_local_pass")

    def test_resume_v6_advances_experiment_id_without_reusing_an_old_version(self):
        parent = ROOT / "outputs/route_layer/r4_codex_independent_pilot_v6"

        state = _resume_state(parent)

        self.assertEqual(state["experiment_id"], "r4_codex_independent_8x8_pilot_v7")
        self.assertEqual(state["parent_run"], "outputs/route_layer/r4_codex_independent_pilot_v6")
        self.assertEqual(state["parent_requests"], 8)
        self.assertEqual(state["accepted_nodes"][-1], "node_1_4")
        self.assertEqual(state["initial_remaining_minimum_moves"], 7)

    def test_resume_accepts_clean_early_stop_for_insufficient_authorized_budget(self):
        source = ROOT / "outputs/route_layer/r4_codex_independent_pilot_v6"
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "parent"
            shutil.copytree(source, parent)
            state_path = parent / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "insufficient_authorized_budget_for_remaining_route"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            resumed = _resume_state(parent)

        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["parent_requests"], 8)
        self.assertEqual(resumed["new_requests_used"], 0)
        self.assertEqual(resumed["accepted_nodes"][-1], "node_1_4")

    def test_resume_quota_stopped_100x100_task_from_accepted_prefix_with_new_budget(self):
        parent = ROOT / "outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1"

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "resumed"
            with (
                patch("run_r4_codex_pilot_continuation.OUTPUT_DIR", output_dir),
                redirect_stdout(StringIO()),
            ):
                preflight_only(resume_from=parent, authorized_request_limit=112)
            resumed = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["accepted_nodes"][-1], "node_68_41")
        self.assertEqual(len(resumed["accepted_edges"]), 1)
        self.assertEqual(resumed["parent_requests"], 1)
        self.assertEqual(resumed["authorized_new_request_limit"], 112)
        self.assertEqual(resumed["initial_remaining_minimum_moves"], 108)
        self.assertEqual(resumed["map_equivalence_audit"]["expected_directed_edge_count"], 10853)
        self.assertIsNone(resumed.get("account_usage_guard"))
        self.assertEqual(resumed["parent_account_usage_guard"]["stop_threshold_percent"], 13)
        self.assertEqual(manifest["api_calls_made_by_this_preflight"], 0)

    def test_resume_can_start_a_fresh_persistent_session_for_one_step_requests(self):
        parent = ROOT / "outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1"

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "resumed-persistent"
            with (
                patch("run_r4_codex_pilot_continuation.OUTPUT_DIR", output_dir),
                redirect_stdout(StringIO()),
            ):
                preflight_only(
                    resume_from=parent,
                    authorized_request_limit=112,
                    codex_session_mode="persistent_per_task",
                )
            resumed = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["codex_session_mode"], "persistent_per_task")
        self.assertIsNone(resumed["codex_session_id"])
        self.assertEqual(manifest["codex_cli_session_mode"], "persistent_per_task")
        self.assertEqual(
            manifest["per_step_map_payload"],
            "full compact map only on initial request; later requests send q/s state and current dynamic directions",
        )
        self.assertEqual(manifest["api_calls_made_by_this_preflight"], 0)

    def test_resume_rejects_quota_stopped_parent_without_a_matching_guard_record(self):
        source = ROOT / "outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1"
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "parent"
            parent.mkdir()
            state = json.loads((source / "state.json").read_text(encoding="utf-8"))
            state["account_usage_guard"]["requests_used_when_stopped"] = 0
            (parent / "state.json").write_text(json.dumps(state), encoding="utf-8")
            shutil.copyfile(source / "final_audit.json", parent / "final_audit.json")

            with self.assertRaisesRegex(RuntimeError, "resume_parent_not_cleanly_capped"):
                _resume_state(parent, authorized_request_limit=108)

    def test_resume_reports_when_suffix_witness_exceeds_authorized_budget(self):
        parent = ROOT / "outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1"
        with self.assertRaisesRegex(
            RuntimeError,
            "resume_parent_insufficient_authorized_budget_for_remaining_route",
        ):
            _resume_state(parent, authorized_request_limit=108)

    def test_user_stopped_task9_parent_requires_fresh_explicit_authorization_to_resume(self):
        parent = ROOT / "outputs/route_layer/r4_codex_task9_stateless_run_20261026_v5"

        with self.assertRaisesRegex(RuntimeError, "resume_parent_not_cleanly_capped"):
            _resume_state(parent, authorized_request_limit=296)

        parent_state_path = parent / "state.json"
        parent_state_before = json.loads(parent_state_path.read_text(encoding="utf-8"))
        resumed = _resume_state(
            parent,
            authorized_request_limit=296,
            user_authorized_user_stop_resume=True,
        )
        parent_state_after = json.loads(parent_state_path.read_text(encoding="utf-8"))

        self.assertEqual(parent_state_after, parent_state_before)
        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(len(resumed["accepted_edges"]), 216)
        self.assertEqual(resumed["accepted_nodes"][-1], "node_15_33")
        self.assertEqual(resumed["pickup_seen"], False)
        self.assertEqual(resumed["parent_requests"], 80)
        self.assertEqual(resumed["cumulative_parent_cli_invocations"], 216)
        self.assertEqual(resumed["unresolved_parent_request_numbers_not_replayed"], [])
        self.assertEqual(resumed.get("resume_prefix_rollbacks", []), [])
        self.assertTrue(resumed["suffix_witness_prefix_audit"]["passed"])
        self.assertEqual(resumed["suffix_witness_prefix_audit"]["accepted_action_count"], 216)
        self.assertGreater(resumed["suffix_witness_prefix_audit"]["minimum_witnessed_remaining_moves"], 0)
        self.assertGreater(len(resumed["initial_continuation_allowed_directions"]), 0)

    def test_one_step_keeps_certified_old_prefix_before_searching_again(self):
        source = ROOT / "outputs/route_layer/r4_codex_task9_stateless_run_20261026_v5"
        state = json.loads((source / "state.json").read_text(encoding="utf-8"))
        state.update({
            "status": "ready",
            "additional_model_requests_authorized": True,
            "authorized_new_request_limit": 512,
            "effective_authorized_new_request_limit": 512,
        })
        directions_seen = []

        class FakeClient:
            def __init__(self, **kwargs):
                self.route_constraint = kwargs["route_constraint"]
                self.call_count = 0

            def chat(self, messages):
                direction = sorted(self.route_constraint.edge_by_direction)[0]
                directions_seen.append(direction)
                return SimpleNamespace(
                    content=json.dumps({
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
                    }),
                    provider_audit={"event_audit": {"tool_item_types": []}},
                    session_id=None,
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=10,
                    latency_ms=1.0,
                )

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            with (
                patch("run_r4_codex_pilot_continuation.OUTPUT_DIR", output_dir),
                patch("run_r4_codex_pilot_continuation._refresh_replay"),
                patch(
                    "run_r4_codex_pilot_continuation.CodexCliRouteStepClient",
                    FakeClient,
                ),
                redirect_stdout(StringIO()),
            ):
                one_step()
            resumed = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(directions_seen.__len__(), 1)
        self.assertEqual(len(resumed["accepted_edges"]), 217)
        self.assertEqual(resumed["attempts"][-1]["current_node_id"], state["accepted_nodes"][-1])
        self.assertEqual(resumed["accepted_nodes"][-2], state["accepted_nodes"][-1])
        self.assertEqual(resumed["attempts"][-1]["target_node_id"], resumed["accepted_nodes"][-1])
        self.assertEqual(resumed["attempts"][-1]["status"], "accepted_legal_step")
        self.assertEqual(resumed["attempts"][-1]["rollback_checkpoint"]["accepted_edge_count"], 216)
        self.assertEqual(resumed.get("resume_prefix_rollbacks", []), [])
        self.assertTrue(resumed["suffix_witness_prefix_audit"]["passed"])

    def test_user_stopped_resume_sets_effective_authorized_request_limit(self):
        parent = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v8"

        resumed = _resume_state(
            parent,
            authorized_request_limit=512,
            user_authorized_user_stop_resume=True,
        )

        self.assertEqual(resumed["effective_authorized_new_request_limit"], 512)
        self.assertTrue(resumed["additional_model_requests_authorized"])
        self.assertEqual(resumed["current_authorization_status"], "user_authorized_resume")

    def test_user_authorized_resume_from_unknown_delivery_parent_never_replays_unknown_call(self):
        source = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v8"
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "v8"
            shutil.copytree(source, parent)

            with self.assertRaisesRegex(RuntimeError, "resume_parent_not_cleanly_capped"):
                _resume_state(parent, authorized_request_limit=512)

            resumed = _resume_state(
                parent,
                authorized_request_limit=512,
                user_authorized_user_stop_resume=True,
            )

        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["parent_requests"], 5)
        self.assertEqual(resumed["cumulative_parent_requests"], 29)
        self.assertEqual(resumed["cumulative_parent_cli_invocations"], 29)
        self.assertEqual(len(resumed["accepted_edges"]), 28)
        self.assertEqual(resumed["accepted_nodes"][-1], "node_54_54")
        self.assertEqual(resumed["new_requests_used"], 0)
        self.assertEqual(resumed["attempts"], [])
        self.assertEqual(resumed["unresolved_parent_request_numbers_not_replayed"], [5])
        self.assertEqual(resumed["authorized_new_request_limit"], 512)
        self.assertEqual(resumed["effective_authorized_new_request_limit"], 512)
        self.assertTrue(resumed["additional_model_requests_authorized"])

    def test_formal_batch_resume_requires_formal_batch_authorization_marker(self):
        source = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v8"
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "formal-parent"
            shutil.copytree(source, parent)
            state_path = parent / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["user_authorization_status"] = "superseded_by_latest_user_stop_instruction"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "resume_formal_batch_authorization_missing"):
                _resume_state(
                    parent,
                    authorized_request_limit=512,
                    user_authorized_formal_task_resume=True,
                )

    def test_formal_batch_resume_continues_validated_prefix_without_replaying_unknown_call(self):
        source = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v8"
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "formal-parent"
            shutil.copytree(source, parent)
            state_path = parent / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["user_authorization_status"] = "granted_for_formal_r4_batch_pilot"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            resumed = _resume_state(
                parent,
                authorized_request_limit=512,
                user_authorized_formal_task_resume=True,
            )

        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["task_id"], "task-0000-0009")
        self.assertEqual(len(resumed["accepted_edges"]), 28)
        self.assertEqual(resumed["accepted_nodes"][-1], "node_54_54")
        self.assertEqual(resumed["unresolved_parent_request_numbers_not_replayed"], [5])
        self.assertEqual(resumed["new_requests_used"], 0)
        self.assertEqual(resumed["attempts"], [])
        _, _, _, package = _load_frozen_task(Path(resumed["source_preflight"]))
        natural_limit = _natural_simple_path_limit(package)
        self.assertEqual(resumed["authorized_new_request_limit"], natural_limit)
        self.assertEqual(resumed["effective_authorized_new_request_limit"], natural_limit)
        self.assertEqual(resumed["route_action_budget"], natural_limit)
        self.assertEqual(resumed["unbounded_authorization_applied"]["natural_simple_path_limit"], natural_limit)
        self.assertEqual(
            resumed["user_authorization_status"],
            "granted_for_formal_r4_batch_pilot",
        )
        self.assertTrue(resumed["additional_model_requests_authorized"])

    def test_user_stopped_resume_preserves_multiple_unknown_invocations_without_replaying(self):
        attempts = [
            {"request_number": 1, "status": "accepted_legal_step", "retry_count": 0},
            {
                "request_number": 2,
                "step_number": 2,
                "current_node_id": "node_1_0",
                "status": "delivery_and_usage_unknown",
                "response": None,
                "route_action_executed": False,
                "retry_count": 0,
                "provider_record": {
                    "delivery_and_usage_unknown_evidence": True,
                    "status": "interrupted_before_turn_completion",
                },
            },
            {"request_number": 3, "status": "accepted_legal_step", "retry_count": 0},
            {
                "request_number": 4,
                "step_number": 3,
                "current_node_id": "node_2_0",
                "status": "delivery_and_usage_unknown",
                "response": None,
                "route_action_executed": False,
                "retry_count": 0,
                "provider_record": {
                    "delivery_and_usage_unknown_evidence": True,
                    "status": "interrupted_before_turn_completion",
                },
            },
        ]
        parent = {
            "status": "stopped_by_user_instruction",
            "new_requests_used": 4,
            "retry_count": 0,
            "attempts": attempts,
            "accepted_edges": ["edge_0_1", "edge_1_2"],
            "accepted_nodes": ["node_0_0", "node_1_0", "node_2_0"],
        }
        audit = {
            "status": "partial_legal_prefix_not_complete",
            "retries": 0,
            "codex_cli_invocations_this_continuation": 4,
            "actual_model_requests_this_continuation": 2,
            "route_continuity_violations": [],
            "additional_model_requests_authorized": False,
            "delivery_unknown_request_numbers": [2, 4],
            "usage_unknown_request_numbers": [2, 4],
            "tool_audit_unknown_request_numbers": [2, 4],
            "tool_events": None,
            "tool_audit_complete": False,
            "accepted_move_count_including_prior_smoke": 2,
            "current_node": "node_2_0",
        }

        self.assertEqual(
            _safe_unresolved_delivery_resume_parent(parent, audit),
            [2, 4],
        )

    def test_resume_without_explicit_authorization_keeps_effective_limit_at_zero(self):
        parent = ROOT / "outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1"
        resumed = _resume_state(parent, authorized_request_limit=112)

        self.assertEqual(resumed["authorized_new_request_limit"], 112)
        self.assertEqual(resumed["effective_authorized_new_request_limit"], 0)
        self.assertFalse(resumed["additional_model_requests_authorized"])

    def test_one_step_refuses_zero_effective_authorization_before_client_creation(self):
        source = ROOT / "outputs/route_layer/r4_codex_task9_progress_feedback_v3_resume_from_7_20261026_v1"

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "run"
            shutil.copytree(source, output_dir)
            state_path = output_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.update({
                "status": "ready",
                "effective_authorized_new_request_limit": 0,
                "additional_model_requests_authorized": False,
                "new_requests_used": 0,
                "attempts": [],
                "accepted_nodes": state["accepted_nodes"][:-1],
                "accepted_edges": state["accepted_edges"][:-1],
                "accepted_directions": state["accepted_directions"][:-1],
            })
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with (
                patch("run_r4_codex_pilot_continuation.OUTPUT_DIR", output_dir),
                patch("run_r4_codex_pilot_continuation._refresh_replay"),
                patch("run_r4_codex_pilot_continuation.CodexCliRouteStepClient") as client_class,
            ):
                with self.assertRaisesRegex(RuntimeError, "model_request_not_authorized"):
                    one_step()

            client_class.assert_not_called()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "awaiting_user_authorization")

    def test_resume_v3_rejects_parent_with_failed_local_edge_check(self):
        source = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3"
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "parent"
            shutil.copytree(source, parent)
            state_path = parent / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["attempts"][-1]["validation"]["deterministic_local_check"]["passed"] = False
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "resume_self_check_mismatch_parent_not_safe"):
                _resume_state(parent, allow_unexecuted_self_check_mismatch=True)


if __name__ == "__main__":
    unittest.main()
