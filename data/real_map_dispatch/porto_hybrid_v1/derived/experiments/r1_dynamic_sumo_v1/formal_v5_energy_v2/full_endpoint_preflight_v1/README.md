# Full endpoint preflight v1 — 2026-10-27

Status: `passed`. This offline route-algorithm gate covers 2,000 frozen OD legs and 4,000 target-route queries (including the earlier sample). The remaining population adds 1,900 OD legs and 3,800 queries. All 1,900 FASTEST and 1,900 remaining-population queries passed; errors: 0. No model calls were made.

This checks route reachability against the frozen synthetic traffic/signal snapshot. It is not a SUMO outcome or real-vehicle performance result. `remaining_route_checks.jsonl` contains the per-query records.
