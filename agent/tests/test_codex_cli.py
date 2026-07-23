"""Codex CLI settings, isolation command, and JSONL runner regressions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import api_server
import src.codex_cli.runner as runner_module
import src.codex_cli.status as status_module
from src.codex_cli.runner import CodexResearchRunner, build_codex_exec_command
from src.codex_cli.status import CodexCliStatus
from src.reports import DeepReportService
from src.session.events import EventBus
from src.session.models import Attempt
from src.session.service import SessionService
from src.session.store import SessionStore


def _status(*, ready: bool = True, installed: bool = True) -> CodexCliStatus:
    return CodexCliStatus(
        installed=installed,
        binary=sys.executable if installed else None,
        version="0.144.5" if installed else None,
        minimum_version="0.144.5",
        version_supported=installed,
        auth_state="authenticated" if ready else ("unauthenticated" if installed else "unavailable"),
        ready=ready,
        message="ready" if ready else "login required",
    )


@pytest.fixture
def settings_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    env_path = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    env_example.write_text(
        "VIBE_TRADING_DEEP_REPORT_ENABLED=1\n"
        "VIBE_TRADING_DEEP_REPORT_PROFILES=equity_deep_research,etf_deep_research\n"
        "VIBE_TRADING_DEEP_RESEARCH_ENGINE=provider\n"
        "VIBE_TRADING_CODEX_DEEP_RESEARCH_ENABLED=0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api_server, "ENV_PATH", env_path)
    monkeypatch.setattr(api_server, "ENV_EXAMPLE_PATH", env_example)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_PROFILES", "equity_deep_research,etf_deep_research")
    monkeypatch.setenv("VIBE_TRADING_DEEP_RESEARCH_ENGINE", "provider")
    monkeypatch.setenv("VIBE_TRADING_CODEX_DEEP_RESEARCH_ENABLED", "0")
    return TestClient(api_server.app, client=("127.0.0.1", 51000))


def test_status_detects_supported_authenticated_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_module, "resolve_codex_binary", lambda: "codex")

    def fake_run(binary: str, *args: str, timeout: float = 8.0):
        del binary, timeout
        if args == ("--version",):
            return status_module.subprocess.CompletedProcess(args, 0, "codex-cli 0.144.5\n", "")
        return status_module.subprocess.CompletedProcess(args, 0, "Logged in using ChatGPT\n", "")

    monkeypatch.setattr(status_module, "_run_check", fake_run)

    result = status_module.inspect_codex_cli()

    assert result.ready is True
    assert result.version == "0.144.5"
    assert result.auth_state == "authenticated"


def test_latest_version_reads_and_caches_npm_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    calls = 0

    def fake_urlopen(request, *, timeout: float):
        nonlocal calls
        del request, timeout
        calls += 1
        return FakeResponse(b'{"version":"0.145.0"}')

    monkeypatch.setattr(status_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(status_module, "_latest_version_cached_at", 0.0)
    monkeypatch.setattr(status_module, "_latest_version_cache", None)

    assert status_module.get_latest_codex_cli_version() == "0.145.0"
    assert status_module.get_latest_codex_cli_version() == "0.145.0"
    assert calls == 1


def test_settings_status_identifies_the_host_command_shell(
    settings_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_server, "get_codex_cli_status", lambda **_: _status())
    monkeypatch.setattr(api_server, "get_latest_codex_cli_version", lambda: "0.145.0")

    response = settings_client.get("/settings/codex-cli/status")

    assert response.status_code == 200
    assert response.json()["command_shell"] == ("powershell" if os.name == "nt" else "terminal")
    assert response.json()["latest_version"] == "0.145.0"


def test_isolated_codex_environment_does_not_reuse_outer_codex_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    monkeypatch.setenv("VIBE_TRADING_CODEX_HOME", str(isolated_home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "global-codex-home"))
    monkeypatch.setenv("CODEX_THREAD_ID", "outer-thread")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "outer-token")
    monkeypatch.setenv("OPENAI_API_KEY", "outer-api-key")

    env = status_module.isolated_codex_environment()

    assert env["CODEX_HOME"] == str(isolated_home.resolve())
    assert "CODEX_THREAD_ID" not in env
    assert "CODEX_ACCESS_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert isolated_home.is_dir()
    assert not (isolated_home / "auth.json").exists()


def test_isolation_probe_rejects_non_whitelisted_user_instructions(tmp_path: Path) -> None:
    safe_payload = [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": f"<environment_context>{tmp_path}</environment_context>"}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": runner_module._PROBE_MARKER}],
        },
    ]
    runner_module._validate_isolation_payload(
        safe_payload,
        run_dir=tmp_path,
        skill_files=[],
    )
    leaked_payload = [
        *safe_payload[:-1],
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "# AGENTS.md instructions\nDo something global."}],
        },
        safe_payload[-1],
    ]

    with pytest.raises(runner_module.CodexRuntimeError, match="Non-whitelisted user instructions"):
        runner_module._validate_isolation_payload(
            leaked_payload,
            run_dir=tmp_path,
            skill_files=[],
        )


def test_exec_command_ignores_global_config_and_does_not_override_codex_home(tmp_path: Path) -> None:
    command = build_codex_exec_command(
        binary="codex",
        run_dir=tmp_path,
        output_schema_path=tmp_path / "schema.json",
        final_output_path=tmp_path / "final.json",
        skill_files=[tmp_path / "user-skill" / "SKILL.md"],
        model="gpt-5.6-terra",
        reasoning_effort="high",
    )
    joined = " ".join(command)

    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--ephemeral" in command
    assert command[command.index("--model") + 1] == "gpt-5.6-terra"
    assert 'model_reasoning_effort="high"' in command
    assert "read-only" in command
    assert 'web_search="disabled"' in command
    assert "project_doc_max_bytes=0" in command
    assert "mcp_servers.vibe_research.required=true" in command
    assert 'mcp_servers.vibe_research.default_tools_approval_mode="approve"' in command
    assert any("mcp_servers.vibe_research.env_vars=" in value for value in command)
    assert "skills.config=[" in joined
    assert "CODEX_HOME" not in joined
    assert "auth.json" not in joined


def test_dedicated_codex_cli_preferences_do_not_depend_on_provider_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGCHAIN_PROVIDER", "deepseek")
    monkeypatch.setenv("LANGCHAIN_MODEL_NAME", "deepseek-v4-pro")
    monkeypatch.setenv("VIBE_TRADING_CODEX_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("VIBE_TRADING_CODEX_REASONING_EFFORT", "ultra")

    assert runner_module.resolve_codex_cli_preferences() == ("gpt-5.6-sol", "ultra")


def test_scoped_mcp_exposes_only_signed_report_tools(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    report = DeepReportService(reports_dir).begin(
        session_id="session_1",
        attempt_id="attempt_1",
        request_content="Research 000001.SZ",
        profile="equity_deep_research",
    )
    run_dir = tmp_path / "runtime" / "run_1"
    run_dir.mkdir(parents=True)
    event_log = run_dir / "events.jsonl"
    token = "test-token-with-sufficient-entropy"
    payload = {
        "version": 1,
        "run_id": "run_1",
        "created_at": "2026-07-18T00:00:00+00:00",
        "session_id": "session_1",
        "attempt_id": "attempt_1",
        "report_id": report.report_id,
        "report_profile": "equity_deep_research",
        "revision_mode": "initial",
        "reports_dir": str(reports_dir.resolve()),
        "run_dir": str(run_dir.resolve()),
        "event_log": str(event_log.resolve()),
        "allowed_tools": ["report_workspace"],
        "financial_rigor_commands": [],
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(token.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    manifest = run_dir / "context.json"
    manifest.write_text(
        json.dumps({**payload, "signature": signature}, ensure_ascii=False),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["VIBE_CODEX_RUN_MANIFEST"] = str(manifest)
    env["VIBE_CODEX_RUN_TOKEN"] = token
    agent_dir = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = os.pathsep.join([str(agent_dir), env.get("PYTHONPATH", "")])

    async def exercise() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src.codex_cli.mcp_server"],
            env=env,
            cwd=agent_dir,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == ["report_workspace"]
                schema = tools.tools[0].inputSchema
                assert "report_id" not in dict(schema.get("properties") or {})
                result = await session.call_tool("report_workspace", {"command": "inspect"})
                assert result.isError is False
                assert result.structuredContent["report_id"] == report.report_id

    asyncio.run(exercise())


def test_research_settings_reject_enabling_unready_codex(
    settings_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_server, "get_codex_cli_status", lambda **kwargs: _status(ready=False))

    response = settings_client.put("/settings/research", json={"codex_cli_enabled": True})

    assert response.status_code == 409
    assert "not ready" in response.json()["detail"]


def test_research_settings_persist_codex_switch(
    settings_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(api_server, "get_codex_cli_status", lambda **kwargs: _status())

    response = settings_client.put("/settings/research", json={"codex_cli_enabled": True})

    assert response.status_code == 200
    assert response.json()["deep_research_engine"] == "codex_cli"
    assert response.json()["codex_cli_enabled"] is True
    assert response.json()["effective_codex_cli_enabled"] is True
    assert "VIBE_TRADING_CODEX_DEEP_RESEARCH_ENABLED=1" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "VIBE_TRADING_DEEP_RESEARCH_ENGINE=codex_cli" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_isolated_cli_model_and_reasoning_preferences_persist_independently(
    settings_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(api_server, "get_codex_cli_status", lambda **kwargs: _status())

    response = settings_client.put(
        "/settings/research",
        json={
            "codex_cli_model": "gpt-5.6-sol",
            "codex_cli_reasoning_effort": "ultra",
        },
    )

    assert response.status_code == 200
    assert response.json()["codex_cli_model"] == "gpt-5.6-sol"
    assert response.json()["codex_cli_reasoning_effort"] == "ultra"
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "VIBE_TRADING_CODEX_MODEL=gpt-5.6-sol" in env_text
    assert "VIBE_TRADING_CODEX_REASONING_EFFORT=ultra" in env_text


def test_isolated_cli_reasoning_effort_rejects_unknown_values(
    settings_client: TestClient,
) -> None:
    response = settings_client.put(
        "/settings/research",
        json={"codex_cli_reasoning_effort": "impossible"},
    )

    assert response.status_code == 400


def test_research_settings_persist_provider_engine_without_cli_readiness(
    settings_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(api_server, "get_codex_cli_status", lambda **kwargs: _status(ready=False))

    response = settings_client.put(
        "/settings/research",
        json={"deep_research_engine": "provider"},
    )

    assert response.status_code == 200
    assert response.json()["deep_research_engine"] == "provider"
    assert response.json()["codex_cli_enabled"] is False
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "VIBE_TRADING_DEEP_RESEARCH_ENGINE=provider" in env_text
    assert "VIBE_TRADING_CODEX_DEEP_RESEARCH_ENABLED=0" in env_text


def test_research_settings_reject_conflicting_engine_and_legacy_switch(
    settings_client: TestClient,
) -> None:
    response = settings_client.put(
        "/settings/research",
        json={"deep_research_engine": "provider", "codex_cli_enabled": True},
    )

    assert response.status_code == 400
    assert "conflicts" in response.json()["detail"]


def test_remote_client_cannot_launch_login_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_example = tmp_path / ".env.example"
    env_example.write_text("VIBE_TRADING_DEEP_REPORT_ENABLED=1\n", encoding="utf-8")
    monkeypatch.setattr(api_server, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(api_server, "ENV_EXAMPLE_PATH", env_example)
    monkeypatch.setenv("API_AUTH_KEY", "secret")
    monkeypatch.setattr(api_server, "get_codex_cli_status", lambda **kwargs: _status())
    launched: list[str] = []
    monkeypatch.setattr(api_server, "launch_codex_login_terminal", lambda binary: launched.append(binary) or True)
    client = TestClient(api_server.app, client=("203.0.113.10", 51000))

    response = client.post(
        "/settings/codex-cli/login",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 403
    assert launched == []
    assert "device-auth" in response.json()["detail"]


def test_local_client_launches_only_fixed_login_command(
    settings_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_server, "get_codex_cli_status", lambda **kwargs: _status())
    launched: list[str] = []
    monkeypatch.setattr(api_server, "launch_codex_login_terminal", lambda binary: launched.append(binary) or True)

    response = settings_client.post("/settings/codex-cli/login")

    assert response.status_code == 200
    assert "CODEX_HOME" in response.json()["command"]
    assert response.json()["command"].endswith("codex login")
    assert launched == [sys.executable]


@pytest.mark.parametrize(
    ("profile", "revision_mode", "expected_tools"),
    [
        ("equity_deep_research", "initial", {"analyze_financial_snapshot", "report_workspace"}),
        ("etf_deep_research", "initial", {"get_fund_flow", "report_workspace"}),
        ("equity_deep_research", "repair", {"report_workspace"}),
    ],
)
def test_session_routes_all_deep_report_profiles_to_codex_without_provider_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    revision_mode: str,
    expected_tools: set[str],
) -> None:
    service = SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    report = service.deep_reports.begin(
        session_id="session_1",
        attempt_id="attempt_1",
        request_content="research",
        profile=profile,
        revision_mode="initial",
    )
    attempt = Attempt(
        attempt_id="attempt_1",
        session_id="session_1",
        prompt="research",
        metadata={
            "response_mode": "deep_report",
            "report_profile": profile,
            "report_id": report.report_id,
            "revision_mode": revision_mode,
        },
    )
    monkeypatch.setenv("VIBE_TRADING_DEEP_RESEARCH_ENGINE", "codex_cli")
    monkeypatch.setenv("VIBE_TRADING_CODEX_DEEP_RESEARCH_ENABLED", "1")
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self):
            return {
                "status": "failed",
                "reason": "codex_process_failed: deliberate test failure",
                "error_code": "codex_process_failed",
                "react_trace": [],
            }

        def cancel(self):
            return None

    class ProviderMustNotStart:
        def __init__(self, *args, **kwargs):
            raise AssertionError("legacy provider was instantiated after Codex routing")

    monkeypatch.setattr("src.codex_cli.CodexResearchRunner", FakeRunner)
    monkeypatch.setattr("src.providers.chat.ChatLLM", ProviderMustNotStart)

    result = asyncio.run(service._run_with_agent(attempt, messages=[]))

    assert result["status"] == "failed"
    allowed = set(captured["allowed_tools"])
    assert expected_tools.issubset(allowed)
    if revision_mode == "repair":
        assert allowed == {"report_workspace"}


class _UsageRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_llm(self, usage, **kwargs):
        self.calls.append({"usage": usage, **kwargs})


def _make_runner(tmp_path: Path, recorder: _UsageRecorder) -> CodexResearchRunner:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    return CodexResearchRunner(
        session_id="session_1",
        attempt_id="attempt_1",
        report_id="report_1",
        report_profile="equity_deep_research",
        revision_mode="initial",
        prompt="Research 000001.SZ",
        history=[],
        reports_dir=reports_dir,
        allowed_tools=["report_workspace"],
        financial_rigor_commands={"calc"},
        event_callback=lambda *_: None,
        usage_recorder=recorder,  # type: ignore[arg-type]
        semaphore=asyncio.Semaphore(1),
    )


def test_runner_parses_jsonl_and_structured_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "sys.stdin.read()\n"
        "payload={'status':'completed','summary':'done','missing_evidence':[], 'section_status':{'thesis':'passed'}}\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(payload), encoding='utf-8')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thread_1'}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(payload)}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':2,'output_tokens':5,'reasoning_output_tokens':1}}), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_TRADING_CODEX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VIBE_TRADING_CODEX_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("VIBE_TRADING_CODEX_REASONING_EFFORT", "medium")
    monkeypatch.setattr(runner_module, "get_codex_cli_status", lambda **kwargs: _status())

    async def skip_probe(*args, **kwargs):
        return None

    monkeypatch.setattr(CodexResearchRunner, "_verify_isolation", skip_probe)
    command_kwargs: dict[str, object] = {}

    def fake_command(**kwargs):
        command_kwargs.update(kwargs)
        return [sys.executable, str(script), str(kwargs["final_output_path"])]

    monkeypatch.setattr(runner_module, "build_codex_exec_command", fake_command)
    recorder = _UsageRecorder()

    result = asyncio.run(_make_runner(tmp_path, recorder).run())

    assert result["status"] == "success"
    assert result["content"] == "done"
    assert result["codex"]["section_status"] == {"thesis": "passed"}
    assert result["codex"]["model"] == "gpt-5.6-terra"
    assert result["codex"]["model_source"] == "codex_cli_settings"
    assert result["codex"]["reasoning_effort"] == "medium"
    assert command_kwargs["model"] == "gpt-5.6-terra"
    assert command_kwargs["reasoning_effort"] == "medium"
    assert recorder.calls[0]["provider"] == "openai-codex-cli"
    assert recorder.calls[0]["model"] == "gpt-5.6-terra"


def test_runner_fails_closed_on_malformed_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "bad_codex.py"
    script.write_text("import sys\nsys.stdin.read()\nprint('not-json', flush=True)\n", encoding="utf-8")
    monkeypatch.setenv("VIBE_TRADING_CODEX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(runner_module, "get_codex_cli_status", lambda **kwargs: _status())

    async def skip_probe(*args, **kwargs):
        return None

    monkeypatch.setattr(CodexResearchRunner, "_verify_isolation", skip_probe)
    monkeypatch.setattr(
        runner_module,
        "build_codex_exec_command",
        lambda **kwargs: [sys.executable, str(script)],
    )

    result = asyncio.run(_make_runner(tmp_path, _UsageRecorder()).run())

    assert result["status"] == "failed"
    assert result["error_code"] == "codex_protocol_error"
    assert "codex_protocol_error" in result["reason"]


def test_runner_preserves_workspace_on_subprocess_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "failed_codex.py"
    script.write_text(
        "import sys\nsys.stdin.read()\nprint('fixed failure', file=sys.stderr, flush=True)\nraise SystemExit(7)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_TRADING_CODEX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(runner_module, "get_codex_cli_status", lambda **kwargs: _status())

    async def skip_probe(*args, **kwargs):
        return None

    monkeypatch.setattr(CodexResearchRunner, "_verify_isolation", skip_probe)
    monkeypatch.setattr(
        runner_module,
        "build_codex_exec_command",
        lambda **kwargs: [sys.executable, str(script)],
    )

    result = asyncio.run(_make_runner(tmp_path, _UsageRecorder()).run())

    assert result["error_code"] == "codex_process_failed"
    assert "fixed failure" in result["reason"]
    assert Path(result["run_dir"]).is_dir()


def test_runner_times_out_and_terminates_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "slow_codex.py"
    script.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setenv("VIBE_TRADING_CODEX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VIBE_TRADING_CODEX_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(runner_module, "get_codex_cli_status", lambda **kwargs: _status())

    async def skip_probe(*args, **kwargs):
        return None

    monkeypatch.setattr(CodexResearchRunner, "_verify_isolation", skip_probe)
    monkeypatch.setattr(
        runner_module,
        "build_codex_exec_command",
        lambda **kwargs: [sys.executable, str(script)],
    )

    result = asyncio.run(_make_runner(tmp_path, _UsageRecorder()).run())

    assert result["error_code"] == "codex_timeout"
    assert Path(result["run_dir"]).is_dir()


def test_runner_cancellation_terminates_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "cancelled_codex.py"
    script.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setenv("VIBE_TRADING_CODEX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VIBE_TRADING_CODEX_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(runner_module, "get_codex_cli_status", lambda **kwargs: _status())

    async def skip_probe(*args, **kwargs):
        return None

    monkeypatch.setattr(CodexResearchRunner, "_verify_isolation", skip_probe)
    monkeypatch.setattr(
        runner_module,
        "build_codex_exec_command",
        lambda **kwargs: [sys.executable, str(script)],
    )
    runner = _make_runner(tmp_path, _UsageRecorder())

    async def exercise() -> dict[str, object]:
        task = asyncio.create_task(runner.run())
        for _ in range(100):
            if runner._process is not None:
                break
            await asyncio.sleep(0.01)
        assert runner._process is not None
        runner.cancel()
        return await task

    result = asyncio.run(exercise())

    assert result["status"] == "cancelled"
