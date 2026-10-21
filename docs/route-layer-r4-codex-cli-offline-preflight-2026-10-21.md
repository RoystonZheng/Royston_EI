# R4 Codex CLI 离线接入预检（2026-10-21）

## 检查范围

用户同意先评估能否用已登录的 Codex 账号替代单独的模型 API key。先检查本机登录态、CLI 参数和现有路线约束测试；随后按用户授权，对同一 8×8 R4 任务发起 1 次单步 smoke。没有读取或输出 OAuth 凭据，也没有修改 R4 运行器。

## 离线检查结果

- 本机 Codex CLI：`codex-cli 0.145.0`。
- `codex login status` 显示已通过 ChatGPT 账号登录。该结果只证明有本地登录态，不证明指定模型当前可调用或计划额度充足。
- 本机 CLI 配置默认模型为 `gpt-6-luna`，推理强度为 `xhigh`。正式测试若采用 CLI，必须显式固定模型和推理强度，并单独记录为 Codex 模型条件；不能与 DeepSeek 条件混称为同一模型实验。
- `codex exec --help` 支持从 stdin 接收提示，指定 `--model`、`--sandbox`、`--output-schema`、`--output-last-message` 和 JSONL 事件输出；`codex exec resume` 支持沿用会话。
- CLI 帮助中没有单次输出 token 上限参数。Schema 可以收紧最终输出，但不能据此宣称内部推理/用量有硬 token 上限。一次 CLI 执行也会消耗 Codex 计划额度或适用 credits，额度与调用模型 API 的账单分属不同计费路径。
- R4 当前仍由 `DeepSeekClient` 直接调用 DeepSeek；模型选择、逐步方向 schema 和本地 validator 尚未接入 Codex CLI。
- 现有 R4 逐步生成、约束和 Agent 单测离线通过：`test_r4_stepwise.py` 13 项、`test_r4_constraints.py` 13 项、`test_r4_agent.py` 17 项，共 43 项。它们验证既有 R4 行为，不代表 Codex CLI 已接受并生成过路线。

## 对实验的影响

若逐步路线每一步分别启动一次 `codex exec`，100×100 任务会产生很多调用；已有预算预检显示，130 个可达任务最短路径合计 17,219 条有向边，且每步输入会重复包含地图。转用 Codex 账号可能减少单独 API 现金支出，但会消耗 Codex 计划额度，也没有证据表明这种调用方式会降低整体用量。

v2 请求确认 `gpt-6-luna` 在该 ChatGPT 账号路径不可用；v3 换用本地模型目录列出的 `gpt-5.6-luna` 后，账号调用和结构化响应成功，但模型调用了本地 Dijkstra 搜索工具，不能作为独立 LLM 选路结果。正式批次和 DeepSeek R4 方案均未改变。

## 单次 Codex 账号 smoke 结果

用户授权后，在 8×8 seed 0 任务上执行一次 CLI 请求：当前节点 `node_0_5`，本步合法方向为 `S`、`N`；仅请求一个方向提议，不规划整条路线。使用 `gpt-6-luna`、low 推理强度、动态 JSON Schema、只读沙箱、临时会话、零重试，并从子进程环境移除 `OPENAI_API_KEY` 与 `DEEPSEEK_API_KEY`。

CLI 返回 HTTP 400：`gpt-6-luna` 不支持通过 ChatGPT 账号使用。调用在模型推理前失败，没有模型响应、路线动作或 usage 事件。CLI 进程退出码为 1；实际模型输入/输出 token 未报告，故记录为未知，而非 0。没有切换模型或重试。该结果只说明本次指定的模型标识不可通过当前账号路径调用，不能据此推断所有 Codex 支持模型都不可调用。

逐步 smoke 产物：`outputs/route_layer/r4_codex_smoke_v2/`（`result.json`、`request_metadata.json`、`cli_events.jsonl`、`cli_stderr.log`）。尚无路线结果，局部合法性校验未运行。


## 切换模型后的单步 smoke v3

按用户允许切换模型的指示，本次用 `gpt-5.6-luna`、low 推理强度，在同一 8×8 seed 0 任务上再执行 1 次，零重试、无 API key。CLI 返回符合 JSON Schema 的 `move/S`，该方向属于本地动态候选集合 `S/N`，五项自检均为 true。

但事件日志显示模型在最终输出前调用了一次本地 shell 命令，对所给方向成本网格运行 Dijkstra，打印了完整九步方向序列 `S,S,S,E,E,E,E,E,E`（总代价 95.208333 秒）；中间消息曾给出 `N`，工具运行后最终改为 `S`。因此该结果仅确认账号链路可用和 schema 输出可用，**不构成 LLM 独立选路的有效实验结果**。不得并入纯 LLM 或 R4 路线性能统计。一次 CLI 会话记录 input 73,264 tokens（cached input 52,864）、output 2,360 tokens（reasoning 385）；不据此推导现金费用。未请求整条路线，也未重试。记录 `outputs/route_layer/r4_codex_smoke_v3/`。

## Stop 勾子配置复核

复核发现仅改安装缓存副本不足以覆盖插件源副本。现已在已安装插件缓存和 `.tmp/marketplaces` 插件源两份 `hooks.json` 中都将 `Stop` 事件设为空数组，并分别保留 `hooks.json.before-disable-stop` 备份；两份文件均为合法 JSON，非 Stop 事件保留，`trace_collector.py` 在两份插件目录均存在。当前 Codex 进程是否已重载配置仍需重启后确认；未声称运行时热更新已生效。


## 禁用本地搜索工具后的单步 smoke v4

用户授权再做 1 次验证。仍使用 `gpt-5.6-luna`、low、相同 seed/map/task/current node/candidate directions；本轮追加禁止工具的提示，并以 CLI 临时关闭 `shell_tool` 与 `unified_exec`。配置预检确认两个开关均为 false；一次调用、零重试。

最终响应为 `move/S`，方向属于动态候选 `S/N`，对应候选边 `edge_node_0_5_node_0_6`；JSON Schema 的字段、枚举、自检键和值均通过本地校验。事件日志只有一个 `agent_message`，没有命令、shell、搜索或 MCP 工具事件。CLI 报告 16,172 输入 token、762 输出 token（reasoning 702）；非致命提示仅为技能描述被压缩。故 v4 作为单步无工具格式 smoke 通过，但只产生一次动作，不能据此宣称完整路线完成、路线最优或正式 R4 批次有效。

产物：`outputs/route_layer/r4_codex_smoke_v4/`。Stop 勾子源配置和安装缓存均为零个 Stop 条目，其他事件的收集脚本存在；当前桌面进程是否热重载尚未验证，重启后方能确认运行态。

## 证据边界

证据类型：本机 CLI 离线检查 + 既有 R4 单元测试 + 三次分别授权的单步 CLI smoke。v2 在模型推理前返回 HTTP 400；v3 得到合法 JSON，但 Dijkstra 工具介入；v4 禁用 shell/unified_exec 后得到合法、无工具调用的单步响应。v4 报告 input/output 用量，但没有全程路线或性能结果。Stop 勾子两份文件配置已关闭，其他事件的 collector 脚本存在且可解析；运行中的桌面进程是否重载仍未验证。CLI 版本、支持模型、登录态和配置可能随时间变化。

## v4 产物一致性复核

2026-10-21 对 `outputs/route_layer/r4_codex_smoke_v4/` 做本地一致性检查时，发现 `request_metadata.json` 曾混入 v2/v3 的旧字段（包括模型 400 错误和本地 Dijkstra 调用），与 v4 原始事件及 `result.json` 矛盾。已按 v4 原始 JSONL、模型响应和结果文件重建元数据，并复核调用次数为 1、重试为 0、工具调用为 0，token 用量一致；现有 v2/v3 证据文件未修改。该修正仅清理记录，不改变模型响应或实验结果。
