"""Official filing discovery and full-document ingestion for CN, HK and US equities."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import requests

from .knowledge import ResearchKnowledgeStore, get_research_knowledge_store
from .source_ingestion import CollectedSource, SourceIngestionService, market_for_symbol


_OFFICIAL_HOSTS = (
    "sse.com.cn",
    "star.sse.com.cn",
    "szse.cn",
    "bse.cn",
    "cninfo.com.cn",
    "hkexnews.hk",
    "sec.gov",
)
_FILING_TERMS = (
    "annual report",
    "interim report",
    "quarterly report",
    "results announcement",
    "10-k",
    "10-q",
    "8-k",
    "年报",
    "年度报告",
    "半年度报告",
    "季度报告",
    "业绩预告",
    "业绩快报",
    "公告",
)
_MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
_MAX_TEXT_CHARS = 1_000_000
_SUBJECT_HEADER_CHARS = 20_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _official_url(value: str, domains: Iterable[str] = _OFFICIAL_HOSTS) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme.lower() in {"http", "https"} and any(
        host == domain or host.endswith(f".{domain}") for domain in domains
    )


def _subject_document_identified(
    symbol: str,
    *,
    title: str,
    url: str,
    body: str,
) -> bool:
    """Require the requested security code in document identity/header.

    A market-wide or fund filing can mention thousands of component codes deep
    in its holdings table.  That does not make it a filing for each component.
    """

    code = str(symbol or "").strip().upper().split(".", 1)[0]
    if not code:
        return False
    # The URL is discovery metadata, not document identity.  CNInfo detail
    # routes contain the requested stock code even when the downloaded page is
    # only the generic JavaScript shell, so accepting the URL creates false
    # annual-report coverage.
    identity = f"{title}\n{body[:_SUBJECT_HEADER_CHARS]}"
    return re.search(rf"(?<!\d){re.escape(code)}(?!\d)", identity, re.I) is not None


@dataclass(frozen=True)
class OfficialFilingRecord:
    provider_id: str
    provider_record_id: str
    symbol: str
    title: str
    publisher: str
    document_url: str
    published_at: str | None = None
    filing_type: str = "filing"
    report_period: str | None = None


class OfficialFilingProvider(ABC):
    provider_id = "official"
    direct_annual_archive = False

    @abstractmethod
    def supports(self, symbol: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def list_filings(self, symbol: str, *, limit: int = 8) -> list[OfficialFilingRecord]:
        raise NotImplementedError

    def list_annual_reports(
        self,
        symbol: str,
        *,
        year: int,
        limit: int = 3,
    ) -> list[OfficialFilingRecord]:
        """Best-effort compatibility path for providers without year search."""

        candidates = self.list_filings(symbol, limit=max(limit * 8, 24))
        needle = str(year)
        annual_forms = {"annual", "10-K", "20-F", "40-F"}
        return [
            item for item in candidates
            if (
                needle in str(item.report_period or "")
                or needle in item.title
                or needle in item.document_url
            )
            and (
                item.filing_type in annual_forms
                or "annual" in item.title.casefold()
                or "年度" in item.title
                or "年报" in item.title
            )
        ][:limit]


class SecEdgarFilingProvider(OfficialFilingProvider):
    provider_id = "sec_edgar"
    direct_annual_archive = True

    def supports(self, symbol: str) -> bool:
        return market_for_symbol(symbol) == "US"

    def list_filings(self, symbol: str, *, limit: int = 8) -> list[OfficialFilingRecord]:
        from backtest.loaders.sec_edgar_client import cik_for, get_submissions

        ticker = str(symbol or "").strip().upper().removesuffix(".US")
        cik = cik_for(ticker)
        if not cik:
            return []
        submissions = get_submissions(cik)
        recent = ((submissions.get("filings") or {}).get("recent") or {})
        forms = list(recent.get("form") or [])
        accessions = list(recent.get("accessionNumber") or [])
        filing_dates = list(recent.get("filingDate") or [])
        report_dates = list(recent.get("reportDate") or [])
        primary_docs = list(recent.get("primaryDocument") or [])
        descriptions = list(recent.get("primaryDocDescription") or [])
        result: list[OfficialFilingRecord] = []
        allowed_forms = {"10-K", "10-Q", "8-K", "20-F", "6-K", "40-F"}
        for index, form in enumerate(forms):
            form_type = str(form or "").upper()
            if form_type not in allowed_forms:
                continue
            accession = str(accessions[index] if index < len(accessions) else "")
            primary = str(primary_docs[index] if index < len(primary_docs) else "")
            if not accession or not primary:
                continue
            cik_value = str(cik).lstrip("0") or "0"
            url = (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{cik_value}/{accession.replace('-', '')}/{primary}"
            )
            description = str(descriptions[index] if index < len(descriptions) else "")
            result.append(
                OfficialFilingRecord(
                    provider_id=self.provider_id,
                    provider_record_id=accession,
                    symbol=symbol,
                    title=f"{form_type} {description or primary}".strip(),
                    publisher="U.S. Securities and Exchange Commission",
                    document_url=url,
                    published_at=str(
                        filing_dates[index] if index < len(filing_dates) else ""
                    )
                    or str(report_dates[index] if index < len(report_dates) else "")
                    or None,
                    filing_type=form_type,
                    report_period=str(
                        report_dates[index] if index < len(report_dates) else ""
                    ) or None,
                )
            )
            if len(result) >= limit:
                break
        return result

    def list_annual_reports(
        self,
        symbol: str,
        *,
        year: int,
        limit: int = 3,
    ) -> list[OfficialFilingRecord]:
        return super().list_annual_reports(symbol, year=year, limit=limit)


class OfficialDomainSearchProvider(OfficialFilingProvider):
    """Discover candidate URLs, then leave authentication to the full-text fetch."""

    def __init__(
        self,
        provider_id: str,
        *,
        domains: tuple[str, ...],
        market: str,
        publisher: str,
    ) -> None:
        self.provider_id = provider_id
        self.domains = domains
        self.market = market
        self.publisher = publisher

    def supports(self, symbol: str) -> bool:
        market = market_for_symbol(symbol)
        if market != self.market:
            return False
        if self.provider_id == "sse_official":
            return symbol.upper().endswith(".SH")
        if self.provider_id == "szse_official":
            return symbol.upper().endswith(".SZ")
        if self.provider_id == "bse_official":
            return symbol.upper().endswith(".BJ")
        return True

    def list_filings(self, symbol: str, *, limit: int = 8) -> list[OfficialFilingRecord]:
        from ddgs import DDGS

        code = str(symbol or "").strip().upper().split(".", 1)[0]
        site_clause = " OR ".join(f"site:{domain}" for domain in self.domains)
        query = f"({site_clause}) {code} (年度报告 OR 半年度报告 OR 季度报告 OR 公告 OR annual report)"
        rows = DDGS().text(query, max_results=max(limit * 3, 12))
        result: list[OfficialFilingRecord] = []
        seen: set[str] = set()
        for raw in rows or []:
            item = dict(raw)
            url = str(item.get("href") or item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or url in seen or not _official_url(url, self.domains):
                continue
            searchable = f"{title} {url}".casefold()
            if not any(term.casefold() in searchable for term in _FILING_TERMS):
                continue
            seen.add(url)
            result.append(
                OfficialFilingRecord(
                    provider_id=self.provider_id,
                    provider_record_id=url,
                    symbol=symbol,
                    title=title or f"{code} official filing",
                    publisher=self.publisher,
                    document_url=url,
                )
            )
            if len(result) >= limit:
                break
        return result

    def list_annual_reports(
        self,
        symbol: str,
        *,
        year: int,
        limit: int = 3,
    ) -> list[OfficialFilingRecord]:
        from ddgs import DDGS

        code = str(symbol or "").strip().upper().split(".", 1)[0]
        site_clause = " OR ".join(f"site:{domain}" for domain in self.domains)
        query = f"({site_clause}) {code} {year} (年度报告 OR 年报 OR annual report)"
        rows = DDGS().text(query, max_results=max(limit * 4, 12))
        result: list[OfficialFilingRecord] = []
        seen: set[str] = set()
        for raw in rows or []:
            item = dict(raw)
            url = str(item.get("href") or item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or url in seen or not _official_url(url, self.domains):
                continue
            searchable = f"{title} {url}".casefold()
            if str(year) not in searchable:
                continue
            if not any(term in searchable for term in ("annual", "年度", "年报")):
                continue
            seen.add(url)
            result.append(OfficialFilingRecord(
                provider_id=self.provider_id,
                provider_record_id=url,
                symbol=symbol,
                title=title or f"{code} {year} 年度报告",
                publisher=self.publisher,
                document_url=url,
                filing_type="annual",
                report_period=f"{year}-12-31",
            ))
            if len(result) >= limit:
                break
        return result


class CninfoAnnualReportProvider(OfficialFilingProvider):
    """Use CNInfo's official announcement archive instead of a web search engine."""

    provider_id = "cninfo_api"
    direct_annual_archive = True
    endpoint = "https://www.cninfo.com.cn/new/hisAnnouncement/query"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def supports(self, symbol: str) -> bool:
        normalized = str(symbol or "").strip().upper()
        return normalized.endswith(".SH") or normalized.endswith(".SZ")

    @staticmethod
    def _clean_title(value: Any) -> str:
        return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", str(value or ""))))

    @staticmethod
    def _published_at(value: Any) -> str | None:
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            return str(value or "").strip() or None
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    def list_filings(self, symbol: str, *, limit: int = 8) -> list[OfficialFilingRecord]:
        current_year = datetime.now(timezone.utc).year
        result: list[OfficialFilingRecord] = []
        for year in (current_year - 1, current_year - 2):
            result.extend(self.list_annual_reports(symbol, year=year, limit=limit))
            if len(result) >= limit:
                break
        return result[:limit]

    def list_annual_reports(
        self,
        symbol: str,
        *,
        year: int,
        limit: int = 3,
    ) -> list[OfficialFilingRecord]:
        normalized = str(symbol or "").strip().upper()
        if not self.supports(normalized):
            return []
        code = normalized.split(".", 1)[0]
        is_sh = normalized.endswith(".SH")
        response = self.session.post(
            self.endpoint,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
                ),
                "Referer": "https://www.cninfo.com.cn/",
                "Accept": "application/json, text/plain, */*",
            },
            data={
                "pageNum": "1",
                "pageSize": "50",
                "column": "sse" if is_sh else "szse",
                "tabName": "fulltext",
                "plate": "sh" if is_sh else "sz",
                "stock": "",
                "searchkey": code,
                "secid": "",
                "category": "category_ndbg_szsh",
                "trade": "",
                # Annual reports are normally published in the following year.
                "seDate": f"{year}-01-01~{year + 1}-12-31",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("announcements") or []
        candidates: list[tuple[int, int, OfficialFilingRecord]] = []
        for raw in rows:
            item = dict(raw or {})
            if str(item.get("secCode") or "").strip() != code:
                continue
            title = self._clean_title(item.get("announcementTitle"))
            if f"{year}年年度报告" not in title:
                continue
            if any(token in title for token in ("摘要", "英文", "取消")):
                continue
            adjunct = str(item.get("adjunctUrl") or "").strip()
            if not adjunct:
                continue
            if adjunct.startswith(("http://", "https://")):
                document_url = adjunct
            else:
                document_url = f"https://static.cninfo.com.cn/{adjunct.lstrip('/')}"
            announcement_id = str(item.get("announcementId") or adjunct)
            published_raw = item.get("announcementTime")
            try:
                published_sort = int(published_raw or 0)
            except (TypeError, ValueError):
                published_sort = 0
            corrected = int(any(token in title for token in ("更正后", "更新后", "修订版")))
            candidates.append((
                corrected,
                published_sort,
                OfficialFilingRecord(
                    provider_id=self.provider_id,
                    provider_record_id=announcement_id,
                    symbol=normalized,
                    title=title,
                    publisher="巨潮资讯网",
                    document_url=document_url,
                    published_at=self._published_at(published_raw),
                    filing_type="annual",
                    report_period=f"{year}-12-31",
                ),
            ))
        candidates.sort(key=lambda value: (value[0], value[1]), reverse=True)
        return [item[2] for item in candidates[: max(1, int(limit))]]

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._suppressed += 1
        elif tag in {"p", "br", "div", "section", "article", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._suppressed:
            self._suppressed -= 1
        elif tag in {"p", "div", "section", "article", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        value = " ".join(self.parts)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n\n", value)
        return value.strip()


def _pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - packaging/runtime boundary
        raise RuntimeError("official filing PDF extraction requires pypdf") from exc
    try:
        pdf = PdfReader(BytesIO(content))
        parts: list[str] = []
        for page in pdf.pages[:400]:
            parts.append(str(page.extract_text() or ""))
            if sum(len(part) for part in parts) >= _MAX_TEXT_CHARS:
                break
    except Exception as exc:
        raise RuntimeError(f"official filing PDF extraction failed with pypdf: {exc}") from exc
    text = "\n\n".join(parts)[:_MAX_TEXT_CHARS].strip()
    if not text:
        raise RuntimeError(
            "official filing PDF extraction failed with pypdf: document has no extractable text"
        )
    return text


class OfficialFilingService:
    def __init__(
        self,
        *,
        store: ResearchKnowledgeStore | None = None,
        ingestion: SourceIngestionService | None = None,
        providers: list[OfficialFilingProvider] | None = None,
        session: requests.Session | None = None,
        extraction: Any | None = None,
    ) -> None:
        self.store = store or get_research_knowledge_store()
        self.ingestion = ingestion or SourceIngestionService(self.store)
        self.session = session or requests.Session()
        self.providers = providers if providers is not None else [
            SecEdgarFilingProvider(),
            CninfoAnnualReportProvider(session=self.session),
            OfficialDomainSearchProvider(
                "sse_official",
                domains=("sse.com.cn", "star.sse.com.cn"),
                market="CN",
                publisher="上海证券交易所",
            ),
            OfficialDomainSearchProvider(
                "szse_official",
                domains=("szse.cn",),
                market="CN",
                publisher="深圳证券交易所",
            ),
            OfficialDomainSearchProvider(
                "bse_official",
                domains=("bse.cn",),
                market="CN",
                publisher="北京证券交易所",
            ),
            OfficialDomainSearchProvider(
                "hkex_official",
                domains=("hkexnews.hk",),
                market="HK",
                publisher="香港交易所披露易",
            ),
        ]
        if extraction is None:
            from .structured_financials import OfficialFinancialExtractionService

            extraction = OfficialFinancialExtractionService(
                store=self.store,
                ingestion=self.ingestion,
            )
        self.extraction = extraction

    def _archived_records(self, symbol: str) -> list[OfficialFilingRecord]:
        """Replay known official URLs so parser/source upgrades repair locally cached gaps."""

        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT o.provider_id,o.provider_record_id,d.title,d.publisher,
                          d.canonical_url,d.published_at
                   FROM source_observations o
                   JOIN source_documents d USING(document_ref)
                   WHERE o.subject_key=? AND o.source_kind='official_filing'
                     AND o.verification_status='official_primary'
                     AND d.superseded_by IS NULL
                   ORDER BY o.observed_at DESC LIMIT 24""",
                (symbol,),
            ).fetchall()
        records: list[OfficialFilingRecord] = []
        seen: set[str] = set()
        for row in rows:
            url = str(row["canonical_url"] or "").strip()
            if not url or url in seen or not _official_url(url):
                continue
            seen.add(url)
            records.append(
                OfficialFilingRecord(
                    provider_id=str(row["provider_id"] or "official_archive"),
                    provider_record_id=str(row["provider_record_id"] or url),
                    symbol=symbol,
                    title=str(row["title"] or f"{symbol} official filing"),
                    publisher=str(row["publisher"] or row["provider_id"] or "official"),
                    document_url=url,
                    published_at=str(row["published_at"] or "") or None,
                )
            )
        return records

    def _downgrade_irrelevant_existing(self, symbol: str) -> int:
        """Correct old subject links created from deep holdings-table matches."""

        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT d.document_ref,d.title,d.canonical_url,d.object_path
                   FROM source_observations o
                   JOIN source_documents d USING(document_ref)
                   WHERE o.subject_key=? AND o.source_kind='official_filing'
                     AND o.verification_status='official_primary'
                     AND NOT EXISTS (
                         SELECT 1 FROM structured_document_extractions e
                         WHERE e.document_ref=d.document_ref AND e.subject_key=o.subject_key
                           AND e.status='validated'
                     )""",
                (symbol,),
            ).fetchall()
            rejected: list[str] = []
            for row in rows:
                try:
                    body = Path(str(row["object_path"] or "")).read_text(encoding="utf-8")
                except OSError:
                    continue
                if _subject_document_identified(
                    symbol,
                    title=str(row["title"] or ""),
                    url=str(row["canonical_url"] or ""),
                    body=body,
                ):
                    continue
                rejected.append(str(row["document_ref"]))
            for document_ref in rejected:
                observations = conn.execute(
                    """SELECT observation_id,metadata_json FROM source_observations
                       WHERE document_ref=? AND subject_key=?""",
                    (document_ref, symbol),
                ).fetchall()
                for observation in observations:
                    try:
                        metadata = json.loads(observation["metadata_json"] or "{}")
                    except json.JSONDecodeError:
                        metadata = {}
                    metadata["subject_identity_status"] = "rejected"
                    metadata["subject_identity_reason"] = "security_code_not_in_document_header"
                    conn.execute(
                        """UPDATE source_observations
                           SET verification_status='source_recorded',source_kind='other',
                               metadata_json=? WHERE observation_id=?""",
                        (
                            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                            observation["observation_id"],
                        ),
                    )
                conn.execute(
                    """UPDATE structured_document_extractions
                       SET status='not_applicable',error='subject_identity_rejected',updated_at=?
                       WHERE document_ref=? AND subject_key=?
                         AND status IN ('needs_review','failed')""",
                    (_utc_now(), document_ref, symbol),
                )
        return len(rejected)

    def _fresh(self, symbol: str, ttl_hours: int) -> bool:
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT MAX(observed_at) AS latest FROM source_observations
                   WHERE subject_key=? AND source_kind='official_filing'
                     AND verification_status='official_primary'""",
                (symbol.upper(),),
            ).fetchone()
        raw = str(row["latest"] or "") if row else ""
        if not raw:
            return False
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - value.astimezone(timezone.utc) < timedelta(hours=ttl_hours)

    def _download(self, record: OfficialFilingRecord) -> tuple[str, str]:
        headers = {
            "User-Agent": os.getenv(
                "VIBE_TRADING_OFFICIAL_SOURCE_USER_AGENT",
                "Vibe-Trading research archive contact=local-user",
            ),
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
        }
        download_url = record.document_url
        if download_url.lower().startswith("http://") and _official_url(download_url):
            download_url = f"https://{download_url[7:]}"
        response = self.session.get(
            download_url,
            headers=headers,
            timeout=(10, 60),
            allow_redirects=True,
            stream=True,
        )
        response.raise_for_status()
        final_url = str(response.url)
        if not _official_url(final_url):
            raise ValueError("official filing redirected outside the approved official domains")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > _MAX_DOWNLOAD_BYTES:
                raise ValueError("official filing exceeds the 32 MiB ingestion limit")
            chunks.append(chunk)
        content = b"".join(chunks)
        content_type = str(response.headers.get("content-type") or "").lower()
        if "pdf" in content_type or final_url.lower().endswith(".pdf") or content[:4] == b"%PDF":
            text = _pdf_text(content)
        else:
            encoding = response.encoding or "utf-8"
            html = content.decode(encoding, errors="replace")
            parser = _TextExtractor()
            parser.feed(html)
            text = parser.text()
        if len(text.strip()) < 40:
            raise ValueError("official filing did not yield readable full text")
        return final_url, text[:_MAX_TEXT_CHARS]

    def annual_report_coverage(
        self,
        symbol: str,
        *,
        years: Iterable[int],
    ) -> dict[str, Any]:
        """Return official annual-report coverage from the shared subject archive."""

        normalized = str(symbol or "").strip().upper()
        requested = sorted({int(value) for value in years}, reverse=True)
        sources = self.store.list_subject_sources(
            normalized,
            source_kind="official_filing",
            verification_status="official_primary",
            limit=100,
        ).get("sources") or []
        documents_by_year: dict[int, list[dict[str, Any]]] = {}
        unusable_documents_by_year: dict[int, list[dict[str, Any]]] = {}
        for source in sources:
            metadata = dict(source.get("metadata") or {})
            filing_type = str(metadata.get("filing_type") or "").casefold()
            title = str(source.get("title") or "")
            if filing_type not in {"annual", "10-k", "20-f", "40-f"} and not any(
                token in title.casefold() for token in ("annual", "年度", "年报")
            ):
                continue
            raw_year = metadata.get("reporting_year")
            if raw_year is None:
                raw_period = str(metadata.get("report_period") or "")
                match = re.search(r"(?:19|20)\d{2}", raw_period or title)
                raw_year = int(match.group(0)) if match else None
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                continue
            if year not in requested:
                continue
            entry = {
                "document_ref": source.get("document_ref"),
                "title": title,
                "source_url": source.get("source_url"),
                "structured_status": source.get("structured_status"),
            }
            normalized_title = re.sub(r"\s+", "", title).casefold()
            structured_status = str(source.get("structured_status") or "").casefold()
            is_derivative = any(
                token in normalized_title
                for token in ("摘要", "英文版", "已取消", "取消公告")
            )
            if is_derivative or structured_status in {"not_applicable", "failed"}:
                unusable_documents_by_year.setdefault(year, []).append(entry)
                continue
            documents_by_year.setdefault(year, []).append(entry)
        covered = [year for year in requested if documents_by_year.get(year)]
        analysis_ready = [
            year
            for year in covered
            if any(
                str(item.get("structured_status") or "").casefold() == "validated"
                for item in documents_by_year[year]
            )
        ]
        needs_review = [
            year
            for year in covered
            if year not in analysis_ready
            and any(
                str(item.get("structured_status") or "").casefold() == "needs_review"
                for item in documents_by_year[year]
            )
        ]
        unusable = [
            year
            for year in requested
            if unusable_documents_by_year.get(year) and not documents_by_year.get(year)
        ]
        return {
            "symbol": normalized,
            "requested_years": requested,
            "covered_years": covered,
            "archived_years": covered,
            "analysis_ready_years": analysis_ready,
            "needs_review_years": needs_review,
            "unusable_years": unusable,
            "missing_years": [year for year in requested if year not in covered],
            "coverage_ratio": (len(covered) / len(requested)) if requested else 1.0,
            "analysis_ready_ratio": (
                len(analysis_ready) / len(requested)
            ) if requested else 1.0,
            "documents_by_year": {str(key): value for key, value in documents_by_year.items()},
            "unusable_documents_by_year": {
                str(key): value for key, value in unusable_documents_by_year.items()
            },
        }

    def backfill_annual_reports(
        self,
        symbol: str,
        *,
        years: Iterable[int],
        force: bool = False,
        limit_per_provider_year: int = 2,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Discover, archive, and structure specific historical annual reports."""

        def emit(stage: str, *, year: int | None = None, **values: Any) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback({"stage": stage, "year": year, **values})
            except Exception:
                # Progress delivery is observability only; it must never abort
                # a filing download or mutate the research result.
                return

        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        current_year = datetime.now(timezone.utc).year
        requested = sorted({int(value) for value in years}, reverse=True)
        if not requested or any(value < 1990 or value >= current_year for value in requested):
            raise ValueError("annual report years must be between 1990 and the last complete year")
        limit = max(1, min(int(limit_per_provider_year), 4))
        relevance_downgraded = self._downgrade_irrelevant_existing(normalized)
        before = self.annual_report_coverage(normalized, years=requested)
        years_to_search = requested if force else list(before["missing_years"])
        for reused_year in requested:
            if reused_year not in years_to_search:
                emit(
                    "reused",
                    year=reused_year,
                    message=f"{reused_year} 年报已归档并可复用",
                )
        attempts: list[dict[str, Any]] = []
        records: list[OfficialFilingRecord] = []
        supported_providers = [
            provider for provider in self.providers if provider.supports(normalized)
        ]
        direct_providers = [
            provider
            for provider in supported_providers
            if provider.direct_annual_archive
        ]
        annual_providers = direct_providers or supported_providers
        for year in years_to_search:
            emit("discovering", year=year, message=f"正在查找 {year} 年官方年报")
            found_for_year = 0
            for provider in annual_providers:
                try:
                    found = provider.list_annual_reports(normalized, year=year, limit=limit)
                    attempts.append({
                        "provider_id": provider.provider_id,
                        "year": year,
                        "status": "ok",
                        "found": len(found),
                    })
                    found_for_year += len(found)
                    records.extend(found)
                    if found:
                        emit(
                            "discovered",
                            year=year,
                            provider_id=provider.provider_id,
                            message=f"已找到 {year} 年官方年报",
                        )
                except Exception as exc:
                    attempts.append({
                        "provider_id": provider.provider_id,
                        "year": year,
                        "status": "failed",
                        "error": str(exc),
                    })
            if found_for_year == 0:
                attempts.append({"provider_id": "all", "year": year, "status": "no_results"})
                emit(
                    "failed",
                    year=year,
                    error="no_official_annual_report_found",
                    message=f"未找到 {year} 年官方年报",
                )

        refreshed = 0
        failed = 0
        document_refs: list[str] = []
        structured_results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            if record.document_url in seen:
                continue
            seen.add(record.document_url)
            reporting_year = (
                int(str(record.report_period)[:4])
                if record.report_period and str(record.report_period)[:4].isdigit()
                else next((year for year in requested if str(year) in record.title), None)
            )
            try:
                emit(
                    "downloading",
                    year=reporting_year,
                    provider_id=record.provider_id,
                    message=f"正在下载并读取 {reporting_year} 年报全文",
                )
                final_url, body = self._download(record)
                emit(
                    "parsing",
                    year=reporting_year,
                    provider_id=record.provider_id,
                    message=f"正在解析 {reporting_year} 年报正文",
                )
                if not _subject_document_identified(
                    normalized,
                    title=record.title,
                    url=final_url,
                    body=body,
                ):
                    raise ValueError(
                        "official filing header does not identify the requested security code"
                    )
                archived = self.ingestion.ingest(
                    CollectedSource(
                        subject_key=normalized,
                        market=market_for_symbol(normalized),
                        source_kind="official_filing",
                        provider_id=record.provider_id,
                        provider_record_id=record.provider_record_id,
                        publisher=record.publisher,
                        title=record.title,
                        source_locator=final_url,
                        content=body,
                        published_at=record.published_at,
                        retrieved_at=_utc_now(),
                        verification_status="official_primary",
                        body_status="full_text",
                        source_class="regulatory_filing",
                        metadata={
                            "filing_type": "annual",
                            "report_period": record.report_period,
                            "reporting_year": reporting_year,
                            "collection_mode": "historical_annual_backfill",
                        },
                    ),
                    origin_type="annual_report_backfill",
                    origin_id=f"annual-backfill:{normalized}:{reporting_year or 'unknown'}",
                )
                document_ref = str(archived["document_ref"])
                document_refs.append(document_ref)
                emit(
                    "validating",
                    year=reporting_year,
                    provider_id=record.provider_id,
                    document_ref=document_ref,
                    message=f"正在校验 {reporting_year} 年财务数据",
                )
                structured_result = self.extraction.extract_document(
                    document_ref,
                    normalized,
                    force=force,
                )
                structured_results.append(structured_result)
                structured_status = str(structured_result.get("status") or "")
                progress_stage = (
                    "completed"
                    if structured_status == "validated"
                    else "needs_review"
                    if structured_status == "needs_review"
                    else "failed"
                )
                emit(
                    progress_stage,
                    year=reporting_year,
                    provider_id=record.provider_id,
                    document_ref=document_ref,
                    error=(
                        None
                        if progress_stage != "failed"
                        else str(structured_result.get("error") or structured_status or "validation_failed")
                    ),
                    message=(
                        f"{reporting_year} 年报已归档，结构化结果待复核"
                        if structured_status == "needs_review"
                        else f"{reporting_year} 年报已归档并通过校验"
                        if structured_status == "validated"
                        else f"{reporting_year} 年报未通过结构化校验"
                    ),
                )
                refreshed += 1
            except Exception as exc:
                failed += 1
                attempts.append({
                    "provider_id": record.provider_id,
                    "year": record.report_period,
                    "status": "document_failed",
                    "url": record.document_url,
                    "error": str(exc),
                })
                emit(
                    "failed",
                    year=reporting_year,
                    provider_id=record.provider_id,
                    error=str(exc),
                    message=f"{reporting_year} 年报处理失败",
                )
        after = self.annual_report_coverage(normalized, years=requested)
        return {
            "symbol": normalized,
            "status": "completed" if not after["missing_years"] else "completed_with_gaps",
            "collection_scope": "historical_annual_reports",
            "refreshed": refreshed,
            "failed": failed,
            "document_refs": list(dict.fromkeys(document_refs)),
            "provider_attempts": attempts,
            "relevance_downgraded": relevance_downgraded,
            "coverage": after,
            "structured": {
                "documents": len(structured_results),
                "validated": sum(item.get("status") == "validated" for item in structured_results),
                "needs_review": sum(item.get("status") == "needs_review" for item in structured_results),
                "metrics": sum(int(item.get("metrics_count") or 0) for item in structured_results),
                "results": structured_results,
            },
        }

    def refresh(
        self,
        symbol: str,
        *,
        force: bool = False,
        ttl_hours: int = 6,
        limit_per_provider: int = 6,
    ) -> dict[str, Any]:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        relevance_downgraded = self._downgrade_irrelevant_existing(normalized)
        if not force and self._fresh(normalized, ttl_hours):
            structured = self.extraction.extract_subject(normalized, force=False)
            return {
                "symbol": normalized,
                "status": "fresh_cache",
                "refreshed": 0,
                "failed": 0,
                "provider_attempts": [],
                "relevance_downgraded": relevance_downgraded,
                "structured": structured,
            }
        records: list[OfficialFilingRecord] = self._archived_records(normalized)
        attempts: list[dict[str, Any]] = []
        for provider in self.providers:
            if not provider.supports(normalized):
                continue
            try:
                found = provider.list_filings(normalized, limit=limit_per_provider)
                attempts.append(
                    {"provider_id": provider.provider_id, "status": "ok", "found": len(found)}
                )
                records.extend(found)
            except Exception as exc:  # provider isolation is intentional
                attempts.append(
                    {"provider_id": provider.provider_id, "status": "failed", "error": str(exc)}
                )
        refreshed = 0
        failed = 0
        structured_results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            if record.document_url in seen:
                continue
            seen.add(record.document_url)
            try:
                final_url, body = self._download(record)
                if not _subject_document_identified(
                    normalized,
                    title=record.title,
                    url=final_url,
                    body=body,
                ):
                    raise ValueError(
                        "official filing header does not identify the requested security code"
                    )
                archived = self.ingestion.ingest(
                    CollectedSource(
                        subject_key=normalized,
                        market=market_for_symbol(normalized),
                        source_kind="official_filing",
                        provider_id=record.provider_id,
                        provider_record_id=record.provider_record_id,
                        publisher=record.publisher,
                        title=record.title,
                        source_locator=final_url,
                        content=body,
                        published_at=record.published_at,
                        retrieved_at=_utc_now(),
                        verification_status="official_primary",
                        body_status="full_text",
                        source_class="regulatory_filing",
                        metadata={
                            "filing_type": record.filing_type,
                            "report_period": record.report_period,
                            "reporting_year": (
                                int(str(record.report_period)[:4])
                                if record.report_period and str(record.report_period)[:4].isdigit()
                                else None
                            ),
                        },
                    ),
                    origin_type="official_refresh",
                    origin_id=f"official-refresh:{normalized}:{_utc_now()[:13]}",
                )
                structured_results.append(
                    self.extraction.extract_document(
                        str(archived["document_ref"]),
                        normalized,
                        force=force,
                    )
                )
                refreshed += 1
            except Exception as exc:
                failed += 1
                attempts.append(
                    {
                        "provider_id": record.provider_id,
                        "status": "document_failed",
                        "url": record.document_url,
                        "error": str(exc),
                    }
                )
        if market_for_symbol(normalized) == "US" and any(
            isinstance(provider, SecEdgarFilingProvider) for provider in self.providers
        ):
            try:
                structured_results.append(
                    self.extraction.ingest_sec_companyfacts(normalized, force=force)
                )
                attempts.append(
                    {
                        "provider_id": "sec_companyfacts",
                        "status": "ok",
                        "found": 1,
                    }
                )
            except Exception as exc:
                attempts.append(
                    {
                        "provider_id": "sec_companyfacts",
                        "status": "failed",
                        "error": str(exc),
                    }
                )
        return {
            "symbol": normalized,
            "status": "completed" if refreshed else "completed_with_gaps",
            "refreshed": refreshed,
            "failed": failed,
            "provider_attempts": attempts,
            "relevance_downgraded": relevance_downgraded,
            "structured": {
                "documents": len(structured_results),
                "validated": sum(item.get("status") == "validated" for item in structured_results),
                "needs_review": sum(item.get("status") == "needs_review" for item in structured_results),
                "not_applicable": sum(item.get("status") == "not_applicable" for item in structured_results),
                "cached": sum(bool(item.get("cached")) for item in structured_results),
                "metrics": sum(int(item.get("metrics_count") or 0) for item in structured_results),
                "results": structured_results,
            },
        }

    def refresh_many(self, symbols: Iterable[str], *, force: bool = False) -> dict[str, Any]:
        results = [self.refresh(symbol, force=force) for symbol in dict.fromkeys(symbols) if symbol]
        return {
            "results": results,
            "refreshed": sum(int(item.get("refreshed") or 0) for item in results),
            "failed": sum(int(item.get("failed") or 0) for item in results),
        }


_service: OfficialFilingService | None = None


def get_official_filing_service() -> OfficialFilingService:
    global _service
    if _service is None:
        _service = OfficialFilingService()
    return _service


__all__ = [
    "OfficialFilingProvider",
    "OfficialFilingRecord",
    "OfficialFilingService",
    "OfficialDomainSearchProvider",
    "SecEdgarFilingProvider",
    "get_official_filing_service",
]
