import tempfile
import unittest
from pathlib import Path

from codex_cli_support import (
    aggregate_codex_usage_cost,
    estimate_codex_usage_cost,
    reserve_indexed_artifacts,
)


class CodexCliSupportTests(unittest.TestCase):
    def test_cost_estimate_uses_cached_subset_and_keeps_invoice_unknown(self):
        result = estimate_codex_usage_cost("gpt-5.6-luna", 1000, 200, 100)

        self.assertAlmostEqual(result["codex_standard_credit_equivalent"], 0.0071)
        self.assertAlmostEqual(result["api_equivalent_usd"], 0.000284)
        self.assertEqual(result["calculation_status"], "standard_rate_equivalent")
        self.assertIsNone(result["account_billing_amount"])
        self.assertFalse(result["api_equivalent_is_invoice"])

    def test_missing_cached_usage_is_labeled_as_uncached_upper_bound(self):
        result = estimate_codex_usage_cost("gpt-5.6-luna", 1000, None, 100)

        self.assertAlmostEqual(result["codex_standard_credit_equivalent"], 0.008)
        self.assertAlmostEqual(result["codex_standard_credit_equivalent_min"], 0.0035)
        self.assertAlmostEqual(result["api_equivalent_usd_min"], 0.00014)
        self.assertAlmostEqual(result["api_equivalent_usd_max"], 0.00032)
        self.assertEqual(
            result["calculation_status"], "upper_bound_assuming_no_cached_input"
        )

    def test_cached_input_greater_than_input_is_not_priced(self):
        result = estimate_codex_usage_cost("gpt-5.6-luna", 10, 11, 0)

        self.assertIsNone(result["api_equivalent_usd"])
        self.assertEqual(
            result["calculation_status"],
            "invalid_usage_cached_input_out_of_range",
        )

    def test_route_aggregate_sums_rate_card_equivalents_and_keeps_invoice_unknown(self):
        estimates = [
            estimate_codex_usage_cost("gpt-5.6-luna", 1000, 200, 100),
            estimate_codex_usage_cost("gpt-5.6-luna", 500, 0, 50),
        ]
        attempts = [
            {
                "provider_audit": {
                    "backend": "codex_cli",
                    "usage_available": True,
                    "input_tokens": 1000,
                    "cached_input_tokens": 200,
                    "output_tokens": 100,
                    "cost_estimate": estimates[0],
                }
            },
            {
                "provider_audit": {
                    "backend": "codex_cli",
                    "usage_available": True,
                    "input_tokens": 500,
                    "cached_input_tokens": 0,
                    "output_tokens": 50,
                    "cost_estimate": estimates[1],
                }
            },
        ]

        result = aggregate_codex_usage_cost(attempts, "gpt-5.6-luna")

        self.assertEqual(result["input_tokens"], 1500)
        self.assertEqual(result["cached_input_tokens"], 200)
        self.assertEqual(result["output_tokens"], 150)
        self.assertAlmostEqual(result["api_equivalent_usd"], 0.000444)
        self.assertAlmostEqual(result["codex_standard_credit_equivalent"], 0.0111)
        self.assertEqual(result["calculation_status"], "summed_official_standard_rate_equivalent")
        self.assertIsNone(result["account_invoice_amount"])

    def test_reservation_skips_existing_artifacts_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "dispatch_round_0001.jsonl"
            existing.write_text("preserve", encoding="utf-8")

            index, paths = reserve_indexed_artifacts(
                root,
                "dispatch_round",
                (".jsonl", ".final.json"),
                (".jsonl",),
            )

            self.assertEqual(index, 2)
            self.assertTrue(paths[".jsonl"].is_file())
            self.assertEqual(existing.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
