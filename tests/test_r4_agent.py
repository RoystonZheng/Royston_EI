import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from route_experiment.mapgen import generate_map
from route_experiment.models import MapConfig, VehicleOrderTask
from route_experiment.r4_constraints import build_r4_route_output_constraint
from route_experiment.llm_adapter import DeepSeekResponseParseError
from route_experiment.r4_agent import (
    audit_partial_edge_slot_output,
    needs_edge_slot_proposal_audit,
    replay_directed_edge_sequence,
    run_r4_route_search,
)
from route_experiment.routing import build_route_input_package, run_traditional_route


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.before_call = None

    def chat(self, messages):
        if self.before_call is not None:
            self.before_call(len(self.calls) + 1)
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class R4AgentTests(unittest.TestCase):
    def setUp(self):
        self.road_map = generate_map(seed=3, size=12, config=MapConfig(size=12))
        pickup = next(node for node in self.road_map.nodes if node.legal_pickup)
        dropoff = next(
            node
            for node in self.road_map.nodes
            if node.legal_dropoff and node.node_id != pickup.node_id
        )
        start = next(
            node
            for node in self.road_map.nodes
            if node.node_id not in {pickup.node_id, dropoff.node_id}
        )
        self.task = VehicleOrderTask(
            task_id="r4-task-1",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id=start.node_id,
            pickup_node_id=pickup.node_id,
            dropoff_node_id=dropoff.node_id,
            task_type="exact_node",
        )
        self.baseline = run_traditional_route(
            self.road_map, self.task, "full_map_dijkstra"
        )
        self.assertEqual(self.baseline.status, "completed")
        self.valid_route = self.baseline.routes[0]
        self.base_messages = [
            {"role": "system", "content": "search only listed roads"},
            {"role": "user", "content": '{"map":"fixture"}'},
        ]

    def _response(self, payload, tokens=5, latency_ms=10.0):
        return SimpleNamespace(
            content=json.dumps(payload),
            model="fake-deepseek",
            input_tokens=tokens,
            output_tokens=2,
            latency_ms=latency_ms,
            retries=0,
            request_id="fake-request",
        )

    def _valid_response(self):
        return self._response(
            {
                "status": "completed",
                "pickup_node_id": self.task.pickup_node_id,
                "dropoff_node_id": self.task.dropoff_node_id,
                "node_sequence": self.valid_route.node_sequence,
                "edge_sequence": self.valid_route.edge_sequence,
            },
            tokens=7,
            latency_ms=12.0,
        )

    def _invalid_response(self):
        return self._response(
            {
                "status": "completed",
                "pickup_node_id": self.task.pickup_node_id,
                "dropoff_node_id": self.task.dropoff_node_id,
                "move_sequence": "E" * 20,
            }
        )

    def test_directed_edge_replay_marks_first_discontinuous_edge(self):
        first = next(
            edge for edge in self.road_map.edges
            if edge.from_node == self.task.start_node_id
        )
        second = next(
            edge for edge in self.road_map.edges
            if edge.from_node != first.to_node
        )

        replay = replay_directed_edge_sequence(
            self.road_map,
            self.task.start_node_id,
            [first.edge_id, second.edge_id],
        )

        self.assertEqual(replay["first_violation_step"], 2)
        self.assertEqual(replay["move_violations"][0]["kind"], "route_edge_node_discontinuity")
        self.assertEqual(replay["validated_prefix_edge_sequence"], [first.edge_id])

    def test_truncated_slot_output_is_audited_without_becoming_a_complete_route(self):
        package = build_route_input_package(self.road_map, self.task)
        constraint = build_r4_route_output_constraint(
            package, self.task.pickup_node_id, self.task.dropoff_node_id
        )
        node_by_id = {node.node_id: node for node in package.nodes}
        min_x = min(node.x for node in package.nodes)
        max_x = max(node.x for node in package.nodes)
        min_y = min(node.y for node in package.nodes)
        width = max_x - min_x + 1
        slot = ((min_y - min_y) * width + (max_x - min_x)) * 4
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        chars = []
        while slot:
            chars.append(alphabet[slot % 36])
            slot //= 36
        code = "".join(reversed(chars or ["0"])).rjust(3, "0")

        audit = audit_partial_edge_slot_output(
            self.road_map,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            '{"edge_slot_sequence":"' + code,
            constraint,
        )

        self.assertFalse(audit["complete_route"])
        self.assertEqual(audit["complete_slot_count"], 1)
        self.assertEqual(audit["invalid_edge_slot_count"], 1)
        self.assertEqual(audit["replay"]["move_violations"][0]["kind"], "missing_directed_edge")
        self.assertFalse(audit["model_step_checks_available"])
        self.assertIsNotNone(audit["energy_estimate"]["route_energy_estimate_kwh"])

    def test_complete_rejected_slot_output_keeps_self_check_count_and_audits(self):
        package = build_route_input_package(self.road_map, self.task)
        constraint = build_r4_route_output_constraint(
            package, self.task.pickup_node_id, self.task.dropoff_node_id
        )
        max_x = max(node.x for node in package.nodes)
        min_x = min(node.x for node in package.nodes)
        min_y = min(node.y for node in package.nodes)
        width = max_x - min_x + 1
        slot = ((min_y - min_y) * width + (max_x - min_x)) * 4
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        chars = []
        while slot:
            chars.append(alphabet[slot % 36])
            slot //= 36
        code = "".join(reversed(chars or ["0"])).rjust(3, "0")
        raw = json.dumps(
            {
                "status": "completed",
                "pickup_node_id": self.task.pickup_node_id,
                "dropoff_node_id": self.task.dropoff_node_id,
                "edge_slot_sequence": code,
                "step_check_sequence": "111",
                "failure_reason_code": "none",
            }
        )

        audit = audit_partial_edge_slot_output(
            self.road_map,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            raw,
            constraint,
        )

        self.assertEqual(audit["complete_slot_count"], 1)
        self.assertTrue(audit["model_step_checks_available"])
        self.assertEqual(audit["model_step_check_count"], 3)
        self.assertFalse(audit["model_step_check_count_matches_route"])
        self.assertEqual(
            audit["replay"]["step_check_records"][0]["model_edge_check"],
            "UNALIGNABLE_COUNT_MISMATCH",
        )
        self.assertIn("complete model response", audit["interpretation"])

    def test_constraint_violations_are_retained_for_offline_audit(self):
        self.assertTrue(needs_edge_slot_proposal_audit("route_constraint_violation"))
        self.assertTrue(needs_edge_slot_proposal_audit("route_output_parse_failed"))
        self.assertFalse(needs_edge_slot_proposal_audit("model_returned_failed"))

    def test_invalid_route_gets_structured_feedback_then_valid_route_stops(self):
        client = SequenceClient([self._invalid_response(), self._valid_response()])
        persisted_attempts = []
        client.before_call = lambda call_number: self.assertEqual(
            len(persisted_attempts), call_number - 1
        )
        task_with_hidden_truth = replace(
            self.task,
            true_pickup_node_id="ground_truth_pickup_secret",
            true_dropoff_node_id="ground_truth_dropoff_secret",
        )

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            task_with_hidden_truth,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=3,
            on_attempt=lambda snapshot: persisted_attempts.append(
                snapshot["attempts"][-1]["attempt"]
            ),
        )

        self.assertEqual(result["status"], "route_validated")
        self.assertEqual(result["api_request_count"], 2)
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(persisted_attempts, [1, 2])
        self.assertTrue(result["attempts"][0]["validation"]["errors"])
        self.assertEqual(result["attempts"][1]["status"], "route_validated")
        second_request = client.calls[1]
        feedback_message = second_request[-1]
        self.assertEqual(feedback_message["role"], "user")
        second_input = json.loads(feedback_message["content"])
        self.assertEqual(second_input["map"], "fixture")
        feedback = second_input["r4_validation_feedback"]
        self.assertEqual(feedback["schema_version"], "r4_route_validation_feedback_v1")
        self.assertIn("route_validation_errors", feedback)
        self.assertEqual(len(second_input), 2)
        self.assertNotIn("ground_truth_pickup_secret", json.dumps(second_input))
        self.assertNotIn("ground_truth_dropoff_secret", json.dumps(second_input))
        serialized_second_request = json.dumps(second_request)
        self.assertNotIn(result["attempts"][0]["raw_output"], serialized_second_request)
        self.assertEqual(result["usage"]["input_tokens"], 12)
        self.assertEqual(result["usage"]["output_tokens"], 4)

    def test_invalid_routes_stop_after_three_planning_attempts(self):
        client = SequenceClient(
            [self._invalid_response(), self._invalid_response(), self._invalid_response()]
        )

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=3,
        )

        self.assertEqual(result["status"], "planning_attempts_exhausted")
        self.assertEqual(result["api_request_count"], 3)
        self.assertEqual(len(result["attempts"]), 3)
        self.assertTrue(all(item["feedback"] is not None for item in result["attempts"][:2]))
        self.assertIsNone(result["attempts"][2]["feedback"])

    def test_valid_first_route_does_not_make_an_extra_request(self):
        client = SequenceClient([self._valid_response(), self._invalid_response()])

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=3,
        )

        self.assertEqual(result["status"], "route_validated")
        self.assertEqual(result["api_request_count"], 1)
        self.assertEqual(len(client.responses), 1)

    def test_malformed_model_output_keeps_usage_instead_of_becoming_api_error(self):
        package = build_route_input_package(self.road_map, self.task)
        constraint = build_r4_route_output_constraint(
            package, self.task.pickup_node_id, self.task.dropoff_node_id
        )
        malformed = '{"status":"completed","edge_slot_sequence":"000'
        response = SimpleNamespace(
            content=malformed,
            model="fake-deepseek",
            finish_reason="length",
            input_tokens=123,
            output_tokens=17,
            latency_ms=456.0,
            retries=0,
            request_id="fake-malformed-request",
        )
        client = SequenceClient([response])
        client.route_constraint = constraint

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=1,
            route_constraint=constraint,
        )

        self.assertEqual(result["status"], "planning_attempts_exhausted")
        self.assertEqual(result["attempts"][0]["status"], "route_output_parse_failed")
        self.assertEqual(result["attempts"][0]["raw_output"], malformed)
        self.assertEqual(result["attempts"][0]["finish_reason"], "length")
        self.assertEqual(result["usage"]["input_tokens"], 123)
        self.assertEqual(result["usage"]["output_tokens"], 17)
        self.assertEqual(result["usage"]["llm_latency_ms"], 456.0)

    def test_unparseable_provider_body_is_preserved_as_scrubbed_diagnostic(self):
        response_body = b'{"error":"Bearer sk-ABCDEFGHIJKLMNOPQRSTUV"'
        client = SequenceClient([DeepSeekResponseParseError(response_body)])

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=1,
        )

        attempt = result["attempts"][0]
        audit = attempt["response_parse_audit"]
        self.assertEqual(audit["response_body_bytes"], len(response_body))
        self.assertEqual(len(audit["response_body_sha256"]), 64)
        self.assertIn("[redacted]", audit["response_body_excerpt"])
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUV", audit["response_body_excerpt"])

    def test_strict_edge_slot_route_is_decoded_and_validated(self):
        package = build_route_input_package(self.road_map, self.task)
        constraint = build_r4_route_output_constraint(
            package, self.task.pickup_node_id, self.task.dropoff_node_id
        )
        code_by_edge = {
            edge_id: code
            for code, edge_id in constraint.edge_id_by_slot_code.items()
        }
        payload = {
            "status": "completed",
            "pickup_node_id": self.task.pickup_node_id,
            "dropoff_node_id": self.task.dropoff_node_id,
            "edge_slot_sequence": "".join(
                code_by_edge[edge_id] for edge_id in self.valid_route.edge_sequence
            ),
            "step_check_sequence": "1" * len(self.valid_route.edge_sequence),
            "failure_reason_code": "none",
        }
        client = SequenceClient([self._response(payload)])
        client.route_constraint = constraint

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=1,
            route_constraint=constraint,
        )

        self.assertEqual(result["status"], "route_validated")
        self.assertEqual(result["route"]["edge_sequence"], self.valid_route.edge_sequence)
        self.assertEqual(result["api_request_count"], 1)
        replay = result["attempts"][0]["replay"]
        self.assertEqual(
            [pair["edge_id"] for pair in replay["proposed_edge_pairs"]],
            self.valid_route.edge_sequence,
        )
        self.assertEqual(
            replay["model_step_check_sequence"],
            "1" * len(self.valid_route.edge_sequence),
        )
        self.assertEqual(len(replay["step_check_records"]), len(self.valid_route.edge_sequence))
        self.assertTrue(replay["local_step_checks_pass"])
        self.assertTrue(all(record["accepted_step"] for record in replay["step_check_records"]))
        self.assertTrue(
            all(
                record["local_directed_edge_check"] == "PASS"
                and record["local_continuity_check"] == "PASS"
                and record["local_endpoint_order_check"] == "PASS"
                for record in replay["step_check_records"]
            )
        )

    def test_out_of_schema_edge_slot_stops_without_spending_a_retry(self):
        package = build_route_input_package(self.road_map, self.task)
        constraint = build_r4_route_output_constraint(
            package, self.task.pickup_node_id, self.task.dropoff_node_id
        )
        code_by_edge = {
            edge_id: code
            for code, edge_id in constraint.edge_id_by_slot_code.items()
        }
        sequence = "".join(code_by_edge[edge_id] for edge_id in self.valid_route.edge_sequence)
        invalid_payload = {
            "status": "completed",
            "pickup_node_id": self.task.pickup_node_id,
            "dropoff_node_id": self.task.dropoff_node_id,
            "edge_slot_sequence": sequence + "ZZZ",
            "step_check_sequence": "1" * (len(sequence) // 3 + 1),
            "failure_reason_code": "none",
        }
        client = SequenceClient(
            [self._response(invalid_payload), self._valid_response()]
        )
        client.route_constraint = constraint

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=3,
            route_constraint=constraint,
        )

        self.assertEqual(result["status"], "route_constraint_violation")
        self.assertEqual(result["api_request_count"], 1)
        self.assertEqual(len(client.responses), 1)

    def test_failed_result_preserves_model_reason_without_retries(self):
        package = build_route_input_package(self.road_map, self.task)
        constraint = build_r4_route_output_constraint(
            package, self.task.pickup_node_id, self.task.dropoff_node_id
        )
        payload = {
            "status": "failed",
            "pickup_node_id": self.task.pickup_node_id,
            "dropoff_node_id": self.task.dropoff_node_id,
            "edge_slot_sequence": "",
            "step_check_sequence": "",
            "failure_reason_code": "unable_to_construct_route",
        }
        client = SequenceClient([self._response(payload)])
        client.route_constraint = constraint

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=1,
            route_constraint=constraint,
        )

        self.assertEqual(result["status"], "model_returned_failed")
        self.assertEqual(result["attempts"][0]["reason"], "model_unable_to_construct_route")
        self.assertEqual(result["api_request_count"], 1)
        self.assertEqual(len(client.responses), 0)

    def test_step_check_count_mismatch_stops_without_spending_a_retry(self):
        package = build_route_input_package(self.road_map, self.task)
        constraint = build_r4_route_output_constraint(
            package, self.task.pickup_node_id, self.task.dropoff_node_id
        )
        code_by_edge = {
            edge_id: code
            for code, edge_id in constraint.edge_id_by_slot_code.items()
        }
        route_codes = "".join(
            code_by_edge[edge_id] for edge_id in self.valid_route.edge_sequence
        )
        invalid_payload = {
            "status": "completed",
            "pickup_node_id": self.task.pickup_node_id,
            "dropoff_node_id": self.task.dropoff_node_id,
            "edge_slot_sequence": route_codes,
            "step_check_sequence": "1" * (len(self.valid_route.edge_sequence) - 1),
            "failure_reason_code": "none",
        }
        client = SequenceClient([self._response(invalid_payload), self._valid_response()])
        client.route_constraint = constraint

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=3,
            route_constraint=constraint,
        )

        self.assertEqual(result["status"], "route_constraint_violation")
        self.assertEqual(result["api_request_count"], 1)
        self.assertEqual(len(client.responses), 1)

    def test_locally_disconnected_route_is_rejected_and_step_is_marked(self):
        package = build_route_input_package(self.road_map, self.task)
        constraint = build_r4_route_output_constraint(
            package, self.task.pickup_node_id, self.task.dropoff_node_id
        )
        code_by_edge = {
            edge_id: code
            for code, edge_id in constraint.edge_id_by_slot_code.items()
        }
        first_edge_id = self.valid_route.edge_sequence[0]
        first_edge = next(edge for edge in self.road_map.edges if edge.edge_id == first_edge_id)
        disconnected_edge = next(
            edge
            for edge in self.road_map.edges
            if edge.from_node != first_edge.to_node
            and edge.edge_id != first_edge_id
        )
        route_codes = code_by_edge[first_edge_id] + code_by_edge[disconnected_edge.edge_id]
        payload = {
            "status": "completed",
            "pickup_node_id": self.task.pickup_node_id,
            "dropoff_node_id": self.task.dropoff_node_id,
            "edge_slot_sequence": route_codes,
            "step_check_sequence": "11",
            "failure_reason_code": "none",
        }
        client = SequenceClient([self._response(payload)])
        client.route_constraint = constraint

        result = run_r4_route_search(
            client,
            self.base_messages,
            self.road_map,
            self.task,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            max_attempts=1,
            route_constraint=constraint,
        )

        self.assertEqual(result["status"], "planning_attempts_exhausted")
        self.assertEqual(result["attempts"][0]["status"], "route_proposal_invalid")
        checks = result["attempts"][0]["replay"]["step_check_records"]
        self.assertTrue(checks[0]["accepted_step"])
        self.assertEqual(checks[1]["local_directed_edge_check"], "PASS")
        self.assertEqual(checks[1]["local_continuity_check"], "FAIL")
        self.assertFalse(checks[1]["accepted_step"])

    def test_constraint_schema_mismatch_is_rejected_before_any_request(self):
        package = build_route_input_package(self.road_map, self.task)
        constraint = build_r4_route_output_constraint(
            package, self.task.pickup_node_id, self.task.dropoff_node_id
        )
        client = SequenceClient([self._valid_response()])

        with self.assertRaisesRegex(RuntimeError, "r4_route_constraint_client_schema_mismatch"):
            run_r4_route_search(
                client,
                self.base_messages,
                self.road_map,
                self.task,
                self.task.pickup_node_id,
                self.task.dropoff_node_id,
                max_attempts=1,
                route_constraint=constraint,
            )

        self.assertEqual(client.calls, [])

    def test_real_deepseek_client_cannot_run_r4_without_a_route_constraint(self):
        from route_experiment.llm_adapter import DeepSeekClient

        transported = []
        client = DeepSeekClient(
            api_key="offline-test-key",
            retries=0,
            transport=lambda *args: transported.append(args),
        )

        with self.assertRaisesRegex(RuntimeError, "r4_route_constraint_required"):
            run_r4_route_search(
                client,
                self.base_messages,
                self.road_map,
                self.task,
                self.task.pickup_node_id,
                self.task.dropoff_node_id,
                max_attempts=1,
            )

        self.assertEqual(transported, [])

    def test_non_json_user_input_is_rejected_before_any_api_call(self):
        client = SequenceClient([self._invalid_response()])
        messages = [
            {"role": "system", "content": "search only listed roads"},
            {"role": "user", "content": "not-json"},
        ]

        with self.assertRaisesRegex(ValueError, "r4_route_input_user_message_must_be_json"):
            run_r4_route_search(
                client,
                messages,
                self.road_map,
                self.task,
                self.task.pickup_node_id,
                self.task.dropoff_node_id,
            )

        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
