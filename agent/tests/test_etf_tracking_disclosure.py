from __future__ import annotations

from src.reports.etf_tracking_disclosure import (
    extract_official_tracking_disclosure,
    register_official_tracking_disclosure,
)
from src.research.knowledge import ResearchKnowledgeStore


DISCLOSURE_TEXT = """
本基金力争日均跟踪偏离度的绝对值不超过 0.20%，年跟踪误差不超过 2%。
3.2 基金净值表现
阶段 份额净值增长率① 份额净值增长率标准差② 业绩比较基准收益率③
业绩比较基准收益率标准差④ ①－③ ②－④
过去三
个月 -10.08% 1.84% -10.10% 1.85% 0.02% -0.01%
过去六
个月 33.18% 2.02% 33.96% 2.03% -0.78% -0.01%
"""


def test_official_tracking_disclosure_parser_handles_pdf_line_breaks() -> None:
    result = extract_official_tracking_disclosure(DISCLOSURE_TEXT)

    assert result["status"] == "complete"
    assert [item["period_key"] for item in result["comparison_rows"]] == ["3m", "6m"]
    assert result["comparison_rows"][1]["tracking_difference"] == -0.0078
    assert result["objective_limits"] == {
        "daily_tracking_deviation_absolute_limit": 0.002,
        "annual_tracking_error_limit": 0.02,
    }


def test_official_tracking_disclosure_registration_is_idempotent(tmp_path) -> None:
    store = ResearchKnowledgeStore(
        path=tmp_path / "knowledge.sqlite3",
        object_dir=tmp_path / "objects",
    )
    kwargs = {
        "store": store,
        "symbol": "588870.SH",
        "source_url": "https://www.sse.com.cn/588870-2025.pdf",
        "source_text": DISCLOSURE_TEXT,
        "title": "588870 2025年年度报告",
        "publisher": "上海证券交易所",
        "published_at": "2026-03-31",
        "report_period": "2025-12-31",
    }

    first = register_official_tracking_disclosure(**kwargs)
    second = register_official_tracking_disclosure(**kwargs)

    assert second["document_ref"] == first["document_ref"]
    assert second["fact_ids"] == first["fact_ids"]
    with store.connect() as connection:
        tracking = connection.execute(
            "SELECT metric,value,scope_key FROM fact_records "
            "WHERE symbol='588870.SH' AND metric='tracking_difference' "
            "ORDER BY scope_key"
        ).fetchall()
        assert [(row["scope_key"], float(row["value"])) for row in tracking] == [
            ("3m", 0.0002),
            ("6m", -0.0078),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_records WHERE evidence_id=?",
            (first["evidence_id"],),
        ).fetchone()[0] == 1
