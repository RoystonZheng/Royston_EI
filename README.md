# 一对一无人网约车 Agent 协同研究

本项目研究一对一、动态到达订单下的无人网约车派单与接驾路线评估。每辆车同一时刻服务一名乘客；研究以可复现模拟为主，不包含真实道路部署结论。

## 入口文件

- [项目协作规则](AGENTS.md)
- [研究记忆库](output/paper/研究记忆库.md)：已确认边界、方案状态、证据限制及最新进度
- [Windows 迁移入口](START_WINDOWS.bat)：下载、校验并恢复完整实验文件；此入口不会启动实验
- [Agent 工作流设计稿](output/paper/第三次课_Agent工作流完善稿.md)
- [两层实验总览](EXPERIMENT_DESIGN.md)

## 当前进度（2026-10-23）

### 已完成

- **路线层传统基线：**在 10 个 100×100 地图种子上，比较全图 Dijkstra、局部 Dijkstra 与局部 A*；每种子包括 10 个订单任务和 5 个正式场景，共归档 450 条任务—方法轨迹。结果是离线模拟基线。
- **纯 LLM 路线实验：**v8 完成 10 种子、150 个任务的真实模型批次，合法路线为 0/150；原始响应、校验和回放均已留档，不能作为有效路线性能对照。详见[实验记录](docs/route-layer-pure-llm-run-2026-10-18-v8.md)。
- **R4 路线 smoke：**DeepSeek 在一个 8×8 任务上完成过一次经本地有向边校验的路线。该结果只覆盖单任务。100×100 预检发现 130 个可达任务中有 128 个最短路径超过当前 32 次请求上限；正式批次待交互规则、预算和熔断门禁冻结。详见[R4 预算预检](docs/route-layer-r4-100x100-preflight-2026-10-21.md)。
- **调度层基线：**D0 贪心与 D1 集中优化已完成原有 10 种子离线矩阵；两者在该组中作出相同选择。另在 NYC TLC 共享输入的 C2、C3、C4 五个场景/车队组合中完成 10 次 D0/D1 回放，逐轮校验通过。每格只有一个固定输入回放，不据此作统计显著性或普遍优势结论。详见[完成报告](docs/dispatch-layer-traditional-baselines-complete-2026-10-20.md)。
- **纯 LLM 调度 smoke：**D5 在 C2 上发出一次请求并选择 3 个匹配；还有 1 单等待，结果为 `partial_run`，不是完整调度性能实验。详见[smoke 记录](docs/dispatch-layer-pure-llm-smoke-2026-10-20.md)。
- **数据准备：**本地保存 NYC TLC 2024 年 High Volume FHV 月度行程数据及区域表；另有 NYC DOT / Manhattan 路网与传感器匹配的预检材料。数据和代理成本的适用边界见[数据目录说明](data/dispatch/nyc_tlc_hvfhv_2024/README.md)及[数据集筛查](docs/dispatch-layer-dataset-survey-2026-10-19.md)。
- **D6/D7 调度 pilot：**2026-10-23 完成 C4/F1/seed 0 的配对回放。容量保护在一轮把最大匹配数从 4 恢复到 5；两种方法各完成 13/790 单，属于部分运行和单 seed pilot，不支持总体性能结论。详见[容量保护 pilot 报告](outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/hybrid_agent_codex_capacity_shield_v1/C4_manhattan_790_order_five_minute_load_sweep/F1/capacity_shield_pilot_report.md)。
- **Porto 固定车队路线执行分支：**已归档 UCI/Figshare 轨迹、候选 OSM 快照、热度/OD 聚合和地图导入样例；2026-10-23 完成固定订单生成与执行模块设计。该分支预先固定车辆及订单顺序，不做实时派单。最终围栏、SUMO 网络、端点 Link 绑定和路线可达预检仍未完成，现有 seed 0–9 文件是旧规则草案，不能作为正式输入。

### 尚待完成

- 冻结 R4、R4-A、R4-B 的统一请求语义、输出长度与预算门禁，再决定是否启动 100×100 正式批次及消融。
- 完成路线层 Agent-readable map 对照，以及调度层 Agent/优化器与纯 LLM 的完整场景对比和重复实验。
- 完成 Porto 候选围栏和 SUMO 路网，按 2026-10-23 模块设计更新固定订单生成器，重生成订单并通过点到 Link 绑定及全部路线腿预检。
- 基于正式对照结果确定论文中的效果结论；当前 smoke、预检和单次回放不支持普遍性能或真实运营主张。

## 实验方案

### 路线层

- [路线层实验方案](output/paper/路线层实验方案_20261009.md)
- [围栏热点站点—公开真实地图与仿真交通实验设计](output/paper/围栏热点站点_公开真实地图与仿真交通实验设计_20261021.md)
- [真实路网派单综合实验设计（新增分支，不覆盖既有结果）](output/paper/真实路网派单综合实验设计_20261022.md)
- [Porto 真实路网派单数据包](data/real_map_dispatch/porto_hybrid_v1/README.md)：UCI 轨迹热度/OD 聚合与候选 OSM 快照已归档；最终围栏、Link 绑定和 SUMO 交通待完成
- [Porto 固定车队路线执行实验设计](output/paper/真实路网固定车队路线执行实验设计_20261022.md)
- [固定订单序列生成与执行模块设计](output/paper/固定订单序列生成与执行模块设计_20261023.md)：当前模块规范；现有旧规则订单草案需重生成并完成预检后才能冻结
- [历史数据版方案（已排除，不作为当前实验设计）](output/paper/围栏热点站点_link路线实验数据版_20261021.md)
- [路线层字段定义](output/paper/路线层实验字段_20261008.md)
- [传统路线实验记录](docs/route-layer-run-2026-10-14.md)
- [纯 LLM v8 实验记录](docs/route-layer-pure-llm-run-2026-10-18-v8.md)
- [R4 工作流设计](docs/route-layer-r4-agent-workflow-design-2026-10-16.md)

### 调度层

- [调度层实验方案](output/paper/调度层实验方案_20261009.md)
- [调度层字段定义](output/paper/调度层实验字段_20261009.md)
- [Agent 候选与调度字段草案](output/paper/实验方案_Agent候选与调度字段草案_20261008.md)
- [调度 Agent 完整实验设计](output/paper/调度Agent_完整可实验设计_20261016.md)
- [NYC TLC 共享三方法数据集说明](docs/dispatch-layer-three-method-shared-dataset-2026-10-20.md)

### 实现与产物

- `route_experiment/`：路线层模拟与评估
- `dispatch_experiment/`：D0–D4 调度层离线实现；默认不调用 API
- `scripts/run_traditional_experiment.py`：路线层传统基线入口
- `scripts/run_dispatch_matrix.py`：调度层矩阵入口
- `outputs/`：模拟输入、结果、校验记录与可视化回放
- `data/`：原始数据、清洗数据及来源清单

## 证据边界

实验方案是设计材料；预检只证明输入、协议或预算检查完成；smoke 只证明有限任务上的接口链路；正式模拟结果仅适用于对应固定场景。TLC 记录的是历史已完成行程，车辆状态和部分接驾成本使用模拟代理，不代表真实纽约运营。不得把这些结果写成真实部署效果或普遍算法优势。
