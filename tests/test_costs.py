import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from route_experiment.costs import (
    enrich_route_method_result,
    evaluate_route_candidate,
    calculate_resource_cost,
)
from route_experiment.evaluation import evaluate_route, price_costs
from route_experiment.models import (
    Edge,
    EvaluationConfig,
    Node,
    PriceConfig,
    RoadMap,
    RouteCandidate,
    RouteMethodResult,
)


class RouteCostTests(unittest.TestCase):
    def setUp(self):
        self.road_map = RoadMap(
            map_id="cost-test-map",
            version="grid-v1-test",
            size=3,
            boundary={"min_x": 0, "max_x": 2, "min_y": 0, "max_y": 0},
            nodes=[
                Node(node_id="n0", x=0, y=0),
                Node(node_id="n1", x=1, y=0),
                Node(node_id="n2", x=2, y=0),
            ],
            edges=[
                Edge(
                    edge_id="e01",
                    from_node="n0",
                    to_node="n1",
                    length_m=1000.0,
                    base_speed_mps=10.0,
                    congestion_level=1,
                    signal_delay_sec=20.0,
                ),
                Edge(
                    edge_id="e12",
                    from_node="n1",
                    to_node="n2",
                    length_m=1000.0,
                    base_speed_mps=10.0,
                    congestion_level=0,
                    signal_delay_sec=0.0,
                ),
            ],
        )
        self.route = RouteCandidate(
            route_id="r1",
            node_sequence=["n0", "n1", "n2"],
            edge_sequence=["e01", "e12"],
            route_reachability=True,
            validation_status="valid",
        )

    def test_evaluate_route_candidate_uses_shared_time_and_energy_model(self):
        evaluation = evaluate_route_candidate(self.road_map, self.route)

        self.assertAlmostEqual(evaluation.base_travel_time_sec, 200.0)
        self.assertAlmostEqual(evaluation.congestion_delay_sec, 25.0)
        self.assertAlmostEqual(evaluation.signal_delay_sec, 20.0)
        self.assertAlmostEqual(evaluation.total_travel_time_sec, 245.0)
        self.assertAlmostEqual(evaluation.route_distance_m, 2000.0)
        self.assertAlmostEqual(evaluation.energy_moving_kwh, 0.36)
        self.assertAlmostEqual(evaluation.energy_idle_signal_kwh, 0.0006)
        self.assertAlmostEqual(evaluation.energy_congestion_kwh, 0.01)
        self.assertAlmostEqual(evaluation.energy_total_kwh, 0.3706)
        self.assertTrue(evaluation.simulation_estimate)
        self.assertEqual(evaluation.energy_model_version, "energy_v1")

    def test_calculate_resource_cost_keeps_units_separate_and_sums_money(self):
        method_result = RouteMethodResult(
            task_id="task-1",
            method_name="test",
            metadata={"compute_time_ms": 10.0},
        )
        evaluation = evaluate_route_candidate(self.road_map, self.route)
        resources, money = calculate_resource_cost(
            method_result,
            evaluation,
            price_config=PriceConfig(
                api_price_per_token=0.01,
                compute_price_per_ms=0.2,
                electricity_price_per_kwh=0.8,
            ),
            input_tokens=100,
            output_tokens=50,
            llm_calls=1,
            llm_latency_ms=12.5,
        )

        self.assertEqual(resources.input_tokens, 100)
        self.assertEqual(resources.output_tokens, 50)
        self.assertEqual(resources.llm_calls, 1)
        self.assertAlmostEqual(resources.llm_latency_ms, 12.5)
        self.assertAlmostEqual(resources.compute_time_ms, 10.0)
        self.assertAlmostEqual(resources.energy_kwh, 0.3706)
        self.assertAlmostEqual(money.api_cost_amount, 1.5)
        self.assertAlmostEqual(money.compute_cost_amount, 2.0)
        self.assertAlmostEqual(money.electricity_cost_amount, 0.29648)
        self.assertAlmostEqual(money.total_monetary_cost, 3.79648)
        self.assertEqual(money.currency, "CNY")

    def test_plan_evaluation_interfaces_match_shared_cost_model(self):
        evaluation = evaluate_route(self.road_map, self.route, EvaluationConfig())
        resources = calculate_resource_cost(
            RouteMethodResult(task_id="task-1", method_name="test"), evaluation
        )[0]
        money = price_costs(
            resources,
            PriceConfig(
                api_price_per_token=0.01,
                compute_price_per_ms=0.2,
                electricity_price_per_kwh=0.8,
            ),
        )

        self.assertAlmostEqual(evaluation.energy_total_kwh, 0.3706)
        self.assertAlmostEqual(money.electricity_cost_amount, 0.29648)

    def test_enrichment_does_not_invent_cost_for_failed_route(self):
        result = RouteMethodResult(
            task_id="task-failed",
            method_name="local_dijkstra",
            status="failed",
            failure_reason="unreachable",
        )

        enriched = enrich_route_method_result(self.road_map, result)

        self.assertIsNone(enriched.evaluation)
        self.assertEqual(enriched.resources.energy_kwh, 0.0)
        self.assertEqual(enriched.monetary_cost.total_monetary_cost, 0.0)
        self.assertEqual(enriched.metadata["cost_status"], "not_evaluated")

    def test_enrichment_preserves_route_and_adds_evaluation_and_cost(self):
        result = RouteMethodResult(
            task_id="task-1",
            method_name="local_dijkstra",
            routes=[self.route],
            best_route_id="r1",
            status="completed",
            metadata={"compute_time_ms": 10.0},
        )

        enriched = enrich_route_method_result(
            self.road_map,
            result,
            price_config=PriceConfig(electricity_price_per_kwh=1.0),
        )

        self.assertIs(enriched.routes[0], self.route)
        self.assertAlmostEqual(enriched.evaluation.energy_total_kwh, 0.3706)
        self.assertAlmostEqual(enriched.resources.energy_kwh, 0.3706)
        self.assertAlmostEqual(enriched.monetary_cost.electricity_cost_amount, 0.3706)
        self.assertEqual(enriched.metadata["cost_status"], "evaluated")

    def test_cost_cli_enriches_json_and_writes_summary(self):
        route_payload = {
            "map_id": self.road_map.map_id,
            "map_version": self.road_map.version,
            "methods": ["local_dijkstra"],
            "input_type": "order_batch",
            "results": [
                {
                    "task_id": "task-1",
                    "task_type": "exact_node",
                    "methods": [
                        RouteMethodResult(
                            task_id="task-1",
                            method_name="local_dijkstra",
                            routes=[self.route],
                            best_route_id="r1",
                            status="completed",
                            valid_route_count=1,
                            metadata={"compute_time_ms": 10.0},
                        ).to_dict()
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            map_path = temp_path / "map.json"
            routes_path = temp_path / "routes.json"
            output_path = temp_path / "costs.json"
            map_path.write_text(json.dumps(self.road_map.to_dict()), encoding="utf-8")
            routes_path.write_text(json.dumps(route_payload), encoding="utf-8")

            subprocess.run(
                [
                    sys.executable,
                    "scripts/calculate_route_costs.py",
                    "--map-json",
                    str(map_path),
                    "--routes-json",
                    str(routes_path),
                    "--output-json",
                    str(output_path),
                    "--electricity-price-per-kwh",
                    "1",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            output = json.loads(output_path.read_text(encoding="utf-8"))
            method = output["results"][0]["methods"][0]
            self.assertEqual(method["metadata"]["cost_status"], "evaluated")
            self.assertAlmostEqual(method["evaluation"]["energy_total_kwh"], 0.3706)
            self.assertAlmostEqual(
                method["monetary_cost"]["electricity_cost_amount"], 0.3706
            )
            self.assertIn("summary", output)
            self.assertEqual(output["summary"]["local_dijkstra"]["evaluated_routes"], 1)
            self.assertAlmostEqual(
                output["summary"]["local_dijkstra"]["total_compute_time_ms"], 10.0
            )

    def test_cost_cli_loads_named_price_profile_and_records_price_provenance(self):
        route_payload = {
            "map_id": self.road_map.map_id,
            "map_version": self.road_map.version,
            "methods": ["local_dijkstra"],
            "input_type": "order_batch",
            "results": [
                {
                    "task_id": "task-1",
                    "task_type": "exact_node",
                    "methods": [
                        RouteMethodResult(
                            task_id="task-1",
                            method_name="local_dijkstra",
                            routes=[self.route],
                            best_route_id="r1",
                            status="completed",
                            valid_route_count=1,
                            metadata={"compute_time_ms": 10.0},
                        ).to_dict()
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            map_path = temp_path / "map.json"
            routes_path = temp_path / "routes.json"
            price_path = temp_path / "price.json"
            output_path = temp_path / "costs.json"
            map_path.write_text(json.dumps(self.road_map.to_dict()), encoding="utf-8")
            routes_path.write_text(json.dumps(route_payload), encoding="utf-8")
            price_path.write_text(
                json.dumps(
                    {
                        "api_price_per_token": 0.01,
                        "compute_price_per_ms": 0.2,
                        "electricity_price_per_kwh": 0.8,
                        "currency": "CNY",
                        "pricing_version": "test_price_v1",
                        "billing_period": "smoke",
                        "pricing_status": "illustrative_smoke_only",
                        "price_source": "unit_test",
                    }
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    "scripts/calculate_route_costs.py",
                    "--map-json",
                    str(map_path),
                    "--routes-json",
                    str(routes_path),
                    "--output-json",
                    str(output_path),
                    "--price-config-json",
                    str(price_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            output = json.loads(output_path.read_text(encoding="utf-8"))
            method = output["results"][0]["methods"][0]
            self.assertAlmostEqual(method["monetary_cost"]["total_monetary_cost"], 2.29648)
            self.assertEqual(method["metadata"]["pricing_status"], "illustrative_smoke_only")
            self.assertEqual(method["metadata"]["price_source"], "unit_test")
            self.assertEqual(output["cost_config"]["pricing"]["pricing_version"], "test_price_v1")


if __name__ == "__main__":
    unittest.main()
