import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from r4_parallel_formal_batch import (  # noqa: E402
    _reconciled_saved_response_evidence,
    _task_with_reconciled_saved_state,
)


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


if __name__ == "__main__":
    unittest.main()
