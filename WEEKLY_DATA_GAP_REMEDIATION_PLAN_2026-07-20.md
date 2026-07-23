# 周报数据缺口治理与补全计划

> 状态：核心治理与黄金回归已完成；P4B2 高成本成分扩展研究待单独授权  
> 制定日期：2026-07-20  
> 适用范围：个股/ETF 周报上下文、分析智能体输出、统一报告目录复用、ETF 相对表现与跟踪质量、成分研究覆盖、Markdown/PDF 交付  
> 黄金标的：000651.SZ 格力电器、588870.SH 科创50ETF汇添富  
> 安全边界：只补充研究数据和修正报告表达；不自动启用监控、不发送外部消息、不执行交易

## 一、目标

本计划解决当前周报中三种不同性质的问题：

1. **不适用项被误报为缺口**：个股报告展示 ETF 专属范围。
2. **同一缺口被重复或错误计数**：机器缺口代码、智能体中文说明、审查说明和安全声明混入同一列表。
3. **确实缺少的研究数据**：ETF 指数相对表现、官方跟踪质量、部分成分公司研究，以及在截止时间内没有达到复用等级的结构研究结论。

完成后，周报必须能够回答：缺什么、为什么缺、是否适用于该标的、能否自动补、使用了什么来源、补全值属于官方披露还是确定性计算，以及该缺口是否真的影响报告质量。

## 二、当前基线与问题定界

### 2.1 000651.SZ 格力电器

- 当前上下文装配器无条件创建 8 个 ETF 专属范围，因此结构化数据和 Markdown 看起来存在大量缺口。
- 周报门控没有把这些 ETF 范围加入个股的硬缺口，但报告渲染仍逐项展示，形成“缺口很多”的错误观感。
- 真正需要核实的是“没有达到复用等级的报告结论”。该项可能由以下原因造成：
  - 周截止日以前没有合格结构报告；
  - 报告或来源数据晚于周截止日，被未来数据门禁排除；
  - 摘要 Claim 的支撑等级不是 `verified` 或 `triangulated`；
  - 结构观点已过期；
  - 报告已发布，但目录登记或知识链接不完整。
- 历史回归不得为了消除缺口而回填周截止日以后才出现的事实或报告。

### 2.2 588870.SH 科创50ETF汇添富

- 已具备：产品档案、跟踪指数、基金份额、折溢价、成分暴露。
- 硬缺口：指数相对表现、跟踪质量。
- 部分覆盖：成分公司研究；当前黄金样本为 5 个选择成分、3 个部分可复用、2 个缺失，实施时应重新解析最新选择快照，不能把历史名单写死。
- 同一个真实缺口同时以机器代码和智能体中文说明进入报告，造成重复。
- “已呈现支撑与反对证据”“仅供人工复核”等正常说明被误放进 `data_gaps`，错误触发 `passed_with_gaps` 或增加警告数量。
- 当前 `tracking_error` 范围把跟踪误差、跟踪偏离度、IOPV 和 IOPV 折溢价混为同一组；IOPV 可用并不能证明官方跟踪误差可用。

## 三、设计原则

### 3.1 先分清可观察事实，再决定补全动作

每个范围统一记录以下状态：

```text
applicability: applicable | not_applicable
availability: complete | partial | missing | not_applicable
source_kind: official_disclosure | verified_market_data | deterministic_calculation | catalog_claim
impact: blocking | confidence_only | disclosure_only
reason_code
data_as_of
fact_ids
evidence_ids
calculation_id
```

其中：

- `not_applicable` 不得进入 `data_gaps`，也不得在用户报告中展示为“缺失”。
- `partial` 必须说明已有覆盖和剩余项目，不能等同于全部缺失。
- 推导值必须登记公式版本、输入事实和截止时间。
- 官方值与估算值使用不同指标名，禁止把代理指标包装成官方披露。

### 3.2 一个缺口只有一个规范身份

- 机器层继续使用稳定英文 `reason_code`。
- 中文 Markdown/PDF 由中央术语表映射，不保存第二套业务状态。
- `data_gaps` 仅由范围状态和规范缺口登记表派生。
- 智能体不能自由增加数据缺口；只能引用输入中已经存在的 `reason_code`。
- 智能体的研究说明进入 `analysis_notes`，安全边界进入 `safety_notes`，反证审查问题进入 `critic.issues`，三者都不得无条件并入 `data_gaps`。

### 3.3 补全不等于隐藏

- 能通过现有合格事实确定性计算的数据，自动补全并保存血缘。
- 能从官方定期报告取得的数据，优先补充官方值。
- 只有价格代理可用时，明确标记为“市场价格口径的跟踪偏离”，继续披露官方指标缺失。
- 市场或官方资料确实不存在时，保留真实缺口，不以空值、默认值或模型推测伪造完整性。

## 四、目标数据契约

### 4.1 保持兼容的增量结构

保留现有 `weekly_review_v2.data_gaps: string[]`，新增结构化明细：

```json
{
  "data_gap_details": [
    {
      "reason_code": "etf_official_tracking_error_unavailable",
      "scope": "tracking_quality",
      "availability": "partial",
      "impact": "confidence_only",
      "source": "weekly_context",
      "missing_items": ["official_tracking_error"],
      "data_as_of": "2026-07-17"
    }
  ],
  "analysis_notes": [],
  "safety_notes": []
}
```

兼容规则：

- `data_gaps` 由 `data_gap_details[].reason_code` 去重派生，旧消费者继续可读。
- 用户报告只从中央术语映射生成中文描述。
- 未登记代码在测试和正式编译中失败，不使用通用英文兜底。
- 旧周报只读，不批量重写；新修订周报使用增量字段。

### 4.2 ETF 范围语义修正

ETF 范围拆分为：

```text
product_profile
tracking_index
index_relative_strength
fund_shares
premium_discount
nav_reference
official_tracking_quality
market_tracking_deviation
component_exposure
component_research
```

迁移规则：

- IOPV 移入 `nav_reference`，不得再使 `official_tracking_quality` 变为完整。
- 官方跟踪误差、官方跟踪偏离度归入 `official_tracking_quality`。
- 基于 ETF 与指数行情计算的偏离归入 `market_tracking_deviation`。
- 为兼容旧读取方，可继续输出旧 `tracking_error` 汇总视图，但必须带 `availability=partial` 和子范围明细，禁止误报完整。

## 五、实施阶段

### P0：修复假缺口和报告污染

目标：先让缺口数量真实可信，再开展数据补全。

- [x] `WeeklyContextAssembler.assemble` 显式接收 `instrument_type`，不再仅凭统一 ETF 范围装配所有标的。
- [x] 个股上下文不输出 ETF 专属范围，并以固定回归测试锁定该契约。
- [x] Markdown/PDF 不渲染不适用范围。
- [x] 建立规范缺口登记表，集中维护 `reason_code`、中文名称、适用资产类型和影响等级。
- [x] 周报服务只从结构化范围状态派生 `data_gaps`，不再直接拼接任意自然语言。
- [x] 修改分析智能体契约：`data_gaps` 只能选择已登记且在输入中存在的代码。
- [x] `critic.verdict=pass` 时，审查说明不降低报告质量；审查说明进入独立分析字段。
- [x] 把安全声明固定在 `safety_notes`，继续保持 `trade_execution=forbidden`。
- [x] 对语义相同的门控缺口、上下文缺口和智能体引用按规范代码去重。

主要修改位置：

- `agent/src/portfolio/weekly/context.py`
- `agent/src/portfolio/weekly/service.py`
- `agent/src/portfolio/weekly/reporting.py`
- `agent/src/portfolio/weekly/presentation.py`
- `agent/src/portfolio/weekly/contracts.py`
- `agent/src/portfolio/analysis_methods.py`

### P1：补全 ETF 指数相对表现

目标：从已经登记的跟踪指数和冻结行情中确定性生成周度相对表现。

- [x] 从合格产品档案读取 `tracked_index_code`，禁止由名称猜测指数代码。
- [x] 对 ETF 和跟踪指数执行同一截止时间、同一交易日历、同一完成日线门禁。
- [x] 优先使用周开始前最后一个共同有效收盘价和周结束时最后一个共同有效收盘价。
- [x] 计算并登记：
  - `etf_market_return_1w`
  - `tracked_index_return_1w`
  - `fund_index_return_gap_1w`
  - `index_relative_strength_1w`
- [x] 保存公式版本、重叠交易日、行情输入指纹、行情调整口径和 `data_as_of`。
- [x] 任一端缺少共同基准日或周末有效日时，保持 `missing/partial`，不使用不同日期强行比较。
- [x] 结果进入周度观点和智能体冻结上下文，但智能体不得改写数值。

建议公式：

```text
weekly_return = end_close / prior_common_close - 1
fund_index_return_gap_1w = etf_weekly_return - index_weekly_return
```

这里得到的是 ETF 二级市场价格相对指数的表现，不是官方跟踪误差。

### P2：补全并分层呈现跟踪质量

目标：同时提供官方口径和可复算的市场价格代理，且明确两者边界。

- [x] 检查现有基金年度报告、半年度报告、产品资料概要和已归档官方来源是否披露跟踪误差或跟踪偏离度。
- [x] 官方值进入结构化 Fact，记录报告期、单位、来源链接、Evidence 和支撑状态。
- [x] 使用 ETF 与跟踪指数的共同完成日线计算 20/60 日市场价格跟踪偏离代理：

```text
daily_gap_t = etf_return_t - index_return_t
annualized_market_tracking_deviation = stddev(daily_gap_t) × sqrt(250)
cumulative_market_return_gap = cumulative_etf_return - cumulative_index_return
```

- [x] 少于 20 个共同交易日不得生成 20 日代理；少于 60 个共同交易日不得生成 60 日代理。
- [x] 代理指标命名中包含“市场价格口径”或 `market_*`，不得写作官方跟踪误差。
- [x] 官方披露与市场代理使用两个独立范围；兼容汇总只反映二者组合状态。
- [x] IOPV 和折溢价单独展示，不再参与官方跟踪质量完整性判断。

建议新增确定性模块：

- `agent/src/portfolio/weekly/etf_metrics.py`

复用接口：

- 统一行情服务及其完成日线门禁；
- `agent/src/reports/etf_product_profile.py` 的产品档案与官方来源归档；
- 统一报告目录 Fact/Evidence 持久化。

### P3：修复格力电器结构结论复用链路

目标：在不引入未来数据的前提下，尽可能复用已经核验的 Deep Research 结论，并把不能复用的原因说清楚。

- [x] 为目录候选输出明确的 `reuse_exclusions`，区分未来数据、观点过期、支撑等级不足、登记缺失和无结构摘要。
- [x] 核查最新格力电器正式 Deep Research 的目录登记、结构观点、Claim 支撑状态和知识链接。
- [x] 核查结果没有发现截止日内的合格报告登记缺失，因此未触发目录改写或重新研究。
- [x] 现有报告晚于历史周截止日，黄金历史回归保留一条真实缺口；没有回填或倒签。
- [ ] 对实时下一周周报验证合格结构结论能够被正常复用，以证明链路已经修复。
- [x] 将泛化提示“暂无达到复用等级的报告结论”改为可审计的具体原因，但用户版不暴露内部长编号。

### P4：补全 ETF 成分研究覆盖

目标：优先零模型复用，只对尚未覆盖的重要成分启动受控研究。

- [x] 重新解析 588870 最新 `selection_id`，输出选择成分、权重覆盖、解释覆盖、研究覆盖和剩余名单。
- [x] 先运行 P4B1 确定性复用，匹配已发布且支撑等级合格的组件研究。
- [x] 对过期、冲突、缺失分别标记，不把三者合并为笼统“无研究”。
- [x] 生成 P4B2 候选清单，包含成分代码、权重、现有证据状态和预计模型调用。
- [ ] 只有用户明确授权后，才对剩余成分执行 P4B2 扩展研究。
- [ ] P4B2 结果通过来源、Claim、Fact、Evidence 和目录登记门禁后，再重新计算 `research_coverage` 与 `fully_supported_coverage`。
- [x] 不因少数成分研究不完整而把 `component_exposure` 降为缺失。

当前历史基线中的 688008.SH、688012.SH 仅作为复核起点，实施时必须以最新选择快照为准。

### P5：重新生成与黄金回归

- [x] 在固定周截止日 2026-07-17 下重新生成 000651.SZ 周报修订版。
- [x] 在固定周截止日 2026-07-17 下重新生成 588870.SH 周报修订版。
- [x] 保留旧运行只读；使用 `force_new` 生成新修订，不覆盖历史产物。
- [x] 核对结构化 JSON、Markdown、PDF 和运行卡片中的缺口数量与含义一致。
- [x] 物化两份 PDF，并逐页检查乱码、截断、重叠、异常空白和英文机器术语。
- [x] 验证每份黄金报告只有已启用的市场分析智能体产生 1 次模型调用；没有隐式启动 Deep Research 或 P4B2。
- [x] 验证监控启用数、外部发送数、交易执行数均为 0。

## 六、测试矩阵

### 6.1 单元测试

- [x] 个股装配不产生 ETF 缺口代码。
- [x] 个股 Markdown/PDF 不展示 ETF 专属范围。
- [x] ETF 仍完整装配所有适用范围。
- [x] 不适用范围不参与质量状态。
- [x] 同一缺口在门控、上下文和智能体中出现时只计一次。
- [x] 正常研究说明和安全声明不能进入 `data_gaps`。
- [x] `critic.verdict=pass` 不因普通说明降级报告。
- [x] 智能体提交未登记或未冻结缺口代码时契约校验失败。
- [x] IOPV 可用而官方跟踪误差缺失时，跟踪质量不得判为完整。
- [x] 指数相对表现只使用截止日前共同完成日线。
- [x] 共同交易日不足或行情冲突时安全降级；指数代码只接受结构化事实。
- [x] 市场价格偏离代理与官方跟踪误差使用不同指标名和来源类型。
- [x] 组件研究部分覆盖只降低自身范围，不污染成分暴露范围。
- [x] 目录候选被排除时返回稳定、可映射的原因代码。

### 6.2 集成测试

- [x] 周报上下文只读取结构化目录、Fact 和 Evidence，不解析旧 Markdown。
- [x] 相对表现计算结果可从输出追溯到 ETF/指数行情输入指纹与来源清单。
- [x] 官方跟踪质量可追溯到原始定期报告页面和内部稳定索引。
- [ ] 格力电器合格结构 Claim 可以在下一份实时周报中复用。
- [x] 历史回归拒绝晚于截止日的数据和不合格 Claim。
- [x] P4B1 复用不产生模型调用；P4B2 未授权时保持关闭。
- [x] 报告质量、覆盖状态、警告数量和 `data_gap_details` 一致。

### 6.3 回归范围

至少运行：

```text
agent/tests/test_portfolio_weekly_run.py
agent/tests/test_portfolio_weekly_api_scheduler.py
agent/tests/test_portfolio_analysis_methods.py
agent/tests/test_report_library.py
agent/tests/test_etf_product_profile.py
agent/tests/test_component_research.py
agent/tests/test_component_research_generation.py
agent/tests/test_equity_deep_research.py
agent/tests/test_etf_deep_research.py
agent/tests/test_report_pdf_api.py
```

并运行相关 Ruff 检查和用户版机器术语扫描。

## 七、黄金验收标准

### 7.1 格力电器

- [x] `data_gaps` 中不存在任何 ETF 专属缺口。
- [x] Markdown/PDF 不展示基金份额、折溢价、跟踪指数、跟踪质量和成分研究范围。
- [ ] 如果结构结论可在截止时间内复用，缺口清零并带完整 Claim/Fact/Evidence 血缘。
- [x] 结构结论因未来数据不可复用，保留一条具体、真实、非重复缺口。
- [x] 未通过回填未来数据把历史回归伪装为完整。

### 7.2 588870

- [x] 指数相对表现范围包含 ETF 周收益、指数周收益和两者差值。
- [x] 跟踪质量明确区分官方披露与市场价格代理。
- [x] IOPV 不再错误地代表跟踪误差完整。
- [x] 成分暴露保持完整；成分研究按实际结果为 `partial`。
- [x] 同一个业务缺口只出现一次。
- [x] 安全声明、支持证据说明和普通反证审查说明不计入数据缺口。
- [x] 未经授权没有执行 P4B2；剩余成分研究缺口保持可见。

### 7.3 共同验收

- [x] 机器 JSON 枚举稳定，用户版 Markdown/PDF 未发现英文机器术语。
- [x] `quality_status` 只由真正影响报告的缺口派生。
- [x] 缺口清单、数据范围、首页警告、运行卡片和 PDF 表达一致。
- [x] 所有数值均来自已冻结事实或已登记确定性公式，智能体不生成新数值。
- [x] 两份周报模型调用、监控、发送和交易副作用符合既定边界。

## 八、发布顺序与回滚

发布顺序固定为：

```text
P0 缺口语义与假缺口修复
→ P1 指数相对表现
→ P2 跟踪质量分层
→ P3 结构结论复用修复
→ P4 成分研究覆盖
→ P5 黄金回归与 PDF 验收
```

回滚与兼容要求：

- 每阶段独立提交和验收，后续阶段不得掩盖前一阶段失败。
- 旧 JSON 和历史 Markdown/PDF 保持只读。
- 新字段采用兼容增量；现有 `data_gaps` 保留。
- 任一新数据源失败时只降低对应范围，不中断市场结构周报生产。
- 任一模型调用失败时保留确定性结果，不自动重试高成本研究。

## 九、完成定义

只有同时满足以下条件，本计划才可标记为完成：

- 假缺口、重复缺口和错放说明全部消失；
- 000651.SZ 不再展示任何 ETF 专属缺口；
- 588870.SH 的指数相对表现可确定性复算；
- 官方跟踪质量与市场价格代理不再混用；
- 成分研究覆盖与最新选择快照一致；
- 仍无法补足的数据具有明确原因、来源边界和影响等级；
- 黄金 JSON、Markdown、PDF 与运行卡片通过一致性验收；
- 未引入未来数据、隐式扩展研究、自动监控、外部发送或交易副作用；
- 全部相关自动化测试和视觉门禁通过。

## 十、2026-07-20 执行记录

### 10.1 已沉淀的通用方法与接口

本轮没有把逻辑写死在格力电器或 588870，新增能力按资产类型、规范代码和输入事实工作：

- `agent/src/portfolio/instruments.py`
  - `infer_portfolio_instrument_type(symbol, explicit=None)`：统一识别个股和 ETF。
  - `portfolio_tick_size(symbol, instrument_type=None)`：统一提供标的最小价格步长。
- `agent/src/reports/data_gaps.py`
  - `data_gap_registry_payload(instrument_type=None)`：向服务、页面和其他报告生产器提供同一份缺口登记表。
  - `make_gap_detail`、`normalize_gap_details`、`gap_codes`：校验适用性、去重并派生兼容代码。
- `agent/src/portfolio/weekly/etf_metrics.py`
  - `build_etf_tracking_metrics(...)`：对任意已登记跟踪指数的 ETF 计算周度相对表现和 20/60 日市场价格跟踪偏离。
  - 方法版本：`etf-tracking-metrics/1.0`。
- `agent/src/reports/etf_tracking_disclosure.py`
  - `extract_official_tracking_disclosure(text)`：确定性解析基金定期报告中的跟踪质量表。
  - `register_official_tracking_disclosure(...)`：把原文、Evidence、Fact 和结构化提取结果幂等写入统一知识库。
- `GET /portfolio/weekly-runs/capabilities`
  - 返回适用资产类型、中央缺口登记表、确定性方法版本、输出范围，以及是否属于官方指标。

这些接口不依赖两个黄金标的的名称；其他个股和 ETF 只要进入同一产品档案、行情与知识登记链路，即可复用。

### 10.2 官方跟踪质量补录

- 官方来源：上海证券交易所 2026-03-31 发布的 588870 2025 年年度报告。
- 原文地址：`https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-31/588870_20260331_S82Q.pdf`
- 内部文档：`doc_21c9e77d20daeb120415b37e`
- Evidence：`evidence_5d1efeb84a104daefea77648`
- 已登记 6 条 Fact，覆盖近三个月、近六个月收益差与波动差，以及合同约定的日均跟踪偏离度和年跟踪误差上限。
- 用户报告展示：近三个月基金净值收益减基准收益 `+0.02%`，近六个月 `-0.78%`；合同目标分别为日均偏离绝对值不超过 `0.20%`、年跟踪误差不超过 `2.00%`。

### 10.3 588870 确定性行情补全

- 跟踪指数来自产品档案：`000688.SH`，没有按名称猜测。
- 指数行情刷新运行：`2e1b8615f20d443e9eef198290d0a117`，写入 285 行候选数据；最终值由东方财富和腾讯交叉验证，异常的单一来源值没有进入正式结果。
- 共同完成交易日：95 个。
- 2026-07-13 至 2026-07-17：ETF 市场价格收益 `-18.83%`，指数收益 `-16.93%`，ETF 相对指数收益差 `-1.91%`。
- 二十日市场价格跟踪偏离年化代理 `9.95%`，六十日代理 `6.39%`；报告明确说明它们不是官方跟踪误差。

### 10.4 最新成分覆盖与 P4B2 候选

- Selection：`p4aselection_c348ad8767d6e0c4e571a89b`
- 选择 5 只，选择权重覆盖与解释覆盖均为 `40.793%`。
- P4B1：3 只部分可复用，2 只缺失；`research_coverage=60.194%`，`fully_supported_coverage=0`，过期 0、冲突 0、缺失 2。
- 成分暴露范围保持完整；只有成分研究范围为部分可用。

| 成分 | 权重 | 当前状态 | 扩展前置条件 | 当前预算上界 |
|---|---:|---|---|---:|
| 澜起科技 `688008.SH` | 8.166% | 缺失 | 先形成通过门禁的冻结 Evidence Pack；另需明确授权并扩展 P4B2 v1 精确范围 | 最多 1 次模型调用，输入 6,000、输出 1,000 tokens |
| 中微公司 `688012.SH` | 8.072% | 缺失 | 先形成通过门禁的冻结 Evidence Pack；另需明确授权并扩展 P4B2 v1 精确范围 | 最多 1 次模型调用，输入 6,000、输出 1,000 tokens |

当前没有执行这两只的模型研究，原因不是链路失败，而是计划中的高成本授权门禁生效。若后续授权，整个批次硬上界为 2 次模型调用、12,000 输入 tokens、2,000 输出 tokens，仍以 Evidence 预检结果为先。

### 10.5 最终黄金运行

| 标的 | 运行 | 结果 | 唯一真实缺口 | 模型与副作用 |
|---|---|---|---|---|
| 格力电器 `000651.SZ` | `wrr_20260717_000651_SZ_r3_90bb231e` | `passed_with_gaps / partial` | 历史报告结论因数据时点晚于周截止日不可复用 | 市场分析智能体 1 次；监控 0、发送 0、交易 0 |
| 科创50ETF汇添富 `588870.SH` | `wrr_20260717_588870_SH_r6_74a85955` | `passed_with_gaps / partial` | 成分公司研究尚未完全覆盖 | 市场分析智能体 1 次；监控 0、发送 0、交易 0 |

两份运行均由 `force_new` 创建新修订，旧运行和旧产物保持只读。结构化 JSON、Markdown、PDF 的缺口含义一致；Markdown/PDF 机器术语扫描未发现本计划关注的英文状态或字段名。两份 PDF 各 5 页，已逐页检查中文字体、表格、分页、页眉页脚、截断和重叠。

### 10.6 自动化验证

- 计划列出的回归范围加本轮新增测试：`163 passed, 1 skipped`。
- P4B2 受控生成专项：`20 passed`；历史 Evidence 夹具已固定登记时间，不再随真实日期推移误判为未来数据。
- PDF API：改用仓库已有的 `pypdfium2`，`12 passed`，不再依赖未声明的 `pypdf`。
- Ruff：本轮修改文件全部通过。

### 10.7 尚未关闭的两项门禁

1. 格力电器的“下一份实时周报复用结构 Claim”需要等到一个完成的后续交易周，不能用未来行情伪造验收；现有单元与历史截止日回归已经覆盖复用和未来数据拒绝规则。
2. 688008.SH、688012.SH 的 P4B2 扩展研究仍需要用户单独给出精确标的、调用与 token 授权；在此之前保留真实部分覆盖状态。
