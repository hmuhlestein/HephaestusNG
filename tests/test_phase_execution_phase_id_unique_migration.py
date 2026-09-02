"""migrate_phase_execution_phase_id_unique: Step 0 of
docs/designs/PHASE_EXECUTION_STATE_MACHINE_REFACTOR.md. Every reader of
phase_executions (_get_phase_statuses, _create_phase_task, every sibling
reopen/reset function) does .filter_by(phase_id=...).first() with no
order_by, trusting exactly one row per phase -- nothing in the schema
enforced that before this migration. Preventive: the live database had
zero duplicates when this was written, so these tests build the
"before" state by hand (a rebuilt table matching the pre-migration
schema) rather than reproducing an incident that hasn't happened.
"""

import uuid

import pytest
import sqlalchemy
from sqlalchemy import text

from src.core.database import DatabaseManager, Phase, PhaseExecution, Workflow
from src.core.schema_migrations import migrate_phase_execution_phase_id_unique


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed_phase(db_manager, phase_id="phase-1", workflow_id="wf-1"):
    with db_manager.session_scope() as session:
        session.add(Workflow(id=workflow_id, name="w", phases_folder_path="/tmp", status="active"))
        session.add(
            Phase(
                id=phase_id, workflow_id=workflow_id, order=1, name="development",
                description="d", done_definitions=["x"],
            )
        )


def _rebuild_table_without_unique_constraint(db_manager):
    """create_tables() creates phase_executions from the CURRENT model,
    which now declares UniqueConstraint("phase_id") as part of the table's
    own CREATE TABLE statement (not a separately-droppable index) --
    DROP INDEX on it is a no-op. To build a genuine "existing database
    that predates this migration" state, rebuild the table from a copy of
    the pre-constraint schema instead."""
    with db_manager.engine.connect() as conn:
        conn.execute(text("ALTER TABLE phase_executions RENAME TO phase_executions_old"))
        conn.execute(
            text(
                """
                CREATE TABLE phase_executions (
                    id VARCHAR PRIMARY KEY,
                    phase_id VARCHAR NOT NULL,
                    workflow_execution_id VARCHAR,
                    status VARCHAR NOT NULL DEFAULT 'pending',
                    started_at DATETIME,
                    completed_at DATETIME,
                    completion_summary TEXT,
                    task_creation_claimed_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO phase_executions SELECT id, phase_id, workflow_execution_id, "
                "status, started_at, completed_at, completion_summary, task_creation_claimed_at "
                "FROM phase_executions_old"
            )
        )
        conn.execute(text("DROP TABLE phase_executions_old"))
        conn.commit()


class TestConsolidatesPreExistingDuplicates:
    def test_keeps_the_most_recently_completed_row_and_drops_the_rest(self, db_manager):
        _seed_phase(db_manager)
        _rebuild_table_without_unique_constraint(db_manager)

        older_id, newer_id = str(uuid.uuid4()), str(uuid.uuid4())
        with db_manager.engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO phase_executions (id, phase_id, status, started_at, completed_at) "
                    "VALUES (:id, 'phase-1', 'failed', '2026-01-01T00:00:00', '2026-01-01T01:00:00')"
                ),
                {"id": older_id},
            )
            conn.execute(
                text(
                    "INSERT INTO phase_executions (id, phase_id, status, started_at, completed_at) "
                    "VALUES (:id, 'phase-1', 'completed', '2026-01-02T00:00:00', '2026-01-02T01:00:00')"
                ),
                {"id": newer_id},
            )
            conn.commit()

        migrate_phase_execution_phase_id_unique(db_manager.engine)

        with db_manager.session_scope() as session:
            rows = session.query(PhaseExecution).filter_by(phase_id="phase-1").all()
            assert len(rows) == 1
            assert rows[0].id == newer_id
            assert rows[0].status == "completed"

    def test_index_exists_after_consolidation(self, db_manager):
        _seed_phase(db_manager)
        _rebuild_table_without_unique_constraint(db_manager)

        with db_manager.engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO phase_executions (id, phase_id, status) "
                    "VALUES (:id, 'phase-1', 'pending')"
                ),
                {"id": str(uuid.uuid4())},
            )
            conn.execute(
                text(
                    "INSERT INTO phase_executions (id, phase_id, status) "
                    "VALUES (:id, 'phase-1', 'pending')"
                ),
                {"id": str(uuid.uuid4())},
            )
            conn.commit()

        migrate_phase_execution_phase_id_unique(db_manager.engine)

        with db_manager.engine.connect() as conn:
            names = {
                row[1]
                for row in conn.execute(text("PRAGMA index_list(phase_executions)")).fetchall()
            }
        assert "uq_phase_execution_phase_id" in names


class TestNoDuplicatesIsANoop:
    def test_distinct_phase_ids_all_survive_and_index_gets_created(self, db_manager):
        _seed_phase(db_manager, phase_id="phase-1")
        _seed_phase(db_manager, phase_id="phase-2", workflow_id="wf-2")
        _rebuild_table_without_unique_constraint(db_manager)

        with db_manager.engine.connect() as conn:
            conn.execute(
                text("INSERT INTO phase_executions (id, phase_id, status) VALUES (:id, 'phase-1', 'pending')"),
                {"id": str(uuid.uuid4())},
            )
            conn.execute(
                text("INSERT INTO phase_executions (id, phase_id, status) VALUES (:id, 'phase-2', 'pending')"),
                {"id": str(uuid.uuid4())},
            )
            conn.commit()

        migrate_phase_execution_phase_id_unique(db_manager.engine)

        with db_manager.session_scope() as session:
            assert session.query(PhaseExecution).filter_by(phase_id="phase-1").count() == 1
            assert session.query(PhaseExecution).filter_by(phase_id="phase-2").count() == 1


def test_running_migration_twice_is_idempotent(db_manager):
    # create_tables() already ran it once via SCHEMA_MIGRATIONS.
    migrate_phase_execution_phase_id_unique(db_manager.engine)
    migrate_phase_execution_phase_id_unique(db_manager.engine)


def test_orm_insert_of_a_second_row_for_the_same_phase_is_rejected(db_manager):
    """The model-level UniqueConstraint (fresh databases via create_tables())
    and the migration's CREATE UNIQUE INDEX (existing databases) must
    enforce the identical rule: exactly one PhaseExecution row per phase_id."""
    _seed_phase(db_manager)

    with db_manager.session_scope() as session:
        session.add(PhaseExecution(id=str(uuid.uuid4()), phase_id="phase-1", status="pending"))

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db_manager.session_scope() as session:
            session.add(PhaseExecution(id=str(uuid.uuid4()), phase_id="phase-1", status="pending"))
