# R4 Codex 分步输入本地预检（2026-10-22）

## 目的与边界

本次继续处理 R4 的路线检索实现，目标是先离线确认 Codex CLI 接口能否收到一致的分步路线契约，以及道路特征语义是否进入路线工作流。没有调用 Codex 或 DeepSeek 路线模型；结果不属于正式 R4 样本。

先前的 r4_codex_formal_checkpoint_v1 是冻结 100×100 seed 0、单个语义订单的**整条路线一次生成检查点**。Qwen 已从 RoadFeatureInputProtocol v1 解析地点端点，路线输入含局部子图和压缩方向网格；Codex 正常返回 unable_to_construct_route，未给出移动序列。该检查点不能证明分步路线生成成功或失败，也不能代表十种子正式实验。

## 本次实现

1. Codex CLI 禁用了工具并以输出 JSON Schema 约束最终响应；共享路线提示此前却要求模型返回 submit_r4_route 工具参数。适配器现在针对 Codex CLI 将这两条工具调用指令转换为 JSON Schema 响应说明。DeepSeek 的工具调用路径不变。
2. R4 分步路线输入现在可携带一个经白名单校验的 RoadFeatureInputProtocol v1 语义片段，其中含匹配的路牌/电线杆、地标、候选关系和 Qwen 意图。它作为 i 字段与有向路线图 m、任务 q、权威当前状态 s 分开序列化；真值字段会在构包阶段被拒绝。模型仍不能据此改变已校验的起点、上车点或下车点。
3. 验证器、本地状态推进、动态方向 schema 和零重试规则保持不变。该上下文不会替代本地有向边校验，也不提供算法回退。

## 离线验证

使用本地 MLX 虚拟环境运行 scripts/run_route_r4_smoke.py 的 8×8 seed 0 道路特征任务，未加 --allow-api：

- Qwen / 协议 / 端点 / 真值隔离 / 可达性 / 调用预算检查：17/17 通过。
- 地图：grid-8x8-seed-0；路线包：64 节点、205 条有向边；紧凑路线图与原图语义等价。
- 分步用户输入包含 i、m、q、s、版本字段；路线系统提示与用户消息合计 4,040 字节，传统全提示对照为 6,334 字节。
- 路线模型请求：0。该检查只证明输入与门禁被正确构造，不证明 Codex 会返回合法动作或完成路线。
- Codex CLI 适配器与分步路线定向测试共 24 项通过；随后全量 unittest discover -s tests 为 171 项通过。Python 编译检查通过。

## 产物

- 本地预检 JSON：outputs/route_layer/r4_stepwise_codex_input_preflight_v1/seed0_semantic_road_feature.json
- 自包含回放：outputs/route_layer/r4_stepwise_codex_input_preflight_v1/replay.html
- 涉及代码：route_experiment/codex_cli_adapter.py、route_experiment/r4_stepwise.py、scripts/run_route_r4_smoke.py
- 测试：tests/test_codex_cli_route_adapter.py、tests/test_r4_stepwise.py

## 费用控制与下一门槛

离线试验不消耗模型请求额度。此前一次 Codex 持久会话试跑报告第一步 9,690 输入 token、第二步 20,626 输入 token，缓存输入为 0；这不足以支持“持久会话更省 token”的说法，因此本轮没有开启持久会话。

下一步若要验证模型行为，应先只做一个有硬请求上限的 Codex 分步动作 smoke，零重试、禁用工具；该结果仍是独立技术试跑。正式批次尚未启动。

## 更新：100×100 冻结任务分步预算预检（2026-10-22）

用户已于 2026-10-21 明确要求后续 LLM 实验使用 Codex CLI；本报告前版中“正式 R4 后端尚未确认”的表述已被更新。后续正式模型组应统一使用 Codex CLI，历史 DeepSeek 数据仍分开报告。

修复了 R4 stepwise runner 把实际模型请求上限当作路线动作上限的问题。现在可独立设置 `max_route_actions` 和 `max_api_calls`；请求数耗尽而路线未完成时返回 `request_limit_exhausted`，保留已校验前缀且不标记完整路线。旧调用未指定新参数时保持旧语义。

新增 100×100 冻结任务预检：地图与 pure LLM v8 地图文件 SHA-256 相同，订单文件 SHA-256 相同；Qwen 意图端点和道路特征片段经校验后传给分步输入；有向紧凑图逐边等价，可达性通过，最短动作下界 109。配置 512 路线动作、1 次实际请求上限时，Qwen/协议/真值隔离/共享输入等 11 项本地门禁全过，路线请求 0。提示为 16,327 字节、schema 1,141 字节；这些字节数不代表 token 或费用。包含新增预算拆分用例后，全量 unittest 172 项通过。可视化和原始产物：`outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/`。

单请求只能检查首个路线动作，不是完整 100×100 路线试验。正式批次仍未开始；需要用同一 Codex CLI 后端对齐模型组、共享地图/订单，并在实际 token 用量基础上冻结整条路线的调用与费用熔断。没有本轮路线模型请求、token 消耗或正式路线结果。
