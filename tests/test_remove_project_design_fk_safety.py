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
    Agent,
    AgentResult,
    AutopilotDesign,
    AutopilotProject,
    BoardConfig,
    CostEntry,
    DatabaseManager,
    DiagnosticRun,
    Feature,
    Memory,
    Phase,
    PhaseExecution,
    PhasePromptVersion,
    PromptProposal,
    Task,
    TaskPromptOverride,
    Ticket,
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

    resp = client.delete("/api/autopilot/projects/proj-1/designs/des-1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"removed": "des-1"}

    session = db_manager.get_session()
    try:
        # If the FK violation still occurs, the delete cascade's own outer
        # except catches it and these rows survive.
        assert session.query(Workflow).filter_by(id="wf-old").first() is None
        assert session.query(Feature).filter_by(id="feat-1").first() is None
        assert session.query(AutopilotDesign).filter_by(id="des-1").first() is None
    finally:
        session.close()


def _seed_core(tmp_path, monkeypatch, design_filename="01-design.md"):
    """Build AutopilotProject "proj-1" / AutopilotDesign "des-1" /
    WorkflowDefinition "autopilot" / Workflow "wf-1" (design_id="des-1") /
    Phase "phase-1" / Task "task-1", committed one at a time in
    FK-dependency order -- same reasoning as the ordering comment in the
    test above (real FK enforcement forced on, and SQLAlchemy's automatic
    insert-ordering within a single flush isn't reliable for this
    up-then-down-then-up chain of FKs).

    Returns the DatabaseManager; callers add whatever extra rows their
    scenario needs on top of this shared base.
    """
    project_dir = tmp_path / "myproject"
    design_dir = project_dir / ".hephaestus" / "specs"
    design_dir.mkdir(parents=True)
    (design_dir / design_filename).write_text("# Design\nSomething.")

    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()
    _force_fk_enforcement(db_manager.engine)

    session = db_manager.get_session()
    try:
        session.add(AutopilotProject(id="proj-1", name="myproject", base_dir=str(project_dir)))
        session.commit()

        session.add(AutopilotDesign(
            id="des-1", project_id="proj-1", filename=design_filename, name="design",
            ordinal=1, extension=".md",
        ))
        session.commit()

        session.add(WorkflowDefinition(id="autopilot", name="Autopilot"))
        session.commit()

        session.add(Workflow(
            id="wf-1", name="Autopilot", status="active",
            phases_folder_path="/tmp/phases", definition_id="autopilot",
            design_id="des-1",
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

    return db_manager


def _delete_design_via_endpoint(project_id="proj-1", design_id="des-1"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, headers={"X-Agent-ID": "system"})
    return client.delete(f"/api/autopilot/projects/{project_id}/designs/{design_id}")


def test_remove_project_design_deletes_agent_result_before_its_validation_review(tmp_path, monkeypatch):
    """agent_results.verified_by_validation_id is an enforced FK to
    validation_reviews.id, set by ResultService's normal task-validation
    flow (src/services/result_service.py) whenever an agent's result gets
    verified against the ValidationReview that approved it -- ordinary
    behavior for any task that went through validation. The cascade
    deleted ValidationReview (by task_id) before AgentResult (by
    task_id): same bug class as the Feature/Workflow ordering above, one
    step further down the chain, invisible to the single scenario the
    existing test covers."""
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

    resp = _delete_design_via_endpoint()
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(AgentResult).filter_by(id="ar-1").first() is None
        assert session.query(ValidationReview).filter_by(id="vr-1").first() is None
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
    finally:
        session.close()


def test_remove_project_design_nulls_workflow_result_id_before_deleting_workflow_result(tmp_path, monkeypatch):
    """workflows.result_id is an enforced FK to workflow_results.id.
    WorkflowResultService._update_result_status_impl (src/services/
    workflow_result_service.py) sets it to a WorkflowResult row whose OWN
    workflow_id is this same workflow's id -- a has_result workflow
    pointing at its own accepted result, ordinary behavior for e.g. a
    bugfix/diagnostic pipeline. The cascade deleted WorkflowResult (by
    workflow_id) without first nulling this self-reference, so the delete
    failed the same way an un-nulled Feature/Workflow pointer did before
    the original fix."""
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

    resp = _delete_design_via_endpoint()
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(WorkflowResult).filter_by(id="res-1").first() is None
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
    finally:
        session.close()


def test_remove_project_design_nulls_phase0_workflow_id_before_deleting_workflow(tmp_path, monkeypatch):
    """autopilot_designs.phase0_workflow_id is an enforced FK to
    workflows.id, persisted immediately after Phase 0 launches
    (src/autopilot/orchestrator/pipeline.py). remove_project_design's own
    design row `d` still exists (deleted last, after Workflow) when the
    Workflow delete runs -- if `d.phase0_workflow_id` still points at one
    of the workflows being deleted, that delete fails. delete_project
    (src/mcp/autopilot/project_routes.py) already nulls this out for its
    own bulk-delete cascade over the same tables; this single-design
    endpoint never got the matching fix."""
    db_manager = _seed_core(tmp_path, monkeypatch)

    session = db_manager.get_session()
    try:
        d = session.query(AutopilotDesign).filter_by(id="des-1").first()
        d.phase0_workflow_id = "wf-1"
        session.commit()
    finally:
        session.close()

    resp = _delete_design_via_endpoint()
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
        assert session.query(AutopilotDesign).filter_by(id="des-1").first() is None
    finally:
        session.close()


def test_remove_project_design_deletes_prompt_proposals_before_their_workflow(tmp_path, monkeypatch):
    """prompt_proposals.workflow_id is an enforced FK to workflows.id with
    no ondelete clause. forensics_analysis -- a real phase in the
    standard autopilot workflow (config/workflows/autopilot/
    workflow.yaml) -- creates one of these rows for every prompt
    improvement it proposes after a pipeline run finishes. Unlike the
    other cases here, this isn't a wrong-ORDER bug: the cascade never
    touched prompt_proposals at all, so any design whose workflow ever
    ran forensics_analysis and produced a proposal could not be deleted."""
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

    resp = _delete_design_via_endpoint()
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        assert session.query(PromptProposal).filter_by(id="prop-1").first() is None
        assert session.query(Workflow).filter_by(id="wf-1").first() is None
    finally:
        session.close()


def test_remove_project_design_full_pipeline_smoke(tmp_path, monkeypatch):
    """Broad regression: seed one row in essentially every table this
    endpoint's cascade touches (or should touch) -- two agents, a
    feature, a second task, a validation review + verified agent result,
    a memory row, a diagnostic run, a board config, a ticket, a cost
    entry, a phase execution, a phase prompt version, a task prompt
    override, a workflow result (self-referenced via Workflow.result_id),
    a phase0_workflow_id link, and a prompt proposal -- approximating a
    design that actually completed a full autopilot run. The other tests
    in this file each isolate one specific FK-ordering bug; this one
    checks the "everything at once, in a realistic shape" case the
    original single-scenario test never attempted."""
    db_manager = _seed_core(tmp_path, monkeypatch)

    session = db_manager.get_session()
    try:
        session.add(Agent(id="agent-1", system_prompt="p", cli_type="claude"))
        session.add(Agent(id="agent-2", system_prompt="p", cli_type="claude"))
        session.commit()

        session.add(Feature(
            id="feat-1", design_id="des-1", feature_key="auth",
            name="Auth", scope="s", status="active", workflow_id="wf-1",
        ))
        session.commit()

        session.add(Task(
            id="task-2", workflow_id="wf-1", phase_id="phase-1",
            raw_description="r2", done_definition="d2", status="done",
            assigned_agent_id="agent-1",
        ))
        session.commit()

        session.add(ValidationReview(
            id="vr-1", task_id="task-1", validator_agent_id="agent-2",
            iteration_number=1, validation_passed=True, feedback="ok",
        ))
        session.commit()

        session.add(AgentResult(
            id="ar-1", agent_id="agent-1", task_id="task-1",
            markdown_content="c", markdown_file_path="/tmp/r.md",
            result_type="implementation", summary="s",
            verified_by_validation_id="vr-1",
        ))
        session.commit()

        session.add(Memory(
            id="mem-1", agent_id="agent-1", content="learned something",
            memory_type="discovery", related_task_id="task-1",
        ))
        session.commit()

        session.add(DiagnosticRun(
            id="diag-1", workflow_id="wf-1", diagnostic_agent_id="agent-2",
            diagnostic_task_id="task-1", total_tasks_at_trigger=2,
            done_tasks_at_trigger=1, failed_tasks_at_trigger=0,
            time_since_last_task_seconds=300,
        ))
        session.commit()

        session.add(BoardConfig(
            id="board-1", workflow_id="wf-1", name="Board",
            columns=[{"id": "todo", "name": "To Do", "order": 0}],
            ticket_types=["bug", "feature"], initial_status="todo",
        ))
        session.commit()

        session.add(Ticket(
            id="ticket-1", workflow_id="wf-1", created_by_agent_id="agent-1",
            title="t", description="d", ticket_type="bug",
            priority="medium", status="todo", task_id="task-1",
            phase_id="phase-1",
        ))
        session.commit()

        session.add(CostEntry(
            id="cost-1", task_id="task-1", agent_id="agent-1",
            workflow_id="wf-1", source="claude_code", cost_usd=0.05,
        ))
        session.commit()

        session.add(PhaseExecution(id="pe-1", phase_id="phase-1", status="completed"))
        session.commit()

        session.add(PhasePromptVersion(id="ppv-1", phase_id="phase-1", version=1, status="active"))
        session.commit()

        session.add(TaskPromptOverride(task_id="task-1", system_prompt="custom"))
        session.commit()

        session.add(WorkflowResult(
            id="res-1", workflow_id="wf-1", agent_id="agent-1",
            result_file_path="/tmp/result.md", result_content="content",
            status="validated",
        ))
        session.commit()

        session.add(PromptProposal(
            id="prop-1", workflow_id="wf-1", phase_name="product_requirements",
            field="description", proposed_value="better description",
            rationale="observed confusion in agent transcript",
        ))
        session.commit()

        wf = session.query(Workflow).filter_by(id="wf-1").first()
        wf.result_found = True
        wf.result_id = "res-1"
        d = session.query(AutopilotDesign).filter_by(id="des-1").first()
        d.phase0_workflow_id = "wf-1"
        session.commit()
    finally:
        session.close()

    resp = _delete_design_via_endpoint()
    assert resp.status_code == 200, resp.text

    session = db_manager.get_session()
    try:
        for model, id_ in [
            (Feature, "feat-1"), (Task, "task-1"), (Task, "task-2"),
            (ValidationReview, "vr-1"), (AgentResult, "ar-1"), (Memory, "mem-1"),
            (DiagnosticRun, "diag-1"), (BoardConfig, "board-1"), (Ticket, "ticket-1"),
            (CostEntry, "cost-1"), (PhaseExecution, "pe-1"), (PhasePromptVersion, "ppv-1"),
            (WorkflowResult, "res-1"), (PromptProposal, "prop-1"),
            (Phase, "phase-1"), (Workflow, "wf-1"), (AutopilotDesign, "des-1"),
        ]:
            assert session.query(model).filter_by(id=id_).first() is None, f"{model.__name__} {id_} survived"
        assert session.query(TaskPromptOverride).filter_by(task_id="task-1").first() is None
        # Agents are never deleted by this cascade -- only terminated --
        # so they should still be here.
        assert session.query(Agent).filter_by(id="agent-1").first() is not None
        assert session.query(Agent).filter_by(id="agent-2").first() is not None
    finally:
        session.close()
