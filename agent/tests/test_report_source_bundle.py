from __future__ import annotations

from src.reports.source_bundle import (
    build_knowledge_source_bundle,
    build_subject_source_bundle,
)


class _ResearchStore:
    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self.rows = rows

    def latest(self, kind: str, symbol: str, limit: int = 20) -> list[dict]:
        assert symbol == "688256.SH"
        return self.rows.get(kind, [])[:limit]


class _KnowledgeStore:
    def list_subject_sources(
        self,
        symbol: str,
        *,
        source_kind: str = "",
        limit: int = 100,
    ) -> dict:
        assert symbol == "588870.SH"
        rows = [
                {
                    "document_ref": "doc-price",
                    "source_kind": "fund_share_scale",
                    "title": "上海证券交易所 ETF 行情快照",
                    "publisher": "上海证券交易所",
                    "source_url": "https://www.sse.com.cn/assortment/fund/etf/list/price/",
                    "verification_status": "source_recorded",
                },
                {
                    "document_ref": "doc-scale",
                    "source_kind": "fund_share_scale",
                    "title": "上海证券交易所 ETF 基金份额",
                    "publisher": "上海证券交易所",
                    "source_url": "https://www.sse.com.cn/assortment/fund/etf/list/scale/",
                    "verification_status": "source_recorded",
                },
            ]
        return {
            "sources": [
                row for row in rows
                if not source_kind or row["source_kind"] == source_kind
            ][:limit]
        }


class _CrowdedKnowledgeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def list_subject_sources(
        self,
        symbol: str,
        *,
        source_kind: str = "",
        limit: int = 100,
    ) -> dict:
        assert symbol == "000651.SZ"
        self.calls.append((source_kind, limit))
        if source_kind == "news":
            return {
                "sources": [{
                    "document_ref": "doc-news",
                    "source_kind": "news",
                    "title": "格力电器最新新闻",
                    "publisher": "示例媒体",
                    "source_url": "https://example.com/gree-news",
                    "published_at": "2026-07-20T08:00:00+08:00",
                    "verification_status": "source_recorded",
                }]
            }
        if source_kind == "structured_financial":
            return {
                "sources": [{
                    "document_ref": f"doc-financial-{index}",
                    "source_kind": "structured_financial",
                    "title": f"财务快照 {index}",
                    "verification_status": "source_recorded",
                } for index in range(limit)]
            }
        return {"sources": []}


def test_source_bundle_grades_traceability_and_repairs_cached_titles() -> None:
    mojibake_title = "寒武纪发布年度报告".encode("utf-8").decode("latin-1")
    store = _ResearchStore({
        "fundamental": [{
            "id": 1,
            "source": "eastmoney",
            "fetched_at": "2026-07-18T17:28:31+00:00",
            "is_live_current": True,
            "payload": {
                "data": {
                    "688256.SH": {
                        "periods": [{
                            "REPORT_DATE_NAME": "2025年报",
                            "NOTICE_DATE": "2026-03-13 00:00:00",
                            "TOTALOPERATEREVE": 6_497_196_198.68,
                            "PARENTNETPROFIT": 2_059_228_538.67,
                            "ROEJQ": 26.96,
                        }]
                    }
                }
            },
        }, {
            "id": 2,
            "source": "eastmoney",
            "fetched_at": "2026-07-18T17:28:31+00:00",
            "is_live_current": True,
            "payload": {"data": {"688256.SH": {"periods": []}}},
        }],
        "news": [{
            "id": 3,
            "source": "cninfo",
            "fetched_at": "2026-07-18T17:28:33+00:00",
            "is_live_current": True,
            "payload": {
                "title": mojibake_title,
                "source": "巨潮资讯",
                "published": "2026-03-13 08:00:00",
                "url": "https://www.cninfo.com.cn/new/disclosure/detail/688256",
            },
        }, {
            "id": 4,
            "source": "eastmoney",
            "fetched_at": "2026-07-18T17:28:33+00:00",
            "is_live_current": True,
            "payload": {
                "title": "AI 算力行业更新",
                "source": "经济参考报",
                "published": "2026-07-18 03:02:21",
                "url": "https://finance.eastmoney.com/example.html",
            },
        }],
        "report": [{
            "id": 5,
            "source": "eastmoney",
            "fetched_at": "2026-07-18T17:28:32+00:00",
            "is_live_current": True,
            "payload": {
                "title": "AI Agent 时代来临",
                "brokerage": "第一上海证券",
                "analyst": "黄晨",
                "publish_date": "2026-04-28",
                "rating": "买入",
            },
        }],
    })

    bundle = build_subject_source_bundle("688256.sh", store)

    assert bundle["symbol"] == "688256.SH"
    assert bundle["traceable_count"] == 4
    assert bundle["excluded_count"] == 1
    assert bundle["verification_counts"] == {
        "official_primary": 1,
        "live_retrieved": 1,
        "source_recorded": 2,
        "historical_context": 0,
    }
    news = next(item for item in bundle["domains"] if item["kind"] == "news")
    assert news["documents"][0]["title"] == "寒武纪发布年度报告"
    assert news["documents"][0]["verification_status"] == "official_primary"
    fundamental = next(
        item for item in bundle["domains"] if item["kind"] == "fundamental"
    )
    assert fundamental["documents"][0]["title"] == "2025年报结构化财务数据"
    assert [item["label"] for item in fundamental["documents"][0]["metrics"]] == [
        "营业收入",
        "归母净利润",
        "ROE",
    ]


def test_knowledge_source_bundle_rewrites_retired_sse_etf_pages() -> None:
    bundle = build_knowledge_source_bundle("588870.sh", _KnowledgeStore())

    domain = next(
        item for item in bundle["domains"] if item["kind"] == "fund_share_scale"
    )
    assert [item["source_url"] for item in domain["documents"]] == [
        "https://www.sse.com.cn/assortment/fund/etf/price/",
        "https://www.sse.com.cn/market/funddata/volumn/etfvolumn/",
    ]


def test_knowledge_source_bundle_reads_each_domain_without_news_starvation() -> None:
    store = _CrowdedKnowledgeStore()

    bundle = build_knowledge_source_bundle("000651.SZ", store)

    news = next(item for item in bundle["domains"] if item["kind"] == "news")
    financials = next(
        item for item in bundle["domains"] if item["kind"] == "structured_financial"
    )
    assert news["document_count"] == 1
    assert news["documents"][0]["source_url"] == "https://example.com/gree-news"
    assert financials["document_count"] == 100
    assert ("news", 100) in store.calls
