import json
import urllib.error
import unittest

from route_experiment.llm_adapter import (
    DeepSeekClient,
    build_r4_route_search_messages,
    validate_llm_route,
)
from route_experiment.models import (
    Edge,
    Node,
    RoadMap,
    RouteCandidate,
    RouteInputPackage,
    VehicleOrderTask,
)
from route_experiment.r4_constraints import build_r4_route_output_constraint


class R4RouteConstraintTests(unittest.TestCase):
    def setUp(self):
        self.package = RouteInputPackage(
            package_id="package-one-way-fixture",
            map_id="map-one-way-fixture",
            map_version="v1",
            road_state_version="road-state-v1",
            random_seed=0,
            start_node_id="node_0_0",
            pickup_node_id="node_1_0",
            dropoff_node_id="node_1_1",
            task_type="exact_node",
            pickup_text=None,
            dropoff_text=None,
            nodes=[
                Node("node_0_0", 0, 0),
                Node("node_1_0", 1, 0, legal_pickup=True),
                Node("node_0_1", 0, 1),
                Node("node_1_1", 1, 1, legal_dropoff=True),
            ],
            edges=[
                Edge(
                    "edge_node_0_0_node_1_0",
                    "node_0_0",
                    "node_1_0",
                    100.0,
                    10.0,
                    one_way=True,
                ),
                Edge(
                    "edge_node_1_0_node_1_1",
                    "node_1_0",
                    "node_1_1",
                    100.0,
                    10.0,
                    one_way=True,
                ),
                Edge(
                    "edge_node_0_1_node_1_1",
                    "node_0_1",
                    "node_1_1",
                    100.0,
                    10.0,
                    one_way=True,
                ),
            ],
            expansion_count=0,
            serialized_token_estimate=0,
            boundary={"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1},
        )

    def test_offline_gate_matches_map_edges_schema_and_validator(self):
        road_map = RoadMap(
            map_id=self.package.map_id,
            version=self.package.map_version,
            size=2,
            nodes=self.package.nodes,
            edges=self.package.edges,
            boundary=self.package.boundary,
        )
        task = VehicleOrderTask(
            task_id="offline-gate-task",
            vehicle_id="offline-gate-vehicle",
            order_id="offline-gate-order",
            start_node_id="node_0_0",
            pickup_node_id="node_1_0",
            dropoff_node_id="node_1_1",
        )

        outgoing = {}
        for edge in road_map.edges:
            outgoing.setdefault(edge.from_node, []).append(edge.to_node)
        complete_paths = []

        def enumerate_paths(node_id, path):
            if node_id == task.dropoff_node_id:
                complete_paths.append(path)
                return
            for next_node_id in outgoing.get(node_id, []):
                if next_node_id not in path:
                    enumerate_paths(next_node_id, path + [next_node_id])

        enumerate_paths(task.start_node_id, [task.start_node_id])
        task_paths = [
            path
            for path in complete_paths
            if task.pickup_node_id in path
            and path.index(task.pickup_node_id) < path.index(task.dropoff_node_id)
        ]
        self.assertEqual(task_paths, [["node_0_0", "node_1_0", "node_1_1"]])

        messages = build_r4_route_search_messages(
            self.package,
            task.start_node_id,
            task.pickup_node_id,
            task.dropoff_node_id,
        )
        user_input = json.loads(messages[-1]["content"])
        encoded_rows = user_input["map"]["directional_cost_codes_by_y"]
        width = user_input["map"]["width"]
        map_slots = {
            y * width * 4 + offset
            for y, row in enumerate(encoded_rows)
            for offset, code in enumerate(row)
            if code != "."
        }

        constraint = build_r4_route_output_constraint(
            self.package, task.pickup_node_id, task.dropoff_node_id
        )
        schema_slots = {int(code, 36) for code in constraint.edge_by_slot_code}
        self.assertEqual(map_slots, schema_slots)
        self.assertEqual(map_slots, {0, 6, 8})

        # The one-way pickup→dropoff road has no reverse dropoff→pickup slot.
        self.assertNotIn(15, map_slots)
        self.assertNotRegex("00F", constraint.edge_slot_pattern)

        valid_payload = {
            "status": "completed",
            "pickup_node_id": task.pickup_node_id,
            "dropoff_node_id": task.dropoff_node_id,
            "edge_slot_sequence": "000006",
            "step_check_sequence": "11",
            "failure_reason_code": "none",
        }
        normalized = constraint.normalize_payload(valid_payload)
        valid_route = RouteCandidate(
            route_id="offline-gate-unique-route",
            node_sequence=normalized["node_sequence"],
            edge_sequence=normalized["edge_sequence"],
        )
        validation = validate_llm_route(
            road_map,
            task,
            valid_route,
            selected_pickup_node_id=task.pickup_node_id,
            selected_dropoff_node_id=task.dropoff_node_id,
        )
        self.assertTrue(validation.valid, validation.errors)
        self.assertEqual(
            valid_route.node_sequence,
            ["node_0_0", "node_1_0", "node_1_1"],
        )
        self.assertEqual(len(normalized["step_check_records"]), 2)

        # Each edge is individually legal, but the second edge starts elsewhere.
        discontinuous_payload = dict(valid_payload)
        discontinuous_payload["edge_slot_sequence"] = "000008"
        discontinuous_payload["step_check_sequence"] = "11"
        discontinuous = constraint.normalize_payload(discontinuous_payload)
        invalid_route = RouteCandidate(
            route_id="offline-gate-discontinuous-route",
            node_sequence=discontinuous["node_sequence"],
            edge_sequence=discontinuous["edge_sequence"],
        )
        invalid_validation = validate_llm_route(
            road_map,
            task,
            invalid_route,
            selected_pickup_node_id=task.pickup_node_id,
            selected_dropoff_node_id=task.dropoff_node_id,
        )
        self.assertFalse(invalid_validation.valid)
        self.assertIn("route_edge_node_discontinuity", invalid_validation.errors)

    def test_schema_pattern_allows_forward_one_way_edge_and_rejects_reverse(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )

        self.assertRegex("000", constraint.edge_slot_pattern)
        self.assertNotRegex("005", constraint.edge_slot_pattern)
        self.assertEqual(
            constraint.edge_id_by_slot_code["000"],
            "edge_node_0_0_node_1_0",
        )
        self.assertNotIn("005", constraint.edge_id_by_slot_code)

    def test_normalization_maps_only_schema_allowed_slots_to_directed_edges(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )

        normalized = constraint.normalize_payload(
            {
                "status": "completed",
                "pickup_node_id": "node_1_0",
                "dropoff_node_id": "node_1_1",
                "edge_slot_sequence": "000006",
                "step_check_sequence": "11",
                "failure_reason_code": "none",
            }
        )

        self.assertEqual(
            normalized["edge_sequence"],
            [
                "edge_node_0_0_node_1_0",
                "edge_node_1_0_node_1_1",
            ],
        )
        self.assertEqual(
            normalized["node_sequence"],
            ["node_0_0", "node_1_0", "node_1_1"],
        )
        self.assertEqual(normalized["step_check_sequence"], "11")
        self.assertEqual(normalized["reason"], None)

    def test_failed_payload_preserves_distinction_between_no_path_and_unable_to_plan(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )

        no_path = constraint.normalize_payload(
            {
                "status": "failed",
                "pickup_node_id": "node_1_0",
                "dropoff_node_id": "node_1_1",
                "edge_slot_sequence": "",
                "step_check_sequence": "",
                "failure_reason_code": "no_directed_path",
            }
        )
        unable = constraint.normalize_payload(
            {
                "status": "failed",
                "pickup_node_id": "node_1_0",
                "dropoff_node_id": "node_1_1",
                "edge_slot_sequence": "",
                "step_check_sequence": "",
                "failure_reason_code": "unable_to_construct_route",
            }
        )

        self.assertEqual(no_path["reason"], "model_confirmed_no_directed_path")
        self.assertEqual(unable["reason"], "model_unable_to_construct_route")

    def test_failed_reason_code_is_required_and_cannot_claim_no_path_without_code(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )
        payload = {
            "status": "failed",
            "pickup_node_id": "node_1_0",
            "dropoff_node_id": "node_1_1",
            "edge_slot_sequence": "",
            "step_check_sequence": "",
        }

        with self.assertRaisesRegex(ValueError, "r4_failure_reason_code_invalid"):
            constraint.normalize_payload(payload)

    def test_preverified_task_constraint_requires_a_nonempty_completed_route(self):
        constraint = build_r4_route_output_constraint(
            self.package,
            "node_1_0",
            "node_1_1",
            allow_failed=False,
        )
        schema = constraint.tool_definition()["function"]["parameters"]

        self.assertEqual(schema["properties"]["status"]["enum"], ["completed"])
        self.assertEqual(
            schema["properties"]["failure_reason_code"]["enum"], ["none"]
        )
        self.assertNotRegex("", constraint.edge_slot_pattern)
        self.assertRegex("000006", constraint.edge_slot_pattern)
        with self.assertRaisesRegex(ValueError, "r4_failed_response_not_allowed"):
            constraint.normalize_payload(
                {
                    "status": "failed",
                    "pickup_node_id": "node_1_0",
                    "dropoff_node_id": "node_1_1",
                    "edge_slot_sequence": "",
                    "step_check_sequence": "",
                    "failure_reason_code": "unable_to_construct_route",
                }
            )

    def test_completed_route_must_not_carry_a_failure_code(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )
        payload = {
            "status": "completed",
            "pickup_node_id": "node_1_0",
            "dropoff_node_id": "node_1_1",
            "edge_slot_sequence": "000006",
            "step_check_sequence": "11",
            "failure_reason_code": "unable_to_construct_route",
        }

        with self.assertRaisesRegex(ValueError, "r4_failure_reason_code_status_mismatch"):
            constraint.normalize_payload(payload)

    def test_normalization_rejects_a_slot_not_present_in_the_directed_graph(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )

        with self.assertRaisesRegex(ValueError, "r4_edge_slot_constraint_violated"):
            constraint.normalize_payload(
                {
                    "status": "completed",
                    "pickup_node_id": "node_1_0",
                    "dropoff_node_id": "node_1_1",
                    "edge_slot_sequence": "000005",
                    "step_check_sequence": "11",
                    "failure_reason_code": "none",
                }
            )

    def test_completed_route_requires_one_self_check_per_edge(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )
        schema = constraint.tool_definition()["function"]["parameters"]
        self.assertIn("step_check_sequence", schema["required"])
        self.assertIn("failure_reason_code", schema["required"])
        self.assertEqual(
            schema["properties"]["failure_reason_code"]["enum"],
            ["none", "no_directed_path", "unable_to_construct_route"],
        )
        self.assertEqual(
            schema["properties"]["step_check_sequence"]["pattern"], "^1{0,3}$"
        )

    def test_edge_and_self_check_sequences_are_bounded_by_simple_path_length(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1", allow_failed=False
        )
        schema = constraint.tool_definition()["function"]["parameters"]
        valid_code = next(iter(constraint.edge_id_by_slot_code))
        max_edges = len(self.package.nodes) - 1
        within_bound = valid_code * max_edges
        over_bound = valid_code * (max_edges + 1)

        self.assertRegex(within_bound, constraint.edge_slot_pattern)
        self.assertNotRegex(over_bound, constraint.edge_slot_pattern)
        self.assertRegex("1" * max_edges, schema["properties"]["step_check_sequence"]["pattern"])
        self.assertNotRegex("1" * (max_edges + 1), schema["properties"]["step_check_sequence"]["pattern"])

        with self.assertRaisesRegex(ValueError, "r4_route_edge_sequence_too_long"):
            constraint.normalize_payload(
                {
                    "status": "completed",
                    "pickup_node_id": "node_1_0",
                    "dropoff_node_id": "node_1_1",
                    "edge_slot_sequence": over_bound,
                    "step_check_sequence": "1" * (max_edges + 1),
                    "failure_reason_code": "none",
                }
            )

        with self.assertRaisesRegex(
            ValueError, "r4_step_check_sequence_count_mismatch"
        ):
            constraint.normalize_payload(
                {
                    "status": "completed",
                    "pickup_node_id": "node_1_0",
                    "dropoff_node_id": "node_1_1",
                    "edge_slot_sequence": "000006",
                    "step_check_sequence": "1",
                    "failure_reason_code": "none",
                }
            )

    def test_client_forces_strict_tool_output_with_the_map_derived_schema(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )
        arguments = json.dumps(
            {
                "status": "completed",
                "pickup_node_id": "node_1_0",
                "dropoff_node_id": "node_1_1",
                "edge_slot_sequence": "000006",
                "step_check_sequence": "11",
                "failure_reason_code": "none",
            }
        )
        captured = {}

        def transport(url, headers, body, timeout):
            captured.update({"url": url, "body": body})
            return {
                "model": "fake-deepseek",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "submit_r4_route",
                                        "arguments": arguments,
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }

        client = DeepSeekClient(
            api_key="offline-test-key",
            retries=0,
            transport=transport,
            route_constraint=constraint,
        )
        response = client.chat(
            [
                {"role": "system", "content": "route only"},
                {"role": "user", "content": "{}"},
            ]
        )

        self.assertEqual(captured["url"], "https://api.deepseek.com/beta/chat/completions")
        self.assertEqual(captured["body"]["tool_choice"], constraint.tool_choice)
        tool = captured["body"]["tools"][0]
        self.assertTrue(tool["function"]["strict"])
        self.assertEqual(
            tool["function"]["parameters"]["properties"]["edge_slot_sequence"]["pattern"],
            constraint.edge_slot_pattern,
        )
        self.assertNotIn("response_format", captured["body"])
        self.assertEqual(response.content, arguments)
        self.assertEqual(
            response.output_constraint_mode,
            "strict_directed_edge_schema",
        )
        self.assertEqual(response.finish_reason, "tool_calls")

    def test_client_preserves_malformed_tool_arguments_and_provider_usage(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )
        malformed = '{"status":"completed","edge_slot_sequence":"000'

        def transport(url, headers, body, timeout):
            return {
                "model": "fake-deepseek",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "submit_r4_route",
                                        "arguments": malformed,
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 123, "completion_tokens": 17},
            }

        client = DeepSeekClient(
            api_key="offline-test-key",
            retries=0,
            transport=transport,
            route_constraint=constraint,
        )
        response = client.chat([{"role": "user", "content": "{}"}])

        self.assertEqual(response.content, malformed)
        self.assertEqual(response.input_tokens, 123)
        self.assertEqual(response.output_tokens, 17)
        self.assertEqual(response.finish_reason, "length")

    def test_constrained_route_client_never_retries_a_transport_failure(self):
        constraint = build_r4_route_output_constraint(
            self.package, "node_1_0", "node_1_1"
        )
        calls = []

        def transport(*args):
            calls.append(args)
            raise urllib.error.URLError("offline fixture")

        client = DeepSeekClient(
            api_key="offline-test-key",
            retries=3,
            transport=transport,
            route_constraint=constraint,
        )

        with self.assertRaisesRegex(RuntimeError, "deepseek_request_failed"):
            client.chat([{"role": "user", "content": "{}"}])

        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
