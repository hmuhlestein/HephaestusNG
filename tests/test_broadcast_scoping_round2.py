"""Tests for the second round of project-scoped broadcast fixes.

The first round (tests/test_project_broadcast_scoping.py) fixed
server.py, autopilot_api.py, and src/services/*.py. A follow-up grep
across the whole tree found broadcast_update call sites that round
missed entirely: src/mcp/tickets_api.py (partially -- some call sites
still unfixed), messaging_api.py, agents_api.py, and memory_api.py.
These tests exercise each fixed call site directly, verifying the
broadcast carries project_id/project_name resolved from the relevant
ticket/task/workflow instead of going out unscoped.
"""

import pytest

import src.mcp.server  # noqa: F401 -- import once here; importing it lazily

# inside a test (e.g. via monkeypatch.setattr("src.mcp.server.process_queue", ...))
# would run its module-level `_set_app_state(server_state)` and clobber the
# fake app state a test just installed.
from src.core.app_context import set_app_state
from src.core.database import (
    Agent,
    AutopilotProject,
    DatabaseManager,
    Task,
    Ticket,
    Workflow,
)


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


class FakeServerState:
    """Captures broadcast_update calls; stubs out the collaborators each
    fixed endpoint touches so it can run against a real (test) DB without
    a full ServerState (tmux, LLM client, vector store, etc.)."""

    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.broadcast_calls = []
        self.agent_manager = _StubAgentManager()
        self.llm_provider = _StubLLMProvider()

    async def broadcast_update(self, data, project_id=None, project_name=None):
        self.broadcast_calls.append(
            {"data": data, "project_id": project_id, "project_name": project_name}
        )


class _StubAgentManager:
    async def terminate_agent(self, agent_id):
        return True


class _StubLLMProvider:
    async def resolve_ticket_clarification(self, **kwargs):
        return "## Clarification\nDo the thing."


class FakeJSONRequest:
    """Stands in for starlette.Request -- reject/approve ticket endpoints
    only ever call `await request.json()`."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


@pytest.fixture
def fake_state(db_manager):
    """Install a fake app state, then RESTORE the previous one.

    Tearing down to None instead of the prior value leaks: _app_state is a
    module-level global in src/core/app_context.py, so every later test in
    the session that calls get_app_state() raises "App state not
    initialized". Measured before this fix: running this file immediately
    before tests/test_update_task_status_ordering.py turned 11 passing
    tests into 5 failures, and the same leak accounted for a large share of
    the suite's order-dependent failures.
    """
    import src.core.app_context as app_context

    previous = app_context._app_state
    state = FakeServerState(db_manager)
    set_app_state(state)
    try:
        yield state
    finally:
        app_context._app_state = previous


def _seed_project_workflow(db_manager, project_id="proj-a", project_name="Project A", workflow_id="wf-1"):
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id=project_id, name=project_name, base_dir=f"/tmp/{project_id}"))
        session.add(
            Workflow(id=workflow_id, name=workflow_id, status="active", project_id=project_id, phases_folder_path="/tmp")
        )


def _seed_ticket(db_manager, ticket_id, workflow_id, approval_status="pending_review"):
    with db_manager.session_scope() as session:
        session.add(
            Ticket(
                id=ticket_id,
                workflow_id=workflow_id,
                created_by_agent_id="agent-1",
                title="t",
                description="d",
                ticket_type="task",
                priority="medium",
                status="open",
                approval_status=approval_status,
            )
        )


class TestGetWorkflowIdForTicket:
    def test_resolves_workflow_id(self, db_manager):
        from src.mcp.tickets_api import _get_workflow_id_for_ticket

        _seed_project_workflow(db_manager)
        _seed_ticket(db_manager, "ticket-1", "wf-1")
        assert _get_workflow_id_for_ticket("ticket-1") == "wf-1"

    def test_missing_ticket_returns_none(self, db_manager):
        from src.mcp.tickets_api import _get_workflow_id_for_ticket

        assert _get_workflow_id_for_ticket("does-not-exist") is None


class TestRejectTicketBroadcast:
    @pytest.mark.asyncio
    async def test_reject_broadcasts_project_context(self, db_manager, fake_state):
        from src.mcp.tickets_api import reject_ticket_endpoint

        _seed_project_workflow(db_manager)
        _seed_ticket(db_manager, "ticket-1", "wf-1")

        request = FakeJSONRequest({"ticket_id": "ticket-1", "rejection_reason": "not needed"})
        response = await reject_ticket_endpoint(request=request, agent_id="ui-user")

        assert response.success is True
        assert len(fake_state.broadcast_calls) == 1
        call = fake_state.broadcast_calls[0]
        assert call["data"]["type"] == "ticket_rejected"
        assert call["project_id"] == "proj-a"
        assert call["project_name"] == "Project A"


class TestApproveTicketBroadcast:
    @pytest.mark.asyncio
    async def test_approve_broadcasts_project_context(self, db_manager, fake_state):
        from src.mcp.tickets_api import approve_ticket_endpoint

        _seed_project_workflow(db_manager)
        _seed_ticket(db_manager, "ticket-1", "wf-1")

        request = FakeJSONRequest({"ticket_id": "ticket-1"})
        response = await approve_ticket_endpoint(request=request, agent_id="ui-user")

        assert response.success is True
        assert len(fake_state.broadcast_calls) == 1
        call = fake_state.broadcast_calls[0]
        assert call["data"]["type"] == "ticket_approved"
        assert call["project_id"] == "proj-a"
        assert call["project_name"] == "Project A"


class TestTicketClarificationBroadcast:
    @pytest.mark.asyncio
    async def test_clarification_broadcasts_project_context(self, db_manager, fake_state, monkeypatch):
        from src.mcp.messaging_api import (
            RequestTicketClarificationRequest,
            request_ticket_clarification_endpoint,
        )
        from src.services.ticket_service import TicketService

        _seed_project_workflow(db_manager)
        _seed_ticket(db_manager, "ticket-1", "wf-1", approval_status="approved")

        async def fake_add_comment(**kwargs):
            return {"comment_id": "comment-1"}

        monkeypatch.setattr(TicketService, "add_comment", fake_add_comment)

        request = RequestTicketClarificationRequest(
            ticket_id="ticket-1",
            conflict_description="conflicting requirements need arbitration",
        )
        response = await request_ticket_clarification_endpoint(request=request, agent_id="agent-1")

        assert response.success is True
        assert len(fake_state.broadcast_calls) == 1
        call = fake_state.broadcast_calls[0]
        assert call["data"]["type"] == "ticket_clarification_requested"
        assert call["project_id"] == "proj-a"
        assert call["project_name"] == "Project A"


class TestTerminateAgentBroadcast:
    @pytest.mark.asyncio
    async def test_terminate_broadcasts_project_context(self, db_manager, fake_state, monkeypatch):
        from src.mcp.agents_api import terminate_agent_endpoint

        async def noop_process_queue():
            pass

        monkeypatch.setattr("src.mcp.server.process_queue", noop_process_queue)

        _seed_project_workflow(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Agent(id="agent-1", system_prompt="t", status="working", cli_type="pi", current_task_id="task-1")
            )
            session.add(
                Task(
                    id="task-1",
                    raw_description="d",
                    done_definition="done",
                    status="in_progress",
                    workflow_id="wf-1",
                    assigned_agent_id="agent-1",
                )
            )

        response = await terminate_agent_endpoint(agent_id="agent-1", reason="test termination")

        assert response["success"] is True
        assert len(fake_state.broadcast_calls) == 1
        call = fake_state.broadcast_calls[0]
        assert call["data"]["type"] == "agent_terminated_manually"
        assert call["project_id"] == "proj-a"
        assert call["project_name"] == "Project A"


class TestReportResultsBroadcast:
    @pytest.mark.asyncio
    async def test_report_results_broadcasts_project_context(self, db_manager, fake_state, monkeypatch):
        from src.mcp.memory_api import ReportResultsRequest, report_results
        from src.services.result_service import ResultService

        _seed_project_workflow(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-1",
                    raw_description="d",
                    done_definition="done",
                    status="in_progress",
                    workflow_id="wf-1",
                )
            )

        def fake_create_result(**kwargs):
            return {
                "status": "stored",
                "result_id": "result-1",
                "task_id": kwargs["task_id"],
                "agent_id": kwargs["agent_id"],
                "verification_status": "pending",
                "created_at": "2026-01-01T00:00:00",
            }

        monkeypatch.setattr(ResultService, "create_result", fake_create_result)

        request = ReportResultsRequest(
            task_id="task-1",
            markdown_file_path="/tmp/result.md",
            result_type="implementation",
            summary="did the thing",
        )
        response = await report_results(request=request, agent_id="agent-1")

        assert response.status == "stored"
        assert len(fake_state.broadcast_calls) == 1
        call = fake_state.broadcast_calls[0]
        assert call["data"]["type"] == "results_reported"
        assert call["project_id"] == "proj-a"
        assert call["project_name"] == "Project A"


class TestGiveValidationReviewBroadcast:
    @pytest.mark.asyncio
    async def test_validation_passed_broadcasts_project_context(self, db_manager, fake_state, monkeypatch):
        from src.mcp.memory_api import GiveValidationReviewRequest, give_validation_review

        async def noop_process_queue():
            pass

        monkeypatch.setattr("src.mcp.server.process_queue", noop_process_queue)

        _seed_project_workflow(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Agent(id="validator-1", system_prompt="t", status="working", cli_type="pi", agent_type="validator")
            )
            session.add(
                Task(
                    id="task-1",
                    raw_description="d",
                    done_definition="done",
                    status="under_review",
                    workflow_id="wf-1",
                    assigned_agent_id="agent-1",
                    validation_iteration=1,
                )
            )

        request = GiveValidationReviewRequest(
            task_id="task-1",
            validator_agent_id="validator-1",
            validation_passed=True,
            feedback="looks good",
        )
        response = await give_validation_review(request=request, agent_id="validator-1")

        assert response.status == "completed"
        assert len(fake_state.broadcast_calls) == 1
        call = fake_state.broadcast_calls[0]
        assert call["data"]["type"] == "validation_passed"
        assert call["project_id"] == "proj-a"
        assert call["project_name"] == "Project A"


class TestGiveValidationReviewClearsStaleFailureReason:
    """Regression, same class of bug as server.py's update_task_status:
    a task validated as passed after an earlier failed attempt (goto/
    retry reuses the same task row) kept that attempt's failure_reason
    forever, feeding the "done but has failure_reason" self-heal that
    wrongly resets genuinely-completed tasks back to failed."""

    @pytest.mark.asyncio
    async def test_success_clears_prior_failure_reason(self, db_manager, fake_state, monkeypatch):
        from src.mcp.memory_api import GiveValidationReviewRequest, give_validation_review

        async def noop_process_queue():
            pass

        monkeypatch.setattr("src.mcp.server.process_queue", noop_process_queue)

        _seed_project_workflow(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Agent(id="validator-1", system_prompt="t", status="working", cli_type="pi", agent_type="validator")
            )
            session.add(
                Task(
                    id="task-1",
                    raw_description="d",
                    done_definition="done",
                    status="under_review",
                    workflow_id="wf-1",
                    assigned_agent_id="agent-1",
                    validation_iteration=1,
                    failure_reason="earlier attempt: output validation failed",
                )
            )

        request = GiveValidationReviewRequest(
            task_id="task-1",
            validator_agent_id="validator-1",
            validation_passed=True,
            feedback="looks good",
        )
        await give_validation_review(request=request, agent_id="validator-1")

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "done"
            assert task.failure_reason is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
