"""Dry-run or apply the unified Report source archive backfill."""

from __future__ import annotations

import argparse
import json

from src.research.backfill import SourceArchiveBackfill


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the idempotent backfill")
    parser.add_argument(
        "--refresh-active",
        action="append",
        default=[],
        metavar="SYMBOL",
        help="after local backfill, refresh one active symbol from official sources",
    )
    args = parser.parse_args()
    result = SourceArchiveBackfill().run(
        dry_run=not args.apply,
        refresh_active_symbols=args.refresh_active,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
