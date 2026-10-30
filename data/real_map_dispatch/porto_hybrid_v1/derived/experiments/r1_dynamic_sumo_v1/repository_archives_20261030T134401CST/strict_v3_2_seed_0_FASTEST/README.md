# Lossless archive: SUMO battery output

This oversized simulation output is stored as zstd parts. Reassemble them in filename order, then decompress with `zstd -d`.

The source is a SUMO-generated experiment output from the Porto hybrid dataset. Attribution and source terms are recorded in `data/real_map_dispatch/porto_hybrid_v1/LICENSES.md` (UCI CC BY 4.0; OpenStreetMap ODbL).

`COMPRESSION_MANIFEST.json` records original and compressed SHA-256 values, part hashes, byte counts, and successful decompression verification.

```sh
cat battery.xml.zst.part-* > battery.xml.zst
zstd -d battery.xml.zst -o battery.xml
```
