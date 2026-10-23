# 固定车队路线执行场景

本目录是新增的“真实路网固定订单序列路线执行实验”数据分支，与原来的派单层设计分开保存。

- `scenario_config.json`：场景、比较组、时钟和运行矩阵配置。
- `route_task.schema.json`：订单、路线腿和仿真事件的字段约定。
- `orders_seed_0_draft.json` 至 `orders_seed_9_draft.json`：固定车队及 OD 的草案数据。它们尚未通过最终 SUMO 有向路网的点到 Link 匹配和路线可达校验，不能作为正式实验输入。
- `../../generate_fixed_fleet_orders.py`：从 `../heat/porto_hotspot_cells_200m.csv` 和 `../../raw/osm/porto_candidate.osm` 可复现生成固定订单。

2026-10-23 的[固定订单序列生成与执行模块设计](../../../../../output/paper/固定订单序列生成与执行模块设计_20261023.md)补充了场景生成、预检、固定序列执行和结果记录规范。当前生成器及 seed 0–9 草案仍按旧端点规则，尚未按新规范更新或重生成。

每个种子固定 10 辆车、每辆 10 单、共 100 单；订单按车辆顺序执行，不存在实时派单或跨车重分配。订单生成器输出每种子 200 条需传统算法预检的路线腿。只有所有点位完成 Link 匹配且所有路线腿验证通过后，才可把状态从 `draft_preflight_pending` 改为 `frozen_prevalidated`。

信号相位、背景车流、队列和实际旅行时间将在独立 SUMO 场景中生成，均应标为仿真值。OSM 信号点不含真实配时。当前未安装 SUMO/netconvert/TraCI，正式交通网络和路线验证尚未运行。
