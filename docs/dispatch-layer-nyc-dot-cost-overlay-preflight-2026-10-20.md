# NYC DOT 成本子段映射与 C4 可达性预检

日期：2026-10-20。状态：**局部观测成本覆盖与乐观可达性上界筛查；不是路线或派单性能实验**。

## 结论

NYC DOT E-ZPass 的 2024 年 1 月速度可以映射到一部分当前 CSCL 曼哈顿有向几何子段，但覆盖度不足以替换 C4 的全域 zone OD 成本：129 条名称/几何匹配的有效传感器折线共长约 57.054 km，其中 56.976 km 被 10 米采样映射到 CSCL 候选子段；这些候选只覆盖曼哈顿 CSCL 有向道路几何 1,309.125 km 中的 55.417 km（4.233%）。

只保留至少 95% 长度被单个传感器覆盖的有向线段后，得到 547 条完整观测弧。即使把区域内任意已观测道路节点都当作免费起终点，C4 的 790 单里只有 54 单存在已观测有向路径（6.835%）；该值是乐观上界，不是可服务率。当前不应使用这份局部成本替换 C4 调度输入，也不应据此重跑 D0/D1 性能比较。

## 输入与复现

- DOT 输入：2024-01-20 23:20:59 E-ZPass 历史快照。有效观测定义为 `n_samples > 0` 且 `median_tt_sec > 0`。
- CSCL 输入：2026-10-20 下载的曼哈顿切片，14,111 条要素；车辆道路 `TRAFDIR=FT/TF/TW` 共 11,435 条。CSCL 几何不是 2024 历史版本。
- 区域输入：NYC TLC 当前出租车区域多边形；C4 126 个起终区域 ID 中返回 125 个，当前 API 切片未返回 `LocationID=265`。TLC 将出租车区域作为行程记录 `LocationID` 的空间参考。[NYC Taxi Zones 数据页](https://data.cityofnewyork.us/Transportation/NYC-Taxi-Zones/8meu-9t5y/data)
- C4 输入：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/multi_vehicle_competition_scenarios_v1/C4_manhattan_790_order_five_minute_load_sweep/orders.json`，790 条由通过筛选的已派遣/完成 TLC trip 记录派生的模拟订单、430 个不同 OD 区域对，来源时间窗 2024-01-20 23:20–23:25；它不代表全量服务请求或未服务需求。
- 完整执行脚本：`scripts/map_ezpass_costs_to_cscl.py`、`scripts/audit_ezpass_observed_graph_connectivity.py`、`scripts/audit_c4_orders_on_observed_cscl_graph.py`。所有输入 SHA-256 写在对应输出 JSON 中；街道方向字段的官方语义见 [`dispatch-layer-nyc-dot-cscl-match-preflight-2026-10-20.md`](dispatch-layer-nyc-dot-cscl-match-preflight-2026-10-20.md)。

## 成本映射方法

1. 只纳入前一轮 129 条同时通过名称与几何筛选的有效传感器折线；每条折线每 10 米取样，选择 25 米内最近且名称和 `FT/TF/TW` 方向相容的 CSCL 几何原语。
2. 将采样区间端点投影到对应 CSCL 原语，按有向原语合并被观测区间；用 DOT `median_speed_fps` 估算**覆盖子段**的时间，不把整条传感器的总行程时间复制给每条匹配道路。
3. 未覆盖区间没有成本值，不补零、不猜速度。一个有向原语被至少一条传感器单独覆盖 95% 以上时，才进入保守“完整观测弧”连通性筛查。
4. `TW` 路段按两个允许方向计入有向几何分母；`FT/TF` 各按一个方向计入。
5. DOT 速度字段与 `link_length_ft / median_tt_sec` 的交叉核对：129 条记录的相对差异中位数 0.020%，最大 0.516%。这支持源字段数值相互一致，不验证路线时间真值。
6. CSCL 端点按 0.5、2、5、10 米容差做连通敏感性诊断；C4 区域可达性上界使用较宽松的 10 米端点吸附和完整观测弧。

## 结果

| 项目 | 结果 |
|---|---:|
| 通过名称/几何的有效传感器路段 | 129 条 |
| 传感器折线总长度 / 映射长度 | 57.054 km / 56.976 km（99.86%） |
| 曼哈顿 CSCL 车辆有向几何总长度 | 1,309.125 km |
| 至少有 DOT 成本候选的有向子段覆盖长度 | 55.417 km（4.233%） |
| 95% 以上被单个传感器覆盖的完整观测弧 | 547 条 |
| 10 米吸附下完整观测图的弱连通分量 | 67 个；最大分量 152 弧 |
| C4 上车区没有完整观测弧相交的区域 | 79、125、148、231，共 443 单上车请求 |
| 起终区都有完整观测图节点相交的 C4 订单 | 141 / 790 |
| 乐观条件下存在已观测有向路径的 C4 订单 | 54 / 790（6.835%） |
| C4 126 个 OD 区域中当前未取得多边形的 ID | 265，涉及 59 个下车订单 |

不同端点吸附容差下，完整观测子图仍明显碎片化：0.5–2 米时 78 个弱连通分量，最大分量 116 弧；5 米时 76 个、最大 148 弧；10 米时 67 个、最大 152 弧。放宽到 10 米也没有形成覆盖 C4 的完整有向路网。

区域可达检查是刻意偏乐观的上界：只要某条完整观测道路原语碰到区域边界，就允许车辆从该区域任意观测道路节点进入或离开，且区域内接驳时间按 0 处理；模型没有加入真实车辆位置、精确上车点、转向限制或坡道/立交分层。因此 54/790 不能当作路线成功率。

机器可读产物：[`成本映射汇总`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_cost_overlay_preflight_v1/summary.json)、[`传感器—有向原语成本候选`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_cost_overlay_preflight_v1/directed_cost_candidates.csv)、[`完整观测图连通性`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_cost_overlay_preflight_v1/observed_full_primitive_graph_connectivity.json)、[`C4 区域可达性上界`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_cost_overlay_preflight_v1/c4_zone_observed_graph_reachability.json)。

## 决策与后续工作

- **不重做路线层。** TLC zone 级行程与局部 E-ZPass 速度不构成现有 100×100 路线层的替代地图或对照输入。
- **不把 DOT 子网接入完整 C4 派单基线。** 只观测到 4.233% 的有向几何，且 4 个原始 C4 上车区没有完整观测道路；可达性 54 单还是免费区域接驳的上界。
- 继续传统调度算法比较时，应沿用能提供统一全量候选成本的 C2/C3 受控竞争场景，或清楚标注使用 TLC zone OD 代理矩阵的 C4 负载机制实验；这些结果不能说成 DOT 道路成本上的真实派单性能。
- 如需额外探索 NYC 道路数据，可单独设计一个小型受限走廊场景，先解决区域锚点、残缺区域 ID、部分路段成本合并方式和车辆初始位置，再决定是否运行，不与完整 C4 混成一个结果。
