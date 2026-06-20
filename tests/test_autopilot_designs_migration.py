"""Regression test for autopilot_designs column migration.

Verifies that the additive ALTER TABLE migration works on databases
that are missing the new columns (status, content_hash, feature_folder, completed_at).
"""

import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


def test_autopilot_designs_migration_adds_columns():
    """Open a DB missing the new columns → run init → query succeeds."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        # Create a minimal DB with the OLD schema (no new columns)
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS autopilot_projects (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR(200) NOT NULL,
                    base_dir TEXT NOT NULL UNIQUE,
                    is_default BOOLEAN DEFAULT 0,
                    is_active BOOLEAN DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
            """))
            conn.execute(text("""
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
            """))
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

        from src.core.database import DatabaseManager, AutopilotProject, AutopilotDesign
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
