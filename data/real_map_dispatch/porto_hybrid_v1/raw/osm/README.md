# OSM candidate snapshot

The candidate-area snapshot was downloaded from the FOSSGIS-operated public Overpass endpoint on 2026-10-22. The exact query is preserved in `porto_candidate.overpass.ql`; the OSM source timestamp is `2026-10-22T04:01:05Z`, and the WGS84 bounding box is west/south/east/north `[-8.635, 41.138, -8.595, 41.162]`.

The extract contains 31,460 nodes, 7,278 ways (all 7,278 have a `highway=*` tag), and 121 turn-restriction relations. It is a **candidate-area snapshot**, not a frozen final fence. The visualization displays all 7,278 original highway ways; road-access colors are annotations only. A structural preflight found the top 10 pickup heat cells' nearest routable nodes mutually reachable in the directed graph before applying turn restrictions. Final candidate-point-to-road matching, access-connection construction, complete restriction semantics, and map-boundary route coverage are still pending.

Attribution: **© OpenStreetMap contributors**. OpenStreetMap data is available under the Open Database License (ODbL); see <https://www.openstreetmap.org/copyright>. Keep OSM files, attribution, and derived database outputs identifiable and review ODbL obligations before redistribution.

The OSM snapshot contains road geometry, direction/access tags, 418 mapped `highway=traffic_signals` nodes, and restriction relations. It does **not** provide signal phase timing or measured dynamic congestion. Those traffic fields must come from separately labeled SUMO simulation outputs.
