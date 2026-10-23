# Codex CLI 路线 smoke 修复与费用核算（2026-10-22）

## 范围

本记录核对 Codex CLI 在路线层分步请求中的临时会话隔离、请求额度门禁、逐步有向边校验和 usage 记账。用户授权在 `grid-8x8-seed-0` 上最多进行 8 次 CLI 请求，零重试；发生工具调用或非法动作即停止。本记录是 Codex 独立 smoke，不作为正式 R4 组间实验结果。正式 R4 后端按用户最近确认仍为 DeepSeek。

## 修复后的完整路线 smoke

实验目录：`outputs/route_layer/r4_codex_ephemeral_fixed_smoke_v1/`。

- 地图：冻结的 `grid-8x8-seed-0`，64 个节点、205 条有向边；预检地图语义哈希与冻结输入一致。
- 任务：起点/上车点 `node_6_0`，下车点 `node_6_2`。这是为检验跨进程临时调用而设置的两步 smoke，`formal_comparison_eligible=false`。
- 模型两次分别返回 `N`：`node_6_0 → node_6_1 → node_6_2`。两步均通过动态方向 schema、模型自检和本地有向边/边界/访问顺序校验；0 工具事件、0 非法动作、0 重试、自检差异为 0。
- 路线到达下车点：200 米、26.667 秒、`energy_v1` 仿真能耗 0.038 kWh。该任务很短，结果只证明这个 smoke 的模型调用和路线校验链可以跑通，不代表完整 R4 任务性能或路线最优性。
- 两个独立 ephemeral CLI 进程都进入并完成一个模型回合，没有尝试恢复上一回合的 thread。

逐回合 JSONL、结构化结果、费用核对、最终审计和回放均保留在上述目录。可直接查看 [回放页面](../outputs/route_layer/r4_codex_ephemeral_fixed_smoke_v1/replay.html) 和 [最终审计](../outputs/route_layer/r4_codex_ephemeral_fixed_smoke_v1/final_audit.json)。

## 修复内容

1. **临时会话错误续接**：Codex CLI 即使在 ephemeral 模式也会返回 thread ID。先前 runner 把它写回状态，adapter 又将传入 ID 当作持久会话，导致下一步尝试恢复不存在的 rollout。现在只有显式 `persistent_per_task` 模式才接收或保存可恢复 session ID；ephemeral 模式逐请求启动新 CLI 进程。
2. **授权额度与路线可完成性**：预检清单现在记录本轮真实授权上限；每步方向候选只保留能在剩余路线动作预算和剩余授权请求数内完成的方向。最短剩余步数超过授权余量时，在发请求前停止并标记预算不足。
3. **调用和模型回合记账**：最终审计分别统计 CLI 进程启动和有 `turn.started`/usage 的模型回合。模型启动前失败会保留为 CLI 失败记录，不伪造成一次模型请求或 token 消耗。

## 本授权窗口的调用与费用

本窗口包含修复前两个诊断任务和修复后的完整 smoke。根据各目录的 `attempt_*.json`、逐步 JSONL 与 usage 审计合并：

| 范围 | CLI 启动 | 模型回合 | 输入 token | 缓存输入 | 输出 token | 费率等值 credits | API 标价等值 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 首个诊断任务 | 2 | 2 | 30,316 | 0 | 2,350 | 0.22208 | $0.0088832 |
| 临时续跑诊断任务 | 2 | 1 | 9,698 | 0 | 369 | 0.05956 | $0.0023824 |
| 修复后完整 smoke | 2 | 2 | 19,390 | 0 | 1,480 | 0.14135 | $0.0056540 |
| **合计** | **6** | **5** | **59,404** | **0** | **4,199** | **0.42299** | **$0.0169196** |

合计 63,603 个已计量 token。临时续跑诊断中的第二次 CLI 启动发生在模型回合开始前，因错误恢复 ephemeral thread 而失败；该次没有 token usage，按零重试要求没有再试。授权上限为 8 次，本窗口实际启动 6 次 CLI；完整 smoke 到达终点后停止，未消耗剩余 2 次。

费率按 GPT-5.6 Luna Standard 每百万输入/缓存输入/输出 token 分别 5 / 0.5 / 30 Codex credits 核算；API 参考价分别为 $0.20 / $0.02 / $1.20。Codex CLI 没有报告账户实际扣款，因此 0.42299 是 Standard token 费率等值，$0.0169196 是 API 参考价等值，二者都不是账户账单或订阅额度实际扣减。

价格复核来源：[Codex Standard 费率表](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

## 验证与准入结论

- `python3 -m unittest discover -s tests -q`：157 项通过。
- Python 源文件编译检查通过；最终 `replay.html` 的内嵌 JavaScript `node --check` 通过。
- 修复后的 Codex CLI 调用链已通过两步完整路线 smoke，可以继续进行**独立 Codex CLI 路线试跑**。
- 该两步任务更改了起点，不能进入正式对比表；未启动十种子批次，也不构成 R4 结果。正式 R4 继续使用现行方案中固定的 DeepSeek 后端。
- 本轮没有再发模型请求。完整 smoke 已完成，剩余两次授权调用保留。

证据类型：用户授权的真实 Codex CLI 模型回合、原始 JSONL usage、离线确定性有向边检查、费用费率复算、可视化回放和本地全量回归测试。
