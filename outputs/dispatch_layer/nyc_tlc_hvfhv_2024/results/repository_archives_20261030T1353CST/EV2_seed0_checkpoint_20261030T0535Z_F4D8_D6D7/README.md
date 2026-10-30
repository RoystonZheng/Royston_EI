# NYC seed_0 checkpoint: 446/630 plus F4 D6/D7 replay audit

Captured 2026-10-30 13:53 CST from stable, individually verified source files. This package contains the official 05:35 UTC canonical union checkpoint (446 valid method runs, 184 missing, zero source/fingerprint mismatches), its time-money v5 sidecar, and a 05:33 UTC full-matrix ledger/fingerprint/trace audit. The sidecar completed route/segment accounting, but economic ranking remains blocked by incomplete tariff inputs; historical fare proxies are not current realized revenue.

The package also contains terminal F4 D6, D7, and D8 result JSONs and the 05:44:52 UTC D6/D7 subset ledger, fingerprint, and unique-trace audits. The D6/D7 subset covers 90/180 method results (90 missing), has zero frozen-input mismatches, and maps 90 references to 45 shared unique traces with no trace errors. The F4/D5 run was still active at capture time and is intentionally excluded; this is a point-in-time partial checkpoint, not a complete matrix or a comparative-effect conclusion.

Raw prompts, model traces, active logs, and process state are excluded. Absolute local workspace paths in copied metadata were replaced with `[WORKSPACE_ROOT]`; the source and published hashes are both recorded in `PACKAGE_MANIFEST.json`.

`SHA256SUMS` covers all package files except itself.
