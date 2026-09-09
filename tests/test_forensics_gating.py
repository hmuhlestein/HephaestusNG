"""Tests for the forensics_analysis phase-skip gate in _create_phase_task.

forensics_analysis reviews every artifact + tmux transcript of a completed
feature run to propose prompt/methodology fixes -- expensive, and only
actionable when something actually went wrong. It should be skipped (and
the workflow advanced past it) on a clean run instead of spawning a full
review agent every time.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.database import DatabaseManager, Phase, PhaseExecution, Task, Workflow


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def forensics_workflow(db_manager, tmp_path):
    """A workflow whose current phase is forensics_analysis, working_directory
    pointing at a real tmp dir so _assess_run_health can inspect .hephaestus/tmux/."""
    working_directory = tmp_path / "worktree"
    working_directory.mkdir()

    session = db_manager.get_session()
    wf = Workflow(
        id="wf-forensics",
        name="Test Workflow",
        status="active",
        phases_folder_path="/tmp",
        working_directory=str(working_directory),
    )
    session.add(wf)
    phase = Phase(
        id="phase-forensics",
        workflow_id="wf-forensics",
        name="forensics_analysis",
        order=11,
        description="Analyze pipeline run",
        done_definitions=["forensics.md created"],
    )
    session.add(phase)
    execution = PhaseExecution(
        id="exec-forensics",
        phase_id="phase-forensics",
        workflow_execution_id="wf-forensics",
        status="pending",
    )
    session.add(execution)
    session.commit()
    session.close()
    return working_directory


class TestForensicsAnalysisGating:
    def test_skips_agent_creation_on_clean_run(self, db_manager, forensics_workflow):
        """No tmux error patterns anywhere -> skip the agent, fire the
        transition directly instead of falling through to real task creation."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        tmux_dir = forensics_workflow / ".hephaestus" / "tmux"
        tmux_dir.mkdir(parents=True)
        (tmux_dir / "development_abc12345.transcript.log").write_text(
            "reading files\nwriting calculator.py\nall tests passed\n"
        )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition", return_value=True
        ) as mock_fire:
            result = _create_phase_task(
                "wf-forensics", "phase-forensics", "forensics_analysis",
                "continue", logger,
            )

        assert result is True
        mock_fire.assert_called_once_with(
            "wf-forensics", "phase-forensics", "forensics_analysis", logger
        )

    def test_creates_agent_when_tmux_errors_present(
        self, db_manager, forensics_workflow
    ):
        """A real error pattern in a tmux log -> do NOT skip; fall through to
        the normal task-creation path (asserted here by confirming the skip
        path's _fire_phase_transition is never called)."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        tmux_dir = forensics_workflow / ".hephaestus" / "tmux"
        tmux_dir.mkdir(parents=True)
        (tmux_dir / "development_abc12345.transcript.log").write_text(
            "Traceback (most recent call last):\nModuleNotFoundError: no module named foo\n"
        )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition", return_value=True
        ) as mock_fire:
            _create_phase_task(
                "wf-forensics", "phase-forensics", "forensics_analysis",
                "continue", logger,
            )

        mock_fire.assert_not_called()

    def test_non_forensics_phase_unaffected(self, db_manager, tmp_path):
        """Regression: the gate must be scoped to forensics_analysis only --
        a differently-named phase must never hit the skip path even if its
        workflow's working_directory has no tmux dir at all."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        working_directory = tmp_path / "worktree2"
        working_directory.mkdir()
        session = db_manager.get_session()
        wf = Workflow(
            id="wf-other",
            name="Test Workflow 2",
            status="active",
            phases_folder_path="/tmp",
            working_directory=str(working_directory),
        )
        session.add(wf)
        phase = Phase(
            id="phase-dev",
            workflow_id="wf-other",
            name="development",
            order=4,
            description="Implement",
            done_definitions=["code written"],
        )
        session.add(phase)
        execution = PhaseExecution(
            id="exec-dev",
            phase_id="phase-dev",
            workflow_execution_id="wf-other",
            status="pending",
        )
        session.add(execution)
        session.commit()
        session.close()

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition", return_value=True
        ) as mock_fire:
            _create_phase_task(
                "wf-other", "phase-dev", "development", "continue", logger
            )

        mock_fire.assert_not_called()


class TestAssessRunHealthErrorCounting:
    """Regression: _assess_run_health's tmux error-pattern scan inflated
    error_count by roughly two orders of magnitude on a real run (350-471
    "hits" for a handful of real errors). Two independent, compounding
    causes: (1) an unqualified "*.log" glob matched BOTH
    {session}.transcript.log and {session}.clean.log for the same session,
    counting every hit twice; (2) pipe-pane captures every terminal
    redraw a TUI emits while overwriting the SAME line (spinners,
    streaming tokens) as literal new lines, so a single error message
    still visible on-screen during N redraws counted as N hits."""

    def test_a_redrawn_error_line_counts_once(self, db_manager, forensics_workflow):
        from src.autopilot.orchestrator.queue import _assess_run_health

        tmux_dir = forensics_workflow / ".hephaestus" / "tmux"
        tmux_dir.mkdir(parents=True)
        # Bare \r (cursor to column 0, then overwrite) repainting the SAME
        # error line -- a real terminal treats this as one row updated in
        # place; Python's str.splitlines() treats bare \r as its own line
        # boundary, so a naive scan of the raw bytes sees TWO lines here,
        # both containing the error text.
        raw = b"AssertionError: something failed\r\rAssertionError: something failed\n"
        (tmux_dir / "development_abc12345.transcript.log").write_bytes(raw)

        logger = MagicMock()
        health = _assess_run_health(forensics_workflow, "wf-forensics", None, logger)

        assert health["error_count"] == 1

    def test_two_genuinely_separate_occurrences_both_count(self, db_manager, forensics_workflow):
        """The fix must not swing the other way and start under-counting --
        two real, separately-printed errors are two hits, not one."""
        from src.autopilot.orchestrator.queue import _assess_run_health

        tmux_dir = forensics_workflow / ".hephaestus" / "tmux"
        tmux_dir.mkdir(parents=True)
        (tmux_dir / "development_abc12345.transcript.log").write_text(
            "AssertionError: first failure\nsome other output\nAssertionError: second failure\n"
        )

        logger = MagicMock()
        health = _assess_run_health(forensics_workflow, "wf-forensics", None, logger)

        assert health["error_count"] == 2

    def test_does_not_double_count_transcript_and_clean_log(self, db_manager, forensics_workflow):
        """The old unqualified "*.log" glob matched BOTH
        {session}.transcript.log and {session}.clean.log for the same
        session -- clean.log must never be scanned on its own."""
        from src.autopilot.orchestrator.queue import _assess_run_health

        tmux_dir = forensics_workflow / ".hephaestus" / "tmux"
        tmux_dir.mkdir(parents=True)
        (tmux_dir / "development_abc12345.transcript.log").write_text(
            "AssertionError: something failed\n"
        )
        (tmux_dir / "development_abc12345.clean.log").write_text(
            "AssertionError: something failed\n"
        )

        logger = MagicMock()
        health = _assess_run_health(forensics_workflow, "wf-forensics", None, logger)

        assert health["error_count"] == 1


@pytest.fixture
def forensics_workflow_no_worktree(db_manager):
    """Same as forensics_workflow, but working_directory is never set --
    the common real-world case: by the time forensics_analysis runs, the
    shared worktree is frequently already gone (working_directory cleared
    by _cleanup_worktree, or the directory itself removed). Confirmed
    live: 64 of the 65 forensics_analysis tasks ever created were in
    exactly this state."""
    session = db_manager.get_session()
    wf = Workflow(
        id="wf-forensics-nowt",
        name="Test Workflow No Worktree",
        status="active",
        phases_folder_path="/tmp",
        working_directory=None,
    )
    session.add(wf)
    phase = Phase(
        id="phase-forensics-nowt",
        workflow_id="wf-forensics-nowt",
        name="forensics_analysis",
        order=11,
        description="Analyze pipeline run",
        done_definitions=["forensics.md created"],
    )
    session.add(phase)
    execution = PhaseExecution(
        id="exec-forensics-nowt",
        phase_id="phase-forensics-nowt",
        workflow_execution_id="wf-forensics-nowt",
        status="pending",
    )
    session.add(execution)
    session.commit()
    session.close()
    return db_manager


class TestForensicsAnalysisGatingWithoutWorktree:
    """Regression for the bug behind "forensics always runs": the skip-gate
    used to be conditioned on Workflow.working_directory pointing at a
    still-existing directory -- when it didn't (the overwhelming majority
    of real runs), _assess_run_health was never even called, so forensics
    spawned unconditionally regardless of whether anything went wrong.
    The DB-based checks below must work with no worktree at all."""

    def test_no_worktree_and_no_task_problems_still_skips(
        self, db_manager, forensics_workflow_no_worktree
    ):
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition", return_value=True
        ) as mock_fire:
            result = _create_phase_task(
                "wf-forensics-nowt", "phase-forensics-nowt", "forensics_analysis",
                "continue", logger,
            )

        assert result is True
        mock_fire.assert_called_once_with(
            "wf-forensics-nowt", "phase-forensics-nowt", "forensics_analysis", logger
        )

    def test_no_worktree_but_a_failed_task_creates_agent(
        self, db_manager, forensics_workflow_no_worktree
    ):
        """A task that failed (regardless of whether it was later retried
        to success) is real evidence something went wrong -- must not be
        masked just because the worktree used to inspect tmux logs is gone."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        session = db_manager.get_session()
        session.add(
            Task(
                id="task-failed-1",
                workflow_id="wf-forensics-nowt",
                raw_description="r",
                done_definition="d",
                status="failed",
                failure_reason="Orphaned: agent terminated unexpectedly",
            )
        )
        session.commit()
        session.close()

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition", return_value=True
        ) as mock_fire:
            _create_phase_task(
                "wf-forensics-nowt", "phase-forensics-nowt", "forensics_analysis",
                "continue", logger,
            )

        mock_fire.assert_not_called()

    def test_no_worktree_but_a_retried_task_creates_agent(
        self, db_manager, forensics_workflow_no_worktree
    ):
        """A task that needed a retry (retry_count > 0) counts even if its
        CURRENT status is "done" -- retry reuses the same row, so the
        historical failure only survives via retry_count/failure_reason,
        not the current status."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        session = db_manager.get_session()
        session.add(
            Task(
                id="task-retried-1",
                workflow_id="wf-forensics-nowt",
                raw_description="r",
                done_definition="d",
                status="done",
                retry_count=1,
            )
        )
        session.commit()
        session.close()

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition", return_value=True
        ) as mock_fire:
            _create_phase_task(
                "wf-forensics-nowt", "phase-forensics-nowt", "forensics_analysis",
                "continue", logger,
            )

        mock_fire.assert_not_called()

    def test_no_worktree_but_an_arbitration_task_creates_agent(
        self, db_manager, forensics_workflow_no_worktree
    ):
        """An arbitration escalation means a phase exhausted its retry/goto
        budget -- a genuine problem, distinct from normal iteration."""
        from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        session = db_manager.get_session()
        session.add(
            Task(
                id="task-arb-1",
                workflow_id="wf-forensics-nowt",
                raw_description="Arbitrate stuck phase: development",
                done_definition="d",
                status="done",
                created_by_agent_id=ARBITRATION_CREATED_BY,
            )
        )
        session.commit()
        session.close()

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition", return_value=True
        ) as mock_fire:
            _create_phase_task(
                "wf-forensics-nowt", "phase-forensics-nowt", "forensics_analysis",
                "continue", logger,
            )

        mock_fire.assert_not_called()
