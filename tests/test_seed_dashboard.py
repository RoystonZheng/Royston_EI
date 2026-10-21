import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SeedDashboardTests(unittest.TestCase):
    def test_seed_dashboard_shows_map_and_algorithm_result_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "experiment"
            subprocess.run(
                [
                    sys.executable,
                    "scripts/run_traditional_experiment.py",
                    "--seeds",
                    "0",
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
            dashboard = output_dir / "seeds" / "seed_0" / "index.html"
            html = dashboard.read_text(encoding="utf-8")
            seed_dir = dashboard.parent

            self.assertIn("原地图", html)
            self.assertIn("map_preview.html", html)
            self.assertIn("visualizations/orders/index.html", html)
            self.assertIn("../../index.html", html)
            self.assertIn("算法结果", html)
            self.assertIn("full_map_dijkstra", html)
            self.assertIn("local_a_star", html)
            self.assertIn("compute_time_ms", html)
            self.assertIn("energy_kwh", html)
            self.assertIn("start_node_id", html)
            self.assertIn("dropoff_node_id", html)
            self.assertIn("订单", html)
            self.assertIn("正式场景", html)
            self.assertIn("单向道路箭头", html)
            self.assertIn("费用状态", html)
            self.assertIn("电费", html)
            self.assertIn("失败原因", html)
            self.assertIn("响应是否收到", html)
            self.assertIn("提议路线耗电估算", html)
            self.assertIn("未提出路线", html)

            map_preview = (seed_dir / "map_preview.html").read_text(encoding="utf-8")
            map_svg = (seed_dir / "map_preview.svg").read_text(encoding="utf-8")
            self.assertIn("seed 0 原始地图", map_preview)
            self.assertIn('src="map_preview.svg"', map_preview)
            self.assertIn("<svg", map_svg)
            self.assertIn("marker-end", map_svg)


if __name__ == "__main__":
    unittest.main()
