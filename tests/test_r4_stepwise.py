import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from route_experiment.llm_adapter import build_r4_route_search_messages
from route_experiment.models import Edge, Node, RoadMap, RouteInputPackage, VehicleOrderTask
from route_experiment.r4_constraints import build_r4_route_output_constraint
from route_experiment.routing import edge_travel_time_sec
from route_experiment.r4_stepwise import (
    audit_r4_stepwise_map_equivalence,
    build_r4_step_output_constraint,
    build_r4_stepwise_messages,
    run_r4_stepwise_route_search,
)


class StepSequenceClient:
    def __init__(self, responses, retries=0):
        self.responses = list(responses)
        self.calls = []
        self.route_constraint = None
        self.retries = retries

    def chat(self, messages):
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class R4StepwiseTests(unittest.TestCase):
    def setUp(self):
        nodes = [
            Node("node_0_0", 0, 0),
            Node("node_1_0", 1, 0),
            Node("node_0_1", 0, 1, legal_pickup=True),
            Node("node_1_1", 1, 1, legal_dropoff=True),
        ]
        edges = [
            Edge("edge_0_1", "node_0_0", "node_0_1", 100.0, 10.0),
            Edge("edge_1_2", "node_0_1", "node_1_1", 100.0, 10.0),
            Edge("edge_1_0", "node_0_1", "node_0_0", 100.0, 10.0),
            Edge("edge_0_east", "node_0_0", "node_1_0", 100.0, 10.0, one_way=True),
            Edge("edge_east_north", "node_1_0", "node_1_1", 100.0, 10.0),
        ]
        self.road_map = RoadMap(
            map_id="stepwise-test-map",
            version="v1",
            size=2,
            nodes=nodes,
            edges=edges,
            boundary={"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1},
        )
        start, _, pickup, dropoff = nodes
        self.task = VehicleOrderTask(
            task_id="stepwise-task",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id=start.node_id,
            pickup_node_id=pickup.node_id,
            dropoff_node_id=dropoff.node_id,
        )
        self.package = RouteInputPackage(
            package_id="stepwise-test-package",
            map_id=self.road_map.map_id,
            map_version=self.road_map.version,
            road_state_version=self.road_map.road_state_version,
            random_seed=0,
            start_node_id=start.node_id,
            pickup_node_id=pickup.node_id,
            dropoff_node_id=dropoff.node_id,
            task_type="exact_node",
            pickup_text=None,
            dropoff_text=None,
            nodes=nodes,
            edges=edges,
            expansion_count=4,
            serialized_token_estimate=0,
            boundary=self.road_map.boundary,
        )
        self.full_constraint = build_r4_route_output_constraint(
            self.package, pickup.node_id, dropoff.node_id, allow_failed=False
        )
        self.edge_by_pair = {(edge.from_node, edge.to_node): edge for edge in edges}

    def _response(self, payload):
        return SimpleNamespace(
            content=json.dumps(payload),
            model="offline-fake",
            request_id="offline-fake-request",
            finish_reason="tool_calls",
            input_tokens=10,
            output_tokens=4,
            latency_ms=1.0,
            retries=0,
            output_constraint_mode="strict_step_schema",
            constraint_schema_sha256="test-schema",
            constraint_schema_bytes=100,
        )

    def _move(self, direction, self_check=None):
        return self._response(
            {
                "action": "move",
                "direction": direction,
                "self_check": self_check or {
                    "allowed_directed_edge": True,
                    "starts_at_current": True,
                    "inside_boundary": True,
                    "pickup_order_ok": True,
                    "target_node_is_new": True,
                },
                "failure_reason_code": "none",
            }
        )

    def _give_up(self):
        return self._response(
            {
                "action": "give_up",
                "direction": "",
                "self_check": {
                    "allowed_directed_edge": False,
                    "starts_at_current": True,
                    "inside_boundary": True,
                    "pickup_order_ok": True,
                    "target_node_is_new": True,
                },
                "failure_reason_code": "unable_to_construct_route",
            }
        )

    def _run(self, client, max_api_calls=8):
        client.route_constraint = self.full_constraint
        return run_r4_stepwise_route_search(
            client,
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=max_api_calls,
        )

    def test_stepwise_payload_is_smaller_and_preserves_every_directed_edge_and_cost(self):
        messages = build_r4_stepwise_messages(
            self.package,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            visited_node_ids=[self.task.start_node_id],
        )
        legacy = build_r4_route_search_messages(
            self.package,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            require_completed_route=True,
        )
        compact = json.loads(messages[1]["content"])
        full = json.loads(legacy[1]["content"])
        self.assertLess(len(messages[1]["content"]), len(legacy[1]["content"]))
        self.assertNotIn("map_id", compact["m"])
        self.assertNotIn("map_version", compact["m"])
        self.assertNotIn("road_state_version", compact["m"])
        map_audit = audit_r4_stepwise_map_equivalence(self.package, compact["m"])
        self.assertTrue(map_audit["passed"])

        self.assertEqual(map_audit["expected_semantics_sha256"], map_audit["recovered_semantics_sha256"])
        self.assertEqual(compact["m"]["g"], full["map"]["directional_cost_codes_by_y"])
        for (source, target), edge in self.edge_by_pair.items():
            sx, sy = int(source.split("_")[1]), int(source.split("_")[2])
            tx, ty = int(target.split("_")[1]), int(target.split("_")[2])
            direction_index = {(1, 0): 0, (-1, 0): 1, (0, 1): 2, (0, -1): 3}[(tx - sx, ty - sy)]
            code = compact["m"]["g"][sy - compact["m"]["y0"]][
                (sx - compact["m"]["x0"]) * 4 + direction_index
            ]
            self.assertNotEqual(code, ".")
            self.assertAlmostEqual(compact["m"]["c"][code][0], edge_travel_time_sec(edge), places=6)
            self.assertEqual(compact["m"]["c"][code][1], edge.length_m)

        budgeted = build_r4_stepwise_messages(
            self.package,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            visited_node_ids=[self.task.start_node_id],
            remaining_action_budget=5,
        )
        budget_payload = json.loads(budgeted[1]["content"])
        self.assertEqual(budget_payload["s"]["b"], 5)
        self.assertIn("先规划完整的剩余路线", budgeted[0]["content"])

        altered = dict(compact["m"])
        altered["x0"] = altered["x0"] + 1
        self.assertEqual(
            audit_r4_stepwise_map_equivalence(self.package, altered)["reason"],
            "compact_grid_boundary_mismatch",
        )

    def test_route_feature_context_is_serialized_separately_from_graph_and_rejects_truth(self):
        context = {
            "protocol": {"name": "RoadFeatureInputProtocol", "version": "v1"},
            "pickup_reference": {
                "entry_id": "feature:sign_01",
                "entry_type": "road_sign",
                "name": "北门方向牌",
                "relation_to_feature": "opposite_side",
            },
            "intent": {
                "pickup_anchor_ref": "feature:sign_01",
                "pickup_side": "west",
            },
        }
        messages = build_r4_stepwise_messages(
            self.package,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            current_node_id=self.task.start_node_id,
            visited_node_ids=[self.task.start_node_id],
            route_feature_context=context,
        )
        payload = json.loads(messages[-1]["content"])
        self.assertEqual(payload["i"], context)
        self.assertIn("RoadFeatureInputProtocol v1", messages[0]["content"])
        self.assertEqual(payload["q"]["p"], self.task.pickup_node_id)
        with self.assertRaisesRegex(ValueError, "contains_evaluator_truth"):
            build_r4_stepwise_messages(
                self.package,
                self.task.start_node_id,
                self.task.pickup_node_id,
                self.task.dropoff_node_id,
                current_node_id=self.task.start_node_id,
                visited_node_ids=[self.task.start_node_id],
                route_feature_context={"true_pickup_node_id": self.task.pickup_node_id},
            )

    def test_stepwise_prompt_uses_explicit_visited_nodes_and_positive_self_check(self):
        messages = build_r4_stepwise_messages(
            self.package,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            current_node_id=self.task.pickup_node_id,
            visited_node_ids=[self.task.start_node_id, self.task.pickup_node_id],
            pickup_seen=True,
            accepted_directions=["N"],
        )
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["v"], "r4-step-v2")
        self.assertEqual(
            payload["s"]["v"],
            [self.task.start_node_id, self.task.pickup_node_id],
        )
        self.assertNotIn("a", payload["s"])
        self.assertIn("target_node_is_new", messages[0]["content"])
        self.assertNotIn("not_revisited", messages[0]["content"])
        constraint = build_r4_step_output_constraint(
            self.package,
            self.full_constraint,
            current_node_id=self.task.start_node_id,
            pickup_seen=False,
            visited_node_ids=[self.task.start_node_id],
        )
        self.assertIn(
            "target_node_is_new",
            constraint.schema["properties"]["self_check"]["required"],
        )
        self.assertIn(
            "首次访问的节点",
            constraint.schema["properties"]["self_check"]["properties"]
            ["target_node_is_new"]["description"],
        )

    def test_dynamic_schema_only_offers_directions_with_a_reachable_route_continuation(self):
        constraint = build_r4_step_output_constraint(
            self.package,
            self.full_constraint,
            current_node_id=self.task.start_node_id,
            pickup_seen=False,
            visited_node_ids=[self.task.start_node_id],
        )
        # East is a legal local edge, but taking it leaves no no-repeat route
        # through the pickup and dropoff. The schema must exclude that dead end.
        self.assertEqual(set(constraint.edge_by_direction), {"N"})
        self.assertEqual(constraint.schema["properties"]["direction"]["enum"], ["N"])
        self.assertEqual(constraint.schema["properties"]["action"]["enum"], ["move"])
        self.assertTrue(constraint.tool_definition()["function"]["strict"])

        no_early_dropoff = build_r4_step_output_constraint(
            self.package,
            self.full_constraint,
            current_node_id="node_1_0",
            pickup_seen=False,
            visited_node_ids=["node_0_0", "node_1_0"],
        )
        self.assertEqual(no_early_dropoff.schema["properties"]["direction"]["enum"], [""])
        self.assertEqual(
            no_early_dropoff.schema["properties"]["action"]["enum"],
            ["give_up"],
        )

    def test_dynamic_schema_prunes_detour_that_cannot_fit_remaining_action_budget(self):
        nodes = [
            Node("start", 0, 1),
            Node("pickup", 2, 1, legal_pickup=True),
            Node("dropoff", 2, 2, legal_dropoff=True),
            Node("direct_mid", 1, 1),
            Node("detour_1", 0, 0),
            Node("detour_2", 1, 0),
            Node("detour_3", 2, 0),
            Node("unused_0_2", 0, 2),
            Node("unused_1_2", 1, 2),
        ]
        edges = [
            Edge("direct_1", "start", "direct_mid", 100.0, 10.0),
            Edge("direct_2", "direct_mid", "pickup", 100.0, 10.0),
            Edge("direct_3", "pickup", "dropoff", 100.0, 10.0),
            Edge("detour_1_edge", "start", "detour_1", 100.0, 10.0),
            Edge("detour_2_edge", "detour_1", "detour_2", 100.0, 10.0),
            Edge("detour_3_edge", "detour_2", "detour_3", 100.0, 10.0),
            Edge("detour_to_pickup", "detour_3", "pickup", 100.0, 10.0),
        ]
        package = replace(
            self.package,
            start_node_id="start",
            pickup_node_id="pickup",
            dropoff_node_id="dropoff",
            nodes=nodes,
            edges=edges,
            expansion_count=len(nodes),
            boundary={"min_x": 0, "max_x": 2, "min_y": 0, "max_y": 2},
        )
        constraint = build_r4_route_output_constraint(
            package, "pickup", "dropoff", allow_failed=False
        )

        step_constraint = build_r4_step_output_constraint(
            package,
            constraint,
            current_node_id="start",
            pickup_seen=False,
            visited_node_ids=["start"],
            remaining_action_budget=3,
        )

        self.assertEqual(set(step_constraint.edge_by_direction), {"E"})
        self.assertEqual(step_constraint.filtered_directions["S"], "remaining_action_budget_insufficient")
        self.assertEqual(step_constraint.schema["properties"]["direction"]["enum"], ["E"])

    def test_blocked_edge_is_removed_from_schema_and_locally_rejected_if_proposed(self):
        blocked_package = replace(self.package, blocked_edge_ids=["edge_0_east"])
        constrained = build_r4_step_output_constraint(
            blocked_package,
            self.full_constraint,
            current_node_id=self.task.start_node_id,
            pickup_seen=False,
            visited_node_ids=[self.task.start_node_id],
        )
        self.assertEqual(constrained.schema["properties"]["direction"]["enum"], ["N"])
        client = StepSequenceClient([self._move("E")])
        client.route_constraint = self.full_constraint
        result = run_r4_stepwise_route_search(
            client,
            blocked_package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=1,
        )
        self.assertEqual(result["attempts"][0]["status"], "step_invalid")
        self.assertIn("blocked_edge", result["attempts"][0]["local_checks"]["errors"])
        self.assertEqual(result["attempts"][0]["request_state"]["n"], self.task.start_node_id)

    def test_give_up_is_allowed_when_only_remaining_path_repeats_a_visited_node(self):
        nodes = [
            Node("node_0_0", 0, 0),
            Node("node_1_0", 1, 0),
            Node("node_0_1", 0, 1, legal_pickup=True),
            Node("node_1_1", 1, 1, legal_dropoff=True),
            Node("node_2_0", 2, 0),
            Node("node_2_1", 2, 1),
        ]
        edges = [
            Edge("start_to_current", "node_0_0", "node_1_0", 100.0, 10.0),
            Edge("current_to_visited_start", "node_1_0", "node_0_0", 100.0, 10.0),
            Edge("start_to_pickup", "node_0_0", "node_0_1", 100.0, 10.0),
            Edge("pickup_to_dropoff", "node_0_1", "node_1_1", 100.0, 10.0),
            Edge("current_to_deadend", "node_1_0", "node_2_0", 100.0, 10.0),
        ]
        package = replace(
            self.package,
            nodes=nodes,
            edges=edges,
            expansion_count=len(nodes),
            boundary={"min_x": 0, "max_x": 2, "min_y": 0, "max_y": 1},
        )
        full_constraint = build_r4_route_output_constraint(
            package, "node_0_1", "node_1_1", allow_failed=False
        )

        constraint = build_r4_step_output_constraint(
            package,
            full_constraint,
            current_node_id="node_1_0",
            pickup_seen=False,
            visited_node_ids=["node_0_0", "node_1_0"],
        )

        self.assertEqual(set(constraint.edge_by_direction), set())
        self.assertEqual(constraint.schema["properties"]["action"]["enum"], ["give_up"])
        self.assertEqual(constraint.schema["properties"]["direction"]["enum"], [""])

    def test_invalid_direction_does_not_advance_state_then_legal_moves_finish_route(self):
        invalid = self._move("W")
        client = StepSequenceClient([invalid, self._move("N"), self._move("E")])
        result = self._run(client, max_api_calls=3)
        self.assertEqual(result["status"], "route_validated")
        self.assertEqual(result["api_request_count"], 3)
        self.assertEqual(result["route"]["edge_sequence"], ["edge_0_1", "edge_1_2"])
        self.assertEqual(result["route"]["validation_status"], "valid")
        self.assertEqual(result["route"]["validation_errors"], [])
        first_user = json.loads(client.calls[0][-1]["content"])
        retry_user = json.loads(client.calls[1][-1]["content"])
        first_state = dict(first_user["s"])
        retry_state = dict(retry_user["s"])
        self.assertEqual(first_state.pop("b"), retry_state.pop("b") + 1)
        self.assertEqual(first_state, retry_state)
        self.assertEqual(retry_user["f"]["code"], "directed_edge_missing")
        self.assertEqual(retry_user["f"]["direction"], "W")
        self.assertFalse(result["steps"][0]["accepted_step"])
        self.assertTrue(all(step["local_checks"]["passed"] for step in result["steps"][1:]))
        self.assertEqual(result["attempts"][0]["status"], "step_invalid")
        self.assertIn("input_user_sha256", result["attempts"][0])

    def test_false_model_self_check_is_recorded_but_local_validator_controls_acceptance(self):
        failed_self_check = {
            "allowed_directed_edge": True,
            "starts_at_current": True,
            "inside_boundary": True,
            "pickup_order_ok": True,
            "target_node_is_new": False,
        }
        client = StepSequenceClient([self._move("N", failed_self_check), self._move("E")])
        result = self._run(client, max_api_calls=2)
        attempt = result["attempts"][0]
        self.assertEqual(result["status"], "route_validated")
        self.assertEqual(attempt["status"], "step_accepted")
        self.assertNotIn("model_self_check_failed", attempt["local_checks"]["errors"])
        self.assertTrue(attempt["local_checks"]["passed"])
        self.assertFalse(attempt["local_checks"]["flags"]["model_self_check_passed"])
        self.assertEqual(attempt["model_self_check_mismatch_fields"], ["target_node_is_new"])
        self.assertTrue(result["steps"][0]["accepted_step"])
        self.assertEqual(result["steps"][0]["to_node_id"], "node_0_1")

    def test_reachable_task_rejects_model_give_up_and_continues_same_state(self):
        client = StepSequenceClient([self._give_up(), self._move("N"), self._move("E")])
        result = self._run(client, max_api_calls=3)

        self.assertEqual(result["status"], "route_validated")
        self.assertEqual(result["api_request_count"], 3)
        rejected = result["attempts"][0]
        self.assertEqual(rejected["status"], "step_invalid")
        self.assertIn("give_up_while_route_reachable", rejected["local_checks"]["errors"])
        self.assertEqual(rejected["request_state"]["n"], self.task.start_node_id)
        retry = json.loads(client.calls[1][-1]["content"])
        first_state = dict(json.loads(client.calls[0][-1]["content"])["s"])
        retry_state = dict(retry["s"])
        self.assertEqual(first_state.pop("b"), retry_state.pop("b") + 1)
        self.assertEqual(retry_state, first_state)
        self.assertEqual(retry["f"]["code"], "give_up_while_route_reachable")

    def test_budget_insufficient_can_be_reported_as_give_up(self):
        client = StepSequenceClient([self._move("E"), self._give_up()])
        client.route_constraint = build_r4_route_output_constraint(
            self.package,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            allow_failed=False,
        )
        result = run_r4_stepwise_route_search(
            client,
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=2,
        )

        self.assertEqual(result["status"], "model_gave_up")
        self.assertEqual(result["attempts"][0]["status"], "step_invalid")
        self.assertIn(
            "move_leaves_no_reachable_route",
            result["attempts"][0]["local_checks"]["errors"],
        )
        self.assertEqual(result["attempts"][1]["remaining_route_reachable"], True)
        self.assertEqual(
            result["attempts"][1]["termination_reason"],
            "remaining_action_budget_insufficient",
        )

    def test_output_parse_failure_keeps_state_and_sends_short_structured_feedback(self):
        malformed = SimpleNamespace(content="not-json", retries=0)
        client = StepSequenceClient([malformed, self._move("N"), self._move("E")])
        result = self._run(client, max_api_calls=3)
        self.assertEqual(result["status"], "route_validated")
        first = json.loads(client.calls[0][-1]["content"])
        second = json.loads(client.calls[1][-1]["content"])
        first_state = dict(first["s"])
        second_state = dict(second["s"])
        self.assertEqual(first_state.pop("b"), second_state.pop("b") + 1)
        self.assertEqual(first_state, second_state)
        self.assertEqual(second["f"]["code"], "r4_step_output_json_invalid")
        self.assertEqual(result["attempts"][0]["status"], "step_output_invalid")

    def test_global_api_call_cap_stops_without_a_fallback(self):
        client = StepSequenceClient([self._move("W"), self._move("W"), self._move("N")])
        result = self._run(client, max_api_calls=2)
        self.assertEqual(result["status"], "step_call_budget_exhausted")
        self.assertEqual(result["api_request_count"], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertIsNone(result["route"])

    def test_route_action_budget_is_separate_from_request_limit(self):
        client = StepSequenceClient([self._move("N")])
        client.route_constraint = self.full_constraint
        result = run_r4_stepwise_route_search(
            client,
            self.package,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_api_calls=1,
            max_route_actions=8,
        )

        sent = json.loads(client.calls[0][-1]["content"])
        self.assertEqual(result["status"], "request_limit_exhausted")
        self.assertEqual(result["api_request_count"], 1)
        self.assertEqual(result["api_call_limit"], 1)
        self.assertEqual(result["route_action_budget"], 8)
        self.assertEqual(result["route_action_budget_mode"], "separate_from_request_limit")
        self.assertEqual(sent["s"]["b"], 8)
        self.assertEqual(result["steps"][0]["to_node_id"], self.task.pickup_node_id)
        self.assertIsNone(result["route"])

    def test_network_error_is_recorded_without_retry_or_route_fallback(self):
        client = StepSequenceClient([RuntimeError("offline test transport failed")])
        result = self._run(client, max_api_calls=2)
        self.assertEqual(result["status"], "api_error")
        self.assertEqual(result["api_request_count"], 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["attempts"][0]["status"], "api_error")
        self.assertIsNone(result["route"])
        self.assertFalse(result["usage"]["usage_complete"])
        self.assertIsNone(result["usage"]["input_tokens"])
        self.assertIsNone(result["usage"]["output_tokens"])
        self.assertIsNone(result["usage"]["llm_latency_ms"])

    def test_rejects_nonzero_network_retry_setting_before_call(self):
        client = StepSequenceClient([self._move("N")], retries=1)
        client.route_constraint = self.full_constraint
        with self.assertRaisesRegex(RuntimeError, "network_retries_must_be_zero"):
            run_r4_stepwise_route_search(
                client,
                self.package,
                self.road_map,
                self.task,
                self.task.pickup_node_id,
                self.task.dropoff_node_id,
                max_api_calls=1,
            )
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
