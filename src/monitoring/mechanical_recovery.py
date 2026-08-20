"""Mechanical/heuristic detectors for stuck-agent detection (no LLM).

Extracted from MonitoringLoop: cluster B — 13 complementary detector methods
run sequentially in _monitoring_cycle's Phase 0 block, plus the shared
_log_agent_event helper. These detectors maintain per-agent state dicts
(previously lazily created on MonitoringLoop, now __init__-declared
attributes on this class) and share _stuck_state across 5+ methods.

See docs/SOLID_OO_REVIEW.md and design_docs/phase_1b_decomposition.md §4.3.
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict

from src.core.database import Agent, AgentLog, Task, Workflow
from src.core.simple_config import get_config
from src.interfaces import get_cli_agent
from src.prompts.loader import get_monitor_nudge

logger = logging.getLogger(__name__)

# Statuses a task can hold while a live agent is still meaningfully tied to
# it -- used throughout this file's detectors to find "the task this stuck/
# frozen/disconnected agent is holding" so it can be failed/reset/reassigned.
# Includes under_review/needs_work, not just assigned/in_progress: an agent
# kept alive for validation (its own status stays "working"/"idle" while its
# task flips to under_review, or to needs_work once a validator sends
# feedback back to it) still holds that task via assigned_agent_id. Without
# these two, any detector below that then fires on that same agent (session
# limit, context overflow, frozen timeout, MCP disconnect, dispatch-failure
# fallback, never-started) can't find its task, so the task is left
# permanently pointing at a terminated agent with no other sweep watching
# this exact combination.
STUCK_TASK_STATUSES = ["assigned", "in_progress", "under_review", "needs_work"]

# Regex patterns and constants from monitor.py module level.
# Imported lazily (inside methods) to avoid circular import at module load,
# since monitor.py will import MechanicalRecoveryDetector inside __init__.
def _get_monitor_module():
    """Lazy reference to monitor module for shared regex/constants."""
    import src.monitoring.monitor as _mod
    return _mod


def _strip_sgr(text: str) -> str:
    """Strip SGR color escape codes (\\\\x1b[...m)."""
    return _get_monitor_module()._SGR_RE.sub("", text)


def _get_regex(name: str):
    """Retrieve a regex constant from the monitor module by name."""
    return getattr(_get_monitor_module(), name)


def _get_constant(name: str):
    """Retrieve a numeric constant from the monitor module by name."""
    return getattr(_get_monitor_module(), name)


class MechanicalRecoveryDetector:
    """Cheap, no-LLM stuck detection + keystroke recovery."""

    UNCONFIRMED_COMPLETION_ESCALATE_AFTER = 3
    NEVER_STARTED_GRACE_SECONDS = 240

    def __init__(self, db_manager, agent_manager, config, auto_restart):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.config = config
        self._auto_restart = auto_restart

        # Shared stuck-output tracking (frozen-duration detection).
        self._stuck_state: Dict[str, Any] = {}

        # Per-detector state — now __init__-declared attributes
        self._switched_to_fallback_model: set = set()
        self._fallback_attempt_count: Dict[str, int] = {}
        self._pending_fallback_verification: Dict[str, tuple] = {}
        self._rep_loop_state: Dict[str, str] = {}
        self._denied_dangerous_cmds: Dict[str, float] = {}
        self._nudged_token_limit: Dict[str, float] = {}
        self._nudged_unconfirmed_completion: Dict[str, float] = {}
        self._unconfirmed_completion_state: Dict[str, dict] = {}
        self._nudged_mcp_disconnected: Dict[str, float] = {}
        self._mcp_disconnect_nudge_count: Dict[str, int] = {}
        self._connection_error_warned: Dict[str, float] = {}
        self._fixed_bad_model: set = set()
        self._paused_credit_exhausted: set = set()
        self._never_started_handled: set = set()

    async def mechanical_recovery_for_agent(self, agent) -> bool:
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
            # Read directly from tmux pane for real-time stuck detection.
            # get_agent_output reads from the stability-tracked clean transcript
            # which withholds output until lines stabilize -- an agent actively
            # streaming but stuck in a loop shows no output there, defeating
            # the frozen-signature comparison entirely.
            out = None
            raw_text = ""
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
                            out = raw_text
                finally:
                    session.close()
            except Exception as _pane_err:
                logger.debug(f"Pane capture for stuck check failed: {_pane_err}")

            # Fallback to get_agent_output if direct capture failed
            if not out:
                out = self.agent_manager.get_agent_output(agent.id, lines=40)
            if not out:
                return
            # Spend/session limit check using the already-captured pane output.
            # The interactive menu ("Stop and wait for limit to reset") only
            # appears in the live pane, not in the transcript log.
            stripped_raw = _strip_sgr(raw_text)
            if stripped_raw:
                spend_limit_hit = _get_regex('_SPEND_LIMIT_RE').search(stripped_raw)
                if spend_limit_hit or _get_regex('_SESSION_LIMIT_RE').search(stripped_raw):
                    # Determine the specific limit kind for accurate logging
                    if spend_limit_hit:
                        matched_text = spend_limit_hit.group(0).lower()
                        if "weekly" in matched_text:
                            limit_kind = "weekly spend limit"
                        else:
                            limit_kind = "monthly spend limit"
                    else:
                        limit_kind = "session limit"
                    logger.warning(
                        f"[SESSION-LIMIT] Agent {agent.id[:8]} ({agent.cli_type}) hit {limit_kind} — "
                        f"terminating immediately (not recoverable)"
                    )
                    with self.db_manager.session_scope() as session:
                        from src.core.database import Phase as _Phase

                        stuck_task = (
                            session.query(Task)
                            .filter_by(assigned_agent_id=agent.id)
                            .filter(Task.status.in_(STUCK_TASK_STATUSES))
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
                                phase = session.query(_Phase).filter_by(id=stuck_task.phase_id).first()
                                if phase:
                                    fallback_tool = getattr(phase, "fallback_cli_tool", None)
                                    fallback_model = getattr(phase, "fallback_cli_model", None)

                            cfg = get_config()

                            # Fall back to global config defaults
                            if not fallback_tool:
                                if cfg.default_fallback_cli_tool and (cfg.default_fallback_cli_tool != agent.cli_type or cfg.default_fallback_cli_model != agent.cli_model):
                                    fallback_tool = cfg.default_fallback_cli_tool
                                    fallback_model = cfg.default_fallback_cli_model

                            # Last resort: default_fallback_cli_tool/_model can
                            # resolve to the exact same cli+model that just hit
                            # the limit (e.g. default_cli_tool and
                            # default_fallback_cli_tool both "pi" on the same
                            # model) -- nothing to actually switch to via that
                            # pair. secondary_cli_model_fallback is normally
                            # reserved for a non-primary cli_type via the
                            # role-based lookup in CLIAgentInterface.fallback_model,
                            # so it's unreachable through THAT path when the
                            # stuck agent's cli_type IS the primary (the common
                            # case here -- every phase agent runs as "pi") --
                            # but it's still a real, different MODEL on the
                            # same CLI harness (pi understands "sonnet" as a
                            # model string), worth trying before giving up and
                            # pausing the whole workflow. Observed live: this
                            # exact case paused a workflow with a viable
                            # secondary_cli_model_fallback configured and
                            # never consulted.
                            if (
                                not fallback_tool
                                or (fallback_tool == agent.cli_type and fallback_model == agent.cli_model)
                            ) and cfg.secondary_cli_model_fallback and cfg.secondary_cli_model_fallback != agent.cli_model:
                                fallback_tool = agent.cli_type
                                fallback_model = cfg.secondary_cli_model_fallback

                            if fallback_tool and (fallback_tool != agent.cli_type or fallback_model != agent.cli_model):
                                logger.warning(
                                    f"[SESSION-LIMIT] Re-dispatching with fallback: "
                                    f"{fallback_tool}/{fallback_model or 'default'}"
                                )
                                self.log_agent_event(
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
                                    if stuck_task.workflow_id:
                                        from src.autopilot.orchestrator.engine_client import resume_workflow
                                        resume_workflow(stuck_task.workflow_id, session=session)
                                except Exception as fallback_err:
                                    logger.error(f"[SESSION-LIMIT] Fallback agent creation failed: {fallback_err}")
                                    stuck_task.status = "failed"
                                    stuck_task.failure_reason = f"Primary hit {limit_kind}, fallback also failed: {fallback_err}"
                                    self.log_agent_event(
                                        agent.id, "session_limit_terminated",
                                        f"Hit {limit_kind} ({agent.cli_type}) — terminated, fallback ({fallback_tool}) also failed: {fallback_err}",
                                        {"task_id": stuck_task.id, "limit_kind": limit_kind, "from_cli_type": agent.cli_type, "fallback_cli_type": fallback_tool},
                                        session=session,
                                    )
                                    session.commit()
                                return True
                            elif stuck_task.workflow_id:
                                workflow = session.query(Workflow).filter_by(id=stuck_task.workflow_id).first()
                                if workflow and workflow.status != "paused":
                                    from src.autopilot.orchestrator.engine_client import pause_workflow
                                    pause_workflow(
                                        stuck_task.workflow_id,
                                        reason="system",
                                        status_reason=f"CLI {limit_kind} hit ({agent.cli_type}), no fallback configured",
                                        session=session,
                                    )
                        if stuck_task:
                            self.log_agent_event(
                                agent.id, "session_limit_terminated",
                                f"Hit {limit_kind} ({agent.cli_type}) — terminated, no fallback configured",
                                {"task_id": stuck_task.id, "limit_kind": limit_kind, "from_cli_type": agent.cli_type},
                                session=session,
                            )
                            await self.agent_manager.terminate_agent(agent.id)
                            self._stuck_state.pop(agent.id, None)
                        return True

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

            # Context overflow: local model hit its context size limit.
            # This is a hard blocker — the agent can't continue with the
            # current model. Terminate and restart with fresh context on
            # the fallback model rather than switching in-session (which
            # would inherit the bloated context and degrade performance).
            if _get_regex('_CONTEXT_OVERFLOW_RE').search(sig):
                logger.warning(
                    f"[CONTEXT-OVERFLOW] Agent {agent.id[:8]} ({agent.cli_type}) "
                    f"hit context size limit — terminating for fresh restart"
                )
                with self.db_manager.session_scope() as session:
                    from src.core.database import Phase as _Phase

                    stuck_task = (
                        session.query(Task)
                        .filter_by(assigned_agent_id=agent.id)
                        .filter(Task.status.in_(STUCK_TASK_STATUSES))
                        .first()
                    )
                    if stuck_task:
                        # Resolve fallback model
                        fallback_tool = None
                        fallback_model = None
                        if stuck_task.phase_id:
                            phase = session.query(_Phase).filter_by(id=stuck_task.phase_id).first()
                            if phase:
                                fallback_tool = getattr(phase, "fallback_cli_tool", None)
                                fallback_model = getattr(phase, "fallback_cli_model", None)
                        if not fallback_tool:
                            cfg = get_config()
                            if cfg.default_fallback_cli_tool and (cfg.default_fallback_cli_tool != agent.cli_type or cfg.default_fallback_cli_model != agent.cli_model):
                                fallback_tool = cfg.default_fallback_cli_tool
                                fallback_model = cfg.default_fallback_cli_model

                        if fallback_tool and fallback_tool != agent.cli_type:
                            logger.info(
                                f"[CONTEXT-OVERFLOW] Restarting with {fallback_tool}/{fallback_model or 'default'}"
                            )
                            stuck_task.status = "pending"
                            stuck_task.assigned_agent_id = None
                            stuck_task.failure_reason = None
                            session.commit()

                            await self.agent_manager.terminate_agent(agent.id)
                            self._stuck_state.pop(agent.id, None)

                            new_agent = await self.agent_manager.create_agent_for_task(
                                task=stuck_task,
                                enriched_data={},
                                memories=[],
                                project_context="",
                                cli_type=fallback_tool,
                                phase_cli_tool=fallback_tool,
                                phase_cli_model=fallback_model,
                            )
                            self.log_agent_event(
                                agent.id, "context_overflow_terminated",
                                f"Context overflow ({agent.cli_model}) — terminated and restarted "
                                f"with {fallback_tool}/{fallback_model or 'default'} (fresh context)",
                                {"task_id": stuck_task.id, "from_model": agent.cli_model, "fallback": fallback_tool},
                                session=session,
                            )
                            return True
                        else:
                            logger.warning(f"[CONTEXT-OVERFLOW] No fallback available for {agent.id[:8]}")

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

                        # Offloaded -- subprocess.run blocks the event loop
                        # for however long tmux takes to respond, same
                        # class of issue fixed elsewhere in this codebase
                        # today.
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None, _sp.run,
                            ["tmux", "send-keys", "-t", session_name, "Escape", ""],
                        )
                        await asyncio.sleep(0.5)
                        await loop.run_in_executor(
                            None, _sp.run,
                            ["tmux", "send-keys", "-t", session_name, "/mcp", "Enter"],
                        )
                        await asyncio.sleep(2.0)
                        await loop.run_in_executor(
                            None, _sp.run,
                            ["tmux", "send-keys", "-t", session_name, "C-r", ""],
                        )
                        await asyncio.sleep(3.0)
                        await loop.run_in_executor(
                            None, _sp.run,
                            ["tmux", "send-keys", "-t", session_name, "Escape", ""],
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
                    elif _get_regex('_MAX_TOKEN_LIMIT_RE').search(_strip_sgr(sig)):
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
                        .filter(_Task.status.in_(STUCK_TASK_STATUSES))
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


    async def detect_cli_model_fallback(self, agent) -> bool:
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

            # Restart-proof floor: _switched_to_fallback_model/_fallback_attempt_count
            # are in-memory only, so a routine `heph restart` used to reset an
            # agent's exhausted _get_constant('MAX_FALLBACK_ATTEMPTS') budget back to zero --
            # observed live, agent e6633fe6 got two full fresh 2-attempt
            # episodes (18:13-18:21, then again 19:07-19:18 after a restart
            # in between), doubling the disruptive switch attempts and, worse,
            # each attempt risks landing as an unconsumed queued "Steering"
            # message that jams the agent's input indefinitely. AgentLog is
            # the durable record of every attempt this mechanism has made;
            # reconstruct prior_attempts/gave_up from it so the cap survives
            # a restart the same way the one-shot set does within a process.
            prior_attempts = 0
            gave_up = False
            try:
                with self.db_manager.session_scope() as session:
                    log_types = [
                        row[0]
                        for row in session.query(AgentLog.log_type)
                        .filter(
                            AgentLog.agent_id == agent.id,
                            AgentLog.log_type.in_(
                                ["cli_model_fallback", "cli_model_fallback_abandoned"]
                            ),
                        )
                        .all()
                    ]
                prior_attempts = log_types.count("cli_model_fallback")
                gave_up = "cli_model_fallback_abandoned" in log_types
            except Exception as e:
                logger.warning(
                    f"[CLI-MODEL-FALLBACK] failed to read prior attempt history "
                    f"for {agent.id[:8]}: {e}"
                )
            # Take whichever count is higher -- DB-derived normally leads
            # (this same function writes the log entry right after
            # incrementing the in-memory counter), but the in-memory value
            # can briefly be higher within one process (e.g. a send failure
            # on this very attempt hasn't been logged yet).
            prior_attempts = max(
                prior_attempts, getattr(self, "_fallback_attempt_count", {}).get(agent.id, 0)
            )
            if gave_up or prior_attempts >= _get_constant('MAX_FALLBACK_ATTEMPTS'):
                self._switched_to_fallback_model.add(agent.id)
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
            if _get_regex('_CONNECTION_ERROR_RE').search(_strip_sgr(recent_output)):
                return False

            self._switched_to_fallback_model.add(agent.id)
            if not hasattr(self, "_fallback_attempt_count"):
                self._fallback_attempt_count = {}
            # Seeded from the DB-derived prior_attempts above, not the
            # in-memory dict alone -- see the restart-proofing note above.
            attempt_num = prior_attempts + 1
            self._fallback_attempt_count[agent.id] = attempt_num
            original_model = agent.cli_model
            logger.warning(
                f"[CLI-MODEL-FALLBACK] Agent {agent.id[:8]} ({agent.cli_type}) frozen "
                f"{int(frozen_for)}s — switching to fallback model '{fallback}' "
                f"(attempt {attempt_num}/{_get_constant('MAX_FALLBACK_ATTEMPTS')})"
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
                    self.log_agent_event(
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
                # nothing to check, so the _get_constant('MAX_FALLBACK_ATTEMPTS') retry budget
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
                self.log_agent_event(
                    agent.id, "cli_model_fallback_send_failed",
                    f"Failed to send switch keystrokes for fallback model "
                    f"'{fallback}': {send_err}",
                    {"task_id": agent.current_task_id, "model": fallback, "attempt": attempt_num},
                )
                if attempt_num < _get_constant('MAX_FALLBACK_ATTEMPTS'):
                    self._switched_to_fallback_model.discard(agent.id)
                return False
            # Reset the freeze baseline (not the whole _stuck_state entry) so
            # this mechanism doesn't immediately re-read the agent as "still
            # frozen" on the next cycle -- but preserve st["recov"], the
            # generic mechanical-recovery escalation counter that lives in
            # the same dict entry. Popping the entire entry here (as this
            # used to) reset recov back to 0 on every attempt, which is the
            # reason that generic backstop never independently escalated
            # during the incident _get_constant('MAX_FALLBACK_ATTEMPTS') was added for.
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


    async def verify_cli_model_fallback(self, agent) -> None:
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
                self.log_agent_event(
                    agent.id, "cli_model_fallback_confirmed",
                    f"Confirmed running on fallback model '{model}'",
                    {"task_id": agent.current_task_id, "model": model},
                )
                pending.pop(agent.id, None)
                return
            grace_seconds = 2 * getattr(self.config, "monitoring_interval_seconds", 60)
            if time.time() - switched_at >= grace_seconds:
                attempt_count = getattr(self, "_fallback_attempt_count", {}).get(agent.id, 1)
                gave_up = attempt_count >= _get_constant('MAX_FALLBACK_ATTEMPTS')
                logger.warning(
                    f"[CLI-MODEL-FALLBACK] Agent {agent.id[:8]} switch to "
                    f"'{model}' not confirmed after "
                    f"{int(time.time() - switched_at)}s — the CLI interaction "
                    "may not have landed as expected"
                    + (" (attempts exhausted, giving up)" if gave_up else "")
                )
                self.log_agent_event(
                    agent.id,
                    "cli_model_fallback_abandoned" if gave_up else "cli_model_fallback_unconfirmed",
                    f"Switch to fallback model '{model}' not confirmed after "
                    f"{int(time.time() - switched_at)}s -- reverting recorded "
                    f"cli_model to '{original_model}'"
                    + (
                        f" -- {attempt_count}/{_get_constant('MAX_FALLBACK_ATTEMPTS')} attempts used, not retrying again"
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
                # Below _get_constant('MAX_FALLBACK_ATTEMPTS'): discard from the one-shot set so
                # _detect_cli_model_fallback can try again next time this agent
                # freezes long enough. At/past the cap: leave it in the set --
                # permanently blocks further attempts for this agent's task,
                # rather than retrying an interaction that keeps failing to
                # confirm indefinitely (see _get_constant('MAX_FALLBACK_ATTEMPTS')).
                if not gave_up:
                    getattr(self, "_switched_to_fallback_model", set()).discard(agent.id)
        except Exception as e:
            logger.warning(f"[CLI-MODEL-FALLBACK] verify failed for {agent.id[:8]}: {e}")
            pending.pop(agent.id, None)


    def log_agent_event(self, agent_id: str, log_type: str, message: str, details: dict, session=None) -> None:
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


    async def detect_repetition_loop(self, agent) -> bool:
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


    async def detect_dangerous_command_confirmation(self, agent) -> bool:
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
            match = _get_regex('_DANGEROUS_CMD_RE').search(_strip_sgr(out))
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


    async def detect_max_token_limit_error(self, agent) -> bool:
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
            if not _get_regex('_MAX_TOKEN_LIMIT_RE').search(_strip_sgr(out)):
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


    async def detect_unconfirmed_task_completion(self, agent) -> bool:
        """Detect an agent whose own transcript shows a complete_my_task/
        update_task_status call for a terminal status, while its assigned
        task is still sitting non-terminal on the server -- the call never
        actually landed.

        Observed live: an agent's Write() and complete_my_task calls both
        rendered as succeeded in its tmux transcript, but a `heph restart`
        landed mid-turn and killed the session before either actually
        reached disk/the server -- the task sat "in_progress" forever with
        no error ever shown to the agent, since from its own point of view
        nothing looked wrong. The generic frozen-output detector eventually
        catches the resulting idle agent too, but only after 5+ minutes and
        with a generic "you appear stuck" nudge -- this fires immediately
        with a nudge that names the exact task_id and asks for a retry,
        the same way _detect_mcp_disconnected does for a visibly-dropped
        connection. This covers the case where nothing about the MCP
        connection looked broken to the agent at all.

        Escalates to a full agent restart after
        UNCONFIRMED_COMPLETION_ESCALATE_AFTER consecutive nudges for the
        same task -- if the transport is persistently broken rather than a
        one-off blip, no amount of re-nudging the same broken connection
        helps.
        """
        if agent.status != "working" or not agent.current_task_id:
            return False
        try:
            if not hasattr(self, "_nudged_unconfirmed_completion"):
                self._nudged_unconfirmed_completion = {}
            if not hasattr(self, "_unconfirmed_completion_state"):
                # agent_id -> {"task_id": str, "count": int} -- tracks
                # consecutive nudges for the CURRENT task specifically, so
                # a later task's unrelated occurrence starts fresh instead
                # of inheriting an old count.
                self._unconfirmed_completion_state = {}

            out = self.agent_manager.get_agent_output(agent.id, lines=60)
            if not out:
                return False
            # Only the LAST attempt in the visible window matters -- an
            # earlier one followed by more work (e.g. a self-review
            # round) means the agent has already moved on.
            match = None
            for m in _get_regex('_COMPLETION_ATTEMPT_RE').finditer(_strip_sgr(out)):
                match = m
            if not match:
                return False

            with self.db_manager.session_scope() as session:
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if not task or task.status not in ("in_progress", "assigned"):
                    # Already terminal, under_review (validation spawned),
                    # etc. -- the call landed, or the task moved on for an
                    # unrelated reason. Nothing to do.
                    return False
                if task.self_review_started_at is not None:
                    # The call DID land -- it correctly triggered the
                    # self-review gate (update_task_status's first "done"
                    # for a self_review-enabled phase deliberately leaves
                    # status as "in_progress" and sends the agent a
                    # checklist, expecting a second "done" call). That
                    # message already tells the agent what to do; a nudge
                    # here would be redundant and, worse, actively
                    # misleading -- it'd claim a connection problem that
                    # never happened.
                    return False
                task_id = task.id

            # Cooldown, not a permanent one-shot flag -- same reasoning as
            # the other mechanical detectors: if the retry also fails to
            # land, keep nudging (up to the escalation threshold below)
            # rather than going silent after the first attempt.
            last_nudged = self._nudged_unconfirmed_completion.get(agent.id)
            if last_nudged is not None and time.time() - last_nudged < 60:
                return False
            self._nudged_unconfirmed_completion[agent.id] = time.time()

            state = self._unconfirmed_completion_state.get(agent.id)
            if not state or state["task_id"] != task_id:
                state = {"task_id": task_id, "count": 0}
                self._unconfirmed_completion_state[agent.id] = state
            state["count"] += 1

            if state["count"] > self.UNCONFIRMED_COMPLETION_ESCALATE_AFTER:
                logger.warning(
                    f"[UNCONFIRMED-COMPLETION] Agent {agent.id[:8]} still hasn't "
                    f"confirmed completion of task {task_id[:8]} after "
                    f"{state['count'] - 1} nudges — restarting instead of nudging again"
                )
                del self._unconfirmed_completion_state[agent.id]
                await self._auto_restart.restart_agent(agent)
                return True

            logger.warning(
                f"[UNCONFIRMED-COMPLETION] Agent {agent.id[:8]} shows a "
                f"complete_my_task/update_task_status attempt but task "
                f"{task_id[:8]} is still non-terminal — nudging to retry "
                f"({state['count']}/{self.UNCONFIRMED_COMPLETION_ESCALATE_AFTER})"
            )
            await self.agent_manager.send_message_to_agent(
                agent.id,
                get_monitor_nudge("task_completion_unconfirmed", task_id=task_id),
            )
            return True
        except Exception as e:
            logger.warning(f"[UNCONFIRMED-COMPLETION] check failed for {agent.id[:8]}: {e}")
        return False


    async def detect_mcp_disconnected(self, agent) -> bool:
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
            if not _get_regex('_MCP_DISCONNECTED_RE').search(_strip_sgr(out)):
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
                        .filter(_Task.status.in_(STUCK_TASK_STATUSES))
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


    async def detect_connection_errors(self, agent) -> bool:
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
            if not _get_regex('_CONNECTION_ERROR_RE').search(stripped):
                return False

            # Check if we've already warned about this agent recently
            if not hasattr(self, "_connection_error_warned"):
                self._connection_error_warned = {}
            last_warned = self._connection_error_warned.get(agent.id)
            if last_warned and time.time() - last_warned < 120:
                return False
            self._connection_error_warned[agent.id] = time.time()

            # Check if the error is persistent (more than 2 occurrences in the output)
            error_count = len(_get_regex('_CONNECTION_ERROR_RE').findall(stripped))
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
                    .filter(_Task.status.in_(STUCK_TASK_STATUSES))
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
                    if cfg.default_fallback_cli_tool and (cfg.default_fallback_cli_tool != agent.cli_type or cfg.default_fallback_cli_model != agent.cli_model):
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
                    self.log_agent_event(
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
                            from src.autopilot.orchestrator.engine_client import resume_workflow
                            if resume_workflow(stuck_task.workflow_id, session=session):
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
                        self.log_agent_event(
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
                    self.log_agent_event(
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


    async def detect_bad_model_error(self, agent) -> bool:
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
            if not _get_regex('_BAD_MODEL_ERROR_RE').search(_strip_sgr(out)):
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


    async def detect_orphaned_idle_agent(self, agent) -> bool:
        """Detect idle agents whose tmux session no longer exists.

        An agent marked 'idle' with no tmux session is orphaned -- the
        session died (backend restart, manual kill, etc.) but the agent
        status wasn't updated. Mark it terminated and fail its task so
        it can be retried.
        """
        if agent.status != "idle":
            return False
        if not agent.tmux_session_name:
            return False
        try:
            session_exists = any(
                s.name == agent.tmux_session_name
                for s in self.agent_manager.tmux_server.sessions
            )
            if not session_exists:
                logger.warning(
                    f"[ORPHAN] Agent {agent.id[:8]} is idle but tmux session "
                    f"'{agent.tmux_session_name}' not found -- terminating"
                )
                with self.db_manager.session_scope() as session:
                    from src.autopilot.orchestrator.engine_client import terminate_agent
                    from src.core.database import Agent, Task
                    db_agent = session.query(Agent).filter_by(id=agent.id).first()
                    if db_agent:
                        # Save task_id before terminate_agent clears it.
                        _task_id = db_agent.current_task_id
                        terminate_agent(agent.id, session=session)
                        # Orphaned-agent-specific: mark the task as failed
                        # (not just pending) so it's visible as an orphan.
                        if _task_id:
                            _task = session.query(Task).filter_by(id=_task_id).first()
                            if _task and _task.status == "pending":
                                _task.status = "failed"
                                _task.failure_reason = "Agent orphaned - tmux session not found"
                return True
        except Exception as e:
            logger.error(f"[ORPHAN] check failed for {agent.id[:8]}: {e}")
        return False


    async def detect_credit_exhausted(self, agent) -> bool:
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
            if not _get_regex('_CREDIT_EXHAUSTED_RE').search(_strip_sgr(out)):
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
                    from src.autopilot.orchestrator.engine_client import pause_workflow
                    pause_workflow(
                        task.workflow_id,
                        reason="system",
                        status_reason=(
                            "OpenRouter credit exhaustion (402) — reload credits at "
                            "openrouter.ai, will auto-resume on its own retry cooldown"
                        ),
                        session=session,
                    )

            await self.agent_manager.terminate_agent(agent.id)
            return True
        except Exception as e:
            logger.warning(f"[CREDIT-EXHAUSTED] check failed for {agent.id[:8]}: {e}")
        return False


    async def detect_agent_never_started(self, agent) -> bool:
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
                    .filter(_Task.status.in_(STUCK_TASK_STATUSES))
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
