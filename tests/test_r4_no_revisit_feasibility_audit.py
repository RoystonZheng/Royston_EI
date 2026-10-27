import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from route_experiment.models import VehicleOrderTask
from scripts.run_r4_codex_formal_checkpoint import load_map
from scripts.audit_r4_no_revisit_feasibility import audit_task, render


class R4NoRevisitFeasibilityAuditTests(unittest.TestCase):
    def test_seed0_task9_replacement_witness_passes_shared_validator(self):
        root = Path(__file__).resolve().parents[1]
        seed_dir = root / "outputs/route_layer/traditional_experiment_v1/seeds/seed_0"
        road_map = load_map(seed_dir / "map_100x100_seed_0.json")
        task = VehicleOrderTask(
            task_id="task-0000-0009",
            vehicle_id="pilot",
            order_id="pilot",
            start_node_id="node_69_41",
            pickup_node_id="node_25_66",
            dropoff_node_id="node_42_43",
            task_type="semantic_place",
        )

        result = audit_task(road_map, task)

        self.assertFalse(result["baseline_route_is_simple"])
        self.assertEqual(result["witness_status"], "validated_witness")
        self.assertEqual(result["witness_edge_count"], 111)
        self.assertTrue(result["validator_accepted"])
        self.assertEqual(result["validator_violation_count"], 0)

    def test_audit_index_renders_witness_rows(self):
        row = {
            "seed": 0,
            "task_id": "task-0000-0009",
            "sample_group": "orders",
            "scenario_type": None,
            "baseline_route_is_simple": False,
            "witness_status": "validated_witness",
            "witness_edge_count": 111,
            "validator_accepted": True,
            "witness_node_sequence": ["node_69_41", "node_25_66", "node_42_43"],
        }
        summary = {
            "candidate_chain_reachable_task_count": 1,
            "sequential_witness_found_and_validated_count": 1,
            "baseline_local_dijkstra_routes_with_revisits": 1,
            "revisited_baseline_tasks_with_replacement_witness": 1,
            "witness_edge_count_stats": {"max": 111},
            "task_rows": [row],
        }
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            render(summary, output_dir)
            page = (output_dir / "index.html").read_text(encoding="utf-8")

        self.assertIn("task-0000-0009", page)
        self.assertIn("node_69_41", page)


if __name__ == "__main__":
    unittest.main()
