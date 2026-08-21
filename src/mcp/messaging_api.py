"""Messaging API routes — broadcast, direct message.

Extracted from server.py for better modularity.
"""

import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src.core.app_context import get_app_state

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
