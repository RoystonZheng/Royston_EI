# 路线层 R4 续跑检查点

日期：2026-10-24  
状态：正式批次尚未开始；本文件记录输入复核与续跑门槛，不是路线效果结果。

## 本次离线复核

重新运行 `scripts/preflight_r4_codex_all_frozen_tasks.py`，结果保存于 `outputs/route_layer/r4_codex_all_frozen_tasks_recheck_20261024/`。本次模型请求数为 0，预检错误数为 0。

- 冻结样本共 150 项：100 个订单任务、50 个正式场景，覆盖 seed 0–9。
- 传统组和纯 LLM 组的十个种子地图、订单文件均逐字节一致；正式场景去掉重复嵌入的地图字段后内容一致。
- 129 项通过端点、来源完整性和有向链路可达性门禁；10 项为预设不可达场景；10 项为非法端点；1 项语义解析失败。所有项目保留在 150 项分母中。
- 当前“一次请求提议一个方向”的协议至少需要 17,072 次模型请求完成 129 项可路由任务。该下界不包括非法提议后的反馈请求，也不是 token、费用或账户额度估计。

## 当前路线状态

- 独立的正式格式检查点 `r4_codex_formal_checkpoint_v1` 曾对 seed 0 的 `task-0000-0009` 发出 1 次请求；模型有响应，但未构造出路线（`unable_to_construct_route`）。离线有向图可达性检查表明该任务可达，因此这次失败属于路线构造失败，不是 API 无响应。
- 分步 task9 试跑 `r4_codex_stepwise_formal_task9_pilot_v1` 发出 1 次请求，接受了 `node_69_41 → node_68_41` 这一个合法方向，随后因额度停止线暂停。合法前缀为 100 米、10 秒、估算 0.018 kWh；完整路线尚未完成，因此完整路线电耗未评估。
- 没有发现仍在运行的 R4/Codex 路线进程。屏幕锁定不会替代或自动启动该进程。
- 已修复 task9 回放的汇总栏：旧渲染器只读取 `cumulative_*` 字段，而当前审计将用量存于 `usage`、标价等值存于单次请求记录，导致汇总栏误显示 0。重生成后显示 15,999 输入、1,028 输出 token 和 $0.004433 标价等值；不是 Codex 账单。全套回归测试 206 项通过。
- 对 task-0000-0009 完成三组输入配对核查：传统组与纯 LLM v8 的 seed 0 地图/订单 SHA-256 一致，R4 使用同一公开语义输入与起点。端点差异来自预设的语义解析能力差异：传统 graph-only 回退到 `node_68_41`（上车点真值不匹配），R4 Qwen 由路牌西侧解析到真值 `node_25_66`。R4 已通过的首步到 `node_68_41` 是途经点，不能误判为完成上车；整条 R4 路线仍未完成。详见 `docs/route-layer-semantic-pair-audit-task-0000-0009-2026-10-24.md`。
- 补齐通用 seed 结果页的模型用量详情：显示请求数、输入/输出 token、模型响应耗时、本地 Qwen 解析耗时，以及标注为“估算，非账单”的 API 标价等值和账户账单状态。已重生成 `outputs/route_layer/r4_codex_formal_checkpoint_v1/seeds/seed_0/index.html`；其中 `task-0000-0009` 保留 15,567 输入、110 输出 token、7,146.893667 ms 响应耗时、$0.0032454 标价等值，Codex CLI 账单状态为未报告。路线仍为模型未构造出完整路线，完整路线能耗未评估。定向测试及全量 207 项测试通过；没有发起新模型请求。
- 为全部冻结样本生成逐任务离线输入：`scripts/prepare_r4_codex_batch_preflights.py` 生成 129 份可路由任务预检和 21 份跳过分类，输出在 `outputs/route_layer/r4_codex_batch_preflights_20261024_v1/`。每份输入通过地图紧凑表示等价性核查；原地图以路径和 SHA-256 引用，不重复嵌入；评估真值独立存放且不进入 prompt。任务输入可由分步执行器读取，封闭边列表会传入本地路网构造。跨文件审计确认 129 份地图引用均匹配、无真值泄漏、无模型请求。批次理论最少 17,072 次单方向请求；这只是待运行任务输入，不是模型实验结果。新增专项测试 4 项通过，全量 unittest 211 项通过。
- 新增正式批次调度器 `scripts/run_r4_codex_formal_batch.py` 和按 seed 展开的索引 `outputs/route_layer/r4_codex_formal_batch_v1/index.html`。默认只做离线预检；执行需要显式请求上限与额度停止线。运行器给每项待跑任务预留最短路径请求下界，再平均分配额外请求；逐请求写检查点、零重试，无法确认请求结果时停止且不重发。索引包含 150 项总分母、129 项可路由任务、21 项跳过项和原地图链接。11:23 UTC 周用量 20% 高于原 13% 停止线，批次在请求前停止；当时请求数 0、未使用 reset。批次程序只接收启动时额度快照和请求上限，无法在本地 CLI 运行过程中直接轮询 Codex app 额度接口。
- 2026-10-24 11:36 UTC 续接：用户要求屏幕锁定后继续。复核没有活跃 R4/Codex 进程；macOS `caffeinate` 可用于后续获准的运行，但不会自动恢复已暂停的批次。账户周用量再次读为 20%，原 13% 停止线未改变；以新门禁真实运行批次调度器，结果 `stopped_before_request_usage_at_or_above_stop_line`、请求数 0、Full reset 未使用。对 150 项冻结输入重新运行零请求批次预检，得到 129 项可路由、21 项跳过、最低 17,072 次方向请求。
- 为支持外层逐请求核额，调度器新增 `--max-new-requests-per-invocation`，并将额度基线达到停止线的情形改为请求前停止。设置为 1 时，每次最多完成一个模型响应并保存续跑检查点；账户快照需由外层刷新后传入下一次运行，调度器本身仍不能读取 Codex app 额度接口。新增检查覆盖停止线相等边界、单请求暂停及同一任务恢复；全量 `unittest discover -s tests` 219 项通过。本轮没有模型请求；正式路线实验仍未完成。
- 2026-10-24 11:40 UTC 再核额度仍为 20%。修复了批次在停止线处返回时 `updated_at_utc` 为空的问题，门禁账本现在保存 `checked_at_utc` 和更新时间；新测试先按预期失败，修复后批次专项 7 项及全量 219 项均通过。再次执行单请求切片门禁，账本仍为 `stopped_before_request_usage_at_or_above_stop_line`、请求数 0，检查时间为 `2026-10-24T11:40:58.791388+00:00`。无活跃进程、未使用 reset、无模型调用。

## 续跑门槛

2026-10-24 10:41:57 UTC 的只读账户额度快照为周用量 18%；11:02:40 UTC 为 19%，11:23:56 UTC、11:36:44 UTC 和 11:40:56 UTC 均为 20%。已记录的临时停止线是 13%。用量快照是共享且取整的，无法归因百分点变化；本次续接没有发出模型请求。账户仍有 1 次 Full reset 可用，本次未使用；没有 R4 路线进程在运行。

在用户明确授权新停止线或使用一次性 reset 前，不发送后续路线请求。后续如恢复，继续使用当前冻结任务、逐步协议、零重试和外置确定性 validator；非法提议不进入已执行路线。除非用户确认方法变更，仍保持一次请求提议一个方向。

## 证据边界

- 复核报告：`outputs/route_layer/r4_codex_all_frozen_tasks_recheck_20261024/summary.json`
- 可视化索引：`outputs/route_layer/r4_codex_all_frozen_tasks_recheck_20261024/index.html`
- 分步 task9 状态：`outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1/state.json`
- 分步 task9 回放：`outputs/route_layer/r4_codex_stepwise_formal_task9_pilot_v1/replay.html`
- 全批次逐任务离线预检清单：`outputs/route_layer/r4_codex_batch_preflights_20261024_v1/manifest.json`
- R4 Codex 批次进度和可视化索引：`outputs/route_layer/r4_codex_formal_batch_v1/manifest.json`、`outputs/route_layer/r4_codex_formal_batch_v1/index.html`
- 以上输入一致性和可达性核查只证明冻结输入及离线门禁状态，不证明模型能完成路线或改善路线质量。

## 2026-10-25 拒绝动作能耗与回放修复

终审器现对可解析但被本地确定性校验拒绝的单步提议，分开记录“实际耗电 0”和“若执行的单步假设能耗”。估算优先查反向道路属性，缺失时用地图中位路段作代理；没有方向/目标的模型错误不推造能耗。回放不再要求候选动作具有合法 edge ID 才能显示，非法单行道逆向提议会用红色虚线标出，并在该步说明本地拒绝原因与估算依据。假设能耗不进入已执行路线累计值。

验证：finalizer/replay 专项测试14项通过；Chromium 检查合成逆向提议时虚线、拒绝原因、假设能耗及反向路属性说明均可见，控制台无 JS 错误；全量 unittest 227项通过（99.849秒）。真实 task9 已重新生成终审与回放，仍为1次请求、1个合法动作、`partial_legal_prefix_not_complete`，已接纳前缀0.018 kWh，整条路线未完成。本次没有模型请求。只读额度周用量24%，高于13%停止线；批次请求数0，故不具备继续模型试跑条件。


- 2026-10-24 11:49 UTC 续接：新增正式批次启动前的完整试跑门禁，校验 task9 的冻结地图/任务/端点、完整路线状态、非法动作/重试/工具事件、usage、路线指标及额度守卫。现有 task9 审计仅完成 `node_69_41 → node_68_41` 一个合法动作，状态 `partial_route_incomplete`；全批执行器据此在任务子进程启动前返回 `stopped_before_request_pilot_incomplete`，正式批次模型请求数为0。读取到账户周用量20%，仍高于13%停止线，Full reset未使用。无活动 R4 路线进程。门禁专项测试及全量 `python3 -m unittest discover -s tests` 共221项通过；该门禁回放、材料和测试不构成新路线结果。

- 2026-10-25 00:26 UTC 续接：周用量快照升至23%，仍高于13%停止线；额度接口显示下一重置为2026-10-31 04:26:32 UTC，1个Full reset可用但未使用。未发现R4路线进程。新增零请求续跑预检 `outputs/route_layer/r4_codex_stepwise_formal_task9_resume_preflight_20261025_v1/`，从已接受的 `node_69_41 → node_68_41` 前缀继续，不重放该步；当前位置 `node_68_41`、合法方向N/S、最短剩余108步、候选请求上限127，10,853条有向边语义等价通过。该预检沿用逐步单方向、零重试和原本地validator，但额度门禁未通过，不能启动模型请求；本次模型请求0。共享额度变化不归因于路线任务。

- 2026-10-25 00:36 UTC 续接：只读周用量为24%，高于13%停止线；1个Full reset仍可用、未使用。本地终审器对真实 task9 目录副本生成 v3 格式后，新的批次门禁正确识别 `partial_legal_prefix_not_complete`，拒绝不完整路线，并报告 `ephemeral_per_request` 与正式 `persistent_per_task` 的会话差异；节点元数据正确保留为 `node_69_41`、`node_25_66`、`node_42_43`。正式批次请求数仍为0，状态 `stopped_before_request_pilot_gate_failed`。已准备从冻结起点开始的 persistent 模式零请求预检。门禁专项13项通过，全量 `python3 -m unittest discover -s tests -q` 共225项通过；本次无模型请求、无活跃路线进程。

- 2026-10-25 00:43 UTC 续接：复核旧的 `r4_codex_formal_checkpoint_v1`（1次无效整路线响应）与 task9 的单个合法前缀为不同实验记录，均未作为完整路线试跑。正式批次冻结150项（129项可路由）、方向请求下界17,072，账本仍为 `stopped_before_request_pilot_gate_failed`、请求数0。另生成 `outputs/route_layer/r4_codex_formal_batch_preflight_20261025_v1/`，零请求预检状态 `offline_batch_preflight_ready_no_model_requests`，其冻结来源哈希与现有清单一致；索引129个地图链接全部存在。持久会话预检保持 ready；通过 `caffeinate -i` 运行适配器与续跑的3项离线单测（会话复用、状态增量、工作目录拒绝、检查点续接），没有模型调用。额度快照仍为24%，高于13%停止线，Full reset未使用，无活动R4进程。当前只完成离线准备；屏幕锁定本身不会启动模型任务，获准运行时可由 `caffeinate -i` 保持本地进程不因闲置睡眠中断，并逐请求复核额度。

- 2026-10-25 约00:45 UTC 输入可比性审计：R4零请求预检的150个任务ID与传统组100个订单+50个正式场景完全一致；10张地图、10份订单源文件和10份场景源文件SHA-256逐项一致。129份可路由预检全部通过有向边/成本语义核验，评估真值未放入提示；21个未路由样本保留原因（10非法端点、10确定不可达、1意图解析未构建）。审计 JSON：`outputs/route_layer/r4_codex_formal_batch_preflight_20261025_v1/input_equivalence_audit.json`。该证据仅证明输入冻结与离线准入，不是路线结果；没有发模型请求。


## 2026-10-25 候选请求上限与授权状态

复核持久会话预检时发现旧 v1 `manifest.json` 将128次候选请求上限写在 `authorization` 字段。为防止把安全上限误当作模型调用授权，生成器现输出 schema v3，明确 `user_authorization_status=not_granted`，并将该值标为候选安全上限；`--request-limit` 帮助文本也明确不授予用户授权。新清单位于 `outputs/route_layer/r4_codex_stepwise_formal_task9_persistent_preflight_20261025_v2/`，模式 `persistent_per_task`、冻结地图语义校验通过、模型请求0；旧 v1 清单已标记为被替代。12项清单专项测试通过；全量227项测试通过（95.445秒）。该128次上限仍是候选配置，不是已获授权的请求数。账户周用量24%，高于13%停止线。


## task9 续跑预检元数据更正

另一份旧 task9 续跑预检 `outputs/route_layer/r4_codex_stepwise_formal_task9_resume_preflight_20261025_v1/manifest.json` 也曾把127次候选上限标为授权；现已改为 schema v3 的 `candidate_request_limit`，明确 `user_authorization_status=not_granted`，模型请求为0。该预检沿用旧 ephemeral 前缀，会话模式为 `ephemeral_per_request`，不能充当完整的 `persistent_per_task` task9 试跑或通过正式批次门禁。已执行的历史试跑清单未改写。

## 2026-10-25 屏幕锁定期间的续跑规则

用户再次要求屏幕可能锁定时继续实验。屏幕锁定本身不会终止已启动进程；获准运行 task9 时可使用 `caffeinate -i` 防止系统因闲置睡眠中断，同时允许显示器锁定或休眠。它不会恢复未启动的实验，也不绕过额度和试跑门禁。

01:37 UTC 只读额度快照仍为周用量 24%，高于既有 13% 停止线；下一周期重置时间为 2026-10-31 04:26:32 UTC。当前没有活跃 R4 路线进程，正式批次仍为 0 次请求。task9 的真实历史试跑仅完成 1 个合法方向；持久会话预检从冻结起点开始、最低 109 步、候选请求上限 128，但未获调用授权。现阶段继续完成离线审计和材料；达到停止线以下并满足完整试跑准入前不发送请求、不使用 Full reset。

## 2026-10-25 持久会话调用用量复核

已有8×8持久会话试跑 `outputs/route_layer/r4_codex_persistent_session_8x8_pilot_v1/` 的2次合法方向提议共用30,316输入、2,350输出、32,666总tokens；第1步为9,690/1,121，第2步为20,626/1,229，cached input为0。历史价卡等值为 API 0.0088832 USD、Codex Standard credit 0.22208；实际账户账单未报告。后续请求虽只传状态增量，实际输入 usage 仍上升；不能将 prompt 字节减少当作 token 节省。该样本只能说明持久会话复用不保证省 token，不能准确外推到100×100 task9。task9最短需109步，现有持久模式零请求预检的128仅是候选安全上限。正式task9持久会话试跑仍未开始；额度超出停止线时必须保存部分路线并停，不承诺完整路线必能在单个周期内完成。
