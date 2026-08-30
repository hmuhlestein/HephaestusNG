"""Project-scoping regression tests for POST /api/autopilot/queue/requeue.

Phase 2 §4.11 (bulk state-mutation project/design-scope audit). requeue_design
terminates agents and pauses workflows, but selected them by
`filename in str(launch_params["design_document"])` with no project filter --
so requeuing a design in one project could pause an unrelated project's
workflow whose design document merely shared (or contained) the same
filename. That is the incident shape 9cb947c was root-caused from: a healthy
agent killed mid-work by another design's queue action.

rerun_design, the sibling endpoint in the same module, was scoped by 9cb947c;
requeue never got the same treatment. These tests pin both halves of the fix.
"""

import pytest


@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def isolate_queue_order(monkeypatch, tmp_path):
    """Neutralize cache invalidation, which is orthogonal to the scoping this
    file tests and touches state outside the test DB. The queue order itself
    now lives in AutopilotDesign.ordinal, inside the test DB, so there is no
    on-disk bookkeeping left to stub."""
    import src.mcp.autopilot.queue_routes as qr

    monkeypatch.setattr(qr, "_invalidate", lambda *a, **k: None)


def _make_workflow(db, wf_id, project_id, design_doc):
    import json

    from src.core.constants import DESIGN_WORKFLOW_DEFINITION_IDS
    from src.core.database import Workflow

    with db.session_scope() as session:
        session.add(
            Workflow(
                id=wf_id,
                name=wf_id,
                phases_folder_path="/tmp",
                status="active",
                project_id=project_id,
                definition_id=next(iter(DESIGN_WORKFLOW_DEFINITION_IDS)),
                launch_params=json.dumps({"design_document": design_doc}),
            )
        )


@pytest.mark.asyncio
async def test_requeue_does_not_pause_another_projects_same_named_design(
    queue_db, isolate_queue_order
):
    from src.mcp.autopilot.queue_routes import requeue_design
    from src.core.database import Workflow

    _make_workflow(queue_db, "wf-mine", "proj-a", "/repos/a/designs/design.md")
    _make_workflow(queue_db, "wf-theirs", "proj-b", "/repos/b/designs/design.md")

    result = await requeue_design({"filename": "design.md", "project_id": "proj-a"})

    assert result["paused_workflows"] == 1
    with queue_db.session_scope() as session:
        assert session.query(Workflow).filter_by(id="wf-mine").first().status == "paused"
        assert session.query(Workflow).filter_by(id="wf-theirs").first().status == "active"


@pytest.mark.asyncio
async def test_requeue_does_not_match_a_design_merely_containing_the_name(
    queue_db, isolate_queue_order
):
    """`filename in design_doc` also matched supersets of the name."""
    from src.mcp.autopilot.queue_routes import requeue_design
    from src.core.database import Workflow

    _make_workflow(queue_db, "wf-exact", "proj-a", "/repos/a/designs/api.md")
    _make_workflow(queue_db, "wf-superset", "proj-a", "/repos/a/designs/legacy-api.md")

    result = await requeue_design({"filename": "api.md", "project_id": "proj-a"})

    assert result["paused_workflows"] == 1
    with queue_db.session_scope() as session:
        assert session.query(Workflow).filter_by(id="wf-exact").first().status == "paused"
        assert session.query(Workflow).filter_by(id="wf-superset").first().status == "active"


@pytest.mark.asyncio
async def test_requeue_resets_a_queued_task_through_the_locked_path(
    queue_db, isolate_queue_order, monkeypatch
):
    """Regression: the batch reset here used to include "queued" tasks in
    the same unlocked status="pending" write as "assigned"/"in_progress"
    ones -- an unlocked write racing claim_next_queued_task's locked
    select-then-dequeue sequence (running on an executor thread) could let
    a task this requeue just reset get dispatched anyway. Queued tasks are
    now routed through QueueService.reset_queued_task_to_pending instead,
    verified here by confirming the task actually lands on "pending" (not
    left "queued", and not silently skipped)."""
    from src.core.database import Task
    from src.mcp.autopilot.queue_routes import requeue_design

    _make_workflow(queue_db, "wf-mine", "proj-a", "/repos/a/designs/design.md")
    with queue_db.session_scope() as session:
        session.add(
            Task(
                id="task-queued", workflow_id="wf-mine", raw_description="r",
                done_definition="d", status="queued",
            )
        )

    from src.services.queue_service import QueueService

    class _FakeServerState:
        def __init__(self, db_manager):
            self.queue_service = QueueService(db_manager, max_concurrent_agents=3)

    monkeypatch.setattr(
        "src.core.app_context.get_app_state",
        lambda: _FakeServerState(queue_db),
    )

    result = await requeue_design({"filename": "design.md", "project_id": "proj-a"})

    assert result["paused_workflows"] == 1
    with queue_db.session_scope() as session:
        task = session.query(Task).filter_by(id="task-queued").first()
        assert task.status == "pending"
        assert task.queue_position is None
