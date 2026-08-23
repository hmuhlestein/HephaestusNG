"""Tests for ProjectRepo model, migration, and resolve_project_repo helper.

REQ-01: ProjectRepo model with correct constraints
REQ-02: Nullable repo_id FK on Task, Ticket, TicketCommit, AgentWorktree
REQ-04: Migration backfills one primary ProjectRepo per AutopilotProject
REQ-05: Migration is non-destructive (base_dir untouched, historical rows keep repo_id=None)
REQ-06: resolve_project_repo falls back to primary repo when repo_id=None
NFR-02: Migration is idempotent (no duplicate rows on rerun)
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import (
    AgentWorktree,
    AutopilotProject,
    Base,
    DatabaseManager,
    ProjectRepo,
    RepoResolutionError,
    Task,
    Ticket,
    TicketCommit,
    resolve_project_repo,
)


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _skip_fk(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def project_with_repo(db_session):
    """Create a project with one primary ProjectRepo."""
    project_id = f"proj-{uuid.uuid4()}"
    repo_id = f"repo-{uuid.uuid4()}"
    base_dir = "/tmp/test-project"

    project = AutopilotProject(
        id=project_id,
        name="Test Project",
        base_dir=base_dir,
    )
    db_session.add(project)

    repo = ProjectRepo(
        id=repo_id,
        project_id=project_id,
        label="primary",
        path=base_dir,
        is_primary=True,
    )
    db_session.add(repo)
    db_session.commit()

    return project, repo


class TestProjectRepoModel:
    """Test ProjectRepo model constraints and behavior."""

    def test_project_repo_creation(self, db_session, project_with_repo):
        """REQ-01: ProjectRepo model exists with correct columns."""
        project, repo = project_with_repo
        assert repo.id.startswith("repo-")
        assert repo.project_id == project.id
        assert repo.label == "primary"
        assert repo.path == "/tmp/test-project"
        assert repo.is_primary is True

    def test_unique_constraint_project_path(self, db_session, project_with_repo):
        """REQ-01: UniqueConstraint on (project_id, path) enforced."""
        project, repo = project_with_repo
        duplicate = ProjectRepo(
            id=f"repo-{uuid.uuid4()}",
            project_id=project.id,
            label="different-label",
            path=repo.path,  # Same path
            is_primary=False,
        )
        db_session.add(duplicate)
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()

    def test_unique_constraint_project_label(self, db_session, project_with_repo):
        """REQ-01: UniqueConstraint on (project_id, label) enforced."""
        project, repo = project_with_repo
        duplicate = ProjectRepo(
            id=f"repo-{uuid.uuid4()}",
            project_id=project.id,
            label=repo.label,  # Same label
            path="/tmp/different-path",
            is_primary=False,
        )
        db_session.add(duplicate)
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()

    def test_multiple_repos_different_paths(self, db_session, project_with_repo):
        """REQ-01: Multiple repos with different paths/labels allowed."""
        project, repo = project_with_repo
        repo2 = ProjectRepo(
            id=f"repo-{uuid.uuid4()}",
            project_id=project.id,
            label="frontend",
            path="/tmp/frontend",
            is_primary=False,
        )
        db_session.add(repo2)
        db_session.commit()
        assert repo2.id is not None

    def test_repo_id_nullable_on_task(self, db_session):
        """REQ-02: Task.repo_id is nullable."""
        task = Task(
            id=f"task-{uuid.uuid4()}",
            raw_description="Test task",
            done_definition="Done when complete",
        )
        db_session.add(task)
        db_session.commit()
        assert task.repo_id is None

    def test_repo_id_nullable_on_ticket(self, db_session):
        """REQ-02: Ticket.repo_id is nullable."""
        ticket = Ticket(
            id=f"ticket-{uuid.uuid4()}",
            workflow_id="wf-1",
            created_by_agent_id="agent-1",
            title="Test",
            description="Test ticket",
            ticket_type="bug",
            priority="medium",
            status="open",
        )
        db_session.add(ticket)
        db_session.commit()
        assert ticket.repo_id is None

    def test_repo_id_nullable_on_ticket_commit(self, db_session):
        """REQ-02: TicketCommit.repo_id is nullable."""
        from datetime import datetime
        commit = TicketCommit(
            id=f"tc-{uuid.uuid4()}",
            ticket_id="ticket-1",
            agent_id="agent-1",
            commit_sha="abc123",
            commit_message="Test commit",
            commit_timestamp=datetime.utcnow(),
        )
        db_session.add(commit)
        db_session.commit()
        assert commit.repo_id is None

    def test_out_of_scope_default_false(self, db_session):
        """REQ-10: TicketCommit.out_of_scope defaults to False."""
        from datetime import datetime
        commit = TicketCommit(
            id=f"tc-{uuid.uuid4()}",
            ticket_id="ticket-1",
            agent_id="agent-1",
            commit_sha="abc123",
            commit_message="Test commit",
            commit_timestamp=datetime.utcnow(),
        )
        db_session.add(commit)
        db_session.commit()
        assert commit.out_of_scope is False


class TestResolveProjectRepo:
    """Test resolve_project_repo helper function."""

    def test_resolve_with_explicit_repo_id(self, db_session, project_with_repo):
        """REQ-06: Returns specified repo when repo_id is valid."""
        project, repo = project_with_repo
        result = resolve_project_repo(db_session, project.id, repo.id)
        assert result.id == repo.id
        assert result.path == repo.path

    def test_resolve_with_none_falls_back_to_primary(self, db_session, project_with_repo):
        """REQ-06: Falls back to primary repo when repo_id is None."""
        project, repo = project_with_repo
        result = resolve_project_repo(db_session, project.id, None)
        assert result.id == repo.id
        assert result.is_primary is True

    def test_resolve_with_invalid_repo_id_falls_back(self, db_session, project_with_repo):
        """REQ-06: Falls back to primary when repo_id doesn't exist."""
        project, repo = project_with_repo
        result = resolve_project_repo(db_session, project.id, "nonexistent-repo")
        assert result.id == repo.id
        assert result.is_primary is True

    def test_resolve_raises_when_no_repos(self, db_session):
        """Raises RepoResolutionError when no repos exist for project."""
        with pytest.raises(RepoResolutionError) as exc_info:
            resolve_project_repo(db_session, "nonexistent-project", None)
        assert exc_info.value.project_id == "nonexistent-project"

    def test_resolve_with_multiple_repos(self, db_session, project_with_repo):
        """REQ-06: Resolves specific repo when multiple exist."""
        project, primary_repo = project_with_repo
        secondary_repo = ProjectRepo(
            id=f"repo-{uuid.uuid4()}",
            project_id=project.id,
            label="frontend",
            path="/tmp/frontend",
            is_primary=False,
        )
        db_session.add(secondary_repo)
        db_session.commit()

        result = resolve_project_repo(db_session, project.id, secondary_repo.id)
        assert result.id == secondary_repo.id
        assert result.label == "frontend"


class TestMigration:
    """Test migrate_project_repos_table function."""

    def test_migration_creates_table_and_columns(self):
        """REQ-04: Migration creates project_repos table and repo_id columns."""
        from src.core.schema_migrations import migrate_project_repos_table

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _skip_fk(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()

        # Create base tables first
        Base.metadata.create_all(engine)

        # Run migration
        migrate_project_repos_table(engine)

        # Verify table exists
        with engine.connect() as conn:
            result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='project_repos'"))
            assert result.fetchone() is not None

            # Verify columns exist
            result = conn.execute(text("PRAGMA table_info(tasks)"))
            columns = [row[1] for row in result.fetchall()]
            assert "repo_id" in columns

        engine.dispose()

    def test_migration_backfills_primary_repo(self):
        """REQ-04: Migration creates one primary ProjectRepo per existing project."""
        from src.core.schema_migrations import migrate_project_repos_table

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _skip_fk(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()

        Base.metadata.create_all(engine)

        # Add a project before migration
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO autopilot_projects (id, name, base_dir, is_default, is_active, cost_total_usd, review_mode, created_at, updated_at) "
                "VALUES ('proj-1', 'Test', '/tmp/test', 0, 0, 0.0, 0, '2024-01-01 00:00:00', '2024-01-01 00:00:00')"
            ))
            conn.commit()

        # Run migration
        migrate_project_repos_table(engine)

        # Verify backfill
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        repos = session.query(ProjectRepo).filter_by(project_id="proj-1").all()
        assert len(repos) == 1
        assert repos[0].is_primary is True
        assert repos[0].path == "/tmp/test"
        assert repos[0].label == "primary"
        session.close()
        engine.dispose()

    def test_migration_idempotent(self):
        """NFR-02: Running migration twice produces no duplicate rows."""
        from src.core.schema_migrations import migrate_project_repos_table

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _skip_fk(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()

        Base.metadata.create_all(engine)

        # Add a project
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO autopilot_projects (id, name, base_dir, is_default, is_active, cost_total_usd, review_mode, created_at, updated_at) "
                "VALUES ('proj-1', 'Test', '/tmp/test', 0, 0, 0.0, 0, '2024-01-01 00:00:00', '2024-01-01 00:00:00')"
            ))
            conn.commit()

        # Run migration twice
        migrate_project_repos_table(engine)
        migrate_project_repos_table(engine)

        # Verify no duplicates
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        repos = session.query(ProjectRepo).filter_by(project_id="proj-1").all()
        assert len(repos) == 1
        session.close()
        engine.dispose()

    def test_migration_preserves_base_dir(self):
        """REQ-05: Migration does not modify AutopilotProject.base_dir."""
        from src.core.schema_migrations import migrate_project_repos_table

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _skip_fk(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()

        Base.metadata.create_all(engine)

        original_base_dir = "/tmp/original-project"
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO autopilot_projects (id, name, base_dir, is_default, is_active, cost_total_usd, review_mode, created_at, updated_at) "
                "VALUES ('proj-1', 'Test', :base_dir, 0, 0, 0.0, 0, '2024-01-01 00:00:00', '2024-01-01 00:00:00')"
            ), {"base_dir": original_base_dir})
            conn.commit()

        migrate_project_repos_table(engine)

        # Verify base_dir unchanged
        with engine.connect() as conn:
            result = conn.execute(text("SELECT base_dir FROM autopilot_projects WHERE id='proj-1'"))
            row = result.fetchone()
            assert row[0] == original_base_dir

        engine.dispose()
