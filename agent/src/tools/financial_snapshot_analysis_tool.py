"""Agent tool for a normalized, evidence-backed financial statement snapshot."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from src.agent.tools import BaseTool
from src.reports.financial_analysis import normalize_financial_snapshot
from src.tools.data_context_tool import DataContextTool
from src.tools.financial_rigor_tool import implied_terminal_earnings
from src.tools.financial_statements_tool import FinancialStatementsTool
from src.tools.research_reports_tool import ResearchReportsTool
from src.tools.shareholder_count_tool import ShareholderCountTool
from src.tools.stock_profile_tool import StockProfileTool


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def _parse_envelope(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"ok": False, "error": "upstream returned invalid JSON"}
    return parsed if isinstance(parsed, dict) else {"ok": False, "error": "upstream returned invalid payload"}


def _statement_periods(envelope: dict[str, Any], code: str) -> list[dict[str, Any]]:
    data = envelope.get("data") or {}
    record = data.get(code) or data.get(code.upper()) or {}
    periods = record.get("periods") if isinstance(record, dict) else []
    return [dict(row) for row in periods if isinstance(row, dict)] if isinstance(periods, list) else []


def _market(code: str) -> str:
    suffix = code.rpartition(".")[2].upper()
    if suffix in {"SH", "SZ", "BJ"}:
        return "a_share"
    if suffix == "HK":
        return "hk"
    if suffix == "US":
        return "us"
    return "unsupported"


def _default_currency(market: str) -> str:
    return {"a_share": "CNY", "hk": "HKD", "us": "USD"}.get(market, "")


def _fact_value(fact: dict[str, Any]) -> Decimal | None:
    try:
        value = fact.get("value")
        return Decimal(str(value)) if value is not None else None
    except Exception:
        return None


def _verified_annual_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reject interim rows accidentally returned by a provider's annual query."""

    verified: list[dict[str, Any]] = []
    for row in rows:
        report_date = str(
            row.get("REPORT_DATE") or row.get("END_DATE") or row.get("REPORT_PERIOD") or ""
        )[:10]
        report_type = " ".join(
            str(row.get(key) or "")
            for key in ("REPORT_TYPE", "REPORT_TYPE_NAME", "PERIOD_TYPE", "FISCAL_PERIOD")
        ).upper()
        if report_date.endswith("-12-31") or any(
            token in report_type for token in ("年报", "ANNUAL", "12M", "FY", "Q4")
        ):
            verified.append(row)
    return verified


class FinancialSnapshotAnalysisTool(BaseTool):
    """Fetch all three statements, normalize them, and return auditable facts."""

    name = "analyze_financial_snapshot"
    description = (
        "Build an evidence-backed financial snapshot for one listed company. "
        "It fetches annual and optional quarterly balance/income/cash-flow data, "
        "normalizes A/H/US field aliases, preserves missing values, calculates "
        "ratios and YoY facts with formula/input Fact IDs, runs safe accounting "
        "reconciliations, and emits review signals (never a fraud verdict). For "
        "A-shares it also reads consensus EPS. If current market cap and its "
        "timestamp/source are supplied, it can run the deterministic market-"
        "implied terminal earnings model. Call this before writing an "
        "equity_deep_research report."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Exact symbol with suffix, e.g. 301308.SZ, 00700.HK, or AAPL.US.",
            },
            "security_name": {"type": "string", "description": "Resolved issuer name."},
            "currency": {"type": "string", "description": "Report currency; inferred from market if omitted."},
            "include_quarters": {"type": "boolean", "default": True},
            "auto_market_data": {
                "type": "boolean",
                "default": True,
                "description": "Refresh a policy-verified latest price when explicit market inputs are absent.",
            },
            "current_price": {"type": "number", "description": "Optional verified current price."},
            "shares": {"type": "number", "description": "Optional total shares outstanding."},
            "market_cap": {"type": "number", "description": "Optional verified current equity market cap."},
            "market_data_source": {
                "type": "string",
                "description": "Required provenance when current price/shares/market cap are supplied.",
            },
            "market_data_as_of": {
                "type": "string",
                "description": "Required timestamp/date for supplied market data.",
            },
        },
        "required": ["code"],
    }
    is_readonly = False  # verified latest-price retrieval may refresh the local market cache
    repeatable = True

    def __init__(
        self,
        default_session_id: str | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.default_session_id = default_session_id
        self.event_callback = event_callback

    def _progress(self, stage: str, message: str, current: int, total: int) -> None:
        if self.event_callback is not None:
            self.event_callback(
                "tool_progress",
                {
                    "tool": self.name,
                    "stage": stage,
                    "message": message,
                    "current": current,
                    "total": total,
                },
            )

    @staticmethod
    def _fetch_statement(code: str, statement: str, cadence: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        envelope = _parse_envelope(
            FinancialStatementsTool().execute(code=code, statement=statement, period=cadence)
        )
        return envelope, _statement_periods(envelope, code)

    def execute(self, **kwargs: Any) -> str:
        code = str(kwargs.get("code") or "").strip().upper()
        market = _market(code)
        if market == "unsupported":
            return json.dumps(
                {"status": "error", "error": "code must end in .SH/.SZ/.BJ/.HK/.US"},
                ensure_ascii=False,
            )
        currency = str(kwargs.get("currency") or _default_currency(market)).strip().upper()
        security_name = str(kwargs.get("security_name") or code).strip()
        include_quarters = bool(kwargs.get("include_quarters", True))
        retrieval_time = datetime.now(timezone.utc).isoformat()

        specs = [(statement, "annual") for statement in ("balance", "income", "cashflow", "indicators")]
        if include_quarters:
            specs.extend((statement, "quarter") for statement in ("balance", "income", "cashflow"))
        statement_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        source_statuses: dict[str, dict[str, Any]] = {}
        self._progress("financial_data", "正在读取三张财务报表", 0, len(specs))
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="financial-snapshot") as pool:
            futures = {
                pool.submit(self._fetch_statement, code, statement, cadence): (statement, cadence)
                for statement, cadence in specs
            }
            completed = 0
            for future in as_completed(futures):
                statement, cadence = futures[future]
                completed += 1
                try:
                    envelope, rows = future.result()
                except Exception as exc:  # one source failure remains a visible gap
                    envelope, rows = {"ok": False, "error": str(exc)}, []
                if cadence == "annual":
                    rows = _verified_annual_rows(rows)
                statement_rows[(statement, cadence)] = rows
                source_statuses[f"{statement}.{cadence}"] = {
                    "status": "live" if envelope.get("ok") else "unavailable",
                    "period_count": len(rows),
                    "error": envelope.get("error"),
                }
                self._progress(
                    "financial_data",
                    f"已读取 {statement}/{cadence}",
                    completed,
                    len(specs),
                )

        analysis = normalize_financial_snapshot(
            symbol=code,
            security_name=security_name,
            market=market,
            currency=currency,
            statement_rows=statement_rows,
            source="eastmoney",
            data_as_of=retrieval_time,
        )
        self._progress("deterministic_calculation", "财务比率、勾稽和异常信号已完成", 1, 2)

        consensus_rows: list[dict[str, Any]] = []
        research_status: dict[str, Any] = {"status": "unavailable"}
        profile_data: dict[str, Any] = {}
        shareholder_reference: dict[str, Any] | None = None
        if market == "a_share":
            report_envelope = _parse_envelope(ResearchReportsTool().execute(code=code, limit=20))
            report_data = report_envelope.get("data") or {}
            consensus_rows = [
                dict(row) for row in (report_data.get("consensus_eps") or []) if isinstance(row, dict)
            ]
            research_status = {
                "status": "live" if report_envelope.get("ok") else "unavailable",
                "source": report_envelope.get("source"),
                "coverage_count": len(report_data.get("reports") or []),
                "consensus_count": len(consensus_rows),
                "forecast_kind": "consensus",
                "source_statuses": report_data.get("source_statuses") or {},
                "error": report_envelope.get("error"),
            }
            holder_envelope = _parse_envelope(ShareholderCountTool().execute(code=code, max_periods=1))
            holder_periods = ((holder_envelope.get("data") or {}).get("periods") or [])
            if holder_periods and isinstance(holder_periods[0], dict):
                shareholder_reference = dict(holder_periods[0])
        else:
            profile_envelope = _parse_envelope(
                StockProfileTool().execute(
                    ticker=code,
                    sections=["key_stats", "financials", "earnings_trend"],
                )
            )
            profile_data = dict(((profile_envelope.get("data") or {}).get("sections") or {}))
            research_status = {
                "status": "live" if profile_envelope.get("ok") else "unavailable",
                "source": profile_envelope.get("source"),
                "error": profile_envelope.get("error"),
            }
            analyst_counts: list[int] = []
            for row in profile_data.get("earnings_trend") or []:
                if not isinstance(row, dict):
                    continue
                end_date = str(row.get("end_date") or "")[:10]
                year = end_date[:4] if len(end_date) >= 4 else None
                eps = row.get("eps_avg")
                if year and eps is not None:
                    consensus_rows.append({"fiscal_year": year, "consensus_eps": eps})
                    try:
                        analyst_count = int(row.get("eps_analysts"))
                    except (TypeError, ValueError):
                        analyst_count = 0
                    if analyst_count > 0:
                        analyst_counts.append(analyst_count)
            research_status.update({
                "coverage_count": min(analyst_counts) if analyst_counts else 0,
                "consensus_count": len(consensus_rows),
                "forecast_kind": "consensus",
            })

        facts: list[dict[str, Any]] = analysis["facts"]
        evidence: list[dict[str, Any]] = analysis["evidence"]
        market_cap = kwargs.get("market_cap")
        current_price = kwargs.get("current_price")
        shares = kwargs.get("shares")
        market_source = str(kwargs.get("market_data_source") or "").strip()
        market_as_of = str(kwargs.get("market_data_as_of") or "").strip()

        if market != "a_share" and profile_data:
            key_stats = dict(profile_data.get("key_stats") or {})
            financials = dict(profile_data.get("financials") or {})
            shares = shares if shares is not None else key_stats.get("sharesOutstanding")
            current_price = current_price if current_price is not None else financials.get("currentPrice")
            if market_cap is None and shares is not None and current_price is not None:
                market_cap = Decimal(str(shares)) * Decimal(str(current_price))
            if not market_source:
                market_source = "yahoo"
            if not market_as_of:
                market_as_of = retrieval_time

        if shares is None:
            total_share_facts = [fact for fact in facts if fact.get("metric") == "total_shares" and fact.get("value") is not None]
            total_share_facts.sort(key=lambda item: str(item.get("period") or ""), reverse=True)
            if total_share_facts:
                shares = total_share_facts[0]["value"]

        if (
            market == "a_share"
            and bool(kwargs.get("auto_market_data", True))
            and current_price is None
            and not market_source
        ):
            market_context = _parse_envelope(
                DataContextTool().execute(
                    action="context",
                    symbols=[code],
                    purpose="latest_price",
                    include=["market"],
                    force_live=True,
                )
            )
            market_block = market_context.get("market") or {}
            selected = next(
                (
                    item.get("selected_quote")
                    for item in (market_block.get("series") or [])
                    if isinstance(item, dict)
                    and str(item.get("symbol") or "").upper() == code
                    and item.get("actionability") == "price_actionable"
                    and isinstance(item.get("selected_quote"), dict)
                ),
                None,
            )
            if isinstance(selected, dict) and selected.get("price") is not None:
                current_price = selected.get("price")
                sources = [str(item) for item in (selected.get("sources") or []) if str(item)]
                market_source = "unified_market:" + ("+".join(sources) or "verified_quorum")
                market_as_of = str(selected.get("verified_at") or selected.get("bar_time") or "")
                source_statuses["market.latest_price"] = {
                    "status": "live",
                    "actionability": "price_actionable",
                    "blocked_reasons": [],
                }
            else:
                matching = next(
                    (
                        item for item in (market_block.get("series") or [])
                        if isinstance(item, dict) and str(item.get("symbol") or "").upper() == code
                    ),
                    {},
                )
                source_statuses["market.latest_price"] = {
                    "status": "unavailable",
                    "actionability": matching.get("actionability") or "analysis_only",
                    "blocked_reasons": matching.get("blocked_reasons") or ["verified_latest_price_missing"],
                }

        if market_cap is None and current_price is not None and shares is not None:
            try:
                market_cap = Decimal(str(current_price)) * Decimal(str(shares))
            except Exception:
                market_cap = None

        if market_source and market_as_of and any(value is not None for value in (market_cap, current_price, shares)):
            evidence_id = _stable_id("ev", code, "market", market_source, market_as_of, market_cap, current_price, shares)
            evidence.append({
                "evidence_id": evidence_id,
                "symbol": code,
                "domain": "market",
                "source": market_source,
                "source_locator": f"{code}/market/{market_as_of}",
                "retrieved_at": retrieval_time,
                "published_at": market_as_of,
                "content_hash": hashlib.sha256(f"{market_cap}|{current_price}|{shares}".encode()).hexdigest(),
                "summary": f"market_cap={market_cap}; current_price={current_price}; shares={shares}",
                "status": "verified_input",
                "metadata": {},
            })
            for metric, value, unit in (
                ("market_cap", market_cap, currency),
                ("current_price", current_price, currency),
                ("total_shares_market", shares, "shares"),
            ):
                if value is None:
                    continue
                facts.append({
                    "fact_id": _stable_id("fact", code, metric, market_as_of, value, evidence_id),
                    "symbol": code,
                    "metric": metric,
                    "value": str(value),
                    "unit": unit,
                    "period": market_as_of[:10],
                    "formula": None,
                    "input_fact_ids": [],
                    "evidence_ids": [evidence_id],
                    "calculation_version": "equity-financial-analysis-v1",
                    "validation_status": "pass",
                    "statement_type": None,
                    "metadata": {},
                })

        consensus_fact_ids_by_year: dict[int, str] = {}
        normalized_consensus: list[tuple[int, Decimal]] = []
        if consensus_rows:
            consensus_evidence_id = _stable_id("ev", code, "consensus", research_status, consensus_rows)
            evidence.append({
                "evidence_id": consensus_evidence_id,
                "symbol": code,
                "domain": "consensus",
                "source": str(research_status.get("source") or "research_provider"),
                "source_locator": f"{code}/consensus/{retrieval_time[:10]}",
                "retrieved_at": retrieval_time,
                "published_at": None,
                "content_hash": hashlib.sha256(json.dumps(consensus_rows, sort_keys=True, default=str).encode()).hexdigest(),
                "summary": f"{len(consensus_rows)} forward EPS consensus rows",
                "status": "verified" if research_status.get("status") == "live" else "unavailable",
                "metadata": {
                    "coverage_count": research_status.get("coverage_count"),
                    "forecast_kind": research_status.get("forecast_kind"),
                },
            })
            for row in consensus_rows:
                try:
                    fiscal_year = int(str(row.get("fiscal_year") or "")[:4])
                    eps = Decimal(str(row.get("consensus_eps")))
                except Exception:
                    continue
                normalized_consensus.append((fiscal_year, eps))
                fact_id = _stable_id("fact", code, "consensus_eps", fiscal_year, str(eps), consensus_evidence_id)
                consensus_fact_ids_by_year[fiscal_year] = fact_id
                facts.append({
                    "fact_id": fact_id,
                    "symbol": code,
                    "metric": "consensus_eps",
                    "value": str(eps),
                    "unit": f"{currency}/share",
                    "period": str(fiscal_year),
                    "formula": None,
                    "input_fact_ids": [],
                    "evidence_ids": [consensus_evidence_id],
                    "calculation_version": "equity-financial-analysis-v1",
                    "validation_status": "pass",
                    "statement_type": None,
                    "metadata": {},
                })

        latest_actual_years = sorted(
            {
                int(str(period.get("fiscal_year")))
                for period in analysis["snapshot"].get("periods", [])
                if period.get("period_type") == "annual" and period.get("fiscal_year") is not None
            },
            reverse=True,
        )
        base_year = latest_actual_years[0] if latest_actual_years else None
        normalized_consensus = sorted({year: eps for year, eps in normalized_consensus}.items())
        forward = [(year, eps) for year, eps in normalized_consensus if base_year is None or year > base_year]

        implied_result: dict[str, Any] = {
            "applicability": "not_applicable",
            "reason": "market_cap_shares_or_three_consecutive_consensus_years_missing",
        }
        market_fact = next((fact for fact in facts if fact.get("metric") == "market_cap"), None)
        shares_fact = next((fact for fact in facts if fact.get("metric") in {"total_shares_market", "total_shares"}), None)
        can_value = (
            market_cap is not None
            and shares is not None
            and bool(market_source and market_as_of)
            and bool(currency)
            and research_status.get("status") == "live"
            and isinstance(research_status.get("coverage_count"), int)
            and research_status["coverage_count"] > 0
            and market_fact is not None
            and shares_fact is not None
            and len(forward) >= 3
            and all(forward[index + 1][0] == forward[index][0] + 1 for index in range(2))
        )
        if can_value:
            selected = forward[:3]
            total_earnings = [eps * Decimal(str(shares)) for _, eps in selected]
            source_fact_ids = [
                *(consensus_fact_ids_by_year[year] for year, _ in selected),
                *([str(market_fact["fact_id"])] if market_fact else []),
                *([str(shares_fact["fact_id"])] if shares_fact else []),
            ]
            implied_result = implied_terminal_earnings(
                market_cap,
                total_earnings[0],
                total_earnings[1],
                total_earnings[2],
                currency=currency,
                forecast_years=[year for year, _ in selected],
                base_year=base_year,
                transition_years=7,
                discount_rates=["0.08", "0.10", "0.12"],
                source_fact_ids=source_fact_ids,
            )
            result_fact_ids: list[str] = []
            if implied_result.get("applicability") == "applicable":
                for item in implied_result.get("implied_terminal_earnings_by_rate") or []:
                    if item.get("applicability") != "applicable":
                        continue
                    rate = str(item.get("discount_rate"))
                    fact_id = _stable_id(
                        "fact", code, "implied_terminal_earnings", rate,
                        implied_result.get("derived_steady_year"), item.get("implied_terminal_earnings_exact"),
                    )
                    result_fact_ids.append(fact_id)
                    facts.append({
                        "fact_id": fact_id,
                        "symbol": code,
                        "metric": "implied_terminal_earnings",
                        "value": item.get("implied_terminal_earnings_exact"),
                        "unit": currency,
                        "period": str(implied_result.get("derived_steady_year") or "steady_state"),
                        "formula": (
                            "market_cap = PV(E1,E2,E3) + PV(linear transition from E3 to L) "
                            "+ PV(zero-growth perpetuity L/r)"
                        ),
                        "input_fact_ids": source_fact_ids,
                        "evidence_ids": [],
                        "calculation_version": "implied-terminal-earnings-v1",
                        "validation_status": "pass" if float(item.get("residual_pct") or 100) <= 0.01 else "fail",
                        "statement_type": None,
                        "metadata": {
                            "discount_rate": rate,
                            "residual_pct": item.get("residual_pct"),
                            "transition_years": implied_result.get("transition_years"),
                            "basis": "net_income_proxy",
                            "limitations": implied_result.get("limitations") or [],
                        },
                    })
                implied_result["result_fact_ids"] = result_fact_ids

        market_gate_passed = bool(
            current_price is not None
            and market_cap is not None
            and market_source
            and market_as_of
        )
        identity_gate_passed = bool(security_name and security_name.upper() != code)
        report_gate_reasons: list[str] = []
        if analysis["financial_gate"]["status"] != "passed":
            report_gate_reasons.append(
                str(analysis["financial_gate"].get("reason") or "financial_statement_gate_failed")
            )
        if not market_gate_passed:
            report_gate_reasons.append("timestamped_price_and_market_cap_required")
        if not identity_gate_passed:
            report_gate_reasons.append("symbol_and_security_name_must_be_uniquely_resolved")
        report_gate = {
            "status": "passed" if not report_gate_reasons else "failed_validation",
            "reason": ";".join(report_gate_reasons) or None,
        }
        module_statuses = {
            "report_gate": report_gate,
            "market_data": {
                "status": "passed" if market_gate_passed else "failed_validation",
                "reason": None if market_gate_passed else "timestamped_price_and_market_cap_required",
            },
            "symbol_identity": {
                "status": "passed" if identity_gate_passed else "failed_validation",
                "reason": None if identity_gate_passed else "symbol_and_security_name_must_be_uniquely_resolved",
            },
            "financial_quality": analysis["financial_gate"],
            "latest_quarter": analysis["latest_quarter"],
            "implied_expectations": {
                "status": "passed" if implied_result.get("applicability") == "applicable" else "insufficient_evidence",
                "reason": implied_result.get("reason"),
            },
            "terminal_scenarios": {
                "status": "not_requested",
                "reason": "Use financial_rigor.validate_terminal_scenarios only after sourced TAM/share/margin facts exist.",
            },
        }
        if report_gate["status"] == "failed_validation":
            quality_status = "failed_validation"
        elif any(item["status"] in {"insufficient_evidence", "warning"} for item in module_statuses.values()):
            quality_status = "passed_with_gaps"
        else:
            quality_status = "passed"

        self._progress("deterministic_calculation", "证据、事实与估值门控已完成", 2, 2)
        payload = {
                "status": "ok",
                "profile": "equity_deep_research",
                "symbol": code,
                "security_name": security_name,
                "quality_status": quality_status,
                "data_as_of": retrieval_time,
                "source_statuses": source_statuses,
                "research_status": research_status,
                "shareholder_market_cap_reference": shareholder_reference,
                "module_statuses": module_statuses,
                "report_gate": report_gate,
                **analysis,
                "facts": facts,
                "evidence": evidence,
                "implied_expectations": implied_result,
                "wording_guard": "异常信号仅用于进一步核查，不构成财务造假判断。反推结果不是完整DCF或目标价。",
        }
        # Convert any provider Decimal values before both persistence and the
        # tool response so the two representations are byte-for-byte replayable.
        payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        if self.event_callback is not None:
            self.event_callback("report.analysis_snapshot", {"analysis": payload})
        return json.dumps(payload, ensure_ascii=False)
