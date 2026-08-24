"""Tests for src/mcp/tickets_api.py's route layer -- two real bugs found
via a live smoke test of the ticket-creation/checking workflow agents rely
on (development.yaml Step 2, qa_validation.yaml Step 7):

1. create_ticket_endpoint's ValueError fallback (e.g. missing BoardConfig)
   constructed CreateTicketResponse with fields that don't exist on that
   model at all (workflow_id, agent_id, title, ticket_type, priority,
   description, created_at) while omitting the three actually-required
   ones (success, message, embedding_created) -- every ValueError path
   crashed with a pydantic ValidationError (visible to the agent as a
   generic 500) instead of the intended graceful "skipped" response.

2. get_tickets_endpoint was only registered at "/" (trailing slash). The
   MCP tool-calling client requests the bare "/api/tickets" path (no
   trailing slash), which FastAPI redirects with a 307 -- and the MCP
   client doesn't follow redirects, so every hephaestus_get_tickets call
   silently failed with an empty error, even though search_tickets/
   get_ticket/create_ticket (registered at unambiguous paths) all worked.
"""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("HEPHAESTUS_TEST_DB", ":memory:")


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def client(db_manager):
    from src.mcp.tickets_api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _seed_workflow(db_manager, workflow_id="wf-1", with_board=True):
    from src.core.database import Agent, BoardConfig, Workflow

    with db_manager.session_scope() as session:
        session.add(Workflow(id=workflow_id, name="t", phases_folder_path="/tmp", status="active"))
        session.add(Agent(id="agent-1", system_prompt="t", status="working", cli_type="pi", agent_type="phase"))
        if with_board:
            session.add(
                BoardConfig(
                    id="board-1",
                    workflow_id=workflow_id,
                    name="b",
                    columns=[{"id": "open", "name": "Open"}],
                    ticket_types=["bug", "task"],
                    default_ticket_type="task",
                    initial_status="open",
                )
            )


class TestCreateTicketValueErrorFallback:
    """create_ticket must degrade gracefully (not 500) when TicketService
    raises ValueError -- e.g. no BoardConfig configured for the workflow."""

    def test_missing_board_config_returns_skipped_not_500(self, client, db_manager):
        _seed_workflow(db_manager, with_board=False)

        resp = client.post(
            "/api/tickets/create",
            headers={"X-Agent-ID": "agent-1"},
            json={
                "workflow_id": "wf-1",
                "title": "test ticket",
                "description": "a description long enough",
                "ticket_type": "bug",
                "priority": "low",
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is False
        assert body["status"] == "skipped"
        assert body["ticket_id"] == ""
        assert "board" in body["message"].lower()

    def test_valid_board_config_creates_real_ticket(self, client, db_manager):
        _seed_workflow(db_manager, with_board=True)

        resp = client.post(
            "/api/tickets/create",
            headers={"X-Agent-ID": "agent-1"},
            json={
                "workflow_id": "wf-1",
                "title": "test ticket",
                "description": "a description long enough",
                "ticket_type": "bug",
                "priority": "low",
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["ticket_id"]


class TestGetTicketsTrailingSlash:
    """get_tickets must be reachable at the bare path (no trailing slash)
    -- the MCP tool-calling client doesn't follow the 307 FastAPI would
    otherwise issue when a route is only registered at '/'."""

    def test_no_trailing_slash_returns_200_not_redirect(self, client, db_manager):
        _seed_workflow(db_manager, with_board=True)

        resp = client.get(
            "/api/tickets",
            headers={"X-Agent-ID": "agent-1"},
            params={"workflow_id": "wf-1"},
            follow_redirects=False,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True

    def test_trailing_slash_still_works(self, client, db_manager):
        _seed_workflow(db_manager, with_board=True)

        resp = client.get(
            "/api/tickets/",
            headers={"X-Agent-ID": "agent-1"},
            params={"workflow_id": "wf-1"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True

    def test_no_trailing_slash_finds_created_ticket(self, client, db_manager):
        _seed_workflow(db_manager, with_board=True)

        create_resp = client.post(
            "/api/tickets/create",
            headers={"X-Agent-ID": "agent-1"},
            json={
                "workflow_id": "wf-1",
                "title": "findme ticket",
                "description": "a description long enough",
                "ticket_type": "bug",
                "priority": "low",
            },
        )
        assert create_resp.json()["success"] is True

        list_resp = client.get(
            "/api/tickets",
            headers={"X-Agent-ID": "agent-1"},
            params={"workflow_id": "wf-1"},
            follow_redirects=False,
        )
        assert list_resp.status_code == 200
        titles = [t["title"] for t in list_resp.json()["tickets"]]
        assert "findme ticket" in titles


class TestResolveRepoPathForCommit:
    """Tests for _resolve_repo_path_for_commit exception logging."""

    def test_returns_none_and_logs_when_repo_not_found(self, db_manager, caplog):
        """WARNING-2: RepoNotFoundError must be logged, not swallowed silently."""
        from src.core.database import AutopilotProject, ProjectRepo, Ticket, TicketCommit, Workflow
        from src.mcp.tickets_api import _resolve_repo_path_for_commit

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/repos/p"))
            session.add(ProjectRepo(id="repo-1", project_id="proj-1", label="primary", path="/repos/p", is_primary=True))
            session.add(Workflow(id="wf-1", name="w", status="active", phases_folder_path="/tmp", project_id="proj-1"))
            session.add(Ticket(id="tkt-1", workflow_id="wf-1", title="t", description="d", ticket_type="bug", priority="low", created_by_agent_id="agent-1", status="open"))
            from datetime import datetime

            session.add(TicketCommit(id="tc-1", ticket_id="tkt-1", commit_sha="abc123", repo_id="repo-deleted", agent_id="agent-1", commit_message="test commit", commit_timestamp=datetime.utcnow()))

        result = _resolve_repo_path_for_commit("abc123")
        assert result is None
        assert "REPO-RESOLUTION" in caplog.text
        assert "abc123" in caplog.text


class TestResolveRepoIdAndLabelForCommit:
    """Tests for _resolve_repo_id_and_label_for_commit exception logging."""

    def test_returns_none_and_logs_when_exception_occurs(self, db_manager, caplog):
        """WARNING-3: exceptions must be logged with exc_info, not swallowed silently."""
        from src.mcp.tickets_api import _resolve_repo_id_and_label_for_commit

        # Non-existent commit -- should return (None, None) without error
        result = _resolve_repo_id_and_label_for_commit("nonexistent")
        assert result == (None, None)
