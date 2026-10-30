# 研究数据同步归档 Wave 2

本目录保留已选研究输出的无损 `tar.zst` 归档。每个包内使用仓库相对路径；解压会还原其相对目录。包清单逐文件记录源/归档 SHA-256、字节数和脱敏计数。

## 状态边界

- NYC 多月 EV2：父 manifest 为 89/90 配对单元审计，1 个 D7 不可用；这是有缺口的检查结果，不代表完整基线结论。
- 多 seed 复核：seed 1 审计 90/90、seed 2 审计 88/90、pooled 267/270；seed 3 分析仍待完成。不得据此宣称完整跨 seed 排名。
- 离线能耗重算：manifest 为 `completed_with_expected_gaps`；包含可重算结果、索引与哈希，保留预期缺口状态。time/money 口径中的 route choice 仍受阻。
- Porto OSM R4 v5：批次已结束，89/99 通过、10 项预检阻塞；属于部分覆盖。
- 最新 GridRoute/SUMO 验收：单次路线验收通过，不能外推为大规模性能结论。
- NYC v4 活动输出、App Support 中暂停的运行控制状态、原始请求/提示/trace、重复快照、旧版 seed4 和 superseded 侧车不在本包内。

## 解包和校验

若文件名带 `.part-###`，先按顺序合并：

```sh
cat <archive>.part-* > <archive>.tar.zst
zstd -t <archive>.tar.zst
tar --use-compress-program=zstd -xf <archive>.tar.zst -C <checkout>
```

用 `WAVE2_MANIFEST.json` 中的 SHA-256 校验各分卷与成员。所有分卷均小于 90 MiB。
