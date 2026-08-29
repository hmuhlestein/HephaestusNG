"""Tests for autopilot_api.py — pure helpers and cache logic."""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

# ── _safe_path ────────────────────────────────────────────────────


class TestSafePath:
    def test_safe_path_basic(self):
        from src.mcp.autopilot._shared import _safe_path

        result = _safe_path("/base", "subdir", "file.txt")
        assert result == Path("/base/subdir/file.txt")

    def test_safe_path_traversal_rejected(self):
        from fastapi import HTTPException

        from src.mcp.autopilot._shared import _safe_path

        with pytest.raises(HTTPException):
            _safe_path("/base", "../etc/passwd")

    def test_safe_path_single_part(self):
        from src.mcp.autopilot._shared import _safe_path

        result = _safe_path("/base", "file.txt")
        assert result == Path("/base/file.txt")


# ── _cache / _store / _invalidate ─────────────────────────────────


class TestCache:
    def test_store_and_get(self):
        from src.mcp.autopilot._shared import _cached, _invalidate, _store

        _store("test_key", {"data": "hello"})
        result = _cached("test_key", ttl=10)
        assert result == {"data": "hello"}
        _invalidate("test_key")
        assert _cached("test_key", ttl=10) is None

    def test_cache_expires(self):
        from src.mcp.autopilot._shared import _cached, _invalidate, _store

        _store("expire_key", {"data": "old"})
        result = _cached("expire_key", ttl=0.001)
        assert result is not None
        time.sleep(0.01)
        result = _cached("expire_key", ttl=0.001)
        assert result is None
        _invalidate("expire_key")

    def test_invalidate_multiple(self):
        from src.mcp.autopilot._shared import _cached, _invalidate, _store

        _store("k1", "v1")
        _store("k2", "v2")
        _invalidate("k1", "k2")
        assert _cached("k1") is None
        assert _cached("k2") is None


# ── _design_id ────────────────────────────────────────────────────


class TestDesignId:
    def test_deterministic(self):
        from src.mcp.autopilot.project_routes import _design_id

        id1 = _design_id("proj-1", "feature.md")
        id2 = _design_id("proj-1", "feature.md")
        assert id1 == id2

    def test_different_inputs(self):
        from src.mcp.autopilot.project_routes import _design_id

        id1 = _design_id("proj-1", "feature.md")
        id2 = _design_id("proj-1", "bugfix.md")
        assert id1 != id2

    def test_format(self):
        from src.mcp.autopilot.project_routes import _design_id

        result = _design_id("proj-1", "feature.md")
        assert isinstance(result, str)
        assert len(result) > 0


# ── _feature_status ───────────────────────────────────────────────


class TestFeatureStatus:
    def test_validated(self):
        from src.mcp.autopilot._shared import _feature_status

        status = _feature_status({"product_validated": True})
        assert status == "validated"

    def test_failed(self):
        from src.mcp.autopilot._shared import _feature_status

        status = _feature_status({"stop_reason": "hard_error"})
        assert status == "failed"

    def test_impasse(self):
        from src.mcp.autopilot._shared import _feature_status

        status = _feature_status({"stop_reason": "impasse"})
        assert status == "failed"

    def test_needs_review(self):
        from src.mcp.autopilot._shared import _feature_status

        status = _feature_status({"tests_passed": 5, "tests_failed": 2})
        assert status == "needs_review"


# ── _read_json / _read_jsonl_tail ─────────────────────────────────


class TestReadJson:
    def test_read_valid_json(self, tmp_path):
        from src.mcp.autopilot._shared import _read_json

        p = tmp_path / "test.json"
        p.write_text('{"key": "value"}')
        result = _read_json(p)
        assert result == {"key": "value"}

    def test_read_missing_file(self):
        from src.mcp.autopilot._shared import _read_json

        result = _read_json(Path("/nonexistent/file.json"))
        assert result is None

    def test_read_invalid_json(self, tmp_path):
        from src.mcp.autopilot._shared import _read_json

        p = tmp_path / "bad.json"
        p.write_text("not json")
        result = _read_json(p)
        assert result is None

    def test_read_jsonl_tail(self, tmp_path):
        from src.mcp.autopilot._shared import _read_jsonl_tail

        p = tmp_path / "log.jsonl"
        lines = [json.dumps({"i": i}) for i in range(10)]
        p.write_text("\n".join(lines))
        result = _read_jsonl_tail(p, limit=3)
        assert len(result) == 3
        assert result[0]["i"] == 7

    def test_read_jsonl_missing(self):
        from src.mcp.autopilot._shared import _read_jsonl_tail

        result = _read_jsonl_tail(Path("/nonexistent/log.jsonl"), limit=5)
        assert result == []


# ── _load_queue_order / _save_queue_order ─────────────────────────


class TestQueueOrder:
    def test_save_and_load(self, tmp_path):
        from src.mcp.autopilot.queue_routes import _load_queue_order, _save_queue_order

        order_file = tmp_path / "order.json"
        with patch(
            "src.mcp.autopilot.queue_routes._get_queue_order_path", return_value=order_file
        ):
            _save_queue_order(["a.md", "b.md", "c.md"])
            result = _load_queue_order()
            assert result == ["a.md", "b.md", "c.md"]

    def test_load_missing(self):
        from src.mcp.autopilot.queue_routes import _load_queue_order

        with patch("src.mcp.autopilot.queue_routes._get_queue_order_path", return_value=None):
            result = _load_queue_order()
            assert result == []


# ── POST /start concurrency cap ──────────────────────────────────


class TestStartPipelineConcurrencyCap:
    """POST /start used to have no project_id or concurrency cap at all --
    a genuinely new project starting while others were already running
    could pile up unboundedly. Exercises the actual start_pipeline route
    body directly (its DB/service dependencies are all mocked out) rather
    than the FastAPI TestClient, since a real /start call also needs a
    real git-repo project directory and worktree machinery unrelated to
    what this is regression-testing."""

    @pytest.mark.asyncio
    async def test_rejects_when_over_concurrency_cap(self):
        from fastapi import HTTPException

        from src.mcp.autopilot.control_routes import start_pipeline

        fake_registry = Mock()
        fake_registry.try_reserve.return_value = (
            False,
            "Max concurrent projects (2) reached: proj-a, proj-b. "
            "Stop one before starting another.",
        )

        with patch(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            return_value="proj-c",
        ), patch(
            "src.autopilot.service.get_registry", return_value=fake_registry
        ):
            with pytest.raises(HTTPException) as exc_info:
                await start_pipeline("/some/new/project")

        assert exc_info.value.status_code == 409
        assert "proj-a" in exc_info.value.detail
        assert "proj-b" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_allows_restart_of_currently_running_project(self):
        from src.mcp.autopilot.control_routes import start_pipeline

        fake_registry = Mock()
        # try_reserve() never treats a project already occupying a slot as a
        # new one -- restarting it is always allowed.
        fake_registry.try_reserve.return_value = (True, "")

        fake_service = Mock()
        fake_service.running = False
        fake_service._start_time = None
        fake_service.start = AsyncMock(return_value={"started": True})

        with patch(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            return_value="proj-a",
        ), patch(
            "src.autopilot.service.get_registry", return_value=fake_registry
        ), patch(
            "src.autopilot.service.get_autopilot_service", return_value=fake_service
        ), patch(
            "src.mcp.autopilot.control_routes._invalidate"
        ):
            result = await start_pipeline("/some/already/running/project")

        assert result == {"started": True}
        fake_service.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_zombie_check_db_failure_fails_conservative_not_destructive(self):
        """Regression (SOLID review Theme B, 2026-08-20): a transient DB
        error inside the zombie-detection query itself used to be treated
        the same as a CONFIRMED zombie, falling through to
        `await service.stop()` -- unconditionally killing a pipeline that,
        per service.running, is otherwise believed healthy and actively
        running. It must now fail conservative instead, matching the
        non-zombie branch's own behavior: raise 409 "already running" and
        leave the service alone."""
        from fastapi import HTTPException
        from sqlalchemy.exc import OperationalError

        import src.core.database as db_module
        from src.mcp.autopilot.control_routes import start_pipeline

        fake_registry = Mock()
        fake_registry.try_reserve.return_value = (True, "")

        fake_service = Mock()
        fake_service.running = True
        fake_service._start_time = time.time() - 999  # past the 45s grace period
        fake_service.stop = AsyncMock()

        def _raise(*a, **kw):
            raise OperationalError("SELECT ...", {}, Exception("database is locked"))

        with patch(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            return_value="proj-a",
        ), patch(
            "src.autopilot.service.get_registry", return_value=fake_registry
        ), patch(
            "src.autopilot.service.get_autopilot_service", return_value=fake_service
        ), patch.object(db_module, "get_db", _raise):
            with pytest.raises(HTTPException) as exc_info:
                await start_pipeline("/some/running/project")

        assert exc_info.value.status_code == 409
        fake_service.stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_archived_design_does_not_trigger_zombie_warning(self, db_manager):
        """pending_designs (the zombie-vs-"all designs done" decision)
        filtered only on status, not archived_at -- an archived design
        (archived_at set, status left at "pending" by design_file_routes.py's
        archive toggle) was still counted, so a queue whose only design has
        been archived logged a misleading "Zombie pipeline detected"
        warning instead of the correct "all designs are done" info line.
        Mirrors queue.py's pending_designs/active_designs, which already
        pair status with archived_at=None."""
        import datetime

        from src.core.database import AutopilotDesign, AutopilotProject
        from src.mcp.autopilot.control_routes import start_pipeline

        session = db_manager.get_session()
        try:
            session.add(
                AutopilotProject(
                    id="proj-zombie-archived",
                    name="Zombie Archived Test",
                    base_dir="/tmp/proj-zombie-archived",
                    is_active=True,
                )
            )
            session.add(
                AutopilotDesign(
                    id="des-archived-zombie",
                    project_id="proj-zombie-archived",
                    filename="d.md",
                    name="D",
                    status="pending",
                    archived_at=datetime.datetime.utcnow(),
                )
            )
            session.commit()
        finally:
            session.close()

        fake_registry = Mock()
        fake_registry.try_reserve.return_value = (True, "")

        fake_service = Mock()
        fake_service.running = True
        fake_service._start_time = time.time() - 999  # past the 45s grace period
        fake_service.stop = AsyncMock()
        fake_service.start = AsyncMock(return_value={"started": True})

        with patch(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            return_value="proj-zombie-archived",
        ), patch(
            "src.autopilot.service.get_registry", return_value=fake_registry
        ), patch(
            "src.autopilot.service.get_autopilot_service", return_value=fake_service
        ), patch(
            "src.mcp.autopilot.control_routes._invalidate"
        ), patch(
            "src.mcp.autopilot.control_routes.logger"
        ) as mock_logger:
            result = await start_pipeline("/some/proj-zombie-archived")

        assert result == {"started": True}
        mock_logger.warning.assert_not_called()
        assert any("all designs are done" in str(c) for c in mock_logger.info.call_args_list)
