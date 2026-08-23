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
                "created_at, updated_at, cost_total_usd, review_mode) "
                "VALUES ('proj-legacy', 'legacy', '/repos/legacy', 0, 0, "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 0.0, 0)"
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
                "created_at, updated_at, cost_total_usd, review_mode) "
                "VALUES ('proj-legacy2', 'legacy2', '/repos/legacy2', 0, 0, "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 0.0, 0)"
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
