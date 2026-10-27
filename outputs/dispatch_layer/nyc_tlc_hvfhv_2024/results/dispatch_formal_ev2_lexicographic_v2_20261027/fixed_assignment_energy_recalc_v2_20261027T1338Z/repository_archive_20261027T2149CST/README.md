# NYC fixed-assignment EV v2 offline recalculation — 2026-10-27

The frozen manifest records 135 source result records, 125 recalculated and included, 10 excluded, zero unrecomputable, and complete output coverage. `v2_integrity_audit.json` passed for 125/125 physical outputs with zero errors, zero model calls, and `dispatch_policy_rerun=false`. The recalculation script SHA-256 matches the manifest. Source-result hashes are listed in `INPUT_SOURCES.tsv`; 125 output JSON files are under `outputs/`.

`pickup_energy_coverage_audit_v2.json` validates the saved initial candidate tables: 125 rounds, 11076 candidate rows, 11076 with the pickup-energy gap, 397 selected pairs, 397 exact one-candidate matches, 397 selected pairs with that gap, and 0 alignment errors. This covers the saved initial candidate tables; it does not extend to later rounds.

These are offline fixed-assignment post-hoc energy calculations. They do not rerun dispatch policy, establish that a live selector used EV v2, or provide a complete paired performance result. The older failed-schema audit and the unverified T132501 coverage audit are intentionally not included; this package preserves the v2 audit and its own source hashes.


The v2 integrity audit binds to the partial official audit at `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_ev2_lexicographic_v2_20261027/partial_audit_checkpoint_20261027T1335Z/run_audit.json` (SHA-256 `2eb0c4cdd5dafa4b78bb08c0f2f11d559ca349b227b8fc4f22b61bc97cce37bd`), which reports 17 of 90 fully valid paired cells. That source audit is retained at its original repository path in the same sync.
