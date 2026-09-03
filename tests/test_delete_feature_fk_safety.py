"""Regression: delete_feature's delete cascade (feature_routes.py) has the
same three FK bugs already fixed in remove_project_design (design_file_
routes.py), delete_project (project_routes.py), and RepairService.rerun's
Step 2b (repair_service.py) -- all four are independent copies of the same
delete-cascade shape, and only two of the four had gotten the fix before
this file: AgentResult deleted after ValidationReview instead of before
(agent_results.verified_by_validation_id is an enforced FK), workflows.
result_id's self-reference to WorkflowResult never nulled before deleting
it, and prompt_proposals.workflow_id rows never deleted at all.

Verified with real SQLite FK enforcement forced on: this test suite's
conftest.py globally disables PRAGMA foreign_keys for test DatabaseManager
instances (most fixtures predate FK enforcement), which would otherwise
let a wrong-order/missing delete succeed silently and give false
confidence."""

from src.core.database import (
    Agent,
    AgentResult,
    AutopilotDesign,
    AutopilotProject,
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


def _seed_core(tmp_path, monkeypatch):
    """Build AutopilotProject "proj-1" / WorkflowDefinition "autopilot" /
    Workflow "wf-1" / Phase "phase-1" / Task "task-1" / Feature "feat-1"
    (workflow_id="wf-1"), committed one row at a time in FK-dependency
    order -- with real FK enforcement on, SQLAlchemy's automatic
    insert-ordering within a single flush isn't reliable for this
    up-then-down chain of FKs.

    Returns the DatabaseManager; callers add whatever extra rows their
    scenario needs on top of this shared base.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()
    _force_fk_enforcement(db_manager.engine)

    session = db_manager.get_session()
    try:
        session.add(AutopilotProject(id="proj-1", name="myproject", base_dir="/tmp/myproject"))
        session.commit()

        # features.design_id is also an enforced FK to autopilot_designs.id.
        session.add(AutopilotDesign(
            id="des-1", project_id="proj-1", filename="01-auth.md", name="auth",
            ordinal=1, extension=".md",
        ))
        session.commit()

        session.add(WorkflowDefinition(id="autopilot", name="Autopilot"))
        session.commit()

        session.add(Workflow(
            id="wf-1", name="Autopilot", status="active",
            phases_folder_path="/tmp/phases", definition_id="autopilot",
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
            id="feat-1", design_id="des-1", feature_key="auth",
            name="Auth", scope="s", status="active", workflow_id="wf-1",
        ))
        session.commit()
    finally:
        session.close()

    return db_manager


def _delete_feature_via_endpoint(feature_id="feat-1"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, headers={"X-Agent-ID": "system"})
    return client.delete(f"/api/autopilot/features/{feature_id}")


def test_delete_feature_deletes_agent_result_before_its_validation_review(tmp_path, monkeypatch):
    """agent_results.verified_by_validation_id is an enforced FK to
    validation_reviews.id, set by ResultService's normal task-validation
    flow for any validated task. delete_feature deleted ValidationReview
    (by task_id) before AgentResult (by task_id) -- same bug, same fix, as
    remove_project_design/delete_project/rerun's identical cascade."""
    db_manager = _seed_core(tmp_path, monkeypatch)

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

    resp = _delete_feature_via_endpoint()
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(AgentResult).filter_by(id="ar-1").first() is None
        assert session.query(ValidationReview).filter_by(id="vr-1").first() is None
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
    finally:
        session.close()


def test_delete_feature_nulls_workflow_result_id_before_deleting_workflow_result(tmp_path, monkeypatch):
    """workflows.result_id is an enforced FK to workflow_results.id.
    WorkflowResultService sets it to a WorkflowResult row whose OWN
    workflow_id is this same workflow's id -- a self-reference, ordinary
    behavior for e.g. a bugfix/diagnostic pipeline. delete_feature deleted
    WorkflowResult (by workflow_id) without first nulling this
    self-reference -- same bug, same fix, as remove_project_design/
    delete_project/rerun's identical cascade."""
    db_manager = _seed_core(tmp_path, monkeypatch)

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

    resp = _delete_feature_via_endpoint()
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(WorkflowResult).filter_by(id="res-1").first() is None
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
    finally:
        session.close()


def test_delete_feature_deletes_prompt_proposals_before_their_workflow(tmp_path, monkeypatch):
    """prompt_proposals.workflow_id is an enforced FK to workflows.id with
    no ondelete clause. forensics_analysis -- a real phase in the standard
    autopilot workflow -- creates one of these rows for every prompt
    improvement it proposes after a pipeline run finishes. delete_feature's
    cascade never touched prompt_proposals at all, so any feature whose
    workflow ever ran forensics_analysis and produced a proposal could not
    be deleted -- same bug, same fix, as remove_project_design/delete_
    project/rerun's identical cascade."""
    db_manager = _seed_core(tmp_path, monkeypatch)

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

    resp = _delete_feature_via_endpoint()
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(PromptProposal).filter_by(id="prop-1").first() is None
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
    finally:
        session.close()
