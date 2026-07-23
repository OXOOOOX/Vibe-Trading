# ETF Deep Research 数据复用与增量分析主计划（P0–P6）

状态：P0–P4B2-G 已完成；P4B2-F 功能优先真实试运行已完成三只生成、统一知识发布、P4B1 回流、幂等和 HTTP API 验收；P5–P6 待后续推进  
适用范围：A 股宽基 ETF、行业 ETF 与主题 ETF  
首个真实验收标的：588870.SH

本文件是 P0–P6 的跨对话主计划和状态入口。详细材料：

- P4A 自动采集与真实外部源验收：[ETF_UNIVERSE_PROVIDER_VALIDATION_2026-07-18.md](ETF_UNIVERSE_PROVIDER_VALIDATION_2026-07-18.md)。
- P4A 早期季度前十大取值验证：[P4A_HOLDING_VALIDATION_2026-07-18.md](P4A_HOLDING_VALIDATION_2026-07-18.md)。
- P4B1 完整开发要求：[P4B1_COMPONENT_RESEARCH_REUSE_PROMPT.md](P4B1_COMPONENT_RESEARCH_REUSE_PROMPT.md)。
- P4B1 真实知识覆盖审计：[P4B1_COMPONENT_RESEARCH_AUDIT_2026-07-18.md](P4B1_COMPONENT_RESEARCH_AUDIT_2026-07-18.md)。
- P4B2 完整实施与生成前准备要求：[P4B2_CONTROLLED_RESEARCH_GENERATION_PROMPT.md](P4B2_CONTROLLED_RESEARCH_GENERATION_PROMPT.md)。

## 一、目标

建立长期维护的 `ETF Research State`，实现：

- 同一份已验证数据只获取一次。
- 同一组输入只执行一次确定性计算或模型模块。
- 同一成分股研究可被多只 ETF 复用。
- 高频监测只处理增量，不反复生成完整报告。
- 日报、周报和结构报告共享同一套 Snapshot、Fact、Evidence 与 Claim。
- 只有观点或结构发生实质变化时才生成新报告或 revision。

明确不做：

- 不另建第二套事实库或报告中心。
- 不把 ETF 份额增长直接归因于国家队。
- 不将旧 Claim 当作新 Evidence。
- 不因盘中每次检查都生成正式报告。
- 不自动把报告条件写入或启用交易、监控规则。

## 二、总体架构

```text
数据源
  -> 现有 Market Cache / Research Cache
  -> ETF Snapshot Builder
  -> 变化检测与复用决策器
       -> reuse：复用既有观点，不调用模型
       -> monitor_delta：只生成确定性监控事件
       -> partial_refresh：刷新局部模块
       -> section_revision：刷新结构报告章节
       -> full_refresh：完整生成 ETF Deep Research
  -> Fact / Evidence / Claim
  -> 统一报告目录的 daily / weekly / structural 轨道
```

## 三、数据与复用分层

### 3.1 原始数据层

继续复用现有行情和研究缓存，新增 ETF 专项数据域：

- ETF 身份与基金管理人。
- 跟踪指数、编制规则版本。
- 指数成分、PCF、权重与覆盖率。
- ETF 总份额、净值、IOPV、折溢价和成交额。
- 规模、跟踪误差与流动性。
- 定期报告前十大基金份额持有人。
- 经官方披露确认的政策资金持有人。
- 成分股财务、公告与重大事件。

### 3.2 不可变 Snapshot 层

- `ETFIdentitySnapshot`
- `ETFUniverseSnapshot`
- `ETFMarketSnapshot`
- `ETFHolderSnapshot`

Snapshot 一旦生成不修改；下一次变化生成新 ID，并保留来源、数据截止时间、覆盖率和内容哈希。

### 3.3 模块结果层

模块结果以输入指纹幂等复用：

- `identity`
- `universe`
- `aggregate_fundamentals`
- `price_volume`
- `flow_liquidity`
- `holder_structure`
- `holding_penetration`
- `scenarios_watchlist`

### 3.4 成分股摘要层

`ComponentResearchDigest` 按证券代码全局共享。ETF 只追加该成分在本 ETF 中的权重、涨跌贡献和选择原因，不重复加载完整个股报告。

### 3.5 报告章节层

章节输入指纹至少包含：

```text
profile_version
+ universe_snapshot_id
+ market_snapshot_id
+ holder_snapshot_id
+ selected_component_digest_ids
+ prompt_version
+ model_id
```

只有影响对应章节的输入变化，章节才进入 stale 或 refresh 状态。

## 四、ETF Deep Research Profile

Profile：`etf_deep_research`  
默认模式：`compact`  
报告类型：`deep_research`  
报告周期：`structural`

固定章节：

1. `executive_summary`：结构性结论与主要矛盾。
2. `index_and_product`：基金、指数及编制规则。
3. `exposure_structure`：行业、主题、因子和集中度。
4. `aggregate_fundamentals`：聚合估值与盈利周期。
5. `price_volume_structure`：日线、周线量价结构。
6. `flow_liquidity_tracking`：份额、折溢价、流动性和跟踪误差。
7. `holding_penetration`：关键成分穿透。
8. `scenarios_watchlist`：情景、成立条件、失效条件和跟踪指标。

ETF Profile 不要求上市公司的三张财务报表、会计科目异常、单家公司长期利润反推或 TAM 模型。

## 五、自适应持仓穿透

所有成分先进行低成本确定性扫描，只有少数进入模型：

| ETF 结构 | 默认穿透数量 |
|---|---:|
| 中证1000等高度分散宽基 | 0–2 |
| 沪深300等中度集中宽基 | 2–3 |
| 科创50等行业暴露较集中的宽基 | 3，最多5 |
| 创新药等集中型行业 ETF | 3–5 |

强制选择条件：

- 单只权重达到约 8%。
- 对近期 ETF 涨跌贡献显著。
- 对聚合盈利或估值贡献显著。
- 出现重大经营、政策或监管事件。
- 既有研究过期或出现证据冲突。

新增一个成分带来的解释覆盖率提升不足 5 个百分点时停止。无论何种情况，默认硬上限为 5 个。

## 六、复用决策

`ETFAnalysisDecision` 支持：

- `reuse`
- `monitor_delta`
- `partial_refresh`
- `section_revision`
- `full_refresh`

首版结构变化触发器包括：

- ETF 单日份额变化超过 1%。
- 五日累计份额变化超过 3%。
- 放量达到近期均量 1.5 倍。
- 突破或跌破结构报告关键点位。
- 关键成分权重变化超过 0.5 个百分点。
- 指数调仓或编制规则变化。
- 关键成分重大公告。
- 官方披露的政策资金持仓发生变化。
- 既有 Claim 满足失效条件。

阈值为首版默认值，必须可配置并在真实运行中校准。

## 七、Token 与调用预算

默认 `compact` 上限：

- ETF 结构化上下文：8,000 tokens。
- 历史有效 Claim：4,000 tokens。
- 新闻与事件摘要：3,000 tokens。
- 持仓摘要：每只 600，最多 5 只，共 3,000 tokens。
- Profile 与编译指令：4,000 tokens。
- 总输入硬上限：24,000 tokens。
- 总输出硬上限：6,000 tokens。
- 自动修复最多 1 轮。

盘中确定性检查可频繁执行，但默认：盘中 AI 解释每日最多 2 次、日报每日 1 次、周报每周 1 次、结构完整刷新每日最多 1 次。超过模型预算后，确定性风险提醒仍可继续运行。

## 八、报告组织

- 普通盘中变化：Monitor Event，不登记正式报告。
- 日报：每交易日原则上一份，重大变化使用 revision。
- 周报：聚合本周 Snapshot 与 Delta，不重新搜索一周数据。
- 结构报告：事件驱动，不按天重复生成。
- 无变化：保存 Analysis Reuse Audit，引用既有报告，不生成 Artifact。
- 正式报告继续进入现有统一报告目录，不新增第二入口。

## 九、实施阶段

### P0：契约与基线

- [x] 固化 ETF Profile、Snapshot、ModuleResult、AnalysisDecision 数据合同。
- [x] 建立 588870 无变化、局部变化和结构变化测试夹具。
- [x] 建立 token、模型调用、缓存命中和重复请求基线指标。
- [x] 固化弱数据停止门控与价格敏感数据实时校验规则。

验收：契约可序列化和校验；相同输入得到稳定 ID；基线指标可查询。

### P1：Profile Registry

- [x] 将公司 Profile 的章节和 Prompt 分派改成注册表。
- [x] 新增 `etf_deep_research` Profile。
- [x] Deep Report API、Session Service 和 Record 支持 ETF Profile。
- [x] 保持现有公司 Deep Report 完全兼容。

验收：公司与 ETF Profile 可并行创建；ETF 不触发公司财务硬门控；未知 Profile fail closed。

### P2：ETF Snapshot

- [x] 新增 ETF Snapshot 合同与不可变持久化。
- [x] 保存来源、数据时间、覆盖率、质量和内容哈希。
- [x] 同一内容幂等复用，不生成重复 Snapshot。
- [x] Snapshot 只引用现有 Fact/Evidence，不复制第二套事实。

验收：相同 Snapshot 重复保存 ID 不变；变化后生成新 ID；读取时可判断 freshness 与 coverage。

### P3：模块缓存与变化路由

- [x] 新增模块输入指纹和结果缓存。
- [x] 实现 `reuse/monitor_delta/partial_refresh/section_revision/full_refresh` 路由。
- [x] 同一 ETF 同一输入使用 single-flight。
- [x] 保存复用、刷新和 token 预算审计记录。
- [x] 无变化决策只写审计，不创建正式报告或 Artifact。

验收：相同输入第二次执行不调用模块；局部变化只刷新受影响模块；并发请求只运行一次。

### P4：自适应持仓穿透

#### P4A：确定性扫描与候选选择

- [x] 建立 ETFUniverseProvider、ETF 到指数审计映射和确定性 fallback 链。
- [x] 接入中证指数官方结构化收盘权重文件。
- [x] 接入 Tushare `index_weight` 与 `fund_portfolio` 降级 Provider，并显式记录权限失败。
- [x] 区分完整指数权重、可信 Top-ranked partial 和随机残缺数据。
- [x] 实现 cache-first、内容哈希去重、single-flight、失败审计和有效缓存降级。
- [x] 提供 Universe 状态、最新快照、单只刷新和当前持仓预热 API。
- [x] 标准化全量或部分成分、权重、涨跌贡献、盈利贡献和事件状态。
- [x] 计算 Top1/3/5/10、HHI 区间、有效成分数和已知权重覆盖率。
- [x] 将 ETF 分类为高度分散、中度分散、聚焦或集中型结构。
- [x] 根据结构设置 0–5 只的最小/最大穿透数量。
- [x] 实现权重、涨跌贡献、盈利贡献、重大事件、证据冲突和研究过期的确定性评分。
- [x] 实现边际解释覆盖率不足 5 个百分点时停止，并保留强制选择例外。
- [x] 输入不完整时明确标为 partial，不把前十大披露冒充全量成分。
- [x] 将选择结果接入 `holding_penetration` 模块指纹与缓存，模型调用数保持为 0。

P4A 验收：五只代表 ETF 已通过中证指数公司官方结构化权重文件真实验收；相同 Universe Snapshot 得到稳定 Selection ID；宽基、聚焦宽基和集中行业 ETF 的选择数量符合结构上限；同一输入第二次同时命中 Snapshot 和 P4A 模块缓存，不再访问网络。

#### P4B1：成分研究摘要确定性复用底座

- [x] 跨 ETF 共享 ComponentResearchDigest。
- [x] 接入已有个股 Deep Report Claim。
- [x] 建立 ETFComponentDigestBinding，分离全局公司研究和 ETF 专属权重/选择理由。
- [x] 建立 reusable、partial_reusable、stale、missing 和 conflicted 状态判定。
- [x] 实现 `analysis_as_of` 截止时间、知识指纹、缓存和跨 ETF single-flight。
- [x] 将 Resolution 挂接到 Deep Report 分析状态和标的档案 API。
- [x] 保存真实知识覆盖审计，全程模型调用和 Token 保持为 0。

P4B1 完整要求：`P4B1_COMPONENT_RESEARCH_REUSE_PROMPT.md`。

#### P4B2：缺失研究的受控模型补充

- [x] 固化生成前准备、Evidence Pack、dry-run、预算、授权和首批试运行方案。
- [x] 实现默认关闭的 P4B2 功能开关、Policy、Plan、Job 和预算台账。
- [x] 实现只读 Preflight、Evidence Pack 和真实候选 dry-run。
- [x] 实现有证据约束的 bounded Component Research 结构化生成器。
- [x] 实现统一 Report/Claim/Fact/Evidence 事务化发布和 P4B1 回流。
- [x] 只对 P4A 入选且状态为 missing、stale 或关键 conflicted 的成分考虑调用模型。
- [x] 跨 ETF、跨报告复用同一成分的有效 Digest，避免重复调用。
- [x] 设置单成分、单 ETF、单日模型调用和 Token 硬上限。
- [ ] 将新摘要、选择原因、解释覆盖率和来源写入 ETF 报告。
- [x] 560010 等高度分散宽基默认不因权重机械生成成分研究。
- [x] 经显式授权后，完成 588870.SH 三只强制候选的首批受控试运行。

P4B2 完整要求：`P4B2_CONTROLLED_RESEARCH_GENERATION_PROMPT.md`。没有明确授权时，只允许实施代码、测试和 dry-run，不允许真实模型调用或生产知识写入。

### P5：周报与前端

- [ ] 新增 `weekly_review` 生产器。
- [ ] 展示复用率、刷新模块、数据截止时间和持仓选择理由。
- [ ] 提供复用、局部刷新和完整刷新入口。

### P6：真实运行观察

- [ ] 588870 科创50型 ETF。
- [ ] 沪深300 ETF。
- [ ] 中证1000 ETF。
- [ ] 集中型创新药 ETF。

## 十、P0–P4A Definition of Done

- 相同数据和 Profile 产生稳定、可审计的指纹。
- 相同输入不会重复运行确定性模块或模型模块。
- 无实质变化不会创建新报告或 Artifact。
- ETF Profile 不依赖上市公司财报框架。
- 复用决策能解释复用了什么、刷新了什么以及原因。
- 价格敏感判断不得复用失效或未验证行情。
- 现有公司 Deep Report、日报、监控和统一报告目录回归通过。
- 所有缓存命中、token 预算和节省量均可观测。

## 十一、P0–P4A 实施记录

- 数据合同位于 `agent/src/reports/contracts.py`。
- ETF 状态、SQLite 持久化、模块缓存、single-flight、变化路由和指标位于 `agent/src/reports/etf_research.py`。
- Profile 注册、ETF Prompt 与动态章节编译位于 `agent/src/reports/profile.py` 和 `agent/src/reports/service.py`。
- 588870 三类场景夹具位于 `agent/tests/fixtures/588870_etf_research_scenarios.json`。
- P0–P3 专项测试位于 `agent/tests/test_etf_deep_research.py`。
- P4A 确定性扫描、集中度计算、候选评分、边际停止与缓存接入位于 `agent/src/reports/etf_penetration.py`。
- P4A 专项测试位于 `agent/tests/test_etf_penetration.py`，当前持仓取值验证位于 `P4A_HOLDING_VALIDATION_2026-07-18.md`。
- `ETFUniverseProvider` 自动采集、来源映射、质量语义、缓存/single-flight、P4A 接入和 API 已完成；真实外部源验收记录位于 `ETF_UNIVERSE_PROVIDER_VALIDATION_2026-07-18.md`。
- 正式运行仍遵循 P4B1–P6 边界：P4A 只完成低成本候选选择，不生成成分研究摘要、周报或正式 ETF Deep Research Artifact。

## 十二、P4 统一状态记录（2026-07-18）

### 12.1 当前链路

```text
ETF 代码
  -> ETF/指数审计映射
  -> ETFUniverseProvider 确定性来源链
  -> ETFUniverseSnapshot
  -> P4A holding_penetration
  -> ETFComponentSelection
  -> P4B1 ComponentResearchDigest 复用（已完成）
  -> P4B2 缺口模型补充（代码与 dry-run 已完成，真实试运行待授权和 Evidence）
```

当前已经完成到 `ComponentDigestResolution`。采集、快照、P4A 选择、P4B1 知识解析、缓存和 API 路径均不调用模型。

### 12.2 Provider 状态

| Provider | 状态 | 当前用途与边界 |
|---|---|---|
| 中证指数官方结构化收盘权重 | 已启用并通过真实验收 | 当前首选完整成分与权重来源 |
| Tushare `index_weight` | 已启用；本机权限不足 | 显式记录 `permission_denied` 后 fallback，不伪装成功 |
| Tushare `fund_portfolio` | 已启用；本机权限不足 | 只能作为季度 Top-ranked partial 降级来源 |
| PCF | 仅保留扩展位 | 申赎数量和现金替代字段不擅自转换为指数权重 |

来源链同时实现了 ETF 到指数审计映射、质量判定、官方源传输重试、cache-first、内容哈希去重、每代码 single-flight、失败/fallback/缓存命中审计和有效缓存降级。

### 12.3 五只 ETF 真实验收

数据来源：中证指数公司官方结构化收盘权重文件。  
数据截止：2026-06-30。  
验收运行：使用隔离 SQLite；首次真实联网，第二次读取均命中 Snapshot 和 P4A 缓存。

| ETF | 跟踪指数 | 成分 | 原始权重覆盖 | P4A 入选 | 第二次读取 |
|---|---|---:|---:|---:|---|
| 588870.SH | 000688.SH 科创50 | 50 / 50 | 99.999% | 5 | Snapshot/P4A 缓存命中，无网络 |
| 510300.SH | 000300.SH 沪深300 | 300 / 300 | 100.008% | 2 | Snapshot/P4A 缓存命中，无网络 |
| 560010.SH | 000852.SH 中证1000 | 1000 / 1000 | 99.986% | 0 | Snapshot/P4A 缓存命中，无网络 |
| 513120.SH | 931787.CSI 港股创新药 | 42 / 42 | 99.998% | 5 | Snapshot/P4A 缓存命中，无网络 |
| 516010.SH | 930901.CSI 动漫游戏 | 28 / 28 | 99.999% | 5 | Snapshot/P4A 缓存命中，无网络 |

510300.SH 原始权重和约 100.008%；Snapshot 合同中的覆盖率字段封顶为 100%，原始行和原始内容哈希仍保留。

隔离验收库最终包含：

- 5 条 `etf_research_snapshots`。
- 5 条 `etf_module_cache`。
- 25 条 `etf_reuse_audit`。
- 模型调用、输入 Token、输出 Token 均为 0。

### 12.4 API 与运行验证

- `GET /research/etf/{symbol}/universe`：只读 Universe、映射、Provider、Snapshot 和 P4A 状态。
- `GET /research/etf/{symbol}/universe/snapshot`：读取最新快照。
- `POST /research/etf/{symbol}/universe/refresh`：按需刷新并支持事件成分强制入选。
- `POST /research/etf/universe/prewarm`：按当前持仓预热 ETF，默认 cache-first。
- `127.0.0.1:8899/health` 和新 Universe 状态 API 已实测返回 200。
- 本轮没有新增前端页面。

### 12.5 测试终态

- Ruff：通过。
- ETF/P4A/Deep Report/报告库/知识库相关回归：153 passed，5 skipped。
- 显式开启真实外部源测试：21 passed。
- 日常采集和 P4A 路径模型调用、输入 Token、输出 Token：0 / 0 / 0。

### 12.6 真实运行库边界

验收主体使用隔离 SQLite。诊断过程中有一次误用默认 Store，在真实运行库写入：

- 1 条 `588870.SH`、`data_as_of=2026-06-30` 的 passed Universe Snapshot。
- 1 条 deterministic `holding_penetration` 模块结果。
- 3 条 `etf_reuse_audit`。
- 0 条 `etf_analysis_runs`。
- 输入/输出 Token 均为 0。

发现后没有执行删除、清理或继续写入。2026-07-18 使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON` 再次确认真实库仍为上述终态。

### 12.7 下一步边界

1. P4B1 已完成：只复用现有个股研究，模型和 Token 保持为 0，并已输出真实知识覆盖审计。
2. P4B2-A 至 P4B2-E 已完成；下一步仅是在补齐 Evidence 且获得精确授权后执行 P4B2-F，门槛见第 14、15 节。
3. P4B2 只补充 P4A 已选且 missing、stale 或关键 conflicted 的成分研究。
4. P5 周报与前端、P6 正式报告运行观察继续保持未实施。

## 十三、P4B1 实施记录（2026-07-18）

### 13.1 已完成合同与服务

- 新增全局 `ComponentResearchDigest`、ETF 专属 `ETFComponentDigestBinding` 和单次选择 `ComponentDigestResolution` 合同。
- 新增 `ComponentResearchDigestService` 和同库 `ComponentResearchDigestStore`；P4B1 表与现有研究缓存共用 `research_cache.sqlite3`，没有新建事实库。
- 建立七类研究维度、集中新鲜度阈值、精确 section 映射和小型可审计关键词 fallback。
- 只按规范化证券代码查询 `subject_key`/`symbol`，不按名称关联，不自动合并 A/H 股。
- Report、Claim、Fact、Evidence 和 Conflict 全部执行 `analysis_as_of` 截止过滤。
- 完成稳定 Digest/Binding/Resolution ID、幂等持久化、知识指纹失效、进程内 single-flight 和第二次真实缓存命中。
- missing 仅保存零知识状态缓存，不生成摘要；Binding 的 `digest_id` 保持为空。

### 13.2 P4A、Deep Report 与档案接入

- P4A 选择中的权重、评分、边际解释增益、强制入选、涨跌贡献、盈利贡献和选择原因保存到 Binding，不进入全局 Digest。
- `DeepReportService.attach_component_digest_resolution` 只写分析状态和零调用指标，不创建正文、PDF 或 Artifact。
- 统一报告目录 API 新增 Digest、Resolution、确定性重解析和指标入口；标的档案返回稳定 `component_research` 与 `profile.etf.component_research` 结构。
- 空 P4A Selection 只生成空 Resolution，不生成 Binding；事件强制选择只解析实际入选成分。

### 13.3 真实审计结果

- 审计文件：`P4B1_COMPONENT_RESEARCH_AUDIT_2026-07-18.md`。
- 513120.SH 使用中证指数官方 2026-06-30 Snapshot，42 / 42 成分、99.998% 权重覆盖；P4A 入选五只均规范为 `.HK` 代码。
- 首批 17 只代表性成分当前均没有按规范代码进入统一报告目录的个股研究：`missing=17`，其余状态均为 0。
- 588870.SH、510300.SH、516010.SH、513120.SH 和空选择 560010.SH 的相同输入第二次均命中 Resolution 缓存；17 个成分状态也命中 Digest 缓存。
- 真实集合没有可复用研究，实际跨 ETF 共享 Digest 数和估算避免模型调用数均为 0；合成专项测试已验证两只 ETF 共享 1 个 Digest、保留 2 个不同 Binding。
- 审计使用真实库只读连接和临时副本；审计窗口前后真实库大小、修改时间和 P4B1 表集合不变。

### 13.4 验证终态

- P4B1 专项：14 passed。
- P0–P4A、ETF Deep Research、个股 Deep Research、Deep Report API、报告目录、知识库和设置 API：与专项合并运行 118 passed，6 skipped。
- 报告工具、PDF、审计、预览、Session 消息和报告监控补充回归：73 passed。
- P4B1 改动文件的 Ruff：通过。
- 全 `agent` Ruff 仍有 294 个既有问题，分布在历史 backtest、CLI、skills、channel 和旧测试文件；本轮未越界改写这些无关文件。
- P4B1 运行与测试的 `model_calls`、`input_tokens`、`output_tokens` 均为 0。

### 13.5 P4B2 方案基线（已由第 15 节实施记录取代）

P4B2 的生成前准备、预算、授权门控和首批试运行方案已经固化在 `P4B2_CONTROLLED_RESEARCH_GENERATION_PROMPT.md`。随后已完成代码、测试和只读 dry-run，见第 15 节；真实试运行仍未授权，也没有生产知识写入。

## 十四、P4B2 执行计划与生成前准备（2026-07-18）

### 14.1 当前起点

- P4A 当前真实入选总数为 17：588870 为 5、510300 为 2、560010 为 0、513120 为 5、516010 为 5。
- P4B1 真实知识覆盖审计结果为 `missing=17`，其他状态均为 0。
- 当前真实集合没有成分重叠，也没有可复用 Digest，因此全量首次生成最多会形成 17 个新研究任务，不能直接启动。
- 当前只读复核发现真实运行库已有空的 P4B1 表，但没有生产 Resolution/Digest/Binding；P4B2 表仍未初始化。P4B2 dry-run 继续只在临时副本写入。

### 14.2 正确执行链路

```text
P4A Selection
  -> P4B1 Resolution
  -> P4B2 Dry-run Plan
  -> 生成前 Preflight 与 Evidence Pack
  -> 授权、预算、幂等和 single-flight 门控
  -> bounded Component Research
  -> 结构化质量检查
  -> 统一 Report / Claim / Fact / Evidence 发布
  -> P4B1 重新解析
  -> reusable / partial_reusable Digest
```

P4B2 不得直接写 Canonical Digest。模型生成内容必须先通过统一知识发布，再由 P4B1 确定性解析。

### 14.3 生成前准备

任何真实模型调用前必须完成：

1. 检查 dirty worktree、服务健康和并发任务。
2. 使用 SQLite 只读模式记录真实研究库表、记录数、大小和修改时间。
3. 确认 Universe Snapshot、P4A Selection 和 P4B1 Resolution 仍然有效。
4. 重新检查候选没有变为 reusable，也没有被其他 ETF 或任务生成。
5. 为每个候选冻结有来源、有截止时间的 Evidence Pack。
6. 排除晚于 `analysis_as_of` 的报告、公告、财报、行情和 Evidence。
7. 检查证券代码，禁止名称匹配和无可靠映射的 A/H 合并。
8. 输出只读 dry-run：候选、P4A 理由、P4B1 状态、Evidence 质量、预计调用和 Token。
9. 确认功能开关、用户授权、白名单和剩余预算。
10. 获得试运行授权后，先创建真实 SQLite 一致性备份，再通过正式迁移初始化表。

任一步骤失败都必须停止，不得静默放宽标准。

### 14.4 首版硬预算

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

功能开关和真实运行开关默认均为关闭。预算必须集中配置，并以实际模型计量结算。

### 14.5 首批试运行范围

首批只考虑 588870.SH 的三只 P4A 强制候选：

- `688256.SH` 寒武纪。
- `688041.SH` 海光信息。
- `688981.SH` 中芯国际。

首批上限：3 次模型调用、18,000 输入 Token、1,800 输出 Token、0 次自动修复。不得自动扩大到澜起科技、中微公司或其他 ETF 成分。

当前主计划只授权制定方案和后续实现；**尚未授权真实试运行**。没有明确授权语句时，P4B2 任务必须在 dry-run 完成后停止。

### 14.6 实施阶段

- P4B2-A：合同、Policy、功能开关、稳定 ID 和预算台账。
- P4B2-B：Preflight、Evidence Pack、候选排序和 dry-run。
- P4B2-C：bounded 结构化生成器、模型计量和质量门控。
- P4B2-D：统一知识发布、事务恢复和 P4B1 回流。
- P4B2-E：专项测试、相关回归和真实候选只读 dry-run。
- P4B2-F：仅在明确授权后执行 588870 三只成分试运行。

### 14.7 计划产物

- 实施要求：`P4B2_CONTROLLED_RESEARCH_GENERATION_PROMPT.md`。
- dry-run 记录：`P4B2_GENERATION_DRY_RUN_YYYY-MM-DD.md`。
- 授权试运行记录：`P4B2_PILOT_VALIDATION_YYYY-MM-DD.md`。

只有首批试运行完成、P4B1 重解析得到 reusable/partial_reusable、第二次请求模型调用为 0 且预算未超限后，才允许讨论扩大范围。

## 十五、P4B2 实施与只读 Dry-run 记录（2026-07-18）

### 15.1 已完成实现

- 新增 `ComponentResearchGenerationPolicy`、`ComponentResearchEvidencePack`、`ComponentResearchGenerationJob`、`ComponentResearchGenerationPlan`、`ComponentResearchPublishResult` 和机器可读 Preflight 合同。
- 新增默认关闭的 `ETF_COMPONENT_RESEARCH_GENERATION_ENABLED` 与 `ETF_COMPONENT_RESEARCH_LIVE_RUN_ENABLED`，并将单成分、单 ETF、单日模型调用和 Token 上限集中到设置 Policy；设置 API 只能在首版审计硬上限内修改。
- 新增 P4B2 Evidence Pack、Plan、Job、Audit、Budget Ledger 和 Publish Result 表的幂等初始化代码；无授权 dry-run 不在真实库建表。
- 实现精确 P4A/P4B1 候选过滤、未来数据排除、冻结 Evidence 指纹、结构化输出校验、Evidence ID 白名单、估值行情门控、0 次自动修复和实际 provider Token 计量要求。
- 实现全局证券 single-flight、数据库部分唯一索引、`BEGIN IMMEDIATE` 原子日预算预留和发布幂等键。
- 实现无 Artifact 的 `component_research` 统一 Report、Claim/Evidence 关系事务化发布；Fact 只引用既有结构化 Fact，不把模型摘要升级为 Fact；发布后由 P4B1 确定性重解析。
- 新增精确范围 Plan、Preflight、执行、Job、取消、当日预算和最近发布结果 API；没有“研究全部 missing”的无界入口。
- 离线测试验证成功发布后得到 `partial_reusable` Digest、第二次相同执行模型调用为 0、事务失败不留下半完成关系、并发预算不超限、560010 空选择为 0 Job。

### 15.2 真实只读 Dry-run

记录文件：`P4B2_GENERATION_DRY_RUN_2026-07-18.md`。

- 当前 17 只代表性成分重新解析仍为 `missing=17`，其余状态均为 0。
- 首批精确候选仍限定为 `688256.SH`、`688041.SH`、`688981.SH`，没有扩大到澜起科技、中微公司或其他 ETF。
- 三只冻结 Evidence Pack 均为 `insufficient`：统一知识中 Source、Fact、Evidence、Claim 均为 0，缺主营身份、最近财务和风险/反向证据。
- 因 Evidence 门控失败，实际可执行 Job 为 0，预计调用和 Token 为 0；理论授权批次硬上限仍为 3 / 18,000 / 1,800。
- 真实库只读窗口前后大小和 mtime 完全不变；P4B2 表、预算、Report/Claim/Fact/Evidence 均未写入。
- 服务健康；真实 P4A Selection 当前有效。真实库已有空 P4B1 表但没有当前 Resolution，P4B2 表不存在。
- 当前没有精确试运行授权，因此没有备份、迁移、真实模型调用或生产发布。

### 15.3 仍未完成

- P4B2-F 588870 三成分真实试运行：已授权并执行生产备份、初始化、Preflight 和精确 Plan 入口，但 Evidence Pack 前置质量不通过，未发生模型调用或知识发布。
- “将新摘要、选择原因、解释覆盖率和来源写入正式 ETF 报告”仍未勾选：当前只完成 Component Research 统一知识记录和 P4B1/标的档案回流，没有创建新的正式 ETF Artifact。
- 在补齐官方/一手 Source、Evidence 和结构化 Fact，并重新生成 complete Evidence Pack 前，不具备完成本批生成或扩大到其他 ETF 的条件。

## 十六、P4B2-F 授权试运行结果（2026-07-18）

验证记录：`P4B2_PILOT_VALIDATION_2026-07-18.md`。

### 16.1 已真实完成

- 已收到并通过机器校验 Prompt 要求的精确三成分授权。
- 已协调 SQLite 写锁并在迁移前创建一致性生产备份；备份路径、大小和 SHA-256 已记录。
- 已通过正式初始化代码创建并复核 P4B1/P4B2 schema，重复初始化幂等。
- 已在生产库生成当前 P4B1 Resolution、三只冻结 Evidence Pack、授权 live Plan 和三个 Job。
- 已调用精确 Plan 执行入口，并再次执行相同请求验证边界。
- 功能开关仅在本次受控进程内开启；持久开关执行后仍为关闭。

### 16.2 门控结果

- Plan `p4b2plan_716d6194e99ac7961e1655e5` 为 `dry_run=false`、`authorized=true`。
- 三只均为 P4A 已选、P4B1 `missing`，但 Evidence Pack 的 Source、Fact、Evidence、Claim 均为 0。
- 三个 Job 均以 `evidence_pack_quality:insufficient` 阻断，`planned_count=0`。
- 实际模型调用、输入 Token、输出 Token、预算台账和 Publish Result 均为 0。
- 没有生成统一 `component_research` Report/Claim，也没有 reusable/partial_reusable P4B1 回流。
- 没有处理其他成分或 ETF，没有 PDF、正式个股报告、监控或交易变化。

### 16.3 当前状态

P4B2-F 已完成授权后的生产备份、初始化、Preflight、计划和执行入口验证，但尚未通过“真实模型生成与知识发布”验收，状态为 `authorized_but_blocked_before_model`。P4B2-F 继续保持未完成，不扩大到其他 ETF。

下一步不是放大 Token，而是先从受控官方来源补齐三只成分的身份、近期财务和风险/反向 Evidence 与结构化 Fact。补证后重新冻结 Evidence Pack、生成新 Plan，并在新的执行窗口内复核授权、预算和数据截止时间。

## 十七、P4B2-G 官方证据源只读审计（2026-07-18）

审计记录：`P4B2_EVIDENCE_SOURCE_AUDIT_2026-07-18.md`。  
机器清单：`P4B2_EVIDENCE_MANIFEST_2026-07-18.json`。

### 17.1 已完成

- 严格限定 `588870.SH` 的 `688256.SH`、`688041.SH`、`688981.SH`，没有扩大标的。
- 为每只成分核验一份 2025 年年度报告和一份 2026 年第一季度报告，共 6 份上交所法定披露；证券代码、名称、披露时间、页数、文件大小、SHA-256 和关键页码已完成文本与目视双重校验。
- 年报覆盖主营业务、风险和治理，一季报覆盖 2026Q1 财务及经营变化；一季报未经审计属性和中芯国际“千元人民币”原始单位已显式保留。
- 建议每只建立 5 条短 Evidence 和 3 条结构化 Fact，估值继续排除；二季度指引等前瞻信息不得升级为历史 Fact。
- 在生产库临时副本中使用正式知识写入合同、Evidence Pack Builder 和模型 Payload 完成仿真：三只均为 `complete`，核心覆盖率均为 1.0、冲突为 0，仅缺 valuation。
- 仿真实际全批输入保守上界为 14,937，低于既有 18,000 上限；单只分别为 4,975、4,970、4,992，均低于 6,000，因此无需放大 Token。

### 17.2 执行边界

- 本阶段模型调用、输入 Token、输出 Token均为 0。
- 生产库新增 SourceDocument、Evidence、Fact、`component_research` Report 和 Publish Result 均为 0；三只证券的 Evidence/Fact 计数保持为 0。
- P4B2 生产状态保持为 Evidence Pack 3、Plan 1、blocked Job 3、Budget Ledger 0、Publish Result 0。
- 运行服务在审计窗口内存在其他数据库活动，因此只确认本任务范围没有写入，不把整个数据库描述为全程只读。

### 17.3 下一授权门槛

下一阶段是受控生产证据入库，不是模型生成。该阶段需要新的明确授权，范围应锁定为清单中的 6 个 SourceDocument、15 条 Evidence 和 9 条 Fact；先创建生产一致性备份，写入后重建三只 Evidence Pack，并在保持模型调用为 0 的条件下返回新 Plan 和精确 Token 预算。只有生产 Pack 均达到 `complete`，才进入下一次模型执行授权判断。

## 十八、P4B2-G 受控生产证据入库（2026-07-18）

验证记录：`P4B2_EVIDENCE_INGEST_VALIDATION_2026-07-18.md`。  
机器结果：`P4B2_EVIDENCE_INGEST_RESULT_2026-07-18.json`。

### 18.1 已完成

- 写入前创建并校验生产 SQLite 一致性备份，备份和生产库 `integrity_check` 均为 `ok`。
- 严格按清单为三只成分新增 6 个上交所 SourceDocument、15 条 Evidence 和 9 条 Fact；新冲突为 0，没有扩大到 P4A 选择中的另外两只成分。
- 知识正文只保存核验过的指定 PDF 页面，并显式保存官方 URL、PDF SHA-256、披露时间和页码；一季报未经审计属性、中芯国际原始千元单位及前瞻指引边界均得到保留。
- 三只生产 Evidence Pack 均从 `insufficient` 变为 `complete`，核心覆盖率均为 1.0，仅保留 valuation 缺口。
- 新建未授权 dry-run Plan `p4b2plan_478711f7012eb0b07eecd1ea`：三只 Job 均为 `planned`、无 Evidence 阻断，输入上界分别为 5,752、5,748、5,707，全批为 17,207，低于 18,000。

### 18.2 执行边界

- 实际模型调用、输入 Token、输出 Token均为 0。
- Budget Ledger、Publish Result 和 `component_research` Report 均为 0。
- 持久生成开关和 live-run 开关仍为关闭。
- 新 Plan 为 `dry_run=true`、`authorized=false`，不能直接进入模型执行。
- P4B1 仍为原有 missing 状态；本阶段完成的是 P4B2 Evidence 门控，不把 Evidence/Facts 冒充为已发布报告 Claim。

### 18.3 下一授权门槛

真实模型生成需要新的明确授权和新 live Plan。既有 18,000 输入上限仍足够，余量 793；1,800 输出上限被三只各 600 完全占满。下一次授权应明确是否继续沿用该输出硬上限，仍须保持最多 3 次模型调用、0 次自动修复和精确三成分范围。

## 十九、P4B2-F 提高输出限额后的真实试运行（2026-07-19）

验证记录：[P4B2_LIVE_GENERATION_ATTEMPT_2026-07-19.md](P4B2_LIVE_GENERATION_ATTEMPT_2026-07-19.md)。  
机器结果：[P4B2_LIVE_GENERATION_ATTEMPT_2026-07-19.json](P4B2_LIVE_GENERATION_ATTEMPT_2026-07-19.json)。

### 19.1 已执行

- 精确授权输出上限提高为单成分 1,000、全批 3,000；输入上限维持单成分 6,000、全批 18,000，最多 3 次调用、0 自动修复、仅三只标的。
- Preflight 通过，新 live Plan `p4b2plan_71d5eddbdeaf767a9e93e702` 预计 3 次调用、17,207 输入、3,000 输出。
- 第一只 `688256.SH` 发起 1 次提供方请求；OpenAI Codex 内部端点在生成前以 HTTP 400 拒绝不支持的 `max_output_tokens` 参数，实际输入/输出计量均为 0。
- 系统立即停止且没有自动重试；`688041.SH`、`688981.SH` Job 均已取消。预算台账为 1 次请求、0/0 tokens；Publish Result 和 `component_research` Report 均为 0。
- 持久生成与 live-run 开关保持关闭，生产库完整性为 `ok`，备份 SHA-256 已复核一致。

### 19.2 边界与修正

- 当前 Codex 内部端点不能提供服务端输出 token 硬上限。适配器已停止发送不支持字段并保留调用后计量校验，但继续使用 Codex 只能形成提示词与事后门控。
- 首次请求前的 P4B1 重建曾确定性处理完整五条 P4A Binding，因此写入了含 `688008.SH`、`688012.SH` 的新 missing Resolution；二者没有模型调用、报告或生成知识。执行路径已修正为按 Plan 授权范围过滤，并新增回归测试。
- 相关测试为 43 passed，`git diff --check` 通过。

### 19.3 当前结论

P4B2-F 状态为 `attempted_but_halted_before_generation`，真实试运行仍未完成。原批次已有 1 次请求计数，不能在“最多 3 次”边界内重新覆盖三只，因此不自动继续。下一步必须在以下边界中获得新的明确选择和授权：改用支持服务端输出上限的提供方并授权 3 次新调用，或明确接受 Codex 仅提示词与事后计量门控后授权 3 次新调用；两者都必须继续锁定精确三只、18,000/3,000 批次 Token 和 0 次自动修复。

## 二十、P4B2 Codex 客户端软限制试运行（2026-07-19）

验证记录：[P4B2_CODEX_SOFT_LIMIT_ATTEMPT_2026-07-19.md](P4B2_CODEX_SOFT_LIMIT_ATTEMPT_2026-07-19.md)。  
机器结果：[P4B2_CODEX_SOFT_LIMIT_ATTEMPT_2026-07-19.json](P4B2_CODEX_SOFT_LIMIT_ATTEMPT_2026-07-19.json)。

- 已按新授权继续使用 Codex，且请求体不再发送 `max_output_tokens`；新备份和 Preflight 均通过。
- `688256.SH`、`688041.SH` 分别实际使用 1,893/1,119 和 1,867/1,323 输入/输出 tokens，均因超过单只 1,000 输出软限制而拒收，没有发布。
- 累计输出为 2,442；`688981.SH` 的 1,000 预留会超过全批 3,000，故未调用并已取消。
- 本批合计 2 次真实生成调用、3,760 输入、2,442 输出、0 发布、0 自动修复；精确三只 P4B1 状态仍为 missing。
- 若同日重新完整覆盖三只，至少需要新的三次调用授权、单只/全批客户端接受上限 1,600/4,800，以及将单日调用硬上限从 5 提高到 6；没有新授权不得继续。

## 二十一、P4B2-F 功能优先真实试运行完成（2026-07-19）

验证记录：[P4B2_FEATURE_FIRST_PILOT_VALIDATION_2026-07-19.md](P4B2_FEATURE_FIRST_PILOT_VALIDATION_2026-07-19.md)。  
机器结果：[P4B2_FEATURE_FIRST_PILOT_VALIDATION_2026-07-19.json](P4B2_FEATURE_FIRST_PILOT_VALIDATION_2026-07-19.json)。

- 功能优先授权解除本批的成分数、模型调用次数和输出 Token 阻断，同时保留精确三只、输入预算、Evidence、结构化、时间和事务安全门控。
- live Plan `p4b2plan_83804aef10b2067a306de9d1` 为 `completed`，三个 Job 均为 `published`。
- 本批实际 3 次模型调用、5,617 输入、3,994 输出，生成 3 个 Publish Result 和 3 份统一 `component_research` Report。
- 每份报告关联 5 Evidence、3 Fact、7 Claim；共 21 Claim，全部有 Evidence 引用。
- 三只 P4B1 Digest 均为 `partial_reusable`；第二次相同执行为 0 模型调用并返回相同发布结果。
- Report Library、Plan 和 Digest HTTP API 已在重启后的 8899 服务上验收：报告数 3、Plan `completed`、Job 全部 `published`、Digest 全部 `partial_reusable`。
- 修复未来截止时间 Digest 遮蔽当前结果、Plan 顶层状态不收敛和内嵌 Job 状态陈旧三个读取缺陷；相关回归 61 passed。
- 持久 P4B2 开关仍为关闭，未扩大标的，未生成 PDF，未修改监控或交易状态。

P4B2-F 已完成。下一步是将这些 Digest 的摘要、P4A 选择原因、解释覆盖率和来源写入正式 ETF 报告；该项仍保持未完成，进入后续 P5/正式报告集成阶段。
