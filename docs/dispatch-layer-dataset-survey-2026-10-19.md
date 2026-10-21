# 调度层实验候选数据集初筛

日期：2026-10-19  
研究对象：一对一无人网约车动态派单；每车同一时刻服务一个订单。  
范围：公开数据源与字段适用性初筛；2026-10-19 后按用户要求下载 NYC TLC 2024 全年文件和区域资产，并完成 2024-01 请求轨迹审计、候选输入导出和一次 D0/D1 zone-cost 探索性试跑。该试跑未达到可用的正式实验条件。

## 结论

**推断：**NYC TLC High Volume FHV 行程数据适合作为真实请求时间和区域 OD 分布来源，但不足以单独复原车队调度过程。公开数据没有完整未服务/取消请求、车辆逐时位置/可用状态或道路路径成本。一次探索性接入试跑也显示，直接把载客 OD 行程时间代理为空车接驾成本会造成极少可行候选，故当前只能作为需求轨迹资料，不能据此报告派单性能。保留现有结果作为受控合成基线。全年原始文件已下载至 [`data/dispatch/nyc_tlc_hvfhv_2024/`](../data/dispatch/nyc_tlc_hvfhv_2024/README.md)，校验清单见该目录 `manifest.json`。

**推断：**如需对传统分配/优化方法做更接近真实路网的离线补充比较，可另看 DARP 实例集。该基准提供真实需求、车辆初始位置和行程时间矩阵，但问题设定为静态拼车，不能与当前动态一对一派单结果混为一组。

## 候选数据集

| 数据集 | 已核实内容 | 与当前实验的匹配度 | 主要限制 |
|---|---|---|---|
| NYC TLC High Volume FHV（优先候选） | **Fact：**NYC 官方按月发布高频网约车行程数据；公开字段包括 `request_datetime`、`pickup_datetime`、`dropoff_datetime`、`PULocationID`、`DOLocationID`、`trip_time`、`trip_miles` 和 `shared_match_flag`。TLC 数据字典说明每行对应一笔已派遣行程；即时请求时间按提交规范使用乘客来电/请求时间。`on_scene_datetime` 仅适用于无障碍车辆。[官方数据页](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)、[高频 FHV 数据字典](https://www.nyc.gov/assets/tlc/downloads/pdf/data_dictionary_trip_records_hvfhs.pdf)、[FHV 提交规范](https://www.nyc.gov/assets/tlc/downloads/pdf/csv_instruction_manual.pdf) | **Inference：**可用请求时间和起终点区域构造真实需求事件流；`pickup_datetime - request_datetime` 是请求至实际上车的历史间隔，可作等待参考，但不等同于普通车辆的司机到达时刻。为保持一对一，可剔除 `shared_match_flag=Y` 的实际拼车行程。官方页提供 taxi zone 对照表/区域文件，便于统一区域 ID。 | **Fact：**每行是已完成/已派遣行程。**Inference：**该数据不包含全体未服务或取消请求，也不含完整逐时车辆位置/空闲状态；因此只能作为需求轨迹重放，车队状态和反事实接驾路线仍需另行构造。实际可行窗口与结果受“只观察完成行程”影响。 |
| 大规模 DARP 真实需求实例 | **Fact：**实例涵盖 NYC、Chicago、Washington, DC；文件含请求、车辆及其容量、地点间最短旅行时间矩阵，全部为静态实例；数据按 CC BY 4.0 发布。[项目与格式说明](https://github.com/aicenter/Ridesharing_DARP_instances)、[论文](https://doi.org/10.1109/ITSC57777.2023.10422146) | **Inference：**可将车辆容量限制为 1，并使用其车辆位置和旅行时间矩阵，补充离线车辆—订单分配/优化器比较。 | **Fact：**仓库实例是静态 DARP/拼车基准，不是动态到达的一对一派单流。改成容量 1 后仍改变了问题设定，只适合作为独立补充实验。 |
| Chicago TNP Trips | **Fact：**芝加哥官方开放平台发布网约车公司上报的行程记录，字段包括起止时间、行程时长/里程和起终区域；官方 2026 数据集说明起止时间四舍五入到 15 分钟。[官方数据集示例](https://data.cityofchicago.org/Transportation/Transportation-Network-Providers-Trips-2026-/8bbv-3wbe/data)、[官方 2022 数据集说明](https://data.cityofchicago.org/Transportation/Transportation-Network-Providers-Trips-2022/2tdj-ffvb/data) | **Inference：**可做较粗粒度的区域需求强度、OD 分布或时段需求校准。 | **Inference：**15 分钟时间精度不适合直接验证秒级接驾等待或订单到达后的动态派单；公开字段也不能还原实时车队状态。 |
| DiDi Ride-Hailing Synthetic Dataset | **Fact：**滴滴科技合作页提供合成数据，4 年、200 个空间区域、30 分钟粒度，共约 1,402.6 万条记录；指标目标经过匿名化，数据不含真实用户轨迹或运营数据，并需通过页面申请获取。[官方数据集说明](https://outreach.didichuxing.com/opendata/datasets/ride-hailing) | **Inference：**适合需求量预测、天气/节假日/大型活动情景压力测试。 | **Fact：**模式是区域—时间指标而非订单级起终点/车辆状态。**Inference：**不能直接作为车辆—订单匹配实验输入。 |

## 建议的实验接入方式

1. **保留已完成的 `traditional_experiment_v1`。** 当前 D0/D1 是受控合成地图上的离线模拟结果；公开行程数据接入后应另建数据版本，不覆盖原结果。
2. **2024-01 审计已完成。** 1 月请求 cohort 共 19,664,474 行；候选输入保留 19,248,258 行，细节见 [审计记录](dispatch-layer-nyc-tlc-2024-01-audit.md)。全行关键字段缺失为 0、区域 lookup 均匹配；实际拼车、请求晚于上车和少量无效时序已按规则排除。
3. **需求轨迹已生成，并做过一次不合格的适配预检。** 使用 `request_datetime` 作为到达时间，区域 ID 作为 OD；轨迹不含实际 pickup/dropoff 结果字段。2024-01-16 18:00–19:00 中，35,349 条跨区候选里固定抽 100 条，配 10 辆合成车运行 D0/D1。两法各服务 5 单（5%），所有可行轮次只有一个候选，接驾等待与空驶均为 0，D0/D1 分配相同。这是代理成本与车队缩放失配的表现，不是 NYC 服务率或算法性能结果。详见[试跑审计](dispatch-layer-nyc-tlc-zone-proxy-pilot-2026-10-19.md)。
4. **先修正路线成本和车队规模模型。** 载客 OD 的完整 trip_time 不能未经校准直接作为空车接驾时间；需要道路路径时间、区域内部接驳距离，并为抽样后的订单量设定匹配的车队数量、初始位置和忙闲状态。公开行程数据不包含这些状态。
5. **预检可行候选密度后再决定正式实验。** D0/D1 应共享需求序列、地图/成本模型和车队初始状态；先确认 300 秒接驾约束下存在足够的可行车辆—订单竞争，再扩展时段和种子，不覆盖原有合成结果。
6. **多车竞争场景输入已生成并完成 C2 机制预检。** 从 2024-01 候选轨迹切出 1、4、44、790 单四类窗口并配置合成车。C2 首轮 D0/D1 均产生 12 个可行候选并分配 3 单，一对一验证通过；这只验证加载和分配机制。C3 当前有 Zone 1 节点及 164→1 OD 缺失于旧 pilot 的最大强连通分量成本矩阵，需先从完整训练期 zone OD 数据重建代理图。合成车辆同区初始化使区域级接驾成本为零，不能据此报告道路 ETA、真实车队表现或算法性能。详见[场景记录](dispatch-layer-nyc-tlc-multivehicle-scenarios-2026-10-19.md)、[C2 输出包](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/c2_mechanism_preflight_v1/)与[派生场景数据包](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/multi_vehicle_competition_scenarios_v1/)。

## 证据边界

- 以上字段和发布方式来自官方数据页、数据字典或项目数据说明；“适合/不适合当前实验”属于按本项目动态一对一派单定义作出的 **Inference**。
- 初筛阶段未分析原始样本。2026-10-19 后续已下载完整 NYC TLC 2024 HVFHV 年度文件（12 个月）及区域对照资产；逐文件字节数和 SHA-256 见 [`manifest.json`](../data/dispatch/nyc_tlc_hvfhv_2024/manifest.json)。目前仅 2024-01 请求 cohort 完成行级审计并生成候选回放轨迹；另运行一次固定窗口 D0/D1 代理试跑，但不构成正式调度对照实验。其他月份尚未审计。
- 路线层不使用本数据集重跑：其空间端点是 taxi-zone 区域 ID，配套几何是区域多边形，不含道路图、边行程时间或 GPS 路径轨迹。该数据可辅助调度层区域级 OD 回放，但不能替代路线层所需的道路网络与逐路径成本。
- NYC Open Data 条款说明数据可公开使用，但政府不保证完整性、准确性或适用性；使用时需记录下载日期和数据版本。[NYC Open Data 使用条款](https://data.cityofnewyork.us/stories/s/Terms-of-Use/k9k7-3cje)
