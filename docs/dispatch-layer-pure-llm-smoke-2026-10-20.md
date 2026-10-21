# NYC TLC 纯 LLM 单轮 Smoke（2026-10-20）

## 运行设置

在共享输入包 C2（4 单、3 辆合成车）上运行 D5 `D5_pure_llm_direct`，seed 0，只执行首个派单决策。调用沿用本地路线层环境配置；模型为 `deepseek-flash`，单次请求、零重试、512 输出 token 上限。实际向模型发送 1 次请求。

## 结果

模型返回可解析的候选 ID 列表，返回状态为 `accepted`。首轮输入含 12 个车辆—订单候选，其中 3 个满足分配约束；模型选出 3 个候选，对应 3 辆不同车辆和 3 个不同订单。所选候选均存在于当前输入且通过硬约束。独立校验器状态为 `valid`，硬约束错误 0、回退 0、LLM 错误 0。

| 项目 | 结果 |
|---|---:|
| LLM 请求数 / 重试数 | 1 / 0 |
| 输入 / 输出 tokens | 3,048 / 101 |
| 模型延迟 | 1,008.35 ms |
| 峰时价格估算 | USD 0.0010356 |
| 候选数 / 接受匹配数 | 12 / 3 |
| 一车一单 | 通过 |
| 硬约束验证 | 通过 |

## 结论边界

这是一次真实模型的单轮接口与约束 smoke，说明当前 C2 输入能够被模型处理并返回可接受匹配。记录标记为 `partial_run=true`；派单后 3 单处于 assigned、1 单仍 waiting，尚未完成接驾或行程，因此不能把 summary 中的 completed/service-rate 字段解读成服务表现，也不能据此判断 D5 优于 D0/D1。成本是按返回 token 和代码中固定的 DeepSeek 峰时价格估算，不是账单凭证。

## 产物

原始结果：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/pure_llm_smoke_v1/C2_four_same_second_orders_three_vehicles/single_fleet/D5_pure_llm_direct_seed_0.json`。

输入数据仍使用 NYC TLC 历史已完成订单、合成车辆初始位置和质心距离接驾代理；该结果只用于受控机制预检，不代表纽约真实运营表现。
