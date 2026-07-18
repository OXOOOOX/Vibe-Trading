# Portfolio AI Monitor W1 影子运行手册

## 1. 目的

W1 用真实行情验证调度、数据门禁、规则状态机、事件去重、容量和恢复能力。影子模式会真实取数、评估并持久化 `would-deliver` 记录，但不会调用飞书外发。

W1 不验证新闻、基本面或真实飞书交付，也不授权任何交易写入。

## 2. 安全开关

```env
VIBE_TRADING_MONITORING_ENABLED=1
VIBE_TRADING_MONITORING_MODE=shadow
VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED=1
```

- `VIBE_TRADING_MONITORING_ENABLED=0` 是最终 kill switch。
- 模式缺失时默认 `shadow`；非法模式会 fail-safe 到 `shadow`。
- 管理员 start 接口不能绕过总开关。
- W1 期间禁止设置 `VIBE_TRADING_MONITORING_MODE=deliver`。

Portfolio 页面的“启动影子监控 / 停止监控服务”按钮是以上开关的用户入口。启动会把 `enabled=1`、`mode=shadow` 持久化到 `agent/.env` 并立即拉起当前运行时；停止会持久化 `enabled=0` 并终止运行时，但保留计划、事件和审计历史。用户不需要编辑代码或配置文件，页面也不提供跳过 W1 门禁直接切换 `deliver` 的入口。

当前运行时只接入沪深京交易日历。其他市场的 active plan 会继续保存，但不会套用 A 股时段取数或显示为“监控中”；页面明确显示“该市场调度待接入”。

## 3. 启动前检查

以下命令统一使用当前本地后端端口；如部署地址不同，只覆盖 `$BaseUrl`，不要逐条修改命令：

```powershell
$BaseUrl = if ($env:VIBE_TRADING_BASE_URL) {
  $env:VIBE_TRADING_BASE_URL.TrimEnd('/')
} else {
  'http://127.0.0.1:8899'
}
```

1. 确认监控数据库和 PortfolioState 路径指向预期环境。
2. 记录 PortfolioState 与 Daily Run 输入的规范化 hash。
3. 执行一次强制维护，确认 SQLite 在线备份成功：

```powershell
Invoke-RestMethod -Method Post `
  -Uri "$BaseUrl/admin/portfolio/monitoring/maintenance" `
  -ContentType 'application/json' `
  -Body '{"force":true}'
```

4. 在 Portfolio 页面只启用一个自愿测试标的，并确认计划、阈值、频率和飞书目标。飞书目标在 shadow 中只用于记录“本应发送给谁”。
5. 重启后端后检查：

```powershell
Invoke-RestMethod "$BaseUrl/portfolio/monitoring/status"
```

必须看到：

- `effective_mode = shadow`
- `runtime.mode = shadow`
- `runtime.mode_valid = true`
- `runtime.running = true`
- leader 实例 `runtime.leader = true`
- 日历是 `exchange_calendar` 或 `cached_exchange_calendar`
- `pending_deliveries = 0`

## 4. 分级影子运行

| 阶段 | 标的数 | 最少覆盖 | 扩级条件 |
| --- | ---: | --- | --- |
| S1 | 1 | 一个完整交易日 | 所有硬门禁通过 |
| S2 | 3 | 一个完整交易日 | 调度、数据源和数据库仍有稳定余量 |
| S3 | M0 实测安全上限 | 至少三个连续完整交易日 | 所有硬门禁持续通过 |

如果某日缺少开盘、午休恢复、收盘前任一关键时段，或者发生未解释的进程离线，该日不计入连续 soak。

## 5. 每日记录模板

| 字段 | 记录值 |
| --- | --- |
| 日期与标的数 |  |
| 运行版本/commit |  |
| calendar mode |  |
| 开盘、午休恢复、收盘前是否覆盖 |  |
| tick count / error tick count |  |
| duration P95 / max |  |
| schedule lag P95 / max |  |
| bar lag P95 / max |  |
| blocked profile 数及原因 |  |
| shadow_suppressed 数 |  |
| duplicate_observation_count |  |
| duplicate_event_count |  |
| pending / uncertain delivery 数 |  |
| 数据库当日增长 |  |
| PortfolioState 前后 hash |  |
| Daily Run 输入前后 hash |  |
| 重启/数据中断/重复 bar 演练结果 |  |
| 异常、解释与处理 |  |
| 当日是否计入连续 soak |  |

## 6. 硬验收门禁

以下任一失败都停止扩级并重新开始连续 soak：

- PortfolioState 和 Daily Run 输入未因监控轮询发生变化。
- shadow 期间飞书实际外发次数为 0。
- `duplicate_event_count = 0`；同一 bar、同一 episode 和重启回放不产生第二个 confirmed event。
- stale、partial、conflict、无来源或超出 bar freshness 的数据只产生 blocked 状态，不产生事件。
- 日历不可用、休市、午休和非交易时段不执行行情评估。
- 双实例同时运行时只有一个 leader，同一到期任务只有一个有效执行者。
- 每个 due profile 必须且只能落入 `evaluated` 或带稳定 reason code 的 `blocked`；两者之和必须等于支持市场的 due profile 数。
- `pending_deliveries = 0`；历史 pending 在 shadow/off 下变为带原因的 `shadow_suppressed`，不会外发。
- `delivery_uncertain` 没有无解释增长，也没有自动盲目重试。
- 数据库未超过配置上限的 80%；备份、保留清理和 WAL checkpoint 成功。
- 只有交易时段内、确实到期且允许评估的任务进入 open-session schedule lag 分位数；休市、午休和无到期 tick 单独计数。
- `P95(schedule lag + duration)` 不超过最短检查周期的 20%，P99 不超过 50%。
- 单次 duration 最大值低于 leader lease 的 50%；否则必须先启用续租和 fencing，再继续 soak。
- verified/actionable 数据可用率不低于 95%；所有不可用项都有稳定 reason code，且超 freshness 上限的数据触发数为 0。
- 按当前日增长外推 30 天后数据库利用率仍低于 50%。

## 7. 回放与故障测试

可复用入口：

```python
from src.portfolio.monitoring import replay_quotes

result = replay_quotes(
    store,
    profile_id,
    quotes,
    delivery_mode="shadow",
    duplicate_indexes={2},
    reopen_before_indexes={2},
)
```

自动化覆盖命令：

```powershell
.\.venv\Scripts\python.exe -m pytest agent/tests/test_portfolio_monitoring.py -q
```

至少演练：阈值震荡、重复 bar、午休缺口、进程重启、双 leader、行情源失败、陈旧行情、SQLite 维护和远端结果未知。

## 8. 关停与回滚

紧急关停：

```env
VIBE_TRADING_MONITORING_ENABLED=0
```

随后调用 stop 或重启后端：

```powershell
Invoke-RestMethod -Method Post `
  -Uri "$BaseUrl/admin/portfolio/monitoring/stop"
```

关停不删除 profile、plan、event、delivery、tick 或维护记录。需要代码回滚时，先停止 runtime，再使用维护接口生成的 SQLite 在线备份；不要直接复制仍在写入的 WAL 数据库文件。

## 9. W1 退出条件

只有自动化硬门禁全部通过、M0 数值门槛已由真实数据冻结，并完成至少三个连续完整交易日的 S3 shadow soak，W1 才能标记为全部完成。之后进入 W2 LLM Planner；真实飞书 `deliver` 仍需等待 W3 的受控链路验收。

## 10. 飞书提醒目标绑定验收

绑定目标时不录入 open_id/chat_id：

1. 在 Portfolio 监控中心点击“生成飞书绑定验证码”。
2. 私聊机器人发送 `绑定监控 <验证码>`；群聊必须发送 `@机器人 绑定监控 <验证码>`。
3. 确认飞书回复绑定成功，页面自动显示并选中对应的私聊或群聊目标。
4. 使用同一验证码再次发送，必须返回无效/已过期/已使用，且不能产生第二个 delivery target。
5. 等待超过 10 分钟后发送未使用验证码，必须拒绝绑定。

该验收只验证目标归属和一次性消费；在 `off` 或 `shadow` 下不得借此发送任何监控事件。

## 11. 持仓雷达状态验收

1. 点亮任一持仓的 AI 监控雷达，刷新页面后该雷达必须仍为点亮状态。
2. 关闭该雷达并再次刷新，必须保持关闭；生成监控草案后也不得自动清空用户的雷达选择。
3. 持仓中已不存在的证券标识必须从本地持久化选择中自动清理。
4. 已生成的“监控标的”卡片必须同时展示持仓名称、6 位证券代码和市场标识。

雷达仅表示用户选择了哪些标的进入监控草案范围；真正开始监控仍需生成草案、审核并显式启用计划。

## 12. 已关闭标的重新打开验收

1. 对一个因 `quote_not_actionable:*` 阻断后被用户关闭的 profile，恢复其双源 verified/raw 行情。
2. 在监控标的卡片选择一个有效飞书目标，点击“重新检测并打开”；确认生成新草案并打开审核抽屉，profile 状态为 `pending_review`，但不会自动激活。
3. 不恢复行情并重复上述场景；profile 必须回到 `drafting`，显示最新 `blocked_reasons`，不得生成可启用价格计划。
4. 对非 closed profile 调用 reopen 必须返回 409；失效飞书目标也必须拒绝。
5. 全局 runtime 为 off 时，即使新计划经用户确认激活，也不得产生调度或飞书投递；reopen 不能绕过 kill switch。
6. 已关闭 profile 点击“查看计划”时，抽屉必须立即出现；抽屉只能提供“重新检测数据源”，不得直接调用旧计划 activate。数据通过门禁后，新草案继续显示在同一抽屉，再显式确认启用。

该入口是“重新校验后重建草案”，不是强制信任低质量数据。真正开始常态检查仍需用户审核并激活计划，且部署端总开关必须处于允许的 shadow/deliver 模式。

## 13. 检查频次与可见心跳验收

1. 在待审核计划中将服务器检查频次切换为每 1 分钟或每 5 分钟，保存后重新打开计划，选择必须持久化。
2. 将任一启用规则切换为 1 分钟 K 线时，服务器检查频次必须自动提升到每 1 分钟；后端也必须拒绝“每 5 分钟检查 + 1 分钟规则”等会漏掉闭合 bar 的组合。
3. runtime 开启且处于交易时段时，页面每 5 秒同步真实 profile 状态；`last_quote_check_at` 更新后的 8 秒内，目标卡片显示一次心跳跳动，并标明“数据正常”或“数据受阻”。
4. 卡片必须显示最近检查和预计下次检查；runtime off、未启动、备用实例、休市、暂停和关闭分别显示真实原因，不得使用循环动画冒充后台正在取数。
5. 开启系统减少动态效果设置时，心跳动画必须停止，但文本反馈仍保留。

## 14. 主动刷新与数据门禁重试验收

1. 草案创建请求携带 `force_fresh=true` 时，后端必须以 `force=True`、`read_only=True` 刷新 1m/5m/1D 原始行情，然后才运行 Planner；刷新不得改写 PortfolioState。
2. “重新检测并打开”和“重新获取数据”必须走同一主动刷新路径，不能只重复读取旧缓存。
3. 主动刷新后仍为 stale/single-source/partial 时继续保持 drafting，但页面显示中文原因和“重新获取数据”按钮，不要求用户先关闭 profile。
4. 当任一主动刷新仍在执行时，`GET /portfolio/monitors` 和单 profile 详情读取必须保持可响应；页面点击“查看计划”必须先展示加载态，不能看起来像按钮失效。
5. 全局运行时关闭时，active profile 必须显示“当前未实际监控 / 等待监控服务启动”，不得显示已过期的“下次预计 N 分钟前”。运行时正常且排程已经逾期时，必须显示“检查排队中：已延迟 N 分钟”。
6. 每个监控标的卡片无需打开抽屉即可看到当前展示计划的检查频次、K 线粒度、启用规则、计划版本、数据截止时间和飞书目标状态；active/paused/closed 优先展示其 `active_plan_version`，待审核状态展示最新 pending review 版本。
7. 刷新异常或超过 30 秒预算时必须保留已有证据并返回明确阻断原因，不得把旧缓存冒充新行情。

## 15. 计划保存、版本与页面断联验收

1. 打开待审核计划，修改任一价格阈值后不点击单独保存，直接点击“保存并启用”；重新读取 active plan，必须得到修改后的值，不能启用修改前的持久化版本。
2. 使用旧 `expected_revision` 重放同一请求，必须返回 409；草案与 active plan 都不能发生部分更新。
3. 清空价格、次数、冷却或频次输入时，页面必须显示行内错误；请求体中不得出现由空字符串转换出的 `0`。
4. 同一 profile 连续重新分析两次，只允许最新版本保持 `pending_review`，此前待审核版本必须为 `superseded`；当前 active plan 不受影响。
5. 抽屉同时提供运行中、待审核和历史版本；待审核版本在刷新后仍可见，并在启用前展示频次、阈值、依据和规则增删 diff。
6. 人为阻断 `/portfolio/monitoring/status` 两个轮询周期后，页面显示“与监控服务断联”和最后同步时间，倒计时、心跳与 Boost 停止；恢复接口后显示已重连并使用新状态。
7. 点击“停止监控服务”必须先显示受影响 active profile 数量；取消确认时不能调用 stop 接口。

## 16. W3 飞书真实交付验收

W3 只能在 W1 连续 soak 全部通过、deliver readiness 为 ready、目标位于测试白名单后进行：

1. 先向一个已绑定的私聊测试目标发送明确标注的测试提醒；数据库必须保存 Feishu 返回的真实 `remote_message_id`、provider request id、接受时间和最终状态。
2. 使用同一 delivery UUID 重放 20 次，飞书侧只能出现一条可见消息，数据库只保留一个逻辑 delivery。
3. provider 明确拒绝时记录 `rejected`；超时、断链或无法确认远端结果时记录 `delivery_uncertain`，不得自动重发。
4. 在 `delivering` 状态硬杀进程，重启后超过 claim lease 的记录必须转为 `delivery_uncertain`；正常停止 deliver runtime 时尚未发送的 pending outbox 必须保留。
5. 首次 canary 只允许一个 profile、一个私聊测试目标和每日上限；覆盖一个完整交易日后，才能扩到三个 profile。
6. 任一重复消息、双 leader、未解释 uncertain、越过每日上限或非白名单投递都立即调用 stop，并将运行模式退回 shadow。

W3 不验证交互卡操作。确认、暂停一天、重新分析、查看计划和关闭监控等带 nonce 的飞书操作属于后续 P1，不能作为开放真实提醒的替代门禁。

## 17. 不可变 soak 快照报告

每个阶段结束时使用独立导出脚本保存状态快照。`--output` 必填；目标文件已存在时默认拒绝覆盖，只有显式传入 `--force` 才会原子替换。报告仅摘录状态接口中的门禁字段，不读取或写入环境变量密钥、飞书 chat id、delivery target id 或数据库路径。

```powershell
$ReportDir = Join-Path $HOME '.vibe-trading\portfolio\monitoring\soak-reports'
$Stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$ReportPath = Join-Path $ReportDir "S2-$Stamp.json"

.\.venv\Scripts\python.exe agent/scripts/portfolio_monitor_soak_report.py `
  --base-url $BaseUrl `
  --stage S2 `
  --expected-profile-count 3 `
  --output $ReportPath
```

启动前后如需证明 PortfolioState 与 Daily Run 输入未变化，启动前先生成一份报告，结束时用相同的两个 JSON 输入并通过 `--baseline-report` 指向启动前报告：

```powershell
.\.venv\Scripts\python.exe agent/scripts/portfolio_monitor_soak_report.py `
  --base-url $BaseUrl `
  --stage S2 `
  --expected-profile-count 3 `
  --portfolio-state $PortfolioStatePath `
  --daily-run-input $DailyRunInputPath `
  --baseline-report $BaselineReportPath `
  --output $ReportPath
```

脚本成功写出报告时退出码为 0，门禁结论读取 `gate_summary.overall_status`。单次状态快照无法证明完整交易日、独立飞书零外发或全窗口单 leader 时，相应门禁必须是 `insufficient_evidence`，不得据此把 W1/W3 标记为通过。
