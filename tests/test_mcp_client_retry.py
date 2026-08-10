"""Tests for mcp/mcp_client.py's _post_task_status retry resilience.

Regression context: an agent's complete_my_task call rendered as sent in
its own tmux transcript, but the HTTP POST never reached the backend at
all (a `heph restart` landed mid-call) -- the task sat "in_progress"
forever with the agent given no indication anything had failed. This
module is imported directly (not as `mcp.mcp_client`) to avoid colliding
with the installed `mcp` SDK package, which shares the top-level name with
this project's own `mcp/` directory.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import mcp_client  # noqa: E402


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient: each call to post() consumes the
    next scripted response/exception, in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        result = self._responses[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def _response(status_code: int, json_body: dict = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or str(json_body or "")
    resp.json.return_value = json_body or {}
    return resp


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Every test exercises the retry path -- never actually wait."""
    monkeypatch.setattr(mcp_client.asyncio, "sleep", AsyncMock())


def _patch_client(monkeypatch, fake_client):
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", lambda: fake_client)


class TestPostTaskStatusRetry:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt_no_retry(self, monkeypatch):
        fake_client = FakeAsyncClient(
            [_response(200, {"success": True, "message": "Task done successfully"})]
        )
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client._post_task_status(
            "t1", "a1", "done", "did the thing", "", []
        )

        assert "Task done successfully" in result
        assert fake_client.calls == 1

    @pytest.mark.asyncio
    async def test_retries_on_connection_error_then_succeeds(self, monkeypatch):
        fake_client = FakeAsyncClient(
            [
                httpx.ConnectError("refused"),
                httpx.ConnectError("refused"),
                _response(200, {"success": True, "message": "Task done successfully"}),
            ]
        )
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client._post_task_status(
            "t1", "a1", "done", "did the thing", "", []
        )

        assert "Task done successfully" in result
        assert fake_client.calls == 3

    @pytest.mark.asyncio
    async def test_retries_on_timeout_then_succeeds(self, monkeypatch):
        fake_client = FakeAsyncClient(
            [
                httpx.ConnectTimeout("timed out"),
                _response(200, {"success": True, "message": "Task done successfully"}),
            ]
        )
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client._post_task_status(
            "t1", "a1", "done", "did the thing", "", []
        )

        assert "Task done successfully" in result
        assert fake_client.calls == 2

    @pytest.mark.asyncio
    async def test_retries_on_5xx_then_succeeds(self, monkeypatch):
        fake_client = FakeAsyncClient(
            [
                _response(503, text="Service Unavailable"),
                _response(200, {"success": True, "message": "Task done successfully"}),
            ]
        )
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client._post_task_status(
            "t1", "a1", "done", "did the thing", "", []
        )

        assert "Task done successfully" in result
        assert fake_client.calls == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts_on_persistent_connection_error(
        self, monkeypatch
    ):
        fake_client = FakeAsyncClient(
            [httpx.ConnectError("refused")] * mcp_client._STATUS_POST_MAX_ATTEMPTS
        )
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client._post_task_status(
            "t1", "a1", "done", "did the thing", "", []
        )

        assert "❌" in result
        assert fake_client.calls == mcp_client._STATUS_POST_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_does_not_retry_on_4xx(self, monkeypatch):
        """A 4xx (bad task_id, unauthenticated, output-artifact rejection,
        etc.) is a real problem -- retrying can't fix it, so it must
        return immediately without burning through the retry budget."""
        fake_client = FakeAsyncClient([_response(404, text="Task not found")])
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client._post_task_status(
            "t1", "a1", "done", "did the thing", "", []
        )

        assert "❌" in result
        assert "Task not found" in result
        assert fake_client.calls == 1

    @pytest.mark.asyncio
    async def test_requires_summary_for_done_status_before_any_network_call(
        self, monkeypatch
    ):
        fake_client = FakeAsyncClient([])
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client._post_task_status("t1", "a1", "done", "   ", "", [])

        assert "summary is required" in result
        assert fake_client.calls == 0
