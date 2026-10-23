# R4 Codex 独立路线试跑 v9（2026-10-22）

## 结论

v9 在同一张 `grid-8x8-seed-0` 地图上完成了 8 次单步 Codex CLI 请求。8 次都返回有向图上的合法移动，并通过本地确定性校验；没有工具调用、非法动作、重试或模型自检差异。但路线没有到达上车点或下车点，因此这是一次未完成的独立试跑，不是正式 R4 对照结果。

## 冻结输入与结果

- 地图：`grid-8x8-seed-0`，64 个节点、205 条有向边。
- 任务：起点 `node_0_4`，上车点 `node_1_2`，下车点 `node_2_3`。
- 接纳路线：`node_0_4 → node_0_3 → node_1_3 → node_1_4 → node_2_4 → node_3_4 → node_4_4 → node_4_5 → node_5_5`。
- 最终停在 `node_5_5`，既未到上车点，也未到下车点；完成任务的剩余最短步数下界为 9。
- 合法前缀为 8 条边、800 米、112.083 秒、`energy_v1` 仿真能耗 0.1493 kWh。该能耗只描述已执行前缀。
- 门禁结果：8/8 合法接纳；0 工具事件、0 非法动作、0 重试、0 自检差异。

这次试跑只用于验证 Codex CLI 逐步输出、动态方向 schema、本地硬校验、usage 记录和可视化，不与 100×100 传统算法实验作性能对比。地图相同不代表任务端点、地图规模和实验条件相同。

## Token 与费用

本轮 8 次请求累计 **77,722 输入 token（缓存输入 0）+ 8,117 输出 token = 85,839 token**。按 GPT-5.6 Luna Standard 官方费率折算为 **0.632120 Codex credits 等值**；按 API token 标价折算为 **$0.02528480 等值**。Codex CLI 没有报告账户实际扣额，这两个数都不是订阅账单或现金支出。

费率来源：[Codex 官方价格与额度说明](https://developers.openai.com/codex/pricing)；[GPT-5.6 Luna API 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。本项目采用的快照记录在 `configs/codex_cli_pricing_2026-10-21.json`。

## 执行问题与修复

本轮执行循环没有落实预先计划的“当剩余最短路线步数超过剩余授权请求数时提前停止”检查，因此用完了全部 8 次授权请求；尽管结尾已至少需要 9 步，任务不可能在本轮调用额度内完成。这没有超出用户授权上限，但多发了本可避免的请求。此问题属于调用编排缺陷，不是道路约束放行了非法移动。

之后在 `scripts/run_r4_codex_pilot_continuation.py` 增加确定性预算门禁：请求前和每次移动后都会比较剩余路线下界与本轮剩余授权请求数；路线不可达或预算不足时停止并写明原因。符合安全条件的预算不足前缀可在后续新授权下续跑。此改动只做本地判断，不调用模型。回归测试覆盖预算不足时不创建模型客户端，以及预算不足停止状态的恢复。

## 产物与验证

- [交互式路线回放](../outputs/route_layer/r4_codex_independent_pilot_v9/replay.html)
- [最终审计](../outputs/route_layer/r4_codex_independent_pilot_v9/final_audit.json)
- [逐请求 Token 和费用复算](../outputs/route_layer/r4_codex_independent_pilot_v9/codex_usage_cost_reconciliation.json)
- [原始状态和请求记录](../outputs/route_layer/r4_codex_independent_pilot_v9/state.json)

修复后运行 `python3 -m unittest discover -s tests`：**145 项通过**。回放内嵌 JavaScript 通过 `node --check`。这些本地测试没有发模型请求。

## 对正式实验准入的影响

v9 证明这条 CLI 调用链能返回并审计合法单步动作，但没有证明它能稳定完成取送路线。结合现有 100×100 路线步数门槛与本次短图任务未完成的结果，**路线层正式 R4 十种子批次仍未达到准入条件**。新预算门禁能避免在任务已无法于本轮额度内完成时继续消耗请求，但它不会改善模型选路，也不替代后续的完整任务验证、动作预算设计和正式实验前的费用熔断。
