# R4 worker_00 失败与 outcome_unknown 逐项 EV2 对账

复核时间：2026-10-28 09:15 UTC（17:15 CST）。这是对冻结worker快照的只读原始attempt/trace/state/final_audit复核，不修改活动manifest/state，不调用模型，不重跑路线。

- worker manifest SHA-256：`e63799f49295ad3dcdd5a1ccf24f495c35e96788cdc3e8c24751faf97268afcb`；记录1,944请求、8 route_complete、6 failed、9 outcome_unknown、1 active、5 pending。活动`task-0006-0007`的request66在途，已接受65个动作；本复核不改变执行状态。
- 6个manifest `failed`任务的最后CLI trace均出现终态错误消息`exceeded retry limit, last status: 429 Too Many Requests`，CLI exit 1，事件流无model response/usage event。故失败原因是provider 429限流，不是LLM返回非法路径，也不是validator拒绝。费用/usage是否记账仍无法从CLI记录确认；旧final_audit把这些request号码同时列入`delivery_unknown_request_numbers`，应解释为“无模型输出/usage可观测”，不能反过来把已看到的429说成未知HTTP状态。

## 六个 provider-429 任务

| task | 失败请求 | 原始trace SHA-256 | final_audit SHA-256 | 已校验合法前缀 | EV2前缀能耗 | 完整路线能耗 |
|---|---:|---|---|---:|---:|---|
| task-0003-0010 | 62 | `3e801d29df449f5644135949fad707c505deece71b76afa0ebbdbc6e6279775c` | `398bc120b6e7ae13aa4cca1827bace63e8546fb8ba0360ead861218258cf0639` | 6.1 km | 0.692662521 kWh | null（未完成）|
| formal-task-0007-feasible | 67 | `499ddb305b6d9a9adf79c0c386cf12390e49ea8a7c174b927cfa61e9d008f084` | `6de223a77b5a9125dff8886f70e0ef8ae2004cefc572f2696688740dba9c67b3` | 6.6 km | 0.758910258 kWh | null（未完成）|
| formal-task-0006-detour | 37 | `13499d81f67d96a78f81edb115f3c38796e32b38f81c66b409643017b1040cea` | `9dad491ef05caef2f7d48ab815a139f31e79e9802a766b4eef0fa4af4b8c3cd0` | 3.6 km | 0.459724454 kWh | null（未完成）|
| task-0004-0008 | 31 | `c47742c73496bf1d2087850e8df5df600452334f9e2b1c9c4b6b700506d2c909` | `44d2477679f47ec7990cf25bad0a0f9ca9b914f813c9a0eb63dd0f4c67a1ff0c` | 3.0 km | 0.375614683 kWh | null（未完成）|
| task-0007-0004 | 29 | `e0c7c8688f810f59ff3af0d98e415fb27ed835a90b37b8a22602dbfa796ab10f` | `d55adef6ba0c3d85a2a7288201dafecd21d20d3b7fa66308a14c0f0d573efa63` | 2.8 km | 0.333386776 kWh | null（未完成）|
| task-0003-0002 | 45 | `bf708030fdc2269c97dd397ba1986d87f0264e89ccc93a6e175377c69df1f606` | `679de078d67fd2fbdb7cbe45d60b651200cef8e2bc847b37594a466fbbaff8a2` | 4.4 km | 0.509701399 kWh | null（未完成）|

各final audit的`invalid_or_stopped_actions`为空；表中能耗只覆盖已接受合法前缀，不是完整路线耗电。供应商拒绝响应的后续部分没有可估能耗；未以0补齐。

## 九个 manifest outcome_unknown 标签的逐项状态

原始state/attempt核对发现，这一计数有8项与其当前最后attempt/final audit不一致。7项有完整`accepted_legal_step`终态和partial audit，final audit的delivery-unknown列表为空；1项`task-0001-0007`已有19个接受动作、EV2前缀能耗0.235694663 kWh，但final audit缺失，下一次调用前driver因`NameError: MAX_FORMAL_STEPWISE_CALLS not defined`退出。只有`task-0007-0008` request25在当前state及final audit明确为`delivery_and_usage_unknown`。

| task | manifest标签 | 原始attempt末项 | EV2合法前缀 | 复核结论 |
|---|---|---|---|---|
| formal-task-0006-feasible | outcome_unknown | request6 `accepted_legal_step` | 0.6 km / 0.077856066 kWh | 无未决request证据；task仍未完成 |
| task-0005-0004 | outcome_unknown | request4 `accepted_legal_step` | 0.4 km / 0.048601485 kWh | 无未决request证据；task仍未完成 |
| task-0009-0008 | outcome_unknown | request16 `accepted_legal_step` | 1.6 km / 0.188765352 kWh | 无未决request证据；task仍未完成 |
| task-0009-0002 | outcome_unknown | request2 `accepted_legal_step` | 0.2 km / 0.026635609 kWh | 无未决request证据；task仍未完成 |
| task-0001-0001 | outcome_unknown | request68 `accepted_legal_step` | 5.7 km / 0.638031365 kWh | 无未决request证据；task仍未完成 |
| task-0002-0004 | outcome_unknown | request101 `accepted_legal_step` | 10.1 km / 1.170896764 kWh | 无未决request证据；task仍未完成 |
| task-0008-0008 | outcome_unknown | request41 `accepted_legal_step` | 2.0 km / 0.224423625 kWh | 无未决request证据；task仍未完成 |
| task-0001-0007 | outcome_unknown | request19 `accepted_legal_step`; 下一次driver在调用前NameError | 1.9 km / 0.235694663 kWh | runner代码错误，不是模型输出/网络unknown；缺final audit，需安全重建partial audit |
| task-0007-0008 | outcome_unknown | request25 `delivery_and_usage_unknown` | 2.4 km / 0.285552097 kWh | 真正投递/usage未知；不得重放该request |

因此当前manifest的“6 failed/9 outcome_unknown”是执行索引状态，不能直接当作6条路线违规及9个未知模型响应。已向owner反馈原始证据，请其追加只读reconciliation；活动runner保持运行。上述partial能耗仅用于已校验前缀，不计完整route-complete分子，也不可求和冒充车辆实际能耗。

## 与非法候选能耗记录的关系

本批六个429任务和九个manifest unknown任务中，保存的accepted prefixes没有validator拒绝动作；它们的prefix EV2能耗已保留。对单独的Porto `ord_0004` pilot，LLM两腿返回空edge sequence且validator拒绝：模型候选没有可映射路段，候选能耗应为null / not estimable，不能填写0。Fallback实际执行轨迹另记EV2 1.158307 kWh，不归给LLM路线。对未来有明确可映射edge trace但被validator拒绝的候选，可按同一EV2输入生成反事实能耗sidecar，并标注“未执行/非法候选”；不可与合法行程或fallback实际仿真能耗混算。

## 分类补充：任务失败原因与请求级delivery/usage状态并列保留

对六个provider-429任务，原始CLI JSONL明确显示错误信息`exceeded retry limit, last status: 429 Too Many Requests`，因此**任务级失败原因**为provider 429；与此同时，它们的final_audit把对应请求号列在`delivery_unknown_request_numbers` / `usage_unknown_request_numbers`，没有canonical final JSON或usage event。因此**请求级审计状态**仍为delivery/usage unknown。两层不可互相覆盖：不把6个failed任务再并入manifest的9个outcome_unknown任务计数，也不抹去原final_audit的unknown字段，不推断实际费用为零；所有原请求均不重发。

截至09:19:20Z worker_00 manifest SHA-256 `dd582b9d1e4ddf438cbe1a6245e0eea9e2ff40ad9e8af332e358d8399e7f2849`记录1,965请求、8 complete/6 failed/9 outcome_unknown/1 active/5 pending；活动task6-0007 request87在途，已接受86动作并完成pickup。当前state的`route_action_budget`与`effective_authorized_new_request_limit`为2449，`candidate_request_limit`为null；未见512人工单任务上限。
