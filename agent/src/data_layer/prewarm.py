"""Market-calendar-aware background prewarm for holdings and manual watchlists."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, time, timedelta
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.config.paths import get_runtime_root


_CN_TZ = ZoneInfo("Asia/Shanghai")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_SLOTS: tuple[tuple[str, time], ...] = (
    ("premarket", time(9, 10)),
    ("intraday", time(9, 35)),
    # Have a verified cache ready before the lunch-break analysis, not after it.
    ("intraday", time(11, 25)),
    ("intraday", time(13, 5)),
    ("intraday", time(15, 10)),
)


class ChinaMarketCalendar:
    """Use the exchange calendar, retaining the last verified copy on disk."""

    def __init__(
        self,
        calendar_fetcher: Callable[[], Any] | None = None,
        *,
        cache_path: Path | None = None,
    ) -> None:
        self.calendar_fetcher = calendar_fetcher
        self.cache_path = cache_path or (
            get_runtime_root() / "data" / "china_exchange_calendar.json"
        )
        self._days: set[str] = set()
        self._loaded_at: datetime | None = None
        self._source_mode = "uninitialized"
        self.mode = "uninitialized"
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            days = {
                str(item)
                for item in payload.get("days", [])
                if len(str(item)) == 10
            }
            if not days:
                return
            loaded_at = datetime.fromisoformat(str(payload.get("updated_at") or ""))
            if loaded_at.tzinfo is None:
                loaded_at = loaded_at.replace(tzinfo=_CN_TZ)
            self._days = days
            self._loaded_at = loaded_at.astimezone(_CN_TZ)
            self._source_mode = self.mode = "cached_exchange_calendar"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _save_cache(self, now: datetime) -> None:
        payload = {
            "schema_version": 1,
            "updated_at": now.isoformat(),
            "days": sorted(self._days),
        }
        temporary = self.cache_path.with_name(
            f"{self.cache_path.name}.{os.getpid()}.{id(self)}.tmp"
        )
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.cache_path)
        except OSError:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _normalize_day(value: Any) -> str | None:
        text = str(value).strip()[:10]
        if len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None

    @classmethod
    def _extract_days(cls, payload: Any) -> set[str]:
        if isinstance(payload, dict):
            values = payload.get("days") or payload.get("trade_date") or []
        elif isinstance(payload, (list, tuple, set)):
            values = payload
        else:
            column = (
                "trade_date"
                if "trade_date" in payload.columns
                else payload.columns[0]
            )
            values = payload[column].tolist()
        return {
            normalized
            for item in values
            if (normalized := cls._normalize_day(item)) is not None
        }

    @classmethod
    def _default_calendar_payload(cls) -> Any:
        # AkShare ships a decoded exchange calendar. Reading that data file does
        # not import AkShare's optional JavaScript runtime, which can be absent
        # even though the verified calendar itself is installed and current.
        try:
            spec = find_spec("akshare")
        except (ImportError, ValueError):
            spec = None
        if spec is not None and spec.origin:
            bundled_path = Path(spec.origin).parent / "file_fold" / "calendar.json"
            try:
                bundled = json.loads(bundled_path.read_text(encoding="utf-8"))
                days = cls._extract_days(bundled)
                if days and max(days) >= datetime.now(_CN_TZ).date().isoformat():
                    return bundled
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

        import akshare as ak

        return ak.tool_trade_date_hist_sina()

    def is_trading_day(self, value: date) -> bool:
        if value.weekday() >= 5:
            self.mode = "weekend"
            return False
        self.mode = self._source_mode
        now = datetime.now(_CN_TZ)
        if self._loaded_at is None or now - self._loaded_at > timedelta(hours=24):
            try:
                fetcher = self.calendar_fetcher
                if fetcher is None:
                    fetcher = self._default_calendar_payload
                payload = fetcher()
                days = self._extract_days(payload)
                if not days:
                    raise ValueError("exchange calendar returned no trading days")
                self._days = days
                self._loaded_at = now
                self._source_mode = self.mode = "exchange_calendar"
                self._save_cache(now)
            except Exception:  # Keep the last verified calendar, otherwise fail closed.
                self._loaded_at = now
                self._source_mode = self.mode = (
                    "cached_exchange_calendar" if self._days else "calendar_unavailable"
                )
        return value.isoformat() in self._days if self._days else False


class DataPrewarmScheduler:
    """Small cooperative scheduler with deduplicated China-market prewarm slots."""

    def __init__(
        self,
        service_factory: Callable[[], Any],
        *,
        calendar: ChinaMarketCalendar | None = None,
        now_factory: Callable[[], datetime] | None = None,
        interval_seconds: float = 30.0,
    ) -> None:
        self.service_factory = service_factory
        self.calendar = calendar or ChinaMarketCalendar()
        self.now_factory = now_factory or (lambda: datetime.now(_CN_TZ))
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._completed_slots: set[str] = set()
        self._active_slots: set[str] = set()
        self.last_run: dict[str, Any] | None = None

    @staticmethod
    def enabled() -> bool:
        value = os.getenv("VIBE_TRADING_DATA_PREWARM_ENABLED", "1").strip().lower()
        return value in _TRUE_VALUES

    async def start(self) -> None:
        if not self.enabled() or (self._task is not None and not self._task.done()):
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="unified-data-prewarm")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            finally:
                self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.run_due_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def run_due_once(self, now: datetime | None = None) -> list[dict[str, Any]]:
        local_now = (now or self.now_factory()).astimezone(_CN_TZ)
        due = [(phase, clock) for phase, clock in _SLOTS if local_now.hour == clock.hour and local_now.minute == clock.minute]
        if not due or not self.calendar.is_trading_day(local_now.date()):
            return []
        completed: list[dict[str, Any]] = []
        for phase, clock in due:
            key = f"{local_now.date().isoformat()}:{phase}:{clock.isoformat()}"
            if key in self._completed_slots:
                continue
            self._completed_slots.add(key)
            self._active_slots.add(key)
            self.last_run = {
                "slot": key,
                "phase": phase,
                "status": "running",
                "at": local_now.isoformat(),
            }
            try:
                result = await asyncio.to_thread(self.service_factory().prewarm, phase=phase)
                record = {"slot": key, "phase": phase, "status": result.get("status", "completed"), "request_id": result.get("request_id"), "at": local_now.isoformat()}
            except Exception as exc:  # preserve the next slot even if one source has an outage
                record = {"slot": key, "phase": phase, "status": "failed", "error": str(exc), "at": local_now.isoformat()}
            finally:
                self._active_slots.discard(key)
            self.last_run = record
            completed.append(record)
        # Bound dedupe memory to the recent trading week.
        cutoff = (local_now.date() - timedelta(days=8)).isoformat()
        self._completed_slots = {key for key in self._completed_slots if key[:10] >= cutoff}
        return completed

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "running": self._task is not None and not self._task.done(),
            "timezone": "Asia/Shanghai",
            "calendar_mode": self.calendar.mode,
            "slots": [{"phase": phase, "time": clock.strftime("%H:%M")} for phase, clock in _SLOTS],
            "active_slots": sorted(self._active_slots),
            "last_run": self.last_run,
        }


_scheduler: DataPrewarmScheduler | None = None


def get_data_prewarm_scheduler() -> DataPrewarmScheduler:
    global _scheduler
    if _scheduler is None:
        from .service import get_unified_data_service

        _scheduler = DataPrewarmScheduler(get_unified_data_service)
    return _scheduler
