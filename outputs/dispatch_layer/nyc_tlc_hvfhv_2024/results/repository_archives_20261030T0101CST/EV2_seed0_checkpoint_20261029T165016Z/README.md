# NYC EV2 seed0 checkpoint (2026-10-29 16:50:16 UTC)

Frozen point-in-time audit while the NYC batch continued. The active F4/D5 run and its live log are not included.

- Official ledger: 412/630 valid, 0 invalid, 218 missing.
- Paired input/scenario fingerprint: 412 results, no mismatches.
- Unique trace audit: 684 references, 617 unique trace files, 67 duplicate references, 36 shared groups; account charges are unavailable.
- F4 q75 D8, D6, and D7 result records are preserved. D6/D7 are deterministic replays from D8's saved policy, not independent live model comparisons. Each stored record reports 50/536 served and 486 unserved; duplicated per-file token fields must not be summed as separate calls.
- V5 sidecar: union of 359 prior results and 53 current results equals 412 with no duplicate, missing, unexpected, or source-hash mismatch; 20,542 segments. No model calls, SUMO reruns, or source-result changes.
- Electricity price and complete shared candidate pool are unavailable; weighted rankings are disabled and route-choice economics remain blocked/null.

The two sidecar JSONL files are gzip-compressed losslessly. `archive_manifest.json` records each original and compressed SHA-256 and size; `SHA256SUMS` validates archived bytes. No raw prompts, state dumps, or traces are included. This checkpoint is not a complete matrix or final performance conclusion.
