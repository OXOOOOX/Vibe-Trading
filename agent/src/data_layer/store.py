"""Small durable stores used by the unified data layer.

Market bars remain in ``market_cache.sqlite3``.  This module owns only control
plane state (request handles, health, watchlist) and durable research/news
documents, keeping credential and trading state completely separate.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def data_root() -> Path:
    return Path(os.getenv("VIBE_TRADING_DATA_ROOT", "~/.vibe-trading/data")).expanduser()


class DataControlStore:
    """Persistent request, source-health, and explicit-watchlist state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_root() / "unified_data.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS data_requests (
                    request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_data_requests_fingerprint
                    ON data_requests(fingerprint, created_at DESC);

                CREATE TABLE IF NOT EXISTS data_handles (
                    handle TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manual_watchlist (
                    symbol TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL,
                    note TEXT
                );

                CREATE TABLE IF NOT EXISTS source_health (
                    source TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    circuit_open_until TEXT,
                    last_status TEXT NOT NULL,
                    last_latency_ms REAL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS refresh_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    def start_request(self, request_id: str, fingerprint: str, purpose: str, symbols: list[str]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO data_requests(request_id, fingerprint, purpose, symbols_json, status, created_at) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (request_id, fingerprint, purpose, json.dumps(symbols), utc_now()),
            )

    def finish_request(self, request_id: str, result: dict[str, Any], *, status: str = "completed", error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE data_requests SET status=?, result_json=?, completed_at=?, error=? WHERE request_id=?",
                (status, json.dumps(result, ensure_ascii=False), utc_now(), error, request_id),
            )

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM data_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["symbols"] = json.loads(item.pop("symbols_json"))
        item["result"] = json.loads(item.pop("result_json") or "null")
        return item

    def put_handle(self, handle: str, payload: dict[str, Any], *, ttl_hours: int = 24) -> None:
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO data_handles(handle, payload_json, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (handle, json.dumps(payload), utc_now(), expires_at),
            )

    def get_handle(self, handle: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload_json, expires_at FROM data_handles WHERE handle=?", (handle,)).fetchone()
        if row is None or row["expires_at"] <= utc_now():
            return None
        return json.loads(row["payload_json"])

    def list_watchlist(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM manual_watchlist ORDER BY added_at DESC").fetchall()
        return [dict(row) for row in rows]

    def add_watchlist(self, symbol: str, note: str | None = None) -> dict[str, Any]:
        clean = symbol.strip().upper()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO manual_watchlist(symbol, added_at, note) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET note=excluded.note",
                (clean, utc_now(), note),
            )
        return next(item for item in self.list_watchlist() if item["symbol"] == clean)

    def remove_watchlist(self, symbol: str) -> bool:
        with self.connect() as conn:
            return bool(conn.execute("DELETE FROM manual_watchlist WHERE symbol=?", (symbol.strip().upper(),)).rowcount)

    def record_source(self, source: str, *, succeeded: bool, latency_ms: float | None = None, error: str | None = None) -> None:
        now = utc_now()
        with self.connect() as conn:
            current = conn.execute("SELECT consecutive_failures FROM source_health WHERE source=?", (source,)).fetchone()
            failures = 0 if succeeded else int(current["consecutive_failures"] if current else 0) + 1
            # Three transport failures open a five-minute circuit. A later success closes it.
            circuit = None
            if failures >= 3:
                circuit = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            conn.execute(
                "INSERT INTO source_health(source, consecutive_failures, circuit_open_until, last_status, last_latency_ms, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
                "consecutive_failures=excluded.consecutive_failures, circuit_open_until=excluded.circuit_open_until, "
                "last_status=excluded.last_status, last_latency_ms=excluded.last_latency_ms, "
                "last_error=excluded.last_error, updated_at=excluded.updated_at",
                (source, failures, circuit, "ok" if succeeded else "failed", latency_ms, error, now),
            )

    def source_health(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()
        now = utc_now()
        records = []
        for row in rows:
            item = dict(row)
            item["circuit_open"] = bool(item["circuit_open_until"] and item["circuit_open_until"] > now)
            records.append(item)
        return records

    def log_event(self, request_id: str, kind: str, status: str, message: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO refresh_events(request_id, kind, status, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (request_id, kind, status, message, utc_now()),
            )

    def prune(self) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("DELETE FROM data_handles WHERE expires_at <= ?", (now,))
            conn.execute("DELETE FROM data_requests WHERE completed_at IS NOT NULL AND completed_at < ?", ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),))


class ResearchCacheStore:
    """Durable research/news cache with a local FTS index."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.getenv("VIBE_TRADING_RESEARCH_CACHE_DB", "~/.vibe-trading/cache/research_cache.sqlite3")).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    title TEXT NOT NULL,
                    published_at TEXT NOT NULL DEFAULT '',
                    source TEXT,
                    url TEXT NOT NULL DEFAULT '',
                    snippet TEXT,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    is_live_current INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(kind, symbol, title, published_at, url)
                );
                CREATE INDEX IF NOT EXISTS idx_research_documents_lookup
                    ON research_documents(kind, symbol, fetched_at DESC);
                CREATE VIRTUAL TABLE IF NOT EXISTS research_documents_fts
                    USING fts5(title, snippet, symbol UNINDEXED, kind UNINDEXED, document_id UNINDEXED);
                """
            )

    def replace_documents(self, kind: str, symbol: str, documents: list[dict[str, Any]], *, source: str | None = None) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("UPDATE research_documents SET is_live_current=0 WHERE kind=? AND symbol=?", (kind, symbol))
            for record in documents:
                title = str(record.get("title") or record.get("name") or "Untitled").strip()
                published_at = str(record.get("published_at") or record.get("publish_date") or record.get("date") or "")
                url = str(record.get("url") or record.get("link") or "")
                snippet = record.get("snippet") or record.get("summary") or record.get("content") or ""
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO research_documents(kind, symbol, title, published_at, source, url, snippet, payload_json, fetched_at, is_live_current) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (kind, symbol, title, published_at, source, url, str(snippet), json.dumps(record, ensure_ascii=False), now),
                )
                row = conn.execute(
                    "SELECT id FROM research_documents WHERE kind=? AND symbol=? AND title=? AND published_at=? AND url=?",
                    (kind, symbol, title, published_at, url),
                ).fetchone()
                if row:
                    conn.execute("DELETE FROM research_documents_fts WHERE document_id=?", (str(row["id"]),))
                    conn.execute(
                        "INSERT INTO research_documents_fts(title, snippet, symbol, kind, document_id) VALUES (?, ?, ?, ?, ?)",
                        (title, str(snippet), symbol, kind, str(row["id"])),
                    )
                    if cursor.rowcount == 0:
                        conn.execute("UPDATE research_documents SET fetched_at=?, is_live_current=1, source=COALESCE(?, source), snippet=? WHERE id=?", (now, source, str(snippet), row["id"]))

    def latest(self, kind: str, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM research_documents WHERE kind=? AND symbol=? ORDER BY fetched_at DESC, published_at DESC LIMIT ?",
                (kind, symbol, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["is_live_current"] = bool(item["is_live_current"])
            result.append(item)
        return result

    def prune(self) -> None:
        now = datetime.now(timezone.utc)
        news_cutoff = (now - timedelta(days=730)).isoformat()
        news_body_cutoff = (now - timedelta(days=180)).isoformat()
        report_body_cutoff = (now - timedelta(days=365 * 5)).isoformat()
        with self.connect() as conn:
            # Keep metadata through the requested horizon; discard expired news records.
            expired = conn.execute("SELECT id FROM research_documents WHERE kind='news' AND fetched_at < ?", (news_cutoff,)).fetchall()
            for row in expired:
                conn.execute("DELETE FROM research_documents_fts WHERE document_id=?", (str(row["id"]),))
            conn.execute("DELETE FROM research_documents WHERE kind='news' AND fetched_at < ?", (news_cutoff,))
            self._strip_expired_bodies(conn, "news", news_body_cutoff)
            self._strip_expired_bodies(conn, "report", report_body_cutoff)

    @staticmethod
    def _strip_expired_bodies(conn: sqlite3.Connection, kind: str, cutoff: str) -> None:
        """Keep searchable metadata while removing expired cached document bodies."""
        rows = conn.execute(
            "SELECT id, payload_json FROM research_documents WHERE kind=? AND fetched_at < ?",
            (kind, cutoff),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                for field in ("content", "body", "html", "snippet", "summary", "text"):
                    payload.pop(field, None)
            conn.execute(
                "UPDATE research_documents SET snippet='', payload_json=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), row["id"]),
            )
