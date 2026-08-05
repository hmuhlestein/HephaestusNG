"""Regression coverage for src.mcp.server._build_phase_dict.

Guards against the CLI-tool routing fields (cli_tool, cli_model,
fallback_cli_tool, fallback_cli_model, glm_api_token_env, thinking_level)
being silently dropped when WorkflowDefinition.phases_config is refreshed
from source YAML at server startup -- a bug that made every per-phase
cli_tool: override in config/workflows/*.yaml a no-op.
"""

from src.mcp.server import _build_phase_dict
from src.sdk.models import Phase


def _make_phase(**overrides) -> Phase:
    defaults = dict(
        id=1,
        name="adversarial_review",
        description="desc",
        done_definitions=["done"],
        working_directory=".",
    )
    defaults.update(overrides)
    return Phase(**defaults)


def test_build_phase_dict_carries_cli_routing_fields():
    phase = _make_phase(
        cli_tool="claude",
        cli_model="sonnet",
        fallback_cli_tool="pi",
        fallback_cli_model="openrouter/xiaomi/mimo-v2.5-pro",
        glm_api_token_env="GLM_TOKEN",
        thinking_level="high",
    )

    phase_dict = _build_phase_dict(phase)

    assert phase_dict["cli_tool"] == "claude"
    assert phase_dict["cli_model"] == "sonnet"
    assert phase_dict["fallback_cli_tool"] == "pi"
    assert phase_dict["fallback_cli_model"] == "openrouter/xiaomi/mimo-v2.5-pro"
    assert phase_dict["glm_api_token_env"] == "GLM_TOKEN"
    assert phase_dict["thinking_level"] == "high"


def test_build_phase_dict_omits_unset_cli_routing_fields():
    phase_dict = _build_phase_dict(_make_phase())

    for key in (
        "cli_tool",
        "cli_model",
        "fallback_cli_tool",
        "fallback_cli_model",
        "glm_api_token_env",
        "thinking_level",
    ):
        assert key not in phase_dict
