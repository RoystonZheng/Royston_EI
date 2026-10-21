# 路线层实验实施计划

> **面向智能体执行者：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 子技能，按任务逐项执行。每一步使用复选框（`- [ ]`）跟踪。

**目标：** 实现并运行 `output/paper/路线层实验方案_20261009.md` 中描述的首版可复现路线层模拟器，先完成离线 Dijkstra/A* 实验，再接入纯 LLM 和车辆 Agent 方法。

**架构：** 使用仅依赖 Python 标准库的程序包生成带随机种子的 100×100 有向道路图，为每个车辆—订单任务生成一份共享的原始路线输入包；精确任务直接复用同一份解析后 `RouteInputPackage`，语义任务由各方法从相同文本和道路图生成候选端点后构造方法专用的解析后包。所有方法通过统一的路线结果结构输出，并使用同一个验证器和评估器计算可达性、端点合法性、交通延迟、电耗、时间和成本。LLM 方法通过适配器接入，支持 `disabled`、确定性的 `mock` 和真实 DeepSeek 模式，不改变模拟器和传统路线基线。

**技术栈：** Python 3.11+ 标准库（`dataclasses`、`heapq`、`json`、`argparse`、`csv`、`time`）；使用 pytest 测试；DeepSeek 适配器使用 `urllib` 实现可选 HTTP 调用。离线首轮实验不需要第三方依赖。

**依据：** `output/paper/路线层实验方案_20261009.md`、`output/paper/路线层实验字段_20261008.md`、`EXPERIMENT_DESIGN.md` 和 `AGENTS.md`。

**2026-10-14 同步状态：** 地图、独立订单、正式场景、R0/R1/R2 传统路线、固定 `graph_only` 语义解析和统一费用评估已完成第一轮 10 个种子正式运行。`scripts/run_traditional_experiment.py` 对种子 0–9 的 100×100 输入执行 70/30 混合订单和五类正式场景，共生成 450 条任务—方法轨迹记录；每条记录包含搜索扩展事件、最终路径、运行时间、统一电耗/费用结果和独立 HTML 回放。价格引用 Tesla 中国官方深圳站点峰时综合充电服务价 `2.29 CNY/kWh`，记录站点、时段、日期、来源和含服务费口径；Tesla 价格按站点/时段变化，不能替代全国统一价格。30% 语义任务仍进入传统路线结果，路牌/电线杆实体和评估器真值不进入该基线；Agent-readable 协议、纯 LLM/Agent 工作流和统一独立验证器仍按后续任务执行。

## 全局约束

- 主实验和正式产物只使用 100×100 离散道路图；不设置 50×50 规模对照。20×20 只可作为单元测试构造，不写入正式实验结果。
- 每个随机种子的地图、道路状态、车辆起点、任务文本和原始语义输入包在所有方法之间完全一致。精确任务共享解析后的路线输入包；语义任务的候选端点由方法自己的解析器生成，再交给相同的路线搜索、验证和评估。
- 路线规划与最终车辆—订单分配分开；一个任务对应一个车辆—订单组合。
- 精确节点任务和语义地点任务分开；首轮使用 70% 精确节点任务、30% 语义地点任务。
- 30% 语义任务是难度控制项，传统基线不能跳过。传统路线使用固定、可审计的 `graph_only` 解析器：只读取地标名称/别名、坐标、方向/邻近和合法上下车点，不读取路牌/电线杆语义，不读取评估器真值；解析不确定时使用记录过的合法候选或结构化失败。
- Agent-readable 组使用 `RoadFeatureInputProtocol v1` 读取道路特征实体、道路侧别、朝向和配对节点关系。基线候选、Agent 候选和评估器真值必须分开记录。
- 只使用路线层方案规定的最小节点字段和道路边字段；首版不加入封闭道路、车道模型、信号灯相位和复杂转向规则。
- 本地运行 Dijkstra 和 A*；保留全图 Dijkstra 结果作为参考，并与局部方法比较，但不把它混入主要方法排名。
- 所有方法输出相同结构的路线结果，并通过同一个验证器；非法、失败、超时和回退结果都保留在日志中。
- 交通、信号灯、电耗和货币成本由路线生成后的统一评估器计算；模拟值必须标注为仿真估计。
- 启用 LLM 方法时记录模型 ID、提示词/结构版本、token 数、延迟、重试次数和成本；实验产物中不得写入 API 密钥。
- 在形成运行产物和多随机种子汇总前，不把实验结果写入论文。

## 重点审查项

- **边界和单向道路：** 路线不能越过 100×100 地图边界，也不能逆向通过单向边；在路由和验证任务中测试。
- **局部输入包截断：** 必须在任何方法运行前生成共享局部输入包；局部图不连通时按规则扩展，并在达到上限后确定性地报告失败；在输入包任务中测试。
- **非法或有歧义的语义端点：** 方法应返回解析状态、候选节点和结构化失败或经过记录的回退结果，同时保留评估器真值用于评分；解析候选与真值不一致不自动判定路线无效。
- **LLM 输出格式错误和超时：** 解析或验证失败不得中断批量实验，也不能静默变成有效路线；在适配器和工作流任务中测试。
- **无路线和评估一致性：** 所有方法使用相同的失败分类和成本模型，包括零路线情况；在评估器和运行器任务中测试。

---

### 任务 1：建立路线实验程序包和共享数据契约

**文件：**
- 创建：`route_experiment/__init__.py`
- 创建：`route_experiment/models.py`
- 创建：`tests/test_models.py`

**接口：**
- 提供 `Node`、`Edge`、`RoadMap`、`VehicleOrderTask`、`RouteInputPackage`、`RouteCandidate`、`RouteMethodResult`、`ScenarioRecord` 和 `RunConfig` 数据类。
- 每个模型提供 JSON 安全的 `to_dict()` 输出，字段名与路线层方案保持一致。

- [ ] **步骤 1：编写失败的契约测试。** 测试必填字段、`null`/空数组约定、JSON 序列化和单位约定（米、秒、kWh）。
- [ ] **步骤 2：运行 `pytest tests/test_models.py -q`，确认因为程序包尚不存在而失败。**
- [ ] **步骤 3：实现数据类和显式的字符串枚举值。** 覆盖任务类型、路线状态、验证状态和失败原因；原始模型输出与验证后的路线输出分开保存。
- [ ] **步骤 4：运行契约测试，确认 JSON 往返后 ID、序列、`null` 和 `[]` 均保持不变。**

### 任务 2：生成带种子的地图、道路状态和任务场景

**文件：**
- 创建：`route_experiment/mapgen.py`
- 创建：`route_experiment/scenario.py`
- 创建：`tests/test_mapgen.py`
- 创建：`tests/test_scenario.py`

**接口：**
- `generate_map(seed: int, size: int, config: MapConfig) -> RoadMap`
- `generate_scenario(seed: int, map_size: int = 100, exact_ratio: float = 0.7) -> ScenarioRecord`
- `validate_scenario_coverage(scenario: ScenarioRecord) -> CoverageReport`

- [ ] **步骤 1：编写失败测试。** 覆盖确定性生成、显式边界、节点/道路边最小字段、合法上下车点标记、单向道路、0/1/2 级拥堵、信号灯延迟，以及可行/绕行/语义/边界场景覆盖标记。
- [ ] **步骤 2：运行针对性测试，确认失败。**
- [ ] **步骤 3：实现带种子的 100×100 图生成器，禁止环绕边。** 根据种子生成道路状态；若场景不满足最低覆盖条件，则按种子派生的方式重新生成，设置最大尝试次数，无法生成时明确报错。
- [ ] **步骤 4：实现精确节点任务和三类简单语义任务。** 在场景记录中保留评估器专用的真实节点 ID；公开任务只保留语义文本。为传统路线登记 `graph_only` 解析器，为 Agent-readable 组登记 `RoadFeatureInputProtocol v1` 可见性。
- [ ] **步骤 5：对种子 0、1、2 运行测试，确认相同种子重复生成的 JSON 完全一致。**

### 任务 3：构建共享的局部 `RouteInputPackage`

**文件：**
- 创建：`route_experiment/input_package.py`
- 创建：`tests/test_input_package.py`

**接口：**
- `build_route_input_package(road_map: RoadMap, task: VehicleOrderTask, config: PackageConfig) -> RouteInputPackage`
- `package_stats(package: RouteInputPackage) -> dict[str, int]`

- [ ] **步骤 1：编写失败测试。** 覆盖初始 10 个网格单元扩展、局部图不连通时的 5 个单元扩展、最多三次扩展、稳定的节点/边排序，以及序列化大小和 token 估计记录。
- [ ] **步骤 2：运行针对性测试，确认失败。**
- [ ] **步骤 3：在方法运行前实现输入包提取。** 对每种方法提供相同的起点、上车点、下车点、节点字段、道路边字段、地图/道路版本、随机种子、扩展次数、节点数、边数和序列化大小估计。
- [ ] **步骤 3a：在语义任务中先保存相同的原始文本/图输入，再运行方法特定的端点解析。** 解析器输出候选节点、解析状态、可见性、是否读取道路特征和是否读取评估器真值；路线输入包只能使用候选节点，不能直接使用评估器真值。
- [ ] **步骤 4：测试人为构造的断开局部图，确认输入包按规则确定性扩展，并在达到上限后报告失败。**

### 任务 4：实现 Dijkstra、A* 和统一路线验证器

**文件：**
- 创建：`route_experiment/routing.py`
- 创建：`route_experiment/validation.py`
- 创建：`tests/test_routing.py`
- 创建：`tests/test_validation.py`

**接口：**
- `run_dijkstra(package: RouteInputPackage, target: str = "dropoff") -> RouteMethodResult`
- `run_a_star(package: RouteInputPackage, target: str = "dropoff") -> RouteMethodResult`
- `validate_route(package: RouteInputPackage, route: RouteCandidate) -> ValidationResult`

- [ ] **步骤 1：编写失败测试。** 覆盖最短路正确性、同一输入包下 A* 与 Dijkstra 的成本一致性、单向道路约束、无路线分类、边界拒绝，以及上下车端点检查。
- [ ] **步骤 2：运行针对性测试，确认失败。**
- [ ] **步骤 3：实现图搜索。** 使用包含拥堵速度倍率和固定信号灯延迟的统一行驶时间作为边权，并返回搜索节点数和搜索耗时。
- [ ] **步骤 4：实现统一验证器。** 检查节点/道路边连续性、道路方向、端点身份、合法上下车点、地图边界和路线状态。
- [ ] **步骤 5：用测试构造的小地图运行验证，确认存在路线时 Dijkstra 与 A* 的验证后最优路线成本一致；不把小地图写入正式实验结果。**

### 任务 5：实现统一交通、电耗和成本评估器

**文件：**
- 创建：`route_experiment/evaluation.py`
- 创建：`tests/test_evaluation.py`

**接口：**
- `evaluate_route(road_map: RoadMap, route: RouteCandidate, config: EvaluationConfig) -> EvaluationResult`
- `price_costs(resources: ResourceUsage, prices: PriceConfig) -> MonetaryCost`

- [ ] **步骤 1：编写失败测试。** 覆盖基础行驶时间、拥堵延迟、信号灯延迟、行驶/等待/拥堵电耗、电价换算，以及 API/算力/车辆电费的分开汇总。
- [ ] **步骤 2：运行针对性测试，确认失败。**
- [ ] **步骤 3：实现所有方法共用的评估器。** 返回仿真估计，并记录电耗模型版本和价格配置版本。
- [ ] **步骤 4：使用手工构造的路线核对精确的秒数、kWh 和货币金额。**
- [x] **步骤 4a：使用命名价格配置运行种子 0–1 烟雾测试。** 检查地图/订单哈希、五类正式场景、R0/R1/R2 结果、非零金额和价格来源/状态均写入产物。

### 任务 6：加入纯 LLM 和车辆 Agent 适配器及离线 mock 模式

**文件：**
- 创建：`route_experiment/llm_adapter.py`
- 创建：`route_experiment/workflows.py`
- 创建：`tests/test_llm_adapter.py`
- 创建：`tests/test_workflows.py`

**接口：**
- `RoutePlanner.plan(package: RouteInputPackage) -> RawPlannerResponse`
- `DeepSeekPlanner(...).plan(package) -> RawPlannerResponse`
- `MockPlanner(...).plan(package) -> RawPlannerResponse`
- `run_pure_llm_workflow(package, planner, validator, evaluator) -> RouteMethodResult`
- `run_agent_workflow(package, planner, validator, evaluator, max_replans=...) -> RouteMethodResult`

- [ ] **步骤 1：编写失败测试。** 覆盖有效 JSON、格式错误 JSON、缺少路线字段、超时/错误捕获、重试次数、确定性 mock 输出，以及 Agent 在不绕过验证器的前提下进行反馈和重规划。
- [ ] **步骤 2：运行针对性测试，确认失败。**
- [ ] **步骤 3：先实现适配器协议和确定性 mock planner。** Mock 必须读取同一份输入包，并通过正常验证器生成路线，使离线运行真正覆盖路线工作流接口。
- [ ] **步骤 4：实现可选的 DeepSeek HTTP 适配器。** 支持可配置 endpoint、model ID、超时、重试策略、提示词/结构版本、脱敏请求日志、token/延迟统计，并禁止保存密钥。
- [ ] **步骤 5：实现纯 LLM 直接规划和 Agent 的“状态读取 → 路线提议 → 程序验证 → 反馈 → 有限重规划”流程。** 最终响应失败时保持失败，或使用明确记录的回退结果。

### 任务 7：运行带种子的路线层实验并写入产物

**文件：**
- 创建：`route_experiment/runner.py`
- 创建：`route_experiment/reporting.py`
- 创建：`scripts/run_route_experiment.py`
- 创建：`tests/test_runner.py`

**接口：**
- `run_seed(seed: int, config: RunConfig) -> RunArtifacts`
- `run_experiment(config: RunConfig) -> ExperimentArtifacts`
- CLI 参数包括地图大小、种子、LLM 模式（`disabled|mock|deepseek`）、输出目录和方法选择。

- [ ] **步骤 1：编写失败测试。** 覆盖相同输入的多方法分发、方法隔离、失败结果保留、稳定产物路径，以及按方法/任务类型/种子聚合汇总。
- [ ] **步骤 2：运行针对性测试，确认失败。**
- [ ] **步骤 3：实现运行器。** 每个种子生成一个场景和每个任务的一份原始输入包；精确任务把同一解析后包发送给选定方法，语义任务分别运行 `graph_only` 或 Agent-readable 解析器后生成候选包，再把候选包发送给同一套路线搜索、验证和评估；在汇总前先保存场景、解析记录、输入包和结果 JSONL。
- [ ] **步骤 4：实现报告汇总。** 覆盖有效性/可达性/端点指标、距离/时间/电耗、P50/P95/最大耗时、API/算力/车辆电费、重试/重规划和失败分类。
- [x] **步骤 5：用 `scripts/run_seed_smoke.py` 在 100×100 的种子 0–1 上运行离线冒烟实验，检查地图/订单哈希、五类正式场景、传统路线和命名价格配置；产物标记为 smoke，不作为正式规模结果。**
- [x] **步骤 6：运行计划中的 100×100 离线首轮传统算法实验。** 使用种子 0–9 和精确/语义任务混合，保留解析器版本、输入可见性、价格配置、方法配置、逐任务搜索轨迹 JSON、HTML 回放和汇总清单；产物见 `outputs/route_layer/traditional_experiment_v1/manifest.json`。

### 任务 8：验证路线层运行并记录证据

**文件：**
- 创建：`docs/route-layer-run-2026-10-12.md`
- 创建：`outputs/route_layer/<run_id>/manifest.json` 及生成的实验产物（不得手工修改）
- 修改：只有在已有验证运行证据后，才修改 `output/paper/研究记忆库.md`

- [ ] **步骤 1：使用 `pytest -q` 运行完整测试套件。**
- [ ] **步骤 2：重新运行一个种子，并对场景和输入包产物进行字节级比较，确认输入确定性。**
- [ ] **步骤 3：从原始日志中检查至少一个有效、一个边界/绕行、一个语义和一个无路线案例；精确任务确认所有方法使用相同的输入包哈希，语义任务确认原始输入包哈希相同且候选解析记录完整。**
- [ ] **步骤 4：撰写简短证据报告。** 明确区分已实现代码、离线仿真结果、LLM/API 结果（如果启用）和未解决限制。
- [ ] **步骤 5：只把稳定且带日期的证据及其输出路径更新到研究记忆库；不得把预期优势写成结论。**

## 执行说明

当前工作区不是 Git 仓库，因此在用户提供或初始化仓库前不能创建实现提交。计划已经准备完成。计划获批后，默认采用当前会话直接执行；由于任务接口之间耦合较紧，而且需要在同一环境中验证离线首轮实验，直接执行最合适。
