# R4 安全门暂停与新增请求对账

- 核验时间：2026-10-30 12:30:54 CST
- 正式批次：100×100 冻结矩阵，routeable=129，frozen=150
- Parent manifest SHA-256：`ad5fe511967cad35012bd259db7549a2e9d87eb97bfba52aea3d0eed8d834561`；当前总请求 `12102`，相对安全检查点 `12097` 新增 `5`。
- 当前状态：`paused_at_parallel_checkpoint`，`halt_no_more_model_requests=true`，`in_flight=[]`，路线安全门 `blocked_pending_current_user_safety_gate`。

## 新增 5 次调用

| Worker / 任务 | 调用 | 结果 | 已接纳路线动作 | usage |
|---|---:|---|---:|---|
| `worker_02 / formal-task-0008-feasible` | 5 | accepted_legal_step；仅前缀 | 1 | 17879 输入 / 265 输出 tokens |
| `worker_02 / formal-task-0008-feasible` | 6 | trace-confirmed HTTP 429 Too Many Requests；usage unknown | 0 | 未报告；不能视为 0 |
| `worker_03 / task-0009-0006` | 1 | trace-confirmed HTTP 429 Too Many Requests；usage unknown | 0 | 未报告；不能视为 0 |
| `worker_recovery_429 / task-0003-0004` | 31 | accepted_legal_step；仅前缀 | 1 | 15468 输入 / 540 输出 tokens |
| `worker_recovery_429 / task-0003-0004` | 32 | trace-confirmed HTTP 429 Too Many Requests；usage unknown | 0 | 未报告；不能视为 0 |

- 两个成功响应合计 CLI 报告 `33,347` 输入、`805` 输出 tokens；账户实际扣费未由 CLI 报告。
- 三个失败 trace 均为 `exceeded retry limit, last status: 429 Too Many Requests`，没有返回路线动作；三个 attempt 的 `usage_unknown_request_numbers` 均包含对应调用，`delivery_unknown_request_count=0`（服务方 429 已明确记录）。原始 trace 中未记录 `Retry-After`。attempt 的外层 `retry_count=0`，但这不表示 Codex CLI 内部没有重试；trace 明确显示 CLI 内部已耗尽重试。没有再重放这些 429 调用。
- 两个成功响应只是单步合法路线前缀；没有新增完整路线，也没有将前缀计作完成。三条 429 调用没有合法动作。

## 暂停核验与当前安全门

- Parent 与三个最近活动 worker 的 halt 标记已重新置为 `true`；parent `in_flight` 为空，所有 worker 摘要 PID 为空。其他 worker 原有 halt 标记保留。
- 当前 `ps` 未发现 R4 runner/aggregator。`worker_recovery_429` LaunchAgent 核验为未加载；screen 中未发现 R4 会话。
- 曾观察到 `worker_recovery_429` LaunchAgent 为 `spawn scheduled`；随后 `launchctl bootout` 返回“service not found”，二次核验也为未加载。不要将该命令结果写成正在运行或启动成功。
- 更新后的 parent `route_safety_gate.formal_continuation_permitted=false`。旧字段 `passed_offline_directed_graph_route_search` 仅描述离线证据，不满足当前验收门。
- 尚未完成：正式在线调用链的逐有向边校验与恰好回退一个合法节点后重搜的全链验证；零模型调用死路/单行反向回放；同任务共同候选池与4:6、6:4双权重选择；将EV2/成本和最终选择绑定；对被选路线做全流程SUMO验收且单行违规为零；保存最终使用的代码/config SHA、validator审计、trace与SUMO日志。
- 上一任务状态分布（58 complete、15 failed、10 outcome unknown、26 ready、20 pending）是12,097请求检查点快照；本事件未重建129项全矩阵状态表。

## 证据文件

- `manifest.json`：本次对账、worker 与请求级哈希、gate blockers。
- `evidence/`：5个attempt JSON及对应5个原始Codex JSONL trace；不含原始prompt。
- `../r4_codex_formal_batch_20261027_v2/index.html`：已更新批次可视化索引。

### 校验边界

当前工作区源码SHA仅是本次审计时快照；attempt未绑定当时执行代码的源码SHA，故不把它们冒充历史执行代码哈希。要通过安全门，必须在零调用验收中记录实际源码与config SHA，并将其与validator、SUMO、轨迹证据绑定。
