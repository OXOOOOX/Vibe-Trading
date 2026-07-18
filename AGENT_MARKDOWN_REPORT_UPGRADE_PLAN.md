# Agent Markdown 增量报告升级计划

状态：方案已收敛，尚未实施
首期范围：用户显式选择的深度研究报告；普通聊天和组合 `daily_update` 保持现状
核心目标：让报告长度由证据覆盖和未解决问题决定，而不是由一次 LLM completion 的典型长度决定

## 1. 已确认的产品决策

1. 不全量替换现有 AgentLoop。普通问答继续使用当前一次性 Assistant Markdown，默认行为和延迟不变。
2. 新增每次消息独立选择的 `auto | chat | deep_report` 模式；报告模式不是 Session 永久属性。
3. 组合晨报现有 `HoldingDailyBrief JSON -> 组合约束 -> 确定性 Markdown/PDF` 链路继续作为事实和决策主链，不允许自由 Markdown 取代严格 JSON。
4. `deep_research` 才使用增量成稿；首期只由用户主动触发，不根据关键词或“重大变化”自动升级。
5. 第一版在后台分阶段成稿，前端只显示章节和校验进度，不开放用户与 Agent 同时编辑草稿。
6. Agent 只按稳定 `section_id` 写入或替换单个章节，不允许反复覆盖整篇 Markdown，也不复用现有基于 `old_text` 的通用 `edit_file` 完成报告编辑。
7. 行情、持仓、财务数字、来源和时间戳属于不可变证据层。Agent 可以引用和解释，不能改写原始事实。
8. 报告没有最低字数，也不在提示词中要求“约 X 字”。停止条件是必答问题已回答、证据不足已显式标记、质量门禁通过。
9. 字数、模型调用次数、token 和耗时只设置安全硬上限；触发上限时优先保留必答结论和风险，不用套话填充，也不静默截断。
10. 显式请求深度报告后，如果增量链路失败，不得静默降级成普通一次性报告并伪装成功；保留可恢复草稿并返回真实状态。
11. 用户后续要求修改某一章节时创建新的报告 revision，旧版本保持可追溯；不原地覆盖已交付的最终报告。
12. 全流程继续遵守研究边界、数据 `actionability` 和组合防护规则，不增加任何真实订单写入能力。
13. “Agent Markdown”在本计划中指仓库内实现的结构化、分章节、可校验 Markdown 协议，不引入名称相近但契约不明确的第三方运行时依赖。
14. `auto` 只负责研判和推荐，不得未经用户确认直接启动高成本 Deep Report；明确简单的问题可以直接进入快速回答。
15. 用户显式选择 `chat` 或 `deep_report` 时始终尊重选择，不再由 Router 覆盖；Router 也不能仅凭提示词长度推荐深度报告。

## 2. 当前问题与升级边界

### 2.1 当前链路

- 普通 Session 的 Agent 可以多轮调用工具，但没有工具调用后，最终内容仍由一次 completion 形成并直接写入 Assistant Message。
- 通用 `write_file` 会覆盖完整文件；`edit_file` 只替换第一个匹配文本，缺少章节身份、revision 冲突、证据引用和 Markdown 结构校验。
- 组合日更 Worker 输出严格 JSON，随后由程序聚合并渲染 Markdown/PDF。这条链路解决了事实和约束问题，但叙述深度受固定 JSON 字段和模板限制。
- 现有 PDF 能接收较长 Markdown，但上游最终正文仍受单次 completion 的规划和输出长度约束。

### 2.2 要解决的问题

- 单次生成必须同时决定结构、篇幅、论证深度和收尾位置，容易向模型熟悉的“典型报告长度”收敛。
- 前部章节占用过多输出预算后，后部风险、反证和结论容易被压缩。
- 新证据在后半程出现时，模型无法回头重组已经输出的前文。
- 单纯提高 `max_tokens` 只能放宽硬上限，不能提供全局修订、证据覆盖或矛盾检查。
- 长报告失败后只能整篇重跑，成本高且容易丢失已经正确的内容。

### 2.3 第一版不做

- 不改变普通聊天默认模式。
- 不让用户实时修改尚在生成的共享草稿。
- 不允许多个 Agent 并发修改同一章节。
- 不把所有工具原始输出、完整网页或完整文档塞入每个章节的上下文。
- 不以最终字数、页数或“看起来更长”作为发布指标。
- 不自动重写组合决策 JSON、持仓事实、预算检查或数据门槛。
- 不在第一版自动触发 `material_change -> deep_research`；该能力在人工触发版本通过评测后再开放。

## 3. 目标架构

```text
用户选择“智能选择”
  -> ReportIntentRouter 低成本研判
  -> 简单问题：直接快速回答
  -> 复杂问题：展示推荐依据、预估耗时并等待用户确认
用户确认“深度报告”
  -> 冻结请求、Session 上下文和数据输入
  -> 研究工具取数并登记 Evidence/Fact
  -> 建立 Research Questions 与章节计划
  -> 为每个章节检索局部证据
  -> Agent 按 section_id 写入章节
  -> 确定性拼装 draft.md
  -> 规则校验 + 全局 Critic 只产出问题清单
  -> Agent 仅修复失败章节
  -> 再校验
  -> 冻结 final.md
  -> 渲染 PDF、写 Artifact Manifest
  -> Session 返回摘要、质量状态和 Artifact
```

新增 `DeepReportService` 作为报告生命周期控制器。它负责状态机、预算、恢复、章节调度、校验、拼装和交付；现有 AgentLoop 继续负责工具型研究能力，但不能自行宣告报告完成。

为避免普通会话行为漂移，`SessionService._run_attempt()` 根据 Attempt 的 `response_mode` 明确路由：

```text
chat         -> 当前 AgentLoop
deep_report  -> DeepReportService
```

两条链路共享模型配置、研究工具注册、权限策略、Portfolio Guard、事件总线和用量统计，但使用不同的最终成稿协议。

### 3.1 智能模式路由

新增 `ReportIntentRouter`，解决“用户不应该先理解系统内部模式，才能得到合适报告”的问题。Router 只判断交付形态，不做研究、不调用行情/持仓写工具，也不生成报告正文。

Router 输出：

```json
{
  "schema_version": 1,
  "router_version": "report-router-v1",
  "decision": "quick_answer",
  "complexity_score": 2,
  "confidence": 0.94,
  "reason_codes": ["single_question", "single_target", "no_artifact_requested"],
  "suggested_profile": null,
  "estimated_sections": 0,
  "user_confirmation_required": false
}
```

`decision` 只允许：

```text
quick_answer                 直接进入普通 chat
recommend_deep_report        推荐 Deep Report，等待用户确认
recommend_existing_artifact  优先查看或修订已有报告，不重复生成
clarify_scope                目标或交付物不清楚，先提出一个必要问题
```

路由分两层：

1. 硬规则优先：用户明确选择模式、明确引用“刚才/这份报告”、已有可复用 Artifact、单一事实查询、目标缺失等可确定情况，不调用 LLM 分类器。
2. 只在规则无法确定时调用一次轻量结构化分类；分类器只能返回 Router JSON，不能调用研究工具或直接启动 Deep Report。

复杂度使用六个可解释维度，每项固定计分，总分 0~10：

```text
目标范围              0~2   单标的/单主题 -> 多标的/组合/跨市场
问题数量              0~2   一个独立问题 -> 四个以上相互关联问题
证据与来源多样性      0~2   单一事实源 -> 多文档、多数据域、附件交叉验证
全局一致性要求        0~2   单结论 -> 情景、反证、风险和跨章节一致性
Artifact/修订需求     0~1   是否明确要求正式报告、PDF、保存或修订旧报告
时间与比较维度        0~1   是否包含跨期、同业、情景或归因比较
```

决策门槛：

- `0~3`：`quick_answer`。
- `6~10`：`recommend_deep_report`。
- `4~5`：调用轻量分类器；分类器置信度低于 `0.70` 时默认快速回答，并在回答末尾提供非阻塞的“生成深度报告”入口。
- 明确要求正式报告/PDF且问题不是单纯格式转换时，最低提升为 `recommend_deep_report`。
- 数据明显不足时不能把“多跑 Deep Research”描述成解决办法；Router 应推荐先补数据或澄清范围。
- 提示词字符数不参与评分。长粘贴文本可能只是输入资料，短提示也可能要求复杂的组合研判。

需要推荐时展示具体原因，例如“涉及 8 个持仓、4 类证据和跨期比较”，而不是笼统显示“该问题较复杂”。Router 可以给出基于历史遥测的耗时区间；样本不足时只显示“耗时和调用量高于快速回答”，不得伪造精确分钟或费用。

Router 永远不自动产生 Deep Report 费用。用户可以选择：

```text
[使用深度报告] [继续快速回答] [调整分析范围]
```

选择结果写入 Message/Attempt metadata，作为后续 Router 评测数据，但不沉淀成跨 Session 的永久偏好，除非用户以后显式设置。

## 4. 请求与运行契约

### 4.1 消息 API

扩展 `SendMessageRequest`，保持旧客户端兼容：

```json
{
  "content": "分析目标和要求",
  "response_mode": "chat",
  "report_profile": null,
  "routing_decision_id": null
}
```

字段规则：

- `response_mode`: `auto | chat | deep_report`；字段缺失时仍按 `chat`，保证旧客户端行为不变。
- `report_profile`: `generic_research | equity_deep_research | portfolio_deep_research | null`。
- `routing_decision_id`: 用户接受或拒绝 Router 推荐时携带的决策 ID。
- `response_mode=auto` 时先运行 Router；前端完成灰度后可以默认显式发送 `auto`，后端永远不把字段缺失解释成智能模式。
- `response_mode=chat` 时忽略 `report_profile`。
- `response_mode=deep_report` 且 profile 为空时，由 Planner 在已启用 profile 中选择并持久化选择依据。
- API 不向普通用户暴露 token、章节数和字数硬上限；这些由服务端配置治理。

Dispatcher 已有 `source_metadata` JSON，直接传递 `response_mode` 和 `report_profile`，不增加 dispatch 数据库列。`Message.metadata` 和新增的 `Attempt.metadata` 同时保存选择，保证排队、重启、编辑重跑和审计后仍可恢复原模式。

深度报告使用 `attempt_id` 作为稳定 `report_id`，API 入队响应增加：

```json
{
  "job_id": "...",
  "message_id": "...",
  "attempt_id": "...",
  "report_id": "...",
  "status": "queued"
}
```

### 4.2 Router 确认协议

`response_mode=auto` 时，消息 API 先执行 Router：

- `quick_answer`：立即以 `chat` 入队，返回正常 `job_id/message_id/attempt_id`，并附 Router 决策。
- `recommend_existing_artifact`：返回已有 Artifact 摘要和“查看/创建修订/仍然重新生成”操作，不直接入队新报告。
- `clarify_scope`：返回一个必要澄清问题，不启动 AgentLoop。
- `recommend_deep_report`：返回 `confirmation_required`，尚不持久化用户消息、不创建 Attempt、不调用研究工具。

推荐响应：

```json
{
  "status": "confirmation_required",
  "routing_decision_id": "route_xxx",
  "decision": "recommend_deep_report",
  "complexity_score": 8,
  "reason_codes": ["multi_target", "cross_period", "formal_artifact"],
  "suggested_profile": "portfolio_deep_research"
}
```

在 `dispatch.db` 增加 `report_routing_decisions` 表，保存 `decision_id`、`session_id`、`content_hash`、Router 版本、输入特征、输出、状态、选择结果、创建时间和 30 分钟过期时间。确认时必须匹配 Session 和原文 hash，并在同一事务中将决策从 `pending` 改为 `accepted_chat | accepted_deep | expired`，防止双击重复入队。

用户点击后重新提交同一 content、显式 `chat | deep_report` 和 `routing_decision_id`。服务端校验后才创建 Message/Attempt。服务重启不丢失待确认决策；过期后重新研判。Router 推荐本身不是 Assistant 分析结论，不写入对话正文，只以推荐卡呈现。

### 4.3 报告查询 API

新增：

```text
GET  /reports/{report_id}
GET  /reports/{report_id}/artifacts/{artifact_id}
POST /reports/{report_id}/resume
POST /reports/{report_id}/revisions
```

- `GET` 返回状态、阶段、章节进度、校验结果摘要、预算使用和公开 Artifact，不返回绝对本地路径。
- `resume` 只恢复 `failed | interrupted` 报告，从第一个未通过章节继续；已经通过且输入 hash 未变化的章节不得重写。
- `revisions` 接受用户修改要求和目标 `section_id`，基于已交付 revision 创建新 revision；不能覆盖旧 Artifact。
- Session SSE 仍是实时进度主通道，报告查询 API 用于刷新、重启恢复和跨终端查看。

### 4.4 Attempt 兼容

`Attempt` 新增默认空字典：

```python
metadata: Dict[str, Any] = field(default_factory=dict)
```

旧 Attempt JSON 没有该字段时按 `{}` 加载并视为 `response_mode=chat`，不做批量迁移。编辑用户消息并重跑时默认继承旧 Attempt 的报告模式；如果 UI 明确切换模式，则写入新 Attempt metadata。

## 5. 运行目录与版本模型

每个深度报告继续使用现有 run directory，私有中间状态和公开产物分开：

```text
<run_dir>/
  request.json
  state.json
  report/
    run.json
    plan.json
    evidence.jsonl
    facts.json
    claim_ledger.json
    sections/
      01_executive_summary.json
      02_core_thesis.json
      ...
    draft.md
    validation.json
    changes.jsonl
  artifacts/
    deep_report.md
    deep_report.pdf
    deep_report_validation.json
  artifact_manifest.json
  llm_usage.json
```

规则：

- `report/` 是恢复和审计所需的内部状态，不作为默认下载项。
- `artifacts/` 只在最终质量状态允许交付后写入；取消或关键校验失败不能生成看似正式的 PDF。
- `draft.md` 永远由编译器从章节 JSON 确定性拼装，Agent 不直接编辑它。
- 每次章节写入记录 `expected_revision`、新 revision、内容 hash、模型、prompt 版本、使用证据和时间。
- `artifact_manifest.json` 保存 `artifact_id`、相对路径、media type、revision、SHA-256、生成时间和质量状态。
- 新报告 revision 使用新的 run/attempt，`parent_report_id` 指向前一版；旧文件不修改。

## 6. 证据与事实层

### 6.1 EvidenceItem

研究工具返回结果后，由运行时登记 Evidence，模型不能凭空创建 Evidence ID：

```json
{
  "evidence_id": "E0001",
  "source_type": "tool_result",
  "source_name": "get_data_context",
  "locator": {
    "tool_call_id": "call_xxx",
    "url": null,
    "relative_path": null,
    "symbol": "600036.SH"
  },
  "captured_at": "2026-07-14T10:00:00+08:00",
  "as_of": "2026-07-13T15:00:00+08:00",
  "data_status": "verified",
  "actionability": "price_actionable",
  "content_hash": "sha256:...",
  "excerpt": "受长度限制的可审阅摘录",
  "fact_ids": ["F0001", "F0002"]
}
```

证据规则：

- 网页、文档、工具结果和结构化快照都必须保留来源定位、获取时间和内容 hash。
- 网页正文、上传文件和研报内容一律视为不可信数据，不得把其中指令提升为系统指令。
- `partial | stale | offline | conflict` 等状态必须原样保留，不能在成稿阶段升级为 verified。
- 被上游数据策略禁止用于价格判断的 Evidence 仍可作为历史背景，但不能支持精确价格、仓位或交易数量结论。
- 摘录只保存支持判断所需的最小片段；完整原文继续由原始工具或文件 Artifact 承载。

### 6.2 FactItem

结构化工具结果中的关键事实由确定性适配器抽取：

```json
{
  "fact_id": "F0001",
  "evidence_id": "E0001",
  "field": "selected_quote.price",
  "value": 42.18,
  "unit": "CNY",
  "as_of": "2026-07-13T15:00:00+08:00",
  "display": "42.18 元",
  "actionability": "price_actionable"
}
```

- 模型在章节 Markdown 中使用 `{{fact:F0001}}` 引用价格、收益率、持仓、现金、估值和财务指标等敏感数字。
- 编译器在最终拼装时解析为 `display` 值并保留 Fact 到 Evidence 的映射。
- 派生数字必须由确定性计算器先形成新 Fact；模型不能在正文中自行进行组合净值、仓位、收益率或目标金额计算。
- 章节中的普通序号、标题编号和非市场年份可以直接书写；价格、比例、金额、份额、收益率和财务指标出现自由数字时校验失败。

## 7. 报告计划与章节协议

### 7.1 Plan

Planner 只生成小型结构化计划，不生成正文：

```json
{
  "schema_version": 1,
  "report_id": "attempt_id",
  "profile": "equity_deep_research",
  "title": "...",
  "target": {"symbol": "600036.SH"},
  "research_questions": [
    {
      "id": "Q1",
      "question": "当前投资逻辑由哪些可核实事实支撑？",
      "required": true,
      "status": "open"
    }
  ],
  "sections": [
    {
      "section_id": "core_thesis",
      "title": "核心投资逻辑",
      "purpose": "回答 Q1 并区分事实、推断与不确定性",
      "question_ids": ["Q1"],
      "required": true,
      "status": "pending",
      "revision": 0
    }
  ]
}
```

Plan 校验要求：

- 最多 10 个正文 section；封面、来源附录和免责声明由编译器生成，不计入上限。
- 每个 required question 必须被至少一个 required section 覆盖。
- 同一 question 最多由两个 section 共同回答，避免重复铺陈。
- “风险与反证”“数据缺口与待验证事项”是所有投资类 profile 的必需 section。
- Plan 允许在正文生成前修改一次；进入 `drafting` 后冻结章节身份和顺序。后续只能修改章节内容，不能让模型边写边无限扩展目录。

### 7.2 SectionDocument

Agent 每次只提交一个章节：

```json
{
  "section_id": "core_thesis",
  "expected_revision": 0,
  "markdown": "章节正文，事实使用 {{fact:F0001}}，来源引用 [E0001]。",
  "question_statuses": [
    {"question_id": "Q1", "status": "answered", "note": "..."}
  ],
  "claims": [
    {
      "claim_id": "C0001",
      "kind": "inference",
      "text": "...",
      "evidence_ids": ["E0001"]
    }
  ],
  "used_fact_ids": ["F0001"],
  "unresolved_questions": [],
  "summary": "供相邻章节和全局校验使用的短摘要"
}
```

`kind` 只允许：

- `fact`：Evidence 直接支持的陈述。
- `inference`：由已列 Evidence 推导的分析，正文必须使用“表明、可能、推断”等合适措辞。
- `scenario`：条件情景或风险，不得写成已经发生的事实。
- `data_gap`：明确说明缺少什么以及它如何限制结论。

除 `data_gap` 外，每条 material claim 至少引用一个 Evidence。`data_gap` 必须引用失败状态或缺失域记录，不能用它规避证据登记。

### 7.3 专用工具

Deep Report registry 新增并只开放以下写工具：

```text
report_create_plan(plan)
report_get_section_context(section_id)
report_replace_section(section_id, expected_revision, payload)
report_get_validation()
```

- Evidence 由运行时拦截研究工具结果后登记，不提供任意 `report_add_evidence(text)`，防止模型自证。
- `report_replace_section` 使用 revision 乐观锁；revision 不匹配时拒绝写入并返回最新章节摘要。
- 深度成稿阶段不向模型提供通用 `write_file` 和 `edit_file`。
- `report_get_validation` 只返回与当前章节相关的问题和全局问题摘要，避免把整个运行目录重新放回上下文。
- 最终 `finalize` 只能由 `DeepReportService` 在质量门禁通过后执行，模型没有直接完成权限。

## 8. 长度控制与停止规则

### 8.1 不再使用目标长度

所有 deep report prompt 删除以下类型指令：

- “生成约 10,000 字报告”
- “每节至少 X 字”
- “尽量详细”但没有必答问题和证据门槛

正文长度由三件事决定：章节覆盖的问题、可用 Evidence 的密度、尚未解决的校验问题。证据不足的章节应简短说明限制，不允许为了达到体量重复背景或扩写常识。

### 8.2 第一版安全上限

默认服务端配置：

```text
max_sections                 10
max_report_chars             30000
max_section_chars             4500
max_model_calls                 18
max_total_tokens            120000
max_section_revisions            2
max_parallel_sections             2
max_section_input_tokens       32000
max_section_output_tokens       6000
```

- 字符数按最终 Markdown 的 Unicode 字符计算，不把 JSON metadata 计入报告长度。
- 没有最小字符数；报告可以明显短于上限。
- `max_section_revisions=2` 表示一次初稿加最多一次修复，不允许循环润色。
- 预算达到 80% 时停止生成 optional section，只完成 required question、风险和数据缺口。
- 达到硬上限时，如果 required question 都已形成合法状态且没有 critical issue，可交付 `passed_with_gaps`；否则状态为 `budget_exhausted`，保留内部草稿但不生成正式 PDF。
- 不把截断的模型输出写入章节。模型 `finish_reason=length` 时该次章节写入无效，缩小证据上下文后只重试一次。

### 8.3 完成条件

只有同时满足以下条件才结束：

1. 所有 required question 状态为 `answered | insufficient_evidence | not_applicable`，不存在 `open`。
2. 所有 required section 至少有一个通过校验的 revision。
3. `insufficient_evidence` 明确列出缺失数据和结论边界。
4. 没有 critical/high validation issue。
5. Portfolio Guard、Evidence 引用、敏感数字和 Markdown/PDF 校验通过。
6. Artifact hash 与当前 final revision 一致。

## 9. 上下文装配策略

每个章节调用只提供：

```text
固定系统规则和 profile rubric              <= 4k tokens
Plan、必答问题和当前章节目的                <= 2k tokens
与当前 question 相关的 Evidence/Fact        <= 20k tokens
当前章节上一 revision                       <= 4k tokens
相邻章节摘要和全局 Claim Ledger             <= 2k tokens
预留模型输出                                <= 6k tokens
```

证据检索先按目标标的、数据域、question_id、时效和来源状态过滤，再做相关性排序。默认最多返回 24 个 EvidenceItem；同一来源的重复条目先去重。

章节 Agent 不读取其他章节全文，只读取摘要和冲突 Claim。全局 Critic 在编译后的报告不超过 40,000 estimated tokens 时读取全文；超过时只读取 Plan、Claim Ledger、章节摘要和分块后的风险/结论交叉检查结果。

上下文压缩只压缩解释性文本，不能删除 Evidence ID、Fact ID、日期、来源状态、actionability 或 unresolved question。

## 10. 校验与修复

### 10.1 确定性校验

按以下顺序执行：

1. JSON Schema：Plan、Section、Evidence、Fact 和 Artifact Manifest 字段与枚举合法。
2. Revision：章节 `expected_revision` 与当前状态一致，输入 hash 未变化。
3. Coverage：所有 required question 有合法状态，必需章节存在。
4. Evidence：引用 ID 存在，来源定位和 content hash 完整，陈述没有使用被禁止的数据状态。
5. Fact：所有 `{{fact:*}}` 可解析；敏感自由数字被拒绝；派生 Fact 有确定性计算来源。
6. Portfolio Guard：`analysis_only`、低覆盖或数据冲突时，不出现精确价格、仓位比例、加减仓数量、止损或目标价。
7. Claim Ledger：同一实体、指标、期间或结论键不能出现互相冲突的值或动作。
8. Markdown：标题层级、表格、代码围栏、引用和链接合法；禁止脚本、危险 HTML 和隐藏内容。
9. Duplication：章节间归一化文本相似度过高或连续重复结论时生成 medium issue，不以机械删除破坏必要引用。
10. Render：final Markdown 可由现有渲染器生成有效 `%PDF-` 文件。

### 10.2 Critic

确定性校验通过后执行一次全局 Critic。Critic 只输出结构化 issue，不直接改正文：

```json
{
  "issues": [
    {
      "issue_id": "I001",
      "severity": "high",
      "section_ids": ["core_thesis", "risk"],
      "type": "semantic_conflict",
      "description": "...",
      "evidence_ids": ["E0001"]
    }
  ]
}
```

只把 high/medium issue 分发给关联章节修复。修复后重跑全部确定性校验；不再执行第二轮开放式 Critic。低严重度措辞偏好不阻塞交付，也不触发无限润色。

### 10.3 质量状态

```text
passed              required coverage 完整，无未解决 high/critical issue
passed_with_gaps    证据缺口已显式披露，无 high/critical issue
failed_validation   结构、证据、事实或安全门禁未通过
budget_exhausted    达到预算且未满足最低完成条件
cancelled           用户取消
interrupted         服务重启或运行时中断，可恢复
```

前端和飞书必须显示真实状态。只有 `passed | passed_with_gaps` 可以出现“最终报告/PDF”入口。

## 11. 状态机与事件

```text
queued
  -> freezing_inputs
  -> collecting_evidence
  -> planning
  -> drafting
  -> deterministic_validation
  -> critic_review
  -> repairing
  -> final_validation
  -> rendering
  -> completed | completed_with_gaps
```

任意运行阶段可以进入：

```text
failed | budget_exhausted | cancelled | interrupted
```

新增 SSE 事件：

```text
report.started
report.evidence_progress
report.plan_ready
report.section_started
report.section_completed
report.section_revised
report.validation_completed
report.rendering
report.completed
report.failed
```

事件只发送短摘要、计数、section_id 和状态，不在 SSE replay buffer 中发送整章正文。取消继续复用现有 Session cancel；DeepReportService 在模型流、章节写入和阶段边界检查同一个 cancellation token。

## 12. 前端体验

### 12.1 输入区

在现有 Agent 输入区增加模式选择：

```text
[智能选择] [快速回答] [深度报告]
```

- 灰度初期默认 `快速回答`；Router 独立门禁通过后，前端默认改为显式发送 `auto`。后端字段缺失仍按快速回答，避免旧客户端行为变化。
- 智能选择判断为简单问题时直接回答；判断应使用深度报告时先显示推荐卡，不自动开始。
- 选择深度报告时显示说明：“按证据和问题分章节生成，耗时和调用量高于快速回答。”
- Swarm 和 Deep Report 第一版互斥；用户选择一个后禁用另一个，避免两套编排器嵌套。

推荐卡必须展示 2~4 个具体 reason code 的用户可读翻译、建议 profile 和可用的耗时区间，并提供“使用深度报告 / 继续快速回答 / 调整范围”。用户明确选择快速回答后，本次请求不再二次弹出推荐。

### 12.2 进度卡

显示：

- 当前阶段和已用时间。
- 证据域覆盖与数据状态。
- 章节 `待开始 / 生成中 / 已完成 / 修复中 / 证据不足`。
- 已用模型调用和 token 预算百分比。
- 取消按钮。

第一版不流式展示正文，避免用户把未校验草稿当成结论。页面刷新后通过 `GET /reports/{report_id}` 恢复卡片。

### 12.3 完成卡

完成后 Assistant Message 只返回：

- 一段不超过 500 字的执行摘要。
- `passed` 或 `passed_with_gaps` 状态。
- 数据截至时间和关键缺口。
- Markdown/PDF Artifact 按钮。
- “修改本报告”入口；用户选择章节或用自然语言描述修改要求后创建新 revision。

## 13. 组合报告接入

### 13.1 保持不变

- 每日 `brief.json`、组合聚合、预算检查和确定性 `daily_update` Markdown/PDF 不改。
- 数据覆盖不足时仍在模型和 PDF 之前停止，不因为增量成稿而绕过分析门槛。
- 综合晨报继续拼接 daily update，并只索引 deep research Artifact。

### 13.2 Deep Research 输入

组合或个股深度报告冻结读取：

```text
portfolio_snapshot.json
mandate_snapshot.json
data_manifest.json
brief.json / aggregate.json / decision.json
上一份成功报告的 Claim Ledger 和结论摘要
用户本次问题
```

- 不读取未登记为 Artifact 的其他 Session 随机文本作为事实。
- 旧报告只用于变化对比，旧行情和旧结论不能覆盖当前 data manifest。
- `portfolio_deep_research` 的关键金额、仓位和动作继续由 aggregate/decision Fact 提供；Agent 只解释和组织。
- 自动重大变化触发放到后续阶段，并且只创建候选任务，不在用户不知情时大量消耗模型预算。

## 14. 安全、权限与渲染

- Report tools 强制路径位于当前 run directory 的 `report/`，禁止绝对路径和 `..` 穿越。
- Deep Report 成稿阶段默认不提供 shell、通用文件写入、交易连接或 broker tools。
- 外部文档中的提示词、HTML、脚本和隐藏字符只作为证据文本；进入 prompt 前进行边界标记和危险内容扫描。
- Markdown 渲染继续先转义不可信 HTML；只开放当前允许的表格、代码块、链接和基础格式。
- Artifact API 校验 report ownership、artifact_id、相对路径和 hash，不返回服务器文件系统布局。
- 日志不保存 API key、认证头、完整私有文档或未脱敏工具参数。

## 15. 失败、恢复和幂等

- 单个章节模型调用失败后重试一次；重试仍失败则报告进入 `failed`，其他已完成章节保持不变。
- 服务重启把运行中的报告标记为 `interrupted`；恢复时核对 request/evidence/plan hash，从第一个未完成或未通过章节继续。
- Evidence 输入变化时不能复用旧章节为当前 final；必须创建新 report revision。
- 同一 `report_id + section_id + expected_revision` 的重复写请求幂等返回已有结果，不重复增加 revision。
- PDF 渲染失败不重跑研究和正文，只重试 render 阶段。
- `resume` 不重新执行已经成功且 hash 匹配的研究工具调用；缺失或过期 Evidence 按 profile 新鲜度规则重新获取，并导致新 revision。
- 取消后允许用户显式创建 resume；取消状态本身不会自动恢复。

## 16. 可观测性与成本治理

每份报告记录：

```text
profile / model / prompt_version
input_tokens / output_tokens / cached_tokens
model_calls / tool_calls / retries
evidence_count / fact_count / source_domains
required_question_count / answered / insufficient
section_count / section_revisions
validation_issue_count by severity/type
draft_chars / final_chars
time_to_first_plan / per_section_latency / total_latency
finish_reason / failure_stage
```

不把“最终字数增加”视为正向 KPI。主指标是 required question coverage、无证据 material claim、跨章节冲突、人工偏好和单位有效结论 token 成本。

运行时告警：

- `finish_reason=length` 比例异常。
- 报告完成率低于 95%。
- 任何 Portfolio Guard critical violation。
- P95 总耗时或 token 超过灰度冻结门槛。
- 同一章节 revision 冲突或恢复循环。
- Artifact hash 与 final revision 不一致。

## 17. 测试计划

### 17.1 单元测试

- 旧 Attempt 无 metadata 时仍按 chat 运行。
- response mode 经 API、Dispatcher、Message、Attempt 到执行器完整传递。
- Router 六维计分、硬规则优先级、4~5 分分类器边界和低置信度回退。
- Router 不使用提示词字符数，不调用研究工具，不自行创建 Deep Report Attempt。
- routing decision 的 Session/content hash 校验、30 分钟过期和双击幂等。
- Plan required question 覆盖、章节数和冻结规则。
- Section revision 乐观锁、重复请求幂等和冲突拒绝。
- Evidence/Fact ID 不存在、hash 不匹配、状态不允许时拒绝引用。
- 敏感自由数字检测与 `{{fact:*}}` 编译。
- Claim Ledger 同期间事实冲突、动作冲突和风险/结论冲突。
- budget 80% 降级、硬上限和 `finish_reason=length` 不落盘。
- deterministic assembly 对相同章节输入产生相同 Markdown/hash。
- Markdown 危险 HTML、脚本、隐藏内容和 prompt injection 样本被阻断。

### 17.2 服务测试

- 普通 chat 回归：响应、事件、取消和工具调用不变。
- 深度报告完整状态机：Evidence、Plan、章节、Critic、修复、PDF、Artifact。
- 一个章节失败不覆盖其他章节，resume 只继续缺失部分。
- 服务在 planning、drafting、rendering 三个阶段重启后可恢复。
- 用户取消时停止模型流，不生成正式 Artifact。
- 编辑消息重跑继承或明确切换 response mode。
- PDF 失败只重试渲染，不重复模型调用。
- 数据不足形成 `passed_with_gaps` 或在 critical gate 下停止，不能伪造完整成功。
- 组合 `analysis_only` 数据不能产生精确价格或仓位建议。

### 17.3 前端测试

- 智能选择/快速回答/深度报告模式选择和灰度默认值。
- 推荐卡原因、三个操作、过期重评和用户拒绝后不重复推荐。
- Deep Report 与 Swarm 互斥。
- 队列、章节进度、刷新恢复、取消、失败、resume 和完成卡。
- `passed_with_gaps` 的缺口提示不可隐藏。
- 没有最终 Artifact 时不展示 PDF 按钮。
- 390px 和 1280px 下进度卡、章节列表和 Artifact 按钮可用。

### 17.4 浏览器与真实运行验证

- 使用固定 Evidence fixture 先验证 UI，不依赖实时行情波动。
- 使用一个单股深度报告完成真实工具、模型、Markdown 和 PDF 全链路。
- 使用一个多持仓报告验证组合 Fact、数据门槛和较长文档。
- 人工抽查 PDF 与 Markdown 的标题、表格、中文换行、引用、页码和下载文件名。

## 18. A/B 评测与发布门禁

Router 先使用独立的 100 条冻结请求评测集：40 条快速问题、40 条适合 Deep Report、10 条应复用/修订既有 Artifact、10 条需要澄清范围。标签由人工根据本计划六维 rubric 冻结，不让 Router 根据输出长度反推标签。

Router 发布门禁：

- 明确用户模式覆盖率 100%，不得改写显式 `chat | deep_report`。
- Deep Report 推荐 precision 至少 80%，recall 至少 85%。
- 应复用既有 Artifact 的请求中，重复生成推荐率不超过 5%。
- Router 自动启动未确认 Deep Report 的次数必须为 0。
- P95 路由耗时不超过 3 秒；硬规则命中的 P95 不超过 100ms。
- 所有推荐均至少包含一个可解释 reason code；提示词长度不能单独改变决策。

Router 门禁通过后，再进行下述报告 A/B：

建立 30 个冻结输入样本：

```text
10 个单股深度研究
10 个组合/多标的研究
5 个数据不足或来源冲突案例
5 个问题多、证据多、容易触及单次输出长度的案例
```

同一模型、温度、工具结果和冻结 Evidence 下比较：

```text
A：当前一次性最终 Markdown
B：增量章节成稿
```

评测者隐藏生成方式。发布门禁：

- B 的 required question coverage 至少 90%，并且比 A 提高至少 15 个百分点；如果 A 已超过 90%，则要求 B 不下降。
- B 不得出现 critical unsupported claim 或 Portfolio Guard violation。
- 无证据 material claim 比例不超过 1%。
- 跨章节高严重度矛盾为 0；全部矛盾数量比 A 至少下降 50%。
- 人工盲评在“证据充分、逻辑完整、风险诚实、结构清晰”综合项上选择 B 的比例至少 65%。
- 完成率至少 95%，P95 总耗时不超过 10 分钟，median token 不超过 A 的 3 倍。
- 最终字数只作为观察项，不设“必须比 A 更长”的要求。

任一安全或事实门禁不通过时不得用可读性优势抵消。

## 19. 实施阶段

### M0：建立一次性输出基线

- 为现有 chat 报告记录 finish reason、字符数、token、耗时和报告类型。
- 建立 30 个冻结评测样本及 required question rubric。
- 建立 100 条 Router 请求集、人工标签和六维复杂度特征提取测试。
- 不改变用户可见输出。

完成条件：可以重复运行同一 Evidence，并产出可比较的 baseline 指标。

### M1：报告协议、存储和确定性校验

- 新建 `agent/src/reports/composer/`，实现模型、Store、编译器、Fact resolver、validators 和 Artifact manifest。
- 实现章节 revision、changes.jsonl、resume 和取消边界。
- 使用固定 fixture 完成单元测试，不接 UI。

完成条件：不调用真实 LLM 也能从章节 fixture 确定性拼装、校验和渲染报告。

### M2：DeepReportService 与 Session 接入

- 扩展 API、Dispatcher、Attempt metadata 和 Session 路由。
- 实现 ReportIntentRouter、严格 JSON 分类器、`report_routing_decisions` 持久化和确认幂等；Router 不调用研究工具。
- 抽取共享 Agent runtime factory，保证 chat/deep 使用相同模型、只读研究工具和安全策略。
- 实现 Evidence 捕获、Planner、分章节 Agent、Critic 和一次修复。
- feature flag 默认关闭。

完成条件：后端 API 能用固定和真实 Evidence 完成一个 `equity_deep_research` Artifact，普通 chat 回归通过。

### M3：前端和 Artifact 交付

- 增加三种模式选择、Router 推荐卡、进度卡、章节状态、取消/resume 和完成卡。
- 接入 Markdown/PDF 下载及 revision 入口。
- 完成前端测试、生产构建和浏览器响应式验证。

完成条件：刷新页面和服务重启后仍能恢复同一 report 状态，未通过门禁的报告不能显示为最终 PDF。

### M4：离线 A/B 与影子运行

- 先跑完 100 条 Router 评测，门禁通过后才允许前端默认使用智能选择。
- 跑完 30 个冻结样本。
- 在真实用户请求上 shadow 生成 B，但仍向用户交付 A；保存用量和校验结果，不自动展示 B。
- 根据实测冻结 P95 延迟、token 和错误率阈值，不凭经验提高预算。

完成条件：第 18 节全部发布门禁通过。

### M5：有限灰度

- 仅对白名单用户开放手动 `deep_report`，默认仍为快速回答。
- 先开放 `equity_deep_research`，稳定后开放 `portfolio_deep_research`。
- 观察至少 20 份真实报告且无 critical 事故后，再接入飞书手动入口和组合 `deep_research` Artifact 索引。
- 自动 material-change 触发另立计划，不属于本轮灰度。

## 20. 配置与回滚

第一版环境配置：

```env
VIBE_TRADING_DEEP_REPORT_ENABLED=0
VIBE_TRADING_DEEP_REPORT_DEFAULT_MODE=chat
VIBE_TRADING_REPORT_ROUTER_ENABLED=0
VIBE_TRADING_REPORT_ROUTER_DEFAULT_MODE=chat
VIBE_TRADING_DEEP_REPORT_MAX_SECTIONS=10
VIBE_TRADING_DEEP_REPORT_MAX_REPORT_CHARS=30000
VIBE_TRADING_DEEP_REPORT_MAX_SECTION_CHARS=4500
VIBE_TRADING_DEEP_REPORT_MAX_MODEL_CALLS=18
VIBE_TRADING_DEEP_REPORT_MAX_TOTAL_TOKENS=120000
VIBE_TRADING_DEEP_REPORT_MAX_SECTION_REVISIONS=2
VIBE_TRADING_DEEP_REPORT_MAX_PARALLEL_SECTIONS=2
```

回滚只需关闭 `VIBE_TRADING_DEEP_REPORT_ENABLED`：

- 新的 deep report 请求返回明确的 `feature_disabled`，不静默改成 chat。
- 普通 chat、DailyPortfolioRun、现有报告 PDF 和历史 Artifact 继续可用。
- 已生成报告保持只读下载，未完成报告标记为 interrupted。
- 数据结构均为新增字段和 run-local 文件，不需要破坏性数据库回滚。

## 21. 主要代码落点

按子系统实施：

- 报告核心：新增 `agent/src/reports/composer/`，容纳 contracts、store、service、compiler、validators、evidence adapters 和专用 tools。
- Session/API：扩展 Attempt metadata、Dispatcher source metadata、消息请求契约和报告查询/恢复路由；普通 AgentLoop 逻辑保持独立。
- Portfolio：只增加手动 deep research 的冻结输入适配器和 Artifact 索引，不修改 daily brief/aggregate/decision 契约。
- 前端：在 Agent 输入区、Router 推荐卡、SSE reducer、消息卡和 API types 中增加智能选择、deep report 模式及进度展示。
- 测试：新增 composer 单元/服务测试，并扩展 Session、API、Agent 页面和组合数据门槛回归测试。

## 22. 最终 Definition of Done

- 用户可以逐次选择智能选择、快速回答或深度报告；旧客户端和普通会话完全兼容。
- 智能选择能解释为何推荐 Deep Report、复用 Artifact 或快速回答，并且未经确认启动 Deep Report 的次数为 0。
- 一份最终报告可以由多个受限 completion 拼装，最终长度不再受单次输出长度直接限制。
- 报告没有最小字数，章节由必答问题和 Evidence 决定，证据不足不会被填充成冗长正文。
- 所有敏感数字来自 Fact，所有 material claim 可以追溯到 Evidence 或明确的数据缺口。
- Agent 不能整篇覆盖报告，章节 revision、输入 hash、变更和 Artifact 均可审计。
- 取消、失败、预算耗尽、服务重启和 PDF 失败都具有明确状态和可测试恢复路径。
- 组合日更的结构化事实、数据门槛和预算约束不被增量 Markdown 绕过。
- A/B 门禁证明改善来自覆盖、一致性和可读性，而不是单纯增加字数。
- 后端测试、前端测试、生产构建、浏览器验证和至少一条真实 Markdown/PDF 链路全部通过。
- 功能默认关闭、可白名单灰度、可一键关闭，并且回滚不影响普通聊天和既有报告。
