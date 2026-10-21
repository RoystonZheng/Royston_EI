# NYC DOT E-ZPass 历史路段速度数据

数据源：[NYC Open Data — EZ Pass Readers 2021-July 2024](https://data.cityofnewyork.us/Transportation/EZ-Pass-Readers-2021-July-2024/erdf-2akx)，由 NYC DOT 提供。官方页面说明数据覆盖 2021-04-08 至 2024-07-07；记录包含传感器路段、编码折线、长度、聚合区间、样本数、中位行程时间和中位速度。

## 当前保存内容

`snapshots_2024-01_C4_v1/` 只保存 C4 场景开始时刻 `2024-01-20T23:20:59` 的单个历史快照：351 条唯一传感器路段记录。清单 `manifest.json` 保留原始 JSON 和派生 CSV 的字节数、SHA-256、查询时间、字段、borough 分布和可用行程时间记录数。此快照用于检查传感器覆盖，不是完整历史数据集，也没有重跑调度器。

`cscl_manhattan_2026-10-20/` 保存当前 NYC Open Data CSCL 的曼哈顿 14,111 条道路要素，用于把 DOT 折线候选匹配到有方向的街道中心线。采集时间、API 查询和 SHA-256 见该目录的 `manifest.json`。CSCL 是 2026 年当前道路几何，不是 2024 年历史版本。

`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_match_preflight_v1/` 保存可复现的传感器—中心线匹配筛查结果。当前 133 条有效曼哈顿 DOT 行程时间中，129 条通过名称及几何筛选；两条 Battery Park Underpass 仅有几何候选，Williamsburg Bridge 两条记录未被曼哈顿 CSCL 切片完整覆盖。该结果不是中心线边成本矩阵。

`cscl_manhattan_2026-10-20/` 同级数据目录还包含 `c4_zone_subset_2026-10-20/`：当前 TLC Taxi Zone API 返回了 C4 请求的 126 个区域中的 125 个多边形，`LocationID=265` 未返回。`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_cost_overlay_preflight_v1/` 保存部分成本映射及 C4 可达性上界：完整观测弧只有 547 条，候选成本覆盖 4.233% 的曼哈顿有向道路几何；在乐观区域免费接驳假设下，仅 54/790 单具有已观测有向路径。因此该 DOT 子网不能替换完整 C4 成本矩阵。

## 使用边界

- DOT 记录是被监测道路段的速度/时间观测，不是全市完整路网；当前快照有 253 条记录同时满足 `n_samples > 0` 和 `median_tt_sec > 0`。
- `polyline` 仅给出路段几何。构建路径还需要连通关系、行驶方向、转向规则以及未监测道路的成本处理。
- CSCL `TRAFDIR` 官方定义为 `FT`（顺地址/数字化方向）、`TF`（逆向）、`TW`（双向）、`NV`（非机动车）；方向一致性检查已在 `dispatch-layer-nyc-dot-cscl-match-preflight-2026-10-20.md` 记录，但边成本分配、拓扑连通性和 C4 路径覆盖尚未验证。
- `aggregation_period_sec` 为 900 的记录是 15 分钟聚合中位数；分钟级更新时间不意味着 1 分钟独立样本。
- TLC 订单数据不含真实车辆 ID 或历史空闲状态，因此 DOT 路段时间不能单独构成真实派车回放或车辆接驾 ETA。
- 本快照不能直接替代既有 NYC zone OD 代理矩阵，也不能作为 NYC 服务率或 D0/D1 性能结论。

详细空间覆盖审计见 [`docs/dispatch-layer-nyc-dot-road-cost-data-audit-2026-10-20.md`](../../../docs/dispatch-layer-nyc-dot-road-cost-data-audit-2026-10-20.md)。
CSCL 几何匹配结果见 [`docs/dispatch-layer-nyc-dot-cscl-match-preflight-2026-10-20.md`](../../../docs/dispatch-layer-nyc-dot-cscl-match-preflight-2026-10-20.md)；子段成本覆盖和 C4 区域可达性见 [`docs/dispatch-layer-nyc-dot-cost-overlay-preflight-2026-10-20.md`](../../../docs/dispatch-layer-nyc-dot-cost-overlay-preflight-2026-10-20.md)。
