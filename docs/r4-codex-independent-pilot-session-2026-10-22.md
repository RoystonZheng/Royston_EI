# Codex 独立 8×8 路线试跑记录（2026-10-22）

## 实验边界

本记录汇总两段独立 Codex CLI 试跑，用于验证单步路线建议、确定性有向边校验、CLI 会话隔离和可视化回放。它们不是路线层正式 R4 结果，不与传统算法或正式 DeepSeek 组作性能比较，也不证明路线最优。正式 R4 的分组定义以实验方案和最新用户决定为准。

本轮最多授权 8 次请求、零重试。两段任务合计启动 4 次 CLI 调用，其中 3 次进入模型回合；第 4 次 CLI 调用未进入模型回合，且未产生 token usage。失败后没有再次发送请求，剩余 4 次授权未使用。

## 任务 A：持久会话诊断

- 地图：`grid-8x8-seed-0`，64 个节点、205 条有向边；地图语义哈希与冻结输入一致。
- 任务：`node_0_4 → node_1_2 → node_6_2`。相对于原起点 `node_0_5`，起点改为 `node_0_4` 以满足 8 次调用上限；因此该任务不具备正式组间可比性。
- 返回 2 次动作：`node_0_4 --E--> node_1_4 --N--> node_1_5`。两步均通过模型自检和本地确定性校验，无工具调用、非法动作或重试。
- 第 2 步后最短剩余路线为 10 步，而当时只剩 6 次授权调用，故预算门禁停止。任务未到达上车点或下车点。
- 已接受前缀：200 m、26.389 s、`energy_v1` 估算 0.038 kWh。
- Usage：30,316 输入、0 缓存、2,350 输出，共 32,666 token。
- 费率卡等值：0.22208 Codex Standard credits；API 标价等值 $0.0088832。Codex CLI 未报告账户实扣。

产物：`outputs/route_layer/r4_codex_persistent_session_8x8_pilot_v1/`。

## 任务 B：临时会话独立试跑

- 地图相同；任务：`node_1_2 → node_1_2 → node_6_2`，即起点与上车点相同，最短路线 5 步。起点调整是为了使用剩余授权且仍保持独立试跑；此任务不具备正式组间可比性。
- 第 1 次模型回合选择 `E`：`node_1_2 → node_2_2`。模型五项自检和本地确定性校验均通过，无工具调用或违规；该合法前缀为 100 m、10.417 s、`energy_v1` 估算 0.019 kWh。
- 下一次 CLI 调用在模型回合启动前失败：CLI 执行 `thread/resume`，错误为 `no rollout found for thread id ...`。事件日志中 `turn.started` 为 0，JSONL 为空，没有可计费的模型用量。没有重试。
- 失败时车辆仍在 `node_2_2`，到下车点最短还需 4 步；任务不完整。
- 实际模型 usage：9,698 输入、0 缓存、369 输出，共 10,067 token。
- 费率卡等值：0.05956 Codex Standard credits；API 标价等值 $0.0023824。Codex CLI 未报告账户实扣。

产物：`outputs/route_layer/r4_codex_ephemeral_8x8_pilot_v1/`，包含 `final_audit.json`、`codex_usage_cost_reconciliation.json`、`replay.html` 和逐步 JSONL。

## 缺陷原因与修复

临时模式下 Codex CLI 仍会返回 thread ID。runner 把该 ID 写进了后续状态；adapter 又把“传入 session ID”自动等同于持久会话，于是下一步错误地运行 `codex exec resume`。该 thread 不存在可恢复 rollout，因此调用在模型生成前退出。

已作代码修复：adapter 只在显式持久模式接收和沿用 session ID；runner 的临时模式不保存返回的 thread ID；审计器分别记录 CLI 启动次数和实际模型回合数，并仅按已开始模型回合核算 token 与费用。当前授权因零重试规则已停止，没有用修复后的代码继续同一步请求。

## 汇总与结论

| 指标 | 本轮两段任务合计 |
|---|---:|
| CLI 调用 | 4 |
| 实际模型回合 | 3 |
| 未开始模型回合的 CLI 错误 | 1 |
| 输入 / 缓存 / 输出 token | 40,014 / 0 / 2,719 |
| Codex Standard credits 等值 | 0.28164 |
| API 标价等值 | $0.0112656 |
| 工具事件 / 非法动作 / 重试 | 0 / 0 / 0 |

两个任务都只得到合法前缀，没有完整到达下车点。结果只说明在这些 8×8 单步试跑中，模型给出的已接受动作通过了本地硬校验；它们不能用作完整路线性能结论。费用数字是按公开费率换算的估值，不是账户账单。费率来源：[Codex 定价](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

## 验证

- 全量 `python3 -m unittest discover -s tests -q`：154 项通过。
- Python 编译检查：adapter、runner、finalizer、renderer 通过。
- `replay.html` 内嵌 JavaScript：`node --check` 通过。
- 上述均为本地验证，没有额外模型请求。
