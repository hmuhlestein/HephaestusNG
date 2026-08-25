"""Tests for the mandatory automated security scan (ash) enforcement.

security_review.yaml marks running scripts/ash MANDATORY, but an agent was
observed skipping it entirely during smoke testing with no note of the
skip (as the prompt explicitly asked for on failure). _run_ash_scan makes
the orchestrator run it unconditionally before the security_review agent
starts, removing the compliance gap.
"""

import subprocess
from unittest.mock import MagicMock, patch

from src.core.constants import CONTEXT_DIR_NAME


class TestRunAshScan:
    def test_writes_results_file_on_success(self, tmp_path):
        from src.autopilot.orchestrator.worktree_integration import _run_ash_scan

        logger = MagicMock()
        fake_result = MagicMock(stdout="scan output here", stderr="", returncode=0)
        with patch("src.autopilot.orchestrator.worktree_integration.subprocess.run", return_value=fake_result):
            with patch("pathlib.Path.exists", return_value=True):
                _run_ash_scan(tmp_path, logger)

        results_path = tmp_path / CONTEXT_DIR_NAME / "ash_results.txt"
        assert results_path.exists()
        assert "scan output here" in results_path.read_text()

    def test_writes_failure_marker_on_timeout(self, tmp_path):
        from src.autopilot.orchestrator.worktree_integration import _run_ash_scan

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.worktree_integration.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ash", timeout=300),
        ):
            with patch("pathlib.Path.exists", return_value=True):
                _run_ash_scan(tmp_path, logger)

        results_path = tmp_path / CONTEXT_DIR_NAME / "ash_results.txt"
        assert results_path.exists()
        assert "TIMED OUT" in results_path.read_text()

    def test_writes_failure_marker_on_exception(self, tmp_path):
        from src.autopilot.orchestrator.worktree_integration import _run_ash_scan

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.worktree_integration.subprocess.run",
            side_effect=OSError("uvx not found"),
        ):
            with patch("pathlib.Path.exists", return_value=True):
                _run_ash_scan(tmp_path, logger)

        results_path = tmp_path / CONTEXT_DIR_NAME / "ash_results.txt"
        assert results_path.exists()
        assert "FAILED TO RUN" in results_path.read_text()
        assert "uvx not found" in results_path.read_text()

    def test_writes_the_failure_marker_when_ash_script_missing(self, tmp_path):
        """If scripts/ash doesn't exist at the derived repo path, don't crash
        -- and DO write the same failure marker every other failure path
        writes.

        This assertion was inverted ("don't write a misleading results
        file") back when verify_output_artifact's ash-scan content check was
        dead and writing nothing was harmless. It isn't any more:
        security_review.yaml tells the agent to cat this file and quote it
        verbatim if it reports a failure, and a security.md with no
        "## Automated Scan Results" section is now rejected. With no file at
        all the agent cats a nonexistent path, has no sanctioned way to
        report why, and gets rejected for a section it had no way to fill.
        Writing the marker is what lets it say "SCAN FAILED TO RUN" and
        continue, exactly as the prompt instructs."""
        from src.autopilot.orchestrator.worktree_integration import _run_ash_scan

        logger = MagicMock()
        with patch("pathlib.Path.exists", return_value=False):
            _run_ash_scan(tmp_path, logger)

        results_path = tmp_path / CONTEXT_DIR_NAME / "ash_results.txt"
        assert results_path.exists()
        assert "SCAN FAILED TO RUN" in results_path.read_text()
        assert "ash not installed" in results_path.read_text()

