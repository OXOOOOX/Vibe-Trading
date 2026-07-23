"""Deterministic ETF universe collection, cache reuse, and P4A integration.

The daily path is intentionally model-free.  Structured index-company files
are preferred, Tushare index weights are the first fallback, and quarterly
fund holdings are accepted only as an explicitly top-ranked partial universe.
PCF quantities are not converted into index weights in this module because
doing so would mix two different data semantics.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable, Iterable, Literal, Sequence

import requests

from .contracts import ETFComponentSelection, ETFResearchSnapshot, utc_now
from .etf_penetration import execute_p4a_selection
from .etf_research import ETFResearchStore, build_etf_snapshot, get_etf_research_store


ETFUniverseQuality = Literal["complete", "partial", "insufficient"]
WeightScale = Literal["auto", "fraction", "percent"]
_QUALIFIED_CN_ETF = re.compile(r"^\d{6}\.(?:SH|SZ)$", re.I)
_SECRET_LIKE = re.compile(r"[A-Za-z0-9_-]{32,}")
_CSI_CLOSE_WEIGHT_TEMPLATE = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
    "file/autofile/closeweight/{code}closeweight.xls"
)
_SSE_ETF_CATALOG_URL = "https://query.sse.com.cn/commonSoaQuery.do"
_SSE_ETF_CATALOG_PAGE = "https://www.sse.com.cn/assortment/fund/etf/list/"
_SZSE_ETF_CATALOG_URL = "https://www.szse.cn/api/report/ShowReport/data"
_SZSE_ETF_CATALOG_PAGE = "https://www.szse.cn/www/market/product/list/etfList/"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _parse_time(value: str | date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        raw = str(value).strip()
        if re.fullmatch(r"\d{8}", raw):
            parsed = datetime.strptime(raw, "%Y%m%d")
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        else:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_day(value: str | date | datetime) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError(f"invalid date: {value}")
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _compact_day(value: str | date | datetime) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError(f"invalid date: {value}")
    return parsed.strftime("%Y%m%d")


def _sanitize_error(exc: BaseException) -> str:
    return _SECRET_LIKE.sub("[REDACTED]", str(exc)).strip()[:500]


def normalize_etf_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    if _QUALIFIED_CN_ETF.fullmatch(symbol):
        return symbol
    if not re.fullmatch(r"\d{6}", symbol):
        return symbol
    return f"{symbol}.SH" if symbol.startswith("5") else f"{symbol}.SZ"


def _plain_cell(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _qualify_index_code(value: Any) -> str:
    code = re.sub(r"[^0-9A-Z]", "", str(value or "").upper().split(".", 1)[0])
    if not code:
        return ""
    if code.startswith("399"):
        return f"{code}.SZ"
    if code.startswith("0") and code.isdigit():
        return f"{code}.SH"
    return f"{code}.CSI"


def _normalize_component_symbol(value: Any, exchange: Any = None) -> str:
    raw = str(value or "").strip().upper().replace(" ", "")
    raw = re.sub(r"\.0$", "", raw)
    if not raw:
        return ""
    suffix_match = re.fullmatch(r"(\d+)\.(SH|SZ|BJ|HK)", raw)
    if suffix_match:
        code, suffix = suffix_match.groups()
        width = 5 if suffix == "HK" else 6
        return f"{code.zfill(width)}.{suffix}"
    if not raw.isdigit():
        return raw
    exchange_text = str(exchange or "").upper()
    if "香港" in exchange_text or exchange_text in {"HK", "SEHK"}:
        return f"{raw.zfill(5)}.HK"
    if "深圳" in exchange_text or exchange_text == "SZ":
        return f"{raw.zfill(6)}.SZ"
    if "上海" in exchange_text or exchange_text == "SH":
        return f"{raw.zfill(6)}.SH"
    if len(raw) <= 5:
        return f"{raw.zfill(5)}.HK"
    return f"{raw.zfill(6)}.SH" if raw.startswith("6") else f"{raw.zfill(6)}.SZ"


def _raw_weight(value: Any) -> tuple[float | None, bool]:
    if value is None or isinstance(value, bool):
        return None, False
    text = str(value).strip().replace(",", "")
    if not text:
        return None, False
    explicit_percent = text.endswith("%")
    if explicit_percent:
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None, explicit_percent
    return number, explicit_percent


@dataclass(frozen=True, slots=True)
class ETFUniverseFreshnessPolicy:
    cadence: Literal["daily", "monthly", "quarterly"]
    max_age_days: int

    def expires_at(self, data_as_of: str) -> str:
        observed = _parse_time(data_as_of)
        if observed is None:
            raise ValueError("freshness policy requires a valid data_as_of")
        return (observed + timedelta(days=self.max_age_days)).isoformat()


@dataclass(frozen=True, slots=True)
class ETFIndexMapping:
    etf_symbol: str
    etf_name: str
    tracked_index_code: str
    tracked_index_name: str
    csi_download_code: str
    mapping_source: str
    source_url: str
    effective_from: str
    valid_until: str
    data_as_of: str
    confidence: Literal["structured", "verified_override"]
    expected_component_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_AUDITED_MAPPINGS: tuple[ETFIndexMapping, ...] = (
    ETFIndexMapping(
        "588870.SH", "汇添富上证科创板50成份ETF", "000688.SH", "上证科创板50成份指数",
        "000688", "fund_manager_product_document",
        "https://www.99fund.com/announcement/zx/upload/2025/20251230/7f5c393de48540da9a3873b7fce3b5cb.pdf",
        "2025-12-31", "2027-12-31", "2026-07-18", "verified_override", 50,
    ),
    ETFIndexMapping(
        "510300.SH", "华泰柏瑞沪深300ETF", "000300.SH", "沪深300指数", "000300",
        "exchange_fund_disclosure",
        "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2023-07-21/510300_20230721_0R3C.pdf",
        "2023-07-20", "2027-12-31", "2026-07-18", "verified_override", 300,
    ),
    ETFIndexMapping(
        "560010.SH", "广发中证1000ETF", "000852.SH", "中证1000指数", "000852",
        "fund_manager_product_page", "https://www.gffunds.com.cn/funds/?fundcode=560010",
        "2026-06-18", "2027-12-31", "2026-07-18", "verified_override", 1000,
    ),
    ETFIndexMapping(
        "513120.SH", "广发中证香港创新药ETF(QDII)", "931787.CSI", "中证香港创新药指数",
        "931787", "fund_manager_product_document",
        "https://www.gffunds.com.cn/jjgg/flwj/202406/P020240628362700079556.pdf",
        "2024-06-28", "2027-12-31", "2026-07-18", "verified_override", 50,
    ),
    ETFIndexMapping(
        "516010.SH", "国泰中证动漫游戏ETF", "930901.CSI", "中证动漫游戏指数", "930901",
        "fund_manager_product_page", "https://etradetest.gtfund.com/etrade/Jijin/view/id/516010",
        "2021-02-25", "2027-12-31", "2026-07-18", "verified_override", 30,
    ),
    ETFIndexMapping(
        "159842.SZ", "银华中证全指证券公司ETF", "399975.SZ", "中证全指证券公司指数",
        "399975", "fund_manager_product_page",
        "https://www.yhfund.com.cn/en/investment/quantitative/index.shtml",
        "2026-06-30", "2027-12-31", "2026-07-18", "verified_override", 50,
    ),
    ETFIndexMapping(
        "512890.SH", "华泰柏瑞中证红利低波动ETF", "H30269.CSI", "中证红利低波动指数",
        "H30269", "exchange_fund_disclosure",
        "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-01-22/512890_20260122_F1IT.pdf",
        "2026-01-22", "2027-12-31", "2026-07-18", "verified_override", 50,
    ),
    ETFIndexMapping(
        "512680.SH", "广发中证军工ETF", "399967.SZ", "中证军工指数", "399967",
        "exchange_fund_disclosure",
        "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2023-06-28/512680_20230628_NLDX.pdf",
        "2023-06-28", "2027-12-31", "2026-07-18", "verified_override", 80,
    ),
)


class ETFUniverseProviderError(RuntimeError):
    """A safe, typed provider failure suitable for audit/API responses."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ETFUniverseUnavailableError(RuntimeError):
    def __init__(self, symbol: str, attempts: Sequence[dict[str, Any]]) -> None:
        super().__init__(f"no qualified ETF universe is available for {symbol}")
        self.symbol = symbol
        self.attempts = [dict(item) for item in attempts]


class AuditedETFIndexMapper:
    """Small, sourced, expiring override table for verified ETF-index links."""

    def __init__(self, mappings: Iterable[ETFIndexMapping] = _AUDITED_MAPPINGS) -> None:
        self._mappings = {item.etf_symbol.upper(): item for item in mappings}

    def supports(self, etf_symbol: str) -> bool:
        return normalize_etf_symbol(etf_symbol) in self._mappings

    def resolve(self, etf_symbol: str, as_of: str | None = None) -> ETFIndexMapping:
        symbol = normalize_etf_symbol(etf_symbol)
        mapping = self._mappings.get(symbol)
        if mapping is None:
            raise ETFUniverseProviderError("mapping_not_found", f"ETF-index mapping not found: {symbol}")
        requested = _parse_time(as_of or utc_now())
        effective = _parse_time(mapping.effective_from)
        expires = _parse_time(mapping.valid_until)
        if requested is None or effective is None or expires is None:
            raise ETFUniverseProviderError("invalid_mapping_window", f"invalid mapping dates: {symbol}")
        if not effective.date() <= requested.date() <= expires.date():
            raise ETFUniverseProviderError("mapping_outside_validity", f"ETF-index mapping expired: {symbol}")
        return mapping

    def list_mappings(self) -> list[dict[str, Any]]:
        return [self._mappings[key].to_dict() for key in sorted(self._mappings)]


def _default_tushare_client() -> Any:
    token = str(os.getenv("TUSHARE_TOKEN") or "").strip()
    if not token or token == "your-tushare-token":
        raise ETFUniverseProviderError("token_missing", "TUSHARE_TOKEN is not configured")
    try:
        import tushare as ts

        return ts.pro_api(token)
    except ETFUniverseProviderError:
        raise
    except Exception as exc:
        raise ETFUniverseProviderError(
            "client_unavailable", f"Tushare client initialization failed: {_sanitize_error(exc)}"
        ) from exc


def _records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if hasattr(value, "to_dict"):
        try:
            return [dict(item) for item in value.to_dict("records")]
        except (TypeError, ValueError):
            pass
    return []


def _classify_tushare_error(exc: BaseException, operation: str) -> ETFUniverseProviderError:
    message = _sanitize_error(exc)
    lowered = message.lower()
    if any(marker in lowered for marker in ("权限", "积分", "permission", "access denied")):
        code = "permission_denied"
    elif any(marker in lowered for marker in ("token", "登录", "认证", "auth")):
        code = "authentication_failed"
    else:
        code = "provider_error"
    return ETFUniverseProviderError(code, f"Tushare {operation} failed: {message}", retryable=False)


class OfficialExchangeETFIndexMapper:
    """Resolve any listed SH/SZ ETF from the exchanges' official product catalogs."""

    def __init__(
        self,
        *,
        http_get: Callable[..., Any] | None = None,
        now: Callable[[], str] = utc_now,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.http_get = http_get or requests.get
        self.now = now
        self.timeout_seconds = timeout_seconds

    def supports(self, etf_symbol: str) -> bool:
        return bool(_QUALIFIED_CN_ETF.fullmatch(normalize_etf_symbol(etf_symbol)))

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        referer: str,
    ) -> Any:
        try:
            response = self.http_get(
                url,
                params=params,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                    ),
                    "Referer": referer,
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise ETFUniverseProviderError(
                "official_catalog_network_error",
                f"official ETF catalog request failed: {_sanitize_error(exc)}",
                retryable=True,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ETFUniverseProviderError(
                "official_catalog_invalid_response",
                f"official ETF catalog returned invalid JSON: {_sanitize_error(exc)}",
            ) from exc

    def _sse_row(self, fund_code: str) -> tuple[dict[str, Any] | None, str]:
        payload = self._get_json(
            _SSE_ETF_CATALOG_URL,
            params={
                "isPagination": "true",
                "pageHelp.pageSize": "10000",
                "pageHelp.pageNo": "1",
                "pageHelp.beginPage": "1",
                "pageHelp.cacheSize": "1",
                "pageHelp.endPage": "1",
                "pagecache": "false",
                "sqlId": "FUND_LIST",
                "fundType": "00",
            },
            referer=_SSE_ETF_CATALOG_PAGE,
        )
        rows = payload.get("result") if isinstance(payload, dict) else []
        row = next(
            (
                dict(item)
                for item in rows or []
                if isinstance(item, dict)
                and str(item.get("fundCode") or "").strip() == fund_code
            ),
            None,
        )
        return row, _SSE_ETF_CATALOG_PAGE

    def _szse_row(self, fund_code: str) -> tuple[dict[str, Any] | None, str]:
        payload = self._get_json(
            _SZSE_ETF_CATALOG_URL,
            params={
                "SHOWTYPE": "JSON",
                "CATALOGID": "1945",
                "TABKEY": "tab1",
                "txtQueryKeyAndJC": fund_code,
                "PAGENO": "1",
                "tab1PAGESIZE": "20",
            },
            referer=_SZSE_ETF_CATALOG_PAGE,
        )
        blocks = payload if isinstance(payload, list) else []
        rows: list[dict[str, Any]] = []
        data_as_of = ""
        for block in blocks:
            if not isinstance(block, dict):
                continue
            metadata = dict(block.get("metadata") or {})
            if str(metadata.get("tabkey") or "") != "tab1":
                continue
            data_as_of = str(metadata.get("subname") or "")
            rows.extend(dict(item) for item in block.get("data") or [] if isinstance(item, dict))
        row = next(
            (
                item
                for item in rows
                if re.search(rf"(?<!\d){re.escape(fund_code)}(?!\d)", _plain_cell(item.get("sys_key")))
            ),
            None,
        )
        if row is not None:
            row = {**row, "_catalog_data_as_of": data_as_of}
        return row, _SZSE_ETF_CATALOG_PAGE

    def resolve(self, etf_symbol: str, as_of: str | None = None) -> ETFIndexMapping:
        symbol = normalize_etf_symbol(etf_symbol)
        if not self.supports(symbol):
            raise ETFUniverseProviderError(
                "mapping_not_found", f"official ETF catalog cannot resolve: {symbol}"
            )
        fund_code = symbol[:6]
        row, source_url = (
            self._sse_row(fund_code) if symbol.endswith(".SH") else self._szse_row(fund_code)
        )
        if row is None:
            raise ETFUniverseProviderError(
                "mapping_not_found", f"official ETF catalog returned no row: {symbol}"
            )

        if symbol.endswith(".SH"):
            raw_index = str(row.get("INDEX_CODE") or "").strip()
            index_name = str(row.get("INDEX_NAME") or "").strip()
            etf_name = str(row.get("secNameFull") or row.get("fundAbbr") or symbol).strip()
            effective_raw = str(row.get("listingDate") or "")
            data_as_of_raw = str(row.get("updateDate") or "")
            mapping_source = "sse_official_etf_catalog"
        else:
            index_cell = _plain_cell(row.get("nhzs"))
            index_match = re.match(r"([0-9A-Z]{6,})\s*(.*)", index_cell, re.I)
            raw_index = index_match.group(1) if index_match else ""
            index_name = index_match.group(2).strip() if index_match else ""
            etf_name = _plain_cell(row.get("kzjcurl")) or symbol
            effective_raw = ""
            data_as_of_raw = str(row.get("_catalog_data_as_of") or "")
            mapping_source = "szse_official_etf_catalog"

        index_code = _qualify_index_code(raw_index)
        if not index_code:
            raise ETFUniverseProviderError(
                "mapping_not_found", f"official ETF catalog has no tracked index code: {symbol}"
            )
        retrieved = _parse_time(self.now()) or datetime.now(timezone.utc)
        effective = _parse_time(effective_raw)
        observed = _parse_time(data_as_of_raw) or retrieved
        return ETFIndexMapping(
            etf_symbol=symbol,
            etf_name=etf_name,
            tracked_index_code=index_code,
            tracked_index_name=index_name or raw_index,
            csi_download_code=raw_index,
            mapping_source=mapping_source,
            source_url=source_url,
            effective_from=(effective or datetime(1990, 1, 1, tzinfo=timezone.utc)).date().isoformat(),
            valid_until=(retrieved + timedelta(days=370)).date().isoformat(),
            data_as_of=observed.date().isoformat(),
            confidence="structured",
            expected_component_count=None,
        )


class CompositeETFIndexMapper:
    """Use overrides, then official exchange catalogs, then optional Tushare."""

    def __init__(
        self,
        *,
        audited: AuditedETFIndexMapper | None = None,
        official: OfficialExchangeETFIndexMapper | None = None,
        client_factory: Callable[[], Any] = _default_tushare_client,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.audited = audited or AuditedETFIndexMapper()
        self.official = official or OfficialExchangeETFIndexMapper(now=now)
        self.client_factory = client_factory
        self.now = now
        self._cache: dict[str, ETFIndexMapping] = {}
        self._lock = threading.Lock()

    def supports(self, etf_symbol: str) -> bool:
        symbol = normalize_etf_symbol(etf_symbol)
        return self.audited.supports(symbol) or self.official.supports(symbol) or (
            bool(_QUALIFIED_CN_ETF.fullmatch(symbol))
            and bool(str(os.getenv("TUSHARE_TOKEN") or "").strip())
        )

    def resolve(self, etf_symbol: str, as_of: str | None = None) -> ETFIndexMapping:
        symbol = normalize_etf_symbol(etf_symbol)
        if self.audited.supports(symbol):
            return self.audited.resolve(symbol, as_of=as_of)
        with self._lock:
            cached = self._cache.get(symbol)
        if cached is not None:
            return cached
        official_error: ETFUniverseProviderError | None = None
        if self.official.supports(symbol):
            try:
                mapping = self.official.resolve(symbol, as_of=as_of)
            except ETFUniverseProviderError as exc:
                official_error = exc
            else:
                with self._lock:
                    self._cache[symbol] = mapping
                return mapping
        if not str(os.getenv("TUSHARE_TOKEN") or "").strip():
            if official_error is not None:
                raise official_error
            raise ETFUniverseProviderError("mapping_not_found", f"ETF-index mapping not found: {symbol}")
        try:
            frame = self.client_factory().etf_basic(
                ts_code=symbol,
                fields=(
                    "ts_code,csname,extname,cname,index_code,index_name,"
                    "setup_date,list_date,list_status"
                ),
            )
        except ETFUniverseProviderError:
            raise
        except Exception as exc:
            raise _classify_tushare_error(exc, "etf_basic") from exc
        rows = _records(frame)
        if not rows:
            raise ETFUniverseProviderError("mapping_not_found", f"Tushare etf_basic returned no row: {symbol}")
        row = rows[0]
        index_code = str(row.get("index_code") or "").strip().upper()
        if not index_code:
            raise ETFUniverseProviderError("mapping_not_found", f"ETF has no tracked index code: {symbol}")
        retrieved = _parse_time(self.now()) or datetime.now(timezone.utc)
        effective_raw = str(row.get("list_date") or row.get("setup_date") or retrieved.date())
        mapping = ETFIndexMapping(
            etf_symbol=symbol,
            etf_name=str(row.get("extname") or row.get("csname") or row.get("cname") or symbol),
            tracked_index_code=index_code,
            tracked_index_name=str(row.get("index_name") or index_code),
            csi_download_code=index_code.split(".", 1)[0],
            mapping_source="tushare_etf_basic",
            source_url="https://tushare.pro/document/2?doc_id=385",
            effective_from=_parse_time(effective_raw).date().isoformat()
            if _parse_time(effective_raw)
            else retrieved.date().isoformat(),
            valid_until=(retrieved + timedelta(days=365)).date().isoformat(),
            data_as_of=retrieved.date().isoformat(),
            confidence="structured",
            expected_component_count=None,
        )
        with self._lock:
            self._cache[symbol] = mapping
        return mapping

    def list_mappings(self) -> list[dict[str, Any]]:
        return self.audited.list_mappings()


@dataclass(frozen=True, slots=True)
class ETFUniverseFetchResult:
    etf_symbol: str
    etf_name: str
    tracked_index_code: str
    tracked_index_name: str
    provider_id: str
    source_type: str
    source_ids: list[str]
    source_urls: list[str]
    data_as_of: str
    retrieved_at: str
    components: list[dict[str, Any]]
    expected_component_count: int
    observed_component_count: int
    observed_weight_coverage: float
    required_field_coverage: float
    universe_complete: bool
    partial_components_are_top_ranked: bool
    quality: ETFUniverseQuality
    warnings: list[str]
    raw_content_hash: str
    mapping: dict[str, Any] = field(default_factory=dict)
    weight_scale: Literal["fraction"] = "fraction"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_snapshot_payload(self) -> dict[str, Any]:
        """Return stable semantic content; retrieval time stays on the snapshot row."""

        return {
            "schema_version": 2,
            "etf_symbol": self.etf_symbol,
            "etf_name": self.etf_name,
            "tracked_index_code": self.tracked_index_code,
            "tracked_index_name": self.tracked_index_name,
            "provider_id": self.provider_id,
            "source_type": self.source_type,
            "source_ids": list(self.source_ids),
            "source_urls": list(self.source_urls),
            "components": [dict(item) for item in self.components],
            "expected_component_count": self.expected_component_count,
            "observed_component_count": self.observed_component_count,
            "observed_weight_coverage": self.observed_weight_coverage,
            "required_field_coverage": self.required_field_coverage,
            "universe_complete": self.universe_complete,
            "partial_components_are_top_ranked": self.partial_components_are_top_ranked,
            "quality": self.quality,
            "warnings": list(self.warnings),
            "raw_content_hash": self.raw_content_hash,
            "mapping": dict(self.mapping),
            "weight_scale": self.weight_scale,
        }


def make_universe_fetch_result(
    *,
    etf_symbol: str,
    etf_name: str,
    tracked_index_code: str,
    tracked_index_name: str,
    provider_id: str,
    source_type: str,
    source_ids: Iterable[str],
    source_urls: Iterable[str],
    data_as_of: str,
    components: Iterable[dict[str, Any]],
    expected_component_count: int | None = None,
    universe_complete: bool = True,
    partial_components_are_top_ranked: bool = False,
    weight_scale: WeightScale = "auto",
    warnings: Iterable[str] = (),
    raw_content: bytes | str | None = None,
    mapping: dict[str, Any] | None = None,
    retrieved_at: str | None = None,
) -> ETFUniverseFetchResult:
    """Normalize weights, merge duplicates, and assign strict source quality."""

    rows = [dict(item) for item in components]
    parsed: list[tuple[dict[str, Any], float, bool]] = []
    invalid_rows = 0
    for row in rows:
        symbol = _normalize_component_symbol(
            row.get("symbol") or row.get("code") or row.get("con_code"),
            row.get("exchange"),
        )
        weight, explicit_percent = _raw_weight(row.get("weight"))
        if not symbol or weight is None or weight <= 0:
            invalid_rows += 1
            continue
        parsed.append((row, weight, explicit_percent))

    inferred_percent = False
    non_explicit = [weight for _row, weight, explicit in parsed if not explicit]
    if weight_scale == "percent":
        inferred_percent = True
    elif weight_scale == "auto" and non_explicit:
        inferred_percent = any(value > 1.0 for value in non_explicit) or sum(non_explicit) > 1.5

    merged: dict[str, dict[str, Any]] = {}
    normalized_warnings = [str(item) for item in warnings if str(item)]
    for row, raw_weight, explicit_percent in parsed:
        symbol = _normalize_component_symbol(
            row.get("symbol") or row.get("code") or row.get("con_code"),
            row.get("exchange"),
        )
        divisor = 100.0 if explicit_percent or inferred_percent else 1.0
        normalized = {
            "symbol": symbol,
            "name": str(row.get("name") or row.get("con_name") or symbol).strip(),
            "weight": round(raw_weight / divisor, 10),
            "metadata": dict(row.get("metadata") or {}),
        }
        for key in (
            "price_contribution", "earnings_contribution", "major_event",
            "evidence_conflict", "research_stale",
        ):
            if key in row:
                normalized[key] = row[key]
        if symbol not in merged:
            merged[symbol] = normalized
            continue
        normalized_warnings.append("duplicate_component_symbols_merged")
        merged[symbol]["weight"] = round(
            float(merged[symbol]["weight"]) + float(normalized["weight"]), 10
        )

    normalized_components = sorted(
        merged.values(), key=lambda item: (-float(item["weight"]), str(item["symbol"]))
    )
    observed_count = len(normalized_components)
    expected_count = max(int(expected_component_count or observed_count), observed_count)
    raw_weight_sum = sum(float(item["weight"]) for item in normalized_components)
    observed_coverage = round(min(1.0, max(0.0, raw_weight_sum)), 8)
    required_coverage = round(len(parsed) / len(rows), 8) if rows else 0.0
    if invalid_rows:
        normalized_warnings.append("component_without_valid_symbol_or_weight_skipped")
    if raw_weight_sum > 1.05:
        normalized_warnings.append("component_weight_sum_above_105pct")
    if universe_complete and raw_weight_sum < 0.90:
        normalized_warnings.append("complete_universe_weight_sum_below_90pct")
    if expected_component_count is not None and observed_count < int(expected_component_count):
        normalized_warnings.append("observed_component_count_below_expected")

    complete = (
        bool(normalized_components)
        and universe_complete
        and observed_count >= expected_count
        and 0.90 <= raw_weight_sum <= 1.05
        and required_coverage >= 0.95
    )
    if complete:
        quality: ETFUniverseQuality = "complete"
    elif normalized_components and partial_components_are_top_ranked and raw_weight_sum <= 1.05:
        quality = "partial"
        normalized_warnings.append("top_ranked_partial_component_universe")
    else:
        quality = "insufficient"
        if normalized_components and not partial_components_are_top_ranked:
            normalized_warnings.append("partial_components_not_confirmed_top_ranked")
        if not normalized_components:
            normalized_warnings.append("component_universe_missing")

    semantic_raw = raw_content
    if semantic_raw is None:
        semantic_raw = _canonical_json({
            "components": normalized_components,
            "data_as_of": data_as_of,
            "provider_id": provider_id,
            "source_type": source_type,
        })
    encoded = semantic_raw if isinstance(semantic_raw, bytes) else semantic_raw.encode("utf-8")
    return ETFUniverseFetchResult(
        etf_symbol=normalize_etf_symbol(etf_symbol),
        etf_name=str(etf_name),
        tracked_index_code=str(tracked_index_code).upper(),
        tracked_index_name=str(tracked_index_name),
        provider_id=provider_id,
        source_type=source_type,
        source_ids=sorted({str(item) for item in source_ids if str(item)}),
        source_urls=sorted({str(item) for item in source_urls if str(item)}),
        data_as_of=_iso_day(data_as_of),
        retrieved_at=retrieved_at or utc_now(),
        components=normalized_components,
        expected_component_count=expected_count,
        observed_component_count=observed_count,
        observed_weight_coverage=observed_coverage,
        required_field_coverage=required_coverage,
        universe_complete=complete,
        partial_components_are_top_ranked=bool(partial_components_are_top_ranked),
        quality=quality,
        warnings=list(dict.fromkeys(normalized_warnings)),
        raw_content_hash=hashlib.sha256(encoded).hexdigest(),
        mapping=dict(mapping or {}),
    )


class ETFUniverseProvider(ABC):
    provider_id: str
    source_priority: int
    freshness_policy: ETFUniverseFreshnessPolicy

    @abstractmethod
    def supports(self, etf_symbol: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def fetch(self, etf_symbol: str, as_of: str | None = None) -> ETFUniverseFetchResult:
        raise NotImplementedError


def _read_csi_close_weight_xls(content: bytes) -> list[dict[str, Any]]:
    import pandas as pd

    frame = pd.read_excel(io.BytesIO(content), dtype=str, keep_default_na=False)
    if frame.empty:
        return []

    def column(*needles: str) -> str:
        for candidate in frame.columns:
            compact = str(candidate).replace(" ", "").lower()
            if all(needle.lower() in compact for needle in needles):
                return str(candidate)
        raise ETFUniverseProviderError(
            "unexpected_source_schema", f"CSI weight file is missing column: {needles}"
        )

    date_column = column("date")
    index_code_column = column("indexcode")
    index_name_column = column("indexname")
    component_code_column = column("constituentcode")
    component_name_column = column("constituentname")
    weight_column = column("weight")
    exchange_column = next(
        (
            str(candidate)
            for candidate in frame.columns
            if "交易所exchange" in str(candidate).replace(" ", "").lower()
        ),
        "",
    )
    records: list[dict[str, Any]] = []
    for item in frame.to_dict("records"):
        records.append({
            "data_as_of": item.get(date_column),
            "index_code": item.get(index_code_column),
            "index_name": item.get(index_name_column),
            "symbol": item.get(component_code_column),
            "name": item.get(component_name_column),
            "exchange": item.get(exchange_column) if exchange_column else None,
            "weight": item.get(weight_column),
        })
    return records


class CSIIndexWeightProvider(ETFUniverseProvider):
    provider_id = "csi_official_close_weight"
    source_priority = 10
    freshness_policy = ETFUniverseFreshnessPolicy("monthly", 45)

    def __init__(
        self,
        *,
        mapper: Any | None = None,
        http_get: Callable[..., Any] | None = None,
        parser: Callable[[bytes], list[dict[str, Any]]] = _read_csi_close_weight_xls,
        timeout_seconds: float = 20.0,
        max_transport_attempts: int = 2,
    ) -> None:
        self.mapper = mapper or CompositeETFIndexMapper()
        self.http_get = http_get or requests.get
        self.parser = parser
        self.timeout_seconds = timeout_seconds
        self.max_transport_attempts = max(1, int(max_transport_attempts))

    def supports(self, etf_symbol: str) -> bool:
        return self.mapper.supports(etf_symbol)

    def fetch(self, etf_symbol: str, as_of: str | None = None) -> ETFUniverseFetchResult:
        mapping = self.mapper.resolve(etf_symbol, as_of=as_of)
        source_url = _CSI_CLOSE_WEIGHT_TEMPLATE.format(code=mapping.csi_download_code)
        transport_attempts = 0
        content = b""
        for transport_attempts in range(1, self.max_transport_attempts + 1):
            try:
                response = self.http_get(source_url, timeout=self.timeout_seconds)
                response.raise_for_status()
                content = bytes(response.content)
                break
            except requests.RequestException as exc:
                if transport_attempts < self.max_transport_attempts:
                    continue
                raise ETFUniverseProviderError(
                    "network_error",
                    f"CSI official weight download failed: {_sanitize_error(exc)}",
                    retryable=True,
                ) from exc
            except Exception as exc:
                raise ETFUniverseProviderError(
                    "network_error",
                    f"CSI official weight download failed: {_sanitize_error(exc)}",
                    retryable=True,
                ) from exc
        if not content or content.lstrip().startswith((b"<html", b"<!DOCTYPE")):
            raise ETFUniverseProviderError("invalid_source_content", "CSI weight file was empty or HTML")
        records = self.parser(content)
        if not records:
            raise ETFUniverseProviderError("empty_result", "CSI official weight file returned no rows")
        data_as_of = max(str(item.get("data_as_of") or "") for item in records)
        requested = _parse_time(as_of)
        observed = _parse_time(data_as_of)
        if requested is not None and observed is not None and observed.date() > requested.date():
            raise ETFUniverseProviderError(
                "historical_as_of_unavailable",
                "CSI latest-file endpoint is newer than the requested as_of",
            )
        components = [
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "exchange": item.get("exchange"),
                "weight": item.get("weight"),
                "metadata": {"index_weight_as_of": _compact_day(data_as_of)},
            }
            for item in records
        ]
        index_name = str(records[0].get("index_name") or mapping.tracked_index_name)
        source_id = f"csi:{mapping.csi_download_code}:{_compact_day(data_as_of)}:closeweight"
        return make_universe_fetch_result(
            etf_symbol=mapping.etf_symbol,
            etf_name=mapping.etf_name,
            tracked_index_code=mapping.tracked_index_code,
            tracked_index_name=index_name,
            provider_id=self.provider_id,
            source_type="official_index_weight",
            source_ids=[source_id, f"mapping:{mapping.etf_symbol}:{mapping.data_as_of}"],
            source_urls=[source_url, mapping.source_url],
            data_as_of=data_as_of,
            components=components,
            expected_component_count=len(records),
            universe_complete=True,
            partial_components_are_top_ranked=False,
            weight_scale="percent",
            warnings=(
                ["official_source_transport_retry_succeeded"]
                if transport_attempts > 1
                else []
            ),
            raw_content=content,
            mapping=mapping.to_dict(),
        )


class TushareIndexWeightProvider(ETFUniverseProvider):
    provider_id = "tushare_index_weight"
    source_priority = 20
    freshness_policy = ETFUniverseFreshnessPolicy("monthly", 45)

    def __init__(
        self,
        *,
        mapper: Any | None = None,
        client_factory: Callable[[], Any] = _default_tushare_client,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.mapper = mapper or CompositeETFIndexMapper(client_factory=client_factory, now=now)
        self.client_factory = client_factory
        self.now = now

    def supports(self, etf_symbol: str) -> bool:
        return self.mapper.supports(etf_symbol)

    def fetch(self, etf_symbol: str, as_of: str | None = None) -> ETFUniverseFetchResult:
        mapping = self.mapper.resolve(etf_symbol, as_of=as_of)
        requested = _parse_time(as_of or self.now())
        if requested is None:
            raise ETFUniverseProviderError("invalid_as_of", "Tushare index_weight requires a valid as_of")
        start = requested - timedelta(days=124)
        try:
            frame = self.client_factory().index_weight(
                index_code=mapping.tracked_index_code,
                start_date=start.strftime("%Y%m%d"),
                end_date=requested.strftime("%Y%m%d"),
            )
        except ETFUniverseProviderError:
            raise
        except Exception as exc:
            raise _classify_tushare_error(exc, "index_weight") from exc
        rows = _records(frame)
        usable = [row for row in rows if str(row.get("trade_date") or "") <= requested.strftime("%Y%m%d")]
        if not usable:
            raise ETFUniverseProviderError("empty_result", "Tushare index_weight returned no usable rows")
        latest_day = max(str(row.get("trade_date") or "") for row in usable)
        latest = [row for row in usable if str(row.get("trade_date") or "") == latest_day]
        components = [
            {
                "symbol": row.get("con_code"),
                "name": row.get("con_name") or row.get("con_code"),
                "weight": row.get("weight"),
                "metadata": {"index_weight_as_of": latest_day},
            }
            for row in latest
        ]
        source_url = "https://tushare.pro/document/2?doc_id=96"
        return make_universe_fetch_result(
            etf_symbol=mapping.etf_symbol,
            etf_name=mapping.etf_name,
            tracked_index_code=mapping.tracked_index_code,
            tracked_index_name=mapping.tracked_index_name,
            provider_id=self.provider_id,
            source_type="index_weight",
            source_ids=[
                f"tushare:index_weight:{mapping.tracked_index_code}:{latest_day}",
                f"mapping:{mapping.etf_symbol}:{mapping.data_as_of}",
            ],
            source_urls=[source_url, mapping.source_url],
            data_as_of=latest_day,
            components=components,
            expected_component_count=len(latest),
            universe_complete=True,
            partial_components_are_top_ranked=False,
            weight_scale="percent",
            warnings=["tushare_index_weight_is_monthly"],
            raw_content=_canonical_json(latest),
            mapping=mapping.to_dict(),
        )


class TushareFundPortfolioProvider(ETFUniverseProvider):
    provider_id = "tushare_fund_portfolio"
    source_priority = 40
    freshness_policy = ETFUniverseFreshnessPolicy("quarterly", 150)

    def __init__(
        self,
        *,
        mapper: Any | None = None,
        client_factory: Callable[[], Any] = _default_tushare_client,
        now: Callable[[], str] = utc_now,
        top_n: int = 10,
    ) -> None:
        self.mapper = mapper or CompositeETFIndexMapper(client_factory=client_factory, now=now)
        self.client_factory = client_factory
        self.now = now
        self.top_n = max(1, int(top_n))

    def supports(self, etf_symbol: str) -> bool:
        return self.mapper.supports(etf_symbol)

    def fetch(self, etf_symbol: str, as_of: str | None = None) -> ETFUniverseFetchResult:
        mapping = self.mapper.resolve(etf_symbol, as_of=as_of)
        requested = _parse_time(as_of or self.now())
        if requested is None:
            raise ETFUniverseProviderError("invalid_as_of", "fund_portfolio requires a valid as_of")
        try:
            frame = self.client_factory().fund_portfolio(ts_code=mapping.etf_symbol)
        except ETFUniverseProviderError:
            raise
        except Exception as exc:
            raise _classify_tushare_error(exc, "fund_portfolio") from exc
        rows = _records(frame)
        requested_day = requested.strftime("%Y%m%d")
        usable = [
            row for row in rows
            if str(row.get("end_date") or "")
            and str(row.get("end_date") or "") <= requested_day
        ]
        if not usable:
            raise ETFUniverseProviderError("empty_result", "Tushare fund_portfolio returned no usable rows")
        latest_period = max(str(row.get("end_date") or "") for row in usable)
        period_rows = [row for row in usable if str(row.get("end_date") or "") == latest_period]
        latest_by_symbol: dict[str, dict[str, Any]] = {}
        for row in sorted(period_rows, key=lambda item: str(item.get("ann_date") or "")):
            symbol = _normalize_component_symbol(row.get("symbol"))
            if symbol:
                latest_by_symbol[symbol] = row
        ranked = sorted(
            latest_by_symbol.values(),
            key=lambda item: float(item.get("stk_mkv_ratio") or 0.0),
            reverse=True,
        )[: self.top_n]
        if not ranked:
            raise ETFUniverseProviderError("empty_result", "fund_portfolio rows had no valid holdings")
        announcement_day = max(str(row.get("ann_date") or "") for row in ranked)
        components = [
            {
                "symbol": row.get("symbol"),
                "name": row.get("name") or row.get("symbol"),
                "weight": row.get("stk_mkv_ratio"),
                "metadata": {
                    "report_period": latest_period,
                    "announcement_date": row.get("ann_date"),
                    "weight_semantics": "share_of_disclosed_stock_market_value_percent",
                },
            }
            for row in ranked
        ]
        return make_universe_fetch_result(
            etf_symbol=mapping.etf_symbol,
            etf_name=mapping.etf_name,
            tracked_index_code=mapping.tracked_index_code,
            tracked_index_name=mapping.tracked_index_name,
            provider_id=self.provider_id,
            source_type="quarterly_fund_holdings",
            source_ids=[
                f"tushare:fund_portfolio:{mapping.etf_symbol}:{latest_period}:{announcement_day}",
                f"mapping:{mapping.etf_symbol}:{mapping.data_as_of}",
            ],
            source_urls=["https://tushare.pro/document/2?doc_id=121", mapping.source_url],
            data_as_of=latest_period,
            components=components,
            expected_component_count=mapping.expected_component_count or len(ranked),
            universe_complete=False,
            partial_components_are_top_ranked=True,
            weight_scale="percent",
            warnings=[
                "quarterly_holdings_are_not_complete_index_components",
                "quarterly_holding_weights_are_not_official_index_weights",
                f"announcement_date:{announcement_day}",
            ],
            raw_content=_canonical_json(ranked),
            mapping=mapping.to_dict(),
        )


@dataclass(frozen=True, slots=True)
class ETFUniverseServiceResult:
    etf_symbol: str
    snapshot: ETFResearchSnapshot
    selection: ETFComponentSelection
    cache_hit: bool
    snapshot_reused: bool
    p4a_cache_hit: bool
    network_fetched: bool
    provider_id: str
    source_type: str
    fallback_used: bool
    cache_fallback: bool
    attempts: list[dict[str, Any]]
    warnings: list[str]
    coalesced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "etf_symbol": self.etf_symbol,
            "snapshot": self.snapshot.to_dict(),
            "selection": self.selection.to_dict(),
            "cache_hit": self.cache_hit,
            "snapshot_reused": self.snapshot_reused,
            "p4a_cache_hit": self.p4a_cache_hit,
            "network_fetched": self.network_fetched,
            "provider_id": self.provider_id,
            "source_type": self.source_type,
            "fallback_used": self.fallback_used,
            "cache_fallback": self.cache_fallback,
            "attempts": [dict(item) for item in self.attempts],
            "warnings": list(self.warnings),
            "coalesced": self.coalesced,
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }


class ETFUniverseService:
    """Cache-first provider coordination with per-symbol single-flight refresh."""

    def __init__(
        self,
        *,
        store: ETFResearchStore | None = None,
        providers: Iterable[ETFUniverseProvider] | None = None,
        mapper: Any | None = None,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.store = store or get_etf_research_store()
        self.now = now
        self.mapper = mapper or CompositeETFIndexMapper(now=now)
        default_providers: list[ETFUniverseProvider] = [
            CSIIndexWeightProvider(mapper=self.mapper),
            TushareIndexWeightProvider(mapper=self.mapper, now=now),
            TushareFundPortfolioProvider(mapper=self.mapper, now=now),
        ]
        self.providers = sorted(
            list(providers) if providers is not None else default_providers,
            key=lambda item: item.source_priority,
        )
        self._flight_guard = threading.Lock()
        self._flights: dict[str, Future[ETFUniverseServiceResult]] = {}

    def get_or_refresh(
        self,
        etf_symbol: str,
        force_refresh: bool = False,
        as_of: str | None = None,
        event_symbols: Iterable[str] = (),
    ) -> ETFUniverseServiceResult:
        symbol = normalize_etf_symbol(etf_symbol)
        if not _QUALIFIED_CN_ETF.fullmatch(symbol):
            raise ValueError("ETF universe requires a market-qualified SH/SZ symbol")
        with self._flight_guard:
            future = self._flights.get(symbol)
            if future is None:
                future = Future()
                self._flights[symbol] = future
                leader = True
            else:
                leader = False
        if not leader:
            return replace(future.result(), coalesced=True)

        try:
            result = self._get_or_refresh_once(
                symbol,
                force_refresh=force_refresh,
                as_of=as_of,
                event_symbols=event_symbols,
            )
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(result)
            return result
        finally:
            with self._flight_guard:
                if self._flights.get(symbol) is future:
                    self._flights.pop(symbol, None)

    def _cached_result(
        self,
        snapshot: ETFResearchSnapshot,
        *,
        attempts: Sequence[dict[str, Any]] = (),
        cache_fallback: bool = False,
        event_symbols: Iterable[str] = (),
    ) -> ETFUniverseServiceResult:
        selection, p4a_hit = execute_p4a_selection(
            store=self.store,
            universe_snapshot=snapshot,
            event_symbols=event_symbols,
        )
        payload = dict(snapshot.payload)
        warnings = list(payload.get("warnings") or [])
        if cache_fallback:
            warnings.append("provider_failure_used_valid_cache")
        return ETFUniverseServiceResult(
            etf_symbol=snapshot.symbol,
            snapshot=snapshot,
            selection=selection,
            cache_hit=True,
            snapshot_reused=True,
            p4a_cache_hit=p4a_hit,
            network_fetched=False,
            provider_id=str(payload.get("provider_id") or "unknown"),
            source_type=str(payload.get("source_type") or "unknown"),
            fallback_used=bool(attempts),
            cache_fallback=cache_fallback,
            attempts=[dict(item) for item in attempts],
            warnings=list(dict.fromkeys(warnings)),
        )

    def _get_or_refresh_once(
        self,
        symbol: str,
        *,
        force_refresh: bool,
        as_of: str | None,
        event_symbols: Iterable[str],
    ) -> ETFUniverseServiceResult:
        latest = self.store.latest_snapshot(symbol, "universe")
        if latest is not None and not force_refresh:
            from .etf_research import snapshot_is_reusable

            if snapshot_is_reusable(latest, now=self.now()):
                self.store.record_universe_audit(
                    symbol=symbol,
                    operation="universe_cache_hit",
                    object_id=latest.snapshot_id,
                    cache_hit=True,
                    metadata={"provider_id": latest.payload.get("provider_id")},
                )
                return self._cached_result(latest, event_symbols=event_symbols)

        attempts: list[dict[str, Any]] = []
        supported = [provider for provider in self.providers if provider.supports(symbol)]
        for position, provider in enumerate(supported):
            started = time.perf_counter()
            try:
                fetched = provider.fetch(symbol, as_of=as_of)
                latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
                if fetched.quality == "insufficient":
                    attempt = {
                        "provider_id": provider.provider_id,
                        "status": "insufficient",
                        "warnings": list(fetched.warnings),
                        "latency_ms": latency_ms,
                    }
                    attempts.append(attempt)
                    self.store.record_universe_audit(
                        symbol=symbol,
                        operation="universe_provider_insufficient",
                        metadata=attempt,
                    )
                    continue
            except ETFUniverseProviderError as exc:
                attempt = {
                    "provider_id": provider.provider_id,
                    "status": "failed",
                    "error_code": exc.code,
                    "error": _sanitize_error(exc),
                    "retryable": exc.retryable,
                    "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                }
                attempts.append(attempt)
                self.store.record_universe_audit(
                    symbol=symbol,
                    operation="universe_provider_failure",
                    metadata=attempt,
                )
                continue
            except Exception as exc:
                attempt = {
                    "provider_id": provider.provider_id,
                    "status": "failed",
                    "error_code": "unexpected_provider_error",
                    "error": _sanitize_error(exc),
                    "retryable": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                }
                attempts.append(attempt)
                self.store.record_universe_audit(
                    symbol=symbol,
                    operation="universe_provider_failure",
                    metadata=attempt,
                )
                continue

            fallback_used = position > 0 or bool(attempts)
            fetch_warnings = list(fetched.warnings)
            if fallback_used:
                fetch_warnings.append("provider_fallback_used")
                fetched = replace(fetched, warnings=list(dict.fromkeys(fetch_warnings)))
            snapshot = build_etf_snapshot(
                symbol=symbol,
                snapshot_type="universe",
                data_as_of=fetched.data_as_of,
                payload=fetched.to_snapshot_payload(),
                coverage_ratio=fetched.required_field_coverage,
                source_ids=fetched.source_ids,
                retrieved_at=fetched.retrieved_at,
                freshness_expires_at=provider.freshness_policy.expires_at(fetched.data_as_of),
            )
            stored, snapshot_reused = self.store.save_snapshot(snapshot)
            selection, p4a_hit = execute_p4a_selection(
                store=self.store,
                universe_snapshot=stored,
                event_symbols=event_symbols,
            )
            success_attempt = {
                "provider_id": provider.provider_id,
                "status": "success",
                "quality": fetched.quality,
                "latency_ms": latency_ms,
                "fallback": fallback_used,
            }
            attempts.append(success_attempt)
            self.store.record_universe_audit(
                symbol=symbol,
                operation="universe_provider_success",
                object_id=stored.snapshot_id,
                metadata={
                    **success_attempt,
                    "source_type": fetched.source_type,
                    "data_as_of": fetched.data_as_of,
                    "observed_component_count": fetched.observed_component_count,
                    "observed_weight_coverage": fetched.observed_weight_coverage,
                    "snapshot_reused": snapshot_reused,
                },
            )
            return ETFUniverseServiceResult(
                etf_symbol=symbol,
                snapshot=stored,
                selection=selection,
                cache_hit=False,
                snapshot_reused=snapshot_reused,
                p4a_cache_hit=p4a_hit,
                network_fetched=True,
                provider_id=fetched.provider_id,
                source_type=fetched.source_type,
                fallback_used=fallback_used,
                cache_fallback=False,
                attempts=attempts,
                warnings=list(fetched.warnings),
            )

        if latest is not None:
            from .etf_research import snapshot_is_reusable

            if snapshot_is_reusable(latest, now=self.now()):
                self.store.record_universe_audit(
                    symbol=symbol,
                    operation="universe_cache_fallback",
                    object_id=latest.snapshot_id,
                    cache_hit=True,
                    metadata={"attempts": attempts},
                )
                return self._cached_result(
                    latest,
                    attempts=attempts,
                    cache_fallback=True,
                    event_symbols=event_symbols,
                )
        self.store.record_universe_audit(
            symbol=symbol,
            operation="universe_unavailable",
            metadata={"attempts": attempts, "supported_provider_count": len(supported)},
        )
        raise ETFUniverseUnavailableError(symbol, attempts)

    def latest_snapshot(self, etf_symbol: str) -> ETFResearchSnapshot | None:
        return self.store.latest_snapshot(normalize_etf_symbol(etf_symbol), "universe")

    def status(self, etf_symbol: str) -> dict[str, Any]:
        symbol = normalize_etf_symbol(etf_symbol)
        latest = self.latest_snapshot(symbol)
        module = self.store.latest_module_result(symbol, "holding_penetration")
        mapping = None
        mapping_error = None
        audited = getattr(self.mapper, "audited", self.mapper)
        if getattr(audited, "supports", lambda _symbol: False)(symbol):
            try:
                mapping = audited.resolve(symbol).to_dict()
            except ETFUniverseProviderError as exc:
                mapping_error = {"code": exc.code, "message": _sanitize_error(exc)}
        if latest is not None:
            from .etf_research import snapshot_is_reusable

            reusable = snapshot_is_reusable(latest, now=self.now())
        else:
            reusable = False
        return {
            "etf_symbol": symbol,
            "supported": any(provider.supports(symbol) for provider in self.providers),
            "mapping": mapping,
            "mapping_error": mapping_error,
            "latest_snapshot": latest.to_dict() if latest else None,
            "snapshot_reusable": reusable,
            "holding_penetration": module.to_dict() if module else None,
            "providers": [
                {
                    "provider_id": provider.provider_id,
                    "source_priority": provider.source_priority,
                    "freshness_policy": asdict(provider.freshness_policy),
                    "supports": provider.supports(symbol),
                }
                for provider in self.providers
            ],
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    def prewarm_current_holdings(
        self,
        *,
        force_refresh: bool = False,
        holdings: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if holdings is None:
            from src.portfolio.state import load_state

            holdings = load_state().holdings
        results: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        seen: set[str] = set()
        for holding in holdings:
            symbol = normalize_etf_symbol(str(holding.get("symbol") or holding.get("code") or ""))
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            name = str(holding.get("name") or "")
            explicitly_etf = (
                "ETF" in name.upper()
                or str(holding.get("asset_type") or holding.get("security_type") or "").lower() == "etf"
                or getattr(getattr(self.mapper, "audited", self.mapper), "supports", lambda _s: False)(symbol)
            )
            if not explicitly_etf:
                skipped.append({"symbol": symbol, "reason": "holding_not_identified_as_etf"})
                continue
            try:
                result = self.get_or_refresh(symbol, force_refresh=force_refresh)
            except (ETFUniverseUnavailableError, ValueError) as exc:
                skipped.append({"symbol": symbol, "reason": _sanitize_error(exc)})
                continue
            results.append(result.to_dict())
        return {
            "results": results,
            "skipped": skipped,
            "requested_count": len(seen),
            "warmed_count": len(results),
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }


_shared_universe_service: ETFUniverseService | None = None
_shared_universe_lock = threading.Lock()


def get_etf_universe_service() -> ETFUniverseService:
    global _shared_universe_service
    with _shared_universe_lock:
        if _shared_universe_service is None:
            _shared_universe_service = ETFUniverseService()
        return _shared_universe_service
