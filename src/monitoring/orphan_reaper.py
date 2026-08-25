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
from typing import Dict, Optional

from src.core.database import utc_now

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
        # session_name -> when it was FIRST observed as a orphan candidate
        # (in tmux, agent-named, no matching active Agent row). The grace
        # period is timed per-session from here, not from the reaper's own
        # last run -- see the fix below GRACE_PERIOD_SECONDS's docstring.
        self._first_seen_orphan: Dict[str, datetime] = {}

    async def _tmux_sessions(self):
        """libtmux's Server.sessions is a property that shells out to
        `tmux list-sessions` -- blocking, offloaded so it doesn't stall
        this process's event loop. Called at each of this file's several
        points that need a fresh session list rather than fetched once,
        since real time (including DB work) elapses between them."""
        import asyncio

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.agent_manager.tmux_server.sessions)

    async def _kill_agent_session(self, session_name: str) -> bool:
        """Flush the stability-tracked transcript, then kill a tmux session
        by name. Shared by both places this file kills a session: an agent
        just marked terminated in the DB (whose tmux process may still be
        running -- that's exactly the gap this helper closes, see its
        caller in cleanup_orphaned_tmux_sessions), and a session with no
        matching Agent row at all. Returns whether a live session was
        actually found and killed."""
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
                        session_name, transcript_dir / f"{session_name}.clean.log",
                    )
        except Exception as e:
            logger.debug(f"[STABLE-TRANSCRIPT] Final flush before kill failed: {e}")

        import asyncio

        loop = asyncio.get_event_loop()
        for tmux_sess in await self._tmux_sessions():
            if tmux_sess.name == session_name:
                await loop.run_in_executor(None, tmux_sess.kill_session)
                logger.info(f"Killed tmux session: {session_name}")
                return True
        return False

    async def cleanup_orphaned_tmux_sessions(self) -> None:
        """Clean up tmux sessions that don't have corresponding active agents.
        Also clean up orphaned agents (working but no active workflow)."""
        from src.autopilot.orchestrator.engine_client import terminate_agent
        from src.core.database import Agent, Task, Workflow

        logger.debug("Starting orphaned tmux session cleanup")

        try:
            # Get all tmux sessions that start with 'agent' (the new naming convention)
            agent_sessions = []
            for session in await self._tmux_sessions():
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
                # allows idle/working/stuck/terminated/starting, so those
                # two never matched anything here. List every
                # non-terminated status instead of just "working" so this
                # stays correct if idle/stuck ever start actually being
                # set. "starting" included too -- a freshly-created agent
                # still in that state can already have a live tmux
                # session, which would otherwise be misjudged as orphaned.
                active_agents = (
                    session.query(Agent)
                    .filter(Agent.status.in_(["working", "idle", "stuck", "starting"]))
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
                            # utc_now() at every write site, so a
                            # datetime.now() cutoff compares a local-time
                            # value against a UTC one. West of UTC that
                            # difference is negative, so this guard matched
                            # unconditionally and the reaper never reaped
                            # here. See CLAUDE.md's utc-only invariant.
                            if (
                                agent.last_activity
                                and (utc_now() - agent.last_activity).total_seconds()
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
                            # terminate_agent (engine_client.py) is a DB-only
                            # primitive -- it flips agent.status but never
                            # touches the actual tmux session, and the
                            # separate orphaned-tmux-session pass below can't
                            # catch it either: its active_session_names
                            # snapshot was taken BEFORE this loop ran, so it
                            # still lists this exact session as belonging to
                            # an active agent and skips it, and even a fresh
                            # snapshot would still make it wait out this
                            # file's unrelated 120s GRACE_PERIOD_SECONDS.
                            # Left unkilled, the CLI process keeps running
                            # unaware the DB now considers it dead, can
                            # finish real work, and later gets correctly but
                            # confusingly rejected ("Agent not authenticated")
                            # when it tries to report -- while whatever
                            # fresh agent got dispatched in its place redoes
                            # the same task. Confirmed live: agents 12e657b5
                            # and 15fbae32 both did a full adversarial review
                            # of the same task for exactly this reason. Kill
                            # it here, immediately -- this agent was just
                            # independently justified as dead (workflow
                            # inactive, no recent activity), so there's
                            # nothing to wait for.
                            if agent.tmux_session_name:
                                await self._kill_agent_session(agent.tmux_session_name)
                session.commit()

            finally:
                session.close()

            # Find orphaned sessions (exist in tmux but not in database)
            # utcnow, not now: self._first_seen_orphan's timestamps are only
            # ever set from this same clock (below), but a local-time value
            # here would still disagree with the utcnow() comparison already
            # used for last_activity earlier in this method. See CLAUDE.md's
            # utc-only invariant.
            current_time = utc_now()

            # First-ever call: nothing has been tracked as a candidate yet,
            # so every apparent orphan right now is one this reaper simply
            # hasn't had a chance to observe twice -- skip entirely rather
            # than seed _first_seen_orphan with a batch that might include
            # sessions mid-registration at process startup.
            if self.last_check_time is None:
                self.last_check_time = current_time
                logger.debug(
                    "First orphan check - skipping all sessions for grace period"
                )
                return
            self.last_check_time = current_time

            # Grace period is timed per-session from when THIS session was
            # first seen as an orphan candidate, not from how long it's
            # been since the reaper itself last ran. The previous version
            # compared elapsed-time-since-last-run against
            # GRACE_PERIOD_SECONDS directly -- under this monitor's default
            # ~60s run cadence, that duration is almost always well under
            # the 120s threshold, so the grace check was true on nearly
            # every cycle and orphan sessions were essentially never
            # actually killed in normal steady-state operation, regardless
            # of how long they'd genuinely been orphaned.
            orphaned_sessions = []
            candidate_names = set()
            for tmux_sess in await self._tmux_sessions():
                if tmux_sess.name not in agent_sessions:
                    continue
                if tmux_sess.name in active_session_names:
                    continue

                candidate_names.add(tmux_sess.name)
                first_seen = self._first_seen_orphan.get(tmux_sess.name)
                if first_seen is None:
                    self._first_seen_orphan[tmux_sess.name] = current_time
                    logger.debug(
                        f"Session {tmux_sess.name} newly seen as an orphan "
                        f"candidate -- starting its own {self.GRACE_PERIOD_SECONDS}s grace period"
                    )
                    continue

                orphaned_for = (current_time - first_seen).total_seconds()
                if orphaned_for < self.GRACE_PERIOD_SECONDS:
                    logger.debug(
                        f"Skipping session {tmux_sess.name} - within its own grace period "
                        f"({orphaned_for:.0f}s < {self.GRACE_PERIOD_SECONDS}s)"
                    )
                    continue

                orphaned_sessions.append(tmux_sess.name)

            # Stop tracking anything that's no longer a candidate (it got
            # registered to an active agent, or the session itself is gone)
            # -- otherwise this dict grows without bound over a long-running
            # process, and a stale entry could let a LATER, genuinely
            # different session that happens to reuse the same name skip
            # its own grace period entirely.
            for name in list(self._first_seen_orphan):
                if name not in candidate_names:
                    del self._first_seen_orphan[name]

            if not orphaned_sessions:
                logger.debug("No orphaned tmux sessions found")
                return

            logger.info(
                f"Found {len(orphaned_sessions)} orphaned tmux sessions (after grace period): {orphaned_sessions}"
            )

            # Kill orphaned sessions (flush-then-kill via the same helper
            # used when an orphaned AGENT is terminated above -- these
            # sessions have no active Agent row by definition, so the
            # helper's own lookup naturally finds none and skips the flush).
            killed_count = 0
            for session_name in orphaned_sessions:
                try:
                    if await self._kill_agent_session(session_name):
                        killed_count += 1
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
