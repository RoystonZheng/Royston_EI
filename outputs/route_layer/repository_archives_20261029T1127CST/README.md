# R4 formal checkpoint — 2026-10-29

This is a time-stamped partial checkpoint.

- The aggregate directed-flow audit validates 37 completed routes and 6,020 directed edges against its recorded parent-manifest snapshot. It is an integrity audit, not a performance claim.
- Two later terminal route audits are included separately because they are outside that aggregate gate.
- The current parent remains running. Worker manifests include active workers and unresolved delivery records; its pause reason conflicts with the running state.
- EV2 payload-phase v14 and v15 are offline sidecars with zero model calls, zero errors, and no route reruns. Missing frozen tariff blocks cost ranking.

See manifest.json, the progress note, and SHA256SUMS for paths and hashes.
