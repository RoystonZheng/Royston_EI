> 更新（2026-10-20）：本报告记录的是首轮 C3 十种子全量回放及当时 C4 中断状态。C4 后续已在紧凑记录模式下完成，完整 D0/D1 固定场景矩阵和当前结论见 [完成报告](dispatch-layer-traditional-baselines-complete-2026-10-20.md)。本报告保留作为历史记录；其中全量记录模式的耗时不可与新报告的紧凑模式耗时直接比较。

# 传统派单基线实验记录（2026-10-20）

## 实验范围

在 NYC TLC 共享输入包上比较 D0（贪心）与 D1（集中优化）。C3 固定回放 44 个请求、18 辆合成车辆；每种方法运行 seed 0–9，共 20 次完整运行。C2 是小型接口烟雾场景，不作为性能样本。原计划的 C4（790 个请求，12/24/48 辆车）尚未完成。

所有运行复用同一订单轨迹、车队布局、训练期 OD 成本矩阵、硬约束和输出校验器。C3 的十个 seed 对应同一份固定订单和车辆状态；本轮没有生成独立需求样本。

## C3 结果

每种方法均完成 42 轮决策；每轮输出通过校验，没有硬约束错误、回退或承诺违规。D0 与 D1 每个 seed 的首轮 DispatchInput 和数据包哈希一致。

| 指标 | D0 贪心 | D1 集中优化 |
|---|---:|---:|
| 服务订单 | 16/44 | 16/44 |
| 平均接驾等待 | 74.272 秒 | 74.272 秒 |
| P95 接驾等待 | 196.924 秒 | 196.924 秒 |
| 空驶距离 | 5941.776 米 | 5941.776 米 |
| 估算电耗 | 30.647 kWh | 26.252 kWh |
| 仿真决策耗时 | 169.89 ± 4.86 ms | 217.38 ± 9.92 ms |

十次运行的服务、等待、距离和电耗结果在各自方法内完全相同；变化只出现在本机决策耗时。因此这些 seed 是固定场景重复执行，不能用于统计显著性检验。D1 与 D0 各服务 16 单，但服务的订单集合有 1 单不同；D1 的估算电耗低 4.395 kWh，属于这一固定合成场景下的差异，不能据此得出普遍优势。

## C4 执行状态与边界

C4 的 F1、seed 0 运行近 3 分钟后，在候选快照深拷贝阶段被中断，未写出结果文件；F1/F2/F4 的其余种子尚未运行。恢复 C4 前需要先定位这段大输入复制的耗时，并重新评估运行规模。

TLC 数据是历史已派单行程，车队位置为合成状态，空车接驾时间为区域中心点距离代理。本文结果用于检查调度方法在固定输入下的机制行为，不代表纽约市实际服务率、等待时间或道路 ETA。

## 产物

- 原始逐轮记录：outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/traditional_baselines_v1/C3_manhattan_multizone_44_order_burst/single_fleet/
- 逐 seed 指标：outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/traditional_baselines_v1/per_seed_metrics.csv
- 汇总指标：outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/traditional_baselines_v1/method_summary.csv
- 输入一致性与校验：outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/traditional_baselines_v1/analysis_summary.json
- 运行计划和中断状态：outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/traditional_baselines_v1/experiment_plan.json、experiment_status.json
