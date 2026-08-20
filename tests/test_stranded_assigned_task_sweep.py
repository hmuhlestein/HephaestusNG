"""_clean_stale_assigned_tasks must reclaim tasks stranded "assigned" with
no agent (src/autopilot/orchestrator/features.py).

process_queue and bump_task_priority_endpoint both dequeue ("queued" ->
"assigned") before the dispatch that can fail. Both now requeue on failure,
but a process death in that window runs no handler at all, leaving the one
task state nothing else can reclaim: get_next_queued_task reads only
"queued", this function's own terminated-agent pass requires
assigned_agent_id isnot(None), and every mechanical_recovery detector looks
its task up by agent. "pending" has coverage (phase_transitions retries it
when unassigned); "assigned" had none.
"""

import uuid
from datetime import datetime, timedelta

import pytest

from src.core.database import DatabaseManager, Task, Workflow

GRACE = 900  # monitoring.stranded_task_grace_seconds default


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    with manager.session_scope() as session:
        session.add(
            Workflow(id="wf-1", name="wf-1", status="active", phases_folder_path="/tmp")
        )
    return manager


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, msg):
        self.messages.append(msg)


def _seed(db, **overrides):
    task_id = str(uuid.uuid4())
    fields = dict(
        id=task_id,
        workflow_id="wf-1",
        raw_description="r",
        done_definition="d",
        status="assigned",
        priority="medium",
        assigned_agent_id=None,
        started_at=None,
        queued_at=datetime.utcnow() - timedelta(seconds=GRACE + 60),
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


class TestStrandedAssignedTaskSweep:
    def test_task_stranded_past_grace_is_requeued(self, db):
        task_id = _seed(db)

        _sweep()

        assert _status(db, task_id) == "queued", (
            "stranded task not reclaimed -- nothing else in the system can "
            "see an 'assigned' task with no agent"
        )

    def test_task_within_grace_is_left_alone(self, db):
        """The grace period is what keeps this sweep off a dispatch that is
        still in flight -- enrichment plus agent launch is legitimately slow,
        and requeueing mid-dispatch double-dispatches the task."""
        task_id = _seed(db, queued_at=datetime.utcnow() - timedelta(seconds=30))

        _sweep()

        assert _status(db, task_id) == "assigned"

    def test_task_with_started_at_is_left_alone(self, db):
        """started_at is set only once an agent really picked the task up."""
        task_id = _seed(db, started_at=datetime.utcnow() - timedelta(hours=3))

        _sweep()

        assert _status(db, task_id) == "assigned"

    def test_task_with_a_live_agent_is_left_alone(self, db):
        """Case 1 of this function owns agent-assigned tasks; this pass must
        not poach one just because it is old."""
        task_id = _seed(db, assigned_agent_id="agent-1")

        _sweep()

        assert _status(db, task_id) == "assigned"

    def test_other_workflows_are_not_touched(self, db):
        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-2", name="wf-2", status="active", phases_folder_path="/tmp"
                )
            )
        task_id = _seed(db, workflow_id="wf-2")

        _sweep()

        assert _status(db, task_id) == "assigned"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
