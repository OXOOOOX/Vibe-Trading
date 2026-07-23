from __future__ import annotations

import asyncio
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.portfolio.monitoring.models import PlanValidationError, validate_monitoring_bundle
from src.portfolio.monitoring.planner import MonitoringPlanner
from src.portfolio.monitoring.report_planner import ReportDrivenMonitoringPlanner
from src.portfolio.monitoring.service import MonitoringService
from src.portfolio.monitoring.store import MonitoringStore
from src.portfolio.state import update_holdings
from src.portfolio.weekly.contracts import WeeklyContractError, validate_weekly_review
from src.portfolio.weekly.context import WeeklyContextAssembler
from src.portfolio.weekly.service import WeeklyReportRunService
from src.portfolio.weekly.store import WeeklyRunStore
from src.portfolio.weekly.verification import normalize_daily_bars, resolve_completed_trading_week


SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeCalendar:
    def __init__(self, holidays: set[date] | None = None) -> None:
        self.holidays = holidays or set()

    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5 and value not in self.holidays


class FakeBarStore:
    def __init__(self, bars: list[dict]) -> None:
        self.bars = bars

    def query_bars(self, **_kwargs) -> list[dict]:
        return list(self.bars)


class FakeMarketService:
    def __init__(self, bars: list[dict]) -> None:
        self.store = FakeBarStore(bars)
        self.refresh_calls: list[dict] = []

    def refresh_sync(self, **kwargs) -> dict:
        self.refresh_calls.append(kwargs)
        return {"run_id": f"refresh-{len(self.refresh_calls)}", "status": "completed"}


class FakeDataService:
    def __init__(self, bars: list[dict]) -> None:
        self.market_service = FakeMarketService(bars)


class FakeAgentSessionService:
    def __init__(self) -> None:
        self.messages: dict[str, list[SimpleNamespace]] = {}

    def create_session(self, title: str, config: dict) -> SimpleNamespace:
        session_id = "weekly-method-session"
        self.messages[session_id] = []
        return SimpleNamespace(session_id=session_id, title=title, config=config)

    async def execute_message(self, session_id: str, _prompt: str, **_kwargs) -> None:
        payload = {
            "regime_interpretation": "当前结构方向较清晰，但仍应等待量价继续确认。",
            "selected_methods": [
                "market_regime",
                "multi_horizon_structure",
                "reaction_evidence",
            ],
            "selected_level_ids": [],
            "evidence_for": ["多周期方向与近期结构相互支持。"],
            "counter_evidence": ["反向结构证据仍可能削弱当前判断。"],
            "cross_horizon_conclusion": "短中周期方向一致，长期结论仍需后续复核。",
            "invalidation_conditions": ["收盘结构反向破坏并得到量价确认。"],
            "confidence": "medium",
            "data_gaps": [],
            "critic": {"verdict": "pass", "issues": []},
        }
        self.messages[session_id].append(
            SimpleNamespace(role="assistant", content=json.dumps(payload, ensure_ascii=False))
        )

    def get_messages(self, session_id: str, limit: int = 20) -> list[SimpleNamespace]:
        return self.messages[session_id][-limit:]


class NoModelClient:
    model_id = "no-model"

    def complete(self, *_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("structured weekly planning must not call a model")


class PlannerMarketStore:
    def quote(self, _symbol):
        return {
            "status": "verified",
            "adjustment": "raw",
            "sources": ["tencent", "mootdx"],
            "last_price": 1.25,
            "bar_time": "2026-07-18T09:00:00+08:00",
        }

    def query_bars(self, **kwargs):
        return make_bars() if kwargs.get("interval") == "1D" else []

    def list_adjustment_factors(self, _symbol):
        return []


class PlannerMarketService:
    store = PlannerMarketStore()

    def refresh_sync(self, **_kwargs):
        return {"status": "completed"}


class PlannerMarket(MonitoringPlanner):
    def __init__(self) -> None:
        super().__init__(PlannerMarketService())


class StaticWeeklyCatalog:
    def __init__(self, store: MonitoringStore, candidate: dict) -> None:
        self.store = store
        self.candidate = candidate

    def choose_candidate(self, _symbol: str, _report_ref: str | None = None):
        return self.candidate, []

    def freeze(self, candidate: dict):
        payload = dict(candidate)
        metadata = dict(payload.get("metadata") or {})
        metadata["monitoring_bundle"] = payload["monitoring_bundle"]
        payload["metadata"] = metadata
        frozen = self.store.save_report_snapshot(payload)
        frozen["monitoring_bundle"] = frozen["metadata"]["monitoring_bundle"]
        return frozen


class StaticEvidenceCollector:
    def collect(self, **_kwargs):
        return {
            "evidence_fingerprint": "weekly-autonomous-evidence",
            "collected_at": "2026-07-20T02:00:00+00:00",
            "facts": [],
        }


def make_bars(
    *,
    through: date = date(2026, 7, 17),
    source_count: int = 2,
    status: str = "verified",
) -> list[dict]:
    values: list[dict] = []
    cursor = date(2026, 3, 2)
    while cursor <= through:
        if cursor.weekday() < 5:
            ordinal = len(values)
            base = 1.0 + ordinal * 0.002
            values.append(
                {
                    "symbol": "588870.SH",
                    "bar_time": cursor.isoformat(),
                    "open": base,
                    "high": base + 0.025,
                    "low": base - 0.012,
                    "close": base + 0.01,
                    "volume": 100_000 + ordinal * 1_000,
                    "amount": 1_000_000 + ordinal * 10_000,
                    "source_count": source_count,
                    "sources": ["tencent", "mootdx"][:source_count],
                    "status": status,
                }
            )
        cursor += timedelta(days=1)
    return values


def service(tmp_path: Path, bars: list[dict]) -> WeeklyReportRunService:
    return WeeklyReportRunService(
        store=WeeklyRunStore(tmp_path / "weekly"),
        data_service=FakeDataService(bars),
        calendar=FakeCalendar(),
        pdf_renderer=lambda _title, _markdown: b"%PDF-1.4\n%%EOF",
        state_loader=lambda: {
            "holdings": [
                {"symbol": "588870.SH", "name": "科创板芯片ETF", "quantity": 10_000}
            ]
        },
        now_provider=lambda: datetime(2026, 7, 18, 10, tzinfo=SHANGHAI),
        enabled_override=True,
    )


async def run_week(
    instance: WeeklyReportRunService,
    week_end: str,
    *,
    force_new: bool = False,
    symbols: list[str] | None = None,
) -> dict:
    records = await instance.start(
        week_end=week_end,
        symbols=symbols,
        refresh_policy="reuse",
        force_new=force_new,
    )
    return await instance.wait(records[0]["run_id"])


def run_week_sync(
    instance: WeeklyReportRunService,
    week_end: str,
    *,
    force_new: bool = False,
    symbols: list[str] | None = None,
) -> dict:
    return asyncio.run(
        run_week(instance, week_end, force_new=force_new, symbols=symbols)
    )


def test_exchange_week_resolver_handles_holiday_and_rejects_unfinished_friday() -> None:
    calendar = FakeCalendar({date(2026, 7, 13)})
    start, end, sessions = resolve_completed_trading_week(
        calendar,
        requested_week_end="2026-07-17",
        now=datetime(2026, 7, 18, 9, tzinfo=SHANGHAI),
    )
    assert (start, end) == ("2026-07-14", "2026-07-17")
    assert sessions == ["2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17"]
    with pytest.raises(ValueError, match="last completed"):
        resolve_completed_trading_week(
            FakeCalendar(),
            requested_week_end="2026-07-17",
            now=datetime(2026, 7, 17, 14, 30, tzinfo=SHANGHAI),
        )


def test_daily_bar_utc_storage_is_normalized_to_shanghai_trading_date() -> None:
    bars = normalize_daily_bars(
        [{
            "bar_time": "2026-07-16T16:00:00+00:00",
            "open": 1.8,
            "high": 1.9,
            "low": 1.7,
            "close": 1.85,
            "volume": 100,
        }],
        through="2026-07-17",
    )
    assert bars[0]["date"] == "2026-07-17"


def test_formal_etf_weekly_run_writes_immutable_structured_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())
    record = run_week_sync(instance, "2026-07-17", symbols=None)

    assert record["status"] == "completed_with_warnings"
    assert record["quality_status"] == "passed_with_gaps"
    assert {item["kind"] for item in record["artifacts"]} == {
        "weekly_review_json",
        "weekly_review_markdown",
        "weekly_review_pdf",
    }
    assert all("588870.SH" in item["filename"] and "科创板芯片ETF" in item["filename"] for item in record["artifacts"])
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")
    assert brief["trade_execution"] == "forbidden"
    assert brief["schema_version"] == 2
    assert record["report_audience"] == "user"
    assert brief["report_audience"] == "user"
    assert brief["data_scopes"] == brief["weekly_context"]["scopes"]
    assert brief["cross_horizon_context"] == brief["weekly_context"]["structured_claims"]
    assert brief["etf_context"]["symbol"] == "588870.SH"
    assert brief["context_fingerprint"] == brief["weekly_context"]["context_fingerprint"]
    assert record["security_name"] == brief["security_name"]
    assert "report_context" in brief["source_manifest"]
    assert brief["side_effects"] == {
        "model_calls": 0,
        "monitoring_activations": 0,
        "deliveries": 0,
        "trade_executions": 0,
    }
    assert brief["monitoring_bundle"]["horizon"] == "weekly"
    assert brief["monitoring_bundle"]["source_report_id"] == brief["report_id"]
    assert brief["review_due_at"].startswith("2026-07-24T15:30:00")
    assert record["watch_only_count"] == 2
    assert record["action_ready_count"] == 1
    assert len(brief["monitoring_claims"]) >= 20
    assert all(level["adjustment"] == "raw" and level["claim_ids"] for level in brief["key_levels"])
    levels = {level["level_type"]: level for level in brief["key_levels"]}
    candidates = {
        candidate["intent"]: candidate
        for candidate in brief["monitoring_bundle"]["candidates"]
    }
    assert candidates["watch"]["trigger"]["lower"] == pytest.approx(levels["support"]["lower"])
    assert candidates["watch"]["trigger"]["upper"] == pytest.approx(levels["support"]["upper"])
    assert candidates["breakout"]["trigger"]["threshold"] == pytest.approx(levels["breakout"]["value"])
    assert candidates["stop_loss"]["trigger"]["threshold"] == pytest.approx(levels["invalidation"]["value"])
    assert candidates["watch"]["calculation_basis"]["method"] == levels["support"]["calculation_basis"]["method"]
    assert candidates["breakout"]["calculation_basis"]["method"] == levels["breakout"]["calculation_basis"]["method"]
    assert candidates["stop_loss"]["calculation_basis"]["method"] == levels["invalidation"]["calculation_basis"]["method"]


def test_weekly_agent_selects_methods_after_deterministic_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())
    instance.session_service = FakeAgentSessionService()
    instance.agent_analysis_enabled_override = True

    record = run_week_sync(instance, "2026-07-17")
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")
    markdown_artifact = next(
        item for item in record["artifacts"] if item["kind"] == "weekly_review_markdown"
    )
    markdown = Path(markdown_artifact["path"]).read_text(encoding="utf-8")

    assert record["model_calls"] == 1
    assert record["agent_analysis_status"] == "completed"
    assert brief["side_effects"]["model_calls"] == 1
    assert brief["agent_analysis"]["critic"]["verdict"] == "pass"
    assert brief["analysis_method_snapshot"]["cutoff_policy"] == "completed_daily_bars_only"
    assert "## 分析方法与反证审查" in markdown
    assert "多周期价格结构" in markdown


def test_weekly_context_excludes_future_reports_and_never_reads_markdown(tmp_path) -> None:
    import sqlite3

    connection = sqlite3.connect(tmp_path / "facts.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE fact_records(
               fact_id TEXT PRIMARY KEY, metric TEXT, value TEXT, unit TEXT,
               period TEXT, created_at TEXT, superseded_by TEXT,
               evidence_ids_json TEXT, input_fact_ids_json TEXT
           )"""
    )
    connection.executemany(
        "INSERT INTO fact_records VALUES (?, ?, ?, ?, ?, ?, NULL, ?, '[]')",
        [
            (
                "fact-past", "premium_discount_rate", "0.002", "ratio",
                "2026-07-17", "2026-07-17T15:10:00+08:00", '["ev-past"]',
            ),
            (
                "fact-future", "etf_fund_units", "1000000", "fund_units",
                "2026-07-20", "2026-07-20T15:10:00+08:00", '["ev-future"]',
            ),
        ],
    )
    connection.commit()

    def report(report_id: str, cutoff: str, fact_id: str) -> dict:
        return {
            "report_id": report_id,
            "status": "published",
            "report_quality_status": "passed",
            "coverage_status": "complete",
            "data_as_of": cutoff,
            "generated_at": cutoff,
            "source_revision": 1,
            "viewpoints": [{
                "horizon": "daily",
                "valid_until": "2026-07-31T15:30:00+08:00",
            }],
            "knowledge_link": {"fact_ids": [fact_id]},
        }

    reports = {
        "past": report("past", "2026-07-17T15:00:00+08:00", "fact-past"),
        "future": report("future", "2026-07-20T15:00:00+08:00", "fact-future"),
    }

    class Knowledge:
        @staticmethod
        def connect():
            return connection

    class Library:
        knowledge = Knowledge()

        @staticmethod
        def subject(*_args, **_kwargs):
            return {
                "security_name": "科创50ETF汇添富",
                "timeline": [reports["future"], reports["past"]],
            }

        @staticmethod
        def get_report(report_id):
            return reports.get(report_id)

        @staticmethod
        def _candidate_payload(item, viewpoint):
            return {
                "report_id": item["report_id"],
                "data_as_of": item["data_as_of"],
                "viewpoint": viewpoint,
                "summary": None,
                "risks": [],
                "pending_items": [],
            }

        @staticmethod
        def read_markdown(*_args, **_kwargs):  # pragma: no cover
            raise AssertionError("weekly context must not parse report Markdown")

    context = WeeklyContextAssembler(Library()).assemble(
        "588870.SH", week_end="2026-07-17"
    )

    assert context["current_reports"]["daily"]["report_id"] == "past"
    assert context["scopes"]["premium_discount"]["availability"] == "complete"
    assert context["scopes"]["fund_shares"]["availability"] == "missing"
    assert context["source_report_ids"] == ["past"]
    assert {item["reason"] for item in context["excluded_items"]} == {
        "future_report_data"
    }
    connection.close()


def test_weekly_context_prefers_structural_identity_over_stale_subject_alias(tmp_path) -> None:
    import sqlite3

    connection = sqlite3.connect(tmp_path / "identity-facts.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE fact_records(
               fact_id TEXT PRIMARY KEY, metric TEXT, value TEXT, unit TEXT,
               period TEXT, created_at TEXT, superseded_by TEXT,
               evidence_ids_json TEXT, input_fact_ids_json TEXT
           )"""
    )
    report = {
        "report_id": "structural-current",
        "security_name": "科创50ETF汇添富",
        "status": "published",
        "report_quality_status": "passed_with_gaps",
        "coverage_status": "partial",
        "data_as_of": "2026-07-16T16:00:00+00:00",
        "generated_at": "2026-07-17T09:00:00+00:00",
        "source_revision": 24,
        "viewpoints": [{"horizon": "structural", "valid_until": None}],
        "knowledge_link": {
            "fact_ids": [],
            "etf_penetration": {
                "selected_count": 5,
                "selected_weight_coverage": 0.40793,
                "research_coverage": 0.6019,
                "partial_reusable_count": 3,
                "missing_count": 2,
            },
        },
    }

    class Knowledge:
        @staticmethod
        def connect():
            return connection

    class Library:
        knowledge = Knowledge()

        @staticmethod
        def subject(*_args, **_kwargs):
            return {"security_name": "科创50指", "timeline": [report]}

        @staticmethod
        def get_report(report_id):
            return report if report_id == report["report_id"] else None

        @staticmethod
        def _candidate_payload(item, viewpoint):
            return {
                "report_id": item["report_id"],
                "security_name": item["security_name"],
                "data_as_of": item["data_as_of"],
                "viewpoint": viewpoint,
                "summary": None,
                "risks": [],
                "pending_items": [],
            }

    context = WeeklyContextAssembler(Library()).assemble(
        "588870.SH", week_end="2026-07-17"
    )

    assert context["security_name"] == "科创50ETF汇添富"
    assert context["scopes"]["component_exposure"]["availability"] == "complete"
    assert context["scopes"]["component_research"]["availability"] == "partial"
    connection.close()


def test_markdown_and_pdf_use_chinese_presentation_labels_without_mutating_json(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    captured_pdf_input: dict[str, str] = {}
    instance = service(tmp_path, make_bars())

    def render_pdf(_title: str, markdown: str) -> bytes:
        captured_pdf_input["markdown"] = markdown
        return b"%PDF-1.4\n%%EOF"

    instance.pdf_renderer = render_pdf
    record = run_week_sync(instance, "2026-07-17")
    markdown_artifact = next(
        item
        for item in record["artifacts"]
        if item["kind"] == "weekly_review_markdown"
    )
    markdown = Path(markdown_artifact["path"]).read_text(encoding="utf-8")
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")

    assert markdown.startswith("# ")
    assert not captured_pdf_input["markdown"].startswith("# ")
    assert captured_pdf_input["markdown"] == re.sub(
        r"^#\s+[^\n]+\n?", "", markdown, count=1
    )
    assert brief["quality_status"] == "passed_with_gaps"
    assert brief["coverage_status"] == "partial"
    assert any(
        item["automation_status"] == "action_ready"
        for item in brief["monitoring_bundle"]["candidates"]
    )
    assert "报告质量 / 数据覆盖：通过（存在数据缺口） / 部分覆盖" in markdown
    assert "交易执行：禁止自动交易" in markdown
    assert "准备状态：条件已映射，可供人工启用" in markdown
    assert "14日平均真实波幅" in markdown
    assert "缺少基金成分与行业暴露数据" in markdown
    assert "当前自动引擎不执行日线收盘确认" in markdown
    assert "报告编号" not in markdown
    assert "场景族编号" not in markdown
    assert "候选编号" not in markdown
    assert "证据编号" not in markdown
    assert "未设置 →" not in markdown
    assert "观察：本周场景首次建立" in markdown
    assert "突破观察：本周场景首次建立" in markdown
    assert "止损风险观察：本周场景首次建立" in markdown

    leaked_codes = {
        "passed_with_gaps",
        "partial",
        "forbidden",
        "action_ready",
        "watch_only",
        "manual_confirmation_required",
        "awaiting_data",
        "price_cross_above",
        "price_cross_below",
        "price_zone_enter",
        "same_bucket_5m_volume_ratio",
        "original_level.lower",
        "trigger.confirmation_count",
        "etf_tracking_index_scope_unavailable",
        "etf_share_premium_tracking_error_scope_unavailable",
        "etf_component_exposure_scope_unavailable",
    }
    assert not [code for code in leaked_codes if code in markdown]


def test_company_equity_weekly_artifacts_do_not_render_etf_only_scopes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())

    record = run_week_sync(
        instance,
        "2026-07-17",
        symbols=["000651.SZ"],
    )
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")
    markdown_artifact = next(
        item for item in record["artifacts"] if item["kind"] == "weekly_review_markdown"
    )
    markdown = Path(markdown_artifact["path"]).read_text(encoding="utf-8")

    assert brief["instrument_type"] == "company_equity"
    assert brief["data_scopes"] == {}
    assert not [code for code in brief["data_gaps"] if code.startswith("etf_")]
    assert "基金份额" not in markdown
    assert "折溢价" not in markdown
    assert "跟踪指数" not in markdown
    assert "成分公司研究" not in markdown


def test_daily_research_conditions_degrade_to_watch_only_without_fake_intraday_mapping(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())
    record = run_week_sync(instance, "2026-07-17")
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")
    candidates = brief["monitoring_bundle"]["candidates"]

    breakout = next(item for item in candidates if item["intent"] == "breakout")
    assert breakout["automation_status"] == "watch_only"
    research = [
        condition["research_condition"]
        for condition in breakout["source_conditions"]
        if condition.get("research_condition")
    ]
    assert {item["interval"] for item in research} == {"1d"}
    assert any(item["kind"] == "daily_volume_ratio" and item["baseline"] == "previous_5_day_average" for item in research)
    assert all(
        condition.get("executable_mapping", {}).get("coverage_status") == "awaiting_data"
        for condition in breakout["source_conditions"]
        if condition.get("research_condition")
    )
    assert not any(
        condition.get("research_condition", {}).get("interval") in {"1m", "5m"}
        for condition in breakout["source_conditions"]
    )


def test_idempotency_and_force_new_revision(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())
    first = run_week_sync(instance, "2026-07-17")
    reused = asyncio.run(instance.start(week_end="2026-07-17", symbols=["588870.SH"], refresh_policy="reuse"))
    revised = run_week_sync(instance, "2026-07-17", force_new=True)

    assert reused[0]["run_id"] == first["run_id"]
    assert reused[0]["deduplicated"] is True
    assert revised["run_id"] != first["run_id"]
    assert revised["revision"] == first["revision"] + 1


def test_monitor_weekly_audience_is_reserved_until_it_has_a_separate_profile(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())

    with pytest.raises(ValueError, match="monitor-facing weekly reports are reserved"):
        asyncio.run(
            instance.start(
                week_end="2026-07-17",
                symbols=["588870.SH"],
                report_audience="monitor",
            )
        )

    assert instance.list_runs() == []


def test_weak_data_stops_before_formal_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars(through=date(2026, 7, 15)))
    record = run_week_sync(instance, "2026-07-17")

    assert record["status"] == "completed_with_warnings"
    assert record["stage"] == "skipped_data_unavailable"
    assert record["quality_status"] == "failed_validation"
    assert record["artifacts"] == []
    assert record["catalog_status"] == "not_registered_diagnostic"
    assert not (tmp_path / "weekly" / record["run_id"] / "outputs" / "weekly_review.json").exists()


def test_next_week_validates_prior_scenarios_and_computes_structured_deltas(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())
    instance.now_provider = lambda: datetime(2026, 7, 11, 10, tzinfo=SHANGHAI)
    first = run_week_sync(instance, "2026-07-10")
    instance.now_provider = lambda: datetime(2026, 7, 18, 10, tzinfo=SHANGHAI)
    second = run_week_sync(instance, "2026-07-17")
    brief = instance.store.read_json(second["run_id"], "outputs/weekly_review.json")

    assert first["status"] in {"completed", "completed_with_warnings"}
    assert len(brief["previous_week_validation"]) == 3
    assert {item["outcome"] for item in brief["previous_week_validation"]} <= {
        "confirmed", "invalidated", "approached", "not_triggered", "unresolved", "insufficient_data"
    }
    assert len(brief["scenario_changes"]) == 3
    assert all(item["change_type"] != "new" for item in brief["scenario_changes"])
    assert all("field_changes" in item and item["reason_claim_ids"] for item in brief["scenario_changes"])
    assert brief["previous_weekly_report"]["run_id"] == first["run_id"]


def test_weekly_bundle_feeds_manual_monitor_plan_without_model_or_activation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())
    record = run_week_sync(instance, "2026-07-17")
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")
    planner = ReportDrivenMonitoringPlanner(market_planner=PlannerMarket(), client=NoModelClient())
    plan, manifest, research = planner.build_from_monitoring_bundle(
        holding={"symbol": "588870.SH"},
        report_snapshot={
            "snapshot_id": "weekly-snapshot-1",
            "report_ref": f"weekly:{record['run_id']}:json",
            "report_type": "weekly_review",
            "title": "科创板芯片ETF周度复盘",
            "revision": 1,
            "body_sha256": "a" * 64,
            "quality_status": "data_limited",
            "generated_at": brief["generated_at"],
            "data_as_of": brief["data_as_of"],
            "monitoring_bundle": brief["monitoring_bundle"],
        },
    )

    assert research is None
    assert manifest["planner_mode"] == "structured_monitoring_bundle"
    assert plan["source_horizon"] == "weekly"
    assert plan["source_report_id"] == brief["report_id"]
    assert datetime.fromisoformat(plan["review_due_at"]) == datetime.fromisoformat(brief["review_due_at"])
    assert plan["automation_policy"]["activation_mode"] == "manual_confirmation_required"
    assert plan["automation_policy"]["trade_execution"] == "forbidden"


def test_weekly_bundle_autonomous_consumer_keeps_unapproved_levels_manual(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())
    record = run_week_sync(instance, "2026-07-17")
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")
    planner = ReportDrivenMonitoringPlanner(market_planner=PlannerMarket(), client=NoModelClient())
    plan, manifest, research = planner.build_from_monitoring_bundle(
        holding={"symbol": "588870.SH"},
        report_snapshot={
            "snapshot_id": "weekly-autonomous-snapshot-1",
            "report_ref": f"weekly:{record['run_id']}:json",
            "report_type": "weekly_review",
            "title": "科创板芯片ETF周度复盘",
            "revision": 1,
            "body_sha256": "b" * 64,
            "quality_status": "ready",
            "generated_at": brief["generated_at"],
            "data_as_of": brief["data_as_of"],
            "monitoring_bundle": brief["monitoring_bundle"],
        },
        autonomous=True,
    )

    assert research is None
    assert plan["automation_policy"]["activation_mode"] == "manual_confirmation_required"
    assert plan["automation_policy"]["activated_by"] == "weekly_report"
    assert plan["automation_policy"]["trade_execution"] == "forbidden"
    assert manifest["requires_manual_activation"] is True
    assert manifest["autonomous_report_approval"] == {
        "approved": False,
        "authority": "selected_ai_autonomous_holding",
        "data_mode": "verified",
        "mapped_condition_count": 3,
        "promoted_candidate_count": 2,
        "trade_execution": "forbidden",
    }
    assert all(not rule["enabled"] for rule in plan["market_rules"])
    assert all(
        scenario["automation_status"] == "watch_only"
        and scenario["mapping_status"] == "mapped"
        for scenario in plan["watch_scenarios"]
    )
    breakout = next(
        scenario for scenario in plan["watch_scenarios"] if scenario["intent"] == "breakout"
    )
    assert {
        condition["kind"]
        for condition in breakout["confirmation_conditions"]["conditions"]
    } == {"price_compare", "rolling_volume_ratio"}
    assert all(
        condition["coverage_status"] == "mapped"
        for condition in breakout["source_conditions"]
    )


def test_autonomous_weekly_job_activates_ai_approved_shadow_plan(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    instance = service(tmp_path, make_bars())
    record = run_week_sync(instance, "2026-07-17")
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")
    update_holdings(holdings=[{
        "name": "科创板芯片ETF",
        "code": "588870",
        "symbol": "588870.SH",
        "quantity": 1000,
        "cost_price": 1.60,
    }])

    monitor_store = MonitoringStore(tmp_path / "weekly-autonomous.sqlite3")
    monitor_store.set_autopilot_config({
        "enabled": True,
        "runtime_mode": "shadow",
        "selected_symbols": ["588870.SH"],
    })
    candidate = {
        "report_ref": f"weekly:{record['run_id']}:json",
        "report_type": "weekly_review",
        "symbol": "588870.SH",
        "title": "科创板芯片ETF周度复盘",
        "source_id": record["run_id"],
        "source_message_id": None,
        "artifact_id": "weekly_review_json",
        "revision": 1,
        "body": json.dumps(brief, ensure_ascii=False),
        "quality_status": "ready",
        "generated_at": brief["generated_at"],
        "data_as_of": brief["data_as_of"],
        "metadata": {"report_id": brief["report_id"], "horizon": "weekly"},
        "monitoring_bundle": brief["monitoring_bundle"],
    }
    planner = ReportDrivenMonitoringPlanner(
        market_planner=PlannerMarket(),
        client=NoModelClient(),
    )
    executor = ThreadPoolExecutor(max_workers=1)
    monitor_service = MonitoringService(
        store=monitor_store,
        planner=planner.market_planner,
        report_catalog=StaticWeeklyCatalog(monitor_store, candidate),
        report_planner=planner,
        planner_executor=executor,
        evidence_collector=StaticEvidenceCollector(),
    )
    try:
        job = monitor_service.create_planner_job(
            ["588870.SH"],
            report_refs={"588870.SH": candidate["report_ref"]},
            activation_mode="autonomous",
            trigger_type="report_ready",
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = monitor_store.get_planner_job(job["job_id"])
            assert job is not None
            if job["status"] in {"ready", "blocked", "failed", "cancelled"}:
                break
            time.sleep(0.02)

        assert job["status"] == "ready", json.dumps(job, ensure_ascii=False, default=str)
        item = job["items"][0]
        assert item["status"] == "ready"
        expected_progress = {
            "stage": "ready",
            "requires_manual_activation": False,
            "activated": True,
            "activation_mode": "autonomous",
            "catalog_report_id": None,
            "source_horizon": "weekly",
            "trade_execution": "forbidden",
        }
        assert all(item["progress"].get(key) == value for key, value in expected_progress.items())
        profile = monitor_store.get_profile(item["profile_id"])
        assert profile is not None
        assert profile["status"] == "active"
        assert profile["active_plan_version"] == item["plan_version"]
        version = next(
            entry for entry in profile["plans"]
            if entry["version"] == profile["active_plan_version"]
        )
        assert version["created_by"] == "autopilot"
        assert version["plan"]["automation_policy"]["activation_mode"] == "autonomous"
        assert version["plan"]["automation_policy"]["activated_by"] == "autopilot"
        assert version["plan"]["automation_policy"]["trade_execution"] == "forbidden"
        decision = version["evidence_manifest"]["autonomous_analysis"][
            "report_activation_decision"
        ]
        assert decision == {
            "source_requires_manual_confirmation": True,
            "autonomous_approval": True,
            "decision_authority": "selected_ai_autonomous_holding",
        }
    finally:
        executor.shutdown(wait=True)


def test_single_source_requires_explicit_consent_for_action_ready(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars(source_count=1))
    record = run_week_sync(instance, "2026-07-17")
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")
    assert all(item["automation_status"] == "watch_only" for item in brief["monitoring_bundle"]["candidates"])
    assert brief["monitoring_bundle"]["price_volume_context"]["single_source_authorized"] is False


def test_contract_rejects_wrong_horizon_and_invalid_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
    instance = service(tmp_path, make_bars())
    record = run_week_sync(instance, "2026-07-17")
    brief = instance.store.read_json(record["run_id"], "outputs/weekly_review.json")
    wrong_horizon = json.loads(json.dumps(brief["monitoring_bundle"]))
    wrong_horizon["horizon"] = "daily"
    with pytest.raises(PlanValidationError, match="horizon"):
        validate_monitoring_bundle(
            wrong_horizon,
            expected_symbol="588870.SH",
            expected_horizon="weekly",
        )
    with pytest.raises(WeeklyContractError, match="weekly review must be an object"):
        validate_weekly_review(None)  # type: ignore[arg-type]


def test_feature_flag_defaults_off(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("VIBE_TRADING_WEEKLY_REPORT_ENABLED", raising=False)
    instance = WeeklyReportRunService(
        store=WeeklyRunStore(tmp_path / "weekly-disabled"),
        data_service=FakeDataService(make_bars()),
        calendar=FakeCalendar(),
        pdf_renderer=lambda *_args: b"%PDF-1.4\n%%EOF",
        state_loader=lambda: {"holdings": []},
        recover_incomplete=False,
    )
    assert instance.enabled() is False
