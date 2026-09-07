"""Tests for the §7.2 fix: LaunchPipeline._detect_launch_failure no longer
trusts a generic "command not found"/"No such file or directory" substring
match in scrollback text on its own -- it corroborates the match against
tmux's own live process state (_pane_has_returned_to_shell, via
`display-message -p '#{pane_current_command}'`) before treating it as a
real failure. A substring match in scrollback is history, not current
state, so on its own it can't tell a CLI that's still running (busy
printing unrelated output that happens to contain one of those phrases)
from one that's actually dead and back at a bare shell prompt.

The docstring on _detect_launch_failure documents a live false positive:
a healthy pi agent logged "ready after 3.1s", then 0.2s later the generic
substring check matched unrelated text from the agent's own in-progress
work and killed the session. These tests construct that same shape of
case (a match against text that isn't actually the shell rejecting the
launch) and confirm it's no longer treated as a failure, while a genuine
dead-pane case (the shell really did reject the command) is still
detected correctly.
"""

import shutil
import subprocess
import time
import uuid
from unittest.mock import MagicMock

import pytest

from src.agents.launch_pipeline import LaunchPipeline

from src.core.database import DatabaseManager


@pytest.fixture
def launch_pipeline():
    from src.agents.manager import AgentManager
    from unittest.mock import Mock

    db_manager = Mock(spec=DatabaseManager)
    llm_provider = Mock()
    agent_manager = AgentManager(db_manager=db_manager, llm_provider=llm_provider)
    return agent_manager._launch


def _pane(capture_pane_lines, pane_current_command):
    """A pane double whose `capture-pane` and `display-message` calls
    return independently -- mirrors what a real tmux pane reports: the
    scrollback text, and (separately) the live foreground process name."""
    pane = MagicMock()

    def _cmd(cmd, *args, **kwargs):
        if cmd == "capture-pane":
            return MagicMock(stdout=capture_pane_lines)
        if cmd == "display-message":
            return MagicMock(stdout=[pane_current_command])
        return MagicMock(stdout=[])

    pane.cmd.side_effect = _cmd
    return pane


class TestGenericRejectionRequiresPaneCorroboration:
    def test_false_positive_when_cli_is_still_running(self, launch_pipeline):
        """Mirrors the docstring's own documented incident: scrollback
        contains a "command not found"-shaped substring from the CLI's own
        legitimate output, but the CLI's process is still the pane's live
        foreground command -- must NOT raise."""
        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [
            r"command not found",
            r"No such file or directory",
        ]
        pane = _pane(
            ["Reading optional config: No such file or directory, using defaults", "› "],
            "pi",
        )

        # Must not raise -- the pane is still running the CLI.
        launch_pipeline._detect_launch_failure(pane, cli_agent, "pi", "session-healthy")

    def test_false_positive_when_interpreter_process_is_running(self, launch_pipeline):
        """Same, but the live process is the CLI's own interpreter (e.g. a
        node/python-launched CLI) rather than a same-named binary -- any
        non-shell foreground process must suppress the false positive."""
        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [r"command not found"]
        pane = _pane(["some tool it ran said: command not found", "working..."], "node")

        launch_pipeline._detect_launch_failure(pane, cli_agent, "claude", "session-node")

    def test_genuine_failure_still_raises_when_pane_is_back_at_shell(self, launch_pipeline):
        """The real failure case: the shell actually rejected the launch
        command and control returned to it -- must still raise."""
        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [
            r"command not found",
            r"No such file or directory",
        ]
        pane = _pane(["zsh: command not found: pi"], "zsh")

        with pytest.raises(Exception, match="shell reported the launch command was not found"):
            launch_pipeline._detect_launch_failure(pane, cli_agent, "pi", "session-dead")

    def test_genuine_failure_detected_for_a_truly_absent_cli(self, launch_pipeline):
        """A CLI that was never installed: the shell rejects it immediately
        and stays at its own prompt -- pane_current_command reports the
        shell itself (bash here, to also cover a non-zsh shell name)."""
        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [
            r"command not found",
            r"No such file or directory",
        ]
        pane = _pane(["bash: codex: command not found"], "bash")

        with pytest.raises(Exception, match="shell reported the launch command was not found"):
            launch_pipeline._detect_launch_failure(pane, cli_agent, "codex", "session-absent")

    def test_confirmation_dialog_pattern_raises_without_needing_corroboration(
        self, launch_pipeline
    ):
        """The Claude Code confirmation-dialog pattern is unambiguous on its
        own and must keep raising regardless of pane_current_command --
        corroboration only applies to the generic substring patterns."""
        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [
            r"command not found",
            r"Bypass Permissions mode",
        ]
        # Foreground process still shows "claude" (i.e. it IS running, just
        # stuck on the dialog) -- the confirmation-dialog branch must still
        # fire since it's checked before pane corroboration.
        pane = _pane(["Bypass Permissions mode?"], "claude")

        with pytest.raises(Exception, match="stuck on an unhandled first-run confirmation"):
            launch_pipeline._detect_launch_failure(pane, cli_agent, "claude", "session-dialog")

    def test_broken_probe_defers_to_the_substring_match_alone(self, launch_pipeline):
        """If the display-message probe itself fails (tmux error), the
        corroboration check must not silently suppress a real failure --
        falls back to today's substring-match-only behavior."""
        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [r"command not found"]
        pane = MagicMock()

        def _cmd(cmd, *args, **kwargs):
            if cmd == "capture-pane":
                return MagicMock(stdout=["command not found: pi"])
            raise RuntimeError("tmux display-message failed")

        pane.cmd.side_effect = _cmd

        with pytest.raises(Exception, match="shell reported the launch command was not found"):
            launch_pipeline._detect_launch_failure(pane, cli_agent, "pi", "session-probe-broken")


class TestLaunchFailureLoggingSurvivesAnsiPadding:
    """Regression, caught chasing a live "claude CLI failed to start"
    incident: every single occurrence's log line showed an EMPTY or
    near-empty string after the colon, making the actual rejection text
    (and thus the real root cause) unrecoverable after the fact. A raw
    capture-pane line is padded to the full terminal width with SGR
    escape codes and trailing spaces -- a single redrawn shell prompt
    line can run 100-200+ raw characters before any visible content. The
    old `.strip()[-300:]` on un-stripped text could spend its entire
    budget on the control-code padding of the last line or two and never
    reach the actual rejection text earlier in the capture, since
    `.strip()` only trims whitespace at the very ends of the whole
    string -- ANSI/SGR bytes there are not whitespace and survive
    untouched, silently consuming the slice."""

    def test_rejection_text_survives_wide_ansi_padded_trailing_lines(self, launch_pipeline):
        """Construct exactly the shape that hid the real error live: the
        genuine rejection appears early, followed by several redrawn-
        prompt lines wide enough (in raw, un-stripped form) to consume a
        300-character tail slice on their own."""
        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [
            r"command not found",
            r"No such file or directory",
        ]
        padded_blank_prompt = "\x1b[1m\x1b[7m%\x1b[27m\x1b[1m\x1b[0m" + (" " * 150) + "\r"
        pane = _pane(
            ["zsh: command not found: claude"] + [padded_blank_prompt] * 6,
            "zsh",
        )

        with pytest.raises(Exception, match="shell reported the launch command was not found"):
            launch_pipeline._detect_launch_failure(pane, cli_agent, "claude", "session-padded")

    def test_logged_message_contains_the_actual_rejection_text(self, launch_pipeline, caplog):
        """Not just "doesn't crash" -- the logged diagnostic must actually
        contain the real rejection text, which is the whole point of the
        fix (the old behavior raised the same exception either way; the
        bug was entirely in what got logged, silently, alongside it)."""
        import logging

        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [r"command not found"]
        padded_blank_prompt = "\x1b[1m\x1b[7m%\x1b[27m\x1b[1m\x1b[0m" + (" " * 150) + "\r"
        pane = _pane(
            ["zsh: command not found: claude"] + [padded_blank_prompt] * 6,
            "zsh",
        )

        with caplog.at_level(logging.ERROR):
            with pytest.raises(Exception):
                launch_pipeline._detect_launch_failure(pane, cli_agent, "claude", "session-padded")

        assert any("command not found: claude" in r.message for r in caplog.records)


class TestPaneHasReturnedToShell:
    """Direct unit tests for the corroboration helper itself."""

    @pytest.mark.parametrize("shell_name", ["zsh", "bash", "sh", "-zsh".lstrip("-"), "fish"])
    def test_true_for_known_shell_names(self, launch_pipeline, shell_name):
        pane = _pane([], shell_name)
        assert launch_pipeline._pane_has_returned_to_shell(pane) is True

    @pytest.mark.parametrize("process_name", ["pi", "claude", "node", "python3", "codex"])
    def test_false_for_a_live_non_shell_process(self, launch_pipeline, process_name):
        pane = _pane([], process_name)
        assert launch_pipeline._pane_has_returned_to_shell(pane) is False

    def test_true_when_probe_returns_empty(self, launch_pipeline):
        """tmux always names *something* for a real pane -- empty output
        means the probe didn't work, not that nothing is running. Defer to
        the substring match (True) rather than treat silence as a live
        process (False)."""
        pane = _pane([], "")
        assert launch_pipeline._pane_has_returned_to_shell(pane) is True

    def test_true_when_probe_raises(self, launch_pipeline):
        pane = MagicMock()
        pane.cmd.side_effect = RuntimeError("boom")
        assert launch_pipeline._pane_has_returned_to_shell(pane) is True


class TestSubshellLaunchShapeDoesNotReadAsABareShell:
    """_pane_has_returned_to_shell must not call a `(a || b)` subshell
    wrapper "back at the shell" while the CLI runs underneath it.

    ClaudeCodeAgent emits `(claude --session-id X ... || claude --resume X
    ...)` whenever a session_id is set -- essentially every phase agent. A
    shell cannot exec-optimize a subshell whose second branch it may still
    need to run, so it stays in the foreground and tmux reports the SHELL
    as pane_current_command for the whole life of a healthy agent. This
    check therefore returned True unconditionally for that shape, reducing
    itself to the bare substring match it exists to corroborate.

    Observed live, twice in one run: an agent that had read its task file
    and set its own goal was killed as "failed to start" because Claude
    Code printed a failing MCP server's ENOENT ("no such file or
    directory") into the pane. IDB-2482.
    """

    @staticmethod
    def _pane(current_command, pane_pid="4242"):
        pane = MagicMock()

        def _cmd(*args, **kwargs):
            fmt = args[-1] if args else ""
            if "pane_current_command" in fmt:
                return MagicMock(stdout=[current_command])
            if "pane_pid" in fmt:
                return MagicMock(stdout=[pane_pid])
            return MagicMock(stdout=[])

        pane.cmd.side_effect = _cmd
        return pane

    def test_a_shell_with_a_live_child_has_not_returned_to_a_prompt(self, monkeypatch):
        monkeypatch.setattr(
            LaunchPipeline, "_pane_shell_has_live_children", staticmethod(lambda _p: True)
        )
        assert LaunchPipeline._pane_has_returned_to_shell(self._pane("zsh")) is False

    def test_a_shell_with_no_children_is_a_bare_prompt(self, monkeypatch):
        monkeypatch.setattr(
            LaunchPipeline, "_pane_shell_has_live_children", staticmethod(lambda _p: False)
        )
        assert LaunchPipeline._pane_has_returned_to_shell(self._pane("zsh")) is True

    def test_a_non_shell_foreground_still_short_circuits(self, monkeypatch):
        """The pre-existing fast path: a CLI in the foreground under its own
        name never needs the child probe at all."""
        def _boom(_p):
            raise AssertionError("must not probe children when the CLI is in the foreground")

        monkeypatch.setattr(
            LaunchPipeline, "_pane_shell_has_live_children", staticmethod(_boom)
        )
        assert LaunchPipeline._pane_has_returned_to_shell(self._pane("node")) is False

    def test_a_broken_child_probe_defers_to_the_substring_match(self):
        """Bias has to match the caller's: a failed probe may fail to rescue
        a false positive, but must never suppress a real launch failure."""
        pane = self._pane("zsh", pane_pid="not-a-pid")
        assert LaunchPipeline._pane_shell_has_live_children(pane) is False
        assert LaunchPipeline._pane_has_returned_to_shell(pane) is True

    @pytest.mark.skipif(
        shutil.which("tmux") is None, reason="tmux not installed"
    )
    def test_against_real_tmux_the_two_launch_shapes_differ(self):
        """The measurement this whole fix rests on, run for real rather than
        asserted from memory: the subshell shape reports a shell with a live
        child, a bare command reports itself."""
        session = f"heptest_{uuid.uuid4().hex[:8]}"

        def _tmux(*args):
            return subprocess.run(
                ["tmux", *args], capture_output=True, text=True, timeout=10
            )

        def _foreground():
            return _tmux(
                "display-message", "-p", "-t", session, "#{pane_current_command}"
            ).stdout.strip()

        try:
            _tmux("new-session", "-d", "-s", session, "sh")
            time.sleep(1)
            _tmux("send-keys", "-t", session, "(sleep 30 || sleep 1)", "Enter")
            time.sleep(2)
            subshell_fg = _foreground()
            pane_pid = _tmux(
                "display-message", "-p", "-t", session, "#{pane_pid}"
            ).stdout.strip()
            children = subprocess.run(
                ["pgrep", "-P", pane_pid], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        finally:
            _tmux("kill-session", "-t", session)

        # The foreground is the shell -- which is exactly why the old check
        # was useless here -- but the shell has a live child.
        assert subshell_fg in LaunchPipeline._SHELL_PROCESS_NAMES, subshell_fg
        assert children, "expected a live child under the subshell wrapper"
