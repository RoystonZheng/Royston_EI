# Porto R1 v5 task checkpoint (20261027T1717CST)

Captured at 2026-10-27T09:17:57.122303+00:00 UTC from the frozen v5 SUMO matrix. The copied batch manifest is a stable timepoint snapshot with SHA-256 `dd84cc42c335dc9b2eb6d426f1362e6980881b660bdef910fa8b13f029cd9f3e`. At capture the batch status was `running` with 13 completed, 1 failed, 2 running and 4 queued of 20 tasks.

This archive adds 9 terminal tasks not present in the earlier repository checkpoints: `seed_02_lowest_energy`, `seed_03_fastest`, `seed_03_lowest_energy`, `seed_04_fastest`, `seed_04_lowest_energy`, `seed_05_fastest`, `seed_05_lowest_energy`, `seed_06_fastest`, `seed_07_fastest`. Each archive contains the task's original inputs, route/output records, audit, summary, motion trace and tripinfo. Source paths, uncompressed sizes and source SHA-256 values are recorded in `ARCHIVE_MEMBERS.tsv`; each tar.gz was read back and verified. All motion traces exceeding 50 MB are stored losslessly inside the compressed tar archives with their original-byte hashes recorded.

The matrix was still running at capture. Task-level completion and SUMO simulation traces do not establish a final matrix effect or a real-world result. Runtime logs, locks, PID files and live partial folders are excluded. The failed task is documented separately; its partial output folder and verbose log are not included.
