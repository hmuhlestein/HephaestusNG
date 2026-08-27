"""Atomicity of the autopilot_designs filename-nullable rebuild.

Adversarial review BLOCKER: a prior version ran the rename in its own
committed transaction, then created/copied/dropped in a second one -- a
crash between the two left autopilot_designs_old holding every design
row with no autopilot_designs table pointing back at it (silent total
data loss). migrate_speckit_design_columns now drives the whole
create+copy+drop+rename sequence as one manually-controlled transaction
on a raw DBAPI connection (isolation_level=None, explicit BEGIN/COMMIT/
ROLLBACK) -- plain SQLAlchemy engine.begin() does NOT make DDL atomic
under pysqlite's default driver.

A second, independent bug surfaced while fixing the first: the initial
atomic rewrite still renamed autopilot_designs itself away (to
autopilot_designs_old) before rebuilding. SQLite's ALTER TABLE RENAME
auto-updates OTHER tables' FK definitions to follow the renamed table
(documented since 3.25.0) -- so under real FK enforcement (which
DatabaseManager's engine always has on), the moment autopilot_designs
was renamed, features.design_id's FK text became "REFERENCES
autopilot_designs_old", and dropping that table afterward left
features permanently referencing a table that no longer exists --
caught here by PRAGMA foreign_key_check, not merely an enforcement-
pragma inconvenience. The fix never renames the table other tables
reference: it builds the replacement under a temp name, drops the
original, then renames the temp table into the final name.
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
                assert "autopilot_designs_new" not in tables
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
        """Simulate a crash right after computing the column list: the CREATE
        TABLE step raises. The transaction must roll back in full --
        autopilot_designs must still exist with its original row, not be
        left dropped or half-replaced.
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
                assert "autopilot_designs_new" not in tables  # rollback undid the create too

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

    def test_rebuild_preserves_foreign_keys_under_real_fk_enforcement(self):
        """DatabaseManager's engine always runs with PRAGMA foreign_keys=ON.
        An earlier fix renamed autopilot_designs itself away before
        rebuilding -- SQLite's ALTER TABLE RENAME auto-updates OTHER
        tables' FK definitions to follow the renamed table, so
        features.design_id silently started pointing at the soon-to-be-
        dropped old table, and PRAGMA foreign_key_check flagged a real,
        permanent integrity break. This DB has features/project_repos/
        workflows tables (the actual FK targets/dependents in production)
        and FK enforcement genuinely on, not just present in the schema.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            setup_engine = create_engine(f"sqlite:///{db_path}")
            with setup_engine.connect() as conn:
                conn.execute(
                    text("""
                    CREATE TABLE autopilot_projects (
                        id VARCHAR PRIMARY KEY, name VARCHAR(200) NOT NULL, base_dir TEXT NOT NULL UNIQUE,
                        is_active BOOLEAN DEFAULT 0, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
                    )
                """)
                )
                conn.execute(
                    text("""
                    CREATE TABLE project_repos (
                        id VARCHAR PRIMARY KEY, project_id VARCHAR NOT NULL, label VARCHAR(100) NOT NULL,
                        path TEXT NOT NULL, is_primary BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME NOT NULL
                    )
                """)
                )
                conn.execute(text("CREATE TABLE workflows (id VARCHAR PRIMARY KEY)"))
                conn.execute(
                    text("""
                    CREATE TABLE autopilot_designs (
                        id VARCHAR PRIMARY KEY,
                        project_id VARCHAR NOT NULL REFERENCES autopilot_projects(id) ON DELETE CASCADE,
                        filename VARCHAR(500) NOT NULL, name VARCHAR(500) NOT NULL,
                        ordinal INTEGER NOT NULL DEFAULT 0, size_bytes INTEGER NOT NULL DEFAULT 0,
                        extension VARCHAR(10) NOT NULL DEFAULT '.md', content_hash VARCHAR(64),
                        status VARCHAR(20) NOT NULL DEFAULT 'pending', feature_folder TEXT, completed_at DATETIME,
                        created_at DATETIME NOT NULL, modified_at DATETIME, file_path TEXT, designs_folder TEXT,
                        phase0_workflow_id VARCHAR, error TEXT, cost_total_usd FLOAT NOT NULL DEFAULT 0.0,
                        workflow_type VARCHAR(20) NOT NULL DEFAULT 'feature', archived_at DATETIME
                    )
                """)
                )
                conn.execute(
                    text("""
                    CREATE TABLE features (
                        id VARCHAR PRIMARY KEY,
                        design_id VARCHAR NOT NULL REFERENCES autopilot_designs(id),
                        feature_key VARCHAR(100) NOT NULL, name VARCHAR NOT NULL, scope TEXT NOT NULL,
                        execution VARCHAR NOT NULL DEFAULT 'parallel', status VARCHAR NOT NULL DEFAULT 'pending',
                        created_at DATETIME NOT NULL
                    )
                """)
                )
                conn.execute(text("INSERT INTO autopilot_projects VALUES ('proj-1','p1','/tmp/p1',0,'2026-01-01','2026-01-01')"))
                conn.execute(
                    text("""INSERT INTO autopilot_designs
                    (id, project_id, filename, name, ordinal, size_bytes, extension, content_hash, status,
                     feature_folder, completed_at, created_at, modified_at, file_path, designs_folder,
                     phase0_workflow_id, error, cost_total_usd, workflow_type, archived_at)
                    VALUES ('des-1','proj-1','design.md','Design',0,0,'.md',NULL,'pending',
                     NULL,NULL,'2026-01-01',NULL,NULL,NULL,NULL,NULL,0.0,'feature',NULL)""")
                )
                conn.execute(text("INSERT INTO features (id, design_id, feature_key, name, scope, created_at) VALUES ('feat-1','des-1','k','Feat','s','2026-01-01')"))
                conn.commit()
            setup_engine.dispose()

            from sqlalchemy import event

            engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

            @event.listens_for(engine, "connect")
            def _enable_fk(dbapi_connection, connection_record):
                cur = dbapi_connection.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

            from src.core.schema_migrations import migrate_speckit_design_columns

            migrate_speckit_design_columns(engine)

            with engine.connect() as conn:
                assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1  # restored, not left off

                info = conn.execute(text("PRAGMA table_info(autopilot_designs)")).fetchall()
                filename_col = next(row for row in info if row[1] == "filename")
                assert filename_col[3] == 0

                assert conn.execute(text("SELECT id FROM autopilot_designs")).fetchall() == [("des-1",)]
                assert conn.execute(text("SELECT id, design_id FROM features")).fetchall() == [("feat-1", "des-1")]

                # The real integrity check the earlier bug tripped: a dangling
                # FK reference to a dropped table shows up here even with
                # enforcement back on.
                assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []

                fk_list = conn.execute(text("PRAGMA foreign_key_list(features)")).fetchall()
                assert fk_list[0][2] == "autopilot_designs"  # still targets the live table, not a dropped one

                # A genuinely live FK: a new child row referencing des-1 must
                # still be insertable under real enforcement.
                conn.execute(text("INSERT INTO features (id, design_id, feature_key, name, scope, created_at) VALUES ('feat-2','des-1','k2','Feat2','s','2026-01-01')"))
                conn.commit()
            engine.dispose()


def test_add_column_failure_other_than_duplicate_is_not_silently_swallowed(monkeypatch, caplog):
    """Adversarial review WARNING: the three ALTER TABLE ADD COLUMN steps
    used to catch bare Exception and always assume "column already
    exists". A real failure (disk full, locked table, permissions) must
    log/propagate instead of vanishing with zero signal.
    """
    import logging

    import src.core.schema_migrations as sm

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE autopilot_designs (id VARCHAR PRIMARY KEY, filename VARCHAR(500) NOT NULL)"))
            conn.execute(text("CREATE TABLE autopilot_projects (id VARCHAR PRIMARY KEY)"))
            conn.commit()

        original_execute = engine.__class__.connect

        class _FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, stmt):
                sql = str(stmt)
                if "ADD COLUMN repo_id" in sql:
                    raise RuntimeError("disk I/O error")  # not a duplicate-column message
                raise AssertionError(f"unexpected statement reached fake connection: {sql}")

            def commit(self):
                pass

        def _fake_connect(self):
            return _FakeConn()

        monkeypatch.setattr(engine.__class__, "connect", _fake_connect)
        with caplog.at_level(logging.WARNING):
            sm.migrate_speckit_design_columns(engine)
        monkeypatch.setattr(engine.__class__, "connect", original_execute)

        assert any("disk I/O error" in r.message and "check this" in r.message for r in caplog.records)
