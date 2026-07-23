"""Central Chinese vocabulary for reader-facing Markdown and PDF artifacts."""

from __future__ import annotations

import re


METRIC_READER_LABELS: dict[str, str] = {
    "begin_cash": "期初现金余额",
    "buffer_entry_rank": "缓冲区进入名次",
    "buffer_retention_rank": "缓冲区保留名次",
    "capex": "资本开支",
    "capex_to_cfo": "资本开支占经营现金流比例",
    "cash": "货币资金",
    "cash_from_financing": "筹资活动现金流",
    "cash_from_investing": "投资活动现金流",
    "cash_profit_divergence": "现金流与利润偏离程度",
    "cash_to_assets": "货币资金占总资产比例",
    "cash_to_interest_bearing_debt": "货币资金与有息负债之比",
    "cash_to_short_debt": "货币资金与短期债务之比",
    "cfo": "经营活动现金流",
    "cfo_to_net_income": "经营活动现金流与净利润之比",
    "cfo_yoy": "经营活动现金流同比增速",
    "construction_in_progress": "在建工程",
    "contract_assets": "合同资产",
    "contract_effective_date": "合同生效日期",
    "contract_liabilities": "合同负债",
    "contract_liabilities_yoy": "合同负债同比增速",
    "current_debt_due": "一年内到期债务",
    "current_price": "最新价格",
    "custodian": "基金托管人",
    "custody_fee_rate": "基金托管费率",
    "debt_ratio": "资产负债率",
    "deducted_net_profit": "扣除非经常性损益后的净利润",
    "diluted_eps": "稀释每股收益",
    "dividends": "现金分红",
    "end_cash": "期末现金余额",
    "etf_component_fully_supported_coverage": "成分研究完全支持覆盖率",
    "etf_component_research_coverage": "成分研究覆盖率",
    "etf_component_weight": "成分权重",
    "etf_estimated_net_flow_1d": "基金单日估算净流入",
    "etf_explanation_coverage": "成分解释覆盖率",
    "etf_fund_units": "基金份额",
    "etf_fund_units_change_1d": "基金份额单日变化",
    "etf_observed_weight_coverage": "已知成分权重覆盖率",
    "etf_selected_weight_coverage": "入选成分权重覆盖率",
    "exchange": "上市交易所",
    "exchange_market_value": "交易所披露市值",
    "fixed_assets": "固定资产",
    "fund_full_name": "基金全称",
    "fund_short_name": "基金简称",
    "fund_units": "基金份额",
    "global_coverage": "总体覆盖率",
    "goodwill": "商誉",
    "goodwill_to_equity": "商誉占净资产比例",
    "gross_margin": "毛利率",
    "gross_profit": "毛利润",
    "gross_profit_yoy": "毛利润同比增速",
    "index_code": "跟踪指数代码",
    "index_name": "跟踪指数名称",
    "intangible_assets": "无形资产",
    "intangible_assets_to_equity": "无形资产占净资产比例",
    "interest_bearing_debt": "有息负债",
    "interest_bearing_debt_to_assets": "有息负债占总资产比例",
    "inventory": "存货",
    "inventory_vs_revenue_growth_gap": "存货与收入增速差",
    "inventory_yoy": "存货同比增速",
    "long_debt": "长期债务",
    "management_fee_rate": "基金管理费率",
    "manager": "基金管理人",
    "market_cap": "总市值",
    "net_margin": "净利率",
    "net_profit_attributable": "归母净利润",
    "net_profit_parent": "归母净利润",
    "net_profit_parent_yoy": "归母净利润同比增速",
    "nonrecurring_profit": "非经常性损益",
    "nonrecurring_profit_to_net_income": "非经常性损益占净利润比例",
    "operating_cashflow": "经营活动现金流",
    "operating_cost": "营业成本",
    "operating_margin": "营业利润率",
    "operating_profit": "营业利润",
    "operating_working_capital": "经营性营运资金",
    "operating_working_capital_change": "经营性营运资金变化",
    "parent_equity": "归属于母公司股东的净资产",
    "peer_group_estimated_net_flow_1d": "同类基金单日估算净流入",
    "peer_group_inflow_member_ratio_1d": "同类基金单日净流入产品占比",
    "peer_group_member_count": "同类基金数量",
    "peer_group_unit_change_coverage": "同类基金份额变化覆盖率",
    "peer_member_current_fund_units": "同类基金当前份额",
    "peer_member_estimated_net_flow_1d": "同类基金单只产品估算净流入",
    "peer_member_fund_units_change_1d": "同类基金单只产品份额变化",
    "peer_member_market_price": "同类基金单只产品市场价格",
    "peer_member_prior_fund_units": "同类基金单只产品上期份额",
    "premium_discount_rate": "折溢价率",
    "published_at": "发布日期",
    "published_fund_units": "公告基金份额",
    "published_net_assets": "公告基金净资产",
    "published_unit_nav": "公告单位净值",
    "receivables": "应收款项",
    "receivables_vs_revenue_growth_gap": "应收款项与收入增速差",
    "receivables_yoy": "应收款项同比增速",
    "regular_rebalance_change_cap": "定期调样比例上限",
    "revenue": "营业收入",
    "revenue_yoy": "营业收入同比增速",
    "review_frequency": "指数定期审核频率",
    "review_months": "指数定期审核月份",
    "short_debt": "短期债务",
    "single_constituent_weight_cap": "单一成分权重上限",
    "source_url": "来源链接",
    "target_component_count": "目标成分数量",
    "top_five_weight_cap": "前五大成分权重上限",
    "total_assets": "总资产",
    "total_equity": "净资产",
    "total_liabilities": "总负债",
    "total_shares": "总股本",
    "total_shares_market": "行情口径总股本",
    "tracked_index_code": "跟踪指数代码",
    "tracked_index_name": "跟踪指数名称",
    "unit_nav": "单位净值",
    "version": "规则版本",
    "company_global_market_share": "公司全球市场份额",
    "global_crystal_oscillator_tam_2025": "全球晶振市场规模",
    "global_crystal_oscillator_tam_2030": "全球晶振市场规模预测",
    "crystal_oscillator_tam_cagr_2025_2030": "全球晶振市场预计复合增速",
    "ultra_high_freq_oscillator_global_suppliers": "全球具备超高频差分晶振量产能力的厂商数量",
    "net_profit_2026H1_low": "2026年上半年归母净利润预告下限",
    "net_profit_2026H1_high": "2026年上半年归母净利润预告上限",
}


UNIT_READER_LABELS: dict[str, str] = {
    "cny": "元",
    "rmb": "元",
    "yuan": "元",
    "cny_per_share": "元/股",
    "cny_per_fund_unit": "元/份",
    "fund_units": "份",
    "shares": "股",
    "share": "股",
    "ratio": "%",
    "decimal": "%",
    "percent": "%",
    "pct": "%",
    "multiple": "倍",
    "times": "倍",
    "count": "个",
    "date": "日期",
    "text": "文本",
    "url": "链接",
}

SOURCE_STATUS_READER_LABELS: dict[str, str] = {
    "official_primary": "官方一手资料",
    "live_retrieved": "本次运行已获取",
    "source_recorded": "来源已登记",
    "historical_context": "历史背景资料",
    "verified": "已核验",
    "triangulated": "已交叉验证",
    "conflicted": "口径存在冲突",
    "weak": "单源参考",
    "insufficient": "证据不足",
}

MODULE_STATUS_READER_LABELS: dict[str, str] = {
    "complete": "完整可用",
    "partial": "部分可用",
    "missing": "缺失",
    "not_applicable": "不适用",
    "passed": "校验通过",
    "warning": "需要留意",
    "failed": "校验失败",
    "insufficient_evidence": "证据不足",
}

VERSION_FIELD_READER_LABELS: dict[str, str] = {
    "added": "新增",
    "updated": "更新",
    "confirmed": "沿用并确认",
    "contradicted": "出现矛盾",
    "superseded": "已被新版本替代",
}

MONITOR_CONDITION_READER_LABELS: dict[str, str] = {
    "price_only": "仅价格接近",
    "confirmed": "条件已确认",
    "divergence": "量价背离",
    "invalidated": "条件已失效",
    "insufficient_data": "数据不足",
}

REASON_READER_LABELS: dict[str, str] = {
    "required_identity_fields_missing": "基金身份档案字段不完整",
    "partial_component_universe": "成分范围仅部分可用",
    "component_universe_missing": "成分范围缺失",
    "component_research_partial": "部分成分研究尚待补充",
    "component_research_gaps": "成分研究存在缺口",
    "component_research_conflicted": "成分研究证据存在冲突",
}

VALUE_READER_LABELS: dict[str, str] = {
    "quarterly": "按季度",
    "monthly": "按月",
    "semiannual": "每半年",
    "annual": "每年",
}

SEMANTICS_READER_LABELS: dict[str, str] = {
    "contractual_annual_rate": "基金合同约定的年费率",
    "fund_share_nav_from_pcf": "申购赎回清单披露的单位净值",
    "official_exchange_end_of_day_fund_units": "交易所日终披露的基金份额",
    "market_price_times_official_exchange_end_of_day_fund_units": "市场价格乘以交易所日终基金份额",
    "same_day_market_price_divided_by_nav_minus_one": "当日市场价格相对单位净值的折溢价",
    "iopv_value_not_published_on_source_page": "来源页面未披露基金份额参考净值",
    "share_delta_times_current_market_price_proxy": "份额变化乘以当日市场价格的估算口径",
    "share_delta_times_same_day_market_price_proxy": "份额变化乘以同日市场价格的估算口径",
    "sum_share_delta_times_current_market_price_proxy": "同类基金份额变化乘以各自当日价格的汇总估算口径",
    "annual_report.published_fund_units": "年度报告披露的基金份额",
    "annual_report.published_net_assets": "年度报告披露的基金资产净值",
    "annual_report.published_unit_nav": "年度报告披露的单位净值",
}

_FORBIDDEN_MACHINE_TERMS = (
    "parent equity",
    "inventory",
    "receivables",
    "unit nav",
    "quarterly",
    "official_primary",
    "source_recorded",
    "global_coverage",
    "contractual_annual_rate",
    "fund_share_nav_from_pcf",
    "official_exchange_end_of_day_fund_units",
    "market_price_times_official_exchange_end_of_day_fund_units",
    "same_day_market_price_divided_by_nav_minus_one",
    "iopv_value_not_published_on_source_page",
)


def metric_reader_label(metric: str) -> str | None:
    return METRIC_READER_LABELS.get(str(metric or "").strip())


def semantics_reader_label(semantics: str) -> str:
    """Return a Chinese display label without leaking an unknown machine token."""

    value = str(semantics or "").strip()
    if not value:
        return "暂缺"
    return SEMANTICS_READER_LABELS.get(value, "其他已登记口径")


def reader_machine_terms(text: str) -> list[str]:
    """Return forbidden machine vocabulary exposed in a human artifact."""

    lowered = str(text or "").casefold()
    return [
        term for term in _FORBIDDEN_MACHINE_TERMS
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered)
    ]
