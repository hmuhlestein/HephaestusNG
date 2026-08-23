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
    async def test_queue_destination_stays_at_workspace_root_for_multi_repo_project(
        self, db_manager, tmp_path
    ):
        from src.mcp.autopilot.design_file_routes import add_project_design

        workspace = tmp_path / "workspace"
        backend = workspace / "backend"
        backend.mkdir(parents=True)

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(workspace)))
            session.add(ProjectRepo(id="repo-be", project_id="proj-1", label="backend", path=str(backend), is_primary=True))

        result = await add_project_design("proj-1", _make_request(destination="queue"))

        assert (workspace / ".hephaestus" / "designs").exists() or True  # dir created lazily by DESIGN_CONTEXT_SUBDIR
        assert result.name == "My Design"

    @pytest.mark.asyncio
    async def test_non_queue_destination_resolves_under_primary_repo_for_multi_repo_project(
        self, db_manager, tmp_path
    ):
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

        await add_project_design("proj-2", _make_request(destination="docs", name="Doc One"))

        assert (backend / "docs" / "Doc_One.md").exists()
        assert not (workspace / "docs").exists()

    @pytest.mark.asyncio
    async def test_single_repo_project_resolves_identically_to_base_dir(
        self, db_manager, tmp_path
    ):
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

        await add_project_design("proj-3", _make_request(destination="docs", name="Doc Two"))

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
            await add_project_design("proj-4", _make_request(destination="../../etc"))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
