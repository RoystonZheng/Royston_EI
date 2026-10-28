# Porto Hybrid Real-Map Experiment Dataset v1

**Status:** the source-data package remains a partial historical proxy, while the fixed-fleet R1 simulation is complete. The 540,159,049-byte Figshare record describing full-year Porto taxi trajectories passed its MD5 and ZIP CRC checks; its exact content equivalence to the current UCI download remains unconfirmed, with a one-row metadata/parsed-count discrepancy recorded. The OSM/SUMO network and v3.3 frozen orders are now in place. R1 completed the 20-cell dynamic SUMO matrix under frozen planning-delay replay; R2 has one failed, non-retried Codex route diagnostic and no formal LLM matrix yet.

The fixed-order generation and execution module design was added on 2026-10-23 ([module specification](../../../output/paper/固定订单序列生成与执行模块设计_20261023.md)). The old `orders_seed_0_draft.json` through `orders_seed_9_draft.json` remain audit drafts. Formal R1 inputs are the separate v3.3 frozen orders and shared endpoint bindings; their static directed reachability passed before the R1 SUMO matrix.

This package contains two independent research designs on a real geographic road network: the earlier dispatch-layer experiment and a new fixed-fleet route-execution experiment. The route-execution branch fixes each vehicle's order sequence and does not run an assignment or dispatch layer. The package combines:

1. Porto Taxi Service Trajectory from UCI for historical taxi OD/endpoint heat proxies;
2. a frozen, attributed OpenStreetMap road-network snapshot for directed vehicle routing;
3. SUMO-generated signal phases, background demand, and congestion states, all labeled as simulated;
4. generated task scenarios, kept distinct from historical taxi records and SUMO traffic outputs.

This package does **not** use the user-supplied `fence10089` dataset. The UCI trajectories are completed taxi trips, not online ride-hailing requests, idle-vehicle records, or platform dispatch logs. No real-platform dispatch claim should be made from this data.

## Contents

```text
raw/uci/train.csv.zip      # 540,159,049 bytes; mirror MD5 and ZIP CRC verified; tracked with Git LFS
raw/osm/                   # full highway-way OSM candidate extract and query; final fence remains provisional
derived/heat/              # 200 m heat and OD aggregate tables
derived/region/            # provisional fence and OSM structural preflight
derived/scenarios/         # fixed-fleet route schema, old drafts, and frozen v3.3 formal inputs
map_import/                 # importable JSON package/schema and browser-side derivation prototype
derived/sumo/              # frozen SUMO network, traffic inputs, and simulation outputs
manifests/SHA256SUMS.txt   # source and derived-file checksums
source_manifest.json       # source URLs, DOI, licenses, versions, and build status
LICENSES.md                # per-source license and attribution instructions
derive_porto_hotspots.py   # aggregates 200 m endpoint cells and OD counts without trip IDs
summarize_osm_candidate.py # directed graph and top-hotspot node preflight
download_osm_candidate.sh  # fetches the candidate-area OSM extract; refuses overwrite
```

## Rebuild expectations

- Preserve the UCI source archive and its CC BY 4.0 attribution.
- Freeze the OSM query area, data date, source URL, raw checksum, coordinate reference system, and ODbL attribution.
- Keep observed historical taxi values separate from OSM map facts and SUMO-generated traffic values.
- Display every `highway=*` Way in the candidate OSM extract as raw background geometry. Treat road-access categories as annotations only; build the experiment Link graph after candidate points are matched and their road-access connections are created.
- The base map contains the complete OSM road snapshot, fence, access restrictions, one-way attributes, and mapped signal locations. It shows no pickup/dropoff heat points or filtered candidates until a `map_order_demand_package` v1 JSON is imported. `map_import/` includes the schema, a historical-taxi proxy package, and browser-side candidate/Link/demand-load calculations. The sample package is not the formal fixed-order scenario and contains no online ride-hailing orders.
- Candidate selection is Top N by pickup count, unioned with every point meeting the configured threshold. Candidate-to-candidate Dijkstra Link and demand-weighted congestion are generated only after package import. The current map preview uses length-based shortest paths and simulated signal/background traffic; it does not yet resolve turn restrictions or represent live traffic. Formal route and congestion evaluation remains gated on a frozen SUMO network and traffic inputs.
- A later per-run order view will overlay that run’s fixed endpoints; the planned mix is 70% candidate-to-candidate orders and 30% orders with one or two road-feature endpoints, with the split inside that 30% to be frozen before formal runs.
- Record `source_type` and seed on every simulated traffic field.
- Do not commit extracted duplicates, temporary caches, credentials, or undocumented snapshots.
- The raw UCI archive exceeds GitHub's normal 100 MiB file limit and is configured for Git LFS. Confirm the destination repository's LFS storage/bandwidth quota before pushing.

See [the dispatch-layer experiment design](experiment_design.md). The independent fixed-fleet route-execution experiment, R1 confidence intervals, corrected local-crop profile, and R2 diagnostics are documented in [fixed_fleet_route_experiment_design_v11_20261024.md](fixed_fleet_route_experiment_design_v11_20261024.md); v10, v9, v8, v7, v6, and v5 remain preserved. R1 raw results are under `derived/experiments/r1_dynamic_sumo_v1/formal_v3_fixed_delay/`. The full-graph, local-crop, and randomized-alias Codex CLI one-call audits are under `derived/experiments/r2_codex_cli_v1/pilot_seed0_order0001_fastest_v3/`, `derived/experiments/r2_codex_cli_v1/pilot_seed0_order0001_fastest_local_crop_v4/`, and `derived/experiments/r2_codex_cli_v1/pilot_seed0_order0001_fastest_local_crop_alias_shuffle_v3/`; none passed hard validation or entered SUMO. Corrected zero-call endpoint-crop profiling covers all 2,000 frozen route legs under `derived/experiments/r2_context_profile_v2/`; its paired request-size estimate finds 565/1,000 orders above the current 75k-token planning gate. v1 is retained as historical output with an incomplete legal-turn serialization count. Neither branch replaces existing synthetic-map experiments.
