# Porto R1 segment economics v5 sidecar archive

This archive preserves the completed offline segment-ledger v5 sidecar. It records 3,800 segments (1,900 deadhead and 1,900 passenger segments), makes no model calls or SUMO reruns, and does not modify source results or the runtime selector. The decision gate remains blocked because no same-snapshot complete candidate pool or passenger fare schedule is available; no electricity tariff is frozen. Link-level energy attribution is unavailable for all segments.

`SOURCE_COMPRESSION.tsv` records source and compressed sizes and SHA-256 values. The compressed JSONL was decompressed and verified against the source SHA-256. This is an accounting audit, not a runtime selector result or profitability claim.
