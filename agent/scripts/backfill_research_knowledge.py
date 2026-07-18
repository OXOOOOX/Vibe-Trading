"""Idempotently index existing Deep Reports and marked research sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.research import ResearchKnowledgeStore  # noqa: E402


def _backfill_sessions(store: ResearchKnowledgeStore, sessions_dir: Path) -> dict[str, int]:
    counts = {"sessions": 0, "messages": 0}
    if not sessions_dir.exists():
        return counts
    for session_dir in sessions_dir.iterdir():
        session_path = session_dir / "session.json"
        messages_path = session_dir / "messages.jsonl"
        if not session_path.exists() or not messages_path.exists():
            continue
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        research = dict((session.get("config") or {}).get("research_session") or {})
        messages: list[dict] = []
        for line in messages_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                messages.append(item)
        linked = any(
            (item.get("metadata") or {}).get("report_id")
            or (item.get("metadata") or {}).get("linked_report_id")
            for item in messages
        )
        if not research and not linked:
            continue
        symbol = str(research.get("symbol") or research.get("resolved_symbol") or "").upper()
        for item in messages:
            if item.get("role") not in {"user", "assistant"} or not str(item.get("content") or "").strip():
                continue
            metadata = dict(item.get("metadata") or {})
            message_symbol = str(metadata.get("report_symbol") or symbol).upper()
            store.index_research_session(
                session_id=str(session.get("session_id") or session_dir.name),
                symbol=message_symbol,
                role=str(item["role"]),
                content=str(item["content"]),
                message_id=str(item.get("message_id") or ""),
            )
            counts["messages"] += 1
        counts["sessions"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--objects", type=Path, default=None)
    parser.add_argument("--reports", type=Path, default=ROOT / "reports")
    parser.add_argument("--sessions", type=Path, default=ROOT / "sessions")
    args = parser.parse_args()
    store = ResearchKnowledgeStore(path=args.db, object_dir=args.objects)
    result = {
        "reports": store.backfill_reports(args.reports),
        "research_sessions": _backfill_sessions(store, args.sessions),
        "database": str(store.path),
        "objects": str(store.object_dir),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
