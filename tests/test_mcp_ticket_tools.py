"""Regression: the search_tickets and update_ticket_status MCP tools were dead.

Both are advertised to agents in MCP_TOOL_REGISTRY, and neither could ever
have run:

- _tool_search_tickets did `TicketSearchService(session)`, but that class has
  no __init__ and only static methods, so it raised
  `TypeError: TicketSearchService() takes no arguments` on its first line --
  before reaching `search_tickets`, a method that does not exist either.
- _tool_update_ticket_status called `TicketService.change_ticket_status`,
  which does not exist (the method is `change_status`) and which additionally
  requires a `comment` the tool never collected.

Found by mypy's [attr-defined] category once c38f143 unblocked it, then
confirmed by construction. These tests pin the tools against the real service
signatures so a rename on either side fails here rather than at an agent's
call site.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.mcp.server._mcp_tool_registry import (
    MCP_TOOL_REGISTRY,
    _tool_search_tickets,
    _tool_update_ticket_status,
)


def _spec(name):
    return next(s for s in MCP_TOOL_REGISTRY if s.name == name)


class TestAdvertisedSchemaMatchesTheService:
    """The schema is the contract agents code against; if it omits an
    argument the service requires, every call fails at runtime."""

    def test_search_requires_workflow_id(self):
        schema = _spec("search_tickets").input_schema
        assert "workflow_id" in schema["properties"]
        assert "workflow_id" in schema["required"]

    def test_update_status_requires_comment(self):
        schema = _spec("update_ticket_status").input_schema
        assert "comment" in schema["properties"]
        assert "comment" in schema["required"]


class TestSearchTickets:
    @pytest.mark.asyncio
    async def test_calls_hybrid_search_statically_with_the_workflow(self):
        with patch(
            "src.mcp.server._mcp_tool_registry.TicketSearchService.hybrid_search",
            new_callable=AsyncMock,
        ) as hybrid:
            hybrid.return_value = [{"id": "t1"}]
            result = await _tool_search_tickets(
                {"query": "login bug", "workflow_id": "wf-1"}
            )

        assert result == {"tickets": [{"id": "t1"}]}
        assert hybrid.await_args.kwargs["query"] == "login bug"
        assert hybrid.await_args.kwargs["workflow_id"] == "wf-1"

    @pytest.mark.asyncio
    async def test_missing_workflow_id_is_rejected(self):
        """Matches the sibling tools in this registry, which 400 rather than
        silently searching the wrong scope."""
        with pytest.raises(HTTPException) as exc:
            await _tool_search_tickets({"query": "anything"})
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_status_becomes_a_filter(self):
        with patch(
            "src.mcp.server._mcp_tool_registry.TicketSearchService.hybrid_search",
            new_callable=AsyncMock,
        ) as hybrid:
            hybrid.return_value = []
            await _tool_search_tickets(
                {"query": "q", "workflow_id": "wf-1", "status": "open"}
            )

        assert hybrid.await_args.kwargs["filters"] == {"status": "open"}

    @pytest.mark.asyncio
    async def test_tags_are_folded_into_the_query(self):
        """tags is not a supported filter key (only status/priority/
        ticket_type are), but _ticket_text indexes tags into the searchable
        document -- so the query is where they can actually match."""
        with patch(
            "src.mcp.server._mcp_tool_registry.TicketSearchService.hybrid_search",
            new_callable=AsyncMock,
        ) as hybrid:
            hybrid.return_value = []
            await _tool_search_tickets(
                {"query": "crash", "workflow_id": "wf-1", "tags": ["auth", "p1"]}
            )

        assert hybrid.await_args.kwargs["query"] == "crash auth p1"
        assert hybrid.await_args.kwargs["filters"] is None


class TestUpdateTicketStatus:
    @pytest.mark.asyncio
    async def test_calls_change_status_with_every_required_argument(self):
        with patch(
            "src.mcp.server._mcp_tool_registry.TicketService.change_status",
            new_callable=AsyncMock,
        ) as change:
            change.return_value = {"ok": True}
            result = await _tool_update_ticket_status(
                {
                    "ticket_id": "tk-1",
                    "new_status": "in_progress",
                    "comment": "picking this up",
                    "agent_id": "agent-7",
                }
            )

        assert result["success"] is True
        kwargs = change.await_args.kwargs
        assert kwargs["ticket_id"] == "tk-1"
        assert kwargs["new_status"] == "in_progress"
        assert kwargs["comment"] == "picking this up"
        assert kwargs["agent_id"] == "agent-7"

    @pytest.mark.asyncio
    async def test_agent_id_defaults_when_absent(self):
        with patch(
            "src.mcp.server._mcp_tool_registry.TicketService.change_status",
            new_callable=AsyncMock,
        ) as change:
            change.return_value = {}
            await _tool_update_ticket_status(
                {"ticket_id": "tk-1", "new_status": "done", "comment": "finished"}
            )

        assert change.await_args.kwargs["agent_id"] == "mcp-claude"

    @pytest.mark.asyncio
    async def test_missing_comment_is_rejected(self):
        """change_status requires a comment; without this guard the tool
        would fail deep inside the service with a TypeError instead."""
        with pytest.raises(HTTPException) as exc:
            await _tool_update_ticket_status(
                {"ticket_id": "tk-1", "new_status": "done"}
            )
        assert exc.value.status_code == 400


class TestSignaturesStillLineUp:
    """Guards the rename that caused this: assert the methods the handlers
    call actually exist on the services, by name."""

    def test_service_methods_exist(self):
        from src.services.ticket_search_service import TicketSearchService
        from src.services.ticket_service import TicketService

        assert callable(getattr(TicketSearchService, "hybrid_search", None))
        assert callable(getattr(TicketService, "change_status", None))

    def test_change_status_accepts_the_arguments_the_tool_sends(self):
        import inspect

        from src.services.ticket_service import TicketService

        params = inspect.signature(TicketService.change_status).parameters
        for required in ("ticket_id", "agent_id", "new_status", "comment"):
            assert required in params, required

    def test_hybrid_search_accepts_the_arguments_the_tool_sends(self):
        import inspect

        from src.services.ticket_search_service import TicketSearchService

        params = inspect.signature(TicketSearchService.hybrid_search).parameters
        for required in ("query", "workflow_id", "limit", "filters"):
            assert required in params, required


def test_the_old_construction_would_still_fail(_unused=None):
    """Pins why the original code could never have worked: the class takes no
    constructor arguments, so `TicketSearchService(session)` is a TypeError
    regardless of which method is called afterwards."""
    from src.services.ticket_search_service import TicketSearchService

    with pytest.raises(TypeError):
        TicketSearchService(MagicMock())
