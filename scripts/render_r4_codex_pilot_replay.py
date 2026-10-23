#!/usr/bin/env python3
"""Render the bounded independent Codex R4 pilot as a self-contained replay."""

import argparse
import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "outputs/route_layer/r4_codex_offline_preflight_20261021/seed0_semantic_road_feature.json"
DEFAULT_RUN = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v1"

TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex 8×8 路线试跑回放</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#182233;font:15px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}header{background:#172554;color:#fff;padding:18px 24px}h1{margin:0 0 6px;font-size:22px}.sub{color:#dbeafe}.notice{margin:14px 18px 0;padding:12px 15px;background:#fff7ed;border:1px solid #fdba74;border-radius:8px;color:#7c2d12;line-height:1.6}main{display:grid;grid-template-columns:minmax(500px,1.1fr) minmax(360px,.9fr);gap:14px;padding:14px 18px}.card{min-width:0;background:#fff;border:1px solid #dbe3f0;border-radius:10px;padding:14px;box-shadow:0 2px 8px #17255412}svg{display:block;width:100%;height:auto;max-height:76vh;background:#fbfdff;border:1px solid #dbe3f0;border-radius:8px}.controls{display:flex;gap:10px;align-items:center;margin:10px 0}.controls input{flex:1}.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:#475569}.key{display:flex;align-items:center;gap:5px}.swatch{width:11px;height:11px;border-radius:50%;display:inline-block}.pathkey{width:20px;border-top:3px solid #0f766e}h2{font-size:17px;margin:4px 0 10px}.metrics{display:grid;grid-template-columns:150px 1fr;gap:8px 10px;font-size:13px}.label{color:#64748b}.value{overflow-wrap:anywhere}.stepbox{margin-top:12px;border-top:1px solid #e5e7eb;padding-top:12px}.steps{max-height:40vh;overflow:auto}.step{padding:9px 10px;border:1px solid #e2e8f0;border-radius:7px;margin:6px 0;background:#fbfdff}.step.active{border-color:#2563eb;background:#eff6ff}.code{font:12px ui-monospace,monospace;color:#334155}.small{font-size:12px;color:#64748b;line-height:1.55}.pill{display:inline-block;padding:2px 7px;border-radius:10px;background:#dcfce7;color:#166534;font-size:11px}.aside{font-size:12px;color:#475569;line-height:1.5}@media(max-width:980px){main{grid-template-columns:1fr}svg{max-height:65vh}}
</style></head><body><header><h1>Codex 8×8 路线试跑：检索与运行轨迹</h1><div class="sub" id="subtitle"></div></header>
<div class="notice" id="notice"></div><main><section class="card"><div id="map"></div><div class="controls"><label>路线动作步数：<output id="stepLabel">0</output></label><input id="slider" type="range" min="0" max="__MAX_STEP__" step="1" value="__MAX_STEP__"></div><div class="legend"><span class="key"><i class="swatch" style="background:#f59e0b"></i>起点</span><span class="key"><i class="swatch" style="background:#22c55e"></i>上车点</span><span class="key"><i class="swatch" style="background:#a855f7"></i>下车点</span><span class="key"><i class="swatch" style="background:#2563eb"></i>当前节点</span><span class="key"><i class="pathkey"></i>已接纳有向路线</span><span class="key"><i class="swatch" style="background:#a0a7b4"></i>地图道路</span></div><p class="small">灰色箭头显示地图中的有向道路；绿色线路显示目前已接纳的路线。拖动滑块可逐步回放每个模型动作。</p></section>
<aside class="card"><h2>任务与实验边界</h2><div id="summary" class="metrics"></div><div class="stepbox"><h2>逐步请求记录</h2><div class="steps" id="steps"></div></div><div class="stepbox"><h2 id="adjustmentTitle">任务调整说明</h2><p class="aside" id="correction"></p></div><div class="stepbox"><h2>结果解释</h2><p class="aside">这是独立 Codex 试跑，记录模型逐步选边和本地约束检查。只有到达下车点后才记完整路线指标；未完成时只展示已接纳合法前缀的仿真能耗。未接纳的候选动作会标为红色虚线，并与实际车辆能耗分开。Codex CLI 未返回账户现金账单。</p></div><div class="stepbox"><h2>原始记录</h2><p class="aside"><a href="state.json">状态 JSON</a> · <a href="manifest.json">试跑清单</a> · <a href="final_audit.json">最终审计</a> · <a href="codex_usage_cost_reconciliation.json">费用复算</a> · <a href="traces/index.html">单步原始 prompt / schema / JSONL</a></p></div></aside></main>
<script>const D=__DATA__;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ns=new Map(D.map.nodes.map(n=>[n.node_id,n]));
const start=D.task.start_node_id,pickup=D.task.pickup_node_id,drop=D.task.dropoff_node_id;
const steps=D.steps;
document.getElementById('subtitle').textContent=`${D.map.map_id} · ${D.task.task_id} · gpt-5.6-luna · 独立试跑 · ${D.metrics.new_requests} 次模型请求`;
document.getElementById('notice').textContent=D.notice;
const adjustment=D.task_adjustment;document.getElementById('adjustmentTitle').textContent=adjustment?'任务调整说明':'方向映射更正';document.getElementById('correction').textContent=adjustment?`为把路线下界控制在本次授权的 8 次请求内，起点从 ${adjustment.start_node_changed_from} 调整为 ${adjustment.start_node_changed_to}；上车点 ${adjustment.pickup_node_unchanged} 和下车点 ${adjustment.dropoff_node_unchanged} 沿用原任务。原起点至少需 ${adjustment.original_start_minimum_moves} 步，新起点至少需 ${adjustment.new_start_minimum_moves} 步。此任务只验证 Codex CLI 调用链，不纳入正式组间比较。`:D.correction?`旧 v5 记录把方向 N 关联为 ${D.correction.reported_edge_id} → ${D.correction.reported_target}。按冻结地图及代码方向定义，N 实际对应 ${D.correction.correct_edge_id} → ${D.correction.correct_target}；方向动作本身合法，旧边 ID 归属错误。原始 v5 文件保留未改。`:'本次是独立任务，没有前置 smoke，也没有需要修正的方向映射。';
const metricRows=[['起点',start],['上车点',pickup],['下车点',drop],['已接纳移动',String(D.metrics.accepted_moves)],['当前节点',D.metrics.current_node],['是否到达上车点',D.metrics.pickup_seen?'是':'否'],['剩余最短步数（拓扑下界）',String(D.metrics.minimum_remaining_moves)],['累计模型请求',`${D.metrics.new_requests} / ${D.metrics.authorized_limit}`],['重试 / 工具事件',`${D.metrics.retries} / ${D.metrics.tool_events}`],['停止动作',String(D.metrics.stopped_actions.length)],['模型自检与本地事实不一致',String(D.metrics.self_check_discrepancy_count)],['自检不一致但本地校验后接纳',String(D.metrics.accepted_self_check_discrepancy_count)],['停止动作实际车辆耗电',`${D.metrics.rejected_vehicle_energy_kwh.toFixed(3)} kWh`],['停止动作若执行的估算耗电',`${D.metrics.hypothetical_rejected_energy_kwh.toFixed(3)} kWh（未实际消耗）`],['累计 token 用量',`${D.metrics.input_tokens.toLocaleString()} 输入 / ${D.metrics.output_tokens.toLocaleString()} 输出`],['累计 API 标价等值',`$${D.metrics.api_equivalent_usd.toFixed(6)}（不是账单）`],['CLI 请求耗时合计',`${(D.metrics.latency_ms/1000).toFixed(1)} 秒`],['合法前缀长度',`${D.metrics.prefix_distance_m} 米`],['合法前缀仿真能耗',`${D.metrics.prefix_energy_kwh.toFixed(3)} kWh`],['账户现金费用','CLI 未提供']];document.getElementById('summary').innerHTML=metricRows.map(([k,v])=>`<div class="label">${esc(k)}</div><div class="value">${esc(v)}</div>`).join('');
function draw(endIndex){const W=760,H=700,pad=58,dx=(W-2*pad)/(D.map.size-1),dy=(H-2*pad)/(D.map.size-1),pt=n=>({x:pad+n.x*dx,y:H-pad-n.y*dy});const offset=(a,b)=>{const p=pt(a),q=pt(b),vx=q.x-p.x,vy=q.y-p.y,L=Math.hypot(vx,vy)||1,o=2.8;return{x1:p.x-vy/L*o,y1:p.y+vx/L*o,x2:q.x-vy/L*o,y2:q.y+vx/L*o}};const shownEdges=new Set(steps.slice(0,endIndex+1).filter(s=>s.accepted).map(s=>s.edge_id));let html=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="8乘8有向道路地图与路线回放"><defs><marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#929aaa"/></marker><marker id="routeArr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#0f766e"/></marker><marker id="rejectArr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#dc2626"/></marker></defs>`;
for(const e of D.map.edges){const a=ns.get(e.from_node),b=ns.get(e.to_node);if(!a||!b)continue;const o=offset(a,b),active=shownEdges.has(e.edge_id);html+=`<line x1="${o.x1}" y1="${o.y1}" x2="${o.x2}" y2="${o.y2}" stroke="${active?'#0f766e':'#a4acba'}" stroke-width="${active?4:1.15}" opacity="${active?1:.48}" marker-end="url(#${active?'routeArr':'arr'})"/>`;}
const active=steps[endIndex],candidate=active&&!active.accepted&&active.edge_id&&active.to_node?active:null;if(candidate){const a=ns.get(candidate.from_node),b=ns.get(candidate.to_node);if(a&&b){const o=offset(a,b);html+=`<line x1="${o.x1}" y1="${o.y1}" x2="${o.x2}" y2="${o.y2}" stroke="#dc2626" stroke-width="4" stroke-dasharray="8 6" marker-end="url(#rejectArr)"/>`;}}
for(let y=0;y<D.map.size;y++)for(let x=0;x<D.map.size;x++){const n=ns.get(`node_${x}_${y}`);if(!n)continue;const p=pt(n),isStart=n.node_id===start,isPickup=n.node_id===pickup,isDrop=n.node_id===drop,currentNode=steps[endIndex].accepted?steps[endIndex].to_node:steps[endIndex].from_node,isCurrent=n.node_id===currentNode;const color=isCurrent?'#2563eb':isStart?'#f59e0b':isPickup?'#22c55e':isDrop?'#a855f7':'#64748b';const r=isStart||isPickup||isDrop||isCurrent?8:3.1;html+=`<circle cx="${p.x}" cy="${p.y}" r="${r}" fill="${color}" stroke="#fff" stroke-width="2"/>`;if(isStart||isPickup||isDrop||isCurrent){const label=isCurrent?'当前':isStart?'起点':isPickup?'上车':'下车';html+=`<text x="${p.x+10}" y="${p.y-9}" font-size="13" font-weight="700" fill="#1f2937">${label} ${n.node_id}</text>`}else if(n.landmark_name){html+=`<text x="${p.x+5}" y="${p.y-5}" font-size="9" fill="#475569">${esc(n.landmark_name)}</text>`}}
html+='</svg>';document.getElementById('map').innerHTML=html;document.getElementById('stepLabel').textContent=`${endIndex} / ${steps.length-1}`;document.querySelectorAll('.step').forEach((el,i)=>el.classList.toggle('active',i===endIndex));}
document.getElementById('steps').innerHTML=steps.map((s,i)=>`<div class="step" data-i="${i}"><div><b>${s.is_start?'任务起点（无模型请求）':s.is_bootstrap?'先前单步 smoke':'模型请求 '+s.request_number}</b>　<span class="pill">${esc(s.status)}</span></div><div class="code">${esc(s.from_node)}${s.direction?' —'+esc(s.direction)+'→ '+esc(s.to_node):''}${s.edge_id?' · '+esc(s.edge_id):''}</div>${s.is_start||s.is_bootstrap?'':`<div class="small">候选方向：${esc((s.allowed_directions||[]).join('、'))}；用量：${esc(s.input_tokens)} 输入 / ${esc(s.output_tokens)} 输出；本地约束检查：${s.local_valid?'通过':'未通过'}；${s.accepted?'已接纳并执行':'候选边（未执行）'}；工具事件：${esc((s.tool_item_types||[]).join(', ')||'无')}${s.self_check_false_fields?.length?`；模型自检为 false：${esc(s.self_check_false_fields.join('、'))}`:''}${s.model_self_check_mismatch_fields?.length?`；自检与本地事实不一致：${esc(s.model_self_check_mismatch_fields.join('、'))}`:''}${s.accepted?'':`；车辆耗电：${Number(s.vehicle_energy_consumed_kwh||0).toFixed(3)} kWh；候选边若执行估算：${s.hypothetical_step_energy_kwh==null?'未知':Number(s.hypothetical_step_energy_kwh).toFixed(3)+' kWh（未实际消耗）'}`}</div>`}</div>`).join('');
const slider=document.getElementById('slider');slider.addEventListener('input',()=>draw(Number(slider.value)));draw(Number(slider.value));
</script></body></html>'''


def _resolve_run_reference(reference):
    if not reference:
        return None
    path = Path(reference)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _load_run_chain(run_dir, parent_run=None):
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    parent = _resolve_run_reference(parent_run or state.get("parent_run"))
    seen = {run_dir.resolve()}
    ancestors = []
    while parent:
        if parent in seen:
            raise RuntimeError("pilot_parent_chain_cycle")
        seen.add(parent)
        parent_state = json.loads((parent / "state.json").read_text(encoding="utf-8"))
        ancestors.append((parent, parent_state))
        parent = _resolve_run_reference(parent_state.get("parent_run"))
    ancestors.reverse()
    return state, ancestors


def render(run_dir, output, parent_run=None):
    run_dir = Path(run_dir).resolve()
    state, ancestors = _load_run_chain(run_dir, parent_run)
    run_states = [entry[1] for entry in ancestors] + [state]
    attempt_entries = [
        (run_path, attempt)
        for run_path, run_state in ancestors + [(run_dir, state)]
        for attempt in run_state.get("attempts", [])
    ]
    all_attempts = [attempt for _, attempt in attempt_entries]
    source_preflight = Path(state.get("source_preflight", PREFLIGHT))
    if not source_preflight.is_absolute():
        source_preflight = ROOT / source_preflight
    map_data = json.loads(source_preflight.read_text(encoding="utf-8"))["map"]
    map_data["size"] = int(map_data["size"])
    steps = []
    audit_path = run_dir / "final_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
    audits_by_run = {}
    for attempt_run_dir, _ in ancestors + [(run_dir, state)]:
        source = attempt_run_dir / "final_audit.json"
        audits_by_run[attempt_run_dir.resolve()] = (
            json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
        )
    invalid_by_run = {
        (attempt_run_dir.resolve(), row.get("request_number")): row
        for attempt_run_dir, run_audit in audits_by_run.items()
        for row in run_audit.get("invalid_or_stopped_actions", [])
    }
    stopped = [
        row
        for run_audit in audits_by_run.values()
        for row in run_audit.get("invalid_or_stopped_actions", [])
    ]
    self_check_discrepancies = [
        row
        for run_audit in audits_by_run.values()
        for row in run_audit.get("model_self_check_discrepancies", [])
    ]
    discrepancy_by_run = {
        (run_dir.resolve(), row.get("request_number")): row
        for run_dir, run_audit in audits_by_run.items()
        for row in run_audit.get("model_self_check_discrepancies", [])
    }
    for display_number, (attempt_run_dir, attempt) in enumerate(attempt_entries, 1):
        response = attempt.get("response", {})
        direction = response.get("direction")
        accepted = attempt.get("status") in {"accepted_legal_step", "route_completed"} and bool(attempt.get("selected_edge_id"))
        local = attempt.get("validation", {}).get("deterministic_local_check", {})
        false_fields = sorted(k for k, v in response.get("self_check", {}).items() if v is False)
        status_label = attempt.get("status")
        if (
            not accepted
            and local.get("passed") is True
            and false_fields
            and attempt.get("status") == "stopped_illegal_or_invalid_action"
        ):
            status_label = "模型自检与本地事实矛盾，动作未执行"
        diagnostic = invalid_by_run.get((attempt_run_dir.resolve(), attempt.get("request_number")), {})
        discrepancy = discrepancy_by_run.get((attempt_run_dir.resolve(), attempt.get("request_number")), {})
        steps.append({
            "request_number": display_number,
            "from_node": attempt.get("current_node_id"),
            "to_node": attempt.get("target_node_id") if accepted else local.get("expected_target_node_id"),
            "edge_id": attempt.get("selected_edge_id") if accepted else attempt.get("allowed_edge_ids", {}).get(direction),
            "direction": direction,
            "allowed_directions": attempt.get("allowed_directions", []),
            "status": status_label,
            "accepted": accepted,
            "local_valid": bool(local.get("passed")),
            "self_check_false_fields": false_fields,
            "model_self_check_mismatch_fields": discrepancy.get(
                "model_self_check_mismatch_fields",
                attempt.get("validation", {}).get("model_self_check_mismatch_fields", []),
            ),
            "tool_item_types": attempt.get("tool_item_types", []),
            "input_tokens": attempt.get("usage", {}).get("input_tokens"),
            "output_tokens": attempt.get("usage", {}).get("output_tokens"),
            "vehicle_energy_consumed_kwh": diagnostic.get("vehicle_energy_consumed_kwh", 0.0) if not accepted else None,
            "hypothetical_step_energy_kwh": diagnostic.get("hypothetical_step_energy_kwh") if not accepted else None,
        })

    bootstrap_included = any(
        bool(item.get("prior_smoke_result") or item.get("prior_action_reconciliation"))
        for item in run_states
    )
    bootstrap_usage = {}
    if bootstrap_included:
        bootstrap = json.loads((ROOT / "outputs/route_layer/r4_codex_smoke_v5/result.json").read_text(encoding="utf-8"))
        bootstrap_usage = bootstrap.get("usage", {})
    auth_limit = int(bootstrap_included) + sum(int(item.get("authorized_new_request_limit", 0) or 0) for item in run_states)
    last_accepted = next((a for a in reversed(all_attempts) if a.get("status") == "accepted_legal_step"), None)
    min_remaining = (
        last_accepted.get("validation", {}).get("deterministic_local_check", {}).get("remaining_minimum_moves_after_move")
        if last_accepted else state.get("initial_remaining_minimum_moves")
    )
    accepted_self_check_discrepancies = [
        row for row in self_check_discrepancies if row.get("action_executed")
    ]
    metrics = {
        "accepted_moves": len(state["accepted_edges"]),
        "current_node": state["accepted_nodes"][-1],
        "pickup_seen": bool(state["pickup_seen"]),
        "minimum_remaining_moves": audit.get("minimum_remaining_moves_at_stop", min_remaining),
        "new_requests": audit.get("cumulative_requests_including_bootstrap_and_parent", int(bootstrap_included) + len(all_attempts)),
        "bootstrap_route_action_included": bootstrap_included,
        "authorized_limit": auth_limit,
        "retries": sum(int(item.get("retry_count", 0) or 0) for item in run_states),
        "tool_events": sum(bool(attempt.get("tool_item_types")) for attempt in all_attempts),
        "stopped_actions": stopped,
        "self_check_discrepancy_count": len(self_check_discrepancies),
        "accepted_self_check_discrepancy_count": len(accepted_self_check_discrepancies),
        "self_check_discrepancies": self_check_discrepancies,
        "rejected_vehicle_energy_kwh": sum(float(row.get("vehicle_energy_consumed_kwh", 0) or 0) for row in stopped),
        "hypothetical_rejected_energy_kwh": sum(float(row.get("hypothetical_step_energy_kwh", 0) or 0) for row in stopped if row.get("hypothetical_step_energy_kwh") is not None),
        "input_tokens": audit.get("cumulative_input_tokens_including_bootstrap_and_parent", 0),
        "output_tokens": audit.get("cumulative_output_tokens_including_bootstrap_and_parent", 0),
        "api_equivalent_usd": audit.get("cumulative_api_equivalent_usd_including_bootstrap_and_parent", 0),
        "latency_ms": sum(float(item.get("usage_totals", {}).get("latency_ms", 0) or 0) for item in run_states) + (float(bootstrap_usage.get("latency_ms", 0) or 0) if bootstrap_included else 0.0),
        "prefix_distance_m": state["route_metrics"]["accepted_prefix_distance_m"],
        "prefix_energy_kwh": state["route_metrics"]["accepted_prefix_energy_kwh"],
    }
    if audit.get("status") == "completed_route":
        if stopped and accepted_self_check_discrepancies:
            notice = (
                "已完成取送路线。历史上有 {0} 个候选动作因模型自检与本地事实矛盾而未执行；"
                "另有 {1} 个自检不一致动作经独立本地约束校验通过后接纳。"
                "未执行候选的实际车辆耗电为 {2:.3f} kWh，若执行的估算能耗为 {3:.3f} kWh（未实际消耗）。"
            ).format(
                len(stopped), len(accepted_self_check_discrepancies),
                metrics["rejected_vehicle_energy_kwh"], metrics["hypothetical_rejected_energy_kwh"],
            )
        elif stopped:
            notice = (
                "已完成取送路线。历史续跑前曾有一次模型自检与本地事实矛盾；该候选动作当时未执行，"
                "实际车辆能耗为 {0:.3f} kWh，若执行的估算能耗为 {1:.3f} kWh（未实际消耗）。"
                "后续通过确定性本地约束校验的动作完成路线。"
            ).format(metrics["rejected_vehicle_energy_kwh"], metrics["hypothetical_rejected_energy_kwh"])
        elif self_check_discrepancies:
            notice = (
                "已完成取送路线。{0} 次模型自检与本地事实不一致；对应动作均由独立本地校验通过后才接纳，"
                "差异字段已逐步记录。"
            ).format(len(self_check_discrepancies))
        else:
            notice = "已完成取送路线；每一步都通过模型自检、动态方向 schema 和本地有向边约束。"
    elif stopped:
        false_fields = stopped[-1].get("model_self_check_false_fields", [])
        notice = (
            "本轮第 {0} 次请求触发停止：模型自检失败字段为 {1}。候选动作未执行，实际车辆耗电为 {2:.3f} kWh；"
            "若执行候选边，估算为 {3:.3f} kWh（不计入实际耗电）。最短剩余步数按最后一条已接纳动作计算：{4} 步。"
        ).format(state.get("new_requests_used"), "、".join(false_fields) or "无", metrics["rejected_vehicle_energy_kwh"], metrics["hypothetical_rejected_energy_kwh"], min_remaining)
        if accepted_self_check_discrepancies:
            notice += "此前另有 {0} 个模型自检与本地事实不一致的动作，经本地硬校验通过后接纳。".format(
                len(accepted_self_check_discrepancies)
            )
    else:
        if state.get("status") in {
            "authorized_call_limit_exhausted",
            "insufficient_authorized_budget_for_remaining_route",
            "remaining_route_unreachable",
        }:
            if state.get("status") == "authorized_call_limit_exhausted":
                budget_explanation = "本轮授权额度已用完"
            elif state.get("status") == "remaining_route_unreachable":
                budget_explanation = "按当前已访问节点约束，剩余目的地已不可达"
            else:
                budget_explanation = "剩余授权请求数不足以覆盖最短剩余路线"
            notice = (
                "{0}（已用 {1}/{2} 次）；路线仍未完成取送，当前节点到完成任务至少还需 {3} 步（拓扑下界）。"
                "只报告本轮已接纳合法前缀的仿真能耗，不作为正式多种子路线结果。"
            ).format(
                budget_explanation,
                state.get("new_requests_used"),
                state.get("authorized_new_request_limit"),
                audit.get("minimum_remaining_moves_at_stop", min_remaining),
            )
        else:
            notice = "该独立 Codex 试跑仍未完成上车与下车任务；只报告已接纳合法前缀的仿真能耗，不作为正式多种子路线结果。"
        if self_check_discrepancies:
            notice += "另有 {0} 次模型自检与本地事实不一致，动作是否接纳以本地确定性校验为准。".format(
                len(self_check_discrepancies)
            )
    if bootstrap_included:
        initial_edge = state["accepted_edges"][0]
        edges_by_id = {edge["edge_id"]: edge for edge in map_data["edges"]}
        first_edge = edges_by_id[initial_edge]
        steps.insert(0, {
            "request_number": 0, "from_node": first_edge["from_node"], "to_node": first_edge["to_node"],
            "edge_id": initial_edge, "direction": state["accepted_directions"][0],
            "status": "先前单步 smoke（方向映射更正）", "accepted": True, "is_bootstrap": True,
            "local_valid": True, "self_check_false_fields": [], "tool_item_types": [],
            "input_tokens": 14153, "output_tokens": 1373,
            "vehicle_energy_consumed_kwh": None, "hypothetical_step_energy_kwh": None,
        })
    else:
        steps.insert(0, {
            "request_number": 0, "from_node": state["start_node_id"], "to_node": state["start_node_id"],
            "edge_id": None, "direction": None, "status": "任务开始", "accepted": True,
            "is_start": True, "local_valid": True, "self_check_false_fields": [], "tool_item_types": [],
            "input_tokens": None, "output_tokens": None,
            "vehicle_energy_consumed_kwh": None, "hypothetical_step_energy_kwh": None,
        })
    payload = {
        "map": map_data,
        "task": {"task_id": state["task_id"], "start_node_id": state["start_node_id"], "pickup_node_id": state["pickup_node_id"], "dropoff_node_id": state["dropoff_node_id"]},
        "steps": steps, "metrics": metrics, "notice": notice, "correction": state.get("prior_action_reconciliation"), "task_adjustment": state.get("task_adjustment"),
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(TEMPLATE.replace("__DATA__", data).replace("__MAX_STEP__", str(len(steps) - 1)), encoding="utf-8")

    trace_dir = run_dir / "traces"
    trace_links = [
        '<li><a href="{0}">{0}</a> <span>({1} bytes)</span></li>'.format(html.escape(path.name, quote=True), path.stat().st_size)
        for path in sorted(trace_dir.iterdir()) if path.is_file() and path.name != "index.html"
    ]
    parent_links = "".join(
        '<p><a href="../../{0}/replay.html">查看 {0} 路线回放</a></p>'.format(html.escape(path.name, quote=True))
        for path, _ in ancestors
    )
    index_html = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Codex 单步原始记录</title><body style="font:15px -apple-system,BlinkMacSystemFont,sans-serif;max-width:900px;margin:32px auto;padding:0 18px;color:#172033">'
        '<h1>Codex 单步原始记录</h1><p><a href="../replay.html">返回交互式路线回放</a></p>'
        '<p>每次请求保留 prompt、动态输出 schema、原始 CLI JSONL 和最终 JSON；拒绝动作也会列在完整回放中。</p>'
        + parent_links + '<ul>' + "\\n".join(trace_links) + '</ul></body></html>'
    )
    (trace_dir / "index.html").write_text(index_html, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--parent-run", type=Path, default=None,
                        help="Optional earlier pilot directory whose accepted steps should be included in the replay")
    args = parser.parse_args()
    output = args.output or args.run_dir / "replay.html"
    print(render(args.run_dir, output, args.parent_run))


if __name__ == "__main__":
    main()
