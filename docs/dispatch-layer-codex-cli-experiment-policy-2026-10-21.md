# 调度层纯 LLM 实验：Codex CLI 记录口径（2026-10-21）

## 用户确认的调整

从 2026-10-21 起，调度层纯 LLM 组 D5 的新实验调用统一改用 Codex CLI，以减少 DeepSeek API 调用；每次运行单独记录 Codex CLI 版本、模型、推理强度、输入/缓存/输出/reasoning/总 token、耗时、CLI/工具事件、原始 JSONL 与最终输出的路径和 SHA-256。旧 DeepSeek 结果保留为历史条件，不与 Codex 结果合并。

## 固定运行口径

- 后端：`codex exec --json`；模型固定为 `gpt-5.6-luna`，推理强度 `low`。
- 输入：完整车辆状态和已到达订单快照；协议 v1 发送全量候选矩阵，协议 v2 只发送硬约束可行边。统一输出 `selected_candidate_ids`。
- 输出约束：Codex CLI `--output-schema`；候选 ID 是否存在、唯一性、一车一单与硬约束由本地程序再次校验。
- 隔离：`--ephemeral`、忽略用户配置、空临时工作目录、只读沙箱，禁用 `shell_tool` 与 `unified_exec`；提示明确禁止工具。若轨迹仍出现工具事件，该轮结果作废并留档。
- 重试：零重试。失败请求保留原始事件和错误，未经用户重新授权不追加同一 smoke 请求。
- 用量：从 JSONL 的 `turn.completed.usage` 读取可用 token 字段。字段缺失时标为未报告/未知，不得记作 0。Codex CLI 不提供可核验的美元账单数，`api_cost_usd` 保持 `null`，另查账户额度/账单时再补充。
- 对比边界：Codex CLI 的系统上下文、推理过程和模型版本与 DeepSeek Chat Completions 不同，单独标记 `D5-Codex CLI`；不得把两者视为相同模型或合并用量/性能统计。

## C2 单轮接入 smoke v1

共享输入为 C2（4 单、3 辆车），seed 0，仅执行首轮。调用使用 `gpt-5.6-luna`、low、零重试，并禁用 shell/unified-exec。Codex CLI 0.145.0 启动 1 次，事件含 `thread.started` 和 `turn.started`，随后服务端以 HTTP 400 拒绝 strict JSON Schema：`uniqueItems` 不被 `codex_output_schema` 支持。没有 `turn.completed` usage，也没有最终 JSON 或派单；工具事件为 0。该次 token/账单用量均记为**未报告/未知**，不记零。耗时约 5.40 秒。

修正：从 strict schema 删除 `uniqueItems`，重复候选 ID 仍由本地校验拒绝。v1 原始结果和 JSONL 保留，未覆盖或重发。

## C2 单轮接入 smoke v2

用户明确授权一次修复后复测。共享输入仍为 C2、seed 0，仅运行首轮，单次 Codex CLI 调用成功。CLI `codex-cli 0.145.0`、模型 `gpt-5.6-luna`、推理强度 `low`；输入/缓存/输出/reasoning/总 token 分别为 17,955/0/216/111/18,171，模型回合耗时约 10.41 秒。原始 JSONL 含一个 `turn.completed`，无工具调用事件；本地 validator 为 `valid`，12 个候选中接受 3 个匹配，硬约束错误 0、回退 0。该时点 3 单 assigned、1 单 waiting；仍是单轮 smoke，不解读服务率。美元账单不可从 CLI 核验，费用字段保持 `null`。

## C2 完整模拟 seed 0：输入协议 v1（探索记录）

首次完成的 C2 全模拟仍把可行和不可行候选一起传给模型。共 5 个模拟轮次、1 次模型调用；输入/缓存/输出/reasoning/总 token 为 17,625/0/167/62/17,792，模型回合耗时约 8.31 秒。结果 3 单完成、1 单未服务；该记录保留为输入协议 v1 探索样本，不与后续 v2 汇总混合。

## 可行候选输入协议 v2

C4 本地预检发现，完整候选表会把大量硬约束不满足的边也传给模型。D0 状态轨迹代理的最大提示分别为 F1 2.61 MB、F2 5.27 MB、F4 10.46 MB。于是 D5 改为只向模型传 `hard_constraint_ok=true` 的候选边，同时保留当前完整车辆状态和已到达订单快照；硬约束边由程序生成，模型可选择的合法匹配集合不变。每条调用写入 `candidate_input_policy=hard_feasible_candidate_edges_v2`、原始/提示候选数、用量与 JSONL。C4 首轮提示从 F1/F2/F4 的 26,014/50,538/99,586 字节降至 10,024/18,558/35,626 字节。输入内容有版本变化，v1 和 v2 结果不合并。

### C2、C3 seed 0 完整模拟

| 场景 | 订单/车辆 | 完成/未服务 | 调度轮次 | Codex 调用 | 输入/缓存/输出/reasoning/总 token |
|---|---:|---:|---:|---:|---:|
| C2 | 4/3 | 3/1 | 5 | 1 | 17,955/0/214/111/18,169 |
| C3 | 44/18 | 16/28 | 42 | 6 | 112,123/9,216/1,193/623/113,316 |

两组每轮 validator 均为 `valid`，硬约束错误、回退、承诺违约和工具事件均为 0。C3 的平均/P95 接驾等待为 74.27/196.92 秒，空驶 5,941.78 米，模拟能耗 26.252 kWh。结果只是一种子、一种合成配置；服务率分别为 75% 和 36.36%，不能视为 NYC 运营率或方法优劣证据。

### C4 seed 0 负载阶梯

| 车队 | 完成/未服务 | 调度轮次 | Codex 调用 | 输入/缓存/输出/reasoning/总 token | 单配置服务率 |
|---|---:|---:|---:|---:|---:|
| F1（12 辆） | 13/777 | 571 | 5 | 114,106/18,432/933/440/115,039 | 1.65% |
| F2（24 辆） | 25/765 | 585 | 10 | 219,470/84,480/2,406/1,296/221,876 | 3.16% |
| F4（48 辆） | 49/741 | 608 | 20 | 474,249/213,888/4,008/2,131/478,257 | 6.20% |

F1 的在线模拟进程在 15 分钟后仍未生成汇总文件，5 次模型调用的 JSONL 和最终响应均已保存。随后用这些响应作无模型调用的确定性离线重放，完成 571 轮模拟；产物明确标为 replay，端到端时延记 `null`。F2 有 1/10 次模型响应重复选择订单，运行器拒绝该匹配；其余 9 次响应被接受。F1/F4 的 5/20 次响应均接受。三档最终逐轮 validator 均通过，硬约束错误、回退和承诺违约为 0；工具事件为 0。

### 用量与结论边界

v2 的 C2、C3、C4 三档合计 42 次 Codex 调用：输入 937,903（其中缓存输入 326,016）、输出 8,754、总计 946,657 token。此前探索性调用另有 8 次已报告用量：C2 smoke 18,171、C2 全模拟 v1 17,792、C3 全模拟 v1 165,538，共 201,501 token；最早的 schema 失败调用没有 `turn.completed` usage，记为未知。至此本调度实验已知 Codex 消耗合计 1,148,158 token（50 次有用量记录的调用），另有 1 次失败调用用量未知。缓存输入已包含在输入 token 中，不重复加到总量。CLI 没有提供可核验的美元账单，费用保持 `null`。以上均为单种子模拟；NYC TLC 输入是已派遣/完成订单，车辆位置为合成值，接驾使用区域级代理。早期机制预检目录中的部分 C4 D0 文件与本批场景指纹不一致；后续核对确认 `traditional_baselines_complete_v1` 的 C2、C3、C4-F1/F2/F4 D0/D1 与本批 D5 输入指纹和三项输入哈希完全一致，详见下方同输入配对记录。当前单种子结果没有显示服务率优势，仍不能宣称算法总体优势或真实 NYC 服务率。

## 产物

- v1 失败结果与原始 JSONL：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_v1/C2_four_same_second_orders_three_vehicles/single_fleet/`
- v2 smoke 结果与原始 CLI JSONL：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_v2/C2_four_same_second_orders_three_vehicles/single_fleet/`
- C2 完整模拟结果与原始 CLI JSONL：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_c2_full_v1/C2_four_same_second_orders_three_vehicles/single_fleet/`
- 协议 v2 C2/C3/C4 结果与 JSONL：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_feasible_edges_v2/`
- F1 中断调用的逐条用量审计：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_feasible_edges_v2/C4_manhattan_790_order_five_minute_load_sweep/F1/partial_run_audit.json`
- 输出 schema：`dispatch_experiment/schemas/codex_direct_assignment.schema.json`
- 运行入口：`scripts/run_nyc_tlc_dispatch_benchmark.py`
- F1 捕获响应离线重放入口：`scripts/replay_nyc_tlc_codex_dispatch.py`

Codex 结果与此前 DeepSeek D5 C2 smoke 分目录保存；两种后端的模型和上下文不同，不合并统计。协议 v2 已完成 C2、C3 seed 0 和 C4 三档各一个完整模拟；正式多种子实验仍未完成。

### D0、D1 与 D5-Codex seed 0 同输入配对核验

将 `traditional_baselines_complete_v1` 的 D0/D1 与 `pure_llm_codex_cli_feasible_edges_v2` 的 D5 配对。C2、C3、C4-F1/F2/F4 五个配置的场景指纹以及 `scenario_json`、`orders_json`、`zone_od_cost_matrix` 三项 SHA-256 完全一致，不需要重跑传统基线。五个配置的 D0、D1、D5 完成订单数相同：3/4、16/44、13/790、25/790、49/790；单种子结果没有显示 D5 服务率优势。等待、空驶距离和能耗差异方向不一致，不能据此判定某一方法总体更优。D5-F2 有 1/10 个模型响应因重复订单选择被本地拒绝；D5-F1 使用已保存的五条 Codex 响应离线重放，调用数没有增加，端到端时延不可比。TLC 订单和区域接驾成本均为代理设定，五格均为 seed 0，不是 NYC 运营结论或多种子统计结果。机器可读配对表和逐项输入指纹见 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/three_method_codex_cli_seed0_matched_v1/paired_comparison.csv` 与 `comparison_report.md`。本次仅做本地哈希及结果核对，没有新增 Codex 调用。

## 费用核算补充（2026-10-21）

此前“CLI 未报告美元账单，因此费用为 null”的记录只适用于账户实际账单金额；不能据此省略可从 token 与公开费率复算的参考等值。已新增统一计算器 `codex_cli_support.py`：按每轮输入、缓存输入和输出 token 分别计算 Codex Standard credits 等值及 API 标价美元等值；reasoning token 已包含在 output tokens 内。缓存用量缺失时输出上下界，usage 不完整时保持未知。账户实际账单/订阅额度扣减仍单独记为未由 CLI 报告，不把参考等值冒充账单。

以 2026-10-21 核对的 GPT-5.6 Luna Standard 费率计算：Codex 为每百万 token 输入/缓存输入/输出 5/0.5/30 credits；API 标价为 $0.20/$0.02/$1.20。[Codex 官方费率](https://developers.openai.com/codex/pricing)；[GPT-5.6 Luna API 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。Codex credits 是标准费率等值，订阅包含额度使用存在计划级倍数/规则，具体额度与账单金额需查看账户 usage 页面或合同。

对 v2 五个配置（C2、C3、C4-F1/F2/F4）的 42 次已记录调用重新读取归档 JSONL，并与各运行 summary 逐项核对：输入 937,903（缓存输入 326,016）、输出 8,754、合计 946,657 token；对应 3.485063 Standard credits 等值、$0.13940252 API 标价等值。五组 token 合计与原始 trace 匹配；账户实际账单金额仍未知。F1 是已保存模型响应的离线仿真重放，费用只计原先 5 次模型调用，不计新的模型请求，也不把重放时延当在线时延。核对产物：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_feasible_edges_v2/codex_usage_cost_reconciliation_20261021.json`；复算脚本：`scripts/reconcile_codex_cli_dispatch_costs.py`。原实验结果文件未改写。

用户授权的 C2 单轮接入复测已用完：1 次真实 Codex CLI 调用，输入/缓存输入/输出为 15,854/0/230 token，合计 16,084 token；回合 usage 1 条、无工具事件、零重试、12 个候选中接受 3 个且本地 validator 通过。费率等值为 0.086170 Standard credits、$0.00344680 API 标价；真实账户账单仍为未知。结果 `partial_run=true`，只证明接口、校验和记账链路，不是服务率结果；产物 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/codex_cli_cost_smoke_20261021/C2_four_same_second_orders_three_vehicles/F3_same_zone/D5_pure_llm_direct_seed_0.json`。

## 来源

- Codex CLI JSONL 和 `turn.completed` token usage 的 eval 记录方式：[OpenAI Developers：Testing Agent Skills Systematically with Evals](https://developers.openai.com/blog/eval-skills)。
- 本项目遇到缺失的 usage 时统一记为未知，不推断为零；CLI 账户账单金额另行核对，不从 token 数倒算。

## Codex CLI 接入修复及历史费用审计（2026-10-21）

调度 Codex adapter 现以排他创建方式续接 trace 序号，避免覆盖；子进程统一禁用 25 项工具/插件/搜索/浏览器/记忆入口并移除 provider API key；未知 JSONL item 一律按工具事件处理；超时和异常时保留已捕获 JSONL、可用 token 与标准费率估值；每次结果完整记录工具策略和费用口径。模型响应发生工具事件时不接受其匹配结果。账户实际账单保持 `null`。

Codex CLI 历史用量复算覆盖调度目录下 51 条 trace，其中 50 条含 usage，1 条 schema 请求失败无 usage。已知用量 1,137,254 输入（含缓存 326,016）、10,904 输出，共 1,148,158 token；按 GPT-5.6 Luna Standard 费率为 4.546318 credits 等值、$0.18185272 API 标价等值。它们是标准费率参考数，不是账户实际扣款。调度结果仍为 seed 0，不能据此报告多种子性能结论。跨层审计见 `docs/codex-cli-experiment-readiness-and-cost-audit-2026-10-21.md` 和 `outputs/codex_cli_audit_2026-10-21/cost_audit.json`。


## D5 Codex CLI 十种子正式批次结果（2026-10-21）

新增 seed 1–9 的 45 个 D5 运行已完成；seed 0 复用归档结果，其中 C4-F1 是已捕获响应的离线重放。五个配置、十个种子共 50 组 D0/D1/D5 配对齐全，150 个方法结果全部完整且输入哈希、场景指纹、地图、订单和车辆信息匹配。模型为 `gpt-5.6-luna`；新批次零重试、零工具事件。

50 个 D5 结果共 425 次调用，输入 8,810,591（缓存输入 4,610,816）、输出 89,927，总计 8,900,518 token。按 GPT-5.6 Luna Standard 公开费率折算为 26.002093 credits、API 标价等值 $1.04008372；Codex CLI 没有报告账户发票或订阅实际扣额，实际账单未知。

三种方法在五个固定场景的完成订单数相同。等待、空驶和能耗的变化方向不一致，没有一致的纯 LLM 优势。18,066 轮已接纳状态/匹配校验全部通过；审计另发现 7 条模型输出因重复订单/车辆或候选不满足硬约束而被拒绝，未用于派单，无重试或回退。重复种子仍使用同一组固定需求场景，因此这些结果不代表独立 NYC 订单样本，也不能写成真实运营服务率、统计显著性或跨场景泛化结论。

逐项结果见 [`comparison_report.md`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_formal_v1/comparison_report.md)，机器可读配对表见 [`paired_results.csv`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_formal_v1/paired_results.csv)，费用和完整性复核见 `outputs/codex_cli_audit_2026-10-21/d5_reconciliation_50_of_50_20261021.json` 与 `d5_final_integrity_50_of_50_20261021.json`。

## D6：Agent + Codex 策略参数 + 数学优化器首轮 pilot（2026-10-22）

D6 与 D5 分开记录：Codex 只在首个已到达订单快照上生成一次有界 `linear_clip` 情境参数，车辆 Agent 按参数筛选硬约束可行候选，数学优化器做全局匹配。C3 seed 0 的首次 CLI 返回有效参数；随后本地因缺少导入而中断。保留的 JSONL/最终响应在字节级相同 prompt 校验后被离线重放以完成仿真，没有第二次模型请求。故 token 与费率等值计入原始单次调用；首次进程耗时未能保存，D6 总决策时间不与其他方法的端到端延迟比较。

该次调用使用 `gpt-5.6-luna` / low，零重试、零工具事件；输入/缓存/输出/总计为 13,453/0/178/13,631 token，按 2026-10-22 标准费率为 0.072605 credits 与 $0.00290420 API 标价等值，账号实扣未知。D6 完成 41 轮，逐轮 validator 有效、硬约束错误与承诺违约为 0、候选回退 2 次；C3 seed 0 完成 16/44，与同输入 D0/D1/D2/D5 相同。相对 D1，平均接驾等待 +4.96 秒、P95 −12.06 秒、空驶 +396.8 米、能耗代理 +0.071 kWh。单个固定案例没有性能优劣结论。

结果、原始 prompt/JSONL/最终响应、输入哈希与用量审计见 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/hybrid_agent_codex_nn_v1/C3_manhattan_multizone_44_order_burst/single_fleet/`。完整方法与 NN 数据标签边界见 `docs/dispatch-layer-hybrid-agent-codex-nn-design-2026-10-22.md`。D7 已更新为最大匹配容量保护组，神经网络排序顺延为 D8。

## D7 最大匹配容量保护配对 pilot（2026-10-23）

在 C4/F1/seed 0 完成 D6/D7 完整仿真。D7 的 Codex 响应通过同 prompt SHA-256 校验后回放给 D6；唯一新调用 1 次，13,451 输入 + 210 输出 = 13,661 token，0.073555 credits / $0.00294220 标准费率等值，账户实扣未知。D7 571 轮 validator 全通过；其中 round-0002 的完整硬可行图最大匹配容量 5、筛选后 4，保护器恢复 1 条边并恢复至 5。D6/D7 均完成 13/790，单 seed 不能证明性能改善。逐轮输入、响应、用量和审计见 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/hybrid_agent_codex_capacity_shield_v1/C4_manhattan_790_order_five_minute_load_sweep/F1/`。
