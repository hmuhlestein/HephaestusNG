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

    def test_scan_is_scoped_to_changed_files(self, tmp_path):
        """Regression: every security_review run scanned the whole worktree
        from scratch, even when only a handful of files changed vs. main --
        the same whole-repo-vs-diff mismatch the qa_validation coverage gate
        had. ash has a built-in --changed-files-only flag (falls back to a
        full scan when git is unavailable, so this is safe even outside a
        normal feature branch) -- use it instead of scanning everything."""
        from src.autopilot.orchestrator.worktree_integration import _run_ash_scan

        logger = MagicMock()
        fake_result = MagicMock(stdout="scan output here", stderr="", returncode=0)
        with patch(
            "src.autopilot.orchestrator.worktree_integration.subprocess.run",
            return_value=fake_result,
        ) as mock_run:
            with patch("pathlib.Path.exists", return_value=True):
                _run_ash_scan(tmp_path, logger)

        called_args = mock_run.call_args[0][0]
        assert "--changed-files-only" in called_args

    def test_base_ref_is_the_projects_configured_base_branch(self, tmp_path):
        """Regression: --changed-files-only alone trusts ash's own
        "origin/main" default for --base-ref, which requires a fetched,
        up-to-date origin remote -- not guaranteed for every project this
        tool runs against. A worktree always has its local base branch
        available (it's what it was created from) via
        config.git.base_branch, the same value WorktreeManager uses
        everywhere else for "what's the base branch" -- pass it explicitly
        instead of relying on a remote-tracking ref that may not exist."""
        from src.autopilot.orchestrator.worktree_integration import _run_ash_scan

        logger = MagicMock()
        fake_result = MagicMock(stdout="scan output here", stderr="", returncode=0)
        fake_config = MagicMock()
        fake_config.git.base_branch = "trunk"
        with patch(
            "src.autopilot.orchestrator.worktree_integration.subprocess.run",
            return_value=fake_result,
        ) as mock_run:
            with patch("pathlib.Path.exists", return_value=True):
                with patch(
                    "src.autopilot.orchestrator.worktree_integration.get_config",
                    return_value=fake_config,
                ):
                    _run_ash_scan(tmp_path, logger)

        called_args = mock_run.call_args[0][0]
        assert "--base-ref" in called_args
        assert called_args[called_args.index("--base-ref") + 1] == "trunk"

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

