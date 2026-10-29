# NYC EV2 seed_0 incremental checkpoint

Captured 2026-10-29T20:16:12+08:00 (2026-10-29T12:16:12+00:00). The parent experiment still has running processes; this is a partial checkpoint, not a final experiment result. It contains 12 stable individual result files with `partial_run=false`. 4 were written after the latest paired audit and are recorded separately; they are not included in that audit's 346-run count.

The paired audit at 2026-10-29T12:00:01+00:00 reports 346/630 valid method runs, 284 missing and 0 invalid. The paired fingerprint audit hashes back to the official audit, covers 346 results, and reports zero scenario/input mismatches. The official audit says the run set is not complete and valid as a whole. This checkpoint makes no aggregate performance or ranking claim.

The included economics sidecar is the distinct delta from 333 to 337 results: its source manifest says `completed_segment_economics_route_choice_blocked`, four new results, zero new model calls and no dispatch replay. Its superseded pre-shared-usage intermediate is excluded.

Source: NYC Taxi and Limousine Commission, 2024 High Volume For-Hire Vehicle Trip Data: https://data.cityofnewyork.us/Transportation/2024-High-Volume-FHV-Trip-Data/bqxa-629f and https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page. The local source manifest lists the license as unspecified. This archive contains simulation results and audit artifacts, not raw Parquet data; see https://data.cityofnewyork.us/stories/s/Terms-of-Use/k9k7-3cje. `manifest.json` records both original and sanitized hashes.

Local `user_config`, credential, prompt, raw request/state fields were removed; local absolute paths were converted to repository-relative references or redacted.
