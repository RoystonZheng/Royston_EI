import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from finalize_r4_codex_pilot_continuation import (  # noqa: E402
    _collect_attempt_traces,
    _reconcile_trace_usage,
    _validate_attempt_trace_alignment,
    _collect_self_check_discrepancies,
    _collect_model_failure_code_overrides,
    _combine_cumulative_usage,
    finalize,
    load_map,
)


class R4CodexPilotFinalizerTests(unittest.TestCase):
    def test_cumulative_usage_stays_unknown_after_parent_unknown_invocation(self):
        parent_audit = {
            "cumulative_input_tokens_including_bootstrap_and_parent": None,
            "cumulative_output_tokens_including_bootstrap_and_parent": None,
            "cumulative_codex_standard_credit_equivalent_including_bootstrap_and_parent": None,
            "cumulative_api_equivalent_usd_including_bootstrap_and_parent": None,
        }

        self.assertIsNone(_combine_cumulative_usage(
            16865,
            parent_audit,
            "cumulative_input_tokens_including_bootstrap_and_parent",
            has_parent=True,
            fallback_prior=471518,
        ))
        self.assertIsNone(_combine_cumulative_usage(
            1569,
            parent_audit,
            "cumulative_output_tokens_including_bootstrap_and_parent",
            has_parent=True,
            fallback_prior=11602,
        ))
        self.assertIsNone(_combine_cumulative_usage(
            0.1,
            parent_audit,
            "cumulative_codex_standard_credit_equivalent_including_bootstrap_and_parent",
            has_parent=True,
            fallback_prior=3.0,
        ))

    def test_partial_final_response_is_tracked_without_inventing_a_jsonl_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            trace_dir = run_dir / "traces"
            trace_dir.mkdir()
            (trace_dir / "r4_step_0001.jsonl").write_text(
                '{"type":"turn.started"}\n{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":2}}\n',
                encoding="utf-8",
            )
            final_path = trace_dir / "r4_step_0002.final.json"
            response = {"action": "move", "direction": "W"}
            final_path.write_text(json.dumps(response), encoding="utf-8")
            attempts = [
                {"request_number": 1, "provider_record": {"status": "accepted"}},
                {
                    "request_number": 2,
                    "response": response,
                    "provider_record": {
                        "status": "recovered_final_file_only",
                        "partial_final_response_evidence": True,
                        "final_json_path": str(final_path),
                        "final_json_sha256": __import__("hashlib").sha256(final_path.read_bytes()).hexdigest(),
                    },
                },
            ]

            rows, evidence_paths = _collect_attempt_traces(run_dir, attempts)

        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["trace_available"])
        self.assertFalse(rows[1]["trace_available"])
        self.assertIsNone(rows[1]["input_tokens"])
        self.assertIsNone(rows[1]["tool_item_types"])
        self.assertIn("r4_step_0002.final.json", evidence_paths[1])

    def test_interrupted_invocation_without_response_remains_delivery_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            trace_dir = run_dir / "traces"
            trace_dir.mkdir()
            prompt = trace_dir / "r4_step_0001.prompt.txt"
            schema = trace_dir / "r4_step_0001.schema.json"
            prompt.write_text("request prompt", encoding="utf-8")
            schema.write_text("{}", encoding="utf-8")
            provider = {
                "status": "delivery_and_usage_unknown",
                "delivery_and_usage_unknown_evidence": True,
                "prompt_path": str(prompt),
                "prompt_sha256": __import__("hashlib").sha256(prompt.read_bytes()).hexdigest(),
                "schema_path": str(schema),
                "schema_sha256": __import__("hashlib").sha256(schema.read_bytes()).hexdigest(),
            }

            rows, evidence_paths = _collect_attempt_traces(
                run_dir,
                [{"request_number": 1, "provider_record": provider}],
            )

        self.assertFalse(rows[0]["trace_available"])
        self.assertTrue(rows[0]["delivery_and_usage_unknown"])
        self.assertEqual(rows[0]["turn_started_count"], 0)
        self.assertIsNone(rows[0]["tool_item_types"])
        self.assertIn("r4_step_0001.prompt.txt", evidence_paths[0])

    def test_started_turn_without_completion_keeps_usage_and_tools_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            trace_dir = run_dir / "traces"
            trace_dir.mkdir()
            (trace_dir / "r4_step_0001.jsonl").write_text(
                '{"type":"thread.started","thread_id":"thread-1"}\n'
                '{"type":"turn.started"}\n',
                encoding="utf-8",
            )

            rows, _ = _collect_attempt_traces(
                run_dir,
                [{"request_number": 1, "provider_record": {"status": "interrupted"}}],
            )

        self.assertTrue(rows[0]["delivery_and_usage_unknown"])
        self.assertEqual(rows[0]["turn_started_status"], "started_without_completion")
        self.assertEqual(rows[0]["turn_started_count"], 1)
        self.assertEqual(rows[0]["turn_completed_usage_count"], 0)
        self.assertIsNone(rows[0]["input_tokens"])
        self.assertIsNone(rows[0]["output_tokens"])
        self.assertIsNone(rows[0]["tool_item_types"])

    def test_self_check_audit_ignores_attempt_without_response(self):
        unknown_attempt = {
            "request_number": 9,
            "status": "delivery_and_usage_unknown",
            "response": None,
            "validation": {},
            "provider_record": {"delivery_and_usage_unknown_evidence": True},
        }

        self.assertEqual(_collect_self_check_discrepancies([unknown_attempt]), [])
        self.assertEqual(_collect_model_failure_code_overrides([unknown_attempt]), [])

    def test_persistent_session_cumulative_trace_usage_is_differenced_once(self):
        raw_inputs = [16390, 33281, 50541, 68266, 86458, 105079]
        raw_cached = [0, 0, 0, 0, 16768, 33792]
        raw_outputs = [252, 311, 467, 676, 789, 865]
        traces = [
            {
                "turn_started_count": 1,
                "turn_completed_usage_count": 1,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "output_tokens": output_tokens,
            }
            for input_tokens, cached_tokens, output_tokens in zip(raw_inputs, raw_cached, raw_outputs)
        ]
        attempts = [{"codex_session_id": "same-session"} for _ in traces]

        rows = _reconcile_trace_usage(traces, attempts, persistent_session=True)

        self.assertEqual(sum(row["input_tokens"] for row in rows), 105079)
        self.assertEqual(sum(row["cached_input_tokens"] for row in rows), 33792)
        self.assertEqual(sum(row["output_tokens"] for row in rows), 865)
        self.assertEqual(rows[1]["input_tokens"], 16891)
        self.assertEqual(rows[5]["output_tokens"], 76)
        self.assertEqual(rows[5]["reported_session_cumulative_usage"]["input_tokens"], 105079)

    def test_ephemeral_trace_usage_is_not_differenced(self):
        rows = _reconcile_trace_usage(
            [{"turn_started_count": 1, "turn_completed_usage_count": 1,
              "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 5}],
            [{}],
            persistent_session=False,
        )

        self.assertEqual(rows[0]["input_tokens"], 100)
        self.assertEqual(rows[0]["usage_accounting_basis"], "reported_per_invocation")

    def test_finalizer_loads_and_hash_checks_file_referenced_map(self):
        task_preflight = ROOT / "outputs/route_layer/r4_codex_batch_preflights_20261024_v1/tasks/seed_0/task-0000-0009/preflight.json"
        payload = json.loads(task_preflight.read_text(encoding="utf-8"))
        road_map = load_map(task_preflight)
        self.assertEqual(road_map.map_id, "grid-100x100-seed-0")

        payload["map_ref"]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            altered = Path(directory) / "preflight.json"
            altered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "frozen_map_reference_hash_mismatch"):
                load_map(altered)

    def test_distinguishes_cli_error_without_model_turn_from_metered_request(self):
        attempts = [
            {"provider_record": {"status": "response_received"}},
            {"provider_record": {"status": "unexpected_model_turn_count"}},
        ]
        traces = [
            {"turn_started_count": 1},
            {"turn_started_count": 0},
        ]

        self.assertEqual(_validate_attempt_trace_alignment(attempts, traces, 2), (1, 2))
        with self.assertRaisesRegex(RuntimeError, "attempt_trace_request_count_mismatch"):
            _validate_attempt_trace_alignment(attempts, traces, 1)

    def test_collects_self_check_false_positive_separately_from_illegal_actions(self):
        attempt = {
            "request_number": 6,
            "status": "accepted_legal_step",
            "response": {
                "self_check": {
                    "allowed_directed_edge": False,
                    "starts_at_current": True,
                    "inside_boundary": False,
                    "pickup_order_ok": True,
                    "target_node_is_new": True,
                }
            },
            "validation": {
                "deterministic_local_check": {
                    "passed": True,
                    "flags": {
                        "allowed_directed_edge": True,
                        "starts_at_current": True,
                        "inside_boundary": True,
                        "pickup_order_ok": True,
                        "not_revisited": True,
                    },
                }
            },
        }

        discrepancies = _collect_self_check_discrepancies([attempt])

        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(
            discrepancies[0]["model_self_check_mismatch_fields"],
            ["allowed_directed_edge", "inside_boundary"],
        )
        self.assertTrue(discrepancies[0]["deterministic_local_check_passed"])
        self.assertTrue(discrepancies[0]["action_executed"])

    def test_collects_model_failure_claim_overridden_by_local_validator(self):
        attempt = {
            "request_number": 9,
            "status": "accepted_legal_step",
            "response": {"failure_reason_code": "unable_to_construct_route"},
            "validation": {
                "model_failure_code_overridden_by_local_validation": True,
                "deterministic_local_check": {"passed": True},
            },
        }

        overrides = _collect_model_failure_code_overrides([attempt])

        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["failure_reason_code"], "unable_to_construct_route")
        self.assertTrue(overrides[0]["action_executed"])

    def test_fresh_bounded_run_manifest_and_cumulative_usage_are_finalized(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5"
        audit = finalize(run_dir, None, "gpt-5.6-luna")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(audit["manifest_status"], "partial_legal_prefix_not_complete")
        self.assertEqual(audit["cumulative_input_tokens_including_bootstrap_and_parent"], 72449)
        self.assertEqual(audit["cumulative_output_tokens_including_bootstrap_and_parent"], 4050)
        self.assertEqual(manifest["status"], "partial_legal_prefix_not_complete")
        self.assertEqual(manifest["actual_model_requests"], 6)
        self.assertEqual(manifest["accepted_legal_actions"], 5)

    def test_v3_cumulative_cost_and_request_totals_include_v2_parent(self):
        audit = finalize(
            ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3",
            ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v2",
            "gpt-5.6-luna",
        )

        self.assertEqual(audit["cumulative_requests_including_bootstrap_and_parent"], 24)
        self.assertEqual(audit["cumulative_input_tokens_including_bootstrap_and_parent"], 305706)
        self.assertEqual(audit["cumulative_output_tokens_including_bootstrap_and_parent"], 28170)
        self.assertAlmostEqual(
            audit["cumulative_codex_standard_credit_equivalent_including_bootstrap_and_parent"],
            2.37363,
        )
        self.assertAlmostEqual(
            audit["cumulative_api_equivalent_usd_including_bootstrap_and_parent"],
            0.0949452,
        )
        stopped = audit["invalid_or_stopped_actions"][0]
        self.assertEqual(audit["minimum_remaining_moves_at_stop"], 8)
        self.assertEqual(stopped["proposed_direction"], "S")
        self.assertEqual(stopped["proposed_target_node_id"], "node_0_3")
        self.assertEqual(stopped["model_self_check_false_fields"], ["not_revisited"])
        self.assertFalse(stopped["action_executed"])
        self.assertEqual(stopped["vehicle_energy_consumed_kwh"], 0.0)

    def test_rejected_one_way_proposal_records_hypothetical_energy_not_consumed_energy(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3"
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "continuation"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            attempt = state["attempts"][-1]
            road_map = load_map(Path(state["source_preflight"]))
            edge_ids = {edge.edge_id for edge in road_map.edges}
            self.assertIn("edge_node_0_5_node_0_4", edge_ids)
            self.assertNotIn("edge_node_0_4_node_0_5", edge_ids)

            attempt["response"]["direction"] = "N"
            local_check = attempt["validation"]["deterministic_local_check"]
            local_check["passed"] = False
            local_check["errors"] = ["direction_not_in_dynamic_schema"]
            local_check["expected_target_node_id"] = "node_0_5"
            attempt["validation"]["direction_in_dynamic_schema"] = False
            state_path.write_text(json.dumps(state), encoding="utf-8")

            audit = finalize(
                run_dir,
                ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v2",
                "gpt-5.6-luna",
            )

        rejected = audit["invalid_or_stopped_actions"][-1]
        self.assertEqual(rejected["vehicle_energy_consumed_kwh"], 0.0)
        self.assertGreater(rejected.get("hypothetical_step_energy_kwh") or 0.0, 0.0)
        self.assertEqual(rejected.get("hypothetical_energy_status"), "estimate_only_not_consumed")
        self.assertEqual(rejected.get("hypothetical_energy_basis", {}).get("reverse_edge_inferred_moves"), 1)

    def test_completed_destination_step_is_counted_as_accepted_not_stopped(self):
        audit = finalize(
            ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v4",
            ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3",
            "gpt-5.6-luna",
        )

        self.assertEqual(audit["status"], "completed_route")
        self.assertTrue(audit["dropoff_reached"])
        self.assertEqual(audit["actual_model_requests_this_continuation"], 8)
        self.assertEqual(audit["accepted_legal_actions_this_continuation"], 8)
        self.assertEqual(audit["invalid_or_stopped_actions"], [])
        self.assertEqual(audit["tool_events"], 0)
        self.assertEqual(audit["retries"], 0)

    def test_independent_task_run_does_not_add_unrelated_bootstrap_smoke_usage(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_independent_pilot_v9"
        audit = finalize(run_dir, None, "gpt-5.6-luna")

        self.assertEqual(audit["actual_model_requests_this_continuation"], 8)
        self.assertEqual(audit["input_tokens_this_continuation"], 77722)
        self.assertEqual(audit["output_tokens_this_continuation"], 8117)
        self.assertEqual(audit["cumulative_requests_including_bootstrap_and_parent"], 8)
        self.assertEqual(audit["cumulative_input_tokens_including_bootstrap_and_parent"], 77722)
        self.assertEqual(audit["cumulative_output_tokens_including_bootstrap_and_parent"], 8117)

    def test_invocation_without_model_turn_keeps_delivery_and_usage_unknown(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v2"
        audit = finalize(run_dir, None, "gpt-5.6-luna")

        self.assertEqual(audit["actual_model_requests_this_continuation"], 21)
        self.assertEqual(audit["codex_cli_invocations_this_continuation"], 22)
        self.assertEqual(audit["delivery_unknown_request_numbers"], [22])
        self.assertEqual(audit["usage_unknown_request_numbers"], [22])
        self.assertFalse(audit["tool_audit_complete"])
        self.assertIsNone(audit["tool_events"])
        self.assertEqual(audit["cli_invocations_without_model_turn"], 1)
        self.assertEqual(audit["invalid_or_stopped_actions"], [])
        self.assertEqual(audit["unresolved_cli_invocation"]["cli_exit_code"], -15)
        self.assertFalse(audit["unresolved_cli_invocation"]["action_executed"])
        self.assertEqual(audit["unresolved_cli_invocation"]["vehicle_energy_consumed_kwh"], 0.0)
        self.assertIsNone(audit["input_tokens_this_continuation"])
        self.assertEqual(audit["known_input_tokens_sum_this_continuation"], 343267)
        self.assertEqual(audit["known_total_tokens_this_continuation"], 353474)
        self.assertIsNone(audit["complete_total_tokens_this_continuation"])
        self.assertIsNone(audit["codex_standard_credit_equivalent_this_continuation"])

    def test_final_audit_preserves_live_account_usage_guard_from_state(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_independent_pilot_v9"
        guard = {
            "baseline_weekly_used_percent": 10,
            "observed_weekly_used_percent_after_request": 10,
            "stop_threshold_percent": 13,
            "max_weekly_usage_increase_percentage_points": 3,
            "credits_balance_changed": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "pilot"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["account_usage_guard"] = guard
            state_path.write_text(json.dumps(state), encoding="utf-8")

            audit = finalize(run_dir, None, "gpt-5.6-luna")

        self.assertEqual(audit["account_usage_guard"], guard)


if __name__ == "__main__":
    unittest.main()
