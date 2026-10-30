# Windows migration package — 2026-10-30

Latest assembled at 2026-10-30 16:16:01 CST. Main branch contains the project code and baseline data; these files carry checkpoint and research-output deltas that were not in the remote tree.

## Package files

- `NYC_TLC_dispatch_seed0_20261030T0734Z.zip` — 15:34 historical base bundle; SHA-256 `706b2e46cb1d4aa8b74bd0708cd9ba8577a5fdcd32cc053885205e77eb939380`. It is not the newest checkpoint by itself.
- `NYC_TLC_seed0_checkpoint_overlay_20261030T161205.zip` — latest NYC overlay, 48 files / 32,271,622 source bytes, SHA-256 `14ccc08554b13426fb3d4c50a7dc932b35a7a539ae72eaf3a9f4e6d2e2ec8e69`. It preserves the official manifest/progress byte-for-byte and records the independent stop state.
- `R4_formal_checkpoint_full_20261030T161453.tar.zst` — full R4 formal batch checkpoint from Application Support; SHA-256 is in the matching `SHA256SUMS.txt` in the release.
- `R4_link_fallback_resumption_20261030.zip` — checkpointed R4 link-fallback runs; SHA-256 `75e1fd802dd5c363e144e276eb8c7f072535bfa2f60023135212faf166844f91`.
- `unaligned_research_outputs_20261030.tar.zst.part-001` through `part-005` — 30,113 stable files, 10,466,256,810 source bytes, split into five verified parts totalling 383,053,665 bytes. Per-part SHA-256 values are in `ARCHIVE_MANIFEST.json`.
- `R4_formal_checkpoint_20261030_sanitized.zip` — sanitized convenience snapshot; the full raw checkpoint is the `.tar.zst` release asset.

## Windows restore

1. Clone `RoystonZheng/Royston_EI` and extract the NYC historical base bundle into the project copy.
2. Extract the NYC checkpoint overlay. Run its `RESTORE_WINDOWS.ps1 -ProjectRoot <project-root>` from PowerShell. It verifies hashes and copies files; it does not start a runner.
3. For the stable outputs archive, run `copy /b part-001+part-002+part-003+part-004+part-005 unaligned_research_outputs_20261030.tar.zst`, then decompress with `zstd` and extract with `tar`.
4. Extract the R4 archives into separate checkpoint folders. Read each `STOP_AND_MIGRATION_STATE.json` before making a future run decision.

## Current stop boundary

The Windows checkpoint is frozen. NYC F1/D5 has 15 raw round traces and no official result; NYC F2/D8 has a completed raw response trace but no official method result. R4 parent and worker manifests retain their original values even where the process scan found no runner. Do not replay uncertain or completed requests, and do not treat a partial route prefix as a completed result.
