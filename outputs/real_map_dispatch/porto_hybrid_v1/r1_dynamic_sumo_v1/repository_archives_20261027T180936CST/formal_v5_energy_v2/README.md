# Porto R1 formal v5 EV v2 output archive

This archive preserves the terminal native outputs for 19 completed SUMO runs and the partial artifacts for the failed `seed_02_fastest` run. Files at or above GitHub's 50 MiB individual-file ceiling were gzip-compressed losslessly; `SOURCE_COMPRESSION.tsv` maps every archived file to its source path, size, SHA-256, archive path, size, SHA-256, and method.

The source batch is `completed_with_gaps`: 19 of 20 runs completed, with 9 complete paired seeds. The failed run is kept as a failure audit and partial simulator artifacts; it is not included in paired metrics. `matrix_summary.json`, `batch_manifest.json`, and `completion_integrity_audit_v2.json` remain at the source data path `data/real_map_dispatch/porto_hybrid_v1/derived/experiments/r1_dynamic_sumo_v1/formal_v5_energy_v2`. The integrity audit passed and its recorded manifest and matrix hashes match those files.

These are SUMO simulation outcomes on a real OSM-derived Porto graph with synthetic background traffic, not observed fleet operations. Paired differences are descriptive (LOWEST_ENERGY minus FASTEST), have no inferential test or causal claim, and exclude the failed pair. Electricity cost is not reported because a frozen tariff is absent.

Restore a compressed trace with `gzip -dc <file>.json.gz > <file>.json`; verify the extracted source using `SOURCE_COMPRESSION.tsv`.
