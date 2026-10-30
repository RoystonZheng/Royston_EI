# Research checkpoint

One terminal NYC multimonth seed3 D5 cell (EV_20241214_224500_q75/F4) with a separately verified, fixed-assignment offline EV2 v2 recalculation. The original result records 559 completed simulation rounds and 48 assigned orders out of 623 (575 unserved); it has 24 CLI invocations with 11 failed turns, including HTTP 429, and performance_claim=false. The EV2 sidecar integrity audit passes for the one output, makes 0 model calls, does not rerun dispatch policy, and recalculates 48 assigned orders. This is a single cell with 629 expected method results missing, not a complete seed3 matrix or an EV2 energy-optimized policy result. No prompt bodies, raw traces, progress logs, or state files are archived.

Exact source-to-archive paths, original SHA-256 hashes, and compression metadata are in `archive_manifest.json`. `SHA256SUMS` verifies archived files; `.gz` entries decompress byte-for-byte to their recorded source hash.
