"""Regression tests for local settings API endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    env_example = tmp_path / ".env.example"
    env_path = tmp_path / ".env"
    env_example.write_text(
        "\n".join(
            [
                "LANGCHAIN_PROVIDER=openrouter",
                "LANGCHAIN_MODEL_NAME=deepseek/deepseek-v4-pro",
                "OPENROUTER_BASE_URL=https://openrouter.ai/api/v1",
                "OPENROUTER_API_KEY=sk-or-v1-your-key-here",
                "LANGCHAIN_TEMPERATURE=0.2",
                "TIMEOUT_SECONDS=90",
                "MAX_RETRIES=3",
                "LANGCHAIN_REASONING_EFFORT=max",
                "TUSHARE_TOKEN=your-tushare-token",
                "VIBE_TRADING_DEEP_REPORT_ENABLED=0",
                "VIBE_TRADING_DEEP_REPORT_PROFILES=equity_deep_research",
                "VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED=0",
                "VIBE_TRADING_DEEP_RESEARCH_ENGINE=provider",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api_server, "ENV_PATH", env_path)
    monkeypatch.setattr(api_server, "ENV_EXAMPLE_PATH", env_example)
    monkeypatch.setattr(api_server, "_baostock_supported", lambda: False)
    monkeypatch.setattr(api_server, "_baostock_installed", lambda: False)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "0")
    monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_PROFILES", "equity_deep_research")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED", "0")
    monkeypatch.setenv("VIBE_TRADING_DEEP_RESEARCH_ENGINE", "provider")
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_get_llm_settings_is_side_effect_free_and_hides_placeholders(
    client: TestClient, tmp_path: Path,
) -> None:
    response = client.get("/settings/llm")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openrouter"
    assert body["model_name"] == "deepseek/deepseek-v4-pro"
    assert body["api_key_configured"] is False
    assert body["api_key_hint"] is None
    assert not Path(body["env_path"]).is_absolute()
    assert body["env_path"].endswith(".env")
    assert body["reasoning_effort"] == "max"
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("placeholder", ["sk-xxx", "xxx", "gsk_xxx"])
def test_llm_settings_treat_documented_key_placeholders_as_unconfigured(
    client: TestClient, tmp_path: Path, placeholder: str,
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "LANGCHAIN_PROVIDER=deepseek",
                "LANGCHAIN_MODEL_NAME=deepseek-v4-pro",
                f"DEEPSEEK_API_KEY={placeholder}",
                "DEEPSEEK_BASE_URL=https://api.deepseek.com/v1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    response = client.get("/settings/llm")

    assert response.status_code == 200
    body = response.json()
    assert body["api_key_configured"] is False
    assert body["api_key_hint"] is None
    assert placeholder not in response.text


def test_update_llm_settings_persists_project_env(
    client: TestClient, tmp_path: Path,
) -> None:
    response = client.put(
        "/settings/llm",
        json={
            "provider": "openrouter",
            "model_name": "deepseek/deepseek-v4-pro",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "or-secret-value",
            "temperature": 0.1,
            "timeout_seconds": 45,
            "max_retries": 1,
            "reasoning_effort": "max",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openrouter"
    assert body["api_key_configured"] is True
    assert body["api_key_hint"] is None
    assert "or-secret-value" not in response.text
    assert "or-s...alue" not in response.text

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "LANGCHAIN_PROVIDER=openrouter" in env_text
    assert "OPENROUTER_API_KEY=or-secret-value" in env_text
    assert "LANGCHAIN_REASONING_EFFORT=max" in env_text
    assert "sk-or-v1-your-key-here" not in env_text


def test_get_data_source_settings_treats_placeholder_as_unconfigured(
    client: TestClient, tmp_path: Path,
) -> None:
    response = client.get("/settings/data-sources")

    assert response.status_code == 200
    body = response.json()
    assert body["tushare_token_configured"] is False
    assert body["tushare_token_hint"] is None
    assert body["baostock_supported"] is False
    assert body["baostock_installed"] is False
    assert not Path(body["env_path"]).is_absolute()
    assert body["env_path"].endswith(".env")
    assert not (tmp_path / ".env").exists()


def test_codex_provider_exposes_gpt_56_model_choices() -> None:
    provider = api_server.LLM_PROVIDER_BY_NAME["openai-codex"]

    assert provider.default_model == "openai-codex/gpt-5.6-terra"
    assert provider.model_discovery == "codex_oauth"
    assert [model.id for model in provider.models] == [
        "openai-codex/gpt-5.3-codex-spark",
        "openai-codex/gpt-5.6-sol",
        "openai-codex/gpt-5.6-terra",
        "openai-codex/gpt-5.6-luna",
    ]


def test_refresh_codex_models_returns_account_visible_choices(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.providers.openai_codex as codex_provider

    monkeypatch.setattr(api_server, "_codex_models_client_version", lambda: "0.145.0")
    monkeypatch.setattr(
        codex_provider,
        "list_openai_codex_models",
        lambda **kwargs: [
            {
                "id": "openai-codex/gpt-5.6-terra",
                "label": "GPT-5.6-Terra",
                "description": "Balanced",
                "default_reasoning_effort": "medium",
                "reasoning_efforts": ["low", "medium", "high"],
            }
        ],
    )

    response = client.get("/settings/llm/models?provider=openai-codex")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "remote"
    assert body["warning"] is None
    assert body["models"][0]["id"] == "openai-codex/gpt-5.6-terra"


def test_refresh_codex_models_falls_back_without_hiding_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.providers.openai_codex as codex_provider

    def _fail(**kwargs: object) -> list[dict]:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(codex_provider, "list_openai_codex_models", _fail)

    response = client.get("/settings/llm/models?provider=openai-codex")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "configured"
    assert "provider unavailable" in body["warning"]
    assert body["models"][2]["id"] == "openai-codex/gpt-5.6-terra"


def test_research_settings_switch_persists_and_applies_without_restart(
    client: TestClient, tmp_path: Path,
) -> None:
    initial = client.get("/settings/research")

    assert initial.status_code == 200
    assert initial.json()["deep_report_enabled"] is False
    assert initial.json()["equity_deep_research_enabled"] is False
    assert initial.json()["monitor_auto_deep_report_enabled"] is False
    assert initial.json()["effective_monitor_auto_deep_report_enabled"] is False
    assert initial.json()["deep_research_engine"] == "provider"
    assert initial.json()["codex_cli_model"] == "gpt-5.6-terra"
    assert initial.json()["codex_cli_reasoning_effort"] == "medium"
    assert not (tmp_path / ".env").exists()

    enabled = client.put(
        "/settings/research",
        json={"deep_report_enabled": True},
    )

    assert enabled.status_code == 200
    body = enabled.json()
    assert body["deep_report_enabled"] is True
    assert body["equity_deep_research_enabled"] is True
    assert body["etf_deep_research_enabled"] is True
    assert body["enabled_profiles"] == ["equity_deep_research", "etf_deep_research"]
    assert body["available_profiles"] == ["equity_deep_research", "etf_deep_research"]
    assert body["monitor_auto_deep_report_enabled"] is False
    assert body["effective_monitor_auto_deep_report_enabled"] is False
    assert api_server.os.environ["VIBE_TRADING_DEEP_REPORT_ENABLED"] == "1"

    auto_enabled = client.put(
        "/settings/research",
        json={"monitor_auto_deep_report_enabled": True},
    )
    assert auto_enabled.status_code == 200
    auto_body = auto_enabled.json()
    assert auto_body["deep_report_enabled"] is True
    assert auto_body["monitor_auto_deep_report_enabled"] is True
    assert auto_body["effective_monitor_auto_deep_report_enabled"] is True
    assert api_server.os.environ["VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED"] == "1"

    disabled = client.put(
        "/settings/research",
        json={"deep_report_enabled": False},
    )
    assert disabled.status_code == 200
    disabled_body = disabled.json()
    assert disabled_body["deep_report_enabled"] is False
    assert disabled_body["monitor_auto_deep_report_enabled"] is False
    assert disabled_body["effective_monitor_auto_deep_report_enabled"] is False

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "VIBE_TRADING_DEEP_REPORT_ENABLED=0" in env_text
    assert "VIBE_TRADING_DEEP_REPORT_PROFILES=equity_deep_research,etf_deep_research" in env_text
    assert "VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED=0" in env_text


def test_research_settings_default_to_disabled_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("VIBE_TRADING_DEEP_REPORT_ENABLED", raising=False)
    monkeypatch.delenv("VIBE_TRADING_DEEP_REPORT_PROFILES", raising=False)
    monkeypatch.delenv("VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED", raising=False)

    body = api_server._build_research_settings_response({})

    assert body.deep_report_enabled is False
    assert body.equity_deep_research_enabled is False
    assert body.etf_deep_research_enabled is False
    assert body.monitor_auto_deep_report_enabled is False
    assert body.effective_monitor_auto_deep_report_enabled is False
    assert body.codex_cli_model == "gpt-5.6-terra"
    assert body.codex_cli_reasoning_effort == "medium"


def test_component_research_settings_are_fail_closed_and_hard_bounded(
    client: TestClient, tmp_path: Path,
) -> None:
    initial = client.get("/settings/research")
    assert initial.status_code == 200
    assert initial.json()["etf_component_research_generation_enabled"] is False
    assert initial.json()["etf_component_research_live_run_enabled"] is False
    assert initial.json()["component_research_generation_policy"]["max_auto_repairs"] == 0

    enabled = client.put(
        "/settings/research",
        json={
            "etf_component_research_generation_enabled": True,
            "etf_component_research_live_run_enabled": True,
            "component_research_max_components_per_etf_run": 3,
            "component_research_max_components_per_day": 5,
            "component_research_max_model_calls_per_day": 5,
            "component_research_max_input_tokens_per_component": 6000,
            "component_research_max_output_tokens_per_component": 1000,
            "component_research_max_input_tokens_per_day": 30000,
            "component_research_max_output_tokens_per_day": 3000,
        },
    )
    assert enabled.status_code == 200
    body = enabled.json()
    assert body["etf_component_research_generation_enabled"] is True
    assert body["etf_component_research_live_run_enabled"] is True
    assert (
        body["component_research_generation_policy"]
        ["max_output_tokens_per_component"]
        == 1000
    )
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ETF_COMPONENT_RESEARCH_GENERATION_ENABLED=1" in env_text
    assert "ETF_COMPONENT_RESEARCH_LIVE_RUN_ENABLED=1" in env_text

    rejected = client.put(
        "/settings/research",
        json={
            "etf_component_research_generation_enabled": False,
            "etf_component_research_live_run_enabled": True,
        },
    )
    assert rejected.status_code == 400


def test_settings_response_never_exposes_configured_secret_hints(
    client: TestClient, tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "LANGCHAIN_PROVIDER=openrouter",
                "OPENROUTER_API_KEY=or-secret-private-value",
                "TUSHARE_TOKEN=ts-secret-private-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    llm_response = client.get("/settings/llm")
    data_response = client.get("/settings/data-sources")

    assert llm_response.status_code == 200
    assert data_response.status_code == 200
    llm_body = llm_response.json()
    data_body = data_response.json()
    assert llm_body["api_key_configured"] is True
    assert llm_body["api_key_hint"] is None
    assert data_body["tushare_token_configured"] is True
    assert data_body["tushare_token_hint"] is None
    assert "or-secret-private-value" not in llm_response.text
    assert "or-s...alue" not in llm_response.text
    assert "ts-secret-private-token" not in data_response.text
    assert "ts-s...oken" not in data_response.text


def test_settings_reads_reject_remote_dev_mode_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    env_path.write_text(
        "\n".join(
            [
                "LANGCHAIN_PROVIDER=openrouter",
                "OPENROUTER_API_KEY=or-secret-value",
                "TUSHARE_TOKEN=ts-secret-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_example.write_text("LANGCHAIN_PROVIDER=openai\n", encoding="utf-8")
    monkeypatch.setattr(api_server, "ENV_PATH", env_path)
    monkeypatch.setattr(api_server, "ENV_EXAMPLE_PATH", env_example)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    remote_client = TestClient(api_server.app, client=("203.0.113.10", 50000))

    llm_response = remote_client.get("/settings/llm")
    data_source_response = remote_client.get("/settings/data-sources")

    assert llm_response.status_code == 403
    assert data_source_response.status_code == 403
    assert "or-s...alue" not in llm_response.text
    assert "ts-s...oken" not in data_source_response.text


def test_settings_reads_allow_loopback_without_bearer_even_when_api_auth_key_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    env_path.write_text(
        "\n".join(
            [
                "LANGCHAIN_PROVIDER=openrouter",
                "OPENROUTER_API_KEY=or-secret-value",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_example.write_text("LANGCHAIN_PROVIDER=openai\n", encoding="utf-8")
    monkeypatch.setattr(api_server, "ENV_PATH", env_path)
    monkeypatch.setattr(api_server, "ENV_EXAMPLE_PATH", env_example)
    monkeypatch.setenv("API_AUTH_KEY", "settings-secret")
    local_client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    unauthenticated_response = local_client.get("/settings/llm")
    authenticated_response = local_client.get(
        "/settings/llm",
        headers={"Authorization": "Bearer settings-secret"},
    )

    assert unauthenticated_response.status_code == 200
    assert authenticated_response.status_code == 200
    assert authenticated_response.json()["api_key_configured"] is True
    assert authenticated_response.json()["api_key_hint"] is None
    assert "or-secret-value" not in authenticated_response.text
    assert "or-s...alue" not in authenticated_response.text


def test_update_data_source_settings_persists_tushare_token(
    client: TestClient, tmp_path: Path,
) -> None:
    response = client.put(
        "/settings/data-sources",
        json={"tushare_token": "ts-secret-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tushare_token_configured"] is True
    assert body["tushare_token_hint"] is None
    assert "ts-secret-token" not in response.text
    assert "ts-s...oken" not in response.text

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "TUSHARE_TOKEN=ts-secret-token" in env_text


def test_settings_writes_reject_remote_dev_mode_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_example = tmp_path / ".env.example"
    env_path = tmp_path / ".env"
    env_example.write_text("LANGCHAIN_PROVIDER=openai\n", encoding="utf-8")
    monkeypatch.setattr(api_server, "ENV_PATH", env_path)
    monkeypatch.setattr(api_server, "ENV_EXAMPLE_PATH", env_example)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    remote_client = TestClient(api_server.app, client=("203.0.113.10", 50000))

    response = remote_client.put(
        "/settings/data-sources",
        json={"tushare_token": "ts-secret-token"},
    )

    assert response.status_code == 403
    assert not env_path.exists()
