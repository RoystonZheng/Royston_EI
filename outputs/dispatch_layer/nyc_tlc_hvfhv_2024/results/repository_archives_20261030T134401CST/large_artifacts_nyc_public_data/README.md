# Lossless archive: NYC D1 optimizer labels

This archive stores the oversized experimental result as zstd parts so each GitHub file remains below the per-file limit. Reassemble the parts in filename order, then decompress with `zstd -d`.

The source is the project-generated D1 optimizer label result derived from NYC TLC 2024 High Volume FHV Trip Data. Source dataset: https://data.cityofnewyork.us/Transportation/2024-High-Volume-FHV-Trip-Data/bqxa-629f. The NYC Open Data record identifies the City of New York/TLC as the provider; preserve that attribution and the source/version when reusing. These labels are training inputs, not an evaluation result or performance claim.

`COMPRESSION_MANIFEST.json` records original and compressed SHA-256 values, every part hash, exact byte counts, and successful recombination/decompression verification.

Reconstruct from this directory:

```sh
cat D1_optimizer_seed_0.json.zst.part-* > D1_optimizer_seed_0.json.zst
zstd -d D1_optimizer_seed_0.json.zst -o D1_optimizer_seed_0.json
```
