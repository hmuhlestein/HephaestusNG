"""Reconciling tmux sessions against the DB's view of active agents:
terminating agents whose workflow is no longer active, and killing tmux
sessions that have no corresponding active agent row.

Extracted from MonitoringLoop, which mixed this reconciliation concern in
with scheduling, mechanical-recovery heuristics, and Guardian dispatch —
see docs/SOLID_OO_REVIEW.md finding 3.4. MonitoringLoop still exposes
_cleanup_orphaned_tmux_sessions (tests call it directly on the
MonitoringLoop instance and set/read its grace-period timestamp there) but
delegates to an OrphanSessionReaper instance instead of doing the
reconciliation itself.
"""

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class OrphanSessionReaper:
    """Reconciles tmux sessions against the DB's active-agent set."""

    #: Newly-created sessions get this long to be registered in the DB
    #: before being considered orphaned.
    GRACE_PERIOD_SECONDS = 120

    def __init__(self, db_manager, agent_manager):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.last_check_time: Optional[datetime] = None

    async def cleanup_orphaned_tmux_sessions(self) -> None:
        """Clean up tmux sessions that don't have corresponding active agents.
        Also clean up orphaned agents (working but no active workflow)."""
        from src.core.database import Agent, Task, Workflow

        logger.debug("Starting orphaned tmux session cleanup")

        try:
            # Get all tmux sessions that start with 'agent' (the new naming convention)
            agent_sessions = []
            for session in self.agent_manager.tmux_server.sessions:
                if session.name.startswith("agent"):
                    agent_sessions.append(session.name)

            if not agent_sessions:
                logger.debug("No agent tmux sessions found")
                return

            logger.debug(
                f"Found {len(agent_sessions)} agent tmux sessions: {agent_sessions}"
            )

            # Get all active agent session names from database
            session = self.db_manager.get_session()
            try:
                active_agents = (
                    session.query(Agent)
                    .filter(Agent.status.in_(["working", "pending", "assigned"]))
                    .all()
                )

                active_session_names = {
                    agent.tmux_session_name
                    for agent in active_agents
                    if agent.tmux_session_name
                }

                logger.debug(
                    f"Found {len(active_session_names)} active agent sessions: {active_session_names}"
                )

                # Clean up orphaned agents (working but no active workflow)
                active_workflow_ids = {
                    wf.id
                    for wf in session.query(Workflow)
                    .filter(Workflow.status.in_(["active", "running"]))
                    .all()
                }

                for agent in active_agents:
                    if agent.current_task_id:
                        task = (
                            session.query(Task)
                            .filter_by(id=agent.current_task_id)
                            .first()
                        )
                        if (
                            task
                            and task.workflow_id
                            and task.workflow_id not in active_workflow_ids
                        ):
                            logger.info(
                                f"Terminating orphaned agent {agent.id[:8]} - workflow {task.workflow_id[:8]} not active"
                            )
                            agent.status = "terminated"
                            agent.current_task_id = None  # Clear stale reference
                session.commit()

            finally:
                session.close()

            # Find orphaned sessions (exist in tmux but not in database)
            # Use grace period based on last check time to avoid killing newly-created sessions
            current_time = datetime.now()

            # Track when we last checked - agents created since last check get grace period
            if self.last_check_time is None:
                self.last_check_time = current_time
                logger.debug(
                    "First orphan check - skipping all sessions for grace period"
                )
                return

            time_since_last_check = (
                current_time - self.last_check_time
            ).total_seconds()

            orphaned_sessions = []
            for tmux_sess in self.agent_manager.tmux_server.sessions:
                if tmux_sess.name not in agent_sessions:
                    continue
                if tmux_sess.name in active_session_names:
                    continue

                # Apply grace period: if we just started monitoring or haven't checked in a while,
                # skip orphan detection to let new agents get registered in DB
                if time_since_last_check < self.GRACE_PERIOD_SECONDS:
                    logger.debug(
                        f"Skipping session {tmux_sess.name} - within grace period "
                        f"({time_since_last_check:.0f}s < {self.GRACE_PERIOD_SECONDS}s)"
                    )
                    continue

                orphaned_sessions.append(tmux_sess.name)

            # Update last check time
            self.last_check_time = current_time

            if not orphaned_sessions:
                logger.debug("No orphaned tmux sessions found")
                return

            logger.info(
                f"Found {len(orphaned_sessions)} orphaned tmux sessions (after grace period): {orphaned_sessions}"
            )

            # Kill orphaned sessions
            killed_count = 0
            for session_name in orphaned_sessions:
                try:
                    # Find and kill the session
                    for tmux_sess in self.agent_manager.tmux_server.sessions:
                        if tmux_sess.name == session_name:
                            tmux_sess.kill_session()
                            logger.info(f"Killed orphaned tmux session: {session_name}")
                            killed_count += 1
                            break
                except Exception as e:
                    logger.warning(
                        f"Failed to kill orphaned session {session_name}: {e}"
                    )

            if killed_count > 0:
                logger.info(
                    f"Successfully cleaned up {killed_count} orphaned tmux sessions"
                )

        except Exception as e:
            logger.error(f"Error during tmux session cleanup: {e}")
            raise
