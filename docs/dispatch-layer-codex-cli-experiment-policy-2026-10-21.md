# 调度层纯 LLM 实验：Codex CLI 记录口径（2026-10-21）

## 用户确认的调整

从 2026-10-21 起，调度层纯 LLM 组 D5 的新实验调用统一改用 Codex CLI，以减少 DeepSeek API 调用；每次运行单独记录 Codex CLI 版本、模型、推理强度、输入/缓存/输出/reasoning/总 token、耗时、CLI/工具事件、原始 JSONL 与最终输出的路径和 SHA-256。旧 DeepSeek 结果保留为历史条件，不与 Codex 结果合并。

## 固定运行口径

- 后端：`codex exec --json`；模型固定为 `gpt-5.6-luna`，推理强度 `low`。
- 输入：沿用 D5 当前派单轮的完整车辆、订单和候选矩阵；统一输出 `selected_candidate_ids`。
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

## C2 完整模拟 seed 0

在 smoke 验证通过后，按用户“实验统一使用 Codex CLI 并记录消耗”的决定运行 C2 完整模拟。共 5 个模拟调度轮次、1 次模型调用；输入/缓存/输出/reasoning/总 token 为 17,625/0/167/62/17,792，模型回合耗时约 8.31 秒。首轮 12 个候选中接受 3 个匹配，validator 为 `valid`，硬约束错误 0、回退 0、工具事件 0。全程最终 3 单完成、1 单未服务，配置内服务率 3/4（75%），平均接驾等待 60 秒、空驶 900 米、模拟能耗 1.677 kWh。Codex CLI 报告的两次运行 token 与各自原始 JSONL `turn.completed.usage` 一致，轨迹及最终 JSON SHA-256 均与结果记录相符。美元账单字段为 `null`，不由 token 推算。

该结果是单一合成场景、单种子的一次完整模拟，不足以支持方法优劣或普遍性能结论；75% 只描述这个配置。输入仍来自已派遣历史订单、合成车辆位置及 taxi-zone 接驾代理，不代表 NYC 实际运营。C3/C4 尚未启动；C2 其余种子也尚未运行。

## 产物

- v1 失败结果与原始 JSONL：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_v1/C2_four_same_second_orders_three_vehicles/single_fleet/`
- v2 smoke 结果与原始 CLI JSONL：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_v2/C2_four_same_second_orders_three_vehicles/single_fleet/`
- C2 完整模拟结果与原始 CLI JSONL：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_c2_full_v1/C2_four_same_second_orders_three_vehicles/single_fleet/`
- 输出 schema：`dispatch_experiment/schemas/codex_direct_assignment.schema.json`
- 运行入口：`scripts/run_nyc_tlc_dispatch_benchmark.py`

Codex 结果与此前 DeepSeek D5 C2 smoke 分目录保存；两种后端的模型和上下文不同，不合并统计。C2 已完成一次 seed 0 完整模拟，仍需按冻结方案扩展种子；C3/C4 尚未运行。

## 来源

- Codex CLI JSONL 和 `turn.completed` token usage 的 eval 记录方式：[OpenAI Developers：Testing Agent Skills Systematically with Evals](https://developers.openai.com/blog/eval-skills)。
- 本项目遇到缺失的 usage 时统一记为未知，不推断为零；CLI 账户账单金额另行核对，不从 token 数倒算。
