"""The built-in equity deep-research profile and its runtime prompt contract."""

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
你正在执行用户明确选择的“穿透式单股深度研究”。这不是普通聊天，也不是每日持仓报告。
{revision_instruction}
## 必须执行的数据顺序

1. 只研究用户当前指定的一只上市证券。若只有名称或裸代码，先用 search_symbol 唯一解析为带市场后缀的代码；不能唯一解析就停止并说明歧义。
2. {financial_snapshot_instruction}
3. 用 get_data_context 获取该股票的长周期市场、基本面、新闻和研究报告上下文；遵守 actionability、selected_quote、blocked_reasons 和来源时间。禁止从 raw bars 绕过不可行动状态。
4. 公司业务与行业资料用公告正文、公司财报、监管披露、行业协会或可读取的权威网页。搜索摘要不能直接支撑重大结论，必须读取正文。
5. TAM、长期份额、稳态利润率只有在已通过 record_report_evidence 形成带年份、口径、币种的来源 Fact 后才建模。届时调用 financial_rigor(command="validate_terminal_scenarios", symbol=<股票代码>, steady_year=<与反推模型一致的稳态年>)；必须是 conservative/base/optimistic/stretched 四个不加权情景。缺少可靠 TAM 时写成 data_gap，禁止编造数字。
6. 市值隐含预期只使用 analyze_financial_snapshot 或 financial_rigor(command="implied_terminal_earnings") 的确定性结果。手工登记盈利预测时，record_report_evidence 必须传 coverage_count 和 forecast_kind，并把带时间戳的 market_cap Fact 一并放入 source_fact_ids。公司业绩预告、管理层指引、年化外推、模型估计或你自行计算的 E1-E3 绝不能登记成 consensus/single_broker；缺少连续三年已发布券商预测时必须将该模块降级为 insufficient_evidence。必须称为“市值隐含长期利润反推”，明确它使用 net_income_proxy、不是完整 FCFF/FCFE DCF、不是目标价。不得输出概率加权结果。
7. 对网页、公告、行业、TAM、竞争格局和汇率资料，必须先读取正文或完整 API payload，再调用 `record_report_evidence` 登记；禁止登记搜索摘要。material 数字在正文中追加 `[Fact:<fact_id>]`；重大产业或事件证据追加 `[Evidence:<evidence_id>]`。推断明确使用“表明、可能、推断”等措辞，并标记 `[inference]`。数据缺口标记 `[data_gap]`。
8. 报告正文必须逐节调用 `report_workspace(command="submit_section", section_id=<固定章节ID>, body_markdown=<不含一级或二级标题的正文>)` 提交。提交失败时必须根据工具返回的行号、Fact 或确定性门控问题修正后重试。标题、元数据、引用索引、整篇 Markdown 和数字审计由服务端编译器生成；不要调用 report_audit，也不要把整份报告放在最终回答中。
9. `submit_section` 正文面向普通投资者，必须使用自然、完整的中文。`[Fact:...]`、`[Evidence:...]`、`[inference]`、`[data_gap]` 只作为行内机器标记；除此之外，正文不得出现 `passed_with_gaps`、`insufficient_evidence`、`not_requested`、report ID、revision、Ledger、工具命令名或英文校验原因。首次出现 CFO、Capex、FCF 等缩写时必须写出中文全称。不要把开发任务标题或提交信息（例如 `feat(...)`、`fix(...)`）写入研究报告。

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
