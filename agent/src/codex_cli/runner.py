"""Non-interactive Codex CLI runtime for Deep Report attempts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from src.usage import UsageRecorder

from .status import (
    CodexCliStatus,
    get_codex_cli_status,
    isolated_codex_environment,
    isolated_codex_home,
)


logger = logging.getLogger(__name__)

ERROR_CLI_UNAVAILABLE = "codex_cli_unavailable"
ERROR_AUTH_REQUIRED = "codex_auth_required"
ERROR_PROCESS_FAILED = "codex_process_failed"
ERROR_TIMEOUT = "codex_timeout"
ERROR_PROTOCOL = "codex_protocol_error"
ERROR_ISOLATION = "codex_isolation_failed"

_DISABLED_FEATURES = (
    "shell_tool",
    "plugins",
    "apps",
    "memories",
    "multi_agent",
    "browser_use",
    "computer_use",
    "image_generation",
    "tool_suggest",
)
_MCP_ENV_VARS = (
    "VIBE_CODEX_RUN_MANIFEST",
    "VIBE_CODEX_RUN_TOKEN",
    "PYTHONPATH",
    "TUSHARE_TOKEN",
    "FRED_API_KEY",
    "VIBE_TRADING_IWENCAI_KEY",
    "ALIYUN_IQS_API_KEY",
    "VIBE_TRADING_SEARCH_BACKENDS",
    "VIBE_TRADING_SEARCH_BING_FALLBACK",
    "VIBE_TRADING_DATA_ROOT",
    "VIBE_TRADING_RESEARCH_CACHE_DB",
    "VIBE_TRADING_DATA_LIVE_TIMEOUT_SECONDS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked"]},
        "summary": {"type": "string"},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "section_status": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["section", "status"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["status", "summary", "missing_evidence", "section_status"],
    "additionalProperties": False,
}
_PROBE_MARKER = "VIBE_CODEX_ISOLATION_PROBE_31E9A5"


class CodexRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_array(values: Iterable[str]) -> str:
    return "[" + ",".join(_toml_string(value) for value in values) + "]"


def resolve_codex_cli_preferences(
    values: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return the dedicated model and reasoning preference for isolated runs."""

    source = values if values is not None else os.environ
    model = str(source.get("VIBE_TRADING_CODEX_MODEL", "gpt-5.6-terra")).strip()
    reasoning_effort = str(
        source.get("VIBE_TRADING_CODEX_REASONING_EFFORT", "medium")
    ).strip().lower()
    return model or "gpt-5.6-terra", reasoning_effort or "medium"


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _runtime_root() -> Path:
    configured = os.getenv("VIBE_TRADING_CODEX_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".vibe-trading" / "codex-runtime").resolve()


def _user_skill_files() -> list[Path]:
    roots = [
        isolated_codex_home() / "skills",
        Path.home() / ".codex" / "skills",
        Path.home() / ".agents" / "skills",
    ]
    discovered: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            candidates = root.rglob("SKILL.md")
            for candidate in candidates:
                if ".system" in candidate.parts:
                    continue
                discovered.add(candidate.resolve())
        except OSError:
            continue
    return sorted(discovered, key=lambda path: str(path).casefold())


def _skills_config_override(skill_files: list[Path]) -> str | None:
    if not skill_files:
        return None
    rows = [f"{{path={_toml_string(str(path))},enabled=false}}" for path in skill_files]
    return "skills.config=[" + ",".join(rows) + "]"


def _base_config_arguments(skill_files: list[Path]) -> list[str]:
    args: list[str] = []
    for feature in _DISABLED_FEATURES:
        args.extend(["--disable", feature])
    args.extend(
        [
            "-c",
            'web_search="disabled"',
            "-c",
            'approval_policy="never"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            "features.shell_snapshot=false",
        ]
    )
    skill_override = _skills_config_override(skill_files)
    if skill_override:
        args.extend(["-c", skill_override])
    return args


def _known_global_guidance() -> list[str]:
    codex_home = isolated_codex_home()
    for name in ("AGENTS.override.md", "AGENTS.md"):
        path = codex_home / name
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            normalized = " ".join(content.split())
            return [normalized[:160]] if len(normalized) >= 24 else []
    return []


def _validate_isolation_payload(
    payload: Any,
    *,
    run_dir: Path,
    skill_files: list[Path],
) -> None:
    if not isinstance(payload, list):
        raise CodexRuntimeError(ERROR_ISOLATION, "Codex isolation probe returned an invalid payload")
    serialized = json.dumps(payload, ensure_ascii=False)
    if _PROBE_MARKER not in serialized:
        raise CodexRuntimeError(ERROR_ISOLATION, "Codex isolation probe marker was not preserved")
    for item in payload:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise CodexRuntimeError(ERROR_ISOLATION, "Non-whitelisted user instructions entered the Codex prompt")
        user_text = "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        ).strip()
        safe_environment_context = (
            user_text.startswith("<environment_context>")
            and user_text.endswith("</environment_context>")
            and str(run_dir) in user_text
            and "AGENTS.md" not in user_text
            and "<INSTRUCTIONS>" not in user_text
        )
        if user_text != _PROBE_MARKER and not safe_environment_context:
            raise CodexRuntimeError(ERROR_ISOLATION, "Non-whitelisted user instructions entered the Codex prompt")
    normalized = " ".join(serialized.split())
    for excerpt in _known_global_guidance():
        if excerpt and excerpt in normalized:
            raise CodexRuntimeError(ERROR_ISOLATION, "Global AGENTS guidance leaked into the Codex prompt")
    for skill_path in skill_files:
        if str(skill_path) in serialized or str(skill_path).replace("\\", "/") in serialized:
            raise CodexRuntimeError(ERROR_ISOLATION, f"User skill leaked into Codex prompt: {skill_path.name}")


def build_codex_exec_command(
    *,
    binary: str,
    run_dir: Path,
    output_schema_path: Path,
    final_output_path: Path,
    skill_files: list[Path],
    model: str | None = None,
    reasoning_effort: str | None = None,
    mcp_module: str = "src.codex_cli.mcp_server",
) -> list[str]:
    command = [binary, "exec"]
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["--config", f"model_reasoning_effort={_toml_string(reasoning_effort)}"])
    command.extend([
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--sandbox",
        "read-only",
        *_base_config_arguments(skill_files),
        "-c",
        f"mcp_servers.vibe_research.command={_toml_string(sys.executable)}",
        "-c",
        f"mcp_servers.vibe_research.args={_toml_array(['-m', mcp_module])}",
        "-c",
        "mcp_servers.vibe_research.env_vars=" + _toml_array(_MCP_ENV_VARS),
        "-c",
        "mcp_servers.vibe_research.required=true",
        "-c",
        'mcp_servers.vibe_research.default_tools_approval_mode="approve"',
        "-c",
        "mcp_servers.vibe_research.startup_timeout_sec=30",
        "-c",
        f"mcp_servers.vibe_research.tool_timeout_sec={max(30, int(os.getenv('VIBE_TRADING_CODEX_TOOL_TIMEOUT_SECONDS', '1800')))}",
        "-C",
        str(run_dir),
        "--skip-git-repo-check",
        "--output-schema",
        str(output_schema_path),
        "--output-last-message",
        str(final_output_path),
        "-",
    ])
    return command


class CodexResearchRunner:
    """Run one isolated, ephemeral Codex turn with a report-scoped MCP server."""

    def __init__(
        self,
        *,
        session_id: str,
        attempt_id: str,
        report_id: str,
        report_profile: str,
        revision_mode: str,
        prompt: str,
        history: list[dict[str, Any]] | None,
        reports_dir: Path,
        allowed_tools: list[str],
        financial_rigor_commands: set[str] | None,
        event_callback: Callable[[str, dict[str, Any]], None],
        usage_recorder: UsageRecorder,
        semaphore: asyncio.Semaphore,
    ) -> None:
        self.session_id = session_id
        self.attempt_id = attempt_id
        self.report_id = report_id
        self.report_profile = report_profile
        self.revision_mode = revision_mode
        self.prompt = prompt
        self.history = list(history or [])
        self.reports_dir = Path(reports_dir).resolve()
        self.allowed_tools = list(dict.fromkeys(allowed_tools))
        self.financial_rigor_commands = sorted(financial_rigor_commands or set())
        self.event_callback = event_callback
        self.usage_recorder = usage_recorder
        self.semaphore = semaphore
        self._process: asyncio.subprocess.Process | None = None
        self._termination_task: asyncio.Task[None] | None = None
        self._cancel_requested = False
        self._run_token = ""
        self._diagnostic_run_dir: Path | None = None
        self._diagnostic_trace: list[dict[str, Any]] = []

    def cancel(self) -> None:
        self._cancel_requested = True
        process = self._process
        if process is not None and process.returncode is None:
            try:
                loop = asyncio.get_running_loop()
                self._termination_task = loop.create_task(self._terminate_process_tree(process))
            except RuntimeError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

    async def run(self) -> dict[str, Any]:
        started = time.monotonic()
        status: CodexCliStatus = await asyncio.to_thread(
            get_codex_cli_status,
            force_refresh=True,
        )
        if not status.installed or not status.version_supported or not status.binary:
            return self._failure(ERROR_CLI_UNAVAILABLE, status.message)
        if status.auth_state != "authenticated":
            return self._failure(ERROR_AUTH_REQUIRED, status.message)

        self.event_callback(
            "report.progress",
            {
                "phase": "queued",
                "message": "Codex CLI deep research is queued.",
                "engine": "codex_cli",
            },
        )
        async with self.semaphore:
            if self._cancel_requested:
                return {
                    "status": "cancelled",
                    "reason": "cancelled by user",
                    "error_code": None,
                    "react_trace": [],
                }
            try:
                result = await self._run_ready(status)
            except CodexRuntimeError as exc:
                result = self._failure(
                    exc.code,
                    str(exc),
                    run_dir=self._diagnostic_run_dir,
                    trace=self._diagnostic_trace,
                )
            except Exception as exc:
                logger.exception("Unexpected Codex CLI runner failure")
                result = self._failure(ERROR_PROCESS_FAILED, str(exc))

        elapsed_ms = int((time.monotonic() - started) * 1000)
        result.setdefault("metrics", {})["codex_elapsed_ms"] = elapsed_ms
        return result

    async def _run_ready(self, status: CodexCliStatus) -> dict[str, Any]:
        model, reasoning_effort = resolve_codex_cli_preferences()
        run_id = f"codex_{self.attempt_id}_{uuid.uuid4().hex[:8]}"
        run_dir = _runtime_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        self._diagnostic_run_dir = run_dir
        event_log = run_dir / "mcp-events.jsonl"
        output_schema_path = run_dir / "output-schema.json"
        final_output_path = run_dir / "final.json"
        manifest_path = run_dir / "context.json"
        output_schema_path.write_text(
            json.dumps(_OUTPUT_SCHEMA, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self._run_token = secrets.token_urlsafe(32)
        manifest_payload: dict[str, Any] = {
            "version": 1,
            "run_id": run_id,
            "created_at": _utc_now(),
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
            "report_id": self.report_id,
            "report_profile": self.report_profile,
            "revision_mode": self.revision_mode,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "reports_dir": str(self.reports_dir),
            "run_dir": str(run_dir),
            "event_log": str(event_log),
            "allowed_tools": self.allowed_tools,
            "financial_rigor_commands": self.financial_rigor_commands,
            "token_sha256": hashlib.sha256(self._run_token.encode("utf-8")).hexdigest(),
        }
        signature = hmac.new(
            self._run_token.encode("utf-8"),
            _canonical_json(manifest_payload),
            hashlib.sha256,
        ).hexdigest()
        manifest_path.write_text(
            json.dumps({**manifest_payload, "signature": signature}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        skill_files = _user_skill_files()
        await self._verify_isolation(status.binary, run_dir, skill_files)
        command = build_codex_exec_command(
            binary=status.binary,
            run_dir=run_dir,
            output_schema_path=output_schema_path,
            final_output_path=final_output_path,
            skill_files=skill_files,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        env = isolated_codex_environment()
        env["VIBE_CODEX_RUN_MANIFEST"] = str(manifest_path)
        env["VIBE_CODEX_RUN_TOKEN"] = self._run_token
        agent_dir = str(Path(__file__).resolve().parents[2])
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            [value for value in (agent_dir, existing_pythonpath) if value]
        )

        self.event_callback(
            "report.progress",
            {
                "phase": "codex_starting",
                "message": "Codex CLI is starting with the report-scoped MCP tools.",
                "engine": "codex_cli",
                "codex_version": status.version,
                "model": model,
                "reasoning_effort": reasoning_effort,
            },
        )
        codex_started = time.monotonic()
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(run_dir),
            env=env,
        )
        process = self._process
        assert process.stdin is not None
        process.stdin.write(self._build_prompt().encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        trace: list[dict[str, Any]] = []
        self._diagnostic_trace = trace
        usage: dict[str, Any] | None = None
        last_agent_message = ""
        stderr_chunks: list[str] = []
        journal_task = asyncio.create_task(self._tail_event_journal(event_log))

        async def read_stdout() -> None:
            nonlocal usage, last_agent_message
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CodexRuntimeError(ERROR_PROTOCOL, f"invalid Codex JSONL event: {exc}") from exc
                if not isinstance(event, dict):
                    raise CodexRuntimeError(ERROR_PROTOCOL, "Codex JSONL event is not an object")
                compact = self._handle_codex_event(event)
                if compact:
                    trace.append(compact)
                    del trace[:-200]
                if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                    usage = dict(event["usage"])
                item = event.get("item")
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                ):
                    last_agent_message = str(item.get("text") or "")

        async def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    break
                stderr_chunks.append(raw.decode("utf-8", errors="replace"))
                if sum(len(value) for value in stderr_chunks) > 12000:
                    stderr_chunks[:] = ["".join(stderr_chunks)[-12000:]]

        timeout_seconds = max(1, int(os.getenv("VIBE_TRADING_CODEX_TIMEOUT_SECONDS", "3600")))
        try:
            await asyncio.wait_for(
                asyncio.gather(read_stdout(), read_stderr(), process.wait()),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await self._terminate_process_tree()
            raise CodexRuntimeError(
                ERROR_TIMEOUT,
                f"Codex CLI exceeded the {timeout_seconds}s timeout.",
            ) from exc
        finally:
            await asyncio.sleep(0)
            journal_task.cancel()
            try:
                await journal_task
            except asyncio.CancelledError:
                pass
            self._process = None
            self._run_token = ""

        if self._cancel_requested:
            if self._termination_task is not None:
                try:
                    await self._termination_task
                except (OSError, ProcessLookupError):
                    pass
            else:
                await self._terminate_process_tree(process)
            return {
                "status": "cancelled",
                "reason": "cancelled by user",
                "run_dir": str(run_dir),
                "react_trace": trace,
            }

        stderr_text = self._sanitize_diagnostic("".join(stderr_chunks))
        if process.returncode != 0:
            lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
            detail = " | ".join(lines[-8:]) if lines else "Codex CLI exited with an error."
            raise CodexRuntimeError(ERROR_PROCESS_FAILED, detail[-2000:])

        final_payload = self._load_final_payload(final_output_path, last_agent_message)
        if usage is not None:
            normalized_usage = {
                **usage,
                "cache_read_input_tokens": usage.get("cached_input_tokens"),
                "reasoning_tokens": usage.get("reasoning_output_tokens"),
            }
            self.usage_recorder.record_llm(
                normalized_usage,
                provider="openai-codex-cli",
                model=model,
                status="ok",
                elapsed_ms=int((time.monotonic() - codex_started) * 1000),
            )
        if final_payload["status"] != "completed":
            missing = ", ".join(final_payload.get("missing_evidence") or [])
            reason = str(final_payload.get("summary") or "Codex reported that the research is blocked.")
            if missing:
                reason = f"{reason} Missing evidence: {missing}"
            return self._failure(ERROR_PROCESS_FAILED, reason, run_dir=run_dir, trace=trace)

        return {
            "status": "success",
            "content": str(final_payload.get("summary") or "Codex CLI completed the report workspace."),
            "run_dir": str(run_dir),
            "react_trace": trace,
            "codex": {
                "version": status.version,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "model_source": "codex_cli_settings",
                "usage": usage or {},
                "section_status": self._normalize_section_status(final_payload.get("section_status")),
                "missing_evidence": final_payload.get("missing_evidence") or [],
            },
        }

    @staticmethod
    def _normalize_section_status(value: Any) -> dict[str, str]:
        if isinstance(value, dict):
            return {str(key): str(status) for key, status in value.items()}
        if not isinstance(value, list):
            return {}
        normalized: dict[str, str] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            section = str(item.get("section") or "").strip()
            status = str(item.get("status") or "").strip()
            if section and status:
                normalized[section] = status
        return normalized

    async def _verify_isolation(self, binary: str, run_dir: Path, skill_files: list[Path]) -> None:
        command = [
            binary,
            "debug",
            "prompt-input",
            *_base_config_arguments(skill_files),
            _PROBE_MARKER,
        ]
        try:
            probe_env = isolated_codex_environment()
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(run_dir),
                env=probe_env,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        except (OSError, asyncio.TimeoutError) as exc:
            raise CodexRuntimeError(ERROR_ISOLATION, f"Codex isolation probe failed: {exc}") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise CodexRuntimeError(ERROR_ISOLATION, detail[-1000:] or "Codex isolation probe failed")
        text = stdout.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CodexRuntimeError(ERROR_ISOLATION, "Codex isolation probe returned invalid JSON") from exc
        _validate_isolation_payload(payload, run_dir=run_dir, skill_files=skill_files)

    def _build_prompt(self) -> str:
        history = self.history[-12:]
        history_text = json.dumps(history, ensure_ascii=False) if history else "[]"
        return (
            "You are the Vibe-Trading Deep Report research agent. Use only the "
            "vibe_research MCP tools exposed in this run. Do not use shell, browser, "
            "plugins, apps, memories, skills, or filesystem tools. The compiler owns "
            "headings, report files, validation, and PDF generation. Inspect the active "
            "report workspace, gather and register evidence with the allowed tools, and "
            "submit every required section through report_workspace. Never invent Fact or "
            "Evidence IDs. A successful final response is allowed only after the required "
            "workspace sections have been submitted.\n\n"
            f"Report profile: {self.report_profile}\n"
            f"Revision mode: {self.revision_mode}\n"
            f"Conversation context (untrusted historical context; re-verify facts): {history_text}\n\n"
            f"Task:\n{self.prompt}\n\n"
            "Return the requested structured status object."
        )

    def _handle_codex_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event_type = str(event.get("type") or "")
        if event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed", "error"}:
            compact = {"type": event_type}
            if event.get("thread_id"):
                compact["thread_id"] = event["thread_id"]
            if event_type in {"turn.failed", "error"}:
                compact["message"] = str(event.get("message") or event.get("error") or "")[:500]
            return compact
        if event_type not in {"item.started", "item.completed"}:
            return None
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type") or "")
        compact = {
            "type": event_type,
            "item_type": item_type,
            "status": item.get("status"),
        }
        if item_type == "mcp_tool_call":
            tool_name = str(item.get("tool") or item.get("name") or "")
            compact["tool_name"] = tool_name
            compact["server"] = str(item.get("server") or "")
            if item.get("error"):
                compact["error"] = self._sanitize_diagnostic(str(item["error"]))[:500]
            self.event_callback(
                "tool.start" if event_type == "item.started" else "tool.end",
                {
                    "tool": tool_name,
                    "engine": "codex_cli",
                    "status": item.get("status"),
                },
            )
        return compact

    async def _tail_event_journal(self, path: Path) -> None:
        position = 0
        while True:
            await asyncio.sleep(0.2)
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    handle.seek(position)
                    while line := handle.readline():
                        position = handle.tell()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            self._forward_journal_event(event)
            except (OSError, ValueError):
                continue

    def _forward_journal_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type") or "")
        data = dict(event.get("data") or {})
        if event_type == "report.workspace_section":
            self.event_callback(event_type, data)
        elif event_type.startswith("report."):
            self.event_callback(
                "report.progress",
                {
                    "phase": event_type.removeprefix("report.").replace("_", "-"),
                    "message": str(data.get("message") or event_type),
                    "engine": "codex_cli",
                },
            )

    @staticmethod
    def _load_final_payload(path: Path, fallback: str) -> dict[str, Any]:
        raw = ""
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            raw = fallback
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexRuntimeError(ERROR_PROTOCOL, "Codex final response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CodexRuntimeError(ERROR_PROTOCOL, "Codex final response was not an object")
        missing = [key for key in _OUTPUT_SCHEMA["required"] if key not in payload]
        if missing:
            raise CodexRuntimeError(ERROR_PROTOCOL, f"Codex final response omitted: {', '.join(missing)}")
        if payload.get("status") not in {"completed", "blocked"}:
            raise CodexRuntimeError(ERROR_PROTOCOL, "Codex final response had an invalid status")
        return payload

    async def _terminate_process_tree(
        self,
        process: asyncio.subprocess.Process | None = None,
    ) -> None:
        target = process or self._process
        if target is None or target.returncode is not None:
            return
        taskkill = shutil.which("taskkill")
        if os.name == "nt" and not taskkill:
            candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            taskkill = str(candidate) if candidate.is_file() else None
        if os.name == "nt" and taskkill:
            killer = await asyncio.create_subprocess_exec(
                taskkill,
                "/PID",
                str(target.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            target.terminate()
            try:
                await asyncio.wait_for(target.wait(), timeout=5)
            except asyncio.TimeoutError:
                target.kill()
                await target.wait()

    def _sanitize_diagnostic(self, value: str) -> str:
        sanitized = value.replace(self._run_token, "[redacted]") if self._run_token else value
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            secret = os.getenv(name, "")
            if secret:
                sanitized = sanitized.replace(secret, "[redacted]")
        return sanitized

    @staticmethod
    def _failure(
        code: str,
        message: str,
        *,
        run_dir: Path | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "reason": f"{code}: {message}",
            "error_code": code,
            "run_dir": str(run_dir) if run_dir else None,
            "react_trace": list(trace or []),
        }
