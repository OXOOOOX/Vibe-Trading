"""Build reusable structured snapshots from archived official filings."""

from __future__ import annotations

import argparse
import json

from src.research.backfill import SourceArchiveBackfill


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write extraction results; otherwise only count candidates",
    )
    args = parser.parse_args()
    service = SourceArchiveBackfill()
    result = service._backfill_structured_financials(dry_run=not args.apply)
    print(json.dumps({"dry_run": not args.apply, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
