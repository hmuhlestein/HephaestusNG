"""Workflow-stuck diagnostic agent state machine.

Extracted from MonitoringLoop: cluster G — _check_workflow_stuck_state
and its four helpers (_log_diagnostic_status_report, _create_diagnostic_agent,
_gather_diagnostic_context, _generate_diagnostic_prompt).

See docs/SOLID_OO_REVIEW.md and design_docs/phase_1b_decomposition.md §4.3.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List

from src.core.database import Task, utc_now

logger = logging.getLogger(__name__)


class WorkflowStuckDiagnostics:
    """Detects stuck workflows and prepares diagnostic context."""

    def __init__(self, db_manager, agent_manager, config, phase_manager):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.config = config
        self.phase_manager = phase_manager

    async def check_workflow_stuck_state(self):
        """Check if workflow is stuck and needs diagnostic agent.

        Triggers diagnostic agent if:
        1. Active workflow exists
        2. Task count > 0
        3. All tasks are finished (done/failed/duplicated)
        4. No validated result submitted
        5. Cooldown period has passed since last diagnostic run
        """
        logger.warning(
            "[DIAGNOSTIC MONITOR] ============================================"
        )
        logger.warning("[DIAGNOSTIC MONITOR] 🔍 _check_workflow_stuck_state() CALLED!")
        logger.warning(
            "[DIAGNOSTIC MONITOR] ============================================"
        )
        logger.info("[DIAGNOSTIC MONITOR] Starting workflow stuck state check...")

        # Condition tracking for debug report
        conditions = {
            "enabled": self.config.diagnostic_agent.diagnostic_agent_enabled,
            "workflow_exists": False,
            "has_tasks": False,
            "all_tasks_finished": False,
            "no_validated_result": False,
            "cooldown_passed": False,
            "stuck_long_enough": False,
        }

        if not self.config.diagnostic_agent.diagnostic_agent_enabled:
            logger.info("[DIAGNOSTIC MONITOR] ❌ Diagnostic agent disabled in config")
            self.log_diagnostic_status_report(
                conditions, trigger=False, reason="Disabled in config"
            )
            return

        if not self.phase_manager or not self.phase_manager.workflow_id:
            logger.info("[DIAGNOSTIC MONITOR] ❌ No active workflow")
            self.log_diagnostic_status_report(
                conditions, trigger=False, reason="No active workflow"
            )
            return

        conditions["workflow_exists"] = True
        workflow_id = self.phase_manager.workflow_id
        logger.info(f"[DIAGNOSTIC MONITOR] ✅ Workflow exists: {workflow_id[:8]}")

        session = self.db_manager.get_session()
        try:
            # Step 1: Check if we have tasks
            from src.core.database import DiagnosticRun, Task, WorkflowResult

            tasks = session.query(Task).filter(Task.workflow_id == workflow_id).all()

            if not tasks:
                logger.info("[DIAGNOSTIC MONITOR] ❌ No tasks in workflow yet")
                self.log_diagnostic_status_report(
                    conditions, trigger=False, reason="No tasks in workflow"
                )
                return

            conditions["has_tasks"] = True
            logger.info(f"[DIAGNOSTIC MONITOR] ✅ Has tasks: {len(tasks)} total")

            # Step 2: Check if all tasks are finished
            active_statuses = [
                "pending",
                "assigned",
                "in_progress",
                "under_review",
                "validation_in_progress",
            ]
            active_tasks = [t for t in tasks if t.status in active_statuses]
            finished_tasks = [t for t in tasks if t.status not in active_statuses]

            if active_tasks:
                logger.info(
                    f"[DIAGNOSTIC MONITOR] ❌ Tasks still active: {len(active_tasks)} active, {len(finished_tasks)} finished"
                )
                self.log_diagnostic_status_report(
                    conditions,
                    trigger=False,
                    reason=f"{len(active_tasks)} active tasks remaining",
                )
                return

            conditions["all_tasks_finished"] = True
            logger.info(
                f"[DIAGNOSTIC MONITOR] ✅ All tasks finished: {len(finished_tasks)} tasks"
            )

            # Step 2.5: Check if a phase was recently completed (cooldown after phase completion)
            from src.core.database import PhaseExecution

            recent_phase_completion = (
                session.query(PhaseExecution)
                .filter(
                    PhaseExecution.workflow_execution_id == workflow_id,
                    PhaseExecution.status == "completed",
                    PhaseExecution.completed_at.isnot(None),
                )
                .order_by(PhaseExecution.completed_at.desc())
                .first()
            )

            if recent_phase_completion:
                time_since_completion = (
                    utc_now() - recent_phase_completion.completed_at
                ).total_seconds()
                phase_cooldown = 120  # 2 minutes after phase completion
                if time_since_completion < phase_cooldown:
                    logger.info(
                        f"[DIAGNOSTIC MONITOR] ❌ Phase recently completed ({recent_phase_completion.phase_id[:8]}), cooling down: {time_since_completion:.0f}s / {phase_cooldown}s"
                    )
                    self.log_diagnostic_status_report(
                        conditions,
                        trigger=False,
                        reason=f"Phase completed {time_since_completion:.0f}s ago, cooling down",
                    )
                    return

            # Step 3: Check if workflow is already marked complete/failed
            from src.core.database import Workflow

            wf_row = session.query(Workflow).filter_by(id=workflow_id).first()
            if wf_row and wf_row.status in ("completed", "failed", "cancelled"):
                logger.info(
                    f"[DIAGNOSTIC MONITOR] ❌ Workflow is {wf_row.status} — no diagnostic needed"
                )
                self.log_diagnostic_status_report(
                    conditions,
                    trigger=False,
                    reason=f"Workflow status is {wf_row.status}",
                )
                return

            validated_result = (
                session.query(WorkflowResult)
                .filter(
                    WorkflowResult.workflow_id == workflow_id,
                    WorkflowResult.status == "validated",
                )
                .first()
            )

            if validated_result:
                logger.info(
                    f"[DIAGNOSTIC MONITOR] ❌ Workflow has validated result: {validated_result.id[:8]}"
                )
                self.log_diagnostic_status_report(
                    conditions, trigger=False, reason="Validated result exists"
                )
                return

            conditions["no_validated_result"] = True

            # Check for any results (validated or not)
            all_results = (
                session.query(WorkflowResult)
                .filter(WorkflowResult.workflow_id == workflow_id)
                .all()
            )
            if all_results:
                logger.info(
                    f"[DIAGNOSTIC MONITOR] ✅ No validated result ({len(all_results)} unvalidated results exist)"
                )
            else:
                logger.info(
                    "[DIAGNOSTIC MONITOR] ✅ No validated result (no results submitted)"
                )

            # Step 4: Check cooldown period
            last_diagnostic = (
                session.query(DiagnosticRun)
                .filter(DiagnosticRun.workflow_id == workflow_id)
                .order_by(DiagnosticRun.triggered_at.desc())
                .first()
            )

            if last_diagnostic:
                time_since_last = (
                    utc_now() - last_diagnostic.triggered_at
                ).total_seconds()
                if time_since_last < self.config.diagnostic_agent.diagnostic_cooldown_seconds:
                    logger.info(
                        f"[DIAGNOSTIC MONITOR] ❌ Cooldown active: {time_since_last:.0f}s / {self.config.diagnostic_agent.diagnostic_cooldown_seconds}s required"
                    )
                    self.log_diagnostic_status_report(
                        conditions,
                        trigger=False,
                        reason=f"Cooldown active ({time_since_last:.0f}s < {self.config.diagnostic_agent.diagnostic_cooldown_seconds}s)",
                    )
                    return
                else:
                    logger.info(
                        f"[DIAGNOSTIC MONITOR] ✅ Cooldown passed: {time_since_last:.0f}s since last diagnostic"
                    )
            else:
                logger.info(
                    "[DIAGNOSTIC MONITOR] ✅ Cooldown passed: No previous diagnostic runs"
                )

            conditions["cooldown_passed"] = True

            # Step 5: Check how long we've been stuck
            latest_task_time = max(
                (
                    t.completed_at or t.created_at
                    for t in tasks
                    if t.completed_at or t.created_at
                ),
                default=None,
            )

            stuck_time = 0
            if latest_task_time:
                stuck_time = (utc_now() - latest_task_time).total_seconds()
                if stuck_time < self.config.diagnostic_agent.diagnostic_min_stuck_time_seconds:
                    logger.info(
                        f"[DIAGNOSTIC MONITOR] ❌ Not stuck long enough: {stuck_time:.0f}s / {self.config.diagnostic_agent.diagnostic_min_stuck_time_seconds}s required"
                    )
                    self.log_diagnostic_status_report(
                        conditions,
                        trigger=False,
                        reason=f"Not stuck long enough ({stuck_time:.0f}s < {self.config.diagnostic_agent.diagnostic_min_stuck_time_seconds}s)",
                    )
                    return
                else:
                    logger.info(
                        f"[DIAGNOSTIC MONITOR] ✅ Stuck long enough: {stuck_time:.0f}s since last activity"
                    )
            else:
                logger.warning(
                    "[DIAGNOSTIC MONITOR] ⚠️  Could not determine stuck time (no task timestamps)"
                )

            conditions["stuck_long_enough"] = True

            # ALL CONDITIONS MET - Trigger diagnostic agent
            logger.warning(
                "[DIAGNOSTIC MONITOR] 🚨 WORKFLOW STUCK DETECTED - All conditions met!"
            )
            logger.warning(
                f"[DIAGNOSTIC MONITOR] 🔥 Stuck for {stuck_time:.0f}s with no progress"
            )
            self.log_diagnostic_status_report(
                conditions, trigger=True, stuck_time=stuck_time
            )

            await self.create_diagnostic_agent(workflow_id, tasks, stuck_time)

        except Exception as e:
            logger.error(
                f"[DIAGNOSTIC MONITOR] ❌ Error checking workflow stuck state: {e}",
                exc_info=True,
            )
            session.rollback()
        finally:
            session.close()


    def log_diagnostic_status_report(
        self,
        conditions: Dict[str, bool],
        trigger: bool,
        reason: str = None,
        stuck_time: float = 0,
    ):
        """Log a status report of all diagnostic conditions.

        Args:
            conditions: Dictionary of condition name -> boolean
            trigger: Whether diagnostic agent was triggered
            reason: Reason for not triggering (if trigger=False)
            stuck_time: How long stuck (if trigger=True)
        """
        logger.info("[DIAGNOSTIC MONITOR] ═══════════════════════════════════════")
        logger.info("[DIAGNOSTIC MONITOR] DIAGNOSTIC STATUS REPORT")
        logger.info("[DIAGNOSTIC MONITOR] ───────────────────────────────────────")

        # Show all conditions
        logger.info(
            f"[DIAGNOSTIC MONITOR] Enabled:              {'✅' if conditions['enabled'] else '❌'}"
        )
        logger.info(
            f"[DIAGNOSTIC MONITOR] Workflow Exists:      {'✅' if conditions['workflow_exists'] else '❌'}"
        )
        logger.info(
            f"[DIAGNOSTIC MONITOR] Has Tasks:            {'✅' if conditions['has_tasks'] else '❌'}"
        )
        logger.info(
            f"[DIAGNOSTIC MONITOR] All Tasks Finished:   {'✅' if conditions['all_tasks_finished'] else '❌'}"
        )
        logger.info(
            f"[DIAGNOSTIC MONITOR] No Validated Result:  {'✅' if conditions['no_validated_result'] else '❌'}"
        )
        logger.info(
            f"[DIAGNOSTIC MONITOR] Cooldown Passed:      {'✅' if conditions['cooldown_passed'] else '❌'}"
        )
        logger.info(
            f"[DIAGNOSTIC MONITOR] Stuck Long Enough:    {'✅' if conditions['stuck_long_enough'] else '❌'}"
        )

        logger.info("[DIAGNOSTIC MONITOR] ───────────────────────────────────────")

        if trigger:
            logger.warning(
                "[DIAGNOSTIC MONITOR] 🚨 RESULT: TRIGGERING DIAGNOSTIC AGENT"
            )
            logger.warning(f"[DIAGNOSTIC MONITOR] 🔥 Stuck Time: {stuck_time:.0f}s")
        else:
            logger.info("[DIAGNOSTIC MONITOR] ✋ RESULT: NOT TRIGGERING")
            if reason:
                logger.info(f"[DIAGNOSTIC MONITOR] 📋 Reason: {reason}")

        logger.info("[DIAGNOSTIC MONITOR] ═══════════════════════════════════════")


    async def create_diagnostic_agent(
        self, workflow_id: str, workflow_tasks: List, stuck_time: float
    ):
        """Log a stalled workflow without creating extra tasks.

        Diagnostic tasks polluted the task list, got restarted on resume,
        and wasted agents. Now we just log and let the pipeline's own
        retry logic handle recovery.

        A prior version of this method tried to mark in_progress/assigned
        tasks with terminated agents as failed directly here, but this
        caller only ever runs after confirming zero tasks are in an active
        status (see the all_tasks_finished gate above) -- so that branch
        could never fire. The real, working version of that logic is
        _clean_stale_assigned_tasks in src/autopilot/orchestrator.py,
        called every tick from background_phase_advancement_sweep.
        """
        logger.warning(
            f"[DIAGNOSTIC MONITOR] Workflow {workflow_id[:8]} stuck for "
            f"{stuck_time:.0f}s — no diagnostic task created, "
            f"pipeline retry logic will handle recovery"
        )


    async def gather_diagnostic_context(
        self, workflow_id: str, workflow_tasks: List, stuck_time: float
    ) -> Dict[str, Any]:
        """Gather all context needed for diagnostic agent.

        Returns:
            Dictionary with:
            - workflow_goal
            - phases_summary
            - recent_agents_history
            - conductor_overviews
            - workflow_status
            - submitted_results
        """
        from src.core.database import Agent, ConductorAnalysis, Phase, WorkflowResult

        session = self.db_manager.get_session()
        try:
            # Get workflow config
            workflow_config = self.phase_manager.get_workflow_config(workflow_id)
            workflow_goal = (
                workflow_config.result_criteria if workflow_config else "Unknown goal"
            )

            # Get all phases
            phases = (
                session.query(Phase)
                .filter(Phase.workflow_id == workflow_id)
                .order_by(Phase.order)
                .all()
            )

            phases_summary = []
            for phase in phases:
                phases_summary.append(
                    {
                        "id": phase.id,
                        "name": phase.name,
                        "order": phase.order,
                        "description": phase.description,
                        "done_definitions": phase.done_definitions,
                        "task_count": len(
                            [t for t in workflow_tasks if t.phase_id == phase.id]
                        ),
                        "done_task_count": len(
                            [
                                t
                                for t in workflow_tasks
                                if t.phase_id == phase.id and t.status == "done"
                            ]
                        ),
                    }
                )

            # Get recent agents (last N completed/failed)
            task_ids = [t.id for t in workflow_tasks]
            recent_agents = (
                session.query(Agent)
                .filter(
                    Agent.current_task_id.in_(task_ids),
                    Agent.status.in_(["terminated"]),
                )
                .order_by(Agent.created_at.desc())
                .limit(self.config.diagnostic_agent.diagnostic_max_agents_to_analyze)
                .all()
            )

            agents_summary = []
            for agent in recent_agents:
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task:
                    agents_summary.append(
                        {
                            "agent_id": agent.id,
                            "task_id": task.id,
                            "task_description": task.enriched_description
                            or task.raw_description,
                            "task_status": task.status,
                            "completion_notes": task.completion_notes,
                            "failure_reason": task.failure_reason,
                            "phase_id": task.phase_id,
                            "created_at": agent.created_at.isoformat(),
                            "agent_type": agent.agent_type,
                        }
                    )

            # Get recent Conductor analyses
            conductor_analyses = (
                session.query(ConductorAnalysis)
                .order_by(ConductorAnalysis.timestamp.desc())
                .limit(self.config.diagnostic_agent.diagnostic_max_conductor_analyses)
                .all()
            )

            conductor_overviews = []
            for analysis in conductor_analyses:
                conductor_overviews.append(
                    {
                        "timestamp": analysis.timestamp.isoformat(),
                        "system_status": analysis.system_status,
                        "coherence_score": analysis.coherence_score,
                        "num_agents": analysis.num_agents,
                        "duplicate_count": analysis.duplicate_count,
                    }
                )

            # Get submitted results (even if rejected)
            submitted_results = (
                session.query(WorkflowResult)
                .filter(WorkflowResult.workflow_id == workflow_id)
                .all()
            )

            results_summary = []
            for result in submitted_results:
                results_summary.append(
                    {
                        "result_id": result.id,
                        "status": result.status,
                        "submitted_at": result.created_at.isoformat()
                        if result.created_at
                        else None,
                        "validation_feedback": result.validation_feedback,
                        "agent_id": result.agent_id,
                    }
                )

            # Calculate task statistics by phase
            tasks_by_phase = {}
            for phase in phases:
                phase_tasks = [t for t in workflow_tasks if t.phase_id == phase.id]
                tasks_by_phase[phase.name] = {
                    "total": len(phase_tasks),
                    "done": len([t for t in phase_tasks if t.status == "done"]),
                    "failed": len([t for t in phase_tasks if t.status == "failed"]),
                }

            return {
                "workflow_goal": workflow_goal,
                "workflow_id": workflow_id,
                "phases_summary": phases_summary,
                "agents_summary": agents_summary,
                "conductor_overviews": conductor_overviews,
                "submitted_results": results_summary,
                "total_tasks": len(workflow_tasks),
                "tasks_by_phase": tasks_by_phase,
                "time_since_last_task": stuck_time,
            }

        finally:
            session.close()


    async def generate_diagnostic_prompt(self, context: Dict[str, Any]) -> str:
        """Generate diagnostic prompt from template.

        Args:
            context: Diagnostic context dictionary

        Returns:
            Formatted diagnostic prompt
        """

        # Load template
        template_path = (
            Path(__file__).parent.parent / "prompts" / "diagnostic_agent_analysis.md"
        )
        with open(template_path, "r") as f:
            template = f.read()

        # Format phases info
        phases_info = []
        for phase in context["phases_summary"]:
            phases_info.append(f"""
### Phase {phase["order"]}: {phase["name"]} (ID: {phase["id"][:8]})

**Description**: {phase["description"]}

**Done Definitions**:
{chr(10).join(f"- {d}" for d in phase["done_definitions"])}

**Progress**: {phase["done_task_count"]}/{phase["task_count"]} tasks completed
""")

        # Format agent history
        agents_history = []
        for i, agent in enumerate(context["agents_summary"], 1):
            status_marker = "✅" if agent["task_status"] == "done" else "❌"
            agents_history.append(f"""
**Agent {i}** (ID: {agent["agent_id"][:8]}, Type: {agent["agent_type"]})
- **Task**: {agent["task_description"]}
- **Status**: {status_marker} {agent["task_status"]}
- **Phase**: {agent["phase_id"][:8] if agent["phase_id"] else "None"}
- **Completed at**: {agent["created_at"]}
{f"- **Notes**: {agent['completion_notes']}" if agent["completion_notes"] else ""}
{f"- **Failure reason**: {agent['failure_reason']}" if agent["failure_reason"] else ""}
""")

        # Format conductor overviews
        conductor_overviews = []
        for i, overview in enumerate(context["conductor_overviews"], 1):
            conductor_overviews.append(f"""
**Analysis {i}** ({overview["timestamp"]}):
- System status: {overview["system_status"]}
- Coherence score: {overview["coherence_score"]:.2f}
- Active agents: {overview["num_agents"]}
- Duplicates detected: {overview["duplicate_count"]}
""")

        # Format tasks by phase
        tasks_by_phase_str = []
        for phase_name, stats in context["tasks_by_phase"].items():
            tasks_by_phase_str.append(
                f"  - {phase_name}: {stats['done']}/{stats['total']} done, {stats['failed']} failed"
            )

        # Format submitted results
        if context["submitted_results"]:
            results_info = []
            for result in context["submitted_results"]:
                status_marker = "✅" if result["status"] == "validated" else "❌"
                results_info.append(f"""
- {status_marker} Result {result["result_id"][:8]}: {result["status"]}
  - Submitted: {result["submitted_at"]}
  - Feedback: {result["validation_feedback"] or "None"}
""")
            submitted_results_info = "\n".join(results_info)
        else:
            submitted_results_info = "No results have been submitted yet."

        # Calculate stuck time formatting
        stuck_seconds = context.get("time_since_last_task", 0)
        if stuck_seconds >= 3600:
            stuck_time_formatted = f"{stuck_seconds / 3600:.1f} hours"
        elif stuck_seconds >= 60:
            stuck_time_formatted = f"{stuck_seconds / 60:.1f} minutes"
        else:
            stuck_time_formatted = f"{stuck_seconds} seconds"

        # Replace placeholders
        prompt = template.format(
            workflow_goal=context["workflow_goal"],
            workflow_id=context["workflow_id"],
            phases_info="\n".join(phases_info),
            agent_count=len(context["agents_summary"]),
            agents_history="\n".join(agents_history)
            if agents_history
            else "No agents have run yet.",
            conductor_overviews="\n".join(conductor_overviews)
            if conductor_overviews
            else "No conductor analyses available.",
            total_tasks=context["total_tasks"],
            tasks_by_phase="\n".join(tasks_by_phase_str),
            stuck_time_formatted=stuck_time_formatted,
            submitted_results_info=submitted_results_info,
            agent_id="{agent_id}",  # Will be replaced by agent manager
            task_id="{task_id}",  # Will be replaced by agent manager
        )

        return prompt
