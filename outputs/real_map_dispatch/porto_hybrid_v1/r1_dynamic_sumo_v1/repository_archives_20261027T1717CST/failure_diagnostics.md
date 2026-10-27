# Porto R1 v5 failed task checkpoint

Capture time UTC: 2026-10-27T09:17:57.122303+00:00

The failed task manifest row is preserved in `batch_manifest_snapshot.json`. Its partial output directory and verbose runtime log are not copied.

Task ID: `seed_02_fastest`
Seed/objective: `2` / `FASTEST`
Exit code: `1`
Manifest error: `runner_exit_nonzero_or_summary_missing`
Runtime log bytes: 1098; SHA-256: `b39f6d0aaa112a632cb3b2aa49b1759f1e84d359cb301db3add682c3f9a1370f`.
Terminal exception: `    raise RuntimeError("SUMO run exceeded 24 simulated hours") RuntimeError: SUMO run exceeded 24 simulated hours`

This is failure evidence for a still-running matrix, not a performance result.
