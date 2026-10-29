# NYC EV2, legacy and time-money checkpoint

Created 2026-10-29 13:49 CST. This archive carries the verified EV2 and legacy increments through the T1335 audit, with source and archived-payload SHA-256 values in `manifest.json`.

The latest official EV2 seed0 segment-ledger audit covers 302 of 630 method runs (302 valid, 328 missing, 0 invalid). The matching frozen-input audit covers all 302 with zero input-hash or scenario-cell mismatches. Seven of 90 scenario-fleet cells have all seven methods. Relative to T1326, T1335 adds one verified D8 result in `EV_20240330_200500_q75/F4`, with no prior result changes or removals. That F4 cell has four methods in the current audit; it is not a complete comparison and does not support an overall method ranking.

The complete single-cell evidence for `EV_20240330_200500_q75/F2` remains paired descriptive evidence only. Legacy `dispatch-agent-v1` seed3 `EV_20241111_200000_q25/F4` now has a verified seven-method audit and replaces the earlier partial-cell checkpoint in this archive. It remains separate from EV2 seed0.

The v5 offline sidecars add 3 source runs/162 segments (T1323), 1/52 (T1330), and 1/106 (T1335). The T1335 formula audit reports zero maximum billable-energy difference, matching source hashes, no source modifications, and no errors. Missing frozen prices and a common candidate pool keep route choice/profit blocked/null; there are no rankings, new model calls, or SUMO reruns from these offline sidecars. Coverage snapshots for 300, 301, and 302 source results are included to preserve the incremental chain.

The compressed tar contains only audited result JSON/JSONL and sidecar payloads. Raw prompt/context traces, live logs, mutable manifests, state files, and unreviewed outputs are excluded. No file exceeds 100 MiB; no Git LFS objects are needed.
