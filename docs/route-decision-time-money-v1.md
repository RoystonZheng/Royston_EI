# Route time and money decision model v2

## Decision rule

For each task, first remove routes that fail reachability, endpoint, legality, or route validation. For each remaining candidate, retain an itemized ledger for its journey segment(s): vehicle-to-pickup deadhead, passenger service, or any separately modeled leg. Each journey segment carries its ordered road-Link energy breakdown. Energy/electricity cost is charged once per Link; fare is credited once at the journey-segment level when observed, or per Link only when an explicit per-km fare schedule is configured. The route-level net cost is the sum of its priced journey segments. Then calculate:

```text
score = w_time * minmax(travel_time_sec)
      + w_money * minmax(net_economic_cost)
```

Both min-max ranges are computed over the same feasible candidates for that task. Lower scores win. If one dimension is constant, that normalized dimension is zero. Exact ties break by raw travel time, raw net cost, then candidate ID. The selector never trades off a hard constraint.

The two profiles are:

| Profile | Time weight | Money weight |
|---|---:|---:|
| `time_priority_0_6_0_4` | 0.6 | 0.4 |
| `money_priority_0_4_0_6` | 0.4 | 0.6 |

## Segment accounting

The money value is the known operating expense minus known fare revenue. Track it by journey segment:

- `deadhead` / vehicle-to-pickup: fare revenue is known zero; energy cost still applies.
- `passenger` / pickup-to-dropoff: use an observed fare amount or explicit fare schedule. If neither is available, fare revenue stays null.
- Energy expense is EV v2 kWh multiplied by the experiment’s explicit electricity price.
- Preserve each road Link’s energy kWh, propulsion draw, regeneration, auxiliary energy, time, and electricity cost. Observed whole-trip fare stays on the trip segment and is not duplicated over Links.
- Add API/token and compute expense only when both usage and a matching rate are known.
- Unknown cost and revenue values stay null. A passenger candidate with unpriced fare revenue or unpriced electricity is excluded from net-economic-cost scoring; its known operating-cost subtotal is reported separately. It cannot be treated as if fare revenue were zero.
- The 3–4 CNY/km example in the transcript is a sensitivity scenario only, not a default or frozen fare. Do not copy it into Porto/NYC runs without an explicit local-currency fare basis.

The meeting transcript around 50–55 minutes discusses comparing distance-based fare revenue with electricity and token/API expense. The transcript gives roughly 3–4 CNY/km as an example and talks about weight pairs for electricity versus token use. Its wording does not unambiguously set 0.6/0.4 and 0.4/0.6 as time-versus-money weights. The current user instruction approved using those pairs for time versus money, so this implementation records that as the adopted design choice. The fare examples remain illustrative, are not defaults, and must not be copied into Porto/NYC experiments without a local currency-specific fare basis.

## Integration and evidence boundaries

`route_experiment/decision.py` contains the reusable deterministic selector and segment-ledger aggregator (`route_time_money_segment_v2`). The shared route evaluator now exposes Link-level energy detail, and `scripts/calculate_route_costs.py --enable-route-decisions` writes it with journey-segment accounting and decisions into a versioned sidecar without changing source route files. It requires an explicit priced electricity profile and known passenger fare revenue before scoring net economics. Candidate pools with one route are recorded as single-candidate decisions and do not prove that the weighting changed route choice.

The current route experiment output format may lack alternative routes, fare schedules, or per-candidate API costs. Such missing inputs are reported as limitations. Existing outputs should be recalculated into sidecars, preserving their source files and input hashes. Where a candidate is a single route leg, the sidecar records that leg; assignment-level comparisons must aggregate all known empty and passenger legs for the same decision before route ranking.

## Meeting link

[Tencent Meeting transcript](https://meeting.tencent.com/cw/2q8pQndO0b)
