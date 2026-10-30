#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the bounded independent Codex R4 pilot as a self-contained replay."""

import argparse
import hashlib
import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "outputs/route_layer/r4_codex_offline_preflight_20261021/seed0_semantic_road_feature.json"
DEFAULT_RUN = ROOT / "outputs/route_layer/r4_codex_pilot_continuation_v1"

TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex __MAP_SIZE__×__MAP_SIZE__ 路线试跑回放</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#182233;font:15px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}header{background:#172554;color:#fff;padding:18px 24px}h1{margin:0 0 6px;font-size:22px}.sub{color:#dbeafe}.notice{margin:14px 18px 0;padding:12px 15px;background:#fff7ed;border:1px solid #fdba74;border-radius:8px;color:#7c2d12;line-height:1.6}main{display:grid;grid-template-columns:minmax(500px,1.1fr) minmax(360px,.9fr);gap:14px;padding:14px 18px}.card{min-width:0;background:#fff;border:1px solid #dbe3f0;border-radius:10px;padding:14px;box-shadow:0 2px 8px #17255412}svg{display:block;width:100%;height:auto;max-height:76vh;background:#fbfdff;border:1px solid #dbe3f0;border-radius:8px}.controls{display:flex;gap:10px;align-items:center;margin:10px 0}.controls input{flex:1}.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:#475569}.key{display:flex;align-items:center;gap:5px}.swatch{width:11px;height:11px;border-radius:50%;display:inline-block}.pathkey{width:20px;border-top:3px solid #0f766e}h2{font-size:17px;margin:4px 0 10px}.metrics{display:grid;grid-template-columns:150px 1fr;gap:8px 10px;font-size:13px}.label{color:#64748b}.value{overflow-wrap:anywhere}.stepbox{margin-top:12px;border-top:1px solid #e5e7eb;padding-top:12px}.steps{max-height:40vh;overflow:auto}.step{padding:9px 10px;border:1px solid #e2e8f0;border-radius:7px;margin:6px 0;background:#fbfdff}.step.active{border-color:#2563eb;background:#eff6ff}.code{font:12px ui-monospace,monospace;color:#334155}.small{font-size:12px;color:#64748b;line-height:1.55}.pill{display:inline-block;padding:2px 7px;border-radius:10px;background:#dcfce7;color:#166534;font-size:11px}.aside{font-size:12px;color:#475569;line-height:1.5}@media(max-width:980px){main{grid-template-columns:1fr}svg{max-height:65vh}}
</style></head><body><header><h1>Codex __MAP_SIZE__×__MAP_SIZE__ 路线试跑：检索与运行轨迹</h1><div class="sub" id="subtitle"></div></header>
<div class="notice" id="notice"></div><main><section class="card"><div id="map"></div><div class="controls"><label>路线动作步数：<output id="stepLabel">0</output></label><input id="slider" type="range" min="0" max="__MAX_STEP__" step="1" value="__MAX_STEP__"></div><div class="legend"><span class="key"><i class="swatch" style="background:#f59e0b"></i>起点</span><span class="key"><i class="swatch" style="background:#22c55e"></i>上车点</span><span class="key"><i class="swatch" style="background:#a855f7"></i>下车点</span><span class="key"><i class="swatch" style="background:#2563eb"></i>当前节点</span><span class="key"><i class="pathkey"></i>已接纳有向路线</span><span class="key"><i class="swatch" style="background:#a0a7b4"></i>地图道路</span></div><p class="small">灰色箭头显示地图中的有向道路；绿色线路显示目前已接纳的路线。拖动滑块可逐步回放每个模型动作。</p></section>
<aside class="card"><h2>任务与实验边界</h2><div id="summary" class="metrics"></div><div class="stepbox"><h2>逐步请求记录</h2><div class="steps" id="steps"></div></div><div class="stepbox"><h2 id="adjustmentTitle">任务调整说明</h2><p class="aside" id="correction"></p></div><div class="stepbox"><h2>结果解释</h2><p class="aside">这是独立 Codex 试跑，记录模型逐步选边和本地约束检查。只有到达下车点后才记完整路线指标；未完成时只展示已接纳合法前缀的仿真能耗。未接纳的候选动作会标为红色虚线，并与实际车辆能耗分开。模型自检与确定性本地校验冲突时，以本地校验决定是否执行，并在单步记录中保留冲突。Codex CLI 未返回账户现金账单。</p></div><div class="stepbox"><h2>原始记录</h2><div id="artifactLinks" class="aside"></div></div></aside></main>
<script>const D=__DATA__;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const artifactLabels={'state.json':'状态 JSON','manifest.json':'试跑清单','final_audit.json':'最终审计','codex_usage_cost_reconciliation.json':'费用复算','authorization_boundary_audit.json':'授权边界审计','usage_reconciliation.json':'用量差分审计','offline_reconciliation.json':'离线调用对账','traces/index.html':'单步原始 prompt / schema / JSONL'};document.getElementById('artifactLinks').innerHTML=(D.artifacts||[]).map(path=>`<a href="${esc(path)}">${esc(artifactLabels[path]||path)}</a>`).join(' · ')||'无原始附件';
const ns=new Map(D.map.nodes.map(n=>[n.node_id,n]));
const start=D.task.start_node_id,pickup=D.task.pickup_node_id,drop=D.task.dropoff_node_id;
const steps=D.steps;
document.getElementById('subtitle').textContent=`${D.map.map_id} · ${D.task.task_id} · gpt-5.6-luna · 独立试跑 · ${D.metrics.new_requests} 次模型请求`;
document.getElementById('notice').textContent=D.notice;
const adjustment=D.task_adjustment;document.getElementById('adjustmentTitle').textContent=adjustment?'任务调整说明':'方向映射更正';document.getElementById('correction').textContent=adjustment?`为把路线下界控制在本次授权的 8 次请求内，起点从 ${adjustment.start_node_changed_from} 调整为 ${adjustment.start_node_changed_to}；上车点 ${adjustment.pickup_node_unchanged} 和下车点 ${adjustment.dropoff_node_unchanged} 沿用原任务。原起点至少需 ${adjustment.original_start_minimum_moves} 步，新起点至少需 ${adjustment.new_start_minimum_moves} 步。此任务只验证 Codex CLI 调用链，不纳入正式组间比较。`:D.correction?`旧 v5 记录把方向 N 关联为 ${D.correction.reported_edge_id} → ${D.correction.reported_target}。按冻结地图及代码方向定义，N 实际对应 ${D.correction.correct_edge_id} → ${D.correction.correct_target}；方向动作本身合法，旧边 ID 归属错误。原始 v5 文件保留未改。`:'本次是独立任务，没有前置 smoke，也没有需要修正的方向映射。';
const metricRows=[['起点',start],['上车点',pickup],['下车点',drop],['已接纳移动',String(D.metrics.accepted_moves)],['当前节点',D.metrics.current_node],['是否到达上车点',D.metrics.pickup_seen?'是':'否'],['剩余最短步数（拓扑下界）',String(D.metrics.minimum_remaining_moves)],['确认模型请求 / 当前授权上限',`${D.metrics.new_requests} / ${D.metrics.authorized_limit}`],['CLI 调用 / 未观测到模型 turn',`${D.metrics.cli_invocations} / ${D.metrics.cli_invocations_without_model_turn}`],['当前调用授权',D.metrics.additional_model_requests_authorized===false?'无后续调用授权':String(D.metrics.current_authorization_status||'未记录')],['重试 / 工具事件',`${D.metrics.retries} / ${D.metrics.tool_events==null?'未知':D.metrics.tool_events}`],['原始工具事件审计缺失请求',String(D.metrics.tool_audit_unknown_request_count)],['停止动作',String(D.metrics.stopped_actions.length)],['模型自检与本地事实不一致',String(D.metrics.self_check_discrepancy_count)],['自检不一致但本地校验后接纳',String(D.metrics.accepted_self_check_discrepancy_count)],['模型失败码被本地校验覆盖',String(D.metrics.model_failure_code_override_count)],['停止动作实际车辆耗电',`${D.metrics.rejected_vehicle_energy_kwh.toFixed(3)} kWh`],['停止动作若执行的估算耗电',`${D.metrics.hypothetical_rejected_energy_kwh.toFixed(3)} kWh（未实际消耗）`],['token 用量',`${D.metrics.input_tokens==null?'未提供':D.metrics.input_tokens.toLocaleString()} 输入 / ${D.metrics.output_tokens==null?'未提供':D.metrics.output_tokens.toLocaleString()} 输出${D.metrics.token_usage_complete?'':'（已知小计，完整用量未知）'}`],['累计 API 标价等值',`$${D.metrics.api_equivalent_usd==null?'未提供':'$'+D.metrics.api_equivalent_usd.toFixed(6)}（不是账单）`],['CLI 请求耗时合计',`${D.metrics.latency_ms==null?'未知':(D.metrics.latency_ms/1000).toFixed(1)+' 秒'}`],['合法前缀长度',D.metrics.prefix_distance_m==null?(D.metrics.accepted_moves===0?'不可估（没有已接纳路线移动）':'不可估（EV v2 路线能耗未评估）'):`${D.metrics.prefix_distance_m} 米`],['合法前缀仿真能耗',D.metrics.prefix_energy_kwh==null?(D.metrics.accepted_moves===0?'不可估（没有已接纳路线移动）':'不可估（EV v2 路线能耗未评估）'):`${D.metrics.prefix_energy_kwh.toFixed(3)} kWh`],['账户现金费用','CLI 未提供']];document.getElementById('summary').innerHTML=metricRows.map(([k,v])=>`<div class="label">${esc(k)}</div><div class="value">${esc(v)}</div>`).join('');
const summaryChildren=Array.from(document.getElementById('summary').children);const latencyLabelIndex=summaryChildren.findIndex(el=>el.classList.contains('label')&&el.textContent==='CLI 请求耗时合计');if(latencyLabelIndex>=0){const latencyValue=summaryChildren[latencyLabelIndex+1];latencyValue.textContent=D.metrics.latency_ms==null?'未知（未记录可核验的 CLI 时延）':`${(D.metrics.latency_ms/1000).toFixed(1)} 秒${D.metrics.latency_complete?'':'（已知小计，未决调用时延未知）'}`;}
function draw(endIndex){const W=760,H=700,pad=58,dx=(W-2*pad)/(D.map.size-1),dy=(H-2*pad)/(D.map.size-1),pt=n=>({x:pad+n.x*dx,y:H-pad-n.y*dy});const offset=(a,b)=>{const p=pt(a),q=pt(b),vx=q.x-p.x,vy=q.y-p.y,L=Math.hypot(vx,vy)||1,o=2.8;return{x1:p.x-vy/L*o,y1:p.y+vx/L*o,x2:q.x-vy/L*o,y2:q.y+vx/L*o}};const shownEdges=new Set(steps.slice(0,endIndex+1).filter(s=>s.accepted).map(s=>s.edge_id));let html=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="__MAP_SIZE__乘__MAP_SIZE__有向道路地图与路线回放"><defs><marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#929aaa"/></marker><marker id="routeArr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#0f766e"/></marker><marker id="rejectArr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#dc2626"/></marker></defs>`;
for(const e of D.map.edges){const a=ns.get(e.from_node),b=ns.get(e.to_node);if(!a||!b)continue;const o=offset(a,b),active=shownEdges.has(e.edge_id);html+=`<line x1="${o.x1}" y1="${o.y1}" x2="${o.x2}" y2="${o.y2}" stroke="${active?'#0f766e':'#a4acba'}" stroke-width="${active?4:1.15}" opacity="${active?1:.48}" marker-end="url(#${active?'routeArr':'arr'})"/>`;}
const active=steps[endIndex],candidate=active&&!active.accepted&&active.candidate_visible_on_map?active:null;if(candidate){const a=ns.get(candidate.from_node),b=ns.get(candidate.to_node);if(a&&b){const o=offset(a,b);html+=`<line x1="${o.x1}" y1="${o.y1}" x2="${o.x2}" y2="${o.y2}" stroke="#dc2626" stroke-width="4" stroke-dasharray="8 6" marker-end="url(#rejectArr)"/>`;}}
for(let y=0;y<D.map.size;y++)for(let x=0;x<D.map.size;x++){const n=ns.get(`node_${x}_${y}`);if(!n)continue;const p=pt(n),isStart=n.node_id===start,isPickup=n.node_id===pickup,isDrop=n.node_id===drop,currentNode=steps[endIndex].accepted?steps[endIndex].to_node:steps[endIndex].from_node,isCurrent=n.node_id===currentNode;const color=isCurrent?'#2563eb':isStart?'#f59e0b':isPickup?'#22c55e':isDrop?'#a855f7':'#64748b';const r=isStart||isPickup||isDrop||isCurrent?8:3.1;html+=`<circle cx="${p.x}" cy="${p.y}" r="${r}" fill="${color}" stroke="#fff" stroke-width="2"/>`;if(isStart||isPickup||isDrop||isCurrent){const label=isCurrent?'当前':isStart?'起点':isPickup?'上车':'下车';html+=`<text x="${p.x+10}" y="${p.y-9}" font-size="13" font-weight="700" fill="#1f2937">${label} ${n.node_id}</text>`}else if(n.landmark_name){html+=`<text x="${p.x+5}" y="${p.y-5}" font-size="9" fill="#475569">${esc(n.landmark_name)}</text>`}}
html+='</svg>';document.getElementById('map').innerHTML=html;document.getElementById('stepLabel').textContent=`${endIndex} / ${steps.length-1}`;document.querySelectorAll('.step').forEach((el,i)=>el.classList.toggle('active',i===endIndex));}
document.getElementById('steps').innerHTML=steps.map((s,i)=>`<div class="step" data-i="${i}"><div><b>${s.is_start?'任务起点（无模型请求）':s.is_bootstrap?'先前单步 smoke':'模型请求 '+s.request_number}</b>　<span class="pill">${esc(s.status)}</span></div><div class="code">${esc(s.from_node)}${s.direction?' —'+esc(s.direction)+'→ '+esc(s.to_node):''}${s.edge_id?' · '+esc(s.edge_id):''}</div>${s.is_start||s.is_bootstrap?'':`<div class="small">候选方向：${esc((s.allowed_directions||[]).join('、'))}；用量：${s.usage_audit_available?esc(s.input_tokens)+' 输入 / '+esc(s.output_tokens)+' 输出':'用量未知（CLI 原始事件日志缺失）'}；本地约束检查：${s.local_valid===null?'未运行（未收到模型路线动作）':s.local_valid?'通过':'未通过'}；${s.accepted?'已接纳并执行':s.action_received?'收到动作，候选边未执行':'没有收到模型路线动作'}${s.failure_reason_code&&s.failure_reason_code!=='none'?`；模型返回失败代码：${esc(s.failure_reason_code)}`:''}${s.model_failure_code_overridden_by_local_validation?'；本地校验覆盖模型失败声明':''}；工具事件：${s.tool_audit_available?esc((s.tool_item_types||[]).join(', ')||'无'):'CLI 原始事件日志缺失，无法审计'}${s.errors?.length?`；本地拒绝原因：${esc(s.errors.join('、'))}`:''}${s.route_recovery?`；安全回退：拒绝候选边 ${esc(s.route_recovery.rejected_edge?.from_node_id||'未知')} —${esc(s.route_recovery.rejected_edge?.direction||'?')}→ ${esc(s.route_recovery.rejected_edge?.to_node_id||'未知')}（${esc(s.route_recovery.rejected_edge?.reason||'本地校验失败')}），该边未进入路线；从 ${esc(s.route_recovery.rollback_node_id||'未知')} 回退 ${esc(s.route_recovery.rollback_count_nodes)} 个节点后重新检索${s.route_recovery.removed_accepted_edge?`；撤销上一段已接纳边 ${esc(s.route_recovery.removed_accepted_edge.from_node_id)} → ${esc(s.route_recovery.removed_accepted_edge.to_node_id)}`:''}`:''}${s.self_check_false_fields?.length?`；模型自检为 false：${esc(s.self_check_false_fields.join('、'))}`:''}${s.model_self_check_mismatch_fields?.length?`；自检与本地事实不一致：${esc(s.model_self_check_mismatch_fields.join('、'))}`:''}${s.failure_detail?`；${esc(s.failure_detail)}`:''}${s.accepted?'':`；车辆耗电：${Number(s.vehicle_energy_consumed_kwh||0).toFixed(3)} kWh；${s.action_received?`候选边若执行估算：${s.hypothetical_step_energy_kwh==null?'未知':Number(s.hypothetical_step_energy_kwh).toFixed(3)+' kWh（未实际消耗）'}`:'没有收到路线动作，无法估算候选边能耗'}${s.hypothetical_energy_basis?.reverse_edge_inferred_moves?'；估算依据为反向道路属性':''}${s.hypothetical_energy_basis?.proxy_edge_moves?'；估算依据含地图中位路段属性':''}`}</div>`}</div>`).join('');
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


def _number_or_none(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _load_map_from_preflight(source_preflight):
    """Load inline-map and hash-referenced formal-batch preflights."""
    payload = json.loads(source_preflight.read_text(encoding="utf-8"))
    if isinstance(payload.get("map"), dict):
        return dict(payload["map"])
    map_ref = payload.get("map_ref")
    if not isinstance(map_ref, dict) or not map_ref.get("path"):
        raise RuntimeError("pilot_replay_map_reference_missing")
    map_path = Path(map_ref["path"])
    if not map_path.is_absolute():
        map_path = ROOT / map_path
    if not map_path.is_file():
        raise RuntimeError("pilot_replay_map_reference_file_missing")
    actual_sha256 = hashlib.sha256(map_path.read_bytes()).hexdigest()
    if actual_sha256 != map_ref.get("sha256"):
        raise RuntimeError("pilot_replay_map_reference_hash_mismatch")
    return json.loads(map_path.read_text(encoding="utf-8"))


def _turn_started_count(event_audit):
    if not isinstance(event_audit, dict):
        return None
    value = _number_or_none(event_audit.get("turn_started_count"))
    if value is not None:
        return int(value)
    event_types = event_audit.get("event_types")
    if isinstance(event_types, list):
        return event_types.count("turn.started")
    return None


def _verified_provider_failure_detail(attempt_run_dir, provider_record):
    """Read only concise, recognized failure data from a hash-verified trace."""
    trace_reference = provider_record.get("trace_jsonl_path") or provider_record.get("trace_path")
    expected_hash = provider_record.get("trace_jsonl_sha256") or provider_record.get("trace_sha256")
    if not trace_reference or not expected_hash:
        return None
    trace_path = Path(trace_reference)
    if not trace_path.is_absolute():
        trace_path = Path(attempt_run_dir) / trace_path
    try:
        raw = trace_path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        return None
    messages = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error" and isinstance(event.get("message"), str):
            messages.append(event["message"])
        error = event.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            messages.append(error["message"])
    joined = " ".join(messages).lower()
    if "429" in joined and "too many requests" in joined:
        return "原始 trace 的 SHA-256 已核验：CLI 记录服务端返回 429 Too Many Requests，且内部重试已耗尽。"
    if "selected model is at capacity" in joined:
        return "原始 trace 的 SHA-256 已核验：模型服务容量已满，CLI 未返回路线动作。"
    if "ssl" in joined or "tls" in joined or "handshake" in joined:
        return "原始 trace 的 SHA-256 已核验：CLI 记录 TLS/SSL 连接或握手中断。"
    return None


def _known_latency_metrics(run_states, bootstrap_included, bootstrap_usage, unresolved_invocation):
    total_ms = 0.0
    has_latency = False
    complete = True
    for run_state in run_states:
        usage = run_state.get("usage_totals", {})
        latency = _number_or_none(usage.get("latency_ms"))
        if latency is None:
            latency = _number_or_none(usage.get("known_latency_ms_sum"))
            complete = False
        if latency is not None:
            total_ms += float(latency)
            has_latency = True
        else:
            complete = False
        if usage.get("unknown_latency_request_numbers") or usage.get("unknown_usage_request_numbers"):
            complete = False
    if bootstrap_included:
        latency = _number_or_none(bootstrap_usage.get("latency_ms"))
        if latency is None:
            complete = False
        else:
            total_ms += float(latency)
            has_latency = True
    if (
        isinstance(unresolved_invocation, dict)
        and unresolved_invocation.get("status") == "delivery_and_usage_unknown"
    ):
        complete = False
    return (total_ms if has_latency else None), complete


def _audit_or_attempt_metric(audit, cumulative_key, usage_key, attempts):
    value = _number_or_none(audit.get(cumulative_key))
    if value is not None:
        return value
    known_sum_key = "known_{0}_sum_this_continuation".format(usage_key)
    value = _number_or_none(audit.get(known_sum_key))
    if value is not None:
        return value
    audit_usage = audit.get("usage")
    if isinstance(audit_usage, dict):
        value = _number_or_none(audit_usage.get(usage_key))
        if value is not None:
            return value
    values = [
        _number_or_none(attempt.get("usage", {}).get(usage_key))
        for attempt in attempts
    ]
    if not attempts:
        return 0
    if any(value is None for value in values):
        return None
    return sum(values)


def _api_equivalent_metric(audit, attempts, bootstrap_included):
    value = _number_or_none(
        audit.get("cumulative_api_equivalent_usd_including_bootstrap_and_parent")
    )
    if value is not None:
        return value
    values = []
    for attempt in attempts:
        estimate = attempt.get("provider_record", {}).get("cost_estimate", {})
        value = _number_or_none(estimate.get("api_equivalent_usd"))
        if value is None:
            return None
        values.append(value)
    if bootstrap_included:
        return None
    return sum(values)


def _budget_gate_detail(stop, pickup_seen):
    evaluations = stop.get("candidate_evaluations", [])
    if not evaluations:
        return ""
    direction_names = {"N": "北", "S": "南", "E": "东", "W": "西"}
    completion_bounds = []
    revisited = []
    for evaluation in evaluations:
        direction = evaluation.get("direction")
        name = direction_names.get(direction, direction or "未知")
        if evaluation.get("reason") == "node_revisited":
            revisited.append(name + "向会重访")
            continue
        moves_after_candidate = evaluation.get("minimum_remaining_moves_after_move")
        if moves_after_candidate is not None:
            completion_bounds.append((int(moves_after_candidate) + 1, name))
    completion_bounds.sort()
    parts = [
        "{0}向至少需{1}次".format(name, calls)
        for calls, name in completion_bounds
    ] + revisited
    if not parts:
        return ""
    lower_bound = stop.get(
        "minimum_remaining_moves", stop.get("remaining_minimum_moves")
    )
    if lower_bound is None:
        lower_bound_text = ""
    elif pickup_seen:
        lower_bound_text = "当前拓扑下界 {0} 是乐观值；".format(lower_bound)
    else:
        lower_bound_text = "当前拓扑下界 {0} 是上车段与下车段分别估算的乐观值；".format(lower_bound)
    return lower_bound_text + "逐方向核算（包含下一步）：" + "、".join(parts) + "。"


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
    map_data = _load_map_from_preflight(source_preflight)
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
    delivery_unknown_by_run = {
        (attempt_run_dir.resolve(), request_number)
        for attempt_run_dir, run_audit in audits_by_run.items()
        for request_number in run_audit.get("delivery_unknown_request_numbers", [])
    }
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
        response = attempt.get("response") or {}
        direction = response.get("direction")
        accepted = attempt.get("status") in {"accepted_legal_step", "route_completed"} and bool(attempt.get("selected_edge_id"))
        validation = attempt.get("validation")
        if not isinstance(validation, dict):
            validation = {}
        local = validation.get("deterministic_local_check")
        if not isinstance(local, dict):
            local = {}
        request_unknown = (
            (attempt_run_dir.resolve(), attempt.get("request_number")) in delivery_unknown_by_run
            or bool(attempt.get("provider_record", {}).get("delivery_and_usage_unknown_evidence"))
        )
        false_fields = sorted(k for k, v in response.get("self_check", {}).items() if v is False)
        status_label = attempt.get("status")
        failure_detail = None
        if request_unknown:
            provider_record = attempt.get("provider_record", {})
            event_audit = provider_record.get("event_audit", {})
            turn_started_count = _turn_started_count(event_audit)
            agent_message_count = event_audit.get("agent_message_count")
            if agent_message_count is None:
                agent_message_count = event_audit.get("item_types", []).count("agent_message")
            exit_code = provider_record.get("cli_exit_code")
            signal_label = "SIGTERM" if exit_code == -15 else None
            if turn_started_count:
                status_label = "CLI turn 已启动；未收到路线响应"
                failure_detail = (
                    "CLI 退出码 {0}（{1}）；已观测到 turn.started（{2} 次），"
                    "未观测到 agent_message（{3} 条）或可提交路线动作。投递与用量未知，未重试。"
                    .format(exit_code, signal_label or "信号来源未记录", turn_started_count, agent_message_count)
                )
            else:
                status_label = (
                    "CLI 收到 SIGTERM；没有模型路线响应"
                    if signal_label
                    else "CLI 调用中断；没有模型路线响应"
                )
                failure_detail = (
                    "CLI 退出码 {0}（{1}）；未观测到模型 turn 或路线动作。投递与用量未知，未重试。"
                    .format(exit_code, signal_label or "信号来源未记录")
                )
        provider_failure_detail = _verified_provider_failure_detail(
            attempt_run_dir, attempt.get("provider_record", {})
        )
        if provider_failure_detail:
            failure_detail = " ".join(
                part for part in (provider_failure_detail, failure_detail) if part
            )
            if not direction:
                status_label = (
                    "服务端 429；未收到路线动作"
                    if "429 Too Many Requests" in provider_failure_detail
                    else "模型服务容量已满；未收到路线动作"
                    if "容量已满" in provider_failure_detail
                    else "CLI 连接中断；未收到路线动作"
                )
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
            "to_node": attempt.get("target_node_id") if accepted else (
                local.get("expected_target_node_id") or diagnostic.get("proposed_target_node_id")
            ),
            "edge_id": attempt.get("selected_edge_id") if accepted else attempt.get("allowed_edge_ids", {}).get(direction),
            "candidate_visible_on_map": bool(
                not accepted
                and attempt.get("current_node_id") in {node["node_id"] for node in map_data["nodes"]}
                and (
                    local.get("expected_target_node_id") or diagnostic.get("proposed_target_node_id")
                ) in {node["node_id"] for node in map_data["nodes"]}
            ),
            "direction": direction,
            "action_received": bool(direction),
            "request_delivery_unknown": request_unknown,
            "failure_detail": failure_detail,
            "failure_reason_code": response.get("failure_reason_code"),
            "model_failure_code_overridden_by_local_validation": bool(
                validation.get(
                    "model_failure_code_overridden_by_local_validation"
                )
            ),
            "allowed_directions": attempt.get("allowed_directions", []),
            "status": status_label,
            "accepted": accepted,
            "local_valid": None if request_unknown or not direction else bool(local.get("passed")),
            "self_check_false_fields": false_fields,
            "model_self_check_mismatch_fields": discrepancy.get(
                "model_self_check_mismatch_fields",
                validation.get("model_self_check_mismatch_fields", []),
            ),
            "tool_item_types": None if request_unknown else attempt.get("tool_item_types", []),
            "tool_audit_available": not bool(
                request_unknown
                or
                attempt.get("provider_record", {}).get("partial_final_response_evidence")
                or attempt.get("provider_record", {}).get("delivery_and_usage_unknown_evidence")
            ),
            "input_tokens": attempt.get("usage", {}).get("input_tokens"),
            "output_tokens": attempt.get("usage", {}).get("output_tokens"),
            "usage_audit_available": (
                isinstance(attempt.get("usage", {}).get("input_tokens"), int)
                and isinstance(attempt.get("usage", {}).get("output_tokens"), int)
            ),
            "vehicle_energy_consumed_kwh": (0.0 if request_unknown else diagnostic.get("vehicle_energy_consumed_kwh", 0.0)) if not accepted else None,
            "hypothetical_step_energy_kwh": diagnostic.get("hypothetical_step_energy_kwh") if not accepted else None,
            "hypothetical_energy_basis": diagnostic.get("hypothetical_energy_basis") if not accepted else None,
            "errors": diagnostic.get("errors", local.get("errors", [])) if not accepted else [],
            "route_recovery": attempt.get("route_recovery"),
        })

    bootstrap_included = any(
        bool(item.get("prior_smoke_result") or item.get("prior_action_reconciliation"))
        for item in run_states
    )
    bootstrap_usage = {}
    if bootstrap_included:
        bootstrap = json.loads((ROOT / "outputs/route_layer/r4_codex_smoke_v5/result.json").read_text(encoding="utf-8"))
        bootstrap_usage = bootstrap.get("usage", {})
    auth_limit = int(bootstrap_included) + sum(
        int(item.get("effective_authorized_new_request_limit", item.get("authorized_new_request_limit", 0)) or 0)
        for item in run_states
    )
    last_accepted = next((a for a in reversed(all_attempts) if a.get("status") == "accepted_legal_step"), None)
    last_validation = (last_accepted or {}).get("validation")
    if not isinstance(last_validation, dict):
        last_validation = {}
    last_local = last_validation.get("deterministic_local_check")
    if not isinstance(last_local, dict):
        last_local = {}
    min_remaining = (
        last_local.get("remaining_minimum_moves_after_move")
        if last_accepted else state.get("initial_remaining_minimum_moves")
    )
    accepted_self_check_discrepancies = [
        row for row in self_check_discrepancies if row.get("action_executed")
    ]
    unresolved_invocation = audit.get("unresolved_cli_invocation") or state.get("unresolved_cli_invocation")
    latency_ms, latency_complete = _known_latency_metrics(
        run_states, bootstrap_included, bootstrap_usage, unresolved_invocation
    )
    metrics = {
        "accepted_moves": len(state["accepted_edges"]),
        "current_node": state["accepted_nodes"][-1],
        "pickup_seen": bool(state["pickup_seen"]),
        "minimum_remaining_moves": audit.get("minimum_remaining_moves_at_stop", min_remaining),
        "new_requests": audit.get("cumulative_requests_including_bootstrap_and_parent", int(bootstrap_included) + len(all_attempts)),
        "bootstrap_route_action_included": bootstrap_included,
        "authorized_limit": auth_limit,
        "cli_invocations": audit.get("codex_cli_invocations_this_continuation", len(all_attempts)),
        "cli_invocations_without_model_turn": int(
            audit.get(
                "cli_invocations_without_model_turn",
                sum(
                    1
                    for attempt in all_attempts
                    if attempt.get("provider_record", {}).get("delivery_and_usage_unknown_evidence")
                    and _turn_started_count(attempt.get("provider_record", {}).get("event_audit", {})) == 0
                ),
            ) or 0
        ),
        "token_usage_complete": audit.get("complete_total_tokens_this_continuation") is not None,
        "current_authorization_status": state.get("current_authorization_status"),
        "additional_model_requests_authorized": state.get("additional_model_requests_authorized"),
        "retries": sum(int(item.get("retry_count", 0) or 0) for item in run_states),
        "tool_events": audit.get(
            "tool_events", sum(bool(attempt.get("tool_item_types")) for attempt in all_attempts)
        ),
        "tool_audit_unknown_request_count": int(
            len(audit.get("tool_audit_unknown_request_numbers", []))
            if "tool_audit_complete" in audit
            else sum(
                bool(
                    attempt.get("provider_record", {}).get("partial_final_response_evidence")
                    or attempt.get("provider_record", {}).get("delivery_and_usage_unknown_evidence")
                )
                for attempt in all_attempts
            )
        ),
        "stopped_actions": stopped,
        "self_check_discrepancy_count": len(self_check_discrepancies),
        "accepted_self_check_discrepancy_count": len(accepted_self_check_discrepancies),
        "model_failure_code_override_count": int(
            audit.get("model_failure_code_override_count", 0) or 0
        ),
        "self_check_discrepancies": self_check_discrepancies,
        "rejected_vehicle_energy_kwh": sum(float(row.get("vehicle_energy_consumed_kwh", 0) or 0) for row in stopped),
        "hypothetical_rejected_energy_kwh": sum(float(row.get("hypothetical_step_energy_kwh", 0) or 0) for row in stopped if row.get("hypothetical_step_energy_kwh") is not None),
        "input_tokens": _audit_or_attempt_metric(
            audit,
            "cumulative_input_tokens_including_bootstrap_and_parent",
            "input_tokens",
            all_attempts,
        ),
        "output_tokens": _audit_or_attempt_metric(
            audit,
            "cumulative_output_tokens_including_bootstrap_and_parent",
            "output_tokens",
            all_attempts,
        ),
        "api_equivalent_usd": _api_equivalent_metric(
            audit, all_attempts, bootstrap_included
        ),
        "latency_ms": latency_ms,
        "latency_complete": latency_complete,
        "prefix_distance_m": state.get("route_metrics", {}).get("accepted_prefix_distance_m"),
        "prefix_energy_kwh": state.get("route_metrics", {}).get("accepted_prefix_energy_kwh"),
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
    elif (
        state.get("status") not in {"route_complete", "completed_route", "stopped_illegal_or_invalid_action", "stopped_tool_event", "stopped_request_error"}
        and isinstance(unresolved_invocation, dict)
        and unresolved_invocation.get("status") == "delivery_and_usage_unknown"
    ):
        request_number = unresolved_invocation.get("request_number")
        request_label = "第 {0} 次".format(request_number) if request_number is not None else "一次"
        notice = (
            "{0} CLI 调用在运行期间中断，没有收到响应回执；投递与用量未知。"
            "该调用不记作模型失败或零用量，也没有路线动作被接纳。"
            "回放仅展示此前 {1} 个通过本地校验的动作；取送任务仍未完成。"
        ).format(request_label, len(state.get("accepted_edges", [])))
        if accepted_self_check_discrepancies:
            notice += "另有 {0} 次模型自检与本地事实不一致，动作是否接纳以本地确定性校验为准。".format(
                len(self_check_discrepancies)
            )
    else:
        if state.get("status") == "quota_cap_reached":
            guard = state.get("account_usage_guard", {})
            stop = state.get("stop_diagnostics", {})
            before = guard.get("baseline_weekly_used_percent")
            after = guard.get("observed_weekly_used_percent_after_request")
            threshold = guard.get("stop_threshold_percent")
            usage_change = (
                "{0}% → {1}%（停止线 {2}%）".format(before, after, threshold)
                if before is not None and after is not None and threshold is not None
                else "达到本次配置的停止线"
            )
            prefix_energy = metrics.get("prefix_energy_kwh")
            prefix_energy_label = (
                "{0:.3f} kWh（仿真）".format(float(prefix_energy))
                if prefix_energy is not None else "未评估"
            )
            notice = (
                "账户周用量监控达到本次试跑停止线：用量快照 {0}；已发出 {1} 次请求后停止。"
                "账户额度是共享的，快照按整数显示且可能延迟，不能单独归因于本次路线请求。"
                "路线未完成；当前位于 {2}，到任务完成至少还需 {3} 步。"
                "本页只展示已接纳合法前缀，仿真能耗为 {4}，不代表完整路线能耗。"
            ).format(
                usage_change,
                guard.get("requests_used_when_stopped", metrics["new_requests"]),
                stop.get("current_node", metrics["current_node"]),
                stop.get("remaining_minimum_moves", audit.get("minimum_remaining_moves_at_stop", min_remaining)),
                prefix_energy_label,
            )
        elif state.get("status") in {
            "authorized_call_limit_exhausted",
            "insufficient_authorized_budget_for_remaining_route",
            "remaining_route_unreachable",
        }:
            stop = state.get("stop_diagnostics", {})
            if (
                state.get("status") == "insufficient_authorized_budget_for_remaining_route"
                and stop.get("reason")
                == "every_available_next_direction_was_filtered_by_remaining_authorized_budget"
            ):
                prefix_energy = metrics.get("prefix_energy_kwh")
                prefix_energy_label = (
                    "{0:.3f} kWh（仿真）".format(float(prefix_energy))
                    if prefix_energy is not None else "未评估"
                )
                notice = (
                    "剩余请求预算不足，门禁在请求前停止：第 {0} 次模型请求未发送。"
                    "已发出 {1}/{2} 次请求，当前位置 {3}，剩余授权请求 {4} 次；"
                    "所有动态候选方向执行后都无法在剩余请求预算内保证完成上车和下车。"
                    "此前已接纳动作均通过有向边校验；路线未完成，只报告合法前缀仿真能耗 {5}。"
                ).format(
                    stop.get("request_number", int(state.get("new_requests_used", 0)) + 1),
                    state.get("new_requests_used"),
                    state.get("authorized_new_request_limit"),
                    stop.get("current_node_id", metrics["current_node"]),
                    stop.get("remaining_authorized_calls"),
                    prefix_energy_label,
                )
                detail = _budget_gate_detail(stop, bool(state.get("pickup_seen")))
                if detail:
                    notice += " " + detail
            elif state.get("status") == "authorized_call_limit_exhausted":
                budget_explanation = "本轮授权额度已用完"
            elif state.get("status") == "remaining_route_unreachable":
                budget_explanation = "按当前已访问节点约束，剩余目的地已不可达"
            else:
                budget_explanation = "剩余授权请求数不足以覆盖最短剩余路线"
            if not (
                state.get("status") == "insufficient_authorized_budget_for_remaining_route"
                and stop.get("reason")
                == "every_available_next_direction_was_filtered_by_remaining_authorized_budget"
            ):
                notice = (
                    "{0}（已用 {1}/{2} 次）；路线仍未完成取送，当前节点到完成任务至少还需 {3} 步（拓扑下界）。"
                    "只报告本轮已接纳合法前缀的仿真能耗，不作为正式多种子路线结果。"
                ).format(
                    budget_explanation,
                    state.get("new_requests_used"),
                    state.get("authorized_new_request_limit"),
                    audit.get("minimum_remaining_moves_at_stop", min_remaining),
                )
        elif state.get("status") == "stopped_by_user_instruction":
            notice = (
                "本次按当前调用授权边界停止，没有继续发出模型请求。"
                "已有路线只到达合法前缀；当前节点 {0}，尚未到上车点，完整路线结果仍待新的明确授权。"
            ).format(metrics["current_node"])
        else:
            notice = "该独立 Codex 试跑仍未完成上车与下车任务；只报告已接纳合法前缀的仿真能耗，不作为正式多种子路线结果。"
        if self_check_discrepancies:
            notice += "另有 {0} 次模型自检与本地事实不一致，动作是否接纳以本地确定性校验为准。".format(
                len(self_check_discrepancies)
            )
    if (
        isinstance(unresolved_invocation, dict)
        and unresolved_invocation.get("status") == "delivery_and_usage_unknown"
        and "投递与用量未知" not in notice
    ):
        request_number = unresolved_invocation.get("request_number")
        request_label = "第 {0} 次".format(request_number) if request_number is not None else "一次"
        notice += (
            "另有{0} CLI 调用因中断未收到回执，投递与用量未知；"
            "它未计作路线动作，且用量与工具审计不作零值处理。"
        ).format(request_label)
    if bootstrap_included and state.get("accepted_edges"):
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
    artifact_names = [
        "state.json", "manifest.json", "final_audit.json",
        "codex_usage_cost_reconciliation.json", "authorization_boundary_audit.json",
        "usage_reconciliation.json", "offline_reconciliation.json", "traces/index.html",
    ]
    artifacts = [name for name in artifact_names if (run_dir / name).is_file()]
    payload = {
        "artifacts": artifacts,
        "map": map_data,
        "task": {"task_id": state["task_id"], "start_node_id": state["start_node_id"], "pickup_node_id": state["pickup_node_id"], "dropoff_node_id": state["dropoff_node_id"]},
        "steps": steps, "metrics": metrics, "notice": notice,
        "unresolved_cli_invocation": unresolved_invocation,
        "correction": state.get("prior_action_reconciliation"), "task_adjustment": state.get("task_adjustment"),
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        TEMPLATE.replace("__DATA__", data)
        .replace("__MAX_STEP__", str(len(steps) - 1))
        .replace("__MAP_SIZE__", str(map_data["size"])),
        encoding="utf-8",
    )

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
