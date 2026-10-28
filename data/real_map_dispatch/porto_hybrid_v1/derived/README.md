# Derived data status

This package has progressed beyond the initial candidate-only stage. The fixed-fleet branch now has frozen v3.3 orders, shared endpoint-to-Link bindings, a frozen SUMO 1.28.0 network and traffic inputs, and a completed R1 dynamic simulation matrix. The ten draft order files below are retained as historical drafts; formal runs use the separate frozen v3.3 inputs.

Generated map and scenario artifacts:

- `heat/porto_hotspot_cells_200m.csv`: 200 m taxi-trip endpoint heat counts by pickup/dropoff proxy.
- `heat/porto_od_grid_counts_200m.csv`: aggregated origin/destination cell counts.
- `heat/aggregation_manifest.json`: row counts, grid transform, time semantics, and output list.
- `region/candidate_fence.geojson`: heat-informed provisional rectangle; not a frozen experiment boundary.
- `region/selection_note.md`: candidate-area counts, rationale, and remaining freeze gates.
- `region/osm_preflight.json`: OSM snapshot hash, basic directed graph counts, signal/restriction counts, and nearest-node/reachability preflight.
- `scenarios/` contains the fixed-fleet route-execution configuration, event/route schema, generator, ten historical draft seeds, and the formal v3.3 frozen orders and endpoint bindings. Formal route legs passed the static traditional reachability preflight.
- `sumo/` contains the frozen SUMO network, signal programs, EV proxy settings, and background demand inputs. R1 results are under `experiments/r1_dynamic_sumo_v1/formal_v3_fixed_delay/`; this is a simulated benchmark with synthetic traffic and uncalibrated EV parameters, not observed Porto operations.
- R2 has three one-call Codex CLI diagnostics: full-graph, endpoint-cropped, and randomized-alias endpoint-cropped. All proposed route sets failed hard route validation and were not replayed in SUMO. They are diagnostic failures, not formal success-rate or performance results.
- `experiments/r2_context_profile_v1/` is retained for history but its serialized graph-size estimate omitted unsignalized legal turns. Use `experiments/r2_context_profile_v2/`, which serializes all induced legal turns, for the corrected zero-call static reachability/input-size profile.

Every later artifact must include source, schema/version, seed when applicable, and SHA-256. Keep historical taxi proxies, OSM map facts, SUMO-simulated traffic, and route-method results clearly distinguished.
