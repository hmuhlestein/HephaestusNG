#!/usr/bin/env python3
"""Tests for WorktreeManager parameterization and ProjectRepo model."""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import pytest
from git import Repo
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import (
    AutopilotProject,
    Base,
    ProjectRepo,
    resolve_repo_path,
)
from src.core.worktree_manager import WorktreeManager


@pytest.fixture
def temp_repo():
    """Create a temporary git repository for testing."""
    temp_dir = tempfile.mkdtemp()
    repo = Repo.init(temp_dir)

    # Create initial commit
    test_file = Path(temp_dir) / "README.md"
    test_file.write_text("# Test Repository\n")
    repo.index.add([str(test_file)])
    repo.index.commit("Initial commit")

    yield repo

    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def db_session():
    """Create an in-memory test database session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def sample_project(db_session):
    """Create a sample project with repos."""
    project = AutopilotProject(
        id="proj-test-123",
        name="Test Project",
        base_dir="/tmp/test-project",
        is_active=True,
    )
    db_session.add(project)

    primary_repo = ProjectRepo(
        id="repo-primary",
        project_id="proj-test-123",
        label="backend",
        path="/tmp/test-project",
        is_primary=True,
    )
    db_session.add(primary_repo)

    secondary_repo = ProjectRepo(
        id="repo-secondary",
        project_id="proj-test-123",
        label="frontend",
        path="/tmp/test-frontend",
        is_primary=False,
    )
    db_session.add(secondary_repo)

    db_session.commit()
    return project


class TestProjectRepoModel:
    """Tests for the ProjectRepo database model."""

    def test_project_repo_creation(self, db_session, sample_project):
        """Test that ProjectRepo rows are created correctly."""
        repos = db_session.query(ProjectRepo).filter_by(project_id="proj-test-123").all()
        assert len(repos) == 2

        primary = db_session.query(ProjectRepo).filter_by(
            project_id="proj-test-123", is_primary=True
        ).first()
        assert primary is not None
        assert primary.label == "backend"
        assert primary.path == "/tmp/test-project"

    def test_project_repo_unique_constraint_path(self, db_session, sample_project):
        """Test that duplicate paths are rejected."""
        duplicate = ProjectRepo(
            id="repo-dup",
            project_id="proj-test-123",
            label="other",
            path="/tmp/test-project",  # Same as primary
            is_primary=False,
        )
        db_session.add(duplicate)
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()
        db_session.rollback()

    def test_project_repo_unique_constraint_label(self, db_session, sample_project):
        """Test that duplicate labels are rejected."""
        duplicate = ProjectRepo(
            id="repo-dup",
            project_id="proj-test-123",
            label="backend",  # Same as primary
            path="/tmp/test-other",
            is_primary=False,
        )
        db_session.add(duplicate)
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()
        db_session.rollback()

    def test_project_repos_relationship(self, db_session, sample_project):
        """Test the relationship from AutopilotProject to ProjectRepo."""
        project = db_session.query(AutopilotProject).filter_by(id="proj-test-123").first()
        assert len(project.repos) == 2


class TestResolveRepoPath:
    """Tests for the resolve_repo_path helper function."""

    def test_resolve_with_explicit_repo_id(self, db_session, sample_project):
        """Test resolving a specific repo_id."""
        result = resolve_repo_path(db_session, "proj-test-123", "repo-secondary")
        assert result == Path("/tmp/test-frontend")

    def test_resolve_with_none_repo_id_falls_back_to_primary(self, db_session, sample_project):
        """Test that None repo_id falls back to primary repo."""
        result = resolve_repo_path(db_session, "proj-test-123", None)
        assert result == Path("/tmp/test-project")

    def test_resolve_with_invalid_repo_id_falls_back(self, db_session, sample_project):
        """Test that invalid repo_id falls back to primary."""
        result = resolve_repo_path(db_session, "proj-test-123", "repo-nonexistent")
        assert result == Path("/tmp/test-project")

    def test_resolve_falls_back_to_any_repo_when_no_primary(self, db_session):
        """Test fallback when no primary repo is marked."""
        project = AutopilotProject(
            id="proj-no-primary",
            name="No Primary",
            base_dir="/tmp/no-primary",
        )
        db_session.add(project)

        repo = ProjectRepo(
            id="repo-only",
            project_id="proj-no-primary",
            label="only",
            path="/tmp/only-repo",
            is_primary=False,
        )
        db_session.add(repo)
        db_session.commit()

        result = resolve_repo_path(db_session, "proj-no-primary", None)
        assert result == Path("/tmp/only-repo")

    def test_resolve_falls_back_to_base_dir_when_no_repos(self, db_session):
        """Test fallback to AutopilotProject.base_dir when no ProjectRepo rows exist."""
        project = AutopilotProject(
            id="proj-legacy",
            name="Legacy Project",
            base_dir="/tmp/legacy-project",
        )
        db_session.add(project)
        db_session.commit()

        result = resolve_repo_path(db_session, "proj-legacy", None)
        assert result == Path("/tmp/legacy-project")

    def test_resolve_raises_on_unknown_project(self, db_session):
        """Test that unknown project_id raises ValueError."""
        with pytest.raises(ValueError, match="No repo path found"):
            resolve_repo_path(db_session, "proj-nonexistent", None)


class TestWorktreeManagerParameterization:
    """Tests for WorktreeManager's new repo_path parameter."""

    def test_constructor_with_repo_path(self, temp_repo, monkeypatch):
        """Test that WorktreeManager accepts repo_path parameter."""
        import src.core.simple_config

        config = src.core.simple_config.Config()
        config.paths.worktree_base_path = Path(tempfile.mkdtemp())
        config.git.main_repo_path = Path(temp_repo.working_dir)

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

        # Should work with explicit repo_path
        from src.core.database import DatabaseManager

        db = DatabaseManager(":memory:")
        db.create_tables()

        manager = WorktreeManager(db, repo_path=Path(temp_repo.working_dir))
        assert manager._project_root == Path(temp_repo.working_dir)

        shutil.rmtree(config.paths.worktree_base_path, ignore_errors=True)

    def test_constructor_without_repo_path_uses_config(self, temp_repo, monkeypatch):
        """Test that WorktreeManager falls back to config when no repo_path."""
        import src.core.simple_config

        config = src.core.simple_config.Config()
        config.paths.worktree_base_path = Path(tempfile.mkdtemp())
        config.git.main_repo_path = Path(temp_repo.working_dir)

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

        from src.core.database import DatabaseManager

        db = DatabaseManager(":memory:")
        db.create_tables()

        manager = WorktreeManager(db)
        assert manager._project_root == Path(temp_repo.working_dir)

        shutil.rmtree(config.paths.worktree_base_path, ignore_errors=True)

    def test_constructor_with_string_path(self, temp_repo, monkeypatch):
        """Test that WorktreeManager accepts string repo_path."""
        import src.core.simple_config

        config = src.core.simple_config.Config()
        config.paths.worktree_base_path = Path(tempfile.mkdtemp())
        config.git.main_repo_path = Path(temp_repo.working_dir)

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

        from src.core.database import DatabaseManager

        db = DatabaseManager(":memory:")
        db.create_tables()

        # Should work with string path
        manager = WorktreeManager(db, repo_path=str(temp_repo.working_dir))
        assert manager._project_root == Path(temp_repo.working_dir)

        shutil.rmtree(config.paths.worktree_base_path, ignore_errors=True)

    def test_reload_still_works(self, temp_repo, monkeypatch):
        """Test that reload() still works for backward compatibility."""
        import src.core.simple_config

        config = src.core.simple_config.Config()
        config.paths.worktree_base_path = Path(tempfile.mkdtemp())
        config.git.main_repo_path = Path(temp_repo.working_dir)

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

        from src.core.database import DatabaseManager

        db = DatabaseManager(":memory:")
        db.create_tables()

        manager = WorktreeManager(db)
        new_path = Path(tempfile.mkdtemp())
        # Create a git repo in the new path
        Repo.init(new_path)
        test_file = new_path / "test.txt"
        test_file.write_text("test")
        Repo(new_path).index.add([str(test_file)])
        Repo(new_path).index.commit("init")

        manager.reload(new_path)
        assert manager._project_root == new_path

        shutil.rmtree(new_path, ignore_errors=True)
        shutil.rmtree(config.paths.worktree_base_path, ignore_errors=True)

    def test_invalid_repo_path_raises(self, monkeypatch):
        """Test that invalid repo_path raises ValueError."""
        import src.core.simple_config

        config = src.core.simple_config.Config()
        config.git.main_repo_path = Path("/nonexistent/path")

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

        from src.core.database import DatabaseManager

        db = DatabaseManager(":memory:")
        db.create_tables()

        with pytest.raises(ValueError, match="Not a valid git repository"):
            WorktreeManager(db, repo_path=Path("/nonexistent/path"))

    def test_reload_resets_worktree_base_cache(self, temp_repo, monkeypatch):
        """BLOCKER-1: Verify worktree_base recomputes after reload().

        Without the cache reset, a global worktree_base_path config override
        would silently redirect worktrees to the wrong project after reload().
        """
        import src.core.simple_config

        config = src.core.simple_config.Config()
        config.git.main_repo_path = Path(temp_repo.working_dir)

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

        from src.core.database import DatabaseManager

        db = DatabaseManager(":memory:")
        db.create_tables()

        manager = WorktreeManager(db)
        # Initially, worktree_base should be under temp_repo
        initial_base = manager.worktree_base
        assert str(temp_repo.working_dir) in str(initial_base)

        # Create a second repo
        new_path = Path(tempfile.mkdtemp())
        Repo.init(new_path)
        test_file = new_path / "test.txt"
        test_file.write_text("test")
        Repo(new_path).index.add([str(test_file)])
        Repo(new_path).index.commit("init")

        # Reload to new repo
        manager.reload(new_path)

        # After reload, worktree_base should be under new_path, NOT temp_repo
        new_base = manager.worktree_base
        assert str(new_path) in str(new_base)
        assert str(temp_repo.working_dir) not in str(new_base)

        shutil.rmtree(new_path, ignore_errors=True)

    def test_reload_with_config_override_resets_correctly(self, temp_repo, monkeypatch):
        """BLOCKER-1: Verify worktree_base resets even with config override.

        When config.paths.worktree_base_path is set, reload() must still
        invalidate the cache so the override is re-evaluated.
        """
        import src.core.simple_config

        config = src.core.simple_config.Config()
        config.git.main_repo_path = Path(temp_repo.working_dir)
        # Set a global override
        override_path = Path(tempfile.mkdtemp())
        config.paths.worktree_base_path = override_path

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

        from src.core.database import DatabaseManager

        db = DatabaseManager(":memory:")
        db.create_tables()

        manager = WorktreeManager(db)
        # With override set, worktree_base should use the override
        assert manager.worktree_base == override_path

        # Create a second repo
        new_path = Path(tempfile.mkdtemp())
        Repo.init(new_path)
        test_file = new_path / "test.txt"
        test_file.write_text("test")
        Repo(new_path).index.add([str(test_file)])
        Repo(new_path).index.commit("init")

        # Reload to new repo — cache should be invalidated
        manager.reload(new_path)
        # After reload, worktree_base should still be the override (since config didn't change)
        # but the cache was reset and recomputed
        assert manager.worktree_base == override_path
        # Verify _project_root changed
        assert manager._project_root == new_path

        shutil.rmtree(new_path, ignore_errors=True)
        shutil.rmtree(override_path, ignore_errors=True)

    def test_resolve_repo_path_with_project_repo(self, db_session):
        """WARNING-2: Verify resolve_repo_path uses ProjectRepo table."""
        from src.core.database import resolve_repo_path

        project = AutopilotProject(
            id="proj-multi",
            name="Multi Repo",
            base_dir="/tmp/base",
        )
        db_session.add(project)

        frontend_repo = ProjectRepo(
            id="repo-frontend",
            project_id="proj-multi",
            label="frontend",
            path="/tmp/frontend",
            is_primary=False,
        )
        db_session.add(frontend_repo)
        db_session.commit()

        # Should resolve to frontend repo, not base_dir
        result = resolve_repo_path(db_session, "proj-multi", "repo-frontend")
        assert result == Path("/tmp/frontend")

        # Should fall back to primary (none set, so any repo)
        result = resolve_repo_path(db_session, "proj-multi", None)
        assert result == Path("/tmp/frontend")

    def test_explicit_repo_path_ignores_config_override(self, temp_repo, monkeypatch):
        """BLOCKER-1: Verify explicit repo_path ignores global worktree_base_path.

        When repo_path is explicitly provided to the constructor, the global
        config.paths.worktree_base_path override should be ignored to prevent
        redirecting worktrees to the wrong project.
        """
        import src.core.simple_config

        config = src.core.simple_config.Config()
        config.git.main_repo_path = Path(temp_repo.working_dir)
        # Set a global override that points to a different location
        override_path = Path(tempfile.mkdtemp()) / "override_worktrees"
        config.paths.worktree_base_path = override_path

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

        from src.core.database import DatabaseManager

        db = DatabaseManager(":memory:")
        db.create_tables()

        # Without explicit repo_path, should use override
        manager_default = WorktreeManager(db)
        assert manager_default.worktree_base == override_path
        assert not manager_default._explicit_repo_path

        # With explicit repo_path, should IGNORE override and use repo path
        manager_explicit = WorktreeManager(db, repo_path=Path(temp_repo.working_dir))
        assert manager_explicit._explicit_repo_path
        assert str(temp_repo.working_dir) in str(manager_explicit.worktree_base)
        assert str(override_path) not in str(manager_explicit.worktree_base)

        shutil.rmtree(override_path, ignore_errors=True)
