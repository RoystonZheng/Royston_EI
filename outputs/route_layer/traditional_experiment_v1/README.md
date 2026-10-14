# Traditional route experiment artifacts

The full first-round run remains in the local workspace at `outputs/route_layer/traditional_experiment_v1/` because it contains about 3 GB of per-task JSON/HTML replay files.

This directory keeps the compact `manifest.json` for provenance. Recreate the complete trace set with:

```bash
caffeinate -dimsu python3 scripts/run_traditional_experiment.py \
  --seed-count 10 --seed-start 0 --size 100 \
  --output-dir outputs/route_layer/traditional_experiment_v1 \
  --price-config-json configs/route_layer/price_smoke_v1.json
```

The local run produced 450 trace records across 10 seeds; see `docs/route-layer-run-2026-10-14.md` for the verified smoke and formal-run report.
