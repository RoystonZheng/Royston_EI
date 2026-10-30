# NYC seed-0 F1 completed-cell checkpoint

This immutable checkpoint captures the official audit set completed at 2026-10-30 04:16:27 UTC (12:16:27 CST). It contains the 429/630 seed-0 ledger and matching fingerprint, trace-usage, and time-money v5 canonical-union audits, plus the seven method outputs for `EV_20240725_211000_q50` / `F1`.

The F1 cell has 12 served orders out of 482 for D5-D8; the D5 usage record contains 114,106 tokens across seven CLI calls. D6 and D7 replay the saved D8 policy result and are not additional live calls. These are single-cell descriptive results, not a matrix-level performance conclusion. The audit snapshot has 429 valid results, 0 invalid, and 201 missing. The v5 sidecar makes no economic ranking because tariff and a shared feasible route pool are unavailable.

The two v5 JSONL exports are gzip-compressed losslessly. `manifest.json` records original and stored SHA-256 values, and the compressed streams were round-trip checked. Raw prompts, context traces, worker state, and the later F2/D8 in-progress result are excluded.
