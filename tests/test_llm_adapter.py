import json
import io
import urllib.error
import unittest
from dataclasses import replace

from route_experiment.llm_adapter import (
    DeepSeekClient,
    LLMRoutePlanner,
    build_route_messages,
    build_r4_route_search_messages,
    parse_route_output,
    replay_move_sequence,
    validate_llm_route,
)
from route_experiment.mapgen import generate_map
from route_experiment.models import MapConfig, RouteCandidate, VehicleOrderTask
from route_experiment.routing import run_traditional_route


class LLMAdapterTests(unittest.TestCase):
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
            task_id="task-llm-1",
            vehicle_id="vehicle-1",
            order_id="order-1",
            start_node_id=start.node_id,
            pickup_node_id=pickup.node_id,
            dropoff_node_id=dropoff.node_id,
            task_type="exact_node",
        )

    def test_parse_route_output_accepts_json_and_rejects_missing_edges(self):
        payload = {
            "status": "completed",
            "pickup_node_id": "node_1_1",
            "dropoff_node_id": "node_2_2",
            "node_sequence": ["node_0_0", "node_1_0"],
            "edge_sequence": ["edge_node_0_0_node_1_0"],
        }
        parsed = parse_route_output(json.dumps(payload))
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["edge_sequence"], ["edge_node_0_0_node_1_0"])
        with self.assertRaises(ValueError):
            parse_route_output('{"status":"completed"}')

    def test_parse_route_output_expands_compact_direction_runs(self):
        payload = {
            "status": "completed",
            "pickup_node_id": "node_1_1",
            "dropoff_node_id": "node_2_2",
            "move_runs": [
                {"direction": "E", "steps": 3},
                {"direction": "N", "steps": 2},
            ],
        }

        parsed = parse_route_output(json.dumps(payload))

        self.assertEqual(parsed["move_sequence"], "EEENN")

    def test_parse_route_output_rejects_invalid_direction_run(self):
        payload = {
            "status": "completed",
            "pickup_node_id": "node_1_1",
            "dropoff_node_id": "node_2_2",
            "move_runs": [{"direction": "Q", "steps": 2}],
        }

        with self.assertRaises(ValueError):
            parse_route_output(json.dumps(payload))

    def test_replay_move_sequence_preserves_proposal_and_marks_one_way_violation(self):
        node_by_id = {node.node_id: node for node in self.road_map.nodes}
        edge_by_id = {edge.edge_id: edge for edge in self.road_map.edges}
        moves = { (1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S" }
        invalid_move = None
        start_node_id = None
        for edge in self.road_map.edges:
            reverse_id = "edge_{0}_{1}".format(edge.to_node, edge.from_node)
            if not edge.one_way or reverse_id in edge_by_id:
                continue
            source = node_by_id[edge.to_node]
            target = node_by_id[edge.from_node]
            invalid_move = moves[(target.x - source.x, target.y - source.y)]
            start_node_id = source.node_id
            break
        self.assertIsNotNone(invalid_move)
        blocked_from_node = node_by_id[next(
            edge.to_node for edge in self.road_map.edges
            if edge.one_way
            and "edge_{0}_{1}".format(edge.to_node, edge.from_node) not in edge_by_id
        )]
        blocked_to_node = next(
            node_by_id[edge.from_node] for edge in self.road_map.edges
            if edge.one_way and edge.to_node == blocked_from_node.node_id
            and "edge_{0}_{1}".format(edge.to_node, edge.from_node) not in edge_by_id
        )
        continuation = next(
            moves[(node.x - blocked_to_node.x, node.y - blocked_to_node.y)]
            for edge in self.road_map.edges
            for node in [node_by_id[edge.to_node]]
            if edge.from_node == blocked_to_node.node_id
            and abs(node.x - blocked_to_node.x) + abs(node.y - blocked_to_node.y) == 1
        )

        replay = replay_move_sequence(
            self.road_map, start_node_id, invalid_move + continuation
        )

        self.assertEqual(replay["attempted_move_sequence"], invalid_move + continuation)
        self.assertEqual(replay["attempted_route_node_sequence"][0], start_node_id)
        self.assertEqual(len(replay["attempted_route_node_sequence"]), 3)
        self.assertEqual(len(replay["validated_prefix_node_sequence"]), 1)
        self.assertEqual(len(replay["move_violations"]), 1)
        violation = replay["move_violations"][0]
        self.assertEqual(violation["step"], 1)
        self.assertEqual(violation["kind"], "one_way_direction")
        self.assertEqual(violation["from_node_id"], start_node_id)
        self.assertEqual(violation["reverse_edge_id"], next(
            edge.edge_id for edge in self.road_map.edges
            if edge.from_node == violation["to_node_id"]
            and edge.to_node == violation["from_node_id"]
        ))
        self.assertTrue(violation["reverse_edge_one_way"])

    def test_planner_keeps_failed_move_trace_and_violation_details(self):
        node_by_id = {node.node_id: node for node in self.road_map.nodes}
        edge_by_id = {edge.edge_id: edge for edge in self.road_map.edges}
        moves = { (1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S" }
        invalid_move = None
        task = None
        for edge in self.road_map.edges:
            reverse_id = "edge_{0}_{1}".format(edge.to_node, edge.from_node)
            if not edge.one_way or reverse_id in edge_by_id:
                continue
            start = node_by_id[edge.to_node]
            target = node_by_id[edge.from_node]
            invalid_move = moves[(target.x - start.x, target.y - start.y)]
            task = replace(self.task, start_node_id=start.node_id)
            if task.start_node_id != task.pickup_node_id and task.start_node_id != task.dropoff_node_id:
                break
        self.assertIsNotNone(invalid_move)
        self.assertIsNotNone(task)

        class FakeClient:
            def chat(self, messages):
                return type(
                    "Response",
                    (),
                    {
                        "content": json.dumps({
                            "status": "completed",
                            "pickup_node_id": task.pickup_node_id,
                            "dropoff_node_id": task.dropoff_node_id,
                            "move_sequence": invalid_move,
                        }),
                        "model": "fake",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "latency_ms": 1.0,
                        "retries": 0,
                        "request_id": None,
                    },
                )()

        result = LLMRoutePlanner(FakeClient()).plan(self.road_map, task)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_reason, "llm_move_sequence_invalid")
        trace = result.metadata["search_trace"]
        self.assertEqual(len(trace["attempted_route_node_sequence"]), 2)
        self.assertEqual(trace["attempted_move_sequence"], invalid_move)
        self.assertEqual(len(trace["move_violations"]), 1)
        self.assertEqual(trace["move_violations"][0]["step"], 1)

    def test_prompt_requests_run_length_encoded_path_with_explicit_legal_move_checks(self):
        messages = build_route_messages(self.road_map, self.task)
        serialized = json.dumps(messages, ensure_ascii=False)

        self.assertIn("move_runs", serialized)
        self.assertIn("steps", serialized)
        self.assertIn("只做路径搜索", serialized)
        self.assertIn("outgoing_mask_by_y", serialized)
        self.assertIn("每一步", serialized)
        self.assertIn("禁止越界", serialized)

    def test_prompt_uses_exact_compact_grid_encoding(self):
        from route_experiment.routing import build_route_input_package

        package = build_route_input_package(self.road_map, self.task)
        payload = json.loads(build_route_messages(self.road_map, self.task, package)[1]["content"])
        map_payload = payload["map"]

        self.assertEqual(map_payload["encoding"], "grid-directed-mask-v2")
        self.assertNotIn("edges", map_payload)
        self.assertNotIn("roadside_features", map_payload)
        self.assertNotIn("roadside_feature_fields", map_payload)
        min_x, max_x = map_payload["x_range"]
        min_y, max_y = map_payload["y_range"]
        node_by_position = {
            (node.x, node.y): node for node in package.nodes
        }
        for y, flags in zip(range(min_y, max_y + 1), map_payload["node_flags_by_y"]):
            self.assertEqual(len(flags), max_x - min_x + 1)
            for x, code in zip(range(min_x, max_x + 1), flags):
                node = node_by_position[(x, y)]
                expected = int(node.legal_pickup) + 2 * int(node.legal_dropoff)
                self.assertEqual(int(code), expected)
        self.assertEqual(
            map_payload["landmarks"],
            [
                [node.x, node.y, node.landmark_name, list(node.landmark_aliases)]
                for node in package.nodes
                if node.landmark_name
            ],
        )
        self.assertEqual(map_payload["blocked_edge_ids"], package.blocked_edge_ids)

        edge_by_pair = {(edge.from_node, edge.to_node): edge for edge in package.edges}
        speed_codes = {0: 8.0, 1: 10.0, 2: 12.0}
        signal_codes = {0: 0.0, 1: 10.0, 2: 15.0, 3: 20.0}

        direction_bits = {"E": 1, "W": 2, "N": 4, "S": 8}

        def check_pair(encoded, source, positive_target, direction_mask):
            sx, sy = source
            tx, ty = positive_target
            node_id = lambda x, y: "node_{0}_{1}".format(x, y)
            forward = edge_by_pair.get((node_id(sx, sy), node_id(tx, ty)))
            reverse = edge_by_pair.get((node_id(tx, ty), node_id(sx, sy)))
            speed_code, congestion_code, signal_code = map(int, encoded)
            self.assertEqual(direction_mask, int(forward is not None) + 2 * int(reverse is not None))
            if forward is not None:
                self.assertEqual(forward.base_speed_mps, speed_codes[speed_code])
                self.assertEqual(forward.congestion_level, congestion_code)
                self.assertEqual(forward.signal_delay_sec, signal_codes[signal_code])
            if reverse is not None:
                self.assertEqual(reverse.base_speed_mps, speed_codes[speed_code])
                self.assertEqual(reverse.congestion_level, congestion_code)
                self.assertEqual(reverse.signal_delay_sec, signal_codes[signal_code])

        outgoing_rows = map_payload["outgoing_mask_by_y"]
        self.assertEqual(len(outgoing_rows), max_y - min_y + 1)
        for row_index, y in enumerate(range(min_y, max_y + 1)):
            row = outgoing_rows[row_index]
            self.assertEqual(len(row), max_x - min_x + 1)
            for col_index, x in enumerate(range(min_x, max_x + 1)):
                node_id = "node_{0}_{1}".format(x, y)
                mask = int(row[col_index], 16)
                expected_mask = 0
                for neighbor, direction in (
                    ((x + 1, y), "E"), ((x - 1, y), "W"),
                    ((x, y + 1), "N"), ((x, y - 1), "S"),
                ):
                    if (node_id, "node_{0}_{1}".format(*neighbor)) in edge_by_pair:
                        expected_mask |= direction_bits[direction]
                self.assertEqual(mask, expected_mask)

        for row_index, y in enumerate(range(min_y, max_y + 1)):
            row = map_payload["horizontal_cost_codes_by_y"][row_index]
            self.assertEqual(len(row), (max_x - min_x) * 3)
            for i, x in enumerate(range(min_x, max_x)):
                source_node_id = "node_{0}_{1}".format(x, y)
                target_node_id = "node_{0}_{1}".format(x + 1, y)
                direction_mask = int(map_payload["outgoing_mask_by_y"][row_index][i], 16)
                direction_mask = (1 if direction_mask & direction_bits["E"] else 0) + (2 if int(map_payload["outgoing_mask_by_y"][row_index][i + 1], 16) & direction_bits["W"] else 0)
                check_pair(row[i * 3 : i * 3 + 3], (x, y), (x + 1, y), direction_mask)
        for column_index, x in enumerate(range(min_x, max_x + 1)):
            column = map_payload["vertical_cost_codes_by_x"][column_index]
            self.assertEqual(len(column), (max_y - min_y) * 3)
            for i, y in enumerate(range(min_y, max_y)):
                mask_from = int(map_payload["outgoing_mask_by_y"][i][column_index], 16)
                mask_to = int(map_payload["outgoing_mask_by_y"][i + 1][column_index], 16)
                direction_mask = (1 if mask_from & direction_bits["N"] else 0) + (2 if mask_to & direction_bits["S"] else 0)
                check_pair(column[i * 3 : i * 3 + 3], (x, y), (x, y + 1), direction_mask)

    def test_r4_route_prompt_uses_explicit_directed_adjacency(self):
        from route_experiment.routing import build_route_input_package, edge_travel_time_sec

        package = build_route_input_package(self.road_map, self.task)
        messages = build_r4_route_search_messages(
            package,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
        )
        context = json.loads(messages[1]["content"])
        self.assertEqual(context["map"]["encoding"], "directional-cost-grid-v1")
        self.assertIn("每个节点连续 4 个字符", messages[0]["content"])
        self.assertIn("未出现的方向禁止通行", messages[0]["content"])
        self.assertIn("首次抵达下车点后立即停止", messages[0]["content"])
        self.assertIn("根据相邻坐标推断反向道路", messages[0]["content"])
        self.assertIn("r4_validation_feedback", messages[0]["content"])
        self.assertIn("minimize_sum_travel_time_sec_then_sum_distance_m", messages[1]["content"])
        self.assertEqual(context["route_query"]["max_route_edges"], len(package.nodes) - 1)
        self.assertEqual(
            context["output_contract"]["edge_slot_encoding"]["examples"],
            {"35": "00Z", "36": "010", "255": "073"},
        )
        self.assertIn("不得重复访问节点", messages[0]["content"])
        self.assertIn("不要枚举或罗列地图中的其他合法边", messages[0]["content"])
        self.assertIn("不是十六进制", messages[0]["content"])
        self.assertIn("edge_slot_sequence", context["output_contract"]["completed"])
        self.assertIn("step_check_sequence", context["output_contract"]["completed"])
        self.assertIn("每一步", context["route_query"]["stepwise_self_check"])
        self.assertIn("map_reading_guide", context)
        self.assertNotIn("move_runs", context["output_contract"]["completed"])
        self.assertIn("3 位 base36", messages[0]["content"])
        self.assertIn("仅允许图中存在的有向边", messages[0]["content"])
        self.assertIn("逐步自检", messages[0]["content"])
        self.assertIn("自检失败只否决当前候选路线", messages[0]["content"])
        self.assertIn("不能把一次候选路线自检失败当作整项任务失败", messages[0]["content"])
        self.assertIn("不能证明全局最优不是无路可走", messages[0]["content"])
        self.assertEqual(
            context["output_contract"]["failed"]["failure_reason_code"],
            ["no_directed_path", "unable_to_construct_route"],
        )
        self.assertNotIn("outgoing_mask_by_y", messages[1]["content"])
        self.assertNotIn("roadside_features", messages[1]["content"])
        self.assertNotIn("true_pickup_node_id", messages[1]["content"])

        node_min_x = min(node.x for node in package.nodes)
        node_max_x = max(node.x for node in package.nodes)
        node_min_y = min(node.y for node in package.nodes)
        node_max_y = max(node.y for node in package.nodes)
        self.assertEqual(
            context["map"]["boundary_inclusive"],
            {"min_x": node_min_x, "max_x": node_max_x, "min_y": node_min_y, "max_y": node_max_y},
        )

        encoded_edges = {}
        node_by_id = {node.node_id: node for node in package.nodes}
        min_x, min_y = node_min_x, node_min_y
        max_x, max_y = node_max_x, node_max_y
        direction_by_slot = "EWNS"
        code_rows = context["map"]["directional_cost_codes_by_y"]
        self.assertEqual(len(code_rows), max_y - min_y + 1)
        for row_index, y in enumerate(range(min_y, max_y + 1)):
            row = code_rows[row_index]
            self.assertEqual(len(row), (max_x - min_x + 1) * 4)
            for x in range(min_x, max_x + 1):
                source_id = "node_{0}_{1}".format(x, y)
                offset = (x - min_x) * 4
                for slot_index, direction in enumerate(direction_by_slot):
                    code = row[offset + slot_index]
                    if code == ".":
                        continue
                    dx, dy = {
                        "E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)
                    }[direction]
                    target_id = "node_{0}_{1}".format(x + dx, y + dy)
                    key = (source_id, target_id)
                    self.assertNotIn(key, encoded_edges)
                    cost_data = context["map"]["edge_cost_legend"][code]
                    encoded_edges[key] = (direction, cost_data[0], cost_data[1])
        self.assertEqual(set(encoded_edges), {(edge.from_node, edge.to_node) for edge in package.edges})
        edge_by_pair = {(edge.from_node, edge.to_node): edge for edge in package.edges}
        for pair, (direction, travel_time_sec, distance_m) in encoded_edges.items():
            edge = edge_by_pair[pair]
            self.assertEqual(direction, {
                (1, 0): "E",
                (-1, 0): "W",
                (0, 1): "N",
                (0, -1): "S",
            }[(node_by_id[edge.to_node].x - node_by_id[edge.from_node].x,
               node_by_id[edge.to_node].y - node_by_id[edge.from_node].y)])
            self.assertAlmostEqual(distance_m, edge.length_m)
            self.assertAlmostEqual(
                travel_time_sec, edge_travel_time_sec(edge), places=6
            )

    def test_preverified_r4_route_prompt_does_not_offer_failed_exit(self):
        from route_experiment.routing import build_route_input_package

        package = build_route_input_package(self.road_map, self.task)
        messages = build_r4_route_search_messages(
            package,
            self.task.start_node_id,
            self.task.pickup_node_id,
            self.task.dropoff_node_id,
            require_completed_route=True,
        )
        user_input = json.loads(messages[1]["content"])

        self.assertEqual(
            user_input["output_contract"].keys(),
            {"completed", "edge_slot_encoding"},
        )
        self.assertNotIn("failed", messages[0]["content"])
        self.assertNotIn("unable_to_construct_route", messages[0]["content"])

    def test_build_messages_do_not_expose_hidden_truth_fields(self):
        messages = build_route_messages(self.road_map, self.task)
        serialized = json.dumps(messages, ensure_ascii=False)
        self.assertIn("move_runs", serialized)
        self.assertNotIn("true_pickup_node_id", serialized)
        self.assertNotIn("true_dropoff_node_id", serialized)
        self.assertNotIn("roadside_features", serialized)
        self.assertNotIn("roadside_feature_fields", serialized)
        prompt = json.loads(messages[1]["content"])
        self.assertEqual(prompt["map"]["input_mode"], "graph_only")

    def test_client_records_model_usage_and_latency_without_logging_key(self):
        seen = {}

        def transport(url, headers, body, timeout):
            seen["url"] = url
            seen["headers"] = headers
            seen["body"] = body
            return {
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": '{"status":"failed","reason":"test"}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            }

        client = DeepSeekClient(api_key="secret-test-key", transport=transport)
        response = client.chat(build_route_messages(self.road_map, self.task))
        self.assertEqual(response.model, "deepseek-v4-flash")
        self.assertEqual(response.input_tokens, 12)
        self.assertEqual(response.output_tokens, 4)
        self.assertNotIn("secret-test-key", json.dumps(seen["body"]))
        self.assertEqual(seen["headers"]["Authorization"], "Bearer secret-test-key")

    def test_client_preserves_safe_http_error_details_without_credentials(self):
        error_body = json.dumps(
            {
                "error": {
                    "message": "Input exceeds maximum context; key sk-sensitive-value must not appear",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                    "param": "messages",
                }
            }
        ).encode("utf-8")

        def transport(url, headers, body, timeout):
            raise urllib.error.HTTPError(
                url,
                400,
                "Bad Request",
                {},
                io.BytesIO(error_body),
            )

        client = DeepSeekClient(api_key="sk-sensitive-value", transport=transport, retries=0)
        with self.assertRaises(RuntimeError) as raised:
            client.chat(build_route_messages(self.road_map, self.task))

        message = str(raised.exception)
        self.assertIn("deepseek_http_status:400", message)
        self.assertIn("context_length_exceeded", message)
        self.assertIn("maximum context", message)
        self.assertNotIn("sk-sensitive-value", message)

    def test_validate_route_recomputes_direction_and_endpoint_status(self):
        baseline = run_traditional_route(self.road_map, self.task, "full_map_dijkstra")
        self.assertEqual(baseline.status, "completed")
        route = baseline.routes[0]
        validation = validate_llm_route(
            self.road_map,
            self.task,
            route,
            selected_pickup_node_id=self.task.pickup_node_id,
            selected_dropoff_node_id=self.task.dropoff_node_id,
        )
        self.assertTrue(validation.valid)
        self.assertTrue(validation.direction_valid)
        invalid = RouteCandidate(
            route_id="bad",
            node_sequence=list(reversed(route.node_sequence)),
            edge_sequence=list(reversed(route.edge_sequence)),
        )
        invalid_result = validate_llm_route(
            self.road_map,
            self.task,
            invalid,
            selected_pickup_node_id=self.task.pickup_node_id,
            selected_dropoff_node_id=self.task.dropoff_node_id,
        )
        self.assertFalse(invalid_result.valid)
        self.assertIn("route_edge_node_discontinuity", invalid_result.errors)

    def test_validate_route_rejects_moves_after_dropoff(self):
        outgoing_by_node = {}
        for edge in self.road_map.edges:
            outgoing_by_node.setdefault(edge.from_node, []).append(edge)
        dropoff = next(
            node
            for node in self.road_map.nodes
            if node.legal_dropoff and outgoing_by_node.get(node.node_id)
        )
        pickup = next(
            node
            for node in self.road_map.nodes
            if node.legal_pickup and node.node_id != dropoff.node_id
        )
        start = next(
            node
            for node in self.road_map.nodes
            if node.node_id not in {pickup.node_id, dropoff.node_id}
        )
        task = replace(
            self.task,
            start_node_id=start.node_id,
            pickup_node_id=pickup.node_id,
            dropoff_node_id=dropoff.node_id,
        )
        baseline = run_traditional_route(self.road_map, task, "full_map_dijkstra")
        self.assertEqual(baseline.status, "completed")
        route = baseline.routes[0]
        extra_edge = outgoing_by_node[dropoff.node_id][0]
        extended = RouteCandidate(
            route_id="route-after-dropoff",
            node_sequence=route.node_sequence + [extra_edge.to_node],
            edge_sequence=route.edge_sequence + [extra_edge.edge_id],
        )

        result = validate_llm_route(
            self.road_map,
            task,
            extended,
            selected_pickup_node_id=pickup.node_id,
            selected_dropoff_node_id=dropoff.node_id,
        )

        self.assertFalse(result.valid)
        self.assertIn("route_does_not_terminate_at_dropoff", result.errors)

    def test_planner_can_use_injected_client(self):
        baseline = run_traditional_route(self.road_map, self.task, "full_map_dijkstra")
        route = baseline.routes[0]
        task = self.task

        class FakeClient:
            def chat(self, messages):
                return type(
                    "Response",
                    (),
                    {
                        "content": json.dumps(
                            {
                                "status": "completed",
                                "pickup_node_id": task.pickup_node_id,
                                "dropoff_node_id": task.dropoff_node_id,
                                "node_sequence": route.node_sequence,
                                "edge_sequence": route.edge_sequence,
                            }
                        ),
                        "model": "fake",
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "latency_ms": 1.0,
                        "retries": 0,
                        "request_id": None,
                    },
                )()

        result = LLMRoutePlanner(FakeClient()).plan(self.road_map, self.task)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.resources.llm_calls, 1)
        self.assertTrue(result.validation.valid)
        self.assertEqual(result.metadata["input_map_encoding"], "grid-directed-mask-v2")
        self.assertGreater(result.metadata["prompt_char_count"], 0)


if __name__ == "__main__":
    unittest.main()
