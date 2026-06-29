"""Tests for autopilot_api.py — pure helpers and cache logic."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ── _safe_path ────────────────────────────────────────────────────


class TestSafePath:
    def test_safe_path_basic(self):
        from src.mcp.autopilot_api import _safe_path

        result = _safe_path("/base", "subdir", "file.txt")
        assert result == Path("/base/subdir/file.txt")

    def test_safe_path_traversal_rejected(self):
        from fastapi import HTTPException

        from src.mcp.autopilot_api import _safe_path

        with pytest.raises(HTTPException):
            _safe_path("/base", "../etc/passwd")

    def test_safe_path_single_part(self):
        from src.mcp.autopilot_api import _safe_path

        result = _safe_path("/base", "file.txt")
        assert result == Path("/base/file.txt")


# ── _cache / _store / _invalidate ─────────────────────────────────


class TestCache:
    def test_store_and_get(self):
        from src.mcp.autopilot_api import _cached, _invalidate, _store

        _store("test_key", {"data": "hello"})
        result = _cached("test_key", ttl=10)
        assert result == {"data": "hello"}
        _invalidate("test_key")
        assert _cached("test_key", ttl=10) is None

    def test_cache_expires(self):
        from src.mcp.autopilot_api import _cached, _invalidate, _store

        _store("expire_key", {"data": "old"})
        result = _cached("expire_key", ttl=0.001)
        assert result is not None
        time.sleep(0.01)
        result = _cached("expire_key", ttl=0.001)
        assert result is None
        _invalidate("expire_key")

    def test_invalidate_multiple(self):
        from src.mcp.autopilot_api import _cached, _invalidate, _store

        _store("k1", "v1")
        _store("k2", "v2")
        _invalidate("k1", "k2")
        assert _cached("k1") is None
        assert _cached("k2") is None


# ── _design_id ────────────────────────────────────────────────────


class TestDesignId:
    def test_deterministic(self):
        from src.mcp.autopilot_api import _design_id

        id1 = _design_id("proj-1", "feature.md")
        id2 = _design_id("proj-1", "feature.md")
        assert id1 == id2

    def test_different_inputs(self):
        from src.mcp.autopilot_api import _design_id

        id1 = _design_id("proj-1", "feature.md")
        id2 = _design_id("proj-1", "bugfix.md")
        assert id1 != id2

    def test_format(self):
        from src.mcp.autopilot_api import _design_id

        result = _design_id("proj-1", "feature.md")
        assert isinstance(result, str)
        assert len(result) > 0


# ── _feature_status ───────────────────────────────────────────────


class TestFeatureStatus:
    def test_validated(self):
        from src.mcp.autopilot_api import _feature_status

        status = _feature_status({"product_validated": True})
        assert status == "validated"

    def test_failed(self):
        from src.mcp.autopilot_api import _feature_status

        status = _feature_status({"stop_reason": "hard_error"})
        assert status == "failed"

    def test_impasse(self):
        from src.mcp.autopilot_api import _feature_status

        status = _feature_status({"stop_reason": "impasse"})
        assert status == "failed"

    def test_needs_review(self):
        from src.mcp.autopilot_api import _feature_status

        status = _feature_status({"tests_passed": 5, "tests_failed": 2})
        assert status == "needs_review"


# ── _read_json / _read_jsonl_tail ─────────────────────────────────


class TestReadJson:
    def test_read_valid_json(self, tmp_path):
        from src.mcp.autopilot_api import _read_json

        p = tmp_path / "test.json"
        p.write_text('{"key": "value"}')
        result = _read_json(p)
        assert result == {"key": "value"}

    def test_read_missing_file(self):
        from src.mcp.autopilot_api import _read_json

        result = _read_json(Path("/nonexistent/file.json"))
        assert result is None

    def test_read_invalid_json(self, tmp_path):
        from src.mcp.autopilot_api import _read_json

        p = tmp_path / "bad.json"
        p.write_text("not json")
        result = _read_json(p)
        assert result is None

    def test_read_jsonl_tail(self, tmp_path):
        from src.mcp.autopilot_api import _read_jsonl_tail

        p = tmp_path / "log.jsonl"
        lines = [json.dumps({"i": i}) for i in range(10)]
        p.write_text("\n".join(lines))
        result = _read_jsonl_tail(p, limit=3)
        assert len(result) == 3
        assert result[0]["i"] == 7

    def test_read_jsonl_missing(self):
        from src.mcp.autopilot_api import _read_jsonl_tail

        result = _read_jsonl_tail(Path("/nonexistent/log.jsonl"), limit=5)
        assert result == []


# ── _load_queue_order / _save_queue_order ─────────────────────────


class TestQueueOrder:
    def test_save_and_load(self, tmp_path):
        from src.mcp.autopilot_api import _load_queue_order, _save_queue_order

        order_file = tmp_path / "order.json"
        with patch(
            "src.mcp.autopilot_api._get_queue_order_path", return_value=order_file
        ):
            _save_queue_order(["a.md", "b.md", "c.md"])
            result = _load_queue_order()
            assert result == ["a.md", "b.md", "c.md"]

    def test_load_missing(self):
        from src.mcp.autopilot_api import _load_queue_order

        with patch("src.mcp.autopilot_api._get_queue_order_path", return_value=None):
            result = _load_queue_order()
            assert result == []
