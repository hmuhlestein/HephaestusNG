"""Tests for CLI agent interface command construction."""

import json
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.interfaces.cli_interface import (
    AGENT_LAUNCH_ENV_PREFIX,
    AGENT_LAUNCH_ENV_STATEMENT,
    CLI_AGENTS,
    ClaudeCodeAgent,
    CodexAgent,
    LaunchResult,
)


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
        assert result.command.startswith(f"({AGENT_LAUNCH_ENV_PREFIX} claude ")
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
        assert result.command.startswith(f"({AGENT_LAUNCH_ENV_PREFIX} claude ")
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
        assert result.command.startswith(f"({AGENT_LAUNCH_ENV_PREFIX} claude ")
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
        assert "--enable" in result.stdout

    def test_launches_interactively_with_deferred_instructions(self):
        result = CodexAgent().get_launch_command(
            system_prompt="system prompt", task_id="task-1", model="gpt-5.6-terra"
        )

        assert "codex --dangerously-bypass-approvals-and-sandbox" in result.command
        assert "--no-alt-screen" in result.command
        assert "--enable goals" in result.command
        assert "--model gpt-5.6-terra" in result.command
        assert result.prompt_delivery == LaunchResult.DEFERRED

    def test_format_goal_command_uses_native_codex_goal_command(self):
        """Codex CLI's goals feature (thread-scoped objective tracking,
        backed by create_goal/update_goal tools) is enabled via --enable
        goals above; /goal <objective> is its slash-command entry point,
        the same shape as Claude Code's /goal <condition>."""
        assert CodexAgent().format_goal_command("all tests pass") == "/goal all tests pass"

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
        assert result.command.startswith(f"{AGENT_LAUNCH_ENV_STATEMENT} (")
        assert "|| codex --dangerously-bypass-approvals-and-sandbox" in result.command
        subprocess.run(["bash", "-n", "-c", result.command], check=True)

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


class TestAgentLaunchDisablesTheAutoUpdater:
    """Every agent Hephaestus launches must run with DISABLE_AUTOUPDATER=1,
    set by the orchestrator in the launch command itself.

    A CLI that updates itself mid-run replaces its own binary in place, and
    for the few seconds that takes its name doesn't resolve -- any agent
    launching in that window dies on the shell's "command not found"
    (_detect_launch_failure, launch_pipeline.py). Observed live ending four
    separate runs across CLI versions 2.1.258 -> .259 -> .260, one of them
    4.5 hours in.

    scripts/agent-safe-bin/claude also sets this and is kept as a second
    line of defence, but it cannot be the primary mechanism: it only fires
    if AGENT_SAFE_BIN_DIR is genuinely on PATH and genuinely contains the
    wrapper, and `scripts/` is not part of the built package (pyproject.toml
    ships `src` only) -- so on a non-editable install that directory does
    not exist, the PATH prefix is inert, and `claude` resolves straight to
    the real binary with the wrapper never running. A variable assignment
    in front of the command has no such failure mode. IDB-2482.
    """

    @pytest.mark.parametrize("cli_type", sorted(CLI_AGENTS))
    def test_every_registered_cli_launches_with_the_updater_disabled(self, cli_type):
        """Parametrized over the registry rather than a hand-written list,
        so a CLI added to CLI_AGENTS without the shared prefix fails here
        instead of shipping a launch path that can still be swapped out
        from under itself."""
        result = CLI_AGENTS[cli_type]().get_launch_command(
            system_prompt="do the thing", task_id="task-1"
        )
        assert "DISABLE_AUTOUPDATER=1" in result.command

    def test_the_statement_form_exports_rather_than_assigning(self):
        """The `(a || b)` launch shapes set their environment up front
        instead of prefixing one command, and there a bare assignment is
        not enough: PATH survives because it is already exported, but
        DISABLE_AUTOUPDATER would be a shell-local variable the CLI process
        never sees -- silently leaving the auto-updater on for exactly the
        CLIs that use this form."""
        assert AGENT_LAUNCH_ENV_STATEMENT.startswith("export DISABLE_AUTOUPDATER=1;")

    def _stub_cli(self, bin_dir, name):
        """A fake CLI that reports the env var it was actually launched
        with, so these tests assert on what reaches the process rather than
        on the text of the command string."""
        stub = bin_dir / name
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'echo "DISABLE_AUTOUPDATER=${DISABLE_AUTOUPDATER:-unset}"\n'
        )
        stub.chmod(0o755)
        return stub

    def _run(self, command, bin_dir):
        return subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            env={"PATH": f"{bin_dir}:/bin:/usr/bin"},
        )

    def test_the_env_var_reaches_the_launched_claude_process(self, tmp_path):
        self._stub_cli(tmp_path, "claude")
        with patch.object(
            ClaudeCodeAgent, "_claude_session_exists", return_value=False
        ):
            result = ClaudeCodeAgent().get_launch_command(
                system_prompt="do the thing",
                task_id="task-1",
                session_id="hephaestus-proj-design-role-abcd1234",
                working_directory=str(tmp_path),
            )

        completed = self._run(result.command, tmp_path)
        assert completed.returncode == 0, completed.stderr
        assert "DISABLE_AUTOUPDATER=1" in completed.stdout

    def test_the_env_var_reaches_both_halves_of_the_session_fallback(self, tmp_path):
        """Claude's launch command is `(--session-id … || --resume …)`. The
        second half runs precisely when the first has already failed, which
        is the retry that most needs the updater off -- a prefix applied to
        only one of the two would leave that attempt exposed."""
        failing_first = tmp_path / "claude"
        failing_first.write_text(
            "#!/usr/bin/env bash\n"
            'echo "DISABLE_AUTOUPDATER=${DISABLE_AUTOUPDATER:-unset}"\n'
            'for arg in "$@"; do\n'
            '    if [[ "$arg" == "--session-id" ]]; then exit 1; fi\n'
            "done\n"
        )
        failing_first.chmod(0o755)

        with patch.object(
            ClaudeCodeAgent, "_claude_session_exists", return_value=False
        ):
            result = ClaudeCodeAgent().get_launch_command(
                system_prompt="do the thing",
                task_id="task-1",
                session_id="hephaestus-proj-design-role-abcd1234",
                working_directory=str(tmp_path),
            )

        completed = self._run(result.command, tmp_path)
        assert completed.returncode == 0, completed.stderr
        # Both attempts ran, and both were told not to auto-update.
        assert completed.stdout.count("DISABLE_AUTOUPDATER=1") == 2
        assert "unset" not in completed.stdout

    def test_the_env_var_reaches_a_codex_resume_pair(self, tmp_path, monkeypatch):
        """Codex uses the statement form, which is the shape a bare
        assignment would have broken."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        working_directory = tmp_path / "worktree"
        working_directory.mkdir()
        session_map = tmp_path / ".hephaestus" / "codex_sessions.json"
        session_map.parent.mkdir(parents=True)
        session_map.write_text(
            json.dumps({"heph-session": "019ff292-2164-74b2-8f9a-01b68469cd99"})
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self._stub_cli(bin_dir, "codex")

        result = CodexAgent().get_launch_command(
            system_prompt="system prompt",
            task_id="task-1",
            session_id="heph-session",
            working_directory=str(working_directory),
        )

        completed = self._run(result.command, bin_dir)
        assert completed.returncode == 0, completed.stderr
        assert "DISABLE_AUTOUPDATER=1" in completed.stdout
        assert "unset" not in completed.stdout
