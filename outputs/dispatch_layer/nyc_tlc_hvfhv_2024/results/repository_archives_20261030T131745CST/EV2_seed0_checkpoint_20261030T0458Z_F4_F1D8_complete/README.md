# NYC seed_0 EV2 checkpoint: 438/630

Scope: the official seed_0 ledger snapshot at 2026-10-30 04:58 UTC, its paired input-fingerprint audit, the 05:01 UTC unique-trace audit, and the complete F4 case `EV_20240725_211000_q50` (D0/D1/D2/D5/D6/D7/D8) plus the F1/D8 result used by that checkpoint.

The official ledger contains 438 valid results, 0 invalid results, and 192 missing results out of 630 expected. The paired fingerprint audit reports 438 audited inputs with no scenario or input-hash mismatches. The unique-trace audit records 1,184 references, 1,105 unique traces, and 79 duplicate references. This is a partial matrix checkpoint, not a completed experiment or a method-effect conclusion.

The time-money v5 sidecar is a post-hoc calculation for the same 438 source results, with 430 newly derived run rows and 24,924 segment rows relative to the 433-result predecessor. It made no model calls and did not rerun SUMO. Complete common candidate-pool coverage and usable tariff inputs remain unavailable, so economic rankings remain blocked/null. Actual account charges are unavailable.

The later F1/D5, F1/D6, F1/D7 and F2/D8 individual outputs are stored separately in the sibling `post_438_terminal_individuals_not_in_official_audit` directory. They are not included in this official 438 ledger/fingerprint snapshot. Raw prompts, context-policy prompt files, CLI/context traces, live logs, and the active F2/D6-D7 replay are excluded. Result JSONs retain source audit metadata and hashes only; no raw trace files are included.

`time_money_v5/compression_verification.json` records source and gzip hashes, sizes, and byte-exact decompression verification. `SHA256SUMS` covers every file in this checkpoint except itself.
