# P4B1 Component Research Digest 复用底座实施 Prompt

## 任务名称

建立跨 ETF 共享的 `ComponentResearchDigest` 复用底座，并接入 P4A、知识库、统一报告目录与标的档案。

## 项目路径

`C:\Users\23479\Documents\GitHub\Vibe-Trading`

## 一、任务目标

在 P4A 已完成关键成分确定性选择的基础上，实现 P4B1：成分研究摘要的确定性发现、引用、质量判断、缓存和跨 ETF 复用。

P4B1 只回答以下问题：

1. P4A 入选的成分是否已经存在可复用的个股研究？
2. 可复用研究引用了哪些 Report、Claim、Fact 和 Evidence？
3. 这些研究是否完整、新鲜、过期、缺失或存在证据冲突？
4. 同一成分被多只 ETF 选中时，能否共享一份研究摘要？
5. 它为什么会被某一只 ETF 选中？
6. 能否将结果提供给 ETF 报告和标的档案，而不重复生成正文？

P4B1 **不得调用模型，不得生成新的个股研究正文，不得创建正式 ETF 报告 Artifact**。所有模型调用数、输入 Token 和输出 Token 必须保持为 0。

P4B2“只对缺失或过期成分调用模型生成摘要”不在本任务范围内。

## 二、当前基础与前置条件

开始实施前必须检查当前工作区和真实代码，不能只依赖本文件中的路径假设。

现有基础包括：

- `agent/src/reports/contracts.py`
  - ETF Snapshot、P4A 选择结果及报告数据合同。
- `agent/src/reports/etf_research.py`
  - ETF Snapshot、模块缓存、single-flight、变化路由和审计指标。
- `agent/src/reports/etf_penetration.py`
  - `ETFComponentSelector`、`execute_p4a_selection`。
- `agent/src/reports/catalog.py`
  - 统一报告目录、标的档案、报告观点和报告比较。
- `agent/src/research/knowledge.py`
  - Fact、Claim、Evidence、冲突和知识关系。
- `agent/src/reports/service.py`
  - Deep Report 生命周期和 ETF 分析模块挂接。
- `agent/src/api/report_library_routes.py`
  - 统一报告目录和标的档案 API。

依赖边界：

- ETF 自动采集层可能由另一任务实现。
- P4B1 必须能使用测试 Snapshot 独立完成测试。
- 当真实 `ETFUniverseSnapshot` 和 P4A Selection 可用后，P4B1 应无需改动核心合同即可接入。
- 如果采集层任务仍在同一工作区修改 `contracts.py`、`etf_research.py`、`service.py` 或 `__init__.py`，不要并行覆盖其改动。先检查 dirty worktree，再决定合并顺序。

## 三、核心设计原则

### 3.1 公司研究全局共享，ETF 关系单独保存

必须区分两类对象：

1. `ComponentResearchDigest`
   - 描述某一个上市证券当前可复用的研究内容和质量。
   - 以规范化证券代码为共享键。
   - 不属于某一只 ETF。
   - 多只 ETF 可以引用同一个 Digest。

2. `ETFComponentDigestBinding`
   - 描述某个成分为什么被某一只 ETF 的某次 P4A Selection 选中。
   - 保存 ETF 专属的权重、评分、贡献和选择原因。
   - 可以引用一个 Digest，也可以标记为 missing、stale 或 conflicted。

不得把 ETF 权重和选择理由写入全局公司摘要，否则同一公司在不同 ETF 中无法正确复用。

### 3.2 只引用知识，不复制第二套知识库

Digest 应优先保存：

- `source_report_ids`
- `claim_ids`
- `fact_ids`
- `evidence_ids`
- `conflict_ids`
- 研究维度覆盖情况
- 数据截止时间和有效期
- 确定性选择出的展示摘要引用

禁止把完整报告正文、完整 Claim 文本或完整 Fact 内容复制进新的数据库表。

如果前端需要展示文本，应在读取时从知识库解析受控长度的 Claim excerpt，或者保存可以由知识指纹失效的短期物化缓存。Canonical Digest 仍以 ID 引用为准。

### 3.3 不允许名称匹配

- 成分与报告必须使用规范化证券代码匹配。
- A 股使用带市场后缀代码，例如 `688256.SH`、`300750.SZ`。
- 港股使用项目现有的规范化代码格式。
- 不允许仅用“寒武纪”“贵州茅台”等名称关联研究。
- 同一公司 A/H 双重上市首版不得自动合并。
- 首版以规范化上市证券代码共享；可以预留 `entity_id`，但没有可靠映射时保持为空。

### 3.4 防止未来数据泄漏

构建 Digest 必须接收 `analysis_as_of`。

- 只允许引用 `data_as_of <= analysis_as_of` 的报告、Claim、Fact 和 Evidence。
- 历史 Snapshot 不能引用后来生成的研究。
- 当前分析可以使用当前截止时间之前的最新有效研究。
- 测试中必须覆盖未来 Claim 被排除的情形。

## 四、数据合同

在遵循项目现有 dataclass、序列化和命名风格的前提下，新增或等价实现以下合同。

### 4.1 ComponentResearchDigest

建议字段：

```text
digest_id
schema_version
component_symbol
security_name
entity_id                         # 可空，首版不强制跨市场实体合并
analysis_as_of
research_data_as_of
created_at
freshness_expires_at
status                            # reusable / partial_reusable / stale / missing / conflicted
quality                           # complete / partial / insufficient
coverage_dimensions
missing_dimensions
stale_dimensions
source_report_ids
claim_ids_by_dimension
fact_ids
evidence_ids
conflict_ids
knowledge_fingerprint
input_fingerprint
warnings
model_id                          # 固定为 deterministic
model_calls                       # 固定为 0
input_tokens                      # 固定为 0
output_tokens                     # 固定为 0
```

`digest_id` 必须由稳定语义输入产生。相同证券、相同 `analysis_as_of` 口径、相同有效知识集合、相同规则版本应得到相同 ID。

### 4.2 ETFComponentDigestBinding

建议字段：

```text
binding_id
etf_symbol
selection_id
component_symbol
digest_id                         # missing 时可空
digest_status
component_weight
selection_score
marginal_explanation_gain
forced
selection_reasons
price_contribution
earnings_contribution
selected_rank
selection_data_as_of
created_at
warnings
```

`binding_id` 必须包含 ETF、Selection 和成分代码。同一公司在不同 ETF 中应产生不同 Binding，但可引用相同 Digest。

### 4.3 ComponentDigestResolution

一次 P4A Selection 的解析结果建议包含：

```text
resolution_id
etf_symbol
selection_id
analysis_as_of
selected_count
reusable_count
partial_reusable_count
stale_count
missing_count
conflicted_count
bindings
digest_ids
reuse_ratio
estimated_avoided_model_calls
warnings
model_calls = 0
input_tokens = 0
output_tokens = 0
```

## 五、研究维度与确定性归类

首版至少识别以下研究维度：

- `business_exposure`：主营业务、行业及与 ETF 主题的关系。
- `earnings_trend`：最近有效报告期的收入、利润或经营趋势。
- `valuation`：有明确截止时间的估值判断。
- `catalysts`：产品、产业、政策或经营催化。
- `risks`：主要风险和反向证据。
- `holder_governance`：重要股东、机构持仓或治理变化。
- `material_events`：近期重大公告和事件。

必须先检查现有 Claim 的 `section_id`、标签、来源关系和报告种类，再建立确定性映射表。不要通过模型分类。

允许使用：

- 明确的 `section_id` 映射。
- 报告类型和报告周期。
- 已存在的 Claim 类型或结构化标签。
- 有限且可测试的关键词规则作为降级方案。

关键词规则必须可审计、可测试，并保留命中原因；不能形成不可解释的大型启发式分类器。

## 六、可复用状态判定

### reusable

- 存在有效研究。
- 核心维度达到首版最低覆盖要求。
- 没有未解决的高严重度冲突。
- 核心维度未过期。

### partial_reusable

- 存在部分新鲜研究，可在 ETF 报告中复用。
- 但存在非核心维度缺口，或只有 `passed_with_gaps` 来源。
- 必须明确列出缺失维度。

### stale

- 存在历史研究，但核心维度超过有效期或满足既有失效条件。
- 不得把 stale 摘要标记为可直接用于当前结论。

### missing

- 没有符合代码、截止时间、质量和状态要求的研究。
- P4B1 只记录缺失，不调用模型补齐。

### conflicted

- 存在未解决的关键 Claim/Fact 冲突，足以影响 ETF 解释。
- 必须保留冲突 ID 和相反观点来源。
- P4B1 不自行裁决冲突。

## 七、新鲜度规则

首版阈值必须集中配置，不得散落在代码中。可以以以下默认值开始，但实施者必须结合现有报告有效期合同校准：

- 主营与行业暴露：90 天。
- 盈利趋势：最近一个有效财报期，或 120 天硬上限。
- 估值：30 天；如果依赖当前价格，必须同时满足行情验证要求。
- 催化与重大事件：7 至 30 天，取决于事件有效期。
- 股东与治理：最近一个正式披露期。
- 风险：90 天，或者直到 Claim 失效条件满足。

优先使用现有 Claim 的 `valid_until`、失效条件和报告有效期。只有缺少显式有效期时才使用默认阈值。

价格敏感内容不得复用未验证行情。P4B1 不负责刷新行情；条件不满足时将估值或价格维度标记为 stale/insufficient。

## 八、知识选择规则

对一个成分构建 Digest 时：

1. 规范化证券代码。
2. 查询同一 `subject_key` 的已发布报告。
3. 排除 `failed_validation`、不可用或晚于 `analysis_as_of` 的报告。
4. 获取报告关联的 Claim、Fact、Evidence 和冲突。
5. 按研究维度归类。
6. 同一 Claim ID 去重。
7. 优先使用数据截止时间更新、质量更高、覆盖完整的来源。
8. 结构性内容优先参考有效的 structural 报告。
9. 近期事件可以参考 daily/weekly 报告，但不得覆盖仍有效的结构性事实。
10. 同一维度存在相反有效 Claim 时标记冲突，不能简单选择最新一条掩盖分歧。

必须输出“为什么选中这些 Claim”的确定性审计信息。

## 九、存储与复用

优先使用现有研究缓存数据库：

`~/.vibe-trading/cache/research_cache.sqlite3`

可以在现有 `ETFResearchStore` 中扩展，或新增职责清晰的 `ComponentResearchDigestStore`。不得新建另一套孤立知识数据库。

建议表：

- `component_research_digests`
- `etf_component_digest_bindings`
- `component_digest_resolutions`

要求：

- migration/初始化必须幂等。
- 使用稳定主键和必要索引。
- 相同输入重复执行不得创建重复记录。
- 同一成分并发构建使用 single-flight。
- 新增有效 Claim、Claim 失效、研究过期或冲突变化时，知识指纹必须变化。
- 多只 ETF 同时选择同一成分时，Digest 只构建一次。
- 保存 cache hit、reuse、stale、missing、conflicted 和 avoided model call 指标。

## 十、P4A 接入

新增确定性服务，名称可根据项目风格调整，例如：

```text
ComponentResearchDigestService.resolve_selection(
    selection,
    analysis_as_of,
)
```

输入：

- `ETFComponentSelection`
- Selection 对应 Universe Snapshot 的 `data_as_of`
- 当前允许使用的研究截止时间

输出：

- `ComponentDigestResolution`
- 每个成分的 Binding 和 Digest 状态

行为要求：

- P4A 选择 0 只时，直接返回空解析结果。
- 只处理 P4A 入选成分，禁止扩展到全部指数成分。
- 不改变 P4A 的成分选择结果。
- 不因 missing/stale 自动调用模型。
- 结果接入 `holding_penetration` 的依赖指纹或单独的确定性子模块指纹。
- 相同 Selection 和相同知识指纹第二次执行必须命中缓存。

## 十一、Deep Report 与标的档案接入

### 11.1 Deep Report

P4B1 只把结构化解析结果挂接到 ETF Deep Report 分析状态，不生成报告正文。

至少保存：

- `resolution_id`
- `selection_id`
- Digest 状态统计
- `digest_ids`
- `binding_ids`
- 复用率
- 缺失、过期和冲突成分
- avoided model calls
- `model_calls=0`
- `tokens=0`

挂接操作不得创建 `report.md`、PDF 或正式 Artifact。

### 11.2 ETF 标的档案

为现有标的档案 Profile 提供结构化输出：

```text
profile.etf.component_research = {
  selection_id,
  resolution_id,
  selected_count,
  reusable_count,
  stale_count,
  missing_count,
  conflicted_count,
  reuse_ratio,
  components: [
    {
      symbol,
      name,
      weight,
      forced,
      selection_reasons,
      digest_id,
      digest_status,
      coverage_dimensions,
      research_data_as_of,
      freshness_expires_at,
      warnings
    }
  ]
}
```

P4B1 至少完成后端合同和 API 输出。若标的档案前端尚未实施，应增加前端可消费的稳定类型和测试夹具，但不要在本任务中扩大为完整页面重构。

个股标的档案可以显示该股票当前 Digest 的研究覆盖和新鲜度，但不得因为它被某只 ETF 选中而修改个股的公共事实。

## 十二、API

遵循现有 API 命名和鉴权方式，提供最小可观测入口。可以包括：

- 查询某个证券的当前 Component Digest。
- 查询某次 ETF Selection 的 Digest Resolution。
- 确定性重新解析某次 Selection。
- 在标的档案 API 中返回 ETF Component Research 摘要状态。

“重新解析”只允许重新读取现有知识和更新确定性缓存，不得调用模型或发起新研究。

不得新建第二套报告中心入口。

## 十三、Token 与调用预算

P4B1 硬性要求：

- `model_calls = 0`
- `input_tokens = 0`
- `output_tokens = 0`
- 不启动 Deep Research Agent。
- 不调用外部 LLM。
- 不为 missing 成分生成文字摘要。

允许计算：

- `estimated_avoided_model_calls`
- `estimated_avoided_input_tokens`
- `estimated_avoided_output_tokens`

这些节省量必须标记为估算值，并保存计算口径。

## 十四、首批验证标的

使用测试 Selection 和现有知识库审计以下代表性成分。没有现有研究时应正确返回 missing，而不是补造内容。

### 588870.SH 科创50 ETF

- `688256.SH` 寒武纪
- `688041.SH` 海光信息
- `688981.SH` 中芯国际
- `688008.SH` 澜起科技
- `688012.SH` 中微公司

### 510300.SH 沪深300 ETF

- `300750.SZ` 宁德时代
- `600519.SH` 贵州茅台

### 516010.SH 游戏 ETF

- `002558.SZ` 巨人网络
- `002555.SZ` 三七互娱
- `002517.SZ` 恺英网络
- `002624.SZ` 完美世界
- `300251.SZ` 光线传媒

### 513120.SH 港股创新药 ETF

港股代码必须从真实 Universe Snapshot 或可靠来源读取并规范化。不得只按公司名称硬编码，也不得自动把港股与 A 股报告合并。

### 560010.SH 中证1000 ETF

- 默认 P4A Selection 为空时，P4B1 不应创建任何成分 Digest Binding。
- 当测试 Selection 通过重大事件强制选中一只成分时，只处理该一只成分。

## 十五、测试要求

必须新增独立 P4B1 测试，并覆盖以下场景：

1. 相同证券被两只 ETF 选中，只构建一个 Digest。
2. 两只 ETF 对同一证券产生不同 Binding 和选择理由。
3. 相同 Selection、相同知识输入得到稳定 Digest ID 和 Resolution ID。
4. 第二次执行命中缓存，不重复构建。
5. 新增有效 Claim 后知识指纹变化，并生成新版本 Digest 或使旧缓存失效。
6. Claim 满足失效条件后，Digest 变为 stale 或重新解析。
7. 没有研究时返回 missing，模型调用仍为 0。
8. 研究部分完整时返回 partial_reusable，并明确缺失维度。
9. 存在未解决关键冲突时返回 conflicted，并保留冲突 ID。
10. 晚于 `analysis_as_of` 的报告和 Claim 不得进入 Digest。
11. `failed_validation` 报告不得进入 Digest。
12. P4A 选择 0 只时返回空 Resolution。
13. P4A partial universe 的已选成分仍可解析，但保留来源警告。
14. 将 Resolution 挂接到 ETF Deep Report 时不创建报告正文或 Artifact。
15. 标的档案 API 返回成分研究状态、数据截止时间和质量。
16. `model_calls`、`input_tokens`、`output_tokens` 始终为 0。
17. 测试数据库与真实运行数据库隔离。
18. 现有 P0-P4A、报告目录、知识库、Deep Report 和设置 API 回归继续通过。

真实外部数据不应成为普通 CI 的硬依赖。

## 十六、真实知识覆盖审计

在不调用模型、不污染运行库的前提下，读取当前真实报告目录和知识库，对首批代表性成分输出审计表：

| 成分 | 规范代码 | 现有报告数 | 有效 Claim 数 | 覆盖维度 | 最新数据截止日 | Digest 状态 | 冲突数 | 可复用 ETF 数 |
|---|---|---:|---:|---|---|---|---:|---:|

需要明确区分：

- 确实没有研究。
- 有报告但没有被索引为 Claim。
- 有 Claim 但已经过期。
- 有可复用研究但维度不完整。
- 有有效研究且可完整复用。
- 有未解决冲突。

将审计结果保存为独立 Markdown 文件，建议：

`P4B1_COMPONENT_RESEARCH_AUDIT_YYYY-MM-DD.md`

## 十七、实施阶段

### P4B1-A：合同与基线

- 固化 Digest、Binding、Resolution 合同。
- 固化状态和研究维度枚举。
- 建立稳定指纹和序列化测试。
- 记录当前代表性成分知识覆盖基线。

### P4B1-B：知识解析与质量判定

- 接入报告目录和知识库。
- 实现证券代码、截止时间、质量、有效期和冲突过滤。
- 实现确定性维度映射与新鲜度规则。

### P4B1-C：存储、缓存与跨 ETF 共享

- 建立 Digest、Binding、Resolution 持久化。
- 实现幂等、single-flight 和知识指纹失效。
- 实现跨 ETF 共用同一 Digest。

### P4B1-D：P4A、Deep Report 与档案接入

- 解析 P4A Selection。
- 挂接 Deep Report 分析状态。
- 向标的档案 API 暴露结构化结果。
- 保证不生成正式报告 Artifact。

### P4B1-E：真实审计与回归

- 对代表性成分运行只读知识覆盖审计。
- 验证缓存命中和复用率。
- 完成专项与相关回归。
- 更新实施计划，但保持 P4B2 未完成。

## 十八、验收标准

P4B1 只有同时满足以下条件才算完成：

1. 同一成分可跨 ETF 共享同一个 Digest。
2. ETF 专属权重和选择理由保存在 Binding，不污染全局公司摘要。
3. 所有知识引用均可追溯至 Report、Claim、Fact 和 Evidence。
4. 不使用名称进行证券关联。
5. 不引用晚于 `analysis_as_of` 的知识。
6. missing、stale、partial、conflicted 和 reusable 可以明确区分。
7. 相同输入第二次真实命中缓存。
8. 新知识、失效和冲突变化能够使缓存失效。
9. P4A 选择 0 只时不产生无意义数据。
10. Deep Report 挂接不创建正文、PDF或正式 Artifact。
11. 标的档案 API 可以展示成分研究覆盖和新鲜度。
12. 全过程模型调用和 Token 消耗为 0。
13. 真实知识覆盖审计已保存到文件。
14. 相关回归通过，测试数据没有污染用户运行库。

## 十九、明确不在本任务范围内

- 不实现 ETF 成分和权重的外部采集 Provider。
- 不实现 P4B2 模型摘要生成。
- 不自动发起个股 Deep Research。
- 不根据名称猜测港股或双重上市公司映射。
- 不创建新的正式 ETF 周报或结构报告。
- 不大规模重做标的档案前端视觉设计。
- 不把完整个股报告复制进 ETF 报告。
- 不把 stale 或 conflicted 研究静默当成当前有效结论。

## 二十、验证与最终汇报

完成后必须：

1. 运行 Ruff 或项目现有 Python 静态检查。
2. 运行 P4B1 专项测试。
3. 运行 P0-P4A、ETF Deep Research、个股 Deep Research、报告目录、知识库和设置 API 相关回归。
4. 检查测试数据与真实运行数据库隔离。
5. 保存真实知识覆盖审计文件。
6. 更新 `ETF_DEEP_RESEARCH_REUSE_PLAN.md`：
   - P4B1 完成项标记为完成。
   - P4B2 继续保持待实施。

最终汇报必须说明：

- 新增了哪些数据合同、表和服务。
- Digest ID、Binding ID 和 Resolution ID 如何保持稳定。
- 如何避免未来数据泄漏和名称误匹配。
- 哪些成分已有可复用研究。
- 哪些成分 missing、stale、partial 或 conflicted。
- 跨 ETF 复用了多少 Digest。
- 第二次执行是否真实命中缓存。
- 避免了多少潜在模型调用。
- 模型调用数和 Token 是否均为 0。
- 运行了哪些测试及结果。
- 是否修改或写入了真实运行库。
- P4B2 开始前仍缺少哪些前置条件。

不要只提交设计文档；需要完成代码、专项测试、相关回归和一次只读的真实知识覆盖审计。
