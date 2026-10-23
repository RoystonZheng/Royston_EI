import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_cli_support import CODEX_DISABLED_TOOL_FEATURES
from dispatch_experiment.llm import CodexCliDirectAssignment
from dispatch_experiment.models import Candidate, Order, RouteLeg, Vehicle


class DispatchCodexCliTests(unittest.TestCase):
    def test_unknown_cli_item_type_is_treated_as_tool_usage(self):
        audit = CodexCliDirectAssignment._audit_events(
            [
                {
                    "type": "item.completed",
                    "item": {"type": "browser_action"},
                }
            ]
        )

        self.assertTrue(audit["tool_usage_detected"])
        self.assertEqual(audit["tool_item_types"], ["browser_action"])

    def test_message_and_reasoning_items_are_not_misclassified_as_tools(self):
        audit = CodexCliDirectAssignment._audit_events(
            [
                {"type": "item.completed", "item": {"type": "agent_message"}},
                {"type": "item.completed", "item": {"type": "reasoning"}},
                {"type": "item.completed", "item": {"type": "plan"}},
            ]
        )

        self.assertFalse(audit["tool_usage_detected"])

    def test_codex_assignment_resumes_existing_trace_and_records_rate_estimate(self):
        with tempfile.TemporaryDirectory() as temporary:
            trace_dir = Path(temporary) / "traces"
            cli_calls = []

            def fake_run(command, **kwargs):
                if command == ["codex", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="codex-cli test", stderr="")
                cli_calls.append(command)
                final_path = Path(command[command.index("--output-last-message") + 1])
                final_path.write_text(
                    json.dumps({"selected_candidate_ids": ["candidate-1"]}),
                    encoding="utf-8",
                )
                events = [
                    {"type": "turn.started"},
                    {"type": "item.completed", "item": {"type": "agent_message"}},
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 200,
                            "output_tokens": 100,
                        },
                    },
                ]
                return SimpleNamespace(
                    returncode=0,
                    stdout="".join(json.dumps(event) + "\n" for event in events),
                    stderr="",
                )

            candidate = Candidate(
                candidate_id="candidate-1",
                dispatch_round_id="round-1",
                vehicle_id="vehicle-1",
                order_id="order-1",
                candidate_type="immediate",
                route_reachability=True,
                pickup_leg=RouteLeg(reachable=True),
                ride_leg=RouteLeg(reachable=True),
                vehicle_available_time_sec=0,
                predicted_pickup_time_sec=10,
                predicted_dropoff_time_sec=20,
                predicted_wait_sec=10,
                empty_distance_m=100,
                total_distance_m=200,
                energy_kwh=0.1,
                current_commitment_ok=True,
                one_to_one_ok=True,
                time_non_overlap_ok=True,
                max_wait_ok=True,
                hard_constraint_ok=True,
            )
            vehicles = [Vehicle("vehicle-1", "node-0", "node-0")]
            orders = [Order("order-1", 0, "node-1", "node-2", "hotspot", status="waiting")]

            with patch("dispatch_experiment.llm.shutil.which", return_value="/usr/bin/codex"), patch(
                "dispatch_experiment.llm.subprocess.run", side_effect=fake_run
            ):
                first_client = CodexCliDirectAssignment(model="gpt-5.6-luna", trace_dir=trace_dir)
                _, first_record = first_client.select(
                    snapshot_time_sec=0,
                    vehicles=vehicles,
                    orders=orders,
                    candidates=[candidate],
                    max_wait_sec=300,
                )
                second_client = CodexCliDirectAssignment(model="gpt-5.6-luna", trace_dir=trace_dir)
                _, second_record = second_client.select(
                    snapshot_time_sec=0,
                    vehicles=vehicles,
                    orders=orders,
                    candidates=[candidate],
                    max_wait_sec=300,
                )

            self.assertEqual(len(cli_calls), 2)
            self.assertEqual(first_client.call_count, 1)
            self.assertEqual(second_client.call_count, 1)
            self.assertTrue(first_record["trace_jsonl_path"].endswith("dispatch_round_0001.jsonl"))
            self.assertTrue(second_record["trace_jsonl_path"].endswith("dispatch_round_0002.jsonl"))
            self.assertAlmostEqual(first_record["cost_estimate"]["codex_standard_credit_equivalent"], 0.0071)
            self.assertIsNone(first_record["account_cost_usd"])
            disabled = {
                cli_calls[0][index + 1]
                for index, token in enumerate(cli_calls[0][:-1])
                if token == "--disable"
            }
            self.assertEqual(disabled, set(CODEX_DISABLED_TOOL_FEATURES))
            self.assertEqual(
                sum(token == "--disable" for token in cli_calls[0]),
                len(CODEX_DISABLED_TOOL_FEATURES),
            )
            self.assertEqual(
                first_record["tool_policy"]["disabled_features"],
                list(CODEX_DISABLED_TOOL_FEATURES),
            )


if __name__ == "__main__":
    unittest.main()
