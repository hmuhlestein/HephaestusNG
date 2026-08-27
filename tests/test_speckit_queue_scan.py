"""Tests for scan_design_queue's Spec Kit auto-scan extension (REQ-17/18/19)."""

from pathlib import Path

from src.autopilot.orchestrator.queue import scan_design_queue
from src.core.database import AutopilotProject, DatabaseManager


def _make_project(db_manager, base_dir: Path, speckit_auto_scan: bool) -> str:
    with db_manager.session_scope() as session:
        proj = AutopilotProject(
            id="proj-1", name="p", base_dir=str(base_dir), speckit_auto_scan=speckit_auto_scan,
        )
        session.add(proj)
        session.flush()
        return proj.id


def _make_feature_dir(base_dir: Path, name: str, with_plan: bool):
    d = base_dir / "specs" / name
    d.mkdir(parents=True)
    (d / "spec.md").write_text("# spec")
    if with_plan:
        (d / "plan.md").write_text("# plan")


def _setup(tmp_path, monkeypatch, speckit_auto_scan: bool):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    project_id = _make_project(db, tmp_path, speckit_auto_scan)
    return project_id


def test_disabled_by_default_never_queues_speckit_feature(tmp_path, monkeypatch):
    project_id = _setup(tmp_path, monkeypatch, speckit_auto_scan=False)
    _make_feature_dir(tmp_path, "001-x", with_plan=True)
    empty_queue_dir = tmp_path / ".hephaestus" / "specs"

    designs = scan_design_queue(empty_queue_dir, set(), speckit_project_id=None)

    assert designs == []


def test_enabled_spec_md_only_not_queued(tmp_path, monkeypatch):
    project_id = _setup(tmp_path, monkeypatch, speckit_auto_scan=True)
    _make_feature_dir(tmp_path, "001-x", with_plan=False)
    empty_queue_dir = tmp_path / ".hephaestus" / "specs"

    designs = scan_design_queue(empty_queue_dir, set(), speckit_project_id=project_id)

    assert designs == []


def test_enabled_plan_added_later_gets_queued(tmp_path, monkeypatch):
    project_id = _setup(tmp_path, monkeypatch, speckit_auto_scan=True)
    _make_feature_dir(tmp_path, "001-x", with_plan=False)
    empty_queue_dir = tmp_path / ".hephaestus" / "specs"

    assert scan_design_queue(empty_queue_dir, set(), speckit_project_id=project_id) == []

    (tmp_path / "specs" / "001-x" / "plan.md").write_text("# plan")
    designs = scan_design_queue(empty_queue_dir, set(), speckit_project_id=project_id)

    assert len(designs) == 1
    assert designs[0].speckit_feature_dir == tmp_path / "specs" / "001-x"


def test_already_processed_feature_never_requeued(tmp_path, monkeypatch):
    project_id = _setup(tmp_path, monkeypatch, speckit_auto_scan=True)
    _make_feature_dir(tmp_path, "001-x", with_plan=True)
    empty_queue_dir = tmp_path / ".hephaestus" / "specs"

    first = scan_design_queue(empty_queue_dir, set(), speckit_project_id=project_id)
    assert len(first) == 1
    processed = {first[0].content_hash}

    second = scan_design_queue(empty_queue_dir, processed, speckit_project_id=project_id)
    assert second == []
