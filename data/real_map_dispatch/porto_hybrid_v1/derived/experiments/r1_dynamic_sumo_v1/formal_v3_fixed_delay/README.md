# Porto fixed-fleet R1 v3 aggregate outputs

This directory contains aggregate records for the completed 10-seed × 2-objective R1 simulation matrix. All planned seed/objective cells remain in the denominator.

- Terminal cells: 19 completed, 1 failed (seed 4 FASTEST); the failed cell was not retried or replaced.
- The nine paired completed seeds support descriptive paired simulation statistics. The confidence intervals describe these fixed simulated seeds, not real Porto operations.
- FASTEST and LOWEST_ENERGY are route-planning objectives for a fixed fleet and fixed order sequence. This experiment does not perform online dispatch or reassignment.
- SUMO traffic, waiting, distance, and energy are simulated. UCI Porto taxi data supplies historical OD/endpoint proxies; it is not real-time ride-hailing demand or traffic.
- The road network uses an attributed OpenStreetMap snapshot. See the project-level LICENSES.md and [OpenStreetMap copyright and license](https://www.openstreetmap.org/copyright). This directory contains aggregate result files, not a copy of the raw network.
- Protocol SHA-256: 4c00711cc7fd2d29aa4c259dcb683167606646f8293c8f2e93a99e6b6df19cd1
- Runner SHA-256: 45e49f6e3cab5f8a14a5469d0b6f4228f717751a21beafff5a7effcfa6f8c7e6
- Confidence interval inputs and results are in ../analysis_ci_v1/. Full design and interpretation limits are in ../../../../fixed_fleet_route_experiment_design_v11_20261024.md.
