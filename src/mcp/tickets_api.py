"""Ticket management API routes.

Extracted from server.py for better modularity (M-1 fix).
"""

import logging
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field

from src.core.database import DatabaseManager, get_db
from src.services.ticket_search_service import TicketSearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tickets", tags=["tickets"])

# Lazy imports to avoid circular dependency
_ticket_service = None


def _get_ticket_service():
    """Get TicketService instance (lazy import to avoid circular deps)."""
    global _ticket_service
    if _ticket_service is None:
        from src.services.ticket_service import TicketService
        _ticket_service = TicketService
    return _ticket_service


async def _broadcast_update(data: dict):
    """Broadcast update to SSE/WebSocket clients."""
    try:
        from src.core.app_context import get_app_state

        server_state = get_app_state()
        await server_state.broadcast_update(data)
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
    phase_id: Optional[str] = Field(
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
            f"[TICKET_CREATE] Calling _get_ticket_service().create_ticket with workflow_id={workflow_id}"
        )
        result = await _get_ticket_service().create_ticket(
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
            phase_id=request.phase_id,
        )

        logger.info(
            "[TICKET_CREATE] ✅ _get_ticket_service().create_ticket returned successfully"
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
            }
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
        # Return a warning response instead of crashing the agent
        return CreateTicketResponse(
            ticket_id="",
            workflow_id=workflow_id if "workflow_id" in dir() else "",
            agent_id=agent_id,
            title=request.title,
            ticket_type=request.ticket_type,
            priority=request.priority,
            status="skipped",
            description=f"Ticket creation skipped: {e}",
            created_at="",
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
        result = await _get_ticket_service().update_ticket(
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
            }
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
        result = await _get_ticket_service().change_status(
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
            }
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
        result = await _get_ticket_service().add_comment(
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
            }
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
        count = _get_ticket_service().get_pending_review_count()
        ticket_ids = _get_ticket_service().get_pending_review_tickets()

        logger.info(f"[PENDING_REVIEW_COUNT] Found {count} tickets pending review")

        return PendingReviewCountResponse(
            count=count,
            ticket_ids=ticket_ids,
        )

    except Exception as e:
        logger.error(f"[PENDING_REVIEW_COUNT] ❌ Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
        ticket = await _get_ticket_service().get_ticket(ticket_id)

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


@router.get("/stats/{workflow_id}", response_model=TicketStatsResponse)
@router.get("/stats", response_model=TicketStatsResponse)
async def get_ticket_stats_endpoint(
    workflow_id: Optional[str] = None,
    project_id: Optional[str] = None,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Retrieve aggregate statistics for workflow tickets."""
    logger.info(f"Agent {agent_id} fetching ticket stats (workflow={workflow_id}, project={project_id})")

    try:
        from sqlalchemy import func

        from src.core.database import BoardConfig, Ticket, TicketComment, TicketCommit, Workflow

        session = server_state.db_manager.get_session()
        try:
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
                .filter_by(workflow_id=workflow_id)
                .group_by(Ticket.priority)
                .all()
            )
            for priority, count in priority_counts:
                by_priority[priority] = count

            # By agent
            by_agent = {}
            agent_counts = (
                session.query(Ticket.assigned_agent_id, func.count(Ticket.id))
                .filter_by(workflow_id=workflow_id)
                .filter(Ticket.assigned_agent_id.isnot(None))
                .group_by(Ticket.assigned_agent_id)
                .all()
            )
            for agent_id_val, count in agent_counts:
                by_agent[agent_id_val] = count

            # Blocked count
            tickets_list = (
                session.query(Ticket).filter_by(workflow_id=workflow_id).all()
            )
            blocked_count = sum(
                1
                for t in tickets_list
                if t.blocked_by_ticket_ids and len(t.blocked_by_ticket_ids) > 0
            )

            # Resolved count
            resolved_count = (
                session.query(func.count(Ticket.id))
                .filter_by(workflow_id=workflow_id, is_resolved=True)
                .scalar()
            )

            # Average comments per ticket
            total_comments = (
                session.query(func.count(TicketComment.id))
                .join(Ticket, TicketComment.ticket_id == Ticket.id)
                .filter(Ticket.workflow_id == workflow_id)
                .scalar()
            )
            avg_comments = total_comments / total_tickets if total_tickets > 0 else 0.0

            # Average commits per ticket
            total_commits = (
                session.query(func.count(TicketCommit.id))
                .join(Ticket, TicketCommit.ticket_id == Ticket.id)
                .filter(Ticket.workflow_id == workflow_id)
                .scalar()
            )
            avg_commits = total_commits / total_tickets if total_tickets > 0 else 0.0

            # Created today
            today_start = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            created_today = (
                session.query(func.count(Ticket.id))
                .filter(
                    Ticket.workflow_id == workflow_id, Ticket.created_at >= today_start
                )
                .scalar()
            )

            # Completed today
            completed_today = (
                session.query(func.count(Ticket.id))
                .filter(
                    Ticket.workflow_id == workflow_id,
                    Ticket.completed_at >= today_start,
                )
                .scalar()
                if Ticket.completed_at
                else 0
            )

            # Velocity last 7 days (tickets completed in last 7 days)
            seven_days_ago = datetime.utcnow() - timedelta(days=7)
            velocity_last_7_days = (
                session.query(func.count(Ticket.id))
                .filter(
                    Ticket.workflow_id == workflow_id,
                    Ticket.is_resolved,
                    Ticket.resolved_at >= seven_days_ago,
                )
                .scalar()
            )

            stats = TicketStats(
                total_tickets=total_tickets or 0,
                by_status=by_status,
                by_type=by_type,
                by_priority=by_priority,
                by_agent=by_agent,
                blocked_count=blocked_count,
                resolved_count=resolved_count or 0,
                avg_comments_per_ticket=avg_comments or 0.0,
                avg_commits_per_ticket=avg_commits or 0.0,
                created_today=created_today or 0,
                completed_today=completed_today or 0,
                velocity_last_7_days=velocity_last_7_days or 0,
            )

            # Convert board_config to dict if it exists
            board_config_dict = None
            if board_config:
                board_config_dict = {
                    "name": board_config.name,
                    "columns": board_config.columns,
                    "ticket_types": board_config.ticket_types,
                    "default_ticket_type": board_config.default_ticket_type,
                    "initial_status": board_config.initial_status,
                    "auto_assign": board_config.auto_assign
                    if hasattr(board_config, "auto_assign")
                    else False,
                    "allow_reopen": board_config.allow_reopen
                    if hasattr(board_config, "allow_reopen")
                    else True,
                    "track_time": board_config.track_time
                    if hasattr(board_config, "track_time")
                    else False,
                }

            return TicketStatsResponse(
                success=True,
                workflow_id=workflow_id,
                stats=stats,
                board_config=board_config_dict,
            )

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Failed to get ticket stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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

        ticket_service = _get_ticket_service()

        if workflow_id:
            result = await ticket_service.get_tickets_by_workflow(
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
                wf_tickets = await ticket_service.get_tickets_by_workflow(
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
        result = await _get_ticket_service().resolve_ticket(
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
            }
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
        result = await _get_ticket_service().link_commit(
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
            }
        )

        return LinkCommitResponse(**result)

    except ValueError as e:
        logger.error(f"Validation error linking commit: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to link commit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


