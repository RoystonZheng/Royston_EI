# Porto OSM R4 `ord_0008` 单订单pilot审计

复核时间：2026-10-28 18:48 CST。独立探索pilot，不属于formal matrix；一次成功投递模型请求，未重放。

## 路线与validator

- manifest SHA-256 `fc23ad7daf2e854a6a9ae9d25f9c26edecfc75c840b6a9cce6c3d9f6da923ab8`；Porto OSM-derived SUMO network；seed0 FASTEST；固定一辆任务车与一笔订单；模型请求数1、两腿；没有dispatch optimization。订单最终由shared Dijkstra fallback完成，模型有效/选中路线均为0。
- 模型响应SHA `afb748488cd093259a6a1924e4960019bfbc21de9f8b9ee140aa07e53c6acb4f`，validator首错`endpoint_alias_in_intermediate_route`。事后逐Link核验发现接驾、载客两段分别有75/75与110/110个转向不在冻结乘客图中，完整路径分别有18次与10次重复Link。它们不是可执行候选路线。
- 实际请求schema SHA `4edc2a93b1753b91bd845299b99e9d95b6a9b395e78a02d68bee82dfbde95e76`限制两腿`minItems=1`并对alias格式设pattern，但没有把有向邻接/合法连续转向编码到schema；合法性由返回后的validator判定。提示内容要求输出中间Link，但实际文字未明确写“禁止重复Link”。故按核验到的提示和schema陈述，不把外部报告中的“明确禁止重复Link”当作事实。

## EV2与经济口径

- 实际SUMO trace EV2 audit SHA `52056a49b3fda5e4cb984daeba328a4851662108e9b5ed6262916a3347fc4706`通过，并与源ledger在1e-8 Wh内吻合。模型`ev_longitudinal_v2`、profile `generic_reference_ev_v1`；推进耗电−再生回收+辅机耗电。空载接驾0.140429696385 kWh，载客0.588173038354 kWh，总signed-net 0.728602734738 kWh。945及3,126个样本坡度缺失按平坡；这是SUMO仿真轨迹估值，不是实车实测。
- 被拒模型路线counterfactual audit SHA `958a1752b67f4fcc4a24a9752ae22ccdec3ba8504eba7bdf1ea11fba6ad96054`分别估算0.516223909671及0.936269752238 kWh、合计1.452493661909 kWh。按独立Link从停到停估算，未执行，不可与fallback轨迹作能耗比较。
- v5 gate SHA `9d577f1fcd1ba5d3431e49f36d2362f39339693165afd140081770b69c85237a`为`blocked_incomplete_candidate_data`，`applied_to_online_route_choice=false`。SUMO summary有€0.3/kWh场景代理和€0.218581代理支出，但不代表市场电费/发票；没有完整收入、方法成本、候选集，故time-money无在线排序。CLI观察35,513 tokens、0.22779 Standard credits等值，实际账户扣费未报告。

## 结论边界

该pilot说明此请求schema没有静态表达完整有向转向合法性，validator在响应后成功阻止了错误路线。它能支持一个实现问题诊断，不能证明纯LLM v7全部失败的因果根因，也不能作为R4正式路线性能结果。
