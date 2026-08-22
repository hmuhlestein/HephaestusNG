"""_clean_stale_assigned_tasks must reclaim a "pending" task with no agent
whose own phase already completed via a sibling task (src/autopilot/
orchestrator/features.py, case 4).

A duplicate-creation race can leave a second task row for a phase that was
never dispatched, while a sibling task does the real work and the phase
completes normally. Case 2 of this same function already reclaims pending/
assigned tasks once the WHOLE WORKFLOW is "completed", but a workflow
sitting "paused" for human review (which can take arbitrarily long) never
reaches that -- even though an individual phase inside it finished hours
ago. Left "pending" forever, this also silently hides the frontend's
"Review" button (DesignQueuePanel.tsx's readyForGitPushReview requires
every non-git_expert task to be done/failed/duplicated). Observed live:
task 36a04e0e (product_requirements) sat pending for 10+ hours after its
sibling completed the phase, on a workflow paused for review.
"""

import uuid
from datetime import datetime, timedelta

import pytest

from src.core.database import DatabaseManager, Phase, PhaseExecution, Task, Workflow


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    with manager.session_scope() as session:
        session.add(
            Workflow(id="wf-1", name="wf-1", status="paused", phases_folder_path="/tmp")
        )
        session.add(Phase(
            id="phase-1", workflow_id="wf-1", name="product_requirements",
            order=1, description="d", done_definitions=["x"],
        ))
    return manager


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, msg):
        self.messages.append(msg)


def _seed_execution(db, status="completed"):
    with db.session_scope() as session:
        session.add(PhaseExecution(
            id="exec-1", phase_id="phase-1", workflow_execution_id="wf-1",
            status=status,
        ))


def _seed_task(db, **overrides):
    task_id = str(uuid.uuid4())
    fields = dict(
        id=task_id,
        workflow_id="wf-1",
        phase_id="phase-1",
        raw_description="r",
        done_definition="d",
        status="pending",
        priority="medium",
        assigned_agent_id=None,
        created_at=datetime.utcnow() - timedelta(hours=10),
    )
    fields.update(overrides)
    with db.session_scope() as session:
        session.add(Task(**fields))
    return task_id


def _status(db, task_id):
    with db.session_scope() as session:
        return session.query(Task).filter_by(id=task_id).first().status


def _sweep():
    from src.autopilot.orchestrator.features import _clean_stale_assigned_tasks

    _clean_stale_assigned_tasks("wf-1", _Logger())


class TestOrphanedPendingTaskInCompletedPhaseSweep:
    def test_orphaned_pending_task_in_completed_phase_is_marked_duplicated(self, db):
        _seed_execution(db, status="completed")
        task_id = _seed_task(db)

        _sweep()

        assert _status(db, task_id) == "duplicated", (
            "orphaned pending task in an already-completed phase not "
            "reclaimed -- nothing else re-evaluates a phase that finished "
            "hours ago, so this task would sit pending forever and hide "
            "the frontend's Review button"
        )

    def test_task_in_still_in_progress_phase_is_left_alone(self, db):
        """A phase currently doing real work (goto/retry re-entry flips
        PhaseExecution back to in_progress first) must not be touched."""
        _seed_execution(db, status="in_progress")
        task_id = _seed_task(db)

        _sweep()

        assert _status(db, task_id) == "pending"

    def test_task_with_an_assigned_agent_is_left_alone(self, db):
        """Case 1 of this function owns agent-assigned tasks."""
        _seed_execution(db, status="completed")
        task_id = _seed_task(db, assigned_agent_id="agent-1")

        _sweep()

        assert _status(db, task_id) == "pending"

    def test_freshly_created_task_within_grace_is_left_alone(self, db):
        """A task created moments ago, before its own commit's
        PhaseExecution flip could plausibly be stale, is not touched --
        matches this sweep's other checks' age-guard convention."""
        _seed_execution(db, status="completed")
        task_id = _seed_task(db, created_at=datetime.utcnow())

        _sweep()

        assert _status(db, task_id) == "pending"

    def test_non_pending_task_is_left_alone(self, db):
        """Only "pending" is in scope here -- an in_progress/assigned task
        with a live or dead agent is case 1's job, not this one's."""
        _seed_execution(db, status="completed")
        task_id = _seed_task(db, status="in_progress", assigned_agent_id="agent-1")

        _sweep()

        assert _status(db, task_id) == "in_progress"

    def test_other_workflows_are_not_touched(self, db):
        with db.session_scope() as session:
            session.add(
                Workflow(id="wf-2", name="wf-2", status="paused", phases_folder_path="/tmp")
            )
            session.add(Phase(
                id="phase-2", workflow_id="wf-2", name="product_requirements",
                order=1, description="d", done_definitions=["x"],
            ))
            session.add(PhaseExecution(
                id="exec-2", phase_id="phase-2", workflow_execution_id="wf-2",
                status="completed",
            ))
        task_id = _seed_task(
            db, workflow_id="wf-2", phase_id="phase-2",
        )

        _sweep()

        assert _status(db, task_id) == "pending"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
