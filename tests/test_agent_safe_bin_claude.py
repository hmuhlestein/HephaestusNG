"""Tests for scripts/agent-safe-bin/claude -- the anti-mid-pipeline-
autoupdate `claude` wrapper CLI agent sessions get on PATH (see
AGENT_SAFE_BIN_DIR in src/interfaces/cli_interface.py). Claude Code
auto-updates itself in place; if that swap happens while a long pipeline
is mid-run, the binary can briefly not exist, and launch_pipeline.py's
_detect_launch_failure sees "command not found" -- observed live to
exhaust an agent's retry budget after 4.5 hours of otherwise healthy
work. This wrapper sets DISABLE_AUTOUPDATER=1 for exactly the processes
launched through it, without touching the user's own interactive
`claude` sessions elsewhere.
"""

import os
import stat
import subprocess
import threading
import time
from pathlib import Path

CLAUDE_SCRIPT = str(
    Path(__file__).parent.parent / "scripts" / "agent-safe-bin" / "claude"
)


def _make_stub_claude(bin_dir: Path) -> Path:
    """A fake `claude` that just dumps what it was called with -- lets
    tests assert on the wrapper's own behavior without depending on the
    real Claude Code CLI being installed."""
    stub = bin_dir / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "ARGS: $*"\n'
        'echo "DISABLE_AUTOUPDATER=${DISABLE_AUTOUPDATER:-unset}"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def _run_wrapper(args, path_dirs):
    env = dict(os.environ)
    # Prepend rather than replace -- the wrapper's own shebang and the
    # stub script's need `env`/`bash` still resolvable from the rest of
    # the real PATH; only the search order in front of it is what this
    # test controls.
    env["PATH"] = ":".join(str(d) for d in path_dirs) + ":" + env.get("PATH", "")
    return subprocess.run(
        [CLAUDE_SCRIPT] + args,
        capture_output=True,
        text=True,
        env=env,
    )


class TestAgentSafeBinClaude:
    def test_script_is_executable(self):
        assert Path(CLAUDE_SCRIPT).stat().st_mode & 0o111

    def test_sets_disable_autoupdater_for_the_real_binary(self, tmp_path):
        _make_stub_claude(tmp_path)
        result = _run_wrapper([], [Path(CLAUDE_SCRIPT).parent, tmp_path])
        assert result.returncode == 0
        assert "DISABLE_AUTOUPDATER=1" in result.stdout

    def test_finds_the_real_binary_skipping_its_own_directory(self, tmp_path):
        """The wrapper's own directory (scripts/agent-safe-bin) is first
        on PATH in real deployment -- it must skip past itself when
        searching for the real `claude`, or it would exec itself forever."""
        _make_stub_claude(tmp_path)
        result = _run_wrapper(["--version"], [Path(CLAUDE_SCRIPT).parent, tmp_path])
        assert result.returncode == 0
        assert "ARGS: --version" in result.stdout

    def test_passes_through_all_arguments_unchanged(self, tmp_path):
        _make_stub_claude(tmp_path)
        result = _run_wrapper(
            ["--dangerously-skip-permissions", "-p", "some prompt"],
            [Path(CLAUDE_SCRIPT).parent, tmp_path],
        )
        assert result.returncode == 0
        assert "ARGS: --dangerously-skip-permissions -p some prompt" in result.stdout

    def test_propagates_the_real_binarys_exit_code(self, tmp_path):
        failing = tmp_path / "claude"
        failing.write_text("#!/usr/bin/env bash\nexit 7\n")
        failing.chmod(failing.stat().st_mode | stat.S_IEXEC)
        result = _run_wrapper([], [Path(CLAUDE_SCRIPT).parent, tmp_path])
        assert result.returncode == 7


class TestAgentSafeBinClaudeSurvivesTheSwapWindow:
    """The wrapper's behavior when the real binary is not there right now.

    This is the case the wrapper exists for and the one it used to get
    wrong: it fell back to a hard-coded /usr/local/bin/claude, which does
    not exist on a Homebrew or npm global install. So on the very machines
    that hit the swap, the "fallback" produced the exact "command not
    found" the wrapper was written to prevent -- from a path that was never
    right, and with nothing in the output saying so. IDB-2482.
    """

    def _run(self, args, path_dirs, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = ":".join(str(d) for d in path_dirs)
        env.update(env_extra or {})
        return subprocess.run(
            [CLAUDE_SCRIPT] + args, capture_output=True, text=True, env=env
        )

    def test_waits_for_the_binary_to_come_back_instead_of_failing_at_once(
        self, tmp_path
    ):
        """A swap is transient. Exiting the instant the name doesn't
        resolve converts a few seconds of unavailability into a failed
        agent launch; waiting here is the cheapest place to ride it out."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        def _appear_shortly():
            time.sleep(1.5)
            _make_stub_claude(bin_dir)

        appearing = threading.Thread(target=_appear_shortly)
        appearing.start()
        try:
            result = self._run(
                ["--version"],
                [Path(CLAUDE_SCRIPT).parent, bin_dir, "/bin", "/usr/bin"],
                {"CLAUDE_WRAPPER_RESOLVE_TIMEOUT": "20"},
            )
        finally:
            appearing.join()

        assert result.returncode == 0, result.stderr
        assert "ARGS: --version" in result.stdout
        assert "DISABLE_AUTOUPDATER=1" in result.stdout

    def test_reports_what_happened_instead_of_exec_ing_a_guessed_path(
        self, tmp_path
    ):
        """With no claude anywhere on PATH and no wait left, the wrapper
        must say that -- naming the wait it gave up on -- rather than
        exec'ing /usr/local/bin/claude and letting the shell report a
        missing file at a path nobody chose."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = self._run(
            [],
            [Path(CLAUDE_SCRIPT).parent, empty, "/bin", "/usr/bin"],
            {"CLAUDE_WRAPPER_RESOLVE_TIMEOUT": "0"},
        )

        assert result.returncode == 127
        assert "after waiting 0s" in result.stderr
        assert "/usr/local/bin" not in result.stderr

    def test_still_reads_as_a_launch_failure_to_the_orchestrator(self, tmp_path):
        """_detect_launch_failure (launch_pipeline.py) matches "command not
        found" against the pane. The wrapper's own give-up message has to
        keep saying that, or a genuine unresolvable launch stops being
        classified as a launch failure at all -- and stops entering the
        retry spacing that depends on it."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = self._run(
            [],
            [Path(CLAUDE_SCRIPT).parent, empty, "/bin", "/usr/bin"],
            {"CLAUDE_WRAPPER_RESOLVE_TIMEOUT": "0"},
        )
        assert "command not found" in result.stderr
