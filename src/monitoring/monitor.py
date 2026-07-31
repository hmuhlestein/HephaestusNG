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
from src.prompts.loader import get_monitor_nudge

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


# How many idle-nudges a stuck task gets before "the agent produced output"
# stops being trusted as "the agent made progress" -- see the stuck-task
# nudge cap in _audit_system_health's own comment for the failure mode this
# closes (an agent that keeps replying without ever calling
# complete_my_task resets the idle check forever on activity alone).
MAX_STUCK_TASK_NUDGES = 3

# How many times _detect_cli_model_fallback/_verify_cli_model_fallback will
# retry an unconfirmed model switch for the same agent before giving up for
# good. Observed live: with no cap, an agent that kept refreezing retried an
# unconfirmed switch 40+ times over 7+ hours -- each retry blindly resent the
# same keystrokes into whatever state the CLI was actually in (the "revert on
# unconfirmed" only patches our own DB record, it never undoes anything in
# the live session), and one of those retries landed on a different, unusable
# catalog entry that broke the session outright.
MAX_FALLBACK_ATTEMPTS = 2


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
                                            self._log_agent_event(
                                                agent.id, "session_limit_terminated",
                                                f"Hit {limit_kind} ({agent.cli_type}) — terminated "
                                                f"and redispatched to {fallback_tool}/{fallback_model or 'default'}",
                                                {
                                                    "task_id": stuck_task.id,
                                                    "limit_kind": limit_kind,
                                                    "from_cli_type": agent.cli_type,
                                                    "fallback_cli_type": fallback_tool,
                                                    "fallback_cli_model": fallback_model,
                                                },
                                                session=session,
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
                                                # If the workflow was paused by a prior limit
                                                # hit (before a fallback was available), clear
                                                # the stale pause so the pipeline resumes.
                                                if stuck_task.workflow_id:
                                                    _wf = (
                                                        session.query(Workflow)
                                                        .filter_by(id=stuck_task.workflow_id)
                                                        .first()
                                                    )
                                                    if _wf and _wf.status == "paused" and _wf.paused_by == "system":
                                                        _wf.status = "active"
                                                        _wf.paused_by = None
                                                        _wf.status_reason = None
                                                        _wf.paused_at = None
                                                        logger.info(
                                                            f"[SESSION-LIMIT] Cleared stale pause on "
                                                            f"workflow {stuck_task.workflow_id[:8]}"
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
                                                self._log_agent_event(
                                                    agent.id, "session_limit_terminated",
                                                    f"Hit {limit_kind} ({agent.cli_type}) — terminated, "
                                                    f"fallback ({fallback_tool}) creation also failed: {fallback_err}",
                                                    {
                                                        "task_id": stuck_task.id,
                                                        "limit_kind": limit_kind,
                                                        "from_cli_type": agent.cli_type,
                                                        "fallback_cli_type": fallback_tool,
                                                    },
                                                    session=session,
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
                                if stuck_task:
                                    self._log_agent_event(
                                        agent.id, "session_limit_terminated",
                                        f"Hit {limit_kind} ({agent.cli_type}) — terminated, "
                                        "no fallback configured",
                                        {
                                            "task_id": stuck_task.id,
                                            "limit_kind": limit_kind,
                                            "from_cli_type": agent.cli_type,
                                        },
                                        session=session,
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
                        msg = get_monitor_nudge("operation_aborted", mcp_note=mcp_note)
                    elif _MAX_TOKEN_LIMIT_RE.search(_strip_sgr(sig)):
                        msg = get_monitor_nudge("max_token_limit_recovery", mcp_note=mcp_note)
                    else:
                        msg = get_monitor_nudge("stuck_or_looping", mcp_note=mcp_note)
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

    async def _detect_cli_model_fallback(self, agent) -> bool:
        """When an agent has been frozen too long on its default model,
        switch it in-place to a configured fallback model via the CLI's own
        model-switching UI -- instead of leaving it frozen -- see
        docs/PI_MODEL_FALLBACK_DESIGN.md. Originally motivated by pi's local
        model having only a single inference slot (an agent queued behind
        another sits frozen for however long that takes), but the mechanism
        itself is CLI-agnostic: this method never checks agent.cli_type
        directly. Whether/how a CLI supports an in-session model switch is
        entirely polymorphic, via
        CLIAgentInterface.model_fallback_keystrokes (cli_interface.py) --
        empty means "not supported for this CLI," so this stays a no-op for
        any CLI that hasn't opted in (today, only PiAgent has).

        The generic frozen-output path above (_mechanical_recovery_for_agent)
        doesn't help here: the agent isn't stuck, it's waiting its turn, and
        a nudge does nothing to speed that up (worst case it looks like a
        new request and pushes it further back). This runs after that check
        and reuses its own frozen-duration tracking (self._stuck_state)
        rather than a second, independent signature comparison -- it already
        computes and updates that state for every agent, every cycle.

        Opt-in via fallback_model(config, is_primary) -- resolved by ROLE,
        not by which CLI product this is (config.cli_model_fallback for
        whichever CLI is currently primary, config.secondary_cli_model_fallback
        for whichever is the secondary/fallback tier) -- unset disables this
        for that role, and so does the fallback happening to equal the
        model the agent is already on (a same-model switch is a no-op that
        would still interrupt the agent, and unlike a genuine switch it
        leaves no persisted trace to prevent re-firing on every restart).
        Only for agents still on their CLI's own baseline default model --
        one already running something else (including a prior switch here,
        or a deliberate phase-level override) is left alone.

        One-shot per agent (self._switched_to_fallback_model) once the switch
        is confirmed -- a standing decision for the rest of the agent's
        task, not a repeatable nudge. _verify_cli_model_fallback clears this
        agent from that set again if the switch is never confirmed, so a
        failed attempt (e.g. a picker timing miss) doesn't permanently
        forfeit this agent's only chance at recovery. No automatic
        switch-back once confirmed (v1; see the design doc's Open
        Questions).
        """
        try:
            cli_agent = get_cli_agent(agent.cli_type)
            # Whether this agent's cli_type IS the currently-configured
            # primary (config.default_cli_tool) -- reused by both
            # fallback_model (which of the two role-keyed config values to
            # read) and the baseline-default gate below, so that swapping
            # default_cli_tool/default_fallback_cli_tool (e.g. running
            # Claude as primary against a local model, pi as the fallback
            # tier) doesn't silently keep either check pinned to the old
            # role.
            is_primary = agent.cli_type == getattr(self.config, "default_cli_tool", None)
            fallback = cli_agent.fallback_model(self.config, is_primary)
            if not fallback:
                return False
            if fallback == agent.cli_model:
                # Configured fallback is the same model this agent is
                # already on (observed live: secondary_cli_model_fallback
                # left at its shipped default happened to equal the
                # phase's own primary model) -- switching would be a
                # literal no-op that still interrupts the agent, and
                # since neither Agent.cli_model nor the baseline-default
                # gate below change as a result, this is not merely
                # wasteful once -- with no persisted signal that a switch
                # was ever "attempted," it would silently re-fire on
                # every backend restart for as long as the agent stays
                # frozen (the in-memory one-shot set is the only thing
                # that would otherwise prevent a repeat, and it doesn't
                # survive a restart).
                return False
            keystrokes = cli_agent.model_fallback_keystrokes(fallback)
            if not keystrokes:
                return False
            # This CLI's own baseline default -- config.cli_model only
            # applies when this agent IS the primary (see manager.py's
            # identical global_model resolution); a secondary-tier CLI's
            # baseline is its own default_model class attribute. Comparing
            # against the wrong baseline would either never match a
            # non-primary CLI's agents (leaving them permanently
            # ineligible) or match a deliberate phase-level override that
            # isn't actually "stuck on the default" at all.
            default_for_cli = (
                getattr(self.config, "cli_model", None)
                if is_primary
                else cli_agent.default_model
            )
            if agent.cli_model != default_for_cli:
                return False
            if not hasattr(self, "_switched_to_fallback_model"):
                self._switched_to_fallback_model = set()
            if agent.id in self._switched_to_fallback_model:
                return False

            st = getattr(self, "_stuck_state", {}).get(agent.id)
            if not st or st.get("since") is None:
                return False
            wait_seconds = getattr(self.config, "cli_model_fallback_wait_seconds", 120)
            frozen_for = time.time() - st["since"]
            if frozen_for < wait_seconds:
                return False

            # Don't fire into an active connection-error retry loop. The
            # keystroke sequence assumes the agent is idle at a shell
            # prompt ready to accept "/model" -- if it's instead mid-retry
            # on a connection failure, "/model" may not open the picker in
            # the wait window, and the follow-up search text then falls
            # through to the normal chat input, which pi queues as a live
            # "Steering" message rather than picker text (observed live:
            # "mimo-v2.5-pro" sent as Steering, never landing as a model
            # switch). Connection errors are a distinct hard blocker
            # already owned by _detect_connection_errors (which is itself
            # fallback-aware) -- leave this one alone rather than risk
            # misdirecting a busy agent.
            recent_output = self.agent_manager.get_agent_output(agent.id, lines=20) or ""
            if _CONNECTION_ERROR_RE.search(_strip_sgr(recent_output)):
                return False

            self._switched_to_fallback_model.add(agent.id)
            if not hasattr(self, "_fallback_attempt_count"):
                self._fallback_attempt_count = {}
            attempt_num = self._fallback_attempt_count.get(agent.id, 0) + 1
            self._fallback_attempt_count[agent.id] = attempt_num
            original_model = agent.cli_model
            logger.warning(
                f"[CLI-MODEL-FALLBACK] Agent {agent.id[:8]} ({agent.cli_type}) frozen "
                f"{int(frozen_for)}s — switching to fallback model '{fallback}' "
                f"(attempt {attempt_num}/{MAX_FALLBACK_ATTEMPTS})"
            )
            # Persist the switch to Agent.cli_model, not just the in-memory
            # one-shot set -- agent.cli_model is surfaced directly in API
            # responses (mcp/api.py, mcp/autopilot_api.py) for UI display,
            # and get_active_agents() re-fetches a fresh row every cycle, so
            # leaving it stale would show the wrong "current model" for this
            # agent from here on, not just for one cycle.
            try:
                with self.db_manager.session_scope() as session:
                    agent_row = session.query(Agent).filter_by(id=agent.id).first()
                    if agent_row:
                        agent_row.cli_model = fallback
                    self._log_agent_event(
                        agent.id,
                        "cli_model_fallback",
                        f"Frozen {int(frozen_for)}s on '{original_model}' — switched "
                        f"in-place to fallback model '{fallback}'",
                        {
                            "task_id": agent.current_task_id,
                            "from_model": original_model,
                            "to_model": fallback,
                            "frozen_seconds": int(frozen_for),
                        },
                        session=session,
                    )
            except Exception as persist_err:
                logger.warning(
                    f"[CLI-MODEL-FALLBACK] Failed to persist cli_model update "
                    f"for {agent.id[:8]}: {persist_err}"
                )
            try:
                for text, wait_after in keystrokes:
                    await self.agent_manager.send_message_to_agent(agent.id, text)
                    if wait_after:
                        await asyncio.sleep(wait_after)
            except Exception as send_err:
                # The one-shot add() and the optimistic DB write above both
                # already happened before we knew the send would actually go
                # through. Without this handler, a send failure (e.g. the
                # tmux session going away mid-send) would leave the agent
                # permanently blocked by the one-shot gate with no pending
                # entry ever created -- _verify_cli_model_fallback has
                # nothing to check, so the MAX_FALLBACK_ATTEMPTS retry budget
                # this function is supposed to enforce never even gets
                # consulted. Treat it the same as an unconfirmed switch:
                # revert the DB write, and allow a retry only if attempts
                # remain.
                logger.warning(
                    f"[CLI-MODEL-FALLBACK] Failed to send switch keystrokes to "
                    f"{agent.id[:8]}: {send_err}"
                )
                try:
                    with self.db_manager.session_scope() as session:
                        agent_row = session.query(Agent).filter_by(id=agent.id).first()
                        if agent_row:
                            agent_row.cli_model = original_model
                except Exception as revert_err:
                    logger.warning(
                        f"[CLI-MODEL-FALLBACK] Failed to revert cli_model for "
                        f"{agent.id[:8]}: {revert_err}"
                    )
                self._log_agent_event(
                    agent.id, "cli_model_fallback_send_failed",
                    f"Failed to send switch keystrokes for fallback model "
                    f"'{fallback}': {send_err}",
                    {"task_id": agent.current_task_id, "model": fallback, "attempt": attempt_num},
                )
                if attempt_num < MAX_FALLBACK_ATTEMPTS:
                    self._switched_to_fallback_model.discard(agent.id)
                return False
            # Reset the freeze baseline (not the whole _stuck_state entry) so
            # this mechanism doesn't immediately re-read the agent as "still
            # frozen" on the next cycle -- but preserve st["recov"], the
            # generic mechanical-recovery escalation counter that lives in
            # the same dict entry. Popping the entire entry here (as this
            # used to) reset recov back to 0 on every attempt, which is the
            # reason that generic backstop never independently escalated
            # during the incident MAX_FALLBACK_ATTEMPTS was added for.
            stuck_entry = self._stuck_state.get(agent.id)
            if stuck_entry:
                stuck_entry["since"] = None
                stuck_entry["sig"] = None
            if not hasattr(self, "_pending_fallback_verification"):
                self._pending_fallback_verification = {}
            self._pending_fallback_verification[agent.id] = (fallback, original_model, time.time())
            return True
        except Exception as e:
            logger.warning(f"[CLI-MODEL-FALLBACK] check failed for {agent.id[:8]}: {e}")
        return False

    async def _verify_cli_model_fallback(self, agent) -> None:
        """Best-effort follow-up to _detect_cli_model_fallback: on a later
        cycle, check whether the model switch it sent actually landed, per
        CLIAgentInterface.model_fallback_confirmed (polymorphic -- e.g.
        pi's "Model: <provider>/<name>" echo). Not blocking -- surfaces
        whether the CLI interaction didn't land as expected (wrong search
        text, picker didn't open in time, etc.) instead of leaving that
        silent. Logged to AgentLog either way so the outcome is attached to
        the agent/task record, not just process logs.

        An unconfirmed switch also clears the agent from
        _switched_to_fallback_model: the one-shot restriction is meant to
        stop a *successful* switch from being re-sent, not to permanently
        strand an agent that we have direct evidence never actually
        switched -- without this, a single failed picker interaction (e.g.
        a transient timing miss) would forfeit this agent's only chance at
        recovery for the rest of its task, even if it later freezes again
        on the still-unswitched original model.
        """
        pending = getattr(self, "_pending_fallback_verification", {})
        entry = pending.get(agent.id)
        if not entry:
            return
        model, original_model, switched_at = entry
        try:
            confirmed = get_cli_agent(agent.cli_type).model_fallback_confirmed(
                self.agent_manager.get_agent_output(agent.id, lines=40) or "", model
            )
            if confirmed is None:
                # This CLI has no way to confirm -- nothing to verify.
                pending.pop(agent.id, None)
                return
            if confirmed:
                logger.info(
                    f"[CLI-MODEL-FALLBACK] Agent {agent.id[:8]} confirmed on "
                    f"fallback model '{model}'"
                )
                self._log_agent_event(
                    agent.id, "cli_model_fallback_confirmed",
                    f"Confirmed running on fallback model '{model}'",
                    {"task_id": agent.current_task_id, "model": model},
                )
                pending.pop(agent.id, None)
                return
            grace_seconds = 2 * getattr(self.config, "monitoring_interval_seconds", 60)
            if time.time() - switched_at >= grace_seconds:
                attempt_count = getattr(self, "_fallback_attempt_count", {}).get(agent.id, 1)
                gave_up = attempt_count >= MAX_FALLBACK_ATTEMPTS
                logger.warning(
                    f"[CLI-MODEL-FALLBACK] Agent {agent.id[:8]} switch to "
                    f"'{model}' not confirmed after "
                    f"{int(time.time() - switched_at)}s — the CLI interaction "
                    "may not have landed as expected"
                    + (" (attempts exhausted, giving up)" if gave_up else "")
                )
                self._log_agent_event(
                    agent.id,
                    "cli_model_fallback_abandoned" if gave_up else "cli_model_fallback_unconfirmed",
                    f"Switch to fallback model '{model}' not confirmed after "
                    f"{int(time.time() - switched_at)}s -- reverting recorded "
                    f"cli_model to '{original_model}'"
                    + (
                        f" -- {attempt_count}/{MAX_FALLBACK_ATTEMPTS} attempts used, not retrying again"
                        if gave_up
                        else ""
                    ),
                    {"task_id": agent.current_task_id, "model": model, "attempt": attempt_count},
                )
                # Revert the optimistic DB write from _detect_cli_model_fallback
                # -- otherwise its own gate (agent.cli_model != config.cli_model)
                # would see this agent as already switched and block the retry
                # this branch just re-enabled, even though the switch never
                # actually confirmed.
                try:
                    with self.db_manager.session_scope() as session:
                        agent_row = session.query(Agent).filter_by(id=agent.id).first()
                        if agent_row:
                            agent_row.cli_model = original_model
                except Exception as revert_err:
                    logger.warning(
                        f"[CLI-MODEL-FALLBACK] Failed to revert cli_model for "
                        f"{agent.id[:8]}: {revert_err}"
                    )
                pending.pop(agent.id, None)
                # Below MAX_FALLBACK_ATTEMPTS: discard from the one-shot set so
                # _detect_cli_model_fallback can try again next time this agent
                # freezes long enough. At/past the cap: leave it in the set --
                # permanently blocks further attempts for this agent's task,
                # rather than retrying an interaction that keeps failing to
                # confirm indefinitely (see MAX_FALLBACK_ATTEMPTS).
                if not gave_up:
                    getattr(self, "_switched_to_fallback_model", set()).discard(agent.id)
        except Exception as e:
            logger.warning(f"[CLI-MODEL-FALLBACK] verify failed for {agent.id[:8]}: {e}")
            pending.pop(agent.id, None)

    def _log_agent_event(self, agent_id: str, log_type: str, message: str, details: dict, session=None) -> None:
        """Persist an AgentLog entry for a monitor-driven intervention --
        keeps a queryable record on the agent/task of why something
        happened (e.g. a model switch or termination) that outlives the
        transient state fields it may have briefly touched (like
        Task.failure_reason, which the session-limit fallback path clears
        again once it re-dispatches). Best-effort: a logging failure must
        never block the intervention itself.

        session: pass the caller's already-open session (e.g. the
        session-limit block already holds one) to add to it directly
        instead of opening a second, nested session_scope() -- avoids any
        question of whether two sessions writing to the same sqlite file
        at once is safe. Only opens its own when called standalone."""
        entry = AgentLog(agent_id=agent_id, log_type=log_type, message=message, details=details)
        if session is not None:
            session.add(entry)
            return
        try:
            with self.db_manager.session_scope() as new_session:
                new_session.add(entry)
        except Exception as e:
            logger.warning(f"Failed to write AgentLog ({log_type}) for {agent_id[:8]}: {e}")

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
                get_monitor_nudge(
                    "thought_loop",
                    top_line=repr(top_line[:60]),
                    top_count=top_count,
                ),
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
                get_monitor_nudge("dangerous_rm_denied"),
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
                get_monitor_nudge("max_token_limit_immediate"),
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
                get_monitor_nudge(
                    "mcp_disconnected",
                    instructions=instructions,
                    current_task_id=agent.current_task_id or "unknown -- call get_my_tasks first",
                ),
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

            reason_text = (
                f"{error_count} persistent connection error(s) detected -- "
                "the LLM/inference endpoint may be unreachable"
            )

            # A connection error means the CURRENT endpoint (e.g. a local
            # inference host) is unreachable, not that the agent did
            # anything wrong -- redispatching onto the SAME model/endpoint
            # guarantees the identical failure. Mirrors the session-limit
            # path: try the phase's (or global) configured
            # fallback_cli_tool/fallback_cli_model via a fresh kill+restart
            # dispatch first; only mark failed (see below, still routes
            # through _advance_phases's retry cap -- see the comment that
            # used to be here) if no fallback is configured or the fallback
            # dispatch itself fails. Observed live: a task retried 46+ times
            # over 5+ hours against a dead local inference host, always onto
            # the same broken endpoint, because nothing here ever tried the
            # phase's already-configured fallback_cli_tool: claude.
            #
            # terminate_agent() is called AFTER the task's status is
            # updated and committed below, not before -- mirroring the
            # session-limit path exactly. Observed live: calling
            # terminate_agent() first left a window where Agent.status was
            # already "terminated" but Task.status was still "in_progress"
            # (pointing at that now-dead agent) -- a separate, unrelated
            # periodic sweep (attempt_recovery's stale-assigned-task
            # cleanup) can see exactly that combination and mark the task
            # failed with a generic "terminated unexpectedly" reason before
            # this function's own session ever gets to it, silently
            # skipping the fallback dispatch entirely.
            with self.db_manager.session_scope() as session:
                from src.core.database import Phase as _Phase
                from src.core.database import Task as _Task

                stuck_task = (
                    session.query(_Task)
                    .filter_by(assigned_agent_id=agent.id)
                    .filter(_Task.status.in_(["assigned", "in_progress"]))
                    .first()
                )
                if not stuck_task:
                    await self.agent_manager.terminate_agent(agent.id)
                    self._stuck_state.pop(agent.id, None)
                    return True

                fallback_tool = None
                fallback_model = None
                if stuck_task.phase_id:
                    phase = session.query(_Phase).filter_by(id=stuck_task.phase_id).first()
                    if phase:
                        fallback_tool = getattr(phase, "fallback_cli_tool", None)
                        fallback_model = getattr(phase, "fallback_cli_model", None)
                if not fallback_tool:
                    cfg = get_config()
                    if cfg.default_fallback_cli_tool and cfg.default_fallback_cli_tool != agent.cli_type:
                        fallback_tool = cfg.default_fallback_cli_tool
                        fallback_model = cfg.default_fallback_cli_model

                if fallback_tool and fallback_tool != agent.cli_type:
                    logger.warning(
                        f"[CONNECTION-ERROR] Re-dispatching with fallback: "
                        f"{fallback_tool}/{fallback_model or 'default'}"
                    )
                    stuck_task.status = "pending"
                    stuck_task.assigned_agent_id = None
                    stuck_task.failure_reason = None
                    self._log_agent_event(
                        agent.id, "connection_error_terminated",
                        f"{reason_text} — terminated and redispatched to "
                        f"{fallback_tool}/{fallback_model or 'default'}",
                        {
                            "task_id": stuck_task.id,
                            "from_cli_type": agent.cli_type,
                            "fallback_cli_type": fallback_tool,
                            "fallback_cli_model": fallback_model,
                        },
                        session=session,
                    )
                    session.commit()
                    await self.agent_manager.terminate_agent(agent.id)
                    self._stuck_state.pop(agent.id, None)
                    try:
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
                            f"[CONNECTION-ERROR] Fallback agent {new_agent.id[:8]} "
                            f"created for task {stuck_task.id[:8]}"
                        )
                        # Same as the session-limit path: clear a stale
                        # system-pause left by an earlier no-fallback event
                        # now that a fallback dispatch has actually succeeded.
                        if stuck_task.workflow_id:
                            _wf = (
                                session.query(Workflow)
                                .filter_by(id=stuck_task.workflow_id)
                                .first()
                            )
                            if _wf and _wf.status == "paused" and _wf.paused_by == "system":
                                _wf.status = "active"
                                _wf.paused_by = None
                                _wf.status_reason = None
                                _wf.paused_at = None
                                logger.info(
                                    f"[CONNECTION-ERROR] Cleared stale pause on "
                                    f"workflow {stuck_task.workflow_id[:8]}"
                                )
                    except Exception as fallback_err:
                        logger.error(
                            f"[CONNECTION-ERROR] Fallback agent creation failed: "
                            f"{fallback_err}"
                        )
                        stuck_task.status = "failed"
                        stuck_task.failure_reason = (
                            f"CLI connection errors: {reason_text}; fallback "
                            f"({fallback_tool}) also failed: {fallback_err}"
                        )
                        self._log_agent_event(
                            agent.id, "connection_error_terminated",
                            f"{reason_text} — terminated, fallback "
                            f"({fallback_tool}) creation also failed: {fallback_err}",
                            {
                                "task_id": stuck_task.id,
                                "from_cli_type": agent.cli_type,
                                "fallback_cli_type": fallback_tool,
                            },
                            session=session,
                        )
                        session.commit()
                else:
                    # No fallback configured for this phase or globally --
                    # mark failed with a real reason (not a silent reset to
                    # pending) so _advance_phases's retry cap
                    # (max_retry_count=2) actually applies instead of the
                    # task getting relabeled "Orphaned" by an unrelated
                    # stale-pending check and exempted from that cap.
                    stuck_task.status = "failed"
                    stuck_task.assigned_agent_id = None
                    stuck_task.failure_reason = f"CLI connection errors: {reason_text}"
                    self._log_agent_event(
                        agent.id, "connection_error_terminated",
                        f"{reason_text} — terminated, no fallback configured",
                        {"task_id": stuck_task.id, "from_cli_type": agent.cli_type},
                        session=session,
                    )
                    logger.info(f"[CONNECTION-ERROR] Task {stuck_task.id[:8]} marked failed: {stuck_task.failure_reason}")
                    session.commit()
                    await self.agent_manager.terminate_agent(agent.id)
                    self._stuck_state.pop(agent.id, None)
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

            # config.cli_model is paired with agents.default_cli_tool (pi)
            # and is typically an OpenRouter path for pi's picker, not one
            # of Claude Code's own model aliases -- sending it to Claude via
            # /model would be nonsensical to it. secondary_cli_model_fallback is
            # Claude's own configured recovery target instead.
            fix_model = getattr(self.config, "secondary_cli_model_fallback", None) or "sonnet"
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
        #   h) MCP server disconnected — nudge to `mcp connect`, don't wait for (c)
        #   i) Claude Code rejected its launch model — fix directly with a
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
            if await self._detect_cli_model_fallback(agent):
                mechanically_intervened.add(agent.id)
            await self._verify_cli_model_fallback(agent)
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
