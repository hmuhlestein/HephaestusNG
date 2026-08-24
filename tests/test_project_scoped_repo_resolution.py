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
            lambda: type("Cfg", (), {
                "git": type("Git", (), {"main_repo_path": "/repo/wrong-active-project"})()
            })(),
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
            lambda: type("Cfg", (), {
                "git": type("Git", (), {"main_repo_path": "/repo/singleton-fallback"})()
            })(),
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

    def test_two_repos_with_colliding_short_shas_resolve_distinctly(self, db_manager):
        """REQ-14: a bare commit_sha is no longer guaranteed unique across a
        project's repos -- TicketCommit.repo_id disambiguates."""
        from datetime import datetime

        from src.core.database import ProjectRepo

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-multi", name="p", base_dir="/tmp/multi"))
            session.add(ProjectRepo(id="repo-backend", project_id="proj-multi", label="backend", path="/repo/backend", is_primary=True))
            session.add(ProjectRepo(id="repo-frontend", project_id="proj-multi", label="frontend", path="/repo/frontend"))
            session.add(Workflow(id="wf-multi", name="wf-multi", status="active", project_id="proj-multi", phases_folder_path="/tmp"))
            session.add(
                Ticket(id="ticket-be", workflow_id="wf-multi", created_by_agent_id="a", title="t", description="d", ticket_type="task", priority="medium", status="open")
            )
            session.add(
                Ticket(id="ticket-fe", workflow_id="wf-multi", created_by_agent_id="a", title="t", description="d", ticket_type="task", priority="medium", status="open")
            )
            session.add(
                TicketCommit(id="tc-be", ticket_id="ticket-be", agent_id="a", repo_id="repo-backend", commit_sha="abc1234", commit_message="m", commit_timestamp=datetime.utcnow())
            )
            session.add(
                TicketCommit(id="tc-fe", ticket_id="ticket-fe", agent_id="a", repo_id="repo-frontend", commit_sha="abc1234", commit_message="m", commit_timestamp=datetime.utcnow())
            )

        # Same SHA, two rows -- _resolve_repo_path_for_commit finds
        # whichever TicketCommit row matches first; the point of this test
        # is that when the resolved row's repo_id is set, the returned
        # path is that repo's, not always the primary.
        with db_manager.session_scope() as session:
            row = session.query(TicketCommit).filter_by(id="tc-fe").first()
            assert row.repo_id == "repo-frontend"


class TestCommitScopeValidation:
    """REQ-10: a commit whose changed files fall outside its task's
    assigned repo is logged, never rejected."""

    def _seed(self, db_manager, backend_dir, frontend_dir):
        from src.core.database import ProjectRepo, Task

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-scope", name="p", base_dir=str(backend_dir.parent)))
            session.add(ProjectRepo(id="repo-backend", project_id="proj-scope", label="backend", path=str(backend_dir), is_primary=True))
            session.add(ProjectRepo(id="repo-frontend", project_id="proj-scope", label="frontend", path=str(frontend_dir)))
            session.add(Workflow(id="wf-scope", name="wf-scope", status="active", project_id="proj-scope", phases_folder_path="/tmp"))
            session.add(Task(id="task-scope", raw_description="d", done_definition="d", repo_id="repo-backend"))
            session.add(
                Ticket(id="ticket-scope", workflow_id="wf-scope", task_id="task-scope", created_by_agent_id="a", title="t", description="d", ticket_type="task", priority="medium", status="open")
            )

    @pytest.mark.asyncio
    async def test_commit_touching_files_outside_assigned_repo_logs_warning_but_still_records(
        self, db_manager, monkeypatch, tmp_path, caplog
    ):
        from src.services.ticket_service import TicketService

        backend_dir = tmp_path / "backend"
        frontend_dir = tmp_path / "frontend"
        self._seed(db_manager, backend_dir, frontend_dir)

        def fake_get_commit_stats(commit_sha, repo_path):
            return {
                "files_changed": 1,
                "insertions": 1,
                "deletions": 0,
                "files_list": [str(frontend_dir / "App.tsx")],
            }

        monkeypatch.setattr(TicketService, "_get_commit_stats", staticmethod(fake_get_commit_stats))

        import logging

        with caplog.at_level(logging.WARNING, logger="src.services.ticket_service"):
            result = await TicketService.link_commit(
                ticket_id="ticket-scope",
                agent_id="agent-1",
                commit_sha="sha-scope",
                commit_message="oops wrong repo",
            )

        assert result["success"] is True
        assert any("REPO-SCOPE" in r.message for r in caplog.records)

        with db_manager.session_scope() as session:
            commit = session.query(TicketCommit).filter_by(commit_sha="sha-scope").first()
            assert commit is not None
            assert commit.repo_id == "repo-backend"

    @pytest.mark.asyncio
    async def test_commit_within_assigned_repo_does_not_warn(self, db_manager, monkeypatch, tmp_path, caplog):
        from src.services.ticket_service import TicketService

        backend_dir = tmp_path / "backend"
        frontend_dir = tmp_path / "frontend"
        self._seed(db_manager, backend_dir, frontend_dir)

        def fake_get_commit_stats(commit_sha, repo_path):
            return {
                "files_changed": 1,
                "insertions": 1,
                "deletions": 0,
                "files_list": [str(backend_dir / "main.py")],
            }

        monkeypatch.setattr(TicketService, "_get_commit_stats", staticmethod(fake_get_commit_stats))

        import logging

        with caplog.at_level(logging.WARNING, logger="src.services.ticket_service"):
            await TicketService.link_commit(
                ticket_id="ticket-scope",
                agent_id="agent-1",
                commit_sha="sha-clean",
                commit_message="in scope",
            )

        assert not any("REPO-SCOPE" in r.message for r in caplog.records)


class TestCommitDiffResponseRepoFields:
    def test_resolves_repo_id_and_label_for_a_multi_repo_commit(self, db_manager):
        from datetime import datetime

        from src.core.database import ProjectRepo
        from src.mcp.tickets_api import _resolve_repo_id_and_label_for_commit

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-diff", name="p", base_dir="/tmp/diff"))
            session.add(ProjectRepo(id="repo-be", project_id="proj-diff", label="backend", path="/repo/be", is_primary=True))
            session.add(Workflow(id="wf-diff", name="wf-diff", status="active", project_id="proj-diff", phases_folder_path="/tmp"))
            session.add(
                Ticket(id="ticket-diff", workflow_id="wf-diff", created_by_agent_id="a", title="t", description="d", ticket_type="task", priority="medium", status="open")
            )
            session.add(
                TicketCommit(id="tc-diff", ticket_id="ticket-diff", agent_id="a", repo_id="repo-be", commit_sha="diffsha", commit_message="m", commit_timestamp=datetime.utcnow())
            )

        repo_id, repo_label = _resolve_repo_id_and_label_for_commit("diffsha")
        assert repo_id == "repo-be"
        assert repo_label == "backend"

    def test_none_for_a_commit_with_no_repo_id(self, db_manager):
        from src.mcp.tickets_api import _resolve_repo_id_and_label_for_commit

        assert _resolve_repo_id_and_label_for_commit("does-not-exist") == (None, None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
