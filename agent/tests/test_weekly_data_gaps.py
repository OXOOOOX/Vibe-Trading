from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.portfolio.weekly.context import WeeklyContextAssembler
from src.portfolio.weekly.etf_metrics import (
    build_etf_tracking_metrics,
    enrich_weekly_context_with_etf_metrics,
)
from src.portfolio.weekly.reporting import _context_lines
from src.reports.data_gaps import (
    gap_codes,
    make_gap_detail,
    normalize_gap_details,
    quality_affecting_gaps,
)
from src.reports.etf_tracking_disclosure import register_official_tracking_disclosure
from src.research.knowledge import ResearchKnowledgeStore


class EmptyLibrary:
    @staticmethod
    def subject(*_args, **_kwargs):
        return {"security_name": "格力电器", "timeline": []}


class KnowledgeLibrary(EmptyLibrary):
    def __init__(self, knowledge: ResearchKnowledgeStore) -> None:
        self.knowledge = knowledge


def _bars(symbol: str, *, daily_gain: float, count: int = 80) -> list[dict]:
    rows: list[dict] = []
    cursor = date(2026, 3, 2)
    close = 1.0
    while len(rows) < count:
        if cursor.weekday() < 5:
            close *= 1.0 + daily_gain
            rows.append({
                "symbol": symbol,
                "date": cursor.isoformat(),
                "close": close,
                "status": "verified",
                "sources": ["source-a", "source-b"],
            })
        cursor += timedelta(days=1)
    return rows


def test_equity_context_does_not_create_etf_scopes_or_gap_codes() -> None:
    context = WeeklyContextAssembler(EmptyLibrary()).assemble(
        "000651.SZ",
        week_end="2026-07-17",
        instrument_type="company_equity",
    )

    assert context["instrument_type"] == "company_equity"
    assert context["scopes"] == {}
    assert context["data_gaps"] == ["reusable_report_claims_unavailable"]
    assert not [code for code in context["data_gaps"] if code.startswith("etf_")]


def test_symbol_level_official_facts_respect_publication_cutoff(tmp_path) -> None:
    store = ResearchKnowledgeStore(
        path=tmp_path / "knowledge.sqlite3",
        object_dir=tmp_path / "objects",
    )
    register_official_tracking_disclosure(
        store=store,
        symbol="588870.SH",
        source_url="https://www.sse.com.cn/588870-2025.pdf",
        source_text=(
            "本基金力争日均跟踪偏离度的绝对值不超过 0.20%，年跟踪误差不超过 2%。\n"
            "过去三个月 -10.08% 1.84% -10.10% 1.85% 0.02% -0.01%\n"
            "过去六个月 33.18% 2.02% 33.96% 2.03% -0.78% -0.01%"
        ),
        title="588870 2025年年度报告",
        publisher="上海证券交易所",
        published_at="2026-03-31",
        report_period="2025-12-31",
    )
    assembler = WeeklyContextAssembler(KnowledgeLibrary(store))

    before_publication = assembler.assemble(
        "588870.SH",
        week_end="2026-03-27",
        instrument_type="etf",
    )
    after_publication = assembler.assemble(
        "588870.SH",
        week_end="2026-04-03",
        instrument_type="etf",
    )

    assert before_publication["scopes"]["official_tracking_quality"]["availability"] == "missing"
    official = after_publication["scopes"]["official_tracking_quality"]
    assert official["availability"] == "complete"
    assert {item["scope_key"] for item in official["facts"]} == {
        "3m", "6m", "contract_objective"
    }
    assert "etf_tracking_error_scope_unavailable" not in after_publication["data_gaps"]
    rendered = "\n".join(_context_lines(after_publication))
    assert "近三个月基金净值收益减业绩比较基准收益：+0.02%" in rendered
    assert "年跟踪误差不超过 2.00%" in rendered


def test_iopv_does_not_make_official_tracking_quality_complete(tmp_path) -> None:
    store = ResearchKnowledgeStore(
        path=tmp_path / "knowledge.sqlite3",
        object_dir=tmp_path / "objects",
    )
    store.register_bundle({
        "facts": [{
            "fact_id": "fact-iopv-only",
            "symbol": "588870.SH",
            "metric": "iopv",
            "value": "1.75",
            "unit": "CNY",
            "period": "2026-07-17",
            "validation_status": "pass",
        }]
    })

    context = WeeklyContextAssembler(KnowledgeLibrary(store)).assemble(
        "588870.SH",
        instrument_type="etf",
    )

    assert context["scopes"]["nav_reference"]["availability"] == "complete"
    assert context["scopes"]["official_tracking_quality"]["availability"] == "missing"
    assert "etf_tracking_error_scope_unavailable" in context["data_gaps"]


def test_gap_registry_filters_asset_applicability_and_deduplicates_sources() -> None:
    assert make_gap_detail(
        "etf_tracking_error_scope_unavailable",
        source="context",
        instrument_type="company_equity",
    ) is None
    first = make_gap_detail(
        "etf_component_research_scope_partial",
        source="context",
        instrument_type="etf",
        availability="partial",
        missing_items=["688008.SH"],
    )
    second = make_gap_detail(
        "etf_component_research_scope_partial",
        source="agent",
        instrument_type="etf",
        availability="partial",
        missing_items=["688012.SH"],
    )

    normalized = normalize_gap_details([first, second], instrument_type="etf")

    assert gap_codes(normalized) == ["etf_component_research_scope_partial"]
    assert normalized[0]["sources"] == ["context", "agent"]
    assert normalized[0]["missing_items"] == ["688008.SH", "688012.SH"]
    assert quality_affecting_gaps(normalized) == normalized


def test_etf_tracking_metrics_are_reproducible_and_use_common_completed_days() -> None:
    etf_bars = _bars("588870.SH", daily_gain=0.002)
    index_bars = _bars("000688.SH", daily_gain=0.001)
    end = etf_bars[-1]["date"]
    week_start = etf_bars[-5]["date"]

    snapshot = build_etf_tracking_metrics(
        etf_symbol="588870.SH",
        tracked_index_code="000688.SH",
        etf_bars=etf_bars,
        index_bars=index_bars,
        week_start=week_start,
        week_end=end,
    )

    relative = snapshot["index_relative_strength"]
    tracking = snapshot["market_tracking_deviation"]
    assert relative["availability"] == "complete"
    assert relative["metrics"]["fund_index_return_gap_1w"] > 0
    assert relative["metrics"]["index_relative_strength_1w"] == "outperformed"
    assert tracking["availability"] == "complete"
    assert tracking["available_windows"] == [20, 60]
    assert tracking["calculation_basis"]["official_metric"] is False

    repeated = build_etf_tracking_metrics(
        etf_symbol="588870.SH",
        tracked_index_code="000688.SH",
        etf_bars=etf_bars,
        index_bars=index_bars,
        week_start=week_start,
        week_end=end,
    )
    assert repeated == snapshot


def test_etf_tracking_enrichment_keeps_official_and_market_metrics_separate() -> None:
    etf_bars = _bars("588870.SH", daily_gain=0.002)
    index_bars = _bars("000688.SH", daily_gain=0.001)
    snapshot = build_etf_tracking_metrics(
        etf_symbol="588870.SH",
        tracked_index_code="000688.SH",
        etf_bars=etf_bars,
        index_bars=index_bars,
        week_start=etf_bars[-5]["date"],
        week_end=etf_bars[-1]["date"],
    )
    gaps = normalize_gap_details(
        [
            make_gap_detail(
                "etf_index_relative_strength_scope_unavailable",
                source="catalog",
                instrument_type="etf",
            ),
            make_gap_detail(
                "etf_market_tracking_deviation_scope_unavailable",
                source="catalog",
                instrument_type="etf",
            ),
            make_gap_detail(
                "etf_tracking_error_scope_unavailable",
                source="catalog",
                instrument_type="etf",
            ),
        ],
        instrument_type="etf",
    )
    context = {
        "schema_version": 2,
        "symbol": "588870.SH",
        "instrument_type": "etf",
        "context_fingerprint": "catalog-fingerprint",
        "scopes": {
            "official_tracking_quality": {
                "availability": "missing",
                "facts": [],
                "data_as_of": None,
            }
        },
        "data_gap_details": gaps,
        "data_gaps": gap_codes(gaps),
        "source_manifest": {},
    }

    enriched = enrich_weekly_context_with_etf_metrics(context, snapshot)

    assert enriched["scopes"]["index_relative_strength"]["availability"] == "complete"
    assert enriched["scopes"]["market_tracking_deviation"]["availability"] == "complete"
    assert enriched["scopes"]["official_tracking_quality"]["availability"] == "missing"
    assert enriched["scopes"]["tracking_error"]["availability"] == "partial"
    assert enriched["data_gaps"] == ["etf_tracking_error_scope_unavailable"]
    assert context["scopes"].get("market_tracking_deviation") is None


def test_tracking_metrics_fail_closed_without_a_common_week_end() -> None:
    etf_bars = _bars("588870.SH", daily_gain=0.002)
    index_bars = _bars("000688.SH", daily_gain=0.001)[:-1]

    snapshot = build_etf_tracking_metrics(
        etf_symbol="588870.SH",
        tracked_index_code="000688.SH",
        etf_bars=etf_bars,
        index_bars=index_bars,
        week_start=etf_bars[-5]["date"],
        week_end=etf_bars[-1]["date"],
    )

    assert snapshot["index_relative_strength"]["availability"] == "missing"
    assert snapshot["market_tracking_deviation"]["availability"] == "complete"
    assert snapshot["market_tracking_deviation"]["data_as_of"] < etf_bars[-1]["date"]


@pytest.mark.parametrize("instrument_type", ["company_equity", "etf"])
def test_unknown_gap_codes_fail_closed(instrument_type: str) -> None:
    with pytest.raises(ValueError, match="unregistered data gap"):
        normalize_gap_details(
            [{"reason_code": "agent_free_form_statement"}],
            instrument_type=instrument_type,
        )
