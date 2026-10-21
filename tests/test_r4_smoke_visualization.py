import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class R4SmokeVisualizationTests(unittest.TestCase):
    def test_v12_replay_shows_the_returned_failure_reason(self):
        source = (
            ROOT
            / "outputs/route_layer/r4_smoke_v12_api/seed0_semantic_road_feature.json"
        )
        renderer = ROOT / "scripts/render_route_r4_smoke.py"

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "replay.html"
            result = subprocess.run(
                [
                    sys.executable,
                    str(renderer),
                    "--input-json",
                    str(source),
                    "--output-html",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            html = output.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("模型未构造出路线；没有报告已确认无路", html)
        self.assertIn("事后 BFS 显示起点—上车点—下车点均可达", html)
        self.assertNotIn("模型返回 failed，但原响应没有失败原因", html)

    def test_v11_replay_does_not_present_undetailed_failure_as_proven_unreachable(self):
        source = (
            ROOT
            / "outputs/route_layer/r4_smoke_v11/seed0_semantic_road_feature.json"
        )
        renderer = ROOT / "scripts/render_route_r4_smoke.py"

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "replay.html"
            result = subprocess.run(
                [
                    sys.executable,
                    str(renderer),
                    "--input-json",
                    str(source),
                    "--output-html",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            html = output.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("这不是地图无路证明", html)
        self.assertIn("事后 BFS 显示起点—上车点—下车点均可达", html)
        self.assertIn("提示和失败分类已离线修正，尚未再次调用 API", html)

    def test_v9_replay_discloses_reconstructed_invalid_retry_json(self):
        source = (
            ROOT
            / "outputs/route_layer/r4_smoke_v9/seed0_semantic_road_feature.json"
        )
        renderer = ROOT / "scripts/render_route_r4_smoke.py"

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "replay.html"
            result = subprocess.run(
                [
                    sys.executable,
                    str(renderer),
                    "--input-json",
                    str(source),
                    "--output-html",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            html = output.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("重试请求格式审计", html)
        self.assertIn("JSONDecodeError: Extra data", html)
        self.assertIn("r4_validation_feedback", html)
        self.assertIn("不能据此认定格式问题就是模型失败的直接原因", html)
        self.assertIn("尚未重新调用 API", html)

    def test_single_retry_replay_reports_the_corrected_api_result(self):
        source = (
            ROOT
            / "outputs/route_layer/r4_smoke_v10_retry_validation/seed0_retry_validation.json"
        )
        renderer = ROOT / "scripts/render_route_r4_smoke.py"

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "replay.html"
            result = subprocess.run(
                [
                    sys.executable,
                    str(renderer),
                    "--input-json",
                    str(source),
                    "--output-html",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            html = output.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("corrected_retry_api_validation", html)
        self.assertIn("修正格式的反馈请求已发送 1 次", html)
        self.assertIn("violation.step", html)
        self.assertIn("one_way_direction", html)
        self.assertIn('"step":12', html)
        self.assertIn("directional_constraint_audit", html)
        self.assertIn("source_direction_slot", html)
        self.assertIn("node_6_4", html)
        self.assertIn("逐步路线自检", html)
        self.assertIn("模型自检是结构化声明", html)
        self.assertIn("已发送 1 次；", html)
        self.assertIn("seed0_retry_validation.json", html)


if __name__ == "__main__":
    unittest.main()
