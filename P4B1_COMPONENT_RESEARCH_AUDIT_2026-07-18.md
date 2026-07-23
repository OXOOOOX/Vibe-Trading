# P4B1 Component Research Digest 真实知识覆盖审计

审计时间：2026-07-18（Asia/Shanghai）  
研究截止：2026-07-18T23:59:59+08:00  
模型调用：0  
输入 Token：0  
输出 Token：0

## 审计方法与隔离边界

- 真实知识源为 `C:\Users\23479\.vibe-trading\cache\research_cache.sqlite3`。
- 对真实库只使用 SQLite `mode=ro` 连接；P4B1 表、Resolution 和 Binding 全部在由 SQLite backup API 生成的临时副本中初始化和运行。
- 审计窗口前后真实库文件大小均为 `19,722,240` 字节，`mtime_ns` 均为 `1784379772057773100`。
- 审计前后真实库均不存在 `component_research_digests`、`etf_component_digest_bindings`、`component_digest_resolutions` 或 `component_research_audit` 表；本次没有向真实运行库写入 P4B1 数据。
- 证券关联只使用规范化代码精确匹配 `subject_key` 与 `symbol`；没有使用公司名称，也没有进行 A/H 股自动合并。
- Report 的 `data_as_of`、`generated_at`，Claim/Fact 的 `created_at`，Evidence 的 `valid_from`/来源发布时间，以及冲突创建时间均受 `analysis_as_of` 截止约束。

## 513120.SH 可靠 Snapshot

513120.SH 使用现有 `ETFUniverseProvider` 的中证指数公司官方结构化收盘权重来源，未按名称硬编码成分：

- Provider：`csi_official_close_weight`
- 来源类型：`official_index_weight`
- 指数：`931787.CSI` 中证香港创新药指数
- 数据截止：2026-06-30
- Snapshot 质量：`complete`
- 成分覆盖：42 / 42
- 原始权重覆盖：99.998%
- 来源 ID：`csi:931787:20260630:closeweight`
- 官方权重文件：`https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/closeweight/931787closeweight.xls`
- ETF/指数映射来源：`https://www.gffunds.com.cn/jjgg/flwj/202406/P020240628362700079556.pdf`

P4A 从该 Snapshot 选择的五只港股均已规范为五位代码加 `.HK` 后缀：`01801.HK`、`06160.HK`、`02359.HK`、`02269.HK`、`09926.HK`。没有映射到同名或相关 A 股研究。

## 真实覆盖结果

| ETF | 成分 | 规范代码 | 现有报告数 | 有效 Claim 数 | 覆盖维度 | 最新数据截止日 | Digest 状态 | 冲突数 | 可复用 ETF 数 |
|---|---|---|---:|---:|---|---|---|---:|---:|
| 588870.SH | 寒武纪 | 688256.SH | 0 | 0 | — | — | missing | 0 | 0 |
| 588870.SH | 海光信息 | 688041.SH | 0 | 0 | — | — | missing | 0 | 0 |
| 588870.SH | 中芯国际 | 688981.SH | 0 | 0 | — | — | missing | 0 | 0 |
| 588870.SH | 澜起科技 | 688008.SH | 0 | 0 | — | — | missing | 0 | 0 |
| 588870.SH | 中微公司 | 688012.SH | 0 | 0 | — | — | missing | 0 | 0 |
| 510300.SH | 宁德时代 | 300750.SZ | 0 | 0 | — | — | missing | 0 | 0 |
| 510300.SH | 贵州茅台 | 600519.SH | 0 | 0 | — | — | missing | 0 | 0 |
| 516010.SH | 巨人网络 | 002558.SZ | 0 | 0 | — | — | missing | 0 | 0 |
| 516010.SH | 三七互娱 | 002555.SZ | 0 | 0 | — | — | missing | 0 | 0 |
| 516010.SH | 恺英网络 | 002517.SZ | 0 | 0 | — | — | missing | 0 | 0 |
| 516010.SH | 完美世界 | 002624.SZ | 0 | 0 | — | — | missing | 0 | 0 |
| 516010.SH | 光线传媒 | 300251.SZ | 0 | 0 | — | — | missing | 0 | 0 |
| 513120.SH | 信达生物 | 01801.HK | 0 | 0 | — | — | missing | 0 | 0 |
| 513120.SH | 百济神州 | 06160.HK | 0 | 0 | — | — | missing | 0 | 0 |
| 513120.SH | 药明康德 | 02359.HK | 0 | 0 | — | — | missing | 0 | 0 |
| 513120.SH | 药明生物 | 02269.HK | 0 | 0 | — | — | missing | 0 | 0 |
| 513120.SH | 康方生物 | 09926.HK | 0 | 0 | — | — | missing | 0 | 0 |

本次代表性集合中：

- `reusable`：0
- `partial_reusable`：0
- `stale`：0
- `missing`：17
- `conflicted`：0

这些成分属于“确实没有按规范代码进入统一报告目录的现有个股研究”，不是“有报告但没有 Claim”、不是“Claim 已过期”，也不是“存在冲突”。P4B1 没有为它们生成摘要，没有启动个股 Deep Research。

## Resolution、复用与缓存

| ETF | P4A 入选 | reusable | partial | stale | missing | conflicted | 复用率 | 第二次执行 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 588870.SH | 5 | 0 | 0 | 0 | 5 | 0 | 0% | Resolution 与 5 个缺失状态均命中缓存 |
| 510300.SH | 2 | 0 | 0 | 0 | 2 | 0 | 0% | Resolution 与 2 个缺失状态均命中缓存 |
| 516010.SH | 5 | 0 | 0 | 0 | 5 | 0 | 0% | Resolution 与 5 个缺失状态均命中缓存 |
| 513120.SH | 5 | 0 | 0 | 0 | 5 | 0 | 0% | Resolution 与 5 个缺失状态均命中缓存 |
| 560010.SH | 0 | 0 | 0 | 0 | 0 | 0 | 0% | 空 Resolution 命中缓存；Binding 数为 0 |

- 首次执行构建 17 个确定性缺失状态记录和 5 个 Resolution；第二次执行命中 17 个 Digest 状态缓存及 5 个 Resolution 缓存，没有重复构建记录。
- 真实集合没有两只 ETF 同时选中同一只已有研究的证券，因此真实跨 ETF 共享 Digest 数为 0；专项测试使用同一证券被两只 ETF 选中的夹具，验证只持久化 1 个 Digest 和 2 个不同 Binding。
- 真实集合 `estimated_avoided_model_calls=0`，因为没有可复用研究。专项夹具中每个 `reusable` 或 `partial_reusable` Digest 按“一只成分原本可能需要一次摘要调用”的口径计为一次避免调用。
- 全部真实审计 Resolution 的 `model_calls`、`input_tokens`、`output_tokens` 均为 0。

## 结论与 P4B2 前置条件

P4B1 已能正确识别当前真实覆盖缺口，但本次代表性成分没有可复用的规范代码级个股研究。P4B2 开始前仍需：

1. 明确 P4B2 的用户授权、单成分/单 ETF/单日调用与 Token 上限。
2. 明确只处理 P4A 入选且为 `missing`、`stale` 或关键 `conflicted` 的成分。
3. 为新研究建立可发布、可索引的 Report/Claim/Fact/Evidence 产出流程；P4B2 不能绕过统一知识库。
4. 保持港股证券代码级研究隔离；没有可靠实体映射前继续禁止 A/H 合并。
5. 继续保持 560010.SH 默认空选择不触发研究，事件驱动时只处理强制入选成分。
