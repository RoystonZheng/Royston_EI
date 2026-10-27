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
    def test_replay_distinguishes_cli_sigterm_from_a_rejected_route_action(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v2"
        finalize(run_dir, None, "gpt-5.6-luna")

        with tempfile.TemporaryDirectory() as temp_dir:
            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")

        data = json.loads(html.split("const D=", 1)[1].split(";\nconst esc", 1)[0])
        failed_request = data["steps"][-1]
        self.assertFalse(failed_request["action_received"])
        self.assertIsNone(failed_request["local_valid"])
        self.assertFalse(failed_request["tool_audit_available"])
        self.assertEqual(failed_request["vehicle_energy_consumed_kwh"], 0.0)
        self.assertEqual(data["unresolved_cli_invocation"]["request_number"], 22)
        self.assertIn("SIGTERM", failed_request["failure_detail"])
        self.assertEqual(data["metrics"]["cli_invocations"], 22)
        self.assertEqual(data["metrics"]["cli_invocations_without_model_turn"], 1)
        self.assertEqual(data["metrics"]["input_tokens"], 343267)
        self.assertFalse(data["metrics"]["token_usage_complete"])
        self.assertIn("投递与用量未知", data["notice"])
        self.assertIn("已知小计，完整用量未知", html)

    def test_replay_distinguishes_started_model_turn_without_route_response(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v9"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["attempts"].append({
                "request_number": 30,
                "cumulative_request_number": 59,
                "step_number": 57,
                "status": "delivery_and_usage_unknown",
                "current_node_id": "node_35_63",
                "target_node_id": None,
                "selected_edge_id": None,
                "allowed_directions": ["N", "W"],
                "allowed_edge_ids": {"N": "edge_node_35_63_node_35_64", "W": "edge_node_35_63_node_34_63"},
                "response": None,
                "validation": {"deterministic_local_check": {"passed": None, "errors": []}},
                "usage": {"input_tokens": None, "output_tokens": None},
                "tool_item_types": None,
                "provider_record": {
                    "delivery_and_usage_unknown_evidence": True,
                    "event_audit": {
                        "event_types": ["thread.started", "item.completed", "turn.started"],
                        "item_types": ["error"],
                        "turn_started_count": 1,
                        "agent_message_count": 0,
                        "usage_event_count": 0,
                        "tool_usage_detected": False,
                    },
                },
            })
            state_path.write_text(json.dumps(state), encoding="utf-8")

            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")
            serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
            data = json.loads(serialized)

        unresolved = data["steps"][-1]
        self.assertEqual(unresolved["status"], "CLI turn 已启动；未收到路线响应")
        self.assertIn("已观测到 turn.started", unresolved["failure_detail"])
        self.assertIn("未观测到 agent_message", unresolved["failure_detail"])
        self.assertIn("投递与用量未知", unresolved["failure_detail"])

    def test_replay_reports_known_latency_subtotal_without_rendering_unknown_as_zero(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v9"

        with tempfile.TemporaryDirectory() as temp_dir:
            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")
            serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
            data = json.loads(serialized)

        self.assertGreater(data["metrics"]["latency_ms"], 0)
        self.assertFalse(data["metrics"]["latency_complete"])
        self.assertEqual(data["metrics"]["cli_invocations_without_model_turn"], 1)
        self.assertIn("已知小计，未决调用时延未知", html)
        self.assertNotIn("0.0 秒", html)

    def test_replay_only_links_to_raw_artifacts_that_exist(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_task9_fresh_r4_step_v3_pilot_20261027_v9"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            (run_dir / "offline_reconciliation.json").write_text("{}", encoding="utf-8")
            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")
            serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
            data = json.loads(serialized)

        self.assertIn("state.json", data["artifacts"])
        self.assertIn("manifest.json", data["artifacts"])
        self.assertIn("traces/index.html", data["artifacts"])
        self.assertIn("offline_reconciliation.json", data["artifacts"])
        self.assertNotIn("final_audit.json", data["artifacts"])
        self.assertNotIn('href="final_audit.json"', html)

    def test_replay_does_not_render_missing_usage_or_tool_audit_as_zero(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            attempt = state["attempts"][-1]
            attempt["provider_record"]["partial_final_response_evidence"] = True
            attempt["tool_item_types"] = None
            attempt["usage"]["input_tokens"] = None
            attempt["usage"]["output_tokens"] = None
            state_path.write_text(json.dumps(state), encoding="utf-8")
            audit_path = run_dir / "final_audit.json"
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["tool_events"] = None
            audit["tool_audit_complete"] = False
            audit["tool_audit_unknown_request_numbers"] = [attempt["request_number"]]
            audit_path.write_text(json.dumps(audit), encoding="utf-8")

            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")

        self.assertIn("CLI 原始事件日志缺失", html)
        self.assertIn("用量未知", html)

    def test_replay_labels_interrupted_invocation_as_delivery_unknown(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_task9_stateless_run_20261026_v6"

        with tempfile.TemporaryDirectory() as temp_dir:
            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")

        serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
        data = json.loads(serialized)
        unresolved = data.get("unresolved_cli_invocation")
        self.assertEqual(unresolved["status"], "delivery_and_usage_unknown")
        self.assertIn("投递与用量未知", data["notice"])
        self.assertIn("不记作模型失败或零用量", data["notice"])

    def test_completed_replay_keeps_delivery_unknown_notice(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v4"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["unresolved_cli_invocation"] = {
                "request_number": 9,
                "status": "delivery_and_usage_unknown",
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")

            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")

        self.assertIn("已完成取送路线", html)
        self.assertIn("第 9 次 CLI 调用", html)
        self.assertIn("投递与用量未知", html)

    def test_replay_surfaces_delivery_unknown_from_state_before_final_audit(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "ready"
            state["unresolved_cli_invocation"] = {
                "request_number": 9,
                "status": "delivery_and_usage_unknown",
            }
            state.setdefault("attempts", []).append({
                "request_number": 9,
                "current_node_id": state["accepted_nodes"][-1],
                "status": "delivery_and_usage_unknown",
                "response": None,
                "validation": {},
                "usage": {"input_tokens": None, "output_tokens": None},
                "tool_item_types": None,
                "provider_record": {"delivery_and_usage_unknown_evidence": True},
            })
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (run_dir / "final_audit.json").unlink()

            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")

        self.assertIn("第 9 次 CLI 调用", html)
        self.assertIn("投递与用量未知", html)
        self.assertIn("不记作模型失败或零用量", html)

    def test_replay_marks_read_only_authorization_stop_and_zero_active_limit(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_task9_progress_feedback_v3_pilot_20261026_v2"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "stopped_by_user_instruction"
            state["effective_authorized_new_request_limit"] = 0
            state["additional_model_requests_authorized"] = False
            state["current_authorization_status"] = "read_only_audit_no_new_model_requests"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            audit_path = run_dir / "final_audit.json"
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["authorized_new_request_limit"] = 0
            audit["current_authorization_status"] = state["current_authorization_status"]
            audit["additional_model_requests_authorized"] = False
            audit_path.write_text(json.dumps(audit), encoding="utf-8")

            replay_path = Path(temp_dir) / "replay.html"
            render(run_dir, replay_path)
            html = replay_path.read_text(encoding="utf-8")

        serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
        data = json.loads(serialized)
        self.assertEqual(data["metrics"]["authorized_limit"], 0)
        self.assertFalse(data["metrics"]["additional_model_requests_authorized"])
        self.assertIn("按当前调用授权边界停止", data["notice"])
        self.assertIn("无后续调用授权", html)

    def test_replay_summary_uses_usage_and_pricing_fields_from_final_audit(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1"
        audit = json.loads((source_run / "final_audit.json").read_text(encoding="utf-8"))
        attempt = json.loads((source_run / "state.json").read_text(encoding="utf-8"))["attempts"][0]
        audit_usage = audit.get("usage", {})
        expected_input_tokens = audit.get(
            "cumulative_input_tokens_including_bootstrap_and_parent",
            audit_usage.get("input_tokens"),
        )
        expected_output_tokens = audit.get(
            "cumulative_output_tokens_including_bootstrap_and_parent",
            audit_usage.get("output_tokens"),
        )
        expected_api_equivalent = attempt["provider_record"]["cost_estimate"]["api_equivalent_usd"]

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay)
            html = replay.read_text(encoding="utf-8")

        serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
        data = json.loads(serialized)
        self.assertEqual(data["metrics"]["input_tokens"], expected_input_tokens)
        self.assertEqual(data["metrics"]["output_tokens"], expected_output_tokens)
        self.assertAlmostEqual(data["metrics"]["api_equivalent_usd"], expected_api_equivalent)

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

    def test_replay_exposes_model_failure_reason_code_for_rejected_step(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3"
        parent_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v2"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["attempts"][-1]["response"]["failure_reason_code"] = "unable_to_construct_route"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            finalize(run_dir, parent_run, "gpt-5.6-luna")

            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay, parent_run)
            html = replay.read_text(encoding="utf-8")
            serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
            data = json.loads(serialized)

        rejected = data["steps"][-1]
        self.assertEqual(rejected["failure_reason_code"], "unable_to_construct_route")
        self.assertIn("模型返回失败代码：", html)

    def test_replay_explains_when_local_validator_overrides_model_failure_claim(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3"
        parent_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v2"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            attempt = next(item for item in state["attempts"] if item.get("selected_edge_id"))
            attempt["response"]["failure_reason_code"] = "unable_to_construct_route"
            attempt.setdefault("validation", {})[
                "model_failure_code_overridden_by_local_validation"
            ] = True
            state_path.write_text(json.dumps(state), encoding="utf-8")
            finalize(run_dir, parent_run, "gpt-5.6-luna")

            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay, parent_run)
            html = replay.read_text(encoding="utf-8")

        self.assertIn("本地校验覆盖模型失败声明", html)
        serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
        data = json.loads(serialized)
        self.assertTrue(any(
            step.get("model_failure_code_overridden_by_local_validation")
            for step in data["steps"]
        ))
        self.assertEqual(data["metrics"]["model_failure_code_override_count"], 1)

    def test_replay_marks_rejected_one_way_transition_for_map_display_without_edge_id(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3"
        parent_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v2"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            shutil.copytree(source_run, run_dir)
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            attempt = state["attempts"][-1]
            attempt["response"]["direction"] = "N"
            local_check = attempt["validation"]["deterministic_local_check"]
            local_check["passed"] = False
            local_check["errors"] = ["direction_not_in_dynamic_schema"]
            local_check["expected_target_node_id"] = "node_0_5"
            attempt["validation"]["direction_in_dynamic_schema"] = False
            state_path.write_text(json.dumps(state), encoding="utf-8")
            finalize(run_dir, parent_run, "gpt-5.6-luna")

            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay, parent_run)
            html = replay.read_text(encoding="utf-8")
            serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
            data = json.loads(serialized)

        rejected = data["steps"][-1]
        self.assertIsNone(rejected["edge_id"])
        self.assertEqual(rejected["from_node"], "node_0_4")
        self.assertEqual(rejected["to_node"], "node_0_5")
        self.assertTrue(rejected.get("candidate_visible_on_map"))
        self.assertEqual(rejected.get("errors"), ["direction_not_in_dynamic_schema"])
        self.assertGreater(rejected["hypothetical_step_energy_kwh"], 0.0)
        self.assertEqual(rejected["hypothetical_energy_basis"]["reverse_edge_inferred_moves"], 1)
        self.assertIn("本地拒绝原因", html)
        self.assertIn("反向道路属性", html)

    def test_replay_marks_destination_reached_as_accepted_and_route_complete(self):
        run_dir = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v4"
        parent_run = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v3"
        finalize(run_dir, parent_run, "gpt-5.6-luna")

        with tempfile.TemporaryDirectory() as temp_dir:
            replay = Path(temp_dir) / "replay.html"
            render(run_dir, replay, parent_run)
            html = replay.read_text(encoding="utf-8")
        serialized = html.split("const D=", 1)[1].split(";\nconst esc", 1)[0]
        data = json.loads(serialized)

        self.assertIn('"status":"route_completed"', html)
        self.assertEqual(data["steps"][-1]["from_node"], "node_5_2")
        self.assertEqual(data["steps"][-1]["to_node"], "node_6_2")
        self.assertEqual(data["steps"][-1]["edge_id"], "edge_node_5_2_node_6_2")
        self.assertEqual(data["steps"][-1]["direction"], "E")
        self.assertEqual(data["steps"][-1]["allowed_directions"], ["E"])
        self.assertTrue(data["steps"][-1]["accepted"])
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
