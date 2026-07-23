from __future__ import annotations

import json
import re
from io import BytesIO
from pathlib import Path

import pytest

from src.research.backfill import SourceArchiveBackfill
from src.research.knowledge import ResearchKnowledgeStore
from src.research.official_filings import (
    CninfoAnnualReportProvider,
    OfficialFilingProvider,
    OfficialFilingRecord,
    OfficialFilingService,
    _pdf_text,
)
from src.research.source_ingestion import CollectedSource, SourceIngestionService
from src.session.models import Message, Session
from src.session.service import SessionService
from src.tools.official_filings_tool import OfficialFilingsTool


def _store(tmp_path: Path) -> ResearchKnowledgeStore:
    return ResearchKnowledgeStore(
        path=tmp_path / "research.sqlite3",
        object_dir=tmp_path / "objects",
    )


def test_official_filing_pdf_uses_pure_python_text_extraction() -> None:
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 720, "Official filing revenue 2025")
    document.save()

    assert "Official filing revenue 2025" in _pdf_text(buffer.getvalue())


def test_source_archive_deduplicates_and_does_not_authenticate_mirrors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ingestion = SourceIngestionService(store)
    common = dict(
        subject_key="600036.SH",
        source_kind="official_filing",
        provider_id="search",
        publisher="转载媒体",
        title="招商银行年度报告（交易所披露）",
        content="已读取的转载正文，标题声称来自交易所，但链接并不是官方域名。",
        verification_status="official_primary",
        body_status="full_text",
        source_class="regulatory_filing",
    )
    first = ingestion.ingest(
        CollectedSource(source_locator="https://example.com/annual", **common),
        origin_type="data_context",
        origin_id="run-1",
    )
    ingestion.ingest(
        CollectedSource(source_locator="https://example.com/annual", **common),
        origin_type="daily_run",
        origin_id="run-2",
    )

    sources = store.list_subject_sources("600036.SH")["sources"]
    assert len(sources) == 1
    assert sources[0]["document_ref"] == first["document_ref"]
    assert sources[0]["verification_status"] == "source_recorded"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 2


def test_official_observation_survives_content_dedup_against_prior_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ingestion = SourceIngestionService(store)
    content = "上海证券交易所 ETF 日终基金份额正式披露正文。"
    ingestion.ingest(
        CollectedSource(
            subject_key="513120.SH", source_kind="fund_share_scale",
            provider_id="cached-provider", publisher="缓存提供方", title="ETF 份额",
            source_locator="provider://cached/fund_share_scale/513120/20260721",
            content=content, verification_status="source_recorded", body_status="full_text",
        ),
        origin_type="provider_cache", origin_id="cache-1",
    )

    official = ingestion.ingest(
        CollectedSource(
            subject_key="513120.SH", source_kind="fund_share_scale",
            provider_id="上海证券交易所", publisher="上海证券交易所", title="ETF 份额",
            source_locator="https://www.sse.com.cn/market/funddata/volumn/etfvolumn/",
            content=content, verification_status="official_primary", body_status="full_text",
            source_class="exchange",
        ),
        origin_type="etf_product_profile", origin_id="refresh-1",
    )

    assert official["verification_status"] == "official_primary"
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT verification_status FROM source_observations ORDER BY observed_at"
        ).fetchall()
    assert [row["verification_status"] for row in rows] == [
        "source_recorded", "official_primary",
    ]


def test_provider_news_preserves_common_link_and_publication_aliases(tmp_path: Path) -> None:
    store = _store(tmp_path)

    SourceIngestionService(store).ingest_provider_documents(
        kind="news",
        symbol="000651.SZ",
        documents=[{
            "title": "格力电器新闻",
            "source": "示例媒体",
            "source_url": "https://example.com/gree-news",
            "published": "2026-07-20 08:30:00",
            "snippet": "这是一条带原文链接和明确发布时间的新闻摘要。",
        }],
        provider_id="example",
        origin_type="data_context",
        origin_id="context-1",
    )

    source = store.list_subject_sources("000651.SZ", source_kind="news")["sources"][0]
    assert source["source_url"] == "https://example.com/gree-news"
    assert source["published_at"] == "2026-07-20 08:30:00"
    assert source["verification_status"] == "live_retrieved"


def test_news_metadata_repair_enriches_existing_document_without_duplicate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    payload = {
        "title": "格力电器新闻",
        "source": "示例媒体",
        "url": "https://example.com/gree-news",
        "published": "2026-07-20 08:30:00",
        "snippet": "",
    }
    stored = store.store_document(
        url=payload["url"],
        content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        title=payload["title"],
        publisher=payload["source"],
        published_at=None,
        cached_status="provider_snapshot",
    )
    store.record_source_observation(
        document_ref=stored.document_ref,
        subject_key="000651.SZ",
        source_kind="news",
        origin_type="data_context",
        origin_id="context-1",
        provider_id="example",
        provider_record_id=payload["url"],
    )
    with store.connect() as conn:
        conn.execute(
            """CREATE TABLE research_documents(
                   id INTEGER PRIMARY KEY,kind TEXT,symbol TEXT,title TEXT,
                   published_at TEXT,source TEXT,url TEXT,snippet TEXT,
                   payload_json TEXT,fetched_at TEXT
               )"""
        )
        conn.execute(
            """INSERT INTO research_documents(
                   id,kind,symbol,title,published_at,source,url,snippet,payload_json,fetched_at
               ) VALUES (1,'news','000651.SZ',?,?,?,?,?,?,?)""",
            (
                payload["title"],
                "",
                "example",
                payload["url"],
                "",
                json.dumps(payload, ensure_ascii=False),
                "2026-07-20T09:00:00+00:00",
            ),
        )
    repair = SourceArchiveBackfill(
        store=store,
        ingestion=SourceIngestionService(store),
        official=object(),  # type: ignore[arg-type]
        financial_extraction=object(),  # type: ignore[arg-type]
    )

    result = repair.repair_news_metadata(symbols=["000651.SZ"], dry_run=False)

    assert result["matched_documents"] == 1
    assert result["repaired"] == 1
    assert result["created"] == 0
    sources = store.list_subject_sources("000651.SZ", source_kind="news")["sources"]
    assert len(sources) == 1
    assert sources[0]["published_at"] == "2026-07-20 08:30:00"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1


def test_report_source_links_include_only_claim_support(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ingestion = SourceIngestionService(store)
    used = ingestion.ingest(
        CollectedSource(
            subject_key="688256.SH",
            source_kind="official_filing",
            provider_id="sse",
            publisher="上海证券交易所",
            title="年度报告",
            source_locator="https://www.sse.com.cn/disclosure/annual.html",
            content="年度报告全文，包含营业收入、利润和现金流量等正式披露信息。",
            verification_status="official_primary",
            body_status="full_text",
            source_class="regulatory_filing",
        ),
        origin_type="official_refresh",
        origin_id="refresh-1",
    )
    ingestion.ingest(
        CollectedSource(
            subject_key="688256.SH",
            source_kind="news",
            provider_id="media",
            publisher="媒体",
            title="未采用新闻",
            source_locator="https://example.com/news",
            content="这是一条未被报告采用的候选新闻正文。",
            verification_status="live_retrieved",
            body_status="full_text",
        ),
        origin_type="data_context",
        origin_id="context-1",
    )
    document = store.read_document(used["document_ref"], limit=1)
    chunk_ref = document["chunks"][0]["chunk_ref"]
    evidence = {
        "evidence_id": "ev_used",
        "symbol": "688256.SH",
        "domain": "financial_statements",
        "published_at": "2026-03-30",
        "summary": "年度报告披露",
        "status": "verified",
        "metadata": {
            "document_ref": used["document_ref"],
            "chunk_refs": [chunk_ref],
            "source_strength": "A",
        },
    }
    store.link_report(
        report_id="report_test",
        revision=1,
        symbol="688256.SH",
        quality_status="passed",
        evidence=[evidence],
        facts=[],
        claims=[{
            "claim_id": "claim_used",
            "section_id": "financial_quality",
            "text": "正式报告引用年度报告",
            "evidence_ids": ["ev_used"],
            "fact_ids": [],
        }],
    )

    report_sources = store.list_report_sources("report_test")["sources"]
    dossier = store.list_subject_sources("688256.SH")["sources"]
    assert [item["document_ref"] for item in report_sources] == [used["document_ref"]]
    assert report_sources[0]["relation_type"] == "cited"
    assert len(dossier) == 2
    assert sorted(item["used_by_report_count"] for item in dossier) == [0, 1]


def test_research_note_is_resolved_only_by_explicit_supported_report_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    note_id = store.index_research_session(
        session_id="session-1",
        symbol="600036.SH",
        role="assistant",
        content="净息差可能继续承压，需要年报证据确认。",
        message_id="message-1",
    )
    stored = store.store_document(
        url="https://www.sse.com.cn/filing.html",
        content="正式报告全文支持净息差变化判断。",
        title="正式披露",
        publisher="上海证券交易所",
        source_class="regulatory_filing",
    )
    chunk_ref = store.read_document(stored.document_ref, limit=1)["chunks"][0]["chunk_ref"]
    store.link_report(
        report_id="report-confirm",
        revision=1,
        symbol="600036.SH",
        quality_status="passed",
        evidence=[{
            "evidence_id": "ev-confirm",
            "symbol": "600036.SH",
            "domain": "financial_statements",
            "summary": "正式披露",
            "status": "verified",
            "metadata": {
                "document_ref": stored.document_ref,
                "chunk_refs": [chunk_ref],
                "source_strength": "A",
            },
        }],
        facts=[],
        claims=[{
            "claim_id": "claim-confirm",
            "section_id": "financial_quality",
            "text": "正式证据确认净息差承压",
            "evidence_ids": ["ev-confirm"],
            "note_claim_ids": [note_id],
        }],
    )

    notes = store.list_research_notes("600036.SH")["notes"]
    assert notes[0]["derived_status"] == "confirmed"
    assert notes[0]["resolutions"][0]["report_id"] == "report-confirm"


def test_ordinary_chat_is_excluded_from_research_notes(monkeypatch) -> None:
    calls: list[dict] = []

    class _Knowledge:
        def index_research_session(self, **payload):
            calls.append(payload)

    import src.research as research

    monkeypatch.setattr(research, "knowledge_enabled", lambda: True)
    monkeypatch.setattr(research, "get_research_knowledge_store", lambda: _Knowledge())
    message = Message(
        session_id="ordinary-chat",
        role="user",
        content="This is an ordinary chat message.",
        metadata={"linked_report_id": "report-context-only"},
    )

    SessionService._index_research_session_message(
        Session(session_id="ordinary-chat", config={}),
        message,
    )
    assert calls == []

    SessionService._index_research_session_message(
        Session(
            session_id="research-chat",
            config={"research_session": {"symbol": "600036.SH"}},
        ),
        message,
    )
    assert calls[0]["symbol"] == "600036.SH"


class _FixtureProvider(OfficialFilingProvider):
    provider_id = "sse_fixture"

    def supports(self, symbol: str) -> bool:
        return symbol.endswith(".SH")

    def list_filings(self, symbol: str, *, limit: int = 8) -> list[OfficialFilingRecord]:
        return [OfficialFilingRecord(
            provider_id=self.provider_id,
            provider_record_id="filing-1",
            symbol=symbol,
            title="2025 年年度报告",
            publisher="上海证券交易所",
            document_url="https://www.sse.com.cn/filing/2025.html",
            published_at="2026-03-30",
        )]


class _FixtureResponse:
    url = "https://www.sse.com.cn/filing/2025.html"
    headers = {"content-type": "text/html; charset=utf-8"}
    encoding = "utf-8"

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        yield (
            "<html><body><h1>年度报告</h1><p>证券代码 600036。营业收入和净利润正式披露正文，"
            "同时包含现金流量、资产负债、风险因素以及管理层讨论与分析。</p></body></html>"
        ).encode()


class _FixtureSession:
    def get(self, *args, **kwargs):
        return _FixtureResponse()


def test_official_provider_requires_full_official_document(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = OfficialFilingService(
        store=store,
        ingestion=SourceIngestionService(store),
        providers=[_FixtureProvider()],
        session=_FixtureSession(),  # type: ignore[arg-type]
    )

    result = service.refresh("600036.SH", force=True)
    sources = store.list_subject_sources("600036.SH")["sources"]
    assert result["refreshed"] == 1
    assert sources[0]["verification_status"] == "official_primary"
    assert sources[0]["body_status"] == "full_text"


class _HistoricalAnnualProvider(OfficialFilingProvider):
    provider_id = "historical_fixture"

    def supports(self, symbol: str) -> bool:
        return symbol == "600036.SH"

    def list_filings(self, symbol: str, *, limit: int = 8):
        return []

    def list_annual_reports(self, symbol: str, *, year: int, limit: int = 3):
        return [OfficialFilingRecord(
            provider_id=self.provider_id,
            provider_record_id=f"annual-{year}",
            symbol=symbol,
            title=f"{year} annual report for 600036",
            publisher="SSE fixture",
            document_url=f"https://www.sse.com.cn/filing/600036-{year}.html",
            published_at=f"{year + 1}-03-30",
            filing_type="annual",
            report_period=f"{year}-12-31",
        )]


class _HistoricalAnnualResponse:
    headers = {"content-type": "text/html; charset=utf-8"}
    encoding = "utf-8"

    def __init__(self, url: str) -> None:
        self.url = url

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        year = re.search(r"(20\d{2})", self.url).group(1)
        yield (
            f"<html><body><h1>600036 {year} annual report</h1>"
            f"<p>Security code 600036. Revenue, net profit, cash flow and balance sheet for {year}. "
            "Official full filing with management discussion and risk disclosures.</p></body></html>"
        ).encode("utf-8")


class _HistoricalAnnualSession:
    def get(self, url: str, *args, **kwargs):
        return _HistoricalAnnualResponse(url)


def test_historical_annual_backfill_archives_requested_years_and_reports_coverage(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = OfficialFilingService(
        store=store,
        ingestion=SourceIngestionService(store),
        providers=[_HistoricalAnnualProvider()],
        session=_HistoricalAnnualSession(),  # type: ignore[arg-type]
    )
    progress: list[dict] = []

    result = service.backfill_annual_reports(
        "600036.SH",
        years=[2025, 2024],
        force=False,
        progress_callback=progress.append,
    )

    assert result["status"] == "completed"
    assert result["coverage"]["covered_years"] == [2025, 2024]
    assert result["coverage"]["missing_years"] == []
    assert len(result["document_refs"]) == 2
    sources = store.list_subject_sources("600036.SH", source_kind="official_filing")["sources"]
    assert {item["metadata"]["reporting_year"] for item in sources} == {2024, 2025}
    assert all(item["metadata"]["collection_mode"] == "historical_annual_backfill" for item in sources)
    assert {
        item["year"] for item in progress if item["stage"] in {"completed", "needs_review"}
    } == {2024, 2025}
    assert {item["stage"] for item in progress if item["year"] == 2025} >= {
        "discovering", "discovered", "downloading", "parsing", "validating",
    }


class _CninfoQueryResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "announcements": [
                {
                    "secCode": "000651",
                    "announcementId": "full-2024",
                    "announcementTitle": "格力电器：2024年年度报告",
                    "adjunctUrl": "finalpage/2025-04-28/full.PDF",
                    "announcementTime": 1745769600000,
                },
                {
                    "secCode": "000651",
                    "announcementId": "summary-2024",
                    "announcementTitle": "格力电器：2024年年度报告摘要",
                    "adjunctUrl": "finalpage/2025-04-28/summary.PDF",
                    "announcementTime": 1745769600001,
                },
                {
                    "secCode": "000333",
                    "announcementId": "other-company",
                    "announcementTitle": "美的集团：2024年年度报告",
                    "adjunctUrl": "finalpage/2025-04-28/other.PDF",
                    "announcementTime": 1745769600002,
                },
            ]
        }


class _CninfoQuerySession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _CninfoQueryResponse()


def test_cninfo_provider_uses_official_archive_and_rejects_summary_and_other_company() -> None:
    session = _CninfoQuerySession()
    provider = CninfoAnnualReportProvider(session=session)  # type: ignore[arg-type]

    records = provider.list_annual_reports("000651.SZ", year=2024)

    assert len(records) == 1
    assert records[0].provider_record_id == "full-2024"
    assert records[0].document_url == "https://static.cninfo.com.cn/finalpage/2025-04-28/full.PDF"
    assert records[0].report_period == "2024-12-31"
    assert session.calls[0]["data"]["searchkey"] == "000651"
    assert session.calls[0]["data"]["column"] == "szse"


class _HttpsCaptureSession:
    def __init__(self) -> None:
        self.url = ""

    def get(self, url: str, *args, **kwargs):
        self.url = url
        return _HistoricalAnnualResponse(url)


def test_official_download_upgrades_approved_http_url_to_https(tmp_path: Path) -> None:
    session = _HttpsCaptureSession()
    service = OfficialFilingService(
        store=_store(tmp_path),
        providers=[],
        session=session,  # type: ignore[arg-type]
    )
    record = OfficialFilingRecord(
        provider_id="fixture",
        provider_record_id="http-annual",
        symbol="600036.SH",
        title="600036 2024 annual report",
        publisher="fixture",
        document_url="http://www.sse.com.cn/filing/600036-2024.html",
    )

    service._download(record)

    assert session.url == "https://www.sse.com.cn/filing/600036-2024.html"


def test_official_filings_tool_emits_machine_receipt_for_annual_backfill(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = OfficialFilingService(
        store=store,
        ingestion=SourceIngestionService(store),
        providers=[_HistoricalAnnualProvider()],
        session=_HistoricalAnnualSession(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "src.tools.official_filings_tool.get_official_filing_service",
        lambda: service,
    )
    events: list[tuple[str, dict]] = []

    payload = json.loads(OfficialFilingsTool(
        event_callback=lambda event_type, data: events.append((event_type, data)),
    ).execute(symbol="600036.SH", annual_years=[2025, 2024], force=True))

    assert payload["ok"] is True
    assert events[0][0] == "report.official_filing_refresh"
    assert events[0][1]["annual_years"] == [2025, 2024]
    assert events[0][1]["refresh"]["coverage"]["covered_years"] == [2025, 2024]


class _RedirectedMirrorResponse(_FixtureResponse):
    url = "https://mirror.example.com/filing/2025.html"


class _RedirectedMirrorSession:
    def get(self, *args, **kwargs):
        return _RedirectedMirrorResponse()


def test_official_provider_downgrades_redirects_outside_official_domains(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = OfficialFilingService(
        store=store,
        ingestion=SourceIngestionService(store),
        providers=[_FixtureProvider()],
        session=_RedirectedMirrorSession(),  # type: ignore[arg-type]
    )

    result = service.refresh("600036.SH", force=True)

    assert result["refreshed"] == 0
    assert result["failed"] == 1
    assert store.list_subject_sources("600036.SH")["sources"] == []


class _WrongSubjectResponse(_FixtureResponse):
    def iter_content(self, chunk_size: int):
        yield (
            "<html><body><h1>其他公司年度报告</h1><p>证券代码 601336。"
            "这是另一家上市公司的正式披露全文，包含财务报表和风险因素。</p></body></html>"
        ).encode()


class _WrongSubjectSession:
    def get(self, *args, **kwargs):
        return _WrongSubjectResponse()


def test_official_provider_rejects_documents_for_a_different_security(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = OfficialFilingService(
        store=store,
        ingestion=SourceIngestionService(store),
        providers=[_FixtureProvider()],
        session=_WrongSubjectSession(),  # type: ignore[arg-type]
    )

    result = service.refresh("600036.SH", force=True)

    assert result["refreshed"] == 0
    assert result["failed"] == 1
    assert store.list_subject_sources("600036.SH")["sources"] == []


class _DeepHoldingOnlyResponse(_FixtureResponse):
    def iter_content(self, chunk_size: int):
        body = (
            "<html><body><h1>证券投资基金年度报告</h1><p>基金投资组合说明。"
            + ("普通披露内容。" * 3_000)
            + "持仓证券代码 600036，持仓名称招商银行。</p></body></html>"
        )
        yield body.encode("utf-8")


class _DeepHoldingOnlySession:
    def get(self, *args, **kwargs):
        return _DeepHoldingOnlyResponse()


def test_official_provider_rejects_component_code_found_only_deep_in_fund_report(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = OfficialFilingService(
        store=store,
        ingestion=SourceIngestionService(store),
        providers=[_FixtureProvider()],
        session=_DeepHoldingOnlySession(),  # type: ignore[arg-type]
    )

    result = service.refresh("600036.SH", force=True)

    assert result["refreshed"] == 0
    assert result["failed"] == 1
    assert store.list_subject_sources("600036.SH")["sources"] == []


class _EmptyProvider(OfficialFilingProvider):
    provider_id = "empty"

    def supports(self, symbol: str) -> bool:
        return True

    def list_filings(self, symbol: str, *, limit: int = 8):
        return []


def test_refresh_downgrades_historical_deep_holdings_false_positive(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    SourceIngestionService(store).ingest(
        CollectedSource(
            subject_key="600036.SH",
            market="CN",
            source_kind="official_filing",
            provider_id="sse",
            provider_record_id="fund-annual",
            publisher="上海证券交易所",
            title="证券投资基金年度报告",
            source_locator="https://www.sse.com.cn/disclosure/fund/annual.pdf",
            content=("基金年度报告正文。" * 3_000) + "持仓证券代码 600036。",
            verification_status="official_primary",
            body_status="full_text",
            source_class="regulatory_filing",
        ),
        origin_type="official_refresh",
        origin_id="old-refresh",
    )
    service = OfficialFilingService(
        store=store,
        ingestion=SourceIngestionService(store),
        providers=[_EmptyProvider()],
        session=_FixtureSession(),  # type: ignore[arg-type]
    )

    result = service.refresh("600036.SH", force=True)
    source = store.list_subject_sources("600036.SH")["sources"][0]

    assert result["relevance_downgraded"] == 1
    assert source["verification_status"] == "source_recorded"
    assert source["source_kind"] == "other"
    assert source["metadata"]["subject_identity_status"] == "rejected"


class _MarketFixtureProvider(OfficialFilingProvider):
    provider_id = "market_fixture"

    def __init__(self, symbol: str, url: str, publisher: str) -> None:
        self.symbol = symbol
        self.url = url
        self.publisher = publisher

    def supports(self, symbol: str) -> bool:
        return symbol == self.symbol

    def list_filings(self, symbol: str, *, limit: int = 8) -> list[OfficialFilingRecord]:
        return [OfficialFilingRecord(
            provider_id=self.provider_id,
            provider_record_id=f"fixture-{symbol}",
            symbol=symbol,
            title=f"{symbol} annual report",
            publisher=self.publisher,
            document_url=self.url,
            published_at="2026-03-30",
        )]


class _MarketFixtureResponse:
    headers = {"content-type": "text/html; charset=utf-8"}
    encoding = "utf-8"

    def __init__(self, url: str, symbol: str) -> None:
        self.url = url
        self.symbol = symbol

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        code = self.symbol.split(".", 1)[0]
        yield (
            f"<html><body><h1>{self.symbol} annual report</h1>"
            f"<p>Security code {code}. Official full filing with revenue, profit, "
            "cash flow, balance sheet and risk disclosures.</p></body></html>"
        ).encode()


class _MarketFixtureSession:
    def __init__(self, url: str, symbol: str) -> None:
        self.url = url
        self.symbol = symbol

    def get(self, *args, **kwargs):
        return _MarketFixtureResponse(self.url, self.symbol)


@pytest.mark.parametrize(
    ("symbol", "url", "publisher", "market"),
    [
        ("600036.SH", "https://www.sse.com.cn/filing/600036.html", "SSE", "CN"),
        ("00700.HK", "https://www1.hkexnews.hk/filing/00700.html", "HKEX", "HK"),
        ("AAPL.US", "https://www.sec.gov/Archives/aapl.html", "SEC", "US"),
    ],
)
def test_official_fixed_samples_cover_cn_hk_and_us(
    tmp_path: Path,
    symbol: str,
    url: str,
    publisher: str,
    market: str,
) -> None:
    store = _store(tmp_path)
    service = OfficialFilingService(
        store=store,
        ingestion=SourceIngestionService(store),
        providers=[_MarketFixtureProvider(symbol, url, publisher)],
        session=_MarketFixtureSession(url, symbol),  # type: ignore[arg-type]
    )

    result = service.refresh(symbol, force=True)
    sources = store.list_subject_sources(symbol)["sources"]

    assert result["refreshed"] == 1
    assert sources[0]["verification_status"] == "official_primary"
    assert sources[0]["market"] == market
