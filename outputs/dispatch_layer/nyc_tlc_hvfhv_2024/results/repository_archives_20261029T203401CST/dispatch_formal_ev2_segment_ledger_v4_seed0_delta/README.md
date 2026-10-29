# NYC EV2 seed_0 incremental checkpoint

Captured 2026-10-29T20:34:01.612085+08:00. The parent batch remains active. This is a partial checkpoint, not a final matrix result. It adds 5 individually terminal result JSON files after the earlier checkpoint, plus the latest paired audits. Each copied method result had `partial_run=false`, stable double reads, and no open write handle.

Latest paired audit: 352 valid, 278 missing, 0 invalid of 630 expected runs. The all-run completion flag is false. The paired input fingerprint audit covers 352 results with zero scenario/fingerprint or input-hash mismatches. 3 method result file(s) in this delta postdate that audit and are not included in its 352 count. No ranking or overall performance claim is made.

This archive contains simulation outputs and sanitized audit snapshots, not raw trip-data Parquet. The local source manifest lists the NYC data license as unspecified. Source: NYC TLC 2024 HVFHV data and NYC TLC trip-record page: https://data.cityofnewyork.us/Transportation/2024-High-Volume-FHV-Trip-Data/bqxa-629f and https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page. NYC Open Data terms: https://data.cityofnewyork.us/stories/s/Terms-of-Use/k9k7-3cje.

`manifest.json` keeps original-source and sanitized-archive hashes. Local user configuration and prompt-path fields were removed; absolute local paths were converted to workspace-relative references or redacted.
