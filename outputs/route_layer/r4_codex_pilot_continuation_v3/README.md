# Codex 独立 8×8 路线试跑 v3

## 结果

本轮从 v2 的合法前缀继续，仍使用冻结地图 grid-8x8-seed-0（64 个节点、205 条有向边）和同一订单 task-0000-0009：起点 node_0_5，上车点 node_1_2，下车点 node_6_2。

按授权最多发 8 次请求、零重试；第 7 次触发停止条件后立即终止，没有发第 8 次。新增请求中 6 次动作被接纳，0 工具调用、0 重试。路线仍未到上车点或下车点，当前在 node_0_4，按最后一条已接纳动作计算至少还需 8 步。本轮不是完整路线，也不能作为正式路线性能结果。

停止原因是模型输出的 self_check.not_revisited=false。它提议从 node_0_4 向南到 node_0_3；该有向边在地图中存在，确定性本地检查通过，且该节点未访问过。为遵守“每项自检都必须为 true”的协议，程序拒绝并未执行这一步。因车辆没有移动，该次实际车辆能耗为 0 kWh；如果执行该候选边，按同一仿真能耗模型估算为 0.018 kWh，该数只作反事实估算，不计入实际消耗。

## Token 与费用

本轮 7 次请求使用 65,626 输入、7,931 输出，共 73,557 tokens；缓存输入 0。按 GPT-5.6 Luna Standard 费率卡复算为 0.566060 Codex Standard credits 等值、$0.02264240 API 标价等值。CLI 没有报告账户现金账单或实际订阅额度扣减。

调用时增加了单进程参数 skills.include_instructions=false，没有修改全局 Codex 设置。v2 平均每次输入约 14,130 tokens；v3 平均约 9,375 tokens，观察到的均值下降约 33.7%。这是两轮观测比较，不能单独证明差异全部由该参数造成。

包括先前 smoke、v1、v2、v3 在内，累计 24 次模型调用、305,706 输入和 28,170 输出，共 333,876 tokens；Standard credits 等值 2.373630，API 标价等值 $0.09494520。费用为官方费率卡等值，不是实际账单。费率来源：[Codex 官方价格说明](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

## 回放与审计

- [交互式路线回放](replay.html)：包括 v1–v3 全部可用的单步记录。红色虚线显示第 7 次被拒绝、未执行的候选边；当前节点保留在 node_0_4。
- [最终审计](final_audit.json)：含停止原因、局部确定性检查、自检失败字段、实际耗电和候选边假设能耗。
- [Token 与费用逐请求复算](codex_usage_cost_reconciliation.json)
- [单步原始 prompt、schema、JSONL 和响应索引](traces/index.html)
- [试跑清单与 CLI 配置](manifest.json)
- 上一段：[v2 回放](../r4_codex_pilot_continuation_v2/replay.html)

## 方法边界

这是独立 Codex CLI 路线试跑，不属于正式多种子路线比较。程序用有向边和任务顺序做确定性门禁；模型每次只选择当前一步。没有用传统路径算法替模型补路。该运行只能说明六个动作通过门禁并在第七次请求的自检协议处停止，不能说明完整路线成功或模型路线质量。
