"""Read-only evidence collection for autonomous monitoring research."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from src.tools.fund_flow_tool import FundFlowTool
from src.tools.sector_tool import SectorInfoTool
from src.tools.stock_news_tool import StockNewsTool


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


_OPERATIONAL_KEYS = {
    "collected_at", "fetched_at", "generated_at", "requested_at",
    "request_id", "elapsed_ms", "latency_ms",
}


def _stable_payload(value: Any) -> Any:
    """Remove collector-only metadata while retaining source fact times."""

    if isinstance(value, dict):
        return {
            key: _stable_payload(item)
            for key, item in value.items()
            if key not in _OPERATIONAL_KEYS
        }
    if isinstance(value, list):
        return [_stable_payload(item) for item in value]
    return value


def _parse(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {"ok": False, "error": "non_object_response"}


class AutonomousEvidenceCollector:
    """Collect only from explicitly read-only tools; no trading tool is reachable."""

    def __init__(
        self,
        *,
        news_tool: Any | None = None,
        fund_flow_tool: Any | None = None,
        sector_tool: Any | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.news_tool = news_tool or StockNewsTool()
        self.fund_flow_tool = fund_flow_tool or FundFlowTool()
        self.sector_tool = sector_tool or SectorInfoTool()
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _fact(
        *,
        fact_id: str,
        category: str,
        trust_level: str,
        source: str,
        data_as_of: str | None,
        unit: str | None,
        adjustment: str | None,
        payload: Any,
        status: str = "ready",
    ) -> dict[str, Any]:
        return {
            "fact_id": fact_id,
            "category": category,
            "trust_level": trust_level,
            "source": source,
            "data_as_of": data_as_of,
            "unit": unit,
            "adjustment": adjustment,
            "status": status,
            "payload": payload,
            "raw_sha256": _hash(_stable_payload(payload)),
        }

    def collect(
        self,
        *,
        symbol: str,
        holding: dict[str, Any],
        report_snapshot: dict[str, Any],
        market_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        collected_at = self.now_factory().astimezone(timezone.utc).isoformat()
        # Collection time is operational metadata, not evidence.  Keep the
        # market fact stable while the verified quote/bars themselves are
        # unchanged so a 30-minute probe cannot manufacture a false change.
        stable_market_evidence = {
            key: market_evidence.get(key)
            for key in (
                "symbol", "holding", "quote", "last_price", "tick_size",
                "atr20", "bar_hashes", "data_as_of", "data_mode",
            )
            if key in market_evidence
        }
        facts = [
            self._fact(
                fact_id="holding",
                category="holding",
                trust_level="A",
                source="portfolio_state",
                data_as_of=str(holding.get("updated_at") or collected_at),
                unit="shares",
                adjustment=None,
                payload={
                    key: holding.get(key)
                    for key in ("symbol", "code", "name", "quantity", "cost_price", "updated_at")
                },
            ),
            self._fact(
                fact_id="verified_market",
                category="market",
                trust_level="A",
                source="verified_consensus_cache",
                data_as_of=str(market_evidence.get("data_as_of") or collected_at),
                unit="CNY",
                adjustment="raw",
                payload=stable_market_evidence,
            ),
            self._fact(
                fact_id="report_snapshot",
                category="artifact",
                trust_level="A" if report_snapshot.get("artifact_id") else "C",
                source=str(report_snapshot.get("report_ref") or "report_catalog"),
                data_as_of=str(report_snapshot.get("data_as_of") or collected_at),
                unit=None,
                adjustment=None,
                payload={
                    key: report_snapshot.get(key)
                    for key in (
                        "snapshot_id", "report_ref", "report_type", "title", "revision",
                        "body_sha256", "quality_status", "generated_at", "data_as_of",
                    )
                },
            ),
        ]
        auxiliary: dict[str, Any] = {}
        calls = (
            ("news", "news", self.news_tool, {"code": symbol, "scope": "stock", "limit": 12}),
            ("fund_flow", "fund_flow", self.fund_flow_tool, {"codes": [symbol], "period": "min", "days": 1}),
            ("sector", "sector_state", self.sector_tool, {"code": symbol, "mode": "membership"}),
        )
        for fact_id, category, tool, kwargs in calls:
            try:
                payload = _parse(tool.execute(**kwargs))
            except Exception as exc:
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            status = "ready" if payload.get("ok") is True else "unavailable"
            source = str(payload.get("source") or getattr(tool, "name", fact_id))
            facts.append(
                self._fact(
                    fact_id=fact_id,
                    category=category,
                    trust_level="B" if status == "ready" else "C",
                    source=source,
                    data_as_of=(
                        payload.get("data_as_of")
                        or payload.get("timestamp")
                        or collected_at
                    ),
                    unit="CNY" if category == "fund_flow" else None,
                    adjustment=None,
                    payload=payload,
                    status=status,
                )
            )
            if category == "fund_flow" and status == "ready":
                rows = (((payload.get("data") or {}).get(symbol) or {}).get("rows") or [])
                latest = rows[-1] if rows else {}
                auxiliary["fund_flow"] = {
                    "status": "ready" if latest else "unavailable",
                    "source": source,
                    "data_as_of": latest.get("timestamp") or collected_at,
                    "main": latest.get("main"),
                    "large": latest.get("large"),
                    "super_large": latest.get("super_large"),
                }
            elif category == "sector_state" and status == "ready":
                boards = ((payload.get("data") or {}).get("boards") or [])
                changes = [item.get("change_pct") for item in boards if isinstance(item.get("change_pct"), (int, float))]
                auxiliary["sector_state"] = {
                    "status": "ready" if boards else "unavailable",
                    "source": source,
                    "data_as_of": collected_at,
                    "change_pct": max(changes) if changes else None,
                    "boards": boards[:20],
                }
        fingerprint_payload = [
            {
                key: fact.get(key)
                # data_as_of is deliberately excluded: some read-only
                # suppliers do not publish a source timestamp.  Their raw
                # payload hash remains the deterministic change detector.
                for key in ("fact_id", "category", "trust_level", "source", "unit", "adjustment", "status", "raw_sha256")
            }
            for fact in facts
        ]
        return {
            "symbol": symbol,
            "collected_at": collected_at,
            "facts": facts,
            "auxiliary": auxiliary,
            "evidence_fingerprint": _hash(fingerprint_payload),
            "tool_policy": {
                "mode": "read_only",
                "allowed": ["portfolio_state", "verified_market", "stock_news", "fund_flow", "sector_info"],
                "trading_tools": "forbidden",
            },
        }
