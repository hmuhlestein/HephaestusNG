"""Regression tests for the "agent dispatched, DB link write fails" gap in
src/autopilot/orchestrator/phase_transitions.py.

Three sites (_retry_failed_tasks, _create_corrective_task_body,
_resume_stuck_workflow_tasks) all follow the same two-step pattern: create
a live agent via create_agent_for_task_direct, THEN write
assigned_agent_id/status="in_progress" back onto the task row in a second,
separate DB transaction. If that second write raises, the task was left
"pending" with assigned_agent_id still None -- invisible to every sweep:
_clean_stale_assigned_tasks's terminated-agent pass requires
assigned_agent_id.isnot(None), and _retry_failed_tasks itself only
re-queries status="failed". Meanwhile a real, live, orphaned agent is
burning tokens on a task nothing points back to.

Each site now catches that specific failure and reverts the task to
"failed" with an "Orphaned: ..." reason, so _retry_failed_tasks's own
~20s sweep picks it back up (its is_orphan check exempts "Orphaned:"
reasons from the retry-count cap).
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from src.core.database import DatabaseManager, Phase, PhaseExecution, Task, Workflow


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
        self.messages.append(("info", msg))

    def warning(self, msg):
        self.messages.append(("warning", msg))

    def error(self, msg):
        self.messages.append(("error", msg))


def _task(db, **overrides):
    task_id = str(uuid.uuid4())
    fields = dict(
        id=task_id,
        workflow_id="wf-1",
        raw_description="r",
        done_definition="d",
        status="failed",
        priority="medium",
        retry_count=0,
    )
    fields.update(overrides)
    with db.session_scope() as session:
        session.add(Task(**fields))
    return task_id


def _status_and_reason(db, task_id):
    with db.session_scope() as session:
        t = session.query(Task).filter_by(id=task_id).first()
        return t.status, t.failure_reason, t.assigned_agent_id


def _break_link_commit(monkeypatch, task_id, agent_id):
    """Make the specific commit that writes assigned_agent_id=agent_id +
    status='in_progress' onto `task_id` raise, while leaving every other
    commit (test setup, the revert-to-failed write, unrelated sessions)
    working normally. Targets the write itself rather than counting
    get_db() calls, so it survives refactors that change how many DB round
    trips sit between dispatch and the link write."""
    real_commit = Session.commit

    def fake_commit(self):
        for obj in list(self.dirty) + list(self.new):
            if (
                isinstance(obj, Task)
                and obj.id == task_id
                and obj.status == "in_progress"
                and obj.assigned_agent_id == agent_id
            ):
                raise RuntimeError("simulated DB failure linking agent to task")
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", fake_commit)


class TestRetryFailedTasksLinkFailure:
    def test_link_failure_reverts_to_failed_not_left_pending(self, db, monkeypatch):
        from src.autopilot.orchestrator import phase_transitions as pt

        task_id = _task(db)
        agent_id = "agent-orphan-1"

        # update_task_status is left real (it's a plain, non-fallible DB
        # write) so it actually flips the row "failed" -> "pending" -- the
        # revert-to-failed guard below requires that transition to have
        # really happened, and get_tasks(status="failed") is how this
        # function finds its candidates in the first place.
        monkeypatch.setattr(
            pt, "create_agent_for_task_direct", lambda *a, **k: {"agent_id": agent_id}
        )
        monkeypatch.setattr(pt, "increment_task_retry_count", lambda tid: 1)
        monkeypatch.setattr(
            "src.autopilot.spec.get_max_task_retries", lambda wf_id: 5
        )
        _break_link_commit(monkeypatch, task_id, agent_id)

        pt._retry_failed_tasks("wf-1", _Logger())

        status, reason, assigned = _status_and_reason(db, task_id)
        assert status == "failed", (
            f"task stranded in {status!r} with a live orphaned agent -- "
            "no sweep re-queries 'pending' tasks with no agent"
        )
        assert reason and "orphaned" in reason.lower()
        assert assigned is None


class TestCreateCorrectiveTaskLinkFailure:
    def test_link_failure_reverts_to_failed_not_left_pending(self, db, monkeypatch):
        from src.autopilot.orchestrator import phase_transitions as pt

        with db.session_scope() as session:
            session.add(
                Phase(
                    id="phase-1",
                    workflow_id="wf-1",
                    order=1,
                    name="development",
                    description="d",
                    done_definitions=["done"],
                )
            )
            session.add(
                PhaseExecution(id=str(uuid.uuid4()), phase_id="phase-1", status="in_progress")
            )

        agent_id = "agent-orphan-2"
        monkeypatch.setattr(
            pt, "create_agent_for_task_direct", lambda *a, **k: {"agent_id": agent_id}
        )

        task_id = str(uuid.uuid4())
        _break_link_commit(monkeypatch, task_id, agent_id)

        result = pt._create_corrective_task_body(
            "wf-1", "phase-1", "development", "fix this", _Logger(), task_id
        )

        assert result is None
        status, reason, assigned = _status_and_reason(db, task_id)
        assert status == "failed", (
            f"corrective task stranded in {status!r} with a live orphaned "
            "agent -- blocks future corrective tasks for this phase"
        )
        assert reason and "orphaned" in reason.lower()
        assert assigned is None


class TestResumeStuckWorkflowTasksLinkFailure:
    def test_link_failure_reverts_to_failed_not_left_pending(self, db, monkeypatch):
        from src.autopilot.orchestrator import phase_transitions as pt

        task_id = _task(db, status="failed")
        agent_id = "agent-orphan-3"

        monkeypatch.setattr(
            pt, "create_agent_for_task_direct", lambda *a, **k: {"agent_id": agent_id}
        )
        _break_link_commit(monkeypatch, task_id, agent_id)

        pt._resume_stuck_workflow_tasks("wf-1", _Logger())

        status, reason, assigned = _status_and_reason(db, task_id)
        assert status == "failed", (
            f"task stranded in {status!r} after a failed resume-link -- only "
            "heals on the next explicit resume of this same workflow"
        )
        assert reason and "orphaned" in reason.lower()
        assert assigned is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
