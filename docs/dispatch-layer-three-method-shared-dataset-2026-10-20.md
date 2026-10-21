# NYC TLC 三组调度方法共享输入包

日期：2026-10-20  
状态：共享数据适配与 D5 运行接口已实现；C3 单种子离线机制比较完成；尚无纯 LLM 实际运行结果。  
产物：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/`

## 目的和方法分组

将 NYC TLC 2024 订单轨迹适配成同一调度输入，供以下三组方法使用：

1. 传统派单：D0 逐单贪心、D1 集中式优化器。
2. 纯 LLM：D5，LLM 直接输出候选车辆—订单匹配，不调用 Agent 情境过滤或全局优化器。
3. 本课题方法：车辆 Agent + 全局优化器；D2 是离线规则情境组，D3/D4 提供函数/反馈消融。D3 为离线 LLM 函数代理；D4 默认离线反馈代理，只有配置 DeepSeek 才是在线候选反馈。

调度状态、订单、车队和候选均进入现有 `dispatch-agent-v1` 输入/输出契约。三个方法组共享同一订单、车队、候选成本、硬约束、输入快照和独立校验器。共同候选枚举仅负责计算可达性和输入成本；最终匹配分别由规则、纯 LLM 或 Agent+优化器决定。

## 数据包结构

`benchmark_manifest.json` 按文件哈希登记三个案例：C2 4 单/3 车，C3 44 单/18 车，C4 790 单/12、24、48 车。C2 适合做小规模接口烟雾；C3 是首个多区域机制比较案例；C4 用于负载阶梯，不建议在预算未核算时直接对纯 LLM 跑完整 790 单。

请求事件来自 TLC 2024 年 1 月已完成单人订单轨迹；接载行程的区域 OD 时间/距离使用 1 月 1–15 日训练样本直接中位数，测试事件来自 1 月 17 日或 20 日。两者时间分离。全部原始月文件、行级审计和既有 C2–C4 场景仍保留，新的共享输入包是派生副本。

## 接驾代理成本

原始 TLC 不提供空车初始位置或空车接驾 ETA。旧 C2–C4 机制预检把车辆放在上车区并将同区接驾设为零、跨区设为不可达；其对三组方法区分度不足。新包使用官方 Taxi Zone 多边形计算区域质心，再以固定参数构造接驾代理：球面直线距离 × 1.3 绕行系数，除以 5 m/s；同区设 300 m。参数全部写在 `pickup_cost_policy.json`，各组使用相同数值。

这能形成可比较的跨区候选等待和空驶成本，但不是道路路线或真实 ETA。车辆位置仍是合成的、区域质心也是近似代表点。载客段继续使用已完成行程的区域 OD 统计，不把 TLC 代理边拼成真实路网。结果仅用于机制对比，不代表纽约市真实服务率、等待时间或车辆运营成本。

## 运行状态和范围

- D5 纯 LLM 直接匹配已接入 `DispatchSimulation`，要求 `feedback_mode=deepseek`；NYC 通用 runner 还要求显式传 `--allow-live-api`，客户端为零重试、512 输出 token、关闭 thinking。没有发出 API 请求。
- 官方 DeepSeek 文档在 2026-10-20 核查的 `deepseek-flash` 对应 DeepSeek-V4.1-Flash-1017。D5 记录模型返回标识、token 和延迟，并按官方峰时价格保守估算美元成本；精确账单仍以服务端账单为准。[官方价格页](https://api-docs.deepseek.com/quick_start/pricing/)
- 已保留一次旧“同区零成本”C2 离线接口预检，放在 `outputs/.../three_method_benchmark_v1_same_zone_preflight_2026-10-20/`；该预检不是新代理成本下的主结果。
- 修正候选快照契约后，C3 seed 0 上 D0/D1/D2 第一轮的完整 `DispatchInput` 完全相同，SHA-256 为 `64ee43c0115a7d44fc47090664ca47346f8ef649a3f232e9a6d3e1bd49cc58a3`，含 72 个车辆—订单候选。三法各运行 42 轮，校验失败 0；都完成 16/44 单，平均接驾等待 74.272 秒，P95 为 196.924 秒，空驶距离为 5,941.776 米。模拟电耗 D0 为 30.647 kWh，D1/D2 为 26.252 kWh；D0 与 D1 的完成数相同，但有一笔车辆—订单配对不同。D2 从 67 个硬约束可行候选中保留 47 个进入优化器。累计 5,473 是所有轮次生成的候选组合数，不是硬可行候选数。该结果只代表一个合成车队场景和一个种子的机制检查，不能说明统计显著性或真实 NYC 性能。产物在 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1/results/C3_manhattan_multizone_44_order_burst/single_fleet/`。
- D5 输入序列化与响应解析使用本地 mock 检查通过，API 调用数为 0；这只确认接口可用，不代表模型决策能力。纯 LLM 首轮建议从 C2 做单次小预算 smoke，再决定是否运行 C3；C4 需先核算请求数与 token 预算。
- NYC runner 增加 `--single-round-smoke`，可在首个共享快照后停止；C2 首轮有候选时 D5 至多发出一次 API 请求，客户端零重试。该模式只检查单轮派单输出，不应读取其不完整仿真服务率。
- 候选输入契约修复前的 C3 临时结果保存在 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/three_method_benchmark_v1_preinput_contract_fix_2026-10-20/`，不纳入当前对照。

可复现入口：`scripts/build_nyc_tlc_three_method_benchmark.py`、`scripts/run_nyc_tlc_dispatch_benchmark.py`。共享包说明和命令见 `outputs/.../three_method_benchmark_v1/README.md`。
