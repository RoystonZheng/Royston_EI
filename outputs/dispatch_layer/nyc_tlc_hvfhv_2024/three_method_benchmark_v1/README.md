# NYC TLC shared-input dispatch benchmark v1

This package adapts NYC TLC 2024 High Volume FHV trip records for controlled dispatch-method comparisons. It preserves the source-derived order cases and creates an explicit, deterministic zone-level pickup cost proxy so methods can evaluate cross-zone vehicles. It is a simulation input package, not a record of actual vehicle availability or measured NYC service performance.

## Method groups

- Traditional: `D0_greedy`, `D1_optimizer`
- Pure LLM direct assignment: `D5_pure_llm_direct`
- Research Agent + optimizer: `D2_agent_optimizer`; `D3` and `D4` are offline ablations by default. `D3` uses an offline context-function surrogate. `D4` uses offline feedback by default; selecting DeepSeek makes live API calls.
- Hybrid Codex: `D6_agent_codex_context_optimizer` uses per-vehicle Agent candidate filtering, one Codex-generated bounded context function per simulation, then the central optimizer. The model never selects assignments or changes hard constraints. It has a zero-retry policy and must receive `--allow-live-api` for a live run; `--codex-policy-replay` reuses an archived response without a new model request.
- Neural candidate ranking: `D7` is a planned second-stage ablation, not implemented in this package. Current TLC data has no counterfactual unserved orders or true vehicle-state histories; an optimizer imitation label must not be described as real demand or operations supervision.

All methods use the same scenario, fleet variant, route-cost inputs, hard rules, `DispatchInput` contract and output validator. A common candidate enumerator calculates route reachability and costs; D5 makes the final matching itself and does not invoke the Agent context filter or mathematical optimizer.

## Cases

| Case | Orders | Fleet | Use |
|---|---:|---:|---|
| C2 | 4 | 3 | Small simultaneous-arrival interface smoke; low evidence value for performance |
| C3 | 44 | 18 | Multizone burst and candidate competition; recommended first mechanism comparison |
| C4 | 790 | 12 / 24 / 48 | Load sensitivity; select `C4-F1`, `C4-F2`, or `C4-F4` using `--fleet-id` |

The request trace is from January 17 (C3) and January 20 (C2/C4) 2024. The direct zone OD ride medians use January 1–15, 2024 training rows. The training and evaluation windows do not overlap. Every scenario JSON, order JSON and cost matrix is hashed in `benchmark_manifest.json`.

## Cost and state interpretation

- TLC records are historical dispatched/completed trips. They omit the counterfactual rejected/unserved requests and fleet state history. Order rows are replay requests, and vehicle positions are synthetic.
- Loaded passenger legs use a direct observed training-period taxi-zone OD median for time and distance. No OD medians are composed into a street route.
- Empty pickup legs use `zone_centroids.json`: Haversine distance between official taxi-zone area centroids, multiplied by a fixed 1.3 detour factor, divided by a fixed 5 m/s speed. Same-zone pickup uses a fixed 300 m distance. This creates differentiated and reproducible candidate costs, but it is an explicit proxy, not observed empty-trip ETA or a road network.
- Taxi-zone polygon source and its hash are recorded in the manifest. Zone 265 is absent from the available polygon slice, but it is used here only as a ride destination in the included cases; no pickup or synthetic vehicle position requires its centroid.
- The outputs are bounded mechanism experiments. Do not describe their service rate, waiting time, or cost as NYC-wide performance.

## Run commands

Offline shared-input comparison on C3:

```bash
python3 scripts/run_nyc_tlc_dispatch_benchmark.py \
  --case C3_manhattan_multizone_44_order_burst \
  --methods D0_greedy D1_optimizer D2_agent_optimizer \
  --output-dir outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results
```

D5 is a live Codex CLI method, has zero retries, and records token usage from the CLI JSONL. The CLI does not expose a verifiable USD bill, so cost remains unknown (`null`); do not infer dollars from tokens. Use a separate output directory for each run:

The current D5 input protocol sends only hard-constraint-feasible candidate edges while retaining the current vehicle and arrived-order state. Results using the earlier full candidate table are labeled protocol v1 and should not be pooled with `hard_feasible_candidate_edges_v2`.

```bash
python3 scripts/run_nyc_tlc_dispatch_benchmark.py \
  --case C2_four_same_second_orders_three_vehicles \
  --methods D5_pure_llm_direct \
  --single-round-smoke \
  --allow-live-api \
  --codex-model gpt-5.6-luna \
  --output-dir outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_codex_cli_vN
```

The script refuses a live D5 or D6 run unless `--allow-live-api` is supplied. Add `--single-round-smoke` to stop after one dispatch decision; on C2 this makes at most one D5 Codex CLI invocation. D6 makes at most one strategy call per simulation. For C4 D5 runs, use `--compact-records` to retain per-round call audits and the first candidate table without serializing every full state snapshot. A full D5 simulation may make additional calls when later dispatch decisions have feasible candidates. No model call is part of building or checking this package.

The first D6 pilot is documented in [the hybrid Agent/Codex/optimizer design and results](../../../../docs/dispatch-layer-hybrid-agent-codex-nn-design-2026-10-22.md) and its [C3 seed-0 report](results/hybrid_agent_codex_nn_v1/C3_manhattan_multizone_44_order_burst/single_fleet/pilot_report.md). D6 used one original Codex request; the completed simulation replayed the stored response after a local interruption, so replay added no token usage. This is a single-seed integration pilot, not a formal performance comparison.

The first offline C3 seed has been run for D0/D1/D2. The first `DispatchInput` is identical across those methods, all 42 rounds pass the validator, and the result summary is in [the shared-input run note](../../../../docs/dispatch-layer-three-method-shared-dataset-2026-10-20.md). This one-seed proxy run is a mechanism check only.

## Build and input check

`scripts/build_nyc_tlc_three_method_benchmark.py` copies immutable scenario, order and cost inputs, computes the taxi-zone area centroids, checks that every pickup and synthetic initial vehicle zone has a centroid, and writes SHA-256 hashes. Rebuilding requires `--overwrite`; the script refuses to rebuild while result files exist.
