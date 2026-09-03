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

import subprocess

from src.core.database import (
    Agent,
    AgentResult,
    AutopilotDesign,
    DatabaseManager,
    Feature,
    Phase,
    PromptProposal,
    Task,
    ValidationReview,
    Workflow,
    WorkflowDefinition,
    WorkflowResult,
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
    # POST /projects refuses a non-repo directory (a project that is not a
    # git repository can never run a phase) -- see _validate_base_dir.
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
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


def _seed_core(tmp_path, monkeypatch, design_filename="01-auth.md"):
    """Build a project via the real POST endpoint (exercises _validate_base_dir
    the same way delete_project's caller does), then WorkflowDefinition
    "autopilot" / Workflow "wf-1" (design_id + project_id set) / Phase
    "phase-1" / Task "task-1", committed one row at a time in
    FK-dependency order -- with real FK enforcement on, SQLAlchemy's
    automatic insert-ordering within a single flush isn't reliable for
    this up-then-down-then-up chain of FKs.

    Returns (db_manager, project_id, design_id); callers add whatever
    extra rows their scenario needs on top of this shared base.
    """
    project_dir = tmp_path / "myproject"
    design_dir = project_dir / ".hephaestus" / "specs"
    design_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    (design_dir / design_filename).write_text("# Design\nSomething.")

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
        design = session.query(AutopilotDesign).filter_by(
            project_id=project_id, filename=design_filename
        ).first()
        design_id = design.id

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
    finally:
        session.close()

    return db_manager, project_id, design_id


def _delete_project_via_endpoint(project_id):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, headers={"X-Agent-ID": "system"})
    return client.delete(f"/api/autopilot/projects/{project_id}")


def test_delete_project_deletes_agent_result_before_its_validation_review(tmp_path, monkeypatch):
    """agent_results.verified_by_validation_id is an enforced FK to
    validation_reviews.id, set by ResultService's normal task-validation
    flow for any validated task. delete_project's cascade deleted
    ValidationReview (by task_id) before AgentResult (by task_id) --
    same bug, same fix, as remove_project_design's identical cascade
    (design_file_routes.py), confirmed there via a real FOREIGN KEY
    error before that fix, never propagated here until now."""
    db_manager, project_id, _design_id = _seed_core(tmp_path, monkeypatch)

    session = db_manager.get_session()
    try:
        session.add(Agent(id="agent-1", system_prompt="p", cli_type="claude"))
        session.commit()

        session.add(ValidationReview(
            id="vr-1", task_id="task-1", validator_agent_id="agent-1",
            iteration_number=1, validation_passed=True, feedback="looks good",
        ))
        session.commit()

        session.add(AgentResult(
            id="ar-1", agent_id="agent-1", task_id="task-1",
            markdown_content="c", markdown_file_path="/tmp/r.md",
            result_type="implementation", summary="s",
            verified_by_validation_id="vr-1",
        ))
        session.commit()
    finally:
        session.close()

    resp = _delete_project_via_endpoint(project_id)
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(AgentResult).filter_by(id="ar-1").first() is None
        assert session.query(ValidationReview).filter_by(id="vr-1").first() is None
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
    finally:
        session.close()


def test_delete_project_nulls_workflow_result_id_before_deleting_workflow_result(tmp_path, monkeypatch):
    """workflows.result_id is an enforced FK to workflow_results.id.
    WorkflowResultService sets it to a WorkflowResult row whose OWN
    workflow_id is this same workflow's id -- a self-reference, ordinary
    behavior for e.g. a bugfix/diagnostic pipeline. delete_project's
    cascade deleted WorkflowResult (by workflow_id) without first
    nulling this self-reference -- same bug, same fix, as
    remove_project_design's identical cascade."""
    db_manager, project_id, _design_id = _seed_core(tmp_path, monkeypatch)

    session = db_manager.get_session()
    try:
        session.add(Agent(id="agent-1", system_prompt="p", cli_type="claude"))
        session.commit()

        session.add(WorkflowResult(
            id="res-1", workflow_id="wf-1", agent_id="agent-1",
            result_file_path="/tmp/result.md", result_content="content",
            status="validated",
        ))
        session.commit()

        wf = session.query(Workflow).filter_by(id="wf-1").first()
        wf.result_found = True
        wf.result_id = "res-1"
        session.commit()
    finally:
        session.close()

    resp = _delete_project_via_endpoint(project_id)
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(WorkflowResult).filter_by(id="res-1").first() is None
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
    finally:
        session.close()


def test_delete_project_deletes_prompt_proposals_before_their_workflow(tmp_path, monkeypatch):
    """prompt_proposals.workflow_id is an enforced FK to workflows.id
    with no ondelete clause. forensics_analysis -- a real phase in the
    standard autopilot workflow -- creates one of these rows for every
    prompt improvement it proposes after a pipeline run finishes.
    delete_project's cascade never touched prompt_proposals at all, so
    any project whose workflow ever ran forensics_analysis and produced
    a proposal could not be deleted -- same bug, same fix, as
    remove_project_design's identical cascade."""
    db_manager, project_id, _design_id = _seed_core(tmp_path, monkeypatch)

    session = db_manager.get_session()
    try:
        session.add(PromptProposal(
            id="prop-1", workflow_id="wf-1", phase_name="product_requirements",
            field="description", proposed_value="better description",
            rationale="observed confusion in agent transcript",
        ))
        session.commit()
    finally:
        session.close()

    resp = _delete_project_via_endpoint(project_id)
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(PromptProposal).filter_by(id="prop-1").first() is None
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
    finally:
        session.close()
