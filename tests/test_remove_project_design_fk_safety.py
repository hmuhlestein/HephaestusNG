"""Regression: remove_project_design's delete cascade deleted Workflow
before Feature, even though features.workflow_id is an enforced FK to
workflows.id -- the same bug class fixed in delete_feature
(feature_routes.py) and rerun_design (repair_service.py), never
propagated here. Verified with real SQLite FK enforcement forced on:
this test suite's conftest.py globally disables PRAGMA foreign_keys for
test DatabaseManager instances (most fixtures predate FK enforcement),
which would otherwise let a wrong-order delete succeed silently and give
false confidence."""

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


def _force_fk_enforcement(engine):
    from sqlalchemy import event

    def _on(dbapi_conn, connection_record, *_args):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # "checkout" (not just "connect") is required: QueuePool hands out up
    # to 5 distinct physical connections, and "connect" only fires once
    # per NEW physical connection -- a later checkout of an already-open
    # one would still read the conftest-forced OFF value without this.
    event.listen(engine, "connect", _on)
    event.listen(engine, "checkout", _on)


def test_remove_project_design_deletes_feature_before_its_workflow(tmp_path, monkeypatch):
    project_dir = tmp_path / "myproject"
    design_dir = project_dir / ".hephaestus" / "specs"
    design_dir.mkdir(parents=True)
    (design_dir / "01-auth.md").write_text("# Auth Design\nImplement OAuth2.")

    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()
    _force_fk_enforcement(db_manager.engine)

    # Each generation committed separately, in FK-dependency order --
    # with real FK enforcement on, SQLAlchemy's automatic insert-ordering
    # within a single flush isn't reliable here (workflows.feature_id ->
    # features.id and autopilot_designs.phase0_workflow_id ->
    # workflows.id both point back "up" the chain this fixture otherwise
    # builds top-down, confusing the unit-of-work dependency sort).
    session = db_manager.get_session()
    try:
        session.add(AutopilotProject(id="proj-1", name="myproject", base_dir=str(project_dir)))
        session.commit()

        session.add(AutopilotDesign(
            id="des-1", project_id="proj-1", filename="01-auth.md", name="auth",
            ordinal=1, extension=".md",
        ))
        session.commit()

        # Workflow.definition_id -> workflow_definitions.id is also an
        # enforced FK.
        session.add(WorkflowDefinition(id="autopilot", name="Autopilot"))
        session.commit()

        session.add(Workflow(
            id="wf-old", name="Autopilot", status="failed",
            phases_folder_path="/tmp/phases", definition_id="autopilot",
            launch_params={"design_document": str(design_dir / "01-auth.md"), "design_id": "des-1"},
            design_id="des-1",
        ))
        session.commit()

        session.add(Phase(
            id="phase-1", workflow_id="wf-old", order=1,
            name="product_requirements", description="d", done_definitions=["x"],
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

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, headers={"X-Agent-ID": "system"})

    resp = client.delete("/api/autopilot/projects/proj-1/designs/01-auth.md")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"removed": "01-auth.md"}

    session = db_manager.get_session()
    try:
        # If the FK violation still occurs, the delete cascade's own outer
        # except catches it and these rows survive.
        assert session.query(Workflow).filter_by(id="wf-old").first() is None
        assert session.query(Feature).filter_by(id="feat-1").first() is None
        assert session.query(AutopilotDesign).filter_by(id="des-1").first() is None
    finally:
        session.close()
