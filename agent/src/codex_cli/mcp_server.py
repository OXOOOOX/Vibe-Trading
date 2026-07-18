"""Report-scoped stdio MCP server launched only by ``CodexResearchRunner``."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from src.reports import DeepReportService
from src.reports.runtime import handle_report_workspace_command, persist_report_event
from src.tools import build_filtered_registry


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_context() -> tuple[dict[str, Any], str]:
    manifest_value = os.getenv("VIBE_CODEX_RUN_MANIFEST", "").strip()
    token = os.getenv("VIBE_CODEX_RUN_TOKEN", "")
    if not manifest_value or not token:
        raise RuntimeError("missing Codex research manifest authentication")
    path = Path(manifest_value).expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid Codex research manifest") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("invalid Codex research manifest payload")
    signature = str(raw.pop("signature", ""))
    expected = hmac.new(token.encode("utf-8"), _canonical_json(raw), hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise RuntimeError("Codex research manifest signature mismatch")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(str(raw.get("token_sha256") or ""), token_hash):
        raise RuntimeError("Codex research token mismatch")
    if int(raw.get("version") or 0) != 1:
        raise RuntimeError("unsupported Codex research manifest version")
    run_dir = Path(str(raw.get("run_dir") or "")).resolve()
    event_log = Path(str(raw.get("event_log") or "")).resolve()
    if path.parent != run_dir or event_log.parent != run_dir:
        raise RuntimeError("Codex research runtime paths are outside the signed run directory")
    reports_dir = Path(str(raw.get("reports_dir") or "")).resolve()
    report_id = str(raw.get("report_id") or "")
    if not reports_dir.is_dir() or not report_id:
        raise RuntimeError("Codex research report workspace is unavailable")
    return raw, token_hash


_CONTEXT, _TOKEN_HASH = _load_context()
_REPORTS = DeepReportService(Path(str(_CONTEXT["reports_dir"])))
_REPORT_ID = str(_CONTEXT["report_id"])
_PROFILE = str(_CONTEXT["report_profile"])
_EVENT_LOG = Path(str(_CONTEXT["event_log"]))


def _validate_report_binding() -> None:
    record = _REPORTS.require(_REPORT_ID)
    if record.profile != _PROFILE:
        raise RuntimeError("report profile does not match the signed Codex context")
    for field in ("session_id", "attempt_id"):
        expected = str(_CONTEXT.get(field) or "")
        actual = str(getattr(record, field, "") or "")
        if expected and actual and not hmac.compare_digest(expected, actual):
            raise RuntimeError(f"report {field} does not match the signed Codex context")


_validate_report_binding()


def _journal(event_type: str, data: dict[str, Any]) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "token_sha256": _TOKEN_HASH,
        "event_type": event_type,
        "data": data,
    }
    with _EVENT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _workspace_handler(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    _validate_report_binding()
    return handle_report_workspace_command(
        _REPORTS,
        _REPORT_ID,
        command,
        payload,
    )


def _event_callback(event_type: str, data: dict[str, Any]) -> None:
    _validate_report_binding()
    persist_report_event(
        _REPORTS,
        _REPORT_ID,
        _PROFILE,
        event_type,
        data,
        allowed_deterministic_commands={
            str(value)
            for value in (_CONTEXT.get("financial_rigor_commands") or [])
            if str(value)
        }
        or None,
    )
    _journal(event_type, {
        "section_id": data.get("section_id"),
        "command": data.get("command"),
        "message": event_type,
    })


_ALLOWED_TOOLS = [str(value) for value in (_CONTEXT.get("allowed_tools") or []) if str(value)]
if not _ALLOWED_TOOLS or "report_workspace" not in _ALLOWED_TOOLS:
    raise RuntimeError("signed Codex context does not include report_workspace")
_REGISTRY = build_filtered_registry(
    _ALLOWED_TOOLS,
    include_shell_tools=False,
    session_id=str(_CONTEXT.get("session_id") or ""),
    event_callback=_event_callback,
    financial_rigor_commands={
        str(value) for value in (_CONTEXT.get("financial_rigor_commands") or []) if str(value)
    } or None,
    report_workspace_handler=_workspace_handler,
)
if "report_workspace" not in _REGISTRY:
    raise RuntimeError("report_workspace tool is unavailable")

server = Server(
    "vibe-research",
    version="1.0.0",
    instructions="Report-scoped research tools. The signed context fixes the active report.",
)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools: list[types.Tool] = []
    for name in _REGISTRY.tool_names:
        tool = _REGISTRY.get(name)
        if tool is None:
            continue
        tools.append(
            types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.parameters or {"type": "object", "properties": {}},
                annotations=types.ToolAnnotations(readOnlyHint=bool(tool.is_readonly)),
            )
        )
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    _validate_report_binding()
    if name not in _REGISTRY:
        return {"status": "error", "error": f"Tool {name!r} is not allowed in this report run"}
    started = time.monotonic()
    _journal("tool.started", {"tool": name})
    raw = await asyncio.to_thread(_REGISTRY.execute, name, dict(arguments or {}))
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"status": "ok", "content": raw}
    if not isinstance(result, dict):
        result = {"status": "ok", "result": result}
    _journal(
        "tool.completed",
        {
            "tool": name,
            "status": result.get("status"),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return result


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
