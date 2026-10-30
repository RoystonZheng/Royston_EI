# Research checkpoint

Zero-call directed-road and SUMO acceptance replay for an already completed route. The 172-edge seed3 one-way gate was validated separately; this artifact replays the archived seed0 task-0000-0009 route with 113 accepted edges, zero direction violations, one pickup/dropoff stop each, and matching directed edge sequence. SUMO 1.28.0 completed the vehicle route at 11,096.57 m in 1,084.5 s. These are simulator checks, not new model samples or route-method performance results. The raw preflight contains rendered prompt/message fields, so only its status and source/map hashes are retained in `pilot_preflight_summary.json`.

Exact source-to-archive paths, original SHA-256 hashes, and compression metadata are in `archive_manifest.json`. `SHA256SUMS` verifies archived files; `.gz` entries decompress byte-for-byte to their recorded source hash.
