import json
import re
import shutil
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
            order_path = root / "order_tasks_seed_0.json"
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
            order_payload = json.loads(order_path.read_text(encoding="utf-8"))
            task = order_payload["tasks"][0]
            map_payload = json.loads(map_path.read_text(encoding="utf-8"))
            node_ids = {node["node_id"] for node in map_payload["nodes"]}
            edge_ids = {edge["edge_id"] for edge in map_payload["edges"]}
            start = next(node for node in map_payload["nodes"] if node["node_id"] == task["start_node_id"])
            directions = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}
            invalid_move = next(
                direction
                for direction, (dx, dy) in directions.items()
                if "node_{0}_{1}".format(start["x"] + dx, start["y"] + dy) in node_ids
                and "edge_{0}_{1}_node_{2}_{3}".format(
                    start["x"], start["y"], start["x"] + dx, start["y"] + dy
                ) not in edge_ids
            )
            route_payload = json.loads(routes_path.read_text(encoding="utf-8"))
            route_item = next(item for item in route_payload["results"] if item["task_id"] == task["task_id"])
            route_item["methods"].append({
                "task_id": task["task_id"],
                "method_name": "pure_llm_deepseek",
                "status": "failed",
                "failure_reason": "llm_move_sequence_invalid",
                "metadata": {
                    "llm_status": "move_sequence_invalid",
                    "move_sequence": invalid_move,
                    "llm_response_received": True,
                    "failure_detail_zh": "模型已返回路线，但路线校验发现非法移动：单行方向相反 1 步。",
                    "proposal_energy_audit": {
                        "status": "estimated",
                        "route_energy_estimate_kwh": 0.123456,
                        "validated_prefix_energy_estimate_kwh": 0.0,
                        "interpretation": "not actual measured consumption",
                    },
                },
                "raw_output": {
                    "status": "completed",
                    "pickup_node_id": task["pickup_node_id"],
                    "dropoff_node_id": task["dropoff_node_id"],
                    "move_sequence": invalid_move,
                },
            })
            routes_path.write_text(json.dumps(route_payload), encoding="utf-8")

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
            self.assertEqual(manifest["trace_count"], 19)
            self.assertEqual(len(manifest["traces"]), 19)
            self.assertTrue((output_dir / "index.html").exists())
            for trace in manifest["traces"]:
                self.assertTrue((output_dir / trace["html_file"]).exists())
                self.assertTrue((output_dir / trace["json_file"]).exists())
                self.assertIn(trace["method_name"], {
                    "full_map_dijkstra", "local_dijkstra", "local_a_star", "pure_llm_deepseek"
                })
            llm_entry = next(item for item in manifest["traces"] if item["method_name"] == "pure_llm_deepseek")
            llm_json = json.loads((output_dir / llm_entry["json_file"]).read_text(encoding="utf-8"))
            self.assertEqual(llm_json["trace"]["attempted_move_sequence"], invalid_move)
            self.assertEqual(llm_json["trace"]["move_violations"][0]["step"], 1)
            self.assertTrue(llm_json["geometry"]["move_violations"][0]["from_xy"])
            llm_html = (output_dir / llm_entry["html_file"]).read_text(encoding="utf-8")
            self.assertIn("违规位置", llm_html)
            self.assertIn("data-step", llm_html)
            self.assertIn("one_way_direction", llm_html)
            self.assertIn("合法前缀", llm_html)
            self.assertIn("违规动作", llm_html)
            self.assertIn("模型提议的后续路线", llm_html)
            self.assertIn("模型响应是否收到", llm_html)
            self.assertIn("提议路线耗电估算", llm_html)
            self.assertIn("0.123456", llm_html)
            self.assertIn('href="../../index.html"', llm_html)
            self.assertIn('href="../../map_preview.html"', llm_html)
            self.assertIn('href="../../../../index.html"', llm_html)
            scope_index = (output_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn('href="../../index.html"', scope_index)
            for trace in manifest["traces"]:
                links = '<a href="{0}">HTML</a> · <a href="{1}">JSON</a>'.format(
                    trace["html_file"], trace["json_file"]
                )
                self.assertIn(links, scope_index)
                self.assertIn(
                    "<td>{0}</td><td>{1}</td>".format(trace["violation_count"], links),
                    scope_index,
                )
            self.assertNotIn("失败原因", scope_index)
            self.assertNotIn("提议路线耗电估算", scope_index)
            self.assertNotIn("0.123456 kWh", scope_index)
            node = shutil.which("node")
            if node:
                script = re.search(r"<script>(.*?)</script>", llm_html, re.DOTALL).group(1)
                syntax_check = subprocess.run(
                    [node, "--check", "-"],
                    input=script,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(syntax_check.returncode, 0, syntax_check.stderr)


if __name__ == "__main__":
    unittest.main()
