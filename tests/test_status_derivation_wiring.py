"""Characterization tests for status-derivation wiring (Phase 2 §4.6).

These tests verify the specific gap the wiring closes: hand-rolled
"all tasks done" checks without phase-completeness gates. The "all tasks
done ≠ all phases done" mistake has recurred independently at least four
times in this codebase's history.
"""

import pytest

from src.core.database import (
    DatabaseManager,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
)


@pytest.fixture
def db_manager(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    db.create_tables()
    return db


class TestDeriveWorkflowStatusPhaseCompleteness:
    """derive_workflow_status must NOT return "completed" when phases are
    incomplete, even if all tasks are done. This is the exact gap the
    hand-rolled checks at _workflow_appears_abandoned, is_design_fully_complete,
    and the approve handler all missed."""

    def test_all_tasks_done_but_phases_incomplete_is_not_completed(self, db_manager):
        """A workflow with all tasks done but incomplete phases should NOT
        derive "completed". This is the historical bug pattern: "all tasks
        done" was used as a proxy for "workflow done" without checking
        whether all phases had actually been dispatched and completed."""
        with db_manager.session_scope() as session:
            wf = Workflow(
                id="wf-1", name="test", status="active",
                phases_folder_path="/tmp",
            )
            session.add(wf)
            # Phase 1: completed, task done
            phase1 = Phase(
                id="p1", workflow_id="wf-1", name="development", order=1,
                description="dev phase", done_definitions=["done"],
            )
            session.add(phase1)
            exec1 = PhaseExecution(
                id="e1", phase_id="p1", workflow_execution_id="wf-1",
                status="completed",
            )
            session.add(exec1)
            task1 = Task(
                id="t1", workflow_id="wf-1", phase_id="p1",
                raw_description="dev task", done_definition="done",
                status="done",
            )
            session.add(task1)
            # Phase 2: in_progress, no tasks (not yet dispatched)
            phase2 = Phase(
                id="p2", workflow_id="wf-1", name="testing", order=2,
                description="test phase", done_definitions=["done"],
            )
            session.add(phase2)
            exec2 = PhaseExecution(
                id="e2", phase_id="p2", workflow_execution_id="wf-1",
                status="in_progress",
            )
            session.add(exec2)

        from src.core.status_derivation import derive_workflow_status
        with db_manager.session_scope() as session:
            derived = derive_workflow_status(session, "wf-1", write_back=False)

        # This is the key assertion: derive_workflow_status must NOT
        # return "completed" when phases are incomplete.
        assert derived != "completed"
        assert derived == "active"

    def test_all_tasks_and_phases_done_is_completed(self, db_manager):
        """When all tasks are done AND all phases are completed,
        derive_workflow_status should return "completed"."""
        with db_manager.session_scope() as session:
            wf = Workflow(
                id="wf-1", name="test", status="active",
                phases_folder_path="/tmp",
            )
            session.add(wf)
            phase1 = Phase(
                id="p1", workflow_id="wf-1", name="dev", order=1,
                description="dev phase", done_definitions=["done"],
            )
            session.add(phase1)
            exec1 = PhaseExecution(
                id="e1", phase_id="p1", workflow_execution_id="wf-1",
                status="completed",
            )
            session.add(exec1)
            task1 = Task(
                id="t1", workflow_id="wf-1", phase_id="p1",
                raw_description="dev task", done_definition="done",
                status="done",
            )
            session.add(task1)

        from src.core.status_derivation import derive_workflow_status
        with db_manager.session_scope() as session:
            derived = derive_workflow_status(session, "wf-1", write_back=False)

        assert derived == "completed"

    def test_no_phases_falls_through_to_task_heuristic(self, db_manager):
        """A workflow with no Phase rows should fall through to the
        task-status heuristic (existing behavior, not changed by wiring)."""
        with db_manager.session_scope() as session:
            wf = Workflow(
                id="wf-1", name="test", status="active",
                phases_folder_path="/tmp",
            )
            session.add(wf)
            task1 = Task(
                id="t1", workflow_id="wf-1",
                raw_description="task", done_definition="done",
                status="done",
            )
            session.add(task1)

        from src.core.status_derivation import derive_workflow_status
        with db_manager.session_scope() as session:
            derived = derive_workflow_status(session, "wf-1", write_back=False)

        assert derived == "completed"
