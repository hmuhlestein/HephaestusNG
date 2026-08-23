"""resolve_repo_path / get_project_repos / repo_id_for_path — REQ-06."""

import pytest

from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo
from src.core.repo_resolution import RepoNotFoundError, get_project_repos, repo_id_for_path, resolve_repo_path


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed_multi_repo_project(db_manager, tmp_path):
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        session.add(ProjectRepo(id="repo-backend", project_id="proj-1", label="backend", path=str(backend), is_primary=True))
        session.add(ProjectRepo(id="repo-frontend", project_id="proj-1", label="frontend", path=str(frontend), is_primary=False))
    return backend, frontend


def test_resolve_repo_path_none_returns_primary(db_manager, tmp_path):
    backend, _ = _seed_multi_repo_project(db_manager, tmp_path)
    with db_manager.session_scope() as session:
        path = resolve_repo_path(session, "proj-1", None)
        assert path == backend


def test_resolve_repo_path_valid_child_repo(db_manager, tmp_path):
    _, frontend = _seed_multi_repo_project(db_manager, tmp_path)
    with db_manager.session_scope() as session:
        path = resolve_repo_path(session, "proj-1", "repo-frontend")
        assert path == frontend


def test_resolve_repo_path_cross_project_repo_id_raises(db_manager, tmp_path):
    _seed_multi_repo_project(db_manager, tmp_path)
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-2", name="p2", base_dir=str(tmp_path / "other")))
    with db_manager.session_scope() as session:
        with pytest.raises(RepoNotFoundError):
            resolve_repo_path(session, "proj-2", "repo-backend")


def test_resolve_repo_path_no_project_repos_falls_back_to_base_dir(db_manager):
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-single", name="single", base_dir="/repos/single"))
    with db_manager.session_scope() as session:
        from pathlib import Path

        path = resolve_repo_path(session, "proj-single", None)
        assert path == Path("/repos/single")


def test_resolve_repo_path_unknown_project_raises_value_error(db_manager):
    with db_manager.session_scope() as session:
        with pytest.raises(ValueError):
            resolve_repo_path(session, "proj-nonexistent", None)


def test_get_project_repos_primary_first(db_manager, tmp_path):
    _seed_multi_repo_project(db_manager, tmp_path)
    with db_manager.session_scope() as session:
        repos = get_project_repos(session, "proj-1")
        assert repos[0].is_primary is True
        assert len(repos) == 2


def test_get_project_repos_bad_project_id_returns_empty_list(db_manager):
    with db_manager.session_scope() as session:
        repos = get_project_repos(session, "proj-bad")
        assert repos == []


def test_repo_id_for_path_matches_correct_repo(db_manager, tmp_path):
    backend, frontend = _seed_multi_repo_project(db_manager, tmp_path)
    with db_manager.session_scope() as session:
        assert repo_id_for_path(session, "proj-1", str(backend / "src" / "main.py")) == "repo-backend"
        assert repo_id_for_path(session, "proj-1", str(frontend / "src" / "App.tsx")) == "repo-frontend"


def test_repo_id_for_path_no_match_returns_none(db_manager, tmp_path):
    _seed_multi_repo_project(db_manager, tmp_path)
    with db_manager.session_scope() as session:
        assert repo_id_for_path(session, "proj-1", "/completely/unrelated/path.py") is None


def test_repo_id_for_path_longest_prefix_wins_for_nested_repos(db_manager, tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-nested", name="n", base_dir=str(outer)))
        session.add(ProjectRepo(id="repo-outer", project_id="proj-nested", label="outer", path=str(outer), is_primary=True))
        session.add(ProjectRepo(id="repo-inner", project_id="proj-nested", label="inner", path=str(inner)))
    with db_manager.session_scope() as session:
        assert repo_id_for_path(session, "proj-nested", str(inner / "file.py")) == "repo-inner"


def test_resolve_repo_path_zero_project_repos_falls_back_to_base_dir(db_manager, tmp_path):
    """WARNING-4: the genuine zero-ProjectRepo-rows path (not just "exactly
    one" -- the test_single_repo_project_resolves_identically_to_base_dir
    test only covers the one-row case). The fallback to
    AutopilotProject.base_dir must be exercised and the WARNING log
    emitted."""
    from pathlib import Path

    base = tmp_path / "legacy_project"
    base.mkdir()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-zero-repos", name="zero", base_dir=str(base)))

    # No ProjectRepo rows at all -- must fall back to base_dir
    with db_manager.session_scope() as session:
        path = resolve_repo_path(session, "proj-zero-repos", None)
        assert path == Path(str(base))

    # Verify zero ProjectRepo rows in the DB
    with db_manager.session_scope() as session:
        assert session.query(ProjectRepo).filter_by(project_id="proj-zero-repos").count() == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
