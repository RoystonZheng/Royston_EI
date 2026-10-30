# NYC seed 0 EV2 and time-money checkpoint

Snapshot: 2026-10-29 17:07:42 UTC. This is a point-in-time audit, not a complete experiment matrix.

## Frozen audit

The official EV2 ledger contains 417 valid of 630 expected method-runs, 0 invalid and 213 missing. The paired input fingerprint audit reports 417 matching runs and zero scenario/input hash mismatches. The unique-trace audit passed with 724 result references, 655 unique trace files, 37 shared-trace groups and 69 duplicate method references; actual account charges are unavailable.

The v5 segment sidecar covers the same 417 source results and 20,666 segment rows. It made 0 new model calls and 0 SUMO reruns. Electricity tariff is null and a complete shared candidate pool is unavailable, so weighted rankings and route-choice conclusions remain blocked. JSONL sidecars were losslessly compressed; source and compressed SHA-256 values are recorded in `manifest.json`.

## Included task results

- The F1/D5 result is part of the 417-run snapshot: 12 of 393 orders served, 381 unserved, simulated EV2 8.182810 kWh. Its eight CLI invocations have usage estimates only; actual billing is unavailable. D5 did not apply a deterministic global energy objective.
- The F2/D8 result completed after the 17:07:42 UTC snapshot and is stored separately; it is not included in the 417-run denominator: 24 of 393 orders served, 369 unserved, simulated EV2 18.013569 kWh. This isolated result is not a cross-method ranking or overall effect claim.

Both results use NYC zone-OD/centroid proxies rather than street-level route simulation. The currently active F2/D5 live log was excluded because it still had open write handles during this snapshot. Raw prompt files, live logs and request traces are not included.
