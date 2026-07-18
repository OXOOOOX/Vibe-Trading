"""Discover report evidence and freeze immutable monitoring snapshots."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.portfolio.daily.store import DailyRunStore
from src.portfolio.state import normalize_symbol
from src.session.store import SessionStore

from .store import MonitoringStore


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_LIMITED_MARKERS = (
    "data limited",
    "limited data",
    "数据受限",
    "数据不足",
    "无法给出可监测",
    "不提供具体点位",
)


def _aware(value: Any, *, fallback: datetime | None = None) -> datetime:
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_SHANGHAI)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            try:
                parsed_date = date.fromisoformat(text[:10])
                return datetime.combine(parsed_date, time(15, 0), _SHANGHAI).astimezone(timezone.utc)
            except ValueError:
                pass
    return (fallback or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _trading_day_age(then: datetime, now: datetime) -> int:
    cursor = then.astimezone(_SHANGHAI).date()
    end = now.astimezone(_SHANGHAI).date()
    age = 0
    while cursor < end:
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
        if cursor.weekday() < 5:
            age += 1
    return age


def _looks_like_report(body: str) -> bool:
    text = str(body or "").strip()
    if len(text) < 500:
        return False
    headings = len(re.findall(r"(?m)^#{1,6}\s+\S", text))
    tables = len(re.findall(r"(?m)^\s*\|.+\|\s*$", text))
    return headings >= 2 or (headings >= 1 and tables >= 4)


def _quality_status(body: str, metadata: dict[str, Any]) -> str:
    declared = str(
        metadata.get("quality_status")
        or metadata.get("data_status")
        or metadata.get("status")
        or ""
    ).lower()
    if declared in {"limited", "data_limited", "weak", "partial"}:
        return "data_limited"
    lowered = body.lower()
    if any(marker in lowered for marker in _LIMITED_MARKERS):
        return "data_limited"
    return "ready"


def _report_type(kind: str) -> str:
    normalized = str(kind or "").lower()
    if normalized in {"holding", "holding_analysis", "portfolio_holding"}:
        return "holding_analysis"
    if normalized in {"daily", "daily_portfolio", "portfolio_daily"}:
        return "daily_portfolio"
    return "single_stock_research"


class MonitorReportCatalog:
    """Read existing report sources without treating Markdown as executable rules."""

    def __init__(
        self,
        *,
        store: MonitoringStore,
        session_store: SessionStore | None = None,
        daily_store: DailyRunStore | None = None,
        deep_report_service: Any | None = None,
        now_provider: Any | None = None,
    ) -> None:
        agent_root = Path(__file__).resolve().parents[3]
        self.store = store
        self.sessions = session_store or SessionStore(agent_root / "sessions")
        self.daily = daily_store or DailyRunStore()
        self.deep_reports = deep_report_service
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _public(candidate: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in candidate.items() if key != "body"}

    def list_candidates(self, symbol: str, *, include_body: bool = False) -> list[dict[str, Any]]:
        normalized = normalize_symbol(symbol).upper()
        candidates = (
            self._deep_report_candidates(normalized)
            + self._session_candidates(normalized)
            + self._daily_candidates(normalized)
        )
        unique: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            unique[candidate["report_ref"]] = candidate
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                1 if item["quality_status"] == "ready" else 0,
                1 if item.get("artifact_id") else 0,
                str(item["generated_at"]),
            ),
            reverse=True,
        )
        return ordered if include_body else [self._public(item) for item in ordered]

    def get_candidate(self, symbol: str, report_ref: str) -> dict[str, Any] | None:
        return next(
            (
                candidate
                for candidate in self.list_candidates(symbol, include_body=True)
                if candidate["report_ref"] == report_ref
            ),
            None,
        )

    def choose_candidate(
        self,
        symbol: str,
        report_ref: str | None = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        candidates = self.list_candidates(symbol, include_body=True)
        if report_ref:
            selected = next((item for item in candidates if item["report_ref"] == report_ref), None)
            return selected, ([] if selected else ["report_ref_not_found"])
        if not candidates:
            return None, ["no_valid_report"]
        selected = candidates[0]
        reasons = list(selected.get("research_reasons") or [])
        if len(candidates) > 1:
            peer = candidates[1]
            if (
                peer["report_type"] == selected["report_type"]
                and peer["generated_at"][:10] == selected["generated_at"][:10]
                and peer["body_sha256"] != selected["body_sha256"]
            ):
                reasons.append("report_revision_conflict")
        return selected, list(dict.fromkeys(reasons))

    def freeze(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return self.store.save_report_snapshot(candidate)

    def _decorate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        generated = _aware(candidate.get("generated_at"))
        data_as_of = _aware(candidate.get("data_as_of"), fallback=generated)
        now = self.now_provider().astimezone(timezone.utc)
        report_type = str(candidate["report_type"])
        max_age = 0 if report_type == "daily_portfolio" else 5
        age = _trading_day_age(data_as_of, now)
        quality = str(candidate.get("quality_status") or "ready")
        reasons: list[str] = []
        if quality != "ready":
            reasons.append("report_data_limited")
        if age > max_age:
            reasons.append("report_stale")
        body = str(candidate.get("body") or "")
        candidate.update(
            generated_at=generated.isoformat(),
            data_as_of=data_as_of.isoformat(),
            quality_status=quality,
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            trading_day_age=age,
            stale=age > max_age,
            research_reasons=reasons,
            excerpt=" ".join(body.strip().split())[:240],
        )
        return candidate

    def _deep_report_candidates(self, symbol: str) -> list[dict[str, Any]]:
        """Expose completed audited Deep Reports as first-class monitor evidence."""
        if self.deep_reports is None:
            return []
        result: list[dict[str, Any]] = []
        try:
            records = self.deep_reports.list(limit=500)
        except Exception:
            return []
        for record in records:
            if (
                str(getattr(record, "symbol", "")).upper() != symbol
                or str(getattr(record, "status", "")) != "completed"
                or str(getattr(record, "profile", "")) != "equity_deep_research"
            ):
                continue
            try:
                body = self.deep_reports.read_markdown(str(record.report_id))
            except (OSError, KeyError, ValueError):
                continue
            quality = str(getattr(record, "quality_status", "passed_with_gaps"))
            result.append(
                self._decorate(
                    {
                        "report_ref": f"deep-report:{record.report_id}",
                        "report_type": "equity_deep_research",
                        "symbol": symbol,
                        "title": (
                            f"{record.security_name or symbol}（{symbol}）穿透式深度研究"
                        ),
                        "source_id": str(record.report_id),
                        "source_message_id": None,
                        "artifact_id": "markdown",
                        "revision": int(getattr(record, "revision", 1) or 1),
                        "body": body,
                        "quality_status": (
                            "ready" if quality in {"passed", "passed_with_gaps"} else "data_limited"
                        ),
                        "generated_at": getattr(record, "updated_at", None)
                        or getattr(record, "created_at", None),
                        "data_as_of": getattr(record, "data_as_of", None)
                        or getattr(record, "report_date", None),
                        "metadata": {
                            "report_id": str(record.report_id),
                            "profile": "equity_deep_research",
                            "deep_report_quality_status": quality,
                            "generation_source": str(
                                getattr(record, "generation_source", "manual") or "manual"
                            ),
                            "generation_reason": str(
                                getattr(record, "generation_reason", "") or ""
                            ),
                        },
                    }
                )
            )
        return result

    def _session_candidates(self, symbol: str) -> list[dict[str, Any]]:
        code = symbol.split(".", 1)[0]
        result: list[dict[str, Any]] = []
        for session in self.sessions.list_sessions(limit=1000):
            config = dict(session.config or {})
            research = dict(config.get("research_session") or {})
            session_symbol = normalize_symbol(
                str(research.get("symbol") or config.get("symbol") or "")
            ).upper()
            title_upper = str(session.title or "").upper()
            if session_symbol != symbol and symbol not in title_upper and code not in title_upper:
                continue
            for message in reversed(self.sessions.get_messages(session.session_id, limit=300)):
                if message.role != "assistant" or not _looks_like_report(message.content):
                    continue
                metadata = dict(message.metadata or {})
                generated_at = metadata.get("generated_at") or message.created_at or session.updated_at
                data_as_of = (
                    metadata.get("data_as_of")
                    or metadata.get("market_as_of")
                    or research.get("data_as_of")
                    or generated_at
                )
                result.append(
                    self._decorate(
                        {
                            "report_ref": f"session:{session.session_id}:{message.message_id}",
                            "report_type": _report_type(str(research.get("kind") or "symbol")),
                            "symbol": symbol,
                            "title": str(metadata.get("title") or session.title or f"{symbol} research report"),
                            "source_id": session.session_id,
                            "source_message_id": message.message_id,
                            "artifact_id": metadata.get("artifact_id"),
                            "revision": int(metadata.get("revision") or 1),
                            "body": message.content,
                            "quality_status": _quality_status(message.content, metadata),
                            "generated_at": generated_at,
                            "data_as_of": data_as_of,
                            "metadata": {
                                "session_status": str(session.status.value),
                                "research_session": research,
                                "message_metadata": metadata,
                            },
                        }
                    )
                )
                break
        return result

    def _daily_candidates(self, symbol: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for record in self.daily.list(limit=120):
            worker_symbols = {
                normalize_symbol(str(worker.get("symbol") or "")).upper()
                for worker in (record.get("workers") or [])
                if isinstance(worker, dict)
            }
            if symbol not in worker_symbols:
                continue
            for artifact in record.get("artifacts") or []:
                if not isinstance(artifact, dict) or artifact.get("superseded") or artifact.get("expired"):
                    continue
                if str(artifact.get("kind") or "") != "master_markdown":
                    continue
                path = Path(str(artifact.get("path") or ""))
                try:
                    body = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                generated_at = artifact.get("created_at") or record.get("completed_at") or record.get("created_at")
                data_as_of = record.get("market_date") or generated_at
                metadata = {
                    "run_id": record.get("run_id"),
                    "run_status": record.get("status"),
                    "data_status": record.get("data_status"),
                    "warnings": record.get("warnings") or [],
                }
                result.append(
                    self._decorate(
                        {
                            "report_ref": f"daily:{record.get('run_id')}:{artifact.get('artifact_id')}",
                            "report_type": "daily_portfolio",
                            "symbol": symbol,
                            "title": f"{record.get('market_date') or ''} daily portfolio report",
                            "source_id": str(record.get("run_id") or ""),
                            "source_message_id": None,
                            "artifact_id": artifact.get("artifact_id"),
                            "revision": int(artifact.get("revision") or record.get("revision") or 1),
                            "body": body,
                            "quality_status": _quality_status(body, metadata),
                            "generated_at": generated_at,
                            "data_as_of": data_as_of,
                            "metadata": metadata,
                        }
                    )
                )
        return result
