from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.portfolio.analysis_methods import METHOD_REGISTRY_VERSION
from src.portfolio.daily.reporting import (
    aggregate_portfolio,
    render_holding_markdown,
    render_master_markdown,
)
from src.portfolio.daily.contracts import BriefContractError, parse_holding_brief
from src.portfolio.daily.service import (
    DailyPortfolioRunService,
    _compact_worker_context,
    _data_status,
    _etf_share_context,
    _market_data_basis,
    _symbol_decision_scopes,
    _validate_brief_against_market_basis,
)
from src.portfolio.daily.store import DailyRunStore
from src.portfolio.mandate import default_mandate, save_mandate
from src.portfolio.state import PortfolioState


class FakeDataService:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def get_context(self, *, symbols, **kwargs):
        self.calls.append(list(symbols))
        return {
            "status": "ok",
            "symbols": list(symbols),
            "retrieved_at": "2026-07-13T09:00:00+08:00",
            "market": {
                "status": "live",
                "series": [
                    {
                        "symbol": symbol,
                        "bar_count": 1,
                        "latest": {"close": 10, "as_of": "2026-07-11T15:00:00+08:00"},
                        "actionability": "price_actionable",
                    }
                    for symbol in symbols
                ],
                "quotes": [{"symbol": symbol, "last_price": 10} for symbol in symbols],
            },
            "research": {
                "news": {
                    "status": "live",
                    "items": {
                        symbol: {
                            "mode": "live",
                            "documents": [{"title": f"{symbol} latest news"}],
                        }
                        for symbol in symbols
                    },
                }
            },
        }


class MissingDataService:
    def get_context(self, *, symbols, **kwargs):
        return {
            "status": "partial",
            "symbols": list(symbols),
            "market": {"status": "partial", "error": "live request deadline reached"},
            "research": {
                "fundamentals": {"status": "partial", "error": "live request deadline reached"},
                "news": {"status": "partial", "error": "live request deadline reached"},
                "reports": {"status": "partial", "error": "live request deadline reached"},
            },
        }


class FakeETFProductProfileService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def get_or_refresh(self, symbol: str, *, force_refresh: bool = False):
        self.calls.append((symbol, force_refresh))
        return {
            "symbol": symbol,
            "data_as_of": "2026-07-17",
            "retrieved_at": "2026-07-18T20:00:00+08:00",
            "refresh_status": "completed",
            "share_history": {
                "tracked_index_code": "000688.SH",
                "tracked_index_name": "上证科创板50成份指数",
                "current_units": 464_552_000,
                "delta_1d": 42_000_000,
                "delta_5d": 132_000_000,
                "delta_20d": 207_000_000,
                "estimated_net_flow_1d": 73_122_000,
                "estimated_net_flow_semantics": (
                    "share_delta_times_current_market_price_proxy"
                ),
            },
            "peer_group": {
                "tracked_index_code": "000688.SH",
                "tracked_index_name": "上证科创板50成份指数",
                "data_as_of": "2026-07-17",
                "member_count": 20,
                "official_index_mapping_count": 20,
                "estimated_net_flow_1d": 8_295_924_000,
                "inflow_member_ratio_1d": 0.8,
                "flow_coverage_ratio": 1.0,
                "unit_change_coverage_ratio": 1.0,
                "members": [
                    {
                        "symbol": symbol,
                        "name": "科创50ETF汇添富",
                        "current_units": 464_552_000,
                        "delta_1d": 42_000_000,
                        "current_price": 1.741,
                        "estimated_net_flow_1d": 73_122_000,
                    }
                ],
            },
        }


class ForbiddenSessionService:
    def create_session(self, **kwargs):
        raise AssertionError("data gate must stop before creating a model Session")


class MethodAwareSessionService:
    def __init__(self) -> None:
        self.prompt = ""
        self.reply = SimpleNamespace(
            role="assistant",
            content=json.dumps(
                {
                    "schema_version": 2,
                    "summary": "已使用冻结日线形成观察结论。",
                    "trend": {
                        "summary": "结构仍需继续确认。",
                        "stage": "震荡",
                        "direction": "横盘",
                        "strength": "中",
                    },
                    "action": "observe",
                    "confidence": "medium",
                    "suggested_amount": None,
                    "reasons": ["多周期结构尚未形成同向确认。"],
                    "risks": ["结构失效后需要重新评估。"],
                    "watch_points": ["等待已登记关键区间的后续反应。"],
                    "condition_order_status": "not_recommended",
                    "condition_order_summary": "当前不新增条件建议。",
                    "condition_orders": [],
                    "data_scopes": {},
                    "data_limited": False,
                    "agent_analysis": {
                        "regime_interpretation": "市场处于等待方向确认的阶段。",
                        "selected_methods": ["market_regime"],
                        "selected_level_ids": [],
                        "evidence_for": ["中期结构尚未被明显破坏。"],
                        "counter_evidence": ["短期方向与中期结构仍有分歧。"],
                        "cross_horizon_conclusion": "跨周期证据尚未完全收敛。",
                        "invalidation_conditions": ["结构方向发生反转时结论失效。"],
                        "confidence": "medium",
                        "data_gaps": [],
                        "critic": {"verdict": "pass", "issues": []},
                    },
                },
                ensure_ascii=False,
            ),
        )

    def create_session(self, **_kwargs):
        return SimpleNamespace(session_id="daily-method-session")

    async def execute_message(self, _session_id, prompt, **_kwargs):
        self.prompt = prompt

    def get_messages(self, _session_id, limit=20):
        return [self.reply]


def fake_pdf(title: str, content: str) -> bytes:
    return b"%PDF-1.4\n" + f"{title}\n{content}".encode("utf-8")


def forbidden_pdf(title: str, content: str) -> bytes:
    raise AssertionError("data gate must stop before rendering PDFs")


def test_data_status_is_partial_when_only_some_domains_or_symbols_are_limited() -> None:
    research_partial = {
        "status": "partial",
        "market": {
            "series": [
                {"symbol": "600036.SH", "actionability": "price_actionable"}
            ]
        },
    }
    mixed_market = {
        "status": "ok",
        "market": {
            "series": [
                {"symbol": "600036.SH", "actionability": "price_actionable"},
                {"symbol": "513120.SH", "actionability": "analysis_only"},
            ]
        },
    }

    assert _data_status([research_partial]) == "partial"
    assert _data_status([mixed_market]) == "partial"
    assert _data_status(
        [
            {
                "status": "partial",
                "market": {
                    "series": [
                        {"symbol": "513120.SH", "actionability": "analysis_only"}
                    ]
                },
            }
        ]
    ) == "limited"


def test_premarket_intraday_gap_does_not_mask_verified_daily_trend() -> None:
    contexts = [
        {
            "decision_scopes": {
                "588870.SH": {
                    "daily_trend": {
                        "status": "verified",
                        "actionability": "price_actionable",
                        "as_of": "2026-07-15 15:00",
                    },
                    "intraday": {
                        "status": "not_started",
                        "actionability": "analysis_only",
                        "reason": "intraday_not_started",
                    },
                    "condition_order": {
                        "status": "verified",
                        "actionability": "price_actionable",
                        "basis": "daily",
                    },
                }
            }
        }
    ]

    scopes = _symbol_decision_scopes(contexts, "588870.SH")

    assert scopes["daily_trend"]["status"] == "verified"
    assert scopes["intraday"]["status"] == "not_started"
    assert scopes["condition_order"]["basis"] == "daily"


def test_compact_worker_context_is_valid_json_without_mid_string_truncation() -> None:
    huge_documents = [
        {
            "title": f"news-{index}",
            "summary": "长摘要" * 3000,
            "data": {"payload": "大字段" * 3000},
        }
        for index in range(20)
    ]
    contexts = [
        {
            "request_id": "ctx-1",
            "status": "partial",
            "symbols": ["588870.SH"],
            "decision_scopes": {
                "588870.SH": {
                    "daily_trend": {"status": "verified", "actionability": "price_actionable"}
                }
            },
            "market": {
                "status": "live",
                "series": [
                    {
                        "symbol": "588870.SH",
                        "interval": "1D",
                        "actionability": "price_actionable",
                        "bar_count": 99,
                        "bars": [
                            {
                                "session_date": f"2026-01-{index:02d}",
                                "bar_time": f"2026-01-{index:02d}T16:00:00+00:00",
                                "open": index - 0.5,
                                "high": index + 1,
                                "low": index - 1,
                                "close": index,
                                "volume": None,
                                "observations": [
                                    {
                                        "source": "tencent",
                                        "volume": index * 1_000_000,
                                        "raw_volume": index * 10_000,
                                        "volume_unit": "share",
                                        "included_in_consensus": True,
                                        "provider_payload": "不应进入 Worker 上下文" * 1000,
                                    }
                                ],
                            }
                            for index in range(1, 100)
                        ],
                    }
                ],
                "quotes": [{"symbol": "588870.SH", "last_price": 1.93}],
            },
            "research": {
                "news": {
                    "status": "partial",
                    "items": {"588870.SH": {"mode": "live", "documents": huge_documents}},
                }
            },
        }
    ]

    payload = _compact_worker_context(contexts, "588870.SH")
    encoded = json.dumps(payload, ensure_ascii=False)

    assert json.loads(encoded)["symbol"] == "588870.SH"
    assert len(encoded) <= 28_000
    assert encoded.endswith("}")
    daily_series = payload["contexts"][0]["market"]["series"][0]
    assert len(daily_series["bars"]) >= 20
    assert daily_series["bars"][-1]["session_date"] == "2026-01-99"
    assert "observations" not in daily_series["bars"][-1]
    assert daily_series["bars"][-1]["volume_status"] == "source_evidence_only"
    assert daily_series["bars"][-1]["volume_evidence"][0]["source"] == "tencent"


def test_market_data_basis_uses_session_date_for_non_trading_day_report() -> None:
    contexts = [
        {
            "market": {
                "series": [
                    {
                        "symbol": "513120.SH",
                        "interval": "1D",
                        "bar_count": 196,
                        "latest": {
                            "session_date": "2026-07-17",
                            "bar_time": "2026-07-16T16:00:00+00:00",
                            "close": 1.128,
                        },
                    }
                ]
            }
        }
    ]

    basis = _market_data_basis(
        contexts,
        "513120.SH",
        report_market_date="2026-07-20",
        generated_at="2026-07-18T22:22:39+08:00",
    )

    assert basis["price_session_date"] == "2026-07-17"
    assert basis["price_basis"] == "previous_trading_session"
    assert basis["generation_context"] == "non_trading_day"
    assert basis["daily_bar_count"] == 196
    assert "非交易日生成" in basis["note"]
    assert "上一交易日 2026-07-17" in basis["note"]
    assert "新闻与公告采用报告生成时可得的最新信息" in basis["note"]


def test_daily_worker_receives_and_validates_frozen_method_snapshot(tmp_path: Path) -> None:
    symbol = "000651.SZ"
    session_service = MethodAwareSessionService()
    bars = []
    start = datetime(2025, 12, 1, tzinfo=timezone.utc)
    for index in range(140):
        day = start + timedelta(days=index)
        close = 35.0 + index * 0.03 + (index % 7) * 0.02
        bars.append(
            {
                "session_date": day.date().isoformat(),
                "bar_time": day.isoformat(),
                "open": close - 0.1,
                "high": close + 0.25,
                "low": close - 0.25,
                "close": close,
                "volume": 1_000_000 + index * 1_000,
            }
        )
    contexts = [
        {
            "status": "ok",
            "retrieved_at": bars[-1]["bar_time"],
            "decision_scopes": {
                symbol: {
                    "daily_trend": {
                        "status": "verified",
                        "actionability": "price_actionable",
                    },
                    "condition_order": {
                        "status": "verified",
                        "actionability": "price_actionable",
                    },
                }
            },
            "market": {
                "status": "live",
                "series": [
                    {
                        "symbol": symbol,
                        "interval": "1D",
                        "bar_count": len(bars),
                        "actionability": "price_actionable",
                        "latest": bars[-1],
                        "bars": bars,
                    }
                ],
            },
        }
    ]
    service = DailyPortfolioRunService(
        store=DailyRunStore(tmp_path / "runs"),
        session_service=session_service,
        structured_monitoring=False,
        recover_incomplete=False,
    )

    brief, session_id = asyncio.run(
        service._analyze_one(
            "daily-method-test",
            holding={"symbol": symbol, "name": "格力电器"},
            assignment={},
            contexts=contexts,
            data_status="ok",
        )
    )

    assert session_id == "daily-method-session"
    assert METHOD_REGISTRY_VERSION in session_service.prompt
    assert brief["analysis_method_snapshot"]["cutoff_policy"] == "completed_daily_bars_only"
    assert brief["agent_analysis"]["status"] == "completed"
    assert brief["agent_analysis"]["selected_methods"] == ["market_regime"]


def test_worker_cannot_claim_daily_series_missing_when_twenty_bars_are_frozen() -> None:
    basis = {"daily_bar_count": 196}

    with pytest.raises(BriefContractError, match="contradicted frozen daily-series"):
        _validate_brief_against_market_basis(
            {
                "summary": "冻结输入未提供可复核的日线序列。",
                "watch_points": ["后续需补充连续日线K线。"],
            },
            basis,
        )

    _validate_brief_against_market_basis(
        {
            "summary": "已使用上一交易日的连续日线分析。",
            "risks": ["成交量只有单一来源，量能结论需谨慎。"],
        },
        basis,
    )


def test_etf_share_profile_is_frozen_and_rendered_in_daily_report(
    tmp_path: Path,
) -> None:
    etf_profiles = FakeETFProductProfileService()
    etf_state = PortfolioState(
        holdings=[
            {
                "symbol": "588870.SH",
                "code": "588870",
                "name": "科创50指",
                "quantity": 10_000,
                "cost_price": 1.7,
                "last_price": 1.741,
                "market_value": 17_410,
            }
        ],
        cash=10_000,
    )
    service = DailyPortfolioRunService(
        store=DailyRunStore(tmp_path / "runs"),
        session_service=None,
        data_service=FakeDataService(),
        etf_product_profile_service=etf_profiles,
        pdf_renderer=fake_pdf,
        state_loader=lambda: etf_state,
        mandate_path=tmp_path / "mandate.json",
    )

    async def exercise():
        record = await service.start(market_date="2026-07-20", force_new=True)
        return await service.wait(record["run_id"])

    completed = asyncio.run(exercise())
    manifest = service.store.read_json(
        completed["run_id"], "inputs/data_manifest.json"
    )
    brief = service.store.read_json(
        completed["run_id"], "outputs/holdings/588870.SH/brief.json"
    )
    markdown_artifact = next(
        item
        for item in completed["artifacts"]
        if item["kind"] == "holding_daily_markdown"
        and item["symbol"] == "588870.SH"
    )
    markdown = Path(markdown_artifact["path"]).read_text(encoding="utf-8")
    symbol_manifest = next(
        item for item in manifest["symbols"] if item["symbol"] == "588870.SH"
    )

    assert etf_profiles.calls == [("588870.SH", False)]
    assert symbol_manifest["domains"]["etf_share"]["status"] == "available"
    assert brief["etf_share_context"]["signal"] == "net_inflow"
    assert brief["etf_share_context"]["market_scope"] == "科创板大盘成长"
    assert "ETF份额已校核" in markdown
    assert "## ETF 份额与宽基资金代理" in markdown
    assert "1日 +42,000,000 份" in markdown
    assert "估算单日净流量 +8,295,924,000 元" in markdown
    assert "份额增加成员占比 80.0%" in markdown
    assert "不等同于指数当日涨跌" in markdown


def test_etf_share_context_keeps_market_proxy_boundary() -> None:
    profile = FakeETFProductProfileService().get_or_refresh("588870.SH")
    context = _etf_share_context(
        [{"etf_product": {"588870.SH": profile}}], "588870.SH"
    )

    assert context is not None
    assert context["signal"] == "net_inflow"
    assert "科创板大盘成长" in context["interpretation"]
    assert "不等同于指数当日涨跌" in context["boundary"]


def test_limited_brief_clears_incompatible_exact_condition_orders() -> None:
    brief = parse_holding_brief(
        json.dumps(
            {
                "summary": "盘中数据尚未校核。",
                "action": "add",
                "confidence": "medium",
                "suggested_amount": 1000,
                "reasons": ["日线可见"],
                "risks": ["盘中数据缺失"],
                "watch_points": ["等待开盘"],
                "condition_order_status": "data_insufficient",
                "condition_orders": [
                    {"trigger": "1.90", "response": "加仓", "priority": "high"}
                ],
                "data_limited": True,
            },
            ensure_ascii=False,
        ),
        symbol="588870.SH",
    )

    assert brief["action"] == "observe"
    assert brief["suggested_amount"] is None
    assert brief["condition_order_status"] == "data_insufficient"
    assert brief["condition_orders"] == []


def portfolio() -> PortfolioState:
    return PortfolioState(
        holdings=[
            {
                "symbol": "600036.SH",
                "code": "600036",
                "name": "招商银行",
                "quantity": 1000,
                "cost_price": 40,
                "last_price": 42,
                "market_value": 42_000,
            },
            {
                "symbol": "688981.SH",
                "code": "688981",
                "name": "中芯国际",
                "quantity": 200,
                "cost_price": 90,
                "last_price": 100,
                "market_value": 20_000,
            },
        ],
        cash=30_000,
        updated_at="2026-07-13T08:55:00+08:00",
    )


def test_daily_run_is_idempotent_and_produces_master_plus_holding_pdfs(tmp_path: Path) -> None:
    mandate_path = tmp_path / "mandate.json"
    mandate = default_mandate()
    for sleeve in mandate["sleeves"]:
        sleeve.update({"configured": True, "target_amount": 60_000, "max_amount": 80_000})
    mandate["cash_policy"].update(
        {"configured": True, "target_amount": 20_000, "min_amount": 10_000, "max_amount": 40_000}
    )
    save_mandate(mandate, path=mandate_path, bump_version=False)
    service = DailyPortfolioRunService(
        store=DailyRunStore(tmp_path / "runs"),
        session_service=None,
        data_service=FakeDataService(),
        pdf_renderer=fake_pdf,
        state_loader=portfolio,
        mandate_path=mandate_path,
    )

    async def exercise():
        accepted = await service.start(market_date="2026-07-13")
        completed = await service.wait(accepted["run_id"])
        repeated = await service.start(market_date="2026-07-13")
        return accepted, completed, repeated

    accepted, completed, repeated = asyncio.run(exercise())

    assert completed["status"] == "completed_with_warnings"
    assert repeated["run_id"] == completed["run_id"]
    assert repeated["deduplicated"] is True
    assert len([a for a in completed["artifacts"] if a["kind"] == "master_pdf"]) == 1
    assert len([a for a in completed["artifacts"] if a["kind"] == "holding_daily_pdf"]) == 2
    assert all(Path(item["path"]).exists() for item in completed["artifacts"])
    master_pdf = next(a for a in completed["artifacts"] if a["kind"] == "master_pdf")
    assert master_pdf["filename"] == "2026-07-13_组合晨会综合报告.pdf"
    holding_pdfs = {
        item["symbol"]: item
        for item in completed["artifacts"]
        if item["kind"] == "holding_daily_pdf"
    }
    assert holding_pdfs["600036.SH"]["filename"] == (
        "2026-07-13_600036.SH_招商银行_个股晨报.pdf"
    )
    assert holding_pdfs["600036.SH"]["security_name"] == "招商银行"
    assert holding_pdfs["688981.SH"]["filename"] == (
        "2026-07-13_688981.SH_中芯国际_个股晨报.pdf"
    )
    assert "2026-07-13 招商银行（600036.SH）个股晨报" in Path(
        holding_pdfs["600036.SH"]["path"]
    ).read_bytes().decode("utf-8")
    artifact_manifest = service.store.read_json(completed["run_id"], "artifact_manifest.json")
    assert all("path" not in item and item.get("relative_path") for item in artifact_manifest["artifacts"])
    brief = service.store.read_json(completed["run_id"], "outputs/holdings/600036.SH/brief.json")
    assert brief["report_profile"] == "daily_update"
    assert brief["portfolio_context"]["market_value"] == 42_000
    assert brief["view"]["action"] == "observe"


def test_daily_run_skips_all_model_and_pdf_work_when_most_data_is_missing(
    tmp_path: Path,
) -> None:
    service = DailyPortfolioRunService(
        store=DailyRunStore(tmp_path / "runs"),
        session_service=ForbiddenSessionService(),
        data_service=MissingDataService(),
        pdf_renderer=forbidden_pdf,
        state_loader=portfolio,
        mandate_path=tmp_path / "mandate.json",
    )

    async def exercise():
        accepted = await service.start(market_date="2026-07-14")
        return await service.wait(accepted["run_id"])

    completed = asyncio.run(exercise())

    assert completed["status"] == "completed_with_warnings"
    assert completed["stage"] == "skipped_data_unavailable"
    assert completed["data_status"] == "limited"
    assert completed["analysis_gate"] == {
        "decision": "skip_report",
        "minimum_coverage_ratio": 0.5,
        "coverage_ratio": 0.0,
        "eligible_count": 0,
        "total_count": 2,
        "eligible_symbols": [],
        "missing_symbols": ["600036.SH", "688981.SH"],
        "missing_market_symbols": ["600036.SH", "688981.SH"],
        "missing_research_symbols": ["600036.SH", "688981.SH"],
        "model_sessions_started": 0,
    }
    assert completed["workers"] == []
    assert completed["artifacts"] == []
    assert completed["progress"] == {"completed": 0, "total": 2, "percent": 0}
    assert "未创建个股研究 Session，也未生成 PDF" in completed["warnings"][0]
    assert not (tmp_path / "runs" / completed["run_id"] / "outputs" / "aggregate.json").exists()


def test_aggregation_never_uses_cash_below_floor() -> None:
    mandate = default_mandate()
    mandate["cash_policy"].update({"configured": True, "target_amount": 15_000, "min_amount": 15_000})
    mandate["sleeves"][0].update({"configured": True, "target_amount": 45_000, "max_amount": 60_000})
    mandate["assignments"] = {
        "688981.SH": {
            "active_sleeve_id": "offensive",
            "assigned_by": "user",
            "confidence": 1,
            "user_locked": True,
        }
    }
    aggregate = aggregate_portfolio(
        portfolio={
            "cash": 20_000,
            "holdings": [{"symbol": "688981.SH", "market_value": 40_000}],
        },
        mandate=mandate,
        briefs=[
            {
                "symbol": "688981.SH",
                "action": "add",
                "suggested_amount": 20_000,
                "summary": "测试",
                "confidence": "high",
                "reasons": ["测试"],
            }
        ],
    )

    assert aggregate["briefs"][0]["constrained_amount"] == 5_000


def test_report_uses_chinese_sleeve_status_and_named_condition_table() -> None:
    mandate = default_mandate()
    mandate["sleeves"][0].update(
        {"configured": True, "target_amount": 20_000, "min_amount": 18_000, "max_amount": 22_000}
    )
    mandate["sleeves"][1].update(
        {"configured": True, "target_amount": 42_000, "min_amount": 40_000, "max_amount": 44_000}
    )
    mandate["assignments"] = {
        "688981.SH": {"active_sleeve_id": "offensive"},
        "600036.SH": {"active_sleeve_id": "defensive"},
    }
    portfolio_data = {
        "cash": 30_000,
        "holdings": [
            {"symbol": "688981.SH", "name": "中芯国际", "market_value": 20_000},
            {"symbol": "600036.SH", "name": "招商银行", "market_value": 42_000},
        ],
    }
    aggregate = aggregate_portfolio(
        portfolio=portfolio_data,
        mandate=mandate,
        briefs=[
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "action": "observe",
                "confidence": "medium",
                "summary": "守住成本线则继续观察。",
                "market_data_basis": {
                    "note": (
                        "本报告在非交易日生成；日线量价采用上一交易日 2026-07-11 的已收盘数据，"
                        "新闻与公告采用报告生成时可得的最新信息。"
                    )
                },
                "condition_orders": [
                    {
                        "trigger": "跌破 37.05（成本线）",
                        "response": "重新评估持仓逻辑",
                        "priority": "high",
                    },
                    {
                        "trigger": "放量突破 38.00",
                        "response": "评估加仓机会",
                        "priority": "normal",
                    },
                ],
            },
            {
                "symbol": "688981.SH",
                "name": "中芯国际",
                "action": "observe",
                "confidence": "medium",
            },
        ],
    )

    assert {item["status"] for item in aggregate["sleeves"]} == {"in_band"}
    master = render_master_markdown(
        market_date="2026-07-14",
        portfolio=portfolio_data,
        mandate=mandate,
        aggregate=aggregate,
    )
    assert "unconfigured" not in master
    assert "🟢 目标区间内" in master
    assert "| 优先级 | 标的 | 触发条件 | 建议响应 |" in master
    assert "招商银行（600036.SH）" in master
    assert "🔴 高" in master
    assert "| 🔵 常规 |  | 放量突破 38.00 | 评估加仓机会 |" in master

    holding = render_holding_markdown(
        market_date="2026-07-14",
        holding=portfolio_data["holdings"][1],
        brief=aggregate["briefs"][0],
        data_status="limited",
    )
    assert "数据状态：部分数据受限" in holding
    assert "标的：招商银行（600036.SH）" in holding
    assert "数据口径：本报告在非交易日生成" in holding
    assert "上一交易日 2026-07-11" in holding
    assert "| 优先级 | 触发条件 | 建议响应 |" in holding


def test_infeasible_targets_disable_all_quantitative_amounts() -> None:
    mandate = default_mandate()
    mandate["cash_policy"].update({"configured": True, "target_amount": 20_000, "min_amount": 10_000})
    mandate["sleeves"][0].update({"configured": True, "target_amount": 80_000, "max_amount": 100_000})
    mandate["assignments"] = {
        "688981.SH": {
            "active_sleeve_id": "offensive",
            "assigned_by": "user",
            "confidence": 1,
            "user_locked": True,
        }
    }

    aggregate = aggregate_portfolio(
        portfolio={
            "cash": 20_000,
            "holdings": [{"symbol": "688981.SH", "market_value": 40_000}],
        },
        mandate=mandate,
        briefs=[
            {
                "symbol": "688981.SH",
                "action": "add",
                "suggested_amount": 10_000,
                "confidence": "high",
            }
        ],
    )

    assert aggregate["quantitative_plan_enabled"] is False
    assert aggregate["briefs"][0]["constrained_amount"] is None
    assert aggregate["decision"]["budget_checks"][2]["passed"] is False


def test_expected_reduction_is_consumed_only_once() -> None:
    mandate = default_mandate()
    mandate["cash_policy"].update({"configured": True, "target_amount": 10_000, "min_amount": 10_000})
    mandate["sleeves"][0].update({"configured": True, "target_amount": 30_000, "max_amount": 40_000})
    mandate["sleeves"][1].update({"configured": True, "target_amount": 10_000, "min_amount": 10_000})
    mandate["assignments"] = {
        "600036.SH": {"active_sleeve_id": "defensive"},
        "688981.SH": {"active_sleeve_id": "offensive"},
        "300750.SZ": {"active_sleeve_id": "offensive"},
    }
    aggregate = aggregate_portfolio(
        portfolio={
            "cash": 10_000,
            "holdings": [
                {"symbol": "600036.SH", "market_value": 20_000},
                {"symbol": "688981.SH", "market_value": 10_000},
                {"symbol": "300750.SZ", "market_value": 10_000},
            ],
        },
        mandate=mandate,
        briefs=[
            {"symbol": "600036.SH", "action": "reduce", "suggested_amount": 10_000},
            {"symbol": "688981.SH", "action": "add", "suggested_amount": 8_000},
            {"symbol": "300750.SZ", "action": "add", "suggested_amount": 8_000},
        ],
    )

    additions = [item for item in aggregate["briefs"] if item["raw_action"] == "add"]
    assert sum(float(item.get("conditional_amount") or 0) for item in additions) == 10_000
    assert all(item.get("funded_now_amount") is None for item in additions)
    assert aggregate["decision"]["cash_summary"]["unused_expected_reduction"] == 0


def test_force_and_holding_retry_create_revisions_and_reuse_frozen_data(tmp_path: Path) -> None:
    data = FakeDataService()
    store = DailyRunStore(tmp_path / "runs")
    service = DailyPortfolioRunService(
        store=store,
        session_service=None,
        data_service=data,
        pdf_renderer=fake_pdf,
        state_loader=portfolio,
        mandate_path=tmp_path / "mandate.json",
    )

    async def exercise():
        first = await service.start(market_date="2026-07-13")
        first = await service.wait(first["run_id"])
        retry = await service.retry(first["run_id"], symbol="600036.SH")
        retry = await service.wait(retry["run_id"])
        forced = await service.start(market_date="2026-07-13", force_new=True)
        forced = await service.wait(forced["run_id"])
        return first, retry, forced

    first, retry, forced = asyncio.run(exercise())

    assert (first["revision"], retry["revision"], forced["revision"]) == (1, 2, 3)
    assert retry["parent_run_id"] == first["run_id"]
    assert retry["retry_symbol"] == "600036.SH"
    assert {item["status"] for item in retry["workers"]} == {"degraded", "reused"}
    assert store.read_json(retry["run_id"], "inputs/data_manifest.json")["reused_data_batch"] is True
    assert len(data.calls) == 2
    assert all(item["superseded"] for item in store.get(first["run_id"])["artifacts"])
    assert all(item["revision"] == 2 for item in retry["artifacts"])


def test_restart_marks_incomplete_run_interrupted(tmp_path: Path) -> None:
    store = DailyRunStore(tmp_path / "runs")
    store.create({"run_id": "dpr_interrupted", "status": "running", "stage": "holdings"})

    DailyPortfolioRunService(
        store=store,
        session_service=None,
        data_service=FakeDataService(),
        state_loader=portfolio,
        mandate_path=tmp_path / "mandate.json",
    )

    record = store.get("dpr_interrupted")
    assert record["status"] == "interrupted"
    assert "按冻结输入重试" in record["error"]


def test_twenty_six_holdings_share_one_batch_and_split_into_two_requests(tmp_path: Path) -> None:
    holdings = [
        {
            "symbol": f"{index:06d}.SZ",
            "code": f"{index:06d}",
            "name": f"测试{index}",
            "quantity": 100,
            "cost_price": 10,
            "last_price": 10,
            "market_value": 1_000,
        }
        for index in range(1, 27)
    ]
    state = PortfolioState(holdings=holdings, cash=10_000)
    data = FakeDataService()
    store = DailyRunStore(tmp_path / "runs")
    service = DailyPortfolioRunService(
        store=store,
        session_service=None,
        data_service=data,
        pdf_renderer=fake_pdf,
        state_loader=lambda: state,
        mandate_path=tmp_path / "mandate.json",
        max_workers=4,
    )

    async def exercise():
        record = await service.start(market_date="2026-07-13")
        return await service.wait(record["run_id"])

    completed = asyncio.run(exercise())
    manifest = store.read_json(completed["run_id"], "inputs/data_manifest.json")

    assert sorted(len(call) for call in data.calls) == [1, 25]
    assert manifest["data_batch_id"] == f"batch_{completed['run_id']}"
    assert len(manifest["symbols"]) == 26
    assert all(
        item["domains"]["market"]["status"] == "available"
        and item["domains"]["news"]["status"] == "available"
        and "as_of" in item["domains"]["market"]
        for item in manifest["symbols"]
    )


def test_retention_expires_files_but_keeps_run_metadata(tmp_path: Path) -> None:
    store = DailyRunStore(tmp_path / "runs")
    created_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    record = store.create({"run_id": "dpr_old", "status": "completed", "created_at": created_at})
    artifact = store.write_artifact("dpr_old", kind="master_pdf", filename="old.pdf", payload=b"%PDF-old")
    record["artifacts"] = [artifact]
    store.save(record)

    assert store.enforce_retention(keep_days=1, keep_latest=0) == 1
    expired = store.get("dpr_old")
    assert expired["artifacts"][0]["expired"] is True
    assert not Path(artifact["path"]).exists()
    assert (store.run_dir("dpr_old") / "run.json").exists()
