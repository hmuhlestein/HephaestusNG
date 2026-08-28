"""Regression test for autopilot_designs column migration.

Verifies that the additive ALTER TABLE migration works on databases
that are missing the new columns (status, content_hash, feature_folder, completed_at).
"""

import datetime
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text


def test_autopilot_designs_migration_adds_columns():
    """Open a DB missing the new columns → run init → query succeeds."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        # Create a minimal DB with the OLD schema (no new columns)
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS autopilot_projects (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR(200) NOT NULL,
                    base_dir TEXT NOT NULL UNIQUE,
                    is_default BOOLEAN DEFAULT 0,
                    is_active BOOLEAN DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
            """)
            )
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS autopilot_designs (
                    id VARCHAR PRIMARY KEY,
                    project_id VARCHAR NOT NULL REFERENCES autopilot_projects(id) ON DELETE CASCADE,
                    filename VARCHAR(500) NOT NULL,
                    name VARCHAR(500) NOT NULL,
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    extension VARCHAR(10) NOT NULL DEFAULT '.md',
                    created_at DATETIME NOT NULL,
                    modified_at DATETIME
                )
            """)
            )
            conn.commit()

        # Verify the new columns DON'T exist yet
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(autopilot_designs)"))
            columns = {row[1] for row in result.fetchall()}
            assert "status" not in columns
            assert "content_hash" not in columns
            assert "feature_folder" not in columns
            assert "completed_at" not in columns

        # Now run the migration via DatabaseManager
        from src.core.database import DatabaseManager

        db_manager = DatabaseManager(str(db_path))
        db_manager.create_tables()

        # Verify the new columns exist after migration
        with db_manager.engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(autopilot_designs)"))
            columns = {row[1] for row in result.fetchall()}
            assert "status" in columns
            assert "content_hash" in columns
            assert "feature_folder" in columns
            assert "completed_at" in columns

        # Verify we can query the table (ORM load works)
        session = db_manager.get_session()
        try:
            from src.core.database import AutopilotDesign

            designs = session.query(AutopilotDesign).all()
            assert designs == []  # Empty table, but no error
        finally:
            session.close()


def test_autopilot_designs_migration_idempotent():
    """Running migration twice doesn't break anything."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        from src.core.database import DatabaseManager

        # First run creates table + migrates
        db_manager1 = DatabaseManager(str(db_path))
        db_manager1.create_tables()

        # Second run should be idempotent
        db_manager2 = DatabaseManager(str(db_path))
        db_manager2.create_tables()

        # Verify columns exist
        with db_manager2.engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(autopilot_designs)"))
            columns = {row[1] for row in result.fetchall()}
            assert "status" in columns
            assert "content_hash" in columns


def test_autopilot_designs_fresh_db_has_columns():
    """Fresh DB (create_all) has all columns from the start."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        from src.core.database import DatabaseManager

        db_manager = DatabaseManager(str(db_path))
        db_manager.create_tables()

        with db_manager.engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(autopilot_designs)"))
            columns = {row[1] for row in result.fetchall()}
            assert "status" in columns
            assert "content_hash" in columns
            assert "feature_folder" in columns
            assert "completed_at" in columns


def test_autopilot_designs_can_insert_and_query():
    """After migration, can insert and query designs with new columns."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        from src.core.database import AutopilotDesign, AutopilotProject, DatabaseManager

        db_manager = DatabaseManager(str(db_path))
        db_manager.create_tables()

        session = db_manager.get_session()
        try:
            # Create a project
            project = AutopilotProject(
                id="proj-test",
                name="Test Project",
                base_dir="/tmp/test",
                is_active=True,
            )
            session.add(project)

            # Create a design with new columns
            design = AutopilotDesign(
                id="des-test",
                project_id="proj-test",
                filename="test_design.md",
                name="Test Design",
                ordinal=0,
                size_bytes=100,
                extension=".md",
                content_hash="abc123",
                status="pending",
            )
            session.add(design)
            session.commit()

            # Query it back
            result = session.query(AutopilotDesign).filter_by(id="des-test").first()
            assert result is not None
            assert result.status == "pending"
            assert result.content_hash == "abc123"
            assert result.feature_folder is None
            assert result.completed_at is None

            # Update status
            result.status = "completed"
            session.commit()

            # Verify update
            result2 = session.query(AutopilotDesign).filter_by(id="des-test").first()
            assert result2.status == "completed"
        finally:
            session.close()


def test_autopilot_designs_source_dir_unique_constraint_enforced():
    """Two rows with the same (project_id, source_dir) must be rejected --
    this is the DB-level backstop for the _resolve_and_enqueue_speckit_feature
    double-enqueue race (design review round 4 BLOCKER)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        from sqlalchemy.exc import IntegrityError

        from src.core.database import AutopilotDesign, AutopilotProject, DatabaseManager

        db_manager = DatabaseManager(str(db_path))
        db_manager.create_tables()

        session = db_manager.get_session()
        try:
            session.add(AutopilotProject(id="proj-race", name="Race Project", base_dir="/tmp/race", is_active=True))
            session.add(
                AutopilotDesign(
                    id="des-race-1",
                    project_id="proj-race",
                    filename=None,
                    name="007-foo",
                    source_dir="/tmp/race/specs/007-foo",
                    status="pending",
                )
            )
            session.commit()

            session.add(
                AutopilotDesign(
                    id="des-race-2",
                    project_id="proj-race",
                    filename=None,
                    name="007-foo-dup",
                    source_dir="/tmp/race/specs/007-foo",
                    status="pending",
                )
            )
            try:
                session.commit()
                assert False, "second insert with the same (project_id, source_dir) should have raised IntegrityError"
            except IntegrityError:
                session.rollback()

            assert session.query(AutopilotDesign).filter_by(project_id="proj-race").count() == 1
        finally:
            session.close()


def test_migrate_speckit_design_source_dir_unique_resolves_pre_existing_duplicates():
    """A database that already has two rows sharing (project_id, source_dir)
    -- created by the pre-round-4 race -- must not fail the migration; the
    older duplicate is dropped and the constraint is applied cleanly."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        from sqlalchemy import MetaData, Table, UniqueConstraint
        from sqlalchemy.orm import sessionmaker

        from src.core.database import AutopilotDesign, AutopilotProject, DatabaseManager

        engine = create_engine(f"sqlite:///{db_path}")
        AutopilotProject.__table__.create(engine)

        # Build the OLD schema: current model's columns, but only the
        # filename constraint -- no source_dir uniqueness yet.
        old_cols = [c.copy() for c in AutopilotDesign.__table__.columns]
        meta = MetaData()
        old_table = Table(
            "autopilot_designs",
            meta,
            *old_cols,
            UniqueConstraint("project_id", "filename", name="uq_design_project_filename"),
        )
        old_table.create(engine)

        Session = sessionmaker(bind=engine)
        session = Session()
        now = datetime.datetime.utcnow()
        session.execute(
            AutopilotProject.__table__.insert().values(
                id="proj-old", name="Old Project", base_dir="/tmp/old", is_active=True, created_at=now, updated_at=now
            )
        )
        session.execute(
            old_table.insert().values(
                id="des-old-1",
                project_id="proj-old",
                filename=None,
                name="007-foo",
                ordinal=0,
                size_bytes=0,
                extension=".md",
                status="pending",
                created_at=now,
                cost_total_usd=0.0,
                source_dir="/tmp/old/specs/007-foo",
            )
        )
        session.execute(
            old_table.insert().values(
                id="des-old-2",
                project_id="proj-old",
                filename=None,
                name="007-foo-dup",
                ordinal=1,
                size_bytes=0,
                extension=".md",
                status="pending",
                created_at=now,
                cost_total_usd=0.0,
                source_dir="/tmp/old/specs/007-foo",
            )
        )
        session.commit()
        session.close()
        engine.dispose()

        # Running the full migration chain must not raise, must drop the
        # older duplicate, and must leave the new constraint in place.
        db_manager = DatabaseManager(str(db_path))
        db_manager.create_tables()

        with db_manager.engine.connect() as conn:
            rows = conn.execute(text("SELECT id FROM autopilot_designs")).fetchall()
            assert [r[0] for r in rows] == ["des-old-1"]

            create_sql = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='autopilot_designs'")
            ).scalar()
            assert "uq_design_project_source_dir" in create_sql
