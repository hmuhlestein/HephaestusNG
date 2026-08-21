"""Ticket management API routes.

Extracted from server.py for better modularity (M-1 fix).
"""

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from src.core.database import get_db
from src.services.ticket_search_service import TicketSearchService
from src.services.ticket_service import TicketService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


def _get_workflow_id_for_ticket(ticket_id: str) -> Optional[str]:
    """Resolve a ticket's workflow_id for broadcast project-tagging.
    Never raises -- a lookup failure just means the broadcast goes out
    without project context, same as omitting workflow_id entirely."""
    try:
        from src.core.database import Ticket, get_db

        with get_db() as db:
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            return ticket.workflow_id if ticket else None
    except Exception:
        return None


def _resolve_repo_path_for_commit(commit_sha: str) -> Optional[str]:
    """Resolve which project's repo a commit lives in via the ticket it's
    linked to. Returns None (never raises) when the commit isn't linked to
    any ticket, or the ticket/workflow/project chain doesn't resolve --
    callers fall back to the process-wide active project in that case."""
    try:
        from src.core.database import AutopilotProject, Ticket, TicketCommit, Workflow, get_db

        with get_db() as db:
            commit = db.query(TicketCommit).filter_by(commit_sha=commit_sha).first()
            if not commit:
                return None
            ticket = db.query(Ticket).filter_by(id=commit.ticket_id).first()
            if not ticket or not ticket.workflow_id:
                return None
            wf = db.query(Workflow).filter_by(id=ticket.workflow_id).first()
            if not wf or not wf.project_id:
                return None
            proj = db.query(AutopilotProject).filter_by(id=wf.project_id).first()
            return proj.base_dir if proj else None
    except Exception:
        return None


async def _broadcast_update(data: dict, workflow_id: Optional[str] = None):
    """Broadcast update to SSE/WebSocket clients.

    workflow_id: resolved to project_id/project_name and merged into the
    payload when given, so clients can filter ticket events by their
    currently-selected project instead of seeing every project's ticket
    activity indiscriminately.
    """
    try:
        from src.core.app_context import get_app_state

        server_state = get_app_state()
        project_id, project_name = None, None
        if workflow_id:
            from src.core.database import resolve_project_for_workflow

            project_id, project_name = resolve_project_for_workflow(workflow_id)
        await server_state.broadcast_update(data, project_id=project_id, project_name=project_name)
    except Exception:
        pass  # Non-critical


# ── Pydantic Models ──────────────────────────────────────────────


class CreateTicketRequest(BaseModel):
    """Request model for creating a ticket."""

    workflow_id: str = Field(
        ..., description="ID of the workflow this ticket belongs to"
    )
    title: str = Field(
        ..., min_length=3, max_length=500, description="Short, descriptive title"
    )
    description: str = Field(..., min_length=10, description="Detailed description")
    ticket_type: str = Field(
        default="task",
        description="Type of ticket (bug, feature, improvement, task, spike)",
    )
    priority: str = Field(
        default="medium",
        pattern="^(low|medium|high|critical)$",
        description="Priority level",
    )
    initial_status: Optional[str] = Field(
        default=None,
        description="Initial status (if None, uses board_config.initial_status)",
    )
    assigned_agent_id: Optional[str] = Field(
        default=None, description="Optional agent to assign to"
    )
    agent_id: Optional[str] = Field(
        default=None, description="Agent ID creating this ticket (overrides header)"
    )
    parent_ticket_id: Optional[str] = Field(
        default=None, description="Parent ticket ID for sub-tickets"
    )
    blocked_by_ticket_ids: List[str] = Field(
        default_factory=list, description="List of ticket IDs blocking this ticket"
    )
    tags: List[str] = Field(
        default_factory=list, description="List of tags for categorization"
    )
    related_task_ids: List[str] = Field(
        default_factory=list, description="List of related task IDs"
    )
    task_id: Optional[str] = Field(
        default=None, description="Task ID this ticket relates to"
    )
    phase_id: Optional[Union[str, int]] = Field(
        default=None, description="Phase ID where this ticket was created"
    )


class CreateTicketResponse(BaseModel):
    """Response model for ticket creation."""

    success: bool
    ticket_id: str
    status: str
    message: str
    embedding_created: bool
    similar_tickets: List[Dict[str, Any]] = Field(default_factory=list)


class UpdateTicketRequest(BaseModel):
    """Request model for updating a ticket."""

    ticket_id: str = Field(..., description="ID of the ticket to update")
    updates: Dict[str, Any] = Field(..., description="Fields to update")
    update_comment: Optional[str] = Field(
        default=None, description="Optional comment explaining changes"
    )


class UpdateTicketResponse(BaseModel):
    """Response model for ticket update."""

    success: bool
    ticket_id: str
    fields_updated: List[str]
    message: str
    embedding_updated: bool


class ChangeTicketStatusRequest(BaseModel):
    """Request model for changing ticket status."""

    ticket_id: str = Field(..., description="ID of the ticket")
    new_status: str = Field(..., description="New status to move to")
    comment: str = Field(
        ..., min_length=10, description="Required comment explaining status change"
    )
    commit_sha: Optional[str] = Field(
        default=None, description="Optional commit SHA to link"
    )


class ChangeTicketStatusResponse(BaseModel):
    """Response model for status change."""

    success: bool
    ticket_id: str
    old_status: str
    new_status: str
    message: str
    blocked: bool = False
    blocking_ticket_ids: List[str] = Field(default_factory=list)


class AddCommentRequest(BaseModel):
    """Request model for adding a comment to a ticket."""

    ticket_id: str = Field(..., description="ID of the ticket")
    comment_text: str = Field(..., min_length=1, description="The comment text")
    comment_type: str = Field(
        default="general",
        description="Type of comment (general, status_change, blocker, resolution)",
    )
    mentions: List[str] = Field(
        default_factory=list, description="List of mentioned agent/ticket IDs"
    )
    attachments: List[str] = Field(
        default_factory=list, description="List of file paths"
    )


class AddCommentResponse(BaseModel):
    """Response model for adding a comment."""

    success: bool
    comment_id: str
    ticket_id: str
    message: str


class SearchTicketsRequest(BaseModel):
    """Request model for searching tickets."""

    workflow_id: str = Field(..., description="ID of the workflow to search tickets in")
    query: str = Field(..., min_length=3, description="Search query (natural language)")
    search_type: str = Field(
        default="hybrid",
        pattern="^(semantic|keyword|hybrid)$",
        description="Search type (default: hybrid)",
    )
    filters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional filters (status, priority, type, etc.)",
    )
    limit: int = Field(default=10, ge=1, le=50, description="Max number of results")
    include_comments: bool = Field(default=True, description="Search in comments too")


class TicketSearchResult(BaseModel):
    """Individual ticket search result."""

    ticket_id: str
    title: str
    description: str
    status: str
    priority: str
    ticket_type: str
    relevance_score: float
    matched_in: List[str] = Field(default_factory=list)
    preview: str
    created_at: str
    assigned_agent_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class SearchTicketsResponse(BaseModel):
    """Response model for ticket search."""

    success: bool
    query: str
    results: List[TicketSearchResult]
    total_found: int
    search_time_ms: float


class TicketStats(BaseModel):
    """Ticket statistics for a workflow."""

    total_tickets: int
    by_status: Dict[str, int] = Field(default_factory=dict)
    by_type: Dict[str, int] = Field(default_factory=dict)
    by_priority: Dict[str, int] = Field(default_factory=dict)
    by_agent: Dict[str, int] = Field(default_factory=dict)
    blocked_count: int = 0
    resolved_count: int = 0
    avg_comments_per_ticket: float = 0.0
    avg_commits_per_ticket: float = 0.0
    created_today: int = 0
    completed_today: int = 0
    velocity_last_7_days: int = 0


class TicketStatsResponse(BaseModel):
    """Response model for ticket statistics."""

    success: bool
    workflow_id: str
    stats: TicketStats
    board_config: Optional[dict] = None


class GetTicketsRequest(BaseModel):
    """Request model for getting/listing tickets."""

    workflow_id: str = Field(..., description="ID of the workflow")
    status: Optional[str] = Field(default=None, description="Filter by status")
    ticket_type: Optional[str] = Field(default=None, description="Filter by type")
    priority: Optional[str] = Field(default=None, description="Filter by priority")
    assigned_agent_id: Optional[str] = Field(
        default=None, description="Filter by assigned agent"
    )
    include_completed: bool = Field(
        default=True, description="Include completed tickets"
    )
    limit: int = Field(default=50, ge=1, le=200, description="Max number of results")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")
    sort_by: str = Field(
        default="created_at", pattern="^(created_at|updated_at|priority|status)$"
    )
    sort_order: str = Field(default="desc", pattern="^(asc|desc)$")


class TicketDetail(BaseModel):
    """Detailed ticket information."""

    id: str  # Primary ticket ID
    ticket_id: str  # Alias for backwards compatibility
    workflow_id: str
    title: str
    description: str
    ticket_type: str
    priority: str
    status: str
    approval_status: Optional[str] = "auto_approved"  # For human review workflow
    created_by_agent_id: str
    assigned_agent_id: Optional[str] = None
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    comment_count: int = 0
    commit_count: int = 0
    is_blocked: bool = False
    blocked_by_ticket_ids: List[str] = Field(default_factory=list)
    is_resolved: bool = False


class GetTicketsResponse(BaseModel):
    """Response model for get tickets."""

    success: bool
    tickets: List[TicketDetail]
    total_count: int
    has_more: bool


class ResolveTicketRequest(BaseModel):
    """Request model for resolving a ticket."""

    ticket_id: str = Field(..., description="ID of the ticket to resolve")
    resolution_comment: str = Field(
        ..., min_length=10, description="Comment explaining resolution"
    )
    commit_sha: Optional[str] = Field(
        default=None, description="Commit that resolved the ticket"
    )


class ResolveTicketResponse(BaseModel):
    """Response model for resolve ticket."""

    success: bool
    ticket_id: str
    message: str
    unblocked_tickets: List[str] = Field(default_factory=list)


class LinkCommitRequest(BaseModel):
    """Request model for linking a commit to a ticket."""

    ticket_id: str = Field(..., description="ID of the ticket")
    commit_sha: str = Field(..., description="Git commit SHA")
    commit_message: Optional[str] = Field(
        default=None, description="Commit message (auto-fetched if not provided)"
    )


class LinkCommitResponse(BaseModel):
    """Response model for link commit."""

    success: bool
    ticket_id: str
    commit_sha: str
    message: str


class PendingReviewCountResponse(BaseModel):
    """Response model for pending review count."""

    count: int
    ticket_ids: List[str]


# Workflow Management Request/Response Models


# ── Route Handlers ──────────────────────────────────────────────


@router.post("/create", response_model=CreateTicketResponse)
async def create_ticket_endpoint(
    request: CreateTicketRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Create a new ticket in the workflow tracking system."""
    # Use agent_id from request if provided, otherwise use header
    # Note: request.agent_id is the agent_id from the payload, agent_id is from X-Agent-ID header
    created_by_agent_id = request.agent_id or agent_id

    logger.info("[TICKET_CREATE] ========== START ==========")
    logger.info(f"[TICKET_CREATE] Agent: {created_by_agent_id}")
    logger.info(f"[TICKET_CREATE] Title: {request.title}")
    logger.info(
        f"[TICKET_CREATE] Type: {request.ticket_type}, Priority: {request.priority}"
    )
    logger.info(f"[TICKET_CREATE] Workflow_ID provided: {request.workflow_id}")
    logger.info(f"[TICKET_CREATE] Task_ID: {request.task_id}")
    logger.info(f"[TICKET_CREATE] Phase_ID: {request.phase_id}")
    logger.info(f"[TICKET_CREATE] Tags: {request.tags}")

    try:
        # workflow_id is now required in the request
        workflow_id = request.workflow_id
        logger.info(f"[TICKET_CREATE] Using workflow_id: {workflow_id}")

        logger.info(
            f"[TICKET_CREATE] Calling TicketService.create_ticket with workflow_id={workflow_id}"
        )
        result = await TicketService.create_ticket(
            workflow_id=workflow_id,
            agent_id=created_by_agent_id,
            title=request.title,
            description=request.description,
            ticket_type=request.ticket_type,
            priority=request.priority,
            initial_status=request.initial_status,
            assigned_agent_id=request.assigned_agent_id,
            parent_ticket_id=request.parent_ticket_id,
            blocked_by_ticket_ids=request.blocked_by_ticket_ids,
            tags=request.tags,
            related_task_ids=request.related_task_ids,
            task_id=request.task_id,
            phase_id=str(request.phase_id) if request.phase_id is not None else None,
        )

        logger.info(
            "[TICKET_CREATE] ✅ TicketService.create_ticket returned successfully"
        )
        logger.info(f"[TICKET_CREATE] Result: {result}")
        logger.info(f"[TICKET_CREATE] Ticket ID: {result.get('ticket_id')}")

        # Broadcast update
        logger.info("[TICKET_CREATE] Broadcasting update...")
        await _broadcast_update(
            {
                "type": "ticket_created",
                "ticket_id": result["ticket_id"],
                "workflow_id": workflow_id,
                "agent_id": agent_id,
                "title": request.title,
            },
            workflow_id=workflow_id,
        )
        logger.info("[TICKET_CREATE] Broadcast complete")

        logger.info("[TICKET_CREATE] Creating response object...")
        response = CreateTicketResponse(**result)
        logger.info(f"[TICKET_CREATE] Response created: {response}")
        logger.info("[TICKET_CREATE] ========== SUCCESS ==========")
        return response

    except HTTPException:
        # Re-raise HTTPException without modification to preserve status code
        raise
    except ValueError as e:
        logger.warning(f"[TICKET_CREATE] ⚠️ ValueError (non-fatal): {e}")
        # Return a warning response instead of crashing the agent. This was
        # previously constructing CreateTicketResponse with fields that
        # don't exist on that model at all (workflow_id, agent_id, title,
        # ticket_type, priority, description, created_at) while omitting
        # the three actually-required ones (success, message,
        # embedding_created) -- every ValueError path (missing board
        # config, invalid ticket_type, etc.) crashed with a pydantic
        # ValidationError instead of the intended graceful response.
        return CreateTicketResponse(
            success=False,
            ticket_id="",
            status="skipped",
            message=f"Ticket creation skipped: {e}",
            embedding_created=False,
        )
    except Exception as e:
        logger.error(f"[TICKET_CREATE] ❌ Unexpected error: {type(e).__name__}: {e}")
        logger.error("[TICKET_CREATE] ========== FAILED (Exception) ==========")
        import traceback

        logger.error(f"[TICKET_CREATE] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update", response_model=UpdateTicketResponse)
async def update_ticket_endpoint(
    request: UpdateTicketRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Update ticket fields (excluding status changes)."""
    logger.info(f"Agent {agent_id} updating ticket {request.ticket_id}")

    try:
        result = await TicketService.update_ticket(
            ticket_id=request.ticket_id,
            agent_id=agent_id,
            updates=request.updates,
            update_comment=request.update_comment,
        )

        # Broadcast update
        await _broadcast_update(
            {
                "type": "ticket_updated",
                "ticket_id": request.ticket_id,
                "agent_id": agent_id,
                "fields_updated": result["fields_updated"],
            },
            workflow_id=_get_workflow_id_for_ticket(request.ticket_id),
        )

        return UpdateTicketResponse(**result)

    except ValueError as e:
        logger.error(f"Validation error updating ticket: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update ticket: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/change-status", response_model=ChangeTicketStatusResponse)
async def change_ticket_status_endpoint(
    request: ChangeTicketStatusRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Move ticket to a different status column."""
    logger.info(
        f"Agent {agent_id} changing status of ticket {request.ticket_id} to {request.new_status}"
    )

    try:
        result = await TicketService.change_status(
            ticket_id=request.ticket_id,
            agent_id=agent_id,
            new_status=request.new_status,
            comment=request.comment,
            commit_sha=request.commit_sha,
        )

        # Broadcast update
        await _broadcast_update(
            {
                "type": "ticket_status_changed",
                "ticket_id": request.ticket_id,
                "agent_id": agent_id,
                "old_status": result["old_status"],
                "new_status": result["new_status"],
                "blocked": result["blocked"],
            },
            workflow_id=_get_workflow_id_for_ticket(request.ticket_id),
        )

        return ChangeTicketStatusResponse(**result)

    except ValueError as e:
        logger.error(f"Validation error changing ticket status: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to change ticket status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/comment", response_model=AddCommentResponse)
async def add_comment_endpoint(
    request: AddCommentRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Add a comment to a ticket."""
    logger.info(f"Agent {agent_id} adding comment to ticket {request.ticket_id}")

    try:
        result = await TicketService.add_comment(
            ticket_id=request.ticket_id,
            agent_id=agent_id,
            comment_text=request.comment_text,
            comment_type=request.comment_type,
            mentions=request.mentions,
            attachments=request.attachments,
        )

        # Broadcast update
        await _broadcast_update(
            {
                "type": "ticket_comment_added",
                "ticket_id": request.ticket_id,
                "agent_id": agent_id,
                "comment_id": result["comment_id"],
            },
            workflow_id=_get_workflow_id_for_ticket(request.ticket_id),
        )

        return AddCommentResponse(**result)

    except ValueError as e:
        logger.error(f"Validation error adding comment: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to add comment: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pending-review-count", response_model=PendingReviewCountResponse)
async def get_pending_review_count_endpoint():
    """Get count of tickets pending human review."""
    logger.info("[PENDING_REVIEW_COUNT] Fetching pending review count")

    try:
        count = TicketService.get_pending_review_count()
        ticket_ids = TicketService.get_pending_review_tickets()

        logger.info(f"[PENDING_REVIEW_COUNT] Found {count} tickets pending review")

        return PendingReviewCountResponse(
            count=count,
            ticket_ids=ticket_ids,
        )

    except Exception as e:
        logger.error(f"[PENDING_REVIEW_COUNT] ❌ Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats/{workflow_id}", response_model=TicketStatsResponse)
@router.get("/stats", response_model=TicketStatsResponse, include_in_schema=False)
async def get_ticket_stats_endpoint(
    workflow_id: Optional[str] = None,
    project_id: Optional[str] = None,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Retrieve aggregate statistics for workflow tickets."""
    logger.info(f"Agent {agent_id} fetching ticket stats (workflow={workflow_id}, project={project_id})")

    try:
        import asyncio

        # Offloaded -- ~10 sequential blocking DB round-trips (several
        # group-by counts plus a full ticket-list scan), all in one
        # request, ran directly on the event loop otherwise.
        return await asyncio.to_thread(
            _compute_ticket_stats, workflow_id, project_id
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get ticket stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _compute_ticket_stats(
    workflow_id: Optional[str], project_id: Optional[str]
) -> Dict[str, Any]:
    """Sync body of get_ticket_stats_endpoint -- run via asyncio.to_thread."""
    from sqlalchemy import func

    from src.core.database import (
        BoardConfig,
        Ticket,
        TicketComment,
        TicketCommit,
        Workflow,
    )

    with get_db() as session:
        # Determine workflow IDs to query
        if workflow_id:
            workflow_ids = [workflow_id]
        elif project_id:
            workflow_ids = [
                wf.id for wf in session.query(Workflow).filter_by(project_id=project_id).all()
            ]
        else:
            return {
                "success": True,
                "workflow_id": None,
                "stats": {"total": 0, "by_status": {}, "by_type": {}, "by_priority": {}},
                "board_config": None,
            }

        # Get board config for this workflow
        board_config = (
            session.query(BoardConfig).filter(BoardConfig.workflow_id.in_(workflow_ids)).first()
        )

        # If no board config found, use a default one
        if not board_config:
            # Return default board config for project-level view
            return {
                "success": True,
                "workflow_id": workflow_ids[0] if workflow_ids else "none",
                "stats": {
                    "total_tickets": 0,
                    "by_status": {},
                    "by_type": {},
                    "by_priority": {},
                    "by_agent": {},
                    "blocked_count": 0,
                    "resolved_count": 0,
                    "avg_comments_per_ticket": 0.0,
                    "avg_commits_per_ticket": 0.0,
                },
                "board_config": {
                    "name": "Default Board",
                    "columns": [
                        {"id": "backlog", "name": "Backlog", "order": 1, "color": "#94a3b8"},
                        {"id": "in-progress", "name": "In Progress", "order": 2, "color": "#f59e0b"},
                        {"id": "review", "name": "In Review", "order": 3, "color": "#ec4899"},
                        {"id": "done", "name": "Done", "order": 4, "color": "#22c55e"},
                    ],
                    "ticket_types": ["feature", "bug", "improvement", "task"],
                    "default_ticket_type": "feature",
                    "initial_status": "backlog",
                    "auto_assign": False,
                    "allow_reopen": True,
                    "track_time": False,
                },
            }

        logger.info(
            f"BoardConfig found: {board_config is not None}, workflow_ids: {workflow_ids}"
        )

        # Total tickets
        total_tickets = (
            session.query(func.count(Ticket.id))
            .filter(Ticket.workflow_id.in_(workflow_ids))
            .scalar()
        )

        # By status
        by_status = {}
        status_counts = (
            session.query(Ticket.status, func.count(Ticket.id))
            .filter(Ticket.workflow_id.in_(workflow_ids))
            .group_by(Ticket.status)
            .all()
        )
        for status, count in status_counts:
            by_status[status] = count

        # By type
        by_type = {}
        type_counts = (
            session.query(Ticket.ticket_type, func.count(Ticket.id))
            .filter(Ticket.workflow_id.in_(workflow_ids))
            .group_by(Ticket.ticket_type)
            .all()
        )
        for ticket_type, count in type_counts:
            by_type[ticket_type] = count

        # By priority
        by_priority = {}
        priority_counts = (
            session.query(Ticket.priority, func.count(Ticket.id))
            .filter(Ticket.workflow_id.in_(workflow_ids))
            .group_by(Ticket.priority)
            .all()
        )
        for priority, count in priority_counts:
            by_priority[priority] = count

        # By agent
        by_agent = {}
        agent_counts = (
            session.query(Ticket.assigned_agent_id, func.count(Ticket.id))
            .filter(Ticket.workflow_id.in_(workflow_ids))
            .filter(Ticket.assigned_agent_id.isnot(None))
            .group_by(Ticket.assigned_agent_id)
            .all()
        )
        for agent_id_val, count in agent_counts:
            by_agent[agent_id_val] = count

        # Blocked count
        tickets_list = (
            session.query(Ticket).filter(Ticket.workflow_id.in_(workflow_ids)).all()
        )
        blocked_count = sum(
            1
            for t in tickets_list
            if t.blocked_by_ticket_ids and len(t.blocked_by_ticket_ids) > 0
        )

        # Resolved count
        resolved_count = (
            session.query(func.count(Ticket.id))
            .filter(Ticket.workflow_id.in_(workflow_ids), Ticket.is_resolved)
            .scalar()
        )

        # Average comments per ticket
        total_comments = (
            session.query(func.count(TicketComment.id))
            .join(Ticket, TicketComment.ticket_id == Ticket.id)
            .filter(Ticket.workflow_id.in_(workflow_ids))
            .scalar()
        )
        avg_comments = total_comments / total_tickets if total_tickets > 0 else 0.0

        # Average commits per ticket
        total_commits = (
            session.query(func.count(TicketCommit.id))
            .join(Ticket, TicketCommit.ticket_id == Ticket.id)
            .filter(Ticket.workflow_id.in_(workflow_ids))
            .scalar()
        )
        avg_commits = total_commits / total_tickets if total_tickets > 0 else 0.0

        stats = {
            "total_tickets": total_tickets,
            "by_status": by_status,
            "by_type": by_type,
            "by_priority": by_priority,
            "by_agent": by_agent,
            "blocked_count": blocked_count,
            "resolved_count": resolved_count,
            "avg_comments_per_ticket": avg_comments,
            "avg_commits_per_ticket": avg_commits,
        }

        return {
            "success": True,
            "workflow_id": workflow_ids[0] if workflow_ids else "none",
            "stats": stats,
            "board_config": {
                "name": board_config.name,
                "columns": board_config.columns,
                "ticket_types": board_config.ticket_types,
                "default_ticket_type": board_config.default_ticket_type,
                "initial_status": board_config.initial_status,
                "auto_assign": board_config.auto_assign,
                "allow_reopen": board_config.allow_reopen,
                "track_time": board_config.track_time,
            } if board_config else None,
        }


@router.get("/{ticket_id}")
async def get_ticket_endpoint(
    ticket_id: str,
    agent_id: str = Header(None, alias="X-Agent-ID"),
):
    """Get full ticket details including comments and history.

    Args:
        ticket_id: The exact ticket ID to fetch (e.g., ticket-c368a0d1-cbd7-4231-a374-0a3a7374064e)
        agent_id: Optional agent ID for logging purposes
    """
    logger.info(f"Agent {agent_id or 'anonymous'} fetching ticket {ticket_id}")

    try:
        ticket = await TicketService.get_ticket(ticket_id)

        if not ticket:
            raise HTTPException(
                status_code=404, detail=f"Ticket not found: {ticket_id}"
            )

        return ticket

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get ticket: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search", response_model=SearchTicketsResponse)
async def search_tickets_endpoint(
    request: SearchTicketsRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """
    Search tickets using hybrid (semantic + keyword) search by default.

    Supports three search modes:
    - "semantic": Vector similarity only
    - "keyword": SQLite FTS5 only
    - "hybrid": Combined (70% semantic + 30% keyword) - DEFAULT
    """
    logger.info(
        f"Agent {agent_id} searching tickets: query='{request.query}', type={request.search_type}"
    )

    try:
        # workflow_id is now required in the request
        workflow_id = request.workflow_id
        logger.info(f"Searching in workflow: {workflow_id}")

        start_time = time.time()

        if request.search_type == "semantic":
            results = await TicketSearchService.semantic_search(
                query_text=request.query,
                workflow_id=workflow_id,
                limit=request.limit,
                filters=request.filters,
            )
        elif request.search_type == "keyword":
            results = await TicketSearchService.keyword_search(
                keywords=request.query,
                workflow_id=workflow_id,
                limit=request.limit,
                filters=request.filters,
            )
        else:  # hybrid (default)
            results = await TicketSearchService.hybrid_search(
                query=request.query,
                workflow_id=workflow_id,
                limit=request.limit,
                filters=request.filters,
                include_comments=request.include_comments,
            )

        search_time_ms = (time.time() - start_time) * 1000

        return SearchTicketsResponse(
            success=True,
            query=request.query,
            results=[TicketSearchResult(**r) for r in results],
            total_found=len(results),
            search_time_ms=search_time_ms,
        )

    except Exception as e:
        logger.error(f"Ticket search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@router.get("", response_model=GetTicketsResponse, include_in_schema=False)
@router.get("/", response_model=GetTicketsResponse)
async def get_tickets_endpoint(
    workflow_id: Optional[str] = None,  # Optional - can filter by project instead
    project_id: Optional[str] = None,  # Filter by project
    agent_id: str = Header(..., alias="X-Agent-ID"),
    status: Optional[str] = None,
    ticket_type: Optional[str] = None,
    priority: Optional[str] = None,
    assigned_agent_id: Optional[str] = None,
    include_completed: bool = True,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc",
):
    """Get/list tickets with filtering and pagination."""

    try:
        logger.info(f"Agent {agent_id} fetching tickets (workflow={workflow_id}, project={project_id})")

        # Build filters dict, only including non-None values
        filters = {}
        if status is not None:
            filters["status"] = status
        if ticket_type is not None:
            filters["ticket_type"] = ticket_type
        if priority is not None:
            filters["priority"] = priority
        if assigned_agent_id is not None:
            filters["assigned_agent_id"] = assigned_agent_id
        if not include_completed:
            filters["include_completed"] = include_completed

        if workflow_id:
            result = await TicketService.get_tickets_by_workflow(
                workflow_id=workflow_id,
                filters=filters,
            )
        elif project_id:
            # Get all workflows for this project, then get tickets for all of them
            from src.core.database import Workflow, get_db
            with get_db() as db:
                workflow_ids = [
                    wf.id for wf in db.query(Workflow).filter_by(project_id=project_id).all()
                ]
            result = []
            for wf_id in workflow_ids:
                wf_tickets = await TicketService.get_tickets_by_workflow(
                    workflow_id=wf_id,
                    filters=filters,
                )
                result.extend(wf_tickets)
        else:
            # No workflow_id or project_id - return empty
            result = []

        # Result is a list of ticket dicts
        tickets = [TicketDetail(**t) for t in result]

        return GetTicketsResponse(
            success=True,
            tickets=tickets,
            total_count=len(tickets),
            has_more=False,  # TODO: Implement pagination in service
        )

    except Exception as e:
        logger.error(f"Failed to get tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resolve", response_model=ResolveTicketResponse)
async def resolve_ticket_endpoint(
    request: ResolveTicketRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Mark ticket as resolved and automatically unblock dependent tickets."""
    logger.info(f"Agent {agent_id} resolving ticket {request.ticket_id}")

    try:
        result = await TicketService.resolve_ticket(
            ticket_id=request.ticket_id,
            agent_id=agent_id,
            resolution_comment=request.resolution_comment,
            commit_sha=request.commit_sha,
        )

        # Broadcast update
        await _broadcast_update(
            {
                "type": "ticket_resolved",
                "ticket_id": request.ticket_id,
                "agent_id": agent_id,
                "unblocked_tickets": result["unblocked_tickets"],
            },
            workflow_id=_get_workflow_id_for_ticket(request.ticket_id),
        )

        return ResolveTicketResponse(**result)

    except ValueError as e:
        logger.error(f"Validation error resolving ticket: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to resolve ticket: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/link-commit", response_model=LinkCommitResponse)
async def link_commit_endpoint(
    request: LinkCommitRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Manually link a git commit to a ticket."""
    logger.info(
        f"Agent {agent_id} linking commit {request.commit_sha} to ticket {request.ticket_id}"
    )

    try:
        result = await TicketService.link_commit(
            ticket_id=request.ticket_id,
            agent_id=agent_id,
            commit_sha=request.commit_sha,
            commit_message=request.commit_message,
            link_method="manual",
        )

        # Broadcast update
        await _broadcast_update(
            {
                "type": "commit_linked",
                "ticket_id": request.ticket_id,
                "agent_id": agent_id,
                "commit_sha": request.commit_sha,
            },
            workflow_id=_get_workflow_id_for_ticket(request.ticket_id),
        )

        return LinkCommitResponse(**result)

    except ValueError as e:
        logger.error(f"Validation error linking commit: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to link commit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Approve / Reject / Commit Diff ─────────────────────────────────


class ApproveTicketResponse(BaseModel):
    """Response model for ticket approval."""

    success: bool
    ticket_id: str
    message: str


class RejectTicketResponse(BaseModel):
    """Response model for ticket rejection."""

    success: bool
    ticket_id: str
    message: str


class FileDiff(BaseModel):
    """File diff information for commit."""

    path: str
    status: str  # modified, added, deleted, renamed
    insertions: int
    deletions: int
    diff: str  # Unified diff content
    language: str  # For syntax highlighting
    old_path: Optional[str] = None  # For renamed files


class CommitDiffResponse(BaseModel):
    """Response model for commit diff."""

    success: bool
    commit_sha: str
    commit_message: str
    author: str
    commit_timestamp: str
    files_changed: int
    total_insertions: int
    total_deletions: int
    total_files: int
    files: List[FileDiff]


@router.post("/approve", response_model=ApproveTicketResponse)
async def approve_ticket_endpoint(
    request: Request,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    """
    Approve a pending ticket.

    Body: {"ticket_id": "ticket-uuid"}
    """
    from src.core.app_context import get_app_state

    logger.info(f"[APPROVE_TICKET] Agent {agent_id} approving ticket")

    try:
        data = await request.json()
        ticket_id = data.get("ticket_id")

        if not ticket_id:
            raise HTTPException(status_code=400, detail="ticket_id required")

        logger.info(f"[APPROVE_TICKET] Ticket ID: {ticket_id}")

        result = await TicketService.approve_ticket(
            ticket_id=ticket_id,
            approved_by=agent_id,
        )

        # Broadcast approval
        server_state = get_app_state()
        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(
            _get_workflow_id_for_ticket(ticket_id)
        )
        await server_state.broadcast_update(
            {
                "type": "ticket_approved",
                "ticket_id": ticket_id,
                "approved_by": agent_id,
                "pending_count": TicketService.get_pending_review_count(),
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        logger.info(f"[APPROVE_TICKET] Ticket {ticket_id} approved successfully")

        return ApproveTicketResponse(**result)

    except ValueError as e:
        logger.error(f"[APPROVE_TICKET] Validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[APPROVE_TICKET] Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reject", response_model=RejectTicketResponse)
async def reject_ticket_endpoint(
    request: Request,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    """
    Reject a pending ticket.

    Body: {"ticket_id": "ticket-uuid", "rejection_reason": "..."}
    """
    from src.core.app_context import get_app_state

    logger.info(f"[REJECT_TICKET] Agent {agent_id} rejecting ticket")

    try:
        data = await request.json()
        ticket_id = data.get("ticket_id")
        rejection_reason = data.get("rejection_reason", "")

        if not ticket_id:
            raise HTTPException(status_code=400, detail="ticket_id required")

        if not rejection_reason:
            raise HTTPException(status_code=400, detail="rejection_reason required")

        logger.info(
            f"[REJECT_TICKET] Ticket ID: {ticket_id}, Reason: {rejection_reason}"
        )

        result = await TicketService.reject_ticket(
            ticket_id=ticket_id,
            rejected_by=agent_id,
            rejection_reason=rejection_reason,
        )

        # Broadcast rejection
        server_state = get_app_state()
        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(
            _get_workflow_id_for_ticket(ticket_id)
        )
        await server_state.broadcast_update(
            {
                "type": "ticket_rejected",
                "ticket_id": ticket_id,
                "rejected_by": agent_id,
                "rejection_reason": rejection_reason,
                "pending_count": TicketService.get_pending_review_count(),
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        logger.info(f"[REJECT_TICKET] Ticket {ticket_id} rejected successfully")

        return RejectTicketResponse(**result)

    except ValueError as e:
        logger.error(f"[REJECT_TICKET] Validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[REJECT_TICKET] Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/commit-diff/{commit_sha}", response_model=CommitDiffResponse)
async def get_commit_diff_endpoint(
    commit_sha: str,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get detailed git diff for a commit (for Git Diff Window in UI)."""
    import asyncio
    import functools
    import os
    import re
    import subprocess

    from src.core.simple_config import get_config

    loop = asyncio.get_event_loop()

    logger.info(f"Agent {agent_id} fetching commit diff for {commit_sha}")

    try:
        # Resolve the repo this commit actually belongs to via the ticket
        # it's linked to -- falls back to the process-wide "active project"
        # singleton (today's behavior) when the commit isn't linked to any
        # ticket, e.g. commits made outside the ticket-linking flow.
        main_repo_path = _resolve_repo_path_for_commit(commit_sha)
        if main_repo_path is None:
            config = get_config()
            main_repo_path = str(config.git.main_repo_path)

        # Helper function to detect language from file extension
        def detect_language(file_path: str) -> str:
            ext_map = {
                ".py": "python",
                ".js": "javascript",
                ".ts": "typescript",
                ".tsx": "tsx",
                ".jsx": "jsx",
                ".go": "go",
                ".rs": "rust",
                ".java": "java",
                ".c": "c",
                ".cpp": "cpp",
                ".h": "c",
                ".hpp": "cpp",
                ".md": "markdown",
                ".yaml": "yaml",
                ".yml": "yaml",
                ".json": "json",
                ".sql": "sql",
                ".sh": "bash",
            }
            ext = os.path.splitext(file_path)[1].lower()
            return ext_map.get(ext, "text")

        # Get commit metadata from the correct repository
        cmd = ["git", "show", "--format=%H|%an|%at|%s", "-s", commit_sha]
        result = await loop.run_in_executor(
            None,
            functools.partial(
                subprocess.run,
                cmd, cwd=main_repo_path, capture_output=True, text=True, check=True, timeout=10
            ),
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=404, detail=f"Commit not found: {commit_sha}"
            )

        parts = result.stdout.strip().split("|", 3)
        commit_hash = parts[0] if len(parts) > 0 else commit_sha
        author = parts[1] if len(parts) > 1 else "unknown"
        timestamp_unix = int(parts[2]) if len(parts) > 2 else 0
        message = parts[3] if len(parts) > 3 else "No message"

        timestamp = (
            datetime.utcfromtimestamp(timestamp_unix).isoformat() + "Z"
            if timestamp_unix > 0
            else datetime.utcnow().isoformat() + "Z"
        )

        # Get file stats from the correct repository
        cmd = ["git", "diff", "--numstat", f"{commit_sha}^", commit_sha]
        result = await loop.run_in_executor(
            None,
            functools.partial(
                subprocess.run,
                cmd, cwd=main_repo_path, capture_output=True, text=True, check=True, timeout=10
            ),
        )

        files_data = []
        total_insertions = 0
        total_deletions = 0

        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue

            insertions = int(parts[0]) if parts[0].isdigit() else 0
            deletions = int(parts[1]) if parts[1].isdigit() else 0
            file_path = parts[2]

            total_insertions += insertions
            total_deletions += deletions

            # Get unified diff for this file from the correct repository
            cmd_diff = ["git", "diff", f"{commit_sha}^", commit_sha, "--", file_path]
            diff_result = await loop.run_in_executor(
                None,
                functools.partial(
                    subprocess.run,
                    cmd_diff, cwd=main_repo_path, capture_output=True, text=True, timeout=10
                ),
            )

            # Determine file status
            status = "modified"
            old_path = None
            if "new file mode" in diff_result.stdout:
                status = "added"
            elif "deleted file mode" in diff_result.stdout:
                status = "deleted"
            elif "rename from" in diff_result.stdout:
                status = "renamed"
                rename_match = re.search(r"rename from (.+)", diff_result.stdout)
                if rename_match:
                    old_path = rename_match.group(1)

            files_data.append(
                FileDiff(
                    path=file_path,
                    status=status,
                    insertions=insertions,
                    deletions=deletions,
                    diff=diff_result.stdout,
                    language=detect_language(file_path),
                    old_path=old_path,
                )
            )

        return CommitDiffResponse(
            success=True,
            commit_sha=commit_hash,
            commit_message=message,
            author=author,
            commit_timestamp=timestamp,
            files_changed=len(files_data),
            total_insertions=total_insertions,
            total_deletions=total_deletions,
            total_files=len(files_data),
            files=files_data,
        )

    except subprocess.CalledProcessError as e:
        logger.error(f"Git command failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get commit diff: {e}")
    except Exception as e:
        logger.error(f"Failed to get commit diff: {e}")
        raise HTTPException(status_code=500, detail=str(e))

