"""Regression test for schema_migrations.migrate_self_review_columns'
development-phase backfill.

A Phase row created before src/phases/phase_manager.py started passing
self_review=phase_def.self_review (commit f3e6ab3) always has
self_review left at its Python None default. Phase.self_review is a
plain Column(JSON) (no none_as_null=True), so SQLAlchemy serializes that
None to the JSON text "null" -- a 4-char string, not a true SQL NULL.
The backfill migration's original `WHERE self_review IS NULL` never
matched those rows, so it silently did nothing for the exact rows it was
written to fix (observed live: 15 existing "development" phase rows all
still stored as the JSON text "null" after the migration had already run
on every backend startup since it landed).
"""

import pytest

from src.core.database import DatabaseManager, Phase, Workflow
from src.core.schema_migrations import migrate_self_review_columns


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed_workflow(db_manager):
    with db_manager.session_scope() as session:
        session.add(
            Workflow(id="wf-1", name="wf-1", status="active", phases_folder_path="/tmp")
        )


def test_backfills_phases_whose_self_review_is_the_json_null_literal(db_manager):
    """The actual shape found in production: typeof(self_review) == 'text',
    value == the 4-char string 'null'. Constructing via the ORM without
    passing self_review at all does NOT reproduce this in this SQLAlchemy/
    SQLite setup (verified: it writes a true SQL NULL instead) -- forcing
    the literal JSON text via raw SQL is the only reliable repro, and is
    exactly what makes this test fail against the original
    `WHERE self_review IS NULL` migration."""
    import sqlalchemy

    _seed_workflow(db_manager)
    with db_manager.engine.connect() as conn:
        conn.execute(
            sqlalchemy.text(
                """
                INSERT INTO phases (id, workflow_id, "order", name, description,
                    done_definitions, outputs, next_steps, working_directory,
                    self_review, retry_count)
                VALUES ('phase-json-null', 'wf-1', 1, 'development', 'd', '[]', '[]',
                    '[]', '/tmp', 'null', 0)
                """
            )
        )
        conn.commit()

    migrate_self_review_columns(db_manager.engine)

    with db_manager.session_scope() as session:
        phase = session.query(Phase).filter_by(id="phase-json-null").first()
        assert phase.self_review == {"enabled": True}


def test_backfills_phases_with_a_true_sql_null(db_manager):
    import sqlalchemy

    _seed_workflow(db_manager)
    with db_manager.engine.connect() as conn:
        conn.execute(
            sqlalchemy.text(
                """
                INSERT INTO phases (id, workflow_id, "order", name, description,
                    done_definitions, outputs, next_steps, working_directory,
                    self_review, retry_count)
                VALUES ('phase-sql-null', 'wf-1', 1, 'development', 'd', '[]', '[]',
                    '[]', '/tmp', NULL, 0)
                """
            )
        )
        conn.commit()

    migrate_self_review_columns(db_manager.engine)

    with db_manager.session_scope() as session:
        phase = session.query(Phase).filter_by(id="phase-sql-null").first()
        assert phase.self_review == {"enabled": True}


def test_does_not_overwrite_an_already_configured_self_review(db_manager):
    _seed_workflow(db_manager)
    with db_manager.session_scope() as session:
        session.add(
            Phase(id="phase-configured", workflow_id="wf-1", order=1, name="development",
                  description="d", done_definitions="[]", outputs="[]", next_steps="[]",
                  working_directory="/tmp", self_review={"enabled": False})
        )

    migrate_self_review_columns(db_manager.engine)

    with db_manager.session_scope() as session:
        phase = session.query(Phase).filter_by(id="phase-configured").first()
        assert phase.self_review == {"enabled": False}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
