# EV2 time-money v5 historical sidecar (354-run snapshot)

Losslessly compressed JSONL outputs from the 2026-10-29 12:33 UTC sidecar. The sidecar records 354 valid of 630 expected method runs, 276 missing, zero new model calls, and status `completed_segment_economics_route_choice_blocked`.

This is a historical, incomplete accounting artifact. Its economics are blocked by missing source inputs, and trace usage attribution requires deduplication. Do not use aggregate cost or route-choice fields for method ranking or a performance claim. A later official audit covers 360/630; this sidecar remains at 354 rows.

The `.jsonl.gz` files use deterministic gzip (`mtime=0`). The manifest records raw source SHA-256, normalized uncompressed SHA-256, compressed SHA-256, row counts, and sizes. Paths were rewritten to workspace-relative references; sensitive prompt/trace/config values and local absolute paths were removed. Decompress with `gzip -dc <file>.jsonl.gz`.
