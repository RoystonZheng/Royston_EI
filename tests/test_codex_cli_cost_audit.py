import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_codex_cli_costs import audit


class CodexCliCostAuditTests(unittest.TestCase):
    def test_missing_usage_stays_unknown_in_full_trace_total(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "route_codex" / "traces"
            trace_dir.mkdir(parents=True)
            (trace_dir / "known.jsonl").write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "turn.started"},
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 7},
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            (trace_dir / "unknown.jsonl").write_text(
                json.dumps({"type": "turn.started"}) + "\n",
                encoding="utf-8",
            )

            result = audit(root, "gpt-5.6-luna")

        self.assertEqual(result["attempts_with_turn_completed_usage"], 1)
        self.assertEqual(result["attempts_started_but_usage_missing"], 1)
        self.assertEqual(result["known_total_tokens_from_reported_usage"], 17)
        self.assertIsNone(result["complete_total_tokens_across_scanned_traces"])
        self.assertEqual(result["sum_of_per_trace_codex_standard_credit_equivalents"], 0.00026)

    def test_dispatch_cost_smoke_is_not_merged_into_experiment_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            trace_dir = (
                Path(temporary)
                / "dispatch_layer"
                / "results"
                / "codex_cli_cost_smoke_20261021"
                / "codex_cli_traces"
            )
            trace_dir.mkdir(parents=True)
            (trace_dir / "dispatch_round_0001.jsonl").write_text(
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 7},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = audit(Path(temporary), "gpt-5.6-luna")

        self.assertIn("dispatch_codex_cli_cost_smoke", result["by_experiment_group"])
        self.assertNotIn("dispatch_layer_codex_cli", result["by_experiment_group"])

    def test_separate_route_continuations_get_separate_cost_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version in ("v1", "v2"):
                trace_dir = root / "route_layer" / f"r4_codex_pilot_continuation_{version}" / "traces"
                trace_dir.mkdir(parents=True)
                (trace_dir / "step.jsonl").write_text(
                    json.dumps({
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 7},
                    }) + "\n",
                    encoding="utf-8",
                )

            result = audit(root, "gpt-5.6-luna")

        groups = result["by_experiment_group"]
        self.assertEqual(groups["route_codex_pilot_continuation_v1"]["trace_files"], 1)
        self.assertEqual(groups["route_codex_pilot_continuation_v2"]["trace_files"], 1)

    def test_non_trace_jsonl_progress_stream_is_not_counted_as_model_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "dispatch_layer" / "results" / "pure_llm_codex_cli_formal_v1"
            trace_dir = run_dir / "seed_1" / "codex_cli_traces"
            trace_dir.mkdir(parents=True)
            (run_dir / "batch_progress.jsonl").write_text(
                json.dumps({"round": 1, "status": "done"}) + "\n",
                encoding="utf-8",
            )
            (trace_dir / "round.jsonl").write_text(
                json.dumps({
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 7},
                }) + "\n",
                encoding="utf-8",
            )

            result = audit(root, "gpt-5.6-luna")

        self.assertEqual(result["files_scanned"], 2)
        self.assertEqual(result["unique_traces"], 1)
        self.assertEqual(result["non_trace_jsonl_files_skipped"], 1)
        self.assertEqual(result["attempts_with_turn_completed_usage"], 1)


if __name__ == "__main__":
    unittest.main()
