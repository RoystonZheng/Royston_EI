# R4 EV2 payload-phase archive (57-row base plus one-row delta)

This archive preserves two passed offline EV2 recalculation sidecars: a 57-row authoritative base and its one-row delta. It contains the dated manifests and JSON outputs only; no prompt, context trace, raw model trace, or live R4 state is included. The recalculation made no model calls or route reruns.

The original sidecars did not record the energy module SHA at calculation time. A post-hoc binding to current source bytes cannot prove which bytes were imported during the original recalculation. Treat this as an offline audit artifact, not a complete R4 experiment or performance ranking.

See `archive_manifest.json` and `SHA256SUMS` for file hashes.
