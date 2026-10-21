# NYC DOT E-ZPass 到 CSCL 曼哈顿匹配预检

日期：2026-10-20。状态：**几何/道路名匹配及方向一致性预检；不是路由或派单实验结果**。

## 结论

把 NYC DOT 2024-01-20 的曼哈顿 E-ZPass 传感器路段匹配到当前 NYC Street Centerline（CSCL）在数据上可行：133 条有有效行程时间的曼哈顿传感器路段中，129 条通过道路名与几何接近双条件筛选（96.99%）。对这些路段的采样方向检查中，2,530 个可比较单行区间均与 CSCL `FT/TF` 方向一致，另有 1,381 个双向区间；没有发现方向冲突。

这只证明存在可继续验证的道路要素候选。当前未把中位行程时间映射/分配给 CSCL 边，没有构造有向最短路或完整 OD 成本矩阵；因此不能称为已可用于全域调度回放。CSCL 是 2026 年当前道路中心线，而行程时间是 2024 年 1 月快照，历史拓扑变化也未核验。

## 数据与官方字段依据

- E-ZPass 输入：`data/dispatch/nyc_dot_ezpass_2021_2024/snapshots_2024-01_C4_v1/c4_2024-01-20_23-20-59.json`，NYC DOT 于 `2024-01-20T23:20:59` 的单个历史快照；曼哈顿有 180 条记录，其中 133 条满足 `n_samples > 0` 且 `median_tt_sec > 0`。
- CSCL 输入：[`data/dispatch/nyc_dot_ezpass_2021_2024/cscl_manhattan_2026-10-20/`](../data/dispatch/nyc_dot_ezpass_2021_2024/cscl_manhattan_2026-10-20/)，按曼哈顿 borough code 查询得到 14,111 条街道要素；其中 `TRAFDIR` 为 `FT/TF/TW` 的车辆方向要素 11,435 条。快照下载时间、查询 URL 和 SHA-256 在 `manifest.json`。
- NYC Open Data 将 CSCL 描述为包含地址范围、交通方向、道路类型和路段类型的道路床中心线。[NYC Open Data CSCL 页面](https://data.cityofnewyork.us/City-Government/NYC-Street-Centerline-CSCL-/inkn-q76z/about_data)
- 附件字段字典定义 `TRAFDIR` 为相对街段地址范围的交通方向：`FT` 顺向、`TF` 逆向、`TW` 双向、`NV` 非机动车。[官方 Centerline.pdf 附件](https://data.cityofnewyork.us/api/views/inkn-q76z/files/4cff63bb-aeb0-4ca3-adb5-d6027dc133d5?download=true&filename=Centerline.pdf)
- NYC 官方 CityOfNewYork 地理元数据说明左右侧地址范围相对线段数字化方向定义；因此本次方向筛查按 `FT=几何数字化方向`、`TF=其反向` 解释。[CityOfNewYork Street Centerline metadata](https://github.com/CityOfNewYork/nyc-geo-metadata/blob/main/Metadata/Metadata_StreetCenterline.md)

## 匹配口径

1. 解码 DOT 路段编码折线，将 E-ZPass 和 CSCL 的 WGS84 经纬度投影为以曼哈顿中部为基准的局部平面坐标，仅用于短距离筛查。
2. 沿每条 E-ZPass 几何每 15 米取样，查找同名 CSCL 车辆道路几何；点到 CSCL 折线段最近距离不超过 25 米计入覆盖。
3. 道路名统一常见缩写、序数街名，并显式处理 6th Ave/Avenue of the Americas、9th Ave/Columbus Ave 和 FDR 俗名；不对桥隧等多街名走廊强行猜测。
4. 只有至少 90% 采样点同时满足道路名候选及 25 米距离，才列为 `name_geometry_match`。另列仅几何接近的候选，避免把路口附近的平行/交叉道路当成路名匹配。
5. 对匹配路径相邻采样点，比较 E-ZPass 折线点序与最近 CSCL 边方向；方向点积绝对余弦小于 0.5 的非平行区间不用于方向一致性比例。双向 `TW` 直接视为方向兼容；单向 `FT/TF` 按几何方向点积判断。

完整实现：[`scripts/audit_ezpass_cscl_match.py`](../scripts/audit_ezpass_cscl_match.py)。

## 结果

| 项目 | 结果 |
|---|---:|
| 曼哈顿 E-ZPass 传感器路段 | 180 |
| 有有效行程时间的传感器路段 | 133 |
| 道路名与几何双条件匹配 | 129 / 133（96.99%） |
| 只有几何接近、名称未能确认 | 2 条有效时间路段（Battery Park Underpass） |
| 曼哈顿 CSCL 切片内未匹配 | 2 条有效时间路段（Williamsburg Bridge，跨区桥梁） |
| 与已匹配有效传感器路段关联的唯一 CSCL `PHYSICALID` | 575（不可视作覆盖率分子） |
| 可比较的单向采样区间 | 2,530，方向一致 2,530，冲突 0 |
| 双向采样区间 | 1,381 |

关联到 575 个 `PHYSICALID` 不是“已观察成本覆盖 575 条完整路段”：传感器折线和 CSCL 要素的空间范围未逐边精确相交，单条传感器时间也尚未分配到路线中的子段。5 分钟 C4 的 790 单和 126 个目的地区域没有因此转换为路线成本。

机器可读记录：[`summary.json`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_match_preflight_v1/summary.json)、[`sensor_cscl_match.csv`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_match_preflight_v1/sensor_cscl_match.csv)、[`sensor_cscl_match.json`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/cscl_match_preflight_v1/sensor_cscl_match.json)。

## 仍未验证

- 逐条中心线边与 DOT 传感器折线的精确相交和覆盖长度；当前局部投影/15 米采样只用于筛查。
- 传感器 `median_tt_sec` 与 CSCL 边的合理时间/速度分配；不能把整段 DOT 时间复制给每个匹配的 CSCL 要素。
- 街段端点吸附、交叉口连接、转向规则、桥隧连接和子图连通性。
- 8 个有有效传感器时间的 C4 上车区内，实际车辆起点到指定上车点的可达率、观测成本占路径长度比例，以及需求区域间的可用路径覆盖。
- 当前 2026 中心线与 2024 交通观测期的拓扑差异。

## 下一步

对这 129 条高置信候选及 2 条几何候选做精确的沿线 map matching：把传感器折线切分并吸附到 CSCL 有向边，记录每个子段的传感器 ID、方向、覆盖长度和未覆盖长度；通过覆盖路段的速度/时间口径生成带缺失标记的边成本。随后只对原 C4 场景可映射的车辆—上车点组合做可达率与已观测成本覆盖统计，并据此判断要不要生成受限 NYC 交通实验。全量 OD 和路径覆盖未过门槛前，不重跑 D0/D1 性能比较。路线层保持独立；这份 TLC + DOT 数据不替代现有 100×100 路线层对照。

### 执行更新（2026-10-20）

上述子段成本覆盖及 C4 区域可达性预检已完成，具体结果见 [`dispatch-layer-nyc-dot-cost-overlay-preflight-2026-10-20.md`](dispatch-layer-nyc-dot-cost-overlay-preflight-2026-10-20.md)。覆盖仅为曼哈顿有向几何的 4.233%；乐观免费区域接驳假设下仅 54/790 单有已观测有向路径。因此不将该 DOT 子网用于完整 C4 派单性能重跑。路线层也不重做。当前后续若继续 NYC 数据，只能先构造单独的受限走廊场景并明确其样本筛选和区域接驳假设。
