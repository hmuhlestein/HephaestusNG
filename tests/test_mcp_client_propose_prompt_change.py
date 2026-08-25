"""Regression coverage for mcp/mcp_client.py's propose_prompt_change tool.

propose_prompt_change was added to _mcp_tool_registry.py (the backend's own
tool dispatch table) and to forensics_analysis's prompt instructions, but
was never added to mcp/mcp_client.py -- the hand-maintained file that
actually exposes tools to CLI agents via the real MCP stdio protocol.
Multiple forensics_analysis runs independently reported this gap in their
own memory notes ("heph_propose_prompt_change tool not available"), and
zero rows were ever written to the prompt_proposals table despite
forensics reports repeatedly drafting rewrites in their "Prompt Rewrites"
section. This file guards against the tool silently disappearing again.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _ensure_real_mcp_sdk_importable() -> None:
    """See test_mcp_client_retry.py's identical helper for why this is
    needed -- duplicated rather than imported to keep each test module
    independently runnable regardless of collection order."""
    for name in list(sys.modules):
        if name == "mcp" or name.startswith("mcp."):
            mod = sys.modules[name]
            origin = getattr(mod, "__file__", "") or ""
            if "site-packages" not in origin:
                del sys.modules[name]

    project_src = str(Path(__file__).resolve().parents[1] / "src")
    sys.path[:] = [p for p in sys.path if p != project_src]


_ensure_real_mcp_sdk_importable()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import mcp_client  # noqa: E402


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient: each call to post() consumes the
    next scripted response/exception, in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.last_call = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        self.last_call = (args, kwargs)
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


def _patch_client(monkeypatch, fake_client):
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", lambda: fake_client)


class TestProposePromptChange:
    @pytest.mark.asyncio
    async def test_files_proposal_and_posts_to_the_prompt_proposals_endpoint(
        self, monkeypatch
    ):
        fake_client = FakeAsyncClient(
            [_response(200, {"success": True, "proposal": {"id": "prop-abc123"}})]
        )
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client.propose_prompt_change(
            phase_name="architecture_design",
            field="additional_notes",
            proposed_value="new value",
            rationale="citing what went wrong in this run",
            current_value="old value",
            evidence="quoted log line",
            workflow_id="wf-1",
            proposing_phase="forensics_analysis",
            agent_id="agent-1",
        )

        assert "prop-abc123" in result
        assert "✅" in result
        assert fake_client.calls == 1
        args, kwargs = fake_client.last_call
        assert args[0].endswith("/api/autopilot/prompt_proposals")
        assert kwargs["json"]["phase_name"] == "architecture_design"
        assert kwargs["json"]["field"] == "additional_notes"
        assert kwargs["json"]["proposed_value"] == "new value"
        assert kwargs["json"]["quoted_current_value"] == "old value"

    @pytest.mark.asyncio
    async def test_rejection_response_is_surfaced_not_swallowed(self, monkeypatch):
        """A 400 (e.g. an off-limits field, or proposing on your own phase)
        must come back as a clear failure the agent can note in its report
        -- not raise, and not look like success."""
        fake_client = FakeAsyncClient(
            [_response(400, text="Field 'spec_gate' is not editable")]
        )
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client.propose_prompt_change(
            phase_name="qa_validation",
            field="spec_gate",
            proposed_value="x",
            rationale="y",
            workflow_id="wf-1",
            agent_id="agent-1",
        )

        assert "❌" in result
        assert "not editable" in result

    @pytest.mark.asyncio
    async def test_connection_failure_returns_an_error_string_not_an_exception(
        self, monkeypatch
    ):
        import httpx

        fake_client = FakeAsyncClient([httpx.ConnectError("refused")])
        _patch_client(monkeypatch, fake_client)

        result = await mcp_client.propose_prompt_change(
            phase_name="architecture_design",
            field="additional_notes",
            proposed_value="x",
            rationale="y",
            workflow_id="wf-1",
            agent_id="agent-1",
        )

        assert "❌" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_environment_for_workflow_and_agent_id(
        self, monkeypatch
    ):
        monkeypatch.setenv("HEPHAESTUS_AGENT_ID", "env-agent")
        monkeypatch.setenv("HEPHAESTUS_WORKFLOW_ID", "env-workflow")
        fake_client = FakeAsyncClient(
            [_response(200, {"success": True, "proposal": {"id": "prop-1"}})]
        )
        _patch_client(monkeypatch, fake_client)

        await mcp_client.propose_prompt_change(
            phase_name="architecture_design",
            field="additional_notes",
            proposed_value="x",
            rationale="y",
        )

        _, kwargs = fake_client.last_call
        assert kwargs["json"]["created_by_agent_id"] == "env-agent"
        assert kwargs["json"]["workflow_id"] == "env-workflow"
