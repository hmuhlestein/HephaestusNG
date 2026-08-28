"""Tests for the speckit_auto_scan column migration (REQ-16)."""

from src.core.database import AutopilotProject, DatabaseManager
from src.core.schema_migrations import migrate_speckit_auto_scan_flag_column


def test_new_projects_default_speckit_auto_scan_to_false(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()

    with db.session_scope() as session:
        proj = AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path))
        session.add(proj)
        session.flush()
        assert proj.speckit_auto_scan is False


def test_migration_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()

    migrate_speckit_auto_scan_flag_column(db.engine)
    migrate_speckit_auto_scan_flag_column(db.engine)  # must not raise
