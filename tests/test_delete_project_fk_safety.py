"""Regression: delete_project relied entirely on SQLAlchemy's
cascade="all, delete-orphan" (AutopilotProject.designs, AutopilotDesign.
features), which never reaches Workflow or any of its dependents (Task,
Phase, Ticket, DiagnosticRun, etc). features.workflow_id and
autopilot_designs.phase0_workflow_id both FK to workflows.id (NO ACTION,
no cascade), so deleting a project that has ever actually run a design
left every Workflow row behind and the cascade's own AutopilotDesign
delete failed with a FOREIGN KEY violation -- caught cleanly (409), but
making this endpoint functionally dead for any used project. Same bug
class as delete_feature/rerun_design/remove_project_design, just never
propagated here.

Verified with real SQLite FK enforcement forced on: this test suite's
conftest.py globally disables PRAGMA foreign_keys for test
DatabaseManager instances (most fixtures predate FK enforcement), which
would otherwise let a wrong-order/missing delete succeed silently and
give false confidence."""

from src.core.database import (
    AutopilotDesign,
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


def test_delete_project_deletes_full_workflow_subtree(tmp_path, monkeypatch):
    project_dir = tmp_path / "myproject"
    design_dir = project_dir / ".hephaestus" / "specs"
    design_dir.mkdir(parents=True)
    (design_dir / "01-auth.md").write_text("# Auth Design\nImplement OAuth2.")

    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()
    _force_fk_enforcement(db_manager.engine)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, headers={"X-Agent-ID": "system"})

    create = client.post(
        "/api/autopilot/projects",
        json={"name": "myproject", "base_dir": str(project_dir)},
    )
    assert create.status_code == 200, create.text
    project_id = create.json()["id"]

    session = db_manager.get_session()
    try:
        design = session.query(AutopilotDesign).filter_by(project_id=project_id, filename="01-auth.md").first()
        assert design is not None
        design_id = design.id
    finally:
        session.close()

    # Each generation committed separately, in FK-dependency order -- with
    # real FK enforcement on, SQLAlchemy's automatic insert-ordering
    # within a single flush isn't reliable here (workflows.feature_id ->
    # features.id points back "up" the chain this fixture otherwise
    # builds top-down, confusing the unit-of-work dependency sort).
    session = db_manager.get_session()
    try:
        session.add(WorkflowDefinition(id="autopilot", name="Autopilot"))
        session.commit()

        session.add(Workflow(
            id="wf-1", name="Autopilot", status="active",
            phases_folder_path="/tmp/phases", definition_id="autopilot",
            design_id=design_id, project_id=project_id,
        ))
        session.commit()

        session.add(Phase(
            id="phase-1", workflow_id="wf-1", order=1,
            name="product_requirements", description="d", done_definitions=["x"],
        ))
        session.commit()

        session.add(Task(
            id="task-1", workflow_id="wf-1", phase_id="phase-1",
            raw_description="r", done_definition="d", status="done",
        ))
        session.commit()

        session.add(Feature(
            id="feat-1", design_id=design_id, feature_key="auth",
            name="Auth", scope="s", status="active", workflow_id="wf-1",
        ))
        session.commit()

        # autopilot_designs.phase0_workflow_id -> workflows.id, also
        # enforced -- exercises the same nulling this fix adds.
        design_row = session.query(AutopilotDesign).filter_by(id=design_id).first()
        design_row.phase0_workflow_id = "wf-1"
        session.commit()
    finally:
        session.close()

    resp = client.delete(f"/api/autopilot/projects/{project_id}")
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        # If the FK violation still occurs, db.delete(proj)'s own flush()
        # raises IntegrityError, caught by delete_project's except block
        # and surfaced as 409 (asserted above as 200 instead) -- every one
        # of these rows would survive that rollback.
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
        assert session.query(Phase).filter_by(id="phase-1").first() is None
        assert session.query(Task).filter_by(id="task-1").first() is None
        assert session.query(Feature).filter_by(id="feat-1").first() is None
        assert session.query(AutopilotDesign).filter_by(id=design_id).first() is None
    finally:
        session.close()
