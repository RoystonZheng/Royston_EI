# Route/EV2 shared-link SUMO batch archive

Batch status at capture: `passed`; the frozen batch manifest reports 129/129 task records passed and 0 failed/blocked. The separate task-level manifest files present in the output also passed. This is a completed simulation batch and file-validation result; it does not establish a comparative route-performance, dispatch, production, or real-vehicle effect.

The archive contains 1,222 eligible JSON/XML experiment files (11,311,239,178 source bytes). The 528 text/log outputs, lock/PID files, raw prompt/state/trace material, and temporary files are excluded. The archive manifest records each source and stored-file SHA-256, byte size, and private-path substitutions.

## Restore and verify

If the archive is split into parts, concatenate them in numeric order, then run `zstd -t` before extraction:

```sh
cat r4_all_shared_link_profiles_sumo_ev2_20261030_v1.tar.zst.part-* > r4_all_shared_link_profiles_sumo_ev2_20261030_v1.tar.zst
zstd -t r4_all_shared_link_profiles_sumo_ev2_20261030_v1.tar.zst
tar --use-compress-program=zstd -xf r4_all_shared_link_profiles_sumo_ev2_20261030_v1.tar.zst -C <checkout>
```

Each part is at most 90 MiB. Verify extracted member hashes against `ARCHIVE_MANIFEST.json`.
