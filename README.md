# 一对一无人网约车 Agent 协同研究

本项目研究一对一、动态到达订单下的无人网约车派单与接驾路线评估。每辆车同一时刻服务一名乘客；研究以可复现模拟为主，不包含真实道路部署结论。

## 入口文件

- [项目协作规则](AGENTS.md)
- [研究记忆库](output/paper/研究记忆库.md)：已确认边界、方案状态、证据限制及最新进度
- [Agent 工作流设计稿](output/paper/第三次课_Agent工作流完善稿.md)
- [两层实验总览](EXPERIMENT_DESIGN.md)

## 当前进度（2026-10-21）

### 已完成

- **路线层传统基线：**在 10 个 100×100 地图种子上，比较全图 Dijkstra、局部 Dijkstra 与局部 A*；每种子包括 10 个订单任务和 5 个正式场景，共归档 450 条任务—方法轨迹。结果是离线模拟基线。
- **纯 LLM 路线实验：**v8 完成 10 种子、150 个任务的真实模型批次，合法路线为 0/150；原始响应、校验和回放均已留档，不能作为有效路线性能对照。详见[实验记录](docs/route-layer-pure-llm-run-2026-10-18-v8.md)。
- **R4 路线 smoke：**DeepSeek 在一个 8×8 任务上完成过一次经本地有向边校验的路线。该结果只覆盖单任务。100×100 预检发现 130 个可达任务中有 128 个最短路径超过当前 32 次请求上限；正式批次待交互规则、预算和熔断门禁冻结。详见[R4 预算预检](docs/route-layer-r4-100x100-preflight-2026-10-21.md)。
- **调度层 D0/D1/D5 对照：**在五个冻结的 NYC TLC 派生场景/车队配置上完成 seeds 0–9，共 50 组三方法配对、150 个结果；每组的场景指纹、地图、订单数和车辆数匹配。D5 Codex CLI 共 425 次调用、8,900,518 个输入与输出 token；公开费率等值为 26.002093 credits / USD 1.04008372，不是账户账单。各配置的完成订单数与 D0/D1 相同，等待、空驶和模拟能耗的差异方向不一致；7 条候选被本地规则拒绝且没有重试或回退。该批是同五个场景的重复模拟，不是独立 NYC 需求抽样。详见[对照报告](outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_formal_v1/comparison_report.md)和[Codex CLI 记录口径](docs/dispatch-layer-codex-cli-experiment-policy-2026-10-21.md)。
- **数据准备：**本地保存 NYC TLC 2024 年 High Volume FHV 月度行程数据及区域表；另有 NYC DOT / Manhattan 路网与传感器匹配的预检材料。数据和代理成本的适用边界见[数据目录说明](data/dispatch/nyc_tlc_hvfhv_2024/README.md)及[数据集筛查](docs/dispatch-layer-dataset-survey-2026-10-19.md)。

### 尚待完成

- 冻结 R4、R4-A、R4-B 的统一请求语义、输出长度与预算门禁，再决定是否启动 100×100 正式批次及消融。
- 完成路线层 Agent-readable map 对照及正式 R4 批次；调度层 D2–D4 Agent/优化器与 D0/D1/D5 的同输入对照仍待完成。
- 基于正式对照结果确定论文中的效果结论；当前 smoke、预检和单次回放不支持普遍性能或真实运营主张。

## 实验方案

### 路线层

- [路线层实验方案](output/paper/路线层实验方案_20261009.md)
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
