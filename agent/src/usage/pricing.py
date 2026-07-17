"""Versioned, timestamp-aware LLM cost estimates.

The usage ledger stores provider-reported tokens and the UTC call start time.
This module keeps pricing separate from token accounting so price changes do not
alter the raw usage record.  Estimates intentionally retain their native
currency; callers must not add CNY and USD without an explicit FX policy.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

PRICING_CATALOG_VERSION = "2026-07-17"
_MILLION = Decimal(1_000_000)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class PriceRule:
    rule_id: str
    providers: frozenset[str]
    models: frozenset[str]
    currency: str
    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_per_million: Decimal | None
    source_url: str
    source_label: str
    additional_sources: tuple[tuple[str, str], ...] = ()
    pricing_timezone: str = "UTC"
    peak_windows: tuple[tuple[time, time], ...] = ()
    peak_multiplier: Decimal = Decimal(1)


def _d(value: str) -> Decimal:
    return Decimal(value)


_RULES = (
    PriceRule(
        rule_id="deepseek-v4-pro-direct-cny-2026-07",
        providers=frozenset({"deepseek"}),
        models=frozenset({"deepseek-v4-pro", "deepseek/deepseek-v4-pro"}),
        currency="CNY",
        input_per_million=_d("3"),
        output_per_million=_d("6"),
        cache_read_per_million=_d("0.025"),
        source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        source_label="DeepSeek 官方价格",
        additional_sources=((
            "腾讯云原厂同步峰谷公告",
            "https://cloud.tencent.com/announce/detail/2353",
        ),),
        pricing_timezone="Asia/Shanghai",
        peak_windows=((time(9), time(12)), (time(14), time(18))),
        peak_multiplier=_d("2"),
    ),
    PriceRule(
        rule_id="deepseek-v4-flash-direct-cny-2026-07",
        providers=frozenset({"deepseek"}),
        models=frozenset(
            {
                "deepseek-v4-flash",
                "deepseek/deepseek-v4-flash",
                "deepseek-chat",
                "deepseek-reasoner",
            }
        ),
        currency="CNY",
        input_per_million=_d("1"),
        output_per_million=_d("2"),
        cache_read_per_million=_d("0.02"),
        source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        source_label="DeepSeek 官方价格",
        additional_sources=((
            "腾讯云原厂同步峰谷公告",
            "https://cloud.tencent.com/announce/detail/2353",
        ),),
        pricing_timezone="Asia/Shanghai",
        peak_windows=((time(9), time(12)), (time(14), time(18))),
        peak_multiplier=_d("2"),
    ),
    PriceRule(
        rule_id="openrouter-deepseek-v4-pro-usd-2026-07",
        providers=frozenset({"openrouter"}),
        models=frozenset({"deepseek/deepseek-v4-pro", "deepseek-v4-pro"}),
        currency="USD",
        input_per_million=_d("0.435"),
        output_per_million=_d("0.87"),
        cache_read_per_million=_d("0.003625"),
        source_url="https://openrouter.ai/deepseek/deepseek-v4-pro",
        source_label="OpenRouter 模型价格",
    ),
    PriceRule(
        rule_id="openrouter-deepseek-v4-flash-usd-2026-07",
        providers=frozenset({"openrouter"}),
        models=frozenset({"deepseek/deepseek-v4-flash", "deepseek-v4-flash"}),
        currency="USD",
        input_per_million=_d("0.14"),
        output_per_million=_d("0.28"),
        cache_read_per_million=_d("0.0028"),
        source_url="https://openrouter.ai/deepseek/deepseek-v4-flash",
        source_label="OpenRouter 模型价格",
    ),
    PriceRule(
        rule_id="openai-chat-latest-usd-2026-07",
        providers=frozenset({"openai"}),
        models=frozenset(
            {
                "chat-latest",
                "gpt-5.5-instant",
                "gpt-5.5-2026-04-23",
            }
        ),
        currency="USD",
        input_per_million=_d("5"),
        output_per_million=_d("30"),
        cache_read_per_million=_d("0.5"),
        source_url="https://developers.openai.com/api/docs/models/chat-latest",
        source_label="OpenAI API 模型价格",
    ),
    PriceRule(
        rule_id="gemini-3-5-flash-usd-2026-06",
        providers=frozenset({"gemini"}),
        models=frozenset({"gemini-3.5-flash"}),
        currency="USD",
        input_per_million=_d("1.5"),
        output_per_million=_d("9"),
        cache_read_per_million=_d("0.15"),
        source_url="https://ai.google.dev/gemini-api/docs/pricing",
        source_label="Google Gemini API 价格",
    ),
    PriceRule(
        rule_id="kimi-k2-6-usd-2026-06",
        providers=frozenset({"moonshot", "kimi"}),
        models=frozenset({"kimi-k2.6"}),
        currency="USD",
        input_per_million=_d("0.95"),
        output_per_million=_d("4"),
        cache_read_per_million=_d("0.16"),
        source_url="https://www.kimi.com/zh-cn/resources/kimi-k2-6-pricing",
        source_label="Kimi K2.6 官方价格",
    ),
    PriceRule(
        rule_id="groq-llama-4-maverick-usd-2025-04",
        providers=frozenset({"groq"}),
        models=frozenset({"meta-llama/llama-4-maverick-17b-128e-instruct"}),
        currency="USD",
        input_per_million=_d("0.5"),
        output_per_million=_d("0.77"),
        cache_read_per_million=None,
        source_url="https://groq.com/newsroom/llama-4-live-day-zero-on-groq-at-lowest-cost",
        source_label="Groq 模型价格",
    ),
)


def _parse_started_at(value: Any) -> datetime | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _find_rule(provider: Any, model: Any) -> PriceRule | None:
    normalized_provider = str(provider or "unknown").strip().lower()
    normalized_model = str(model or "unknown").strip().lower()
    return next(
        (
            rule
            for rule in _RULES
            if normalized_provider in rule.providers and normalized_model in rule.models
        ),
        None,
    )


def _pricing_tier(rule: PriceRule, started_at: datetime) -> tuple[str, Decimal, datetime]:
    if not rule.peak_windows:
        return "standard", Decimal(1), started_at.astimezone(timezone.utc)
    local_started_at = started_at.astimezone(_SHANGHAI)
    local_time = local_started_at.timetz().replace(tzinfo=None)
    is_peak = any(start <= local_time < end for start, end in rule.peak_windows)
    return (
        "peak" if is_peak else "standard",
        rule.peak_multiplier if is_peak else Decimal(1),
        local_started_at,
    )


def _amount(value: Decimal) -> float:
    return round(float(value), 10)


def estimate_llm_cost(event: dict[str, Any]) -> dict[str, Any]:
    """Estimate one LLM event without inventing missing token data."""

    if event.get("kind") != "llm_call":
        return {"status": "not_applicable", "catalog_version": PRICING_CATALOG_VERSION}

    rule = _find_rule(event.get("provider"), event.get("model"))
    if rule is None:
        reason = (
            "subscription_or_credit_billing"
            if str(event.get("provider") or "").lower() == "openai-codex"
            else "price_not_configured"
        )
        return {
            "status": "unpriced",
            "reason": reason,
            "catalog_version": PRICING_CATALOG_VERSION,
        }

    started_at = _parse_started_at(event.get("started_at"))
    input_tokens = event.get("input_tokens")
    output_tokens = event.get("output_tokens")
    if started_at is None or input_tokens is None or output_tokens is None:
        return {
            "status": "unreported",
            "reason": "call_time_or_tokens_unreported",
            "currency": rule.currency,
            "rule_id": rule.rule_id,
            "catalog_version": PRICING_CATALOG_VERSION,
            "source_url": rule.source_url,
            "source_label": rule.source_label,
        }

    input_count = max(0, int(input_tokens))
    output_count = max(0, int(output_tokens))
    tier, multiplier, local_started_at = _pricing_tier(rule, started_at)
    input_rate = rule.input_per_million * multiplier
    output_rate = rule.output_per_million * multiplier
    cache_rate = (
        rule.cache_read_per_million * multiplier
        if rule.cache_read_per_million is not None
        else None
    )
    output_cost = Decimal(output_count) * output_rate / _MILLION
    cached_tokens = event.get("cache_read_input_tokens")

    if cache_rate is not None and cached_tokens is None and input_count > 0:
        lower_input_rate = min(input_rate, cache_rate)
        upper_input_rate = max(input_rate, cache_rate)
        minimum = Decimal(input_count) * lower_input_rate / _MILLION + output_cost
        maximum = Decimal(input_count) * upper_input_rate / _MILLION + output_cost
        estimated = maximum
        status = "partial"
        cache_tokens_reported = False
    else:
        cached_count = min(input_count, max(0, int(cached_tokens or 0)))
        uncached_count = input_count - cached_count
        input_cost = Decimal(uncached_count) * input_rate / _MILLION
        if cache_rate is not None:
            input_cost += Decimal(cached_count) * cache_rate / _MILLION
        estimated = input_cost + output_cost
        minimum = estimated
        maximum = estimated
        status = "complete"
        cache_tokens_reported = cached_tokens is not None or cache_rate is None

    return {
        "status": status,
        "currency": rule.currency,
        "estimated_cost": _amount(estimated),
        "minimum_estimated_cost": _amount(minimum),
        "maximum_estimated_cost": _amount(maximum),
        "tier": tier,
        "multiplier": float(multiplier),
        "pricing_timezone": rule.pricing_timezone,
        "local_started_at": local_started_at.isoformat(),
        "cache_tokens_reported": cache_tokens_reported,
        "rates_per_million": {
            "input": float(input_rate),
            "cache_read_input": float(cache_rate) if cache_rate is not None else None,
            "output": float(output_rate),
        },
        "rule_id": rule.rule_id,
        "catalog_version": PRICING_CATALOG_VERSION,
        "source_url": rule.source_url,
        "source_label": rule.source_label,
        "sources": [
            {"label": rule.source_label, "url": rule.source_url},
            *[
                {"label": label, "url": url}
                for label, url in rule.additional_sources
            ],
        ],
    }


def aggregate_llm_costs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate event estimates by native currency."""

    if not rows:
        return {
            "coverage": "not_applicable",
            "priced_calls": 0,
            "unpriced_calls": 0,
            "total_calls": 0,
            "currencies": [],
            "catalog_version": PRICING_CATALOG_VERSION,
            "time_basis": "started_at",
        }

    totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "currency": "",
            "estimated_cost": Decimal(0),
            "minimum_estimated_cost": Decimal(0),
            "maximum_estimated_cost": Decimal(0),
            "calls": 0,
            "peak_calls": 0,
        }
    )
    estimates = [estimate_llm_cost(row) for row in rows]
    priced = [estimate for estimate in estimates if estimate.get("status") in {"complete", "partial"}]
    for estimate in priced:
        currency = str(estimate["currency"])
        bucket = totals[currency]
        bucket["currency"] = currency
        bucket["estimated_cost"] += Decimal(str(estimate["estimated_cost"]))
        bucket["minimum_estimated_cost"] += Decimal(
            str(estimate["minimum_estimated_cost"])
        )
        bucket["maximum_estimated_cost"] += Decimal(
            str(estimate["maximum_estimated_cost"])
        )
        bucket["calls"] += 1
        bucket["peak_calls"] += int(estimate.get("tier") == "peak")

    currencies = []
    for currency in sorted(totals):
        item = totals[currency]
        currencies.append(
            {
                **item,
                "estimated_cost": _amount(item["estimated_cost"]),
                "minimum_estimated_cost": _amount(item["minimum_estimated_cost"]),
                "maximum_estimated_cost": _amount(item["maximum_estimated_cost"]),
            }
        )

    if len(priced) == len(rows) and all(item.get("status") == "complete" for item in priced):
        coverage = "complete"
    elif priced:
        coverage = "partial"
    else:
        coverage = "unreported"
    sources = {
        (str(source.get("label")), str(source.get("url")))
        for item in estimates
        for source in (
            item.get("sources")
            or ([{"label": item.get("source_label"), "url": item.get("source_url")}]
                if item.get("source_url") else [])
        )
        if source.get("url")
    }
    return {
        "coverage": coverage,
        "priced_calls": len(priced),
        "unpriced_calls": len(rows) - len(priced),
        "total_calls": len(rows),
        "currencies": currencies,
        "catalog_version": PRICING_CATALOG_VERSION,
        "time_basis": "started_at",
        "sources": [
            {"label": label, "url": url}
            for label, url in sorted(sources)
        ],
    }
