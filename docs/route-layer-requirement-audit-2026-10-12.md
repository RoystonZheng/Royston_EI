# 路线层实验要求复核

日期：2026-10-14（三次核查，增补 10 个种子传统算法正式运行、搜索轨迹持久化和 HTML 回放）  
复核范围：当前已完成的“100×100 随机地图、独立订单任务、图规则语义解析、传统路线基线、统一费用评估、语义道路特征场景、逐任务检索轨迹与可视化”切片。  
依据：`AGENTS.md`、`output/paper/路线层实验方案_20261009.md`、`output/paper/研究记忆库.md`、`docs/superpowers/plans/2026-10-12-route-layer-experiment.md`。

## 结论

- **传统算法第一轮已完成，完整路线层实验仍不完全符合。** 当前实现已覆盖地图生成、独立订单任务生成、正式场景集生成、R0/R1/R2、统一费用评估、逐任务搜索轨迹和 HTML 回放；纯 LLM、Agent 工作流、Agent-readable 对照和独立完整验证器仍未完成。
- **地图与道路特征展示阶段：已满足当前要求。** 100×100 地图可以按种子稳定生成；同一张图中已加入路牌、电线杆及其侧别、朝向、配对节点等语义道路特征，并能在浏览器中可视化展示三类受控场景。
- **完整路线层实验：尚未全部满足。** R0/R1/R2、图规则语义解析、交通/电耗评估和 10 个种子传统算法正式运行已经完成；统一验证器的独立模块、LLM/Agent 工作流和 Agent-readable 对照仍未完成，本轮没有把这些未完成部分写成实验结果。
- **语义任务口径已按用户纠正更新。** 30% 语义地点任务是难度控制项，不是传统算法的失败项；传统路线使用不读取道路特征实体和评估器真值的 `graph_only` 解析器，先生成合法候选再运行路线。Agent-readable 组未来使用 `RoadFeatureInputProtocol v1`，两者的候选和真值分别记录。

### 订单任务生成专项核对

订单任务生成器与方案中的任务生成要求逐项核对如下：

- **任务边界符合**：每条记录是一个车辆—订单组合；生成器只读取既有 `RoadMap`，不承担最终订单分配，也不改写地图。
- **任务比例符合首版要求**：默认批次为 10 条任务，其中 7 条精确节点任务、3 条语义地点任务；总任务数和比例均可由配置调整。方案没有规定每张地图必须固定多少条任务，因此当前默认值不构成冲突，正式运行前仍需在 run config 中固定。
- **语义类型符合**：3 条语义任务分别覆盖“地标附近”“地标相对位置”“道路特征位置”。
- **真值隔离符合**：语义任务的公开 `tasks` 只保留自然语言描述，不包含 `true_pickup_node_id`/`true_dropoff_node_id`；真值单独写入评估用 `ground_truth`。
- **地图绑定和可复现符合**：任务引用同一地图的 `map_id`/`map_version`，使用合法上下车点、地标和道路特征；相同地图种子与任务种子可复现且不修改地图。
- **正式场景集已具备生成器**：`generate_formal_scenario_set` 已生成可行、边界、非法端点、绕行和不可达五类独立案例；局部输入范围外案例仍需在 `RouteInputPackage` 构建器中补充。

因此，本模块的判定是“**订单任务生成子模块符合，完整路线层实验尚不完全符合**”。订单 JSON 是后续 `RouteInputPackage` 的输入数据，不是路线规划结果。

本次判定依据是方案中的“方法必须接收统一 `RouteInputPackage` 并输出经过统一验证和评估的路线结果”这一闭环要求。当前代码目录已有地图、订单、正式场景、R0/R1/R2 传统路线、图规则语义解析和费用评估模块；独立完整验证器、R3/R4 模型工作流、Agent-readable 对照和多种子 runner 仍待实现。

## 已满足的地图生成要求

| 要求 | 状态 | 证据与说明 |
|---|---|---|
| 主地图规模为 100×100 | 已满足 | `map_100x100_seed_0.json`；节点数 10,000，范围为 `0..99 × 0..99`。 |
| 正式地图规模为 100×100 | 已满足 | 当前方案已删除 50×50/20×20 规模对照；正式地图固定为 100×100。 |
| 使用随机种子且可复现 | 已满足 | 种子 0 重复生成结果一致；测试 `test_same_seed_generates_identical_100_by_100_map` 通过。 |
| 有显式边界、禁止环绕道路 | 已满足 | 边界摘要为 `min_x=0,max_x=99,min_y=0,max_y=99`；测试检查道路端点不越界且无环绕连接。 |
| 节点保留最小字段 | 已满足 | 节点包含 ID、坐标、地标及别名、合法上车点、合法下车点。 |
| 道路边保留最小字段 | 已满足 | 道路边包含方向、长度、基础速度、单向属性、拥堵等级和固定信号灯等待。 |
| 单向道路可表达 | 已满足 | 种子 0 生成 2,998 条单向边；反向通行不通过隐式规则补齐。 |
| 拥堵分级可表达 | 已满足 | 0/1/2 级道路边数量分别为 22,070/10,757/3,775。 |
| 固定信号灯延迟可表达 | 已满足 | 2,894 条道路边带非零信号灯延迟；不模拟信号相位。 |
| 地标和合法上下车点可表达 | 已满足 | 12 个地标、2,500 个合法上车点、2,480 个合法下车点。 |
| 语义道路特征可表达 | 已满足 | 生成 20 个道路特征实体：10 个 `road_sign`、10 个 `utility_pole`；每个实体保留名称/别名、所在节点、道路侧别、朝向和配对节点关系。 |
| 道路特征输入范式可表达 | 已满足接口切片 | 固定采用 `RoadFeatureInputProtocol v1` 的“实体—特征—关系—约束”结构；图搜索基线可忽略语义特征，Agent 输入可读取其语义关系。 |
| 道路特征对照场景 | 已满足展示切片 | `map_100x100_seed_0_scenarios.json` 包含 3 个场景：路牌近侧定位、双向道路另一侧修正、道路特征语义锚点；记录 `graph_only` 与 `road_feature_input_protocol_v1` 两种可见性。 |
| 道路特征保持首版简化范围 | 已满足 | 当前未加入封闭道路、道路类型、车道数、复杂转向和实时信号相位。 |
| 地图可视化展示 | 已满足 | 已在浏览器复核：道路按拥堵等级着色，单向道路显示箭头，地标显示为紫色标记，路牌/电线杆显示为不同符号，场景起点与两种候选点显示为对照标记，并有摘要和图例。 |

## 尚未满足或只完成接口的路线层要求

| 要求 | 状态 | 当前情况 |
|---|---|---|
| 场景最低覆盖 | 已完成 | `generate_formal_scenario_set` 为种子 0–9 各生成可行、边界、非法端点、绕行和不可达五类独立 `ScenarioRecord`；不可达场景通过记录场景专属封闭边集合实现，不修改基础地图。 |
| 70% 精确节点 + 30% 语义地点任务 | 已完成首轮传统运行 | 每个种子均生成 10 条任务：7 条精确节点、3 条语义任务，三类语义任务各 1 条；R0/R1/R2 均完成 10/10 混合任务。公开 `tasks` 不包含正确节点 ID，真值单独放在评估用 `ground_truth` 中。 |
| 固定的 `RoadFeatureInputProtocol v1` | 部分满足 | 数据模型、道路特征序列化和 3 个展示场景已完成；从正式 `RouteInputPackage` 到协议的确定性转换、模型调用和实际 Agent 输入链路尚未实现。 |
| 共享局部 `RouteInputPackage` | 已完成首版提取器 | `build_route_input_package` 已实现初始 10 格、断开后每次 5 格、最多 3 次扩展，并保留场景封闭边字段；尚未接入完整路线运行器的统一输入包持久化。 |
| 全图 Dijkstra 参考路线 | 已完成首轮运行 | `full_map_dijkstra` 在种子 0–9 的 100×100 混合订单中均完成 10/10；每条记录保留扩展节点顺序、代价、最终路径和 HTML 回放。 |
| 局部 Dijkstra 与 A* | 已完成首轮运行 | `local_dijkstra` 和 `local_a_star` 在同一局部包上运行；种子 0–9 的混合订单均完成 10/10，并保留逐段检索轨迹和最终路径。 |
| 统一路线验证器 | 部分满足 | 传统基线已检查端点存在、上下车点合法、可达性和方向约束；完整路线连续性、边界和评估器统一验证仍待独立模块。 |
| 统一交通、电耗和金钱成本评估 | 已完成首版 | `route_experiment/costs.py` 按统一道路分段模型重算基础时间、拥堵延迟、信号等待和三类电耗，并将 token、算力毫秒、电耗分别换算为 API、算力和电费；价格支持命名配置文件、来源、站点、时段和状态字段。种子 0 传统路线已有成本产物；多种子烟雾测试引用 Tesla 中国官方深圳站点峰时综合充电服务价 `2.29 CNY/kWh`，该值不是全国统一电价。 |
| 纯 LLM 与车辆 Agent 工作流 | 待后续实现 | 尚未接入 DeepSeek、离线 mock、解析失败、反馈重规划和回退记录。 |
| Agent-readable 与原始图结构对照 | 部分满足 | 已在同一地图中建立 `graph_only` 与 `road_feature_input_protocol_v1` 的受控场景记录和可视化；传统基线仍对语义任务完成图规则解析和路线生成。尚未在统一路线任务上运行两种输入并比较定位、路线和成本指标。 |
| Agent + 传统算法 / Agent + LLM 消融 | 待后续实现 | 尚未实现可替换路线工具和消融运行。 |
| 10 个种子正式路线层实验 | 已完成传统首轮 | `scripts/run_traditional_experiment.py` 已运行种子 0–9；每个种子 30 条订单方法轨迹 + 15 条正式场景方法轨迹，共 450 条，清单和总索引位于 `outputs/route_layer/traditional_experiment_v1/`；每个种子另有原地图—算法结果联动总览页。 |

## 方案闭环核查

| 方案闭环 | 当前判定 | 核查依据 |
|---|---|---|
| 地图与道路状态生成 | 已完成首个切片 | 100×100、边界、单向、拥堵、固定信号灯、地标、合法上下车点和道路特征均已进入种子 0 产物。 |
| 中立规则生成统一局部输入包 | 已完成首版 | `build_route_input_package` 已按固定扩展规则提取局部节点、边、道路特征和 token 估计。 |
| R0 全图 Dijkstra 参考路线 | 已完成首版 | 结果见 `outputs/route_layer/traditional_routes/routes_map_seed_0_task_seed_0.json`。 |
| R1/R2 局部 Dijkstra/A* | 已完成首版 | 与 R0 使用同一订单输入；结果和搜索展开节点已写入传统基线产物。 |
| R3 纯 LLM 路线规划 | 未完成 | 当前没有 API 调用、解析器、失败/重试日志或模型版本记录。 |
| R4 Agent 工作流与反馈重规划 | 未完成 | 当前只有数据模型和展示场景，没有状态读取、路线候选、程序检查和反馈循环。 |
| 统一路线验证器与交通/电耗/金钱评估 | 部分满足 | 传统基线已写入基础路线时间/距离、端点/可达性、`semantic_resolution` 和图规则语义解析状态；统一交通、电耗和金额评估器已完成首版并接入种子 0 传统结果，但完整独立验证器、Agent 语义解析和多种子汇总仍待实现。 |
| 订单任务生成与地图绑定 | 已完成首版切片 | 任务生成器独立于地图生成器，通过读取 `RoadMap` 或地图 JSON 使用节点、合法上下车点、地标和道路特征；输出任务 JSON，不把订单写回地图。 |
| 10 个种子、70/30 任务、正式场景集和传统路线结果 | 已完成传统首轮 | 种子 0–9 均生成地图、订单、五类正式场景，并运行 R0/R1/R2；统一汇总、价格配置和逐任务轨迹均写入第一轮实验清单。 |

## 可复现证据

- 地图 JSON：`outputs/route_layer/map_generation/map_100x100_seed_0.json`
- 地图摘要：`outputs/route_layer/map_generation/map_100x100_seed_0_summary.json`
- 可视化片段：`outputs/route_layer/map_generation/map_100x100_seed_0.html`
- 浏览器预览：`outputs/route_layer/map_generation/map_100x100_seed_0_preview.html`
- 道路特征场景：`outputs/route_layer/map_generation/map_100x100_seed_0_scenarios.json`
- 订单任务：`outputs/route_layer/order_tasks/order_tasks_map_seed_0_task_seed_0.json`
- 正式场景生成入口：`scripts/generate_formal_scenarios.py`；第一轮批量运行由 `scripts/run_traditional_experiment.py --seed-count 10` 完成。
- 传统路线入口：`scripts/run_traditional_routes.py`；传统订单结果：`outputs/route_layer/traditional_routes/routes_map_seed_0_task_seed_0.json`（含顶层 `semantic_policy` 与每条语义结果的 `semantic_resolution`）；正式场景结果：`outputs/route_layer/traditional_routes/formal_routes_seed_0.json`。
- 费用评估入口：`scripts/calculate_route_costs.py`；种子 0 订单费用结果：`outputs/route_layer/traditional_routes/routes_map_seed_0_task_seed_0_costs.json`；正式场景费用结果：`outputs/route_layer/traditional_routes/formal_routes_seed_0_costs.json`。当前产物使用 `energy_v1` 和 `tesla_cn_station_peak_v1`，电耗价格为 Tesla 中国深圳站点峰时综合充电服务价 `2.29 CNY/kWh`，并在产物中保留官方来源、站点、时段和采集日期。
- 多种子烟雾入口：`scripts/run_seed_smoke.py`；默认运行种子 0–1 的 100×100 地图、70/30 订单、五类正式场景、R0/R1/R2 和费用链路；产物见 `outputs/route_layer/traditional_seed_smoke/manifest.json`。参考价格配置：`configs/route_layer/price_smoke_v1.json`，引用 [Tesla 中国官方深圳站点页面](https://www.tesla.cn/findus/location/supercharger/cnsc9691?region=CN)，采用 10:00–12:00、14:00–19:00 的 `2.29 CNY/kWh` 峰时综合充电服务价。
- 当前测试：`python3 -m unittest discover -s tests -v`，31/31 通过；另有 `python3 -m py_compile route_experiment/*.py scripts/*.py`、种子 0–1 多种子烟雾运行和种子 0–9 第一轮传统算法运行通过。

## 当前边界

本次证据支持“100×100 随机道路图生成、独立订单任务生成、五类正式场景生成、R0/R1/R2 传统路线第一轮 10 种子运行、图规则语义解析、统一交通/电耗/金额评估、语义道路特征记录、逐任务搜索轨迹和 HTML 可视化回放已经跑通”。种子 0–9 中三种传统方法均完成每个种子的 10/10 条混合任务；每个种子五类正式场景均按可行/边界/绕行成功、非法端点/不可达失败记录。传统基线不读取路牌/电线杆语义，因此仍可作为后续 Agent-readable 对照的传统基线。该结果不支持 Agent 可读性提升、LLM 效果、能耗优势或真实道路效果结论；纯 LLM、Agent 工作流、Agent-readable 对照和统一独立验证器仍待后续实验。
