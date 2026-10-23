# Codex 100×100 完整路线候选的离线成本敏感性（2026-10-21）

本文件只做离线估算，模型请求数为 0。候选方式是每个任务调用一次，返回整条有序路线和逐边结构化自检标记，再由本地确定性程序检查边方向、连续性、边界、上车/下车顺序及不重复节点；整条路线全部合法后才接纳。候选协议尚未批准为正式实验口径。

## 输入量级对照

| 口径 | 每组 prompt + schema | 每组中心输入 token 估算 | 每组输入参考价 |
|---|---:|---:|---:|
| 逐边调用，重复地图和步级 schema | 446.2 MB | 约 3.69 亿 | API 标价等值约 $73.86；Standard 等值约 1,846 credits |
| 每任务一次完整路线，含 Codex schema | 5.81 MB | 约 412 万 | API 标价等值约 $0.82；Standard 等值约 20.58 credits |

按当前小图输入拟合，完整路线候选约为逐边方式的 1/90 输入 token；序列化字节约小 77 倍。这里只比较 R4 一组使用的同批 130 个可达任务。R4-A/R4-B 的不同地图表达尚未量取。

## 精确字节审计与 token 外推

脚本逐一重建 10 个冻结种子的 130 个可达任务，调用正式 route package、完整路线 prompt、地图派生路线 JSON Schema，并按 Codex CLI 适配器格式统计字节。完整路线每组 prompt 3,289,666 字节、schema 2,525,148 字节，合计 5,814,814 字节；最大单任务 schema 40,287 字节。schema 审计没有调用模型，评估真值只用于离线解析语义任务的目标点和构造任务包，没有放入 prompt。

Token 估算使用 Codex pilot v5–v8 的 28 条 8×8 调用拟合输入 token 与 prompt+schema 字节关系。观测输入每次 4,273–4,568 字节；100×100 单任务 prompt+schema 中位数为 41,742 字节，仍明显超出拟合范围。因此估算只是低/中心/高敏感性情景，不能视为 tokenizer 测量、预算或账单。中心值约 4,115,708 输入 token/组；按费率卡折算只计算输入。完整路线输出、反馈调用和 Codex 账户实际消耗未知。

可复现材料：`codex_cli_full_route_schema_size_audit_20261021.json`、`codex_cli_100x100_full_route_cost_sensitivity_20261021.json`、脚本 `scripts/audit_r4_codex_full_route_schema_sizes.py`。逐边对照见 `codex_cli_100x100_stepwise_cost_sensitivity_20261021.json`。

## 下一道门禁

继续离线核验完整路线输出最大长度、JSON Schema 对 Codex CLI 的实际支持、任意一步非法时整条提议被拒且不推进状态，并加入每任务/全批 token 停止门禁。通过后再冻结三组共同交互口径；本文件不授权模型请求或正式批次。
