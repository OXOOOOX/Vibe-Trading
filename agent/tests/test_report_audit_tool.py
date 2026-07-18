"""Tests for the ``report_audit`` agent tool.

Covers the markdown data-point extraction (tables + ``label: value`` lines,
Chinese units), sampling (clamp + reproducibility), the verdict logic across
single/two-source cases (incl. the split-source WARN and the single-source
FAIL that the original upstream logic mishandled), the tool's JSON-Schema
contract, ``execute`` happy/error paths, and auto-discovery.
"""

from __future__ import annotations

import json
from typing import Any

from src.tools import build_registry
from src.tools.report_audit_tool import (
    ReportAuditTool,
    _clean_num,
    _is_valid_label,
    _pct_diff,
    extract_data_points,
    render_verdict,
    sample_points,
)

# ── helpers ───────────────────────────────────────────────────────────────


def test_clean_num_handles_wide_comma() -> None:
    assert _clean_num("1,234") == 1234.0
    assert _clean_num("1，234") == 1234.0  # wide (full-width) comma
    assert _clean_num("abc") is None


def test_is_valid_label_filters_noise() -> None:
    assert _is_valid_label("营业收入") is True
    assert _is_valid_label("来源") is False     # skip-listed
    assert _is_valid_label("a") is False        # too short
    assert _is_valid_label("2024") is False     # year only
    assert _is_valid_label("+56%") is False     # bare growth rate


# ── extract_data_points ───────────────────────────────────────────────────


_REPORT_MD = (
    "收入：7518亿元\n"
    "毛利率：56%\n"
    "\n"
    "| 指标 | 2024 | 2023 |\n"
    "|------|------|------|\n"
    "| 营业收入 | 7518亿 | 6500亿 |\n"
    "| 净利润 | 1900亿 | 1600亿 |\n"
)


def test_extract_finds_table_and_kv_points() -> None:
    points = extract_data_points(_REPORT_MD)
    labels = {p["label"] for p in points}
    assert "收入" in labels              # KV line
    assert "毛利率" in labels            # KV line
    assert "营业收入 · 2024" in labels   # table cell
    assert "净利润 · 2024" in labels
    for p in points:
        assert {"id", "label", "reported_value", "unit", "line_number"} <= set(p)


def test_extract_assigns_unique_ids() -> None:
    ids = [p["id"] for p in extract_data_points(_REPORT_MD)]
    assert len(ids) == len(set(ids))


# ── sample_points ─────────────────────────────────────────────────────────


def _pts(n: int) -> list[dict[str, Any]]:
    return [
        {"id": i, "label": f"l{i}", "reported_value": float(i), "unit": "",
         "line_number": i, "raw_text": ""}
        for i in range(n)
    ]


def test_sample_returns_all_when_fewer_than_three() -> None:
    assert len(sample_points(_pts(2), ratio=0.5)) == 2


def test_sample_clamps_to_max_thirty() -> None:
    assert len(sample_points(_pts(500), ratio=1.0)) == 30


def test_sample_is_reproducible_with_seed() -> None:
    a = sample_points(_pts(20), ratio=0.5, seed=7)
    b = sample_points(_pts(20), ratio=0.5, seed=7)
    assert [p["id"] for p in a] == [p["id"] for p in b]


# ── _pct_diff ─────────────────────────────────────────────────────────────


def test_pct_diff() -> None:
    assert _pct_diff(100, 101) == 0.01
    assert _pct_diff(100, 100) == 0.0
    assert _pct_diff(0, 0) == 0.0
    assert _pct_diff(0, 5) == float("inf")


# ── render_verdict ────────────────────────────────────────────────────────


def test_verdict_single_source_pass() -> None:
    out = render_verdict([{
        "id": 1, "label": "rev", "reported_value": 100, "unit": "",
        "fetched_value": 100.5, "fetched_source": "m",
    }])
    assert out["verdict"] == "PASS"
    assert out["pass_count"] == 1 and out["fail_count"] == 0


def test_verdict_single_source_fail_fails_report() -> None:
    # Regression: a single-source failure must FAIL, not silently WARN.
    out = render_verdict([{
        "id": 1, "label": "rev", "reported_value": 100, "unit": "",
        "fetched_value": 150, "fetched_source": "m",
    }])
    assert out["verdict"] == "FAIL"
    assert out["fail_count"] == 1
    assert out["fail_items"][0]["label"] == "rev"


def test_verdict_two_sources_both_pass() -> None:
    out = render_verdict([{
        "id": 1, "label": "rev", "reported_value": 100, "unit": "",
        "fetched_value": 100.5, "fetched_source": "m",
        "fetched_value2": 99.8, "fetched_source2": "s",
    }])
    assert out["verdict"] == "PASS"
    assert out["pass_count"] == 1


def test_verdict_two_sources_both_fail() -> None:
    out = render_verdict([{
        "id": 1, "label": "rev", "reported_value": 100, "unit": "",
        "fetched_value": 150, "fetched_source": "m",
        "fetched_value2": 200, "fetched_source2": "s",
    }])
    assert out["verdict"] == "FAIL"
    assert out["fail_count"] == 1


def test_verdict_two_sources_split_is_warn_not_fail() -> None:
    # One source agrees, one misses -> caliber mismatch, not a hard fail.
    out = render_verdict([{
        "id": 1, "label": "rev", "reported_value": 100, "unit": "",
        "fetched_value": 100.5, "fetched_source": "m",
        "fetched_value2": 150, "fetched_source2": "s",
    }])
    assert out["verdict"] == "PASS"
    assert out["warn_count"] == 1 and out["fail_count"] == 0


def test_verdict_fails_points_without_fetched_value() -> None:
    out = render_verdict([
        {"id": 1, "label": "a", "reported_value": 100, "fetched_value": None},
        {"id": 2, "label": "b", "reported_value": 100,
         "fetched_value": 100, "fetched_source": "m"},
    ])
    assert out["total"] == 2
    assert out["verdict"] == "FAIL"
    assert out["fail_items"][0]["reason"] == "missing_fetched_value"


# ── tool contract ─────────────────────────────────────────────────────────


def test_tool_metadata() -> None:
    tool = ReportAuditTool()
    assert tool.name == "report_audit"
    assert tool.is_readonly is True
    assert tool.repeatable is True
    assert tool.parameters["required"] == ["command"]
    assert set(tool.parameters["properties"]["command"]["enum"]) == {"extract", "verdict"}


def test_tool_is_auto_discovered() -> None:
    assert "report_audit" in build_registry().tool_names


# ── execute ───────────────────────────────────────────────────────────────


def _run(**kwargs: Any) -> dict[str, Any]:
    return json.loads(ReportAuditTool().execute(**kwargs))


def test_execute_extract_happy() -> None:
    env = _run(command="extract", report_text=_REPORT_MD, ratio=0.5, seed=42)
    assert env["status"] == "ok"
    assert env["total_extracted"] >= 4
    assert env["sample_size"] >= 1
    assert "sample" in env and "hint" in env
    assert env["audit_token"].startswith("ra1.")
    assert len(env["audit_id"]) == 64
    assert len(env["report_sha256"]) == 64


def _matching_results(extract_env: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": point["id"],
            "fetched_value": point["reported_value"],
            "fetched_source": "authoritative-source",
        }
        for point in extract_env["sample"]
    ]


def test_execute_verdict_fail() -> None:
    extracted = _run(command="extract", report_text=_REPORT_MD, ratio=1, seed=1)
    results = _matching_results(extracted)
    results[0]["fetched_value"] *= 2
    env = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        results=results,
    )
    assert env["status"] == "ok"
    assert env["verdict"] == "FAIL"


def test_execute_verdict_requires_audit_token() -> None:
    env = _run(command="verdict", results=[{
        "id": 1, "fetched_value": 100, "fetched_source": "m",
    }])
    assert env["status"] == "error"
    assert "audit_token is required" in env["error"]


def test_execute_verdict_requires_every_extracted_sample_id() -> None:
    extracted = _run(command="extract", report_text=_REPORT_MD, ratio=1, seed=1)
    results = _matching_results(extracted)
    omitted_id = results.pop()["id"]
    env = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        results=results,
    )
    assert env["status"] == "error"
    assert f"missing sample ids: [{omitted_id}]" in env["error"]


def test_execute_verdict_rejects_fifteen_of_twenty_samples_regression() -> None:
    report = "\n".join(
        f"FinancialMetric{i:02d}: {100 + i}" for i in range(20)
    )
    extracted = _run(command="extract", report_text=report, ratio=1, seed=1)
    assert extracted["sample_size"] == 20

    env = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        results=_matching_results(extracted)[:15],
    )
    assert env["status"] == "error"
    assert "incomplete audit sample coverage" in env["error"]
    assert "missing sample ids" in env["error"]


def test_execute_verdict_rejects_duplicate_or_unexpected_ids() -> None:
    extracted = _run(command="extract", report_text=_REPORT_MD, ratio=1, seed=1)
    results = _matching_results(extracted)
    duplicate = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        results=[*results, dict(results[0])],
    )
    assert duplicate["status"] == "error"
    assert "duplicate sample ids" in duplicate["error"]

    unexpected_results = [dict(result) for result in results]
    unexpected_results[0]["id"] = 99999
    unexpected = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        results=unexpected_results,
    )
    assert unexpected["status"] == "error"
    assert "unexpected sample ids: [99999]" in unexpected["error"]


def test_execute_verdict_rejects_unverified_or_mutated_sample() -> None:
    extracted = _run(command="extract", report_text=_REPORT_MD, ratio=1, seed=1)
    results = _matching_results(extracted)
    results[0]["fetched_value"] = None
    unverified = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        results=results,
    )
    assert unverified["status"] == "error"
    assert "require fetched_value" in unverified["error"]

    results = _matching_results(extracted)
    results[0]["reported_value"] = results[0]["fetched_value"] + 1
    mutated = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        results=results,
    )
    assert mutated["status"] == "error"
    assert "reported_value does not match" in mutated["error"]


def test_execute_verdict_can_bind_exact_artifact_content() -> None:
    extracted = _run(
        command="extract",
        report_text=_REPORT_MD,
        artifact_id="report-123",
        ratio=1,
        seed=1,
    )
    results = _matching_results(extracted)
    env = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        artifact_id="report-123",
        report_text=_REPORT_MD,
        results=results,
    )
    assert env["status"] == "ok"
    assert env["verdict"] == "PASS"
    assert env["audit_status"] == "complete"
    assert env["expected_sample_size"] == extracted["sample_size"]
    assert env["artifact_id"] == "report-123"
    assert env["content_binding_verified"] is True
    assert env["report_sha256"] == extracted["report_sha256"]


def test_bound_pass_verdict_emits_persistable_report_audit_event() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    tool = ReportAuditTool(event_callback=lambda event, payload: events.append((event, payload)))
    extracted = json.loads(tool.execute(
        command="extract",
        report_text=_REPORT_MD,
        artifact_id="final_response",
        ratio=1,
        seed=1,
    ))
    verdict = json.loads(tool.execute(
        command="verdict",
        audit_token=extracted["audit_token"],
        artifact_id="final_response",
        report_text=_REPORT_MD,
        results=_matching_results(extracted),
    ))
    assert verdict["verdict"] == "PASS"
    assert events == [("report.audit_result", {"result": {
        key: verdict[key]
        for key in verdict
        if key not in {"status", "command"}
    }})]


def test_execute_verdict_rejects_wrong_artifact_or_content() -> None:
    extracted = _run(
        command="extract",
        report_text=_REPORT_MD,
        artifact_id="report-123",
        ratio=1,
        seed=1,
    )
    results = _matching_results(extracted)
    wrong_id = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        artifact_id="report-456",
        results=results,
    )
    assert wrong_id["status"] == "error"
    assert "artifact_id does not match" in wrong_id["error"]

    wrong_content = _run(
        command="verdict",
        audit_token=extracted["audit_token"],
        artifact_id="report-123",
        report_text=_REPORT_MD + "\nchanged",
        results=results,
    )
    assert wrong_content["status"] == "error"
    assert "report_text does not match" in wrong_content["error"]


def test_execute_verdict_rejects_modified_audit_token() -> None:
    extracted = _run(command="extract", report_text=_REPORT_MD, ratio=1, seed=1)
    token = extracted["audit_token"]
    modified = token[:-1] + ("0" if token[-1] != "0" else "1")
    env = _run(
        command="verdict",
        audit_token=modified,
        results=_matching_results(extracted),
    )
    assert env["status"] == "error"
    assert "integrity check failed" in env["error"]


def test_execute_missing_report_text_is_error() -> None:
    assert _run(command="extract")["status"] == "error"


def test_execute_missing_results_is_error() -> None:
    assert _run(command="verdict")["status"] == "error"


def test_execute_unknown_command_is_error() -> None:
    env = _run(command="bogus")
    assert env["status"] == "error"
    assert "unknown command" in env["error"]
