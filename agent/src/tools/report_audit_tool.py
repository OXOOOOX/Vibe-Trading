"""Agent tool: research-report data audit (extract → verify → verdict).

A ``BaseTool`` wrapper around the report-audit routines that guard published
research against hallucinated numbers. Auto-discovered and registered via
``BaseTool.__subclasses__()``.

Two-phase quality gate:

1. ``extract`` — parse a markdown report, collect its numeric data points
   (tables, ``label: value`` lines, ``亿 / 万亿 / x / 倍 / %`` units), and
   draw a random sample (default 15%, clamped to [3, 30]).
2. ``verdict`` — compare each sampled point's reported value against one or two
   authoritative fetched values at a 1% tolerance; a single failed point fails
   the whole report, mixed results warn.

The tool takes text/results only — it does not fetch data itself. Pair it with
market-data / financial-statement tools: read the report, extract here, fetch
there, then verdict here. Read-only: returns JSON, writes nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from random import Random
from typing import Any, Callable

from src.agent.tools import BaseTool

# ---------------------------------------------------------------------------
# Markdown data-point extraction (handles Chinese financial reports)
# ---------------------------------------------------------------------------

_KV_LABEL_RE = re.compile(
    r"(?P<label>[一-龥A-Za-z][^|\n：:*]{1,30})[：:]\s*[~约]?\$?"
    r"(?P<num>[\d,，.]+)\s*"
    r"(?P<unit>亿[元美港]?元?|万亿|[xX倍]|%|[BMT])?"
)

_NUMUNIT_RE = re.compile(r"[~约]?\$?([\d,，.]+)\s*(亿[元美港]?元?|万亿|[xX倍]|%|[BMT])?")
_TABLE_SEP_RE = re.compile(r"^\|[\-\s|:]+\|$")

_SKIP_LABELS = {
    "来源", "sources", "source", "说明", "注意", "备注", "数据来源",
    "n/a", "—", "-", "/", "合计", "total", "单位", "趋势",
}

_QUARTER_RE = re.compile(r"(20\d{2}|Q[1-4]|\d{4}\s*Q[1-4])")


def _clean_num(s: str) -> float | None:
    """Normalise a numeric string with commas (ASCII or wide) to float."""
    s = s.replace(",", "").replace("，", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _is_valid_label(label: str) -> bool:
    """True if a label looks like a meaningful financial field name."""
    label = label.strip()
    if len(label) < 2:
        return False
    if re.fullmatch(r"[\d\s年季度Q]+", label):
        return False
    if re.match(r"^[+\-*#|~$>_`]", label):
        return False
    if "**" in label or "`" in label or "__" in label:
        return False
    if re.fullmatch(r"[+-]?\d+(\.\d+)?%", label):
        return False
    if label.lower() in _SKIP_LABELS:
        return False
    return True


def _parse_md_tables(lines: list[str]) -> list[tuple[str, str, float, str, int, str]]:
    """Parse markdown tables into (row_label, col_header, value, unit, lineno, raw)."""
    results: list[tuple[str, str, float, str, int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "|" in line and not _TABLE_SEP_RE.match(line):
            headers_raw = [h.strip().strip("*_").strip() for h in line.split("|")]
            headers_raw = [h for h in headers_raw if h]
            if i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1].strip()):
                i += 2  # skip the separator row
                while i < len(lines):
                    dline = lines[i].strip()
                    if not dline or not dline.startswith("|"):
                        break
                    cells = [c.strip().strip("*_~").strip() for c in dline.split("|")]
                    cells = [c for c in cells if c != ""]
                    if len(cells) < 2:
                        i += 1
                        continue
                    row_label = cells[0]
                    for col_idx, cell in enumerate(cells[1:], start=1):
                        col_header = (
                            headers_raw[col_idx]
                            if col_idx < len(headers_raw)
                            else f"col{col_idx}"
                        )
                        m = _NUMUNIT_RE.search(cell)
                        if m:
                            val = _clean_num(m.group(1))
                            unit = (m.group(2) or "").strip()
                            if val and val != 0 and val < 1e15:
                                results.append((row_label, col_header, val, unit, i + 1, dline))
                    i += 1
                continue
        i += 1
    return results


def extract_data_points(md_text: str) -> list[dict[str, Any]]:
    """Extract recognizable financial data points from a markdown report.

    Covers multi-column tables and ``label: value unit`` lines. De-duplicates
    by (label, value, unit).

    Args:
        md_text: Full markdown text of the report.

    Returns:
        List of point dicts: ``{id, label, reported_value, unit, raw_text,
        line_number}``.
    """
    points: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(label: str, val: float | None, unit: str, lineno: int, raw: str) -> None:
        label = re.sub(r"[*_`]+", "", label).strip()
        if not _is_valid_label(label):
            return
        if val is None or val == 0 or val > 1e15:
            return
        if _QUARTER_RE.fullmatch(label.strip()):
            return
        key = f"{label}|{round(val, 4)}|{unit}"
        if key in seen:
            return
        seen.add(key)
        points.append({
            "id": len(points) + 1,
            "label": label,
            "reported_value": val,
            "unit": unit,
            "raw_text": raw[:120],
            "line_number": lineno,
        })

    lines = md_text.split("\n")
    in_code = False

    # 1. Multi-column tables.
    _YOY_HEADERS = {"YOY", "YOY增速", "增速", "同比", "变化", "趋势", "说明", "备注"}
    for row_label, col_header, val, unit, lineno, raw in _parse_md_tables(lines):
        if not _is_valid_label(row_label):
            continue
        if col_header.upper() in _YOY_HEADERS:
            continue
        label = f"{row_label} · {col_header}" if (col_header and col_header != row_label) else row_label
        _add(label, val, unit, lineno, raw)

    # 2. ``label: value unit`` lines.
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or stripped.startswith("> ") or re.match(r"^#{1,6}\s", stripped):
            continue
        if "|" in stripped:
            continue  # handled as a table above
        for m in _KV_LABEL_RE.finditer(stripped):
            _add(
                m.group("label"),
                _clean_num(m.group("num")),
                (m.group("unit") or "").strip(),
                lineno,
                stripped,
            )

    return points


def sample_points(
    points: list[dict[str, Any]], ratio: float = 0.15, seed: int | None = None,
) -> list[dict[str, Any]]:
    """Draw a random sample, clamped to [3, 30], ordered by line number.

    Args:
        points: Data points returned by :func:`extract_data_points`.
        ratio: Sampling fraction.
        seed: Optional seed for reproducibility.

    Returns:
        Sampled points sorted by ``line_number``.
    """
    n = max(3, min(30, math.ceil(len(points) * ratio)))
    n = min(n, len(points))
    if n == 0:
        return []
    rng = Random(seed)
    sampled = rng.sample(points, n)
    return sorted(sampled, key=lambda p: p["line_number"])


# ---------------------------------------------------------------------------
# Stateless extract -> verdict binding
# ---------------------------------------------------------------------------

_AUDIT_TOKEN_VERSION = 1
_AUDIT_TOKEN_PREFIX = "ra1"


def _canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for hashes and audit tokens."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sample_identity(point: dict[str, Any]) -> dict[str, Any]:
    """Keep the immutable fields needed to replay a sampled point safely."""
    return {
        "id": point.get("id"),
        "label": point.get("label", ""),
        "reported_value": point.get("reported_value"),
        "unit": point.get("unit", ""),
        "line_number": point.get("line_number", 0),
    }


def _encode_audit_token(manifest: dict[str, Any]) -> tuple[str, str]:
    """Encode a portable manifest and return ``(token, audit_id)``.

    The trailing digest is an integrity checksum, not an authentication
    signature. It catches truncation and accidental edits while keeping the
    extract/verdict workflow state-free across worker processes.
    """
    payload = _canonical_json(manifest)
    digest = hashlib.sha256(payload).hexdigest()
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{_AUDIT_TOKEN_PREFIX}.{encoded}.{digest}", digest


def _decode_audit_token(token: str) -> tuple[dict[str, Any], str]:
    """Decode and integrity-check an audit token."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("audit_token is required for verdict")
    try:
        prefix, encoded, supplied_digest = token.split(".", 2)
    except ValueError as exc:
        raise ValueError("invalid audit_token format") from exc
    if prefix != _AUDIT_TOKEN_PREFIX:
        raise ValueError("unsupported audit_token version")
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("invalid audit_token payload") from exc
    actual_digest = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(supplied_digest, actual_digest):
        raise ValueError("audit_token integrity check failed")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid audit_token manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != _AUDIT_TOKEN_VERSION:
        raise ValueError("unsupported audit_token manifest")
    if not isinstance(manifest.get("sample"), list):
        raise ValueError("audit_token has no sample manifest")
    return manifest, actual_digest


def _bind_results_to_manifest(
    results: list[dict[str, Any]], manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate complete sample coverage and restore immutable report fields."""
    expected_points = manifest["sample"]
    expected_by_id: dict[int, dict[str, Any]] = {}
    for point in expected_points:
        point_id = point.get("id") if isinstance(point, dict) else None
        if not isinstance(point_id, int) or isinstance(point_id, bool):
            raise ValueError("audit_token contains an invalid sample id")
        if point_id in expected_by_id:
            raise ValueError(f"audit_token contains duplicate sample id: {point_id}")
        expected_by_id[point_id] = point

    supplied_by_id: dict[int, dict[str, Any]] = {}
    duplicate_ids: list[int] = []
    invalid_ids: list[Any] = []
    for item in results:
        if not isinstance(item, dict):
            raise ValueError("each verdict result must be an object")
        point_id = item.get("id")
        if not isinstance(point_id, int) or isinstance(point_id, bool):
            invalid_ids.append(point_id)
            continue
        if point_id in supplied_by_id:
            duplicate_ids.append(point_id)
        supplied_by_id[point_id] = item

    if invalid_ids:
        raise ValueError(f"verdict results contain invalid sample ids: {invalid_ids}")
    if duplicate_ids:
        raise ValueError(
            f"verdict results contain duplicate sample ids: {sorted(set(duplicate_ids))}"
        )

    expected_ids = set(expected_by_id)
    supplied_ids = set(supplied_by_id)
    missing_ids = sorted(expected_ids - supplied_ids)
    unexpected_ids = sorted(supplied_ids - expected_ids)
    if missing_ids or unexpected_ids:
        details: list[str] = []
        if missing_ids:
            details.append(f"missing sample ids: {missing_ids}")
        if unexpected_ids:
            details.append(f"unexpected sample ids: {unexpected_ids}")
        raise ValueError("incomplete audit sample coverage; " + "; ".join(details))

    bound: list[dict[str, Any]] = []
    unverified_ids: list[int] = []
    for expected in expected_points:
        point_id = expected["id"]
        supplied = supplied_by_id[point_id]
        if supplied.get("fetched_value") is None:
            unverified_ids.append(point_id)

        # Identity fields are optional in the verdict request. When callers
        # include them, reject any attempt to audit a different value/label.
        for field in ("label", "unit"):
            if field in supplied and supplied[field] != expected[field]:
                raise ValueError(
                    f"sample id {point_id} {field} does not match extract manifest"
                )
        if "reported_value" in supplied:
            try:
                same_value = float(supplied["reported_value"]) == float(
                    expected["reported_value"]
                )
            except (TypeError, ValueError):
                same_value = False
            if not same_value:
                raise ValueError(
                    f"sample id {point_id} reported_value does not match extract manifest"
                )

        canonical = dict(supplied)
        canonical.update(expected)
        bound.append(canonical)

    if unverified_ids:
        raise ValueError(
            "all sampled points require fetched_value; unverified sample ids: "
            f"{unverified_ids}"
        )
    return bound


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

_TOLERANCE = 0.01  # 1% absolute relative tolerance


def _pct_diff(reported: float, fetched: float) -> float:
    """Absolute relative percent difference (inf when reported is 0 and fetched isn't)."""
    if reported == 0:
        return 0.0 if fetched == 0 else float("inf")
    return abs(reported - fetched) / abs(reported)


def render_verdict(results: list[dict[str, Any]], report_name: str = "") -> dict[str, Any]:
    """Render a PASS/FAIL verdict from per-point verification results.

    Each result with a ``fetched_value`` is judged against the reported value
    at :data:`_TOLERANCE`. A second source (``fetched_value2``) may be supplied;
    both sources must pass for a point to pass, both fail to fail, otherwise the
    point warns (treated as a caliber mismatch, not a failure).

    Args:
        results: List of verification objects — ``{id, label, reported_value,
            unit, fetched_value, fetched_source, (optional) fetched_value2,
            fetched_source2, ...}``.
        report_name: Optional report name for display.

    Returns:
        Verdict dict: ``verdict`` is ``PASS`` (zero failures) or ``FAIL``.
    """
    fail_items: list[dict[str, Any]] = []
    warn_items: list[dict[str, Any]] = []
    pass_count = 0
    total = 0

    for item in results:
        fetched = item.get("fetched_value")
        if fetched is None:
            total += 1
            fail_items.append({
                "id": item.get("id"),
                "label": item.get("label", "?"),
                "reported": item.get("reported_value"),
                "unit": item.get("unit", ""),
                "reason": "missing_fetched_value",
                "raw_text": item.get("raw_text", ""),
                "line_number": item.get("line_number", 0),
            })
            continue
        total += 1
        reported = float(item.get("reported_value", 0))
        label = item.get("label", "?")
        unit = item.get("unit", "")
        source = item.get("fetched_source", "?")
        fetched = float(fetched)
        diff1 = _pct_diff(reported, fetched)

        fetched2 = item.get("fetched_value2")
        source2 = item.get("fetched_source2", "")
        pass1 = diff1 <= _TOLERANCE

        if fetched2 is None:
            # Single source: pass or fail outright.
            if pass1:
                pass_count += 1
            else:
                fail_items.append({
                    "id": item.get("id"), "label": label,
                    "reported": reported, "unit": unit,
                    "fetched": fetched, "source": source,
                    "fetched2": None, "source2": source2,
                    "diff1_pct": round(diff1 * 100, 2), "diff2_pct": None,
                    "raw_text": item.get("raw_text", ""),
                    "line_number": item.get("line_number", 0),
                })
        else:
            # Two sources: PASS only if both agree within tolerance; FAIL if
            # both miss; otherwise WARN (a caliber / GAAP mismatch, not a fail).
            f2 = float(fetched2)
            diff2 = _pct_diff(reported, f2)
            pass2 = diff2 <= _TOLERANCE
            if pass1 and pass2:
                pass_count += 1
            elif not pass1 and not pass2:
                fail_items.append({
                    "id": item.get("id"), "label": label,
                    "reported": reported, "unit": unit,
                    "fetched": fetched, "source": source,
                    "fetched2": f2, "source2": source2,
                    "diff1_pct": round(diff1 * 100, 2),
                    "diff2_pct": round(diff2 * 100, 2),
                    "raw_text": item.get("raw_text", ""),
                    "line_number": item.get("line_number", 0),
                })
            else:
                warn_items.append({
                    "id": item.get("id"), "label": label,
                    "reported": reported, "unit": unit,
                    "diff1_pct": round(diff1 * 100, 2),
                    "diff2_pct": round(diff2 * 100, 2),
                })

    fail_count = len(fail_items)
    verdict = "PASS" if fail_count == 0 else "FAIL"
    return {
        "verdict": verdict,
        "report_name": report_name,
        "total": total,
        "pass_count": pass_count,
        "warn_count": len(warn_items),
        "fail_count": fail_count,
        "fail_items": fail_items,
        "warn_items": warn_items,
    }


class ReportAuditTool(BaseTool):
    """Research-report data audit gate (extract sample → verdict)."""

    name = "report_audit"
    description = (
        "Audit a research report's numeric data points for accuracy before "
        "publishing. Two sub-commands via `command`: 'extract' parses a "
        "markdown report and returns a random sample (~15%, clamped 3-30) of "
        "its financial data points to verify; 'verdict' compares each sampled "
        "point's reported value against one or two authoritative fetched values "
        "at 1% tolerance and returns a PASS/FAIL gate (one failure fails the "
        "report). The extract response includes an audit_token that binds the "
        "sample to the exact report content. Workflow: read the report, extract "
        "here, verify every sample point against market-data/financial-statement "
        "tools, then submit all sample IDs plus audit_token to verdict."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["extract", "verdict"],
                "description": "Which audit phase to run.",
            },
            "report_text": {
                "type": "string",
                "description": "extract: full markdown report. verdict (optional): "
                               "the final artifact text to confirm the same digest.",
            },
            "ratio": {
                "type": "number", "default": 0.15,
                "description": "extract: fraction of data points to sample.",
            },
            "seed": {
                "type": "integer",
                "description": "extract: random seed for reproducible sampling.",
            },
            "results": {
                "type": "array", "items": {"type": "object"},
                "description": "verdict: list of {id, label, reported_value, unit, "
                               "fetched_value, fetched_source, (optional) "
                               "fetched_value2, fetched_source2}.",
            },
            "audit_token": {
                "type": "string",
                "description": "verdict: opaque token returned by extract; required "
                               "to prove complete coverage of that exact sample.",
            },
            "artifact_id": {
                "type": "string",
                "description": "Optional stable artifact/report ID. If supplied to "
                               "extract it must be repeated for verdict.",
            },
            "report_name": {
                "type": "string",
                "description": "verdict: report name for display.",
            },
        },
        "required": ["command"],
    }
    is_readonly = True
    repeatable = True  # loop.py dedups non-repeatable tools by name; extract
                       # and verdict are commonly called back-to-back.

    def __init__(
        self,
        default_session_id: str | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.default_session_id = default_session_id
        self.event_callback = event_callback

    def execute(self, **kwargs: Any) -> str:
        """Dispatch to ``extract`` or ``verdict`` and return a JSON envelope.

        Args:
            **kwargs: ``command`` plus the inputs for that phase.

        Returns:
            JSON string — ``status="ok"`` with the result on success,
            ``status="error"`` with a message otherwise.
        """
        command = str(kwargs.get("command") or "").strip()
        try:
            if command == "extract":
                report_text = kwargs.get("report_text")
                if not isinstance(report_text, str) or not report_text.strip():
                    return _err("report_text (non-empty markdown) is required for extract")
                ratio = float(kwargs.get("ratio") or 0.15)
                seed = kwargs.get("seed")
                seed = int(seed) if seed is not None else None
                points = extract_data_points(report_text)
                sampled = sample_points(points, ratio=ratio, seed=seed)
                artifact_id = str(kwargs.get("artifact_id") or "").strip()
                manifest = {
                    "version": _AUDIT_TOKEN_VERSION,
                    "report_sha256": _sha256_text(report_text),
                    "artifact_id": artifact_id or None,
                    "sample": [_sample_identity(point) for point in sampled],
                }
                audit_token, audit_id = _encode_audit_token(manifest)
                result: dict[str, Any] = {
                    "total_extracted": len(points),
                    "sample_size": len(sampled),
                    "ratio": ratio,
                    "seed": seed,
                    "sample": sampled,
                    "audit_id": audit_id,
                    "audit_token": audit_token,
                    "report_sha256": manifest["report_sha256"],
                    "artifact_id": manifest["artifact_id"],
                    "hint": (
                        "Fetch every point in `sample` from an authoritative "
                        "source, then call 'verdict' with this audit_token and "
                        "one result for every sample id. For content binding, "
                        "also pass the final artifact as report_text."
                    ),
                }
            elif command == "verdict":
                results = kwargs.get("results")
                if not isinstance(results, list) or not results:
                    return _err("results (non-empty list) is required for verdict")
                manifest, audit_id = _decode_audit_token(kwargs.get("audit_token"))
                artifact_id = str(kwargs.get("artifact_id") or "").strip()
                expected_artifact_id = str(manifest.get("artifact_id") or "")
                if expected_artifact_id and artifact_id != expected_artifact_id:
                    return _err(
                        "artifact_id does not match extract manifest "
                        f"(expected {expected_artifact_id!r})"
                    )

                verdict_report_text = kwargs.get("report_text")
                content_binding_verified = False
                if verdict_report_text is not None:
                    if not isinstance(verdict_report_text, str) or not verdict_report_text.strip():
                        return _err(
                            "report_text must be non-empty when supplied for verdict"
                        )
                    content_binding_verified = hmac.compare_digest(
                        _sha256_text(verdict_report_text),
                        str(manifest.get("report_sha256") or ""),
                    )
                    if not content_binding_verified:
                        return _err(
                            "report_text does not match the report audited by extract"
                        )

                bound_results = _bind_results_to_manifest(results, manifest)
                result = render_verdict(
                    bound_results,
                    report_name=str(kwargs.get("report_name") or ""),
                )
                result.update({
                    "audit_id": audit_id,
                    "audit_status": "complete",
                    "expected_sample_size": len(manifest["sample"]),
                    "report_sha256": manifest.get("report_sha256"),
                    "artifact_id": manifest.get("artifact_id"),
                    "content_binding_verified": content_binding_verified,
                })
                if self.event_callback is not None:
                    self.event_callback("report.audit_result", {"result": dict(result)})
            else:
                return _err(f"unknown command: {command}")
        except Exception as exc:  # noqa: BLE001 - surface a clean tool error
            return json.dumps(
                {"status": "error", "command": command, "error": str(exc)},
                ensure_ascii=False,
            )
        return json.dumps(
            {"status": "ok", "command": command, **result}, ensure_ascii=False,
        )


def _err(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)
