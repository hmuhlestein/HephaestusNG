"""migrate_project_repos_table: idempotent backfill of one primary
ProjectRepo per existing AutopilotProject, without touching base_dir or
backfilling repo_id on historical rows. REQ-04, REQ-05."""

import pytest
import sqlalchemy

from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo, Task
from src.core.schema_migrations import migrate_project_repos_table


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def test_backfills_one_primary_repo_per_existing_project(db_manager):
    # create_tables() already ran the migration once via SCHEMA_MIGRATIONS;
    # simulate a "pre-migration" project by inserting straight into
    # autopilot_projects and deleting any auto-created project_repos row.
    with db_manager.engine.connect() as conn:
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO autopilot_projects (id, name, base_dir, is_default, is_active, "
                "created_at, updated_at, cost_total_usd, review_mode, speckit_auto_scan_enabled) "
                "VALUES ('proj-legacy', 'legacy', '/repos/legacy', 0, 0, "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 0.0, 0, 0)"
            )
        )
        conn.commit()

    migrate_project_repos_table(db_manager.engine)

    with db_manager.session_scope() as session:
        repos = session.query(ProjectRepo).filter_by(project_id="proj-legacy").all()
        assert len(repos) == 1
        assert repos[0].path == "/repos/legacy"
        assert repos[0].is_primary is True

        project = session.query(AutopilotProject).filter_by(id="proj-legacy").first()
        assert project.base_dir == "/repos/legacy"  # untouched


def test_running_migration_twice_is_a_noop(db_manager):
    with db_manager.engine.connect() as conn:
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO autopilot_projects (id, name, base_dir, is_default, is_active, "
                "created_at, updated_at, cost_total_usd, review_mode, speckit_auto_scan_enabled) "
                "VALUES ('proj-legacy2', 'legacy2', '/repos/legacy2', 0, 0, "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 0.0, 0, 0)"
            )
        )
        conn.commit()

    migrate_project_repos_table(db_manager.engine)
    with db_manager.session_scope() as session:
        count_after_first = session.query(ProjectRepo).filter_by(project_id="proj-legacy2").count()

    migrate_project_repos_table(db_manager.engine)
    with db_manager.session_scope() as session:
        count_after_second = session.query(ProjectRepo).filter_by(project_id="proj-legacy2").count()

    assert count_after_first == 1
    assert count_after_second == 1


def test_all_repo_id_columns_exist_and_are_nullable(db_manager):
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-x", name="x", base_dir="/repos/x"))
    with db_manager.session_scope() as session:
        # No repo_id passed -- must not require NOT NULL.
        session.add(
            Task(
                id="task-x",
                raw_description="d",
                done_definition="d",
                workflow_id=None,
            )
        )
    with db_manager.session_scope() as session:
        task = session.query(Task).filter_by(id="task-x").first()
        assert task.repo_id is None


def test_migration_does_not_backfill_repo_id_on_existing_rows(db_manager):
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-y", name="y", base_dir="/repos/y"))
        session.add(Task(id="task-y", raw_description="d", done_definition="d"))

    migrate_project_repos_table(db_manager.engine)

    with db_manager.session_scope() as session:
        task = session.query(Task).filter_by(id="task-y").first()
        assert task.repo_id is None


def test_uq_project_repos_one_primary_index_rejects_a_second_primary_row(db_manager):
    """BLOCKER fix: a partial unique index on (project_id) WHERE
    is_primary=1 must make it impossible to persist two primary
    ProjectRepo rows for the same project, regardless of caller
    discipline -- this is the DB-level backstop for the check-then-insert
    race in both add_project_repo and this migration's own backfill loop."""
    import sqlalchemy.exc

    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-race", name="race", base_dir="/repos/race"))
        session.add(
            ProjectRepo(id="repo-race-1", project_id="proj-race", label="one", path="/repos/race/one", is_primary=True)
        )

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db_manager.session_scope() as session:
            session.add(
                ProjectRepo(
                    id="repo-race-2", project_id="proj-race", label="two", path="/repos/race/two", is_primary=True
                )
            )


def test_concurrent_backfill_for_same_project_only_creates_one_primary_row(db_manager):
    """Simulates two racing migration runs (or a migration racing
    add_project_repo) both losing their has_primary check before either
    commits: manually insert a primary row for a project AFTER seeding it
    but BEFORE calling migrate_project_repos_table again, mimicking what a
    second concurrent runner's INSERT would have already committed. The
    migration's own insert must lose cleanly to the unique index rather
    than raising an uncaught error or creating a duplicate."""
    with db_manager.engine.connect() as conn:
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO autopilot_projects (id, name, base_dir, is_default, is_active, "
                "created_at, updated_at, cost_total_usd, review_mode, speckit_auto_scan_enabled) "
                "VALUES ('proj-concurrent', 'concurrent', '/repos/concurrent', 0, 0, "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 0.0, 0, 0)"
            )
        )
        conn.commit()

    with db_manager.session_scope() as session:
        session.add(
            ProjectRepo(
                id="repo-concurrent-winner",
                project_id="proj-concurrent",
                label="winner",
                path="/repos/concurrent",
                is_primary=True,
            )
        )

    # Should not raise, despite the has_primary check inside the migration
    # racing against a row that (in this simulation) already exists.
    migrate_project_repos_table(db_manager.engine)

    with db_manager.session_scope() as session:
        repos = session.query(ProjectRepo).filter_by(project_id="proj-concurrent").all()
        assert len(repos) == 1
        assert repos[0].id == "repo-concurrent-winner"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
