"""Intelligent monitoring and self-healing system for Hephaestus."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.agents.manager import AgentManager
from src.core.constants import HEPHAESTUS_LOGS_DIR
from src.core.database import (
    Agent,
    AgentLog,
    ConductorAnalysis,
    DatabaseManager,
    DetectedDuplicate,
    Task,
)
from src.core.simple_config import get_config
from src.interfaces import LLMProviderInterface
from src.monitoring.conductor import Conductor
from src.monitoring.guardian import Guardian
from src.phases import PhaseManager

logger = logging.getLogger(__name__)

# How many idle-nudges a stuck task gets before "the agent produced output"
# stops being trusted as "the agent made progress" -- see the stuck-task
# nudge cap in _audit_system_health's own comment for the failure mode this
# closes (an agent that keeps replying without ever calling
# complete_my_task resets the idle check forever on activity alone).
MAX_STUCK_TASK_NUDGES = 3


class MonitoringLoop:
    """Main monitoring loop for the system with trajectory monitoring."""

    UNCONFIRMED_COMPLETION_ESCALATE_AFTER = 3

    # SOLID review finding 3.5: the mechanical-recovery sweep in
    # _monitoring_cycle used to hardcode a 12-call sequential if-chain
    # instead of iterating a list -- adding a new check meant editing that
    # chain by hand, easy to get the early-exit-vs-accumulate semantics
    # wrong. All 12 already share the same async (agent) -> bool shape
    # (mechanical_recovery.py's own detect+intervene methods, exposed here
    # via thin delegators tests call directly -- see tests/test_monitor.py),
    # so a plain ordered name list is enough; no new Protocol/ABC needed for
    # a shape every method already satisfies structurally.
    #
    # Early-exit checks: the first one to fire skips every later check for
    # this agent this cycle -- these three conditions mean the agent isn't
    # in a normal working state at all, so none of the CLI-interaction
    # checks below make sense to run against it this cycle.
    _EARLY_EXIT_CHECKS = (
        "_detect_orphaned_idle_agent",
        "_detect_credit_exhausted",
        "_detect_agent_never_started",
    )
    # Accumulating checks: every one runs regardless of the others' results
    # (an agent can match more than one condition in the same cycle).
    # "_verify_cli_model_fallback" returns None (never adds to the
    # intervened set) rather than bool, but fits this same iteration
    # unmodified -- bool(None) is False like every other non-firing check.
    _ACCUMULATING_CHECKS = (
        "_mechanical_recovery_for_agent",
        "_detect_cli_model_fallback",
        "_verify_cli_model_fallback",
        "_detect_repetition_loop",
        "_detect_dangerous_command_confirmation",
        "_detect_max_token_limit_error",
        "_detect_unconfirmed_task_completion",
        "_detect_mcp_disconnected",
        "_detect_connection_errors",
        "_detect_bad_model_error",
    )

    def __init__(
        self,
        db_manager: DatabaseManager,
        agent_manager: AgentManager,
        llm_provider: LLMProviderInterface,
        phase_manager: Optional[PhaseManager] = None,
    ):
        """Initialize monitoring loop with trajectory monitoring.

        Args:
            db_manager: Database manager
            agent_manager: Agent manager
            llm_provider: LLM provider
            phase_manager: Optional phase manager for workflow monitoring
        """
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.phase_manager = phase_manager
        self.llm_provider = llm_provider

        # Initialize trajectory monitoring components
        self.guardian = Guardian(
            db_manager=db_manager,
            agent_manager=agent_manager,
            llm_provider=llm_provider,
        )
        self.conductor = Conductor(
            db_manager=db_manager,
            agent_manager=agent_manager,
            llm_provider=llm_provider,
        )

        self.config = get_config()
        self.running = False

        # Cache for Guardian summaries
        self.guardian_summaries_cache: Dict[str, Dict[str, Any]] = {}

        # Orphaned tmux session reconciliation collaborator (SOLID review
        # 3.4) — _cleanup_orphaned_tmux_sessions below delegates to this.
        from src.monitoring.orphan_reaper import OrphanSessionReaper

        self._orphan_reaper = OrphanSessionReaper(db_manager, agent_manager)

        # Phase 1b decomposition collaborators (SOLID review / design doc
        # phase_1b_decomposition.md §4.3).  Each collaborator owns a
        # cluster of methods formerly inlined in MonitoringLoop; delegator
        # stubs below forward the old underscored names for test compat.
        from src.monitoring.auto_restart import AutoRestart
        from src.monitoring.diagnostic_agent import WorkflowStuckDiagnostics
        from src.monitoring.guardian_dispatch import GuardianDispatcher
        from src.monitoring.health_audit import SystemHealthAuditor
        from src.monitoring.mechanical_recovery import MechanicalRecoveryDetector

        self._auto_restart = AutoRestart(db_manager, agent_manager, self.guardian)
        self._mechanical_recovery = MechanicalRecoveryDetector(
            db_manager, agent_manager, self.config, self._auto_restart
        )
        self._guardian_dispatch = GuardianDispatcher(
            db_manager, agent_manager, self.config, self.guardian,
            phase_manager, self._auto_restart,
            self.guardian_summaries_cache,
        )
        self._health_auditor = SystemHealthAuditor(db_manager, agent_manager, self.config)
        self._diagnostics = WorkflowStuckDiagnostics(
            db_manager, agent_manager, self.config, phase_manager
        )

    # ---- Test-visible attribute properties ----
    # Tests directly read/write these attributes on MonitoringLoop instances.
    # Each property delegates to the owning collaborator so that mutations
    # (e.g. make_monitoring_loop._stuck_state["a1"] = {...}) are visible
    # to the collaborator's methods through the same dict object.

    @property
    def _stuck_task_nudges(self):
        return self._health_auditor._stuck_task_nudges

    @_stuck_task_nudges.setter
    def _stuck_task_nudges(self, value):
        self._health_auditor._stuck_task_nudges = value

    @property
    def _stuck_state(self):
        return self._mechanical_recovery._stuck_state

    @_stuck_state.setter
    def _stuck_state(self, value):
        self._mechanical_recovery._stuck_state = value

    @property
    def _switched_to_fallback_model(self):
        return self._mechanical_recovery._switched_to_fallback_model

    @_switched_to_fallback_model.setter
    def _switched_to_fallback_model(self, value):
        self._mechanical_recovery._switched_to_fallback_model = value

    @property
    def _fallback_attempt_count(self):
        return self._mechanical_recovery._fallback_attempt_count

    @_fallback_attempt_count.setter
    def _fallback_attempt_count(self, value):
        self._mechanical_recovery._fallback_attempt_count = value

    @property
    def _pending_fallback_verification(self):
        return self._mechanical_recovery._pending_fallback_verification

    @_pending_fallback_verification.setter
    def _pending_fallback_verification(self, value):
        self._mechanical_recovery._pending_fallback_verification = value

    @property
    def _denied_dangerous_cmds(self):
        return self._mechanical_recovery._denied_dangerous_cmds

    @_denied_dangerous_cmds.setter
    def _denied_dangerous_cmds(self, value):
        self._mechanical_recovery._denied_dangerous_cmds = value

    @property
    def _nudged_token_limit(self):
        return self._mechanical_recovery._nudged_token_limit

    @_nudged_token_limit.setter
    def _nudged_token_limit(self, value):
        self._mechanical_recovery._nudged_token_limit = value

    @property
    def _nudged_unconfirmed_completion(self):
        return self._mechanical_recovery._nudged_unconfirmed_completion

    @_nudged_unconfirmed_completion.setter
    def _nudged_unconfirmed_completion(self, value):
        self._mechanical_recovery._nudged_unconfirmed_completion = value

    @property
    def _nudged_mcp_disconnected(self):
        return self._mechanical_recovery._nudged_mcp_disconnected

    @_nudged_mcp_disconnected.setter
    def _nudged_mcp_disconnected(self, value):
        self._mechanical_recovery._nudged_mcp_disconnected = value

    @property
    def _health_findings(self):
        return self._health_auditor._health_findings

    @_health_findings.setter
    def _health_findings(self, value):
        self._health_auditor._health_findings = value

    async def start(self):
        """Start the monitoring loop."""
        self.running = True
        logger.info("Starting monitoring loop")

        while self.running:
            try:
                # Write heartbeat file so external watchdogs can verify we're alive
                heartbeat = Path(HEPHAESTUS_LOGS_DIR) / "monitor_heartbeat"
                heartbeat.write_text(str(time.time()))

                await self._monitoring_cycle()
            except Exception as e:
                logger.error(f"Error in monitoring cycle: {e}", exc_info=True)

            # Wait for next cycle
            await asyncio.sleep(self.config.monitoring_interval_seconds)

    async def stop(self):
        """Stop the monitoring loop."""
        logger.info("Stopping monitoring loop")
        self.running = False

    async def _mechanical_recovery_for_agent(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.mechanical_recovery_for_agent()."""
        return await self._mechanical_recovery.mechanical_recovery_for_agent(*args, **kwargs)


    async def _detect_cli_model_fallback(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_cli_model_fallback()."""
        return await self._mechanical_recovery.detect_cli_model_fallback(*args, **kwargs)


    async def _verify_cli_model_fallback(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.verify_cli_model_fallback()."""
        return await self._mechanical_recovery.verify_cli_model_fallback(*args, **kwargs)


    def _log_agent_event(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.log_agent_event()."""
        return self._mechanical_recovery.log_agent_event(*args, **kwargs)


    async def _detect_repetition_loop(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_repetition_loop()."""
        return await self._mechanical_recovery.detect_repetition_loop(*args, **kwargs)


    async def _detect_dangerous_command_confirmation(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_dangerous_command_confirmation()."""
        return await self._mechanical_recovery.detect_dangerous_command_confirmation(*args, **kwargs)


    async def _detect_max_token_limit_error(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_max_token_limit_error()."""
        return await self._mechanical_recovery.detect_max_token_limit_error(*args, **kwargs)


    async def _detect_unconfirmed_task_completion(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_unconfirmed_task_completion()."""
        return await self._mechanical_recovery.detect_unconfirmed_task_completion(*args, **kwargs)


    async def _detect_mcp_disconnected(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_mcp_disconnected()."""
        return await self._mechanical_recovery.detect_mcp_disconnected(*args, **kwargs)


    async def _detect_connection_errors(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_connection_errors()."""
        return await self._mechanical_recovery.detect_connection_errors(*args, **kwargs)


    async def _detect_bad_model_error(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_bad_model_error()."""
        return await self._mechanical_recovery.detect_bad_model_error(*args, **kwargs)


    async def _detect_orphaned_idle_agent(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_orphaned_idle_agent()."""
        return await self._mechanical_recovery.detect_orphaned_idle_agent(*args, **kwargs)


    async def _detect_credit_exhausted(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_credit_exhausted()."""
        return await self._mechanical_recovery.detect_credit_exhausted(*args, **kwargs)


    async def _detect_agent_never_started(self, *args, **kwargs):
        """Delegator to _mechanical_recovery.detect_agent_never_started()."""
        return await self._mechanical_recovery.detect_agent_never_started(*args, **kwargs)


    async def _monitoring_cycle(self):
        """Execute one monitoring cycle with trajectory monitoring."""
        logger.debug("Starting trajectory monitoring cycle")

        # DEBUG: Log phase_manager status
        logger.info(
            f"[DIAGNOSTIC CYCLE] phase_manager exists: {self.phase_manager is not None}"
        )
        if self.phase_manager:
            logger.info(
                f"[DIAGNOSTIC CYCLE] phase_manager.workflow_id: {self.phase_manager.workflow_id[:8] if self.phase_manager.workflow_id else 'None'}"
            )
        else:
            logger.info("[DIAGNOSTIC CYCLE] phase_manager is None")

        # Get all active agents
        agents = self.agent_manager.get_active_agents()
        logger.info(f"Trajectory monitoring {len(agents)} active agents")

        # Phase 0: cheap mechanical recovery (no LLM). Nine complementary checks:
        #   a) OpenRouter credits exhausted — pause workflow + terminate
        #      immediately, before any other check wastes a recovery attempt
        #      on an agent that's about to be torn down anyway
        #   b) never started — zero output since launch, ≥4 min — terminate,
        #      reset to pending; uses persisted Agent timestamps so it works
        #      correctly even right after a restart, unlike (c) below
        #   c) frozen output — same substantive 40-line sig for ≥5 min
        #   d) agent frozen on its default model, CLI supports an in-session
        #      switch (polymorphic, CLIAgentInterface.model_fallback_keystrokes) —
        #      switch it to a configured fallback model rather than nudging
        #      (which does nothing for an agent that isn't stuck, just
        #      waiting), reusing (c)'s own frozen-duration state
        #   e) repetition loop — output growing but same sentence repeats 5+ times
        #      in the last 80 lines (LLM cycling "Actually, let me try…")
        #   f) pending rm confirmation — auto-deny immediately, don't wait for (c)
        #   g) max output token limit hit — nudge immediately, don't wait for (c)
        #   h) completion call apparently sent but task still non-terminal —
        #      nudge to retry immediately, don't wait for (c)
        #   i) MCP server disconnected — nudge to `mcp connect`, don't wait for (c)
        #   j) Claude Code rejected its launch model — fix directly with a
        #      real `/model <x>` keystroke send, since the agent can't
        #      invoke that slash command itself
        mechanically_intervened = set()
        for agent in agents:
            for check_name in self._EARLY_EXIT_CHECKS:
                if await getattr(self, check_name)(agent):
                    mechanically_intervened.add(agent.id)
                    break
            else:
                for check_name in self._ACCUMULATING_CHECKS:
                    if await getattr(self, check_name)(agent):
                        mechanically_intervened.add(agent.id)

        # Phase 1: Guardian Analysis (Parallel)
        guardian_summaries = []
        guardian_tasks = []

        for agent in agents:
            if agent.id in mechanically_intervened:
                # Mechanical recovery already nudged/restarted/terminated
                # this agent this cycle -- running Guardian immediately
                # afterward on the same pre-intervention `agents` snapshot
                # double-intervenes: a redundant nudge on top of the one
                # just sent, or worse, a "missing tmux session" false
                # positive reviving an agent mechanical recovery just
                # deliberately terminated and failed. Let the next cycle
                # re-evaluate with fresh state instead.
                logger.debug(
                    f"Skipping Guardian analysis for agent {agent.id[:8]} -- "
                    "mechanical recovery already intervened this cycle"
                )
                continue
            # Create async task for each Guardian analysis
            task = asyncio.create_task(self._guardian_analysis_for_agent(agent))
            guardian_tasks.append(task)

        # Wait for all Guardian analyses to complete
        if guardian_tasks:
            guardian_results = await asyncio.gather(
                *guardian_tasks, return_exceptions=True
            )

            # Filter out exceptions and None results
            guardian_summaries = [
                result
                for result in guardian_results
                if result and not isinstance(result, Exception)
            ]

            # Log any exceptions
            for i, result in enumerate(guardian_results):
                if isinstance(result, Exception):
                    logger.error(
                        f"Guardian analysis failed for agent {agents[i].id}: {result}"
                    )

        # Debug: Log what we collected
        logger.info(f"DEBUG - Collected {len(guardian_summaries)} Guardian summaries")
        for i, summary in enumerate(guardian_summaries):
            if summary:
                logger.info(
                    f"DEBUG - Summary {i}: agent_id={summary.get('agent_id')}, "
                    f"has_trajectory_summary={bool(summary.get('trajectory_summary'))}"
                )

        # Phase 2: Conductor Analysis (if we have summaries)
        if guardian_summaries:
            try:
                logger.info(
                    f"DEBUG - Passing {len(guardian_summaries)} summaries to Conductor"
                )
                conductor_analysis = await self.conductor.analyze_system_state(
                    guardian_summaries
                )

                # Log system status
                logger.info(f"System Status: {conductor_analysis['system_status']}")

                # Save Conductor analysis to dedicated table
                await self._save_conductor_analysis(conductor_analysis)

                # Execute conductor decisions
                if conductor_analysis.get("decisions"):
                    await self.conductor.execute_decisions(
                        conductor_analysis["decisions"]
                    )

                # Generate and log detailed report if needed
                if conductor_analysis.get("coherence", {}).get("score", 1.0) < 0.5:
                    report = await self.conductor.generate_detailed_report(
                        conductor_analysis
                    )
                    logger.warning(f"Low system coherence detected:\n{report}")

            except Exception as e:
                logger.error(f"Conductor analysis failed: {e}")

        # Clean up orphaned tmux sessions
        try:
            await self._cleanup_orphaned_tmux_sessions()
        except Exception as e:
            logger.error(f"Error cleaning up orphaned tmux sessions: {e}")

        # Auto-discover active workflow if phase_manager has no workflow_id
        if self.phase_manager and not self.phase_manager.workflow_id:
            logger.info(
                "[AUTO-DISCOVER] phase_manager.workflow_id is None, checking for active workflows..."
            )
            try:
                wf_id = self.phase_manager.load_active_workflow()
                if wf_id:
                    logger.info(
                        f"[AUTO-DISCOVER] ✅ Loaded active workflow: {wf_id[:8]}..."
                    )
            except Exception as e:
                logger.warning(f"[AUTO-DISCOVER] Failed to load active workflow: {e}")

        # Offloaded -- does blocking DB queries directly on the event loop
        # otherwise, same class of issue fixed elsewhere in this codebase.
        await asyncio.get_event_loop().run_in_executor(
            None, self._maybe_switch_tracked_workflow
        )

        # Propagate phase_manager to agent_manager so spawned agents get phase context
        if self.phase_manager and self.agent_manager and not self.agent_manager.phase_manager:
            self.agent_manager.phase_manager = self.phase_manager

        # Phase progression is now handled by the orchestrator (_advance_phases).
        # The monitor no longer creates tasks or advances phases.

        # Check if workflow is stuck and needs diagnostic agent
        logger.info("[DIAGNOSTIC] Checking if diagnostic agent needed...")
        logger.info(
            f"[DIAGNOSTIC] phase_manager exists: {self.phase_manager is not None}"
        )
        logger.info(
            f"[DIAGNOSTIC] workflow_id: {self.phase_manager.workflow_id[:8] if (self.phase_manager and self.phase_manager.workflow_id) else 'N/A'}"
        )

        # Phase 3: System Health Audit
        try:
            await self._audit_system_health()
        except Exception as e:
            logger.error(f"Error in system health audit: {e}")

        # Offloaded -- does blocking DB queries directly on the event loop
        # otherwise, same class of issue fixed elsewhere in this codebase.
        await asyncio.get_event_loop().run_in_executor(
            None, self._log_active_workflow_diagnostics
        )

        if self.phase_manager and self.phase_manager.workflow_id:
            logger.info(
                f"[DIAGNOSTIC] ✅ Conditions met - running diagnostic check for workflow {self.phase_manager.workflow_id[:8]}"
            )
            try:
                await self._check_workflow_stuck_state()
            except Exception as e:
                logger.error(f"[DIAGNOSTIC] Error checking workflow stuck state: {e}")
        else:
            if not self.phase_manager:
                logger.warning("[DIAGNOSTIC] ❌ SKIPPED - No phase_manager")
            elif not self.phase_manager.workflow_id:
                logger.warning(
                    "[DIAGNOSTIC] ❌ SKIPPED - phase_manager.workflow_id is None"
                )
                logger.warning(
                    "[DIAGNOSTIC] 💡 This likely means there's an active workflow in the DB that wasn't loaded on startup"
                )

    def _maybe_switch_tracked_workflow(self) -> None:
        """Check if tracked workflow is still the most recent active one.

        When the pipeline restarts with a new design, it launches a new
        workflow. The monitor should switch to track the new workflow
        instead of the old one. Extracted from _monitoring_cycle (SOLID
        review 3.4) -- inline DB-querying business logic that had grown
        alongside the method's scheduling/coordination role.
        """
        if not (self.phase_manager and self.phase_manager.workflow_id):
            return
        try:
            session = self.db_manager.get_session()
            from src.core.database import Workflow
            try:
                # Get the tracked workflow's status
                tracked_wf = session.query(Workflow).filter_by(id=self.phase_manager.workflow_id).first()
                # Find the most recent active workflow
                latest_active = (
                    session.query(Workflow)
                    .filter_by(status="active")
                    .order_by(Workflow.created_at.desc())
                    .first()
                )
                if latest_active and latest_active.id != self.phase_manager.workflow_id:
                    # A newer active workflow exists — switch to it
                    logger.info(
                        f"[WORKFLOW-SWITCH] Tracked workflow {self.phase_manager.workflow_id[:8]} "
                        f"is {tracked_wf.status if tracked_wf else 'unknown'}, "
                        f"switching to newer active workflow {latest_active.id[:8]}"
                    )
                    self.phase_manager.workflow_id = latest_active.id
                    self.phase_manager.active_workflow = None  # Force reload
                    self.phase_manager.load_active_workflow()
                elif tracked_wf and tracked_wf.status in ("completed", "failed", "paused") and not latest_active:
                    # Tracked workflow is done and no new active workflow — clear
                    logger.info(
                        f"[WORKFLOW-SWITCH] Tracked workflow {self.phase_manager.workflow_id[:8]} "
                        f"is {tracked_wf.status} with no active workflows — clearing"
                    )
                    self.phase_manager.workflow_id = None
            finally:
                session.close()
        except Exception as e:
            logger.error(f"[WORKFLOW-SWITCH] Check failed: {e}")

    def _log_active_workflow_diagnostics(self) -> None:
        """Log per-workflow task-status counts for every active workflow.
        Diagnostic only -- no decisions made here. Extracted from
        _monitoring_cycle (SOLID review 3.4), same rationale as
        _maybe_switch_tracked_workflow above.
        """
        session = self.db_manager.get_session()
        try:
            from src.core.database import Workflow

            active_workflows = session.query(Workflow).filter_by(status="active").all()
            logger.info(
                f"[DIAGNOSTIC] Active workflows in database: {len(active_workflows)}"
            )
            for wf in active_workflows:
                task_count = session.query(Task).filter_by(workflow_id=wf.id).count()
                done_count = (
                    session.query(Task)
                    .filter_by(workflow_id=wf.id, status="done")
                    .count()
                )
                failed_count = (
                    session.query(Task)
                    .filter_by(workflow_id=wf.id, status="failed")
                    .count()
                )
                active_count = (
                    session.query(Task)
                    .filter(
                        Task.workflow_id == wf.id,
                        Task.status.in_(["pending", "assigned", "in_progress"]),
                    )
                    .count()
                )
                logger.info(
                    f"[DIAGNOSTIC]   - {wf.name} (ID: {wf.id[:8]}..., {task_count} total: {done_count} done, {failed_count} failed, {active_count} active)"
                )
        finally:
            session.close()

    async def _guardian_analysis_for_agent(self, *args, **kwargs):
        """Delegator to _guardian_dispatch.guardian_analysis_for_agent()."""
        return await self._guardian_dispatch.guardian_analysis_for_agent(*args, **kwargs)


    async def _auto_restart_agent(self, agent: Agent) -> None:
        """Delegator to _auto_restart.requeue_and_terminate() (Phase 1b decomposition,
        phase_1b_decomposition.md section 4.3). The implementation moved to
        src/monitoring/auto_restart.py so MechanicalRecoveryDetector and
        GuardianDispatcher share one copy; this stub keeps the old name for
        tests that call it directly on the loop.
        """
        await self._auto_restart.requeue_and_terminate(agent)

    def _get_past_summaries_for_agent(self, *args, **kwargs):
        """Delegator to _guardian_dispatch.get_past_summaries_for_agent()."""
        return self._guardian_dispatch.get_past_summaries_for_agent(*args, **kwargs)


    async def _update_agent_health_from_trajectory(self, *args, **kwargs):
        """Delegator to _guardian_dispatch.update_agent_health_from_trajectory()."""
        return await self._guardian_dispatch.update_agent_health_from_trajectory(*args, **kwargs)


    async def _save_conductor_analysis(self, analysis: Dict[str, Any]):
        """Save Conductor analysis to dedicated table.

        Args:
            analysis: Conductor analysis result
        """
        # Offloaded -- session_scope() does blocking DB I/O directly on the
        # event loop otherwise, same class of issue fixed elsewhere in this
        # codebase.
        await asyncio.get_event_loop().run_in_executor(
            None, self._save_conductor_analysis_sync, analysis
        )

    def _save_conductor_analysis_sync(self, analysis: Dict[str, Any]):
        """Sync body of _save_conductor_analysis -- run via run_in_executor."""
        try:
            with self.db_manager.session_scope() as session:
                # Extract duplicate info
                duplicates = analysis.get("duplicates", [])
                coherence_info = analysis.get("coherence", {})
                decisions = analysis.get("decisions", [])

                # Count decision types
                termination_count = sum(
                    1 for d in decisions if d.get("type") == "terminate_duplicate"
                )
                coordination_count = sum(
                    1 for d in decisions if d.get("type") == "coordinate_resources"
                )

                # Save main Conductor analysis
                conductor_analysis = ConductorAnalysis(
                    coherence_score=coherence_info.get("score", 0.7),
                    num_agents=analysis.get("num_agents", 0),
                    system_status=analysis.get("system_status", "Unknown"),
                    duplicate_count=len(duplicates),
                    termination_count=termination_count,
                    coordination_count=coordination_count,
                    details=analysis,
                )
                session.add(conductor_analysis)
                session.flush()  # Get the ID

                # Save detected duplicates
                for dup in duplicates:
                    duplicate_entry = DetectedDuplicate(
                        conductor_analysis_id=conductor_analysis.id,
                        agent1_id=dup.get("agent1"),
                        agent2_id=dup.get("agent2"),
                        similarity_score=dup.get("similarity", 0.0),
                        work_description=dup.get("work", "Unknown duplicate work"),
                    )
                    session.add(duplicate_entry)

                # Also keep a log entry for backwards compatibility
                log_entry = AgentLog(
                    agent_id=None,  # System-level log
                    log_type="conductor_analysis",
                    message=f"Conductor: coherence={coherence_info.get('score', 0):.2f}, "
                    f"{len(duplicates)} duplicates, {analysis.get('system_status', 'Unknown')[:50]}",
                    details={"conductor_analysis_id": conductor_analysis.id},
                )
                session.add(log_entry)

                logger.debug(f"Saved Conductor analysis ID {conductor_analysis.id}")

        except Exception as e:
            logger.error(f"Failed to save Conductor analysis: {e}")
            session.rollback()
        finally:
            session.close()

    async def _handle_missing_tmux_session(self, *args, **kwargs):
        """Delegator to _guardian_dispatch.handle_missing_tmux_session()."""
        return await self._guardian_dispatch.handle_missing_tmux_session(*args, **kwargs)


    def _write_agent_tmux_log(self, *args, **kwargs):
        """Delegator to _guardian_dispatch.write_agent_tmux_log()."""
        return self._guardian_dispatch.write_agent_tmux_log(*args, **kwargs)


    async def _audit_system_health(self, *args, **kwargs):
        """Delegator to _health_auditor.audit_system_health()."""
        return await self._health_auditor.audit_system_health(*args, **kwargs)


    async def _cleanup_orphaned_tmux_sessions(self):
        """Clean up tmux sessions that don't have corresponding active agents.
        Also clean up orphaned agents (working but no active workflow).

        Delegates to OrphanSessionReaper (SOLID review 3.4) — kept as a
        public method here since tests call it directly on the
        MonitoringLoop instance.

        FIX #18: Removed fragile two-way state sync. The reaper owns
        last_check_time entirely; tests should access
        monitor._orphan_reaper.last_check_time directly.
        """
        await self._orphan_reaper.cleanup_orphaned_tmux_sessions()

    async def _check_workflow_stuck_state(self, *args, **kwargs):
        """Delegator to _diagnostics.check_workflow_stuck_state()."""
        return await self._diagnostics.check_workflow_stuck_state(*args, **kwargs)


    def _log_diagnostic_status_report(self, *args, **kwargs):
        """Delegator to _diagnostics.log_diagnostic_status_report()."""
        return self._diagnostics.log_diagnostic_status_report(*args, **kwargs)


    async def _create_diagnostic_agent(self, *args, **kwargs):
        """Delegator to _diagnostics.create_diagnostic_agent()."""
        return await self._diagnostics.create_diagnostic_agent(*args, **kwargs)


    async def _gather_diagnostic_context(self, *args, **kwargs):
        """Delegator to _diagnostics.gather_diagnostic_context()."""
        return await self._diagnostics.gather_diagnostic_context(*args, **kwargs)


    async def _generate_diagnostic_prompt(self, *args, **kwargs):
        """Delegator to _diagnostics.generate_diagnostic_prompt()."""
        return await self._diagnostics.generate_diagnostic_prompt(*args, **kwargs)

