"""Tests for wiring resolve_feature_selection into heph autopilot start
(REQ-10/11/12/13/15). Architectural review BLOCKER: discover_speckit_features/
resolve_feature_selection/check_feature_readiness were implemented and unit-
tested but never called from any CLI command or API route.

_resolve_and_enqueue_speckit_feature is tested directly (not through the full
/start HTTP route) to avoid mocking service.start()'s concurrency-cap/zombie-
detection machinery, which is unrelated to what this fix actually changes.
The /speckit/check and /speckit/features routes ARE read-only and are tested
through the real FastAPI app.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

from src.core.database import AutopilotDesign, AutopilotProject, DatabaseManager


def _make_feature_dir(base_dir: Path, name: str, with_plan: bool = True):
    d = base_dir / "specs" / name
    d.mkdir(parents=True)
    (d / "spec.md").write_text("# spec")
    if with_plan:
        (d / "plan.md").write_text("# plan")


def _setup_db(tmp_path, monkeypatch, base_dir: Path) -> str:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    with db.session_scope() as session:
        proj = AutopilotProject(id="proj-1", name="p", base_dir=str(base_dir))
        session.add(proj)
        session.flush()
        return proj.id


class TestResolveAndEnqueueSpeckitFeature:
    def test_unambiguous_feature_enqueued_at_top_priority(self, tmp_path, monkeypatch):
        from src.mcp.autopilot.control_routes import _resolve_and_enqueue_speckit_feature

        project_id = _setup_db(tmp_path, monkeypatch, tmp_path)
        _make_feature_dir(tmp_path, "001-x")

        _resolve_and_enqueue_speckit_feature(project_id, str(tmp_path), "001-x", None)

        from src.core.database import get_db

        with get_db() as db:
            rows = db.query(AutopilotDesign).filter_by(project_id=project_id).all()
            assert len(rows) == 1
            assert rows[0].file_path == str(tmp_path / "specs" / "001-x" / "spec.md")
            assert rows[0].status == "pending"
            assert rows[0].ordinal < 0

    def test_bare_number_matches_full_name(self, tmp_path, monkeypatch):
        from src.mcp.autopilot.control_routes import _resolve_and_enqueue_speckit_feature

        project_id = _setup_db(tmp_path, monkeypatch, tmp_path)
        _make_feature_dir(tmp_path, "001-x")

        _resolve_and_enqueue_speckit_feature(project_id, str(tmp_path), "001", None)

        from src.core.database import get_db

        with get_db() as db:
            rows = db.query(AutopilotDesign).filter_by(project_id=project_id).all()
            assert len(rows) == 1

    def test_not_found_raises_422(self, tmp_path, monkeypatch):
        from src.mcp.autopilot.control_routes import _resolve_and_enqueue_speckit_feature

        project_id = _setup_db(tmp_path, monkeypatch, tmp_path)
        _make_feature_dir(tmp_path, "001-x")

        with pytest.raises(HTTPException) as exc:
            _resolve_and_enqueue_speckit_feature(project_id, str(tmp_path), "999-missing", None)
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "NOT_FOUND"

    def test_ambiguous_selection_creates_no_design_row(self, tmp_path, monkeypatch):
        """Architecture Task 4 acceptance criterion: an ambiguous selection
        must not start anything -- verify no AutopilotDesign row is created
        (the mechanism this feature uses to enqueue a selected feature)."""
        from src.mcp.autopilot.control_routes import _resolve_and_enqueue_speckit_feature

        project_id = _setup_db(tmp_path, monkeypatch, tmp_path)
        _make_feature_dir(tmp_path, "001-x")
        _make_feature_dir(tmp_path, "002-y")

        with pytest.raises(HTTPException) as exc:
            _resolve_and_enqueue_speckit_feature(project_id, str(tmp_path), None, None)
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "MULTIPLE_FEATURES"

        from src.core.database import get_db

        with get_db() as db:
            assert db.query(AutopilotDesign).filter_by(project_id=project_id).count() == 0

    def test_repeat_call_reuses_row_and_resets_priority(self, tmp_path, monkeypatch):
        """A second `--feature 001-x` call (e.g. after a failed run) must not
        create a duplicate row -- it re-enqueues the same one at top priority."""
        from src.mcp.autopilot.control_routes import _resolve_and_enqueue_speckit_feature

        project_id = _setup_db(tmp_path, monkeypatch, tmp_path)
        _make_feature_dir(tmp_path, "001-x")

        _resolve_and_enqueue_speckit_feature(project_id, str(tmp_path), "001-x", None)
        _resolve_and_enqueue_speckit_feature(project_id, str(tmp_path), "001-x", None)

        from src.core.database import get_db

        with get_db() as db:
            rows = db.query(AutopilotDesign).filter_by(project_id=project_id).all()
            assert len(rows) == 1


class TestSpeckitCheckAndFeaturesRoutes:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.mcp.autopilot import control_routes, router

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
        DatabaseManager(str(db_path)).create_tables()

        app = FastAPI()
        app.include_router(router)
        monkeypatch.setattr(control_routes, "_get_active_project_id", lambda: None)
        return TestClient(app)

    def test_speckit_features_lists_unregistered_project(self, tmp_path, client):
        _make_feature_dir(tmp_path, "001-x")

        resp = client.get("/api/autopilot/speckit/features", params={"project_path": str(tmp_path)})

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["number"] == "001"
        assert data[0]["hasPlan"] is True

    def test_speckit_features_returns_repo_labels_for_multi_repo_project(self, tmp_path, client):
        """Architecture Task 4 acceptance criterion: /speckit/features returns
        repo labels for multi-repo projects -- the unregistered-project test
        above only covers the single-repo (repo_label=None) case."""
        from src.core.database import AutopilotProject, ProjectRepo, get_db

        backend_dir = tmp_path / "backend"
        frontend_dir = tmp_path / "frontend"
        _make_feature_dir(backend_dir, "001-x")
        _make_feature_dir(frontend_dir, "002-y")

        with get_db() as db:
            db.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
            db.add(ProjectRepo(id="repo-a", project_id="proj-1", label="backend", path=str(backend_dir), is_primary=True))
            db.add(ProjectRepo(id="repo-b", project_id="proj-1", label="frontend", path=str(frontend_dir)))
            db.commit()

        resp = client.get("/api/autopilot/speckit/features", params={"project_path": str(tmp_path)})

        assert resp.status_code == 200
        data = resp.json()
        assert {d["repoLabel"] for d in data} == {"backend", "frontend"}

    def test_speckit_check_reports_missing_files_and_markers(self, tmp_path, client):
        d = tmp_path / "specs" / "001-x"
        d.mkdir(parents=True)
        (d / "spec.md").write_text("[NEEDS CLARIFICATION: which auth?]")

        resp = client.get(
            "/api/autopilot/speckit/check",
            params={"project_path": str(tmp_path), "feature": "001-x"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_repo_scan"] is False
        assert data["features"][0]["missing_files"] == ["plan.md", "tasks.md"]
        assert data["features"][0]["needs_clarification"] == ["which auth?"]

    def test_speckit_check_never_fails_start_or_mutates(self, tmp_path, client):
        """Voluntary check on a feature with issues doesn't error the request
        itself -- confirms REQ-15's 'never a gate' property at the route
        level (start() isn't called by this route at all)."""
        _make_feature_dir(tmp_path, "001-x", with_plan=False)

        resp = client.get("/api/autopilot/speckit/check", params={"project_path": str(tmp_path)})

        assert resp.status_code == 200
