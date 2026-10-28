# Incremental research checkpoint — 2026-10-28 07:41Z

This archive contains sanitized copies of stable, useful experiment artifacts captured during the GitHub synchronization monitor. It is a progress checkpoint, not a terminal batch archive or a performance claim. Raw workspace sources remain unchanged.

## Frozen multimonth audit snapshot

The audit snapshot at `2026-10-28T07:20:09Z` records 142/180 complete, input-matched, validator-valid paired cells and 1,017/1,260 result files; 38 paired cells remain incomplete. D8/F2/seed3 is SHA-matched to this snapshot. D5, D6, D7, and F4/D0 were generated later and remain individual results until a new aggregate audit.

## EV2 time/money sidecars

The full sidecar contains 134 run rows and 7,552 segment rows, with 134/630 valid method runs in its aggregate audit. The refresh contains 53 new run rows and 3,100 segment rows; its 53 run-pair keys are a strict subset of the full sidecar. The refresh manifest carries the same cumulative 134/630 audit coverage, so the two audit counts must not be added. Both record zero new model calls. EV2 D1 and D2 were generated after this sidecar snapshot and are listed separately. Missing tariffs keep electricity costs null; the time/money route-choice gate remains blocked.

## Current R4 progress

The parent manifest is `paused_at_parallel_checkpoint` with counts `failed=35, outcome_unknown=5, pending=62, ready=6, route_complete=21` and 24 unresolved in-flight entries. Worker 00 is `running` with counts `active=1, failed=6, outcome_unknown=8, pending=9, route_complete=5` and 9 unresolved entries. The scopes differ; counts are not additive. Worker 00 is active, so neither status is terminal. Only allowlisted counts, statuses, timestamps, and hashes are included; no raw task manifests, prompts, traces, or logs are archived.

## Sanitization and omissions

Raw prompts, messages, trace contents, credentials, user-specific absolute paths, active task outputs, progress logs, and the live R4 output tree are omitted. Structured metrics and safe SHA-256 provenance are retained. Every archived file is below 100 MB; no Git LFS object is introduced. See `OMISSIONS.tsv` and `CHECKPOINT.json` for the file-level record.
