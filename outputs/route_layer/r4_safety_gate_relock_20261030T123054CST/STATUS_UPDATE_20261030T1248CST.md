# R4 继续采样状态增量（12:48 CST）

## 快照范围

- 核验时间：2026-10-30 12:48:11 CST（04:48:11 UTC）。formal worker 调用范围按 parent 最新授权事件 `2026-10-30T12:38:37.653378+08:00` 起算，worker trace 统计到最新可读事件约 04:47:22 UTC。
- 这是运行中快照，不是批次终态。parent manifest SHA-256 为 `1f69bae0e211619e114f2f722eb41adebf3e1fbf29a7e5e64196be2ce5a67af3`，状态仍为 `paused_at_parallel_checkpoint`，其计数仍为 12,102；parent 最新授权事件明确记载 `sampling_allowed=true`，同时 `decision_function_compliance_status=blocked_online_v5_compliance_not_proven`。
- worker 分片新增调用尚未合并进 parent 总计。不得将下列 54 次分片调用直接加到 12,102；先按 task ID 去重，再更新 parent ledger。

## formal worker 增量

从 12:38:37 授权事件到本快照，三个现有 worker 的 `request_recorded` 共 54 次：36 次返回并通过本地有向边校验的单步动作，18 次在原始 trace 中明确为 HTTP 429。成功单步合计 CLI usage 为 621,526 输入 tokens、11,975 输出 tokens；18 次 429 的 usage 均未报告，不能记为 0。CLI 未报告实际账户扣费。

每个 worker attempt 的外层 `retry_count=0`。18 条 Codex JSONL trace 都显示 `exceeded retry limit, last status: 429 Too Many Requests`；trace 内没有 `Retry-After` 字段。失败调用保留为错误/usage unknown，没有因 429 重放同一个响应。没有任何一条新增成功完成路线事件；36 个合法方向只是路线前缀动作，不是36条完整路线。

| 分片 | 当前观测 worker manifest SHA-256 | 备注 |
|---|---|---|
| `worker_02` | `08f4267efa7cfc0d0f01ef06e8c361a5887620318d84158eacbb5b29f523446e` | 现有唯一 supervisor screen `r4_formal_worker_02_20261030`（PID 34679） |
| `worker_03` | `7db23400ee35645346a10ac667a9196fe727c93451b5f12f7e38f9f0cf3d1e17` | 现有唯一 supervisor screen `r4_formal_worker_03_20261030`（PID 34680） |
| `worker_recovery_429` | `c2a517b4cced1bab1a064a2fd0b576db3242b8f4e8156da0993ee0c9c286f8d3` | 现有唯一 supervisor screen `r4_formal_worker_recovery_429_20261030`（PID 34682） |

三个 screen supervisor 的 shell argv 均为 `run_r4_resilient_chunk_supervisor.py --batch-dir <对应 worker> --project-root /Users/didi/Documents/personalcodexfile/第二次课 --pilot-audit .../r4_codex_formal_task9_pilot_20261027_v3/final_audit.json --chunk-size 512`。本快照时没有可靠的 parent 级汇总或最终 task ledger；PID/argv 会随 supervisor 进入冷却、派发而变化。所有任务仍须保留各自 trace 与状态。

### 18 个明确 429 的原始 trace

| 分片 / 任务 / attempt | 原始 trace SHA-256 |
|---|---|
| worker_02 / task-0006-0001 / 3 | `05339136ffa7a0b816399faf5ef48735ca436d38586b30ce4d0579118c4b4b68` |
| worker_02 / task-0008-0007 / 3 | `3f63d7e461622e4148feb1862da10bd060897e20eeabbfb843389463fcad6e67` |
| worker_02 / task-0005-0002 / 3 | `032f2c195780090d38144f311acb3b999ecd96c1276e641222daaccdfa6b6ed9` |
| worker_02 / task-0007-0006 / 4 | `ee6d9f4f8e9ab9cd4f276956940ccd261b01792d1102a40d6731ff55dba0c369` |
| worker_02 / task-0001-0009 / 3 | `bf3e7352be4d0cceffe322011e7d8e1d113bba8be279ad484e88efc1316ada4a` |
| worker_02 / formal-task-0005-detour / 3 | `8e07b5ca873c83a18ac8243a9dfb4785b5eb3aab600eebb4ac02eb644ae70a10` |
| worker_03 / task-0008-0010 / 2 | `c491c213faeec293e85e235db654039d8ecad0e09001bfc27af9ff1d0038ab92` |
| worker_03 / task-0009-0010 / 5 | `2038c67e0181dfc9a390f6d50428a2d55a27e25343298dcb9ef04facd6e8dc68` |
| worker_03 / task-0005-0001 / 4 | `753b19f35b492f2938ec57748093637c0632a4f076c5362161dbf633cdcc1b00` |
| worker_03 / task-0004-0004 / 2 | `7fc45983e6610a1b62154cea405725795e4cdb256491c9f869815bef0b622d9f` |
| worker_03 / task-0003-0003 / 3 | `5b6c8757005ad7dc5eba14ef0d70ea4106cf83b96d26cbd43a92be0050dc063f` |
| worker_03 / formal-task-0000-detour / 17 | `210d5ebdef8d3c5498882882ba2c7d7e189de45944bfeb6227c8b6c9522b1b6e` |
| worker_recovery_429 / task-0003-0006 / 2 | `9fc6b48325fd25c949e05d82271b5f2c5c67deadd42214fbb0ffca0b92ed5678` |
| worker_recovery_429 / task-0003-0010 / 2 | `f6cceee9589ad77029223f1a848c115b60b7e9d7613c3a163ce602700d3ecb94` |
| worker_recovery_429 / formal-task-0003-boundary / 3 | `7deadb2253ac4c69f9d6d479d43d6f12a70503ee55be2d32ab94816cf91d6d0f` |
| worker_recovery_429 / task-0004-0005 / 3 | `4606a7a61e7d8f099475a19f6408f11d1bdbf8ac1fc4a825025a1fc6d92b0c8e` |
| worker_recovery_429 / task-0004-0007 / 3 | `7b408b7aa4c685b42c2b8cc0b843fdb1ffad86beab6ac87974fde172181a9604` |
| worker_recovery_429 / task-0004-0008 / 3 | `239d0b83e5f10a8a1a51adbe2da66ea977ac0da74249ba477389bbc13c1091c9` |

## 重叠任务审计

跨三个 worker 发现六个相同 `task_id`：

- `formal-task-0002-detour`：worker_02 failed，recovery route_complete。
- `task-0006-0002`：worker_02 failed，recovery 为零请求 ready。
- `task-0008-0005`：worker_02 failed，recovery 为零请求 ready。
- `task-0002-0005`：worker_03 failed，recovery route_complete。
- `task-0003-0006`：worker_03 与 recovery 均 failed；recovery 在本次授权后又产生1个合法动作和1个429，属于已发生的逻辑任务重叠，调用与轨迹均保留。
- `task-0006-0010`：worker_03 failed，recovery 为零请求 ready。

已向 formal owner 发出去重指令。owner 当前报告会在三条现有请求落盘后，只隔离 recovery 中零请求 ready 的三项；不改写已完成/失败历史，不杀在途请求。后续先核对父清单与 shard ledger，再继续唯一且未完成任务。

## 独立 link-fallback pilot（不计入129项）

`task-0000-0002`、`grid-100x100-seed-0`、`r4-step-v4-link-fallback`：state SHA-256 `d6a0c5867e8cfd598399724a3b84cd0290b31eb4f68c8a1ff8beafa6a4ef0a0f`，47次 invocation 中46个动作被本地校验接受，最后一次为明确429；当前节点 `node_58_67`，pickup `node_48_68` 未到达，路线未完成。46次已知 usage 合计 1,322,592 输入、19,354 输出 tokens；实际账户费用不可得。第47次失败 trace SHA-256 `42a235ed2e1c8ac6a2757d0a521e018a8ddf2ded0e3e88f72ae11d9a865beaa3`。

## 方法合规状态

最新授权允许正式批次继续采样，不代表决策方法已符合 time-money v5。online selector、4:6/6:4 双profile选择、候选理由与选中路线 EV2/SUMO 绑定仍未由这批新增单步调用证明。正式批次共129个可路由任务尚未完成；本快照无新增完整路线、无SUMO验收结果。不能把36个新动作或pilot的46边前缀写成完整路线或实验效果。

## 原始文件

- parent manifest：`[R4_APP_SUPPORT_OUTPUT_ROOT]/manifest.json`
- worker manifests、attempts、原始 trace：同目录 `parallel_workers/worker_02/`、`parallel_workers/worker_03/`、`parallel_workers/worker_recovery_429/`
- pilot state、attempts、trace、回放：`outputs/route_layer/r4_link_fallback_preflight_task0002_v1/`
- 证据 SHA 是观测文件内容的散列；当前源码/config SHA 未绑定到这些历史 attempt，不能当作执行时版本证明。
