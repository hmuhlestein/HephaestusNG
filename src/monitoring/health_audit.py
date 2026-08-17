"""System health audit — DB-driven stuck-task detection and nudging.

Extracted from MonitoringLoop: cluster F — _audit_system_health was a
standalone concern that owns _stuck_task_nudges and _health_findings
as its own instance attributes rather than MonitoringLoop's.

See docs/SOLID_OO_REVIEW.md and design_docs/phase_1b_decomposition.md §4.3.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src.core.database import Agent, Task
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
        from src.mcp.autopilot.control_routes import run_health_audit

        result = run_health_audit(self.db_manager)

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
                        # Fire spec gate for gated phases so phase execution
                        # is properly marked as completed
                        try:
                            from src.autopilot.spec import GATED_PHASES, build_phase_output
                            from src.core.database import Phase as _Phase
                            from pathlib import Path as _Path
                            _phase = session.query(_Phase).filter_by(id=task.phase_id).first() if task.phase_id else None
                            if _phase and _phase.name in GATED_PHASES:
                                _wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
                                if _wf and _wf.working_directory:
                                    phase_output = build_phase_output(_phase.name, _Path(_wf.working_directory), skip_independent_verification=True)
                                    from src.core.database import DatabaseManager as _DbMgr
                                    from src.phases import PhaseManager
                                    pm = PhaseManager(_DbMgr())
                                    pm.workflow_id = task.workflow_id
                                    pm.mark_phase_complete(_phase.id, "Phase completed (monitor promoted stuck task)", phase_output=phase_output)
                        except Exception as e:
                            logger.error(f"[HEALTH] Failed to fire spec gate for task {task.id[:8]}: {e}")
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
