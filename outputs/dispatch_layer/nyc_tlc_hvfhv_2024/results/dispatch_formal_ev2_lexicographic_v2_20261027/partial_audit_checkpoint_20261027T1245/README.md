# NYC EV v2 seed0 部分检查点审计

审计时间（UTC）：2026-10-27T04:43:50+00:00
结果目录：outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_ev2_lexicographic_v2_20261027/partial_audit_checkpoint_20261027T1245

## 检查结果

- 预期90个日期×车队配对格；本次完整、输入一致且逐轮校验通过：4/90。
- 已有方法结果JSON：34/630；未完成或缺失方法项：596。
- 去重Codex trace：63个，turn completed：63；输入1115040（缓存737152是子集）、输出11489、合计1126529 tokens。
- 损坏JSONL行0、CLI error事件0、工具调用事件0。
- 审计脚本exit code 2表示预期矩阵未完整；没有触发任何模型调用。CLI token日志不是实际账户账单。

## 解释边界

这是仍在运行批次的时间点完整性快照。4/90配对格和单seed结果仅用于核验数据落盘、共同输入、validator和trace；不能作为策略效果、方法排名或跨月份结论。源runner在审计后继续运行，后续完整审计需覆盖终态结果。

机器可读文件：`run_audit.json`、`usage_audit.json`、`paired_statistics.json`、逐方法与配对CSV。
