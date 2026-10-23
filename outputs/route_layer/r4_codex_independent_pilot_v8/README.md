# Codex R4 独立路线试跑 v8

## 本轮结果

- 试跑后端：Codex CLI 登录账号，`gpt-5.6-luna`，low 推理强度。历史 DeepSeek 结果继续单独归档；本轮验证用户后来指定的 Codex CLI 后端。
- 地图：`grid-8x8-seed-0`，64 个节点、205 条有向边。
- 任务：`node_0_5 → node_1_2（上车）→ node_6_2（下车）`。从 v7 的已校验合法路线前缀继续。
- 本轮授权最多 8 次新请求；实际 7 次到达下车点后停止。7/7 个动作通过本地确定性约束检查，0 工具调用、0 非法动作、0 重试；模型自检与本地校验无差异。
- 完整父链路线共 29 条有向边、2,900 米、357.014 秒，`energy_v1` 仿真能耗为 0.53575 kWh。

## 与同任务传统最短路线对照

离线在同一冻结地图和同一取送任务上运行 R0 全图 Dijkstra，得到 9 条有向边、900 米、95.208 秒、0.164 kWh。Codex 路线合法且完成了取送，但距离约为 Dijkstra 的 3.22 倍、行驶时间约为 3.75 倍、仿真能耗约为 3.27 倍。该对照仅用于诊断这项 8×8 试跑的路线效率，不是正式组间统计结论。

## Token 与费用

本轮 7 次请求共 68,904 输入 token（缓存输入 0）和 4,337 输出 token，共 73,241 token。按 GPT-5.6 Luna Standard 费率复算为 0.474630 credits 等值；按官方 API token 标价折算为 $0.01898520。Codex CLI 未提供账户订阅实际扣额或现金账单，这两个价格均为参考等值。

包括启动 smoke 与 v1–v8 父链在内，累计 30 次模型请求、298,261 输入（缓存输入 9,600，包含在输入中）和 23,104 输出 token。累计 Standard 费率等值为 2.141225 credits，API 标价等值为 $0.08564900；不代表账户实扣。

## 回放和审计

- [交互式地图与完整父链轨迹回放](replay.html)
- [本轮最终审计](final_audit.json)
- [本轮逐请求 token 与费用复算](codex_usage_cost_reconciliation.json)
- [本轮试跑清单](manifest.json)
- `traces/` 保存每次 prompt、schema、CLI JSONL 和模型结构化输出。

官方费率来源：[Codex Standard token rates](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API token prices](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。订阅实际使用额度由账户计划和使用仪表盘决定，API 标价不等于 Codex 账户账单。

## 100×100 成本敏感性

离线逐任务量取了完整路线 prompt 和 Codex schema：R4 每组 130 个可达任务合计约 5.81 MB，逐边方式约 446 MB。Token 拟合外推的输入量级约低 90 倍；输出 token 与账户实扣未知。这是待评估候选，不是已批准的 R4 协议。见[完整路线候选成本敏感性](../r4_preflight/codex_cli_100x100_full_route_cost_sensitivity_20261021.md)。

## 结论边界

v8 证明 Codex CLI 在这项固定 8×8 任务中能够完成一次无违规的完整取送路线，并留下可逐步回放和复算的调用记录。它仍是单任务试跑，不证明跨种子成功率或泛化能力。路线比同任务 Dijkstra 明显更长；100×100 正式实验仍未准入。现有 stepwise runner 的 32 步路线预算也不足以覆盖大多数正式任务。
