"""Tests for scan_design_queue's Spec Kit auto-scan extension (REQ-17/18/19)."""

import json
from pathlib import Path

from src.autopilot.orchestrator.queue import pick_next_design, scan_design_queue
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
    _setup(tmp_path, monkeypatch, speckit_auto_scan=False)
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


def test_multiple_speckit_features_survive_manual_reorder_file(tmp_path, monkeypatch):
    """Adversarial review BLOCKER: Spec Kit entries all share the basename
    "spec.md" -- a manual-reorder lookup keyed on bare filename silently
    collapsed every feature but the last into one dict slot."""
    project_id = _setup(tmp_path, monkeypatch, speckit_auto_scan=True)
    _make_feature_dir(tmp_path, "001-a", with_plan=True)
    _make_feature_dir(tmp_path, "002-b", with_plan=True)
    queue_dir = tmp_path / ".hephaestus" / "specs"
    queue_dir.mkdir(parents=True)
    order_file = tmp_path / ".hephaestus" / ".queue_order.json"
    order_file.write_text(json.dumps(["some_unrelated_design.md"]))

    designs = scan_design_queue(queue_dir, set(), speckit_project_id=project_id)

    dirs = {d.speckit_feature_dir.name for d in designs}
    assert dirs == {"001-a", "002-b"}


def test_pick_next_design_reads_speckit_auto_scan_flag_from_db(tmp_path, monkeypatch):
    """Architectural review BLOCKER: scan_design_queue's speckit_project_id
    param was never actually passed by pick_next_design (the only
    production caller), so toggling AutopilotProject.speckit_auto_scan had
    zero effect. This drives pick_next_design itself, not scan_design_queue
    directly, so it fails if that wiring regresses."""
    project_id = _setup(tmp_path, monkeypatch, speckit_auto_scan=True)
    _make_feature_dir(tmp_path, "001-x", with_plan=True)
    queue_dir = tmp_path / ".hephaestus" / "specs"
    queue_dir.mkdir(parents=True)

    from src.autopilot.orchestrator import OrchestratorLogger

    logger = OrchestratorLogger(tmp_path)
    result = pick_next_design(queue_dir, set(), logger, project_id=project_id)

    assert result is not None
    assert result.speckit_feature_dir == tmp_path / "specs" / "001-x"


def test_pick_next_design_never_auto_scans_when_flag_disabled(tmp_path, monkeypatch):
    project_id = _setup(tmp_path, monkeypatch, speckit_auto_scan=False)
    _make_feature_dir(tmp_path, "001-x", with_plan=True)
    queue_dir = tmp_path / ".hephaestus" / "specs"
    queue_dir.mkdir(parents=True)

    from src.autopilot.orchestrator import OrchestratorLogger

    logger = OrchestratorLogger(tmp_path)
    result = pick_next_design(queue_dir, set(), logger, project_id=project_id)

    assert result is None
