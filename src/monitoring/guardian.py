"""Guardian monitoring system with trajectory thinking for individual agents."""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, NotRequired, Optional, TypedDict

from src.agents.manager import AgentManager
from src.core.database import Agent, AgentLog, DatabaseManager, Task, utc_now
from src.interfaces import LLMProviderInterface
from src.prompts.loader import get_prompt

logger = logging.getLogger(__name__)


class GuardianTrajectoryAnalysis(TypedDict):
    """Canonical shape of analyze_agent_with_trajectory's return value
    (SOLID review 3.7) -- guardian_dispatch.py builds a GuardianAnalysis
    DB row (src/core/database.py) straight from this dict's keys, and this
    codebase has independently found the same bug shape here twice: an LLM
    response key ("steering_recommendation") renamed to a different result
    key ("steering_message") with the old name still read downstream, and
    "last_claude_message_marker" simply never copied out of the raw LLM
    response despite the prompt asking for it. A plain Dict[str, Any]
    return type let both drift silently; this doesn't, since a wrong key
    name here is now a type error, not a runtime None.

    NotRequired fields are ones _get_default_analysis/
    _get_timeout_escalation_analysis genuinely don't set -- those paths
    skip _build_accumulated_context entirely (no LLM call was analyzed),
    so they have nothing real to report for them; GuardianAnalysis's
    corresponding DB columns get None in that case, which is the correct
    degraded value, not a bug.

    current_focus was a distinct gap from the naming mismatches above: it
    had a DB column, a prompt-template placeholder, and a frontend
    consumer (TaskDetailModal.tsx, labeled "Current Focus" next to
    "Accumulated Goal"), but no producer ever computed a value for it --
    always "Unknown"/None end-to-end, silently, for every agent ever
    monitored. Added to the LLM's required JSON output
    (guardian_trajectory_analysis.md) as a short noun phrase for what the
    agent is doing at this exact moment, narrower than trajectory_summary's
    full-sentence narrative. Still NotRequired here: the two fallback
    paths below have no LLM response to draw it from, same as
    last_claude_message_marker/conversation_length/session_duration.
    """

    # Any, not str: these come straight from a SQLAlchemy Column(String)
    # attribute, which mypy sees as Column[str] rather than str at the
    # class-definition sites that populate this dict (a pre-existing,
    # repo-wide ORM/mypy friction, not something specific to this dict).
    agent_id: Any
    agent_type: Any
    trajectory_summary: str
    current_phase: str
    trajectory_aligned: bool
    alignment_score: float
    alignment_issues: List[str]
    needs_steering: bool
    steering_type: Optional[str]
    steering_message: Optional[str]
    accumulated_goal: str
    active_constraints: List[str]
    last_claude_message_marker: NotRequired[Optional[str]]
    conversation_length: NotRequired[int]
    session_duration: NotRequired[str]
    current_focus: NotRequired[Optional[str]]
    # True only from _get_default_analysis's "LLM analysis failed" fallback
    # -- guardian_dispatch.py's update_agent_health_from_trajectory must
    # NOT treat this analysis's benign trajectory_aligned=True as evidence
    # of real progress (it refreshes Agent.last_activity on that signal).
    # Absent/False for every real LLM-judged analysis, aligned or not, and
    # for _get_timeout_escalation_analysis (already trajectory_aligned=
    # False, so it never hit this path anyway). See guardian_dispatch.py's
    # own comment for the confirmed live incident this field fixes.
    analysis_unavailable: NotRequired[bool]

# Consecutive Guardian LLM-analysis timeouts (see analyze_agent_with_trajectory's
# GUARDIAN_LLM_TIMEOUT) before the timeout pattern itself is treated as a stuck
# signal, instead of silently returning the benign "aligned" default forever.
GUARDIAN_TIMEOUT_ESCALATION_THRESHOLD = 3

# Claude Code's session-reuse launch command (see cli_interface.py's
# ClaudeCodeAgent.get_launch_command) tries --session-id then falls back to
# --resume (or vice versa); whichever branch doesn't match the session's
# actual state prints this exact rejection before the other branch runs and
# succeeds. It's expected noise on any phase that reuses a session (see
# session_roles in workflow.yaml), not a live problem -- but shown raw, the
# Guardian LLM has read this as an unresolved issue and sent the agent a
# fabricated "fix your session conflict" steering message. Stripped before
# the LLM sees it; left untouched everywhere else (transcript log, the live
# output viewer, detect_agent_exited/detect_garbled_output) since those are
# either historical record or unrelated pattern checks.
_BENIGN_SESSION_ERROR_RE = re.compile(
    r"^Error: Session ID [0-9a-f-]+ is already in use\.?\s*$", re.MULTILINE
)


def _sanitize_tmux_output_for_llm(tmux_output: str) -> str:
    return _BENIGN_SESSION_ERROR_RE.sub(
        "[Hephaestus note: expected session-resume artifact, not a live error -- ignore]",
        tmux_output,
    )


# External eval (§3.5): the Guardian's own trajectory-analysis LLM call
# misjudged a healthy, already-finished agent as "stuck in a search loop"
# and sent three consecutive "[GUARDIAN - LAST RESORT]: stop exploring and
# write features.json" nudges -- AFTER the agent had already written and
# verified that exact file. Each time, the agent's own reply correctly said
# the work was done and the real blocker was that complete_my_task wasn't
# callable (a separate MCP-registration bug). ~40 of that run's 56 minutes
# were spent on Guardian re-prodding already-finished work instead of
# recognizing "agent reports a tool it needs is unavailable" as a
# fundamentally different situation from a stuck loop -- one more "keep
# working" nudge can't fix a missing tool registration, and cutting the
# agent off (steer_agent's Esc interrupt for stuck/idle) can even sever the
# very reply that explains the real blocker.
#
# Two independent, narrowly-scoped signals must BOTH be present (see
# detect_tool_unavailable_blocker below) -- requiring the compound match is
# what keeps a genuinely stuck agent's passing remark ("I can't get this to
# work") from being incorrectly exempted from real stuck-loop nudging;
# mirrors this module's other confirmation gates (GUARDIAN_TIMEOUT_
# ESCALATION_THRESHOLD, the 2-consecutive-flag gate in
# _evaluate_steering_eligibility) in spirit, though here the "confirmation"
# is two co-occurring signals in one cycle rather than repeated cycles --
# a real capability outage is a stable, re-readable fact in the transcript
# each cycle, not a one-off judgment call that benefits from waiting.

# Signal 1: the agent believes its actual work is already finished.
# Deliberately just "already" + a completion verb (not a longer phrase) --
# broad within THIS category is fine because signal 2 below is what
# supplies the specificity; the incident's own phrasing ("work was already
# done") is exactly this shape.
_WORK_ALREADY_DONE_RE = re.compile(
    r"already\s+(?:done|complete|completed|finished|wrote|written|created|verified)",
    re.IGNORECASE,
)

# Signal 2: the agent names a specific tool/function (an identifier in the
# snake_case shape every MCP tool in this codebase uses -- complete_my_task,
# update_task_status, ...) and reports it as not callable/registered/
# resolving/available, in either word order ("X isn't callable" / "isn't
# registered ... X" / "can't call X"). Anchored to this curated
# capability-registration vocabulary (callable/registered/resolve/exposed/
# recognized/available/working/found) rather than a bare "can't do X" --
# that generic phrasing is exactly the passing remark a genuinely stuck
# agent might also make, and must NOT trip this detector on its own.
_NOT_WORD = (
    r"(?:isn'?t|is\s+not|wasn'?t|was\s+not|doesn'?t|does\s+not|didn'?t|"
    r"did\s+not|couldn'?t|could\s+not|can'?t|cannot|not)"
)
_UNAVAIL_TARGET = (
    r"(?:callable|registered|resolve(?:d|ing)?|exposed|recognized|"
    r"available|working|found|showing up)"
)
_TOOL_NAME_TOKEN = r"`?\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,5}\b`?"
_TOOL_UNAVAILABLE_RE = re.compile(
    rf"(?:{_TOOL_NAME_TOKEN}[^\n.]{{0,50}}{_NOT_WORD}[^\n.]{{0,25}}{_UNAVAIL_TARGET}"
    rf"|{_NOT_WORD}[^\n.]{{0,25}}{_UNAVAIL_TARGET}[^\n.]{{0,50}}{_TOOL_NAME_TOKEN}"
    # "can't call X" specifically -- "call" alone is too generic to pair
    # with _UNAVAIL_TARGET (would match "can't call this a success"), so
    # it's only accepted immediately adjacent to a named tool token.
    rf"|{_NOT_WORD}\s+call\s+(?:the\s+|this\s+)?{_TOOL_NAME_TOKEN})",
    re.IGNORECASE,
)


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

        # Consecutive Guardian LLM-analysis timeouts per agent (see
        # analyze_agent_with_trajectory's GUARDIAN_LLM_TIMEOUT except-block).
        # Reset to 0 on any successful analysis for that agent.
        self._consecutive_timeouts: Dict[str, int] = {}

    async def analyze_agent_with_trajectory(
        self,
        agent: Agent,
        tmux_output: str,
        past_summaries: List[Dict[str, Any]],
    ) -> GuardianTrajectoryAnalysis:
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
            if task["phase_id"] and task["workflow_id"]:
                phase_info = await self._get_phase_context(
                    task["phase_id"], task["workflow_id"]
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
            logger.info(f"Task ID: {task['id']}")
            logger.info(f"Phase Info: {'Present' if phase_info else 'None'}")
            logger.info("=" * 60)

            # Hard timeout so a slow/over-streaming model (mimo can stream a reasoning
            # trace for minutes and still fail to parse) can NEVER freeze the monitor
            # loop. On timeout we fall back to the benign default analysis.
            guardian_llm_timeout = 90
            try:
                analysis = await asyncio.wait_for(
                    self.llm_provider.analyze_agent_trajectory(
                        agent_output=_sanitize_tmux_output_for_llm(tmux_output),
                        accumulated_context=accumulated_context,
                        past_summaries=past_summaries,
                        task_info={
                            "description": task["enriched_description"]
                            or task["raw_description"],
                            "done_definition": task["done_definition"],
                            "task_id": task["id"],
                            "agent_id": agent.id,
                            "phase_info": phase_info,  # NEW: Pass phase information
                        },
                        last_message_marker=last_message_marker,
                    ),
                    timeout=guardian_llm_timeout,
                )
                self._consecutive_timeouts[agent.id] = 0
            except asyncio.TimeoutError:
                timeouts = self._consecutive_timeouts.get(agent.id, 0) + 1
                self._consecutive_timeouts[agent.id] = timeouts
                logger.warning(
                    f"Guardian analysis timed out (>{guardian_llm_timeout}s) for agent {agent.id} "
                    f"— using default analysis (model over-streamed the structured call) "
                    f"[{timeouts} consecutive]"
                )
                # The benign default (trajectory_aligned=True, needs_steering=False)
                # exists so a slow/over-streaming LLM call can never freeze the
                # monitor loop itself -- but returning it unconditionally, forever,
                # means an agent that's ACTUALLY stuck (not just slow to analyze)
                # never gets flagged: every cycle reports "fine" regardless of how
                # many times in a row the analysis itself failed to complete.
                # Observed live: an agent hard-stopped on a model error timed out
                # here 4+ times over 12 minutes, and Guardian reported "aligned,
                # no steering needed" every single time. After repeated timeouts,
                # treat the timeout pattern itself as the stuck signal.
                if timeouts >= GUARDIAN_TIMEOUT_ESCALATION_THRESHOLD:
                    logger.warning(
                        f"Agent {agent.id} has timed out {timeouts} consecutive "
                        "Guardian analyses — escalating to steering/auto-restart "
                        "instead of defaulting to 'aligned'"
                    )
                    return self._get_timeout_escalation_analysis(agent, timeouts)
                return self._get_default_analysis(agent)

            # Log what we got back from GPT-5
            logger.info("=" * 60)
            logger.info(f"✅ GUARDIAN GPT-5 RESPONSE for agent {agent.id}")
            logger.info("=" * 60)
            logger.info(f"Full Response: {analysis}")
            logger.info("=" * 60)

            # GPT-5 returns the complete trajectory analysis
            # Extract and enhance the results
            result: GuardianTrajectoryAnalysis = {
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
                # SOLID review 3.7: the LLM is explicitly asked to return
                # last_claude_message_marker "to mark conversation position
                # for next cycle" (guardian_trajectory_analysis.md), but it
                # was never copied out of the raw LLM response here -- the
                # marker-based "avoid re-analyzing the same content"
                # mechanism was silently a no-op, since
                # `past_summaries[-1].get("last_claude_message_marker")`
                # above always read back None from every prior cycle.
                "last_claude_message_marker": analysis.get(
                    "last_claude_message_marker"
                ),
                "conversation_length": accumulated_context["conversation_length"],
                "session_duration": accumulated_context["session_duration"],
                # SOLID review 3.7: newly added to the LLM's required output --
                # previously had a DB column/prompt slot/frontend consumer but
                # no producer anywhere, so this was always None.
                "current_focus": analysis.get("current_focus"),
            }

            # Cache for Conductor
            self.trajectory_cache[agent.id] = {
                "analysis": result,
                "accumulated_context": accumulated_context,
                "timestamp": utc_now(),
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
        with self.db_manager.session_scope() as session:
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
            session_start = logs[0].created_at if logs else utc_now()
            context = {
                "overall_goal": task.enriched_description if task else "Unknown",
                "done_definition": task.done_definition if task else "Unknown",
                "constraints": [],
                "lifted_constraints": [],
                "references": {},  # Resolved "this/that" references
                "standing_instructions": [],
                "conversation_length": len(conversation_history),
                "session_start": session_start,
                # SOLID review 3.7: this was never set, so the prompt always
                # showed the LLM "Session Duration: Unknown" -- session_start
                # was already computed and unused for this.
                "session_duration": str(utc_now() - session_start).split(".")[0],
                # SOLID review 3.7: current_focus is carried forward from the
                # most recent prior cycle's own analysis below, same as
                # overall_goal's evolved_goal update -- defaults to "Unknown"
                # on the agent's first cycle, when there is no prior summary.
                "current_focus": "Unknown",
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

                # Carry forward the most recent cycle's current_focus.
                if summary.get("current_focus"):
                    context["current_focus"] = summary["current_focus"]

            return context

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
        # Check if task is already done — don't send messages to completed agents
        task = await self._get_agent_task(agent)
        if task and task.get("status") == "done":
            logger.info(
                f"[GUARDIAN] Skipping steering for agent {agent.id[:8]} — "
                f"task {task.get('id', '')[:8]} is already done"
            )
            return

        logger.info(
            f"[GUARDIAN] Agent {agent.id[:8]} flagged: {steering_type} — "
            f"last resort steering (parent already detected impasse)"
        )

        # Fetched once, up front, so both the tool-unavailable check below
        # and the queued-message check further down read the same snapshot.
        tmux_output = self.agent_manager.get_agent_output(agent.id, lines=50)

        # A trajectory-analysis LLM judgment of "stuck"/"idle" produces the
        # generic "stop exploring, do the work" nudge (plus an Esc interrupt
        # keystroke, below) -- exactly the framing that's actively
        # counterproductive when the agent's own output shows the work is
        # already done and it's blocked on a missing tool, not looping. Soft
        # concerns (drifting/off_track/...) already require 2 confirmed
        # passes and never carry this framing, so they're not gated here.
        if steering_type in (
            SteeringType.STUCK.value,
            "idle",
        ) and self.detect_tool_unavailable_blocker(tmux_output):
            self._record_tool_unavailable_stall(agent, steering_type, message)
            return

        # Genuinely stuck/idle agents act on the first flag — waiting only
        # prolongs a frozen agent. Everything else (drifting, off_track,
        # over_engineering, confused, ...) is a single LLM trajectory
        # judgment call and can be wrong; require the SAME type to be flagged
        # on >=2 consecutive passes before acting, matching what this
        # docstring already claims ("flagged multiple times") but the caller
        # never actually enforced. This is what let a single off-track
        # judgment interrupt a legitimate in-progress file write.

        # Use consolidated rate-limiter (L-3 fix)
        eligible, reason = self._evaluate_steering_eligibility(agent.id, steering_type)
        if not eligible:
            logger.info(
                f"[GUARDIAN] Discarding — {reason} for agent {agent.id[:8]}"
            )
            return

        # Check if there's already a queued message
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

        await self._apply_steering(agent, steering_type, message)

    async def _apply_steering(
        self, agent: Agent, steering_type: str, message: str
    ) -> None:
        """Send the actual steering intervention and record it.

        Split out from steer_agent (SOLID review 3.6): steer_agent's own
        eligibility/precondition checks above (task-done, rate-limit,
        already-queued) all need I/O of their own (a DB read, a tmux
        read), so they can't be made pure either -- but the side-effecting
        intervention itself (keystrokes, message send, in-memory record,
        DB log) is a genuinely separable unit, testable/mockable on its
        own without re-deriving the eligibility logic every time.
        """
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
        with self.db_manager.session_scope() as session:
            log_entry = AgentLog(
                agent_id=agent.id,
                log_type="guardian_steering",
                message=f"Guardian last-resort steering: {steering_type}",
                details={
                    "type": steering_type,
                    "message": message,
                    "timestamp": utc_now().isoformat(),
                    "model": "parent_child_last_resort",
                },
            )
            session.add(log_entry)

    def _record_tool_unavailable_stall(
        self, agent: Agent, steering_type: str, message: str
    ) -> None:
        """Escalate a detected tool-unavailable stall distinctly from a
        normal steering intervention -- deliberately does NOT call
        _apply_steering (no nudge, no Esc interrupt): another "stop
        exploring and do the work" message can't fix a missing tool
        registration, and the interrupt keystroke risks cutting off the
        agent's own explanation of the real blocker. This needs a human
        or a different automated recovery path, so it's logged under a
        distinct log_type (guardian_tool_unavailable_stall, not
        guardian_steering) so it's queryable/alertable separately from
        ordinary steering activity.
        """
        logger.warning(
            f"[GUARDIAN - TOOL UNAVAILABLE] Agent {agent.id[:8]} reports its "
            f"work is already done and blocked on an unavailable tool -- "
            f"suppressing generic '{steering_type}' nudge, escalating "
            f"instead of re-prodding already-finished work"
        )
        self._record_steering(
            agent.id, f"{steering_type}_TOOL_UNAVAILABLE_STALL", message
        )
        with self.db_manager.session_scope() as session:
            log_entry = AgentLog(
                agent_id=agent.id,
                log_type="guardian_tool_unavailable_stall",
                message=(
                    f"Guardian suppressed a generic '{steering_type}' nudge — "
                    "agent output shows work already done, blocked on an "
                    "unavailable tool"
                ),
                details={
                    "steering_type": steering_type,
                    "suppressed_message": message,
                    "timestamp": utc_now().isoformat(),
                },
            )
            session.add(log_entry)

    def _evaluate_steering_eligibility(
        self, agent_id: str, steering_type: str
    ) -> tuple[bool, str]:
        """Evaluate whether an agent is eligible for steering.
        
        Consolidates the consecutive-flag confirmation gate and the
        cooldown check into one function with a clear reason output.
        
        Args:
            agent_id: Agent ID to check
            steering_type: Type of steering being attempted
            
        Returns:
            Tuple of (eligible: bool, reason: str)
        """
        # Types that need 2 consecutive flags before acting
        needs_confirmation = {
            SteeringType.DRIFTING.value,
            SteeringType.OFF_TRACK.value,
            SteeringType.OVER_ENGINEERING.value,
            SteeringType.CONFUSED.value,
            SteeringType.VIOLATING_CONSTRAINTS.value,
        }

        # Check consecutive-flag confirmation gate
        if steering_type in needs_confirmation:
            flag_state = self._consecutive_flags.get(agent_id)
            now = utc_now()
            stale = (
                flag_state is None
                or flag_state["type"] != steering_type
                or (now - flag_state["last_seen"]) > timedelta(minutes=10)
            )
            if stale:
                self._consecutive_flags[agent_id] = {
                    "type": steering_type,
                    "count": 1,
                    "last_seen": now,
                }
                return False, f"first flag ({steering_type}) — waiting for confirmation"

            flag_state["count"] += 1
            flag_state["last_seen"] = now
            if flag_state["count"] < 2:
                return False, f"{flag_state['count']}/2 flags — waiting for confirmation"

            # Confirmed — clear and proceed
            del self._consecutive_flags[agent_id]

        # Check cooldown — max 1 steering per 10 minutes
        if agent_id in self.steering_history:
            recent_steerings = [
                s for s in self.steering_history[agent_id]
                if datetime.fromisoformat(s["timestamp"]) > utc_now() - timedelta(minutes=10)
            ]
            if recent_steerings:
                return False, "cooldown active (10 minutes)"

        return True, "eligible"

    def detect_agent_exited(self, tmux_output: str, health_check_pattern: Optional[str] = None) -> bool:
        """Detect if agent has exited to the command line.

        Looks for shell prompts like '$', '%', '>>>', 'bquote>' which indicate
        the agent session ended and we're at a shell.

        health_check_pattern (cli_agent.get_health_check_pattern()), when
        given, is checked first: if the CLI's own ready-for-input UI is
        still present anywhere in tmux_output, the agent plainly has not
        exited, no matter what any individual line looks like below. Same
        principle as the fix to _detect_launch_failure (launch_pipeline.py)
        -- a confirmed-alive signal must win over a generic pattern match.

        The trailing "%"/"$" check below is also scoped to exclude a digit
        right before the prompt character: a real shell prompt's "%"/"$"
        is preceded by a path, hostname, or "~", never a bare number, so
        this excludes a legitimate progress or cost line ("Building... 87
        %", "Remaining: $45 $") from being misread as a shell prompt.
        """
        if not tmux_output:
            return False
        if health_check_pattern:
            try:
                if re.search(health_check_pattern, tmux_output):
                    return False
            except re.error:
                pass
        lines = tmux_output.strip().split("\n")[-5:]  # Check last 5 lines
        for line in lines:
            line = line.strip()
            # Shell prompts at start of line
            if line.startswith(("$ ", "% ", ">>> ", "bquote> ")):
                return True
            # zsh/bash prompt patterns
            if re.search(r"(?<!\d) %$", line) or re.search(r"(?<!\d) \$$", line):
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

    def detect_tool_unavailable_blocker(self, tmux_output: str) -> bool:
        """True if the agent's own recent output reports it has already
        finished its real work and is blocked because a tool/capability it
        needs is unavailable -- see the module-level comment above
        _WORK_ALREADY_DONE_RE for the incident this guards against.

        Both signals must be present in the SAME tmux_output snapshot; see
        that same comment for why this compound-AND is the false-positive
        guard rather than a repeated-cycle confirmation gate.
        """
        if not tmux_output:
            return False
        return bool(_WORK_ALREADY_DONE_RE.search(tmux_output)) and bool(
            _TOOL_UNAVAILABLE_RE.search(tmux_output)
        )

    def _record_steering(self, agent_id: str, steering_type: str, message: str):
        """Record steering in history."""
        if agent_id not in self.steering_history:
            self.steering_history[agent_id] = []

        self.steering_history[agent_id].append(
            {
                "type": steering_type,
                "message": message,
                "timestamp": utc_now().isoformat(),
            }
        )

        # Keep only last 10 steerings
        self.steering_history[agent_id] = self.steering_history[agent_id][-10:]

    def record_auto_restart(self, agent_id: str, reason: str):
        """Public method to record an auto-restart event.
        
        Called by MonitoringLoop when it restarts an agent, to keep
        Guardian's steering history accurate without reaching into
        private methods.
        """
        self._record_steering(agent_id, "AUTO_RESTART", reason)

    def _extract_last_error(self, tmux_output: str) -> str:
        """Extract last error message from output."""
        lines = tmux_output.split("\n")
        for i in range(len(lines) - 1, -1, -1):
            if "error" in lines[i].lower():
                # Get error and next 2 lines for context
                error_context = lines[i : min(i + 3, len(lines))]
                return " ".join(error_context)[:200]
        return "The error details are not clear from the output."

    async def _get_agent_task(self, agent: Agent) -> Optional[Dict[str, Any]]:
        """Get task for agent.
        
        Returns a dict with task primitives to avoid DetachedInstanceError
        across await boundaries (H-0d fix).
        """
        with self.db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id=agent.current_task_id).first()
            if not task:
                return None
            return {
                "id": task.id,
                "status": task.status,
                "phase_id": task.phase_id,
                "workflow_id": task.workflow_id,
                "enriched_description": task.enriched_description,
                "raw_description": task.raw_description,
                "done_definition": task.done_definition,
            }

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
        with self.db_manager.session_scope() as session:
            from src.autopilot.spec import load_phase_output_artifacts
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

            # Prefer workflow.yaml's required_output override when this
            # phase has one -- phase.outputs is a per-workflow-instance
            # snapshot taken at workflow-creation time from whatever
            # workflow.yaml said then, and never refreshed afterward. A
            # workflow created before an output-format change (e.g. the
            # OKF single-file refactor) otherwise keeps telling Guardian --
            # and, through it, the agent -- to produce the OLD file(s) for
            # its entire remaining run, not just its next retry.
            # load_phase_output_artifacts reads the override fresh from
            # disk. Phases with no override fall back to phase.outputs
            # (parsed, since it's a Text column holding a JSON-encoded
            # string, not a native list) so non-file descriptive text like
            # "source code in project path" is preserved as before.
            override = load_phase_output_artifacts(workflow_id).get(phase.name)
            if override:
                phase_outputs = override if isinstance(override, list) else [override]
            else:
                phase_outputs = phase.outputs
                if isinstance(phase_outputs, str):
                    try:
                        parsed = json.loads(phase_outputs)
                        if isinstance(parsed, list):
                            phase_outputs = parsed
                    except Exception:
                        pass

            return {
                "phase_id": phase.id,
                "phase_number": phase.order,
                "phase_name": phase.name,
                "phase_description": phase.description,
                "done_definitions": phase.done_definitions or [],
                "additional_notes": phase.additional_notes,
                "outputs": phase_outputs,
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

    def _get_default_analysis(self, agent: Agent) -> GuardianTrajectoryAnalysis:
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
            "analysis_unavailable": True,
        }

    def _get_timeout_escalation_analysis(
        self, agent: Agent, consecutive_timeouts: int
    ) -> GuardianTrajectoryAnalysis:
        """Analysis returned after GUARDIAN_TIMEOUT_ESCALATION_THRESHOLD consecutive
        Guardian LLM-analysis timeouts for this agent.

        Unlike _get_default_analysis (a neutral "aligned" fallback for an
        occasional slow call), this treats the timeout pattern itself as
        evidence the agent needs intervention: needs_steering=True with
        steering_type="stuck" feeds the same nudge + auto-restart path
        (_guardian_analysis_for_agent's "Auto-restart if agent keeps ignoring
        steering" block, monitor.py) that a real stuck-trajectory detection
        would trigger, and low trajectory_aligned/alignment_score also
        increments health_check_failures via _update_agent_health_from_trajectory.
        """
        return {
            "agent_id": agent.id,
            "agent_type": agent.agent_type,
            "trajectory_summary": (
                f"Guardian analysis timed out {consecutive_timeouts} times in a "
                "row — agent output has not changed enough to analyze, or the "
                "model itself is not responding"
            ),
            "current_phase": "unknown",
            "trajectory_aligned": False,
            "alignment_score": 0.2,
            "alignment_issues": [
                f"{consecutive_timeouts} consecutive Guardian analysis timeouts"
            ],
            "needs_steering": True,
            "steering_type": "stuck",
            "steering_message": get_prompt("guardian_unresponsive_steering"),
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
        self._consecutive_timeouts.pop(agent_id, None)
