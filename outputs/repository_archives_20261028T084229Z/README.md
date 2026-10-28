# Research checkpoint — 20261028T084229Z

This package preserves a two-seed audit snapshot, three completed individual result files, and a safe R4 progress record. The audit and individual outputs remain distinct where timestamps or experiment branches differ. Local absolute paths and credential patterns were removed or normalized.

## NYC multimonth audit snapshot

The source audit timestamp is 2026-10-28T08:34:50+00:00. It covers seeds 0 and 3, reporting 146 of 180 valid paired cells and 1041 of 1260 method result files. The remaining 219 expected method entries are absent or partial. The report describes 30 quantile-stratified dates and synthetic fleets; it does not establish citywide NYC effects. CLI token totals are not account billing. The D5 seed-3/F2 individual JSON hash matches a row in this audit; the D0 seed-3/F4 JSON was written after the audit timestamp and is separate.

## Other completed individual result

The EV2 seed-0/F4 D0 result belongs to the segment-ledger branch and is not merged into the legacy multi-month audit. All three individual files have `partial_run=false` and were closed when checked. They are isolated completed outputs, not a complete formal matrix.

## Three-experiment EV2/time-money review

The repository review document is a read-only cross-check dated 2026-10-28 08:29 UTC across the R4 grid, NYC TLC dispatch and Porto road experiments. It distinguishes EV2 accounting from time-money selection, records blocked economic rankings where tariffs/cost inputs or a shared feasible candidate pool are missing, and does not claim online selector adoption.

## R4 live progress

At capture time the parent manifest reports `paused_at_parallel_checkpoint` with 5949 requests; worker 00 reports `running`, 1796 requests, 7 route-complete, 6 failed, 9 outcome-unknown, 1 active and 6 pending tasks, plus an in-flight request. Parent and worker scopes differ and counts must not be added. The worker manifest file is current on disk and a Codex process matching the exact output root was observed, but its internal update timestamp is stale. This is a progress snapshot; no raw manifest/state, task IDs, prompts or active worker outputs were copied, and no final result is claimed.

## Directly updated files

The current paper draft, new three-experiment review, evidence audit, time-money conformity audit and research memory are synchronized at their repository paths. No tests, model calls or SUMO runs were started by this monitoring pass.
