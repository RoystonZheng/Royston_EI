# NYC TLC 2024 High Volume FHV Trip Data

## Acquisition

- Coverage: January–December 2024, published as 12 monthly Parquet files.
- Retrieved: 2026-10-19 from the official NYC TLC monthly data distribution.
- Supporting files: TLC taxi-zone lookup CSV and zone-boundary ZIP, stored in `raw/`.
- Source dataset: [NYC Open Data, 2024 High-Volume FHV Trip Data](https://data.cityofnewyork.us/Transportation/2024-High-Volume-FHV-Trip-Data/bqxa-629f).
- Distribution and supporting files: [NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page).
- Monthly files, byte counts, SHA-256 digests, and source URLs: [`manifest.json`](manifest.json).

## Intended use in the dispatch experiment

Use `request_datetime` with `PULocationID` and `DOLocationID` to build a historical zone-level request-arrival trace. The trip record also includes actual pickup/dropoff times, trip duration/distance, and shared-ride flags. Exclude records marked as shared matches when constructing the one-vehicle/one-order subset, then audit missing timestamps, invalid intervals, duplicate records, and zone IDs before generating replay input.

This is a request-trace input, not a complete dispatch log. The official dictionary describes dispatched trip records; it does not provide the universe of unserved/cancelled requests or a complete time-varying fleet state. Vehicle availability and initial positions therefore need explicit assumptions or a separate source. `pickup_datetime - request_datetime` is a historical request-to-pickup interval, not a general driver-arrival timestamp; `on_scene_datetime` is only reported for accessible vehicles.

## Route-layer boundary

The trip endpoints are taxi-zone IDs and the accompanying geometry is zone polygons. These files contain no road graph, edge travel times, or GPS route trajectories, so they do not support a controlled rerun of the current route-layer experiment. The existing route-layer results remain unchanged.

## Derived multi-vehicle scenarios

Four separate request-slice scenarios were built from the audited January candidate trace for controlled one-to-one dispatch experiments. The package is at [`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/multi_vehicle_competition_scenarios_v1/`](../../../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/multi_vehicle_competition_scenarios_v1/); its manifest records source-trace and per-file SHA-256 hashes. It contains a one-order/three-vehicle mechanism case, four same-second orders competing for three vehicles, a 44-order ten-second burst with 18 vehicles, and a 790-order five-minute load sweep with 12/24/48 vehicles.

Vehicle initial positions are synthetic and deliberately colocated with request pickup zones. This guarantees known same-zone candidate overlap at zone resolution, but assumes zero zone-level pickup time and omits within-zone movement; it does not represent observed NYC fleet state or validated road ETA. The request trace itself contains already-dispatched/completed trips, so these inputs are scenario fixtures rather than unbiased demand. Bounded C2, C3, and C4 D0/D1 mechanism preflights are archived at [`c2_mechanism_preflight_v1/`](../../../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/c2_mechanism_preflight_v1/), [`c3_mechanism_preflight_v1/`](../../../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/c3_mechanism_preflight_v1/), and [`c4_load_sweep_mechanism_v1/`](../../../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/c4_load_sweep_mechanism_v1/); none is a performance result. See the [scenario design and audit](../../../docs/dispatch-layer-nyc-tlc-multivehicle-scenarios-2026-10-19.md). The scenario builder refuses to overwrite an existing package:

```bash
python3 scripts/build_nyc_tlc_multi_vehicle_scenarios.py
```

## Use and attribution

The NYC Open Data page reports the dataset license as unspecified. Keep the official source attribution and review the portal's [terms of use](https://data.cityofnewyork.us/stories/s/Terms-of-Use/k9k7-3cje) before redistribution. The local copy is for this project's research workflow.

## Reproduce the January audit and pilot

Run from the project root with DuckDB 1.5.6 installed:

```bash
python3 -m pip install duckdb==1.5.6
python3 scripts/audit_nyc_tlc_hvfhv_month.py --year 2024 --month 1
python3 scripts/run_nyc_tlc_zone_proxy_dispatch_pilot.py
python3 scripts/run_nyc_tlc_c3_mechanism_preflight.py
python3 scripts/run_nyc_tlc_c4_load_sweep.py
```

Each pilot/preflight script refuses to overwrite its output directory; use a clean workspace copy or pass a new `--output-dir` to preserve earlier artifacts.

## Evidence status

Downloaded files are recorded with byte counts and SHA-256 digests in `manifest.json`. A January 2024 request-time audit and zone-level candidate trace have been created; see the [audit report](../../../docs/dispatch-layer-nyc-tlc-2024-01-audit.md), [machine-readable audit](../../../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/audit_2024-01/audit_2024-01.json), and [candidate trace](../../../outputs/dispatch_layer/nyc_tlc_hvfhv_2024/audit_2024-01/request_trace_2024-01.parquet). An early D0/D1 zone-cost pilot exposed sparse candidate coverage under its initial fleet assumptions; separate C2/C3/C4 preflights then checked matching, multi-round competition, and fleet-load sensitivity with explicit same-zone-only empty pickup. These are controlled mechanism results, not NYC performance estimates. Road travel costs, cross-zone pickup ETAs, and fleet-state assumptions still need a defensible model before a formal TLC-based performance comparison. The route-layer results remain unchanged.
