# NYC terminal-method checkpoints (20261027T1647CST)

This directory preserves individually completed method outputs collected at the timestamp in each archive name. Each JSON passed a stable-read check, was not open by a process during capture, declares `run_mode=complete_simulation`, `partial_run=false`, `performance_claim=false`, and has zero round-validation errors. Archive members were verified against source SHA-256 values after compression.

| Archive | Branch | Case | Fleet | Methods present / expected |
|---|---|---|---|---:|
| `nyc_seed0_official_F4_20261027T1647CST.tar.gz` | official seed0 EV v2 | `EV_20240411_200000_q50` | F4 | 6/7 |
| `nyc_seed3_replicate_F4_20261027T1647CST.tar.gz` | seed3 replicate | `EV_20240624_210000_q25` | F4 | 6/7 |
| `nyc_seed4_legacy_F4_20261027T1647CST.tar.gz` | seed4 legacy replicate | `EV_20241107_214500_q50` | F4 | 7/7 |
| `nyc_seed4_legacy_F1_20261027T1647CST.tar.gz` | seed4 legacy replicate | `EV_20241115_224000_q75` | F1 | 7/7 |
| `nyc_seed4_legacy_F2_20261027T1647CST.tar.gz` | seed4 legacy replicate | `EV_20241115_224000_q75` | F2 | 3/7 |


The three tracks remain separate: official EV v2 seed0, seed3 replicate, and seed4 legacy replicate. The seed0 and seed3 F4 snapshots contain 6/7 methods; seed4 legacy F4 and F1 contain 7/7; seed4 legacy F2 contains D0, D1, D2 (3/7). These are cell-level checkpoints, not a complete multi-month matrix, cross-month comparison, ranking, or overall-effect result. The source batches were still active at collection time; no live progress logs, prompts, traces, locks, or PID files are included.
