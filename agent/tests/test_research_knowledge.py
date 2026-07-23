from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.research.knowledge import ResearchKnowledgeStore, normalize_url
from src.tools.report_evidence_tool import RecordReportEvidenceTool
from src.tools import web_reader_tool
from src.session.events import EventBus
from src.session.service import SessionService
from src.session.store import SessionStore


def make_store(tmp_path: Path) -> ResearchKnowledgeStore:
    return ResearchKnowledgeStore(
        path=tmp_path / "research_cache.sqlite3",
        object_dir=tmp_path / "objects",
    )


def test_migration_preserves_legacy_research_documents(tmp_path: Path) -> None:
    path = tmp_path / "research_cache.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE research_documents(id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO research_documents(title) VALUES ('legacy')")
    store = ResearchKnowledgeStore(path=path, object_dir=tmp_path / "objects")
    with store.connect() as conn:
        assert conn.execute("SELECT title FROM research_documents").fetchone()[0] == "legacy"
        assert conn.execute("SELECT MAX(version) FROM research_knowledge_schema").fetchone()[0] >= 4
    assert path.with_suffix(".sqlite3.pre-knowledge-v1.bak").exists()


def test_normalize_url_removes_tracking_and_sorts_query() -> None:
    assert normalize_url("HTTPS://Example.COM:443/a//b?utm_source=x&z=2&a=1#part") == (
        "https://example.com/a/b?a=1&z=2"
    )


def test_register_bundle_retries_short_sqlite_writer_contention(
    tmp_path: Path, monkeypatch,
) -> None:
    store = make_store(tmp_path)
    attempts: list[int] = []

    def flaky_register(_bundle: dict) -> dict:
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return {"evidence": 1, "facts": 0, "conflicts": []}

    monkeypatch.setattr(store, "_register_bundle_once", flaky_register)
    monkeypatch.setattr("src.research.knowledge.time.sleep", lambda _seconds: None)

    result = store.register_bundle({"evidence": [{"evidence_id": "ev-1"}]})

    assert result == {"evidence": 1, "facts": 0, "conflicts": []}
    assert len(attempts) == 3


def test_etf_coverage_plan_uses_etf_specific_domains(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    plan = store.create_coverage_plan(
        symbol="159516.SZ",
        profile="etf_deep_research",
        as_of="2026-07-23",
        report_id="report_etf_coverage",
    )

    domains = {item["domain"] for item in plan["domains"]}
    assert "index_methodology" in domains
    assert "creation_redemption" in domains
    assert "tracking_quality" in domains
    assert "component_research" in domains
    assert "financial_statements" not in domains
    assert plan["coverage_policy_version"] == "etf-coverage-v1"


def test_rolling_metric_comparison_ignores_period_without_added_stale_pair() -> None:
    previous = {
        "symbol": "159516.SZ",
        "metric": "fund_units",
        "value": "100",
        "period": "2026-07-22",
        "scope_key": "",
        "unit": "fund_units",
        "currency": "",
    }
    current = {**previous, "value": "110", "period": "2026-07-23"}

    assert ResearchKnowledgeStore._comparison_key(previous) == (
        ResearchKnowledgeStore._comparison_key(current)
    )
    assert ResearchKnowledgeStore._values_consistent(
        previous["value"], current["value"], metric="fund_units"
    ) is False


def test_full_document_is_content_addressed_chunked_and_searchable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    body = "# 行业规模\n\n" + "晶体振荡器市场继续增长。" * 900
    first = store.store_document(
        url="https://example.com/report?utm_campaign=test",
        content=body,
        title="晶振行业报告",
        publisher="示例研究",
        aliases=("泰晶科技", "603738.SH"),
    )
    second = store.store_document(
        url="https://example.com/report",
        content=body,
        title="晶振行业报告",
        publisher="示例研究",
    )
    assert first.document_ref == second.document_ref
    assert Path(first.object_path).read_text(encoding="utf-8") == body
    assert len(first.chunk_catalog) > 1
    tail = store.read_document(first.document_ref, chunk_refs=[first.chunk_catalog[-1]["chunk_ref"]])
    assert tail["chunks"][0]["text"]
    result = store.search(query="泰晶科技 晶振", limit=5)
    assert any(item["document_ref"] == first.document_ref for item in result["chunks"])


def test_repeated_document_capture_enriches_missing_publication_metadata(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.store_document(
        url="https://example.com/news/1",
        content="same provider payload",
        title="示例新闻",
    )

    second = store.store_document(
        url="https://example.com/news/1",
        content="same provider payload",
        title="示例新闻",
        publisher="示例媒体",
        published_at="2026-07-20 09:00:00",
    )

    assert second.document_ref == first.document_ref
    document = store.document(first.document_ref)
    assert document is not None
    assert document["publisher"] == "example.com"
    assert document["published_at"] == "2026-07-20 09:00:00"


def test_web_reader_tool_forwards_subject_key_from_research_session(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _SessionStore:
        @staticmethod
        def get_session(session_id: str):
            assert session_id == "session-news"
            return type("SessionFixture", (), {
                "config": {"research_session": {"resolved_symbol": "000651.SZ"}}
            })()

    def fake_read_url(url: str, no_cache: bool = False, subject_key: str = "") -> str:
        captured.update(url=url, no_cache=no_cache, subject_key=subject_key)
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(web_reader_tool, "read_url", fake_read_url)
    tool = web_reader_tool.WebReaderTool(
        default_session_id="session-news",
        session_store=_SessionStore(),
    )

    assert json.loads(tool.execute(url="https://example.com/news"))["status"] == "ok"
    assert captured == {
        "url": "https://example.com/news",
        "no_cache": False,
        "subject_key": "000651.SZ",
    }
    assert "subject_key" in tool.parameters["properties"]


def test_read_url_preview_truncation_keeps_full_tail_in_chunk_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    import src.research.knowledge as knowledge_module

    monkeypatch.setenv("VIBE_TRADING_RESEARCH_KNOWLEDGE_ENABLED", "1")
    monkeypatch.setattr(knowledge_module, "_shared_store", store)
    full = "Title: 长网页\n\n# 第一节\n\n" + ("前半部分。" * 2000) + "\n\n# 最后一节\n\n唯一尾部证据XYZ。"

    class Response:
        status_code = 200
        text = full

    monkeypatch.setattr(web_reader_tool.requests, "get", lambda *args, **kwargs: Response())
    result = json.loads(
        web_reader_tool.read_url(
            "https://example.test/long-report",
            subject_key="603738.SH",
        )
    )
    assert result["status"] == "ok"
    assert result["length"] > 8000
    assert "preview truncated" in result["content"]
    assert result["document_ref"].startswith("doc_")
    tail = store.read_document(result["document_ref"], query="唯一尾部证据XYZ")
    assert any("唯一尾部证据XYZ" in item["text"] for item in tail["chunks"])
    dossier = store.list_subject_sources("603738.SH")["sources"]
    assert dossier[0]["document_ref"] == result["document_ref"]
    assert dossier[0]["verification_status"] == "live_retrieved"


def test_document_ref_evidence_uses_source_metadata_and_detects_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    import src.research.knowledge as knowledge_module

    monkeypatch.setenv("VIBE_TRADING_RESEARCH_KNOWLEDGE_ENABLED", "1")
    monkeypatch.setattr(knowledge_module, "_shared_store", store)
    one = store.store_document(
        url="https://www.sse.com.cn/disclosure/one",
        content="# 财务数据\n\n2025年度营业收入为100亿元。",
        title="年度报告",
        publisher="上海证券交易所",
        published_at="2026-03-01",
    )
    tool = RecordReportEvidenceTool()
    output = json.loads(tool.execute(
        symbol="603738.SH",
        document_ref=one.document_ref,
        chunk_refs=[one.chunk_catalog[0]["chunk_ref"]],
        domain="announcement",
        facts=[{
            "metric": "revenue",
            "value": 100,
            "unit": "亿元",
            "currency": "CNY",
            "period": "2025",
            "scope_key": "consolidated",
        }],
    ))
    assert output["status"] == "ok"
    assert output["document_ref"] == one.document_ref
    with store.connect() as conn:
        evidence = conn.execute("SELECT source_strength FROM evidence_records").fetchone()
    assert evidence["source_strength"] == "A"

    two = store.store_document(
        url="https://www.szse.cn/disclosure/two",
        content="# 财务数据\n\n2025年度营业收入为130亿元。",
        title="另一正式披露",
        publisher="深圳证券交易所",
        published_at="2026-03-02",
    )
    conflicting = json.loads(tool.execute(
        symbol="603738.SH",
        document_ref=two.document_ref,
        chunk_refs=[two.chunk_catalog[0]["chunk_ref"]],
        domain="announcement",
        facts=[{
            "metric": "revenue", "value": 130, "unit": "亿元", "currency": "CNY",
            "period": "2025", "scope_key": "consolidated",
        }],
    ))
    assert conflicting["status"] == "ok"
    assert conflicting["conflicts"][0]["resolution_status"] == "needs_third_source"


def test_official_structured_fact_supersedes_lower_priority_provider_and_keeps_audit(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    provider = store.store_document(
        url="https://data.example.test/000651/annual",
        content="Provider snapshot revenue 1900.",
        title="结构化财务快照",
        publisher="示例数据提供方",
        source_class="commercial_research",
    )
    official = store.store_document(
        url="https://www.cninfo.com.cn/new/disclosure/detail?stockCode=000651",
        content="2025 年年度报告营业收入为 1880 亿元。",
        title="格力电器2025年年度报告",
        publisher="巨潮资讯",
        source_class="regulatory_filing",
    )
    store.record_structured_extraction(
        document_ref=official.document_ref,
        subject_key="000651.SZ",
        extractor_id="annual-report",
        extractor_version="1",
        extraction_method="table",
        status="completed",
        result={"metrics": [{"metric": "revenue", "value": "1880"}]},
        validation={"status": "passed"},
    )
    base = {
        "symbol": "000651.SZ",
        "metric": "revenue",
        "unit": "亿元",
        "period": "2025-12-31",
        "validation_status": "pass",
        "metadata": {"currency": "CNY", "scope_key": "consolidated"},
    }
    store.register_bundle({
        "evidence": [{
            "evidence_id": "provider-evidence",
            "symbol": "000651.SZ",
            "domain": "financial_statements",
            "metadata": {
                "document_ref": provider.document_ref,
                "source_strength": "C",
            },
        }],
        "facts": [{
            **base,
            "fact_id": "provider-fact",
            "value": "1900",
            "evidence_ids": ["provider-evidence"],
        }],
    })
    result = store.register_bundle({
        "evidence": [{
            "evidence_id": "official-evidence",
            "symbol": "000651.SZ",
            "domain": "financial_statements",
            "metadata": {
                "document_ref": official.document_ref,
                "source_strength": "A",
            },
        }],
        "facts": [{
            **base,
            "fact_id": "official-fact",
            "value": "1880",
            "evidence_ids": ["official-evidence"],
        }],
    })

    assert result["conflicts"][0]["resolution_status"] == "resolved_source_priority"
    assert result["conflicts"][0]["selected_fact_id"] == "official-fact"
    assert result["conflicts"][0]["selected_source_tier"] == "official_structured"
    with store.connect() as conn:
        provider_row = conn.execute(
            "SELECT superseded_by FROM fact_records WHERE fact_id='provider-fact'"
        ).fetchone()
        official_row = conn.execute(
            "SELECT superseded_by FROM fact_records WHERE fact_id='official-fact'"
        ).fetchone()
        conflict = conn.execute(
            "SELECT resolution_reason FROM fact_conflicts WHERE resolution_status='resolved_source_priority'"
        ).fetchone()
    assert provider_row[0] == "official-fact"
    assert official_row[0] is None
    assert "official_structured" in conflict[0]


def test_search_summary_cannot_be_registered_as_evidence() -> None:
    result = json.loads(
        RecordReportEvidenceTool().execute(
            symbol="603738.SH",
            source="search engine",
            source_locator="https://example.com/result",
            published_at="2026-07-19",
            source_read_status="search_summary",
            excerpt="This is only a search result summary, not an opened source.",
            domain="news",
            facts=[{
                "metric": "event_summary",
                "value": "unverified",
                "period": "2026-07-19",
            }],
        )
    )

    assert result["status"] == "error"
    assert "search snippets" in result["error"]


def test_research_session_messages_remain_unverified_claims(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.index_research_session(
        session_id="session-1", symbol="603738.SH", role="user",
        content="我认为需求会翻倍", message_id="u1",
    )
    store.index_research_session(
        session_id="session-1", symbol="603738.SH", role="assistant",
        content="需求可能进入上行周期", message_id="a1",
    )
    with store.connect() as conn:
        statuses = {row[0] for row in conn.execute("SELECT claim_status FROM claim_records")}
        fact_count = conn.execute("SELECT COUNT(*) FROM fact_records").fetchone()[0]
    assert statuses == {"hypothesis", "unverified_prior_claim"}
    assert fact_count == 0


def test_corrected_source_supersedes_old_fact_and_scope_difference_is_not_value_conflict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.store_document(
        url="https://example.test/filing", content="营业收入100亿元。",
        publisher="交易所", published_at="2026-03-01",
    )
    second = store.store_document(
        url="https://example.test/filing", content="更正后营业收入101亿元。",
        publisher="交易所", published_at="2026-03-02",
    )
    ev1 = {"evidence_id": "ev1", "symbol": "603738.SH", "domain": "announcement", "metadata": {"document_ref": first.document_ref, "chunk_refs": [first.chunk_catalog[0]["chunk_ref"]], "source_strength": "A"}}
    ev2 = {"evidence_id": "ev2", "symbol": "603738.SH", "domain": "announcement", "metadata": {"document_ref": second.document_ref, "chunk_refs": [second.chunk_catalog[0]["chunk_ref"]], "source_strength": "A"}}
    base = {"symbol": "603738.SH", "metric": "revenue", "unit": "亿元", "period": "2025", "validation_status": "pass", "metadata": {"currency": "CNY", "scope_key": "consolidated"}}
    store.register_bundle({"evidence": [ev1], "facts": [{**base, "fact_id": "fact1", "value": "100", "evidence_ids": ["ev1"]}]})
    result = store.register_bundle({"evidence": [ev2], "facts": [{**base, "fact_id": "fact2", "value": "101", "evidence_ids": ["ev2"]}]})
    assert result["conflicts"] == []
    with store.connect() as conn:
        assert conn.execute("SELECT superseded_by FROM fact_records WHERE fact_id='fact1'").fetchone()[0] == "fact2"

    scope_result = store.register_bundle({
        "evidence": [ev2],
        "facts": [{
            **base, "fact_id": "fact3", "value": "80", "evidence_ids": ["ev2"],
            "metadata": {"currency": "CNY", "scope_key": "parent_company"},
        }],
    })
    assert scope_result["conflicts"] == []
    with store.connect() as conn:
        mismatch = conn.execute("SELECT resolution_status FROM fact_conflicts WHERE conflict_type='scope_mismatch'").fetchone()
    assert mismatch[0] == "not_conflict"


def test_component_weight_scope_correction_hides_legacy_false_conflicts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    base = {
        "symbol": "588870.SH",
        "metric": "etf_component_weight",
        "unit": "ratio",
        "period": "2026-06-30",
        "validation_status": "pass",
    }
    # Legacy component Facts lacked scope_key and were therefore misclassified
    # as contradictory values for the same component.
    store.register_bundle({"facts": [
        {**base, "fact_id": "fact-a", "value": "0.12"},
        {**base, "fact_id": "fact-b", "value": "0.08"},
    ]})
    assert store.unresolved_conflicts(
        "588870.SH", fact_ids=["fact-a", "fact-b"]
    )

    store.register_bundle({"facts": [
        {
            **base,
            "fact_id": "fact-a",
            "value": "0.12",
            "metadata": {"component_symbol": "688256.SH"},
        },
        {
            **base,
            "fact_id": "fact-b",
            "value": "0.08",
            "metadata": {"component_symbol": "688041.SH"},
        },
    ]})

    assert store.unresolved_conflicts(
        "588870.SH", fact_ids=["fact-a", "fact-b"]
    ) == []
    with store.connect() as conn:
        scopes = {
            row["fact_id"]: row["scope_key"]
            for row in conn.execute(
                "SELECT fact_id, scope_key FROM fact_records WHERE fact_id IN ('fact-a', 'fact-b')"
            )
        }
    assert scopes == {"fact-a": "688256.SH", "fact-b": "688041.SH"}

    coverage = {
        "symbol": "588870.SH",
        "metric": "etf_component_research_coverage",
        "unit": "ratio",
        "period": "2026-06-30",
        "validation_status": "pass",
    }
    store.register_bundle({"facts": [
        {**coverage, "fact_id": "coverage-old", "value": "0"},
        {**coverage, "fact_id": "coverage-new", "value": "0.6"},
    ]})
    assert store.unresolved_conflicts(
        "588870.SH", fact_ids=["coverage-old", "coverage-new"]
    ) == []


def test_backfill_is_idempotent_and_rejects_failed_report_claims(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    reports = tmp_path / "reports"
    report = reports / "report_0123456789abcdef"
    (report / "analysis").mkdir(parents=True)
    (report / "manifest.json").write_text(json.dumps({
        "report_id": report.name,
        "revision": 1,
        "symbol": "603738.SH",
        "profile": "equity_deep_research",
        "quality_status": "failed_validation",
        "data_as_of": "2026-07-18",
    }), encoding="utf-8")
    (report / "analysis" / "evidence.jsonl").write_text(json.dumps({
        "evidence_id": "ev_old", "symbol": "603738.SH", "domain": "industry",
        "source": "旧资料", "source_locator": "https://example.com/old",
        "published_at": "2025-01-01", "summary": "旧资料中可追溯的原始摘要内容。",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    (report / "analysis" / "facts.jsonl").write_text(json.dumps({
        "fact_id": "fact_old", "symbol": "603738.SH", "metric": "tam",
        "value": "10", "unit": "亿元", "period": "2025", "evidence_ids": ["ev_old"],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    (report / "claims.jsonl").write_text(json.dumps({
        "claim_id": "claim_old", "claim_type": "inference", "text": "旧报告判断",
        "fact_ids": ["fact_old"], "evidence_ids": ["ev_old"],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    first = store.backfill_reports(reports)
    second = store.backfill_reports(reports)
    assert first["reports"] == second["reports"] == 1
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_knowledge_links").fetchone()[0] == 1
        assert conn.execute("SELECT claim_status FROM claim_records WHERE claim_id='claim_old'").fetchone()[0] == "rejected_prior"


def test_linked_report_followup_uses_structured_context_not_old_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    import src.research.knowledge as knowledge_module

    monkeypatch.setenv("VIBE_TRADING_RESEARCH_KNOWLEDGE_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_RESEARCH_HISTORY_REUSE_ENABLED", "1")
    monkeypatch.setattr(knowledge_module, "_shared_store", store)
    service = SessionService(
        SessionStore(tmp_path / "sessions"),
        EventBus(),
        tmp_path / "runs",
    )
    record = service.deep_reports.begin(
        session_id="session-1", attempt_id="attempt-1", request_content="603738.SH",
    )
    store.link_report(
        report_id=record.report_id, revision=1, symbol="603738.SH", quality_status="passed",
        evidence=[], facts=[], claims=[{
            "claim_id": "claim-context", "claim_type": "inference",
            "section_id": "executive_summary", "text": "上次判断仅供复核",
        }],
    )
    monkeypatch.setattr(service.deep_reports, "read_markdown", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old Markdown must not be read")))
    context = service._linked_report_context(record.report_id)
    assert "上次判断仅供复核" in context
    assert "prior_claims_not_evidence" in context
    assert "LINKED_MARKDOWN" not in context
