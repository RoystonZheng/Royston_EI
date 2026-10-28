# 未纳入本次同步的内容

- 指定 R4 目录中的 raw parent/worker manifests、task state、活动 worker 输出、request payload/prompt、quota watch 日志和账号用量遥测；worker 仍在运行。仅同步脱敏时间点摘要。
- 两个 NYC 批次仍在运行的 `progress.jsonl`、live execution logs、prompt/context traces，以及尚无单任务完成证据的其他输出。
- 任何凭据、缓存、临时文件、锁/PID、`.DS_Store`、`.bak`、`.pyc`、`.worktrees` 或 `.git` 内容。
- 整个 `Application Support` 未扫描；只读取用户指定的 R4 精确输出目录。

状态：这些项目仍留在本地活动运行目录；后续批次终态后再做完整审计归档。
