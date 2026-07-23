"""Canonical source classification for report-library evidence."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


SOURCE_KIND_LABELS = {
    "official_filing": ("官方披露", "交易所、法定披露平台或监管机构原文"),
    "company_disclosure": ("公司公告与行动", "公司公告、业绩预告及公司行动资料"),
    "fund_product": ("ETF 产品资料", "基金管理人或交易所发布的产品身份、费率与净值资料"),
    "index_methodology": ("指数编制方案", "指数公司发布的规则版本与关键编制参数"),
    "index_constituents": ("指数成分与权重", "指数公司发布的成分名单及权重文件"),
    "fund_share_scale": ("ETF 份额", "交易所发布的 ETF 份额及同指数产品组数据"),
    "market_data": ("行情与汇率", "交易行情、估值快照及汇率数据"),
    "structured_financial": ("财务数据", "结构化财务快照及可复核指标"),
    "consensus_data": ("一致预期与评级", "机构一致预期、评级和目标价资料"),
    "derived_analysis": ("派生计算", "由已记录事实或报告快照计算的分析结果"),
    "news": ("新闻", "已记录来源与抓取时间的新闻材料"),
    "broker_research": ("券商研报", "券商研究报告及其可追溯元数据"),
}

_OFFICIAL_HOSTS = (
    "sse.com.cn",
    "szse.cn",
    "bse.cn",
    "cninfo.com.cn",
    "hkexnews.hk",
    "sec.gov",
    "99fund.com",
    "csindex.com.cn",
)
_EXPLICIT_DOMAIN_KINDS = {
    "official_filing": "official_filing",
    "company_disclosure": "company_disclosure",
    "fund_product": "fund_product",
    "index_methodology": "index_methodology",
    "index_constituents": "index_constituents",
    "fund_share_scale": "fund_share_scale",
    "market_data": "market_data",
    "structured_financial": "structured_financial",
    "consensus_data": "consensus_data",
    "derived_analysis": "derived_analysis",
    "news": "news",
    "report": "broker_research",
    "broker_research": "broker_research",
}
_FINANCIAL_DOMAINS = {
    "financial_statement",
    "financial_statements",
    "financial",
    "fundamental",
}
_MARKET_DOMAINS = {"market", "fx", "identity_market"}
_DERIVED_HINTS = (
    "derived",
    "financial_rigor",
    "implied_terminal",
    "派生计算",
)
_FINANCIAL_HINTS = (
    "analyze_financial_snapshot",
    "financial snapshot",
    "公司年报",
    "公司年报/季报",
)


def _official_url(value: Any) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme.lower() in {"http", "https"} and any(
        host == domain or host.endswith(f".{domain}") for domain in _OFFICIAL_HOSTS
    )


def classify_source_kind(
    document: Mapping[str, Any],
    domain: str,
    *,
    current_kind: str = "",
) -> str:
    """Classify a source by its actual material type, not its report section."""

    normalized_domain = str(domain or "").strip().casefold()
    if normalized_domain in _EXPLICIT_DOMAIN_KINDS:
        return _EXPLICIT_DOMAIN_KINDS[normalized_domain]
    source_class = str(document.get("source_class") or "").strip().casefold()
    canonical_url = str(document.get("canonical_url") or "").strip()
    title = str(document.get("title") or "").strip()
    publisher = str(document.get("publisher") or "").strip()
    haystack = " ".join((canonical_url, title, publisher)).casefold()
    if not normalized_domain and current_kind:
        if current_kind == "fund_share_scale":
            if "产品列表" in title:
                return "fund_product"
            if "行情快照" in title or "/api/qt/ulist" in canonical_url:
                return "market_data"
        return current_kind

    if source_class in {"regulatory_filing", "company_disclosure"}:
        return "official_filing"
    if normalized_domain in _FINANCIAL_DOMAINS:
        return "structured_financial"
    if normalized_domain == "etf_universe":
        return "index_constituents"
    if normalized_domain in _MARKET_DOMAINS:
        return "market_data"
    if normalized_domain == "consensus":
        return "consensus_data"
    if normalized_domain == "company_actions":
        return "official_filing" if _official_url(canonical_url) else "company_disclosure"
    if normalized_domain == "announcement":
        if _official_url(canonical_url):
            return "official_filing"
        if "data.eastmoney.com/notices/" in haystack or (
            not canonical_url.startswith(("http://", "https://"))
            and any(marker in title for marker in ("公告", "业绩预告", "减持"))
        ):
            return "company_disclosure"
        return "news"
    if normalized_domain == "other":
        if any(marker in haystack for marker in _DERIVED_HINTS):
            return "derived_analysis"
        if any(marker in haystack for marker in _FINANCIAL_HINTS):
            return "structured_financial"
    if source_class == "broker_research":
        return "broker_research"
    return "news"


def audit_source_classifications(store: Any, *, apply: bool = False) -> dict[str, Any]:
    """Audit every stored observation and optionally correct its source kind."""

    with store.connect() as conn:
        rows = conn.execute(
            """SELECT o.observation_id,o.source_kind,o.metadata_json,
                      d.canonical_url,d.publisher,d.source_class,d.title
               FROM source_observations o
               JOIN source_documents d USING(document_ref)
               ORDER BY o.observation_id"""
        ).fetchall()
        transitions: Counter[tuple[str, str]] = Counter()
        domains: Counter[str] = Counter()
        updates: list[tuple[str, str]] = []
        examples: list[dict[str, str]] = []
        for raw in rows:
            row = dict(raw)
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except json.JSONDecodeError:
                metadata = {}
            domain = str(metadata.get("domain") or metadata.get("cache_kind") or "")
            current = str(row.get("source_kind") or "other")
            expected = classify_source_kind(row, domain, current_kind=current)
            if expected == current:
                continue
            transitions[(current, expected)] += 1
            domains[domain or "(unmarked)"] += 1
            updates.append((expected, str(row["observation_id"])))
            if len(examples) < 50:
                examples.append(
                    {
                        "observation_id": str(row["observation_id"]),
                        "domain": domain or "(unmarked)",
                        "from": current,
                        "to": expected,
                        "title": str(row.get("title") or ""),
                    }
                )
        if apply and updates:
            conn.executemany(
                "UPDATE source_observations SET source_kind=? WHERE observation_id=?",
                updates,
            )
            conn.commit()
    return {
        "total_observations": len(rows),
        "misclassified": len(updates),
        "corrected": len(updates) if apply else 0,
        "transitions": [
            {"from": source, "to": target, "count": count}
            for (source, target), count in sorted(transitions.items())
        ],
        "affected_domains": [
            {"domain": domain, "count": count}
            for domain, count in sorted(domains.items())
        ],
        "examples": examples,
    }
