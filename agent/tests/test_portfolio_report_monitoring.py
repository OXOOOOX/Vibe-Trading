from __future__ import annotations

import copy
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.portfolio_monitor_routes import register_portfolio_monitor_routes
from src.portfolio.monitoring.models import PlanValidationError, validate_plan
from src.portfolio.monitoring.planner import MonitoringPlanner
from src.portfolio.monitoring.price_volume import PriceVolumeAnalyzer
from src.portfolio.monitoring.report_catalog import MonitorReportCatalog
from src.portfolio.monitoring.report_planner import (
    DeterministicMarketRepairError,
    ReportDrivenMonitoringPlanner,
)
from src.portfolio.monitoring.service import MonitoringService
from src.portfolio.monitoring.store import MonitoringStore
from src.portfolio.daily.store import DailyRunStore
from src.portfolio.state import update_holdings
from src.session.models import Message, Session
from src.session.store import SessionStore


class MarketStore:
    def quote(self, symbol: str):
        return {
            "symbol": symbol,
            "interval": "5m",
            "bar_time": "2026-07-16T02:00:00+00:00",
            "session_date": "2026-07-16",
            "adjustment": "raw",
            "last_price": 40.0,
            "status": "verified",
            "sources": ["tencent", "mootdx"],
        }

    def query_bars(self, *, interval: str, **_kwargs):
        if interval == "1D":
            return [
                {
                    "bar_time": f"2026-06-{index:02d}T07:00:00+00:00",
                    "session_date": f"2026-06-{index:02d}",
                    "open": 38 + index / 10,
                    "high": 39 + index / 10,
                    "low": 37 + index / 10,
                    "close": 38.5 + index / 10,
                    "status": "verified",
                    "sources": ["tencent", "mootdx"],
                }
                for index in range(1, 21)
            ]
        return [
            {
                "bar_time": f"2026-07-16T02:{index:02d}:00+00:00",
                "session_date": "2026-07-16",
                "open": 39.8,
                "high": 40.1,
                "low": 39.7,
                "close": 40.0,
                "volume": 1000 + index,
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            }
            for index in range(1, 10)
        ]


class MarketService:
    def __init__(self) -> None:
        self.store = MarketStore()
        self.refresh_calls = 0

    def refresh_sync(self, **_kwargs):
        self.refresh_calls += 1
        return {"status": "completed"}


def planner_output(*, metric: str = "same_bucket_5m_volume_ratio") -> dict:
    return {
        "report": {
            "title": "招商银行关键点位监控研究",
            "quality_status": "ready",
            "generated_at": "2026-07-16T02:01:00+00:00",
            "data_as_of": "2026-07-16T02:00:00+00:00",
            "summary": "观察 40.20 上方是否形成闭合 K 线确认。",
            "evidence_notes": ["价格采用未复权原始口径。"],
        },
        "watch_scenarios": [
            {
                "scenario_id": "breakout-4020",
                "label": "40.20 突破观察",
                "intent": "breakout",
                "evidence_refs": ["报告/关键点位/阻力位"],
                "original_level": {
                    "kind": "price",
                    "value": 40.2,
                    "unit": "CNY",
                    "adjustment": "raw",
                    "source_text": "40.20 元上方观察突破",
                },
                "trigger": {
                    "kind": "price_cross_above",
                    "threshold": 40.2,
                    "interval": "5m",
                    "confirmation_count": 2,
                },
                "approach_policy": {"distance_bps": 100, "source": "report"},
                "volume_confirmation": {
                    "metric": metric,
                    "comparator": "gte",
                    "threshold": 1.2,
                    "min_samples": 5,
                    "unit": "ratio",
                },
                "resolution_policy": {
                    "rejection_hysteresis_bps": 30,
                    "max_observation_bars": 6,
                },
                "invalidation": {"kind": "price_cross_below", "level": 36.0},
                "rationale": "报告给出明确阻力位，需观察突破后是否站稳。",
            }
        ],
    }


def autonomous_planner_output() -> dict:
    output = planner_output()
    scenario = output["watch_scenarios"][0]
    scenario.update(
        source_conditions=[
            {
                "condition_id": "source-breakout",
                "source_text": "40.20 元上方观察突破",
                "role": "required",
                "coverage_status": "mapped",
                "reason": "",
                "evidence_refs": ["报告/关键点位"],
            },
            {
                "condition_id": "source-confirm",
                "source_text": "30 分钟收盘确认",
                "role": "required",
                "coverage_status": "mapped",
                "reason": "",
                "evidence_refs": ["报告/确认信号"],
            },
        ],
        entry_conditions={
            "operator": "all",
            "conditions": [{
                "condition_id": "entry-breakout",
                "source_condition_id": "source-breakout",
                "kind": "price_compare",
                "operator": "gte",
                "value": 40.2,
                "interval": "5m",
                "consecutive": 1,
                "lookback_bars": 1,
                "freshness_seconds": 900,
            }],
        },
        confirmation_conditions={
            "operator": "all",
            "conditions": [{
                "condition_id": "confirm-30m",
                "source_condition_id": "source-confirm",
                "kind": "price_compare",
                "operator": "gte",
                "value": 40.2,
                "interval": "30m",
                "consecutive": 1,
                "lookback_bars": 1,
                "freshness_seconds": 3600,
            }],
        },
        invalidation_conditions={"operator": "all", "conditions": []},
        sequence_policy={"enabled": True, "max_wait_bars": 6, "reset_on_invalidation": True},
        action_template={
            "action": "observe",
            "sizing": {"kind": "default_policy", "source": "system_default"},
            "confidence_floor": "medium",
        },
        automation_status="action_ready",
    )
    return output


class FakeClient:
    model_id = "fake-monitor-model"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(self, _messages):
        self.calls += 1
        return self.responses.pop(0)


def snapshot(store: MonitoringStore) -> dict:
    body = "# 招商银行深研\n\n## 关键点位\n\n40.20 元上方观察突破。\n" + "证据。" * 300
    return store.save_report_snapshot(
        {
            "report_ref": "session:test:message-1",
            "report_type": "single_stock_research",
            "symbol": "600036.SH",
            "title": "招商银行深研",
            "source_id": "test",
            "source_message_id": "message-1",
            "artifact_id": None,
            "revision": 1,
            "body": body,
            "quality_status": "ready",
            "generated_at": "2026-07-16T01:00:00+00:00",
            "data_as_of": "2026-07-16T01:00:00+00:00",
            "metadata": {},
        }
    )


def build_plan(tmp_path, responses: list[str] | None = None):
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    client = FakeClient(responses or [json.dumps(planner_output(), ensure_ascii=False)])
    planner = ReportDrivenMonitoringPlanner(
        market_planner=MonitoringPlanner(MarketService()),
        client=client,
    )
    report_snapshot = snapshot(store)
    plan, manifest, research = planner.build(
        job_id="job-1",
        holding={"symbol": "600036.SH", "name": "招商银行", "quantity": 1000, "cost_price": 38},
        report_snapshot=report_snapshot,
        research_required=False,
    )
    return store, planner, client, report_snapshot, plan, manifest, research


def test_report_snapshot_is_immutable_and_schema_v4_rejects_unknown_metrics(tmp_path) -> None:
    store, _planner, _client, report_snapshot, plan, _manifest, _research = build_plan(tmp_path)

    assert plan["schema_version"] == 4
    assert plan["analysis_ref"]["snapshot_id"] == report_snapshot["snapshot_id"]
    assert plan["analysis_ref"]["body_sha256"] == hashlib.sha256(
        report_snapshot["body"].encode("utf-8")
    ).hexdigest()
    assert plan["watch_scenarios"][0]["volume_confirmation"]["mode"] == "classify_only"
    assert store.save_report_snapshot(report_snapshot)["snapshot_id"] == report_snapshot["snapshot_id"]

    invented = copy.deepcopy(plan)
    invented["watch_scenarios"][0]["volume_confirmation"]["metric"] = "AI_magic_money_flow"
    with pytest.raises(PlanValidationError, match="metric is not allowed"):
        validate_plan(invented, expected_symbol="600036.SH")

    expression = copy.deepcopy(plan)
    expression["watch_scenarios"][0]["trigger"]["threshold"] = "last_price * 1.02"
    with pytest.raises(PlanValidationError, match="number"):
        validate_plan(expression, expected_symbol="600036.SH")


def test_autonomous_report_planner_emits_v5_with_full_source_coverage(tmp_path) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    client = FakeClient([json.dumps(autonomous_planner_output(), ensure_ascii=False)])
    planner = ReportDrivenMonitoringPlanner(
        market_planner=MonitoringPlanner(MarketService()),
        client=client,
    )
    plan, manifest, _research = planner.build(
        job_id="autonomous-job",
        holding={"symbol": "600036.SH", "name": "招商银行", "quantity": 1000, "cost_price": 38},
        report_snapshot=snapshot(store),
        research_required=False,
        autonomous=True,
        supplemental_evidence={
            "evidence_fingerprint": "bundle-fingerprint",
            "facts": [],
            "auxiliary": {},
        },
    )
    assert plan["schema_version"] == 5
    assert plan["automation_policy"] == {
        "activation_mode": "autonomous",
        "activated_by": "autopilot",
        "evidence_fingerprint": "bundle-fingerprint",
        "trade_execution": "forbidden",
    }
    scenario = plan["watch_scenarios"][0]
    assert scenario["automation_status"] == "action_ready"
    assert scenario["confirmation_conditions"]["conditions"][0]["interval"] == "30m"
    assert all(item["coverage_status"] == "mapped" for item in scenario["source_conditions"])
    assert manifest["market_evidence"]["supplemental_evidence"]["evidence_fingerprint"] == "bundle-fingerprint"


def test_autonomous_planner_canonicalizes_price_alias_and_fails_closed_on_missing_mapping(
    tmp_path,
) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    output = autonomous_planner_output()
    scenario = output["watch_scenarios"][0]
    scenario["entry_conditions"]["conditions"][0]["metric"] = "last_price"
    scenario["confirmation_conditions"]["conditions"] = []
    client = FakeClient([json.dumps(output, ensure_ascii=False)])
    planner = ReportDrivenMonitoringPlanner(
        market_planner=MonitoringPlanner(MarketService()),
        client=client,
    )

    plan, _manifest, _research = planner.build(
        job_id="autonomous-canonical-job",
        holding={"symbol": "600036.SH", "name": "test", "quantity": 1000, "cost_price": 38},
        report_snapshot=snapshot(store),
        research_required=False,
        autonomous=True,
        supplemental_evidence={"evidence_fingerprint": "bundle-fingerprint", "facts": []},
    )

    normalized = plan["watch_scenarios"][0]
    assert client.calls == 1
    assert "metric" not in normalized["entry_conditions"]["conditions"][0]
    assert normalized["source_conditions"][1]["coverage_status"] == "awaiting_data"
    assert normalized["automation_status"] == "watch_only"


def test_strict_json_gets_only_one_repair(tmp_path) -> None:
    good = json.dumps(planner_output(), ensure_ascii=False)
    _store, _planner, client, _snapshot, plan, _manifest, _research = build_plan(
        tmp_path,
        responses=["not-json", good],
    )
    assert client.calls == 2
    assert plan["schema_version"] == 4

    bad_metric = json.dumps(planner_output(metric="hallucinated_indicator"), ensure_ascii=False)
    with pytest.raises(PlanValidationError, match="after one repair"):
        build_plan(tmp_path / "bad", responses=[bad_metric, bad_metric])


def test_report_catalog_matches_symbol_and_marks_stale_limited_reports(tmp_path) -> None:
    store = MonitoringStore(tmp_path / "catalog.sqlite3")
    sessions = SessionStore(tmp_path / "sessions")
    session = Session(
        session_id="research-1",
        title="招商银行 600036.SH 深研",
        created_at="2026-07-01T10:00:00+08:00",
        updated_at="2026-07-01T11:00:00+08:00",
        config={"research_session": {"kind": "symbol", "symbol": "600036.SH"}},
    )
    sessions.create_session(session)
    body = (
        "# 招商银行深研\n\n## 数据说明\n\n当前数据受限。\n\n"
        "## 关键点位\n\n仅保留研究证据。\n\n"
        + "证据材料。" * 150
    )
    sessions.append_message(
        Message(
            message_id="report-1",
            session_id=session.session_id,
            role="assistant",
            content=body,
            created_at="2026-07-01T11:00:00+08:00",
            metadata={"data_as_of": "2026-07-01T15:00:00+08:00"},
        )
    )
    catalog = MonitorReportCatalog(
        store=store,
        session_store=sessions,
        daily_store=DailyRunStore(tmp_path / "daily"),
        now_provider=lambda: datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    candidates = catalog.list_candidates("600036.SH")
    assert len(candidates) == 1
    assert candidates[0]["quality_status"] == "data_limited"
    assert candidates[0]["stale"] is True
    assert candidates[0]["research_reasons"] == ["report_data_limited", "report_stale"]
    assert catalog.list_candidates("000001.SZ") == []

    full = catalog.get_candidate("600036.SH", candidates[0]["report_ref"])
    assert full is not None
    frozen = catalog.freeze(full)
    assert frozen["body"] == body
    assert frozen["body_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_report_catalog_prefers_completed_penetrative_deep_report_evidence(tmp_path) -> None:
    store = MonitoringStore(tmp_path / "deep-catalog.sqlite3")
    body = "# 招商银行（600036.SH）穿透式深度研究\n\n## 核心结论\n\n证据。" + "证据。" * 300
    record = SimpleNamespace(
        report_id="report_0123456789abcdef",
        symbol="600036.SH",
        security_name="招商银行",
        status="completed",
        profile="equity_deep_research",
        quality_status="passed_with_gaps",
        revision=2,
        updated_at="2026-07-16T02:00:00+00:00",
        created_at="2026-07-16T01:00:00+00:00",
        data_as_of="2026-07-16T02:00:00+00:00",
        report_date="2026-07-16",
        generation_source="portfolio_monitor_autopilot",
        generation_reason="原报告过期",
    )

    class DeepReports:
        def list(self, *, limit: int):
            assert limit == 500
            return [record]

        def read_markdown(self, report_id: str):
            assert report_id == record.report_id
            return body

    catalog = MonitorReportCatalog(
        store=store,
        session_store=SessionStore(tmp_path / "empty-sessions"),
        daily_store=DailyRunStore(tmp_path / "empty-daily"),
        deep_report_service=DeepReports(),
        now_provider=lambda: datetime(2026, 7, 16, 3, tzinfo=timezone.utc),
    )

    candidates = catalog.list_candidates("600036.SH")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["report_type"] == "equity_deep_research"
    assert candidate["title"] == "招商银行（600036.SH）穿透式深度研究"
    assert candidate["quality_status"] == "ready"
    assert candidate["research_reasons"] == []
    assert candidate["metadata"]["generation_source"] == "portfolio_monitor_autopilot"
    assert candidate["metadata"]["generation_reason"] == "原报告过期"


def test_report_catalog_uses_unified_current_family_and_loads_etf_structural_bundle(
    tmp_path,
) -> None:
    store = MonitoringStore(tmp_path / "unified-catalog.sqlite3")
    body = "# 科创芯片ETF结构性研究\n\n" + "证据。" * 300
    bundle_path = tmp_path / "monitoring_bundle.json"
    bundle_path.write_text(
        json.dumps(
            {
                "horizon": "structural",
                "monitoring_status": "available",
                "review_due_at": "2099-07-21T09:00:00+08:00",
                "source_valid_until": "2099-07-21T09:00:00+08:00",
                "valid_until": "2099-07-21T09:00:00+08:00",
                "candidates": [{"scenario_id": "support"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    record = SimpleNamespace(
        report_id="deep_etf_1",
        symbol="588870.SH",
        security_name="科创芯片ETF",
        status="completed",
        profile="etf_deep_research",
        quality_status="passed_with_gaps",
        revision=3,
        updated_at="2026-07-20T02:00:00+00:00",
        created_at="2026-07-20T01:00:00+00:00",
        data_as_of="2026-07-18T07:00:00+00:00",
        report_date="2026-07-20",
        generation_source="manual",
        generation_reason="",
    )

    class DeepReports:
        def list(self, *, limit: int):
            return [record]

        def read_markdown(self, report_id: str):
            return body

        def artifact_path(self, report_id: str, artifact_id: str):
            assert (report_id, artifact_id) == ("deep_etf_1", "monitoring_bundle")
            return bundle_path

    class ReportLibrary:
        def subject(self, symbol: str, **kwargs):
            assert symbol == "588870.SH"
            assert kwargs == {"include_timeline": False, "history_mode": "current_families"}
            return {
                "current": {
                    "structural": {
                        "latest": {"report_id": "catalog-structural-1"},
                        "latest_complete": {"report_id": "catalog-structural-1"},
                    }
                }
            }

        def get_report(self, report_id: str):
            assert report_id == "catalog-structural-1"
            return {
                "report_id": report_id,
                "family_id": "family-588870-structural",
                "symbol": "588870.SH",
                "source_type": "deep_report",
                "source_id": "deep_etf_1",
                "report_kind": "deep_research",
                "report_quality_status": "passed_with_gaps",
                "coverage_status": "partial",
                "report_period": {"label": "结构性"},
                "viewpoints": [{"horizon": "structural"}],
                "relations": [],
            }

    catalog = MonitorReportCatalog(
        store=store,
        session_store=SessionStore(tmp_path / "empty-sessions"),
        daily_store=DailyRunStore(tmp_path / "empty-daily"),
        deep_report_service=DeepReports(),
        report_library_service=ReportLibrary(),
        now_provider=lambda: datetime(2026, 7, 20, 3, tzinfo=timezone.utc),
    )

    candidates = catalog.list_candidates("588870.SH")
    assert len(candidates) == 1
    assert candidates[0]["report_type"] == "etf_deep_research"
    assert candidates[0]["catalog_report_id"] == "catalog-structural-1"
    assert candidates[0]["catalog_family_id"] == "family-588870-structural"
    assert candidates[0]["monitoring_bundle"]["horizon"] == "structural"
    assert candidates[0]["research_reasons"] == []


def test_structural_bundle_adapter_preserves_manual_confirmation_and_lineage() -> None:
    planner = ReportDrivenMonitoringPlanner(
        market_planner=MonitoringPlanner(MarketService()),
        client=FakeClient([]),
    )
    report_snapshot = {
        "snapshot_id": "snapshot-structural",
        "report_ref": "deep-report:deep_etf_1",
        "report_type": "etf_deep_research",
        "title": "科创芯片ETF结构性研究",
        "revision": 3,
        "body_sha256": "a" * 64,
        "quality_status": "ready",
        "generated_at": "2026-07-20T02:00:00+00:00",
        "data_as_of": "2026-07-18T07:00:00+00:00",
        "metadata": {"report_period": {"label": "结构性"}},
        "monitoring_bundle": {
            "report_id": "catalog-structural-1",
            "symbol": "588870.SH",
            "horizon": "structural",
            "monitoring_status": "available",
            "activation_policy": "manual_confirmation_required",
            "trade_execution": "forbidden",
            "review_due_at": "2099-07-21T09:00:00+08:00",
            "valid_until": "2099-07-21T09:00:00+08:00",
            "candidates": [
                {
                    "scenario_id": "structural-stop",
                    "label": "结构失效位",
                    "intent": "structural_invalidation",
                    "level": {"kind": "point", "price": 1.7},
                    "source_text": "有效跌破 1.700 后结构失效",
                    "machine_expressible": True,
                    "actionability": "action_ready",
                    "volume_conditions": ["放量跌破提高置信度"],
                    "lineage": {
                        "status": "complete",
                        "claim_ids": ["claim-structural-stop"],
                        "fact_ids": ["fact-close"],
                        "evidence_ids": ["evidence-bars"],
                    },
                }
            ],
        },
    }

    plan, manifest, research = planner.build_from_structural_monitoring_bundle(
        holding={"symbol": "588870.SH"},
        report_snapshot=report_snapshot,
    )

    assert research is None
    assert plan["source_horizon"] == "structural"
    assert plan["source_report_id"] == "catalog-structural-1"
    assert plan["market_rules"][0]["enabled"] is True
    assert plan["watch_scenarios"][0]["evidence_refs"] == [
        "claim-structural-stop",
        "fact-close",
        "evidence-bars",
    ]
    assert manifest["requires_manual_activation"] is True
    assert manifest["trade_execution"] == "forbidden"


def test_autonomous_monitoring_queues_deep_report_only_with_explicit_gate_and_gap(
    tmp_path, monkeypatch,
) -> None:
    store = MonitoringStore(tmp_path / "auto-deep.sqlite3")
    executor = ThreadPoolExecutor(max_workers=1)
    submitted: list[dict] = []
    service = MonitoringService(
        store=store,
        planner_executor=executor,
        auto_deep_report_submitter=lambda payload: submitted.append(payload) or {
            "status": "queued",
            "job_id": "dispatch-1",
        },
    )
    kwargs = {
        "autonomous": True,
        "job_id": "planner-1",
        "symbol": "600036.SH",
        "holding": {"name": "招商银行"},
        "selected": {"report_type": "single_stock_research"},
        "research_reasons": ["report_stale", "report_data_limited", "report_stale"],
        "research_date": "2026-07-16",
        "trigger_type": "scheduled_refresh",
    }
    try:
        monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "1")
        monkeypatch.setenv("VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED", "1")
        result = service._maybe_queue_auto_deep_report(**kwargs)
        assert result == {"status": "queued", "job_id": "dispatch-1"}
        assert submitted == [{
            "job_id": "planner-1",
            "symbol": "600036.SH",
            "security_name": "招商银行",
            "research_reasons": ["report_stale", "report_data_limited"],
            "research_date": "2026-07-16",
            "trigger_type": "scheduled_refresh",
        }]

        refreshed = service._maybe_queue_auto_deep_report(
            **{**kwargs, "selected": {"report_type": "equity_deep_research"}},
        )
        assert refreshed == {"status": "queued", "job_id": "dispatch-1"}
        assert service._maybe_queue_auto_deep_report(
            **{**kwargs, "research_reasons": []},
        ) is None
        monkeypatch.setenv("VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED", "0")
        assert service._maybe_queue_auto_deep_report(**kwargs) is None
        assert len(submitted) == 2
    finally:
        executor.shutdown(wait=True)


def test_monitor_auto_deep_report_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("VIBE_TRADING_DEEP_REPORT_ENABLED", raising=False)
    monkeypatch.delenv("VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED", raising=False)

    assert MonitoringService._auto_deep_report_enabled() is False


def test_structural_report_refresh_is_bounded_to_one_monitor_generated_revision(
    tmp_path, monkeypatch,
) -> None:
    store = MonitoringStore(tmp_path / "structural-refresh.sqlite3")
    executor = ThreadPoolExecutor(max_workers=1)
    submitted: list[dict] = []
    service = MonitoringService(
        store=store,
        planner_executor=executor,
        auto_deep_report_submitter=lambda payload: submitted.append(payload) or {
            "status": "queued",
            "job_id": "dispatch-refresh-1",
            "session_id": "session-refresh-1",
        },
    )
    selected = {
        "report_ref": "deep-report:report-source-1",
        "report_type": "etf_deep_research",
        "source_id": "report-source-1",
        "monitoring_bundle_sha256": "b" * 64,
        "metadata": {
            "report_id": "report-source-1",
            "generation_source": "manual",
        },
    }
    kwargs = {
        "autonomous": True,
        "job_id": "planner-refresh-1",
        "symbol": "513120.SH",
        "holding": {"name": "HK创新药"},
        "selected": selected,
        "research_date": "2026-07-20",
        "trigger_type": "report_ready",
        "bundle_status": "not_recommended",
    }
    try:
        monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "1")
        monkeypatch.setenv("VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED", "1")
        result = service._maybe_queue_structural_report_refresh(**kwargs)
        assert result["status"] == "queued"
        assert submitted == [{
            "job_id": "planner-refresh-1",
            "symbol": "513120.SH",
            "security_name": "HK创新药",
            "research_reasons": [
                "structural_monitoring_not_recommended",
                "no_qualified_structural_monitoring_points",
            ],
            "research_date": "2026-07-20",
            "trigger_type": "report_ready",
            "structural_refresh": True,
            "parent_report_id": "report-source-1",
            "source_bundle_sha256": "b" * 64,
        }]

        already_refreshed = copy.deepcopy(selected)
        already_refreshed["metadata"]["generation_source"] = (
            "portfolio_monitor_structural_refresh"
        )
        skipped = service._maybe_queue_structural_report_refresh(
            **{**kwargs, "selected": already_refreshed},
        )
        assert skipped == {
            "status": "refresh_already_attempted",
            "parent_report_id": "report-source-1",
            "deduplicated": True,
        }
        assert len(submitted) == 1
    finally:
        executor.shutdown(wait=True)


def test_planner_job_cancel_recovery_and_symbol_retry_are_durable(tmp_path) -> None:
    store = MonitoringStore(tmp_path / "job-state.sqlite3")
    job = store.create_planner_job(
        symbols=["600036.SH"],
        report_refs={},
        research_policy="if_needed",
        delivery_target_id=None,
        force_fresh=True,
    )
    store.update_planner_job_status(job["job_id"], "researching")
    store.update_planner_item(job["job_id"], "600036.SH", status="researching")

    recovered = MonitoringStore(store.path)
    assert job["job_id"] in recovered.recover_planner_jobs()
    assert recovered.get_planner_job(job["job_id"])["status"] == "queued"
    cancelled = recovered.cancel_planner_job(job["job_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["items"][0]["status"] == "cancelled"
    retried = recovered.retry_planner_item(job["job_id"], "600036.SH")
    assert retried["status"] == "queued"
    assert retried["items"][0]["status"] == "queued"
    assert retried["items"][0]["attempt"] == 2


def test_watch_episode_sends_approach_and_confirmed_result_even_without_volume(tmp_path) -> None:
    store, planner, _client, _snapshot, plan, manifest, _research = build_plan(tmp_path)
    target = store.bind_target(channel="feishu", chat_id="ou_episode")
    profile_id, version = store.save_draft(
        symbol="600036.SH",
        market="SH",
        instrument_type="company_equity",
        plan=plan,
        evidence_manifest=manifest,
        input_snapshot_hash="holding-hash",
        delivery_target_id=target["target_id"],
        model_id=planner.model_id,
    )
    store.activate(profile_id, version, max_active=10)

    def quote(price: float, minute: int) -> dict:
        return {
            "last_price": price,
            "interval": "5m",
            "bar_time": f"2026-07-16T02:{minute:02d}:00+00:00",
            "session_date": "2026-07-16",
            "status": "verified",
            "sources": ["tencent", "mootdx"],
        }

    approaching = store.evaluate_quote(profile_id, quote(40.0, 0), delivery_mode="deliver")
    assert [event["kind"] for event in approaching] == ["watch_episode_approaching"]
    assert approaching[0]["phase"] == "approaching"
    assert approaching[0]["volume_verdict"] == "insufficient_evidence"

    assert store.evaluate_quote(profile_id, quote(40.25, 5), delivery_mode="deliver") == []
    confirmed = store.evaluate_quote(profile_id, quote(40.3, 10), delivery_mode="deliver")
    assert [event["kind"] for event in confirmed] == ["market_rule_trigger"]
    assert confirmed[0]["episode_id"] == approaching[0]["episode_id"]
    assert confirmed[0]["outcome"] == "confirmed_breakout"
    assert len(store.pending_deliveries()) == 2
    episode = store.list_watch_episodes(profile_id)[0]
    assert episode["state"] == "confirmed"
    assert episode["approach_notified"] is True
    assert episode["result_notified"] is True


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (1.5, "price_volume_confirmed"),
        (0.8, "low_volume_probe"),
        (1.1, "price_volume_divergence"),
        (None, "insufficient_evidence"),
    ],
)
def test_episode_volume_verdicts_are_classification_only(ratio, expected) -> None:
    scenario = {
        "volume_confirmation": {
            "metric": "same_bucket_5m_volume_ratio",
            "comparator": "gte",
            "threshold": 1.2,
            "mode": "classify_only",
        }
    }
    quote = {"price_volume": {"status": "ready", "volume_ratio": ratio}}
    verdict, _actual = MonitoringStore._episode_volume_verdict(scenario, quote)
    assert verdict == expected


def test_watch_episode_false_breakout_has_one_terminal_result(tmp_path) -> None:
    store, planner, _client, _snapshot, plan, manifest, _research = build_plan(tmp_path)
    target = store.bind_target(channel="feishu", chat_id="ou_false_breakout")
    profile_id, version = store.save_draft(
        symbol="600036.SH",
        market="SH",
        instrument_type="company_equity",
        plan=plan,
        evidence_manifest=manifest,
        input_snapshot_hash="holding-hash",
        delivery_target_id=target["target_id"],
        model_id=planner.model_id,
    )
    store.activate(profile_id, version, max_active=10)

    def evaluate(price: float, minute: int):
        return store.evaluate_quote(
            profile_id,
            {
                "last_price": price,
                "interval": "5m",
                "bar_time": f"2026-07-16T03:{minute:02d}:00+00:00",
                "session_date": "2026-07-16",
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            },
            delivery_mode="deliver",
        )

    approach = evaluate(40.0, 0)
    assert approach[0]["kind"] == "watch_episode_approaching"
    assert evaluate(40.25, 5) == []
    rejected = evaluate(40.0, 10)
    assert len(rejected) == 1
    assert rejected[0]["kind"] == "watch_episode_result"
    assert rejected[0]["phase"] == "rejected"
    assert rejected[0]["outcome"] == "false_breakout"
    assert rejected[0]["episode_id"] == approach[0]["episode_id"]
    assert len(store.list_events()) == 2


def test_5m_confirmation_episode_uses_1m_approach_without_counting_it_as_confirmation(tmp_path) -> None:
    store, planner, _client, _snapshot, plan, manifest, _research = build_plan(tmp_path)
    target = store.bind_target(channel="feishu", chat_id="ou_one_minute_approach")
    profile_id, version = store.save_draft(
        symbol="600036.SH",
        market="SH",
        instrument_type="company_equity",
        plan=plan,
        evidence_manifest=manifest,
        input_snapshot_hash="holding-hash",
        delivery_target_id=target["target_id"],
        model_id=planner.model_id,
    )
    store.activate(profile_id, version, max_active=10)

    def evaluate(price: float, interval: str, minute: int):
        return store.evaluate_quote(
            profile_id,
            {
                "last_price": price,
                "interval": interval,
                "bar_time": f"2026-07-16T02:{minute:02d}:00+00:00",
                "session_date": "2026-07-16",
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            },
            delivery_mode="deliver",
        )

    assert evaluate(39.5, "5m", 0) == []
    approaching = evaluate(40.0, "1m", 1)
    assert approaching[0]["kind"] == "watch_episode_approaching"
    assert store.list_watch_episodes(profile_id)[0]["observed_bars"] == 0
    assert evaluate(40.25, "1m", 2) == []
    assert evaluate(40.26, "1m", 3) == []
    assert store.list_watch_episodes(profile_id)[0]["observed_bars"] == 0
    assert evaluate(40.25, "5m", 5) == []
    assert store.list_watch_episodes(profile_id)[0]["observed_bars"] == 1
    confirmed = evaluate(40.3, "5m", 10)
    assert confirmed[0]["kind"] == "market_rule_trigger"
    assert confirmed[0]["episode_id"] == approaching[0]["episode_id"]


def test_same_clock_cumulative_volume_uses_matching_sources_units_and_deduplicates_bars() -> None:
    def bar(day: str, minute: int, volume: float) -> dict:
        return {
            "bar_time": f"{day}T01:{minute:02d}:00+00:00",
            "session_date": day,
            "close": 40.0,
            "volume": volume,
            "volume_unit": "share",
            "status": "verified",
            "sources": ["tencent", "mootdx"],
        }

    rows = []
    for day in ("2026-07-09", "2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15"):
        rows.extend([bar(day, 30, 100), bar(day, 35, 100)])
    rows.extend([bar("2026-07-16", 30, 100), bar("2026-07-16", 35, 200)])
    rows.append(bar("2026-07-16", 35, 200))

    class CumulativeStore:
        def query_bars(self, **_kwargs):
            return rows

    result = PriceVolumeAnalyzer().analyze_cumulative(
        market_store=CumulativeStore(),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 16, 1, 41, tzinfo=timezone.utc),
        policy={"baseline_sessions": 5, "min_samples": 5},
    )
    assert result["status"] == "ready"
    assert result["cumulative_volume"] == 300
    assert result["cumulative_volume_ratio"] == 1.5
    assert result["baseline_samples"] == 5
    assert result["volume_unit"] == "shares"


class StaticCatalog:
    def __init__(self, store: MonitoringStore, candidate: dict) -> None:
        self.store = store
        self.candidate = candidate

    def list_candidates(self, _symbol: str):
        return [{key: value for key, value in self.candidate.items() if key != "body"}]

    def choose_candidate(self, _symbol: str, _report_ref: str | None = None):
        return self.candidate, []

    def freeze(self, candidate: dict):
        return self.store.save_report_snapshot(candidate)


def test_autonomous_no_point_structural_report_uses_no_model_market_repair(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED", "1")
    update_holdings(
        holdings=[{
            "name": "HK创新药",
            "code": "513120",
            "symbol": "513120.SH",
            "quantity": 1000,
            "cost_price": 1.18,
        }]
    )
    store = MonitoringStore(tmp_path / "structural-no-points.sqlite3")
    store.set_autopilot_config({
        "enabled": True,
        "runtime_mode": "shadow",
        "selected_symbols": ["513120.SH"],
    })
    candidate = {
        "report_ref": "deep-report:report-no-points-1",
        "report_type": "etf_deep_research",
        "symbol": "513120.SH",
        "title": "513120 structural research",
        "source_id": "report-no-points-1",
        "source_message_id": None,
        "artifact_id": "markdown",
        "revision": 1,
        "body": "# Structural report\n\nNo qualified monitoring points.",
        "body_sha256": "c" * 64,
        "quality_status": "ready",
        "generated_at": "2026-07-20T01:00:00+00:00",
        "data_as_of": "2026-07-18T07:00:00+00:00",
        "metadata": {
            "report_id": "report-no-points-1",
            "generation_source": "manual",
        },
        "monitoring_bundle_sha256": "d" * 64,
        "monitoring_bundle": {
            "report_id": "report-no-points-1",
            "symbol": "513120.SH",
            "horizon": "structural",
            "monitoring_status": "not_recommended",
            "activation_policy": "manual_confirmation_required",
            "trade_execution": "forbidden",
            "review_due_at": "2099-07-21T09:00:00+08:00",
            "valid_until": "2099-07-21T09:00:00+08:00",
            "candidates": [],
        },
    }
    submitted: list[dict] = []
    market_service = MarketService()
    client = FakeClient([])
    report_planner = ReportDrivenMonitoringPlanner(
        market_planner=MonitoringPlanner(market_service),
        client=client,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    service = MonitoringService(
        store=store,
        planner=report_planner.market_planner,
        report_catalog=StaticCatalog(store, candidate),
        report_planner=report_planner,
        planner_executor=executor,
        auto_deep_report_submitter=lambda payload: submitted.append(payload) or {},
    )
    try:
        trigger, _created = store.enqueue_autopilot_trigger(
            symbol="513120.SH",
            trigger_type="report_ready",
            dedupe_key="structural-refresh-lifecycle",
        )
        job = service.create_planner_job(
            ["513120.SH"],
            activation_mode="autonomous",
            trigger_type="report_ready",
            autopilot_trigger_id=trigger["trigger_id"],
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = store.get_planner_job(job["job_id"])
            assert job is not None
            if job["status"] in {"ready", "blocked", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert job["status"] == "ready", json.dumps(job, ensure_ascii=False, default=str)
        item = job["items"][0]
        assert item["status"] == "ready"
        assert item["profile_id"]
        assert item["progress"]["stage"] == "ready"
        assert item["progress"]["activated"] is True
        assert item["progress"]["trade_execution"] == "forbidden"
        assert submitted == []
        assert client.calls == 0
        profile = store.get_profile(item["profile_id"])
        assert profile is not None
        assert profile["status"] == "active"
        active_plan = next(
            plan for plan in profile["plans"]
            if plan["version"] == profile["active_plan_version"]
        )
        assert active_plan["model_id"] == "market-analysis-methods/1.2"
        assert active_plan["evidence_manifest"]["planner_mode"] == (
            "deterministic_multi_method_market_repair"
        )
        assert active_plan["evidence_manifest"]["model_calls"] == 0
        assert len(active_plan["plan"]["market_rules"]) == 4
        scenarios = active_plan["plan"]["watch_scenarios"]
        assert all(
            any(
                condition.get("kind") in {"volume_ratio", "rolling_volume_ratio"}
                and condition.get("value") == 1.2
                for condition in scenario["confirmation_conditions"]["conditions"]
            )
            for scenario in scenarios
        )
        run = next(
            row
            for row in service.list_autopilot_runs(limit=10)
            if row["trigger_id"] == trigger["trigger_id"]
        )
        assert run["status"] == "completed"
        assert run["build_state"]["status"] == "active"
        assert run["build_state"]["stage"] == "ready"
        assert run["build_state"]["progress_percent"] == 100
        assert run["build_state"]["self_repair"] == {
            "policy": "bounded",
            "infrastructure_retry_limit": 1,
            "infrastructure_retries_used": 0,
            "agent_iteration_limit": 2,
            "agent_token_budget": 12000,
            "strategy": "continuity_then_multi_method",
            "full_report_retry_enabled": False,
            "circuit_open": False,
            "token_spend_allowed": False,
        }
    finally:
        executor.shutdown(wait=True)


def test_no_model_market_repair_blocks_with_stable_reason_when_history_is_short() -> None:
    class ShortHistoryMarketStore(MarketStore):
        def query_bars(self, *, interval: str, **kwargs):
            rows = super().query_bars(interval=interval, **kwargs)
            return rows[:10] if interval == "1D" else rows

    market_service = MarketService()
    market_service.store = ShortHistoryMarketStore()
    client = FakeClient([])
    planner = ReportDrivenMonitoringPlanner(
        market_planner=MonitoringPlanner(market_service),
        client=client,
    )
    snapshot = {
        "snapshot_id": "snapshot-short-history",
        "report_ref": "deep-report:report-short-history",
        "report_type": "etf_deep_research",
        "title": "short history structural research",
        "source_id": "report-short-history",
        "revision": 1,
        "body_sha256": "e" * 64,
        "quality_status": "ready",
        "generated_at": "2026-07-20T01:00:00+00:00",
        "data_as_of": "2026-07-18T07:00:00+00:00",
        "monitoring_bundle": {
            "report_id": "report-short-history",
            "symbol": "513120.SH",
            "horizon": "structural",
            "monitoring_status": "data_insufficient",
            "candidates": [],
        },
    }

    with pytest.raises(DeterministicMarketRepairError) as caught:
        planner.build_from_verified_market_repair(
            holding={"name": "HK创新药", "symbol": "513120.SH"},
            report_snapshot=snapshot,
            autonomous=True,
        )

    assert "deterministic_market_repair_insufficient_daily_history" in caught.value.reasons
    assert client.calls == 0


def test_runtime_observation_events_do_not_rebuild_the_monitor_profile(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    update_holdings(
        holdings=[{
            "name": "格力电器",
            "code": "000651",
            "symbol": "000651.SZ",
            "quantity": 100,
            "cost_price": 37.05,
        }]
    )
    store = MonitoringStore(tmp_path / "observation-only-events.sqlite3")
    store.set_autopilot_config({
        "enabled": True,
        "runtime_mode": "shadow",
        "selected_symbols": ["000651.SZ"],
        "trigger_types": ["approaching", "invalidated"],
    })
    executor = ThreadPoolExecutor(max_workers=1)
    service = MonitoringService(store=store, planner_executor=executor)
    ticks: list[bool] = []
    service.autopilot_tick = lambda **_kwargs: ticks.append(True) or {}
    try:
        assert service.enqueue_autopilot_event(
            trigger_type="approaching",
            symbol="000651.SZ",
            fingerprint="episode-approaching",
            payload={"event_id": "event-approaching", "phase": "approaching"},
        ) is None
        assert service.enqueue_autopilot_event(
            trigger_type="invalidated",
            symbol="000651.SZ",
            fingerprint="episode-invalidated",
            payload={"event_id": "event-invalidated", "phase": "rejected"},
        ) is None
        assert store.list_autopilot_triggers(limit=10) == []
        assert ticks == []
    finally:
        executor.shutdown(wait=True)


def test_autopilot_failure_circuit_opens_for_unchanged_input_and_resets_for_new_report(
    tmp_path,
) -> None:
    store = MonitoringStore(tmp_path / "autopilot-failure-circuit.sqlite3")
    store.set_autopilot_config({
        "enabled": True,
        "runtime_mode": "shadow",
        "selected_symbols": ["000651.SZ"],
    })
    executor = ThreadPoolExecutor(max_workers=1)
    service = MonitoringService(store=store, planner_executor=executor)
    try:
        common_payload = {
            "report_hash": "same-report-hash",
            "report_ref": "deep-report:report-same",
            "holding_hash": "same-holding-hash",
        }
        stable_progress = {
            "daily_tail_hash": "tail-a",
            "volume_signature": "volume-a",
            "adjustment_factor_revision": "factor-a",
            "method_registry_version": "1.1.0",
            "level_snapshot_id": "snapshot-a",
            "continuity": {"status": "blocked", "event_count": 1},
            "volume_gate": {"status": "ready"},
        }
        for index, trigger_type in enumerate(("holdings_changed", "report_ready"), start=1):
            trigger, _created = store.enqueue_autopilot_trigger(
                symbol="000651.SZ",
                trigger_type=trigger_type,
                dedupe_key=f"failure-{index}",
                payload=common_payload,
            )
            job = store.create_planner_job(
                symbols=["000651.SZ"],
                report_refs={"000651.SZ": common_payload["report_ref"]},
                research_policy="if_needed",
                delivery_target_id=None,
                force_fresh=True,
                activation_mode="autonomous",
                trigger_type=trigger_type,
                autopilot_trigger_id=trigger["trigger_id"],
            )
            store.update_planner_item(
                job["job_id"],
                "000651.SZ",
                status="failed",
                report_ref=common_payload["report_ref"],
                blocked_reasons=["structural_report_refresh_queue_failed"],
                progress=stable_progress,
                error="database is locked",
            )
            store.update_planner_job_status(job["job_id"], "failed")
            store.update_autopilot_trigger(
                trigger["trigger_id"],
                status="failed",
                planner_job_id=job["job_id"],
                error="planner_job_failed",
            )

        circuit = service._autopilot_circuit_state(
            "000651.SZ",
            {"payload": common_payload},
            {"progress": stable_progress},
        )
        assert circuit["open"] is True
        assert circuit["failure_count"] == 2
        assert circuit["threshold"] == 2

        changed_input = service._autopilot_circuit_state(
            "000651.SZ",
            {"payload": {**common_payload, "report_hash": "new-report-hash"}},
            {"progress": stable_progress},
        )
        assert changed_input["open"] is False
        assert changed_input["failure_count"] == 0

        changed_market = service._autopilot_circuit_state(
            "000651.SZ",
            {"payload": common_payload},
            {"progress": {**stable_progress, "daily_tail_hash": "tail-b"}},
        )
        assert changed_market["open"] is False
        assert changed_market["failure_count"] == 0
    finally:
        executor.shutdown(wait=True)


def test_reconcile_marks_legacy_no_action_without_profile_as_blocked(tmp_path) -> None:
    store = MonitoringStore(tmp_path / "legacy-no-action.sqlite3")
    store.set_autopilot_config({
        "enabled": True,
        "runtime_mode": "shadow",
        "selected_symbols": ["513120.SH"],
    })
    executor = ThreadPoolExecutor(max_workers=1)
    service = MonitoringService(store=store, planner_executor=executor)
    try:
        trigger, _created = store.enqueue_autopilot_trigger(
            symbol="513120.SH",
            trigger_type="report_ready",
            dedupe_key="legacy-no-action",
        )
        job = store.create_planner_job(
            symbols=["513120.SH"],
            report_refs={"513120.SH": "deep-report:report-legacy"},
            research_policy="if_needed",
            delivery_target_id=None,
            force_fresh=True,
            activation_mode="autonomous",
            trigger_type="report_ready",
            autopilot_trigger_id=trigger["trigger_id"],
        )
        store.update_planner_item(
            job["job_id"],
            "513120.SH",
            status="ready",
            report_ref="deep-report:report-legacy",
            progress={
                "stage": "no_action",
                "outcome": "report_has_no_qualified_monitoring_points",
                "structural_report_refresh": {
                    "status": "refresh_already_attempted",
                    "refresh_outcome": "completed",
                    "report_quality_status": "failed_validation",
                },
            },
        )
        store.update_planner_job_status(job["job_id"], "ready")
        store.update_autopilot_trigger(
            trigger["trigger_id"],
            status="completed",
            planner_job_id=job["job_id"],
        )

        assert service._reconcile_structural_report_refreshes() == 1
        repaired = store.get_planner_job(job["job_id"])
        assert repaired is not None
        assert repaired["status"] == "blocked"
        assert repaired["items"][0]["status"] == "blocked"
        assert repaired["items"][0]["blocked_reasons"] == [
            "structural_report_refresh_failed_validation"
        ]
        run = next(
            item
            for item in service.list_autopilot_runs(limit=10)
            if item["trigger_id"] == trigger["trigger_id"]
        )
        assert run["status"] == "blocked"
        assert run["build_state"]["status"] == "blocked"
        assert run["build_state"]["stage_label"] == "研究结果未通过门禁"
    finally:
        executor.shutdown(wait=True)


def test_completed_structural_refresh_requeues_planning_with_new_report(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED", "1")
    update_holdings(holdings=[{
        "name": "Gree Electric",
        "code": "000651",
        "symbol": "000651.SZ",
        "quantity": 100,
        "cost_price": 39.0,
    }])
    store = MonitoringStore(tmp_path / "structural-refresh-replan.sqlite3")
    store.set_autopilot_config({
        "enabled": True,
        "runtime_mode": "shadow",
        "selected_symbols": ["000651.SZ"],
    })
    candidate = {
        "report_ref": "deep-report:report-parent",
        "report_type": "equity_deep_research",
        "symbol": "000651.SZ",
        "source_id": "report-parent",
        "metadata": {"report_id": "report-parent", "generation_source": "manual"},
        "monitoring_bundle_sha256": "a" * 64,
        "monitoring_bundle": {
            "monitoring_status": "not_recommended",
            "candidates": [],
        },
    }
    executor = ThreadPoolExecutor(max_workers=1)
    service = MonitoringService(
        store=store,
        report_catalog=StaticCatalog(store, candidate),
        planner_executor=executor,
        auto_deep_report_submitter=lambda _payload: {
            "status": "refresh_already_attempted",
            "refresh_outcome": "completed",
            "report_id": "report-refreshed",
            "report_quality_status": "passed_with_gaps",
            "deduplicated": True,
        },
    )
    monkeypatch.setattr(service, "_autopilot_authorized", lambda _symbol: True)
    monkeypatch.setattr(service, "_holding", lambda _symbol: {
        "name": "Gree Electric",
        "code": "000651",
        "symbol": "000651.SZ",
        "quantity": 100,
        "cost_price": 39.0,
    })
    submitted_jobs: list[str] = []
    monkeypatch.setattr(service, "_submit_planner_job", submitted_jobs.append)
    try:
        trigger, _created = store.enqueue_autopilot_trigger(
            symbol="000651.SZ",
            trigger_type="report_ready",
            dedupe_key="completed-refresh-replan",
        )
        job = store.create_planner_job(
            symbols=["000651.SZ"],
            report_refs={"000651.SZ": candidate["report_ref"]},
            research_policy="if_needed",
            delivery_target_id=None,
            force_fresh=True,
            activation_mode="autonomous",
            trigger_type="report_ready",
            autopilot_trigger_id=trigger["trigger_id"],
        )
        progress = {
            "stage": "structural_report_refresh_requested",
            "structural_parent_report_id": "report-parent",
            "structural_report_type": "equity_deep_research",
            "structural_generation_source": "manual",
            "structural_source_bundle_sha256": "a" * 64,
            "structural_bundle_status": "not_recommended",
        }
        store.update_planner_item(
            job["job_id"],
            "000651.SZ",
            status="researching",
            report_ref=candidate["report_ref"],
            progress=progress,
        )
        store.update_planner_job_status(job["job_id"], "researching")
        store.update_autopilot_trigger(
            trigger["trigger_id"],
            status="running",
            planner_job_id=job["job_id"],
        )
        assert service._reconcile_structural_report_refreshes() == 1
        resumed = store.get_planner_job(job["job_id"])
        assert resumed is not None
        assert resumed["status"] == "researching"
        assert resumed["items"][0]["status"] == "queued"
        assert resumed["items"][0]["report_ref"] == "deep-report:report-refreshed"
        assert resumed["items"][0]["attempt"] == 2
        assert resumed["items"][0]["completed_at"] is None
        assert submitted_jobs == [job["job_id"]]
    finally:
        executor.shutdown(wait=True)


def test_async_planner_job_creates_pending_review_without_auto_activation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    update_holdings(
        holdings=[
            {
                "name": "招商银行",
                "code": "600036",
                "symbol": "600036.SH",
                "quantity": 1000,
                "cost_price": 38,
            }
        ]
    )
    store = MonitoringStore(tmp_path / "job.sqlite3")
    report_snapshot = snapshot(store)
    client = FakeClient([json.dumps(planner_output(), ensure_ascii=False)])
    market_service = MarketService()
    report_planner = ReportDrivenMonitoringPlanner(
        market_planner=MonitoringPlanner(market_service),
        client=client,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    service = MonitoringService(
        store=store,
        planner=report_planner.market_planner,
        report_catalog=StaticCatalog(store, report_snapshot),
        report_planner=report_planner,
        planner_executor=executor,
    )
    try:
        job = service.create_planner_job(
            ["600036.SH"],
            report_refs={"600036.SH": report_snapshot["report_ref"]},
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = store.get_planner_job(job["job_id"])
            assert job is not None
            if job["status"] in {"ready", "blocked", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert job["status"] == "ready"
        assert job["items"][0]["status"] == "ready"
        profile = store.get_profile(job["items"][0]["profile_id"])
        assert profile is not None
        assert profile["status"] == "pending_review"
        assert profile["active_plan_version"] is None
        assert profile["plans"][0]["plan"]["schema_version"] == 4
        assert market_service.refresh_calls == 1
    finally:
        executor.shutdown(wait=True)


def test_report_candidate_and_planner_job_http_contracts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio-api.json"))
    update_holdings(
        holdings=[
            {
                "name": "招商银行",
                "code": "600036",
                "symbol": "600036.SH",
                "quantity": 1000,
                "cost_price": 38,
            }
        ]
    )
    store = MonitoringStore(tmp_path / "api.sqlite3")
    report_snapshot = snapshot(store)
    client = FakeClient([json.dumps(planner_output(), ensure_ascii=False)])
    report_planner = ReportDrivenMonitoringPlanner(
        market_planner=MonitoringPlanner(MarketService()),
        client=client,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    service = MonitoringService(
        store=store,
        planner=report_planner.market_planner,
        report_catalog=StaticCatalog(store, report_snapshot),
        report_planner=report_planner,
        planner_executor=executor,
    )
    recommendation = store.save_recommendation(
        {
            "symbol": "600036.SH",
            "scenario_id": "scenario-api",
            "scenario_fingerprint": "scenario-api-fingerprint",
            "status": "ready",
            "action": "observe",
            "valid_until": "2026-07-16T15:00:00+08:00",
            "trade_execution": "forbidden",
        }
    )

    class Runtime:
        def status(self):
            return {"running": False, "mode": "off"}

    app = FastAPI()
    register_portfolio_monitor_routes(
        app,
        lambda: None,
        get_service=lambda: service,
        get_runtime=Runtime,
        set_runtime_config=lambda _enabled, mode: mode or "off",
    )
    try:
        with TestClient(app) as http:
            candidates = http.get(
                "/portfolio/monitor-report-candidates",
                params={"symbol": "600036.SH"},
            )
            assert candidates.status_code == 200
            assert candidates.json()["candidates"][0]["report_ref"] == report_snapshot["report_ref"]
            assert "body" not in candidates.json()["candidates"][0]

            created = http.post(
                "/portfolio/monitor-planner-jobs",
                json={
                    "symbols": ["600036.SH"],
                    "report_refs": {"600036.SH": report_snapshot["report_ref"]},
                    "research_policy": "if_needed",
                    "force_fresh": True,
                },
            )
            assert created.status_code == 202
            assert created.headers["location"].startswith("/portfolio/monitor-planner-jobs/")
            job_id = created.json()["job_id"]
            deadline = time.monotonic() + 5
            result = created
            while time.monotonic() < deadline:
                result = http.get(f"/portfolio/monitor-planner-jobs/{job_id}")
                if result.json()["status"] in {"ready", "blocked", "failed", "cancelled"}:
                    break
                time.sleep(0.02)
            assert result.status_code == 200
            assert result.json()["status"] == "ready"
            assert result.json()["items"][0]["plan_version"] == 1

            autopilot = http.get("/portfolio/monitoring/autopilot")
            assert autopilot.status_code == 200
            assert autopilot.json()["enabled"] is False
            assert autopilot.json()["automatic_trading"] == "forbidden"
            configured = http.put(
                "/portfolio/monitoring/autopilot",
                json={
                    "enabled": False,
                    "runtime_mode": "shadow",
                    "selected_symbols": ["600036", "600036.SH"],
                },
            )
            assert configured.status_code == 200
            assert configured.json()["runtime_mode"] == "shadow"
            assert configured.json()["selected_symbols"] == ["600036.SH"]
            assert http.get("/portfolio/monitoring/autopilot").json()["selected_symbols"] == [
                "600036.SH"
            ]
            assert http.get("/portfolio/monitoring/autopilot/runs").json() == {"runs": []}

            service.store.set_autopilot_config({
                "enabled": True,
                "runtime_mode": "shadow",
                "selected_symbols": ["600036.SH"],
            })
            stale_disable = http.put(
                "/portfolio/monitoring/autopilot",
                json={"enabled": False, "selected_symbols": []},
            )
            assert stale_disable.status_code == 409
            assert stale_disable.json()["detail"]["error_code"] == (
                "monitor_autopilot_change_source_required"
            )
            assert http.get("/portfolio/monitoring/autopilot").json()["enabled"] is True
            explicit_disable = http.put(
                "/portfolio/monitoring/autopilot",
                json={
                    "enabled": False,
                    "selected_symbols": [],
                    "change_source": "holding_selection",
                },
            )
            assert explicit_disable.status_code == 200
            assert explicit_disable.json()["enabled"] is False

            autonomous_without_consent = http.post(
                "/portfolio/monitor-planner-jobs",
                json={
                    "symbols": ["600036.SH"],
                    "activation_mode": "autonomous",
                    "research_policy": "if_needed",
                },
            )
            assert autonomous_without_consent.status_code == 400
            assert autonomous_without_consent.json()["detail"]["error_code"] == "invalid_monitor_planner_request"

            listed = http.get("/portfolio/monitor-recommendations")
            assert listed.status_code == 200
            assert listed.json()["recommendations"][0]["trade_execution"] == "forbidden"
            acknowledged = http.post(
                f"/portfolio/monitor-recommendations/{recommendation['recommendation_id']}/acknowledge",
                json={"feedback_status": "continue_observing"},
            )
            assert acknowledged.status_code == 200
            assert acknowledged.json()["feedback_status"] == "continue_observing"
    finally:
        executor.shutdown(wait=True)
