"""Tests for autopilot API endpoints."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def autopilot_dirs(tmp_path):
    """Create temp queue, features, and state directories."""
    queue_dir = tmp_path / "queue"
    features_dir = tmp_path / "features"
    state_dir = tmp_path / "state"
    queue_dir.mkdir()
    features_dir.mkdir()
    state_dir.mkdir()

    import src.mcp.autopilot_api as api_mod

    api_mod._cache.clear()

    old_state = api_mod.AUTOPILOT_STATE_DIR
    old_queue = api_mod.DESIGN_QUEUE_DIR
    old_features = api_mod.FEATURES_DIR

    api_mod.AUTOPILOT_STATE_DIR = str(state_dir)
    api_mod.configure_autopilot_api(
        design_queue_dir=str(queue_dir),
        features_dir=str(features_dir),
    )

    yield {
        "queue": queue_dir,
        "features": features_dir,
        "state": state_dir,
    }

    api_mod.AUTOPILOT_STATE_DIR = old_state
    api_mod.DESIGN_QUEUE_DIR = old_queue
    api_mod.FEATURES_DIR = old_features
    api_mod._cache.clear()


@pytest.fixture
def client(autopilot_dirs, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp import autopilot_api
    from src.mcp.autopilot_api import router

    app = FastAPI()
    app.include_router(router)

    # Mock _get_active_project_id to return None (no DB needed)
    monkeypatch.setattr(autopilot_api, "_get_active_project_id", lambda: None)

    return TestClient(app)


# ── Path Traversal ───────────────────────────────────────────────


class TestPathTraversal:
    def test_queue_delete_rejects_traversal(self, client):
        resp = client.delete("/api/autopilot/queue/../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_queue_content_rejects_traversal(self, client):
        resp = client.get("/api/autopilot/queue/../../etc/passwd/content")
        assert resp.status_code in (400, 404)

    def test_feature_detail_rejects_traversal(self, client):
        resp = client.get("/api/autopilot/features/../../etc")
        assert resp.status_code in (400, 404)

    def test_feature_report_rejects_traversal(self, client):
        resp = client.get("/api/autopilot/features/../../etc/report")
        assert resp.status_code in (400, 404)

    def test_feature_artifact_rejects_traversal(self, client):
        resp = client.get("/api/autopilot/features/foo/../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_queue_add_rejects_path_in_name(self, client):
        resp = client.post(
            "/api/autopilot/queue",
            json={
                "name": "../../etc/cron",
                "content": "test",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            assert ".." not in data["filename"]


# ── Design Queue ─────────────────────────────────────────────────


class TestDesignQueue:
    def test_empty_queue(self, client):
        resp = client.get("/api/autopilot/queue")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_add_and_list(self, client):
        resp = client.post(
            "/api/autopilot/queue",
            json={
                "name": "Test Feature",
                "content": "# Design\nHello world",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Feature"
        assert data["extension"] == ".md"

        resp = client.get("/api/autopilot/queue")
        assert len(resp.json()) == 1
        assert resp.json()[0]["filename"] == data["filename"]

    def test_add_duplicate_rejects(self, client):
        client.post(
            "/api/autopilot/queue",
            json={
                "name": "Dup Test",
                "content": "content",
            },
        )
        resp = client.post(
            "/api/autopilot/queue",
            json={
                "name": "Dup Test",
                "content": "content",
            },
        )
        assert resp.status_code == 409

    def test_delete(self, client, autopilot_dirs):
        f = autopilot_dirs["queue"] / "to_delete.md"
        f.write_text("content")

        resp = client.delete("/api/autopilot/queue/to_delete.md")
        assert resp.status_code == 200
        assert not f.exists()

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/autopilot/queue/nope.md")
        assert resp.status_code == 404

    def test_content(self, client, autopilot_dirs):
        f = autopilot_dirs["queue"] / "readme.md"
        f.write_text("# Hello")

        resp = client.get("/api/autopilot/queue/readme.md/content")
        assert resp.status_code == 200
        assert resp.json()["content"] == "# Hello"

    def test_reorder(self, client, autopilot_dirs):
        for name in ["a.md", "b.md", "c.md"]:
            (autopilot_dirs["queue"] / name).write_text(name)

        resp = client.post(
            "/api/autopilot/queue/reorder", json={"filenames": ["c.md", "a.md", "b.md"]}
        )
        assert resp.status_code == 200

        resp = client.get("/api/autopilot/queue")
        filenames = [i["filename"] for i in resp.json()]
        assert filenames == ["c.md", "a.md", "b.md"]

    def test_reorder_rejects_unknown(self, client, autopilot_dirs):
        (autopilot_dirs["queue"] / "a.md").write_text("a")

        resp = client.post(
            "/api/autopilot/queue/reorder", json={"filenames": ["a.md", "ghost.md"]}
        )
        assert resp.status_code == 400


class TestQueueRerun:
    """Rerun must drive the in-process AutopilotService singleton (the same
    one the play/pause button uses), not spawn/kill a separate
    `python -m src.autopilot.orchestrator` subprocess. The subprocess path
    could run concurrently with the in-process service -- both independently
    calling run_phase0 -- which was the root cause of design docs getting
    copied twice.
    """

    def test_rerun_uses_in_process_service_not_subprocess(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        import subprocess as subprocess_mod

        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "designs").mkdir(parents=True)
        design_file = project_dir / ".hephaestus" / "designs" / "my_design.md"
        design_file.write_text("# Design")

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(return_value={"started": True})
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator._resolve_project_id", lambda project_path: "proj-fixed"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator._get_or_create_project_id",
            lambda project_path: "proj-fixed",
        )

        popen_mock = Mock(side_effect=AssertionError("subprocess.Popen must not be called by rerun"))
        monkeypatch.setattr(subprocess_mod, "Popen", popen_mock)

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 200, resp.text
        popen_mock.assert_not_called()
        fake_service.start.assert_called_once()
        call_kwargs = fake_service.start.call_args.kwargs
        assert call_kwargs["project_path"] == str(project_dir)
        assert not (Path(autopilot_dirs["state"]) / "orchestrator.pid").exists()

    def test_rerun_stops_running_service_before_restarting(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "designs").mkdir(parents=True)
        (project_dir / ".hephaestus" / "designs" / "my_design.md").write_text("# Design")

        fake_service = Mock()
        fake_service.running = True  # already running -- rerun must stop it first
        fake_service.start = AsyncMock(return_value={"started": True})
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator._resolve_project_id", lambda project_path: "proj-fixed"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator._get_or_create_project_id",
            lambda project_path: "proj-fixed",
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 200, resp.text
        fake_service.stop.assert_called_once()
        fake_service.start.assert_called_once()

    def test_rerun_maps_runtime_error_to_409_not_400(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        """service.start() raising RuntimeError means "already running" (its
        own docstring: "Raises: RuntimeError: If pipeline is already
        running") -- matches /start's own convention of mapping that to 409,
        not the generic 400 a first pass at this used. Real scenario: another
        request races in and starts something else between Step 1's stop()
        and Step 6's start()."""
        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "designs").mkdir(parents=True)
        (project_dir / ".hephaestus" / "designs" / "my_design.md").write_text("# Design")

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(
            side_effect=RuntimeError("Pipeline is already running")
        )
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator._resolve_project_id", lambda project_path: "proj-fixed"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator._get_or_create_project_id",
            lambda project_path: "proj-fixed",
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 409, resp.text

    def test_rerun_maps_value_error_to_400(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        """service.start() raising ValueError means bad input (e.g. project
        path isn't a git repo) -- matches /start's own convention of 400."""
        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "designs").mkdir(parents=True)
        (project_dir / ".hephaestus" / "designs" / "my_design.md").write_text("# Design")

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(
            side_effect=ValueError("Project path is not a git repository")
        )
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator._resolve_project_id", lambda project_path: "proj-fixed"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator._get_or_create_project_id",
            lambda project_path: "proj-fixed",
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 400, resp.text

    def test_rerun_rejects_when_over_concurrency_cap(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        """Regression: rerun_design used to call service.start() with no
        concurrency-cap check at all, unlike POST /start -- a "rerun" on a
        brand-new, not-yet-running project could silently exceed
        max_concurrent_projects even while POST /start would reject the
        identical project with a 409."""
        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "designs").mkdir(parents=True)
        (project_dir / ".hephaestus" / "designs" / "my_design.md").write_text("# Design")

        monkeypatch.setattr(
            "src.autopilot.orchestrator._resolve_project_id", lambda project_path: None
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator._get_or_create_project_id",
            lambda project_path: "proj-over-cap",
        )
        fake_registry = Mock()
        fake_registry.try_reserve.return_value = (
            False,
            "Max concurrent projects (2) reached: proj-a, proj-b. "
            "Stop one before starting another.",
        )
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 409, resp.text
        assert "proj-a" in resp.text


# ── Caching ──────────────────────────────────────────────────────


class TestCaching:
    def test_queue_caching(self, client, autopilot_dirs):
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        (autopilot_dirs["queue"] / "cached.md").write_text("x")

        resp1 = client.get("/api/autopilot/queue")
        assert len(resp1.json()) == 1

        (autopilot_dirs["queue"] / "new.md").write_text("y")
        resp2 = client.get("/api/autopilot/queue")
        assert len(resp2.json()) == 1  # cached

    def test_add_invalidates_cache(self, client, autopilot_dirs):
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        client.get("/api/autopilot/queue")

        client.post(
            "/api/autopilot/queue",
            json={
                "name": "Cache Test",
                "content": "x",
            },
        )

        resp = client.get("/api/autopilot/queue")
        assert len(resp.json()) == 1


# ── Multi-project queue-dir scoping ────────────────────────────────


@pytest.fixture
def two_project_client(tmp_path, monkeypatch):
    """Test client wired to a real DB with two distinct AutopilotProject
    rows, each with its own design-queue directory -- for verifying
    _get_effective_queue_dir(project_id) resolves the RIGHT project's
    directory instead of silently falling back to whichever one is
    is_active."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)

    from src.core.database import AutopilotProject, DatabaseManager

    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    proj_a_dir = tmp_path / "proj-a"
    proj_b_dir = tmp_path / "proj-b"
    proj_a_dir.mkdir()
    proj_b_dir.mkdir()

    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-a", name="proj-a", base_dir=str(proj_a_dir), is_active=True))
        session.add(AutopilotProject(id="proj-b", name="proj-b", base_dir=str(proj_b_dir), is_active=True))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    import src.mcp.autopilot_api as api_mod

    api_mod._cache.clear()
    api_mod._queue_dir_by_project.clear()
    api_mod._features_dir_by_project.clear()

    yield client, proj_a_dir, proj_b_dir

    api_mod._cache.clear()
    api_mod._queue_dir_by_project.clear()
    api_mod._features_dir_by_project.clear()


class TestQueueDirProjectScoping:
    """Regression: with two projects both is_active, _get_effective_queue_dir()
    (no project_id) silently resolved EVERY caller against whichever ONE
    project happened to be picked by is_active's .first() -- these prove
    project_id, once passed, resolves that project specifically and keeps
    the two projects' queues fully isolated."""

    def test_add_and_list_are_isolated_per_project(self, two_project_client):
        client, proj_a_dir, proj_b_dir = two_project_client

        resp = client.post(
            "/api/autopilot/queue",
            json={"name": "Design A", "content": "a", "project_id": "proj-a"},
        )
        assert resp.status_code == 200, resp.text

        resp = client.post(
            "/api/autopilot/queue",
            json={"name": "Design B", "content": "b", "project_id": "proj-b"},
        )
        assert resp.status_code == 200, resp.text

        queue_a = client.get("/api/autopilot/queue?project_id=proj-a").json()
        queue_b = client.get("/api/autopilot/queue?project_id=proj-b").json()

        assert [d["name"] for d in queue_a] == ["Design A"]
        assert [d["name"] for d in queue_b] == ["Design B"]

        # File actually landed under the RIGHT project's own directory.
        assert any(proj_a_dir.rglob("Design_A.md"))
        assert any(proj_b_dir.rglob("Design_B.md"))
        assert not any(proj_a_dir.rglob("Design_B.md"))
        assert not any(proj_b_dir.rglob("Design_A.md"))

    def test_remove_only_affects_its_own_project(self, two_project_client):
        client, proj_a_dir, proj_b_dir = two_project_client

        client.post(
            "/api/autopilot/queue",
            json={"name": "Shared Name", "content": "a", "project_id": "proj-a"},
        )
        client.post(
            "/api/autopilot/queue",
            json={"name": "Shared Name", "content": "b", "project_id": "proj-b"},
        )

        resp = client.delete("/api/autopilot/queue/Shared_Name.md?project_id=proj-a")
        assert resp.status_code == 200, resp.text

        assert client.get("/api/autopilot/queue?project_id=proj-a").json() == []
        assert len(client.get("/api/autopilot/queue?project_id=proj-b").json()) == 1


# ── Features ─────────────────────────────────────────────────────


class TestFeatures:
    def test_empty_features(self, client):
        resp = client.get("/api/autopilot/features")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_features(self, client, autopilot_dirs):
        feature_dir = autopilot_dirs["features"] / "20260101-120000_my_feature"
        feature_dir.mkdir()
        docs = feature_dir / "docs"
        docs.mkdir()
        (docs / "pipeline_metrics.json").write_text(
            json.dumps(
                {
                    "product_validated": True,
                    "iterations": 2,
                    "total_time_seconds": 300,
                    "stop_reason": "completed",
                }
            )
        )
        (feature_dir / "feature_report.html").write_text("<html>report</html>")

        resp = client.get("/api/autopilot/features")
        assert len(resp.json()) == 1
        assert resp.json()[0]["status"] == "validated"
        assert resp.json()[0]["has_report"] is True

    def test_feature_detail(self, client, autopilot_dirs):
        feature_dir = autopilot_dirs["features"] / "20260101-120000_detail_test"
        feature_dir.mkdir()
        docs = feature_dir / "docs"
        docs.mkdir()
        (docs / "pipeline_metrics.json").write_text(
            json.dumps(
                {
                    "product_validated": False,
                    "stop_reason": "max_iterations",
                    "qa_passed": False,
                }
            )
        )
        (docs / "qa_report.md").write_text("# QA Report\nSome content here")

        resp = client.get("/api/autopilot/features/20260101-120000_detail_test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "needs_review"
        assert data["qa_passed"] is False
        assert len(data["qa_summary"]) > 0

    def test_feature_not_found(self, client):
        resp = client.get("/api/autopilot/features/nonexistent")
        assert resp.status_code == 404

    def test_feature_doc(self, client, autopilot_dirs):
        feature_dir = autopilot_dirs["features"] / "20260101-000000_art"
        feature_dir.mkdir()
        docs = feature_dir / "docs"
        docs.mkdir()
        (docs / "test.md").write_text("# Test document")

        resp = client.get("/api/autopilot/features/20260101-000000_art/docs/test.md")
        assert resp.status_code == 200
        assert resp.json()["content"] == "# Test document"


# ── Human Input ──────────────────────────────────────────────────


class TestHumanInput:
    def test_no_pending_input(self, client):
        resp = client.get("/api/autopilot/input")
        assert resp.status_code == 200
        assert resp.json() is None

    def test_submit_and_read(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        request_file = state_dir / "input_request_abc123.json"
        request_file.write_text(
            json.dumps(
                {
                    "id": "abc123",
                    "reason": "Test impasse",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "options": ["c", "s", "q"],
                    "labels": {"c": "Continue", "s": "Skip", "q": "Quit"},
                }
            )
        )

        resp = client.get("/api/autopilot/input")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "abc123"
        assert data["reason"] == "Test impasse"

    def test_submit_response(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        request_file = state_dir / "input_request_def456.json"
        request_file.write_text(
            json.dumps(
                {
                    "id": "def456",
                    "reason": "Credits exhausted",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "options": ["c", "s", "q"],
                    "labels": {},
                }
            )
        )

        resp = client.post(
            "/api/autopilot/input",
            json={
                "request_id": "def456",
                "choice": "c",
            },
        )
        assert resp.status_code == 200

        response_file = state_dir / "input_response_def456.json"
        assert response_file.exists()
        data = json.loads(response_file.read_text())
        assert data["choice"] == "c"

    def test_submit_invalid_choice(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        (state_dir / "input_request_x.json").write_text(
            json.dumps(
                {
                    "id": "x",
                    "reason": "r",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "options": [],
                    "labels": {},
                }
            )
        )

        resp = client.post(
            "/api/autopilot/input",
            json={
                "request_id": "x",
                "choice": "invalid",
            },
        )
        assert resp.status_code == 400

    def test_submit_to_missing_request(self, client):
        resp = client.post(
            "/api/autopilot/input",
            json={
                "request_id": "nonexistent",
                "choice": "c",
            },
        )
        assert resp.status_code == 404

    def test_dismiss(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        request_file = state_dir / "input_request_dismiss_me.json"
        request_file.write_text(
            json.dumps(
                {
                    "id": "dismiss_me",
                    "reason": "test",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "options": [],
                    "labels": {},
                }
            )
        )

        resp = client.delete("/api/autopilot/input/dismiss_me")
        assert resp.status_code == 200
        assert not request_file.exists()

    def test_stale_request_cleanup(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        request_file = state_dir / "input_request_stale.json"
        request_file.write_text(
            json.dumps(
                {
                    "id": "stale",
                    "reason": "old request",
                    "timestamp": old_ts,
                    "options": [],
                    "labels": {},
                }
            )
        )

        resp = client.get("/api/autopilot/input")
        assert resp.status_code == 200
        assert resp.json() is None
        assert not request_file.exists()


# ── Pipeline Status ──────────────────────────────────────────────


class TestPipelineStatus:
    def test_status_default(self, client):
        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False
        assert data["designs_processed"] == 0

    def test_status_with_state(self, client, autopilot_dirs):
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        state_dir = autopilot_dirs["state"]
        run_dir = state_dir / "run-20260101"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(
            json.dumps(
                {
                    "designs_processed": 5,
                    "designs_succeeded": 4,
                    "designs_failed": 1,
                    "current_design": "My Feature",
                    "total_elapsed": 3600,
                }
            )
        )

        resp = client.get("/api/autopilot/status")
        data = resp.json()
        assert data["designs_processed"] == 5
        assert data["current_design"] == "My Feature"

    def test_status_reports_which_project_is_running(self, client, autopilot_dirs, monkeypatch):
        """The (single, global) AutopilotService's running project must be
        surfaced so the frontend can tell the user what's actually running
        on a 409 conflict, instead of a generic "another project" message
        that's equally confusing whether it's a genuine cross-project
        conflict or the caller's own just-started run (a self-conflict from
        status-polling lag)."""
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        class FakeService:
            running = True

            def status(self):
                return {
                    "running": True,
                    "project_path": "/Users/test/some-project",
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        # No project_id given (global status) -- the handler asks the
        # registry for whatever's running, not a single global service.
        fake_registry = Mock()
        fake_registry.running.return_value = [FakeService()]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running_project_path"] == "/Users/test/some-project"
        # No AutopilotProject DB row registered for this path (no DB wired
        # in this test) -- falls back to the directory basename.
        assert data["running_project_name"] == "some-project"

    def test_status_sums_design_counts_across_concurrently_running_projects(
        self, client, autopilot_dirs, monkeypatch
    ):
        """Regression: with no project_id given, the global status endpoint
        used to report running_services[0].status() outright -- whichever
        project happened to be first in registry dict-iteration order.
        Concurrent projects are only possible at all since the multi-project
        concurrency diff; before that, there was only ever one service to
        ask. designs_processed/succeeded/failed must be summed across every
        running project, not just the first one's."""
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        class FakeService:
            def __init__(self, processed, succeeded, failed):
                self.running = True
                self._processed = processed
                self._succeeded = succeeded
                self._failed = failed

            def status(self):
                return {
                    "running": True,
                    "project_path": "/Users/test/project-a",
                    "current_design": None,
                    "designs_processed": self._processed,
                    "designs_succeeded": self._succeeded,
                    "designs_failed": self._failed,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [
            FakeService(5, 4, 1),
            FakeService(3, 2, 1),
        ]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["designs_processed"] == 8
        assert data["designs_succeeded"] == 6
        assert data["designs_failed"] == 2

    def test_status_reports_every_running_project_not_just_the_first(
        self, client, autopilot_dirs, monkeypatch
    ):
        """running_project_path/running_project_name are singular fields
        that can't represent more than one concurrently running project --
        running_projects must list every one of them (id/name/base_dir),
        so a caller hitting the concurrency cap can identify and stop
        exactly the project(s) blocking it instead of a bare stop-all call
        that would also kill an unrelated project it was never told about."""
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        class FakeService:
            def __init__(self, project_id, project_path):
                self.running = True
                self.project_id = project_id
                self._project_path = project_path

            def status(self):
                return {
                    "running": True,
                    "project_path": self._project_path,
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [
            FakeService("proj-a", "/Users/test/project-a"),
            FakeService("proj-b", "/Users/test/project-b"),
        ]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        running_projects = resp.json()["running_projects"]
        assert len(running_projects) == 2
        assert {p["id"] for p in running_projects} == {"proj-a", "proj-b"}
        assert {p["base_dir"] for p in running_projects} == {
            "/Users/test/project-a",
            "/Users/test/project-b",
        }

    def test_status_running_projects_survives_missing_project_id_attr(
        self, client, autopilot_dirs, monkeypatch
    ):
        """Must not crash if a service object doesn't expose project_id
        (defensive -- real AutopilotService always sets it, but this
        endpoint shouldn't 500 on a test double or future refactor that
        doesn't)."""
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        class FakeService:
            running = True

            def status(self):
                return {
                    "running": True,
                    "project_path": "/Users/test/some-project",
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [FakeService()]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        assert resp.json()["running_projects"][0]["id"] is None

    def test_self_conflict_detected_even_when_not_first_in_running_list(
        self, client, autopilot_dirs, monkeypatch
    ):
        """Regression: is_self_conflict only compared project_path against
        running_project_path, which (with no project_id given, exactly how
        the frontend's self-conflict check calls this endpoint) is just
        running_services[0]'s path -- arbitrary registry order. A caller
        whose own project IS running but isn't index 0 in that list got
        is_self_conflict=False, so the UI's 409 handler treated it as a
        genuine cross-project conflict and offered to "stop X and start X"."""
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        class FakeService:
            def __init__(self, project_id, project_path):
                self.running = True
                self.project_id = project_id
                self._project_path = project_path

            def status(self):
                return {
                    "running": True,
                    "project_path": self._project_path,
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [
            FakeService("proj-a", "/Users/test/project-a"),
            FakeService("proj-b", "/Users/test/project-b"),
        ]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get(
            "/api/autopilot/status",
            params={"project_path": "/Users/test/project-b"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_self_conflict"] is True

    def test_self_conflict_false_for_a_genuinely_different_project(
        self, client, autopilot_dirs, monkeypatch
    ):
        """Sanity check the fix isn't overbroad: a project_path that matches
        none of the running projects must still report is_self_conflict=False."""
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        class FakeService:
            def __init__(self, project_id, project_path):
                self.running = True
                self.project_id = project_id
                self._project_path = project_path

            def status(self):
                return {
                    "running": True,
                    "project_path": self._project_path,
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [
            FakeService("proj-a", "/Users/test/project-a"),
            FakeService("proj-b", "/Users/test/project-b"),
        ]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get(
            "/api/autopilot/status",
            params={"project_path": "/Users/test/project-c"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_self_conflict"] is False


# ── Messages ─────────────────────────────────────────────────────


class TestMessages:
    def test_messages_empty(self, client, autopilot_dirs):
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        resp = client.get("/api/autopilot/messages")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_messages_with_events(self, client, autopilot_dirs):
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        state_dir = autopilot_dirs["state"]
        run_dir = state_dir / "run-20260101"
        run_dir.mkdir(parents=True)
        events_file = run_dir / "events.jsonl"
        events_file.write_text(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "type": "design_started",
                    "name": "A",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "timestamp": "2026-01-01T00:01:00",
                    "type": "design_completed",
                    "name": "A",
                }
            )
            + "\n"
        )

        resp = client.get("/api/autopilot/messages?limit=10")
        assert len(resp.json()) == 2
        assert resp.json()[0]["type"] == "design_started"


# ── Logs ─────────────────────────────────────────────────────────


class TestLogs:
    def test_logs_empty(self, client, autopilot_dirs):
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        resp = client.get("/api/autopilot/logs")
        assert resp.status_code == 200
        assert resp.json()["lines"] == []

    def test_logs_with_content(self, client, autopilot_dirs):
        import src.mcp.autopilot_api as api_mod

        api_mod._cache.clear()

        state_dir = autopilot_dirs["state"]
        run_dir = state_dir / "run-20260101"
        run_dir.mkdir(parents=True)
        (run_dir / "orchestrator.log").write_text(
            "Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n"
        )

        resp = client.get("/api/autopilot/logs?lines=3")
        assert len(resp.json()["lines"]) == 3
        assert resp.json()["lines"][0] == "Line 3"


# ── Projects ─────────────────────────────────────────────────────


@pytest.fixture
def project_dirs(tmp_path):
    """Create a project directory with .hephaestus/designs containing test files."""
    project_dir = tmp_path / "myproject"
    design_dir = project_dir / ".hephaestus" / "designs"
    design_dir.mkdir(parents=True)

    (design_dir / "01-auth.md").write_text("# Auth Design\nImplement OAuth2.")
    (design_dir / "02-payments.md").write_text("# Payments\nStripe integration.")
    (design_dir / "readme.txt").write_text("General readme.")

    return {
        "project_dir": project_dir,
        "design_dir": design_dir,
    }


@pytest.fixture
def project_client(tmp_path, project_dirs, monkeypatch):
    """Test client with a temporary database for project tests."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)

    from src.core.database import DatabaseManager

    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, headers={"X-Agent-ID": "system"})

    import src.mcp.autopilot_api as api_mod

    api_mod._cache.clear()

    yield client, project_dirs

    api_mod._cache.clear()


class TestProjects:
    def test_create_project(self, project_client):
        client, dirs = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test Project",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Project"
        assert data["design_count"] == 3
        assert data["id"].startswith("proj-")

    def test_create_project_auto_syncs_designs(self, project_client):
        client, dirs = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = resp.json()["id"]

        resp = client.get(f"/api/autopilot/projects/{project_id}/designs")
        assert resp.status_code == 200
        designs = resp.json()
        assert len(designs) == 3
        # Ordinal-prefixed files come first
        assert designs[0]["filename"] == "01-auth.md"
        assert designs[0]["ordinal"] == 1
        assert designs[1]["filename"] == "02-payments.md"
        assert designs[1]["ordinal"] == 2

    def test_create_project_nonexistent_dir(self, project_client):
        client, _ = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Bad",
                "base_dir": "/nonexistent/path",
            },
        )
        assert resp.status_code == 400

    def test_create_project_duplicate_dir(self, project_client):
        client, dirs = project_client
        client.post(
            "/api/autopilot/projects",
            json={
                "name": "First",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Second",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        assert resp.status_code == 409

    def test_list_projects(self, project_client):
        client, dirs = project_client
        client.post(
            "/api/autopilot/projects",
            json={
                "name": "P1",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        resp = client.get("/api/autopilot/projects")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["name"] == "P1"

    def test_get_project(self, project_client):
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = create.json()["id"]

        resp = client.get(f"/api/autopilot/projects/{project_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == project_id

    def test_get_project_not_found(self, project_client):
        client, _ = project_client
        resp = client.get("/api/autopilot/projects/nonexistent")
        assert resp.status_code == 404

    def test_update_project_name(self, project_client):
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Old Name",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = create.json()["id"]

        resp = client.put(
            f"/api/autopilot/projects/{project_id}",
            json={
                "name": "New Name",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    def test_update_project_is_default(self, project_client):
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = create.json()["id"]

        resp = client.put(
            f"/api/autopilot/projects/{project_id}",
            json={
                "is_default": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True

    def test_delete_project(self, project_client):
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={
                "name": "To Delete",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = create.json()["id"]

        resp = client.delete(f"/api/autopilot/projects/{project_id}")
        assert resp.status_code == 200

        resp = client.get(f"/api/autopilot/projects/{project_id}")
        assert resp.status_code == 404

    def test_delete_project_not_found(self, project_client):
        client, _ = project_client
        resp = client.delete("/api/autopilot/projects/nonexistent")
        assert resp.status_code == 404


class TestProjectDesigns:
    def _create_project(self, client, dirs):
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        return resp.json()["id"]

    def test_list_designs(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.get(f"/api/autopilot/projects/{pid}/designs")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_designs_sorted_by_ordinal(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.get(f"/api/autopilot/projects/{pid}/designs")
        designs = resp.json()
        ordinals = [d["ordinal"] for d in designs]
        assert ordinals == sorted(ordinals)

    def test_add_design(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "New Feature",
                "content": "# New Feature\nDescription here.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New Feature"
        assert data["extension"] == ".md"

        # Verify file was created on disk
        design_dir = dirs["design_dir"]
        assert (design_dir / "New_Feature.md").exists()

    def test_add_design_duplicate(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Dup Test",
                "content": "first",
            },
        )
        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Dup Test",
                "content": "second",
            },
        )
        assert resp.status_code == 409

    def test_remove_design(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/01-auth.md")
        assert resp.status_code == 200

        # Verify file was deleted
        design_dir = dirs["design_dir"]
        assert not (design_dir / "01-auth.md").exists()

        # Verify DB record was deleted
        resp = client.get(f"/api/autopilot/projects/{pid}/designs")
        assert len(resp.json()) == 2

    def test_remove_design_not_found(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/nonexistent.md")
        assert resp.status_code == 404

    def test_get_design_content(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/01-auth.md/content")
        assert resp.status_code == 200
        assert "OAuth2" in resp.json()["content"]

    def test_reorder_designs(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        designs = client.get(f"/api/autopilot/projects/{pid}/designs").json()
        reversed_ids = [d["id"] for d in reversed(designs)]

        resp = client.put(
            f"/api/autopilot/projects/{pid}/designs/reorder",
            json={
                "design_ids": reversed_ids,
            },
        )
        assert resp.status_code == 200

        reordered = client.get(f"/api/autopilot/projects/{pid}/designs").json()
        assert reordered[0]["id"] == reversed_ids[0]

    def test_reorder_invalid_id(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.put(
            f"/api/autopilot/projects/{pid}/designs/reorder",
            json={
                "design_ids": ["nonexistent-id"],
            },
        )
        assert resp.status_code == 400

    def test_sync_project(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        # Add a new file to the filesystem
        design_dir = dirs["design_dir"]
        (design_dir / "03-api.md").write_text("# API Design")

        resp = client.post(f"/api/autopilot/projects/{pid}/sync")
        assert resp.status_code == 200
        designs = resp.json()
        assert len(designs) == 4
        filenames = [d["filename"] for d in designs]
        assert "03-api.md" in filenames

    def test_sync_removes_deleted_files(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        # Delete a file from filesystem
        design_dir = dirs["design_dir"]
        (design_dir / "01-auth.md").unlink()

        resp = client.post(f"/api/autopilot/projects/{pid}/sync")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_design_not_found_project(self, project_client):
        client, _ = project_client
        resp = client.get("/api/autopilot/projects/nonexistent/designs")
        assert resp.status_code == 404

    def test_remove_design_cascades_orphaned_workflow_with_no_design_id(
        self, project_client
    ):
        """Regression test: observed live in production that a completed
        Phase 0 workflow (and its first per-feature workflow) can end up with
        Workflow.design_id left NULL and Feature.workflow_id never linked
        (both cascade lookups miss it), so deleting the design that spawned
        them left the workflows permanently orphaned -- they survived the
        delete and their tasks/phase executions kept accumulating. The fix
        adds a fallback match on launch_params (design_id or
        design_document filename), the same way _relink_features_to_workflows
        already matches Feature.workflow_id when the direct link is missing.
        """
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import (
            AutopilotDesign,
            Workflow,
            WorkflowDefinition,
            get_db,
        )

        design_id = "des-test-orphan"
        with get_db() as db:
            # Workflow.definition_id is a foreign key to workflow_definitions
            # -- these rows must exist for the Workflow inserts below to
            # satisfy PRAGMA foreign_keys=ON.
            db.add(WorkflowDefinition(id="feature_architect", name="Feature Architect"))
            db.add(WorkflowDefinition(id="autopilot", name="Autopilot"))

            db.add(
                AutopilotDesign(
                    id=design_id,
                    project_id=pid,
                    filename="orphan-design.md",
                    name="Orphan Design",
                    ordinal=10,
                    size_bytes=10,
                    extension=".md",
                    status="pending",
                )
            )

            # Orphaned Phase 0 workflow: design_id column never got set, but
            # launch_params carries the real design_id.
            db.add(
                Workflow(
                    id="wf-orphan-phase0",
                    name="feature_architect",
                    description="Phase 0: Feature Architect for Orphan",
                    definition_id="feature_architect",
                    design_id=None,
                    phases_folder_path=".",
                    status="completed",
                    launch_params={
                        "design_document": str(dirs["design_dir"] / "orphan-design.md"),
                        "project_path": str(dirs["project_dir"]),
                        "design_id": design_id,
                    },
                )
            )
            # Orphaned per-feature workflow: neither design_id nor
            # Feature.workflow_id got linked, but launch_params still points
            # at the same design document.
            db.add(
                Workflow(
                    id="wf-orphan-feature",
                    name="autopilot",
                    description="Autopilot: Orphan - Feature: Core",
                    definition_id="autopilot",
                    design_id=None,
                    phases_folder_path=".",
                    status="failed",
                    launch_params={
                        "design_document": str(dirs["design_dir"] / "orphan-design.md"),
                        "project_path": str(dirs["project_dir"]),
                        "feature_id": "core",
                    },
                )
            )
            db.commit()

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/orphan-design.md")
        assert resp.status_code == 200, resp.text

        with get_db() as db:
            remaining = (
                db.query(Workflow)
                .filter(Workflow.id.in_(["wf-orphan-phase0", "wf-orphan-feature"]))
                .all()
            )
            assert remaining == []

    def test_design_status_surfaces_failure_reason(self, project_client):
        """Regression: AutopilotDesign had no column to store *why* a design
        failed -- orchestrator.py's run_phase0 always passed error=... to
        _update_design_status, but it was silently dropped ("unknown field
        'error'") and the design detail modal had nothing to show, even for
        a specific, actionable reason. The status endpoint must surface it
        once the design is actually failed."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "designs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "failed-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-failed",
                    project_id=pid,
                    filename="failed-design.md",
                    name="Failed Design",
                    ordinal=10,
                    size_bytes=10,
                    extension=".md",
                    status="failed",
                    error="Invalid features.json: features array must have at least 1 entry, got 0",
                )
            )
            db.commit()

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/failed-design.md/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "failed"
        assert body["error"] == (
            "Invalid features.json: features array must have at least 1 entry, got 0"
        )

    def test_design_status_includes_cost_total(self, project_client):
        """cost_total_usd must be surfaced per-feature and summed at the
        design level so the UI can show a budget indicator without an extra
        network call."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Feature, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "designs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "cost-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-cost",
                    project_id=pid,
                    filename="cost-design.md",
                    name="Cost Design",
                    ordinal=13,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-cost-1",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    status="active",
                )
            )

        with get_db() as db:
            db.add(
                Feature(
                    id="feat-cost-1",
                    design_id="des-test-cost",
                    feature_key="core",
                    name="Core",
                    scope="s",
                    status="active",
                    workflow_id="wf-cost-1",
                    cost_total_usd=1.5,
                )
            )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/cost-design.md/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["features"][0]["cost_total_usd"] == 1.5
        assert body["cost_total_usd"] == 1.5

    def test_design_status_surfaces_budget_pause_reason(self, project_client):
        """A budget-triggered pause must be distinguishable from a plain
        user pause: the design-status endpoint (polled by DesignQueuePanel)
        has to surface paused_by/status_reason, same as WorkflowCard already
        does for the workflow-list page, otherwise a budget pause renders
        as an indistinguishable generic 'Paused' badge."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "designs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "budget-paused-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-budget-paused",
                    project_id=pid,
                    filename="budget-paused-design.md",
                    name="Budget Paused Design",
                    ordinal=14,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-budget-paused-1",
                    name="autopilot",
                    definition_id="autopilot",
                    phases_folder_path="/tmp",
                    status="paused",
                    paused_by="budget",
                    status_reason="Budget limit reached",
                    launch_params={
                        "design_document": str(design_dir / "budget-paused-design.md"),
                        "project_path": str(dirs["project_dir"]),
                    },
                )
            )

        resp = client.get(
            f"/api/autopilot/projects/{pid}/designs/budget-paused-design.md/status"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "paused"
        assert body["paused_by"] == "budget"
        assert body["status_reason"] == "Budget limit reached"
        assert body["workflows"][0]["paused_by"] == "budget"
        assert body["workflows"][0]["status_reason"] == "Budget limit reached"

    def test_design_status_omits_error_when_not_failed(self, project_client):
        """The error field shouldn't leak a stale message from a previous
        failed attempt once the design is no longer in a failed state."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "designs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "ok-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-ok",
                    project_id=pid,
                    filename="ok-design.md",
                    name="OK Design",
                    ordinal=11,
                    size_bytes=10,
                    extension=".md",
                    status="pending",
                    error="stale error from a previous run",
                )
            )
            db.commit()

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/ok-design.md/status")
        assert resp.status_code == 200, resp.text
        assert resp.json()["error"] is None

    def test_task_row_shows_config_description_and_goto_reason(self, project_client):
        """Regression: the queue's task rows used to show the raw task
        prompt verbatim ("Execute development: ...\\n\\nWHY YOU'RE HERE:
        ..."), truncated to 200 chars -- noisy, and a long phase
        description could push the actual goto reason past the truncation
        point entirely. phase_description must come from the phase's own
        config-sourced Phase.description (not re-parsed from the task
        text), and goto_reason must be the clean text after
        GOTO_REASON_PREFIX, parsed from the FULL untruncated description."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.constants import GOTO_REASON_PREFIX
        from src.core.database import (
            AutopilotDesign,
            Feature,
            Phase,
            Task,
            Workflow,
            get_db,
        )

        design_dir = dirs["project_dir"] / ".hephaestus" / "designs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "goto-design.md").write_text("# Design")

        # Separate get_db() blocks, in FK dependency order (Workflow before
        # Feature/Phase, both before Task) -- each commits before the next
        # starts, sidestepping any reliance on the ORM's automatic flush
        # ordering for a same-transaction multi-insert.
        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-goto",
                    project_id=pid,
                    filename="goto-design.md",
                    name="Goto Design",
                    ordinal=12,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-goto-1",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    status="active",
                )
            )

        with get_db() as db:
            db.add(
                Feature(
                    id="feat-goto-1",
                    design_id="des-test-goto",
                    feature_key="core",
                    name="Core",
                    scope="s",
                    status="active",
                    workflow_id="wf-goto-1",
                )
            )
            db.add(
                Phase(
                    id="phase-goto-1",
                    workflow_id="wf-goto-1",
                    order=4,
                    name="development",
                    description="Implement all components according to the architecture.",
                    done_definitions=["x"],
                )
            )

        with get_db() as db:
            # A padded-length base description, long enough that a naive
            # 200-char truncation of the combined text would cut off the
            # goto reason below it -- pins down that goto_reason is parsed
            # from the FULL description, not the truncated `description`
            # field.
            padding = "x" * 250
            full_description = (
                f"Execute development: {padding}\n\n"
                f"{GOTO_REASON_PREFIX}6 BLOCKER findings in adversarial review\n"
                "Address this specifically -- this is not a fresh implementation "
                "pass, it's a return from review with a concrete issue to fix."
            )
            db.add(
                Task(
                    id="task-goto-1",
                    raw_description=full_description,
                    enriched_description=full_description,
                    done_definition="d",
                    status="pending",
                    phase_id="phase-goto-1",
                    workflow_id="wf-goto-1",
                    action="goto",
                    action_target_phase="development",
                )
            )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/goto-design.md/status")
        assert resp.status_code == 200, resp.text
        tasks = resp.json()["features"][0]["tasks"]
        assert len(tasks) == 1
        task = tasks[0]
        assert task["phase_description"] == (
            "Implement all components according to the architecture."
        )
        assert task["goto_reason"] == "6 BLOCKER findings in adversarial review"
        assert task["action"] == "goto"
        assert task["action_target_phase"] == "development"


class TestProjectPathTraversal:
    def test_design_content_rejects_traversal(self, project_client):
        client, dirs = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        pid = resp.json()["id"]

        resp = client.get(
            f"/api/autopilot/projects/{pid}/designs/../../etc/passwd/content"
        )
        assert resp.status_code in (400, 404)

    def test_design_remove_rejects_traversal(self, project_client):
        client, dirs = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        pid = resp.json()["id"]

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/../../etc/passwd")
        assert resp.status_code in (400, 404)


class TestCostEntryAgentBinding:
    """ticket-5a75167a: POST /cost-entries authenticated a caller's identity
    but never bound it to the entry being written -- a caller authenticated
    as one real agent could supply a *different* agent_id in the body and
    post a cost entry impersonating another agent's task. System/SDK
    identities have no single agent to bind to (they post cost entries on
    behalf of whichever agent/task they're servicing), so only a real
    per-agent UUID caller is bound to its own authenticated identity."""

    def _mock_cost_stack(self, monkeypatch):
        from contextlib import contextmanager
        from unittest.mock import MagicMock

        import src.core.cost_derivation as cost_derivation_mod
        import src.core.database as database_mod

        recorded = {}

        def fake_record_cost(**kwargs):
            recorded.update(kwargs)
            entry = MagicMock()
            entry.id = "entry-1"
            entry.cost_usd = kwargs["cost_usd"]
            return entry

        monkeypatch.setattr(cost_derivation_mod, "record_cost", fake_record_cost)

        @contextmanager
        def fake_get_db():
            yield MagicMock()

        monkeypatch.setattr(database_mod, "get_db", fake_get_db)
        return recorded

    def test_real_agent_cannot_claim_a_different_agent_id(self, client, monkeypatch):
        import src.mcp.autopilot_api as api_mod

        monkeypatch.setattr(
            api_mod, "verify_agent_authentication", AsyncMock(return_value=True)
        )
        self._mock_cost_stack(monkeypatch)

        resp = client.post(
            "/api/autopilot/cost-entries",
            json={
                "task_id": "someone-elses-task",
                "agent_id": "someone-elses-agent",
                "source": "pi",
                "cost_usd": 0.01,
            },
            headers={"X-Agent-ID": "11111111-1111-1111-1111-111111111111"},
        )
        assert resp.status_code == 403

    def test_real_agent_id_matching_header_is_allowed(self, client, monkeypatch):
        import src.mcp.autopilot_api as api_mod

        monkeypatch.setattr(
            api_mod, "verify_agent_authentication", AsyncMock(return_value=True)
        )
        recorded = self._mock_cost_stack(monkeypatch)

        own_id = "11111111-1111-1111-1111-111111111111"
        resp = client.post(
            "/api/autopilot/cost-entries",
            json={
                "task_id": "my-task",
                "agent_id": own_id,
                "source": "pi",
                "cost_usd": 0.01,
            },
            headers={"X-Agent-ID": own_id},
        )
        assert resp.status_code == 200
        assert recorded["agent_id"] == own_id

    def test_system_identity_may_post_on_behalf_of_any_agent(self, client, monkeypatch):
        import src.mcp.autopilot_api as api_mod

        monkeypatch.setattr(
            api_mod, "verify_agent_authentication", AsyncMock(return_value=True)
        )
        recorded = self._mock_cost_stack(monkeypatch)

        resp = client.post(
            "/api/autopilot/cost-entries",
            json={
                "task_id": "some-task",
                "agent_id": "some-other-real-agent",
                "source": "pi",
                "cost_usd": 0.01,
            },
            headers={"X-Agent-ID": "orchestrator"},
        )
        assert resp.status_code == 200
        assert recorded["agent_id"] == "some-other-real-agent"
