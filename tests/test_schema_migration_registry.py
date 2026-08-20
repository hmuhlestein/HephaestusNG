"""Tests for DatabaseManager's schema_migrations bookkeeping (SOLID review
finding 4.1: "no migration registry, no schema-version table").

_run_schema_migration wraps each of the 18 existing _migrate_* methods --
their own internal idempotency (ALTER TABLE, skip if the column already
exists) is unchanged. What's new: a schema_migrations row recorded per
migration id, so create_tables() doesn't unconditionally re-run and
re-log all 18 on every single app startup, and a genuine failure (not
just "already exists") is now logged at WARNING instead of silently at
DEBUG.
"""

from unittest.mock import Mock

import pytest

from src.core.database import DatabaseManager, SchemaMigration


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def test_all_18_migrations_recorded_after_create_tables(db_manager):
    with db_manager.session_scope() as session:
        recorded_ids = {
            row.id for row in session.query(SchemaMigration).all()
        }

    expected = {
        "_migrate_task_dependency_columns",
        "_migrate_autopilot_designs_columns",
        "_migrate_feature_model_columns",
        "_migrate_total_gotos_column",
        "_migrate_workflow_gotos_reset_at_column",
        "_migrate_task_retry_count_column",
        "_migrate_phase_retry_count_column",
        "_migrate_self_review_columns",
        "_migrate_phase_execution_task_claim_column",
        "_migrate_autopilot_designs_error_column",
        "_migrate_workflow_paused_by_column",
        "_migrate_workflow_status_reason_column",
        "_migrate_workflow_paused_at_column",
        "_migrate_workflow_paused_retry_count_column",
        "_migrate_task_action_target_phase_column",
        "_migrate_cost_tracking_columns",
        "_migrate_phase_fallback_columns",
        "_migrate_review_mode_columns",
    }
    assert expected <= recorded_ids


def test_second_create_tables_call_does_not_rerun_already_recorded_migrations(db_manager):
    """The actual point of the registry: a migration already recorded as
    attempted must not be re-run on a later startup."""
    calls = []
    original = db_manager._migrate_self_review_columns
    db_manager._migrate_self_review_columns = lambda: (calls.append(1), original())[1]

    db_manager.create_tables()  # second call, same (already-migrated) database

    assert calls == [], "already-recorded migration was re-run"


def test_migration_not_yet_recorded_still_runs_on_a_fresh_db(tmp_path, monkeypatch):
    """Sanity check for the above: confirms the spy itself would have
    caught a real re-run, by observing it fire once on a genuinely fresh
    database."""
    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))

    calls = []
    original = db._migrate_self_review_columns
    db._migrate_self_review_columns = lambda: (calls.append(1), original())[1]

    db.create_tables()

    assert calls == [1]


def test_failed_migration_is_not_recorded_and_retries_next_startup(db_manager, caplog):
    """A migration that raises despite its own internal handling must be
    logged loudly (not silently at debug) and must NOT be marked applied,
    so a genuine failure gets another chance next startup instead of
    being masked forever."""
    boom = Mock(side_effect=RuntimeError("simulated real failure, not 'already exists'"))

    import logging

    with caplog.at_level(logging.WARNING, logger="src.core.database"):
        db_manager._run_schema_migration("_fake_migration_for_test", boom)

    with db_manager.session_scope() as session:
        recorded = (
            session.query(SchemaMigration)
            .filter_by(id="_fake_migration_for_test")
            .first()
        )
    assert recorded is None, "a failed migration must not be recorded as applied"
    assert any(
        "_fake_migration_for_test" in r.message and "failed" in r.message.lower()
        for r in caplog.records
    ), "the failure must be logged at warning level, not silently"

    # And it retries on the next attempt (not permanently skipped).
    ok = Mock()
    db_manager._run_schema_migration("_fake_migration_for_test", ok)
    ok.assert_called_once()
    with db_manager.session_scope() as session:
        recorded = (
            session.query(SchemaMigration)
            .filter_by(id="_fake_migration_for_test")
            .first()
        )
    assert recorded is not None


def test_running_a_migration_twice_directly_is_still_idempotent(db_manager):
    """_run_schema_migration is new bookkeeping around the existing
    methods, which must keep being safe to call more than once (e.g. a
    fresh app version running against a database that already has these
    columns from a previous version's un-registered migration calls)."""
    db_manager._run_schema_migration(
        "_migrate_self_review_columns", db_manager._migrate_self_review_columns
    )
    with db_manager.session_scope() as session:
        count = (
            session.query(SchemaMigration)
            .filter_by(id="_migrate_self_review_columns")
            .count()
        )
    assert count == 1, "recording the same migration id twice must not create duplicate rows"
