# Vibe-Trading 统一深度研究报告 v3 执行计划

> 状态：实施中；R1（公司/ETF）、R2、R4 核心闭环已落地，R5/R6 完成首轮前端接入，指数与真实运行验收待后续  
> 制定日期：2026-07-18  
> 适用范围：个股、ETF、指数深度研究，以及正式单标的日报、周报和监控研究  
> 前置基线：统一研究知识层 schema v2、统一报告目录 M0–M3、Deep Report Workspace/Revision、ETF Research P0–P4B 已完成；P4B2-F 的真实补充生成不是本轮报告编译前置条件  
> ETF 专项基线：[ETF_DEEP_RESEARCH_REUSE_PLAN.md](ETF_DEEP_RESEARCH_REUSE_PLAN.md)  
> 基线验证：成分研究复用、受控生成和 ETF 穿透相关专项共 41 项通过

## 一、目标与实施结论

本计划不建设第二套 Evidence、Fact、Claim、历史研究、报告目录或监控引擎。在现有能力上增加四个共享层：

1. **资产类型与 Profile 路由**：上市公司、ETF、指数使用各自的数据门控、章节和分析模型。
2. **统一引用层**：正文使用论文式小标，末尾同时生成数据依据与可追溯参考资料。
3. **统一阅读与产物层**：Web、Markdown、PDF 使用同一份结构化报告数据。
4. **报告到监控的结构化接口**：所有正式报告生成 `monitoring_bundle.json`，但报告流水线不得直接创建、修改或激活监控计划。

最终数据流：

```text
用户发起正式研究
  -> InstrumentResolver 唯一解析资产类型
  -> Profile Registry 选择公司 / ETF / 指数合同
  -> ResearchCoveragePlan 生成本轮证据缺口
  -> 历史有效资料复用 + 实时敏感数据刷新
  -> 最多三轮定向搜索、原文读取、冲突核验
  -> Snapshot / Fact / 确定性计算
  -> ETF 专用：P4A Selection + P4B Digest Resolution
  -> Report Workspace 提交章节和结构化监控候选
  -> Claim 支撑、语义、数字、引用、监控 Bundle 审计
  -> ReportViewModel
  -> report.md + report.pdf + monitoring_bundle.json
  -> ReportEnvelope 登记统一报告目录
```

核心发布原则：

- 证据缺口按 Claim 或模块局部降级，不轻易让整份报告失败。
- 股票身份、报告币种、财务期间、ETF/指数身份等硬门控失败时只生成诊断。
- `passed_with_gaps` 仍是正式报告，必须生成 Markdown、PDF 和监控 Bundle。
- 监控候选允许为空；不得为了格式完整编造点位。
- 报告质量与监控可执行性分别校验。
- 正式报告不输出概率加权目标价，不把估值便宜直接转换为买入点。

### 1.1 本轮实施范围

本轮不再开发或复制 ETF P4A/P4B。本轮工作的核心是把已经完成的 ETF 研究底座接入正式 Deep Report，并同时完成公司、ETF、指数共用的引用、阅读和监控产物升级。

本轮包含：

- 将 `ETFComponentSelection`、`ComponentDigestResolution` 和已发布的成分研究记录编译进 ETF `holding_penetration` 正式章节。
- 在 Web、Markdown、PDF 中展示成分选择原因、解释覆盖率、摘要复用状态、数据截止时间和来源。
- 完成论文式正文小标、末尾参考资料、内部报告索引码和引用闭包。
- 为正式报告生成独立校验的 `monitoring_bundle.json`。
- 完成结构化 Web 阅读、真实进度、诊断与部分完整状态的用户语言。

本轮不包含：

- 重写 P4A 的成分采集、集中度计算、自适应选择或缓存逻辑。
- 重写 P4B 的 Digest 解析、受控生成、预算、发布和回流逻辑。
- 在生成 ETF 报告时隐式触发新的成分模型研究；需要新增研究时仍走 P4B 自己的功能开关、证据、预算和授权门控。
- 新建 `weekly_review` 生产器；它继续属于 ETF P5 的独立后续工作。
- 从报告侧直接创建、修改或激活监控计划，以及任何自动交易行为。

P4B“能力完成”和“当前标的已有足够可复用摘要”是两个不同概念。正式报告必须读取本次 `analysis_as_of` 下的真实 Resolution；不能因为 P4B 已完成就假定所有成分均为 `reusable`，也不能把历史审计中的 `missing` 数量硬编码到报告。

## 二、职责边界

### 2.1 继续复用的既有能力

研究知识层继续负责：

- `source_documents`、正文分块、全文检索和内容哈希。
- Evidence、Fact、Claim 的稳定 ID。
- 来源类型、独立发布者、事实时效、更正和冲突。
- `ResearchCoveragePlan`、`ResearchDelta` 和历史事实复用。

统一报告目录继续负责：

- ReportEnvelope、Artifact、revision/successor 关系。
- 标的时间线、四周期观点、最新/最近完整双基准。
- `ViewpointDelta` 和结构化报告比较。

监控系统继续负责：

- 当前可验证原始价格、分钟/日线数据和交易日历。
- 条件白名单、接近区、确认、失效和观察窗口。
- 计划草案、人工复核、独立授权的自主激活和事件推送。
- 自动交易继续固定为 `forbidden`。

ETF P4 研究底座继续负责：

- P4A：ETF Universe、权重标准化、集中度、确定性候选选择、Selection ID、缓存和来源审计。
- P4B1：全局 `ComponentResearchDigest`、ETF 专属 Binding、`ComponentDigestResolution`、新鲜度与冲突判定。
- P4B2：证据约束的受控补充、预算与授权门控、统一 Report/Claim/Fact/Evidence 发布，以及发布后的 P4B1 确定性回流。
- P4 的结果是报告编译输入，不由报告写作 Agent 重算、覆盖或扩大范围。

### 2.2 本计划新增的能力

- `InstrumentResolver` 与 `index_deep_research` Profile。
- Claim 级来源支撑和语义审计。
- `ReportViewModel` 阅读合同。
- `ReportReference`、论文式引用和内部报告索引码。
- `MonitoringBundle`、`StructuralContext`、`MonitoringCandidate`。
- 真实研究进度事件和 10～15 分钟检索预算。
- 新报告的 Bundle 优先监控接入路径。

## 三、资产类型与 Profile 路由

### 3.1 公共路由

```text
instrument_type=company_equity -> equity_deep_research
instrument_type=etf            -> etf_deep_research
instrument_type=index          -> index_deep_research
instrument_type=portfolio      -> 组合容器，不直接生成跨标的点位
```

`InstrumentResolver` 必须输出：

```text
symbol
security_name
instrument_type
market
currency
tick_size
resolver_source
resolved_at
```

无法唯一解析或资产类型冲突时 fail closed，不允许模型自行选择 Profile。

### 3.2 公司 Profile

沿用现有八章：

1. 核心结论。
2. 公司业务与产业位置。
3. 三张报表与财务质量。
4. 会计科目异常与核查清单。
5. 市值隐含预期。
6. 长期经营情景与叙事阶段。
7. 反方论证、风险与催化剂。
8. 结论与跟踪框架。

监控候选只允许来自结构性失效、长期支撑/阻力、突破确认、趋势恢复和重新研究事件。公司估值结论不能直接成为价格触发器。

### 3.3 ETF Profile

沿用现有 ETF 八章：

1. 核心结论。
2. 指数与产品。
3. 暴露结构。
4. 聚合基本面。
5. 量价结构。
6. 份额、流动性与跟踪。
7. 关键持仓穿透。
8. 情景与跟踪框架。

ETF 硬门控：

- 基金、管理人、交易所和跟踪指数可唯一确认。
- 指数规则版本或当前规则来源可识别。
- 成分/权重披露带日期和覆盖率。
- 最新价格为可验证原始价格。
- 价格、净值、份额、规模、成交额的口径和时间可识别。
- 至少六个月日线；尽量提供两年周线。

ETF 上市不足所需历史时，可以引用跟踪指数历史作为代理，但必须标记代理关系；所有 ETF 价格候选仍必须转换为 ETF 当前原始价格。

ETF P0–P4B 已作为既有研究底座复用。P4A 提供：

- 全量或部分成分的权重标准化与重复代码合并。
- Top1/3/5/10、HHI 区间、有效成分数下界和已知权重覆盖率。
- 高度分散、中度分散、聚焦和集中型结构分类。
- 基于权重、涨跌贡献、盈利贡献、重大事件、证据冲突和研究过期的 0～5 只自适应选择。
- 边际解释增益低于 5 个百分点时停止，并保留强制选择例外。
- Selection ID、输入指纹、模块缓存、质量状态和零模型调用审计。

P4B 提供：

- 跨 ETF 共享的 `ComponentResearchDigest`，以及隔离 ETF 权重和选择原因的 `ETFComponentDigestBinding`。
- `reusable | partial_reusable | stale | missing | conflicted` 的确定性 Resolution。
- `analysis_as_of` 截止过滤、知识指纹、缓存、single-flight 和来源血缘。
- 对 P4A 已选且确需补充的成分进行有证据、有限额、有授权的受控生成，并通过统一知识记录回流 P4B1。

正式报告只能消费 P4A/P4B 的现有结果：

- 不重新排序、不扩大 P4A 的 0～5 只选择范围，也不让模型覆盖 Selection。
- 不把 `stale`、`missing` 或 `conflicted` 摘要包装成有效研究结论。
- 不因摘要缺失让整份 ETF 报告失败；对应内容以数据缺口进入 `passed_with_gaps`。
- P4A 合法选择为 0 时，`holding_penetration` 说明结构分散与停止原因，不视为数据缺失，也不强行选择成分。
- P4A Universe 为 partial/insufficient 时，保留已知权重覆盖率、Universe 质量和警告，不把前十大持仓冒充全量指数。

#### 3.3.1 `holding_penetration` 编译合同

服务端为每次 ETF 报告冻结一个穿透输入快照：

```text
selection_id
universe_snapshot_id
universe_quality_status
universe_weight_coverage
selected_components[]
  symbol
  security_name
  normalized_weight
  selection_reasons[]
  contribution_metrics[]
  digest_resolution_status
  digest_id
  binding_id
  knowledge_fingerprint
  digest_data_as_of
  source_claim_ids[]
  source_evidence_ids[]
  source_report_ids[]
```

章节确定性展示：

- 成分名称、代码、权重、选择原因，以及可用时的涨跌/盈利/重大事件贡献。
- `reusable`：展示受支持摘要、有效期和来源。
- `partial_reusable`：只展示已覆盖维度，并列出未覆盖维度和限制。
- `stale | missing | conflicted`：展示用户可理解的数据缺口、冲突或过期原因，不调用模型补写。
- P4B2 产生的 `component_research` 没有独立 Markdown/PDF Artifact 时，引用统一报告记录的稳定内部索引码，并继续穿透到其原始 Evidence；不得伪造文件或下载入口。

覆盖指标优先复用 P4A/P4B 已发布字段，报告编译器只做一致性校验：

```text
observed_weight_coverage
  = P4A Universe Snapshot 已知有效成分权重覆盖率

selected_weight_coverage
  = P4A Selection 中入选成分权重之和

explanation_coverage
  = P4A Selection 中各入选成分 marginal_explanation_gain 之和，上限为 1

research_coverage
  = reusable 或 partial_reusable 入选成分权重 / P4A 入选成分总权重

fully_supported_coverage
  = reusable 入选成分权重 / P4A 入选成分总权重
```

编译器不得重新定义 P4A 的覆盖口径。研究覆盖率分母为零时返回 `not_applicable`，禁止静默补零。`partial_reusable` 必须与完全支持覆盖率分开显示，不能通过合并比例掩盖研究缺口。

### 3.4 Index Profile

新增八章：

1. 核心结论。
2. 指数身份与编制规则。
3. 成分、行业和因子暴露。
4. 聚合估值与盈利周期。
5. 宏观与行业驱动。
6. 日线、周线量价结构。
7. 反方证据与逻辑失效条件。
8. 结构情景与跟踪框架。

指数不是直接交易资产：

- 不要求 ETF 份额、折溢价、持有人和跟踪误差。
- 所有监控候选默认 `watch_only`。
- 建议行动只能是观察或重新研究。
- 不输出买入、减仓、退出或仓位建议。

## 四、研究获取与分析升级

### 4.1 三轮检索补证

每个重大证据缺口按以下顺序处理：

1. 查询知识库中的有效原始资料和历史 Fact。
2. 刷新价格、股本、财报、ETF份额、指数成分等时间敏感数据。
3. 精确查找交易所、公告、基金公司、指数公司、政府和行业协会来源。
4. 尝试同一原始资料的 PDF、官方 API、公告镜像或缓存版本。
5. 查找两个独立二级来源。
6. 搜索相反结论、不同统计口径和冲突数据。
7. 对仍不足的重大 Claim 执行最后一轮定向补证。

默认预算：

- 目标研究时长 10 分钟，硬上限 15 分钟。
- 每个证据缺口最多三轮搜索。
- 每个重大 Claim 最多读取八份正文。
- 单一数据提供方最多重试两次。
- 10 分钟时仍有有效进展，可提示用户后延长至 15 分钟。
- 15 分钟后交付通过门控的部分报告，不无限等待。

### 4.2 Claim 支撑状态

使用现有 `source_class`、`independence_group`、`source_strength`、`scope_key`、时效和冲突记录，生成：

```text
verified
triangulated
conflicted
weak
unsupported
```

重大 Claim 的发布门槛：

- 一项与该事实领域匹配的权威原始来源；或者
- 两项发布关系独立、口径兼容的二级来源。

规则：

- `verified`、`triangulated` 可以进入核心结论。
- `conflicted` 必须并列展示口径，不能自动选取更有利数字。
- `weak` 只能作为待验证线索。
- `unsupported` 从正式结论删除。
- 搜索摘要不能直接支撑重大 Claim。
- 未识别来源默认 `unclassified`，不能默认认定为主流媒体。

### 4.3 财务与语义审计

公司报告：

- 所有确定性财务异常必须进入核查章节或记录排除理由。
- 勾稽结果必须进入报告摘要。
- 业绩预告不能写成已经确认的利润拐点。
- 低负债、高现金或净资产不能被推导为股价安全垫。
- 隐含预期模型不可用时禁止判断市场预期偏高或偏低。
- 不同产品、频率、应用和商业化阶段不能合并成一个结论。
- TAM 口径冲突必须并列展示。

ETF/指数报告：

- ETF份额变化不能自动归因于国家队或特定主体。
- 指数成分覆盖不足时不能把前十大成分冒充完整指数。
- 聚合估值必须记录权重覆盖率和缺失成分处理方式。
- 折溢价、跟踪误差、成交额和份额必须区分时间口径。
- 一次盘中异动不能升级为结构性趋势结论。
- 成分股内部报告只能作为复用索引，关键事实仍需穿透原始来源。

## 五、统一引用与参考资料

### 5.1 正文显示

Agent 继续在 Workspace 使用：

```text
[Fact:fact_id]
[Evidence:evidence_id]
```

编译器将其转换为一个统一的论文式来源编号：

- Web：右上角可点击 `[1]`。
- PDF：右上角小标 `[1]`。
- Markdown：`[^1]` 脚注。
- 同一句多个资料显示为 `[1–3]`。
- 同一 `document_ref` 在同一 revision 中只分配一个编号。

读者正文不得出现 Fact ID、Evidence ID、`〔数据1〕` 或 `〔来源1〕`。

### 5.2 ReportReference

```text
reference_id
citation_number
source_kind
title
publisher
author
published_at
retrieved_at
data_as_of
public_url
internal_report_id
internal_revision
internal_index_code
filename
document_ref
locators[]
evidence_ids[]
fact_ids[]
content_hash
```

`source_kind` 白名单：

```text
regulatory_filing
company_disclosure
fund_disclosure
index_document
web_page
api_dataset
uploaded_document
internal_report
```

### 5.3 报告末尾

先生成“数据依据”：

```text
指标 | 数值 | 期间/时点 | 口径 | 参考资料
```

只公开正文实际使用的重要 Fact；完整 Ledger 继续内部保存。

最后生成“参考资料”：

- 网页显示发布机构、标题、发布日期、访问时间和可点击原文。
- API 数据显示提供者、数据集/接口、数据时间和版本，不伪造网页链接。
- 内部报告显示报告名称、文件名、数据截止时间、revision、索引码和报告中心链接。

### 5.4 内部报告索引码

```text
VT-{品类}-{代码或组合}-{日期}-R{revision}-{短ID}
```

示例：

```text
VT-ETF-588870-20260718-R01-6A0499
VT-EQ-603738-20260716-R14-A9C65F
VT-IDX-000688-20260718-R01-42B711
```

索引码要求：

- 创建后永久不变。
- 每个 revision 唯一。
- 报告中心搜索框可直接解析。
- Artifact 归档后仍保留墓碑记录。
- 导出文件离开系统后仍可凭索引码查找。

### 5.5 references.json v2

```text
schema_version
content_sha256
citations[]
claim_citation_map
fact_citation_map
evidence_citation_map
internal_report_links[]
broken_links[]
```

必须绑定最终 Markdown SHA-256。引用编号只用于读者展示；监控 Bundle 使用稳定 Claim/Fact/Evidence/Reference ID。

## 六、统一阅读模型与报告产物

### 6.1 ReportViewModel

```text
identity
revision
instrument_type
report_profile
data_as_of
publication_state
quality_status
coverage_by_domain[]
decision_summary
key_metrics[]
visuals[]
sections[]
claims[]
gaps[]
references[]
viewpoints[]
monitoring_summary
methodology
```

约束：

- ReportViewModel 只负责阅读，不复制知识库事实或报告目录版本逻辑。
- Web、Markdown、PDF 的关键数字、Claim、引用和状态必须一致。
- 图表数据只能来自 Fact Ledger。
- Markdown 使用数据表作为图表的可访问替代。

### 6.2 正式产物

正式通过：

```text
report.md
report.pdf
monitoring_bundle.json
revision_diff.md（存在父版本时）
```

内部审计产物：

```text
report_view.json
references.json
numeric_audit.json
semantic_audit.json
validation.json
claims.jsonl
```

质量失败：

```text
diagnostic.md
rejected_draft.md（仅内部）
validation.json
```

诊断报告不生成正式 PDF 或监控 Bundle。

### 6.3 Web 阅读

报告中心和聊天完成卡共享一个结构化详情组件，首屏显示：

- 证券名称、代码、资产类型、索引码、revision 和数据日期。
- 完整、部分完整或仅诊断。
- 财务/产品、市场、行业、预期等覆盖状态。
- 核心判断、支持条件、失效条件和三个后续验证指标。
- 关键指标和两张核心图表。
- 监控可用性、候选数和复核日期。

正文引用支持：

- 点击小标滚动到参考资料。
- 悬浮显示发布者、标题和日期。
- 网页新标签打开。
- 内部报告在报告中心打开指定 revision。

## 七、MonitoringBundle 公共合同

### 7.1 顶层结构

```json
{
  "schema_version": 1,
  "bundle_id": "monbundle_xxx",
  "report_id": "report_xxx",
  "report_revision": 1,
  "source_report_sha256": "sha256",
  "symbol": "588870.SH",
  "instrument_type": "etf",
  "report_profile": "etf_deep_research",
  "horizon": "structural",
  "generated_at": "2026-07-18T15:20:00+08:00",
  "data_as_of": "2026-07-18T15:00:00+08:00",
  "valid_from": "2026-07-18T15:20:00+08:00",
  "valid_until": "2026-10-16T15:00:00+08:00",
  "review_due_at": "2026-08-17T15:00:00+08:00",
  "price_basis": {
    "adjustment": "raw",
    "currency": "CNY",
    "tick_size": 0.001,
    "source_fact_ids": []
  },
  "report_quality_status": "passed_with_gaps",
  "monitoring_status": "available",
  "status_reason_codes": [],
  "structural_context": {},
  "candidates": [],
  "integrity": {
    "compiler_version": "monitoring-bundle-v1",
    "references_sha256": "sha256"
  }
}
```

`monitoring_status`：

```text
available
not_recommended
data_insufficient
```

- `available`：存在可供监控系统使用或复核的候选。
- `not_recommended`：数据充足，但没有稳定且值得监控的候选。
- `data_insufficient`：缺少原始价格、长周期行情或必要证据。

### 7.2 StructuralContext

机器字段使用英文稳定枚举，前端显示中文：

```text
trend_stage: declining | range | basing | advancing
trend_direction: up | down | sideways
trend_strength: strong | medium | weak
thesis_state: intact | weakening | invalidated
structural_levels[]
thesis_invalidation_conditions[]
review_triggers[]
```

ETF 的 `review_triggers` 还应支持以下非价格事件：

- 跟踪指数调仓、编制规则变化或 Universe Snapshot 发生实质变化。
- P4A Selection、选择原因或解释覆盖率发生变化。
- 入选成分的 P4B Resolution 在 `reusable/partial_reusable/stale/missing/conflicted` 之间迁移。
- 关键成分出现重大公告、盈利预期变化或足以推翻当前穿透结论的新证据。

这些事件只触发复核或重新研究，不直接产生交易动作。

### 7.3 MonitoringCandidate

```text
scenario_id
scenario_version
scenario_fingerprint
candidate_type: price_level | event_trigger
intent
label
level
approach_condition
trigger_condition
confirmation_conditions[]
volume_condition
invalidation_conditions[]
observation_window
suggested_action
rationale
source_text
claim_ids[]
fact_ids[]
evidence_ids[]
reference_ids[]
machine_expressible
automation_status: action_ready | watch_only
blocked_reasons[]
```

深度报告 `intent` 白名单：

```text
structural_invalidation
major_support
major_resistance
breakout_confirmation
trend_recovery
research_review
```

规则：

- 深度报告一般输出 2～6 个，合法范围为 0～6 个。
- `scenario_id` 由 `symbol + horizon + semantic_slot` 生成，不包含价格。
- 点位变化增加 `scenario_version` 并更新 fingerprint，ID 保持稳定。
- 所有价格使用原始不复权口径。
- 结构研究可以使用复权序列分析收益，但输出点位必须通过确定性调整因子转换为原始价格。
- 无法重放转换时设为 `watch_only`；无法确认原始价格时 Bundle 为 `data_insufficient` 且候选为空。
- 量能默认为分类条件，不得压制已经发生的价格事实。
- 候选必须引用 Claim、Fact、Evidence 和 Reference。

### 7.4 有效期默认值

| Horizon | 默认有效期 | 默认复核时间 |
|---|---:|---:|
| daily | 下一交易日收盘 | 下一交易日前 |
| weekly | 14天 | 7天 |
| structural | 90天 | 30天 |

强制规则：

- `valid_from <= review_due_at <= valid_until`。
- 候选有效期不得超过 Bundle。
- 来源过期、指数调仓、分红、拆分、ETF份额折算或价格口径变化时提前复核。
- 到期候选不得继续触发。

### 7.5 组合报告

组合主报告仍生成 Bundle，但只作为容器：

```text
instrument_type=portfolio
symbol=null
monitoring_status=not_recommended
candidates=[]
child_bundle_refs[]
```

可监控点位只存在于对应持仓子报告，禁止在组合主报告混合多个证券点位。

## 八、报告与监控系统集成

### 8.1 新报告路径

监控规划器按以下顺序处理：

1. 从统一报告目录读取 `monitoring_bundle` Artifact。
2. 校验 Bundle schema、报告哈希、引用哈希和有效期。
3. 获取当前可验证原始价格和最新行情。
4. 检查币种、tick size 和价格口径。
5. 将结构候选确定性映射成监控计划草案。
6. 默认保存为 `pending_review`。

新报告不得再次让模型阅读整篇 Markdown 猜测点位。

### 8.2 旧报告兼容

- 没有 Bundle 的旧报告继续使用现有 Markdown 抽取路径。
- 标记 `source_mode=legacy_markdown_extraction`。
- 模型 JSON 修复最多一次。
- 允许返回零场景，不再把空场景视为错误。
- 旧路径候选默认 `watch_only`，除非完成全部条件映射和价格门控。

### 8.3 权限与激活边界

- `action_ready` 仅表示可以无歧义映射，不等于已激活。
- 报告发布不能调用监控激活接口。
- 报告刷新不能修改已有活动计划。
- 手动导入只创建 `pending_review`。
- 只有监控子系统在用户明确启用自主模式后，才能通过独立门控激活。
- 自动交易始终为 `forbidden`。

## 九、API、目录与状态

### 9.1 Deep Report API

```text
GET  /reports/{report_id}?include_view=true&include_references=true
GET  /reports/{report_id}/artifacts/markdown
GET  /reports/{report_id}/artifacts/pdf
GET  /reports/{report_id}/artifacts/monitoring_bundle
GET  /reports/{report_id}/artifacts/diff
POST /reports/{report_id}/refresh
POST /reports/{report_id}/revisions
```

证据补充 revision：

```json
{
  "revision_mode": "evidence_refresh",
  "gap_ids": ["industry_share", "tam_scope"]
}
```

### 9.2 报告目录

`ReportEnvelope` 增加：

```text
report_profile
instrument_type
citation_code
monitoring_status
monitoring_candidate_count
monitoring_review_due_at
```

新增：

```text
GET /report-library/references/{citation_code}
```

报告中心支持按公司、ETF、指数、监控可用性和复核日期筛选。

### 9.3 Schema 迁移

知识层事实表保持不变。报告目录迁移到 schema v3：

- 迁移前使用 SQLite Backup API 备份。
- 只为新报告原生写入新增字段。
- 旧目录记录将 `instrument_type/report_profile` 标为 `unknown`，不反向重写历史 Artifact。
- 目录开关关闭后，现有 Deep Report、日报、监控和飞书行为保持兼容。

### 9.4 状态分离

```text
Report.status:
  running | completed | failed | cancelled

Report.quality_status:
  passed | passed_with_gaps | failed_validation

Report.publication_state:
  complete | partial | diagnostic

MonitoringBundle.monitoring_status:
  available | not_recommended | data_insufficient
```

程序异常才进入技术失败。证据缺口、空候选和监控不可用不能显示为通用系统异常。

## 十、真实动态进度

后端只在实际动作发生时发送：

```text
coverage.planned
source.search_started
source.search_completed
source.opened
source.rejected
fact.extracted
conflict.detected
claim.support_updated
module.completed
report.compiling
report.auditing
report.rendering_pdf
report.publishing
```

事件包含：

```text
attempt_id
report_id
stage
completed
total
source_counts
gap_counts
elapsed_seconds
estimated_remaining_seconds
user_message
```

前端行为：

- 创建后 1 秒内进入动态运行态。
- 每 10 秒至少发送一次心跳。
- 只处理当前 `attempt_id` 的事件。
- 10 分钟需要延长时明确提示用户。
- 支持取消、断线重连和恢复。
- 不展示内部提示词、模型思维、工具名或工程错误码。

## 十一、实施阶段

### R0：基线冻结与迁移门禁

- [x] 冻结 ReportReference、MonitoringBundle 和公司/ETF InstrumentResolver 首版合同；ReportViewModel 仍待统一。
- [x] 将既有 `ETFComponentSelection`、`ComponentDigestResolution` 和 component research 内部报告记录冻结为只读输入合同。
- [ ] 记录当前知识库、报告目录和监控库基线。
- [ ] 建立报告目录 schema v3 迁移备份和回退测试。
- [ ] 增加 Profile/Knowledge/Report Library 配置依赖健康检查。

完成标准：关闭新开关时现有公司报告、ETF P0–P4B、日报、监控和飞书行为不变；相同输入的 Selection、Resolution、Digest 和内部报告 ID 保持稳定，本轮报告编译不产生额外成分研究模型调用。

### R1：资产路由与 ETF P4 正式报告桥接

- [x] 接入公司/ETF InstrumentResolver；精确 A 股 ETF 代码即使普通股票搜索无结果也可识别。
- [x] 公司和 ETF Profile 改为由 instrument type 强制选择，服务端会纠正带市场代码的错误 Profile。
- [ ] 新增 Index Profile 及其硬门控。
- [x] 在 ETF 分析状态中冻结 P4A Selection 与 P4B Resolution 快照和指纹。
- [x] 实现 `holding_penetration` 确定性 ViewModel、覆盖率和五种 Resolution 的用户化展示。
- [x] 将选择原因、贡献指标、Digest 摘要、限制和来源编译进正式 ETF 报告。
- [x] 对 0 选择、partial Universe、缺失/过期/冲突 Digest 实现局部降级，不触发隐式 P4B 生成。
- [x] DeepReportRecord/ReportEnvelope 原生保存 profile、instrument type 和内部索引码。

完成标准：ETF 不触发公司财务门控；指数不触发 ETF 产品门控；类型冲突 fail closed；ETF 正式报告可完整解释 P4A/P4B 的实际输出，摘要缺失只形成明确缺口，不再阻断可发布报告。

### R2：统一引用层

- [x] 实现 Fact -> Evidence -> Document 传递引用闭包。
- [x] 实现论文式小标和同文档去重。
- [x] 生成数据依据和参考资料。
- [x] 实现内部报告索引码、解析 API 和报告中心跳转。
- [x] 对无 Artifact 的 `component_research` 记录生成可解析内部引用，并保留其物化后的原始 Evidence 血缘。
- [x] 升级 references.json schema v2 并绑定最终 Markdown 哈希。

完成标准：正文无内部 ID；网页来源可点击；内部报告可凭索引码打开；Web/Markdown/PDF 编号一致。

### R3：检索与 Claim 审计

- [ ] CoveragePlan 驱动三轮补证和 10～15 分钟预算。
- [x] 实现确定性 ClaimSupportEvaluator 首版，按权威来源、独立来源数、冲突与缺口分类为 verified/triangulated/weak/conflicted/insufficient。
- [ ] 接入来源独立性、范围、时效、冲突和反方搜索。
- [ ] 接入公司财务、ETF/指数聚合和语义门控。
- [x] ETF 持仓穿透只读取已冻结的 P4A Selection 与 P4B Resolution，不在报告流水线内生成成分摘要。
- [ ] 校验 Digest 中的重大事实仍可穿透到原始 Claim/Evidence，不允许“内部报告引用内部报告”后丢失来源。
- [x] 将 ETF 成分摘要缺失等软缺口从整份报告失败中分离。

完成标准：重大 Claim 均有合格来源或明确降级；搜索摘要不支撑核心结论。

### R4：MonitoringBundle 生产

- [x] 新增 Workspace 结构化 Bundle 提交命令。
- [x] 编译器总是为正式报告生成合法空或非空 Bundle。
- [x] 实现 StructuralContext、稳定候选 ID、原始 CNY 价格、tick size、有效期和血缘校验。
- [ ] 将监控摘要确定性渲染进对应报告章节。
- [x] 报告质量和监控状态独立审计。

完成标准：0～6 个候选均合法；所有价格可重放为原始价格；Bundle 与报告/引用哈希绑定。

### R5：统一阅读与三种产物

- [ ] Web 报告改为结构化阅读组件。
- [ ] 增加公司、ETF、指数各自关键图表。
- [x] ETF 报告目录卡展示选择权重覆盖、研究覆盖、完全支持覆盖、选择数量和数据截止时间；完整详情图表仍待补充。
- [ ] Markdown/PDF 由 ReportViewModel 编译。
- [ ] PDF 增加封面摘要、页码、图表、监控摘要和参考资料。
- [x] 正式 Markdown 与 `monitoring_bundle.json` 原子持久化，PDF 延续现有按需物化；统一 ReportViewModel 原子发布仍待完成。

完成标准：Web、Markdown、PDF 数字、Claim、引用和监控候选一致；不在本阶段新增 `weekly_review` 生产器。

### R6：监控接入与前端

- [ ] 新报告优先读取 Bundle，旧报告保留 Markdown 兼容。
- [ ] 允许旧路径和新路径返回零候选。
- [ ] Bundle 导入只生成 pending review。
- [x] 报告中心显示监控状态、候选数、复核日期和结构依据，并支持预览/下载 JSON。
- [ ] 提供“生成监控草案”，不提供报告侧直接激活。

完成标准：action_ready 不自动激活；watch_only 不能产生越权行动；自动交易保持禁止。

### R7：受控发布与真实观察

- [ ] 影子生成 v3 产物，不改变当前用户交付。
- [ ] 对公司、ETF、指数分别完成真实报告。
- [ ] 对一份无候选报告验证正常发布。
- [ ] 对价格口径不匹配报告验证 data_insufficient。
- [ ] 验证刷新和章节修订不修改历史 Artifact。
- [ ] 观察 ETF 报告对 P4A/P4B 结果的只读消费，不因生成报告扩大成分研究范围或重复产生模型调用。
- [ ] Web 稳定后再评估飞书入口；不接入每日自动任务。

## 十二、测试计划

### 12.1 合同与迁移

- company_equity、ETF、index 唯一路由和冲突失败。
- Report catalog schema v2 -> v3 备份、迁移、重复迁移和关闭开关。
- 历史 schema v2 Deep Report 仍可读取。
- 新 revision 使用 v3，并保持父版本不可变。
- P4A 相同输入得到稳定 Selection ID、第二次命中模块缓存且模型调用为零。
- P4A partial/insufficient 结果进入报告覆盖状态，不被模型补成完整成分宇宙。
- P4B 相同输入得到稳定 Resolution/Digest/Binding；报告编译只读消费，不新建 Generation Job。

### 12.2 ETF P4 正式报告桥接

- 588870 固定夹具为“P4A 入选五只、P4B 全部 missing”时，报告为 `passed_with_gaps`，章节展示选择理由和缺口，模型调用数为 0。
- 560010 固定夹具为 P4A 合法选择 0 时，报告仍可通过，研究覆盖率为 `not_applicable`，不强行选择成分。
- `reusable` Digest 正确展示摘要、有效期、内部索引和原始来源。
- `partial_reusable` 只展示已覆盖维度，研究覆盖和完全支持覆盖分别计算。
- `stale/missing/conflicted` 不被升级为有效结论，且不自动触发 P4B 生成。
- Universe partial/insufficient 时，覆盖率和来源警告进入正文，不把前十大持仓冒充全量成分。
- component research 没有 Markdown/PDF Artifact 时，界面不显示伪下载按钮，但内部索引仍可定位统一报告记录。
- P4B 开关关闭、预算耗尽或没有新增生成授权时，正式 ETF 报告仍按现有 Resolution 正常发布或局部降级。
- 同一 Selection/Resolution 编译 Web、Markdown、PDF 后，选择数量、权重、状态、覆盖率和来源完全一致。

### 12.3 引用

- Fact 自动穿透到原始 Evidence/Document。
- 同一网页多条 Evidence 只生成一个编号。
- 多来源 Claim 生成紧凑引用区间。
- 网页、API、上传文件和内部报告分别正确渲染。
- 内部索引码唯一、可搜索、可解析、归档后有墓碑。
- 引用缺失、孤立编号、错误链接和哈希不一致被发现。
- 表格、图表和普通段落引用一致。

### 12.4 报告分析

- 权威原始来源或两个独立二级来源门槛。
- 转载内容不会误算为独立来源。
- TAM、指数估值、权重覆盖和份额口径冲突并列保留。
- 公司财务异常全部进入报告或有排除理由。
- ETF份额变化不自动归因特定主体。
- 业绩预告不升级为确认事实。
- 隐含预期模块不可用时不输出估值方向。

### 12.5 MonitoringBundle

- `candidates=[]` 在 not_recommended/data_insufficient 下合法。
- 结构报告最多六个候选。
- scenario ID 跨 revision 稳定，版本和 fingerprint 正确变化。
- 原始价格、tick size、币种和时区校验。
- 复权点位转换可以重放；无法转换时降级。
- `valid_from <= review_due_at <= valid_until`。
- Claim/Fact/Evidence/Reference 血缘完整。
- Bundle 与最终报告和 references 哈希一致。
- Bundle 失败不会把已通过研究内容错误标为技术失败。

### 12.6 监控集成

- 新报告不再调用 Markdown LLM 抽取。
- 旧报告仍可走兼容路径。
- 空候选不产生 planner error。
- 导入生成 pending review，不自动激活。
- action_ready、watch_only 和 blocked reason 正确映射。
- 当前价格或口径变化后拒绝过期候选。
- 明确启用自主模式前不得激活。

### 12.7 前端和产物

- 创建后立即进入真实动态进度。
- SSE 断开后恢复当前 attempt。
- 报告详情正确区分公司、ETF、指数。
- 小标跳转、来源悬浮、网页链接和内部报告打开。
- Web/Markdown/PDF/Bundle 使用相同事实和引用。
- PDF 页码、中文字体、表格、图表和参考资料无截断。
- 诊断、部分完整和完整状态使用用户语言。

## 十三、真实验收样本

1. 财务和一致预期完整的盈利 A 股公司。
2. 缺少一致预期但财务完整的公司。
3. 588870.SH ETF。
4. 沪深300类宽基 ETF。
5. 集中型行业 ETF。
6. 一个主要指数。
7. 一份引用内部个股报告的 ETF 成分穿透报告。
8. 一份 P4A 有选择但 P4B 摘要全部缺失、仍可正式发布的 ETF 报告。
9. 一份 P4A 合法选择为 0 的高度分散 ETF 报告。
10. 一份数据充分但没有稳定点位的正式报告。
11. 一份原始价格口径不足的报告。
12. 一份来源数值或统计范围冲突的报告。

泰晶科技既有报告继续作为语义回归样本，重点检查：

- 单一商业研究来源不能支撑重大市场份额结论。
- TAM 口径冲突不能被合并成单一结论。
- 财务异常触发器必须进入正文。
- 估值不可判断时不能声称市场已前置定价。
- 预告利润不能写成确认拐点。

## 十四、运行指标

至少记录：

- 创建任务到首个进度事件时间。
- 报告总耗时、搜索轮数、读取正文数和数据源失败数。
- 历史 Fact 复用数、新增数、更新数和冲突数。
- verified/triangulated/weak/conflicted Claim 数量。
- passed、passed_with_gaps、failed_validation 比例。
- 正式报告三产物完整率。
- monitoring available/not_recommended/data_insufficient 比例。
- action_ready/watch_only 候选数。
- Bundle 导入、草案生成、人工批准和拒绝数量。
- Markdown 兼容抽取调用次数，目标随 v3 报告增长逐步下降。
- 报告中心打开、PDF下载、来源点击和内部索引解析次数。

## 十五、最终 Definition of Done

- 公司、ETF、指数都使用正确 Profile，不交叉套用硬门控。
- 每份正式报告都有 Markdown、PDF 和合法 MonitoringBundle。
- 正文只显示轻量论文式引用，末尾有数据依据和参考资料。
- 网页资料可以直接打开，内部报告可以凭稳定索引码定位。
- 所有重大数字可从 Fact Ledger 重放并追溯原始资料。
- 所有重大 Claim 满足来源门槛或明确降级。
- ETF `holding_penetration` 只消费 P4A/P4B，能解释 Selection、Resolution、覆盖率、缺口和来源，不重复生成成分研究。
- P4B 能力完成不被误写成研究覆盖率 100%；覆盖结论始终来自本次冻结的 Resolution。
- P4A 选择为 0、P4B 摘要缺失或过期均可局部降级，不因格式要求编造成分结论。
- 软证据缺口不会让可用报告无故失败。
- `candidates=[]` 不构成错误。
- 所有价格候选均为可验证的原始价格。
- 报告发布不会创建、修改或激活监控计划。
- action_ready 只表示机器可表达，不表示已经执行。
- 新报告监控路径不再让模型重新读取整篇 Markdown。
- Web、Markdown、PDF、references.json 和 monitoring_bundle.json 相互一致且哈希绑定。
- 旧报告、旧 API、日报、监控、飞书和历史 Artifact 保持兼容。
