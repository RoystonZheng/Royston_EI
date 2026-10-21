#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render a stored R4 smoke result as a self-contained interactive replay."""

import argparse
import html
import json
from pathlib import Path


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>R4 路线检索烟雾回放</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#1f2937;background:#f4f6fb}
body{margin:0}header{padding:18px 24px;background:#172554;color:white}h1{margin:0 0 7px;font-size:23px}.sub{opacity:.9;font-size:14px}
.notice{margin:14px 18px 0;padding:11px 14px;background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;color:#7c2d12}
main{display:grid;grid-template-columns:minmax(560px,1.25fr) minmax(360px,.75fr);gap:14px;padding:14px 18px}
.card{background:white;border:1px solid #dbe3f0;border-radius:10px;padding:14px;box-shadow:0 2px 8px #17255412;min-width:0}
canvas{display:block;width:100%;height:min(70vh,720px);min-height:480px;border:1px solid #dbe3f0;border-radius:8px;background:#fbfdff}
.controls{display:flex;align-items:center;gap:10px;margin:10px 0}.controls input{flex:1}.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:#475569}
.key{display:inline-flex;align-items:center;gap:5px}.dot{width:10px;height:10px;border-radius:50%;display:inline-block}.line{width:20px;border-top:3px solid;display:inline-block}.green{background:#0f766e}.orange{background:#f59e0b}.blue{background:#2563eb}.redline{border-color:#dc2626;border-top-style:dashed}.purple{border-color:#7c3aed;border-top-style:dashed}.gray{border-color:#94a3b8}
h2{font-size:17px;margin:4px 0 10px}.attempts{display:flex;gap:8px;margin-bottom:12px}.attempts button{padding:7px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#f8fafc;cursor:pointer;font:inherit}.attempts button[aria-pressed=true]{background:#dbeafe;border-color:#2563eb;color:#1d4ed8}
.grid{display:grid;grid-template-columns:130px 1fr;gap:7px 10px;font-size:13px}.label{color:#64748b}.value{overflow-wrap:anywhere}.bad{color:#b91c1c;font-weight:600}.good{color:#047857;font-weight:600}
.panel{border-top:1px solid #e5e7eb;margin-top:13px;padding-top:12px}.errors{color:#991b1b;padding-left:20px;margin:6px 0}.small{font-size:12px;color:#64748b;line-height:1.55}.pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;padding:9px;border-radius:6px;font:12px ui-monospace,monospace;max-height:180px;overflow:auto}
.check-table-wrap{overflow-x:auto}.check-table{width:100%;border-collapse:collapse;font-size:12px}.check-table th,.check-table td{padding:6px 7px;border-bottom:1px solid #e5e7eb;text-align:left;white-space:nowrap}.check-table th{color:#64748b;font-weight:600}.check-pass{color:#047857;font-weight:600}.check-fail{color:#b91c1c;font-weight:600}
.links a{color:#1d4ed8;margin-right:14px}.feature-list{font-size:12px;color:#475569;line-height:1.5}
@media(max-width:980px){main{grid-template-columns:1fr}canvas{height:60vh;min-height:430px}}
</style></head><body>
<header><h1>R4 路线检索与运行轨迹</h1><div class="sub" id="subtitle"></div></header>
<div class="notice" id="notice"></div>
<main><section class="card">
<canvas id="map" width="1000" height="760" aria-label="道路地图和路线回放"></canvas>
<div class="controls"><label for="step">回放步数：<output id="step-label">0</output></label><input id="step" type="range" min="0" max="0" value="0"></div>
<div class="legend">
<span class="key"><i class="dot orange"></i>起点/上车/下车</span><span class="key"><i class="dot blue"></i>道路特征</span>
<span class="key"><i class="line green"></i>违规前路线段</span><span class="key"><i class="line redline"></i>违规移动</span>
<span class="key"><i class="line purple"></i>违规后的模型提议</span><span class="key"><i class="line gray"></i>地图有向道路</span>
</div><p class="feature-list" id="features"></p>
</section><aside class="card">
<h2>任务与地点解析</h2><div class="grid" id="task-info"></div>
<div class="panel"><h2>每轮路线请求</h2><div class="attempts" id="attempt-buttons"></div><div class="grid" id="attempt-info"></div></div>
<div class="panel"><h2>路线限制校验</h2><div id="validation" class="grid"></div></div>
<div class="panel"><h2>逐步路线自检</h2><p class="small">模型自检是结构化声明，不是隐藏思维链；每一步都由本地程序独立复核，只有通过才更新路线状态。</p><div id="step-checks"></div></div>
<div class="panel"><h2>反馈内容</h2><pre class="pre" id="feedback"></pre></div>
<div class="panel"><h2>重试请求格式审计</h2><div class="grid" id="retry-audit"></div><p class="small" id="retry-audit-note"></p></div>
<div class="panel"><h2>能耗与费用</h2><div class="grid" id="cost-info"></div><p class="small" id="energy-note"></p></div>
<div class="panel"><h2>实验状态</h2><div class="grid" id="final-info"></div></div>
<div class="panel links"><a href="__RESULT_JSON_NAME__">查看本次烟雾 JSON</a><a href="posthoc_reachability_audit.json" id="reach-link">查看事后可达性审计</a><a href="retry_input_and_map_audit.json" id="retry-link">查看离线格式审计</a></div>
</aside></main>
<script>
const DATA=__PAYLOAD__;
const ROUTE_MODEL=DATA[DATA.route_model_backend||'deepseek']||DATA.deepseek||{};
const map=DATA.map, task=DATA.task_public, attempts=ROUTE_MODEL.attempts||[];
const retryAudit=DATA.retry_input_and_map_audit;
if(!DATA.posthoc_reachability_audit)document.getElementById('reach-link').hidden=true;
if(!retryAudit)document.getElementById('retry-link').hidden=true;
const q=DATA.qwen.parsed_intent, canvas=document.getElementById('map'),ctx=canvas.getContext('2d');
const nodeById=Object.fromEntries(map.nodes.map(n=>[n.node_id,n]));
const boundary=map.boundary, start=task.start_node_id;
const pickup=(q.pickup.candidate_entity_refs||[])[0],dropoff=(q.dropoff.candidate_entity_refs||[])[0];
let selected=0;
function el(id){return document.getElementById(id)}
function row(container,label,value,cls=''){const a=document.createElement('div'),b=document.createElement('div');a.className='label';a.textContent=label;b.className='value '+cls;b.textContent=value==null?'—':String(value);container.append(a,b)}
function failureLabel(code){if(code==='model_confirmed_no_directed_path')return '模型声称有向图不可达；未经独立可达性验证';if(code==='model_unable_to_construct_route')return '模型未构造出路线；没有报告已确认无路';if(code==='model_declared_no_route'||code==='llm_declined_route')return '模型只返回 failed，未说明原因；这不是地图无路证明';return code||'模型未提供失败原因'}
function stepErrorLabel(code){return ({directed_edge_missing:'该方向没有地图有向边',edge_not_allowed_from_current:'边起点不是当前节点',target_outside_map:'目标点越界',blocked_edge:'道路被禁行',dropoff_before_pickup:'上车前到达下车点',node_revisited:'重复经过已访问节点',move_after_dropoff:'到达下车点后仍继续移动',move_leaves_no_reachable_route:'该动作会使剩余路线不可达',move_exceeds_remaining_action_budget:'该动作会使路线超出剩余调用预算',remaining_route_unreachable:'当前已无符合不重复节点约束的续路',remaining_action_budget_insufficient:'剩余调用预算不足以完成路线',no_safe_continuation:'没有同时满足可达性与预算的后续动作',give_up_while_route_reachable:'仍有可行续路，不能放弃',model_self_check_failed:'模型自检未全部通过',r4_step_output_json_invalid:'模型返回内容不是有效 JSON',r4_step_direction_invalid:'模型返回了无效方向'})[code]||code||'无拒绝原因'}
function pos(n){return [48+(n.x-boundary.min_x)/(Math.max(1,boundary.max_x-boundary.min_x))*904,710-(n.y-boundary.min_y)/(Math.max(1,boundary.max_y-boundary.min_y))*660]}
function arrow(a,b,color){const dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy);if(!len)return;const ux=dx/len,uy=dy/len,x=a[0]+dx*.68,y=a[1]+dy*.68;ctx.fillStyle=color;ctx.beginPath();ctx.moveTo(x+ux*5,y+uy*5);ctx.lineTo(x-ux*4+uy*4,y-uy*4-ux*4);ctx.lineTo(x-ux*4-uy*4,y-uy*4+ux*4);ctx.closePath();ctx.fill()}
function draw(){
 const a=attempts[selected]||{}, replay=a.replay||(a.partial_proposal_audit||{}).replay||{},path=replay.attempted_route_node_sequence||[],pairs=replay.proposed_edge_pairs||[],violations=replay.move_violations||[],nstep=Number(el('step').value||0);
 ctx.clearRect(0,0,canvas.width,canvas.height);ctx.strokeStyle='#eef2f7';ctx.lineWidth=1;
 for(let x=boundary.min_x;x<=boundary.max_x;x++){const p=pos({x,y:boundary.min_y}),q=pos({x,y:boundary.max_y});ctx.beginPath();ctx.moveTo(...p);ctx.lineTo(...q);ctx.stroke()}
 for(let y=boundary.min_y;y<=boundary.max_y;y++){const p=pos({x:boundary.min_x,y}),q=pos({x:boundary.max_x,y});ctx.beginPath();ctx.moveTo(...p);ctx.lineTo(...q);ctx.stroke()}
 for(const e of map.edges){const na=nodeById[e.from_node],nb=nodeById[e.to_node];if(!na||!nb)continue;const p=pos(na),q=pos(nb);ctx.strokeStyle='#cbd5e1';ctx.lineWidth=1.3;ctx.beginPath();ctx.moveTo(...p);ctx.lineTo(...q);ctx.stroke();arrow(p,q,'#94a3b8')}
 const vmap=new Map(violations.map(v=>[Number(v.step),v]));
 const totalSteps=pairs.length||Math.max(0,path.length-1);
 for(let i=1;i<=Math.min(nstep,totalSteps);i++){const pair=pairs.length?pairs[i-1]:{from_node_id:path[i-1],to_node_id:path[i]},na=nodeById[pair.from_node_id],nb=nodeById[pair.to_node_id],v=vmap.get(i);if(!na)continue;const p=pos(na);if(!nb){if(v){ctx.strokeStyle='#b91c1c';ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(p[0]-8,p[1]-8);ctx.lineTo(p[0]+8,p[1]+8);ctx.moveTo(p[0]+8,p[1]-8);ctx.lineTo(p[0]-8,p[1]+8);ctx.stroke();ctx.fillStyle='#991b1b';ctx.font='bold 13px sans-serif';ctx.fillText('非法方向第'+i+'步',p[0]+9,p[1]+17)}continue}const q=pos(nb),disconnected=v&&v.kind==='route_edge_node_discontinuity',afterViolation=replay.first_violation_step&&i>replay.first_violation_step;ctx.beginPath();ctx.moveTo(...p);ctx.lineTo(...q);if(disconnected||afterViolation){ctx.strokeStyle='#7c3aed';ctx.lineWidth=4;ctx.setLineDash([7,5])}else if(v){ctx.strokeStyle='#dc2626';ctx.lineWidth=5;ctx.setLineDash([7,5])}else{ctx.strokeStyle='#0f766e';ctx.lineWidth=4;ctx.setLineDash([])}ctx.stroke();ctx.setLineDash([]);arrow(p,q,(disconnected||afterViolation)?'#7c3aed':(v?'#dc2626':'#0f766e'));if(v){const marker=disconnected?p:q;ctx.strokeStyle='#b91c1c';ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(marker[0]-8,marker[1]-8);ctx.lineTo(marker[0]+8,marker[1]+8);ctx.moveTo(marker[0]+8,marker[1]-8);ctx.lineTo(marker[0]-8,marker[1]+8);ctx.stroke();ctx.fillStyle='#991b1b';ctx.font='bold 13px sans-serif';ctx.fillText(disconnected?'路线断点第'+i+'步':'违规第'+i+'步',marker[0]+9,marker[1]+17)}}
 for(const f of map.roadside_features||[]){const n=nodeById[f.node_id];if(!n)continue;const p=pos(n);ctx.fillStyle='#2563eb';ctx.beginPath();ctx.rect(p[0]-5,p[1]-5,10,10);ctx.fill();ctx.fillStyle='#1e3a8a';ctx.font='12px sans-serif';ctx.fillText(f.label,p[0]+7,p[1]-7)}
 for(const [id,label,color] of [[start,'起点','#f59e0b'],[pickup,'上车','#16a34a'],[dropoff,'下车','#db2777']]){const n=nodeById[id];if(!n)continue;const p=pos(n);ctx.fillStyle=color;ctx.beginPath();ctx.arc(p[0],p[1],9,0,Math.PI*2);ctx.fill();ctx.strokeStyle='white';ctx.lineWidth=2;ctx.stroke();ctx.fillStyle='#111827';ctx.font='bold 12px sans-serif';ctx.fillText(label+' '+id,p[0]+10,p[1]-8)}
 if(pairs.length){const pair=pairs[Math.min(nstep,pairs.length)-1],cur=pair?(nodeById[pair.to_node_id]||nodeById[pair.from_node_id]):nodeById[start];if(cur){const p=pos(cur);ctx.strokeStyle='#92400e';ctx.lineWidth=2;ctx.beginPath();ctx.arc(p[0],p[1],15,0,Math.PI*2);ctx.stroke()}}else if(path.length){const cur=nodeById[path[Math.min(nstep,path.length-1)]];if(cur){const p=pos(cur);ctx.strokeStyle='#92400e';ctx.lineWidth=2;ctx.beginPath();ctx.arc(p[0],p[1],15,0,Math.PI*2);ctx.stroke()}}
 el('step-label').value=nstep+'/'+(replay.move_count||0)
}
function selectAttempt(i){
 if(!attempts.length){selected=0;el('attempt-info').textContent='尚无模型请求记录；当前页面展示的是本地预检地图。';el('validation').textContent='本地确定性校验代码已就绪，尚未进行真实模型验证。';el('step-checks').textContent='没有 API 提议可回放。';el('feedback').textContent='没有 API 调用。';el('cost-info').textContent='没有模型请求或路线提议。';el('energy-note').textContent='尚无通过校验的路线，实际路线能耗未计算。';draw();return;}
 selected=i;const a=attempts[i],p=a.parsed_output||{},partial=a.partial_proposal_audit||{},replay=a.replay||partial.replay||{},e=a.proposal_energy_estimate||partial.energy_estimate||{},v=a.validation||a.local_checks||{},feed=a.feedback;
 document.querySelectorAll('#attempt-buttons button').forEach((b,j)=>b.setAttribute('aria-pressed',j===i?'true':'false'));
 const slider=el('step');slider.max=String(replay.move_count||0);slider.value=slider.max;slider.disabled=Number(slider.max)===0;
 const completeResponse=(partial.interpretation||'').startsWith('complete model response');
 const info=el('attempt-info');info.replaceChildren();row(info,'状态',a.status,['step_invalid','step_output_invalid','api_error','model_gave_up'].includes(a.status)?'bad':'');row(info,'模型',a.model);row(info,'输入 / 输出 token',`${a.input_tokens??'—'} / ${a.output_tokens??'—'}`);row(info,'请求输入字节',a.input_bytes);row(info,'输入内容 SHA-256',a.input_user_sha256);row(info,'当前节点',a.request_state?.n||'—');row(info,'到达上车点',a.request_state?.p==null?'—':(a.request_state.p?'是':'否'));row(info,'允许方向',Array.isArray(a.allowed_directions)?(a.allowed_directions.join('、')||'无'):'—');row(info,'被本地过滤的方向',Object.entries(a.filtered_directions||{}).map(([dir,reason])=>`${dir}（${stepErrorLabel(reason)}）`).join('、')||'无');row(info,'剩余路线步数下界 / 调用预算',`${a.remaining_minimum_moves??'—'} / ${a.remaining_action_budget??a.request_state?.b??'—'}`);row(info,'方向提议',p.direction||a.direction||'无');row(info,'时延',Number.isFinite(Number(a.latency_ms))?`${Number(a.latency_ms).toFixed(3)} ms`:'—');row(info,'finish_reason',a.finish_reason||'—');row(info,'网络重试',a.retries??'—');row(info,DATA.route_model_backend==='codex_cli'?'Codex 额度费用':'API 费用估算',Number.isFinite(Number(a.api_cost_estimate_amount))?`${Number(a.api_cost_estimate_amount).toFixed(6)} CNY`:(DATA.route_model_backend==='codex_cli'?'CLI 未提供金额':'—'));row(info,'输出硬约束',a.output_constraint_mode||'未启用');row(info,'当前 schema 哈希',a.constraint_schema_sha256||'—');const providerAudit=a.provider_audit||{};if(providerAudit.backend==='codex_cli'){row(info,'工具事件',providerAudit.event_audit?.tool_usage_detected?providerAudit.event_audit.tool_item_types.join('、'):'无');row(info,'Codex 原始事件记录',providerAudit.trace_jsonl_path||a.request_id||'—');}row(info,'本地拒绝原因',a.parse_error||(a.local_checks?.errors||[]).join(', ')||'—');const shownRoute=p.direction||a.direction||(p.move_sequence||(p.edge_slot_sequence?(p.edge_slot_sequence.match(/.{1,3}/g)||[]).join(' '):(partial.recovered_edge_slot_sequence?(partial.recovered_edge_slot_sequence.match(/.{1,3}/g)||[]).join(' '):'')));row(info,'本步路线提议',shownRoute||'没有可恢复的移动');
 const val=el('validation');val.replaceChildren();if(a.local_checks){const title=document.createElement('p');title.className='small '+(a.local_checks.passed?'good':'bad');title.textContent=a.local_checks.passed?'本地校验通过；该动作已接纳并推进状态。':'本地校验拒绝；当前位置和已访问状态保持不变。';val.append(title);for(const [key,passed] of Object.entries(a.local_checks.flags||{})){row(val,key,passed?'通过':'未通过',passed?'good':'bad')}for(const error of a.local_checks.errors||[]){row(val,'拒绝原因',stepErrorLabel(error),'bad')}}else if(v.errors&&v.errors.length){const ul=document.createElement('ul');ul.className='errors';for(const x of v.errors){const li=document.createElement('li');li.textContent=x;ul.append(li)}val.append(ul)}else{const t=document.createElement('p');t.className='small';t.textContent=partial.complete_slot_count?(completeResponse?'模型返回完整 JSON，但本地有向边/连续性校验拒绝接纳。下方为离线重建的提议轨迹，不代表车辆实际行驶。':'响应不完整或格式异常；下方仅回放已恢复的完整槽码，不代表车辆实际行驶。'):'本轮没有完整路线提议，因此没有完整路线校验结论。';val.append(t)}
 const checkContainer=el('step-checks');checkContainer.replaceChildren();const checkRecords=(replay.step_check_records)||[];if(checkRecords.length){const wrap=document.createElement('div');wrap.className='check-table-wrap';const table=document.createElement('table');table.className='check-table';const head=document.createElement('tr');for(const label of ['步','有向边','模型声明','地图出边','连续','端点顺序','接纳']){const th=document.createElement('th');th.textContent=label;head.append(th)}const thead=document.createElement('thead');thead.append(head);table.append(thead);const tbody=document.createElement('tbody');for(const record of checkRecords){const tr=document.createElement('tr');const values=[record.step,`${record.from_node_id||'—'} → ${record.to_node_id||'—'}`,record.model_edge_check,record.local_directed_edge_check,record.local_continuity_check,record.local_endpoint_order_check,record.accepted_step?'是':'否'];for(let j=0;j<values.length;j++){const td=document.createElement('td');td.textContent=String(values[j]);if(j>=2)td.className=values[j]==='PASS'||values[j]==='是'?'check-pass':'check-fail';tr.append(td)}tbody.append(tr)}table.append(tbody);wrap.append(table);checkContainer.append(wrap)}else{const note=document.createElement('p');note.className='small';note.textContent=a.status==='model_returned_failed'?'模型未提出路线，本轮没有逐步路线自检。':'该记录没有可回放的完整路线逐步自检。';checkContainer.append(note)}
 const selfCheckInfo=partial.model_step_checks_available?`模型返回 ${partial.model_step_check_count} 个自检标记；路线有 ${partial.complete_slot_count} 步。${partial.model_step_check_count_matches_route?'数量一致，但自检仍需本地复核。':'数量不一致，不能逐步对应。'}\n原始自检序列：${partial.model_step_check_sequence}`:'';
 const stepAudit=a.model_self_check?{本步请求状态:a.request_state||null,模型自检声明:a.model_self_check,校验反馈:feed||null}:null;el('feedback').textContent=stepAudit?JSON.stringify(stepAudit,null,2):(feed?JSON.stringify(feed,null,2):(partial.complete_slot_count?(selfCheckInfo||'模型未返回可识别的逐步自检序列。'):'无结构化重试反馈。'));
 const costs=el('cost-info');costs.replaceChildren();row(costs,'提议序列距离',e.estimated_route_distance_m==null?'—':`${Number(e.estimated_route_distance_m).toFixed(3)} m`);row(costs,partial.complete_slot_count?(completeResponse?'违规提议假设能耗':'不完整提议假设能耗'):'提议路线能耗估算',e.route_energy_estimate_kwh==null?'未计算':`${Number(e.route_energy_estimate_kwh).toFixed(6)} kWh`,'bad');row(costs,'合法前缀能耗估算',e.validated_prefix_energy_estimate_kwh==null?'—':`${Number(e.validated_prefix_energy_estimate_kwh).toFixed(6)} kWh`);row(costs,'假设电费',e.hypothetical_electricity_cost_amount==null?'—':`${Number(e.hypothetical_electricity_cost_amount).toFixed(6)} CNY`);if(partial.complete_slot_count){row(costs,'回放槽码数',partial.complete_slot_count);row(costs,'不在合法有向边集槽码',partial.invalid_edge_slot_count);row(costs,'边连续性失败步数',partial.continuity_violation_count);if(partial.model_step_checks_available)row(costs,'模型自检数量/路线步数',`${partial.model_step_check_count}/${partial.complete_slot_count}`,partial.model_step_check_count_matches_route?'':'bad')}
 el('energy-note').textContent=e.interpretation||'没有完整且通过验证的路线，因此实际路线耗电未计算。';draw();
}
if(retryAudit){
 const retryValidation=retryAudit.corrected_retry_api_validation;
 const auditInfo=el('retry-audit');
 row(auditInfo,'v9 第二轮历史输入','无效 JSON：'+retryAudit.v9_legacy_retry_parse_error,'bad');
 row(auditInfo,'证据来源','由已保存的首轮 JSON、校验反馈和旧版拼接代码离线重建');
 row(auditInfo,'修复格式','反馈放入 JSON 顶层 r4_validation_feedback 字段','good');
 row(auditInfo,'修复后输入 JSON',retryAudit.fixed_retry_message_parseable_json?'解析通过；'+retryAudit.fixed_retry_user_json_bytes+' 字节':'未通过','good');
 if(retryValidation){
  const routeSummary=retryValidation.route_valid?'路线校验通过':'路线校验失败';
  const violation=retryValidation.first_violation;
  const directionAudit=retryAudit.directional_constraint_audit;
  el('notice').textContent='单次烟雾复测，不是正式实验样本。修正格式的反馈请求已发送 1 次，输入 JSON 可解析；模型返回路线，但'+routeSummary+(violation?'，第 '+violation.step+' 步 '+violation.kind+'：'+violation.from_node_id+' → '+violation.to_node_id:'')+(directionAudit?'；请求中 '+directionAudit.move.from_node_id+' 的 '+directionAudit.move.direction+' 槽明确为“'+directionAudit.source_direction_slot+'”，表示无该方向道路':'')+'。v9 的旧第二轮曾返回 no_valid_route；本次确认修正格式能完成请求，但不能单独确定旧失败由格式错误造成。';
  row(auditInfo,'修复后 API 验证','已发送 1 次；'+retryValidation.response_status,'bad');
  row(auditInfo,'修复后路线结果',routeSummary+(violation?'；第 '+violation.step+' 步 '+violation.kind:''),'bad');
  if(directionAudit){row(auditInfo,'道路方向编码复核',directionAudit.move.from_node_id+' 的 '+directionAudit.move.direction+' 槽='+JSON.stringify(directionAudit.source_direction_slot)+'，无该方向边；反向 '+directionAudit.reverse_move.direction+' 槽='+directionAudit.reverse_direction_slot+' 且为单行边','bad');}
  row(auditInfo,'本次 API 费用估算',Number(retryValidation.api_cost_estimate_amount).toFixed(6)+' CNY');
  el('retry-audit-note').textContent='本次请求的输入、响应、校验与 token 记录见本页对应单次结果 JSON。非法提议的耗电仅作假设估算，不计为有效路线实耗；原始 v9 产物未覆盖。';
 }else{
  el('notice').textContent='烟雾结果，不是正式实验样本。第一轮路线违规，第二轮模型返回失败。离线重建发现旧版把反馈拼在 JSON 对象之后，第二轮输入不是合法 JSON（'+retryAudit.v9_legacy_retry_parse_error+'）。代码已修复为顶层 r4_validation_feedback 字段，但没有重新调用 API 验证；不能据此认定格式问题就是模型失败的直接原因。事后有向图审计确认任务可达，该审计没有发给模型，也没有用于路线回退。';
  row(auditInfo,'模型验证','尚未重新调用 API','bad');
  el('retry-audit-note').textContent='原始 v9 API 结果保持不变；审计不代表服务端收到的 HTTP 原始字节已被单独归档，也不证明格式缺陷造成了 no_valid_route。';
 }
}else{
 const reach=DATA.posthoc_reachability_audit;
 const firstAttempt=(ROUTE_MODEL.attempts||[])[0];
 const reason=firstAttempt&&firstAttempt.parsed_output&&firstAttempt.parsed_output.reason;
 if(DATA.route_search&&DATA.route_search.generation_mode==='stepwise'){
  const deep=ROUTE_MODEL||{},usage=deep.usage||{},calls=deep.model_invocation_count??deep.request_count??0,limit=deep.max_step_api_calls||DATA.route_search.max_step_api_calls||0;
  const valid=deep.status==='route_validated';
  const lastAttempt=attempts[attempts.length-1],last=lastAttempt&&lastAttempt.status;
  const termination=lastAttempt?.termination_reason||lastAttempt?.local_checks?.errors?.join(', ')||'未分类';
  el('notice').textContent=`逐步路线检索 smoke：每次模型只提议一个方向；本地 schema 仅开放仍可达且预算允许的方向，确定性校验通过后才更新状态。当前 ${calls}/${limit} 次调用，零网络重试。${valid?'路线通过本地逐步及最终整条校验。':last==='api_error'?'请求失败，已停止，没有路线回退。':deep.status==='model_gave_up'?`模型放弃，原因：${stepErrorLabel(termination)}。`:'尚未得到完整合法路线；已接纳前缀保留，被拒绝动作没有推进状态。'} 这不是正式实验结果。`;
 }else if(firstAttempt&&firstAttempt.status==='route_constraint_violation'){
  const partial=firstAttempt.partial_proposal_audit||{};
  const preflight=DATA.route_search.reachability_preflight||{};
  const selfCheck=partial.model_step_checks_available?`模型给出 ${partial.model_step_check_count} 个自检标记，但只返回 ${partial.complete_slot_count} 条移动，不能逐步对应。`:'模型没有可识别的逐步自检。';
  const reachable=preflight.chain_reachable?'本地预检确认起点、上车点、下车点在有向图中可达。':'本地预检未确认完整可达链。';
  el('notice').textContent=`单次烟雾失败，不是正式实验结果。DeepSeek 返回完整工具参数，但本地约束拒绝了提议：${partial.complete_slot_count||0} 个路线槽码中 ${partial.invalid_edge_slot_count||0} 个不属于允许的有向边，${partial.continuity_violation_count||0} 步与前一步不连续。${selfCheck}${reachable}没有重试；未接纳、未执行，也没有传统算法兜底。路线实际耗电未计算；下方能耗/电费仅是假设提议值。`;
 }else if(firstAttempt&&firstAttempt.status==='route_output_parse_failed'){
  const partial=firstAttempt.partial_proposal_audit||{};
  el('notice').textContent='烟雾结果，不是正式实验样本。DeepSeek 返回无法解析的工具参数；离线恢复 '+(partial.complete_slot_count||0)+' 个完整槽码，其中 '+(partial.invalid_edge_slot_count||0)+' 个不在地图的合法有向边集中、'+(partial.continuity_violation_count||0)+' 步不连续。下方能耗仅是假设提议估算；完整路线实际耗电未计算。';
 }else if(DATA.status==='model_returned_failed'){
  let detail='原响应没有失败原因；不能据此认定地图无路。';
  if(reason==='model_unable_to_construct_route')detail='模型未构造出路线；没有报告已确认无路。';
  else if(reason==='model_confirmed_no_directed_path')detail='模型声称有向图不可达；此结论未经独立验证。';
  const reachText=reach&&reach.task_reachable?'事后 BFS 显示起点—上车点—下车点均可达；审计没有发给模型或用于兜底。':'未提供事后可达性审计。';
  const legacyNote=reason?'':'提示和失败分类已离线修正，尚未再次调用 API。';
  el('notice').textContent='烟雾结果，不是正式实验样本。模型返回 failed：'+detail+reachText+legacyNote;
 }else{
  el('notice').textContent='烟雾结果，不是正式实验样本。查看每轮路线提议、验证状态、能耗估算和请求费用。';
 }
 el('retry-audit').textContent='当前没有单独的重试输入格式审计。';
 el('retry-audit-note').textContent='';
}
const mode=DATA.route_search.generation_mode||'whole_route';const invocationCount=DATA.model_invocation_count??DATA.api_request_count??0;el('subtitle').textContent=`${map.map_id} · ${task.task_id} · ${task.task_type} · ${mode==='stepwise'?'逐步方向提议':'整条路线提议'} · ${invocationCount} 次路线请求`;
const ti=el('task-info');row(ti,'自然语言上车目标',task.pickup_text);row(ti,'解析上车点',pickup);row(ti,'自然语言下车目标',task.dropoff_text);row(ti,'解析下车点',dropoff);row(ti,'起点',start);row(ti,'Qwen 候选检查',Object.values(DATA.qwen.checks).every(Boolean)?`${Object.keys(DATA.qwen.checks).length} 项全部通过`:'存在未通过项','good');row(ti,'最终状态',DATA.status,'bad');if(DATA.route_search.prompt_byte_reduction_percent!=null)row(ti,'提示字节变化',`精简前 ${DATA.route_search.legacy_serialized_prompt_bytes} → 精简后 ${DATA.route_search.serialized_prompt_bytes}（减少 ${DATA.route_search.prompt_byte_reduction_percent}%）`);if(DATA.route_search.map_equivalence_audit)row(ti,'地图语义核对',DATA.route_search.map_equivalence_audit.passed?`通过：${DATA.route_search.map_equivalence_audit.expected_semantics_sha256}`:'未通过','good');
el('features').textContent='地图道路特征：'+(map.roadside_features||[]).map(f=>`${f.label}（${f.node_id}，${f.side_of_road}）`).join('；');
const statusLabel={step_accepted:'本步接纳',step_invalid:'本步拦截',step_output_invalid:'响应格式无效',model_gave_up:'模型放弃',api_error:'请求失败'};const buttons=el('attempt-buttons');attempts.forEach((a,i)=>{const b=document.createElement('button');b.type='button';const label=statusLabel[a.status]||a.status;b.textContent=`第 ${a.attempt} 次 · ${label}`;b.addEventListener('click',()=>selectAttempt(i));buttons.append(b)});
const reach=DATA.route_search.reachability_preflight||{},deep=ROUTE_MODEL||{},usage=deep.usage||{},cost=deep.estimated_cost||{};const fi=el('final-info');row(fi,'R4 正式结果','否（单任务烟雾 / 本地预检）','bad');row(fi,'路线模型后端',DATA.route_model_backend||'deepseek');row(fi,'路线生成模式',mode==='stepwise'?'逐步方向提议':'整条路线提议');row(fi,'实际模型调用 / 上限',`${invocationCount} / ${deep.max_step_api_calls||DATA.route_search.max_step_api_calls||'—'}`);row(fi,'合计 token',usage.usage_complete===false?'未知（请求未取得完整用量）':`${usage.input_tokens??0} 输入 / ${usage.output_tokens??0} 输出`);row(fi,'用户输入累计字节',usage.serialized_user_input_bytes??'—');row(fi,'系统和用户消息累计字节',usage.serialized_messages_bytes??'—');row(fi,DATA.route_model_backend==='codex_cli'?'Codex 额度费用':'API 费用估算',cost.api_cost_amount!=null&&Number.isFinite(Number(cost.api_cost_amount))?`${Number(cost.api_cost_amount).toFixed(6)} CNY`:(DATA.route_model_backend==='codex_cli'?'Codex CLI 未提供金额，不能换算账户额度成本':(DATA.api_request_count?'未知（用量未返回）':'尚无请求')));row(fi,'路线耗电',cost.route_electricity_status==='calculated_from_validated_route'?'按合法路线计算':'未计算：没有通过校验的完整路线');row(fi,'有向可达预检',reach.chain_reachable?'起点—上车点—下车点均可达':'未通过');
el('step').addEventListener('input',draw);selectAttempt(0);
</script></body></html>"""


def render(source: Path, output: Path) -> Path:
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("schema_version") != "route_r4_smoke_result_v2":
        raise ValueError("unsupported R4 smoke result schema")
    audit_path = source.with_name("posthoc_reachability_audit.json")
    if audit_path.exists():
        data["posthoc_reachability_audit"] = json.loads(
            audit_path.read_text(encoding="utf-8")
        )
    retry_audit_path = source.with_name("retry_input_and_map_audit.json")
    if retry_audit_path.exists():
        data["retry_input_and_map_audit"] = json.loads(
            retry_audit_path.read_text(encoding="utf-8")
        )
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        HTML_TEMPLATE.replace("__PAYLOAD__", payload).replace(
            "__RESULT_JSON_NAME__", html.escape(source.name, quote=True)
        ),
        encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-html")
    args = parser.parse_args()
    source = Path(args.input_json)
    output = Path(args.output_html) if args.output_html else source.with_name("replay.html")
    print(render(source, output))


if __name__ == "__main__":
    main()
