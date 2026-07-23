# ETFUniverseProvider 自动采集层与 P4A 接入验收

验收日期：2026-07-18  
日常路径模型调用：0  
日常路径输入/输出 Token：0 / 0

## 1. 已交付边界

自动采集链已经接入 `ETFUniverseSnapshot -> P4A holding_penetration`：

1. `csi_official_close_weight`：已实现并启用，直接读取中证指数公司结构化收盘权重 XLS，优先级 10，月度数据最长复用 45 天。
2. `tushare_index_weight`：已实现并启用，读取 `index_weight`，优先级 20，月度数据最长复用 45 天。当前本机 Token 对该接口没有权限，失败会记录为 `permission_denied`，不会伪装成功。
3. `tushare_fund_portfolio`：已实现并启用，读取基金季度持仓，优先级 40，最长复用 150 天；只标记为 `quarterly_fund_holdings` 和 `top-ranked partial`，不冒充完整指数成分或指数权重。
4. PCF：仅保留扩展位。PCF 字段是申赎篮子的数量/现金替代语义，当前没有可靠的确定性换算链将其变为指数权重，因此未接入 P4A 权重输入。
5. ETF 到指数映射：先查带来源、有效期和置信度的 8 只 ETF 审计表；其他合格沪深 ETF 再尝试 Tushare `etf_basic`。映射失败、过期或权限不足均显式失败。

服务按 ETF 代码执行 cache-first、逐级 provider fallback、官方源传输错误最多 2 次尝试、每代码 single-flight、语义内容哈希去重、成功/失败/降级/缓存命中审计。刷新失败时仅允许复用仍在有效期且质量通过的快照；价格敏感的陈旧行情仍由原有门控拒绝。

## 2. 质量语义

- `complete`：存在成分，观测数达到预期数，权重和处于 90%–105%，必填字段覆盖不低于 95%。
- `partial`：只有明确按权重排序的披露（例如基金前十大持仓）才允许进入 P4A，并保留已知权重覆盖率和警告。
- `insufficient`：随机缺失、未知排序的部分成分、权重异常、空结果或必填字段不足；不得进入 P4A。
- 权重统一存为 0–1 小数；百分数、小数、重复代码和异常权重和均有专项测试。

因此，沪深 300 前十大约 22% 的来源若能证明是前十大，可以作为可信 `partial` 进入 P4A；随机拿到约 22% 则会失败关闭。

## 3. 首批五只 ETF 真实外部源验收

本次使用隔离 SQLite：`%TEMP%\vibe-etf-acceptance-2baff993ff5742568a6378a477ffd157\research_cache.sqlite3`。每只先强制真实抓取，再执行一次普通读取验证缓存。五只均从中证指数公司结构化收盘权重文件取得 2026-06-30 数据。

| ETF | 跟踪指数 | Provider | 预期/观测成分 | 权重覆盖 | Snapshot 质量 | P4A 入选 | 第二次读取 |
|---|---|---|---:|---:|---|---:|---|
| 588870.SH | 000688.SH 科创50 | csi_official_close_weight | 50 / 50 | 99.999% | passed | 5 | Snapshot/P4A 均命中缓存，无网络 |
| 510300.SH | 000300.SH 沪深300 | csi_official_close_weight | 300 / 300 | 100.000%* | passed | 2 | Snapshot/P4A 均命中缓存，无网络 |
| 560010.SH | 000852.SH 中证1000 | csi_official_close_weight | 1000 / 1000 | 99.986% | passed | 0 | Snapshot/P4A 均命中缓存，无网络 |
| 513120.SH | 931787.CSI 港股创新药 | csi_official_close_weight | 42 / 42 | 99.998% | passed | 5 | Snapshot/P4A 均命中缓存，无网络 |
| 516010.SH | 930901.CSI 动漫游戏 | csi_official_close_weight | 28 / 28 | 99.999% | passed | 5 | Snapshot/P4A 均命中缓存，无网络 |

\* 原始权重和约 100.008%，合同中的覆盖率字段封顶为 100%，原始行与原始内容哈希仍完整保留。

隔离库最终包含 5 条 `etf_research_snapshots`、5 条 `etf_module_cache` 和 25 条 `etf_reuse_audit`。五只的首次调用均为真实网络抓取，第二次均 `cache_hit=true`、`network_fetched=false`、`p4a_cache_hit=true`；模型与 Token 均为 0。

### 每只 ETF 的来源

| ETF | ETF-指数映射来源 | 成分权重来源 |
|---|---|---|
| 588870.SH | 汇添富基金产品文件 | 中证指数 `000688closeweight.xls` |
| 510300.SH | 上交所基金公告 | 中证指数 `000300closeweight.xls` |
| 560010.SH | 广发基金产品页 | 中证指数 `000852closeweight.xls` |
| 513120.SH | 广发基金产品文件 | 中证指数 `931787closeweight.xls` |
| 516010.SH | 国泰基金产品页 | 中证指数 `930901closeweight.xls` |

扩展映射表还覆盖 `159842.SZ -> 399975`、`512890.SH -> H30269`、`512680.SH -> 399967`，均保存来源 URL、有效窗口和置信度；本轮没有把它们算入首批五只验收。

## 4. API 与预热

- `GET /research/etf/{symbol}/universe`：只读状态、映射、provider 与缓存/P4A 状态，不触发外部抓取。
- `GET /research/etf/{symbol}/universe/snapshot`：读取最新快照。
- `POST /research/etf/{symbol}/universe/refresh`：按需刷新，可传 `as_of` 和事件成分代码。
- `POST /research/etf/universe/prewarm`：按当前持仓预热已识别 ETF，默认仍遵循 cache-first。

没有新增前端页面。

本机 `127.0.0.1:8899` 无需重启：`GET /health` 返回 200/healthy，新 `GET /research/etf/588870.SH/universe` 也返回 200，并显示快照可复用、P4A 为 deterministic、模型与 Token 为 0。

## 5. 测试与真实库检查

- 自动测试覆盖 provider 合同、权重尺度、重复/异常、完整/Top-ranked partial/随机 partial、Tushare 权限失败、provider fallback、一次传输重试、快照哈希去重、强制刷新、有效缓存降级、过期缓存拒绝、并发 single-flight、P4A 五种结构、重大事件强制入选、API 和模型/Token 为零。
- 真实外部源测试由 `VIBE_TRADING_RUN_LIVE_ETF_PROVIDER=1` 显式开启，参数化抓取首批五只 ETF；默认测试环境跳过，避免把网络波动伪装成单元测试失败。
- 相关 ETF/P4A/Deep Report/报告库/知识库回归：153 passed，5 skipped。新增 provider 专项在默认离线门控下为 16 passed、5 skipped；显式开启真实外部源后为 21 passed。
- Ruff：通过。

最终使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON` 检查 `~/.vibe-trading/cache/research_cache.sqlite3`。检查时库内有 1 条 `588870.SH` universe snapshot、1 条 deterministic `holding_penetration` 模块结果和 3 条审计，输入/输出 Token 均为 0。该 1 组记录来自验收诊断期间一次误用默认 store 的调用；发现后没有执行任何测试清理、删除或继续写入真实库。

## 6. 已知限制

- 当前环境 Tushare `etf_basic/index_weight/fund_portfolio` 权限不足，首批验收实际使用中证指数公司官方结构化权重源；Tushare 分支已由可控错误和 fallback 测试覆盖，但未在本机完成成功态实源验收。
- PCF 暂不转换为指数权重；若后续接入，必须新增明确的 PCF 数量/现金替代合同，不可复用 `index_weight` 字段含义。
- 审计映射表有有效期，需在到期前用官方基金/交易所披露复核；过期后系统会显式失败，而不是静默沿用。
- 本次只完成采集、快照、P4A 选择和 API 接入，不扩展 P4B 成分研究摘要，也不生成新的正式 ETF 报告或前端入口。
