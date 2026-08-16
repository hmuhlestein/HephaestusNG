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

    def test_skips_gracefully_when_ash_script_missing(self, tmp_path):
        """If scripts/ash doesn't exist at the derived repo path, don't crash
        and don't write a misleading results file."""
        from src.autopilot.orchestrator.worktree_integration import _run_ash_scan

        logger = MagicMock()
        with patch("pathlib.Path.exists", return_value=False):
            _run_ash_scan(tmp_path, logger)

        results_path = tmp_path / CONTEXT_DIR_NAME / "ash_results.txt"
        assert not results_path.exists()


class TestAshScanWiredIntoPhaseTaskCreation:
    def test_security_review_triggers_scan(self, tmp_path, monkeypatch):
        from src.core.database import DatabaseManager, Phase, PhaseExecution, Workflow

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
        db = DatabaseManager(str(db_path))
        db.create_tables()

        working_directory = tmp_path / "worktree"
        working_directory.mkdir()

        session = db.get_session()
        session.add(
            Workflow(
                id="wf-sec",
                name="Test",
                status="active",
                phases_folder_path="/tmp",
                working_directory=str(working_directory),
            )
        )
        session.add(
            Phase(
                id="phase-sec",
                workflow_id="wf-sec",
                name="security_review",
                order=8,
                description="Security review",
                done_definitions=["security.md created"],
            )
        )
        session.add(
            PhaseExecution(
                id="exec-sec",
                phase_id="phase-sec",
                workflow_execution_id="wf-sec",
                status="pending",
            )
        )
        session.commit()
        session.close()

        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions._run_ash_scan") as mock_scan:
            _create_phase_task("wf-sec", "phase-sec", "security_review", "continue", logger)

        mock_scan.assert_called_once_with(working_directory, logger)

    def test_other_phases_do_not_trigger_scan(self, tmp_path, monkeypatch):
        from src.core.database import DatabaseManager, Phase, PhaseExecution, Workflow

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
        db = DatabaseManager(str(db_path))
        db.create_tables()

        working_directory = tmp_path / "worktree"
        working_directory.mkdir()

        session = db.get_session()
        session.add(
            Workflow(
                id="wf-dev",
                name="Test",
                status="active",
                phases_folder_path="/tmp",
                working_directory=str(working_directory),
            )
        )
        session.add(
            Phase(
                id="phase-dev",
                workflow_id="wf-dev",
                name="development",
                order=4,
                description="Implement",
                done_definitions=["code written"],
            )
        )
        session.add(
            PhaseExecution(
                id="exec-dev",
                phase_id="phase-dev",
                workflow_execution_id="wf-dev",
                status="pending",
            )
        )
        session.commit()
        session.close()

        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions._run_ash_scan") as mock_scan:
            _create_phase_task("wf-dev", "phase-dev", "development", "continue", logger)

        mock_scan.assert_not_called()
