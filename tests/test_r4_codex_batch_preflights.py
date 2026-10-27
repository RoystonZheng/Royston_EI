import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from prepare_r4_codex_batch_preflights import build_task_preflight, export_batch_preflights
from run_r4_codex_pilot_continuation import _load_frozen_task


class R4CodexBatchPreflightTests(unittest.TestCase):
    def setUp(self):
        self.summary_path = ROOT / "outputs/route_layer/r4_codex_all_frozen_tasks_recheck_20261024/summary.json"
        self.summary = json.loads(self.summary_path.read_text(encoding="utf-8"))

    def test_semantic_order_preflight_reuses_frozen_qwen_endpoint_without_truth_leak(self):
        row = next(item for item in self.summary["task_rows"] if item["task_id"] == "task-0000-0009")
        preflight = build_task_preflight(row)

        self.assertEqual(preflight["task_public"]["task_id"], "task-0000-0009")
        self.assertEqual(preflight["route_search"]["generation_mode"], "stepwise")
        self.assertEqual(preflight["evaluation_truth"], {
            "true_pickup_node_id": "node_25_66",
            "true_dropoff_node_id": "node_42_43",
        })
        payload = json.loads(preflight["route_search"]["messages"][-1]["content"])
        self.assertEqual(payload["q"]["p"], "node_25_66")
        self.assertEqual(payload["q"]["d"], "node_42_43")
        self.assertEqual(payload["i"]["protocol"]["version"], "v1")
        self.assertTrue(preflight["route_search"]["map_equivalence_audit"]["passed"])
        self.assertFalse(preflight["route_search"]["evaluator_truth_in_prompt"])
        self.assertNotIn("true_pickup_node_id", preflight["route_search"]["rendered_prompt"])

    def test_batch_export_keeps_all_150_denominator_and_only_prepares_129_routeable_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "batch_preflight"
            manifest = export_batch_preflights(self.summary, output)

            self.assertEqual(manifest["task_count"], 150)
            self.assertEqual(manifest["routeable_task_count"], 129)
            self.assertEqual(manifest["skipped_task_count"], 21)
            self.assertEqual(manifest["model_requests"], 0)
            self.assertEqual(len(list((output / "tasks").rglob("preflight.json"))), 129)
            self.assertEqual(len(manifest["skipped_tasks"]), 21)

    def test_formal_scenario_preflight_preserves_scenario_blocked_edge_set(self):
        row = next(
            item for item in self.summary["task_rows"]
            if item["sample_group"] == "formal_scenarios"
            and item["scenario_type"] == "feasible"
            and item.get("route_chain_reachable") is True
        )
        preflight = build_task_preflight(row)
        self.assertEqual(preflight["task_public"]["scenario_type"], "feasible")
        self.assertEqual(preflight["route_search"]["blocked_edge_ids"], [])
        self.assertEqual(preflight["evaluation_truth"]["true_pickup_node_id"], row["pickup_node_id"])

    def test_file_referenced_map_and_blocked_edges_load_in_stepwise_runner(self):
        row = next(
            item for item in self.summary["task_rows"]
            if item["sample_group"] == "formal_scenarios"
            and item["scenario_type"] == "detour"
            and item.get("route_chain_reachable") is True
        )
        preflight = build_task_preflight(row)
        map_path = ROOT / preflight["map_ref"]["path"]
        first_edge_id = json.loads(map_path.read_text(encoding="utf-8"))["edges"][0]["edge_id"]
        preflight["route_search"]["blocked_edge_ids"] = [first_edge_id]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preflight.json"
            path.write_text(json.dumps(preflight), encoding="utf-8")
            loaded, road_map, task, package = _load_frozen_task(path)

        self.assertEqual(loaded["map_ref"]["sha256"], preflight["map_ref"]["sha256"])
        self.assertEqual(task.task_id, row["task_id"])
        self.assertEqual(package.blocked_edge_ids, [first_edge_id])
        self.assertNotIn(first_edge_id, {edge.edge_id for edge in package.edges})


if __name__ == "__main__":
    unittest.main()
