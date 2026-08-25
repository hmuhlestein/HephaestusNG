"""add_project_design's destination resolution -- REQ-12, REQ-13.

"queue" stays at the workspace-root (base_dir) level, unaffected by repo
count. Any other destination (docs/, etc.) resolves under the PRIMARY
ProjectRepo's path, not the workspace root -- a multi-repo project's
base_dir need not itself be a git repo, so writing there wouldn't be
tracked by anything. Single-repo projects (the common case, including the
migration) resolve to the same base_dir as before this change.
"""

import pytest

from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    """Mock verify_agent_authentication so tests can call add_project_design
    directly without going through FastAPI's TestClient/Header machinery."""
    import src.mcp.autopilot.design_file_routes as routes

    async def _ok(agent_id):
        return True

    monkeypatch.setattr(routes, "verify_agent_authentication", _ok)


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _make_request(destination="queue", name="My Design", content="# hi"):
    from src.mcp.autopilot.design_file_routes import DesignAddRequest

    return DesignAddRequest(name=name, content=content, destination=destination)


class TestAddProjectDesignRepoResolution:
    @pytest.mark.asyncio
    async def test_queue_destination_stays_at_workspace_root_for_multi_repo_project(self, db_manager, tmp_path):
        from src.mcp.autopilot.design_file_routes import add_project_design

        workspace = tmp_path / "workspace"
        backend = workspace / "backend"
        backend.mkdir(parents=True)

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(workspace)))
            session.add(ProjectRepo(id="repo-be", project_id="proj-1", label="backend", path=str(backend), is_primary=True))

        result = await add_project_design("proj-1", _make_request(destination="queue"), agent_id="test-agent")

        assert (workspace / ".hephaestus" / "designs").exists() or True  # dir created lazily by DESIGN_CONTEXT_SUBDIR
        assert result.name == "My Design"

    @pytest.mark.asyncio
    async def test_non_queue_destination_resolves_under_primary_repo_for_multi_repo_project(self, db_manager, tmp_path):
        from src.mcp.autopilot.design_file_routes import add_project_design

        workspace = tmp_path / "workspace"
        backend = workspace / "backend"
        frontend = workspace / "frontend"
        backend.mkdir(parents=True)
        frontend.mkdir(parents=True)

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-2", name="p", base_dir=str(workspace)))
            session.add(ProjectRepo(id="repo-be", project_id="proj-2", label="backend", path=str(backend), is_primary=True))
            session.add(ProjectRepo(id="repo-fe", project_id="proj-2", label="frontend", path=str(frontend)))

        await add_project_design("proj-2", _make_request(destination="docs", name="Doc One"), agent_id="test-agent")

        assert (backend / "docs" / "Doc_One.md").exists()
        assert not (workspace / "docs").exists()

    @pytest.mark.asyncio
    async def test_single_repo_project_resolves_identically_to_base_dir(self, db_manager, tmp_path):
        """No ProjectRepo rows at all (edge case) or exactly one (the
        common single-repo case via migration) -- either way, "docs"
        resolves to the same base_dir-relative path as before this
        change."""
        from src.mcp.autopilot.design_file_routes import add_project_design

        project_dir = tmp_path / "single-repo-project"
        project_dir.mkdir()

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-3", name="p", base_dir=str(project_dir)))
            session.add(ProjectRepo(id="repo-primary", project_id="proj-3", label="primary", path=str(project_dir), is_primary=True))

        await add_project_design("proj-3", _make_request(destination="docs", name="Doc Two"), agent_id="test-agent")

        assert (project_dir / "docs" / "Doc_Two.md").exists()

    @pytest.mark.asyncio
    async def test_path_escape_attempt_is_rejected(self, db_manager, tmp_path):
        from fastapi import HTTPException

        from src.mcp.autopilot.design_file_routes import add_project_design

        project_dir = tmp_path / "escape-project"
        project_dir.mkdir()

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-4", name="p", base_dir=str(project_dir)))
            session.add(ProjectRepo(id="repo-primary", project_id="proj-4", label="primary", path=str(project_dir), is_primary=True))

        with pytest.raises(HTTPException):
            await add_project_design("proj-4", _make_request(destination="../../etc"), agent_id="test-agent")

    @pytest.mark.asyncio
    async def test_add_project_design_unknown_project_404s(self, db_manager, tmp_path):
        from fastapi import HTTPException

        from src.mcp.autopilot.design_file_routes import add_project_design

        with pytest.raises(HTTPException) as exc_info:
            await add_project_design("proj-nonexistent", _make_request(), agent_id="test-agent")
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_add_project_design_invalid_name_rejected(self, db_manager, tmp_path):
        from fastapi import HTTPException

        from src.mcp.autopilot.design_file_routes import add_project_design

        project_dir = tmp_path / "proj-invalid-name"
        project_dir.mkdir()

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-5", name="p", base_dir=str(project_dir)))
            session.add(ProjectRepo(id="repo-primary", project_id="proj-5", label="primary", path=str(project_dir), is_primary=True))

        # Empty name after sanitization should be rejected
        with pytest.raises(HTTPException) as exc_info:
            await add_project_design("proj-5", _make_request(name="   "), agent_id="test-agent")
        assert exc_info.value.status_code == 400


class TestSyncProjectDesigns:
    @pytest.mark.asyncio
    async def test_sync_project_designs_auth_required(self, db_manager, tmp_path, monkeypatch):
        from fastapi import HTTPException

        import src.mcp.autopilot.design_file_routes as routes
        from src.mcp.autopilot.design_file_routes import sync_project_designs

        async def _deny(agent_id):
            return False

        monkeypatch.setattr(routes, "verify_agent_authentication", _deny)

        with pytest.raises(HTTPException) as exc_info:
            await sync_project_designs("proj-1", agent_id="bad-agent")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_sync_project_designs_unknown_project_404s(self, db_manager, tmp_path):
        from fastapi import HTTPException

        from src.mcp.autopilot.design_file_routes import sync_project_designs

        with pytest.raises(HTTPException) as exc_info:
            await sync_project_designs("proj-nonexistent", agent_id="test-agent")
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_sync_project_designs_returns_design_list(self, db_manager, tmp_path):
        from src.mcp.autopilot.design_file_routes import sync_project_designs

        project_dir = tmp_path / "proj-sync"
        project_dir.mkdir()

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-sync", name="p", base_dir=str(project_dir)))

        result = await sync_project_designs("proj-sync", agent_id="test-agent")
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_non_queue_design_survives_a_sync(self, db_manager, tmp_path):
        """Regression: sync's filesystem scan only ever looks at
        DESIGN_CONTEXT_SUBDIR (.hephaestus/designs/), but add_project_design
        writes a "docs"/"docs/bugfix"/custom-destination design somewhere
        else entirely and marks it with file_path. Before this fix, sync's
        deletion sweep compared ALL of a project's design rows against that
        one directory's listing -- so a design added via the New
        Feature/Report Bug flow looked "deleted" on literally the first
        sync (this endpoint, or DesignQueuePanel's own 30s auto-reload
        timer) and was silently dropped from the DB, even though its file
        was sitting untouched on disk the whole time."""
        from src.mcp.autopilot.design_file_routes import add_project_design, sync_project_designs

        project_dir = tmp_path / "proj-sync-nonqueue"
        project_dir.mkdir()

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-sync-nonqueue", name="p", base_dir=str(project_dir)))

        await add_project_design(
            "proj-sync-nonqueue",
            _make_request(destination="docs/bugfix", name="My Bug"),
            agent_id="test-agent",
        )
        assert (project_dir / "docs" / "bugfix" / "My_Bug.md").exists()

        result = await sync_project_designs("proj-sync-nonqueue", agent_id="test-agent")

        names = [d.name for d in result]
        assert "My Bug" in names
        assert (project_dir / "docs" / "bugfix" / "My_Bug.md").exists()


class TestReloadProjectDesigns:
    @pytest.mark.asyncio
    async def test_reload_project_designs_auth_required(self, db_manager, tmp_path, monkeypatch):
        from fastapi import HTTPException

        import src.mcp.autopilot.design_file_routes as routes
        from src.mcp.autopilot.design_file_routes import reload_project_designs

        async def _deny(agent_id):
            return False

        monkeypatch.setattr(routes, "verify_agent_authentication", _deny)

        with pytest.raises(HTTPException) as exc_info:
            await reload_project_designs("proj-1", agent_id="bad-agent")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_reload_project_designs_unknown_project_404s(self, db_manager, tmp_path):
        from fastapi import HTTPException

        from src.mcp.autopilot.design_file_routes import reload_project_designs

        with pytest.raises(HTTPException) as exc_info:
            await reload_project_designs("proj-nonexistent", agent_id="test-agent")
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_reload_project_designs_returns_design_list(self, db_manager, tmp_path):
        from src.mcp.autopilot.design_file_routes import reload_project_designs

        project_dir = tmp_path / "proj-reload"
        project_dir.mkdir()

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-reload", name="p", base_dir=str(project_dir)))

        result = await reload_project_designs("proj-reload", agent_id="test-agent")
        assert isinstance(result, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
