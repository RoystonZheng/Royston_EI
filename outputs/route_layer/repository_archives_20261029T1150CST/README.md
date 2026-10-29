# R4 formal checkpoint — 2026-10-29 11:49 CST

This is a time-stamped partial checkpoint, not a completed batch or performance result.

- The parent manifest is still `running`: 40 route_complete, 35 failed, 5 outcome_unknown, 4 ready, 3 active, and 42 pending; 24 delivery records remain unresolved. Its pause reason says delivery has ended, which conflicts with the active and unresolved fields.
- The full-flow audit validates 40/40 completed routes and 6,695 directed edges. A separate per-segment audit validates 40 completed routes across 87 states and 8,207 accepted edges. These are distinct integrity scopes, not performance metrics.
- The EV2 cumulative audit covers 40/40 completed tasks with zero integrity errors, zero additional model calls, and no route reruns.
- The terminal final audit and usage reconciliation for `task-0002-0001` are included separately. Active task logs, raw prompts, replay HTML, manifests containing state, and trace files are excluded.

See `manifest.json` and `SHA256SUMS` for exact source/archive hashes.
