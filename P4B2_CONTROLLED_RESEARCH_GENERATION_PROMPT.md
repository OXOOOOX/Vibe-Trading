# P4B2 受控成分研究生成与统一知识写回实施 Prompt

## 任务名称

建立 P4B2 受控成分研究生成器：只对 P4A 已选且 P4B1 判定为缺失、过期或关键冲突的成分生成有证据约束的研究，并写回统一 Report / Claim / Fact / Evidence 链路。

## 项目路径

`C:\Users\23479\Documents\GitHub\Vibe-Trading`

## 授权边界

本 Prompt 默认授权：

- 检查现有代码和真实运行状态。
- 实现 P4B2 代码、合同、预算门控、dry-run、测试和文档。
- 使用临时数据库或真实数据库只读连接完成验证。
- 生成候选清单、证据缺口和预计 Token 预算。

本 Prompt **默认不授权**：

- 真实模型调用。
- 向真实运行知识库发布模型生成研究。
- 对 17 只成分执行批量研究。
- 自动启用盘中、日报、周报或定时研究任务。

如果任务发布者没有额外写明以下授权语句，实施者必须在 dry-run 完成后停止：

```text
已授权 P4B2 首批试运行：仅限 588870.SH 的 688256.SH、688041.SH、688981.SH；
最多 3 次模型调用；单成分输入不超过 6,000 tokens、输出不超过 600 tokens；
全批输入不超过 18,000 tokens、输出不超过 1,800 tokens；不允许自动修复或扩大标的。
```

即使存在授权，任何前置门控失败时也必须停止，不得为了完成试运行而绕过数据、证据、预算或发布质量要求。

## 一、当前基线

开始实施前必须完整读取并核对：

- `ETF_DEEP_RESEARCH_REUSE_PLAN.md`
- `P4B1_COMPONENT_RESEARCH_REUSE_PROMPT.md`
- `P4B1_COMPONENT_RESEARCH_AUDIT_2026-07-18.md`
- `ETF_UNIVERSE_PROVIDER_VALIDATION_2026-07-18.md`

当前已知基线：

- ETFUniverseProvider、ETFUniverseSnapshot 和 P4A 已完成真实外部源验收。
- P4B1 的 ComponentResearchDigest、Binding、Resolution、知识指纹和缓存已经实现。
- 首批五只 ETF 的 P4A 入选数量为 5、2、0、5、5，共 17 只成分。
- 当前 17 只代表性成分全部为 `missing`。
- `reusable=0`、`partial_reusable=0`、`stale=0`、`conflicted=0`。
- 当前真实集合没有跨 ETF 重叠，首次生成没有复用收益。
- P4B1 审计没有在真实运行库创建 P4B1 表；所有写入验证在临时副本完成。
- P4B2 尚未实现，新研究写回统一知识库的发布流程尚未建立。

因此，本任务不得把“P4B1 能发现缺失”误认为“P4B2 已经可以安全批量生成”。

## 二、目标与非目标

### 2.1 目标

实现以下完整受控链路：

```text
ETFUniverseSnapshot
  -> P4A ETFComponentSelection
  -> P4B1 ComponentDigestResolution
  -> P4B2 Generation Plan / Dry Run
  -> 预算、证据、截止时间、重复调用和授权门控
  -> 受控 Component Research 生成
  -> 结构化质量验证
  -> 统一 Report / Claim / Fact / Evidence 发布
  -> P4B1 重新解析
  -> reusable / partial_reusable Digest
  -> ETF 标的档案与后续报告复用
```

### 2.2 非目标

- 不生成 17 份完整个股 Deep Report。
- 不复制完整个股报告到 ETF 报告。
- 不建立第二套 Fact、Claim 或 Evidence 库。
- 不在 P4B2 中直接写 Canonical ComponentResearchDigest。
- 不自动修改交易建议、监控规则、仓位或报告观点。
- 不自动研究 P4A 未选中的成分。
- 不在盘中监控时自动调用模型。
- 不实现 P5 周报或 P6 正式报告观察。
- 不因缺少研究而扩大到指数全部成分。
- 不按公司名称匹配 A 股、港股或双重上市证券。

## 三、核心原则

### 3.1 P4B1 是判定层，P4B2 是缺口补充层

P4B2 只能消费 P4B1 Resolution，不得自行绕过 P4B1 重新定义候选。

默认可进入生成计划的状态：

- `missing`
- `stale`
- 关键且未解决的 `conflicted`

默认不生成：

- `reusable`
- `partial_reusable`

`partial_reusable` 只有在关键维度缺失、P4A 强制入选且得到显式授权时才可以进入补充计划；首版默认跳过。

### 3.2 模型输出不是知识事实

模型只能生成有证据引用的 Claim 草案和受控摘要。事实数据和 Evidence 必须来自模型调用前已经建立的 Evidence Pack。

禁止：

- 让模型自行猜测公司经营数据。
- 让模型输出无法追溯来源的财务数字。
- 把模型摘要直接当作 Fact。
- 没有 Evidence ID 的关键 Claim 进入发布状态。

### 3.3 P4B2 不直接写 Digest

正确顺序：

1. P4B2 发布统一 Report / Claim / Fact / Evidence。
2. 发布完成后调用 P4B1 确定性重解析。
3. P4B1 根据统一知识生成 reusable 或 partial_reusable Digest。

如果 P4B2 直接写 Digest 而绕过统一知识库，任务视为未完成。

### 3.4 证券代码隔离

- 只按规范化市场代码处理证券。
- 588870.SH 首批试运行仅允许 `688256.SH`、`688041.SH`、`688981.SH`。
- 港股必须保持 `.HK` 代码级隔离。
- 没有可靠实体映射时禁止 A/H 自动合并。
- 不允许用公司名称、简称或模糊字符串关联现有研究。

### 3.5 防止未来数据泄漏

每次生成必须记录：

- `selection_data_as_of`
- `analysis_as_of`
- `evidence_data_as_of`
- `generated_at`

任何 Report、Claim、Fact、Evidence、公告、财报或行情的时间不得晚于 `analysis_as_of`。

历史运行不得引用后来披露的信息。测试必须覆盖未来 Evidence 和未来 Claim 被拒绝。

## 四、实施前准备与只读预检

任何真实模型调用前必须完成以下步骤，并形成机器可读 Preflight Result。

### 4.1 工作区检查

- 检查 dirty worktree。
- 保留用户和其他任务的修改。
- 不执行 `git reset --hard`、`git checkout --` 或无关清理。
- 如果其他任务同时修改 reports contracts、service、knowledge 或 API，先处理冲突边界。

### 4.2 服务与数据库状态

- 检查 `127.0.0.1:8899/health`。
- 读取当前研究缓存数据库路径。
- 使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON` 记录当前表、记录数、文件大小和修改时间。
- 确认当前是否已初始化 P4B1 表。
- dry-run 阶段不得为了方便直接向真实库建表或写入测试数据。

### 4.3 生产初始化与备份

只有在获得真实试运行授权后才执行：

1. 暂停或协调可能同时写研究库的任务。
2. 解析真实数据库绝对路径。
3. 创建带时间戳的 SQLite 一致性备份。
4. 记录备份路径、大小和哈希。
5. 通过正式初始化/迁移代码创建 P4B1/P4B2 表，不手工拼接临时 SQL 修改生产库。
6. 验证 migration 幂等。
7. 确认新表初始数据符合预期。
8. 不删除已有 ETF Snapshot、P4A 结果、报告、Fact、Claim 或 Evidence。

备份失败、数据库忙、迁移失败或发现未知并发写入时必须停止。

### 4.4 P4A 和 P4B1 新鲜度

对每个候选检查：

- Universe Snapshot 是否可复用。
- Selection ID 是否对应当前 Snapshot。
- P4A 选择是否仍有效。
- P4B1 Resolution 是否基于当前知识指纹。
- 候选是否仍为允许生成的状态。
- 同一证券是否已被其他 ETF 或其他任务排队、生成或发布。

如果最新 P4B1 已变为 reusable，必须跳过模型调用。

### 4.5 用户授权和预算

Preflight 必须输出：

- 是否存在显式授权。
- 授权的 ETF 和成分范围。
- 单成分调用和 Token 上限。
- 单 ETF 上限。
- 单日全局上限。
- 当前已经消耗的当日预算。
- 本次计划预计消耗。
- 预算是否足够。

没有显式授权时只能 dry-run。

## 五、数据合同

按照项目现有 dataclass、序列化、UTC 时间和稳定 ID 风格新增或等价实现以下合同。

### 5.1 ComponentResearchGenerationPolicy

建议字段：

```text
policy_version
enabled
live_run_enabled
eligible_statuses
allow_partial_reusable
max_components_per_etf_run
max_components_per_day
max_model_calls_per_component
max_model_calls_per_day
max_input_tokens_per_component
max_output_tokens_per_component
max_input_tokens_per_day
max_output_tokens_per_day
max_auto_repairs
digest_reuse_days
allowed_report_kinds
allowed_security_markets
```

首版默认：

```text
enabled = false
live_run_enabled = false
eligible_statuses = [missing, stale, conflicted]
allow_partial_reusable = false
max_components_per_etf_run = 3
max_components_per_day = 5
max_model_calls_per_component = 1
max_model_calls_per_day = 5
max_input_tokens_per_component = 6000
max_output_tokens_per_component = 600
max_input_tokens_per_day = 30000
max_output_tokens_per_day = 3000
max_auto_repairs = 0
digest_reuse_days = 30
```

阈值必须集中配置并可通过设置安全修改，不能散落在 Prompt、API 和服务代码中。

### 5.2 ComponentResearchEvidencePack

建议字段：

```text
evidence_pack_id
component_symbol
security_name
analysis_as_of
selection_id
resolution_id
source_ids
fact_ids
evidence_ids
existing_claim_ids
conflict_ids
coverage_dimensions
missing_dimensions
market_data_status
financial_period
latest_event_at
required_field_coverage
quality
warnings
input_fingerprint
```

Evidence Pack 必须在模型调用前确定并冻结，模型只能使用其中的内容。

### 5.3 ComponentResearchGenerationJob

建议字段：

```text
job_id
idempotency_key
etf_symbol
selection_id
resolution_id
component_symbol
digest_status_before
priority
depth                         # 首版 pilot 固定 bounded
evidence_pack_id
policy_version
prompt_version
model_id
analysis_as_of
status                        # planned/blocked/approved/running/published/failed/skipped/cancelled
blocked_reasons
estimated_input_tokens
estimated_output_tokens
actual_input_tokens
actual_output_tokens
model_calls
created_at
started_at
finished_at
```

### 5.4 ComponentResearchGenerationPlan

建议字段：

```text
plan_id
etf_symbol
selection_id
resolution_id
analysis_as_of
dry_run
authorized
candidate_count
eligible_count
planned_count
skipped_reusable_count
skipped_budget_count
blocked_count
estimated_model_calls
estimated_input_tokens
estimated_output_tokens
budget_remaining
jobs
warnings
created_at
```

相同 Selection、Resolution、知识指纹、Policy 和授权范围应得到稳定 Plan ID。

### 5.5 ComponentResearchPublishResult

建议字段：

```text
publish_id
job_id
component_symbol
report_id
claim_ids
fact_ids
evidence_ids
quality_status
coverage_status
published_at
p4b1_resolution_id_after
p4b1_digest_id_after
p4b1_digest_status_after
warnings
```

## 六、Evidence Pack 生成

P4B2 首版不能把“联网搜索结果文本”直接丢给模型。必须先建立结构化、有来源、有截止时间的 Evidence Pack。

至少尝试覆盖：

- `business_exposure`：公司主营和与 ETF 主题的关系。
- `earnings_trend`：最近有效财报期的收入、利润和经营趋势。
- `catalysts`：有来源的产品、产业、政策或经营催化。
- `risks`：已披露风险和反向证据。
- `material_events`：近期重大公告或事件。

可选维度：

- `valuation`：只有行情和估值数据经过当前门控验证时才允许。
- `holder_governance`：只有正式披露来源时才允许。

来源优先级：

1. 交易所、公司公告、定期报告和监管披露。
2. 项目现有结构化财务、行情和知识缓存。
3. 可靠的官方或一手行业来源。
4. 经过来源保留的补充研究材料。

禁止把搜索摘要、未署名转载、模型记忆或无截止时间资料作为关键 Evidence。

Evidence Pack 门控：

- 没有公司身份和主营证据：阻止调用。
- 没有最近有效经营/财务信息：允许只生成结构性摘要的前提必须显式标记，否则阻止调用。
- 没有任何风险或反向证据：阻止发布为 complete。
- Evidence 时间晚于 `analysis_as_of`：排除并记录。
- 关键来源冲突未结构化保存：阻止调用或标记 conflicted。
- 价格敏感数据未验证：排除估值/量价维度，不允许模型补猜。

## 七、生成 Prompt 与结构化输出

新增专用 Profile，例如 `component_research_digest_v1`，不得复用完整上市公司 Deep Report Prompt。

模型任务应限制为：

- 基于冻结 Evidence Pack 生成短、结构化、有来源的成分研究。
- 解释该公司对 ETF 主题暴露的意义。
- 提炼经营趋势、催化、风险和失效条件。
- 每项关键 Claim 必须引用 Evidence ID。
- 明确数据截止时间和覆盖缺口。

建议结构化输出：

```text
component_symbol
analysis_as_of
research_data_as_of
business_exposure_summary
earnings_trend_summary
catalyst_claims[]
risk_claims[]
material_event_claims[]
valuation_claims[]              # 可空
holder_governance_claims[]      # 可空
invalidation_conditions[]
coverage_dimensions
missing_dimensions
warnings
```

每个 Claim 草案至少包含：

```text
text
dimension
stance
confidence
evidence_ids
valid_until
invalidation_conditions
```

输出总长度不得超过 Policy 的单成分输出 Token 上限。

## 八、模型调用前最终门控

调用模型前按顺序再次检查：

1. 功能开关 `enabled`。
2. 真实运行开关 `live_run_enabled`。
3. 用户显式授权。
4. ETF 和成分在授权白名单中。
5. 当前 Selection 仍有效。
6. P4B1 状态仍为 eligible。
7. Evidence Pack 质量通过。
8. 没有同证券正在运行或已发布的等价 Job。
9. 单成分预算通过。
10. 单 ETF 预算通过。
11. 单日全局预算通过。
12. idempotency key 未成功执行。
13. 模型 ID 和 Prompt 版本已记录。

任一失败均不得调用模型，必须返回明确 blocked/skipped 原因。

## 九、预算、幂等与并发

### 9.1 硬预算

首版默认硬预算：

| 范围 | 上限 |
|---|---:|
| 单成分模型调用 | 1 |
| 单成分输入 Token | 6,000 |
| 单成分输出 Token | 600 |
| 单 ETF 单次成分 | 3 |
| 单日全局成分 | 5 |
| 单日全局模型调用 | 5 |
| 单日全局输入 Token | 30,000 |
| 单日全局输出 Token | 3,000 |
| 自动修复 | 0 |

实际 Token 必须从模型响应或项目统一计量获取，不能只保存估算值。

### 9.2 幂等

`idempotency_key` 至少包含：

- component symbol
- selection ID
- Resolution ID
- Evidence Pack fingerprint
- analysis_as_of
- Prompt version
- model ID
- Policy version

同一 idempotency key 成功发布后，再次请求必须直接复用发布结果，不得再次调用模型。

### 9.3 single-flight

- 同一证券全局 single-flight，不以 ETF 为隔离单位。
- 两只 ETF 同时请求同一证券时只能有一个生成 Job。
- 生成成功后两只 ETF 通过 P4B1 引用同一 Digest。
- 进程内锁和数据库级幂等必须同时存在，不能只依赖内存锁。

## 十、统一知识发布

生成结果通过质量检查后，必须写入现有统一研究体系。

要求：

- 建立可索引的研究 Report/Record。
- 使用规范化 `subject_key=component_symbol`。
- 保存 `data_as_of`、`generated_at`、Prompt、模型、Token 和 Evidence Pack ID。
- Fact 必须来自已有结构化事实或 Evidence，不把模型摘要升级为 Fact。
- Claim 必须保存 Evidence 关系。
- 报告质量和覆盖状态必须明确。
- `failed_validation` 结果不得进入可复用知识。
- 发布操作必须事务化或具备可恢复状态。
- 发布失败时不留下半完成的 Claim/Report 关系。

首版可以生成结构化研究记录，但不得自动生成 PDF，也不得伪装成完整个股 Deep Report。

如果需要新增 `report_kind`，必须遵循统一报告目录的兼容和迁移规则；不得建立第二个报告入口。

发布完成后必须：

1. 重新运行 P4B1 Resolution。
2. 检查 Digest 状态是否变为 reusable 或 partial_reusable。
3. 检查新 Digest 的 Claim/Evidence 可追溯性。
4. 检查 ETF Binding 是否引用新 Digest。
5. 检查标的档案 API 是否可读取研究状态。

## 十一、数据库与审计

在现有 `research_cache.sqlite3` 中增加职责清晰、幂等初始化的 P4B2 状态表，名称可按项目风格调整，例如：

- `component_research_generation_plans`
- `component_research_generation_jobs`
- `component_research_generation_audit`
- `component_research_budget_ledger`

不得复制 Fact、Claim、Evidence 正文到这些表。

审计至少记录：

- plan/dry-run 创建。
- 授权判定。
- 候选进入和跳过原因。
- Evidence Pack 质量。
- 预算预留和实际结算。
- 模型调用开始和结束。
- schema/质量验证结果。
- 发布结果。
- P4B1 重解析结果。
- 缓存命中和避免调用。

预算预留必须避免两个并发 Job 同时超出单日上限。

## 十二、功能开关与 API

按照现有设置和鉴权方式实现，默认关闭。

建议开关：

```text
ETF_COMPONENT_RESEARCH_GENERATION_ENABLED=false
ETF_COMPONENT_RESEARCH_LIVE_RUN_ENABLED=false
```

建议最小 API：

- 创建/读取 dry-run Generation Plan。
- 查询单个 Plan 和 Job 状态。
- 显式授权后执行指定 Plan。
- 取消尚未开始的 Job。
- 查询当日预算使用情况。
- 查询某成分最近一次发布结果。

真实执行接口必须同时要求：

- 服务端功能开关开启。
- 请求携带明确确认参数。
- Plan 未过期。
- 授权范围与 Plan 完全一致。

禁止提供“研究全部 missing 成分”的无上限快捷入口。

## 十三、Dry-run 输出

实现完成后，必须先对当前真实状态执行只读 dry-run，不调用模型、不写统一知识库。

至少输出：

| ETF | 成分 | P4A理由 | P4B1状态 | Evidence Pack质量 | 是否计划 | 阻止/跳过原因 | 预计输入 | 预计输出 |
|---|---|---|---|---|---|---|---:|---:|

Dry-run 还必须输出：

- 当前 17 只状态统计。
- 根据单 ETF 和全局上限实际计划的成分数。
- 本次预计模型调用数。
- 预计 Token 总量。
- 当日剩余预算。
- 是否存在相同证券的已运行或已发布 Job。
- 是否具有真实执行授权。
- 如果没有授权，明确标记 `dry_run_only`。

将结果保存为：

`P4B2_GENERATION_DRY_RUN_YYYY-MM-DD.md`

## 十四、首批试运行计划

首批只允许研究 588870.SH 的三只强制候选：

| 成分 | 代码 | 首批原因 |
|---|---|---|
| 寒武纪 | 688256.SH | P4A 强制候选，ETF 权重高 |
| 海光信息 | 688041.SH | P4A 强制候选，ETF 权重高 |
| 中芯国际 | 688981.SH | P4A 强制候选，ETF 权重高 |

首批不得自动扩大到：

- 688008.SH 澜起科技。
- 688012.SH 中微公司。
- 其他 ETF 的 12 只成分。
- P4A 未选成分。

首批批次硬上限：

- 最多 3 次模型调用。
- 总输入不超过 18,000 tokens。
- 总输出不超过 1,800 tokens。
- 自动修复 0 次。
- 单个 Job 失败不补跑、不扩大其他标的。

没有任务发布者明确授权语句时，只生成这三只的 dry-run 计划，不执行模型。

## 十五、首批试运行后验收

如果获得授权并完成首批试运行，必须验证：

1. 实际模型调用不超过 3。
2. 实际输入/输出 Token 不超过批次硬上限。
3. 没有生成完整个股 Deep Report 或 PDF。
4. 每个发布 Claim 都可以追溯到 Evidence。
5. 没有未来数据泄漏。
6. 三只证券代码正确，没有名称匹配。
7. 发布结果进入统一 Report/Claim/Fact/Evidence。
8. P4B1 重解析后变为 reusable、partial_reusable，或给出明确未通过原因。
9. 第二次相同请求模型调用为 0。
10. 588870 ETF Binding 引用新的 Canonical Digest。
11. ETF 标的档案 API 显示研究覆盖和数据截止时间。
12. 其他 14 只成分仍未被模型处理。
13. 560010.SH 仍为 0 选择、0 生成。
14. 没有自动修改任何监控、交易或报告观点。

验收记录保存为：

`P4B2_PILOT_VALIDATION_YYYY-MM-DD.md`

## 十六、实施阶段

### P4B2-A：合同、Policy 与功能开关

- 固化 Policy、Evidence Pack、Plan、Job 和 Publish Result 合同。
- 增加默认关闭的功能开关。
- 建立稳定 ID、幂等键和预算配置。

### P4B2-B：Preflight、Evidence Pack 与 Dry-run

- 实现只读工作区、数据库、Selection、Resolution 和授权检查。
- 实现 Evidence Pack 构建和质量门控。
- 实现候选优先级、预算预演和 dry-run 文档。
- 本阶段模型调用必须为 0。

### P4B2-C：受控生成与结构化校验

- 实现专用 bounded Component Research Profile。
- 实现结构化输出和 Evidence 引用验证。
- 实现模型调用计量、预算结算、失败和取消状态。
- 默认不执行真实模型。

### P4B2-D：统一知识发布与 P4B1 回流

- 实现 Report/Claim/Fact/Evidence 事务化发布。
- 实现发布后的 P4B1 重解析。
- 接入标的档案和复用指标。
- 不生成 PDF 或完整个股 Deep Report。

### P4B2-E：专项测试、回归和 dry-run 验收

- 完成所有离线专项测试。
- 运行相关回归。
- 对真实库只读执行当前候选 dry-run。
- 保存 `P4B2_GENERATION_DRY_RUN_YYYY-MM-DD.md`。
- 没有额外授权时在此停止。

### P4B2-F：授权后的 588870 三成分试运行

- 仅在出现明确授权语句时执行。
- 执行生产备份、迁移、预算预留和三只试运行。
- 保存 `P4B2_PILOT_VALIDATION_YYYY-MM-DD.md`。
- 试运行通过前不扩展其他 ETF。

## 十七、测试要求

普通测试不得访问真实模型或外部网络。必须覆盖：

1. 功能开关默认关闭。
2. 没有授权只允许 dry-run。
3. reusable 和 partial_reusable 默认跳过。
4. missing、stale 和关键 conflicted 可进入候选。
5. P4A 未选成分不能进入计划。
6. 空 Selection 返回 0 Job。
7. 560010 空选择不调用模型。
8. 过期 Selection 或 Resolution 被阻止。
9. 未来 Evidence 被排除。
10. 名称匹配被拒绝，只接受规范代码。
11. 港股与 A 股代码隔离。
12. Evidence Pack 不完整时阻止调用。
13. 未验证行情不进入估值或量价 Claim。
14. 单成分 Token 上限生效。
15. 单 ETF 三成分上限生效。
16. 单日五成分和全局 Token 上限生效。
17. 自动修复保持为 0。
18. 相同 idempotency key 不重复调用。
19. 两只 ETF 同时请求同一证券只运行一次。
20. 并发预算预留不会超限。
21. 模型结构化输出 schema 校验。
22. 无 Evidence ID 的关键 Claim 发布失败。
23. 发布事务失败不留下半完成知识关系。
24. 成功发布后 P4B1 重解析得到新 Digest。
25. 第二次相同请求模型调用为 0。
26. Deep Report 和标的档案可以读取发布结果。
27. 不生成 PDF、正式个股 Deep Report 或无关 ETF Artifact。
28. 不修改监控、交易或组合状态。
29. 测试数据库与真实运行库隔离。
30. P0-P4B1、报告目录、知识库和设置 API 回归继续通过。

真实模型试运行必须由独立显式开关和授权控制，普通 CI 中默认跳过。

## 十八、验收标准

P4B2 实现阶段只有同时满足以下条件才算完成：

1. 默认不会调用模型。
2. dry-run 可以列出真实候选、Evidence 缺口和预计预算。
3. 只有 P4A 已选且 P4B1 eligible 的成分可以进入计划。
4. Evidence Pack 在模型调用前冻结并可审计。
5. 模型输出必须经过结构化 schema 和 Evidence 引用检查。
6. 新研究写入统一 Report/Claim/Fact/Evidence，不直接写 Digest。
7. 发布后由 P4B1 重新生成 Digest。
8. 同证券跨 ETF 使用数据库幂等和 single-flight。
9. 单成分、单 ETF、单日模型和 Token 硬上限生效。
10. 无授权时没有任何真实模型调用或生产知识写入。
11. 有授权的首批试运行严格限制为 588870 三只成分。
12. 第二次相同请求模型调用为 0。
13. 不生成 PDF、完整个股 Deep Report 或自动交易/监控变化。
14. 相关回归通过，真实库边界有记录。

## 十九、主计划更新规则

更新 `ETF_DEEP_RESEARCH_REUSE_PLAN.md` 时：

- 代码、dry-run、发布链路实际完成后，才勾选对应 P4B2 实现项。
- 没有真实模型调用时，不得写“P4B2 真实试运行完成”。
- 没有用户授权时，首批试运行必须保持未完成。
- 只有试运行通过后才讨论扩大到 513120、516010 或其他 ETF。
- P5 和 P6 继续保持未完成。

## 二十、最终汇报

最终汇报必须说明：

- 实际实现了哪些合同、表、服务、API 和开关。
- 功能开关默认值。
- 当前真实候选和 dry-run 计划。
- Evidence Pack 的来源、覆盖和阻止原因。
- 单成分、单 ETF 和单日预算是否生效。
- 是否存在用户真实试运行授权。
- 是否发生真实模型调用。
- 实际模型调用数和 Token；未调用时必须明确为 0。
- 是否写入真实运行库及写入内容。
- 是否创建了生产数据库备份，备份路径是什么。
- 是否生成统一 Report/Claim/Fact/Evidence。
- P4B1 重解析后的状态。
- 第二次请求是否真实避免模型调用。
- 测试、静态检查和回归结果。
- 是否生成 PDF、完整个股报告、监控或交易变化。
- 下一批是否具备扩大条件。

不要只提交设计文档。默认任务应完成代码、测试、相关回归和真实候选 dry-run；没有明确授权时必须在真实模型调用前停止。

## 二十一、提高输出限额的授权层与提供方边界

在基础授权之外，仅当任务发布者逐字提供以下扩展授权时，允许把首批输出上限提高到单成分 1,000、全批 3,000：

```text
已授权 P4B2 首批试运行提高输出限额：仅限 588870.SH 的 688256.SH、688041.SH、688981.SH；最多 3 次模型调用；单成分输入不超过 6,000 tokens、输出不超过 1,000 tokens；全批输入不超过 18,000 tokens、输出不超过 3,000 tokens；不允许自动修复或扩大标的。
```

扩展授权不得改变以下规则：

- 单成分调用仍为 1 次，自动修复仍为 0。
- 任一请求失败后不自动补跑，不用第四次调用覆盖失败。
- Plan 执行和 P4B1 重建输入都必须过滤到 `authorization_scope`，不得仅限制模型循环而让外围状态处理扩大到其他 Binding。
- 每次提供方请求都计入模型调用次数，即使端点在生成前拒绝且实际 token 为 0。
- 不能把“输入/输出预计值”表述为提供方已执行的硬限制。

`openai-codex` 的 ChatGPT Codex 内部端点已验证不接受公开 Responses API 的 `max_output_tokens` 字段。使用该端点时，不得声称存在服务端输出硬上限；只能依赖提示词约束和响应后的实际用量校验。若授权要求严格服务端输出硬上限，必须改用已明确支持该参数的提供方，并在切换提供方、模型和新调用次数前获得新的明确授权。

2026-07-19 的首个扩展授权请求因此在生成前以 HTTP 400 失败，后两只取消，没有自动重试、没有 token 消耗、没有研究发布。详细记录见 `P4B2_LIVE_GENERATION_ATTEMPT_2026-07-19.md` 与对应 JSON；该结果不得记为 P4B2-F 完成。

## 二十二、Codex 客户端软限制试运行结果

用户明确接受不向 Codex 发送服务端最大输出参数后，新的三成分批次仍必须保留客户端预算和“超限不发布”门控。2026-07-19 的实际结果为：

- `688256.SH`：1,893 输入、1,119 输出，超过单只 1,000，拒收。
- `688041.SH`：1,867 输入、1,323 输出，超过单只 1,000，拒收。
- `688981.SH`：累计输出预算不足，在提供方调用前取消。
- 合计 2 次模型生成调用、3,760 输入、2,442 输出、0 发布、0 自动修复。

被拒收响应不得事后绕过预算直接发布。若需要重新完整运行三只，必须取得新的三次调用与更高客户端接受上限授权；同日执行还必须显式提高单日模型调用硬上限。详细记录见 `P4B2_CODEX_SOFT_LIMIT_ATTEMPT_2026-07-19.md`。

## 二十三、功能优先授权与最终验收

在用户明确要求解除相关限制并优先保证功能实现后，新增机器可校验的功能优先授权模式。该模式仅对精确三成分试点解除单日成分数、模型调用次数和输出 Token 阻断；输入预算、Evidence 完整性、Evidence ID 白名单、结构化校验、未来数据排除、事务发布、single-flight 和幂等继续生效。

最终 live Plan `p4b2plan_83804aef10b2067a306de9d1` 已完成：3 次模型调用，5,617 输入、3,994 输出，3 个 Publish Result，3 份统一 Report，21 条有 Evidence 的 Claim，三只 P4B1 均为 `partial_reusable`。第二次相同执行模型调用为 0。

验收同时修复默认 Digest 读取被未来截止时间记录遮蔽、Plan 顶层状态不收敛、Plan API 内嵌 Job 状态陈旧三个问题。重启后的 8899 HTTP API 已返回 Plan `completed`、三个 Job `published`、三份报告和三个 `partial_reusable` Digest。详细证据见 `P4B2_FEATURE_FIRST_PILOT_VALIDATION_2026-07-19.md` 与对应 JSON。
