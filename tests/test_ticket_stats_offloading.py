"""Regression: get_ticket_stats_endpoint ran ~10 sequential blocking DB
round-trips (several group-by counts plus a full ticket-list scan) inline
inside async def with no offload -- each one blocked the event loop for
the whole request."""

from unittest.mock import patch

import pytest

from src.mcp.tickets_api import get_ticket_stats_endpoint
from src.services.ticket_service import TicketService


@pytest.mark.asyncio
async def test_get_ticket_stats_offloads_to_thread():
    with patch("asyncio.to_thread") as mock_to_thread:
        mock_to_thread.return_value = {"success": True, "stats": {}}
        result = await get_ticket_stats_endpoint(
            workflow_id="wf-1", project_id=None, agent_id="agent-1"
        )

    assert result == {"success": True, "stats": {}}
    mock_to_thread.assert_called_once()
    from src.mcp.tickets_api import _compute_ticket_stats

    func_arg, wf_arg, proj_arg = mock_to_thread.call_args.args
    assert func_arg == _compute_ticket_stats
    assert wf_arg == "wf-1"
    assert proj_arg is None


@pytest.mark.asyncio
async def test_get_tickets_by_workflow_offloads_to_thread():
    """Regression: get_tickets_endpoint loops get_tickets_by_workflow once
    per workflow for a project-wide fetch -- each iteration ran its own
    blocking DB query directly on the event loop with no offload."""
    with patch("asyncio.to_thread") as mock_to_thread:
        mock_to_thread.return_value = [{"id": "ticket-1"}]
        result = await TicketService.get_tickets_by_workflow(
            "wf-1", filters={"status": "backlog"}
        )

    assert result == [{"id": "ticket-1"}]
    mock_to_thread.assert_called_once_with(
        TicketService._get_tickets_by_workflow_sync, "wf-1", {"status": "backlog"}
    )
