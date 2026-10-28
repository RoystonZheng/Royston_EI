# Porto OSM ord_0009 schema拒绝审计

复核时间：2026-10-28 19:06 CST。目录`outputs/route_layer/porto_r4_osm_seed0_ord0009_validator_v2b_zero_call_20261028/`的命名含`zero_call`，但实际manifest记录1次CLI调用/1次model call。

## 原始请求结果

- manifest SHA `81dac3cfebbc32df0788f205bc51e92f8724c929d27ef6fd693463849612ff61`；request ledger SHA `7758da78ce25656df3479ba048dc2baeb3020ec2d4f70075ccd744c16b4e982e`。ledger记`cli_exit_code=1`、`final_response_path=null`、`usage_available=false`。
- 原始provider call JSONL SHA `a9ec30aa9d835b093532bcc9e810b9056d9bfde11003c854d8cb608b6b85985a`返回HTTP 400 `invalid_json_schema`：provider不支持`$.properties.routes.items.anyOf[0].properties.intermediate_edge_ids.items.pattern`里的regex lookaround。因此这是已确认的schema提交拒绝，不是模型返回的非法路线，也不是delivery-unknown。
- 当前manifest把`route_comparison_status`标为`model_response_missing_or_not_delivered`，与原始HTTP错误不完全一致，应由runner owner更正为schema rejection。usage记录为不可用/0观察token；Codex账户实际扣费未报告，不能断言实际费用为0。

## EV2与决策边界

没有模型路线响应，SUMO trial未完成；`ev2_trace_audit.json` SHA `7571a584d58f0304573091a23d223e4397f19d3c117a4e3581fb3b782f3a1258`记录0腿、`mismatch_or_incomplete`。能耗必须标不可估/null，不能填0。EV2模块SHA `a53ac3860e7a9d169b5ceb0f8a853a0cb73061cc106ba0ec235f1e3c600e893`、profile SHA `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`与当前版本一致。固定目标FASTEST，time-money gate为not_applicable（SHA `48750d74d76d74de138a2a7c64042377f3dfbe2f921951463a3b2da60db8c656`），未应用经济评分。

保留该失败请求，不重发ord_0009。应将schema里的regex改为provider兼容写法并用真实发送格式做离线验证，再通过唯一runner以新的独立订单继续实验。