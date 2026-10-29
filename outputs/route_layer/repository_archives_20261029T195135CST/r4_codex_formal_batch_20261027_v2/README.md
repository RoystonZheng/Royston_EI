# R4 formal batch progress snapshot

Captured: 2026-10-29T19:53:08+08:00 (2026-10-29T11:53:08+00:00) from the exact App Support output directory. The parent manifest state was `paused_at_parallel_checkpoint`. This is a point-in-time status record, not a final experimental result.

At capture, `formal_driver.pid` existed but its process was not alive, and no worker execution lock was open. Parent and worker manifests remain separate scopes. Their request counts and task states are not summed.

Only aggregate status counts, manifest SHA-256 values, and lock/PID presence observations are stored. Raw manifests, task rows, prompts, traces and runtime state are excluded.
