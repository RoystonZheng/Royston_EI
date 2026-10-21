# NYC TLC multi-vehicle competition scenarios v1

Derived scenario inputs built from the audited January request trace. The raw TLC files and source trace were not modified. Synthetic vehicles are deliberately colocated with observed pickup zones to create known candidate overlap at zone resolution. No NYC road ETA or real fleet state is implied. These are scenario inputs, not performance results.

See `scenario_manifest.json` and each scenario's `scenario.json`. `orders.json` uses the dispatch experiment's order field names; `orders.parquet` preserves source provenance.

C2's bounded D0/D1 matching-mechanism preflight is recorded separately in [`../c2_mechanism_preflight_v1/`](../c2_mechanism_preflight_v1/README.md). It checks candidate generation and one-to-one assignment on the first simultaneous arrival round; it is not a performance result. The preflight and current C3 cost-map gap are documented in [`scenario design and results`](../../../../docs/dispatch-layer-nyc-tlc-multivehicle-scenarios-2026-10-19.md).
