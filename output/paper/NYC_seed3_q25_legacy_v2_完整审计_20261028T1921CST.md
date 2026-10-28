# NYC seed3 q25 legacy v2 完整增量审计

核验时间：2026-10-28T19:21:38+08:00。范围为legacy seed3 replicate_v1，decision contract v2，与NYC EV2 v4 seed0分开。

## 官方paired audit

只读全矩阵审计`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/analysis_legacy_seed0_seed3_20261028T1918CST_postcell/run_audit.json`，SHA `47447da28216029126fda0cea282b1a078e9286d1377a4993ed50eab54ff7230`，时间`2026-10-28T11:20:27+00:00`；两seed结果为152/180有效paired cells，177个method文件缺失或partial，已完成结果文件1083/1260。该审计确认seed3 q25/F1和F2均为完整、同输入、validator通过。
该计数是legacy contract v2 的独立结果池。NYC EV2 v4 seed0仍使用自己的134/630快照；两者不合并。

## seed3 q25结果文件

| fleet | method | rounds | served/orders | wait avg/p95 (s) | EV2 (kWh) | CLI calls | input/cached/output tokens | SHA-256 |
|---|---|---:|---:|---:|---:|---:|---|---|
| F1 | D0_greedy | 485 | 12/471 | 131.332545/297.770442 | 8.889010178 | 0 | — | `def958bb048b892f8c72edfe4c2958b68f93ea44bbfae5a759ed0d152f87fdb5` |
| F1 | D1_optimizer | 485 | 12/471 | 131.332545/297.770442 | 8.889010178 | 0 | — | `b446775a2227e8b6f6274c94de2cf02ab063ca10921dc5110525fbae37553e2b` |
| F1 | D2_agent_optimizer | 485 | 12/471 | 131.332545/297.770442 | 8.889010178 | 0 | — | `c07622175091f302277d3eeb45dab5ef7151c8d4d4805835449c6ff43bdfaf7d` |
| F1 | D5_pure_llm_direct | 485 | 12/471 | 131.415878/297.770442 | 8.889010178 | 7 | 107545/71168/1290 | `42d57a13cabb2a2794eeccf5c90c3cd6f06b2148d5cb594f947f828db711b271` |
| F1 | D6_agent_codex_context_optimizer | 487 | 12/471 | 142.680046/291.356426 | 8.889010178 | 1 | 13451/0/172 | `9e86dcff5d44f361ebf47f879ff494ccf9b7799a91b052b409b6c1193388e7bf` |
| F1 | D7_agent_codex_capacity_shield_optimizer | 487 | 12/471 | 142.680046/291.356426 | 8.889010178 | 1 | 13451/0/172 | `6971ba29964c66b925dee8a3f9e314b8349fe997ec7212f069968ca55db912ed` |
| F1 | D8_agent_codex_nn_capacity_shield_optimizer | 487 | 12/471 | 148.728326/297.770442 | 8.889010178 | 1 | 13451/0/172 | `c57d89fab06dfac53468879ee981367cdb941054ceab70cb86476a38a9a64993` |
| F2 | D0_greedy | 494 | 24/471 | 103.983386/273.503739 | 16.060693857 | 0 | — | `0348d213fd38b3da511767c02ac651d948def68f4b2fd0cc625d8958ef5f4761` |
| F2 | D1_optimizer | 494 | 24/471 | 103.983386/273.503739 | 16.060693857 | 0 | — | `5e2877c63e5aa418f9ef53948f926242c2597fb834f1da2d55ce55d6b9efc309` |
| F2 | D2_agent_optimizer | 494 | 24/471 | 103.983386/273.503739 | 16.060693857 | 0 | — | `1070a95410e8b7c41d4466918670eae9e17ed0a853dc00165da24b32984fb6ef` |
| F2 | D5_pure_llm_direct | 496 | 24/471 | 116.543616/273.503739 | 16.060693857 | 14 | 237997/164736/1817 | `0688607cf1daa01645de2b6e503a671d0a2c7ee400048742149b293d96d0c485` |
| F2 | D6_agent_codex_context_optimizer | 494 | 24/471 | 103.983386/273.503739 | 16.060693857 | 1 | 13455/0/178 | `d1bbcac59ed70f88b1a4c58e7cf18dd2ed62a12cda84927f94a7c5a1b9c9425b` |
| F2 | D7_agent_codex_capacity_shield_optimizer | 494 | 24/471 | 103.983386/273.503739 | 16.060693857 | 1 | 13455/0/178 | `7159d5800a9e2ddb7c877add88a0c297af2b84257bc351cc1d5159c064b577ab` |
| F2 | D8_agent_codex_nn_capacity_shield_optimizer | 493 | 24/471 | 103.983386/273.503739 | 16.154561908 | 1 | 13455/0/178 | `1fb2b4f165032a8dc1ac628c91bc66d2bcd6da24962085d07dd1620b2378104d` |

## Decision contract and EV2 provenance

All raw outputs declare `nyc_dispatch_decision_contract_v2`, `ev_longitudinal_v2`, profile SHA `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`. Current source `route_experiment/energy_model.py` SHA-256: `a53ac3860e7a9d169b5ceb0f8ca853a0cb73061cc106ba0ec235f1e3c600e893`; the legacy run JSONs record the model/profile, while source SHA is separately bound by this audit.

| method | recorded function | energy role |
|---|---|---|
| D0_greedy | `deterministic_greedy_v1` | `not_used_by_the_assignment_rule` |
| D1_optimizer | `global_min_cost_flow_lexicographic_v2` | `ev_v2_candidate_energy_is_the_last_global_optimizer_objective_tier` |
| D2_agent_optimizer | `global_min_cost_flow_lexicographic_v2` | `ev_v2_candidate_energy_is_the_last_global_optimizer_objective_tier` |

## D6/D7 replay check

- F1: D6 and D7 select identical vehicle/order assignments in 487/487 rounds; the D7 shield is enabled and preserves capacity in 487/487 rounds, with 0 restored candidates. This cell shows no assignment change from the shield.
- F2: D6 and D7 select identical vehicle/order assignments in 494/494 rounds; the D7 shield is enabled and preserves capacity in 494/494 rounds, with 0 restored candidates. This cell shows no assignment change from the shield.

The EV2 kWh values are model estimates over synthetic fleets and selected-date zone proxies; they are not vehicle telemetry. D5 F1 records 7 calls and 108,835 total tokens; D5 F2 records 14 calls and 239,814 total tokens. Actual account charges are not available. Successful calls are not repeated.
