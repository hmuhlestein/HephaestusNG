"""A model name belongs to a CLI tool, not to the tool slot.

"sonnet"/"opus" are Claude Code aliases, "auto" is kiro's "you pick", pi
wants a .gguf filename. The config used to hold ONE model string applied
to whichever tool happened to be default, in a separate key from the tool
itself -- two values that had to agree with nothing enforcing it.

They stopped agreeing. 94f5e443 switched default_cli_tool claude->kiro AND
cli_model sonnet->auto; dea1e374 switched the tool back and left the model.
Every agent then launched as `claude --model auto` and died at 0 tokens
with "There's an issue with the selected model (auto)". Nothing warned:
the file looked fine, and the mismatch only showed at launch.

These tests pin the shape that makes that unwritable, and the precedence
that lets an operator change a model without editing this repo.
"""

import os
from unittest.mock import patch

import pytest

from src.core.simple_config import AgentConfig
from src.workflow_engine.yaml_loader import build_phase, resolve_model_for_tool


def _agents(**agents):
    return AgentConfig({"agents": agents})


class TestModelIsKeyedByTool:
    def test_each_tool_gets_its_own_model(self):
        cfg = _agents(default_cli_tool="claude", models={"claude": "sonnet", "kiro": "auto"})
        assert cfg.model_for("claude") == "sonnet"
        assert cfg.model_for("kiro") == "auto"

    def test_a_tool_with_no_entry_defers_to_its_own_adapter(self):
        """None is a real answer: every CLIAgentInterface declares a
        default_model correct for itself, so the tool answers for itself
        rather than inheriting a stranger's model."""
        cfg = _agents(default_cli_tool="claude", models={"claude": "sonnet"})
        assert cfg.model_for("pi") is None
        assert cfg.model_for("kiro") is None

    def test_legacy_flat_key_applies_only_to_the_default_tool(self):
        """This is the whole bug in one assertion: a flat cli_model must
        never reach a tool that is not the default one."""
        cfg = _agents(default_cli_tool="claude", cli_model="sonnet")
        assert cfg.model_for("claude") == "sonnet"
        assert cfg.model_for("kiro") is None
        assert cfg.model_for("pi") is None

    def test_explicit_entry_beats_the_legacy_flat_key(self):
        cfg = _agents(default_cli_tool="claude", cli_model="sonnet", models={"claude": "opus"})
        assert cfg.model_for("claude") == "opus"

    def test_empty_tool_name_is_not_a_lookup(self):
        assert _agents(default_cli_tool="claude", models={"claude": "sonnet"}).model_for("") is None


class TestOperatorCanOverrideWithoutEditingTheRepo:
    """The original complaint: CLI_MODEL existed but was dead config,
    because the shipped workflow YAML always won."""

    def test_per_tool_env_var_sets_that_tool(self):
        with patch.dict(os.environ, {"CLI_MODEL_CLAUDE": "opus"}, clear=False):
            cfg = _agents(default_cli_tool="claude", models={"claude": "sonnet"})
            cfg.apply_env_overrides()
        assert cfg.model_for("claude") == "opus"

    def test_per_tool_env_var_does_not_leak_to_other_tools(self):
        with patch.dict(os.environ, {"CLI_MODEL_CLAUDE": "opus"}, clear=False):
            cfg = _agents(default_cli_tool="claude", models={"kiro": "auto"})
            cfg.apply_env_overrides()
        assert cfg.model_for("claude") == "opus"
        assert cfg.model_for("kiro") == "auto"

    def test_legacy_env_var_still_works_and_stays_scoped(self):
        with patch.dict(os.environ, {"CLI_MODEL": "haiku"}, clear=False):
            cfg = _agents(default_cli_tool="claude")
            cfg.apply_env_overrides()
        assert cfg.model_for("claude") == "haiku"
        assert cfg.model_for("kiro") is None


class TestMismatchIsAnnouncedNotDiscoveredAtLaunch:
    def test_the_original_bug_produces_a_warning(self):
        """claude configured with 'auto' -- kiro's default -- is the exact
        state that killed every agent, and it said nothing."""
        cfg = _agents(default_cli_tool="claude", cli_model="auto")
        warnings = cfg.model_mismatch_warnings()
        assert len(warnings) == 1
        assert "claude" in warnings[0] and "auto" in warnings[0] and "kiro" in warnings[0]

    def test_a_tools_own_default_is_not_a_mismatch(self):
        assert _agents(default_cli_tool="kiro", models={"kiro": "auto"}).model_mismatch_warnings() == []

    def test_a_correct_config_is_silent(self):
        assert _agents(default_cli_tool="claude", models={"claude": "sonnet"}).model_mismatch_warnings() == []


class TestResolutionPrecedence:
    def test_operator_config_beats_the_shipped_workflow_yaml(self):
        with patch("src.core.simple_config.Config") as C:
            C.return_value.agents.model_for.return_value = "opus"
            got = resolve_model_for_tool("claude", {"claude": "sonnet"}, "sonnet", "claude")
        assert got == "opus"

    def test_workflow_map_used_when_no_operator_setting(self):
        with patch("src.core.simple_config.Config") as C:
            C.return_value.agents.model_for.return_value = None
            got = resolve_model_for_tool("claude", {"claude": "sonnet"}, None, "claude")
        assert got == "sonnet"

    def test_legacy_default_model_applies_only_to_the_workflows_default_tool(self):
        with patch("src.core.simple_config.Config") as C:
            C.return_value.agents.model_for.return_value = None
            assert resolve_model_for_tool("claude", None, "sonnet", "claude") == "sonnet"
            assert resolve_model_for_tool("kiro", None, "sonnet", "claude") is None

    def test_nothing_configured_defers_to_the_adapter(self):
        with patch("src.core.simple_config.Config") as C:
            C.return_value.agents.model_for.return_value = None
            assert resolve_model_for_tool("pi", None, None, "claude") is None

    def test_unreadable_config_falls_through_instead_of_raising(self):
        with patch("src.core.simple_config.Config", side_effect=RuntimeError("no config")):
            assert resolve_model_for_tool("claude", {"claude": "sonnet"}, None, "claude") == "sonnet"


class TestPhaseResolution:
    def _phase(self, phase_cfg, **kw):
        kw.setdefault("default_thinking", "low")
        kw.setdefault("default_cli_tool", "claude")
        with patch("src.core.simple_config.Config") as C:
            C.return_value.agents.model_for.return_value = kw.pop("operator_model", None)
            return build_phase(
                phase_cfg,
                kw.pop("default_model", None),
                kw.pop("default_thinking"),
                None, None,
                kw.pop("default_cli_tool"),
                kw.pop("models", None),
            )

    def test_phase_inherits_its_tools_model(self):
        p = self._phase({"id": 1, "name": "x"}, models={"claude": "sonnet"})
        assert (p.cli_tool, p.cli_model) == ("claude", "sonnet")

    def test_a_phase_that_overrides_the_tool_gets_that_tools_model(self):
        """The per-tool property that matters most: switching a phase's CLI
        must not hand it the default tool's model."""
        p = self._phase({"id": 1, "name": "x", "cli_tool": "kiro"},
                        models={"claude": "sonnet", "kiro": "auto"})
        assert (p.cli_tool, p.cli_model) == ("kiro", "auto")

    def test_explicit_phase_model_beats_the_operator_override(self):
        """A per-phase model is a deliberate choice, not a default."""
        p = self._phase({"id": 1, "name": "x", "cli_model": "opus"},
                        models={"claude": "sonnet"}, operator_model="haiku")
        assert p.cli_model == "opus"

    def test_no_model_anywhere_leaves_it_for_the_adapter(self):
        """It used to hard-code 'xiaomi/mimo-v2.5' here -- a model nothing
        in this deployment can run, handed silently to any workflow that
        omitted default_model."""
        p = self._phase({"id": 1, "name": "x"})
        assert p.cli_model is None
