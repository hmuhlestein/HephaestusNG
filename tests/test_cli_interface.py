"""Tests for CLI agent interface command construction."""

import json
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from src.interfaces.cli_interface import ClaudeCodeAgent, CodexAgent, LaunchResult


class TestClaudeSessionExists:
    """_claude_session_exists mirrors Claude Code's own project-key
    sanitization (every '/', '.', '_' in the canonical path becomes '-')
    to look up whether a session uuid already has a stored transcript."""

    def test_returns_true_when_session_file_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        working_directory = "/Users/test/code/Proj/.worktrees/wt_feature-x"
        project_key = "-Users-test-code-Proj--worktrees-wt-feature-x"
        session_dir = tmp_path / ".claude" / "projects" / project_key
        session_dir.mkdir(parents=True)
        (session_dir / "abc123.jsonl").write_text("{}")

        with patch("os.path.realpath", return_value=working_directory):
            assert ClaudeCodeAgent._claude_session_exists(
                working_directory, "abc123"
            )

    def test_returns_false_when_session_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        working_directory = "/Users/test/code/Proj/.worktrees/wt_feature-x"

        with patch("os.path.realpath", return_value=working_directory):
            assert not ClaudeCodeAgent._claude_session_exists(
                working_directory, "does-not-exist"
            )

    def test_never_raises_on_bad_input(self):
        # No home directory patch -- exercises the real filesystem, which
        # simply won't have this uuid; the point is no exception escapes.
        assert not ClaudeCodeAgent._claude_session_exists("", "whatever")


class TestGetLaunchCommandSessionOrdering:
    """Regression: launching with a reused session_id always tried
    --session-id first and ate a guaranteed "already in use" error on every
    resumed session before falling back to --resume. That error text was
    then fed straight to Guardian's LLM analysis, which misread it as a
    live problem and fabricated a bogus steering message (see
    src/monitoring/guardian.py's _sanitize_tmux_output_for_llm). Checking
    for an existing session file first lets the launch command try the
    branch that will actually succeed, avoiding the error in the common
    case while keeping the || fallback for when the heuristic is wrong."""

    def _agent(self):
        agent = ClaudeCodeAgent()
        return agent

    def test_tries_resume_first_when_session_already_exists(self):
        agent = self._agent()
        with patch.object(
            ClaudeCodeAgent, "_claude_session_exists", return_value=True
        ):
            result = agent.get_launch_command(
                system_prompt="do the thing",
                task_id="task-1",
                session_id="hephaestus-proj-design-role-abcd1234",
                working_directory="/tmp/some/worktree",
            )
        assert result.command.startswith('(PATH="')
        assert '" claude --resume ' in result.command
        assert '" claude --session-id ' in result.command
        assert " || " in result.command

    def test_tries_session_id_first_when_session_is_new(self):
        agent = self._agent()
        with patch.object(
            ClaudeCodeAgent, "_claude_session_exists", return_value=False
        ):
            result = agent.get_launch_command(
                system_prompt="do the thing",
                task_id="task-2",
                session_id="hephaestus-proj-design-role-abcd1234",
                working_directory="/tmp/some/worktree",
            )
        assert result.command.startswith('(PATH="')
        assert '" claude --session-id ' in result.command
        assert '" claude --resume ' in result.command
        assert " || " in result.command

    def test_defaults_to_session_id_first_without_working_directory(self):
        # No working_directory means the existence check can't run at all --
        # must fall back to the original always-safe ordering, not skip
        # the check silently in a way that picks the wrong default.
        agent = self._agent()
        with patch.object(
            ClaudeCodeAgent, "_claude_session_exists"
        ) as mock_exists:
            result = agent.get_launch_command(
                system_prompt="do the thing",
                task_id="task-3",
                session_id="hephaestus-proj-design-role-abcd1234",
            )
        mock_exists.assert_not_called()
        assert result.command.startswith('(PATH="')
        assert '" claude --session-id ' in result.command


class TestGetLaunchCommandInstalledAgent:
    """When install.sh has generated+installed a per-phase Claude Code
    subagent (~/.claude/agents/hephaestus-{phase}.md), the launch command
    should use --agent <name> -- Claude Code's own officially supported
    named-agent flag -- instead of hand-rolling --append-system-prompt.
    Mirrors PiAgent's equivalent per-phase agent-file lookup."""

    def test_uses_agent_flag_when_installed_file_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "hephaestus-development.md").write_text("---\nname: x\n---\nbody")

        agent = ClaudeCodeAgent()
        result = agent.get_launch_command(
            system_prompt="do the thing",
            task_id="task-1",
            phase_name="development",
        )
        assert "--agent hephaestus-development" in result.command
        assert "--append-system-prompt" not in result.command
        assert result.prompt_delivery == "agent_file"

    def test_falls_back_to_append_system_prompt_when_no_installed_file(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))

        agent = ClaudeCodeAgent()
        result = agent.get_launch_command(
            system_prompt="do the thing",
            task_id="task-2",
            phase_name="development",
        )
        assert "--append-system-prompt" in result.command
        assert "--agent " not in result.command
        assert result.prompt_delivery == "flag"

    def test_falls_back_to_append_system_prompt_without_phase_name(self):
        agent = ClaudeCodeAgent()
        result = agent.get_launch_command(
            system_prompt="do the thing",
            task_id="task-3",
        )
        assert "--append-system-prompt" in result.command
        assert "--agent " not in result.command
        assert result.prompt_delivery == "flag"


class TestCodexAgent:
    def test_installed_codex_supports_required_launch_options(self):
        if not shutil.which("codex"):
            pytest.skip("Codex CLI is not installed")

        result = subprocess.run(["codex", "--help"], capture_output=True, text=True)

        assert result.returncode == 0
        assert "--dangerously-bypass-approvals-and-sandbox" in result.stdout
        assert "--no-alt-screen" in result.stdout

    def test_launches_interactively_with_deferred_instructions(self):
        result = CodexAgent().get_launch_command(
            system_prompt="system prompt", task_id="task-1", model="gpt-5.6-terra"
        )

        assert "codex --dangerously-bypass-approvals-and-sandbox" in result.command
        assert "--no-alt-screen" in result.command
        assert "--model gpt-5.6-terra" in result.command
        assert result.prompt_delivery == LaunchResult.DEFERRED

    def test_uses_codex_default_model_when_no_override_is_given(self):
        result = CodexAgent().get_launch_command(
            system_prompt="system prompt", task_id="task-1"
        )

        assert "--model" not in result.command

    def test_formats_task_as_plain_prompt(self):
        assert CodexAgent().format_message("Implement the change") == "Implement the change"

    def test_resumes_recorded_session(self, tmp_path):
        working_directory = tmp_path / "worktree"
        session_map = working_directory / ".hephaestus" / "codex_sessions.json"
        session_map.parent.mkdir(parents=True)
        session_map.write_text(
            json.dumps({"heph-session": "019ff292-2164-74b2-8f9a-01b68469cd99"})
        )

        result = CodexAgent().get_launch_command(
            system_prompt="system prompt",
            task_id="task-1",
            session_id="heph-session",
            working_directory=str(working_directory),
        )

        assert "codex resume 019ff292-2164-74b2-8f9a-01b68469cd99" in result.command

    def test_records_session_created_in_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        working_directory = tmp_path / "worktree"
        working_directory.mkdir()
        transcript = tmp_path / ".codex" / "sessions" / "2026" / "08" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "session_id": "019ff292-2164-74b2-8f9a-01b68469cd99",
                        "cwd": str(working_directory),
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "content": "Hephaestus Session ID: heph-session"
                    },
                }
            )
            + "\n"
        )

        agent = CodexAgent()
        agent.record_session("heph-session", str(working_directory), time.time())

        assert CodexAgent._saved_session_id("heph-session", str(working_directory)) == (
            "019ff292-2164-74b2-8f9a-01b68469cd99"
        )

    def test_does_not_record_unmarked_session_from_same_directory(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        working_directory = tmp_path / "worktree"
        working_directory.mkdir()
        transcript = tmp_path / ".codex" / "sessions" / "2026" / "08" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "session_id": "019ff292-2164-74b2-8f9a-01b68469cd99",
                        "cwd": str(working_directory),
                    },
                }
            )
            + "\n"
        )

        CodexAgent().record_session("heph-session", str(working_directory), time.time())

        assert CodexAgent._saved_session_id("heph-session", str(working_directory)) is None
