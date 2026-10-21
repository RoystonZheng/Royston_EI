# NYC TLC zone-cost 调度层探索性试跑

日期：2026-10-19。状态：**数据适配预检；不是正式实验结果**。

## 目的与输入

用一段 NYC TLC 2024-01 请求轨迹检查现有 D0 贪心与 D1 全局优化器能否在 taxi-zone 代理图上运行，并观察是否出现多车可接同一订单的竞争。需求取 2024-01-16 18:00–19:00 的请求；从训练期 2024-01-01 至 2024-01-15 的已派遣单程行程构造有向区域 OD 中位数成本图。只保留训练图最大强连通分量内的跨区事件，以 `MD5(source_record_id)` 固定抽取 100 条。车队为 10 辆合成车辆，初始区域按训练期上车区域频率有放回抽样；no-show 设为 0。D0、D1 使用相同订单、区域成本图、车辆状态和场景指纹。

## 观察结果

| 指标 | D0 贪心 | D1 优化器 |
|---|---:|---:|
| 抽样订单 | 100 | 100 |
| 完成订单 | 5 | 5 |
| 模拟服务率 | 5% | 5% |
| 可行候选数 | 5 | 5 |
| 存在同一订单多车可行的轮次 | 0 | 0 |
| 总空驶距离 | 0 km | 0 km |
| 已服务订单接驾等待 | 均为 0 秒 | 均为 0 秒 |

该小时候选轨迹有 38,782 条事件，其中 35,349 条在训练图内且为跨区事件；100 条仅占该分母的 0.283%。每个有可行候选的轮次都只有一个车辆—订单候选。D0 与 D1 的五笔分配完全一致，不能据此评价两算法的相对性能。

## 为什么不能作为结果

这次试跑说明当前数据接入方案失配：区域 OD 的完整载客 trip_time 被当作空车接驾时间，且 10 辆合成车铺在 260 个区域；结果仅出现 5 个可行候选，全部 pickup-leg 时间与空驶距离为零，没有多车竞争。其余 95 单未服务反映这组代理图、抽样率和车队假设的组合，不是 NYC 实际车队的服务率。输入轨迹也只来自已派遣/完成行程，并按事后行程字段筛选，存在选择偏差。

因此不得把 5% 服务率、零空驶或 D0/D1 相同写成数据集性能结论，也不应覆盖原有 `traditional_experiment_v1`。本产物只记录预检失败和下一步数据需求。

## 下一步

若继续做 NYC 数据驱动的调度实验，先补上真实道路网络上的区域间路径时间和区域内接驳距离，再为 100 单抽样明确按同一抽样比例缩放的车队数量、空闲/忙碌状态和初始化规则；之后先检查 300 秒接驾约束下的可行候选密度及多车冲突，再决定是否扩大为多时段、多种子实验。TLC 行程数据本身不提供车队状态、未服务订单、道路图或路径，因此这几项必须另有数据来源或作为明确的模拟假设。

路线层不重跑：这批 TLC 数据没有道路边、边成本或 GPS 路径；本试跑构造的 OD 代理图也不是路线层地图。

## 可复现产物

- 输入轨迹审计：[NYC TLC 2024-01 审计](dispatch-layer-nyc-tlc-2024-01-audit.md)
- 运行脚本：[run_nyc_tlc_zone_proxy_dispatch_pilot.py](../scripts/run_nyc_tlc_zone_proxy_dispatch_pilot.py)
- 运行清单：[pilot_manifest.json](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/zone_proxy_pilot_2024-01-16_18-19/pilot_manifest.json)
- D0 逐轮结果：[D0_greedy_pilot.json](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/zone_proxy_pilot_2024-01-16_18-19/D0_greedy_pilot.json)
- D1 逐轮结果：[D1_optimizer_pilot.json](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/zone_proxy_pilot_2024-01-16_18-19/D1_optimizer_pilot.json)
- 固定抽样订单：[sampled_test_orders.json](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/zone_proxy_pilot_2024-01-16_18-19/sampled_test_orders.json)
- 训练区间 OD 成本代理：[zone_cost_matrix_train_2024-01-01_15.csv.gz](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/zone_proxy_pilot_2024-01-16_18-19/zone_cost_matrix_train_2024-01-01_15.csv.gz)

需要 DuckDB 1.5.6；运行命令见数据集目录 README。原始 TLC 文件以只读方式使用。
