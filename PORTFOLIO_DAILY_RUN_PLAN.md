# 每日组合晨会升级设计

状态：第一阶段开发闭环完成，进入用户验收；第二阶段 M7 尚未开始  
范围：飞书一键触发、全持仓刷新、个股日报、分区审查、组合决策、PDF 交付  
第一期：手动触发；稳定后增加交易日 09:12 自动触发

## 1. 已确认的产品决策

1. 现金先由用户手工维护，继续保存为组合事实数据。
2. Agent 可以为未分类持仓给出并采用初始分区；用户可以编辑最终分类。
3. 用户编辑过的分类具有最高优先级，Agent 后续只能给出改区建议，不能静默覆盖。
4. 分区目标同时支持目标金额、下限和上限，第一版以金额为主要口径。
5. 每个持仓每天都会生成独立的 `daily_update` 报告并自动渲染 PDF；重大变化或用户主动要求时再生成 `deep_research` 深度报告。
6. 系统默认只向飞书发送一份综合 PDF；综合 PDF 包含个股附录。
7. 每只个股的独立 PDF 继续保留。用户在飞书选择查看时，直接发送已生成文件，不重新分析。
8. 第一阶段只提供一键手动晨会；稳定后增加交易日 09:12 自动运行。
9. 用户界面和报告文案不讨论程序是否具有下单能力；运行时继续只装配研究所需工具。
10. 第一版按中国市场交易日处理 A 股和场内 ETF；跨市场组合与多市场日历放到后续版本。

## 2. 核心架构决策

新增 `DailyPortfolioRun`，作为一次完整组合晨会的控制对象。

- 飞书：触发、展示进度、接收报告、发起后续追问。
- DailyPortfolioRun：冻结输入、调度各阶段、重试、取消、幂等、汇总结果。
- Session：承载后续对话，不作为跨阶段的事实来源。
- Worker/Subagent：处理单个持仓、单个分区或组合裁决。
- Artifact：作为阶段间的正式交接物，包括 JSON、Markdown 和 PDF。

每日运行使用动态任务图，不直接套用固定持仓数量的静态 swarm preset：

```text
mandate_preflight                       为新持仓建立初始分区并落盘
  -> snapshot                           冻结 PortfolioState 和有效 Mandate
  -> refresh_data
  -> holding_report[symbol] x N       并发 3~4
  -> sleeve_review[sleeve_id] x M
  -> portfolio_decision
  -> render_individual_pdfs
  -> render_master_pdf
  -> deliver_feishu
```

现有 `SessionDispatcher` 继续负责单 Session 排队。新增的 `DailyPortfolioRunService` 负责多阶段 DAG；它可以复用 Session 执行能力，但不能把 Daily Run 简化成一条超长 Session 消息。

## 3. 数据边界

### 3.1 事实层：PortfolioState

继续使用现有 `portfolio_state.json`，只保存客观事实：

- holdings
- recent_trades
- cash
- cash_currency
- updated_at

组合分区和目标金额不写入 PortfolioState，避免事实数据与策略目标相互污染。

### 3.2 策略层：PortfolioMandate

新增文件：

```text
~/.vibe-trading/portfolio/portfolio_mandate.json
```

建议结构：

```json
{
  "schema_version": 1,
  "version": 3,
  "suggestion_revision": 7,
  "base_currency": "CNY",
  "classification_policy": {
    "version": 1,
    "auto_assign_new": true,
    "reclassification_confirmations": 2,
    "min_confidence": 0.75,
    "apply_reclassification_next_run": true,
    "dimensions": [
      "volatility",
      "drawdown",
      "earnings_stability",
      "cyclicality",
      "growth_and_catalyst",
      "cashflow_and_dividend",
      "portfolio_role"
    ]
  },
  "cash_policy": {
    "target_amount": 0,
    "min_amount": 0,
    "max_amount": null
  },
  "sleeves": [
    {
      "id": "offensive",
      "name": "进攻型",
      "parent_id": null,
      "target_amount": 0,
      "min_amount": 0,
      "max_amount": null,
      "rebalance_band_amount": 0,
      "single_position_max_amount": null,
      "sort_order": 10
    },
    {
      "id": "defensive",
      "name": "防守型",
      "parent_id": null,
      "target_amount": 0,
      "min_amount": 0,
      "max_amount": null,
      "rebalance_band_amount": 0,
      "single_position_max_amount": null,
      "sort_order": 20
    }
  ],
  "assignments": {
    "600036.SH": {
      "active_sleeve_id": "defensive",
      "assigned_by": "agent",
      "confidence": 0.82,
      "rationale": "盈利与波动特征更符合防守型定位",
      "user_locked": false,
      "suggested_sleeve_id": "defensive",
      "suggested_rationale": "",
      "suggestion_run_count": 0,
      "needs_user_review": false,
      "classification_policy_version": 1,
      "updated_at": "2026-07-13T09:12:00+08:00"
    }
  },
  "updated_at": "2026-07-13T09:12:00+08:00"
}
```

分类规则：

1. Daily Run 创建前先执行 `mandate_preflight`；新持仓或未分类持仓由 Agent 按版本化分类标准建立初始 `active_sleeve_id` 并落盘，然后才冻结 Mandate 快照和计算幂等键。
2. 已有 Agent 分类默认保持稳定；新的改区结论先写入 `suggested_sleeve_id`，不得在同一次运行中改变当前分区。
3. 同一改区建议连续满足 `reclassification_confirmations` 次且置信度达到门槛后，才在下一次 Daily Run 生效；重大投资逻辑变化可触发提前建议，但仍要留下变更记录。
4. 用户编辑分类：写入 `assigned_by=user`、`user_locked=true`，并清空待生效的 Agent 改区计数。
5. 对于 `user_locked=true` 的持仓，Agent 只能更新 `suggested_sleeve_id` 和理由。
6. 每次分类和改区都记录分类标准版本、证据、旧分区、新分区和生效 run_id。
7. 持仓被删除时，assignment 暂时保留为历史记录，但不参与当前计算。
8. 新增多级分区时使用 `parent_id`，不需要更换数据模型；第一版 UI 只开放进攻型、防守型和现金。

第一版分类标准：

- 每个维度输出 0~5 分、证据和数据时间，分类结果必须可解释，不能只输出标签。
- 波动、回撤、周期敏感度、增长弹性和事件催化越高，越偏进攻型。
- 盈利稳定性、现金流、股息、低波动和组合稳定器作用越强，越偏防守型。
- `portfolio_role` 用于识别指数底仓、现金替代、对冲或卫星仓，防止只按证券名称分类。
- 证据不足时 Agent 仍可给出初始分区，但必须设置低置信度和 `needs_user_review=true`；低置信度不得自动触发后续改区。
- 分类标准本身发生变化时提升 `classification_policy.version`，旧分类不自动重算，只生成待审核建议。
- `mandate.version` 只在有效目标、现金策略、当前分区或用户锁发生变化时递增；单纯更新建议、证据和连续确认计数只递增 `suggestion_revision`，避免建议元数据导致同一天重复生成报告。

目标验证：

- `min_amount <= target_amount <= max_amount`；`max_amount=null` 表示不设硬上限。
- 子分区金额必须能映射到父分区；不能重复计算同一持仓市值。
- 如果分区目标与现金目标之和超过当前组合净值，允许保存配置，但 Daily Run 必须标记“目标资金缺口”并采用失败关闭：保留研究、方向和优先级，暂停所有定量调仓金额，不得自行按比例缩减用户目标。
- 如果目标总额小于组合净值，差额归入未分配现金，不自动摊入任意分区。

## 4. DailyPortfolioRun 数据模型

### 4.1 运行记录

建议使用独立 SQLite：

```text
~/.vibe-trading/portfolio/daily_runs.sqlite3
```

`daily_runs` 核心字段：

```text
run_id
market_date
revision
trigger                 manual | scheduled
status                  pending | waiting_data | running | completed | partial | failed | cancelled | interrupted
stage                   snapshot | refresh | holdings | sleeves | decision | render | deliver
portfolio_snapshot_id
portfolio_updated_at
mandate_version
report_profile          daily_update
refresh_policy          ensure_fresh | force | reuse
data_batch_id
artifact_revision
holding_total
holding_completed
holding_failed
created_at
started_at
completed_at
error
artifact_root
feishu_chat_id
feishu_message_id
```

幂等键：

```text
market_date + portfolio_snapshot_id + mandate_version + report_profile
```

- `trigger` 只记录来源，不参与幂等；同一输入先手动运行、后自动运行不会生成重复报告。
- 同一输入重复点击，返回已有运行及其进度。
- `refresh_policy=force` 或“按最新数据重新生成”显式绕过已有结果，并创建新的 revision。
- 运行中持仓或 Mandate 发生变化时，本次运行继续基于冻结快照完成，但结果标记 `input_outdated=true`，飞书提供“按最新配置重新生成”。
- 手动触发发生在休市日或当日盘前窗口之后时，复用现有交易日解析逻辑确定 `market_date`，不得把休市日或已经结束的交易日作为新的盘前目标日。

### 4.2 产物目录

```text
~/.vibe-trading/portfolio/daily_runs/YYYY-MM-DD/<run_id>/
  run.json
  portfolio_snapshot.json
  mandate_snapshot.json
  data_manifest.json
  artifact_manifest.json
  holdings/
    600036.SH/
      daily_update/
        brief.json
        report.md
        report.pdf
      deep_research/
        <artifact_id>/
          report.md
          report.pdf
  sleeves/
    offensive.json
    defensive.json
  decision.json
  master/
    report.md
    report.pdf
```

所有下游阶段只读取当前运行目录中的冻结产物，不直接读取其他 Session 的历史消息。

`artifact_manifest.json` 为每个产物分配稳定的 `artifact_id`，并记录类型、symbol、revision、相对路径、SHA-256、生成时间和失效状态。API、飞书和 Session 只传递 `run_id` / `artifact_id`，不暴露或持久化本地绝对路径。

产物保留策略：

- 默认保留个股 PDF、综合 PDF 和结构化 JSON 90 天。
- Daily Run 元数据默认保留 1 年。
- 用户收藏的报告永久保留，直到主动取消收藏。
- 清理前先将 Artifact 标记为过期；Session 查询过期 Artifact 时返回可理解的提示，不留下悬空路径。
- 保留天数必须可配置，第一版不建设复杂归档 UI。

## 5. 分析结果契约

### 5.1 HoldingDailyBrief

每个持仓必须生成 `brief.json`，然后由同一内容生成 Markdown/PDF。

```json
{
  "run_id": "daily-20260713-001",
  "snapshot_id": "sha256:...",
  "report_profile": "daily_update",
  "symbol": "600036.SH",
  "name": "招商银行",
  "sleeve_id": "defensive",
  "data_status": "verified",
  "data_as_of": "2026-07-13T09:11:20+08:00",
  "material_change": true,
  "change_summary": [],
  "portfolio_context": {
    "market_value": 0,
    "portfolio_weight": 0,
    "sleeve_weight": 0,
    "cost_price": 0,
    "pnl_pct": 0
  },
  "view": {
    "action": "hold",
    "priority": "normal",
    "confidence": 0.7,
    "rationale": [],
    "invalidating_conditions": []
  },
  "conditional_observations": [],
  "risks": [],
  "source_refs": [],
  "generated_at": "2026-07-13T09:16:00+08:00"
}
```

`action` 枚举：

```text
increase_candidate | reduce_candidate | exit_candidate | hold | observe
```

数据门槛：

- `data_status=verified`：允许给出完整研究结论和条件观察位。
- `data_status=partial`：允许方向性结论，但所有缺口必须显式列出。
- `data_status=limited`：动作强制为 `observe`，不得给出金额、比例或具体价格型结论。
- 单个持仓失败时重试一次；仍失败则生成失败占位报告，组合任务继续运行。

报告深度：

- `daily_update`：每个持仓每日必有，目标长度 1~3 页，重点是相对上一份成功报告的新增数据、逻辑变化、组合角色和今日观察条件。
- `deep_research`：完整研究报告，仅在重大公告、异常波动、投资逻辑变化、Agent 明确升级或用户主动要求时生成。
- 两种报告都会自动生成 PDF；“是否查看”只影响发送，不影响已计划报告的生成。
- 第一版综合 PDF 拼接全部 `daily_update`；深度报告在综合报告中列出摘要和 Artifact 引用，不默认拼接十份长篇深度报告。

### 5.2 SleeveReview

每个分区输出：

```text
current_amount
target_amount
min_amount
max_amount
gap_to_target
status                  below_min | below_target | in_band | above_target | above_max
eligible_increases[]
eligible_reductions[]
hold_or_observe[]
unresolved_risks[]
```

### 5.3 PortfolioDecision

组合裁决必须包含可验证的预算检查：

```text
nav = actual_cash + sum(holding.market_value)
available_cash = max(0, actual_cash - cash_policy.min_amount)
sleeve_gap = target_amount - current_amount
```

硬约束：

1. 没有现金数据或关键市值数据时，不生成定量组合金额方案。
2. 目标金额总和超过当前可配置净值时，设置 `quantitative_plan_enabled=false`，不得输出定量金额。
3. 任何建议增加金额不得突破现金下限、分区上限或 `single_position_max_amount`。
4. 未超过 `rebalance_band_amount` 的偏差不触发金额调整，避免小幅波动造成频繁改动。
5. 同一笔预期减持回款只能计算一次；依赖减持回款的增加建议必须标记为 `conditional_on_reduction`，已有现金可覆盖的建议标记为 `funded_now`。
6. 强风险或投资逻辑失效可以覆盖分区低配状态，但必须写明覆盖原因。
7. 分区低配不构成必须增加的理由；没有合格标的时保留现金。
8. 个股信号积极但分区已超配时，只能持有、观察或提出分区内替换方案。
9. 一个持仓在同一快照中只能归属一个叶子分区；父分区只汇总，不得重复计入市值。

`decision.json` 至少包含：

```text
portfolio_summary
cash_summary
sleeve_summaries[]
today_observation_points[]
increase_candidates[]
reduce_candidates[]
exit_candidates[]
hold_items[]
observe_items[]
conditional_order_observations[]
budget_checks[]
quantitative_plan_enabled
data_gaps[]
```

## 6. 数据刷新策略

复用现有 UnifiedDataService：

1. 从冻结的 PortfolioState 获取全部持仓标的。
2. 一次准备市场、新闻、研报和基本面数据；市场与新闻按新鲜度刷新，基本面默认复用有来源和时间戳的最新缓存，不要求每日强制刷新。
3. 超过 25 个标的时按 25 个一组分批，但所有批次仍属于同一个 `data_batch_id`。
4. `refresh_policy=ensure_fresh`：若 09:10 预热批次满足配置的新鲜度和完整性门槛则复用，否则只补刷新缺失或过期部分。
5. `refresh_policy=force`：无条件创建新的数据批次；`refresh_policy=reuse`：只读取已有数据并如实标记缺口。
6. 手动和 09:12 自动晨会默认都使用 `ensure_fresh`；“强制刷新并重新生成”使用 `force`。
7. 同一幂等运行内不得由各个 Holding Worker 重复刷新同一标的。
8. Worker 只读取 `data_manifest.json` 中登记的 context/handle。
9. `data_manifest.json` 按标的和数据域记录各自的来源、批次、`as_of`、抓取时间、缓存状态和冲突状态；不能用一个虚假的统一时间掩盖不同来源的时间差。
10. 盘前报告不得使用尚未发生的当日盘中行情；行情基准必须明确写为上一交易日收盘、盘前可用快照或其他真实可核实时间。
11. 如果某个来源失败，保留来源状态、失败原因和缓存时间，不把缓存数据描述为当前实时数据。

现有 09:10 预热继续保留。第二阶段的 09:12 自动任务先检查预热状态：

- 已完成：直接使用该批数据创建冻结快照。
- 仍在运行：Daily Run 进入 `waiting_data`，完成后继续。
- 超时或失败：自行补充刷新，并在报告中记录数据状态。
- 09:12 的定时触发与同一输入的手动触发共享幂等结果，不重复生成或发送。

## 7. 个股报告和综合 PDF

每个 Holding Worker 总是输出：

```text
brief.json + report.md + report.pdf
```

这里的默认 `report.md` / `report.pdf` 是 `daily_update`。若生成 `deep_research`，它使用独立 Artifact，不覆盖当日日报。

综合报告内容顺序：

1. 封面：日期、数据截至时间、组合快照版本、Mandate 版本。
2. 一页结论：今日最重要的观察要点和动作候选。
3. 组合总览：净值、现金、集中度和数据覆盖。
4. 分区总览：进攻/防守等分区的当前金额、目标、上下限和缺口。
5. 条件观察清单：只收录满足研究门槛的标的。
6. 风险与数据缺口。
7. 个股附录：按分区顺序拼接每个 `daily_update/report.md` 的完整内容。
8. 深度研究索引：列出已存在的 `deep_research` 摘要和 Artifact 引用。

渲染方式：

- 每只个股独立渲染 PDF。
- 综合 PDF 从同一批 Markdown 源重新渲染，不依赖 PDF 合并库。
- 飞书默认自动发送综合 PDF。
- “查看个股报告”只发送已有 `holdings/<symbol>/daily_update/report.pdf` 或已生成的深度报告 Artifact，不产生新 Session、不重新调用模型。

## 8. 飞书交互

### 8.1 入口卡片

新增主入口：`今日晨会`

```text
今日组合晨会
持仓：10 个
现金：未设置 / 已设置
分区配置：Mandate v3

[生成今日报告]
[查看最近报告] [分区配置概览]
```

如果现金未设置，允许生成研究报告，但完成卡片必须标记“未生成定量资金分配”。

### 8.2 运行进度卡

使用同一张卡持续更新：

```text
快照             已完成
全持仓数据刷新   10/10
个股报告         7/10
分区复核         等待中
组合裁决         等待中
PDF              等待中

[取消本次运行]
```

### 8.3 完成卡片

```text
今日组合晨会已完成
数据截至：09:11
持仓覆盖：10/10
个股 PDF：10/10

进攻型：当前 / 目标 / 缺口
防守型：当前 / 目标 / 缺口
现金：实际 / 最低保留

增加候选：N
减少候选：N
持有观察：N

今日优先关注：最多 3 项

[发送综合 PDF]
[查看个股报告] [查看运行详情]
[按最新数据重新生成]
```

综合 PDF 默认随完成消息自动发送；按钮用于再次发送，不重新渲染。

“查看个股报告”打开持仓选择卡，选择后发送已有 PDF。列表展示报告状态：已完成、数据受限或生成失败。

## 9. HTTP 接口

### 9.1 Mandate

```text
GET   /portfolio/mandate
PUT   /portfolio/mandate
PATCH /portfolio/mandate/assignments/{symbol}
POST  /portfolio/mandate/suggest-classifications
```

`PATCH assignments` 用于用户最终编辑，默认写入 `user_locked=true`。

### 9.2 Daily Run

```text
POST /portfolio/daily-runs
GET  /portfolio/daily-runs
GET  /portfolio/daily-runs/latest
GET  /portfolio/daily-runs/{run_id}
POST /portfolio/daily-runs/{run_id}/cancel
POST /portfolio/daily-runs/{run_id}/retry
GET  /portfolio/daily-runs/{run_id}/reports/master
GET  /portfolio/daily-runs/{run_id}/reports/holdings/{symbol}
```

启动请求：

```json
{
  "trigger": "manual",
  "refresh_policy": "ensure_fresh",
  "report_profile": "daily_update",
  "deliver_to_feishu": true,
  "feishu_chat_id": null
}
```

`retry` 支持：

```json
{
  "stage": "holding_report",
  "symbol": "600036.SH"
}
```

第一版 `retry` 只开放个股报告重试，并执行依赖失效传播：

```text
holding_report 重试
  -> 对应 sleeve review 失效
  -> portfolio decision 失效
  -> master PDF 失效
  -> artifact_revision + 1
  -> 自动重新执行失效的下游阶段
```

重试不会原地覆盖已发送报告；旧 Artifact 标记为 superseded，新综合报告使用新的 revision，并在飞书中明确提示报告已更新。任意阶段自由重试和精细断点恢复留到后续版本。

API 放在新的 `agent/src/api/portfolio_daily_routes.py`，避免继续扩大 `agent/api_server.py`。

## 10. 代码模块规划

```text
agent/src/portfolio/mandate.py
agent/src/portfolio/daily/models.py
agent/src/portfolio/daily/store.py
agent/src/portfolio/daily/contracts.py
agent/src/portfolio/daily/orchestrator.py
agent/src/portfolio/daily/workers.py
agent/src/portfolio/daily/reporting.py
agent/src/portfolio/daily/scheduler.py
agent/src/api/portfolio_daily_routes.py
agent/src/tools/daily_report_tool.py
```

需要修改：

```text
agent/api_server.py                         注册新路由和生命周期
agent/src/channels/feishu.py                晨会入口、进度卡、个股 PDF 选择器
agent/src/channels/runtime.py               Daily Run 控制消息和报告交付
agent/src/data_layer/service.py             必要时增加快照 manifest 导出，不重复实现数据源
frontend/src/lib/api.ts                     Mandate 和 Daily Run 类型/API
frontend/src/pages/Portfolio.tsx            分区、目标、现金和运行状态编辑界面
```

稳定个股 Session 只保存 `latest_daily_run_id` / `latest_artifact_id` 引用。后续追问通过受控的 `daily_report` 读取接口获取 Artifact，不保存本地绝对路径，也不复制十份完整报告到 Session 历史中。

## 11. 第一版最小闭环

第一版必须完成：

1. 手工维护现金、进攻型/防守型目标金额和上下限。
2. Agent 为新持仓初始分类，用户编辑后锁定；既有分类具备防抖。
3. 飞书一键启动，按一个快照准备全部持仓数据。
4. 每个持仓生成 `daily_update` JSON、Markdown 和 PDF。
5. 生成分区复核、组合结论和综合 PDF。
6. 飞书自动发送综合 PDF，并可选择发送已有个股 PDF。
7. 支持幂等、取消和依赖安全的单个持仓重试。

第一版暂不建设：

- 多级分区编辑 UI；数据模型保留 `parent_id`。
- 进程重启后从任意任务断点自动续跑；重启时将未完成运行标为 `interrupted`，用户可一键按冻结输入重试。
- 任意阶段自由重试。
- 自动触发深度研究；仍可通过现有个股研究入口主动生成。
- 跨 A 股/港股/美股的多市场晨会时钟。

## 12. 开发任务清单与验收

### M1：Mandate 与组合分区

- [x] 实现 Mandate 原子读写和版本号。
- [x] 实现多级 sleeve、目标金额、上下限校验。
- [x] 实现版本化分类标准、Agent 初始分区和连续两次确认的分类防抖。
- [x] 实现 Daily Run 创建前的 mandate_preflight，以及 version / suggestion_revision 分离。
- [x] 实现用户修改和 `user_locked` 覆盖规则。
- [x] Portfolio 页面第一版只增加进攻型、防守型、现金和目标编辑区。
- [x] 当前未设置现金时给出清晰提示，但不阻止研究报告。

验收：Agent 可为全部未分类持仓分区；普通单日信号不会改变已有分区；用户改区后再次建议不会覆盖用户结果。

### M2：Daily Run 存储与调度

- [x] 实现 DailyRunStore 和运行状态机。
- [x] 实现快照 hash、修订后的幂等键、refresh_policy 和 revision。
- [x] 实现取消和依赖安全的个股报告重试。
- [x] 服务重启时将未完成运行标记为 `interrupted`，第一版不做任意断点自动续跑。
- [x] 实现动态 holding/sleeve DAG 和 3~4 并发上限。

验收：同一输入手动与自动触发只产生一个运行；强制重跑产生新 revision；取消后不继续启动新 Worker；重启后不会把旧运行误报为仍在执行。

### M3：全持仓刷新与结构化个股报告

- [x] 按 refresh_policy 准备全部冻结持仓的数据并输出带分域时间戳的 data_manifest。
- [x] 市场/新闻按新鲜度刷新，研报和基本面允许使用有来源、有时间戳的合格缓存。
- [x] Worker 禁止自行重复刷新已有标的。
- [x] 实现 HoldingDailyBrief JSON 校验和一次自动修复/重试。
- [x] 实现 `daily_update` 与 `deep_research` 报告契约，第一版 Daily Run 只自动生成 daily_update。
- [x] 数据受限时强制 `observe`。
- [x] 单个持仓失败不拖垮整份组合报告。

验收：当前 10 个持仓均产生状态明确的 daily_update；同一标的在一次运行内只有一套数据 manifest；盘前报告不引用尚未发生的当日盘中数据；0 个持仓时明确拒绝启动，26 个持仓时正确分批且保持同一个 data_batch_id。

### M4：分区复核、现金约束与组合裁决

- [x] 实现 SleeveReview。
- [x] 实现现金下限、分区上下限、单标的上限、再平衡带宽和重复回款检查。
- [x] 目标不可行或现金缺失时强制关闭定量金额计划。
- [x] 区分 `funded_now` 与 `conditional_on_reduction`。
- [x] 实现 `decision.json`。
- [x] 为所有硬约束增加单元测试。

验收：构造超额目标、现金不足、分区超配、数据缺失等场景，系统都不能输出违反约束的金额建议。

### M5：PDF 报告

- [x] 每只持仓自动生成 Markdown 和 PDF。
- [x] 综合报告自动包含全部个股附录。
- [x] 统一中文字体、日期、标题和文件名清理。
- [x] 个股 PDF 选择发送不触发重新分析。
- [x] 实现 Artifact ID、校验和、revision、superseded 标记和可配置保留策略。

验收：产生 10 份个股 PDF 和 1 份综合 PDF；抽取任意个股 PDF 与综合附录内容一致。

### M6：飞书闭环

- [x] 新增“今日晨会”入口卡。
- [x] 同卡更新阶段进度。
- [x] 完成后自动发送综合 PDF 和短摘要。
- [x] 新增个股报告选择器。
- [x] 新增取消、重试、按最新数据重跑。
- [x] 个股重试后自动重算分区、组合决策和综合 PDF，并发布新 revision。
- [x] 复用已发布的“分析菜单 → 组合晨会”入口；新增操作均为卡片回调，不需要新增飞书控制台菜单项。

验收：真实飞书端完成“点击 -> 进度 -> 综合 PDF -> 选择个股 PDF -> 后续追问”全链路。

第一阶段开发验证（2026-07-14）：

- 后端专项测试覆盖 Mandate、幂等/revision、26 标的分批、数据门禁、约束预算、Artifact、单股重试和飞书卡片回调。
- 前端全量测试、生产构建和 `/portfolio` 桌面/手机响应式浏览器检查通过。
- 本地 API 已重启到当前实现，新增路由已加载，飞书频道状态为 running。
- 未由自动化代替用户在真实飞书账号中点击卡片；真实端点击链路作为本阶段的用户验收项保留。

### M7：09:12 自动晨会（第二阶段）

- [ ] 仅交易日触发。
- [ ] 与 09:10 预热衔接。
- [ ] 每个市场日期只自动创建一次。
- [ ] 服务重启后不重复发送。
- [ ] 自动运行失败时发送简短失败卡，允许手动重试。

验收：模拟交易日、周末、重启和预热超时，均不重复生成或漏掉可恢复任务。

## 13. 后续增强项

- 任意任务的断点自动续跑和 worker lease 恢复。
- 任意阶段依赖感知重试。
- 多级分区编辑 UI。
- 重大事件自动升级为 `deep_research`。
- 多市场日历、时区和分市场晨会。
- 报告收藏、归档和保留策略管理界面。

## 14. 发布前验证

后端：

```text
Mandate schema/override tests
Daily Run state/idempotency/cancel/interrupted tests
refresh policy, data snapshot and no-duplicate-refresh tests
holding/sleeve/portfolio contract tests
cash and allocation invariant tests
classification hysteresis and user-lock tests
artifact id/revision/retention tests
zero-holding and over-25-symbol batching tests
holding retry downstream invalidation tests
PDF generation tests
Feishu card/runtime delivery tests
```

前端：

```text
Portfolio target editor tests
Daily Run status UI tests
production build
```

真实链路：

```text
启动 API -> 检查 /channels/status
飞书点击“今日晨会”
核对 10 个个股报告和综合 PDF
选择发送一份个股 PDF，确认未触发新运行
修改一个分区后强制重跑，确认 mandate_version 和 revision 更新
重试一个个股报告，确认分区结论、组合结论和综合 PDF 全部进入新 revision
```
