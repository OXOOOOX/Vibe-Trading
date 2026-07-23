"""Transactional, auditable portfolio ledger.

The legacy JSON file remains a compatibility projection.  SQLite owns the
revision, immutable events, attempt-scoped pending mutations and reconciliation
previews so a cancelled Agent attempt can never partially update holdings.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ledger_path(projection_path: Path) -> Path:
    override = os.getenv("VIBE_TRADING_PORTFOLIO_DB_PATH")
    if override:
        return Path(override).expanduser()
    return projection_path.with_name("portfolio.db")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class PortfolioRevisionConflict(ValueError):
    """Raised when a write was prepared against an obsolete snapshot."""


class PortfolioLedger:
    """SQLite-backed portfolio state with a JSON compatibility projection."""

    def __init__(self, projection_path: Path) -> None:
        self.projection_path = Path(projection_path)
        self.path = ledger_path(self.projection_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holdings (
                    symbol TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'applied',
                    symbol TEXT,
                    payload_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    attempt_id TEXT,
                    idempotency_key TEXT,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency
                    ON events(idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS lots (
                    lot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    acquired_at TEXT,
                    original_quantity REAL NOT NULL,
                    remaining_quantity REAL NOT NULL,
                    unit_cost REAL,
                    source_event_id TEXT,
                    completeness TEXT NOT NULL DEFAULT 'complete'
                );
                CREATE TABLE IF NOT EXISTS pending_mutations (
                    mutation_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    base_revision INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(attempt_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_pending_attempt
                    ON pending_mutations(attempt_id, sequence);
                CREATE TABLE IF NOT EXISTS reconciliations (
                    reconciliation_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    base_revision INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    committed_at TEXT
                );
                """
            )
            initialized = connection.execute(
                "SELECT value FROM meta WHERE key='initialized'"
            ).fetchone()
            if initialized is None:
                self._import_legacy_projection(connection)
            elif connection.execute(
                "SELECT value FROM meta WHERE key='authority_mode'"
            ).fetchone() is None:
                self._set_meta(
                    connection,
                    "authority_mode",
                    "legacy_json_pending_reconciliation"
                    if self.projection_path.exists()
                    else "sqlite",
                )

    def _set_meta(self, connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _json(value)),
        )

    def _meta(self, connection: sqlite3.Connection, key: str, default: Any) -> Any:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return _loads(row["value"], default) if row else default

    def _import_legacy_projection(self, connection: sqlite3.Connection) -> None:
        legacy: dict[str, Any] = {}
        if self.projection_path.exists():
            legacy = _loads(self.projection_path.read_text(encoding="utf-8"), {})
            backup = self.projection_path.with_suffix(self.projection_path.suffix + ".v1.backup")
            if not backup.exists():
                shutil.copy2(self.projection_path, backup)

        revision = 1 if legacy else 0
        for raw in legacy.get("holdings") or []:
            if not isinstance(raw, dict):
                continue
            holding = dict(raw)
            symbol = str(holding.get("symbol") or holding.get("code") or "").upper()
            if not symbol:
                continue
            holding.setdefault("cost_basis_kind", "unknown")
            holding.setdefault("lot_completeness", "incomplete")
            holding.setdefault("source", "legacy_unknown")
            holding.setdefault(
                "provenance",
                {"source": "legacy_json_import", "as_of": legacy.get("updated_at")},
            )
            connection.execute(
                "INSERT OR REPLACE INTO holdings(symbol, payload_json, revision) VALUES (?, ?, ?)",
                (symbol, _json(holding), revision),
            )
        for index, raw in enumerate(legacy.get("recent_trades") or []):
            if not isinstance(raw, dict):
                continue
            event_id = str(raw.get("trade_id") or f"legacy-{index}-{uuid.uuid4().hex[:12]}")
            payload = dict(raw)
            payload.setdefault("exactness", "unavailable")
            payload.setdefault("reconciliation_status", "legacy_unknown")
            connection.execute(
                "INSERT OR IGNORE INTO events(event_id,event_type,status,symbol,payload_json,source,"
                "attempt_id,idempotency_key,revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    "trade",
                    "legacy_unknown",
                    str(payload.get("symbol") or payload.get("code") or "").upper() or None,
                    _json(payload),
                    "legacy_json_import",
                    None,
                    f"legacy-import:{event_id}",
                    revision,
                    str(payload.get("recorded_at") or legacy.get("updated_at") or utc_now()),
                ),
            )
        self._set_meta(connection, "revision", revision)
        self._set_meta(connection, "schema_version", SCHEMA_VERSION)
        self._set_meta(connection, "cash", legacy.get("cash"))
        self._set_meta(connection, "cash_currency", legacy.get("cash_currency") or "CNY")
        self._set_meta(connection, "updated_at", legacy.get("updated_at") or utc_now())
        self._set_meta(
            connection,
            "authority_mode",
            "legacy_json_pending_reconciliation" if legacy else "sqlite",
        )
        self._set_meta(connection, "initialized", True)

    def revision(self) -> int:
        with self._connect() as connection:
            return int(self._meta(connection, "revision", 0) or 0)

    def snapshot(self, *, event_limit: int = 200) -> dict[str, Any]:
        with self._connect() as connection:
            return self._snapshot(connection, event_limit=event_limit)

    def _snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        event_limit: int = 200,
        prefer_projection: bool = True,
    ) -> dict[str, Any]:
        ledger_holdings = [
            _loads(row["payload_json"], {})
            for row in connection.execute("SELECT payload_json FROM holdings ORDER BY rowid")
        ]
        authority_mode = str(self._meta(connection, "authority_mode", "sqlite") or "sqlite")
        projection: dict[str, Any] = {}
        if (
            prefer_projection
            and authority_mode == "legacy_json_pending_reconciliation"
            and self.projection_path.exists()
        ):
            projection = _loads(self.projection_path.read_text(encoding="utf-8"), {})
        holdings = (
            [dict(item) for item in projection.get("holdings") or [] if isinstance(item, dict)]
            if projection
            else ledger_holdings
        )
        recent_events: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM events ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (event_limit,),
        ):
            payload = _loads(row["payload_json"], {})
            payload.setdefault("event_id", row["event_id"])
            payload.setdefault("trade_id", row["event_id"] if row["event_type"] == "trade" else None)
            payload["event_type"] = row["event_type"]
            payload["event_status"] = row["status"]
            payload["event_source"] = row["source"]
            payload["event_revision"] = row["revision"]
            payload["attempt_id"] = row["attempt_id"]
            recent_events.append(payload)

        reversal_by_event: dict[str, str] = {}
        for item in recent_events:
            reversed_event_id = str(item.get("reverses_event_id") or "").strip()
            if item.get("event_type") == "reversal" and reversed_event_id:
                reversal_by_event[reversed_event_id] = str(item.get("event_id") or "")
        recent_trades: list[dict[str, Any]] = []
        for item in recent_events:
            if item.get("event_type") != "trade":
                continue
            projected = dict(item)
            event_id = str(projected.get("event_id") or "")
            if event_id in reversal_by_event:
                projected["event_status"] = "reversed"
                projected["reversed_by_event_id"] = reversal_by_event[event_id]
            recent_trades.append(projected)
        ledger_cash = self._meta(connection, "cash", None)
        ledger_currency = str(self._meta(connection, "cash_currency", "CNY") or "CNY")
        cash = projection.get("cash") if projection else ledger_cash
        cash_currency = str(projection.get("cash_currency") or ledger_currency) if projection else ledger_currency
        parallel_match = self._comparison_fingerprint(
            holdings,
            cash,
            cash_currency,
        ) == self._comparison_fingerprint(ledger_holdings, ledger_cash, ledger_currency)
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": int(self._meta(connection, "revision", 0) or 0),
            "holdings": holdings,
            "recent_trades": recent_trades[:event_limit],
            "ledger_events": recent_events,
            "cash": cash,
            "cash_currency": cash_currency,
            "updated_at": self._meta(connection, "updated_at", None),
            "provenance": {
                "authoritative_store": (
                    "legacy_json" if authority_mode == "legacy_json_pending_reconciliation" else "sqlite"
                ),
                "shadow_store": (
                    "sqlite" if authority_mode == "legacy_json_pending_reconciliation" else "json_projection"
                ),
                "authority_mode": authority_mode,
                "requires_reconciliation_commit": authority_mode == "legacy_json_pending_reconciliation",
                "parallel_validation": {
                    "status": "match" if parallel_match else "difference_detected",
                    "compared_revision": int(self._meta(connection, "revision", 0) or 0),
                },
                "ledger_path": str(self.path),
                "projection_path": str(self.projection_path),
            },
            "performance": self._performance(connection),
        }

    @staticmethod
    def _comparison_fingerprint(
        holdings: list[dict[str, Any]],
        cash: Any,
        cash_currency: str,
    ) -> str:
        facts = []
        for item in holdings:
            facts.append(
                {
                    "symbol": str(item.get("symbol") or item.get("code") or "").upper(),
                    "quantity": item.get("quantity"),
                    "cost_price": item.get("cost_price"),
                }
            )
        facts.sort(key=lambda item: item["symbol"])
        return _json({"holdings": facts, "cash": cash, "cash_currency": cash_currency})

    @staticmethod
    def _performance(connection: sqlite3.Connection) -> dict[str, Any]:
        realized = 0.0
        dividends = 0.0
        fees_taxes = 0.0
        broker_reported: float | None = None
        exact = True
        observed = False
        for row in connection.execute(
            "SELECT event_type,payload_json,status FROM events WHERE status IN ('applied','legacy_unknown')"
        ):
            payload = _loads(row["payload_json"], {})
            if row["event_type"] == "cash_dividend":
                dividends += float(payload.get("amount") or 0)
                observed = True
            fees_taxes += float(payload.get("fees") or 0) + float(payload.get("taxes") or 0)
            if payload.get("realized_pnl_net") is not None:
                realized += float(payload["realized_pnl_net"])
                observed = True
            elif row["event_type"] == "trade" and str(payload.get("side")) == "sell":
                exact = False
            if payload.get("broker_reported_pnl") is not None:
                broker_reported = float(payload["broker_reported_pnl"])
        return {
            "realized_pnl": realized if observed else None,
            "cash_dividends": dividends if observed else None,
            "fees_and_taxes": fees_taxes if observed else None,
            "broker_reported_pnl": broker_reported,
            "status": "broker_reported" if broker_reported is not None else "exact" if observed and exact else "unavailable",
            "unexplained_difference": None,
        }

    def _write_projection(self, snapshot: dict[str, Any]) -> None:
        self.projection_path.parent.mkdir(parents=True, exist_ok=True)
        payload = _json(snapshot)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.projection_path.parent, delete=False
        ) as handle:
            handle.write(json.dumps(json.loads(payload), ensure_ascii=False, indent=2, sort_keys=True))
            tmp_name = handle.name
        Path(tmp_name).replace(self.projection_path)

    def _ledger_snapshot(self, *, event_limit: int = 200) -> dict[str, Any]:
        with self._connect() as connection:
            return self._snapshot(
                connection,
                event_limit=event_limit,
                prefer_projection=False,
            )

    @staticmethod
    def _apply_trade_lots(
        connection: sqlite3.Connection,
        payload: dict[str, Any],
        event_id: str,
    ) -> dict[str, Any]:
        """Update FIFO lots and add deterministic realized-P&L fields when complete."""

        result = dict(payload)
        symbol = str(result.get("symbol") or "").upper()
        side = str(result.get("side") or "")
        quantity = float(result.get("quantity") or 0)
        price = float(result.get("price") or 0)
        fees = float(result.get("fees") or 0)
        taxes = float(result.get("taxes") or 0)
        if not symbol or quantity <= 0 or price <= 0:
            return result
        if side == "buy":
            unit_cost = (quantity * price + fees + taxes) / quantity
            connection.execute(
                "INSERT OR REPLACE INTO lots(lot_id,symbol,acquired_at,original_quantity,"
                "remaining_quantity,unit_cost,source_event_id,completeness) VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"lot:{event_id}",
                    symbol,
                    result.get("trade_date") or result.get("recorded_at"),
                    quantity,
                    quantity,
                    unit_cost,
                    event_id,
                    str(result.get("pre_trade_lot_completeness") or "complete"),
                ),
            )
            result["lot_cost_including_fees"] = unit_cost
            return result
        if side != "sell" or result.get("pre_trade_lot_completeness") != "complete":
            result["exactness"] = "broker_reported" if result.get("broker_reported_pnl") is not None else "unavailable"
            result["realized_pnl_net"] = result.get("broker_reported_pnl")
            result["reconciliation_gap"] = "historical FIFO lots are incomplete"
            return result

        lots = list(
            connection.execute(
                "SELECT * FROM lots WHERE symbol=? AND remaining_quantity>0 "
                "ORDER BY COALESCE(acquired_at,''), rowid",
                (symbol,),
            )
        )
        if sum(float(row["remaining_quantity"]) for row in lots) + 1e-9 < quantity:
            result["exactness"] = "broker_reported" if result.get("broker_reported_pnl") is not None else "unavailable"
            result["realized_pnl_net"] = result.get("broker_reported_pnl")
            result["reconciliation_gap"] = "known FIFO lots do not cover the sale"
            return result
        remaining = quantity
        cost = 0.0
        consumed: list[dict[str, Any]] = []
        for lot in lots:
            if remaining <= 1e-12:
                break
            take = min(remaining, float(lot["remaining_quantity"]))
            unit_cost = float(lot["unit_cost"] or 0)
            cost += take * unit_cost
            next_remaining = float(lot["remaining_quantity"]) - take
            connection.execute(
                "UPDATE lots SET remaining_quantity=? WHERE lot_id=?",
                (next_remaining, lot["lot_id"]),
            )
            consumed.append({"lot_id": lot["lot_id"], "quantity": take, "unit_cost": unit_cost})
            remaining -= take
        calculated = quantity * price - cost - fees - taxes
        result["fifo_cost"] = cost
        result["consumed_lots"] = consumed
        result["calculated_realized_pnl_net"] = calculated
        if result.get("broker_reported_pnl") is not None:
            reported = float(result["broker_reported_pnl"])
            result["realized_pnl_net"] = reported
            result["exactness"] = "broker_reported"
            result["unexplained_difference"] = reported - calculated
        else:
            result["realized_pnl_net"] = calculated
            result["exactness"] = "exact"
        return result

    def replace_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        event_type: str,
        event_payload: dict[str, Any],
        source: str,
        expected_revision: int | None = None,
        attempt_id: str | None = None,
        idempotency_key: str | None = None,
        event_status: str = "applied",
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_revision = int(self._meta(connection, "revision", 0) or 0)
            if expected_revision is not None and current_revision != int(expected_revision):
                raise PortfolioRevisionConflict(
                    f"portfolio revision changed: expected {expected_revision}, current {current_revision}"
                )
            if idempotency_key:
                existing = connection.execute(
                    "SELECT event_id FROM events WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if existing:
                    connection.rollback()
                    return self.snapshot()
            revision = current_revision + 1
            connection.execute("DELETE FROM holdings")
            for raw in snapshot.get("holdings") or []:
                holding = dict(raw)
                symbol = str(holding.get("symbol") or holding.get("code") or "").upper()
                if not symbol:
                    continue
                holding.setdefault("cost_basis_kind", "unknown")
                holding.setdefault("lot_completeness", "incomplete")
                holding.setdefault("provenance", {"source": source, "as_of": utc_now()})
                connection.execute(
                    "INSERT INTO holdings(symbol,payload_json,revision) VALUES (?,?,?)",
                    (symbol, _json(holding), revision),
                )
            event_id = str(event_payload.get("event_id") or event_payload.get("trade_id") or uuid.uuid4().hex)
            event_payload = dict(event_payload)
            event_payload.setdefault("event_id", event_id)
            event_payload.setdefault("recorded_at", utc_now())
            if event_type == "broker_snapshot" or (
                event_type != "trade" and not (snapshot.get("holdings") or [])
            ):
                connection.execute("DELETE FROM lots")
            elif event_type == "manual_adjustment" and event_payload.get("symbol"):
                connection.execute("DELETE FROM lots WHERE symbol=?", (str(event_payload["symbol"]).upper(),))
            if event_type == "trade":
                event_payload = self._apply_trade_lots(connection, event_payload, event_id)
            connection.execute(
                "INSERT INTO events(event_id,event_type,status,symbol,payload_json,source,attempt_id,"
                "idempotency_key,revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    event_type,
                    event_status,
                    str(event_payload.get("symbol") or "").upper() or None,
                    _json(event_payload),
                    source,
                    attempt_id,
                    idempotency_key,
                    revision,
                    str(event_payload.get("recorded_at") or utc_now()),
                ),
            )
            self._set_meta(connection, "revision", revision)
            self._set_meta(connection, "cash", snapshot.get("cash"))
            self._set_meta(connection, "cash_currency", snapshot.get("cash_currency") or "CNY")
            self._set_meta(connection, "updated_at", utc_now())
            connection.commit()
            self._write_projection(self._ledger_snapshot())
            return self.snapshot()

    def pending_snapshot(self, attempt_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT preview_json FROM pending_mutations WHERE attempt_id=? "
                "ORDER BY sequence DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            return _loads(row["preview_json"], {}) if row else None

    def stage(
        self,
        *,
        attempt_id: str,
        action: str,
        payload: dict[str, Any],
        preview: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_revision = int(self._meta(connection, "revision", 0) or 0)
            first = connection.execute(
                "SELECT base_revision FROM pending_mutations WHERE attempt_id=? ORDER BY sequence LIMIT 1",
                (attempt_id,),
            ).fetchone()
            base_revision = int(first["base_revision"]) if first else int(expected_revision)
            if current_revision != base_revision:
                raise PortfolioRevisionConflict(
                    f"portfolio revision changed: expected {base_revision}, current {current_revision}"
                )
            existing = connection.execute(
                "SELECT mutation_id,preview_json FROM pending_mutations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                connection.rollback()
                return {
                    "mutation_id": existing["mutation_id"],
                    "preview": _loads(existing["preview_json"], {}),
                    "deduplicated": True,
                }
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 AS value FROM pending_mutations WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()["value"]
            )
            mutation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO pending_mutations(mutation_id,attempt_id,sequence,base_revision,action,"
                "payload_json,preview_json,idempotency_key,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    mutation_id,
                    attempt_id,
                    sequence,
                    base_revision,
                    action,
                    _json(payload),
                    _json(preview),
                    idempotency_key,
                    utc_now(),
                ),
            )
            connection.commit()
            return {"mutation_id": mutation_id, "preview": preview, "deduplicated": False}

    def commit_attempt(self, attempt_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            rows = list(
                connection.execute(
                    "SELECT * FROM pending_mutations WHERE attempt_id=? ORDER BY sequence",
                    (attempt_id,),
                )
            )
            if not rows:
                return self.snapshot()
            connection.execute("BEGIN IMMEDIATE")
            current_revision = int(self._meta(connection, "revision", 0) or 0)
            base_revision = int(rows[0]["base_revision"])
            if current_revision != base_revision:
                raise PortfolioRevisionConflict(
                    f"portfolio revision changed before attempt commit: expected {base_revision}, current {current_revision}"
                )
            preview = _loads(rows[-1]["preview_json"], {})
            revision = current_revision + 1
            connection.execute("DELETE FROM holdings")
            for raw in preview.get("holdings") or []:
                holding = dict(raw)
                symbol = str(holding.get("symbol") or holding.get("code") or "").upper()
                if symbol:
                    connection.execute(
                        "INSERT INTO holdings(symbol,payload_json,revision) VALUES (?,?,?)",
                        (symbol, _json(holding), revision),
                    )
            for row in rows:
                payload = _loads(row["payload_json"], {})
                event_id = str(payload.get("event_id") or payload.get("trade_id") or row["mutation_id"])
                event_type = {
                    "record_trade": "trade",
                    "update_holdings": "broker_snapshot",
                    "clear": "manual_adjustment",
                }.get(row["action"], "manual_adjustment")
                if event_type == "broker_snapshot" or (
                    event_type != "trade" and not (preview.get("holdings") or [])
                ):
                    connection.execute("DELETE FROM lots")
                if event_type == "trade":
                    payload = self._apply_trade_lots(connection, payload, event_id)
                connection.execute(
                    "INSERT INTO events(event_id,event_type,status,symbol,payload_json,source,attempt_id,"
                    "idempotency_key,revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_id,
                        event_type,
                        "applied",
                        str(payload.get("symbol") or "").upper() or None,
                        _json(payload),
                        "agent_attempt",
                        attempt_id,
                        row["idempotency_key"],
                        revision,
                        str(payload.get("recorded_at") or utc_now()),
                    ),
                )
            self._set_meta(connection, "revision", revision)
            self._set_meta(connection, "cash", preview.get("cash"))
            self._set_meta(connection, "cash_currency", preview.get("cash_currency") or "CNY")
            self._set_meta(connection, "updated_at", utc_now())
            connection.execute("DELETE FROM pending_mutations WHERE attempt_id=?", (attempt_id,))
            connection.commit()
            self._write_projection(self._ledger_snapshot())
            return self.snapshot()

    def discard_attempt(self, attempt_id: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pending_mutations WHERE attempt_id=?", (attempt_id,)
            )
            connection.commit()
            return int(cursor.rowcount or 0)

    def reverse_event(self, event_id: str, *, reason: str = "user_requested_reversal") -> dict[str, Any]:
        with self._connect() as connection:
            original = connection.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        if original is None:
            raise ValueError(f"Trade {event_id} was not found.")
        snapshot = self.snapshot()
        return self.replace_snapshot(
            snapshot,
            event_type="reversal",
            event_payload={
                "reverses_event_id": event_id,
                "symbol": original["symbol"],
                "reason": reason,
                "holding_effect": "none_requires_explicit_adjustment",
            },
            source="user_reversal",
            idempotency_key=f"reversal:{event_id}",
        )

    def create_reconciliation(self, request: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
        reconciliation_id = uuid.uuid4().hex
        base_revision = self.revision()
        record = {
            "reconciliation_id": reconciliation_id,
            "status": "preview",
            "base_revision": base_revision,
            "request": request,
            "preview": preview,
            "created_at": utc_now(),
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reconciliations(reconciliation_id,status,base_revision,request_json,"
                "preview_json,created_at) VALUES (?,?,?,?,?,?)",
                (
                    reconciliation_id,
                    "preview",
                    base_revision,
                    _json(request),
                    _json(preview),
                    record["created_at"],
                ),
            )
            connection.commit()
        return record

    def reconciliation(self, reconciliation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliations WHERE reconciliation_id=?", (reconciliation_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Reconciliation {reconciliation_id} was not found.")
        return {
            "reconciliation_id": row["reconciliation_id"],
            "status": row["status"],
            "base_revision": row["base_revision"],
            "request": _loads(row["request_json"], {}),
            "preview": _loads(row["preview_json"], {}),
            "created_at": row["created_at"],
            "committed_at": row["committed_at"],
        }

    def commit_reconciliation(self, reconciliation_id: str, expected_revision: int) -> dict[str, Any]:
        record = self.reconciliation(reconciliation_id)
        if record["status"] == "committed":
            return {**record, "state": self.snapshot(), "deduplicated": True}
        if int(record["base_revision"]) != int(expected_revision):
            raise PortfolioRevisionConflict(
                f"reconciliation expected revision {record['base_revision']}, got {expected_revision}"
            )
        target = dict(record["preview"].get("target_state") or {})
        committed = self.replace_snapshot(
            target,
            event_type="broker_snapshot",
            event_payload={
                "reconciliation_id": reconciliation_id,
                "broker_reported_pnl": record["request"].get("broker_reported_pnl"),
                "source_label": record["request"].get("source_label") or "broker_reconciliation",
                "unexplained_pnl": record["preview"].get("unexplained_pnl"),
            },
            source="broker_reconciliation",
            expected_revision=expected_revision,
            idempotency_key=f"reconciliation:{reconciliation_id}",
        )
        with self._connect() as connection:
            connection.execute(
                "UPDATE reconciliations SET status='committed', committed_at=? WHERE reconciliation_id=?",
                (utc_now(), reconciliation_id),
            )
            self._set_meta(connection, "authority_mode", "sqlite")
            connection.commit()
        committed = self.snapshot()
        self._write_projection(committed)
        return {**self.reconciliation(reconciliation_id), "state": committed, "deduplicated": False}


def get_ledger(projection_path: Path) -> PortfolioLedger:
    return PortfolioLedger(projection_path)
