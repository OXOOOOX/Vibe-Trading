"""Prompt and target helpers for background portfolio analysis sessions."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from src.portfolio.state import normalize_symbol

PortfolioAnalysisScope = Literal["holding", "portfolio", "market"]
MarketAnalysisPhase = Literal["premarket", "intraday"]
PORTFOLIO_ANALYSIS_SOURCE = "portfolio_analysis"
MARKET_ANALYSIS_TIMEZONE = ZoneInfo("Asia/Shanghai")
MARKET_ANALYSIS_INTRADAY_CUTOFF = time(11, 30)
MARKET_ANALYSIS_CLOSE = time(15, 0)


def current_market_analysis_time() -> datetime:
    """Return the authoritative clock used for time-sensitive portfolio analysis."""
    return datetime.now(MARKET_ANALYSIS_TIMEZONE)


def _as_market_time(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=MARKET_ANALYSIS_TIMEZONE)
    return timestamp.astimezone(MARKET_ANALYSIS_TIMEZONE)


def resolve_market_analysis_phase(now: Optional[datetime] = None) -> MarketAnalysisPhase:
    """Choose the morning or lunch-session prompt using Shanghai local time."""
    local_timestamp = _as_market_time(now or current_market_analysis_time())
    is_weekday = local_timestamp.weekday() < 5
    is_intraday_window = MARKET_ANALYSIS_INTRADAY_CUTOFF <= local_timestamp.time() < MARKET_ANALYSIS_CLOSE
    return "intraday" if is_weekday and is_intraday_window else "premarket"


def find_holding(holdings: list[dict[str, Any]], symbol: str) -> Optional[dict[str, Any]]:
    """Find a holding by its normalized, exact market symbol."""
    target = normalize_symbol(symbol).upper()
    for holding in holdings:
        candidate = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
        if candidate == target:
            return holding
    return None


def build_analysis_title(
    scope: PortfolioAnalysisScope,
    holding: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    market_phase: Optional[MarketAnalysisPhase] = None,
) -> str:
    """Create a searchable title without embedding stale price information."""
    timestamp = now or datetime.now()
    if scope == "portfolio":
        return f"全持仓报告 · {timestamp:%Y-%m-%d}"
    if scope == "market":
        timestamp = _as_market_time(timestamp)
        phase = market_phase or resolve_market_analysis_phase(timestamp)
        label = "盘前分析" if phase == "premarket" else "盘中分析"
        return f"{label} · {timestamp:%Y-%m-%d}"
    assert holding is not None
    name = str(holding.get("name") or "持仓标的").strip()
    symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
    return f"持仓分析 · {name} ({symbol})"


def build_analysis_prompt(
    scope: PortfolioAnalysisScope,
    holding: Optional[dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    market_phase: Optional[MarketAnalysisPhase] = None,
) -> str:
    """Build the first message for a research-only portfolio analysis session."""
    common_rules = """
这是一个研究分析任务，不是交易执行任务。不得创建、提交、修改、取消真实订单或条件单；只能给出条件单观察建议。
你必须先调用 portfolio_state(action="get")，将它作为当前持仓、成本、现金和近期交易的唯一事实来源。不要用旧聊天、记忆或代理 ETF 替换真实持仓。
使用系统提示中的运行时间解释“最近/今天/本周”。任何最新行情、新闻或事件都要标注来源、发布日期或数据截至时间；若无法核实，明确说明数据缺口。
价格敏感结论应优先调用 verified_market_data 获取精确标的的多源校核行情，并披露数据源、校核时间、复权口径和冲突。为趋势分析加载 technical-basic 技能，并获取足够的日线数据；不要把不同复权口径混为同一价格结论。
输出使用中文、清晰的小标题和必要的 Markdown 表格。所有建议均为研究观察，不构成自动交易指令。
""".strip()

    if scope == "holding":
        assert holding is not None
        name = str(holding.get("name") or "该持仓").strip()
        symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
        return f"""请对我当前持有的 {name}（{symbol}）做一次单标的持仓分析。

{common_rules}

在读取结构化持仓后，确认 {symbol} 仍在持仓中，并完成以下内容：
1. 给出该标的数量、成本、浮盈亏、组合权重，以及它对组合集中度、主题重叠和风险暴露的影响。
2. 搜索最近 30 天的个股/ETF 新闻，重点标出最近 7 天的事项；列出发布日期、来源、可能影响和待验证点。必要时结合 web_search 和 read_url 交叉核验。
3. 说明当前走势、趋势阶段、量价与波动、关键支撑/阻力以及会推翻判断的条件。
4. 给出“条件单观察位”表：情景、触发价或区间、确认条件、止损/失效位、止盈位、建议仓位动作和依据。它们仅供我手动评估，绝不能创建或提交订单。
5. 最后列出未来一周最值得跟踪的新闻、价格和组合层面风险。
"""

    if scope == "market":
        timestamp = _as_market_time(now or current_market_analysis_time())
        phase = market_phase or resolve_market_analysis_phase(timestamp)
        trigger_time = timestamp.strftime("%Y-%m-%d %H:%M Asia/Shanghai")
        if phase == "premarket":
            same_day = timestamp.weekday() < 5 and timestamp.time() < MARKET_ANALYSIS_INTRADAY_CUTOFF
            target_session = "今天" if same_day else "下一交易日"
            timing_rule = (
                "这是早间盘前版本，只使用触发时刻已经发生并可核实的数据，不得引用尚未发生的当日盘中行情。"
                if same_day
                else "当前已不在当日早间窗口。请先核实交易日历，将分析目标切换到下一交易日，不得把休市日或已收盘时段写成正在交易。"
            )
            task = f"""请根据我当前的整个持仓，生成{target_session}的盘前分析。触发时间：{trigger_time}。

{common_rules}

{timing_rule}完成以下内容：
1. 汇总隔夜到今晨的全球股指、股指期货、汇率、利率、商品、政策和重要新闻，说明它们对当前实际持仓的传导路径；所有事实标注来源和时间。
2. 对每个实际持仓检查隔夜/今晨新闻、公告和事件，区分已确认催化、潜在影响和待验证事项。
3. 获取最新可用校核行情和足够的日线数据，给出今日可能的高开/低开风险、趋势背景、关键支撑阻力及判断失效条件。
4. 给出“今日盘前观察清单”：标的、核心变量、开盘后需要确认的量价信号、关键区间、风险等级和仅供人工决策的条件动作。
5. 最后给出今天最优先关注的 3—5 项组合风险与观察顺序；不要生成下周泛化报告。
"""
        else:
            task = f"""请根据我当前的整个持仓，生成今天午间的盘中分析。触发时间：{trigger_time}。

{common_rules}

这是午休盘中版本。重点复盘今天上午已经发生的交易，并为下午时段更新判断；不得把昨收或缓存旧价冒充上午收盘数据。完成以下内容：
1. 获取今天上午截至午休的最新可用分时/日内校核行情，汇总每个实际持仓的涨跌、成交量价、振幅、相对强弱和对组合盈亏的贡献。
2. 对照开盘前预期与上午实际走势，指出已经验证、被否定和仍待下午确认的逻辑；明确数据截至时间和缺口。
3. 搜索开盘后新增的公告、新闻、政策和市场异动，标注发布时间、来源及其对持仓的即时影响。
4. 给出“下午盘中观察清单”：标的、上午状态、下午关键区间、确认信号、失效条件、风险等级和仅供人工决策的条件动作。
5. 最后按优先级列出下午最需要盯住的 3—5 项组合风险，不要重复生成完整周度持仓报告。
"""
        return task

    return f"""请对我当前的整个持仓生成一份细致的组合报告。

{common_rules}

在读取结构化持仓后，完成以下内容：
1. 汇总现金、总市值、总盈亏、仓位权重、集中度，并识别行业、主题或相关性重叠及风险贡献。
2. 对每个实际持仓标的获取并标注最新校核行情、近期新闻、走势状态、关键价位和持仓逻辑的有效性；不要分析未持有的标的，除非明确标为基准或代理。
3. 给出组合压力情景、优先关注事项和再平衡观察建议，并清楚区分事实、推断和数据缺口。
4. 用“条件单观察清单”汇总各标的的触发价/区间、确认条件、失效位和建议动作；全部仅供人工决策，绝不能创建、提交或修改订单。
5. 最后按优先级列出下一周需要跟踪的新闻、价格和组合风险。
"""
