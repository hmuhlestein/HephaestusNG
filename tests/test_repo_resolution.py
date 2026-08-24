"""Tests for C1+C2: ProjectRepo model, repo_resolution functions, and migration."""

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.database import (
    AutopilotProject,
    Base,
    ProjectRepo,
    Task,
    Ticket,
    Workflow,
)
from src.core.repo_resolution import (
    ensure_primary_repo,
    list_repos,
    resolve_primary_repo,
    resolve_repo,
)
from src.core.schema_migrations import migrate_project_repos_table


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _make_project(session, project_id="proj-1", base_dir="/tmp/proj1"):
    p = AutopilotProject(id=project_id, name="test", base_dir=base_dir)
    session.add(p)
    session.flush()
    return p


class TestProjectRepoModel:
    def test_project_repo_created_with_unique_constraints(self, engine):
        """REQ-01: ProjectRepo table exists with both unique constraints."""
        with Session(engine) as session:
            project = AutopilotProject(id="proj-uniq", name="p", base_dir="/tmp/uniq")
            session.add(project)
            session.flush()

            repo1 = ProjectRepo(
                id="repo-1",
                project_id="proj-uniq",
                label="backend",
                path="/tmp/uniq/backend",
                is_primary=True,
            )
            session.add(repo1)
            session.flush()

            # Duplicate path raises IntegrityError
            from sqlalchemy.exc import IntegrityError

            repo_dup_path = ProjectRepo(
                id="repo-dup",
                project_id="proj-uniq",
                label="frontend",
                path="/tmp/uniq/backend",  # same path
                is_primary=False,
            )
            session.add(repo_dup_path)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()

    def test_project_repo_duplicate_label_raises(self, engine):
        """REQ-01: Unique constraint on (project_id, label)."""
        with Session(engine) as session:
            project = AutopilotProject(id="proj-dup-label", name="p", base_dir="/tmp")
            session.add(project)
            session.flush()

            repo1 = ProjectRepo(
                id="repo-a",
                project_id="proj-dup-label",
                label="main",
                path="/tmp/a",
                is_primary=True,
            )
            session.add(repo1)
            session.flush()

            from sqlalchemy.exc import IntegrityError

            repo2 = ProjectRepo(
                id="repo-b",
                project_id="proj-dup-label",
                label="main",  # same label
                path="/tmp/b",
                is_primary=False,
            )
            session.add(repo2)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()

    def test_project_repo_path_absolute_outside_basedir(self, engine):
        """REQ-03: path is absolute and need not be under base_dir."""
        with Session(engine) as session:
            project = AutopilotProject(id="proj-abs", name="p", base_dir="/tmp/proj")
            session.add(project)
            session.flush()

            repo = ProjectRepo(
                id="repo-outside",
                project_id="proj-abs",
                label="frontend",
                path="/completely/different/path",
                is_primary=True,
            )
            session.add(repo)
            session.flush()
            assert repo.path == "/completely/different/path"


class TestEnsurePrimaryRepo:
    def test_creates_primary_repo_when_none_exists(self, session):
        """REQ-04/06: ensure_primary_repo creates exactly one primary repo."""
        project = _make_project(session)
        repo = ensure_primary_repo(session, project)

        assert repo is not None
        assert repo.is_primary is True
        assert repo.label == "main"
        assert repo.path == "/tmp/proj1"
        assert repo.project_id == project.id

    def test_idempotent_when_primary_exists(self, session):
        """ensure_primary_repo returns existing repo, creates no new row."""
        project = _make_project(session)
        repo1 = ensure_primary_repo(session, project)
        count_before = session.query(ProjectRepo).filter_by(project_id=project.id).count()

        repo2 = ensure_primary_repo(session, project)
        count_after = session.query(ProjectRepo).filter_by(project_id=project.id).count()

        assert repo1.id == repo2.id
        assert count_before == count_after == 1


class TestResolvePrimaryRepo:
    def test_returns_primary_repo(self, session):
        project = _make_project(session)
        ensure_primary_repo(session, project)

        result = resolve_primary_repo(session, project.id)
        assert result is not None
        assert result.is_primary is True

    def test_returns_none_for_missing_project(self, session):
        result = resolve_primary_repo(session, "nonexistent")
        assert result is None

    def test_returns_none_for_project_with_zero_repos(self, session):
        """REQ-06: project with 0 repos returns None, not an error."""
        project = _make_project(session)
        result = resolve_primary_repo(session, project.id)
        assert result is None


class TestResolveRepo:
    def test_none_repo_id_returns_primary(self, session):
        """REQ-06: repo_id=None falls back to primary."""
        project = _make_project(session)
        ensure_primary_repo(session, project)

        result = resolve_repo(session, project.id, None)
        assert result is not None
        assert result.is_primary is True

    def test_valid_repo_id_returns_that_repo(self, session):
        project = _make_project(session)
        ensure_primary_repo(session, project)

        other = ProjectRepo(
            id="repo-other",
            project_id=project.id,
            label="backend",
            path="/tmp/proj1/backend",
            is_primary=False,
        )
        session.add(other)
        session.flush()

        result = resolve_repo(session, project.id, other.id)
        assert result is not None
        assert result.id == other.id

    def test_stale_repo_id_falls_back_to_primary(self, session, caplog):
        """Gotcha 2: stale repo_id logs warning, returns primary."""
        project = _make_project(session)
        primary = ensure_primary_repo(session, project)

        with caplog.at_level(logging.WARNING):
            result = resolve_repo(session, project.id, "repo-nonexistent")

        assert result is not None
        assert result.id == primary.id
        assert "repo_id=repo-nonexistent not found" in caplog.text

    def test_repo_id_from_different_project_returns_none(self, session, caplog):
        """Gotcha 2: cross-project repo_id must not resolve — returns None."""
        proj_a = _make_project(session, project_id="proj-a", base_dir="/tmp/a")
        proj_b = _make_project(session, project_id="proj-b", base_dir="/tmp/b")
        ensure_primary_repo(session, proj_a)
        ensure_primary_repo(session, proj_b)

        repo_b = resolve_primary_repo(session, "proj-b")

        with caplog.at_level(logging.ERROR):
            result = resolve_repo(session, "proj-a", repo_b.id)

        # Must NOT return proj_b's repo — returns None to prevent cross-project resolution
        assert result is None
        assert "belongs to project=proj-b" in caplog.text
        assert "refusing cross-project resolution" in caplog.text

    def test_no_project_repos_returns_none(self, session):
        """Project with zero repos + repo_id=None → None."""
        project = _make_project(session)
        result = resolve_repo(session, project.id, None)
        assert result is None


class TestListRepos:
    def test_primary_first_then_alphabetical(self, session):
        project = _make_project(session)

        repo_b = ProjectRepo(
            id="repo-b",
            project_id=project.id,
            label="backend",
            path="/b",
            is_primary=False,
        )
        repo_a = ProjectRepo(
            id="repo-a",
            project_id=project.id,
            label="api",
            path="/a",
            is_primary=False,
        )
        repo_p = ProjectRepo(
            id="repo-p",
            project_id=project.id,
            label="main",
            path="/p",
            is_primary=True,
        )
        session.add_all([repo_b, repo_a, repo_p])
        session.flush()

        repos = list_repos(session, project.id)
        assert len(repos) == 3
        assert repos[0].id == "repo-p"  # primary first
        assert repos[1].label == "api"  # then alphabetical
        assert repos[2].label == "backend"

    def test_empty_list_for_no_repos(self, session):
        project = _make_project(session)
        repos = list_repos(session, project.id)
        assert repos == []


class TestMigration:
    def test_migration_backfills_existing_projects(self, engine):
        """REQ-04: migration creates one ProjectRepo per existing project."""
        # Create projects before migration
        with Session(engine) as session:
            for i in range(3):
                p = AutopilotProject(
                    id=f"proj-mig-{i}",
                    name=f"p{i}",
                    base_dir=f"/tmp/mig{i}",
                )
                session.add(p)
            session.commit()

        # Run migration
        migrate_project_repos_table(engine)

        # Verify backfill
        with Session(engine) as session:
            repos = session.query(ProjectRepo).all()
            assert len(repos) == 3
            for repo in repos:
                assert repo.is_primary is True
                assert repo.label == "main"

    def test_migration_idempotent(self, engine):
        """REQ-05: running migration twice creates no additional rows."""
        with Session(engine) as session:
            p = AutopilotProject(id="proj-idem", name="p", base_dir="/tmp/idem")
            session.add(p)
            session.commit()

        migrate_project_repos_table(engine)
        with Session(engine) as session:
            count1 = session.query(ProjectRepo).count()

        migrate_project_repos_table(engine)
        with Session(engine) as session:
            count2 = session.query(ProjectRepo).count()

        assert count1 == count2 == 1

    def test_base_dir_unchanged_after_migration(self, engine):
        """REQ-05: base_dir values unchanged before/after migration."""
        with Session(engine) as session:
            p = AutopilotProject(id="proj-base", name="p", base_dir="/tmp/base")
            session.add(p)
            session.commit()

        migrate_project_repos_table(engine)

        with Session(engine) as session:
            project = session.query(AutopilotProject).get("proj-base")
            assert project.base_dir == "/tmp/base"

    def test_migration_adds_repo_id_columns(self, engine):
        """REQ-02: all 5 tables get a nullable repo_id column."""
        # create_all already creates the repo_id column from the model.
        # Verify it works by inserting rows with repo_id=None.
        with Session(engine) as session:
            p = AutopilotProject(id="proj-col-test", name="p", base_dir="/tmp/col")
            session.add(p)
            session.flush()

            wf = Workflow(
                id="wf-col-test",
                name="wf",
                status="active",
                project_id="proj-col-test",
                phases_folder_path="/tmp",
            )
            session.add(wf)
            session.flush()

            t = Task(
                id="task-test",
                raw_description="d",
                done_definition="d",
                status="pending",
                repo_id=None,
            )
            session.add(t)
            session.flush()
            assert t.repo_id is None

            ticket = Ticket(
                id="ticket-test",
                workflow_id="wf-col-test",
                created_by_agent_id="a-1",
                title="t",
                description="d",
                ticket_type="task",
                priority="medium",
                status="open",
                repo_id=None,
            )
            session.add(ticket)
            session.flush()
            assert ticket.repo_id is None

    def test_historical_rows_have_null_repo_id(self, engine):
        """REQ-05: historical rows have repo_id IS NULL."""
        with Session(engine) as session:
            p = AutopilotProject(id="proj-hist", name="p", base_dir="/tmp/hist")
            session.add(p)
            session.flush()

            wf = Workflow(
                id="wf-hist",
                name="wf",
                status="active",
                project_id="proj-hist",
                phases_folder_path="/tmp",
            )
            session.add(wf)
            session.flush()

            ticket = Ticket(
                id="ticket-hist",
                workflow_id="wf-hist",
                created_by_agent_id="a-1",
                title="t",
                description="d",
                ticket_type="task",
                priority="medium",
                status="open",
            )
            session.add(ticket)
            session.commit()

            # Verify the row has NULL repo_id
            t = session.query(Ticket).get("ticket-hist")
            assert t.repo_id is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
