"""Regression: the autopilot_designs rebuild-and-swap migrations left every
table that references autopilot_designs pointing at a table they then dropped.

Since SQLite 3.25, `ALTER TABLE x RENAME TO y` also rewrites the foreign key
clauses of every OTHER table referencing x. The rebuild idiom
(rename -> recreate -> copy -> drop) therefore rewrote workflows.design_id and
features.design_id to REFERENCES "autopilot_designs_old"(id), and then dropped
autopilot_designs_old. Under PRAGMA foreign_keys=ON, every INSERT into either
table fails with "no such table: main.autopilot_designs_old" -- so no workflow
could be created, and no design in any project could start. Observed live:
Phase 0 launched, created its worktree, and died on the workflow INSERT.
"""

import sqlite3

import pytest
from sqlalchemy import create_engine, text

from src.core.schema_migrations import repair_dangling_autopilot_designs_fk


def _rebuild_the_old_way(conn):
    """The rename-recreate-copy-drop the migrations did before the
    legacy_alter_table guard -- i.e. how a real database got into this state."""
    conn.execute("ALTER TABLE autopilot_designs RENAME TO autopilot_designs_old")
    conn.execute("CREATE TABLE autopilot_designs (id TEXT PRIMARY KEY, filename TEXT)")
    conn.execute("INSERT INTO autopilot_designs SELECT id, filename FROM autopilot_designs_old")
    conn.execute("DROP TABLE autopilot_designs_old")


@pytest.fixture
def broken_db(tmp_path):
    db_path = tmp_path / "broken.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE autopilot_designs (id TEXT PRIMARY KEY, filename TEXT NOT NULL);
        CREATE TABLE workflows (
            id TEXT PRIMARY KEY,
            design_id TEXT REFERENCES autopilot_designs(id)
        );
        CREATE TABLE features (
            id TEXT PRIMARY KEY,
            design_id TEXT REFERENCES autopilot_designs(id)
        );
        INSERT INTO autopilot_designs VALUES ('des-1', 'a.md');
        """
    )
    _rebuild_the_old_way(conn)
    conn.close()
    return db_path


def _connect(db_path, foreign_keys=True):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
    return conn


def test_the_break_is_real(broken_db):
    """Pins the failure this repairs -- without it the repair test below could
    pass against a database that was never broken in the first place."""
    conn = _connect(broken_db)
    with pytest.raises(sqlite3.OperationalError, match="autopilot_designs_old"):
        conn.execute("INSERT INTO workflows VALUES ('wf-1', 'des-1')")


def test_repair_restores_inserts_and_keeps_enforcement(broken_db):
    engine = create_engine(f"sqlite:///{broken_db}")
    repair_dangling_autopilot_designs_fk(engine)
    engine.dispose()

    conn = _connect(broken_db)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    conn.execute("INSERT INTO workflows VALUES ('wf-1', 'des-1')")
    conn.execute("INSERT INTO features VALUES ('feat-1', 'des-1')")

    # Repointed, not merely disabled: a design_id that does not exist must
    # still be rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO workflows VALUES ('wf-2', 'des-nonexistent')")


def test_repair_is_a_no_op_on_a_healthy_database(tmp_path):
    db_path = tmp_path / "healthy.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE autopilot_designs (id TEXT PRIMARY KEY);
        CREATE TABLE workflows (id TEXT PRIMARY KEY, design_id TEXT REFERENCES autopilot_designs(id));
        """
    )
    before = conn.execute("SELECT sql FROM sqlite_master WHERE name='workflows'").fetchone()[0]
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}")
    repair_dangling_autopilot_designs_fk(engine)
    engine.dispose()

    conn = sqlite3.connect(db_path)
    after = conn.execute("SELECT sql FROM sqlite_master WHERE name='workflows'").fetchone()[0]
    assert after == before


def test_repair_defers_while_a_rebuild_is_still_mid_swap(tmp_path):
    """A surviving autopilot_designs_old means the rows are still in the old
    table -- repointing the references then would strand them. That case
    belongs to _resume_interrupted_autopilot_designs_rebuild, which runs
    first."""
    db_path = tmp_path / "midswap.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE autopilot_designs (id TEXT PRIMARY KEY);
        CREATE TABLE workflows (id TEXT PRIMARY KEY, design_id TEXT REFERENCES autopilot_designs(id));
        """
    )
    conn.execute("ALTER TABLE autopilot_designs RENAME TO autopilot_designs_old")
    conn.execute("CREATE TABLE autopilot_designs (id TEXT PRIMARY KEY)")
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}")
    repair_dangling_autopilot_designs_fk(engine)
    engine.dispose()

    conn = sqlite3.connect(db_path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='workflows'").fetchone()[0]
    assert "autopilot_designs_old" in sql


def test_the_rebuild_migration_no_longer_breaks_referencing_tables(tmp_path):
    """The other half of the fix: with legacy_alter_table set, the rename
    leaves referencing tables alone, so the repair above has nothing to do on
    any database migrated from here on."""
    from src.core.schema_migrations import migrate_speckit_design_columns

    db_path = tmp_path / "fresh.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE autopilot_designs (
            id VARCHAR PRIMARY KEY,
            project_id VARCHAR,
            filename VARCHAR NOT NULL,
            name VARCHAR,
            ordinal INTEGER,
            size_bytes INTEGER,
            extension VARCHAR,
            status VARCHAR,
            created_at DATETIME
        );
        CREATE TABLE workflows (id TEXT PRIMARY KEY, design_id TEXT REFERENCES autopilot_designs(id));
        """
    )
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}")
    migrate_speckit_design_columns(engine)
    engine.dispose()

    conn = sqlite3.connect(db_path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='workflows'").fetchone()[0]
    assert "autopilot_designs_old" not in sql
