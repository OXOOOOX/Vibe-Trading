"""AgentLoop guards for preserving explicit swarm routing intent."""

from __future__ import annotations

from types import SimpleNamespace

from src.agent.loop import _inject_swarm_preset_hint


def _call(arguments: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(name="run_swarm", arguments=arguments)


def test_agent_loop_injects_user_named_preset_when_model_omits_it() -> None:
    call = _call({"prompt": "Evaluate A-share opportunities."})

    _inject_swarm_preset_hint([call], "investment_committee")

    assert call.arguments["preset_name"] == "investment_committee"


def test_agent_loop_preserves_model_explicit_preset() -> None:
    call = _call({
        "prompt": "Run a quantitative strategy review.",
        "preset_name": "quant_strategy_desk",
    })

    _inject_swarm_preset_hint([call], "investment_committee")

    assert call.arguments["preset_name"] == "quant_strategy_desk"
