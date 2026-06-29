"""Intelligent monitoring and self-healing system for Hephaestus."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from enum import Enum

from src.core.simple_config import get_config
from src.core.database import DatabaseManager, Agent, Task, AgentLog, GuardianAnalysis, ConductorAnalysis, DetectedDuplicate
from src.core.constants import WORKTREES_SUBDIR, CONTEXT_DIR_NAME, HEPHAESTUS_LOGS_DIR
from src.agents.manager import AgentManager
from src.interfaces import LLMProviderInterface, get_cli_agent
from src.memory.rag import RAGSystem
from src.phases import PhaseManager
from src.monitoring.guardian import Guardian
from src.monitoring.conductor import Conductor
from src.monitoring.trajectory_context import TrajectoryContext

logger = logging.getLogger(__name__)


class AgentState(Enum):
    """Agent state enumeration."""
    HEALTHY = "healthy"
    STUCK_WAITING = "stuck_waiting"
    STUCK_ERROR = "stuck_error"
    STUCK_CONFUSED = "stuck_confused"
    UNRECOVERABLE = "unrecoverable"


class MonitoringDecision(Enum):
    """Monitoring decision enumeration."""
    CONTINUE = "continue"
    NUDGE = "nudge"
    ANSWER = "answer"
    RESTART = "restart"
    RECREATE = "recreate"


class IntelligentMonitor:
    """LLM-powered monitoring system for agent health and intervention."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        agent_manager: AgentManager,
        llm_provider: LLMProviderInterface,
        rag_system: RAGSystem,
    ):
        """Initialize intelligent monitor.

        Args:
            db_manager: Database manager
            agent_manager: Agent manager
            llm_provider: LLM provider for analysis
            rag_system: RAG system for context
        """
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.llm_provider = llm_provider
        self.rag_system = rag_system
        self.config = get_config()

    async def analyze_agent_state(self, agent: Agent) -> Dict[str, Any]:
        """Analyze agent state and decide on intervention.

        Args:
            agent: Agent to analyze

        Returns:
            Analysis result with state and decision
        """
        logger.debug(f"Analyzing agent {agent.id} state")

        try:
            # Collect comprehensive context
            context = await self._collect_agent_context(agent)

            # Analyze with LLM
            analysis = await self.llm_provider.analyze_agent_state(
                agent_output=context["tmux_output"],
                task_info={
                    "description": context["task_description"],
                    "done_definition": context["done_definition"],
                    "time_elapsed": context["time_elapsed"],
                },
                project_context=context["project_context"],
            )

            logger.info(
                f"Agent {agent.id} analysis: state={analysis['state']}, "
                f"decision={analysis['decision']}, confidence={analysis.get('confidence', 0)}"
            )

            return analysis

        except Exception as e:
            logger.error(f"Failed to analyze agent {agent.id}: {e}")
            return {
                "state": AgentState.HEALTHY.value,
                "decision": MonitoringDecision.CONTINUE.value,
                "message": "",
                "reasoning": "Analysis failed, assuming healthy",
                "confidence": 0.1,
            }

    async def _collect_agent_context(self, agent: Agent) -> Dict[str, Any]:
        """Collect comprehensive context for agent analysis.

        Args:
            agent: Agent to collect context for

        Returns:
            Context dictionary
        """
        # Get tmux output
        tmux_output = self.agent_manager.get_agent_output(
            agent.id,
            lines=self.config.tmux_output_lines,
        )

        # Get task details
        session = self.db_manager.get_session()
        task = session.query(Task).filter_by(id=agent.current_task_id).first()
        session.close()

        if not task:
            logger.error(f"Task {agent.current_task_id} not found for agent {agent.id}")
            task_description = "Unknown task"
            done_definition = "Unknown"
            time_elapsed = 0
        else:
            task_description = task.enriched_description or task.raw_description
            done_definition = task.done_definition
            time_elapsed = int((datetime.utcnow() - task.started_at).total_seconds() / 60) if task.started_at else 0

        # Get project context
        project_context = await self.agent_manager.get_project_context()

        # Search for similar past issues if agent appears stuck
        similar_issues = []
        if self._appears_stuck(tmux_output):
            similar_issues = await self.rag_system.search_error_solutions(
                tmux_output[-500:],  # Last 500 chars
                limit=3,
            )

        return {
            "tmux_output": tmux_output,
            "task_description": task_description,
            "done_definition": done_definition,
            "time_elapsed": time_elapsed,
            "project_context": project_context,
            "similar_issues": similar_issues,
        }

    def _appears_stuck(self, output: str) -> bool:
        """Quick check if agent appears stuck.

        Args:
            output: Agent output

        Returns:
            True if appears stuck
        """
        stuck_indicators = [
            "error",
            "failed",
            "stuck",
            "waiting",
            "timeout",
            "rate limit",
        ]

        output_lower = output.lower()
        return any(indicator in output_lower for indicator in stuck_indicators)

    async def execute_intervention(
        self,
        agent: Agent,
        decision: Dict[str, Any],
    ):
        """Execute the monitoring decision.

        Args:
            agent: Agent to intervene on
            decision: Decision from analysis
        """
        action = decision.get("decision", MonitoringDecision.CONTINUE.value)
        message = decision.get("message", "")
        reasoning = decision.get("reasoning", "")

        logger.info(f"Executing intervention for agent {agent.id}: {action}")

        if action == MonitoringDecision.CONTINUE.value:
            # No action needed
            return

        elif action == MonitoringDecision.NUDGE.value:
            # Send helpful nudge message
            await self._nudge_agent(agent, message)
            await self._log_intervention(agent, "nudged", message)

        elif action == MonitoringDecision.ANSWER.value:
            # Answer agent's question with context
            enriched_answer = await self._enrich_answer(message, agent.current_task_id)
            await self._send_agent_message(agent, enriched_answer)
            await self._log_intervention(agent, "answered", enriched_answer)

        elif action == MonitoringDecision.RESTART.value:
            # Restart the agent
            await self.agent_manager.restart_agent(agent.id, reasoning)
            await self._log_intervention(agent, "restarted", reasoning)

        elif action == MonitoringDecision.RECREATE.value:
            # Create new agent with enhanced approach
            await self._recreate_agent_with_new_approach(agent, reasoning)
            await self._log_intervention(agent, "recreated", reasoning)

    async def _nudge_agent(self, agent: Agent, message: str):
        """Send a nudge message to the agent.

        Args:
            agent: Agent to nudge
            message: Nudge message
        """
        if not message:
            message = f"""
[HEPHAESTUS ASSISTANT]: Just checking in! You're working on task {agent.current_task_id}.
If you're stuck or need help, remember you can:
- Create sub-tasks using create_task
- Save discoveries using save_memory
- Update task status when done using update_task_status

Current time: {datetime.utcnow().isoformat()}
"""

        await self._send_agent_message(agent, message)

    async def _send_agent_message(self, agent: Agent, message: str):
        """Send a message to the agent.

        Args:
            agent: Agent to message
            message: Message to send
        """
        formatted_message = f"\n[HEPHAESTUS]: {message}\n"
        await self.agent_manager.send_message_to_agent(agent.id, formatted_message)

    async def _enrich_answer(self, answer: str, task_id: str) -> str:
        """Enrich an answer with additional context.

        Args:
            answer: Base answer
            task_id: Related task ID

        Returns:
            Enriched answer
        """
        # Search for relevant knowledge
        relevant_knowledge = await self.rag_system.retrieve_for_task(
            task_description=answer,
            requesting_agent_id="monitor",
            limit=5,
        )

        if relevant_knowledge:
            enriched = f"{answer}\n\nAdditional context from knowledge base:\n"
            for memory in relevant_knowledge[:3]:
                enriched += f"- {memory['content'][:200]}...\n"
            return enriched

        return answer

    async def _recreate_agent_with_new_approach(self, agent: Agent, reason: str):
        """Recreate agent with a new approach.

        Args:
            agent: Agent to recreate
            reason: Reason for recreation
        """
        logger.info(f"Recreating agent {agent.id} with new approach: {reason}")

        session = self.db_manager.get_session()
        try:
            # Get task
            task = session.query(Task).filter_by(id=agent.current_task_id).first()
            if not task:
                logger.error(f"Task {agent.current_task_id} not found")
                return

            # Terminate old agent
            await self.agent_manager.terminate_agent(agent.id)

            # Get failure context
            failure_context = f"""
Previous agent failed with: {reason}
Previous approach issues:
- {reason}

Please try a different approach, considering:
- Break down the task into smaller steps
- Use create_task for complex sub-tasks
- Save any discoveries or errors encountered
"""

            # Get enhanced memories including failure patterns
            memories = await self.rag_system.retrieve_for_task(
                task_description=f"{task.enriched_description} {failure_context}",
                requesting_agent_id="monitor",
                limit=15,
            )

            # Create new agent with enhanced context
            enriched_data = {
                "enriched_description": task.enriched_description,
                "completion_criteria": [task.done_definition],
                "agent_prompt": failure_context,
                "required_capabilities": ["recovery", "problem_solving"],
                "estimated_complexity": 8,  # Increase complexity
            }

            project_context = await self.agent_manager.get_project_context()

            # Preserve the task's phase CLI/thinking config on recovery — otherwise a
            # restarted phase agent silently reverts to the default tool/model/budget.
            phase_cli_tool = phase_cli_model = phase_glm_token_env = phase_thinking_level = None
            if task.phase_id:
                from src.core.database import Phase
                ps = self.db_manager.get_session()
                try:
                    ph = ps.query(Phase).filter_by(id=task.phase_id).first()
                    if ph:
                        phase_cli_tool = ph.cli_tool
                        phase_cli_model = ph.cli_model
                        phase_glm_token_env = ph.glm_api_token_env
                        phase_thinking_level = ph.thinking_level
                finally:
                    ps.close()

            new_agent = await self.agent_manager.create_agent_for_task(
                task=task,
                enriched_data=enriched_data,
                memories=memories,
                project_context=f"{project_context}\n\n{failure_context}",
                phase_cli_tool=phase_cli_tool,
                phase_cli_model=phase_cli_model,
                phase_glm_token_env=phase_glm_token_env,
                phase_thinking_level=phase_thinking_level,
            )

            logger.info(f"Created new agent {new_agent.id} to replace {agent.id}")

        except Exception as e:
            logger.error(f"Failed to recreate agent: {e}")
            session.rollback()
        finally:
            session.close()

    async def _log_intervention(self, agent: Agent, intervention_type: str, details: str):
        """Log an intervention.

        Args:
            agent: Agent involved
            intervention_type: Type of intervention
            details: Intervention details
        """
        session = self.db_manager.get_session()
        try:
            log_entry = AgentLog(
                agent_id=agent.id,
                log_type="intervention",
                message=f"Intervention: {intervention_type}",
                details={"type": intervention_type, "details": details[:500]},
            )
            session.add(log_entry)
            session.commit()
        except Exception as e:
            logger.error(f"Failed to log intervention: {e}")
            session.rollback()
        finally:
            session.close()


class MonitoringLoop:
    """Main monitoring loop for the system with trajectory monitoring."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        agent_manager: AgentManager,
        llm_provider: LLMProviderInterface,
        rag_system: RAGSystem,
        phase_manager: Optional[PhaseManager] = None,
    ):
        """Initialize monitoring loop with trajectory monitoring.

        Args:
            db_manager: Database manager
            agent_manager: Agent manager
            llm_provider: LLM provider
            rag_system: RAG system
            phase_manager: Optional phase manager for workflow monitoring
        """
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.phase_manager = phase_manager
        self.llm_provider = llm_provider
        self.rag_system = rag_system

        # Initialize trajectory monitoring components
        self.guardian = Guardian(
            db_manager=db_manager,
            agent_manager=agent_manager,
            llm_provider=llm_provider,
        )
        self.conductor = Conductor(
            db_manager=db_manager,
            agent_manager=agent_manager,
        )
        self.trajectory_context = TrajectoryContext(db_manager=db_manager)

        # Keep old monitor for fallback
        self.intelligent_monitor = IntelligentMonitor(
            db_manager=db_manager,
            agent_manager=agent_manager,
            llm_provider=llm_provider,
            rag_system=rag_system,
        )

        self.config = get_config()
        self.running = False

        # Cache for Guardian summaries
        self.guardian_summaries_cache: Dict[str, Dict[str, Any]] = {}

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

    async def _mechanical_recovery_for_agent(self, agent):
        """Cheap, no-LLM stuck detection + keystroke recovery (the CLI/keystroke-level
        monitor). If an agent's substantive TUI output is frozen for FROZEN_SECONDS
        (a pi/mimo thought-loop that never exits), send the CLI's recovery keystrokes
        (Esc, polymorphic via CLIAgentInterface) + a short nudge. Bounded by MAX_RECOV;
        beyond that the Guardian / restart path takes over.
        """
        FROZEN_SECONDS = 300   # >a normal turn; a real loop stays frozen indefinitely
        MAX_RECOV = 2
        try:
            import re
            if not hasattr(self, "_stuck_state"):
                self._stuck_state = {}
            out = self.agent_manager.get_agent_output(agent.id, lines=40)
            if not out:
                return
            # Drop volatile lines (status bar %/tokens/$/MCP/time, spinner glyphs) so a
            # live spinner or ticking cost doesn't masquerade as real progress.
            sig = "\n".join(
                ln for ln in out.splitlines()
                if not re.search(r"%/[\d.]+M|\$[\d.]+|MCP:|Took |[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣿]", ln)
            ).strip()
            now = time.time()
            st = self._stuck_state.setdefault(agent.id, {"sig": None, "since": None, "recov": 0})
            if sig and sig == st["sig"]:
                if st["since"] is None:
                    st["since"] = now
            else:
                # Output changed → real progress; reset everything.
                st["sig"] = sig
                st["since"] = None
                st["recov"] = 0
                return
            frozen_for = now - st["since"] if st["since"] else 0
            # Fast-path: "Operation aborted" leaves the agent idle at the shell
            # prompt.  The output signature changed (so the 5-min clock reset),
            # but the agent won't self-rescue — 30 s is enough to be sure.
            abort_frozen = "Operation aborted" in sig and frozen_for >= 30
            if (abort_frozen or frozen_for >= FROZEN_SECONDS) and st["recov"] < MAX_RECOV:
                st["recov"] += 1
                st["since"] = now  # restart the window after an attempt
                logger.warning(
                    f"[MECH-RECOVERY] Agent {agent.id[:8]} ({agent.cli_type}) output frozen "
                    f"{int(frozen_for)}s — recovery attempt {st['recov']}/{MAX_RECOV} (keys + nudge)"
                )
                # Reconnect MCP if disconnected before sending the nudge.
                mcp_disconnected = bool(re.search(r"MCP:\s*0/", out))
                if mcp_disconnected:
                    logger.warning(
                        f"[MECH-RECOVERY] Agent {agent.id[:8]}: MCP disconnected — reconnecting"
                    )
                    session_name = getattr(agent, "tmux_session_name", None)
                    if session_name:
                        import subprocess as _sp
                        _sp.run(["tmux", "send-keys", "-t", session_name, "Escape", ""], check=False)
                        await asyncio.sleep(0.5)
                        _sp.run(["tmux", "send-keys", "-t", session_name, "/mcp", "Enter"], check=False)
                        await asyncio.sleep(2.0)
                        _sp.run(["tmux", "send-keys", "-t", session_name, "C-r", ""], check=False)
                        await asyncio.sleep(3.0)
                        _sp.run(["tmux", "send-keys", "-t", session_name, "Escape", ""], check=False)
                        await asyncio.sleep(0.5)
                if await self.agent_manager.send_recovery_keystrokes(agent.id):
                    mcp_note = " MCP was disconnected and has been reconnected." if mcp_disconnected else ""
                    if "Operation aborted" in sig:
                        msg = (
                            "Your last tool call was aborted. Review what you have already "
                            "completed in this session. If the work is done, call "
                            "update_task_status with status='done'. If you are genuinely "
                            f"blocked, call it with status='failed' and explain why.{mcp_note}"
                        )
                    else:
                        msg = (
                            "You appear stuck or looping. Stop, state your single next concrete "
                            f"action in one line, then do it. If blocked, save a memory and call "
                            f"update_task_status.{mcp_note}"
                        )
                    await self.agent_manager.send_message_to_agent(agent.id, msg)
            elif frozen_for >= FROZEN_SECONDS and st["recov"] >= MAX_RECOV:
                # All recovery attempts exhausted and agent is still frozen.
                # Fail the task so the monitor's retry-bound path handles it
                # (MAX_PHASE_ATTEMPTS → impasse if exceeded). §9.4 / §11.2 fix #2.
                logger.warning(
                    f"[MECH-RECOVERY] Agent {agent.id[:8]} frozen {int(frozen_for)}s after "
                    f"{MAX_RECOV} recovery attempts — abandoning: fail task, terminate agent"
                )
                session = self.db_manager.get_session()
                try:
                    from src.core.database import Task as _Task
                    stuck_task = session.query(_Task).filter_by(
                        assigned_agent_id=agent.id, status="in_progress"
                    ).first()
                    if stuck_task:
                        stuck_task.status = "failed"
                        stuck_task.failure_reason = (
                            f"Agent output frozen {int(frozen_for)}s; "
                            f"{MAX_RECOV} recovery attempts exhausted"
                        )
                        session.commit()
                        logger.info(
                            f"[MECH-RECOVERY] Task {stuck_task.id[:8]} marked failed; "
                            f"phase will be retried (MAX_PHASE_ATTEMPTS bound)"
                        )
                finally:
                    session.close()
                await self.agent_manager.terminate_agent(agent.id)
                self._stuck_state.pop(agent.id, None)
        except Exception as e:
            logger.debug(f"[MECH-RECOVERY] check failed for {agent.id[:8]}: {e}")

    async def _detect_repetition_loop(self, agent):
        """Detect and interrupt an LLM thought-loop where the same sentence repeats
        many times in recent output (output IS growing, just cycling the same text).

        Unlike the frozen-output check in _mechanical_recovery_for_agent, this fires
        when the model keeps adding the same paragraph over and over — a semantic loop
        that the frozen-sig check misses because the content hash changes each tick.

        Trigger: any normalised line of ≥ 30 chars appears ≥ 5 times in the last 80
        lines of output. One recovery attempt is made (keys + targeted nudge); if the
        loop resumes it will be caught again on the next cycle.
        """
        MIN_LINE_LEN = 30
        WINDOW_LINES = 120
        REPEAT_THRESHOLD = 12
        try:
            if not hasattr(self, "_rep_loop_state"):
                self._rep_loop_state = {}
            out = self.agent_manager.get_agent_output(agent.id, lines=WINDOW_LINES)
            if not out:
                return
            # Normalise: strip leading whitespace, drop blank/trivial lines.
            # Also exclude bare filesystem paths and shell prompts — these repeat
            # legitimately in ls output, shell prompts, and long file writes.
            import re as _re
            _fs_path = _re.compile(r"^[/~][\w./\-]+$")
            lines = [
                ln.strip() for ln in out.splitlines()
                if len(ln.strip()) >= MIN_LINE_LEN
                and not _fs_path.match(ln.strip())
            ]
            if not lines:
                return
            # Count occurrences of each normalised line.
            from collections import Counter
            counts = Counter(lines)
            top_line, top_count = counts.most_common(1)[0]
            if top_count < REPEAT_THRESHOLD:
                self._rep_loop_state.pop(agent.id, None)
                return
            # Guard: only fire once per unique repeated phrase to avoid spam.
            last_phrase = self._rep_loop_state.get(agent.id)
            if last_phrase == top_line:
                return
            self._rep_loop_state[agent.id] = top_line
            logger.warning(
                f"[REP-LOOP] Agent {agent.id[:8]} ({agent.cli_type}): "
                f"line repeated {top_count}× in last {WINDOW_LINES} lines — "
                f"interrupting. Phrase: {top_line[:60]!r}"
            )
            from src.interfaces.cli_interface import get_cli_agent
            try:
                cli = get_cli_agent(agent.cli_type)
                keys = cli.recovery_keystrokes()
            except Exception:
                keys = []
            if keys:
                await self.agent_manager.send_recovery_keystrokes(agent.id)
            await self.agent_manager.send_message_to_agent(
                agent.id,
                f"You are in a thought loop — the phrase {top_line[:60]!r} "
                f"has appeared {top_count} times. STOP. Do not repeat that "
                "reasoning again. Pick ONE concrete next step, execute it, "
                "and if you are still blocked call update_task_status with "
                "status='failed' and explain why.",
            )
        except Exception as e:
            logger.debug(f"[REP-LOOP] check failed for {agent.id[:8]}: {e}")

    async def _monitoring_cycle(self):
        """Execute one monitoring cycle with trajectory monitoring."""
        logger.debug("Starting trajectory monitoring cycle")

        # DEBUG: Log phase_manager status
        logger.info(f"[DIAGNOSTIC CYCLE] phase_manager exists: {self.phase_manager is not None}")
        if self.phase_manager:
            logger.info(f"[DIAGNOSTIC CYCLE] phase_manager.workflow_id: {self.phase_manager.workflow_id[:8] if self.phase_manager.workflow_id else 'None'}")
        else:
            logger.info("[DIAGNOSTIC CYCLE] phase_manager is None")

        # Get all active agents
        agents = self.agent_manager.get_active_agents()
        logger.info(f"Trajectory monitoring {len(agents)} active agents")

        # Phase 0: cheap mechanical recovery (no LLM). Two complementary checks:
        #   a) frozen output — same substantive 40-line sig for ≥5 min
        #   b) repetition loop — output growing but same sentence repeats 5+ times
        #      in the last 80 lines (LLM cycling "Actually, let me try…")
        for agent in agents:
            await self._mechanical_recovery_for_agent(agent)
            await self._detect_repetition_loop(agent)

        # Phase 1: Guardian Analysis (Parallel)
        guardian_summaries = []
        guardian_tasks = []

        for agent in agents:
            # Create async task for each Guardian analysis
            task = asyncio.create_task(
                self._guardian_analysis_for_agent(agent)
            )
            guardian_tasks.append(task)

        # Wait for all Guardian analyses to complete
        if guardian_tasks:
            guardian_results = await asyncio.gather(*guardian_tasks, return_exceptions=True)

            # Filter out exceptions and None results
            guardian_summaries = [
                result for result in guardian_results
                if result and not isinstance(result, Exception)
            ]

            # Log any exceptions
            for i, result in enumerate(guardian_results):
                if isinstance(result, Exception):
                    logger.error(f"Guardian analysis failed for agent {agents[i].id}: {result}")

        # Debug: Log what we collected
        logger.info(f"DEBUG - Collected {len(guardian_summaries)} Guardian summaries")
        for i, summary in enumerate(guardian_summaries):
            if summary:
                logger.info(f"DEBUG - Summary {i}: agent_id={summary.get('agent_id')}, "
                           f"has_trajectory_summary={bool(summary.get('trajectory_summary'))}")

        # Phase 2: Conductor Analysis (if we have summaries)
        if guardian_summaries:
            try:
                logger.info(f"DEBUG - Passing {len(guardian_summaries)} summaries to Conductor")
                conductor_analysis = await self.conductor.analyze_system_state(
                    guardian_summaries
                )

                # Log system status
                logger.info(f"System Status: {conductor_analysis['system_status']}")

                # Save Conductor analysis to dedicated table
                await self._save_conductor_analysis(conductor_analysis)

                # Execute conductor decisions
                if conductor_analysis.get('decisions'):
                    await self.conductor.execute_decisions(
                        conductor_analysis['decisions']
                    )

                # Generate and log detailed report if needed
                if conductor_analysis.get('coherence', {}).get('score', 1.0) < 0.5:
                    report = await self.conductor.generate_detailed_report(conductor_analysis)
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
            logger.info("[AUTO-DISCOVER] phase_manager.workflow_id is None, checking for active workflows...")
            try:
                wf_id = self.phase_manager.load_active_workflow()
                if wf_id:
                    logger.info(f"[AUTO-DISCOVER] ✅ Loaded active workflow: {wf_id[:8]}...")
            except Exception as e:
                logger.warning(f"[AUTO-DISCOVER] Failed to load active workflow: {e}")

        # Propagate phase_manager to agent_manager so spawned agents get phase context
        if self.phase_manager and self.agent_manager and not self.agent_manager.phase_manager:
            self.agent_manager.phase_manager = self.phase_manager

        # Check phase progression if workflow is active
        if self.phase_manager and self.phase_manager.workflow_id:
            try:
                await self._check_phase_progression()
            except Exception as e:
                logger.error(f"Error checking phase progression: {e}")

        # Check if workflow is stuck and needs diagnostic agent
        logger.info("[DIAGNOSTIC] Checking if diagnostic agent needed...")
        logger.info(f"[DIAGNOSTIC] phase_manager exists: {self.phase_manager is not None}")
        logger.info(f"[DIAGNOSTIC] workflow_id: {self.phase_manager.workflow_id[:8] if (self.phase_manager and self.phase_manager.workflow_id) else 'N/A'}")

        # Phase 3: System Health Audit
        try:
            await self._audit_system_health()
        except Exception as e:
            logger.error(f"Error in system health audit: {e}")

        # DEBUG: Check database for active workflows
        session = self.db_manager.get_session()
        try:
            from src.core.database import Workflow
            active_workflows = session.query(Workflow).filter_by(status='active').all()
            logger.info(f"[DIAGNOSTIC] Active workflows in database: {len(active_workflows)}")
            for wf in active_workflows:
                task_count = session.query(Task).filter_by(workflow_id=wf.id).count()
                done_count = session.query(Task).filter_by(workflow_id=wf.id, status='done').count()
                failed_count = session.query(Task).filter_by(workflow_id=wf.id, status='failed').count()
                active_count = session.query(Task).filter(
                    Task.workflow_id == wf.id,
                    Task.status.in_(['pending', 'assigned', 'in_progress'])
                ).count()
                logger.info(f"[DIAGNOSTIC]   - {wf.name} (ID: {wf.id[:8]}..., {task_count} total: {done_count} done, {failed_count} failed, {active_count} active)")
        finally:
            session.close()

        if self.phase_manager and self.phase_manager.workflow_id:
            logger.info(f"[DIAGNOSTIC] ✅ Conditions met - running diagnostic check for workflow {self.phase_manager.workflow_id[:8]}")
            try:
                await self._check_workflow_stuck_state()
            except Exception as e:
                logger.error(f"[DIAGNOSTIC] Error checking workflow stuck state: {e}")
        else:
            if not self.phase_manager:
                logger.warning("[DIAGNOSTIC] ❌ SKIPPED - No phase_manager")
            elif not self.phase_manager.workflow_id:
                logger.warning("[DIAGNOSTIC] ❌ SKIPPED - phase_manager.workflow_id is None")
                logger.warning("[DIAGNOSTIC] 💡 This likely means there's an active workflow in the DB that wasn't loaded on startup")

    async def _guardian_analysis_for_agent(self, agent: Agent) -> Optional[Dict[str, Any]]:
        """Perform Guardian analysis for a single agent.

        Args:
            agent: Agent to analyze

        Returns:
            Guardian analysis result or None if failed
        """
        session = self.db_manager.get_session()
        try:
            # Skip agents that are too young (grace period for spin-up)
            agent_age_seconds = (datetime.utcnow() - agent.created_at).total_seconds()
            if agent_age_seconds < self.config.guardian_min_agent_age_seconds:
                logger.debug(
                    f"Skipping Guardian analysis for agent {agent.id} "
                    f"(age: {agent_age_seconds:.0f}s, min: {self.config.guardian_min_agent_age_seconds}s)"
                )
                return None

            # The orchestrator runs in-process (AutopilotService), not as a tmux
            # agent — never health-check or "recreate" it for a missing tmux session
            # (that was a 60s phantom-restart loop after the Tier 2 in-process move).
            if agent.agent_type == 'orchestrator':
                logger.debug(f"Skipping orchestrator agent {agent.id[:8]} (runs in-process)")
                return None

            # Special handling for agents with missing tmux sessions
            if agent.tmux_session_name and not self.agent_manager.tmux_server.has_session(agent.tmux_session_name):
                # Check if task is already done before restarting
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == 'done':
                    logger.info(f"Agent {agent.id} has missing tmux session but task {task.id[:8]} is done — not restarting")
                    return None
                logger.warning(f"Agent {agent.id} has missing tmux session {agent.tmux_session_name}, recreating")
                await self._handle_missing_tmux_session(agent)
                return None

            # Get agent output
            tmux_output = self.agent_manager.get_agent_output(
                agent.id,
                lines=self.config.tmux_output_lines,
            )

            if not tmux_output:
                logger.warning(f"No output from agent {agent.id}")
                return None

            # Persist scrollback to docs/tmux/ so the forensics agent can read it.
            # Use the session already open in this try block to avoid a second round-trip.
            if agent.current_task_id:
                try:
                    from src.core.database import Phase as _Phase
                    _task = session.query(Task).filter_by(id=agent.current_task_id).first()
                    if _task and _task.phase_id:
                        _phase = session.query(_Phase).filter_by(id=_task.phase_id).first()
                        if _phase:
                            self._write_agent_tmux_log(agent.id, _phase.name, tmux_output)
                except Exception:
                    pass  # non-fatal; don't interrupt the monitoring cycle

            # DETECT: Agent exited to command line (shows $, %, >>>, bquote>)
            if self.guardian.detect_agent_exited(tmux_output):
                # Check if task is already done before restarting
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == 'done':
                    logger.info(f"Agent {agent.id[:8]} exited but task {task.id[:8]} is done — not restarting")
                    return None
                logger.warning(
                    f"Agent {agent.id[:8]} exited to command line — restarting"
                )
                await self._handle_missing_tmux_session(agent)
                return None

            # Detect garbled TUI output (CLI rendering corruption)
            # Get TUI status patterns from the active CLI interface
            tui_patterns = None
            try:
                from src.core.simple_config import get_config
                from src.interfaces.cli_interface import get_cli_agent
                config = get_config()
                cli_agent = get_cli_agent(getattr(config, 'cli_agent_type', 'pi'))
                tui_patterns = cli_agent.get_tui_status_patterns()
            except Exception:
                pass  # No CLI agent configured — use no patterns (strictest check)
            if self.guardian.detect_garbled_output(tmux_output, tui_patterns=tui_patterns):
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == 'done':
                    logger.info(f"Agent {agent.id[:8]} garbled but task done — not restarting")
                    return None
                logger.warning(
                    f"Agent {agent.id[:8]} has garbled TUI output — restarting"
                )
                await self._handle_missing_tmux_session(agent)
                return None

            # Get past summaries for this agent
            past_summaries = self._get_past_summaries_for_agent(agent.id)

            # Perform Guardian analysis with trajectory thinking
            analysis = await self.guardian.analyze_agent_with_trajectory(
                agent=agent,
                tmux_output=tmux_output,
                past_summaries=past_summaries,
            )

            # Cache the summary
            self.guardian_summaries_cache[agent.id] = {
                "summary": analysis,
                "timestamp": datetime.utcnow(),
            }

            # Execute steering if needed
            if analysis.get('needs_steering', False):
                await self.guardian.steer_agent(
                    agent=agent,
                    steering_type=analysis.get('steering_type', 'general'),
                    message=analysis.get('steering_message'),  # Guardian should map from steering_recommendation
                )

                # Auto-restart if agent keeps ignoring steering
                past = self._get_past_summaries_for_agent(agent.id, limit=5)
                consecutive_stuck = sum(
                    1 for s in past
                    if s.get('needs_steering') and s.get('steering_type') in ('stuck', 'idle')
                )
                if consecutive_stuck >= self.config.max_ignored_steering:
                    # Check if agent has recent activity before restarting
                    if agent.last_activity:
                        idle_seconds = (datetime.utcnow() - agent.last_activity).total_seconds()
                        if idle_seconds < 300:
                            logger.info(
                                f"Agent {agent.id[:8]} marked stuck but was active {idle_seconds:.0f}s ago — not restarting"
                            )
                        else:
                            logger.warning(
                                f"Agent {agent.id[:8]} ignored steering {consecutive_stuck} times. "
                                f"Auto-restarting..."
                            )
                            await self._auto_restart_agent(agent)
                    else:
                        logger.warning(
                            f"Agent {agent.id[:8]} ignored steering {consecutive_stuck} times. "
                            f"Auto-restarting..."
                        )
                        await self._auto_restart_agent(agent)

            # Update agent health based on trajectory alignment
            await self._update_agent_health_from_trajectory(agent, analysis)

            return analysis

        except Exception as e:
            logger.error(f"Guardian analysis failed for agent {agent.id}: {e}")
            return None
        finally:
            session.close()

    async def _auto_restart_agent(self, agent: Agent) -> None:
        """Kill a stuck agent's tmux session and mark it for restart."""
        try:
            if agent.tmux_session_name:
                self.agent_manager.tmux_server.kill_session(agent.tmux_session_name)
                logger.info(f"Killed tmux session {agent.tmux_session_name}")

            session = self.db_manager.get_session()
            try:
                agent.status = "terminated"
                agent.health_check_failures = 0
                session.commit()
            finally:
                session.close()

            # Record the restart
            self.guardian._record_steering(agent.id, "AUTO_RESTART", "Agent ignored steering too many times, auto-restarted")

        except Exception as e:
            logger.error(f"Failed to auto-restart agent {agent.id}: {e}")

    def _get_past_summaries_for_agent(self, agent_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get past Guardian summaries for an agent.

        Args:
            agent_id: Agent ID
            limit: Maximum number of summaries to return

        Returns:
            List of past summaries
        """
        session = self.db_manager.get_session()
        try:
            # Get past Guardian summaries from dedicated table
            analyses = session.query(GuardianAnalysis).filter(
                GuardianAnalysis.agent_id == agent_id
            ).order_by(GuardianAnalysis.timestamp.desc()).limit(limit).all()

            summaries = []
            for analysis in reversed(analyses):  # Reverse to get chronological order
                # Convert to dict format expected by Guardian
                summary = {
                    'current_phase': analysis.current_phase,
                    'trajectory_aligned': analysis.trajectory_aligned,
                    'alignment_score': analysis.alignment_score,
                    'needs_steering': analysis.needs_steering,
                    'steering_type': analysis.steering_type,
                    'trajectory_summary': analysis.trajectory_summary,
                    'accumulated_goal': analysis.accumulated_goal,
                    'timestamp': analysis.timestamp.isoformat() if analysis.timestamp else None
                }
                summaries.append(summary)

            # If new tables don't have data yet, fallback to old AgentLog method
            if not summaries:
                logs = session.query(AgentLog).filter(
                    AgentLog.agent_id == agent_id,
                    AgentLog.log_type.in_(['guardian_analysis', 'guardian_summary'])
                ).order_by(AgentLog.created_at.desc()).limit(limit).all()

                for log in reversed(logs):
                    if log.details:
                        summaries.append(log.details)

            return summaries

        finally:
            session.close()

    async def _update_agent_health_from_trajectory(self, agent: Agent, analysis: Dict[str, Any]):
        """Update agent health based on trajectory analysis.

        PARENT-CHILD MODEL: Parent monitors via tmux peek and task progress.
        Guardian trajectory analysis is a signal for last-resort steering.
        health_check_failures is incremented when trajectory is off-track,
        so the Guardian can decide whether to intervene.
        """
        session = self.db_manager.get_session()
        try:
            db_agent = session.query(Agent).filter_by(id=agent.id).first()
            if not db_agent:
                return

            db_agent.last_activity = datetime.utcnow()

            # Track health_check_failures for Guardian last-resort steering
            if analysis.get('trajectory_aligned', True):
                # Agent is on track — reset failures so it recovers
                db_agent.health_check_failures = 0
            else:
                alignment_score = analysis.get('alignment_score', 0.5)
                if alignment_score < 0.3:
                    db_agent.health_check_failures += 2
                elif alignment_score < 0.5:
                    db_agent.health_check_failures += 1

            # Save to dedicated Guardian analysis table
            guardian_analysis = GuardianAnalysis(
                agent_id=agent.id,
                current_phase=analysis.get('current_phase'),
                trajectory_aligned=analysis.get('trajectory_aligned', True),
                alignment_score=analysis.get('alignment_score', 1.0),
                needs_steering=analysis.get('needs_steering', False),
                steering_type=analysis.get('steering_type'),
                steering_recommendation=analysis.get('steering_recommendation'),
                trajectory_summary=analysis.get('trajectory_summary', 'No summary'),
                last_claude_message_marker=analysis.get('last_claude_message_marker'),  # NEW
                accumulated_goal=analysis.get('accumulated_goal'),
                current_focus=analysis.get('current_focus'),
                session_duration=analysis.get('session_duration'),
                conversation_length=analysis.get('conversation_length'),
                details=analysis
            )
            session.add(guardian_analysis)

            # Also keep a simplified log entry for backwards compatibility
            summary_log = AgentLog(
                agent_id=agent.id,
                log_type='guardian_analysis',
                message=f"Guardian: {analysis.get('current_phase', 'unknown')} phase, "
                       f"score={analysis.get('alignment_score', 0):.2f}, "
                       f"aligned={analysis.get('trajectory_aligned', False)}",
                details={'guardian_analysis_id': guardian_analysis.id}  # Reference to the full analysis
            )
            session.add(summary_log)
            session.commit()

        finally:
            session.close()

    async def _save_conductor_analysis(self, analysis: Dict[str, Any]):
        """Save Conductor analysis to dedicated table.

        Args:
            analysis: Conductor analysis result
        """
        session = self.db_manager.get_session()
        try:
            # Extract duplicate info
            duplicates = analysis.get('duplicates', [])
            coherence_info = analysis.get('coherence', {})
            decisions = analysis.get('decisions', [])

            # Count decision types
            termination_count = sum(1 for d in decisions if d.get('type') == 'terminate_duplicate')
            coordination_count = sum(1 for d in decisions if d.get('type') == 'coordinate_resources')

            # Save main Conductor analysis
            conductor_analysis = ConductorAnalysis(
                coherence_score=coherence_info.get('score', 0.7),
                num_agents=analysis.get('num_agents', 0),
                system_status=analysis.get('system_status', 'Unknown'),
                duplicate_count=len(duplicates),
                termination_count=termination_count,
                coordination_count=coordination_count,
                details=analysis
            )
            session.add(conductor_analysis)
            session.flush()  # Get the ID

            # Save detected duplicates
            for dup in duplicates:
                duplicate_entry = DetectedDuplicate(
                    conductor_analysis_id=conductor_analysis.id,
                    agent1_id=dup.get('agent1'),
                    agent2_id=dup.get('agent2'),
                    similarity_score=dup.get('similarity', 0.0),
                    work_description=dup.get('work', 'Unknown duplicate work')
                )
                session.add(duplicate_entry)

            # Also keep a log entry for backwards compatibility
            log_entry = AgentLog(
                agent_id=None,  # System-level log
                log_type='conductor_analysis',
                message=f"Conductor: coherence={coherence_info.get('score', 0):.2f}, "
                       f"{len(duplicates)} duplicates, {analysis.get('system_status', 'Unknown')[:50]}",
                details={'conductor_analysis_id': conductor_analysis.id}
            )
            session.add(log_entry)

            session.commit()
            logger.debug(f"Saved Conductor analysis ID {conductor_analysis.id}")

        except Exception as e:
            logger.error(f"Failed to save Conductor analysis: {e}")
            session.rollback()
        finally:
            session.close()

    async def _check_agent(self, agent: Agent):
        """Check a single agent's health (fallback method).

        Args:
            agent: Agent to check
        """
        # This is now a fallback method - Guardian analysis handles most of this
        # Only used if Guardian analysis is disabled or fails

        # Check task timeout
        if self._is_task_timed_out(agent):
            logger.warning(f"Agent {agent.id} task timed out")
            await self._handle_timeout(agent)

    def _is_agent_responsive(self, agent: Agent) -> bool:
        """Check if agent is responsive.

        Args:
            agent: Agent to check

        Returns:
            True if responsive
        """
        # Check if tmux session exists first
        if agent.tmux_session_name:
            if not self.agent_manager.tmux_server.has_session(agent.tmux_session_name):
                logger.warning(f"Agent {agent.id} tmux session {agent.tmux_session_name} missing")
                return False

        # Check last activity time
        if agent.last_activity:
            time_since_activity = datetime.utcnow() - agent.last_activity
            max_idle = timedelta(minutes=self.config.stuck_detection_minutes)

            if time_since_activity > max_idle:
                return False

        # Check tmux output for activity
        output = self.agent_manager.get_agent_output(agent.id, lines=50)
        if not output:
            return False

        # Check for stuck patterns
        cli_agent = get_cli_agent(agent.cli_type)
        if cli_agent.is_stuck(output):
            return False

        return True

    def _is_task_timed_out(self, agent: Agent) -> bool:
        """Check if agent's task has timed out.

        Args:
            agent: Agent to check

        Returns:
            True if timed out
        """
        session = self.db_manager.get_session()
        task = session.query(Task).filter_by(id=agent.current_task_id).first()
        session.close()

        if not task or not task.started_at:
            return False

        # Calculate timeout based on complexity
        complexity = task.estimated_complexity or 5
        timeout_minutes = self.config.agent_timeout_minutes * (1 + complexity / 10)

        time_on_task = datetime.utcnow() - task.started_at
        return time_on_task > timedelta(minutes=timeout_minutes)

    async def _handle_stuck_agent(self, agent: Agent):
        """Handle a stuck agent with trajectory-based intervention.

        Args:
            agent: Stuck agent
        """
        logger.info(f"Handling stuck agent {agent.id} with trajectory analysis")

        # Build accumulated context for better understanding
        accumulated_context = self.trajectory_context.build_accumulated_context(
            agent_id=agent.id,
            include_full_history=True,
        )

        # Check for specific issues in trajectory
        # Guardian only steers as last resort (health_check_failures >= 3)
        blockers = accumulated_context.get('discovered_blockers', [])
        if blockers and agent.health_check_failures >= 3:
            logger.info(f"Agent {agent.id} has blockers ({agent.health_check_failures} failures): {blockers}")

            # Last resort: try to help with top 3 blockers
            for blocker in blockers[:3]:
                message = f"I see you're blocked on: {blocker}. Try a different approach or create a sub-task if it's complex."
                await self.guardian.steer_agent(
                    agent=agent,
                    steering_type='last_resort_stuck',
                    message=message,
                )
        elif blockers:
            # Not enough failures yet — just log for observability
            logger.info(f"Agent {agent.id} has blockers (will steer after 3+ failures): {blockers[:2]}")
        else:
            # No blockers — just do trajectory analysis
            analysis = await self.intelligent_monitor.analyze_agent_state(agent)
            await self.intelligent_monitor.execute_intervention(agent, analysis)

    async def _handle_missing_tmux_session(self, agent: Agent):
        """Handle an agent with a missing tmux session by restarting it.

        Args:
            agent: Agent with missing tmux session
        """
        logger.info(f"Handling missing tmux session for agent {agent.id}")

        # Use the restart agent functionality which will recreate the tmux session
        await self.agent_manager.restart_agent(
            agent.id,
            f"Tmux session {agent.tmux_session_name} was missing, recreating"
        )

    async def _check_phase_progression(self):
        """Check workflow phases for progression needs."""
        logger.debug("Checking phase progression")

        # Get current phase status
        workflow_status = self.phase_manager.get_workflow_status()
        if not workflow_status or "error" in workflow_status:
            return

        # Don't advance a paused/failed workflow, unless it auto-recovers.
        wf_db_status = workflow_status.get("workflow_status", "active")
        if wf_db_status == "paused":
            # Auto-resume: if the stalled phase now has a done task, un-pause and continue.
            try:
                from src.core.database import Workflow, Task as DBTask
                session_check = self.db_manager.get_session()
                wf_obj = session_check.query(Workflow).filter_by(
                    id=self.phase_manager.workflow_id
                ).first()
                if wf_obj:
                    phases_status = workflow_status.get("phases", [])
                    in_prog = [p for p in phases_status if p["status"] == "in_progress"]
                    if in_prog:
                        stalled_phase_id = in_prog[0].get("id")
                        done_task = session_check.query(DBTask).filter_by(
                            phase_id=stalled_phase_id, status="done"
                        ).first()
                        if done_task:
                            logger.info(
                                f"[PHASE-PROGRESSION] Paused workflow has done task in stalled phase "
                                f"— auto-resuming."
                            )
                            wf_obj.status = "active"
                            session_check.commit()
                            wf_db_status = "active"
            except Exception as _e:
                logger.debug(f"[PHASE-PROGRESSION] Auto-resume check failed: {_e}")
            finally:
                try:
                    session_check.close()
                except Exception:
                    pass
        if wf_db_status != "active":
            logger.debug(f"[PHASE-PROGRESSION] Workflow is {wf_db_status} — skipping phase advancement")
            return

        phases = workflow_status.get("phases", [])

        # Find the most recently completed phase whose next phase is still pending.
        # This handles the case where Phase N completed but Phase N+1 was never
        # promoted to in_progress (e.g., after a workflow restart or race condition).
        completed_phases = [p for p in phases if p["status"] == "completed"]
        pending_phases = [p for p in phases if p["status"] == "pending"]
        in_progress_phases = [p for p in phases if p["status"] == "in_progress"]

        # Don't advance to future phases when a GOTO has rewound execution to an
        # earlier phase that is still running. Without this guard, the
        # _check_phase_progression logic sees the already-completed gated phase
        # (e.g. qa_validation) and its still-pending successor (product_validation)
        # and fires a second task for the successor while the rewound phase is
        # still in progress — producing spurious parallel execution.
        if completed_phases and pending_phases and not in_progress_phases:
            # The highest-order completed phase that has a pending successor
            completed_phases.sort(key=lambda p: p["order"])
            last_completed = completed_phases[-1]
            has_pending_successor = any(
                p["order"] == last_completed["order"] + 1 for p in pending_phases
            )
            if has_pending_successor:
                session = self.db_manager.get_session()
                try:
                    from src.core.database import Phase, Task as _T2
                    # Find the completed phase and re-evaluate transition via engine
                    completed_phase = session.query(Phase).filter_by(
                        workflow_id=self.phase_manager.workflow_id,
                        order=last_completed["order"]
                    ).first()
                    if completed_phase:
                        # Find the pending successor phase
                        successor_phase = session.query(Phase).filter_by(
                            workflow_id=self.phase_manager.workflow_id,
                            order=last_completed["order"] + 1,
                        ).first()
                        # Skip re-evaluation if the successor already has any tasks
                        # (failed counts — the transition fired but the task didn't
                        # survive). Re-triggering mark_phase_complete here would call
                        # the engine a second time and could produce a spurious retry
                        # of the already-completed phase.
                        if successor_phase:
                            existing_tasks = session.query(_T2).filter_by(
                                phase_id=successor_phase.id
                            ).count()
                            if existing_tasks > 0:
                                logger.debug(
                                    f"[PHASE-PROGRESSION] {completed_phase.name} completed, "
                                    f"{successor_phase.name} has {existing_tasks} task(s) — "
                                    "transition already fired, skipping re-evaluation"
                                )
                                session.close()
                                return
                        logger.info(f"[PHASE-PROGRESSION] Phase {completed_phase.name} (order {completed_phase.order}) completed, "
                                    f"but next phase is pending. Re-evaluating transition.")
                        phase_output = self._build_spec_phase_output(completed_phase.name)
                        result = self.phase_manager.mark_phase_complete(
                            completed_phase.id,
                            f"Phase completed with {last_completed['tasks']['completed']} tasks",
                            phase_output=phase_output,
                        )
                        if result.get("action") == "arbitrate":
                            logger.info(f"[PHASE-PROGRESSION] Arbitration needed for {completed_phase.name}")
                            await self._spawn_arbitration_agent(
                                completed_phase.workflow_id,
                                completed_phase.id,
                                completed_phase.name,
                                result.get("arbitration_metadata", {}),
                            )
                        elif result.get("should_continue") and result.get("target_phase_id"):
                            logger.info(f"[PHASE-PROGRESSION] Engine decision: {result.get('action')} -> {result.get('target_phase')}")
                            await self._create_phase_task_and_agent(
                                completed_phase.workflow_id,
                                result["target_phase_id"],
                                result["target_phase"],
                                result["action"],
                            )
                finally:
                    session.close()

        for phase_info in phases:
            if phase_info["status"] == "in_progress":
                # Get detailed phase from database
                session = self.db_manager.get_session()
                try:
                    from src.core.database import Phase
                    phase = session.query(Phase).filter_by(
                        workflow_id=self.phase_manager.workflow_id,
                        order=phase_info["order"]
                    ).first()

                    if not phase:
                        continue

                    # Check if phase is complete
                    if self.phase_manager.check_phase_completion(phase.id):
                        logger.info(f"Phase {phase.name} appears complete, evaluating transition")

                        # Hybrid spec gate (§9.1): for gated phases (QA / product
                        # validation) score the agent's structured result against
                        # the spec and pass it as phase_output, so the engine's
                        # evaluation point drives goto/retry/continue. Other phases
                        # pass {} and use the engine's default evaluation.
                        phase_output = self._build_spec_phase_output(phase.name)

                        # Mark phase as complete and get evaluation result
                        result = self.phase_manager.mark_phase_complete(
                            phase.id,
                            f"Phase completed with {phase_info['tasks']['completed']} tasks",
                            phase_output=phase_output,
                        )

                        # Create task+agent for the resolved target phase
                        if result.get("action") == "arbitrate":
                            await self._spawn_arbitration_agent(
                                phase.workflow_id,
                                phase.id,
                                phase.name,
                                result.get("arbitration_metadata", {}),
                            )
                        elif result.get("should_continue") and result.get("target_phase_id"):
                            await self._create_phase_task_and_agent(
                                phase.workflow_id,
                                result["target_phase_id"],
                                result["target_phase"],
                                result["action"],
                            )
                    else:
                        # Phase not complete — check for the stalled-phase pattern:
                        # all tasks failed (output floor or agent crash), no active tasks.
                        # Without this, the phase hangs forever when an agent exits after
                        # getting a rejected update_task_status (§11.2 fix #1).
                        from src.core.database import Task as _T
                        active_tasks = session.query(_T).filter(
                            _T.phase_id == phase.id,
                            _T.status.in_(["pending", "assigned", "in_progress",
                                           "under_review", "validation_in_progress"]),
                        ).count()
                        failed_tasks = session.query(_T).filter_by(
                            phase_id=phase.id, status="failed"
                        ).count()
                        done_tasks = session.query(_T).filter_by(
                            phase_id=phase.id, status="done"
                        ).count()
                        # Check if an arbitration task just completed for this phase
                        arb_done = session.query(_T).filter(
                            _T.phase_id == phase.id,
                            _T.created_by_agent_id == "arbitration",
                            _T.status == "done",
                        ).first()
                        arb_pending = session.query(_T).filter(
                            _T.phase_id == phase.id,
                            _T.created_by_agent_id == "arbitration",
                            _T.status.in_(["pending", "assigned", "in_progress"]),
                        ).first()
                        if arb_done and not arb_pending:
                            await self._check_arbitration_completion(
                                phase.workflow_id, phase.id, phase.name
                            )
                        elif active_tasks == 0 and failed_tasks > 0 and done_tasks == 0:
                            logger.warning(
                                f"[PHASE-PROGRESSION] Phase {phase.name} stalled: "
                                f"{failed_tasks} failed task(s), no active tasks — "
                                f"creating retry (bounded by MAX_PHASE_ATTEMPTS)"
                            )
                            await self._create_phase_task_and_agent(
                                phase.workflow_id, phase.id, phase.name, "retry"
                            )

                finally:
                    session.close()

        # 3c-bis. Fire gate for completed gated phases that the monitor missed.
        # The in_progress loop above won't catch phases that already flipped to
        # completed. And _check_phase_progression only fires when the next phase
        # is pending (not in_progress). This catches the gap: if a gated phase
        # completed and the gate hasn't fired yet, fire it now.
        from src.autopilot.spec import GATED_PHASES
        for phase_info in phases:
            if phase_info["status"] == "completed" and phase_info["name"] in GATED_PHASES:
                session = self.db_manager.get_session()
                try:
                    from src.core.database import Phase, PhaseExecution
                    phase = session.query(Phase).filter_by(
                        workflow_id=self.phase_manager.workflow_id,
                        order=phase_info["order"]
                    ).first()
                    if not phase:
                        continue
                    execution = session.query(PhaseExecution).filter_by(phase_id=phase.id).first()
                    if execution and execution.status == "completed":
                        # Phase already marked complete — check if we logged the gate
                        if not hasattr(self, '_gated_phases_fired'):
                            self._gated_phases_fired = set()
                        if phase.id not in self._gated_phases_fired:
                            phase_output = self._build_spec_phase_output(phase.name)
                            if phase_output:  # Non-empty = gated phase has output
                                logger.info(f"[SPEC-GATE] {phase.name}: gate fired from completion path (missed by monitor)")
                                self._gated_phases_fired.add(phase.id)
                                # Re-evaluate: if score < 0.7, engine will GOTO dev
                                result = self.phase_manager.mark_phase_complete(
                                    phase.id,
                                    f"Phase completed (gate fired from completion path)",
                                    phase_output=phase_output,
                                )
                                if result.get("action") == "goto" and result.get("target_phase_id"):
                                    logger.info(f"[SPEC-GATE] {phase.name}: GOTO {result.get('target_phase')} (score too low)")
                                    await self._create_phase_task_and_agent(
                                        phase.workflow_id,
                                        result["target_phase_id"],
                                        result["target_phase"],
                                        result["action"],
                                    )
                                elif result.get("action") == "continue":
                                    logger.info(f"[SPEC-GATE] {phase.name}: PASSED (score >= 0.7)")
                finally:
                    session.close()

    def _build_spec_phase_output(self, phase_name: str) -> dict:
        """Compute the hybrid spec-gate phase_output for a gated phase.

        Returns {"score": float, "spec_gate": {...}} for QA/product-validation
        phases (read from the workflow's working directory), else {}.
        """
        try:
            from src.autopilot.spec import build_phase_output, GATED_PHASES
            if phase_name not in GATED_PHASES:
                return {}

            from src.core.database import Workflow
            wd = None
            session = self.db_manager.get_session()
            try:
                wf = session.query(Workflow).filter_by(
                    id=self.phase_manager.workflow_id
                ).first()
                wd = wf.working_directory if wf else None
            finally:
                session.close()

            if not wd:
                logger.warning(f"[SPEC-GATE] {phase_name}: working_directory is None on workflow {self.phase_manager.workflow_id}, cannot score")
                return {}
            phase_output = build_phase_output(phase_name, wd)
            logger.info(f"[SPEC-GATE] {phase_name}: {phase_output}")
            return phase_output
        except Exception as e:
            logger.warning(f"[SPEC-GATE] could not build phase_output for {phase_name}: {e}")
            return {}

    def _write_agent_tmux_log(self, agent_id: str, phase_name: str, tmux_output: str) -> None:
        """Write the agent's full tmux scrollback to docs/tmux/<phase>_<agent_id>.log.

        Called on every monitor cycle — overwrites so the file always contains
        the complete captured session up to the most recent poll. The forensics
        phase reads these files for a full picture of what each agent did.
        """
        if not tmux_output or not self.phase_manager or not self.phase_manager.workflow_id:
            return
        try:
            from pathlib import Path as _P
            from src.core.database import Workflow as _Workflow
            session = self.db_manager.get_session()
            try:
                wf = session.query(_Workflow).filter_by(
                    id=self.phase_manager.workflow_id
                ).first()
                wd = wf.working_directory if wf else None
            finally:
                session.close()

            if not wd:
                return

            # Resolve to project root so logs survive worktree removal.
            # The working_directory may be a worktree (.worktrees/wt_*);
            # walk up past the .worktrees dir to get the stable project root.
            wd_path = _P(wd)
            if WORKTREES_SUBDIR in wd_path.parts:
                for parent in wd_path.parents:
                    if parent.name == WORKTREES_SUBDIR:
                        wd_path = parent.parent
                        break

            # .hephaestus/ is git-excluded — run artifacts never get committed
            tmux_dir = wd_path / CONTEXT_DIR_NAME / "tmux"
            tmux_dir.mkdir(parents=True, exist_ok=True)
            log_file = tmux_dir / f"{phase_name}_{agent_id[:8]}.log"
            log_file.write_text(tmux_output)
            logger.debug(f"[TMUX-LOG] {phase_name}/{agent_id[:8]}: wrote {len(tmux_output)} chars")

            # Update the manifest so forensics can enumerate logs without ls truncation.
            import json as _json
            manifest_path = tmux_dir / "tmux_log_manifest.json"
            manifest: dict = {}
            if manifest_path.exists():
                try:
                    manifest = _json.loads(manifest_path.read_text())
                except Exception:
                    manifest = {}
            manifest[f"{phase_name}_{agent_id[:8]}"] = str(log_file)
            manifest_path.write_text(_json.dumps(manifest, indent=2))
        except Exception as e:
            logger.debug(f"[TMUX-LOG] Failed to write log for {agent_id[:8]}: {e}")

    async def _create_phase_task_and_agent(
        self, workflow_id: str, phase_id: str, phase_name: str, action: str
    ):
        """Create task and agent for a specific phase (used after engine evaluation).

        This replaces the sequential-only _create_next_phase_task with a target-aware
        version that handles CONTINUE, GOTO, and RETRY.

        Args:
            workflow_id: Workflow ID
            phase_id: Target phase UUID
            phase_name: Target phase name (for logging)
            action: Engine action ('continue', 'goto', 'retry')
        """
        session = self.db_manager.get_session()
        try:
            from src.core.database import Phase, Task, PhaseExecution, Workflow
            import uuid

            # Bail immediately if the workflow is no longer active.
            wf_check = session.query(Workflow).filter_by(id=workflow_id).first()
            if not wf_check or wf_check.status not in ("active", "paused"):
                logger.debug(f"[PHASE-TASK] Workflow {workflow_id[:8]} is {getattr(wf_check, 'status', 'missing')} — skipping task creation for {phase_name}")
                return

            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                logger.error(f"Target phase not found: {phase_id}")
                return

            # Check if phase already has an active task
            existing_task = session.query(Task).filter(
                Task.phase_id == phase_id,
                Task.status.in_(["pending", "assigned", "in_progress", "queued"])
            ).first()
            if existing_task:
                logger.info(f"Phase {phase_name} already has active task {existing_task.id[:8]}, skipping creation")
                return

            # Guard against the timing gap: task is done but its agent cleanup is
            # still in flight (tmux session alive, agent row still "working").
            # Without this, the monitor would spawn a second agent while the first is
            # still running — causing 2-3 agents competing on the same phase.
            # Primary: check the agents table directly (covers any action).
            from src.core.database import Agent as _Agent
            active_phase_agent = (
                session.query(_Agent)
                .filter(_Agent.status.in_(["working", "idle", "starting"]))
                .join(Task, Task.assigned_agent_id == _Agent.id)
                .filter(Task.phase_id == phase_id)
                .first()
            )
            if active_phase_agent:
                logger.info(
                    f"Phase {phase_name} has active agent {active_phase_agent.id[:8]} "
                    f"(task done but cleanup pending) — skipping creation"
                )
                return

            # Backstop: in-memory per-phase cooldown (30 s) catches cases where the
            # agent row is already cleaned up but a second monitor path fires in the
            # same cycle before the first task is committed.
            if not hasattr(self, "_phase_last_created"):
                self._phase_last_created: dict = {}
            last_t = self._phase_last_created.get(phase_id, 0.0)
            if time.time() - last_t < 30:
                logger.info(
                    f"Phase {phase_name} task created {int(time.time() - last_t)}s ago — "
                    f"skipping (30 s cooldown)"
                )
                return
            self._phase_last_created[phase_id] = time.time()

            execution = session.query(PhaseExecution).filter_by(phase_id=phase_id).first()

            # Idempotency for forward 'continue': the first continue into a phase sets
            # its execution to 'in_progress' (below). A duplicate continue therefore
            # finds the execution already 'in_progress' with the phase's task already
            # 'done' — skip it. The active-task guard above misses this because the
            # prior task is already 'done', not active. A goto/retry re-enters from
            # 'completed'/'pending' (not 'in_progress') and uses action != 'continue',
            # so legitimate re-runs still proceed.
            if action == "continue" and execution and execution.status == "in_progress":
                done_task = session.query(Task).filter(
                    Task.phase_id == phase_id,
                    Task.status == "done",
                ).first()
                if done_task:
                    logger.info(f"Phase {phase_name} execution already in_progress with completed task {done_task.id[:8]} — skipping duplicate (continue)")
                    return

            # Bound re-entry (retry/goto). The engine can decide 'retry'/'goto' for a
            # phase on every monitor cycle; without a cap that creates an unbounded pile
            # of phase tasks and the phase never advances (observed: architecture_design
            # retried each cycle, accumulating tasks). Count only monitor-created phase
            # tasks (agent sub-tasks share the phase_id but aren't re-entries). Per the
            # bounded-recovery principle, stop after MAX_PHASE_ATTEMPTS.
            MAX_PHASE_ATTEMPTS = 3
            if action in ("retry", "goto"):
                # Only count retry/goto tasks — successful 'continue' tasks are
                # forward progress, not re-entries, and shouldn't count toward
                # the retry bound.
                monitor_retries = session.query(Task).filter(
                    Task.phase_id == phase_id,
                    Task.created_by_agent_id == "monitor",
                    Task.action.in_(["retry", "goto"]),
                ).count()
                if monitor_retries >= MAX_PHASE_ATTEMPTS:
                    logger.warning(
                        f"[PHASE-PROGRESSION] Phase {phase_name} hit the {action} bound "
                        f"({monitor_retries}/{MAX_PHASE_ATTEMPTS} retry/goto attempts) — pausing the "
                        f"workflow for human review (impasse, §9.4) instead of looping forever."
                    )
                    # Bounded-recovery exhaustion on a phase → impasse, not silent
                    # continue (§9.4 / §11.2). Pause the run so a human can Resume/Rerun
                    # from the UI rather than accumulating phase tasks indefinitely.
                    from src.core.database import Workflow
                    wf = session.query(Workflow).filter_by(id=workflow_id).first()
                    if wf and wf.status == "active":
                        wf.status = "paused"
                        session.commit()
                    return

            # Create task
            task_id = str(uuid.uuid4())
            task_description = f"Execute {phase.name}: {phase.description}"
            done_definition = " AND ".join(phase.done_definitions) if phase.done_definitions else "Complete phase objectives"

            task = Task(
                id=task_id,
                raw_description=task_description,
                enriched_description=task_description,
                done_definition=done_definition,
                status="pending",
                priority="high",
                phase_id=phase.id,
                workflow_id=workflow_id,
                created_by_agent_id="monitor",
                action=action,
            )
            session.add(task)

            # Ensure phase execution is in_progress (reuse `execution` from above)
            if execution and execution.status in ("pending", "completed"):
                execution.status = "in_progress"
                from datetime import datetime
                execution.started_at = datetime.utcnow()

            session.commit()

            logger.info(f"[{action.upper()}] Created task for phase {phase_name} (task_id={task_id[:8]})")

            # Create agent
            try:
                phase_cli_tool = phase.cli_tool
                phase_cli_model = phase.cli_model
                phase_glm_token_env = phase.glm_api_token_env
                phase_thinking_level = getattr(phase, 'thinking_level', None)

                # Ensure agent_manager has phase_manager so spawned agents get phase context
                if self.phase_manager and not self.agent_manager.phase_manager:
                    self.agent_manager.phase_manager = self.phase_manager

                project_context = await self.agent_manager.get_project_context()

                agent = await self.agent_manager.create_agent_for_task(
                    task=task,
                    enriched_data={"enriched_description": task_description},
                    memories=[],
                    project_context=project_context,
                    working_directory=None,
                    phase_cli_tool=phase_cli_tool,
                    phase_cli_model=phase_cli_model,
                    phase_glm_token_env=phase_glm_token_env,
                    phase_thinking_level=phase_thinking_level,
                )

                task.assigned_agent_id = agent.id
                task.status = "in_progress"
                from datetime import datetime
                task.started_at = datetime.utcnow()
                session.commit()

                logger.info(f"[{action.upper()}] Created agent {agent.id[:8]} for phase {phase_name}")

            except Exception as agent_err:
                logger.error(f"Failed to create agent for task {task_id[:8]}: {agent_err}")
                try:
                    task.status = "queued"
                    from datetime import datetime
                    task.queued_at = datetime.utcnow()
                    session.commit()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Failed to create phase task+agent: {e}")
            session.rollback()
        finally:
            session.close()

    async def _handle_timeout(self, agent: Agent):
        """Handle a timed-out agent.

        Args:
            agent: Timed-out agent
        """
        logger.warning(f"Handling timeout for agent {agent.id}")

        # Force analysis with timeout context
        analysis = {
            "state": AgentState.UNRECOVERABLE.value,
            "decision": MonitoringDecision.RECREATE.value,
            "message": "",
            "reasoning": "Task timed out, creating new agent with fresh approach",
            "confidence": 0.9,
        }

        await self.intelligent_monitor.execute_intervention(agent, analysis)

    async def _audit_system_health(self):
        """Audit system health across all autopilot workflows.

        Delegates to shared run_health_audit() function.
        """
        from src.mcp.autopilot_api import run_health_audit
        result = run_health_audit(self.db_manager)

        # Log findings
        for f in result["findings"]:
            log_fn = logger.warning if f["severity"] in ("warning", "error") else logger.info
            log_fn(f"[HEALTH] {f['type']}: {f['message']}")

        # Store for API access
        self._health_findings = result["findings"]

        # Task stuck detection: tasks in_progress > 10min with no active agent
        try:
            session = self.db_manager.get_session()
            from src.core.database import Task, Agent
            from datetime import datetime, timedelta
            stale_cutoff = datetime.utcnow() - timedelta(minutes=10)
            stuck_tasks = session.query(Task).filter(
                Task.status == "in_progress",
                Task.started_at < stale_cutoff,
                Task.started_at.isnot(None),
            ).all()
            for task in stuck_tasks:
                # Check if agent is still active
                agent = session.query(Agent).filter_by(
                    id=task.assigned_agent_id, status="working"
                ).first() if task.assigned_agent_id else None
                if not agent:
                    # If the agent called update_task_status(done) but the session
                    # was killed before the response was processed, completion_notes
                    # will be set. Promote to done instead of failing.
                    if task.completion_notes:
                        logger.info(
                            f"[HEALTH] Task {task.id[:8]} stuck in_progress but has "
                            f"completion_notes — promoting to done (agent finished then crashed)"
                        )
                        task.status = "done"
                        task.completed_at = datetime.utcnow()
                    else:
                        logger.warning(f"[HEALTH] Task {task.id[:8]} stuck in_progress for >10min with no active agent — marking failed")
                        task.status = "failed"
                        task.failure_reason = "Task stuck: no active agent for >10 minutes"
                    session.commit()
        except Exception as e:
            logger.error(f"Error in task stuck detection: {e}")
        finally:
            session.close()

    async def _cleanup_orphaned_tmux_sessions(self):
        """Clean up tmux sessions that don't have corresponding active agents.
        Also clean up orphaned agents (working but no active workflow)."""
        logger.debug("Starting orphaned tmux session cleanup")

        try:
            # Get all tmux sessions that start with 'agent' (the new naming convention)
            agent_sessions = []
            for session in self.agent_manager.tmux_server.sessions:
                if session.name.startswith('agent'):
                    agent_sessions.append(session.name)

            if not agent_sessions:
                logger.debug("No agent tmux sessions found")
                return

            logger.debug(f"Found {len(agent_sessions)} agent tmux sessions: {agent_sessions}")

            # Get all active agent session names from database
            session = self.db_manager.get_session()
            try:
                active_agents = session.query(Agent).filter(
                    Agent.status.in_(['working', 'pending', 'assigned'])
                ).all()

                active_session_names = {
                    agent.tmux_session_name for agent in active_agents
                    if agent.tmux_session_name
                }

                logger.debug(f"Found {len(active_session_names)} active agent sessions: {active_session_names}")

                # Clean up orphaned agents (working but no active workflow)
                from src.core.database import Workflow
                active_workflow_ids = {wf.id for wf in session.query(Workflow).filter(
                    Workflow.status.in_(['active', 'running'])
                ).all()}

                for agent in active_agents:
                    if agent.current_task_id:
                        task = session.query(Task).filter_by(id=agent.current_task_id).first()
                        if task and task.workflow_id and task.workflow_id not in active_workflow_ids:
                            logger.info(f"Terminating orphaned agent {agent.id[:8]} - workflow {task.workflow_id[:8]} not active")
                            agent.status = 'terminated'
                session.commit()

            finally:
                session.close()

            # Find orphaned sessions (exist in tmux but not in database)
            # Use grace period based on last check time to avoid killing newly-created sessions
            GRACE_PERIOD_SECONDS = 120
            current_time = datetime.now()
            
            # Track when we last checked - agents created since last check get grace period
            if not hasattr(self, '_last_orphan_check_time'):
                self._last_orphan_check_time = current_time
                logger.debug("First orphan check - skipping all sessions for grace period")
                return
            
            time_since_last_check = (current_time - self._last_orphan_check_time).total_seconds()
            
            orphaned_sessions = []
            for tmux_sess in self.agent_manager.tmux_server.sessions:
                if tmux_sess.name not in agent_sessions:
                    continue
                if tmux_sess.name in active_session_names:
                    continue
                
                # Apply grace period: if we just started monitoring or haven't checked in a while,
                # skip orphan detection to let new agents get registered in DB
                if time_since_last_check < GRACE_PERIOD_SECONDS:
                    logger.debug(f"Skipping session {tmux_sess.name} - within grace period ({time_since_last_check:.0f}s < {GRACE_PERIOD_SECONDS}s)")
                    continue
                    
                orphaned_sessions.append(tmux_sess.name)
            
            # Update last check time
            self._last_orphan_check_time = current_time

            if not orphaned_sessions:
                logger.debug("No orphaned tmux sessions found")
                return

            logger.info(f"Found {len(orphaned_sessions)} orphaned tmux sessions (after grace period): {orphaned_sessions}")

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
                    logger.warning(f"Failed to kill orphaned session {session_name}: {e}")

            if killed_count > 0:
                logger.info(f"Successfully cleaned up {killed_count} orphaned tmux sessions")

        except Exception as e:
            logger.error(f"Error during tmux session cleanup: {e}")
            raise

    async def _check_workflow_stuck_state(self):
        """Check if workflow is stuck and needs diagnostic agent.

        Triggers diagnostic agent if:
        1. Active workflow exists
        2. Task count > 0
        3. All tasks are finished (done/failed/duplicated)
        4. No validated result submitted
        5. Cooldown period has passed since last diagnostic run
        """
        logger.warning("[DIAGNOSTIC MONITOR] ============================================")
        logger.warning("[DIAGNOSTIC MONITOR] 🔍 _check_workflow_stuck_state() CALLED!")
        logger.warning("[DIAGNOSTIC MONITOR] ============================================")
        logger.info("[DIAGNOSTIC MONITOR] Starting workflow stuck state check...")

        # Condition tracking for debug report
        conditions = {
            "enabled": self.config.diagnostic_agent_enabled,
            "workflow_exists": False,
            "has_tasks": False,
            "all_tasks_finished": False,
            "no_validated_result": False,
            "cooldown_passed": False,
            "stuck_long_enough": False,
        }

        if not self.config.diagnostic_agent_enabled:
            logger.info("[DIAGNOSTIC MONITOR] ❌ Diagnostic agent disabled in config")
            self._log_diagnostic_status_report(conditions, trigger=False, reason="Disabled in config")
            return

        if not self.phase_manager or not self.phase_manager.workflow_id:
            logger.info("[DIAGNOSTIC MONITOR] ❌ No active workflow")
            self._log_diagnostic_status_report(conditions, trigger=False, reason="No active workflow")
            return

        conditions["workflow_exists"] = True
        workflow_id = self.phase_manager.workflow_id
        logger.info(f"[DIAGNOSTIC MONITOR] ✅ Workflow exists: {workflow_id[:8]}")

        session = self.db_manager.get_session()
        try:
            # Step 1: Check if we have tasks
            from src.core.database import Task, WorkflowResult, DiagnosticRun

            tasks = session.query(Task).filter(
                Task.workflow_id == workflow_id
            ).all()

            if not tasks:
                logger.info("[DIAGNOSTIC MONITOR] ❌ No tasks in workflow yet")
                self._log_diagnostic_status_report(conditions, trigger=False, reason="No tasks in workflow")
                return

            conditions["has_tasks"] = True
            logger.info(f"[DIAGNOSTIC MONITOR] ✅ Has tasks: {len(tasks)} total")

            # Step 2: Check if all tasks are finished
            active_statuses = ['pending', 'assigned', 'in_progress',
                              'under_review', 'validation_in_progress']
            active_tasks = [t for t in tasks if t.status in active_statuses]
            finished_tasks = [t for t in tasks if t.status not in active_statuses]

            if active_tasks:
                logger.info(f"[DIAGNOSTIC MONITOR] ❌ Tasks still active: {len(active_tasks)} active, {len(finished_tasks)} finished")
                self._log_diagnostic_status_report(conditions, trigger=False,
                                                   reason=f"{len(active_tasks)} active tasks remaining")
                return

            conditions["all_tasks_finished"] = True
            logger.info(f"[DIAGNOSTIC MONITOR] ✅ All tasks finished: {len(finished_tasks)} tasks")

            # Step 2.5: Check if a phase was recently completed (cooldown after phase completion)
            from src.core.database import PhaseExecution
            recent_phase_completion = session.query(PhaseExecution).filter(
                PhaseExecution.workflow_execution_id == workflow_id,
                PhaseExecution.status == 'completed',
                PhaseExecution.completed_at.isnot(None)
            ).order_by(PhaseExecution.completed_at.desc()).first()

            if recent_phase_completion:
                time_since_completion = (datetime.utcnow() - recent_phase_completion.completed_at).total_seconds()
                phase_cooldown = 120  # 2 minutes after phase completion
                if time_since_completion < phase_cooldown:
                    logger.info(f"[DIAGNOSTIC MONITOR] ❌ Phase recently completed ({recent_phase_completion.phase_id[:8]}), cooling down: {time_since_completion:.0f}s / {phase_cooldown}s")
                    self._log_diagnostic_status_report(conditions, trigger=False,
                                                       reason=f"Phase completed {time_since_completion:.0f}s ago, cooling down")
                    return

            # Step 3: Check if workflow is already marked complete/failed
            from src.core.database import Workflow as _WF
            wf_row = session.query(_WF).filter_by(id=workflow_id).first()
            if wf_row and wf_row.status in ('completed', 'failed', 'cancelled'):
                logger.info(f"[DIAGNOSTIC MONITOR] ❌ Workflow is {wf_row.status} — no diagnostic needed")
                self._log_diagnostic_status_report(conditions, trigger=False,
                                                   reason=f"Workflow status is {wf_row.status}")
                return

            validated_result = session.query(WorkflowResult).filter(
                WorkflowResult.workflow_id == workflow_id,
                WorkflowResult.status == 'validated'
            ).first()

            if validated_result:
                logger.info(f"[DIAGNOSTIC MONITOR] ❌ Workflow has validated result: {validated_result.id[:8]}")
                self._log_diagnostic_status_report(conditions, trigger=False, reason="Validated result exists")
                return

            conditions["no_validated_result"] = True

            # Check for any results (validated or not)
            all_results = session.query(WorkflowResult).filter(
                WorkflowResult.workflow_id == workflow_id
            ).all()
            if all_results:
                logger.info(f"[DIAGNOSTIC MONITOR] ✅ No validated result ({len(all_results)} unvalidated results exist)")
            else:
                logger.info("[DIAGNOSTIC MONITOR] ✅ No validated result (no results submitted)")

            # Step 4: Check cooldown period
            last_diagnostic = session.query(DiagnosticRun).filter(
                DiagnosticRun.workflow_id == workflow_id
            ).order_by(DiagnosticRun.triggered_at.desc()).first()

            if last_diagnostic:
                time_since_last = (datetime.utcnow() - last_diagnostic.triggered_at).total_seconds()
                if time_since_last < self.config.diagnostic_cooldown_seconds:
                    logger.info(f"[DIAGNOSTIC MONITOR] ❌ Cooldown active: {time_since_last:.0f}s / {self.config.diagnostic_cooldown_seconds}s required")
                    self._log_diagnostic_status_report(conditions, trigger=False,
                                                       reason=f"Cooldown active ({time_since_last:.0f}s < {self.config.diagnostic_cooldown_seconds}s)")
                    return
                else:
                    logger.info(f"[DIAGNOSTIC MONITOR] ✅ Cooldown passed: {time_since_last:.0f}s since last diagnostic")
            else:
                logger.info("[DIAGNOSTIC MONITOR] ✅ Cooldown passed: No previous diagnostic runs")

            conditions["cooldown_passed"] = True

            # Step 5: Check how long we've been stuck
            latest_task_time = max(
                (t.completed_at or t.created_at for t in tasks if t.completed_at or t.created_at),
                default=None
            )

            stuck_time = 0
            if latest_task_time:
                stuck_time = (datetime.utcnow() - latest_task_time).total_seconds()
                if stuck_time < self.config.diagnostic_min_stuck_time_seconds:
                    logger.info(f"[DIAGNOSTIC MONITOR] ❌ Not stuck long enough: {stuck_time:.0f}s / {self.config.diagnostic_min_stuck_time_seconds}s required")
                    self._log_diagnostic_status_report(conditions, trigger=False,
                                                       reason=f"Not stuck long enough ({stuck_time:.0f}s < {self.config.diagnostic_min_stuck_time_seconds}s)")
                    return
                else:
                    logger.info(f"[DIAGNOSTIC MONITOR] ✅ Stuck long enough: {stuck_time:.0f}s since last activity")
            else:
                logger.warning("[DIAGNOSTIC MONITOR] ⚠️  Could not determine stuck time (no task timestamps)")

            conditions["stuck_long_enough"] = True

            # ALL CONDITIONS MET - Trigger diagnostic agent
            logger.warning("[DIAGNOSTIC MONITOR] 🚨 WORKFLOW STUCK DETECTED - All conditions met!")
            logger.warning(f"[DIAGNOSTIC MONITOR] 🔥 Stuck for {stuck_time:.0f}s with no progress")
            self._log_diagnostic_status_report(conditions, trigger=True, stuck_time=stuck_time)

            await self._create_diagnostic_agent(workflow_id, tasks, stuck_time)

        except Exception as e:
            logger.error(f"[DIAGNOSTIC MONITOR] ❌ Error checking workflow stuck state: {e}", exc_info=True)
            session.rollback()
        finally:
            session.close()

    def _log_diagnostic_status_report(self, conditions: Dict[str, bool], trigger: bool, reason: str = None, stuck_time: float = 0):
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
        logger.info(f"[DIAGNOSTIC MONITOR] Enabled:              {'✅' if conditions['enabled'] else '❌'}")
        logger.info(f"[DIAGNOSTIC MONITOR] Workflow Exists:      {'✅' if conditions['workflow_exists'] else '❌'}")
        logger.info(f"[DIAGNOSTIC MONITOR] Has Tasks:            {'✅' if conditions['has_tasks'] else '❌'}")
        logger.info(f"[DIAGNOSTIC MONITOR] All Tasks Finished:   {'✅' if conditions['all_tasks_finished'] else '❌'}")
        logger.info(f"[DIAGNOSTIC MONITOR] No Validated Result:  {'✅' if conditions['no_validated_result'] else '❌'}")
        logger.info(f"[DIAGNOSTIC MONITOR] Cooldown Passed:      {'✅' if conditions['cooldown_passed'] else '❌'}")
        logger.info(f"[DIAGNOSTIC MONITOR] Stuck Long Enough:    {'✅' if conditions['stuck_long_enough'] else '❌'}")

        logger.info("[DIAGNOSTIC MONITOR] ───────────────────────────────────────")

        if trigger:
            logger.warning("[DIAGNOSTIC MONITOR] 🚨 RESULT: TRIGGERING DIAGNOSTIC AGENT")
            logger.warning(f"[DIAGNOSTIC MONITOR] 🔥 Stuck Time: {stuck_time:.0f}s")
        else:
            logger.info("[DIAGNOSTIC MONITOR] ✋ RESULT: NOT TRIGGERING")
            if reason:
                logger.info(f"[DIAGNOSTIC MONITOR] 📋 Reason: {reason}")

        logger.info("[DIAGNOSTIC MONITOR] ═══════════════════════════════════════")

    async def _create_diagnostic_agent(self, workflow_id: str, workflow_tasks: List, stuck_time: float):
        """Create and spawn a diagnostic agent.

        Args:
            workflow_id: ID of stuck workflow
            workflow_tasks: All tasks in the workflow
            stuck_time: How long we've been stuck (seconds)
        """
        import uuid
        from src.core.database import Task, DiagnosticRun

        logger.info(f"[DIAGNOSTIC MONITOR] 🔍 Creating diagnostic agent for workflow {workflow_id[:8]}")

        session = self.db_manager.get_session()
        try:
            # Gather context for diagnostic agent
            logger.info("[DIAGNOSTIC MONITOR] Gathering diagnostic context...")
            context = await self._gather_diagnostic_context(workflow_id, workflow_tasks, stuck_time)
            logger.info(f"[DIAGNOSTIC MONITOR] Context gathered: {len(context['phases_summary'])} phases, {len(context['agents_summary'])} agents reviewed")

            # Create diagnostic task on the most-recent active/done phase, not the first.
            current_phase_id = None
            for t in reversed(workflow_tasks):
                if t.phase_id and t.status in ('done', 'in_progress', 'failed'):
                    current_phase_id = t.phase_id
                    break
            
            if not current_phase_id:
                logger.warning("[DIAGNOSTIC MONITOR] No phase_id found on any task, skipping diagnostic creation")
                return

            task_id = str(uuid.uuid4())
            diagnostic_task = Task(
                id=task_id,
                raw_description="DIAGNOSTIC: Analyze why workflow has stalled and create tasks to progress toward goal",
                enriched_description=f"Diagnostic analysis for workflow {workflow_id[:8]} - {len(workflow_tasks)} tasks completed, stuck for {stuck_time:.0f}s",
                done_definition="Created 1-5 new tasks with clear phase assignments and completion criteria to push workflow toward its goal",
                status="pending",
                priority="high",
                workflow_id=workflow_id,
                created_by_agent_id="monitor",
                phase_id=current_phase_id,
            )
            session.add(diagnostic_task)
            session.flush()
            logger.info(f"[DIAGNOSTIC MONITOR] Created diagnostic task: {task_id[:8]}")

            # Create diagnostic run record
            run_id = str(uuid.uuid4())
            diagnostic_run = DiagnosticRun(
                id=run_id,
                workflow_id=workflow_id,
                diagnostic_task_id=task_id,
                total_tasks_at_trigger=len(workflow_tasks),
                done_tasks_at_trigger=len([t for t in workflow_tasks if t.status == 'done']),
                failed_tasks_at_trigger=len([t for t in workflow_tasks if t.status == 'failed']),
                time_since_last_task_seconds=int(stuck_time),
                workflow_goal=context['workflow_goal'],
                phases_analyzed=context['phases_summary'],
                agents_reviewed=context['agents_summary'],
                status="created",
            )
            session.add(diagnostic_run)
            session.commit()
            logger.info(f"[DIAGNOSTIC MONITOR] Created diagnostic run: {run_id[:8]}")

            # Generate diagnostic prompt
            logger.info("[DIAGNOSTIC MONITOR] Generating diagnostic prompt...")
            diagnostic_prompt = await self._generate_diagnostic_prompt(context)
            prompt_size = len(diagnostic_prompt)
            logger.info(f"[DIAGNOSTIC MONITOR] Prompt generated: {prompt_size} characters")

            # Spawn diagnostic agent (no worktree, works in main repo)
            enriched_data = {
                'enriched_description': diagnostic_task.enriched_description,
                'completion_criteria': [diagnostic_task.done_definition],
                'diagnostic_context': context,
                'validation_prompt': diagnostic_prompt,  # Use validation_prompt field for custom prompt
            }

            logger.info("[DIAGNOSTIC MONITOR] Spawning diagnostic agent...")
            agent = await self.agent_manager.create_agent_for_task(
                task=diagnostic_task,
                enriched_data=enriched_data,
                memories=[],  # Diagnostic agent gets everything in prompt
                project_context="",
                agent_type="diagnostic",
                use_existing_worktree=True,
                working_directory=str(self.config.main_repo_path),  # Use main repo
            )

            # Update diagnostic run with agent ID
            diagnostic_run.diagnostic_agent_id = agent.id
            diagnostic_run.status = "running"
            session.commit()

            logger.info("[DIAGNOSTIC MONITOR] ✅ Diagnostic agent created successfully!")
            logger.info(f"[DIAGNOSTIC MONITOR] Agent ID: {agent.id[:8]}")
            logger.info(f"[DIAGNOSTIC MONITOR] Task ID: {task_id[:8]}")
            logger.info(f"[DIAGNOSTIC MONITOR] Run ID: {run_id[:8]}")
            logger.info(f"[DIAGNOSTIC MONITOR] Workflow: {workflow_id[:8]}")

        except Exception as e:
            logger.error(f"[DIAGNOSTIC MONITOR] ❌ Failed to create diagnostic agent: {e}", exc_info=True)
            session.rollback()
            raise
        finally:
            session.close()

    async def _spawn_arbitration_agent(
        self,
        workflow_id: str,
        phase_id: str,
        phase_name: str,
        metadata: Dict[str, Any],
    ):
        """Spawn an LLM arbitration agent when a scope-gate GOTO budget is exhausted.

        The agent reads design.md + requirements_analysis.md + scope_review_result.json,
        decides PROCEED or IMPASSE, writes arbitration_result.json, and marks done.
        The monitor detects the completed arbitration task on the next poll cycle and
        calls mark_phase_complete(force_action=...) to resume the pipeline.
        """
        import uuid
        from src.core.database import Task

        logger.warning(f"[ARBITRATE] Spawning arbitration agent for phase {phase_name}")

        # Locate docs dir from any existing phase task for this workflow
        session = self.db_manager.get_session()
        try:
            docs_dir_str = ""
            sample_task = session.query(Task).filter_by(workflow_id=workflow_id).first()
            if sample_task and sample_task.raw_description:
                import re
                m = re.search(r"Docs Path[:\s]+([^\s]+)", sample_task.raw_description or "")
                if m:
                    docs_dir_str = m.group(1)

            # Guard: if arbitration task already pending/done for this phase, skip
            existing = session.query(Task).filter(
                Task.phase_id == phase_id,
                Task.created_by_agent_id == "arbitration",
                Task.status.in_(["pending", "assigned", "in_progress", "done"]),
            ).first()
            if existing:
                logger.info(f"[ARBITRATE] Arbitration task already exists for {phase_name} ({existing.id[:8]}) — skipping")
                return

            prompt = f"""You are an ARBITRATION AGENT. The scope_review → product_requirements
GOTO loop has exhausted its retry budget ({metadata.get('retries', '?')}/{metadata.get('max_retries', '?')} attempts).

Your job: read the design doc and the current requirements_analysis.md, then decide:
- PROCEED: requirements are close enough — the pipeline should continue to architecture.
- IMPASSE: requirements are fundamentally misaligned — human intervention is needed.

STEPS:
1. Read ./.hephaestus/design.md (the source-of-truth design document)
2. Read ./docs/requirements_analysis.md (the current requirements under review)
3. Read ./docs/scope_review_result.json (the last scope reviewer verdict + correction_instructions)
4. Make a judgement: are the requirements SUBSTANTIALLY faithful to the design, even if imperfect?
   - Minor wording differences → PROCEED
   - Extra low-risk requirements that logically follow from design → PROCEED
   - Wrong domain, completely missing core requirements, or fundamental mismatch → IMPASSE
5. Write ./docs/arbitration_result.json with EXACTLY this schema:
   {{
     "decision": "PROCEED",
     "reasoning": "one-paragraph explanation",
     "scope_drift_summary": "brief description of any accepted drift"
   }}
6. Mark your task done.

CRITICAL: Write the JSON and mark the task done. Do NOT rewrite requirements_analysis.md.
"""

            task_id = str(uuid.uuid4())
            task = Task(
                id=task_id,
                raw_description=f"ARBITRATION: Resolve scope_review/product_requirements loop for workflow {workflow_id[:8]}",
                enriched_description=prompt,
                done_definition="arbitration_result.json written to ./docs/ with decision PROCEED or IMPASSE",
                status="pending",
                priority="high",
                phase_id=phase_id,
                workflow_id=workflow_id,
                created_by_agent_id="arbitration",
                action="arbitrate",
            )
            session.add(task)
            session.commit()
            logger.info(f"[ARBITRATE] Created arbitration task {task_id[:8]} for phase {phase_name}")

            enriched_data = {
                "enriched_description": prompt,
                "completion_criteria": [task.done_definition],
                "validation_prompt": prompt,
            }
            agent = await self.agent_manager.create_agent_for_task(
                task=task,
                enriched_data=enriched_data,
                memories=[],
                project_context="",
                agent_type="diagnostic",
                use_existing_worktree=True,
                working_directory=str(self.config.main_repo_path),
            )
            logger.info(f"[ARBITRATE] ✅ Arbitration agent {agent.id[:8]} spawned for phase {phase_name}")

        except Exception as e:
            logger.error(f"[ARBITRATE] Failed to spawn arbitration agent: {e}", exc_info=True)
            session.rollback()
        finally:
            session.close()

    async def _check_arbitration_completion(self, workflow_id: str, phase_id: str, phase_name: str):
        """Poll for a completed arbitration task; if found, resolve the phase.

        Called from _check_phases() when a phase is in_progress with only arbitration
        tasks (no regular pending/in_progress tasks).
        """
        import json
        from pathlib import Path
        from src.core.database import Task, Workflow

        session = self.db_manager.get_session()
        try:
            done_arb = session.query(Task).filter(
                Task.phase_id == phase_id,
                Task.created_by_agent_id == "arbitration",
                Task.status == "done",
            ).first()
            if not done_arb:
                return

            # Read arbitration_result.json — try docs_dir derived from task description
            result_path = None
            for task in session.query(Task).filter_by(workflow_id=workflow_id).all():
                import re
                m = re.search(r"Docs Path[:\s]+(\S+)", task.raw_description or "")
                if m:
                    result_path = Path(m.group(1)) / "arbitration_result.json"
                    break
            # Fallback: check main repo docs/
            if not result_path:
                result_path = Path(str(self.config.main_repo_path)) / "docs" / "arbitration_result.json"

            decision = "IMPASSE"  # safe default
            reasoning = "arbitration_result.json not found"
            if result_path and result_path.exists():
                try:
                    data = json.loads(result_path.read_text())
                    decision = str(data.get("decision", "IMPASSE")).upper()
                    reasoning = data.get("reasoning", "")
                except Exception as e:
                    logger.error(f"[ARBITRATE] Could not read arbitration_result.json: {e}")

            logger.warning(f"[ARBITRATE] Arbitration decision for {phase_name}: {decision} — {reasoning[:100]}")

            if decision == "PROCEED":
                phase_output = self._build_spec_phase_output(phase_name)
                result = self.phase_manager.mark_phase_complete(
                    phase_id,
                    f"Arbitration: PROCEED — {reasoning[:120]}",
                    phase_output=phase_output,
                    force_action="continue",
                )
                if result.get("should_continue") and result.get("target_phase_id"):
                    await self._create_phase_task_and_agent(
                        workflow_id,
                        result["target_phase_id"],
                        result["target_phase"],
                        result["action"],
                    )
            else:
                # IMPASSE — pause the workflow for human review
                wf = session.query(Workflow).filter_by(id=workflow_id).first()
                if wf and wf.status == "active":
                    wf.status = "paused"
                    session.commit()
                logger.error(
                    f"[ARBITRATE] IMPASSE on {phase_name} after arbitration — "
                    f"workflow {workflow_id[:8]} paused for human review."
                )

        except Exception as e:
            logger.error(f"[ARBITRATE] _check_arbitration_completion error: {e}", exc_info=True)
        finally:
            session.close()

    async def _gather_diagnostic_context(self, workflow_id: str, workflow_tasks: List, stuck_time: float) -> Dict[str, Any]:
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
        from src.core.database import Agent, ConductorAnalysis, WorkflowResult, Phase

        session = self.db_manager.get_session()
        try:
            # Get workflow config
            workflow_config = self.phase_manager.get_workflow_config(workflow_id)
            workflow_goal = workflow_config.result_criteria if workflow_config else "Unknown goal"

            # Get all phases
            phases = session.query(Phase).filter(
                Phase.workflow_id == workflow_id
            ).order_by(Phase.order).all()

            phases_summary = []
            for phase in phases:
                phases_summary.append({
                    'id': phase.id,
                    'name': phase.name,
                    'order': phase.order,
                    'description': phase.description,
                    'done_definitions': phase.done_definitions,
                    'task_count': len([t for t in workflow_tasks if t.phase_id == phase.id]),
                    'done_task_count': len([t for t in workflow_tasks if t.phase_id == phase.id and t.status == 'done']),
                })

            # Get recent agents (last N completed/failed)
            task_ids = [t.id for t in workflow_tasks]
            recent_agents = session.query(Agent).filter(
                Agent.current_task_id.in_(task_ids),
                Agent.status.in_(['terminated'])
            ).order_by(Agent.created_at.desc()).limit(self.config.diagnostic_max_agents_to_analyze).all()

            agents_summary = []
            for agent in recent_agents:
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task:
                    agents_summary.append({
                        'agent_id': agent.id,
                        'task_id': task.id,
                        'task_description': task.enriched_description or task.raw_description,
                        'task_status': task.status,
                        'completion_notes': task.completion_notes,
                        'failure_reason': task.failure_reason,
                        'phase_id': task.phase_id,
                        'created_at': agent.created_at.isoformat(),
                        'agent_type': agent.agent_type,
                    })

            # Get recent Conductor analyses
            conductor_analyses = session.query(ConductorAnalysis).order_by(
                ConductorAnalysis.timestamp.desc()
            ).limit(self.config.diagnostic_max_conductor_analyses).all()

            conductor_overviews = []
            for analysis in conductor_analyses:
                conductor_overviews.append({
                    'timestamp': analysis.timestamp.isoformat(),
                    'system_status': analysis.system_status,
                    'coherence_score': analysis.coherence_score,
                    'num_agents': analysis.num_agents,
                    'duplicate_count': analysis.duplicate_count,
                })

            # Get submitted results (even if rejected)
            submitted_results = session.query(WorkflowResult).filter(
                WorkflowResult.workflow_id == workflow_id
            ).all()

            results_summary = []
            for result in submitted_results:
                results_summary.append({
                    'result_id': result.id,
                    'status': result.status,
                    'submitted_at': result.created_at.isoformat() if result.created_at else None,
                    'validation_feedback': result.validation_feedback,
                    'agent_id': result.agent_id,
                })

            # Calculate task statistics by phase
            tasks_by_phase = {}
            for phase in phases:
                phase_tasks = [t for t in workflow_tasks if t.phase_id == phase.id]
                tasks_by_phase[phase.name] = {
                    'total': len(phase_tasks),
                    'done': len([t for t in phase_tasks if t.status == 'done']),
                    'failed': len([t for t in phase_tasks if t.status == 'failed']),
                }

            return {
                'workflow_goal': workflow_goal,
                'workflow_id': workflow_id,
                'phases_summary': phases_summary,
                'agents_summary': agents_summary,
                'conductor_overviews': conductor_overviews,
                'submitted_results': results_summary,
                'total_tasks': len(workflow_tasks),
                'tasks_by_phase': tasks_by_phase,
                'time_since_last_task': stuck_time,
            }

        finally:
            session.close()

    async def _generate_diagnostic_prompt(self, context: Dict[str, Any]) -> str:
        """Generate diagnostic prompt from template.

        Args:
            context: Diagnostic context dictionary

        Returns:
            Formatted diagnostic prompt
        """
        from pathlib import Path

        # Load template
        template_path = Path(__file__).parent.parent / "prompts" / "diagnostic_agent_analysis.md"
        with open(template_path, 'r') as f:
            template = f.read()

        # Format phases info
        phases_info = []
        for phase in context['phases_summary']:
            phases_info.append(f"""
### Phase {phase['order']}: {phase['name']} (ID: {phase['id'][:8]})

**Description**: {phase['description']}

**Done Definitions**:
{chr(10).join(f"- {d}" for d in phase['done_definitions'])}

**Progress**: {phase['done_task_count']}/{phase['task_count']} tasks completed
""")

        # Format agent history
        agents_history = []
        for i, agent in enumerate(context['agents_summary'], 1):
            status_marker = "✅" if agent['task_status'] == 'done' else "❌"
            agents_history.append(f"""
**Agent {i}** (ID: {agent['agent_id'][:8]}, Type: {agent['agent_type']})
- **Task**: {agent['task_description']}
- **Status**: {status_marker} {agent['task_status']}
- **Phase**: {agent['phase_id'][:8] if agent['phase_id'] else 'None'}
- **Completed at**: {agent['created_at']}
{f"- **Notes**: {agent['completion_notes']}" if agent['completion_notes'] else ""}
{f"- **Failure reason**: {agent['failure_reason']}" if agent['failure_reason'] else ""}
""")

        # Format conductor overviews
        conductor_overviews = []
        for i, overview in enumerate(context['conductor_overviews'], 1):
            conductor_overviews.append(f"""
**Analysis {i}** ({overview['timestamp']}):
- System status: {overview['system_status']}
- Coherence score: {overview['coherence_score']:.2f}
- Active agents: {overview['num_agents']}
- Duplicates detected: {overview['duplicate_count']}
""")

        # Format tasks by phase
        tasks_by_phase_str = []
        for phase_name, stats in context['tasks_by_phase'].items():
            tasks_by_phase_str.append(
                f"  - {phase_name}: {stats['done']}/{stats['total']} done, {stats['failed']} failed"
            )

        # Format submitted results
        if context['submitted_results']:
            results_info = []
            for result in context['submitted_results']:
                status_marker = "✅" if result['status'] == 'validated' else "❌"
                results_info.append(f"""
- {status_marker} Result {result['result_id'][:8]}: {result['status']}
  - Submitted: {result['submitted_at']}
  - Feedback: {result['validation_feedback'] or 'None'}
""")
            submitted_results_info = '\n'.join(results_info)
        else:
            submitted_results_info = "No results have been submitted yet."

        # Calculate stuck time formatting
        stuck_seconds = context.get('time_since_last_task', 0)
        if stuck_seconds >= 3600:
            stuck_time_formatted = f"{stuck_seconds/3600:.1f} hours"
        elif stuck_seconds >= 60:
            stuck_time_formatted = f"{stuck_seconds/60:.1f} minutes"
        else:
            stuck_time_formatted = f"{stuck_seconds} seconds"

        # Replace placeholders
        prompt = template.format(
            workflow_goal=context['workflow_goal'],
            workflow_id=context['workflow_id'],
            phases_info='\n'.join(phases_info),
            agent_count=len(context['agents_summary']),
            agents_history='\n'.join(agents_history) if agents_history else "No agents have run yet.",
            conductor_overviews='\n'.join(conductor_overviews) if conductor_overviews else "No conductor analyses available.",
            total_tasks=context['total_tasks'],
            tasks_by_phase='\n'.join(tasks_by_phase_str),
            stuck_time_formatted=stuck_time_formatted,
            submitted_results_info=submitted_results_info,
            agent_id="{agent_id}",  # Will be replaced by agent manager
            task_id="{task_id}",  # Will be replaced by agent manager
        )

        return prompt