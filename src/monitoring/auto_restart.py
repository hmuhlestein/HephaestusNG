"""Shared collaborator for killing a stuck agent's tmux session and marking
it for restart.

Extracted from MonitoringLoop to satisfy SOLID review finding: both the
mechanical-recovery cluster (cluster B) and the Guardian-dispatch cluster
(cluster C) call this logic, so it gets its own home rather than living
on either cluster or requiring a back-reference to MonitoringLoop.

Bug note: this method sets status="terminated" and clears current_task_id
but never sets terminated_at -- a violation of the agent-termination
critical invariant (see AGENTS.md). Logged for Phase 3; DO NOT FIX here.
"""

import logging

from src.autopilot.orchestrator.engine_client import terminate_agent
from src.core.database import Agent

logger = logging.getLogger(__name__)


class AutoRestart:
    """Kills a stuck agent's tmux session and marks it for restart."""

    def __init__(self, db_manager, agent_manager, guardian):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.guardian = guardian

    async def restart_agent(self, agent: Agent) -> None:
        """Kill a stuck agent's tmux session and mark it for restart.

        Resets the agent's current task back to "pending" (cleared
        assigned_agent_id) BEFORE killing the session and marking the
        agent terminated -- mirroring the ordering _detect_connection_
        errors/_detect_agent_never_started/the context-overflow path all
        use, and unlike this function's own prior behavior, which only
        ever touched the Agent row. Without this, a Task stayed
        "assigned"/"in_progress" pointing at an agent this function had
        just marked "terminated" -- indistinguishable from a genuinely
        still-working agent to anything reading the Task alone, until an
        unrelated periodic sweep (attempt_recovery's stale-assigned-task
        cleanup) eventually found the mismatch and failed the task with a
        generic "terminated unexpectedly" reason. Observed live: an
        agent doing real, valuable work (a complete adversarial review
        with real findings, written to disk) got marked terminated here,
        couldn't report its own completion afterward (verify_agent_
        authentication correctly rejects a terminated agent's calls, by
        design), and the pipeline burned through the rest of its retry
        budget and failed with no visible cause -- while the agent's tmux
        session kept running to completion in the background, orphaned.
        """
        try:
            task_id = agent.current_task_id
            if task_id:
                try:
                    with self.db_manager.session_scope() as session:
                        from src.core.database import Task as _Task

                        stuck_task = (
                            session.query(_Task)
                            .filter_by(id=task_id)
                            .filter(_Task.status.in_(["assigned", "in_progress"]))
                            .first()
                        )
                        if stuck_task:
                            stuck_task.status = "pending"
                            stuck_task.assigned_agent_id = None
                            stuck_task.failure_reason = None
                            logger.info(
                                f"[AUTO-RESTART] Task {stuck_task.id[:8]} reset to pending before restarting agent {agent.id[:8]}"
                            )
                except Exception as e:
                    logger.error(f"[AUTO-RESTART] Failed to reset task {task_id[:8]} before restarting agent {agent.id[:8]}: {e}")

            if agent.tmux_session_name:
                # Final flush of the stability-tracked "clean" transcript
                # before the session (and its scrollback) disappears --
                # this kill path bypasses terminate_agent's own clean-
                # shutdown flush entirely, see AgentManager._flush_stable_transcript.
                try:
                    transcript_dir = self.agent_manager._resolve_tmux_transcript_dir(agent)
                    if transcript_dir:
                        self.agent_manager._flush_stable_transcript(
                            agent.tmux_session_name,
                            transcript_dir / f"{agent.tmux_session_name}.clean.log",
                        )
                except Exception as e:
                    logger.error(f"[STABLE-TRANSCRIPT] Final flush before auto-restart failed: {e}")

                self.agent_manager.tmux_server.kill_session(agent.tmux_session_name)
                logger.info(f"Killed tmux session {agent.tmux_session_name}")

            with self.db_manager.session_scope() as session:
                # Re-query the agent from this session to avoid detached object bugs
                db_agent = session.query(Agent).filter_by(id=agent.id).first()
                if db_agent:
                    terminate_agent(agent.id, session=session)
                    # health_check_failures reset is auto-restart-specific.
                    db_agent = session.query(Agent).filter_by(id=agent.id).first()
                    if db_agent:
                        db_agent.health_check_failures = 0
                else:
                    logger.warning(f"Agent {agent.id} not found in DB during restart")

            # Record the restart
            self.guardian.record_auto_restart(
                agent.id,
                "Agent ignored steering too many times, auto-restarted",
            )

        except Exception as e:
            logger.error(f"Failed to auto-restart agent {agent.id}: {e}")
