"""SQLite persistence and atomic state transitions for portfolio monitoring."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from src.config.paths import get_runtime_root
from src.portfolio.state import normalize_symbol

from .evaluator import clear_for, condition_for
from .models import (
    PlanValidationError,
    utc_now,
    validate_plan,
    validate_plan_for_activation,
)
from .price_volume import target_distance_bps, target_reached


_BINDING_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_BINDING_CODE_LENGTH = 8
_BINDING_CODE_TTL_SECONDS = 600
_CN_TZ = ZoneInfo("Asia/Shanghai")


class StaleLeaderError(RuntimeError):
    """A runtime write was attempted with an expired fencing token or claim."""


def monitoring_db_path() -> Path:
    override = os.getenv("VIBE_TRADING_MONITORING_DB")
    if override:
        return Path(override).expanduser()
    return get_runtime_root() / "portfolio" / "monitoring" / "monitoring.sqlite3"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else fallback
    except json.JSONDecodeError:
        return fallback


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _normalize_autopilot_symbols(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("autopilot selected_symbols must be a list")
    normalized: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("autopilot selected_symbols must contain strings")
        symbol = normalize_symbol(item).upper()
        if not symbol:
            continue
        if not symbol.endswith((".SH", ".SZ", ".BJ")):
            raise ValueError(
                "自主监控目前只支持沪深京 A 股和场内 ETF，暂不支持："
                f"{symbol}"
            )
        normalized.add(symbol)
    return sorted(normalized)


def _normalize_binding_code(value: str) -> str:
    compact = "".join(character for character in str(value).upper() if character.isalnum())
    if len(compact) != _BINDING_CODE_LENGTH or any(
        character not in _BINDING_CODE_ALPHABET for character in compact
    ):
        raise ValueError("invalid or expired monitoring binding code")
    return compact


def _binding_code_hash(value: str) -> str:
    return hashlib.sha256(_normalize_binding_code(value).encode("ascii")).hexdigest()


def _timestamp_due(value: Any, now: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) <= now.astimezone(timezone.utc)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return round(float(ordered[index]), 3)


class MonitoringStore:
    """Durable domain store. All episode/event/outbox changes are atomic."""

    SUPPORTED_SCHEMA_VERSION = 10

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or monitoring_db_path()
        # Ephemeral progress is shared by the API service and runtime in the
        # same process; durable monitoring facts remain in SQLite.
        self.price_volume_backfills: dict[str, dict[str, Any]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current_schema_version = self._read_existing_schema_version()
        if current_schema_version > self.SUPPORTED_SCHEMA_VERSION:
            raise RuntimeError(
                "monitoring database schema version "
                f"{current_schema_version} is newer than supported version "
                f"{self.SUPPORTED_SCHEMA_VERSION}"
            )
        migration_backup = self._backup_before_migration(
            current_version=current_schema_version,
            target_version=self.SUPPORTED_SCHEMA_VERSION,
        )
        try:
            self.initialize()
            with self.connect() as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise RuntimeError(f"monitoring database integrity check failed: {integrity}")
        except Exception:
            if migration_backup is not None:
                self._restore_migration_backup(migration_backup)
            raise

    def _read_existing_schema_version(self) -> int:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0
        try:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=10) as connection:
                has_meta = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
                ).fetchone()
                if has_meta:
                    row = connection.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()
                    return int(row[0]) if row else 0
        except (sqlite3.Error, TypeError, ValueError):
            return 0
        return 0

    def _backup_before_migration(
        self,
        *,
        current_version: int,
        target_version: int,
    ) -> Path | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        if current_version >= target_version:
            return None
        backup_dir = self.path.parent / "migration_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_dir / (
            f"{self.path.stem}.schema-v{current_version}-to-v{target_version}.{stamp}.sqlite3"
        )
        with sqlite3.connect(self.path, timeout=10) as source:
            with sqlite3.connect(backup_path) as destination:
                source.backup(destination)
        return backup_path

    def _restore_migration_backup(self, backup_path: Path) -> None:
        # SQLite's backup API safely replaces the live database even when
        # Windows still has WAL sidecars open; raw file replacement does not.
        with sqlite3.connect(backup_path, timeout=10) as source:
            with sqlite3.connect(self.path, timeout=10) as destination:
                source.backup(destination)
                destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '1');

                CREATE TABLE IF NOT EXISTS delivery_targets (
                    target_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL DEFAULT 'p2p',
                    session_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    UNIQUE(channel, chat_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_delivery_binding_codes (
                    binding_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    claimed_at TEXT,
                    claimed_sender_id TEXT,
                    claimed_chat_id TEXT,
                    target_id TEXT,
                    FOREIGN KEY(target_id) REFERENCES delivery_targets(target_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_profiles (
                    profile_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL UNIQUE,
                    market TEXT NOT NULL,
                    instrument_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_plan_version INTEGER,
                    profile_revision INTEGER NOT NULL DEFAULT 1,
                    delivery_target_id TEXT,
                    input_snapshot_hash TEXT,
                    input_outdated INTEGER NOT NULL DEFAULT 0,
                    blocked_reasons_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    paused_at TEXT,
                    resume_at TEXT,
                    pause_reason TEXT,
                    closed_at TEXT,
                    last_quote_check_at TEXT,
                    last_success_at TEXT,
                    next_quote_run_at TEXT,
                    FOREIGN KEY(delivery_target_id) REFERENCES delivery_targets(target_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_plan_versions (
                    profile_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    plan_json TEXT NOT NULL,
                    evidence_manifest_json TEXT NOT NULL,
                    evidence_manifest_sha256 TEXT NOT NULL,
                    planner_input_sha256 TEXT NOT NULL,
                    planner_output_sha256 TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    data_as_of TEXT,
                    hard_valid_until TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    superseded_at TEXT,
                    PRIMARY KEY(profile_id, version),
                    FOREIGN KEY(profile_id) REFERENCES monitor_profiles(profile_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_rules (
                    rule_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    client_rule_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    target_intent TEXT,
                    target_level INTEGER,
                    alert_cue TEXT NOT NULL DEFAULT 'none',
                    parameters_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'armed',
                    confirmation_progress INTEGER NOT NULL DEFAULT 0,
                    armed_epoch INTEGER NOT NULL DEFAULT 1,
                    last_condition_value INTEGER,
                    last_observation_id TEXT,
                    last_bar_time TEXT,
                    last_triggered_at TEXT,
                    cooldown_until TEXT,
                    valid_until TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(profile_id, plan_version, client_rule_id),
                    FOREIGN KEY(profile_id, plan_version)
                        REFERENCES monitor_plan_versions(profile_id, version)
                );

                CREATE TABLE IF NOT EXISTS monitor_signal_states (
                    signal_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    signal_type TEXT NOT NULL,
                    client_rule_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    episode INTEGER NOT NULL DEFAULT 0,
                    release_progress INTEGER NOT NULL DEFAULT 0,
                    last_bar_time TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(profile_id, plan_version, signal_type, client_rule_id),
                    FOREIGN KEY(profile_id, plan_version)
                        REFERENCES monitor_plan_versions(profile_id, version)
                );

                CREATE TABLE IF NOT EXISTS monitor_observations (
                    observation_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    data_as_of TEXT,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    UNIQUE(profile_id, domain, source_key, data_as_of, payload_hash),
                    FOREIGN KEY(profile_id) REFERENCES monitor_profiles(profile_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_events (
                    event_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    rule_id TEXT,
                    armed_epoch INTEGER,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    facts_json TEXT NOT NULL,
                    observation_id TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    episode_id TEXT,
                    phase TEXT,
                    outcome TEXT,
                    volume_verdict TEXT,
                    UNIQUE(profile_id, plan_version, rule_id, armed_epoch),
                    FOREIGN KEY(profile_id) REFERENCES monitor_profiles(profile_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_report_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    report_ref TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_message_id TEXT,
                    artifact_id TEXT,
                    revision INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    quality_status TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    data_as_of TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(report_ref, revision, body_sha256)
                );

                CREATE TABLE IF NOT EXISTS monitor_planner_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    requested_symbols_json TEXT NOT NULL,
                    report_refs_json TEXT NOT NULL DEFAULT '{}',
                    research_policy TEXT NOT NULL,
                    delivery_target_id TEXT,
                    force_fresh INTEGER NOT NULL DEFAULT 1,
                    activation_mode TEXT NOT NULL DEFAULT 'manual',
                    trigger_type TEXT,
                    evidence_fingerprint TEXT,
                    autopilot_trigger_id TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_autopilot_config (
                    config_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    activation_mode TEXT NOT NULL DEFAULT 'autonomous',
                    research_policy TEXT NOT NULL DEFAULT 'if_needed',
                    trigger_types_json TEXT NOT NULL DEFAULT '[]',
                    selected_symbols_json TEXT NOT NULL DEFAULT '[]',
                    daily_close_enabled INTEGER NOT NULL DEFAULT 1,
                    delivery_target_id TEXT,
                    runtime_mode TEXT NOT NULL DEFAULT 'shadow',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(delivery_target_id) REFERENCES delivery_targets(target_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_autopilot_triggers (
                    trigger_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    evidence_fingerprint TEXT,
                    planner_job_id TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    UNIQUE(symbol, trigger_type, dedupe_key),
                    FOREIGN KEY(planner_job_id) REFERENCES monitor_planner_jobs(job_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_evidence_bundles (
                    bundle_id TEXT PRIMARY KEY,
                    job_id TEXT,
                    trigger_id TEXT,
                    symbol TEXT NOT NULL,
                    evidence_fingerprint TEXT NOT NULL,
                    bundle_json TEXT NOT NULL,
                    bundle_sha256 TEXT NOT NULL,
                    data_as_of TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(symbol, evidence_fingerprint),
                    FOREIGN KEY(job_id) REFERENCES monitor_planner_jobs(job_id),
                    FOREIGN KEY(trigger_id) REFERENCES monitor_autopilot_triggers(trigger_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_condition_coverage (
                    coverage_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    scenario_id TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    coverage_status TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    reason TEXT,
                    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    UNIQUE(profile_id, plan_version, scenario_id, condition_id),
                    FOREIGN KEY(profile_id,plan_version)
                        REFERENCES monitor_plan_versions(profile_id,version)
                );

                CREATE TABLE IF NOT EXISTS monitor_recommendations (
                    recommendation_id TEXT PRIMARY KEY,
                    profile_id TEXT,
                    plan_version INTEGER,
                    episode_id TEXT,
                    symbol TEXT NOT NULL,
                    scenario_id TEXT,
                    scenario_fingerprint TEXT,
                    status TEXT NOT NULL,
                    action TEXT NOT NULL,
                    recommendation_json TEXT NOT NULL,
                    recommendation_sha256 TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    feedback_status TEXT NOT NULL DEFAULT 'pending',
                    acknowledged_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(episode_id, scenario_fingerprint),
                    FOREIGN KEY(profile_id) REFERENCES monitor_profiles(profile_id),
                    FOREIGN KEY(episode_id) REFERENCES monitor_watch_episodes(episode_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_risk_preferences (
                    symbol TEXT PRIMARY KEY,
                    preference_json TEXT NOT NULL,
                    preference_sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_decision_choices (
                    choice_record_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    choice_id TEXT NOT NULL,
                    decision_revision INTEGER NOT NULL,
                    evidence_fingerprint TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    choice_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_condition_order_drafts (
                    draft_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    draft_sha256 TEXT NOT NULL,
                    evidence_fingerprint TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    cancelled_at TEXT
                );

                CREATE TABLE IF NOT EXISTS monitor_planner_job_items (
                    job_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_ref TEXT,
                    report_snapshot_id TEXT,
                    research_snapshot_id TEXT,
                    research_date TEXT,
                    profile_id TEXT,
                    plan_version INTEGER,
                    blocked_reasons_json TEXT NOT NULL DEFAULT '[]',
                    validation_errors_json TEXT NOT NULL DEFAULT '[]',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, symbol),
                    FOREIGN KEY(job_id) REFERENCES monitor_planner_jobs(job_id),
                    FOREIGN KEY(report_snapshot_id) REFERENCES monitor_report_snapshots(snapshot_id),
                    FOREIGN KEY(research_snapshot_id) REFERENCES monitor_report_snapshots(snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_watch_episodes (
                    episode_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    rule_id TEXT NOT NULL,
                    client_rule_id TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    state TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    outcome TEXT,
                    started_at TEXT NOT NULL,
                    approach_observation_id TEXT,
                    first_cross_observation_id TEXT,
                    terminal_observation_id TEXT,
                    first_cross_at TEXT,
                    resolved_at TEXT,
                    observed_bars INTEGER NOT NULL DEFAULT 0,
                    approach_notified INTEGER NOT NULL DEFAULT 0,
                    result_notified INTEGER NOT NULL DEFAULT 0,
                    volume_verdict TEXT,
                    facts_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(profile_id) REFERENCES monitor_profiles(profile_id),
                    UNIQUE(profile_id, plan_version, rule_id, session_date, started_at)
                );

                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    delivery_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    delivery_target_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claimed_at TEXT,
                    delivered_at TEXT,
                    remote_message_id TEXT,
                    provider TEXT,
                    provider_request_id TEXT,
                    accepted_at TEXT,
                    receipt_status TEXT,
                    error TEXT,
                    delivery_mode TEXT NOT NULL DEFAULT 'deliver',
                    would_deliver INTEGER NOT NULL DEFAULT 0,
                    suppressed_at TEXT,
                    suppression_reason TEXT,
                    claim_owner_id TEXT,
                    claim_fencing_token INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, delivery_target_id),
                    FOREIGN KEY(event_id) REFERENCES monitor_events(event_id),
                    FOREIGN KEY(delivery_target_id) REFERENCES delivery_targets(target_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_draft_batches (
                    batch_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    requested_symbols_json TEXT NOT NULL,
                    delivery_target_id TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS monitor_draft_batch_items (
                    batch_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    profile_id TEXT,
                    plan_version INTEGER,
                    blocked_reasons_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    PRIMARY KEY(batch_id, symbol),
                    FOREIGN KEY(batch_id) REFERENCES monitor_draft_batches(batch_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_leases (
                    lease_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_profile_claims (
                    profile_id TEXT PRIMARY KEY,
                    tick_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(profile_id) REFERENCES monitor_profiles(profile_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_profile_tick_outcomes (
                    tick_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(tick_id, profile_id),
                    FOREIGN KEY(profile_id) REFERENCES monitor_profiles(profile_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_health_episodes (
                    episode_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    opened_at TEXT,
                    recovered_at TEXT,
                    FOREIGN KEY(profile_id) REFERENCES monitor_profiles(profile_id)
                );

                CREATE TABLE IF NOT EXISTS monitor_runtime_ticks (
                    tick_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    due_profiles INTEGER NOT NULL DEFAULT 0,
                    all_due_profiles INTEGER NOT NULL DEFAULT 0,
                    evaluated_profiles INTEGER NOT NULL DEFAULT 0,
                    blocked_profiles INTEGER NOT NULL DEFAULT 0,
                    supported_blocked_profiles INTEGER NOT NULL DEFAULT 0,
                    outcome_profiles INTEGER NOT NULL DEFAULT 0,
                    outcome_contract_version INTEGER NOT NULL DEFAULT 0,
                    events_created INTEGER NOT NULL DEFAULT 0,
                    duplicate_events INTEGER NOT NULL DEFAULT 0,
                    shadow_suppressed INTEGER NOT NULL DEFAULT 0,
                    schedule_lag_ms REAL,
                    closed_session_due_profiles INTEGER NOT NULL DEFAULT 0,
                    closed_session_backlog_lag_ms REAL,
                    bar_lag_ms REAL,
                    database_size_bytes INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS monitor_runtime_counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_maintenance_runs (
                    maintenance_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_monitor_profiles_due
                    ON monitor_profiles(status, next_quote_run_at);
                CREATE INDEX IF NOT EXISTS idx_monitor_events_latest
                    ON monitor_events(first_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_events_cursor
                    ON monitor_events(first_seen_at, event_id);
                CREATE INDEX IF NOT EXISTS idx_monitor_outbox_pending
                    ON delivery_outbox(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_monitor_binding_codes_pending
                    ON monitor_delivery_binding_codes(status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_monitor_runtime_ticks_finished
                    ON monitor_runtime_ticks(finished_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_maintenance_started
                    ON monitor_maintenance_runs(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_signal_states_profile
                    ON monitor_signal_states(profile_id, plan_version);
                CREATE INDEX IF NOT EXISTS idx_monitor_observations_recent
                    ON monitor_observations(domain, observed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_report_candidates
                    ON monitor_report_snapshots(symbol, generated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_planner_jobs_status
                    ON monitor_planner_jobs(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_monitor_autopilot_triggers_status
                    ON monitor_autopilot_triggers(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_monitor_evidence_symbol
                    ON monitor_evidence_bundles(symbol, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_recommendations_recent
                    ON monitor_recommendations(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_decision_choices_recent
                    ON monitor_decision_choices(symbol, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_condition_drafts_recent
                    ON monitor_condition_order_drafts(symbol, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_watch_episode_active
                    ON monitor_watch_episodes(profile_id, plan_version, state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_monitor_profile_outcomes_created
                    ON monitor_profile_tick_outcomes(created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_health_episode_active
                    ON monitor_health_episodes(profile_id, kind)
                    WHERE status IN ('probing','open');
                """
            )
            delivery_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(delivery_outbox)").fetchall()
            }
            delivery_migrations = {
                "delivery_mode": "TEXT NOT NULL DEFAULT 'deliver'",
                "would_deliver": "INTEGER NOT NULL DEFAULT 0",
                "suppressed_at": "TEXT",
                "suppression_reason": "TEXT",
                "provider": "TEXT",
                "provider_request_id": "TEXT",
                "accepted_at": "TEXT",
                "receipt_status": "TEXT",
                "claim_owner_id": "TEXT",
                "claim_fencing_token": "INTEGER",
            }
            for column, declaration in delivery_migrations.items():
                if column not in delivery_columns:
                    connection.execute(f"ALTER TABLE delivery_outbox ADD COLUMN {column} {declaration}")

            event_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(monitor_events)").fetchall()
            }
            event_migrations = {
                "episode_id": "TEXT",
                "phase": "TEXT",
                "outcome": "TEXT",
                "volume_verdict": "TEXT",
            }
            for column, declaration in event_migrations.items():
                if column not in event_columns:
                    connection.execute(f"ALTER TABLE monitor_events ADD COLUMN {column} {declaration}")

            planner_job_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(monitor_planner_jobs)").fetchall()
            }
            planner_job_migrations = {
                "activation_mode": "TEXT NOT NULL DEFAULT 'manual'",
                "trigger_type": "TEXT",
                "evidence_fingerprint": "TEXT",
                "autopilot_trigger_id": "TEXT",
            }
            for column, declaration in planner_job_migrations.items():
                if column not in planner_job_columns:
                    connection.execute(
                        f"ALTER TABLE monitor_planner_jobs ADD COLUMN {column} {declaration}"
                    )

            autopilot_config_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(monitor_autopilot_config)"
                ).fetchall()
            }
            if "selected_symbols_json" not in autopilot_config_columns:
                connection.execute(
                    "ALTER TABLE monitor_autopilot_config "
                    "ADD COLUMN selected_symbols_json TEXT NOT NULL DEFAULT '[]'"
                )

            rule_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(monitor_rules)").fetchall()
            }
            rule_migrations = {
                "target_intent": "TEXT",
                "target_level": "INTEGER",
                "alert_cue": "TEXT NOT NULL DEFAULT 'none'",
            }
            for column, declaration in rule_migrations.items():
                if column not in rule_columns:
                    connection.execute(f"ALTER TABLE monitor_rules ADD COLUMN {column} {declaration}")

            runtime_tick_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(monitor_runtime_ticks)"
                ).fetchall()
            }
            runtime_tick_migrations = {
                "duplicate_events": "INTEGER NOT NULL DEFAULT 0",
                "all_due_profiles": "INTEGER NOT NULL DEFAULT 0",
                "supported_blocked_profiles": "INTEGER NOT NULL DEFAULT 0",
                "outcome_profiles": "INTEGER NOT NULL DEFAULT 0",
                "outcome_contract_version": "INTEGER NOT NULL DEFAULT 0",
                "closed_session_due_profiles": "INTEGER NOT NULL DEFAULT 0",
                "closed_session_backlog_lag_ms": "REAL",
            }
            for column, declaration in runtime_tick_migrations.items():
                if column not in runtime_tick_columns:
                    connection.execute(
                        f"ALTER TABLE monitor_runtime_ticks ADD COLUMN {column} {declaration}"
                    )

            lease_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(runtime_leases)").fetchall()
            }
            if "fencing_token" not in lease_columns:
                connection.execute(
                    "ALTER TABLE runtime_leases "
                    "ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 1"
                )

            # v4 stored target metadata only inside the immutable plan JSON.
            # Backfill by the stable client_rule_id while deliberately keeping
            # every legacy cue disabled so an upgrade can never start audio.
            plan_rows = connection.execute(
                "SELECT profile_id,version,plan_json FROM monitor_plan_versions"
            ).fetchall()
            for plan_row in plan_rows:
                plan = _loads(plan_row["plan_json"], {})
                if not isinstance(plan, dict):
                    continue
                for rule in plan.get("market_rules") or []:
                    if not isinstance(rule, dict) or not rule.get("client_rule_id"):
                        continue
                    kind = str(rule.get("kind") or "")
                    target_intent = rule.get("target_intent")
                    target_level = rule.get("target_level")
                    if kind.startswith("price_"):
                        # The earliest plan schemas did not persist target
                        # metadata.  Use the same deterministic defaults as
                        # validate_plan so their durable rule rows still expose
                        # a complete, typed event contract after migration.
                        target_intent = target_intent or (
                            "breakout" if kind == "price_cross_above" else "watch"
                        )
                        target_level = target_level if target_level is not None else 1
                    connection.execute(
                        """UPDATE monitor_rules
                           SET target_intent=COALESCE(target_intent,?),
                               target_level=COALESCE(target_level,?)
                           WHERE profile_id=? AND plan_version=? AND client_rule_id=?""",
                        (
                            target_intent,
                            target_level,
                            plan_row["profile_id"],
                            plan_row["version"],
                            str(rule["client_rule_id"]),
                        ),
                    )

            # Older databases could accumulate several reviewable drafts for
            # one profile.  Keep the newest review version and make every
            # older draft explicitly historical before enforcing the invariant
            # at the database boundary.
            connection.execute(
                """UPDATE monitor_plan_versions AS stale
                   SET status='superseded', superseded_at=COALESCE(superseded_at, ?)
                   WHERE stale.status='pending_review'
                     AND EXISTS (
                       SELECT 1 FROM monitor_plan_versions AS newer
                       WHERE newer.profile_id=stale.profile_id
                         AND newer.status='pending_review'
                         AND newer.version>stale.version
                     )""",
                (utc_now(),),
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_plan_single_pending
                   ON monitor_plan_versions(profile_id)
                   WHERE status='pending_review'"""
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '10')"
            )

    @staticmethod
    def _profile(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["blocked_reasons"] = _loads(value.pop("blocked_reasons_json", "[]"), [])
        value["input_outdated"] = bool(value.get("input_outdated"))
        return value

    @staticmethod
    def _latest_quote_snapshots(
        connection: sqlite3.Connection,
        profile_ids: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if profile_ids == []:
            return {}
        params: list[Any] = []
        profile_filter = ""
        if profile_ids is not None:
            placeholders = ",".join("?" for _ in profile_ids)
            profile_filter = f"AND profile_id IN ({placeholders})"
            params.extend(profile_ids)
        rows = connection.execute(
            f"""WITH ranked AS (
                   SELECT profile_id,observation_id,observed_at,data_as_of,status,payload_json,
                          ROW_NUMBER() OVER (
                              PARTITION BY profile_id
                              ORDER BY observed_at DESC, observation_id DESC
                          ) AS row_number
                   FROM monitor_observations
                   WHERE domain='quote' {profile_filter}
               )
               SELECT profile_id,observation_id,observed_at,data_as_of,status,payload_json,row_number
               FROM ranked WHERE row_number<=20
               ORDER BY profile_id,row_number""",
            params,
        ).fetchall()
        observations: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            payload = _loads(row["payload_json"], {})
            def payload_number(field: str) -> float | None:
                raw = payload.get(field)
                return (
                    float(raw)
                    if isinstance(raw, (int, float)) and not isinstance(raw, bool)
                    else None
                )

            price = payload_number("last_price")
            observations.setdefault(str(row["profile_id"]), []).append({
                "price": price,
                "observed_at": row["observed_at"],
                "data_as_of": row["data_as_of"],
                "status": row["status"],
                "interval": payload.get("interval"),
                "sources": payload.get("sources") or [],
                "session_open": payload_number("session_open"),
                "session_high": payload_number("session_high"),
                "session_low": payload_number("session_low"),
                "session_date": payload.get("session_date"),
                "volume_ratio": payload_number("volume_ratio"),
                "price_volume": (
                    dict(payload["price_volume"])
                    if isinstance(payload.get("price_volume"), dict)
                    else None
                ),
                "historical_price_volume": (
                    dict(payload["historical_price_volume"])
                    if isinstance(payload.get("historical_price_volume"), dict)
                    else None
                ),
                "price_volume_backfill": (
                    dict(payload["price_volume_backfill"])
                    if isinstance(payload.get("price_volume_backfill"), dict)
                    else None
                ),
            })
        snapshots: dict[str, dict[str, Any]] = {}
        for profile_id, recent in observations.items():
            latest = recent[0]
            previous = next(
                (
                    candidate
                    for candidate in recent[1:]
                    if candidate["interval"] == latest["interval"]
                    and candidate["price"] is not None
                    and (
                        not latest["data_as_of"]
                        or not candidate["data_as_of"]
                        or candidate["data_as_of"] != latest["data_as_of"]
                    )
                ),
                None,
            )
            previous_price = previous["price"] if previous else None
            price = latest["price"]
            if price is None or previous_price is None:
                change = None
                change_pct = None
                trend = "unknown"
            else:
                change = price - previous_price
                change_pct = (change / previous_price * 100) if previous_price else None
                tolerance = max(abs(previous_price) * 1e-8, 1e-10)
                trend = "flat" if abs(change) <= tolerance else "up" if change > 0 else "down"
            snapshots[profile_id] = {
                **latest,
                "previous_price": previous_price,
                "previous_data_as_of": previous["data_as_of"] if previous else None,
                "price_change": round(change, 8) if change is not None else None,
                "price_change_pct": round(change_pct, 4) if change_pct is not None else None,
                "trend": trend,
            }
        return snapshots

    @staticmethod
    def _plan(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["plan"] = _loads(value.pop("plan_json"), {})
        value["evidence_manifest"] = _loads(value.pop("evidence_manifest_json"), {})
        return value

    @staticmethod
    def _event(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["facts"] = _loads(value.pop("facts_json", "{}"), {})
        return value

    @staticmethod
    def _delivery(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["would_deliver"] = bool(value.get("would_deliver"))
        return value

    @staticmethod
    def _increment_counter(connection: sqlite3.Connection, name: str, amount: int = 1) -> None:
        now = utc_now()
        connection.execute(
            """INSERT INTO monitor_runtime_counters(name,value,updated_at) VALUES(?,?,?)
               ON CONFLICT(name) DO UPDATE SET value=value+excluded.value,updated_at=excluded.updated_at""",
            (name, amount, now),
        )

    def counter_value(self, name: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM monitor_runtime_counters WHERE name=?",
                (name,),
            ).fetchone()
        return int(row["value"]) if row else 0

    @staticmethod
    def _bind_target_in_connection(
        connection: sqlite3.Connection,
        *,
        channel: str,
        chat_id: str,
        chat_type: str,
        session_key: str,
        now: str,
    ) -> dict[str, Any]:
        target_id = uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO delivery_targets(target_id, channel, chat_id, chat_type, session_key, status, created_at)
            VALUES(?,?,?,?,?,'active',?)
            ON CONFLICT(channel, chat_id) DO UPDATE SET
                chat_type=excluded.chat_type, session_key=excluded.session_key,
                status='active', revoked_at=NULL
            """,
            (target_id, channel, chat_id, chat_type, session_key, now),
        )
        row = connection.execute(
            "SELECT * FROM delivery_targets WHERE channel=? AND chat_id=?",
            (channel, chat_id),
        ).fetchone()
        return dict(row) if row else {}

    def bind_target(
        self,
        *,
        channel: str,
        chat_id: str,
        chat_type: str = "p2p",
        session_key: str = "",
    ) -> dict[str, Any]:
        if not channel.strip() or not chat_id.strip():
            raise ValueError("channel and chat_id are required")
        now = utc_now()
        with self.transaction() as connection:
            return self._bind_target_in_connection(
                connection,
                channel=channel.strip(),
                chat_id=chat_id.strip(),
                chat_type=chat_type or "p2p",
                session_key=session_key,
                now=now,
            )

    @staticmethod
    def _expire_binding_codes(connection: sqlite3.Connection, now: str) -> None:
        connection.execute(
            """UPDATE monitor_delivery_binding_codes SET status='expired'
               WHERE status='pending' AND expires_at<=?""",
            (now,),
        )

    @staticmethod
    def _binding_attempt(
        row: sqlite3.Row | dict[str, Any],
        target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        value = dict(row)
        value.pop("code_hash", None)
        if target:
            value["target"] = target
        return value

    def create_binding_code(
        self,
        *,
        ttl_seconds: int = _BINDING_CODE_TTL_SECONDS,
    ) -> dict[str, Any]:
        ttl = max(60, min(int(ttl_seconds), 3600))
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=ttl)).isoformat()
        binding_id = uuid.uuid4().hex
        with self.transaction() as connection:
            self._expire_binding_codes(connection, now)
            for _ in range(10):
                raw = "".join(
                    secrets.choice(_BINDING_CODE_ALPHABET)
                    for _ in range(_BINDING_CODE_LENGTH)
                )
                try:
                    connection.execute(
                        """INSERT INTO monitor_delivery_binding_codes(
                           binding_id,code_hash,status,created_at,expires_at
                           ) VALUES(?,?,'pending',?,?)""",
                        (binding_id, _binding_code_hash(raw), now, expires_at),
                    )
                except sqlite3.IntegrityError:
                    binding_id = uuid.uuid4().hex
                    continue
                code = f"{raw[:4]}-{raw[4:]}"
                return {
                    "binding_id": binding_id,
                    "code": code,
                    "command": f"绑定监控 {code}",
                    "status": "pending",
                    "created_at": now,
                    "expires_at": expires_at,
                }
        raise RuntimeError("could not allocate a unique monitoring binding code")

    def get_binding_code(self, binding_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.transaction() as connection:
            self._expire_binding_codes(connection, now)
            row = connection.execute(
                "SELECT * FROM monitor_delivery_binding_codes WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if not row:
                return None
            target = None
            if row["target_id"]:
                target_row = connection.execute(
                    "SELECT * FROM delivery_targets WHERE target_id=?",
                    (row["target_id"],),
                ).fetchone()
                target = dict(target_row) if target_row else None
            return self._binding_attempt(row, target)

    def claim_binding_code(
        self,
        *,
        code: str,
        channel: str,
        chat_id: str,
        chat_type: str,
        sender_id: str,
        session_key: str = "",
    ) -> dict[str, Any]:
        if channel != "feishu" or chat_type not in {"p2p", "group"}:
            raise ValueError("monitoring delivery binding only supports Feishu chats")
        if not chat_id.strip() or not sender_id.strip():
            raise ValueError("chat_id and sender_id are required")
        code_hash = _binding_code_hash(code)
        now = utc_now()
        with self.transaction() as connection:
            self._expire_binding_codes(connection, now)
            row = connection.execute(
                """SELECT * FROM monitor_delivery_binding_codes
                   WHERE code_hash=? AND status='pending'""",
                (code_hash,),
            ).fetchone()
            if not row:
                raise ValueError("invalid or expired monitoring binding code")
            target = self._bind_target_in_connection(
                connection,
                channel=channel,
                chat_id=chat_id.strip(),
                chat_type=chat_type,
                session_key=session_key,
                now=now,
            )
            connection.execute(
                """UPDATE monitor_delivery_binding_codes SET
                   status='claimed',claimed_at=?,claimed_sender_id=?,claimed_chat_id=?,target_id=?
                   WHERE binding_id=? AND status='pending'""",
                (now, sender_id.strip(), chat_id.strip(), target["target_id"], row["binding_id"]),
            )
        return target

    def list_targets(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM delivery_targets ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def get_default_delivery_target_id(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='default_feishu_delivery_target_id'"
            ).fetchone()
            target_id = str(row["value"] or "").strip() if row else ""
            if not target_id:
                return None
            target = connection.execute(
                "SELECT status,channel FROM delivery_targets WHERE target_id=?",
                (target_id,),
            ).fetchone()
        if not target or target["status"] != "active" or target["channel"] != "feishu":
            return None
        return target_id

    def set_default_delivery_target(self, target_id: str | None) -> dict[str, Any]:
        normalized = str(target_id or "").strip()
        with self.transaction() as connection:
            if normalized:
                target = connection.execute(
                    "SELECT * FROM delivery_targets WHERE target_id=?",
                    (normalized,),
                ).fetchone()
                if not target or target["status"] != "active" or target["channel"] != "feishu":
                    raise ValueError("default Feishu delivery target is not active")
            connection.execute(
                """INSERT INTO schema_meta(key,value) VALUES('default_feishu_delivery_target_id',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (normalized,),
            )
        return self.get_delivery_settings()

    def get_delivery_settings(self) -> dict[str, Any]:
        targets = [
            item for item in self.list_targets()
            if item.get("channel") == "feishu"
        ]
        active = [item for item in targets if item.get("status") == "active"]
        default_target_id = self.get_default_delivery_target_id()
        effective_target_id = default_target_id or (
            str(active[0].get("target_id") or "") if len(active) == 1 else None
        )
        return {
            "targets": targets,
            "default_target_id": default_target_id,
            "effective_target_id": effective_target_id,
            "requires_selection": len(active) > 1 and not default_target_id,
        }

    def get_target(self, target_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_targets WHERE target_id=?",
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def revoke_target(self, target_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                "UPDATE delivery_targets SET status='revoked', revoked_at=? WHERE target_id=?",
                (utc_now(), target_id),
            )
            row = connection.execute("SELECT * FROM delivery_targets WHERE target_id=?", (target_id,)).fetchone()
        if not row:
            raise KeyError(target_id)
        if self.get_default_delivery_target_id() == target_id:
            self.set_default_delivery_target(None)
        return dict(row)

    def create_batch(self, symbols: list[str], delivery_target_id: str | None) -> str:
        batch_id = uuid.uuid4().hex
        normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO monitor_draft_batches VALUES(?, 'generating', ?, ?, ?, NULL)",
                (batch_id, _json(normalized), delivery_target_id, now),
            )
            connection.executemany(
                "INSERT INTO monitor_draft_batch_items(batch_id,symbol,status) VALUES(?,?,'generating')",
                [(batch_id, symbol) for symbol in normalized],
            )
        return batch_id

    def finish_batch_item(
        self,
        batch_id: str,
        symbol: str,
        *,
        status: str,
        profile_id: str | None = None,
        plan_version: int | None = None,
        blocked_reasons: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE monitor_draft_batch_items SET status=?, profile_id=?, plan_version=?,
                   blocked_reasons_json=?, error=? WHERE batch_id=? AND symbol=?""",
                (status, profile_id, plan_version, _json(blocked_reasons or []), error, batch_id, symbol),
            )

    def finish_batch(self, batch_id: str) -> None:
        with self.connect() as connection:
            statuses = [row[0] for row in connection.execute(
                "SELECT status FROM monitor_draft_batch_items WHERE batch_id=?", (batch_id,)
            ).fetchall()]
            status = "completed" if statuses and all(item == "ready" for item in statuses) else "completed_with_blocks"
            connection.execute(
                "UPDATE monitor_draft_batches SET status=?, completed_at=? WHERE batch_id=?",
                (status, utc_now(), batch_id),
            )

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            batch = connection.execute("SELECT * FROM monitor_draft_batches WHERE batch_id=?", (batch_id,)).fetchone()
            items = connection.execute(
                "SELECT * FROM monitor_draft_batch_items WHERE batch_id=? ORDER BY symbol", (batch_id,)
            ).fetchall()
        if not batch:
            return None
        value = dict(batch)
        value["requested_symbols"] = _loads(value.pop("requested_symbols_json"), [])
        value["items"] = []
        for row in items:
            item = dict(row)
            item["blocked_reasons"] = _loads(item.pop("blocked_reasons_json"), [])
            value["items"].append(item)
        return value

    @staticmethod
    def _report_snapshot(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["metadata"] = _loads(value.pop("metadata_json", "{}"), {})
        return value

    def save_report_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Freeze one immutable report body and return its durable snapshot."""

        body = str(snapshot.get("body") or "")
        if not body.strip():
            raise ValueError("report snapshot body is required")
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        report_ref = str(snapshot.get("report_ref") or "").strip()
        if not report_ref:
            raise ValueError("report_ref is required")
        revision = max(1, int(snapshot.get("revision") or 1))
        now = utc_now()
        snapshot_id = str(snapshot.get("snapshot_id") or uuid.uuid4().hex)
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT * FROM monitor_report_snapshots
                   WHERE report_ref=? AND revision=? AND body_sha256=?""",
                (report_ref, revision, body_sha256),
            ).fetchone()
            if existing:
                return self._report_snapshot(existing)
            connection.execute(
                """INSERT INTO monitor_report_snapshots(
                   snapshot_id,report_ref,report_type,symbol,title,source_id,
                   source_message_id,artifact_id,revision,body,body_sha256,
                   quality_status,generated_at,data_as_of,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    report_ref,
                    str(snapshot.get("report_type") or "single_stock_research"),
                    str(snapshot.get("symbol") or "").upper(),
                    str(snapshot.get("title") or "Untitled report"),
                    str(snapshot.get("source_id") or report_ref),
                    snapshot.get("source_message_id"),
                    snapshot.get("artifact_id"),
                    revision,
                    body,
                    body_sha256,
                    str(snapshot.get("quality_status") or "ready"),
                    str(snapshot.get("generated_at") or now),
                    str(snapshot.get("data_as_of") or snapshot.get("generated_at") or now),
                    _json(snapshot.get("metadata") or {}),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM monitor_report_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        assert row is not None
        return self._report_snapshot(row)

    def get_report_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_report_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        return self._report_snapshot(row) if row else None

    def list_report_snapshots(
        self,
        *,
        report_type: str | None = None,
        since: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if report_type:
            clauses.append("report_type=?")
            params.append(report_type)
        if since:
            clauses.append("generated_at>=?")
            params.append(since)
        params.append(max(1, min(int(limit), 2000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_report_snapshots WHERE "
                + " AND ".join(clauses)
                + " ORDER BY generated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._report_snapshot(row) for row in rows]

    def create_planner_job(
        self,
        *,
        symbols: list[str],
        report_refs: dict[str, str],
        research_policy: str,
        delivery_target_id: str | None,
        force_fresh: bool,
        activation_mode: str = "manual",
        trigger_type: str | None = None,
        evidence_fingerprint: str | None = None,
        autopilot_trigger_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        if not normalized:
            raise ValueError("at least one symbol is required")
        if activation_mode not in {"manual", "autonomous"}:
            raise ValueError("activation_mode must be manual or autonomous")
        job_id = uuid.uuid4().hex
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO monitor_planner_jobs(
                   job_id,status,requested_symbols_json,report_refs_json,research_policy,
                   delivery_target_id,force_fresh,activation_mode,trigger_type,
                   evidence_fingerprint,autopilot_trigger_id,created_at,updated_at
                   ) VALUES(?,'queued',?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    _json(normalized),
                    _json(report_refs),
                    research_policy,
                    delivery_target_id,
                    int(force_fresh),
                    activation_mode,
                    trigger_type,
                    evidence_fingerprint,
                    autopilot_trigger_id,
                    now,
                    now,
                ),
            )
            connection.executemany(
                """INSERT INTO monitor_planner_job_items(
                   job_id,symbol,status,report_ref,updated_at
                   ) VALUES(?,?,'queued',?,?)""",
                [
                    (job_id, symbol, report_refs.get(symbol), now)
                    for symbol in normalized
                ],
            )
        result = self.get_planner_job(job_id)
        assert result is not None
        return result

    @staticmethod
    def _planner_item(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["blocked_reasons"] = _loads(value.pop("blocked_reasons_json", "[]"), [])
        value["validation_errors"] = _loads(value.pop("validation_errors_json", "[]"), [])
        value["progress"] = _loads(value.pop("progress_json", "{}"), {})
        return value

    def get_planner_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            job = connection.execute(
                "SELECT * FROM monitor_planner_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            items = connection.execute(
                """SELECT * FROM monitor_planner_job_items
                   WHERE job_id=? ORDER BY symbol""",
                (job_id,),
            ).fetchall()
        if not job:
            return None
        value = dict(job)
        value["requested_symbols"] = _loads(value.pop("requested_symbols_json", "[]"), [])
        value["report_refs"] = _loads(value.pop("report_refs_json", "{}"), {})
        value["force_fresh"] = bool(value.get("force_fresh"))
        value["cancel_requested"] = bool(value.get("cancel_requested"))
        value["items"] = [self._planner_item(item) for item in items]
        return value

    def recover_planner_jobs(self) -> list[str]:
        """Requeue non-terminal work after a process restart."""

        now = utc_now()
        with self.transaction() as connection:
            rows = connection.execute(
                """SELECT job_id FROM monitor_planner_jobs
                   WHERE status IN ('queued','researching','planning','validating')
                     AND cancel_requested=0"""
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                connection.execute(
                    f"""UPDATE monitor_planner_jobs SET status='queued',updated_at=?
                        WHERE job_id IN ({placeholders})""",
                    (now, *job_ids),
                )
                connection.execute(
                    f"""UPDATE monitor_planner_job_items SET status='queued',updated_at=?
                        WHERE job_id IN ({placeholders})
                          AND status IN ('queued','researching','planning','validating')""",
                    (now, *job_ids),
                )
        return job_ids

    def planner_job_cancel_requested(self, job_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM monitor_planner_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def cancel_planner_job(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM monitor_planner_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(job_id)
            if str(row["status"]) in {"ready", "blocked", "failed", "cancelled"}:
                raise RuntimeError("planner job has already finished")
            connection.execute(
                """UPDATE monitor_planner_jobs SET status='cancelled',cancel_requested=1,
                   completed_at=?,updated_at=? WHERE job_id=?""",
                (now, now, job_id),
            )
            connection.execute(
                """UPDATE monitor_planner_job_items SET status='cancelled',completed_at=?,updated_at=?
                   WHERE job_id=? AND status IN ('queued','researching','planning','validating')""",
                (now, now, job_id),
            )
        result = self.get_planner_job(job_id)
        assert result is not None
        return result

    def update_planner_job_status(self, job_id: str, status: str) -> None:
        now = utc_now()
        completed_at = now if status in {"ready", "blocked", "failed", "cancelled"} else None
        with self.connect() as connection:
            connection.execute(
                """UPDATE monitor_planner_jobs SET status=?,started_at=COALESCE(started_at,?),
                   completed_at=?,updated_at=? WHERE job_id=?""",
                (status, now, completed_at, now, job_id),
            )

    def update_planner_item(
        self,
        job_id: str,
        symbol: str,
        *,
        status: str,
        report_ref: str | None = None,
        report_snapshot_id: str | None = None,
        research_snapshot_id: str | None = None,
        research_date: str | None = None,
        profile_id: str | None = None,
        plan_version: int | None = None,
        blocked_reasons: list[str] | None = None,
        validation_errors: list[str] | None = None,
        progress: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        completed_at = now if status in {"ready", "blocked", "failed", "cancelled"} else None
        with self.connect() as connection:
            connection.execute(
                """UPDATE monitor_planner_job_items SET
                   status=?,report_ref=COALESCE(?,report_ref),
                   report_snapshot_id=COALESCE(?,report_snapshot_id),
                   research_snapshot_id=COALESCE(?,research_snapshot_id),
                   research_date=COALESCE(?,research_date),profile_id=COALESCE(?,profile_id),
                   plan_version=COALESCE(?,plan_version),blocked_reasons_json=?,
                   validation_errors_json=?,progress_json=?,error=?,
                   started_at=COALESCE(started_at,?),completed_at=?,updated_at=?
                   WHERE job_id=? AND symbol=?""",
                (
                    status,
                    report_ref,
                    report_snapshot_id,
                    research_snapshot_id,
                    research_date,
                    profile_id,
                    plan_version,
                    _json(blocked_reasons or []),
                    _json(validation_errors or []),
                    _json(progress or {}),
                    error,
                    now,
                    completed_at,
                    now,
                    job_id,
                    symbol.upper(),
                ),
            )

    def requeue_planner_item_for_report(
        self,
        job_id: str,
        symbol: str,
        *,
        report_ref: str,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resume a planner item after its asynchronous report refresh finishes."""

        now = utc_now()
        normalized_symbol = symbol.upper()
        with self.transaction() as connection:
            job = connection.execute(
                "SELECT report_refs_json FROM monitor_planner_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            item = connection.execute(
                """SELECT 1 FROM monitor_planner_job_items
                   WHERE job_id=? AND symbol=?""",
                (job_id, normalized_symbol),
            ).fetchone()
            if not job or not item:
                raise KeyError(f"{job_id}:{normalized_symbol}")
            report_refs = _loads(job["report_refs_json"], {})
            report_refs[normalized_symbol] = report_ref
            connection.execute(
                """UPDATE monitor_planner_jobs SET status='queued',cancel_requested=0,
                   report_refs_json=?,completed_at=NULL,updated_at=? WHERE job_id=?""",
                (_json(report_refs), now, job_id),
            )
            connection.execute(
                """UPDATE monitor_planner_job_items SET status='queued',report_ref=?,
                   report_snapshot_id=NULL,research_snapshot_id=NULL,research_date=NULL,
                   profile_id=NULL,plan_version=NULL,attempt=attempt+1,
                   blocked_reasons_json='[]',validation_errors_json='[]',progress_json=?,
                   error=NULL,started_at=NULL,completed_at=NULL,updated_at=?
                   WHERE job_id=? AND symbol=?""",
                (
                    report_ref,
                    _json(progress or {}),
                    now,
                    job_id,
                    normalized_symbol,
                ),
            )
        result = self.get_planner_job(job_id)
        assert result is not None
        return result

    def retry_planner_item(self, job_id: str, symbol: str) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            job = connection.execute(
                "SELECT status FROM monitor_planner_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            item = connection.execute(
                """SELECT status FROM monitor_planner_job_items
                   WHERE job_id=? AND symbol=?""",
                (job_id, symbol.upper()),
            ).fetchone()
            if not job or not item:
                raise KeyError(f"{job_id}:{symbol}")
            if str(item["status"]) not in {"blocked", "failed", "cancelled"}:
                raise RuntimeError("only a terminal planner item can be retried")
            connection.execute(
                """UPDATE monitor_planner_jobs SET status='queued',cancel_requested=0,
                   completed_at=NULL,updated_at=? WHERE job_id=?""",
                (now, job_id),
            )
            connection.execute(
                """UPDATE monitor_planner_job_items SET status='queued',attempt=attempt+1,
                   blocked_reasons_json='[]',validation_errors_json='[]',progress_json='{}',
                   error=NULL,started_at=NULL,completed_at=NULL,updated_at=?
                   WHERE job_id=? AND symbol=?""",
                (now, job_id, symbol.upper()),
            )
        result = self.get_planner_job(job_id)
        assert result is not None
        return result

    def automatic_research_used(self, symbol: str, research_date: str, *, excluding_job_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM monitor_planner_job_items
                   WHERE symbol=? AND research_date=? AND job_id<>?
                     AND research_snapshot_id IS NOT NULL LIMIT 1""",
                (symbol.upper(), research_date, excluding_job_id),
            ).fetchone()
        return row is not None

    def save_draft(
        self,
        *,
        symbol: str,
        market: str,
        instrument_type: str,
        plan: dict[str, Any],
        evidence_manifest: dict[str, Any],
        input_snapshot_hash: str,
        delivery_target_id: str | None,
        model_id: str,
        created_by: str = "monitor_planner",
    ) -> tuple[str, int]:
        normalized = validate_plan(plan, expected_symbol=symbol)
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute("SELECT * FROM monitor_profiles WHERE symbol=?", (symbol,)).fetchone()
            if existing and existing["status"] == "closed":
                raise ValueError("closed monitor profile must be explicitly reopened")
            profile_id = str(existing["profile_id"]) if existing else uuid.uuid4().hex
            if not existing:
                connection.execute(
                    """INSERT INTO monitor_profiles(
                       profile_id,symbol,market,instrument_type,status,delivery_target_id,
                       input_snapshot_hash,created_at,updated_at,next_quote_run_at
                       ) VALUES(?,?,?,?, 'pending_review', ?,?,?,?,NULL)""",
                    (profile_id, symbol, market, instrument_type, delivery_target_id, input_snapshot_hash, now, now),
                )
                revision = 1
            else:
                revision = int(existing["profile_revision"]) + 1
                next_status = (
                    str(existing["status"])
                    if str(existing["status"]) in {"active", "paused"}
                    else "pending_review"
                )
                connection.execute(
                    """UPDATE monitor_profiles SET status=?, profile_revision=?,
                       delivery_target_id=COALESCE(?, delivery_target_id), input_snapshot_hash=?,
                       input_outdated=0,blocked_reasons_json='[]', updated_at=? WHERE profile_id=?""",
                    (next_status, revision, delivery_target_id, input_snapshot_hash, now, profile_id),
                )
            version = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM monitor_plan_versions WHERE profile_id=?", (profile_id,)
            ).fetchone()[0])
            connection.execute(
                """UPDATE monitor_plan_versions SET status='superseded',superseded_at=?
                   WHERE profile_id=? AND status IN ('draft','pending_review')""",
                (now, profile_id),
            )
            connection.execute(
                """INSERT INTO monitor_plan_versions(
                   profile_id,version,status,schema_version,plan_json,evidence_manifest_json,
                   evidence_manifest_sha256,planner_input_sha256,planner_output_sha256,
                   prompt_version,model_id,data_as_of,hard_valid_until,created_by,created_at
                   ) VALUES(?,?, 'pending_review', ?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    profile_id, version, int(normalized["schema_version"]),
                    _json(normalized), _json(evidence_manifest), _hash(evidence_manifest),
                    _hash({"symbol": symbol, "evidence": evidence_manifest}), _hash(normalized),
                    f"monitor-plan-v{normalized['schema_version']}", model_id, evidence_manifest.get("data_as_of"),
                    normalized["hard_valid_until"], created_by, now,
                ),
            )
            self._insert_rules(connection, profile_id, version, normalized, now)
        return profile_id, version

    @staticmethod
    def _insert_rules(connection: sqlite3.Connection, profile_id: str, version: int, plan: dict[str, Any], now: str) -> None:
        for rule in plan.get("market_rules") or []:
            connection.execute(
                """INSERT INTO monitor_rules(
                   rule_id,profile_id,plan_version,client_rule_id,kind,severity,
                   target_intent,target_level,alert_cue,parameters_json,
                   enabled,state,valid_until,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'armed',?,?)""",
                (
                    uuid.uuid4().hex, profile_id, version, rule["client_rule_id"], rule["kind"],
                    rule["severity"], rule.get("target_intent"), rule.get("target_level"),
                    str(rule.get("alert_cue") or "none"), _json(rule["parameters"]),
                    int(rule.get("enabled", True)),
                    rule.get("valid_until"), now,
                ),
            )

    @classmethod
    def _replace_plan_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        version: int,
        plan: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            """UPDATE monitor_plan_versions SET schema_version=?,plan_json=?,planner_output_sha256=?,
               prompt_version=?,hard_valid_until=? WHERE profile_id=? AND version=?""",
            (
                int(plan["schema_version"]),
                _json(plan),
                _hash(plan),
                f"monitor-plan-v{plan['schema_version']}",
                plan["hard_valid_until"],
                profile_id,
                version,
            ),
        )
        connection.execute(
            "DELETE FROM monitor_rules WHERE profile_id=? AND plan_version=?",
            (profile_id, version),
        )
        cls._insert_rules(connection, profile_id, version, plan, now)

    def save_blocked_profile(
        self, *, symbol: str, market: str, instrument_type: str, blocked_reasons: list[str]
    ) -> str:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM monitor_profiles WHERE symbol=?", (symbol,)).fetchone()
            if row and row["status"] == "closed":
                return str(row["profile_id"])
            profile_id = str(row["profile_id"]) if row else uuid.uuid4().hex
            if row:
                next_status = (
                    str(row["status"])
                    if str(row["status"]) in {"active", "paused"}
                    else "drafting"
                )
                connection.execute(
                    """UPDATE monitor_profiles SET status=?, blocked_reasons_json=?,
                       profile_revision=profile_revision+1, updated_at=? WHERE profile_id=?""",
                    (next_status, _json(blocked_reasons), now, profile_id),
                )
            else:
                connection.execute(
                    """INSERT INTO monitor_profiles(
                       profile_id,symbol,market,instrument_type,status,blocked_reasons_json,created_at,updated_at
                       ) VALUES(?,?,?,?, 'drafting', ?,?,?)""",
                    (profile_id, symbol, market, instrument_type, _json(blocked_reasons), now, now),
                )
        return profile_id

    def list_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM monitor_profiles ORDER BY updated_at DESC").fetchall()
            plan_rows = connection.execute(
                """SELECT * FROM monitor_plan_versions
                   WHERE status IN ('active','pending_review')
                   ORDER BY profile_id, version DESC"""
            ).fetchall()
            quote_snapshots = self._latest_quote_snapshots(
                connection,
                [str(row["profile_id"]) for row in rows],
            )
        plans_by_key = {
            (str(plan["profile_id"]), int(plan["version"])): plan
            for plan in plan_rows
        }
        latest_pending: dict[str, sqlite3.Row] = {}
        for plan in plan_rows:
            if plan["status"] == "pending_review":
                latest_pending.setdefault(str(plan["profile_id"]), plan)

        profiles: list[dict[str, Any]] = []
        for row in rows:
            value = self._profile(row)
            profile_id = str(row["profile_id"])
            active_version = row["active_plan_version"]
            selected = (
                plans_by_key.get((profile_id, int(active_version)))
                if active_version is not None
                else latest_pending.get(profile_id) if row["status"] == "pending_review" else None
            )
            value["display_plan"] = self._plan(selected) if selected else None
            value["last_quote"] = quote_snapshots.get(profile_id)
            profiles.append(value)
        return profiles

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM monitor_profiles WHERE profile_id=?", (profile_id,)).fetchone()
            plans = connection.execute(
                "SELECT * FROM monitor_plan_versions WHERE profile_id=? ORDER BY version DESC", (profile_id,)
            ).fetchall()
            quote_snapshot = self._latest_quote_snapshots(connection, [profile_id]).get(profile_id)
            episodes = connection.execute(
                """SELECT * FROM monitor_watch_episodes
                   WHERE profile_id=? ORDER BY started_at DESC LIMIT 50""",
                (profile_id,),
            ).fetchall()
        if not row:
            return None
        value = self._profile(row)
        value["plans"] = [self._plan(plan) for plan in plans]
        value["last_quote"] = quote_snapshot
        value["watch_episodes"] = []
        for episode in episodes:
            episode_value = dict(episode)
            episode_value["facts"] = _loads(episode_value.pop("facts_json", "{}"), {})
            episode_value["approach_notified"] = bool(episode_value.get("approach_notified"))
            episode_value["result_notified"] = bool(episode_value.get("result_notified"))
            value["watch_episodes"].append(episode_value)
        return value

    def get_profile_by_symbol(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT profile_id FROM monitor_profiles WHERE symbol=?", (symbol.upper(),)).fetchone()
        return self.get_profile(str(row[0])) if row else None

    def get_plan(self, profile_id: str, version: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_plan_versions WHERE profile_id=? AND version=?", (profile_id, version)
            ).fetchone()
        return self._plan(row) if row else None

    def create_preopen_notices(
        self,
        *,
        market_date: str,
        first_check_at: str,
        delivery_mode: str,
        lease_guard: dict[str, Any],
        allowed_profile_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Create one durable 09:00 notice per Feishu target and trading day."""

        now = utc_now()
        created_event_ids: list[str] = []
        with self.transaction() as connection:
            self._assert_fenced_write(
                connection,
                lease_key=str(lease_guard["lease_key"]),
                owner_id=str(lease_guard["owner_id"]),
                fencing_token=int(lease_guard["fencing_token"]),
            )
            rows = connection.execute(
                """SELECT p.* FROM monitor_profiles p
                   JOIN delivery_targets t ON t.target_id=p.delivery_target_id
                   WHERE p.status='active' AND p.active_plan_version IS NOT NULL
                     AND p.market IN ('SH','SZ','BJ') AND t.status='active'
                   ORDER BY p.delivery_target_id,p.symbol"""
            ).fetchall()
            grouped: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                profile_id = str(row["profile_id"])
                if allowed_profile_ids is not None and profile_id not in allowed_profile_ids:
                    continue
                grouped.setdefault(str(row["delivery_target_id"]), []).append(row)

            for target_id, profiles in grouped.items():
                event_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vibe-trading:monitoring-preopen:{market_date}:{target_id}",
                ).hex
                if connection.execute(
                    "SELECT 1 FROM monitor_events WHERE event_id=?",
                    (event_id,),
                ).fetchone():
                    continue
                representative = profiles[0]
                symbols = [str(profile["symbol"]) for profile in profiles]
                connection.execute(
                    """INSERT INTO monitor_events(
                           event_id,profile_id,symbol,plan_version,rule_id,armed_epoch,kind,
                           status,severity,title,summary,facts_json,observation_id,
                           first_seen_at,last_seen_at
                       ) VALUES(?,?,?,?,?,1,'monitoring_preopen_notice','confirmed','info',?,?,?,NULL,?,?)""",
                    (
                        event_id,
                        representative["profile_id"],
                        representative["symbol"],
                        int(representative["active_plan_version"]),
                        f"preopen:{target_id}:{market_date}",
                        "AI 持仓监控盘前提示",
                        "今日将在 09:35 开始首轮行情检查；只有触发监控规则时才会发送信号提醒。",
                        _json({
                            "market_date": market_date,
                            "first_check_at": first_check_at,
                            "active_profile_count": len(profiles),
                            "symbols": symbols,
                            "delivery_target_id": target_id,
                            "delivery_mode": delivery_mode,
                        }),
                        now,
                        now,
                    ),
                )
                delivery_status = "shadow_suppressed" if delivery_mode == "shadow" else "pending"
                connection.execute(
                    """INSERT INTO delivery_outbox(
                           delivery_id,event_id,delivery_target_id,status,delivery_mode,
                           would_deliver,suppressed_at,suppression_reason,created_at,updated_at
                       ) VALUES(?,?,?,?,?,1,?,?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        event_id,
                        target_id,
                        delivery_status,
                        delivery_mode,
                        now if delivery_mode == "shadow" else None,
                        "shadow_mode" if delivery_mode == "shadow" else None,
                        now,
                        now,
                    ),
                )
                if delivery_mode == "shadow":
                    self._increment_counter(connection, "shadow_suppressed_delivery_count")
                created_event_ids.append(event_id)
        return [event for event_id in created_event_ids if (event := self.get_event(event_id))]

    def update_draft(self, profile_id: str, version: int, plan: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            profile = connection.execute("SELECT * FROM monitor_profiles WHERE profile_id=?", (profile_id,)).fetchone()
            current = connection.execute(
                "SELECT * FROM monitor_plan_versions WHERE profile_id=? AND version=?", (profile_id, version)
            ).fetchone()
            if not profile or not current:
                raise KeyError(profile_id)
            if int(profile["profile_revision"]) != expected_revision:
                raise RuntimeError("profile revision conflict")
            if current["status"] not in {"draft", "pending_review"}:
                raise ValueError("only a draft plan can be edited")
            normalized = validate_plan(plan, expected_symbol=str(profile["symbol"]))
            self._replace_plan_in_transaction(
                connection,
                profile_id=profile_id,
                version=version,
                plan=normalized,
                now=now,
            )
            connection.execute(
                "UPDATE monitor_profiles SET profile_revision=profile_revision+1, updated_at=? WHERE profile_id=?",
                (now, profile_id),
            )
        value = self.get_plan(profile_id, version)
        assert value is not None
        return value

    @staticmethod
    def _activate_in_transaction(
        connection: sqlite3.Connection,
        *,
        profile: sqlite3.Row,
        plan_row: sqlite3.Row,
        plan: dict[str, Any],
        max_active: int,
        now: str,
    ) -> None:
        profile_id = str(profile["profile_id"])
        version = int(plan_row["version"])
        if profile["status"] == "closed":
            raise ValueError("closed monitor cannot be activated")
        if (
            plan_row["status"] == "active"
            and profile["status"] == "active"
            and profile["active_plan_version"] == version
        ):
            return
        if plan_row["status"] not in {"draft", "pending_review"}:
            raise ValueError("only a pending review plan can be activated")
        if not profile["delivery_target_id"]:
            raise ValueError("an active Feishu delivery target is required")
        target = connection.execute(
            "SELECT status FROM delivery_targets WHERE target_id=?",
            (profile["delivery_target_id"],),
        ).fetchone()
        if not target or target["status"] != "active":
            raise ValueError("the configured delivery target is not active")
        if not [rule for rule in plan["market_rules"] if rule.get("enabled", True)]:
            raise PlanValidationError("at least one enabled market rule is required")
        active_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM monitor_profiles WHERE status='active' AND profile_id<>?",
                (profile_id,),
            ).fetchone()[0]
        )
        if active_count >= max_active:
            raise OverflowError(f"active monitor limit reached ({max_active})")
        connection.execute(
            """UPDATE monitor_plan_versions SET status='superseded', superseded_at=?
               WHERE profile_id=? AND status IN ('active','draft','pending_review') AND version<>?""",
            (now, profile_id, version),
        )
        connection.execute(
            """UPDATE monitor_plan_versions SET status='active', activated_at=?, superseded_at=NULL
               WHERE profile_id=? AND version=?""",
            (now, profile_id, version),
        )
        connection.execute(
            """UPDATE monitor_profiles SET status='active', active_plan_version=?,
               profile_revision=profile_revision+1, blocked_reasons_json='[]',
               paused_at=NULL,resume_at=NULL,pause_reason=NULL,closed_at=NULL,
               next_quote_run_at=?,updated_at=? WHERE profile_id=?""",
            (version, now, now, profile_id),
        )

    def activate(
        self,
        profile_id: str,
        version: int,
        *,
        max_active: int,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            profile = connection.execute(
                "SELECT * FROM monitor_profiles WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            plan_row = connection.execute(
                "SELECT * FROM monitor_plan_versions WHERE profile_id=? AND version=?",
                (profile_id, version),
            ).fetchone()
            if not profile or not plan_row:
                raise KeyError(profile_id)
            if (
                expected_revision is not None
                and int(profile["profile_revision"]) != expected_revision
            ):
                raise RuntimeError("profile revision conflict")
            already_active = (
                plan_row["status"] == "active"
                and profile["status"] == "active"
                and profile["active_plan_version"] == version
            )
            validator = validate_plan if already_active else validate_plan_for_activation
            plan = validator(
                _loads(plan_row["plan_json"], {}),
                expected_symbol=str(profile["symbol"]),
            )
            self._activate_in_transaction(
                connection,
                profile=profile,
                plan_row=plan_row,
                plan=plan,
                max_active=max_active,
                now=now,
            )
        value = self.get_profile(profile_id)
        assert value is not None
        return value

    def save_and_activate(
        self,
        profile_id: str,
        version: int,
        plan: dict[str, Any],
        expected_revision: int,
        *,
        max_active: int,
    ) -> dict[str, Any]:
        """Atomically persist the reviewed draft and make that exact revision active."""

        now = utc_now()
        with self.transaction() as connection:
            profile = connection.execute(
                "SELECT * FROM monitor_profiles WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            plan_row = connection.execute(
                "SELECT * FROM monitor_plan_versions WHERE profile_id=? AND version=?",
                (profile_id, version),
            ).fetchone()
            if not profile or not plan_row:
                raise KeyError(profile_id)
            if int(profile["profile_revision"]) != expected_revision:
                raise RuntimeError("profile revision conflict")
            if plan_row["status"] not in {"draft", "pending_review"}:
                raise ValueError("only a draft plan can be saved and activated")
            normalized = validate_plan_for_activation(
                plan,
                expected_symbol=str(profile["symbol"]),
            )
            self._replace_plan_in_transaction(
                connection,
                profile_id=profile_id,
                version=version,
                plan=normalized,
                now=now,
            )
            self._activate_in_transaction(
                connection,
                profile=profile,
                plan_row=plan_row,
                plan=normalized,
                max_active=max_active,
                now=now,
            )
        value = self.get_profile(profile_id)
        assert value is not None
        return value

    def reopen(self, profile_id: str, *, delivery_target_id: str) -> dict[str, Any]:
        """Move a closed profile back to drafting for an explicit fresh review."""

        now = utc_now()
        with self.transaction() as connection:
            profile = connection.execute(
                "SELECT * FROM monitor_profiles WHERE profile_id=?", (profile_id,)
            ).fetchone()
            if not profile:
                raise KeyError(profile_id)
            if profile["status"] != "closed":
                raise ValueError("only a closed monitor can be reopened")
            target = connection.execute(
                "SELECT status FROM delivery_targets WHERE target_id=?", (delivery_target_id,)
            ).fetchone()
            if not target or target["status"] != "active":
                raise ValueError("the configured delivery target is not active")
            connection.execute(
                """UPDATE monitor_plan_versions SET status='superseded', superseded_at=COALESCE(superseded_at, ?)
                   WHERE profile_id=? AND status='active'""",
                (now, profile_id),
            )
            connection.execute(
                """UPDATE monitor_profiles SET status='drafting',active_plan_version=NULL,
                   delivery_target_id=?,input_outdated=0,paused_at=NULL,resume_at=NULL,
                   pause_reason=NULL,closed_at=NULL,next_quote_run_at=NULL,
                   profile_revision=profile_revision+1,updated_at=? WHERE profile_id=?""",
                (delivery_target_id, now, profile_id),
            )
        value = self.get_profile(profile_id)
        assert value is not None
        return value

    @staticmethod
    def _prepare_rules_for_pause(
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        plan_version: int | None,
        now: str,
    ) -> None:
        """Cancel unfinished observations without losing a triggered episode."""

        if plan_version is None:
            return
        connection.execute(
            """UPDATE monitor_rules
               SET state=CASE
                       WHEN state IN ('cooldown','suppressed') THEN state
                       ELSE 'armed'
                   END,
                   confirmation_progress=0,
                   last_condition_value=CASE
                       WHEN state IN ('cooldown','suppressed') THEN last_condition_value
                       ELSE NULL
                   END,
                   last_bar_time=CASE
                       WHEN state IN ('cooldown','suppressed') THEN last_bar_time
                       ELSE NULL
                   END,
                   updated_at=?
               WHERE profile_id=? AND plan_version=?""",
            (now, profile_id, plan_version),
        )

    def transition(self, profile_id: str, action: str, *, resume_at: str | None = None, reason: str = "") -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM monitor_profiles WHERE profile_id=?", (profile_id,)).fetchone()
            if not row:
                raise KeyError(profile_id)
            if action == "pause":
                if row["status"] != "active":
                    raise ValueError("only an active monitor can be paused")
                connection.execute(
                    """UPDATE monitor_profiles SET status='paused',paused_at=?,resume_at=?,pause_reason=?,
                       profile_revision=profile_revision+1,updated_at=? WHERE profile_id=?""",
                    (now, resume_at, reason, now, profile_id),
                )
                self._prepare_rules_for_pause(
                    connection,
                    profile_id=profile_id,
                    plan_version=row["active_plan_version"],
                    now=now,
                )
            elif action == "resume":
                if row["status"] != "paused":
                    raise ValueError("only a paused monitor can be resumed")
                connection.execute(
                    """UPDATE monitor_profiles SET status='active',paused_at=NULL,resume_at=NULL,pause_reason=NULL,
                       next_quote_run_at=?,profile_revision=profile_revision+1,updated_at=? WHERE profile_id=?""",
                    (now, now, profile_id),
                )
            elif action == "close":
                connection.execute(
                    """UPDATE monitor_profiles SET status='closed',closed_at=?,next_quote_run_at=NULL,
                       profile_revision=profile_revision+1,updated_at=? WHERE profile_id=?""",
                    (now, now, profile_id),
                )
            else:
                raise ValueError("unknown transition")
        value = self.get_profile(profile_id)
        assert value is not None
        return value

    def close_autopilot_profile(
        self,
        profile_id: str,
        *,
        delivery_mode: str = "shadow",
        reason: str = "holding_removed",
    ) -> dict[str, Any]:
        """Close an autonomous profile and persist one terminal summary."""

        if delivery_mode not in {"shadow", "deliver"}:
            raise ValueError("delivery_mode must be shadow or deliver")
        now = utc_now()
        with self.transaction() as connection:
            profile = connection.execute(
                "SELECT * FROM monitor_profiles WHERE profile_id=?", (profile_id,)
            ).fetchone()
            if profile is None:
                raise KeyError(profile_id)
            version = int(profile["active_plan_version"] or 0)
            owner = connection.execute(
                """SELECT created_by FROM monitor_plan_versions
                   WHERE profile_id=? AND (
                       version=? OR (?=0 AND status='pending_review')
                   ) ORDER BY version DESC LIMIT 1""",
                (profile_id, version, version),
            ).fetchone()
            if owner is None or str(owner["created_by"]) != "autopilot":
                raise ValueError("only an autopilot-created profile can be closed automatically")
            connection.execute(
                """UPDATE delivery_outbox SET status='cancelled',error=?,updated_at=?
                   WHERE status='pending' AND event_id IN (
                       SELECT event_id FROM monitor_events WHERE profile_id=?
                   )""",
                (reason, now, profile_id),
            )
            recommendations = connection.execute(
                """SELECT recommendation_id,recommendation_json
                   FROM monitor_recommendations
                   WHERE profile_id=? AND status NOT IN ('cancelled','expired')""",
                (profile_id,),
            ).fetchall()
            for recommendation in recommendations:
                payload = _loads(recommendation["recommendation_json"], {})
                payload = {
                    **(payload if isinstance(payload, dict) else {}),
                    "status": "cancelled",
                    "valid_until": now,
                    "cancel_reason": reason,
                }
                connection.execute(
                    """UPDATE monitor_recommendations SET status='cancelled',
                           recommendation_json=?,recommendation_sha256=?,valid_until=?,updated_at=?
                       WHERE recommendation_id=?""",
                    (
                        _json(payload), _hash(payload), now, now,
                        recommendation["recommendation_id"],
                    ),
                )
            connection.execute(
                """UPDATE monitor_profiles SET status='closed',closed_at=?,next_quote_run_at=NULL,
                       profile_revision=profile_revision+1,updated_at=? WHERE profile_id=?""",
                (now, now, profile_id),
            )
            if reason == "level_method_migration" and version:
                connection.execute(
                    """UPDATE monitor_plan_versions SET status='superseded',
                           superseded_at=COALESCE(superseded_at, ?)
                       WHERE profile_id=? AND version=? AND status='active'""",
                    (now, profile_id, version),
                )
                connection.execute(
                    """UPDATE monitor_rules SET enabled=0,confirmation_progress=0,
                           cooldown_until=NULL,updated_at=?
                       WHERE profile_id=? AND plan_version=?""",
                    (now, profile_id, version),
                )
            connection.execute(
                """UPDATE monitor_watch_episodes SET state='unresolved',phase='unresolved',
                       outcome=?,resolved_at=?,result_notified=1,updated_at=?
                   WHERE profile_id=? AND resolved_at IS NULL""",
                (reason, now, now, profile_id),
            )
            if version:
                selection_removed = reason == "selection_removed"
                lifecycle_rule = f"lifecycle:{reason}"
                title = (
                    f"{profile['symbol']} 已移出自主监控"
                    if selection_removed
                    else f"{profile['symbol']} 已退出持仓监控"
                )
                summary = (
                    "该标的已停止自动研究与自主观察；手工监控不受影响，也不会执行任何交易。"
                    if selection_removed
                    else "当前持仓已清零，系统停止新观察并关闭未决情景；不会执行任何交易。"
                )
                event_id = uuid.uuid4().hex
                inserted = connection.execute(
                    """INSERT OR IGNORE INTO monitor_events(
                           event_id,profile_id,symbol,plan_version,rule_id,armed_epoch,kind,status,
                           severity,title,summary,facts_json,observation_id,first_seen_at,last_seen_at,
                           phase,outcome
                       ) VALUES(?,?,?,?,?,1,'holding_monitor_closed','confirmed','info',?,?,?,NULL,?,?,
                                'unresolved',?)""",
                    (
                        event_id,
                        profile_id,
                        profile["symbol"],
                        version,
                        lifecycle_rule,
                        title,
                        summary,
                        _json({"lifecycle": True, "reason": reason, "trade_execution": "forbidden"}),
                        now,
                        now,
                        reason,
                    ),
                ).rowcount == 1
                if inserted and profile["delivery_target_id"]:
                    outbox_status = "shadow_suppressed" if delivery_mode == "shadow" else "pending"
                    connection.execute(
                        """INSERT INTO delivery_outbox(
                               delivery_id,event_id,delivery_target_id,status,delivery_mode,would_deliver,
                               suppressed_at,suppression_reason,created_at,updated_at
                           ) VALUES(?,?,?,?,?,1,?,?,?,?)""",
                        (
                            uuid.uuid4().hex,
                            event_id,
                            profile["delivery_target_id"],
                            outbox_status,
                            delivery_mode,
                            now if delivery_mode == "shadow" else None,
                            "shadow_mode" if delivery_mode == "shadow" else None,
                            now,
                            now,
                        ),
                    )
                    if delivery_mode == "shadow":
                        self._increment_counter(connection, "shadow_suppressed_delivery_count")
        value = self.get_profile(profile_id)
        assert value is not None
        return value

    def reopen_autopilot_profile(self, profile_id: str) -> dict[str, Any]:
        """Reopen a deselected autonomous profile without requiring a delivery target."""

        now = utc_now()
        with self.transaction() as connection:
            profile = connection.execute(
                "SELECT * FROM monitor_profiles WHERE profile_id=?", (profile_id,)
            ).fetchone()
            if profile is None:
                raise KeyError(profile_id)
            if str(profile["status"]) != "closed":
                raise ValueError("only a closed monitor can be reopened")
            version = int(profile["active_plan_version"] or 0)
            owner = connection.execute(
                "SELECT created_by FROM monitor_plan_versions WHERE profile_id=? AND version=?",
                (profile_id, version),
            ).fetchone()
            if owner is None or str(owner["created_by"]) != "autopilot":
                raise ValueError("only an autopilot-created profile can be reopened automatically")
            connection.execute(
                """UPDATE monitor_plan_versions SET status='superseded',
                       superseded_at=COALESCE(superseded_at, ?)
                   WHERE profile_id=? AND status='active'""",
                (now, profile_id),
            )
            connection.execute(
                """UPDATE monitor_profiles SET status='drafting',active_plan_version=NULL,
                       input_outdated=0,paused_at=NULL,resume_at=NULL,pause_reason=NULL,
                       closed_at=NULL,next_quote_run_at=NULL,
                       profile_revision=profile_revision+1,updated_at=? WHERE profile_id=?""",
                (now, profile_id),
            )
        value = self.get_profile(profile_id)
        assert value is not None
        return value

    def due_profiles(self, now: str | None = None) -> list[dict[str, Any]]:
        current = now or utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM monitor_profiles WHERE status='active'
                   AND (next_quote_run_at IS NULL OR next_quote_run_at<=?) ORDER BY next_quote_run_at""",
                (current,),
            ).fetchall()
        return [self._profile(row) for row in rows]

    def record_profile_tick_outcome(
        self,
        *,
        tick_id: str,
        profile_id: str,
        status: str,
        reason_code: str,
        lease_guard: dict[str, Any] | None = None,
    ) -> bool:
        """Persist exactly one terminal outcome for a due profile in a tick."""

        if status not in {"evaluated", "blocked"}:
            raise ValueError("profile tick status must be evaluated or blocked")
        reason = str(reason_code or "").strip()
        if not reason or len(reason) > 120:
            raise ValueError("profile tick reason_code is required")
        with self.transaction() as connection:
            if lease_guard:
                self._assert_fenced_write(
                    connection,
                    lease_key=str(lease_guard["lease_key"]),
                    owner_id=str(lease_guard["owner_id"]),
                    fencing_token=int(lease_guard["fencing_token"]),
                )
            cursor = connection.execute(
                """INSERT INTO monitor_profile_tick_outcomes(
                       tick_id,profile_id,status,reason_code,created_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(tick_id,profile_id) DO NOTHING""",
                (tick_id, profile_id, status, reason, utc_now()),
            )
            if cursor.rowcount != 1:
                self._increment_counter(
                    connection,
                    "duplicate_profile_tick_outcome_count",
                )
                return False
            return True

    def profile_tick_outcomes(self, tick_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM monitor_profile_tick_outcomes
                   WHERE tick_id=? ORDER BY profile_id""",
                (tick_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @classmethod
    def _insert_health_event(
        cls,
        connection: sqlite3.Connection,
        *,
        profile: sqlite3.Row,
        episode_id: str,
        recovered: bool,
        reason_code: str,
        delivery_mode: str,
        now: str,
    ) -> str:
        event_id = uuid.uuid4().hex
        kind = "data_source_recovered" if recovered else "data_source_unavailable"
        title = (
            f"{profile['symbol']} 数据源已恢复"
            if recovered
            else f"{profile['symbol']} 数据源连续不可用"
        )
        summary = (
            "行情数据已恢复，常态监控将从新的闭合数据继续。"
            if recovered
            else "连续检查未获得可执行行情，价格规则暂不判断。"
        )
        connection.execute(
            """INSERT INTO monitor_events(
                   event_id,profile_id,symbol,plan_version,rule_id,armed_epoch,kind,
                   status,severity,title,summary,facts_json,observation_id,
                   first_seen_at,last_seen_at
               ) VALUES(?,?,?,?,?,1,?,'confirmed',?,?,?,?,NULL,?,?)""",
            (
                event_id,
                profile["profile_id"],
                profile["symbol"],
                int(profile["active_plan_version"]),
                f"health:{episode_id}:{'recovered' if recovered else 'opened'}",
                kind,
                "info" if recovered else "warning",
                title,
                summary,
                _json(
                    {
                        "health_episode_id": episode_id,
                        "reason_code": reason_code,
                        "recovered": recovered,
                        "delivery_mode": delivery_mode,
                    }
                ),
                now,
                now,
            ),
        )
        if profile["delivery_target_id"]:
            delivery_status = (
                "shadow_suppressed" if delivery_mode == "shadow" else "pending"
            )
            connection.execute(
                """INSERT INTO delivery_outbox(
                       delivery_id,event_id,delivery_target_id,status,delivery_mode,
                       would_deliver,suppressed_at,suppression_reason,created_at,updated_at
                   ) VALUES(?,?,?,?,?,1,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    event_id,
                    profile["delivery_target_id"],
                    delivery_status,
                    delivery_mode,
                    now if delivery_mode == "shadow" else None,
                    "shadow_mode" if delivery_mode == "shadow" else None,
                    now,
                    now,
                ),
            )
            if delivery_mode == "shadow":
                cls._increment_counter(connection, "shadow_suppressed_delivery_count")
        return event_id

    def record_data_health(
        self,
        profile_id: str,
        *,
        healthy: bool,
        reason_code: str,
        delivery_mode: str,
        open_after: int = 2,
        lease_guard: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Open/recover one durable blindness episode without alert spam."""

        if delivery_mode not in {"shadow", "deliver"}:
            raise ValueError("delivery_mode must be shadow or deliver")
        now = utc_now()
        event_ids: list[str] = []
        with self.transaction() as connection:
            if lease_guard:
                self._assert_fenced_write(
                    connection,
                    lease_key=str(lease_guard["lease_key"]),
                    owner_id=str(lease_guard["owner_id"]),
                    fencing_token=int(lease_guard["fencing_token"]),
                )
            profile = connection.execute(
                "SELECT * FROM monitor_profiles WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            if not profile or profile["active_plan_version"] is None:
                return []
            episode = connection.execute(
                """SELECT * FROM monitor_health_episodes
                   WHERE profile_id=? AND kind='quote_data_blind'
                     AND status IN ('probing','open')
                   ORDER BY started_at DESC LIMIT 1""",
                (profile_id,),
            ).fetchone()
            if not healthy:
                if not episode:
                    connection.execute(
                        """INSERT INTO monitor_health_episodes(
                               episode_id,profile_id,kind,status,reason_code,
                               occurrence_count,started_at,last_seen_at
                           ) VALUES(?,?,'quote_data_blind','probing',?,1,?,?)""",
                        (uuid.uuid4().hex, profile_id, reason_code, now, now),
                    )
                    return []
                count = int(episode["occurrence_count"] or 0) + 1
                next_status = "open" if count >= max(2, int(open_after)) else "probing"
                connection.execute(
                    """UPDATE monitor_health_episodes SET status=?,reason_code=?,
                           occurrence_count=?,last_seen_at=?,
                           opened_at=CASE WHEN ?='open' AND opened_at IS NULL THEN ? ELSE opened_at END
                       WHERE episode_id=?""",
                    (
                        next_status,
                        reason_code,
                        count,
                        now,
                        next_status,
                        now,
                        episode["episode_id"],
                    ),
                )
                if episode["status"] != "open" and next_status == "open":
                    event_ids.append(
                        self._insert_health_event(
                            connection,
                            profile=profile,
                            episode_id=str(episode["episode_id"]),
                            recovered=False,
                            reason_code=reason_code,
                            delivery_mode=delivery_mode,
                            now=now,
                        )
                    )
            elif episode:
                was_open = episode["status"] == "open"
                connection.execute(
                    """UPDATE monitor_health_episodes SET status=?,last_seen_at=?,recovered_at=?
                       WHERE episode_id=?""",
                    (
                        "recovered" if was_open else "resolved_silent",
                        now,
                        now,
                        episode["episode_id"],
                    ),
                )
                if was_open:
                    event_ids.append(
                        self._insert_health_event(
                            connection,
                            profile=profile,
                            episode_id=str(episode["episode_id"]),
                            recovered=True,
                            reason_code=reason_code,
                            delivery_mode=delivery_mode,
                            now=now,
                        )
                    )
        return [event for event_id in event_ids if (event := self.get_event(event_id))]

    @staticmethod
    def _insert_lifecycle_event(
        connection: sqlite3.Connection,
        *,
        profile: sqlite3.Row,
        plan_version: int,
        lifecycle_key: str,
        kind: str,
        title: str,
        summary: str,
        facts: dict[str, Any],
        now: str,
    ) -> bool:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO monitor_events(
               event_id,profile_id,symbol,plan_version,rule_id,armed_epoch,kind,status,
               severity,title,summary,facts_json,observation_id,first_seen_at,last_seen_at
               ) VALUES(?,?,?,?,?,0,?,'confirmed','warning',?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex,
                profile["profile_id"],
                profile["symbol"],
                plan_version,
                f"lifecycle:{lifecycle_key}",
                kind,
                title,
                summary,
                _json({"lifecycle": True, **facts}),
                None,
                now,
                now,
            ),
        )
        return cursor.rowcount == 1

    @classmethod
    def _create_renewal_draft(
        cls,
        connection: sqlite3.Connection,
        *,
        profile: sqlite3.Row,
        source_plan: sqlite3.Row,
        now_datetime: datetime,
        now: str,
    ) -> int | None:
        existing = connection.execute(
            """SELECT version FROM monitor_plan_versions
               WHERE profile_id=? AND status='pending_review'
               ORDER BY version DESC LIMIT 1""",
            (profile["profile_id"],),
        ).fetchone()
        if existing:
            return int(existing["version"])

        renewed = _loads(source_plan["plan_json"], {})
        if not isinstance(renewed, dict):
            return None
        renewed = json.loads(_json(renewed))
        renewed["hard_valid_until"] = (now_datetime + timedelta(days=90)).isoformat()
        for rule in renewed.get("market_rules") or []:
            if isinstance(rule, dict):
                rule["valid_until"] = (now_datetime + timedelta(days=45)).isoformat()
        try:
            normalized = validate_plan(
                renewed,
                expected_symbol=str(profile["symbol"]),
            )
        except PlanValidationError:
            return None

        source_evidence = _loads(source_plan["evidence_manifest_json"], {})
        evidence = dict(source_evidence) if isinstance(source_evidence, dict) else {}
        evidence["renewal"] = {
            "source_plan_version": int(source_plan["version"]),
            "created_at": now,
            "reason": "plan_or_rule_expired",
            "requires_human_review": True,
        }
        profile_id = str(profile["profile_id"])
        version = int(
            connection.execute(
                """SELECT COALESCE(MAX(version),0)+1 FROM monitor_plan_versions
                   WHERE profile_id=?""",
                (profile_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO monitor_plan_versions(
               profile_id,version,status,schema_version,plan_json,evidence_manifest_json,
               evidence_manifest_sha256,planner_input_sha256,planner_output_sha256,
               prompt_version,model_id,data_as_of,hard_valid_until,created_by,created_at
               ) VALUES(?,?,'pending_review',?,?,?,?,?,?,?,?,?,?, 'monitor_renewal',?)""",
            (
                profile_id,
                version,
                int(normalized["schema_version"]),
                _json(normalized),
                _json(evidence),
                _hash(evidence),
                _hash({"renewal_of": int(source_plan["version"]), "evidence": evidence}),
                _hash(normalized),
                source_plan["prompt_version"],
                source_plan["model_id"],
                source_plan["data_as_of"],
                normalized["hard_valid_until"],
                now,
            ),
        )
        cls._insert_rules(connection, profile_id, version, normalized, now)
        connection.execute(
            """UPDATE monitor_profiles SET profile_revision=profile_revision+1,updated_at=?
               WHERE profile_id=?""",
            (now, profile_id),
        )
        return version

    def maintain_profiles(self, holding_hashes: dict[str, str]) -> None:
        """Reconcile holdings, timed resumes, and hard plan expiry."""

        now = utc_now()
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_profiles WHERE status IN ('active','paused')"
            ).fetchall()
            for row in rows:
                profile_id = str(row["profile_id"])
                symbol = str(row["symbol"])
                if symbol not in holding_hashes:
                    if row["status"] == "active":
                        connection.execute(
                            """UPDATE monitor_profiles SET status='paused',paused_at=?,resume_at=NULL,
                               pause_reason='holding_removed',next_quote_run_at=NULL,updated_at=?
                               WHERE profile_id=?""",
                            (now, now, profile_id),
                        )
                        self._prepare_rules_for_pause(
                            connection,
                            profile_id=profile_id,
                            plan_version=row["active_plan_version"],
                            now=now,
                        )
                    continue
                if (
                    not bool(row["input_outdated"])
                    and row["input_snapshot_hash"]
                    and row["input_snapshot_hash"] != holding_hashes[symbol]
                ):
                    connection.execute(
                        "UPDATE monitor_profiles SET input_outdated=1,updated_at=? WHERE profile_id=?",
                        (now, profile_id),
                    )
                if (
                    row["status"] == "paused"
                    and row["resume_at"]
                    and _timestamp_due(row["resume_at"], datetime.now(timezone.utc))
                    and row["pause_reason"] != "holding_removed"
                ):
                    connection.execute(
                        """UPDATE monitor_profiles SET status='active',paused_at=NULL,resume_at=NULL,
                           pause_reason=NULL,next_quote_run_at=?,updated_at=? WHERE profile_id=?""",
                        (now, now, profile_id),
                    )
                if row["active_plan_version"] is not None:
                    plan = connection.execute(
                        """SELECT * FROM monitor_plan_versions
                           WHERE profile_id=? AND version=?""",
                        (profile_id, row["active_plan_version"]),
                    ).fetchone()
                    current_datetime = datetime.now(timezone.utc)
                    active_rules = connection.execute(
                        """SELECT * FROM monitor_rules
                           WHERE profile_id=? AND plan_version=? AND enabled=1
                             AND valid_until IS NOT NULL""",
                        (profile_id, row["active_plan_version"]),
                    ).fetchall()
                    expired_rules = [
                        rule
                        for rule in active_rules
                        if _timestamp_due(rule["valid_until"], current_datetime)
                    ]
                    lifecycle_changed = False
                    for expired_rule in expired_rules:
                        connection.execute(
                            """UPDATE monitor_rules SET state='expired',updated_at=?
                               WHERE rule_id=?""",
                            (now, expired_rule["rule_id"]),
                        )
                        lifecycle_changed = self._insert_lifecycle_event(
                            connection,
                            profile=row,
                            plan_version=int(row["active_plan_version"]),
                            lifecycle_key=f"rule:{expired_rule['rule_id']}:expired",
                            kind="rule_expired",
                            title="监控规则已到期",
                            summary=(
                                f"规则 {expired_rule['client_rule_id']} 已停止评估；"
                                "系统已保留审计记录并准备待审核续期草案。"
                            ),
                            facts={
                                "client_rule_id": expired_rule["client_rule_id"],
                                "valid_until": expired_rule["valid_until"],
                            },
                            now=now,
                        ) or lifecycle_changed
                    plan_expired = bool(
                        plan and _timestamp_due(plan["hard_valid_until"], current_datetime)
                    )
                    if plan_expired and plan:
                        lifecycle_changed = self._insert_lifecycle_event(
                            connection,
                            profile=row,
                            plan_version=int(row["active_plan_version"]),
                            lifecycle_key="plan:expired",
                            kind="plan_expired",
                            title="监控计划已到期",
                            summary="计划已停止运行；系统已保留审计记录并准备待审核续期草案。",
                            facts={"hard_valid_until": plan["hard_valid_until"]},
                            now=now,
                        ) or lifecycle_changed
                    remaining_rule_count = int(
                        connection.execute(
                            """SELECT COUNT(*) FROM monitor_rules
                               WHERE profile_id=? AND plan_version=? AND enabled=1
                                 AND state<>'expired'""",
                            (profile_id, row["active_plan_version"]),
                        ).fetchone()[0]
                    )
                    rule_coverage_expired = bool(expired_rules) and remaining_rule_count == 0
                    if lifecycle_changed and plan:
                        self._create_renewal_draft(
                            connection,
                            profile=row,
                            source_plan=plan,
                            now_datetime=current_datetime,
                            now=now,
                        )
                    if plan_expired or rule_coverage_expired:
                        connection.execute(
                            """UPDATE monitor_profiles SET status='expired',next_quote_run_at=NULL,
                               updated_at=? WHERE profile_id=?""",
                            (now, profile_id),
                        )
                        connection.execute(
                            """UPDATE monitor_plan_versions SET status='expired'
                               WHERE profile_id=? AND version=?""",
                            (profile_id, row["active_plan_version"]),
                        )
                        connection.execute(
                            """UPDATE monitor_rules SET state='expired',updated_at=?
                               WHERE profile_id=? AND plan_version=?""",
                            (now, profile_id, row["active_plan_version"]),
                        )

    def schedule_next(
        self,
        profile_id: str,
        *,
        seconds: int,
        success: bool,
        blocked_reasons: list[str] | None = None,
        lease_guard: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.transaction() as connection:
            if lease_guard:
                self._assert_fenced_write(
                    connection,
                    lease_key=str(lease_guard["lease_key"]),
                    owner_id=str(lease_guard["owner_id"]),
                    fencing_token=int(lease_guard["fencing_token"]),
                    profile_id=profile_id,
                    tick_id=str(lease_guard["tick_id"]),
                )
            connection.execute(
                """UPDATE monitor_profiles SET last_quote_check_at=?, last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
                   next_quote_run_at=?, blocked_reasons_json=?, updated_at=? WHERE profile_id=?""",
                (
                    now.isoformat(), int(success), now.isoformat(), (now + timedelta(seconds=seconds)).isoformat(),
                    _json(blocked_reasons or []), now.isoformat(), profile_id,
                ),
            )

    @staticmethod
    def _signal_state(
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        plan_version: int,
        signal_type: str,
        client_rule_id: str = "",
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT * FROM monitor_signal_states
               WHERE profile_id=? AND plan_version=? AND signal_type=? AND client_rule_id=?""",
            (profile_id, plan_version, signal_type, client_rule_id),
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = _loads(value.pop("payload_json", "{}"), {})
        return value

    @staticmethod
    def _put_signal_state(
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        plan_version: int,
        signal_type: str,
        client_rule_id: str,
        state: str,
        episode: int,
        release_progress: int,
        last_bar_time: str | None,
        payload: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            """INSERT INTO monitor_signal_states(
               signal_id,profile_id,plan_version,signal_type,client_rule_id,state,
               episode,release_progress,last_bar_time,payload_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(profile_id,plan_version,signal_type,client_rule_id) DO UPDATE SET
                 state=excluded.state,episode=excluded.episode,
                 release_progress=excluded.release_progress,
                 last_bar_time=excluded.last_bar_time,payload_json=excluded.payload_json,
                 updated_at=excluded.updated_at""",
            (
                uuid.uuid4().hex,
                profile_id,
                plan_version,
                signal_type,
                client_rule_id,
                state,
                episode,
                release_progress,
                last_bar_time,
                _json(payload),
                now,
                now,
            ),
        )

    @classmethod
    def _insert_signal_event(
        cls,
        connection: sqlite3.Connection,
        *,
        profile: sqlite3.Row,
        plan_version: int,
        observation_id: str,
        event_kind: str,
        signal_key: str,
        event_sequence: int,
        severity: str,
        title: str,
        summary: str,
        facts: dict[str, Any],
        delivery_mode: str,
        price_volume_mode: str,
        now: str,
        episode_id: str | None = None,
        phase: str | None = None,
        outcome: str | None = None,
        volume_verdict: str | None = None,
        delivery_allowed: bool | None = None,
    ) -> str | None:
        event_id = uuid.uuid4().hex
        try:
            connection.execute(
                """INSERT INTO monitor_events(
                   event_id,profile_id,symbol,plan_version,rule_id,armed_epoch,kind,status,
                   severity,title,summary,facts_json,observation_id,first_seen_at,last_seen_at,
                   episode_id,phase,outcome,volume_verdict
                   ) VALUES(?,?,?,?,?,? ,?,'confirmed',?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    profile["profile_id"],
                    profile["symbol"],
                    plan_version,
                    f"signal:{signal_key}",
                    event_sequence,
                    event_kind,
                    severity,
                    title,
                    summary,
                    _json(facts),
                    observation_id,
                    now,
                    now,
                    episode_id,
                    phase,
                    outcome,
                    volume_verdict,
                ),
            )
        except sqlite3.IntegrityError:
            cls._increment_counter(connection, "duplicate_event_count")
            return None
        # Shadow price-volume analysis remains queryable in monitor_events but
        # intentionally has no outbox row.  Deliver mode is still subordinate
        # to the global monitoring delivery mode.
        should_deliver = price_volume_mode == "deliver" if delivery_allowed is None else delivery_allowed
        if profile["delivery_target_id"] and should_deliver:
            status = "shadow_suppressed" if delivery_mode == "shadow" else "pending"
            suppression_reason = "shadow_mode" if delivery_mode == "shadow" else None
            suppressed_at = now if delivery_mode == "shadow" else None
            connection.execute(
                """INSERT OR IGNORE INTO delivery_outbox(
                   delivery_id,event_id,delivery_target_id,status,delivery_mode,would_deliver,
                   suppressed_at,suppression_reason,created_at,updated_at
                   ) VALUES(?,?,?,?,?,1,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    event_id,
                    profile["delivery_target_id"],
                    status,
                    delivery_mode,
                    suppressed_at,
                    suppression_reason,
                    now,
                    now,
                ),
            )
            if delivery_mode == "shadow":
                cls._increment_counter(connection, "shadow_suppressed_delivery_count")
        return event_id

    @staticmethod
    def _base_target_decision(
        rule: dict[str, Any],
        price_volume: dict[str, Any],
    ) -> tuple[str, str, list[str]]:
        status = str(price_volume.get("status") or "insufficient_data")
        reasons = [str(value) for value in (price_volume.get("reason_codes") or [])]
        intent = str(rule.get("target_intent") or "watch")
        if status != "ready":
            return "insufficient_data", "量价数据不足，价格提醒保持有效", reasons
        if intent in {"add_position", "buy_point"}:
            if bool(price_volume.get("accelerated_decline")):
                return "opposes_add", "放量加速下跌，不宜补仓", [*reasons, "opposes_add"]
            if any(
                code in reasons
                for code in {"add_shrinking_reversal", "add_volume_breakout"}
            ):
                return (
                    "supports_action",
                    "量价出现止跌或反转确认，可作为加仓点复核证据",
                    reasons,
                )
            return "no_confirmation", "量价暂未确认加仓位置", reasons
        if intent == "take_profit":
            if any(
                code in reasons
                for code in {
                    "take_profit_high_volume_stall",
                    "take_profit_price_up_volume_down",
                }
            ):
                return (
                    "supports_action",
                    "量价出现动能衰竭迹象，可复核止盈位置",
                    reasons,
                )
            if "strong_bullish_momentum" in reasons:
                return (
                    "no_confirmation",
                    "价涨量增且收盘强势，尚未出现动能衰竭证据",
                    reasons,
                )
        return "no_confirmation", "量价仅作风险背景，价格提醒保持有效", reasons

    @staticmethod
    def _episode_volume_verdict(
        scenario: dict[str, Any],
        quote: dict[str, Any],
    ) -> tuple[str, Any]:
        policy = scenario.get("volume_confirmation") or {}
        metric = str(policy.get("metric") or "same_bucket_5m_volume_ratio")
        price_volume = quote.get("price_volume")
        actual: Any = None
        if metric == "same_bucket_5m_volume_ratio" and isinstance(price_volume, dict):
            if str(price_volume.get("status") or "") != "ready":
                return "insufficient_evidence", price_volume.get("confirmation_ratio") or price_volume.get("volume_ratio")
            samples = price_volume.get("baseline_samples")
            if isinstance(samples, (int, float)) and samples < int(policy.get("min_samples") or 1):
                return "insufficient_evidence", price_volume.get("volume_ratio")
            actual = price_volume.get("confirmation_ratio") or price_volume.get("volume_ratio")
        elif metric == "same_clock_cumulative_volume_ratio":
            evidence = quote.get("cumulative_volume_evidence")
            if not isinstance(evidence, dict) or str(evidence.get("status") or "") != "ready":
                return "insufficient_evidence", quote.get("cumulative_volume_ratio")
            if int(evidence.get("baseline_samples") or 0) < int(policy.get("min_samples") or 1):
                return "insufficient_evidence", quote.get("cumulative_volume_ratio")
            actual = quote.get("cumulative_volume_ratio")
        elif metric == "absolute_cumulative_volume":
            if str(quote.get("cumulative_volume_unit") or "") != str(policy.get("unit") or ""):
                return "insufficient_evidence", quote.get("cumulative_volume")
            actual = quote.get("cumulative_volume")
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return "insufficient_evidence", actual
        threshold = float(policy.get("threshold") or 0)
        comparator = str(policy.get("comparator") or "gte")
        meets = float(actual) >= threshold if comparator == "gte" else float(actual) <= threshold
        if meets:
            return "price_volume_confirmed", actual
        if metric.endswith("ratio") and float(actual) < 1:
            return "low_volume_probe", actual
        return "price_volume_divergence", actual

    @staticmethod
    def _active_watch_episode(
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        plan_version: int,
        rule_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT * FROM monitor_watch_episodes
               WHERE profile_id=? AND plan_version=? AND rule_id=?
                 AND state IN ('approaching','testing')
               ORDER BY started_at DESC LIMIT 1""",
            (profile_id, plan_version, rule_id),
        ).fetchone()

    @classmethod
    def _resolve_watch_episode(
        cls,
        connection: sqlite3.Connection,
        *,
        profile: sqlite3.Row,
        plan_version: int,
        episode: sqlite3.Row,
        scenario: dict[str, Any],
        quote: dict[str, Any],
        observation_id: str,
        outcome: str,
        delivery_mode: str,
        price_volume_mode: str,
        now: str,
    ) -> str | None:
        verdict, actual = cls._episode_volume_verdict(scenario, quote)
        facts = _loads(episode["facts_json"], {})
        facts.update(
            {
                # Episode lifecycle notices are visual/text context only. The
                # YMCA cue belongs exclusively to a confirmed rule event.
                "alert_cue": "none",
                "source_report": scenario.get("analysis_ref"),
                "scenario": scenario,
                "last_price": quote.get("last_price"),
                "bar_time": quote.get("bar_time"),
                "data_as_of": quote.get("bar_time"),
                "sources": quote.get("sources") or [],
                "quality_status": quote.get("status"),
                "volume_target": scenario.get("volume_confirmation"),
                "volume_actual": actual,
                "volume_verdict": verdict,
                "outcome": outcome,
                "next_condition": "next trading day rearm" if outcome == "unresolved" else "wait for a fresh approach episode",
                "delivery_mode": delivery_mode,
                "price_volume_mode": price_volume_mode,
            }
        )
        phase = "unresolved" if outcome == "unresolved" else "rejected"
        connection.execute(
            """UPDATE monitor_watch_episodes SET state=?,phase=?,outcome=?,
               terminal_observation_id=?,resolved_at=?,result_notified=1,
               volume_verdict=?,facts_json=?,updated_at=? WHERE episode_id=?""",
            (
                phase,
                phase,
                outcome,
                observation_id,
                now,
                verdict,
                _json(facts),
                now,
                episode["episode_id"],
            ),
        )
        summary_by_outcome = {
            "false_breakout": "价格越过点位后未满足闭合 K 线确认并退回原侧。",
            "approach_withdrawn": "价格临近关键点位后提前离开临界区。",
            "observation_window_expired": "观察窗口内未形成连续闭合 K 线确认，本轮观察已结束。",
            "unresolved": "收盘时观察仍未完成，已记录未决结论并等待下一交易日重新布防。",
            "invalidated": "报告中的失效条件已满足，本情景终止；系统不会沿用原动作建议。",
        }
        return cls._insert_signal_event(
            connection,
            profile=profile,
            plan_version=plan_version,
            observation_id=observation_id,
            event_kind="watch_episode_result",
            signal_key=f"episode:{episode['episode_id']}:result",
            event_sequence=2,
            severity="warning" if outcome != "unresolved" else "info",
            title=(
                f"{profile['symbol']} 关键点位收盘未决"
                if outcome == "unresolved"
                else f"{profile['symbol']} 关键点位未能确认"
            ),
            summary=summary_by_outcome[outcome],
            facts=facts,
            delivery_mode=delivery_mode,
            price_volume_mode=price_volume_mode,
            now=now,
            episode_id=str(episode["episode_id"]),
            phase=phase,
            outcome=outcome,
            volume_verdict=verdict,
            delivery_allowed=True,
        )

    @classmethod
    def _confirm_compound_episode(
        cls,
        connection: sqlite3.Connection,
        *,
        profile: sqlite3.Row,
        plan_version: int,
        episode: sqlite3.Row,
        scenario: dict[str, Any],
        analysis_ref: dict[str, Any],
        compound: dict[str, Any],
        quote: dict[str, Any],
        observation_id: str,
        delivery_mode: str,
        price_volume_mode: str,
        now: str,
    ) -> str | None:
        verdict, actual = cls._episode_volume_verdict(scenario, quote)
        rule_row = connection.execute(
            """SELECT client_rule_id,kind,target_intent,target_level,alert_cue,
                      parameters_json
               FROM monitor_rules
               WHERE rule_id=? AND profile_id=? AND plan_version=?""",
            (episode["rule_id"], profile["profile_id"], plan_version),
        ).fetchone()
        if rule_row is None:
            raise RuntimeError("watch episode has no corresponding monitor rule")
        rule_parameters = _loads(rule_row["parameters_json"], {})
        rule_kind = str(rule_row["kind"])
        direction = (
            "above"
            if rule_kind == "price_cross_above"
            else "below"
            if rule_kind == "price_cross_below"
            else None
        )
        recommendation = (
            (quote.get("recommendation_candidates") or {}).get(str(episode["client_rule_id"]))
            if isinstance(quote.get("recommendation_candidates"), dict)
            else None
        )
        if isinstance(recommendation, dict):
            recommendation = {
                **recommendation,
                "profile_id": str(profile["profile_id"]),
                "plan_version": plan_version,
                "episode_id": str(episode["episode_id"]),
            }
            recommendation_id = str(recommendation.get("recommendation_id") or uuid.uuid4().hex)
            recommendation["recommendation_id"] = recommendation_id
            connection.execute(
                """INSERT INTO monitor_recommendations(
                       recommendation_id,profile_id,plan_version,episode_id,symbol,
                       scenario_id,scenario_fingerprint,status,action,recommendation_json,
                       recommendation_sha256,valid_until,feedback_status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(episode_id,scenario_fingerprint) DO UPDATE SET
                       status=excluded.status,action=excluded.action,
                       recommendation_json=excluded.recommendation_json,
                       recommendation_sha256=excluded.recommendation_sha256,
                       valid_until=excluded.valid_until,updated_at=excluded.updated_at""",
                (
                    recommendation_id,
                    profile["profile_id"],
                    plan_version,
                    episode["episode_id"],
                    profile["symbol"],
                    scenario.get("scenario_id"),
                    scenario.get("scenario_fingerprint"),
                    recommendation.get("status") or "evidence_pending",
                    recommendation.get("action") or "observe",
                    _json(recommendation),
                    _hash(recommendation),
                    recommendation.get("valid_until"),
                    recommendation.get("feedback_status") or "pending",
                    recommendation.get("created_at") or now,
                    now,
                ),
            )
        facts = _loads(episode["facts_json"], {})
        facts.update(
            {
                "client_rule_id": str(rule_row["client_rule_id"]),
                "rule_kind": rule_kind,
                "direction": direction,
                "threshold": rule_parameters.get("threshold"),
                "target_intent": rule_row["target_intent"],
                "target_level": rule_row["target_level"],
                "confirmation_count": int(
                    rule_parameters.get("confirmation_count", 2)
                ),
                "alert_cue": str(rule_row["alert_cue"] or "none"),
                "parameters": rule_parameters,
                "source_report": analysis_ref,
                "scenario": scenario,
                "last_price": quote.get("last_price"),
                "bar_time": quote.get("bar_time"),
                "sources": quote.get("sources") or [],
                "quality_status": quote.get("status"),
                "compound_assessment": compound,
                "volume_target": scenario.get("volume_confirmation"),
                "volume_actual": actual,
                "volume_verdict": verdict,
                "recommendation": recommendation,
                "outcome": "confirmed",
                "next_condition": "wait for plan rearm or fresh evidence",
                "delivery_mode": delivery_mode,
                "price_volume_mode": price_volume_mode,
                "trade_execution": "forbidden",
            }
        )
        connection.execute(
            """UPDATE monitor_watch_episodes SET state='confirmed',phase='confirmed',
                   outcome='confirmed',terminal_observation_id=?,resolved_at=?,
                   result_notified=1,volume_verdict=?,facts_json=?,updated_at=?
               WHERE episode_id=?""",
            (observation_id, now, verdict, _json(facts), now, episode["episode_id"]),
        )
        return cls._insert_signal_event(
            connection,
            profile=profile,
            plan_version=plan_version,
            observation_id=observation_id,
            event_kind="watch_episode_result",
            signal_key=f"episode:{episode['episode_id']}:result",
            event_sequence=2,
            severity=(
                "warning"
                if recommendation and recommendation.get("action") != "observe"
                else "info"
            ),
            title=f"{profile['symbol']} 监控情景已确认",
            summary=(
                "价格与报告必要条件已确认，已生成仅供人工决策的建议。"
                if recommendation and recommendation.get("status") == "ready"
                else "价格事实已确认，但必要证据仍不完整，仅继续观察，不给出买卖数量。"
            ),
            facts=facts,
            delivery_mode=delivery_mode,
            price_volume_mode=price_volume_mode,
            now=now,
            episode_id=str(episode["episode_id"]),
            phase="confirmed",
            outcome="confirmed",
            volume_verdict=verdict,
            delivery_allowed=True,
        )

    def _process_watch_episodes(
        self,
        connection: sqlite3.Connection,
        *,
        profile: sqlite3.Row,
        plan_version: int,
        plan: dict[str, Any],
        rule_rows: list[sqlite3.Row],
        quote: dict[str, Any],
        observation_id: str,
        delivery_mode: str,
        price_volume_mode: str,
        now: str,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        if int(plan.get("schema_version") or 1) < 4:
            return {}, []
        scenario_by_rule = {
            str(item.get("client_rule_id") or ""): item
            for item in (plan.get("watch_scenarios") or [])
            if isinstance(item, dict)
        }
        analysis_ref = dict(plan.get("analysis_ref") or {})
        session_date = str(quote.get("session_date") or quote.get("bar_time") or "")[:10]
        assessments: dict[str, dict[str, Any]] = {}
        created: list[str] = []
        for row in rule_rows:
            scenario = scenario_by_rule.get(str(row["client_rule_id"]))
            if not scenario:
                continue
            compound = (
                (quote.get("compound_assessments") or {}).get(str(row["client_rule_id"]))
                if isinstance(quote.get("compound_assessments"), dict)
                else None
            )
            rule_interval = str(scenario.get("trigger", {}).get("interval") or "5m")
            approach_interval = str(
                scenario.get("approach_policy", {}).get("check_interval") or "1m"
            )
            quote_interval = str(quote.get("interval") or "")
            if quote_interval not in {rule_interval, approach_interval}:
                continue
            confirmation_bar = quote_interval == rule_interval
            metadata = {
                "client_rule_id": row["client_rule_id"],
                "kind": row["kind"],
                "parameters": _loads(row["parameters_json"], {}),
            }
            distance = target_distance_bps(metadata, quote.get("last_price"))
            reached = target_reached(metadata, quote.get("last_price"))
            near_bps = float(scenario.get("approach_policy", {}).get("distance_bps") or 100)
            resolution = scenario.get("resolution_policy") or {}
            rejection_bps = float(resolution.get("rejection_hysteresis_bps") or 30)
            max_bars = int(resolution.get("max_observation_bars") or 6)
            active = self._active_watch_episode(
                connection,
                profile_id=str(profile["profile_id"]),
                plan_version=plan_version,
                rule_id=str(row["rule_id"]),
            )
            if active and str(active["session_date"]) != session_date:
                event_id = self._resolve_watch_episode(
                    connection,
                    profile=profile,
                    plan_version=plan_version,
                    episode=active,
                    scenario={**scenario, "analysis_ref": analysis_ref},
                    quote=quote,
                    observation_id=observation_id,
                    outcome="unresolved",
                    delivery_mode=delivery_mode,
                    price_volume_mode=price_volume_mode,
                    now=now,
                )
                if event_id:
                    created.append(event_id)
                active = None

            if active is None and (
                reached
                or (distance is not None and distance <= near_bps)
                or bool(isinstance(compound, dict) and compound.get("entry_met"))
            ):
                episode_id = uuid.uuid4().hex
                phase = "testing" if reached or bool((compound or {}).get("entry_met")) else "approaching"
                verdict, actual = self._episode_volume_verdict(scenario, quote)
                facts = {
                    # Approaching/testing is not a confirmed cue event.
                    "alert_cue": "none",
                    "source_report": analysis_ref,
                    "scenario": scenario,
                    "point": scenario.get("trigger"),
                    "last_price": quote.get("last_price"),
                    "distance_bps": distance,
                    "bar_time": quote.get("bar_time"),
                    "data_as_of": quote.get("bar_time"),
                    "sources": quote.get("sources") or [],
                    "quality_status": quote.get("status"),
                    "volume_target": scenario.get("volume_confirmation"),
                    "volume_actual": actual,
                    "volume_verdict": verdict,
                    "next_condition": "observe closed bars for confirmation or rejection",
                    "delivery_mode": delivery_mode,
                    "price_volume_mode": price_volume_mode,
                    "compound_assessment": compound,
                }
                connection.execute(
                    """INSERT INTO monitor_watch_episodes(
                       episode_id,profile_id,plan_version,rule_id,client_rule_id,session_date,
                       state,phase,started_at,approach_observation_id,first_cross_observation_id,
                       first_cross_at,observed_bars,approach_notified,volume_verdict,facts_json,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                    (
                        episode_id,
                        profile["profile_id"],
                        plan_version,
                        row["rule_id"],
                        row["client_rule_id"],
                        session_date,
                        phase,
                        phase,
                        now,
                        observation_id,
                        observation_id if reached else None,
                        now if reached else None,
                        1 if confirmation_bar else 0,
                        verdict,
                        _json(facts),
                        now,
                    ),
                )
                active = self._active_watch_episode(
                    connection,
                    profile_id=str(profile["profile_id"]),
                    plan_version=plan_version,
                    rule_id=str(row["rule_id"]),
                )
                event_id = self._insert_signal_event(
                    connection,
                    profile=profile,
                    plan_version=plan_version,
                    observation_id=observation_id,
                    event_kind="watch_episode_approaching",
                    signal_key=f"episode:{episode_id}:approach",
                    event_sequence=1,
                    severity="info",
                    title=f"{profile['symbol']} 接近报告关键点位",
                    summary="价格已进入临界区，开始观察能否确认突破/跌破，或回到原侧。",
                    facts=facts,
                    delivery_mode=delivery_mode,
                    price_volume_mode=price_volume_mode,
                    now=now,
                    episode_id=episode_id,
                    phase=phase,
                    outcome=None,
                    volume_verdict=verdict,
                    delivery_allowed=True,
                )
                if event_id:
                    created.append(event_id)
            elif active is not None:
                observed_bars = int(active["observed_bars"] or 0) + int(confirmation_bar)
                phase = str(active["phase"])
                if (reached or bool((compound or {}).get("entry_met"))) and phase == "approaching":
                    phase = "testing"
                    connection.execute(
                        """UPDATE monitor_watch_episodes SET state='testing',phase='testing',
                           first_cross_observation_id=?,first_cross_at=?,observed_bars=?,updated_at=?
                           WHERE episode_id=?""",
                        (observation_id, now, observed_bars, now, active["episode_id"]),
                    )
                else:
                    connection.execute(
                        """UPDATE monitor_watch_episodes SET observed_bars=?,updated_at=?
                           WHERE episode_id=?""",
                        (observed_bars, now, active["episode_id"]),
                    )
                outcome: str | None = None
                if isinstance(compound, dict) and compound.get("invalidated"):
                    outcome = "invalidated"
                elif not reached and phase == "testing" and distance is not None and distance >= rejection_bps:
                    outcome = "false_breakout"
                elif not reached and phase == "approaching" and distance is not None and distance > near_bps:
                    outcome = "approach_withdrawn"
                elif confirmation_bar and observed_bars >= max_bars:
                    outcome = "observation_window_expired"
                if outcome:
                    refreshed = connection.execute(
                        "SELECT * FROM monitor_watch_episodes WHERE episode_id=?",
                        (active["episode_id"],),
                    ).fetchone()
                    assert refreshed is not None
                    event_id = self._resolve_watch_episode(
                        connection,
                        profile=profile,
                        plan_version=plan_version,
                        episode=refreshed,
                        scenario={**scenario, "analysis_ref": analysis_ref},
                        quote=quote,
                        observation_id=observation_id,
                        outcome=outcome,
                        delivery_mode=delivery_mode,
                        price_volume_mode=price_volume_mode,
                        now=now,
                    )
                    if event_id:
                        created.append(event_id)
                    active = None

                if (
                    active is not None
                    and int(plan.get("schema_version") or 1) >= 5
                    and isinstance(compound, dict)
                    and str(
                        compound.get("automation_status")
                        or scenario.get("automation_status")
                        or ""
                    ) == "action_ready"
                    and compound.get("confirmation_met")
                    and not compound.get("evidence_pending")
                ):
                    refreshed = connection.execute(
                        "SELECT * FROM monitor_watch_episodes WHERE episode_id=?",
                        (active["episode_id"],),
                    ).fetchone()
                    assert refreshed is not None
                    event_id = self._confirm_compound_episode(
                        connection,
                        profile=profile,
                        plan_version=plan_version,
                        episode=refreshed,
                        scenario=scenario,
                        analysis_ref=analysis_ref,
                        compound=compound,
                        quote=quote,
                        observation_id=observation_id,
                        delivery_mode=delivery_mode,
                        price_volume_mode=price_volume_mode,
                        now=now,
                    )
                    if event_id:
                        created.append(event_id)
                    active = None

            if active is not None:
                verdict, actual = self._episode_volume_verdict(scenario, quote)
                assessments[str(row["rule_id"])] = {
                    "episode_id": active["episode_id"],
                    "client_rule_id": row["client_rule_id"],
                    "target_intent": scenario.get("intent"),
                    "phase": "testing" if reached else "approaching",
                    "distance_bps": distance,
                    "volume_verdict": verdict,
                    "volume_actual": actual,
                    "compound_assessment": compound,
                }
        return assessments, created

    def _process_price_volume_signals(
        self,
        connection: sqlite3.Connection,
        *,
        profile: sqlite3.Row,
        plan_version: int,
        plan: dict[str, Any],
        rule_rows: list[sqlite3.Row],
        quote: dict[str, Any],
        observation_id: str,
        delivery_mode: str,
        price_volume_mode: str,
        now: str,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        policy = plan.get("price_volume_policy")
        price_volume = quote.get("price_volume")
        if (
            price_volume_mode == "off"
            or int(plan.get("schema_version") or 1) < 2
            or not isinstance(policy, dict)
            or not bool(policy.get("enabled", True))
            or not isinstance(price_volume, dict)
        ):
            return {}, []

        created: list[str] = []
        evidence_bar_time = str(
            quote.get("price_volume_bar_time") or quote.get("bar_time") or ""
        )
        pv_state = self._signal_state(
            connection,
            profile_id=str(profile["profile_id"]),
            plan_version=plan_version,
            signal_type="price_volume",
        )
        previous_pv_payload = (pv_state or {}).get("payload") or {}
        event_sequence = int(previous_pv_payload.get("event_sequence") or 0)
        current_accelerated = bool(
            price_volume.get("status") == "ready"
            and price_volume.get("accelerated_decline")
        )
        observed_pv_state = "accelerated_decline" if current_accelerated else str(
            price_volume.get("status") or "insufficient_data"
        )
        previous_pv_state = str((pv_state or {}).get("state") or "")
        release_progress = int((pv_state or {}).get("release_progress") or 0)
        is_new_evidence = (
            not pv_state
            or str(pv_state.get("last_bar_time") or "") != evidence_bar_time
        )
        enters_accelerated_episode = bool(
            is_new_evidence
            and current_accelerated
            and previous_pv_state != "accelerated_decline"
        )
        if enters_accelerated_episode:
            event_sequence += 1
            event_id = self._insert_signal_event(
                connection,
                profile=profile,
                plan_version=plan_version,
                observation_id=observation_id,
                event_kind="price_volume_accelerated_decline",
                signal_key="price_volume",
                event_sequence=event_sequence,
                severity="critical",
                title=f"{profile['symbol']} 放量加速下跌",
                summary="放量加速下跌，不宜补仓",
                facts={
                    "price_volume": price_volume,
                    "last_price": quote.get("last_price"),
                    "bar_time": evidence_bar_time or quote.get("bar_time"),
                    "price_volume_bar_time": evidence_bar_time or None,
                    "quote_bar_time": quote.get("bar_time"),
                    "sources": quote.get("sources") or [],
                    "quality_status": quote.get("status"),
                    "delivery_mode": delivery_mode,
                    "price_volume_mode": price_volume_mode,
                },
                delivery_mode=delivery_mode,
                price_volume_mode=price_volume_mode,
                now=now,
            )
            if event_id:
                created.append(event_id)

        # Keep one accelerated-decline episode open until two distinct,
        # actionable closed bars are no longer accelerating.  A single pause,
        # duplicate observation, or insufficient-data bar must not manufacture
        # a fresh risk episode on the next accelerating bar.
        persisted_pv_state = observed_pv_state
        if current_accelerated:
            release_progress = 0
        elif previous_pv_state == "accelerated_decline":
            if is_new_evidence and price_volume.get("status") == "ready":
                release_progress += 1
            if release_progress < 2:
                persisted_pv_state = "accelerated_decline"
            else:
                release_progress = 2
        elif is_new_evidence:
            release_progress = 0
        self._put_signal_state(
            connection,
            profile_id=str(profile["profile_id"]),
            plan_version=plan_version,
            signal_type="price_volume",
            client_rule_id="",
            state=persisted_pv_state,
            episode=int((pv_state or {}).get("episode") or 0)
            + (1 if enters_accelerated_episode else 0),
            release_progress=release_progress,
            last_bar_time=evidence_bar_time or None,
            payload={**price_volume, "event_sequence": event_sequence},
            now=now,
        )

        if int(plan.get("schema_version") or 1) >= 4:
            # Schema v4 target lifecycle is persisted in monitor_watch_episodes.
            # Keep this function responsible only for standalone price-volume
            # diagnostics so one episode cannot emit duplicate proximity events.
            return {}, created

        plan_rules = {
            str(item.get("client_rule_id") or ""): item
            for item in plan.get("market_rules", [])
        }
        assessments: dict[str, dict[str, Any]] = {}
        near_bps = float(plan.get("near_trigger_distance_bps", 100))
        for row in rule_rows:
            metadata = plan_rules.get(str(row["client_rule_id"]))
            if not metadata or not str(metadata.get("kind") or "").startswith("price_"):
                continue
            rule_interval = str(metadata.get("parameters", {}).get("interval") or "5m")
            if rule_interval != str(quote.get("interval") or ""):
                # A mixed 1m/5m plan may evaluate both observations in one tick.
                # Only the rule's own closed-bar stream may advance or leave its
                # target episode; otherwise the later quote can manufacture a
                # fresh proximity event on every scheduler pass.
                continue
            client_rule_id = str(metadata["client_rule_id"])
            signal = self._signal_state(
                connection,
                profile_id=str(profile["profile_id"]),
                plan_version=plan_version,
                signal_type="target_assessment",
                client_rule_id=client_rule_id,
            )
            previous = (signal or {}).get("payload") or {}
            decision, message, reasons = self._base_target_decision(metadata, price_volume)
            release_progress = int((signal or {}).get("release_progress") or 0)
            intent = str(metadata.get("target_intent") or "watch")
            previous_evidence = str(previous.get("evidence_bar_time") or "")
            new_target_evidence = bool(evidence_bar_time and evidence_bar_time != previous_evidence)
            if intent in {"add_position", "buy_point"}:
                if decision == "opposes_add":
                    release_progress = 0
                elif previous.get("decision") == "opposes_add":
                    ratio = price_volume.get("volume_ratio")
                    qualifies = bool(
                        price_volume.get("status") == "ready"
                        and (
                            "price_stabilized" in (price_volume.get("reason_codes") or [])
                            or (
                                isinstance(ratio, (int, float))
                                and not isinstance(ratio, bool)
                                and float(ratio) < 1.2
                            )
                        )
                    )
                    if new_target_evidence:
                        release_progress = release_progress + 1 if qualifies else 0
                    if release_progress < 2:
                        decision = "opposes_add"
                        message = "放量加速下跌，不宜补仓"
                        reasons = [*reasons, "accelerated_decline_clearance_pending"]

            distance = target_distance_bps(metadata, quote.get("last_price"))
            reached = target_reached(metadata, quote.get("last_price"))
            phase = (
                "reached"
                if reached
                else "approaching"
                if distance is not None and distance <= near_bps
                else None
            )
            assessment = {
                "client_rule_id": client_rule_id,
                "target_intent": intent,
                "target_level": int(metadata.get("target_level") or 1),
                "phase": phase,
                "decision": decision,
                "distance_bps": distance,
                "message": message,
                "reason_codes": list(dict.fromkeys(reasons)),
            }
            if phase is not None:
                assessments[str(row["rule_id"])] = assessment

            previous_state = str((signal or {}).get("state") or "outside")
            entering = phase is not None and previous_state not in {"approaching", "reached"}
            episode = int((signal or {}).get("episode") or 0) + (1 if entering else 0)
            target_event_sequence = int(previous.get("event_sequence") or 0)
            changed = bool(
                phase is not None
                and previous_state in {"approaching", "reached"}
                and (
                    previous.get("decision") != decision
                    or previous.get("phase") != phase
                )
            )
            if entering or changed:
                target_event_sequence += 1
                event_kind = "target_proximity" if entering else "target_assessment_changed"
                event_id = self._insert_signal_event(
                    connection,
                    profile=profile,
                    plan_version=plan_version,
                    observation_id=observation_id,
                    event_kind=event_kind,
                    signal_key=f"target:{client_rule_id}",
                    event_sequence=target_event_sequence,
                    severity="warning" if changed or decision == "opposes_add" else "info",
                    title=(
                        f"{profile['symbol']} 接近目标位"
                        if entering and phase == "approaching"
                        else f"{profile['symbol']} 到达目标位"
                        if entering
                        else f"{profile['symbol']} 目标位量价判断变化"
                    ),
                    summary=message,
                    facts={
                        "price_volume": price_volume,
                        "target_assessment": assessment,
                        "last_price": quote.get("last_price"),
                        "bar_time": quote.get("bar_time"),
                        "price_volume_bar_time": evidence_bar_time or None,
                        "sources": quote.get("sources") or [],
                        "quality_status": quote.get("status"),
                        "delivery_mode": delivery_mode,
                        "price_volume_mode": price_volume_mode,
                    },
                    delivery_mode=delivery_mode,
                    price_volume_mode=price_volume_mode,
                    now=now,
                )
                if event_id:
                    created.append(event_id)
            stored_payload = {
                **assessment,
                "phase": phase or "outside",
                "evidence_bar_time": evidence_bar_time or None,
                "event_sequence": target_event_sequence,
            }
            self._put_signal_state(
                connection,
                profile_id=str(profile["profile_id"]),
                plan_version=plan_version,
                signal_type="target_assessment",
                client_rule_id=client_rule_id,
                state=phase or "outside",
                episode=episode,
                release_progress=release_progress,
                last_bar_time=str(quote.get("bar_time") or "") or None,
                payload=stored_payload,
                now=now,
            )
        return assessments, created

    def evaluate_quote(
        self,
        profile_id: str,
        quote: dict[str, Any],
        *,
        delivery_mode: str = "deliver",
        price_volume_mode: str = "off",
        lease_guard: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if delivery_mode not in {"shadow", "deliver"}:
            raise ValueError("delivery_mode must be shadow or deliver")
        if price_volume_mode not in {"off", "shadow", "deliver"}:
            raise ValueError("price_volume_mode must be off, shadow, or deliver")
        now = utc_now()
        with self.transaction() as connection:
            if lease_guard:
                self._assert_fenced_write(
                    connection,
                    lease_key=str(lease_guard["lease_key"]),
                    owner_id=str(lease_guard["owner_id"]),
                    fencing_token=int(lease_guard["fencing_token"]),
                    profile_id=profile_id,
                    tick_id=str(lease_guard["tick_id"]),
                )
            profile = connection.execute("SELECT * FROM monitor_profiles WHERE profile_id=?", (profile_id,)).fetchone()
            if not profile or profile["status"] != "active" or profile["active_plan_version"] is None:
                return []
            version = int(profile["active_plan_version"])
            payload_hash = _hash(quote)
            observation_id = uuid.uuid4().hex
            try:
                connection.execute(
                    """INSERT INTO monitor_observations(
                       observation_id,profile_id,domain,source_key,observed_at,data_as_of,status,payload_json,payload_hash
                       ) VALUES(?,?,'quote',?,?,?,?,?,?)""",
                    (
                        observation_id, profile_id, ",".join(quote.get("sources") or ["unknown"]), now,
                        quote.get("bar_time"), quote.get("status", "unknown"), _json(quote), payload_hash,
                    ),
                )
            except sqlite3.IntegrityError:
                self._increment_counter(connection, "duplicate_observation_count")
                return []
            rows = connection.execute(
                "SELECT * FROM monitor_rules WHERE profile_id=? AND plan_version=? AND enabled=1",
                (profile_id, version),
            ).fetchall()
            plan_row = connection.execute(
                """SELECT plan_json FROM monitor_plan_versions
                   WHERE profile_id=? AND version=?""",
                (profile_id, version),
            ).fetchone()
            plan_payload = _loads(plan_row["plan_json"], {}) if plan_row else {}
            episode_assessments, episode_events = self._process_watch_episodes(
                connection,
                profile=profile,
                plan_version=version,
                plan=plan_payload,
                rule_rows=rows,
                quote=quote,
                observation_id=observation_id,
                delivery_mode=delivery_mode,
                price_volume_mode=price_volume_mode,
                now=now,
            )
            if int(plan_payload.get("schema_version") or 1) >= 5:
                price_volume_assessments, signal_events = {}, []
            else:
                price_volume_assessments, signal_events = self._process_price_volume_signals(
                    connection,
                    profile=profile,
                    plan_version=version,
                    plan=plan_payload,
                    rule_rows=rows,
                    quote=quote,
                    observation_id=observation_id,
                    delivery_mode=delivery_mode,
                    price_volume_mode=price_volume_mode,
                    now=now,
                )
            assessments = {**price_volume_assessments, **episode_assessments}
            created: list[str] = [*episode_events, *signal_events]
            for row in rows:
                rule = dict(row)
                rule["parameters"] = _loads(rule.pop("parameters_json"), {})
                if int(plan_payload.get("schema_version") or 1) >= 5:
                    # Schema v5 reaches a terminal state only through the
                    # source-condition preserving compound evaluator above.
                    # The legacy price-rule loop remains the approach sensor
                    # and must not emit a second, simplified confirmation.
                    continue
                if str(rule["parameters"].get("interval") or "5m") != str(quote.get("interval") or ""):
                    continue
                if rule.get("valid_until") and _timestamp_due(rule["valid_until"], datetime.now(timezone.utc)):
                    connection.execute(
                        "UPDATE monitor_rules SET state='expired',updated_at=? WHERE rule_id=?", (now, rule["rule_id"])
                    )
                    continue
                if rule.get("last_bar_time") == quote.get("bar_time"):
                    continue
                condition = condition_for(rule, quote)
                state = str(rule["state"])
                is_price_cross = str(rule["kind"]) in {
                    "price_cross_above",
                    "price_cross_below",
                }
                if condition is None:
                    self._increment_counter(connection, "data_blocked_rule_count")
                    connection.execute(
                        """UPDATE monitor_rules SET state='data_blocked',confirmation_progress=0,
                           last_bar_time=?,last_observation_id=?,updated_at=? WHERE rule_id=?""",
                        (quote.get("bar_time"), observation_id, now, rule["rule_id"]),
                    )
                    continue
                has_event_in_epoch = False
                if (
                    state in {"armed", "candidate", "data_blocked"}
                    or rule.get("last_condition_value") is None
                ):
                    has_event_in_epoch = connection.execute(
                        """SELECT 1 FROM monitor_events
                           WHERE profile_id=? AND plan_version=? AND rule_id=? AND armed_epoch=?
                           LIMIT 1""",
                        (
                            profile_id,
                            version,
                            rule["rule_id"],
                            rule["armed_epoch"],
                        ),
                    ).fetchone() is not None
                if (
                    state == "data_blocked"
                    or rule.get("last_condition_value") is None
                    or (
                        has_event_in_epoch
                        and state not in {"cooldown", "suppressed"}
                    )
                ):
                    # A first/recovered observation is only a baseline. If the
                    # current epoch already emitted an event, it cannot be
                    # reused: a deep hysteresis clear advances the epoch;
                    # anything else remains suppressed.
                    cleared_existing_epoch = bool(
                        has_event_in_epoch and clear_for(rule, quote)
                    )
                    baseline_state = (
                        "armed"
                        if cleared_existing_epoch
                        else "suppressed"
                        if has_event_in_epoch or (is_price_cross and condition)
                        else "armed"
                    )
                    baseline_epoch = int(rule["armed_epoch"]) + int(
                        cleared_existing_epoch
                    )
                    baseline_cooldown = (
                        None
                        if cleared_existing_epoch or not has_event_in_epoch
                        else rule.get("cooldown_until")
                    )
                    connection.execute(
                        """UPDATE monitor_rules SET state=?,confirmation_progress=0,
                           armed_epoch=?,last_condition_value=?,last_bar_time=?,
                           last_observation_id=?,cooldown_until=?,updated_at=?
                           WHERE rule_id=?""",
                        (
                            baseline_state,
                            baseline_epoch,
                            int(condition),
                            quote.get("bar_time"),
                            observation_id,
                            baseline_cooldown,
                            now,
                            rule["rule_id"],
                        ),
                    )
                    continue
                if state in {"cooldown", "suppressed"}:
                    if clear_for(rule, quote):
                        connection.execute(
                            """UPDATE monitor_rules SET state='armed',confirmation_progress=0,armed_epoch=armed_epoch+1,
                               last_condition_value=0,last_bar_time=?,last_observation_id=?,cooldown_until=NULL,updated_at=?
                               WHERE rule_id=?""",
                            (quote.get("bar_time"), observation_id, now, rule["rule_id"]),
                        )
                    else:
                        next_state = "suppressed" if rule.get("cooldown_until") and str(rule["cooldown_until"]) <= now else state
                        connection.execute(
                            """UPDATE monitor_rules SET state=?,last_condition_value=1,last_bar_time=?,
                               last_observation_id=?,updated_at=? WHERE rule_id=?""",
                            (next_state, quote.get("bar_time"), observation_id, now, rule["rule_id"]),
                        )
                    continue
                if state == "candidate" and not self._bars_are_continuous(
                    str(rule.get("last_bar_time") or ""),
                    str(quote.get("bar_time") or ""),
                    str(rule["parameters"].get("interval") or "5m"),
                ):
                    # Lunch, suspension, a data gap, or a missed sequence
                    # invalidates unfinished consecutive confirmation.
                    reset_state = "suppressed" if is_price_cross and condition else "armed"
                    connection.execute(
                        """UPDATE monitor_rules SET state=?,confirmation_progress=0,
                           last_condition_value=?,last_bar_time=?,last_observation_id=?,updated_at=?
                           WHERE rule_id=?""",
                        (
                            reset_state,
                            int(condition),
                            quote.get("bar_time"),
                            observation_id,
                            now,
                            rule["rule_id"],
                        ),
                    )
                    continue
                if (
                    is_price_cross
                    and state == "armed"
                    and condition
                ):
                    crossing_is_adjacent = self._bars_are_continuous(
                        str(rule.get("last_bar_time") or ""),
                        str(quote.get("bar_time") or ""),
                        str(rule["parameters"].get("interval") or "5m"),
                    )
                    if bool(rule.get("last_condition_value")) or not crossing_is_adjacent:
                        # Confirmation may only begin on an adjacent observed
                        # false -> true transition. A gap is indistinguishable
                        # from a crossing that happened while monitoring was
                        # unavailable, so it is suppressed until a deep clear.
                        connection.execute(
                            """UPDATE monitor_rules SET state='suppressed',confirmation_progress=0,
                               last_condition_value=1,last_bar_time=?,last_observation_id=?,updated_at=?
                               WHERE rule_id=?""",
                            (quote.get("bar_time"), observation_id, now, rule["rule_id"]),
                        )
                        continue
                progress = int(rule["confirmation_progress"] or 0) + 1 if condition else 0
                required = int(rule["parameters"].get("confirmation_count", 2))
                if not condition:
                    connection.execute(
                        """UPDATE monitor_rules SET state='armed',confirmation_progress=0,last_condition_value=0,
                           last_bar_time=?,last_observation_id=?,updated_at=? WHERE rule_id=?""",
                        (quote.get("bar_time"), observation_id, now, rule["rule_id"]),
                    )
                    continue
                if progress < required:
                    connection.execute(
                        """UPDATE monitor_rules SET state='candidate',confirmation_progress=?,last_condition_value=1,
                           last_bar_time=?,last_observation_id=?,updated_at=? WHERE rule_id=?""",
                        (progress, quote.get("bar_time"), observation_id, now, rule["rule_id"]),
                    )
                    continue
                event_id = uuid.uuid4().hex
                event_title = f"{profile['symbol']} 监控条件已满足"
                rule_kind = str(rule["kind"])
                direction = (
                    "above"
                    if rule_kind.endswith("_above")
                    else "below"
                    if rule_kind.endswith("_below")
                    else "enter"
                    if rule_kind.endswith("_enter")
                    else "exit"
                    if rule_kind.endswith("_exit")
                    else None
                )
                active_episode = self._active_watch_episode(
                    connection,
                    profile_id=profile_id,
                    plan_version=version,
                    rule_id=str(rule["rule_id"]),
                )
                episode_id = str(active_episode["episode_id"]) if active_episode else None
                episode_volume_verdict: str | None = None
                episode_volume_actual: Any = None
                if active_episode:
                    scenario = next(
                        (
                            item
                            for item in (plan_payload.get("watch_scenarios") or [])
                            if str(item.get("client_rule_id") or "") == str(rule["client_rule_id"])
                        ),
                        {},
                    )
                    episode_volume_verdict, episode_volume_actual = self._episode_volume_verdict(
                        scenario,
                        quote,
                    )
                facts = {
                    "client_rule_id": rule["client_rule_id"],
                    "rule_kind": rule_kind,
                    "direction": direction,
                    "threshold": rule["parameters"].get("threshold"),
                    "target_intent": rule.get("target_intent"),
                    "target_level": rule.get("target_level"),
                    "confirmation_count": required,
                    "alert_cue": str(rule.get("alert_cue") or "none"),
                    "parameters": rule["parameters"],
                    "last_price": quote.get("last_price"), "bar_time": quote.get("bar_time"),
                    "sources": quote.get("sources") or [], "quality_status": quote.get("status"),
                    "delivery_mode": delivery_mode,
                }
                if active_episode:
                    facts.update(
                        episode_id=episode_id,
                        phase="confirmed",
                        outcome="confirmed_breakout",
                        volume_verdict=episode_volume_verdict,
                        volume_actual=episode_volume_actual,
                        source_report=plan_payload.get("analysis_ref"),
                        next_condition="wait for hysteresis clear before rearming",
                    )
                if isinstance(quote.get("price_volume"), dict):
                    facts["price_volume"] = quote["price_volume"]
                    facts["price_volume_bar_time"] = quote.get(
                        "price_volume_bar_time"
                    )
                if rule["rule_id"] in assessments:
                    facts["target_assessment"] = assessments[rule["rule_id"]]
                try:
                    connection.execute(
                        """INSERT INTO monitor_events(
                           event_id,profile_id,symbol,plan_version,rule_id,armed_epoch,kind,status,severity,
                           title,summary,facts_json,observation_id,first_seen_at,last_seen_at,
                           episode_id,phase,outcome,volume_verdict
                           ) VALUES(?,?,?,?,?,?,'market_rule_trigger','confirmed',?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            event_id, profile_id, profile["symbol"], version, rule["rule_id"], rule["armed_epoch"],
                            rule["severity"], event_title,
                            f"闭合行情连续 {required} 次满足 {rule['kind']}。仅供观察，不会执行交易。",
                            _json(facts), observation_id, now, now,
                            episode_id,
                            "confirmed" if active_episode else None,
                            "confirmed_breakout" if active_episode else None,
                            episode_volume_verdict,
                        ),
                    )
                except sqlite3.IntegrityError:
                    self._increment_counter(connection, "duplicate_event_count")
                    continue
                if profile["delivery_target_id"]:
                    delivery_status = "shadow_suppressed" if delivery_mode == "shadow" else "pending"
                    suppression_reason = "shadow_mode" if delivery_mode == "shadow" else None
                    suppressed_at = now if delivery_mode == "shadow" else None
                    connection.execute(
                        """INSERT OR IGNORE INTO delivery_outbox(
                           delivery_id,event_id,delivery_target_id,status,delivery_mode,would_deliver,
                           suppressed_at,suppression_reason,created_at,updated_at
                           ) VALUES(?,?,?,?,?,1,?,?,?,?)""",
                        (
                            uuid.uuid4().hex,
                            event_id,
                            profile["delivery_target_id"],
                            delivery_status,
                            delivery_mode,
                            suppressed_at,
                            suppression_reason,
                            now,
                            now,
                        ),
                    )
                    if delivery_mode == "shadow":
                        self._increment_counter(connection, "shadow_suppressed_delivery_count")
                if active_episode:
                    episode_facts = _loads(active_episode["facts_json"], {})
                    episode_facts.update(facts)
                    connection.execute(
                        """UPDATE monitor_watch_episodes SET state='confirmed',phase='confirmed',
                           outcome='confirmed_breakout',terminal_observation_id=?,resolved_at=?,
                           result_notified=1,volume_verdict=?,facts_json=?,updated_at=?
                           WHERE episode_id=?""",
                        (
                            observation_id,
                            now,
                            episode_volume_verdict,
                            _json(episode_facts),
                            now,
                            active_episode["episode_id"],
                        ),
                    )
                cooldown = int(rule["parameters"].get("cooldown_minutes", 60))
                cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=cooldown)).isoformat()
                connection.execute(
                    """UPDATE monitor_rules SET state='cooldown',confirmation_progress=0,last_condition_value=1,
                       last_bar_time=?,last_observation_id=?,last_triggered_at=?,cooldown_until=?,updated_at=?
                       WHERE rule_id=?""",
                    (quote.get("bar_time"), observation_id, now, cooldown_until, now, rule["rule_id"]),
                )
                created.append(event_id)
        return [event for event_id in created if (event := self.get_event(event_id))]

    @staticmethod
    def _bars_are_continuous(previous: str, current: str, interval: str) -> bool:
        if not previous or not current:
            return False
        try:
            left = datetime.fromisoformat(previous.replace("Z", "+00:00"))
            right = datetime.fromisoformat(current.replace("Z", "+00:00"))
            delta = (right - left).total_seconds()
        except (TypeError, ValueError):
            return False
        expected_seconds = {"1m": 60, "5m": 300}.get(interval)
        if expected_seconds is None:
            return False
        return abs(delta - expected_seconds) <= 1.0

    def resolve_market_close_episodes(
        self,
        profile_id: str,
        session_date: str,
        *,
        delivery_mode: str = "deliver",
        price_volume_mode: str = "off",
        lease_guard: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Close unfinished schema-v4 episodes once the market session is closed."""

        now = utc_now()
        created: list[str] = []
        with self.transaction() as connection:
            if lease_guard:
                self._assert_fenced_write(
                    connection,
                    lease_key=str(lease_guard["lease_key"]),
                    owner_id=str(lease_guard["owner_id"]),
                    fencing_token=int(lease_guard["fencing_token"]),
                )
            profile = connection.execute(
                "SELECT * FROM monitor_profiles WHERE profile_id=?", (profile_id,)
            ).fetchone()
            if not profile or profile["active_plan_version"] is None:
                return []
            version = int(profile["active_plan_version"])
            plan_row = connection.execute(
                """SELECT plan_json FROM monitor_plan_versions
                   WHERE profile_id=? AND version=?""",
                (profile_id, version),
            ).fetchone()
            plan = _loads(plan_row["plan_json"], {}) if plan_row else {}
            if int(plan.get("schema_version") or 1) < 4:
                return []
            episodes = connection.execute(
                """SELECT * FROM monitor_watch_episodes
                   WHERE profile_id=? AND plan_version=? AND session_date=?
                     AND state IN ('approaching','testing')""",
                (profile_id, version, session_date),
            ).fetchall()
            if not episodes:
                return []
            latest = connection.execute(
                """SELECT payload_json FROM monitor_observations
                   WHERE profile_id=? AND domain='quote'
                   ORDER BY observed_at DESC LIMIT 1""",
                (profile_id,),
            ).fetchone()
            quote = _loads(latest["payload_json"], {}) if latest else {}
            quote.setdefault("session_date", session_date)
            observation_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO monitor_observations(
                   observation_id,profile_id,domain,source_key,observed_at,data_as_of,status,
                   payload_json,payload_hash
                   ) VALUES(?,?,'episode_close','market_calendar',?,?, 'verified',?,?)""",
                (
                    observation_id,
                    profile_id,
                    now,
                    quote.get("bar_time"),
                    _json({"session_date": session_date, "last_quote": quote}),
                    _hash({"session_date": session_date, "profile_id": profile_id}),
                ),
            )
            scenarios = {
                str(item.get("client_rule_id") or ""): item
                for item in (plan.get("watch_scenarios") or [])
            }
            for episode in episodes:
                scenario = scenarios.get(str(episode["client_rule_id"]), {})
                event_id = self._resolve_watch_episode(
                    connection,
                    profile=profile,
                    plan_version=version,
                    episode=episode,
                    scenario={**scenario, "analysis_ref": plan.get("analysis_ref")},
                    quote=quote,
                    observation_id=observation_id,
                    outcome="unresolved",
                    delivery_mode=delivery_mode,
                    price_volume_mode=price_volume_mode,
                    now=now,
                )
                if event_id:
                    created.append(event_id)
        return [event for event_id in created if (event := self.get_event(event_id))]

    def list_watch_episodes(
        self,
        profile_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM monitor_watch_episodes
                   WHERE profile_id=? ORDER BY started_at DESC LIMIT ?""",
                (profile_id, max(1, min(int(limit), 500))),
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["facts"] = _loads(value.pop("facts_json", "{}"), {})
            value["approach_notified"] = bool(value.get("approach_notified"))
            value["result_notified"] = bool(value.get("result_notified"))
            values.append(value)
        return values

    def list_events(self, *, limit: int = 50, symbol: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        clause = ""
        if symbol:
            clause = "WHERE symbol=?"
            params.append(symbol.upper())
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM monitor_events {clause} ORDER BY first_seen_at DESC LIMIT ?", params
            ).fetchall()
        return [self._event(row) for row in rows]

    def latest_event_cursor(self) -> str | None:
        """Return the newest durable event id without replaying event content."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT event_id FROM monitor_events ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return str(row["event_id"]) if row else None

    def list_events_from_start(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return the oldest durable events in monotonic insertion order."""

        bounded_limit = max(1, min(int(limit), 1000))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_events ORDER BY rowid ASC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [self._event(row) for row in rows]

    def list_events_after(self, event_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Return events after a known cursor in monotonic insertion order."""

        bounded_limit = max(1, min(int(limit), 1000))
        with self.connect() as connection:
            cursor = connection.execute(
                "SELECT rowid AS event_rowid FROM monitor_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if not cursor:
                raise KeyError(event_id)
            rows = connection.execute(
                """SELECT * FROM monitor_events
                   WHERE rowid>? ORDER BY rowid ASC LIMIT ?""",
                (cursor["event_rowid"], bounded_limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def list_signal_states(self, profile_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM monitor_signal_states WHERE profile_id=?
                   ORDER BY signal_type,client_rule_id""",
                (profile_id,),
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["payload"] = _loads(value.pop("payload_json", "{}"), {})
            values.append(value)
        return values

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM monitor_events WHERE event_id=?", (event_id,)).fetchone()
            deliveries = connection.execute("SELECT * FROM delivery_outbox WHERE event_id=?", (event_id,)).fetchall()
        if not row:
            return None
        value = self._event(row)
        value["deliveries"] = [self._delivery(delivery) for delivery in deliveries]
        return value

    def acknowledge_event(self, event_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                "UPDATE monitor_events SET status='resolved',acknowledged_at=?,last_seen_at=? WHERE event_id=?",
                (utc_now(), utc_now(), event_id),
            )
        value = self.get_event(event_id)
        if not value:
            raise KeyError(event_id)
        return value

    def pending_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT d.*, t.channel,t.chat_id,t.chat_type,t.session_key,
                          e.profile_id,e.symbol
                   FROM delivery_outbox d
                   JOIN delivery_targets t ON t.target_id=d.delivery_target_id
                   JOIN monitor_events e ON e.event_id=d.event_id
                   WHERE d.status='pending' AND t.status='active' ORDER BY d.created_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._delivery(row) for row in rows]

    def deliver_readiness(
        self,
        *,
        allowlist: set[str],
        test_target_id: str | None,
        daily_limit: int,
        soak_approved: bool,
        callback_ready: bool,
        max_profiles: int = 1,
    ) -> dict[str, Any]:
        """Evaluate the fail-closed gates required before real delivery."""

        normalized = {str(value).strip().upper() for value in allowlist if str(value).strip()}
        reasons: list[str] = []
        if not soak_approved:
            reasons.append("shadow_soak_not_approved")
        if not callback_ready:
            reasons.append("delivery_callback_unavailable")
        if not normalized:
            reasons.append("deliver_allowlist_required")
        if len(normalized) > max(1, int(max_profiles)):
            reasons.append("deliver_allowlist_exceeds_release_limit")
        if not test_target_id:
            reasons.append("private_test_target_required")
        if int(daily_limit) < 1:
            reasons.append("daily_delivery_limit_invalid")

        with self.connect() as connection:
            target = (
                connection.execute(
                    "SELECT * FROM delivery_targets WHERE target_id=?",
                    (test_target_id,),
                ).fetchone()
                if test_target_id
                else None
            )
            if test_target_id and (
                not target
                or target["status"] != "active"
                or target["chat_type"] != "p2p"
            ):
                reasons.append("private_test_target_not_active")
            profiles = connection.execute(
                """SELECT profile_id,symbol,status,delivery_target_id
                   FROM monitor_profiles"""
            ).fetchall()
            uncertain = int(
                connection.execute(
                    "SELECT COUNT(*) FROM delivery_outbox WHERE status='delivery_uncertain'"
                ).fetchone()[0]
            )

        resolved: list[dict[str, Any]] = []
        for row in profiles:
            if (
                str(row["profile_id"]).upper() in normalized
                or str(row["symbol"]).upper() in normalized
            ):
                resolved.append(dict(row))
        if normalized and len(resolved) != len(normalized):
            reasons.append("deliver_allowlist_profile_not_found")
        if any(row["status"] != "active" for row in resolved):
            reasons.append("deliver_allowlist_profile_not_active")
        if test_target_id and any(
            str(row["delivery_target_id"] or "") != test_target_id for row in resolved
        ):
            reasons.append("deliver_profile_target_mismatch")
        if uncertain:
            reasons.append("unresolved_delivery_uncertain")
        return {
            "ready": not reasons,
            "blocked_reasons": sorted(set(reasons)),
            "allowlist": sorted(normalized),
            "resolved_profile_ids": sorted(str(row["profile_id"]) for row in resolved),
            "test_target_id": test_target_id,
            "daily_limit": int(daily_limit),
            "max_profiles": max(1, int(max_profiles)),
            "soak_approved": bool(soak_approved),
            "callback_ready": bool(callback_ready),
            "uncertain_deliveries": uncertain,
        }

    def suppress_pending_deliveries(self, *, reason: str = "shadow_mode") -> int:
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE delivery_outbox SET status='shadow_suppressed',delivery_mode='shadow',
                   would_deliver=1,suppressed_at=?,suppression_reason=?,updated_at=?
                   WHERE status='pending'""",
                (now, reason, now),
            )
            if cursor.rowcount:
                self._increment_counter(
                    connection,
                    "shadow_suppressed_delivery_count",
                    int(cursor.rowcount),
                )
            return int(cursor.rowcount)

    def suppress_delivery(self, delivery_id: str, *, reason: str) -> bool:
        """Suppress one still-pending delivery after a feature kill-switch change."""

        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE delivery_outbox SET status='shadow_suppressed',delivery_mode='shadow',
                   would_deliver=1,suppressed_at=?,suppression_reason=?,updated_at=?
                   WHERE delivery_id=? AND status='pending'""",
                (now, reason, now, delivery_id),
            )
            if cursor.rowcount:
                self._increment_counter(
                    connection,
                    "shadow_suppressed_delivery_count",
                )
            return cursor.rowcount == 1

    def mark_delivering_uncertain(
        self,
        *,
        reason: str,
        owner_id: str | None = None,
        stale_before: str | None = None,
    ) -> int:
        now = utc_now()
        clauses = ["status='delivering'"]
        params: list[Any] = [reason, now]
        if owner_id is not None:
            clauses.append("(claim_owner_id=? OR claim_owner_id IS NULL)")
            params.append(owner_id)
        if stale_before is not None:
            clauses.append("claimed_at<?")
            params.append(stale_before)
        with self.transaction() as connection:
            cursor = connection.execute(
                f"""UPDATE delivery_outbox
                    SET status='delivery_uncertain',receipt_status='delivery_uncertain',
                        error=?,updated_at=?
                    WHERE {' AND '.join(clauses)}""",
                params,
            )
            return int(cursor.rowcount)

    def recover_stale_deliveries(self, *, timeout_seconds: int = 180) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(1, timeout_seconds))
        ).isoformat()
        return self.mark_delivering_uncertain(
            reason="delivery claim timed out during runtime recovery",
            stale_before=cutoff,
        )

    def claim_delivery(
        self,
        delivery_id: str,
        *,
        lease_guard: dict[str, Any] | None = None,
        daily_limit: int | None = None,
        required_target_id: str | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        with self.transaction() as connection:
            if lease_guard:
                self._assert_fenced_write(
                    connection,
                    lease_key=str(lease_guard["lease_key"]),
                    owner_id=str(lease_guard["owner_id"]),
                    fencing_token=int(lease_guard["fencing_token"]),
                )
            row = connection.execute(
                "SELECT delivery_target_id FROM delivery_outbox WHERE delivery_id=? AND status='pending'",
                (delivery_id,),
            ).fetchone()
            if not row:
                return False
            if required_target_id and str(row["delivery_target_id"]) != required_target_id:
                return False
            if daily_limit is not None:
                local_now = now.astimezone(_CN_TZ)
                start_of_day = local_now.replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                ).astimezone(timezone.utc).isoformat()
                sent_today = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM delivery_outbox
                           WHERE status IN ('delivered','delivering','delivery_uncertain')
                             AND claimed_at>=?""",
                        (start_of_day,),
                    ).fetchone()[0]
                )
                if sent_today >= max(1, int(daily_limit)):
                    connection.execute(
                        "UPDATE delivery_outbox SET error='daily_delivery_limit_reached',updated_at=? WHERE delivery_id=?",
                        (now_text, delivery_id),
                    )
                    return False
            owner_id = str(lease_guard["owner_id"]) if lease_guard else None
            fencing_token = int(lease_guard["fencing_token"]) if lease_guard else None
            cursor = connection.execute(
                """UPDATE delivery_outbox SET status='delivering',attempts=attempts+1,
                       claimed_at=?,claim_owner_id=?,claim_fencing_token=?,error=NULL,updated_at=?
                   WHERE delivery_id=? AND status='pending'""",
                (now_text, owner_id, fencing_token, now_text, delivery_id),
            )
            return cursor.rowcount == 1

    def finish_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        remote_message_id: str | None = None,
        provider: str | None = None,
        provider_request_id: str | None = None,
        accepted_at: str | None = None,
        receipt_status: str | None = None,
        error: str | None = None,
        lease_guard: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"delivered", "rejected", "delivery_uncertain"}:
            raise ValueError("invalid terminal delivery status")
        now = utc_now()
        with self.transaction() as connection:
            if lease_guard:
                self._assert_fenced_write(
                    connection,
                    lease_key=str(lease_guard["lease_key"]),
                    owner_id=str(lease_guard["owner_id"]),
                    fencing_token=int(lease_guard["fencing_token"]),
                )
            cursor = connection.execute(
                """UPDATE delivery_outbox SET
                       status=?,remote_message_id=?,provider=?,provider_request_id=?,
                       accepted_at=?,receipt_status=?,error=?,
                       delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END,
                       updated_at=?
                   WHERE delivery_id=? AND status='delivering'
                     AND (? IS NULL OR claim_owner_id=? OR claim_owner_id IS NULL)
                     AND (? IS NULL OR claim_fencing_token=?)""",
                (
                    status,
                    remote_message_id,
                    provider,
                    provider_request_id,
                    accepted_at,
                    receipt_status or status,
                    error,
                    status,
                    now,
                    now,
                    delivery_id,
                    str(lease_guard["owner_id"]) if lease_guard else None,
                    str(lease_guard["owner_id"]) if lease_guard else None,
                    int(lease_guard["fencing_token"]) if lease_guard else None,
                    int(lease_guard["fencing_token"]) if lease_guard else None,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleLeaderError("delivery claim is no longer owned by this runtime")

    def reconcile_uncertain_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        remote_message_id: str | None = None,
        provider: str = "feishu",
        note: str,
    ) -> dict[str, Any]:
        """Manually close an ambiguous provider outcome without retrying it."""

        if status not in {"delivered", "rejected"}:
            raise ValueError("manual delivery status must be delivered or rejected")
        if status == "delivered" and not str(remote_message_id or "").strip():
            raise ValueError("remote_message_id is required for delivered reconciliation")
        if not str(note or "").strip():
            raise ValueError("a reconciliation note is required")
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE delivery_outbox SET status=?,remote_message_id=?,provider=?,
                       accepted_at=CASE WHEN ?='delivered' THEN COALESCE(accepted_at,?) ELSE accepted_at END,
                       receipt_status=?,error=?,
                       delivered_at=CASE WHEN ?='delivered' THEN COALESCE(delivered_at,?) ELSE delivered_at END,
                       updated_at=?
                   WHERE delivery_id=? AND status='delivery_uncertain'""",
                (
                    status,
                    str(remote_message_id).strip() if remote_message_id else None,
                    provider,
                    status,
                    now,
                    status,
                    f"manually_reconciled: {str(note).strip()}",
                    status,
                    now,
                    now,
                    delivery_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("delivery is not awaiting manual reconciliation")
            row = connection.execute(
                "SELECT * FROM delivery_outbox WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        assert row is not None
        return self._delivery(row)

    def record_runtime_tick(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {
            "tick_id": str(payload.get("tick_id") or uuid.uuid4().hex),
            "owner_id": str(payload.get("owner_id") or "unknown"),
            "mode": str(payload.get("mode") or "off"),
            "decision": str(payload.get("decision") or "unknown"),
            "started_at": str(payload.get("started_at") or utc_now()),
            "finished_at": str(payload.get("finished_at") or utc_now()),
            "duration_ms": float(payload.get("duration_ms") or 0),
            "due_profiles": int(payload.get("due_profiles") or 0),
            "all_due_profiles": int(payload.get("all_due_profiles") or 0),
            "evaluated_profiles": int(payload.get("evaluated_profiles") or 0),
            "blocked_profiles": int(payload.get("blocked_profiles") or 0),
            "supported_blocked_profiles": int(
                payload.get("supported_blocked_profiles") or 0
            ),
            "outcome_profiles": int(payload.get("outcome_profiles") or 0),
            "outcome_contract_version": 1,
            "events_created": int(payload.get("events_created") or 0),
            "duplicate_events": int(payload.get("duplicate_events") or 0),
            "shadow_suppressed": int(payload.get("shadow_suppressed") or 0),
            "schedule_lag_ms": payload.get("schedule_lag_ms"),
            "closed_session_due_profiles": int(
                payload.get("closed_session_due_profiles") or 0
            ),
            "closed_session_backlog_lag_ms": payload.get(
                "closed_session_backlog_lag_ms"
            ),
            "bar_lag_ms": payload.get("bar_lag_ms"),
            "database_size_bytes": self.database_size_bytes(),
            "error": payload.get("error"),
        }
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO monitor_runtime_ticks(
                   tick_id,owner_id,mode,decision,started_at,finished_at,duration_ms,
                   due_profiles,all_due_profiles,evaluated_profiles,blocked_profiles,
                   supported_blocked_profiles,outcome_profiles,outcome_contract_version,
                   events_created,duplicate_events,
                   shadow_suppressed,schedule_lag_ms,closed_session_due_profiles,
                   closed_session_backlog_lag_ms,bar_lag_ms,database_size_bytes,error
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(row.values()),
            )
        return row

    def database_size_bytes(self) -> int:
        return sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                self.path.with_name(f"{self.path.name}-wal"),
                self.path.with_name(f"{self.path.name}-shm"),
            )
            if candidate.exists()
        )

    def runtime_health(self, *, hours: int = 24) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT duration_ms,schedule_lag_ms,due_profiles,all_due_profiles,
                          evaluated_profiles,supported_blocked_profiles,outcome_profiles,
                          outcome_contract_version,
                          closed_session_due_profiles,
                          closed_session_backlog_lag_ms,bar_lag_ms,database_size_bytes,
                          events_created,duplicate_events,error
                   FROM monitor_runtime_ticks WHERE finished_at>=? ORDER BY finished_at""",
                (cutoff,),
            ).fetchall()
            latest = connection.execute(
                "SELECT * FROM monitor_runtime_ticks ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
            counter_rows = connection.execute(
                "SELECT name,value,updated_at FROM monitor_runtime_counters ORDER BY name"
            ).fetchall()
        durations = [float(row["duration_ms"]) for row in rows]
        schedule_lags = [
            float(row["schedule_lag_ms"])
            for row in rows
            if int(row["outcome_contract_version"] or 0) >= 1
            and row["schedule_lag_ms"] is not None
        ]
        closed_backlog_lags = [
            float(row["closed_session_backlog_lag_ms"])
            for row in rows
            if row["closed_session_backlog_lag_ms"] is not None
        ]
        bar_lags = [float(row["bar_lag_ms"]) for row in rows if row["bar_lag_ms"] is not None]
        sizes = [int(row["database_size_bytes"]) for row in rows]
        events_created = sum(int(row["events_created"]) for row in rows)
        duplicate_events = sum(int(row["duplicate_events"]) for row in rows)
        event_attempts = events_created + duplicate_events
        return {
            "window_hours": max(1, hours),
            "tick_count": len(rows),
            "error_tick_count": sum(1 for row in rows if row["error"]),
            "outcome_invariant_failure_count": sum(
                1
                for row in rows
                if int(row["outcome_contract_version"] or 0) >= 1
                and (
                    int(row["outcome_profiles"] or 0)
                    != int(row["all_due_profiles"] or 0)
                    or int(row["evaluated_profiles"] or 0)
                    + int(row["supported_blocked_profiles"] or 0)
                    != int(row["due_profiles"] or 0)
                )
            ),
            "events_created": events_created,
            "duplicate_event_count": duplicate_events,
            "event_attempt_count": event_attempts,
            "duplicate_event_rate": round(duplicate_events / event_attempts, 6)
            if event_attempts
            else 0.0,
            "duration_ms": {
                "p50": _percentile(durations, 0.50),
                "p95": _percentile(durations, 0.95),
                "p99": _percentile(durations, 0.99),
                "max": round(max(durations), 3) if durations else None,
            },
            "schedule_lag_ms": {
                "p50": _percentile(schedule_lags, 0.50),
                "p95": _percentile(schedule_lags, 0.95),
                "p99": _percentile(schedule_lags, 0.99),
                "max": round(max(schedule_lags), 3) if schedule_lags else None,
            },
            "closed_session_backlog": {
                "due_profile_ticks": sum(
                    int(row["closed_session_due_profiles"] or 0) for row in rows
                ),
                "lag_ms": {
                    "p50": _percentile(closed_backlog_lags, 0.50),
                    "p95": _percentile(closed_backlog_lags, 0.95),
                    "p99": _percentile(closed_backlog_lags, 0.99),
                    "max": round(max(closed_backlog_lags), 3)
                    if closed_backlog_lags
                    else None,
                },
            },
            "bar_lag_ms": {
                "p50": _percentile(bar_lags, 0.50),
                "p95": _percentile(bar_lags, 0.95),
                "p99": _percentile(bar_lags, 0.99),
                "max": round(max(bar_lags), 3) if bar_lags else None,
            },
            "database_growth_bytes": (sizes[-1] - sizes[0]) if len(sizes) >= 2 else 0,
            "latest_tick": dict(latest) if latest else None,
            "counters": {
                str(row["name"]): {
                    "value": int(row["value"]),
                    "updated_at": row["updated_at"],
                }
                for row in counter_rows
            },
        }

    def maintenance_status(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_maintenance_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["details"] = _loads(value.pop("details_json", "{}"), {})
        return value

    def _backup_database(self) -> Path:
        backup_root = Path(
            os.getenv(
                "VIBE_TRADING_MONITOR_BACKUP_DIR",
                str(self.path.parent / "backups"),
            )
        ).expanduser()
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = backup_root / f"monitoring-{stamp}.sqlite3"
        source = self.connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination

    def _prune_backups(self, backup_root: Path, *, keep_days: int) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - max(1, keep_days) * 86400
        removed = 0
        for candidate in backup_root.glob("monitoring-*.sqlite3"):
            try:
                if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def run_maintenance(self, *, force: bool = False) -> dict[str, Any]:
        interval_hours = _env_int(
            "VIBE_TRADING_MONITOR_BACKUP_INTERVAL_HOURS",
            24,
            minimum=1,
        )
        last = self.maintenance_status()
        if not force and last and last.get("status") == "completed":
            try:
                finished = datetime.fromisoformat(str(last.get("finished_at") or "").replace("Z", "+00:00"))
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - finished < timedelta(hours=interval_hours):
                    return {**last, "skipped": True, "reason": "maintenance_not_due"}
            except ValueError:
                pass

        maintenance_id = uuid.uuid4().hex
        started_at = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO monitor_maintenance_runs(
                   maintenance_id,kind,status,started_at,details_json
                   ) VALUES(?,'daily','running',?,'{}')""",
                (maintenance_id, started_at),
            )
        before = self.database_size_bytes()
        details: dict[str, Any] = {"database_size_before_bytes": before}
        try:
            backup = self._backup_database()
            observation_days = _env_int(
                "VIBE_TRADING_MONITOR_OBSERVATION_RETENTION_DAYS",
                30,
                minimum=1,
            )
            metric_days = _env_int(
                "VIBE_TRADING_MONITOR_METRICS_RETENTION_DAYS",
                30,
                minimum=1,
            )
            observation_cutoff = (datetime.now(timezone.utc) - timedelta(days=observation_days)).isoformat()
            metric_cutoff = (datetime.now(timezone.utc) - timedelta(days=metric_days)).isoformat()
            with self.transaction() as connection:
                observations = connection.execute(
                    """DELETE FROM monitor_observations WHERE observed_at<?
                       AND observation_id NOT IN (
                           SELECT observation_id FROM monitor_events WHERE observation_id IS NOT NULL
                       )""",
                    (observation_cutoff,),
                ).rowcount
                ticks = connection.execute(
                    "DELETE FROM monitor_runtime_ticks WHERE finished_at<?",
                    (metric_cutoff,),
                ).rowcount
            with self.connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            backup_days = _env_int(
                "VIBE_TRADING_MONITOR_BACKUP_KEEP_DAYS",
                14,
                minimum=1,
            )
            removed_backups = self._prune_backups(backup.parent, keep_days=backup_days)
            details.update(
                backup_path=str(backup),
                observations_pruned=int(observations),
                runtime_ticks_pruned=int(ticks),
                expired_backups_pruned=removed_backups,
                database_size_after_bytes=self.database_size_bytes(),
                observation_retention_days=observation_days,
                metric_retention_days=metric_days,
                backup_keep_days=backup_days,
            )
            status = "completed"
            error = None
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            details["database_size_after_bytes"] = self.database_size_bytes()
        finished_at = utc_now()
        with self.connect() as connection:
            connection.execute(
                """UPDATE monitor_maintenance_runs SET status=?,finished_at=?,details_json=?,error=?
                   WHERE maintenance_id=?""",
                (status, finished_at, _json(details), error, maintenance_id),
            )
        result = self.maintenance_status()
        assert result is not None
        return result

    def acquire_fenced_lease(
        self,
        lease_key: str,
        owner_id: str,
        *,
        ttl_seconds: int = 90,
    ) -> int | None:
        """Acquire/renew a lease and return its monotonically increasing token."""

        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_leases WHERE lease_key=?",
                (lease_key,),
            ).fetchone()
            if (
                row
                and row["owner_id"] != owner_id
                and str(row["expires_at"]) > now_text
            ):
                return None
            previous_token = int(row["fencing_token"] or 0) if row else 0
            same_live_owner = bool(
                row
                and row["owner_id"] == owner_id
                and str(row["expires_at"]) > now_text
            )
            token = previous_token if same_live_owner else previous_token + 1
            connection.execute(
                """INSERT INTO runtime_leases(
                       lease_key,owner_id,fencing_token,expires_at,updated_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(lease_key) DO UPDATE SET
                       owner_id=excluded.owner_id,
                       fencing_token=excluded.fencing_token,
                       expires_at=excluded.expires_at,
                       updated_at=excluded.updated_at""",
                (lease_key, owner_id, token, expires, now_text),
            )
        return token

    def acquire_lease(
        self,
        lease_key: str,
        owner_id: str,
        *,
        ttl_seconds: int = 90,
    ) -> bool:
        """Compatibility wrapper for callers that do not yet need fencing."""

        return self.acquire_fenced_lease(
            lease_key,
            owner_id,
            ttl_seconds=ttl_seconds,
        ) is not None

    def heartbeat_lease(
        self,
        lease_key: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: int = 90,
    ) -> bool:
        now = datetime.now(timezone.utc)
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE runtime_leases SET expires_at=?,updated_at=?
                   WHERE lease_key=? AND owner_id=? AND fencing_token=?
                     AND expires_at>?""",
                (
                    (now + timedelta(seconds=max(1, ttl_seconds))).isoformat(),
                    now.isoformat(),
                    lease_key,
                    owner_id,
                    int(fencing_token),
                    now.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def claim_profiles(
        self,
        *,
        lease_key: str,
        owner_id: str,
        fencing_token: int,
        tick_id: str,
        profile_ids: list[str],
        ttl_seconds: int = 90,
    ) -> set[str]:
        """Claim due profiles under the current fencing token."""

        if not profile_ids:
            return set()
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        claimed: set[str] = set()
        with self.transaction() as connection:
            lease = connection.execute(
                """SELECT 1 FROM runtime_leases
                   WHERE lease_key=? AND owner_id=? AND fencing_token=? AND expires_at>?""",
                (lease_key, owner_id, int(fencing_token), now_text),
            ).fetchone()
            if not lease:
                return set()
            for profile_id in profile_ids:
                existing = connection.execute(
                    "SELECT * FROM monitor_profile_claims WHERE profile_id=?",
                    (profile_id,),
                ).fetchone()
                if (
                    existing
                    and str(existing["expires_at"]) > now_text
                    and (
                        existing["owner_id"] != owner_id
                        or int(existing["fencing_token"]) != int(fencing_token)
                    )
                ):
                    continue
                connection.execute(
                    """INSERT INTO monitor_profile_claims(
                           profile_id,tick_id,owner_id,fencing_token,expires_at,updated_at
                       ) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(profile_id) DO UPDATE SET
                           tick_id=excluded.tick_id,
                           owner_id=excluded.owner_id,
                           fencing_token=excluded.fencing_token,
                           expires_at=excluded.expires_at,
                           updated_at=excluded.updated_at""",
                    (
                        profile_id,
                        tick_id,
                        owner_id,
                        int(fencing_token),
                        expires,
                        now_text,
                    ),
                )
                claimed.add(profile_id)
        return claimed

    @staticmethod
    def _assert_fenced_write(
        connection: sqlite3.Connection,
        *,
        lease_key: str,
        owner_id: str,
        fencing_token: int,
        profile_id: str | None = None,
        tick_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        lease = connection.execute(
            """SELECT 1 FROM runtime_leases
               WHERE lease_key=? AND owner_id=? AND fencing_token=? AND expires_at>?""",
            (lease_key, owner_id, int(fencing_token), now),
        ).fetchone()
        if not lease:
            raise StaleLeaderError("runtime lease is no longer valid")
        if profile_id is not None:
            claim = connection.execute(
                """SELECT 1 FROM monitor_profile_claims
                   WHERE profile_id=? AND owner_id=? AND fencing_token=?
                     AND tick_id=? AND expires_at>?""",
                (profile_id, owner_id, int(fencing_token), tick_id or "", now),
            ).fetchone()
            if not claim:
                raise StaleLeaderError("profile claim is no longer valid")

    def release_lease(self, lease_key: str, owner_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM runtime_leases WHERE lease_key=? AND owner_id=?",
                (lease_key, owner_id),
            )
            connection.execute(
                "DELETE FROM monitor_profile_claims WHERE owner_id=?",
                (owner_id,),
            )

    def get_autopilot_config(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_autopilot_config WHERE config_id='default'"
            ).fetchone()
        if row is None:
            return {
                "config_id": "default",
                "enabled": False,
                "activation_mode": "autonomous",
                "research_policy": "if_needed",
                "trigger_types": [
                    "report_ready", "holdings_changed", "scheduled_close", "approaching",
                    "invalidated", "material_evidence_changed",
                ],
                "selected_symbols": [],
                "daily_close_enabled": True,
                "delivery_target_id": None,
                "runtime_mode": "shadow",
                "revision": 0,
                "created_at": None,
                "updated_at": None,
                "automatic_trading": "forbidden",
            }
        value = dict(row)
        value["enabled"] = bool(value.get("enabled"))
        value["daily_close_enabled"] = bool(value.get("daily_close_enabled"))
        value["trigger_types"] = _loads(value.pop("trigger_types_json", "[]"), [])
        value["selected_symbols"] = _normalize_autopilot_symbols(
            _loads(value.pop("selected_symbols_json", "[]"), [])
        )
        value["automatic_trading"] = "forbidden"
        return value

    def set_autopilot_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_autopilot_config()
        allowed_trigger_types = {
            "report_ready", "holdings_changed", "scheduled_close", "approaching",
            "invalidated", "material_evidence_changed",
        }
        trigger_types = payload.get("trigger_types", current["trigger_types"])
        if not isinstance(trigger_types, list) or not set(trigger_types).issubset(allowed_trigger_types):
            raise ValueError("autopilot trigger_types contains an unsupported value")
        selected_symbols = _normalize_autopilot_symbols(
            payload.get("selected_symbols", current.get("selected_symbols", []))
        )
        enabled = payload.get("enabled", current["enabled"])
        daily_close = payload.get("daily_close_enabled", current["daily_close_enabled"])
        if not isinstance(enabled, bool) or not isinstance(daily_close, bool):
            raise ValueError("autopilot flags must be booleans")
        if not selected_symbols:
            enabled = False
        runtime_mode = str(payload.get("runtime_mode") or current["runtime_mode"] or "shadow")
        if runtime_mode not in {"shadow", "deliver"}:
            raise ValueError("autopilot runtime_mode must be shadow or deliver")
        target_id = payload.get("delivery_target_id", current.get("delivery_target_id"))
        if target_id:
            target = self.get_target(str(target_id))
            if not target or target["status"] != "active":
                raise ValueError("autopilot delivery target is not active")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO monitor_autopilot_config(
                       config_id,enabled,activation_mode,research_policy,trigger_types_json,
                       selected_symbols_json,daily_close_enabled,delivery_target_id,runtime_mode,
                       revision,created_at,updated_at
                   ) VALUES('default',?,'autonomous','if_needed',?,?,?,?,?,1,?,?)
                   ON CONFLICT(config_id) DO UPDATE SET
                       enabled=excluded.enabled,trigger_types_json=excluded.trigger_types_json,
                       selected_symbols_json=excluded.selected_symbols_json,
                       daily_close_enabled=excluded.daily_close_enabled,
                       delivery_target_id=excluded.delivery_target_id,runtime_mode=excluded.runtime_mode,
                       revision=monitor_autopilot_config.revision+1,updated_at=excluded.updated_at""",
                (
                    int(enabled), _json(sorted(set(trigger_types))), _json(selected_symbols),
                    int(daily_close),
                    str(target_id) if target_id else None, runtime_mode, now, now,
                ),
            )
        return self.get_autopilot_config()

    def cancel_autopilot_symbols(
        self,
        symbols: set[str] | list[str],
        *,
        reason: str = "selection_removed",
    ) -> int:
        """Cancel non-terminal autonomous triggers and their planner jobs."""

        normalized = _normalize_autopilot_symbols(list(symbols))
        if not normalized:
            return 0
        placeholders = ",".join("?" for _ in normalized)
        now = utc_now()
        with self.transaction() as connection:
            rows = connection.execute(
                f"""SELECT trigger_id,planner_job_id FROM monitor_autopilot_triggers
                    WHERE symbol IN ({placeholders}) AND status IN ('queued','running')""",
                tuple(normalized),
            ).fetchall()
            autonomous_jobs = connection.execute(
                f"""SELECT DISTINCT jobs.job_id
                    FROM monitor_planner_jobs AS jobs
                    JOIN monitor_planner_job_items AS items ON items.job_id=jobs.job_id
                    WHERE jobs.activation_mode='autonomous'
                      AND jobs.status IN ('queued','researching','planning','validating')
                      AND items.symbol IN ({placeholders})""",
                tuple(normalized),
            ).fetchall()
            job_ids = sorted({
                *(
                    str(row["planner_job_id"])
                    for row in rows
                    if row["planner_job_id"]
                ),
                *(str(row["job_id"]) for row in autonomous_jobs),
            })
            connection.execute(
                f"""UPDATE monitor_autopilot_triggers SET status='cancelled',
                       completed_at=COALESCE(completed_at,?),error=?,updated_at=?
                    WHERE symbol IN ({placeholders}) AND status IN ('queued','running')""",
                (now, reason, now, *normalized),
            )
            if job_ids:
                job_placeholders = ",".join("?" for _ in job_ids)
                connection.execute(
                    f"""UPDATE monitor_planner_jobs SET status='cancelled',cancel_requested=1,
                           completed_at=COALESCE(completed_at,?),updated_at=?
                        WHERE job_id IN ({job_placeholders})
                          AND status IN ('queued','researching','planning','validating')""",
                    (now, now, *job_ids),
                )
                connection.execute(
                    f"""UPDATE monitor_planner_job_items SET status='cancelled',
                           completed_at=COALESCE(completed_at,?),updated_at=?
                        WHERE job_id IN ({job_placeholders})
                          AND status IN ('queued','researching','planning','validating')""",
                    (now, now, *job_ids),
                )
        return len(rows) + len(autonomous_jobs)

    def enqueue_autopilot_trigger(
        self,
        *,
        symbol: str,
        trigger_type: str,
        dedupe_key: str,
        payload: dict[str, Any] | None = None,
        evidence_fingerprint: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if trigger_type not in {
            "report_ready", "holdings_changed", "scheduled_close", "approaching",
            "invalidated", "material_evidence_changed",
        }:
            raise ValueError("unsupported autopilot trigger type")
        normalized_symbol = normalize_symbol(symbol).upper()
        config = self.get_autopilot_config()
        if (
            not config.get("enabled")
            or normalized_symbol not in set(config.get("selected_symbols") or [])
        ):
            raise ValueError("symbol is not selected for autonomous monitoring")
        trigger_id = uuid.uuid4().hex
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO monitor_autopilot_triggers(
                       trigger_id,symbol,trigger_type,dedupe_key,status,payload_json,
                       evidence_fingerprint,created_at,updated_at
                   ) VALUES(?,?,?,?, 'queued', ?,?,?,?)
                   ON CONFLICT(symbol,trigger_type,dedupe_key) DO NOTHING""",
                (
                    trigger_id, normalized_symbol, trigger_type, dedupe_key,
                    _json(payload or {}), evidence_fingerprint, now, now,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                """SELECT * FROM monitor_autopilot_triggers
                   WHERE symbol=? AND trigger_type=? AND dedupe_key=?""",
                (normalized_symbol, trigger_type, dedupe_key),
            ).fetchone()
        assert row is not None
        value = dict(row)
        value["payload"] = _loads(value.pop("payload_json", "{}"), {})
        return value, created

    def list_autopilot_triggers(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if status:
                rows = connection.execute(
                    """SELECT * FROM monitor_autopilot_triggers WHERE status=?
                       ORDER BY created_at DESC LIMIT ?""",
                    (status, max(1, min(limit, 500))),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM monitor_autopilot_triggers
                       ORDER BY created_at DESC LIMIT ?""",
                    (max(1, min(limit, 500)),),
                ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = _loads(value.pop("payload_json", "{}"), {})
            result.append(value)
        return result

    def update_autopilot_trigger(
        self,
        trigger_id: str,
        *,
        status: str,
        planner_job_id: str | None = None,
        evidence_fingerprint: str | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        started = now if status == "running" else None
        completed = now if status in {"completed", "blocked", "failed", "cancelled"} else None
        with self.connect() as connection:
            connection.execute(
                """UPDATE monitor_autopilot_triggers SET status=?,
                       planner_job_id=COALESCE(?,planner_job_id),
                       evidence_fingerprint=COALESCE(?,evidence_fingerprint),
                       started_at=COALESCE(started_at,?),completed_at=?,
                       error=?,updated_at=? WHERE trigger_id=?""",
                (
                    status, planner_job_id, evidence_fingerprint, started,
                    completed, error, now, trigger_id,
                ),
            )

    def save_evidence_bundle(
        self,
        *,
        symbol: str,
        bundle: dict[str, Any],
        job_id: str | None = None,
        trigger_id: str | None = None,
    ) -> dict[str, Any]:
        fingerprint = str(bundle.get("evidence_fingerprint") or _hash(bundle))
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO monitor_evidence_bundles(
                       bundle_id,job_id,trigger_id,symbol,evidence_fingerprint,bundle_json,
                       bundle_sha256,data_as_of,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,evidence_fingerprint) DO UPDATE SET
                       job_id=COALESCE(excluded.job_id,job_id),
                       trigger_id=COALESCE(excluded.trigger_id,trigger_id)""",
                (
                    uuid.uuid4().hex, job_id, trigger_id, symbol.upper(), fingerprint,
                    _json(bundle), _hash(bundle), bundle.get("collected_at"), now,
                ),
            )
            row = connection.execute(
                """SELECT * FROM monitor_evidence_bundles
                   WHERE symbol=? AND evidence_fingerprint=?""",
                (symbol.upper(), fingerprint),
            ).fetchone()
        assert row is not None
        value = dict(row)
        value["bundle"] = _loads(value.pop("bundle_json", "{}"), {})
        return value

    def get_latest_evidence_bundle(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM monitor_evidence_bundles
                   WHERE symbol=? ORDER BY created_at DESC LIMIT 1""",
                (symbol.upper(),),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["bundle"] = _loads(value.pop("bundle_json", "{}"), {})
        return value

    def save_condition_coverage(
        self,
        *,
        profile_id: str,
        plan_version: int,
        plan: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            for scenario in plan.get("watch_scenarios") or []:
                for condition in scenario.get("source_conditions") or []:
                    connection.execute(
                        """INSERT OR REPLACE INTO monitor_condition_coverage(
                               coverage_id,profile_id,plan_version,scenario_id,condition_id,
                               role,coverage_status,source_text,reason,evidence_refs_json,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            uuid.uuid4().hex, profile_id, int(plan_version),
                            str(scenario.get("scenario_id") or ""),
                            str(condition.get("condition_id") or ""),
                            str(condition.get("role") or "required"),
                            str(condition.get("coverage_status") or "unsupported"),
                            str(condition.get("source_text") or ""),
                            str(condition.get("reason") or ""),
                            _json(condition.get("evidence_refs") or []), now,
                        ),
                    )

    def activate_autonomous(
        self,
        profile_id: str,
        version: int,
        *,
        trigger_type: str,
        evidence_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Activate a v5 plan after hard validation, preserving unchanged scenarios."""

        now = utc_now()
        with self.transaction() as connection:
            profile = connection.execute(
                "SELECT * FROM monitor_profiles WHERE profile_id=?", (profile_id,)
            ).fetchone()
            plan_row = connection.execute(
                "SELECT * FROM monitor_plan_versions WHERE profile_id=? AND version=?",
                (profile_id, version),
            ).fetchone()
            if not profile or not plan_row:
                raise KeyError(profile_id)
            config = connection.execute(
                """SELECT enabled,selected_symbols_json FROM monitor_autopilot_config
                   WHERE config_id='default'"""
            ).fetchone()
            selected_symbols = set(
                _loads(config["selected_symbols_json"], []) if config is not None else []
            )
            if (
                config is None
                or not bool(config["enabled"])
                or str(profile["symbol"]).upper() not in selected_symbols
            ):
                raise PlanValidationError(
                    "symbol is no longer selected for autonomous monitoring"
                )
            plan = validate_plan_for_activation(
                _loads(plan_row["plan_json"], {}), expected_symbol=str(profile["symbol"])
            )
            if int(plan.get("schema_version") or 0) < 5:
                raise PlanValidationError("autonomous activation requires schema_version=5")
            if not any(rule.get("enabled", True) for rule in plan.get("market_rules") or []):
                raise PlanValidationError("at least one enabled market rule is required")
            old_version = profile["active_plan_version"]
            old_scenarios: dict[str, str] = {}
            if old_version is not None:
                old_row = connection.execute(
                    "SELECT plan_json FROM monitor_plan_versions WHERE profile_id=? AND version=?",
                    (profile_id, old_version),
                ).fetchone()
                old_plan = _loads(old_row["plan_json"], {}) if old_row else {}
                old_scenarios = {
                    str(item.get("scenario_fingerprint")): str(item.get("client_rule_id"))
                    for item in old_plan.get("watch_scenarios") or []
                    if item.get("scenario_fingerprint")
                }
            new_scenarios = {
                str(item.get("scenario_fingerprint")): str(item.get("client_rule_id"))
                for item in plan.get("watch_scenarios") or []
                if item.get("scenario_fingerprint")
            }
            if old_version is not None:
                for fingerprint in set(old_scenarios).intersection(new_scenarios):
                    old_rule = connection.execute(
                        """SELECT * FROM monitor_rules WHERE profile_id=? AND plan_version=?
                           AND client_rule_id=?""",
                        (profile_id, old_version, old_scenarios[fingerprint]),
                    ).fetchone()
                    new_rule = connection.execute(
                        """SELECT * FROM monitor_rules WHERE profile_id=? AND plan_version=?
                           AND client_rule_id=?""",
                        (profile_id, version, new_scenarios[fingerprint]),
                    ).fetchone()
                    if not old_rule or not new_rule:
                        continue
                    connection.execute(
                        """UPDATE monitor_rules SET state=?,confirmation_progress=?,armed_epoch=?,
                               last_condition_value=?,last_observation_id=?,last_bar_time=?,
                               last_triggered_at=?,cooldown_until=?,updated_at=? WHERE rule_id=?""",
                        (
                            old_rule["state"], old_rule["confirmation_progress"], old_rule["armed_epoch"],
                            old_rule["last_condition_value"], old_rule["last_observation_id"],
                            old_rule["last_bar_time"], old_rule["last_triggered_at"],
                            old_rule["cooldown_until"], now, new_rule["rule_id"],
                        ),
                    )
                    connection.execute(
                        """UPDATE monitor_watch_episodes SET plan_version=?,rule_id=?,client_rule_id=?,updated_at=?
                           WHERE profile_id=? AND plan_version=? AND client_rule_id=?
                             AND state IN ('outside','approaching','testing')""",
                        (
                            version, new_rule["rule_id"], new_scenarios[fingerprint], now,
                            profile_id, old_version, old_scenarios[fingerprint],
                        ),
                    )
            connection.execute(
                """UPDATE monitor_plan_versions SET status='superseded',superseded_at=?
                   WHERE profile_id=? AND version<>? AND status IN ('active','draft','pending_review')""",
                (now, profile_id, version),
            )
            connection.execute(
                """UPDATE monitor_plan_versions SET status='active',activated_at=?,superseded_at=NULL,
                       created_by='autopilot' WHERE profile_id=? AND version=?""",
                (now, profile_id, version),
            )
            connection.execute(
                """UPDATE monitor_profiles SET status='active',active_plan_version=?,
                       profile_revision=profile_revision+1,blocked_reasons_json='[]',
                       paused_at=NULL,resume_at=NULL,pause_reason=NULL,closed_at=NULL,
                       next_quote_run_at=?,updated_at=? WHERE profile_id=?""",
                (version, now, now, profile_id),
            )
        value = self.get_profile(profile_id)
        assert value is not None
        value["autonomous_activation"] = {
            "activated_by": "autopilot",
            "trigger_type": trigger_type,
            "evidence_fingerprint": evidence_fingerprint,
        }
        return value

    def save_recommendation(self, recommendation: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        recommendation_id = str(recommendation.get("recommendation_id") or uuid.uuid4().hex)
        payload = {**recommendation, "recommendation_id": recommendation_id}
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO monitor_recommendations(
                       recommendation_id,profile_id,plan_version,episode_id,symbol,scenario_id,
                       scenario_fingerprint,status,action,recommendation_json,recommendation_sha256,
                       valid_until,feedback_status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(episode_id,scenario_fingerprint) DO UPDATE SET
                       status=excluded.status,action=excluded.action,
                       recommendation_json=excluded.recommendation_json,
                       recommendation_sha256=excluded.recommendation_sha256,
                       valid_until=excluded.valid_until,updated_at=excluded.updated_at""",
                (
                    recommendation_id, payload.get("profile_id"), payload.get("plan_version"),
                    payload.get("episode_id"), payload.get("symbol"), payload.get("scenario_id"),
                    payload.get("scenario_fingerprint"), payload.get("status"), payload.get("action"),
                    _json(payload), _hash(payload), payload.get("valid_until"),
                    payload.get("feedback_status") or "pending", payload.get("created_at") or now, now,
                ),
            )
            row = connection.execute(
                """SELECT * FROM monitor_recommendations
                   WHERE episode_id IS ? AND scenario_fingerprint IS ?""",
                (payload.get("episode_id"), payload.get("scenario_fingerprint")),
            ).fetchone()
        assert row is not None
        return self._recommendation(row)

    @staticmethod
    def _recommendation(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        payload = _loads(value.pop("recommendation_json", "{}"), {})
        return {**value, **payload}

    def list_recommendations(
        self,
        *,
        symbol: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol.upper())
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM monitor_recommendations {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._recommendation(row) for row in rows]

    def acknowledge_recommendation(self, recommendation_id: str, feedback_status: str) -> dict[str, Any]:
        if feedback_status not in {"handled", "continue_observing", "ignored"}:
            raise ValueError("unsupported recommendation feedback")
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE monitor_recommendations SET feedback_status=?,acknowledged_at=?,updated_at=?
                   WHERE recommendation_id=?""",
                (feedback_status, now, now, recommendation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(recommendation_id)
            row = connection.execute(
                "SELECT * FROM monitor_recommendations WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()
        assert row is not None
        return self._recommendation(row)

    def get_risk_preference(self, symbol: str) -> dict[str, Any] | None:
        normalized = str(symbol or "").upper()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_risk_preferences WHERE symbol=?",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        preference = _loads(value.pop("preference_json", "{}"), {})
        return {**value, **preference}

    def save_risk_preference(self, symbol: str, preference: dict[str, Any]) -> dict[str, Any]:
        normalized = str(symbol or "").upper()
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT revision,created_at FROM monitor_risk_preferences WHERE symbol=?",
                (normalized,),
            ).fetchone()
            revision = int(existing["revision"] or 0) + 1 if existing else 1
            payload = {
                **preference,
                "symbol": normalized,
                "revision": revision,
                "updated_at": now,
            }
            connection.execute(
                """INSERT INTO monitor_risk_preferences(
                       symbol,preference_json,preference_sha256,revision,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                       preference_json=excluded.preference_json,
                       preference_sha256=excluded.preference_sha256,
                       revision=excluded.revision,
                       updated_at=excluded.updated_at""",
                (
                    normalized,
                    _json(payload),
                    _hash(payload),
                    revision,
                    str(existing["created_at"]) if existing else now,
                    now,
                ),
            )
            connection.execute(
                """UPDATE monitor_condition_order_drafts
                   SET status='stale',updated_at=?
                   WHERE symbol=? AND status IN ('draft','validated','needs_risk_preferences')""",
                (now, normalized),
            )
        value = self.get_risk_preference(normalized)
        assert value is not None
        return value

    def record_decision_choice(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        record = {
            **payload,
            "choice_record_id": str(payload.get("choice_record_id") or uuid.uuid4().hex),
            "created_at": str(payload.get("created_at") or now),
        }
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT choice_json FROM monitor_decision_choices WHERE idempotency_key=?",
                (record["idempotency_key"],),
            ).fetchone()
            if existing is not None:
                return _loads(existing["choice_json"], {})
            connection.execute(
                """INSERT INTO monitor_decision_choices(
                       choice_record_id,decision_id,symbol,choice_id,decision_revision,
                       evidence_fingerprint,idempotency_key,status,choice_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["choice_record_id"], record["decision_id"], record["symbol"],
                    record["choice_id"], int(record["decision_revision"]),
                    record["evidence_fingerprint"], record["idempotency_key"],
                    record.get("status") or "recorded", _json(record), record["created_at"],
                ),
            )
        return record

    def save_condition_order_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        payload = {
            **draft,
            "draft_id": str(draft.get("draft_id") or uuid.uuid4().hex),
            "created_at": str(draft.get("created_at") or now),
            "updated_at": now,
        }
        with self.transaction() as connection:
            connection.execute(
                """UPDATE monitor_condition_order_drafts
                   SET status='stale',updated_at=?
                   WHERE symbol=? AND side=? AND status IN ('draft','validated','needs_risk_preferences')""",
                (now, payload["symbol"], payload["side"]),
            )
            connection.execute(
                """INSERT INTO monitor_condition_order_drafts(
                       draft_id,decision_id,symbol,side,status,draft_json,draft_sha256,
                       evidence_fingerprint,valid_until,created_at,updated_at,cancelled_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    payload["draft_id"], payload["decision_id"], payload["symbol"],
                    payload["side"], payload["status"], _json(payload), _hash(payload),
                    payload["evidence_fingerprint"], payload["valid_until"],
                    payload["created_at"], now, payload.get("cancelled_at"),
                ),
            )
        return payload

    def get_condition_order_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_condition_order_drafts WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        payload = _loads(value.pop("draft_json", "{}"), {})
        return {**payload, **value}

    def latest_condition_order_draft(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM monitor_condition_order_drafts
                   WHERE symbol=? ORDER BY created_at DESC LIMIT 1""",
                (str(symbol or "").upper(),),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        payload = _loads(value.pop("draft_json", "{}"), {})
        return {**payload, **value}

    def update_condition_order_draft_status(
        self,
        draft_id: str,
        status: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT draft_json FROM monitor_condition_order_drafts WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise KeyError(draft_id)
            payload = _loads(row["draft_json"], {})
            payload.update(
                status=status,
                updated_at=now,
                cancelled_at=now if status == "cancelled" else payload.get("cancelled_at"),
            )
            connection.execute(
                """UPDATE monitor_condition_order_drafts
                   SET status=?,draft_json=?,draft_sha256=?,updated_at=?,cancelled_at=?
                   WHERE draft_id=?""",
                (
                    status, _json(payload), _hash(payload), now,
                    payload.get("cancelled_at"), draft_id,
                ),
            )
        return payload

    def metrics(self) -> dict[str, Any]:
        price_volume_window_hours = 24
        price_volume_cutoff = (
            datetime.fromisoformat(utc_now().replace("Z", "+00:00"))
            - timedelta(hours=price_volume_window_hours)
        ).isoformat()
        with self.connect() as connection:
            counts = {
                name: int(connection.execute(query).fetchone()[0])
                for name, query in {
                    "profiles": "SELECT COUNT(*) FROM monitor_profiles",
                    "active_profiles": "SELECT COUNT(*) FROM monitor_profiles WHERE status='active'",
                    "events": "SELECT COUNT(*) FROM monitor_events",
                    "pending_deliveries": "SELECT COUNT(*) FROM delivery_outbox WHERE status='pending'",
                    "uncertain_deliveries": "SELECT COUNT(*) FROM delivery_outbox WHERE status='delivery_uncertain'",
                    "shadow_suppressed_deliveries": (
                        "SELECT COUNT(*) FROM delivery_outbox WHERE status='shadow_suppressed'"
                    ),
                    "blocked_profiles": (
                        "SELECT COUNT(*) FROM monitor_profiles WHERE blocked_reasons_json<>'[]'"
                    ),
                }.items()
            }
            delivery_rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM delivery_outbox GROUP BY status ORDER BY status"
            ).fetchall()
            observation_rows = connection.execute(
                """SELECT status,COUNT(*) AS count FROM monitor_observations
                   GROUP BY status ORDER BY status"""
            ).fetchall()
            price_volume_rows = connection.execute(
                """SELECT observation_id,profile_id,observed_at,payload_json
                   FROM monitor_observations
                   WHERE domain='quote' AND observed_at>=?
                   ORDER BY observed_at DESC,observation_id DESC""",
                (price_volume_cutoff,),
            ).fetchall()
            profile_rows = connection.execute(
                """SELECT symbol,status,last_quote_check_at,last_success_at,next_quote_run_at,
                   blocked_reasons_json,input_outdated FROM monitor_profiles
                   WHERE status IN ('active','paused') ORDER BY symbol"""
            ).fetchall()
        counts["database_path"] = str(self.path)
        database_size = self.database_size_bytes()
        maximum = _env_int(
            "VIBE_TRADING_MONITOR_MAX_DB_BYTES",
            512 * 1024 * 1024,
            minimum=1,
        )
        counts["database_size_bytes"] = database_size
        counts["database_max_bytes"] = maximum
        counts["database_utilization"] = round(database_size / maximum, 6)
        counts["delivery_status_counts"] = {
            str(row["status"]): int(row["count"]) for row in delivery_rows
        }
        counts["observation_status_counts"] = {
            str(row["status"]): int(row["count"]) for row in observation_rows
        }
        conflict_reasons = {
            "source_signature_mismatch",
            "source_signature_missing",
            "volume_conflict",
            "volume_unit_conflict",
            "volume_unit_unknown",
        }
        evidence_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for row in price_volume_rows:
            payload = _loads(row["payload_json"], {})
            price_volume = payload.get("price_volume")
            if not isinstance(price_volume, dict):
                continue
            evidence_bar_time = str(
                payload.get("price_volume_bar_time")
                or payload.get("bar_time")
                or row["observation_id"]
            )
            evidence_by_key.setdefault(
                (str(row["profile_id"]), evidence_bar_time),
                price_volume,
            )
        status_counts: dict[str, int] = {}
        reason_counts: dict[str, int] = {}
        conflict_count = 0
        for price_volume in evidence_by_key.values():
            status = str(price_volume.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            reasons = {
                str(reason)
                for reason in (price_volume.get("reason_codes") or [])
                if str(reason)
            }
            for reason in sorted(reasons):
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if reasons.intersection(conflict_reasons):
                conflict_count += 1
        observation_count = len(evidence_by_key)
        evidence_count = (
            status_counts.get("ready", 0)
            + status_counts.get("insufficient_data", 0)
        )
        insufficient_count = status_counts.get("insufficient_data", 0)
        counts["price_volume_quality"] = {
            "window_hours": price_volume_window_hours,
            "observation_count": observation_count,
            "evidence_count": evidence_count,
            "disabled_count": status_counts.get("disabled", 0),
            "status_counts": status_counts,
            "reason_counts": reason_counts,
            "insufficient_rate": round(insufficient_count / evidence_count, 6)
            if evidence_count
            else 0.0,
            "conflict_rate": round(conflict_count / evidence_count, 6)
            if evidence_count
            else 0.0,
        }
        counts["profile_health"] = [
            {
                "symbol": row["symbol"],
                "status": row["status"],
                "last_quote_check_at": row["last_quote_check_at"],
                "last_success_at": row["last_success_at"],
                "next_quote_run_at": row["next_quote_run_at"],
                "blocked_reasons": _loads(row["blocked_reasons_json"], []),
                "input_outdated": bool(row["input_outdated"]),
            }
            for row in profile_rows
        ]
        counts["runtime_health"] = self.runtime_health()
        counts["maintenance"] = self.maintenance_status()
        return counts
