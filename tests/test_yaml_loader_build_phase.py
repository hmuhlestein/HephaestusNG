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


def test_autopilot_phases_use_claude_sonnet_with_pi_fallback():
    from src.workflow_engine.yaml_loader import load_full_workflow_definition

    workflow = load_full_workflow_definition(
        Path(__file__).resolve().parents[1] / "config" / "workflows" / "autopilot"
    )

    assert len(workflow.phases) == 14
    for phase in workflow.phases:
        assert phase.cli_tool == "claude"
        assert phase.cli_model == "sonnet"
        assert phase.fallback_cli_tool == "pi"
        assert phase.fallback_cli_model == "openrouter/xiaomi/mimo-v2.5-pro"


class TestCompletionMarkers:
    """The "WHEN YOU ARE DONE - MARK YOUR TASK AS COMPLETE" completion
    instructions used to be independently copy-pasted (with 3-4 drifting
    variants) across 15 phase YAML files' additional_notes. build_phase now
    substitutes <<COMPLETION_HEADER>>/<<COMPLETION_FOOTER>>/
    <<COMPLETION_STOP_LINE>> markers with shared text, so every phase gets
    the same, fullest wording (CRITICAL warning + wait-for-confirmation +
    stop-immediately) from one source instead of drifting copies."""

    def test_substitutes_header_and_footer_markers(self):
        from src.workflow_engine.yaml_loader import _COMPLETION_FOOTER, _COMPLETION_HEADER

        phase = build_phase(
            _base_cfg(
                additional_notes=(
                    "Some phase-specific text.\n\n"
                    "<<COMPLETION_HEADER>>\n\n"
                    'complete_my_task({"status": "done"})\n\n'
                    "<<COMPLETION_FOOTER>>\n"
                )
            ),
            default_model="openrouter/xiaomi/mimo-v2.5-pro",
            default_thinking="low",
        )

        assert _COMPLETION_HEADER in phase.additional_notes
        assert _COMPLETION_FOOTER in phase.additional_notes
        assert "<<" not in phase.additional_notes
        assert "Some phase-specific text." in phase.additional_notes
        assert 'complete_my_task({"status": "done"})' in phase.additional_notes

    def test_substitutes_standalone_stop_line_marker(self):
        """feature_architect's own completion instructions don't match the
        header/footer shape (no CRITICAL warning, no "wait for
        confirmation" line) -- it only opts into the shared stop-immediately
        sentence."""
        from src.workflow_engine.yaml_loader import _COMPLETION_STOP_LINE

        phase = build_phase(
            _base_cfg(
                additional_notes="Mark your task as done.\n\n<<COMPLETION_STOP_LINE>>\n"
            ),
            default_model="openrouter/xiaomi/mimo-v2.5-pro",
            default_thinking="low",
        )

        assert _COMPLETION_STOP_LINE in phase.additional_notes
        assert "<<" not in phase.additional_notes

    def test_phase_with_no_markers_is_unaffected(self):
        phase = build_phase(
            _base_cfg(additional_notes="No completion boilerplate here at all."),
            default_model="openrouter/xiaomi/mimo-v2.5-pro",
            default_thinking="low",
        )

        assert phase.additional_notes == "No completion boilerplate here at all."

    def test_every_completion_marker_in_every_real_workflow_phase_resolves(self):
        """Live-data check: no phase YAML anywhere under config/workflows/
        ends up with a literal, unresolved <<...>> marker in its rendered
        additional_notes."""
        from src.workflow_engine.yaml_loader import (
            build_phase_list,
            load_workflow_from_dir,
        )

        base = Path(__file__).resolve().parents[1] / "config" / "workflows"
        for workflow_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            cfg = load_workflow_from_dir(workflow_dir)
            for phase in build_phase_list(cfg):
                notes = phase.additional_notes or ""
                assert "<<" not in notes and ">>" not in notes, (
                    f"{workflow_dir.name}/{phase.name} has an unresolved "
                    f"completion marker"
                )
