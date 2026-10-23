import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from finalize_r4_codex_pilot_continuation import finalize  # noqa: E402
from render_r4_codex_pilot_replay import render  # noqa: E402


class R4CodexPilotReplayRendererTests(unittest.TestCase):
    def test_replay_labels_accepted_self_check_mismatch_as_locally_validated(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["parent_run"] = None
            attempt = state["attempts"][-1]
            attempt["status"] = "accepted_legal_step"
            attempt["selected_edge_id"] = "edge_node_4_7_node_4_6"
            attempt["target_node_id"] = "node_4_6"
            attempt["validation"]["model_self_check_mismatch_fields"] = [
                "allowed_directed_edge", "inside_boundary"
            ]
            state["accepted_nodes"].append("node_4_6")
            state["accepted_edges"].append("edge_node_4_7_node_4_6")
            state["accepted_directions"].append("S")
            state_path.write_text(json.dumps(state), encoding="utf-8")

            audit_path = run_dir / "final_audit.json"
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["model_self_check_discrepancies"] = [{
                "request_number": attempt["request_number"],
                "model_self_check_mismatch_fields": [
                    "allowed_directed_edge", "inside_boundary"
                ],
                "deterministic_local_check_passed": True,
                "action_executed": True,
            }]
            audit["model_self_check_discrepancy_count"] = 1
            audit_path.write_text(json.dumps(audit), encoding="utf-8")

            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")

        self.assertIn("自检不一致但本地校验后接纳", html)
        self.assertIn("经本地硬校验通过后接纳", html)
        self.assertIn(
            'model_self_check_mismatch_fields',
            html,
        )
        self.assertIn("allowed_directed_edge", html)
        self.assertIn("inside_boundary", html)

    def test_replay_includes_rejected_self_check_and_keeps_current_node_at_source(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3"
        parent_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v2"
        finalize(run_dir, parent_run, "gpt-5.6-luna")

        with tempfile.TemporaryDirectory() as temp_dir:
            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay, parent_run)
            html = replay.read_text(encoding="utf-8")

        self.assertIn('"request_number":23', html)
        self.assertIn('"from_node":"node_0_4"', html)
        self.assertIn('"to_node":"node_0_3"', html)
        self.assertIn('"accepted":false', html)
        self.assertIn("not_revisited", html)
        self.assertIn("候选边", html)

    def test_replay_marks_destination_reached_as_accepted_and_route_complete(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v4"
        parent_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3"
        finalize(run_dir, parent_run, "gpt-5.6-luna")

        with tempfile.TemporaryDirectory() as temp_dir:
            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay, parent_run)
            html = replay.read_text(encoding="utf-8")

        self.assertIn('"status":"route_completed"', html)
        self.assertIn(
            '"from_node":"node_5_2","to_node":"node_6_2","edge_id":"edge_node_5_2_node_6_2","direction":"E","allowed_directions":["E"],"status":"route_completed","accepted":true',
            html,
        )
        self.assertIn("已完成取送路线", html)

    def test_replay_renders_fresh_independent_task_without_bootstrap_action(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_independent_pilot_v9"
        finalize(run_dir, None, "gpt-5.6-luna")

        with tempfile.TemporaryDirectory() as temp_dir:
            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")

        self.assertIn('"task_id":"codex-short-task-0001"', html)
        self.assertIn('"from_node":"node_0_4"', html)
        self.assertIn('"to_node":"node_0_3"', html)
        self.assertNotIn('"is_bootstrap":true', html)
        self.assertIn('"is_start":true', html)
        self.assertIn("授权额度已用完", html)
        self.assertIn("拓扑下界", html)


if __name__ == "__main__":
    unittest.main()
