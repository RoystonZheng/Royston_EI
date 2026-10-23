# Derived data status

Generated candidate artifacts:

- `heat/porto_hotspot_cells_200m.csv`: 200 m taxi-trip endpoint heat counts by pickup/dropoff proxy.
- `heat/porto_od_grid_counts_200m.csv`: aggregated origin/destination cell counts.
- `heat/aggregation_manifest.json`: row counts, grid transform, time semantics, and output list.
- `region/candidate_fence.geojson`: heat-informed provisional rectangle; not a frozen experiment boundary.
- `region/selection_note.md`: candidate-area counts, rationale, and remaining freeze gates.
- `region/osm_preflight.json`: OSM snapshot hash, basic directed graph counts, signal/restriction counts, and nearest-node/reachability preflight.

The heat aggregation omits trip and taxi IDs. The OSM preflight uses nearest road nodes only, counts but does not apply turn restrictions, and does not create final station-to-directed-Link bindings. `scenarios/` now contains a fixed-fleet route-execution configuration, event/route schema, generator, and ten draft 100-order seeds. These drafts do not have final point-to-directed-Link bindings and their 200 route legs per seed have not passed the traditional reachability validator. No SUMO network, final signal program, background-flow configuration, or simulation results exist yet. Every later artifact must include source, schema/version, seed when applicable, and SHA-256.
