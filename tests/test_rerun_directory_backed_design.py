"""Regression: a directory-backed design could not be rerun at all.

/queue/rerun was filename-based end to end -- the request field, the queue
path check, the design lookup, and the workflow match via
launch_params LIKE %filename%. A Spec Kit design has no filename (its source
is a specs/<n>-<slug>/ directory), so the button either 404'd on a queue path
that never existed or, before spec_key, matched a synthetic filename and then
404'd on the same check. Rerun is keyed by design_id now, like every other
design endpoint.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.autopilot.repair_service import RepairService
from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    DatabaseManager,
    Task,
    Workflow,
    directory_spec_key,
)


@pytest.fixture
def speckit_project(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db = DatabaseManager(db_path)
    db.create_tables()

    project_dir = tmp_path / "parent"
    source_dir = project_dir / "specs" / "001-conversation-history"
    source_dir.mkdir(parents=True)
    (source_dir / "spec.md").write_text("# Conversation history\n")

    with db.session_scope() as session:
        session.add(
            AutopilotProject(id="proj-1", name="ParentChat", base_dir=str(project_dir))
        )
        session.add(
            AutopilotDesign(
                id="des-speckit",
                project_id="proj-1",
                spec_key=directory_spec_key("001-conversation-history"),
                filename=None,
                source_dir=str(source_dir),
                name="001-conversation-history",
                ordinal=5,
                status="active",
            )
        )
        # Its own feature workflow, linked only by the FK -- no launch_params
        # for a filename substring to match, which is exactly the case the old
        # matcher missed.
        session.add(
            Workflow(
                id="wf-speckit",
                name="autopilot",
                phases_folder_path="/tmp",
                status="active",
                definition_id="autopilot",
                design_id="des-speckit",
            )
        )
        session.add(
            Task(
                id="task-speckit",
                workflow_id="wf-speckit",
                phase_id=None,
                raw_description="x",
                done_definition="x",
                status="in_progress",
            )
        )
    return {"project_dir": project_dir, "db": db}


@pytest.mark.asyncio
async def test_a_directory_backed_design_can_be_rerun(speckit_project):
    service = MagicMock()
    service.running = False
    service.start = AsyncMock(return_value=None)
    service.stop = AsyncMock(return_value=None)

    with patch("src.autopilot.service.get_autopilot_service", return_value=service), patch(
        "src.autopilot.service.get_registry"
    ) as registry, patch(
        "src.autopilot.orchestrator.state._resolve_project_id", return_value="proj-1"
    ), patch(
        "src.autopilot.orchestrator.state._get_or_create_project_id", return_value="proj-1"
    ):
        registry.return_value.try_reserve.return_value = (True, "")
        result = await RepairService().rerun(
            project_path=str(speckit_project["project_dir"]),
            design_id="des-speckit",
            invalidate=lambda *a, **k: None,
        )

    assert result["rerun"] is True
    assert result["spec_key"] == "_workspace:001-conversation-history"
    service.start.assert_awaited()

    with speckit_project["db"].session_scope() as session:
        # Clean slate: the design's own workflow and task are gone.
        assert session.query(Workflow).filter_by(id="wf-speckit").first() is None
        assert session.query(Task).filter_by(id="task-speckit").first() is None
        # And it is pinned to the front of the queue by ordinal, which is what
        # pick_next_design orders by -- the .queue_order.json mirror is keyed
        # by filename and cannot carry a design that has none.
        design = session.query(AutopilotDesign).filter_by(id="des-speckit").first()
        assert design.ordinal < 0


@pytest.mark.asyncio
async def test_an_unknown_design_id_is_a_404(speckit_project):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await RepairService().rerun(
            project_path=str(speckit_project["project_dir"]),
            design_id="des-nope",
            invalidate=lambda *a, **k: None,
        )
    assert exc.value.status_code == 404
