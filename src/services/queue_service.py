"""Queue service for managing agent concurrency and task queueing."""

import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import and_

from src.core.database import Agent, DatabaseManager, Phase, Task, Workflow

logger = logging.getLogger(__name__)


class QueueService:
    """Manages task queueing and agent concurrency limits."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        max_concurrent_agents: int,
        cli_model_concurrency_limits: Optional[Dict[str, int]] = None,
        default_cli_tool: Optional[str] = None,
        default_cli_model: Optional[str] = None,
        cli_model_fallback: Optional[str] = None,
        secondary_cli_model_fallback: Optional[str] = None,
    ):
        """Initialize queue service.

        Args:
            db_manager: Database manager instance
            max_concurrent_agents: Maximum number of agents that can run concurrently
            cli_model_concurrency_limits: Per-(cli_tool, cli_model) concurrency
                cap, keyed by "cli_tool/cli_model" (e.g. a local model with a
                single inference slot: {"pi/Qwen3.6-27B-UD-Q4_K_XL.gguf": 1}).
                A task whose phase would resolve to a combo already at its
                limit is dispatched on that cli_tool's configured fallback
                MODEL instead (same CLI, different model -- e.g. pi staying
                pi but Qwen -> mimo-v2.5-pro), same as
                CLIAgentInterface.fallback_model's in-session switch target,
                just applied at dispatch time instead of mid-session. Only
                skipped over (not dequeued) if no fallback is configured, or
                the fallback combo is itself at its own configured limit.
                Distinct from max_concurrent_agents, which caps total agents
                regardless of which CLI/model they're on. Unset/empty = no
                per-cli/model limits (original behavior).
            default_cli_tool: Mirrors agents.default_cli_tool -- needed here
                to resolve a queued task's phase to the same cli_type/model
                AgentManager.create_agent_for_task would dispatch it as,
                without actually creating the agent.
            default_cli_model: Mirrors agents.cli_model (the primary tier's
                global default model).
            cli_model_fallback: Mirrors agents.cli_model_fallback -- the
                fallback model for whichever CLI is currently default_cli_tool
                (the "primary" tier, in CLIAgentInterface.fallback_model's
                role-based terms).
            secondary_cli_model_fallback: Mirrors agents.secondary_cli_model_fallback
                -- the fallback model for the non-default (secondary) CLI tier.
        """
        self.db_manager = db_manager
        self.max_concurrent_agents = max_concurrent_agents
        self.cli_model_concurrency_limits = cli_model_concurrency_limits or {}
        self.default_cli_tool = default_cli_tool
        self.default_cli_model = default_cli_model
        self.cli_model_fallback = cli_model_fallback
        self.secondary_cli_model_fallback = secondary_cli_model_fallback
        logger.info(
            f"QueueService initialized with max_concurrent_agents={max_concurrent_agents}, "
            f"cli_model_concurrency_limits={self.cli_model_concurrency_limits}"
        )

    def _resolve_fallback_model(self, cli_type: str) -> Optional[str]:
        """The configured fallback model for cli_type, resolved by ROLE
        (primary vs secondary tier) -- mirrors
        CLIAgentInterface.fallback_model's own role lookup exactly, since
        this is used to pick the SAME target that mechanism would switch an
        already-running agent to mid-session, just applied at dispatch time
        instead."""
        is_primary = cli_type == self.default_cli_tool
        return self.cli_model_fallback if is_primary else self.secondary_cli_model_fallback

    def get_active_agent_count(self, project_id: Optional[str] = None) -> int:
        """Get count of currently active agents (not terminated).

        Args:
            project_id: When given, count only agents whose current task
                belongs to a workflow of this project. When omitted, counts
                globally across every project (original behavior, kept for
                callers not yet updated).

        Returns:
            Number of active agents
        """
        with self.db_manager.session_scope() as session:
            query = session.query(Agent).filter(
                Agent.status.in_(["working", "starting", "idle"])
            )
            if project_id:
                query = query.join(Task, Agent.current_task_id == Task.id).join(
                    Workflow, Task.workflow_id == Workflow.id
                ).filter(Workflow.project_id == project_id)
            count = query.count()
            logger.debug(f"Active agent count: {count} (project_id={project_id})")
            return count

    def _resolve_cli_and_model(self, session, task: Task) -> tuple:
        """What AgentManager.create_agent_for_task would resolve task's
        cli_type/model to, without creating an agent. Mirrors that method's
        own phase-then-global-default resolution (manager.py's
        `cli_type = phase_cli_tool or cli_type or self.config.default_cli_tool`
        and its `model = (phase_cli_model if phase_cli_tool else None) or
        global_model or cli_agent.default_model`) -- deliberately not the
        explicit `cli_type=` override create_agent_for_task also accepts,
        since that's only ever passed by the session-limit fallback
        redispatch mid-run, never for a fresh queue dispatch like this.
        """
        from src.interfaces.cli_interface import get_cli_agent

        phase = session.query(Phase).filter_by(id=task.phase_id).first() if task.phase_id else None
        phase_cli_tool = getattr(phase, "cli_tool", None) if phase else None
        phase_cli_model = getattr(phase, "cli_model", None) if phase else None
        cli_type = phase_cli_tool or self.default_cli_tool
        if not cli_type:
            return None, None
        cli_agent = get_cli_agent(cli_type)
        global_model = self.default_cli_model if cli_type == self.default_cli_tool else None
        model = (phase_cli_model if phase_cli_tool else None) or global_model or cli_agent.default_model
        return cli_type, model

    def get_active_agent_count_for_cli_model(self, cli_type: str, model: str) -> int:
        """Count of currently active agents on this exact (cli_type, model)
        combo -- the budget a per-cli/model concurrency limit is checked
        against, e.g. a local model with a single inference slot."""
        with self.db_manager.session_scope() as session:
            return (
                session.query(Agent)
                .filter(
                    Agent.status.in_(["working", "starting", "idle"]),
                    Agent.cli_type == cli_type,
                    Agent.cli_model == model,
                )
                .count()
            )

    def should_queue_task(self, project_id: Optional[str] = None) -> bool:
        """Check if we should queue the next task instead of creating an agent.

        Args:
            project_id: When given, checks that project's own agent count
                against max_concurrent_agents -- each project gets its own
                independent budget instead of sharing one global cap, so
                one project's queue depth can't starve another's. When
                omitted, checks the global count (original behavior).

        Returns:
            True if we've reached the concurrent agent limit, False otherwise
        """
        active_count = self.get_active_agent_count(project_id)
        should_queue = active_count >= self.max_concurrent_agents
        logger.debug(
            f"Should queue: {should_queue} (active={active_count}, max={self.max_concurrent_agents}, project_id={project_id})"
        )
        return should_queue

    def enqueue_task(self, task_id: str) -> None:
        """Mark a task as queued (or blocked if ticket is blocked).

        Args:
            task_id: ID of the task to enqueue
        """
        with self.db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                logger.error(f"Task {task_id} not found for enqueueing")
                return

            # Check if task's ticket is blocked
            if task.ticket_id:
                from src.services.task_blocking_service import TaskBlockingService

                blocking_info = TaskBlockingService.check_task_blocked(task_id)

                if blocking_info["is_blocked"]:
                    # Task's ticket is blocked - set status to 'blocked' instead of 'queued'
                    task.status = "blocked"
                    task.queued_at = None  # Don't set queued_at for blocked tasks

                    blocker_titles = [
                        t["title"] for t in blocking_info["blocking_tickets"]
                    ]
                    reason = f"Blocked by tickets: {', '.join(blocker_titles)}"

                    # Store blocking reason in completion_notes
                    task.completion_notes = f"Blocked: {reason}"

                    # Explicit commit (not just relying on session_scope's
                    # exit-commit): _recalculate_queue_positions below opens
                    # its OWN session, and this shared connection needs this
                    # write durably committed before that second session
                    # queries the same rows.
                    session.commit()

                    logger.info(
                        f"Task {task_id} marked as 'blocked' (not queued) because ticket {task.ticket_id} "
                        f"is blocked by: {blocking_info['blocking_ticket_ids']}"
                    )
                    return

            # Task is not blocked - proceed with normal queueing
            task.status = "queued"
            task.queued_at = datetime.utcnow()

            session.commit()

            # Recalculate all queue positions to ensure correct ordering
            self._recalculate_queue_positions()

            # Get updated position
            with self.db_manager.session_scope() as session_refresh:
                task_refreshed = (
                    session_refresh.query(Task).filter_by(id=task_id).first()
                )
                position = task_refreshed.queue_position if task_refreshed else None
                logger.info(f"Task {task_id} queued at position {position}")

    def _calculate_queue_position(self, session, new_task: Task) -> int:
        """Calculate position in queue based on priority.

        Queue order:
        1. priority_boosted (should not exist for new tasks, but included for completeness)
        2. priority (high > medium > low)
        3. queued_at (earlier first)

        Args:
            session: Database session
            new_task: The task being queued

        Returns:
            Queue position (1-indexed)
        """
        from sqlalchemy import case, or_

        # Define priority ordering using case statement
        priority_order = case(
            (Task.priority == "high", 3),
            (Task.priority == "medium", 2),
            (Task.priority == "low", 1),
            else_=2,
        )

        new_priority_value = {"high": 3, "medium": 2, "low": 1}.get(
            new_task.priority, 2
        )

        # Count tasks ahead in the queue
        # A task is ahead if:
        # 1. It's boosted (and new task is not boosted), OR
        # 2. It has higher priority value, OR
        # 3. It has same priority value but was queued earlier
        ahead_count = (
            session.query(Task)
            .filter(
                Task.status == "queued",
                Task.id != new_task.id,
                or_(
                    # Boosted tasks are always ahead (unless new task is also boosted)
                    and_(Task.priority_boosted, not new_task.priority_boosted),
                    # Among non-boosted or both boosted: higher priority is ahead
                    and_(
                        or_(
                            and_(Task.priority_boosted, new_task.priority_boosted),
                            and_(
                                not Task.priority_boosted, not new_task.priority_boosted
                            ),
                        ),
                        priority_order > new_priority_value,
                    ),
                    # Same priority level and boost status: earlier queued_at is ahead
                    and_(
                        or_(
                            and_(Task.priority_boosted, new_task.priority_boosted),
                            and_(
                                not Task.priority_boosted, not new_task.priority_boosted
                            ),
                        ),
                        priority_order == new_priority_value,
                        Task.queued_at < new_task.queued_at,
                    ),
                ),
            )
            .count()
        )

        return ahead_count + 1

    def get_next_queued_task(self, project_id: Optional[str] = None) -> Optional[Task]:
        """Get the next task from the queue based on priority.

        Priority order:
        1. priority_boosted DESC (boosted tasks first)
        2. priority (high > medium > low)
        3. queued_at ASC (earlier first)

        Skips blocked tasks (status='blocked').

        Args:
            project_id: When given, only considers queued tasks belonging
                to this project's workflows -- required so one project's
                queue can't have its priority ordering interleaved with
                (and starved by) another project's. When omitted, considers
                every queued task globally (original behavior).

        Returns:
            Next task to process, or None if queue is empty
        """
        with self.db_manager.session_scope() as session:
            # Custom ordering using CASE for priority
            from sqlalchemy import case

            priority_order = case(
                (Task.priority == "high", 3),
                (Task.priority == "medium", 2),
                (Task.priority == "low", 1),
                else_=2,
            )

            # Get all queued tasks (excluding blocked)
            # Note: We only look at "queued" status, blocked tasks have status="blocked"
            query = session.query(Task).filter(
                Task.status
                == "queued"  # Blocked tasks have status='blocked', not 'queued'
            )
            if project_id:
                query = query.join(Workflow, Task.workflow_id == Workflow.id).filter(
                    Workflow.project_id == project_id
                )
            tasks = query.order_by(
                Task.priority_boosted.desc(),
                priority_order.desc(),
                Task.queued_at.asc(),
            ).all()

            # Filter out any tasks that shouldn't be processed
            # (additional safety check in case a task is queued but its ticket is blocked)
            for task in tasks:
                # If task has a ticket, verify it's not blocked
                if task.ticket_id:
                    from src.services.task_blocking_service import TaskBlockingService

                    blocking_info = TaskBlockingService.check_task_blocked(task.id)
                    if blocking_info["is_blocked"]:
                        logger.warning(
                            f"Task {task.id} is queued but its ticket is blocked. "
                            f"Blocked by: {blocking_info['blocking_ticket_ids']}. "
                            f"This task should have status='blocked', not 'queued'. Skipping."
                        )
                        continue

                # A configured per-cli/model concurrency limit (e.g. a local
                # model with a single inference slot) -- rather than stall
                # this task in the queue, dispatch it on that cli_tool's
                # configured fallback MODEL instead (same CLI, different
                # model -- e.g. pi staying pi but Qwen -> mimo-v2.5-pro),
                # which doesn't share the primary model's slot constraint.
                # Only skip over the task (not dequeue it) if no fallback is
                # configured, or the fallback combo is itself saturated --
                # the next-highest-priority task that fits still gets picked
                # up this same pass instead of the whole queue stalling
                # behind one combo.
                if self.cli_model_concurrency_limits:
                    cli_type, model = self._resolve_cli_and_model(session, task)
                    if cli_type and model:
                        limit = self.cli_model_concurrency_limits.get(f"{cli_type}/{model}")
                        if limit is not None:
                            active = self.get_active_agent_count_for_cli_model(cli_type, model)
                            if active >= limit:
                                fallback_model = self._resolve_fallback_model(cli_type)
                                if fallback_model and fallback_model != model:
                                    fallback_limit = self.cli_model_concurrency_limits.get(
                                        f"{cli_type}/{fallback_model}"
                                    )
                                    fallback_active = (
                                        self.get_active_agent_count_for_cli_model(cli_type, fallback_model)
                                        if fallback_limit is not None
                                        else 0
                                    )
                                    if fallback_limit is None or fallback_active < fallback_limit:
                                        logger.info(
                                            f"Task {task.id}'s primary combo {cli_type}/{model} at its "
                                            f"concurrency limit ({active}/{limit}) -- dispatching on "
                                            f"fallback model {fallback_model} instead"
                                        )
                                        task._dispatch_cli_override = (cli_type, fallback_model)
                                        return task
                                logger.debug(
                                    f"Task {task.id} would dispatch on {cli_type}/{model}, already at "
                                    f"its concurrency limit ({active}/{limit}) with no usable fallback "
                                    "-- skipping for now"
                                )
                                continue

                # Task is valid, return it
                logger.info(
                    f"Next queued task: {task.id} (priority={task.priority}, boosted={task.priority_boosted})"
                )
                return task

            logger.debug("No queued tasks found")
            return None

    def dequeue_task(self, task_id: str) -> None:
        """Remove a task from the queue (mark as assigned).

        Args:
            task_id: ID of the task to dequeue
        """
        with self.db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                logger.error(f"Task {task_id} not found for dequeueing")
                return

            if task.status != "queued":
                logger.warning(f"Task {task_id} is not queued (status={task.status})")
                return

            task.status = "assigned"
            task.queue_position = None  # Clear queue position

            # Explicit commit before _recalculate_queue_positions, which
            # opens its own session against this same shared connection.
            session.commit()

            # Update queue positions for remaining tasks
            self._recalculate_queue_positions()

            logger.info(f"Task {task_id} dequeued and marked as assigned")

    def _recalculate_queue_positions(self) -> None:
        """Recalculate queue positions for all queued tasks."""
        try:
            with self.db_manager.session_scope() as session:
                from sqlalchemy import case

                priority_order = case(
                    (Task.priority == "high", 3),
                    (Task.priority == "medium", 2),
                    (Task.priority == "low", 1),
                    else_=2,
                )

                queued_tasks = (
                    session.query(Task)
                    .filter(Task.status == "queued")
                    .order_by(
                        Task.priority_boosted.desc(),
                        priority_order.desc(),
                        Task.queued_at.asc(),
                    )
                    .all()
                )

                for position, task in enumerate(queued_tasks, start=1):
                    task.queue_position = position

                logger.debug(f"Recalculated positions for {len(queued_tasks)} queued tasks")
        except Exception as e:
            logger.error(f"Failed to recalculate queue positions: {e}")

    def get_queue_status(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Get current queue status information.

        Args:
            project_id: When given, scopes both the active-agent count and
                the queued-task list to this project. When omitted, reports
                globally (original behavior).

        Returns:
            Dictionary with queue status information
        """
        with self.db_manager.session_scope() as session:
            active_agents = self.get_active_agent_count(project_id)

            query = session.query(Task).filter(Task.status == "queued")
            if project_id:
                query = query.join(Workflow, Task.workflow_id == Workflow.id).filter(
                    Workflow.project_id == project_id
                )
            queued_tasks = query.order_by(Task.queue_position.asc()).all()

            queued_task_details = [
                {
                    "task_id": task.id,
                    "description": task.enriched_description or task.raw_description,
                    "priority": task.priority,
                    "priority_boosted": task.priority_boosted,
                    "queue_position": task.queue_position,
                    "queued_at": task.queued_at.isoformat() if task.queued_at else None,
                    "phase_id": task.phase_id,
                }
                for task in queued_tasks
            ]

            slots_available = max(0, self.max_concurrent_agents - active_agents)

            return {
                "active_agents": active_agents,
                "max_concurrent_agents": self.max_concurrent_agents,
                "queued_tasks_count": len(queued_tasks),
                "queued_tasks": queued_task_details,
                "slots_available": slots_available,
                "at_capacity": active_agents >= self.max_concurrent_agents,
            }

    def boost_task_priority(self, task_id: str) -> bool:
        """Boost a task's priority to bypass the queue.

        Args:
            task_id: ID of the task to boost

        Returns:
            True if successful, False otherwise
        """
        try:
            with self.db_manager.session_scope() as session:
                task = session.query(Task).filter_by(id=task_id).first()
                if not task:
                    logger.error(f"Task {task_id} not found for priority boost")
                    return False

                if task.status != "queued":
                    logger.warning(
                        f"Cannot boost task {task_id} - not queued (status={task.status})"
                    )
                    return False

                task.priority_boosted = True
                task.queue_position = 1  # Move to front

                # Explicit commit before _recalculate_queue_positions, which
                # opens its own session against this same shared connection.
                session.commit()

                # Recalculate other queue positions
                self._recalculate_queue_positions()

                logger.info(f"Task {task_id} priority boosted")
                return True
        except Exception as e:
            logger.error(f"Failed to boost task {task_id} priority: {e}")
            return False

    def get_queued_tasks(self) -> List[Task]:
        """Get all queued tasks ordered by priority.

        Returns:
            List of queued tasks
        """
        with self.db_manager.session_scope() as session:
            from sqlalchemy import case

            priority_order = case(
                (Task.priority == "high", 3),
                (Task.priority == "medium", 2),
                (Task.priority == "low", 1),
                else_=2,
            )

            tasks = (
                session.query(Task)
                .filter(Task.status == "queued")
                .order_by(
                    Task.priority_boosted.desc(),
                    priority_order.desc(),
                    Task.queued_at.asc(),
                )
                .all()
            )

            return tasks
