# 路线层第一轮传统算法运行记录

日期：2026-10-14  
证据类型：已完成离线模拟实验  
运行范围：只运行传统路线基线，不包含纯 LLM、车辆 Agent 或 Agent-readable 对照。

## 运行配置

- 地图：100×100 有向离散道路图。
- 随机种子：0–9。
- 每个种子：10 条订单任务，其中 7 条精确节点任务、3 条语义地点任务。
- 正式场景：可行、边界、绕行、非法端点、不可达五类。
- 方法：
  - R0：`full_map_dijkstra`
  - R1：`local_dijkstra`
  - R2：`local_a_star`
- 语义解析：固定 `graph_only`，不读取路牌/电线杆道路特征实体，也不读取评估器真值。
- 费用：沿用 `energy_v1` 和 `tesla_cn_station_peak_v1` 价格配置。
- 运行入口：`python3 scripts/run_traditional_experiment.py --seed-count 10 --seed-start 0 --size 100 --output-dir outputs/route_layer/traditional_experiment_v1 --price-config-json configs/route_layer/price_smoke_v1.json`

## 结果摘要

- 运行状态：`passed`。
- 订单轨迹记录：10 个种子 × 10 条任务 × 3 种方法 = 300 条。
- 正式场景轨迹记录：10 个种子 × 5 类场景 × 3 种方法 = 150 条。
- 总轨迹记录：450 条。
- 每条记录同时保存：
  - 原始搜索 JSON；
  - 每一段搜索的扩展节点顺序；
  - 每次扩展的累计代价、优先级和队列规模；
  - 最终节点/边路径；
  - 路线运行时间、能耗和费用字段；
  - 独立 HTML 回放，可用滑块按搜索步数回放。

订单批次中，R0/R1/R2 在每个种子均完成 10/10 条任务。正式场景中，可行、边界、绕行三类按预期完成，非法端点和不可达两类按预期记录为失败；失败场景保留失败原因和未完成/部分完成的检索轨迹，不伪造路线费用。

## 产物入口

- 总索引：[traditional_experiment_v1/index.html](../outputs/route_layer/traditional_experiment_v1/index.html)
- 总清单：[manifest.json](../outputs/route_layer/traditional_experiment_v1/manifest.json)
- 单种子订单轨迹索引：`outputs/route_layer/traditional_experiment_v1/seeds/seed_<seed>/visualizations/orders/index.html`
- 单种子正式场景轨迹索引：`outputs/route_layer/traditional_experiment_v1/seeds/seed_<seed>/visualizations/formal_scenarios/index.html`
- 单种子路线和费用 JSON：`outputs/route_layer/traditional_experiment_v1/seeds/seed_<seed>/traditional_*_routes_costs_seed_<seed>.json`

## 证据边界

这轮结果证明传统算法实验链路、成本评估链路和逐任务检索轨迹持久化已经跑通。它不证明 Agent-readable 地图带来收益，也不证明 LLM 或车辆 Agent 的路线效果；后续对照必须复用相同的地图、任务、场景、验证器和成本配置。
