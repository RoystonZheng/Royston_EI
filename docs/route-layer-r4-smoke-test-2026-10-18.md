# R4 单任务烟雾检查记录（更新至 2026-10-20）

## 结论

**截至 R4 smoke v17，路线合法性烟雾仍未通过，正式多种子实验未启动。** v17 的本地 Qwen/协议门禁 16/16 通过，路线任务两段均可达；获准的一次 DeepSeek 请求返回完整但违规的 32 步提议，其中 10 步不是地图允许的有向边、全部步骤不连续，且未到达上车点/下车点。本地 validator 拒绝接纳，实际路线电耗未计算；提议的假设能耗单独留痕。请求用量 2,405/212 输入/输出 token，费用估算 0.007851 CNY。v15 的 API 成本仍未知，因此累计已知费用小计更新为 0.080949 CNY，不能视为累计总费用。v17 证明本地校验能拒绝该提议，但不能说明服务端为什么返回不符合本地 schema 的参数。详情见 [v17 回放](../outputs/route_layer/r4_smoke_v17_api/replay.html) 与 [v17 JSON](../outputs/route_layer/r4_smoke_v17_api/seed0_semantic_road_feature.json)。

## 输入与意图解析

- 地图：`grid-8x8-seed-0`，64 个节点、205 条有向边、4 个路边特征。
- 任务：`task-0000-0009`，任务类型 `semantic_road_feature_position`。任务文本描述电线杆 A 西侧上车、体育馆东门附近下车。
- Qwen：`mlx-community/Qwen3-4B-Instruct-2507-4bit` revision `50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b`；本地意图推理约 2,273 ms。模型输入约 2,723 字节；正确返回 `node_1_2` 与 `node_6_2`，两者都在候选白名单中，并正确引用路边特征和地标证据。模型输入不含评估真值字段。
- 完整协议只在本地保存，序列化约 92,804 字节；本次向 Qwen 查询的是协议语义索引和任务相关候选，没有把完整地图协议反复发给模型。

## DeepSeek 路线请求与校验

| 指标 | 结果 |
|---|---:|
| 请求模型 | `deepseek-flash` |
| 请求数 / 重试数 | 1 / 0 |
| 路线请求 JSON 大小 | 2,650 字节 |
| 输入 / 输出 token | 921 / 82 |
| 请求延迟 | 1,124.713 ms |
| API 费用估算 | 0.003009 CNY |
| 端点 ID | 与 Qwen 解析结果一致 |
| 路线合法性 | 失败 |

模型返回的移动序列为 `NNNENEEEEES`。第 3 步从 `node_0_7` 向北移动到 `node_0_8`，超出地图边界；其后继续产生图外节点，整条提议没有到达上车点或下车点。独立路线校验确认越界、缺失节点/边、路线不连续和端点顺序错误。离线全图 Dijkstra 事后确认该任务在同一地图上可达；该结果没有提供给模型，也没有作为 R4 兜底路线。

## 失败路线的耗电审计

失败提议包含 11 步，首次违规前有 2 步合法前缀。按项目 `energy_v1` 模型估算：

- 完整提议耗电：约 **0.198 kWh**，其中 9 步只能按图外代理边估算；这不是可执行路线能耗。
- 首次违规前合法前缀：约 **0.036 kWh**，也不作为有效路线成本。
- 按当前 Tesla 参考价 2.29 CNY/kWh 计算，完整提议的假设电费约 0.45342 CNY，合法前缀约 0.08244 CNY；两者都只是审计估算，不计入有效路线费用。
- 有效路线电费：**0**，因为没有通过校验的路线。
- 按项目计算单价估算，本地 Qwen 意图推理计算费约 **0.0000227 CNY**；未包含模型加载开销。

## 协议引用复核与修正

API 请求完成后复核完整协议时发现：当次协议有一条 `landmark_at_node` 关系引用了 `landmark:node_6_1`，但实体表没有登记这个 landmark 实体。Qwen 实际收到的语义索引包含该地标名称和候选点，且无评估真值泄漏；不过完整协议的引用完整性检查不通过，因此本次端到端烟雾仍不能判为通过。

已在 `scripts/run_route_r4_smoke.py` 修正适配器，为地标登记实体，并加上 API 前的关系/候选引用门禁。修正后离线复核得到 274 个实体、210 个特征、214 条关系、5 个语义索引条目，引用均存在且不含真值字段。此修正只做了离线验证，没有再次运行 Qwen/DeepSeek；已执行烟雾产物保留当时真实输入，不被事后修正覆盖。

## 产物和下一道门槛

- 完整输入、模型响应、逐步回放、校验、能耗估算和费用记录：[单任务烟雾 JSON](../outputs/route_layer/r4_smoke_v1/seed0_semantic_road_feature.json)
- 可复现的单任务门控脚本：[run_route_r4_smoke.py](../scripts/run_route_r4_smoke.py)

## 复测记录：显式有向邻接表（R4 smoke v5）

上次越界的根因之一是请求使用十六进制有向出口掩码，而系统提示没有定义方向位、行列索引和边界解码方式。已改成紧凑显式邻接结构：节点 ID 映射到实际可走的 E/W/N/S 出边，边值带目标节点、统一公式计算的通行时间和距离；未出现的方向就是禁止通行。离线测试逐边对照共享路线包，确认有向边无遗漏/增造。

- 本地 Qwen、协议引用、候选白名单与真值隔离均通过。
- 路线输入为 64 节点、205 条有向边，JSON 7,813 字节；起点→上车点、上车点→下车点均可达，端点合法，请求未含评估真值。
- DeepSeek 收到 1 次响应，无重试；输入/输出 token 为 **4,381/128**，API 费用估算 **0.013527 CNY**。JSON 在 `move_runs` 中途截断，解析器报告未结束字符串；输出 token 数正好达到当时的 128 上限。
- 从截断响应恢复出 12 步方向串 `NENESESESESE`。该部分在地图上没有非法移动，但尚未经过 `node_1_2` 上车点或 `node_6_2` 下车点，不能视为路线成功或完整有效路线。
- 对部分提议做事后估算：约 **0.223 kWh**、假设电费 **0.51067 CNY**；这只是未完成提议的地图能耗估算，不是有效路线能耗或实测值。详情见 [部分路线审计](../outputs/route_layer/r4_smoke_v5/posthoc_failure_energy_audit.json)。
- 独立离线测试全套 52 项通过；R4 本地输入预检没有发 API，API 计数为 0。

第二次调用的 API 费用为 0.013527 CNY；连同第一次 0.003009 CNY，已记录烟雾 API 费用合计 **0.016536 CNY**。当前脚本把下一次烟雾输出上限设为 512 token，并在 JSON 解析失败时保留可恢复步骤与能耗估算；这个新上限尚未经过 API 验证。

## 正式批次输入规模预检

对传统组已归档的相同输入（种子 0–9，每种子 10 条订单任务 + 5 条正式场景，共 150 条）先做了显式 JSON 预检，发现其在 100×100 下体量过大；随后改为 `directional-cost-grid-v1` 紧凑格式，并再次本地预检，没有调用 API。语义任务离线保存的真值端点仅用作包尺寸代理，已在序列化前移除。格式按每节点四个字符槽位 E/W/N/S 编码当前节点实际出边，`.` 表示无边；单字符成本码映射到统一边通行时间和距离。离线测试能从编码逐边还原原路线包，方向和边成本都一致。

紧凑格式下，150 条任务中 140 条通过端点/路线包构造门槛，10 条非法上车端点在路线 API 前即可识别；可构造包包含 476–10,000 个节点和 1,694–36,713 条有向边。用户 JSON 体量为 3,604–42,014 字节，中位 **20,395 字节**、均值 **21,247 字节**、P95 **37,226 字节**。8×8 的本地 Qwen 门禁通过，输入为 64 节点/205 条有向边、1,652 字节，两段均可达；该运行没有发 API，产物见 [R4 smoke v6 本地预检](../outputs/route_layer/r4_smoke_v6/seed0_semantic_road_feature.json)。

若主组和 R4-A 每个可行任务最多各规划三次，R4-B 每任务一次，形式上的最大调用数为 **980**。由于新紧凑编码尚未通过 DeepSeek 实测 token 化，当前按两种字节/token 粗略假设，主组上限约 **15.91–27.67 CNY**，三组和两项消融合计约 **37.12–64.56 CNY**（含每次最多 512 输出 token）；这只是预算预警，不是正式预算。详细逐任务体量、种子输入文件哈希和假设写入 [路线输入尺寸审计](../outputs/route_layer/r4_preflight/route_input_size_audit.json)。

## 受控复测：紧凑有向成本网格（R4 smoke v7）

本次按用户授权仅发送 **1 次** DeepSeek 请求，关闭网络重试，输出上限 512 token。此前的 Qwen 意图、协议引用、候选白名单和真值隔离检查全部通过。Qwen 本地推理耗时约 **1,953 ms**，本地计算费用估算 **0.00001953 CNY**。路线请求输入为 1,652 字节；DeepSeek 完整响应，输入/输出 token 为 **974/69**，耗时约 **1,204 ms**，API 费用估算 **0.003129 CNY**。模型/API 与本地推理估算费用合计 **0.00314853 CNY**。未发生 HTTP/API 错误、超时或截断。

模型输出移动序列 `ESSSEEEEENNNN`，正确填写上车点 `node_1_2` 与下车点 `node_6_2`，但独立校验在以下位置判定路线失败：

- 第 1 步从 `node_0_5` 向东到 `node_1_5`，该方向没有出边，反方向是单行边。
- 第 12 步从 `node_6_4` 向北到 `node_6_5`，该方向没有出边，反方向是单行边。
- 提议在第 9 步已经到达下车点 `node_6_2`，仍继续走了 4 步，最后结束在 `node_6_6`。

假设按模型所提坐标路线估算，其距离约 **1,300 m**、耗电约 **0.24105 kWh**、参考电费约 **0.55200 CNY**；这些是失败提议的地图估算，不是可执行路线的实耗或有效路线费用。首步即违规，所以合法前缀距离和耗电估算均为 0。API 费用和假设车辆电费分开列示。

复核还发现旧版 `validate_llm_route` 只要求路线经过下车点，没有要求末节点恰好等于下车点；因此 v7 产物的历史校验错误列表未包含“越过下车点”。已在提示中明确“首次抵达下车点后立即停止”，验证器新增 `route_does_not_terminate_at_dropoff`，并加入回归测试。原始 API 产物 [R4 smoke v7 JSON](../outputs/route_layer/r4_smoke_v7/seed0_semantic_road_feature.json) 保持原样；此次代码修改只由本地测试验证，**未再次调用模型**。

本次复测费用 0.003129 CNY；三次烟雾 API 费用累计估算 **0.019665 CNY**。当前紧凑格式的 100×100 token 化和反馈重规划仍未由 API 实测。正式 R4 的初次规划与最多两次反馈重规划尚未端到端跑通；因此当前不能把本烟雾当作路线效果样本，也不能据此启动正式十种子批次。

## 修正后复测：终点规则加入提示与校验（R4 smoke v8）

此次先用系统 Python 启动时，因缺少本地 `mlx_lm` 依赖在 Qwen 加载前退出，**没有发出 API 请求**；随后改用已安装 MLX 的专用虚拟环境，只完成授权的一次请求。Qwen 意图、协议引用、候选白名单和真值隔离门禁通过；DeepSeek 正常响应且无重试，输入/输出 token 为 **998/90**，延迟约 **1,138 ms**，API 费用估算 **0.003264 CNY**。Qwen 推理约 **1,991 ms**，本地计算费估算 **0.00001991 CNY**。

模型返回 `ESSSENNNEEEEESSS`，响应中的上下车点 ID 与 Qwen 结果一致，但路线不合法：第 1 步从 `node_0_5` 向东到 `node_1_5`，该方向没有出边，反向才是单行边；方向序列几何上在第 4 步经过上车点，之后偏离并结束在 `node_7_2`，没有到达下车点 `node_6_2`。新版验证器报告 `edge_missing`、`route_endpoint_order_invalid` 和 `route_does_not_terminate_at_dropoff`，未发现越界。模型没有因新增“到下车点立即停止”规则而形成有效路径。

该提议长 1,600 m，假设耗电 **0.29535 kWh**、参考电费 **0.6763515 CNY**；首步即违规，合法前缀能耗估算为 0。这些是按模型提议路线计算的假设值，不是可执行路线能耗。此次结果保存在 [R4 smoke v8 JSON](../outputs/route_layer/r4_smoke_v8/seed0_semantic_road_feature.json)。第四次烟雾 API 费用为 0.003264 CNY，四次合计估算 **0.022929 CNY**；R4 仍未达到路线合法性门槛，也没有测试反馈重规划。

## v8 后状态（已由 v9 更新）

在 v8 完成时，正式 R4 仍未开始；反馈重规划尚未由 API 验证。该时点结论已被下文 v9 更新：反馈闭环现已实测两轮，但路线烟雾仍未通过，正式批次依旧未启动。

## 2026-10-19：反馈重规划控制器离线实现

新增 `route_experiment/r4_agent.py`，并接入 `scripts/run_route_r4_smoke.py`。控制器在路线 JSON 无法解析或路线未通过独立验证时，最多发送结构化错误反馈并重新规划；总规划次数上限为 3（首次请求加最多两次重规划）。模型明确返回 `status=failed` 或 API 请求失败时立即停止。重试会重发原始地图/任务输入并附加最近一次的结构化反馈，不回传上一轮完整模型原文，以减少额外输入 token。反馈只包含校验错误码、程序回放发现的违规步及模型端点字段是否匹配，不包含评估器真值、传统算法路线或兜底方案。烟雾脚本的 API 客户端保持零重试；每条响应先落盘再进行下一次请求，并保留逐次原始响应、token、时延、校验、反馈和失败路线的假设能耗。无效路线能耗只作为地图估算，不计作有效路线耗电。

命令行默认 `--max-planning-attempts 1`，可选 1–3；不带 `--api-once`（也可写 `--allow-api`）时只运行本地门禁，不访问 DeepSeek。**在 v9 新授权烟雾之前，本轮只完成离线实现与测试，没有发送 API 请求。** `python3 -m unittest discover -s tests` 共 56 项通过；其中 `tests/test_r4_agent.py` 的 3 项使用假客户端，分别验证违规后反馈、三次上限和首轮成功立即停止。这些测试只证明本地控制流；v9 的真实模型效果见下文。正式十种子批次仍未启动，完整实验预算仍待实际 token 数据复核。

## 2026-10-19：受控反馈闭环烟雾（R4 smoke v9）

用户授权在 8×8 单任务上使用 `directional-cost-grid-v1`，最多 3 次 DeepSeek 规划、每次最多 512 输出 token、网络重试为 0；路线合法或模型明确失败即停止。实际运行使用 **2 次请求**，在第二次模型返回失败后停止，未发出第三次请求。Qwen 意图、候选白名单、协议引用和不泄漏真值等 15 项本地门禁全部通过。

任务为 `task-0000-0009`：起点 `node_0_5`，上车点 `node_1_2`，下车点 `node_6_2`。第一轮 DeepSeek 正常返回完整 JSON，998/82 输入/输出 token，时延 1,374.159 ms，零重试，API 费用估算 0.003240 CNY。路线方向序列为 `ESSSENNNEEEEE`；第 1 步从 `node_0_5` 向东到 `node_1_5`，但该方向不存在合法有向边（反向单行边存在）。路线最终也没有在下车点结束。独立验证报告 `edge_missing`、`route_edge_node_discontinuity`、`route_endpoint_order_invalid`、`route_does_not_terminate_at_dropoff`。路线提议距离估算 1,300 m、假设耗电 0.24075 kWh、假设电费 0.5513175 CNY；首步违规，合法前缀耗电估算为 0。上述路线耗电和电费只是对非法提议的地图估算，不是实际或有效路线耗电。

控制器把这四类程序校验信息及第 1 步违规反馈给模型，没有提供传统算法路线或评估真值。第二轮正常返回 `status=failed`、`reason=no_valid_route`，1207/40 输入/输出 token，时延 1,326.637 ms，零重试，API 费用估算 0.003741 CNY。随后仅对存档地图做了独立的有向图 BFS 可达性审计：起点到上车点、上车点到下车点均可达。因此模型的 `no_valid_route` 是本轮路线规划失败，不能解释为地图客观无解。BFS 路径只用于事后诊断，没有传给模型或作为路线回退；审计单独保存在 [v9 事后可达性审计](../outputs/route_layer/r4_smoke_v9/posthoc_reachability_audit.json)。

两次 API 请求合计 2,205 输入、122 输出 token，API 费用估算 **0.006981 CNY**；Qwen 本地计算费用估算约 **0.00002226 CNY**。连同此前四次烟雾，累计为六次 API 请求、API 费用估算 **0.029910 CNY**。可交互回放见 [R4 smoke v9 回放页](../outputs/route_layer/r4_smoke_v9/replay.html)，由 `scripts/render_route_r4_smoke.py` 从已保存 JSON 离线生成；完整请求、响应、校验、反馈和能耗审计保存在 [R4 smoke v9 JSON](../outputs/route_layer/r4_smoke_v9/seed0_semantic_road_feature.json)。这是单任务接口/控制流烟雾，不是正式效果样本。反馈重规划已经真实调用验证，但尚未得到合法路线；正式 R4 十种子及消融实验仍未启动，100×100 正式预算仍需基于目标配置做离线 token 预检并单独核算。

## 2026-10-19：v9 重试输入格式离线复核与预算更新

对照 v9 的保存请求与 `route_experiment/r4_agent.py` 后发现，第二轮请求把结构化反馈直接追加到原始 JSON 之后，因此整条 user content 不是合法 JSON（可复现为 `JSONDecodeError: Extra data`）。服务端仍返回了第二轮响应，但不能确认格式错误是否导致模型返回 `no_valid_route`，也不能把旧闭环视为格式正确的重规划流程。旧请求首轮为 998 input tokens，第二轮为 1,207，实测增加 209 tokens。

已修复为把反馈写入原输入对象的 `r4_validation_feedback` 顶层字段，系统提示明确它是校验器反馈、不是地图事实；新增回归测试要求重试消息可完整 `json.loads`，当前全套离线测试 57 项通过。本次只做离线修复，**没有发起 API 请求**，所以修复后的模型重规划效果尚未验证。审计文件记录旧消息的解析失败和新消息的合法 JSON：[反馈输入与地图编码审计](../outputs/route_layer/r4_smoke_v9/retry_input_and_map_audit.json)。

同一审计从 v9 实际地图解码 `directional-cost-grid-v1`，205/205 条有向边及其时间/距离成本均与底图一致。起点 `node_0_5` 的 E 槽没有出边，而反向单行边存在，故首步违规是模型没有遵循已提供的方向限制；输入编码未发现增造或遗漏道路。

基于旧尺寸审计的 140 个可构造任务、R4/R4-A 各最多三次且 R4-B 一次，最大调用数为 980。另 10 条未构造项经逐种子核对，正好是预设的 `illegal_endpoint` 场景；上下车点都标为非法，应作为 API 前端点拒绝结果保留在每组 150 条的成功率分母中，不需要路线搜索请求。更新预算预检列出三种场景：8×8 实测 token/字节比例外推并按 v9 反馈增加量估算约 **23.87 CNY**；用户输入每字节按 1 token、系统每次 200 token、观测反馈增量估算约 **64.91 CNY**；再把反馈按每字节 1 token 作压力估算约 **65.73 CNY**。以上均是假设，不是报价或封顶金额；还需在正式运行前冻结预算，并在修复后的单次授权 API 烟雾通过后再评估是否启动多种子实验。详情见 [反馈重规划成本预检](../outputs/route_layer/r4_preflight/retry_feedback_cost_preflight.json)。

## 2026-10-19：修正反馈 JSON 的单次 API 重验证（R4 smoke v10）

用户授权使用 v9 同一张 8×8 地图和首轮违规反馈，以修正后的 JSON 顶层 `r4_validation_feedback` 发送 1 次 DeepSeek 请求；输出上限 512 token、客户端网络重试为 0。检查确认原地图和任务未改、输入可解析且为 2,377 字节，未把 API 密钥写入产物。DeepSeek 正常返回完整路线，输入/输出 token 为 1,200/77，延迟 1,850.404 ms，零重试，API 费用估算 0.003831 CNY。

返回路线序列 `SSSENNEEEEESS`，模型填写的上/下车点 ID 正确，但第 12 步从 `node_6_4` 向南至 `node_6_3`，该方向没有有向边，反向单行边存在；路线校验失败。逐槽复核本次实际发送的地图编码，`node_6_4` 的 S 槽为 `.`，反向 `node_6_3` 的 N 槽为 `7` 且对应单行边，模型违反了输入中明确表示的方向限制。路线假设耗电 0.24505 kWh、假设电费 0.5611645 CNY；违规前合法前缀 11 步，假设耗电 0.20675 kWh。均为地图上的失败提议估算，不是实际或有效路线耗电，电费未计入有效路线成本。此次验证证明修正后的 JSON 请求可以获得正常模型响应，但不能据单次复测断定 v9 的 `no_valid_route` 由 JSON 格式错误造成；也没有得到合法路线。R4 正式十种子与消融批次仍未启动。连同 v9 前六次烟雾，已记录 API 费用估算累计 0.033741 CNY。

证据类型：用户授权的单次真实 API 重验证 + 本地路线校验与回放；产物见 `outputs/route_layer/r4_smoke_v10_retry_validation/seed0_retry_validation.json`、`outputs/route_layer/r4_smoke_v10_retry_validation/replay.html` 和 `outputs/route_layer/r4_smoke_v10_retry_validation/retry_input_and_map_audit.json`。原始 v9 文件保持不变。

## 2026-10-19：生成期有向边硬约束（离线实现，未调用 API）

根因已确认：原路线请求使用 Chat Completions `response_format=json_object`，该选项约束的是 JSON 格式，不会约束输出边是否属于地图；方向网格和提示词仍由模型自行遵守。DeepSeek 文档说明 strict function tool 可按 JSON Schema 约束工具参数，字符串 `pattern` 可用来限制字段格式；Chat Completions strict tool calls 需走 Beta base URL。来源：[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)、[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。

已在本地实现 `build_r4_route_output_constraint`：按局部 `RouteInputPackage` 中实际存在的有向边生成三位大写 base36 槽码；strict 工具 schema 只接受这些槽码组成的路线序列。反向单行边对应的槽码不会被加入允许集合。客户端强制调用该工具；若缺少约束或 schema 与路线包不一致，会在请求前停止；发生网络失败不重试；若本地收到集合外槽码，会记录约束违规并停止，不再自动重发。R4 提案保存精确边序列，断开的边分段回放并按模型选出的实际边分别估算假设能耗，不伪造连接道路。

这项约束只限制“每个输出动作是地图中的合法有向边”，**不保证所有动作首尾连续，也不保证经过上下车点或路线一定可达**；这些仍由原有本地验证器检查。它不调用 Dijkstra/A* 来生成或修复路线。

离线测试确认：单向边正向槽码通过、反向槽码不在 schema 中；集合外槽码被本地拒绝；缺少/错配约束时请求前拦截；strict 模式的客户端网络重试固定为 0。全套 `python3 -m unittest discover -s tests -v` 共 **69 项**通过。没有调用 DeepSeek API，当前无法确认账户实际模型对 Beta strict schema 的接受情况或实际 token 费用。

schema 字节体量离线样本：从 v10 存档重建同一 8×8 任务（64 节点/205 有向边）的约束 schema 为 **851 字节**；种子 0 的一个 2,726 节点/9,864 有向边局部包为 **11,558 字节**；同种子的整张 10,000 节点/36,602 有向边地图为 **40,234 字节**。这些是 schema 字节数，不是 token 数，也不是费用；服务端是否把完整工具 schema 计入输入 token 需在一次小地图验证中读取 usage。正式批次保持未启动。新代码见 `route_experiment/r4_constraints.py`、`route_experiment/llm_adapter.py`、`route_experiment/r4_agent.py`、`scripts/run_route_r4_smoke.py`；新默认产物目录为 `outputs/route_layer/r4_smoke_v11/`。

## 2026-10-19：零违规接纳门槛与逐步自检记录（离线实现）

用户确认路线结果“不允许违规”。本轮将其定义为：任何违反地图方向、路径连续性、端点顺序、边界或终点规则的路线都不能被接纳为有效结果；模型仍可能提出不合法路线，但这类提议必须保留审计并标记失败。此定义不把 LLM 的自检声明当作正确性证明，也不使用传统算法生成或修复 R4 路线。

在已有 strict 有向边输出约束上增加紧凑的结构化自然语言地图指南，说明节点坐标、E/W/N/S 出边槽、缺边禁止通行、反向单行规则、成本码和边界。模型一次返回整条路线，同时按路线边数输出 `step_check_sequence`，每条边对应一个 `1`，表示模型声称已逐步自检；不要求长篇思维链。strict schema 将此字段列为必填，本地归一化器会拒绝自检步数与路线边数不一致的响应。

控制器和回放现在逐步并列记录模型声明，以及本地对实际有向边、连续性和上车/下车顺序的复核；整体验证器仍核验边界、起点、终点、禁行边和路线可达性。任何单步或整条路线校验失败都不能成为 `route_validated`。回放中明确提示“模型自检为模型声明”，旧 v9 结果未保存自检字段，现重新生成其 HTML 后显示“历史记录没有保存逐步自检字段”，不会伪造旧模型曾执行过检查。

离线验证：新增/更新约束、提示、控制器和可视化回归后，`python3 -m unittest discover -s tests` **72 项全部通过**，包括一条真实有向边组成但第二步断开的路线被标记无效、且不得接纳；相关模块 `py_compile` 通过；用已存 v10 JSON 重新生成回放到临时目录，Node.js `--check` 检查内嵌脚本通过。没有调用 API，也没有启动十种子实验。DeepSeek 服务端是否接受新 schema、模型是否会按逐步字段返回，以及实际 token 增量尚未验证；历史 8×8 API 结果不受本次代码影响，正式路线结果数量为 0。新 schema 的字节尺寸本轮未复测，不把旧 schema 尺寸沿用到当前字段集合。

## 2026-10-19：严格 schema 单次 API 烟雾（R4 smoke v11）

用户授权仅发 1 次 8×8 DeepSeek 请求，512 输出 token 上限、零重试、最多一次路线规划。运行前确认 API key 已配置但未输出密钥；系统 Python 缺少 `mlx_lm`，故没有用它发请求；改用已安装 MLX 的 `/tmp/route-r4-mlx-venv/bin/python` 执行。仅发生 **1 次 API 请求**，客户端重试为 0；strict 有向边工具 schema 请求被服务端接受，响应以约束模式 `strict_directed_edge_schema` 返回。schema 本次为 1,052 字节（不等于 token 数）。DeepSeek Flash 输入/输出 **2,168/116 token**，时延约 **1,466.649 ms**，API 费用估算 **0.006852 CNY**；Qwen 本地推理计算费估算 **0.0000212089 CNY**。

模型按契约返回 `status=failed`，未给出路线，也未说明更细原因；没有路线违规动作可供逐步自检，车辆路线耗电不适用。对归档 64 节点/205 条有向边地图做的事后 BFS 可达性审计显示起点 `node_0_5` → 上车点 `node_1_2`、上车点 → 下车点 `node_6_2` 均可达。该审计未发送给模型，也未用于回退。结论是 strict schema 的当前服务端接口可以完成一次符合约束格式的请求，但模型本次没有规划路线；因此**烟雾未通过路线成功门槛**，不能验证合法路线的逐步自检，也不能据此启动正式批次。API 费用估算累计从此前 0.033741 CNY 更新为 **0.040593 CNY**，共 8 次烟雾 API 请求；没有重发或追加请求。

可视化见 [R4 smoke v11 回放](../outputs/route_layer/r4_smoke_v11/replay.html)，原始请求/响应与成本见 [R4 smoke v11 JSON](../outputs/route_layer/r4_smoke_v11/seed0_semantic_road_feature.json)，可达性诊断见 [事后 BFS 审计](../outputs/route_layer/r4_smoke_v11/posthoc_reachability_audit.json)。页面由已存档案离线生成，内嵌 JavaScript 语法检查通过。正式 R4 十种子及消融实验仍未启动。

## 2026-10-19：v11 提前失败分支的离线修复

复核 v11 请求与提示后，确认 DeepSeek 接口正常接受 strict schema 并返回了结构化 `failed`，不是 API 请求、JSON 解析或道路边约束拒绝。事后 BFS 显示本任务的 start→pickup 和 pickup→dropoff 均可达。原提示却允许“不能确认路线”就返回 `failed`，还把任一候选路线自检失败直接定义为整项失败；strict 输出也没有失败原因字段。因此可以确认的是提示/输出契约存在提前退出和失败归类不明的问题，不能从这条响应推断模型内部究竟在哪一步放弃。

已离线修复 `route_experiment/llm_adapter.py` 与 `route_experiment/r4_constraints.py`：提示要求先尝试构造完整路线，当前候选自检失败后继续搜索其他候选；全局最优性未证明不等于无路。失败输出新增必填 `failure_reason_code`，区分 `no_directed_path`（模型声称已确认至少一段不可达）与 `unable_to_construct_route`（未构造出路线但没有确认无路），完成路线使用 `none`。失败审计文案明确“模型声称无路”未经独立可达性验证。v11 回放已从同一原始 JSON 重新生成，显示失败原因缺失及事后可达结论；未改写原始 JSON。未加 Dijkstra/A* 兜底、未开启重试，也未调用 API。`python3 -m unittest discover -s tests` 全套 77 项通过，内嵌 JavaScript 语法检查通过。

这证明离线输出契约和提示逻辑已修正，不证明 DeepSeek 会因此返回路线，也不把 v11 的失败结果改写成成功。v11 原始 JSON 保持不变；回放只更新了对旧失败代码的解释。验证模型行为变化需要另行授权一次零重试烟雾请求。

## 2026-10-19：修复后本地烟雾门禁

使用 `/tmp/route-r4-mlx-venv/bin/python scripts/run_route_r4_smoke.py`，未传 `--api-once`，产物写入 `outputs/route_layer/r4_smoke_v12_local_gate/seed0_semantic_road_feature.json`。本地 Qwen3-4B 意图检查 15/15 通过；路线输入构造通过，地图包为 64 节点/205 条有向边，生成的 strict schema 为 1,307 字节，提示已包含“自检失败后继续搜索候选”规则和区分失败原因的字段。`api_request_count=0`、DeepSeek `not_called`。

这证明修复后的本地输入/提示/schema 门禁可运行；该步骤未让 DeepSeek 搜索路线，不能据此判断路线合法率或模型是否会返回 `completed`。全套离线测试 77 项通过，相关 Python 模块编译检查通过。正式 R4 实验仍未启动。

## 2026-10-19：修复后单次 API 路线验证（R4 smoke v12）

在 v11 提前失败分支离线修复并通过本地门禁后，对同一 8×8 语义任务进行了获准的单次验证：输入地图 `grid-8x8-seed-0`（64 个节点、205 条有向边），起点 `node_0_5`，Qwen 解析的上车点 `node_1_2`，下车点 `node_6_2`。本次调用使用由当前有向图生成的 strict schema（1,307 字节），512 输出 token 上限、零网络重试、最多一次规划。DeepSeek Flash 接受 schema 并正常返回结构化结果；请求数为 1，输入/输出 token 为 **2,377/141**，时延 **1,111.523 ms**，API 费用估算 **0.007554 CNY**。Qwen 解析和本地协议门禁成功。

DeepSeek 返回 `status=failed`、`failure_reason_code=unable_to_construct_route`，路线边序列和自检序列均为空。因此本次没有返回可供路线校验器接纳的路线；不是“返回了违规路线”，也不是 API/格式/schema 请求失败。按已存地图边做事后独立有向 BFS，`node_0_5→node_1_2` 和 `node_1_2→node_6_2` 两段都可达，完整任务在地图上有解。可达性审计及其中路径只用于事后诊断，没有发送给模型，也没有作为路线回退。

由于模型没有给出任何移动序列，无法按模型提议计算路线耗电；产物中的路线电费为 0，含义是“没有有效路线费用”，不能解读成完成路线耗电为零。Qwen 本地推理计算费用估算约 **0.000024005 CNY**，与 DeepSeek API 费用分开记录。连同此前八次烟雾请求，本次之后累计为 **9 次 API 请求、0.048147 CNY API 费用估算**。

结论：修复后的提示、strict schema、API 请求和失败原因解析均跑通，但 DeepSeek 仍未构造出这张可达地图上的路线，故“能否返回合法路线”的烟雾验证**未通过**。这一次无法检验路线自检字段或合法路线能耗，也不能作为正式效果样本；不得据此启动正式十种子实验。原始输入/响应见 [v12 JSON](../outputs/route_layer/r4_smoke_v12_api/seed0_semantic_road_feature.json)，离线有向 BFS 审计见 [v12 可达性审计](../outputs/route_layer/r4_smoke_v12_api/posthoc_reachability_audit.json)，可交互记录见 [v12 回放页](../outputs/route_layer/r4_smoke_v12_api/replay.html)。回放初次生成时沿用了 v11 的“没有失败原因”通用提示，复核后已改为展示 v12 的 `unable_to_construct_route` 并保留旧 v11 的真实说明；新增回归测试后，完整离线测试 **78 项通过**，内嵌 JavaScript 语法检查通过。原始 API JSON 未改。

## 2026-10-19：同输入再次验证（R4 smoke v13）

用户要求再次验证。在与 v12 相同的 `grid-8x8-seed-0`、`task-0000-0009`（起点 `node_0_5`、上车点 `node_1_2`、下车点 `node_6_2`）及相同提示/地图 JSON 上，仅发 1 次 DeepSeek Flash 请求，512 输出 token 上限、零网络重试、最多一次规划。v12 与 v13 的系统提示及用户任务输入哈希一致；本次仍使用 1,307 字节 strict 有向边 schema，DeepSeek 接受约束并正常返回。

模型再次返回 `status=failed`、`failure_reason_code=unable_to_construct_route`，路线序列和自检序列均为空；其完整原始响应与 v12 一致。输入/输出 token 为 **2,377/141**，延迟 **932.902 ms**，API 费用估算 **0.007554 CNY**，Qwen 本地计算费用估算 **0.000022207 CNY**，网络重试为 0。事后 BFS 再次确认起点→上车点和上车点→下车点均可达，审计路径没有发送给模型或用于回退。没有移动序列，因此没有可估算的失败路线耗电。

结论仍是：这次也未返回合法路线；API、strict schema、输入格式和失败原因解析正常，地图客观可达，但模型未构造路线。v12/v13 是同一任务的重复调用，不是两个独立任务样本，不能据此计算路线成功率或证明模型在所有任务上都失败。两次复验合计 API 费用估算 0.015108 CNY；累计十次烟雾请求费用估算 **0.055701 CNY**。正式十种子实验仍未启动。原始输入/响应见 [v13 JSON](../outputs/route_layer/r4_smoke_v13_api/seed0_semantic_road_feature.json)，独立可达性记录见 [v13 BFS 审计](../outputs/route_layer/r4_smoke_v13_api/posthoc_reachability_audit.json)，页面见 [v13 回放](../outputs/route_layer/r4_smoke_v13_api/replay.html)。

## 2026-10-20：一次授权的同输入路线复验（R4 smoke v14）

用户授权在现有 8×8 R4 任务上发 1 次 DeepSeek 请求，512 输出 token 上限、零重试、最多一次规划。Qwen 本地解析/协议门禁 15/15 通过；DeepSeek strict 有向边 schema 为 1,307 字节、含 205 条允许边并被请求接受。实际请求 **1 次**，输入/输出 **2,377/141 token**，客户端重试 **0**，时延约 **60,933 ms**，API 费用估算 **0.007554 CNY**；Qwen 本地计算费用估算 **0.000021033 CNY**。

DeepSeek 返回结构化 `failed / unable_to_construct_route`，没有路线边序列或逐步自检记录。地图、任务及两条路线提示消息与 v13 完全一致，消息 SHA-256 均相同；本次保存的原始模型响应也与 v13 相同。v13 对相同地图的事后 BFS 审计显示起点→上车点和上车点→下车点均可达，且搜索路径没有发送给模型或用作回退。没有路线提议，因此路线能耗未评估，不能把 `electricity_cost_amount=0` 解读为行驶零耗电。

这是同一任务的重复验证，不是新独立样本；R4 正式十种子/消融实验仍未启动。累计 **11 次烟雾 API 请求、费用估算 0.063255 CNY**。原始记录见 [v14 JSON](../outputs/route_layer/r4_smoke_v14_api/seed0_semantic_road_feature.json)，回放见 [v14 回放页](../outputs/route_layer/r4_smoke_v14_api/replay.html)。内嵌 JavaScript 语法检查通过，产物未包含 API key。

## 2026-10-20：只允许完整路线的 R4 单次验证（v15）

用户授权在同一 8×8 任务上发 1 次 DeepSeek 请求，512 输出 token 上限、客户端零重试、最多一次路线规划。离线实现先增加有向可达性门禁；本次本地 Qwen/协议门禁 15/15 通过，起点→上车点和上车点→下车点均确认可达。模型输入只包含方向成本网格和任务端点；门禁只判定可达，不输出路径。工具 schema 仅允许非空 `completed` 路线及地图中 205 条真实有向边槽码，schema 为 1,251 字节。

DeepSeek 请求实际发送 1 次、网络重试 0 次。客户端在解析阶段报 `Unterminated string starting at: line 1 column 108 (char 107)`，未捕获可用路线或可核验的模型工具参数；目前无法确定异常发生在完整 API JSON 还是工具参数 JSON。没有路线提议，路线能耗未评估。初始日志的 token 计数为零占位；用量复制发生在解析之后，异常使输入/输出 token、延迟和 API 成本都无法观测，现已在 v15 产物更正为未知，避免把它们解释成零消耗。Qwen 本地推理计算费估算为 0.000024088 CNY。

按 [DeepSeek Tool Calls 文档](https://api-docs.deepseek.com/guides/tool_calls/) 与 [Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/) 核对，请求使用的 `/beta` strict tool、`pattern`、命名 tool choice、`reasoning_effort=none` 和 `max_tokens` 都属于文档支持的配置。官方响应文档同时提醒，工具 `arguments` 仍应在客户端验证，可能不总是有效 JSON；响应还通过 `finish_reason` 标明是否因 `length` 截断。v15 未保存响应正文和 `finish_reason`，因此不能确认解析错误属于外层响应损坏、工具参数损坏还是长度截断，也不能归因于 strict schema 或模型路径能力。当前适配器已为这些分支补上留痕，待后续获授权请求验证。

结论：提示和 schema 已阻止模型选择 `failed` 退出，但本次没有取得可验证路线；不能认定“LLM 已能作为路线规划器”，也不能启动正式批次。累计已知 API 烟雾费用仍为此前 11 次的 0.063255 CNY；v15 单次费用未知，故不报告累计总成本。离线修复将 malformed tool arguments 交由路线工作流解析，以便保留原输出、provider usage、`finish_reason` 和失败分类；外层响应 JSON 损坏时，记录脱敏响应片段、字节数和 SHA-256。全套离线测试 **85 项通过**。这些新诊断尚未由另一条 API 请求验证。产物：[v15 JSON](../outputs/route_layer/r4_smoke_v15_api/seed0_semantic_road_feature.json)、[v15 回放](../outputs/route_layer/r4_smoke_v15_api/replay.html)。本次真实模型结果仍未通过合法路线门槛。

## 2026-10-20：下一次单次 API 验证的本地预检（v16 preflight）

为避免直接消耗下一次 API 请求，先用固定输入重跑本地门禁并保存候选请求。Qwen/协议门禁全部通过（15/15）；两段有向可达性均为真；候选路线 schema 继续要求非空完整路线并只允许地图中存在的 205 条有向边。输出 token 上限设为 1,024，DeepSeek 未调用（API 请求数 0）。

与 v15 比对，地图、任务、system/user 路线消息和 strict schema SHA-256 完全一致；改变仅为 API 输出上限及新的日志诊断代码。路线消息 SHA-256 为 `e4ca5bfcb2d40f789d27e2568f47500af924fa32e5c2a2521e141405394f2b16`。这份预检证明候选调用不会走失败分支或把 BFS 路线作为模型输入；它没有验证模型能否规划路线。候选请求需用户另行授权。产物：[v16 preflight JSON](../outputs/route_layer/r4_smoke_v16_preflight/seed0_semantic_road_feature.json)、[v16 预检回放](../outputs/route_layer/r4_smoke_v16_preflight/replay.html)。

## 2026-10-20：R4 小型有向图离线门禁

在现有单向边夹具上补充端到端离线门禁，使用 2×2 节点网格、3 条有向边：`node_0_0→node_1_0→node_1_1` 是起点→上车点→下车点的唯一合法路线；另有 `node_0_1→node_1_1`，但从起点无法到达 `node_0_1`。`node_1_1→node_1_0` 的反向单行边不存在。

检查结果：从 `directional-cost-grid-v1` 解码出的实际边槽集合与 strict 输出 schema 允许集合完全一致，均为 `{0,6,8}`；反向槽 `15`（`00F`）不在地图或 schema 中。唯一合法路线归一化为 `node_0_0→node_1_0→node_1_1`，通过端点顺序、方向、连续性、下车点终止校验，且两条边各有一条模型自检声明。另构造 `000008`：两条边各自都在地图/schema 中，但首尾断开；独立路线校验器报告 `route_edge_node_discontinuity` 并拒绝接纳。

全程仅运行本地测试夹具，没有初始化 Qwen，也没有调用 DeepSeek/API；没有发生模型搜索、兜底或路线能耗计算。`python3 -m unittest discover -s tests` 共 **79 项通过**。这验证了地图序列化、schema 和本地校验器在小型例图上的契约一致性，不代表 DeepSeek 会找到路线，也不能替代模型端烟雾。该烟雾仍需单独、明确授权；正式 R4 批次未启动。回归测试见 [`tests/test_r4_constraints.py`](../tests/test_r4_constraints.py)。

## 2026-10-20：R4 smoke v16 单次 API 验证与输出约束修正

用户授权在原 8×8 任务上发 1 次 DeepSeek 请求，1,024 输出 token、零重试、最多一次路线提议。调用前的本地门禁通过：地图 `grid-8x8-seed-0`、任务 `task-0000-0009`，Qwen/协议检查 16/16 通过，start→pickup 与 pickup→dropoff 均有向可达，DeepSeek 请求前计数为 0。此次实际 API 请求 1 次，输入/输出 2,257/1,024 token，时延 63,172.719 ms，重试 0，API 费用估算 0.009843 CNY；Qwen 本地计算费用估算 0.0000293896 CNY。

响应 `finish_reason=length`，工具参数 JSON 在 `edge_slot_sequence` 字符串中途截断，解析错误为 `Unterminated string`。它不是 API 认证或额度失败。离线对原始部分响应做回放，恢复到 244 个完整槽码；按路线包的 base36 规则解码，其中 160 个不属于地图中的有向边，244 步均与上一节点不连续，211 步违反上车/下车顺序。模型没有给出完整路线，也没有到达逐步自检字段；这些是部分输出的本地审计，不是完整模型提议或已执行轨迹。截断部分的地图参数假设能耗估算为 4.439700 kWh、假设电费 10.166913 CNY，合法连续前缀为 0 步/0 kWh；这不是实际行驶耗电。正式路线耗电记为未计算，而非 0。

根因有两层：输出上限先使 JSON 中断；在中断前模型输出的是近似按十六进制顺序枚举槽码，而输入协议规定 base36，且没有构成从起点出发的连续路线。DeepSeek 官方文档说明 strict tool schema 支持 `pattern`，也提醒客户端必须校验工具参数；本次响应因 `finish_reason=length` 未形成完整工具调用，因此不能把 partial stream 当作已满足 schema 的路线。[Tool Calls 指南](https://api-docs.deepseek.com/guides/tool_calls/)、[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。

已在本地收紧输出契约：一条不重复节点的路线最多输出 `节点数−1` 条边，本任务上限为 63；自检码也使用同一上限。提示新增 base36 与十六进制的区分示例（35=`00Z`、36=`010`、255=`073`），明确禁止按编号枚举边。回放现在把截断槽码逐步标出地图出边、连续性、端点顺序检查及假设能耗；没有完整路线时路线耗电显示“未计算”。这些离线修正尚未经过新的 API 请求验证，不能据此声称模型已能规划合法路线。

验证：`python3 -m unittest discover -s tests` 共 89 项通过；相关 Python 编译通过；v16 回放内嵌 JavaScript 通过 `node --check`，HTML 未检测到 API key 格式。没有追加 API 请求。产物为 [v16 原始及派生审计 JSON](../outputs/route_layer/r4_smoke_v16_api/seed0_semantic_road_feature.json) 与 [v16 交互回放](../outputs/route_layer/r4_smoke_v16_api/replay.html)。R4 正式多种子实验仍未启动。

## 2026-10-20：R4 smoke v17 单次 API 复测与违规提议审计

用户授权在同一 8×8 任务上再发 1 次 DeepSeek 请求，输出上限 1,024 token、零重试、最多一次路线提议；本次遵守单次上限，没有启动批量实验。Qwen/协议门禁 16/16 通过，地图 `grid-8x8-seed-0`、任务 `task-0000-0009`，起点 `node_0_5`、上车点 `node_1_2`、下车点 `node_6_2`；本地有向图预检确认两段均可达。strict 工具 schema 含 205 条有向边、1,261 字节，输出边数上限为 63。API 请求 1 次，`finish_reason=tool_calls`，输入/输出 token 为 2,405/212，时延 1,378.598 ms，零重试；API 费用估算 0.007851 CNY，Qwen 本地计算费用估算 0.000021321 CNY。

DeepSeek 返回完整 JSON，声明 `completed` 且端点 ID 正确；本地归一化器以 `r4_edge_slot_constraint_violated` 拒绝。路线序列含 32 个槽码，其中 10 个不属于本地 schema 的允许边集合（9 个越界、1 个反向单行），32 步均与当前节点不连续；提议没有经过上车点或下车点，最终落在 `node_1_0`。模型返回 63 个自检标记，对应 32 个动作，无法逐步匹配。独立本地校验没有接纳该路线；没有执行路线、传统算法兜底或重试。此结果说明提示和请求中的 schema 未能阻止模型返回集合外序列，但本地硬校验将其挡下；单次响应不能确定服务端为何放行不匹配的工具参数，不能把它表述为 API 认证/额度错误。

对违规提议作离线轨迹审计后，假设路径能耗为 0.600100 kWh、按参考价估算假设电费 1.374229 CNY；合法前缀为 0 步。它们只是模型提议的反事实估算，不是实际车辆电耗或有效路线成本。实际路线电耗字段保持 `null`，状态为 `not_evaluated_no_validated_route`。修正了烟雾 runner 的离线落盘逻辑，使 `route_constraint_violation` 也保存完整槽码、自检数量、逐步有向边/连续性检查和假设能耗；回放明确区分完整 JSON 的违规提议与被截断响应。

验证：R4 单测 17 项通过；全量 `python3 -m unittest discover -s tests` **91 项通过**，`python3 -m compileall -q route_experiment scripts tests` 通过，回放内嵌 JavaScript 通过 `node --check`，HTML 未检测到 API key 格式。产物：[v17 原始响应及离线审计](../outputs/route_layer/r4_smoke_v17_api/seed0_semantic_road_feature.json)、[v17 交互回放](../outputs/route_layer/r4_smoke_v17_api/replay.html)。R4 正式十种子及消融实验仍未启动；再次请求需新的授权。

## 2026-10-20：R4 分步动作生成离线实现（没有调用 API）

按用户建议实现了紧凑路线输入和逐步生成。新输入移除重复协议说明及地图版本标识，将动作改为当前节点的单个 E/W/N/S 方向；地图方向成本网格、端点、目标、边界和停止规则仍保留。每次 API 请求的 strict schema 只允许当前地图中真实存在、未禁行、不会重复访问且符合上下车顺序的出方向。本地程序收到方向后按冻结地图独立查边，逐步核对边方向/起点、禁行、边界、重复节点和上下车顺序；模型的结构化自检如有任一项为假也拒绝动作。只有检查通过才推进状态。拒绝时保留当前位置、记录步号/方向/错误码，并在调用预算尚有剩余时把短反馈放进下一请求。抵达下车点后再运行完整路线 validator；没有传统算法回退。

需要区分在线逐步校验和完整路线生成后的整条校验：若 API 一次返回全路径，程序只能在响应后拒绝整条提议；如要逐步拦截后续动作，每一步都需模型—本地 validator 往返，调用数和费用随步数增加。当前 smoke runner 的默认上限是 16 次方向请求、每次最多 128 输出 token、零重试，总输出 token 配置上限 2,048；每次请求都重复发送地图，累计输入 token/费用要等 API 实际返回后才能精确记录。如果请求已发出但没有 provider 用量，token、时延和 API 费用会记为未知而不是零。达到调用上限即停止。

离线假客户端覆盖有向边和成本一致、边界变更检测、动态允许方向、禁行边、重复节点/上车顺序过滤、非法方向或模型自检失败不推进状态、格式反馈、请求错误停止、调用上限及零重试。使用 8×8 seed 0 任务运行本地 Qwen/协议门禁，路线输入构建通过，紧凑图语义 SHA-256 审计通过，system+user 提示字节数从 6,334 降至 2,126（−66.44%；字节数不等于 token），DeepSeek 请求数为 0。该任务起点到上车点最少 4 步、上车点到下车点最少 5 步，总下界 9 次请求；16 次调用上限预留 7 次给非最短动作/失败重提，但不保证完成。产物：[本地预检 JSON](../outputs/route_layer/r4_stepwise_offline_v2/seed0_semantic_road_feature.json)、[逐步方案回放页](../outputs/route_layer/r4_stepwise_offline_v2/replay.html)。

验证：完整测试集 **100 项通过**，相关 Python 模块编译通过，逐步方案回放内嵌 JavaScript 通过 `node --check`。这只证明当前本地状态机、输入压缩审计和回放可运行；v17 仍是最新真实 DeepSeek 结果，本轮没有新 API 路径样本，不代表 DeepSeek 已能返回合法路线。由于逐步动作会增加付费请求数，真实模型验证需新的单次授权；正式十种子实验未启动。具体代码见 `route_experiment/r4_stepwise.py`、`scripts/run_route_r4_smoke.py`、`scripts/render_route_r4_smoke.py`、`tests/test_r4_stepwise.py` 与 [R4 工作流设计第 12 节](route-layer-r4-agent-workflow-design-2026-10-16.md)。

## 2026-10-20：分步路线的首次真实模型尝试与放弃声明校验修复

在 8×8 `grid-8x8-seed-0`、语义任务 `task-0000-0009` 上，使用分步输入、动态方向 schema、每步本地校验、每步输出 128 token、零重试，启动一条真实 DeepSeek 路线尝试。测试使用当时 16 次调用上限；模型前 11 次均返回方向，11 步全部通过本地有向边、连续性、端点顺序和模型自检，未出现非法移动。第 12 次返回 `action=give_up`、`failure_reason_code=unable_to_construct_route`。API 正常返回，无 API 错误；总计 12 次请求、14,700 输入 token、1,492 输出 token、零重试，按项目价格配置估算 API 费用 0.048576 CNY。没有完整路线，路线电耗/电费保持未计算；这不是零耗电。

对原始地图和已接受前缀作离线有向图审计，从模型停在的 `node_7_7` 仍可沿 `SWSWWWWWSSSEEEEE` 到上车点 `node_1_2`、再到下车点 `node_6_2`，共需 16 个后续动作；含模型已走的 11 步，该见证路线共 27 步且不重复节点。该路径仅用于事后证明“仍可达”，没有发给模型、没有替代模型路线，也没有计为完成实验。模型声明放弃时的程序旧版未执行独立可达性检查，直接将其作为终态；同时当时只有 4 次后续请求预算，不足以完成该见证路径。因此这次失败是模型提前放弃与 smoke 调用上限不足，不是 API、鉴权或道路地图不可达。原始 JSON 和单步回放：[真实请求记录](../outputs/route_layer/r4_stepwise_live_v1/seed0_semantic_road_feature.json)、[轨迹回放](../outputs/route_layer/r4_stepwise_live_v1/replay.html)、[离线可达性见证审计](../outputs/route_layer/r4_stepwise_live_v1/posthoc_reachability_audit.json)。

据此完成的本地修正：保留正式路线方案的“不重复节点”约束；确定性可达性门禁将当前已访问节点从剩余两段的有向边搜索中排除。若剩余两段仍可达，动态 schema 不提供 `give_up`；若服务端仍返回放弃声明，本地以相同门禁复核，并在仍可达时用 `give_up_while_route_reachable` 拒绝终止、反馈继续，路线状态不变。该门禁只判断可达性，不把搜索路径发送给模型，也不作为算法回退。将原始第 12 次请求状态重放到修复后的 schema builder，`node_7_7` 的动作枚举变为仅 `move`，允许方向为 `S`，因此旧版 `give_up` 在新 schema 下会被挡下。每次输入加入剩余动作预算，提示要求先规划完整剩余路线再提交下一步，避免只按局部方向贪心；smoke runner 默认上限改为 32 次、每次 128 输出 token、零重试，输出 token 配置上限 4,096。上限只覆盖当前 8×8 单任务 smoke，不能当作 100×100 正式批次预算。

修复验证：`tests/test_r4_stepwise.py` 12 项通过，覆盖可达时拒绝 `give_up` 并继续、无重复路径不可达时允许结束、schema 过滤和剩余预算输入；全量测试 103 项通过，Python 编译检查和失败轨迹 HTML 的内嵌 JavaScript 语法检查通过。更新后的 smoke runner 再次通过 Qwen/协议及路线输入门禁 16/16，205 条地图有向边/成本等价检查通过，DeepSeek 请求数为 0；记录见[修复后本地预检](../outputs/route_layer/r4_stepwise_live_v1/final_local_preflight.json)。当前没有再次请求 API，因此这只是离线修正；LLM 能否走完路线仍未验证。正式 R4 和十种子实验没有启动。下一次真实模型路线尝试须单独授权；单次成功也只证明该固定场景的一次端到端成功，不代表最优性或跨种子成功率。

## 2026-10-20：紧凑输入与剩余预算门禁后的分步模型尝试（R4 stepwise v2）

在 8×8 `grid-8x8-seed-0` 的语义地点任务上，以逐步方向工具进行一次 DeepSeek smoke；每步最多 128 输出 token、最多 32 次动作请求、网络重试为 0。地图/端点可达性预检通过，输入为 `directional-cost-grid-step-v1`。运行审计记录的单次提示字节由旧格式 6,334 降到 2,353（−62.85%；提示字节变化不能换算成 token 节省）。

32 次请求均获得模型响应，累计输入/输出 41,292/3,972 token，模型时延合计 35,061.357 ms，API 费用按项目价格估算 0.135792 CNY。模型给出 29 个通过本地校验的方向动作、2 个被拒绝动作和 1 个 `give_up`。第 3 次请求提出 `N`，但 `node_0_7` 当时只有 `E` 是有效有向出边，程序以 `directed_edge_missing` 拒绝；第 9 次请求从 `node_4_6` 提出 `N` 并重复访问节点，程序以 `node_revisited` 拒绝。两次拒绝均未改变权威路线状态。模型最后停在 `node_6_0` 并主动放弃；在不重复节点的正式约束下，剩余目标链已不可达。此次属于路线搜索失败，不是 API 认证、额度或 JSON 返回失败。

没有合法完整路线；实际行驶耗电/电费保持未计算。已接纳的 29 步前缀假设能耗为 0.54245 kWh、参考电费估算 1.2422105 CNY；此值是未完成路线前缀的模型估算，不是完成任务的路线成本。两条被拒绝动作的假设能耗另行保存在 JSON 中，也不与已接纳前缀相加。

离线将真实提议流重放到更新版动态 schema，首次分歧发生在第 26 次请求：当前位置 `node_2_2`，模型当时提出 `S`；剩余预算 7 次，下一步可选方向 `W/S/E` 的每一个都会使最短合法目标链超出此预算，故新门禁仅允许 `give_up`。这说明确定性程序可以阻止已知的预算死路，但不能证明模型会因此规划成功。实际运行中仍有两次集合外动作，说明服务端动态 schema 不是绝对防线；本地 validator 才是动作能否推进状态的权威门禁。

之后进行的无 API 最终预检通过 17 项输入与路线门禁，地图 64 个节点、205 条有向边；新旧编码的有向边及时间/距离成本哈希一致，首步最短动作下界 9、调用预算 32，API 请求数为 0。`tests/test_r4_stepwise.py` 13 项通过，全量测试 **104 项通过**，Python 编译检查及回放 HTML 内嵌 JavaScript 语法检查通过。原始运行、离线重放、回放和本地预检分别见 [v2 JSON](../outputs/route_layer/r4_stepwise_live_v2/seed0_semantic_road_feature.json)、[约束审计](../outputs/route_layer/r4_stepwise_live_v2/posthoc_constraint_replay_audit.json)、[交互回放](../outputs/route_layer/r4_stepwise_live_v2/replay.html) 和 [本地预检](../outputs/route_layer/r4_stepwise_live_v2/local_preflight_final.json)。

截至 v2：用户建议的精简输入和外置确定性逐步校验已实现并通过离线验证，程序没有接纳两次违规提议；当时真实模型尝试仍未形成完整路线，不能称 DeepSeek 已可作为成功的路线规划器，正式十种子 R4 未开始。后续 v3 验证见下一节。

## 2026-10-20：修复版逐步门禁端到端通过（R4 stepwise v3）

在修复“剩余路线可达性 + 调用预算”门禁后，对同一 `grid-8x8-seed-0` 语义道路特征任务进行 1 次有界 DeepSeek 验证。任务为起点 `node_0_5`、Qwen 根据“电线杆 A 旁、西侧的合法上车点”解析到 `node_1_2`，并根据“体育馆东门附近”解析到 `node_6_2`。Qwen 解析门禁全通过；路线输入使用压缩的 `directional-cost-grid-step-v1`，64 节点/205 条有向边，新旧输入地图的有向边与成本语义哈希一致。提示长度由 6,334 降至 2,440 字节（61.48%；字节减少不等于 token 减少）。

DeepSeek 共请求 31 次（上限 32），每次输出最多 128 token、零重试；累计输入/输出 40,270/3,844 token，模型累计延迟 30,292.507 ms，API 费用估算 0.132342 CNY。模型共提出 6 个本地不允许的动作：5 次在当步没有北向有向边时仍提出 `N`，1 次重复访问节点；本地逐步校验均拒绝这些动作且没有推进状态。这 6 次提议的模型自检字段仍全部填为 `true`，显示模型自检只能作为声明，不能代替程序校验。最终路线由模型方向提议逐步组成，包含 25 条有向边/26 个节点，不重复节点，经过解析后的上车点并在下车点结束。全部已接纳步骤的本地检查和模型结构化自检通过，共享路线 validator 返回 `valid`，无任何违规步被接纳；没有 Dijkstra/A* 路线补全或回退。

此路线的仿真距离为 2,500 m、仿真通行时间 316.042 秒、估算电耗 0.46275 kWh、电费估算 1.0596975 CNY；这些是当前模型和固定能耗参数下的仿真估计，不是实车测量，也不证明达到全局最优。API 加本地计算及仿真电费的总金额估算为 1.192060 CNY。该次结果证明 R4 分步生成、硬校验和反馈能够在这项固定 smoke 任务上产出一条可接纳的路线；6 次被拒提议也证明动态 strict schema 本身不能保证原始模型输出永远在允许集合内，安全保证来自本地拒绝后才不改变路线状态。

复核发现结果 JSON 的顶层共享验证已为 `valid`，但嵌套路线对象仍保留 `validation_status=unvalidated`。已修正运行代码，并新增回归断言；对 v3 派生字段作离线归一化，逐步 API 响应记录保持不变。原始捕获副本与 SHA-256、独立路线核验见 [v3 离线成功审计](../outputs/route_layer/r4_stepwise_live_v3/posthoc_success_validation_audit.json)；可视化见 [v3 逐步路线回放](../outputs/route_layer/r4_stepwise_live_v3/replay.html)，结果及原始捕获分别为 [归一化结果 JSON](../outputs/route_layer/r4_stepwise_live_v3/seed0_semantic_road_feature.json) 与 [原始 API 捕获副本](../outputs/route_layer/r4_stepwise_live_v3/seed0_semantic_road_feature_raw_capture.json)。

验证：定向 R4 测试 13 项通过，全量测试 **104 项通过**，`compileall` 和交互回放内嵌 JavaScript 语法检查通过。此次是单一 8×8 smoke，不是 100×100、十种子或统计性结果；它不证明其他任务成功率或最优性。R4 正式主组、R4-A 与 R4-B 尚未启动。
