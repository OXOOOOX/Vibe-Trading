"""Deterministic financial normalization, reconciliation, and review signals."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Iterable, Mapping

from .contracts import (
    DerivedFinancialFact,
    EvidenceItem,
    FinancialAlert,
    FinancialCoverage,
    FinancialPeriod,
    FinancialSnapshot,
)

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)
CALCULATION_VERSION = "equity-financial-analysis-v1"
_MAX_PERIODS_PER_SERIES = 8


# Eastmoney uses different field names across A/H/US reports.  The first
# present numeric alias wins; absent values remain None and are never zero-filled.
FIELD_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "income": {
        "revenue": (
            "TOTAL_OPERATE_INCOME", "OPERATE_INCOME", "TOTAL_REVENUE", "REVENUE",
            "OPERATING_REVENUE", "营业总收入", "营业收入",
        ),
        "operating_cost": (
            "OPERATE_COST", "COST_OF_REVENUE", "OPERATING_COST", "营业成本",
        ),
        "gross_profit": ("GROSS_PROFIT", "GROSSPROFIT", "毛利润"),
        "operating_profit": (
            "OPERATE_PROFIT", "OPERATING_PROFIT", "OPERATING_INCOME", "营业利润",
        ),
        "net_profit_parent": (
            "PARENT_NETPROFIT", "NETPROFIT_PARENT_COMPANY", "NET_INCOME_COMMON",
            "NET_INCOME", "NETPROFIT", "归母净利润",
        ),
        "deducted_net_profit": (
            "DEDUCT_PARENT_NETPROFIT", "DEDUCT_NETPROFIT", "扣非归母净利润",
        ),
        "asset_impairment": (
            "ASSET_IMPAIRMENT_LOSS", "ASSET_IMPAIRMENT", "资产减值损失",
        ),
        "credit_impairment": (
            "CREDIT_IMPAIRMENT_LOSS", "CREDIT_IMPAIRMENT", "信用减值损失",
        ),
        "diluted_eps": ("DILUTED_EPS", "EPS_DILUTED", "稀释每股收益"),
    },
    "balance": {
        "cash": (
            "MONETARYFUNDS", "MONEY_CAP", "CASH_EQUIVALENTS",
            "CASH_AND_CASH_EQUIVALENTS", "CASH", "货币资金",
        ),
        "restricted_cash": (
            "RESTRICTED_CASH", "RESTRICTED_FUNDS", "受限资金",
        ),
        "receivables": (
            "ACCOUNTS_RECE", "ACCOUNTS_RECEIVABLE", "TRADE_RECEIVABLES", "应收账款",
        ),
        "other_receivables": (
            "OTHER_RECE", "OTHER_RECEIVABLES", "OTHER_ACCOUNTS_RECEIVABLE", "其他应收款",
        ),
        "inventory": ("INVENTORY", "INVENTORIES", "存货"),
        "contract_assets": ("CONTRACT_ASSET", "CONTRACT_ASSETS", "合同资产"),
        "fixed_assets": ("FIXED_ASSET", "FIXED_ASSETS", "PROPERTY_PLANT_EQUIPMENT", "固定资产"),
        "construction_in_progress": ("CIP", "CONSTRUCTION_IN_PROGRESS", "在建工程"),
        "goodwill": ("GOODWILL", "商誉"),
        "intangible_assets": ("INTANGIBLE_ASSET", "INTANGIBLE_ASSETS", "无形资产"),
        "total_assets": ("TOTAL_ASSETS", "TOTALASSETS", "资产总计"),
        "short_debt": ("SHORT_LOAN", "SHORT_TERM_DEBT", "SHORT_BORROWING", "短期借款"),
        "current_debt_due": (
            "NONCURRENT_LIAB_1YEAR", "CURRENT_PORTION_LONG_TERM_DEBT", "一年内到期的非流动负债",
        ),
        "long_debt": ("LONG_LOAN", "LONG_TERM_DEBT", "LONG_BORROWING", "长期借款"),
        "bonds_payable": ("BOND_PAYABLE", "BONDS_PAYABLE", "应付债券"),
        "contract_liabilities": ("CONTRACT_LIAB", "CONTRACT_LIABILITIES", "合同负债"),
        "total_liabilities": ("TOTAL_LIABILITIES", "TOTAL_LIABILITY", "负债合计"),
        "total_equity": (
            "TOTAL_EQUITY", "TOTAL_HOLDER_EQUITY", "TOTAL_STOCKHOLDERS_EQUITY",
            "股东权益合计", "所有者权益合计",
        ),
        "parent_equity": (
            "TOTAL_PARENT_EQUITY", "PARENT_HOLDER_EQUITY", "STOCKHOLDERS_EQUITY",
            "归母股东权益",
        ),
        "total_shares": (
            "TOTAL_SHARES", "TOTAL_SHARE", "SHARES_OUTSTANDING", "ISSUED_SHARES", "总股本",
        ),
    },
    "cashflow": {
        "cfo": (
            "NETCASH_OPERATE", "NET_CASH_OPERATING", "OPERATING_CASH_FLOW",
            "CASH_FROM_OPERATING_ACTIVITIES", "经营活动产生的现金流量净额",
        ),
        "capex": (
            "CONSTRUCT_LONG_ASSET", "CAPITAL_EXPENDITURE", "CAPEX",
            "购建固定资产无形资产和其他长期资产支付的现金",
        ),
        "cash_from_investing": (
            "NETCASH_INVEST", "NET_CASH_INVESTING", "CASH_FROM_INVESTING_ACTIVITIES",
            "投资活动产生的现金流量净额",
        ),
        "cash_from_financing": (
            "NETCASH_FINANCE", "NET_CASH_FINANCING", "CASH_FROM_FINANCING_ACTIVITIES",
            "筹资活动产生的现金流量净额",
        ),
        "dividends": (
            "ASSIGN_DIVIDEND_PORFIT", "DIVIDENDS_PAID", "CASH_DIVIDENDS_PAID", "分配股利利润或偿付利息支付的现金",
        ),
        "net_borrowing": ("NET_BORROWING", "DEBT_ISSUED_REPAID_NET", "借款净增加额"),
        "net_increase_cash": (
            "NET_INCREASE_CASH", "CASH_EQUIVALENT_INCREASE", "NET_CHANGE_IN_CASH",
            "现金及现金等价物净增加额",
        ),
        "begin_cash": ("BEGIN_CASH", "CASH_BEGINNING", "期初现金及现金等价物余额"),
        "end_cash": ("END_CASH", "CASH_ENDING", "期末现金及现金等价物余额"),
        "fx_effect": ("EFFECT_EXCHANGE_RATE", "FX_EFFECT_ON_CASH", "汇率变动对现金及现金等价物的影响"),
    },
    "indicators": {
        "total_shares": ("TOTAL_SHARES", "TOTAL_SHARE", "SHARES_OUTSTANDING", "总股本"),
        "diluted_eps": ("DILUTED_EPS", "BASIC_EPS", "EPS", "每股收益"),
        "gross_margin_reported": ("GROSS_MARGIN", "GROSSPROFIT_MARGIN", "销售毛利率"),
        "roe_reported": ("ROE", "WEIGHTAVG_ROE", "净资产收益率"),
    },
}

REQUIRED_FIELDS = {
    "income": ("revenue", "operating_cost", "operating_profit", "net_profit_parent"),
    "balance": (
        "cash", "receivables", "inventory", "total_assets", "total_liabilities", "parent_equity",
    ),
    "cashflow": ("cfo", "capex", "net_increase_cash"),
}

GATE_FIELDS = {
    "income": ("revenue", "net_profit_parent"),
    "balance": ("total_assets", "total_liabilities", "parent_equity"),
    "cashflow": ("cfo",),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("raw")
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value or value in {"--", "-", "N/A", "null", "None"}:
            return None
        if value.endswith("%"):
            value = value[:-1]
            try:
                return _CTX.divide(Decimal(value), Decimal("100"))
            except InvalidOperation:
                return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _first_decimal(row: Mapping[str, Any], aliases: Iterable[str]) -> Decimal | None:
    for alias in aliases:
        if alias in row:
            parsed = _decimal(row.get(alias))
            if parsed is not None:
                return parsed
    return None


def _date_text(row: Mapping[str, Any]) -> str:
    raw = row.get("REPORT_DATE") or row.get("END_DATE") or row.get("REPORT_PERIOD") or ""
    return str(raw).strip()[:10]


def _update_text(row: Mapping[str, Any]) -> str:
    raw = (
        row.get("UPDATE_DATE") or row.get("NOTICE_DATE") or row.get("ANNOUNCE_DATE")
        or row.get("PUBLISH_DATE") or ""
    )
    return str(raw).strip()


def _fiscal_year(period_end: str) -> int | None:
    try:
        return int(period_end[:4])
    except (TypeError, ValueError):
        return None


def _period_type(cadence: str, row: Mapping[str, Any], period_end: str) -> str:
    if cadence == "annual":
        return "annual"
    report_type = str(row.get("REPORT_TYPE") or row.get("REPORT_TYPE_NAME") or "").lower()
    if period_end.endswith("-03-31") or "single" in report_type or "单季度" in report_type:
        return "quarter"
    return "ytd"


def _deduplicate_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        period_end = _date_text(row)
        if period_end:
            grouped[period_end].append(row)
    chosen: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for period_end, candidates in grouped.items():
        ordered = sorted(candidates, key=lambda item: (_update_text(item), _hash_payload(item)), reverse=True)
        chosen.append(ordered[0])
        superseded.extend(ordered[1:])
    chosen.sort(key=_date_text, reverse=True)
    return chosen[:_MAX_PERIODS_PER_SERIES], superseded


def _raw_fact(
    *, symbol: str, metric: str, value: Decimal, currency: str, period: str,
    statement: str, evidence_id: str,
) -> DerivedFinancialFact:
    fact_id = _stable_id("fact", symbol, statement, metric, period, str(value), evidence_id)
    unit = "shares" if metric == "total_shares" else (
        "ratio" if metric.endswith("_reported") else currency
    )
    return DerivedFinancialFact(
        fact_id=fact_id,
        symbol=symbol,
        metric=metric,
        value=str(value),
        unit=unit,
        period=period,
        formula=None,
        input_fact_ids=[],
        evidence_ids=[evidence_id],
        calculation_version=CALCULATION_VERSION,
        validation_status="pass",
        statement_type=statement,
    )


def _derived_fact(
    *, symbol: str, metric: str, value: Decimal, unit: str, period: str,
    formula: str, inputs: list[DerivedFinancialFact], status: str = "pass",
) -> DerivedFinancialFact:
    fact_id = _stable_id(
        "fact", symbol, metric, period, formula, str(value), [item.fact_id for item in inputs]
    )
    return DerivedFinancialFact(
        fact_id=fact_id,
        symbol=symbol,
        metric=metric,
        value=str(value),
        unit=unit,
        period=period,
        formula=formula,
        input_fact_ids=[item.fact_id for item in inputs],
        evidence_ids=list(dict.fromkeys(eid for item in inputs for eid in item.evidence_ids)),
        calculation_version=CALCULATION_VERSION,
        validation_status=status,  # type: ignore[arg-type]
        statement_type=None,
    )


def normalize_financial_snapshot(
    *,
    symbol: str,
    security_name: str,
    market: str,
    currency: str,
    statement_rows: Mapping[tuple[str, str], Iterable[Mapping[str, Any]]],
    source: str = "eastmoney",
    data_as_of: str | None = None,
) -> dict[str, Any]:
    """Normalize provider rows and calculate an auditable financial ledger."""

    retrieved_at = data_as_of or _now()
    evidence: list[EvidenceItem] = []
    periods: list[FinancialPeriod] = []
    facts: list[DerivedFinancialFact] = []
    superseded_evidence_ids: list[str] = []

    for (statement, cadence), raw_rows in sorted(statement_rows.items()):
        if statement not in FIELD_ALIASES or cadence not in {"annual", "quarter"}:
            continue
        chosen, superseded = _deduplicate_rows(raw_rows)
        for row in superseded:
            period_end = _date_text(row)
            evidence_id = _stable_id("ev", symbol, statement, cadence, period_end, _hash_payload(row))
            evidence.append(
                EvidenceItem(
                    evidence_id=evidence_id,
                    symbol=symbol,
                    domain="financial_statement",
                    source=source,
                    source_locator=f"{symbol}/{statement}/{cadence}/{period_end}",
                    retrieved_at=retrieved_at,
                    published_at=_update_text(row) or None,
                    content_hash=_hash_payload(row),
                    summary=f"{statement} {cadence} {period_end} superseded provider row",
                    status="superseded",
                )
            )
            superseded_evidence_ids.append(evidence_id)

        for row in chosen:
            period_end = _date_text(row)
            normalized_values: dict[str, str | None] = {}
            for metric, aliases in FIELD_ALIASES[statement].items():
                value = _first_decimal(row, aliases)
                normalized_values[metric] = str(value) if value is not None else None
            evidence_id = _stable_id("ev", symbol, statement, cadence, period_end, _hash_payload(row))
            present_summary = {key: value for key, value in normalized_values.items() if value is not None}
            evidence.append(
                EvidenceItem(
                    evidence_id=evidence_id,
                    symbol=symbol,
                    domain="financial_statement",
                    source=source,
                    source_locator=f"{symbol}/{statement}/{cadence}/{period_end}",
                    retrieved_at=retrieved_at,
                    published_at=_update_text(row) or None,
                    content_hash=_hash_payload(row),
                    summary=f"{statement} {cadence} {period_end}: {json.dumps(present_summary, ensure_ascii=False)}",
                    metadata={"raw_field_count": len(row), "cadence": cadence},
                )
            )
            periods.append(
                FinancialPeriod(
                    period_end=period_end,
                    period_type=_period_type(cadence, row, period_end),  # type: ignore[arg-type]
                    fiscal_year=_fiscal_year(period_end),
                    statement_type=statement,  # type: ignore[arg-type]
                    source_evidence_id=evidence_id,
                    values=normalized_values,
                    raw_update_at=_update_text(row) or None,
                )
            )
            for metric, raw_value in normalized_values.items():
                value = _decimal(raw_value)
                if value is not None:
                    facts.append(
                        _raw_fact(
                            symbol=symbol, metric=metric, value=value, currency=currency,
                            period=period_end, statement=statement, evidence_id=evidence_id,
                        )
                    )

    required = [f"{statement}.{metric}" for statement, metrics in REQUIRED_FIELDS.items() for metric in metrics]
    present = sorted(
        {
            f"{fact.statement_type}.{fact.metric}"
            for fact in facts
            if fact.statement_type in REQUIRED_FIELDS
            and fact.metric in REQUIRED_FIELDS[str(fact.statement_type)]
        }
    )
    complete_period_sets = {
        statement: {
            period.period_end
            for period in periods
            if period.statement_type == statement
            and period.period_type == "annual"
            and all(period.values.get(field) is not None for field in GATE_FIELDS[statement])
        }
        for statement in ("income", "balance", "cashflow")
    }
    comparable_periods = {
        statement: len(periods_for_statement)
        for statement, periods_for_statement in complete_period_sets.items()
    }
    common_full_periods = sorted(
        set.intersection(*(set(value) for value in complete_period_sets.values())),
        reverse=True,
    )
    coverage = FinancialCoverage(
        required_fields=required,
        present_fields=present,
        missing_fields=sorted(set(required) - set(present)),
        comparable_periods=comparable_periods,
        provider_status="live" if periods else "unavailable",
        coverage_ratio=round(len(present) / len(required), 4) if required else 0.0,
    )
    snapshot = FinancialSnapshot(
        symbol=symbol,
        security_name=security_name or symbol,
        market=market,
        report_currency=currency,
        data_as_of=retrieved_at,
        periods=sorted(periods, key=lambda item: (item.period_end, item.statement_type), reverse=True),
        coverage=coverage,
        superseded_evidence_ids=superseded_evidence_ids,
    )

    derived = derive_financial_facts(snapshot, facts)
    all_facts = facts + derived
    reconciliations = reconcile_financial_statements(snapshot, all_facts)
    alerts = detect_financial_alerts(all_facts)
    financial_gate_passed = bool(currency) and len(common_full_periods) >= 2
    latest_quarter_present = any(period.period_type in {"quarter", "ytd"} for period in periods)
    return {
        "snapshot": snapshot.to_dict(),
        "evidence": [item.__dict__ if hasattr(item, "__dict__") else {
            field: getattr(item, field) for field in item.__dataclass_fields__
        } for item in evidence],
        "facts": [item.__dict__ if hasattr(item, "__dict__") else {
            field: getattr(item, field) for field in item.__dataclass_fields__
        } for item in all_facts],
        "reconciliations": reconciliations,
        "alerts": [item.__dict__ if hasattr(item, "__dict__") else {
            field: getattr(item, field) for field in item.__dataclass_fields__
        } for item in alerts],
        "financial_gate": {
            "status": "passed" if financial_gate_passed else "failed_validation",
            "reason": None if financial_gate_passed else "three_statements_need_two_aligned_complete_annual_periods_and_known_currency",
            "coverage": coverage.coverage_ratio,
            "common_full_periods": common_full_periods,
            "required_gate_fields": GATE_FIELDS,
        },
        "latest_quarter": {
            "status": "passed" if latest_quarter_present else "insufficient_evidence",
            "reason": None if latest_quarter_present else "latest_quarter_unavailable",
        },
    }


def _fact_index(facts: Iterable[DerivedFinancialFact]) -> dict[tuple[str, str], DerivedFinancialFact]:
    index: dict[tuple[str, str], DerivedFinancialFact] = {}
    for fact in facts:
        key = (fact.metric, fact.period)
        if key not in index or (index[key].formula is not None and fact.formula is None):
            index[key] = fact
    return index


def _fact_decimal(fact: DerivedFinancialFact | None) -> Decimal | None:
    return _decimal(fact.value) if fact is not None else None


def derive_financial_facts(
    snapshot: FinancialSnapshot,
    raw_facts: list[DerivedFinancialFact],
) -> list[DerivedFinancialFact]:
    """Calculate ratios and growth metrics using only comparable annual facts."""

    annual_periods = sorted(
        {
            period.period_end for period in snapshot.periods if period.period_type == "annual"
        },
        reverse=True,
    )
    result: list[DerivedFinancialFact] = []
    index = _fact_index(raw_facts)

    def add(metric: str, value: Decimal, unit: str, period: str, formula: str, inputs: list[DerivedFinancialFact]) -> None:
        item = _derived_fact(
            symbol=snapshot.symbol, metric=metric, value=value, unit=unit,
            period=period, formula=formula, inputs=inputs,
        )
        result.append(item)
        index[(metric, period)] = item

    for period in annual_periods:
        revenue = index.get(("revenue", period))
        cost = index.get(("operating_cost", period))
        gross = index.get(("gross_profit", period))
        if gross is None and revenue and cost:
            revenue_value, cost_value = _fact_decimal(revenue), _fact_decimal(cost)
            if revenue_value is not None and cost_value is not None:
                add("gross_profit", _CTX.subtract(revenue_value, cost_value), snapshot.report_currency,
                    period, "revenue - operating_cost", [revenue, cost])

        ratio_specs = (
            ("gross_margin", "gross_profit", "revenue", "gross_profit / revenue"),
            ("operating_margin", "operating_profit", "revenue", "operating_profit / revenue"),
            ("net_margin", "net_profit_parent", "revenue", "net_profit_parent / revenue"),
            ("cfo_to_net_income", "cfo", "net_profit_parent", "cfo / net_profit_parent"),
            ("capex_to_cfo", "capex", "cfo", "abs(capex) / abs(cfo)"),
            ("cash_to_short_debt", "cash", "short_debt", "cash / short_debt"),
            ("cash_to_assets", "cash", "total_assets", "cash / total_assets"),
            ("debt_ratio", "total_liabilities", "total_assets", "total_liabilities / total_assets"),
            ("goodwill_to_equity", "goodwill", "parent_equity", "goodwill / parent_equity"),
            ("intangible_assets_to_equity", "intangible_assets", "parent_equity", "intangible_assets / parent_equity"),
            ("other_receivables_to_assets", "other_receivables", "total_assets", "other_receivables / total_assets"),
            ("impairment_to_revenue", "total_impairment", "revenue", "total_impairment / revenue"),
        )

        asset_impairment = index.get(("asset_impairment", period))
        credit_impairment = index.get(("credit_impairment", period))
        if asset_impairment or credit_impairment:
            ai = _fact_decimal(asset_impairment) or Decimal("0")
            ci = _fact_decimal(credit_impairment) or Decimal("0")
            add(
                "total_impairment", _CTX.add(ai, ci), snapshot.report_currency, period,
                "asset_impairment + credit_impairment",
                [item for item in (asset_impairment, credit_impairment) if item is not None],
            )

        debt_parts = [
            index.get((metric, period))
            for metric in ("short_debt", "current_debt_due", "long_debt", "bonds_payable")
        ]
        debt_inputs = [item for item in debt_parts if item is not None]
        if debt_inputs:
            debt = sum((_fact_decimal(item) or Decimal("0") for item in debt_inputs), Decimal("0"))
            add("interest_bearing_debt", debt, snapshot.report_currency, period,
                "short_debt + current_debt_due + long_debt + bonds_payable", debt_inputs)
            total_assets = index.get(("total_assets", period))
            total_assets_value = _fact_decimal(total_assets)
            if total_assets and total_assets_value not in {None, Decimal("0")}:
                add(
                    "interest_bearing_debt_to_assets",
                    _CTX.divide(debt, total_assets_value),
                    "ratio",
                    period,
                    "interest_bearing_debt / total_assets",
                    [index[("interest_bearing_debt", period)], total_assets],
                )
            cash = index.get(("cash", period))
            if cash and debt != 0:
                add("cash_to_interest_bearing_debt", _CTX.divide(_fact_decimal(cash) or Decimal("0"), debt),
                    "ratio", period, "cash / interest_bearing_debt", [cash, index[("interest_bearing_debt", period)]])

        working_inputs = [
            index.get((metric, period))
            for metric in ("receivables", "inventory", "contract_assets", "contract_liabilities")
        ]
        if any(working_inputs):
            receivables = _fact_decimal(index.get(("receivables", period))) or Decimal("0")
            inventory = _fact_decimal(index.get(("inventory", period))) or Decimal("0")
            contract_assets = _fact_decimal(index.get(("contract_assets", period))) or Decimal("0")
            contract_liabilities = _fact_decimal(index.get(("contract_liabilities", period))) or Decimal("0")
            value = _CTX.subtract(_CTX.add(_CTX.add(receivables, inventory), contract_assets), contract_liabilities)
            add("operating_working_capital", value, snapshot.report_currency, period,
                "receivables + inventory + contract_assets - contract_liabilities",
                [item for item in working_inputs if item is not None])

        cfo = index.get(("cfo", period))
        net_income = index.get(("net_profit_parent", period))
        cfo_value, net_income_value = _fact_decimal(cfo), _fact_decimal(net_income)
        if cfo and net_income and net_income_value not in {None, Decimal("0")} and cfo_value is not None:
            divergence = _CTX.divide(_CTX.subtract(cfo_value, net_income_value), abs(net_income_value))
            add("cash_profit_divergence", divergence, "ratio", period,
                "(cfo - net_profit_parent) / abs(net_profit_parent)", [cfo, net_income])

        deducted = index.get(("deducted_net_profit", period))
        deducted_value = _fact_decimal(deducted)
        if net_income and deducted and net_income_value not in {None, Decimal("0")} and deducted_value is not None:
            nonrecurring = _CTX.subtract(net_income_value, deducted_value)
            add(
                "nonrecurring_profit",
                nonrecurring,
                snapshot.report_currency,
                period,
                "net_profit_parent - deducted_net_profit",
                [net_income, deducted],
            )
            add(
                "nonrecurring_profit_to_net_income",
                _CTX.divide(nonrecurring, abs(net_income_value)),
                "ratio",
                period,
                "nonrecurring_profit / abs(net_profit_parent)",
                [index[("nonrecurring_profit", period)], net_income],
            )

        for metric, numerator_name, denominator_name, formula in ratio_specs:
            if (metric, period) in index:
                continue
            numerator = index.get((numerator_name, period))
            denominator = index.get((denominator_name, period))
            numerator_value, denominator_value = _fact_decimal(numerator), _fact_decimal(denominator)
            if numerator is None or denominator is None or numerator_value is None or denominator_value in {None, Decimal("0")}:
                continue
            if metric == "capex_to_cfo":
                value = _CTX.divide(abs(numerator_value), abs(denominator_value))
            else:
                value = _CTX.divide(numerator_value, denominator_value)
            add(metric, value, "ratio", period, formula, [numerator, denominator])

    growth_metrics = (
        "revenue", "gross_profit", "net_profit_parent", "cfo", "receivables",
        "other_receivables", "inventory", "contract_liabilities",
    )
    for current, previous in zip(annual_periods, annual_periods[1:]):
        current_working = index.get(("operating_working_capital", current))
        previous_working = index.get(("operating_working_capital", previous))
        current_working_value = _fact_decimal(current_working)
        previous_working_value = _fact_decimal(previous_working)
        if (
            current_working is not None
            and previous_working is not None
            and current_working_value is not None
            and previous_working_value is not None
        ):
            add(
                "operating_working_capital_change",
                _CTX.subtract(current_working_value, previous_working_value),
                snapshot.report_currency,
                current,
                "operating_working_capital[current] - operating_working_capital[previous]",
                [current_working, previous_working],
            )
        for metric in growth_metrics:
            current_fact = index.get((metric, current))
            previous_fact = index.get((metric, previous))
            current_value, previous_value = _fact_decimal(current_fact), _fact_decimal(previous_fact)
            if current_fact is None or previous_fact is None or current_value is None or previous_value in {None, Decimal("0")}:
                continue
            yoy = _CTX.divide(_CTX.subtract(current_value, previous_value), abs(previous_value))
            add(f"{metric}_yoy", yoy, "ratio", current,
                f"({metric}[current] - {metric}[previous]) / abs({metric}[previous])",
                [current_fact, previous_fact])

        revenue_yoy = index.get(("revenue_yoy", current))
        receivables_yoy = index.get(("receivables_yoy", current))
        inventory_yoy = index.get(("inventory_yoy", current))
        if revenue_yoy and receivables_yoy:
            add("receivables_vs_revenue_growth_gap",
                _CTX.subtract(_fact_decimal(receivables_yoy) or Decimal("0"), _fact_decimal(revenue_yoy) or Decimal("0")),
                "ratio", current, "receivables_yoy - revenue_yoy", [receivables_yoy, revenue_yoy])
        if revenue_yoy and inventory_yoy:
            add("inventory_vs_revenue_growth_gap",
                _CTX.subtract(_fact_decimal(inventory_yoy) or Decimal("0"), _fact_decimal(revenue_yoy) or Decimal("0")),
                "ratio", current, "inventory_yoy - revenue_yoy", [inventory_yoy, revenue_yoy])

    return result


def _reconciliation_status(actual: Decimal, expected: Decimal) -> tuple[str, Decimal]:
    denominator = max(abs(actual), abs(expected), Decimal("1e-28"))
    difference = _CTX.divide(abs(_CTX.subtract(actual, expected)), denominator)
    if difference <= Decimal("0.01"):
        return "pass", difference
    if difference <= Decimal("0.03"):
        return "warning", difference
    return "fail", difference


def reconcile_financial_statements(
    snapshot: FinancialSnapshot,
    facts: list[DerivedFinancialFact],
) -> list[dict[str, Any]]:
    """Run only accounting identities that can be reconstructed safely."""

    index = _fact_index(facts)
    annual_periods = sorted(
        {period.period_end for period in snapshot.periods if period.period_type == "annual"},
        reverse=True,
    )
    checks: list[dict[str, Any]] = []

    def missing(rule: str, period: str, status: str, reason: str) -> None:
        checks.append({
            "check_id": _stable_id("check", snapshot.symbol, rule, period),
            "rule": rule,
            "period": period,
            "status": status,
            "reason": reason,
            "input_fact_ids": [],
        })

    for period in annual_periods:
        assets = index.get(("total_assets", period))
        liabilities = index.get(("total_liabilities", period))
        equity = index.get(("total_equity", period))
        if assets and liabilities and equity:
            actual = _fact_decimal(assets) or Decimal("0")
            expected = _CTX.add(_fact_decimal(liabilities) or Decimal("0"), _fact_decimal(equity) or Decimal("0"))
            status, difference = _reconciliation_status(actual, expected)
            checks.append({
                "check_id": _stable_id("check", snapshot.symbol, "assets_equals_liabilities_plus_equity", period),
                "rule": "assets = liabilities + equity",
                "period": period,
                "actual": str(actual),
                "expected": str(expected),
                "relative_difference_pct": str(_CTX.multiply(difference, Decimal("100"))),
                "status": status,
                "input_fact_ids": [assets.fact_id, liabilities.fact_id, equity.fact_id],
            })
        else:
            missing("assets = liabilities + equity", period, "insufficient_data", "required balance-sheet fields missing")

        gross = next((fact for fact in facts if fact.metric == "gross_profit" and fact.period == period and fact.formula is None), None)
        revenue = index.get(("revenue", period))
        cost = index.get(("operating_cost", period))
        if gross and revenue and cost:
            actual = _fact_decimal(gross) or Decimal("0")
            expected = _CTX.subtract(_fact_decimal(revenue) or Decimal("0"), _fact_decimal(cost) or Decimal("0"))
            status, difference = _reconciliation_status(actual, expected)
            checks.append({
                "check_id": _stable_id("check", snapshot.symbol, "gross_profit_identity", period),
                "rule": "gross_profit = revenue - operating_cost",
                "period": period,
                "actual": str(actual),
                "expected": str(expected),
                "relative_difference_pct": str(_CTX.multiply(difference, Decimal("100"))),
                "status": status,
                "input_fact_ids": [gross.fact_id, revenue.fact_id, cost.fact_id],
            })
        else:
            missing("gross_profit = revenue - operating_cost", period, "insufficient_data", "reported gross profit or revenue/cost missing")

    for period in annual_periods:
        begin_cash = index.get(("begin_cash", period))
        end_cash = index.get(("end_cash", period))
        net_change = index.get(("net_increase_cash", period))
        if begin_cash and end_cash and net_change:
            actual = _fact_decimal(end_cash) or Decimal("0")
            expected = _CTX.add(
                _fact_decimal(begin_cash) or Decimal("0"),
                _fact_decimal(net_change) or Decimal("0"),
            )
            status, difference = _reconciliation_status(actual, expected)
            checks.append({
                "check_id": _stable_id("check", snapshot.symbol, "cash_bridge", period),
                "rule": "ending_cash = beginning_cash + net_change_cash",
                "period": period,
                "actual": str(actual),
                "expected": str(expected),
                "relative_difference_pct": str(_CTX.multiply(difference, Decimal("100"))),
                "status": status,
                "input_fact_ids": [end_cash.fact_id, begin_cash.fact_id, net_change.fact_id],
            })
        else:
            missing(
                "ending_cash = beginning_cash + net_change_cash",
                period,
                "insufficient_data",
                "cash-flow statement beginning/end cash or net change is missing",
            )
    return checks


def detect_financial_alerts(facts: list[DerivedFinancialFact]) -> list[FinancialAlert]:
    """Return review triggers, never a fraud score or accusation."""

    by_metric: dict[str, list[DerivedFinancialFact]] = defaultdict(list)
    for fact in facts:
        by_metric[fact.metric].append(fact)
    for values in by_metric.values():
        values.sort(key=lambda item: item.period, reverse=True)

    alerts: list[FinancialAlert] = []

    def add(
        rule: str,
        selected: list[DerivedFinancialFact],
        finding: str,
        explanations: list[str],
        checks: list[str],
        *,
        severity: str = "review",
        consecutive: bool = False,
    ) -> None:
        alerts.append(
            FinancialAlert(
                alert_id=_stable_id("alert", rule, [item.fact_id for item in selected]),
                rule=rule,
                severity=severity,  # type: ignore[arg-type]
                periods=[item.period for item in selected],
                fact_ids=[item.fact_id for item in selected],
                consecutive=consecutive,
                finding=finding,
                normal_explanations=explanations,
                next_checks=checks,
            )
        )

    for fact in by_metric.get("receivables_vs_revenue_growth_gap", []):
        if (_fact_decimal(fact) or Decimal("0")) > Decimal("0.10"):
            add(
                "receivables_growth_outpaces_revenue",
                [fact],
                "应收款增速比收入增速高出超过10个百分点，需要核查回款质量。",
                ["业务季节性或大客户账期变化", "项目验收节点导致暂时性应收增加"],
                ["应收账款账龄和坏账准备", "前五大客户及期后回款", "合同资产与验收政策"],
            )
    for fact in by_metric.get("inventory_vs_revenue_growth_gap", []):
        if (_fact_decimal(fact) or Decimal("0")) > Decimal("0.15"):
            add(
                "inventory_growth_outpaces_revenue",
                [fact],
                "存货增速比收入增速高出超过15个百分点，需要核查备货与跌价风险。",
                ["战略备货或新产品爬坡", "原材料价格周期和供应链安全库存"],
                ["存货构成与库龄", "跌价准备计提", "期后去库存和订单兑现"],
            )

    weak_cash = [
        fact for fact in by_metric.get("cfo_to_net_income", [])
        if (_fact_decimal(fact) or Decimal("999")) < Decimal("0.70")
    ]
    if len(weak_cash) >= 2:
        add(
            "cash_conversion_weak_two_years",
            weak_cash[:2],
            "连续两个完整年度经营现金流低于归母净利润的70%。",
            ["扩张期营运资金占用", "结算周期或税费支付时点变化"],
            ["经营现金流附注", "应收、存货和合同负债变化", "期后现金回款"],
            severity="elevated",
            consecutive=True,
        )

    cash_to_assets = {fact.period: fact for fact in by_metric.get("cash_to_assets", [])}
    debt_to_assets = {fact.period: fact for fact in by_metric.get("interest_bearing_debt_to_assets", [])}
    for period in sorted(set(cash_to_assets) & set(debt_to_assets), reverse=True):
        cash_share = cash_to_assets[period]
        debt_share = debt_to_assets[period]
        if (
            (_fact_decimal(cash_share) or Decimal("0")) >= Decimal("0.20")
            and (_fact_decimal(debt_share) or Decimal("0")) >= Decimal("0.20")
        ):
            add(
                "high_cash_and_debt_coexist",
                [cash_share, debt_share],
                "现金和有息债务均达到总资产的20%以上，需结合受限资金和融资成本解释并存原因。",
                ["经营区域或主体之间资金受限", "预留并购、建设或季节性采购资金"],
                ["受限资金明细", "债务利率与到期结构", "母子公司资金可调度性"],
            )
            break

    for fact in by_metric.get("goodwill_to_equity", []):
        if (_fact_decimal(fact) or Decimal("0")) > Decimal("0.30"):
            add(
                "goodwill_weight_high",
                [fact],
                "商誉超过归母权益的30%，减值测试可能对净资产和利润造成较大影响。",
                ["并购标的仍处于高速增长期", "可辨认资产较少导致收购溢价集中于商誉"],
                ["商誉对应资产组", "减值测试关键假设", "被收购业务业绩承诺完成度"],
                severity="elevated",
            )
            break

    for fact in by_metric.get("intangible_assets_to_equity", []):
        if (_fact_decimal(fact) or Decimal("0")) > Decimal("0.30"):
            add(
                "capitalized_assets_weight_high",
                [fact],
                "无形资产超过归母权益的30%，需要核查资本化政策、摊销年限和减值测试。",
                ["研发密集型商业模式", "特许权或长期软件资产占比较高"],
                ["研发费用资本化条件", "无形资产构成与剩余摊销期", "减值测试假设"],
                severity="elevated",
            )
            break

    other_receivable_growth = by_metric.get("other_receivables_yoy", [])
    other_receivable_weight = {fact.period: fact for fact in by_metric.get("other_receivables_to_assets", [])}
    for growth in other_receivable_growth:
        weight = other_receivable_weight.get(growth.period)
        if (
            (_fact_decimal(growth) or Decimal("0")) > Decimal("0.50")
            or (weight is not None and (_fact_decimal(weight) or Decimal("0")) > Decimal("0.10"))
        ):
            add(
                "other_receivables_change_material",
                [growth, *([weight] if weight else [])],
                "其他应收款增速超过50%或占总资产超过10%，需要核查交易对手与款项性质。",
                ["保证金、押金或临时往来款增加", "并购处置或关联方结算时点变化"],
                ["其他应收款前五名", "关联方余额", "账龄、坏账准备与期后回收"],
            )
            break

    nonrecurring = by_metric.get("nonrecurring_profit_to_net_income", [])
    for current, previous in zip(nonrecurring, nonrecurring[1:]):
        current_value = _fact_decimal(current)
        previous_value = _fact_decimal(previous)
        if (
            current_value is not None
            and previous_value is not None
            and (abs(current_value) > Decimal("0.30") or abs(current_value - previous_value) > Decimal("0.30"))
        ):
            add(
                "nonrecurring_profit_swing",
                [current, previous],
                "非经常性损益占归母净利润比例较高或年度变化超过30个百分点。",
                ["政府补助、资产处置或公允价值变动", "行业周期中的一次性损失或收益"],
                ["非经常性损益明细", "补助与处置收益可持续性", "扣非利润趋势"],
            )
            break

    revenue_growth = {fact.period: fact for fact in by_metric.get("revenue_yoy", [])}
    cfo_growth = {fact.period: fact for fact in by_metric.get("cfo_yoy", [])}
    contract_growth = {fact.period: fact for fact in by_metric.get("contract_liabilities_yoy", [])}
    for period in sorted(set(revenue_growth) & set(cfo_growth) & set(contract_growth), reverse=True):
        revenue_fact = revenue_growth[period]
        cfo_fact = cfo_growth[period]
        contract_fact = contract_growth[period]
        if (
            (_fact_decimal(revenue_fact) or Decimal("0")) > Decimal("0.10")
            and (_fact_decimal(cfo_fact) or Decimal("0")) <= Decimal("0")
            and (_fact_decimal(contract_fact) or Decimal("0")) <= Decimal("0")
        ):
            add(
                "revenue_growth_without_cash_or_contract_support",
                [revenue_fact, cfo_fact, contract_fact],
                "收入增长超过10%，但经营现金流与合同负债未同步改善，需要核查回款和订单质量。",
                ["收入确认与回款存在正常时间差", "合同结构从预收转为验收后结算"],
                ["应收账款期后回款", "合同负债变动附注", "主要订单交付与验收节点"],
            )
            break

    impairments = by_metric.get("impairment_to_revenue", [])
    for current, previous in zip(impairments, impairments[1:]):
        current_value, previous_value = _fact_decimal(current), _fact_decimal(previous)
        if current_value is not None and previous_value is not None and abs(current_value - previous_value) > Decimal("0.05"):
            add(
                "impairment_ratio_swing",
                [current, previous],
                "减值损失占收入比例年度变化超过5个百分点。",
                ["一次性资产处置或行业周期触底", "会计估计和资产组口径调整"],
                ["减值项目构成", "估值参数和会计估计变更", "次年是否转回或继续计提"],
            )
            break
    return alerts
