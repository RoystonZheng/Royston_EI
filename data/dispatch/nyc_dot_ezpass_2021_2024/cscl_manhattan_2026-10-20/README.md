# NYC CSCL Manhattan snapshot

This folder stores a read-only Socrata API slice of the official NYC Street Centerline (CSCL), filtered to borough code `1` (Manhattan). The download has 14,111 rows and was retrieved on 2026-10-20; `manifest.json` records the selected fields, request URL, byte count, and SHA-256.

`TRAFDIR` is retained with each row. The official Centerline field dictionary defines `FT` as with the segment address range, `TF` as against it, `TW` as two-way, and `NV` as non-vehicular. The accompanying City of New York metadata states that address ranges are relative to the line's digitized direction.

The matching preflight indexes only `FT`, `TF`, and `TW` features. The snapshot is current 2026 street geometry, while the matched DOT travel-time observations are from January 2024; it is not a historical 2024 road network. The API fields `avgtravtime` and `posted_speed` are not used as measured historical travel costs in this preflight.

The raw snapshot supports the reproducible analysis in `scripts/audit_ezpass_cscl_match.py`. A name-and-proximity match does not mean DOT travel time has already been transferred to the complete CSCL feature or its subsegments.
