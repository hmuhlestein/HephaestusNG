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
def client(autopilot_dirs):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from src.mcp.autopilot_api import router

    app = FastAPI()
    app.include_router(router)
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
        artifacts = feature_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "pipeline_metrics.json").write_text(json.dumps({
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
        artifacts = feature_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "pipeline_metrics.json").write_text(json.dumps({
            "product_validated": False,
            "stop_reason": "max_iterations",
            "qa_passed": False,
        }))
        (artifacts / "qa_report.md").write_text("# QA Report\nSome content here")

        resp = client.get("/api/autopilot/features/20260101-120000_detail_test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "needs_review"
        assert data["qa_passed"] is False
        assert len(data["qa_summary"]) > 0

    def test_feature_not_found(self, client):
        resp = client.get("/api/autopilot/features/nonexistent")
        assert resp.status_code == 404

    def test_feature_artifact(self, client, autopilot_dirs):
        feature_dir = autopilot_dirs["features"] / "20260101-000000_art"
        feature_dir.mkdir()
        artifacts = feature_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "test.md").write_text("# Test artifact")

        resp = client.get("/api/autopilot/features/20260101-000000_art/artifacts/test.md")
        assert resp.status_code == 200
        assert resp.json()["content"] == "# Test artifact"


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
