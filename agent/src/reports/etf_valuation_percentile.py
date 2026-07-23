"""Durable tracked-index valuation percentiles for ETF report dossiers.

The report center reads stored snapshots only.  A network refresh is explicit
and records the third-party source, observation date, and mapping method so an
ETF's own quote is never presented as company-style PE/PB/PS valuation.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urljoin

from backtest.loaders._http import resolve_min_interval, throttled_get
from src.research.knowledge import ResearchKnowledgeStore


_INDEX_LIST_URL = "https://baifenwei.com/indices/"
_METHODOLOGY_URL = "https://baifenwei.com/methodology/"
_ROW_PATTERN = re.compile(
    r'<tr\b(?P<attrs>[^>]*)\bid="idx-(?P<code>[^"]+)"[^>]*>(?P<body>.*?)</tr>',
    re.IGNORECASE | re.DOTALL,
)
_CELL_PATTERN = re.compile(r"<td\b(?P<attrs>[^>]*)>(?P<body>.*?)</td>", re.IGNORECASE | re.DOTALL)
_ATTR_PATTERN = re.compile(r'(?P<name>[\w:-]+)\s*=\s*"(?P<value>[^"]*)"', re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _attributes(value: str) -> dict[str, str]:
    return {
        match.group("name").lower(): html.unescape(match.group("value"))
        for match in _ATTR_PATTERN.finditer(value)
    }


def _text(value: str) -> str:
    stripped = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html.unescape(stripped)).strip()


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _index_code(value: str) -> str:
    return str(value or "").strip().upper().split(".", 1)[0]


def _temperature(percentile: float | None) -> str:
    if percentile is None:
        return "暂无"
    if percentile < 10:
        return "极冷"
    if percentile < 30:
        return "偏冷"
    if percentile < 70:
        return "正常"
    if percentile < 90:
        return "偏热"
    return "极热"


def _data_timestamp(value: str) -> str:
    day = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return f"{day}T00:00:00+08:00"
    return _utc_now()


def _get_index_page(url: str) -> str:
    response = throttled_get(
        url,
        host_key="baifenwei",
        min_interval=resolve_min_interval("VIBE_TRADING_BAIFENWEI_MIN_INTERVAL", 1.0),
        params={},
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Vibe-Trading/1.0 (+local report dossier refresh)",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.text


def _get_csindex_history(index_code: str) -> list[tuple[Any, ...]]:
    import akshare as ak

    end = date.today()
    start = end - timedelta(days=3653)
    frame = ak.stock_zh_index_hist_csindex(
        symbol=index_code,
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    return [tuple(row) for row in frame.itertuples(index=False, name=None)]


def parse_index_valuation_rows(page: str) -> list[dict[str, Any]]:
    """Parse the public index table without executing page scripts."""

    rows: list[dict[str, Any]] = []
    for match in _ROW_PATTERN.finditer(page or ""):
        cells = list(_CELL_PATTERN.finditer(match.group("body")))
        if len(cells) < 9:
            continue
        values = [_attributes(cell.group("attrs")).get("data-v") for cell in cells]
        href_match = re.search(r'<a\b[^>]*href="([^"]+)"', cells[0].group("body"), re.IGNORECASE)
        name_match = re.search(r"<strong\b[^>]*>(.*?)</strong>", cells[0].group("body"), re.IGNORECASE | re.DOTALL)
        metrics = {
            "pe": _number(values[2]),
            "pe_percentile": _number(values[3]),
            "pb": _number(values[4]),
            "pb_percentile": _number(values[5]),
            "ps": _number(values[6]),
            "ps_percentile": _number(values[7]),
        }
        if not values[1] or not any(value is not None for value in metrics.values()):
            continue
        rows.append({
            "index_code": _index_code(match.group("code")),
            "index_name": _text(name_match.group(1)) if name_match else _index_code(match.group("code")),
            "data_date": values[1],
            "detail_url": urljoin(_INDEX_LIST_URL, html.unescape(href_match.group(1))) if href_match else _INDEX_LIST_URL,
            **metrics,
        })
    return rows


class BaifenweiIndexValuationProvider:
    """Read PE/PB/PS and their ten-year percentiles for an ETF's index."""

    def __init__(
        self,
        *,
        get_text_fn: Callable[[str], str] = _get_index_page,
        get_csindex_history_fn: Callable[[str], list[tuple[Any, ...]]] = (
            _get_csindex_history
        ),
        now_provider: Callable[[], str] = _utc_now,
    ) -> None:
        self.get_text = get_text_fn
        self.get_csindex_history = get_csindex_history_fn
        self.now_provider = now_provider

    def fetch(
        self,
        symbol: str,
        *,
        tracked_index_code: str,
        tracked_index_name: str = "",
    ) -> dict[str, Any]:
        retrieved_at = self.now_provider()
        page_error: str | None = None
        try:
            page = self.get_text(_INDEX_LIST_URL)
            rows = parse_index_valuation_rows(page)
        except Exception as exc:
            rows = []
            page_error = str(exc)[:180]
        wanted_code = _index_code(tracked_index_code)
        matched = next((row for row in rows if row["index_code"] == wanted_code), None)
        page_date = max((str(row.get("data_date") or "") for row in rows), default="")
        page_date = page_date or retrieved_at[:10]
        if matched is None and wanted_code:
            try:
                fallback = self._csindex_fallback(
                    symbol,
                    tracked_index_code=wanted_code,
                    tracked_index_name=tracked_index_name,
                    retrieved_at=retrieved_at,
                )
            except Exception:
                fallback = None
            if fallback is not None:
                if page_error:
                    fallback["warnings"].append(
                        "第三方百分位页本次不可用，当前使用中证指数官方历史滚动市盈率计算。"
                    )
                return fallback
        source_fingerprint = matched or {
            "data_date": page_date,
            "available_codes": sorted(row["index_code"] for row in rows),
        }
        source_id = _stable_id("baifenwei", source_fingerprint)
        source = {
            "source_id": source_id,
            "provider_id": "baifenwei_index_valuation",
            "label": "百分位 · 指数估值",
            "publisher": "百分位 baifenwei.com",
            "verification_status": "public_secondary",
            "url": matched["detail_url"] if matched else _INDEX_LIST_URL,
            "methodology_url": _METHODOLOGY_URL,
            "retrieved_at": retrieved_at,
        }
        if matched is None:
            display_index = tracked_index_name or wanted_code or "跟踪指数"
            return {
                "schema_version": 1,
                "symbol": str(symbol or "").strip().upper(),
                "tracked_index_code": wanted_code or None,
                "tracked_index_name": display_index,
                "status": "unavailable",
                "lookback_years": 10,
                "data_as_of": _data_timestamp(page_date),
                "retrieved_at": retrieved_at,
                "mapping_method": "tracked_index_code_not_covered",
                "metrics": [],
                "source": source,
                "unavailable_reason": (
                    f"当前百分位数据源尚未覆盖跟踪指数 {display_index}"
                    f"{f'（{wanted_code}）' if wanted_code else ''}。"
                ),
                "warnings": ["百分位仅描述历史相对位置，不代表未来涨跌或买卖信号。"],
            }

        metrics = []
        for key, label in (("pe", "PE · 市盈率"), ("pb", "PB · 市净率"), ("ps", "PS · 市销率")):
            percentile = _number(matched.get(f"{key}_percentile"))
            metrics.append({
                "key": key,
                "label": label,
                "value": _number(matched.get(key)),
                "percentile": percentile,
                "temperature": _temperature(percentile),
            })
        return {
            "schema_version": 1,
            "symbol": str(symbol or "").strip().upper(),
            "tracked_index_code": matched["index_code"],
            "tracked_index_name": matched["index_name"],
            "status": "available",
            "lookback_years": 10,
            "data_as_of": _data_timestamp(str(matched["data_date"])),
            "retrieved_at": retrieved_at,
            "mapping_method": "tracked_index_code_exact",
            "metrics": metrics,
            "source": source,
            "unavailable_reason": None,
            "warnings": [
                "这里展示跟踪指数估值，不是 ETF 产品自身的公司式 PE/PB/PS。",
                "百分位仅描述历史相对位置，不代表未来涨跌或买卖信号。",
            ],
        }

    def _csindex_fallback(
        self,
        symbol: str,
        *,
        tracked_index_code: str,
        tracked_index_name: str,
        retrieved_at: str,
    ) -> dict[str, Any] | None:
        rows = self.get_csindex_history(tracked_index_code)
        observations: list[tuple[str, float]] = []
        for row in rows:
            if len(row) < 2:
                continue
            value = _number(row[-1])
            if value is None or value <= 0:
                continue
            day = str(row[0] or "")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                observations.append((day, value))
        observations.sort(key=lambda item: item[0])
        if not observations:
            return None
        data_date, current_pe = observations[-1]
        percentile = (
            sum(value < current_pe for _, value in observations)
            / len(observations)
            * 100
        )
        detail_url = (
            "https://www.csindex.com.cn/zh-CN/indices/index-detail/"
            f"{tracked_index_code}#/indices/family/detail?indexCode={tracked_index_code}"
        )
        source_id = _stable_id("csindexvaluation", {
            "index_code": tracked_index_code,
            "data_date": data_date,
            "current_pe": current_pe,
            "percentile": percentile,
            "observation_count": len(observations),
        })
        return {
            "schema_version": 1,
            "symbol": str(symbol or "").strip().upper(),
            "tracked_index_code": tracked_index_code,
            "tracked_index_name": tracked_index_name or tracked_index_code,
            "status": "available",
            "lookback_years": 10,
            "data_as_of": _data_timestamp(data_date),
            "retrieved_at": retrieved_at,
            "mapping_method": "tracked_index_code_exact_csindex",
            "metrics": [{
                "key": "pe",
                "label": "PE · 滚动市盈率",
                "value": current_pe,
                "percentile": percentile,
                "temperature": _temperature(percentile),
            }],
            "source": {
                "source_id": source_id,
                "provider_id": "csindex_official_history",
                "label": "中证指数 · 历史估值",
                "publisher": "中证指数有限公司",
                "verification_status": "official_primary",
                "url": detail_url,
                "methodology_url": detail_url,
                "retrieved_at": retrieved_at,
            },
            "unavailable_reason": None,
            "warnings": [
                "中证历史接口当前只提供滚动市盈率，PB、PS 百分位暂不补造。",
                "百分位按近 10 年有效正值样本中低于当前 PE 的观测占比计算。",
                "百分位仅描述历史相对位置，不代表未来涨跌或买卖信号。",
            ],
        }


class ETFValuationPercentileStore:
    """Append-only ETF valuation snapshots stored beside the report catalog."""

    def __init__(self, knowledge_store: ResearchKnowledgeStore) -> None:
        self.knowledge = knowledge_store
        self.initialize()

    def initialize(self) -> None:
        with self.knowledge.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS etf_valuation_percentile_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    tracked_index_code TEXT,
                    data_as_of TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_etf_valuation_percentile_symbol
                    ON etf_valuation_percentile_snapshots(
                        symbol, data_as_of DESC, retrieved_at DESC
                    );
                """
            )

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = dict(payload)
        snapshot_id = str(value.get("snapshot_id") or _stable_id("etfvaluation", {
            "symbol": value.get("symbol"),
            "tracked_index_code": value.get("tracked_index_code"),
            "data_as_of": value.get("data_as_of"),
            "status": value.get("status"),
            "metrics": value.get("metrics"),
        }))
        value["snapshot_id"] = snapshot_id
        with self.knowledge.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO etf_valuation_percentile_snapshots(
                   snapshot_id,symbol,tracked_index_code,data_as_of,retrieved_at,
                   status,payload_json,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    str(value.get("symbol") or "").upper(),
                    value.get("tracked_index_code"),
                    str(value.get("data_as_of") or ""),
                    str(value.get("retrieved_at") or _utc_now()),
                    str(value.get("status") or "unavailable"),
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    _utc_now(),
                ),
            )
        return value

    def latest(self, symbol: str) -> dict[str, Any] | None:
        with self.knowledge.connect() as conn:
            row = conn.execute(
                """SELECT payload_json FROM etf_valuation_percentile_snapshots
                   WHERE symbol=? ORDER BY data_as_of DESC, retrieved_at DESC LIMIT 1""",
                (str(symbol or "").upper(),),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def count(self, symbol: str) -> int:
        with self.knowledge.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM etf_valuation_percentile_snapshots WHERE symbol=?",
                (str(symbol or "").upper(),),
            ).fetchone()
        return int(row["count"] if row is not None else 0)


class ETFValuationPercentileService:
    """Refresh and retrieve the report layer's tracked-index valuation snapshot."""

    def __init__(
        self,
        knowledge_store: ResearchKnowledgeStore,
        *,
        provider: BaifenweiIndexValuationProvider | None = None,
    ) -> None:
        self.store = ETFValuationPercentileStore(knowledge_store)
        self.provider = provider or BaifenweiIndexValuationProvider()

    def refresh(
        self,
        symbol: str,
        *,
        tracked_index_code: str,
        tracked_index_name: str = "",
    ) -> dict[str, Any]:
        payload = self.provider.fetch(
            symbol,
            tracked_index_code=tracked_index_code,
            tracked_index_name=tracked_index_name,
        )
        saved = self.store.save(payload)
        return {**saved, "history_count": self.store.count(symbol)}

    def latest_snapshot(self, symbol: str) -> dict[str, Any] | None:
        snapshot = self.store.latest(symbol)
        if snapshot is None:
            return None
        return {**snapshot, "history_count": self.store.count(symbol)}
