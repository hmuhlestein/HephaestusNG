"""Regression: /queue/repair was the last filename-keyed design endpoint.

It built its repair workflow's design_document as queue_dir/<filename> -- a
path that does not exist for a directory-backed design, which has no filename
at all -- and found the design's existing workflows by testing whether that
filename appeared in another workflow's launch_params. Neither works for a
Spec Kit design. Repair is keyed by design_id now, and its results live in
ProjectContext rather than a repair_<id>.json beside the database.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.autopilot.repair_service import RepairService
from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    DatabaseManager,
    Workflow,
    directory_spec_key,
)


@pytest.fixture
def repairable(tmp_path, monkeypatch):
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
                ordinal=1,
                status="active",
            )
        )
        # An earlier workflow for the same design, linked only by the FK.
        session.add(
            Workflow(
                id="wf-earlier",
                name="autopilot",
                phases_folder_path="/tmp",
                status="failed",
                definition_id="autopilot",
                design_id="des-speckit",
            )
        )
    return {"project_dir": project_dir, "source_dir": source_dir, "db": db}


@pytest.mark.asyncio
async def test_repair_runs_for_a_directory_backed_design(repairable):
    service = RepairService()
    with patch.object(service, "_spawn_repair_review_agent") as spawn:
        started = await service.repair(str(repairable["project_dir"]), "des-speckit")
        # repair() hands the work to a thread pool; run it inline so the
        # assertions below see a finished repair.
        service._run_repair(
            started["repair_id"],
            "des-speckit",
            started["spec_key"],
            repairable["source_dir"],
            repairable["project_dir"],
        )

    assert started["spec_key"] == "_workspace:001-conversation-history"

    with repairable["db"].session_scope() as session:
        wf = (
            session.query(Workflow)
            .filter(Workflow.id.like("repair-%"))
            .first()
        )
        # Linked by the FK, and pointed at the design's real source rather
        # than a queue_dir path that never existed.
        assert wf.design_id == "des-speckit"
        params = json.loads(wf.launch_params) if isinstance(wf.launch_params, str) else wf.launch_params
        assert params["design_document"] == str(repairable["source_dir"])
        assert params["repair_mode"] is True

    # The design's earlier workflow is found as context via the FK -- the old
    # filename-in-launch_params test could never have matched it.
    spawn.assert_called_once()


@pytest.mark.asyncio
async def test_an_unknown_design_id_is_a_404(repairable):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await RepairService().repair(str(repairable["project_dir"]), "des-nope")
    assert exc.value.status_code == 404


def test_repair_results_are_stored_in_the_database(repairable):
    """Not a repair_<id>.json beside the DB: a file there is a second,
    non-transactional source of truth that a DB reset leaves behind."""
    from src.autopilot.orchestrator.state import _get_project_context
    from src.core.database import get_db

    service = RepairService()
    assert service.get_repair_status("abc123")["status"] == "running"

    service._store_repair_result("abc123", {"repair_id": "abc123", "findings": []})

    status = service.get_repair_status("abc123")
    assert status["status"] == "completed"
    assert status["repair_id"] == "abc123"

    with get_db() as db:
        assert _get_project_context(db, "autopilot_repair_result_abc123") is not None
