"""ETF universe providers, cache coordination, and P4A integration."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from src.reports import (
    AuditedETFIndexMapper,
    CSIIndexWeightProvider,
    CompositeETFIndexMapper,
    ETFUniverseFreshnessPolicy,
    ETFUniverseProvider,
    ETFUniverseProviderError,
    ETFUniverseService,
    ETFUniverseUnavailableError,
    OfficialExchangeETFIndexMapper,
    TushareIndexWeightProvider,
    build_etf_snapshot,
    execute_p4a_selection,
    make_universe_fetch_result,
)
from src.reports.etf_research import ETFResearchStore


NOW = "2026-07-18T08:00:00+00:00"
DATA_AS_OF = "2026-06-30"


class _JSONResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _szse_catalog_payload(code: str, name: str = "半导体设备ETF国泰") -> list[dict]:
    return [{
        "metadata": {"tabkey": "tab1", "subname": "2026-07-22"},
        "data": [{
            "sys_key": f"<a><u>{code}</u></a>",
            "kzjcurl": f"<a><u>{name}</u></a>",
            "nhzs": "931743 中证半导体材料设备主题指数",
            "glrmc": "国泰基金管理有限公司",
        }],
    }]


@pytest.mark.parametrize("symbol", ["159516.SZ", "159999.SZ"])
def test_official_szse_catalog_resolves_current_and_future_etfs_without_static_mapping(
    monkeypatch, symbol: str
) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    mapper = OfficialExchangeETFIndexMapper(
        http_get=lambda *args, **kwargs: _JSONResponse(_szse_catalog_payload(symbol[:6])),
        now=lambda: NOW,
    )

    mapping = mapper.resolve(symbol)

    assert mapping.etf_symbol == symbol
    assert mapping.etf_name == "半导体设备ETF国泰"
    assert mapping.tracked_index_code == "931743.CSI"
    assert mapping.tracked_index_name == "中证半导体材料设备主题指数"
    assert mapping.mapping_source == "szse_official_etf_catalog"
    assert mapping.source_url.endswith("/market/product/list/etfList/")


def test_composite_mapper_prefers_official_catalog_without_tushare_token(monkeypatch) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    official = OfficialExchangeETFIndexMapper(
        http_get=lambda *args, **kwargs: _JSONResponse(_szse_catalog_payload("159516")),
        now=lambda: NOW,
    )
    mapper = CompositeETFIndexMapper(
        audited=AuditedETFIndexMapper(()),
        official=official,
        client_factory=lambda: (_ for _ in ()).throw(AssertionError("Tushare must not be used")),
        now=lambda: NOW,
    )

    assert mapper.supports("159516.SZ") is True
    assert mapper.resolve("159516.SZ").tracked_index_code == "931743.CSI"


def _components(weights: list[float], *, percent: bool = True) -> list[dict]:
    return [
        {
            "symbol": f"{index + 1:06d}.SZ",
            "name": f"成分{index + 1}",
            "weight": weight if percent else weight / 100.0,
        }
        for index, weight in enumerate(weights)
    ]


def _weighted_components(count: int, head: list[float]) -> list[dict]:
    tail_count = count - len(head)
    remaining = max(0.0, 100.0 - sum(head))
    tail_weight = remaining / tail_count if tail_count else 0.0
    return _components([*head, *([tail_weight] * tail_count)])


def _result(
    symbol: str = "510300.SH",
    *,
    components: list[dict] | None = None,
    expected_count: int | None = None,
    complete: bool = True,
    partial_top: bool = False,
    provider_id: str = "fake_official",
):
    rows = components or _components([60.0, 40.0])
    return make_universe_fetch_result(
        etf_symbol=symbol,
        etf_name="测试ETF",
        tracked_index_code="000300.SH",
        tracked_index_name="测试指数",
        provider_id=provider_id,
        source_type="official_index_weight" if complete else "quarterly_fund_holdings",
        source_ids=[f"source:{provider_id}"],
        source_urls=["https://example.test/source"],
        data_as_of=DATA_AS_OF,
        components=rows,
        expected_component_count=expected_count or len(rows),
        universe_complete=complete,
        partial_components_are_top_ranked=partial_top,
        weight_scale="percent",
        mapping={"mapping_source": "test", "confidence": "structured"},
        retrieved_at=NOW,
    )


class _Provider(ETFUniverseProvider):
    source_priority = 10
    freshness_policy = ETFUniverseFreshnessPolicy("monthly", 45)

    def __init__(self, factory, *, provider_id: str = "fake_official", delay: float = 0.0):
        self.provider_id = provider_id
        self.factory = factory
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def supports(self, etf_symbol: str) -> bool:
        return True

    def fetch(self, etf_symbol: str, as_of: str | None = None):
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        value = self.factory(etf_symbol, as_of)
        if isinstance(value, BaseException):
            raise value
        return value


def test_fetch_result_separates_field_and_weight_coverage_and_normalizes_scales() -> None:
    percent = _result(components=_components([60.0, 40.0]))
    fraction = make_universe_fetch_result(
        etf_symbol="510300.SH",
        etf_name="测试ETF",
        tracked_index_code="000300.SH",
        tracked_index_name="测试指数",
        provider_id="fraction_provider",
        source_type="official_index_weight",
        source_ids=["source:fraction"],
        source_urls=[],
        data_as_of=DATA_AS_OF,
        components=_components([60.0, 40.0], percent=False),
        expected_component_count=2,
        weight_scale="fraction",
    )

    assert percent.quality == "complete"
    assert fraction.quality == "complete"
    assert percent.observed_weight_coverage == 1.0
    assert fraction.observed_weight_coverage == 1.0
    assert percent.required_field_coverage == 1.0
    assert [item["weight"] for item in percent.components] == [0.6, 0.4]
    assert [item["weight"] for item in fraction.components] == [0.6, 0.4]


def test_duplicate_components_merge_but_abnormal_weight_sum_is_insufficient() -> None:
    duplicate = _result(
        components=[
            {"symbol": "000001.SZ", "name": "A", "weight": 30},
            {"symbol": "000001.SZ", "name": "A", "weight": 30},
            {"symbol": "000002.SZ", "name": "B", "weight": 40},
        ],
        expected_count=2,
    )
    abnormal = _result(components=_components([80.0, 40.0]), expected_count=2)

    assert duplicate.quality == "complete"
    assert duplicate.observed_component_count == 2
    assert "duplicate_component_symbols_merged" in duplicate.warnings
    assert abnormal.quality == "insufficient"
    assert "component_weight_sum_above_105pct" in abnormal.warnings


def test_top_ranked_partial_at_22pct_enters_p4a_but_random_partial_fails(tmp_path) -> None:
    store = ETFResearchStore(tmp_path / "research.sqlite3")
    rows = _components([3.0, 2.8, 2.6, 2.4, 2.2, 2.0, 1.9, 1.8, 1.7, 1.6])
    top_ranked = _result(
        components=rows,
        expected_count=300,
        complete=False,
        partial_top=True,
        provider_id="quarterly_top10",
    )
    random_partial = _result(
        components=rows,
        expected_count=300,
        complete=False,
        partial_top=False,
        provider_id="random_partial",
    )
    assert top_ranked.observed_weight_coverage == pytest.approx(0.22)
    assert top_ranked.required_field_coverage == 1.0
    assert top_ranked.quality == "partial"
    assert random_partial.quality == "insufficient"

    usable_snapshot = build_etf_snapshot(
        symbol="510300.SH",
        snapshot_type="universe",
        data_as_of=top_ranked.data_as_of,
        payload=top_ranked.to_snapshot_payload(),
        coverage_ratio=top_ranked.required_field_coverage,
        source_ids=top_ranked.source_ids,
    )
    rejected_snapshot = build_etf_snapshot(
        symbol="510300.SH",
        snapshot_type="universe",
        data_as_of=random_partial.data_as_of,
        payload=random_partial.to_snapshot_payload(),
        coverage_ratio=random_partial.required_field_coverage,
        source_ids=random_partial.source_ids,
    )
    assert usable_snapshot.quality_status == "passed_with_gaps"
    selection, _hit = execute_p4a_selection(store=store, universe_snapshot=usable_snapshot)
    assert selection.quality == "partial"
    assert len(selection.selected) == 2
    assert rejected_snapshot.quality_status == "failed_validation"
    with pytest.raises(ValueError, match="failed or stale"):
        execute_p4a_selection(store=store, universe_snapshot=rejected_snapshot)


def test_csi_official_provider_parses_structured_weights_and_audited_mapping() -> None:
    class _Response:
        content = b"binary-xls-content"

        @staticmethod
        def raise_for_status() -> None:
            return None

    provider = CSIIndexWeightProvider(
        http_get=lambda *_args, **_kwargs: _Response(),
        parser=lambda _content: [
            {
                "data_as_of": "20260630",
                "index_code": "000300",
                "index_name": "沪深300",
                "symbol": "000001",
                "name": "平安银行",
                "exchange": "深圳证券交易所",
                "weight": "60",
            },
            {
                "data_as_of": "20260630",
                "index_code": "000300",
                "index_name": "沪深300",
                "symbol": "600000",
                "name": "浦发银行",
                "exchange": "上海证券交易所",
                "weight": "40",
            },
        ],
    )
    result = provider.fetch("510300.SH")

    assert result.provider_id == "csi_official_close_weight"
    assert result.source_type == "official_index_weight"
    assert result.tracked_index_code == "000300.SH"
    assert result.data_as_of == "2026-06-30T00:00:00+00:00"
    assert result.quality == "complete"
    assert [item["symbol"] for item in result.components] == ["000001.SZ", "600000.SH"]
    assert result.mapping["mapping_source"] == "exchange_fund_disclosure"


def test_csi_official_provider_retries_one_transport_failure() -> None:
    class _Response:
        content = b"binary-xls-content"

        @staticmethod
        def raise_for_status() -> None:
            return None

    calls = 0

    def flaky_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.ConnectionError("temporary disconnect")
        return _Response()

    provider = CSIIndexWeightProvider(
        http_get=flaky_get,
        parser=lambda _content: [
            {
                "data_as_of": "20260630",
                "index_code": "000300",
                "index_name": "沪深300",
                "symbol": "600000",
                "name": "浦发银行",
                "exchange": "上海证券交易所",
                "weight": "100",
            }
        ],
    )
    result = provider.fetch("510300.SH")

    assert calls == 2
    assert result.quality == "complete"
    assert "official_source_transport_retry_succeeded" in result.warnings


def test_tushare_permission_error_is_explicit_and_never_silent() -> None:
    class _DeniedClient:
        @staticmethod
        def index_weight(**_kwargs):
            raise RuntimeError("抱歉，您没有接口访问权限，积分不足")

    provider = TushareIndexWeightProvider(client_factory=lambda: _DeniedClient())
    with pytest.raises(ETFUniverseProviderError) as raised:
        provider.fetch("510300.SH", as_of=NOW)
    assert raised.value.code == "permission_denied"
    assert "index_weight" in str(raised.value)


def test_service_falls_back_after_provider_failure_and_audits_reason(tmp_path) -> None:
    failed = _Provider(
        lambda _symbol, _as_of: ETFUniverseProviderError(
            "permission_denied", "Tushare permission denied"
        ),
        provider_id="tushare_index_weight",
    )
    failed.source_priority = 10
    good = _Provider(lambda symbol, _as_of: _result(symbol, provider_id="quarterly_top10"))
    good.provider_id = "quarterly_top10"
    good.source_priority = 40
    service = ETFUniverseService(
        store=ETFResearchStore(tmp_path / "research.sqlite3"),
        providers=[failed, good],
        now=lambda: NOW,
    )

    result = service.get_or_refresh("510300.SH")

    assert result.provider_id == "quarterly_top10"
    assert result.fallback_used is True
    assert result.attempts[0]["error_code"] == "permission_denied"
    assert result.attempts[-1]["status"] == "success"
    assert "provider_fallback_used" in result.warnings


def test_cache_reuse_force_refresh_and_changed_content_snapshot_ids(tmp_path) -> None:
    state = {"weights": [60.0, 40.0]}
    provider = _Provider(
        lambda symbol, _as_of: _result(symbol, components=_components(state["weights"]))
    )
    store = ETFResearchStore(tmp_path / "research.sqlite3")
    service = ETFUniverseService(store=store, providers=[provider], now=lambda: NOW)

    first = service.get_or_refresh("510300.SH")
    repeated = service.get_or_refresh("510300.SH")
    forced_same = service.get_or_refresh("510300.SH", force_refresh=True)
    state["weights"] = [55.0, 45.0]
    changed = service.get_or_refresh("510300.SH", force_refresh=True)

    assert provider.calls == 3
    assert first.cache_hit is False
    assert repeated.cache_hit is True
    assert forced_same.snapshot_reused is True
    assert forced_same.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert changed.snapshot_reused is False
    assert changed.snapshot.snapshot_id != first.snapshot.snapshot_id


def test_concurrent_force_refresh_is_single_flight(tmp_path) -> None:
    provider = _Provider(lambda symbol, _as_of: _result(symbol), delay=0.08)
    service = ETFUniverseService(
        store=ETFResearchStore(tmp_path / "research.sqlite3"),
        providers=[provider],
        now=lambda: NOW,
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _index: service.get_or_refresh("510300.SH", force_refresh=True),
            range(8),
        ))

    assert provider.calls == 1
    assert len({item.snapshot.snapshot_id for item in results}) == 1
    assert sum(item.coalesced for item in results) == 7


def test_valid_cache_fallback_is_allowed_but_expired_cache_is_rejected(tmp_path) -> None:
    store = ETFResearchStore(tmp_path / "research.sqlite3")
    seed = _Provider(lambda symbol, _as_of: _result(symbol))
    service = ETFUniverseService(store=store, providers=[seed], now=lambda: NOW)
    first = service.get_or_refresh("510300.SH")

    failure = _Provider(
        lambda _symbol, _as_of: ETFUniverseProviderError("network_error", "offline"),
        provider_id="failed",
    )
    service.providers = [failure]
    fallback = service.get_or_refresh("510300.SH", force_refresh=True)
    assert fallback.cache_fallback is True
    assert fallback.snapshot.snapshot_id == first.snapshot.snapshot_id

    expired = ETFUniverseService(
        store=store,
        providers=[failure],
        now=lambda: "2026-10-01T00:00:00+00:00",
    )
    with pytest.raises(ETFUniverseUnavailableError):
        expired.get_or_refresh("510300.SH", force_refresh=True)


def test_event_symbol_can_force_one_csi1000_component_without_model_calls(tmp_path) -> None:
    rows = _components([0.1] * 1000)
    provider = _Provider(
        lambda symbol, _as_of: _result(
            symbol,
            components=rows,
            expected_count=1000,
        )
    )
    service = ETFUniverseService(
        store=ETFResearchStore(tmp_path / "research.sqlite3"),
        providers=[provider],
        now=lambda: NOW,
    )

    normal = service.get_or_refresh("560010.SH")
    event = service.get_or_refresh("560010.SH", event_symbols=["000777.SZ"])

    assert normal.selection.selected == []
    assert [item.symbol for item in event.selection.selected] == ["000777.SZ"]
    assert event.selection.selected[0].forced is True
    assert event.to_dict()["model_calls"] == 0
    assert event.to_dict()["input_tokens"] == 0
    assert event.to_dict()["output_tokens"] == 0


@pytest.mark.parametrize(
    ("symbol", "count", "head", "expected_selected"),
    [
        ("588870.SH", 50, [12.0, 10.0, 9.0, 8.0, 7.0], 5),
        ("510300.SH", 300, [4.5, 3.5, 3.0], 2),
        ("560010.SH", 1000, [], 0),
        ("513120.SH", 42, [12.0, 11.0, 10.0, 9.0, 8.0], 5),
        ("516010.SH", 28, [12.0, 11.0, 10.0, 9.0, 8.0], 5),
    ],
)
def test_first_batch_p4a_profiles_are_bounded_and_model_free(
    tmp_path, symbol: str, count: int, head: list[float], expected_selected: int
) -> None:
    rows = _weighted_components(count, head)
    fetched = _result(symbol, components=rows, expected_count=count)
    snapshot = build_etf_snapshot(
        symbol=symbol,
        snapshot_type="universe",
        data_as_of=fetched.data_as_of,
        payload=fetched.to_snapshot_payload(),
        coverage_ratio=fetched.required_field_coverage,
        source_ids=fetched.source_ids,
    )
    store = ETFResearchStore(tmp_path / f"{symbol}.sqlite3")
    selection, _hit = execute_p4a_selection(store=store, universe_snapshot=snapshot)

    assert len(selection.selected) == expected_selected
    assert len(selection.selected) <= 5
    metrics = store.baseline_metrics(symbol)
    assert metrics["model_runs"] == 0
    assert metrics["input_tokens"] == 0
    assert metrics["output_tokens"] == 0


@pytest.mark.skipif(
    os.getenv("VIBE_TRADING_RUN_LIVE_ETF_PROVIDER") != "1",
    reason="live provider verification is opt-in",
)
@pytest.mark.parametrize(
    ("symbol", "minimum_count"),
    [
        ("588870.SH", 45),
        ("510300.SH", 250),
        ("560010.SH", 900),
        ("513120.SH", 30),
        ("516010.SH", 20),
    ],
)
def test_live_csi_provider_opt_in(symbol: str, minimum_count: int) -> None:
    result = CSIIndexWeightProvider().fetch(symbol)
    assert result.quality == "complete"
    assert result.observed_component_count >= minimum_count
    assert result.observed_weight_coverage >= 0.95
