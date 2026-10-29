# NYC EV2 v4 seed 0 checkpoint — 2026-10-29 11:50 CST

This is a partial audit checkpoint, not a final matrix or performance conclusion.

- The full matrix audit validates 269 of 630 method runs; 361 are missing and none of the present runs are invalid.
- The independent frozen-input audit covers the same 269 results with no contract or fingerprint mismatch. The D2-specific audit separately validates 86 of 90 D2 runs; four remain missing.
- The compressed increment contains 11 terminal result JSON files absent from the verified 11:09 CST remote checkpoint. Their original SHA-256 values and sizes are recorded in `manifest.json`; tar compression is lossless.
- This remains a simulation checkpoint. It does not establish a full-matrix ranking or performance effect. Energy is simulated, actual account billing is unknown, and the tariff/time-money ranking remains blocked.

See `manifest.json` and `SHA256SUMS` for exact source/archive hashes.
