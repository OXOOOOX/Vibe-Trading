"""SQLite-backed usage event ledger and aggregations."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.usage.normalization import TOKEN_FIELDS
from src.usage.pricing import aggregate_llm_costs, estimate_llm_cost

_DEFAULT_DB_PATH = Path.home() / ".vibe-trading" / "sessions.db"
_TERMINAL_STATUSES = {"ok", "error", "cancelled"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class UsageStore:
    """Durable, process-safe event ledger sharing ``sessions.db`` with goals."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or _DEFAULT_DB_PATH).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=15, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_scopes (
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    recording_started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope_type, scope_id)
                );

                CREATE TABLE IF NOT EXISTS usage_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    session_id TEXT,
                    attempt_id TEXT,
                    parent_tool_call_id TEXT,
                    kind TEXT NOT NULL CHECK(kind IN ('llm_call', 'tool_call', 'resource_call')),
                    category TEXT,
                    provider TEXT,
                    model TEXT,
                    tool_name TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    elapsed_ms INTEGER,
                    cache_mode TEXT,
                    network_request INTEGER NOT NULL DEFAULT 0,
                    cache_access INTEGER NOT NULL DEFAULT 0,
                    query_summary TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    cache_read_input_tokens INTEGER,
                    cache_write_input_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (scope_type, scope_id)
                        REFERENCES usage_scopes(scope_type, scope_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS usage_scope_links (
                    parent_scope_type TEXT NOT NULL,
                    parent_scope_id TEXT NOT NULL,
                    child_scope_type TEXT NOT NULL,
                    child_scope_id TEXT NOT NULL,
                    child_attempt_id TEXT NOT NULL DEFAULT '',
                    relationship TEXT NOT NULL DEFAULT 'child',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (
                        parent_scope_type, parent_scope_id,
                        child_scope_type, child_scope_id, child_attempt_id
                    ),
                    FOREIGN KEY (parent_scope_type, parent_scope_id)
                        REFERENCES usage_scopes(scope_type, scope_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (child_scope_type, child_scope_id)
                        REFERENCES usage_scopes(scope_type, scope_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_usage_events_scope_time
                    ON usage_events(scope_type, scope_id, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_events_scope_attempt_time
                    ON usage_events(scope_type, scope_id, attempt_id, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_events_scope_kind
                    ON usage_events(scope_type, scope_id, kind, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_events_type_started
                    ON usage_events(scope_type, started_at DESC, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_scope_links_child
                    ON usage_scope_links(child_scope_type, child_scope_id);
                """
            )
            link_info = connection.execute(
                "PRAGMA table_info(usage_scope_links)"
            ).fetchall()
            link_columns = {str(row[1]) for row in link_info}
            link_primary_key = [
                str(row[1])
                for row in sorted(link_info, key=lambda item: int(item[5] or 0))
                if int(row[5] or 0) > 0
            ]
            expected_primary_key = [
                "parent_scope_type",
                "parent_scope_id",
                "child_scope_type",
                "child_scope_id",
                "child_attempt_id",
            ]
            if (
                "child_attempt_id" not in link_columns
                or link_primary_key != expected_primary_key
            ):
                attempt_expression = (
                    "COALESCE(child_attempt_id, '')"
                    if "child_attempt_id" in link_columns
                    else "''"
                )
                connection.execute(
                    "ALTER TABLE usage_scope_links RENAME TO usage_scope_links_legacy"
                )
                connection.execute(
                    """CREATE TABLE usage_scope_links (
                        parent_scope_type TEXT NOT NULL,
                        parent_scope_id TEXT NOT NULL,
                        child_scope_type TEXT NOT NULL,
                        child_scope_id TEXT NOT NULL,
                        child_attempt_id TEXT NOT NULL DEFAULT '',
                        relationship TEXT NOT NULL DEFAULT 'child',
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (
                            parent_scope_type, parent_scope_id,
                            child_scope_type, child_scope_id, child_attempt_id
                        ),
                        FOREIGN KEY (parent_scope_type, parent_scope_id)
                            REFERENCES usage_scopes(scope_type, scope_id)
                            ON DELETE CASCADE,
                        FOREIGN KEY (child_scope_type, child_scope_id)
                            REFERENCES usage_scopes(scope_type, scope_id)
                            ON DELETE CASCADE
                    )"""
                )
                connection.execute(
                    f"""INSERT OR IGNORE INTO usage_scope_links (
                            parent_scope_type, parent_scope_id,
                            child_scope_type, child_scope_id, child_attempt_id,
                            relationship, created_at
                        )
                        SELECT parent_scope_type, parent_scope_id,
                               child_scope_type, child_scope_id, {attempt_expression},
                               relationship, created_at
                        FROM usage_scope_links_legacy"""
                )
                connection.execute("DROP TABLE usage_scope_links_legacy")
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS idx_usage_scope_links_child
                       ON usage_scope_links(child_scope_type, child_scope_id)"""
                )

    def start_scope(self, scope_type: str, scope_id: str) -> int:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO usage_scopes
                   (scope_type, scope_id, revision, recording_started_at, updated_at)
                   VALUES (?, ?, 0, ?, ?)""",
                (scope_type, scope_id, now, now),
            )
            row = connection.execute(
                "SELECT revision FROM usage_scopes WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            ).fetchone()
        return int(row["revision"] if row else 0)

    def delete_scope(self, scope_type: str, scope_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM usage_scopes WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            )

    def link_scope(
        self,
        parent_scope_type: str,
        parent_scope_id: str,
        child_scope_type: str,
        child_scope_id: str,
        *,
        relationship: str = "child",
        child_attempt_id: str | None = None,
    ) -> bool:
        """Link a child ledger without duplicating its events.

        Monitoring Deep Reports keep their normal session ledger while the
        originating monitor job dynamically includes the same events.
        """

        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for scope_type, scope_id in (
                (parent_scope_type, parent_scope_id),
                (child_scope_type, child_scope_id),
            ):
                connection.execute(
                    """INSERT OR IGNORE INTO usage_scopes
                       (scope_type, scope_id, revision, recording_started_at, updated_at)
                       VALUES (?, ?, 0, ?, ?)""",
                    (scope_type, scope_id, now, now),
                )
            inserted = connection.execute(
                """INSERT OR IGNORE INTO usage_scope_links
                   (parent_scope_type, parent_scope_id, child_scope_type,
                    child_scope_id, child_attempt_id, relationship, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    parent_scope_type,
                    parent_scope_id,
                    child_scope_type,
                    child_scope_id,
                    str(child_attempt_id or ""),
                    relationship,
                    now,
                ),
            ).rowcount
            if inserted:
                connection.execute(
                    """UPDATE usage_scopes
                       SET revision = revision + 1, updated_at = ?
                       WHERE scope_type = ? AND scope_id = ?""",
                    (now, parent_scope_type, parent_scope_id),
                )
            connection.commit()
        return bool(inserted)

    def upsert_event(self, event: dict[str, Any]) -> tuple[int, bool]:
        """Insert/update one event and return ``(revision, changed)``."""

        now = _utc_now()
        payload = dict(event)
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        payload.setdefault("started_at", now)
        payload.setdefault("status", "ok")
        payload["metadata_json"] = json.dumps(
            payload.pop("metadata", {}) or {}, ensure_ascii=False, sort_keys=True, default=str
        )
        columns = (
            "event_id", "scope_type", "scope_id", "session_id", "attempt_id",
            "parent_tool_call_id", "kind", "category", "provider", "model",
            "tool_name", "status", "started_at", "completed_at", "elapsed_ms",
            "cache_mode", "network_request", "cache_access", "query_summary",
            *TOKEN_FIELDS, "metadata_json", "created_at", "updated_at",
        )
        payload["network_request"] = int(bool(payload.get("network_request")))
        payload["cache_access"] = int(bool(payload.get("cache_access")))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO usage_scopes
                   (scope_type, scope_id, revision, recording_started_at, updated_at)
                   VALUES (?, ?, 0, ?, ?)""",
                (payload["scope_type"], payload["scope_id"], now, now),
            )
            existing = connection.execute(
                "SELECT * FROM usage_events WHERE event_id = ?", (payload["event_id"],)
            ).fetchone()
            if existing is not None:
                payload["created_at"] = existing["created_at"]
                if existing["status"] in _TERMINAL_STATUSES and payload["status"] == "running":
                    revision = connection.execute(
                        "SELECT revision FROM usage_scopes WHERE scope_type = ? AND scope_id = ?",
                        (payload["scope_type"], payload["scope_id"]),
                    ).fetchone()["revision"]
                    connection.commit()
                    return int(revision), False
                comparable = [
                    column
                    for column in columns
                    if column not in {"updated_at", "started_at", "completed_at"}
                ]
                if all(existing[column] == payload.get(column) for column in comparable):
                    revision = connection.execute(
                        "SELECT revision FROM usage_scopes WHERE scope_type = ? AND scope_id = ?",
                        (payload["scope_type"], payload["scope_id"]),
                    ).fetchone()["revision"]
                    connection.commit()
                    return int(revision), False

            values = [payload.get(column) for column in columns]
            placeholders = ", ".join("?" for _ in columns)
            assignments = ", ".join(
                f"{column}=excluded.{column}"
                for column in columns
                if column not in {"event_id", "created_at"}
            )
            connection.execute(
                f"""INSERT INTO usage_events ({', '.join(columns)})
                    VALUES ({placeholders})
                    ON CONFLICT(event_id) DO UPDATE SET {assignments}""",
                values,
            )
            connection.execute(
                """UPDATE usage_scopes
                   SET revision = revision + 1, updated_at = ?
                   WHERE scope_type = ? AND scope_id = ?""",
                (now, payload["scope_type"], payload["scope_id"]),
            )
            connection.execute(
                """UPDATE usage_scopes
                   SET revision = revision + 1, updated_at = ?
                   WHERE EXISTS (
                       SELECT 1 FROM usage_scope_links AS link
                       WHERE link.parent_scope_type = usage_scopes.scope_type
                         AND link.parent_scope_id = usage_scopes.scope_id
                         AND link.child_scope_type = ?
                         AND link.child_scope_id = ?
                         AND (
                             link.child_attempt_id = ''
                             OR link.child_attempt_id = COALESCE(?, '')
                         )
                   )""",
                (
                    now,
                    payload["scope_type"],
                    payload["scope_id"],
                    payload.get("attempt_id"),
                ),
            )
            revision = connection.execute(
                "SELECT revision FROM usage_scopes WHERE scope_type = ? AND scope_id = ?",
                (payload["scope_type"], payload["scope_id"]),
            ).fetchone()["revision"]
            connection.commit()
        return int(revision), True

    def get_summary(
        self,
        scope_type: str,
        scope_id: str,
        *,
        current_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            scope = connection.execute(
                "SELECT * FROM usage_scopes WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            ).fetchone()
            if scope is None:
                empty = self._empty_aggregate(unreported=True)
                return {
                    "recording_status": "unrecorded",
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "revision": 0,
                    "recording_started_at": None,
                    "current_attempt_id": current_attempt_id,
                    "session": empty,
                    "current_attempt": empty,
                    "direct": empty,
                    "linked_scopes": [],
                }
            direct_rows = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM usage_events
                       WHERE scope_type = ? AND scope_id = ? ORDER BY sequence ASC""",
                    (scope_type, scope_id),
                ).fetchall()
            ]
            linked_scopes = [
                {
                    "scope_type": str(row["child_scope_type"]),
                    "scope_id": str(row["child_scope_id"]),
                    "attempt_id": str(row["child_attempt_id"] or "") or None,
                    "relationship": str(row["relationship"]),
                }
                for row in connection.execute(
                    """SELECT child_scope_type, child_scope_id, child_attempt_id, relationship
                       FROM usage_scope_links
                       WHERE parent_scope_type = ? AND parent_scope_id = ?
                       ORDER BY created_at ASC""",
                    (scope_type, scope_id),
                ).fetchall()
            ]
            linked_rows: list[dict[str, Any]] = []
            if linked_scopes:
                predicates = " OR ".join(
                    """(scope_type = ? AND scope_id = ?
                         AND (? = '' OR attempt_id = ?))"""
                    for _ in linked_scopes
                )
                parameters = [
                    value
                    for linked in linked_scopes
                    for value in (
                        linked["scope_type"],
                        linked["scope_id"],
                        linked["attempt_id"] or "",
                        linked["attempt_id"] or "",
                    )
                ]
                linked_rows = [
                    dict(row)
                    for row in connection.execute(
                        f"""SELECT * FROM usage_events
                            WHERE {predicates} ORDER BY sequence ASC""",
                        parameters,
                    ).fetchall()
                ]

        rows = [*direct_rows, *linked_rows]
        current_rows = [row for row in rows if current_attempt_id and row.get("attempt_id") == current_attempt_id]
        return {
            "recording_status": "recording",
            "scope_type": scope_type,
            "scope_id": scope_id,
            "revision": int(scope["revision"]),
            "recording_started_at": scope["recording_started_at"],
            "current_attempt_id": current_attempt_id,
            "session": self._aggregate(rows),
            "current_attempt": self._aggregate(current_rows),
            "direct": self._aggregate(direct_rows),
            "linked_scopes": linked_scopes,
        }

    def list_events(
        self,
        scope_type: str,
        scope_id: str,
        *,
        kind: str | None = None,
        category: str | None = None,
        attempt_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            scope = connection.execute(
                "SELECT revision FROM usage_scopes WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            ).fetchone()
            linked = connection.execute(
                """SELECT child_scope_type, child_scope_id, child_attempt_id
                   FROM usage_scope_links
                   WHERE parent_scope_type = ? AND parent_scope_id = ?""",
                (scope_type, scope_id),
            ).fetchall()

        scope_pairs = [(scope_type, scope_id, ""), *[
            (
                str(row["child_scope_type"]),
                str(row["child_scope_id"]),
                str(row["child_attempt_id"] or ""),
            )
            for row in linked
        ]]
        clauses = [
            "(" + " OR ".join(
                "(scope_type = ? AND scope_id = ? AND (? = '' OR attempt_id = ?))"
                for _ in scope_pairs
            ) + ")"
        ]
        params: list[Any] = [
            value
            for pair in scope_pairs
            for value in (pair[0], pair[1], pair[2], pair[2])
        ]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if attempt_id:
            clauses.append("attempt_id = ?")
            params.append(attempt_id)
        if cursor:
            try:
                cursor_value = int(cursor)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid usage cursor") from exc
            clauses.append("sequence < ?")
            params.append(cursor_value)

        fetch_limit = max(1, min(int(limit), 100)) + 1
        params.append(fetch_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM usage_events WHERE {' AND '.join(clauses)}
                    ORDER BY sequence DESC LIMIT ?""",
                params,
            ).fetchall()

        has_more = len(rows) >= fetch_limit
        visible = rows[: fetch_limit - 1]
        items = [self._event_item(row) for row in visible]
        return {
            "recording_status": "recording" if scope else "unrecorded",
            "revision": int(scope["revision"]) if scope else 0,
            "items": items,
            "next_cursor": str(visible[-1]["sequence"]) if has_more and visible else None,
        }

    def get_type_summary(
        self,
        scope_type: str,
        *,
        started_at: str,
        completed_at: str,
        scope_limit: int = 20,
    ) -> dict[str, Any]:
        """Aggregate one scope type and all linked child ledgers in a time range."""

        with self._connect() as connection:
            scopes = connection.execute(
                """SELECT * FROM usage_scopes
                   WHERE scope_type = ?
                     AND recording_started_at <= ?
                     AND updated_at >= ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (scope_type, completed_at, started_at, max(1, min(scope_limit, 100))),
            ).fetchall()
            rows = connection.execute(
                """SELECT DISTINCT event.* FROM usage_events AS event
                   WHERE event.started_at >= ? AND event.started_at <= ?
                     AND (
                         event.scope_type = ?
                         OR EXISTS (
                             SELECT 1 FROM usage_scope_links AS link
                             WHERE link.parent_scope_type = ?
                               AND link.child_scope_type = event.scope_type
                               AND link.child_scope_id = event.scope_id
                               AND (
                                   link.child_attempt_id = ''
                                   OR link.child_attempt_id = COALESCE(event.attempt_id, '')
                               )
                         )
                     )
                   ORDER BY event.sequence ASC""",
                (started_at, completed_at, scope_type, scope_type),
            ).fetchall()
            all_scope_stats = connection.execute(
                """SELECT COUNT(*) AS scope_count,
                          COALESCE(SUM(revision), 0) AS revision,
                          MIN(recording_started_at) AS recording_started_at
                   FROM usage_scopes
                   WHERE scope_type = ?
                     AND recording_started_at <= ?
                     AND updated_at >= ?""",
                (scope_type, completed_at, started_at),
            ).fetchone()
            linked_scope_count = connection.execute(
                """SELECT COUNT(DISTINCT child_scope_type || ':' || child_scope_id
                                              || ':' || child_attempt_id)
                   FROM usage_scope_links AS link
                   WHERE link.parent_scope_type = ?
                     AND link.created_at <= ?
                     AND EXISTS (
                         SELECT 1 FROM usage_scopes AS parent
                         WHERE parent.scope_type = link.parent_scope_type
                           AND parent.scope_id = link.parent_scope_id
                           AND parent.recording_started_at <= ?
                           AND parent.updated_at >= ?
                     )""",
                (scope_type, completed_at, completed_at, started_at),
            ).fetchone()[0]

        event_rows = [dict(row) for row in rows]
        return {
            "recording_status": "recording" if int(all_scope_stats["scope_count"] or 0) else "unrecorded",
            "scope_type": scope_type,
            "scope_id": f"{scope_type}:{started_at}:{completed_at}",
            "revision": int(all_scope_stats["revision"] or 0),
            "recording_started_at": all_scope_stats["recording_started_at"],
            "current_attempt_id": None,
            "session": self._aggregate(event_rows),
            "current_attempt": self._empty_aggregate(),
            "started_at": started_at,
            "completed_at": completed_at,
            "scope_count": int(all_scope_stats["scope_count"] or 0),
            "linked_scope_count": int(linked_scope_count or 0),
            "recent_scopes": [
                {
                    "scope_id": str(row["scope_id"]),
                    "revision": int(row["revision"]),
                    "recording_started_at": row["recording_started_at"],
                    "updated_at": row["updated_at"],
                }
                for row in scopes
            ],
        }

    def list_type_events(
        self,
        scope_type: str,
        *,
        started_at: str,
        completed_at: str,
        kind: str | None = None,
        category: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        clauses = [
            "event.started_at >= ?",
            "event.started_at <= ?",
            """(event.scope_type = ? OR EXISTS (
                SELECT 1 FROM usage_scope_links AS link
                WHERE link.parent_scope_type = ?
                  AND link.child_scope_type = event.scope_type
                  AND link.child_scope_id = event.scope_id
                  AND (
                      link.child_attempt_id = ''
                      OR link.child_attempt_id = COALESCE(event.attempt_id, '')
                  )
            ))""",
        ]
        params: list[Any] = [started_at, completed_at, scope_type, scope_type]
        if kind:
            clauses.append("event.kind = ?")
            params.append(kind)
        if category:
            clauses.append("event.category = ?")
            params.append(category)
        if cursor:
            try:
                cursor_value = int(cursor)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid usage cursor") from exc
            clauses.append("event.sequence < ?")
            params.append(cursor_value)

        fetch_limit = max(1, min(int(limit), 100)) + 1
        params.append(fetch_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT event.* FROM usage_events AS event
                    WHERE {' AND '.join(clauses)}
                    ORDER BY event.sequence DESC LIMIT ?""",
                params,
            ).fetchall()
            scope_stats = connection.execute(
                """SELECT COUNT(*) AS scope_count,
                          COALESCE(SUM(revision), 0) AS revision
                   FROM usage_scopes
                   WHERE scope_type = ?
                     AND recording_started_at <= ?
                     AND updated_at >= ?""",
                (scope_type, completed_at, started_at),
            ).fetchone()
        has_more = len(rows) >= fetch_limit
        visible = rows[: fetch_limit - 1]
        return {
            "recording_status": "recording" if int(scope_stats["scope_count"] or 0) else "unrecorded",
            "revision": int(scope_stats["revision"] or 0),
            "items": [self._event_item(row) for row in visible],
            "next_cursor": str(visible[-1]["sequence"]) if has_more and visible else None,
        }

    @staticmethod
    def _event_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["network_request"] = bool(item.get("network_request"))
        item["cache_access"] = bool(item.get("cache_access"))
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        item["cost"] = estimate_llm_cost(item)
        return item

    @classmethod
    def _empty_aggregate(cls, *, unreported: bool = False) -> dict[str, Any]:
        tokens = {field: (None if unreported else 0) for field in TOKEN_FIELDS}
        tokens.update(
            {
                "cache_hit_rate": None,
                "coverage": "unreported" if unreported else "not_applicable",
                "reported_calls": 0,
                "total_calls": 0,
                "unreported_calls": 0,
            }
        )
        return {
            "tokens": tokens,
            "cost": aggregate_llm_costs([]),
            "calls": {
                "llm_calls": 0,
                "agent_tools": 0,
                "external_requests": 0,
                "cache_accesses": 0,
                "failures": 0,
                "running": 0,
            },
            "models": [],
            "tools": [],
            "categories": [],
            "providers": [],
        }

    @classmethod
    def _aggregate(cls, rows: list[dict[str, Any]]) -> dict[str, Any]:
        llm_rows = [row for row in rows if row.get("kind") == "llm_call"]
        if not rows:
            return cls._empty_aggregate()

        tokens: dict[str, Any] = {}
        coverage_by_field: dict[str, str] = {}
        for field in TOKEN_FIELDS:
            values = [row[field] for row in llm_rows if row.get(field) is not None]
            if not llm_rows:
                tokens[field] = 0
                coverage_by_field[field] = "not_applicable"
            elif not values:
                tokens[field] = None
                coverage_by_field[field] = "unreported"
            else:
                tokens[field] = sum(int(value) for value in values)
                coverage_by_field[field] = "complete" if len(values) == len(llm_rows) else "partial"

        required_coverage = [coverage_by_field[field] for field in ("input_tokens", "output_tokens", "total_tokens")]
        if not llm_rows:
            overall_coverage = "not_applicable"
        elif all(value == "complete" for value in required_coverage):
            overall_coverage = "complete"
        elif all(value == "unreported" for value in required_coverage):
            overall_coverage = "unreported"
        else:
            overall_coverage = "partial"

        input_tokens = tokens.get("input_tokens")
        cached_tokens = tokens.get("cache_read_input_tokens")
        tokens.update(
            {
                "cache_hit_rate": (
                    round(cached_tokens / input_tokens, 6)
                    if isinstance(input_tokens, int) and input_tokens > 0 and isinstance(cached_tokens, int)
                    else None
                ),
                "coverage": overall_coverage,
                "coverage_by_field": coverage_by_field,
                "reported_calls": sum(1 for row in llm_rows if row.get("total_tokens") is not None),
                "total_calls": len(llm_rows),
                "unreported_calls": sum(1 for row in llm_rows if row.get("total_tokens") is None),
            }
        )

        calls = {
            "llm_calls": len(llm_rows),
            "agent_tools": sum(1 for row in rows if row.get("kind") == "tool_call"),
            "external_requests": sum(
                1 for row in rows if row.get("kind") == "resource_call" and bool(row.get("network_request"))
            ),
            "cache_accesses": sum(
                1 for row in rows if row.get("kind") == "resource_call" and bool(row.get("cache_access"))
            ),
            "failures": sum(1 for row in rows if row.get("status") == "error"),
            "running": sum(1 for row in rows if row.get("status") == "running"),
        }

        return {
            "tokens": tokens,
            "cost": aggregate_llm_costs(llm_rows),
            "calls": calls,
            "models": cls._distribution(llm_rows, "model"),
            "tools": cls._distribution([row for row in rows if row.get("kind") == "tool_call"], "tool_name"),
            "categories": cls._distribution([row for row in rows if row.get("kind") != "llm_call"], "category"),
            "providers": cls._distribution([row for row in rows if row.get("kind") == "resource_call"], "provider"),
        }

    @staticmethod
    def _distribution(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"key": "unknown", "count": 0, "failures": 0, "elapsed_ms": 0}
        )
        for row in rows:
            key = str(row.get(field) or "unknown")
            bucket = grouped[key]
            bucket["key"] = key
            bucket["count"] += 1
            bucket["failures"] += int(row.get("status") == "error")
            bucket["elapsed_ms"] += int(row.get("elapsed_ms") or 0)
        return sorted(grouped.values(), key=lambda item: (-item["count"], item["key"]))
