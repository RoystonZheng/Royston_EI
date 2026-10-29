# NYC multi-month seed_3 incremental checkpoint

Captured at the archive directory creation time. The parent batch remains running. This archive adds 11 stable, individually terminal result JSON files (15,839,490 source bytes). These files do not make the overall batch complete and support no aggregate performance, ranking or cross-month claim.

The new results complete the seven-method file sets for `EV_20241202_210000_q50/F1` and `EV_20241222_205500_q25/F4`. `EV_20241202_210000_q50/F2` has three method files in this delta and remains incomplete. File-set completeness is structural; it is not an outcome or effectiveness conclusion.

This archive contains simulation outputs, not raw trip-data Parquet. The local source manifest lists the NYC data license as unspecified. Source: NYC TLC 2024 HVFHV data and NYC TLC trip-record page: https://data.cityofnewyork.us/Transportation/2024-High-Volume-FHV-Trip-Data/bqxa-629f and https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page. NYC Open Data terms: https://data.cityofnewyork.us/stories/s/Terms-of-Use/k9k7-3cje.

Original and sanitized SHA-256 values are recorded per file in `manifest.json`. Local user configuration and prompt-path fields were removed; absolute local paths were converted to workspace-relative references or redacted.
