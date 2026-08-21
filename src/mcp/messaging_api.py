"""Messaging API routes — broadcast, direct message, ticket clarification.

Extracted from server.py for better modularity.
"""

import logging
from typing import List

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src.core.app_context import get_app_state
from src.core.database import Task, Ticket, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["messaging"])


def _get_server_state():
    """Get server state (lazy import to avoid circular deps)."""
    return get_app_state()


# ── Request / Response Models ─────────────────────────────────────────


class BroadcastMessageRequest(BaseModel):
    """Request model for broadcasting a message to all agents."""

    message: str = Field(..., description="Message content to broadcast")


class BroadcastMessageResponse(BaseModel):
    """Response model for message broadcast."""

    success: bool = Field(..., description="Whether broadcast was successful")
    recipient_count: int = Field(
        ..., description="Number of agents message was sent to"
    )
    message: str = Field(..., description="Status message")


class SendMessageRequest(BaseModel):
    """Request model for sending a direct message to an agent."""

    recipient_agent_id: str = Field(
        ..., description="ID of the agent to send message to"
    )
    message: str = Field(..., description="Message content")


class SendMessageResponse(BaseModel):
    """Response model for direct message."""

    success: bool = Field(..., description="Whether message was sent successfully")
    message: str = Field(..., description="Status message")


class RequestTicketClarificationRequest(BaseModel):
    """Request model for ticket clarification."""

    ticket_id: str = Field(..., description="ID of the ticket needing clarification")
    conflict_description: str = Field(
        ..., min_length=20, description="Clear description of the conflict or issue"
    )
    context: str = Field(
        default="", description="Additional context relevant to the clarification"
    )
    potential_solutions: List[str] = Field(
        default_factory=list, description="List of potential solutions being considered"
    )


class RequestTicketClarificationResponse(BaseModel):
    """Response model for ticket clarification."""

    success: bool
    ticket_id: str
    clarification: str  # Markdown-formatted detailed response
    comment_id: str  # ID of the comment where clarification was stored
    message: str


# ── Routes ───────────────────────────────────────────────────────────


@router.post("/broadcast_message", response_model=BroadcastMessageResponse)
async def broadcast_message(
    request: BroadcastMessageRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Broadcast a message to all active agents except the sender."""
    server_state = _get_server_state()
    logger.info(
        f"Agent {agent_id[:8]} broadcasting message: {request.message[:100]}..."
    )

    try:
        recipient_count = (
            await server_state.agent_manager.broadcast_message_to_all_agents(
                sender_agent_id=agent_id, message=request.message
            )
        )

        await server_state.broadcast_update(
            {
                "type": "agent_broadcast",
                "sender_agent_id": agent_id,
                "recipient_count": recipient_count,
                "message_preview": request.message[:100],
            }
        )

        return BroadcastMessageResponse(
            success=True,
            recipient_count=recipient_count,
            message=f"Message broadcast to {recipient_count} agent(s)",
        )

    except Exception as e:
        logger.error(f"Failed to broadcast message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send_message", response_model=SendMessageResponse)
async def send_message(
    request: SendMessageRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Send a direct message to a specific agent."""
    server_state = _get_server_state()
    logger.info(
        f"Agent {agent_id[:8]} sending message to {request.recipient_agent_id[:8]}: "
        f"{request.message[:100]}..."
    )

    try:
        success = await server_state.agent_manager.send_direct_message(
            sender_agent_id=agent_id,
            recipient_agent_id=request.recipient_agent_id,
            message=request.message,
        )

        if not success:
            return SendMessageResponse(
                success=False,
                message=f"Failed to send message - recipient agent "
                f"{request.recipient_agent_id[:8]} may not exist or is terminated",
            )

        await server_state.broadcast_update(
            {
                "type": "agent_direct_message",
                "sender_agent_id": agent_id,
                "recipient_agent_id": request.recipient_agent_id,
                "message_preview": request.message[:100],
            }
        )

        return SendMessageResponse(
            success=True,
            message=f"Message sent to agent {request.recipient_agent_id[:8]}",
        )

    except Exception as e:
        logger.error(f"Failed to send direct message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _gather_clarification_context(ticket_id: str):
    """Sync body of request_ticket_clarification_endpoint's DB-gathering
    step -- run via asyncio.to_thread. Returns (workflow_id, ticket_details,
    tickets_context, tasks_context)."""
    with get_db() as db:
        # 1. Validate ticket exists
        ticket = db.query(Ticket).filter_by(id=ticket_id).first()
        if not ticket:
            logger.error(f"[CLARIFICATION] Ticket not found: {ticket_id}")
            raise HTTPException(
                status_code=404, detail=f"Ticket not found: {ticket_id}"
            )

        logger.info(f"[CLARIFICATION] Ticket found: {ticket.title}")

        # 2. Gather context - Latest 60 tickets
        recent_tickets = (
            db.query(Ticket).order_by(Ticket.created_at.desc()).limit(60).all()
        )
        tickets_context = [
            {
                "ticket_id": t.id,
                "title": t.title,
                "description": t.description,
                "status": t.status,
                "priority": t.priority,
                "ticket_type": t.ticket_type,
            }
            for t in recent_tickets
        ]
        logger.info(
            f"[CLARIFICATION] Gathered {len(tickets_context)} recent tickets for context"
        )

        # 3. Gather context - Latest 60 tasks
        recent_tasks = (
            db.query(Task).order_by(Task.created_at.desc()).limit(60).all()
        )
        tasks_context = [
            {
                "id": t.id,
                "description": t.description,
                "status": t.status,
                "phase_id": t.phase_id,
            }
            for t in recent_tasks
        ]

        # 4. Prepare ticket details
        ticket_details = {
            "ticket_id": ticket.id,
            "title": ticket.title,
            "description": ticket.description,
            "status": ticket.status,
            "priority": ticket.priority,
            "ticket_type": ticket.ticket_type,
            "assigned_agent_id": ticket.assigned_agent_id,
            "tags": ticket.tags or [],
        }

        return ticket.workflow_id, ticket_details, tickets_context, tasks_context


@router.post(
    "/tickets/request-clarification",
    response_model=RequestTicketClarificationResponse,
)
async def request_ticket_clarification_endpoint(
    request: RequestTicketClarificationRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """
    Request LLM-powered clarification for a ticket with conflicting/unclear requirements.

    Process:
    1. Gathers comprehensive context (ticket details, 60 recent tickets, 60 recent tasks)
    2. Calls LLM with structured reasoning prompt
    3. Returns detailed markdown guidance
    4. Stores clarification as ticket comment for audit trail
    """
    logger.info("[CLARIFICATION] ========== START ==========")
    logger.info(
        f"[CLARIFICATION] Agent {agent_id[:8]} requesting clarification for ticket "
        f"{request.ticket_id}"
    )
    logger.info(f"[CLARIFICATION] Conflict: {request.conflict_description[:100]}...")

    try:
        import asyncio

        # Offloaded -- three sequential blocking queries (ticket lookup,
        # 60 recent tickets, 60 recent tasks) ran directly on the event
        # loop otherwise, same class of issue fixed elsewhere in this
        # codebase.
        ticket_workflow_id, ticket_details, tickets_context, tasks_context = (
            await asyncio.to_thread(
                _gather_clarification_context, request.ticket_id
            )
        )

        # 5. Call LLM for clarification
        logger.info("[CLARIFICATION] Calling LLM arbitrator with full context...")
        logger.info(
            f"[CLARIFICATION] Potential solutions provided: {len(request.potential_solutions)}"
        )

        server_state = _get_server_state()
        clarification_markdown = (
            await server_state.llm_provider.resolve_ticket_clarification(
                ticket_id=request.ticket_id,
                conflict_description=request.conflict_description,
                context=request.context,
                potential_solutions=request.potential_solutions,
                ticket_details=ticket_details,
                related_tickets=tickets_context,
                active_tasks=tasks_context,
            )
        )

        logger.info(
            f"[CLARIFICATION] LLM arbitration complete, {len(clarification_markdown)} chars"
        )

        # 6. Store clarification as ticket comment
        comment_text = f"""## AUTOMATED CLARIFICATION REQUEST

**Agent**: `{agent_id}`
**Conflict Description**: {request.conflict_description}

---

{clarification_markdown}

---

*This clarification was automatically generated by the Hephaestus arbitration system.*
"""

        from src.services.ticket_service import TicketService
        comment_result = await TicketService.add_comment(
            ticket_id=request.ticket_id,
            agent_id=agent_id,
            comment_text=comment_text,
            comment_type="clarification",
            mentions=[],
            attachments=[],
        )

        logger.info(
            f"[CLARIFICATION] Stored as comment {comment_result['comment_id']}"
        )
        logger.info("[CLARIFICATION] ========== SUCCESS ==========")

        # Broadcast update
        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(
            ticket_workflow_id
        )
        await server_state.broadcast_update(
            {
                "type": "ticket_clarification_requested",
                "ticket_id": request.ticket_id,
                "agent_id": agent_id,
                "comment_id": comment_result["comment_id"],
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        return RequestTicketClarificationResponse(
            success=True,
            ticket_id=request.ticket_id,
            clarification=clarification_markdown,
            comment_id=comment_result["comment_id"],
            message="Clarification generated and stored successfully",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CLARIFICATION] Error: {e}", exc_info=True)
        logger.error("[CLARIFICATION] ========== FAILED ==========")
        raise HTTPException(
            status_code=500, detail=f"Failed to generate clarification: {str(e)}"
        )
