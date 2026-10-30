# NYC EV2 seed0 checkpoint (2026-10-29 16:31:20 UTC)

Frozen point-in-time audit while the 630-run NYC batch continued. The active D6/D7 replay output is not included.

- Official ledger: 409/630 valid, 0 invalid, 221 missing.
- Paired input/scenario fingerprint: 409 results, no mismatches.
- Unique trace audit: 681 references, 616 unique trace files, 65 duplicate references, 35 shared groups; account charges are unavailable.
- V5 sidecar: union of 50 prior results and 359 current results equals 409 with no duplicate, missing, unexpected, or source-hash mismatch; 20,542 segments. No model calls, SUMO reruns, or source-result changes.
- Electricity price and complete shared candidate pool are unavailable; weighted rankings are disabled and route-choice economics remain blocked/null.

The two large JSONL sidecar files are gzip-compressed losslessly. `archive_manifest.json` records each original and compressed SHA-256 and size; `SHA256SUMS` validates archived bytes. This checkpoint is not a complete matrix or final performance conclusion.
