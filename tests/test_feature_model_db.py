"""Tests for Feature model database schema and migration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.database import AutopilotDesign, DatabaseManager, Feature, Workflow


def test_feature_table_exists():
    """Feature table should be created on startup."""
    db = DatabaseManager(":memory:")
    db.create_tables()
    assert Feature.__tablename__ == "features"


def test_feature_columns():
    """Feature should have all required columns."""
    db = DatabaseManager(":memory:")
    db.create_tables()
    col_names = {c.name for c in Feature.__table__.columns}
    expected = {
        "id", "design_id", "feature_key", "name", "scope", "files",
        "depends_on", "execution", "status", "workflow_id",
        "scope_doc_path", "feature_record_path",
        "created_at", "started_at", "completed_at", "error",
    }
    assert expected.issubset(col_names)


def test_autopilot_design_new_columns():
    """AutopilotDesign should have file_path, designs_folder, phase0_workflow_id."""
    db = DatabaseManager(":memory:")
    db.create_tables()
    col_names = {c.name for c in AutopilotDesign.__table__.columns}
    assert "file_path" in col_names
    assert "designs_folder" in col_names
    assert "phase0_workflow_id" in col_names


def test_workflow_new_columns():
    """Workflow should have workflow_type and feature_id."""
    db = DatabaseManager(":memory:")
    db.create_tables()
    col_names = {c.name for c in Workflow.__table__.columns}
    assert "workflow_type" in col_names
    assert "feature_id" in col_names


def test_migration_idempotent():
    """Calling migration twice should not error."""
    db = DatabaseManager(":memory:")
    db.create_tables()
    db._migrate_feature_model_columns()
    db._migrate_feature_model_columns()


def test_feature_default_status():
    """Feature status should default to 'pending'."""
    db = DatabaseManager(":memory:")
    db.create_tables()
    session = db.get_session()
    try:
        feat = Feature(
            id="test-123",
            design_id="des-test",
            feature_key="auth",
            name="Auth",
            scope="Authentication",
            execution="parallel",
        )
        session.add(feat)
        session.commit()
        loaded = session.query(Feature).get("test-123")
        assert loaded.status == "pending"
        assert loaded.execution == "parallel"
    finally:
        session.close()
