#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render one seed-level map and algorithm-result dashboard.

The dashboard keeps the original map visible while a result table selects one
task-method pair.  It is intentionally standalone HTML so it can be opened
from the generated output directory without a web server.
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _node_xy(node_id):
    match = re.match(r"^node_(-?\d+)_(-?\d+)$", str(node_id or ""))
    if not match:
        return None
    return [int(match.group(1)), int(match.group(2))]


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_links(seed_dir):
    links = {}
    for scope, folder in (("orders", "orders"), ("formal", "formal_scenarios")):
        manifest_path = seed_dir / "visualizations" / folder / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = _read_json(manifest_path)
        for item in manifest.get("traces", []):
            key = (scope, item.get("task_id"), item.get("method_name"))
            link = {
                "html": "visualizations/{0}/{1}".format(folder, item["html_file"]),
                "json": "visualizations/{0}/{1}".format(folder, item["json_file"]),
            }
            if item.get("method_name") == "pure_llm_deepseek":
                trace_path = manifest_path.parent / item["json_file"]
                if trace_path.exists():
                    link["trace"] = _read_json(trace_path).get("trace")
            links[key] = link
    return links


def _task_lookup(seed_dir):
    lookup = {}
    order_payload = _read_json(next(seed_dir.glob("order_tasks_seed_*.json")))
    for task in order_payload.get("tasks", []):
        lookup[("orders", task["task_id"])] = task
    formal_payload = _read_json(next(seed_dir.glob("formal_scenarios_seed_*.json")))
    for scenario in formal_payload.get("scenarios", []):
        for task in scenario.get("tasks", []):
            task = dict(task)
            task["scenario_type"] = scenario.get("scenario_type")
            lookup[("formal", task["task_id"])] = task
    return lookup


def _trace_nodes(trace):
    route = list(trace.get("route_node_sequence", []))
    expanded = []
    for leg in trace.get("legs", []):
        expanded.extend(leg.get("expanded_node_ids", []))
        if not route:
            route.extend(leg.get("route_node_sequence", []))
    return list(dict.fromkeys(route)), list(dict.fromkeys(expanded))


def _endpoint_ids(task, trace):
    legs = trace.get("legs", [])
    leg_start = legs[0].get("source_node_id") if legs else None
    leg_pickup = legs[0].get("target_node_id") if legs else None
    leg_dropoff = legs[-1].get("target_node_id") if legs else None
    return {
        "start_node_id": task.get("start_node_id") or leg_start,
        "pickup_node_id": task.get("pickup_node_id") or leg_pickup,
        "dropoff_node_id": task.get("dropoff_node_id") or leg_dropoff,
    }


def _result_record(scope, item, method, task, links):
    metadata = method.get("metadata") or {}
    trace = metadata.get("search_trace") or {}
    evaluation = method.get("evaluation") or {}
    resources = method.get("resources") or {}
    money = method.get("monetary_cost") or {}
    validation = method.get("validation") or {}
    route_nodes, expanded_nodes = _trace_nodes(trace)
    endpoints = _endpoint_ids(task, trace)
    link = links.get((scope, item.get("task_id"), method.get("method_name")), {})
    if not trace and link.get("trace"):
        trace = link["trace"]
        route_nodes, expanded_nodes = _trace_nodes(trace)
        endpoints = _endpoint_ids(task, trace)
    return {
        "scope": scope,
        "task_id": item.get("task_id"),
        "scenario_id": item.get("scenario_id"),
        "scenario_type": item.get("scenario_type") or task.get("scenario_type"),
        "task_type": item.get("task_type") or task.get("task_type"),
        "method_name": method.get("method_name"),
        "algorithm": trace.get("algorithm"),
        "status": method.get("status"),
        "failure_reason": method.get("failure_reason"),
        "failure_reason_label_zh": metadata.get("failure_reason_label_zh"),
        "failure_detail_zh": metadata.get("failure_detail_zh"),
        "llm_response_received": metadata.get("llm_response_received"),
        "pickup_text": task.get("pickup_text"),
        "dropoff_text": task.get("dropoff_text"),
        **endpoints,
        "compute_time_ms": metadata.get("compute_time_ms"),
        "llm_calls": resources.get("llm_calls"),
        "llm_input_tokens": resources.get("input_tokens"),
        "llm_output_tokens": resources.get("output_tokens"),
        "llm_latency_ms": resources.get("llm_latency_ms"),
        "qwen_intent_latency_ms": metadata.get("qwen_intent_latency_ms"),
        "codex_api_equivalent_usd": (
            metadata.get("codex_usage_cost_estimate") or {}
        ).get("api_equivalent_usd"),
        "account_billing_status": metadata.get("account_billing_status"),
        "expanded_nodes": (
            None
            if trace.get("algorithm") == "llm" or method.get("method_name") == "pure_llm_deepseek"
            else metadata.get("expanded_nodes", trace.get("expanded_nodes", 0))
        ),
        "route_distance_m": evaluation.get("route_distance_m"),
        "travel_time_sec": evaluation.get("total_travel_time_sec"),
        "energy_kwh": resources.get("energy_kwh", evaluation.get("energy_total_kwh")),
        "proposal_energy_status": (metadata.get("proposal_energy_audit") or {}).get("status"),
        "proposal_energy_estimate_kwh": (metadata.get("proposal_energy_audit") or {}).get("route_energy_estimate_kwh"),
        "valid_prefix_energy_estimate_kwh": (metadata.get("proposal_energy_audit") or {}).get("validated_prefix_energy_estimate_kwh"),
        "proposal_energy_interpretation": (metadata.get("proposal_energy_audit") or {}).get("interpretation"),
        "electricity_cost": money.get("electricity_cost_amount"),
        "compute_cost": money.get("compute_cost_amount"),
        "api_cost": money.get("api_cost_amount"),
        "total_cost": money.get("total_monetary_cost"),
        "cost_status": metadata.get("cost_status"),
        "validation_status": validation.get("status"),
        "validation_errors": validation.get("errors", []),
        "route_node_ids": route_nodes,
        "attempted_route_node_ids": trace.get("attempted_route_node_sequence", []),
        "validated_prefix_node_ids": trace.get("validated_prefix_node_sequence", []),
        "move_violations": trace.get("move_violations", []),
        "expanded_node_ids": expanded_nodes,
        "html_file": link.get("html"),
        "json_file": link.get("json"),
    }


def _map_payload(map_payload):
    edges = []
    for edge in map_payload.get("edges", []):
        start = _node_xy(edge.get("from_node"))
        end = _node_xy(edge.get("to_node"))
        if start is None or end is None:
            continue
        edges.append([start[0], start[1], end[0], end[1], 1 if edge.get("one_way") else 0])
    features = []
    for feature in map_payload.get("roadside_features", []):
        xy = _node_xy(feature.get("node_id"))
        if xy is None:
            continue
        features.append({
            "x": xy[0], "y": xy[1], "label": feature.get("label"),
            "feature_type": feature.get("feature_type"),
            "side_of_road": feature.get("side_of_road"),
            "orientation": feature.get("orientation"),
        })
    return {
        "map_id": map_payload.get("map_id"),
        "size": map_payload.get("size"),
        "boundary": map_payload.get("boundary"),
        "edges": edges,
        "features": features,
        "node_count": len(map_payload.get("nodes", [])),
        "edge_count": len(map_payload.get("edges", [])),
    }


def _map_preview_svg(map_payload: dict) -> str:
    """Render the source road network as a static, standalone SVG image."""

    boundary = map_payload.get("boundary") or {}
    min_x = int(boundary.get("min_x", 0))
    max_x = int(boundary.get("max_x", int(map_payload.get("size", 1)) - 1))
    min_y = int(boundary.get("min_y", 0))
    max_y = int(boundary.get("max_y", int(map_payload.get("size", 1)) - 1))
    width, height, pad = 1100, 940, 55
    scale = min(
        (width - 2 * pad) / max(1, max_x - min_x),
        (height - 2 * pad) / max(1, max_y - min_y),
    )

    def pos(x, y):
        return (pad + (x - min_x) * scale, height - pad - (y - min_y) * scale)

    segments = {}
    for edge in map_payload.get("edges", []):
        if isinstance(edge, dict):
            start_xy = _node_xy(edge.get("from_node"))
            end_xy = _node_xy(edge.get("to_node"))
            one_way = bool(edge.get("one_way"))
            if start_xy is None or end_xy is None:
                continue
            start = (start_xy[0], start_xy[1])
            end = (end_xy[0], end_xy[1])
        else:
            start = (int(edge[0]), int(edge[1]))
            end = (int(edge[2]), int(edge[3]))
            one_way = bool(edge[4])
        key = tuple(sorted((start, end)))
        current = segments.get(key)
        if current is None or (one_way and not current[4]):
            segments[key] = [start[0], start[1], end[0], end[1], one_way]

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {1}" role="img" aria-labelledby="map-title map-desc">'.format(width, height),
        '<title id="map-title">{}</title>'.format(html.escape(map_payload.get("map_id", "原始地图"))),
        '<desc id="map-desc">未叠加任何订单路线的道路网络、单向道路和道路特征。</desc>',
        '<defs><marker id="one-way-arrow" markerWidth="5" markerHeight="5" refX="4" refY="2.5" orient="auto"><path d="M0,0 L5,2.5 L0,5 z" fill="#64748b"/></marker></defs>',
        '<rect x="0" y="0" width="100%" height="100%" fill="#fbfdff"/>',
    ]
    for x in range(min_x, max_x + 1, 10):
        px, _ = pos(x, min_y)
        parts.append('<line x1="{0:.1f}" y1="{1}" x2="{0:.1f}" y2="{2}" stroke="#e8edf4" stroke-width="1"/>'.format(px, pad, height - pad))
        parts.append('<text x="{:.1f}" y="{}" text-anchor="middle" font-size="11" fill="#64748b">{}</text>'.format(px, height - 18, x))
    for y in range(min_y, max_y + 1, 10):
        _, py = pos(min_x, y)
        parts.append('<line x1="{0}" y1="{1:.1f}" x2="{2}" y2="{1:.1f}" stroke="#e8edf4" stroke-width="1"/>'.format(pad, py, width - pad))
        parts.append('<text x="22" y="{:.1f}" text-anchor="middle" font-size="11" fill="#64748b">{}</text>'.format(py + 4, y))
    parts.append('<rect x="{0}" y="{1}" width="{2}" height="{3}" fill="none" stroke="#334155" stroke-width="2" stroke-dasharray="7 5"/>'.format(pad, pad, width - 2 * pad, height - 2 * pad))
    for x1, y1, x2, y2, one_way in segments.values():
        ax, ay = pos(x1, y1)
        bx, by = pos(x2, y2)
        marker = ' marker-end="url(#one-way-arrow)"' if one_way else ""
        stroke = "#64748b" if one_way else "#aab6c7"
        stroke_width = "2" if one_way else "1.5"
        parts.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" stroke-width="{}"{} />'.format(ax, ay, bx, by, stroke, stroke_width, marker))
    for feature in map_payload.get("roadside_features", []):
        xy = _node_xy(feature.get("node_id"))
        if xy is None:
            continue
        px, py = pos(xy[0], xy[1])
        label = html.escape(str(feature.get("label") or feature.get("feature_type") or "道路特征"))
        parts.append('<circle cx="{:.1f}" cy="{:.1f}" r="5.5" fill="#8b5cf6" stroke="white" stroke-width="1.5"><title>{}</title></circle>'.format(px, py, label))
        parts.append('<text x="{:.1f}" y="{:.1f}" font-size="11" fill="#6042a2">{}</text>'.format(px + 8, py - 6, label))
    parts.append("</svg>")
    return "\n".join(parts)


def _map_preview_html(map_payload: dict) -> str:
    map_id = html.escape(str(map_payload.get("map_id", "原始地图")))
    size = int(map_payload.get("size", 0) or 0)
    node_count = len(map_payload.get("nodes", []))
    edge_count = len(map_payload.get("edges", []))
    feature_count = len(map_payload.get("roadside_features", []))
    seed_match = re.search(r"seed-(\d+)$", str(map_payload.get("map_id", "")))
    seed = seed_match.group(1) if seed_match else "?"
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>seed {seed} 原始地图</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0;background:#f4f7fb;color:#172033}}header{{padding:18px 24px;background:#14213d;color:white}}h1{{margin:0 0 7px;font-size:23px}}header p{{margin:0;color:#dbe7ff}}nav{{margin-top:12px}}nav a{{color:#bfdbfe;margin-right:18px}}main{{padding:16px 22px}}.meta,.legend{{background:white;border:1px solid #dbe3ef;border-radius:8px;padding:10px 14px;margin-bottom:12px}}img{{display:block;width:min(100%,1250px);height:auto;margin:auto;background:white;border:1px solid #dbe3ef;border-radius:8px}}</style></head>
<body><header><h1>seed {seed} 原始地图</h1><p>{map_id} · {size}×{size} · {nodes} 个节点 · {edges} 条有向边 · {features} 个道路特征</p><nav><a href="index.html">返回本 seed 总览</a><a href="../../index.html">返回 10 个种子索引</a></nav></header>
<main><div class="meta">此图只显示该种子的基础道路网络、单向方向和道路特征，不叠加订单路线。</div><div class="legend">灰线：道路　箭头：单向道路　紫色标记：路牌/电线杆等道路特征　虚线框：地图边界</div><img src="map_preview.svg" alt="seed {seed} 的原始道路地图"></main></body></html>""".format(
        seed=seed, map_id=map_id, size=size, nodes=node_count, edges=edge_count, features=feature_count
    )


def build_dashboard_payload(seed_dir: Path) -> dict:
    map_payload = _read_json(next(seed_dir.glob("map_*_seed_*.json")))
    task_lookup = _task_lookup(seed_dir)
    links = _manifest_links(seed_dir)
    records = []
    methods = set()
    route_files = [
        ("orders", next(seed_dir.glob("*order_routes_costs_seed_*.json"))),
        ("formal", next(seed_dir.glob("*formal_routes_costs_seed_*.json"))),
    ]
    for scope, route_path in route_files:
        payload = _read_json(route_path)
        for item in payload.get("results", []):
            task = task_lookup.get((scope, item.get("task_id")), {})
            for method in item.get("methods", []):
                if method.get("method_name"):
                    methods.add(method.get("method_name"))
                records.append(_result_record(scope, item, method, task, links))
    seed_match = re.search(r"seed_(\d+)$", seed_dir.name)
    return {
        "seed": int(seed_match.group(1)) if seed_match else seed_dir.name,
        "methods": sorted(methods),
        "map": _map_payload(map_payload),
        "records": records,
    }


def _html(payload):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("</", "<\\/")
    return r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>种子路线实验总览</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#172033;background:#f4f7fb}
body{margin:0}.top{padding:20px 26px;background:#14213d;color:white}.top h1{margin:0 0 7px;font-size:24px}.top p{margin:0;color:#dbe7ff}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-top:15px}.stat{background:#fff;color:#172033;border-radius:9px;padding:9px 12px;min-width:120px;box-shadow:0 1px 4px #10254b20}.stat b{display:block;font-size:18px}.stat span{font-size:12px;color:#68758a}
.toolbar{margin:16px 22px 0;display:flex;gap:10px;flex-wrap:wrap;align-items:center}.toolbar label{font-size:13px}.toolbar input,.toolbar select{padding:7px;border:1px solid #cbd5e1;border-radius:6px;background:white}
.page-nav{margin:14px 22px 0;padding:10px 14px;background:#fff;border:1px solid #dbe3ef;border-radius:8px;font-size:14px}.page-nav a{color:#1d4ed8;margin-right:14px}
.layout{display:grid;grid-template-columns:minmax(560px,1.1fr) minmax(500px,1fr);gap:16px;padding:16px 22px}.card{background:white;border:1px solid #dbe3ef;border-radius:10px;padding:14px;box-shadow:0 2px 9px #10254b12}.card h2{font-size:17px;margin:0 0 10px}
canvas{width:100%;height:620px;border:1px solid #dbe3ef;border-radius:8px;background:#fbfdff}.legend{font-size:12px;color:#536174;margin-top:8px}.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin:0 4px 0 10px}.road{background:#aab6c7}.route{background:#dc2626}.expanded{background:#2563eb}.feature{background:#8b5cf6}.endpoint{background:#f59e0b}.proposed{background:#7c3aed}.violation{background:#dc2626}
.table-wrap{max-height:620px;overflow:auto;border:1px solid #e2e8f0;border-radius:7px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border-bottom:1px solid #e7edf5;padding:7px 6px;text-align:left;vertical-align:top;white-space:nowrap}th{position:sticky;top:0;background:#eff4ff;z-index:1}tr.selected{background:#fff3cd}tr:hover{background:#f8fbff;cursor:pointer}.muted{color:#69778d}.pill{padding:2px 6px;border-radius:10px;background:#e7eefb}.ok{color:#087443}.bad{color:#b42318}.reason-cell{white-space:normal;min-width:190px;max-width:320px}.detail{font-size:13px;line-height:1.55;background:#f8fafc;border-radius:7px;padding:10px;margin-bottom:10px}.detail b{color:#334155}.detail a{color:#1d4ed8}.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 12px}.empty{padding:30px;text-align:center;color:#748197}
@media(max-width:1100px){.layout{grid-template-columns:1fr}canvas{height:520px}}
</style></head><body>
<header class="top"><h1>路线层实验：种子总览</h1><p id="subtitle"></p><div class="stats" id="stats"></div></header>
<nav class="page-nav">__SEED_NAV__</nav>
<div class="toolbar"><label>数据范围 <select id="scope"><option value="all">全部</option><option value="orders">订单</option><option value="formal">正式场景</option></select></label><label>算法 <select id="method"><option value="all">全部</option></select></label><label>状态 <select id="status"><option value="all">全部</option><option value="completed">completed</option><option value="failed">failed</option></select></label><label>搜索 <input id="query" placeholder="任务、节点或场景"></label></div>
<main class="layout"><section class="card"><h2>原地图与当前结果</h2><canvas id="map" width="1000" height="760"></canvas><div class="legend"><i class="road"></i>原道路 <i class="road"></i>单向道路箭头 <i class="route"></i>合法最终路线 <i class="expanded"></i>搜索展开 <i class="feature"></i>路牌/电线杆等道路特征 <i class="endpoint"></i>起点/上车点/终点 <i class="proposed"></i>模型提议路线（虚线） <i class="violation"></i>违反限制的位置</div></section>
<section class="card"><h2>对应算法结果</h2><div id="detail" class="detail"><div class="empty">点击下面任意结果查看路线和详细字段</div></div><div class="table-wrap"><table><thead><tr><th>范围/任务</th><th>算法</th><th>状态</th><th>失败原因</th><th>本地时间 ms</th><th>模型耗时 ms</th><th>输入/输出 token</th><th>电力 kWh</th><th>起点 → 上车点 → 终点</th><th>距离 m</th><th>行驶 s</th><th>展开</th><th>费用 CNY</th><th>入口</th></tr></thead><tbody id="results"></tbody></table></div></section></main>
<script>
const DATA=__DATA__; const records=DATA.records||[]; let selected=0;
const $=id=>document.getElementById(id); const fmt=(v,d=2)=>v===null||v===undefined||v===''?'—':Number.isFinite(Number(v))?Number(v).toFixed(d):String(v); const fmtTokenPair=(a,b)=>a==null&&b==null?'—':[a,b].map(v=>v==null?'—':Number(v).toLocaleString('zh-CN')).join(' / '); const fmtApiUsd=v=>v==null?'—':'$'+Number(v).toFixed(6)+'（估算，非账单）'; const fmtBillingStatus=v=>v==='not_reported_by_codex_cli'?'Codex CLI 未报告账单':v||'—'; const fmtEnergy=r=>{if(r.status==='failed'&&r.method_name==='pure_llm_deepseek'){if(r.proposal_energy_estimate_kwh!==null&&r.proposal_energy_estimate_kwh!==undefined)return (r.proposal_energy_status==='partial_estimated'?'部分估算 ':'提议估算 ')+fmt(r.proposal_energy_estimate_kwh,4);return r.llm_response_received===false?'未收到响应':'未提出路线'}return r.status==='failed'?'—':fmt(r.energy_kwh,4)}; const fmtCost=r=>r.cost_status==='not_evaluated'?'—':fmt(r.total_cost,4); const fmtComponent=(r,v)=>r.cost_status==='not_evaluated'?'—':fmt(v,4);
const violationNames={one_way_direction:'单行方向相反',missing_directed_edge:'该方向没有道路边',outside_boundary:'越过地图边界',blocked_edge:'经过场景封闭边'};
$('subtitle').textContent=`种子 ${DATA.seed} · ${DATA.map.map_id} · 方法：${(DATA.methods||[]).join('、')||'—'} · 原地图与 ${records.length} 条任务—算法结果联动展示`;
$('stats').innerHTML=[['seed','种子'],['node_count','地图节点'],['edge_count','道路边'],['features.length','道路特征'],['records.length','结果记录']].map(([k,label])=>{let v=k.includes('.')?DATA.map.features.length: k==='records.length'?records.length:DATA.map[k]??DATA.seed;return `<div class="stat"><b>${v}</b><span>${label}</span></div>`}).join('');
const methods=[...new Set(records.map(r=>r.method_name))]; $('method').innerHTML='<option value="all">全部</option>'+methods.map(x=>`<option>${x}</option>`).join('');
function nodeXY(id){const m=/^node_(-?\d+)_(-?\d+)$/.exec(id||'');return m?[Number(m[1]),Number(m[2])]:null}
const B=DATA.map.boundary; function pos(x,y){return [35+(x-B.min_x)/Math.max(1,B.max_x-B.min_x)*930,720-(y-B.min_y)/Math.max(1,B.max_y-B.min_y)*660]}
function arrow(ctx,a,b){const dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy);if(len<10)return;const ux=dx/len,uy=dy/len,mx=a[0]+dx*.62,my=a[1]+dy*.62;ctx.beginPath();ctx.moveTo(mx,my);ctx.lineTo(mx-ux*8+uy*4,my-uy*8-ux*4);ctx.lineTo(mx-ux*8-uy*4,my-uy*8+ux*4);ctx.closePath();ctx.fill()}
function strokeNodePath(ctx,ids,color,width,dash){if(!ids||ids.length<2)return;ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash(dash||[]);ctx.beginPath();ids.forEach((id,i)=>{const xy=nodeXY(id);if(!xy)return;const p=pos(xy[0],xy[1]);if(i)ctx.lineTo(p[0],p[1]);else ctx.moveTo(p[0],p[1])});ctx.stroke();ctx.setLineDash([])}
function draw(){const c=$('map'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);ctx.lineWidth=1;ctx.strokeStyle='#d8e0eb';for(const e of DATA.map.edges){const a=pos(e[0],e[1]),b=pos(e[2],e[3]);ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke();if(e[4]){ctx.fillStyle='#7d8da5';arrow(ctx,a,b)}}for(const f of DATA.map.features){const p=pos(f.x,f.y);ctx.fillStyle='#8b5cf6';ctx.beginPath();ctx.arc(p[0],p[1],5,0,Math.PI*2);ctx.fill();ctx.fillStyle='#6042a2';ctx.font='10px sans-serif';ctx.fillText(f.label||f.feature_type,p[0]+6,p[1]-5)}const r=records[selected];if(!r)return;const ex=new Set(r.expanded_node_ids||[]);ctx.fillStyle='#2563eb66';for(const id of ex){const xy=nodeXY(id);if(!xy)continue;const p=pos(xy[0],xy[1]);ctx.beginPath();ctx.arc(p[0],p[1],3,0,Math.PI*2);ctx.fill()}strokeNodePath(ctx,r.route_node_ids||[],'#dc2626',5,[]);strokeNodePath(ctx,r.attempted_route_node_ids||[],'#7c3aed',3,[6,5]);strokeNodePath(ctx,r.validated_prefix_node_ids||[],'#0f766e',4,[]);for(const v of r.move_violations||[]){const axy=nodeXY(v.from_node_id),bxy=nodeXY(v.to_node_id);if(!axy||!bxy)continue;const a=pos(axy[0],axy[1]),b=pos(bxy[0],bxy[1]);ctx.strokeStyle='#dc2626';ctx.lineWidth=4;ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#dc2626';arrow(ctx,a,b);ctx.strokeStyle='#b91c1c';ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(b[0]-6,b[1]-6);ctx.lineTo(b[0]+6,b[1]+6);ctx.moveTo(b[0]+6,b[1]-6);ctx.lineTo(b[0]-6,b[1]+6);ctx.stroke();ctx.fillStyle='#991b1b';ctx.font='bold 12px sans-serif';ctx.fillText('违规第'+v.step+'步',b[0]+8,b[1]+14)}[['start_node_id','#16a34a','起'],['pickup_node_id','#f59e0b','上'],['dropoff_node_id','#dc2626','终']].forEach(([key,color,label])=>{const xy=nodeXY(r[key]);if(!xy)return;const p=pos(xy[0],xy[1]);ctx.fillStyle=color;ctx.beginPath();ctx.arc(p[0],p[1],8,0,Math.PI*2);ctx.fill();ctx.fillStyle='#111827';ctx.font='bold 11px sans-serif';ctx.fillText(label,p[0]+9,p[1]-8)})}
function filtered(){const scope=$('scope').value,method=$('method').value,status=$('status').value,q=$('query').value.trim().toLowerCase();return records.map((r,i)=>({...r,_i:i})).filter(r=>(scope==='all'||r.scope===scope)&&(method==='all'||r.method_name===method)&&(status==='all'||r.status===status)).filter(r=>{if(!q)return true;return [r.task_id,r.scenario_type,r.method_name,r.start_node_id,r.pickup_node_id,r.dropoff_node_id].some(v=>String(v||'').toLowerCase().includes(q))})}
function render(){const rows=filtered();$('results').innerHTML=rows.length?rows.map(r=>`<tr class="${r._i===selected?'selected':''}" data-i="${r._i}"><td>${r.scope==='orders'?'订单':'正式'}<br><span class="muted">${r.task_id}</span>${r.scenario_type?'<br><span class="muted">'+r.scenario_type+'</span>':''}</td><td>${r.method_name}</td><td class="${r.status==='completed'?'ok':'bad'}">${r.status||'—'}</td><td class="reason-cell">${r.failure_detail_zh||'—'}</td><td>${fmt(r.compute_time_ms,2)}</td><td>${fmt(r.llm_latency_ms,2)}</td><td>${fmtTokenPair(r.llm_input_tokens,r.llm_output_tokens)}</td><td>${fmtEnergy(r)}</td><td>${r.start_node_id||'—'} → ${r.pickup_node_id||'—'} → ${r.dropoff_node_id||'—'}</td><td>${fmt(r.route_distance_m,1)}</td><td>${fmt(r.travel_time_sec,1)}</td><td>${r.expanded_nodes??'—'}</td><td>${fmtCost(r)}</td><td>${r.html_file?`<a href="${r.html_file}" target="_blank">HTML</a> · <a href="${r.json_file}" target="_blank">JSON</a>`:'—'}</td></tr>`).join(''):'<tr><td colspan="14" class="empty">没有匹配的结果</td></tr>';document.querySelectorAll('#results tr[data-i]').forEach(row=>row.addEventListener('click',()=>{selected=Number(row.dataset.i);render();renderDetail();draw()}));}
function renderDetail(){const r=records[selected];if(!r){$('detail').innerHTML='<div class="empty">没有结果</div>';return}const violations=r.move_violations||[];const violationText=violations.map(v=>'第 '+v.step+' 步 '+v.move+'：'+v.from_node_id+' → '+v.to_node_id+'（'+(violationNames[v.kind]||v.kind)+'）').join('；')||'—';const energyNote=r.proposal_energy_interpretation||'';const prefixEnergy=r.valid_prefix_energy_estimate_kwh===null||r.valid_prefix_energy_estimate_kwh===undefined?'—':fmt(r.valid_prefix_energy_estimate_kwh,6)+' kWh';$('detail').innerHTML=`<div><b>${r.scope==='orders'?'订单':'正式场景'} / ${r.task_id}</b> · <b>${r.method_name}</b> · <span class="${r.status==='completed'?'ok':'bad'}">${r.status}</span></div><div class="detail-grid"><span><b>起点：</b>${r.start_node_id||'—'}</span><span><b>上车点：</b>${r.pickup_node_id||'—'}</span><span><b>终点：</b>${r.dropoff_node_id||'—'}</span><span><b>任务类型：</b>${r.task_type||'—'}</span><span><b>场景类型：</b>${r.scenario_type||'—'}</span><span><b>本地计算时间：</b>${fmt(r.compute_time_ms,2)} ms</span><span><b>模型请求次数：</b>${fmt(r.llm_calls,0)}</span><span><b>模型输入 / 输出 token：</b>${fmtTokenPair(r.llm_input_tokens,r.llm_output_tokens)}</span><span><b>模型响应耗时：</b>${fmt(r.llm_latency_ms,2)} ms</span><span><b>本地 Qwen 意图解析耗时：</b>${fmt(r.qwen_intent_latency_ms,2)} ms</span><span><b>API 标价等值（估算，非账单）：</b>${fmtApiUsd(r.codex_api_equivalent_usd)}</span><span><b>账户账单状态：</b>${fmtBillingStatus(r.account_billing_status)}</span><span><b>车辆实际路线能耗：</b>${r.status==='completed'?fmt(r.energy_kwh,4)+' kWh':'未形成有效可执行路线'}</span><span><b>模型提议路线耗电估算：</b>${fmtEnergy(r)}${r.proposal_energy_estimate_kwh!==null&&r.proposal_energy_estimate_kwh!==undefined?' kWh':''}</span><span><b>合法前缀耗电估算：</b>${prefixEnergy}</span><span><b>路线估算说明：</b>${energyNote||'—'}</span><span><b>路线距离：</b>${fmt(r.route_distance_m,1)} m</span><span><b>总行驶时间：</b>${fmt(r.travel_time_sec,1)} s</span><span><b>展开节点：</b>${r.expanded_nodes??'—'}</span><span><b>电费：</b>${fmtComponent(r,r.electricity_cost)} CNY</span><span><b>算力费：</b>${fmtComponent(r,r.compute_cost)} CNY</span><span><b>API 费：</b>${fmtComponent(r,r.api_cost)} CNY</span><span><b>总费用：</b>${fmtCost(r)} CNY</span><span><b>费用状态：</b>${r.cost_status||'—'}</span><span><b>校验：</b>${r.validation_status||'—'}</span><span><b>响应是否收到：</b>${r.llm_response_received===true?'是':r.llm_response_received===false?'否':'—'}</span><span><b>失败原因：</b>${r.failure_detail_zh||'—'}</span><span><b>原始失败码：</b>${r.failure_reason||'—'}</span><span><b>违规步骤：</b>${violationText}</span></div><div><b>地点文本：</b>${r.pickup_text||'—'}；${r.dropoff_text||'—'}</div><div>${r.html_file?`<a href="${r.html_file}" target="_blank">打开逐步检索回放</a> · <a href="${r.json_file}" target="_blank">打开原始 JSON</a>`:''}</div>`}
['scope','method','status','query'].forEach(id=>$(id).addEventListener('input',()=>{render();renderDetail();draw()}));render();renderDetail();draw();
</script></body></html>""".replace("__DATA__", encoded).replace(
        "__SEED_NAV__",
        '<a href="map_preview.html">查看 seed {} 原始地图</a> · '
        '<a href="visualizations/orders/index.html">全部订单结果</a> · '
        '<a href="visualizations/formal_scenarios/index.html">全部正式场景结果</a> · '
        '<a href="../../index.html">全部 10 个种子</a>'.format(payload.get("seed", "?")),
    )


def render_seed_dashboard(seed_dir: Path) -> Path:
    map_path = next(seed_dir.glob("map_*_seed_*.json"))
    map_payload = _read_json(map_path)
    (seed_dir / "map_preview.svg").write_text(
        _map_preview_svg(map_payload), encoding="utf-8"
    )
    (seed_dir / "map_preview.html").write_text(
        _map_preview_html(map_payload), encoding="utf-8"
    )
    output_path = seed_dir / "index.html"
    output_path.write_text(_html(build_dashboard_payload(seed_dir)), encoding="utf-8")
    return output_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", required=True)
    args = parser.parse_args()
    print(str(render_seed_dashboard(Path(args.seed_dir))))


if __name__ == "__main__":
    main()
