# R4 当前验收门审计

状态：**未通过，正式矩阵保持暂停**。本次没有模型调用。

## 已通过的安全证据

- 新鲜零模型调用测试：`tests/test_r4_codex_pilot_acceptance.py::R4CodexPilotAcceptanceTests::test_formal_online_step_path_rejects_oneway_reverse_backtracks_once_and_reroutes`，1 passed。formal online step 函数拒绝单行反向 `W`（`one_way_direction`），回退恰好1个节点至`node_0_0`，下一次检索仅给合法方向`N`，随后完成路线。
- SUMO全程复放：[manifest.json](../r4_grid_route_sumo_acceptance_20261030T004602CST/manifest.json)。冻结地图`grid-100x100-seed-0`，完成113条有向边，车辆完成，路径匹配，单行违规0。此复放使用已归档完成路线，不是新的LLM路线样本。

## 尚未通过的门禁

Codex R4正式路线链没有调用`score_route_candidates`。因此现有R4正式任务没有同任务多路线候选池，也没有在同一候选池上同时运行4:6与6:4、保存完整排名/理由和对应的EV2/SUMO结果。代码里已有两组profile，但目前没有R4正式选择证据。用户同意的3.5 CNY/乘客公里是假设收入，不是实测票价；尚未接到本批候选路线的正式选择输出。

因此，既有单行校验与SUMO安全证据不能替代所缺的R4决策链证据；129项正式矩阵继续暂停。下一步是先把候选池、EV2 v2和两个权重profile的输出接入R4正式路线完成路径，再用零调用测试核对选择审计，并对被选路线完成SUMO零单行违规验收。

## 机器证据

- 详细机器清单：[`manifest.json`](manifest.json)
- 零调用测试日志：[`offline_test.log`](offline_test.log)
- 回退门禁报告：[`../r4_oneway_rollback_gate_20261029T2145CST/manifest.json`](../r4_oneway_rollback_gate_20261029T2145CST/manifest.json)
- 新SUMO日志：[`../r4_grid_route_sumo_acceptance_20261030T004602CST/sumo.log`](../r4_grid_route_sumo_acceptance_20261030T004602CST/sumo.log)
- 当前权威parent manifest SHA-256：`23b87def7ac1ebc238eadec69e8fee93915a0428181f5cf76a17e5c9ca5988a4`；请求数12089，任务状态`{'route_complete': 58, 'outcome_unknown': 10, 'failed': 14, 'ready': 25, 'pending': 22}`，请求级delivery unknown 26，无in-flight。


## 当前用户执行边界（2026-10-30 CST）

本轮用户直接选择“在途结束后停发（推荐）”。以上单行回退与SUMO结果均为零模型调用安全门证据；该报告中的技术缺口与后续建议不构成恢复正式R4模型请求的授权。正式矩阵继续暂停。
