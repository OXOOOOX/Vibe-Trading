# Deep Research、Report 与周报生产链 v2 修订计划

> 状态：已实施并完成黄金样本验收  
> 制定日期：2026-07-20  
> 完成日期：2026-07-20  
> 适用范围：个股/ETF Deep Research、正式 Report、统一报告目录、Markdown/PDF 和正式周报  
> 关系说明：本计划补充并修订现有 Deep Research、ETF 复用和报告目录计划，不覆盖其历史实施记录。

## 一、目标与实施原则

本轮工作的核心顺序为：

> 先保证正式 Report 只消费合格证据，再开放给周报复用，最后优化 Markdown/PDF 展示。

实施原则：

- 机器 JSON 是唯一结构化事实来源；Markdown/PDF 只是面向用户的派生视图。
- 正式报告不得把弱来源或搜索摘要包装成已核验核心事实。
- 缺口按 Claim 或数据范围局部降级，不得无故把整份报告判为不可用。
- 周报保持零模型调用，不隐式启动 Deep Research 或组件研究。
- 报告、监控和交易继续解耦；报告生产不得自动激活监控或执行交易。
- 旧报告保持只读和可追溯，不批量重写正式历史产物。

## 二、统一正式报告数据契约

### 2.1 Claim 支撑等级

为正式报告和统一目录中的 Claim 增加冻结的支撑状态：

```text
verified
triangulated
conflicted
weak
insufficient
```

发布规则：

- `verified`、`triangulated`：允许进入核心结论和周报复用上下文。
- `conflicted`：必须并列展示冲突口径，禁止自动选取更有利数字。
- `weak`：只能作为“单源参考”或“待验证线索”，不得进入核心摘要。
- `insufficient`：只生成数据缺口，不生成方向性结论。
- 确定性计算只有在全部输入事实合格时，才能继承为可复用结论。

报告登记时，将 `claim_support_audit.json` 的结果持久化到知识库。新增字段：

```text
support_status
support_reason
support_audit_version
fact_ids
evidence_ids
audited_at
```

原有 `claim_status` 继续表示 Claim 生命周期，不与支撑等级混用。

### 2.2 官方来源优先级

事实选择优先级固定为：

1. 已验证、已结构化的官方法定披露；
2. 官方原文中的可定位事实；
3. 两个独立来源交叉验证；
4. 单一结构化行情或财务提供方；
5. 搜索摘要或二手线索。

约束：

- 官方事实存在时，报告不得选择低优先级同口径事实作为主要依据。
- 单一结构化提供方数据必须显式标记为“单源参考”。
- 搜索摘要不得直接支持重大 Claim。
- 来源冲突必须保留原始口径、时点、优先级和最终选择原因。

000651.SZ 下一份黄金报告必须优先使用已经入库并验证的巨潮资讯 2025 年年度报告，不得继续把无公开链接的 Eastmoney 快照写成“可核验核心事实”。

### 2.3 模块状态 v2

废弃递归的 `deterministic_analysis.details`，改为扁平结构：

```json
{
  "module_id": "holding_penetration",
  "availability": "complete | partial | missing | not_applicable",
  "validation": "passed | warning | failed",
  "coverage": 0.6,
  "reason_code": "component_research_partial",
  "missing_items": [],
  "narrative_result": {},
  "deterministic_result": {},
  "details": {}
}
```

约束：

- `details` 中禁止再次嵌套完整 ModuleState。
- 一个局部数据缺口只能把模块标为 `partial`，不能把整章标为缺失。
- 报告级 `quality_status` 只能由最终模块状态统一派生。
- 首页“尚待补充”只能根据最终 `missing_items` 生成。
- 为旧报告提供只读兼容适配器，但不重写历史 Manifest。

## 三、修正 Deep Research 与 Report 输出

### 3.1 标的身份统一

名称优先级固定为：

1. 官方标的档案简称；
2. 官方产品全称；
3. 用户输入或持仓名称仅作为别名。

验收要求：

- 588870.SH 正式标题为“科创50ETF汇添富”。
- 基金全称在产品详情中展示。
- “科创芯片ETF”“科创50指”不得再成为正式标题。
- ETF 日报统一称“ETF晨报”。
- 输出中保留名称来源和标的档案快照 ID。

### 3.2 ETF 穿透结果

ETF 报告必须在模块顶层直接输出：

```text
selection_id
universe_snapshot_id
selected_count
selected_weight_coverage
explanation_coverage
research_coverage
fully_supported_coverage
partial_reusable_count
missing_count
selected_components[]
```

588870.SH 当前黄金结果应满足：

- 选择成分 5 个；
- 部分可复用研究 3 个；
- 缺失研究 2 个；
- 目录中的 `selected_count` 和覆盖率不得再为 `null`；
- 不得继续声称“没有可验证的成分选择快照”；
- 缺少两个组件研究只降低组件研究覆盖，不得把成分暴露整体判为缺失。

### 3.3 中央中文术语层

建立 Deep Research、日报、周报共用的中央术语表，覆盖：

- 财务指标；
- ETF 产品指标；
- 来源状态；
- 模块状态；
- 版本差异字段；
- 监控条件；
- 数据质量和缺口原因。

用户版 Markdown/PDF 禁止出现以下机器术语：

```text
parent equity
inventory
receivables
unit nav
quarterly
official_primary
source_recorded
global_coverage
```

机器 JSON 继续保留英文键。遇到未登记术语时，用户版编译校验失败，不再使用 `replace("_", " ")` 生成英文兜底。

### 3.4 引用与参考资料

每项参考资料至少提供：

```text
发布者
资料标题
公开链接或内部稳定索引码
发布日期或数据时点
获取时间
来源等级
```

约束：

- 禁止输出多个无法区分的“eastmoney，eastmoney”。
- 内部报告可以使用稳定索引码，但必须继续穿透到原始 Evidence。
- 引用正文、Fact、Evidence、来源文档之间必须能双向追溯。

## 四、PDF 与用户交付契约

保留 PDF 懒生成机制，但明确区分：

```text
materialization_status:
  generatable
  materialized
  failed
```

规则：

- 报告库登记时允许 PDF 为 `generatable`。
- 用户预览、下载、飞书发送或其他正式交付前，必须物化 PDF。
- PDF 物化失败时不得继续显示为可下载。
- PDF 必须由 Markdown 或结构化 ViewModel 派生，不维护第二份正文。

用户版要求：

- 不重复标题；
- 增加页码、标的、报告日期和版本页脚；
- 用户正文无英文机器术语；
- 长哈希、内部 ID 和完整审计信息只保留在 JSON；
- 数据依据使用紧凑表格；
- 主要阅读内容保持清晰，详细来源集中放在报告末尾；
- PDF 生成后渲染全部页面，检查截断、重叠、黑块、乱码和异常空白。

## 五、报告目录与周报复用

### 5.1 报告目录接口

`current.daily`、`current.weekly`、`current.structural` 中的摘要、风险和待验证项统一返回：

```json
{
  "claim_id": "...",
  "section_id": "...",
  "text": "...",
  "support_status": "verified",
  "fact_ids": [],
  "evidence_ids": [],
  "data_as_of": "...",
  "valid_until": "..."
}
```

目录规则：

- 失败和诊断报告不得成为 `current`。
- 修订报告继承同一 `family_id`。
- 默认时间线只展示每个家族的当前版本。
- 完整历史通过显式历史模式查看。
- `latest` 与 `latest_complete` 继续分别保留。
- 目录不得丢失 Claim 的支撑等级和证据血缘。

### 5.2 周报上下文装配器

新增确定性的 `WeeklyContextAssembler`，冻结：

```text
市场行情
最新合格日报
最新完整日报
最新结构性报告
ETF 产品档案
跟踪指数
份额与溢折价
成分选择
组件研究覆盖
来源与证据
```

选择规则：

- 日报 `market_date` 必须不晚于 `week_end`。
- 市场类字段 `data_as_of` 不得晚于周截止时间。
- 结构报告只提供结构事实、风险和观察对象。
- Deep Research 不得覆盖周报自行计算的价格、趋势和关键位。
- 周报只消费 `verified`、`triangulated` Claim。
- `weak` Claim 只能进入“待验证事项”。
- 周报不得读取或解析旧报告 Markdown。
- 失败、诊断、过期和未来数据必须被排除并记录原因。

### 5.3 `weekly_review_v2`

保留 v1 历史读取，默认生成切换到 v2。新增：

```text
data_scopes
cross_horizon_context
etf_context
context_fingerprint
source_manifest
```

ETF 数据范围拆分为：

- 跟踪指数；
- 指数相对强弱；
- 基金份额；
- 溢折价；
- 跟踪误差或 IOPV；
- 成分暴露；
- 成分研究覆盖。

不得再固定写入“ETF 范围不可用”。

周报继续保持：

- 零模型调用；
- 不隐式启动 Deep Research；
- 不自动生成组件研究；
- 不自动激活监控或执行交易；
- 同周修订仍由 `force_new` 显式触发。

## 六、测试与验收

### 6.1 黄金样本

#### 000651.SZ

- [x] 核心财务事实引用官方 2025 年年度报告。
- [x] 核心结论至少由 `verified` 或 `triangulated` Claim 支撑。
- [x] 弱来源不能冒充正式核心事实。
- [x] 参考资料包含可打开的官方链接。
- [x] Markdown/PDF 不出现英文财务机器键。

#### 588870.SH

- [x] 正式名称为“科创50ETF汇添富”。
- [x] 正确显示 5 个选择成分、3 个部分复用、2 个缺失。
- [x] 跟踪指数、份额、溢价和成分范围不再被整体标记为缺失。
- [x] 局部组件缺口只降低对应数据范围。
- [x] Markdown/PDF 不出现英文状态码和产品机器键。

### 6.2 自动化发布门禁

- [x] 用户版英文机器术语扫描。
- [x] 核心 Claim 支撑等级检查。
- [x] 官方来源优先级检查。
- [x] 模块嵌套深度检查。
- [x] 首页缺口与模块状态一致性检查。
- [x] ETF 官方名称检查。
- [x] 引用公开链接或内部索引检查。
- [x] PDF 物化、页码、标题和页面渲染检查。
- [x] 未来数据拒绝检查。
- [x] 周报零模型调用检查。
- [x] 周报不得固定报告已有 ETF 数据缺失。
- [x] 旧版报告和接口兼容测试。

## 七、实施与发布顺序

### P0：正式报告证据质量

- [x] 持久化 Claim 支撑等级。
- [x] 实现官方事实优先选择。
- [x] 阻止弱 Claim 进入核心摘要和周报复用。
- [x] 修复引用中的无链接、重复发布者和来源等级缺失。

### P1：结构与用户输出

- [x] 上线扁平 ModuleState v2。
- [x] 增加旧 ModuleState 只读适配器。
- [x] 修复 ETF 成分穿透顶层字段。
- [x] 完成中央中文术语表。
- [x] 统一官方标的名称。
- [x] 完成 PDF 物化状态和页脚/页码。

### P2：黄金报告验收

- [x] 使用最新代码重新生成 000651.SZ。
- [x] 使用最新代码重新生成 588870.SH。
- [x] 强制物化两份 PDF。
- [x] 完成 Markdown、JSON、引用、Claim、模块状态和逐页 PDF 验收。

### P3：报告目录复用

- [x] 目录返回可复用 Claim 的支撑等级和血缘。
- [x] 修订报告继承相同 `family_id`。
- [x] 默认时间线压缩为当前版本。
- [x] 验证 `latest` 和 `latest_complete` 选择正确。

### P4：周报 v2

- [x] 实现 `WeeklyContextAssembler`。
- [x] 实现 `weekly_review_v2`。
- [x] 接入 ETF 产品、指数、份额和成分范围。
- [x] 完成 v1/v2 兼容与同周修订对照。
- [x] 验收通过后将 v2 设为默认。

## 八、完成定义

只有同时满足以下条件，本计划才可标记为完成：

- 000651.SZ 和 588870.SH 黄金报告通过全部结构化与视觉验收；
- 核心结论不存在仅由 `weak` 或 `insufficient` 支撑的情况；
- 用户版 Markdown/PDF 无未登记英文机器术语；
- ETF 名称、成分数量和研究覆盖与官方档案、P4A/P4B 结果一致；
- 报告目录能够输出带支撑等级和证据血缘的可复用 Claim；
- `weekly_review_v2` 不再把已具备的 ETF 数据固定标记为缺失；
- 周报全流程模型调用数保持为 0；
- 所有相关既有测试和新增发布门禁通过；
- 旧报告保持可读、可下载和可追溯。

## 九、实施验收记录

### 9.1 黄金穿透报告

| 标的 | 正式报告 | 验收结果 | 已知且如实保留的缺口 |
|---|---|---|---|
| 000651.SZ 格力电器 | `report_a5a0af17dd684338`（第 9 版） | `passed_with_gaps`；官方年报优先；Markdown/PDF 已物化并逐页检查 | 缺少连续三年、可追溯的前瞻一致预期，未生成隐含预期和长期情景伪精确值 |
| 588870.SH 科创50ETF汇添富 | `report_81fe2719bb4f4119`（第 24 版） | `passed_with_gaps`；5 个选择成分、3 个部分复用、2 个缺失；产品/指数/份额/折溢价可用 | 688008.SH、688012.SH 暂无可复用组件研究；聚合基本面、相对强弱和跟踪误差仍待补充 |

两份报告都是完整、可交付的正式报告；`passed_with_gaps` 表示报告明确披露资料边界，不表示系统已经取得市场上所有可能数据。

### 9.2 周报 v2

最终验收运行：

- 000651.SZ：`wrr_20260717_000651_SZ_r6_3493f15f`。
- 588870.SH：`wrr_20260717_588870_SH_r8_65e825fd`。
- 两份周报模型调用数均为 `0`，未自动启用监控、发送提醒或执行交易。
- 588870.SH 已接入产品档案、跟踪指数、基金份额、折溢价、成分暴露与成分研究范围；仅相对强弱和跟踪误差保持缺口。
- 000651.SZ 的结构报告数据时点晚于周截止日，周报按未来数据门禁正确排除，没有回填未来信息。
- 用户版 Markdown/PDF 已移除内部长编号、重复标题和新增场景的逐字段“未设置”噪声。

### 9.3 自动化与视觉门禁

- 聚焦回归测试覆盖知识库、正式报告、报告目录、周报 v2、PDF API、ETF 桥接和日报兼容。
- 四份最终 PDF 均已渲染全部页面检查；未发现截断、重叠、乱码、异常黑块或标题重复。
- 机器 JSON 继续保留英文枚举和内部 ID；用户版 Markdown/PDF 通过中央中文术语层输出。
