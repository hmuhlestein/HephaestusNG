"""Tests for ProjectRepo model, migration, and repo_resolution helpers.

Covers: REQ-01, REQ-02, REQ-03, REQ-04, REQ-05, REQ-06, NFR-01, NFR-02.
"""

import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import (
    AutopilotProject,
    Base,
    ProjectRepo,
    Task,
)
from src.core.repo_resolution import list_repos, resolve_primary_repo, resolve_repo
from src.core.schema_migrations import migrate_project_repos_table


@pytest.fixture
def db_engine():
    """Create an in-memory SQLite engine for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Create a test database session."""
    session_local = sessionmaker(bind=db_engine)
    session = session_local()
    yield session
    session.close()


# ── ProjectRepo Model Tests (REQ-01, REQ-03, NFR-02) ──────────────


class TestProjectRepoModel:
    """REQ-01: ProjectRepo model with correct constraints."""

    def test_project_repo_table_exists(self, db_engine):
        """REQ-01: project_repos table exists after create_all."""
        inspector = inspect(db_engine)
        assert "project_repos" in inspector.get_table_names()

    def test_project_repo_columns(self, db_engine):
        """REQ-01: project_repos has all required columns."""
        inspector = inspect(db_engine)
        columns = {c["name"] for c in inspector.get_columns("project_repos")}
        assert "id" in columns
        assert "project_id" in columns
        assert "label" in columns
        assert "path" in columns
        assert "is_primary" in columns
        assert "created_at" in columns

    def test_project_repo_unique_constraints(self, db_engine):
        """NFR-02: UniqueConstraint on (project_id, path) and (project_id, label)."""
        inspector = inspect(db_engine)
        constraints = inspector.get_unique_constraints("project_repos")
        constraint_names = {c["name"] for c in constraints}
        assert "uq_project_repo_path" in constraint_names
        assert "uq_project_repo_label" in constraint_names

    def test_project_repo_create_and_query(self, db_session):
        """REQ-01: Can create and query a ProjectRepo."""
        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="test-project",
            base_dir="/tmp/test",
        )
        db_session.add(project)
        db_session.flush()

        repo = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            label="backend",
            path="/tmp/test/backend",
            is_primary=True,
        )
        db_session.add(repo)
        db_session.commit()

        result = db_session.query(ProjectRepo).filter_by(project_id=project.id).first()
        assert result is not None
        assert result.label == "backend"
        assert result.path == "/tmp/test/backend"
        assert result.is_primary is True

    def test_project_repo_absolute_path(self, db_session):
        """REQ-03: ProjectRepo.path stores absolute paths."""
        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="test-project",
            base_dir="/tmp/test",
        )
        db_session.add(project)
        db_session.flush()

        # Absolute path outside base_dir
        repo = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            label="frontend",
            path="/home/user/projects/frontend",
            is_primary=False,
        )
        db_session.add(repo)
        db_session.commit()

        result = db_session.query(ProjectRepo).filter_by(label="frontend").first()
        assert result.path == "/home/user/projects/frontend"

    def test_project_repo_duplicate_path_raises(self, db_session):
        """NFR-02: Duplicate (project_id, path) raises IntegrityError."""
        from sqlalchemy.exc import IntegrityError

        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="test-project",
            base_dir="/tmp/test",
        )
        db_session.add(project)
        db_session.flush()

        repo1 = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            label="backend",
            path="/tmp/test/backend",
        )
        db_session.add(repo1)
        db_session.commit()

        # Same path, same project
        repo2 = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            label="other",
            path="/tmp/test/backend",
        )
        db_session.add(repo2)
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_project_repo_duplicate_label_raises(self, db_session):
        """NFR-02: Duplicate (project_id, label) raises IntegrityError."""
        from sqlalchemy.exc import IntegrityError

        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="test-project",
            base_dir="/tmp/test",
        )
        db_session.add(project)
        db_session.flush()

        repo1 = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            label="main",
            path="/tmp/test",
        )
        db_session.add(repo1)
        db_session.commit()

        # Same label, same project, different path
        repo2 = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            label="main",
            path="/tmp/test/other",
        )
        db_session.add(repo2)
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_project_repo_different_projects_same_path(self, db_session):
        """Different projects can have repos at the same path."""
        proj1 = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="project-1",
            base_dir="/tmp/test1",
        )
        proj2 = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="project-2",
            base_dir="/tmp/test2",
        )
        db_session.add_all([proj1, proj2])
        db_session.flush()

        repo1 = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=proj1.id,
            label="main",
            path="/shared/path",
        )
        repo2 = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=proj2.id,
            label="main",
            path="/shared/path",
        )
        db_session.add_all([repo1, repo2])
        db_session.commit()  # Should not raise

    def test_autopilot_project_repos_relationship(self, db_session):
        """AutopilotProject.repos relationship works."""
        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="test-project",
            base_dir="/tmp/test",
        )
        db_session.add(project)
        db_session.flush()

        for label, path in [("backend", "/tmp/test/backend"), ("frontend", "/tmp/test/frontend")]:
            db_session.add(ProjectRepo(
                id=f"repo-{uuid.uuid4().hex[:12]}",
                project_id=project.id,
                label=label,
                path=path,
            ))
        db_session.commit()

        db_session.refresh(project)
        assert len(project.repos) == 2
        labels = {r.label for r in project.repos}
        assert labels == {"backend", "frontend"}

    def test_cascade_delete(self, db_session):
        """Deleting project cascades to repos."""
        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="test-project",
            base_dir="/tmp/test",
        )
        db_session.add(project)
        db_session.flush()

        db_session.add(ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            label="main",
            path="/tmp/test",
        ))
        db_session.commit()

        db_session.delete(project)
        db_session.commit()

        assert db_session.query(ProjectRepo).count() == 0


# ── repo_id Column Tests (REQ-02) ──────────────────────────────────


class TestRepoIdColumns:
    """REQ-02: Nullable repo_id FK on Task, Ticket, TicketCommit, AgentWorktree, Feature."""

    def test_task_has_repo_id(self, db_engine):
        """REQ-02: tasks table has repo_id column."""
        inspector = inspect(db_engine)
        columns = {c["name"] for c in inspector.get_columns("tasks")}
        assert "repo_id" in columns

    def test_ticket_has_repo_id(self, db_engine):
        """REQ-02: tickets table has repo_id column."""
        inspector = inspect(db_engine)
        columns = {c["name"] for c in inspector.get_columns("tickets")}
        assert "repo_id" in columns

    def test_ticket_commits_has_repo_id(self, db_engine):
        """REQ-02: ticket_commits table has repo_id column."""
        inspector = inspect(db_engine)
        columns = {c["name"] for c in inspector.get_columns("ticket_commits")}
        assert "repo_id" in columns

    def test_agent_worktrees_has_repo_id(self, db_engine):
        """REQ-02: agent_worktrees table has repo_id column."""
        inspector = inspect(db_engine)
        columns = {c["name"] for c in inspector.get_columns("agent_worktrees")}
        assert "repo_id" in columns

    def test_features_has_repo_id(self, db_engine):
        """REQ-02: features table has repo_id column."""
        inspector = inspect(db_engine)
        columns = {c["name"] for c in inspector.get_columns("features")}
        assert "repo_id" in columns

    def test_task_repo_id_nullable(self, db_session):
        """REQ-05: Task.repo_id is nullable (no backfill required)."""
        task = Task(
            id=f"task-{uuid.uuid4().hex[:8]}",
            raw_description="test task",
            done_definition="done",
            repo_id=None,
        )
        db_session.add(task)
        db_session.commit()

        result = db_session.query(Task).first()
        assert result.repo_id is None


# ── Migration Tests (REQ-04, REQ-05) ───────────────────────────────


class TestProjectRepoMigration:
    """REQ-04/05: Migration creates project_repos and backfills."""

    def _create_engine_without_project_repos(self):
        """Create engine with all tables except project_repos."""
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        # Create all tables normally
        Base.metadata.create_all(engine)
        # Drop project_repos to simulate pre-migration state
        with engine.connect() as conn:
            conn.execute(text("DROP TABLE IF EXISTS project_repos"))
            conn.commit()
        return engine

    def test_migration_creates_table(self):
        """REQ-04: Migration creates project_repos table."""
        engine = self._create_engine_without_project_repos()

        # Verify table doesn't exist
        inspector = inspect(engine)
        assert "project_repos" not in inspector.get_table_names()

        # Run migration
        migrate_project_repos_table(engine)

        # Verify table now exists
        inspector = inspect(engine)
        assert "project_repos" in inspector.get_table_names()

    def test_migration_backfills_existing_projects(self):
        """REQ-04: Migration creates one ProjectRepo per existing AutopilotProject."""
        engine = self._create_engine_without_project_repos()

        # Create some projects
        with Session(engine) as session:
            for i in range(3):
                session.add(AutopilotProject(
                    id=f"proj-{uuid.uuid4().hex[:8]}",
                    name=f"project-{i}",
                    base_dir=f"/tmp/project-{i}",
                ))
            session.commit()

        # Run migration
        migrate_project_repos_table(engine)

        # Verify each project has exactly one primary repo
        with Session(engine) as session:
            projects = session.query(AutopilotProject).all()
            for project in projects:
                repos = session.query(ProjectRepo).filter_by(project_id=project.id).all()
                assert len(repos) == 1
                assert repos[0].is_primary is True
                assert repos[0].path == project.base_dir
                assert repos[0].label == "main"

    def test_migration_is_idempotent(self):
        """REQ-04: Running migration twice doesn't duplicate rows."""
        engine = self._create_engine_without_project_repos()

        with Session(engine) as session:
            session.add(AutopilotProject(
                id=f"proj-{uuid.uuid4().hex[:8]}",
                name="test",
                base_dir="/tmp/test",
            ))
            session.commit()

        # Run migration twice
        migrate_project_repos_table(engine)
        migrate_project_repos_table(engine)

        # Should still have exactly one repo
        with Session(engine) as session:
            repos = session.query(ProjectRepo).all()
            assert len(repos) == 1

    def test_migration_preserves_base_dir(self):
        """REQ-05: Migration doesn't modify AutopilotProject.base_dir."""
        engine = self._create_engine_without_project_repos()

        base_dir = "/tmp/my-precious-project"
        with Session(engine) as session:
            session.add(AutopilotProject(
                id=f"proj-{uuid.uuid4().hex[:8]}",
                name="test",
                base_dir=base_dir,
            ))
            session.commit()

        migrate_project_repos_table(engine)

        with Session(engine) as session:
            project = session.query(AutopilotProject).first()
            assert project.base_dir == base_dir

    def test_migration_adds_repo_id_columns(self):
        """REQ-02: Migration adds repo_id column to tasks, tickets, etc."""
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)

        migrate_project_repos_table(engine)

        inspector = inspect(engine)
        for table in ["tasks", "tickets", "ticket_commits", "agent_worktrees", "features"]:
            columns = {c["name"] for c in inspector.get_columns(table)}
            assert "repo_id" in columns, f"{table} missing repo_id column"

    def test_migration_reraises_on_backfill_failure(self, monkeypatch):
        """Adversarial review WARNING: a backfill failure used to be
        logged and swallowed, letting the migration record as applied
        with some projects left without a ProjectRepo row. Must re-raise
        so _run_schema_migration doesn't mark it applied and it retries
        on next startup."""
        engine = self._create_engine_without_project_repos()

        with Session(engine) as session:
            session.add(AutopilotProject(
                id=f"proj-{uuid.uuid4().hex[:8]}",
                name="test",
                base_dir="/tmp/test",
            ))
            session.commit()

        def failing_commit(self, *args, **kwargs):
            raise RuntimeError("simulated backfill commit failure")

        monkeypatch.setattr(Session, "commit", failing_commit)

        with pytest.raises(RuntimeError, match="simulated backfill commit failure"):
            migrate_project_repos_table(engine)

    def test_migration_no_backfill_required(self):
        """REQ-05: Historical rows remain valid with repo_id=NULL."""
        engine = self._create_engine_without_project_repos()

        # Create a task before migration (no repo_id)
        with Session(engine) as session:
            session.add(Task(
                id=f"task-{uuid.uuid4().hex[:8]}",
                raw_description="pre-migration task",
                done_definition="done",
            ))
            session.commit()

        migrate_project_repos_table(engine)

        # Task should still exist with repo_id=None
        with Session(engine) as session:
            task = session.query(Task).first()
            assert task is not None
            assert task.repo_id is None


# ── repo_resolution Tests (REQ-06) ─────────────────────────────────


class TestRepoResolution:
    """REQ-06: resolve_repo falls back to primary when repo_id is unset."""

    def _setup_project_with_repos(self, session, num_repos=2):
        """Helper: create a project with N repos."""
        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="test-project",
            base_dir="/tmp/test",
        )
        session.add(project)
        session.flush()

        repos = []
        for i in range(num_repos):
            repo = ProjectRepo(
                id=f"repo-{uuid.uuid4().hex[:12]}",
                project_id=project.id,
                label=f"repo-{i}",
                path=f"/tmp/test/repo-{i}",
                is_primary=(i == 0),
            )
            session.add(repo)
            repos.append(repo)
        session.commit()
        return project, repos

    def test_resolve_primary_repo(self, db_session):
        """resolve_primary_repo returns the is_primary=True repo."""
        project, repos = self._setup_project_with_repos(db_session)
        primary = resolve_primary_repo(db_session, project.id)
        assert primary is not None
        assert primary.is_primary is True
        assert primary.path == "/tmp/test/repo-0"

    def test_resolve_primary_repo_none_when_no_repos(self, db_session):
        """resolve_primary_repo returns None for project with no repos."""
        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="empty-project",
            base_dir="/tmp/empty",
        )
        db_session.add(project)
        db_session.commit()

        result = resolve_primary_repo(db_session, project.id)
        assert result is None

    def test_resolve_repo_with_valid_repo_id(self, db_session):
        """resolve_repo returns the specified repo when repo_id is valid."""
        project, repos = self._setup_project_with_repos(db_session, num_repos=3)
        non_primary = repos[1]

        result = resolve_repo(db_session, project.id, non_primary.id)
        assert result is not None
        assert result.id == non_primary.id

    def test_resolve_repo_with_invalid_repo_id_falls_back(self, db_session):
        """REQ-06: resolve_repo falls back to primary when repo_id is invalid."""
        project, repos = self._setup_project_with_repos(db_session)

        result = resolve_repo(db_session, project.id, "repo-nonexistent")
        assert result is not None
        assert result.is_primary is True

    def test_resolve_repo_with_none_repo_id(self, db_session):
        """REQ-06: resolve_repo uses primary when repo_id is None."""
        project, repos = self._setup_project_with_repos(db_session)

        result = resolve_repo(db_session, project.id, None)
        assert result is not None
        assert result.is_primary is True

    def test_resolve_repo_with_empty_string_repo_id(self, db_session):
        """REQ-06: resolve_repo uses primary when repo_id is empty string."""
        project, repos = self._setup_project_with_repos(db_session)

        result = resolve_repo(db_session, project.id, "")
        assert result is not None
        assert result.is_primary is True

    def test_resolve_repo_no_repos_returns_none(self, db_session):
        """resolve_repo returns None for project with no repos."""
        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="empty",
            base_dir="/tmp/empty",
        )
        db_session.add(project)
        db_session.commit()

        result = resolve_repo(db_session, project.id, None)
        assert result is None

    def test_list_repos_primary_first(self, db_session):
        """list_repos returns repos sorted: primary first, then by label."""
        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="test",
            base_dir="/tmp/test",
        )
        db_session.add(project)
        db_session.flush()

        for label, path, is_primary in [
            ("backend", "/tmp/test/backend", False),
            ("main", "/tmp/test", True),
            ("frontend", "/tmp/test/frontend", False),
        ]:
            db_session.add(ProjectRepo(
                id=f"repo-{uuid.uuid4().hex[:12]}",
                project_id=project.id,
                label=label,
                path=path,
                is_primary=is_primary,
            ))
        db_session.commit()

        repos = list_repos(db_session, project.id)
        assert len(repos) == 3
        assert repos[0].is_primary is True
        # After primary, sorted by label
        assert repos[1].label == "backend"
        assert repos[2].label == "frontend"

    def test_list_repos_empty_project(self, db_session):
        """list_repos returns empty list for project with no repos."""
        project = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="empty",
            base_dir="/tmp/empty",
        )
        db_session.add(project)
        db_session.commit()

        repos = list_repos(db_session, project.id)
        assert repos == []
