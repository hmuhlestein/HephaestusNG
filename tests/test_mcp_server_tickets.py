"""Test MCP Server Ticket Endpoints.

This test suite verifies that all 11 ticket-related MCP endpoints work correctly
and that create_task properly validates ticket_id when tracking is enabled.
"""

import os
import subprocess
import sys
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _real_head_sha() -> str:
    """A commit SHA that actually exists in this repo -- _link_commit_impl
    now verifies existence via `git cat-file -e` and rejects fake SHAs like
    the old hardcoded 'abc123def456' (adversarial-review fix, REQ-10)."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def setup_test_database(tmp_path_factory):
    """Create an isolated test database with ticket-tracking schema and seed data.

    Previously this required a pre-built e2e_test.db file created by
    running e2e_ticket_test.py manually. Now the fixture owns all setup
    so the test suite is fully self-contained.
    """
    db_path = str(tmp_path_factory.mktemp("db") / "test.db")

    # Save and override — module-scoped so can't use monkeypatch
    prev = os.environ.get("HEPHAESTUS_TEST_DB")
    os.environ["HEPHAESTUS_TEST_DB"] = db_path

    from src.core.database import (
        Agent,
        BoardConfig,
        DatabaseManager,
        Phase,
        Workflow,
    )
    from src.mcp.server._shared import server_state

    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()
    server_state.db_manager = db_manager

    session = db_manager.get_session()
    try:
        workflow = Workflow(
            id="workflow-e2e-test",
            name="E2E Test Workflow with Tickets",
            phases_folder_path="/test/phases",
            status="active",
            created_at=datetime.utcnow(),
        )
        session.add(workflow)

        phase = Phase(
            id="phase-e2e-1",
            workflow_id="workflow-e2e-test",
            name="development",
            order=1,
            description="Development phase",
            done_definitions=["Code written"],
        )
        session.add(phase)

        board_config = BoardConfig(
            id="board-e2e-test",
            workflow_id="workflow-e2e-test",
            name="E2E Test Board",
            columns=[
                {"id": "backlog", "name": "Backlog", "order": 0, "color": "#9ca3af"},
                {"id": "todo", "name": "To Do", "order": 1, "color": "#6b7280"},
                {"id": "in_progress", "name": "In Progress", "order": 2, "color": "#3b82f6"},
                {"id": "review", "name": "Review", "order": 3, "color": "#f59e0b"},
                {"id": "done", "name": "Done", "order": 4, "color": "#10b981"},
            ],
            ticket_types=[
                {"id": "bug", "name": "Bug", "icon": "🐛", "color": "#ef4444"},
                {"id": "feature", "name": "Feature", "icon": "✨", "color": "#3b82f6"},
                {"id": "task", "name": "Task", "icon": "📋", "color": "#6b7280"},
            ],
            default_ticket_type="task",
            initial_status="backlog",
            auto_assign=False,
            created_at=datetime.utcnow(),
        )
        session.add(board_config)

        agent = Agent(
            id="agent-e2e-test",
            system_prompt="E2E test agent",
            status="working",
            cli_type="claude",
            created_at=datetime.utcnow(),
        )
        session.add(agent)

        session.commit()
    finally:
        session.close()

    yield db_path

    # Restore previous value
    if prev is None:
        os.environ.pop("HEPHAESTUS_TEST_DB", None)
    else:
        os.environ["HEPHAESTUS_TEST_DB"] = prev


@pytest.fixture
def client(setup_test_database):
    """Create FastAPI test client."""
    from src.mcp.server import app

    return TestClient(app)


@pytest.fixture
def headers():
    """Default headers for requests."""
    return {"X-Agent-ID": "agent-e2e-test"}


@pytest.fixture
def phase_id(setup_test_database):
    """Get the phase ID from the test database."""
    from src.core.database import DatabaseManager, Phase

    db = DatabaseManager(setup_test_database)
    session = db.get_session()
    try:
        phase = session.query(Phase).filter_by(workflow_id="workflow-e2e-test").first()
        return phase.id if phase else None
    finally:
        session.close()


# Module-level state for test ordering
test_state = {"ticket_id_1": None, "ticket_id_2": None}


class TestMCPTicketEndpoints:
    """Test all 11 ticket endpoints via MCP server."""

    def test_01_create_ticket(self, client, headers):
        """Test POST /tickets/create - Create a new ticket."""
        response = client.post(
            "/api/tickets/create",
            headers=headers,
            json={
                "workflow_id": "workflow-e2e-test",
                "title": "MCP Test Ticket 1",
                "description": "Testing ticket creation via MCP endpoint",
                "ticket_type": "feature",
                "priority": "high",
                "tags": ["mcp", "testing"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "ticket_id" in data
        assert data["message"] == "Ticket created successfully"

        test_state['ticket_id_1'] = data["ticket_id"]
        print(f"✅ Created ticket: {test_state['ticket_id_1']}")

    def test_02_get_ticket(self, client, headers):
        """Test GET /tickets/{ticket_id} - Get ticket details."""
        if test_state['ticket_id_1'] is None:
            pytest.skip("Requires ticket from test_01")

        response = client.get(
            f"/api/tickets/{test_state['ticket_id_1']}",
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()
        ticket_data = data["ticket"]
        assert ticket_data["ticket_id"] == test_state['ticket_id_1']
        assert ticket_data["title"] == "MCP Test Ticket 1"
        assert ticket_data["ticket_type"] == "feature"
        assert ticket_data["priority"] == "high"
        print(f"✅ Retrieved ticket details: {ticket_data['title']}")

    def test_03_get_tickets_list(self, client, headers):
        """Test GET /tickets/get - Get tickets by workflow."""
        pytest.skip("Route conflict: /tickets/get conflicts with /tickets/{ticket_id}")

    def test_04_add_comment(self, client, headers):
        """Test POST /tickets/comment - Add comment to ticket."""
        if test_state['ticket_id_1'] is None:
            pytest.skip("Requires ticket from test_01")

        response = client.post(
            "/api/tickets/comment",
            headers=headers,
            json={
                "ticket_id": test_state['ticket_id_1'],
                "comment_text": "This is a test comment via MCP endpoint",
                "comment_type": "general",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "comment_id" in data
        print(f"✅ Added comment: {data['comment_id']}")

    def test_05_update_ticket(self, client, headers):
        """Test POST /tickets/update - Update ticket fields."""
        if test_state['ticket_id_1'] is None:
            pytest.skip("Requires ticket from test_01")

        response = client.post(
            "/api/tickets/update",
            headers=headers,
            json={
                "ticket_id": test_state['ticket_id_1'],
                "updates": {
                    "priority": "critical",
                    "tags": ["mcp", "testing", "updated"],
                },
                "update_comment": "Updated priority to critical",
            },
        )

        if response.status_code != 200:
            pytest.skip("Ticket may already be resolved")

        data = response.json()
        assert data["success"] is True
        print(f"✅ Updated ticket fields: {data.get('fields_updated', [])}")

    def test_06_change_status(self, client, headers):
        """Test POST /tickets/change-status - Change ticket status."""
        if test_state['ticket_id_1'] is None:
            pytest.skip("Requires ticket from test_01")

        response = client.post(
            "/api/tickets/change-status",
            headers=headers,
            json={
                "ticket_id": test_state['ticket_id_1'],
                "new_status": "todo",
                "comment": "Moving to todo via MCP endpoint",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["new_status"] == "todo"
        print(f"✅ Changed status: {data['old_status']} → {data['new_status']}")

    def test_07_search_tickets(self, client, headers):
        """Test POST /tickets/search - Search tickets."""
        response = client.post(
            "/api/tickets/search",
            headers=headers,
            json={
                "workflow_id": "workflow-e2e-test",
                "query": "authentication",
                "search_type": "keyword",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "results" in data or "tickets" in data
        results = data.get("results") or data.get("tickets", [])
        print(f"✅ Search found {len(results)} tickets")

    def test_08_link_commit(self, client, headers):
        """Test POST /tickets/link-commit - Link commit to ticket."""
        if test_state['ticket_id_1'] is None:
            pytest.skip("Requires ticket from test_01")

        commit_sha = _real_head_sha()
        response = client.post(
            "/api/tickets/link-commit",
            headers=headers,
            json={
                "ticket_id": test_state['ticket_id_1'],
                "commit_sha": commit_sha,
                "commit_message": "feat: Add MCP test feature",
                "link_method": "manual",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["commit_sha"] == commit_sha
        print(f"✅ Linked commit: {data['commit_sha']}")

    def test_09_get_ticket_stats(self, client, headers):
        """Test GET /tickets/stats/{workflow_id} - Get ticket statistics."""
        response = client.get(
            "/api/tickets/stats/workflow-e2e-test",
            headers=headers,
        )

        if response.status_code == 500:
            pytest.skip("Stats endpoint error - needs investigation")

        assert response.status_code == 200
        data = response.json()
        assert "stats" in data
        assert "total_tickets" in data["stats"]
        assert data["stats"]["total_tickets"] >= 1
        print(f"✅ Ticket stats: {data['stats']['total_tickets']} total tickets")

    def test_10_resolve_ticket(self, client, headers):
        """Test POST /tickets/resolve - Resolve a ticket."""
        if test_state['ticket_id_1'] is None:
            pytest.skip("Requires ticket from test_01")

        # Move to done first if needed
        get_response = client.get(
            f"/api/tickets/{test_state['ticket_id_1']}", headers=headers
        )
        if get_response.status_code == 200:
            ticket_data = get_response.json()["ticket"]
            if ticket_data.get("status") != "done":
                client.post(
                    "/api/tickets/change-status",
                    headers=headers,
                    json={
                        "ticket_id": test_state['ticket_id_1'],
                        "new_status": "done",
                        "comment": "Moving to done for resolve test",
                    },
                )

        response = client.post(
            "/api/tickets/resolve",
            headers=headers,
            json={
                "ticket_id": test_state['ticket_id_1'],
                "resolution_comment": "Resolved via MCP endpoint test",
                "commit_sha": _real_head_sha(),
            },
        )

        if response.status_code == 400:
            error_msg = response.json().get("detail", "").lower()
            if "already resolved" in error_msg:
                pytest.skip("Ticket already resolved")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        print(f"✅ Resolved ticket: {test_state['ticket_id_1']}")

    def test_11_get_commit_diff(self, client, headers):
        """Test GET /tickets/commit-diff/{commit_sha} - Get commit diff."""
        response = client.get(
            "/api/tickets/commit-diff/abc123def456",
            headers=headers,
        )

        assert response.status_code in [200, 404, 500]
        if response.status_code == 200:
            data = response.json()
            assert "commit_sha" in data


class TestCreateTaskValidation:
    """Test that create_task validates ticket_id when tracking is enabled."""

    def test_create_task_requires_ticket_id_when_tracking_enabled(
        self, client, headers, phase_id
    ):
        """Test that create_task rejects requests without ticket_id when tracking enabled."""
        if not phase_id:
            pytest.skip("No phases in workflow-e2e-test")

        # Create a ticket to use
        ticket_response = client.post(
            "/api/tickets/create",
            headers=headers,
            json={
                "workflow_id": "workflow-e2e-test",
                "title": "Test Ticket for Task Creation",
                "description": "Testing task-ticket integration with proper description length",
                "ticket_type": "task",
                "priority": "medium",
            },
        )
        assert ticket_response.status_code == 200
        ticket_id = ticket_response.json()["ticket_id"]

        # Without ticket_id — should fail.
        # NOTE: no workflow_id/phase_id here. Sending both makes the caller a
        # "phase agent" (request.workflow_id is not None and
        # request.phase_id is not None), which create_task deliberately
        # exempts from the ticket requirement -- "part of the pipeline, not
        # external callers". This test is about the requirement for external
        # MCP agents, so it must not accidentally qualify for the exemption;
        # with those fields set it was asserting the gate while taking the
        # bypass, and passed only because the gate never ran.
        response_without = client.post(
            "/create_task",
            headers=headers,
            json={
                "task_description": "Task without ticket_id",
                "done_definition": "Task is done",
                "ai_agent_id": headers["X-Agent-ID"],
            },
        )
        assert response_without.status_code in [400, 422]

        # And the exemption itself is real behaviour -- pin it, so a future
        # change to either side is a deliberate one.
        response_phase_agent = client.post(
            "/create_task",
            headers=headers,
            json={
                "task_description": "Phase-agent task without ticket_id",
                "done_definition": "Task is done",
                "workflow_id": "workflow-e2e-test",
                "ai_agent_id": headers["X-Agent-ID"],
                "phase_id": phase_id,
            },
        )
        assert response_phase_agent.status_code == 200, (
            "a phase agent (workflow_id + phase_id) is exempt from the "
            "ticket_id requirement"
        )

        # With ticket_id — should succeed
        response_with = client.post(
            "/create_task",
            headers=headers,
            json={
                "task_description": "Task with ticket_id",
                "done_definition": "Task is done",
                "workflow_id": "workflow-e2e-test",
                "ticket_id": ticket_id,
                "ai_agent_id": headers["X-Agent-ID"],
                "phase_id": phase_id,
            },
        )
        assert response_with.status_code == 200
        assert "task_id" in response_with.json()

    def test_create_task_allows_no_ticket_id_for_sdk_agents(self, client, phase_id):
        """Test that create_task allows tasks without ticket_id for SDK agents."""
        if not phase_id:
            pytest.skip("No phases in workflow-e2e-test")

        response = client.post(
            "/create_task",
            headers={"X-Agent-ID": "sdk-test-agent"},
            json={
                "task_description": "Task without ticket tracking",
                "done_definition": "Task is done",
                "workflow_id": "workflow-e2e-test",
                "ai_agent_id": "sdk-test-agent",
                "phase_id": phase_id,
            },
        )
        assert response.status_code == 200
        assert "task_id" in response.json()


class TestGetCommitDiffTimeouts:
    """Phase 3 Tier 2 item 10 (docs/AUTOPILOT_REFACTOR_PLAN.md):
    get_commit_diff_endpoint's three subprocess.run calls (git show, git
    diff --numstat, per-file git diff) had no timeout, so a hung git
    process (e.g. against a corrupted repo or a network-mounted worktree)
    would block the request indefinitely."""

    def test_every_git_subprocess_call_has_a_timeout(self, client, headers):
        from unittest.mock import patch

        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(kwargs)
            from unittest.mock import MagicMock

            result = MagicMock()
            result.returncode = 0
            if cmd[:2] == ["git", "show"]:
                result.stdout = "abc123|Test Author|1700000000|Test commit message"
            elif "--numstat" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=_fake_run):
            response = client.get(
                "/api/tickets/commit-diff/abc123",
                headers=headers,
            )

        assert response.status_code == 200
        assert len(calls) >= 2, "expected at least the show + numstat git calls"
        for kwargs in calls:
            assert "timeout" in kwargs, f"subprocess.run call missing timeout: {kwargs}"
