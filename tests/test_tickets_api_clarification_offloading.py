"""Regression: request_ticket_clarification_endpoint ran three sequential
blocking DB queries (ticket lookup, 60 recent tickets, 60 recent tasks)
directly inside async def with no offload."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.mcp.messaging_api import (
    RequestTicketClarificationRequest,
    _gather_clarification_context,
    request_ticket_clarification_endpoint,
)


@pytest.mark.asyncio
async def test_request_clarification_offloads_context_gathering(monkeypatch):
    fake_state = Mock()
    fake_state.llm_provider.resolve_ticket_clarification = AsyncMock(
        return_value="clarification text"
    )
    fake_state.broadcast_update = AsyncMock()
    monkeypatch.setattr(
        "src.mcp.messaging_api._get_server_state", lambda: fake_state
    )
    monkeypatch.setattr(
        "src.core.database.resolve_project_for_workflow",
        lambda wf_id: (None, None),
    )

    from src.services.ticket_service import TicketService

    monkeypatch.setattr(
        TicketService,
        "add_comment",
        AsyncMock(return_value={"comment_id": "comment-1"}),
    )

    with patch("asyncio.to_thread") as mock_to_thread:
        mock_to_thread.return_value = (
            "wf-1",
            {"ticket_id": "ticket-1"},
            [],
            [],
        )
        request = RequestTicketClarificationRequest(
            ticket_id="ticket-1",
            conflict_description="conflicting requirements need arbitration",
        )
        response = await request_ticket_clarification_endpoint(
            request=request, agent_id="agent-1"
        )

    assert response.success is True
    mock_to_thread.assert_called_once_with(
        _gather_clarification_context, "ticket-1"
    )
