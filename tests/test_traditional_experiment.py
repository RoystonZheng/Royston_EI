import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TraditionalExperimentTests(unittest.TestCase):
    def test_batch_runner_keeps_seed_routes_costs_and_visual_trace_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "experiment"
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_traditional_experiment.py",
                    "--seeds",
                    "0",
                    "1",
                    "--size",
                    "20",
                    "--output-dir",
                    str(output_dir),
                    "--price-config-json",
                    "configs/route_layer/price_smoke_v1.json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn('"status": "passed"', completed.stdout)
            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "passed")
            self.assertEqual(manifest["seeds"], [0, 1])
            self.assertEqual(manifest["total_trace_count"], 90)
            self.assertTrue((output_dir / "index.html").exists())
            for seed_record in manifest["seeds_detail"]:
                self.assertEqual(seed_record["order_trace_count"], 30)
                self.assertEqual(seed_record["formal_trace_count"], 15)
                self.assertTrue(
                    (output_dir / seed_record["order_visual_index"]).exists()
                )
                self.assertTrue(
                    (output_dir / seed_record["formal_visual_index"]).exists()
                )
                self.assertTrue(
                    (output_dir / seed_record["order_routes_costs"]).exists()
                )
                self.assertTrue(
                    (output_dir / seed_record["formal_routes_costs"]).exists()
                )


if __name__ == "__main__":
    unittest.main()
