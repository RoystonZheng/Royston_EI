# Seed 4 legacy audit and incremental results — 2026-10-28 07:58Z

This is a stable point-in-time research archive. It contains a one-seed legacy audit, three individual completed results, and a sanitized status snapshot from the exact monitored R4 application-support directory. It is not a terminal matrix archive or a performance claim. Source files in the workspace remain unchanged.

## Seed 4 legacy audit

The report was generated at 2026-10-28T07:50:19Z for 30 quantile-stratified dates, three fleets, and seed 4: 84/90 paired cells meet the full completeness, input-match, seed-consistency, validator, and replay-audit gates; 615/630 method-result files are complete, leaving six paired cells incomplete. This one-seed legacy snapshot is separate from the two-seed main audit (142/180 paired cells and 1,017/1,260 files) archived earlier. Do not add their counts. Selected dates are not a random sample of all NYC dates; trips and synthetic fleets are proxy inputs, so this does not establish citywide effects. Token counts are not account billing.

## Individual results

Two EV2 outputs (D1 and D2, seed 0, July 2 F4) are complete and postdate the last 07:13Z EV2 sidecar; they are not included in its 134/630 coverage. The seed 3 multimonth D6 F4 output is complete and postdates the 07:20Z two-seed audit; it remains separate until an updated aggregate audit binds its hash.

## R4 progress

The parent remains paused at its parallel checkpoint; worker 00 reports running with an in-flight request and `stopped_request_error`. Current task counts are preserved in `r4_app_support/progress_status.json`; parent and worker scopes differ and their counts are not additive. Two lock files were open during the check. Raw task outputs, manifests, prompts, traces, logs, and process IDs are excluded while the worker is active.

## Sanitization

CSV/JSON result paths are normalized, raw prompt/message/trace fields are removed, and local absolute paths and credential patterns are checked. All files are below 100 MB; no Git LFS object is introduced. `CHECKPOINT.json` records source and archive SHA-256 values; `SHA256SUMS` covers each archive file except itself.
