"""Tests for get_project_design_status's feature/design status derivation.

Covers two bugs found in the same live incident:
1. A stray monitor-generated DIAGNOSTIC task made "all real tasks done" look
   like "mixed statuses" and fall through to a stale DB value forever — this
   is what made a completed feature's pause/resume button silently no-op.
2. The design-level overall_status let the coarse, rarely-updated
   design_status field override a live 'paused' workflow signal, so pausing
   a workflow never flipped the design row's displayed status either.
"""

import asyncio
import uuid

import pytest

from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    DatabaseManager,
    Feature,
    Task,
    Workflow,
)


@pytest.fixture
def status_env(tmp_path, monkeypatch):
    """Real sqlite file (get_project_design_status uses get_db(), which reads
    HEPHAESTUS_TEST_DB), plus a real design file on disk (the endpoint reads
    it directly), plus a seeded AutopilotProject/AutopilotDesign pair."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))

    manager = DatabaseManager(str(db_path))
    manager.create_tables()

    design_dir = tmp_path / ".hephaestus" / "specs"
    design_dir.mkdir(parents=True)
    filename = "sample_design.md"
    (design_dir / filename).write_text("# Sample Design\n")

    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    design_id = f"des-{uuid.uuid4().hex[:8]}"

    session = manager.get_session()
    try:
        session.add(
            AutopilotProject(id=project_id, name="Test Project", base_dir=str(tmp_path))
        )
        session.add(
            AutopilotDesign(
                id=design_id,
                project_id=project_id,
                filename=filename,
                name="Sample Design",
                status="active",
            )
        )
        session.commit()
    finally:
        session.close()

    return {
        "manager": manager,
        "project_id": project_id,
        "design_id": design_id,
        "filename": filename,
    }


def _make_workflow(
    session, design_id, filename, status="active", definition_id="autopilot", **overrides
):
    wf_id = f"wf-{uuid.uuid4().hex[:8]}"
    wf = Workflow(
        id=wf_id,
        name="Test Workflow",
        phases_folder_path="/tmp",
        status=status,
        definition_id=definition_id,
        design_id=design_id,
        launch_params={"design_document": f"docs/spec/{filename}"},
        **overrides,
    )
    session.add(wf)
    session.commit()
    return wf_id


def _make_feature(session, design_id, workflow_id, status="active"):
    feat_id = f"feat-{uuid.uuid4().hex[:8]}"
    session.add(
        Feature(
            id=feat_id,
            design_id=design_id,
            feature_key="sample-feature",
            name="Sample Feature",
            scope="test scope",
            status=status,
            workflow_id=workflow_id,
        )
    )
    session.commit()
    return feat_id


def _make_task(session, workflow_id, status, raw_description="do work"):
    task_id = str(uuid.uuid4())
    session.add(
        Task(
            id=task_id,
            raw_description=raw_description,
            done_definition="done",
            status=status,
            workflow_id=workflow_id,
        )
    )
    session.commit()
    return task_id


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestFeatureStatusDerivation:
    def test_all_done_plus_stray_diagnostic_task_shows_completed(self, status_env):
        """The exact bug: 9 real tasks done + 1 stray DIAGNOSTIC task pending
        used to leave the feature stuck showing its stale 'active' DB value."""
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            wf_id = _make_workflow(session, status_env["design_id"], status_env["filename"])
            feat_id = _make_feature(session, status_env["design_id"], wf_id, status="active")
            for _ in range(3):
                _make_task(session, wf_id, "done")
            _make_task(
                session,
                wf_id,
                "pending",
                raw_description="DIAGNOSTIC: Analyze why workflow has stalled",
            )
        finally:
            session.close()

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )
        feature = next(f for f in result["features"] if f["id"] == feat_id)
        assert feature["status"] == "completed"

    def test_self_heals_db_column(self, status_env):
        """Once derived as completed, the Feature row itself should be
        updated too — other code paths read Feature.status directly."""
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            wf_id = _make_workflow(session, status_env["design_id"], status_env["filename"])
            feat_id = _make_feature(session, status_env["design_id"], wf_id, status="active")
            _make_task(session, wf_id, "done")
        finally:
            session.close()

        _run(get_project_design_status(status_env["project_id"], status_env["filename"]))

        session = manager.get_session()
        try:
            feat = session.query(Feature).filter_by(id=feat_id).first()
            assert feat.status == "completed"
        finally:
            session.close()

    def test_paused_db_status_wins_even_with_pending_tasks(self, status_env):
        """A deliberately paused feature must keep showing 'paused' even
        though its blocked tasks look like unfinished 'pending' work."""
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            wf_id = _make_workflow(
                session, status_env["design_id"], status_env["filename"], status="paused"
            )
            feat_id = _make_feature(session, status_env["design_id"], wf_id, status="paused")
            _make_task(session, wf_id, "blocked")
        finally:
            session.close()

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )
        feature = next(f for f in result["features"] if f["id"] == feat_id)
        assert feature["status"] == "paused"

    def test_no_real_tasks_yet_trusts_db_status(self, status_env):
        """Only a stray diagnostic task exists (no real work started) — should
        trust the DB status rather than reporting something misleading."""
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            wf_id = _make_workflow(session, status_env["design_id"], status_env["filename"])
            feat_id = _make_feature(session, status_env["design_id"], wf_id, status="active")
            _make_task(
                session,
                wf_id,
                "pending",
                raw_description="DIAGNOSTIC: Analyze why workflow has stalled",
            )
        finally:
            session.close()

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )
        feature = next(f for f in result["features"] if f["id"] == feat_id)
        assert feature["status"] == "active"


class TestDesignOverallStatusDerivation:
    def test_paused_workflow_overrides_stale_active_design_status(self, status_env):
        """design_status ('active', set once at pipeline start and only ever
        updated by run_design_aggregate at the very end) must not hide a
        workflow that's actually paused right now."""
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            design = (
                session.query(AutopilotDesign)
                .filter_by(id=status_env["design_id"])
                .first()
            )
            design.status = "active"
            session.commit()
            _make_workflow(
                session, status_env["design_id"], status_env["filename"], status="paused"
            )
        finally:
            session.close()

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )
        assert result["status"] == "paused"

    def test_active_workflow_beats_paused_design_status(self, status_env):
        manager = status_env["manager"]
        session = manager.get_session()
        try:
            design = (
                session.query(AutopilotDesign)
                .filter_by(id=status_env["design_id"])
                .first()
            )
            design.status = "active"
            session.commit()
            _make_workflow(
                session, status_env["design_id"], status_env["filename"], status="active"
            )
        finally:
            session.close()

        from src.mcp.autopilot.design_file_routes import get_project_design_status

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )
        assert result["status"] == "active"


class TestPhase0FeatureArchitectVisibility:
    """Phase 0 (Feature Architect) decomposes a design into the Feature rows
    the rest of this endpoint already surfaces, but is itself a separate
    Workflow (1:1 Feature:Workflow means it can't be phase order=0 within
    one). Nothing showed its live task/agent while it ran -- the Design
    Queue UI only ever showed a static "pending" placeholder or the real
    decomposed features, with no way to watch Phase 0 itself."""

    def test_running_phase0_appears_as_pseudo_feature(self, status_env):
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            phase0_wf_id = _make_workflow(
                session,
                status_env["design_id"],
                status_env["filename"],
                status="active",
                definition_id="feature_architect",
            )
            _make_task(session, phase0_wf_id, "in_progress")
        finally:
            session.close()

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )

        phase0_entries = [f for f in result["features"] if f["name"] == "Feature Architect"]
        assert len(phase0_entries) == 1
        assert phase0_entries[0]["status"] == "active"
        assert phase0_entries[0]["workflow_id"] == phase0_wf_id
        assert len(phase0_entries[0]["tasks"]) == 1

    def test_phase0_entry_appears_before_real_features(self, status_env):
        """Feature Architect ran first chronologically -- it should list
        first, not get buried after the features it produced."""
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            phase0_wf_id = _make_workflow(
                session,
                status_env["design_id"],
                status_env["filename"],
                status="completed",
                definition_id="feature_architect",
            )
            _make_task(session, phase0_wf_id, "done")

            feature_wf_id = _make_workflow(
                session, status_env["design_id"], status_env["filename"]
            )
            _make_feature(session, status_env["design_id"], feature_wf_id, status="active")
        finally:
            session.close()

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )

        assert result["features"][0]["name"] == "Feature Architect"

    def test_completed_phase0_shows_completed_status(self, status_env):
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            phase0_wf_id = _make_workflow(
                session,
                status_env["design_id"],
                status_env["filename"],
                status="completed",
                definition_id="feature_architect",
            )
            _make_task(session, phase0_wf_id, "done")
        finally:
            session.close()

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )

        phase0_entry = next(f for f in result["features"] if f["name"] == "Feature Architect")
        assert phase0_entry["status"] == "completed"

    def test_no_phase0_workflow_no_pseudo_feature_added(self, status_env):
        """Regression: must not fabricate a Feature Architect entry for
        designs that never had a Phase 0 workflow at all."""
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            wf_id = _make_workflow(session, status_env["design_id"], status_env["filename"])
            _make_feature(session, status_env["design_id"], wf_id, status="active")
        finally:
            session.close()

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )

        assert not any(f["name"] == "Feature Architect" for f in result["features"])

    def test_no_phase0_task_yet_no_pseudo_feature_added(self, status_env):
        """A Phase 0 Workflow row exists but no task has been created for it
        yet -- don't show an empty, misleading entry."""
        from src.mcp.autopilot.design_file_routes import get_project_design_status

        manager = status_env["manager"]
        session = manager.get_session()
        try:
            _make_workflow(
                session,
                status_env["design_id"],
                status_env["filename"],
                status="active",
                definition_id="feature_architect",
            )
        finally:
            session.close()

        result = _run(
            get_project_design_status(status_env["project_id"], status_env["filename"])
        )

        assert not any(f["name"] == "Feature Architect" for f in result["features"])
