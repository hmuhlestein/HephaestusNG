"""Atomicity of the autopilot_designs filename-nullable rebuild.

Adversarial review BLOCKER: a prior version ran the rename in its own
committed transaction, then created/copied/dropped in a second one -- a
crash between the two left autopilot_designs_old holding every design
row with no autopilot_designs table pointing back at it (silent total
data loss). migrate_speckit_design_columns now runs the whole
rename+create+copy+drop sequence inside a single engine.begin()
transaction, which SQLite rolls back in full (DDL included) if
anything in the middle raises.
"""

import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text


def _create_old_schema_db(db_path):
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.execute(
            text("""
            CREATE TABLE autopilot_projects (
                id VARCHAR PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                base_dir TEXT NOT NULL UNIQUE,
                is_active BOOLEAN DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """)
        )
        # Mirrors the real pre-migration schema: every OTHER column this
        # migration doesn't touch already exists (added by earlier,
        # already-applied migrations in SCHEMA_MIGRATIONS -- this one is
        # registered last), only filename is still NOT NULL and repo_id/
        # source_dir are still missing.
        conn.execute(
            text("""
            CREATE TABLE autopilot_designs (
                id VARCHAR PRIMARY KEY,
                project_id VARCHAR NOT NULL REFERENCES autopilot_projects(id) ON DELETE CASCADE,
                filename VARCHAR(500) NOT NULL,
                name VARCHAR(500) NOT NULL,
                ordinal INTEGER NOT NULL DEFAULT 0,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                extension VARCHAR(10) NOT NULL DEFAULT '.md',
                content_hash VARCHAR(64),
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                feature_folder TEXT,
                completed_at DATETIME,
                created_at DATETIME NOT NULL,
                modified_at DATETIME,
                file_path TEXT,
                designs_folder TEXT,
                phase0_workflow_id VARCHAR,
                error TEXT,
                cost_total_usd FLOAT NOT NULL DEFAULT 0.0,
                workflow_type VARCHAR(20) NOT NULL DEFAULT 'feature',
                archived_at DATETIME
            )
        """)
        )
        conn.execute(
            text("""
            INSERT INTO autopilot_projects (id, name, base_dir, created_at, updated_at)
            VALUES ('proj-1', 'proj-1', '/tmp/proj-1', '2026-01-01', '2026-01-01')
        """)
        )
        conn.execute(
            text("""
            INSERT INTO autopilot_designs (id, project_id, filename, name, created_at)
            VALUES ('des-1', 'proj-1', 'design.md', 'Design One', '2026-01-01')
        """)
        )
        conn.commit()
    engine.dispose()


class TestMigrationRebuildsFilenameNullable:
    def test_rebuild_makes_filename_nullable_and_preserves_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            _create_old_schema_db(db_path)

            from src.core.schema_migrations import migrate_speckit_design_columns

            engine = create_engine(f"sqlite:///{db_path}")
            migrate_speckit_design_columns(engine)

            with engine.connect() as conn:
                info = conn.execute(text("PRAGMA table_info(autopilot_designs)")).fetchall()
                filename_col = next(row for row in info if row[1] == "filename")
                assert filename_col[3] == 0  # notnull flag cleared

                rows = conn.execute(text("SELECT id, filename, name FROM autopilot_designs")).fetchall()
                assert len(rows) == 1
                assert rows[0][0] == "des-1"
                assert rows[0][1] == "design.md"

                tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
                assert "autopilot_designs_old" not in tables
            engine.dispose()

    def test_idempotent_second_run_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            _create_old_schema_db(db_path)

            from src.core.schema_migrations import migrate_speckit_design_columns

            engine = create_engine(f"sqlite:///{db_path}")
            migrate_speckit_design_columns(engine)
            migrate_speckit_design_columns(engine)  # must not raise or re-rebuild

            with engine.connect() as conn:
                rows = conn.execute(text("SELECT id FROM autopilot_designs")).fetchall()
                assert len(rows) == 1
            engine.dispose()

    def test_crash_mid_rebuild_rolls_back_leaving_original_table_intact(self, monkeypatch):
        """Simulate a crash between the RENAME and the INSERT: the CREATE
        TABLE step raises. The transaction must roll back in full --
        autopilot_designs must still exist with its original row, not be
        left renamed away with nothing usable in its place.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            _create_old_schema_db(db_path)

            import sqlalchemy.schema as sa_schema

            from src.core.schema_migrations import migrate_speckit_design_columns

            original_create_table = sa_schema.CreateTable

            def _boom(*args, **kwargs):
                raise RuntimeError("simulated crash mid-migration")

            monkeypatch.setattr(sa_schema, "CreateTable", _boom)

            engine = create_engine(f"sqlite:///{db_path}")
            migrate_speckit_design_columns(engine)  # logs a warning, does not raise

            monkeypatch.setattr(sa_schema, "CreateTable", original_create_table)

            with engine.connect() as conn:
                tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
                assert "autopilot_designs" in tables
                assert "autopilot_designs_old" not in tables  # rollback undid the rename too

                rows = conn.execute(text("SELECT id, filename FROM autopilot_designs")).fetchall()
                assert len(rows) == 1
                assert rows[0][0] == "des-1"

                info = conn.execute(text("PRAGMA table_info(autopilot_designs)")).fetchall()
                filename_col = next(row for row in info if row[1] == "filename")
                assert filename_col[3] == 1  # still NOT NULL -- rebuild never committed
            engine.dispose()

            # A later, un-sabotaged run recovers cleanly.
            engine2 = create_engine(f"sqlite:///{db_path}")
            migrate_speckit_design_columns(engine2)
            with engine2.connect() as conn:
                info = conn.execute(text("PRAGMA table_info(autopilot_designs)")).fetchall()
                filename_col = next(row for row in info if row[1] == "filename")
                assert filename_col[3] == 0
                rows = conn.execute(text("SELECT id FROM autopilot_designs")).fetchall()
                assert len(rows) == 1
            engine2.dispose()
