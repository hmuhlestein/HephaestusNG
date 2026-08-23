"""add/list ProjectRepo endpoints (no update/delete in v1) -- REQ-24."""

import asyncio

import pytest
from fastapi import HTTPException

from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    import src.mcp.autopilot.project_repo_routes as routes

    async def _ok(agent_id):
        return True

    monkeypatch.setattr(routes, "verify_agent_authentication", _ok)


class TestAddProjectRepo:
    @pytest.mark.asyncio
    async def test_rejects_non_directory_path_before_creating_any_row(self, db_manager, tmp_path):
        from src.mcp.autopilot.project_repo_routes import ProjectRepoCreate, add_project_repo

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))

        with pytest.raises(HTTPException) as exc_info:
            await add_project_repo("proj-1", ProjectRepoCreate(label="backend", path=str(tmp_path / "does-not-exist")))
        assert exc_info.value.status_code == 400

        with db_manager.session_scope() as session:
            assert session.query(ProjectRepo).count() == 0

    @pytest.mark.asyncio
    async def test_rejects_directory_missing_dot_git(self, db_manager, tmp_path):
        from src.mcp.autopilot.project_repo_routes import ProjectRepoCreate, add_project_repo

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()

        with pytest.raises(HTTPException) as exc_info:
            await add_project_repo("proj-1", ProjectRepoCreate(label="backend", path=str(not_a_repo)))
        assert exc_info.value.status_code == 400
        assert "not a git repository" in exc_info.value.detail

        with db_manager.session_scope() as session:
            assert session.query(ProjectRepo).count() == 0

    @pytest.mark.asyncio
    async def test_first_repo_added_is_primary(self, db_manager, tmp_path):
        from src.mcp.autopilot.project_repo_routes import ProjectRepoCreate, add_project_repo

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / ".git").mkdir()

        result = await add_project_repo("proj-1", ProjectRepoCreate(label="backend", path=str(backend)))
        assert result.is_primary is True

    @pytest.mark.asyncio
    async def test_second_repo_added_is_not_primary(self, db_manager, tmp_path):
        from src.mcp.autopilot.project_repo_routes import ProjectRepoCreate, add_project_repo

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        backend = tmp_path / "backend"
        frontend = tmp_path / "frontend"
        for d in (backend, frontend):
            d.mkdir()
            (d / ".git").mkdir()

        await add_project_repo("proj-1", ProjectRepoCreate(label="backend", path=str(backend)))
        second = await add_project_repo("proj-1", ProjectRepoCreate(label="frontend", path=str(frontend)))
        assert second.is_primary is False

    @pytest.mark.asyncio
    async def test_duplicate_label_surfaces_as_409_not_a_raw_500(self, db_manager, tmp_path):
        from src.mcp.autopilot.project_repo_routes import ProjectRepoCreate, add_project_repo

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        backend = tmp_path / "backend"
        backend2 = tmp_path / "backend2"
        for d in (backend, backend2):
            d.mkdir()
            (d / ".git").mkdir()

        await add_project_repo("proj-1", ProjectRepoCreate(label="backend", path=str(backend)))
        with pytest.raises(HTTPException) as exc_info:
            await add_project_repo("proj-1", ProjectRepoCreate(label="backend", path=str(backend2)))
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_unknown_project_404s(self, db_manager, tmp_path):
        from src.mcp.autopilot.project_repo_routes import ProjectRepoCreate, add_project_repo

        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / ".git").mkdir()

        with pytest.raises(HTTPException) as exc_info:
            await add_project_repo("proj-does-not-exist", ProjectRepoCreate(label="backend", path=str(backend)))
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_unauthenticated_request_401s(self, db_manager, tmp_path, monkeypatch):
        from src.mcp.autopilot import project_repo_routes as routes

        async def _deny(agent_id):
            return False

        monkeypatch.setattr(routes, "verify_agent_authentication", _deny)

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / ".git").mkdir()

        with pytest.raises(HTTPException) as exc_info:
            await routes.add_project_repo("proj-1", routes.ProjectRepoCreate(label="backend", path=str(backend)))
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_concurrent_first_add_only_yields_one_primary(self, db_manager, tmp_path):
        """BLOCKER fix (REQ-01..06 concurrency): two concurrent 'add first
        repo' calls for a project with 0 existing repos must not both
        become primary. The in-process per-project lock in add_project_repo
        serializes the check-then-insert so the second caller correctly
        observes count()==1 and inserts non-primary."""
        from src.mcp.autopilot.project_repo_routes import ProjectRepoCreate, add_project_repo

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-race", name="p", base_dir=str(tmp_path)))
        backend = tmp_path / "backend"
        frontend = tmp_path / "frontend"
        for d in (backend, frontend):
            d.mkdir()
            (d / ".git").mkdir()

        results = await asyncio.gather(
            add_project_repo("proj-race", ProjectRepoCreate(label="backend", path=str(backend))),
            add_project_repo("proj-race", ProjectRepoCreate(label="frontend", path=str(frontend))),
        )

        primaries = [r for r in results if r.is_primary]
        assert len(primaries) == 1

        with db_manager.session_scope() as session:
            assert session.query(ProjectRepo).filter_by(project_id="proj-race", is_primary=True).count() == 1


class TestListProjectRepos:
    @pytest.mark.asyncio
    async def test_lists_all_repos_for_a_project(self, db_manager, tmp_path):
        from src.mcp.autopilot.project_repo_routes import list_project_repos

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
            session.add(ProjectRepo(id="repo-be", project_id="proj-1", label="backend", path=str(tmp_path / "be"), is_primary=True))
            session.add(ProjectRepo(id="repo-fe", project_id="proj-1", label="frontend", path=str(tmp_path / "fe")))

        result = await list_project_repos("proj-1")
        assert len(result) == 2
        assert result[0].is_primary is True

    @pytest.mark.asyncio
    async def test_unknown_project_404s(self, db_manager):
        from src.mcp.autopilot.project_repo_routes import list_project_repos

        with pytest.raises(HTTPException) as exc_info:
            await list_project_repos("proj-does-not-exist")
        assert exc_info.value.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
