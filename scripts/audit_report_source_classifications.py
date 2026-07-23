from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from src.research.knowledge import get_research_knowledge_store  # noqa: E402
from src.research.source_classification import (  # noqa: E402
    audit_source_classifications,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit report-library source classifications."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Correct misclassified source observations in the configured store.",
    )
    args = parser.parse_args()
    result = audit_source_classifications(
        get_research_knowledge_store(),
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.apply or result["misclassified"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
