# GitHub sync archive

Contains a frozen NYC EV2 seed0 audit checkpoint (398/630 at 2026-10-29 15:45:29 UTC) and two offline R4 EV2 sidecars: a 57-row base plus a one-row delta. The NYC batch was still running at that checkpoint, so it is not a final matrix or ranking. The R4 EV2 sidecars made no model calls or route reruns.

A later control audit found that an automatic R4 resume event had cleared worker_02’s halt flag. With no in-flight request, runner, aggregator, lock holder, or open handle present, the parent, parallel scheduler, and all eight workers were re-locked at 2026-10-29 16:10:42 UTC. The sanitized stop snapshot is in the adjacent `../repository_archives_20261030T001042CST/r4_stop_relock_20261029T161042Z/` archive.

Subdirectories include manifests and SHA-256 inventories.
