# NYC dispatch audit result archives

These compressed bundles preserve the complete, hash-verified result JSONs and audit-listed CLI trace files at three distinct partial-checkpoint times. They do not merge methods or seeds. Each archive contains its own `MANIFEST.tsv`; `ALL_FILES.tsv` lists original per-file sizes and SHA-256 values.

## Checkpoint boundaries

- **seed0_ev2** — `partial_audit_checkpoint_20261027T1517`, audited `2026-10-27T07:17:44+00:00`; 82/630 results and 11/90 complete valid cells. 官方EV v2单seed时间点审计：11/90个完整有效配对格，82/630个方法结果。只能说明部分进度，不用于完整方法排名、效果或跨月结论。
- **seed3_replicate** — `seed3_audit_checkpoint_20261027T1518`, audited `2026-10-27T07:18:25+00:00`; 184/630 results and 25/90 complete valid cells. 独立seed3时间点审计：25/90格、184/630个结果；存在混合代码历史，必须隔离保存，不并入官方主汇总或跨seed结论。
- **seed4_legacy_replicate** — `seed4_legacy_audit_checkpoint_20261027T1511`, audited `2026-10-27T07:09:48+00:00`; 435/630 results and 58/90 complete valid cells. 旧协议legacy seed4部分审计：58/90格、435/630个方法结果。与EV v2主seed及其他seed分开保留，不作方法排名或合并汇总。

Archives include the audited method result JSONs marked complete in each `method_runs.csv` and every trace entry in the corresponding `usage_audit.json`. Each source file matched its audit hash before and after compression; each archive member was re-hashed after decompression. No credential-pattern matches were found. Each individual source file and compressed archive is below the regular GitHub 100 MiB file limit.

Extraction: unpack each archive into the corresponding seed root; archive members are relative to that root and contain `evaluation/...` paths. These are auditable progress snapshots, not final effect estimates.
