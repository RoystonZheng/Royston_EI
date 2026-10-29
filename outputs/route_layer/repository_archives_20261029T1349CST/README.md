# R4 verified route checkpoint

Created 2026-10-29 13:49 CST. This package combines the latest reviewed route-layer audit and payload-phase EV2 recalculations with three stable terminal task audits from the exact App Support R4 formal-batch directory.

The full-flow audit `r4_full_flow_audit_20261029T1400CST_repo_relative.json` validates 49/49 completed routes, 8,438 directed edges, and zero violations. Its source parent snapshot SHA is recorded in the audit; the raw parent snapshot is intentionally excluded because it contains live events and trace references. The separate `parent_status_summary_20261029T1349CST.json` is a timepoint-only summary of the parent manifest and workers.

At the captured parent snapshot the formal batch remained `running`; parent task counts are recorded separately from worker counts. The three newly included single-task bundles are route-complete and have terminal audits with empty `invalid_or_stopped_actions`: seed 4 `task-0004-0003`, seed 6 `formal-task-0006-feasible`, and seed 1 `task-0001-0008`. Their usage reconciliation is included. These task-level route results do not mean the parent formal batch has completed and do not support cross-task performance ranking.

The T1345 and T1400 payload-phase manifests passed with zero errors and zero model calls. Their run JSONs record offline EV2 recalculations; cash cost remains null. Existing Porto single-pilot evidence and previously terminal task bundles are retained.

Raw prompt/context traces, mutable states/manifests, attempt files, replay HTML, PID/lock files, and unrelated App Support contents are excluded. The full App Support tree was not scanned. No file exceeds 100 MiB; no Git LFS objects are needed.
