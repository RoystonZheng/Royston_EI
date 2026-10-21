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
from route_experiment.llm_adapter import replay_move_sequence


def _node_xy(node_id: str):
    match = re.match(r"^node_(-?\d+)_(-?\d+)$", str(node_id or ""))
    if not match:
        return None
    return [int(match.group(1)), int(match.group(2))]


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


def _task_lookup(seed_dir: Path) -> dict:
    """Load task endpoints and formal-scenario constraints for legacy replay."""

    lookup = {}
    for path in seed_dir.glob("order_tasks_seed_*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for task in payload.get("tasks", []):
            lookup[task.get("task_id")] = dict(task)
    for path in seed_dir.glob("formal_scenarios_seed_*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for scenario in payload.get("scenarios", []):
            for task in scenario.get("tasks", []):
                task = dict(task)
                task["scenario_type"] = scenario.get("scenario_type")
                task["blocked_edge_ids"] = list(scenario.get("blocked_edge_ids", []))
                lookup[task.get("task_id")] = task
    return lookup


def _legacy_llm_trace(method_result: dict, task: dict, road_map: RoadMap) -> dict:
    """Rebuild a replay trace from stored LLM output without calling the API."""

    metadata = method_result.get("metadata") or {}
    output = method_result.get("raw_output") or {}
    move_sequence = (
        metadata.get("move_sequence")
        or metadata.get("recovered_partial_move_sequence")
        or output.get("move_sequence")
    )
    pickup = output.get("pickup_node_id") or task.get("pickup_node_id")
    dropoff = output.get("dropoff_node_id") or task.get("dropoff_node_id")
    if not move_sequence or not task.get("start_node_id"):
        return {
            "schema_version": "route_search_trace_v1",
            "method_name": method_result.get("method_name"),
            "algorithm": "llm",
            "status": method_result.get("status", "failed"),
            "legs": [
                {
                    "source_node_id": task.get("start_node_id"),
                    "target_node_id": pickup,
                    "status": "not_started",
                    "expanded_node_ids": [],
                    "expanded_events": [],
                },
                {
                    "source_node_id": pickup,
                    "target_node_id": dropoff,
                    "status": "not_started",
                    "expanded_node_ids": [],
                    "expanded_events": [],
                },
            ],
            "expanded_nodes": 0,
            "route_node_sequence": [],
            "route_edge_sequence": [],
            "attempted_move_sequence": "",
            "attempted_route_node_sequence": [],
            "validated_prefix_node_sequence": [],
            "move_violations": [],
            "move_count": 0,
            "failure_reason": method_result.get("failure_reason"),
            "failure_detail_zh": metadata.get("failure_detail_zh"),
        }
    replay = replay_move_sequence(
        road_map,
        task["start_node_id"],
        move_sequence,
        blocked_edge_ids=task.get("blocked_edge_ids", []),
    )
    return {
        "schema_version": "route_search_trace_v1",
        "method_name": method_result.get("method_name"),
        "algorithm": "llm",
        "status": "invalid" if replay["move_violations"] else method_result.get("status"),
        "legs": [
            {
                "source_node_id": task.get("start_node_id"),
                "target_node_id": pickup,
                "status": "invalid" if replay["move_violations"] else "completed",
                "expanded_node_ids": [],
                "expanded_events": [],
            },
            {
                "source_node_id": pickup,
                "target_node_id": dropoff,
                "status": "not_started" if replay["move_violations"] else "completed",
                "expanded_node_ids": [],
                "expanded_events": [],
            },
        ],
        "expanded_nodes": 0,
        "route_node_sequence": [],
        "route_edge_sequence": [],
        **replay,
        "failure_reason": method_result.get("failure_reason"),
        "failure_detail_zh": metadata.get("failure_detail_zh"),
        "proposal_recovered_partial_response": bool(
            metadata.get("recovered_partial_move_sequence")
        ),
        "trace_reconstructed_from_stored_model_output": True,
    }


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
    attempted_node_ids = list(trace.get("attempted_route_node_sequence", []))
    validated_prefix_node_ids = list(trace.get("validated_prefix_node_sequence", []))
    node_ids.extend(attempted_node_ids)
    node_ids.extend(validated_prefix_node_ids)
    for violation in trace.get("move_violations", []):
        node_ids.extend([violation.get("from_node_id"), violation.get("to_node_id")])
    unique_node_ids = list(dict.fromkeys(node_ids))
    nodes = []
    for node_id in unique_node_ids:
        if not node_id:
            continue
        node = node_lookup.get(node_id)
        xy = [node.x, node.y] if node else _node_xy(node_id)
        if xy is not None:
            nodes.append({"node_id": node_id, "x": xy[0], "y": xy[1]})
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
    geometry_nodes = {node["node_id"]: [node["x"], node["y"]] for node in nodes}
    move_violations = []
    for violation in trace.get("move_violations", []):
        move_violations.append({
            **violation,
            "from_xy": geometry_nodes.get(violation.get("from_node_id")),
            "to_xy": geometry_nodes.get(violation.get("to_node_id")),
        })
    context_edges = []
    violation_xy = [
        xy for item in move_violations for xy in (item.get("from_xy"), item.get("to_xy")) if xy
    ]
    if violation_xy:
        for edge in road_map.edges:
            source = node_lookup.get(edge.from_node)
            target = node_lookup.get(edge.to_node)
            if source is None or target is None:
                continue
            if any(
                max(abs(source.x - xy[0]), abs(source.y - xy[1]),
                    abs(target.x - xy[0]), abs(target.y - xy[1])) <= 2
                for xy in violation_xy
            ):
                context_edges.append({
                    "edge_id": edge.edge_id,
                    "from_node": edge.from_node,
                    "to_node": edge.to_node,
                    "one_way": edge.one_way,
                    "from_xy": [source.x, source.y],
                    "to_xy": [target.x, target.y],
                })
    return {
        "map_id": road_map.map_id,
        "map_size": road_map.size,
        "boundary": road_map.boundary,
        "nodes": nodes,
        "expanded_events": expanded_events,
        "route_node_ids": route_node_ids,
        "route_edges": route_edges,
        "attempted_route_node_ids": attempted_node_ids,
        "attempted_route_edge_ids": list(trace.get("attempted_route_edge_sequence", [])),
        "validated_prefix_node_ids": validated_prefix_node_ids,
        "move_violations": move_violations,
        "move_count": trace.get("move_count", len(trace.get("attempted_move_sequence", ""))),
        "first_violation_step": trace.get("first_violation_step"),
        "road_context_edges": context_edges,
        "endpoint_node_ids": list(dict.fromkeys(endpoints)),
    }


def _record_html(record: dict) -> str:
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    title = html.escape(record["trace_id"])
    if (record.get("trace") or {}).get("algorithm") == "llm":
        legend = (
            '<span><i class="dot blue"></i>搜索展开</span>'
            '<span><i class="dot orange"></i>当前节点</span>'
            '<span><i class="line-key teal"></i>合法前缀/通过校验的路线</span>'
            '<span><i class="line-key red-dashed"></i>违规动作（叉号）</span>'
            '<span><i class="line-key violet-dashed"></i>首个违规后的模型提议</span>'
        )
    else:
        legend = (
            '<span><i class="dot blue"></i>搜索展开</span>'
            '<span><i class="dot orange"></i>当前节点</span>'
            '<span><i class="line-key red-solid"></i>最终路线</span>'
        )
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>路线规划检索轨迹 - {title}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0;background:#f4f6fb;color:#1f2937}}
header{{padding:16px 22px;background:#172554;color:#fff}}
h1{{font-size:20px;margin:0 0 6px}} .meta{{font-size:13px;opacity:.9}}
.breadcrumb{{padding:10px 22px;background:#fff;border-bottom:1px solid #dbe3f0;font-size:13px}}.breadcrumb a{{color:#1d4ed8;margin-right:14px}}
main{{display:grid;grid-template-columns:minmax(620px,1fr) 360px;gap:14px;padding:14px}}
.card{{background:#fff;border:1px solid #dbe3f0;border-radius:10px;padding:14px;box-shadow:0 2px 8px #17255412}}
canvas{{width:100%;height:680px;border:1px solid #dbe3f0;border-radius:8px;background:#fbfdff}}
label{{font-size:13px}} input{{width:100%}} table{{width:100%;border-collapse:collapse;font-size:12px}}
td,th{{padding:5px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
.legend span{{display:inline-block;margin:0 12px 5px 0;font-size:12px}} .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}}
.line-key{{display:inline-block;width:18px;height:0;border-top:3px solid;margin:0 4px 3px 0;vertical-align:middle}}
.teal{{border-color:#0f766e}} .red-solid{{border-color:#dc2626}} .red-dashed{{border-color:#dc2626;border-top-style:dashed}} .violet-dashed{{border-color:#7c3aed;border-top-style:dashed}}
.blue{{background:#2563eb}} .red{{background:#dc2626}} .orange{{background:#f59e0b}}
.violet{{background:#7c3aed}} .violation-list{{font-size:12px;line-height:1.6;color:#7f1d1d;padding-left:20px}}
.violation-list button{{border:0;background:transparent;color:#b91c1c;text-decoration:underline;cursor:pointer;padding:0;font:inherit}}
</style></head><body>
<header><h1>路线规划检索与运行轨迹</h1><div class="meta">{title}</div></header>
<nav class="breadcrumb"><a href="../../index.html">返回 seed 总览</a><a href="../../map_preview.html">查看原始地图</a><a href="../../../../index.html">返回 10 个种子索引</a></nav>
<main><section class="card"><canvas id="map" width="1000" height="760"></canvas>
<p><label>回放进度：<output id="stepLabel">0</output></label>
<input id="step" type="range" min="0" value="0"></p>
<div class="legend">{legend}</div></section>
<section class="card"><div id="summary"></div><div id="violationPanel" hidden><h3>路线限制违规位置</h3><p>红色虚线和叉号标记违规移动；模型提议的后续路线仅按方向序列回放，不代表可通行或实际执行。</p><ol id="violations" class="violation-list"></ol></div><h3>每段检索</h3><table><thead><tr><th>段</th><th>状态</th><th>起点 → 终点</th><th>展开</th></tr></thead><tbody id="legs"></tbody></table></section></main>
<script>
const DATA = {payload};
const trace = DATA.trace || {{}}; const geo = DATA.geometry || {{}};
const canvas = document.getElementById('map'); const ctx = canvas.getContext('2d');
const nodes = Object.fromEntries((geo.nodes || []).map(n => [n.node_id, n]));
const events = geo.expanded_events || []; const route = geo.route_node_ids || [];
const attempted = geo.attempted_route_node_ids || []; const violations = geo.move_violations || [];
const endpointIds = new Set(geo.endpoint_node_ids || []);
const minX = geo.boundary.min_x, maxX = geo.boundary.max_x, minY = geo.boundary.min_y, maxY = geo.boundary.max_y;
function pos(n) {{ return [35 + (n.x-minX)/(Math.max(1,maxX-minX))*930, 720 - (n.y-minY)/(Math.max(1,maxY-minY))*660]; }}
function arrow(a,b,color) {{ const dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy);if(!len)return;const ux=dx/len,uy=dy/len,mx=a[0]+dx*.65,my=a[1]+dy*.65;ctx.fillStyle=color;ctx.beginPath();ctx.moveTo(mx+ux*5,my+uy*5);ctx.lineTo(mx-ux*4+uy*4,my-uy*4-ux*4);ctx.lineTo(mx-ux*4-uy*4,my-uy*4+ux*4);ctx.closePath();ctx.fill(); }}
function draw() {{
  const limit = Number(document.getElementById('step').value || 0); ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.strokeStyle='#eef2f7'; ctx.lineWidth=1;
  for(let x=minX;x<=maxX;x++) {{ const a=pos({{x,y:minY}}),b=pos({{x,y:maxY}});ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke(); }}
  for(let y=minY;y<=maxY;y++) {{ const a=pos({{x:minX,y}}),b=pos({{x:maxX,y}});ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke(); }}
  for(const e of geo.road_context_edges||[]) {{ const a=pos(nodes[e.from_node]||{{x:e.from_xy[0],y:e.from_xy[1]}}),b=pos(nodes[e.to_node]||{{x:e.to_xy[0],y:e.to_xy[1]}});ctx.strokeStyle='#94a3b8';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke();if(e.one_way)arrow(a,b,'#64748b'); }}
  const expanded = new Set(events.slice(0,limit).map(e=>e.node_id));
  for(const id of expanded) {{ const n=nodes[id]; if(!n)continue; const p=pos(n);ctx.fillStyle='#2563ebaa';ctx.beginPath();ctx.arc(p[0],p[1],5,0,Math.PI*2);ctx.fill(); }}
  if(route.length>1) {{ ctx.strokeStyle='#dc2626';ctx.lineWidth=5;ctx.beginPath();route.forEach((id,i)=>{{const n=nodes[id];if(!n)return;const p=pos(n);if(i)ctx.lineTo(p[0],p[1]);else ctx.moveTo(p[0],p[1]);}});ctx.stroke(); }}
  if(attempted.length>1) {{ const n=Math.min(limit,attempted.length-1);const violationByStep=new Map(violations.map(v=>[Number(v.step),v]));for(let i=1;i<=n;i++){{const aNode=nodes[attempted[i-1]],bNode=nodes[attempted[i]];if(!aNode||!bNode)continue;const a=pos(aNode),b=pos(bNode),v=violationByStep.get(i);ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);if(v){{ctx.strokeStyle='#dc2626';ctx.lineWidth=4;ctx.setLineDash([5,4]);}}else if(geo.first_violation_step&&i>geo.first_violation_step){{ctx.strokeStyle='#7c3aed';ctx.lineWidth=3;ctx.setLineDash([5,5]);}}else{{ctx.strokeStyle='#0f766e';ctx.lineWidth=3;ctx.setLineDash([]);}}ctx.stroke();ctx.setLineDash([]);if(v){{arrow(a,b,'#dc2626');ctx.strokeStyle='#b91c1c';ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(b[0]-6,b[1]-6);ctx.lineTo(b[0]+6,b[1]+6);ctx.moveTo(b[0]+6,b[1]-6);ctx.lineTo(b[0]-6,b[1]+6);ctx.stroke();ctx.fillStyle='#991b1b';ctx.font='bold 12px sans-serif';ctx.fillText('违规第'+i+'步',b[0]+8,b[1]+15);}} }} }}
  for(const id of endpointIds) {{ const n=nodes[id];if(!n)continue;const p=pos(n);ctx.fillStyle='#f59e0b';ctx.beginPath();ctx.arc(p[0],p[1],8,0,Math.PI*2);ctx.fill();ctx.fillStyle='#111827';ctx.font='11px sans-serif';ctx.fillText(id,p[0]+8,p[1]-8); }}
  const currentId=attempted.length?attempted[Math.min(limit,attempted.length-1)]:(events[Math.max(0,limit-1)]||{{}}).node_id; if(currentId&&nodes[currentId]) {{ const p=pos(nodes[currentId]);ctx.fillStyle='#f59e0b';ctx.strokeStyle='#92400e';ctx.lineWidth=2;ctx.beginPath();ctx.arc(p[0],p[1],10,0,Math.PI*2);ctx.stroke(); }}
  document.getElementById('stepLabel').value = limit + '/' + Math.max(events.length,geo.move_count||0);
}}
const slider=document.getElementById('step');slider.max=Math.max(events.length,geo.move_count||0);slider.value=slider.max;slider.addEventListener('input',draw); 
    document.getElementById('summary').innerHTML='<b>算法：</b>'+trace.method_name+' ('+trace.algorithm+')<br><b>状态：</b>'+trace.status+'<br><b>模型响应是否收到：</b>'+(DATA.llm_response_received===true?'是':DATA.llm_response_received===false?'否':'—')+'<br><b>展开节点：</b>'+(trace.algorithm==='llm'?'—':trace.expanded_nodes)+'<br><b>合法路线节点：</b>'+route.length+(attempted.length?'<br><b>模型提议步数：</b>'+geo.move_count+'，路线校验违规 '+violations.length+' 处':'')+(DATA.failure_detail_zh?'<br><b>失败原因：</b>'+DATA.failure_detail_zh:'')+(DATA.failure_reason?'<br><b>原始失败码：</b>'+DATA.failure_reason:'');
const energy=DATA.proposal_energy_audit||{{}};
if(energy.status==='estimated'){{document.getElementById('summary').innerHTML+='<br><b>提议路线耗电估算：</b>'+Number(energy.route_energy_estimate_kwh).toFixed(6)+' kWh<br><b>合法前缀耗电估算：</b>'+Number(energy.validated_prefix_energy_estimate_kwh).toFixed(6)+' kWh';}}
else if(energy.status==='partial_estimated'){{document.getElementById('summary').innerHTML+='<br><b>截断响应可恢复路线前缀耗电估算：</b>'+Number(energy.route_energy_estimate_kwh).toFixed(6)+' kWh；不是完整路线';}}
else if(energy.status==='no_route_proposed'){{document.getElementById('summary').innerHTML+='<br><b>提议路线耗电：</b>未提出或无法恢复路线步骤，因此不估算';}}
if(energy.interpretation){{document.getElementById('summary').innerHTML+='<br><small>'+energy.interpretation+'</small>';}}
if(violations.length){{document.getElementById('violationPanel').hidden=false;const labels={{one_way_direction:'单行方向相反',missing_directed_edge:'该方向没有道路边',outside_boundary:'越过地图边界',blocked_edge:'经过场景封闭边'}};document.getElementById('violations').innerHTML=violations.map(v=>'<li><button data-step="'+v.step+'">第 '+v.step+' 步 '+v.move+'：'+v.from_node_id+' → '+v.to_node_id+'</button> · '+(labels[v.kind]||v.kind)+(v.reverse_edge_id?'（仅允许 '+v.to_node_id+' → '+v.from_node_id+'）':'')+'</li>').join('');document.querySelectorAll('#violations button').forEach(b=>b.addEventListener('click',()=>{{slider.value=b.dataset.step;draw();}}));}}
document.getElementById('legs').innerHTML=(trace.legs||[]).map((leg,i)=>'<tr><td>'+(i+1)+'</td><td>'+leg.status+'</td><td>'+leg.source_node_id+' → '+leg.target_node_id+'</td><td>'+((leg.expanded_node_ids||[]).length)+'</td></tr>').join('');
draw();
</script></body></html>""".format(title=title, payload=payload, legend=legend)


def render(map_path: Path, routes_path: Path, output_dir: Path) -> dict:
    road_map = load_road_map(map_path)
    payload = json.loads(routes_path.read_text(encoding="utf-8"))
    tasks = _task_lookup(routes_path.parent)
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for item in payload.get("results", []):
        task_id = item.get("task_id", "unknown-task")
        task = tasks.get(task_id, {})
        for method_result in item.get("methods", []):
            method_name = method_result.get("method_name", "unknown-method")
            trace = method_result.get("metadata", {}).get("search_trace")
            if trace is None and method_name == "pure_llm_deepseek":
                trace = _legacy_llm_trace(method_result, task, road_map)
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
                "schema_version": "route_trace_visual_record_v1",
                "trace_id": trace_id,
                "map_id": road_map.map_id,
                "task_id": task_id,
                "task_type": item.get("task_type"),
                "scenario_id": item.get("scenario_id"),
                "scenario_type": item.get("scenario_type"),
                "method_name": method_name,
                "status": method_result.get("status"),
                "failure_reason": method_result.get("failure_reason"),
                "failure_detail_zh": (method_result.get("metadata") or {}).get("failure_detail_zh"),
                "llm_response_received": (method_result.get("metadata") or {}).get("llm_response_received"),
                "proposal_energy_audit": (method_result.get("metadata") or {}).get("proposal_energy_audit"),
                "validation": method_result.get("validation"),
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
                    "failure_detail_zh": (method_result.get("metadata") or {}).get("failure_detail_zh"),
                    "llm_response_received": (method_result.get("metadata") or {}).get("llm_response_received"),
                    "proposal_energy_audit": (method_result.get("metadata") or {}).get("proposal_energy_audit"),
                    "expanded_nodes": trace.get("expanded_nodes", 0),
                    "violation_count": len(trace.get("move_violations", [])),
                    "json_file": json_file,
                    "html_file": html_file,
                }
            )

    rows = []
    for record in records:
        expanded_display = "—" if record["method_name"] == "pure_llm_deepseek" else record["expanded_nodes"]
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><a href=\"{}\">HTML</a> · <a href=\"{}\">JSON</a></td></tr>".format(
                html.escape(record["trace_id"]),
                html.escape(record["method_name"]),
                html.escape(str(record["status"])),
                expanded_display,
                record["violation_count"],
                html.escape(record["html_file"]),
                html.escape(record["json_file"]),
            )
        )
    index = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>路线规划轨迹索引</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;margin:28px;color:#1f2937}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left}}th{{background:#eef2ff}}nav{{margin:12px 0 18px}}nav a{{color:#1d4ed8;margin-right:14px}}</style>
<h1>路线规划检索与运行轨迹</h1><p>地图：{map_id}；记录数：{count}。每条记录均包含原始 JSON 轨迹和独立 HTML 回放。传统算法的“展开节点”表示图搜索展开量；纯 LLM 没有同类图搜索记录，因此显示为“—”。模型输出的非法路线会保留方向序列、回放合法前缀并标出违规步骤。</p>
<nav><a href="../../index.html">返回 seed 总览</a><a href="../../map_preview.html">查看原始地图</a><a href="../../../../index.html">返回 10 个种子索引</a></nav>
<table><thead><tr><th>记录</th><th>方法</th><th>状态</th><th>展开节点</th><th>违规步数</th><th>查看</th></tr></thead><tbody>{rows}</tbody></table></html>""".format(
        map_id=html.escape(road_map.map_id), count=len(records), rows="".join(rows)
    )
    (output_dir / "index.html").write_text(index, encoding="utf-8")
    manifest = {
        "schema_version": "route_trace_visual_manifest_v1",
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
