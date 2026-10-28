# Meeting evidence supplement: electricity and token decision objective v1

**Status:** requirements draft; not a frozen implementation contract. The trigger for choosing a weight profile is unresolved.

## Evidence

Source: [Tencent Meeting transcript](https://meeting.tencent.com/cw/2q8pQndO0b), read from the user-provided public share page on 2026-10-27. The public minutes detail response returned code 0 and 241 paragraphs. The normalized 48:33–55:11 excerpt contains 13 paragraphs; SHA-256 `23817a43e2930ca10327d66cb3cfd55345bed7620e372cf2e54ebf3ca9240ed4`. Speaker labels are pseudonyms: S1 is inferred to be the teacher because S1 addresses “郑同学”; S2 is inferred to be the student because S2 replies “老师”. This role mapping comes from the transcript text, not voice identification.

- **48:33–49:21:** the teacher proposes expressing token/delay and energy consumption as comparable loss/cost quantities. The student notes token pricing exists but order income is not yet reliably quantified.
- **49:21–52:06:** the teacher describes the EV energy cost for each route segment or distance interval, then comparing the route's physical energy cost with token resource cost. A minimum-cost reference should first be measured in a simple or grid-map experiment.
- **53:00–54:00:** the teacher discusses order income/profit and a reward target, giving 5% or 15% as examples. The per-kilometre 3–4 CNY example is illustrative, not a frozen tariff or fare.
- **54:00–55:11:** the two stated weight pairs are electricity/token = 0.6/0.4 and 0.4/0.6. The wording about which pair applies above/below the profit threshold is ambiguous; the transcript does not specify the threshold definition, comparison direction, or how to resolve the clauses.

This is distinct from [`route-time-money-segment-v5.md`](route-time-money-segment-v5.md), whose time/money weight pairs were approved by the user. The teacher's 0.6/0.4 and 0.4/0.6 pairs must be labeled electricity/token, not time/money.

## Candidate accounting contract

These rules are a proposed way to operationalize the discussion; they are not all stated verbatim by the teacher.

1. **Electricity per journey segment:** retain EV v2 signed net kWh. Sum road-Link energy within a journey segment, then price `max(segment_net_kWh, 0)` using a frozen, explicit electricity tariff. Do not clip regeneration separately on every Link.
2. **Token cost per method request/order:** price recorded input/output token usage using the applicable model/API rate card. Keep it at the method or decision-request level; do not allocate token charges to physical road Links. Count once for the selected method/order, not once per alternative candidate.
3. **Order revenue:** where a reliable fare/revenue observation exists, record it once per passenger order. Empty pickup legs have no passenger revenue. If fare is unknown, net profit remains null/blocked; do not infer a Link fare or fill with zero.
4. **Reference baseline:** estimate the minimum-cost reference from a predeclared simple/grid experiment. Preserve the dataset, tariff, token rate card, and denominator. The transcript's 3–4 CNY/km is an example only.
5. **Two reportable profiles:** electricity/token weights `(0.6, 0.4)` and `(0.4, 0.6)`. Until the threshold policy is clarified, report both profiles as separate sensitivity results; do not automatically switch profiles or call either one the final adaptive policy.
6. **Shared comparison set:** score only a complete, predeclared pool of hard-feasible alternatives from the same task/scenario. Missing route energy, token usage/rate, baseline, or required revenue blocks the affected economic comparison instead of silently ranking a partial pool.

One candidate mathematical form for discussion is a weighted score over consistently normalized energy cost and token cost. The transcript does not choose the normalization method or define the profit denominator, so neither is frozen here. Current frozen experiment batches remain unchanged; a future runtime selector or result claim requires a versioned contract and auditable inputs.

## Existing pure-LLM grid input inventory

The offline alignment sidecar [`manifest.json`](../outputs/route_layer/pure_llm_energy_token_input_alignment_v1_20261028/manifest.json) binds 596 EV v2 rows from saved pure-LLM grid versions v1/v4/v5/v6/v7/v8 to their original per-task token usage records. The source manifest token totals matched the task-row totals; all 596 recorded API-cost fields equal the saved reference rate calculation. Sidecar SHA-256: manifest `b09b608ecfffdf396e0e14b8789f8f83cd3abff463fdfc5770b620c5740ef47b`, summary `205158a27a72af18e84b642a5e0f4bf1eb8fd96a1104b04c17c0ddcf317de396`, JSONL `60ff9ea805456e084fb9793a70fa070adc5fd46bb0fbde5dc5ed3b8198fa8c9e`.

The inventory has 55,865,014 input and 991,262 output tokens. 387 rows have non-zero usage priced at the saved reference rate; their combined estimate is CNY 170.568828, not an account invoice. 209 rows record zero tokens alongside request errors, so their billing remains unverified. EV v2 status counts are 153 estimates, 2 partial estimates, 1 not estimable, and 440 with no proposed route. Those estimates include invalid or hypothetical proposals; they are not observed vehicle energy.

No decision score was generated: the files contain 596 single-method task records and only 31 route candidates in total, with no complete shared candidate pool, no frozen generic-grid electricity tariff, no order revenue, and no confirmed profit/normalization rule. The old Tesla station electricity-price reference is preserved in source metadata but was not used to bill the generic EV v2 rows. No source file was modified, and this inventory made zero model calls or route reruns.

## Open decisions

- What exactly is the profit/benefit threshold, and what are its comparison direction and denominator?
- Should the two electricity/token profiles both be reported, or should one be selected automatically once the threshold rule is specified?
- What reliable order-level revenue source and common currency apply to each experiment?
- How should energy/token costs be normalized if their raw monetary values are already available in a common currency?

## 2026-10-28 implementation decision

The segment-accounting question should be included in the experiments because segment energy cost and passenger revenue are inputs to the money side of route comparison. Keep this bounded to the evidence: record EV v2 energy/electricity cost by journey segment, attach an observed fare once to its passenger order/segment, and keep API/token and compute expense once at method level. The transcript does not support charging a passenger fare to each road Link.

The approved time-versus-money profiles remain a separate experiment (`time/money = 0.6/0.4` and `0.4/0.6`). The teacher's transcript instead states `electricity/token = 0.6/0.4` and `0.4/0.6`; its profit threshold and automatic profile-switch direction are still ambiguous. Both electricity/token profiles can be reported as a separate sensitivity analysis once their baseline, tariff, and token rate are frozen. Do not implement automatic switching from the transcript wording alone.

On 2026-10-28 the R4, NYC dispatch, and Porto experiment conversations were sent this distinction and asked to report their actual selector integration points. Delivery of the messages is confirmed; their acknowledgment and code integration remain pending.

## EV v2 model and traffic-input fidelity

The current EV model is `ev_longitudinal_v2`, profile `generic_reference_ev_v1` (profile SHA-256 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`). It is a brand-independent reference profile, not a Tesla curve. The force model is

```text
F = (curb_mass + payload + rotating_mass) * acceleration
  + 0.5 * air_density * drag_coefficient * frontal_area * speed^2
  + rolling_resistance_coefficient * mass * gravity * cos(grade)
  + mass * gravity * sin(grade)

net_kWh = propulsion_draw_kWh - regenerated_kWh + auxiliary_kWh
```

Distances are metres, speed m/s, acceleration m/s², mass kg, grade radians, force newtons, power watts, time seconds, and energy kWh. The reference profile has 1,900 kg curb mass, 75 kg passenger payload, 50 kg rotating mass, 0.9 propulsion efficiency, 0.65 regenerative efficiency with a 50 kW cap, 800 W auxiliary load, max acceleration 1.2 m/s², and max deceleration 2.0 m/s².

Congestion level changes the speed cap through the frozen experiment speed multiplier; signal delay inserts stop, idle, and restart events. The model does not infer queue lengths or real stop-and-go traffic. If those observations are absent, report energy as route-condition estimates or partial estimates using `trace_fidelity` and the profile hash; do not describe it as measured congestion energy or vehicle telemetry. Missing grade is treated flat by the current adapter and is counted as a missing-grade step.


## 2026-10-28 direct transcript verification

The user-provided Tencent share link was re-read through its read-only minutes detail endpoint on 2026-10-28. The endpoint returned code 0 and 241 transcript paragraphs. A raw ASR excerpt for 49:08–55:10 is preserved at `docs/meeting_transcript_excerpt_20261028_49-55_raw.md` (SHA-256 `d4e11ecfb4d0a34c5aba075be47639ba86315392d59b94da02b66d9c398a4fc8`). Speaker labels are preserved as returned; identity is not verified by voiceprint.

The excerpt confirms the teacher's sequence: model physical EV cost in each route interval, sum the intervals for a candidate path, measure a minimum-cost reference on a simple or grid map, then compare the path cost and expected gain against that reference. The 5%/15% profit targets and 3–4 CNY/km examples are illustrative. At 54:00 the two discrete weights are expressly electricity/token `(0.6, 0.4)` and `(0.4, 0.6)`. The teacher mentions switching around a profit threshold, but the threshold, denominator, and branch wording do not form a fully executable rule; at 54:50 the teacher also says continuous per-node weights may add computation time and recommends two combinations. Thus the profiles are suitable for separate sensitivity reporting; an automatic threshold switch remains blocked pending a defined rule.

This E/token objective is distinct from the already approved time/money scorer. Existing `route_experiment/decision.py` has time/money profiles and segment accounting helpers; the E/token profiles have not been ranked in a new shared experiment sidecar as of this note. Revenue should be recorded once per passenger order/segment boundary when observed, while electricity is priced after summing signed-net Link energy within the journey segment. Token/model-call cost belongs to the route-generation request/method ledger, not each road Link.
