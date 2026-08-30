"""spec_key: one per-project identity for a design's source, whatever it is.

filename was doing two jobs -- naming a file in the queue dir, and answering
"have I queued this source already". Those coincide for a file-backed design
and not at all for a directory-backed one, so the Spec Kit autoscan
synthesized a path-shaped stand-in ("speckit/<repo>/<n>-<slug>.md") and stored
it in filename. Nothing existed at that path; three consumers treated it as
one anyway.
"""

import sqlite3

import pytest
from sqlalchemy import create_engine

from src.core.database import directory_spec_key
from src.core.schema_migrations import migrate_design_spec_key

PRE_MIGRATION_SCHEMA = """
CREATE TABLE autopilot_projects (id VARCHAR PRIMARY KEY, name VARCHAR, base_dir VARCHAR);
CREATE TABLE project_repos (
    id VARCHAR PRIMARY KEY, project_id VARCHAR, label VARCHAR, path VARCHAR, is_primary BOOLEAN
);
CREATE TABLE autopilot_designs (
    id VARCHAR PRIMARY KEY,
    project_id VARCHAR NOT NULL,
    filename VARCHAR(500),
    name VARCHAR(500) NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    extension VARCHAR(10) NOT NULL DEFAULT '.md',
    content_hash VARCHAR(64),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    feature_folder TEXT,
    completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_at DATETIME,
    file_path TEXT,
    designs_folder TEXT,
    phase0_workflow_id VARCHAR,
    error TEXT,
    cost_total_usd FLOAT DEFAULT 0.0,
    workflow_type VARCHAR(20) NOT NULL DEFAULT 'feature',
    source_dir TEXT,
    repo_id VARCHAR,
    archived_at DATETIME,
    CONSTRAINT uq_design_project_filename UNIQUE (project_id, filename),
    CONSTRAINT uq_design_project_source_dir UNIQUE (project_id, source_dir)
);
CREATE TABLE workflows (
    id VARCHAR PRIMARY KEY,
    design_id VARCHAR REFERENCES autopilot_designs(id)
);
INSERT INTO autopilot_projects VALUES ('proj-1', 'ParentChat', '/tmp/parent');
INSERT INTO project_repos VALUES ('repo-1', 'proj-1', 'front-end', '/tmp/parent/front-end', 1);
"""


def _design(**kw):
    cols = {
        "id": None, "project_id": "proj-1", "filename": None, "name": "d",
        "ordinal": 1, "size_bytes": 0, "extension": ".md", "content_hash": None,
        "status": "pending", "feature_folder": None, "completed_at": None,
        "modified_at": None, "file_path": None, "designs_folder": None,
        "phase0_workflow_id": None, "error": None, "cost_total_usd": 0.0,
        "workflow_type": "feature", "source_dir": None, "repo_id": None,
        "archived_at": None,
    }
    cols.update(kw)
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    return f"INSERT INTO autopilot_designs ({names}) VALUES ({marks})", list(cols.values())


@pytest.fixture
def migrated(tmp_path):
    db_path = tmp_path / "pre.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executescript(PRE_MIGRATION_SCHEMA)
    for kw in (
        {"id": "des-file", "filename": "01-auth.md", "name": "auth"},
        # What the autoscan used to write: a path that names nothing.
        {"id": "des-speckit", "filename": "speckit/_workspace/001-conversation-history.md",
         "name": "001-conversation-history"},
        {"id": "des-speckit-repo", "filename": "speckit/front-end/002-payments.md",
         "name": "002-payments"},
        # Directory-sourced: no filename at all, so the old constraint never
        # covered it.
        {"id": "des-dir", "source_dir": "/tmp/parent/front-end/specs/003-search",
         "repo_id": "repo-1", "name": "003-search"},
    ):
        sql, vals = _design(**kw)
        conn.execute(sql, vals)
    conn.execute("INSERT INTO workflows VALUES ('wf-1', 'des-file')")
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}")
    migrate_design_spec_key(engine)
    engine.dispose()
    return db_path


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return {r["id"]: r for r in conn.execute("SELECT * FROM autopilot_designs")}


def test_a_file_backed_design_keeps_its_filename_as_the_key(migrated):
    row = _rows(migrated)["des-file"]
    assert row["spec_key"] == "01-auth.md"
    assert row["filename"] == "01-auth.md"


def test_a_synthetic_filename_becomes_a_colon_key_and_the_filename_is_cleared(migrated):
    rows = _rows(migrated)
    assert rows["des-speckit"]["spec_key"] == "_workspace:001-conversation-history"
    assert rows["des-speckit"]["filename"] is None
    assert rows["des-speckit-repo"]["spec_key"] == "front-end:002-payments"
    assert rows["des-speckit-repo"]["filename"] is None


def test_a_directory_sourced_design_is_keyed_by_repo_and_directory(migrated):
    row = _rows(migrated)["des-dir"]
    assert row["spec_key"] == directory_spec_key("003-search", "front-end")
    assert row["filename"] is None


def test_uniqueness_moved_to_spec_key_and_now_covers_filenameless_designs(migrated):
    conn = sqlite3.connect(migrated, isolation_level=None)
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='autopilot_designs'"
    ).fetchone()[0]
    assert "uq_design_project_spec_key" in create_sql
    assert "uq_design_project_filename" not in create_sql

    # The hole the old constraint left: SQLite treats NULLs as distinct, so
    # two designs with no filename never collided on (project_id, filename).
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO autopilot_designs (id, project_id, spec_key, name, ordinal, "
            "size_bytes, extension, status, workflow_type) VALUES "
            "('des-dup', 'proj-1', '_workspace:001-conversation-history', 'dup', 9, 0, "
            "'.md', 'pending', 'feature')"
        )


def test_referencing_tables_are_not_left_dangling_by_the_rebuild(migrated):
    """The rebuild renames autopilot_designs, which since SQLite 3.25 rewrites
    other tables' FK clauses -- the outage this repo already had once."""
    conn = sqlite3.connect(migrated, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    wf_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='workflows'").fetchone()[0]
    assert "autopilot_designs_old" not in wf_sql
    conn.execute("INSERT INTO workflows VALUES ('wf-2', 'des-file')")


def test_running_it_twice_is_a_noop(migrated):
    before = _rows(migrated)
    engine = create_engine(f"sqlite:///{migrated}")
    migrate_design_spec_key(engine)
    engine.dispose()
    after = _rows(migrated)
    assert {k: dict(v) for k, v in before.items()} == {k: dict(v) for k, v in after.items()}
