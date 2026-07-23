"""Persistence for immutable single-symbol weekly report runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config.paths import get_runtime_root
from src.portfolio.daily.store import DailyRunStore


class WeeklyRunStore(DailyRunStore):
    """Reuse the proven atomic run/artifact store under an isolated root."""

    def __init__(self, base_dir: Path | None = None) -> None:
        super().__init__(base_dir or get_runtime_root() / "portfolio" / "weekly_runs")

    def find_reusable(self, run_key: str) -> dict[str, Any] | None:
        for record in self.list(limit=10_000):
            if (
                record.get("run_key") == run_key
                and record.get("status") not in {"failed", "cancelled", "interrupted"}
            ):
                return record
        return None

    def next_revision_for_key(self, run_key: str) -> int:
        revisions = [
            int(record.get("revision") or 1)
            for record in self.list(limit=10_000)
            if record.get("run_key") == run_key
        ]
        return max(revisions, default=0) + 1

    def previous_success(
        self,
        *,
        symbol: str,
        before_week_end: str,
        excluding_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        candidates = [
            record
            for record in self.list(limit=10_000)
            if str(record.get("symbol") or "").upper() == symbol.upper()
            and str(record.get("week_end") or "") < before_week_end
            and str(record.get("run_id") or "") != str(excluding_run_id or "")
            and record.get("status") in {"completed", "completed_with_warnings"}
            and record.get("quality_status") in {"passed", "passed_with_gaps"}
        ]
        candidates.sort(
            key=lambda item: (
                str(item.get("week_end") or ""),
                int(item.get("revision") or 1),
                str(item.get("completed_at") or ""),
            ),
            reverse=True,
        )
        return candidates[0] if candidates else None
