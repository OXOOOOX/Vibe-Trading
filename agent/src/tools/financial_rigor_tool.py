"""Agent tool: financial-rigor checks with exact decimal arithmetic.

A thin ``BaseTool`` wrapper around eight pure-stdlib verification routines that
guard investment research against numerical error and hallucinated metrics.
Auto-discovered and registered via ``BaseTool.__subclasses__()``.

All arithmetic uses ``decimal.Decimal`` under a shared 28-digit context, so
results are free of IEEE-754 drift and are reproducible and auditable. The
tool takes raw numbers only — it does not fetch data. Pair it with the
market-data / financial-statement tools: fetch there, verify here.

Sub-commands (selected via ``command``):

- ``verify_market_cap`` — price × shares vs a reported cap; verdict at 1%/5%.
- ``verify_valuation`` — PE / PB / ROE / P/FCF / FCF yield / dividend yield /
  PS derived from raw per-share inputs.
- ``cross_validate`` — one field across several sources, flag deviations over
  a tolerance (default 2%), expose the median consensus.
- ``benford`` — Benford's-law first-digit check on a list of values; needs
  ≥50 samples; reports MAD / chi-square / conformity.
- ``calc`` — safe exact evaluation of an arithmetic expression string
  (AST-whitelisted: numbers and +, -, *, / only).
- ``three_scenario`` — bull / base / bear target prices from EPS-growth and
  target-PE assumptions.
- ``implied_terminal_earnings`` — reverse a current equity market cap into the
  steady-state earnings it implies.  This is explicitly a net-income proxy,
  not a full FCFF/FCFE DCF or a target-price model.
- ``validate_terminal_scenarios`` — validate four unweighted
  TAM × share × margin terminal-earnings scenarios with exact arithmetic.

Read-only: returns JSON verdicts, writes nothing.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
from decimal import Context, Decimal, ROUND_HALF_EVEN
from typing import Any, Callable

from src.agent.tools import BaseTool

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def _stable_fact_id(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"fact_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"

# Benford's-law expected first-digit frequencies.
_BENFORD = {d: math.log10(1 + 1 / d) for d in range(1, 10)}

# AST operator handlers for the ``calc`` evaluator. Only numbers and
# +, -, *, / (with optional unary sign) are honoured; anything else raises.
_AST_BINOPS = {
    ast.Add: _CTX.add,
    ast.Sub: _CTX.subtract,
    ast.Mult: _CTX.multiply,
    ast.Div: _CTX.divide,
}
_AST_UNARYOPS = {
    ast.UAdd: lambda d: d,
    ast.USub: lambda d: -d,
}


def _err(msg: str) -> str:
    """Build the standard error JSON envelope."""
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)


def _exact(value: Any) -> Decimal:
    """Convert any numeric to an exact Decimal, avoiding float binary traps."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _fmt(value: float) -> str:
    """Render a large number with K / M / B / T suffixes for readability."""
    abs_v = abs(value)
    if abs_v >= 1e12:
        return f"{value / 1e12:.2f}T"
    if abs_v >= 1e9:
        return f"{value / 1e9:.2f}B"
    if abs_v >= 1e6:
        return f"{value / 1e6:.2f}M"
    if abs_v >= 1e3:
        return f"{value / 1e3:.2f}K"
    return f"{value:,.2f}"


def _eval_arith_node(node: ast.AST) -> Decimal:
    """Recursively evaluate an arithmetic AST node in the Decimal domain.

    Args:
        node: An AST node from a parsed expression.

    Returns:
        The exact Decimal value of the node.

    Raises:
        ValueError: If the node is not a supported numeric/arithmetic form.
    """
    if isinstance(node, ast.Constant):
        # bool is a subclass of int — reject it explicitly.
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numeric constants are allowed")
        return _exact(node.value)
    if isinstance(node, ast.BinOp):
        op_fn = _AST_BINOPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        return op_fn(_eval_arith_node(node.left), _eval_arith_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op_fn = _AST_UNARYOPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"unsupported unary operator: {type(node.op).__name__}")
        return op_fn(_eval_arith_node(node.operand))
    raise ValueError(f"disallowed element in expression: {type(node).__name__}")


def _safe_arith(expr: str) -> Decimal:
    """Evaluate a numeric arithmetic expression in the exact-Decimal domain.

    The expression is parsed and evaluated recursively with Decimal arithmetic,
    so ``0.1 + 0.2`` is exactly ``0.3`` — no IEEE-754 drift, and no ``eval``.
    Only numbers and the operators ``+ - * /`` (with optional unary sign) are
    permitted; any other AST node raises ``ValueError``.

    Args:
        expr: Arithmetic expression string, e.g. ``"510 * 9.11e9"``.

    Returns:
        The exact Decimal result.

    Raises:
        ValueError: If the expression is malformed or contains a disallowed
            element.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"malformed expression: {exc}") from exc
    return _eval_arith_node(tree.body)


# ---------------------------------------------------------------------------
# Core verification routines (pure, return structured dicts, no I/O)
# ---------------------------------------------------------------------------

def verify_market_cap(
    price: Any, shares: Any, reported_cap: Any, currency: str = "",
) -> dict[str, Any]:
    """Verify ``market cap = price × shares`` against a reported value.

    Args:
        price: Current share price.
        shares: Total share count.
        reported_cap: The market-cap figure being checked.
        currency: Optional currency label for display only.

    Returns:
        Verdict dict. ``verdict`` is ``pass`` (≤1%), ``warn`` (1–5%) or
        ``fail`` (>5%).
    """
    p, s, r = _exact(price), _exact(shares), _exact(reported_cap)
    calculated = _CTX.multiply(p, s)
    deviation = abs(float(calculated - r) / float(r)) * 100 if r != 0 else 0.0
    if deviation > 5:
        verdict = "fail"
    elif deviation > 1:
        verdict = "warn"
    else:
        verdict = "pass"
    return {
        "price": float(p),
        "shares": float(s),
        "currency": currency,
        "calculated_market_cap": float(calculated),
        "calculated_market_cap_display": _fmt(float(calculated)),
        "reported_market_cap": float(r),
        "deviation_pct": round(deviation, 4),
        "verdict": verdict,
    }


def verify_valuation(
    price: Any,
    eps: Any | None = None,
    bvps: Any | None = None,
    fcf_per_share: Any | None = None,
    dividend: Any | None = None,
    revenue_per_share: Any | None = None,
) -> dict[str, Any]:
    """Derive valuation ratios from raw per-share inputs (exact decimal).

    Each optional input, when supplied and non-zero, contributes its metric(s).
    ROE additionally requires both ``eps`` and ``bvps``.

    Args:
        price: Current share price.
        eps: Earnings per share (TTM).
        bvps: Book value per share.
        fcf_per_share: Free cash flow per share.
        dividend: Dividend per share.
        revenue_per_share: Revenue per share.

    Returns:
        Dict with ``price`` and a ``metrics`` map (PE, PB, ROE_pct, P_FCF,
        FCF_yield_pct, dividend_yield_pct, PS — whichever apply).
    """
    p = _exact(price)
    metrics: dict[str, float] = {}
    if eps is not None:
        e = _exact(eps)
        if e != 0:
            metrics["PE"] = float(_CTX.divide(p, e))
            metrics["earnings_yield_pct"] = float(_CTX.divide(e, p) * 100)
    if bvps is not None:
        b = _exact(bvps)
        if b != 0:
            metrics["PB"] = float(_CTX.divide(p, b))
            if eps is not None and _exact(eps) != 0:
                metrics["ROE_pct"] = float(_CTX.divide(_exact(eps), b) * 100)
    if fcf_per_share is not None:
        f = _exact(fcf_per_share)
        if f != 0:
            metrics["P_FCF"] = float(_CTX.divide(p, f))
            metrics["FCF_yield_pct"] = float(_CTX.divide(f, p) * 100)
    if dividend is not None:
        d = _exact(dividend)
        if p != 0:
            metrics["dividend_yield_pct"] = float(_CTX.divide(d, p) * 100)
    if revenue_per_share is not None:
        rps = _exact(revenue_per_share)
        if rps != 0:
            metrics["PS"] = float(_CTX.divide(p, rps))
    return {"price": float(p), "metrics": metrics}


def cross_validate(
    field_name: str,
    source_values: dict[str, Any],
    unit: str = "",
    tolerance_pct: float = 2.0,
) -> dict[str, Any]:
    """Compare one field across sources, flag deviations over a tolerance.

    The median of the supplied values is used as the reference, and each
    source's percent deviation from it is reported.

    Args:
        field_name: Field being compared (e.g. ``"revenue"``).
        source_values: Mapping of source name to numeric value.
        unit: Optional unit label for display.
        tolerance_pct: Percent deviation above which a source is inconsistent.

    Returns:
        Dict with ``median_reference``/``consensus``, ``all_consistent`` and a
        per-source breakdown.
    """
    values = {k: _exact(v) for k, v in source_values.items()}
    nums = sorted(float(v) for v in values.values())
    n = len(nums)
    if n == 0:
        median = 0.0
    elif n % 2 == 1:
        median = nums[n // 2]
    else:
        median = (nums[n // 2 - 1] + nums[n // 2]) / 2
    per_source: list[dict[str, Any]] = []
    all_consistent = True
    for src, val in values.items():
        dev = abs(float(val) - median) / median * 100 if median != 0 else 0.0
        consistent = dev <= tolerance_pct
        all_consistent = all_consistent and consistent
        per_source.append({
            "source": src,
            "value": float(val),
            "deviation_pct": round(dev, 4),
            "consistent": consistent,
        })
    return {
        "field": field_name,
        "unit": unit,
        "tolerance_pct": tolerance_pct,
        "median_reference": median,
        "consensus": median,
        "all_consistent": all_consistent,
        "per_source": per_source,
    }


def benford_check(values: list[Any]) -> dict[str, Any]:
    """First-digit Benford's-law check on a list of financial values.

    Args:
        values: Numeric values to inspect.

    Returns:
        Dict with ``sample_size``, ``reliable`` (False when ``n < 50``), and —
        when reliable — ``mad`` (Nigrini's MAD), ``chi2``, ``conformity`` and a
        per-digit ``distribution``.
    """
    digits: list[int] = []
    for raw in values:
        v = abs(float(raw))
        if v > 0:
            sig = 10 ** (math.log10(v) - math.floor(math.log10(v)))
            d = int(sig)
            if 1 <= d <= 9:
                digits.append(d)
    n = len(digits)
    if n < 50:
        return {
            "sample_size": n,
            "reliable": False,
            "note": "Benford analysis needs >= 50 samples to be meaningful",
        }
    counts = {d: 0 for d in range(1, 10)}
    for d in digits:
        counts[d] += 1
    observed = {d: counts[d] / n for d in range(1, 10)}
    mad = sum(abs(observed[d] - _BENFORD[d]) for d in range(1, 10)) / 9
    chi2 = sum(
        (counts[d] - _BENFORD[d] * n) ** 2 / (_BENFORD[d] * n) for d in range(1, 10)
    )
    if mad < 0.006:
        conformity = "close"
    elif mad < 0.012:
        conformity = "acceptable"
    elif mad < 0.015:
        conformity = "marginal"
    else:
        conformity = "nonconforming"
    distribution = [
        {
            "digit": d,
            "observed": round(observed[d], 4),
            "expected": round(_BENFORD[d], 4),
            "deviation": round(observed[d] - _BENFORD[d], 4),
        }
        for d in range(1, 10)
    ]
    return {
        "sample_size": n,
        "reliable": True,
        "mad": round(mad, 6),
        "chi2": round(chi2, 4),
        "conformity": conformity,
        "is_conforming": mad < 0.015,
        "distribution": distribution,
    }


def exact_calc(expr: str) -> dict[str, Any]:
    """Evaluate an arithmetic expression with exact decimal arithmetic.

    Args:
        expr: Arithmetic expression string (numbers and ``+ - * /`` only).

    Returns:
        Dict with ``result`` (float) and ``result_exact`` (Decimal string).

    Raises:
        ValueError: If the expression is malformed or contains a disallowed
            element (surfaced by the caller as a tool error).
    """
    d = _safe_arith(expr)
    return {"expr": expr, "result": float(d), "result_exact": str(d)}


def three_scenario_valuation(
    current_price: Any,
    current_eps: Any,
    shares_billion: Any,
    growth_optimistic: Any,
    growth_neutral: Any,
    growth_pessimistic: Any,
    pe_optimistic: Any,
    pe_neutral: Any,
    pe_pessimistic: Any,
    years: int = 3,
    currency: str = "",
) -> dict[str, Any]:
    """Bull / base / bear target prices from EPS-growth and target-PE assumptions.

    Future EPS = ``current_eps × (1 + growth) ** years``; target price = future
    EPS × target PE. All math is exact decimal.

    Args:
        current_price: Current share price.
        current_eps: Current EPS.
        shares_billion: Share count in billions.
        growth_optimistic / growth_neutral / growth_pessimistic: Annual EPS
            growth rate per scenario (e.g. ``0.15`` for 15%).
        pe_optimistic / pe_neutral / pe_pessimistic: Target PE per scenario.
        years: Forecast horizon in years.
        currency: Optional currency label.

    Returns:
        Dict with the assumptions and a ``scenarios`` list, each carrying its
        ``future_eps``, ``target_price`` and ``upside_pct``.
    """
    p, eps, shares = _exact(current_price), _exact(current_eps), _exact(shares_billion)
    spec = [
        ("bull", growth_optimistic, pe_optimistic),
        ("base", growth_neutral, pe_neutral),
        ("bear", growth_pessimistic, pe_pessimistic),
    ]
    scenarios: list[dict[str, Any]] = []
    normalized: list[str] = []
    for name, growth, pe in spec:
        g, target_pe = _exact(growth), _exact(pe)
        # Defensive: LLMs frequently pass "15%" as 15 instead of 0.15. Treat
        # |growth| > 1 (i.e. > 100%) as a percent and normalize, flagging it.
        if abs(float(g)) > 1:
            g = _CTX.divide(g, Decimal("100"))
            normalized.append(name)
        future_eps = eps
        for _ in range(int(years)):
            future_eps = _CTX.multiply(future_eps, _CTX.add(Decimal("1"), g))
        target_price = _CTX.multiply(future_eps, target_pe)
        upside = float(target_price - p) / float(p) * 100 if p != 0 else 0.0
        scenarios.append({
            "scenario": name,
            "annual_growth": float(g),
            "target_pe": float(target_pe),
            "future_eps": float(future_eps),
            "target_price": float(target_price),
            "upside_pct": round(upside, 2),
        })
    result: dict[str, Any] = {
        "current_price": float(p),
        "current_eps": float(eps),
        "shares_billion": float(shares),
        "years": int(years),
        "currency": currency,
        "scenarios": scenarios,
    }
    if normalized:
        result["growth_normalized_from_percent"] = normalized
        result["note"] = (
            f"growth values > 1 (100%) were treated as percentages and divided "
            f"by 100 for scenarios: {normalized}. Pass 0.15 for 15% to avoid this."
        )
    return result


def _discount(value: Decimal, rate: Decimal, year: int) -> Decimal:
    """Discount ``value`` by ``rate`` for an integer number of years."""

    return _CTX.divide(value, _CTX.power(_CTX.add(Decimal("1"), rate), year))


def _terminal_model_present_value(
    *,
    terminal_earnings: Decimal,
    earnings: tuple[Decimal, Decimal, Decimal],
    discount_rate: Decimal,
    transition_years: int,
) -> Decimal:
    """Present value used by :func:`implied_terminal_earnings`.

    The first three forecast earnings are explicit.  Earnings then move
    linearly from E3 to the steady-state level over ``transition_years``.  At
    the end of that transition the remaining steady-state stream is valued as
    a zero-growth perpetuity.  The deliberately simple model is monotonic in
    ``terminal_earnings``, which makes the reverse solution deterministic and
    auditable.
    """

    e1, e2, e3 = earnings
    present_value = _CTX.add(
        _CTX.add(_discount(e1, discount_rate, 1), _discount(e2, discount_rate, 2)),
        _discount(e3, discount_rate, 3),
    )
    years = Decimal(transition_years)
    delta = _CTX.subtract(terminal_earnings, e3)
    for step in range(1, transition_years + 1):
        weight = _CTX.divide(Decimal(step), years)
        transition_earnings = _CTX.add(e3, _CTX.multiply(delta, weight))
        present_value = _CTX.add(
            present_value,
            _discount(transition_earnings, discount_rate, 3 + step),
        )
    terminal_value = _CTX.divide(terminal_earnings, discount_rate)
    return _CTX.add(
        present_value,
        _discount(terminal_value, discount_rate, 3 + transition_years),
    )


def implied_terminal_earnings(
    market_cap: Any,
    earnings_e1: Any,
    earnings_e2: Any,
    earnings_e3: Any,
    *,
    discount_rates: list[Any] | None = None,
    transition_years: int = 7,
    basis: str = "net_income_proxy",
    currency: str = "",
    forecast_years: list[int] | None = None,
    base_year: int | None = None,
    terminal_revenue: Any | None = None,
    source_fact_ids: list[str] | None = None,
    symbol: str = "",
) -> dict[str, Any]:
    """Reverse market capitalization into implied steady-state earnings.

    This deliberately does **not** claim to be a complete DCF.  The supplied
    net earnings are treated as an equity-cash-flow proxy, the transition is
    linear, terminal growth is zero, and every requested discount rate is
    solved independently without probability weighting.
    """

    if basis != "net_income_proxy":
        raise ValueError("basis must be 'net_income_proxy' in v1")
    if not 5 <= int(transition_years) <= 12:
        raise ValueError("transition_years must be between 5 and 12")

    cap = _exact(market_cap)
    earnings = (_exact(earnings_e1), _exact(earnings_e2), _exact(earnings_e3))
    if cap <= 0:
        return {
            "applicability": "not_applicable",
            "reason": "market_cap_must_be_positive",
            "limitations": ["No valuation multiple fallback was used."],
        }
    if earnings[2] <= 0:
        return {
            "applicability": "not_applicable",
            "reason": "earnings_e3_must_be_positive",
            "limitations": ["No valuation multiple fallback was used."],
        }

    normalized_years: list[int] | None = None
    derived_steady_year: int | None = None
    if forecast_years is not None:
        try:
            normalized_years = [int(year) for year in forecast_years]
        except (TypeError, ValueError) as exc:
            raise ValueError("forecast_years must contain integer fiscal years") from exc
        if len(normalized_years) != 3 or any(
            right != left + 1 for left, right in zip(normalized_years, normalized_years[1:])
        ):
            return {
                "applicability": "not_applicable",
                "reason": "forecast_years_must_be_three_consecutive_years",
                "forecast_years": normalized_years,
                "limitations": ["No valuation multiple fallback was used."],
            }
        if base_year is not None and normalized_years[0] != int(base_year) + 1:
            return {
                "applicability": "not_applicable",
                "reason": "forecast_years_do_not_follow_base_year",
                "forecast_years": normalized_years,
                "base_year": int(base_year),
                "limitations": ["No valuation multiple fallback was used."],
            }
        derived_steady_year = normalized_years[-1] + int(transition_years)
    elif base_year is not None:
        derived_steady_year = int(base_year) + 3 + int(transition_years)

    rates = [_exact(value) for value in (discount_rates or ["0.08", "0.10", "0.12"])]
    if not rates or any(rate <= 0 or rate >= 1 for rate in rates):
        raise ValueError("discount_rates must be non-empty decimals strictly between 0 and 1")

    terminal_revenue_decimal = _exact(terminal_revenue) if terminal_revenue is not None else None
    if terminal_revenue_decimal is not None and terminal_revenue_decimal <= 0:
        raise ValueError("terminal_revenue must be positive when supplied")

    solutions: list[dict[str, Any]] = []
    for rate in rates:
        zero_pv = _terminal_model_present_value(
            terminal_earnings=Decimal("0"),
            earnings=earnings,
            discount_rate=rate,
            transition_years=int(transition_years),
        )
        if zero_pv > cap:
            solutions.append(
                {
                    "discount_rate": float(rate),
                    "applicability": "not_applicable",
                    "reason": "market_cap_below_nonnegative_terminal_model_floor",
                    "model_floor": float(zero_pv),
                    "model_floor_exact": str(zero_pv),
                }
            )
            continue

        low = Decimal("0")
        high = max(earnings[2], _CTX.multiply(cap, rate), Decimal("1"))
        for _ in range(256):
            if _terminal_model_present_value(
                terminal_earnings=high,
                earnings=earnings,
                discount_rate=rate,
                transition_years=int(transition_years),
            ) >= cap:
                break
            high = _CTX.multiply(high, Decimal("2"))
        else:
            solutions.append(
                {
                    "discount_rate": float(rate),
                    "applicability": "not_applicable",
                    "reason": "unable_to_bracket_nonnegative_solution",
                }
            )
            continue

        terminal = high
        present_value = Decimal("0")
        relative_residual = Decimal("1")
        iterations = 0
        for iterations in range(1, 257):
            terminal = _CTX.divide(_CTX.add(low, high), Decimal("2"))
            present_value = _terminal_model_present_value(
                terminal_earnings=terminal,
                earnings=earnings,
                discount_rate=rate,
                transition_years=int(transition_years),
            )
            relative_residual = _CTX.divide(abs(_CTX.subtract(present_value, cap)), cap)
            # Solve materially tighter than the public 0.01% acceptance gate so
            # nearby terminal values also replay stably after JSON conversion.
            if relative_residual <= Decimal("0.00000001"):
                break
            if present_value < cap:
                low = terminal
            else:
                high = terminal

        item: dict[str, Any] = {
            "discount_rate": float(rate),
            "applicability": "applicable",
            "implied_terminal_earnings": float(terminal),
            "implied_terminal_earnings_exact": str(terminal),
            "model_present_value": float(present_value),
            "model_present_value_exact": str(present_value),
            "residual_pct": float(_CTX.multiply(relative_residual, Decimal("100"))),
            "iterations": iterations,
        }
        if terminal_revenue_decimal is not None:
            margin = _CTX.divide(terminal, terminal_revenue_decimal)
            item["implied_terminal_margin"] = float(margin)
            item["implied_terminal_margin_exact"] = str(margin)
        solutions.append(item)

    applicable = [item for item in solutions if item["applicability"] == "applicable"]
    normalized_source_fact_ids = list(dict.fromkeys(source_fact_ids or []))
    fact_period = str(derived_steady_year) if derived_steady_year is not None else "steady_state"
    derived_facts = [
        {
            "fact_id": _stable_fact_id(
                symbol, "implied_terminal_earnings", item["discount_rate"], fact_period,
                item["implied_terminal_earnings_exact"], normalized_source_fact_ids,
            ),
            "symbol": symbol,
            "metric": "implied_terminal_earnings",
            "value": item["implied_terminal_earnings_exact"],
            "unit": currency,
            "period": fact_period,
            "formula": (
                "market_cap = PV(E1,E2,E3) + PV(linear transition from E3 to L) "
                "+ PV(zero-growth perpetuity L/r)"
            ),
            "input_fact_ids": normalized_source_fact_ids,
            "evidence_ids": [],
            "calculation_version": "implied-terminal-earnings-v1",
            "validation_status": "pass" if item["residual_pct"] <= 0.01 else "fail",
            "statement_type": None,
            "metadata": {
                "discount_rate": item["discount_rate"],
                "residual_pct": item["residual_pct"],
                "transition_years": int(transition_years),
                "basis": basis,
            },
        }
        for item in applicable
    ]
    return {
        "applicability": "applicable" if applicable else "not_applicable",
        "model": "implied_terminal_earnings",
        "basis": basis,
        "transition_method": "linear_earnings",
        "terminal_growth": 0.0,
        "market_cap": float(cap),
        "market_cap_exact": str(cap),
        "forecast_earnings": [float(value) for value in earnings],
        "forecast_earnings_exact": [str(value) for value in earnings],
        "forecast_years": normalized_years,
        "base_year": int(base_year) if base_year is not None else None,
        "transition_years": int(transition_years),
        "derived_steady_year": derived_steady_year,
        "currency": currency,
        "implied_terminal_earnings_by_rate": solutions,
        "source_fact_ids": normalized_source_fact_ids,
        "derived_facts": derived_facts,
        "limitations": [
            "Net income is used as an equity-cash-flow proxy; this is not a full FCFF/FCFE DCF.",
            "The model assumes a linear earnings transition and zero terminal growth.",
            "Results are market-implied expectations, not target prices.",
            "No probability-weighted result is produced.",
        ],
    }


_TERMINAL_SCENARIO_IDS = ("conservative", "base", "optimistic", "stretched")


def _relative_error(actual: Decimal, expected: Decimal) -> Decimal:
    denominator = max(abs(expected), Decimal("1e-28"))
    return _CTX.divide(abs(_CTX.subtract(actual, expected)), denominator)


def validate_terminal_scenarios(
    scenarios: list[dict[str, Any]],
    *,
    currency: str = "",
    tam_currency: str = "",
    tolerance_pct: Any = "1",
    symbol: str = "",
    steady_year: int | None = None,
) -> dict[str, Any]:
    """Validate four exact, unweighted TAM/share/margin scenarios."""

    if not isinstance(scenarios, list) or len(scenarios) != 4:
        raise ValueError("scenarios must contain exactly four scenario objects")
    tolerance = _CTX.divide(_exact(tolerance_pct), Decimal("100"))
    if tolerance < 0 or tolerance > Decimal("0.10"):
        raise ValueError("tolerance_pct must be between 0 and 10")

    by_id: dict[str, dict[str, Any]] = {}
    for raw in scenarios:
        if not isinstance(raw, dict):
            raise ValueError("every scenario must be an object")
        scenario_id = str(raw.get("scenario_id") or "").strip().lower()
        if scenario_id not in _TERMINAL_SCENARIO_IDS or scenario_id in by_id:
            raise ValueError(
                "scenario_id values must be unique: conservative, base, optimistic, stretched"
            )
        if any(key in raw for key in ("probability", "weight", "weighted_value")):
            raise ValueError("probability weighting is not allowed for terminal scenarios")
        by_id[scenario_id] = raw

    if set(by_id) != set(_TERMINAL_SCENARIO_IDS):
        raise ValueError(
            "scenario_id values must be exactly conservative, base, optimistic, stretched"
        )

    different_currency = bool(currency and tam_currency and currency != tam_currency)
    validated: list[dict[str, Any]] = []
    derived_facts: list[dict[str, Any]] = []
    violations: list[str] = []
    previous_earnings: Decimal | None = None
    for scenario_id in _TERMINAL_SCENARIO_IDS:
        raw = by_id[scenario_id]
        for key in ("tam", "market_share", "net_margin"):
            if raw.get(key) is None:
                raise ValueError(f"{scenario_id}.{key} is required")
        tam = _exact(raw["tam"])
        share = _exact(raw["market_share"])
        margin = _exact(raw["net_margin"])
        if tam <= 0:
            raise ValueError(f"{scenario_id}.tam must be positive")
        if share < 0 or share > 1:
            raise ValueError(f"{scenario_id}.market_share must be between 0 and 1")
        if margin < 0 or margin > 1:
            raise ValueError(f"{scenario_id}.net_margin must be between 0 and 1")

        if different_currency and raw.get("fx_rate") is None:
            raise ValueError(f"{scenario_id}.fx_rate is required when TAM and report currencies differ")
        fx_rate = _exact(raw.get("fx_rate", 1))
        if fx_rate <= 0:
            raise ValueError(f"{scenario_id}.fx_rate must be positive")
        source_fact_ids = [str(item) for item in (raw.get("source_fact_ids") or []) if str(item)]
        if len(set(source_fact_ids)) < 3:
            raise ValueError(
                f"{scenario_id}.source_fact_ids must include distinct TAM, market-share, and margin facts"
            )
        if different_currency and not str(raw.get("fx_fact_id") or "").strip():
            raise ValueError(f"{scenario_id}.fx_fact_id is required when currencies differ")

        converted_tam = _CTX.multiply(tam, fx_rate)
        terminal_revenue = _CTX.multiply(converted_tam, share)
        terminal_earnings = _CTX.multiply(terminal_revenue, margin)
        scenario_violations: list[str] = []
        if raw.get("terminal_revenue") is not None:
            supplied_revenue = _exact(raw["terminal_revenue"])
            if _relative_error(supplied_revenue, terminal_revenue) > tolerance:
                scenario_violations.append("terminal_revenue_formula_mismatch")
        if raw.get("terminal_earnings") is not None:
            supplied_earnings = _exact(raw["terminal_earnings"])
            if _relative_error(supplied_earnings, terminal_earnings) > tolerance:
                scenario_violations.append("terminal_earnings_formula_mismatch")
        if previous_earnings is not None and terminal_earnings <= previous_earnings:
            scenario_violations.append("terminal_earnings_not_strictly_increasing")
        previous_earnings = terminal_earnings
        violations.extend(f"{scenario_id}:{item}" for item in scenario_violations)
        validated.append(
            {
                "scenario_id": scenario_id,
                "tam": float(tam),
                "tam_exact": str(tam),
                "market_share": float(share),
                "market_share_exact": str(share),
                "net_margin": float(margin),
                "net_margin_exact": str(margin),
                "fx_rate": float(fx_rate),
                "fx_rate_exact": str(fx_rate),
                "terminal_revenue": float(terminal_revenue),
                "terminal_revenue_exact": str(terminal_revenue),
                "terminal_earnings": float(terminal_earnings),
                "terminal_earnings_exact": str(terminal_earnings),
                "source_fact_ids": source_fact_ids,
                "fx_fact_id": str(raw.get("fx_fact_id") or "") or None,
                "validation_status": "pass" if not scenario_violations else "fail",
                "violations": scenario_violations,
            }
        )
        fact_period = str(steady_year) if steady_year is not None else "steady_state"
        for metric, value in (
            ("terminal_scenario_revenue", terminal_revenue),
            ("terminal_scenario_earnings", terminal_earnings),
        ):
            derived_facts.append({
                "fact_id": _stable_fact_id(
                    symbol, scenario_id, metric, fact_period, str(value), source_fact_ids,
                    str(raw.get("fx_fact_id") or ""),
                ),
                "symbol": symbol,
                "metric": metric,
                "value": str(value),
                "unit": currency or tam_currency,
                "period": fact_period,
                "formula": (
                    "terminal_revenue = TAM * fx_rate * market_share"
                    if metric == "terminal_scenario_revenue"
                    else "terminal_earnings = terminal_revenue * net_margin"
                ),
                "input_fact_ids": list(dict.fromkeys([
                    *source_fact_ids,
                    *([str(raw.get("fx_fact_id"))] if raw.get("fx_fact_id") else []),
                ])),
                "evidence_ids": [],
                "calculation_version": "terminal-scenario-v1",
                "validation_status": "pass" if not scenario_violations else "fail",
                "statement_type": None,
                "metadata": {
                    "scenario_id": scenario_id,
                    "currency": currency or tam_currency,
                    "tam_currency": tam_currency or currency,
                },
            })

    return {
        "validation_status": "pass" if not violations else "fail",
        "formula": "terminal_revenue = TAM * fx_rate * market_share; terminal_earnings = terminal_revenue * net_margin",
        "currency": currency,
        "tam_currency": tam_currency or currency,
        "tolerance_pct": float(_CTX.multiply(tolerance, Decimal("100"))),
        "scenarios": validated,
        "violations": violations,
        "probability_weighted_result": None,
        "derived_facts": derived_facts,
    }


class FinancialRigorTool(BaseTool):
    """Exact-decimal financial verification across eight sub-commands."""

    name = "financial_rigor"
    description = (
        "Verify financial-data accuracy with exact decimal arithmetic (no float "
        "drift). Takes raw numbers only — does not fetch data. Pair it with the "
        "market-data / financial-statement tools: fetch there, verify here. Eight "
        "sub-commands selected via `command`: 'verify_market_cap' (price x shares "
        "vs reported cap, verdict at 1%/5%), 'verify_valuation' (PE/PB/ROE/P-FCF/"
        "yields/PS from raw per-share inputs), 'cross_validate' (one field across "
        "sources, flag deviations > tolerance_pct, expose median consensus), "
        "'benford' (Benford first-digit fabrication check, needs >=50 samples), "
        "'calc' (safe exact arithmetic on an expression string), 'three_scenario' "
        "(bull/base/bear target prices from EPS-growth and target-PE assumptions), "
        "'implied_terminal_earnings' (reverse current market cap into unweighted "
        "steady-state earnings; not a full DCF/target price), and "
        "'validate_terminal_scenarios' (exactly four TAM/share/margin scenarios)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": [
                    "verify_market_cap", "verify_valuation", "cross_validate",
                    "benford", "calc", "three_scenario",
                    "implied_terminal_earnings", "validate_terminal_scenarios",
                ],
                "description": "Which verification to run.",
            },
            "price": {"type": "number", "description": "Share price."},
            "shares": {
                "type": "number",
                "description": "verify_market_cap: total share count; "
                               "three_scenario: share count in billions.",
            },
            "reported_cap": {"type": "number", "description": "Reported market cap."},
            "currency": {"type": "string", "description": "Currency label (display only)."},
            "eps": {"type": "number", "description": "Earnings per share."},
            "bvps": {"type": "number", "description": "Book value per share."},
            "fcf_per_share": {"type": "number", "description": "Free cash flow per share."},
            "dividend": {"type": "number", "description": "Dividend per share."},
            "revenue_per_share": {"type": "number", "description": "Revenue per share."},
            "field": {"type": "string", "description": "cross_validate: field name."},
            "source_values": {
                "type": "object",
                "description": "cross_validate: mapping of source name to value.",
            },
            "unit": {"type": "string", "description": "cross_validate: unit label."},
            "tolerance_pct": {
                "type": "number", "default": 2.0,
                "description": "cross_validate: max acceptable percent deviation.",
            },
            "values": {
                "type": "array", "items": {"type": "number"},
                "description": "benford: list of financial values to inspect.",
            },
            "expr": {
                "type": "string",
                "description": "calc: arithmetic expression (numbers and + - * /).",
            },
            "growth": {
                "type": "array", "items": {"type": "number"},
                "minItems": 3, "maxItems": 3,
                "description": "three_scenario: annual EPS growth as a decimal [bull, base, bear], e.g. 0.15 for 15% (values > 1 are auto-treated as percent).",
            },
            "pe": {
                "type": "array", "items": {"type": "number"},
                "minItems": 3, "maxItems": 3,
                "description": "three_scenario: target PE [bull, base, bear].",
            },
            "years": {"type": "integer", "default": 3, "description": "three_scenario horizon."},
            "market_cap": {"type": "number", "description": "Current equity market capitalization."},
            "earnings_e1": {"type": "number", "description": "First forward-year total net earnings."},
            "earnings_e2": {"type": "number", "description": "Second forward-year total net earnings."},
            "earnings_e3": {"type": "number", "description": "Third forward-year total net earnings; must be positive."},
            "discount_rates": {
                "type": "array", "items": {"type": "number"},
                "description": "Independent discount rates, defaults to [0.08, 0.10, 0.12].",
            },
            "transition_years": {
                "type": "integer", "minimum": 5, "maximum": 12, "default": 7,
                "description": "Years from E3 to steady state.",
            },
            "basis": {
                "type": "string", "enum": ["net_income_proxy"], "default": "net_income_proxy",
                "description": "v1 model basis; explicitly not full FCFF/FCFE.",
            },
            "forecast_years": {
                "type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3,
                "description": "Three consecutive fiscal years corresponding to E1-E3.",
            },
            "base_year": {"type": "integer", "description": "Latest completed actual fiscal year."},
            "terminal_revenue": {"type": "number", "description": "Optional terminal revenue for implied margin."},
            "source_fact_ids": {
                "type": "array", "items": {"type": "string"},
                "description": "Fact IDs supporting the deterministic inputs.",
            },
            "scenarios": {
                "type": "array", "minItems": 4, "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "scenario_id": {"type": "string", "enum": list(_TERMINAL_SCENARIO_IDS)},
                        "tam": {"type": "number"},
                        "market_share": {"type": "number", "minimum": 0, "maximum": 1},
                        "net_margin": {"type": "number", "minimum": 0, "maximum": 1},
                        "fx_rate": {"type": "number"},
                        "terminal_revenue": {"type": "number"},
                        "terminal_earnings": {"type": "number"},
                        "source_fact_ids": {"type": "array", "items": {"type": "string"}},
                        "fx_fact_id": {"type": "string"},
                    },
                    "required": ["scenario_id", "tam", "market_share", "net_margin", "source_fact_ids"],
                    "additionalProperties": False,
                },
                "description": "Exactly four unweighted terminal scenarios.",
            },
            "tam_currency": {"type": "string", "description": "Currency of TAM before optional FX conversion."},
            "symbol": {"type": "string", "description": "Symbol for derived Fact lineage."},
            "steady_year": {"type": "integer", "description": "Steady-state year shared with implied expectations."},
        },
        "required": ["command"],
    }
    is_readonly = True
    repeatable = True  # loop.py dedups non-repeatable tools by name; users call
                       # different sub-commands / params in one session.

    def __init__(
        self,
        default_session_id: str | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        allowed_commands: set[str] | None = None,
    ) -> None:
        self.default_session_id = default_session_id
        self.event_callback = event_callback
        all_commands = set(self.parameters["properties"]["command"]["enum"])
        self.all_commands = all_commands
        if allowed_commands is None:
            self.allowed_commands = all_commands
        else:
            unknown = set(allowed_commands) - all_commands
            if unknown:
                raise ValueError(f"unknown financial_rigor command policy: {sorted(unknown)}")
            self.allowed_commands = set(allowed_commands)
        self.parameters = copy.deepcopy(type(self).parameters)
        self.parameters["properties"]["command"]["enum"] = sorted(self.allowed_commands)

    def execute(self, **kwargs: Any) -> str:
        """Dispatch to the requested sub-command and return a JSON envelope.

        Args:
            **kwargs: ``command`` plus the inputs for that sub-command.

        Returns:
            JSON string — ``status="ok"`` with the verdict on success,
            ``status="error"`` with a message otherwise.
        """
        command = str(kwargs.get("command") or "").strip()
        if command not in self.all_commands:
            return _err(f"unknown command: {command}")
        if command not in self.allowed_commands:
            return _err(f"command is not allowed in this report profile: {command}")
        try:
            if command == "verify_market_cap":
                for key in ("price", "shares", "reported_cap"):
                    if kwargs.get(key) is None:
                        return _err(f"{key} is required for verify_market_cap")
                result: dict[str, Any] = verify_market_cap(
                    kwargs["price"], kwargs["shares"], kwargs["reported_cap"],
                    currency=str(kwargs.get("currency") or ""),
                )
            elif command == "verify_valuation":
                if kwargs.get("price") is None:
                    return _err("price is required for verify_valuation")
                result = verify_valuation(
                    kwargs["price"], kwargs.get("eps"), kwargs.get("bvps"),
                    kwargs.get("fcf_per_share"), kwargs.get("dividend"),
                    kwargs.get("revenue_per_share"),
                )
            elif command == "cross_validate":
                if not kwargs.get("field") or not kwargs.get("source_values"):
                    return _err("field and source_values are required for cross_validate")
                result = cross_validate(
                    str(kwargs["field"]), kwargs["source_values"],
                    unit=str(kwargs.get("unit") or ""),
                    tolerance_pct=float(kwargs.get("tolerance_pct") or 2.0),
                )
            elif command == "benford":
                vals = kwargs.get("values")
                if not isinstance(vals, list) or not vals:
                    return _err("values (non-empty list) is required for benford")
                result = benford_check(vals)
            elif command == "calc":
                if not kwargs.get("expr"):
                    return _err("expr is required for calc")
                result = exact_calc(str(kwargs["expr"]))
            elif command == "three_scenario":
                for key in ("price", "eps", "shares", "growth", "pe"):
                    if kwargs.get(key) is None:
                        return _err(f"{key} is required for three_scenario")
                growth = kwargs["growth"]
                pe = kwargs["pe"]
                if not isinstance(growth, list) or len(growth) != 3:
                    return _err("growth must be a list of 3 numbers [bull, base, bear]")
                if not isinstance(pe, list) or len(pe) != 3:
                    return _err("pe must be a list of 3 numbers [bull, base, bear]")
                result = three_scenario_valuation(
                    kwargs["price"], kwargs["eps"], kwargs["shares"],
                    growth[0], growth[1], growth[2],
                    pe[0], pe[1], pe[2],
                    years=int(kwargs.get("years") or 3),
                    currency=str(kwargs.get("currency") or ""),
                )
            elif command == "implied_terminal_earnings":
                for key in ("market_cap", "earnings_e1", "earnings_e2", "earnings_e3"):
                    if kwargs.get(key) is None:
                        return _err(f"{key} is required for implied_terminal_earnings")
                result = implied_terminal_earnings(
                    kwargs["market_cap"], kwargs["earnings_e1"], kwargs["earnings_e2"],
                    kwargs["earnings_e3"],
                    discount_rates=kwargs.get("discount_rates"),
                    transition_years=int(kwargs.get("transition_years") or 7),
                    basis=str(kwargs.get("basis") or "net_income_proxy"),
                    currency=str(kwargs.get("currency") or ""),
                    forecast_years=kwargs.get("forecast_years"),
                    base_year=kwargs.get("base_year"),
                    terminal_revenue=kwargs.get("terminal_revenue"),
                    source_fact_ids=kwargs.get("source_fact_ids"),
                    symbol=str(kwargs.get("symbol") or ""),
                )
            elif command == "validate_terminal_scenarios":
                scenarios = kwargs.get("scenarios")
                if not isinstance(scenarios, list):
                    return _err("scenarios is required for validate_terminal_scenarios")
                result = validate_terminal_scenarios(
                    scenarios,
                    currency=str(kwargs.get("currency") or ""),
                    tam_currency=str(kwargs.get("tam_currency") or ""),
                    tolerance_pct=kwargs.get("tolerance_pct", 1.0),
                    symbol=str(kwargs.get("symbol") or ""),
                    steady_year=int(kwargs["steady_year"]) if kwargs.get("steady_year") is not None else None,
                )
            else:
                return _err(f"unknown command: {command}")
        except Exception as exc:  # noqa: BLE001 - surface a clean tool error
            return json.dumps(
                {"status": "error", "command": command, "error": str(exc)},
                ensure_ascii=False,
            )
        if self.event_callback is not None and command in {
            "implied_terminal_earnings", "validate_terminal_scenarios",
        }:
            self.event_callback(
                "report.deterministic_result",
                {"command": command, "result": result},
            )
        return json.dumps({"status": "ok", "command": command, **result}, ensure_ascii=False)
