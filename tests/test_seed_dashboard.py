import subprocess
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from render_seed_dashboard import render_seed_dashboard


class SeedDashboardTests(unittest.TestCase):
    def test_r4_checkpoint_dashboard_shows_recorded_model_usage_without_claiming_invoice(self):
        source_seed = ROOT / "outputs/route_layer/r4_codex_formal_checkpoint_v1/seeds/seed_0"

        with tempfile.TemporaryDirectory() as temp_dir:
            seed_dir = Path(temp_dir) / "seed_0"
            shutil.copytree(source_seed, seed_dir)
            dashboard = render_seed_dashboard(seed_dir)
            html = dashboard.read_text(encoding="utf-8")

        self.assertIn("模型请求次数", html)
        self.assertIn("模型输入 / 输出 token", html)
        self.assertIn("模型响应耗时", html)
        self.assertIn("API 标价等值（估算，非账单）", html)
        self.assertIn('"llm_input_tokens":15567', html)
        self.assertIn('"llm_output_tokens":110', html)
        self.assertIn('"codex_api_equivalent_usd":0.0032454000000000003', html)
        self.assertIn("账户账单状态", html)

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
