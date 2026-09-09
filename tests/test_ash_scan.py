"""Tests for the mandatory automated security scan (ash) enforcement.

security_review.yaml marks running scripts/ash MANDATORY, but an agent was
observed skipping it entirely during smoke testing with no note of the
skip (as the prompt explicitly asked for on failure). _run_ash_scan makes
the orchestrator run it unconditionally before the security_review agent
starts, removing the compliance gap.
"""

import json
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

    def test_appends_detect_secrets_findings_to_results_file(self, tmp_path):
        """Regression: ash's own console summary gives detect-secrets a
        bare pass/fail count with no file:line detail (unlike
        bandit/checkov/npm-audit, whose console output already includes it
        inline) -- security_review agents had nowhere to look, and fell
        back to re-running detect-secrets themselves against the whole
        repo every dispatch (observed live: 8/8 dispatches, identical
        472-finding unscoped result). The real per-finding detail exists in
        detect-secrets' own SARIF file under .ash/, which the scan's
        cleanup deletes -- must be read and appended before that happens."""
        from src.autopilot.orchestrator.worktree_integration import _run_ash_scan

        sarif_path = (
            tmp_path / ".ash" / "ash_output" / "scanners" / "detect-secrets" / "source" / "results_sarif.sarif"
        )
        sarif_path.parent.mkdir(parents=True)
        sarif_path.write_text(json.dumps({
            "runs": [{
                "results": [
                    {"ruleId": "SECRET-AWS-ACCESS-KEY", "message": {
                        "text": "Secret of type 'AWS Access Key' detected in file 'src/config.py' at line 12"
                    }},
                ]
            }]
        }))

        logger = MagicMock()
        fake_result = MagicMock(stdout="ash summary table here", stderr="", returncode=2)
        with patch(
            "src.autopilot.orchestrator.worktree_integration.subprocess.run",
            return_value=fake_result,
        ):
            with patch("pathlib.Path.exists", return_value=True):
                _run_ash_scan(tmp_path, logger)

        results_text = (tmp_path / CONTEXT_DIR_NAME / "ash_results.txt").read_text()
        assert "ash summary table here" in results_text
        assert "src/config.py" in results_text
        assert "line 12" in results_text

    def test_no_detect_secrets_section_when_sarif_absent(self, tmp_path):
        """No SARIF file (scanner didn't run, or ash's own layout changed)
        must not break the scan -- ash's own output is still written as
        before, just without the enrichment."""
        from src.autopilot.orchestrator.worktree_integration import _run_ash_scan

        logger = MagicMock()
        fake_result = MagicMock(stdout="ash summary table here", stderr="", returncode=0)
        with patch(
            "src.autopilot.orchestrator.worktree_integration.subprocess.run",
            return_value=fake_result,
        ):
            with patch("pathlib.Path.exists", return_value=True):
                _run_ash_scan(tmp_path, logger)

        results_text = (tmp_path / CONTEXT_DIR_NAME / "ash_results.txt").read_text()
        assert results_text.strip() == "ash summary table here"


class TestExtractDetectSecretsFindings:
    def test_returns_none_when_sarif_file_missing(self, tmp_path):
        from src.autopilot.orchestrator.worktree_integration import _extract_detect_secrets_findings

        assert _extract_detect_secrets_findings(tmp_path) is None

    def test_returns_none_when_sarif_is_malformed(self, tmp_path):
        from src.autopilot.orchestrator.worktree_integration import _extract_detect_secrets_findings

        sarif_path = (
            tmp_path / ".ash" / "ash_output" / "scanners" / "detect-secrets" / "source" / "results_sarif.sarif"
        )
        sarif_path.parent.mkdir(parents=True)
        sarif_path.write_text("not json")

        assert _extract_detect_secrets_findings(tmp_path) is None

    def test_returns_none_when_no_findings(self, tmp_path):
        from src.autopilot.orchestrator.worktree_integration import _extract_detect_secrets_findings

        sarif_path = (
            tmp_path / ".ash" / "ash_output" / "scanners" / "detect-secrets" / "source" / "results_sarif.sarif"
        )
        sarif_path.parent.mkdir(parents=True)
        sarif_path.write_text(json.dumps({"runs": [{"results": []}]}))

        assert _extract_detect_secrets_findings(tmp_path) is None

    def test_formats_each_finding_using_message_text(self, tmp_path):
        """message.text (real ash v3.7.0/detect-secrets 1.5.0 output,
        captured live) already reads as "<type> detected in file '<path>'
        at line <n>" -- use it directly rather than reassembling the same
        shape from the separate ruleId/location fields."""
        from src.autopilot.orchestrator.worktree_integration import _extract_detect_secrets_findings

        sarif_path = (
            tmp_path / ".ash" / "ash_output" / "scanners" / "detect-secrets" / "source" / "results_sarif.sarif"
        )
        sarif_path.parent.mkdir(parents=True)
        sarif_path.write_text(json.dumps({
            "runs": [{
                "results": [
                    {"ruleId": "SECRET-SECRET-KEYWORD", "message": {
                        "text": "Secret of type 'Secret Keyword' detected in file 'secret.py' at line 1"
                    }},
                    {"ruleId": "SECRET-AWS-ACCESS-KEY", "message": {
                        "text": "Secret of type 'AWS Access Key' detected in file 'secret.py' at line 1"
                    }},
                ]
            }]
        }))

        result = _extract_detect_secrets_findings(tmp_path)

        assert "2 finding(s)" in result
        assert "Secret Keyword' detected in file 'secret.py' at line 1" in result
        assert "AWS Access Key' detected in file 'secret.py' at line 1" in result

    def test_falls_back_to_raw_fields_when_message_text_missing(self, tmp_path):
        """A future ash/detect-secrets version could drop message.text --
        reconstruct the same shape from ruleId/location rather than
        silently losing the finding."""
        from src.autopilot.orchestrator.worktree_integration import _extract_detect_secrets_findings

        sarif_path = (
            tmp_path / ".ash" / "ash_output" / "scanners" / "detect-secrets" / "source" / "results_sarif.sarif"
        )
        sarif_path.parent.mkdir(parents=True)
        sarif_path.write_text(json.dumps({
            "runs": [{
                "results": [
                    {
                        "ruleId": "SECRET-GENERIC",
                        "locations": [{"physicalLocation": {
                            "artifactLocation": {"uri": "src/api.py"},
                            "region": {"startLine": 42},
                        }}],
                    },
                ]
            }]
        }))

        result = _extract_detect_secrets_findings(tmp_path)

        assert "SECRET-GENERIC" in result
        assert "src/api.py" in result
        assert "line 42" in result

    def test_caps_findings_and_notes_the_remainder(self, tmp_path):
        """A summary for an agent's context window, not a full report dump
        -- a large real finding count (the ticket's own example: 472) must
        not blow up ash_results.txt."""
        from src.autopilot.orchestrator.worktree_integration import (
            _MAX_DETECT_SECRETS_FINDINGS,
            _extract_detect_secrets_findings,
        )

        sarif_path = (
            tmp_path / ".ash" / "ash_output" / "scanners" / "detect-secrets" / "source" / "results_sarif.sarif"
        )
        sarif_path.parent.mkdir(parents=True)
        many_results = [
            {"ruleId": "SECRET-GENERIC", "message": {"text": f"finding {i}"}}
            for i in range(_MAX_DETECT_SECRETS_FINDINGS + 5)
        ]
        sarif_path.write_text(json.dumps({"runs": [{"results": many_results}]}))

        result = _extract_detect_secrets_findings(tmp_path)

        assert result.count("- finding ") == _MAX_DETECT_SECRETS_FINDINGS
        assert "and 5 more" in result

