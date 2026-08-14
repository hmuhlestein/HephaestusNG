"""Regression coverage for src.workflow_engine.yaml_loader.build_phase.

build_phase() previously never read cli_tool (or glm_api_token_env) from a
phase's YAML config into the returned Phase object -- so even an explicit
cli_tool: claude in a phase YAML (e.g. adversarial_review.yaml) had zero
effect, independent of the separate WorkflowDefinition.phases_config
serialization bug fixed in src/mcp/server.py.
"""

from pathlib import Path

from src.workflow_engine.yaml_loader import build_phase


def _base_cfg(**overrides) -> dict:
    cfg = {
        "id": 1,
        "name": "adversarial_review",
    }
    cfg.update(overrides)
    return cfg


def test_build_phase_reads_cli_tool_and_glm_token_from_yaml():
    phase = build_phase(
        _base_cfg(cli_tool="claude", cli_model="sonnet", glm_api_token_env="GLM_TOKEN"),
        default_model="openrouter/xiaomi/mimo-v2.5-pro",
        default_thinking="low",
    )

    assert phase.cli_tool == "claude"
    assert phase.cli_model == "sonnet"
    assert phase.glm_api_token_env == "GLM_TOKEN"


def test_build_phase_defaults_cli_tool_to_none_when_unset():
    phase = build_phase(
        _base_cfg(),
        default_model="openrouter/xiaomi/mimo-v2.5-pro",
        default_thinking="low",
    )

    assert phase.cli_tool is None
    assert phase.glm_api_token_env is None


def test_build_phase_inherits_workflow_level_default_cli_tool():
    phase = build_phase(
        _base_cfg(),
        default_model="openrouter/xiaomi/mimo-v2.5-pro",
        default_thinking="low",
        default_cli_tool="pi",
    )

    assert phase.cli_tool == "pi"


def test_build_phase_own_cli_tool_overrides_workflow_default():
    phase = build_phase(
        _base_cfg(cli_tool="claude"),
        default_model="openrouter/xiaomi/mimo-v2.5-pro",
        default_thinking="low",
        default_cli_tool="pi",
    )

    assert phase.cli_tool == "claude"


def test_autopilot_phases_use_codex_terra_with_pi_fallback():
    from src.workflow_engine.yaml_loader import load_full_workflow_definition

    workflow = load_full_workflow_definition(
        Path(__file__).resolve().parents[1] / "config" / "workflows" / "autopilot"
    )

    assert len(workflow.phases) == 14
    for phase in workflow.phases:
        assert phase.cli_tool == "codex"
        assert phase.cli_model == "gpt-5.6-terra"
        assert phase.fallback_cli_tool == "pi"
        assert phase.fallback_cli_model == "openrouter/xiaomi/mimo-v2.5-pro"
