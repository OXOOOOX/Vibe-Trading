# P4B2-F 588870.SH 首批授权试运行验证

执行时间：2026-07-18 23:03–23:06（Asia/Shanghai）  
执行结果：`authorized_but_blocked_before_model`  
试运行范围：仅 `588870.SH` 的 `688256.SH`、`688041.SH`、`688981.SH`

## 1. 授权与预算边界

任务发布者已提供 Prompt 要求的完整精确授权语句，机器校验结果为 `authorization_valid=true`，范围和预算均完全匹配：

- 最多 3 次模型调用。
- 单成分输入不超过 6,000 tokens，输出不超过 600 tokens。
- 全批输入不超过 18,000 tokens，输出不超过 1,800 tokens。
- 自动修复 0 次，不允许扩大标的。

用户随后提出后续可讨论放大 Token，但没有给出新的本批精确扩容授权。因此本次继续使用上述已授权硬上限，没有扩大 Token 或标的范围。

功能开关仅在本次受控执行进程内开启。执行结束后持久配置仍为：

```text
ETF_COMPONENT_RESEARCH_GENERATION_ENABLED=0
ETF_COMPONENT_RESEARCH_LIVE_RUN_ENABLED=0
```

这避免本次试运行结束后出现无人值守的真实生成。

## 2. 生产备份与初始化

真实研究库：

```text
C:\Users\23479\.vibe-trading\cache\research_cache.sqlite3
```

在任何 P4B2 生产迁移前，通过 SQLite backup API 创建了一致性备份：

```text
备份路径：C:\Users\23479\.vibe-trading\cache\backups\research_cache.pre-p4b2.20260718T150300Z.sqlite3
备份大小：22,405,120 bytes
SHA-256：1c59946df848cc07a6ed8f6434536b259172d869bd318e2ed218bad13fc3004d
```

写锁协调、备份及正式初始化均成功。重复运行正式初始化后表和数据保持一致，迁移幂等检查通过。生产库当前已初始化 P4B1 和 P4B2 表，未删除或覆盖已有 Snapshot、报告或知识记录。

## 3. 当前 P4A 与 P4B1

- P4A Selection：`p4aselection_c348ad8767d6e0c4e571a89b`。
- Selection 质量：`complete`。
- Universe 数据截止：`2026-06-30T00:00:00+00:00`。
- 本次 P4B1 Resolution：`componentresolution_5c78ed200494f91c4b7981ff`。
- Resolution 分析截止：`2026-07-18T15:04:18.056241+00:00`。
- 588870.SH 当前 P4A 实际选择 5 只，P4B1 状态均为 `missing`。

授权试运行只从这 5 只中精确选择以下 3 只，没有处理澜起科技、中微公司或其他 ETF：

| 成分 | 权重 | P4A 原因 | P4B1 试运行前状态 |
|---|---:|---|---|
| 688256.SH 寒武纪 | 9.204% | `weight_at_least_8pct`，forced=true | missing |
| 688041.SH 海光信息 | 7.913% | `weight_at_least_5pct`，forced=false | missing |
| 688981.SH 中芯国际 | 7.438% | `weight_at_least_5pct`，forced=false | missing |

## 4. 生产 Generation Plan 与 Preflight

- Plan：`p4b2plan_716d6194e99ac7961e1655e5`。
- `dry_run=false`，`authorized=true`。
- 候选 3，P4B1 eligible 3。
- `planned_count=0`，`blocked_count=3`。
- 服务健康、Selection 当前有效、Resolution 当前有效。
- P4B1/P4B2 schema 均已初始化。
- 授权范围准确，预算充足。
- 工作区为 dirty，所有既有修改均保留。

机器 Preflight 的基础设施、授权和预算检查通过，但计划级 Evidence 质量门控对三个 Job 全部失败。这一门控发生在模型调用和预算预留之前。

## 5. Evidence Pack 结果

| 成分 | Evidence Pack | Source | Fact | Evidence | Claim | 质量 |
|---|---|---:|---:|---:|---:|---|
| 688256.SH | `p4b2evidencepack_bef5927689ef173772a00f78` | 0 | 0 | 0 | 0 | insufficient |
| 688041.SH | `p4b2evidencepack_b1888e04f84cb21f3103acb1` | 0 | 0 | 0 | 0 | insufficient |
| 688981.SH | `p4b2evidencepack_a8f483cc7b4a0908805dcda8` | 0 | 0 | 0 | 0 | insufficient |

三只均缺少以下全部研究维度：

- business_exposure
- earnings_trend
- catalysts
- risks
- material_events
- valuation
- holder_governance

共同阻止原因是 `evidence_pack_quality:insufficient`；具体警告为缺少主营身份资料、近期财务证据和风险/反向证据。Token 上限不是本次阻断原因。

## 6. 真实执行结果

精确 Plan 的执行入口已实际调用两次。因为没有任何 `planned` Job，两次均返回空发布结果，且没有进入模型 runner：

| 项目 | 第一次 | 第二次 |
|---|---:|---:|
| 模型调用 | 0 | 0 |
| 输入 Token | 0 | 0 |
| 输出 Token | 0 | 0 |
| Publish Result | 0 | 0 |

生产预算台账仍为空，没有预留或结算任何额度。三个 Job 均保持 `blocked`，实际模型调用和 Token 均为 0。

## 7. 生产写入与未发生事项

本次授权后实际写入的是运行控制和确定性状态：

- P4B1：5 个零知识 Digest 状态、5 个 ETF Binding、1 个 Resolution、6 条审计。
- P4B2：3 个冻结 Evidence Pack、1 个授权 Plan、3 个 blocked Job、1 条生成审计。

未发生：

- 没有模型生成内容。
- 没有 `component_research` Report、Claim 或 Publish Result。
- 没有新增模型事实，也没有修改既有 Fact/Evidence。
- 没有 P4B1 reusable 或 partial_reusable 回流结果。
- 没有 PDF、完整个股 Deep Report 或 ETF Artifact。
- 没有修改监控、交易、仓位、组合或其他 ETF。
- `560010.SH` 没有生成任务。

## 8. 验收结论与下一步

P4B2-F 的授权、生产备份、迁移、确定性 P4B1 解析、生产计划和执行入口均已真实运行；质量门控按设计阻止了无证据模型调用。因此本批状态不是“生成成功”，而是：

```text
authorized_but_blocked_before_model
```

要完成三只成分的真实模型试运行，下一步必须先通过受控来源采集，把按精确证券代码关联的官方 Source、Evidence 和结构化 Fact 写入统一知识库，使 Evidence Pack 达到 `complete`。补证后必须重新生成新的冻结 Evidence Pack 和 Plan；不得复用本次空证据计划，也不得仅通过放大 Token 绕过门控。
