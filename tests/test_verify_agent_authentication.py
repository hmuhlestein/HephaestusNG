"""Tests for verify_agent_authentication's retry-on-transient-miss behavior.

Regression coverage for a real bug found during smoke testing: agent
creation (create_agent_for_task_direct) runs in a background thread via
asyncio.run(), separate from the request-handling thread, sharing the same
StaticPool SQLite connection as the rest of the server. Under load, a
freshly-committed Agent row was occasionally not yet visible to a query
landing on this thread microseconds later — logged as "Rejected unknown
agent" for an agent whose row demonstrably existed seconds afterward. This
permanently blocked that agent from ever completing its task (every
hephaestus_update_task_status call kept failing with 401), so the pipeline
retried the whole phase forever.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp.server import verify_agent_authentication


class _FakeAgent:
    def __init__(self, status):
        self.status = status


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self._result


class _FakeSession:
    def __init__(self, results):
        # results: list of return values, one per call to .query(...).first()
        self._results = list(results)
        self.calls = 0

    def query(self, model):
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return _FakeQuery(result)

    def close(self):
        pass


class TestVerifyAgentAuthentication:
    @pytest.mark.asyncio
    async def test_known_system_agent_trusted_immediately(self):
        from src.mcp.server import KNOWN_SYSTEM_AGENTS

        any_known = next(iter(KNOWN_SYSTEM_AGENTS))
        assert await verify_agent_authentication(any_known) is True

    @pytest.mark.asyncio
    async def test_sdk_prefixed_agent_trusted_immediately(self):
        assert await verify_agent_authentication("sdk-something") is True
        assert await verify_agent_authentication("mcp-something") is True

    @pytest.mark.asyncio
    async def test_active_agent_found_on_first_try(self):
        fake_session = _FakeSession([_FakeAgent("working")])
        with patch("src.mcp.server.server_state") as mock_state:
            mock_state.db_manager.get_session.return_value = fake_session
            result = await verify_agent_authentication("agent-1")
        assert result is True
        assert fake_session.calls == 1

    @pytest.mark.asyncio
    async def test_terminated_agent_rejected_without_retry(self):
        """A terminated agent is a confirmed rejection, not a visibility
        race — must not retry (would just waste 0.3s for no benefit)."""
        fake_session = _FakeSession([_FakeAgent("terminated")])
        with patch("src.mcp.server.server_state") as mock_state:
            mock_state.db_manager.get_session.return_value = fake_session
            result = await verify_agent_authentication("agent-2")
        assert result is False
        assert fake_session.calls == 1

    @pytest.mark.asyncio
    async def test_transient_miss_retries_and_succeeds(self):
        """First lookup finds nothing (agent row not visible yet), second
        lookup (after the retry sleep) finds it active -> must succeed,
        not reject as permanently unknown."""
        fake_session = _FakeSession([None, _FakeAgent("working")])
        with patch("src.mcp.server.server_state") as mock_state, patch(
            "asyncio.sleep", new_callable=AsyncMock
        ):
            mock_state.db_manager.get_session.return_value = fake_session
            result = await verify_agent_authentication("agent-3")
        assert result is True
        assert fake_session.calls == 2

    @pytest.mark.asyncio
    async def test_genuinely_unknown_agent_rejected_after_retry(self):
        fake_session = _FakeSession([None, None])
        with patch("src.mcp.server.server_state") as mock_state:
            mock_state.db_manager.get_session.return_value = fake_session
            result = await verify_agent_authentication("agent-4")
        assert result is False
        assert fake_session.calls == 2
