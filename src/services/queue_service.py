"""Queue service for managing agent concurrency and task queueing."""

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, case

from src.core.database import Agent, DatabaseManager, Phase, Task, Workflow, utc_now

logger = logging.getLogger(__name__)

# Shared priority ordering for queue queries (SOLID review finding 3.14):
# was a byte-identical `case()` expression hand-duplicated in 4 methods here.
# case() builds a stateless expression tree bound to Task.priority, not to any
# session/query, so one module-level instance is safe to reuse in every
# .order_by() call below -- adding a new priority tier now only needs
# updating this one place, not silently drifting out of sync across 4.
_PRIORITY_ORDER_CASE = case(
    (Task.priority == "high", 3),
    (Task.priority == "medium", 2),
    (Task.priority == "low", 1),
    else_=2,
)


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
                single inference slot: {"pi/Qwen3.8-27B-UD-Q4_K_XL.gguf": 1}).
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
        # Guards try_reserve_cli_model_slot/release_cli_model_slot -- closes
        # the TOCTOU race between "check active agent count for this combo"
        # and the real Agent row landing in the DB (worktree setup + prompt
        # generation take seconds, during which a second, independent
        # dispatch call -- process_queue, create_task, restart_task_endpoint,
        # bump_task_priority_endpoint, or the orchestrator's own
        # create_agent_for_task_direct -- could run its own check and also
        # see room. A threading.Lock (not asyncio.Lock) because
        # create_agent_for_task_direct's caller runs in a background
        # thread with its own event loop, not the main asyncio loop these
        # other call sites share.
        self._reservation_lock = threading.Lock()
        self._pending_reservations: Dict[str, int] = {}
        # Guards claim_next_queued_task's should_queue_task -> get_next_queued_task
        # -> dequeue_task sequence. Before process_queue's DB work moved off the
        # event loop (run_in_executor, perf: stop process_queue's dispatch chain
        # from blocking the event loop), running these three calls with no
        # `await` between them on the single-threaded event loop gave the
        # sequence atomicity for free -- no two concurrent process_queue()
        # coroutines could interleave mid-sequence, since nothing here ever
        # yielded control. Once that sequence started running via
        # run_in_executor on a real thread pool, two overlapping process_queue()
        # calls could genuinely execute it concurrently on different OS
        # threads -- neither get_next_queued_task's SELECT nor dequeue_task's
        # status check-then-write is an atomic DB operation on its own, so both
        # could select and dequeue the SAME task, double-dispatching it.
        # RLock, not Lock: claim_next_queued_task holds this lock across its
        # own dequeue_task call, and dequeue_task itself also acquires it --
        # because dequeue_task has DIRECT callers outside claim_next_queued_task
        # (task_admin_routes.py's bump_task_priority_endpoint and
        # cancel_queued_task_endpoint, both async FastAPI handlers). Those
        # endpoints used to run unlocked on the event-loop thread, which was
        # harmless before process_queue's DB work moved to run_in_executor
        # (same history as above) but is a genuine race against an
        # executor-thread claim ever since: both sides check-then-write
        # task.status ("queued" -> "assigned") with no serialization, so an
        # admin cancel/bump landing mid-claim can dequeue a task the claim
        # is simultaneously dequeuing, or clobber its queue_position reset.
        # Both entry points now funnel through the same lock.
        self._dequeue_lock = threading.RLock()
        logger.info(
            f"QueueService initialized with max_concurrent_agents={max_concurrent_agents}, "
            f"cli_model_concurrency_limits={self.cli_model_concurrency_limits}"
        )

    def try_reserve_cli_model_slot(self, cli_type: str, model: str) -> bool:
        """Atomically check AND reserve a slot for (cli_type, model) against
        its configured concurrency limit -- the fix for the check-then-act
        race described on self._reservation_lock above. Always succeeds
        (no-op, no reservation held) for a combo with no configured limit,
        matching the feature's original no-op-when-unconfigured behavior.

        MUST be paired with exactly one release_cli_model_slot call once
        the real dispatch attempt this reservation was for finishes --
        success or failure -- normally from a `finally` block wrapping the
        actual AgentDispatchService.dispatch/create_agent_for_task call.
        Reservations don't self-expire; a caller that reserves and never
        releases permanently steals a slot from this combo.
        """
        key = f"{cli_type}/{model}"
        limit = self.cli_model_concurrency_limits.get(key)
        if limit is None:
            return True
        with self._reservation_lock:
            active = self.get_active_agent_count_for_cli_model(cli_type, model)
            pending = self._pending_reservations.get(key, 0)
            if active + pending >= limit:
                return False
            self._pending_reservations[key] = pending + 1
            return True

    def release_cli_model_slot(self, cli_type: str, model: str) -> None:
        """Release a reservation made by try_reserve_cli_model_slot. Safe
        to call even when no reservation is outstanding for this combo
        (e.g. it has no configured limit, so try_reserve was a no-op) --
        callers that always reserve-then-release don't need to track
        whether a given combo actually had a limit configured."""
        key = f"{cli_type}/{model}"
        with self._reservation_lock:
            if key in self._pending_reservations:
                self._pending_reservations[key] -= 1
                if self._pending_reservations[key] <= 0:
                    del self._pending_reservations[key]

    def resolve_fallback_model(self, cli_type: str) -> Optional[str]:
        """The configured fallback model for cli_type, resolved by ROLE
        (primary vs secondary tier) -- mirrors
        CLIAgentInterface.fallback_model's own role lookup exactly, since
        this is used to pick the SAME target that mechanism would switch an
        already-running agent to mid-session, just applied at dispatch time
        instead.

        Public: called from every dispatch call site that needs to resolve
        a fallback for the per-cli/model concurrency gate -- server.py's
        create_task/restart_task_endpoint/bump_task_priority_endpoint and
        orchestrator.py's create_agent_for_task_direct, in addition to this
        class's own get_next_queued_task."""
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
            # "stuck" included -- this docstring's own "not terminated"
            # promise otherwise breaks silently: a stuck agent still holds
            # its tmux session/task slot but wouldn't count against the
            # concurrency cap, letting the system over-dispatch new agents
            # past max_concurrent_agents exactly while stuck agents are
            # already straining resources.
            query = session.query(Agent).filter(
                Agent.status.in_(["working", "starting", "idle", "stuck"])
            )
            if project_id:
                query = query.join(Task, Agent.current_task_id == Task.id).join(
                    Workflow, Task.workflow_id == Workflow.id
                ).filter(Workflow.project_id == project_id)
            count = query.count()
            logger.debug(f"Active agent count: {count} (project_id={project_id})")
            return count

    def resolve_cli_and_model(self, session, task: Task) -> tuple:
        """What AgentManager.create_agent_for_task would resolve task's
        cli_type/model to, without creating an agent. Mirrors that method's
        own phase-then-global-default resolution (manager.py's
        `cli_type = phase_cli_tool or cli_type or self.config.default_cli_tool`
        and its `model = (phase_cli_model if phase_cli_tool else None) or
        global_model or cli_agent.default_model`) -- deliberately not the
        explicit `cli_type=` override create_agent_for_task also accepts,
        since that's only ever passed by the session-limit fallback
        redispatch mid-run, never for a fresh queue dispatch like this.

        Public: called from every dispatch call site that needs to resolve
        a task's effective combo for the per-cli/model concurrency gate --
        see resolve_fallback_model's docstring for the full list.
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

    def resolve_cli_model_dispatch(self, session, task: Task) -> tuple:
        """The single decision + atomic reservation every dispatch call
        site needs before creating an agent for `task`: which cli_tool/
        cli_model to actually use, and whether that reservation must be
        released later. Consolidates resolve_cli_and_model +
        try_reserve_cli_model_slot + resolve_fallback_model (previously
        re-derived ad hoc at each of get_next_queued_task, create_task,
        restart_task_endpoint, bump_task_priority_endpoint, and
        orchestrator.py's create_agent_for_task_direct -- five copies of
        the same decision tree to drift out of sync).

        Returns (cli_type_override, model_override, reservation, saturated):
          - cli_type_override, model_override: None to use the phase's own
            primary combo unmodified (either no limit is configured for
            it, or it had a free slot); otherwise the fallback combo the
            caller should dispatch on instead.
          - reservation: (cli_type, model) the caller MUST pass to
            release_cli_model_slot exactly once after the real dispatch
            attempt finishes (success or failure) -- normally from a
            `finally` block. None means nothing was reserved (no limit
            configured for the resolved primary combo), nothing to
            release.
          - saturated: True if the primary combo is at its limit and no
            fallback is usable. The other three return values are all
            None/None/None in this case -- the caller decides what
            "saturated with no fallback" means for it (queue the task,
            dispatch on the primary anyway, etc.), since that varies by
            call site and isn't this method's concern.
        """
        if not self.cli_model_concurrency_limits:
            return None, None, None, False
        cli_type, model = self.resolve_cli_and_model(session, task)
        if not (cli_type and model):
            return None, None, None, False
        if self.try_reserve_cli_model_slot(cli_type, model):
            return None, None, (cli_type, model), False
        fallback_model = self.resolve_fallback_model(cli_type)
        if (
            fallback_model
            and fallback_model != model
            and self.try_reserve_cli_model_slot(cli_type, fallback_model)
        ):
            return cli_type, fallback_model, (cli_type, fallback_model), False
        return None, None, None, True

    def get_active_agent_count_for_cli_model(self, cli_type: str, model: str) -> int:
        """Count of currently active agents on this exact (cli_type, model)
        combo -- the budget a per-cli/model concurrency limit is checked
        against, e.g. a local model with a single inference slot."""
        with self.db_manager.session_scope() as session:
            # "stuck" included -- same reasoning as get_active_agent_count:
            # a stuck agent still holds its slot on this exact combo.
            return (
                session.query(Agent)
                .filter(
                    Agent.status.in_(["working", "starting", "idle", "stuck"]),
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
            task.queued_at = utc_now()

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
        from sqlalchemy import or_

        priority_order = _PRIORITY_ORDER_CASE

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
                                Task.priority_boosted.is_(False), not new_task.priority_boosted
                            ),
                        ),
                        priority_order > new_priority_value,
                    ),
                    # Same priority level and boost status: earlier queued_at is ahead
                    and_(
                        or_(
                            and_(Task.priority_boosted, new_task.priority_boosted),
                            and_(
                                Task.priority_boosted.is_(False), not new_task.priority_boosted
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
            priority_order = _PRIORITY_ORDER_CASE

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
                # usable -- the next-highest-priority task that fits still
                # gets picked up this same pass instead of the whole queue
                # stalling behind one combo. resolve_cli_model_dispatch
                # atomically reserves whichever combo it picks (closing the
                # check-then-act race against every other dispatch call
                # site sharing this limit); task._reserved_cli_model tells
                # process_queue which reservation it must release once the
                # real dispatch attempt (success or failure) finishes.
                if self.cli_model_concurrency_limits:
                    cli_override, model_override, reservation, saturated = self.resolve_cli_model_dispatch(
                        session, task
                    )
                    if saturated:
                        logger.debug(
                            f"Task {task.id}'s combo is already at its concurrency limit with no "
                            "usable fallback -- skipping for now"
                        )
                        continue
                    if cli_override:
                        logger.info(
                            f"Task {task.id}'s primary combo at its concurrency limit -- "
                            f"dispatching on fallback model {model_override} instead"
                        )
                        task._dispatch_cli_override = (cli_override, model_override)
                    if reservation:
                        task._reserved_cli_model = reservation

                # Task is valid, return it
                logger.info(
                    f"Next queued task: {task.id} (priority={task.priority}, boosted={task.priority_boosted})"
                )
                return task

            logger.debug("No queued tasks found")
            return None

    def dequeue_task(self, task_id: str) -> bool:
        """Remove a task from the queue (mark as assigned).

        Takes _dequeue_lock so that direct callers (task_admin_routes.py's
        bump/cancel endpoints) are serialized against claim_next_queued_task's
        check-select-dequeue sequence running on an executor thread -- see
        _dequeue_lock's docstring for the race this closes. RLock makes this
        safe to call from inside claim_next_queued_task, which already holds
        the lock.

        Returns:
            True if the task was queued and is now assigned. False if it
            wasn't found or wasn't queued -- callers that treat a dequeue
            as unconditionally successful without checking this (the
            original shape of this method, and of claim_next_queued_task)
            can hand out a task that was deleted or transitioned out of
            "queued" by something else holding the lock first.

        Args:
            task_id: ID of the task to dequeue
        """
        with self._dequeue_lock:
            with self.db_manager.session_scope() as session:
                task = session.query(Task).filter_by(id=task_id).first()
                if not task:
                    logger.error(f"Task {task_id} not found for dequeueing")
                    return False

                if task.status != "queued":
                    logger.warning(f"Task {task_id} is not queued (status={task.status})")
                    return False

                task.status = "assigned"
                task.queue_position = None  # Clear queue position

                # Explicit commit before _recalculate_queue_positions, which
                # opens its own session against this same shared connection.
                session.commit()

                # Update queue positions for remaining tasks
                self._recalculate_queue_positions()

                logger.info(f"Task {task_id} dequeued and marked as assigned")
                return True

    def claim_next_queued_task(self, project_id: Optional[str] = None) -> Optional[Task]:
        """Atomically check capacity, select, and dequeue the next queued
        task -- see _dequeue_lock's docstring for why this needs a lock
        rather than calling should_queue_task/get_next_queued_task/
        dequeue_task separately.

        Every caller that mutates or deletes a Task row now takes this
        same lock (dequeue_task, boost_task_priority, cancel_queued_task),
        so in practice dequeue_task below always succeeds for the task
        get_next_queued_task just selected -- but checking its return
        value instead of assuming success is what makes that an
        invariant this method enforces, not a fact every future caller
        has to remember to preserve. Returning a task dequeue_task
        actually failed to claim (e.g. deleted between the select and
        the dequeue by some caller this lock doesn't yet cover) would
        hand process_queue a task to dispatch that isn't really claimed.
        """
        with self._dequeue_lock:
            if self.should_queue_task(project_id):
                logger.debug(f"At capacity - not processing queue (project_id={project_id})")
                return None

            task = self.get_next_queued_task(project_id)
            if not task:
                logger.debug("No queued tasks to process")
                return None

            if not self.dequeue_task(task.id):
                logger.warning(
                    f"Task {task.id} was selected as the next queued task but "
                    "could not be dequeued (deleted or changed state concurrently) "
                    "-- skipping this cycle"
                )
                return None

            return task

    def _recalculate_queue_positions(self) -> None:
        """Recalculate queue positions for all queued tasks."""
        try:
            with self.db_manager.session_scope() as session:
                priority_order = _PRIORITY_ORDER_CASE

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

        Takes _dequeue_lock: called from task_admin_routes.py's
        bump_task_priority_endpoint on the event-loop thread, and its
        check-then-write on priority_boosted/queue_position (plus the
        position recalculation) races claim_next_queued_task's locked
        select-dequeue sequence on an executor thread over the same rows
        -- the same race dequeue_task's own lock closes, one call earlier
        in the bump endpoint's sequence.

        Args:
            task_id: ID of the task to boost

        Returns:
            True if successful, False otherwise
        """
        try:
            with self._dequeue_lock:
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

    def cancel_queued_task(
        self, task_id: str, reason: str = "Cancelled by user from queue"
    ) -> Tuple[str, Optional[str]]:
        """Cancel a queued task: check it's queued, mark it failed, and
        renumber the remaining queue -- all under _dequeue_lock.

        The lock is the point: task_admin_routes.py's
        cancel_queued_task_endpoint runs on the event-loop thread while
        claim_next_queued_task holds the same lock across its
        select-then-dequeue sequence on an executor thread. An UNLOCKED
        status="failed" write from here could land inside that window,
        after the claim already selected the task but before its
        dequeue_task ran -- the claim then warns "not queued", still
        returns the task, and process_queue dispatches an agent for a task
        the user just cancelled. Serialized instead: either the cancel
        wins the lock first (the claim's selection never sees the task) or
        the claim finishes first (cancel reports not-queued, matching the
        endpoint's existing 400 semantics).

        Returns:
            ("cancelled", workflow_id) on success,
            ("not_found", None) if the task doesn't exist,
            ("not_queued", None) if it's no longer queued.
        """
        with self._dequeue_lock:
            with self.db_manager.session_scope() as session:
                task = session.query(Task).filter_by(id=task_id).first()
                if not task:
                    logger.error(f"Task {task_id} not found for cancel")
                    return "not_found", None

                if task.status != "queued":
                    logger.warning(
                        f"Cannot cancel task {task_id} - not queued (status={task.status})"
                    )
                    return "not_queued", None

                workflow_id = task.workflow_id
                task.status = "failed"
                task.failure_reason = reason
                task.completed_at = utc_now()
                # Explicit commit before _recalculate_queue_positions, which
                # opens its own session against this same shared connection.
                session.commit()

                self._recalculate_queue_positions()

                logger.info(f"Task {task_id} cancelled: {reason}")
                return "cancelled", workflow_id

    def pause_queued_task(self, task_id: str) -> bool:
        """Pause a queued task (-> "blocked"), under _dequeue_lock.

        Same race class as cancel_queued_task (see its docstring): reached
        via task_admin_routes.py's pause_task_endpoint's "queued" case
        instead of the cancel endpoint. An unlocked status="blocked" write
        here could land inside claim_next_queued_task's select-then-dequeue
        window, letting a task the user just paused get dispatched anyway.

        Returns:
            True if the task was queued and is now blocked, False if it
            wasn't found or wasn't queued.
        """
        with self._dequeue_lock:
            with self.db_manager.session_scope() as session:
                task = session.query(Task).filter_by(id=task_id).first()
                if not task:
                    logger.error(f"Task {task_id} not found for pause")
                    return False

                if task.status != "queued":
                    logger.warning(
                        f"Cannot pause task {task_id} as queued - not queued (status={task.status})"
                    )
                    return False

                task.status = "blocked"
                task.queue_position = None
                # Explicit commit before _recalculate_queue_positions, which
                # opens its own session against this same shared connection.
                session.commit()

                self._recalculate_queue_positions()

                logger.info(f"Task {task_id} paused from queue")
                return True

    def reset_queued_task_to_pending(self, task_id: str) -> bool:
        """Pull a queued task out of the queue and reset it to "pending",
        under _dequeue_lock -- for stop_workflow, which resets its
        workflow's "assigned"/"in_progress" tasks to "pending" too (a
        clean, undispatched state for the next resume) but previously left
        "queued" tasks untouched: still eligible for claim_next_queued_task
        to dispatch even after the workflow was stopped.

        Same race class as cancel_queued_task/pause_queued_task: an
        unlocked write here could land inside claim_next_queued_task's
        select-then-dequeue window and let a task this stop just reset
        get dispatched anyway.

        Returns:
            True if the task was queued and is now pending, False if it
            wasn't found or wasn't queued.
        """
        with self._dequeue_lock:
            with self.db_manager.session_scope() as session:
                task = session.query(Task).filter_by(id=task_id).first()
                if not task:
                    logger.error(f"Task {task_id} not found for stop-reset")
                    return False

                if task.status != "queued":
                    logger.warning(
                        f"Cannot reset task {task_id} as queued - not queued (status={task.status})"
                    )
                    return False

                task.status = "pending"
                task.queue_position = None
                # Explicit commit before _recalculate_queue_positions, which
                # opens its own session against this same shared connection.
                session.commit()

                self._recalculate_queue_positions()

                logger.info(f"Task {task_id} reset to pending from queue (workflow stop)")
                return True

    def get_queued_tasks(self) -> List[Task]:
        """Get all queued tasks ordered by priority.

        Returns:
            List of queued tasks
        """
        with self.db_manager.session_scope() as session:
            priority_order = _PRIORITY_ORDER_CASE

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
