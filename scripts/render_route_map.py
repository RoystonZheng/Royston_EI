#!/usr/bin/env python3
"""Render a seeded route map as a compact, inspectable HTML canvas view."""

import argparse
import html
import json
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _node_xy(node_id):
    _, x, y = node_id.split("_")
    return int(x), int(y)


def compact_map_data(road_map, showcase=None):
    nodes = {node.node_id: node for node in road_map.nodes}
    segments = OrderedDict()
    for edge in road_map.edges:
        pair = tuple(sorted((edge.from_node, edge.to_node)))
        current = segments.get(pair)
        if current is None:
            x1, y1 = _node_xy(edge.from_node)
            x2, y2 = _node_xy(edge.to_node)
            current = {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "congestion": edge.congestion_level,
                "one_way": edge.one_way,
                "signal": edge.signal_delay_sec,
            }
            segments[pair] = current
        elif not edge.one_way:
            current["one_way"] = False
    landmarks = []
    for node in road_map.nodes:
        if node.landmark_name:
            landmarks.append([node.x, node.y, node.landmark_name])
    feature_by_id = {feature.feature_id: feature for feature in road_map.roadside_features}
    features = []
    for feature in road_map.roadside_features:
        node = nodes[feature.node_id]
        paired = nodes.get(feature.paired_node_id) if feature.paired_node_id else None
        features.append(
            {
                "id": feature.feature_id,
                "type": feature.feature_type,
                "label": feature.label,
                "x": node.x,
                "y": node.y,
                "paired_x": paired.x if paired else None,
                "paired_y": paired.y if paired else None,
                "side": feature.side_of_road,
                "orientation": feature.orientation,
            }
        )
    scenarios = []
    for scenario in (showcase or {}).get("scenarios", []):
        def point(node_id):
            node = nodes.get(node_id)
            return [node.x, node.y] if node else None

        scenarios.append(
            {
                "id": scenario["scenario_id"],
                "title": scenario["title"],
                "type": scenario["scenario_type"],
                "description": scenario["description"],
                "query": scenario["semantic_query"],
                "start": point(scenario["start_node_id"]),
                "baseline": point(scenario.get("baseline_selected_node_id")),
                "agent": point(scenario.get("agent_selected_node_id")),
                "dropoff": point(scenario.get("dropoff_node_id")),
                "feature_ids": [feature_id for feature_id in scenario["feature_ids"] if feature_id in feature_by_id],
                "baseline_behavior": scenario["expected_baseline_behavior"],
                "agent_behavior": scenario["expected_agent_behavior"],
                "route_effect": scenario["expected_route_effect"],
            }
        )
    return {
        "size": road_map.size,
        "segments": [
            [
                item["x1"],
                item["y1"],
                item["x2"],
                item["y2"],
                item["congestion"],
                1 if item["one_way"] else 0,
            ]
            for item in segments.values()
        ],
        "landmarks": landmarks,
        "features": features,
        "scenarios": scenarios,
    }


def build_fragment(road_map, summary, showcase=None):
    data = compact_map_data(road_map, showcase)
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    summary_json = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    title = "100×100 随机道路图与路线特征场景"
    return f'''<div id="route-map-100x100" role="img" aria-label="{title}，按拥堵等级显示道路，箭头表示单向道路，紫色标记表示地标。">
  <style>
    #route-map-100x100 {{
      color: var(--foreground);
      --route-series-1: var(--viz-series-1, light-dark(#4f83cc, #8ab4f8));
      --route-series-2: var(--viz-series-2, light-dark(#d98a35, #f2b66d));
      --route-series-3: var(--viz-series-3, light-dark(#c94f4f, #f28b82));
      --route-series-4: var(--viz-series-4, light-dark(#7c4dba, #c4a7e7));
      --route-feature-sign: var(--viz-series-2, light-dark(#b45309, #fbbf24));
      --route-feature-pole: var(--viz-series-1, light-dark(#334155, #cbd5e1));
      --route-scenario-start: var(--viz-series-4, light-dark(#15803d, #86efac));
      --route-scenario-baseline: var(--viz-series-3, light-dark(#b91c1c, #fca5a5));
      --route-scenario-agent: var(--viz-series-1, light-dark(#0369a1, #7dd3fc));
      --route-background: var(--muted, light-dark(#f1f5f9, #1e293b));
      --route-border: var(--border, light-dark(#cbd5e1, #475569));
      --route-muted: var(--muted-foreground, light-dark(#475569, #cbd5e1));
      --route-foreground: var(--foreground, light-dark(#111827, #f3f4f6));
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 760px;
      margin: 0 auto;
    }}
    #route-map-100x100 .map-title {{ font-size: 18px; font-weight: 500; margin-bottom: 4px; }}
    #route-map-100x100 .map-meta {{ color: var(--muted-foreground); font-size: 12px; margin-bottom: 10px; }}
    #route-map-100x100 canvas {{ display: block; width: 100%; aspect-ratio: 1 / 1; background: var(--route-background); border: 1px solid var(--route-border); }}
    #route-map-100x100 .map-legend {{ display: flex; flex-wrap: wrap; gap: 10px 16px; margin-top: 9px; color: var(--muted-foreground); font-size: 12px; }}
    #route-map-100x100 .legend-item {{ display: inline-flex; align-items: center; gap: 5px; }}
    #route-map-100x100 .swatch {{ width: 18px; height: 3px; display: inline-block; background: var(--route-series-1); }}
    #route-map-100x100 .swatch.level-1 {{ background: var(--route-series-2); }}
    #route-map-100x100 .swatch.level-2 {{ background: var(--route-series-3); }}
    #route-map-100x100 .dot {{ width: 8px; height: 8px; display: inline-block; border-radius: 50%; background: var(--route-series-4); }}
    #route-map-100x100 .dot.feature-sign {{ background: var(--route-feature-sign); }}
    #route-map-100x100 .dot.feature-pole {{ background: var(--route-feature-pole); }}
    #route-map-100x100 .map-summary {{ color: var(--muted-foreground); font-size: 12px; margin-top: 8px; }}
    #route-map-100x100 .scenario-list {{ display: grid; gap: 8px; margin-top: 12px; }}
    #route-map-100x100 .scenario-card {{ border: 1px solid var(--route-border); border-radius: 8px; padding: 8px 10px; background: var(--route-background); }}
    #route-map-100x100 .scenario-title {{ color: var(--route-foreground); font-size: 13px; font-weight: 600; }}
    #route-map-100x100 .scenario-text {{ color: var(--route-muted); font-size: 12px; margin-top: 3px; line-height: 1.45; }}
    #route-map-100x100 .sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
  </style>
  <div class="map-title">{title}</div>
  <div class="map-meta">种子 {{SUMMARY["seed"]}} · 地图 {{SUMMARY["map_id"]}} · 边界 0–99 × 0–99</div>
  <canvas id="route-map-canvas" width="720" height="720" aria-label="道路图画布"></canvas>
  <div class="map-legend" aria-label="图例">
    <span class="legend-item"><span class="swatch"></span>低拥堵</span>
    <span class="legend-item"><span class="swatch level-1"></span>中拥堵</span>
    <span class="legend-item"><span class="swatch level-2"></span>高拥堵</span>
    <span class="legend-item"><span class="dot"></span>地标</span>
    <span class="legend-item"><span class="dot feature-sign"></span>路牌</span>
    <span class="legend-item"><span class="dot feature-pole"></span>电线杆</span>
    <span class="legend-item">箭头：单向道路</span>
    <span class="legend-item">红圈：图搜索候选</span>
    <span class="legend-item">蓝圈：Agent 特征定位</span>
  </div>
  <div class="map-summary">节点 {{SUMMARY["node_count"]}} · 道路边 {{SUMMARY["edge_count"]}} · 单向边 {{SUMMARY["one_way_edge_count"]}} · 信号灯边 {{SUMMARY["signal_edge_count"]}} · 路牌/电线杆 {{SUMMARY.get("roadside_feature_type_counts", {{}}).get("road_sign", 0)}}/{{SUMMARY.get("roadside_feature_type_counts", {{}}).get("utility_pole", 0)}}</div>
  <div class="scenario-list" id="route-scenario-list" aria-label="路线特征场景"></div>
  <div class="sr-only">地图数据摘要：{{JSON.stringify(SUMMARY)}}</div>
  <script>
  (() => {{
    const root = document.getElementById("route-map-100x100");
    const canvas = document.getElementById("route-map-canvas");
    const ctx = canvas.getContext("2d");
    const data = DATA;
    const summary = SUMMARY;
    const css = getComputedStyle(root);
    const color = (name, fallback) => {{
      const value = css.getPropertyValue(name).trim();
      return value && !value.startsWith("light-dark(") && !value.startsWith("var(") ? value : fallback;
    }};
    const colors = [
      color("--route-series-1", "rgb(79, 131, 204)"),
      color("--route-series-2", "rgb(217, 138, 53)"),
      color("--route-series-3", "rgb(201, 79, 79)"),
    ];
    const landmarkColor = color("--route-series-4", "rgb(124, 77, 186)");
    const foreground = color("--route-foreground", "rgb(17, 24, 39)");
    const border = color("--route-border", "rgb(148, 163, 184)");
    const muted = color("--route-muted", "rgb(71, 85, 105)");
    const featureSign = color("--route-feature-sign", "rgb(245, 158, 11)");
    const featurePole = color("--route-feature-pole", "rgb(51, 65, 85)");
    const scenarioStart = color("--route-scenario-start", "rgb(21, 128, 61)");
    const scenarioBaseline = color("--route-scenario-baseline", "rgb(185, 28, 28)");
    const scenarioAgent = color("--route-scenario-agent", "rgb(3, 105, 161)");
    root.querySelectorAll(".map-meta").forEach((node) => {{ node.textContent = `种子 ${{summary.seed}} · 地图 ${{summary.map_id}} · 边界 0–99 × 0–99`; }});
    const featureCounts = summary.roadside_feature_type_counts || {{}};
    root.querySelectorAll(".map-summary").forEach((node) => {{ node.textContent = `节点 ${{summary.node_count}} · 道路边 ${{summary.edge_count}} · 单向边 ${{summary.one_way_edge_count}} · 信号灯边 ${{summary.signal_edge_count}} · 路牌/电线杆 ${{featureCounts.road_sign || 0}}/${{featureCounts.utility_pole || 0}}`; }});

    function draw() {{
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const width = Math.max(320, rect.width);
      const height = width;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const pad = 20;
      const scale = (width - pad * 2) / (data.size - 1);
      const point = (x, y) => [pad + x * scale, height - pad - y * scale];
      ctx.lineCap = "round";
      ctx.globalAlpha = 0.72;
      data.segments.forEach(([x1, y1, x2, y2, congestion, oneWay]) => {{
        const a = point(x1, y1);
        const b = point(x2, y2);
        ctx.beginPath();
        ctx.moveTo(a[0], a[1]);
        ctx.lineTo(b[0], b[1]);
        ctx.strokeStyle = colors[congestion] || colors[0];
        ctx.lineWidth = oneWay ? 1.4 : 0.9;
        ctx.stroke();
        if (oneWay) {{
          const mx = a[0] * 0.58 + b[0] * 0.42;
          const my = a[1] * 0.58 + b[1] * 0.42;
          const dx = b[0] - a[0];
          const dy = b[1] - a[1];
          const length = Math.max(1, Math.hypot(dx, dy));
          const ux = dx / length;
          const uy = dy / length;
          ctx.beginPath();
          ctx.moveTo(mx + ux * 3, my + uy * 3);
          ctx.lineTo(mx - ux * 2 - uy * 2, my - uy * 2 + ux * 2);
          ctx.lineTo(mx - ux * 2 + uy * 2, my - uy * 2 - ux * 2);
          ctx.closePath();
          ctx.fillStyle = colors[congestion] || colors[0];
          ctx.fill();
        }}
      }});
      ctx.globalAlpha = 1;
      ctx.fillStyle = landmarkColor;
      data.landmarks.forEach(([x, y]) => {{
        const p = point(x, y);
        ctx.beginPath();
        ctx.arc(p[0], p[1], 3.5, 0, Math.PI * 2);
        ctx.fill();
      }});
      data.features.forEach((feature) => {{
        const p = point(feature.x, feature.y);
        ctx.strokeStyle = feature.type === "road_sign" ? featureSign : featurePole;
        ctx.fillStyle = ctx.strokeStyle;
        ctx.lineWidth = 1.5;
        if (feature.type === "road_sign") {{
          ctx.beginPath();
          ctx.moveTo(p[0], p[1] - 4);
          ctx.lineTo(p[0] + 4, p[1]);
          ctx.lineTo(p[0], p[1] + 4);
          ctx.lineTo(p[0] - 4, p[1]);
          ctx.closePath();
          ctx.fill();
        }} else {{
          ctx.beginPath();
          ctx.moveTo(p[0], p[1] - 4);
          ctx.lineTo(p[0], p[1] + 4);
          ctx.stroke();
          ctx.beginPath();
          ctx.arc(p[0], p[1] - 4, 2, 0, Math.PI * 2);
          ctx.fill();
        }}
      }});
      data.scenarios.forEach((scenario) => {{
        const start = scenario.start && point(scenario.start[0], scenario.start[1]);
        const baseline = scenario.baseline && point(scenario.baseline[0], scenario.baseline[1]);
        const agent = scenario.agent && point(scenario.agent[0], scenario.agent[1]);
        if (start && baseline) {{
          ctx.save();
          ctx.globalAlpha = 0.55;
          ctx.setLineDash([4, 4]);
          ctx.strokeStyle = scenarioBaseline;
          ctx.beginPath(); ctx.moveTo(start[0], start[1]); ctx.lineTo(baseline[0], baseline[1]); ctx.stroke();
          ctx.restore();
        }}
        if (start && agent) {{
          ctx.save();
          ctx.globalAlpha = 0.65;
          ctx.strokeStyle = scenarioAgent;
          ctx.beginPath(); ctx.moveTo(start[0], start[1]); ctx.lineTo(agent[0], agent[1]); ctx.stroke();
          ctx.restore();
        }}
        if (start) {{
          ctx.fillStyle = scenarioStart;
          ctx.fillRect(start[0] - 3, start[1] - 3, 6, 6);
        }}
        if (baseline) {{
          ctx.strokeStyle = scenarioBaseline;
          ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(baseline[0], baseline[1], 5, 0, Math.PI * 2); ctx.stroke();
        }}
        if (agent) {{
          ctx.strokeStyle = scenarioAgent;
          ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(agent[0], agent[1], 5, 0, Math.PI * 2); ctx.stroke();
        }}
      }});
      ctx.strokeStyle = border;
      ctx.lineWidth = 1;
      ctx.strokeRect(pad, pad, width - pad * 2, height - pad * 2);
      ctx.fillStyle = muted;
      ctx.font = "11px system-ui, sans-serif";
      ctx.fillText("0", pad - 2, height - 5);
      ctx.fillText(String(data.size - 1), width - pad - 18, height - 5);
      ctx.fillText(String(data.size - 1), 3, pad + 4);
      ctx.fillStyle = foreground;
    }}
    const scenarioList = document.getElementById("route-scenario-list");
    data.scenarios.forEach((scenario) => {{
      const card = document.createElement("div");
      card.className = "scenario-card";
      const titleNode = document.createElement("div");
      titleNode.className = "scenario-title";
      titleNode.textContent = `${{scenario.title}} · ${{scenario.type}}`;
      const textNode = document.createElement("div");
      textNode.className = "scenario-text";
      textNode.textContent = `输入：${{scenario.query}} | 图搜索：${{scenario.baseline_behavior}} | Agent：${{scenario.agent_behavior}} | 预期：${{scenario.route_effect}}`;
      card.appendChild(titleNode);
      card.appendChild(textNode);
      scenarioList.appendChild(card);
    }});
    window.addEventListener("resize", draw);
    draw();
  }})();
  </script>
</div>'''.replace("DATA", data_json).replace("SUMMARY", summary_json)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("map_json")
    parser.add_argument("summary_json")
    parser.add_argument("output_html")
    parser.add_argument("--showcase-json", default=None)
    args = parser.parse_args()

    map_payload = json.loads(Path(args.map_json).read_text(encoding="utf-8"))
    summary = json.loads(Path(args.summary_json).read_text(encoding="utf-8"))
    showcase = {}
    if args.showcase_json:
        showcase = json.loads(Path(args.showcase_json).read_text(encoding="utf-8"))

    class RoadMapView:
        pass

    road_map = RoadMapView()
    road_map.size = map_payload["size"]
    road_map.nodes = [type("NodeView", (), node) for node in map_payload["nodes"]]
    road_map.edges = [type("EdgeView", (), edge) for edge in map_payload["edges"]]
    road_map.roadside_features = [
        type("FeatureView", (), feature)
        for feature in map_payload.get("roadside_features", [])
    ]
    fragment = build_fragment(
        road_map,
        dict(summary, seed=_seed_from_map_id(summary["map_id"])),
        showcase,
    )
    output = Path(args.output_html)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(fragment, encoding="utf-8")
    print("fragment_path={0}".format(output))
    print("fragment_bytes={0}".format(output.stat().st_size))


def _seed_from_map_id(map_id):
    return int(map_id.rsplit("-", 1)[-1])


if __name__ == "__main__":
    main()
