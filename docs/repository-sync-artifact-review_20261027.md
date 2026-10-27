# GitHub 同步材料审查记录（2026-10-27）

本轮只从研究工作区与指定的 R4 外部输出目录筛选可复核材料。归档包均保留文件级 SHA-256；活动批次只保存明确带时间戳的进度检查点。

## 已归档材料

- NYC 派单 seed0 EV v2：15:49 官方审计快照为 13/90 个完整配对格、93/630 个方法结果。归档路径 outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/repository_archives_20261027T1552CST/seed0_ev2_1549_results_and_traces.tar.gz，SHA-256 f39a32c937354136b9cb8f648200a47d0e90dba5d38a9a0819bac77dcdbfb79e。这是部分进度，不能作为完整矩阵或方法排名。
- NYC seed0 固定分配 EV v2 离线侧车：15:46 快照覆盖 92 个已存在结果，13/90 格、538 个预期结果缺失。目录 repository_archives_20261027T1554CST/nyc_ev2_fixed_assignment_recalc_1545.tar.gz。它重算已保存分派的接驾能耗代理，不改变既有分派。
- NYC seed4 legacy EV v2 离线侧车：460 个已存在结果、61/90 格，170 个预期结果缺失；单独保存于 repository_archives_20261027T1554CST/nyc_seed4_legacy_ev2_recalc_20261027T1535.tar.gz，不并入 seed0。
- seed0 15:36 与 seed4 legacy 15:20 的较早审计点也保存在 repository_archives_20261027T1548CST/。其中 seed4 一条 trace 当前内容与该审计记录不一致，明确列在 EXCLUDED_UNVERIFIED.tsv，没有纳入归档。
- seed3 replicate 25/90 的部分快照及其独立 trace 保存在 repository_archives_20261027T1524CST/。它的混合代码历史使其必须与官方 seed0 和 legacy seed4 分开。
- Route layer EV v2 payload-phase 离线侧车：450 个槽位中 390 个有完成路线并成功重算，60 个未完成或无有效路线保持未评估；0 错误，没有重跑路由或模型调用。索引为 outputs/route_layer/traditional_experiment_v1_energy_v2_payload_phases_20261027_v2/phase_energy_index.jsonl。
- Porto 固定车队 R1 v3 汇总：20 个预设种子/目标槽位中 19 个完成、seed4 FASTEST 失败并留在分母；配对区间使用 9 个共同完成种子。只同步聚合结果、协议、运行器、设计稿和置信区间；所有交通与能耗值都是 SUMO 模拟值，不代表 Porto 实测或真实派单。
- Route layer 归档包位于 outputs/route_layer/repository_archives_20261027T1540CST/，包括完成的路线成本离线评估、决策侧车、已完成的纯 LLM 对照批次（该协议 0/129 合法完整路线）和两个单任务诊断。各包独立标明协议和边界，不替代仍在运行的正式 R4 批次。

## R4 正式批次快照

唯一跟踪的实时 manifest 是 /Users/didi/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/r4_codex_formal_batch_20261027_v2/manifest.json。它持续运行，工作区同名目录是旧副本。同步进仓库的 manifest 必须标明读取时间和 SHA-256，只作为进度证据；运行中的任务目录、worker 日志、锁和 PID 文件不整目录复制。

## 暂缓的文件

- NYC D1 训练标签 outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_v1/training_d1_labels/TR_20240103_200000_q10/F1/D1_optimizer_seed_0.json 为 5,011,177,103 字节，SHA-256 0cfe5420507e8b6ab09e7bbb82266c3d35c81deda3acc10f9ea1b15bc55a9b63。gzip -1 估算约 307,682,716 字节，仍超过 GitHub 单文件常规限制，需要分卷；该 NYC TLC 页面未标注明确数据许可，暂不公开副本，待核清许可及分卷方案。[NYC TLC 2024 HVFHV 数据页](https://data.cityofnewyork.us/Transportation/2024-High-Volume-FHV-Trip-Data/bqxa-629f)
- Porto strict_v3_2/seed_0/FASTEST/battery.xml 为 206,336,074 字节，SHA-256 cf3a0d48c6ab71f6cbc7a200657a80d744c5af9e10cb6468ad1b7f1842ee2c80，属于已被 formal v3 fixed-delay 基线替代的诊断输出，本轮不再上传。
- 已同步的 UCI Porto 原始压缩数据通过现有 Git LFS 路径维护，本轮不重复添加。

本记录用于描述这次审查范围和限制，不把时间点检查结果改写为最终实验结论。


### R4 当前时间点快照（15:58 CST）

外部执行镜像 manifest 的 SHA-256 为 ff0a60dee56e9f8027aea4a516e969e499bebfcbc4364b8390a2f373fb1f4b18，更新时间 2026-10-27T07:58:00.559191Z，状态 running，1,795 次登记请求；任务状态为 3 route_complete、17 outcome_unknown、101 pending、7 ready、1 active，21 次请求投递状态未知。外部目录 lock 文件存在；formal_driver.pid 中记录的 54222 当前不存活，但仍发现匹配的 chunk-driver 进程。因此该 PID 文件是陈旧线索，不能据此判断批次已停止。本轮只复制 manifest，不复制活动 worker 输出。

## 2026-10-27 16:10 CST 检查点

- NYC seed0 的离线 EV v2 重算目录 `fixed_assignment_energy_recalc_v2_20261027T1604` 已有稳定清单和独立完整性审计：98/630 个预期方法输出、14/90 个配对格，532 个预期结果仍缺；98/98 输出核验通过，错误数为0，状态 `completed_with_expected_gaps`。压缩归档位于 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/repository_archives_20261027T1610CST/nyc_ev2_fixed_assignment_recalc_20261027T1610CST.tar.gz`，SHA-256 `b9435865b60ddc778f12b8712a7c60dc371c12b6951283f7aa649113979a5c00`；104个源目录文件，共13,214,138字节，压缩后974,070字节。内层103项校验和与外层归档校验均通过。
- 指定外部 R4 manifest 在 2026-10-27 16:08:28 CST 读取，SHA-256 `9fe49af7d67bd67d6f54ff20b451c15cb33e4681020722279e7a0346dfb2643a`，状态 `running`；任务为3 route_complete、17 outcome_unknown、101 pending、8 ready，另有21项 unresolved_in_flight。四个 worker 记录为 paused_at_request_chunk_limit。此 manifest 仅是进度证据，不能当作正式批次终态。
- 本轮未复制 seed0 正在写入的正式派单目录、worker 输出、锁或 PID。上述 NYC 检查点不代表完整矩阵，也不支持方法排名或总体效果结论。

## 2026-10-27 16:16 CST 运行中批次进度更新

- R4外部manifest于16:12:37 CST读取，SHA-256 `7346b6ff93ab3b29c09db1d9b4b310add7f609f1dca5441e0e81f9f99e599c34`，状态`running`：3 route_complete、17 outcome_unknown、101 pending、3 active、5 ready，21项投递状态未决。manifest记录的worker PID未存活，但16:14:36 CST系统进程表仍见worker_00至worker_03的chunk driver及batch watcher；执行锁存在。本次只更新manifest快照，不复制worker文件。
- NYC seed0的`EV_20240411_200000_q50/F4/D0_greedy`单方法结果在16:10:01 CST写入，文件1,604,350字节，SHA-256 `02b24d5277320cb688d49ef33f397b8ad5447295fbfc7df16698b4c58c091ec7`。状态字段为`complete_simulation`、`partial_run: false`、`performance_claim: false`；文件稳定且未被进程打开，记录内563轮审计、0个round validation errors。归档包为`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/repository_archives_20261027T1616CST/nyc_seed0_ev2_F4_D0_greedy_20261027T161001.tar.gz`，SHA-256 `ba2d5bbf3a0c5685175fb17a085490c14e425f193bc520f7af8c4326978190e3`。
- 该归档只包含一个完成的方法结果，不代表此格7种方法齐全或90格矩阵终态，不据此形成配对比较或效果结论。
