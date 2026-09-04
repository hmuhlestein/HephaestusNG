"""Regression test: TaskService.get_tasks must support filtering by
phase_id server-side.

Found live: PhaseDetailPanel.tsx's "Tasks" sub-tab called
getTasks(0, 100, undefined, undefined) with no phase scoping at all --
the 100 most-recently-CREATED tasks SYSTEM-WIDE, then filtered
client-side by phase_id. On a busy instance (2,000+ tasks), any phase
whose own task wasn't among the globally most-recent 100 silently
disappeared from its own "Tasks" tab -- showing "No tasks in this phase
yet" for a phase that genuinely had one, e.g. an older task still
pending on a paused workflow while unrelated phases elsewhere kept
churning out newer tasks. The fix adds a phase_id query param so the
DB query itself is scoped to the phase, independent of how much
unrelated activity exists elsewhere.
"""

import pytest

from src.core.database import DatabaseManager, Phase, Task, Workflow
from src.mcp.frontend.task_service import TaskService


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def task_service(db_manager):
    return TaskService(db_manager=db_manager, agent_manager=None)


@pytest.mark.asyncio
async def test_phase_id_filter_finds_an_old_task_buried_under_newer_unrelated_ones(
    db_manager, task_service
):
    with db_manager.session_scope() as session:
        session.add(
            Workflow(id="wf-1", name="wf", phases_folder_path="/tmp", definition_id="autopilot")
        )
        session.add(
            Phase(
                id="phase-target", workflow_id="wf-1", name="security_review",
                order=8, description="d", done_definitions=["x"],
            )
        )
        session.add(
            Phase(
                id="phase-other", workflow_id="wf-1", name="development",
                order=5, description="d", done_definitions=["x"],
            )
        )
        # The task we care about -- created first, so it's the OLDEST row.
        session.add(
            Task(
                id="task-target", workflow_id="wf-1", phase_id="phase-target",
                raw_description="r", done_definition="d", status="pending",
                failure_reason="CLI session limit reached",
            )
        )
        # 100 newer, unrelated tasks in a DIFFERENT phase -- enough to push
        # task-target off the end of a "100 most recent, no phase filter"
        # query, reproducing the exact live symptom.
        for i in range(100):
            session.add(
                Task(
                    id=f"task-noise-{i}", workflow_id="wf-1", phase_id="phase-other",
                    raw_description="r", done_definition="d", status="pending",
                )
            )

    result = await task_service.get_tasks(skip=0, limit=100, phase_id="phase-target")

    assert [t["id"] for t in result] == ["task-target"], (
        "phase_id filtering must scope the query itself, not rely on the "
        "task happening to fall within an unscoped 'most recent N' window"
    )
    assert result[0]["failure_reason"] == "CLI session limit reached"
