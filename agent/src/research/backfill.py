"""Idempotent local backfill for the unified report source archive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.config.paths import get_runtime_root

from .knowledge import ResearchKnowledgeStore, get_research_knowledge_store, normalize_url
from .official_filings import OfficialFilingService, get_official_filing_service
from .source_ingestion import SourceIngestionService
from .structured_financials import OfficialFinancialExtractionService


class SourceArchiveBackfill:
    def __init__(
        self,
        *,
        store: ResearchKnowledgeStore | None = None,
        ingestion: SourceIngestionService | None = None,
        official: OfficialFilingService | None = None,
        financial_extraction: OfficialFinancialExtractionService | None = None,
    ) -> None:
        self.store = store or get_research_knowledge_store()
        self.ingestion = ingestion or SourceIngestionService(self.store)
        self.official = official or get_official_filing_service()
        self.financial_extraction = financial_extraction or OfficialFinancialExtractionService(
            store=self.store,
            ingestion=self.ingestion,
        )

    def _backfill_structured_financials(self, *, dry_run: bool) -> dict[str, int]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT o.document_ref,o.subject_key
                   FROM source_observations o
                   JOIN source_documents d USING(document_ref)
                   WHERE o.verification_status='official_primary'
                     AND o.subject_key NOT IN ('PORTFOLIO','MARKET')
                   ORDER BY o.subject_key,o.document_ref"""
            ).fetchall()
        if dry_run:
            return {"candidates": len(rows)}
        counts = {
            "candidates": len(rows),
            "validated": 0,
            "needs_review": 0,
            "not_applicable": 0,
            "failed": 0,
            "cached": 0,
            "metrics": 0,
        }
        for row in rows:
            result = self.financial_extraction.extract_document(
                str(row["document_ref"]),
                str(row["subject_key"]),
                force=False,
            )
            status = str(result.get("status") or "failed")
            if status in counts:
                counts[status] += 1
            counts["cached"] += int(bool(result.get("cached")))
            counts["metrics"] += int(result.get("metrics_count") or 0)
        return counts

    def _research_cache_rows(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_documents'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute("SELECT * FROM research_documents ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def _backfill_research_cache(self, *, dry_run: bool) -> int:
        rows = self._research_cache_rows()
        if dry_run:
            return len(rows)
        with self.store.connect() as conn:
            completed_origins = {
                str(row["origin_id"])
                for row in conn.execute(
                    "SELECT DISTINCT origin_id FROM source_observations "
                    "WHERE origin_type='legacy_cache'"
                ).fetchall()
            }
        count = 0
        for row in rows:
            origin_id = f"research-cache:{row.get('id')}"
            if origin_id in completed_origins:
                continue
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            for key in ("title", "published_at", "source", "url", "snippet"):
                if key not in payload and row.get(key) is not None:
                    payload[key] = row.get(key)
            archived = self.ingestion.ingest_provider_documents(
                kind=str(row.get("kind") or "other"),
                symbol=str(row.get("symbol") or ""),
                documents=[payload],
                provider_id=str(row.get("source") or "research_cache"),
                origin_type="legacy_cache",
                origin_id=origin_id,
            )
            count += len(archived)
        return count

    def repair_news_metadata(
        self,
        *,
        symbols: list[str] | None = None,
        dry_run: bool = True,
    ) -> dict[str, int]:
        """Enrich archived news from the durable provider cache.

        Older provider payloads use fields such as ``published`` instead of
        ``published_at``.  Replaying only news rows is idempotent: the
        content-addressed document is enriched in place and the repair
        observation has a stable origin id.
        """

        requested = {
            str(symbol or "").strip().upper() for symbol in symbols or [] if symbol
        }
        rows = [
            row
            for row in self._research_cache_rows()
            if str(row.get("kind") or "") == "news"
            and (
                not requested
                or str(row.get("symbol") or "").strip().upper() in requested
            )
        ]
        result = {
            "candidates": len(rows),
            "matched_documents": 0,
            "repaired": 0,
            "created": 0,
            "with_link": 0,
            "with_published_at": 0,
        }
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            for key in (
                "title",
                "published_at",
                "source",
                "url",
                "snippet",
                "fetched_at",
            ):
                if key not in payload and row.get(key) is not None:
                    payload[key] = row.get(key)
            published_at = str(
                payload.get("published_at")
                or payload.get("publish_date")
                or payload.get("published")
                or payload.get("publication_date")
                or payload.get("date")
                or ""
            ).strip()
            locator = str(
                payload.get("url")
                or payload.get("link")
                or payload.get("source_url")
                or payload.get("canonical_url")
                or payload.get("article_url")
                or payload.get("web_url")
                or payload.get("href")
                or ""
            ).strip()
            result["with_link"] += int(locator.startswith(("http://", "https://")))
            result["with_published_at"] += int(bool(published_at))
            matched: list[dict[str, Any]] = []
            if locator.startswith(("http://", "https://")):
                with self.store.connect() as conn:
                    matched = [
                        dict(item)
                        for item in conn.execute(
                            """SELECT DISTINCT d.document_ref,d.published_at
                               FROM source_documents d
                               JOIN source_observations o USING(document_ref)
                               WHERE o.subject_key=? AND o.source_kind='news'
                                 AND d.canonical_url=?""",
                            (
                                str(row.get("symbol") or "").strip().upper(),
                                normalize_url(locator),
                            ),
                        ).fetchall()
                    ]
            result["matched_documents"] += len(matched)
            if matched:
                needs_repair = [
                    item for item in matched
                    if published_at and not str(item.get("published_at") or "").strip()
                ]
                result["repaired"] += len(needs_repair)
                if not dry_run and needs_repair:
                    refs = [str(item["document_ref"]) for item in needs_repair]
                    placeholders = ",".join("?" for _ in refs)
                    with self.store.connect() as conn:
                        conn.execute(
                            f"""UPDATE source_documents SET published_at=?
                                WHERE document_ref IN ({placeholders})
                                  AND COALESCE(published_at,'')=''""",
                            (published_at, *refs),
                        )
                continue
            if dry_run:
                continue
            archived = self.ingestion.ingest_provider_documents(
                kind="news",
                symbol=str(row.get("symbol") or ""),
                documents=[payload],
                provider_id=str(row.get("source") or "research_cache"),
                origin_type="news_metadata_repair",
                origin_id=f"research-cache-news:{row.get('id')}",
            )
            result["created"] += len(archived)
        return result

    def _backfill_manifests(self, base_dir: Path, origin_type: str, *, dry_run: bool) -> int:
        paths = sorted(base_dir.glob("*/inputs/data_manifest.json")) if base_dir.exists() else []
        if dry_run:
            return len(paths)
        count = 0
        for path in paths:
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            self.ingestion.ingest_data_manifest(
                manifest,
                origin_type=origin_type,
                origin_id=str(manifest.get("run_id") or path.parents[1].name),
            )
            count += 1
        return count

    def _backfill_research_sessions(self, sessions_dir: Path, *, dry_run: bool) -> int:
        count = 0
        if not sessions_dir.exists():
            return count
        for session_dir in sorted(path for path in sessions_dir.iterdir() if path.is_dir()):
            try:
                session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            config = dict(session.get("config") or {})
            research = dict(config.get("research_session") or {})
            symbol = str(research.get("symbol") or research.get("resolved_symbol") or "").upper()
            if not symbol:
                continue
            message_path = session_dir / "messages.jsonl"
            if not message_path.exists():
                continue
            for line in message_path.read_text(encoding="utf-8").splitlines():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = str(message.get("role") or "")
                content = str(message.get("content") or "")
                message_id = str(message.get("message_id") or "")
                if role not in {"user", "assistant"} or not content or not message_id:
                    continue
                count += 1
                if not dry_run:
                    self.store.index_research_session(
                        session_id=str(session.get("session_id") or session_dir.name),
                        symbol=symbol,
                        role=role,
                        content=content,
                        message_id=message_id,
                    )
        return count

    def run(
        self,
        *,
        dry_run: bool = True,
        reports_dir: Path | None = None,
        daily_runs_dir: Path | None = None,
        weekly_runs_dir: Path | None = None,
        sessions_dir: Path | None = None,
        refresh_active_symbols: list[str] | None = None,
    ) -> dict[str, Any]:
        repo_root = Path(__file__).resolve().parents[3]
        runtime_root = get_runtime_root()
        reports = reports_dir or repo_root / "agent" / "reports"
        daily = daily_runs_dir or runtime_root / "portfolio" / "daily_runs"
        weekly = weekly_runs_dir or runtime_root / "portfolio" / "weekly_runs"
        sessions = sessions_dir or repo_root / "agent" / "sessions"

        result: dict[str, Any] = {
            "dry_run": dry_run,
            "research_cache_documents": self._backfill_research_cache(dry_run=dry_run),
            "news_metadata_repair": self.repair_news_metadata(dry_run=dry_run),
            "daily_manifests": self._backfill_manifests(daily, "daily_run", dry_run=dry_run),
            "weekly_manifests": self._backfill_manifests(weekly, "weekly_run", dry_run=dry_run),
            "research_session_messages": self._backfill_research_sessions(sessions, dry_run=dry_run),
            "structured_financials": self._backfill_structured_financials(dry_run=dry_run),
        }
        if dry_run:
            result["deep_reports"] = len(list(reports.glob("report_*/manifest.json"))) if reports.exists() else 0
            with self.store.connect() as conn:
                result["report_link_candidates"] = int(
                    conn.execute("SELECT COUNT(*) FROM report_knowledge_links").fetchone()[0]
                )
            result["integrity"] = self.store.source_archive_integrity()
            return result

        result["deep_report_backfill"] = self.store.backfill_reports(reports)
        result["report_source_backfill"] = self.store.backfill_report_source_links()
        active = [str(symbol).upper() for symbol in refresh_active_symbols or [] if symbol]
        if active:
            result["official_refresh"] = self.official.refresh_many(active, force=False)
        result["integrity"] = self.store.source_archive_integrity()
        return result


__all__ = ["SourceArchiveBackfill"]
