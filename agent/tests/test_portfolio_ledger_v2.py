from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.loop import AgentLoop
from src.data_layer.service import UnifiedDataService
from src.portfolio.answer_guard import find_portfolio_answer_conflict
from src.portfolio.daily.service import _compact_market_bar, _validate_brief_against_market_basis
from src.portfolio.state import (
    commit_attempt_mutations,
    commit_reconciliation,
    discard_attempt_mutations,
    load_state,
    preview_reconciliation,
    record_trade,
    update_holdings,
)
from src.session.events import EventBus
from src.session.models import Attempt, AttemptStatus, Session
from src.session.service import SessionService
from src.session.store import SessionStore
from src.tools.market_rules_tool import MarketRulesTool
from src.research.turn_context import build_research_turn_context, update_proxy_authorizations


REPLAY_FIXTURE = Path(__file__).parent / "fixtures" / "portfolio_session_regressions_36h.json"


def _replay_scenarios() -> dict[str, dict[str, object]]:
    payload = json.loads(REPLAY_FIXTURE.read_text(encoding="utf-8"))
    assert payload["sanitized"] is True
    assert payload["source_audit"] == {
        "session_count": 18,
        "message_count": 203,
        "symbol_mentions": {"588870": 47, "588000": 19, "588100": 0},
    }
    return payload["scenarios"]


def _seed_588870(path: Path, *, quantity: float = 5000, cost: float = 2.1) -> None:
    update_holdings(
        raw_text=f"科创50ETF汇添富 588870 {quantity:g} {cost:g}",
        path=path,
    )


def _sell(quantity: float, *, trade_id: str | None = None) -> dict[str, object]:
    return {
        **({"trade_id": trade_id} if trade_id else {}),
        "code": "588870",
        "symbol": "588870.SH",
        "name": "科创50ETF汇添富",
        "side": "sell",
        "quantity": quantity,
        "price": 1.856,
    }


def test_cancelled_attempt_never_changes_authoritative_holdings(tmp_path: Path) -> None:
    scenarios = _replay_scenarios()
    replay = scenarios["cancelled_sale"]
    followup = scenarios["invalid_followup_sale"]
    path = tmp_path / "portfolio_state.json"
    _seed_588870(path, quantity=float(replay["initial_quantity"]))
    before = load_state(path)

    preview = record_trade(
        trade=_sell(float(replay["quantity"])),
        path=path,
        attempt_id="attempt-cancelled",
        expected_revision=before.revision,
        idempotency_key="attempt-cancelled:call-1",
    )

    assert preview.holdings[0]["quantity"] == 9000
    assert load_state(path).holdings[0]["quantity"] == replay["expected_quantity"]
    assert discard_attempt_mutations("attempt-cancelled", path) == 1
    assert load_state(path).holdings[0]["quantity"] == replay["expected_quantity"]
    followup_preview = record_trade(
        trade=_sell(float(followup["quantity"])),
        path=path,
        attempt_id="attempt-corrected",
        expected_revision=before.revision,
        idempotency_key="attempt-corrected:call-1",
    )
    assert followup_preview.holdings[0]["quantity"] == 6300
    assert discard_attempt_mutations("attempt-corrected", path) == 1
    final = load_state(path)
    assert final.holdings[0]["quantity"] == replay["expected_quantity"]
    assert len(final.recent_trades) == followup["expected_committed_events"]


def test_pending_commit_is_atomic_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_state.json"
    _seed_588870(path, quantity=10_000)
    revision = load_state(path).revision
    first = record_trade(
        trade=_sell(1000), path=path, attempt_id="attempt-ok",
        expected_revision=revision, idempotency_key="attempt-ok:call-1",
    )
    duplicate = record_trade(
        trade=_sell(1000), path=path, attempt_id="attempt-ok",
        expected_revision=revision, idempotency_key="attempt-ok:call-1",
    )
    assert first.holdings[0]["quantity"] == 9000
    assert duplicate.holdings[0]["quantity"] == 9000

    committed = commit_attempt_mutations("attempt-ok", path)
    assert committed.holdings[0]["quantity"] == 9000
    assert committed.revision == revision + 1
    again = commit_attempt_mutations("attempt-ok", path)
    assert again.revision == committed.revision


def test_broker_reported_pnl_wins_without_hiding_fifo_difference(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_state.json"
    record_trade(
        path=path,
        trade={
            "code": "600036", "symbol": "600036.SH", "name": "招商银行",
            "side": "buy", "quantity": 100, "price": 37.48, "fees": 5.04,
        },
    )
    record_trade(
        path=path,
        trade={
            "code": "600036", "symbol": "600036.SH", "name": "招商银行",
            "side": "buy", "quantity": 100, "price": 37.62, "fees": 5.04,
        },
    )
    state = record_trade(
        path=path,
        trade={
            "code": "600036", "symbol": "600036.SH", "name": "招商银行",
            "side": "sell", "quantity": 200, "price": 38.0,
            "fees": 4.45, "broker_reported_pnl": 271.64,
        },
    )

    sale = state.recent_trades[0]
    assert sale["exactness"] == "broker_reported"
    assert sale["realized_pnl_net"] == 271.64
    assert sale["calculated_realized_pnl_net"] != 271.64
    assert sale["unexplained_difference"] == pytest.approx(
        271.64 - sale["calculated_realized_pnl_net"]
    )
    assert state.performance["broker_reported_pnl"] == 271.64
    assert state.performance["status"] == "broker_reported"


def test_reconciliation_preview_flags_known_cancelled_candidate(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_state.json"
    _seed_588870(path)
    record_trade(trade=_sell(100, trade_id="553a08a-cancelled"), path=path)

    record = preview_reconciliation(
        raw_text="科创50ETF汇添富 588870 5000 2.100",
        broker_reported_pnl=271.64,
        source_label="broker-test",
        path=path,
    )

    assert record["preview"]["requires_explicit_commit"] is True
    assert record["preview"]["suspicious_events"][0]["event_id"].startswith("553a08a")
    assert load_state(path).holdings[0]["quantity"] == 4900


def test_legacy_json_remains_authoritative_until_explicit_reconciliation(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_state.json"
    path.write_text(
        json.dumps(
            {
                "holdings": [
                    {
                        "name": "科创50ETF汇添富",
                        "code": "588870",
                        "symbol": "588870.SH",
                        "quantity": 10_500,
                        "cost_price": 1.938,
                    }
                ],
                "cash": 100.0,
                "cash_currency": "CNY",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    legacy = load_state(path)
    assert legacy.provenance["authoritative_store"] == "legacy_json"
    assert legacy.provenance["requires_reconciliation_commit"] is True
    assert legacy.provenance["parallel_validation"]["status"] == "match"

    preview = preview_reconciliation(
        raw_text="科创50ETF汇添富 588870 13700 2.100",
        source_label="broker-confirmed",
        path=path,
    )
    assert load_state(path).holdings[0]["quantity"] == 10_500

    result = commit_reconciliation(
        preview["reconciliation_id"],
        expected_revision=preview["base_revision"],
        path=path,
    )
    assert result["state"]["holdings"][0]["quantity"] == 13_700
    assert result["state"]["provenance"]["authoritative_store"] == "sqlite"
    assert result["state"]["provenance"]["requires_reconciliation_commit"] is False


def test_agent_scope_blocks_proxy_price_but_allows_authorized_proxy_flow() -> None:
    replay = _replay_scenarios()["symbol_scope"]
    context = {
        "primary_symbol": replay["primary_symbol"],
        "allowed_data_symbols": ["588870.SH"],
        "proxy_relations": [{"symbol": replay["proxy_symbol"], "role": replay["proxy_role"]}],
        "portfolio_revision": 3,
        "live_required": True,
    }
    agent = AgentLoop(
        registry=SimpleNamespace(),
        llm=SimpleNamespace(),
        attempt_id="attempt-1",
        research_turn_context=context,
    )
    price_call = SimpleNamespace(
        id="price", name="get_data_context",
        arguments={"symbols": [replay["proxy_symbol"]], "include": replay["blocked_include"]},
    )
    flow_call = SimpleNamespace(
        id="flow", name="get_fund_flow", arguments={"codes": ["588000.SH"]},
    )

    violation = agent._research_scope_violation(price_call)
    assert violation and violation["error_code"] == "research_symbol_scope_violation"
    assert agent._research_scope_violation(flow_call) is None
    flow_args = agent._prepare_tool_args(flow_call)
    assert flow_args["_subject_symbol"] == "588870.SH"
    assert flow_args["_relationship"] == "fund_flow_reference"


def test_explicit_proxy_consent_is_persisted_without_price_authority(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio_state.json"))
    config = {"symbol": "588870.SH", "security_name": "科创50ETF汇添富"}
    changed = update_proxy_authorizations(
        config,
        [("message-1", "588000 的资金流可以作为参考，但我买的是 588870。")],
    )
    turn = build_research_turn_context(
        {"research_session": config},
        "下午现在怎么操作？",
    )

    assert changed is True
    assert config["proxy_relations"] == [{
        "symbol": "588000.SH",
        "role": "fund_flow_reference",
        "authorized_by_message_id": "message-1",
    }]
    assert turn.primary_symbol == "588870.SH"
    assert turn.allowed_data_symbols == ["588870.SH"]
    assert turn.live_required is True


def test_answer_guard_rejects_actor_attribution_and_missing_live_timestamp() -> None:
    messages = [{"role": "tool", "name": "get_fund_flow", "content": "{}"}]
    context = {"primary_symbol": "588870.SH", "live_required": True}
    actor = find_portfolio_answer_conflict(messages, "主力正在清仓，散户接盘。", context)
    assert actor and actor.conflict_code == "unsupported_fund_flow_actor_attribution"
    stale = find_portfolio_answer_conflict(
        [], "当前价格是 1.800 元。", context
    )
    assert stale and stale.conflict_code == "exact_live_claim_missing_as_of"
    assert find_portfolio_answer_conflict(
        [], "截至 14:30，当前价格是 1.800 元。", context
    ) is None


def test_daily_volume_preserves_units_and_rejects_100x_hand_error() -> None:
    replay = _replay_scenarios()["volume_units"]
    compact = _compact_market_bar({
        "volume": replay["shares"],
        "observations": [{
            "volume": 125_119_900,
            "raw_volume": 1_251_199,
            "volume_unit": "share",
            "included_in_consensus": True,
        }],
    })
    assert compact["volume_unit"] == "share"
    assert compact["raw_volume"] == 1_251_199
    assert compact["volume_display"]["yi_shares"] == pytest.approx(replay["yi_shares"])
    assert compact["volume_display"]["wan_lots"] == pytest.approx(replay["wan_lots"])
    with pytest.raises(Exception, match="volume unit"):
        _validate_brief_against_market_basis(
            {"summary": "成交量达到 1.25 亿手"},
            {"daily_bar_count": 20, "volume": 125_119_900, "volume_unit": "share"},
        )


def test_versioned_market_rules_never_guess_broker_commission() -> None:
    payload = json.loads(MarketRulesTool().execute())
    assert payload["rules"]["stamp_duty"]["rate"] == 0.0005
    assert payload["rules"]["dividend_tax_fifo"]["official_url"].startswith("https://")
    assert payload["rules"]["broker_commission"]["exact_value_policy"] == "unavailable_without_broker_evidence"


def test_session_replay_keeps_flow_metrics_and_broker_pnl_independent() -> None:
    scenarios = _replay_scenarios()
    flow = scenarios["flow_metric_split"]
    assert flow["secondary_market_order_size_net_flow_cny"] == -10_560_000_000
    assert flow["etf_estimated_subscription_flow_cny"] == 13_700_000_000
    assert flow["actor_attribution_supported"] is False

    broker = scenarios["broker_pnl"]
    assert broker["symbol"] == "600036.SH"
    assert broker["broker_reported_net_pnl"] == 271.64
    assert broker["unexplained_difference_policy"] == "show_unexplained_never_guess"


def test_etf_share_flow_context_is_distinct_from_order_flow(monkeypatch) -> None:
    profile = {
        "symbol": "588870.SH",
        "quality_status": "passed",
        "data_as_of": "2026-07-21",
        "retrieved_at": "2026-07-22T00:00:00+00:00",
        "share_history": {
            "current_units": 1_000_000,
            "delta_1d": 100_000,
            "estimated_net_flow_1d": 137_000_000,
            "estimated_net_flow_semantics": "share_delta_times_exchange_price_proxy",
        },
        "peer_group": {
            "tracked_index_code": "000688.SH",
            "member_count": 5,
            "estimated_net_flow_1d": 200_000_000,
            "unit_change_coverage_ratio": 0.8,
        },
        "sources": [{"source_id": "sse.units"}],
    }
    fake = SimpleNamespace(get_or_refresh=lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(
        "src.reports.etf_product_profile.get_etf_product_profile_service",
        lambda: fake,
    )

    result = UnifiedDataService._etf_product_context(
        ["588870.SH"], False, 0.0, SimpleNamespace(is_set=lambda: False)
    )
    item = result["items"]["588870.SH"]
    assert item["metric_family"] == "etf_fund_unit_change_estimated_flow"
    assert item["estimated_net_flow_1d"] == 137_000_000
    assert "not turnover" in item["disclaimer"]


def test_session_cancel_discards_staged_portfolio_write(tmp_path: Path, monkeypatch) -> None:
    portfolio_path = tmp_path / "portfolio_state.json"
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(portfolio_path))
    _seed_588870(portfolio_path)
    store = SessionStore(tmp_path / "sessions")
    service = SessionService(store=store, event_bus=EventBus(), runs_dir=tmp_path / "runs")
    session = Session(session_id="session-cancel", title="cancel")
    store.create_session(session)
    attempt = Attempt(
        attempt_id="attempt-session-cancel",
        session_id=session.session_id,
        prompt="卖出 4700 股后又取消",
        metadata={"response_mode": "chat"},
    )
    store.create_attempt(attempt)

    async def fake_run(_attempt: Attempt, **_kwargs):
        record_trade(
            trade=_sell(4700),
            attempt_id=_attempt.attempt_id,
            idempotency_key=f"{_attempt.attempt_id}:call-1",
        )
        return {"status": "cancelled", "reason": "cancelled by user", "react_trace": []}

    monkeypatch.setattr(service, "_run_with_agent", fake_run)
    asyncio.run(service._run_attempt(session, attempt))

    assert load_state(portfolio_path).holdings[0]["quantity"] == 5000
    assert store.get_attempt(session.session_id, attempt.attempt_id).status == AttemptStatus.CANCELLED
