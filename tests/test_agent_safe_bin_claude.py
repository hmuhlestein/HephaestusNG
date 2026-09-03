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
