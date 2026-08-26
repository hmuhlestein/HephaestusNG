"""_clean_stale_assigned_tasks' second block (src/autopilot/orchestrator/
features.py, "Pending/assigned tasks in already-completed workflows") must
force-fail every task still doing real work once its workflow is marked
"completed" -- not just "pending"/"assigned".

Regression, des-c7b9 tech-debt pass: this block's filter was
["pending", "assigned"], omitting "in_progress", "queued", and "blocked"
entirely -- same bug class as the already-audited first block a few lines
above it (line 536, which does include the full 5-status set). A task still
in_progress/queued/blocked when its workflow gets marked "completed" was
never caught by this sweep -- sits attached to a dead workflow indefinitely,
invisible to any active-workflow sweep.
"""

import uuid
from datetime import datetime

import pytest

from src.core.database import DatabaseManager, Task, Workflow


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    with manager.session_scope() as session:
        session.add(Workflow(id="wf-1", name="wf-1", status="completed", phases_folder_path="/tmp"))
    return manager


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, msg):
        self.messages.append(msg)


def _seed_task(db, status):
    task_id = str(uuid.uuid4())
    with db.session_scope() as session:
        session.add(
            Task(
                id=task_id,
                workflow_id="wf-1",
                raw_description="r",
                done_definition="d",
                status=status,
                priority="medium",
                assigned_agent_id=None,
                created_at=datetime.utcnow(),
            )
        )
    return task_id


def _status(db, task_id):
    with db.session_scope() as session:
        return session.query(Task).filter_by(id=task_id).first().status


def _sweep():
    from src.autopilot.orchestrator.features import _clean_stale_assigned_tasks

    _clean_stale_assigned_tasks("wf-1", _Logger())


class TestOrphanedActiveTaskInCompletedWorkflowSweep:
    @pytest.mark.parametrize("status", ["pending", "queued", "blocked", "assigned", "in_progress"])
    def test_active_task_in_completed_workflow_is_marked_failed(self, db, status):
        task_id = _seed_task(db, status)

        _sweep()

        assert _status(db, task_id) == "failed", f"a {status!r} task in a completed workflow was not reclaimed -- sits attached to a dead workflow indefinitely"

    def test_done_task_in_completed_workflow_is_left_alone(self, db):
        task_id = _seed_task(db, "done")

        _sweep()

        assert _status(db, task_id) == "done"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
