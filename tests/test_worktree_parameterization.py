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
