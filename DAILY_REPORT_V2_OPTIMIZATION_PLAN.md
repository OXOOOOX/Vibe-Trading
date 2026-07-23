# 日报生产链 v2 优化计划

> 状态：实施中（分析方法层与 Agent 契约已落地，其余阶段待继续）  
> 制定日期：2026-07-20  
> 前置条件：Deep Research、正式 Report、统一报告目录和 `weekly_review_v2` 已完成黄金样本验收  
> 适用范围：个股晨报、ETF 晨报、组合晨会、日报登记、Markdown/PDF 和监控候选

## 一、目标

本计划不重做现有组合晨会，而是在现有 `DailyPortfolioRunService` 之上统一日报的数据、证据和交付契约：

- 日报只消费当日截止时间前冻结的数据和合格报告 Claim。
- 个股晨报、ETF 晨报和组合晨会共享同一结构化上下文，不解析历史 Markdown。
- 数据不足时在模型调用和 PDF 生成前停止或局部降级，不用模型猜测缺失数字。
- 用户版 Markdown/PDF 统一使用中央中文术语，机器 JSON 保持稳定英文键。
- 日报、监控候选、消息发送和交易执行继续解耦。

## 二、当前基线与主要改进点

现有日报链路已经具备：

- 组合与 Mandate 冻结、幂等运行、重试和取消；
- `ensure_fresh | force | reuse` 刷新策略；
- 核心数据不足时在模型分析和 PDF 前停止；
- 个股附录、组合综合报告和 PDF 产物；
- 非交易日使用上一已收盘交易日量价并明确说明；
- 监控候选人工确认、`trade_execution=forbidden`；
- ETF 产品资料接入和“ETF晨报”命名。

仍需统一的部分：

1. Daily Run 记录、Holding Brief、组合决策和 Artifact 仍存在多个版本与状态口径。
2. 日报尚无与周报对等的 `DailyContextAssembler`，报告目录 Claim、昨日成功日报和结构报告的选择逻辑不够集中。
3. 日报自身的中文枚举表与正式 Report 中央术语层尚未完全合并。
4. PDF 生成、可下载状态、文件命名和逐页视觉门禁需要统一为正式交付契约。
5. ETF 的产品、指数、份额、折溢价、成分与组件研究范围需要拆开登记，不能用一个总缺口覆盖。
6. 固定交易日窗口只能作为滚动边界基准，需要多周期结构、摆动点、波动归一化、触及反应和 Agent 反证审查共同形成研究结论。

## 三、`DailyContextAssembler`

新增只读取结构化数据的确定性装配器，输出：

```text
schema_version
market_date
report_cutoff
price_basis
portfolio_snapshot
mandate_snapshot
market_scopes
news_context
previous_daily
structural_context
etf_context
reusable_claims
pending_verification
source_manifest
excluded_items
context_fingerprint
```

### 3.1 截止时间规则

- `market_date` 是报告归属交易日，不等于所有数据都发生在该日。
- 盘前、休市日和非交易日的量价必须使用最近已收盘交易日，并在 `price_basis` 中登记。
- 新闻可以使用报告生成时的最新合格资料，但必须分别记录事件时间、发布时间和获取时间。
- 历史日报的 `market_date` 不得晚于当前日报归属日。
- 市场 Fact 的 `data_as_of` 不得晚于报告截止时间。
- 结构报告晚于日报截止时间时必须排除并记录 `future_report_data`。
- 装配器不得读取或解析历史 Markdown/PDF。

### 3.2 报告复用规则

- 只复用 `verified`、`triangulated` Claim。
- `weak`、`conflicted` Claim 只进入“待验证事项”，不得进入今日方向结论。
- `insufficient` 只生成缺口。
- Deep Research 提供业务结构、风险、观察对象和中长期失效条件，不覆盖日报自行计算的当日量价。
- 昨日成功日报只提供变化基线，不把旧动作机械复制到今日。
- 所有复用 Claim 保留 `claim_id`、Fact/Evidence 血缘、数据时点和有效期。

## 四、日报数据契约 v2

### 4.1 Daily Run

将运行记录升级为 `schema_version=2`，新增：

```text
context_fingerprint
source_manifest_id
market_session_basis
single_source_authorized
model_calls
model_cost
materialization_status
catalog_status
side_effects
```

保留现有幂等键，并增加冻结上下文指纹；同一输入重复触发继续复用，只有 `force_new` 或输入指纹变化才创建修订。

### 4.2 Holding Daily Brief

将持仓日报升级为统一结构：

```text
schema_version
run_id
report_family_id
symbol
security_name
instrument_type
market_date
data_as_of
price_basis
quality_status
coverage_status
data_scopes
change_set
cross_horizon_context
portfolio_context
daily_view
monitoring_claims
risks
data_gaps
source_manifest
context_fingerprint
side_effects
```

约束：

- `daily_view` 与 `intraday`、`weekly`、`structural` 视角分离。
- 动作、置信度和优先级继续使用稳定机器枚举，中文只在用户视图转换。
- 任何价格、比例、金额和日期必须能够回放到冻结 Fact 或确定性计算。
- 报告中出现数据缺口时，只降低对应范围，不默认把整份日报判为不可用。

### 4.3 ETF 数据范围

ETF 晨报至少拆分：

- 产品身份；
- 跟踪指数；
- 指数相对强弱；
- 基金份额及变化；
- 折溢价；
- 跟踪误差或基金份额参考净值；
- 成分暴露；
- 成分研究覆盖；
- 同指数产品组资金流代理。

产品正式名称优先使用官方档案；持仓别名只作搜索别名。某一范围缺失不得把其他范围标记为缺失。

## 五、刷新、单源授权与分析门禁

### 5.1 默认刷新策略

- `ensure_fresh` 默认先执行一次自动补刷新，再判断数据不足。
- `reuse` 只读冻结缓存并如实标记时点，不伪装为实时数据。
- `force` 创建新数据批次和新修订，不覆盖已发送 Artifact。
- 同一运行内所有 Holding Worker 只读统一 `data_manifest`，不得重复刷新同一标的。

### 5.2 单源数据

- 单源数据不得静默降级为已验证。
- 用户必须通过显式 `single_source_authorized=true` 授权使用。
- 用户版报告显示“单一来源参考”警告、来源名称和时间。
- 未授权时，相关价格型监控候选保持 `watch_only` 或停止生成。

### 5.3 模型边界

- 先完成身份、行情、来源和数字门禁，再启动 Holding 分析 Session。
- 模型只能解释冻结上下文，不能新增未登记数字、替换官方名称或补写缺失 ETF 范围。
- 每个运行记录真实 `model_calls`、输入输出消耗和失败持仓。
- 弱数据运行必须在模型和 PDF 前停止；局部缺口允许其他持仓或数据范围继续。

### 5.4 分析方法注册表与 Agent 反证

- [x] 建立版本化 `market-analysis-methods/1.0`，统一市场状态、多周期结构、已确认摆动点、波动归一化和触及反应证据。
- [x] 支撑阻力改为带方法、截止日、证据评分、失效条件和价格口径的候选区间，不再把单一窗口极值直接称为正式结论。
- [x] Holding Worker 只允许选择已登记方法和候选编号；Agent 文字不得新增数字、日期、价格、比例或区间。
- [x] Agent 必须同时输出支持证据、反对证据、失效条件和反证审查结果。
- [x] 前复权等非原始价格候选只可用于结构分析，不得直接映射为原始价格监控点位。
- [x] ETF 增加指数相对表现、跟踪误差、份额、折溢价和成分贡献的专用范围要求。
- [ ] 完成日报 v2 黄金运行后，按事件变化阈值决定是否启动 Agent，避免无变化日报重复消耗模型。

## 六、用户版 Markdown/PDF

### 6.1 中央中文术语层

日报复用 `reader_terms.py`，合并现有日报枚举表，至少覆盖：

- 动作、优先级和置信度；
- 数据范围与来源状态；
- ETF 产品与指数指标；
- 组合分区和预算状态；
- 监控条件与自动化准备状态；
- 刷新、缓存、单源授权和缺口原因。

未登记机器术语触发用户版编译失败，不采用英文下划线兜底。

### 6.2 文件与显示名称

- 个股：`YYYY-MM-DD_代码_名称_个股晨报.md/.pdf`。
- ETF：`YYYY-MM-DD_代码_名称_ETF晨报.md/.pdf`。
- 组合：`YYYY-MM-DD_组合晨会.md/.pdf`。
- 页面标题、PDF 文件名和界面下载标签都必须同时显示日期、代码和名称。

### 6.3 PDF 交付

统一使用：

```text
materialization_status:
  generatable
  materialized
  failed
```

要求：

- 普通点击预览，只有下载图标触发下载。
- 预览、下载或飞书发送前强制物化 PDF。
- PDF 标题不重复；页脚显示标的、报告日期、版本和页码。
- 内部长 ID、完整审计信息和机器枚举只保留在 JSON。
- 所有正式 PDF 渲染全部页面，检查截断、重叠、黑块、乱码和异常空白。

## 七、统一报告目录与版本比较

- 个股日报与 ETF 晨报按 `report_family_id` 登记修订关系。
- `current.daily.latest` 与 `latest_complete` 分开保留。
- 默认时间线只显示家族当前版本，显式历史模式展示全部修订。
- 日报登记 Claim 的支撑等级、Fact/Evidence 血缘、数据时点和有效期。
- `change_set` 只展示用户能理解的事实和结论变化；内部字段差异留在 JSON。
- 同日强制修订不得把旧 Artifact 原地覆盖，已发送文件标记 `superseded`。

## 八、监控与副作用边界

- 日报可以生成结构化监控候选，但不自动激活。
- `activation_policy=manual_confirmation_required`。
- `trade_execution=forbidden`。
- 初始化、页面水合和轮询只读；只有用户显式操作可以启用或关闭监控。
- 停止监控保留可恢复历史，不删除事件与计划。
- 测试飞书交付只验证卡片和文件，不向真实外部会话发送。

## 九、黄金样本与验收

### 9.1 000651.SZ 格力电器

- [ ] 官方名称、上一交易日量价和报告日期正确。
- [ ] 复用结构报告时不读取 Markdown，且未来结构数据被排除。
- [ ] 核心财务或结构 Claim 只来自合格支撑等级。
- [ ] 非交易日报告明确使用上一已收盘交易日量价。
- [ ] Markdown/PDF 无英文财务机器键和内部长 ID。

### 9.2 588870.SH 科创50ETF汇添富

- [ ] 正式名称和“ETF晨报”文件名正确。
- [ ] 产品、指数、份额、折溢价、成分、组件研究分别显示覆盖状态。
- [ ] 5 个成分选择与 3 个部分复用、2 个缺失保持一致。
- [ ] 相对强弱或跟踪误差缺失只降低对应范围。
- [ ] 用户版产品指标和计算口径全部中文化。

### 9.3 自动化门禁

- [ ] 截止时间和未来数据拒绝测试。
- [ ] 刷新优先与单源显式授权测试。
- [ ] 弱数据在模型/PDF 前停止测试。
- [ ] 报告目录结构化 Claim 复用测试。
- [ ] 旧日报只读兼容与修订家族测试。
- [ ] 中央中文术语扫描。
- [ ] PDF 命名、物化、标题、页脚和逐页渲染测试。
- [ ] 非交易日价格口径测试。
- [ ] 监控、发送和交易副作用为零测试。

## 十、实施顺序

### P0：日报上下文与截止时间

- [ ] 实现 `DailyContextAssembler`。
- [ ] 冻结 `source_manifest` 和 `context_fingerprint`。
- [ ] 接入报告目录结构化 Claim，禁止解析历史 Markdown。
- [ ] 完成交易日、盘前、休市日和新闻时点门禁。

### P1：契约和数据范围

- [ ] 升级 Daily Run 与 Holding Brief v2。
- [ ] 拆分 ETF 数据范围并统一官方名称。
- [ ] 记录真实模型调用和副作用计数。
- [ ] 为旧版运行与 Artifact 提供只读适配器。

### P2：用户输出与 PDF

- [ ] 日报接入中央中文术语层。
- [ ] 统一个股晨报、ETF 晨报和组合晨会命名。
- [ ] 接入 PDF 物化状态、页脚和用户版内部 ID 隐藏。
- [ ] 增加逐页 PDF 视觉门禁。

### P3：目录、比较与监控候选

- [ ] 登记日报家族、Claim 支撑等级和证据血缘。
- [ ] 实现可读 `change_set` 和默认压缩时间线。
- [ ] 对齐结构化监控候选，保持人工启用和禁止自动交易。
- [ ] 验证 Feishu 交付但不发送到真实外部会话。

### P4：黄金运行与切换

- [ ] 生成 000651.SZ 与 588870.SH 黄金日报。
- [ ] 验收 JSON、Markdown、PDF、目录、版本比较和监控候选。
- [ ] 与现有日报同日影子对照。
- [ ] 验收通过后将 v2 设为默认，保留 v1 历史读取。

## 十一、完成定义

只有同时满足以下条件，本计划才能标记为完成：

- 000651.SZ 和 588870.SH 黄金日报通过结构化与逐页 PDF 验收；
- 日报不读取历史 Markdown，不使用未来市场或报告数据；
- 弱数据在模型和 PDF 前停止，局部缺口不扩大为整份失败；
- 用户版 Markdown/PDF 无未登记英文机器术语和内部长 ID；
- ETF 名称、数据范围和成分研究覆盖与正式结构报告一致；
- 非交易日量价使用上一已收盘交易日，新闻保持当前且分别标时；
- 报告目录可输出带支撑等级和证据血缘的日报 Claim；
- PDF 可预览、可下载、物化状态正确且逐页无视觉缺陷；
- 监控激活、外部发送和交易执行保持显式人工边界；
- 所有相关既有测试和新增门禁通过，旧日报继续可读和可追溯。
