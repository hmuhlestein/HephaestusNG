"""Regression: RepairService.rerun()'s Step 2b delete cascade never
cleared AutopilotDesign.phase0_workflow_id before deleting the Workflow
row it points to, failing the delete with a real sqlite3.IntegrityError
(FOREIGN KEY constraint failed) -- caught by rerun()'s own outer except
and silently logged, so the whole Step 2b transaction rolled back (leaving
the OLD workflow, including its now-permanently-missing worktree, in
place) while "start pipeline" proceeded anyway. Observed live: the
orchestrator got stuck resuming a deleted-worktree workflow forever
(~3s/cycle, 0 designs processed), which is exactly what "the Rerun button
does nothing" looked like from the UI."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    DatabaseManager,
    Phase,
    Task,
    Workflow,
)


@pytest.fixture
def project_dirs(tmp_path):
    project_dir = tmp_path / "myproject"
    design_dir = project_dir / ".hephaestus" / "specs"
    design_dir.mkdir(parents=True)
    (design_dir / "01-auth.md").write_text("# Auth Design\nImplement OAuth2.")
    return {"project_dir": project_dir, "design_dir": design_dir}


@pytest.fixture
def seeded_db(tmp_path, project_dirs, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    session = db_manager.get_session()
    try:
        proj = AutopilotProject(id="proj-1", name="myproject", base_dir=str(project_dirs["project_dir"]))
        session.add(proj)
        design = AutopilotDesign(
            id="des-1", project_id="proj-1", filename="01-auth.md", name="auth",
            ordinal=1, extension=".md",
        )
        session.add(design)
        wf = Workflow(
            id="wf-old", name="Feature Architect", status="failed",
            paused_by="review", phases_folder_path="/tmp/phases",
            definition_id="feature_architect",
            launch_params={"design_document": str(project_dirs["design_dir"] / "01-auth.md"), "design_id": "des-1"},
            design_id="des-1",
        )
        session.add(wf)
        session.add(Phase(
            id="phase-1", workflow_id="wf-old", order=1,
            name="feature_architect", description="d", done_definitions=["x"],
        ))
        session.add(Task(
            id="task-1", workflow_id="wf-old", phase_id="phase-1",
            raw_description="r", done_definition="d", status="done",
        ))
        session.commit()

        # The FK this fix addresses: phase0_workflow_id pointing at the
        # workflow rerun() is about to delete.
        design.phase0_workflow_id = "wf-old"
        session.commit()
    finally:
        session.close()

    return db_manager, project_dirs


@pytest.mark.asyncio
async def test_rerun_clears_phase0_workflow_id_before_deleting_its_workflow(seeded_db):
    db_manager, dirs = seeded_db
    from src.autopilot.repair_service import RepairService

    with patch("src.autopilot.service.get_autopilot_service") as mock_get_service, \
         patch("src.autopilot.service.get_registry") as mock_get_registry:
        mock_service = MagicMock()
        mock_service.running = False
        mock_service.start = AsyncMock(return_value=None)
        mock_get_service.return_value = mock_service
        mock_get_registry.return_value.try_reserve.return_value = (True, "")

        result = await RepairService().rerun(
            project_path=str(dirs["project_dir"]),
            filename="01-auth.md",
            load_queue_order=lambda project_id: [],
            save_queue_order=lambda order, project_id: None,
            invalidate=lambda *a, **k: None,
        )

    assert result.get("status") == "success" or result is not None

    session = db_manager.get_session()
    try:
        # The old workflow must actually be gone -- if the FK violation
        # still occurs, this delete silently no-ops and the row survives.
        assert session.query(Workflow).filter_by(id="wf-old").first() is None

        design = session.query(AutopilotDesign).filter_by(id="des-1").first()
        assert design.phase0_workflow_id is None
        assert design.status == "pending"
    finally:
        session.close()
