# 地图需求数据包

地图底图固定显示完整 OSM 原始道路线、候选围栏、单行属性和 OSM 信号点。上车热度点、下车热度点、筛选候选点、Dijkstra Link 和需求拥堵层均为空，直到用户导入 `map_order_demand_package` v1 JSON。格式定义见 `map_order_demand_package.schema.json`。

## 当前演示数据包

`porto-map-order-package-v1.json` 包含 840 个 100 m 上车格、866 个 100 m 下车格、222 个需求节点和 22,977 个 200 m OD 对，加权需求 355,525 次。数据源为 Porto 已完成出租车轨迹聚合，不含司机 ID、订单 ID 或原始轨迹；它不是在线网约车订单，也不是正式固定车队实验订单。

## 数据格式

- `pickup_points` / `dropoff_points`：所有聚合端点，字段为 `point_id`, `lon`, `lat`, `count`。
- `demand_nodes`：OD 网格中心，字段为 `node_id`, `lon`, `lat`。
- `demand_pairs`：聚合需求，字段为 `origin_node_id`, `destination_node_id`, `order_count`。
- 逐单情景可用 `orders` 替代 `demand_pairs`；每项含 `order_id`, `pickup`, `dropoff`，可选 `order_count`。
- `metadata.source_type`、`source_period` 与 `trip_count_semantics` 说明数据是历史代理、实测订单还是合成情景；展示层保留来源说明，不混称。

## 导入后的计算

1. **候选点：**按上车次数取 Top N，再并入所有次数达到阈值的点（并集）。默认 Top 30，阈值 0 表示关闭。上车、下车、候选点各自有显示开关。
2. **候选 Link：**候选点吸附到 300 m 内最近的机动车可路由节点，以道路长度运行有向 Dijkstra。对每一对可匹配候选点计算方向路线，并把共用的底层 OSM 道路片段叠加显示；点到道路节点的接入线也会显示。单行方向生效，尚未解析的转向限制使该 Link 仍属于预览，不是正式路线校验。
3. **需求负载与通行时间：**OD 端点吸附到同一机动车图，按长度最短路分配到基准道路路径，并把 `order_count` 累加到每条路段。两个端点落到同一接入节点的 OD 不产生路段负载；无可接入/不可达的 OD 单独计数。只有实际承载需求的道路显示绿—黄—红负载层。
4. **动态演示：**地图的仿真时间滑块驱动演示信号相位与背景交通系数，进而更新队列、估算信号等待与路段通过时间。当前 60 秒信号周期、相位偏移、道路等级自由流速度、背景变化和排队率均为演示参数，不是 Porto 实测值。

路段负载按 `loadRatio = log(1 + segmentDemand) / log(1 + maxSegmentDemand)` 归一化。当前演示使用 `backgroundFactor = 0.5 + 0.5 sin(2πt/180 − π/2)`、`dynamicDemand = loadRatio × (0.65 + 0.35 × backgroundFactor)`、`queue = min(30, 18 × dynamicDemand + 3 if red)`；估算通行时间为道路等级自由流时间 + `queue / 0.45` 秒 + 当前信号至下一绿灯的等待秒数。颜色在绿到红之间随负载、背景和信号等待变化。这是可解释的占位计算，不能作为真实交通预测精度结论。

OSM 明确机动车禁行、准入待核实、以及不在当前机动车候选类别内的 Way 统一放入单一“机动车禁行／未纳入路由”图层并从 Dijkstra 图排除；它表示本实验路由规则，不是逐条现场核验结论。其余原始道路都保留为底图。Map UI 只保留用户指定的 Link、不可路由道路、单行线（含方向箭头）、上下车点、筛选候选点、信号灯和拥堵开关；道路样式与方向箭头共用一个单行线开关。

当前没有 Porto 实时信号相位或交通速度数据。正式实验需以冻结 SUMO 网络、背景车流和共同预测器替换演示参数，并由车辆进入/离开 Link 的事件记录实际通过时间。围栏仍是候选区域，需在正式实验前复核。

来源与许可：Porto Taxi Service Trajectory，UCI / Figshare，CC BY 4.0；OpenStreetMap contributors，ODbL。
