"""Agent launch pipeline — worktree resolution, tmux session creation, prompt delivery, and the create/restart orchestrators. Extracted from AgentManager per design_docs/manager_py_decomposition_prompt.md."""

import asyncio
import functools
import logging
import shlex
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import libtmux

from src.agents._create_agent_for_task_steps import (
    _deliver_initial_prompt_flow,
    _insert_stub_agent_row,
    _phase_sibling_guard,
    _prepare_tmux_and_prompt,
    _run_launch_preparations,
    _send_launch_command_and_record_agent,
)
from src.core.constants import AUTOPILOT_STATE_DIR, CONTEXT_DIR_NAME, TMUX_PANE_HEIGHT, TMUX_PANE_WIDTH
from src.core.database import (
    Agent,
    AgentLog,
    BoardConfig,
    Task,
    TaskStatus,
    get_db,
    utc_now,
)
from src.core.phase_lookup import resolve_task_phase
from src.core.worktree_manager import WorktreeManager
from src.interfaces import LaunchResult, get_cli_agent

logger = logging.getLogger(__name__)


class PhaseConfig(NamedTuple):
    """Resolved phase configuration for agent creation."""

    cli_type: str
    phase_cli_tool: Optional[str]
    cli_model: Optional[str]
    glm_token_env: Optional[str]
    thinking_level: Optional[str]
    fallback_cli_tool: Optional[str]
    fallback_cli_model: Optional[str]


class WorktreeResolution(NamedTuple):
    """Result of worktree resolution."""

    branch_path: str
    branch_name: Optional[str]
    context_files: Dict[str, str]


class LaunchPipeline:
    """Agent launch pipeline — worktree resolution, tmux session creation, prompt delivery, and the create/restart orchestrators. Extracted from AgentManager per design_docs/manager_py_decomposition_prompt.md."""

    # Mirrors AgentManager._CLAUDE_CODE_CONFIRMATION_PATTERN (manager.py) --
    # _detect_launch_failure needs it on self, and AgentManager itself no
    # longer owns launch-failure detection after this split.
    _CLAUDE_CODE_CONFIRMATION_PATTERN = r"Bypass Permissions mode"

    def __init__(self, agent_manager):
        self._agent_manager = agent_manager

    @property
    def db_manager(self):
        return self._agent_manager.db_manager

    @property
    def config(self):
        return self._agent_manager.config

    @property
    def branch_manager(self):
        return self._agent_manager.branch_manager

    @property
    def tmux_server(self):
        return self._agent_manager.tmux_server

    @property
    def llm_provider(self):
        return self._agent_manager.llm_provider

    @property
    def phase_manager(self):
        return self._agent_manager.phase_manager

    @property
    def _messenger(self):
        return self._agent_manager._messenger

    @property
    def _prompt_builder(self):
        return self._agent_manager._prompt_builder

    @property
    def _output_capture(self):
        return self._agent_manager._output_capture

    def _build_glm_env_vars(
        self,
        model: str,
        glm_token_env: Optional[str],
        agent_id: str,
        label: str = "agent",
    ) -> Optional[Dict[str, str]]:
        """Build ANTHROPIC_* env vars for GLM models, or None if the model
        isn't GLM or no token is configured.

        Shared by create_agent_for_task and restart_agent — previously
        duplicated independently in each (SOLID review finding 3.2).
        """
        from src.core.utils import is_glm_model

        if not is_glm_model(model):
            return None

        import os

        token_env_var = glm_token_env or getattr(self.config.agents, "glm_api_token_env", "GLM_API_TOKEN")
        token = os.getenv(token_env_var)
        if not token:
            logger.warning(f"GLM model configured but {token_env_var} not found, using standard Claude")
            return None

        logger.info(f"Setting up GLM-4.6 environment variables for {label} {agent_id}")
        return {
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "GLM-4.6",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "GLM-4.6",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "GLM-4.6",
        }

    def _resolve_mcp_timeout_ms(
        self,
        cli_type: str,
        task_workflow_id: Optional[str],
        label: str = "agent",
    ) -> Optional[int]:
        """Resolve MCP_TOOL_TIMEOUT (in ms) for Claude Code agents when the
        target workflow has human-approval ticket review enabled.

        Returns None if not applicable. Shared by create_agent_for_task and
        restart_agent — restart_agent previously resolved this via the
        caller's already-open session rather than a fresh get_db() call
        like create_agent_for_task, a drift this unification fixes (SOLID
        review finding 3.2).
        """
        if cli_type != "claude":
            return None

        try:
            workflow_id = None
            if task_workflow_id:
                workflow_id = task_workflow_id
            elif hasattr(self, "phase_manager") and self.phase_manager and hasattr(self.phase_manager, "workflow_id"):
                workflow_id = self.phase_manager.workflow_id
            else:
                with get_db() as db:
                    board_config = db.query(BoardConfig).filter_by(ticket_human_review=True).first()
                    if board_config:
                        workflow_id = board_config.workflow_id

            if not workflow_id:
                return None

            with get_db() as db:
                board_config = db.query(BoardConfig).filter_by(workflow_id=workflow_id).first()
                if board_config and board_config.ticket_human_review:
                    timeout_seconds = board_config.approval_timeout_seconds or 1800
                    timeout_ms = timeout_seconds * 1000
                    logger.info(f"Human approval enabled for workflow {workflow_id}: Setting MCP_TOOL_TIMEOUT={timeout_ms}ms ({timeout_seconds}s) for {label}")
                    return timeout_ms
        except Exception as e:
            logger.warning(f"Failed to check board config for MCP_TOOL_TIMEOUT ({label}): {e}")
        return None

    def _resolve_project_base_dir(self, workflow_id: Optional[str]) -> Optional[Path]:
        """Resolve workflow_id's project repo path via Workflow.project_id ->
        (Workflow.feature_id -> Feature.repo_id ->) resolve_repo_path. Never
        raises -- returns None on any lookup failure (no workflow_id,
        workflow/project row missing, or no project_id) so callers can fall
        back to today's default-instance behavior instead of erroring.

        WARNING-1 fix: distinguishes "workflow doesn't resolve to a project"
        (safe to fall back, returns None) from "project resolved but the
        assigned repo_id is invalid" (RepoNotFoundError -- a genuine data-
        integrity error that should abort dispatch loudly, not silently
        substitute a different repo). The latter raises instead of returning
        None, so _scoped_worktree_manager can distinguish the two cases.
        """
        if not workflow_id:
            return None
        try:
            from src.core.database import Feature, Workflow
            from src.core.repo_resolution import RepoNotFoundError, resolve_repo_path

            session = self.db_manager.get_session()
            try:
                wf = session.query(Workflow).filter_by(id=workflow_id).first()
                if not wf or not wf.project_id:
                    return None
                repo_id = None
                if wf.feature_id:
                    feature = session.query(Feature).filter_by(id=wf.feature_id).first()
                    if feature is not None:
                        repo_id = feature.repo_id
                try:
                    return resolve_repo_path(session, wf.project_id, repo_id)
                except RepoNotFoundError:
                    # Project resolved but the assigned repo_id is invalid --
                    # this is a data-integrity error, not a "workflow doesn't
                    # resolve" case. Re-raise so _scoped_worktree_manager can
                    # distinguish it from the None/"no project" fallback.
                    raise
                except ValueError as e:
                    logger.warning(f"[WORKTREE] Could not resolve repo path for workflow {workflow_id}: {e}")
                    return None
            finally:
                session.close()
        except RepoNotFoundError:
            # Propagate -- _scoped_worktree_manager catches this specifically.
            raise
        except Exception as e:
            logger.warning(f"[WORKTREE] Could not resolve project for workflow {workflow_id}: {e}")
            return None

    def _scoped_worktree_manager(self, workflow_id: Optional[str]) -> WorktreeManager:
        """Return a WorktreeManager instance safely scoped to workflow_id's
        project. Constructs a FRESH instance and reload()s it -- mirrors
        orchestrator.py's precedented pattern (construct fresh, reload
        immediately, use, discard) rather than reload()-ing the shared
        self.branch_manager singleton in place.

        A fresh instance is required, not just a reload of the shared one:
        dispatch can run on genuinely different ThreadPoolExecutor worker
        threads (MAX_PARALLEL_FEATURES), so reload-then-use on a SHARED
        instance still races against another thread's reload landing in
        between reload() and the git operations that follow it.

        Falls back to self.branch_manager, unreloaded, when workflow_id
        doesn't resolve to a project -- preserves today's default/
        single-project behavior for that edge case rather than erroring.

        WARNING-1 fix: when the project resolved but the assigned repo_id
        is invalid (RepoNotFoundError), this is a genuine data-integrity
        error that must NOT silently fall back to the shared, unreloaded
        branch_manager. Raise instead of substituting a different repo.
        """
        from src.core.repo_resolution import RepoNotFoundError

        try:
            base_dir = self._resolve_project_base_dir(workflow_id)
        except RepoNotFoundError:
            # Project resolved but repo_id is invalid -- this is a real
            # data-integrity error. Do NOT silently fall back to the shared
            # branch_manager (which may be pointed at a completely different
            # repo from a concurrent operation). Raise so the caller knows
            # the dispatch cannot proceed safely.
            raise
        if base_dir is None:
            # WARNING-1: Create fresh instance instead of returning shared singleton
            base_dir = Path(self.config.git.main_repo_path)
        wt_mgr = WorktreeManager(db_manager=self.db_manager, repo_path=base_dir)
        return wt_mgr

    @staticmethod
    def _ensure_codegraph_initialized(working_directory: str) -> None:
        """Pre-warm the codegraph daemon so agents don't race to launch it.

        Runs `codegraph status .` which connects to the existing daemon or
        launches one if needed. After this returns, the daemon is running
        and subsequent pi instances (with the codegraph extension) will
        connect to it instead of each spawning their own.
        """
        import subprocess

        try:
            result = subprocess.run(
                ["which", "codegraph"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                return  # codegraph not installed, skip

            logger.info(f"[CODEGRAPH] Pre-warming daemon in {working_directory}")
            subprocess.run(
                ["codegraph", "status", "."],
                cwd=working_directory,
                capture_output=True,
                timeout=30,
            )
        except Exception as e:
            logger.warning(f"[CODEGRAPH] Pre-warm failed (non-fatal): {e}")

    async def _check_termination_race(
        self,
        agent_id: str,
        task_id: str,
        session_name: str,
        agent_id_to_return: str,
        task: Optional[Task] = None,
    ) -> Optional[object]:
        """Check whether the agent or task was terminated/cancelled during
        the CLI-init sleep. Returns an AgentInfo if launch should be
        aborted; None if safe to proceed.

        Currently create-only but called by both create and restart after
        extraction (documented gap-closing for restart).

        task: the create path's in-memory Task object, already speculatively
        mutated (assigned_agent_id/status/started_at set to "in_progress")
        a few lines before this check runs, by a caller session that won't
        commit until well after this function returns. If we detect the
        agent was terminated in the meantime (e.g. a workflow pause) but
        only kill the tmux session, that stale optimistic mutation is still
        sitting on `task` -- create_agent_for_task_direct's later
        session.commit() persists it anyway, since an aborted launch
        returns the exact same AgentInfo shape as a real success and the
        caller has no way to tell the difference. The task ends up
        "in_progress" pointing at an agent that was killed seconds after
        creation, invisible to every sweep until health_audit's 30-minute
        stuck-timeout finally notices -- with a failure_reason that reads
        like the agent hung, when it was actually killed almost
        immediately by something else. Restart's call site never mutates
        `task` before calling this, so passing it there would be a no-op;
        left as the default None rather than threading it through for no
        reason.
        """
        with self.db_manager.get_session() as _term_check:
            _current = _term_check.query(Agent).filter_by(id=agent_id).first()
            _agent_terminated = bool(_current and _current.status == "terminated")

            _fresh_task = _term_check.query(Task).filter_by(id=task_id).first()
            # A mismatched assigned_agent_id alone isn't proof this task was
            # genuinely reassigned to a live competitor -- the output-artifact
            # hard-floor check (_run_done_hard_floor_checks) rejects a "done"
            # claim without ever clearing assigned_agent_id, by design, so the
            # SAME agent can retry in place. If that agent later dies/gets
            # replaced instead of retrying, assigned_agent_id is left pointing
            # at a now-terminated agent forever -- and every future dispatch
            # attempt for this task (a fresh agent, its own id necessarily
            # different) reads that stale value as "someone else already owns
            # this," aborting immediately even though the "someone else" has
            # been dead for hours. Only trust the mismatch if the OTHER agent
            # is actually still alive. Observed live: task a12d727b's four
            # dispatch attempts across two days all aborted this way after its
            # first real agent's rejected "done" claim left assigned_agent_id
            # pointing at itself, terminated, never reassigned since.
            _other_agent_is_live = False
            if _fresh_task and _fresh_task.assigned_agent_id and _fresh_task.assigned_agent_id != agent_id:
                _other_agent = _term_check.query(Agent).filter_by(id=_fresh_task.assigned_agent_id).first()
                _other_agent_is_live = bool(_other_agent and _other_agent.status != "terminated")
            _task_cancelled = bool(_fresh_task and (_fresh_task.status in TaskStatus.TERMINAL or _other_agent_is_live))

            if _agent_terminated or _task_cancelled:
                reason = "was terminated" if _agent_terminated else f"its task {task_id} was reassigned/cancelled (status={_fresh_task.status}, assigned_agent_id={_fresh_task.assigned_agent_id})"
                logger.warning(f"Agent {agent_id} {reason} while its CLI was still initializing -- aborting launch, not delivering initial prompt")
                if self.tmux_server.has_session(session_name):
                    self.tmux_server.kill_session(session_name)

                if task is not None and _fresh_task is not None:
                    task.assigned_agent_id = _fresh_task.assigned_agent_id
                    task.status = _fresh_task.status
                    task.started_at = _fresh_task.started_at
                    if _agent_terminated:
                        # pause_project_workflows stamps this same message on
                        # every task it catches in its own tasks_to_reset
                        # pass -- but that pass and this one both key off
                        # Task.status == "in_progress"/reading the just-
                        # terminated Agent row, so either can win the race to
                        # observe/act on a given task first. When THIS path
                        # wins, _fresh_task.failure_reason (just copied above)
                        # is still whatever predates the pause, since the
                        # other pass hasn't committed its own copy of this
                        # message yet. paused_by is set in the exact same
                        # commit as the agent termination we just detected,
                        # so it's a reliable proxy for "was this specific
                        # termination caused by a user pause" even when the
                        # task-reset side of that same pause hasn't landed.
                        from src.core.database import Workflow

                        _wf = _term_check.query(Workflow).filter_by(id=_fresh_task.workflow_id).first()
                        if _wf and _wf.paused_by == "user":
                            task.failure_reason = "User terminated: workflow was paused"

                class AgentInfo:
                    def __init__(self, id):
                        self.id = id

                return AgentInfo(agent_id_to_return)
        return None

    def _detect_launch_failure(self, pane, cli_agent, cli_type: str, session_name: str) -> None:
        """Detect whether the CLI's launch command was rejected by the
        shell or by the CLI itself, leaving a dead pane.  Uses
        cli_agent.get_launch_rejection_patterns() — the base generic
        patterns plus any CLI-specific wording the subclass overrides.

        Detects which pattern fired to preserve distinct error messages
        (generic shell rejection vs. CLI-specific confirmation dialog).

        Callers MUST skip this entirely once _wait_for_cli_ready has
        already confirmed the CLI is ready (returned True). The base
        patterns ("command not found", "No such file or directory") are
        generic substrings matched against the last 15 captured pane
        lines -- once the CLI is up and doing real work, its own normal
        output (a missing optional file it gracefully handles, a shell
        command it runs internally) can easily contain one of these
        phrases with nothing to do with its own launch. Observed live: a
        pi agent logged "ready after 3.1s", then 0.2s later this check
        matched unrelated text from the agent's own in-progress work and
        killed the session, marking a perfectly running agent "failed to
        start".
        """
        import re

        try:
            launch_check = pane.cmd("capture-pane", "-p", "-S", "-15").stdout
            launch_check_text = "\n".join(launch_check) if launch_check else ""
        except Exception:
            launch_check_text = ""

        patterns = cli_agent.get_launch_rejection_patterns()
        for pattern in patterns:
            if re.search(pattern, launch_check_text, re.IGNORECASE):
                # Pre-split wordings: ONLY the Claude Code confirmation-dialog
                # pattern raised the "stuck on a dialog" message; every other
                # pattern (base shell rejections and pi's model-not-found)
                # raised the generic shell-rejection message.
                if pattern == self._CLAUDE_CODE_CONFIRMATION_PATTERN:
                    logger.error(f"{cli_type} launch command is stuck on an unhandled confirmation dialog in tmux session {session_name}: {launch_check_text.strip()[-300:]}")
                    raise Exception(f"{cli_type} CLI is stuck on an unhandled first-run confirmation dialog")
                logger.error(f"{cli_type} launch command failed in tmux session {session_name}: {launch_check_text.strip()[-300:]}")
                raise Exception(f"{cli_type} CLI failed to start -- shell reported the launch command was not found")

    async def _wait_for_cli_ready(
        self,
        pane,
        cli_agent,
        cli_type: str,
        agent_id: str,
        floor: float = 3.0,
        timeout: float = 25.0,
        poll_interval: float = 0.5,
    ) -> bool:
        """Wait for the CLI tool itself (not just the shell -- see
        _wait_for_shell_ready for that earlier stage) to finish starting up
        and render its ready-for-input UI, instead of always blocking for a
        flat `timeout` regardless of how long that actually takes.

        `floor` is a mandatory minimum wait before polling starts: right
        after the launch command is sent, the CLI process hasn't even
        begun rendering yet, so an immediate capture-pane would just poll a
        blank/mid-startup pane repeatedly for no benefit. `timeout` is the
        same ceiling the previous flat sleep always paid up front -- kept
        as a safety net so a CLI whose ready pattern never appears (a
        genuinely slow start, or a pattern mismatch) waits no longer than
        today's behavior already did, not longer.

        Uses cli_agent.get_health_check_pattern() -- already defined per
        CLI type for exactly this "is it ready" signal, previously unused
        anywhere in the codebase.

        Returns True once the ready pattern is matched, False on timeout --
        the caller uses this to decide whether _detect_launch_failure should
        even run (see that method's own docstring for why a confirmed-ready
        CLI must skip it).
        """
        import re

        logger.info(f"Waiting up to {timeout:.0f}s for {cli_type} agent {agent_id} to become ready (floor {floor:.0f}s)...")
        start = time.monotonic()
        await asyncio.sleep(floor)

        pattern = cli_agent.get_health_check_pattern()
        loop = asyncio.get_event_loop()
        # Poll-count loop, not a time.monotonic() deadline: this codebase's
        # own launch_pipeline tests broadly mock asyncio.sleep to return
        # instantly (to keep the suite fast), which would otherwise leave
        # nothing to gate a wall-clock deadline and turn every non-matching
        # poll into a real, un-mocked ~timeout-second busy loop -- a
        # regression from the old flat `await asyncio.sleep(25)`, which
        # those same mocks made free. A fixed poll count keeps this
        # function's total wait bounded by asyncio.sleep alone, exactly
        # like the code it replaces.
        max_polls = max(1, int((timeout - floor) / poll_interval))
        for _ in range(max_polls):
            try:
                captured = await loop.run_in_executor(None, pane.cmd, "capture-pane", "-p", "-S", "-10")
                text = "\n".join(captured.stdout) if captured.stdout else ""
            except Exception:
                text = ""
            if text and re.search(pattern, text):
                logger.info(f"{cli_type} agent {agent_id} ready after {time.monotonic() - start:.1f}s")
                return True
            await asyncio.sleep(poll_interval)

        logger.warning(f"{cli_type} agent {agent_id} did not match its ready pattern within {timeout:.0f}s -- proceeding anyway (same ceiling as the previous flat wait)")
        return False

    def _check_duplicate_active_agent(self, task: Task) -> Optional[Agent]:
        """Guard: don't create a second agent for a task that already has one.

        Returns the existing active agent if found, None otherwise.
        Create-only — restart already knows its agent.
        """
        with self.db_manager.get_session() as session:
            from src.core.database import Agent as _GuardAgent

            existing = (
                session.query(_GuardAgent)
                .filter(
                    _GuardAgent.current_task_id == task.id,
                    _GuardAgent.status.in_(["working", "idle"]),
                )
                .first()
            )
            if existing:
                logger.warning(f"Agent {existing.id[:8]} already active for task {task.id[:8]} — skipping duplicate creation")
                return existing
        return None

    def _resolve_phase_config(
        self,
        task: Task,
        cli_type: Optional[str],
        phase_cli_tool: Optional[str],
        phase_cli_model: Optional[str],
        phase_glm_token_env: Optional[str],
        phase_thinking_level: Optional[str],
    ) -> PhaseConfig:
        """Resolve phase CLI/model/thinking config with fallback to global defaults.

        Create-only — restart reads frozen agent.cli_type/agent.cli_model.
        """
        fallback_cli_tool = None
        fallback_cli_model = None
        if task.phase_id and (phase_cli_tool is None and phase_cli_model is None and phase_glm_token_env is None and phase_thinking_level is None):
            try:
                from src.core.database import Phase

                with self.db_manager.get_session() as _ps:
                    _ph = _ps.query(Phase).filter_by(id=task.phase_id).first()
                    if _ph:
                        phase_cli_tool = _ph.cli_tool
                        phase_cli_model = _ph.cli_model
                        fallback_cli_tool = getattr(_ph, "fallback_cli_tool", None)
                        fallback_cli_model = getattr(_ph, "fallback_cli_model", None)
                        phase_glm_token_env = _ph.glm_api_token_env
                        phase_thinking_level = _ph.thinking_level
            except Exception as e:
                logger.warning(f"Could not derive phase config for task {task.id}: {e}")

        cli_type = phase_cli_tool or cli_type or self.config.agents.default_cli_tool

        if not fallback_cli_tool and self.config.agents.default_fallback_cli_tool:
            if self.config.agents.default_fallback_cli_tool != cli_type:
                fallback_cli_tool = self.config.agents.default_fallback_cli_tool
                fallback_cli_model = self.config.agents.default_fallback_cli_model

        return PhaseConfig(
            cli_type=cli_type,
            phase_cli_tool=phase_cli_tool,
            cli_model=phase_cli_model,
            glm_token_env=phase_glm_token_env,
            thinking_level=phase_thinking_level,
            fallback_cli_tool=fallback_cli_tool,
            fallback_cli_model=fallback_cli_model,
        )

    def _resolve_worktree(
        self,
        task: Task,
        wt_mgr: WorktreeManager,
        *,
        create_if_missing: bool,
        agent_id: str,
        context_files: Optional[Dict[str, str]] = None,
    ) -> WorktreeResolution:
        """Resolve the working directory for an agent.

        Shared — create passes create_if_missing=True (fail-loudly for shared
        worktrees), restart passes create_if_missing=False (silent None on
        missing).
        """
        branch_path = None
        branch_name = None
        resolved_context = context_files or {}

        if task.workflow_id:
            from src.core.database import Workflow

            with self.db_manager.get_session() as session:
                wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
                if wf and wf.working_directory:
                    if ".worktrees/" in wf.working_directory:
                        # Shared worktree path — must exist and be a valid git repo
                        wt_path = Path(wf.working_directory)
                        if not (wt_path.exists() and (wt_path / ".git").exists()):
                            if create_if_missing:
                                raise RuntimeError(
                                    f"Workflow {task.workflow_id[:8]}'s shared worktree "
                                    f"{wt_path} is missing or not a valid git worktree. "
                                    "Refusing to fork a disconnected replacement or "
                                    "silently recover it -- find out what deleted it."
                                )
                            # restart: silent None, don't raise
                        else:
                            branch_path = wf.working_directory
                            branch_name = f"shared-{task.workflow_id[:8]}"
                            if create_if_missing:
                                wt_mgr.reload(wt_path)
                            else:
                                # Restart: reload on a throwaway manager —
                                # HEAD never reloaded the shared
                                # self.branch_manager singleton in place
                                # (races concurrent dispatch threads).
                                from src.core.worktree_manager import WorktreeManager

                                WorktreeManager(db_manager=self.db_manager).reload(wt_path)
                            logger.info(f"Using shared worktree for agent {agent_id[:8]} at {branch_path}")
                    elif not create_if_missing and Path(wf.working_directory).exists():
                        # Restart-only: non-worktrees working directory (e.g.
                        # legacy or direct path) — use it if it exists on disk.
                        # Create deliberately does NOT take this branch: it
                        # always forks an isolated per-agent worktree for
                        # non-shared workflows (pre-split behavior).
                        branch_path = wf.working_directory
                        branch_name = f"shared-{task.workflow_id[:8]}"
                        logger.info(f"Using workflow working directory for agent {agent_id[:8]} at {branch_path}")

        if branch_path is None and create_if_missing and context_files is not None:
            branch_info = wt_mgr.create_agent_worktree(
                agent_id=agent_id,
                parent_agent_id=getattr(task, "created_by_agent_id", None),
                context_files=resolved_context,
            )
            branch_path = branch_info["working_directory"]
            branch_name = branch_info["branch_name"]
            wt_mgr.switch_to_branch(branch_name)
            logger.info(f"Created worktree {branch_name} for agent {agent_id[:8]} at {branch_path}")
        elif branch_path is None and not create_if_missing:
            # restart fallback: agent's own tracked worktree
            try:
                candidate = self.branch_manager.get_agent_branch_path(agent_id)
                if candidate and Path(candidate).exists():
                    branch_path = candidate
            except Exception as e:
                logger.debug(f"[RESTART] Could not resolve agent branch path for {agent_id[:8]}: {e}")

        return WorktreeResolution(
            branch_path=branch_path,
            branch_name=branch_name,
            context_files=resolved_context,
        )

    def _resolve_env_and_model(
        self,
        cli_type: str,
        task: Task,
        agent_id: str,
        label: str,
        *,
        phase_cli_model: Optional[str] = None,
        phase_cli_tool: Optional[str] = None,
        phase_glm_token_env: Optional[str] = None,
        agent_cli_model: Optional[str] = None,
    ) -> Tuple[Dict[str, str], str, Any]:
        """Resolve model and environment variables for agent launch.

        Shared — create passes phase_cli_model/phase_cli_tool/phase_glm_token_env;
        restart passes agent_cli_model.
        Returns (env_vars, model, cli_agent).
        """
        cli_agent = get_cli_agent(cli_type)
        global_model = getattr(self.config.agents, "cli_model", None) if cli_type == self.config.agents.default_cli_tool else None
        if agent_cli_model is not None:
            # restart path: prefer agent's frozen model
            model = agent_cli_model or global_model or cli_agent.default_model
        else:
            # create path: prefer phase config
            model = (phase_cli_model if phase_cli_tool else None) or global_model or cli_agent.default_model

        glm_token = phase_glm_token_env if phase_glm_token_env else None
        env_vars = self._build_glm_env_vars(model, glm_token, agent_id, label=label)

        timeout_ms = self._resolve_mcp_timeout_ms(cli_type, task.workflow_id, label=label)
        if timeout_ms is not None:
            env_vars = env_vars or {}
            env_vars["MCP_TOOL_TIMEOUT"] = str(timeout_ms)

        env_vars = env_vars or {}
        env_vars["HEPHAESTUS_AGENT_ID"] = agent_id
        env_vars["HEPHAESTUS_TASK_ID"] = task.id
        if task.workflow_id:
            env_vars["HEPHAESTUS_WORKFLOW_ID"] = task.workflow_id
        if task.phase_id:
            env_vars["HEPHAESTUS_PHASE_ID"] = task.phase_id
        import os

        _api_port = os.environ.get("HEPHAESTUS_PORT") or str(getattr(self.config.server, "mcp_port", 8300))
        env_vars["HEPHAESTUS_API_URL"] = f"http://localhost:{_api_port}"

        return env_vars, model, cli_agent

    def _resolve_phase_name_and_thinking(
        self,
        task: Task,
        phase_thinking_override: Optional[str],
    ) -> Tuple[Optional[str], str, Optional[str]]:
        """Resolve phase name, order, and thinking level.

        Shared — returns (phase_name, phase_order, thinking_level).
        """
        phase_name = None
        phase_order = "?"
        if task.phase_id:
            session = self.db_manager.get_session()
            try:
                phase = resolve_task_phase(session, task)
                if phase:
                    phase_name = phase.name
                    phase_order = str(phase.order)
            finally:
                session.close()

        thinking_level = phase_thinking_override or getattr(self.config.agents, "cli_thinking_level", "medium")
        return phase_name, phase_order, thinking_level

    def _resolve_session_id(
        self,
        task: Task,
        agent_type: str,
        phase_name: Optional[str],
        model: str,
        *,
        excluded_types: Tuple[str, ...],
        excluded_phases: Tuple[str, ...] = (),
    ) -> str:
        """Generate deterministic session ID for persistent agent sessions.

        Shared — create passes excluded_types including 'arbitration';
        restart passes a shorter tuple (Phase 3 mismatch, preserved as-is).

        excluded_phases: only passed by the new-task dispatch call site, not
        restart. A goto that re-enters a review-style gated phase (e.g.
        feature_review) for the same design creates a NEW task, dispatched
        here -- resuming the PRIOR review's session hands the "fresh
        reviewer" agent that phase's own instructions demand the earlier
        agent's finished conversation instead, and it echoes the old
        verdict instead of re-checking current state (observed live:
        feature_review re-reported 4 already-fixed BLOCKERs verbatim,
        including the earlier agent's own id in its save_memory call,
        because --resume replayed that agent's session). Restart, by
        contrast, continues the SAME interrupted task/session and must
        keep resuming -- excluded_phases is empty there.
        """
        session_id = ""
        if task.workflow_id and agent_type not in excluded_types and phase_name not in excluded_phases:
            try:
                _s = self.db_manager.get_session()
                try:
                    from src.core.database import Workflow

                    _wf = _s.query(Workflow).filter_by(id=task.workflow_id).first()
                    if _wf and _wf.launch_params:
                        _lp = _wf.launch_params if isinstance(_wf.launch_params, dict) else {}
                        _pid = _lp.get("project_id") or _lp.get("project_path", "")
                        _dsl = _lp.get("design_slug") or _lp.get("design_id") or _lp.get("feature_id", "")
                        if _pid and _dsl and phase_name:
                            from src.autopilot.phases import get_session_id

                            session_id = get_session_id(_pid, _dsl, phase_name, model=model, workflow_id=task.workflow_id)
                finally:
                    _s.close()
            except Exception as e:
                logger.debug(f"[SESSION] Could not generate session ID: {e}")

        if session_id:
            logger.info(f"[SESSION] Using session ID: {session_id} for phase {phase_name}")
        return session_id

    def _prepare_launch_environment(
        self,
        session_name: str,
        working_directory: Optional[str],
        env_vars: Dict[str, str],
        task: Task,
        phase_name: Optional[str],
        cli_agent=None,
        prewarm_codegraph: bool = True,
    ) -> "libtmux.Session":
        """Create tmux session and prepare the launch environment.

        Shared — caller provides the session_name (create uses base name,
        restart appends '_r'). cli_agent is used for prepare_working_directory.
        prewarm_codegraph mirrors pre-split behavior: create pre-warmed
        codegraph, restart did not.
        """
        if working_directory and cli_agent:
            cli_agent.prepare_working_directory(working_directory)
            if prewarm_codegraph:
                # Fire-and-forget: this pre-warm only benefits OTHER agents
                # that might connect to the same codegraph daemon later
                # ("so agents don't race to launch it" -- see
                # _ensure_codegraph_initialized's own docstring), not THIS
                # agent's own launch, which never waits on codegraph at
                # all. Blocking this thread on it anyway added 3.6s+
                # measured cold-start latency to every single agent
                # launch, on the critical path, for zero benefit to that
                # specific launch. A daemon thread, not a plain one: must
                # never block process shutdown if it's still mid-subprocess.
                import threading

                threading.Thread(
                    target=self._ensure_codegraph_initialized,
                    args=(working_directory,),
                    daemon=True,
                ).start()

        if task.phase_id and working_directory:
            from pathlib import Path as _Path

            phase_output_dir = _Path(working_directory) / ".hephaestus" / (phase_name or task.phase_id)
            phase_output_dir.mkdir(parents=True, exist_ok=True)

        return self._create_tmux_session(session_name, working_directory=working_directory, env_vars=env_vars)

    async def _build_and_send_launch_command(
        self,
        cli_agent,
        tmux_session,
        *,
        system_prompt: str,
        task: Task,
        model: str,
        thinking_level: Optional[str],
        phase_name: Optional[str],
        agent_id: str,
        session_id: str,
        working_directory: Optional[str],
        instructions_pointer: str,
        env_vars: Dict[str, str],
        label: str,
    ) -> Tuple[Any, Any, float]:
        """Build launch command, export env vars, and return pane + timestamp.

        Shared — returns (launch_result, pane, cli_launch_started_at).
        The caller is responsible for sending the command to the pane
        (after echoing task info).
        """
        launch_result = cli_agent.get_launch_command(
            system_prompt=system_prompt,
            task_id=task.id,
            model=model,
            thinking_level=thinking_level,
            phase_name=phase_name,
            agent_id=agent_id,
            workflow_id=task.workflow_id,
            phase_id=task.phase_id,
            session_id=session_id,
            working_directory=working_directory,
            instructions_pointer=instructions_pointer,
        )
        pane = tmux_session.attached_window.attached_pane

        if env_vars:
            logger.info(f"Exporting {len(env_vars)} environment variables for {label}: {', '.join(env_vars.keys())}")
            await self._export_env_vars_and_verify(tmux_session, pane, env_vars, label=label)

        cli_launch_started_at = utc_now().timestamp()
        return launch_result, pane, cli_launch_started_at

    async def _deliver_initial_prompt(
        self,
        pane,
        cli_agent,
        cli_type: str,
        initial_message: str,
        agent_id: str,
        task: Task,
        *,
        agent_type: str = "phase",
        instructions_rel_path: Optional[str] = None,
    ) -> None:
        """Deliver initial prompt with confirmation keys, goal, retry, and verification.

        Shared — unifies the confirmation-key loop + call ordering.

        Initial prompt sent BEFORE /goal, not after (see _send_goal_command's
        own docstring for why this used to be reversed, and why that
        assumption was wrong): /goal is a real chat turn to the CLI, not a
        side-channel the CLI consumes out-of-band -- it goes through the
        same UserPromptSubmit hook pipeline as any other message. Observed
        live: a UserPromptSubmit hook timed out and its output (context
        establishing "this is Hephaestus's own goal-tracking mechanism, not
        a user request") was discarded, so a freshly-launched agent's very
        FIRST input was a bare, unframed AND-chain of done_definition
        clauses with nothing yet telling it this was an autonomous task
        dispatch -- it read as an ambiguous standalone request and the
        agent stopped to ask for clarification, deadlocking the task (no
        human was watching that pane to answer). Sending the task-pointer
        message first establishes "you are an autonomous agent, read your
        instructions file, begin working" as context BEFORE /goal ever
        arrives, so the same hook failure can't strand /goal with no frame
        of reference. The wait below (mirroring _send_goal_command's own
        post-send sleep) gives the agent a moment to actually start
        processing the initial prompt before /goal lands, to avoid the
        opposite interleaving problem chunked delivery already works
        around -- not a guaranteed idle-check, but the same class of flat
        wait already used throughout this dispatch sequence.
        """
        for key in cli_agent.post_launch_confirmation_keys():
            pane.send_keys(key)
            await asyncio.sleep(1.5)

        await self._send_initial_prompt_with_retry(
            pane=pane,
            cli_agent=cli_agent,
            cli_type=cli_type,
            initial_message=initial_message,
            agent_id=agent_id,
            task_id=task.id,
            max_retries=3,
        )

        await asyncio.sleep(3)
        await self._send_goal_command(pane, cli_agent, task, agent_type)

    def _wait_for_shell_ready(self, pane, timeout: float = 2.0, poll_interval: float = 0.1) -> None:
        """Block until a freshly-created tmux pane's shell has actually
        started accepting input, not just until new_session() returns.

        new_session() returns as soon as the pane exists, before the shell
        inside it (zsh, sourcing .zshrc, initializing the prompt theme,
        etc.) is done starting up. Sending keys before that finishes races
        the shell's own startup output -- the first bytes we send can land
        mid-init and get corrupted (e.g. "export FOO=" arrives as
        "eexport FOO=", silently failing as an unrecognized command).
        Observed live: a restarted agent's HEPHAESTUS_* env exports and its
        PATH-prefixed launch line both corrupted this way, leaving it
        without its identity env vars and without the agent-safe-bin `rm`
        guardrail for that entire session.

        Polls the pane's captured content for two consecutive stable reads
        (same non-empty content twice in a row) as a readiness signal,
        since the exact prompt string varies by shell/theme. Gives up after
        `timeout` regardless, so a pane that never stabilizes doesn't block
        agent creation forever.

        Also called unconditionally a second time, later in the same
        launch, immediately before sending launch_result.command itself
        (both in create_agent_for_task's flow, via
        _create_agent_for_task_steps.py, and in restart_agent) --
        "stable" works equally well as "the shell has finished processing
        everything already sent and is idle again" as it does for
        "freshly started," so the same poll covers both. Most valuable
        exactly when env_vars was empty, since a non-empty env_vars
        already gets an equivalent readback-and-retry guard via
        _export_env_vars_and_verify -- but cheap enough (near-instant once
        the pane is genuinely idle) to run either way rather than branch
        on it.
        """
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                captured = pane.cmd("capture-pane", "-p", "-S", "-10").stdout
                current = "\n".join(captured) if captured else ""
            except Exception:
                current = ""
            if current and current == last:
                return
            last = current
            time.sleep(poll_interval)

    def _create_tmux_session(
        self,
        session_name: str,
        working_directory: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
    ) -> libtmux.Session:
        """Create a new tmux session.

        Args:
            session_name: Name for the tmux session
            working_directory: Working directory for the session (should be a worktree path)
            env_vars: Optional dictionary of environment variables to set on the session

        Returns:
            Created tmux session
        """
        # Check if session already exists
        if self.tmux_server.has_session(session_name):
            logger.warning(f"Session {session_name} already exists, killing it")
            self.tmux_server.kill_session(session_name)

        # Create new session with working directory (should be worktree path)
        session_kwargs = {
            "session_name": session_name,
            "window_name": "agent",
            "attach": False,
            "x": 150,  # Initial width in columns
            "y": 50,  # Initial height in rows
        }
        # Use provided working directory (which should be a worktree path)
        # Fallback to project root from config if not provided
        if not working_directory:
            working_directory = str(self.config.paths.project_root)
            logger.warning(f"No working directory provided, using project root: {working_directory}")
        session_kwargs["start_directory"] = working_directory

        session = self.tmux_server.new_session(**session_kwargs)

        # New sessions inherit the tmux server's environment at the time it
        # was first started -- if that happened to be a shell running
        # inside a Claude Code session (e.g. `heph restart` invoked from
        # this very CLI), every future pane on this server carries
        # CLAUDECODE=1 and friends indefinitely, regardless of who spawns
        # the pane afterward. `claude` itself refuses to launch when it
        # sees CLAUDECODE=1 ("cannot be launched inside another Claude
        # Code session"), so an agent using the claude CLI would silently
        # never start. Clear it in the new pane before anything runs.
        try:
            pane0 = session.attached_window.attached_pane
            self._wait_for_shell_ready(pane0)
            pane0.send_keys(
                "unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION CLAUDE_AGENT_SDK_VERSION",
                enter=True,
            )
        except Exception:
            pass  # Non-critical -- worst case the launch command fails visibly

        # A large history-limit -- NOT the raw pipe-pane transcript below --
        # is what the viewer's clean, human-readable transcript actually
        # depends on. _poll_stable_transcript reconstructs correct text
        # from tmux's own capture-pane rendering (cursor positioning,
        # overwrites, and line wrapping already resolved) because the raw
        # pipe-pane bytes below re-show every intermediate \r/cursor-
        # redrawn TUI frame as its own line and can't always be turned
        # back into correct text by regex stripping alone. capture-pane's
        # own scrollback is bounded by history-limit, so any single poll
        # interval producing more new output than that forces a lossy
        # reset (see _capture_pane_lines/_poll_stable_transcript) --
        # confirmed live TWICE now (agents 3eab5529 and 09f35b63): each
        # clean transcript started mid-paragraph/mid-sentence, everything
        # before that point unrecoverably gone. 1000 (then 50000) was
        # sized as if the durable pipe-pane file made this moot; it
        # doesn't, since that file isn't what the viewer reads -- and
        # since _poll_stable_transcript only runs when the frontend viewer
        # is actually open and polling, the dangerous window isn't "output
        # per second" but "output accumulated before anyone first looks,"
        # which can be minutes on an unwatched agent. No history-limit
        # fully closes that window, but a much larger one substantially
        # shrinks how often it's hit. Sized generously -- tens of MB per
        # session even at this size, negligible next to the cost of losing
        # transcript history.
        try:
            session.set_option("history-limit", "500000")
        except Exception:
            pass  # Non-critical

        # tmux's default (remain-on-exit off) destroys the whole session the
        # instant the pane's foreground process dies, for ANY reason --
        # clean exit, crash, or an external kill (e.g. OOM). That destroys
        # capture-pane's scrollback and any exit-status banner along with
        # it, which is why a dead agent shows up to Guardian as a session
        # that's simply gone rather than one with something inspectable in
        # it. pipe-pane's durable transcript survives regardless, but this
        # keeps the live session itself around long enough for
        # capture-pane/health checks to see what actually happened.
        # remain-on-exit is a window option, not a session option.
        try:
            session.attached_window.set_window_option("remain-on-exit", "on")
        except Exception:
            pass  # Non-critical

        # Continuously tee this session's output to a durable file via tmux's
        # pipe-pane, independent of history-limit and of how the session later
        # dies. terminate_agent() only captures a scrollback snapshot on its
        # own "clean shutdown" path — the orphan reaper and auto-restart kill
        # paths kill sessions with no capture at all, losing the transcript
        # needed to audit what an agent actually ran. pipe-pane appends every
        # byte in real time, so it survives all of those kill paths.
        try:
            tmux_dir = Path(working_directory) / CONTEXT_DIR_NAME / "tmux"
            tmux_dir.mkdir(parents=True, exist_ok=True)
            transcript_path = tmux_dir / f"{session_name}.transcript.log"
            # pipe-pane gets the pane's raw pty bytes, unlike capture-pane
            # (which tmux itself renders to plain text). Keep ANSI codes
            # so the frontend can render colors via ansi-to-html; strip
            # everything else that doesn't carry row/column/color
            # information. Runs as a subprocess, not imported, so it's a
            # real standalone script (pty_filter.py) rather than an
            # inline one-liner -- see that file's own docstring for why
            # unbuffered true-short-reads matter here (a naive
            # line-buffered filter can sit frozen for a live TUI's entire
            # run) and why CSI G/C/D/B/A/H/J/K/f specifically must survive
            # the strip (output_capture.py's _read_transcript_log
            # reconstructs rows from them). sys.executable (not a bare
            # "python3") pins this to the exact interpreter already
            # running the backend, avoiding any PATH ambiguity.
            #
            # `cd` into this file's own directory first, rather than
            # trusting the pane's cwd: this pipe-pane command is a plain
            # shell command tmux execs against whatever cwd the SPAWNING
            # tmux server process itself has, not necessarily this pane's
            # working_directory -- and unlike Perl, CPython's interpreter
            # bootstrap needs a valid getcwd() and fails with a fatal,
            # silent-to-the-transcript startup error ("OSError: failed to
            # make path absolute") if it doesn't have one. Confirmed live:
            # a long-running tmux server whose own cwd had since been
            # deleted made every pipe-pane invocation of this filter fail
            # at Python startup, before a single byte was ever captured.
            # pty_filter.py's own directory is guaranteed to exist for as
            # long as this code is running at all, unlike the pane's cwd
            # (a worktree, which can be removed out from under a still-
            # live pane -- see worktree_removal.py).
            pty_filter_path = Path(__file__).parent / "pty_filter.py"
            python_exe = sys.executable or "python3"
            pipe_cmd = (
                f"cd {shlex.quote(str(pty_filter_path.parent))} && "
                f"{shlex.quote(python_exe)} {shlex.quote(str(pty_filter_path))} "
                f">> {shlex.quote(str(transcript_path))}"
            )
            session.attached_window.attached_pane.cmd("pipe-pane", "-o", pipe_cmd)
        except Exception as e:
            logger.warning(f"Failed to enable pipe-pane transcript logging: {e}")

        # Use a large pane so captured output isn't hard-wrapped at 80x24 and
        # so Ink-based TUIs (Claude Code, pi) -- which size their live-
        # rendering viewport off the pane's reported terminal height and
        # redraw-in-place via absolute cursor positioning once content
        # exceeds it, permanently discarding anything scrolled out -- have
        # far more room before they need to start doing that. TMUX_PANE_WIDTH
        # is shared with output_capture.py's raw-transcript row
        # reconstruction, which auto-wraps at this same fixed width to match
        # where the terminal itself wrapped -- it can't observe the pane's
        # actual width later (usually long gone by the time a terminated
        # agent's transcript is read), so the two must agree.
        #
        # `resize-pane` (the previous approach here, via pane.set_width /
        # pane.resize_pane) is a no-op on a single-pane window -- there's no
        # sibling pane to redistribute space from -- so those calls never
        # actually resized anything; the window instead just tracked
        # whatever real client last attached, under tmux's default
        # `window-size latest`. `resize-window` is the call that actually
        # resizes a single-pane window, and only takes effect once
        # `window-size` is set to `manual` (otherwise a later client attach
        # silently reverts it). Confirmed empirically: set_width/resize_pane
        # left a test pane at 80x24 even with window-size manual already
        # set; window-size manual + resize-window is the only combination
        # that worked, verified by a process inside the pane observing the
        # new size via os.get_terminal_size().
        try:
            session.set_option("window-size", "manual")
            session.attached_window.cmd(
                "resize-window", "-x", str(TMUX_PANE_WIDTH), "-y", str(TMUX_PANE_HEIGHT)
            )
        except Exception:
            pass  # Non-critical

        # Note: env_vars are exported in the shell before launching the agent
        # (see create_agent_for_task and restart_agent methods)

        logger.debug(f"Created tmux session: {session_name}")
        return session

    async def _export_env_vars_and_verify(self, tmux_session, pane, env_vars: Optional[Dict[str, str]], label: str) -> None:
        """Export env_vars into a pane's shell, then verify one of them
        actually landed before the caller proceeds to launch the CLI in
        that same shell.

        _wait_for_shell_ready reduces the startup race this guards against
        but doesn't eliminate it -- a shell that's still slow past that
        wait can still swallow/corrupt an export. Re-sends once on a failed
        readback rather than silently launching the CLI missing its
        identity env vars (HEPHAESTUS_AGENT_ID etc., which MCP tool calls
        and the pi cost-tracker extension depend on) and, when env_vars
        carries a PATH override, the agent-safe-bin `rm` guardrail.
        """
        if not env_vars:
            return

        check_key = "HEPHAESTUS_AGENT_ID" if "HEPHAESTUS_AGENT_ID" in env_vars else next(iter(env_vars))
        expected = env_vars[check_key]

        for attempt in range(2):
            for key, value in env_vars.items():
                tmux_session.set_environment(key, value)
            for key, value in env_vars.items():
                pane.send_keys(f'export {key}="{value}"', enter=True)
                await asyncio.sleep(0.1)
            await asyncio.sleep(1.0)

            sentinel = f"__ENVCHECK_{uuid.uuid4().hex[:8]}__"
            pane.send_keys(f'echo "{sentinel}:${check_key}"', enter=True)
            await asyncio.sleep(0.5)
            try:
                captured = pane.cmd("capture-pane", "-p", "-S", "-20").stdout
                output = "\n".join(captured) if captured else ""
            except Exception:
                output = ""
            if f"{sentinel}:{expected}" in output:
                return
            logger.warning(f"[ENV-EXPORT] {label}: readback for {check_key} didn't match on attempt {attempt + 1} -- {'retrying' if attempt == 0 else 'giving up, launching anyway'}")

    def _write_task_instructions(self, worktree_path: str, task_id: str, content: str) -> str:
        """Persist an agent's full initial instructions as a markdown file in
        its worktree, so every phase agent -- the first in a workflow
        included -- receives its task the same way later phases already
        receive prior phases' outputs (spec.md, architecture.md,
        requirements.md written by _gather_worktree_context): as a file to
        read, not a wall of text pasted live into the terminal.

        Returns the path relative to the worktree, for use in the short
        pointer message actually sent over tmux.
        """
        tasks_dir = Path(worktree_path) / ".hephaestus" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        instructions_path = tasks_dir / f"{task_id}.md"
        instructions_path.write_text(content)
        instructions_path.chmod(0o644)
        return f".hephaestus/tasks/{task_id}.md"

    @staticmethod
    def _build_instructions_pointer(
        task_id: str,
        instructions_rel_path: str,
        restarted: bool = False,
        agent_name: str = None,
    ) -> str:
        """Short, constant-size message pointing an agent at its full
        instructions file. This -- not the file's content -- is what
        actually reaches the CLI: over tmux for most CLIs (step 7 in
        create_agent_for_task/restart_agent), or embedded directly in the
        launch command for a CLI like OpenCode that has no separate
        post-launch "send a message" step (see instructions_pointer kwarg
        on CLIAgentInterface.get_launch_command).

        agent_name (e.g. "hephaestus-qa-validation") is included so the
        execute message self-identifies which agent was invoked -- useful
        for verifying from tmux output that the correct pi agent ran.
        """
        detail = " (including the restart note)" if restarted else ""
        verb = "continue" if restarted else "begin"
        agent_tag = f"[{agent_name}] " if agent_name else ""
        return f"Task ID: {task_id}\n\n{agent_tag}Your full task instructions{detail} are in {instructions_rel_path} -- read that file now, then {verb}."

    async def _send_goal_command(self, pane, cli_agent, task: Task, agent_type: str) -> None:
        """Set a self-checked completion condition (e.g. Claude Code's
        `/goal <condition>`, via cli_agent.format_goal_command -- a no-op
        empty string for CLIs with no such mechanism) so the agent keeps
        working until task.done_definition is actually met, instead of
        stopping on its own judgment.

        Sent AFTER the task pointer now (see _deliver_initial_prompt's own
        docstring for the incident this fixes) -- /goal is a real chat
        turn the CLI's own UserPromptSubmit hook pipeline processes like
        any other message, not a side-channel exempt from it. Sending it
        first used to mean a freshly-launched agent's very FIRST input,
        with zero established context, was a bare AND-chain of
        done_definition clauses -- if the hook that would normally frame
        it (e.g. injecting "this is Hephaestus's own goal-tracking
        mechanism") ever failed or timed out, the agent had nothing telling
        it this wasn't an ambiguous standalone user request, and stopped to
        ask for clarification with no human present to answer.

        Only meaningful for phase agents: validator/result_validator/
        diagnostic/arbitration agents work from a specialized
        validation_prompt in enriched_data (see
        AgentPromptBuilder.format_initial_message), not task.done_definition
        -- a goal built from it would describe someone else's task.
        """
        if agent_type in ("validator", "result_validator", "diagnostic", "arbitration"):
            return
        condition = (task.done_definition or "").strip()
        if not condition:
            return

        # Name this phase's actual resolved input file(s) in the goal
        # condition itself, not just in the (separately-delivered, best-
        # effort) "INPUTS AVAILABLE" manifest -- the self-checked-completion
        # hook re-evaluates the goal on every attempted stop, so a named
        # file the agent never engaged with keeps failing the check instead
        # of silently going unread. Same class of enforcement as the
        # server-side verify_requirements_cover_scope_cli_flags/
        # verify_development_produced_a_commit hard floors, at the CLI's
        # own self-check layer instead of only at task-completion time.
        try:
            with get_db() as _db:
                _phase = resolve_task_phase(_db, task)
                if _phase and task.workflow_id:
                    from src.autopilot.spec import load_phase_inputs, resolve_phase_input
                    from src.core.database import Workflow

                    _wf = _db.query(Workflow).filter_by(id=task.workflow_id).first()
                    if _wf and _wf.working_directory:
                        declared = load_phase_inputs(task.workflow_id).get(_phase.name) or {}
                        names = (declared.get("required") or []) + (declared.get("optional") or [])
                        resolved_names = []
                        for name in names:
                            found = resolve_phase_input(_wf.working_directory, name, task.workflow_id)
                            if found:
                                resolved_names.append(found.name)
                        if resolved_names:
                            condition += (
                                f" AND actually read and resolved {', '.join(resolved_names)} "
                                "(a mention in passing does not count -- incorporate its content into your work)"
                            )
        except Exception as e:
            logger.warning(f"Could not resolve input file names for task {task.id[:8]}'s goal condition: {e}")

        # done_definition is written as pure success criteria (an AND-chain
        # of what must be true for "done") -- it has no clause for a
        # legitimate give-up. When the task genuinely can't succeed for a
        # reason outside the agent's control (e.g. git_expert blocked by an
        # unrelated open bug ticket the merge gate correctly refuses to
        # ignore), the agent has nowhere to go: update_task_status(failed)
        # is a real, supported terminal status, but the goal condition as
        # written only ever matches "done". The CLI's own self-checked-
        # completion hook then refuses to let the turn end, since by
        # definition the goal was never met -- it just cycles "goal not
        # met -- continuing" until the CLI's own stop-hook block cap gives
        # up and ends the turn anyway, leaving the task stuck in_progress
        # server-side with an idle agent nothing will ever nudge again.
        # Observed live: task 7ef17b96 (git_expert) deadlocked exactly this
        # way over ticket-6de20f94. Appending an explicit OR-escape makes a
        # legitimate failed-with-reason call satisfy the goal too, so the
        # hook lets the turn end the moment the agent actually calls it,
        # instead of only after however many blocked-turn retries it takes
        # to hit the CLI's cap.
        condition = (
            f"{condition} OR the task has been legitimately marked failed "
            "via update_task_status(status='failed') with a clear reason "
            "recorded (e.g. blocked by something outside this task's "
            "control, like an open bug ticket only a different task can "
            "resolve)"
        )
        goal_command = cli_agent.format_goal_command(condition)
        if not goal_command:
            return

        if cli_agent.needs_chunked_delivery:
            chunk_size = 2500
            for i in range(0, len(goal_command), chunk_size):
                pane.send_keys(goal_command[i : i + chunk_size], enter=False)
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.5)
            pane.send_keys("", enter=True)
        else:
            pane.send_keys(goal_command, enter=True)
        logger.info(f"[GOAL] Set /goal for task {task.id[:8]} ({len(condition)} chars)")
        await asyncio.sleep(3)

    async def _verify_instructions_file_read(self, pane, instructions_rel_path: str, agent_id: str) -> None:
        """Best-effort signal that the agent actually opened its
        instructions file, not just that the pointer text was delivered.
        Most CLIs echo the path of a file they Read/cat as part of their
        own tool-call rendering, so a short wait followed by one capture-
        pane check catches the common "agent never touched the file"
        failure mode. Logs a warning only -- never raises or retries, since
        this is a heuristic across CLIs with very different TUI rendering,
        not a reliable contract.
        """
        await asyncio.sleep(15)
        try:
            captured = pane.cmd("capture-pane", "-p", "-S", "-200").stdout
            output = "\n".join(captured) if captured else ""
        except Exception:
            return
        filename = instructions_rel_path.rsplit("/", 1)[-1]
        if filename not in output and instructions_rel_path not in output:
            logger.warning(
                f"[INSTRUCTIONS-CHECK] Agent {agent_id[:8]} shows no sign of having opened {instructions_rel_path} within 15s of the pointer being sent -- it may be idle instead of working."
            )

    def _gather_worktree_context(self, task: Task) -> Dict[str, str]:
        """Collect inbound context to copy into the worktree's .hephaestus/ dir.

        The backend (unsandboxed) reads out-of-tree inputs — the design document,
        qa_spec, and project context from the workflow's launch params — and writes
        them inside the agent's worktree so the agent never reads outside its CWD.

        Returns a {relative_path: content} map written under <worktree>/.hephaestus/.
        """
        context: Dict[str, str] = {}
        try:
            from src.core.database import Workflow

            workflow_id = getattr(task, "workflow_id", None)
            launch_params = {}
            if workflow_id:
                session = self.db_manager.get_session()
                try:
                    wf = session.query(Workflow).filter_by(id=workflow_id).first()
                    if wf and wf.launch_params:
                        launch_params = wf.launch_params if isinstance(wf.launch_params, dict) else {}
                finally:
                    session.close()

            # Design document — the key external input (phase 1 extracts from it,
            # phase 8 re-validates against it).
            design_doc = launch_params.get("design_document")
            if design_doc:
                p = Path(design_doc)
                if p.exists() and p.is_file():
                    try:
                        context["spec.md"] = p.read_text()
                    except Exception as e:
                        logger.warning(f"Could not read design document {p}: {e}")

            # Project context (free-form notes from the orchestrator).
            project_context = launch_params.get("project_context")
            if project_context:
                context["context.md"] = str(project_context)

            # Spec gate (hybrid completion gate, §9.1). Per-project file lives in the
            # global state dir; copied in so QA/validation agents can read it.
            spec_path = Path(AUTOPILOT_STATE_DIR) / "qa_spec.json"
            if spec_path.exists():
                try:
                    context["qa_spec.json"] = spec_path.read_text()
                except Exception:
                    pass

            # Architecture context for architect-as-adversarial-reviewer (§10.1.1).
            # When the architect is re-invoked for phase 4, it needs access to the
            # architecture.md and requirements.md from previous phases.
            # These are in the shared worktree's docs/ directory.
            if workflow_id:
                session2 = self.db_manager.get_session()
                try:
                    wf2 = session2.query(Workflow).filter_by(id=workflow_id).first()
                    if wf2 and wf2.working_directory:
                        worktree_docs = Path(wf2.working_directory) / "docs"
                        if worktree_docs.exists():
                            arch_path = worktree_docs / "architecture.md"
                            if arch_path.exists():
                                try:
                                    context["architecture.md"] = arch_path.read_text()
                                except Exception:
                                    pass
                            req_path = worktree_docs / "requirements.md"
                            if req_path.exists():
                                try:
                                    context["requirements.md"] = req_path.read_text()
                                except Exception:
                                    pass
                finally:
                    session2.close()
        except Exception as e:
            logger.warning(f"Failed to gather worktree context for task {getattr(task, 'id', '?')}: {e}")

        return context

    def _format_initial_message(
        self,
        task: Task,
        agent_id: str,
        branch_path: str = None,
        agent_type: str = "phase",
        enriched_data: dict = None,
    ) -> str:
        """Format the initial message to send to the agent.

        Delegates to AgentPromptBuilder (SOLID review 3.1) — kept as a
        public method here since tests call it directly on the AgentManager
        instance, and some of them mutate self.phase_manager post-construction
        expecting this to see the new value, hence the re-sync below.
        """
        self._prompt_builder.phase_manager = self.phase_manager
        return self._prompt_builder.format_initial_message(
            task=task,
            agent_id=agent_id,
            branch_path=branch_path,
            agent_type=agent_type,
            enriched_data=enriched_data,
        )

    async def _verify_prompt_delivery(self, pane, verification_string: str, wait_seconds: int = 10) -> bool:
        """Verify that a prompt was delivered to the agent.

        Args:
            pane: tmux pane object
            verification_string: String to look for in output
            wait_seconds: Seconds to wait before checking

        Returns:
            True if verification string found, False otherwise
        """
        await asyncio.sleep(wait_seconds)
        output = pane.cmd("capture-pane", "-p", "-S", "-1000").stdout
        output_text = "\n".join(output) if output else ""
        return verification_string in output_text

    async def _record_cli_session(self, cli_agent, session_id: str, working_directory: Optional[str], launched_at: float) -> None:
        """Persist a CLI session after its transcript has been flushed."""
        if not session_id or not working_directory:
            return
        for attempt in range(5):
            if cli_agent.record_session(session_id, working_directory, launched_at):
                return
            if attempt < 4:
                await asyncio.sleep(1)
        logger.warning("Could not record CLI session %s after prompt delivery", session_id)

    async def _send_initial_prompt_with_retry(
        self,
        pane,
        cli_agent,
        cli_type: str,
        initial_message: str,
        agent_id: str,
        task_id: str,
        max_retries: int = 3,
        verify_delivery: bool = False,
    ) -> None:
        """Send initial prompt with optional verification and retry.

        Args:
            pane: tmux pane object
            cli_agent: CLI agent interface instance
            cli_type: Type of CLI agent (claude, opencode, etc.)
            initial_message: The initial message to send
            agent_id: Agent ID for logging
            task_id: Task ID for verification
            max_retries: Maximum number of retry attempts (only used if verify_delivery=True)
            verify_delivery: Whether to verify delivery and retry on failure (default: False)

        Raises:
            Exception: If verify_delivery=True and all retries fail
        """
        # Use Task ID as verification string (always present in initial message)
        verification_string = f"Task ID: {task_id}"

        # Check if this is OpenCode (prompt already loaded via -p flag)
        is_opencode = cli_type == "opencode"

        # Whether this CLI agent needs chunked delivery is now a property of
        # the CLIAgentInterface implementation (cli_agent.needs_chunked_delivery/
        # display_name) instead of isinstance-checking concrete classes here —
        # a new chunked-delivery CLI opts in on its own class instead of this
        # method needing to know about it (SOLID review 3.3).

        # If verification is disabled, just send once and return
        if not verify_delivery:
            if is_opencode:
                # OpenCode: Prompt already loaded via -p flag, just send Enter after 5 seconds
                logger.info("OpenCode agent: Prompt loaded via -p flag, waiting 5 seconds then sending Enter")
                await asyncio.sleep(5)
                pane.send_keys("", enter=True)  # Send Enter to submit the prompt
                logger.info(f"OpenCode: Enter sent to agent {agent_id}")
            elif cli_agent.needs_chunked_delivery:
                # Send in chunks to avoid tmux buffer issues with large prompts
                agent_name = cli_agent.display_name
                logger.info(f"Sending initial prompt to {agent_name} agent {agent_id} (verification disabled)")
                formatted_message = cli_agent.format_message(initial_message)

                chunk_size = 2500  # characters per chunk
                num_chunks = (len(formatted_message) + chunk_size - 1) // chunk_size
                logger.info(f"{agent_name} agent: Sending prompt in {num_chunks} chunks ({len(formatted_message)} total chars)")

                for i in range(0, len(formatted_message), chunk_size):
                    chunk = formatted_message[i : i + chunk_size]
                    # enter=False is required: libtmux's send_keys defaults
                    # enter=True, which SUBMITS each chunk as its own message.
                    # Observed live with pi: the agent started working off the
                    # first 2500-char fragment alone, while every later chunk
                    # arrived mid-run and queued up as a garbled mid-word
                    # "Steering:" message.
                    pane.send_keys(chunk, enter=False)
                    await asyncio.sleep(0.2)  # Delay between chunks to avoid overwhelming tmux

                # Now send Enter to submit the entire message
                logger.info("All chunks sent, submitting message with Enter")
                await asyncio.sleep(0.5)  # Brief pause before Enter
                pane.send_keys("", enter=True)  # This sends just the Enter key
                logger.info(f"Initial prompt sent to {agent_name} agent {agent_id}")
            else:
                # Other agents: Send entire prompt in one go
                logger.info(f"Sending initial prompt to agent {agent_id} (verification disabled)")
                formatted_message = cli_agent.format_message(initial_message)
                logger.info(f"Non-Claude agent: Sending entire prompt in one message ({len(formatted_message)} chars)")
                pane.send_keys(formatted_message, enter=True)
                logger.info(f"Initial prompt sent to agent {agent_id}")

            # Check for a CLI-level session/rate-limit rejection right after
            # delivery. This is the only call path that actually runs
            # (verify_delivery defaults to False at every real call site, so
            # this same check placed in the verify_delivery=True branch
            # below is unreachable dead code). Claude Code prints "You've
            # hit your session limit" and otherwise just sits idle -- no
            # exception, no non-zero exit -- so this pane-text check is the
            # only way to notice at all. Anchored to that confirmed exact
            # phrase (not the bare fragment "you've hit", which is generic
            # enough to risk matching unrelated prose) plus a couple of
            # other CLIs' likely wording; bare "429" is deliberately
            # excluded -- a bare 3-digit number is too likely to appear
            # incidentally in the freshly-echoed task prompt itself (this
            # codebase's own task prompts routinely discuss HTTP status
            # codes and rate limits).
            await asyncio.sleep(3)  # let the CLI print any rejection message
            try:
                output = "\n".join(pane.cmd("capture-pane", "-p", "-S", "-50").stdout)
                # Scope the check to content AFTER this delivery's own
                # verification_string (the freshly-echoed "Task ID: ..."
                # line every prompt starts with) -- not the whole last-50-
                # line block. A CLI session reused across dispatches (e.g.
                # qa_validation's persistent session ID) can still have an
                # OLD, already-resolved rejection banner sitting in
                # scrollback from a PRIOR task's attempt; checking the
                # whole block wrongly treats that stale text as evidence
                # THIS delivery was rejected. Falls back to the whole
                # block if the marker isn't found (capture raced the
                # send) rather than silently skipping the check.
                marker_pos = output.rfind(verification_string)
                scoped_output = output[marker_pos:] if marker_pos != -1 else output
                output_lower = scoped_output.lower()
                # Match Claude session/rate/limit messages — weekly and monthly
                # spend limits are distinct from "session limit" and must be
                # caught here too, otherwise the agent starts "successfully"
                # and the monitor has to catch it on a later cycle (if it even
                # runs before the agent is terminated another way). Anchored to
                # confirmed exact phrases to avoid false-positive on prompt text.
                for indicator in (
                    "you've hit your session limit",
                    "you've hit your weekly limit",
                    "you've hit your monthly limit",
                    "too many requests, please slow down",
                    # Claude Code's rolling-usage-window banner ("Usage
                    # limit reached · continuing automatically at 3:10pm")
                    # -- same failure class, but auto-continues instead of
                    # erroring, so it needs its own indicator (see
                    # patterns.py's _USAGE_LIMIT_RE for the mid-task
                    # equivalent of this same gap).
                    "usage limit reached",
                ):
                    if indicator in output_lower:
                        raise Exception(f"CLI session limit detected: '{indicator}' found in output")
            except Exception as check_err:
                if "CLI session limit detected" in str(check_err):
                    raise
                # Non-critical check failure (e.g. capture-pane raced the
                # session closing) -- don't fail agent creation over it.

            return

        # Verification enabled - retry loop
        for attempt in range(1, max_retries + 1):
            logger.info(f"Sending initial prompt to agent {agent_id} (attempt {attempt}/{max_retries})")

            if is_opencode:
                # OpenCode: Prompt already loaded via -p flag, just send Enter after 5 seconds
                logger.info("OpenCode agent: Prompt loaded via -p flag, waiting 5 seconds then sending Enter")
                await asyncio.sleep(5)
                pane.send_keys("", enter=True)  # Send Enter to submit the prompt
            elif cli_agent.needs_chunked_delivery:
                # Send in chunks to avoid tmux buffer issues with large prompts
                agent_name = cli_agent.display_name
                formatted_message = cli_agent.format_message(initial_message)
                chunk_size = 2000  # characters per chunk
                num_chunks = (len(formatted_message) + chunk_size - 1) // chunk_size
                logger.info(f"{agent_name} agent: Sending prompt in {num_chunks} chunks ({len(formatted_message)} total chars)")

                for i in range(0, len(formatted_message), chunk_size):
                    chunk = formatted_message[i : i + chunk_size]
                    # enter=False: see the verification-disabled branch above --
                    # libtmux defaults enter=True, which submits each chunk as
                    # its own message instead of accumulating one prompt.
                    pane.send_keys(chunk, enter=False)
                    await asyncio.sleep(0.1)  # Delay between chunks to avoid overwhelming tmux

                # Now send Enter to submit the entire message
                logger.info("All chunks sent, submitting message with Enter")
                await asyncio.sleep(0.5)  # Brief pause before Enter
                pane.send_keys("", enter=True)  # This sends just the Enter key
            else:
                # Other agents: Send entire prompt in one go
                formatted_message = cli_agent.format_message(initial_message)
                logger.info(f"Non-Claude agent: Sending entire prompt in one message ({len(formatted_message)} chars)")
                pane.send_keys(formatted_message, enter=True)

            # Verify delivery
            if await self._verify_prompt_delivery(pane, verification_string, wait_seconds=10):
                logger.info(f"✓ Initial prompt verified for agent {agent_id} on attempt {attempt}")
                return

            logger.warning(f"✗ Initial prompt NOT verified for agent {agent_id} on attempt {attempt}")

            if attempt < max_retries:
                logger.info(f"Retrying prompt delivery for agent {agent_id}...")
                await asyncio.sleep(2)  # Brief pause before retry

        # All retries failed
        error_msg = f"Failed to deliver initial prompt to agent {agent_id} after {max_retries} attempts"
        logger.error(error_msg)
        raise Exception(error_msg)

    async def create_agent_for_task(
        self,
        task: Task,
        enriched_data: Dict[str, Any],
        memories: List[Dict[str, Any]],
        project_context: str,
        cli_type: Optional[str] = None,
        working_directory: Optional[str] = None,
        agent_type: str = "phase",
        use_existing_worktree: bool = False,
        commit_sha: Optional[str] = None,
        phase_cli_tool: Optional[str] = None,
        phase_cli_model: Optional[str] = None,
        phase_glm_token_env: Optional[str] = None,
        phase_thinking_level: Optional[str] = None,
        assign_to_task: bool = False,
    ) -> Agent:
        """Create an agent for a specific task.

        Args:
            task: Task to assign to agent (REQUIRED)
            enriched_data: Enriched task data from LLM
            memories: Relevant memories from RAG
            project_context: Current project context
            cli_type: Type of CLI agent to use
            working_directory: Working directory for the agent
            agent_type: Type of agent (phase, validator, result_validator, monitor)
            use_existing_worktree: If True, use working_directory as-is without creating new worktree
            commit_sha: Specific commit to create worktree from (for validators)
            phase_cli_tool: Per-phase CLI tool override (falls back to cli_type or global default)
            phase_cli_model: Per-phase CLI model override (falls back to global default)
            phase_glm_token_env: Per-phase GLM token env variable override (falls back to global default)
            assign_to_task: If True, set task.assigned_agent_id/status="in_progress"/
                started_at in the SAME commit as the stub Agent row below, instead of
                leaving it to the caller to do afterward. Closes a real race: the stub
                Agent row (with current_task_id set) is committed here, before the slow
                worktree/tmux/prompt work below -- a caller that only assigns the task
                AFTER this method returns can lose that write entirely if the process
                dies in between (e.g. a `heph restart` landing mid-dispatch), leaving
                Agent.current_task_id correctly set but Task.assigned_agent_id
                permanently null even after the task later completes successfully.
                Observed live: exactly this sequence orphaned a task's assigned_agent_id
                forever, hiding its "view tmux output" button. Defaults to False since
                not every caller wants this task flipped to "in_progress" immediately
                (e.g. validator agents, which don't take over the reviewed task).

        Returns:
            Created agent

        Raises:
            ValueError: If task is None
        """
        if task is None:
            raise ValueError("task is REQUIRED for create_agent_for_task \u2014 cannot create agent without a task")

        # git_expert dispatches like any other phase, in review mode
        # or not -- the agent-safe-bin/git wrapper on every agent's PATH
        # (scripts/agent-safe-bin/git) is the actual guardrail: it blocks
        # `git merge` and any push targeting main/master until
        # .hephaestus/review_approved exists, but allows commit/push-to-
        # feature-branch/`gh pr create` unconditionally. The pipeline
        # itself pauses for final human review once the whole workflow
        # would otherwise be complete (see PhaseManager._complete_workflow's
        # review_mode check) -- that's the actual human-in-the-loop gate,
        # not a blanket dispatch-time block on this one phase.

        existing = self._check_duplicate_active_agent(task)
        if existing:
            return existing

        # Phase-sibling guard: don't dispatch if the phase already has
        # another active task. Protects against concurrent dispatch from
        # different code paths (orchestrator sweep, HTTP route, validator
        # spawn) targeting the same phase.
        phase_sibling = _phase_sibling_guard(self, task)
        if phase_sibling is not None:
            return None

        agent_id = str(uuid.uuid4())
        wt_mgr = self._scoped_worktree_manager(task.workflow_id)
        phase_config = self._resolve_phase_config(task, cli_type, phase_cli_tool, phase_cli_model, phase_glm_token_env, phase_thinking_level)
        cli_type = phase_config.cli_type

        from src.core.log_context import set_log_context

        set_log_context(agent=agent_id, task=task.id, workflow=task.workflow_id or "")
        logger.info(f"Creating {cli_type} agent {agent_id} for task {task.id}")

        # Insert a stub Agent row BEFORE worktree creation so the
        # agent_worktrees.agent_id FK passes (see _insert_stub_agent_row
        # for the assign_to_task same-commit race it closes and the
        # try/except/finally connection-leak history).
        agent = _insert_stub_agent_row(
            self,
            agent_id=agent_id,
            cli_type=cli_type,
            agent_type=agent_type,
            task=task,
            assign_to_task=assign_to_task,
        )

        try:
            # Three independent operations -- worktree resolution (git
            # work), system-prompt generation (an LLM round-trip), and
            # complexity classification (a second, conditional LLM
            # round-trip) -- run concurrently (see
            # _run_launch_preparations for the live 42s-stall evidence).
            wt_resolution, system_prompt, thinking_level, phase_name, phase_order = await _run_launch_preparations(
                self,
                task=task,
                wt_mgr=wt_mgr,
                working_directory=working_directory,
                enriched_data=enriched_data,
                memories=memories,
                project_context=project_context,
                phase_config=phase_config,
                agent_id=agent_id,
            )
            prep = await _prepare_tmux_and_prompt(
                self,
                cli_type=cli_type,
                task=task,
                agent_id=agent_id,
                agent_type=agent_type,
                phase_config=phase_config,
                wt_resolution=wt_resolution,
                system_prompt=system_prompt,
                enriched_data=enriched_data,
                phase_name=phase_name,
            )
            # Bound in THIS scope: the except block below's
            # `"tmux_session" in locals()` / session_name references depend
            # on exactly which step results are bound at exception time.
            tmux_session = prep.tmux_session
            session_name = prep.session_name
            launch = await _send_launch_command_and_record_agent(
                self,
                prep=prep,
                task=task,
                agent_id=agent_id,
                system_prompt=system_prompt,
                cli_type=cli_type,
                thinking_level=thinking_level,
                phase_name=phase_name,
                phase_order=phase_order,
            )

            # Some phases have real, uninterruptible work that must finish
            # before the agent's first prompt is meaningful (e.g.
            # security_review's mandatory ash scan -- the agent is meant to
            # read its results, which don't exist until the scan
            # completes). The Agent/tmux session and Task row above already
            # exist and show real, live activity for this phase -- unlike
            # the old approach (running the scan before any of that
            # existed at all), so a stuck/orphan detector reading Task.
            # status or Agent.status sees exactly what's true: dispatched,
            # working. dispatch_grace_until is the piece those detectors
            # still need explicitly: elapsed-time-based ones (Task.created_at
            # or Agent.launched_at) would otherwise judge this same window
            # as staleness on their own, shorter defaults. See
            # PRE_DISPATCH_BLOCKING_STEPS' own docstring and
            # Task.dispatch_grace_until's in database.py.
            from src.autopilot.orchestrator.worktree_integration import (
                PRE_DISPATCH_BLOCKING_STEPS,
            )

            blocking_step = PRE_DISPATCH_BLOCKING_STEPS.get(phase_name)
            if blocking_step and prep.branch_path:
                # Don't start a multi-minute blocking step for a launch
                # that's already doomed -- _deliver_initial_prompt_flow's
                # OWN termination-race check further below would still
                # catch a stop/pause that happened by now and correctly
                # abort before delivering the prompt, but only after
                # burning the full scan first. This is the cheap half of
                # that gap: a stop/pause that lands WHILE the blocking step
                # itself is running (it's a plain blocking subprocess call,
                # no cancellation point mid-flight) still isn't caught
                # until the step naturally finishes -- accepted tradeoff,
                # not fixed here.
                pre_scan_abort = await self._check_termination_race(
                    agent_id, task.id, session_name,
                    agent_id_to_return=launch.agent_id_to_return, task=task,
                )
                if pre_scan_abort is not None:
                    return pre_scan_abort

                blocking_fn, grace_seconds = blocking_step
                with self.db_manager.session_scope() as grace_session:
                    grace_task = grace_session.query(Task).filter_by(id=task.id).first()
                    if grace_task:
                        grace_task.dispatch_grace_until = utc_now() + timedelta(seconds=grace_seconds)
                await asyncio.get_event_loop().run_in_executor(
                    None, blocking_fn, Path(prep.branch_path), logger
                )

            term_race_result = await _deliver_initial_prompt_flow(
                self,
                prep=prep,
                launch=launch,
                task=task,
                agent_id=agent_id,
                system_prompt=system_prompt,
                cli_type=cli_type,
            )
            if term_race_result is not None:
                return term_race_result

            class AgentInfo:
                def __init__(self, id):
                    self.id = id

            return AgentInfo(launch.agent_id_to_return)

        except Exception as e:
            logger.error(f"Failed to create agent with {cli_type}: {e}")
            if phase_config.fallback_cli_tool and phase_config.fallback_cli_tool != cli_type:
                logger.warning(f"Primary CLI tool '{cli_type}' failed, trying fallback: {phase_config.fallback_cli_tool}/{phase_config.fallback_cli_model or 'default'}")
                try:
                    if "tmux_session" in locals():
                        try:
                            tmux_session.kill_session()
                        except Exception as e:
                            logger.warning(f"Failed to kill stale tmux session before CLI fallback; it may linger: {e}")
                    if "agent_id" in locals():
                        try:
                            from src.autopilot.orchestrator.engine_client import (
                                terminate_agent,
                            )

                            with self.db_manager.get_session() as _cs:
                                # Also releases the task back to "pending"
                                # with no agent, which is the state the
                                # fallback create_agent_for_task below
                                # expects to claim it from.
                                terminate_agent(agent_id, session=_cs)
                                _cs.commit()
                        except Exception as e:
                            logger.warning(f"Failed to terminate agent {agent_id} before CLI fallback; its task may not be claimable: {e}")
                        try:
                            wt_mgr.discard_agent(agent_id)
                        except Exception as e:
                            logger.warning(f"Failed to discard worktree for agent {agent_id} before CLI fallback: {e}")
                    return await self.create_agent_for_task(
                        task=task,
                        enriched_data=enriched_data,
                        memories=memories,
                        project_context=project_context,
                        cli_type=phase_config.fallback_cli_tool,
                        working_directory=working_directory,
                        agent_type=agent_type,
                        use_existing_worktree=use_existing_worktree,
                        commit_sha=commit_sha,
                        phase_cli_tool=phase_config.fallback_cli_tool,
                        phase_cli_model=phase_config.fallback_cli_model,
                        phase_glm_token_env=phase_config.glm_token_env,
                        phase_thinking_level=phase_config.thinking_level,
                    )
                except Exception as fallback_error:
                    logger.error(f"Fallback '{phase_config.fallback_cli_tool}' also failed: {fallback_error}")
                    e = fallback_error

            try:
                if "tmux_session" in locals():
                    tmux_session.kill_session()
                    logger.info(f"Killed tmux session {session_name}")
            except Exception as cleanup_error:
                logger.error(f"Failed to kill tmux session during cleanup: {cleanup_error}")

            try:
                cleanup_session = self.db_manager.get_session()
                try:
                    if "agent_id" in locals():
                        from src.autopilot.orchestrator.engine_client import (
                            terminate_agent,
                        )

                        if terminate_agent(agent_id, session=cleanup_session):
                            logger.info(f"Marked agent {agent_id} as terminated")
                    task_record = cleanup_session.query(Task).filter_by(id=task.id).first()
                    if task_record:
                        task_record.status = "failed"
                        task_record.failure_reason = f"Agent creation failed: {str(e)}"
                        task_record.completed_at = utc_now()
                        logger.info(f"Marked task {task.id} as failed")
                        if "CLI session limit detected" in str(e) and task_record.workflow_id:
                            from src.core.database import Workflow as _Workflow

                            workflow_record = cleanup_session.query(_Workflow).filter_by(id=task_record.workflow_id).first()
                            if workflow_record and workflow_record.status != "paused":
                                from src.autopilot.orchestrator.engine_client import pause_workflow

                                pause_workflow(
                                    task_record.workflow_id,
                                    reason="system",
                                    status_reason=(f"CLI session limit hit ({cli_type}), no working fallback -- will auto-resume on its own retry cooldown once the limit resets"),
                                    session=cleanup_session,
                                )
                                logger.warning(f"[SESSION-LIMIT] Pausing workflow {task_record.workflow_id[:8]} -- {cli_type} session limit hit with no working fallback")
                    cleanup_session.commit()
                except Exception as db_error:
                    logger.error(f"Failed to update database during cleanup: {db_error}")
                    cleanup_session.rollback()
                finally:
                    cleanup_session.close()
            except Exception as session_error:
                logger.error(f"Failed to get database session during cleanup: {session_error}")
            raise

    async def restart_agent(self, agent_id: str, reason: str = ""):
        """Restart a stuck agent.

        Args:
            agent_id: ID of agent to restart
            reason: Reason for restart
        """
        logger.info(f"Restarting agent {agent_id}: {reason}")

        session = self.db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                logger.warning(f"Agent {agent_id} not found")
                return

            # Restart loop protection: max 3 restarts per agent
            if agent.restart_count >= 3:
                logger.warning(f"Agent {agent_id[:8]} exceeded max restarts ({agent.restart_count}), terminating")
                # Capture the task before terminating -- the primitive
                # clears current_task_id as part of the invariant.
                task_id = agent.current_task_id
                tmux_session_name = agent.tmux_session_name
                from src.autopilot.orchestrator.engine_client import terminate_agent

                terminate_agent(agent.id, session=session)
                session.commit()
                # terminate_agent is DB-only -- it never touches the actual
                # tmux session (see its own docstring). Every OTHER branch
                # in this method that terminates an agent flushes and kills
                # its session (see the normal-restart path below); this
                # early-return one didn't, leaving the CLI process alive
                # and unaware it had been terminated -- it can keep working
                # for hours, finish real work, and later get rejected
                # ("Agent not authenticated") when it tries to report,
                # while a freshly-dispatched replacement redoes the same
                # task. Same bug class independently found and fixed in
                # OrphanSessionReaper.cleanup_orphaned_tmux_sessions.
                if tmux_session_name:
                    try:
                        transcript_dir = self._output_capture._resolve_tmux_transcript_dir(agent)
                        if transcript_dir:
                            self._output_capture._flush_stable_transcript(
                                tmux_session_name,
                                transcript_dir / f"{tmux_session_name}.clean.log",
                            )
                    except Exception as e:
                        logger.debug(f"[STABLE-TRANSCRIPT] Final flush before terminate failed: {e}")
                    try:
                        tmux_session = self._output_capture._find_tmux_session(tmux_session_name)
                        if tmux_session:
                            tmux_session.kill_session()
                    except Exception as e:
                        logger.warning(
                            f"Failed to kill tmux session {tmux_session_name} "
                            f"after exceeding max restarts: {e}"
                        )
                task = session.query(Task).filter_by(id=task_id).first()
                if task and task.status not in ("done", "failed"):
                    task.status = "failed"
                    task.failure_reason = f"Agent exceeded max restarts ({agent.restart_count})"
                    session.commit()
                return

            agent.restart_count = (agent.restart_count or 0) + 1
            agent.health_check_failures = 0
            session.commit()

            task = session.query(Task).filter_by(id=agent.current_task_id).first()
            if not task:
                logger.error(f"Task {agent.current_task_id} not found")
                return

            # Kill existing tmux session
            if agent.tmux_session_name:
                try:
                    transcript_dir = self._output_capture._resolve_tmux_transcript_dir(agent)
                    if transcript_dir:
                        self._output_capture._flush_stable_transcript(
                            agent.tmux_session_name,
                            transcript_dir / f"{agent.tmux_session_name}.clean.log",
                        )
                except Exception as e:
                    logger.debug(f"[STABLE-TRANSCRIPT] Final flush before restart failed: {e}")

                try:
                    tmux_session = self._output_capture._find_tmux_session(agent.tmux_session_name)
                    if tmux_session:
                        tmux_session.kill_session()
                except Exception as e:
                    logger.warning(f"Failed to kill tmux session {agent.tmux_session_name} before restart; the old agent may still be running: {e}")

            # Resolve env vars and model (restart path: uses agent's frozen values)
            env_vars, model, cli_agent = self._resolve_env_and_model(
                agent.cli_type,
                task,
                agent_id,
                label="restarted agent",
                agent_cli_model=agent.cli_model,
            )

            # Resolve worktree (restart: create_if_missing=False, silent None)
            loop = asyncio.get_event_loop()
            wt_resolution = await loop.run_in_executor(
                None,
                functools.partial(
                    self._resolve_worktree,
                    task,
                    self.branch_manager,
                    create_if_missing=False,
                    agent_id=agent_id,
                ),
            )
            restart_wd = wt_resolution.branch_path

            new_session_name = f"{self.config.agents.tmux_session_prefix}_{agent_id[:8]}_r"

            # Resolve phase name BEFORE preparing the launch environment: the
            # .hephaestus/ output dir is named by phase NAME (pre-split
            # behavior), not by the raw phase_id.
            restart_phase_name = None
            if task.phase_id:
                restart_session = self.db_manager.get_session()
                try:
                    restart_phase = resolve_task_phase(restart_session, task)
                    if restart_phase:
                        restart_phase_name = restart_phase.name
                except Exception as e:
                    logger.warning(f"Failed to resolve phase name for restarted agent {agent_id}; output dir will be named by phase_id: {e}")
                finally:
                    restart_session.close()

            # Prepare launch environment
            if restart_wd:
                cli_agent.prepare_working_directory(restart_wd)
            tmux_session = await loop.run_in_executor(
                None,
                functools.partial(
                    self._prepare_launch_environment,
                    new_session_name,
                    restart_wd,
                    env_vars,
                    task,
                    restart_phase_name,
                    cli_agent=cli_agent,
                    prewarm_codegraph=False,
                ),
            )

            restart_system_prompt = (
                f"\u26a0\ufe0f RESTART: You were restarted because: {reason}. "
                f"Continue working on task {task.id}. "
                f"Do NOT re-read files you already analyzed. Pick up where you left off.\n\n"
                f"{agent.system_prompt}"
            )

            # Session ID for restart (excludes validator/result_validator/diagnostic, NOT arbitration)
            session_id = self._resolve_session_id(
                task,
                agent.agent_type or "phase",
                restart_phase_name,
                model,
                excluded_types=("validator", "result_validator", "diagnostic"),
            )

            restart_message = (
                f"\u26a0\ufe0f You were restarted ({reason}). Your prior work is committed in this "
                f"worktree \u2014 do NOT redo it; run `git log` / `git status` and inspect existing "
                f"files first, then continue toward completion.\n\n" + self._format_initial_message(task, agent_id, agent_type=(agent.agent_type or "phase"))
            )
            instructions_pointer = ""
            if restart_wd:
                instructions_rel_path = self._write_task_instructions(restart_wd, task.id, restart_message)
                instructions_pointer = self._build_instructions_pointer(
                    task.id,
                    instructions_rel_path,
                    restarted=True,
                    agent_name=f"hephaestus-{restart_phase_name.replace('_', '-')}" if restart_phase_name else None,
                )
                if agent.cli_type == "codex" and session_id:
                    instructions_pointer += f"\nHephaestus Session ID: {session_id}"

            # Build and send launch command
            launch_result, pane, cli_launch_started_at = await self._build_and_send_launch_command(
                cli_agent,
                tmux_session,
                system_prompt=restart_system_prompt,
                task=task,
                model=model,
                thinking_level=None,
                phase_name=restart_phase_name,
                agent_id=agent_id,
                session_id=session_id,
                working_directory=restart_wd,
                instructions_pointer=instructions_pointer,
                env_vars=env_vars,
                label=f"restarted agent {agent_id[:8]}",
            )
            # This exact path is the one _wait_for_shell_ready's own
            # docstring cites as "Observed live" corrupting the launch
            # line -- _build_and_send_launch_command only calls
            # _wait_for_shell_ready-equivalent protection (via
            # _export_env_vars_and_verify's readback) when env_vars is
            # non-empty; a restart with none skipped straight from pane
            # creation to sending launch_result.command with no wait at
            # all, not even a flat sleep. Confirm the shell has settled
            # before shipping it, same fix as create_agent_for_task's
            # identical gap (_create_agent_for_task_steps.py).
            await loop.run_in_executor(None, self._wait_for_shell_ready, pane)
            pane.send_keys(launch_result.command, enter=True)

            restart_cli_type = agent.cli_type
            restart_task_id = task.id

            if (
                launch_result.prompt_delivery
                in (
                    LaunchResult.AGENT_FILE,
                    LaunchResult.DEFERRED,
                )
                and restart_system_prompt
            ):
                restart_message = restart_system_prompt + "\n\n---\n\n" + restart_message
                if restart_wd:
                    self._write_task_instructions(restart_wd, task.id, restart_message)

            agent.tmux_session_name = new_session_name
            agent.status = "working"
            agent.health_check_failures = 0
            agent.last_activity = utc_now()
            agent.launched_at = utc_now()

            log_entry = AgentLog(
                agent_id=agent_id,
                log_type="restarted",
                message=f"Agent restarted: {reason}",
                details={"new_session": new_session_name},
            )
            session.add(log_entry)
            session.commit()

            try:
                cli_ready = await self._wait_for_cli_ready(pane, cli_agent, restart_cli_type, agent_id)
                term_race_result = await self._check_termination_race(
                    agent_id,
                    restart_task_id,
                    new_session_name,
                    agent_id_to_return=agent_id,
                )
                if term_race_result is not None:
                    return term_race_result
                if not cli_ready:
                    self._detect_launch_failure(pane, cli_agent, restart_cli_type, new_session_name)
                if self.tmux_server.has_session(new_session_name):
                    await self._deliver_initial_prompt(
                        pane,
                        cli_agent,
                        restart_cli_type,
                        instructions_pointer if restart_wd else restart_message,
                        agent_id,
                        task,
                        agent_type=agent.agent_type or "phase",
                    )
                    # See the create-path's identical comment: neither call
                    # reads the other's result, so run them concurrently
                    # when both apply (restart_wd gates whether there's an
                    # instructions file to check at all).
                    record_coro = self._record_cli_session(cli_agent, session_id, restart_wd, cli_launch_started_at)
                    if restart_wd:
                        await asyncio.gather(
                            record_coro,
                            self._verify_instructions_file_read(pane, instructions_rel_path, agent_id),
                        )
                    else:
                        await record_coro
                    logger.info(f"[RESTART] Delivered continue-prompt to agent {agent_id} ({restart_cli_type})")
                else:
                    logger.error(f"[RESTART] Session {new_session_name} died before prompt delivery")
            except Exception as e:
                logger.error(f"[RESTART] Failed to deliver continue-prompt to agent {agent_id}: {e}")

            logger.info(f"Agent {agent_id} restarted successfully")

        except Exception as e:
            logger.error(f"Failed to restart agent {agent_id}: {e}")
            session.rollback()
        finally:
            session.close()
