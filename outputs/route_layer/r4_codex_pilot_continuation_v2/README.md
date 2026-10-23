# Codex 8×8 路线独立续跑 v2（部分结果）

## 结果

- 同一冻结地图和任务：`grid-8x8-seed-0`，64 个节点、205 条有向边；起点 `node_0_5`，上车点 `node_1_2`，下车点 `node_6_2`。
- 继承 v1 的 9 条合法边后，本轮新增 8 次 Codex CLI 请求。8 次动作全部通过动态方向 schema 与本地有向边、边界、访问顺序、不重复和剩余可达性校验；0 重试、0 工具事件、0 非法动作。
- 本轮结束于 `node_4_4`，仍未到上车点或下车点。当前已接受路线共 17 条边、1,700 米，仿真行驶约 210.625 秒，合法前缀能耗 0.318 kWh；到完成取送最少还需 10 步。因为本轮 8 次请求已用完，结果仍是部分路线，不能作为完整路线成功或正式多种子结果。
- 本轮 usage：113,037 输入（缓存 0）+ 10,080 输出 = 123,117 tokens；Codex CLI 未给出账户现金账单。

## 费用复算

按 2026-10-21 核对的 GPT-5.6 Luna 官方 Standard token rates：Codex Standard 输入/缓存输入/输出为每百万 5/0.5/30 credits；API 标价为每百万 $0.20/$0.02/$1.20。API 等值按本轮 token 复算为 **$0.03470340**；Codex Standard 费率等值为 **0.867585 credits**。这是费率卡等值，非账户发票或订阅实际扣额。

连同 v5 单步 smoke 和 v1，整个独立试跑链累计 17 次请求、240,080 输入 + 20,239 输出 = 260,319 tokens；累计 API 标价等值 **$0.07230280**、Codex Standard 费率等值 **1.807570 credits**。路线仍未完成。

费用只使用 Codex JSONL 中明确报告的 token usage；缓存输入是输入 token 的子集，不重复相加，reasoning 已包含在输出中。官方费率来源：[Codex 价格表](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

## 方法边界

本组是“本地约束筛选 + Codex 分步选边”。程序依据完整地图检查允许的有向边，并根据拓扑可达性筛除无效动作；模型仍选择当前一步。地图、任务、Validator 和费用口径与前一段保持一致。该设置不是无约束纯 LLM，也不应用传统算法自动补路。

本轮第一步在 `node_5_7` 只允许 E；后续可选方向数量依节点变化。第 2 个请求从 E/S 里选 E，使最短剩余步数暂时由 15 增至 16；后续动作仍合法，最终降到 10。此结果可用于检查合法动作生成和局部方向决策，不能证明全程完成能力或路线质量。

## 可视化与记录

- 整合 v1 与本轮逐步动作的路线回放：[replay.html](replay.html)
- 最终审计：[final_audit.json](final_audit.json)
- 本轮请求和用量费用明细：[codex_usage_cost_reconciliation.json](codex_usage_cost_reconciliation.json)
- 当前状态：[state.json](state.json)
- 试跑预检与边界：[manifest.json](manifest.json)
- 每次请求的 prompt、schema、原始 CLI JSONL 和最终响应：[traces/index.html](traces/index.html)
- 上一段回放：[../r4_codex_pilot_continuation_v1/replay.html](../r4_codex_pilot_continuation_v1/replay.html)

