import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SeedSmokeTests(unittest.TestCase):
    def test_multi_seed_smoke_checks_determinism_routes_and_prices(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "smoke"
            price_path = Path(temp_dir) / "price.json"
            price_path.write_text(
                json.dumps(
                    {
                        "api_price_per_token": 0.000003,
                        "compute_price_per_ms": 0.00000001,
                        "electricity_price_per_kwh": 0.8,
                        "currency": "CNY",
                        "pricing_version": "test_smoke_price_v1",
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
                    "scripts/run_seed_smoke.py",
                    "--seeds",
                    "0",
                    "1",
                    "--size",
                    "20",
                    "--output-dir",
                    str(output_dir),
                    "--price-config-json",
                    str(price_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "passed")
            self.assertEqual(manifest["seeds"], [0, 1])
            self.assertEqual(manifest["price_profile"]["pricing_version"], "test_smoke_price_v1")
            self.assertEqual(len(manifest["summaries"]), 2)
            for summary in manifest["summaries"]:
                self.assertTrue(summary["deterministic_check"]["passed"])
                self.assertEqual(summary["exact_task_count"], 7)
                self.assertEqual(summary["semantic_task_count"], 3)
                self.assertEqual(
                    set(summary["formal_scenario_types"]),
                    {"feasible", "boundary", "illegal_endpoint", "detour", "unreachable"},
                )
                for method in summary["mixed_summary"].values():
                    self.assertEqual(method["completed"], 10)
                    self.assertEqual(method["valid_routes"], 10)
                    self.assertEqual(method["evaluated_routes"], 10)
                    self.assertGreater(method["total_electricity_cost"], 0.0)


if __name__ == "__main__":
    unittest.main()
