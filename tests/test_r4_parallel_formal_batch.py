import hashlib
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from r4_parallel_formal_batch import (  # noqa: E402
    _has_runnable_tasks,
    _launchd_active,
    _reconciled_saved_response_evidence,
    _route_safety_gate_block_reason,
    _task_with_reconciled_saved_state,
    start_workers,
)
from run_r4_formal_chunk_driver import route_safety_gate_block_reason as shard_gate_block_reason  # noqa: E402
from r4_route_safety_gate import REQUIRED_SOURCE_FILES  # noqa: E402


class R4ParallelSavedResponseReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.worker_root = Path(self.temp.name)
        self.run = self.worker_root / "tasks" / "seed_0" / "task-test" / "run_000"
        self.trace = self.run / "traces" / "r4_step_0001.jsonl"
        self.trace.parent.mkdir(parents=True)
        self.events = [
            {"type": "thread.started", "thread_id": "thread-test"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"action":"move","direction":"E"}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 5}},
        ]
        self.trace.write_text("".join(json.dumps(event) + "\n" for event in self.events), encoding="utf-8")
        self.trace_sha = hashlib.sha256(self.trace.read_bytes()).hexdigest()
        (self.run / "state.json").write_text(json.dumps({
            "task_id": "task-test",
            "status": "ready",
            "new_requests_used": 1,
            "reconciled_saved_response_request_numbers": [1],
            "attempts": [{
                "request_number": 1,
                "status": "accepted_legal_step",
                "trace_sha256": self.trace_sha,
                "reconciled_from_saved_complete_response": True,
                "tool_item_types": [],
                "provider_record": {"usage_available": True},
                "usage": {"input_tokens": 100, "output_tokens": 5},
                "response": {"action": "move", "direction": "E"},
                "allowed_directions": ["E"],
                "allowed_edge_ids": {"E": "edge-test"},
                "selected_edge_id": "edge-test",
                "validation": {
                    "deterministic_local_check": {"passed": True},
                    "direction_in_dynamic_schema": True,
                },
            }],
        }), encoding="utf-8")
        self.row = {
            "task_id": "task-test",
            "expected_request_number": 1,
            "status": "outcome_unknown_no_retry",
            "trace_path": "tasks/seed_0/task-test/run_000/traces/r4_step_0001.jsonl",
            "trace_sha256": self.trace_sha,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_marks_unknown_delivery_resolved_only_with_saved_validated_turn(self):
        evidence = _reconciled_saved_response_evidence(self.worker_root, self.row)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["request_number"], 1)
        self.assertEqual(evidence["input_tokens"], 100)

    def test_does_not_reconcile_without_matching_trace_hash(self):
        row = dict(self.row, trace_sha256="0" * 64)
        self.assertIsNone(_reconciled_saved_response_evidence(self.worker_root, row))

    def test_repairs_stale_task_status_from_quiescent_accepted_state(self):
        task = {
            "task_id": "task-test",
            "status": "outcome_unknown",
            "requests_used": 0,
            "runs": [{"output_dir": str(self.run), "requests_used": 0}],
        }
        repaired = _task_with_reconciled_saved_state(task, self.worker_root)
        self.assertEqual(repaired["status"], "ready")
        self.assertEqual(repaired["requests_used"], 1)
        self.assertEqual(repaired["runs"][0]["requests_used"], 1)

    def test_keeps_active_request_state_unmodified(self):
        state_path = self.run / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "request_in_progress"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        task = {
            "task_id": "task-test",
            "status": "outcome_unknown",
            "requests_used": 0,
            "runs": [{"output_dir": str(self.run), "requests_used": 0}],
        }
        repaired = _task_with_reconciled_saved_state(task, self.worker_root)
        self.assertEqual(repaired, task)


class R4ParallelWorkerResumeTests(unittest.TestCase):
    def test_unknown_and_duplicate_suppressed_rows_do_not_restart_a_shard(self):
        manifest = {
            "tasks": [
                {"status": "route_complete"},
                {"status": "failed"},
                {"status": "outcome_unknown"},
                {"status": "duplicate_suppressed"},
            ]
        }
        self.assertFalse(_has_runnable_tasks(manifest))

    def test_pending_or_resumable_rows_keep_a_shard_runnable(self):
        for status in ("pending", "ready", "active", "budget_paused"):
            with self.subTest(status=status):
                self.assertTrue(_has_runnable_tasks({"tasks": [{"status": status}]}))


class R4ParallelSafetyGateTests(unittest.TestCase):
    def test_pending_safety_gate_blocks_parallel_worker_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({
                "route_safety_gate": {
                    "status": "pending",
                    "formal_continuation_permitted": False,
                },
                "in_flight": None,
            }), encoding="utf-8")
            with mock.patch("r4_parallel_formal_batch._assert_original_runner_stopped"):
                with self.assertRaisesRegex(RuntimeError, "r4_route_safety_gate_not_passed"):
                    start_workers(root, 53, None, root / "pilot.json", 128)

    def test_undeclared_safety_gate_does_not_block_legacy_batches(self):
        self.assertIsNone(_route_safety_gate_block_reason({}))
        self.assertIsNotNone(_route_safety_gate_block_reason({
            "route_safety_gate": {"status": "passed", "formal_continuation_permitted": True}
        }))

    def test_only_current_complete_acceptance_evidence_unlocks_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = {
                "time_priority_0_6_0_4": {
                    "candidate_count": 3, "selected_candidate_id": "fast",
                    "selection_reason": "minimum normalized score", "ev2_status": "passed",
                    "sumo_status": "passed", "sumo_one_way_violations": 0,
                },
                "money_priority_0_4_0_6": {
                    "candidate_count": 3, "selected_candidate_id": "efficient",
                    "selection_reason": "minimum normalized score", "ev2_status": "passed",
                    "sumo_status": "passed", "sumo_one_way_violations": 0,
                },
            }
            evidence = {
                "schema_version": "r4_route_safety_acceptance_v2", "status": "passed", "model_calls": 0,
                "checks": {
                    "online_step_chain": {"status": "passed", "formal_entrypoint_guarded": True,
                        "direct_one_step_guarded": True, "local_edge_validator_enabled": True},
                    "zero_call_recovery_replay": {"status": "passed", "model_calls": 0,
                        "illegal_candidate_rejected": True, "rollback_count_nodes": 1,
                        "reroute_from_rollback_node": True, "completed_route": True, "one_way_violations": 0},
                    "full_flow_sumo": {"status": "passed", "vehicle_completed": True,
                        "route_edge_sequence_matched": True, "one_way_violations": 0,
                        "sumo_log_sha256": "a" * 64, "validator_audit_sha256": "b" * 64},
                    "paired_candidate_decision": {"status": "passed", "candidate_pool_sha256": "c" * 64,
                        "profiles": profiles},
                },
                "source_sha256": {
                    relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                    for relative in REQUIRED_SOURCE_FILES
                },
                "config_sha256": {"frozen_input": "d" * 64},
            }
            artifact_path = root / "acceptance.json"
            artifact_path.write_text(json.dumps(evidence), encoding="utf-8")
            artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            gate = {
                "schema_version": "r4_route_safety_gate_v2", "status": "passed",
                "formal_continuation_permitted": True,
                "acceptance_artifact": {"path": str(artifact_path), "sha256": artifact_sha},
            }
            self.assertIsNone(_route_safety_gate_block_reason({"route_safety_gate": gate}))
            for profile in profiles.values():
                profile["candidate_count"] = 1
                profile["single_candidate_no_weight_effect"] = True
                profile["selection_reason"] = "only feasible candidate; profile weights have no comparative effect"
            evidence["checks"]["paired_candidate_decision"]["profiles"] = profiles
            artifact_path.write_text(json.dumps(evidence), encoding="utf-8")
            gate["acceptance_artifact"]["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            self.assertIsNone(_route_safety_gate_block_reason({"route_safety_gate": gate}))
            profiles["time_priority_0_6_0_4"]["single_candidate_no_weight_effect"] = False
            artifact_path.write_text(json.dumps(evidence), encoding="utf-8")
            gate["acceptance_artifact"]["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            self.assertIn("single_candidate_weight_effect_not_disclosed", _route_safety_gate_block_reason({"route_safety_gate": gate}))
            profiles.pop("money_priority_0_4_0_6")
            evidence["checks"]["paired_candidate_decision"]["profiles"] = profiles
            artifact_path.write_text(json.dumps(evidence), encoding="utf-8")
            gate["acceptance_artifact"]["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            self.assertIsNotNone(_route_safety_gate_block_reason({"route_safety_gate": gate}))

    def test_direct_shard_launch_reads_and_honors_parent_safety_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "parallel_workers" / "worker_02"
            worker.mkdir(parents=True)
            parent = root / "manifest.json"
            parent.write_text(json.dumps({
                "route_safety_gate": {
                    "status": "pending",
                    "formal_continuation_permitted": False,
                },
            }), encoding="utf-8")
            self.assertEqual(
                shard_gate_block_reason(worker),
                "r4_route_safety_gate_not_passed:current_acceptance_evidence_missing",
            )
            parent.write_text(json.dumps({
                "route_safety_gate": {
                    "status": "passed",
                    "formal_continuation_permitted": True,
                },
            }), encoding="utf-8")
            self.assertEqual(
                shard_gate_block_reason(worker),
                "r4_route_safety_gate_not_passed:current_acceptance_evidence_missing",
            )

    def test_launchd_spawn_scheduled_is_not_counted_as_a_live_worker(self):
        with mock.patch(
            "r4_parallel_formal_batch._launchd_print",
            return_value=(True, "state = spawn scheduled\nlast exit code = 2\n"),
        ):
            self.assertFalse(_launchd_active("worker-test"))
        with mock.patch(
            "r4_parallel_formal_batch._launchd_print",
            return_value=(True, "state = running\n"),
        ):
            self.assertTrue(_launchd_active("worker-test"))


if __name__ == "__main__":
    unittest.main()
