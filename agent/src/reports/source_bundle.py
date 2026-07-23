"""Read-only, traceability-first source bundle for a report subject dossier."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from src.research.source_classification import SOURCE_KIND_LABELS


_KIND_META = {
    "fundamental": ("年度财务数据", "结构化年度指标，不等同于交易所年报原文"),
    "news": ("相关新闻", "保留发布方、原文链接和抓取时间"),
    "broker_research": ("券商研报", "B 级券商观点；保留机构、分析师、关联范围和发布日期"),
}
_VERIFICATION_ORDER = {
    "official_primary": 0,
    "live_retrieved": 1,
    "source_recorded": 2,
    "historical_context": 3,
}
_OFFICIAL_HOSTS = (
    "cninfo.com.cn",
    "sse.com.cn",
    "szse.cn",
    "hkexnews.hk",
    "sec.gov",
    "99fund.com",
    "csindex.com.cn",
)
_STALE_SOURCE_URL_REPLACEMENTS = {
    "https://www.sse.com.cn/assortment/fund/etf/list/price/": (
        "https://www.sse.com.cn/assortment/fund/etf/price/"
    ),
    "https://www.sse.com.cn/assortment/fund/etf/list/scale/": (
        "https://www.sse.com.cn/market/funddata/volumn/etfvolumn/"
    ),
}


def _repair_mojibake(value: Any) -> str:
    text = str(value or "").strip()
    if not text or any("\u4e00" <= char <= "\u9fff" for char in text):
        return text
    if not any("\u0080" <= char <= "\u00ff" for char in text):
        return text
    candidates = [text]
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return text
    for encoding in ("utf-8", "gb18030"):
        try:
            candidates.append(raw.decode(encoding))
        except UnicodeDecodeError:
            pass

    def score(candidate: str) -> int:
        cjk = sum("\u4e00" <= char <= "\u9fff" for char in candidate)
        latin_noise = sum("\u0080" <= char <= "\u00ff" for char in candidate)
        replacement = candidate.count("\ufffd")
        return cjk * 6 - latin_noise * 2 - replacement * 12

    return max(candidates, key=score).strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _clean_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if not url.startswith(("https://", "http://")):
        return None
    return _STALE_SOURCE_URL_REPLACEMENTS.get(url, url)


def _is_official_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in _OFFICIAL_HOSTS)


def _periods(payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    data = _mapping(payload.get("data"))
    value = data.get(symbol)
    if not isinstance(value, Mapping):
        value = next(
            (
                candidate
                for key, candidate in data.items()
                if str(key).upper() == symbol.upper() and isinstance(candidate, Mapping)
            ),
            {},
        )
    return [dict(item) for item in _mapping(value).get("periods") or [] if isinstance(item, Mapping)]


def _metric(label: str, value: Any, unit: str) -> dict[str, Any] | None:
    if not isinstance(value, (int, float)):
        return None
    return {"label": label, "value": float(value), "unit": unit}


def _fundamental_document(row: dict[str, Any], payload: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    periods = _periods(payload, symbol)
    if not periods:
        return None
    latest = periods[0]
    period_label = _repair_mojibake(
        latest.get("REPORT_DATE_NAME") or latest.get("REPORT_YEAR") or latest.get("REPORT_DATE")
    )
    source_url = _clean_url(payload.get("url") or row.get("url"))
    current = bool(row.get("is_live_current"))
    verification = (
        "official_primary"
        if current and _is_official_url(source_url)
        else "source_recorded"
        if current
        else "historical_context"
    )
    metrics = [
        _metric("营业收入", latest.get("TOTALOPERATEREVE"), "CNY"),
        _metric("归母净利润", latest.get("PARENTNETPROFIT"), "CNY"),
        _metric("ROE", latest.get("ROEJQ"), "%"),
    ]
    return {
        "document_id": f"research-cache:{row.get('id')}",
        "kind": "fundamental",
        "title": f"{period_label or '最近年度'}结构化财务数据",
        "summary": "已保存可复核的年度指标快照；当前记录不是交易所年报 PDF 原文。",
        "publisher": _repair_mojibake(row.get("source")) or "未记录",
        "provider": _repair_mojibake(row.get("source")) or None,
        "published_at": str(latest.get("NOTICE_DATE") or latest.get("REPORT_DATE") or "") or None,
        "retrieved_at": row.get("fetched_at"),
        "source_url": source_url,
        "verification_status": verification,
        "metrics": [item for item in metrics if item is not None],
    }


def _research_document(kind: str, row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    title = _repair_mojibake(payload.get("title") or row.get("title"))
    provider = _repair_mojibake(row.get("source"))
    publisher = _repair_mojibake(
        payload.get("source") or payload.get("brokerage") or row.get("source")
    )
    retrieved_at = str(row.get("fetched_at") or "").strip()
    if not title or not publisher or not retrieved_at:
        return None
    source_url = _clean_url(payload.get("url") or payload.get("link") or row.get("url"))
    current = bool(row.get("is_live_current"))
    if not current:
        verification = "historical_context"
    elif _is_official_url(source_url):
        verification = "official_primary"
    elif source_url:
        verification = "live_retrieved"
    else:
        verification = "source_recorded"
    published_at = str(
        payload.get("published_at")
        or payload.get("published")
        or payload.get("publish_date")
        or payload.get("date")
        or row.get("published_at")
        or ""
    ).strip()
    if kind == "broker_research":
        details = [
            f"评级 {_repair_mojibake(payload.get('rating'))}" if payload.get("rating") else "",
            f"分析师 {_repair_mojibake(payload.get('analyst'))}" if payload.get("analyst") else "",
        ]
        summary = " · ".join(item for item in details if item) or "券商研报元数据"
    else:
        summary = _repair_mojibake(payload.get("snippet") or row.get("snippet"))
    return {
        "document_id": f"research-cache:{row.get('id')}",
        "kind": kind,
        "title": title,
        "summary": summary or None,
        "publisher": publisher,
        "provider": provider or None,
        "analyst": payload.get("analyst") or payload.get("analysts"),
        "association_scope": payload.get("association_scope") or payload.get("relation_scope") or "direct_subject",
        "related_symbol": payload.get("related_symbol") or payload.get("symbol"),
        "evidence_level": "B" if kind == "broker_research" else None,
        "published_at": published_at or None,
        "retrieved_at": retrieved_at,
        "source_url": source_url,
        "verification_status": verification,
        "metrics": [],
    }


def build_subject_source_bundle(
    symbol: str,
    store: Any,
    *,
    limit_per_kind: int = 6,
    scan_limit_per_kind: int = 40,
) -> dict[str, Any]:
    """Project cached research into a compact, honestly-tiered dossier bundle."""

    normalized = str(symbol or "").strip().upper()
    domains: list[dict[str, Any]] = []
    all_documents: list[dict[str, Any]] = []
    excluded_count = 0
    for kind, (label, description) in _KIND_META.items():
        documents: list[dict[str, Any]] = []
        legacy_kind = "report" if kind == "broker_research" else kind
        for raw in store.latest(legacy_kind, normalized, limit=scan_limit_per_kind):
            row = _mapping(raw)
            payload = _mapping(row.get("payload"))
            item = (
                _fundamental_document(row, payload, normalized)
                if kind == "fundamental"
                else _research_document(kind, row, payload)
            )
            if item is None:
                excluded_count += 1
                continue
            documents.append(item)
        documents.sort(
            key=lambda item: str(item.get("published_at") or item.get("retrieved_at") or ""),
            reverse=True,
        )
        documents.sort(
            key=lambda item: _VERIFICATION_ORDER.get(
                str(item.get("verification_status")), 99
            )
        )
        documents = documents[:limit_per_kind]
        all_documents.extend(documents)
        domains.append({
            "kind": kind,
            "label": label,
            "description": description,
            "document_count": len(documents),
            "documents": documents,
        })

    counts = {
        status: sum(
            item.get("verification_status") == status for item in all_documents
        )
        for status in _VERIFICATION_ORDER
    }
    retrieved_values = [
        str(item.get("retrieved_at") or "") for item in all_documents if item.get("retrieved_at")
    ]
    return {
        "symbol": normalized,
        "generated_at": max(retrieved_values, default=None),
        "traceable_count": len(all_documents),
        "excluded_count": excluded_count,
        "verification_counts": counts,
        "domains": domains,
        "verification_contract": {
            "official_primary": "监管机构、交易所或法定披露站点的可回溯原文",
            "live_retrieved": "本次有效来源抓取且保留直接链接；不代表新闻观点已被事实交叉验证",
            "source_recorded": "来源和抓取时间已记录，但缺少可直接打开的官方原文",
            "historical_context": "历史缓存，只能作为背景材料",
        },
    }


_ARCHIVE_KIND_META = {
    "official_filing": ("官方披露", "交易所、法定披露平台或监管机构原文"),
    "fund_product": ("ETF 产品资料", "基金管理人或交易所发布的产品身份、费率与净值资料"),
    "index_methodology": ("指数编制方案", "指数公司发布的规则版本与关键编制参数"),
    "fund_share_scale": ("ETF 份额", "交易所发布的 ETF 份额及同指数产品组数据"),
    "structured_financial": ("财务数据", "结构化财务快照及可复核指标"),
    "news": ("新闻", "已记录来源与抓取时间的新闻材料"),
    "broker_research": ("券商研报", "券商研究报告及其可追溯元数据"),
}
_ARCHIVE_KIND_META.update(
    {
        kind: SOURCE_KIND_LABELS[kind]
        for kind in (
            "company_disclosure",
            "index_constituents",
            "market_data",
            "consensus_data",
            "derived_analysis",
        )
    }
)
_ARCHIVE_KIND_ORDER = (
    "official_filing",
    "company_disclosure",
    "fund_product",
    "index_methodology",
    "index_constituents",
    "fund_share_scale",
    "market_data",
    "structured_financial",
    "consensus_data",
    "derived_analysis",
    "news",
    "broker_research",
)


def build_knowledge_source_bundle(
    symbol: str,
    knowledge_store: Any,
    *,
    legacy_store: Any | None = None,
    limit_per_kind: int = 100,
) -> dict[str, Any]:
    """Build the dossier from authoritative source documents, with legacy fallback."""

    normalized = str(symbol or "").strip().upper()
    # Read every domain independently.  A single subject-wide page lets a busy
    # domain (for example structured financial snapshots) consume the whole
    # page and makes later domains such as news appear empty even though they
    # are present in the archive.
    rows: list[dict[str, Any]] = []
    capped_per_kind = max(1, min(int(limit_per_kind), 200))
    for kind in _ARCHIVE_KIND_ORDER:
        archived = knowledge_store.list_subject_sources(
            normalized,
            source_kind=kind,
            limit=capped_per_kind,
        )
        rows.extend(list(archived.get("sources") or []))
    if not rows and legacy_store is not None:
        return build_subject_source_bundle(
            normalized,
            legacy_store,
            limit_per_kind=min(limit_per_kind, 20),
        )

    domains: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    for kind in _ARCHIVE_KIND_ORDER:
        label, description = _ARCHIVE_KIND_META[kind]
        selected: list[dict[str, Any]] = []
        seen_content_hashes: set[str] = set()
        for row in rows:
            if str(row.get("source_kind") or "") != kind:
                continue
            if row.get("superseded_by"):
                continue
            content_hash = str(row.get("content_hash") or "")
            if content_hash and content_hash in seen_content_hashes:
                continue
            if content_hash:
                seen_content_hashes.add(content_hash)
            metadata = dict(row.get("metadata") or {})
            selected.append(
                {
                    "document_id": row.get("document_ref"),
                    "kind": kind,
                    "title": row.get("title") or metadata.get("title") or "未命名资料",
                    "summary": metadata.get("summary"),
                    "publisher": row.get("publisher") or row.get("provider_id") or "未记录",
                    "provider": row.get("provider_id") or None,
                    "analyst": metadata.get("analyst") or metadata.get("analysts"),
                    "association_scope": metadata.get("association_scope") or metadata.get("relation_scope") or (
                        "direct_subject" if kind == "broker_research" else None
                    ),
                    "related_symbol": metadata.get("related_symbol") or metadata.get("symbol"),
                    "evidence_level": "B" if kind == "broker_research" else (
                        "A" if row.get("verification_status") == "official_primary" else None
                    ),
                    "published_at": row.get("published_at"),
                    "retrieved_at": row.get("observed_at") or row.get("retrieved_at"),
                    "source_url": _clean_url(row.get("source_url")),
                    "source_locator": row.get("source_locator"),
                    "verification_status": row.get("verification_status") or "source_recorded",
                    "body_status": row.get("body_status") or "metadata_only",
                    "used_by_report_count": int(row.get("used_by_report_count") or 0),
                    "structured_status": row.get("structured_status"),
                    "structured_metrics_count": int(
                        row.get("structured_metrics_count") or 0
                    ),
                    "ocr_performed": bool(row.get("ocr_performed")),
                    "structured_extractor_version": row.get(
                        "structured_extractor_version"
                    ),
                    "structured_failed_checks": list(
                        row.get("structured_failed_checks") or []
                    ),
                    "structured_error": row.get("structured_error") or None,
                    "structured_auto_repair_available": bool(
                        row.get("structured_auto_repair_available")
                    ),
                    "reporting_year": metadata.get("reporting_year"),
                    "filing_type": metadata.get("filing_type"),
                    "metrics": list(metadata.get("metrics") or []),
                }
            )
            if len(selected) >= capped_per_kind:
                break
        documents.extend(selected)
        domains.append(
            {
                "kind": kind,
                "label": label,
                "description": description,
                "document_count": len(selected),
                "documents": selected,
            }
        )
    counts = {
        status: sum(item.get("verification_status") == status for item in documents)
        for status in _VERIFICATION_ORDER
    }
    return {
        "symbol": normalized,
        "generated_at": max(
            (str(item.get("retrieved_at") or "") for item in documents),
            default=None,
        ),
        "traceable_count": len(documents),
        "excluded_count": 0,
        "verification_counts": counts,
        "domains": domains,
        "verification_contract": {
            "official_primary": "官方域名全文已读取并保存内容哈希",
            "live_retrieved": "本次已读取非官方来源全文；不代表其中观点已经事实认证",
            "source_recorded": "已记录来源或结构化提供方快照，但不是官方原文",
            "historical_context": "历史缓存，只作为背景材料",
        },
    }
