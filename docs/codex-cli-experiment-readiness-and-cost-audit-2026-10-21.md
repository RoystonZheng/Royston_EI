# Codex CLI 实验链路就绪与费用核算（初版 2026-10-21；复核 2026-10-22）

## 核查结论

已修复并通过本地测试验证目前发现的 Codex CLI 接入问题：重启客户端后 trace 文件序号会从头开始、不同运行记录可能撞名；未知 JSONL item 类型没有一律按工具事件处理；超时和异常路径可能丢失已返回的 usage；轨迹策略摘要未完整列出实际禁用的功能；费用汇总容易把缺失用量误读为零或完整总量。现在 route 与 dispatch 两条 Codex CLI 适配器共用不会覆盖已有文件的 trace 序号、25 项功能禁用清单和 token 费率估算；未知工具事件会使该轮无效，未知用量保持未知。

本地验证：截至 2026-10-22，完整 `python3 -m unittest discover -s tests` 共 **145 项通过**；Codex CLI `exec --help` 支持通用 `--disable <FEATURE>` 参数。最新路线回放内嵌 JavaScript 通过 `node --check`。

**Codex CLI 调用、usage 留痕和费率估算链路可以运行，但路线层正式 R4 十种子批次仍未准入。** 最新独立 8×8 v9 试跑 8 次动作均合法，却未到上车点；同时修复了独立任务回放和费用汇总，以及授权请求数不足时的本地早停门禁。详细证据见 [v9 试跑报告](r4-codex-independent-pilot-v9-2026-10-22.md)。调度 D5-Codex 十种子配对结果已另行完成，不能据此推断路线层 R4 已就绪。既有 DeepSeek 结果保留为历史条件，不与 Codex 用量和性能合并。

## 官方费率口径

按 GPT-5.6 Luna Standard 费率快照分别核算 Codex 标准 credits 等值与 API 美元标价等值：

| 费率 | 输入 / 每百万 token | 缓存输入 / 每百万 token | 输出 / 每百万 token |
|---|---:|---:|---:|
| Codex Standard | 5 credits | 0.5 credits | 30 credits |
| GPT-5.6 Luna API 标价 | $0.20 | $0.02 | $1.20 |

Codex CLI 的 ChatGPT 登录用量不按 API 标价直接出账。Standard credits 等值是费率卡估算，订阅已包含额度可能按不同规则扣减；CLI 未报告本次试验实际账户账单或实际 credits 扣减。Codex usage 页面是账户额度/消费的核对来源。来源：[Codex 官方价格与额度说明](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)、[Codex 与 ChatGPT 计划](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)、[个人计划 credits 说明](https://help.openai.com/en/articles/12642688-using-credits-for-flexible-usage-in-chatgpt-personal-plans)。

## 本地 trace 历史用量

审计 `outputs/` 中路径属于 Codex 实验的 65 条唯一 JSONL trace，逐条按已配置模型 `gpt-5.6-luna` 的费率表复算。Codex JSONL 本身不记录模型 slug，因此费率模型来自各实验 runner 的模型配置。63 条 trace 有 `turn.completed.usage`；2 条启动后没有 usage，单独列为未知，不将其补成 0。缓存输入计入输入 token 子集，不重复相加；reasoning token 已包含在输出 token 内。

| 实验组 | trace / 有 usage / usage 未知 | 已知输入（含缓存） | 已知缓存输入 | 已知输出（含 reasoning） | 已知 token 合计 | Codex Standard 等值 | API 标价等值 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 调度 D5-Codex CLI 历史结果 | 51 / 50 / 1 | 1,137,254 | 326,016 | 10,904 | 1,148,158 | 4.546318 credits | $0.18185272 |
| 单独的调度成本 smoke | 1 / 1 / 0 | 15,854 | 0 | 230 | 16,084 | 0.086170 credits | $0.00344680 |
| 路线 Codex 续跑 pilot | 8 / 8 / 0 | 112,890 | 0 | 8,786 | 121,676 | 0.828030 credits | $0.03312120 |
| 其他路线 Codex CLI smoke | 5 / 4 / 1 | 103,589 | 52,864 | 4,495 | 108,084 | 0.414907 credits | $0.01659628 |
| 合计（63 条已报告 usage） | 65 / 63 / 2 | 1,369,587 | 378,880 | 24,415 | 1,394,002 | 5.875425 credits | $0.23501700 |

扫描过程中发现一条单独归档的调度成本 smoke：它属于 `single_round_smoke`，返回 1 个调度决策响应，`partial_run=true`、`performance_claim=false`；不并入 D5 seed 0 配对结果。该文件不是本次测试或费用审计生成的；本报告只据存档 JSONL 核算其 usage。记录位于 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/codex_cli_cost_smoke_20261021/C2_four_same_second_orders_three_vehicles/F3_same_zone/`。

合计只覆盖 63 条有 usage 的记录，**不是 65 条 trace 的完整 token 总量或实际账户费用**。未知记录是调度 v1 strict schema HTTP 400 和路线 Codex smoke v2 HTTP 400；两条都没有 `turn.completed.usage`。相应单条估算为 `null`。

8 次路线 pilot 请求的费用复算结果为 0.828030 Codex Standard credits 等值、$0.03312120 API 标价等值。该路线仍是不完整前缀；费用不是完整路线成本。

## 产物和复算

- 完整逐 trace 清单：[cost_audit.json](../outputs/codex_cli_audit_2026-10-21/cost_audit.json)
- 官方费率快照：[codex_cli_pricing_2026-10-21.json](../configs/codex_cli_pricing_2026-10-21.json)
- 审计入口：[audit_codex_cli_costs.py](../scripts/audit_codex_cli_costs.py)
- 公共 trace/费用逻辑：[codex_cli_support.py](../codex_cli_support.py)
- 路线 pilot 汇总：[README.md](../outputs/route_layer/r4_codex_pilot_continuation_v1/README.md)
- 路线 adapter：[codex_cli_adapter.py](../route_experiment/codex_cli_adapter.py)
- 调度 adapter：[llm.py](../dispatch_experiment/llm.py)

本次实现/核算记录属于本地代码与归档 trace 审计，不追加模型调用，也不构成新模型质量实验。

## 本轮末次就绪复核（2026-10-21）

- C2 D5 单轮真实复测：15,854 输入、0 缓存输入、230 输出 token；1 次请求、零重试、零工具事件、validator 通过。费用估值 0.086170 Codex Standard credits、$0.00344680 API 标价等值；该次为 `partial_run=true`，不作为派单性能结果。具体单次产物见 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/codex_cli_cost_smoke_20261021/`。
- 路线层本地门禁用隔离 Python 3.13 环境（`mlx-lm 0.32.2`）重跑，17/17 项通过；未传 `--allow-api`，Codex/DeepSeek 请求均为 0。结果 `outputs/route_layer/r4_codex_cli_local_preflight_costfix_20261021/seed0_semantic_road_feature.json`。正式 R4 仍需先做一次有调用上限的 Codex 完整路线 pilot：现有 8 次续跑只有合法前缀，未到上车点；费用和动作链路可记录，但模型完整路线成功尚未证明。
- 真实 C2 调用 stderr 有一次非致命模型缓存字段错误；调用仍正常返回并通过校验。调用后 `~/.codex/models_cache.json` 已刷新，当前 5 个模型条目都有 `base_instructions`；未手工改写 CLI 私有缓存，避免额外请求复测。
- 本轮末次完整测试套件为 122 项通过；Codex CLI 为 0.145.0，`exec --help` 确认实验适配器使用的隔离、JSONL、schema、工具禁用参数均存在。

就绪判断：调度 D5-Codex 的记账与单轮真实调用通过，可启动正式多种子对照；路线层 Codex 运行链路和费用汇总已接好，但建议先完成受控全路线 pilot，再启动十种子路线批次。费率等值不代表账户实际账单或订阅额度扣减。

## Codex 路线 pilot v2 续跑与更新后的费用快照（2026-10-21）

用户授权在 v1 基础上最多追加 8 次 Codex CLI 请求，零重试，遇到工具事件、非法动作或调用错误立即停止。v1 当时停在 `node_5_7`，离取送任务至少还需 16 步，故这轮授权上限不足以完成路线；仍按授权执行，以记录后续单步动作，不改变地图或订单。

8 次请求均返回合法动作并通过确定性门禁，无重试、无工具事件、无非法动作。路线从 `node_5_7` 到达 `node_4_4`；与 v1 合并后共 17 条合法边、1,700 米、约 210.625 秒、前缀仿真能耗 0.318 kWh。上车点 `node_1_2` 和下车点 `node_6_2` 均未到达，剩余最短步数 10；该结果仍是部分路线，不能作为完整路线或正式 R4 结果。

本轮 8 个请求共 113,037 输入（缓存 0）和 10,080 输出 token，合计 123,117 token；按 GPT-5.6 Luna Standard 费率折算为 0.867585 credits 等值、$0.03470340 API 标价等值。含前置 v5 单步和 v1 在内的独立试跑链累计 17 次请求，240,080 输入、20,239 输出（共 260,319 token），费率等值为 1.807570 credits 和 $0.07230280。账户实际账单和订阅实际额度扣减仍未知。明细 `outputs/route_layer/r4_codex_pilot_continuation_v2/codex_usage_cost_reconciliation.json`；逐 trace 审计 `outputs/codex_cli_audit_2026-10-21/cost_audit_after_r4_codex_pilot_v2.json`；回放 `outputs/route_layer/r4_codex_pilot_continuation_v2/replay.html`。

费用扫描本轮复查发现 96 个候选 JSONL，其中 1 个是 dispatch 的 `batch_progress.jsonl` 而非模型调用 trace，已从模型用量计数中排除。当前共有 95 条唯一模型 trace，93 条有 `turn.completed.usage`，2 条有 turn 开始但没有 usage；已知部分为 1,904,552 输入（缓存 454,912）和 39,153 输出，共 1,943,705 token，费率等值 8.650246 credits 和 $0.34600984。完整 token 总量与账户实费仍未知。相较先前快照，工作区出现 22 条 dispatch seed 1 真实调用 trace；其 `pure_llm_codex_cli_formal_v1/experiment_manifest.json` 当前标注为 `running`。这批调度文件不是本次路线续跑创建或修改的，审计分组单独归于调度层。

为避免上述误计数，`audit_codex_cli_costs.py` 现在跳过没有 `turn.started` 或 usage 事件的非 trace JSONL；4 项费用审计单测通过。路线可视化 renderer 已支持合并父段和续跑段；回放可逐步查看 16 个新增动作，完整审计数据和 JSONL 均随 run 保存。

## Codex 独立路线 pilot v3（2026-10-21）

在 v2 的已接纳前缀上继续使用同一张 `grid-8x8-seed-0` 地图（64 节点、205 条有向边）和同一任务（`node_0_5` → `node_1_2` → `node_6_2`）。本轮按授权最多 8 次新请求、零重试；第 7 次触发停止规则后立即停下，未发送第 8 次。6 个提议动作被接纳，0 工具事件、0 重试。路线停在 `node_0_4`，取送点都未到达，尚需至少 8 步，因此仍是部分试跑。

停止原因须与道路违法区分：第 7 次模型提议 `S`，地图上 `node_0_4 → node_0_3` 的有向边存在，本地确定性合法性检查通过；但是模型自检把 `not_revisited` 返回为 `false`，与本地检查“目标节点未访问”相矛盾。按协议要求每个自检字段必须为 true，该响应被拒绝且没有执行。因此这次不是实际违规行驶，也不是车辆耗电 0.018 kWh：实际车辆能耗为 0 kWh；若执行该候选边，模型估算为 0.018 kWh，只是未发生动作的假设值。

本轮 7 次真实请求共 65,626 输入、7,931 输出（合计 73,557 token，缓存输入 0）；按 GPT-5.6 Luna Standard 费率卡估算为 0.566060 credits 等值和 $0.02264240 API 标价等值。CLI 未提供账户实际账单或订阅扣额。包括初始 smoke 与 v1–v3 在内累计 24 次模型调用、305,706 输入、28,170 输出（333,876 token），费率等值 2.373630 credits / $0.09494520。费率估算不是实际现金支出。

为减少重复输入，本轮只在 route adapter 的 CLI 调用上增加 `--config skills.include_instructions=false`，没有改全局 Codex 设置。观察到的平均输入从 v2 每请求约 14,130 降至 v3 约 9,375 token（约少 33.7%）；这是两轮观察值，不足以证明下降由此参数单独造成。恢复逻辑、累计成本汇总、回放中被拒候选及其耗电标签均已加回归测试；21 项相关定向测试通过，触及文件的 Python 编译检查通过。本轮不是完整路线成功证据，也不与固定模型/协议的正式 R4 结果合并。

产物：[`v3 说明`](../outputs/route_layer/r4_codex_pilot_continuation_v3/README.md)、[交互式回放](../outputs/route_layer/r4_codex_pilot_continuation_v3/replay.html)、[最终审计](../outputs/route_layer/r4_codex_pilot_continuation_v3/final_audit.json)、[逐请求用量与费用复算](../outputs/route_layer/r4_codex_pilot_continuation_v3/codex_usage_cost_reconciliation.json)。完整 pytest/unittest 总套件未在本轮重复执行；上列测试是针对修复点的专项测试。

## R4 stepwise 输入协议 v2 离线修正（2026-10-21）

复查 v3 第 7 次原始 prompt 与响应后，确认模型收到的已访问节点是三位 base36 拼接串，并需回答双重否定字段 `not_revisited`；当时本地校验判定目标未访问而模型把该字段报为 false。现有证据不能证明编码或字段命名是唯一根因，所以只将其作为降低误读风险的协议修正，不宣称已修复模型能力。

路线 stepwise payload 升为 `r4-step-v2`：`s.v` 直接列出按访问顺序排列的 `node_x_y` 编号；删除重复传输的方向历史；自检字段改为正向 `target_node_is_new`，并在 JSON Schema 为五项自检增加含义说明。所有自检必须严格为 true、方向必须在动态候选中、确定性道路校验必须通过的门禁保持不变；false 或旧协议字段会被拒绝。新输入将略微增加访问列表字符数，但去掉了重复方向序列与 base36 解码说明，尚未通过 tokenizer 或真实 usage 实测其净成本。

以回归测试先失败后实现；R4 stepwise 14 项、Codex route adapter 5 项、pilot 恢复/协议 2 项、费用 finalizer 1 项、回放 renderer 1 项，共 **23 项专项测试通过**；Python 编译检查通过。没有发送模型请求。该变更只证明离线协议、解析与严格拒绝规则互相一致；新 schema 的真实模型可用性和完整路线成功尚待一次有界 live pilot 验证，因此路线层尚未达到正式多种子实验准入。

## 调度 D5 Codex 正式批次进度快照（2026-10-21）

检查时 `run_nyc_tlc_codex_formal_batch.py --seeds 1..9` 的 PID 95924 及其 seed 4 / C4 / F4 D5 子进程仍在运行；进度账本已有 39 个完成事件，最后一个状态为 D5 正在进行、7 条 trace、6 个完成 turn。未终止进程、未修改调度 adapter 或批次配置。待批次退出后需逐项核对 paired inputs、结果完整性、trace usage 与费用汇总，当前不宣告调度正式批次完成。

并行只读审计 19 个已生成且 `partial_run=false` 的 D5 结果（全批应有 45 个）时，每个结果的 JSONL trace 数、完成 turn 数、输入/缓存输入/输出 token 均与结果汇总相符，按费率配置逐条复算的 credits 与 API 标价也全部相符；19 项 `commitment_violations` 合计为 0。当前部分合计 151 次 CLI 调用、3,069,425 输入（缓存 1,476,096）、31,610 输出 token，费率等值 9.652993 credits / $0.38611972 API 标价。该金额是已完成 19 个结果的费率估值，不是账户账单；仍有其余 26 个结果未完成，不能当作全批费用或最终服务率。

截至本次检查，全量 `unittest discover -s tests` 已重新运行并通过 **129 项**；Codex CLI 版本为 0.145.0，帮助确认 `--config`、`--output-schema`、`--json`、`--ephemeral`、`--sandbox`、`--disable` 和 `--output-last-message` 参数可用。官方价格页再次核实 GPT-5.6 Luna Standard 为 5/0.5/30 credits 每百万输入/缓存输入/输出 token，API 标价为 $0.20/$0.02/$1.20；Codex CLI 未报告实际账单或订阅实际扣额。[Codex 官方 Standard 费率](https://developers.openai.com/codex/pricing)；[GPT-5.6 Luna API 费率](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

后续实时快照：D5 批次 PID 95924 仍运行；截至 2026-10-21 09:28:13 UTC，45 个预期 D5 结果中有 28 个完整结果，正执行 seed 6 的 C4/F2 D5（进度账本记录 9 条 trace、8 个完成 turn）。该进度不是最终费用或性能结论。

## R4 Codex 独立路线 pilot v4 完成（2026-10-21）

用户授权在 v3 最后已接纳节点基础上最多新增 8 次 Codex CLI 请求、零重试；若出现工具调用或非法动作立即停止。v3 被拒绝的 `S` 动作没有执行或重放。使用 `r4-step-v2`，将已访问节点以完整 `node_x_y` 列表传入，把双重否定自检 `not_revisited` 改为正向 `target_node_is_new`。v4 第 1 次再次选择 `S`，本地目标节点为未访问的 `node_0_3`，模型五项自检与本地有向边校验均通过；其后 7 次调用也全部通过门禁，最终完成到 `node_1_2` 上车并到 `node_6_2` 下车。8/8 接纳，0 工具事件、0 重试、0 非法移动，路线无连续性错误且无重复节点。

该试跑包含程序按剩余最短步数下界筛除不能在动作预算内完成的方向；Codex CLI 选择每一步，程序确定性校验并执行。因此这是“LLM 逐步选边 + 本地可行性过滤/硬校验”，不是无约束纯 LLM，也没有用 Dijkstra/A* 替模型补路。v4 8 次续跑中 7 步 schema 只有一个允许方向、1 步有两个方向，说明这段成功验证的是受约束的调用与执行链，模型本身的路线选择空间有限。完整路线 31 条有向边、3,100 米，仿真驾驶时间 379.514 秒，`energy_v1` 估算能耗 0.577450 kWh；没有与传统基线比较，不能据此声称最优或性能优越。v3 第 7 次错误自检的候选动作仍列入组合回放：当时实际车辆能耗 0 kWh，若执行的反事实估算为 0.018 kWh。

v4 8 次请求 usage 为 78,820 输入（缓存 8,320）、3,346 输出，共 82,166 token；官方 GPT-5.6 Luna Standard 费率卡等值为 0.457040 credits / $0.01828160 API 标价。初始 smoke 与 v1–v4 合计 32 次请求、384,526 输入（缓存 8,320）、31,516 输出，共 416,042 token；费率卡等值 2.830670 credits / $0.11322680。账户实际现金账单/订阅扣额未由 CLI 报告。该输入协议在这一个任务上完成了完整路线，但单次观察不能证明更名或编码变更是因果修复，也不能支撑泛化能力结论。

审计、JSONL 逐请求费用与回放产物见 [`v4 说明`](../outputs/route_layer/r4_codex_pilot_continuation_v4/README.md)、[交互式回放](../outputs/route_layer/r4_codex_pilot_continuation_v4/replay.html)、[最终审计](../outputs/route_layer/r4_codex_pilot_continuation_v4/final_audit.json) 和 [token 费率复算](../outputs/route_layer/r4_codex_pilot_continuation_v4/codex_usage_cost_reconciliation.json)。正式路线多种子批次没有在本轮启动。

## R4 Codex 独立路线 pilot v5 受限复试（2026-10-21）

用户授权在同一冻结 8×8 地图和任务上最多发 8 次 Codex CLI 请求，零重试；遇到工具调用或无效动作立即停止。唯一方向动态 schema、五项自检和本地有向边/边界/顺序/不重访校验均保留。预检 0 次模型调用，确认地图 64 节点、205 条有向边等价；当前状态到下车点的最短剩余路程为 10 步，超过本轮 8 次授权，因此这轮即使无错误也不能完成任务。

实际发送 6 次请求后停止：前 5 次动作合法并被接纳；第 6 次模型选择 schema 允许的 `S`，对应 `node_4_7 → node_4_6`，本地确定性检查通过，但模型错误地把 `allowed_directed_edge` 与 `inside_boundary` 自检填为 false。该候选没有执行，零工具、零重试；不是本地发现非法边，而是模型自检不一致触发停止。车辆停在 `node_4_7`，未到上车点/下车点。实际合法前缀 6 条边、600 m、69.375 秒、估算 0.112 kWh；未执行候选动作耗电记 0 kWh，若执行的估算增量为 0.019 kWh。

6 次请求共 58,296 输入（缓存 0）、2,677 输出，共 60,973 token；官方 GPT-5.6 Luna Standard 费率等值 0.371790 credits、$0.01487160 API 标价。含启动 smoke 后累计 7 次、72,449 输入、4,050 输出、76,499 token；费率等值 0.483745 credits / $0.01934980 API 标价。账户实付金额及订阅额度扣减仍未知。价格依据：[Codex Standard 定价](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API 定价](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

本轮再次暴露的限制是：schema 能保证只提出动态允许方向，但模型可能对自己刚返回的合法方向错误报告约束自检为 false；当前保守门禁宁可停止也不执行与自检矛盾的动作。v5 是部分路线且未使用完 8 次授权，不能当作模型的完整路线失败率或性能结果。离线修复批次标识后，路线专项测试 8 项通过；完整 unittest **135 项通过**。回放及每次响应/JSONL usage 见 [`v5 说明`](../outputs/route_layer/r4_codex_independent_pilot_v5/README.md)、[交互回放](../outputs/route_layer/r4_codex_independent_pilot_v5/replay.html)、[最终审计](../outputs/route_layer/r4_codex_independent_pilot_v5/final_audit.json) 和 [费用复算](../outputs/route_layer/r4_codex_independent_pilot_v5/codex_usage_cost_reconciliation.json)。

## R4 Codex 独立路线 pilot v6：本地硬校验裁决（2026-10-21）

用户授权从 v5 最后一个已接纳节点继续，最多 8 次 Codex CLI 请求、零重试；遇工具调用或非法动作立即停止。v5 第 6 次候选未执行，且动态 schema 与本地检查均确认该动作合法，因此没有把它重放；v6 从 `node_4_7` 开始。零请求预检确认地图/任务仍为 `grid-8x8-seed-0` / `node_0_5 → node_1_2 → node_6_2`，地图语义哈希一致，205 条有向边。

本轮 8/8 动作均通过本地有向边、起点连续性、边界、取送顺序、不重访和剩余路线校验；0 非法/停止动作、0 工具事件、0 重试。第 6 次模型将 `target_node_is_new` 错报为 false，但本地确定性校验确认目标 `node_2_5` 未访问且路线转移合法，因此记录差异后接纳。此后到达 `node_1_4` 并触及请求上限，尚未到达上车点 `node_1_2` 或下车点 `node_6_2`。合并启动 smoke 和前序续跑后的合法前缀为 14 条边、1,400 m、167.083 秒、0.260 kWh 仿真能耗；v6 自身新增 8 条边。该项是部分路线试跑，不是完整路线或正式 R4 对照结果。

为避免自检误报阻断已由程序确认合法的动作，Codex stepwise runner 现要求模型自检字段结构完整，但将本地确定性校验作为唯一的路线状态推进门禁；每个模型自检与本地事实的差异仍逐步写入 JSON、审计及回放。任何本地非法方向、边界、端点顺序、重复节点或不可继续动作仍被拒绝，且不会执行或计入车辆能耗。该口径是独立 Codex pilot 的经验证实现；若用于正式 R4/R4-A/R4-B，必须先将三组共同协议、后端和预算写入方案，不能把 Codex smoke 与历史 DeepSeek 正式组混并。

v6 的 8 次请求为 78,004 输入、0 缓存输入、8,203 输出，共 86,207 token；GPT-5.6 Luna Standard 费率等值为 0.636110 credits / $0.02544440 API 标价。含启动 smoke 和父续跑链共 15 次请求、150,453 输入、12,253 输出，费率等值 1.119855 credits / $0.04479420。费率依据于 2026-10-21 核验：[Codex Standard 费率](https://developers.openai.com/codex/pricing)；[GPT-5.6 Luna API 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。CLI 未报告实际现金账单或订阅额度扣减。

新增回归覆盖本地合法但自检为 false 的动作接纳、v5 未执行矛盾动作的安全续跑、差异审计及回放标注；完整 unittest 139 项通过。产物见 [`v6 说明`](../outputs/route_layer/r4_codex_independent_pilot_v6/README.md)、[交互回放](../outputs/route_layer/r4_codex_independent_pilot_v6/replay.html)、[最终审计](../outputs/route_layer/r4_codex_independent_pilot_v6/final_audit.json) 和 [费用复算](../outputs/route_layer/r4_codex_independent_pilot_v6/codex_usage_cost_reconciliation.json)。

## D5 Codex 正式批次实时审计快照（2026-10-21 09:47 UTC）

PID 95924 仍在运行，45 个计划结果中已观察到 34 个完整结果，涵盖 seed 1–6 的 30 项和 seed 7 的 4 项；未停止或改写运行中批次。34/34 个结果均有完整 token usage，278 次 CLI 调用的 trace usage 与结果 summary 相符，逐条费率复算相符；对应 D0/D1 基线的输入哈希、场景 fingerprint、地图 ID、订单数和车辆数全部一致；`commitment_violations` 共 0。已完成部分累计 5,681,681 输入（缓存输入 2,992,256）、58,298 输出 token，费率等值 16.692193 credits / $0.66768772 API 标价。仍有 11/45 项未完成，数字只代表已完成部分，非全批结果或账户账单。

逐结果复算文件：[`D5 34/45 token 费用`](../outputs/codex_cli_audit_2026-10-21/d5_reconciliation_34_of_45_20261021.json)；[`D5 34/45 配对与完整性检查`](../outputs/codex_cli_audit_2026-10-21/d5_paired_integrity_34_of_45_20261021.json)。


## D5 正式批次终态与十种子配对审计（2026-10-21）

D5 Codex CLI 正式批次的 9 个新增种子（seed 1–9）已完成，批次 manifest 为 `completed`，45/45 项完整。将已归档的 seed 0 五项纳入后，十种子×五配置共 50/50 个 D5 结果；D0、D1、D5 三种方法共 150/150 个结果完整。逐组复核输入文件哈希、场景指纹、地图 ID、订单数及车辆数，50 组全部一致；50 个 D5 结果的 JSONL token usage、调用数均与对应结果 summary 一致，`commitment_violations` 合计为 0。

检查时发现 seed 0 的 C4/F1 D5 文件实际为 `D5_pure_llm_direct_seed_0_replay.json`，此前生成的总索引却指向不存在的无后缀文件，因此它曾被误显示为缺失且配对标记为 false。已按实际归档文件重新生成 `paired_results.csv`，现在 150 行都完整，50 个任务组的同输入标记全部为 true。原始模型结果文件未改动。

50 个 D5 结果共 425 次 Codex CLI 调用，usage 合计：8,810,591 输入 token（其中缓存输入 4,610,816）、89,927 输出 token，总计 8,900,518 token。按 GPT-5.6 Luna Standard Codex 费率（每百万输入/缓存输入/输出 token 分别 5 / 0.5 / 30 credits）折算为 26.002093 credits；按官方 API 标价（$0.20 / $0.02 / $1.20）折算为 $1.04008372。两者都是费率等值，不是用户账户发票金额，也不能推导订阅实际扣额。费率页于 2026-10-21 核对：[Codex 费率](https://developers.openai.com/codex/pricing)；[GPT-5.6 Luna API 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

十种子是对同一组五类冻结场景的重复仿真/模型运行，不是十份独立的纽约需求样本；报告只支持本基准内的配对比较。最终审计与费用文件：[`D5 十种子完整性审计`](../outputs/codex_cli_audit_2026-10-21/d5_final_integrity_50_of_50_20261021.json)、[`D5 十种子 token 与费用复算`](../outputs/codex_cli_audit_2026-10-21/d5_reconciliation_50_of_50_20261021.json)、[`重新生成的成对结果索引`](../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_formal_v1/paired_results.csv)。

路线层 Codex 独立 8×8 R4 试跑按单轮授权最多 8 次、零重试的规则执行，在第 6 次出现模型自检与本地合法性事实矛盾后停止；未执行矛盾动作，任务没有完成。该结果是部分试跑，不能据此宣称路线层正式实验已完成或 R4 可直接启动。D5 调度实验完成不代表路线 R4 pilot 门槛也已通过。

## R4 Codex 独立路线 pilot v7（2026-10-21）

用户再次授权从 v6 合法前缀继续，最多 8 次 Codex CLI 请求、零重试；出现工具调用或非法动作即停止。预检没有发模型请求，确认地图有 64 个节点、205 条有向边，当前 `node_1_4` 到下车点最短还需 7 步，8 次额度足够覆盖当时的最短路线。为避免恢复脚本把 v6 错标为旧版 v3，先修复 experiment ID 版本递增，并用恢复回归测试验证 v6 会生成 v7。之后正式发出 8 次请求。

8/8 动作都通过本地有向边、边界、起点连续性、取送顺序、不重访和停止规则检查；0 非法动作、0 工具调用、0 重试，自检与本地事实 0 差异。模型先从 `node_1_4` 经 `node_1_3` 向东，再经 `node_3_3`、`node_3_2` 返回西行，到达 pickup `node_1_2` 后又向西到 `node_0_2` 和 `node_0_1`。因此路线合法但未送达；触及请求上限时到 dropoff `node_6_2` 的最短剩余距离为 7 步。合并启动 smoke 及前序合法前缀，共 22 边、2,200 米、263.056 秒、模拟能耗 0.4073 kWh，均是部分路线累计值。

本轮 8 次请求共 78,904 输入（缓存 9,600）和 6,514 输出 token，合计 85,418 token。GPT-5.6 Luna Standard 官方费率等值为 0.546740 credits；API 标价等值为 $0.02186960。含 smoke 和 v1–v7 前序在内共 23 次请求、229,357 输入（缓存 9,600）、18,767 输出，等值 1.666595 credits / $0.06666380。缓存 token 是输入的子集；CLI 没有报告账户实际扣额。官方费率来源：[Codex Standard 定价](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API 定价](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

完整回放、原始 JSONL、逐次 prompt/schema、审计和费用复算位于 [`v7 产物目录`](../outputs/route_layer/r4_codex_independent_pilot_v7/)。全量 unittest 140 项通过。该 pilot 证明调用与硬校验链能连续工作，但没有证明完整取送成功；100×100 正式批次仍不准入。当前 100×100 stepwise 输入预算是基于 DeepSeek 小图 smoke 的不确定代理，Codex 8×8 实测每步仍约 9.8k 输入 token，且每步重复地图上下文。正式批次前需冻结三组统一的调用/反馈方式和费用熔断上限，并通过单项完整任务验证。

## R4 Codex 独立路线 pilot v8：完整路线与成本复核（2026-10-21）

用户授权从 v7 的已校验合法前缀继续，最多 8 次新请求，零重试；发生工具调用或非法动作立即停止。Codex CLI 在 7 次请求后到达下车点，故本轮提前停止；7 个动作均通过本地确定性校验，0 工具、0 非法动作、0 重试，模型自检与本地事实无差异。合并启动 smoke 与 v1–v8 父链后，固定 8×8 任务完成 29 条有向边。完整回放、JSONL、schema、最终审计及逐请求费用见 `outputs/route_layer/r4_codex_independent_pilot_v8/`。

同一冻结地图和任务上的离线全图 Dijkstra 路线为 9 条边、900 m、95.208 秒、`energy_v1` 估算 0.164 kWh；Codex 路线为 29 条边、2,900 m、357.014 秒、估算 0.53575 kWh，即时间约为 Dijkstra 参考的 3.75 倍。这证明单项取送链路可以完成并被完整审计，不证明路线质量或跨任务成功率。

v8 新增 7 次请求用量为 68,904 输入（缓存 0）、4,337 输出 token，共 73,241 token；按 GPT-5.6 Luna Standard 费率折算 0.474630 credits，按官方 API token 标价折算 $0.01898520。包括 smoke 与 v1–v8 父链累计 30 次请求、298,261 输入（缓存输入 9,600，包含在输入中）、23,104 输出 token；Standard 费率等值 2.141225 credits，API 标价等值 $0.08564900。Codex CLI 未提供账户实际扣额或现金账单。[Codex 官方价格表](https://developers.openai.com/codex/pricing)列出 GPT-5.6 Luna Standard 每百万输入/缓存输入/输出 token 为 5/0.5/30 credits；[模型 API 价格页](https://developers.openai.com/api/docs/models/gpt-5.6-luna)列出 $0.20/$0.02/$1.20。API 标价不能当作登录 Codex 账号的实际账单。

为复核 100×100 成本，新增离线敏感性文件 `outputs/route_layer/r4_preflight/codex_cli_100x100_stepwise_cost_sensitivity_20261021.json`，未发新模型请求。它用 v5–v8 中 28 次同格式 8×8 请求拟合 prompt+schema 序列化字节与 CLI 输入 token，剔除 v7 的单次输入 token 异常值；斜率为 0.543 token/byte。将窄尺寸区间的拟合外推到 100×100，得到单组约 3.69 亿输入 token 的中心敏感性值，三组 API 标价等值约 $221.57 输入；按每步 300–1,000 输出 token 场景估算，合计约 $240.17–$283.56。该范围不是统计置信区间、账单或支出上限，且未实测 100×100 Codex 输入。当前 runner 每任务最多 32 个动作，而 130 条可达任务中 128 条的最短有向路径已超过 32 步，不能按当前设置完成正式批次。用户已将后续 LLM 实验后端更新为 Codex CLI；历史 DeepSeek 批次保留独立，不与 Codex 试跑合并。正式准入仍需统一三组调用协议、调整并验证动作预算、加入运行时 token/credit 熔断；此前不启动 100×100 十种子批次。

随后补做了“一任务一次完整路线 + 逐边自检 + 本地整条硬校验”候选的精确离线序列化审计，130 个可达任务的 Codex prompt 与 output schema 合计 5,814,814 字节/组（schema 2,525,148 字节），逐边方式对应约 446,197,487 字节/组。基于小图 Codex 输入拟合外推，候选中心输入约 411.6 万 token/组，逐边约 3.69 亿，输入代理约低 89.7 倍；候选完整路线输出和反馈 token 未测。schema 字节已实测，100×100 tokenization/完整输出/账户费用仍未知。此候选会改变交互方式，尚未获准作为正式协议或发起模型请求。审计脚本 `scripts/audit_r4_codex_full_route_schema_sizes.py`，产物 `outputs/route_layer/r4_preflight/codex_cli_full_route_schema_size_audit_20261021.json` 与 `codex_cli_100x100_full_route_cost_sensitivity_20261021.md`。

## 后续复核：独立 R4 Codex pilot v9（2026-10-22）

独立 8×8 任务用完授权的 8 次请求，8/8 有向移动通过本地硬校验，0 工具调用、0 非法动作、0 重试、0 自检差异；但没到 pickup `node_1_2` 或 dropoff `node_2_3`，最终在 `node_5_5`，剩余完成下界为 9 步。已接纳前缀为 800 米/112.083 秒/0.1493 kWh。Token 为 77,722 输入（缓存 0）+8,117 输出，GPT-5.6 Luna Standard 等值 0.632120 credits、API 标价等值 $0.02528480；账户实际账单未知。此前回放器因强制要求前置 smoke 而无法渲染独立任务，费用汇总也将另一条 smoke 混入累计值；现已修复，回放从原始任务起点显示并只累计本轮用量。

本轮执行编排没有触发预设的剩余授权预算早停，所以在已无法于本轮调用额度内完成时仍用完 8 次请求。随后已在 stepwise runner 加入请求前/合法移动后的最短路线与剩余授权调用比较；不足时本地停止并记录原因，预算不足的安全前缀可以在获得新授权后续跑。完整细节见 [v9 试跑报告](r4-codex-independent-pilot-v9-2026-10-22.md)。路线层正式 R4 仍未准入。
