"""Persistent background jobs for historical annual-report backfills."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {
    "completed",
    "completed_with_gaps",
    "failed",
    "cancelled",
    "interrupted",
}
PHASES = ("discovery", "download", "parsing", "validation")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe_key(symbol: str, years: list[int], force: bool) -> str:
    encoded = json.dumps(
        [symbol, sorted(years, reverse=True), bool(force)],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _year_record(year: int) -> dict[str, Any]:
    return {
        "year": int(year),
        "status": "pending",
        "current_stage": "pending",
        "message": "等待处理",
        "provider_id": None,
        "document_ref": None,
        "error": None,
        "updated_at": None,
        "phases": {phase: "pending" for phase in PHASES},
    }


class AnnualReportBackfillJobStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(
            os.getenv(
                "VIBE_TRADING_ANNUAL_REPORT_JOB_DB",
                "~/.vibe-trading/cache/annual_report_jobs.sqlite3",
            )
        ).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()
        self._mark_abandoned_jobs_interrupted()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _init_schema(self) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS annual_report_backfill_jobs (
                       job_id TEXT PRIMARY KEY,
                       symbol TEXT NOT NULL,
                       dedupe_key TEXT NOT NULL,
                       status TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       record_json TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_annual_report_jobs_symbol_created
                   ON annual_report_backfill_jobs(symbol, created_at DESC)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_annual_report_jobs_dedupe_status
                   ON annual_report_backfill_jobs(dedupe_key, status)"""
            )

    def _mark_abandoned_jobs_interrupted(self) -> None:
        now = _utc_now()
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """SELECT job_id,record_json FROM annual_report_backfill_jobs
                   WHERE status IN ('queued','running')"""
            ).fetchall()
            for row in rows:
                record = json.loads(row["record_json"])
                record.update({
                    "status": "interrupted",
                    "stage": "interrupted",
                    "message": "服务重启导致任务中断，可重新发起补齐。",
                    "error": "service_restarted",
                    "completed_at": now,
                    "updated_at": now,
                })
                connection.execute(
                    """UPDATE annual_report_backfill_jobs
                       SET status='interrupted',updated_at=?,record_json=? WHERE job_id=?""",
                    (now, json.dumps(record, ensure_ascii=False, sort_keys=True), row["job_id"]),
                )

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self.connect() as connection:
            connection.execute(
                """INSERT INTO annual_report_backfill_jobs(
                       job_id,symbol,dedupe_key,status,created_at,updated_at,record_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    record["job_id"],
                    record["symbol"],
                    record["dedupe_key"],
                    record["status"],
                    record["created_at"],
                    record["updated_at"],
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                ),
            )
        return record

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM annual_report_backfill_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return json.loads(row["record_json"]) if row else None

    def latest(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT record_json FROM annual_report_backfill_jobs
                   WHERE symbol=? ORDER BY created_at DESC LIMIT 1""",
                (symbol.upper(),),
            ).fetchone()
        return json.loads(row["record_json"]) if row else None

    def active_for_dedupe(self, dedupe_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT record_json FROM annual_report_backfill_jobs
                   WHERE dedupe_key=? AND status IN ('queued','running')
                   ORDER BY created_at DESC LIMIT 1""",
                (dedupe_key,),
            ).fetchone()
        return json.loads(row["record_json"]) if row else None

    def update(self, job_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM annual_report_backfill_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            record = json.loads(row["record_json"])
            mutate(record)
            record["updated_at"] = _utc_now()
            connection.execute(
                """UPDATE annual_report_backfill_jobs
                   SET status=?,updated_at=?,record_json=? WHERE job_id=?""",
                (
                    record["status"],
                    record["updated_at"],
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                    job_id,
                ),
            )
        return record


class AnnualReportBackfillJobService:
    def __init__(self, store: AnnualReportBackfillJobStore | None = None) -> None:
        self.store = store or AnnualReportBackfillJobStore()

    def create_job(
        self,
        *,
        symbol: str,
        years: list[int],
        force: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        normalized = str(symbol or "").strip().upper()
        requested = sorted({int(year) for year in years}, reverse=True)
        current_year = datetime.now(timezone.utc).year
        if not normalized:
            raise ValueError("symbol is required")
        if not requested or len(requested) > 12:
            raise ValueError("between 1 and 12 annual report years are required")
        if any(year < 1990 or year >= current_year for year in requested):
            raise ValueError("annual report years must be between 1990 and the last complete year")
        dedupe_key = _dedupe_key(normalized, requested, force)
        active = self.store.active_for_dedupe(dedupe_key)
        if active is not None:
            return active, True
        now = _utc_now()
        record = {
            "schema_version": 1,
            "job_id": f"annual_backfill_{uuid.uuid4().hex[:20]}",
            "dedupe_key": dedupe_key,
            "symbol": normalized,
            "years": requested,
            "force": bool(force),
            "status": "queued",
            "stage": "queued",
            "message": "任务已进入后台队列",
            "progress_pct": 0,
            "year_progress": [_year_record(year) for year in requested],
            "result": None,
            "error": None,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
        }
        return self.store.create(record), False

    @staticmethod
    def _progress_pct(year_progress: list[dict[str, Any]]) -> int:
        if not year_progress:
            return 100
        phase_values = {"pending": 0.0, "running": 0.5, "completed": 1.0, "reused": 1.0, "failed": 1.0}
        completed = 0.0
        total = float(len(year_progress) * len(PHASES))
        for item in year_progress:
            phases = dict(item.get("phases") or {})
            completed += sum(phase_values.get(str(phases.get(phase)), 0.0) for phase in PHASES)
        return max(0, min(100, round(completed / total * 100)))

    def _apply_progress(self, job_id: str, event: dict[str, Any]) -> None:
        year = event.get("year")
        stage = str(event.get("stage") or "running")

        def mutate(record: dict[str, Any]) -> None:
            record["stage"] = stage
            record["message"] = str(event.get("message") or record.get("message") or "")
            if year is not None:
                item = next(
                    (value for value in record["year_progress"] if int(value["year"]) == int(year)),
                    None,
                )
                if item is not None:
                    phases = item["phases"]
                    if stage == "discovering":
                        phases["discovery"] = "running"
                        item["status"] = "running"
                    elif stage == "discovered":
                        phases["discovery"] = "completed"
                    elif stage == "downloading":
                        phases["discovery"] = "completed"
                        phases["download"] = "running"
                    elif stage == "parsing":
                        phases["download"] = "completed"
                        phases["parsing"] = "running"
                    elif stage == "validating":
                        phases["parsing"] = "completed"
                        phases["validation"] = "running"
                    elif stage in {"completed", "needs_review"}:
                        for phase in PHASES:
                            phases[phase] = "completed"
                        item["status"] = stage
                    elif stage == "reused":
                        for phase in PHASES:
                            phases[phase] = "reused"
                        item["status"] = "reused"
                    elif stage == "failed":
                        running_phase = next(
                            (phase for phase in PHASES if phases.get(phase) == "running"),
                            "discovery",
                        )
                        phases[running_phase] = "failed"
                        item["status"] = "failed"
                    item["current_stage"] = stage
                    item["message"] = str(event.get("message") or item.get("message") or "")
                    item["provider_id"] = event.get("provider_id") or item.get("provider_id")
                    item["document_ref"] = event.get("document_ref") or item.get("document_ref")
                    item["error"] = event.get("error") or item.get("error")
                    item["updated_at"] = _utc_now()
            record["progress_pct"] = self._progress_pct(record["year_progress"])

        self.store.update(job_id, mutate)

    @staticmethod
    def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
        structured = dict(result.get("structured") or {})
        return {
            "symbol": result.get("symbol"),
            "status": result.get("status"),
            "refreshed": int(result.get("refreshed") or 0),
            "failed": int(result.get("failed") or 0),
            "document_refs": list(result.get("document_refs") or []),
            "relevance_downgraded": int(result.get("relevance_downgraded") or 0),
            "coverage": dict(result.get("coverage") or {}),
            "structured": {
                "documents": int(structured.get("documents") or 0),
                "validated": int(structured.get("validated") or 0),
                "needs_review": int(structured.get("needs_review") or 0),
                "metrics": int(structured.get("metrics") or 0),
            },
        }

    def run_job(self, job_id: str, official_service: Any) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in {"queued", "interrupted"}:
            return job

        started_at = _utc_now()

        def mark_running(record: dict[str, Any]) -> None:
            record.update({
                "status": "running",
                "stage": "preparing",
                "message": "正在检查已有年报覆盖",
                "started_at": started_at,
                "completed_at": None,
                "error": None,
            })

        self.store.update(job_id, mark_running)
        try:
            result = official_service.backfill_annual_reports(
                job["symbol"],
                years=job["years"],
                force=bool(job.get("force")),
                progress_callback=lambda event: self._apply_progress(job_id, event),
            )
            compact = self._compact_result(dict(result or {}))
            terminal_status = (
                "completed" if not (compact.get("coverage") or {}).get("missing_years")
                else "completed_with_gaps"
            )

            def mark_completed(record: dict[str, Any]) -> None:
                record.update({
                    "status": terminal_status,
                    "stage": "completed",
                    "message": (
                        "历史年报补齐完成"
                        if terminal_status == "completed"
                        else "任务完成，仍有未取得的报告年度"
                    ),
                    "progress_pct": 100,
                    "result": compact,
                    "completed_at": _utc_now(),
                    "error": None,
                })

            return self.store.update(job_id, mark_completed)
        except Exception as exc:
            def mark_failed(record: dict[str, Any]) -> None:
                record.update({
                    "status": "failed",
                    "stage": "failed",
                    "message": "历史年报后台任务执行失败",
                    "completed_at": _utc_now(),
                    "error": str(exc),
                })

            return self.store.update(job_id, mark_failed)


_DEFAULT_SERVICE: AnnualReportBackfillJobService | None = None


def get_annual_report_backfill_job_service() -> AnnualReportBackfillJobService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = AnnualReportBackfillJobService()
    return _DEFAULT_SERVICE


__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "AnnualReportBackfillJobService",
    "AnnualReportBackfillJobStore",
    "get_annual_report_backfill_job_service",
]
