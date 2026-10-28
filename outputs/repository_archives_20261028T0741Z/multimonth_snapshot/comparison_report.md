# NYC TLC 多月调度实验结果审计

审计时间（UTC）：2026-10-28T07:20:09+00:00

## 覆盖情况

- 预期配对单元：180（30 日期 × 3 车队 × 2 个 seed）
- 全方法完整、同输入、seed 一致、逐轮校验有效且回放审计通过：142
- 方法结果文件：1017/1260
- 唯一 Codex JSONL 文件：2663；turn 完成：2593；输入 token：49245336；缓存输入 token：29159168；输出 token：402191；总 token：49647527；工具调用事件：0；CLI 错误事件：72（其中重连提示：2）；非工具提示项：2663；损坏行：0
- 每个 seed 的 trace 数与 token 统计见 `usage_by_seed.csv`；Codex 账户实际扣费仍无法由 CLI trace 得知。

## 方法汇总

见 `method_summary_by_fleet.csv`。均值按窗口等权计算；F1/F2/F4 分开列出，ALL 为每日期的三个车队窗口等权汇总。

## D8 配对差值

见 `paired_statistics.csv`。正值表示该指标 D8 高于对应基线；服务率和服务数越高通常越好，等待、空驶、电耗和计算时间越低通常越好。95% 区间按日期与 seed 双重成簇重采样，同一日期的三种车队一起保留，并对日期等权。

各 seed 的单独配对效应与日期间标准差见 `seed_effects.csv`，用于检查主结论是否依赖某一个运行 seed。

只保留 D8 策略调用有效的敏感性分析见 `accepted_policy_sensitivity.csv`；该子集可能受策略接受与场景难度共同影响，属于选择偏差敏感性分析。

策略调用接受率以实际发起调用为分母；调用失败、回放接受/失败和无可行候选的未调用轮次分开报告，详见 `method_policy_summary_by_seed.csv`。

## 解释限制

- TLC completed trips and synthetic fleets are proxy inputs, not real requests and vehicle trajectories.
- Thirty selected calendar dates are date clusters, but dates were quantile-stratified rather than sampled from all NYC dates; intervals describe selected-window variation and do not establish citywide effects.
- Replicate seeds estimate simulation/model-run variation only on these selected inputs; dates remain the demand clusters.
- Token usage is not actual Codex account billing; replayed responses must not be counted twice.
- 全矩阵完整率要求全部方法单元覆盖、同输入哈希、seed 一致、validator 通过并符合 D6/D7 回放协议。主配对效应按 D8 与各基线分别核验，并按 intention-to-treat 保留配置回退；某个消融缺项不会剔除有效的 D8-D2 主比较。D6/D7 的组件归因只看接受 D8 策略且逐字节 prompt 哈希一致、没有新增回放调用的敏感性子集，详见 `accepted_policy_sensitivity.csv`。失败或缺项列在 `run_audit.json`。
- LLM 接受率、回退、请求用量和每轮安全校验须与乘客结果并列查看，不能只看一个汇总指标。
