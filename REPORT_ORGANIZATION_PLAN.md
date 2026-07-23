# Vibe-Trading 统一报告组织与观点比较计划

> 状态：M0–M3 已完成并启用；M4 进入运行观察期  
> 实施日期：2026-07-18  
> 前置能力：统一研究知识层已完成并在当前运行环境启用  
> 本计划范围：报告目录、标的档案、周期观点、报告关系与观点差异  
> 不在本批次：量价分析规则、放量突破判定、监控条件和交易点位算法

## 一、方案结论

报告组织层建设在现有统一研究知识层之上，不再建设第二套历史资料、Evidence、Fact、Claim、全文检索或事实冲突系统。

```text
统一研究知识层（已完成）
  Source Document → Evidence → Fact → Claim
  ResearchCoveragePlan / ResearchDelta / 历史复用 / 事实冲突
                         ↓ 引用稳定 ID
统一报告组织层（本计划）
  Report Catalog → Horizon Viewpoint → Report Relation → ViewpointDelta
                         ↓
  标的档案 / 组合档案 / 报告时间线 / 多报告比较 / AI 变化说明
```

核心原则：

1. 研究知识层回答“资料从哪里来、事实是否有效、事实发生了什么变化”。
2. 报告组织层回答“生成了哪些报告、适用于什么周期、判断发生了什么变化”。
3. `ResearchDelta` 只比较事实和证据；`ViewpointDelta` 只比较观点、行动、置信度和条件。
4. 历史 Claim 可以展示和比较，但不能作为新事实，也不能替代新研究。
5. AI 只解释结构化差异，不读取整篇历史 Markdown，不覆盖确定性比较结果。
6. 不设置综合“可信度分数”；分别展示来源、事实校验、覆盖、时效和适用周期。

### 1.1 本次实施结果

- 知识数据库已升级到 schema v2，保留 v1 数据并生成报告目录迁移前备份。
- 已新增统一报告契约、目录表、四周期观点、Artifact/版本关系和差异缓存。
- Deep Report、个股日更、组合日报和正式监控研究已在发布后幂等登记；`skip_report` 和普通监控事件不会入库。
- 服务启动时会自动扫描 `catalog_epoch` 之后的遗漏报告；也可通过维护 API 手动修复，失败次数和最近失败原因可查询。
- `/reports` 已默认进入统一报告中心，包含标的档案、全部新报告、组合档案和旧版兼容入口。
- 结构化比较先判定同周期延续/更新/分化或跨周期差异；AI解释可独立关闭、失败和审计。
- 未回填旧报告目录；当前真实目录为 0 条，等待启用后下一份正式报告自然进入。旧报告和既有知识链接未删除。

## 二、已完成的前置基线

以下能力视为既有能力，本计划只调用，不重复实现：

- `research_cache.sqlite3` 中的版本化知识表、内容寻址对象库和迁移前备份。
- 完整网页/PDF 保存、章节段落分块及中文 FTS5/BM25 检索。
- 稳定 ID 去重、Evidence/Fact 登记、数字重放、口径冲突和更正覆盖。
- `query_research_knowledge`、`read_research_document` 及知识搜索、证券历史、原始来源 API。
- Deep Report 八领域覆盖计划、`research_coverage` 和 `history_delta`。
- 报告预览中的“本次使用的信息、与上次相比、历史研究”。
- 日报、监控、飞书继续遵守实时数据硬门控，不会被历史知识绕过。

当前回填基线：

| 项目 | 数量 |
|---|---:|
| 原研究缓存 | 1567 |
| 原始来源文档 | 172 |
| 全局去重 Evidence | 173 |
| 全局去重 Fact | 1562 |
| 历史 Claim | 295 |
| 已关联 Deep Report | 15 |
| 待处理事实冲突 | 25 |

当前验证基线为后端相关测试 152 项、前端交互测试 37 项、TypeScript 检查和生产构建通过，API 8899 健康检查正常。后续报告组织改动不得降低这些基线。

## 三、职责边界与历史策略

### 3.1 知识层继续拥有

- `source_documents`、`source_chunks`、`evidence_records`、`fact_records`、`claim_records`。
- `report_knowledge_links`、`fact_conflicts`、`research_coverage_snapshots`、`research_deltas`。
- 原文存储、检索、事实时效、事实冲突和历史事实复用。
- 旧 Deep Report 和明确研究会话的知识回填。

### 3.2 报告组织层新增

- 正式报告的统一目录和产物引用。
- 标的、组合、报告类型、生成时间、数据截止时间及版本关系。
- 一份报告在不同分析周期下的结构化观点。
- 同周期观点变化和跨周期观点并列展示。
- 面向用户的标的档案、组合档案、时间线及比较界面。

### 3.3 历史报告策略

- 不把回填的 15 份 Deep Report 自动迁入新报告目录；它们继续通过旧版入口和知识历史访问。
- 新报告目录只原生登记功能上线后正式发布的报告。
- 旧版报告入口可以在新报告中心稳定后下线，但不能直接物理删除仍被 `claim_records.origin_id` 或 `report_knowledge_links.report_id` 引用的 Artifact。
- 将来如需物理清理，必须先建立保留 `report_id`、revision、哈希、来源关系和 Claim 定位的墓碑记录，再执行独立的保留策略迁移。
- 普通聊天不进入报告目录；明确的 `research_session` 只进入知识层，除非后续被正式发布为报告。

## 四、统一报告契约

### 4.1 ReportEnvelope

所有新正式报告在发布完成后登记统一 `ReportEnvelope`：

```text
ReportEnvelope
- schema_version
- report_id
- family_id
- report_kind
- subject_type: symbol | portfolio
- subject_key
- symbol
- security_name
- status: published | diagnostic | archived
- report_quality_status: passed | passed_with_gaps | failed_validation
- coverage_status: complete | partial | insufficient | unknown
- generated_at
- data_as_of
- source_type
- source_id
- source_revision
- knowledge_link
  - coverage_snapshot_id
  - evidence_ids[]
  - fact_ids[]
  - claim_ids[]
- viewpoints[]
- artifacts[]
- relations[]
```

规则：

- `generated_at` 是报告生成时间；`data_as_of` 是报告实际使用的数据截止时间，两者必须分开。
- `report_quality_status` 是报告整体校验状态，不复用或覆盖 Fact 的 `validation_status`。
- `knowledge_link` 只保存知识层稳定 ID，不复制 Evidence、Fact 或 Claim 正文。
- `failed_validation` 可以登记为 `diagnostic`，但不能参与当前观点选择。
- Daily Run 因数据硬门控执行 `skip_report` 时不登记报告。

### 4.2 报告类型

首批允许：

```text
deep_research
daily_holding
daily_portfolio
weekly_review
monitor_research
```

报告类型描述交付形态，不代表分析周期。回测结果不进入投资报告目录，继续保留在策略/旧版报告入口。

### 4.3 周期观点

一份报告可以声明一个或多个 `ReportViewpoint`，但同一报告在同一周期只能有一个主观点：

```text
ReportViewpoint
- viewpoint_id
- report_id
- horizon: intraday | daily | weekly | structural
- stance: bullish | neutral | bearish | mixed | unknown
- action: observe | add | reduce | exit | not_applicable
- confidence: low | medium | high | unknown
- summary_claim_id
- reason_claim_ids[]
- risk_claim_ids[]
- condition_claim_ids[]
- invalidation_claim_ids[]
- valid_from
- valid_until
```

周期含义：

- `intraday`：盘中分钟至当日收盘前。
- `daily`：当日或下一交易日。
- `weekly`：本周或未来一至两周。
- `structural`：波段、中期及结构性判断。

观点字段必须来自已发布报告的结构化结论和 Claim，不允许报告目录模型补造新的市场事实、价格或交易条件。

### 4.4 生产者映射

- Deep Report：发布时从结论章节生成 `viewpoint_manifest`，每个观点必须绑定已登记 Claim。
- 持仓日更：将现有 `action`、`confidence`、`trend`、`reasons`、`risks` 和条件映射为 `daily` 观点。
- 组合日报：登记为 `subject_type=portfolio`；个股子报告分别进入对应标的档案。
- 周报：登记 `weekly` 观点，不因生成日期较新而覆盖 `daily` 或 `structural` 观点。
- 监控研究：由正式研究产物声明周期；监控事件和告警本身不是报告。

### 4.5 版本与前后关系

- 同一数据截止点、同一分析任务的章节修订或修复属于 `revision_of`。
- 使用更新数据重新分析属于 `supersedes`，即使上游 Deep Report 仍保留 parent revision 关系。
- 新交易日的日报是新一期报告，不是旧日报的原地修订。
- 报告 Artifact 保持不可变；新 revision 或 successor 使用新 `report_id`。

## 五、存储与服务设计

### 5.1 数据库

在现有 `research_cache.sqlite3` 的下一知识 schema 版本中追加：

```text
report_catalog_entries
report_viewpoints
report_artifact_links
report_relations
viewpoint_delta_cache
report_library_meta
```

关键约束：

- `UNIQUE(source_type, source_id, source_revision)`，保证重复发布和修复任务幂等。
- `UNIQUE(report_id, horizon)`，防止同一报告产生多个互相竞争的主观点。
- Artifact 只保存相对来源定位、文件名、媒体类型、SHA-256 和可用状态，不向 API 暴露服务器绝对路径。
- `report_library_meta` 保存启用时间 `catalog_epoch`；默认不扫描或迁移早于该时间的旧报告。
- schema 迁移继续使用 SQLite Backup API，功能开关关闭后旧报告、知识搜索和日报流程保持可用。

### 5.2 服务边界

新增 `ReportLibraryService`，负责：

- 幂等登记报告、观点、Artifact 和关系。
- 按标的、组合、周期、类型、状态和日期查询。
- 计算每个周期的最新观点和最近完整观点。
- 生成确定性的 `ViewpointDelta`。
- 从知识层读取 `ResearchDelta` 和 Claim 片段。

现有 `ResearchKnowledgeStore` 继续负责知识数据和数据库迁移；`ReportLibraryService` 不复制 Evidence、Fact 或事实冲突。日报与监控报告只通过既有知识层接口登记自身 Claim，并保存返回的稳定 ID。

报告发布成功但目录登记失败时，原报告仍算发布成功；服务记录索引失败并由启动/维护任务扫描 `catalog_epoch` 之后的 Deep Report、Daily Run 和监控研究进行幂等修复。目录故障不得触发重复模型生成或重复飞书投递。

## 六、当前观点与差异判定

### 6.1 当前观点

系统不生成跨周期的单一“总观点”。每个标的分别维护四条周期轨道。

同一轨道的候选条件：

1. `status=published`。
2. `report_quality_status != failed_validation`。
3. 观点周期相同。
4. `valid_until` 未过期或未声明固定失效时间。

排序规则：

1. `data_as_of` 较新者优先。
2. 相同数据截止点下，`generated_at` 较新者优先。
3. 同一报告家族内，revision 较新者优先。

如果最新观点为 `passed_with_gaps` 或 `coverage_status=partial`，界面同时展示：

- 最新可用观点。
- 最近一份 `passed + complete` 的完整观点。
- 两者数据截止点和缺口差异。

系统不允许一份更旧但质量较高的报告静默覆盖最新观点，也不允许一份最新但校验失败的诊断产物成为当前观点。

### 6.2 ResearchDelta 与 ViewpointDelta

```text
ResearchDelta（现有知识层）
- added / updated / confirmed / superseded / contradicted / stale / still_unverified
- 比较 Fact、Evidence、来源和时效

ViewpointDelta（新增报告层）
- relation: continued | updated | diverged | different_horizon | not_comparable
- stance_changes
- action_changes
- confidence_changes
- condition_changes
- invalidation_changes
- base_viewpoint_id
- current_viewpoint_id
- research_delta_report_id
```

判定规则：

- 不同标的：`not_comparable`。
- 不同周期：`different_horizon`，不标记为冲突。
- 同周期且行动、倾向、关键条件均未变化：`continued`。
- 同周期且有一般字段变化：`updated`。
- 同周期、有效期重叠且倾向或行动方向相反：`diverged`。
- 事实更新原因从对应 `ResearchDelta` 展开，不由报告层重新计算。

### 6.3 AI 变化说明

AI 输入仅包含：

- 两份或多份结构化 `ReportViewpoint`。
- 确定性的 `ViewpointDelta`。
- 已存在的 `ResearchDelta`。
- 通过 Claim ID 读取的必要最小片段。

禁止输入整篇旧 Markdown，禁止把历史 Claim 提升为 Fact，禁止生成统一交易结论。AI 输出必须携带 `report_id + claim_id + section_id` 引用；失败时只返回结构化差异，不影响报告阅读。

## 七、API 与前端

### 7.1 新增 API

```text
GET  /report-library/reports
GET  /report-library/status
GET  /report-library/subjects/{subject_key}
GET  /report-library/reports/{report_id}
POST /report-library/comparisons
POST /report-library/reconcile
```

- 列表接口支持 symbol/name、subject type、report kind、horizon、质量、状态、日期和 cursor 分页。
- 标的详情返回四周期当前观点、最近完整观点和报告时间线。
- 报告详情返回 `ReportEnvelope`，知识内容继续通过现有 `/research/...` API 穿透。
- 比较接口接受 2 至 4 个 `{report_id, horizon}`，先返回结构化结果；`include_ai_summary=true` 时附加有引用的AI说明。
- 现有 `/reports`、`/portfolio/daily-runs`、Artifact 下载和知识 API 保持兼容。

### 7.2 报告中心

将 `/reports` 逐步升级为：

1. **标的档案**：默认入口，按代码或名称搜索。
2. **全部新报告**：按生成时间查看正式新报告和显著变化。
3. **组合档案**：组合日报及其持仓子报告。
4. **旧版报告**：保留当前 Deep Report、Daily Run 和回测兼容入口。

标的档案包含：

- 四个周期观点卡片。
- 最新观点与最近完整观点的并列提示。
- 按交易日排列的报告时间线。
- 报告类型、周期、质量、覆盖、生成时间和数据截止时间筛选。
- 选择 2 至 4 份报告进行比较的抽屉。

比较抽屉先展示确定性字段表，再展示 `ResearchDelta`，最后展示可选的AI变化说明。现有报告右侧面板继续承担原文、来源、历史研究和知识变化穿透，不新增独立知识库管理页面。

## 八、实施顺序

### M0：共享契约与迁移门禁

- [x] 冻结 `ReportEnvelope`、`ReportViewpoint` 和 `ViewpointDelta`。
- [x] 将报告目录表加入知识 schema 下一版本并生成迁移前备份。
- [x] 增加 `VIBE_TRADING_REPORT_LIBRARY_ENABLED` 和 `VIBE_TRADING_REPORT_VIEWPOINT_AI_ENABLED`。
- [x] 保存 `catalog_epoch`，明确不回填旧报告目录。

验收：关闭报告目录开关时，现有知识层、Deep Report、日报、监控、飞书和旧版报告行为不变。

### M1：新报告登记

- [x] Deep Report 发布后登记目录、知识链接、观点和 Artifact。
- [x] 持仓日更和组合日报完成后登记目录。
- [x] 正式监控研究接入；普通监控事件不登记为报告。
- [x] 增加启动时幂等修复、手动修复 API 和索引失败指标。

验收：开关启用后产生的新正式报告全部可查；重复回调不产生重复记录；目录故障不造成重复生成或重复投递。

### M2：标的档案与时间线

- [x] 实现报告目录和标的详情 API。
- [x] 将新报告中心接入标的档案、组合档案和旧版入口。
- [x] 实现四周期观点卡、最新/完整双基准和报告时间线。

验收：用户能从一个标的入口找到其全部新报告，并明确区分生成时间、数据时间和适用周期。

### M3：结构化比较与AI说明

- [x] 实现 `ViewpointDelta` 和 2 至 4 份报告比较。
- [x] 联合展示现有 `ResearchDelta`。
- [x] AI只读取结构化差异和最小 Claim 片段，输出可追溯引用。

验收：跨周期观点不误报冲突；同周期变化可解释；关闭AI开关后结构化比较完整可用。

### M4：兼容入口收敛

- [ ] 持续累计新报告登记覆盖率、孤儿记录和用户访问路径；当前已有目录状态、失败计数和修复结果，访问路径统计需在真实报告产生后观察。
- [x] 新入口已成为默认入口，旧版报告保留为显式兼容标签页。
- [x] 未物理删除任何历史 Artifact；后续删除必须另立保留策略计划。

## 九、测试与验收

本次实施验证：

- 后端报告/知识/日报/监控相关回归：57 项通过。
- 前端全量测试：36 个测试文件、358 项通过。
- TypeScript 检查与 `npm run build`：通过。
- API 已重启在 8899；健康检查、目录状态、列表和修复接口通过。
- 测试环境默认关闭生产目录钩子；已确认测试后真实目录仍为 0 条，没有测试数据污染。
- 应用内浏览器存在初始化冲突，因此未完成截图级页面验收；组件交互、生产构建和真实 API 验证已完成。

### 9.1 后端

- schema v2 迁移、备份、重复迁移和功能开关回退。
- `ReportEnvelope` 必填字段、时区、枚举和知识 ID 引用校验。
- Deep Report、持仓日更、组合日报和监控研究的幂等登记。
- `failed_validation`、`skip_report` 和目录登记失败路径。
- revision 与 successor 的区分，Artifact 不可变和目录修复。
- 当前观点排序、过期处理、最新/完整双基准。
- 同周期延续、更新、分化以及跨周期不可直接冲突。
- AI比较输入不得包含整篇 Markdown；每条说明必须引用 Claim。
- 现有 152 项知识/报告相关后端测试持续通过。

### 9.2 前端

- 标的搜索、筛选、cursor 分页和空状态。
- 四周期观点卡及数据截止时间展示。
- 最新报告存在缺口时，同时展示最近完整观点。
- 2 至 4 份报告选择、结构化差异、知识差异和引用跳转。
- AI不可用、知识层关闭、Artifact 缺失和旧版入口兼容。
- 现有 37 项报告预览交互测试、TypeScript 检查和生产构建持续通过。

### 9.3 588870 真实验收

自动化契约和前端交互已使用 588870 覆盖同周期变化、最新/完整双基准、生成时间与数据时间展示。真实目录坚持不造数、不回填旧目录，因此以下真实验收将在 588870 下一份正式新报告产生后完成：

新体系启用后，以 588870 的新生成报告验证：

1. 当日报告进入 `daily`，周度意见进入 `weekly`，深度结构判断进入 `structural`。
2. 日度谨慎、周度积极时显示“不同周期”，不显示为互相否定。
3. 下一份当日报告改变行动或条件时，生成同周期 `ViewpointDelta`。
4. 如果新报告证据有缺口，同时展示最新观点与最近完整观点。
5. 观点变化原因只能引用 ResearchDelta 和 Claim，不重新搜索或注入旧报告全文。
6. 报告中心的差异展示不改变任何量价条件、监控计划或告警执行。

## 十、Definition of Done

- 功能启用后，所有新正式投资报告均进入统一目录，且没有超过一个修复周期的孤儿报告。
- 每个报告明确展示标的、类型、生成时间、数据截止时间、质量、覆盖和适用周期。
- 同一标的四个周期独立维护当前观点，不生成失真的跨周期总判断。
- `ResearchDelta` 与 `ViewpointDelta` 职责清晰且不存在第二套事实比较逻辑。
- 所有观点依据可沿 Claim → Fact/Evidence → Source Document 穿透。
- AI说明可关闭、可失败、可审计，且不会影响确定性比较结果。
- 旧报告知识引用不因旧版入口下线而失效。
- 现有知识层、实时数据门控、日报、监控、飞书和 Artifact API 保持兼容。
- 量价分析和报告意见如何转化为监控条件留在下一份独立计划中处理。
