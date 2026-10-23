# Codex 8×8 独立路线试跑（部分结果）

## 结果状态

- 地图：`grid-8x8-seed-0`，64 个节点、205 条有向边；任务为 `task-0000-0009`。
- 实际新增 Codex 模型请求：8/8；重试 0；工具事件 0。另有 1 次本地轨迹文件重名错误发生在模型调用前，用量为 0，已记录并修复。
- 8 个新增动作均经确定性本地校验通过；试跑到达 `node_5_7`，未到上车点 `node_1_2`，也未到下车点 `node_6_2`。状态为部分合法前缀，不能计作完整路线或正式 R4 实验。
- 当前合法前缀（包含此前 v5 的首步）：`node_0_5 → node_0_6 → node_1_6 → node_1_7 → node_2_7 → node_3_7 → node_4_7 → node_4_6 → node_5_6 → node_5_7`；共 9 条有向边、900 m、仿真行驶时间约 110 秒、前缀仿真耗电 0.168 kWh。该电耗只覆盖已接纳前缀，不是订单全程能耗。
- 本轮 8 个请求累计 `112,890` 输入 / `8,786` 输出 token，模型等待约 `153.1` 秒。加上之前 v5 单步 smoke 后为 `127,043` 输入 / `10,159` 输出 token。Codex CLI 未报告账号现金费用，不能换算货币成本；无硬输出 token 上限。
- 最后一步后到完成上车—下车链至少还需 16 步；已用完本次授权的 8 次新增请求，不再继续调用。

## 方法边界

这是独立 Codex 试跑，不并入固定 DeepSeek 的正式 R4。模型每次只选一个方向；动态 JSON Schema 和确定性程序先筛选允许的有向边、未访问节点、上车顺序、可达性与剩余路线步数下界。程序不生成完整路线，也不代替模型选择；因此结果属于“本地约束筛选 + Codex 分步选边”，不是无约束纯 LLM 路线实验，也不是纯模型输出。

## 关键更正

v5 的方向动作 `N` 原本合法，但旧结果文件把它错配到 `node_0_4` 的 S 边。按冻结实现定义，N 为 y 增加方向，因此实际目标是 `node_0_6`。原 v5 文件未改；更正依据和旧事后 BFS 审计受影响范围见 [`direction_mapping_correction.json`](direction_mapping_correction.json)。

## 查看与复现

- 交互式地图与逐步轨迹：[replay.html](replay.html)
- 离线完整性核查：[final_audit.json](final_audit.json)
- 完整当前状态、每步响应和用量：[state.json](state.json)
- 试跑配置与汇总：[manifest.json](manifest.json)
- 每个请求的 prompt、schema、CLI JSONL 和最终响应：[`traces/index.html`](traces/index.html)
- 单步结构化记录：`attempt_*.json`；本地无请求故障：[`recovery_events.json`](recovery_events.json)
- 一步一个请求运行器：[`scripts/run_r4_codex_pilot_continuation.py`](../../../scripts/run_r4_codex_pilot_continuation.py)
- 回放生成器：[`scripts/render_r4_codex_pilot_replay.py`](../../../scripts/render_r4_codex_pilot_replay.py)
