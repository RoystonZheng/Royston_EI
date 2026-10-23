# R4 Codex 独立 8×8 试跑 v5

## 本轮范围

这是与 DeepSeek 正式 R4 分开的 Codex CLI 独立试跑。复用冻结任务 `grid-8x8-seed-0` / `task-0000-0009`：起点 `node_0_5`、上车点 `node_1_2`、下车点 `node_6_2`。启动状态包含此前 smoke 的首条合法边 `node_0_5 → node_0_6`。路线提示采用 `r4-step-v2`；模型为 `gpt-5.6-luna`、low，最多新增 8 次单步请求、零重试，工具事件或无效动作即停止。

本轮离线预检通过，未发模型请求；确认 64 个节点、205 条有向边和输入地图等价。预检时从当前点到任务终点至少还需 10 步，因此 8 次授权本身不足以完成路线。本轮实际在第 6 次请求后因模型自检返回 false 而停止，剩余 2 次授权未使用。

## 观察到的结果

- 6 次 Codex CLI 请求，0 重试、0 工具事件；前 5 个新动作均通过动态 schema 和本地确定性道路校验。
- 第 6 次模型提议 `node_4_7` 向南到 `node_4_6`。这条有向边在动态候选中，本地边界、方向、连续性、禁行和不重访检查全部通过；但模型将 `allowed_directed_edge` 和 `inside_boundary` 两项自检标为 false。按门禁停止，未执行该动作。
- 实际停在 `node_4_7`，尚未到上车点或下车点；完整任务状态为未完成。合法前缀共 6 条边、600 米，估算驾驶时间 69.375 秒、电耗 0.112 kWh。
- 第 6 步若执行的模拟增量能耗估算为 0.019 kWh；实际消耗为 0 kWh，标记为“校验停止后未执行”。

## 用量与费用口径

本轮 6 次真实调用合计 58,296 输入（缓存 0）和 2,677 输出 token，共 60,973 token。按 2026-10-21 GPT-5.6 Luna Standard 官方费率快照复算，为 0.371790 credits 等值、$0.01487160 API 标价等值。加上启动 smoke 的 1 次调用后，累计 7 次、72,449 输入、4,050 输出、76,499 token；费率等值 0.483745 credits / $0.01934980 API 标价。

Codex CLI 未返回账户实付账单或订阅额度实际扣减。API 标价仅供比较，不是 Codex 订阅账单。费率来源：[Codex Standard 定价](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API 定价](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

## 证据与解读限制

这是受限的单任务试跑，不是路线层正式 R4 结果，也不与 DeepSeek 组或 v4 完成路线合并计算。程序按剩余合法动作预算构造方向候选，并执行本地硬校验；该做法防止非法移动被执行，但不会替模型改路线。第 6 步体现的是模型自检误报导致安全停止，不是本地检测到非法边。候选过滤限制和单任务规模也不支持路线质量、最优性、泛化或模型优劣结论。

产物：`state.json`、`manifest.json`、`attempt_*.json`、`traces/`、`final_audit.json`、`codex_usage_cost_reconciliation.json` 和 `replay.html`。本轮没有发生额外请求来修复失败。
