"""Tests for autopilot API endpoints."""

import json
import os
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

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
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from src.mcp import autopilot_api
    from src.mcp.autopilot_api import router

    app = FastAPI()
    app.include_router(router)

    # Mock _get_active_project_id to return None (no DB needed)
    monkeypatch.setattr(autopilot_api, '_get_active_project_id', lambda: None)

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
        resp = client.post("/api/autopilot/queue", json={
            "name": "../../etc/cron",
            "content": "test",
        })
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
        resp = client.post("/api/autopilot/queue", json={
            "name": "Test Feature",
            "content": "# Design\nHello world",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Feature"
        assert data["extension"] == ".md"

        resp = client.get("/api/autopilot/queue")
        assert len(resp.json()) == 1
        assert resp.json()[0]["filename"] == data["filename"]

    def test_add_duplicate_rejects(self, client):
        client.post("/api/autopilot/queue", json={
            "name": "Dup Test",
            "content": "content",
        })
        resp = client.post("/api/autopilot/queue", json={
            "name": "Dup Test",
            "content": "content",
        })
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

        resp = client.post("/api/autopilot/queue/reorder", json={
            "filenames": ["c.md", "a.md", "b.md"]
        })
        assert resp.status_code == 200

        resp = client.get("/api/autopilot/queue")
        filenames = [i["filename"] for i in resp.json()]
        assert filenames == ["c.md", "a.md", "b.md"]

    def test_reorder_rejects_unknown(self, client, autopilot_dirs):
        (autopilot_dirs["queue"] / "a.md").write_text("a")

        resp = client.post("/api/autopilot/queue/reorder", json={
            "filenames": ["a.md", "ghost.md"]
        })
        assert resp.status_code == 400


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

        client.post("/api/autopilot/queue", json={
            "name": "Cache Test",
            "content": "x",
        })

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
        (docs / "pipeline_metrics.json").write_text(json.dumps({
            "product_validated": True,
            "iterations": 2,
            "total_time_seconds": 300,
            "stop_reason": "completed",
        }))
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
        (docs / "pipeline_metrics.json").write_text(json.dumps({
            "product_validated": False,
            "stop_reason": "max_iterations",
            "qa_passed": False,
        }))
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
        request_file.write_text(json.dumps({
            "id": "abc123",
            "reason": "Test impasse",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "options": ["c", "s", "q"],
            "labels": {"c": "Continue", "s": "Skip", "q": "Quit"},
        }))

        resp = client.get("/api/autopilot/input")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "abc123"
        assert data["reason"] == "Test impasse"

    def test_submit_response(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        request_file = state_dir / "input_request_def456.json"
        request_file.write_text(json.dumps({
            "id": "def456",
            "reason": "Credits exhausted",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "options": ["c", "s", "q"],
            "labels": {},
        }))

        resp = client.post("/api/autopilot/input", json={
            "request_id": "def456",
            "choice": "c",
        })
        assert resp.status_code == 200

        response_file = state_dir / "input_response_def456.json"
        assert response_file.exists()
        data = json.loads(response_file.read_text())
        assert data["choice"] == "c"

    def test_submit_invalid_choice(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        (state_dir / "input_request_x.json").write_text(json.dumps({
            "id": "x", "reason": "r",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "options": [], "labels": {},
        }))

        resp = client.post("/api/autopilot/input", json={
            "request_id": "x",
            "choice": "invalid",
        })
        assert resp.status_code == 400

    def test_submit_to_missing_request(self, client):
        resp = client.post("/api/autopilot/input", json={
            "request_id": "nonexistent",
            "choice": "c",
        })
        assert resp.status_code == 404

    def test_dismiss(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        request_file = state_dir / "input_request_dismiss_me.json"
        request_file.write_text(json.dumps({
            "id": "dismiss_me",
            "reason": "test",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "options": [], "labels": {},
        }))

        resp = client.delete("/api/autopilot/input/dismiss_me")
        assert resp.status_code == 200
        assert not request_file.exists()

    def test_stale_request_cleanup(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        request_file = state_dir / "input_request_stale.json"
        request_file.write_text(json.dumps({
            "id": "stale",
            "reason": "old request",
            "timestamp": old_ts,
            "options": [], "labels": {},
        }))

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
        (run_dir / "state.json").write_text(json.dumps({
            "designs_processed": 5,
            "designs_succeeded": 4,
            "designs_failed": 1,
            "current_design": "My Feature",
            "total_elapsed": 3600,
        }))

        resp = client.get("/api/autopilot/status")
        data = resp.json()
        assert data["designs_processed"] == 5
        assert data["current_design"] == "My Feature"


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
            json.dumps({"timestamp": "2026-01-01T00:00:00", "type": "design_started", "name": "A"}) + "\n" +
            json.dumps({"timestamp": "2026-01-01T00:01:00", "type": "design_completed", "name": "A"}) + "\n"
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
    """Create a project directory with docs/design-queue containing test files."""
    project_dir = tmp_path / "myproject"
    design_dir = project_dir / "docs" / "design-queue"
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
    import os
    db_path = str(tmp_path / "test.db")
    os.environ["HEPHAESTUS_TEST_DB"] = db_path

    from src.core.database import DatabaseManager
    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from src.mcp.autopilot_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    import src.mcp.autopilot_api as api_mod
    api_mod._cache.clear()

    yield client, project_dirs

    os.environ.pop("HEPHAESTUS_TEST_DB", None)
    api_mod._cache.clear()


class TestProjects:
    def test_create_project(self, project_client):
        client, dirs = project_client
        resp = client.post("/api/autopilot/projects", json={
            "name": "Test Project",
            "base_dir": str(dirs["project_dir"]),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Project"
        assert data["design_count"] == 3
        assert data["id"].startswith("proj-")

    def test_create_project_auto_syncs_designs(self, project_client):
        client, dirs = project_client
        resp = client.post("/api/autopilot/projects", json={
            "name": "Test",
            "base_dir": str(dirs["project_dir"]),
        })
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
        resp = client.post("/api/autopilot/projects", json={
            "name": "Bad",
            "base_dir": "/nonexistent/path",
        })
        assert resp.status_code == 400

    def test_create_project_duplicate_dir(self, project_client):
        client, dirs = project_client
        client.post("/api/autopilot/projects", json={
            "name": "First",
            "base_dir": str(dirs["project_dir"]),
        })
        resp = client.post("/api/autopilot/projects", json={
            "name": "Second",
            "base_dir": str(dirs["project_dir"]),
        })
        assert resp.status_code == 409

    def test_list_projects(self, project_client):
        client, dirs = project_client
        client.post("/api/autopilot/projects", json={
            "name": "P1",
            "base_dir": str(dirs["project_dir"]),
        })
        resp = client.get("/api/autopilot/projects")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["name"] == "P1"

    def test_get_project(self, project_client):
        client, dirs = project_client
        create = client.post("/api/autopilot/projects", json={
            "name": "Test",
            "base_dir": str(dirs["project_dir"]),
        })
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
        create = client.post("/api/autopilot/projects", json={
            "name": "Old Name",
            "base_dir": str(dirs["project_dir"]),
        })
        project_id = create.json()["id"]

        resp = client.put(f"/api/autopilot/projects/{project_id}", json={
            "name": "New Name",
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    def test_update_project_is_default(self, project_client):
        client, dirs = project_client
        create = client.post("/api/autopilot/projects", json={
            "name": "Test",
            "base_dir": str(dirs["project_dir"]),
        })
        project_id = create.json()["id"]

        resp = client.put(f"/api/autopilot/projects/{project_id}", json={
            "is_default": True,
        })
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True

    def test_delete_project(self, project_client):
        client, dirs = project_client
        create = client.post("/api/autopilot/projects", json={
            "name": "To Delete",
            "base_dir": str(dirs["project_dir"]),
        })
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
        resp = client.post("/api/autopilot/projects", json={
            "name": "Test",
            "base_dir": str(dirs["project_dir"]),
        })
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

        resp = client.post(f"/api/autopilot/projects/{pid}/designs", json={
            "name": "New Feature",
            "content": "# New Feature\nDescription here.",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New Feature"
        assert data["extension"] == ".md"

        # Verify file was created on disk
        design_dir = dirs["project_dir"] / "docs" / "design-queue"
        assert (design_dir / "New_Feature.md").exists()

    def test_add_design_duplicate(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        client.post(f"/api/autopilot/projects/{pid}/designs", json={
            "name": "Dup Test",
            "content": "first",
        })
        resp = client.post(f"/api/autopilot/projects/{pid}/designs", json={
            "name": "Dup Test",
            "content": "second",
        })
        assert resp.status_code == 409

    def test_remove_design(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/01-auth.md")
        assert resp.status_code == 200

        # Verify file was deleted
        design_dir = dirs["project_dir"] / "docs" / "design-queue"
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

        resp = client.put(f"/api/autopilot/projects/{pid}/designs/reorder", json={
            "design_ids": reversed_ids,
        })
        assert resp.status_code == 200

        reordered = client.get(f"/api/autopilot/projects/{pid}/designs").json()
        assert reordered[0]["id"] == reversed_ids[0]

    def test_reorder_invalid_id(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.put(f"/api/autopilot/projects/{pid}/designs/reorder", json={
            "design_ids": ["nonexistent-id"],
        })
        assert resp.status_code == 400

    def test_sync_project(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        # Add a new file to the filesystem
        design_dir = dirs["project_dir"] / "docs" / "design-queue"
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
        design_dir = dirs["project_dir"] / "docs" / "design-queue"
        (design_dir / "01-auth.md").unlink()

        resp = client.post(f"/api/autopilot/projects/{pid}/sync")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_design_not_found_project(self, project_client):
        client, _ = project_client
        resp = client.get("/api/autopilot/projects/nonexistent/designs")
        assert resp.status_code == 404


class TestProjectPathTraversal:
    def test_design_content_rejects_traversal(self, project_client):
        client, dirs = project_client
        resp = client.post("/api/autopilot/projects", json={
            "name": "Test",
            "base_dir": str(dirs["project_dir"]),
        })
        pid = resp.json()["id"]

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/../../etc/passwd/content")
        assert resp.status_code in (400, 404)

    def test_design_remove_rejects_traversal(self, project_client):
        client, dirs = project_client
        resp = client.post("/api/autopilot/projects", json={
            "name": "Test",
            "base_dir": str(dirs["project_dir"]),
        })
        pid = resp.json()["id"]

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/../../etc/passwd")
        assert resp.status_code in (400, 404)
