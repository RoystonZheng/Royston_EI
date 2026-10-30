# NYC seed 0 EV2 and time-money checkpoint

Snapshot time: 2026-10-30 02:21–02:23 UTC. This is an audited point-in-time snapshot, not the complete 630-run experiment matrix.

## Audit coverage

The official EV2 ledger reports 425 valid of 630 expected method-runs, 0 invalid and 205 missing. The paired input fingerprint audit covers all 425 valid runs with zero scenario or input-hash mismatches. The unique-trace audit passed with 1,112 result references, 1,039 unique trace files, 39 shared-trace groups and 73 duplicate method references; actual account charges are unavailable.

The v5 sidecar covers the same 425 source results and 3,786 segment rows. It added 63 source results, made 0 model calls and 0 SUMO reruns. Electricity tariff is null and a complete shared route pool is unavailable, so weighted rankings and monetary route-choice conclusions remain blocked. Its two JSONL files were losslessly gzipped; source and compressed SHA-256 values and round-trip verification are recorded in `manifest.json`.

## Included completed F4 cell results

All four results below belong to case `EV_20240702_213000_q25`, fleet F4, and the audited 425-run snapshot. They are individual simulated cell outcomes, not an aggregate effect claim:

- D5: 37/393 served, 356 unserved, simulated EV2 26.775791 kWh. It records 366 CLI invocations but usage is missing or partial; actual billing is unavailable. D5 uses direct LLM assignment with a deterministic validator and no global optimizer.
- D6: 48/393 served, 345 unserved, simulated EV2 35.553430 kWh.
- D7: 48/393 served, 345 unserved, simulated EV2 35.553430 kWh.
- D8: 48/393 served, 345 unserved, simulated EV2 35.395683 kWh.

D6 and D7 replay the existing saved context-policy call; no new invocation was made for those replays. Their usage fields are rate-card estimates, not actual account charges. D8 records one model call and a rate-card estimate; actual account charges are unavailable. Do not treat these records as time-money rankings.

The later F1/D6+D7 deterministic replay for case `EV_20240725_211000_q50` still had an active log handle during this check and is excluded. Raw prompts, request traces, live logs and worker state are not included. All NYC results use a zone-OD/centroid proxy, not street-level route simulation.
