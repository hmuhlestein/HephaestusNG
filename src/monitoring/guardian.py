"""Guardian monitoring system with trajectory thinking for individual agents."""

import asyncio
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from src.agents.manager import AgentManager
from src.core.database import Agent, AgentLog, DatabaseManager, Task
from src.interfaces import LLMProviderInterface

logger = logging.getLogger(__name__)


class SteeringType(Enum):
    """Types of steering interventions."""

    STUCK = "stuck"
    DRIFTING = "drifting"
    VIOLATING_CONSTRAINTS = "violating_constraints"
    OVER_ENGINEERING = "over_engineering"
    CONFUSED = "confused"
    OFF_TRACK = "off_track"


class TrajectoryPhase(Enum):
    """Agent work phases."""

    EXPLORATION = "exploration"
    INFORMATION_GATHERING = "information_gathering"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    EXPLANATION = "explanation"
    COMPLETED = "completed"


class Guardian:
    """
    Guardian system that monitors individual agents using trajectory thinking.

    This replaces the old nudge system with intelligent monitoring that:
    - Builds accumulated context from entire agent session
    - Tracks persistent constraints and goals
    - Detects trajectory drift and violations
    - Provides targeted steering interventions
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        agent_manager: AgentManager,
        llm_provider: LLMProviderInterface,
    ):
        """Initialize Guardian.

        Args:
            db_manager: Database manager
            agent_manager: Agent manager for tmux operations
            llm_provider: LLM provider for trajectory analysis
        """
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.llm_provider = llm_provider

        # Cache for agent trajectories to avoid recomputing
        self.trajectory_cache: Dict[str, Dict[str, Any]] = {}

        # Track steering history to avoid over-messaging
        self.steering_history: Dict[str, List[Dict[str, Any]]] = {}

        # Track consecutive same-type flags per agent, so soft concerns
        # (drifting/off_track/etc) require confirmation across >=2 passes
        # before Guardian acts on them, rather than a single LLM trajectory
        # judgment call. Genuinely stuck/idle agents still act on the first
        # flag — waiting there only prolongs a frozen agent.
        self._consecutive_flags: Dict[str, Dict[str, Any]] = {}

    async def analyze_agent_with_trajectory(
        self,
        agent: Agent,
        tmux_output: str,
        past_summaries: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Analyze agent using GPT-5 with trajectory thinking.

        This method calls GPT-5 to apply trajectory thinking and understand:
        - Where the agent is in its overall journey
        - What constraints and goals persist
        - Whether the agent is on track
        - If steering intervention is needed

        Args:
            agent: Agent to analyze
            tmux_output: Current tmux output (last N lines)
            past_summaries: Previous Guardian summaries for this agent

        Returns:
            GPT-5 analysis with trajectory-aware summary and steering decision
        """
        logger.info(
            f"Guardian GPT-5 analyzing agent {agent.id} with trajectory thinking"
        )

        try:
            # Build accumulated context from entire session
            accumulated_context = await self._build_accumulated_context(
                agent, past_summaries
            )

            # Get task details for context
            task = await self._get_agent_task(agent)
            if not task:
                logger.error(f"No task found for agent {agent.id}")
                return self._get_default_analysis(agent)

            # Get Phase context if task has a phase
            phase_info = None
            if task.phase_id and task.workflow_id:
                phase_info = await self._get_phase_context(
                    task.phase_id, task.workflow_id
                )
                if phase_info:
                    logger.info(
                        f"📋 Loaded Phase context: {phase_info['workflow_context']['current_position']} - {phase_info['phase_name']}"
                    )

            # Call GPT-5 to analyze trajectory
            # This is the CORE - GPT-5 does the trajectory thinking, not static checks

            # Extract last message marker from most recent summary
            last_message_marker = None
            if past_summaries:
                # Get the most recent summary's marker
                last_message_marker = past_summaries[-1].get(
                    "last_claude_message_marker"
                )

            # Log summary of what we're sending to GPT-5
            logger.info("=" * 60)
            logger.info(f"🤖 GUARDIAN GPT-5 ANALYSIS for agent {agent.id}")
            logger.info("=" * 60)
            logger.info(
                f"Overall Goal: {accumulated_context.get('overall_goal', 'Unknown')[:100]}..."
            )
            logger.info(f"Past Summaries Count: {len(past_summaries)}")
            logger.info(
                f"Last Message Marker: {last_message_marker or 'None (first analysis)'}"
            )
            logger.info(f"Task ID: {task.id}")
            logger.info(f"Phase Info: {'Present' if phase_info else 'None'}")
            logger.info("=" * 60)

            # Hard timeout so a slow/over-streaming model (mimo can stream a reasoning
            # trace for minutes and still fail to parse) can NEVER freeze the monitor
            # loop. On timeout we fall back to the benign default analysis.
            GUARDIAN_LLM_TIMEOUT = 90
            try:
                analysis = await asyncio.wait_for(
                    self.llm_provider.analyze_agent_trajectory(
                        agent_output=tmux_output,
                        accumulated_context=accumulated_context,
                        past_summaries=past_summaries,
                        task_info={
                            "description": task.enriched_description
                            or task.raw_description,
                            "done_definition": task.done_definition,
                            "task_id": task.id,
                            "agent_id": agent.id,
                            "phase_info": phase_info,  # NEW: Pass phase information
                        },
                        last_message_marker=last_message_marker,
                    ),
                    timeout=GUARDIAN_LLM_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Guardian analysis timed out (>{GUARDIAN_LLM_TIMEOUT}s) for agent {agent.id} "
                    f"— using default analysis (model over-streamed the structured call)"
                )
                return self._get_default_analysis(agent)

            # Log what we got back from GPT-5
            logger.info("=" * 60)
            logger.info(f"✅ GUARDIAN GPT-5 RESPONSE for agent {agent.id}")
            logger.info("=" * 60)
            logger.info(f"Full Response: {analysis}")
            logger.info("=" * 60)

            # GPT-5 returns the complete trajectory analysis
            # Extract and enhance the results
            result = {
                "agent_id": agent.id,
                "agent_type": agent.agent_type,  # Include agent type for Conductor
                "trajectory_summary": analysis.get(
                    "trajectory_summary", "No summary"
                ),  # Use consistent key name
                "current_phase": analysis.get("current_phase", "unknown"),
                "trajectory_aligned": analysis.get("trajectory_aligned", True),
                "alignment_score": analysis.get("alignment_score", 0.5),
                "alignment_issues": analysis.get("alignment_issues", []),
                "needs_steering": analysis.get("needs_steering", False),
                "steering_type": analysis.get("steering_type"),
                "steering_message": analysis.get(
                    "steering_recommendation"
                ),  # Map from LLM response key
                "accumulated_goal": accumulated_context["overall_goal"],
                "active_constraints": accumulated_context["constraints"],
                # Remove progress_percentage as requested
            }

            # Cache for Conductor
            self.trajectory_cache[agent.id] = {
                "analysis": result,
                "accumulated_context": accumulated_context,
                "timestamp": datetime.utcnow(),
            }

            # Log the GPT-5 analysis
            logger.info(
                f"GPT-5 Guardian analysis for {agent.id}: "
                f"phase={result['current_phase']}, "
                f"aligned={result['trajectory_aligned']}, "
                f"needs_steering={result['needs_steering']}"
            )

            return result

        except Exception as e:
            logger.error(f"GPT-5 Guardian analysis failed for agent {agent.id}: {e}")
            return self._get_default_analysis(agent)

    async def _build_accumulated_context(
        self,
        agent: Agent,
        past_summaries: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Build accumulated context from entire agent session.

        This implements the core trajectory thinking concept:
        - Extract overall goals from entire conversation
        - Track constraints that persist until lifted
        - Resolve references like "this/that"
        - Understand the complete journey
        """
        logger.debug(f"Building accumulated context for agent {agent.id}")

        # Get all agent logs to understand full conversation
        session = self.db_manager.get_session()
        try:
            logs = (
                session.query(AgentLog)
                .filter_by(agent_id=agent.id)
                .order_by(AgentLog.created_at)
                .all()
            )

            # Extract conversation history
            conversation_history = []
            for log in logs:
                if log.log_type in ["input", "output", "message"]:
                    conversation_history.append(
                        {
                            "type": log.log_type,
                            "content": log.message,
                            "timestamp": log.created_at,
                        }
                    )

            # Get task for initial context
            task = session.query(Task).filter_by(id=agent.current_task_id).first()

            # Build accumulated context
            context = {
                "overall_goal": task.enriched_description if task else "Unknown",
                "done_definition": task.done_definition if task else "Unknown",
                "constraints": [],
                "lifted_constraints": [],
                "references": {},  # Resolved "this/that" references
                "standing_instructions": [],
                "conversation_length": len(conversation_history),
                "session_start": logs[0].created_at if logs else datetime.utcnow(),
            }

            # Extract constraints and goals from summaries
            for summary in past_summaries:
                if "constraints" in summary:
                    for constraint in summary["constraints"]:
                        if constraint not in context["lifted_constraints"]:
                            if constraint not in context["constraints"]:
                                context["constraints"].append(constraint)

                if "lifted_constraints" in summary:
                    for lifted in summary["lifted_constraints"]:
                        if lifted in context["constraints"]:
                            context["constraints"].remove(lifted)
                        context["lifted_constraints"].append(lifted)

                # Update goal if it evolved
                if "evolved_goal" in summary:
                    context["overall_goal"] = summary["evolved_goal"]

            return context

        finally:
            session.close()

    async def steer_agent(
        self,
        agent: Agent,
        steering_type: str,
        message: str,
    ):
        """
        PARENT-CHILD MODEL: Guardian acts as last resort.

        By default, the parent workflow monitors its children via tmux peek
        and task progress. If the parent detects a problem and prompts the
        human, the human can dismiss — and the Guardian gets ONE chance to
        steer the agent before the parent terminates it.

        The Guardian only steers if:
        1. The agent has been flagged multiple times (trajectory analysis)
        2. The parent has already detected impasse and prompted human
        3. The cooldown has elapsed (no spam)
        """
        logger.info(
            f"[GUARDIAN] Agent {agent.id[:8]} flagged: {steering_type} — "
            f"last resort steering (parent already detected impasse)"
        )

        # Genuinely stuck/idle agents act on the first flag — waiting only
        # prolongs a frozen agent. Everything else (drifting, off_track,
        # over_engineering, confused, ...) is a single LLM trajectory
        # judgment call and can be wrong; require the SAME type to be flagged
        # on >=2 consecutive passes before acting, matching what this
        # docstring already claims ("flagged multiple times") but the caller
        # never actually enforced. This is what let a single off-track
        # judgment interrupt a legitimate in-progress file write.
        NEEDS_CONFIRMATION = {
            SteeringType.DRIFTING.value,
            SteeringType.OFF_TRACK.value,
            SteeringType.OVER_ENGINEERING.value,
            SteeringType.CONFUSED.value,
            SteeringType.VIOLATING_CONSTRAINTS.value,
        }
        if steering_type in NEEDS_CONFIRMATION:
            flag_state = self._consecutive_flags.get(agent.id)
            now = datetime.utcnow()
            stale = (
                flag_state is None
                or flag_state["type"] != steering_type
                or (now - flag_state["last_seen"]) > timedelta(minutes=10)
            )
            if stale:
                self._consecutive_flags[agent.id] = {
                    "type": steering_type,
                    "count": 1,
                    "last_seen": now,
                }
                logger.info(
                    f"[GUARDIAN] Agent {agent.id[:8]} flagged {steering_type} "
                    f"(1/2) — waiting for a second consecutive flag before acting"
                )
                return
            flag_state["count"] += 1
            flag_state["last_seen"] = now
            if flag_state["count"] < 2:
                logger.info(
                    f"[GUARDIAN] Agent {agent.id[:8]} flagged {steering_type} "
                    f"({flag_state['count']}/2) — waiting for confirmation before acting"
                )
                return
            # Confirmed on a second consecutive pass — clear and proceed to act.
            del self._consecutive_flags[agent.id]

        # Check cooldown - max 1 steering per 10 minutes (longer than before)
        if not self._should_steer_agent(agent.id):
            logger.info(
                f"[GUARDIAN] Discarding — cooldown active for agent {agent.id[:8]}"
            )
            return

        # Check if there's already a queued message
        tmux_output = self.agent_manager.get_agent_output(agent.id, lines=50)
        if "Press up to edit queued messages" in tmux_output:
            logger.info(
                f"[GUARDIAN] Discarding — previous message still queued for agent {agent.id[:8]}"
            )
            self._record_steering(
                agent.id,
                f"{steering_type}_DISCARDED",
                f"Queued message detected: {message[:200]}...",
            )
            return

        # Only break a possible thought-loop (Esc for pi, polymorphic per CLI)
        # for genuinely stuck/idle agents — that's the one case where an
        # in-flight generation is actually a non-terminating loop worth
        # killing. For softer concerns, the agent is doing finite, possibly
        # valid work (e.g. a large file write); interrupting it destroys
        # real progress for no benefit, since the steering message will be
        # read at the agent's next natural pause anyway.
        if steering_type in (SteeringType.STUCK.value, "idle"):
            await self.agent_manager.send_recovery_keystrokes(agent.id)
        formatted_message = f"\n[GUARDIAN - LAST RESORT]: {message}\n"
        await self.agent_manager.send_message_to_agent(agent.id, formatted_message)
        self._record_steering(agent.id, steering_type, message)

        # Save to database
        session = self.db_manager.get_session()
        try:
            log_entry = AgentLog(
                agent_id=agent.id,
                log_type="guardian_steering",
                message=f"Guardian last-resort steering: {steering_type}",
                details={
                    "type": steering_type,
                    "message": message,
                    "timestamp": datetime.utcnow().isoformat(),
                    "model": "parent_child_last_resort",
                },
            )
            session.add(log_entry)
            session.commit()
        finally:
            session.close()

    def _should_steer_agent(self, agent_id: str) -> bool:
        """Check if we should steer agent (avoid over-messaging).

        Last-resort model: max 1 steering per 10 minutes.
        """
        if agent_id not in self.steering_history:
            self.steering_history[agent_id] = []
            return True

        # Check recent steering
        recent_steerings = [
            s
            for s in self.steering_history[agent_id]
            if datetime.fromisoformat(s["timestamp"])
            > datetime.utcnow() - timedelta(minutes=10)
        ]

        return len(recent_steerings) == 0

    def detect_agent_exited(self, tmux_output: str) -> bool:
        """Detect if agent has exited to the command line.

        Looks for shell prompts like '$', '%', '>>>', 'bquote>' which indicate
        the agent session ended and we're at a shell.
        """
        if not tmux_output:
            return False
        lines = tmux_output.strip().split("\n")[-5:]  # Check last 5 lines
        for line in lines:
            line = line.strip()
            # Shell prompts at start of line
            if line.startswith(("$ ", "% ", ">>> ", "bquote> ")):
                return True
            # Python REPL
            if line.startswith(">>> "):
                return True
            # zsh/bash prompt patterns
            if line.endswith(" %") or line.endswith(" $"):
                return True
        return False

    def detect_garbled_output(
        self, tmux_output: str, tui_patterns: Optional[List[str]] = None
    ) -> bool:
        """Detect garbled/repeating TUI output.

        Only flags output that is clearly corrupted — not the CLI tool's
        normal status bar rendering.

        Args:
            tmux_output: Raw tmux capture-pane output
            tui_patterns: Optional list of regex patterns that are normal
                         TUI status (from CLIAgentInterface.get_tui_status_patterns).
                         Stripped before checking for garbling.
        """
        if not tmux_output or len(tmux_output) < 200:
            return False
        text = tmux_output[-500:]
        # Strip known TUI status patterns (CLI-specific)
        clean = text
        if tui_patterns:
            for pat in tui_patterns:
                import re

                clean = re.sub(pat, "", clean, flags=re.IGNORECASE)
        clean = clean.strip()
        # If most of the output is TUI status, it's not garbled
        if len(clean) < 100:
            return False
        # Only flag if there's a clear repeating pattern (3+ char substring repeated 8+ times)
        for window in [3, 4, 5]:
            if len(clean) < window * 8:
                continue
            seen = {}
            for i in range(len(clean) - window):
                chunk = clean[i : i + window]
                if chunk.isalpha() and len(chunk) >= 3:
                    seen[chunk] = seen.get(chunk, 0) + 1
            if any(v >= 8 for v in seen.values()):
                return True
        return False

    def _record_steering(self, agent_id: str, steering_type: str, message: str):
        """Record steering in history."""
        if agent_id not in self.steering_history:
            self.steering_history[agent_id] = []

        self.steering_history[agent_id].append(
            {
                "type": steering_type,
                "message": message,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )

        # Keep only last 10 steerings
        self.steering_history[agent_id] = self.steering_history[agent_id][-10:]

    def _extract_last_error(self, tmux_output: str) -> str:
        """Extract last error message from output."""
        lines = tmux_output.split("\n")
        for i in range(len(lines) - 1, -1, -1):
            if "error" in lines[i].lower():
                # Get error and next 2 lines for context
                error_context = lines[i : min(i + 3, len(lines))]
                return " ".join(error_context)[:200]
        return "The error details are not clear from the output."

    async def _get_agent_task(self, agent: Agent) -> Optional[Task]:
        """Get task for agent."""
        session = self.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=agent.current_task_id).first()
            return task
        finally:
            session.close()

    async def _get_phase_context(
        self, phase_id: str, workflow_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get phase context for Guardian analysis.

        Args:
            phase_id: Phase ID
            workflow_id: Workflow ID

        Returns:
            Phase context dictionary or None
        """
        session = self.db_manager.get_session()
        try:
            from src.core.database import Phase, Workflow

            # Get the phase
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                return None

            # Get workflow for context
            workflow = session.query(Workflow).filter_by(id=workflow_id).first()

            # Get all phases in workflow for position context
            all_phases = (
                session.query(Phase)
                .filter_by(workflow_id=workflow_id)
                .order_by(Phase.order)
                .all()
            )

            return {
                "phase_id": phase.id,
                "phase_number": phase.order,
                "phase_name": phase.name,
                "phase_description": phase.description,
                "done_definitions": phase.done_definitions or [],
                "additional_notes": phase.additional_notes,
                "outputs": phase.outputs,
                "next_steps": phase.next_steps,
                "working_directory": phase.working_directory,
                "workflow_context": {
                    "workflow_id": workflow_id,
                    "workflow_name": workflow.name if workflow else "Unknown",
                    "total_phases": len(all_phases),
                    "current_position": f"Phase {phase.order} of {len(all_phases)}",
                    "all_phase_names": [p.name for p in all_phases],
                },
            }
        finally:
            session.close()

    def _get_default_analysis(self, agent: Agent) -> Dict[str, Any]:
        """Get default analysis when LLM analysis fails."""
        return {
            "agent_id": agent.id,
            "agent_type": agent.agent_type,  # Include agent type for Conductor
            "trajectory_summary": "LLM analysis unavailable - using default",  # Use consistent key name
            "current_phase": "unknown",
            "trajectory_aligned": True,
            "alignment_score": 0.5,
            "alignment_issues": [],
            "needs_steering": False,
            "steering_type": None,
            "steering_message": None,  # Keep consistent field name
            "accumulated_goal": "Unknown",
            "active_constraints": [],
        }

    def get_cached_trajectory(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get cached trajectory for agent (used by Conductor)."""
        return self.trajectory_cache.get(agent_id)

    def clear_agent_cache(self, agent_id: str):
        """Clear cached data for agent."""
        if agent_id in self.trajectory_cache:
            del self.trajectory_cache[agent_id]
        if agent_id in self.steering_history:
            del self.steering_history[agent_id]
