# R4 不重访规则对齐与单任务预检（2026-10-23）

## 已确认的规则

研究记忆库已记录 2026-10-20 用户确认：正式路线不得重复访问节点。该规则适用于完整行程 `start→pickup→dropoff`，两段之间也不能重复经过 pickup 以外的节点。它不是待确认事项。路线方法可以提出候选，但同一个本地确定性 validator 决定路线是否有效。

## 传统结果的影响

只读审计了传统实验 10 个种子、100 个订单与 50 个正式场景，共 450 条算法任务记录：390 条输出路线、60 条无路线结果。160/390 条原标为 `valid` 的路线跨接驾/载客两段重复访问节点，至少影响 54 个任务。保留了源文件和原始路线；这 160 条不得计入共用不重访口径下的有效路线、路线能耗和路线费用。它们是违规提议，不代表任务不存在替代路线。逐段单独检查均无内部重访。

可视化与逐记录链接：`outputs/route_layer/traditional_no_revisit_audit_20261023_v2/index.html`；机器可读记录与源文件哈希在同目录 `summary.json`。复现脚本：`scripts/audit_traditional_route_no_revisit.py`。

## R4 全量冻结任务预检

对 10 个种子的 150 项冻结任务复核了整路线一次请求候选，不发送模型请求。129 条分段有向可达任务全部通过紧凑地图逐边/成本等价、端点和评估真值隔离门禁；29 条语义任务携带通过来源核验的 `RoadFeatureInputProtocol v1` 上下文。候选假设 129 次请求，相对当前逐步方法最低 17,072 次请求少 99.24%，但这是请求量假设，不是模型单次生成能力或成功率证据。提示中位 21,950 字节、最大 44,629 字节；不得把字节数换算成 token 或费用。

传统 `local_dijkstra` 拼接参考路线中 73 条为全程不重访，可作为满足该约束的存在见证；另 56 条发生跨段重访，尚未搜索替代路线。故 129 个分段可达任务不能都称为已证明满足不重访约束的可行任务。新审计结果：`outputs/route_layer/r4_codex_full_route_candidate_preflight_20261023_v5/index.html` 与 `summary.json`。历史 v4 预检也保留。

## 一个语义路线 smoke 的请求前预检

为避免无依据地启动十种子批次，离线准备了一个道路特征语义任务：seed 8 `task-0008-0003`，`node_81_53 → node_49_66 → node_19_69`。任务地图和订单文件与纯 LLM 冻结输入 SHA-256 一致；Qwen 意图来源及公开字段匹配；RoadFeatureInputProtocol v1 上下文在提示中；紧凑有向图 11,095 条边及成本等价检查通过；本地 `local_dijkstra` 参考长度 80 边、全程不重访，参考路径本身未进入模型提示。提示 18,229 字节、schema 738 字节、输出最多 512 边。模型请求为 0、重试为 0、无传统算法 fallback。

请求前页面及输入：`outputs/route_layer/r4_codex_semantic_full_route_pilot_preflight_20261023_v1/index.html`，同目录 `prompt.json`、`schema.json`、`preflight.json`。它只是准备完成的 smoke 候选，未验证模型行为、费用、整路线成功率或性能。

## 结论与下一门槛

旧候选文档中“发请求前须决定是否允许重访”的待定表述由本记录更新：用户已确认不重访为硬规则，继续沿用，不能放开。传统原始结果不改写；评估时共同应用全程不重访 validator。若传统路线不通过，保留为违规提议；是否重跑传统基线以主动搜索其他合法路线属于另一项方法选择，不能和事后重新标注混为一谈。

整路线候选尚未经过模型验证，正式十种子 R4 未启动。下一门槛为获得授权后，对上述冻结语义任务进行最多 1 次 Codex CLI 请求，零重试；完整路线由本地确定性 validator 检查有向边、边界、端点顺序及全程不重访，不使用 Dijkstra/A* fallback。无论模型返回有效或失败，都保存原始响应、轨迹和用量；一次 smoke 不构成正式实验或费用预算证明。

## 2026-10-23 本地 validator 离线验收补充

更新后的预检版本 v3 将同一任务的 80 边 Dijkstra 参考节点路径转换成方向串，送入正式整路线 validator 验收，结果 `accepted=true`、0 项违规；参考路线序列仍未进入模型提示。另加回归测试，在 pickup 之后回到此前访问过的 start 节点，validator 于第 2 步以 `node_revisited` 拒绝。

可视化及结构化结果现以 `outputs/route_layer/r4_codex_semantic_full_route_pilot_preflight_20261023_v3/` 为最新版本；v1 保留历史。专项 10 项测试通过；随后全量 192 项测试通过。模型请求仍为 0；这只验证本地合法/违规判定链，不证明模型能生成完整路线。

## 单次执行 runner 已准备（默认离线）

新增 `scripts/run_r4_codex_semantic_full_route_pilot.py`：默认执行最新预检重建与 hash/schema 新鲜度核验，不构造 Codex 客户端，也不发模型请求。只有显式加入 `--send-one-request` 才会尝试单次调用；调用前先保留一次请求名额，零重试/零传统算法 fallback，已有输出目录拒绝再次运行。执行结果会保存原始 JSON/JSONL 与 usage、整路线 validator、完成路线电耗或违规提议能耗审计，并调用传统组同一轨迹渲染器输出 seed 原地图/结果总览、原地图预览和独立 HTML 回放。CLI 环境已检测到 `codex-cli 0.145.0`，此检查没有发模型请求。

离线默认路径已实跑并显示 `offline_ready_no_model_request_sent`；测试用的假响应贯通执行、验证、归档和 HTML 渲染，输出只在临时目录创建后清理，并未保留成实验结果。四项 runner 专项测试覆盖默认零调用、合法路线评价、跨段重访拒绝/提议耗电记录和假响应回放；全量 192 项测试通过。它们不验证真实模型行为或真实账单。

执行命令（需先获得该次模型请求授权）：

```bash
python3 scripts/run_r4_codex_semantic_full_route_pilot.py --send-one-request
```

无 flag 的命令仅执行离线核验。执行前输出目录 `outputs/route_layer/r4_codex_semantic_full_route_pilot_v1/` 不存在；正式请求仍未发送。

## 同一失败订单的新版请求前检查（task 0000-0009）

回查当前打开的 `r4_codex_formal_checkpoint_v1`：seed 0 的 `task-0000-0009` 已消耗 1 次 Codex CLI 请求，返回 `status=failed`、`unable_to_construct_route` 和空移动串；不是 API 错误，也没有路线动作可供判为道路违规。usage 为 15,567 输入、110 输出 token，约 7.147 秒；0 重试。CLI 未提供账户实际扣款，路线电耗为未评估（null），不能当作 0。原检查点保持不变。

针对同一地图、同一冻结订单、同一 Qwen RoadFeatureProtocol v1 解析端点，离线构建新版整路线候选：`node_69_41 → node_25_66 → node_42_43`。旧请求的标准分段最短路拼接会跨段重访；这不能证明无合法路线。新预检在模型外构造一个可行性见证：第一段避免提前经过终点，第二段绕开第一段已访问节点，得到 111 边的全程不重访路径；该见证经 R4 确定性 validator 验收，0 项违规。见证路线没有放入提示，不是模型回退，也不代表全局最优。

新候选仍使用冻结地图/订单，提示加入可核验的道路特征上下文；紧凑图 10,853 条有向边与原输入逐边及成本等价，评估真值未进入提示。提示 17,933 字节、schema 738 字节、路线动作上限 512；这些字节数不代表 token。预检没有调用模型，输出在 `outputs/route_layer/r4_codex_semantic_full_route_pilot_preflight_20261023_task9_v1/`。专项测试 5 项通过，完整 `unittest` 193 项通过。

**当前门槛：**候选尚未验证模型能否一次生成路线；十种子 R4 仍未启动。若用户授权，只对这个相同 task 再发 1 次 Codex CLI 请求，零重试、禁用工具、整路线由本地 validator 复核；失败后停止，不追加请求。新的请求使用命令：

```bash
python3 scripts/run_r4_codex_semantic_full_route_pilot.py \
  --preflight-dir outputs/route_layer/r4_codex_semantic_full_route_pilot_preflight_20261023_task9_v1 \
  --output-dir outputs/route_layer/r4_codex_semantic_full_route_pilot_task9_v1 \
  --send-one-request
```

## 十种子冻结任务的全程不重访可行性审计

进一步对整路线候选 v5 中 129 条分段有向可达任务逐项构造模型外的可行性见证。对每条任务以同一地图、同一端点及正式场景封闭边为输入，先搜索一条不提前经过 dropoff 的 `start→pickup` 最短路，再从 `pickup→dropoff` 搜索一条避开第一段已访问节点的最短路。将拼接所得全路径交给 R4 共用确定性 validator。

结果为 129/129 找到且通过校验的不重访见证，0 条出现 validator 拒绝，最长 370 边，低于 512 边上限。原先 56 条 local Dijkstra 拼接参考路线含跨段重访的任务也全部找到替代见证。这证明 129 个任务各自存在符合全局不重访规则的路线，不证明这些见证是全局最优，也不表示模型已能生成它们；见证节点序列未写入任何模型提示。其余 21 个任务仍按冻结门禁分别保留为非法端点、预设不可达或语义意图解析失败，没有发路线请求。

逐任务节点序列、参考路线重复情况及共用校验结果：`outputs/route_layer/r4_no_revisit_feasibility_audit_20261023_v1/index.html` 和 `summary.json`。复现脚本 `scripts/audit_r4_no_revisit_feasibility.py`；任务 0000-0009 的替代见证有专项测试。模型请求 0，传统原始输出未改，行驶能耗未在此可行性审计中计算。

这项结果解除“129 条中是否确有不重访路线”的离线可行性不确定性，但不解除模型请求授权门槛，也不启动正式批次。该段当时提出的“发 1 次新版整路线 Codex smoke”现由文末“方法对齐更正”取代：正式 R4 下一门槛是先冻结预算，再按逐步协议进行有界试跑。历史整路线 smoke 候选仍保留，但不属于当前正式方法。

## 2026-10-23 方法对齐更正

上段提出的“再发 1 次新版整路线 smoke”与当前正式 R4 定义不一致，现予更新。实验方案第 10.3 节将 R4 固定为逐步路线生成：每次 Codex 请求只提议一个方向，本地确定性校验通过后推进权威状态；不能用一次整路线请求替代。

用户当前打开的 `outputs/route_layer/r4_codex_formal_checkpoint_v1/` 已记录一次真实整路线请求，结果为 `unable_to_construct_route` 和空移动串。它保留为不同调用协议下的失败检查点，不是逐步方法的首步尝试，也不触发自动重试。`r4_codex_semantic_full_route_pilot_preflight_20261023_task9_v1/` 与对应 runner 仍可作为探索性方法变体，但未经方法变更确认，不作为正式 R4 下一门槛或正式证据。

正式逐步协议的零请求预检位于 `outputs/route_layer/r4_codex_stepwise_100x100_preflight_v2/`：11 项本地门禁通过，seed 0 `task-0000-0009` 的最短动作下界 109，已知不重访合法见证 111 步。单次请求只覆盖首个动作。全量 129 条可达任务的模型外不重访见证审计已通过，但正式逐步协议仍至少需要 17,072 次路线请求，拒绝动作还会增加请求；完整 100×100 逐步 Codex usage 与费用/时间熔断尚未冻结。因此下一道门槛是先冻结单任务试跑的请求数、运行时间和费用上限，再按逐步协议运行；预算耗尽而未到终点只能报告为部分结果。当前不发送模型请求、不启动十种子批次。

本更正按路线层实验方案 10.3 节、stepwise 零请求预检、已存档的 seed 0 Codex 响应/usage 和 129 项不重访离线审计形成；无新增模型请求或费用。

## 100×100 逐步试跑入口已准备

已扩展 `scripts/run_r4_codex_pilot_continuation.py`，可直接从冻结 stepwise 预检创建独立任务状态。请求前输出目录 `outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1/` 已生成：seed 0 `task-0000-0009`、当前节点 `node_69_41`、上车 `node_25_66`、下车 `node_42_43`，动作上限512、候选 API 请求上限128、最短动作下界109。RoadFeatureInputProtocol v1 上下文会随每步消息传递；每步方向由动态有向边 schema 限定，模型动作由确定性本地校验后才推进。零重试；工具调用、非法/无效动作、模型放弃或请求错误会停止并保留记录，无传统路线兜底。

`replay.html` 已按传统 pilot 回放器样式生成，当前显示0次请求、0步动作和待运行状态；每次逐步响应成功归档后会自动刷新。补充修复了零动作预检状态缺少路线成本字段导致回放器报错的问题。测试使用冻结真实预检输入和假模型响应，不调用模型。

128次是待授权上限，不代表已授权或已确认费用预算。该任务至少需要109个动作请求；已知合法见证为111步。如果超过请求上限或遇到任何无效提议，结果标为未完成/失败且不接纳违规动作。实际 token、订阅额度与账户费用均未知；本次模型请求为0。全量回归198项通过（含本地单步假响应与HTML回放），Python编译检查通过。

只有在用户授权这一个冻结任务后才会启动 Codex 请求；授权范围为最多128次逐步请求、零重试、零传统算法 fallback，终止于完成、首次无效动作、调用错误或预算耗尽。正式十种子实验仍未启动。
