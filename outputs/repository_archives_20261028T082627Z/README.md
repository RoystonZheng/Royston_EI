# Research sync checkpoint — 20261028T082627Z

This checkpoint preserves newly verified research files and stable experimental outputs. These are evidence snapshots; they do not turn a pilot or partial sample into a completed formal matrix. Local absolute paths were converted to workspace-relative paths or redacted.

## Seed 4 audit

The independent audit dated 2026-10-28T08:06:05+00:00 covers one seed: 87 of 90 paired cells are fully valid; 627 of 630 method result files are present. This is a single-seed audit. Three cells remain unresolved: D5 on 2024-04-11/F4 (delivery outcome uncertain), and D7 on 2024-07-18/F2 and 2024-08-21/F4 (invalid policy parameters). No same-request replay is authorized by this evidence. These files do not support cross-month effect or ranking claims.

## Incremental completed results

- Twelve D5–D8 result JSONs preserve the three audited seed-4 resumed cells.
- Four complete results preserve selected EV2 segment-ledger runs; the parent batch remains incomplete.
- Nine complete seed-3 outputs preserve one seven-method F1 case and two individual results; they are not a complete evaluation matrix.
- Raw prompt, request, context-policy trace, and call-log files are omitted.

## Porto OSM pilot

The source manifest status is `pilot_route_complete` for one order. The time-money candidate gate is blocked by incomplete candidate data. The same-snapshot comparison rejected an illegal directed turn; the SUMO one-order result uses a shared-link fallback. `formal_matrix_result` is false. Treat this as a pilot/debug artifact, not a formal model-route result.

## R4 formal batch progress

The app-support progress snapshot records the parent as `paused_at_parallel_checkpoint`. Worker 00's manifest reports `running` with an in-flight marker, but its update timestamp is stale and the formal-driver PID is not alive. This state is unresolved; no active worker outputs or raw manifests were copied, and no final result is claimed.

## Time-money sidecar

The offline accounting sidecar reports `completed_segment_economics_route_choice_blocked`, 12 segment rows, and 0 new model calls. It records segment economics only; route-choice evaluation is blocked.

## Updated direct files

The current paper draft, evidence audit, time-money conformity audit, research memory, R4 pilot/formal scripts, and their acceptance/resume tests are synchronized at their repository paths. Test files were synchronized without running tests in this monitoring pass.
