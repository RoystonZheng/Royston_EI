# R4 formal batch progress checkpoint

Snapshot time: 2026-10-30 17:15 UTC (2026-10-30 01:15 CST)

This is a time-point status record, not a final experimental result. The source is the exact current output directory:
`/Users/didi/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/r4_codex_formal_batch_20261027_v2`

- Parent manifest SHA-256: `dc8f8ea93238df44cb406a4826f9a34e2f1085138e877b8242cca2be4a956fb9`
- Parent manifest updated: 2026-10-29 17:12:40 UTC
- User instruction in the current conversation: stop sending requests after the in-flight request finishes. At this snapshot, the parent reports `halt_no_more_model_requests=true` and parallel `in_flight=[]`.
- Request accounting: 12,097 model requests; 26 request-level delivery-unknown records remain isolated and are not retried.
- Frozen routeable task statuses: 58 route_complete, 15 failed, 10 outcome_unknown, 26 ready, 20 pending (129 total).
- All eight worker manifests report halt=true and pid=null. The exact source directory had no open file handles at the check.
- Candidate-pool generation, 4:6/6:4 profile selection with rationale, and EV2/SUMO binding for the selected route remain incomplete. The formal matrix is not complete; no aggregate effect claim follows from this checkpoint.

Completed routes, failures, unknown outcomes, legal incomplete prefixes, and pending tasks remain separate. This checkpoint contains no raw prompts, request traces, or active worker output.
