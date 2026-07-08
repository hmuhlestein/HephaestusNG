"""Tests for autopilot API endpoints."""

import json
import os
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
        (project_dir / "docs" / "design").mkdir(parents=True)
        design_file = project_dir / "docs" / "design" / "my_design.md"
        design_file.write_text("# Design")

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(return_value={"started": True})
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda: fake_service
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
        (project_dir / "docs" / "design").mkdir(parents=True)
        (project_dir / "docs" / "design" / "my_design.md").write_text("# Design")

        fake_service = Mock()
        fake_service.running = True  # already running -- rerun must stop it first
        fake_service.start = AsyncMock(return_value={"started": True})
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda: fake_service
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
        (project_dir / "docs" / "design").mkdir(parents=True)
        (project_dir / "docs" / "design" / "my_design.md").write_text("# Design")

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(
            side_effect=RuntimeError("Pipeline is already running")
        )
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda: fake_service
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
        (project_dir / "docs" / "design").mkdir(parents=True)
        (project_dir / "docs" / "design" / "my_design.md").write_text("# Design")

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(
            side_effect=ValueError("Project path is not a git repository")
        )
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda: fake_service
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 400, resp.text


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

        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda: FakeService()
        )

        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running_project_path"] == "/Users/test/some-project"
        # No AutopilotProject DB row registered for this path (no DB wired
        # in this test) -- falls back to the directory basename.
        assert data["running_project_name"] == "some-project"


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
    """Create a project directory with docs/design containing test files."""
    project_dir = tmp_path / "myproject"
    design_dir = project_dir / "docs" / "design"
    design_dir.mkdir(parents=True)

    (design_dir / "01-auth.md").write_text("# Auth Design\nImplement OAuth2.")
    (design_dir / "02-payments.md").write_text("# Payments\nStripe integration.")
    (design_dir / "readme.txt").write_text("General readme.")

    return {
        "project_dir": project_dir,
        "design_dir": design_dir,
    }


@pytest.fixture
def project_client(tmp_path, project_dirs):
    """Test client with a temporary database for project tests."""
    db_path = str(tmp_path / "test.db")
    os.environ["HEPHAESTUS_TEST_DB"] = db_path

    from src.core.database import DatabaseManager

    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    import src.mcp.autopilot_api as api_mod

    api_mod._cache.clear()

    yield client, project_dirs

    # Restore conftest.py's default instead of removing the key entirely —
    # popping it left any test running immediately after this one (without
    # its own override) falling through to get_db()'s literal default
    # ("hephaestus.db"), silently writing test data into the real
    # production database instead of the isolated :memory: default.
    os.environ["HEPHAESTUS_TEST_DB"] = ":memory:"
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

        from src.core.database import AutopilotDesign, Workflow, get_db

        design_id = "des-test-auth"
        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id=design_id,
                    project_id=pid,
                    filename="01-auth.md",
                    name="Auth",
                    ordinal=1,
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
                    name="autopilot-phase0",
                    description="Phase 0: Feature Architect for Auth",
                    definition_id="autopilot-phase0",
                    design_id=None,
                    phases_folder_path=".",
                    status="completed",
                    launch_params={
                        "design_document": str(dirs["design_dir"] / "01-auth.md"),
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
                    description="Autopilot: Auth - Feature: Core",
                    definition_id="autopilot",
                    design_id=None,
                    phases_folder_path=".",
                    status="failed",
                    launch_params={
                        "design_document": str(dirs["design_dir"] / "01-auth.md"),
                        "project_path": str(dirs["project_dir"]),
                        "feature_id": "core",
                    },
                )
            )
            db.commit()

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/01-auth.md")
        assert resp.status_code == 200, resp.text

        with get_db() as db:
            remaining = (
                db.query(Workflow)
                .filter(Workflow.id.in_(["wf-orphan-phase0", "wf-orphan-feature"]))
                .all()
            )
            assert remaining == []


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
