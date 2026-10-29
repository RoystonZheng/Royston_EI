# NYC EV2 seed-0 incremental checkpoint

The newest included paired official audit and input-fingerprint snapshot is 2026-10-29 07:38 UTC. Eight complete, hash-matched result files were added since the GitHub snapshot at 07:03 UTC. All eight pass the official row audit and the paired input fingerprint checks.

Coverage remains incomplete: 333 of 630 runs are present and valid, 297 are missing, and 15 of 90 scenario-fleet cells contain all seven methods. This checkpoint does not support a full-matrix ranking or a final effect claim. Four completed economics sidecar deltas cover 20 accounting rows; all 20 are blocked by incomplete economic inputs. They made no SUMO reruns and did not modify source results.

Paths are repository-relative. Result and sidecar payloads are sanitized; source and archived hashes are recorded in manifest.json and SHA256SUMS.
