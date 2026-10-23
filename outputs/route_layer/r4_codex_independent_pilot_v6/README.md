# R4 Codex 独立 8×8 续跑 v6

## 本轮结果

- 地图：`grid-8x8-seed-0`，64 个节点、205 条有向边；任务：`node_0_5 → node_1_2（上车）→ node_6_2（下车）`。
- 从 v5 最后一个合法节点 `node_4_7` 继续。本轮最多 8 次请求、零重试；实际发出 8 次，8 个动作都通过动态方向白名单和本地确定性校验，没有工具调用、非法动作或重试。
- 第 6 次模型将 `target_node_is_new` 错报为 `false`；程序验证目标 `node_2_5` 未访问、该有向边合法，因此接纳动作，同时记录模型自检差异。路线状态只由本地硬校验推进。
- 到达 `node_1_4` 后触及请求上限，仍未到上车点或下车点。该产物是合法路线前缀，不是完整取送路线或正式 R4 结果。
- 含最初 smoke、前序续跑和本轮在内，当前合法前缀为 14 条边、1,400 m、167.083 秒、`energy_v1` 仿真能耗 0.260 kWh；本轮新增 8 条边。

## 用量与费率估算

本轮 8 次 Codex CLI 请求共 78,004 输入 token（缓存输入 0）和 8,203 输出 token，合计 86,207 token。按 GPT-5.6 Luna Standard 费率卡估算为 0.636110 credits 等值；按 API token 价格折算为 $0.02544440。含前序任务链共 15 次请求、150,453 输入和 12,253 输出 token，费率卡等值 1.119855 credits / $0.04479420。Codex CLI 未提供账户实际账单或订阅额度扣减金额。

价格依据（2026-10-21 核对）：[Codex Standard credits 费率](https://developers.openai.com/codex/pricing)、[GPT-5.6 Luna API token 价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。缓存 token 是输入 token 的子集，本轮无缓存输入。

## 文件

- [交互式地图与轨迹回放](replay.html)
- [逐请求原始记录索引](traces/index.html)
- [最终审计](final_audit.json)
- [token 与费率复算](codex_usage_cost_reconciliation.json)
- [运行清单](manifest.json)
- [状态与逐次动作](state.json)

本次 8×8 Codex 试跑不能证明 100×100 可扩展性、路线最优性或跨种子成功率。正式路线实验仍须另行冻结相同方法、输入和预算口径。
