# R4 Codex 批次运行器

日期：2026-10-24  
状态：批次执行器已离线验收；正式模型运行尚未启动。

## 运行方式

`scripts/run_r4_codex_formal_batch.py --preflight-only` 只创建批次账本与 HTML 索引，不调用模型。索引按 seed 展开 129 个可路由任务，并列出 21 个冻结分母中的跳过项；任务开始后，索引链接原地图、逐方向回放和终审 JSON。

正式运行必须显式传入 `--execute`、累计 `--request-limit`、运行前周用量快照 `--usage-baseline-percent` 和 `--usage-stop-percent`。额度基线达到或超过停止线时，运行器会在请求前停止。累计请求上限低于尚未完成任务的最短步数之和时，也会在请求前停止。129 项初始最少为 17,072 次方向请求；地图、端点、评估真值和方向约束均沿用已冻结预检。

若要在每个模型响应后核对 Codex 共享额度，可加 `--max-new-requests-per-invocation 1`。运行器完成一个响应、保存该步审计并返回续跑检查点；外层应读取新的 Codex 额度快照，再将快照作为下一次执行的 `--usage-baseline-percent`。批次脚本本身没有账户额度 MCP 接口，不能在一个长进程内自动读取额度。停止线为 `13` 时，下一次执行的快照到达 `13` 就会停止；用量百分比是共享账户的取整值，不能单独归因到路线请求。

在 macOS 上手动启动长批次时，可用 `caffeinate -dimsu python3 scripts/run_r4_codex_formal_batch.py ...` 阻止系统进入睡眠；屏幕锁定本身不会停止已运行的进程。当前没有活跃批次进程，因此屏幕锁定不会让已暂停的实验自动恢复。

每个任务单独保存状态、CLI trace、合法动作、拒绝动作、仿真能耗、费用估算和回放。请求预算先为所有待跑任务预留最短路线下界，再把额外额度平均分给待跑任务，每个任务单次 continuation 最多 512 次请求。运行器使用每任务一个持久 Codex CLI 会话、每一步一个动态方向 schema、外部确定性 validator 和零重试；验证失败的动作不会前进到车辆轨迹。任务路线失败会单独记入结果，不作传统算法回退。

批次账本在每个请求前写入 `in_flight` 检查点，每个调用完成后立即对账。进程重启时，如果子任务状态表明请求尚在处理中或结果不确定，运行器会停止并标记 `outcome_unknown`，不会重发该步。路线因单任务请求上限暂停时，获新额度后可以从合法前缀建立新 continuation；父子审计与 token 记录保持链接。

CLI 进程本身不能读取 Codex app 的共享周额度接口，因此批次程序只检查本次启动提供的快照和累计请求硬上限；长批次期间若要严格执行动态周用量停止线，需要由外层任务在每次请求间刷新额度快照。2026-10-24 11:40:56 UTC 只读快照为 20%，原停止线为 13%，本次真实批次门禁以 `stopped_before_request_usage_at_or_above_stop_line` 停止；请求数为 0，Full reset 未使用。manifest 写入门禁检查时间 `2026-10-24T11:40:58.791388+00:00`。调度器增加了单次执行请求数上限，供外层逐请求核额。

## 当前产物

- 批次索引：`outputs/route_layer/r4_codex_formal_batch_v1/index.html`
- 批次账本：`outputs/route_layer/r4_codex_formal_batch_v1/manifest.json`
- 129 项逐任务预检：`outputs/route_layer/r4_codex_batch_preflights_20261024_v1/`
- 最低请求数与冻结输入复核：`outputs/route_layer/r4_codex_all_frozen_tasks_recheck_20261024/summary.json`

批次索引当前状态为 `stopped_before_request_usage_at_or_above_stop_line`、请求数 0。它是运行前准备结果，不是路线实验结果。


### 2026-10-24 11:49 UTC：完整路线试跑准入门禁

批次启动前新增 task9 试跑审计门禁。门禁要求试跑来自冻结地图与订单、起终点一致、整条路线完成且没有已接纳非法动作/重试/工具调用，并具备 token、时延、距离、时间、能耗及额度前后记录。当前 `r4_codex_stepwise_formal_task9_pilot_v1/final_audit.json` 状态为 `partial_route_incomplete`，仅有 1 次请求和 1 个合法方向，故批次账本已更新为 `stopped_before_request_pilot_incomplete`；正式批次请求数仍为 0。

同次只读额度快照为周用量 20%，既有停止线为 13%；没有使用 Full reset。task9 现有审计的 0.018 kWh 只对应 100 米合法前缀，不是完整路线电耗。当前无活动 R4 路线进程，屏幕锁定不会自动恢复一个未启动的批次。索引与账本：`outputs/route_layer/r4_codex_formal_batch_v1/index.html`、`manifest.json`。新增门禁专项测试及全量 `python3 -m unittest discover -s tests` 共221项通过；测试未调用模型。

### 2026-10-25 00:36 UTC：会话口径与终审格式门禁修复

复核发现试跑使用 `ephemeral_per_request`，而正式批次指定 `persistent_per_task`，两者 usage 不能直接作同口径成本验证；门禁现检查并锁定试跑 `manifest.json` 哈希。另将正式续跑终审器实际生成的 `r4_codex_pilot_final_audit_v3` 纳入识别，并要求明确记录已到达上车点、下车点、路线连续性和无重访；v3 的起点/上车点/下车点也会正确显示在门禁记录中。用真实试跑目录的临时副本重新运行终审器，生成 v3 审计后门禁按预期拒绝：会话模式不匹配且路线仍未完成；只保留一个合法前缀动作，0.018 kWh 是前缀估算。

本轮针对性门禁测试 13 项通过，包含 v3 格式、上车点必达、终点、会话模式及证据哈希变更检查；全量 `python3 -m unittest discover -s tests -q` 共225项通过。本轮未发模型请求。只读 Codex 额度快照为周用量 24%，高于既有 13% 停止线；Full reset 未使用。正式批次账本仍为请求数 0，状态 `stopped_before_request_pilot_gate_failed`，没有活跃路线进程。新生成的 `r4_codex_stepwise_formal_task9_persistent_preflight_20261025_v1/` 是从冻结起点开始、与正式批次会话模式一致的零请求预检，不是完整试跑或额度授权。

### 2026-10-25 00:43 UTC：持久会话的离线续跑验证

另外用 `caffeinate -i` 跑过持久 Codex 会话复用、后续请求只发送当前状态、非空会话工作目录前置拦截、以及单步检查点续接测试，3项通过、无模型请求。新增全批次离线预检 `outputs/route_layer/r4_codex_formal_batch_preflight_20261025_v1/`，保留150项分母、129项可路由、21项跳过和17,072次最低请求数；来源清单哈希与既有批次一致，索引地图链接129/129存在。旧 `r4_codex_formal_checkpoint_v1` 的无效整路线响应与 task9 合法前缀分开保留。正式批次仍需一条完整且采用 `persistent_per_task` 的 task9 试跑；当前账户用量24%超过13%停止线，因此批次账本没有新请求，Full reset未使用。锁屏期间可用 `caffeinate -i` 保持已启动的本地运行进程，但不会绕过账户额度守卫或自动启动尚未准入的批次。

### 2026-10-25 约00:45 UTC：R4 与传统组输入等价性审计

针对新零请求预检和传统组冻结数据完成逐文件核验：100个订单任务与50个正式场景任务的ID集合完全相同；10/10地图、10/10订单源文件、10/10正式场景源文件SHA-256一致。129个可路由预检的地图边方向及成本语义审计全部通过，评估真值未进入模型提示。另21项均保留在总分母并标注为非法端点、确定不可达或意图解析未构建，不计为模型已运行结果。机器可读报告：`outputs/route_layer/r4_codex_formal_batch_preflight_20261025_v1/input_equivalence_audit.json`。无模型请求。

### 2026-10-25 00:56 UTC：拒绝提议的能耗和地图回放审计

finalizer 对可解析方向但被本地路网拒绝的动作记录单步假设能耗，并保持车辆实际耗电为0；方向不可解析时不虚构估算。renderer 即使没有合法 edge ID 也会显示候选移动的红色虚线，并展示拒绝原因和反向道路/地图中位路段估算依据。该假设值不会计入有效路线或批次的路线能耗汇总。14项专项测试、Chromium fixture 检查和全量227项 unittest 通过。真实 task9 重新终审仍是1个已接受合法方向、`partial_legal_prefix_not_complete`；正式批次仍0请求。当前周额度24%高于13%停止线，不能启动模型请求。本修复只改进异常提议审计和回放，未产生路线结果。


### 2026-10-25 01:30 UTC：预检安全上限不等于用户授权

旧持久会话预检清单曾把128次候选请求上限标为 `authorization`。生成器及 `--request-limit` 命令帮助现明确表示：该字段是候选安全上限，`user_authorization_status` 为 `not_granted`；仅运行 `--preflight-only`，模型请求0。新版清单 `outputs/route_layer/r4_codex_stepwise_formal_task9_persistent_preflight_20261025_v2/manifest.json` 使用 schema v3、`persistent_per_task` 会话，并通过冻结地图语义哈希核验；v1 清单注明已被替代。12项相关测试和全量227项测试通过（95.445秒）。本项不更改冻结任务或模型提示，也不授权正式试跑。账户周用量24%，高于13%停止线。


### 2026-10-25 续跑预检的授权标记复核

全量扫描找到 task9 续跑预检的另一个旧 manifest 把127次候选上限标为授权；现已改用 schema v3 并明确 `user_authorization_status=not_granted`。产物仍为0模型请求、`ephemeral_per_request`，因其父前缀会话模式与正式要求不一致，不能作为完整 persistent pilot。所有已执行试跑历史记录保持原样。
