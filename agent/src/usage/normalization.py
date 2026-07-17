"""Provider-neutral token usage normalization.

Only provider-reported values are accepted. Missing values remain ``None``;
this module never estimates token counts from text length.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _pick(source: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in source:
            value = _coerce_optional_int(source.get(key))
            if value is not None:
                return value
    return None


def normalize_usage(usage: Any) -> dict[str, int | None] | None:
    """Normalize common LangChain/OpenAI/Anthropic usage payloads.

    Returns ``None`` only when the provider supplied no recognizable usage
    fields. An explicit all-zero usage payload remains an all-zero payload.
    """

    raw = _mapping(usage)
    if not raw:
        return None

    input_details = _mapping(
        raw.get("input_token_details")
        or raw.get("input_tokens_details")
        or raw.get("prompt_tokens_details")
    )
    output_details = _mapping(
        raw.get("output_token_details")
        or raw.get("output_tokens_details")
        or raw.get("completion_tokens_details")
    )

    input_tokens = _pick(raw, "input_tokens", "prompt_tokens")
    output_tokens = _pick(raw, "output_tokens", "completion_tokens")
    total_tokens = _pick(raw, "total_tokens")
    cache_read = _pick(
        raw,
        "cache_read_input_tokens",
        "cached_input_tokens",
        "cache_read_tokens",
    )
    if cache_read is None:
        cache_read = _pick(
            input_details,
            "cache_read",
            "cached_tokens",
            "cache_read_input_tokens",
            "cached_input_tokens",
        )

    cache_write = _pick(
        raw,
        "cache_write_input_tokens",
        "cache_creation_input_tokens",
        "cache_write_tokens",
    )
    if cache_write is None:
        cache_write = _pick(
            input_details,
            "cache_creation",
            "cache_write",
            "cache_creation_input_tokens",
            "cache_write_input_tokens",
        )

    reasoning = _pick(raw, "reasoning_tokens")
    if reasoning is None:
        reasoning = _pick(output_details, "reasoning", "reasoning_tokens")

    recognized = any(
        key in raw
        for key in (
            "input_tokens",
            "prompt_tokens",
            "output_tokens",
            "completion_tokens",
            "total_tokens",
            "input_token_details",
            "input_tokens_details",
            "prompt_tokens_details",
            "output_token_details",
            "output_tokens_details",
            "completion_tokens_details",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cache_write_input_tokens",
            "reasoning_tokens",
        )
    )
    if not recognized:
        return None

    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_write_input_tokens": cache_write,
        "reasoning_tokens": reasoning,
    }
