"""Intelligent monitoring and self-healing system for Hephaestus."""

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.agents.manager import AgentManager
from src.core.constants import CONTEXT_DIR_NAME, HEPHAESTUS_LOGS_DIR, WORKTREES_SUBDIR
from src.core.database import (
    Agent,
    AgentLog,
    ConductorAnalysis,
    DatabaseManager,
    DetectedDuplicate,
    GuardianAnalysis,
    Task,
    Workflow,
)
from src.core.simple_config import get_config
from src.interfaces import LLMProviderInterface, get_cli_agent
from src.memory.rag import RAGSystem
from src.monitoring.conductor import Conductor
from src.monitoring.guardian import Guardian
from src.monitoring.trajectory_context import TrajectoryContext
from src.phases import PhaseManager

logger = logging.getLogger(__name__)

_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# Matches pi's "⚠️ Dangerous command:" confirmation screen and captures the
# command it's asking about. Deliberately anchored to this exact prompt
# shape (not a generic "does the screen contain the word rm" check) to
# avoid false-positive matches on an agent's own reasoning text that merely
# mentions "rm" or "dangerous".
_DANGEROUS_CMD_RE = re.compile(
    r"Dangerous command:\s*\n\s*(\S[^\n]*)", re.IGNORECASE
)

# pi's exact error text when the underlying model hits its per-turn output
# token ceiling mid-generation. Anchored to this specific string (not a
# generic "did generation fail" check) so this detector never fires on an
# agent's own reasoning text that happens to discuss token limits.
_MAX_TOKEN_LIMIT_RE = re.compile(
    r"Error: Model stopped because it reached the maximum output token limit",
    re.IGNORECASE,
)

# Claude session limit detection -- "You've hit your session limit" or similar
# messages that indicate the CLI agent can't actually do work. Anchored to
# that confirmed exact phrase (not the bare fragment "You've hit", which is
# generic enough to risk matching an agent's own reasoning text or an echoed
# task prompt) -- same reasoning as AgentManager._send_initial_prompt_with_retry's
# equivalent check.
_SESSION_LIMIT_RE = re.compile(
    r"(?:you've hit your session limit|session limit|rate limit|too many requests)",
    re.IGNORECASE,
)

# Claude Code's exact message when the account/org hits its configured
# monthly spend cap -- the agent cannot make any more API calls until a
# human raises the limit or the billing period resets. Same failure class
# as a session limit (hard blocker, not recoverable by retrying), so it
# gets identical handling below: fail the task, terminate the agent, and
# pause the workflow only if the phase has no fallback_cli_tool -- a
# configured fallback should get a chance to run instead of sitting paused.
#
# Claude sometimes shows the message as text, other times as an interactive
# menu: "What do you want to do? 1. Stop and wait for limit to reset 2."
# Both patterns are matched here.
_SPEND_LIMIT_RE = re.compile(
    r"(?:you've hit your (?:monthly|weekly) (?:spend )?limit|stop and wait for limit to reset)",
    re.IGNORECASE,
)

# pi's status-line MCP indicator, e.g. "MCP: 0/1 servers". The denominator
# group excludes "0/0" (no servers configured at all -- not a failure) by
# requiring at least one digit that isn't a leading zero. Only observable
# via AgentManager.get_agent_output -- get_agent_output returns raw output
# as TUI chrome for every other caller.
_MCP_DISCONNECTED_RE = re.compile(r"MCP:\s*0/[1-9]\d*\s*servers", re.IGNORECASE)

# LLM connection errors that indicate the agent can't reach the API
_CONNECTION_ERROR_RE = re.compile(r"(?:Error:\s*(?:Connection error|Request timed out)|Retry failed after \d+ attempts:\s*Connection error)", re.IGNORECASE)

# OpenRouter's exact 402 error phrasing when a key's credits/weekly limit
# can't cover the requested max_tokens. Anchored to this specific phrase
# (not a generic "credit"/"402" keyword match) to avoid false positives on
# an agent's own reasoning text that happens to discuss billing or HTTP
# codes -- same care check_api_credits already takes for the same reason.
_CREDIT_EXHAUSTED_RE = re.compile(
    r"requires more credits, or fewer max_tokens", re.IGNORECASE
)

# Claude Code's exact rejection when launched with a --model string it
# doesn't recognize (e.g. a stale OpenRouter path baked into a Phase row
# from before default_cli_tool/cli_model changed). This is a hard stop --
# no amount of "just try again" recovers it, and unlike the MCP-disconnect
# case, the agent CANNOT self-remediate: /model is a client-side slash
# command Claude Code's input loop intercepts before it ever reaches the
# model, so no tool call or generated response can invoke it -- only
# literal keystrokes typed into the pane (which is exactly what
# send_message_to_agent does) can.
_BAD_MODEL_ERROR_RE = re.compile(
    r"issue with the selected model", re.IGNORECASE
)


def _strip_sgr(text: str) -> str:
    """Strip SGR color escape codes (\\x1b[...m).

    AgentManager._read_transcript_log deliberately KEEPS these when it
    strips other ANSI, since other callers display output to a human and
    want color preserved. Any detector here that compares tmux output
    content across polls (frozen-signature check, repetition-loop line
    counting) must strip them first -- a TUI that re-emits color codes on
    every redraw otherwise makes two reads of identical visible content
    differ byte-for-byte, silently defeating the comparison every time.
    """
    return _SGR_RE.sub("", text)


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


# Hard timeout for analyze_agent_state's LLM call -- see the call site's own
# comment for why this must never be unbounded (mirrors Guardian's
# GUARDIAN_LLM_TIMEOUT in guardian.py).
AGENT_STATE_LLM_TIMEOUT = 90

# How many idle-nudges a stuck task gets before "the agent produced output"
# stops being trusted as "the agent made progress" -- see the stuck-task
# nudge cap in _audit_system_health's own comment for the failure mode this
# closes (an agent that keeps replying without ever calling
# complete_my_task resets the idle check forever on activity alone).
MAX_STUCK_TASK_NUDGES = 3


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

            # Hard timeout so a slow/over-streaming model can never freeze this
            # shared monitoring loop task -- same reasoning and value as
            # Guardian's GUARDIAN_LLM_TIMEOUT (guardian.py) and Conductor's
            # CONDUCTOR_LLM_TIMEOUT (langchain_llm_client.py): an unbounded await
            # here previously froze the entire monitoring cycle (and therefore
            # every agent's auto-recovery, not just this one) for as long as the
            # model stayed silent.
            analysis = await asyncio.wait_for(
                self.llm_provider.analyze_agent_state(
                    agent_output=context["tmux_output"],
                    task_info={
                        "description": context["task_description"],
                        "done_definition": context["done_definition"],
                        "time_elapsed": context["time_elapsed"],
                    },
                    project_context=context["project_context"],
                ),
                timeout=AGENT_STATE_LLM_TIMEOUT,
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
            time_elapsed = (
                int((datetime.utcnow() - task.started_at).total_seconds() / 60)
                if task.started_at
                else 0
            )

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
        # Check if task is already done — don't send messages to completed agents
        if agent.current_task_id:
            with self.db_manager.session_scope() as session:
                from src.core.database import Task
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == "done":
                    logger.info(
                        f"[MONITOR] Skipping message to agent {agent.id[:8]} — "
                        f"task {task.id[:8]} is already done"
                    )
                    return

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

            # Same restart-loop protection as AgentManager.restart_agent.
            # This path creates a brand-new Agent row via create_agent_for_task
            # rather than incrementing restart_count on the existing one, so
            # without this check it has no bound at all: a decision-maker
            # that keeps returning RECREATE for the same stuck task could
            # spin up unlimited new agents.
            if (agent.restart_count or 0) >= 3:
                logger.warning(
                    f"Agent {agent.id[:8]} exceeded max restarts "
                    f"({agent.restart_count}), failing task instead of recreating"
                )
                task.status = "failed"
                task.failure_reason = f"Agent exceeded max restarts ({agent.restart_count})"
                session.commit()
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
            phase_cli_tool = phase_cli_model = phase_glm_token_env = (
                phase_thinking_level
            ) = None
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

            # Carry the restart count forward onto the new agent row -- it's
            # a fresh Agent id, so without this the max-restarts check above
            # would never see accumulated attempts across recreations.
            db_new_agent = session.query(Agent).filter_by(id=new_agent.id).first()
            if db_new_agent:
                db_new_agent.restart_count = (agent.restart_count or 0) + 1
                session.commit()

            logger.info(f"Created new agent {new_agent.id} to replace {agent.id}")

        except Exception as e:
            logger.error(f"Failed to recreate agent: {e}")
            session.rollback()
        finally:
            session.close()

    async def _log_intervention(
        self, agent: Agent, intervention_type: str, details: str
    ):
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

        # Tracks task_id -> (how many nudges sent, when we last nudged) an
        # idle-but-still-"working" agent, for the stuck-task check in
        # _audit_system_health. Reset on restart like the other in-process
        # monitoring state on this class (e.g. _last_phase_states
        # equivalents elsewhere) -- acceptable since a restart already
        # re-derives everything from DB state.
        self._stuck_task_nudges: Dict[str, Tuple[int, datetime]] = {}

        # Orphaned tmux session reconciliation collaborator (SOLID review
        # 3.4) — _cleanup_orphaned_tmux_sessions below delegates to this.
        from src.monitoring.orphan_reaper import OrphanSessionReaper

        self._orphan_reaper = OrphanSessionReaper(db_manager, agent_manager)

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

    async def _mechanical_recovery_for_agent(self, agent) -> bool:
        """Cheap, no-LLM stuck detection + keystroke recovery (the CLI/keystroke-level
        monitor). If an agent's substantive TUI output is frozen for frozen_seconds
        (a pi/mimo thought-loop that never exits), send the CLI's recovery keystrokes
        (Esc, polymorphic via CLIAgentInterface) + a short nudge. Bounded by max_recov;
        beyond that the Guardian / restart path takes over.

        Returns True if a real intervention (nudge or termination) happened
        this call, so the caller can skip Guardian analysis for this agent
        this same cycle -- see _monitoring_cycle.
        """
        frozen_seconds = 300  # >a normal turn; a real loop stays frozen indefinitely
        max_recov = 2
        try:
            if not hasattr(self, "_stuck_state"):
                self._stuck_state = {}
            out = self.agent_manager.get_agent_output(agent.id, lines=40)
            if not out:
                return

            # Spend/session limit check: read directly from tmux pane
            # because the interactive menu ("Stop and wait for limit to
            # reset") only appears in the live pane, not in the transcript
            # log that get_agent_output reads from.
            try:
                session = self.db_manager.get_session()
                try:
                    _agent = session.query(Agent).filter_by(id=agent.id).first()
                    if _agent and _agent.tmux_session_name:
                        _sess = next(
                            (s for s in self.agent_manager.tmux_server.sessions
                             if s.name == _agent.tmux_session_name), None
                        )
                        if _sess:
                            raw = _sess.attached_window.attached_pane.cmd(
                                "capture-pane", "-p", "-S", "-40"
                            ).stdout
                            raw_text = "\n".join(raw) if raw else ""
                            stripped_raw = _strip_sgr(raw_text)
                            spend_limit_hit = _SPEND_LIMIT_RE.search(stripped_raw)
                            if spend_limit_hit or _SESSION_LIMIT_RE.search(stripped_raw):
                                limit_kind = "monthly spend limit" if spend_limit_hit else "session limit"
                                logger.warning(
                                    f"[SESSION-LIMIT] Agent {agent.id[:8]} ({agent.cli_type}) hit {limit_kind} — "
                                    f"terminating immediately (not recoverable)"
                                )
                                with self.db_manager.session_scope() as session:
                                    from src.core.database import Phase as _Phase

                                    stuck_task = (
                                        session.query(Task)
                                        .filter_by(assigned_agent_id=agent.id)
                                        .filter(Task.status.in_(["assigned", "in_progress"]))
                                        .first()
                                    )
                                    if stuck_task:
                                        stuck_task.status = "failed"
                                        stuck_task.failure_reason = f"CLI {limit_kind} reached"
                                        logger.info(
                                            f"[SESSION-LIMIT] Task {stuck_task.id[:8]} marked failed; "
                                            f"phase will be retried"
                                        )

                                        fallback_tool = None
                                        fallback_model = None
                                        if stuck_task.phase_id:
                                            phase = (
                                                session.query(_Phase)
                                                .filter_by(id=stuck_task.phase_id)
                                                .first()
                                            )
                                            if phase:
                                                fallback_tool = getattr(phase, "fallback_cli_tool", None)
                                                fallback_model = getattr(phase, "fallback_cli_model", None)

                                        # Fall back to global config defaults
                                        if not fallback_tool:
                                            cfg = get_config()
                                            logger.warning(
                                                f"[SESSION-LIMIT] Phase fallback: {getattr(phase, 'fallback_cli_tool', None) if phase else 'no phase'}, "
                                                f"Global fallback: {cfg.default_fallback_cli_tool}, Agent type: {agent.cli_type}"
                                            )
                                            if cfg.default_fallback_cli_tool and cfg.default_fallback_cli_tool != agent.cli_type:
                                                fallback_tool = cfg.default_fallback_cli_tool
                                                fallback_model = cfg.default_fallback_cli_model

                                        if fallback_tool and fallback_tool != agent.cli_type:
                                            logger.warning(
                                                f"[SESSION-LIMIT] Re-dispatching with fallback: "
                                                f"{fallback_tool}/{fallback_model or 'default'}"
                                            )
                                            session.commit()
                                            await self.agent_manager.terminate_agent(agent.id)
                                            self._stuck_state.pop(agent.id, None)

                                            try:
                                                stuck_task.status = "pending"
                                                stuck_task.assigned_agent_id = None
                                                stuck_task.failure_reason = None
                                                session.commit()

                                                new_agent = await self.agent_manager.create_agent_for_task(
                                                    task=stuck_task,
                                                    enriched_data={},
                                                    memories=[],
                                                    project_context="",
                                                    cli_type=fallback_tool,
                                                    phase_cli_tool=fallback_tool,
                                                    phase_cli_model=fallback_model,
                                                )
                                                logger.info(
                                                    f"[SESSION-LIMIT] Fallback agent {new_agent.id[:8]} "
                                                    f"created for task {stuck_task.id[:8]}"
                                                )
                                            except Exception as fallback_err:
                                                logger.error(
                                                    f"[SESSION-LIMIT] Fallback agent creation failed: "
                                                    f"{fallback_err}"
                                                )
                                                stuck_task.status = "failed"
                                                stuck_task.failure_reason = (
                                                    f"Primary hit {limit_kind}, fallback also failed: "
                                                    f"{fallback_err}"
                                                )
                                                session.commit()
                                            return True
                                        elif stuck_task.workflow_id:
                                            workflow = (
                                                session.query(Workflow)
                                                .filter_by(id=stuck_task.workflow_id)
                                                .first()
                                            )
                                            if workflow and workflow.status != "paused":
                                                workflow.status = "paused"
                                                workflow.paused_by = "system"
                                                workflow.status_reason = (
                                                    f"CLI {limit_kind} hit ({agent.cli_type}), no "
                                                    "fallback configured -- will auto-resume on its "
                                                    "own retry cooldown once the limit resets"
                                                )
                                                workflow.paused_at = datetime.utcnow()
                                                logger.warning(
                                                    f"[SESSION-LIMIT] Pausing workflow "
                                                    f"{stuck_task.workflow_id[:8]} -- no fallback "
                                                    "configured for this phase"
                                                )
                                await self.agent_manager.terminate_agent(agent.id)
                                self._stuck_state.pop(agent.id, None)
                                return True
                finally:
                    session.close()
            except Exception as _pane_err:
                logger.debug(f"Pane capture for spend-limit check failed: {_pane_err}")
            # Strip SGR color codes here, for the signature only -- other
            # consumers of get_agent_output still get color preserved. See
            # _strip_sgr's docstring: a TUI that re-emits color codes on
            # every redraw otherwise makes two reads of an identical frozen
            # screen differ byte-for-byte, silently disabling this whole
            # detector -- observed live: an agent hard-stopped on a model
            # error sat frozen for 12+ minutes with zero [MECH-RECOVERY] log
            # lines, because its frozen screen still had colored text.
            out_no_color = _strip_sgr(out)
            # Drop volatile lines (status bar %/tokens/$/MCP/time, spinner glyphs) so a
            # live spinner or ticking cost doesn't masquerade as real progress.
            sig = "\n".join(
                ln
                for ln in out_no_color.splitlines()
                if not re.search(r"%/[\d.]+M|\$[\d.]+|MCP:|Took |[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣿]", ln)
            ).strip()
            now = time.time()
            st = self._stuck_state.setdefault(
                agent.id, {"sig": None, "since": None, "recov": 0}
            )
            if sig and sig == st["sig"]:
                if st["since"] is None:
                    st["since"] = now
            else:
                # Output changed → real progress; reset everything.
                st["sig"] = sig
                st["since"] = None
                st["recov"] = 0
                # Also refresh Agent.last_activity -- the separate "task
                # stuck" check in _audit_system_health relies entirely on
                # this field, which is otherwise ONLY touched by an MCP
                # tool call (_touch_agent_activity, server.py) or a
                # successful Guardian analysis cycle. A read-heavy phase
                # (e.g. feature_review reading design.md + several scope.md
                # files before writing anything) can go 5+ minutes without
                # either of those firing while genuinely, visibly working --
                # the stuck-task check would then kill it on its hard
                # stuck_detection_minutes timer despite real progress being
                # right here in the tmux output. Observed live: the same
                # feature_review task died to "no agent activity for >5
                # minutes" on three consecutive retries, each time visibly
                # active (spinner, new tool calls) when checked manually.
                try:
                    with self.db_manager.session_scope() as _session:
                        from src.core.database import Agent as _Agent

                        _db_agent = (
                            _session.query(_Agent).filter_by(id=agent.id).first()
                        )
                        if _db_agent:
                            _db_agent.last_activity = datetime.utcnow()
                except Exception:
                    pass  # best-effort; the mechanical-recovery check itself must not fail
                return
            frozen_for = now - st["since"] if st["since"] else 0

            # Session limit: hard blocker — can't recover, fail immediately.
            # This fires on an already-running agent mid-session (unlike
            # AgentManager.create_agent_for_task's equivalent check, which
            # only sees a session-limit rejection during initial prompt
            # delivery) -- e.g. an agent that did 10+ minutes of real work
            # before running out of session budget. If the phase has no
            # Fast-path: "Operation aborted" leaves the agent idle at the shell
            # prompt.  The output signature changed (so the 5-min clock reset),
            # but the agent won't self-rescue — 30 s is enough to be sure.
            abort_frozen = "Operation aborted" in sig and frozen_for >= 30
            if (abort_frozen or frozen_for >= frozen_seconds) and st[
                "recov"
            ] < max_recov:
                st["recov"] += 1
                st["since"] = now  # restart the window after an attempt
                logger.warning(
                    f"[MECH-RECOVERY] Agent {agent.id[:8]} ({agent.cli_type}) output frozen "
                    f"{int(frozen_for)}s — recovery attempt {st['recov']}/{max_recov} (keys + nudge)"
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

                        _sp.run(
                            ["tmux", "send-keys", "-t", session_name, "Escape", ""],
                            check=False,
                        )
                        await asyncio.sleep(0.5)
                        _sp.run(
                            ["tmux", "send-keys", "-t", session_name, "/mcp", "Enter"],
                            check=False,
                        )
                        await asyncio.sleep(2.0)
                        _sp.run(
                            ["tmux", "send-keys", "-t", session_name, "C-r", ""],
                            check=False,
                        )
                        await asyncio.sleep(3.0)
                        _sp.run(
                            ["tmux", "send-keys", "-t", session_name, "Escape", ""],
                            check=False,
                        )
                        await asyncio.sleep(0.5)
                if await self.agent_manager.send_recovery_keystrokes(agent.id):
                    mcp_note = (
                        " MCP was disconnected and has been reconnected."
                        if mcp_disconnected
                        else ""
                    )
                    if "Operation aborted" in sig:
                        msg = (
                            "Your last tool call was aborted. Review what you have already "
                            "completed in this session. If the work is done, call "
                            "update_task_status with status='done'. If you are genuinely "
                            f"blocked, call it with status='failed' and explain why.{mcp_note}"
                        )
                    elif _MAX_TOKEN_LIMIT_RE.search(_strip_sgr(sig)):
                        msg = (
                            "You hit the model's output token limit. Do NOT redo work that "
                            "already succeeded — check what was actually written before "
                            "continuing. Break remaining work into smaller chunks: one file "
                            "read or one write per turn. If the task is done, call "
                            "update_task_status with status='done'."
                            f"{mcp_note}"
                        )
                    else:
                        msg = (
                            "You appear stuck or looping. Stop, state your single next concrete "
                            f"action in one line, then do it. If blocked, save a memory and call "
                            f"update_task_status.{mcp_note}"
                        )
                    await self.agent_manager.send_message_to_agent(agent.id, msg)
                    # Re-baseline st["sig"] to the pane AFTER our own nudge
                    # lands, not before. The nudge text gets echoed into the
                    # pane (most CLIs show sent messages in the transcript),
                    # so the very next poll's sig almost always differs from
                    # the pre-nudge baseline captured above -- purely from
                    # our own message, not the agent doing anything. Left
                    # unbaselined, that "changed" (line ~825) reads as real
                    # progress and resets st["recov"] to 0 every single
                    # cycle, so max_recov is never actually reached: the
                    # agent sits frozen at the same "Operation aborted"
                    # prompt while the nudge fires over and over, never
                    # escalating to fail+terminate. Observed live: 5+
                    # consecutive "Operation aborted" nudges for the same
                    # agent well past max_recov=2. Best-effort -- if the
                    # re-capture fails, fall through with the stale
                    # baseline (matches this function's pre-existing
                    # behavior before this fix).
                    try:
                        post_nudge_out = self.agent_manager.get_agent_output(agent.id, lines=40)
                        if post_nudge_out:
                            post_nudge_no_color = _strip_sgr(post_nudge_out)
                            st["sig"] = "\n".join(
                                ln
                                for ln in post_nudge_no_color.splitlines()
                                if not re.search(r"%/[\d.]+M|\$[\d.]+|MCP:|Took |[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣿]", ln)
                            ).strip()
                    except Exception:
                        pass
                    return True
            elif (abort_frozen or frozen_for >= frozen_seconds) and st["recov"] >= max_recov:
                # All recovery attempts exhausted and agent is still frozen.
                # Mirrors the nudge-trigger condition above (abort_frozen or
                # frozen_for >= frozen_seconds), not just frozen_seconds --
                # without this, an "Operation aborted" agent that exhausts
                # max_recov via the fast 30s abort_frozen path then has to
                # sit frozen for the FULL frozen_seconds (300s, timed from
                # the last nudge's since=now reset) before this branch would
                # ever fire, since neither branch's condition was true in
                # between: recov >= max_recov blocks the nudge branch, and
                # frozen_for was only ~30s, not yet 300s. Fail the task so
                # the monitor's retry-bound path handles it
                # (MAX_PHASE_ATTEMPTS → impasse if exceeded). §9.4 / §11.2 fix #2.
                logger.warning(
                    f"[MECH-RECOVERY] Agent {agent.id[:8]} frozen {int(frozen_for)}s after "
                    f"{max_recov} recovery attempts — abandoning: fail task, terminate agent"
                )
                with self.db_manager.session_scope() as session:
                    from src.core.database import Task as _Task

                    stuck_task = (
                        session.query(_Task)
                        .filter_by(assigned_agent_id=agent.id)
                        .filter(_Task.status.in_(["assigned", "in_progress"]))
                        .first()
                    )
                    if stuck_task:
                        stuck_task.status = "failed"
                        stuck_task.failure_reason = (
                            f"Agent output frozen {int(frozen_for)}s; "
                            f"{max_recov} recovery attempts exhausted"
                        )
                        logger.info(
                            f"[MECH-RECOVERY] Task {stuck_task.id[:8]} marked failed; "
                            f"phase will be retried (MAX_PHASE_ATTEMPTS bound)"
                        )
                await self.agent_manager.terminate_agent(agent.id)
                self._stuck_state.pop(agent.id, None)
                return True
        except Exception as e:
            logger.warning(f"[MECH-RECOVERY] check failed for {agent.id[:8]}: {e}")
        return False

    async def _detect_repetition_loop(self, agent) -> bool:
        """Detect and interrupt an LLM thought-loop where the same sentence repeats
        many times in recent output (output IS growing, just cycling the same text).

        Unlike the frozen-output check in _mechanical_recovery_for_agent, this fires
        when the model keeps adding the same paragraph over and over — a semantic loop
        that the frozen-sig check misses because the content hash changes each tick.

        Trigger: any normalised line of ≥ 30 chars appears ≥ 5 times in the last 80
        lines of output. One recovery attempt is made (keys + targeted nudge); if the
        loop resumes it will be caught again on the next cycle.
        """
        min_line_len = 30
        window_lines = 120
        repeat_threshold = 12
        try:
            if not hasattr(self, "_rep_loop_state"):
                self._rep_loop_state = {}
            out = self.agent_manager.get_agent_output(agent.id, lines=window_lines)
            if not out:
                return
            # Strip SGR color codes before comparing lines -- same gap as
            # _mechanical_recovery_for_agent's frozen-signature check (see
            # _strip_sgr's docstring): a repeated line wrapped in varying
            # color codes on each redraw would otherwise count as a distinct
            # line every time, never reaching repeat_threshold.
            out = _strip_sgr(out)
            # Normalise: strip leading whitespace, drop blank/trivial lines.
            # Also exclude bare filesystem paths and shell prompts — these repeat
            # legitimately in ls output, shell prompts, and long file writes.
            import re as _re
            _fs_path = _re.compile(r"^[/~][\w./\-]+$")
            lines = [
                ln.strip() for ln in out.splitlines()
                if len(ln.strip()) >= min_line_len
                and not _fs_path.match(ln.strip())
            ]
            if not lines:
                return
            # Count occurrences of each normalised line.
            from collections import Counter

            counts = Counter(lines)
            top_line, top_count = counts.most_common(1)[0]
            if top_count < repeat_threshold:
                self._rep_loop_state.pop(agent.id, None)
                return
            # Diversity guard: if the window contains many distinct lines, the
            # repeated phrase is likely markdown in a file write, not a reasoning
            # loop. Only fire if the top phrase accounts for >30% of all lines.
            if top_count / len(lines) < 0.30:
                self._rep_loop_state.pop(agent.id, None)
                return
            # Guard: only fire once per unique repeated phrase to avoid spam.
            last_phrase = self._rep_loop_state.get(agent.id)
            if last_phrase == top_line:
                return
            self._rep_loop_state[agent.id] = top_line
            logger.warning(
                f"[REP-LOOP] Agent {agent.id[:8]} ({agent.cli_type}): "
                f"line repeated {top_count}× in last {window_lines} lines — "
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
            return True
        except Exception as e:
            logger.warning(f"[REP-LOOP] check failed for {agent.id[:8]}: {e}")
        return False

    async def _detect_dangerous_command_confirmation(self, agent) -> bool:
        """Detect a pending 'Dangerous command' confirmation for an rm
        command and auto-deny it (Escape) + nudge the agent, instead of
        relying on the generic frozen-output detector.

        A static Yes/No confirmation screen is a DIFFERENT failure mode than
        a frozen "Thinking..." loop: it isn't reliably caught by
        _mechanical_recovery_for_agent's signature comparison (observed
        live: an agent sat on an unanswered rm -rf confirmation for 9+
        minutes, well past frozen_seconds, with zero [MECH-RECOVERY] log
        lines -- something elsewhere in that 40-line window kept the
        captured signature changing between polls). This check is narrowly
        scoped to rm specifically, matches immediately (no frozen-timer
        wait), and always denies -- the system prompt already tells every
        agent to NEVER run rm/destructive commands in the first place, so
        there is no case where approving is the right call here.
        """
        try:
            if not hasattr(self, "_denied_dangerous_cmds"):
                self._denied_dangerous_cmds = {}
            out = self.agent_manager.get_agent_output(agent.id, lines=40)
            if not out:
                return
            match = _DANGEROUS_CMD_RE.search(_strip_sgr(out))
            if not match:
                return
            command = match.group(1).strip()
            if not re.search(r"(^|[/\s;&|])rm\b", command):
                # Only auto-handle rm -- a different dangerous command (e.g.
                # curl | sh) still needs a human or Guardian's judgment call.
                return

            # Cooldown, not a permanent one-shot flag: if the first Escape
            # doesn't register (e.g. a second, later rm prompt appears for
            # the same agent), retry after a short window instead of
            # leaving it stuck forever because we already "handled" this
            # agent once.
            last_denied = self._denied_dangerous_cmds.get(agent.id)
            if last_denied is not None and time.time() - last_denied < 30:
                return
            self._denied_dangerous_cmds[agent.id] = time.time()

            logger.warning(
                f"[DANGEROUS-CMD] Agent {agent.id[:8]} ({agent.cli_type}) has a "
                f"pending rm confirmation — auto-denying and nudging: {command[:120]!r}"
            )
            from src.interfaces.cli_interface import get_cli_agent

            try:
                cli = get_cli_agent(agent.cli_type)
                keys = cli.recovery_keystrokes()  # Escape for pi -- cancels the prompt
            except Exception:
                keys = []
            if keys:
                await self.agent_manager.send_recovery_keystrokes(agent.id)
            await self.agent_manager.send_message_to_agent(
                agent.id,
                "Your rm command was denied — you must NEVER run `rm -rf` or any "
                "other destructive filesystem command; this is a hard rule from "
                "your system prompt, not a suggestion. If you need to replace or "
                "clean up a file/directory, overwrite it directly with your "
                "write/edit tools instead of deleting it first. Continue your "
                "task without using rm.",
            )
            return True
        except Exception as e:
            logger.warning(f"[DANGEROUS-CMD] check failed for {agent.id[:8]}: {e}")
        return False

    async def _detect_max_token_limit_error(self, agent) -> bool:
        """Detect pi's own "Error: Model stopped because it reached the
        maximum output token limit" message and immediately nudge with
        specific, actionable guidance instead of waiting for the generic
        frozen-output detector's 300s threshold (or Guardian's periodic
        analysis) to eventually notice and send a generic "you appear
        stuck" nudge.

        Unlike _detect_dangerous_command_confirmation, there is no dialog
        to dismiss here -- pi already returned control after the error, so
        this is nudge-only, no recovery keystrokes.

        On mimo-v2.5-pro's much larger output ceiling this should be rare
        (routine multi-file work fit comfortably under the old, smaller
        ceiling too, once chunked) -- hitting it at all now usually means a
        genuine runaway (a single write far too large, or a reasoning loop
        that doesn't visibly repeat text the way _detect_repetition_loop's
        pattern-matching would catch) rather than the systemic ceiling
        problem the model switch already solved.
        """
        try:
            if not hasattr(self, "_nudged_token_limit"):
                self._nudged_token_limit = {}
            out = self.agent_manager.get_agent_output(agent.id, lines=40)
            if not out:
                return
            if not _MAX_TOKEN_LIMIT_RE.search(_strip_sgr(out)):
                return

            # Cooldown, not a permanent one-shot flag -- same reasoning as
            # _detect_dangerous_command_confirmation: if this keeps
            # happening for the same agent, keep nudging rather than going
            # silent after the first attempt.
            last_nudged = self._nudged_token_limit.get(agent.id)
            if last_nudged is not None and time.time() - last_nudged < 30:
                return
            self._nudged_token_limit[agent.id] = time.time()

            logger.warning(
                f"[MAX-TOKEN-LIMIT] Agent {agent.id[:8]} ({agent.cli_type}) hit "
                "the model's output token limit — nudging with chunking guidance"
            )
            await self.agent_manager.send_message_to_agent(
                agent.id,
                "You just hit the model's output token limit — whatever you were "
                "doing (reading, reasoning, or writing) was too large for one "
                "turn. Break it into smaller pieces: one file read or write per "
                "turn, not several chained together, and don't try to redo the "
                "whole thing at once. Check what actually got written before "
                "continuing — a write that hit this limit may be truncated.",
            )
            return True
        except Exception as e:
            logger.warning(f"[MAX-TOKEN-LIMIT] check failed for {agent.id[:8]}: {e}")
        return False

    async def _detect_mcp_disconnected(self, agent) -> bool:
        """Detect a dropped MCP server connection (pi's "MCP: 0/N servers"
        status line) and nudge the agent to reconnect, instead of leaving
        it to notice on its own or requiring a full agent restart.

        Observed live: an agent's MCP connection to the hephaestus server
        dropped mid-session (likely from the backend being briefly
        unreachable during a `heph restart`) and stayed down across many
        work cycles -- not a transient blip pi retries on its own. The
        agent kept making real progress (file writes don't need MCP) but
        would never have been able to call complete_my_task/create_task/
        save_memory, silently stranding an otherwise-finished task.
        Confirmed live that pi exposes `mcp status` and `mcp connect
        <server>` as tools the agent itself can invoke to reconnect without
        losing session state -- no restart needed.

        Uses get_agent_output, which returns raw output without stripping.
        strips the "MCP: N/M servers" line as TUI chrome for every other
        caller (both via _read_transcript_log's mcp_status_re filter and
        strip_tui_chrome on its capture-pane fallback), so this detector
        would never see it through the normal path.

        The trigger regex only matches pi's own status-line text -- it will
        never fire for another CLI's differently-shaped output. The nudge
        text is still fetched polymorphically via
        CLIAgentInterface.mcp_reconnect_instructions rather than hardcoded,
        so this stays harness-agnostic like recovery_keystrokes: a CLI with
        no known reconnect mechanism gets no nudge instead of pi-specific
        syntax that would confuse it.
        """
        try:
            if not hasattr(self, "_nudged_mcp_disconnected"):
                self._nudged_mcp_disconnected = {}
            out = self.agent_manager.get_agent_output(agent.id, lines=50)
            if not out:
                return
            if not _MCP_DISCONNECTED_RE.search(_strip_sgr(out)):
                # MCP reconnected — reset nudge count
                if hasattr(self, "_mcp_disconnect_nudge_count"):
                    self._mcp_disconnect_nudge_count.pop(agent.id, None)
                return

            from src.interfaces.cli_interface import get_cli_agent

            try:
                instructions = get_cli_agent(agent.cli_type).mcp_reconnect_instructions(
                    "hephaestus"
                )
            except Exception:
                instructions = ""
            if not instructions:
                logger.debug(
                    f"[MCP-DISCONNECTED] Agent {agent.id[:8]} ({agent.cli_type}) has "
                    "0 connected MCP servers, but no known reconnect mechanism for "
                    "this CLI — not nudging"
                )
                return

            last_nudged = self._nudged_mcp_disconnected.get(agent.id)
            if last_nudged is not None and time.time() - last_nudged < 45:
                return
            self._nudged_mcp_disconnected[agent.id] = time.time()

            # Track nudge count — after 3 failed nudges, terminate the agent
            # so the pipeline can retry with a fresh session.
            if not hasattr(self, "_mcp_disconnect_nudge_count"):
                self._mcp_disconnect_nudge_count = {}
            count = self._mcp_disconnect_nudge_count.get(agent.id, 0) + 1
            self._mcp_disconnect_nudge_count[agent.id] = count
            logger.debug(f"[MCP-DISCONNECTED] Agent {agent.id[:8]} nudge count: {count}")

            if count > 3:
                logger.warning(
                    f"[MCP-DISCONNECTED] Agent {agent.id[:8]} ({agent.cli_type}) still disconnected "
                    f"after {count} nudges — terminating so pipeline can retry"
                )
                # Reset count for this agent
                self._mcp_disconnect_nudge_count.pop(agent.id, None)
                self._nudged_mcp_disconnected.pop(agent.id, None)
                await self.agent_manager.terminate_agent(agent.id)
                # Reset assigned tasks to pending
                with self.db_manager.session_scope() as session:
                    from src.core.database import Task as _Task
                    stuck_tasks = (
                        session.query(_Task)
                        .filter_by(assigned_agent_id=agent.id)
                        .filter(_Task.status.in_(["assigned", "in_progress"]))
                        .all()
                    )
                    for t in stuck_tasks:
                        t.status = "pending"
                        t.assigned_agent_id = None
                        logger.info(
                            f"[MCP-DISCONNECTED] Task {t.id[:8]} reset to pending"
                        )
                return True

            logger.warning(
                f"[MCP-DISCONNECTED] Agent {agent.id[:8]} ({agent.cli_type}) has "
                "0 connected MCP servers — nudging to reconnect"
            )
            # Send Escape first to break any spinner/loop, then the message
            await self.agent_manager.send_recovery_keystrokes(agent.id)
            await asyncio.sleep(0.5)
            await self.agent_manager.send_message_to_agent(
                agent.id,
                "Your MCP connection to the hephaestus server is down (0 "
                "connected servers) — this is a client-side connection issue, "
                f"not a backend problem. {instructions} Once reconnected, "
                "verify with `mcp status` that hephaestus is actually back, "
                "then check specifically: have you already called "
                f"complete_my_task for your CURRENT task_id ({agent.current_task_id or 'unknown -- call get_my_tasks first'}) "
                "in THIS session? If not, call it now with your real "
                "results — do not just say 'task already completed' and "
                "stop. A resumed session can make you recall finishing a "
                "DIFFERENT, earlier task; that does not count for this one.",
            )
            return True
        except Exception as e:
            logger.warning(f"[MCP-DISCONNECTED] check failed for {agent.id[:8]}: {e}")
        return False

    async def _detect_connection_errors(self, agent) -> bool:
        """Detect persistent LLM connection errors and terminate the agent.

        When the LLM API is unreachable (connection errors, timeouts), the
        agent retries a few times then sits stuck. Detect this pattern and
        terminate so the pipeline can retry with a fresh session.
        """
        try:
            out = self.agent_manager.get_agent_output(agent.id, lines=20)
            if not out:
                return False
            stripped = _strip_sgr(out)
            if not _CONNECTION_ERROR_RE.search(stripped):
                return False

            # Check if we've already warned about this agent recently
            if not hasattr(self, "_connection_error_warned"):
                self._connection_error_warned = {}
            last_warned = self._connection_error_warned.get(agent.id)
            if last_warned and time.time() - last_warned < 120:
                return False
            self._connection_error_warned[agent.id] = time.time()

            # Check if the error is persistent (more than 2 occurrences in the output)
            error_count = len(_CONNECTION_ERROR_RE.findall(stripped))
            if error_count < 2:
                logger.info(f"[CONNECTION-ERROR] Agent {agent.id[:8]} has {error_count} connection error(s) — waiting for recovery")
                return False

            logger.warning(
                f"[CONNECTION-ERROR] Agent {agent.id[:8]} ({agent.cli_type}) has "
                f"{error_count} persistent connection errors — terminating so pipeline can retry"
            )
            self._connection_error_warned.pop(agent.id, None)
            await self.agent_manager.terminate_agent(agent.id)

            # Reset assigned tasks to pending
            with self.db_manager.session_scope() as session:
                from src.core.database import Task as _Task
                stuck_tasks = (
                    session.query(_Task)
                    .filter_by(assigned_agent_id=agent.id)
                    .filter(_Task.status.in_(["assigned", "in_progress"]))
                    .all()
                )
                for t in stuck_tasks:
                    t.status = "pending"
                    t.assigned_agent_id = None
                    logger.info(f"[CONNECTION-ERROR] Task {t.id[:8]} reset to pending")
            return True
        except Exception as e:
            logger.warning(f"[CONNECTION-ERROR] check failed for {agent.id[:8]}: {e}")
        return False

    async def _detect_bad_model_error(self, agent) -> bool:
        """Detect Claude Code's "issue with the selected model" rejection
        and fix it directly by sending `/model <config default>` as literal
        pane input, instead of nudging the agent to do it -- the agent
        cannot: /model is a client-side slash command Claude Code's input
        loop intercepts before it reaches the model at all, so no reply the
        agent generates can invoke it, only real keystrokes typed into the
        pane (which is exactly what send_message_to_agent delivers here,
        unlike a normal nudge where the text is meant to be read and acted
        on BY the model).

        Only meaningful for cli_type == "claude" -- this is Claude Code's
        own slash-command syntax and error phrasing, not a cross-CLI
        concept like mcp_reconnect_instructions.

        Observed live: a Phase row's stale cli_model (baked in from before
        default_cli_tool/cli_model changed) got handed to a freshly-launched
        Claude agent, which rejected it outright and sat frozen -- unable to
        do anything, including the one thing that would have fixed it.
        """
        try:
            if agent.cli_type != "claude":
                return False
            if not hasattr(self, "_fixed_bad_model"):
                self._fixed_bad_model = set()
            if agent.id in self._fixed_bad_model:
                return False
            out = self.agent_manager.get_agent_output(agent.id, lines=40)
            if not out:
                return False
            if not _BAD_MODEL_ERROR_RE.search(_strip_sgr(out)):
                return False
            self._fixed_bad_model.add(agent.id)

            fix_model = getattr(self.config, "cli_model", None) or "sonnet"
            logger.warning(
                f"[BAD-MODEL] Agent {agent.id[:8]} (claude) rejected its "
                f"launch model — sending '/model {fix_model}' directly"
            )
            await self.agent_manager.send_message_to_agent(agent.id, f"/model {fix_model}")
            return True
        except Exception as e:
            logger.warning(f"[BAD-MODEL] check failed for {agent.id[:8]}: {e}")
        return False

    async def _detect_credit_exhausted(self, agent) -> bool:
        """Detect OpenRouter's 402 "requires more credits" error and pause
        the workflow immediately, instead of nudging/retrying a task that
        is guaranteed to fail again until a human reloads credits.

        Observed live: an agent hit this error and simply sat frozen --
        unlike a generic provider error, pi did not auto-continue past it.
        The generic frozen-output recovery (keystrokes + a "you seem
        stuck" nudge) would just retry the same doomed LLM call forever.
        This is external and human-actionable only (reload credits at
        OpenRouter), so the correct response is the same one
        _maybe_retry_failed_tasks already uses for exhausted retries:
        pause the workflow (status="paused", paused_by="system") and let
        _retry_exhausted_paused_workflows's existing cooldown-retry
        mechanism pick it back up automatically once credits are
        restored, rather than inventing a second resume path.

        One-shot per agent (a set, not a time cooldown) since the action
        here -- pause + terminate -- is terminal for this agent, not a
        repeatable nudge.
        """
        try:
            if not hasattr(self, "_paused_credit_exhausted"):
                self._paused_credit_exhausted = set()
            if agent.id in self._paused_credit_exhausted:
                return False
            out = self.agent_manager.get_agent_output(agent.id, lines=40)
            if not out:
                return False
            if not _CREDIT_EXHAUSTED_RE.search(_strip_sgr(out)):
                return False
            self._paused_credit_exhausted.add(agent.id)

            logger.warning(
                f"[CREDIT-EXHAUSTED] Agent {agent.id[:8]} ({agent.cli_type}) hit "
                "an OpenRouter 402 credit-exhaustion error — pausing workflow"
            )

            with self.db_manager.session_scope() as session:
                task = (
                    session.query(Task).filter_by(id=agent.current_task_id).first()
                )
                if not task:
                    return False
                task.status = "failed"
                task.failure_reason = (
                    "OpenRouter credit exhaustion (402: requires more credits)"
                )
                workflow = (
                    session.query(Workflow).filter_by(id=task.workflow_id).first()
                )
                if workflow and workflow.status != "paused":
                    workflow.status = "paused"
                    workflow.paused_by = "system"
                    workflow.status_reason = (
                        "OpenRouter credit exhaustion (402) — reload credits at "
                        "openrouter.ai, will auto-resume on its own retry cooldown"
                    )
                    workflow.paused_at = datetime.utcnow()

            await self.agent_manager.terminate_agent(agent.id)
            return True
        except Exception as e:
            logger.warning(f"[CREDIT-EXHAUSTED] check failed for {agent.id[:8]}: {e}")
        return False

    #: How long an agent may show zero activity since its prompt was
    #: delivered before _detect_agent_never_started gives up on it.
    #: Deliberately shorter than _mechanical_recovery_for_agent's
    #: frozen_seconds (300s): "never produced any output at all" is a
    #: stronger signal than "was producing output, then stopped", and a
    #: keystroke nudge can't help a request that never returned in the
    #: first place -- there's nothing to interrupt a reply out of.
    NEVER_STARTED_GRACE_SECONDS = 240

    async def _detect_agent_never_started(self, agent) -> bool:
        """Detect an agent whose initial prompt was delivered but that has
        produced zero substantive output since -- Agent.last_activity
        (only ever refreshed by a real output-signature change in
        _mechanical_recovery_for_agent, an MCP tool call, or a successful
        Guardian cycle) has stayed at its launch-time value the whole
        time.

        Unlike _mechanical_recovery_for_agent's frozen-output check, this
        reads persisted Agent.launched_at/last_activity from the DB
        instead of in-memory _stuck_state -- so it correctly identifies
        an agent that's been silent since launch even on the very FIRST
        monitoring cycle after a backend restart, when _stuck_state was
        just wiped and hasn't had a chance to accumulate 300s of
        observed frozen time yet. Observed live: a pi agent queued behind
        several other concurrently-launched agents on the same local
        model server sat at its initial "Begin now." banner with zero
        output for 10+ minutes, un-nudgeable by Enter (confirmed manually
        -- the process was blocked on the in-flight completion request,
        not waiting on stdin), while _mechanical_recovery_for_agent
        stayed silent because its own tracking had just been reset by an
        unrelated restart minutes earlier.

        Deliberately compares against launched_at, not created_at:
        restart_agent refreshes launched_at (and last_activity) on every
        restart but leaves created_at at the agent's original creation
        time, which predates every restart. Comparing last_activity to
        created_at would make (last_activity - created_at) always look
        large for a restarted agent regardless of whether it's had any
        real activity since THIS restart -- permanently disqualifying
        every restarted agent from ever being caught by this check, the
        exact scenario (a resumed "_r" session hanging again) this exists
        to catch.

        Terminates and resets the task to pending (same remedy as
        _detect_connection_errors) rather than nudging -- nothing has
        ever been received to nudge a reply out of.
        """
        try:
            if agent.status != "working" or not agent.current_task_id:
                return False
            if not agent.launched_at or not agent.last_activity:
                return False
            # last_activity is stamped at launch-command-send time (see
            # create_agent_for_task/restart_agent) and only moves forward
            # from there on real activity -- if it's still within a few
            # seconds of launched_at, nothing has happened since launch.
            if (agent.last_activity - agent.launched_at).total_seconds() > 5:
                return False
            elapsed = (datetime.utcnow() - agent.last_activity).total_seconds()
            if elapsed < self.NEVER_STARTED_GRACE_SECONDS:
                return False

            if not hasattr(self, "_never_started_handled"):
                self._never_started_handled = set()
            if agent.id in self._never_started_handled:
                return False
            self._never_started_handled.add(agent.id)

            task_id = agent.current_task_id
            logger.warning(
                f"[NEVER-STARTED] Agent {agent.id[:8]} ({agent.cli_type}) produced "
                f"no output {int(elapsed)}s after launch — terminating so pipeline can retry"
            )
            await self.agent_manager.terminate_agent(agent.id)

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
                        f"[NEVER-STARTED] Task {stuck_task.id[:8]} reset to pending for retry"
                    )
            return True
        except Exception as e:
            logger.warning(f"[NEVER-STARTED] check failed for {agent.id[:8]}: {e}")
        return False

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

        # Phase 0: cheap mechanical recovery (no LLM). Eight complementary checks:
        #   a) OpenRouter credits exhausted — pause workflow + terminate
        #      immediately, before any other check wastes a recovery attempt
        #      on an agent that's about to be torn down anyway
        #   b) never started — zero output since launch, ≥4 min — terminate,
        #      reset to pending; uses persisted Agent timestamps so it works
        #      correctly even right after a restart, unlike (c) below
        #   c) frozen output — same substantive 40-line sig for ≥5 min
        #   d) repetition loop — output growing but same sentence repeats 5+ times
        #      in the last 80 lines (LLM cycling "Actually, let me try…")
        #   e) pending rm confirmation — auto-deny immediately, don't wait for (c)
        #   f) max output token limit hit — nudge immediately, don't wait for (c)
        #   g) MCP server disconnected — nudge to `mcp connect`, don't wait for (c)
        #   h) Claude Code rejected its launch model — fix directly with a
        #      real `/model <x>` keystroke send, since the agent can't
        #      invoke that slash command itself
        mechanically_intervened = set()
        for agent in agents:
            if await self._detect_credit_exhausted(agent):
                mechanically_intervened.add(agent.id)
                continue
            if await self._detect_agent_never_started(agent):
                mechanically_intervened.add(agent.id)
                continue
            if await self._mechanical_recovery_for_agent(agent):
                mechanically_intervened.add(agent.id)
            if await self._detect_repetition_loop(agent):
                mechanically_intervened.add(agent.id)
            if await self._detect_dangerous_command_confirmation(agent):
                mechanically_intervened.add(agent.id)
            if await self._detect_max_token_limit_error(agent):
                mechanically_intervened.add(agent.id)
            if await self._detect_mcp_disconnected(agent):
                mechanically_intervened.add(agent.id)
            if await self._detect_connection_errors(agent):
                mechanically_intervened.add(agent.id)
            if await self._detect_bad_model_error(agent):
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

        # Check if tracked workflow is still the most recent active one.
        # When the pipeline restarts with a new design, it launches a new workflow.
        # The monitor should switch to track the new workflow instead of the old one.
        if self.phase_manager and self.phase_manager.workflow_id:
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
                logger.debug(f"[WORKFLOW-SWITCH] Check failed: {e}")

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

        # DEBUG: Check database for active workflows
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

    async def _guardian_analysis_for_agent(
        self, agent: Agent
    ) -> Optional[Dict[str, Any]]:
        """Perform Guardian analysis for a single agent.

        Args:
            agent: Agent to analyze

        Returns:
            Guardian analysis result or None if failed
        """
        from src.core.log_context import set_log_context
        set_log_context(agent=agent.id, task=agent.current_task_id or "")
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
            if agent.agent_type == "orchestrator":
                logger.debug(
                    f"Skipping orchestrator agent {agent.id[:8]} (runs in-process)"
                )
                return None

            # Special handling for agents with missing tmux sessions
            if (
                agent.tmux_session_name
                and not self.agent_manager.tmux_server.has_session(
                    agent.tmux_session_name
                )
            ):
                # Check if task is already done before restarting
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == "done":
                    logger.info(
                        f"Agent {agent.id} has missing tmux session but task {task.id[:8]} is done — not restarting"
                    )
                    return None
                logger.warning(
                    f"Agent {agent.id} has missing tmux session {agent.tmux_session_name}, recreating"
                )
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

                    _task = (
                        session.query(Task).filter_by(id=agent.current_task_id).first()
                    )
                    if _task and _task.phase_id:
                        _phase = (
                            session.query(_Phase).filter_by(id=_task.phase_id).first()
                        )
                        if _phase:
                            self._write_agent_tmux_log(
                                agent.id, _phase.name, tmux_output
                            )
                except Exception:
                    pass  # non-fatal; don't interrupt the monitoring cycle

            # DETECT: Agent exited to command line (shows $, %, >>>, bquote>)
            if self.guardian.detect_agent_exited(tmux_output):
                # Check if task is already done before restarting
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == "done":
                    logger.info(
                        f"Agent {agent.id[:8]} exited but task {task.id[:8]} is done — not restarting"
                    )
                    return None
                logger.warning(
                    f"Agent {agent.id[:8]} exited to command line — restarting"
                )
                await self._handle_missing_tmux_session(agent)
                return None

            # Detect garbled TUI output (CLI rendering corruption)
            # Get TUI status patterns from this agent's own CLI interface --
            # not a global default, since a mixed fleet (e.g. pi + claude)
            # would otherwise check every agent's output against pi's
            # patterns regardless of what CLI it's actually running.
            tui_patterns = None
            try:
                from src.interfaces.cli_interface import get_cli_agent

                cli_agent = get_cli_agent(agent.cli_type)
                tui_patterns = cli_agent.get_tui_status_patterns()
            except Exception:
                pass  # No CLI agent configured — use no patterns (strictest check)
            if self.guardian.detect_garbled_output(
                tmux_output, tui_patterns=tui_patterns
            ):
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == "done":
                    logger.info(
                        f"Agent {agent.id[:8]} garbled but task done — not restarting"
                    )
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
            if analysis.get("needs_steering", False):
                # Compute consecutive-stuck count up front so it's available
                # both for the signal emitted below and the auto-restart
                # check further down (previously computed after the signal
                # was emitted, so the signal's metadata always saw a
                # not-yet-assigned value and silently reported 0).
                past = self._get_past_summaries_for_agent(agent.id, limit=5)
                consecutive_stuck = sum(
                    1
                    for s in past
                    if s.get("needs_steering")
                    and s.get("steering_type") in ("stuck", "idle")
                )

                # Enhancement 4: Emit signal to orchestrator
                from src.monitoring.signals import (
                    MonitorSignal,
                    SignalType,
                    get_signal_queue,
                )

                steering_type = analysis.get("steering_type", "general")
                signal_type_map = {
                    "stuck": SignalType.STUCK_PATTERN,
                    "idle": SignalType.STUCK_PATTERN,
                    "drifting": SignalType.TRAJECTORY_DEVIATION,
                    "off_track": SignalType.TRAJECTORY_DEVIATION,
                    "over_engineering": SignalType.TRAJECTORY_DEVIATION,
                }
                signal_type = signal_type_map.get(
                    steering_type, SignalType.STUCK_PATTERN
                )
                task = await self.guardian._get_agent_task(agent)
                workflow_id = task.get("workflow_id") if task else None
                if workflow_id:
                    get_signal_queue().emit(
                        MonitorSignal(
                            type=signal_type,
                            workflow_id=workflow_id,
                            agent_id=agent.id,
                            confidence=0.7,
                            evidence=f"Guardian detected {steering_type}: "
                            f"{analysis.get('summary', '')[:100]}",
                            metadata={
                                "steering_type": steering_type,
                                "consecutive_flags": consecutive_stuck,
                            },
                        )
                    )

                await self.guardian.steer_agent(
                    agent=agent,
                    steering_type=analysis.get("steering_type", "general"),
                    message=analysis.get(
                        "steering_message"
                    ),  # Guardian should map from steering_recommendation
                )

                # Auto-restart if agent keeps ignoring steering
                if consecutive_stuck >= self.config.max_ignored_steering:
                    # Check if agent has recent activity before restarting
                    if agent.last_activity:
                        idle_seconds = (
                            datetime.utcnow() - agent.last_activity
                        ).total_seconds()
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
                    logger.debug(f"[STABLE-TRANSCRIPT] Final flush before auto-restart failed: {e}")

                self.agent_manager.tmux_server.kill_session(agent.tmux_session_name)
                logger.info(f"Killed tmux session {agent.tmux_session_name}")

            with self.db_manager.session_scope() as session:
                # Re-query the agent from this session to avoid detached object bugs
                db_agent = session.query(Agent).filter_by(id=agent.id).first()
                if db_agent:
                    db_agent.status = "terminated"
                    db_agent.current_task_id = None  # Clear stale reference
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

    def _get_past_summaries_for_agent(
        self, agent_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get past Guardian summaries for an agent.

        Args:
            agent_id: Agent ID
            limit: Maximum number of summaries to return

        Returns:
            List of past summaries
        """
        with self.db_manager.session_scope() as session:
            # Get past Guardian summaries from dedicated table
            analyses = (
                session.query(GuardianAnalysis)
                .filter(GuardianAnalysis.agent_id == agent_id)
                .order_by(GuardianAnalysis.timestamp.desc())
                .limit(limit)
                .all()
            )

            summaries = []
            for analysis in reversed(analyses):  # Reverse to get chronological order
                # Convert to dict format expected by Guardian
                summary = {
                    "current_phase": analysis.current_phase,
                    "trajectory_aligned": analysis.trajectory_aligned,
                    "alignment_score": analysis.alignment_score,
                    "needs_steering": analysis.needs_steering,
                    "steering_type": analysis.steering_type,
                    "trajectory_summary": analysis.trajectory_summary,
                    "accumulated_goal": analysis.accumulated_goal,
                    "timestamp": analysis.timestamp.isoformat()
                    if analysis.timestamp
                    else None,
                }
                summaries.append(summary)

            # If new tables don't have data yet, fallback to old AgentLog method
            if not summaries:
                logs = (
                    session.query(AgentLog)
                    .filter(
                        AgentLog.agent_id == agent_id,
                        AgentLog.log_type.in_(
                            ["guardian_analysis", "guardian_summary"]
                        ),
                    )
                    .order_by(AgentLog.created_at.desc())
                    .limit(limit)
                    .all()
                )

                for log in reversed(logs):
                    if log.details:
                        summaries.append(log.details)

            return summaries

    async def _update_agent_health_from_trajectory(
        self, agent: Agent, analysis: Dict[str, Any]
    ):
        """Update agent health based on trajectory analysis.

        PARENT-CHILD MODEL: Parent monitors via tmux peek and task progress.
        Guardian trajectory analysis is a signal for last-resort steering.
        health_check_failures is incremented when trajectory is off-track,
        so the Guardian can decide whether to intervene.
        """
        with self.db_manager.session_scope() as session:
            db_agent = session.query(Agent).filter_by(id=agent.id).first()
            if not db_agent:
                return

            # Track health_check_failures for Guardian last-resort steering
            if analysis.get("trajectory_aligned", True):
                # Agent is on track — reset failures so it recovers. This
                # also counts as real progress, mirroring the mechanical-
                # recovery detector's "tmux output changed" touch above:
                # refresh last_activity.
                db_agent.health_check_failures = 0
                db_agent.last_activity = datetime.utcnow()
            else:
                alignment_score = analysis.get("alignment_score", 0.5)
                if alignment_score < 0.3:
                    db_agent.health_check_failures += 2
                elif alignment_score < 0.5:
                    db_agent.health_check_failures += 1
                # Deliberately NOT touching last_activity here. Doing so
                # unconditionally (on every Guardian cycle, aligned or not)
                # defeated the max_ignored_steering auto-restart check
                # above: a persistently stuck agent that keeps failing
                # trajectory analysis would look "recently active" one
                # cycle later purely because Guardian ran, not because it
                # made progress -- silently disabling the restart's
                # idle_seconds >= 300 gate.

            # Save to dedicated Guardian analysis table
            guardian_analysis = GuardianAnalysis(
                agent_id=agent.id,
                current_phase=analysis.get("current_phase"),
                trajectory_aligned=analysis.get("trajectory_aligned", True),
                alignment_score=analysis.get("alignment_score", 1.0),
                needs_steering=analysis.get("needs_steering", False),
                steering_type=analysis.get("steering_type"),
                steering_recommendation=analysis.get("steering_recommendation"),
                trajectory_summary=analysis.get("trajectory_summary", "No summary"),
                last_claude_message_marker=analysis.get(
                    "last_claude_message_marker"
                ),  # NEW
                accumulated_goal=analysis.get("accumulated_goal"),
                current_focus=analysis.get("current_focus"),
                session_duration=analysis.get("session_duration"),
                conversation_length=analysis.get("conversation_length"),
                details=analysis,
            )
            session.add(guardian_analysis)

            # Also keep a simplified log entry for backwards compatibility
            summary_log = AgentLog(
                agent_id=agent.id,
                log_type="guardian_analysis",
                message=f"Guardian: {analysis.get('current_phase', 'unknown')} phase, "
                f"score={analysis.get('alignment_score', 0):.2f}, "
                f"aligned={analysis.get('trajectory_aligned', False)}",
                details={
                    "guardian_analysis_id": guardian_analysis.id
                },  # Reference to the full analysis
            )
            session.add(summary_log)

    async def _save_conductor_analysis(self, analysis: Dict[str, Any]):
        """Save Conductor analysis to dedicated table.

        Args:
            analysis: Conductor analysis result
        """
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
                logger.warning(
                    f"Agent {agent.id} tmux session {agent.tmux_session_name} missing"
                )
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
        blockers = accumulated_context.get("discovered_blockers", [])
        if blockers and agent.health_check_failures >= 3:
            logger.info(
                f"Agent {agent.id} has blockers ({agent.health_check_failures} failures): {blockers}"
            )

            # Last resort: try to help with top 3 blockers
            for blocker in blockers[:3]:
                message = f"I see you're blocked on: {blocker}. Try a different approach or create a sub-task if it's complex."
                await self.guardian.steer_agent(
                    agent=agent,
                    steering_type="last_resort_stuck",
                    message=message,
                )
        elif blockers:
            # Not enough failures yet — just log for observability
            logger.info(
                f"Agent {agent.id} has blockers (will steer after 3+ failures): {blockers[:2]}"
            )
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
            agent.id, f"Tmux session {agent.tmux_session_name} was missing, recreating"
        )

    def _write_agent_tmux_log(
        self, agent_id: str, phase_name: str, tmux_output: str
    ) -> None:
        """Write the agent's full tmux scrollback to docs/tmux/<phase>_<agent_id>.log.

        Called on every monitor cycle — overwrites so the file always contains
        the complete captured session up to the most recent poll. The forensics
        phase reads these files for a full picture of what each agent did.
        """
        if (
            not tmux_output
            or not self.phase_manager
            or not self.phase_manager.workflow_id
        ):
            return
        try:
            from pathlib import Path

            from src.core.database import Workflow

            session = self.db_manager.get_session()
            try:
                wf = (
                    session.query(Workflow)
                    .filter_by(id=self.phase_manager.workflow_id)
                    .first()
                )
                wd = wf.working_directory if wf else None
            finally:
                session.close()

            if not wd:
                return

            # Resolve to project root so logs survive worktree removal.
            # The working_directory may be a worktree (.worktrees/wt_*);
            # walk up past the .worktrees dir to get the stable project root.
            wd_path = Path(wd)
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
            logger.debug(
                f"[TMUX-LOG] {phase_name}/{agent_id[:8]}: wrote {len(tmux_output)} chars"
            )

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

                    if nudge_count >= MAX_STUCK_TASK_NUDGES:
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
                                "No activity has been seen from you in a while. "
                                f"Your CURRENT task_id is {task.id} -- if a resumed "
                                "session made you recall completing a DIFFERENT, "
                                "earlier task, that is not this one and does not "
                                "count. Check specifically: have you already called "
                                f"complete_my_task for task_id {task.id} in this "
                                "session? If not, do that now (verify your actual "
                                "work against the current code first, don't assume "
                                "an earlier task's fix covers this one). If you "
                                "have called it and are still here, say so "
                                "explicitly and stop.",
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
                if task.completion_notes:
                    logger.info(
                        f"[HEALTH] Task {task.id[:8]} stuck in_progress but has "
                        f"completion_notes — promoting to done (agent finished then crashed)"
                    )
                    task.status = "done"
                    task.completed_at = datetime.utcnow()
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
        except Exception as e:
            logger.error(f"Error in task stuck detection: {e}")
        finally:
            session.close()

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

    async def _check_workflow_stuck_state(self):
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
            self._log_diagnostic_status_report(
                conditions, trigger=False, reason="Disabled in config"
            )
            return

        if not self.phase_manager or not self.phase_manager.workflow_id:
            logger.info("[DIAGNOSTIC MONITOR] ❌ No active workflow")
            self._log_diagnostic_status_report(
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
                self._log_diagnostic_status_report(
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
                self._log_diagnostic_status_report(
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
                    datetime.utcnow() - recent_phase_completion.completed_at
                ).total_seconds()
                phase_cooldown = 120  # 2 minutes after phase completion
                if time_since_completion < phase_cooldown:
                    logger.info(
                        f"[DIAGNOSTIC MONITOR] ❌ Phase recently completed ({recent_phase_completion.phase_id[:8]}), cooling down: {time_since_completion:.0f}s / {phase_cooldown}s"
                    )
                    self._log_diagnostic_status_report(
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
                self._log_diagnostic_status_report(
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
                self._log_diagnostic_status_report(
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
                    datetime.utcnow() - last_diagnostic.triggered_at
                ).total_seconds()
                if time_since_last < self.config.diagnostic_cooldown_seconds:
                    logger.info(
                        f"[DIAGNOSTIC MONITOR] ❌ Cooldown active: {time_since_last:.0f}s / {self.config.diagnostic_cooldown_seconds}s required"
                    )
                    self._log_diagnostic_status_report(
                        conditions,
                        trigger=False,
                        reason=f"Cooldown active ({time_since_last:.0f}s < {self.config.diagnostic_cooldown_seconds}s)",
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
                stuck_time = (datetime.utcnow() - latest_task_time).total_seconds()
                if stuck_time < self.config.diagnostic_min_stuck_time_seconds:
                    logger.info(
                        f"[DIAGNOSTIC MONITOR] ❌ Not stuck long enough: {stuck_time:.0f}s / {self.config.diagnostic_min_stuck_time_seconds}s required"
                    )
                    self._log_diagnostic_status_report(
                        conditions,
                        trigger=False,
                        reason=f"Not stuck long enough ({stuck_time:.0f}s < {self.config.diagnostic_min_stuck_time_seconds}s)",
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
            self._log_diagnostic_status_report(
                conditions, trigger=True, stuck_time=stuck_time
            )

            await self._create_diagnostic_agent(workflow_id, tasks, stuck_time)

        except Exception as e:
            logger.error(
                f"[DIAGNOSTIC MONITOR] ❌ Error checking workflow stuck state: {e}",
                exc_info=True,
            )
            session.rollback()
        finally:
            session.close()

    def _log_diagnostic_status_report(
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

    async def _create_diagnostic_agent(
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

    async def _gather_diagnostic_context(
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
                .limit(self.config.diagnostic_max_agents_to_analyze)
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
                .limit(self.config.diagnostic_max_conductor_analyses)
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

    async def _generate_diagnostic_prompt(self, context: Dict[str, Any]) -> str:
        """Generate diagnostic prompt from template.

        Args:
            context: Diagnostic context dictionary

        Returns:
            Formatted diagnostic prompt
        """
        from pathlib import Path

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
