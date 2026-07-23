"""Discover report evidence and freeze immutable monitoring snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.portfolio.daily.store import DailyRunStore
from src.portfolio.weekly.store import WeeklyRunStore
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
        weekly_store: WeeklyRunStore | None = None,
        deep_report_service: Any | None = None,
        report_library_service: Any | None = None,
        now_provider: Any | None = None,
    ) -> None:
        agent_root = Path(__file__).resolve().parents[3]
        self.store = store
        self.sessions = session_store or SessionStore(agent_root / "sessions")
        self.daily = daily_store or DailyRunStore()
        self.weekly = weekly_store or WeeklyRunStore()
        self.deep_reports = deep_report_service
        self.report_library = report_library_service
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _public(candidate: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in candidate.items() if key != "body"}

    def list_candidates(self, symbol: str, *, include_body: bool = False) -> list[dict[str, Any]]:
        normalized = normalize_symbol(symbol).upper()
        discovered = (
            self._deep_report_candidates(normalized)
            + self._session_candidates(normalized)
            + self._daily_candidates(normalized)
            + self._weekly_candidates(normalized)
            + self._monitor_research_candidates(normalized)
        )
        candidates = self._catalog_candidates(normalized, discovered)
        unique: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            unique[candidate["report_ref"]] = candidate
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                1 if isinstance(item.get("monitoring_bundle"), dict) else 0,
                1 if item.get("cataloged") else 0,
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
        payload = dict(candidate)
        metadata = dict(payload.get("metadata") or {})
        if isinstance(payload.get("monitoring_bundle"), dict):
            metadata["monitoring_bundle"] = payload["monitoring_bundle"]
        payload["metadata"] = metadata
        frozen = self.store.save_report_snapshot(payload)
        frozen_metadata = frozen.get("metadata") if isinstance(frozen.get("metadata"), dict) else {}
        if isinstance(frozen_metadata.get("monitoring_bundle"), dict):
            frozen["monitoring_bundle"] = frozen_metadata["monitoring_bundle"]
        return frozen

    def _decorate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        generated = _aware(candidate.get("generated_at"))
        data_as_of = _aware(candidate.get("data_as_of"), fallback=generated)
        now = self.now_provider().astimezone(timezone.utc)
        report_type = str(candidate["report_type"])
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
        max_age = 5 if report_type == "weekly_review" else (
            0 if report_type == "daily_portfolio" or metadata.get("run_id") else 5
        )
        age = _trading_day_age(data_as_of, now)
        quality = str(candidate.get("quality_status") or "ready")
        reasons: list[str] = []
        if quality != "ready":
            reasons.append("report_data_limited")
        bundle = candidate.get("monitoring_bundle")
        bundle_deadlines: list[datetime] = []
        if isinstance(bundle, dict):
            for key in ("review_due_at", "source_valid_until", "valid_until"):
                if bundle.get(key):
                    bundle_deadlines.append(_aware(bundle[key]))
        stale = min(bundle_deadlines) <= now if bundle_deadlines else age > max_age
        if stale:
            reasons.append("report_stale")
        review_due_at = str(metadata.get("review_due_at") or "")
        if review_due_at:
            try:
                review_due = _aware(review_due_at)
                if review_due <= now:
                    reasons.append("weekly_review_due")
            except ValueError:
                reasons.append("weekly_review_due_invalid")
        body = str(candidate.get("body") or "")
        candidate.update(
            generated_at=generated.isoformat(),
            data_as_of=data_as_of.isoformat(),
            quality_status=quality,
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            trading_day_age=age,
            stale=stale,
            research_reasons=reasons,
            excerpt=" ".join(body.strip().split())[:240],
        )
        if isinstance(bundle, dict):
            candidate["monitoring_bundle_sha256"] = hashlib.sha256(
                json.dumps(bundle, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
        return candidate

    def _catalog_candidates(
        self,
        symbol: str,
        discovered: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Overlay the unified report catalog on resolvable source artifacts.

        Source stores remain authoritative for immutable bodies and bundles.  The
        catalog supplies report identity, horizon, quality, coverage and version
        relationships, so monitoring no longer invents a second ordering model.
        Uncataloged session reports remain a compatibility fallback.
        """

        if self.report_library is None:
            return discovered
        try:
            if hasattr(self.report_library, "subject") and hasattr(
                self.report_library, "get_report"
            ):
                subject = self.report_library.subject(
                    symbol,
                    include_timeline=False,
                    history_mode="current_families",
                )
                report_ids: list[str] = []
                for horizon in ("intraday", "daily", "weekly", "structural"):
                    horizon_state = (subject.get("current") or {}).get(horizon) or {}
                    for key in ("latest", "latest_complete"):
                        report_id = str((horizon_state.get(key) or {}).get("report_id") or "")
                        if report_id and report_id not in report_ids:
                            report_ids.append(report_id)
                reports = [
                    report
                    for report in (
                        self.report_library.get_report(report_id) for report_id in report_ids
                    )
                    if isinstance(report, dict)
                ]
            else:
                payload = self.report_library.list_reports(
                    query=symbol,
                    subject_type="symbol",
                    status="published",
                    limit=100,
                )
                reports = payload.get("reports") if isinstance(payload, dict) else []
        except Exception:
            return discovered
        if not isinstance(reports, list):
            return discovered

        def source_match(report: dict[str, Any]) -> dict[str, Any] | None:
            source_type = str(report.get("source_type") or "")
            source_id = str(report.get("source_id") or "")
            link = report.get("knowledge_link") if isinstance(report.get("knowledge_link"), dict) else {}
            locator = str(link.get("monitoring_bundle_source_locator") or "")
            if source_type == "deep_report":
                expected = f"deep-report:{source_id}"
                return next(
                    (item for item in discovered if item.get("report_ref") == expected),
                    None,
                )
            if source_type == "monitor_research":
                return next(
                    (item for item in discovered if item.get("report_ref") == source_id),
                    None,
                )
            if locator.startswith("daily-run:"):
                _, run_id, artifact_id = locator.split(":", 2)
                return next(
                    (
                        item for item in discovered
                        if str((item.get("metadata") or {}).get("run_id") or "") == run_id
                        and str(item.get("artifact_id") or "") == artifact_id
                    ),
                    None,
                )
            if locator.startswith("weekly-run:"):
                _, run_id, artifact_id = locator.split(":", 2)
                return next(
                    (
                        item for item in discovered
                        if str((item.get("metadata") or {}).get("run_id") or "") == run_id
                        and str(item.get("artifact_id") or "") == artifact_id
                    ),
                    None,
                )
            return None

        cataloged: list[dict[str, Any]] = []
        consumed: set[str] = set()
        for raw_report in reports:
            if not isinstance(raw_report, dict):
                continue
            report_symbol = normalize_symbol(
                str(raw_report.get("symbol") or raw_report.get("subject_key") or "")
            ).upper()
            if report_symbol != symbol or raw_report.get("report_quality_status") == "failed_validation":
                continue
            candidate = source_match(raw_report)
            if candidate is None:
                continue
            value = dict(candidate)
            metadata = dict(value.get("metadata") or {})
            viewpoints = [
                dict(item)
                for item in raw_report.get("viewpoints") or []
                if isinstance(item, dict)
            ]
            metadata.update(
                catalog_report_id=raw_report.get("report_id"),
                catalog_family_id=raw_report.get("family_id"),
                catalog_report_kind=raw_report.get("report_kind"),
                report_quality_status=raw_report.get("report_quality_status"),
                coverage_status=raw_report.get("coverage_status"),
                report_period=raw_report.get("report_period") or {},
                viewpoint_horizons=[str(item.get("horizon") or "") for item in viewpoints],
            )
            value.update(
                metadata=metadata,
                cataloged=True,
                catalog_report_id=raw_report.get("report_id"),
                catalog_family_id=raw_report.get("family_id"),
                catalog_report_kind=raw_report.get("report_kind"),
                report_quality_status=raw_report.get("report_quality_status"),
                coverage_status=raw_report.get("coverage_status"),
                report_period=raw_report.get("report_period") or {},
                viewpoints=viewpoints,
                relations=[
                    dict(item)
                    for item in raw_report.get("relations") or []
                    if isinstance(item, dict)
                ],
            )
            cataloged.append(value)
            consumed.add(str(candidate.get("report_ref") or ""))
        if not cataloged:
            return discovered
        # A session report has no formal catalog entry by design and remains a
        # compatibility fallback.  Published daily/weekly/deep revisions are
        # intentionally restricted to the catalog's current/latest-complete
        # set so one report family cannot create a revision-trigger storm.
        return [
            *cataloged,
            *(
                item
                for item in discovered
                if str(item.get("report_ref") or "").startswith("session:")
                and str(item.get("report_ref") or "") not in consumed
            ),
        ]

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
            profile = str(getattr(record, "profile", "") or "")
            if (
                str(getattr(record, "symbol", "")).upper() != symbol
                or str(getattr(record, "status", "")) != "completed"
                or profile
                not in {"equity_deep_research", "etf_deep_research", "index_deep_research"}
            ):
                continue
            try:
                body = self.deep_reports.read_markdown(str(record.report_id))
            except (OSError, KeyError, ValueError):
                continue
            quality = str(getattr(record, "quality_status", "passed_with_gaps"))
            monitoring_bundle: dict[str, Any] | None = None
            try:
                monitoring_path = self.deep_reports.artifact_path(
                    str(record.report_id),
                    "monitoring_bundle",
                )
                parsed_bundle = json.loads(monitoring_path.read_text(encoding="utf-8"))
                if isinstance(parsed_bundle, dict):
                    monitoring_bundle = parsed_bundle
            except (AttributeError, OSError, KeyError, ValueError, json.JSONDecodeError):
                monitoring_bundle = None
            result.append(
                self._decorate(
                    {
                        "report_ref": f"deep-report:{record.report_id}",
                        "report_type": profile,
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
                            "profile": profile,
                            "deep_report_quality_status": quality,
                            "generation_source": str(
                                getattr(record, "generation_source", "manual") or "manual"
                            ),
                            "generation_reason": str(
                                getattr(record, "generation_reason", "") or ""
                            ),
                            "monitoring_source": (
                                "structured_monitoring_bundle"
                                if monitoring_bundle is not None
                                else "legacy_extraction"
                            ),
                            "horizon": "structural",
                        },
                        "monitoring_bundle": monitoring_bundle,
                    }
                )
            )
        return result

    def _monitor_research_candidates(self, symbol: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for snapshot in self.store.list_report_snapshots(
            report_type="monitor_research",
            limit=500,
        ):
            if str(snapshot.get("symbol") or "").upper() != symbol:
                continue
            result.append(
                self._decorate(
                    {
                        "report_ref": str(snapshot.get("report_ref") or ""),
                        "report_type": "monitor_research",
                        "symbol": symbol,
                        "title": str(snapshot.get("title") or f"{symbol} 监控研究"),
                        "source_id": str(snapshot.get("source_id") or ""),
                        "source_message_id": snapshot.get("source_message_id"),
                        "artifact_id": snapshot.get("artifact_id"),
                        "revision": int(snapshot.get("revision") or 1),
                        "body": str(snapshot.get("body") or ""),
                        "quality_status": str(snapshot.get("quality_status") or "ready"),
                        "generated_at": snapshot.get("generated_at"),
                        "data_as_of": snapshot.get("data_as_of"),
                        "metadata": dict(snapshot.get("metadata") or {}),
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
            artifacts = [
                item
                for item in record.get("artifacts") or []
                if isinstance(item, dict)
                and not item.get("superseded")
                and not item.get("expired")
            ]
            holding_json = next(
                (
                    item for item in artifacts
                    if str(item.get("kind") or "") == "holding_daily_json"
                    and str(item.get("symbol") or "").upper() == symbol
                ),
                None,
            )
            holding_markdown = next(
                (
                    item for item in artifacts
                    if str(item.get("kind") or "") == "holding_daily_markdown"
                    and str(item.get("symbol") or "").upper() == symbol
                ),
                None,
            )
            master_markdown = next(
                (item for item in artifacts if str(item.get("kind") or "") == "master_markdown"),
                None,
            )
            source_artifact = holding_json or holding_markdown or master_markdown
            body_artifact = holding_markdown or master_markdown
            if source_artifact is None or body_artifact is None:
                continue
            try:
                body = Path(str(body_artifact.get("path") or "")).read_text(encoding="utf-8")
            except OSError:
                continue
            brief: dict[str, Any] = {}
            if holding_json is not None:
                try:
                    parsed = json.loads(
                        Path(str(holding_json.get("path") or "")).read_text(encoding="utf-8")
                    )
                    if isinstance(parsed, dict):
                        brief = parsed
                except (OSError, json.JSONDecodeError):
                    brief = {}
            bundle = brief.get("monitoring_bundle")
            generated_at = (
                (bundle or {}).get("generated_at")
                if isinstance(bundle, dict)
                else source_artifact.get("created_at")
            ) or record.get("completed_at") or record.get("created_at")
            data_as_of = (
                (bundle or {}).get("data_as_of")
                if isinstance(bundle, dict)
                else brief.get("data_as_of")
            ) or record.get("market_date") or generated_at
            metadata = {
                "run_id": record.get("run_id"),
                "run_status": record.get("status"),
                "data_status": record.get("data_status"),
                "warnings": record.get("warnings") or [],
                "monitoring_source": (
                    "structured_monitoring_bundle"
                    if isinstance(bundle, dict)
                    else "legacy_extraction"
                ),
                "monitoring_bundle_artifact_id": (
                    holding_json.get("artifact_id") if holding_json else None
                ),
            }
            quality_status = _quality_status(body, metadata)
            if isinstance(bundle, dict) and bundle.get("monitoring_status") == "data_insufficient":
                quality_status = "data_limited"
            result.append(
                self._decorate(
                    {
                        "report_ref": f"daily:{record.get('run_id')}:{source_artifact.get('artifact_id')}",
                        "report_type": "holding_analysis",
                        "symbol": symbol,
                        "title": f"{record.get('market_date') or ''} {symbol} holding daily report",
                        "source_id": str(record.get("run_id") or ""),
                        "source_message_id": None,
                        "artifact_id": source_artifact.get("artifact_id"),
                        "revision": int(source_artifact.get("revision") or record.get("revision") or 1),
                        "body": body,
                        "quality_status": quality_status,
                        "generated_at": generated_at,
                        "data_as_of": data_as_of,
                        "metadata": metadata,
                        "monitoring_bundle": bundle if isinstance(bundle, dict) else None,
                    }
                )
            )
        return result

    def _weekly_candidates(self, symbol: str) -> list[dict[str, Any]]:
        """Expose immutable weekly JSON bundles without parsing Markdown rules."""

        result: list[dict[str, Any]] = []
        for record in self.weekly.list(limit=240):
            if (
                str(record.get("symbol") or "").upper() != symbol
                or str(record.get("status") or "")
                not in {"completed", "completed_with_warnings"}
                or str(record.get("quality_status") or "") == "failed_validation"
            ):
                continue
            artifacts = [
                item
                for item in record.get("artifacts") or []
                if isinstance(item, dict)
                and not item.get("superseded")
                and not item.get("expired")
            ]
            json_artifact = next(
                (item for item in artifacts if item.get("kind") == "weekly_review_json"),
                None,
            )
            markdown_artifact = next(
                (item for item in artifacts if item.get("kind") == "weekly_review_markdown"),
                None,
            )
            if json_artifact is None or markdown_artifact is None:
                continue
            try:
                brief = json.loads(
                    Path(str(json_artifact.get("path") or "")).read_text(encoding="utf-8")
                )
                body = Path(str(markdown_artifact.get("path") or "")).read_text(
                    encoding="utf-8"
                )
            except (OSError, json.JSONDecodeError):
                continue
            bundle = brief.get("monitoring_bundle") if isinstance(brief, dict) else None
            if not isinstance(bundle, dict):
                continue
            quality_status = (
                "ready"
                if brief.get("quality_status") in {"passed", "passed_with_gaps"}
                else "data_limited"
            )
            result.append(
                self._decorate(
                    {
                        "report_ref": f"weekly:{record.get('run_id')}:{json_artifact.get('artifact_id')}",
                        "report_type": "weekly_review",
                        "symbol": symbol,
                        "title": (
                            f"{record.get('week_end') or ''} "
                            f"{record.get('security_name') or symbol} 周度复盘"
                        ),
                        "source_id": str(record.get("run_id") or ""),
                        "source_message_id": None,
                        "artifact_id": json_artifact.get("artifact_id"),
                        "revision": int(record.get("revision") or 1),
                        "body": body,
                        "quality_status": quality_status,
                        "generated_at": brief.get("generated_at"),
                        "data_as_of": brief.get("data_as_of"),
                        "metadata": {
                            "run_id": record.get("run_id"),
                            "report_id": brief.get("report_id"),
                            "report_kind": "weekly_review",
                            "horizon": "weekly",
                            "week_start": brief.get("week_start"),
                            "week_end": brief.get("week_end"),
                            "review_due_at": brief.get("review_due_at"),
                            "source_valid_until": brief.get("source_valid_until"),
                            "monitoring_source": "structured_monitoring_bundle",
                            "monitoring_bundle_artifact_id": json_artifact.get("artifact_id"),
                        },
                        "monitoring_bundle": bundle,
                    }
                )
            )
        return result
