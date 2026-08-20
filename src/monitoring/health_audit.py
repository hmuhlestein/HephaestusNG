"""System health audit — DB-driven stuck-task detection and nudging.

Extracted from MonitoringLoop: cluster F — _audit_system_health was a
standalone concern that owns _stuck_task_nudges and _health_findings
as its own instance attributes rather than MonitoringLoop's.

See docs/SOLID_OO_REVIEW.md and design_docs/phase_1b_decomposition.md §4.3.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from src.prompts.loader import get_monitor_nudge

logger = logging.getLogger(__name__)

# Idle-nudge cap — see _audit_system_health's own comment for the failure
# mode this closes.
MAX_STUCK_TASK_NUDGES = 3


class SystemHealthAuditor:
    """DB-driven health checks and stuck-task nudge/termination logic."""

    def __init__(self, db_manager, agent_manager, config):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.config = config

        # Tracks task_id -> (how many nudges sent, when we last nudged)
        self._stuck_task_nudges: Dict[str, Tuple[int, datetime]] = {}
        self._health_findings: List[Dict[str, Any]] = []

    async def audit_system_health(self):
        """Audit system health across all autopilot workflows.

        Delegates to shared run_health_audit() function.
        """
        import asyncio

        from src.mcp.autopilot.control_routes import run_health_audit

        # run_health_audit does real subprocess work (pgrep, tmux
        # list-panes, git branch --list; up to 10s each) -- offloaded so
        # it doesn't stall this process's event loop. The /health HTTP
        # endpoint already offloads the same shared function; this call
        # site, awaited directly inside the Monitor's own loop, is a
        # SEPARATE process from that endpoint and did not.
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_health_audit, self.db_manager)

        # Log findings
        for f in result["findings"]:
            log_fn = (
                logger.warning if f["severity"] in ("warning", "error") else logger.info
            )
            log_fn(f"[HEALTH] {f['type']}: {f['message']}")

        # Store for API access
        self._health_findings = result["findings"]

        # Task stuck detection: an in_progress task is only genuinely stuck
        # if its agent has produced no activity for stuck_detection_minutes
        # -- not merely because the task has been open that long. A
        # legitimately long phase (e.g. architecture_design) can run well
        # past that mark while the agent keeps working (observed live: a
        # 10-minute-old task with an agent that had reported activity 30s
        # earlier got killed anyway under the old started_at-only check).
        # An agent that looks idle gets nudged once and given one more
        # window to respond before its task is failed, in case it's mid a
        # slow tool call rather than truly stuck.
        try:
            session = self.db_manager.get_session()
            from src.core.database import Agent, Task

            idle_minutes = timedelta(minutes=self.config.stuck_detection_minutes)
            idle_cutoff = datetime.utcnow() - idle_minutes
            candidate_tasks = (
                session.query(Task)
                .filter(
                    Task.status == "in_progress",
                    Task.started_at < idle_cutoff,
                    Task.started_at.isnot(None),
                )
                .all()
            )
            live_task_ids = {t.id for t in candidate_tasks}
            for stale_id in list(self._stuck_task_nudges):
                if stale_id not in live_task_ids:
                    self._stuck_task_nudges.pop(stale_id, None)

            for task in candidate_tasks:
                agent = (
                    session.query(Agent).filter_by(id=task.assigned_agent_id).first()
                    if task.assigned_agent_id
                    else None
                )

                if agent and agent.status == "working":
                    last_seen = agent.last_activity or task.started_at
                    nudge_count, nudged_at = self._stuck_task_nudges.get(task.id, (0, None))

                    if last_seen >= idle_cutoff:
                        # Producing output within the window -- healthy
                        # right now, take no action. Deliberately NOT
                        # clearing nudge_count/nudged_at here: an agent
                        # stuck in a belief loop (e.g. confusing this task
                        # with an already-completed earlier one in the same
                        # resumed session) can reply right after each nudge
                        # -- satisfying this exact check -- and then go
                        # idle again soon after, never actually calling
                        # complete_my_task. If this branch reset the
                        # counter, that cycle could repeat forever and the
                        # cap below would never be reached -- observed
                        # live: the task stayed in_progress indefinitely
                        # this way. Nudge history only clears when the task
                        # leaves the candidate set entirely (see the
                        # live_task_ids sweep above) or once genuinely
                        # marked stuck below.
                        continue

                    if nudged_at is not None and datetime.utcnow() - nudged_at < idle_minutes:
                        continue  # still within the post-nudge grace period

                    try:
                        max_nudges = int(getattr(self.config, 'max_stuck_nudges', MAX_STUCK_TASK_NUDGES))
                    except (TypeError, ValueError):
                        max_nudges = MAX_STUCK_TASK_NUDGES
                    if nudge_count >= max_nudges:
                        logger.warning(
                            f"[HEALTH] Task {task.id[:8]}: agent {agent.id[:8]} has "
                            f"been nudged {nudge_count} times without completing "
                            "the task -- treating as genuinely stuck rather than "
                            "nudging again"
                        )
                        # Fall through to the stuck-handling block below.
                    else:
                        try:
                            await self.agent_manager.send_message_to_agent(
                                agent.id,
                                get_monitor_nudge("stuck_task_no_activity", task_id=task.id),
                            )
                            self._stuck_task_nudges[task.id] = (nudge_count + 1, datetime.utcnow())
                            logger.info(
                                f"[HEALTH] Nudged idle agent {agent.id[:8]} for task "
                                f"{task.id[:8]} (no activity since {last_seen}, "
                                f"nudge #{nudge_count + 1})"
                            )
                        except Exception as e:
                            logger.warning(
                                f"[HEALTH] Failed to nudge agent {agent.id[:8]}: {e}"
                            )
                        continue  # give it one more window before failing

                # No agent, agent not active, or no response even after a
                # nudge and a full grace period -- genuinely stuck.
                self._stuck_task_nudges.pop(task.id, None)

                # If the agent called update_task_status(done) but the session
                # was killed before the response was processed, completion_notes
                # will be set. Promote to done instead of failing.
                # BUT: for gated phases, we must validate the gate result
                # before promoting to done, otherwise invalid results bypass
                # the gate validation.
                if task.completion_notes:
                    from src.autopilot.spec import GATED_PHASES
                    from src.core.database import Phase as _Phase
                    
                    phase = session.query(_Phase).filter_by(id=task.phase_id).first() if task.phase_id else None
                    is_gated = phase and phase.name in GATED_PHASES
                    
                    if is_gated:
                        # For gated phases, don't promote to done without gate validation
                        # Mark as failed so the gate can be re-evaluated properly
                        logger.warning(
                            f"[HEALTH] Task {task.id[:8]} stuck in_progress in gated phase '{phase.name}' — "
                            f"marking failed (gate validation required, cannot promote directly to done)"
                        )
                        task.status = "failed"
                        task.failure_reason = (
                            f"Task stuck in gated phase '{phase.name}' — agent finished but "
                            f"gate validation was not completed. Retry to re-run with proper validation."
                        )
                    else:
                        logger.info(
                            f"[HEALTH] Task {task.id[:8]} stuck in_progress but has "
                            f"completion_notes — promoting to done (agent finished then crashed)"
                        )
                        task.status = "done"
                        task.failure_reason = None
                        task.completed_at = datetime.utcnow()
                        # No spec-gate firing here, deliberately: this branch
                        # is only reachable when `is_gated` (computed above)
                        # is already False, so a gated phase's stuck task
                        # never reaches this promote-to-done path at all --
                        # it takes the `if is_gated:` branch above instead,
                        # which marks it "failed" specifically so a proper
                        # re-run goes through real gate validation, not this
                        # heuristic. A prior version of this branch had a
                        # dead "fire spec gate for gated phases" block here
                        # that re-checked the identical is_gated condition
                        # and could therefore never fire -- removed as part
                        # of the health_audit.py Theme C fix (SOLID review
                        # priority #3); see
                        # design_docs/phase3_except_exception_survey_findings.md.
                else:
                    logger.warning(
                        f"[HEALTH] Task {task.id[:8]} stuck in_progress with no "
                        f"agent activity for >{self.config.stuck_detection_minutes} "
                        "minutes (including a nudge) — marking failed"
                    )
                    task.status = "failed"
                    task.failure_reason = (
                        f"Task stuck: no agent activity for "
                        f">{self.config.stuck_detection_minutes} minutes"
                    )
                session.commit()

                # Collect cost data for stuck tasks (done or failed) -- the
                # agent consumed LLM tokens before going silent.
                try:
                    from src.services.cost_collection_service import collect_task_cost
                    collect_task_cost(task.id)
                except Exception as e:
                    logger.error(f"[COST-COLLECT] Failed for stuck task {task.id[:8]}: {e}")
        except Exception as e:
            logger.error(f"Error in task stuck detection: {e}")
        finally:
            session.close()
