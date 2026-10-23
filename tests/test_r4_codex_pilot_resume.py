import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_r4_codex_pilot_continuation import (  # noqa: E402
    _authorized_call_budget_stop_reason,
    _initial_state,
    _model_self_check_all_true,
    _model_self_check_is_well_formed,
    _resume_state,
)


class R4CodexPilotResumeTests(unittest.TestCase):
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
