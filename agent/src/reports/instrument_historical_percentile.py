"""Durable historical percentiles for every report-library instrument.

The contract is intentionally broader than company valuation.  ETFs and
indexes use index valuation, mainland company equities use point-in-time
BaoStock valuation fields, and markets without a trustworthy valuation
history fall back to an explicitly labelled adjusted-price percentile.

Reading a dossier is offline: network access only happens during an explicit
refresh and every result, including an unavailable result, is append-only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from src.reports.etf_valuation_percentile import BaifenweiIndexValuationProvider
from src.research.knowledge import ResearchKnowledgeStore


_LOOKBACK_YEARS = 10
_MIN_OBSERVATIONS = 120
_VALUATION_FIELDS = (
    ("pe_ttm", "peTTM", "PE · 滚动市盈率"),
    ("pb_mrq", "pbMRQ", "PB · 市净率"),
    ("ps_ttm", "psTTM", "PS · 滚动市销率"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, value: Any) -> str:
    raw = json.dumps(
        value,
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
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _clean_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _symbol_code(value: str) -> str:
    return _clean_symbol(value).split(".", 1)[0]


def _is_mainland_company_equity(symbol: str) -> bool:
    clean = _clean_symbol(symbol)
    code, _, market = clean.partition(".")
    return code.isdigit() and market in {"SH", "SZ", "BJ"}


def _temperature(percentile: float | None, *, valuation: bool) -> str:
    if percentile is None:
        return "暂无"
    if valuation:
        if percentile < 10:
            return "极冷"
        if percentile < 30:
            return "偏冷"
        if percentile < 70:
            return "正常"
        if percentile < 90:
            return "偏热"
        return "极热"
    if percentile < 10:
        return "极低"
    if percentile < 30:
        return "偏低"
    if percentile < 70:
        return "中位"
    if percentile < 90:
        return "偏高"
    return "极高"


def _percentile(values: Iterable[float], current: float) -> float:
    observations = list(values)
    if not observations:
        return 0.0
    return sum(value < current for value in observations) / len(observations) * 100


def _data_timestamp(day: str) -> str:
    value = str(day or "")[:10]
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return _utc_now()
    return f"{value}T00:00:00+08:00"


def _baostock_code(symbol: str) -> str:
    clean = _clean_symbol(symbol)
    code, _, market = clean.partition(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market)
    if not prefix or not code.isdigit():
        raise ValueError(f"BaoStock does not support symbol {clean}")
    return f"{prefix}.{code}"


def _get_baostock_history(symbol: str) -> list[dict[str, Any]]:
    import baostock as bs

    login = bs.login()
    if str(login.error_code) != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    try:
        end = date.today()
        start = end - timedelta(days=3653)
        result = bs.query_history_k_data_plus(
            _baostock_code(symbol),
            "date,code,peTTM,pbMRQ,psTTM,pcfNcfTTM",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            frequency="d",
            adjustflag="3",
        )
        if str(result.error_code) != "0":
            raise RuntimeError(f"BaoStock query failed: {result.error_msg}")
        rows: list[dict[str, Any]] = []
        while result.next():
            rows.append(dict(zip(result.fields, result.get_row_data(), strict=False)))
        return rows
    finally:
        bs.logout()


def _yahoo_symbol(symbol: str) -> str:
    clean = _clean_symbol(symbol)
    return clean[:-3] if clean.endswith(".US") else clean


def _get_yahoo_price_history(symbol: str) -> list[tuple[str, float]]:
    import yfinance as yf

    frame = yf.Ticker(_yahoo_symbol(symbol)).history(
        period="10y",
        auto_adjust=True,
        actions=False,
    )
    rows: list[tuple[str, float]] = []
    for timestamp, value in frame.get("Close", []).items():
        parsed = _number(value)
        if parsed is not None:
            rows.append((str(timestamp)[:10], parsed))
    return rows


def _source(
    *,
    source_id: str,
    provider_id: str,
    label: str,
    publisher: str,
    url: str,
    methodology_url: str,
    retrieved_at: str,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "provider_id": provider_id,
        "label": label,
        "publisher": publisher,
        "verification_status": "public_secondary",
        "url": url,
        "methodology_url": methodology_url,
        "retrieved_at": retrieved_at,
    }


class BaoStockEquityValuationProvider:
    """Compute like-for-like daily PE/PB/PS percentiles for mainland stocks."""

    def __init__(
        self,
        *,
        get_history_fn: Callable[[str], list[dict[str, Any]]] = _get_baostock_history,
        now_provider: Callable[[], str] = _utc_now,
        minimum_observations: int = _MIN_OBSERVATIONS,
    ) -> None:
        self.get_history = get_history_fn
        self.now_provider = now_provider
        self.minimum_observations = max(1, int(minimum_observations))

    def fetch(
        self,
        symbol: str,
        *,
        instrument_name: str = "",
        currency: str = "CNY",
    ) -> dict[str, Any]:
        clean = _clean_symbol(symbol)
        retrieved_at = self.now_provider()
        try:
            rows = sorted(
                (
                    row
                    for row in self.get_history(clean)
                    if isinstance(row, dict) and str(row.get("date") or "")
                ),
                key=lambda row: str(row.get("date") or ""),
            )
        except Exception as exc:
            return self._unavailable(
                clean,
                instrument_name=instrument_name,
                currency=currency,
                retrieved_at=retrieved_at,
                reason=f"公司历史估值数据获取失败：{str(exc)[:180]}",
            )
        if not rows:
            return self._unavailable(
                clean,
                instrument_name=instrument_name,
                currency=currency,
                retrieved_at=retrieved_at,
                reason="公司历史估值数据源当前没有返回有效交易日。",
            )

        latest = rows[-1]
        data_date = str(latest.get("date") or "")[:10]
        metrics: list[dict[str, Any]] = []
        warnings: list[str] = []
        for key, raw_field, label in _VALUATION_FIELDS:
            current = _number(latest.get(raw_field))
            observations = [
                (str(row.get("date") or "")[:10], parsed)
                for row in rows
                if (parsed := _number(row.get(raw_field))) is not None and parsed > 0
            ]
            if current is None or current <= 0:
                warnings.append(f"{label} 当前值无效或不适用，本次不计算其历史分位。")
                continue
            if len(observations) < self.minimum_observations:
                warnings.append(
                    f"{label} 只有 {len(observations)} 个有效交易日，少于最低 "
                    f"{self.minimum_observations} 个样本，本次不展示分位。"
                )
                continue
            percentile = _percentile((value for _, value in observations), current)
            metrics.append({
                "key": key,
                "label": label,
                "value": current,
                "unit": "multiple",
                "percentile": percentile,
                "temperature": _temperature(percentile, valuation=True),
                "observation_count": len(observations),
                "sample_start": observations[0][0],
                "sample_end": observations[-1][0],
                "definition": raw_field,
            })

        source_id = _stable_id("baostockvaluation", {
            "symbol": clean,
            "data_date": data_date,
            "metrics": metrics,
        })
        payload = {
            "schema_version": 2,
            "symbol": clean,
            "instrument_type": "company_equity",
            "instrument_name": instrument_name or clean,
            "valuation_basis": "company_valuation",
            "scope_label": f"{instrument_name or clean} · 公司估值",
            "status": "available" if metrics else "unavailable",
            "lookback_years": _LOOKBACK_YEARS,
            "data_as_of": _data_timestamp(data_date),
            "retrieved_at": retrieved_at,
            "mapping_method": "symbol_exact_baostock_daily_valuation",
            "percentile_method": "strict_lower_empirical_cdf",
            "metrics": metrics,
            "source": _source(
                source_id=source_id,
                provider_id="baostock_daily_valuation",
                label="BaoStock · 历史估值",
                publisher="BaoStock",
                url="https://www.baostock.com/",
                methodology_url="https://www.baostock.com/mainContent?file=pythonAPI.md",
                retrieved_at=retrieved_at,
            ),
            "unavailable_reason": None if metrics else "当前没有达到样本门槛的有效公司估值指标。",
            "warnings": [
                *warnings,
                "PE 只使用正值样本；亏损期的负 PE 不参与排序，PB/PS 也只使用正值样本。",
                "分位只描述同一标的、同一指标定义下的历史相对位置，不代表未来方向。",
            ],
            "currency": currency or "CNY",
        }
        return _with_sample_summary(payload)

    def _unavailable(
        self,
        symbol: str,
        *,
        instrument_name: str,
        currency: str,
        retrieved_at: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "symbol": symbol,
            "instrument_type": "company_equity",
            "instrument_name": instrument_name or symbol,
            "valuation_basis": "company_valuation",
            "scope_label": f"{instrument_name or symbol} · 公司估值",
            "status": "unavailable",
            "lookback_years": _LOOKBACK_YEARS,
            "data_as_of": retrieved_at,
            "retrieved_at": retrieved_at,
            "mapping_method": "symbol_exact_baostock_daily_valuation",
            "percentile_method": "strict_lower_empirical_cdf",
            "metrics": [],
            "source": _source(
                source_id=_stable_id("baostockvaluation", {"symbol": symbol, "reason": reason}),
                provider_id="baostock_daily_valuation",
                label="BaoStock · 历史估值",
                publisher="BaoStock",
                url="https://www.baostock.com/",
                methodology_url="https://www.baostock.com/mainContent?file=pythonAPI.md",
                retrieved_at=retrieved_at,
            ),
            "unavailable_reason": reason,
            "warnings": ["数据缺失时不会用价格或当前估值反推历史估值。"],
            "currency": currency or "CNY",
            "sample_start": None,
            "sample_end": None,
            "sample_count": 0,
        }


class YahooPricePercentileProvider:
    """Use adjusted close only when comparable valuation history is unavailable."""

    def __init__(
        self,
        *,
        get_history_fn: Callable[[str], list[tuple[str, float]]] = _get_yahoo_price_history,
        now_provider: Callable[[], str] = _utc_now,
        minimum_observations: int = _MIN_OBSERVATIONS,
    ) -> None:
        self.get_history = get_history_fn
        self.now_provider = now_provider
        self.minimum_observations = max(1, int(minimum_observations))

    def fetch(
        self,
        symbol: str,
        *,
        instrument_type: str = "company_equity",
        instrument_name: str = "",
        currency: str = "",
    ) -> dict[str, Any]:
        clean = _clean_symbol(symbol)
        retrieved_at = self.now_provider()
        try:
            observations = sorted(
                (
                    (str(day)[:10], parsed)
                    for day, value in self.get_history(clean)
                    if (parsed := _number(value)) is not None and parsed > 0
                ),
                key=lambda item: item[0],
            )
        except Exception as exc:
            observations = []
            failure = f"价格历史数据获取失败：{str(exc)[:180]}"
        else:
            failure = ""
        enough = len(observations) >= self.minimum_observations
        current = observations[-1][1] if observations else None
        percentile = (
            _percentile((value for _, value in observations), current)
            if enough and current is not None
            else None
        )
        data_date = observations[-1][0] if observations else retrieved_at[:10]
        metric = ({
            "key": "adjusted_close",
            "label": "复权收盘价",
            "value": current,
            "unit": currency or "price",
            "percentile": percentile,
            "temperature": _temperature(percentile, valuation=False),
            "observation_count": len(observations),
            "sample_start": observations[0][0],
            "sample_end": observations[-1][0],
            "definition": "split_and_dividend_adjusted_close",
        } if enough and current is not None else None)
        ticker = _yahoo_symbol(clean)
        source_id = _stable_id("yahoopricepercentile", {
            "symbol": clean,
            "data_date": data_date,
            "metric": metric,
        })
        reason = failure or (
            f"有效价格样本只有 {len(observations)} 个交易日，少于最低 "
            f"{self.minimum_observations} 个样本。"
        )
        payload = {
            "schema_version": 2,
            "symbol": clean,
            "instrument_type": instrument_type or "company_equity",
            "instrument_name": instrument_name or clean,
            "valuation_basis": "adjusted_price_history",
            "scope_label": f"{instrument_name or clean} · 价格位置（非估值）",
            "status": "available" if metric else "unavailable",
            "lookback_years": _LOOKBACK_YEARS,
            "data_as_of": _data_timestamp(data_date),
            "retrieved_at": retrieved_at,
            "mapping_method": "symbol_exact_yahoo_adjusted_close",
            "percentile_method": "strict_lower_empirical_cdf",
            "metrics": [metric] if metric else [],
            "source": _source(
                source_id=source_id,
                provider_id="yahoo_adjusted_price_history",
                label="Yahoo Finance · 复权价格历史",
                publisher="Yahoo Finance",
                url=f"https://finance.yahoo.com/quote/{ticker}/history/",
                methodology_url="https://help.yahoo.com/kb/SLN28256.html",
                retrieved_at=retrieved_at,
            ),
            "unavailable_reason": None if metric else reason,
            "warnings": [
                "当前市场缺少可核验、同口径的长期估值序列，因此展示复权价格分位，不把它冒充 PE/PB/PS 估值分位。",
                "价格分位只描述历史价格位置，不代表高估、低估、未来方向或买卖信号。",
            ],
            "currency": currency or None,
        }
        return _with_sample_summary(payload)


def _with_sample_summary(payload: dict[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    metrics = [item for item in value.get("metrics") or [] if isinstance(item, dict)]
    starts = [str(item.get("sample_start")) for item in metrics if item.get("sample_start")]
    ends = [str(item.get("sample_end")) for item in metrics if item.get("sample_end")]
    counts = [int(item.get("observation_count") or 0) for item in metrics]
    value.setdefault("sample_start", min(starts) if starts else None)
    value.setdefault("sample_end", max(ends) if ends else None)
    value.setdefault("sample_count", max(counts) if counts else None)
    return value


def _normalize_index_snapshot(
    payload: dict[str, Any],
    *,
    instrument_type: str,
    instrument_name: str = "",
) -> dict[str, Any]:
    value = dict(payload)
    source = dict(value.get("source") or {})
    provider_id = str(source.get("provider_id") or "")
    tracked_name = str(value.get("tracked_index_name") or instrument_name or value.get("symbol") or "")
    normalized_metrics: list[dict[str, Any]] = []
    for item in value.get("metrics") or []:
        if not isinstance(item, dict):
            continue
        metric = dict(item)
        metric.setdefault("unit", "multiple")
        metric.setdefault("definition", metric.get("key"))
        normalized_metrics.append(metric)
    value.update({
        "schema_version": 2,
        "instrument_type": instrument_type,
        "instrument_name": instrument_name or tracked_name,
        "valuation_basis": (
            "tracked_index_valuation" if instrument_type == "etf" else "index_valuation"
        ),
        "scope_label": (
            f"{tracked_name} · 跟踪指数估值"
            if instrument_type == "etf"
            else f"{tracked_name} · 指数估值"
        ),
        "percentile_method": (
            "source_reported_ten_year_percentile"
            if provider_id == "baifenwei_index_valuation"
            else "strict_lower_empirical_cdf"
        ),
        "metrics": normalized_metrics,
    })
    return _with_sample_summary(value)


class InstrumentHistoricalPercentileStore:
    """Append-only generic snapshots, with one-time legacy ETF migration."""

    def __init__(self, knowledge_store: ResearchKnowledgeStore) -> None:
        self.knowledge = knowledge_store
        self.initialize()

    def initialize(self) -> None:
        with self.knowledge.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS instrument_historical_percentile_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    instrument_type TEXT NOT NULL,
                    valuation_basis TEXT NOT NULL,
                    data_as_of TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_instrument_historical_percentile_symbol
                    ON instrument_historical_percentile_snapshots(
                        symbol, data_as_of DESC, retrieved_at DESC
                    );
                """
            )
            legacy_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='etf_valuation_percentile_snapshots'"
            ).fetchone()
            if legacy_exists is None:
                return
            rows = conn.execute(
                "SELECT payload_json,created_at FROM etf_valuation_percentile_snapshots"
            ).fetchall()
            for row in rows:
                try:
                    legacy = json.loads(str(row["payload_json"] or "{}"))
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(legacy, dict):
                    continue
                value = _normalize_index_snapshot(legacy, instrument_type="etf")
                snapshot_id = str(value.get("snapshot_id") or _stable_id(
                    "historicalpct",
                    {
                        "symbol": value.get("symbol"),
                        "data_as_of": value.get("data_as_of"),
                        "metrics": value.get("metrics"),
                    },
                ))
                value["snapshot_id"] = snapshot_id
                self._insert(conn, value, created_at=str(row["created_at"] or _utc_now()))

    @staticmethod
    def _insert(conn: Any, value: dict[str, Any], *, created_at: str) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO instrument_historical_percentile_snapshots(
               snapshot_id,symbol,instrument_type,valuation_basis,data_as_of,
               retrieved_at,status,payload_json,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                str(value.get("snapshot_id") or ""),
                _clean_symbol(str(value.get("symbol") or "")),
                str(value.get("instrument_type") or "unknown"),
                str(value.get("valuation_basis") or "unknown"),
                str(value.get("data_as_of") or ""),
                str(value.get("retrieved_at") or _utc_now()),
                str(value.get("status") or "unavailable"),
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = _with_sample_summary(dict(payload))
        snapshot_id = str(value.get("snapshot_id") or _stable_id(
            "historicalpct",
            {
                "symbol": value.get("symbol"),
                "instrument_type": value.get("instrument_type"),
                "valuation_basis": value.get("valuation_basis"),
                "data_as_of": value.get("data_as_of"),
                "status": value.get("status"),
                "metrics": value.get("metrics"),
            },
        ))
        value["snapshot_id"] = snapshot_id
        with self.knowledge.connect() as conn:
            self._insert(conn, value, created_at=_utc_now())
        return value

    def latest(self, symbol: str) -> dict[str, Any] | None:
        with self.knowledge.connect() as conn:
            row = conn.execute(
                """SELECT payload_json FROM instrument_historical_percentile_snapshots
                   WHERE symbol=? ORDER BY data_as_of DESC, retrieved_at DESC LIMIT 1""",
                (_clean_symbol(symbol),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"] or "{}"))
        return payload if isinstance(payload, dict) else None

    def count(self, symbol: str) -> int:
        with self.knowledge.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM "
                "instrument_historical_percentile_snapshots WHERE symbol=?",
                (_clean_symbol(symbol),),
            ).fetchone()
        return int(row["count"] if row is not None else 0)


class InstrumentHistoricalPercentileService:
    """Dispatch instrument-specific percentile providers behind one contract."""

    supports_all_instruments = True

    def __init__(
        self,
        knowledge_store: ResearchKnowledgeStore,
        *,
        index_provider: BaifenweiIndexValuationProvider | None = None,
        equity_provider: BaoStockEquityValuationProvider | None = None,
        price_provider: YahooPricePercentileProvider | None = None,
        store: InstrumentHistoricalPercentileStore | None = None,
    ) -> None:
        self.store = store or InstrumentHistoricalPercentileStore(knowledge_store)
        self.index_provider = index_provider or BaifenweiIndexValuationProvider()
        self.equity_provider = equity_provider or BaoStockEquityValuationProvider()
        self.price_provider = price_provider or YahooPricePercentileProvider()

    def refresh(
        self,
        symbol: str,
        *,
        instrument_type: str | None = None,
        instrument_name: str = "",
        currency: str = "",
        tracked_index_code: str = "",
        tracked_index_name: str = "",
    ) -> dict[str, Any]:
        clean = _clean_symbol(symbol)
        kind = str(instrument_type or "").strip() or self._infer_instrument_type(clean)
        if kind in {"etf", "index"}:
            index_code = tracked_index_code or (_symbol_code(clean) if kind == "index" else "")
            raw = self.index_provider.fetch(
                clean,
                tracked_index_code=index_code,
                tracked_index_name=tracked_index_name or instrument_name,
            )
            payload = _normalize_index_snapshot(
                raw,
                instrument_type=kind,
                instrument_name=instrument_name,
            )
        elif kind == "company_equity" and _is_mainland_company_equity(clean):
            payload = self.equity_provider.fetch(
                clean,
                instrument_name=instrument_name,
                currency=currency or "CNY",
            )
        else:
            payload = self.price_provider.fetch(
                clean,
                instrument_type=kind,
                instrument_name=instrument_name,
                currency=currency,
            )
        saved = self.store.save(payload)
        return {**saved, "history_count": self.store.count(clean)}

    def latest_snapshot(self, symbol: str) -> dict[str, Any] | None:
        snapshot = self.store.latest(symbol)
        if snapshot is None:
            return None
        return {**snapshot, "history_count": self.store.count(symbol)}

    @staticmethod
    def _infer_instrument_type(symbol: str) -> str:
        from src.reports.instrument_profile import instrument_type

        return instrument_type(symbol)


def get_instrument_historical_percentile_service() -> InstrumentHistoricalPercentileService:
    from src.research.knowledge import get_research_knowledge_store

    return InstrumentHistoricalPercentileService(get_research_knowledge_store())
