from src.reports.etf_valuation_percentile import (
    BaifenweiIndexValuationProvider,
    ETFValuationPercentileService,
    parse_index_valuation_rows,
)
from src.research.knowledge import ResearchKnowledgeStore


INDEX_PAGE = """
<table data-valuation-sort>
  <tbody>
    <tr class="tone-hot-strong" id="idx-000688" data-category="broad">
      <td class="index-name"><a href="/index/kc50/"><strong>科创50</strong></a></td>
      <td class="date-cell" data-v="2026-07-17">2026-07-17</td>
      <td class="num" data-v="209.9431">209.94</td>
      <td class="pct-cell" data-v="98.5517">98.6%</td>
      <td class="num" data-v="8.3463">8.35</td>
      <td class="pct-cell" data-v="97.7931">97.8%</td>
      <td class="num" data-v="10.8570">10.86</td>
      <td class="pct-cell" data-v="90.3448">90.3%</td>
      <td data-v="98.5517"><span class="tag hot-strong">极热</span></td>
    </tr>
  </tbody>
</table>
"""


def _provider(
    csindex_rows: list[tuple[object, ...]] | None = None,
) -> BaifenweiIndexValuationProvider:
    return BaifenweiIndexValuationProvider(
        get_text_fn=lambda _url: INDEX_PAGE,
        get_csindex_history_fn=lambda _code: csindex_rows or [],
        now_provider=lambda: "2026-07-19T12:00:00+00:00",
    )


def test_parses_index_values_and_percentiles_from_public_table() -> None:
    rows = parse_index_valuation_rows(INDEX_PAGE)

    assert rows == [{
        "index_code": "000688",
        "index_name": "科创50",
        "data_date": "2026-07-17",
        "detail_url": "https://baifenwei.com/index/kc50/",
        "pe": 209.9431,
        "pe_percentile": 98.5517,
        "pb": 8.3463,
        "pb_percentile": 97.7931,
        "ps": 10.857,
        "ps_percentile": 90.3448,
    }]


def test_provider_maps_etf_to_exact_tracked_index_and_labels_temperature() -> None:
    snapshot = _provider().fetch(
        "588870.SH",
        tracked_index_code="000688.SH",
        tracked_index_name="上证科创板50成份指数",
    )

    assert snapshot["status"] == "available"
    assert snapshot["tracked_index_name"] == "科创50"
    assert snapshot["data_as_of"] == "2026-07-17T00:00:00+08:00"
    assert snapshot["metrics"][0] == {
        "key": "pe",
        "label": "PE · 市盈率",
        "value": 209.9431,
        "percentile": 98.5517,
        "temperature": "极热",
    }
    assert snapshot["source"]["url"] == "https://baifenwei.com/index/kc50/"
    assert snapshot["source"]["verification_status"] == "public_secondary"


def test_all_etfs_receive_an_explicit_snapshot_when_index_is_not_covered() -> None:
    snapshot = _provider().fetch(
        "159999.SZ",
        tracked_index_code="931999.CSI",
        tracked_index_name="测试指数",
    )

    assert snapshot["status"] == "unavailable"
    assert snapshot["metrics"] == []
    assert "测试指数（931999）" in snapshot["unavailable_reason"]


def test_uncovered_baifenwei_index_falls_back_to_official_csindex_pe_history() -> None:
    snapshot = _provider([
        ("2026-07-14", 10.0),
        ("2026-07-15", 20.0),
        ("2026-07-16", 30.0),
        ("2026-07-17", 40.0),
    ]).fetch(
        "512890.SH",
        tracked_index_code="H30269.SH",
        tracked_index_name="中证红利低波动指数",
    )

    assert snapshot["status"] == "available"
    assert snapshot["metrics"] == [{
        "key": "pe",
        "label": "PE · 滚动市盈率",
        "value": 40.0,
        "percentile": 75.0,
        "temperature": "偏热",
    }]
    assert snapshot["source"]["verification_status"] == "official_primary"
    assert "PB、PS 百分位暂不补造" in snapshot["warnings"][0]


def test_percentile_snapshots_are_durable_and_idempotent(tmp_path) -> None:
    knowledge = ResearchKnowledgeStore(
        path=tmp_path / "research_cache.sqlite3",
        object_dir=tmp_path / "objects",
    )
    service = ETFValuationPercentileService(knowledge, provider=_provider())

    saved = service.refresh(
        "588870.SH",
        tracked_index_code="000688.SH",
        tracked_index_name="上证科创板50成份指数",
    )
    repeated = service.refresh(
        "588870.SH",
        tracked_index_code="000688.SH",
        tracked_index_name="上证科创板50成份指数",
    )
    loaded = service.latest_snapshot("588870.SH")

    assert loaded is not None
    assert loaded["snapshot_id"] == saved["snapshot_id"] == repeated["snapshot_id"]
    assert loaded["history_count"] == 1
