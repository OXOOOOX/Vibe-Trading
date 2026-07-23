"""SQLite storage for incremental source bars and verified consensus bars."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 5


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def market_cache_db_path() -> Path:
    override = os.getenv("VIBE_TRADING_MARKET_CACHE_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibe-trading" / "cache" / "market_cache" / "market_cache.sqlite3"


class MarketCacheStore:
    """Small transactional store optimized for a local portfolio workload."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or market_cache_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS instruments (
                    symbol TEXT PRIMARY KEY,
                    name TEXT,
                    market TEXT,
                    asset_type TEXT,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    tick_size REAL NOT NULL DEFAULT 0.001,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_bars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    bar_time TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    requested_source TEXT NOT NULL,
                    actual_source TEXT NOT NULL,
                    adapter_name TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    acquisition_mode TEXT NOT NULL,
                    requested_adjustment TEXT NOT NULL,
                    actual_adjustment TEXT NOT NULL,
                    adjustment_confidence TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL NOT NULL,
                    volume REAL,
                    raw_volume REAL,
                    raw_volume_unit TEXT NOT NULL DEFAULT 'unknown',
                    volume_unit TEXT NOT NULL DEFAULT 'unknown',
                    amount REAL,
                    vwap REAL,
                    retrieved_at TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    quality_flags TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(symbol, interval, bar_time, actual_adjustment, actual_source)
                );
                CREATE INDEX IF NOT EXISTS idx_source_bars_lookup
                    ON source_bars(symbol, interval, actual_adjustment, bar_time);

                CREATE TABLE IF NOT EXISTS consensus_bars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    bar_time TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    volume_status TEXT NOT NULL DEFAULT 'unavailable',
                    volume_source_count INTEGER NOT NULL DEFAULT 0,
                    volume_sources_json TEXT NOT NULL DEFAULT '[]',
                    volume_unit TEXT,
                    amount REAL,
                    vwap REAL,
                    status TEXT NOT NULL,
                    price_spread_pct REAL,
                    volume_spread_pct REAL,
                    amount_spread_pct REAL,
                    source_count INTEGER NOT NULL,
                    sources_json TEXT NOT NULL,
                    observations_json TEXT NOT NULL,
                    quality_flags TEXT NOT NULL DEFAULT '[]',
                    verified_at TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    UNIQUE(symbol, interval, bar_time, adjustment)
                );
                CREATE INDEX IF NOT EXISTS idx_consensus_lookup
                    ON consensus_bars(symbol, interval, adjustment, bar_time);

                CREATE TABLE IF NOT EXISTS latest_quotes (
                    symbol TEXT PRIMARY KEY,
                    interval TEXT NOT NULL,
                    bar_time TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    last_price REAL,
                    volume REAL,
                    volume_status TEXT NOT NULL DEFAULT 'unavailable',
                    volume_source_count INTEGER NOT NULL DEFAULT 0,
                    volume_sources_json TEXT NOT NULL DEFAULT '[]',
                    volume_unit TEXT,
                    amount REAL,
                    vwap REAL,
                    status TEXT NOT NULL,
                    price_spread_pct REAL,
                    source_count INTEGER NOT NULL,
                    sources_json TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    batch_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cache_coverage (
                    symbol TEXT NOT NULL,
                    actual_source TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    actual_adjustment TEXT NOT NULL,
                    min_bar_time TEXT NOT NULL,
                    max_bar_time TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    last_success_at TEXT NOT NULL,
                    last_batch_id TEXT NOT NULL,
                    PRIMARY KEY(symbol, actual_source, interval, actual_adjustment)
                );

                CREATE TABLE IF NOT EXISTS source_poll_state (
                    symbol TEXT NOT NULL,
                    requested_source TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    actual_source TEXT,
                    source_fingerprint TEXT,
                    last_attempt_at TEXT NOT NULL,
                    last_status TEXT NOT NULL,
                    last_success_at TEXT,
                    latest_bar_time TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    error_category TEXT,
                    error TEXT,
                    PRIMARY KEY(symbol, requested_source, interval, adjustment)
                );

                CREATE TABLE IF NOT EXISTS refresh_runs (
                    run_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    status TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    total_items INTEGER NOT NULL DEFAULT 0,
                    completed_items INTEGER NOT NULL DEFAULT 0,
                    conflict_items INTEGER NOT NULL DEFAULT 0,
                    failed_items INTEGER NOT NULL DEFAULT 0,
                    current_symbol TEXT,
                    current_source TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_refresh_runs_active
                    ON refresh_runs(dedupe_key, status, created_at);

                CREATE TABLE IF NOT EXISTS refresh_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES refresh_runs(run_id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_sources_json TEXT NOT NULL DEFAULT '[]',
                    actual_sources_json TEXT NOT NULL DEFAULT '[]',
                    attempts_json TEXT NOT NULL DEFAULT '[]',
                    rows_written INTEGER NOT NULL DEFAULT 0,
                    message TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(run_id, symbol, interval, adjustment)
                );

                CREATE TABLE IF NOT EXISTS adjustment_factors (
                    symbol TEXT NOT NULL,
                    effective_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    factor REAL NOT NULL,
                    confidence TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, effective_date, source)
                );
                """
            )
            item_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(refresh_items)").fetchall()
            }
            if "attempts_json" not in item_columns:
                conn.execute(
                    "ALTER TABLE refresh_items ADD COLUMN attempts_json TEXT NOT NULL DEFAULT '[]'"
                )
            source_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(source_bars)").fetchall()
            }
            if "raw_volume_unit" not in source_columns:
                conn.execute(
                    "ALTER TABLE source_bars ADD COLUMN raw_volume_unit TEXT NOT NULL DEFAULT 'unknown'"
                )
            consensus_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(consensus_bars)").fetchall()
            }
            for column, definition in (
                ("volume_status", "TEXT NOT NULL DEFAULT 'unavailable'"),
                ("volume_source_count", "INTEGER NOT NULL DEFAULT 0"),
                ("volume_sources_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("volume_unit", "TEXT"),
            ):
                if column not in consensus_columns:
                    conn.execute(f"ALTER TABLE consensus_bars ADD COLUMN {column} {definition}")
            quote_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(latest_quotes)").fetchall()
            }
            for column, definition in (
                ("volume_status", "TEXT NOT NULL DEFAULT 'unavailable'"),
                ("volume_source_count", "INTEGER NOT NULL DEFAULT 0"),
                ("volume_sources_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("volume_unit", "TEXT"),
            ):
                if column not in quote_columns:
                    conn.execute(f"ALTER TABLE latest_quotes ADD COLUMN {column} {definition}")
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def mark_running_interrupted(self) -> int:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE refresh_runs SET status='interrupted', completed_at=?, "
                "error=COALESCE(error, 'Service restarted during refresh') "
                "WHERE status IN ('queued', 'running')",
                (now,),
            )
            conn.execute(
                "UPDATE refresh_items SET status='interrupted', completed_at=? "
                "WHERE status IN ('queued', 'fetching', 'verifying')",
                (now,),
            )
            return int(cursor.rowcount)

    def migrate_price_basis_contract_v2(self) -> list[str]:
        """Remove legacy overseas daily bars that were mislabelled as ``raw``.

        Yahoo's daily quote OHLC is split-adjusted. Older cache versions stored
        it as raw, while the old Nasdaq and Stooq paths did not prove their
        corporate-action semantics. Retaining any of those rows would
        contaminate newly strict raw consensus. The migration removes those
        source rows and all derived raw-daily consensus for affected symbols;
        the service then rebuilds consensus from remaining classified sources.
        """

        migration_key = "price_basis_contract_v2"
        with self.transaction() as conn:
            completed = conn.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                (migration_key,),
            ).fetchone()
            if completed:
                return []

            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM source_bars
                WHERE interval='1D' AND actual_adjustment='raw'
                  AND LOWER(actual_source) IN ('yahoo', 'yfinance', 'nasdaq', 'stooq')
                  AND (UPPER(symbol) LIKE '%.US' OR UPPER(symbol) LIKE '%.HK')
                """
            ).fetchall()
            symbols = [str(row["symbol"]).upper() for row in rows]
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                conn.execute(
                    f"""
                    DELETE FROM source_bars
                    WHERE symbol IN ({placeholders}) AND interval='1D'
                      AND actual_adjustment='raw'
                      AND LOWER(actual_source) IN ('yahoo', 'yfinance', 'nasdaq', 'stooq')
                    """,
                    symbols,
                )
                conn.execute(
                    f"""
                    DELETE FROM cache_coverage
                    WHERE symbol IN ({placeholders}) AND interval='1D'
                      AND actual_adjustment='raw'
                      AND LOWER(actual_source) IN ('yahoo', 'yfinance', 'nasdaq', 'stooq')
                    """,
                    symbols,
                )
                conn.execute(
                    f"""
                    DELETE FROM source_poll_state
                    WHERE symbol IN ({placeholders}) AND interval='1D'
                      AND adjustment='raw'
                      AND LOWER(requested_source) IN ('yahoo', 'yfinance', 'nasdaq', 'stooq')
                    """,
                    symbols,
                )
                # Consensus rows embed source observations as JSON. Rebuilding
                # the whole raw-daily path is safer than editing those blobs.
                conn.execute(
                    f"""
                    DELETE FROM consensus_bars
                    WHERE symbol IN ({placeholders}) AND interval='1D'
                      AND adjustment='raw'
                    """,
                    symbols,
                )
                conn.execute(
                    f"""
                    DELETE FROM latest_quotes
                    WHERE symbol IN ({placeholders}) AND interval='1D'
                      AND adjustment='raw'
                    """,
                    symbols,
                )

            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                (migration_key, utc_now()),
            )
        return symbols

    def volume_consensus_v2_targets(self) -> list[tuple[str, str, str]]:
        """Return retained intraday series that still need volume-v2 rebuild."""

        migration_key = "volume_consensus_v2"
        with self.connect() as conn:
            completed = conn.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                (migration_key,),
            ).fetchone()
            if completed:
                return []
            rows = conn.execute(
                """
                SELECT DISTINCT symbol, interval, actual_adjustment
                FROM source_bars
                WHERE interval IN ('1m', '5m')
                ORDER BY symbol, interval, actual_adjustment
                """
            ).fetchall()
        return [
            (str(row["symbol"]), str(row["interval"]), str(row["actual_adjustment"]))
            for row in rows
        ]

    def mark_volume_consensus_v2_complete(self) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('volume_consensus_v2', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (utc_now(),),
            )

    def migrate_mootdx_volume_contract_v3(self) -> list[tuple[str, str, str]]:
        """Normalize retained China-market Mootdx volume from lots to shares.

        ``raw_volume`` is immutable provider evidence.  Only the canonical
        derived volume, unit and VWAP are repaired, after which the service
        rebuilds consensus for the returned series.  The migration is
        idempotent and records its completion in ``schema_meta``.
        """

        migration_key = "mootdx_volume_contract_v3"
        with self.transaction() as conn:
            completed = conn.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                (migration_key,),
            ).fetchone()
            if completed:
                return []
            rows = conn.execute(
                """
                SELECT DISTINCT symbol, interval, actual_adjustment
                FROM source_bars
                WHERE LOWER(actual_source)='mootdx'
                  AND interval='1D'
                  AND (UPPER(symbol) LIKE '%.SH'
                       OR UPPER(symbol) LIKE '%.SZ'
                       OR UPPER(symbol) LIKE '%.BJ')
                  AND raw_volume IS NOT NULL
                ORDER BY symbol, interval, actual_adjustment
                """
            ).fetchall()
            targets = [
                (
                    str(row["symbol"]).upper(),
                    str(row["interval"]),
                    str(row["actual_adjustment"]),
                )
                for row in rows
            ]
            conn.execute(
                """
                UPDATE source_bars
                SET volume=raw_volume * 100.0,
                    raw_volume_unit='lot',
                    volume_unit='share',
                    vwap=CASE
                        WHEN amount IS NOT NULL AND raw_volume > 0
                        THEN amount / (raw_volume * 100.0)
                        ELSE vwap
                    END
                WHERE LOWER(actual_source)='mootdx'
                  AND interval='1D'
                  AND (UPPER(symbol) LIKE '%.SH'
                       OR UPPER(symbol) LIKE '%.SZ'
                       OR UPPER(symbol) LIKE '%.BJ')
                  AND raw_volume IS NOT NULL
                """
            )
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (migration_key, utc_now()),
            )
        return targets

    def migrate_mootdx_intraday_volume_contract_v4(self) -> list[tuple[str, str, str]]:
        """Restore TDX minute evidence, which is already expressed in shares."""

        migration_key = "mootdx_intraday_volume_contract_v4"
        with self.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM schema_meta WHERE key=?", (migration_key,)
            ).fetchone():
                return []
            rows = conn.execute(
                """
                SELECT DISTINCT symbol,interval,actual_adjustment
                FROM source_bars
                WHERE LOWER(actual_source)='mootdx'
                  AND interval IN ('1m','5m')
                  AND raw_volume IS NOT NULL
                  AND (UPPER(symbol) LIKE '%.SH'
                       OR UPPER(symbol) LIKE '%.SZ'
                       OR UPPER(symbol) LIKE '%.BJ')
                ORDER BY symbol,interval,actual_adjustment
                """
            ).fetchall()
            targets = [
                (str(row["symbol"]), str(row["interval"]), str(row["actual_adjustment"]))
                for row in rows
            ]
            conn.execute(
                """
                UPDATE source_bars
                SET volume=raw_volume,
                    raw_volume_unit='share',
                    volume_unit='share',
                    vwap=CASE
                        WHEN amount IS NOT NULL AND raw_volume>0
                        THEN amount/raw_volume
                        ELSE vwap
                    END
                WHERE LOWER(actual_source)='mootdx'
                  AND interval IN ('1m','5m')
                  AND raw_volume IS NOT NULL
                  AND (UPPER(symbol) LIKE '%.SH'
                       OR UPPER(symbol) LIKE '%.SZ'
                       OR UPPER(symbol) LIKE '%.BJ')
                """
            )
            conn.execute(
                """
                UPDATE source_bars
                SET raw_volume_unit=CASE
                    WHEN interval='1D' THEN 'lot'
                    WHEN interval IN ('1m','5m') THEN 'share'
                    ELSE raw_volume_unit
                END
                WHERE LOWER(actual_source)='mootdx'
                  AND (UPPER(symbol) LIKE '%.SH'
                       OR UPPER(symbol) LIKE '%.SZ'
                       OR UPPER(symbol) LIKE '%.BJ')
                """
            )
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?)",
                (migration_key, utc_now()),
            )
        return targets

    def migrate_raw_volume_unit_contract_v5(self) -> int:
        """Backfill the provider unit beside immutable raw volume evidence."""

        migration_key = "raw_volume_unit_contract_v5"
        with self.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM schema_meta WHERE key=?", (migration_key,)
            ).fetchone():
                return 0
            cursor = conn.execute(
                """
                UPDATE source_bars
                SET raw_volume_unit=CASE
                    WHEN LOWER(actual_source) IN ('tencent','eastmoney') THEN 'lot'
                    WHEN LOWER(actual_source)='mootdx' AND interval='1D' THEN 'lot'
                    WHEN LOWER(actual_source)='mootdx' AND interval IN ('1m','5m') THEN 'share'
                    ELSE raw_volume_unit
                END
                WHERE raw_volume IS NOT NULL
                  AND (raw_volume_unit IS NULL OR raw_volume_unit IN ('','unknown'))
                  AND (UPPER(symbol) LIKE '%.SH'
                       OR UPPER(symbol) LIKE '%.SZ'
                       OR UPPER(symbol) LIKE '%.BJ')
                """
            )
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?)",
                (migration_key, utc_now()),
            )
        return int(cursor.rowcount)

    def list_adjustment_factors(self, symbol: str) -> list[dict[str, Any]]:
        """Return traceable corporate-action factors for continuity checks."""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol,effective_date,source,factor,confidence,retrieved_at
                FROM adjustment_factors
                WHERE symbol=?
                ORDER BY effective_date,source
                """,
                (symbol.upper(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_adjustment_factors(self, rows: Iterable[dict[str, Any]]) -> int:
        """Persist factor evidence without changing its revision on no-op runs."""

        normalized = [
            {
                "symbol": str(row["symbol"]).upper(),
                "effective_date": str(row["effective_date"])[:10],
                "source": str(row["source"]),
                "factor": float(row["factor"]),
                "confidence": str(row.get("confidence") or "candidate"),
                "retrieved_at": str(row.get("retrieved_at") or utc_now()),
            }
            for row in rows
            if float(row.get("factor") or 0) > 0
        ]
        if not normalized:
            return 0
        with self.connect() as conn:
            for row in normalized:
                conn.execute(
                    """
                    INSERT INTO adjustment_factors(
                        symbol,effective_date,source,factor,confidence,retrieved_at
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(symbol,effective_date,source) DO UPDATE SET
                        factor=excluded.factor,
                        confidence=excluded.confidence,
                        retrieved_at=CASE
                            WHEN ABS(adjustment_factors.factor-excluded.factor)>0.00000001
                              OR adjustment_factors.confidence<>excluded.confidence
                            THEN excluded.retrieved_at
                            ELSE adjustment_factors.retrieved_at
                        END
                    """,
                    (
                        row["symbol"], row["effective_date"], row["source"],
                        row["factor"], row["confidence"], row["retrieved_at"],
                    ),
                )
        return len(normalized)

    def migrate_etf_adjustment_contract_v2(self) -> list[str]:
        """Quarantine BaoStock ETF rows whose requested qfq basis was not proven."""

        migration_key = "etf_adjustment_contract_v2"
        with self.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM schema_meta WHERE key=?", (migration_key,)
            ).fetchone():
                return []
            rows = conn.execute(
                """
                SELECT DISTINCT symbol FROM source_bars
                WHERE LOWER(actual_source)='baostock'
                  AND actual_adjustment='qfq'
                  AND SUBSTR(UPPER(symbol),1,2) IN ('15','16','50','51','52','56','58')
                  AND (UPPER(symbol) LIKE '%.SH' OR UPPER(symbol) LIKE '%.SZ')
                """
            ).fetchall()
            symbols = [str(row["symbol"]) for row in rows]
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                conn.execute(
                    f"""
                    DELETE FROM cache_coverage
                    WHERE LOWER(actual_source)='baostock'
                      AND actual_adjustment='qfq'
                      AND symbol IN ({placeholders})
                    """,
                    symbols,
                )
                conn.execute(
                    f"""
                    UPDATE source_bars
                    SET actual_adjustment='source_default',
                        adjustment_confidence='provider_etf_adjustment_unverified'
                    WHERE LOWER(actual_source)='baostock'
                      AND actual_adjustment='qfq'
                      AND symbol IN ({placeholders})
                    """,
                    symbols,
                )
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?)",
                (migration_key, utc_now()),
            )
        return symbols

    def upsert_instrument(self, symbol: str, *, name: str | None = None) -> None:
        upper = symbol.upper()
        market = upper.rsplit(".", 1)[-1] if "." in upper else "unknown"
        asset_type = "etf" if upper[:2] in {"15", "16", "50", "51", "52", "56", "58"} else "equity"
        tick_size = 0.001 if asset_type == "etf" else 0.01
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO instruments(symbol, name, market, asset_type, currency, tick_size, updated_at)
                VALUES(?, ?, ?, ?, 'CNY', ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name=COALESCE(excluded.name, instruments.name), market=excluded.market,
                    asset_type=excluded.asset_type, tick_size=excluded.tick_size,
                    updated_at=excluded.updated_at
                """,
                (upper, name, market, asset_type, tick_size, utc_now()),
            )

    def instrument(self, symbol: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol.upper(),)).fetchone()
        return dict(row) if row else {"symbol": symbol.upper(), "tick_size": 0.001}

    def create_run(
        self,
        *,
        run_id: str,
        dedupe_key: str,
        profile: str,
        symbols: list[str],
        config: dict[str, Any],
        items: list[tuple[str, str, str]],
    ) -> None:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO refresh_runs(
                    run_id, dedupe_key, profile, status, symbols_json, config_json,
                    total_items, created_at
                ) VALUES(?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (run_id, dedupe_key, profile, json.dumps(symbols), json.dumps(config), len(items), now),
            )
            conn.executemany(
                """
                INSERT INTO refresh_items(run_id, symbol, interval, adjustment, status)
                VALUES(?, ?, ?, ?, 'queued')
                """,
                [(run_id, symbol, interval, adjustment) for symbol, interval, adjustment in items],
            )

    def find_active_run(self, dedupe_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM refresh_runs WHERE dedupe_key=? AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
        return self._decode_run(row) if row else None

    def update_run(self, run_id: str, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "status", "completed_items", "conflict_items", "failed_items",
            "current_symbol", "current_source", "started_at", "completed_at", "error",
        }
        payload = {key: value for key, value in values.items() if key in allowed}
        if not payload:
            return
        clause = ", ".join(f"{key}=?" for key in payload)
        with self.connect() as conn:
            conn.execute(f"UPDATE refresh_runs SET {clause} WHERE run_id=?", (*payload.values(), run_id))

    def update_item(self, run_id: str, symbol: str, interval: str, adjustment: str, **values: Any) -> None:
        allowed = {
            "status", "requested_sources_json", "actual_sources_json", "attempts_json", "rows_written",
            "message", "started_at", "completed_at",
        }
        payload = {key: value for key, value in values.items() if key in allowed}
        if not payload:
            return
        clause = ", ".join(f"{key}=?" for key in payload)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE refresh_items SET {clause} WHERE run_id=? AND symbol=? AND interval=? AND adjustment=?",
                (*payload.values(), run_id, symbol, interval, adjustment),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM refresh_runs WHERE run_id=?", (run_id,)).fetchone()
            items = conn.execute(
                "SELECT * FROM refresh_items WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        if not row:
            return None
        result = self._decode_run(row)
        result["items"] = [self._decode_item(item) for item in items]
        return result

    def latest_active_run(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM refresh_runs WHERE status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return self.get_run(row["run_id"]) if row else None

    def latest_finished_run(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM refresh_runs WHERE status IN ('completed','partial') "
                "AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
        return self.get_run(row["run_id"]) if row else None

    def latest_source_attempts(self, limit: int = 2000) -> list[dict[str, Any]]:
        """Return the newest real network attempt for each requested provider.

        Source health is provider-scoped, while refresh runs are symbol- and
        interval-scoped.  Reading only the single newest run drops a provider's
        newer failure as soon as another symbol finishes refreshing.  Scan a
        bounded set of finished items instead and keep the newest actual attempt
        for every requested source.  Cache reuse/backoff rows are deliberately
        skipped because they are decisions, not new upstream requests.
        """

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT attempts_json, completed_at, id
                FROM refresh_items
                WHERE completed_at IS NOT NULL
                  AND attempts_json IS NOT NULL
                  AND attempts_json != '[]'
                ORDER BY completed_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()

        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                attempts = json.loads(row["attempts_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(attempts, list):
                continue
            for raw in reversed(attempts):
                if not isinstance(raw, dict):
                    continue
                status = str(raw.get("status") or "")
                if status in {"cache_fresh", "retry_backoff"}:
                    continue
                requested = str(raw.get("requested_source") or "").strip()
                if not requested or requested in latest:
                    continue
                latest[requested] = {
                    **raw,
                    "updated_at": str(row["completed_at"]),
                }
        return list(latest.values())

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["symbols"] = json.loads(result.pop("symbols_json") or "[]")
        result["config"] = json.loads(result.pop("config_json") or "{}")
        total = int(result.get("total_items") or 0)
        completed = int(result.get("completed_items") or 0)
        result["progress_pct"] = round(completed / total * 100, 1) if total else 0.0
        return result

    @staticmethod
    def _decode_item(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["requested_sources"] = json.loads(result.pop("requested_sources_json") or "[]")
        result["actual_sources"] = json.loads(result.pop("actual_sources_json") or "[]")
        result["attempts"] = json.loads(result.pop("attempts_json") or "[]")
        return result

    def coverage(self, symbol: str, actual_source: str, interval: str, adjustment: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM cache_coverage
                WHERE symbol=? AND actual_source=? AND interval=? AND actual_adjustment=?
                """,
                (symbol.upper(), actual_source, interval, adjustment),
            ).fetchone()
        return dict(row) if row else None

    def source_poll_state(
        self, symbol: str, requested_source: str, interval: str, adjustment: str
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM source_poll_state
                WHERE symbol=? AND requested_source=? AND interval=? AND adjustment=?
                """,
                (symbol.upper(), requested_source, interval, adjustment),
            ).fetchone()
        return dict(row) if row else None

    def record_source_attempt(
        self,
        *,
        symbol: str,
        requested_source: str,
        interval: str,
        adjustment: str,
        status: str,
        attempted_at: str,
        actual_source: str | None = None,
        source_fingerprint: str | None = None,
        latest_bar_time: str | None = None,
        error_category: str | None = None,
        error: str | None = None,
        retry_base_seconds: int = 60,
    ) -> dict[str, Any]:
        """Persist source-level success freshness and bounded failure backoff."""
        success = status == "success"
        with self.transaction() as conn:
            previous = conn.execute(
                """
                SELECT * FROM source_poll_state
                WHERE symbol=? AND requested_source=? AND interval=? AND adjustment=?
                """,
                (symbol.upper(), requested_source, interval, adjustment),
            ).fetchone()
            previous_failures = (
                int(previous["consecutive_failures"] or 0)
                if previous is not None
                else 0
            )
            failures = 0 if success else previous_failures + 1
            last_success_at = (
                attempted_at
                if success
                else (previous["last_success_at"] if previous is not None else None)
            )
            retained_latest = (
                latest_bar_time
                if success
                else (previous["latest_bar_time"] if previous is not None else None)
            )
            next_retry_at = None
            if not success:
                delay = min(max(1, int(retry_base_seconds)) * (2 ** (failures - 1)), 3600)
                next_retry_at = (
                    datetime.fromisoformat(attempted_at.replace("Z", "+00:00"))
                    + timedelta(seconds=delay)
                ).isoformat()
            conn.execute(
                """
                INSERT INTO source_poll_state(
                    symbol, requested_source, interval, adjustment,
                    actual_source, source_fingerprint, last_attempt_at, last_status,
                    last_success_at, latest_bar_time, consecutive_failures,
                    next_retry_at, error_category, error
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,requested_source,interval,adjustment) DO UPDATE SET
                    actual_source=COALESCE(excluded.actual_source, source_poll_state.actual_source),
                    source_fingerprint=COALESCE(
                        excluded.source_fingerprint, source_poll_state.source_fingerprint
                    ),
                    last_attempt_at=excluded.last_attempt_at,
                    last_status=excluded.last_status,
                    last_success_at=excluded.last_success_at,
                    latest_bar_time=excluded.latest_bar_time,
                    consecutive_failures=excluded.consecutive_failures,
                    next_retry_at=excluded.next_retry_at,
                    error_category=excluded.error_category,
                    error=excluded.error
                """,
                (
                    symbol.upper(), requested_source, interval, adjustment,
                    actual_source, source_fingerprint, attempted_at, status,
                    last_success_at, retained_latest, failures, next_retry_at,
                    error_category, error,
                ),
            )
        result = self.source_poll_state(symbol, requested_source, interval, adjustment)
        assert result is not None
        return result

    def tail_start(self, symbol: str, actual_source: str, interval: str, adjustment: str, count: int) -> str | None:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT bar_time FROM source_bars
                WHERE symbol=? AND actual_source=? AND interval=? AND actual_adjustment=?
                ORDER BY bar_time DESC LIMIT ?
                """,
                (symbol.upper(), actual_source, interval, adjustment, count),
            ).fetchall()
        return rows[-1]["bar_time"] if rows else None

    def upsert_source_bars(self, bars: Iterable[dict[str, Any]]) -> int:
        rows = list(bars)
        if not rows:
            return 0
        columns = (
            "symbol", "interval", "bar_time", "session_date", "requested_source",
            "actual_source", "adapter_name", "source_fingerprint", "acquisition_mode",
            "requested_adjustment", "actual_adjustment", "adjustment_confidence",
            "open", "high", "low", "close", "volume", "raw_volume", "raw_volume_unit", "volume_unit",
            "amount", "vwap", "retrieved_at", "batch_id", "payload_hash", "quality_flags",
        )
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f"{column}=excluded.{column}" for column in columns
            if column not in {"symbol", "interval", "bar_time", "actual_adjustment", "actual_source"}
        )
        with self.transaction() as conn:
            daily_sessions = {
                (
                    row["symbol"], row["actual_adjustment"], row["actual_source"],
                    row["session_date"], row["bar_time"],
                )
                for row in rows
                if row.get("interval") == "1D"
            }
            conn.executemany(
                """
                DELETE FROM source_bars
                WHERE symbol=? AND interval='1D' AND actual_adjustment=?
                  AND actual_source=? AND session_date=? AND bar_time<>?
                """,
                daily_sessions,
            )
            conn.executemany(
                f"INSERT INTO source_bars({','.join(columns)}) VALUES({placeholders}) "
                f"ON CONFLICT(symbol,interval,bar_time,actual_adjustment,actual_source) DO UPDATE SET {updates}",
                [tuple(row.get(column) for column in columns) for row in rows],
            )
            keys = {(row["symbol"], row["actual_source"], row["interval"], row["actual_adjustment"]) for row in rows}
            now = utc_now()
            for symbol, source, interval, adjustment in keys:
                coverage = conn.execute(
                    """
                    SELECT MIN(bar_time) AS min_time, MAX(bar_time) AS max_time, COUNT(*) AS row_count
                    FROM source_bars WHERE symbol=? AND actual_source=? AND interval=? AND actual_adjustment=?
                    """,
                    (symbol, source, interval, adjustment),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO cache_coverage(
                        symbol, actual_source, interval, actual_adjustment, min_bar_time,
                        max_bar_time, row_count, last_success_at, last_batch_id
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,actual_source,interval,actual_adjustment) DO UPDATE SET
                        min_bar_time=excluded.min_bar_time, max_bar_time=excluded.max_bar_time,
                        row_count=excluded.row_count, last_success_at=excluded.last_success_at,
                        last_batch_id=excluded.last_batch_id
                    """,
                    (symbol, source, interval, adjustment, coverage["min_time"], coverage["max_time"],
                     coverage["row_count"], now, rows[-1]["batch_id"]),
                )
        return len(rows)

    def source_bars(
        self, symbol: str, interval: str, adjustment: str, *, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["symbol=?", "interval=?", "actual_adjustment=?"]
        params: list[Any] = [symbol.upper(), interval, adjustment]
        if start:
            clauses.append("bar_time>=?")
            params.append(start)
        if end:
            clauses.append("bar_time<=?")
            params.append(end)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM source_bars WHERE {' AND '.join(clauses)} ORDER BY bar_time, actual_source",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def source_tail(
        self,
        symbol: str,
        actual_source: str,
        interval: str,
        adjustment: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM source_bars
                WHERE symbol=? AND actual_source=? AND interval=? AND actual_adjustment=?
                ORDER BY bar_time DESC LIMIT ?
                """,
                (symbol.upper(), actual_source, interval, adjustment, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def replace_consensus(self, rows: Iterable[dict[str, Any]]) -> int:
        payload = list(rows)
        if not payload:
            return 0
        columns = (
            "symbol", "interval", "bar_time", "session_date", "adjustment", "open",
            "high", "low", "close", "volume", "volume_status", "volume_source_count",
            "volume_sources_json", "volume_unit", "amount", "vwap", "status",
            "price_spread_pct", "volume_spread_pct", "amount_spread_pct", "source_count",
            "sources_json", "observations_json", "quality_flags", "verified_at", "batch_id",
        )
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f"{column}=excluded.{column}" for column in columns
            if column not in {"symbol", "interval", "bar_time", "adjustment"}
        )
        with self.transaction() as conn:
            daily_sessions = {
                (
                    row["symbol"], row["adjustment"], row["session_date"], row["bar_time"]
                )
                for row in payload
                if row.get("interval") == "1D"
            }
            conn.executemany(
                """
                DELETE FROM consensus_bars
                WHERE symbol=? AND interval='1D' AND adjustment=?
                  AND session_date=? AND bar_time<>?
                """,
                daily_sessions,
            )
            conn.executemany(
                f"INSERT INTO consensus_bars({','.join(columns)}) VALUES({placeholders}) "
                f"ON CONFLICT(symbol,interval,bar_time,adjustment) DO UPDATE SET {updates}",
                [tuple(row.get(column) for column in columns) for row in payload],
            )
        return len(payload)

    def refresh_latest_quote(self, symbol: str) -> dict[str, Any] | None:
        accepted_statuses = ["verified"]
        upper = symbol.upper()
        if upper.endswith((".US", ".HK")) or upper.endswith(("-USDT", "/USDT")):
            accepted_statuses.append("single_source")
        status_placeholders = ",".join("?" for _ in accepted_statuses)
        with self.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM consensus_bars
                WHERE symbol=? AND adjustment='raw' AND status IN ({status_placeholders})
                ORDER BY bar_time DESC, CASE interval WHEN '1m' THEN 0 WHEN '5m' THEN 1 ELSE 2 END
                LIMIT 50
                """,
                (upper, *accepted_statuses),
            ).fetchall()
            if not rows:
                return None
            row = rows[0]
            # US/HK/crypto portfolios may display a one-source forming bar, but
            # an immediately preceding verified bar is the safer quote for AI
            # planning. Prefer that quorum result while it is no more than one
            # 5-minute polling window behind the newest accepted observation.
            if row["status"] != "verified":
                verified = next(
                    (
                        candidate
                        for candidate in rows
                        if candidate["status"] == "verified"
                        and candidate["interval"] in {"1m", "5m"}
                    ),
                    None,
                )
                if verified is not None:
                    newest_time = datetime.fromisoformat(
                        str(row["bar_time"]).replace("Z", "+00:00")
                    )
                    verified_time = datetime.fromisoformat(
                        str(verified["bar_time"]).replace("Z", "+00:00")
                    )
                    if newest_time - verified_time <= timedelta(minutes=10):
                        row = verified
            conn.execute(
                """
                INSERT INTO latest_quotes(
                    symbol, interval, bar_time, session_date, adjustment, last_price,
                    volume, volume_status, volume_source_count, volume_sources_json,
                    volume_unit, amount, vwap, status, price_spread_pct, source_count,
                    sources_json, verified_at, batch_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    interval=excluded.interval, bar_time=excluded.bar_time,
                    session_date=excluded.session_date, adjustment=excluded.adjustment,
                    last_price=excluded.last_price, volume=excluded.volume,
                    volume_status=excluded.volume_status,
                    volume_source_count=excluded.volume_source_count,
                    volume_sources_json=excluded.volume_sources_json,
                    volume_unit=excluded.volume_unit,
                    amount=excluded.amount, vwap=excluded.vwap, status=excluded.status,
                    price_spread_pct=excluded.price_spread_pct,
                    source_count=excluded.source_count, sources_json=excluded.sources_json,
                    verified_at=excluded.verified_at, batch_id=excluded.batch_id
                """,
                (
                    row["symbol"], row["interval"], row["bar_time"], row["session_date"],
                    row["adjustment"], row["close"], row["volume"], row["volume_status"],
                    row["volume_source_count"], row["volume_sources_json"], row["volume_unit"],
                    row["amount"], row["vwap"],
                    row["status"], row["price_spread_pct"], row["source_count"],
                    row["sources_json"], row["verified_at"], row["batch_id"],
                ),
            )
        return self.quote(symbol)

    def quote(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM latest_quotes WHERE symbol=?", (symbol.upper(),)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["sources"] = json.loads(result.pop("sources_json") or "[]")
        result["volume_sources"] = json.loads(result.pop("volume_sources_json") or "[]")
        return self._with_freshness(result)

    def list_quotes(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                rows = conn.execute(
                    f"SELECT * FROM latest_quotes WHERE symbol IN ({placeholders}) ORDER BY symbol",
                    [symbol.upper() for symbol in symbols],
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM latest_quotes ORDER BY symbol").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["sources"] = json.loads(item.pop("sources_json") or "[]")
            item["volume_sources"] = json.loads(item.pop("volume_sources_json") or "[]")
            result.append(self._with_freshness(item))
        return result

    @staticmethod
    def _with_freshness(item: dict[str, Any]) -> dict[str, Any]:
        if item.get("interval") not in {"1m", "5m"}:
            return item
        symbol = str(item.get("symbol") or "").upper()
        timezone_name = (
            "Asia/Shanghai" if symbol.endswith((".SH", ".SZ", ".BJ", ".HK"))
            else "America/New_York" if symbol.endswith(".US")
            else "UTC"
        )
        market_tz = ZoneInfo(timezone_name)
        now = datetime.now(market_tz)
        if item.get("session_date") != now.date().isoformat():
            return item
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close_hour = 16 if symbol.endswith(".US") else 15
        market_close = now.replace(hour=market_close_hour, minute=0, second=0, microsecond=0)
        if not market_open <= now <= market_close:
            return item
        bar_time = datetime.fromisoformat(str(item["bar_time"]).replace("Z", "+00:00")).astimezone(market_tz)
        allowed_delay = timedelta(minutes=2 if item["interval"] == "1m" else 10)
        if now - bar_time > allowed_delay:
            item["status"] = "stale"
        return item

    def list_coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                rows = conn.execute(
                    "SELECT c.*, i.name AS name FROM cache_coverage c "
                    "LEFT JOIN instruments i ON i.symbol=c.symbol "
                    f"WHERE c.symbol IN ({placeholders}) "
                    "ORDER BY c.symbol, c.interval, c.actual_adjustment, c.actual_source",
                    [symbol.upper() for symbol in symbols],
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT c.*, i.name AS name FROM cache_coverage c "
                    "LEFT JOIN instruments i ON i.symbol=c.symbol "
                    "ORDER BY c.symbol, c.interval, c.actual_adjustment, c.actual_source"
                ).fetchall()
        return [dict(row) for row in rows]

    def query_bars(
        self,
        *,
        symbol: str,
        interval: str,
        adjustment: str,
        view: str = "consensus",
        start: str | None = None,
        end: str | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        table = "consensus_bars" if view == "consensus" else "source_bars"
        adjustment_column = "adjustment" if view == "consensus" else "actual_adjustment"
        clauses = ["symbol=?", "interval=?", f"{adjustment_column}=?"]
        params: list[Any] = [symbol.upper(), interval, adjustment]
        if start:
            clauses.append("bar_time>=?")
            params.append(start)
        if end:
            clauses.append("bar_time<=?")
            params.append(end)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY bar_time DESC LIMIT ?",
                params,
            ).fetchall()
        result = [dict(row) for row in reversed(rows)]
        for item in result:
            for key in (
                "sources_json", "volume_sources_json", "observations_json", "quality_flags"
            ):
                if key in item:
                    item[key.removesuffix("_json")] = json.loads(item.pop(key) or "[]")
        return result

    def query_same_time_bucket_bars(
        self,
        *,
        symbol: str,
        interval: str,
        adjustment: str,
        local_time_bucket: str,
        view: str = "consensus",
        before: str | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Return historical bars from one Asia/Shanghai HH:MM bucket.

        Price-volume baselines need one seasonal bucket across many sessions,
        especially for 1m rules.  Filtering that bucket in SQLite avoids
        loading several thousand unrelated intraday bars into every analyzer.
        """

        table = "consensus_bars" if view == "consensus" else "source_bars"
        adjustment_column = "adjustment" if view == "consensus" else "actual_adjustment"
        clauses = [
            "symbol=?",
            "interval=?",
            f"{adjustment_column}=?",
            "strftime('%H:%M', datetime(bar_time, '+8 hours'))=?",
        ]
        params: list[Any] = [
            symbol.upper(),
            interval,
            adjustment,
            str(local_time_bucket),
        ]
        if before:
            clauses.append("bar_time<?")
            params.append(before)
        params.append(max(1, min(int(limit), 2000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} "
                "ORDER BY bar_time DESC LIMIT ?",
                params,
            ).fetchall()
        result = [dict(row) for row in reversed(rows)]
        for item in result:
            for key in (
                "sources_json", "volume_sources_json", "observations_json", "quality_flags"
            ):
                if key in item:
                    item[key.removesuffix("_json")] = json.loads(item.pop(key) or "[]")
        return result

    def cache_summaries(
        self,
        limit: int = 200,
        *,
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_symbols = sorted({
            str(symbol).strip().upper()
            for symbol in (symbols or [])
            if str(symbol).strip()
        })
        if symbols is not None and not normalized_symbols:
            return []
        where_clause = ""
        params: list[Any] = []
        if normalized_symbols:
            placeholders = ",".join("?" for _ in normalized_symbols)
            where_clause = f"WHERE c.symbol IN ({placeholders})"
            params.extend(normalized_symbols)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.* FROM consensus_bars c
                JOIN (
                    SELECT symbol, interval, adjustment, MAX(bar_time) AS max_time
                    FROM consensus_bars GROUP BY symbol, interval, adjustment
                ) latest ON latest.symbol=c.symbol AND latest.interval=c.interval
                    AND latest.adjustment=c.adjustment AND latest.max_time=c.bar_time
                {where_clause}
                ORDER BY c.verified_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        summaries: list[dict[str, Any]] = []
        coverage = self.list_coverage(normalized_symbols or None)
        for row in rows:
            item = dict(row)
            sources = json.loads(item.pop("sources_json") or "[]")
            volume_sources = json.loads(item.pop("volume_sources_json") or "[]")
            observations = json.loads(item.pop("observations_json") or "[]")
            quality_flags = json.loads(item.pop("quality_flags") or "[]")
            matching = [
                entry for entry in coverage
                if entry["symbol"] == item["symbol"] and entry["interval"] == item["interval"]
                and entry["actual_adjustment"] == item["adjustment"]
            ]
            start_date = min((entry["min_bar_time"] for entry in matching), default=item["bar_time"])
            end_date = max((entry["max_bar_time"] for entry in matching), default=item["bar_time"])
            summaries.append(
                {
                    "file_name": f"{item['symbol']}__{item['interval']}__adj-{item['adjustment']}.json",
                    "path": f"sqlite://{self.path}#{item['symbol']}/{item['interval']}/{item['adjustment']}",
                    "symbol": item["symbol"],
                    "status": item["status"],
                    "consensus_close": item["close"],
                    "spread_pct": item["price_spread_pct"],
                    "requested_adjustment": item["adjustment"],
                    "actual_adjustment": item["adjustment"],
                    "source_adjustments": {
                        obs.get("actual_source", obs.get("source", "unknown")): {
                            "adjustment": obs.get("actual_adjustment", item["adjustment"]),
                            "confidence": obs.get("adjustment_confidence", "unknown"),
                        }
                        for obs in observations
                    },
                    "sources": sources,
                    "volume_sources": volume_sources,
                    "volume_status": item["volume_status"],
                    "volume_source_count": item["volume_source_count"],
                    "volume_unit": item["volume_unit"],
                    "observations": observations,
                    "quality_flags": quality_flags,
                    "interval": item["interval"],
                    "bar_time": item["bar_time"],
                    "verified_at": item["verified_at"],
                    "batch_id": item["batch_id"],
                    "start_date": start_date,
                    "end_date": end_date,
                    "source_count": item["source_count"],
                    "volume": item["volume"],
                    "amount": item["amount"],
                    "vwap": item["vwap"],
                    "volume_spread_pct": item["volume_spread_pct"],
                    "amount_spread_pct": item["amount_spread_pct"],
                    "modified_at": item["verified_at"],
                }
            )
        return summaries

    def prune_sessions(self, symbol: str, interval: str, adjustment: str, keep_sessions: int) -> int:
        with self.transaction() as conn:
            sessions = conn.execute(
                """
                SELECT DISTINCT session_date FROM source_bars
                WHERE symbol=? AND interval=? AND actual_adjustment=?
                ORDER BY session_date DESC LIMIT ?
                """,
                (symbol.upper(), interval, adjustment, keep_sessions),
            ).fetchall()
            if len(sessions) < keep_sessions:
                return 0
            cutoff = sessions[-1]["session_date"]
            affected_sources = conn.execute(
                """
                SELECT DISTINCT actual_source FROM source_bars
                WHERE symbol=? AND interval=? AND actual_adjustment=?
                """,
                (symbol.upper(), interval, adjustment),
            ).fetchall()
            cursor = conn.execute(
                "DELETE FROM source_bars WHERE symbol=? AND interval=? AND actual_adjustment=? AND session_date<?",
                (symbol.upper(), interval, adjustment, cutoff),
            )
            conn.execute(
                "DELETE FROM consensus_bars WHERE symbol=? AND interval=? AND adjustment=? AND session_date<?",
                (symbol.upper(), interval, adjustment, cutoff),
            )
            for source_row in affected_sources:
                source = source_row["actual_source"]
                coverage = conn.execute(
                    """
                    SELECT MIN(bar_time) AS min_time, MAX(bar_time) AS max_time, COUNT(*) AS row_count
                    FROM source_bars
                    WHERE symbol=? AND actual_source=? AND interval=? AND actual_adjustment=?
                    """,
                    (symbol.upper(), source, interval, adjustment),
                ).fetchone()
                if not coverage["row_count"]:
                    conn.execute(
                        "DELETE FROM cache_coverage WHERE symbol=? AND actual_source=? AND interval=? AND actual_adjustment=?",
                        (symbol.upper(), source, interval, adjustment),
                    )
                    continue
                conn.execute(
                    """
                    UPDATE cache_coverage SET min_bar_time=?, max_bar_time=?, row_count=?
                    WHERE symbol=? AND actual_source=? AND interval=? AND actual_adjustment=?
                    """,
                    (coverage["min_time"], coverage["max_time"], coverage["row_count"],
                     symbol.upper(), source, interval, adjustment),
                )
            return int(cursor.rowcount)
