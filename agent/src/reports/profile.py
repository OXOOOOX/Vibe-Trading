"""Registered equity and ETF deep-research prompt contracts."""

from __future__ import annotations

from typing import Any


EQUITY_DEEP_RESEARCH_PROFILE: dict[str, Any] = {
    "profile": "equity_deep_research",
    "version": "1.0",
    "required_sections": [
        ("executive_summary", "核心结论"),
        ("business_position", "公司业务与产业位置"),
        ("financial_quality", "三张报表与财务质量"),
        ("accounting_review", "会计科目异常与核查清单"),
        ("implied_expectations", "市值隐含预期"),
        ("terminal_narrative", "长期经营情景与叙事阶段"),
        ("counter_thesis", "反方论证、风险与催化剂"),
        ("conclusion_watchlist", "结论与跟踪框架"),
    ],
    "compiler_sections": ["数据缺口与方法说明"],
    "quality_statuses": ["passed", "passed_with_gaps", "failed_validation"],
    "claim_types": ["fact", "calculation", "inference", "opinion", "data_gap"],
}

ETF_DEEP_RESEARCH_PROFILE: dict[str, Any] = {
    "profile": "etf_deep_research",
    "version": "1.0",
    "default_mode": "compact",
    "report_kind": "deep_research",
    "report_horizon": "structural",
    "required_sections": [
        ("executive_summary", "核心结论"),
        ("index_and_product", "指数与产品"),
        ("exposure_structure", "暴露结构"),
        ("aggregate_fundamentals", "聚合基本面"),
        ("price_volume_structure", "量价结构"),
        ("flow_liquidity_tracking", "份额、流动性与跟踪"),
        ("holding_penetration", "关键持仓穿透"),
        ("scenarios_watchlist", "情景与跟踪框架"),
    ],
    "compiler_sections": ["数据缺口与方法说明"],
    "quality_statuses": ["passed", "passed_with_gaps", "failed_validation"],
    "claim_types": ["fact", "calculation", "inference", "opinion", "data_gap"],
    "token_budget": {"input_tokens": 24_000, "output_tokens": 6_000},
}

REPORT_PROFILES: dict[str, dict[str, Any]] = {
    EQUITY_DEEP_RESEARCH_PROFILE["profile"]: EQUITY_DEEP_RESEARCH_PROFILE,
    ETF_DEEP_RESEARCH_PROFILE["profile"]: ETF_DEEP_RESEARCH_PROFILE,
}


def get_report_profile(name: str) -> dict[str, Any]:
    """Return a registered report profile or fail closed."""

    try:
        return REPORT_PROFILES[str(name)]
    except KeyError as exc:
        raise ValueError(f"unsupported report profile: {name}") from exc


def report_profile_names() -> tuple[str, ...]:
    return tuple(REPORT_PROFILES)


def build_equity_deep_research_prompt(
    user_request: str,
    *,
    parent_report_id: str | None = None,
    revision_sections: list[str] | None = None,
    revision_mode: str = "initial",
) -> str:
    """Wrap an explicit user request in the non-negotiable report contract."""

    revision_instruction = ""
    financial_snapshot_instruction = (
        "第一项核心财务调用必须是 analyze_financial_snapshot。传入准确代码和公司名称；该工具会在缺少显式输入时自动尝试政策门控的最新价格刷新。若你已经从 get_data_context 等来源取得带来源和时间的当前市值/价格/总股本，则把这些值及 market_data_source、market_data_as_of 传入，禁止传入无出处数字。"
    )
    if parent_report_id:
        targets = "、".join(revision_sections or []) or "用户指定内容"
        if revision_mode == "full_refresh":
            revision_instruction = (
                f"\n这是报告 {parent_report_id} 的 full_refresh 新 revision。必须重新获取 FinancialSnapshot "
                "和市场数据，并重新提交全部八个章节；旧章节仅用于对比，不得作为已通过正文直接沿用。\n"
            )
        elif revision_mode == "repair":
            revision_instruction = (
                f"\n这是报告 {parent_report_id} 的 repair revision。使用已复制的 Ledger 修复未通过章节，"
                "不得恢复目标价或手工替代确定性计算。analyze_financial_snapshot 已从本次工具白名单移除；"
                "不得重新登记 Ledger 中已有的财务 Fact。先 inspect，再优先把旧章节改成可校验正文；"
                "找不到准确 Fact 的数字必须删除或改成不含数字的 data_gap。\n"
            )
        else:
            revision_instruction = (
                f"\n这是报告 {parent_report_id} 的 section_revision。只对 {targets} 重新研究和提交；"
                "未变化且工作区状态为 passed 的章节由编译器按哈希复用。\n"
            )
        if revision_sections or revision_mode == "repair":
            financial_snapshot_instruction = (
                "父报告的 FinancialSnapshot、Evidence 与 Fact Ledger 已复制到当前 revision。"
                "除非用户明确要求新数据，或指定章节确实依赖更新后的财务数据，否则不得重新运行 "
                "analyze_financial_snapshot；只登记新增证据并重写指定章节。"
            )

    return f"""[EQUITY_DEEP_RESEARCH_PROFILE]
官方资料优先：在普通网页搜索之前调用 get_official_filings(symbol=<准确代码>)。只有工具返回 official_primary 且已保存 document_ref 的全文才能作为官方原文；搜索摘要和转载不得冒充监管披露。
你正在执行用户明确选择的“穿透式单股深度研究”。这不是普通聊天，也不是每日持仓报告。
{revision_instruction}
## 必须执行的数据顺序

0. 先调用 query_research_knowledge(command="history", symbol=<股票代码>)、query_research_knowledge(command="financials", symbol=<股票代码>) 和 query_research_knowledge(command="search", symbol=<股票代码>)。financials 返回的 validated 快照是按官方原文内容哈希持久化的一次性结构化结果，应直接复用其 Fact/Evidence，不得重新 OCR 同一 document_ref；只有来源内容哈希变化、抽取器版本升级或快照为 needs_review 时才重新抽取。历史 Claim 只能作为“上次判断”或待验证问题，不能作为本次 Evidence。首次研究则明确记录暂无历史正式报告。
1. 只研究用户当前指定的一只上市证券。若只有名称或裸代码，先用 search_symbol 唯一解析为带市场后缀的代码；不能唯一解析就停止并说明歧义。
2. {financial_snapshot_instruction}
3. 用 get_data_context 获取该股票的长周期市场、基本面、新闻和研究报告上下文；遵守 actionability、selected_quote、blocked_reasons 和来源时间。禁止从 raw bars 绕过不可行动状态。
4. 公司业务与行业资料用公告正文、公司财报、监管披露、行业协会或可读取的权威网页。搜索摘要只能作为 source_lead，不能直接支撑重大结论。打开候选网页时必须调用 read_url(url=<原始链接>, subject_key=<当前股票代码>)，使读取过的全文进入该标的档案；read_url 返回 document_ref 和 chunk_catalog 后，使用 read_research_document 读取所需后续片段。登记 Evidence 时优先传 document_ref 与 chunk_refs，不得把来源名称、发布时间或链接自由改写。TAM、市占率和竞争格局默认需要两个独立来源，出现同口径冲突时继续寻找第三个独立来源；无法解决则降级结论，不选择更乐观的数字。
5. TAM、长期份额、稳态利润率只有在已通过 record_report_evidence 形成带年份、口径、币种的来源 Fact 后才建模。届时调用 financial_rigor(command="validate_terminal_scenarios", symbol=<股票代码>, steady_year=<与反推模型一致的稳态年>)；必须是 conservative/base/optimistic/stretched 四个不加权情景。缺少可靠 TAM 时写成 data_gap，禁止编造数字。
6. 市值隐含预期只使用 analyze_financial_snapshot 或 financial_rigor(command="implied_terminal_earnings") 的确定性结果。手工登记盈利预测时，record_report_evidence 必须传 coverage_count 和 forecast_kind，并把带时间戳的 market_cap Fact 一并放入 source_fact_ids。公司业绩预告、管理层指引、年化外推、模型估计或你自行计算的 E1-E3 绝不能登记成 consensus/single_broker；缺少连续三年已发布券商预测时必须将该模块降级为 insufficient_evidence。必须称为“市值隐含长期利润反推”，明确它使用 net_income_proxy、不是完整 FCFF/FCFE DCF、不是目标价。不得输出概率加权结果。
7. 对网页、公告、行业、TAM、竞争格局和汇率资料，必须先读取正文或完整 API payload，再调用 `record_report_evidence` 登记；禁止登记搜索摘要。material 数字在正文中追加 `[Fact:<fact_id>]`；重大产业或事件证据追加 `[Evidence:<evidence_id>]`。推断明确使用“表明、可能、推断”等措辞，并标记 `[inference]`。数据缺口标记 `[data_gap]`。
8. 报告正文必须逐节调用 `report_workspace(command="submit_section", section_id=<固定章节ID>, body_markdown=<不含一级或二级标题的正文>)` 提交。提交失败时必须根据工具返回的行号、Fact 或确定性门控问题修正后重试。标题、元数据、引用索引、整篇 Markdown 和数字审计由服务端编译器生成；不要调用 report_audit，也不要把整份报告放在最终回答中。
9. `submit_section` 正文面向普通投资者，必须使用自然、完整的中文。`[Fact:...]`、`[Evidence:...]`、`[inference]`、`[data_gap]` 只作为行内机器标记；除此之外，正文不得出现 `passed_with_gaps`、`insufficient_evidence`、`not_requested`、report ID、revision、Ledger、工具命令名或英文校验原因。首次出现 CFO、Capex、FCF 等缩写时必须写出中文全称。不要把开发任务标题或提交信息（例如 `feat(...)`、`fix(...)`）写入研究报告。
10. 八节提交完成后调用一次 `report_workspace(command="submit_monitoring_bundle", monitoring_bundle={{...}})`。这里只提交结构趋势、逻辑失效条件、复核触发器以及 0—6 个中长期候选；没有可核验点位时必须提交 `candidates=[]`。候选不得包含买卖、仓位或自动执行指令，必须引用当前报告真实 Fact/Evidence，并保留原报告文字。报告能否发布与候选是否可执行分别校验。

## 数据门控

- 若 analyze_financial_snapshot.report_gate.status=failed_validation（包括证券身份不唯一、无带时间戳的价格/市值、三张报表不足两个可比完整财年、币种未知或期间错配），整份报告质量为 failed_validation：可以输出诊断报告，但不能输出正式估值或投资结论。
- 缺少最新季度、E1-E3、TAM、行业来源或券商目标价，只影响对应研究内容。报告状态由服务端记录；正文只需用中文说明哪些判断暂时无法形成。
- 缺失值保持缺失，禁止补零、猜测、同业替代或用篇幅掩盖。
- 会计规则命中只能写“异常信号”“需核查事项”或“财务质量风险”。不得仅凭规则使用“造假”“虚增”等结论。
- 不设置最低字数或页数；证据不足的章节应短而明确。
- equity_deep_research 禁止调用 three_scenario、verify_valuation 生成目标价、合理市值或概率情景；只能比较市值隐含长期利润与经过验证的经营情景利润。

## Report Workspace 章节格式

先调用 `report_workspace(command="inspect")` 查看当前 revision、章节状态、Fact 指标目录、Evidence 域目录和模块状态。首次 inspect 只返回目录，不返回整个 Ledger；随后必须用 `fact_metrics=[...]` 或 `evidence_domains=[...]` 分批读取当前 revision 的真实 Fact/Evidence ID。full_refresh 默认不返回旧章节正文，禁止复用父 revision 的 Fact/Evidence ID；repair 或 section_revision 只有在确实需要旧正文时才传 `section_ids=[...]` 与 `include_section_bodies=true`。然后依次提交以下八个 section_id；服务端会生成对应标准标题：

### `executive_summary` → 核心结论
回答市场正在定价什么、三个最重要事实、主要缺口和三个跟踪指标。

### `business_position` → 公司业务与产业位置
说明业务、收入来源、客户/供应链、行业周期、竞争格局和公司位置。

### `financial_quality` → 三张报表与财务质量
解释收入利润、资产负债表扩张、现金利润匹配、资本开支和融资依赖；引用确定性 Fact。

### `accounting_review` → 会计科目异常与核查清单
逐条给出触发规则、期间、正常解释和下一步核查，不得输出欺诈概率。

### `implied_expectations` → 市值隐含预期
逐个列出8%、10%、12%折现率结果、残差、稳态年、模型前提和限制；不可用时只说明证据缺口。

### `terminal_narrative` → 长期经营情景与叙事阶段
只有通过四情景确定性校验时才展示 TAM×份额×利润率结果。叙事阶段属于 inference，不得发明“截距占比”等无定义指标。

### `counter_thesis` → 反方论证、风险与催化剂
至少一个能推翻主结论的反方解释；催化剂包含可观察指标与时间窗口。

### `conclusion_watchlist` → 结论与跟踪框架
给出研究判断、成立条件、失效条件和数据更新清单；不是交易指令，不凭空给仓位。

### 编译器生成 → 数据缺口与方法说明
列出模块状态、未解决问题、数据截至时间和模型限制。不要自行编写来源清单或引用索引；编译器会从持久化 Ledger 自动附加引用索引，来源信息不能被删除。

不得使用模板占位符，不得复制外部 Skill 的固定公司、日期或结论。不要用 write_file，不要自行生成一级或二级标题，也不要在最终回答中重复完整报告。八个章节全部成功提交后，最终回答只需简短说明章节已提交，正式 Markdown 由服务端编译、审计和持久化。

## 用户请求

{user_request.strip()}
"""


def build_etf_deep_research_prompt(
    user_request: str,
    *,
    parent_report_id: str | None = None,
    revision_sections: list[str] | None = None,
    revision_mode: str = "initial",
) -> str:
    """Build the compact ETF research contract without equity financial gates."""

    revision_instruction = ""
    if parent_report_id:
        targets = "、".join(revision_sections or []) or "受变化影响的章节"
        if revision_mode == "full_refresh":
            revision_instruction = (
                f"\n当前任务是报告 {parent_report_id} 的 full_refresh revision。"
                "必须重新执行数据准备并重新提交全部八个章节；父版本只用于差异比较，"
                "不得把父章节直接当成本轮已通过正文。"
            )
        elif revision_mode == "repair":
            revision_instruction = (
                f"\n当前任务是报告 {parent_report_id} 的 repair revision。"
                f"必须 inspect 并重新提交 {targets}；即使复制后的父章节显示 status=passed，"
                "只要父报告存在全局发布审查问题，该目标章节就不得原样复用。"
                "对 claim_support_gate 的 weak/insufficient 推断，必须补充独立权威来源，"
                "或删除推断并改写成明确的 data_gap；禁止仅修改 Claim 类型来绕过审查。"
            )
        else:
            revision_instruction = (
                f"\n当前任务是报告 {parent_report_id} 的 section_revision。"
                f"只刷新 {targets}；未变化章节必须优先复用已通过的父版本。"
            )
    section_lines = "\n".join(
        f"- `{section_id}`：{heading}"
        for section_id, heading in ETF_DEEP_RESEARCH_PROFILE["required_sections"]
    )
    return f"""[ETF_DEEP_RESEARCH_PROFILE]
官方资料优先：调用 get_official_filings(symbol=<ETF代码>) 获取基金定期报告和交易所披露；只有 official_primary 全文可作为官方原文，搜索摘要只作线索。
你正在执行用户明确选择的 ETF 深度研究流程，不得套用上市公司三张财务报表、会计异常、TAM 或单家公司长期利润反推框架。最终产物属于“ETF 结构研究”“ETF 穿透研究（部分覆盖）”还是“ETF 穿透式深度研究”，只由服务端根据持仓与成分研究覆盖率判定；你不得自行宣称完整穿透。{revision_instruction}

## 数据与复用顺序

1. 唯一解析 ETF 代码后，首先调用 `prepare_etf_research`。它会核验原始价格、复用现有 Universe/P4A/P4B，并把 Selection、Resolution、Fact 和 Evidence 写入当前报告；它不会启动 P4B2 生成。若返回 `status=error`，该错误是本次生成的终止状态：立即向用户报告 `stage`、`error`、`missing_hard_fields` 和来源错误并停止，不得继续网页研究、提交章节或声称报告完成。
2. 再查询当前 ETF 的历史正式报告和仍有效的 Fact。旧 Claim 只能作为上次观点，不得充当本次 Evidence。
3. 核验基金、管理人、跟踪指数、指数规则版本、份额、净值和数据截止时间；打开候选网页时必须调用 `read_url(url=<原始链接>, subject_key=<当前ETF代码>)`，使读取过的全文进入该标的档案；P4 已提供的成分选择不得由模型重新排序或扩展。
4. 价格敏感判断必须使用已验证且未过期的市场 Snapshot；弱数据或单一未授权来源必须停止相关结论并标记 `[data_gap]`。
5. 优先复用相同 Snapshot 和模块输入指纹。没有实质变化时不得重复搜索、重复穿透成分或创建新报告。
6. `holding_penetration` 的确定性表格、选择原因、解释覆盖率和 P4B 状态由编译器生成。你只补充有证据的解释，不得把 missing、stale 或 conflicted 摘要改写成有效结论。
7. ETF 份额或持有人变化只说明资金申赎或披露持仓变化；除非有官方持有人披露，不得归因为国家队、证金或其他特定主体。
8. 所有重要数字同行追加 `[Fact:<fact_id>]`；重大事件或规则来源追加 `[Evidence:<evidence_id>]`；推断标记 `[inference]`。
9. 调用 `report_workspace(command="inspect")` 读取 `etf_penetration` 与当前 Ledger，再逐节调用 `report_workspace(command="submit_section", ...)`。标题、P4 确定性表格、方法说明、数字审计、引用索引和完整 Artifact 由服务端生成。
10. 全文章节完成后调用 `report_workspace(command="submit_monitoring_bundle", monitoring_bundle={{...}})`，只提交 0—6 个结构性失效位、大级别支撑/阻力、长期突破确认、趋势恢复或重新研究触发器。必须使用原始不复权价格并引用当前 Fact/Evidence；无法形成合格点位时提交 `candidates=[]`，不得为了格式编造价格。该产物只供后续人工确认的监控流程消费，禁止买卖、仓位或自动执行指令。
11. 最终回复必须以 `report_workspace(command="inspect")` 返回的 manifest/validation 为准；只有发布状态通过且正式 Artifact 已存在时，才能说报告完成或提供下载。
12. `etf_readiness.status` 为 `structure_ready` 或 `penetration_partial` 时，必须按对应层级向用户说明尚缺的成分研究；只有 `penetration_ready` 才能使用“穿透式深度研究已完成”。

## 固定章节

{section_lines}

量价章节必须同时说明价格方向、成交量相对基准、所处位置、份额/净值是否确认、可能含义、反证和失效条件。持仓穿透必须解释选择原因和解释覆盖率，不得堆叠完整个股报告。

输入上下文上限 24,000 tokens，输出上限 6,000 tokens；成分摘要每只最多约 600 tokens。证据不足的章节应短而明确，不得用篇幅掩盖缺口。

## 用户请求

{user_request.strip()}
"""


def build_deep_research_prompt(
    profile: str,
    user_request: str,
    *,
    parent_report_id: str | None = None,
    revision_sections: list[str] | None = None,
    revision_mode: str = "initial",
) -> str:
    """Dispatch prompt construction through the registered Profile contract."""

    get_report_profile(profile)
    builder = (
        build_equity_deep_research_prompt
        if profile == "equity_deep_research"
        else build_etf_deep_research_prompt
    )
    return builder(
        user_request,
        parent_report_id=parent_report_id,
        revision_sections=revision_sections,
        revision_mode=revision_mode,
    )
