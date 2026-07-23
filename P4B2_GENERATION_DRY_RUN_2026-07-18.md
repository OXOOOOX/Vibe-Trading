# P4B2 受控成分研究生成 Dry-run

执行时间：2026-07-18T14:44:27.353670+00:00（Asia/Shanghai 22:44）  
运行模式：`dry_run_only`  
真实模型调用：0  
真实输入 / 输出 Token：0 / 0  
真实统一知识写入：0  
生产数据库备份：未创建（没有真实试运行授权，也没有生产迁移或写入）

## 1. 只读边界与 Preflight

- 服务：`http://127.0.0.1:8899/health` 返回 200 / `healthy`。
- 真实研究库：`C:\Users\23479\.vibe-trading\cache\research_cache.sqlite3`。
- 只读窗口前后文件大小均为 `22,380,544` 字节，`mtime_ns` 均为 `1784385444586163000`；真实库未被本次 dry-run 修改。
- 真实库当前已有 P4B1 表，但均为空：`component_research_digests=0`、`etf_component_digest_bindings=0`、`component_digest_resolutions=0`、`component_research_audit=0`。这与早先 P4B1 审计时“表尚未初始化”的基线不同，说明运行状态在两次检查之间已经变化；本次没有把临时 Resolution 写回真实库。
- 真实库仍没有 P4B2 表；本次只在 SQLite backup API 创建的临时副本中初始化 P4B1/P4B2。临时副本写入：Evidence Pack 3、Plan 1、Job 3、Audit 1、Budget Ledger 0、Publish Result 0。
- 当前 worktree 为 dirty；本次保留了所有已有修改，没有清理、重置或覆盖无关文件。
- 当前真实 P4A Selection 仍是 `p4aselection_c348ad8767d6e0c4e571a89b`，对应 2026-06-30 Universe Snapshot，快照质量和选择均为 `complete`。
- 真实库没有当前 Resolution 行，所以 dry-run 在临时副本中确定性生成 `componentresolution_b1a82622872542d3c69c6bde`；真实执行前必须在授权备份后通过正式初始化和重解析建立当前 Resolution。
- 显式试运行授权：不存在。Preflight 标记 `dry_run_only=true`。

Preflight 阻止项：

1. `explicit_pilot_authorization_missing`
2. `p4b2_schema_not_initialized`
3. `resolution_not_initialized_in_runtime_database`

## 2. 当前 17 只状态复核

本次在真实库只读副本中，按规范市场代码和当前知识截止时间重新解析同日 P4B1 审计的 17 只代表性成分：

| 状态 | 数量 |
|---|---:|
| reusable | 0 |
| partial_reusable | 0 |
| stale | 0 |
| missing | 17 |
| conflicted | 0 |

所有 17 只仍为真正的代码级 `missing`，没有可复用 Claim/Evidence 覆盖，也没有跨 ETF 重叠收益。`560010.SH` 继续保持 0 选择、0 Job、0 模型调用。

## 3. 588870.SH 首批精确候选

Dry-run Plan：`p4b2plan_e8ed72c42056db62adde976e`  
Selection：`p4aselection_c348ad8767d6e0c4e571a89b`  
Resolution（仅临时副本）：`componentresolution_b1a82622872542d3c69c6bde`

| ETF | 成分 | P4A 实际理由 | P4B1 状态 | Evidence Pack 质量 | 是否计划调用 | 阻止/跳过原因 | 预计输入 | 预计输出 |
|---|---|---|---|---|---|---|---:|---:|
| 588870.SH | 寒武纪 `688256.SH` | `weight_at_least_8pct`，forced=true，权重 9.204% | missing | insufficient，0 / 3 核心维度 | 否 | `evidence_pack_quality:insufficient` | 0 | 0 |
| 588870.SH | 海光信息 `688041.SH` | `weight_at_least_5pct`，forced=false，权重 7.913% | missing | insufficient，0 / 3 核心维度 | 否 | `evidence_pack_quality:insufficient` | 0 | 0 |
| 588870.SH | 中芯国际 `688981.SH` | `weight_at_least_5pct`，forced=false，权重 7.438% | missing | insufficient，0 / 3 核心维度 | 否 | `evidence_pack_quality:insufficient` | 0 | 0 |

真实 P4A 数据与任务文本中的“这三只均为强制候选”并不完全一致：当前只有寒武纪是强制入选；海光信息和中芯国际是普通高权重入选。本实现没有为了匹配任务描述而修改 P4A 结果。三只仍属于允许的首批精确白名单，但 Evidence 门控优先，当前不得调用模型。

未进入首批范围：`688008.SH` 澜起科技、`688012.SH` 中微公司、其他 ETF 的 12 只成分，以及所有 P4A 未选成分。

## 4. Evidence Pack 缺口

三只 Evidence Pack 均满足代码隔离和未来数据过滤，但统一知识中没有可用于本次生成的结构化 Evidence、Fact 或既有 Claim：

| 成分 | Source | Fact | Evidence | Claim | Conflict | 核心覆盖 | 质量 |
|---|---:|---:|---:|---:|---:|---|---|
| 688256.SH | 0 | 0 | 0 | 0 | 0 | 0 / 3 | insufficient |
| 688041.SH | 0 | 0 | 0 | 0 | 0 | 0 / 3 | insufficient |
| 688981.SH | 0 | 0 | 0 | 0 | 0 | 0 / 3 | insufficient |

每只均明确记录：

- `missing_business_identity_evidence`
- `missing_recent_financial_evidence`
- `missing_risk_counterevidence`

因此不能把联网搜索摘要、模型记忆或无截止时间资料直接送入模型，也不能让模型补猜财务数字或风险。下一步前置工作是把交易所、公司公告、定期报告和监管披露先登记为统一 Source / Evidence / Fact，再重新生成冻结 Evidence Pack。

## 5. 预算与幂等状态

| 项目 | 当日已用 | 当前剩余 | 本次可执行计划 |
|---|---:|---:|---:|
| 成分数 | 0 | 5 | 0 |
| 模型调用 | 0 | 5 | 0 |
| 输入 Token | 0 | 30,000 | 0 |
| 输出 Token | 0 | 3,000 | 0 |

- 单成分硬上限仍为 1 次、输入 6,000、输出 600；单 ETF 最多 3 只；自动修复为 0。
- 若三只 Evidence Pack 后续全部通过，授权批次的理论硬上限仍为 3 次、18,000 输入、1,800 输出；当前可执行估算为 0，因为 Evidence 门控先失败。
- 真实库没有 P4B2 Job 表，因此不存在同证券 `running` 或 `published` Job，也没有预算预留。
- 专项测试已验证数据库级 single-flight、原子预算预留、成功发布后的幂等命中，以及第二次相同请求模型调用为 0；本次真实 dry-run 没有可发布结果可供实测复用。

## 6. 停止结论

本次按授权边界停止在 dry-run：

- 没有真实模型调用，模型调用数为 0。
- 没有真实输入/输出 Token，均为 0。
- 没有写入真实 Report / Claim / Fact / Evidence。
- 没有 P4B1 生产 Resolution、Digest 或 Binding 写入。
- 没有创建生产数据库备份，因为备份与迁移只允许在获得精确试运行授权后执行。
- 没有生成 PDF、完整个股 Deep Report、ETF Artifact，也没有修改监控、交易、仓位或组合状态。
- 当前既缺精确授权，也缺完整 Evidence Pack；即使以后补充授权，Evidence 门控未通过时仍必须停止。
