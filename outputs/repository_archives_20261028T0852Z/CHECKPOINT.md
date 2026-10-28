# 检查点说明

捕获时间：2026-10-28T08:52:55.091214+00:00

- GitHub `main` 的前一已核验提交：`cd8c81845481a0ca4287cd08b76fc7c1fd7951dd`。
- 两个 NYC 实验的父级 manifest 均为 `running`；本次只加入各自已结束且校验稳定的一个 D2 结果文件。进度日志和其他活动输出不纳入。
- R4 父 manifest 为 `paused_at_parallel_checkpoint`；worker_00 为 `running`，模型请求计数 `1849`，任务计数 `{'outcome_unknown': 9, 'route_complete': 7, 'failed': 6, 'active': 1, 'pending': 6}`。
- R4 本次仅归档无任务 ID、无 prompt、无 PID、无原始 manifest/state 的安全状态摘要。
- 本检查点为增量审计证据，不是任何运行批次的最终归档。
