"""Shared instrument semantics for portfolio reports and monitoring.

The project currently supports mainland company equities and exchange-traded
fund products.  Keep the inference in one place so report assemblers do not
silently disagree about whether an ETF-only scope applies to a symbol.
"""

from __future__ import annotations

from typing import Literal


PortfolioInstrumentType = Literal["company_equity", "etf"]

_ETF_CODE_PREFIXES = ("15", "16", "50", "51", "52", "56", "58")


def infer_portfolio_instrument_type(
    symbol: str,
    *,
    explicit: str | None = None,
) -> PortfolioInstrumentType:
    """Return the stable portfolio instrument type for a normalized symbol.

    An explicit, already validated type always wins.  The prefix fallback is
    intentionally the same policy previously used by daily and weekly flows,
    but is now reusable by every report consumer.
    """

    requested = str(explicit or "").strip().lower()
    if requested in {"company_equity", "etf"}:
        return requested  # type: ignore[return-value]
    code = str(symbol or "").strip().upper().split(".", 1)[0]
    return "etf" if code.startswith(_ETF_CODE_PREFIXES) else "company_equity"


def portfolio_tick_size(
    symbol: str,
    *,
    instrument_type: str | None = None,
) -> float:
    """Return the report/monitoring price tick used by current A-share flows."""

    kind = infer_portfolio_instrument_type(symbol, explicit=instrument_type)
    return 0.001 if kind == "etf" else 0.01
