# Route decision segment v5 usage-gate archive

This archive preserves the completed offline sidecar: 20 compressed source result files across 10 seeds, covering 150 tasks, with zero model calls. `manifest.json` and `progress.json` are copied from the source. `SOURCE_COMPRESSION.tsv` records original and compressed SHA-256 values and byte counts; every compressed file was decompressed and hash-checked.

The 150 tasks have two profiles each: 130 tasks are blocked by incomplete economic inputs (260 blocked profile rows), while 20 tasks have no feasible candidates (40 profile rows). The categories remain separate. This offline accounting sidecar does not rerun routes or models, alter the source route results, or support a method-effect claim.
