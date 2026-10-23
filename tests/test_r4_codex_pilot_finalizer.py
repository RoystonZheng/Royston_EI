import sys
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from finalize_r4_codex_pilot_continuation import (  # noqa: E402
    _validate_attempt_trace_alignment,
    _collect_self_check_discrepancies,
    finalize,
)


class R4CodexPilotFinalizerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
