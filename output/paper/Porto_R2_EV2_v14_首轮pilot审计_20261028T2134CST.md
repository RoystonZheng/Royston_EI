# Porto R2 EV2 v1.14 首轮 pilot 审计

审计时间：2026-10-28 21:34 CST  
分组：**Porto OSM/SUMO真实道路几何 + 合成订单/交通仿真；路线层；pure LLM；seed 0 / FASTEST；pilot，不是正式矩阵。**

## 状态结论

旧 Porto R2 traditional 与 pure-LLM 两个矩阵仍各自为 20/20 terminal，未改写、未重跑。新一轮 R2 在 `porto_r2_execute_20261025` worktree 里按 v1.14 signed-net EV2 protocol 做了完整零调用预检，并启动唯一的 `s0:FASTEST:ord_0001` 模型 pilot。

pilot 有一个成功收到的模型响应（Codex CLI exit 0、一个 turn 和 usage event、retry=0），模型对接驾与载客两段都返回空 `intermediate_edge_ids`。两个端点都不同且没有直接合法转向，validator 因 `ValueError:illegal_directed_turn` 拒绝两段路线。随后 runner 用共同的 `shared_link_fallback` 完成了该订单的 SUMO 仿真。因此这是“模型响应成功—两段被 validator 拒绝—fallback 完成仿真”，不是模型未返回、不是 API 失败，也不是有效 LLM 路线结果。

结果目录 `porto_r2_llm_20261028T132929Z` 状态为 `pure_llm_budget_blocked_partial`：实际调用 1 次、完成 1 个订单（两段均为 fallback）、失败 0、validator 拒绝 2 段；其余 1,999 个订单因本次 pilot 的单调用范围而未执行。0/20 单元可作为完整方法结果。不能把 fallback 成绩记入 pure-LLM 路线质量或跨方法比较。

## 预检与执行证据

- 零调用预检：`porto_r2_signednet_ev2_v14_preflight_20261028T2123CST`，2000/2000 请求记录和路线腿记录生成，`requests_failed=0`，`actual_model_call_count=0`。2000 请求的旧默认 75,000-token门槛均超限；本次只对pilot单请求使用更高估算门槛。pilot估算输入为 205,979 tokens；Codex CLI模型目录当前显示 `gpt-5.6-luna` context window 272,000。
- 唯一模型请求：实际用量 input 211,041、output 813、reasoning 768 tokens；费率标准额度估算 1.079595，Codex CLI未报告账户实扣。成功或投递未知的调用不得重放；该成功响应已归档。
- 同一固定单调用pilot共有1,000,000,000-token和10,000-standard-credit runner门槛，仅用于满足runner参数；本批次停在`max_model_calls=1`是pilot边界，不是整个R2项目额度上限。
- 新模型响应SHA `3e59391135509ecaf98e52fef512d861d5a28b3386edad4decca85104caa11d4`；原始CLI JSONL SHA `d3e30921425bae75275ca7c8bf3414ba58da619c2074ae0af8a7d6918613e8bc`；调用账本状态 `accepted`、turn=1、usage event=1、retry=0。
- 预检summary SHA `ac1b50cb6afa73005b8338a0cd08f5ccae13ef38a0067bad548b544d65546ef4`；manifest `request_manifest.jsonl` SHA `cb2d42e3309c157a8ae079c4040056cfa8690a58e5a356fd146019e676a08124`；`route_leg_manifest.jsonl` SHA `975917474968a7310a9de0754b9da7d6de374d44dbaf9bfc836806791bb45fc5`。
- frozen input manifest SHA `846e01d17dadf41eba1284ddd26311c382d9b38af745ad7c1cfc4c00c46db898`；network SHA `9481618cfdc0f0fa2a4300a38db6fe7f13321dfc22c554feb1cf91b4f8aa7387`；R2 runner SHA `2db21ba0e429d3845ffd0a267f4bfe2beffa72ac339768ca29bd297f149958bb`；route problem SHA `95b34e883e8df7104ca04ce16d05cd4849a8ec201e2580d0e4afbdfb737bb6d9`；protocol v1.14 SHA `887472d75934f293b1ff15e564b48dfd481b9a3e36feafb983e2baf0dca506d7`；R1 v5 runtime SHA `dda71ccc0ea75c08d12a29968870a99dfad3cbf491786fe4acd43bbef05b2e05`。

## EV2分段能耗核对

EV2模型公式为推进能耗 − 再生能耗 + 辅机能耗。R2运行时对接驾腿传入payload 0 kg，对载客腿传入profile的75 kg。源模块 `route_experiment/energy_model.py` SHA `a53ac3860e7a9d169b5ceb0f8ca853a0cb73061cc106ba0ec235f1e3c600e893`；两条route-leg结果都绑定 `ev_longitudinal_v2` profile SHA `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。该profile的辅助功率为800 W。

本次EV2观测是fallback路线的SUMO模拟迹线：空载接驾signed-net 175.855876 Wh；75 kg载客signed-net 445.293814 Wh。分段先分别累计signed-net，再各自按 `max(segment_net,0)` 计价，电价假设0.30 EUR/kWh，复算费用0.18634491 EUR，与汇总一致；0.20/0.40 EUR/kWh敏感性为0.12422994/0.24845988 EUR。这是固定电价模拟估值，不是充电账单。SUMO速度/加速度轨迹未包含道路坡度，结果标记 `observed_sumo_speed_trace_grade_missing`；该订单没有独立的pickup停车/重启或dwell能耗账目，不能把它们说成已计入。两个逐route-leg记录都包含profile SHA，但summary顶层 `energy_profile_sha256` 为空；已要求owner把module/profile SHA及payload 0/75 kg补到聚合summary/manifest。轨迹JSON SHA `9e16ed1cac4eb65742067b47a28f54a721ac2ea9dd876d43013c99a4250dac6f`。

## 返工要求与后续门槛

已向Porto owner发送返工：旧成功调用不重放；对端点不同且无直接合法转向的leg，schema不得允许空中间路径（按leg生成`minItems >= 1`），prompt明确要求沿合法successor链给出完整中间Link；仍无合法路径时如实保留validator拒绝与fallback。更新后用唯一新run-id重做零调用预检和一个fresh pilot；只有两段模型提案均通过validator并留下可审计的模型选路记录，才考虑扩展正式矩阵。

正式FASTEST/LOWEST_ENERGY两目标、10 seed矩阵尚未开始。本pilot只覆盖seed 0 FASTEST 的一个订单；它不能证明模型性能，也不能代表R2完整实验。
