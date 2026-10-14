#!/usr/bin/env python3
"""Render persisted R0/R1/R2 search traces as replayable HTML records.

The renderer never reruns a routing algorithm.  It consumes the trace stored
by ``run_traditional_routes.py`` and writes one JSON record plus one standalone
HTML replay for every task-method pair.
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_experiment.models import Edge, Node, RoadMap, RoadsideFeature


def load_road_map(path: Path) -> RoadMap:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RoadMap(
        map_id=payload["map_id"],
        version=payload["version"],
        size=payload["size"],
        nodes=[Node(**node) for node in payload["nodes"]],
        edges=[Edge(**edge) for edge in payload["edges"]],
        boundary=payload["boundary"],
        road_state_version=payload.get("road_state_version", "road_state_v1"),
        roadside_features=[
            RoadsideFeature(**feature)
            for feature in payload.get("roadside_features", [])
        ],
    )


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return cleaned.strip("._") or "trace"


def _trace_geometry(road_map: RoadMap, trace: dict) -> dict:
    node_lookup = {node.node_id: node for node in road_map.nodes}
    edge_lookup = {edge.edge_id: edge for edge in road_map.edges}
    node_ids = []
    expanded_events = []
    for leg in trace.get("legs", []):
        for event in leg.get("expanded_events", []):
            expanded_events.append(
                {
                    "step": len(expanded_events) + 1,
                    "node_id": event.get("node_id"),
                    "g_cost_sec": event.get("g_cost_sec"),
                    "priority_sec": event.get("priority_sec"),
                    "frontier_size": event.get("frontier_size"),
                    "leg": leg.get("source_node_id", ""),
                }
            )
            if event.get("node_id") in node_lookup:
                node_ids.append(event["node_id"])
        for node_id in leg.get("route_node_sequence", []):
            if node_id in node_lookup:
                node_ids.append(node_id)
    route_node_ids = [
        node_id
        for node_id in trace.get("route_node_sequence", [])
        if node_id in node_lookup
    ]
    node_ids.extend(route_node_ids)
    unique_node_ids = list(dict.fromkeys(node_ids))
    nodes = [
        {
            "node_id": node_id,
            "x": node_lookup[node_id].x,
            "y": node_lookup[node_id].y,
        }
        for node_id in unique_node_ids
    ]
    route_edges = []
    for edge_id in trace.get("route_edge_sequence", []):
        edge = edge_lookup.get(edge_id)
        if edge is None:
            continue
        route_edges.append(
            {
                "edge_id": edge.edge_id,
                "from_node": edge.from_node,
                "to_node": edge.to_node,
            }
        )
    endpoints = []
    for leg in trace.get("legs", []):
        if leg.get("source_node_id"):
            endpoints.append(leg["source_node_id"])
        if leg.get("target_node_id"):
            endpoints.append(leg["target_node_id"])
    return {
        "map_id": road_map.map_id,
        "map_size": road_map.size,
        "boundary": road_map.boundary,
        "nodes": nodes,
        "expanded_events": expanded_events,
        "route_node_ids": route_node_ids,
        "route_edges": route_edges,
        "endpoint_node_ids": list(dict.fromkeys(endpoints)),
    }


def _record_html(record: dict) -> str:
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    title = html.escape(record["trace_id"])
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>传统路线检索轨迹 - {title}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0;background:#f4f6fb;color:#1f2937}}
header{{padding:16px 22px;background:#172554;color:#fff}}
h1{{font-size:20px;margin:0 0 6px}} .meta{{font-size:13px;opacity:.9}}
main{{display:grid;grid-template-columns:minmax(620px,1fr) 360px;gap:14px;padding:14px}}
.card{{background:#fff;border:1px solid #dbe3f0;border-radius:10px;padding:14px;box-shadow:0 2px 8px #17255412}}
canvas{{width:100%;height:680px;border:1px solid #dbe3f0;border-radius:8px;background:#fbfdff}}
label{{font-size:13px}} input{{width:100%}} table{{width:100%;border-collapse:collapse;font-size:12px}}
td,th{{padding:5px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
.legend span{{display:inline-block;margin-right:10px;font-size:12px}} .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}}
.blue{{background:#2563eb}} .red{{background:#dc2626}} .orange{{background:#f59e0b}}
</style></head><body>
<header><h1>传统算法检索与运行轨迹</h1><div class="meta">{title}</div></header>
<main><section class="card"><canvas id="map" width="1000" height="760"></canvas>
<p><label>检索回放步数：<output id="stepLabel">0</output></label>
<input id="step" type="range" min="0" value="0"></p>
<div class="legend"><span><i class="dot blue"></i>已展开节点</span><span><i class="dot orange"></i>当前节点</span><span><i class="dot red"></i>最终路线</span></div></section>
<section class="card"><div id="summary"></div><h3>每段检索</h3><table><thead><tr><th>段</th><th>状态</th><th>起点 → 终点</th><th>展开</th></tr></thead><tbody id="legs"></tbody></table></section></main>
<script>
const DATA = {payload};
const trace = DATA.trace || {{}}; const geo = DATA.geometry || {{}};
const canvas = document.getElementById('map'); const ctx = canvas.getContext('2d');
const nodes = Object.fromEntries((geo.nodes || []).map(n => [n.node_id, n]));
const events = geo.expanded_events || []; const route = geo.route_node_ids || [];
const endpointIds = new Set(geo.endpoint_node_ids || []);
const minX = geo.boundary.min_x, maxX = geo.boundary.max_x, minY = geo.boundary.min_y, maxY = geo.boundary.max_y;
function pos(n) {{ return [35 + (n.x-minX)/(Math.max(1,maxX-minX))*930, 720 - (n.y-minY)/(Math.max(1,maxY-minY))*660]; }}
function draw() {{
  const limit = Number(document.getElementById('step').value || 0); ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.strokeStyle='#eef2f7'; ctx.lineWidth=1;
  for(let x=minX;x<=maxX;x++) {{ const a=pos({{x,y:minY}}),b=pos({{x,y:maxY}});ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke(); }}
  for(let y=minY;y<=maxY;y++) {{ const a=pos({{x:minX,y}}),b=pos({{x:maxX,y}});ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke(); }}
  const expanded = new Set(events.slice(0,limit).map(e=>e.node_id));
  for(const id of expanded) {{ const n=nodes[id]; if(!n)continue; const p=pos(n);ctx.fillStyle='#2563ebaa';ctx.beginPath();ctx.arc(p[0],p[1],5,0,Math.PI*2);ctx.fill(); }}
  if(route.length>1) {{ ctx.strokeStyle='#dc2626';ctx.lineWidth=5;ctx.beginPath();route.forEach((id,i)=>{{const n=nodes[id];if(!n)return;const p=pos(n);if(i)ctx.lineTo(p[0],p[1]);else ctx.moveTo(p[0],p[1]);}});ctx.stroke(); }}
  for(const id of endpointIds) {{ const n=nodes[id];if(!n)continue;const p=pos(n);ctx.fillStyle='#f59e0b';ctx.beginPath();ctx.arc(p[0],p[1],8,0,Math.PI*2);ctx.fill();ctx.fillStyle='#111827';ctx.font='11px sans-serif';ctx.fillText(id,p[0]+8,p[1]-8); }}
  const current = events[Math.max(0,limit-1)]; if(current && nodes[current.node_id]) {{ const p=pos(nodes[current.node_id]);ctx.fillStyle='#f59e0b';ctx.strokeStyle='#92400e';ctx.lineWidth=2;ctx.beginPath();ctx.arc(p[0],p[1],10,0,Math.PI*2);ctx.stroke(); }}
  document.getElementById('stepLabel').value = limit + '/' + events.length;
}}
const slider=document.getElementById('step');slider.max=events.length;slider.value=events.length;slider.addEventListener('input',draw); 
document.getElementById('summary').innerHTML='<b>算法：</b>'+trace.method_name+' ('+trace.algorithm+')<br><b>状态：</b>'+trace.status+'<br><b>展开节点：</b>'+trace.expanded_nodes+'<br><b>最终路线节点：</b>'+route.length;
document.getElementById('legs').innerHTML=(trace.legs||[]).map((leg,i)=>'<tr><td>'+(i+1)+'</td><td>'+leg.status+'</td><td>'+leg.source_node_id+' → '+leg.target_node_id+'</td><td>'+((leg.expanded_node_ids||[]).length)+'</td></tr>').join('');
draw();
</script></body></html>""".format(title=title, payload=payload)


def render(map_path: Path, routes_path: Path, output_dir: Path) -> dict:
    road_map = load_road_map(map_path)
    payload = json.loads(routes_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for item in payload.get("results", []):
        task_id = item.get("task_id", "unknown-task")
        for method_result in item.get("methods", []):
            method_name = method_result.get("method_name", "unknown-method")
            trace = method_result.get("metadata", {}).get("search_trace")
            if trace is None:
                trace = {
                    "schema_version": "route_search_trace_v1",
                    "method_name": method_name,
                    "algorithm": "a_star" if method_name == "local_a_star" else "dijkstra",
                    "status": "missing",
                    "legs": [],
                    "expanded_nodes": 0,
                    "route_node_sequence": [],
                    "route_edge_sequence": [],
                }
            trace_id = "{0}__{1}".format(task_id, method_name)
            if item.get("scenario_id"):
                trace_id = "{0}__{1}".format(item["scenario_id"], method_name)
            record = {
                "schema_version": "traditional_trace_visual_record_v1",
                "trace_id": trace_id,
                "map_id": road_map.map_id,
                "task_id": task_id,
                "task_type": item.get("task_type"),
                "scenario_id": item.get("scenario_id"),
                "scenario_type": item.get("scenario_type"),
                "method_name": method_name,
                "status": method_result.get("status"),
                "failure_reason": method_result.get("failure_reason"),
                "trace": trace,
            }
            record["geometry"] = _trace_geometry(road_map, trace)
            stem = _safe_name(trace_id)
            json_file = stem + ".json"
            html_file = stem + ".html"
            (output_dir / json_file).write_text(
                json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            (output_dir / html_file).write_text(
                _record_html(record), encoding="utf-8"
            )
            records.append(
                {
                    "trace_id": trace_id,
                    "task_id": task_id,
                    "scenario_type": item.get("scenario_type"),
                    "method_name": method_name,
                    "status": method_result.get("status"),
                    "expanded_nodes": trace.get("expanded_nodes", 0),
                    "json_file": json_file,
                    "html_file": html_file,
                }
            )

    rows = []
    for record in records:
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><a href=\"{}\">HTML</a> · <a href=\"{}\">JSON</a></td></tr>".format(
                html.escape(record["trace_id"]),
                html.escape(record["method_name"]),
                html.escape(str(record["status"])),
                record["expanded_nodes"],
                html.escape(record["html_file"]),
                html.escape(record["json_file"]),
            )
        )
    index = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>传统算法轨迹索引</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;margin:28px;color:#1f2937}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left}}th{{background:#eef2ff}}</style>
<h1>传统算法检索与运行轨迹</h1><p>地图：{map_id}；记录数：{count}。每条记录均包含原始 JSON 轨迹和独立 HTML 回放。</p>
<table><thead><tr><th>记录</th><th>方法</th><th>状态</th><th>展开节点</th><th>查看</th></tr></thead><tbody>{rows}</tbody></table></html>""".format(
        map_id=html.escape(road_map.map_id), count=len(records), rows="".join(rows)
    )
    (output_dir / "index.html").write_text(index, encoding="utf-8")
    manifest = {
        "schema_version": "traditional_trace_visual_manifest_v1",
        "map_id": road_map.map_id,
        "routes_source": str(routes_path),
        "trace_count": len(records),
        "traces": records,
        "index_html": "index.html",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-json", required=True)
    parser.add_argument("--routes-json", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    manifest = render(
        Path(args.map_json), Path(args.routes_json), Path(args.output_dir)
    )
    print(
        json.dumps(
            {
                "output_dir": args.output_dir,
                "index_html": str(Path(args.output_dir) / "index.html"),
                "trace_count": manifest["trace_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
