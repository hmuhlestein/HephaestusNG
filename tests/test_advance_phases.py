"""Tests for orchestrator._advance_phases and related phase transition functions.

These tests address the critical test coverage gap identified in ARCHITECTURE_REVIEW.md:
"_advance_phases has no test referencing it anywhere in tests/"
"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.database import (
    DatabaseManager,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
)


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    """Create a test database manager.

    _advance_phases and friends open their own session via the module-level
    get_db(), which reads HEPHAESTUS_TEST_DB — point it at the same sqlite
    file this fixture's DatabaseManager uses, or those functions would
    silently read/write the default hephaestus.db instead of test data.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def sample_workflow(db_manager):
    """Create a sample workflow with phases."""
    with db_manager.session_scope() as session:
        # Create workflow
        wf = Workflow(
            id="wf-1",
            name="Test Workflow",
            status="active",
            phases_folder_path="/tmp",
        )
        session.add(wf)
        
        # Create phases
        phase1 = Phase(
            id="phase-1",
            workflow_id="wf-1",
            name="requirements",
            order=1,
            description="Gather requirements",
            done_definitions=["requirements documented"],
        )
        phase2 = Phase(
            id="phase-2",
            workflow_id="wf-1",
            name="implementation",
            order=2,
            description="Implement the feature",
            done_definitions=["code written and tested"],
        )
        session.add(phase1)
        session.add(phase2)
        
        # Create phase executions
        exec1 = PhaseExecution(
            id="exec-1",
            phase_id="phase-1",
            workflow_execution_id="wf-1",
            status="in_progress",
        )
        session.add(exec1)
        
        return wf


class TestClaimPhaseTaskCreation:
    """Tests for _claim_phase_task_creation, the atomic guard that stops two
    independent code paths (server.py's synchronous initial-task creation
    and the orchestrator's background self-heal) from both creating a
    phase's first task."""

    def test_first_caller_wins(self, db_manager, sample_workflow):
        from src.autopilot.orchestrator import _claim_phase_task_creation

        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-1") is True

    def test_second_caller_loses(self, db_manager, sample_workflow):
        """The core guarantee: only one of two callers racing for the same
        phase can ever win the claim, regardless of ordering."""
        from src.autopilot.orchestrator import _claim_phase_task_creation

        with db_manager.session_scope() as session:
            first = _claim_phase_task_creation(session, "phase-1")
        with db_manager.session_scope() as session:
            second = _claim_phase_task_creation(session, "phase-1")

        assert first is True
        assert second is False

    def test_different_phases_both_win(self, db_manager, sample_workflow):
        """Claims are scoped per phase -- unrelated phases don't block each other."""
        from src.autopilot.orchestrator import _claim_phase_task_creation

        with db_manager.session_scope() as session:
            session.add(
                PhaseExecution(
                    id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1",
                    status="pending",
                )
            )

        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-1") is True
        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-2") is True


class TestAdvancePhases:
    """Tests for _advance_phases function."""
    
    def test_returns_false_when_workflow_not_found(self, db_manager):
        """Should return False when workflow doesn't exist."""
        from src.autopilot.orchestrator import _advance_phases
        
        logger = MagicMock()
        result = _advance_phases("nonexistent-wf", logger)
        assert result is False
    
    def test_returns_false_when_workflow_paused(self, db_manager, sample_workflow):
        """Should return False when workflow is paused (no done tasks)."""
        from src.autopilot.orchestrator import _advance_phases
        
        # Pause the workflow
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
        
        logger = MagicMock()
        result = _advance_phases("wf-1", logger)
        assert result is False
    
    def test_auto_resumes_paused_workflow_with_done_task(self, db_manager, sample_workflow):
        """Should auto-resume paused workflow if it has a done task in stalled phase."""
        from src.autopilot.orchestrator import _advance_phases
        
        # Pause the workflow and add a done task
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
            
            task = Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="Test task",
                done_definition="Done when complete",
                status="done",
            )
            session.add(task)
        
        logger = MagicMock()
        # This should auto-resume and then try to advance
        # (may return True or False depending on phase state)
        _advance_phases("wf-1", logger)
        
        # Verify workflow was resumed
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"


class TestCaseStartFirstPhase:
    """Tests for _case_start_first_phase function."""
    
    def test_starts_first_phase_when_no_progress(self, db_manager, sample_workflow):
        """Should start first phase when no phases are in progress or completed."""
        from src.autopilot.orchestrator import (
            _case_start_first_phase,
            _get_phase_statuses,
        )
        
        with db_manager.session_scope() as session:
            # Reset phase execution to pending
            exec = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            if exec:
                exec.status = "pending"
            
            phase_statuses = _get_phase_statuses(session, "wf-1")
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            completed = [p for p in phase_statuses if p["status"] == "completed"]
        
        logger = MagicMock()
        with patch("src.autopilot.orchestrator._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_start_first_phase(session, "wf-1", pending, in_progress, completed, logger)
                assert result is True
                mock_create.assert_called_once()


    def test_race_guard_skips_duplicate_when_other_path_wins_the_claim(
        self, db_manager, sample_workflow
    ):
        """Regression: /start_workflow_execution creates phase 1's initial
        task synchronously, but _advance_phases's polling loop can also
        decide to create phase 1's task independently, racing it. A plain
        Task.count()==0 check (even with a sleep-and-retry) isn't safe --
        both sides can observe zero tasks. Observed live: a duplicate
        task+agent got spawned for the same phase, burning a full agent run
        duplicating work the first task had already completed.

        _case_start_first_phase now closes this by construction: it must
        win an atomic claim (PhaseExecution.task_creation_claimed_at) before
        creating a task. Simulates the other code path winning that claim
        first -- _case_start_first_phase must see the lost claim and skip.
        """
        from src.autopilot.orchestrator import (
            _case_start_first_phase,
            _claim_phase_task_creation,
            _get_phase_statuses,
        )

        with db_manager.session_scope() as session:
            exec = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            if exec:
                exec.status = "pending"

            phase_statuses = _get_phase_statuses(session, "wf-1")
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            completed = [p for p in phase_statuses if p["status"] == "completed"]

        # Simulate /start_workflow_execution's synchronous path winning the
        # claim first (it hasn't created the Task row yet -- e.g. still
        # queued behind agent creation -- which is exactly the live scenario
        # this regression came from).
        with db_manager.session_scope() as session:
            won = _claim_phase_task_creation(session, "phase-1")
            assert won is True

        logger = MagicMock()
        with patch("src.autopilot.orchestrator._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_start_first_phase(
                    session, "wf-1", pending, in_progress, completed, logger
                )
                assert result is None, (
                    "should not create a duplicate task once the other "
                    "path has already won the task-creation claim"
                )
                mock_create.assert_not_called()


class TestCaseInProgressNoTasks:
    """Tests for _case_in_progress_no_tasks function."""
    
    def test_creates_task_for_phase_without_tasks(self, db_manager, sample_workflow):
        """Should create task when in-progress phase has no tasks."""
        from src.autopilot.orchestrator import _case_in_progress_no_tasks
        
        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            in_progress = [{"phase": phase, "status": "in_progress"}]
        
        logger = MagicMock()
        with patch("src.autopilot.orchestrator._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_in_progress_no_tasks(session, "wf-1", in_progress, logger)
                assert result is True
                mock_create.assert_called_once()


class TestMaybeRetryFailedTasks:
    """Tests for _maybe_retry_failed_tasks function."""
    
    def test_retries_all_failed_tasks(self, db_manager, sample_workflow):
        """Should reset all failed tasks to pending when all tasks failed."""
        from src.autopilot.orchestrator import _maybe_retry_failed_tasks
        
        with db_manager.session_scope() as session:
            # Add failed tasks
            for i in range(3):
                task = Task(
                    id=f"task-fail-{i}",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description=f"Failed task {i}",
                    done_definition="Done",
                    status="failed",
                    failure_reason="Error",
                )
                session.add(task)
            
            phase = session.query(Phase).filter_by(id="phase-1").first()
        
        logger = MagicMock()
        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            result = _maybe_retry_failed_tasks(session, phase, logger)
            assert result is True
            
            # Verify tasks were reset
            tasks = session.query(Task).filter_by(phase_id="phase-1", status="pending").all()
            assert len(tasks) == 3


class TestGetPhaseStatuses:
    """Tests for _get_phase_statuses helper."""
    
    def test_returns_phase_statuses(self, db_manager, sample_workflow):
        """Should return all phases with their execution statuses."""
        from src.autopilot.orchestrator import _get_phase_statuses
        
        with db_manager.session_scope() as session:
            statuses = _get_phase_statuses(session, "wf-1")
            
            assert len(statuses) == 2
            assert statuses[0]["phase"].name == "requirements"
            assert statuses[0]["status"] == "in_progress"
            assert statuses[1]["phase"].name == "implementation"
            assert statuses[1]["status"] == "pending"


class TestFirePhaseTransition:
    """Tests for _fire_phase_transition function."""
    
    @patch("src.autopilot.orchestrator.PhaseManager")
    @patch("src.autopilot.orchestrator._create_phase_task")
    def test_fires_transition_successfully(self, mock_create, mock_pm_class, db_manager, sample_workflow):
        """Should fire phase transition and create next task."""
        from src.autopilot.orchestrator import _fire_phase_transition
        
        # Mock phase manager
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase_id": "phase-2",
            "target_phase": "implementation",
        }
        mock_create.return_value = True
        
        logger = MagicMock()
        result = _fire_phase_transition("wf-1", "phase-1", "requirements", logger)
        
        assert result is True
        mock_pm.mark_phase_complete.assert_called_once()
        mock_create.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
