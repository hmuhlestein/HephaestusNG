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
    Feature,
    Phase,
    Task,
    Workflow,
    WorkflowDefinition,
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
            design_id="des-1",
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


@pytest.fixture
def seeded_db_with_feature(tmp_path, project_dirs, monkeypatch):
    """Same as seeded_db, plus a Feature row pointing at the workflow
    rerun() deletes -- reproduces the sibling FK bug this fix addresses:
    features.workflow_id -> workflows.id is also enforced, and rerun()
    used to delete Workflow before Feature."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    # conftest.py's session-scoped _skip_fk_enforcement_for_tests fixture
    # forces PRAGMA foreign_keys=OFF on every DatabaseManager's connections
    # (most test fixtures predate FK enforcement and would break under it).
    # That means an assertion here that all rows ended up deleted would
    # pass regardless of deletion ORDER -- SQLite silently allows deleting
    # a referenced parent row with FK checking off, so this test would give
    # false confidence without re-enabling it. "checkout" (not just
    # "connect") is required: QueuePool hands out up to 5 distinct physical
    # connections, and "connect" only fires once per NEW physical
    # connection -- a later checkout of an already-open one would still
    # read OFF without this.
    from sqlalchemy import event

    def _force_fk_on(dbapi_conn, connection_record, *_args):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    event.listen(db_manager.engine, "connect", _force_fk_on)
    event.listen(db_manager.engine, "checkout", _force_fk_on)

    # Each generation committed separately, in FK-dependency order --
    # with real FK enforcement on, SQLAlchemy's automatic insert-ordering
    # within a single flush isn't reliable here (workflows.feature_id ->
    # features.id and autopilot_designs.phase0_workflow_id -> workflows.id
    # both point back "up" the chain this fixture otherwise builds
    # top-down, confusing the unit-of-work dependency sort).
    session = db_manager.get_session()
    try:
        session.add(AutopilotProject(id="proj-1", name="myproject", base_dir=str(project_dirs["project_dir"])))
        session.commit()

        session.add(AutopilotDesign(
            id="des-1", project_id="proj-1", filename="01-auth.md", name="auth",
            ordinal=1, extension=".md",
        ))
        session.commit()

        # Workflow.definition_id -> workflow_definitions.id is also an
        # enforced FK -- missing here (with FK enforcement off elsewhere
        # in the suite, this gap never surfaced), causing the Workflow
        # insert itself to fail once this fixture forces real enforcement.
        session.add(WorkflowDefinition(id="feature_architect", name="Feature Architect"))
        session.commit()

        session.add(Workflow(
            id="wf-old", name="Feature Architect", status="failed",
            paused_by="review", phases_folder_path="/tmp/phases",
            definition_id="feature_architect",
            launch_params={"design_document": str(project_dirs["design_dir"] / "01-auth.md"), "design_id": "des-1"},
            design_id="des-1",
        ))
        session.commit()

        session.add(Phase(
            id="phase-1", workflow_id="wf-old", order=1,
            name="feature_architect", description="d", done_definitions=["x"],
        ))
        session.commit()

        session.add(Task(
            id="task-1", workflow_id="wf-old", phase_id="phase-1",
            raw_description="r", done_definition="d", status="done",
        ))
        session.commit()

        session.add(Feature(
            id="feat-1", design_id="des-1", feature_key="auth",
            name="Auth", scope="s", status="active", workflow_id="wf-old",
        ))
        session.commit()
    finally:
        session.close()

    return db_manager, project_dirs


@pytest.mark.asyncio
async def test_rerun_deletes_feature_before_its_workflow(seeded_db_with_feature):
    db_manager, dirs = seeded_db_with_feature
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
            design_id="des-1",
            invalidate=lambda *a, **k: None,
        )

    assert result.get("status") == "success" or result is not None

    session = db_manager.get_session()
    try:
        # If the FK violation still occurs, this delete silently no-ops
        # (caught by rerun()'s own outer except) and the row survives.
        assert session.query(Workflow).filter_by(id="wf-old").first() is None
        assert session.query(Feature).filter_by(id="feat-1").first() is None
    finally:
        session.close()
