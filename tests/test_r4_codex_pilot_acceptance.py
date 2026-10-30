import json
import hashlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_r4_codex_pilot_continuation as pilot  # noqa: E402
import render_r4_codex_pilot_replay as replay  # noqa: E402
from route_experiment.models import Edge, Node, RoadMap, RouteInputPackage, VehicleOrderTask  # noqa: E402


def _grant_mocked_test_requests(state):
    """Make request authorization explicit for tests with mocked clients."""
    state["effective_authorized_new_request_limit"] = state["authorized_new_request_limit"]
    state["additional_model_requests_authorized"] = True
    state["current_authorization_status"] = "test_explicit_authorization"
    state["user_authorization_status"] = "granted_for_task9_continuation"
    return state


class FakeStepClient:
    def __init__(self, **kwargs):
        self.call_count = 0

    def chat(self, messages):
        self.call_count += 1
        return SimpleNamespace(
            content=json.dumps(
                {
                    "action": "move",
                    "direction": "S",
                    "failure_reason_code": "none",
                    "self_check": {
                        "allowed_directed_edge": False,
                        "starts_at_current": True,
                        "inside_boundary": False,
                        "pickup_order_ok": True,
                        "target_node_is_new": True,
                    },
                }
            ),
            provider_audit={"event_audit": {"tool_item_types": []}},
            input_tokens=100,
            cached_input_tokens=0,
            output_tokens=20,
            latency_ms=1.0,
            session_id=None,
        )


class R4CodexPilotAcceptanceTests(unittest.TestCase):
    def test_direct_online_one_step_is_blocked_by_stale_passed_offline_batch_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "formal_batch"
            run_dir = root / "parallel_workers" / "worker_02" / "tasks" / "seed_0" / "task" / "run_000"
            run_dir.mkdir(parents=True)
            (root / "manifest.json").write_text(json.dumps({
                "route_safety_gate": {
                    "status": "passed_offline_directed_graph_route_search",
                    "formal_continuation_permitted": True,
                },
            }), encoding="utf-8")
            with patch.object(pilot, "OUTPUT_DIR", run_dir):
                with self.assertRaisesRegex(RuntimeError, "r4_route_safety_gate_not_passed"):
                    pilot.one_step()

    def test_formal_online_step_path_rejects_oneway_reverse_backtracks_once_and_reroutes(self):
        nodes = [
            Node("node_0_0", 0, 0),
            Node("node_1_0", 1, 0),
            Node("node_0_1", 0, 1),
            Node("node_1_1", 1, 1),
            Node("node_0_2", 0, 2, legal_pickup=True),
            Node("node_1_2", 1, 2, legal_dropoff=True),
        ]
        edges = [
            Edge("edge_start_east_oneway", "node_0_0", "node_1_0", 100, 10, one_way=True),
            Edge("edge_start_north", "node_0_0", "node_0_1", 100, 10),
            Edge("edge_east_north", "node_1_0", "node_1_1", 100, 10),
            Edge("edge_center_west", "node_1_1", "node_0_1", 100, 10),
            Edge("edge_north_pickup", "node_0_1", "node_0_2", 100, 10),
            Edge("edge_pickup_dropoff", "node_0_2", "node_1_2", 100, 10),
        ]
        road_map = RoadMap(
            map_id="synthetic-oneway-map",
            version="v1",
            size=3,
            nodes=nodes,
            edges=edges,
            boundary={"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 2},
        )
        task = VehicleOrderTask(
            task_id="synthetic-oneway-task",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id="node_0_0",
            pickup_node_id="node_0_2",
            dropoff_node_id="node_1_2",
        )
        package = RouteInputPackage(
            package_id="synthetic-oneway-package",
            map_id=road_map.map_id,
            map_version=road_map.version,
            road_state_version=road_map.road_state_version,
            random_seed=0,
            start_node_id=task.start_node_id,
            pickup_node_id=task.pickup_node_id,
            dropoff_node_id=task.dropoff_node_id,
            task_type="exact_node",
            pickup_text=None,
            dropoff_text=None,
            nodes=nodes,
            edges=edges,
            expansion_count=len(nodes),
            serialized_token_estimate=0,
            boundary=road_map.boundary,
        )
        directions = ["E", "W", "N", "N", "E"]
        captured = []

        class OfflineResponseSequenceClient:
            def __init__(self, **kwargs):
                self.route_constraint = kwargs["route_constraint"]
                self.last_record = {}

            def chat(self, messages):
                direction = directions[len(captured)]
                allowed = sorted(self.route_constraint.edge_by_direction)
                captured.append({
                    "direction": direction,
                    "allowed": allowed,
                    "request": json.loads(messages[-1]["content"]),
                })
                return SimpleNamespace(
                    content=json.dumps({
                        "action": "move",
                        "direction": direction,
                        "failure_reason_code": "none",
                        "self_check": {
                            "allowed_directed_edge": True,
                            "starts_at_current": True,
                            "inside_boundary": True,
                            "pickup_order_ok": True,
                            "target_node_is_new": True,
                        },
                    }),
                    provider_audit={"event_audit": {"tool_item_types": []}},
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    latency_ms=0,
                    session_id=None,
                )

            @staticmethod
            def _redact_diagnostics(text):
                return text

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            preflight = Path(temp_dir) / "preflight.json"
            preflight.write_text(json.dumps({
                "map": {
                    "map_id": road_map.map_id,
                    "version": road_map.version,
                    "size": road_map.size,
                    "nodes": [vars(node) for node in nodes],
                    "edges": [vars(edge) for edge in edges],
                    "boundary": road_map.boundary,
                }
            }), encoding="utf-8")
            state = {
                "status": "ready",
                "source_preflight": str(preflight),
                "source_preflight_sha256": hashlib.sha256(preflight.read_bytes()).hexdigest(),
                "map_id": road_map.map_id,
                "task_id": task.task_id,
                "start_node_id": task.start_node_id,
                "pickup_node_id": task.pickup_node_id,
                "dropoff_node_id": task.dropoff_node_id,
                "accepted_nodes": [task.start_node_id],
                "accepted_edges": [],
                "accepted_directions": [],
                "pickup_seen": False,
                "route_action_budget": 10,
                "authorized_new_request_limit": 10,
                "effective_authorized_new_request_limit": 10,
                "additional_model_requests_authorized": True,
                "new_requests_used": 0,
                "attempts": [],
                "retry_count": 0,
                "codex_session_mode": "ephemeral_per_request",
                "codex_session_id": None,
                "route_feature_context": None,
                "compact_visited_state": False,
                "enable_progress_guidance": False,
                "suffix_witness_gate_version": "r4-suffix-witness-gate-v1",
                "suffix_witness_gate_enabled": True,
                "rejected_directions_by_node": {},
                "backtracked_directions_by_node": {},
                "suffix_witness_prefix_audit": {
                    "passed": True,
                    "accepted_action_count": 0,
                    "prefix_sha256": pilot.accepted_prefix_sha256([task.start_node_id], []),
                    "model_calls": 0,
                },
            }
            (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            with (
                patch.object(pilot, "OUTPUT_DIR", run_dir),
                patch.object(pilot, "_load_frozen_task", return_value=({}, road_map, task, package)),
                patch.object(pilot, "CodexCliRouteStepClient", OfflineResponseSequenceClient),
                patch.object(pilot, "_refresh_replay"),
            ):
                with redirect_stdout(StringIO()):
                    for _ in directions:
                        pilot.one_step()
            final = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            (run_dir / "traces").mkdir(exist_ok=True)
            replay_path = replay.render(run_dir, run_dir / "replay.html")
            replay_html = replay_path.read_text(encoding="utf-8")

        self.assertEqual(final["status"], "route_complete")
        self.assertEqual(final["accepted_nodes"], ["node_0_0", "node_0_1", "node_0_2", "node_1_2"])
        self.assertEqual(final["accepted_edges"], ["edge_start_north", "edge_north_pickup", "edge_pickup_dropoff"])
        self.assertEqual(final["pickup_seen"], True)
        self.assertEqual(len(final["route_recovery_events"]), 1)
        recovery = final["route_recovery_events"][0]
        self.assertEqual(recovery["rejected_edge"]["reason"], "one_way_direction")
        self.assertEqual(recovery["rollback_count_nodes"], 1)
        self.assertEqual(recovery["rollback_node_id"], "node_0_0")
        self.assertEqual(recovery["removed_accepted_edge"]["edge_id"], "edge_start_east_oneway")
        self.assertEqual(captured[2]["allowed"], ["N"])
        self.assertEqual(captured[2]["request"]["f"]["code"], "rejected_candidate_and_backtracked_one_node")
        self.assertEqual(final["attempts"][1]["status"], "rejected_and_reroute_from_last_legal_node")
        self.assertEqual(final["route_recovery_events"][0]["rejected_edge_added_to_route"], False)
        self.assertIn("安全回退", replay_html)
        self.assertIn("one_way_direction", replay_html)
        self.assertIn("撤销上一段已接纳边", replay_html)

    def test_rejected_reverse_one_way_action_rolls_back_one_node_and_preserves_reroute_audit(self):
        state = {
            "task_id": "synthetic-one-way-task",
            "start_node_id": "node_0_0",
            "pickup_node_id": "node_1_0",
            "dropoff_node_id": "node_2_0",
            "accepted_nodes": ["node_0_0", "node_1_0"],
            "accepted_edges": ["edge_0_1_one_way_east"],
            "accepted_directions": ["E"],
            "pickup_seen": True,
        }

        recovery = pilot._rollback_rejected_action_one_node(
            state,
            current_node_id="node_1_0",
            rejected_direction="W",
            rejected_target_node_id="node_0_0",
            rejection_code="directed_edge_missing",
        )

        self.assertEqual(state["accepted_nodes"], ["node_0_0"])
        self.assertEqual(state["accepted_edges"], [])
        self.assertEqual(state["accepted_directions"], [])
        self.assertFalse(state["pickup_seen"])
        self.assertEqual(recovery["rollback_node_id"], "node_0_0")
        self.assertEqual(recovery["rollback_count_nodes"], 1)
        self.assertEqual(recovery["rejected_edge"], {
            "from_node_id": "node_1_0",
            "to_node_id": "node_0_0",
            "direction": "W",
            "reason": "directed_edge_missing",
        })
        self.assertEqual(recovery["removed_accepted_edge"], {
            "from_node_id": "node_0_0",
            "to_node_id": "node_1_0",
            "edge_id": "edge_0_1_one_way_east",
            "direction": "E",
        })
        self.assertEqual(state["backtracked_directions_by_node"], {"node_0_0": ["E"]})
        self.assertEqual(state["route_recovery_events"], [recovery])
        self.assertEqual(
            state["pending_route_research_feedback"]["retry_from_node_id"],
            "node_0_0",
        )

    def test_explicit_unbounded_authorization_replaces_512_with_simple_path_bound(self):
        state = {
            "additional_model_requests_authorized": True,
            "authorization_quote": "不设置上限，尽快完成实验。",
            "authorized_new_request_limit": 512,
            "effective_authorized_new_request_limit": 512,
            "route_action_budget": 512,
            "new_requests_used": 105,
        }

        changed = pilot._apply_unbounded_route_budget(
            state, SimpleNamespace(nodes=[SimpleNamespace(node_id=str(i)) for i in range(10000)])
        )

        self.assertTrue(changed)
        self.assertEqual(state["authorized_new_request_limit"], 9999)
        self.assertEqual(state["effective_authorized_new_request_limit"], 9999)
        self.assertEqual(state["route_action_budget"], 9999)
        self.assertEqual(state["new_requests_used"], 105)
        self.assertEqual(state["unbounded_authorization_applied"]["natural_simple_path_limit"], 9999)

    def test_unbounded_route_budget_requires_explicit_no_limit_authorization(self):
        state = {
            "additional_model_requests_authorized": True,
            "authorization_quote": "最多授权 128 次请求。",
            "authorized_new_request_limit": 128,
            "effective_authorized_new_request_limit": 128,
            "route_action_budget": 512,
        }

        changed = pilot._apply_unbounded_route_budget(
            state, SimpleNamespace(nodes=[SimpleNamespace(node_id=str(i)) for i in range(10000)])
        )

        self.assertFalse(changed)
        self.assertEqual(state["authorized_new_request_limit"], 128)
        self.assertEqual(state["effective_authorized_new_request_limit"], 128)
        self.assertEqual(state["route_action_budget"], 512)

    def test_persistent_usage_ledger_stores_per_call_delta_and_raw_counter(self):
        state = {
            "codex_session_mode": "persistent_per_task",
            "attempts": [{
                "codex_session_id": "same-session",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 10,
                    "output_tokens": 8,
                    "session_cumulative": {
                        "input_tokens": 100,
                        "cached_input_tokens": 10,
                        "output_tokens": 8,
                    },
                },
            }],
        }

        usage = pilot._attempt_usage_record(
            {"input_tokens": 160, "cached_input_tokens": 20, "output_tokens": 13, "latency_ms": 9},
            state,
            "same-session",
        )

        self.assertEqual(usage["input_tokens"], 60)
        self.assertEqual(usage["cached_input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["session_cumulative"]["input_tokens"], 160)
        self.assertEqual(usage["accounting_basis"], "persistent_session_cumulative_delta")

    def test_fresh_state_accepts_frozen_100x100_stepwise_preflight_and_keeps_road_features(self):
        preflight = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"

        state = pilot._initial_state(
            "r4_codex_stepwise_100x100_task9_test",
            authorized_request_limit=128,
            preflight_path=preflight,
        )

        self.assertEqual(state["map_id"], "grid-100x100-seed-0")
        self.assertEqual(state["task_id"], "task-0000-0009")
        self.assertEqual(state["accepted_nodes"], ["node_69_41"])
        self.assertEqual(state["route_action_budget"], 512)
        self.assertEqual(state["initial_remaining_minimum_moves"], 109)
        self.assertTrue(state["route_feature_context"])
        self.assertEqual(state["authorized_new_request_limit"], 128)
        self.assertEqual(state["status"], "ready")

    def test_task9_authorization_record_is_scoped_and_does_not_change_default(self):
        preflight = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"

        default_state = pilot._initial_state(
            "task9-default-authorization-audit",
            authorized_request_limit=138,
            preflight_path=preflight,
        )
        authorized_state = pilot._initial_state(
            "task9-explicit-authorization-audit",
            authorized_request_limit=138,
            preflight_path=preflight,
            user_authorized_task9_experiment=True,
        )

        self.assertNotIn("user_authorization_status", default_state)
        self.assertEqual(default_state["effective_authorized_new_request_limit"], 0)
        self.assertFalse(default_state["additional_model_requests_authorized"])
        self.assertEqual(authorized_state["user_authorization_status"], "granted_for_task9_continuation")
        _, _, _, package = pilot._load_frozen_task(preflight)
        natural_limit = pilot._natural_simple_path_limit(package)
        self.assertEqual(authorized_state["authorized_new_request_limit"], natural_limit)
        self.assertEqual(authorized_state["effective_authorized_new_request_limit"], natural_limit)
        self.assertEqual(authorized_state["route_action_budget"], natural_limit)
        self.assertEqual(authorized_state["unbounded_authorization_applied"]["natural_simple_path_limit"], natural_limit)
        self.assertTrue(authorized_state["additional_model_requests_authorized"])
        self.assertEqual(authorized_state["authorization_scope"], "complete the task9 route experiment only; no ten-seed batch")
        self.assertEqual(authorized_state["authorization_quote"], "完成全部这部分实验无限制！")

    def test_100x100_one_step_resends_frozen_road_feature_context_and_accepts_only_a_legal_edge(self):
        preflight = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"
        captured = {}

        class LegalWestStepClient:
            def __init__(self, **kwargs):
                self.last_record = {}

            def chat(self, messages):
                captured["payload"] = json.loads(messages[-1]["content"])
                return SimpleNamespace(
                    content=json.dumps({
                        "action": "move",
                        "direction": "W",
                        "failure_reason_code": "none",
                        "self_check": {
                            "allowed_directed_edge": True,
                            "starts_at_current": True,
                            "inside_boundary": True,
                            "pickup_order_ok": True,
                            "target_node_is_new": True,
                        },
                    }),
                    provider_audit={"event_audit": {"tool_item_types": []}},
                    input_tokens=20,
                    cached_input_tokens=0,
                    output_tokens=8,
                    latency_ms=1.0,
                    session_id=None,
                )

            @staticmethod
            def _redact_diagnostics(text):
                return text

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            state = pilot._initial_state(
                "stepwise-100x100-legal-edge-test",
                authorized_request_limit=128,
                preflight_path=preflight,
            )
            _grant_mocked_test_requests(state)
            output_dir.mkdir()
            (output_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", LegalWestStepClient),
            ):
                pilot.one_step()
            updated = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            replay_html = output_dir / "replay.html"
            replay_exists = replay_html.is_file()
            replay_content = replay_html.read_text(encoding="utf-8") if replay_exists else ""

        self.assertEqual(captured["payload"]["i"], state["route_feature_context"])
        self.assertEqual(updated["new_requests_used"], 1)
        self.assertTrue(replay_exists)
        self.assertIn("node_68_41", replay_content)

    def test_v3_persistent_one_step_sends_compact_state_and_progress_without_changing_validator(self):
        preflight = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"
        captured = {}
        client_args = []

        class LegalProgressStepClient:
            def __init__(self, **kwargs):
                client_args.append(kwargs)
                self.last_record = {}
                self.constraint = kwargs["route_constraint"]

            def chat(self, messages):
                payload = json.loads(messages[-1]["content"])
                captured["payload"] = payload
                direction = self.constraint.schema["properties"]["direction"]["enum"][0]
                captured["direction"] = direction
                return SimpleNamespace(
                    content=json.dumps({
                        "action": "move",
                        "direction": direction,
                        "failure_reason_code": "none",
                        "self_check": {
                            "allowed_directed_edge": True,
                            "starts_at_current": True,
                            "inside_boundary": True,
                            "pickup_order_ok": True,
                            "target_node_is_new": True,
                        },
                    }),
                    provider_audit={"event_audit": {"tool_item_types": []}},
                    input_tokens=20,
                    cached_input_tokens=0,
                    output_tokens=8,
                    latency_ms=1.0,
                    session_id="persistent-v3-session",
                )

            @staticmethod
            def _redact_diagnostics(text):
                return text

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            state = pilot._initial_state(
                "stepwise-100x100-v3-test",
                authorized_request_limit=138,
                preflight_path=preflight,
                codex_session_mode="persistent_per_task",
                input_protocol="r4-step-v3",
            )
            _grant_mocked_test_requests(state)
            output_dir.mkdir()
            (output_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", LegalProgressStepClient),
            ):
                pilot.one_step()
            updated = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            replay_html = output_dir / "replay.html"
            replay_exists = replay_html.is_file()
            replay_content = replay_html.read_text(encoding="utf-8") if replay_exists else ""

        payload = captured["payload"]
        self.assertEqual(state["route_input_protocol"], "r4-step-v3")
        self.assertEqual(state["initial_remaining_minimum_moves"], 109)
        self.assertEqual(payload["v"], "r4-step-v3")
        self.assertEqual(payload["s"]["vf"], "grid_slot_base36")
        self.assertIsInstance(payload["s"]["v"], str)
        self.assertIn("by_direction", payload["r"])
        self.assertTrue(payload["i"])
        self.assertTrue(client_args[0]["persist_session"])
        self.assertEqual(updated["new_requests_used"], 1)
        self.assertEqual(updated["accepted_edges"].__len__(), 1)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["accepted_directions"], [captured["direction"]])
        self.assertEqual(updated["status"], "ready")
        self.assertTrue(updated["attempts"][0]["validation"]["deterministic_local_check"]["passed"])
        self.assertTrue(replay_exists)
        self.assertIn(updated["accepted_nodes"][-1], replay_content)

    def test_100x100_zero_request_preflight_renders_a_replay_without_route_metrics(self):
        preflight = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            (run_dir / "traces").mkdir()
            state = pilot._initial_state(
                "r4_codex_stepwise_100x100_render_test",
                authorized_request_limit=128,
                preflight_path=preflight,
            )
            pilot._write_json(run_dir / "state.json", state)
            target = run_dir / "replay.html"

            replay.render(run_dir, target)

            html = target.read_text(encoding="utf-8")

        self.assertIn("node_69_41", html)
        self.assertIn("grid-100x100-seed-0", html)
        self.assertTrue("Codex 100×100 路线试跑" in html, "回放标题应反映实际 100×100 地图尺寸")
        self.assertTrue('aria-label="100乘100有向道路地图与路线回放"' in html, "地图辅助标签应反映实际尺寸")
        self.assertIn("0", html)

    def test_quota_cap_stop_reason_is_explained_in_replay_notice(self):
        source_run = ROOT / "outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            (run_dir / "traces").mkdir()
            for name in ("state.json", "final_audit.json"):
                (run_dir / name).write_bytes((source_run / name).read_bytes())
            target = run_dir / "replay.html"

            replay.render(run_dir, target)

            html = target.read_text(encoding="utf-8")

        self.assertTrue("周用量" in html, "回放 HTML 应包含账户周用量停止说明")
        self.assertTrue("不能单独归因于本次路线请求" in html, "回放应说明账户额度快照的归因限制")
        self.assertTrue("只展示已接纳合法前缀" in html, "回放应区分前缀能耗和完整路线结果")

    def test_fresh_pilot_starts_at_frozen_task_origin_without_inherited_smoke_move(self):
        state = pilot._initial_state("fresh-task-test")

        self.assertEqual(state["accepted_nodes"], [state["start_node_id"]])
        self.assertEqual(state["accepted_edges"], [])
        self.assertEqual(state["accepted_directions"], [])
        self.assertIsNone(state["codex_session_id"])
        self.assertEqual(state["codex_session_mode"], "ephemeral_per_request")

    def test_preflight_manifest_separates_candidate_limit_from_user_authorization(self):
        preflight = ROOT / "outputs/route_layer/r4_preflight/r4_codex_ephemeral_8x8_seed0_20261022.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "PREFLIGHT_PATH", preflight),
            ):
                pilot.preflight_only(
                    experiment_id="request-limit-manifest-test",
                    authorized_request_limit=4,
                )

            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(state["authorized_new_request_limit"], 4)
        self.assertEqual(manifest["schema_version"], "r4_codex_pilot_continuation_manifest_v3")
        self.assertEqual(manifest["candidate_request_limit"], 4)
        self.assertEqual(manifest["user_authorization_status"], "not_granted")
        self.assertEqual(manifest["effective_authorized_new_request_limit"], 0)
        self.assertFalse(manifest["additional_model_requests_authorized"])
        self.assertEqual(manifest["request_limit_semantics"], "candidate_safety_cap_not_user_authorization")
        self.assertNotIn("authorization", manifest)
        self.assertEqual(manifest["initial_state"]["candidate_requests_in_this_continuation"], 4)
        self.assertEqual(
            manifest["initial_state"]["can_complete_with_candidate_request_limit"],
            manifest["initial_state"]["minimum_remaining_moves"] <= 4,
        )

    def test_one_step_filters_directions_to_the_remaining_authorized_calls(self):
        preflight = ROOT / "outputs/route_layer/r4_preflight/r4_codex_ephemeral_fixed_smoke_seed0_20261022.json"
        client_args = []
        observed_budgets = []

        class BudgetAwareClient:
            def __init__(self, **kwargs):
                client_args.append(kwargs)
                self.last_record = {}

            def chat(self, messages):
                request = json.loads(messages[-1]["content"])
                observed_budgets.append(request["s"]["b"])
                return SimpleNamespace(
                    content=json.dumps({
                        "action": "move",
                        "direction": "N",
                        "failure_reason_code": "none",
                        "self_check": {
                            "allowed_directed_edge": True,
                            "starts_at_current": True,
                            "inside_boundary": True,
                            "pickup_order_ok": True,
                            "target_node_is_new": True,
                        },
                    }),
                    provider_audit={"event_audit": {"tool_item_types": []}},
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=5,
                    latency_ms=1,
                    session_id=None,
                )

            @staticmethod
            def _redact_diagnostics(text):
                return text

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            with (
                patch.object(pilot, "PREFLIGHT_PATH", preflight),
                patch.object(pilot, "OUTPUT_DIR", output_dir),
            ):
                state = _grant_mocked_test_requests(
                    pilot._initial_state("budget-aware-step", authorized_request_limit=2)
                )
                (output_dir).mkdir(parents=True)
                (output_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
                with patch.object(pilot, "CodexCliRouteStepClient", BudgetAwareClient):
                    pilot.one_step()
                updated = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(client_args[0]["route_constraint"].edge_by_direction.keys(), {"N"})
        self.assertEqual(observed_budgets, [2])
        self.assertEqual(updated["accepted_directions"], ["N"])

    def test_skips_model_request_when_remaining_authorization_cannot_finish_route(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 7,
            "attempts": [],
        })
        _grant_mocked_test_requests(source_state)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient") as client_class,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "insufficient_authorized_budget_for_remaining_route"
                ):
                    pilot.one_step()
                client_class.assert_not_called()

            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(state["status"], "insufficient_authorized_budget_for_remaining_route")
        self.assertEqual(state["new_requests_used"], 0)
        self.assertEqual(state["stop_diagnostics"]["unused_authorized_calls"], 7)

    def test_no_direction_due_to_budget_is_recorded_as_pre_request_budget_stop(self):
        preflight = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"
        filtered = {
            "W": "remaining_action_budget_insufficient",
            "N": "remaining_action_budget_insufficient",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            output_dir.mkdir()
            state = pilot._initial_state(
                "pre-request-budget-stop-test",
                authorized_request_limit=128,
                preflight_path=preflight,
            )
            _grant_mocked_test_requests(state)
            state["new_requests_used"] = 86
            (output_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            step_constraint = SimpleNamespace(
                remaining_minimum_moves=41,
                edge_by_direction={},
                filtered_directions=filtered,
                can_give_up=True,
            )
            with (
                patch.object(pilot, "PREFLIGHT_PATH", preflight),
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "build_r4_step_output_constraint", return_value=step_constraint),
                patch.object(pilot, "_refresh_replay"),
                patch.object(pilot, "CodexCliRouteStepClient") as client_class,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "insufficient_authorized_budget_for_remaining_route"
                ):
                    pilot.one_step()
                client_class.assert_not_called()

            updated = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            failure_path = output_dir / "pre_request_failures" / "pre_request_01.json"
            failure = json.loads(failure_path.read_text(encoding="utf-8"))

        self.assertEqual(updated["status"], "insufficient_authorized_budget_for_remaining_route")
        self.assertEqual(updated["new_requests_used"], 86)
        self.assertEqual(failure["request_number"], 87)
        self.assertFalse(failure["model_request_started"])
        self.assertEqual(failure["remaining_authorized_calls"], 42)
        self.assertEqual(failure["filtered_directions"], filtered)

    def test_budget_stop_replay_explains_that_no_next_model_request_was_sent(self):
        preflight = ROOT / "outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            (run_dir / "traces").mkdir()
            state = pilot._initial_state(
                "pre-request-budget-stop-replay-test",
                authorized_request_limit=128,
                preflight_path=preflight,
            )
            state["status"] = "insufficient_authorized_budget_for_remaining_route"
            state["new_requests_used"] = 86
            state["accepted_nodes"] = ["node_26_66"]
            state["stop_diagnostics"] = {
                "reason": "every_available_next_direction_was_filtered_by_remaining_authorized_budget",
                "request_number": 87,
                "current_node": "node_26_66",
                "current_node_id": "node_26_66",
                "remaining_minimum_moves": 41,
                "unused_authorized_calls": 42,
                "remaining_authorized_calls": 42,
                "filtered_directions": {
                    "W": "remaining_action_budget_insufficient",
                    "N": "remaining_action_budget_insufficient",
                },
                "candidate_evaluations": [
                    {
                        "direction": "E",
                        "reason": "node_revisited",
                        "minimum_remaining_moves_after_move": None,
                    },
                    {
                        "direction": "N",
                        "reason": "remaining_action_budget_insufficient",
                        "minimum_remaining_moves_after_move": 46,
                    },
                    {
                        "direction": "S",
                        "reason": "remaining_action_budget_insufficient",
                        "minimum_remaining_moves_after_move": 44,
                    },
                    {
                        "direction": "W",
                        "reason": "remaining_action_budget_insufficient",
                        "minimum_remaining_moves_after_move": 42,
                    },
                ],
            }
            state["route_metrics"] = {
                "accepted_prefix_distance_m": 0.0,
                "accepted_prefix_energy_kwh": 0.0,
            }
            pilot._write_json(run_dir / "state.json", state)
            pilot._write_json(run_dir / "final_audit.json", {
                "status": "partial_legal_prefix_not_complete",
                "minimum_remaining_moves_at_stop": 41,
                "actual_model_requests_this_continuation": 86,
            })
            target = run_dir / "replay.html"

            replay.render(run_dir, target)

            rendered = target.read_text(encoding="utf-8")

        self.assertTrue("第 87 次模型请求未发送" in rendered, "回放应明确记录请求前停止")
        self.assertTrue("剩余请求预算不足" in rendered, "回放应解释当前失败原因")
        self.assertTrue("当前拓扑下界 41 是上车段与下车段分别估算的乐观值" in rendered, "回放应说明下界与候选方向预算的区别")
        self.assertTrue("西向至少需43次" in rendered, "回放应显示最省预算的方向也超出剩余调用数")
        self.assertTrue("东向会重访" in rendered, "回放应显示因不重访约束而排除的方向")

    def test_locally_valid_move_is_accepted_and_false_model_checks_are_recorded(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 32,
            "attempts": [],
        })
        _grant_mocked_test_requests(source_state)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", FakeStepClient),
            ):
                pilot.one_step()

            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        attempt = state["attempts"][-1]
        self.assertEqual(attempt["status"], "accepted_legal_step")
        self.assertEqual(attempt["selected_edge_id"], "edge_node_4_7_node_4_6")
        self.assertEqual(state["accepted_nodes"][-1], "node_4_6")
        self.assertEqual(state["status"], "ready")
        self.assertTrue(attempt["validation"]["deterministic_local_check"]["passed"])
        self.assertFalse(attempt["validation"]["all_model_self_checks_true"])
        self.assertEqual(
            attempt["validation"]["model_self_check_mismatch_fields"],
            ["allowed_directed_edge", "inside_boundary"],
        )

    def test_locally_valid_move_overrides_model_failure_claim_and_records_the_mismatch(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 32,
            "attempts": [],
        })
        _grant_mocked_test_requests(source_state)

        class FailureClaimStepClient(FakeStepClient):
            def chat(self, messages):
                response = super().chat(messages)
                payload = json.loads(response.content)
                payload["failure_reason_code"] = "unable_to_construct_route"
                response.content = json.dumps(payload)
                return response

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", FailureClaimStepClient),
            ):
                pilot.one_step()

            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        attempt = state["attempts"][-1]
        self.assertEqual(attempt["status"], "accepted_legal_step")
        self.assertEqual(attempt["response"]["failure_reason_code"], "unable_to_construct_route")
        self.assertTrue(attempt["validation"]["deterministic_local_check"]["passed"])
        self.assertTrue(attempt["validation"]["model_failure_code_overridden_by_local_validation"])
        self.assertEqual(state["accepted_nodes"][-1], "node_4_6")

    def test_one_step_reuses_and_persists_codex_session_id(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 32,
            "attempts": [],
            "codex_session_id": "existing-codex-session",
            "codex_session_mode": "persistent_per_task",
        })
        _grant_mocked_test_requests(source_state)
        client_args = []

        class SessionStepClient(FakeStepClient):
            def __init__(self, **kwargs):
                client_args.append(kwargs)
                super().__init__(**kwargs)

            def chat(self, messages):
                response = super().chat(messages)
                response.session_id = "existing-codex-session"
                response.provider_audit["session_id"] = response.session_id
                return response

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", SessionStepClient),
            ):
                pilot.one_step()
            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(len(client_args), 1)
        self.assertTrue(client_args[0]["persist_session"])
        self.assertEqual(client_args[0]["session_id"], "existing-codex-session")
        self.assertEqual(state["codex_session_id"], "existing-codex-session")

    def test_ephemeral_one_step_does_not_persist_cli_session_id(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 32,
            "attempts": [],
            "codex_session_id": None,
            "codex_session_mode": "ephemeral_per_request",
        })
        _grant_mocked_test_requests(source_state)
        client_args = []

        class EphemeralSessionStepClient(FakeStepClient):
            def __init__(self, **kwargs):
                client_args.append(kwargs)
                super().__init__(**kwargs)

            def chat(self, messages):
                response = super().chat(messages)
                response.session_id = "cli-thread-id-for-audit-only"
                response.provider_audit["session_id"] = response.session_id
                return response

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", EphemeralSessionStepClient),
            ):
                pilot.one_step()
            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(len(client_args), 1)
        self.assertFalse(client_args[0]["persist_session"])
        self.assertIsNone(client_args[0]["session_id"])
        self.assertIsNone(state["codex_session_id"])

    def test_preflight_rejection_is_recorded_without_counting_or_appending_a_request(self):
        source_state = json.loads(
            (ROOT / "outputs/route_layer/r4_codex_independent_pilot_v5/state.json").read_text(
                encoding="utf-8"
            )
        )
        source_state.update({
            "status": "ready",
            "new_requests_used": 0,
            "authorized_new_request_limit": 32,
            "attempts": [],
        })
        _grant_mocked_test_requests(source_state)

        class PreflightRejectedClient:
            last_record = {
                "backend": "codex_cli",
                "status": "not_called",
                "stop_reason": "persistent_working_directory_not_empty",
            }

            def __init__(self, **kwargs):
                pass

            @staticmethod
            def _redact_diagnostics(text):
                return text

            def chat(self, messages):
                raise RuntimeError("codex_cli_persistent_working_directory_not_empty")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            with (
                patch.object(pilot, "OUTPUT_DIR", output_dir),
                patch.object(pilot, "CodexCliRouteStepClient", PreflightRejectedClient),
            ):
                pilot.one_step()
            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(state["new_requests_used"], 0)
        self.assertEqual(state["attempts"], [])
        self.assertEqual(state["status"], "stopped_pre_request_error")
        self.assertEqual(
            state["pre_request_failures"][0]["provider_record"]["stop_reason"],
            "persistent_working_directory_not_empty",
        )


if __name__ == "__main__":
    unittest.main()
