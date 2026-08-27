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
