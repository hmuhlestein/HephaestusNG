"""Regression tests: _create_task_steps.py's manually-managed DB sessions
must not leak on a commit()/query() failure.

Found live 2026-08-19, alongside the resolve_phase_id name-lookup bug: an
unresolved phase_id (a phase NAME instead of a UUID, per that bug) made
`UPDATE tasks SET ... phase_id=? ...` fail with a real FOREIGN KEY
constraint violation. _apply_enrichment_to_task's session.commit() had no
try/finally around it, so the failing commit propagated straight out with
the session never rolled back or closed -- a leaked connection holding a
failed, uncommitted transaction. The caller's own failure-recovery write
(_handle_task_processing_failure, meant to mark the task "failed") then
plausibly collided with that leaked transaction and failed too, since
nothing in that path had a second layer of error handling -- leaving
three tasks in production permanently stuck at status="pending" with no
phase_id and no error visible anywhere.

A follow-up survey (docs/SOLID_OO_REVIEW_UPDATE_2026-08-19.md finding 1.15,
design_docs/phase3_except_exception_survey_findings.md) found the same
leak shape at 4 more sites in this file, including
_handle_task_processing_failure itself -- the exact second half of the
incident this module's docstring describes. All are covered below.

Asserts directly on rollback()/close() being called, rather than trying
to reproduce an actually-held SQLite lock: a mocked commit() failure never
sends real SQL to the database (SQLAlchemy's commit() is what triggers the
internal flush), so there is nothing to "hold a lock" in a test either way
-- the thing that actually changed is whether each function cleans up its
own session on the way out, which is directly observable.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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


def _spy_session_lifecycle():
    """Patch Session.rollback/close to count calls while still doing the
    real thing, matching the pattern above. Returns the call-count dict."""
    real_rollback = Session.rollback
    real_close = Session.close
    calls = {"rollback": 0, "close": 0}

    def spy_rollback(self, *a, **k):
        calls["rollback"] += 1
        return real_rollback(self, *a, **k)

    def spy_close(self, *a, **k):
        calls["close"] += 1
        return real_close(self, *a, **k)

    return calls, spy_rollback, spy_close


def test_persist_new_task_rolls_back_and_closes_on_a_failed_commit(
    db_manager, _wired_server_state
):
    from src.mcp.server._create_task_steps import _persist_new_task

    request = CreateTaskRequest(
        task_description="d", done_definition="d", ai_agent_id="agent-1",
    )

    lock_error = OperationalError("INSERT INTO tasks ...", {}, Exception("database is locked"))
    calls, spy_rollback, spy_close = _spy_session_lifecycle()

    with (
        patch.object(Session, "commit", side_effect=lock_error),
        patch.object(Session, "rollback", spy_rollback),
        patch.object(Session, "close", spy_close),
    ):
        with pytest.raises(OperationalError):
            _persist_new_task("agent-1", request, str(uuid.uuid4()))

    assert calls["rollback"] == 1, "a failed commit must be rolled back, not left dangling"
    assert calls["close"] == 1, "the session must be closed even when commit() raises"


def test_check_for_duplicate_task_rolls_back_and_closes_on_a_failed_commit(
    db_manager, _wired_server_state, monkeypatch
):
    """The outer except in _check_for_duplicate_task deliberately swallows
    a failure (dedup-checking is best-effort, not a hard requirement) --
    that resilience behavior must be preserved. What must change is
    whether the session gets cleaned up first."""
    import src.mcp.server._create_task_steps as steps
    from src.mcp.server._create_task_steps import _check_for_duplicate_task

    task_id = str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())
    _seed_task(db_manager, task_id, workflow_id)

    monkeypatch.setattr(steps, "get_config", lambda: MagicMock(task_dedup_enabled=True))
    _wired_server_state.embedding_service = MagicMock(
        generate_embedding=AsyncMock(return_value=[0.1, 0.2, 0.3])
    )
    _wired_server_state.task_similarity_service = MagicMock(
        check_for_duplicates=AsyncMock(
            return_value={
                "is_duplicate": True,
                "duplicate_of": "other-task",
                "max_similarity": 0.97,
            }
        )
    )

    lock_error = OperationalError("UPDATE tasks ...", {}, Exception("database is locked"))
    calls, spy_rollback, spy_close = _spy_session_lifecycle()

    with (
        patch.object(Session, "commit", side_effect=lock_error),
        patch.object(Session, "rollback", spy_rollback),
        patch.object(Session, "close", spy_close),
    ):
        result = await_or_run(
            _check_for_duplicate_task(task_id, None, {"enriched_description": "x"})
        )

    assert result is False, (
        "a commit failure while marking a duplicate must still degrade to "
        "'continue without dedup', not propagate out of the caller's flow"
    )
    assert calls["rollback"] == 1, "a failed commit must be rolled back, not left dangling"
    assert calls["close"] == 1, "the session must be closed even when commit() raises"


def test_handle_task_processing_failure_rolls_back_and_closes_on_a_failed_commit(
    db_manager, _wired_server_state
):
    """This is the caller's own failure-recovery write named in this
    module's docstring -- the second half of the documented incident. It
    must not itself raise (nothing above it in the fire-and-forget
    background task would catch it), but it must clean up its session."""
    from src.mcp.server._create_task_steps import _handle_task_processing_failure

    task_id = str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())
    _seed_task(db_manager, task_id, workflow_id)

    lock_error = OperationalError("UPDATE tasks ...", {}, Exception("database is locked"))
    calls, spy_rollback, spy_close = _spy_session_lifecycle()

    with (
        patch.object(Session, "commit", side_effect=lock_error),
        patch.object(Session, "rollback", spy_rollback),
        patch.object(Session, "close", spy_close),
    ):
        # Must not raise.
        await_or_run(_handle_task_processing_failure(task_id, ValueError("boom")))

    assert calls["rollback"] == 1, "a failed commit must be rolled back, not left dangling"
    assert calls["close"] == 1, "the session must be closed even when commit() raises"


def test_resolve_phase_and_enrich_closes_the_session_on_a_failed_query(
    db_manager, _wired_server_state, monkeypatch
):
    """This block is read-only (no write, no commit) -- there's nothing to
    roll back, but the session must still be closed if the query raises."""
    import src.services.task_enrichment_service as enrichment_module
    from src.mcp.server._create_task_steps import _resolve_phase_and_enrich

    workflow_id = str(uuid.uuid4())
    phase_id = str(uuid.uuid4())

    monkeypatch.setattr(
        enrichment_module.TaskEnrichmentService, "resolve_phase_id", lambda **kw: phase_id
    )
    monkeypatch.setattr(
        enrichment_module.TaskEnrichmentService,
        "get_phase_context_str",
        lambda pid: ("", None),
    )
    monkeypatch.setattr(
        enrichment_module.TaskEnrichmentService,
        "enrich",
        AsyncMock(
            return_value={
                "enriched_task": {"enriched_description": "x", "estimated_complexity": 5},
                "context_memories": [],
                "project_context": "",
            }
        ),
    )

    request = CreateTaskRequest(
        task_description="d", done_definition="d", ai_agent_id="agent-1",
        workflow_id=workflow_id, cwd=None,
    )

    lock_error = OperationalError("SELECT phases ...", {}, Exception("database is locked"))
    calls = {"close": 0}
    real_close = Session.close

    def spy_close(self, *a, **k):
        calls["close"] += 1
        return real_close(self, *a, **k)

    with (
        patch.object(Session, "query", side_effect=lock_error),
        patch.object(Session, "close", spy_close),
    ):
        with pytest.raises(OperationalError):
            await_or_run(_resolve_phase_and_enrich(request, "agent-1", "task-1"))

    assert calls["close"] == 1, "the session must be closed even when the query raises"


def await_or_run(coro):
    """Run an async function's coroutine to completion in a fresh event
    loop -- these tests aren't themselves async (no pytest-asyncio marker
    needed for the rest of the file), so drive the one async call directly."""
    import asyncio

    return asyncio.run(coro)
