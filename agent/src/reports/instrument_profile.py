"""Durable, source-labelled instrument snapshots for report dossiers.

The report archive must not reinterpret ad-hoc facts emitted by individual
daily, weekly, monitoring, or deep-research producers.  This module owns one
normalised contract for current identity, market scale, valuation, and dividend
metadata.  Refreshing is explicit and append-only; reading a dossier only reads
the latest stored snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

from backtest.loaders.eastmoney_client import get_json, resolve_secid
from backtest.loaders.yahoo_client import get_quote_summary
from backtest.loaders._http import resolve_min_interval, throttled_get
from src.research.knowledge import ResearchKnowledgeStore


_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_QUOTE_DELAY_URL = "https://push2delay.eastmoney.com/api/qt/stock/get"
_DIVIDEND_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EASTMONEY_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_SINA_ETF_DIVIDEND_URL = "https://finance.sina.com.cn/realstock/company/{symbol}/hfq.js"
_QUOTE_FIELDS = (
    "f43,f50,f57,f58,f84,f85,f86,f107,f116,f117,f127,f128,f129,"
    "f162,f163,f164,f167,f168,f169,f170,f173,f189"
)
_ETF_PREFIXES = ("15", "16", "50", "51", "52", "56", "58")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _yahoo_raw(value: Any) -> Any:
    return value.get("raw") if isinstance(value, dict) else value


def _quote_time(value: Any, fallback: str) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return fallback
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return fallback


def _listing_date(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 8:
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def instrument_type(symbol: str) -> str:
    clean = str(symbol or "").strip().upper()
    code, _, market = clean.partition(".")
    if market in {"CSI", "INDEX"}:
        return "index"
    if (market == "SH" and code.startswith(("000", "950", "990"))) or (
        market == "SZ" and code.startswith("399")
    ):
        return "index"
    return "etf" if code.startswith(_ETF_PREFIXES) else "company_equity"


def _sina_get_text(url: str) -> str:
    response = throttled_get(
        url,
        host_key="sina",
        min_interval=resolve_min_interval("VIBE_TRADING_SINA_MIN_INTERVAL", 0.5),
        params={},
        headers={"Referer": "https://finance.sina.com.cn/fund/"},
        timeout=15,
    )
    response.raise_for_status()
    return response.text


def _metric(
    key: str,
    label: str,
    value: Any,
    unit: str,
    category: str,
    *,
    source_id: str,
    data_as_of: str,
    raw_field: str,
    semantics: str,
    unavailable_reason: str = "上游当前未提供该字段",
) -> dict[str, Any]:
    parsed = _number(value)
    return {
        "key": key,
        "label": label,
        "value": parsed,
        "unit": unit,
        "category": category,
        "status": "available" if parsed is not None else "unavailable",
        "unavailable_reason": None if parsed is not None else unavailable_reason,
        "source_id": source_id,
        "data_as_of": data_as_of,
        "raw_field": raw_field,
        "semantics": semantics,
    }


class EastmoneyInstrumentProfileProvider:
    """Fetch a normalised China-listed equity or ETF profile without auth."""

    def __init__(
        self,
        *,
        get_json_fn: Callable[..., Any] = get_json,
        get_text_fn: Callable[[str], str] = _sina_get_text,
        get_quote_summary_fn: Callable[[str, list[str]], dict[str, Any]] = get_quote_summary,
        now_provider: Callable[[], str] = _utc_now,
    ) -> None:
        self.get_json = get_json_fn
        self.get_text = get_text_fn
        self.get_quote_summary = get_quote_summary_fn
        self.now_provider = now_provider

    def fetch(self, symbol: str) -> dict[str, Any]:
        clean = str(symbol or "").strip().upper()
        if clean.endswith((".US", ".HK")):
            return self._fetch_yahoo(clean)
        if not clean.endswith((".SH", ".SZ", ".BJ")):
            raise ValueError("instrument profile currently supports China, US, and HK listings")
        retrieved_at = self.now_provider()
        secid = resolve_secid(clean)
        quote_params = {
            "secid": secid,
            "fields": _QUOTE_FIELDS,
            "fltt": "2",
            "invt": "2",
            "ut": _EASTMONEY_UT,
        }
        quote_url = _QUOTE_URL
        quote_provider_id = "eastmoney_push2_quote"
        transport_warning: str | None = None
        try:
            quote_payload = self.get_json(
                quote_url,
                params=quote_params,
                timeout=15,
                urllib_fallback=True,
            )
        except Exception:
            quote_url = _QUOTE_DELAY_URL
            quote_provider_id = "eastmoney_push2_delay_quote"
            quote_payload = self.get_json(
                quote_url,
                params=quote_params,
                timeout=15,
                urllib_fallback=True,
            )
            transport_warning = "主行情通道限流，本快照使用东方财富延迟行情容灾通道。"
        quote = (quote_payload or {}).get("data")
        if not isinstance(quote, dict) or not quote.get("f57"):
            raise RuntimeError(f"Eastmoney quote returned no instrument data for {clean}")

        kind = instrument_type(clean)
        quote_as_of = _quote_time(quote.get("f86"), retrieved_at)
        quote_source_id = f"{quote_provider_id}:{clean}:{quote_as_of}"
        is_etf = kind == "etf"
        is_index = kind == "index"
        is_company = kind == "company_equity"
        metrics = [
            _metric(
                "current_price",
                "最新价",
                quote.get("f43"),
                "CNY",
                "market",
                source_id=quote_source_id,
                data_as_of=quote_as_of,
                raw_field="f43",
                semantics="latest_exchange_quote",
            ),
            _metric(
                "price_change_pct",
                "当日涨跌幅",
                quote.get("f170"),
                "pct",
                "market",
                source_id=quote_source_id,
                data_as_of=quote_as_of,
                raw_field="f170",
                semantics="change_from_previous_close_percent",
            ),
            _metric(
                "turnover_rate",
                "换手率",
                quote.get("f168"),
                "pct",
                "market",
                source_id=quote_source_id,
                data_as_of=quote_as_of,
                raw_field="f168",
                semantics="exchange_turnover_rate_percent",
            ),
            _metric(
                "total_market_cap",
                (
                    "ETF 市值（价格×份额）"
                    if is_etf else "指数成份总市值" if is_index else "总市值"
                ),
                quote.get("f116"),
                "CNY",
                "scale",
                source_id=quote_source_id,
                data_as_of=quote_as_of,
                raw_field="f116",
                semantics="market_price_times_total_units" if is_etf else "total_equity_market_cap",
            ),
            _metric(
                "circulating_market_cap",
                "流通市值",
                quote.get("f117"),
                "CNY",
                "scale",
                source_id=quote_source_id,
                data_as_of=quote_as_of,
                raw_field="f117",
                semantics="market_price_times_circulating_units",
            ),
            _metric(
                "total_shares",
                "基金份额" if is_etf else "总股本",
                quote.get("f84"),
                "fund_units" if is_etf else "shares",
                "scale",
                source_id=quote_source_id,
                data_as_of=quote_as_of,
                raw_field="f84",
                semantics="listed_fund_units" if is_etf else "total_shares_outstanding",
            ),
            _metric(
                "float_shares",
                "流通份额" if is_etf else "流通股本",
                quote.get("f85"),
                "fund_units" if is_etf else "shares",
                "scale",
                source_id=quote_source_id,
                data_as_of=quote_as_of,
                raw_field="f85",
                semantics="circulating_fund_units" if is_etf else "circulating_shares",
            ),
        ]

        sources = [{
            "source_id": quote_source_id,
            "provider_id": quote_provider_id,
            "label": "东方财富延迟行情" if quote_url == _QUOTE_DELAY_URL else "东方财富实时行情",
            "data_as_of": quote_as_of,
            "retrieved_at": retrieved_at,
            "url": f"{quote_url}?{urlencode(quote_params)}",
        }]
        warnings: list[str] = []
        if transport_warning:
            warnings.append(transport_warning)

        if is_etf:
            warnings.append("ETF 自身不适用公司 PE/PB；跟踪指数估值应使用独立指数口径。")
            distributions, distribution_source, distribution_warning = self._etf_distribution(
                clean,
                current_price=_number(quote.get("f43")),
                quote_as_of=quote_as_of,
                retrieved_at=retrieved_at,
            )
            metrics.extend(distributions)
            if distribution_source:
                sources.append(distribution_source)
            if distribution_warning:
                warnings.append(distribution_warning)
        elif is_company:
            metrics.extend([
                _metric(
                    "pe_dynamic", "动态市盈率", quote.get("f162"), "multiple", "valuation",
                    source_id=quote_source_id, data_as_of=quote_as_of, raw_field="f162",
                    semantics="provider_dynamic_pe",
                ),
                _metric(
                    "pe_ttm", "市盈率 TTM", quote.get("f164"), "multiple", "valuation",
                    source_id=quote_source_id, data_as_of=quote_as_of, raw_field="f164",
                    semantics="trailing_twelve_month_pe",
                ),
                _metric(
                    "pe_static", "静态市盈率", quote.get("f163"), "multiple", "valuation",
                    source_id=quote_source_id, data_as_of=quote_as_of, raw_field="f163",
                    semantics="provider_static_pe",
                ),
                _metric(
                    "pb", "市净率", quote.get("f167"), "multiple", "valuation",
                    source_id=quote_source_id, data_as_of=quote_as_of, raw_field="f167",
                    semantics="price_to_book",
                ),
                _metric(
                    "roe", "净资产收益率", quote.get("f173"), "pct", "profitability",
                    source_id=quote_source_id, data_as_of=quote_as_of, raw_field="f173",
                    semantics="provider_reported_roe_percent",
                ),
            ])
            dividend, dividend_source, dividend_warning = self._stock_dividend_ttm(
                clean,
                current_price=_number(quote.get("f43")),
                quote_as_of=quote_as_of,
                retrieved_at=retrieved_at,
            )
            if dividend_source:
                sources.append(dividend_source)
            metrics.extend(dividend)
            if dividend_warning:
                warnings.append(dividend_warning)
            if not any(
                item["key"] == "dividend_yield_ttm"
                and item["status"] == "available"
                for item in metrics
            ):
                warnings.append("暂无可核验的近 12 个月现金分红数据。")

        else:
            metrics.extend([
                _metric(
                    "pe_ttm", "指数滚动市盈率", quote.get("f164"), "multiple", "valuation",
                    source_id=quote_source_id, data_as_of=quote_as_of, raw_field="f164",
                    semantics="provider_index_trailing_pe",
                ),
                _metric(
                    "pb", "指数市净率", quote.get("f167"), "multiple", "valuation",
                    source_id=quote_source_id, data_as_of=quote_as_of, raw_field="f167",
                    semantics="provider_index_price_to_book",
                ),
            ])
            warnings.append("指数历史估值分位使用独立指数口径，不以成份公司或当前行情反推。")

        core_keys = (
            ("current_price", "total_market_cap", "total_shares")
            if is_etf
            else ("current_price",) if is_index
            else ("current_price", "total_market_cap", "total_shares", "pe_ttm", "pb")
        )
        available = {item["key"] for item in metrics if item["status"] == "available"}
        quality_status = "complete" if all(key in available for key in core_keys) else "partial"
        identity = {
            "symbol": clean,
            "name": str(quote.get("f58") or clean),
            "instrument_type": kind,
            "exchange": clean.rsplit(".", 1)[-1],
            "currency": "CNY",
            "industry": (str(quote.get("f127") or "") or None) if is_company else None,
            "region": (str(quote.get("f128") or "") or None) if is_company else None,
            "concepts": [] if not is_company else [
                item.strip() for item in str(quote.get("f129") or "").split(",") if item.strip()
            ],
            "listing_date": _listing_date(quote.get("f189")),
        }
        return {
            "schema_version": 1,
            "symbol": clean,
            "instrument_type": kind,
            "data_as_of": quote_as_of,
            "retrieved_at": retrieved_at,
            "quality_status": quality_status,
            "identity": identity,
            "metrics": metrics,
            "sources": sources,
            "warnings": warnings,
        }

    def _fetch_yahoo(self, symbol: str) -> dict[str, Any]:
        retrieved_at = self.now_provider()
        summary = self.get_quote_summary(
            symbol,
            ["price", "summaryDetail", "defaultKeyStatistics", "financialData", "assetProfile"],
        )
        if not isinstance(summary, dict) or not summary:
            raise RuntimeError(f"Yahoo quoteSummary returned no instrument data for {symbol}")
        price = dict(summary.get("price") or {})
        detail = dict(summary.get("summaryDetail") or {})
        stats = dict(summary.get("defaultKeyStatistics") or {})
        financials = dict(summary.get("financialData") or {})
        asset = dict(summary.get("assetProfile") or {})
        current_price = _number(_yahoo_raw(price.get("regularMarketPrice")))
        shares = _number(_yahoo_raw(stats.get("sharesOutstanding")))
        float_shares = _number(_yahoo_raw(stats.get("floatShares")))
        market_cap = _number(
            _yahoo_raw(price.get("marketCap")) or _yahoo_raw(detail.get("marketCap"))
        )
        currency = str(
            _yahoo_raw(price.get("currency"))
            or _yahoo_raw(detail.get("currency"))
            or ("HKD" if symbol.endswith(".HK") else "USD")
        ).upper()
        data_as_of = _quote_time(_yahoo_raw(price.get("regularMarketTime")), retrieved_at)
        source_id = f"yahoo_quote_summary:{symbol}:{data_as_of}"
        source_url = f"https://finance.yahoo.com/quote/{symbol.split('.', 1)[0]}/"
        metrics = [
            _metric(
                "current_price", "最新价", current_price, currency, "market",
                source_id=source_id, data_as_of=data_as_of, raw_field="regularMarketPrice",
                semantics="latest_exchange_quote",
            ),
            _metric(
                "price_change_pct", "当日涨跌幅",
                _yahoo_raw(price.get("regularMarketChangePercent")), "ratio", "market",
                source_id=source_id, data_as_of=data_as_of,
                raw_field="regularMarketChangePercent",
                semantics="change_from_previous_close_ratio",
            ),
            _metric(
                "total_market_cap", "总市值", market_cap, currency, "scale",
                source_id=source_id, data_as_of=data_as_of, raw_field="marketCap",
                semantics="total_equity_market_cap",
            ),
            _metric(
                "circulating_market_cap", "流通市值",
                current_price * float_shares
                if current_price is not None and float_shares is not None else None,
                currency, "scale", source_id=source_id, data_as_of=data_as_of,
                raw_field="regularMarketPrice*floatShares",
                semantics="derived_market_price_times_float_shares",
            ),
            _metric(
                "total_shares", "总股本", shares, "shares", "scale",
                source_id=source_id, data_as_of=data_as_of, raw_field="sharesOutstanding",
                semantics="total_shares_outstanding",
            ),
            _metric(
                "float_shares", "流通股本", float_shares, "shares", "scale",
                source_id=source_id, data_as_of=data_as_of, raw_field="floatShares",
                semantics="public_float_shares",
            ),
            _metric(
                "pe_ttm", "市盈率 TTM", _yahoo_raw(detail.get("trailingPE")),
                "multiple", "valuation", source_id=source_id, data_as_of=data_as_of,
                raw_field="trailingPE", semantics="trailing_twelve_month_pe",
            ),
            _metric(
                "pe_forward", "远期市盈率", _yahoo_raw(detail.get("forwardPE")),
                "multiple", "valuation", source_id=source_id, data_as_of=data_as_of,
                raw_field="forwardPE", semantics="provider_forward_pe",
            ),
            _metric(
                "pb", "市净率", _yahoo_raw(stats.get("priceToBook")),
                "multiple", "valuation", source_id=source_id, data_as_of=data_as_of,
                raw_field="priceToBook", semantics="price_to_book",
            ),
            _metric(
                "roe", "净资产收益率", _yahoo_raw(financials.get("returnOnEquity")),
                "ratio", "profitability", source_id=source_id, data_as_of=data_as_of,
                raw_field="returnOnEquity", semantics="provider_reported_roe_ratio",
            ),
            _metric(
                "dividend_yield_indicated", "指示性年股息率",
                _yahoo_raw(detail.get("dividendYield")), "ratio", "dividend",
                source_id=source_id, data_as_of=data_as_of, raw_field="dividendYield",
                semantics="provider_indicated_annual_dividend_yield",
                unavailable_reason="Yahoo 当前未提供指示性股息率",
            ),
            _metric(
                "indicated_dividend_rate", "指示性每股年股息",
                _yahoo_raw(detail.get("dividendRate")), f"{currency}_per_share", "dividend",
                source_id=source_id, data_as_of=data_as_of, raw_field="dividendRate",
                semantics="provider_indicated_annual_dividend_per_share",
                unavailable_reason="Yahoo 当前未提供指示性每股年股息",
            ),
        ]
        available = {item["key"] for item in metrics if item["status"] == "available"}
        core = ("current_price", "total_market_cap", "total_shares", "pe_ttm", "pb")
        quality_status = "complete" if all(key in available for key in core) else "partial"
        return {
            "schema_version": 1,
            "symbol": symbol,
            "instrument_type": "company_equity",
            "data_as_of": data_as_of,
            "retrieved_at": retrieved_at,
            "quality_status": quality_status,
            "identity": {
                "symbol": symbol,
                "name": str(
                    _yahoo_raw(price.get("longName"))
                    or _yahoo_raw(price.get("shortName"))
                    or symbol
                ),
                "instrument_type": "company_equity",
                "exchange": str(_yahoo_raw(price.get("exchangeName")) or symbol.rsplit(".", 1)[-1]),
                "currency": currency,
                "industry": str(_yahoo_raw(asset.get("industry")) or "") or None,
                "region": str(_yahoo_raw(asset.get("country")) or "") or None,
                "concepts": [str(_yahoo_raw(asset.get("sector")))]
                if _yahoo_raw(asset.get("sector")) else [],
                "listing_date": None,
            },
            "metrics": metrics,
            "sources": [{
                "source_id": source_id,
                "provider_id": "yahoo_quote_summary",
                "label": "Yahoo Finance 标的资料",
                "data_as_of": data_as_of,
                "retrieved_at": retrieved_at,
                "url": source_url,
            }],
            "warnings": [],
        }

    def _etf_distribution(
        self,
        symbol: str,
        *,
        current_price: float | None,
        quote_as_of: str,
        retrieved_at: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
        exchange = symbol.rsplit(".", 1)[-1].lower()
        code = symbol.split(".", 1)[0]
        url = _SINA_ETF_DIVIDEND_URL.format(symbol=f"{exchange}{code}")
        source_id = f"sina:etf_distribution:{symbol}:{quote_as_of[:10]}"
        reason = "未发现可核验的 ETF 分红记录"
        rows: list[dict[str, Any]] = []
        source: dict[str, Any] | None = None
        warning: str | None = None
        try:
            raw = self.get_text(url)
            body = raw.split("=", 1)[1].split("\n/*", 1)[0].strip().rstrip(";")
            payload = json.loads(body)
            raw_rows = payload.get("data") if isinstance(payload, dict) else []
            for item in raw_rows or []:
                if not isinstance(item, dict) or str(item.get("d")) == "1900-01-01":
                    continue
                try:
                    date_value = datetime.strptime(str(item.get("d")), "%Y-%m-%d").date()
                    cumulative = float(item.get("u"))
                except (TypeError, ValueError):
                    continue
                rows.append({"date": date_value, "cumulative": cumulative})
            rows.sort(key=lambda item: item["date"], reverse=True)
            source = {
                "source_id": source_id,
                "provider_id": "sina_etf_distribution",
                "label": "新浪 ETF 累计分红",
                "data_as_of": quote_as_of,
                "retrieved_at": retrieved_at,
                "url": url,
                "latest_distribution_date": (
                    rows[0]["date"].isoformat() if rows else None
                ),
            }
        except Exception:
            warning = "ETF 分红数据源暂不可用，本次快照未计算分配收益率。"

        trailing_distribution: float | None = None
        if rows:
            try:
                reference = datetime.fromisoformat(quote_as_of.replace("Z", "+00:00")).date()
            except ValueError:
                reference = datetime.now(timezone.utc).date()
            cutoff = reference - timedelta(days=365)
            trailing_distribution = 0.0
            for index, item in enumerate(rows):
                older_cumulative = (
                    rows[index + 1]["cumulative"] if index + 1 < len(rows) else 0.0
                )
                distribution = max(0.0, item["cumulative"] - older_cumulative)
                if cutoff < item["date"] <= reference:
                    trailing_distribution += distribution
            trailing_distribution = round(trailing_distribution, 12)
        distribution_yield = (
            trailing_distribution / current_price
            if trailing_distribution is not None and current_price not in (None, 0)
            else None
        )
        metrics = [
            _metric(
                "distribution_yield_ttm",
                "近 12 个月分配收益率",
                distribution_yield,
                "ratio",
                "dividend",
                source_id=source_id,
                data_as_of=quote_as_of,
                raw_field="cumulative_dividend_u",
                semantics="trailing_365d_cash_distribution_per_unit_divided_by_current_price",
                unavailable_reason=reason,
            ),
            _metric(
                "distribution_per_unit_ttm",
                "近 12 个月每份分配",
                trailing_distribution,
                "CNY_per_fund_unit",
                "dividend",
                source_id=source_id,
                data_as_of=quote_as_of,
                raw_field="cumulative_dividend_u",
                semantics="trailing_365d_cash_distribution_per_fund_unit",
                unavailable_reason=reason,
            ),
        ]
        return metrics, source, warning

    def _stock_dividend_ttm(
        self,
        symbol: str,
        *,
        current_price: float | None,
        quote_as_of: str,
        retrieved_at: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
        """Aggregate every implemented cash dividend in the trailing 365 days.

        Eastmoney reports cash dividend per ten pre-action shares.  When an
        event also sends or transfers shares, or a later event does so, older
        cash amounts are divided by the cumulative share-expansion factor so
        every amount is expressed on the current-share basis before summing.
        """

        code = symbol.split(".", 1)[0]
        dividend_params = {
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "pageSize": "50",
            "pageNumber": "1",
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "ALL",
            "quoteColumns": "",
            "source": "WEB",
            "client": "WEB",
            "filter": f'(SECURITY_CODE="{code}")',
        }
        source_id = f"eastmoney:sharebonus_ttm:{symbol}:{quote_as_of[:10]}"
        reason = "未发现可核验的已实施现金分红记录"
        try:
            payload = self.get_json(
                _DIVIDEND_URL,
                params=dividend_params,
                timeout=15,
                urllib_fallback=True,
            )
            raw_rows = ((payload or {}).get("result") or {}).get("data") or []
        except Exception:
            metrics = self._stock_dividend_metrics(
                source_id=source_id,
                quote_as_of=quote_as_of,
                dividend_per_share=None,
                dividend_yield=None,
                unavailable_reason="股票分红数据源暂不可用",
            )
            return metrics, None, "股票分红数据源暂不可用，本次快照未计算近 12 个月股息率。"

        try:
            reference = datetime.fromisoformat(quote_as_of.replace("Z", "+00:00")).date()
        except ValueError:
            reference = datetime.now(timezone.utc).date()
        cutoff = reference - timedelta(days=365)
        implemented: list[dict[str, Any]] = []
        for raw in raw_rows:
            if not isinstance(raw, dict) or str(raw.get("ASSIGN_PROGRESS") or "") != "实施分配":
                continue
            try:
                ex_date = datetime.strptime(
                    str(raw.get("EX_DIVIDEND_DATE") or "")[:10], "%Y-%m-%d"
                ).date()
            except ValueError:
                continue
            if ex_date > reference:
                continue
            cash_per_10 = _number(raw.get("PRETAX_BONUS_RMB"))
            transfer_per_10 = _number(raw.get("BONUS_IT_RATIO"))
            if transfer_per_10 is None:
                transfer_per_10 = (
                    (_number(raw.get("BONUS_RATIO")) or 0.0)
                    + (_number(raw.get("IT_RATIO")) or 0.0)
                )
            implemented.append({
                "ex_date": ex_date,
                "cash_per_10": cash_per_10,
                "transfer_per_10": max(0.0, transfer_per_10 or 0.0),
                "plan": str(raw.get("IMPL_PLAN_PROFILE") or ""),
            })

        # A duplicated disclosure row must not apply the same share expansion
        # twice.  For one ex-date, keep the largest reported transfer ratio.
        transfer_by_date: dict[Any, float] = {}
        for item in implemented:
            transfer_by_date[item["ex_date"]] = max(
                transfer_by_date.get(item["ex_date"], 0.0),
                float(item["transfer_per_10"]),
            )

        distributions: list[dict[str, Any]] = []
        seen_distributions: set[tuple[Any, float, str]] = set()
        total_current_basis = 0.0
        for item in implemented:
            cash_per_10 = item["cash_per_10"]
            ex_date = item["ex_date"]
            if cash_per_10 is None or not cutoff < ex_date <= reference:
                continue
            distribution_key = (ex_date, round(float(cash_per_10), 12), str(item["plan"]))
            if distribution_key in seen_distributions:
                continue
            seen_distributions.add(distribution_key)
            adjustment_factor = 1.0
            for action_date, transfer_per_10 in transfer_by_date.items():
                if ex_date <= action_date <= reference:
                    adjustment_factor *= 1.0 + transfer_per_10 / 10.0
            current_basis = (cash_per_10 / 10.0) / adjustment_factor
            total_current_basis += current_basis
            distributions.append({
                "ex_dividend_date": ex_date.isoformat(),
                "cash_per_10_pre_action_shares": cash_per_10,
                "share_adjustment_factor": adjustment_factor,
                "cash_per_current_share": current_basis,
                "plan": item["plan"],
            })

        has_history = bool(implemented)
        dividend_per_share = round(total_current_basis, 12) if has_history else None
        dividend_yield = (
            dividend_per_share / current_price
            if dividend_per_share is not None and current_price not in (None, 0)
            else None
        )
        metrics = self._stock_dividend_metrics(
            source_id=source_id,
            quote_as_of=quote_as_of,
            dividend_per_share=dividend_per_share,
            dividend_yield=dividend_yield,
            unavailable_reason=reason,
        )
        source = {
            "source_id": source_id,
            "provider_id": "eastmoney_sharebonus_ttm",
            "label": "东方财富分红送配（近 12 个月汇总）",
            "data_as_of": quote_as_of,
            "retrieved_at": retrieved_at,
            "url": f"{_DIVIDEND_URL}?{urlencode(dividend_params)}",
            "window_start_exclusive": cutoff.isoformat(),
            "window_end_inclusive": reference.isoformat(),
            "distribution_count": len(distributions),
            "distributions": distributions,
            "formula": "sum((cash_per_10/10)/cumulative_share_expansion)/current_price",
        } if has_history else None
        return metrics, source, None

    @staticmethod
    def _stock_dividend_metrics(
        *,
        source_id: str,
        quote_as_of: str,
        dividend_per_share: float | None,
        dividend_yield: float | None,
        unavailable_reason: str,
    ) -> list[dict[str, Any]]:
        return [
            _metric(
                "dividend_yield_ttm",
                "近 12 个月股息率",
                dividend_yield,
                "ratio",
                "dividend",
                source_id=source_id,
                data_as_of=quote_as_of,
                raw_field="PRETAX_BONUS_RMB+BONUS_IT_RATIO",
                semantics="trailing_365d_adjusted_cash_dividend_per_current_share_divided_by_current_price",
                unavailable_reason=unavailable_reason,
            ),
            _metric(
                "dividend_per_share_ttm",
                "近 12 个月每股现金分红",
                dividend_per_share,
                "CNY_per_share",
                "dividend",
                source_id=source_id,
                data_as_of=quote_as_of,
                raw_field="PRETAX_BONUS_RMB+BONUS_IT_RATIO",
                semantics="trailing_365d_cash_dividend_on_current_share_basis",
                unavailable_reason=unavailable_reason,
            ),
        ]


class InstrumentProfileStore:
    """Append-only profile snapshots stored beside the report catalog."""

    def __init__(self, knowledge_store: ResearchKnowledgeStore) -> None:
        self.knowledge = knowledge_store
        self.initialize()

    def initialize(self) -> None:
        with self.knowledge.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS instrument_profile_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    instrument_type TEXT NOT NULL,
                    data_as_of TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    quality_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_instrument_profile_symbol
                    ON instrument_profile_snapshots(symbol, data_as_of DESC, retrieved_at DESC);
                """
            )

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = dict(payload)
        snapshot_id = str(value.get("snapshot_id") or _stable_id(
            "instrumentsnap",
            value.get("symbol"),
            value.get("data_as_of"),
            value.get("identity"),
            value.get("metrics"),
        ))
        value["snapshot_id"] = snapshot_id
        with self.knowledge.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO instrument_profile_snapshots(
                   snapshot_id,symbol,instrument_type,data_as_of,retrieved_at,
                   quality_status,payload_json,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    str(value.get("symbol") or "").upper(),
                    str(value.get("instrument_type") or "company_equity"),
                    str(value.get("data_as_of") or ""),
                    str(value.get("retrieved_at") or _utc_now()),
                    str(value.get("quality_status") or "partial"),
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    _utc_now(),
                ),
            )
        return value

    def latest(self, symbol: str) -> dict[str, Any] | None:
        with self.knowledge.connect() as conn:
            row = conn.execute(
                """SELECT payload_json FROM instrument_profile_snapshots
                   WHERE symbol=? ORDER BY data_as_of DESC, retrieved_at DESC LIMIT 1""",
                (str(symbol or "").strip().upper(),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"] or "{}"))
        return payload if isinstance(payload, dict) else None

    def history_count(self, symbol: str) -> int:
        with self.knowledge.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM instrument_profile_snapshots WHERE symbol=?",
                (str(symbol or "").strip().upper(),),
            ).fetchone()
        return int(row["count"] if row else 0)


class InstrumentProfileService:
    """Refresh and retrieve the report layer's canonical instrument profile."""

    def __init__(
        self,
        knowledge_store: ResearchKnowledgeStore,
        *,
        provider: Any | None = None,
        store: InstrumentProfileStore | None = None,
    ) -> None:
        self.store = store or InstrumentProfileStore(knowledge_store)
        self.provider = provider or EastmoneyInstrumentProfileProvider()

    def latest_snapshot(self, symbol: str) -> dict[str, Any] | None:
        snapshot = self.store.latest(symbol)
        if snapshot is not None:
            snapshot["history_count"] = self.store.history_count(symbol)
        return snapshot

    def refresh(self, symbol: str) -> dict[str, Any]:
        snapshot = self.store.save(self.provider.fetch(symbol))
        snapshot["history_count"] = self.store.history_count(symbol)
        return snapshot


def get_instrument_profile_service() -> InstrumentProfileService:
    from src.research.knowledge import get_research_knowledge_store

    return InstrumentProfileService(get_research_knowledge_store())
