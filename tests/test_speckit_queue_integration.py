"""Spec-Kit-aware design-queue integration tests (REQ-03/04/05/06/07/11/12/13).

Covers scan_design_queue's directory detection + auto-scan gate, and
pick_next_design's source_dir reconstruction and auto-create-row write
site -- the pieces that must agree on the source_dir/file_path mutual
exclusivity invariant (NFR-02) for a Spec-Kit design to ever round-trip.
"""

import logging

import pytest


@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _make_project(db, project_id, base_dir, speckit_autoscan_enabled=False):
    from src.core.database import AutopilotProject

    with db.session_scope() as session:
        session.add(
            AutopilotProject(
                id=project_id,
                name=project_id,
                base_dir=str(base_dir),
                is_active=True,
                speckit_autoscan_enabled=speckit_autoscan_enabled,
            )
        )


def _make_repo(db, repo_id, project_id, label, path, is_primary=True):
    from src.core.database import ProjectRepo

    with db.session_scope() as session:
        session.add(
            ProjectRepo(
                id=repo_id,
                project_id=project_id,
                label=label,
                path=str(path),
                is_primary=is_primary,
            )
        )


def _make_feature_dir(repo_path, dir_name, has_plan=False):
    feature_dir = repo_path / "specs" / dir_name
    feature_dir.mkdir(parents=True)
    (feature_dir / "spec.md").write_text(f"spec for {dir_name}")
    if has_plan:
        (feature_dir / "plan.md").write_text(f"plan for {dir_name}")
    return feature_dir


class _NullLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_detection_unconditional_but_not_queued_when_disabled(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import scan_design_queue
    from src.core.speckit_detection import find_speckit_features
    from src.core.database import get_db

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _make_feature_dir(repo_path, "003-checkout-flow", has_plan=True)
    _make_project(queue_db, "proj-a", repo_path, speckit_autoscan_enabled=False)
    _make_repo(queue_db, "repo-a", "proj-a", "primary", repo_path)

    with get_db() as db:
        detected = find_speckit_features(db, "proj-a")
    assert len(detected) == 1  # detection is unconditional (REQ-03/NFR-05)

    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()
    for _ in range(3):  # "regardless of elapsed time" -- repeated scans
        designs = scan_design_queue(queue_dir, set(), project_id="proj-a")
        assert designs == []  # detected, never queued while disabled (REQ-12)


def test_enabled_withholds_until_plan_present(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import scan_design_queue

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = _make_feature_dir(repo_path, "004-signup", has_plan=False)
    _make_project(queue_db, "proj-b", repo_path, speckit_autoscan_enabled=True)
    _make_repo(queue_db, "repo-b", "proj-b", "primary", repo_path)

    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()
    designs = scan_design_queue(queue_dir, set(), project_id="proj-b")
    assert designs == []  # enabled, but no plan.md yet (REQ-13)

    (feature_dir / "plan.md").write_text("now with a plan")
    designs = scan_design_queue(queue_dir, set(), project_id="proj-b")
    assert len(designs) == 1
    assert designs[0].source_dir == feature_dir
    assert designs[0].repo_id == "repo-b"


def test_multi_repo_scan_tags_correct_repo(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import scan_design_queue

    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    _make_feature_dir(backend, "001-x", has_plan=True)
    _make_feature_dir(frontend, "002-y", has_plan=True)
    _make_project(queue_db, "proj-c", backend, speckit_autoscan_enabled=True)
    _make_repo(queue_db, "repo-backend", "proj-c", "backend", backend, is_primary=True)
    _make_repo(queue_db, "repo-frontend", "proj-c", "frontend", frontend, is_primary=False)

    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()
    designs = scan_design_queue(queue_dir, set(), project_id="proj-c")
    assert len(designs) == 2
    by_repo = {d.repo_id for d in designs}
    assert by_repo == {"repo-backend", "repo-frontend"}


def test_regression_file_only_scan_unaffected(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import scan_design_queue

    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "design.md").write_text("hello")

    without_project = scan_design_queue(queue_dir, set())
    with_project_none = scan_design_queue(queue_dir, set(), project_id=None)
    assert [d.path for d in without_project] == [d.path for d in with_project_none]
    assert len(without_project) == 1
    assert without_project[0].source_dir is None


def test_pick_next_design_reconstructs_from_source_dir(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import pick_next_design
    from src.core.database import AutopilotDesign

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = _make_feature_dir(repo_path, "005-z", has_plan=True)
    _make_project(queue_db, "proj-d", repo_path)
    _make_repo(queue_db, "repo-d", "proj-d", "primary", repo_path)

    with queue_db.session_scope() as session:
        session.add(
            AutopilotDesign(
                id="des-1",
                project_id="proj-d",
                filename=None,
                file_path=None,
                source_dir=str(feature_dir),
                repo_id="repo-d",
                name="005-z",
                content_hash="abc123",
                status="pending",
            )
        )

    entry = pick_next_design(tmp_path / "unused_queue", set(), _NullLogger(), project_id="proj-d")
    assert entry is not None
    assert entry.source_dir == feature_dir
    assert entry.path == feature_dir
    assert entry.repo_id == "repo-d"

    with queue_db.session_scope() as session:
        design = session.query(AutopilotDesign).filter_by(id="des-1").first()
        assert design.status == "processing"


def test_pick_next_design_missing_source_dir_returns_none(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import pick_next_design
    from src.core.database import AutopilotDesign

    _make_project(queue_db, "proj-e", tmp_path / "repo")
    with queue_db.session_scope() as session:
        session.add(
            AutopilotDesign(
                id="des-2",
                project_id="proj-e",
                filename=None,
                file_path=None,
                source_dir=str(tmp_path / "does-not-exist"),
                name="gone",
                content_hash="def456",
                status="pending",
            )
        )

    entry = pick_next_design(tmp_path / "unused_queue", set(), _NullLogger(), project_id="proj-e")
    assert entry is None


def test_auto_create_row_for_directory_sourced_design_and_round_trip(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import pick_next_design
    from src.core.database import AutopilotDesign

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = _make_feature_dir(repo_path, "006-w", has_plan=True)
    _make_project(queue_db, "proj-f", repo_path, speckit_autoscan_enabled=True)
    _make_repo(queue_db, "repo-f", "proj-f", "primary", repo_path)

    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()

    first = pick_next_design(queue_dir, set(), _NullLogger(), project_id="proj-f")
    assert first is not None
    assert first.db_id is not None

    with queue_db.session_scope() as session:
        rows = session.query(AutopilotDesign).filter_by(project_id="proj-f").all()
        assert len(rows) == 1
        row = rows[0]
        assert row.source_dir == str(feature_dir)
        assert row.repo_id == "repo-f"
        assert row.filename is None
        assert row.file_path is None

    # Round-trip: a second call picks the now-pending DB row via the
    # source_dir branch, not a second auto-create.
    second = pick_next_design(queue_dir, set(), _NullLogger(), project_id="proj-f")
    assert second is not None
    assert second.source_dir == feature_dir
    with queue_db.session_scope() as session:
        assert session.query(AutopilotDesign).filter_by(project_id="proj-f").count() == 1
