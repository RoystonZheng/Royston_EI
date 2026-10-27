> Historical specification: replaced by [route-time-money-segment-v4.md](route-time-money-segment-v4.md). Existing v3 sidecars remain preserved.

# Route time and money decision model v3

This is the current specification for `route_experiment/decision.py` version `route_time_money_segment_v3_billable_net_energy`. It defines a route decision rule and an auditable segment ledger. It does not establish full fleet profitability.

## Route decision

First exclude candidates that fail reachability, endpoint, legality, or route validation. For each remaining candidate, use the same route-time definition, money-cost scope, currency, and candidate set. Lower scores are preferred:

```text
score = time_weight * minmax(travel_time_sec)
      + money_weight * minmax(net_economic_cost)
```

Both min-max normalizations use the same feasible candidates for one decision scenario. A constant dimension contributes zero. Ties break by raw travel time, raw net cost, then candidate ID. Weighted scores never compensate for a hard-constraint failure.

| Profile | Time weight | Money weight |
|---|---:|---:|
| `time_priority_0_6_0_4` | 0.6 | 0.4 |
| `money_priority_0_4_0_6` | 0.4 | 0.6 |

These time-versus-money weights are the user-approved experiment mapping. They must not be attributed to the meeting transcript as its explicit time/money weights.

## Journey-segment ledger

Keep an itemized row for each journey segment, such as vehicle-to-pickup deadhead and passenger service. Keep ordered road-Link energy details nested under the segment so its inputs remain auditable.

- **Energy:** retain signed EV v2 net kWh for every Link and segment, along with propulsion draw, regeneration, auxiliary use, distance, and time where available.
- **Electricity billing:** sum signed Link energy within the journey segment first. Then set `billable_energy_kwh = max(segment_net_energy_kwh, 0)` and calculate `electricity_cost = billable_energy_kwh * explicit_electricity_price`. Do not clip regenerative energy Link by Link; that changes the billed amount.
- **Fare revenue:** deadhead revenue is known zero. Record an observed passenger fare once on the passenger journey segment. Allocate revenue to Links only when an explicit per-kilometre fare schedule is part of the frozen scenario.
- **Net cost:** for a declared cost scope, compute priced operating expense minus known fare revenue. Preserve known subtotals and leave unknown components null.
- **API/token and compute expense:** record measured usage and matching rate separately from road-Link and journey-segment energy. Attribute this expense to the relevant decision or method; do not duplicate a method-level charge across every route candidate or Link. If the comparison claims full experiment economics, all in-scope API/compute and vehicle-cost inputs need a common currency and pricing basis. Otherwise report a clearly scoped vehicle operating cost and keep API/compute expense separate.
- **Unknowns:** missing fare, tariff, usage, or rate remains null. Never convert an unknown to zero. A net-cost score is blocked when required inputs for the declared scope are missing or incomparable. An operating-cost-only result must not be labeled full economic cost or profit.

The 3–4 CNY/km figures recorded in the existing transcript summary are illustrative sensitivity values, not a frozen fare. They must not be applied to Porto, NYC, or another scenario without an explicit local-currency fare basis.

## Transcript evidence boundary

The project’s earlier transcript notes describe the discussion around 00:50–00:55 as comparing distance-based fare revenue with electricity and token/API expense, with weight pairs discussed for electricity versus token expense. They do not unambiguously establish the approved time-versus-money weight mapping above. The Tencent transcript page could not be independently opened during the current follow-up, so this specification preserves the prior project interpretation and the user-approved design separately; it does not claim a fresh transcript verification.

## Implementation and reporting

`route_experiment/decision.py` provides the deterministic candidate scorer and the segment economics aggregator. Candidate outputs with one feasible route are recorded as single-candidate decisions and do not show that a weight changed route choice. Recalculation of existing results belongs in a hash-bound versioned sidecar; keep source route and dispatch outputs unchanged.

The net-cost field is a declared, priced scope rather than a full profit model. Vehicle depreciation, maintenance, insurance, taxes, tolls, and any other excluded expense must remain explicit limitations. Energy values are simulated model outputs unless supported by vehicle telemetry.

Meeting link: [Tencent Meeting transcript](https://meeting.tencent.com/cw/2q8pQndO0b)
