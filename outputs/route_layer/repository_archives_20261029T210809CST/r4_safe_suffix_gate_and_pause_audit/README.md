# R4 route-gate and safe-pause evidence

Captured 20261029T210809CST (Asia/Shanghai). This package groups read-only route audits and a zero-request safe pause record.

- Directed-edge audit: 56 completed routes checked across 102 states; 0 directed-edge violations across 11,446 accepted edges.
- Actual suffix audit: 56/56 completed routes passed.
- Preflight checks: 29 ready states and 27 pending initial states passed at their recorded timestamps.
- Release/checkpoint records confirm 0 model requests during release, no unknown-task replay, and a safe pause at request count 11,799. The formal parent remains paused; these checks do not establish complete SUMO validation or final experiment completion.

The source files are independently hashed in `manifest.json`. Local absolute paths were normalized; prompts, credentials, raw traces, and backups are excluded.
