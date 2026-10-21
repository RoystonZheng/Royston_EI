# C4 订单区 Taxi Zone 多边形切片

这是 NYC Open Data Taxi Zones 数据集 `8meu-9t5y` 中，C4 订单涉及的 126 个上/下车区域 ID 的 GeoJSON 子集。当前 API 返回 125 个多边形，`LocationID=265` 未返回；查询条件、原始响应哈希和区域 ID 清单记录在 `manifest.json`。

多边形用于把 C4 区域 OD 端点与曼哈顿 DOT 观测子网作空间可达性上界筛查。区域内任意已观测节点被视为免费接入，故结果只用于排除明显不可覆盖组合，不是精确上车点、完整 ETA 或路由成功率。
