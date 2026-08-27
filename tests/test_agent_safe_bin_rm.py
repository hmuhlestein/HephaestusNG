"""Tests for scripts/agent-safe-bin/rm — the protected `rm` wrapper CLI
agent sessions get on PATH (see AGENT_SAFE_BIN_DIR in
src/interfaces/cli_interface.py). pi's own destructive-command confirmation
extension was removed, so this script is the only remaining guardrail
against a real `rm -rf` running immediately outside .hephaestus/.
"""

import subprocess
from pathlib import Path

RM_SCRIPT = str(
    Path(__file__).parent.parent / "scripts" / "agent-safe-bin" / "rm"
)


def _run_rm(args, cwd):
    """Run the wrapper with a PATH that resolves the real `rm` normally,
    but with the wrapper script itself invoked directly (not via PATH
    lookup) so the test doesn't depend on install location."""
    return subprocess.run(
        [RM_SCRIPT] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


class TestAgentSafeBinRm:
    def test_allows_deletion_inside_hephaestus_dir(self, tmp_path):
        target = tmp_path / ".hephaestus" / "features" / "orphan"
        target.mkdir(parents=True)
        result = _run_rm(["-rf", ".hephaestus/features/orphan"], cwd=tmp_path)
        assert result.returncode == 0
        assert not target.exists()

    def test_blocks_relative_path_outside_hephaestus(self, tmp_path):
        target = tmp_path / "src" / "important.py"
        target.parent.mkdir(parents=True)
        target.write_text("critical code")
        result = _run_rm(["-rf", "src/important.py"], cwd=tmp_path)
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr
        assert target.exists()

    def test_blocks_absolute_path_outside_hephaestus(self, tmp_path):
        target = tmp_path / "src" / "important.py"
        target.parent.mkdir(parents=True)
        target.write_text("critical code")
        result = _run_rm(["-rf", str(target)], cwd=tmp_path)
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr
        assert target.exists()

    def test_blocks_traversal_out_of_hephaestus(self, tmp_path):
        """A traversal trick (cd into .hephaestus/, then `rm ../src/...`)
        must still resolve to the real path and get blocked -- the check
        operates on the resolved path, not the literal argument string."""
        heph = tmp_path / ".hephaestus"
        heph.mkdir()
        target = tmp_path / "src" / "important.py"
        target.parent.mkdir(parents=True)
        target.write_text("critical code")
        result = _run_rm(["-rf", "../src/important.py"], cwd=heph)
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr
        assert target.exists()

    def test_blocks_if_any_argument_is_outside_hephaestus(self, tmp_path):
        """Mixed arguments (one safe, one not) must fail closed -- refusing
        the whole call rather than partially executing it."""
        safe = tmp_path / ".hephaestus" / "features" / "orphan"
        safe.mkdir(parents=True)
        unsafe = tmp_path / "src" / "important.py"
        unsafe.parent.mkdir(parents=True)
        unsafe.write_text("critical code")
        result = _run_rm(
            ["-rf", ".hephaestus/features/orphan", "src/important.py"],
            cwd=tmp_path,
        )
        assert result.returncode != 0
        assert unsafe.exists()

    def test_script_is_executable(self):
        assert Path(RM_SCRIPT).stat().st_mode & 0o111
