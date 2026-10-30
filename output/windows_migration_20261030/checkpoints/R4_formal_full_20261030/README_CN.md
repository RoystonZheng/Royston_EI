# R4 full checkpoint for Windows migration

Captured: 2026-10-30 16:14:53 CST

`R4_formal_checkpoint_full_20261030T161453.tar.zst` is a byte-preserving archive of the complete R4 formal batch output tree (all source files, including prompts, traces, raw manifests and logs). The archive SHA-256 is listed in `SHA256SUMS.txt`.

## Stop state

The local process scan found no matching R4 runner at capture time. The parent manifest still reports `running` while `worker_recovery_429` reports `stopped_provider_or_tool_contract_failure`. Keep those source values unchanged; the independent stop record documents the process scan and latest task trace. Task `task-0009-0004/run_000` has state `stopped_request_error` and run manifest `partial_legal_prefix_not_complete`. Do not replay the request or count this partial prefix as a completed route.

`STOP_AND_MIGRATION_STATE.json` includes hashes for the source manifests, task state, and latest trace. This is a migration snapshot, not a new experiment result.
