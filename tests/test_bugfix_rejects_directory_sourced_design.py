"""A directory-sourced DesignEntry must never reach the bugfix workflow's
content-read path (REQ-09/NFR-04 scope guard). Without the assertion, the
existing `except OSError` around Path(design_entry.path).read_text()
silently swallows IsADirectoryError and writes an empty scope.md instead
of failing loudly -- see architecture.md's Gotchas item 1.
"""

import pytest


@pytest.fixture
def bugfix_db(tmp_path, monkeypatch):
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


class _NullLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_directory_sourced_design_raises_before_reading_content(bugfix_db, tmp_path):
    from src.autopilot.orchestrator.pipeline import run_bugfix_single_feature
    from src.autopilot.orchestrator.state import DesignEntry

    feature_dir = tmp_path / "specs" / "003-checkout-flow"
    feature_dir.mkdir(parents=True)
    (feature_dir / "spec.md").write_text("spec content")

    entry = DesignEntry(
        path=feature_dir,
        name="003-checkout-flow",
        content_hash="abc123",
        db_id=None,
        source_dir=feature_dir,
    )

    with pytest.raises(AssertionError):
        run_bugfix_single_feature(entry, tmp_path, _NullLogger())

    # The scope.md that the (unreached) content-read block would otherwise
    # have written must not exist -- confirms the assert fires BEFORE that
    # write, not merely somewhere in the same function.
    scope_candidates = list((tmp_path / ".hephaestus" / "specs").rglob("scope.md"))
    assert scope_candidates == []


def test_pick_next_design_returns_pinned_directory_sourced_row(bugfix_db, tmp_path):
    """Regression: pick_next_design's DB-first path used to construct
    DesignEntry(source_dir=..., repo_id=...) for a directory-sourced row --
    a TypeError against the old DesignEntry (no such fields), silently
    caught by the surrounding try/except and mis-logged as "DB queue read
    failed, falling back to file scan". That meant an explicit --feature
    selection never actually got returned by the primary path. Assert the
    real DesignEntry (not a file-scan fallback) comes back with source_dir
    set and matching the pinned row."""
    from src.autopilot.orchestrator import OrchestratorLogger
    from src.autopilot.orchestrator.queue import pick_next_design
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    feature_dir = tmp_path / "specs" / "001-x"
    feature_dir.mkdir(parents=True)
    (feature_dir / "spec.md").write_text("spec content")

    with get_db() as db:
        db.add(AutopilotProject(id="proj-pin", name="p", base_dir=str(tmp_path), is_active=True))
        db.add(
            AutopilotDesign(
                id="des-pin1",
                project_id="proj-pin",
                filename=None,
                name="001-x",
                ordinal=-1,
                extension=".md",
                source_dir=str(feature_dir),
                status="pending",
            )
        )
        db.commit()

    result = pick_next_design(tmp_path, set(), OrchestratorLogger(tmp_path), project_id="proj-pin")

    assert result is not None
    assert result.db_id == "des-pin1"
    assert result.source_dir == feature_dir
