# NYC EV2 v4 seed 0 partial audit checkpoint — 2026-10-28 21:26 CST

This is a time-stamped intermediate checkpoint, not a completed experiment. The source audit reports 233 of 630 method-runs present and valid, 397 missing, and 0 invalid. The runner was still active at capture time.

The seven method archives contain those 233 completed result JSON files, grouped by method. Archive compression is lossless over the sanitized JSON payloads. For each file, manifest.json records the source SHA-256 and size, sanitized JSON SHA-256 and size, and any removed prompt-path or stderr fields. Absolute local paths were made repository-relative.

The repository-relative audit JSON preserves the source audit data while converting local absolute paths. Its SHA differs from the source audit SHA; both values are recorded in manifest.json.

Run shasum -a 256 -c SHA256SUMS in this directory to verify delivered files. Extract method bundles with tar -xzf <archive-name> -C <destination>. Do not infer whole-matrix method performance from this incomplete checkpoint.
