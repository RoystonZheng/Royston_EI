# R4 external manifest observation — 20261027T175310CST

The exact external root path currently reports `running` with 4326 model requests, 19 unresolved delivery/usage outcomes, and task states `{"active": 6, "pending": 97, "ready": 11, "route_complete": 15}`. The source manifest was modified at 2026-10-27T17:52:58+08:00 and has SHA-256 `a5b6ca1d542368ee26712805a02e8264dd338068f21bf7c6d57477fabe69f65f`. Worker manifest hashes and timestamps are recorded in `manifest_observation.json`; this file contains no raw prompts, responses or authorization data.

The root `.r4_model_execution.lock` names PID 13960, which was not alive at the recorded process check. Worker manifests were updating at capture. The same external root has also returned older-mtime/lower-count manifest versions between fresher versions; see the recorded sequence. Treat the lock/manifest mismatch as an operational anomaly and keep checking this exact path. No active task output is archived as terminal.
