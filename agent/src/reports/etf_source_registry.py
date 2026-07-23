"""Declarative, reusable source and acquisition rules for ETF research.

The registry separates *where a fact comes from* from the product-profile
compiler.  Adding a manager page, index provider, or exchange feed should be a
configuration change plus (only when necessary) a named parser, rather than a
new symbol-specific branch in the report service.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any, Iterable


DEFAULT_ETF_SOURCE_RULES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "etf_source_rules.json"
)


@dataclass(frozen=True, slots=True)
class ETFSourceRule:
    rule_id: str
    label: str
    phase: str
    slot: str
    source_kind: str
    publisher: str
    verification_status: str
    priority: int
    parser_id: str
    response_type: str
    url_template: str
    request_params: dict[str, Any]
    scope: dict[str, Any]
    provides: tuple[str, ...]
    required_for_publish: bool
    freshness_days: int
    refresh_trigger: str
    failure_policy: str
    encodings: tuple[str, ...]
    title_template: str
    published_at: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ETFSourceRule":
        request = dict(raw.get("request") or {})
        freshness = dict(raw.get("freshness") or {})
        return cls(
            rule_id=str(raw.get("rule_id") or "").strip(),
            label=str(raw.get("label") or "").strip(),
            phase=str(raw.get("phase") or "").strip(),
            slot=str(raw.get("slot") or "").strip(),
            source_kind=str(raw.get("source_kind") or "").strip(),
            publisher=str(raw.get("publisher") or "").strip(),
            verification_status=str(raw.get("verification_status") or "source_recorded"),
            priority=int(raw.get("priority") or 0),
            parser_id=str(raw.get("parser_id") or "").strip(),
            response_type=str(request.get("response_type") or "html").strip(),
            url_template=str(request.get("url") or "").strip(),
            request_params=dict(request.get("params") or {}),
            scope=dict(raw.get("scope") or {}),
            provides=tuple(str(item) for item in raw.get("provides") or []),
            required_for_publish=bool(raw.get("required_for_publish")),
            freshness_days=max(0, int(freshness.get("ttl_days") or 0)),
            refresh_trigger=str(
                freshness.get("refresh_trigger")
                or "explicit_refresh_and_report_generation"
            ),
            failure_policy=str(raw.get("failure_policy") or "warn_and_use_cache"),
            encodings=tuple(str(item) for item in raw.get("encodings") or []),
            title_template=str(raw.get("title") or raw.get("label") or "").strip(),
            published_at=(str(raw.get("published_at")) if raw.get("published_at") else None),
        )

    def applies_to(self, context: dict[str, Any]) -> bool:
        fund_code = str(context.get("fund_code") or "")
        exchange = str(context.get("exchange") or "").upper()
        manager = str(context.get("manager") or "")
        index_code = str(context.get("index_code") or "").upper()

        allowed_codes = {str(item) for item in self.scope.get("fund_codes") or []}
        if allowed_codes and fund_code not in allowed_codes:
            return False
        allowed_exchanges = {
            str(item).upper() for item in self.scope.get("exchanges") or []
        }
        if allowed_exchanges and exchange not in allowed_exchanges:
            return False
        manager_keywords = [
            str(item) for item in self.scope.get("manager_keywords") or []
        ]
        if manager_keywords and not any(item in manager for item in manager_keywords):
            return False
        allowed_indexes = {
            str(item).upper() for item in self.scope.get("index_codes") or []
        }
        if allowed_indexes and index_code not in allowed_indexes:
            return False
        patterns = [str(item) for item in self.scope.get("index_code_patterns") or []]
        if patterns and not any(re.search(pattern, index_code) for pattern in patterns):
            return False
        return True

    @staticmethod
    def _render(template: str, context: dict[str, Any]) -> str:
        required = {
            name
            for _, name, _, _ in Formatter().parse(template)
            if name
        }
        missing = sorted(name for name in required if not context.get(name))
        if missing:
            raise ValueError(f"source rule template fields missing: {', '.join(missing)}")
        return template.format(**context)

    def resolved_url(self, context: dict[str, Any]) -> str:
        return self._render(self.url_template, context)

    def resolved_title(self, context: dict[str, Any]) -> str:
        return self._render(self.title_template, context)

    def resolved_params(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            key: self._render(value, context) if isinstance(value, str) else value
            for key, value in self.request_params.items()
        }

    def public(self, context: dict[str, Any], *, include_url: bool = True) -> dict[str, Any]:
        result = {
            "rule_id": self.rule_id,
            "label": self.label,
            "phase": self.phase,
            "slot": self.slot,
            "source_kind": self.source_kind,
            "publisher": self.publisher,
            "verification_status": self.verification_status,
            "priority": self.priority,
            "parser_id": self.parser_id,
            "response_type": self.response_type,
            "provides": list(self.provides),
            "required_for_publish": self.required_for_publish,
            "freshness_days": self.freshness_days,
            "refresh_trigger": self.refresh_trigger,
            "failure_policy": self.failure_policy,
            "encodings": list(self.encodings),
        }
        if include_url:
            result["url"] = self.resolved_url(context)
        return result


class ETFSourceRegistry:
    """Load, validate, select, and render ETF source acquisition rules."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_ETF_SOURCE_RULES_PATH)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.schema_version = str(payload.get("schema_version") or "")
        self.rules = tuple(
            ETFSourceRule.from_dict(item)
            for item in payload.get("rules") or []
            if isinstance(item, dict)
        )
        self._by_id = {rule.rule_id: rule for rule in self.rules}
        self._validate()

    def _validate(self) -> None:
        if not self.schema_version:
            raise ValueError("ETF source registry schema_version is required")
        if len(self._by_id) != len(self.rules):
            raise ValueError("ETF source rule ids must be unique")
        for rule in self.rules:
            required = (
                rule.rule_id, rule.label, rule.phase, rule.slot,
                rule.source_kind, rule.publisher, rule.parser_id, rule.url_template,
            )
            if not all(required):
                raise ValueError(f"ETF source rule is incomplete: {rule.rule_id or '<unknown>'}")
            if not rule.url_template.startswith("https://"):
                raise ValueError(f"ETF source rule must use HTTPS: {rule.rule_id}")
            if rule.verification_status == "official_primary":
                host = re.sub(r"^https://", "", rule.url_template).split("/", 1)[0]
                if not any(
                    official in host
                    for official in (
                        "sse.com.cn", "szse.cn", "csindex.com.cn", "99fund.com"
                    )
                ):
                    raise ValueError(
                        f"official_primary rule uses an unapproved host: {rule.rule_id}"
                    )

    def get(self, rule_id: str) -> ETFSourceRule:
        try:
            return self._by_id[rule_id]
        except KeyError as exc:
            raise KeyError(f"unknown ETF source rule: {rule_id}") from exc

    def select(
        self,
        *,
        phase: str,
        context: dict[str, Any],
        slots: Iterable[str] | None = None,
    ) -> list[ETFSourceRule]:
        allowed_slots = {str(item) for item in slots or []}
        selected = [
            rule
            for rule in self.rules
            if rule.phase == phase
            and (not allowed_slots or rule.slot in allowed_slots)
            and rule.applies_to(context)
        ]
        return sorted(selected, key=lambda item: (-item.priority, item.rule_id))

    def plan(self, *, phase: str, context: dict[str, Any]) -> dict[str, Any]:
        rules = self.select(phase=phase, context=context)
        return {
            "registry_version": self.schema_version,
            "phase": phase,
            "context": {
                key: context.get(key)
                for key in ("symbol", "fund_code", "exchange", "manager", "index_code")
                if context.get(key)
            },
            "rules": [rule.public(context) for rule in rules],
        }


def source_context(
    symbol: str,
    *,
    manager: str | None = None,
    index_code: str | None = None,
) -> dict[str, Any]:
    normalized = str(symbol or "").strip().upper()
    fund_code = normalized.split(".", 1)[0]
    exchange = normalized.split(".", 1)[1] if "." in normalized else ""
    index_number = str(index_code or "").split(".", 1)[0]
    return {
        "symbol": normalized,
        "fund_code": fund_code,
        "exchange": exchange,
        "manager": str(manager or ""),
        "index_code": str(index_code or "").upper(),
        "index_number": index_number,
    }


_shared_registry: ETFSourceRegistry | None = None


def get_etf_source_registry() -> ETFSourceRegistry:
    global _shared_registry
    if _shared_registry is None:
        _shared_registry = ETFSourceRegistry()
    return _shared_registry
