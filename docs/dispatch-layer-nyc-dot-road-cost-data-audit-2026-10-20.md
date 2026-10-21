# NYC DOT 路段成本数据可行性审计

日期：2026-10-20。状态：**官方数据源与单时段空间覆盖预检；不是调度实验结果**。

## 结论

NYC DOT 的 E-ZPass 历史路段数据与 2024 年 1 月 TLC 场景日期重叠，可以为部分监测道路段提供历史行程时间；但传感器数据不是完整街道图，不能直接给 C3/C4 生成全区域跨区接驾时间矩阵。C4 当前 790 单包含 126 个目的地区域，现有监测网络仅支持其中一小部分区域端点。故此步只下载并审计一份 C4 时刻快照，未替换现有调度成本矩阵、未重跑 D0/D1，也未重跑路线层。

## 核验的数据源

1. **NYC DOT E-ZPass Readers 2021–July 2024**：官方数据页说明历史数据覆盖 2021-04-08 至 2024-07-07，包含传感器路段、编码折线、`link_length_ft`、`aggregation_period_sec`、`n_samples`、`median_tt_sec` 和 `median_speed_fps`。本地保存的快照时间为 `2024-01-20T23:20:59`，与 C4 五分钟场景开始时刻相邻。
2. **NYC Street Centerline (CSCL)**：官方资料提供城市街道中心线、交通方向、道路类型和路段几何，可作为后续建立完整拓扑的候选输入；在线字段还列出 `avgtravtime` 和 `posted_speed`，但当前可读字段说明未给出 `avgtravtime` 的单位和生成口径，不应直接把它当成已经校准的历史 ETA。
3. **NYC DOT Traffic Speeds**：官方将其描述为来自五区传感器的实时速度数据，主要覆盖主干道和高速路，数据每日更新、一天多次/分钟变化。这个滚动数据源不应当作 2024 年 1 月的历史快照。本审计优先选用有历史覆盖的 E-ZPass 表。
4. **TLC HVFHV Trip Records**：公开字段包括请求、上车和下车时间、起终 taxi zone、载客里程和时间，但没有逐车位置或可用/空闲状态。TLC 的月报能给出 base 级每月 unique dispatched vehicles，只能作为规模参考，不能还原特定五分钟的车辆状态。

官方来源：

- [NYC DOT E-ZPass Readers 2021–July 2024](https://data.cityofnewyork.us/Transportation/EZ-Pass-Readers-2021-July-2024/erdf-2akx)
- [NYC Street Centerline (CSCL)](https://data.cityofnewyork.us/City-Government/Centerline/3mf9-qshr)
- [NYC DOT Traffic Speeds](https://data.cityofnewyork.us/Transportation/DOT-Traffic-Speeds/i4gi-tjb9)；[NYC DOT 对实时速度探测器的说明](https://www.nyc.gov/html/dot/html/about/datafeeds.shtml)
- [TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)；[HVFHV 字段字典](https://www.nyc.gov/assets/tlc/downloads/pdf/data_dictionary_trip_records_hvfhs.pdf)
- [TLC Aggregated Reports](https://home4.nyc.gov/site/tlc/about/aggregated-reports.page)

## C4 快照与覆盖统计

公开 Socrata API 查询条件为 `median_calculation_timestamp = '2024-01-20T23:20:59'`，请求字段见快照清单。原始 API 响应与派生 CSV 均已保存在 [`data/dispatch/nyc_dot_ezpass_2021_2024/snapshots_2024-01_C4_v1/`](../data/dispatch/nyc_dot_ezpass_2021_2024/snapshots_2024-01_C4_v1/)；manifest 固定记录查询 URL、字节数与 SHA-256。

| 核查项 | 结果 |
|---|---:|
| 传感器路段记录 / 唯一路段 | 351 / 351 |
| 同时 `n_samples > 0` 且 `median_tt_sec > 0` | 253 |
| 有效路段的聚合窗口 | 900 秒（15 分钟） |
| borough 分布 | Manhattan 180、Brooklyn 46、Queens 89、Staten Island 36 |
| 15 米端点吸附后的无向连通分量 | 40 |
| 最大分量中的有效路段 | 128 |

快照中没有 Bronx 路段。15 米吸附的连通性只用于覆盖上界诊断：没有校验道路方向、转向限制或路线是否可通行，因此最大分量不能直接当作可用的有向导航图。

将传感器折线与当前 TLC taxi-zone 多边形作空间叠加后，C4 的 12 个上车区域里有 9 个至少碰到一条监测线段，其中 East Village 的两条线段在该快照没有有效时间；因此只有 8 个区域有有效传感器时间。最大无向分量也触及这 8 个区域。C4 的 790 单覆盖 126 个目的地区域；只有 86 单落在这 8 个区域，51 单的起终区域都在其中。把 C4 限定到这些可监测区域会丢掉 739/790 单，不能代表原场景负载。

路段—区域相交采用已下载 TLC taxi-zone polygon 与 DOT 折线：按本地 `.prj` 将折线坐标转至同一平面坐标系，用折线顶点和相邻顶点中点判断区域相交。连通分量只用有效时间路段端点，并按 15 米容差合并；这些统计是单个 C4 时刻的空间覆盖审计，不是路径规划结果。

另外，API 对 C3 的 `2024-01-17 20:00–20:05` 和 pilot 的 `2024-01-16 18:00–18:05` 也返回了历史读数，说明 E-ZPass 表覆盖这些实验日期；C3 十秒订单突发仍短于其 15 分钟聚合区间，不能把分钟更新时间解读为十秒交通状态。

## TLC 车辆状态边界

HVFHV 公布的逐单字段没有车辆 ID 或每车空闲/忙碌状态。TLC 月度 FHV Base Aggregate 报告提供每个 base 的月度派单总数和 unique dispatched vehicles，但没有车辆位置、上线时间或逐时 availability。因此车队规模可用公开月度总量作宽泛校准参考，单次模拟的车辆位置和忙碌状态仍必须明确标成假设。

## 下一步

先做一个**曼哈顿传感器子网的有向路径成本预检**：用 CSCL 的几何与 `TRAFDIR` 建立完整有向拓扑，将 E-ZPass 有效路段时间匹配到拓扑边；单独报告观测边比例、路径中观测成本占比和缺失边处理，禁止静默补零。预检后再判断能否为 C4 的区域对构造完整接驾成本，以及需要把样本限制到哪些区域/时段。路径覆盖没有通过预先设定的门槛前，不重跑 C4 性能实验。

### 执行更新（2026-10-20）

已完成 E-ZPass—CSCL 名称/几何/方向筛查及后续子段成本覆盖和 C4 区域可达性上界筛查：分别见 [`dispatch-layer-nyc-dot-cscl-match-preflight-2026-10-20.md`](dispatch-layer-nyc-dot-cscl-match-preflight-2026-10-20.md) 与 [`dispatch-layer-nyc-dot-cost-overlay-preflight-2026-10-20.md`](dispatch-layer-nyc-dot-cost-overlay-preflight-2026-10-20.md)。虽然 129/133 条有效曼哈顿传感器读数通过名称与几何匹配，但实测速度候选只覆盖 4.233% 的曼哈顿有向道路几何；对 C4 订单区的乐观免费接驳检查只有 54/790 单存在已观测有向路径。因此当前结论是：不重跑完整 C4 性能比较，也不重做路线层。若继续使用 NYC DOT 成本，应先设计独立的受限走廊实验并明确区域接入和车辆初始位置。

当前不重跑路线层：TLC 订单数据没有道路边和 GPS 轨迹；DOT 速度快照只覆盖部分路段。若未来把 CSCL + 传感器数据正式接入路线层，应作为独立的 NYC 道路图基准，不能与现有 100×100 合成路线层结果混写。

## 产物

- DOT 原始快照：[JSON](../data/dispatch/nyc_dot_ezpass_2021_2024/snapshots_2024-01_C4_v1/c4_2024-01-20_23-20-59.json)、[CSV](../data/dispatch/nyc_dot_ezpass_2021_2024/snapshots_2024-01_C4_v1/c4_2024-01-20_23-20-59.csv)、[manifest](../data/dispatch/nyc_dot_ezpass_2021_2024/snapshots_2024-01_C4_v1/manifest.json)
- C4 场景与既有机制结果：[场景说明](dispatch-layer-nyc-tlc-multivehicle-scenarios-2026-10-19.md)、[C4 运行清单](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/c4_load_sweep_mechanism_v1/preflight_manifest.json)
