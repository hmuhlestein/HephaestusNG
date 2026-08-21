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
    ProjectRepo,
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
        # Post-migration, every project has a primary ProjectRepo backfilled
        # from base_dir -- seed it here so resolve_repo() (which reads only
        # ProjectRepo, not AutopilotProject.base_dir) has something to find.
        session.add(
            ProjectRepo(
                id=f"{project_id}-primary-repo",
                project_id=project_id,
                label="main",
                path=base_dir,
                is_primary=True,
            )
        )
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

    @pytest.mark.asyncio
    async def test_resolves_repo_exactly_once(self, db_manager, monkeypatch):
        """Adversarial review BLOCKER #3: link_commit previously called
        resolve_repo() twice (once for main_repo_path, once for
        resolved_repo_id) -- a TOCTOU window where the two calls could
        disagree. Must resolve once and reuse the result for both."""
        from src.services.ticket_service import TicketService

        _seed_project_workflow_ticket(
            db_manager, project_id="proj-b", base_dir="/repo/proj-b",
            workflow_id="wf-b", ticket_id="ticket-b",
        )

        monkeypatch.setattr(
            TicketService, "_get_commit_stats",
            staticmethod(lambda commit_sha, repo_path: {
                "files_changed": 0, "insertions": 0, "deletions": 0, "files_list": [],
            }),
        )

        import src.core.repo_resolution as repo_resolution_module

        call_count = {"n": 0}
        real_resolve_repo = repo_resolution_module.resolve_repo

        def counting_resolve_repo(*args, **kwargs):
            call_count["n"] += 1
            return real_resolve_repo(*args, **kwargs)

        monkeypatch.setattr(repo_resolution_module, "resolve_repo", counting_resolve_repo)

        result = await TicketService.link_commit(
            ticket_id="ticket-b",
            agent_id="agent-1",
            commit_sha="abc999",
            commit_message="fix things",
        )

        assert result["success"] is True
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_survives_git_cat_file_timeout(self, db_manager, monkeypatch):
        """Adversarial review WARNING (run 3): the soft commit-existence
        check's `except OSError` didn't catch subprocess.TimeoutExpired
        (a SubprocessError, not an OSError) -- a hung/wedged git process
        crashed the link instead of soft-failing per the check's own
        "must not crash the link" docstring."""
        import subprocess

        from src.services.ticket_service import TicketService

        _seed_project_workflow_ticket(
            db_manager, project_id="proj-c", base_dir="/repo/proj-c",
            workflow_id="wf-c", ticket_id="ticket-c",
        )

        monkeypatch.setattr(
            TicketService, "_get_commit_stats",
            staticmethod(lambda commit_sha, repo_path: {
                "files_changed": 0, "insertions": 0, "deletions": 0, "files_list": [],
            }),
        )

        def timing_out_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git cat-file -e", timeout=5)

        monkeypatch.setattr(subprocess, "run", timing_out_run)

        result = await TicketService.link_commit(
            ticket_id="ticket-c",
            agent_id="agent-1",
            commit_sha="abc999",
            commit_message="fix things",
        )

        assert result["success"] is True


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

    def test_logs_warning_on_db_failure_instead_of_swallowing_silently(
        self, db_manager, monkeypatch, caplog
    ):
        """Adversarial review WARNING: a DB failure inside
        _resolve_repo_path_for_commit used to be swallowed with no log,
        making it indistinguishable from a commit that simply isn't
        linked to any ticket. Must log at WARNING level before
        returning None."""
        import logging

        from src.mcp import tickets_api

        def raise_get_db():
            raise RuntimeError("simulated DB failure")

        monkeypatch.setattr("src.core.database.get_db", raise_get_db)

        with caplog.at_level(logging.WARNING, logger=tickets_api.logger.name):
            result = tickets_api._resolve_repo_path_for_commit("abc123")

        assert result is None
        assert any(
            "abc123" in record.message and "simulated DB failure" in record.message
            for record in caplog.records
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
