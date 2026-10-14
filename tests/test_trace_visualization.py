import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TraditionalTraceVisualizationTests(unittest.TestCase):
    def test_renderer_writes_one_visual_record_per_task_and_method(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            map_path = root / "map.json"
            order_path = root / "orders.json"
            routes_path = root / "routes.json"
            output_dir = root / "trace_visualizations"

            subprocess.run(
                [
                    sys.executable,
                    "scripts/generate_route_map.py",
                    "--seed",
                    "0",
                    "--size",
                    "20",
                    "--output-dir",
                    str(root / "map_generation"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            generated_map = root / "map_generation" / "map_20x20_seed_0.json"
            map_path.write_text(generated_map.read_text(encoding="utf-8"), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    "scripts/generate_order_tasks.py",
                    str(map_path),
                    str(order_path),
                    "--seed",
                    "0",
                    "--task-count",
                    "6",
                    "--exact-ratio",
                    "0.5",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    "scripts/run_traditional_routes.py",
                    "--map-json",
                    str(map_path),
                    "--order-json",
                    str(order_path),
                    "--output-json",
                    str(routes_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    "scripts/render_traditional_traces.py",
                    "--map-json",
                    str(map_path),
                    "--routes-json",
                    str(routes_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["trace_count"], 18)
            self.assertEqual(len(manifest["traces"]), 18)
            self.assertTrue((output_dir / "index.html").exists())
            for trace in manifest["traces"]:
                self.assertTrue((output_dir / trace["html_file"]).exists())
                self.assertTrue((output_dir / trace["json_file"]).exists())
                self.assertIn(trace["method_name"], {
                    "full_map_dijkstra", "local_dijkstra", "local_a_star"
                })


if __name__ == "__main__":
    unittest.main()
