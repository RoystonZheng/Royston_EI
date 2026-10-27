#!/usr/bin/env python3
"""Render a self-contained interactive R4 segment replay from saved JSON."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Dict, List


def _compact_map_payload(map_payload: Dict[str, Any]) -> Dict[str, Any]:
    nodes = map_payload["nodes"]
    index = {row["node_id"]: pos for pos, row in enumerate(nodes)}
    coords = {row["node_id"]: (int(row["x"]), int(row["y"])) for row in nodes}
    edges = [
        [index[row["from_node"]], index[row["to_node"]], bool(row.get("one_way", False))]
        for row in map_payload["edges"]
        if row["from_node"] in index and row["to_node"] in index
    ]
    return {
        "nodes": [[row["node_id"], int(row["x"]), int(row["y"])] for row in nodes],
        "edges": edges,
        "features": [
            [
                coords.get(row["node_id"], (None, None))[0],
                coords.get(row["node_id"], (None, None))[1],
                str(row.get("feature_type", "feature")),
                str(row.get("label", row.get("feature_id", "道路特征"))),
            ]
            for row in map_payload.get("roadside_features", [])
        ],
        "boundary": map_payload.get("boundary", {}),
    }


def _segments_from_audit(audit: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for attempt in audit.get("attempts", []):
        segment_audit = attempt.get("audit")
        rows.append(
            {
                "request_number": attempt.get("request_number"),
                "status": attempt.get("status"),
                "model": attempt.get("model"),
                "usage": attempt.get("usage"),
                "error_type": attempt.get("error_type"),
                "proposal": attempt.get("proposal"),
                "audit": segment_audit,
                "attempted_prefix_energy": attempt.get("attempted_prefix_energy"),
                "source_run_dir": attempt.get("source_run_dir"),
                "started_at_utc": attempt.get("started_at_utc"),
                "completed_at_utc": attempt.get("completed_at_utc"),
                "request_start": attempt.get("request_start"),
            }
        )
    return rows


def render_replay(map_payload: Dict[str, Any], task: Dict[str, Any], audit: Dict[str, Any], output_path: Path) -> None:
    compact_map = _compact_map_payload(map_payload)
    route_summary = {
        key: value
        for key, value in audit.get("route", {}).items()
        if key not in {"node_sequence", "edge_sequence", "direction_sequence"}
    }
    audit_summary = {
        key: value
        for key, value in audit.items()
        if key not in {"attempts", "segment_audits", "route"}
    }
    audit_summary["route"] = route_summary
    data = {
        "map": compact_map,
        "task": task,
        "audit": audit_summary,
        "segments": _segments_from_audit(audit),
    }
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    status = html.escape(str(audit.get("status", "unknown")))
    route = audit.get("route", {})
    endpoints = " → ".join(
        str(route.get(key) or task.get(key) or "未记录")
        for key in ("start_node_id", "pickup_node_id", "dropoff_node_id")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>R4 分段路线回放</title>
<style>
:root{color-scheme:light dark;--bg:#f4f6fb;--fg:#172033;--panel:#fff;--muted:#5a6577;--line:#dce2ed;--blue:#2563eb;--green:#138a55;--orange:#e89012;--red:#d32f2f;--purple:#7c3aed}
@media(prefers-color-scheme:dark){:root{--bg:#111827;--fg:#e5e7eb;--panel:#1f2937;--muted:#aab3c2;--line:#3a4658}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
header{padding:18px 22px;background:#172554;color:#fff}header h1{margin:0 0 4px;font-size:21px}header p{margin:3px 0;color:#dbeafe}.wrap{max-width:1480px;margin:auto;padding:16px;display:grid;grid-template-columns:minmax(0,1.7fr) minmax(320px,1fr);gap:16px}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;min-width:0}.mapbox{grid-row:span 2}canvas{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:#fbfdff}.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:12px 0}.toolbar label{display:flex;gap:8px;align-items:center;flex:1;min-width:220px}.toolbar input{flex:1;min-width:100px}.legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}.linekey{display:inline-block;width:18px;border-top:3px solid;margin-right:4px;vertical-align:middle}.metric{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:10px 0}.metric div{padding:8px 0;border-bottom:1px solid var(--line)}.label{color:var(--muted);font-size:12px}.value{font-weight:650;overflow-wrap:anywhere}.scroll{max-height:580px;overflow:auto}.segment{width:100%;display:grid;grid-template-columns:52px 1fr auto;gap:7px;text-align:left;color:var(--fg);background:transparent;border:0;border-bottom:1px solid var(--line);padding:10px 4px;cursor:pointer}.segment[aria-current=true]{background:color-mix(in srgb,var(--blue) 10%,transparent)}.segment small{color:var(--muted)}.bad{color:var(--red)}.good{color:var(--green)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);padding:10px;border-radius:6px;font-size:12px}.notice{color:var(--muted)}@media(max-width:900px){.wrap{grid-template-columns:1fr}.mapbox{grid-row:auto}.scroll{max-height:350px}}
</style><header><h1>R4 分段路径搜索与运行轨迹</h1><p>状态：__STATUS__ · 任务：__TASK__</p><p>起点 → 上车点 → 下车点：__ENDPOINTS__</p></header>
<main class="wrap"><section class="mapbox"><h2>原地图与路线</h2><canvas id="map" width="1100" height="760" role="img" aria-label="冻结道路地图及选中分段的路线轨迹"></canvas><div class="toolbar"><label>回放分段 <output id="stepLabel">0</output><input id="segmentRange" type="range" min="0" max="0" value="0" aria-label="选择回放分段"></label><button id="play" type="button">播放</button><span id="segmentStatus" aria-live="polite"></span></div><div class="legend"><span><i class="linekey" style="color:#97a3b5"></i>冻结道路</span><span><i class="linekey" style="color:#138a55"></i>已接纳路线</span><span><i class="linekey" style="color:#e89012;border-top-style:dashed"></i>当前段提议</span><span><i class="linekey" style="color:#d32f2f;border-top-style:dashed"></i>违规/拒绝动作</span><span><i class="dot" style="background:#7c3aed"></i>道路特征</span><span><i class="dot" style="background:#2563eb"></i>起点</span><span><i class="dot" style="background:#e89012"></i>上车点</span><span><i class="dot" style="background:#d32f2f"></i>下车点</span></div></section>
<section><h2>任务与汇总</h2><div class="metric" id="metrics"></div><p class="notice" id="energyNote"></p></section><section><h2>每段请求</h2><div class="scroll" id="segments"></div><h3>选中段的动作和校验</h3><pre id="detail">未选择分段。</pre></section></main><script id="replay-data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('replay-data').textContent),C=document.getElementById('map'),X=C.getContext('2d'),R=document.getElementById('segmentRange'),L=document.getElementById('stepLabel'),S=document.getElementById('segmentStatus');
const nodes=D.map.nodes,segments=D.segments,byId=new Map(nodes.map((n,i)=>[n[0],i])),bounds=D.map.boundary||{};
const minX=Number(bounds.min_x??Math.min(...nodes.map(n=>n[1]))),maxX=Number(bounds.max_x??Math.max(...nodes.map(n=>n[1]))),minY=Number(bounds.min_y??Math.min(...nodes.map(n=>n[2]))),maxY=Number(bounds.max_y??Math.max(...nodes.map(n=>n[2])));
const pad={l:34,r:28,t:24,b:28},scale=Math.min((C.width-pad.l-pad.r)/Math.max(1,maxX-minX),(C.height-pad.t-pad.b)/Math.max(1,maxY-minY));
function xy(id){const n=nodes[byId.get(id)];return n?[pad.l+(n[1]-minX)*scale,C.height-pad.b-(n[2]-minY)*scale]:null}
function line(a,b,color,width,dash){const p=xy(a),q=xy(b);if(!p||!q)return;X.beginPath();X.setLineDash(dash||[]);X.strokeStyle=color;X.lineWidth=width;X.moveTo(...p);X.lineTo(...q);X.stroke();X.setLineDash([])}
function pathFor(seg,color,dash){const rows=seg?.audit?.steps||[];for(const row of rows){if(row.to_node_id)line(row.from_node_id,row.to_node_id,color,2.7,dash);if(row.violation_kinds?.length&&row.to_node_id){const p=xy(row.to_node_id);if(p){X.fillStyle='#d32f2f';X.beginPath();X.arc(...p,6,0,Math.PI*2);X.fill()}}}}
function draw(){X.clearRect(0,0,C.width,C.height);X.strokeStyle='#d8dee8';X.lineWidth=1;for(const e of D.map.edges){const a=nodes[e[0]],b=nodes[e[1]];X.beginPath();X.moveTo(pad.l+(a[1]-minX)*scale,C.height-pad.b-(a[2]-minY)*scale);X.lineTo(pad.l+(b[1]-minX)*scale,C.height-pad.b-(b[2]-minY)*scale);X.stroke()}
 for(const f of D.map.features){if(f[0]===null||f[1]===null)continue;const p=[pad.l+(f[0]-minX)*scale,C.height-pad.b-(f[1]-minY)*scale];X.fillStyle='#7c3aed';X.fillRect(p[0]-2.5,p[1]-2.5,5,5)}
 const selected=Number(R.value);for(let i=0;i<Math.min(selected,segments.length);i++){const seg=segments[i];if(seg.audit?.accepted)pathFor(seg,'#138a55',[]);else pathFor(seg,'#7c3aed',[5,4])}
 if(selected>0&&selected<=segments.length){const seg=segments[selected-1];if(!seg.audit?.accepted)pathFor(seg,'#e89012',[5,4]);if(seg.audit?.steps)for(const row of seg.audit.steps)if(row.violation_kinds?.length)pathFor({audit:{steps:[row]}},'#d32f2f',[3,3])}
 const task=D.task,rt=D.audit.route||{};for(const [id,color,label] of [[rt.start_node_id||task.start_node_id,'#2563eb','起点'],[rt.pickup_node_id||task.pickup_node_id,'#e89012','上车点'],[rt.dropoff_node_id||task.dropoff_node_id,'#d32f2f','下车点']]){const p=xy(id);if(p){X.fillStyle=color;X.beginPath();X.arc(...p,6,0,Math.PI*2);X.fill();X.fillStyle='#172033';X.font='12px sans-serif';X.fillText(label,p[0]+8,p[1]-7)}}
 L.value=selected;if(!selected){S.textContent='显示冻结地图';document.getElementById('detail').textContent='选择分段以查看模型提议、本地逐边检查和违规前合法前缀能耗。'}else{const seg=segments[selected-1],a=seg.audit||{},requestNumber=seg.request_number??selected;S.textContent=`请求 ${requestNumber}：${a.accepted?'整段接纳':seg.status==='response_received'?'整段拒绝':seg.status||'未决'}`;document.getElementById('detail').textContent=JSON.stringify({proposal:seg.proposal,audit:a,attempted_prefix_energy:seg.attempted_prefix_energy,error_type:seg.error_type,request_start:seg.request_start},null,2)}}
function renderSegments(){const box=document.getElementById('segments');box.replaceChildren();segments.forEach((seg,i)=>{const b=document.createElement('button');b.type='button';b.className='segment';b.setAttribute('aria-current',String(i+1===Number(R.value)));const status=seg.audit?.accepted?'整段合法接纳':seg.audit?'整段拒绝':seg.status==='request_in_progress'?'请求未决':seg.status||'无响应',requestNumber=seg.request_number??i+1;b.innerHTML=`<b>请求 ${requestNumber}</b><span>${status}<br><small>${(seg.proposal?.actions||[]).length} 个提议动作</small></span><small>${seg.usage?.input_tokens??'用量未知'} 输入</small>`;b.addEventListener('click',()=>{R.value=String(i+1);renderSegments();draw()});box.append(b)})}
const route=D.audit.route||{},usage=D.audit.usage||{},energy=D.audit.energy||{};const rows=[['运行状态',D.audit.status||'unknown'],['请求数',D.audit.api_request_count??segments.length],['接纳步数',route.move_count??'—'],['拒绝段数',D.audit.rejected_segment_count??'—'],['完整路线',D.audit.route_complete?'是':'否'],['能耗范围',energy.scope||'未计算'],['估算电耗',energy.energy_kwh==null?'未估算':`${Number(energy.energy_kwh).toFixed(6)} kWh`],['路线距离',energy.distance_m==null?'—':`${Number(energy.distance_m).toFixed(1)} m`],['路线时间',energy.travel_time_sec==null?'—':`${Number(energy.travel_time_sec).toFixed(2)} s`],['输入 token',usage.input_tokens??'未知'],['输出 token',usage.output_tokens??'未知']];document.getElementById('metrics').innerHTML=rows.map(([a,b])=>`<div><div class="label">${a}</div><div class="value">${b}</div></div>`).join('');const attempted=D.audit.attempted_prefix_energy||[];document.getElementById('energyNote').textContent=(energy.note||'此处只展示确定性接纳路线（或合法前缀）的共享评估估值。')+(attempted.length?` 被拒绝提案的违规前合法前缀估算记录有 ${attempted.length} 条，点击对应请求查看；非法边本身没有可定义的道路能耗。`:'');
R.max=String(segments.length);R.value=String(segments.length);R.addEventListener('input',()=>{renderSegments();draw()});let timer=null;document.getElementById('play').addEventListener('click',e=>{if(timer){clearInterval(timer);timer=null;e.currentTarget.textContent='播放';return}e.currentTarget.textContent='暂停';timer=setInterval(()=>{R.value=String((Number(R.value)+1)%(segments.length+1));renderSegments();draw();if(Number(R.value)===segments.length){clearInterval(timer);timer=null;e.currentTarget.textContent='播放'}},500)});renderSegments();draw();
</script></html>"""
        .replace("__STATUS__", status)
        .replace("__TASK__", html.escape(str(audit.get("task_id", task.get("task_id", "unknown")))))
        .replace("__ENDPOINTS__", html.escape(endpoints))
        .replace("__DATA__", encoded),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", type=Path, required=True, help="Frozen map JSON")
    parser.add_argument("--task", type=Path, required=True, help="Task JSON or task object")
    parser.add_argument("--audit", type=Path, required=True, help="Saved run/final audit JSON")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    task_payload = json.loads(args.task.read_text(encoding="utf-8"))
    if isinstance(task_payload, dict) and "tasks" in task_payload:
        audit_task_id = json.loads(args.audit.read_text(encoding="utf-8")).get("task_id")
        task_payload = next(row for row in task_payload["tasks"] if row["task_id"] == audit_task_id)
    render_replay(
        json.loads(args.map.read_text(encoding="utf-8")),
        task_payload,
        json.loads(args.audit.read_text(encoding="utf-8")),
        args.output,
    )


if __name__ == "__main__":
    main()
