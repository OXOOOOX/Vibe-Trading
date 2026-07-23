# P4B2-G 受控生产证据入库验证

执行时间：2026-07-18 23:45-23:50（Asia/Shanghai）  
执行模式：`production_evidence_ingest_zero_model`  
范围：仅 `588870.SH` 的 `688256.SH`、`688041.SH`、`688981.SH`

机器结果：`P4B2_EVIDENCE_INGEST_RESULT_2026-07-18.json`

## 1. 执行结论

受控生产证据入库已完成：生产统一知识库新增 6 个上交所 SourceDocument、15 条 Evidence 和 9 条 Fact，未产生结构化冲突。三只新的 Evidence Pack 均为 `complete`，核心覆盖率均为 1.0，仅继续排除 valuation。

本阶段没有调用模型，没有生成 Claim、`component_research` Report 或 Publish Result，也没有处理授权范围外证券。

## 2. 备份与来源校验

写入前已创建 SQLite 一致性备份：

```text
C:\Users\23479\.vibe-trading\cache\backups\research_cache.p4b2g-pre-ingest-20260718T154530Z.sqlite3
```

- 大小：23,146,496 bytes。
- SHA-256：`a9214d60c4083de8d8ebb9122ce02fea6664fe042bc09ca1406f1f9c7a6168f2`。
- 备份和写入后的生产数据库 `PRAGMA integrity_check` 均为 `ok`。
- 6 份 PDF 的文件大小、页数、SHA-256、证券代码和公司名称在写入前重新核验。
- 知识正文只包含机器核验且已目视复核的指定页面，并在正文头部保存官方 URL、PDF SHA-256、披露时间和受控页码。

## 3. 知识写入结果

| 成分 | SourceDocument | Evidence | Fact | 新冲突 |
|---|---:|---:|---:|---:|
| 688256.SH 寒武纪 | 2 | 5 | 3 | 0 |
| 688041.SH 海光信息 | 2 | 5 | 3 | 0 |
| 688981.SH 中芯国际 | 2 | 5 | 3 | 0 |
| 合计 | 6 | 15 | 9 | 0 |

统一知识全局计数由 SourceDocument 186、Evidence 191、Fact 2,302 变为 192、206、2,311。Claim 仍为 1,606，报告目录仍为 119，知识关系仍为 134。

中芯国际财务 Fact 保留 `CNY_thousand` 原始单位；三个一季报来源均保留未经审计属性。中芯国际二季度指引只作为明确标记的前瞻 Evidence，没有升级为已实现 Fact。

## 4. Evidence Pack 与新 Plan

新 dry-run Plan：`p4b2plan_478711f7012eb0b07eecd1ea`。

| 成分 | Pack | 质量 | 核心覆盖率 | 输入上界 | 输出上限 |
|---|---|---|---:|---:|---:|
| 688256.SH | `p4b2evidencepack_6454f31e27b42bafa8230611` | complete | 1.0 | 5,752 | 600 |
| 688041.SH | `p4b2evidencepack_706ed09a0300b45a5ef42ceb` | complete | 1.0 | 5,748 | 600 |
| 688981.SH | `p4b2evidencepack_9a5f42b1e4e21dc4836d9dbb` | complete | 1.0 | 5,707 | 600 |
| 全批 | - | complete | 1.0 | 17,207 | 1,800 |

全批输入仍低于 18,000 上限，余量 793；每只均低于 6,000。输出计划恰好占满 1,800 上限，没有剩余输出预算。

该 Plan 特意保持 `dry_run=true`、`authorized=false`，三个 Job 为 `planned` 且无 Evidence 阻断，但未执行。后续真实生成必须创建新的有效 live Plan 并重新获得模型执行授权。

## 5. 零调用和范围复核

- 实际模型调用、输入 Token、输出 Token：`0 / 0 / 0`。
- Budget Ledger：0。
- Publish Result：0。
- `component_research` Report：0。
- 自动修复：0。
- 扩大标的：0。
- 持久开关 `ETF_COMPONENT_RESEARCH_GENERATION_ENABLED=0`、`ETF_COMPONENT_RESEARCH_LIVE_RUN_ENABLED=0`。

P4B1 仍保持原有 `missing` 语义，因为本阶段只补入来源、Evidence 和 Fact，没有生成报告 Claim。当前变化是 P4B2 Evidence 门控已从 `insufficient` 变为三只全部 `complete`。

## 6. 下一门槛

下一阶段才是三只成分的真实模型生成与统一知识发布。建议继续保持精确三成分、最多 3 次调用和 0 次自动修复；模型执行前需要确认是否沿用 18,000/1,800 硬上限，尤其是输出预算当前没有余量。
