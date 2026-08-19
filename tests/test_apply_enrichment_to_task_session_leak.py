"""Regression test: _apply_enrichment_to_task must not leak its DB session
on a commit() failure.

Found live 2026-08-19, alongside the resolve_phase_id name-lookup bug: an
unresolved phase_id (a phase NAME instead of a UUID, per that bug) made
`UPDATE tasks SET ... phase_id=? ...` fail with a real FOREIGN KEY
constraint violation. This function's session.commit() had no try/finally
around it, so the failing commit propagated straight out with the session
never rolled back or closed -- a leaked connection holding a failed,
uncommitted transaction. The caller's own failure-recovery write
(_handle_task_processing_failure, meant to mark the task "failed") then
plausibly collided with that leaked transaction and failed too, since
nothing in that path has a second layer of error handling -- leaving
three tasks in production permanently stuck at status="pending" with no
phase_id and no error visible anywhere.

Asserts directly on rollback()/close() being called, rather than trying
to reproduce an actually-held SQLite lock: a mocked commit() failure never
sends real SQL to the database (SQLAlchemy's commit() is what triggers the
internal flush), so there is nothing to "hold a lock" in a test either way
-- the thing that actually changed is whether this function cleans up its
own session on the way out, which is directly observable.
"""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.core.database import Task, Workflow
from src.mcp.server._shared import CreateTaskRequest


@pytest.fixture
def _wired_server_state(db_manager, monkeypatch):
    import src.mcp.server._create_task_steps as steps

    monkeypatch.setattr(steps.server_state, "db_manager", db_manager)
    return steps.server_state


def _seed_task(db_manager, task_id, workflow_id):
    with db_manager.session_scope() as session:
        session.add(
            Workflow(id=workflow_id, name="w", phases_folder_path="/tmp", status="active")
        )
        session.add(
            Task(
                id=task_id, workflow_id=workflow_id, raw_description="x", done_definition="x",
                status="pending",
            )
        )


def test_a_failed_commit_rolls_back_and_closes_the_session_instead_of_leaking_it(
    db_manager, _wired_server_state
):
    from src.mcp.server._create_task_steps import _apply_enrichment_to_task

    task_id = str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())
    _seed_task(db_manager, task_id, workflow_id)

    request = CreateTaskRequest(
        task_description="d", done_definition="d", ai_agent_id="agent-1",
        workflow_id=workflow_id,
    )

    lock_error = OperationalError("UPDATE tasks ...", {}, Exception("database is locked"))
    real_rollback = Session.rollback
    real_close = Session.close
    calls = {"rollback": 0, "close": 0}

    def spy_rollback(self, *a, **k):
        calls["rollback"] += 1
        return real_rollback(self, *a, **k)

    def spy_close(self, *a, **k):
        calls["close"] += 1
        return real_close(self, *a, **k)

    with (
        patch.object(Session, "commit", side_effect=lock_error),
        patch.object(Session, "rollback", spy_rollback),
        patch.object(Session, "close", spy_close),
    ):
        with pytest.raises(OperationalError):
            _apply_enrichment_to_task(
                task_id, request, "no-such-phase", workflow_id,
                {"enriched_description": "enriched", "estimated_complexity": 5},
            )

    assert calls["rollback"] == 1, "a failed commit must be rolled back, not left dangling"
    assert calls["close"] == 1, "the session must be closed even when commit() raises"
