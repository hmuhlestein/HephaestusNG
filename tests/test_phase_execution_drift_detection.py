"""Step 1 of docs/designs/PHASE_EXECUTION_STATE_MACHINE_REFACTOR.md:
detection-only invariant checks for PhaseExecution/real-state drift. No
write path -- these only log. See that document for the three concrete
incidents (workflow 72ed4df8) this is meant to surface immediately on any
future recurrence instead of requiring a live incident to notice.
"""

from unittest.mock import MagicMock

import pytest

from src.autopilot.orchestrator import phase_transitions as pt
from src.core.database import DatabaseManager, Phase, PhaseExecution, Task, Workflow


@pytest.fixture(autouse=True)
def _reset_debounce_state():
    """_drift_previously_seen is module-level, in-memory state -- clear it
    before and after every test so tests don't leak debounce state into
    each other (test order/parallelism would otherwise make this flaky)."""
    pt._drift_previously_seen.clear()
    yield
    pt._drift_previously_seen.clear()


@pytest.fixture
def db_manager(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    db.create_tables()
    return db


def _seed(db_manager, execution_status: str, task_status: str, workflow_status: str = "active"):
    with db_manager.session_scope() as session:
        session.add(Workflow(id="wf-1", name="w", phases_folder_path="/tmp", status=workflow_status))
        session.add(Phase(id="phase-1", workflow_id="wf-1", order=1, name="development", description="d", done_definitions=["x"]))
        session.add(PhaseExecution(id="exec-1", phase_id="phase-1", status=execution_status))
        session.add(Task(id="task-1", workflow_id="wf-1", phase_id="phase-1", raw_description="r", done_definition="d", status=task_status))


class TestFindPhaseExecutionDrift:
    def test_finds_a_live_task_whose_execution_is_not_in_progress(self, db_manager):
        _seed(db_manager, execution_status="failed", task_status="pending")
        with db_manager.session_scope() as session:
            results = pt.find_phase_execution_drift(session, "wf-1")
            assert len(results) == 1
            phase, execution, task = results[0]
            assert phase.id == "phase-1"
            assert execution.status == "failed"
            assert task.id == "task-1"

    def test_no_drift_when_execution_is_in_progress(self, db_manager):
        _seed(db_manager, execution_status="in_progress", task_status="in_progress")
        with db_manager.session_scope() as session:
            assert pt.find_phase_execution_drift(session, "wf-1") == []

    def test_a_done_task_is_not_drift_here_even_if_execution_mismatched(self, db_manager):
        """A "done" task's phase execution being stuck non-in_progress is
        find_stuck_active_workflows's territory (or another self-heal's),
        not this one -- "done" is not a live status."""
        _seed(db_manager, execution_status="failed", task_status="done")
        with db_manager.session_scope() as session:
            assert pt.find_phase_execution_drift(session, "wf-1") == []

    def test_pending_task_counts_as_live(self, db_manager):
        """A task that exists but hasn't been picked up yet is still real
        work under this phase (see _release_pending_phases_with_orphaned_task's
        own "never dispatched, stale >1min" case) -- must not be blind to it."""
        _seed(db_manager, execution_status="pending", task_status="pending")
        with db_manager.session_scope() as session:
            results = pt.find_phase_execution_drift(session, "wf-1")
            assert len(results) == 1


class TestFindStuckActiveWorkflows:
    def test_finds_an_active_workflow_with_a_failed_execution(self, db_manager):
        _seed(db_manager, execution_status="failed", task_status="done", workflow_status="active")
        with db_manager.session_scope() as session:
            results = pt.find_stuck_active_workflows(session)
            assert len(results) == 1
            workflow, phase, execution = results[0]
            assert workflow.id == "wf-1"
            assert execution.status == "failed"

    def test_no_match_when_workflow_is_not_active(self, db_manager):
        _seed(db_manager, execution_status="failed", task_status="done", workflow_status="paused")
        with db_manager.session_scope() as session:
            assert pt.find_stuck_active_workflows(session) == []

    def test_no_match_when_execution_is_not_failed(self, db_manager):
        _seed(db_manager, execution_status="pending", task_status="pending", workflow_status="active")
        with db_manager.session_scope() as session:
            assert pt.find_stuck_active_workflows(session) == []


class TestCheckAndLogPhaseExecutionDriftDebounce:
    def test_does_not_log_on_first_sighting(self, db_manager):
        _seed(db_manager, execution_status="failed", task_status="pending")
        logger = MagicMock()
        with db_manager.session_scope() as session:
            pt.check_and_log_phase_execution_drift(session, "wf-1", logger)
        logger.warning.assert_not_called()

    def test_logs_when_the_same_drift_is_still_present_on_a_second_check(self, db_manager):
        _seed(db_manager, execution_status="failed", task_status="pending")
        logger = MagicMock()
        with db_manager.session_scope() as session:
            pt.check_and_log_phase_execution_drift(session, "wf-1", logger)
        logger.warning.assert_not_called()

        with db_manager.session_scope() as session:
            pt.check_and_log_phase_execution_drift(session, "wf-1", logger)
        logger.warning.assert_called_once()

    def test_resolved_drift_is_not_logged_and_resets_debounce(self, db_manager):
        """If the mismatch disappears between checks (e.g. the phase
        genuinely reopened), it must not be logged, and a LATER,
        unrelated recurrence must need its own two-in-a-row confirmation
        again -- not piggyback on the earlier, now-irrelevant sighting."""
        _seed(db_manager, execution_status="failed", task_status="pending")
        logger = MagicMock()
        with db_manager.session_scope() as session:
            pt.check_and_log_phase_execution_drift(session, "wf-1", logger)

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "in_progress"

        with db_manager.session_scope() as session:
            pt.check_and_log_phase_execution_drift(session, "wf-1", logger)
        logger.warning.assert_not_called()

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "failed"

        with db_manager.session_scope() as session:
            pt.check_and_log_phase_execution_drift(session, "wf-1", logger)
        logger.warning.assert_not_called()  # first sighting of the NEW recurrence


class TestCheckAndLogPhaseExecutionDriftMultiWorkflow:
    """Regression: the sweep calls check_and_log_phase_execution_drift once
    per active/paused workflow per tick (background_loops.py), not once
    per tick overall. A single shared debounce set previously had every
    workflow's call clear() whatever the PREVIOUS workflow in that same
    tick's iteration had just recorded, so a workflow's own persistent
    drift was compared against some OTHER workflow's keys on the next
    tick and could never reach "second sighting" -- with >1 monitored
    workflow (the normal case), real drift silently never got logged."""

    def test_two_workflows_each_reach_second_sighting_independently(self, db_manager):
        with db_manager.session_scope() as session:
            session.add(Workflow(id="wf-a", name="a", phases_folder_path="/tmp", status="active"))
            session.add(Phase(id="phase-a", workflow_id="wf-a", order=1, name="development", description="d", done_definitions=["x"]))
            session.add(PhaseExecution(id="exec-a", phase_id="phase-a", status="failed"))
            session.add(Task(id="task-a", workflow_id="wf-a", phase_id="phase-a", raw_description="r", done_definition="d", status="pending"))

            session.add(Workflow(id="wf-b", name="b", phases_folder_path="/tmp", status="active"))
            session.add(Phase(id="phase-b", workflow_id="wf-b", order=1, name="development", description="d", done_definitions=["x"]))
            session.add(PhaseExecution(id="exec-b", phase_id="phase-b", status="pending"))
            # wf-b has no live task -- no drift for it.

        logger = MagicMock()

        with db_manager.session_scope() as session:
            pt.check_and_log_phase_execution_drift(session, "wf-a", logger)
            pt.check_and_log_phase_execution_drift(session, "wf-b", logger)
        logger.warning.assert_not_called()  # wf-a's first sighting

        with db_manager.session_scope() as session:
            pt.check_and_log_phase_execution_drift(session, "wf-a", logger)
            pt.check_and_log_phase_execution_drift(session, "wf-b", logger)
        logger.warning.assert_called_once()  # wf-a's second sighting -- must fire


class TestCheckAndLogStuckActiveWorkflows:
    def test_logs_immediately_no_debounce_needed(self, db_manager):
        _seed(db_manager, execution_status="failed", task_status="done", workflow_status="active")
        logger = MagicMock()
        with db_manager.session_scope() as session:
            pt.check_and_log_stuck_active_workflows(session, logger)
        logger.warning.assert_called_once()

    def test_does_not_raise_when_nothing_is_stuck(self, db_manager):
        _seed(db_manager, execution_status="in_progress", task_status="in_progress")
        logger = MagicMock()
        with db_manager.session_scope() as session:
            pt.check_and_log_stuck_active_workflows(session, logger)
        logger.warning.assert_not_called()
