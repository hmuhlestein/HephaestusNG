"""Service layer for managing tickets in the ticket tracking system."""

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.core.database import (
    Agent,
    BoardConfig,
    Task,
    Ticket,
    TicketComment,
    TicketCommit,
    TicketHistory,
    Workflow,
    get_db,
)
from src.services.ticket_history_service import TicketHistoryService
from src.services.ticket_search_service import TicketSearchService

logger = logging.getLogger(__name__)


class TicketApprovalManager:
    """Manages ticket approval workflow with timeout and blocking."""

    def __init__(self):
        self._pending_approvals: Dict[
            str, asyncio.Event
        ] = {}  # ticket_id -> asyncio.Event
        self._approval_decisions: Dict[
            str, Dict[str, Any]
        ] = {}  # ticket_id -> {"approved": bool, "reason": str}

    async def wait_for_approval(
        self, ticket_id: str, timeout_seconds: int
    ) -> Dict[str, Any]:
        """
        Block and wait for human approval decision.

        Args:
            ticket_id: ID of the ticket awaiting approval
            timeout_seconds: How long to wait before timing out

        Returns:
            Dictionary with keys:
                - approved (bool): Whether ticket was approved
                - reason (str): Rejection reason if rejected
                - timed_out (bool): Whether request timed out
        """
        event = asyncio.Event()
        self._pending_approvals[ticket_id] = event

        try:
            logger.info(
                f"[APPROVAL_MANAGER] Waiting for approval of ticket {ticket_id} (timeout: {timeout_seconds}s)"
            )

            # Wait for approval with timeout
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)

            # Get decision
            decision = self._approval_decisions.pop(ticket_id, None)
            if decision:
                logger.info(
                    f"[APPROVAL_MANAGER] Ticket {ticket_id} decision: approved={decision.get('approved')}"
                )
                return {**decision, "timed_out": False}
            else:
                # Event was set but no decision found (shouldn't happen)
                logger.error(
                    f"[APPROVAL_MANAGER] Ticket {ticket_id} event set but no decision recorded"
                )
                return {
                    "approved": False,
                    "reason": "No decision recorded",
                    "timed_out": False,
                }

        except asyncio.TimeoutError:
            logger.warning(
                f"[APPROVAL_MANAGER] Ticket {ticket_id} approval timed out after {timeout_seconds}s"
            )
            return {"approved": False, "reason": "Approval timeout", "timed_out": True}
        finally:
            # Cleanup
            self._pending_approvals.pop(ticket_id, None)
            self._approval_decisions.pop(ticket_id, None)

    def record_decision(self, ticket_id: str, approved: bool, reason: str = "") -> None:
        """
        Record approval decision and wake up waiting coroutine.

        Args:
            ticket_id: ID of the ticket
            approved: Whether ticket was approved
            reason: Rejection reason if rejected
        """
        logger.info(
            f"[APPROVAL_MANAGER] Recording decision for ticket {ticket_id}: approved={approved}"
        )
        self._approval_decisions[ticket_id] = {"approved": approved, "reason": reason}
        event = self._pending_approvals.get(ticket_id)
        if event:
            event.set()
        else:
            logger.warning(
                f"[APPROVAL_MANAGER] No pending approval found for ticket {ticket_id}"
            )

    def is_pending(self, ticket_id: str) -> bool:
        """Check if ticket is awaiting approval."""
        return ticket_id in self._pending_approvals

    def get_pending_count(self) -> int:
        """Get count of tickets pending review."""
        return len(self._pending_approvals)

    def get_pending_ticket_ids(self) -> List[str]:
        """Get list of ticket IDs pending human review."""
        return list(self._pending_approvals.keys())


# Global instance
approval_manager = TicketApprovalManager()


class TicketService:
    """Service for managing ticket operations."""

    @staticmethod
    def _check_circular_blocking(ticket_id: str, blocked_by_ids: List[str], db) -> None:
        """
        Check for circular blocking dependencies.

        Prevents scenarios like: A blocks B, B blocks A -- and longer
        chains (A blocks B, B blocks C, C blocks A), which a pairwise
        direct-neighbor check misses entirely.

        Args:
            ticket_id: ID of the ticket being updated
            blocked_by_ids: List of ticket IDs that would block this ticket
            db: Database session

        Raises:
            ValueError: If circular blocking is detected
        """
        # BFS over the "blocked_by" graph, starting from each candidate
        # blocker. An edge here means "X is blocked_by Y" (X depends on Y).
        # If following these edges transitively from any candidate ever
        # reaches ticket_id again, adding this dependency would close a
        # cycle of some length -- not just the direct A<->B case the old
        # pairwise check caught. `visited` also guards against an
        # already-existing cycle in the data sending this into an infinite
        # loop.
        visited = set()
        queue = list(blocked_by_ids)
        while queue:
            current_id = queue.pop(0)
            if current_id == ticket_id:
                raise ValueError(
                    f"Circular blocking detected: adding this dependency "
                    f"would create a cycle back to ticket {ticket_id}"
                )
            if current_id in visited:
                continue
            visited.add(current_id)
            current_ticket = db.query(Ticket).filter_by(id=current_id).first()
            if current_ticket and current_ticket.blocked_by_ticket_ids:
                queue.extend(current_ticket.blocked_by_ticket_ids)

    @staticmethod
    def _validate_ticket_creation(
        db,
        workflow_id: str,
        ticket_type: str,
        initial_status: Optional[str],
        blocked_by_ticket_ids: List[str],
        agent_id: str,
    ) -> Tuple[Workflow, BoardConfig, str]:
        """
        Validate a ticket-creation request against the workflow and its
        board config.

        Args:
            db: Database session
            workflow_id: ID of the workflow this ticket belongs to
            ticket_type: Type of ticket to validate against board_config
            initial_status: Requested initial status, or None to use board_config's default
            blocked_by_ticket_ids: List of ticket IDs that would block the new ticket
            agent_id: ID of the agent creating the ticket

        Returns:
            Tuple of (workflow, board_config, resolved initial_status)

        Raises:
            ValueError: If validation fails
        """
        # Validate workflow exists and is active
        logger.info(f"[TICKET_SERVICE] Querying for workflow: {workflow_id}")
        workflow = db.query(Workflow).filter_by(id=workflow_id).first()
        logger.info(f"[TICKET_SERVICE] Workflow found: {workflow is not None}")
        if not workflow:
            logger.error(f"[TICKET_SERVICE] ❌ Workflow not found: {workflow_id}")
            logger.error("[TICKET_SERVICE] ========== FAILED ==========")
            raise ValueError(f"Workflow not found: {workflow_id}")
        if workflow.status not in ["active", "paused", "completed"]:
            raise ValueError(
                f"Workflow is not in a state that allows ticket creation: {workflow.status}"
            )

        # Validate board_config exists for workflow
        board_config = (
            db.query(BoardConfig).filter_by(workflow_id=workflow_id).first()
        )
        if not board_config:
            raise ValueError(
                f"Board configuration not found for workflow: {workflow_id}"
            )

        # Validate board config structure
        if (
            not isinstance(board_config.columns, list)
            or len(board_config.columns) == 0
        ):
            raise ValueError(
                "Invalid board configuration: columns must be a non-empty list"
            )

        if (
            not isinstance(board_config.ticket_types, list)
            or len(board_config.ticket_types) == 0
        ):
            raise ValueError(
                "Invalid board configuration: ticket_types must be a non-empty list"
            )

        # Validate ticket_type is allowed by board_config
        allowed_types = [
            t["id"] if isinstance(t, dict) else t for t in board_config.ticket_types
        ]
        if ticket_type not in allowed_types:
            raise ValueError(
                f"Invalid ticket type '{ticket_type}'. Allowed types: {allowed_types}"
            )

        # Use board_config.initial_status if initial_status is None
        if initial_status is None:
            initial_status = board_config.initial_status

        # Validate status is valid per board_config
        valid_statuses = [
            col["id"] if isinstance(col, dict) else col
            for col in board_config.columns
        ]
        if initial_status not in valid_statuses:
            raise ValueError(
                f"Invalid status '{initial_status}'. Valid statuses: {valid_statuses}"
            )

        # Ensure initial_status exists in columns
        if board_config.initial_status not in valid_statuses:
            raise ValueError(
                f"Invalid board config: initial_status '{board_config.initial_status}' not in columns"
            )

        # Validate all blocked_by_ticket_ids exist and belong to same workflow
        for blocking_ticket_id in blocked_by_ticket_ids:
            blocking_ticket = (
                db.query(Ticket).filter_by(id=blocking_ticket_id).first()
            )
            if not blocking_ticket:
                raise ValueError(f"Blocking ticket not found: {blocking_ticket_id}")
            if blocking_ticket.workflow_id != workflow_id:
                raise ValueError(
                    f"Blocking ticket {blocking_ticket_id} belongs to different workflow"
                )

        # Check for circular blocking (even for new ticket)
        # This prevents creating a ticket blocked by another that might later try to be blocked by this one
        # Note: This is a basic check; more complex cycles would require graph traversal

        # Validate agent exists
        agent = db.query(Agent).filter_by(id=agent_id).first()
        if not agent:
            raise ValueError(f"Agent not found: {agent_id}")

        return workflow, board_config, initial_status

    @staticmethod
    def _delete_ticket_cascade(db, ticket_id: str, reason: str) -> bool:
        """
        Delete a ticket and its dependent TicketHistory/TicketComment/
        TicketCommit rows in one transaction.

        Args:
            db: Database session
            ticket_id: ID of the ticket to delete
            reason: Human-readable reason for the deletion, used only in the log message

        Returns:
            True if the ticket existed and was deleted, False if it was already gone.
        """
        ticket = db.query(Ticket).filter_by(id=ticket_id).first()
        if not ticket:
            logger.warning(
                f"[TICKET_SERVICE] Ticket {ticket_id} not found for deletion (already deleted?)"
            )
            return False

        # Delete associated records first to avoid foreign key constraint errors
        history_count = db.query(TicketHistory).filter_by(ticket_id=ticket_id).delete()
        comment_count = db.query(TicketComment).filter_by(ticket_id=ticket_id).delete()
        commit_count = db.query(TicketCommit).filter_by(ticket_id=ticket_id).delete()

        # Now delete the ticket
        db.delete(ticket)
        db.commit()
        logger.info(
            f"[TICKET_SERVICE] ✅ Ticket {ticket_id} deleted due to {reason} "
            f"(deleted {history_count} history, {comment_count} comments, {commit_count} commits)"
        )
        return True

    @staticmethod
    async def _broadcast_ticket_event(
        event_type: str,
        workflow_id: str,
        payload: Dict[str, Any],
        failure_context: str,
    ) -> None:
        """
        Broadcast a ticket lifecycle event, logging and swallowing any
        failure so a broadcast issue never fails ticket creation itself.

        Args:
            event_type: The "type" field of the broadcast payload
            workflow_id: Workflow ID used to resolve broadcast scoping
            payload: Event-specific fields merged into the broadcast payload
                (workflow_id and pending_count are added by this helper)
            failure_context: Short description used in the failure log message
        """
        try:
            from src.core.app_context import get_app_state
            from src.core.database import resolve_project_for_workflow

            server_state = get_app_state()
            bcast_project_id, bcast_project_name = resolve_project_for_workflow(
                workflow_id
            )
            await server_state.broadcast_update(
                {
                    "type": event_type,
                    "workflow_id": workflow_id,
                    "pending_count": approval_manager.get_pending_count(),
                    **payload,
                },
                project_id=bcast_project_id,
                project_name=bcast_project_name,
            )
        except Exception as e:
            logger.warning(f"[TICKET_SERVICE] Failed to broadcast {failure_context}: {e}")

    @staticmethod
    async def _wait_for_ticket_approval(
        ticket_id: str, workflow_id: str, title: str, approval_timeout: int
    ) -> None:
        """
        Wait for human approval of a pending ticket. Deletes the ticket
        and raises ValueError on timeout or rejection; marks it approved
        and returns normally otherwise.

        Args:
            ticket_id: ID of the newly created, pending-review ticket
            workflow_id: Workflow ID used to resolve broadcast scoping
            title: Ticket title, included in the pending-review broadcast
            approval_timeout: Seconds to wait before treating this as a timeout

        Raises:
            ValueError: If the ticket times out or is rejected
        """
        logger.info(
            f"[TICKET_SERVICE] Waiting for human approval (timeout: {approval_timeout}s)..."
        )

        await TicketService._broadcast_ticket_event(
            "ticket_pending_review",
            workflow_id,
            {"ticket_id": ticket_id, "title": title},
            "pending review",
        )

        decision = await approval_manager.wait_for_approval(ticket_id, approval_timeout)

        if decision["timed_out"]:
            logger.error(f"[TICKET_SERVICE] Approval timeout for ticket {ticket_id}")
            with get_db() as db:
                deleted = TicketService._delete_ticket_cascade(db, ticket_id, "timeout")
            if deleted:
                await TicketService._broadcast_ticket_event(
                    "ticket_deleted",
                    workflow_id,
                    {"ticket_id": ticket_id, "reason": "timeout"},
                    "ticket deletion",
                )
            raise ValueError(
                f"Ticket approval timeout after {approval_timeout} seconds. Please try again."
            )

        if not decision["approved"]:
            rejection_reason = decision.get("reason", "No reason provided")
            logger.error(
                f"[TICKET_SERVICE] Ticket {ticket_id} rejected: {rejection_reason}"
            )
            with get_db() as db:
                deleted = TicketService._delete_ticket_cascade(db, ticket_id, "rejection")
            if deleted:
                await TicketService._broadcast_ticket_event(
                    "ticket_deleted",
                    workflow_id,
                    {
                        "ticket_id": ticket_id,
                        "reason": "rejected",
                        "rejection_reason": rejection_reason,
                    },
                    "ticket deletion",
                )
            raise ValueError(f"Ticket rejected by human reviewer: {rejection_reason}")

        # Approved - update ticket
        logger.info(f"[TICKET_SERVICE] Ticket {ticket_id} approved by human")
        with get_db() as db:
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            if ticket:
                ticket.approval_status = "approved"
                ticket.approval_decided_at = datetime.utcnow()
                db.commit()
                logger.info(
                    f"[TICKET_SERVICE] Ticket {ticket_id} approval status updated to 'approved'"
                )

        await TicketService._broadcast_ticket_event(
            "ticket_approved", workflow_id, {"ticket_id": ticket_id}, "approval"
        )

    @staticmethod
    async def _index_new_ticket(
        ticket_id: str,
        title: str,
        description: str,
        workflow_id: str,
        ticket_type: str,
        priority: str,
        status: str,
        tags: List[str],
        agent_id: str,
        assigned_agent_id: Optional[str],
        blocked_by_ticket_ids: List[str],
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        Generate the new ticket's embedding, store it, and check for
        near-duplicate tickets. Never raises -- a failure is logged and
        reported as embedding_created=False, since the ticket itself is
        already committed by this point.

        Returns:
            Tuple of (embedding_created, similar_tickets)
        """
        logger.info(f"[TICKET_SERVICE] Generating embedding for ticket {ticket_id}...")
        try:
            logger.info(
                f"Creating ticket for workflow {workflow_id}: '{title}' by agent {agent_id}"
            )

            embedding_id = await TicketSearchService.index_ticket(
                ticket_id=ticket_id,
                title=title,
                description=description,
                comments=[],
                workflow_id=workflow_id,
                ticket_type=ticket_type,
                priority=priority,
                status=status,
                tags=tags,
                created_at=datetime.utcnow().isoformat() + "Z",
                updated_at=datetime.utcnow().isoformat() + "Z",
                created_by_agent_id=agent_id,
                assigned_agent_id=assigned_agent_id,
                is_blocked=len(blocked_by_ticket_ids) > 0,
            )

            # Update ticket record with embedding
            with get_db() as db:
                ticket = db.query(Ticket).filter_by(id=ticket_id).first()
                # Embedding is stored in Qdrant, just mark it
                ticket.embedding_id = embedding_id
                db.commit()

            # Find semantically similar tickets for duplicate detection
            similar_tickets = await TicketSearchService.find_related_tickets(
                ticket_id, limit=3
            )

            # Warn if potential duplicate detected
            if (
                similar_tickets
                and similar_tickets[0]["similarity_score"]
                >= TicketSearchService.DUPLICATE_THRESHOLD
            ):
                logger.warning(
                    f"Potential duplicate ticket detected: {similar_tickets[0]['ticket_id']} "
                    f"(similarity: {similar_tickets[0]['similarity_score']:.2f})"
                )

            return True, similar_tickets

        except Exception as e:
            # Log error but don't fail ticket creation
            logger.error(
                f"[TICKET_SERVICE] ❌ Failed to generate embedding for ticket {ticket_id}: {e}"
            )
            return False, []

    @staticmethod
    async def create_ticket(
        workflow_id: str,
        agent_id: str,
        title: str,
        description: str,
        ticket_type: str,
        priority: str,
        initial_status: Optional[str] = None,
        assigned_agent_id: Optional[str] = None,
        parent_ticket_id: Optional[str] = None,
        blocked_by_ticket_ids: List[str] = None,
        tags: List[str] = None,
        related_task_ids: List[str] = None,
        task_id: Optional[str] = None,
        phase_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new ticket in the workflow.

        Args:
            workflow_id: ID of the workflow this ticket belongs to
            agent_id: ID of the agent creating the ticket
            title: Short, descriptive title for the ticket
            description: Detailed description of what needs to be done
            ticket_type: Type of ticket (bug, feature, improvement, task, spike)
            priority: Priority level (low, medium, high, critical)
            initial_status: Initial status (if None, uses board_config.initial_status)
            assigned_agent_id: Optional agent to assign ticket to
            parent_ticket_id: Optional parent ticket ID for sub-tickets
            blocked_by_ticket_ids: List of ticket IDs blocking this ticket
            tags: List of tags for categorization
            related_task_ids: List of related task IDs
            task_id: Optional task ID this ticket relates to
            phase_id: Optional phase ID where this ticket was created

        Returns:
            Dictionary containing ticket details and status

        Raises:
            ValueError: If validation fails
        """
        logger.info("[TICKET_SERVICE] ========== START ==========")
        logger.info(f"[TICKET_SERVICE] workflow_id: {workflow_id}")
        logger.info(f"[TICKET_SERVICE] agent_id: {agent_id}")
        logger.info(f"[TICKET_SERVICE] title: {title[:60]}...")
        logger.info(
            f"[TICKET_SERVICE] ticket_type: {ticket_type}, priority: {priority}"
        )

        blocked_by_ticket_ids = blocked_by_ticket_ids or []
        tags = tags or []
        related_task_ids = related_task_ids or []

        with get_db() as db:
            workflow, board_config, initial_status = (
                TicketService._validate_ticket_creation(
                    db,
                    workflow_id,
                    ticket_type,
                    initial_status,
                    blocked_by_ticket_ids,
                    agent_id,
                )
            )

            # Check if human review is enabled
            human_review_enabled = board_config.ticket_human_review or False
            approval_timeout = board_config.approval_timeout_seconds or 1800
            logger.info(
                f"[TICKET_SERVICE] Human review enabled: {human_review_enabled}"
            )

            # Determine initial approval_status
            if human_review_enabled:
                approval_status = "pending_review"
                approval_requested_at = datetime.utcnow()
                logger.info(
                    f"[TICKET_SERVICE] Ticket will require human approval (timeout: {approval_timeout}s)"
                )
            else:
                approval_status = "auto_approved"
                approval_requested_at = None

            # Generate unique ticket ID
            ticket_id = f"ticket-{uuid.uuid4()}"

            # Create ticket record
            ticket = Ticket(
                id=ticket_id,
                workflow_id=workflow_id,
                created_by_agent_id=agent_id,
                assigned_agent_id=assigned_agent_id,
                title=title,
                description=description,
                ticket_type=ticket_type,
                priority=priority,
                status=initial_status,
                parent_ticket_id=parent_ticket_id,
                task_id=task_id,
                phase_id=phase_id,
                blocked_by_ticket_ids=blocked_by_ticket_ids,
                tags=tags,
                related_task_ids=related_task_ids,
                related_ticket_ids=[],  # Will be populated later
                embedding=None,  # Will be added in Wave 2
                embedding_id=None,
                is_resolved=False,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                # Human approval fields
                approval_status=approval_status,
                approval_requested_at=approval_requested_at,
                approval_decided_at=None,
                approval_decided_by=None,
                rejection_reason=None,
            )

            db.add(ticket)
            logger.info(f"[TICKET_SERVICE] Ticket object created: {ticket_id}")
            db.flush()  # Flush to get the ID for history
            logger.info("[TICKET_SERVICE] db.flush() completed")

            # Record creation in ticket_history
            logger.info("[TICKET_SERVICE] Recording ticket history...")
            await TicketHistoryService.record_change(
                ticket_id=ticket_id,
                agent_id=agent_id,
                change_type="created",
                old_value=None,
                new_value=initial_status,
                metadata={
                    "title": title,
                    "ticket_type": ticket_type,
                    "priority": priority,
                },
                db=db,
            )
            logger.info("[TICKET_SERVICE] History recorded")

            logger.info("[TICKET_SERVICE] Committing transaction...")
            db.commit()
            logger.info(
                "[TICKET_SERVICE] ✅ Transaction committed - ticket saved to database"
            )

        # If human review required, wait for approval
        if human_review_enabled:
            await TicketService._wait_for_ticket_approval(
                ticket_id, workflow_id, title, approval_timeout
            )

        # Generate embedding and store in Qdrant (outside transaction)
        embedding_created, similar_tickets = await TicketService._index_new_ticket(
            ticket_id=ticket_id,
            title=title,
            description=description,
            workflow_id=workflow_id,
            ticket_type=ticket_type,
            priority=priority,
            status=initial_status,
            tags=tags,
            agent_id=agent_id,
            assigned_agent_id=assigned_agent_id,
            blocked_by_ticket_ids=blocked_by_ticket_ids,
        )

        result = {
            "success": True,
            "ticket_id": ticket_id,
            "status": initial_status,
            "message": "Ticket created successfully",
            "embedding_created": embedding_created,
            "similar_tickets": similar_tickets,
        }
        logger.info(f"[TICKET_SERVICE] Returning result: {result}")
        logger.info("[TICKET_SERVICE] ========== SUCCESS ==========")
        return result

    @staticmethod
    async def update_ticket(
        ticket_id: str,
        agent_id: str,
        updates: Dict[str, Any],
        update_comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update ticket fields (excluding status changes).

        Args:
            ticket_id: ID of the ticket to update
            agent_id: ID of the agent making the update
            updates: Dictionary of fields to update
            update_comment: Optional comment explaining changes

        Returns:
            Dictionary with update status and fields updated

        Raises:
            ValueError: If validation fails
        """
        # Allowed fields for update
        allowed_fields = [
            "title",
            "description",
            "priority",
            "assigned_agent_id",
            "ticket_type",
            "tags",
            "blocked_by_ticket_ids",
        ]

        with get_db() as db:
            # Validate ticket exists
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            if not ticket:
                raise ValueError(f"Ticket not found: {ticket_id}")

            # Validate agent exists
            agent = db.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                raise ValueError(f"Agent not found: {agent_id}")

            fields_updated = []

            # Process each update
            for field, new_value in updates.items():
                if field not in allowed_fields:
                    raise ValueError(
                        f"Field '{field}' cannot be updated. Allowed fields: {allowed_fields}"
                    )

                # Get old value
                old_value = getattr(ticket, field)

                # Special validation for blocked_by_ticket_ids
                if field == "blocked_by_ticket_ids" and new_value:
                    # Check for circular blocking
                    TicketService._check_circular_blocking(ticket_id, new_value, db)

                # Update field
                setattr(ticket, field, new_value)
                fields_updated.append(field)

                # Create history entry for each field
                await TicketHistoryService.record_change(
                    ticket_id=ticket_id,
                    agent_id=agent_id,
                    change_type="field_updated",
                    old_value=json.dumps(old_value) if old_value else None,
                    new_value=json.dumps(new_value) if new_value else None,
                    metadata={"field_name": field},
                    db=db,
                )

            # Update timestamp
            ticket.updated_at = datetime.utcnow()

            # If update_comment provided, create comment
            if update_comment:
                comment_id = f"comment-{uuid.uuid4()}"
                comment = TicketComment(
                    id=comment_id,
                    ticket_id=ticket_id,
                    agent_id=agent_id,
                    comment_text=update_comment,
                    comment_type="general",
                    created_at=datetime.utcnow(),
                )
                db.add(comment)

                await TicketHistoryService.record_change(
                    ticket_id=ticket_id,
                    agent_id=agent_id,
                    change_type="commented",
                    old_value=None,
                    new_value=update_comment,
                    metadata={"comment_id": comment_id},
                    db=db,
                )

            db.commit()

        # If title or description changed, regenerate embedding
        embedding_updated = False
        if "title" in fields_updated or "description" in fields_updated:
            try:
                await TicketSearchService.reindex_ticket(ticket_id)
                embedding_updated = True
                logger.info(f"Regenerated embedding for ticket {ticket_id}")
            except Exception as e:
                logger.error(
                    f"Failed to regenerate embedding for ticket {ticket_id}: {e}"
                )

        return {
            "success": True,
            "ticket_id": ticket_id,
            "fields_updated": fields_updated,
            "message": f"Updated {len(fields_updated)} field(s)",
            "embedding_updated": embedding_updated,
        }

    @staticmethod
    async def change_status(
        ticket_id: str,
        agent_id: str,
        new_status: str,
        comment: str,
        commit_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Move ticket to a different status column.

        Args:
            ticket_id: ID of the ticket
            agent_id: ID of the agent making the change
            new_status: New status to move to
            comment: Required comment explaining status change
            commit_sha: Optional commit SHA to link

        Returns:
            Dictionary with status change details

        Raises:
            ValueError: If validation fails or ticket is blocked
        """
        with get_db() as db:
            # Validate ticket exists
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            if not ticket:
                raise ValueError(f"Ticket not found: {ticket_id}")

            # Get board config to validate new_status
            board_config = (
                db.query(BoardConfig).filter_by(workflow_id=ticket.workflow_id).first()
            )
            if not board_config:
                raise ValueError(
                    f"Board configuration not found for workflow: {ticket.workflow_id}"
                )

            # Validate new_status is valid per board_config
            valid_statuses = [
                col["id"] if isinstance(col, dict) else col
                for col in board_config.columns
            ]
            if new_status not in valid_statuses:
                raise ValueError(
                    f"Invalid status '{new_status}'. Valid statuses: {valid_statuses}"
                )

            # CRITICAL: Check if ticket is blocked
            if ticket.blocked_by_ticket_ids and len(ticket.blocked_by_ticket_ids) > 0:
                # Get blocking ticket titles for clearer error message
                blocking_tickets = (
                    db.query(Ticket)
                    .filter(Ticket.id.in_(ticket.blocked_by_ticket_ids))
                    .all()
                )
                blocking_titles = [f"{t.id}: {t.title}" for t in blocking_tickets]

                error_message = (
                    f"Cannot change status: Ticket is blocked by {len(blocking_titles)} ticket(s): "
                    f"{', '.join(blocking_titles[:3])}"
                )
                if len(blocking_titles) > 3:
                    error_message += f" and {len(blocking_titles) - 3} more"

                return {
                    "success": False,
                    "ticket_id": ticket_id,
                    "old_status": ticket.status,
                    "new_status": ticket.status,  # Unchanged
                    "message": error_message,
                    "blocked": True,
                    "blocking_ticket_ids": ticket.blocked_by_ticket_ids,
                    "blocking_tickets": blocking_titles,
                }

            # Store old status
            old_status = ticket.status

            # Update ticket status
            ticket.status = new_status
            ticket.updated_at = datetime.utcnow()

            # Update timing fields based on status
            # This is configurable - here we use simple rules
            if new_status == board_config.initial_status:
                # Reset if moved back to initial
                ticket.started_at = None
                ticket.completed_at = None
            elif (
                ticket.started_at is None and new_status != board_config.initial_status
            ):
                # Mark as started if moving from initial status
                ticket.started_at = datetime.utcnow()

            # Check if this is a completion status (last column)
            columns = board_config.columns
            last_column_id = (
                columns[-1]["id"] if isinstance(columns[-1], dict) else columns[-1]
            )
            if new_status == last_column_id:
                ticket.completed_at = datetime.utcnow()
                ticket.is_resolved = True
                ticket.resolved_at = datetime.utcnow()
            elif ticket.is_resolved and new_status != last_column_id:
                # Reopening a resolved ticket — clear resolution
                ticket.is_resolved = False
                ticket.resolved_at = None

            # Create status change comment automatically
            status_comment_id = f"comment-{uuid.uuid4()}"
            status_comment = TicketComment(
                id=status_comment_id,
                ticket_id=ticket_id,
                agent_id=agent_id,
                comment_text=comment,
                comment_type="status_change",
                created_at=datetime.utcnow(),
            )
            db.add(status_comment)

            # Record in ticket_history
            await TicketHistoryService.record_status_transition(
                ticket_id=ticket_id,
                agent_id=agent_id,
                from_status=old_status,
                to_status=new_status,
                db=db,
            )

            # If commit_sha provided, link commit
            if commit_sha:
                await TicketService.link_commit(
                    ticket_id=ticket_id,
                    agent_id=agent_id,
                    commit_sha=commit_sha,
                    commit_message=f"Status change: {old_status} -> {new_status}",
                    link_method="status_change",
                    db=db,
                )

            db.commit()

            return {
                "success": True,
                "ticket_id": ticket_id,
                "old_status": old_status,
                "new_status": new_status,
                "message": f"Status changed from {old_status} to {new_status}",
                "blocked": False,
                "blocking_ticket_ids": [],
            }

    @staticmethod
    async def add_comment(
        ticket_id: str,
        agent_id: str,
        comment_text: str,
        comment_type: str = "general",
        mentions: List[str] = None,
        attachments: List[str] = None,
    ) -> Dict[str, Any]:
        """
        Add a comment to a ticket.

        Args:
            ticket_id: ID of the ticket
            agent_id: ID of the agent adding comment
            comment_text: The comment text
            comment_type: Type of comment (general, status_change, blocker, resolution)
            mentions: List of mentioned agent/ticket IDs
            attachments: List of file paths

        Returns:
            Dictionary with comment details

        Raises:
            ValueError: If validation fails
        """
        mentions = mentions or []
        attachments = attachments or []

        with get_db() as db:
            # Validate ticket exists
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            if not ticket:
                raise ValueError(f"Ticket not found: {ticket_id}")

            # Validate agent exists
            agent = db.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                raise ValueError(f"Agent not found: {agent_id}")

            # Create comment record
            comment_id = f"comment-{uuid.uuid4()}"
            comment = TicketComment(
                id=comment_id,
                ticket_id=ticket_id,
                agent_id=agent_id,
                comment_text=comment_text,
                comment_type=comment_type,
                mentions=mentions,
                attachments=attachments,
                created_at=datetime.utcnow(),
            )

            db.add(comment)

            # Record in ticket_history
            await TicketHistoryService.record_change(
                ticket_id=ticket_id,
                agent_id=agent_id,
                change_type="commented",
                old_value=None,
                new_value=comment_text,
                metadata={
                    "comment_id": comment_id,
                    "comment_type": comment_type,
                    "mentions": mentions,
                },
                db=db,
            )

            # Update ticket timestamp
            ticket.updated_at = datetime.utcnow()

            # Check comment count - every 5 comments, reindex ticket
            comment_count = (
                db.query(TicketComment).filter_by(ticket_id=ticket_id).count()
            )

            db.commit()

        # Every 5 comments, reindex ticket with comments included
        if comment_count % 5 == 0:
            try:
                await TicketSearchService.reindex_ticket(ticket_id)
                logger.info(
                    f"Reindexed ticket {ticket_id} after {comment_count} comments"
                )
            except Exception as e:
                logger.error(f"Failed to reindex ticket {ticket_id}: {e}")

        return {
            "success": True,
            "comment_id": comment_id,
            "ticket_id": ticket_id,
            "message": "Comment added successfully",
        }

    @staticmethod
    async def get_ticket(ticket_id: str) -> Optional[Dict[str, Any]]:
        """
        Get full ticket details including comments and history.

        Args:
            ticket_id: ID of the ticket

        Returns:
            Dictionary with full ticket details or None if not found
        """
        with get_db() as db:
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            if not ticket:
                return None

            # Get comments
            comments = (
                db.query(TicketComment)
                .filter_by(ticket_id=ticket_id)
                .order_by(TicketComment.created_at)
                .all()
            )

            # Get history
            history = (
                db.query(TicketHistory)
                .filter_by(ticket_id=ticket_id)
                .order_by(TicketHistory.changed_at)
                .all()
            )

            # Get commits
            commits = (
                db.query(TicketCommit)
                .filter_by(ticket_id=ticket_id)
                .order_by(TicketCommit.commit_timestamp)
                .all()
            )

            # Get all tasks that reference this ticket
            related_tasks = db.query(Task).filter_by(ticket_id=ticket_id).all()
            related_task_ids = [task.id for task in related_tasks]

            # Find all tickets that are blocked by this ticket
            # Query for tickets where blocked_by_ticket_ids contains this ticket_id
            all_tickets = (
                db.query(Ticket).filter(Ticket.workflow_id == ticket.workflow_id).all()
            )
            blocks_ticket_ids = []
            for t in all_tickets:
                if t.blocked_by_ticket_ids and ticket_id in t.blocked_by_ticket_ids:
                    blocks_ticket_ids.append(t.id)

            # Frontend expects structure: {ticket: {...}, comments: [], history: [], commits: []}
            ticket_data = {
                "id": ticket.id,
                "ticket_id": ticket.id,
                "workflow_id": ticket.workflow_id,
                "title": ticket.title,
                "description": ticket.description,
                "ticket_type": ticket.ticket_type,
                "priority": ticket.priority,
                "status": ticket.status,
                "approval_status": ticket.approval_status,  # For human review workflow
                "created_by_agent_id": ticket.created_by_agent_id,
                "assigned_agent_id": ticket.assigned_agent_id,
                "created_at": ticket.created_at.isoformat() + "Z",
                "updated_at": ticket.updated_at.isoformat() + "Z",
                "started_at": ticket.started_at.isoformat() + "Z"
                if ticket.started_at
                else None,
                "completed_at": ticket.completed_at.isoformat() + "Z"
                if ticket.completed_at
                else None,
                "parent_ticket_id": ticket.parent_ticket_id,
                "related_task_ids": related_task_ids,  # Dynamically fetched from tasks
                "related_ticket_ids": ticket.related_ticket_ids or [],
                "tags": ticket.tags or [],
                "blocked_by_ticket_ids": ticket.blocked_by_ticket_ids or [],
                "blocks_ticket_ids": blocks_ticket_ids,  # Dynamically computed reverse relationship
                "is_blocked": bool(
                    ticket.blocked_by_ticket_ids
                    and len(ticket.blocked_by_ticket_ids) > 0
                ),
                "is_resolved": ticket.is_resolved,
                "resolved_at": ticket.resolved_at.isoformat() + "Z"
                if ticket.resolved_at
                else None,
                "comment_count": len(comments),
                "commit_count": len(commits),
            }

            comments_data = [
                {
                    "id": c.id,
                    "ticket_id": ticket.id,
                    "agent_id": c.agent_id,
                    "comment_text": c.comment_text,
                    "comment_type": c.comment_type,
                    "created_at": c.created_at.isoformat() + "Z",
                    "updated_at": c.updated_at.isoformat() + "Z"
                    if c.updated_at
                    else None,
                    "is_edited": c.is_edited if c.is_edited is not None else False,
                    "mentions": c.mentions or [],
                    "attachments": c.attachments or [],
                }
                for c in comments
            ]

            history_data = [
                {
                    "id": h.id,
                    "ticket_id": ticket.id,
                    "agent_id": h.agent_id,
                    "change_type": h.change_type,
                    "field_name": h.field_name,
                    "old_value": h.old_value,
                    "new_value": h.new_value,
                    "change_description": h.change_description,
                    "metadata": h.change_metadata,
                    "changed_at": h.changed_at.isoformat() + "Z",
                }
                for h in history
            ]

            commits_data = [
                {
                    "commit_sha": c.commit_sha,
                    "commit_message": c.commit_message,
                    "commit_timestamp": c.commit_timestamp.isoformat() + "Z",
                    "files_changed": c.files_changed,
                    "insertions": c.insertions,
                    "deletions": c.deletions,
                    "files_list": c.files_list or [],
                    "agent_id": c.agent_id,
                    "link_method": c.link_method,
                }
                for c in commits
            ]

            return {
                "ticket": ticket_data,
                "comments": comments_data,
                "history": history_data,
                "commits": commits_data,
            }

    @staticmethod
    async def get_tickets_by_workflow(
        workflow_id: str, filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all tickets for a workflow with optional filtering.

        Args:
            workflow_id: ID of the workflow
            filters: Optional filters (status, priority, assigned_agent_id, etc.)

        Returns:
            List of ticket dictionaries
        """
        filters = filters or {}

        with get_db() as db:
            query = db.query(Ticket).filter_by(workflow_id=workflow_id)

            # Apply filters
            if "status" in filters:
                query = query.filter(Ticket.status == filters["status"])
            if "priority" in filters:
                query = query.filter(Ticket.priority == filters["priority"])
            if "assigned_agent_id" in filters:
                query = query.filter(
                    Ticket.assigned_agent_id == filters["assigned_agent_id"]
                )
            if "ticket_type" in filters:
                query = query.filter(Ticket.ticket_type == filters["ticket_type"])
            if "is_resolved" in filters:
                query = query.filter(Ticket.is_resolved == filters["is_resolved"])

            tickets = query.order_by(Ticket.created_at.desc()).all()

            return [
                {
                    "id": t.id,
                    "workflow_id": t.workflow_id,
                    "ticket_id": t.id,  # For backwards compatibility
                    "title": t.title,
                    "description": t.description[:200],  # Truncated
                    "ticket_type": t.ticket_type,
                    "priority": t.priority,
                    "status": t.status,
                    "approval_status": t.approval_status,  # For human review workflow
                    "created_by_agent_id": t.created_by_agent_id,
                    "assigned_agent_id": t.assigned_agent_id,
                    "created_at": t.created_at.isoformat() + "Z",
                    "updated_at": t.updated_at.isoformat() + "Z",
                    "started_at": t.started_at.isoformat() + "Z"
                    if t.started_at
                    else None,
                    "completed_at": t.completed_at.isoformat() + "Z"
                    if t.completed_at
                    else None,
                    "tags": t.tags or [],
                    "comment_count": 0,  # TODO: Query actual count
                    "commit_count": 0,  # TODO: Query actual count
                    "is_blocked": bool(
                        t.blocked_by_ticket_ids and len(t.blocked_by_ticket_ids) > 0
                    ),
                    "blocked_by_ticket_ids": t.blocked_by_ticket_ids or [],
                    "is_resolved": t.is_resolved,
                }
                for t in tickets
            ]

    @staticmethod
    async def get_tickets_by_status(
        workflow_id: str, status: str
    ) -> List[Dict[str, Any]]:
        """
        Get all tickets with a specific status.

        Args:
            workflow_id: ID of the workflow
            status: Status to filter by

        Returns:
            List of ticket dictionaries
        """
        return await TicketService.get_tickets_by_workflow(
            workflow_id, filters={"status": status}
        )

    @staticmethod
    async def assign_ticket(ticket_id: str, agent_id: str) -> Dict[str, Any]:
        """
        Assign a ticket to an agent.

        Args:
            ticket_id: ID of the ticket
            agent_id: ID of the agent to assign to

        Returns:
            Dictionary with assignment status

        Raises:
            ValueError: If validation fails
        """
        with get_db() as db:
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            if not ticket:
                raise ValueError(f"Ticket not found: {ticket_id}")

            agent = db.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                raise ValueError(f"Agent not found: {agent_id}")

            old_agent_id = ticket.assigned_agent_id
            ticket.assigned_agent_id = agent_id
            ticket.updated_at = datetime.utcnow()

            # Record in history
            await TicketHistoryService.record_change(
                ticket_id=ticket_id,
                agent_id=agent_id,
                change_type="assigned",
                old_value=old_agent_id,
                new_value=agent_id,
                metadata=None,
                db=db,
            )

            db.commit()

            return {
                "success": True,
                "ticket_id": ticket_id,
                "assigned_agent_id": agent_id,
                "message": "Ticket assigned successfully",
            }

    @staticmethod
    def _get_commit_stats(commit_sha: str, repo_path: str) -> Dict[str, Any]:
        """
        Get commit statistics from git.

        Args:
            commit_sha: Commit SHA to analyze
            repo_path: Path to git repository

        Returns:
            Dictionary with commit stats (files_changed, insertions, deletions, files_list)
        """
        import subprocess

        try:
            # Get diff stats
            cmd = ["git", "show", "--numstat", "--format=", commit_sha]
            result = subprocess.run(
                cmd, cwd=repo_path, capture_output=True, text=True, check=True
            )

            files_changed = 0
            insertions = 0
            deletions = 0
            files_list = []

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 3:
                    adds = int(parts[0]) if parts[0].isdigit() else 0
                    dels = int(parts[1]) if parts[1].isdigit() else 0
                    file_path = parts[2]

                    insertions += adds
                    deletions += dels
                    files_changed += 1
                    files_list.append(file_path)

            return {
                "files_changed": files_changed,
                "insertions": insertions,
                "deletions": deletions,
                "files_list": files_list,
            }
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to get commit stats for {commit_sha}: {e}")
            return {
                "files_changed": 0,
                "insertions": 0,
                "deletions": 0,
                "files_list": [],
            }

    @staticmethod
    async def link_commit(
        ticket_id: str,
        agent_id: str,
        commit_sha: str,
        commit_message: str,
        link_method: str = "manual",
        db=None,
    ) -> Dict[str, Any]:
        """
        Link a git commit to a ticket.

        Args:
            ticket_id: ID of the ticket
            agent_id: ID of the agent linking the commit
            commit_sha: Git commit SHA
            commit_message: Commit message
            link_method: How the commit was linked (manual, auto_detected, worktree, status_change)
            db: optional existing session to participate in the caller's
                own transaction (change_status/resolve_ticket) instead of
                opening a separate one that commits independently.
                Without this, an exception in the caller's own transaction
                *after* this call returns leaves a permanently-persisted
                commit link + history row pointing at a ticket status
                change that itself rolled back -- the two were never
                actually atomic despite reading that way at the call site.

        Returns:
            Dictionary with link status

        Raises:
            ValueError: If validation fails
        """
        if db is not None:
            return await TicketService._link_commit_impl(
                db, ticket_id, agent_id, commit_sha, commit_message, link_method
            )
        with get_db() as db:
            return await TicketService._link_commit_impl(
                db, ticket_id, agent_id, commit_sha, commit_message, link_method
            )

    @staticmethod
    async def _link_commit_impl(
        db,
        ticket_id: str,
        agent_id: str,
        commit_sha: str,
        commit_message: str,
        link_method: str,
    ) -> Dict[str, Any]:
        from src.core.simple_config import get_config

        ticket = db.query(Ticket).filter_by(id=ticket_id).first()
        if not ticket:
            raise ValueError(f"Ticket not found: {ticket_id}")

        # Check if commit already linked
        existing = (
            db.query(TicketCommit)
            .filter_by(ticket_id=ticket_id, commit_sha=commit_sha)
            .first()
        )
        if existing:
            return {
                "success": True,
                "ticket_id": ticket_id,
                "commit_sha": commit_sha,
                "message": "Commit already linked to this ticket",
            }

        # Get real commit stats from git -- resolve the ticket's own
        # project repo rather than assuming it's whichever project the
        # process-wide singleton currently points at (only one project
        # could ever be active before multi-project concurrency).
        main_repo_path = None
        if ticket.workflow_id:
            wf = db.query(Workflow).filter_by(id=ticket.workflow_id).first()
            if wf and wf.project_id:
                from src.core.database import AutopilotProject

                proj = db.query(AutopilotProject).filter_by(id=wf.project_id).first()
                if proj and proj.base_dir:
                    main_repo_path = proj.base_dir
        if main_repo_path is None:
            config = get_config()
            main_repo_path = str(config.git.main_repo_path)
        # _get_commit_stats shells out to `git show --numstat` --
        # blocking, offloaded so it doesn't stall the event loop.
        loop = asyncio.get_event_loop()
        commit_stats = await loop.run_in_executor(
            None, TicketService._get_commit_stats, commit_sha, main_repo_path
        )

        # Create commit link with real stats
        commit_id = f"tc-{uuid.uuid4()}"
        ticket_commit = TicketCommit(
            id=commit_id,
            ticket_id=ticket_id,
            agent_id=agent_id,
            commit_sha=commit_sha,
            commit_message=commit_message,
            commit_timestamp=datetime.utcnow(),
            link_method=link_method,
            files_changed=commit_stats["files_changed"],
            insertions=commit_stats["insertions"],
            deletions=commit_stats["deletions"],
            files_list=commit_stats["files_list"],
        )

        db.add(ticket_commit)

        # Record in history
        await TicketHistoryService.link_commit(
            ticket_id=ticket_id,
            commit_sha=commit_sha,
            message=commit_message,
            db=db,
        )

        return {
            "success": True,
            "ticket_id": ticket_id,
            "commit_sha": commit_sha,
                "message": "Commit linked successfully",
            }

    @staticmethod
    async def resolve_ticket(
        ticket_id: str,
        agent_id: str,
        resolution_comment: str,
        commit_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Mark ticket as resolved and automatically unblock dependent tickets.

        Args:
            ticket_id: ID of the ticket to resolve
            agent_id: ID of the agent resolving the ticket
            resolution_comment: Comment explaining resolution
            commit_sha: Optional commit SHA that resolved the ticket

        Returns:
            Dictionary with resolution status and unblocked ticket IDs

        Raises:
            ValueError: If validation fails
        """
        logger.info(f"Resolving ticket {ticket_id} by agent {agent_id}")

        with get_db() as db:
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            if not ticket:
                raise ValueError(f"Ticket not found: {ticket_id}")

            # Set is_resolved = True and resolved_at = now()
            ticket.is_resolved = True
            ticket.resolved_at = datetime.utcnow()
            ticket.updated_at = datetime.utcnow()

            # Add resolution comment
            comment_id = f"comment-{uuid.uuid4()}"
            comment = TicketComment(
                id=comment_id,
                ticket_id=ticket_id,
                agent_id=agent_id,
                comment_text=resolution_comment,
                comment_type="resolution",
                created_at=datetime.utcnow(),
            )
            db.add(comment)

            # If commit_sha provided, link commit
            if commit_sha:
                await TicketService.link_commit(
                    ticket_id=ticket_id,
                    agent_id=agent_id,
                    commit_sha=commit_sha,
                    commit_message="Ticket resolution",
                    link_method="resolution",
                    db=db,
                )

            # Find all tickets blocked by this ticket
            # Query for tickets where blocked_by_ticket_ids contains this ticket_id
            all_tickets = (
                db.query(Ticket).filter(Ticket.workflow_id == ticket.workflow_id).all()
            )

            unblocked_ticket_ids = []

            for dependent_ticket in all_tickets:
                if (
                    dependent_ticket.blocked_by_ticket_ids
                    and ticket_id in dependent_ticket.blocked_by_ticket_ids
                ):
                    # Remove this ticket_id from their blocked_by_ticket_ids
                    # Need to create a new list to trigger SQLAlchemy's change tracking
                    new_blocked_list = [
                        tid
                        for tid in dependent_ticket.blocked_by_ticket_ids
                        if tid != ticket_id
                    ]
                    dependent_ticket.blocked_by_ticket_ids = new_blocked_list
                    dependent_ticket.updated_at = datetime.utcnow()

                    # Add comment to each unblocked ticket
                    unblock_comment_id = f"comment-{uuid.uuid4()}"
                    unblock_comment = TicketComment(
                        id=unblock_comment_id,
                        ticket_id=dependent_ticket.id,
                        agent_id=agent_id,
                        comment_text=f"Unblocked - {ticket_id} was resolved",
                        comment_type="blocker",
                        created_at=datetime.utcnow(),
                    )
                    db.add(unblock_comment)

                    # Record in history
                    await TicketHistoryService.record_change(
                        ticket_id=dependent_ticket.id,
                        agent_id=agent_id,
                        change_type="unblocked",
                        old_value=json.dumps([ticket_id]),
                        new_value=None,
                        metadata={"resolved_ticket_id": ticket_id},
                        db=db,
                    )

                    unblocked_ticket_ids.append(dependent_ticket.id)

            # Record resolution in history
            await TicketHistoryService.record_change(
                ticket_id=ticket_id,
                agent_id=agent_id,
                change_type="resolved",
                old_value="False",
                new_value="True",
                metadata={
                    "resolution_comment": resolution_comment,
                    "unblocked_tickets": unblocked_ticket_ids,
                },
                db=db,
            )

            db.commit()

            logger.info(
                f"Resolved ticket {ticket_id}, unblocking {len(unblocked_ticket_ids)} dependent tickets: "
                f"{unblocked_ticket_ids}"
            )

        # Unblock tasks associated with unblocked tickets (outside transaction)
        unblocked_task_ids = []
        if unblocked_ticket_ids:
            from src.services.task_blocking_service import TaskBlockingService

            logger.info(
                f"Checking for tasks to unblock for tickets: {unblocked_ticket_ids}"
            )

            with get_db() as db:
                # Find all tasks associated with the unblocked tickets
                tasks_to_check = (
                    db.query(Task)
                    .filter(
                        Task.ticket_id.in_(unblocked_ticket_ids),
                        Task.status == "blocked",
                    )
                    .all()
                )

                for task in tasks_to_check:
                    try:
                        # BUG FIX: Only unblock task if ALL blocking tickets are resolved
                        # Get the task's ticket and check if it still has blockers
                        ticket = db.query(Ticket).filter_by(id=task.ticket_id).first()
                        if not ticket:
                            logger.warning(
                                f"Task {task.id} references non-existent ticket {task.ticket_id}"
                            )
                            continue

                        # Check if ticket still has blocking dependencies
                        if (
                            ticket.blocked_by_ticket_ids
                            and len(ticket.blocked_by_ticket_ids) > 0
                        ):
                            # Ticket still has other blockers - don't unblock the task yet
                            remaining_blockers = ticket.blocked_by_ticket_ids
                            logger.info(
                                f"Task {task.id} still blocked - ticket {task.ticket_id} has {len(remaining_blockers)} "
                                f"remaining blocker(s): {remaining_blockers}"
                            )
                            continue

                        # All blockers resolved - safe to unblock the task
                        result = TaskBlockingService.unblock_task(task.id)
                        if result["success"]:
                            unblocked_task_ids.append(task.id)
                            logger.info(
                                f"Unblocked task {task.id} (ticket {task.ticket_id}) - ALL blockers resolved"
                            )
                    except Exception as e:
                        logger.error(f"Failed to unblock task {task.id}: {e}")

            logger.info(
                f"Unblocked {len(unblocked_task_ids)} tasks: {unblocked_task_ids}"
            )

        return {
            "success": True,
            "ticket_id": ticket_id,
            "message": f"Ticket resolved. Unblocked {len(unblocked_ticket_ids)} ticket(s) and {len(unblocked_task_ids)} task(s)",
            "unblocked_tickets": unblocked_ticket_ids,
            "unblocked_tasks": unblocked_task_ids,
        }

    @staticmethod
    async def approve_ticket(
        ticket_id: str,
        approved_by: str,
    ) -> Dict[str, Any]:
        """
        Approve a pending ticket.

        Args:
            ticket_id: ID of the ticket to approve
            approved_by: User/agent approving the ticket

        Returns:
            Dictionary with approval status

        Raises:
            ValueError: If ticket not found or not pending review
        """
        logger.info(f"[TICKET_SERVICE] Approving ticket {ticket_id} by {approved_by}")

        with get_db() as db:
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            if not ticket:
                raise ValueError(f"Ticket not found: {ticket_id}")

            if ticket.approval_status != "pending_review":
                raise ValueError(
                    f"Ticket is not pending review (current status: {ticket.approval_status})"
                )

            # Record who approved it (will be updated to 'approved' by create_ticket flow)
            ticket.approval_decided_by = approved_by
            db.commit()

        # Record decision in approval manager to wake up waiting coroutine
        approval_manager.record_decision(ticket_id, approved=True, reason="")

        logger.info(f"[TICKET_SERVICE] ✅ Ticket {ticket_id} approved by {approved_by}")

        return {
            "success": True,
            "ticket_id": ticket_id,
            "message": "Ticket approved",
        }

    @staticmethod
    async def reject_ticket(
        ticket_id: str,
        rejected_by: str,
        rejection_reason: str,
    ) -> Dict[str, Any]:
        """
        Reject a pending ticket.

        Args:
            ticket_id: ID of the ticket to reject
            rejected_by: User/agent rejecting the ticket
            rejection_reason: Reason for rejection

        Returns:
            Dictionary with rejection status

        Raises:
            ValueError: If ticket not found or not pending review
        """
        logger.info(
            f"[TICKET_SERVICE] Rejecting ticket {ticket_id} by {rejected_by}: {rejection_reason}"
        )

        with get_db() as db:
            ticket = db.query(Ticket).filter_by(id=ticket_id).first()
            if not ticket:
                raise ValueError(f"Ticket not found: {ticket_id}")

            if ticket.approval_status != "pending_review":
                raise ValueError(
                    f"Ticket is not pending review (current status: {ticket.approval_status})"
                )

            # Record rejection info (will be deleted by create_ticket flow)
            ticket.approval_decided_by = rejected_by
            ticket.rejection_reason = rejection_reason
            db.commit()

        # Record decision in approval manager to wake up waiting coroutine
        approval_manager.record_decision(
            ticket_id, approved=False, reason=rejection_reason
        )

        logger.info(f"[TICKET_SERVICE] ❌ Ticket {ticket_id} rejected by {rejected_by}")

        return {
            "success": True,
            "ticket_id": ticket_id,
            "message": "Ticket rejected",
        }

    @staticmethod
    def get_pending_review_count() -> int:
        """Get count of tickets pending human review."""
        return approval_manager.get_pending_count()

    @staticmethod
    def get_pending_review_tickets() -> List[str]:
        """Get list of ticket IDs pending human review."""
        return approval_manager.get_pending_ticket_ids()
