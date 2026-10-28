# NYC EV2 与分段经济侧账覆盖补充快照

核验时间：2026-10-28 04:21 UTC。此文件补充而不覆盖较早快照。

- 当前正式 EV2 审计：79/630 份结果有效，0 份无效，551 份此时未出现。审计文件 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0/time_money_ev2_coverage_snapshots/ev2_segment_ledger_audit_live_20261028T0420Z.json` SHA-256 `0b0f59061cd93670a1d4a1fad1d5c6d82d8a3b75c322c5ae971660550d9f33fe`，审计内时间 `2026-10-28T04:21:00+00:00`。缺失只代表快照，不记作失败。
- 最新 v5 侧账：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0/time_money_v5_segment_sidecar_20261028T0420Z/manifest.json` SHA-256 `b5f81591ddbc4bff75692eeb737a3143e7fdbe3f1136723e8b89e2ed7a61ebee`。覆盖 79 份结果与 4348 条行程段（空驶 2174、载客 2174）；源结果哈希复核通过、源文件未改、0 新模型调用、0 SUMO 重跑。
- 79 项时间—金额门禁均为 `{'blocked_incomplete_economic_inputs': 79}`，加权排名为 0。电价未冻结且共同路线候选池不可用；历史票价只作代理，不能称为实际收入。费用缺失保留 null。
- EV2 中位账在每个 journey segment 汇总 signed-net 后计费；采样规则来自共享EV2模型。纽约接驾仍是质心起停代理，不是道路级真实轨迹。dispatch runtime 仍是 `nyc_dispatch_decision_contract_v4`：D1/D2/D6/D7/D8将EV2作词典序末级目标，D0为贪心，D5将EV2提供给LLM但不做全局能耗优化；time-money v5仍未在线接入。
- Progress JSONL 截至 `2026-10-28T04:24:50+00:00` 共 357 条事件，文件 SHA-256 `2f7c14805244879b574b56a66c40d96abe4b28e59fe6ba8fc6d5e51da5439a95`；最新为 F4/D2 进度。该矩阵仍在执行。

证据：哈希绑定的 04:20Z EV2 审计、04:20Z v5 sidecar、progress JSONL 与在线 contract 审计。
