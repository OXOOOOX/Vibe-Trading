# P4B2-G 三成分官方证据源只读审计

执行时间：2026-07-18 23:08-23:26（Asia/Shanghai）  
执行模式：`read_only_source_audit`  
分析截止：`2026-07-18T15:04:18.056241+00:00`  
范围：仅 `588870.SH` 的 `688256.SH`、`688041.SH`、`688981.SH`

机器可读清单：`P4B2_EVIDENCE_MANIFEST_2026-07-18.json`

## 1. 执行结论

三只成分均找到了可以满足 P4B2 核心 Evidence 门控的上交所法定披露组合：每只一份 2025 年年度报告和一份 2026 年第一季度报告。

- 年度报告提供精确证券身份、主营业务、风险因素和治理材料。
- 一季报提供截至 2026-03-31 的最新财务数据和经营变化原因。
- 六份文件的证券代码、公司名称、披露日期、页数、文件大小和 SHA-256 已核验。
- 年报和一季报披露时间均不晚于分析截止时间，没有未来数据泄漏。
- 一季报明确为未经审计，后续 Fact 必须保留这一来源属性。
- 当前不需要扩大 Token。按 5 条短 Evidence、3 条结构化 Fact 和每条摘要不超过 120 个汉字预演，全批保守输入上界为 15,915，低于已授权的 18,000。

本阶段没有把 PDF、Evidence 或 Fact 写入生产知识库，也没有调用模型。

## 2. 官方文件清单

### 688256.SH 寒武纪

| 文件 | 上交所披露 | 页数 | SHA-256 |
|---|---|---:|---|
| [2025年年度报告](https://star.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-03-13/688256_20260313_GWH3.pdf) | 2026-03-13 | 219 | `e9360bfb...7507f4ab` |
| [2026年第一季度报告](https://star.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-04-30/688256_20260430_I4ZK.pdf) | 2026-04-30 | 12 | `6aed917a...fed18d0` |

已目视和文本双重核验：

- 年报第 12-13 页：主营业务、云端/边缘产品、IP 与软件。
- 年报第 24-25 页：核心竞争力、研发迭代和行业竞争风险。
- 年报第 39 页：董事会、公司治理和信息披露机制。
- 一季报第 1-2 页：主要财务数据。
- 一季报第 3 页：收入和利润变化原因。

候选结构化 Fact：

| 指标 | 2026Q1 | 单位 | 定位 |
|---|---:|---|---|
| 营业收入 | 2,884,696,746.86 | CNY | 一季报第1页 |
| 归母净利润 | 1,013,213,581.94 | CNY | 一季报第1页 |
| 经营现金流净额 | 833,967,832.10 | CNY | 一季报第2页 |

### 688041.SH 海光信息

| 文件 | 上交所披露 | 页数 | SHA-256 |
|---|---|---:|---|
| [2025年年度报告](https://star.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-04-08/688041_20260408_L1O8.pdf) | 2026-04-08 | 231 | `abe3c5f8...b1106c02` |
| [2026年第一季度报告](https://star.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-04-08/688041_20260408_8IRS.pdf) | 2026-04-08 | 12 | `10689264...3b616be` |

已目视和文本双重核验：

- 年报第 12-13 页：CPU/DCU 主营业务和应用领域。
- 年报第 29-30 页：研发、知识产权、客户及供应商集中风险。
- 年报第 22 页：产业生态、知识产权管理和治理机制。
- 一季报第 1-2 页：主要财务数据。
- 一季报第 3 页：AI 算力需求、产品迭代、研发投入和现金流变化原因。

候选结构化 Fact：

| 指标 | 2026Q1 | 单位 | 定位 |
|---|---:|---|---|
| 营业收入 | 4,033,592,186.34 | CNY | 一季报第1页 |
| 归母净利润 | 687,094,336.71 | CNY | 一季报第1页 |
| 经营现金流净额 | 67,617,712.14 | CNY | 一季报第2页 |

### 688981.SH 中芯国际

| 文件 | 上交所披露 | 页数 | SHA-256 |
|---|---|---:|---|
| [2025年年度报告](https://star.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-03-27/688981_20260327_AQDC.pdf) | 2026-03-27 | 232 | `c022cf93...9ef35531` |
| [2026年第一季度报告](https://star.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-05-15/688981_20260515_4MY8.pdf) | 2026-05-15 | 15 | `86d567a9...1366cd5` |

已目视和文本双重核验：

- 年报第 13-14 页：晶圆代工主营业务、服务和经营模式。
- 年报第 19-20 页：研发迭代、行业周期和供应链风险。
- 年报第 36 页：董事会、股息政策及公司治理。
- 一季报第 3-4 页：财务和经营数据。
- 一季报第 2 页：二季度收入及毛利率指引。该内容必须标记为前瞻性陈述，不能写成已实现 Fact。

候选结构化 Fact：

| 指标 | 2026Q1 | 原始单位 | 定位 |
|---|---:|---|---|
| 营业收入 | 17,617,218 | 千元人民币 | 一季报第3页 |
| 归母净利润 | 1,361,209 | 千元人民币 | 一季报第3页 |
| 经营现金流净额 | 5,131,729 | 千元人民币 | 一季报第3页 |

中芯国际的原始报表单位为千元；生产入库时应保留原始单位或通过有审计记录的确定性换算生成派生 Fact，不能静默改写单位。

## 3. 建议 Evidence 映射

每只建议建立 5 条短 Evidence：

| Evidence domain | 来源 | 目标维度 |
|---|---|---|
| `business_position` | 年报主营业务页 | business_exposure |
| `risks` | 年报风险因素页 | risks |
| `company_actions` | 年报治理页 | holder_governance、material_events |
| `financial_statements` | 一季报财务页 | earnings_trend |
| `catalysts` | 一季报变化原因或正式指引页 | catalysts |

估值继续排除。没有与分析截止严格对齐的可验证市场快照时，不创建 valuation Evidence 或 Claim。

## 4. Evidence Pack 与 Token 预演

预演使用了当前 P4B2 实际 `_model_payload` 和 `conservative_token_upper_bound`，不是按自然语言经验估算。假设：

- 每只 2 个 SourceDocument；
- 每只 5 条 Evidence；
- 每条 Evidence 摘要最多 120 个汉字；
- 每只 3 条财务 Fact；
- 无旧 Claim、无冲突、无估值内容。

| 成分 | 保守输入上界 | 单只上限 | 预计输出上限 |
|---|---:|---:|---:|
| 688256.SH | 5,301 | 6,000 | 600 |
| 688041.SH | 5,307 | 6,000 | 600 |
| 688981.SH | 5,307 | 6,000 | 600 |
| 全批 | 15,915 | 18,000 | 1,800 |

结论：当前已授权 Token 足够，暂不需要扩容。剩余输入余量为 2,085；应通过短摘要、精确页码和少量结构化 Fact 控制上下文，而不是把整份年报交给模型。

随后在生产库的 SQLite 临时副本中，使用真实 `ResearchKnowledgeStore.register_bundle`、`ComponentResearchEvidencePackBuilder` 和 `_model_payload` 完成了合同级仿真。每只实际写入临时库 5 条 Evidence、3 条 Fact，均无冲突：

| 成分 | 仿真 Pack 质量 | 核心覆盖率 | 仿真实际输入上界 |
|---|---|---:|---:|
| 688256.SH | complete | 1.0 | 4,975 |
| 688041.SH | complete | 1.0 | 4,970 |
| 688981.SH | complete | 1.0 | 4,992 |
| 全批 | complete | 1.0 | 14,937 |

仿真覆盖 business_exposure、earnings_trend、catalysts、risks、material_events 和 holder_governance，仅保留 valuation 缺口。这证明 `complete` 与预算判断经过了真实合同和构建器验证，不只是文档推测。

## 5. 生产边界复核

本阶段只将六份官方 PDF 下载到工作区临时目录并渲染选定页面：

```text
C:\Users\23479\Documents\GitHub\Vibe-Trading\tmp\pdfs\p4b2-evidence
```

生产库复核结果：

- 三只证券的 `evidence_records=0`、`fact_records=0`，与阶段开始前一致。
- P4B2 仍为 Evidence Pack 3、Plan 1、blocked Job 3、Budget Ledger 0、Publish Result 0。
- P4B2 实际模型调用和输入/输出 Token 均为 0。
- `component_research` Report 仍为 0。
- 统一知识总数仍为 SourceDocument 186、Evidence 191、Fact 2,302、Claim 1,606、报告目录 119、知识关系 134。

真实数据库文件的大小和 mtime 在窗口内发生变化，说明运行服务存在其他数据库活动；但上述所有 P4B2 和统一知识范围计数保持不变。本阶段因此只能确认“没有本范围 Source/Evidence/Fact/P4B2 写入”，不能声称整个运行库在窗口内绝对无写入。

## 6. 下一门槛

当前六个来源已足以进入“人工/确定性复核后生产入库”阶段，但该阶段会新增 SourceDocument、Evidence 和 Fact，属于新的生产写入权限，不包含在本次只读来源审计中。

建议下一阶段继续保持模型调用为 0，先完成：

1. 创建生产一致性备份。
2. 按清单校验 PDF 哈希、证券代码、披露时间、页码和原始单位。
3. 写入 6 个 SourceDocument、15 条短 Evidence 和 9 条结构化财务 Fact。
4. 重新运行 P4B1 和 P4B2 Evidence Pack；只有三只均达到 `complete` 才生成新的授权 Plan。
5. 将生产入库结果和实际新 Plan Token 预算提交复核，再决定是否执行模型。
