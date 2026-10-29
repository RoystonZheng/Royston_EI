# R4 formal batch safety-gate snapshot

Captured 2026-10-29T20:06:29+08:00 (2026-10-29T12:06:29+00:00) from the exact App Support output directory. The parent manifest was `paused_user_requested_safety_gate` and `halt_no_more_model_requests=true`; this is a progress record, not a final result.

All worker execution locks were closed. The formal driver PID file was present but its process was not alive. Parent and worker request counts are kept separate and are not summed.

The snapshot stores aggregate status counts, manifest SHA-256 values, and lock/PID presence only. It excludes raw manifests, task rows, prompts, traces and runtime state.
