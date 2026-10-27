# Porto R1 v3 uncertainty analysis

This additive analysis reads the completed v3 aggregate and paired seed table. It does not alter formal runs or replace missing runs.

Completion-rate intervals use Wilson 95% intervals over 10 pre-registered seeds. Paired metric intervals use two-sided Student-t 95% intervals over the 9 seeds where both methods completed (df=8). Delta is LOWEST_ENERGY minus FASTEST.

These intervals summarize variation across this frozen set of simulated seeds. They do not establish a real-world effect, a calibrated operational distribution, or an AI map-readability effect.

## Paired metric differences

| Metric | n | Mean paired difference | 95% CI |
|---|---:|---:|---:|
| Fleet makespan (s) | 9 | 1100.131 | [882.884, 1317.378] |
| Net simulated energy (kWh) | 9 | -14.350 | [-15.187, -13.512] |
| Actual waiting time sum (s) | 9 | 1272.456 | [920.288, 1624.623] |
| Actual distance (km) | 9 | -15.203 | [-16.978, -13.427] |

## Completion rate Wilson intervals

| Method | Completed | Estimate | 95% Wilson CI |
|---|---:|---:|---:|
| FASTEST | 9/10 | 90% | [59.6%, 98.2%] |
| LOWEST_ENERGY | 10/10 | 100% | [72.2%, 100.0%] |
