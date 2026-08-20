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
        from src.autopilot.orchestrator.engine_client import terminate_agent
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
                # "pending"/"assigned" are Task.status values, not
                # Agent.status ones -- Agent.status's CheckConstraint only
                # allows idle/working/stuck/terminated, so those two never
                # matched anything here. List every non-terminated status
                # instead of just "working" so this stays correct if
                # idle/stuck ever start actually being set.
                active_agents = (
                    session.query(Agent)
                    .filter(Agent.status.in_(["working", "idle", "stuck"]))
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

                # Clean up orphaned agents (working but no active workflow).
                #
                # A workflow paused_by="review" is a special case, not a
                # genuine stop: it means one specific manual-only phase
                # (git_expert) is waiting on a human, but per
                # _advance_phases's own paused_by=="review" carve-out
                # (phase_transitions.py), every OTHER phase keeps
                # advancing/dispatching normally while it waits. Excluding
                # it here undercuts that fix -- any agent legitimately
                # dispatched into an unrelated phase during a review-pause
                # would just get killed the moment its last_activity grace
                # window (below) elapses, landing its task right back at
                # "pending" and looking permanently stuck. Confirmed live:
                # a freshly-dispatched development-phase agent (task
                # 146d191d) was killed this way ~3 minutes after launch,
                # solely because its workflow's status read "paused" at
                # this exact moment. Other pause reasons (user, budget,
                # system) are genuine full stops and stay excluded.
                from sqlalchemy import and_, or_
                active_workflow_ids = {
                    wf.id
                    for wf in session.query(Workflow)
                    .filter(
                        or_(
                            Workflow.status == "active",
                            and_(Workflow.status == "paused", Workflow.paused_by == "review"),
                        )
                    )
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
                            # A workflow status flip can be very recent (e.g.
                            # a false failure elsewhere that hasn't been
                            # corrected yet) while the agent itself is
                            # demonstrably still alive and reporting in --
                            # give it a short grace window instead of killing
                            # on the workflow-status read alone. Also fixes a
                            # standing invariant violation: this path set
                            # status="terminated" without ever setting
                            # terminated_at.
                            # utcnow, not now: last_activity is stamped with
                            # datetime.utcnow() at every write site, so a
                            # datetime.now() cutoff compares a local-time
                            # value against a UTC one. West of UTC that
                            # difference is negative, so this guard matched
                            # unconditionally and the reaper never reaped
                            # here. See CLAUDE.md's utc-only invariant.
                            if (
                                agent.last_activity
                                and (datetime.utcnow() - agent.last_activity).total_seconds()
                                < 30
                            ):
                                logger.debug(
                                    f"Skipping agent {agent.id[:8]} - workflow "
                                    f"{task.workflow_id[:8]} not active, but agent "
                                    "reported activity within the last 30s"
                                )
                                continue
                            logger.info(
                                f"Terminating orphaned agent {agent.id[:8]} - workflow {task.workflow_id[:8]} not active"
                            )
                            terminate_agent(agent.id, session=session)
                session.commit()

            finally:
                session.close()

            # Find orphaned sessions (exist in tmux but not in database)
            # Use grace period based on last check time to avoid killing newly-created sessions.
            # utcnow, not now: self.last_check_time is only ever set from this
            # same variable (below), but a local-time clock here would still
            # disagree with the utcnow() comparison already used for
            # last_activity earlier in this method. See CLAUDE.md's
            # utc-only invariant.
            current_time = datetime.utcnow()

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
                    # Final flush of the stability-tracked "clean"
                    # transcript before the session (and its scrollback)
                    # disappears -- this kill path bypasses
                    # terminate_agent's own clean-shutdown flush entirely.
                    # These sessions have no active Agent row by definition
                    # (that's why they're "orphaned"), so look up whatever
                    # Agent row this session name last belonged to (any
                    # status) rather than requiring a live one.
                    try:
                        db_session = self.db_manager.get_session()
                        try:
                            from src.core.database import Agent as _Agent

                            last_agent = (
                                db_session.query(_Agent)
                                .filter_by(tmux_session_name=session_name)
                                .first()
                            )
                        finally:
                            db_session.close()
                        if last_agent:
                            transcript_dir = self.agent_manager._resolve_tmux_transcript_dir(last_agent)
                            if transcript_dir:
                                self.agent_manager._flush_stable_transcript(
                                    session_name,
                                    transcript_dir / f"{session_name}.clean.log",
                                )
                    except Exception as e:
                        logger.debug(f"[STABLE-TRANSCRIPT] Final flush before reap failed: {e}")

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
