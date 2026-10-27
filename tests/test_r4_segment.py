import json
import unittest
from types import SimpleNamespace

from route_experiment.models import Edge, Node, RoadMap, RouteInputPackage, VehicleOrderTask
from route_experiment.r4_segment import (
    _allowed_directions_at_node,
    _progress_safe_directions_at_node,
    build_r4_segment_messages,
    build_r4_segment_schema,
    run_r4_segment_route_search,
    validate_r4_segment_proposal,
)


class SegmentClient:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.calls = []
        self.route_constraint = None
        self.retries = 0

    def chat(self, messages):
        self.calls.append(messages)
        payload = self.proposals.pop(0)
        return SimpleNamespace(
            content=json.dumps(payload),
            model="offline-fake",
            request_id="fake-{0}".format(len(self.calls)),
            input_tokens=10,
            output_tokens=5,
            cached_input_tokens=0,
            latency_ms=1.0,
            retries=0,
        )


class FailingSegmentClient:
    retries = 0
    route_constraint = None

    def chat(self, messages):
        raise RuntimeError("delivery_unknown_secret_sk-1234567890abcdefgh")


def check_flags(**overrides):
    flags = {
        "allowed_directed_edge": True,
        "starts_at_current": True,
        "inside_boundary": True,
        "pickup_order_ok": True,
        "target_node_is_new": True,
    }
    flags.update(overrides)
    return flags


class R4SegmentTests(unittest.TestCase):
    def setUp(self):
        self.road_map = RoadMap(
            map_id="segment-test-map",
            version="v1",
            size=2,
            nodes=[
                Node("node_0_0", 0, 0),
                Node("node_1_0", 1, 0),
                Node("node_0_1", 0, 1, legal_pickup=True),
                Node("node_1_1", 1, 1, legal_dropoff=True),
            ],
            edges=[
                Edge("north", "node_0_0", "node_0_1", 100, 10),
                Edge("east", "node_0_1", "node_1_1", 100, 10),
                Edge("back", "node_0_1", "node_0_0", 100, 10),
                Edge("oneway_east", "node_0_0", "node_1_0", 100, 10, one_way=True),
                Edge("east_north", "node_1_0", "node_1_1", 100, 10),
            ],
            boundary={"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1},
        )
        self.task = VehicleOrderTask(
            task_id="segment-task",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id="node_0_0",
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
        )
        self.package = RouteInputPackage(
            package_id="segment-package",
            map_id=self.road_map.map_id,
            map_version=self.road_map.version,
            road_state_version=self.road_map.road_state_version,
            random_seed=0,
            start_node_id=self.task.start_node_id,
            pickup_node_id=self.task.pickup_node_id,
            dropoff_node_id=self.task.dropoff_node_id,
            task_type="exact_node",
            pickup_text=None,
            dropoff_text=None,
            nodes=self.road_map.nodes,
            edges=self.road_map.edges,
            expansion_count=len(self.road_map.nodes),
            serialized_token_estimate=0,
            boundary=self.road_map.boundary,
        )

    def test_progress_safe_feedback_excludes_local_edge_that_cannot_finish_task(self):
        local = _allowed_directions_at_node(
            self.road_map,
            current_node_id="node_0_0",
            visited_node_ids=["node_0_0"],
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
            pickup_seen=False,
            blocked_edge_ids=(),
        )
        safe = _progress_safe_directions_at_node(
            self.road_map,
            current_node_id="node_0_0",
            visited_node_ids=["node_0_0"],
            pickup_node_id="node_0_1",
            dropoff_node_id="node_1_1",
            pickup_seen=False,
            blocked_edge_ids=(),
            accepted_action_count=0,
            progress_limit=2,
        )

        self.assertEqual(local, ["E", "N"])
        self.assertEqual(safe, ["N"])

    def test_schema_caps_segment_length_and_requires_per_step_checks(self):
        schema = build_r4_segment_schema(max_segment_actions=10)

        actions = schema["properties"]["actions"]
        self.assertEqual(actions["maxItems"], 10)
        self.assertEqual(actions["items"]["required"], ["direction", "self_check"])
        self.assertEqual(
            actions["items"]["properties"]["self_check"]["required"],
            [
                "allowed_directed_edge",
                "starts_at_current",
                "inside_boundary",
                "pickup_order_ok",
                "target_node_is_new",
            ],
        )

    def test_schema_can_require_a_full_segment(self):
        schema = build_r4_segment_schema(
            max_segment_actions=10,
            min_segment_actions=10,
        )

        actions = schema["properties"]["actions"]
        self.assertEqual(actions["minItems"], 10)
        self.assertEqual(actions["maxItems"], 10)

    def test_messages_include_compact_state_features_and_no_freeform_cot_request(self):
        context = {"protocol": "RoadFeatureInputProtocol-v1", "pickup": {"landmark": "北门"}}
        messages = build_r4_segment_messages(
            self.package,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            visited_node_ids=[self.task.start_node_id],
            accepted_directions=[],
            pickup_seen=False,
            max_segment_actions=10,
            remaining_action_budget=4,
            accepted_action_count=0,
            progress_limit=4,
            route_feature_context=context,
        )
        payload = json.loads(messages[1]["content"])

        self.assertEqual(payload["i"], context)
        self.assertEqual(payload["seg"]["max_actions"], 10)
        self.assertEqual(payload["s"]["vf"], "grid_slot_base36")
        self.assertIn("每节点严格占连续4个字符", messages[0]["content"])
        self.assertIn("按 s.vl 个字符切块", messages[0]["content"])
        self.assertIn("floor(k/m.w)", messages[0]["content"])
        self.assertIn("新坐标重新定位 m.g 行和单元格", messages[0]["content"])
        self.assertIn("self_check", messages[0]["content"])
        self.assertIn("外部确定性程序", messages[0]["content"])
        self.assertIn("progress_safe_directions_at_failure_state", messages[0]["content"])
        self.assertIn("满足路线进度预算", messages[0]["content"])
        self.assertNotIn("ground_truth", messages[1]["content"])

    def test_validator_accepts_complete_segment_and_reports_endpoint(self):
        result = validate_r4_segment_proposal(
            self.road_map,
            start_node_id=self.task.start_node_id,
            pickup_node_id=self.task.pickup_node_id,
            dropoff_node_id=self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            pickup_seen=False,
            visited_node_ids=[self.task.start_node_id],
            payload={
                "actions": [
                    {"direction": "N", "self_check": check_flags()},
                    {"direction": "E", "self_check": check_flags()},
                ]
            },
            accepted_action_count=0,
            progress_limit=4,
        )

        self.assertTrue(result["accepted"])
        self.assertTrue(result["route_complete"])
        self.assertEqual(result["committed_node_sequence"], ["node_0_1", "node_1_1"])
        self.assertEqual(result["committed_edge_sequence"], ["north", "east"])

    def test_validator_rejects_a_response_shorter_than_the_required_segment(self):
        result = validate_r4_segment_proposal(
            self.road_map,
            start_node_id=self.task.start_node_id,
            pickup_node_id=self.task.pickup_node_id,
            dropoff_node_id=self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            pickup_seen=False,
            visited_node_ids=[self.task.start_node_id],
            payload={"actions": [{"direction": "N", "self_check": check_flags()}]},
            accepted_action_count=0,
            progress_limit=4,
            max_segment_actions=2,
            min_segment_actions=2,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["violations"][0]["kind"], "segment_too_short")

    def test_model_self_check_mismatch_is_logged_but_local_validator_decides(self):
        result = validate_r4_segment_proposal(
            self.road_map,
            start_node_id=self.task.start_node_id,
            pickup_node_id=self.task.pickup_node_id,
            dropoff_node_id=self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            pickup_seen=False,
            visited_node_ids=[self.task.start_node_id],
            payload={
                "actions": [
                    {"direction": "N", "self_check": check_flags(target_node_is_new=False)}
                ]
            },
            accepted_action_count=0,
            progress_limit=4,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["committed_edge_sequence"], ["north"])
        self.assertEqual(result["steps"][0]["self_check_mismatch_fields"], ["target_node_is_new"])
        self.assertEqual(result["model_self_check_discrepancy_count"], 1)
        self.assertEqual(result["violations"], [])

    def test_rejection_feedback_lists_legal_directions_and_reproposal_completes(self):
        client = SegmentClient([
            {"actions": [
                {"direction": "S", "self_check": check_flags()},
                {"direction": "N", "self_check": check_flags()},
            ]},
            {"actions": [
                {"direction": "N", "self_check": check_flags()},
                {"direction": "E", "self_check": check_flags()},
            ]},
        ])

        result = run_r4_segment_route_search(
            client,
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=3,
            max_segment_actions=2,
            max_total_actions=512,
            max_progress_multiplier=2,
            max_feedback_requests=1,
            require_full_segments=True,
        )

        retry_payload = json.loads(client.calls[1][1]["content"])
        self.assertEqual(retry_payload["s"]["n"], self.task.start_node_id)
        self.assertEqual(retry_payload["f"]["code"], "outside_boundary_or_missing_node")
        self.assertEqual(retry_payload["f"]["allowed_directions_at_failure_state"], ["E", "N"])
        self.assertEqual(retry_payload["f"]["progress_safe_directions_at_failure_state"], ["N"])
        history = retry_payload["f"]["recent_rejected_prefix_constraints"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["prefix_directions"], [])
        self.assertEqual(history[0]["failure_node"], self.task.start_node_id)
        self.assertEqual(history[0]["failed_direction"], "S")
        self.assertTrue(result["route_complete"])
        self.assertEqual(result["api_request_count"], 2)

    def test_runner_accepts_an_initial_recovery_feedback_snapshot(self):
        feedback = {
            "code": "one_way_direction",
            "step": 2,
            "direction": "W",
            "from_node_id": "node_0_1",
            "to_node_id": "node_0_0",
            "allowed_directions_at_failure_state": ["E"],
            "rejected_segment_is_atomic": True,
        }
        client = SegmentClient([{"actions": [
            {"direction": "N", "self_check": check_flags()},
            {"direction": "E", "self_check": check_flags()},
        ]}])

        result = run_r4_segment_route_search(
            client,
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=2,
            max_segment_actions=2,
            max_total_actions=512,
            max_progress_multiplier=2,
            max_feedback_requests=1,
            require_full_segments=True,
            initial_rejected_segments=1,
            initial_feedback=feedback,
            request_number_offset=1,
        )

        retry_payload = json.loads(client.calls[0][1]["content"])
        self.assertEqual(retry_payload["f"], feedback)
        self.assertTrue(result["route_complete"])
        self.assertEqual(result["initial_rejected_segment_count"], 1)
        self.assertEqual(result["attempts"][0]["request_number"], 2)

    def test_validator_rejects_whole_segment_atomically_on_endpoint_order_error(self):
        result = validate_r4_segment_proposal(
            self.road_map,
            start_node_id=self.task.start_node_id,
            pickup_node_id=self.task.pickup_node_id,
            dropoff_node_id=self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            pickup_seen=False,
            visited_node_ids=[self.task.start_node_id],
            payload={
                "actions": [
                    {"direction": "N", "self_check": check_flags()},
                    {"direction": "S", "self_check": check_flags()},
                ]
            },
            accepted_action_count=0,
            progress_limit=4,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["violations"][0]["kind"], "node_revisited")
        self.assertEqual(result["committed_edge_sequence"], [])
        self.assertEqual(result["committed_node_sequence"], [])

    def test_validator_rejects_edge_not_in_compact_model_input(self):
        result = validate_r4_segment_proposal(
            self.road_map,
            start_node_id=self.task.start_node_id,
            pickup_node_id=self.task.pickup_node_id,
            dropoff_node_id=self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            pickup_seen=False,
            visited_node_ids=[self.task.start_node_id],
            payload={"actions": [{"direction": "N", "self_check": check_flags()}]},
            accepted_action_count=0,
            progress_limit=4,
            allowed_edge_ids=["oneway_east"],
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["violations"][0]["kind"], "directed_edge_missing")
        self.assertEqual(result["committed_edge_sequence"], [])

    def test_validator_rejects_legal_detour_when_progress_lower_bound_exceeds_budget(self):
        nodes = [
            Node("node_1_1", 1, 1),
            Node("node_0_1", 0, 1),
            Node("node_0_0", 0, 0),
            Node("node_0_-1", 0, -1),
            Node("node_1_-1", 1, -1),
            Node("node_1_0", 1, 0, legal_pickup=True),
            Node("node_2_0", 2, 0, legal_dropoff=True),
        ]
        road_map = RoadMap(
            map_id="detour-map",
            version="v1",
            size=3,
            nodes=nodes,
            edges=[
                Edge("short-1", "node_1_1", "node_1_0", 100, 10),
                Edge("short-2", "node_1_0", "node_2_0", 100, 10),
                Edge("detour-1", "node_1_1", "node_0_1", 100, 10),
                Edge("detour-2", "node_0_1", "node_0_0", 100, 10),
                Edge("detour-3", "node_0_0", "node_0_-1", 100, 10),
                Edge("detour-4", "node_0_-1", "node_1_-1", 100, 10),
                Edge("detour-5", "node_1_-1", "node_1_0", 100, 10),
            ],
            boundary={"min_x": 0, "max_x": 2, "min_y": -1, "max_y": 1},
        )

        result = validate_r4_segment_proposal(
            road_map,
            start_node_id="node_1_1",
            pickup_node_id="node_1_0",
            dropoff_node_id="node_2_0",
            current_node_id="node_1_1",
            pickup_seen=False,
            visited_node_ids=["node_1_1"],
            payload={"actions": [{"direction": "W", "self_check": check_flags()}]},
            accepted_action_count=0,
            progress_limit=4,
        )

        self.assertFalse(result["accepted"])
        self.assertIn("progress_budget_exceeded", [v["kind"] for v in result["violations"]])
        self.assertEqual(result["committed_edge_sequence"], [])

    def test_runner_completes_two_segments_with_independent_local_validation(self):
        client = SegmentClient(
            [
                {"actions": [{"direction": "N", "self_check": check_flags()}]},
                {"actions": [{"direction": "E", "self_check": check_flags()}]},
            ]
        )

        result = run_r4_segment_route_search(
            client,
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=2,
            max_segment_actions=1,
            max_total_actions=512,
            max_progress_multiplier=2,
        )

        self.assertEqual(result["status"], "completed_route")
        self.assertEqual(result["api_request_count"], 2)
        self.assertEqual(result["route"]["direction_sequence"], ["N", "E"])
        self.assertEqual(result["route"]["edge_sequence"], ["north", "east"])
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(audit["accepted"] for audit in result["segment_audits"]))

    def test_runner_requires_remaining_shortest_path_length_in_final_segment_schema(self):
        client = SegmentClient(
            [{"actions": [
                {"direction": "N", "self_check": check_flags()},
                {"direction": "E", "self_check": check_flags()},
            ]}]
        )

        result = run_r4_segment_route_search(
            client,
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=1,
            max_segment_actions=2,
            max_total_actions=512,
            max_progress_multiplier=2,
            require_full_segments=True,
        )

        self.assertEqual(result["status"], "completed_route")
        self.assertEqual(client.route_constraint.schema["properties"]["actions"]["minItems"], 2)
        self.assertEqual(client.route_constraint.schema["properties"]["actions"]["maxItems"], 2)

    def test_runner_persists_request_start_before_call_and_records_result(self):
        client = SegmentClient([{"actions": [{"direction": "N", "self_check": check_flags()}]}])
        events = []
        original_chat = client.chat

        def ordered_chat(messages):
            events.append("call")
            return original_chat(messages)

        client.chat = ordered_chat
        result = run_r4_segment_route_search(
            client,
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=1,
            max_segment_actions=1,
            on_request_start=lambda record: events.append(("start", record)),
            on_attempt=lambda record: events.append(("attempt", record)),
        )

        self.assertEqual(events[0][0], "start")
        self.assertEqual(events[1], "call")
        self.assertEqual(events[2][0], "attempt")
        self.assertEqual(events[0][1]["request_number"], 1)
        self.assertEqual(len(events[0][1]["messages_sha256"]), 64)
        self.assertEqual(result["status"], "segment_request_budget_exhausted")

    def test_runner_durably_records_exact_rendered_cli_prompt_before_call(self):
        client = SegmentClient([{"actions": [{"direction": "N", "self_check": check_flags()}]}])
        client.render_request_prompt = lambda messages: "persistent state-only prompt"
        starts = []

        run_r4_segment_route_search(
            client,
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=1,
            max_segment_actions=1,
            on_request_start=starts.append,
        )

        start = starts[0]
        self.assertEqual(start["rendered_cli_prompt_bytes"], len("persistent state-only prompt".encode("utf-8")))
        self.assertEqual(
            start["rendered_cli_prompt_sha256"],
            __import__("hashlib").sha256(b"persistent state-only prompt").hexdigest(),
        )

    def test_runner_does_not_persist_raw_exception_text_or_retry(self):
        events = []
        result = run_r4_segment_route_search(
            FailingSegmentClient(),
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=1,
            max_segment_actions=1,
            on_request_start=lambda record: events.append(("start", record)),
            on_attempt=lambda record: events.append(("attempt", record)),
        )

        self.assertEqual(result["status"], "api_error")
        self.assertEqual(result["api_request_count"], 1)
        self.assertEqual(result["attempts"][0]["error_type"], "RuntimeError")
        self.assertNotIn("secret", json.dumps(result["attempts"]))
        self.assertEqual(events[0][0], "start")
        self.assertEqual(events[1][0], "attempt")


if __name__ == "__main__":
    unittest.main()
