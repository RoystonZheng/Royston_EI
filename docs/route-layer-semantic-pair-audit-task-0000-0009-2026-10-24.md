# seed 0 task-0000-0009 语义输入配对核查

日期：2026-10-24  
性质：单任务输入与语义解析审计，不是 R4 完整路线结果。

## 结论

传统组、纯 LLM v8 与 R4 task9 试跑使用相同的公开订单表达、起点和地图。传统组与纯 LLM 组 seed 0 地图文件 SHA-256 均为 `bdf7da792ecf78ae393f5441055d592f3585674bea48d4f12d2d2d1ce632adbe`，订单文件 SHA-256 均为 `867d863c961406fd89a620177253cf50493b0883988069a033d147d6496e7999`。公开任务为：从 `node_69_41` 出发，“请在公交站路牌旁、西侧的合法上车点上车”，再“请在科技园附近下车”。R4 紧凑方向地图的等价性核查通过：10,853 条有向边完整匹配，方向/成本语义哈希一致。

语义任务允许各组按其输入能力解析候选端点，所以“解析后的上车节点不同”本身不代表地图或任务不一致。评估真值为上车 `node_25_66`、下车 `node_42_43`：

- 纯 LLM v8（历史 DeepSeek 组）在同一 task ID 上收到模型响应，但失败码为 `pickup_unresolved`，没有路线。依照方案，它与后续 Codex 结果分开报告，不并入新后端组。
- 传统组 `graph_only` 看不到道路路牌特征。其语义解析回退到离起点最近的合法上车点 `node_68_41`，下车点为 `node_42_43`；传统结果的 `pickup_exact_match` 为 false。该算法结果虽完成且路径合法，终点语义准确性仍须单独计入。对应模拟估算为 3,300 米、总行驶 332.9167 秒、能耗 0.603 kWh；这些指标针对回退节点 `node_68_41`，不是上车真值路线。
- R4 的本地 Qwen 读取 `feature:sign_08`，解析“西侧”到 `node_25_66`，解析科技园到 `node_42_43`；预检中两个端点均通过候选合法性检查，且与本任务真值相符。这只是单个试跑的解析证据，不能据此推断整体准确率。
- R4 Codex 仅接受了第一步 `node_69_41 → node_68_41`。该节点是传统组选择的候选上车点，但在 R4 路线中只是经过节点；R4 的上车目标仍是 `node_25_66`。当前有效前缀 100 米、10 秒、模拟估算 0.018 kWh。整条路线未完成，故 R4 完整路线距离、耗时和能耗均未评估。

因此，可比较的结论是这次语义解析输出不同且 R4 输出命中标注真值；不能把传统组到 `node_68_41` 的完整路线成本与 R4 的单步前缀作路线性能比较。完整 R4 批次仍未完成。

## 复核依据

- 公开输入与真值：`outputs/route_layer/traditional_experiment_v1/seeds/seed_0/order_tasks_seed_0.json`
- 传统组语义解析和成本结果：`outputs/route_layer/traditional_experiment_v1/seeds/seed_0/traditional_order_routes_costs_seed_0.json`
- 传统组路线与端点验证：`outputs/route_layer/traditional_experiment_v1/seeds/seed_0/visualizations/orders/task-0000-0009__local_dijkstra.json`
- R4 Qwen 解析、同图等价性及公开任务：`outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/preflight.json`
- R4 单步状态与审计：`outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1/state.json`、`final_audit.json`
- 本次仅读取本地产物；没有模型请求。
