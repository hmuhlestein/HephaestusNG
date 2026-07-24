"""Tests for the multi-project residual gap fixed after the broadcast
scoping round: a handful of call sites resolved "the project's repo" from
the process-wide config singleton (config.main_repo_path/project_root)
instead of the actual ticket/commit's own project -- fine when only one
project could ever be active, wrong once two are active simultaneously
and the singleton points at whichever was activated last.

Covers TicketService.link_commit (src/services/ticket_service.py) and
_resolve_repo_path_for_commit (src/mcp/tickets_api.py). The third fixed
site, TaskCompletionService.verify_output_artifact's feature_dir fallback,
is covered in tests/test_task_completion_service.py.
"""

import pytest

from src.core.database import (
    AutopilotProject,
    DatabaseManager,
    Ticket,
    TicketCommit,
    Workflow,
)


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed_project_workflow_ticket(db_manager, project_id="proj-a", base_dir="/tmp/proj-a", workflow_id="wf-1", ticket_id="ticket-1"):
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id=project_id, name=project_id, base_dir=base_dir))
        session.add(
            Workflow(id=workflow_id, name=workflow_id, status="active", project_id=project_id, phases_folder_path="/tmp")
        )
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
            )
        )


class TestLinkCommitUsesOwnProjectRepo:
    @pytest.mark.asyncio
    async def test_uses_tickets_own_project_base_dir(self, db_manager, monkeypatch):
        from src.services.ticket_service import TicketService

        _seed_project_workflow_ticket(db_manager, project_id="proj-a", base_dir="/repo/proj-a")

        captured = {}

        def fake_get_commit_stats(commit_sha, repo_path):
            captured["repo_path"] = repo_path
            return {"files_changed": 1, "insertions": 2, "deletions": 0, "files_list": ["f.py"]}

        monkeypatch.setattr(TicketService, "_get_commit_stats", staticmethod(fake_get_commit_stats))
        monkeypatch.setattr(
            "src.core.simple_config.get_config",
            lambda: type("Cfg", (), {"main_repo_path": "/repo/wrong-active-project"})(),
        )

        result = await TicketService.link_commit(
            ticket_id="ticket-1",
            agent_id="agent-1",
            commit_sha="abc123",
            commit_message="fix things",
        )

        assert result["success"] is True
        assert captured["repo_path"] == "/repo/proj-a"

    @pytest.mark.asyncio
    async def test_falls_back_to_singleton_when_ticket_has_no_project(self, db_manager, monkeypatch):
        from src.services.ticket_service import TicketService

        with db_manager.session_scope() as session:
            session.add(
                Workflow(id="wf-2", name="wf-2", status="active", phases_folder_path="/tmp")
            )
            session.add(
                Ticket(
                    id="ticket-2",
                    workflow_id="wf-2",
                    created_by_agent_id="agent-1",
                    title="t",
                    description="d",
                    ticket_type="task",
                    priority="medium",
                    status="open",
                )
            )

        captured = {}

        def fake_get_commit_stats(commit_sha, repo_path):
            captured["repo_path"] = repo_path
            return {"files_changed": 0, "insertions": 0, "deletions": 0, "files_list": []}

        monkeypatch.setattr(TicketService, "_get_commit_stats", staticmethod(fake_get_commit_stats))
        monkeypatch.setattr(
            "src.core.simple_config.get_config",
            lambda: type("Cfg", (), {"main_repo_path": "/repo/singleton-fallback"})(),
        )

        await TicketService.link_commit(
            ticket_id="ticket-2",
            agent_id="agent-1",
            commit_sha="def456",
            commit_message="fix things",
        )

        assert captured["repo_path"] == "/repo/singleton-fallback"


class TestResolveRepoPathForCommit:
    def test_resolves_via_linked_ticket(self, db_manager):
        from src.mcp.tickets_api import _resolve_repo_path_for_commit

        _seed_project_workflow_ticket(db_manager, project_id="proj-a", base_dir="/repo/proj-a")
        with db_manager.session_scope() as session:
            session.add(
                TicketCommit(
                    id="tc-1",
                    ticket_id="ticket-1",
                    agent_id="agent-1",
                    commit_sha="abc123",
                    commit_message="m",
                    commit_timestamp=__import__("datetime").datetime.utcnow(),
                )
            )

        assert _resolve_repo_path_for_commit("abc123") == "/repo/proj-a"

    def test_returns_none_when_commit_not_linked_to_any_ticket(self, db_manager):
        from src.mcp.tickets_api import _resolve_repo_path_for_commit

        assert _resolve_repo_path_for_commit("does-not-exist") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
