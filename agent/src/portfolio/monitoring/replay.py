"""Deterministic quote replay helpers for monitoring soak and fault tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .store import MonitoringStore


def replay_quotes(
    store: MonitoringStore,
    profile_id: str,
    quotes: Iterable[dict[str, Any]],
    *,
    delivery_mode: str = "shadow",
    price_volume_mode: str = "off",
    duplicate_indexes: set[int] | None = None,
    reopen_before_indexes: set[int] | None = None,
) -> dict[str, Any]:
    """Replay quotes, optionally duplicating inputs or reopening SQLite mid-stream.

    Indexes are zero-based. Reopening creates a new store instance against the
    same path and exercises persisted rule, episode, observation, and outbox
    state rather than keeping the original Python object alive.
    """

    if delivery_mode not in {"shadow", "deliver"}:
        raise ValueError("delivery_mode must be shadow or deliver")
    if price_volume_mode not in {"off", "shadow", "deliver"}:
        raise ValueError("price_volume_mode must be off, shadow, or deliver")
    duplicates = duplicate_indexes or set()
    reopens = reopen_before_indexes or set()
    active_store = store
    created_events: list[str] = []
    observations_submitted = 0
    reopen_count = 0

    for index, quote in enumerate(quotes):
        if index in reopens:
            active_store = MonitoringStore(active_store.path)
            reopen_count += 1
        copies = 2 if index in duplicates else 1
        for _ in range(copies):
            observations_submitted += 1
            created_events.extend(
                event["event_id"]
                for event in active_store.evaluate_quote(
                    profile_id,
                    dict(quote),
                    delivery_mode=delivery_mode,
                    price_volume_mode=price_volume_mode,
                )
            )

    metrics = active_store.metrics()
    return {
        "profile_id": profile_id,
        "delivery_mode": delivery_mode,
        "price_volume_mode": price_volume_mode,
        "observations_submitted": observations_submitted,
        "reopen_count": reopen_count,
        "event_ids": created_events,
        "events_created": len(created_events),
        "pending_deliveries": metrics["pending_deliveries"],
        "shadow_suppressed_deliveries": metrics["shadow_suppressed_deliveries"],
        "runtime_counters": metrics["runtime_health"]["counters"],
    }
