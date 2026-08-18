"""Agent launch pipeline — worktree resolution, tmux session creation, prompt delivery, and the create/restart orchestrators. Extracted from AgentManager per design_docs/manager_py_decomposition_prompt.md."""

import asyncio
import logging
import shlex
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import libtmux

from src.core.constants import AUTOPILOT_STATE_DIR, CONTEXT_DIR_NAME, DESIGN_CONTEXT_SUBDIR
from src.core.database import (
    Agent,
    AgentLog,
    BoardConfig,
    Task,
    TaskStatus,
    get_db,
)
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

        token_env_var = glm_token_env or getattr(
            self.config, "glm_api_token_env", "GLM_API_TOKEN"
        )
        token = os.getenv(token_env_var)
        if not token:
            logger.warning(
                f"GLM model configured but {token_env_var} not found, using standard Claude"
            )
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
            elif (
                hasattr(self, "phase_manager")
                and self.phase_manager
                and hasattr(self.phase_manager, "workflow_id")
            ):
                workflow_id = self.phase_manager.workflow_id
            else:
                with get_db() as db:
                    board_config = (
                        db.query(BoardConfig).filter_by(ticket_human_review=True).first()
                    )
                    if board_config:
                        workflow_id = board_config.workflow_id

            if not workflow_id:
                return None

            with get_db() as db:
                board_config = (
                    db.query(BoardConfig).filter_by(workflow_id=workflow_id).first()
                )
                if board_config and board_config.ticket_human_review:
                    timeout_seconds = board_config.approval_timeout_seconds or 1800
                    timeout_ms = timeout_seconds * 1000
                    logger.info(
                        f"Human approval enabled for workflow {workflow_id}: "
                        f"Setting MCP_TOOL_TIMEOUT={timeout_ms}ms ({timeout_seconds}s) for {label}"
                    )
                    return timeout_ms
        except Exception as e:
            logger.warning(
                f"Failed to check board config for MCP_TOOL_TIMEOUT ({label}): {e}"
            )
        return None

    def _resolve_project_base_dir(self, workflow_id: Optional[str]) -> Optional[Path]:
        """Resolve workflow_id's project base_dir via Workflow.project_id ->
        AutopilotProject.base_dir. Never raises -- returns None on any
        lookup failure (no workflow_id, workflow/project row missing, or no
        project_id) so callers can fall back to today's default-instance
        behavior instead of erroring.
        """
        if not workflow_id:
            return None
        try:
            from src.core.database import AutopilotProject, Workflow

            session = self.db_manager.get_session()
            try:
                wf = session.query(Workflow).filter_by(id=workflow_id).first()
                if not wf or not wf.project_id:
                    return None
                proj = (
                    session.query(AutopilotProject)
                    .filter_by(id=wf.project_id)
                    .first()
                )
                if not proj or not proj.base_dir:
                    return None
                return Path(proj.base_dir)
            finally:
                session.close()
        except Exception as e:
            logger.warning(
                f"[WORKTREE] Could not resolve project for workflow {workflow_id}: {e}"
            )
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
        """
        base_dir = self._resolve_project_base_dir(workflow_id)
        if base_dir is None:
            return self.branch_manager
        wt_mgr = WorktreeManager(db_manager=self.db_manager)
        wt_mgr.reload(base_dir)
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
                capture_output=True, timeout=5,
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
        self, agent_id: str, task_id: str, session_name: str, agent_id_to_return: str
    ) -> Optional[object]:
        """Check whether the agent or task was terminated/cancelled during
        the CLI-init sleep. Returns an AgentInfo if launch should be
        aborted; None if safe to proceed.

        Currently create-only but called by both create and restart after
        extraction (documented gap-closing for restart).
        """
        with self.db_manager.get_session() as _term_check:
            _current = _term_check.query(Agent).filter_by(id=agent_id).first()
            _agent_terminated = bool(_current and _current.status == "terminated")

            _fresh_task = _term_check.query(Task).filter_by(id=task_id).first()
            _task_cancelled = bool(
                _fresh_task
                and (
                    _fresh_task.status in TaskStatus.TERMINAL
                    or (
                        _fresh_task.assigned_agent_id
                        and _fresh_task.assigned_agent_id != agent_id
                    )
                )
            )

            if _agent_terminated or _task_cancelled:
                reason = (
                    "was terminated"
                    if _agent_terminated
                    else f"its task {task_id} was reassigned/cancelled "
                    f"(status={_fresh_task.status}, assigned_agent_id="
                    f"{_fresh_task.assigned_agent_id})"
                )
                logger.warning(
                    f"Agent {agent_id} {reason} while its CLI was still "
                    "initializing -- aborting launch, not delivering initial prompt"
                )
                if self.tmux_server.has_session(session_name):
                    self.tmux_server.kill_session(session_name)

                class AgentInfo:
                    def __init__(self, id):
                        self.id = id

                return AgentInfo(agent_id_to_return)
        return None

    def _detect_launch_failure(
        self, pane, cli_agent, cli_type: str, session_name: str
    ) -> None:
        """Detect whether the CLI's launch command was rejected by the
        shell or by the CLI itself, leaving a dead pane.  Uses
        cli_agent.get_launch_rejection_patterns() — the base generic
        patterns plus any CLI-specific wording the subclass overrides.

        Detects which pattern fired to preserve distinct error messages
        (generic shell rejection vs. CLI-specific confirmation dialog).
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
                    logger.error(
                        f"{cli_type} launch command is stuck on an unhandled confirmation "
                        f"dialog in tmux session {session_name}: "
                        f"{launch_check_text.strip()[-300:]}")
                    raise Exception(
                        f"{cli_type} CLI is stuck on an unhandled first-run confirmation "
                        "dialog"
                    )
                logger.error(
                    f"{cli_type} launch command failed in tmux session {session_name}: "
                    f"{launch_check_text.strip()[-300:]}")
                raise Exception(
                    f"{cli_type} CLI failed to start -- shell reported the launch "
                    "command was not found"
                )

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
                logger.warning(
                    f"Agent {existing.id[:8]} already active for task "
                    f"{task.id[:8]} — skipping duplicate creation"
                )
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
        if task.phase_id and (
            phase_cli_tool is None
            and phase_cli_model is None
            and phase_glm_token_env is None
            and phase_thinking_level is None
        ):
            try:
                from src.core.database import Phase
                with self.db_manager.get_session() as _ps:
                    _ph = _ps.query(Phase).filter_by(id=task.phase_id).first()
                    if _ph:
                        phase_cli_tool = _ph.cli_tool
                        phase_cli_model = _ph.cli_model
                        fallback_cli_tool = getattr(_ph, 'fallback_cli_tool', None)
                        fallback_cli_model = getattr(_ph, 'fallback_cli_model', None)
                        phase_glm_token_env = _ph.glm_api_token_env
                        phase_thinking_level = _ph.thinking_level
            except Exception as e:
                logger.warning(f"Could not derive phase config for task {task.id}: {e}")

        cli_type = phase_cli_tool or cli_type or self.config.default_cli_tool

        if not fallback_cli_tool and self.config.default_fallback_cli_tool:
            if self.config.default_fallback_cli_tool != cli_type:
                fallback_cli_tool = self.config.default_fallback_cli_tool
                fallback_cli_model = self.config.default_fallback_cli_model

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
                            logger.info(
                                f"Using shared worktree for agent {agent_id[:8]} "
                                f"at {branch_path}"
                            )
                    elif not create_if_missing and Path(wf.working_directory).exists():
                        # Restart-only: non-worktrees working directory (e.g.
                        # legacy or direct path) — use it if it exists on disk.
                        # Create deliberately does NOT take this branch: it
                        # always forks an isolated per-agent worktree for
                        # non-shared workflows (pre-split behavior).
                        branch_path = wf.working_directory
                        branch_name = f"shared-{task.workflow_id[:8]}"
                        logger.info(
                            f"Using workflow working directory for agent {agent_id[:8]} "
                            f"at {branch_path}"
                        )

        if branch_path is None and create_if_missing and context_files is not None:
            branch_info = wt_mgr.create_agent_worktree(
                agent_id=agent_id,
                parent_agent_id=getattr(task, "created_by_agent_id", None),
                context_files=resolved_context,
            )
            branch_path = branch_info["working_directory"]
            branch_name = branch_info["branch_name"]
            wt_mgr.switch_to_branch(branch_name)
            logger.info(
                f"Created worktree {branch_name} for agent {agent_id[:8]} "
                f"at {branch_path}"
            )
        elif branch_path is None and not create_if_missing:
            # restart fallback: agent's own tracked worktree
            try:
                candidate = self.branch_manager.get_agent_branch_path(agent_id)
                if candidate and Path(candidate).exists():
                    branch_path = candidate
            except Exception as e:
                logger.debug(
                    f"[RESTART] Could not resolve agent branch path for "
                    f"{agent_id[:8]}: {e}"
                )

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
        global_model = (
            getattr(self.config, "cli_model", None)
            if cli_type == self.config.default_cli_tool
            else None
        )
        if agent_cli_model is not None:
            # restart path: prefer agent's frozen model
            model = agent_cli_model or global_model or cli_agent.default_model
        else:
            # create path: prefer phase config
            model = (phase_cli_model if phase_cli_tool else None) or global_model or cli_agent.default_model

        glm_token = phase_glm_token_env if phase_glm_token_env else None
        env_vars = self._build_glm_env_vars(model, glm_token, agent_id, label=label)

        timeout_ms = self._resolve_mcp_timeout_ms(
            cli_type, task.workflow_id, label=label
        )
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
        _api_port = os.environ.get("HEPHAESTUS_PORT") or str(getattr(self.config, "mcp_port", 8300))
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
            from src.core.database import Phase
            session = self.db_manager.get_session()
            try:
                if task.phase_id.isdigit():
                    phase = (
                        session.query(Phase)
                        .filter_by(order=int(task.phase_id), workflow_id=task.workflow_id)
                        .first()
                    )
                else:
                    phase = session.query(Phase).filter_by(id=task.phase_id).first()
                if phase:
                    phase_name = phase.name
                    phase_order = str(phase.order)
            finally:
                session.close()

        thinking_level = phase_thinking_override or getattr(
            self.config, "cli_thinking_level", "medium"
        )
        return phase_name, phase_order, thinking_level

    def _resolve_session_id(
        self,
        task: Task,
        agent_type: str,
        phase_name: Optional[str],
        model: str,
        *,
        excluded_types: Tuple[str, ...],
    ) -> str:
        """Generate deterministic session ID for persistent agent sessions.

        Shared — create passes excluded_types including 'arbitration';
        restart passes a shorter tuple (Phase 3 mismatch, preserved as-is).
        """
        session_id = ""
        if task.workflow_id and agent_type not in excluded_types:
            try:
                _s = self.db_manager.get_session()
                try:
                    from src.core.database import Workflow
                    _wf = _s.query(Workflow).filter_by(id=task.workflow_id).first()
                    if _wf and _wf.launch_params:
                        _lp = (
                            _wf.launch_params
                            if isinstance(_wf.launch_params, dict)
                            else {}
                        )
                        _pid = _lp.get("project_id") or _lp.get("project_path", "")
                        _dsl = (
                            _lp.get("design_slug")
                            or _lp.get("design_id")
                            or _lp.get("feature_id", "")
                        )
                        if _pid and _dsl and phase_name:
                            from src.autopilot.phases import get_session_id
                            session_id = get_session_id(_pid, _dsl, phase_name, model=model)
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
                self._ensure_codegraph_initialized(working_directory)

        if task.phase_id and working_directory:
            from pathlib import Path as _Path
            phase_output_dir = _Path(working_directory) / ".hephaestus" / (phase_name or task.phase_id)
            phase_output_dir.mkdir(parents=True, exist_ok=True)

        return self._create_tmux_session(
            session_name, working_directory=working_directory, env_vars=env_vars
        )

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
            logger.info(
                f"Exporting {len(env_vars)} environment variables for {label}: "
                f"{', '.join(env_vars.keys())}"
            )
            await self._export_env_vars_and_verify(
                tmux_session, pane, env_vars, label=label
            )

        cli_launch_started_at = datetime.utcnow().timestamp()
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
        """
        for key in cli_agent.post_launch_confirmation_keys():
            pane.send_keys(key)
            await asyncio.sleep(1.5)

        await self._send_goal_command(pane, cli_agent, task, agent_type)

        await self._send_initial_prompt_with_retry(
            pane=pane,
            cli_agent=cli_agent,
            cli_type=cli_type,
            initial_message=initial_message,
            agent_id=agent_id,
            task_id=task.id,
            max_retries=3,
        )

    def _wait_for_shell_ready(
        self, pane, timeout: float = 2.0, poll_interval: float = 0.1
    ) -> None:
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
            "y": 50,   # Initial height in rows
        }
        # Use provided working directory (which should be a worktree path)
        # Fallback to project root from config if not provided
        if not working_directory:
            working_directory = str(self.config.project_root)
            logger.warning(
                f"No working directory provided, using project root: {working_directory}"
            )
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
                "unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION_ID "
                "CLAUDE_CODE_CHILD_SESSION CLAUDE_AGENT_SDK_VERSION",
                enter=True,
            )
        except Exception:
            pass  # Non-critical -- worst case the launch command fails visibly

        # Keep a small scrollback — pipe-pane already captures the full
        # transcript to a durable file, so large history-limit just wastes
        # memory and slows tmux's rendering for no benefit.
        try:
            session.set_option("history-limit", "1000")
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
            # so the frontend can render colors via ansi-to-html. Only
            # strip \r to prevent spinner bloat.
            # Strip terminal control sequences but keep ANSI color codes.
            # Keep: SGR color sequences (\x1b[...m) and \r (for spinner collapsing)
            # Strip: everything else aggressively
            # Perl fully block-buffers STDOUT whenever it isn't a TTY (true
            # here -- redirected to transcript_path via `>>`), so a plain
            # `perl -pe '...'` sits on every byte pipe-pane feeds it until
            # the buffer fills or perl exits. Two fixes needed, not one:
            #
            # 1. $|=1 (autoflush) handles the OUTPUT side -- without it,
            #    even a perl that has processed a line won't push it to
            #    disk promptly.
            # 2. sysread() in an explicit loop, not -pe's implicit
            #    while(<>){...}, handles the INPUT side -- -pe reads one
            #    "line" (up to $/, "\n" by default) before there's anything
            #    to flush at all. Modern TUIs (Claude Code's included)
            #    redraw mostly via \r + cursor-positioning escapes, not
            #    literal "\n". Confirmed live: a transcript sat frozen at
            #    exactly the byte offset of the launch command's own
            #    trailing newline for an agent's entire multi-minute run,
            #    while tmux capture-pane on the same live session showed
            #    extensive fresh output the whole time -- $|=1 alone (a
            #    prior fix) never got the chance to flush anything because
            #    perl was still blocked waiting for a "\n" that wasn't
            #    coming. sysread(STDIN, $buf, 65536) returns as soon as
            #    ANY data is available on the pipe (a true short read),
            #    exactly like tmux's own pipe-pane delivery, so each
            #    sysread pairs with an immediate print+flush of whatever
            #    arrived. A multi-byte escape sequence split across two
            #    reads won't be matched by either substitution pass and
            #    survives unstripped in the transcript -- a rare cosmetic
            #    imperfection, not a functional blocker, unlike minutes of
            #    frozen scrollback.
            _pty_filter = (
                r"$|=1; while (sysread(STDIN, my $buf, 65536)) { "
                r"$buf =~ s/\x1b\][^\x07]*\x07//g; "  # OSC with BEL
                r"$buf =~ s/\x1b\][^\x1b]*\x1b\\//g; "  # OSC with ST (single backslash)
                r"$buf =~ s/\x1b\[[?]?[0-9;]*[^0-9;m]//g; "  # All CSI/DEC except m (color)
                r"$buf =~ s/\x1b[()][A-Za-z0-9]//g; "  # Charset selection
                r"$buf =~ s/\x1b[^\x1b\x5b\x5d]//g; "  # Any other bare ESC sequences
                r"print $buf; }"
            )
            pipe_cmd = f"perl -e {shlex.quote(_pty_filter)} >> {shlex.quote(str(transcript_path))}"
            session.attached_window.attached_pane.cmd("pipe-pane", "-o", pipe_cmd)
        except Exception as e:
            logger.warning(f"Failed to enable pipe-pane transcript logging: {e}")

        # Use a wide terminal so captured output isn't hard-wrapped at 80 columns.
        # This matches what a developer would see in a full-width terminal.
        try:
            pane = session.attached_window.attached_pane
            # Try both methods for reliability
            pane.set_width(150)
            try:
                pane.resize_pane(width=150)
            except Exception:
                pass
        except Exception:
            pass  # Non-critical

        # Note: env_vars are exported in the shell before launching the agent
        # (see create_agent_for_task and restart_agent methods)

        logger.debug(f"Created tmux session: {session_name}")
        return session

    async def _export_env_vars_and_verify(
        self, tmux_session, pane, env_vars: Optional[Dict[str, str]], label: str
    ) -> None:
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

        check_key = (
            "HEPHAESTUS_AGENT_ID"
            if "HEPHAESTUS_AGENT_ID" in env_vars
            else next(iter(env_vars))
        )
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
            logger.warning(
                f"[ENV-EXPORT] {label}: readback for {check_key} didn't match "
                f"on attempt {attempt + 1} -- "
                f"{'retrying' if attempt == 0 else 'giving up, launching anyway'}"
            )

    def _write_task_instructions(
        self, worktree_path: str, task_id: str, content: str
    ) -> str:
        """Persist an agent's full initial instructions as a markdown file in
        its worktree, so every phase agent -- the first in a workflow
        included -- receives its task the same way later phases already
        receive prior phases' outputs (design.md, architecture.md,
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
        task_id: str, instructions_rel_path: str, restarted: bool = False,
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
        return (
            f"Task ID: {task_id}\n\n"
            f"{agent_tag}Your full task instructions{detail} are in {instructions_rel_path} "
            f"-- read that file now, then {verb}."
        )

    async def _send_goal_command(
        self, pane, cli_agent, task: Task, agent_type: str
    ) -> None:
        """Set a self-checked completion condition (e.g. Claude Code's
        `/goal <condition>`, via cli_agent.format_goal_command -- a no-op
        empty string for CLIs with no such mechanism) so the agent keeps
        working until task.done_definition is actually met, instead of
        stopping on its own judgment. Sent BEFORE the task pointer (see
        _build_instructions_pointer) since a command like /goal is consumed
        by the CLI itself rather than as a chat turn, so ordering relative
        to the task description doesn't matter -- but sending it after
        would risk landing while the agent is mid-tool-call reading its
        instructions file, the same interleaving problem chunked delivery
        already works around.

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
        logger.info(
            f"[GOAL] Set /goal for task {task.id[:8]} ({len(condition)} chars)"
        )
        await asyncio.sleep(3)

    async def _verify_instructions_file_read(
        self, pane, instructions_rel_path: str, agent_id: str
    ) -> None:
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
                f"[INSTRUCTIONS-CHECK] Agent {agent_id[:8]} shows no sign of "
                f"having opened {instructions_rel_path} within 15s of the "
                "pointer being sent -- it may be idle instead of working."
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
                        launch_params = (
                            wf.launch_params
                            if isinstance(wf.launch_params, dict)
                            else {}
                        )
                finally:
                    session.close()

            # Design document — the key external input (phase 1 extracts from it,
            # phase 8 re-validates against it).
            design_doc = launch_params.get("design_document")
            if design_doc:
                p = Path(design_doc)
                if p.exists() and p.is_file():
                    try:
                        context["design.md"] = p.read_text()
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
                                    context["requirements.md"] = (
                                        req_path.read_text()
                                    )
                                except Exception:
                                    pass
                finally:
                    session2.close()
        except Exception as e:
            logger.warning(
                f"Failed to gather worktree context for task {getattr(task, 'id', '?')}: {e}"
            )

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

    async def _verify_prompt_delivery(
        self, pane, verification_string: str, wait_seconds: int = 10
    ) -> bool:
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

    async def _record_cli_session(
        self, cli_agent, session_id: str, working_directory: Optional[str], launched_at: float
    ) -> None:
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
                logger.info(
                    "OpenCode agent: Prompt loaded via -p flag, waiting 5 seconds then sending Enter"
                )
                await asyncio.sleep(5)
                pane.send_keys("", enter=True)  # Send Enter to submit the prompt
                logger.info(f"OpenCode: Enter sent to agent {agent_id}")
            elif cli_agent.needs_chunked_delivery:
                # Send in chunks to avoid tmux buffer issues with large prompts
                agent_name = cli_agent.display_name
                logger.info(
                    f"Sending initial prompt to {agent_name} agent {agent_id} (verification disabled)"
                )
                formatted_message = cli_agent.format_message(initial_message)

                chunk_size = 2500  # characters per chunk
                num_chunks = (len(formatted_message) + chunk_size - 1) // chunk_size
                logger.info(
                    f"{agent_name} agent: Sending prompt in {num_chunks} chunks ({len(formatted_message)} total chars)"
                )

                for i in range(0, len(formatted_message), chunk_size):
                    chunk = formatted_message[i : i + chunk_size]
                    # enter=False is required: libtmux's send_keys defaults
                    # enter=True, which SUBMITS each chunk as its own message.
                    # Observed live with pi: the agent started working off the
                    # first 2500-char fragment alone, while every later chunk
                    # arrived mid-run and queued up as a garbled mid-word
                    # "Steering:" message.
                    pane.send_keys(chunk, enter=False)
                    await asyncio.sleep(
                        0.2
                    )  # Delay between chunks to avoid overwhelming tmux

                # Now send Enter to submit the entire message
                logger.info("All chunks sent, submitting message with Enter")
                await asyncio.sleep(0.5)  # Brief pause before Enter
                pane.send_keys("", enter=True)  # This sends just the Enter key
                logger.info(f"Initial prompt sent to {agent_name} agent {agent_id}")
            else:
                # Other agents: Send entire prompt in one go
                logger.info(
                    f"Sending initial prompt to agent {agent_id} (verification disabled)"
                )
                formatted_message = cli_agent.format_message(initial_message)
                logger.info(
                    f"Non-Claude agent: Sending entire prompt in one message ({len(formatted_message)} chars)"
                )
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
                output_lower = output.lower()
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
                ):
                    if indicator in output_lower:
                        raise Exception(
                            f"CLI session limit detected: '{indicator}' found in output"
                        )
            except Exception as check_err:
                if "CLI session limit detected" in str(check_err):
                    raise
                # Non-critical check failure (e.g. capture-pane raced the
                # session closing) -- don't fail agent creation over it.

            return

        # Verification enabled - retry loop
        for attempt in range(1, max_retries + 1):
            logger.info(
                f"Sending initial prompt to agent {agent_id} (attempt {attempt}/{max_retries})"
            )

            if is_opencode:
                # OpenCode: Prompt already loaded via -p flag, just send Enter after 5 seconds
                logger.info(
                    "OpenCode agent: Prompt loaded via -p flag, waiting 5 seconds then sending Enter"
                )
                await asyncio.sleep(5)
                pane.send_keys("", enter=True)  # Send Enter to submit the prompt
            elif cli_agent.needs_chunked_delivery:
                # Send in chunks to avoid tmux buffer issues with large prompts
                agent_name = cli_agent.display_name
                formatted_message = cli_agent.format_message(initial_message)
                chunk_size = 2000  # characters per chunk
                num_chunks = (len(formatted_message) + chunk_size - 1) // chunk_size
                logger.info(
                    f"{agent_name} agent: Sending prompt in {num_chunks} chunks ({len(formatted_message)} total chars)"
                )

                for i in range(0, len(formatted_message), chunk_size):
                    chunk = formatted_message[i : i + chunk_size]
                    # enter=False: see the verification-disabled branch above --
                    # libtmux defaults enter=True, which submits each chunk as
                    # its own message instead of accumulating one prompt.
                    pane.send_keys(chunk, enter=False)
                    await asyncio.sleep(
                        0.1
                    )  # Delay between chunks to avoid overwhelming tmux

                # Now send Enter to submit the entire message
                logger.info("All chunks sent, submitting message with Enter")
                await asyncio.sleep(0.5)  # Brief pause before Enter
                pane.send_keys("", enter=True)  # This sends just the Enter key
            else:
                # Other agents: Send entire prompt in one go
                formatted_message = cli_agent.format_message(initial_message)
                logger.info(
                    f"Non-Claude agent: Sending entire prompt in one message ({len(formatted_message)} chars)"
                )
                pane.send_keys(formatted_message, enter=True)

            # Verify delivery
            if await self._verify_prompt_delivery(
                pane, verification_string, wait_seconds=10
            ):
                logger.info(
                    f"✓ Initial prompt verified for agent {agent_id} on attempt {attempt}"
                )
                return

            logger.warning(
                f"✗ Initial prompt NOT verified for agent {agent_id} on attempt {attempt}"
            )

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
            raise ValueError(
                "task is REQUIRED for create_agent_for_task \u2014 cannot create agent without a task"
            )

        # Git history and remotes are external state. In review mode
        # (AutopilotProject.review_mode), a pipeline may prepare a Git
        # hand-off, but must never autonomously launch an agent that can
        # commit, push, open a PR, or merge -- an operator explicitly
        # performs those actions after reviewing the validated worktree.
        if task.phase_id:
            from src.core.database import AutopilotProject, Phase, resolve_project_for_workflow

            with self.db_manager.get_session() as _phase_session:
                phase = _phase_session.query(Phase).filter_by(id=task.phase_id).first()
                if phase and phase.name == "git_commit_push":
                    project_id, _ = resolve_project_for_workflow(task.workflow_id)
                    review_mode = False
                    if project_id:
                        proj = _phase_session.query(AutopilotProject).get(project_id)
                        review_mode = bool(proj and proj.review_mode)
                    if review_mode:
                        raise PermissionError(
                            "git_commit_push is manual-only in review mode: explicit "
                            "human approval is required before any commit, push, PR, or merge"
                        )

        existing = self._check_duplicate_active_agent(task)
        if existing:
            return existing

        # Phase-sibling guard: don't dispatch if the phase already has
        # another active task. Protects against concurrent dispatch from
        # different code paths (orchestrator sweep, HTTP route, validator
        # spawn) targeting the same phase.
        from src.autopilot.orchestrator.engine_client import check_phase_sibling_active
        _guard_session = self.db_manager.get_session()
        try:
            phase_sibling = check_phase_sibling_active(
                _guard_session, task.id, task.phase_id,
                created_by_filter=False,
            )
            if phase_sibling is not None:
                logger.warning(
                    f"[create_agent_for_task] Skipping dispatch for task "
                    f"{task.id[:8]}: phase {task.phase_id[:8]} already has active "
                    f"task {phase_sibling.id[:8]} ({phase_sibling.status}) -- "
                    f"avoiding duplicate agent"
                )
                return None
        finally:
            _guard_session.close()

        agent_id = str(uuid.uuid4())
        wt_mgr = self._scoped_worktree_manager(task.workflow_id)
        phase_config = self._resolve_phase_config(
            task, cli_type, phase_cli_tool, phase_cli_model, phase_glm_token_env, phase_thinking_level
        )
        cli_type = phase_config.cli_type

        from src.core.log_context import set_log_context
        set_log_context(agent=agent_id, task=task.id, workflow=task.workflow_id or "")
        logger.info(f"Creating {cli_type} agent {agent_id} for task {task.id}")

        # Insert a stub Agent row BEFORE worktree creation so the
        # agent_worktrees.agent_id FK passes.
        session = self.db_manager.get_session()
        agent = Agent(
            id=agent_id,
            system_prompt="(pending: worktree + prompt setup)",
            status="idle",
            cli_type=cli_type,
            agent_type=agent_type,
            current_task_id=task.id,
            last_activity=datetime.utcnow(),
            health_check_failures=0,
        )
        session.add(agent)
        if assign_to_task:
            claimed_task = session.query(Task).filter_by(id=task.id).first()
            if claimed_task:
                claimed_task.assigned_agent_id = agent_id
                claimed_task.status = "in_progress"
                claimed_task.started_at = datetime.utcnow()
        session.commit()
        session.close()

        try:
            context_files = self._gather_worktree_context(task)
            wt_resolution = self._resolve_worktree(
                task, wt_mgr, create_if_missing=True, agent_id=agent_id, context_files=context_files
            )
            branch_path = wt_resolution.branch_path

            # Generate system prompt
            phase_name = None
            if task.phase_id:
                try:
                    from src.core.database import Phase
                    with self.db_manager.get_session() as _ps:
                        _ph = _ps.query(Phase).filter_by(id=task.phase_id).first()
                        if _ph:
                            phase_name = _ph.name
                except Exception:
                    pass

            system_prompt = await self.llm_provider.generate_agent_prompt(
                task={
                    "id": task.id,
                    "description": task.raw_description,
                    "enriched_description": task.enriched_description,
                    "done_definition": task.done_definition,
                    "agent_id": agent_id,
                },
                memories=memories,
                project_context=project_context,
                phase_name=phase_name,
            )

            env_vars, model, cli_agent = self._resolve_env_and_model(
                cli_type, task, agent_id, label="agent",
                phase_cli_model=phase_config.cli_model,
                phase_cli_tool=phase_config.phase_cli_tool,
                phase_glm_token_env=phase_config.glm_token_env,
            )

            session_name = f"{self.config.tmux_session_prefix}_{agent_id[:8]}"
            tmux_session = self._prepare_launch_environment(
                session_name, branch_path, env_vars, task, phase_name, cli_agent=cli_agent
            )

            phase_name_resolved, phase_order, thinking_level = self._resolve_phase_name_and_thinking(
                task, phase_config.thinking_level
            )
            if phase_name_resolved:
                phase_name = phase_name_resolved

            # Complexity-adaptive reasoning
            try:
                if thinking_level in ("high", "medium") and getattr(task, "workflow_id", None):
                    if not hasattr(self, "_complexity_cache"):
                        self._complexity_cache = {}
                    complexity = self._complexity_cache.get(task.workflow_id)
                    if complexity is None:
                        design_text = ""
                        try:
                            if working_directory:
                                wd = Path(working_directory)
                                cands = []
                                dq = wd / DESIGN_CONTEXT_SUBDIR
                                if dq.is_dir():
                                    cands += sorted(dq.glob("*.md"))
                                cands += [
                                    wd / CONTEXT_DIR_NAME / "design.md",
                                    wd / CONTEXT_DIR_NAME / "design_document.md",
                                    wd / CONTEXT_DIR_NAME / "requirements.md",
                                ]
                                for _p in cands:
                                    if _p.is_file():
                                        design_text = _p.read_text()[:6000]
                                        break
                        except Exception:
                            pass
                        if not design_text:
                            design_text = (
                                (enriched_data or {}).get("enriched_description")
                                or task.enriched_description
                                or task.raw_description
                                or ""
                            )
                        complexity = await self.llm_provider.classify_complexity(
                            design_text, workflow_id=task.workflow_id
                        )
                        self._complexity_cache[task.workflow_id] = complexity
                    if complexity == "low":
                        thinking_level = "low"
                    elif complexity == "medium" and thinking_level == "high":
                        thinking_level = "medium"
                    logger.info(
                        f"[COMPLEXITY] phase budget {phase_config.thinking_level} \u2192 {thinking_level} "
                        f"(design complexity={complexity}) for agent {agent_id[:8]}"
                    )
            except Exception as e:
                logger.debug(f"[COMPLEXITY] adaptive thinking skipped: {e}")

            session_id = self._resolve_session_id(
                task, agent_type, phase_name, model,
                excluded_types=("validator", "result_validator", "diagnostic", "arbitration"),
            )

            initial_message = self._format_initial_message(
                task, agent_id, branch_path, agent_type, enriched_data
            )
            instructions_rel_path = self._write_task_instructions(
                branch_path, task.id, initial_message
            )
            instructions_pointer = self._build_instructions_pointer(
                task.id, instructions_rel_path,
                agent_name=f"hephaestus-{phase_name.replace('_', '-')}" if phase_name else None,
            )
            if cli_type == "codex" and session_id:
                instructions_pointer += f"\nHephaestus Session ID: {session_id}"

            # Build and send launch command
            launch_result, pane, cli_launch_started_at = await self._build_and_send_launch_command(
                cli_agent, tmux_session,
                system_prompt=system_prompt, task=task, model=model,
                thinking_level=thinking_level, phase_name=phase_name,
                agent_id=agent_id, session_id=session_id,
                working_directory=branch_path, instructions_pointer=instructions_pointer,
                env_vars=env_vars, label=f"agent {agent_id[:8]}",
            )

            # Echo task info to terminal
            task_desc = (task.enriched_description or task.raw_description or "")[:200]
            pane.send_keys('echo "="', enter=True)
            pane.send_keys(f"echo -- {shlex.quote(f'AGENT: {agent_id[:8]}')}", enter=True)
            pane.send_keys(f"echo -- {shlex.quote(f'PHASE: {phase_order}. {phase_name}')}", enter=True)
            pane.send_keys(f"echo -- {shlex.quote(f'TASK: {task_desc}')}", enter=True)
            pane.send_keys('echo "="', enter=True)
            await asyncio.sleep(0.3)

            # Send the launch command
            pane.send_keys(launch_result.command, enter=True)

            # Update agent record
            session = self.db_manager.get_session()
            agent = session.merge(Agent(
                id=agent_id,
                system_prompt=system_prompt,
                status="working",
                cli_type=cli_type,
                cli_model=model,
                tmux_session_name=session_name,
                current_task_id=task.id,
                last_activity=datetime.utcnow(),
                launched_at=datetime.utcnow(),
                health_check_failures=0,
                agent_type=agent_type,
            ))
            task.assigned_agent_id = agent_id
            task.status = "in_progress"
            task.started_at = datetime.utcnow()
            log_entry = AgentLog(
                agent_id=agent_id, log_type="created",
                message=f"Agent created for task: {task.enriched_description[:100]}",
                details={"cli_type": cli_type, "task_id": task.id},
            )
            session.add(log_entry)
            session.commit()
            agent_id_to_return = agent.id
            session.close()

            # Wait for CLI to initialize
            logger.info(f"=== INITIAL PROMPT DELIVERY for agent {agent_id} ===")
            logger.info(f"CLI type: {cli_type}")
            logger.info(f"Tmux session: {session_name}")

            if launch_result.prompt_delivery in (
                LaunchResult.AGENT_FILE, LaunchResult.DEFERRED,
            ) and system_prompt:
                initial_message = system_prompt + "\n\n---\n\n" + initial_message
                self._write_task_instructions(branch_path, task.id, initial_message)

            logger.info(f"Initial message length: {len(initial_message)} characters")
            wait_time = 25
            logger.info(f"Waiting {wait_time} seconds for {cli_type} agent {agent_id} to initialize...")
            await asyncio.sleep(wait_time)

            # Termination race check
            term_race_result = await self._check_termination_race(
                agent_id, task.id, session_name, agent_id_to_return=agent_id_to_return,
            )
            if term_race_result is not None:
                return term_race_result

            if not self.tmux_server.has_session(session_name):
                logger.error(f"Tmux session {session_name} died during initialization wait!")
                raise Exception("Tmux session died during initialization wait")

            self._detect_launch_failure(pane, cli_agent, cli_type, session_name)

            # Deliver initial prompt
            await self._deliver_initial_prompt(
                pane, cli_agent, cli_type, instructions_pointer, agent_id, task,
            )
            await self._record_cli_session(cli_agent, session_id, branch_path, cli_launch_started_at)
            await self._verify_instructions_file_read(pane, instructions_rel_path, agent_id)

            logger.info(f"=== END INITIAL PROMPT DELIVERY for agent {agent_id} ===")

            class AgentInfo:
                def __init__(self, id):
                    self.id = id
            return AgentInfo(agent_id_to_return)

        except Exception as e:
            logger.error(f"Failed to create agent with {cli_type}: {e}")
            if phase_config.fallback_cli_tool and phase_config.fallback_cli_tool != cli_type:
                logger.warning(
                    f"Primary CLI tool '{cli_type}' failed, trying fallback: "
                    f"{phase_config.fallback_cli_tool}/{phase_config.fallback_cli_model or 'default'}"
                )
                try:
                    if "tmux_session" in locals():
                        try:
                            tmux_session.kill_session()
                        except Exception:
                            pass
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
                        except Exception:
                            pass
                        try:
                            wt_mgr.discard_agent(agent_id)
                        except Exception:
                            pass
                    return await self.create_agent_for_task(
                        task=task, enriched_data=enriched_data, memories=memories,
                        project_context=project_context, cli_type=phase_config.fallback_cli_tool,
                        working_directory=working_directory, agent_type=agent_type,
                        use_existing_worktree=use_existing_worktree, commit_sha=commit_sha,
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
                        task_record.completed_at = datetime.utcnow()
                        logger.info(f"Marked task {task.id} as failed")
                        if "CLI session limit detected" in str(e) and task_record.workflow_id:
                            from src.core.database import Workflow as _Workflow
                            workflow_record = cleanup_session.query(_Workflow).filter_by(id=task_record.workflow_id).first()
                            if workflow_record and workflow_record.status != "paused":
                                from src.autopilot.orchestrator.engine_client import pause_workflow
                                pause_workflow(
                                    task_record.workflow_id,
                                    reason="system",
                                    status_reason=(
                                        f"CLI session limit hit ({cli_type}), no working "
                                        "fallback -- will auto-resume on its own retry "
                                        "cooldown once the limit resets"
                                    ),
                                    session=cleanup_session,
                                )
                                logger.warning(
                                    f"[SESSION-LIMIT] Pausing workflow "
                                    f"{task_record.workflow_id[:8]} -- {cli_type} "
                                    "session limit hit with no working fallback"
                                )
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
                logger.warning(
                    f"Agent {agent_id[:8]} exceeded max restarts ({agent.restart_count}), terminating"
                )
                # Capture the task before terminating -- the primitive
                # clears current_task_id as part of the invariant.
                task_id = agent.current_task_id
                from src.autopilot.orchestrator.engine_client import terminate_agent

                terminate_agent(agent.id, session=session)
                session.commit()
                task = session.query(Task).filter_by(id=task_id).first()
                if task and task.status not in ("done", "failed"):
                    task.status = "failed"
                    task.failure_reason = (
                        f"Agent exceeded max restarts ({agent.restart_count})"
                    )
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
                    transcript_dir = self._resolve_tmux_transcript_dir(agent)
                    if transcript_dir:
                        self._flush_stable_transcript(
                            agent.tmux_session_name,
                            transcript_dir / f"{agent.tmux_session_name}.clean.log",
                        )
                except Exception as e:
                    logger.debug(f"[STABLE-TRANSCRIPT] Final flush before restart failed: {e}")

                try:
                    if self.tmux_server.has_session(agent.tmux_session_name):
                        tmux_session = None
                        for tmux_sess in self.tmux_server.sessions:
                            if tmux_sess.name == agent.tmux_session_name:
                                tmux_session = tmux_sess
                                break
                        if tmux_session:
                            tmux_session.kill_session()
                except Exception:
                    pass

            # Resolve env vars and model (restart path: uses agent's frozen values)
            env_vars, model, cli_agent = self._resolve_env_and_model(
                agent.cli_type, task, agent_id, label="restarted agent",
                agent_cli_model=agent.cli_model,
            )

            # Resolve worktree (restart: create_if_missing=False, silent None)
            wt_resolution = self._resolve_worktree(
                task, self.branch_manager, create_if_missing=False, agent_id=agent_id,
            )
            restart_wd = wt_resolution.branch_path

            new_session_name = f"{self.config.tmux_session_prefix}_{agent_id[:8]}_r"

            # Resolve phase name BEFORE preparing the launch environment: the
            # .hephaestus/ output dir is named by phase NAME (pre-split
            # behavior), not by the raw phase_id.
            restart_phase_name = None
            if task.phase_id:
                from src.core.database import Phase
                restart_session = self.db_manager.get_session()
                try:
                    if task.phase_id.isdigit():
                        restart_phase = (
                            restart_session.query(Phase)
                            .filter_by(order=int(task.phase_id), workflow_id=task.workflow_id)
                            .first()
                        )
                    else:
                        restart_phase = restart_session.query(Phase).filter_by(id=task.phase_id).first()
                    if restart_phase:
                        restart_phase_name = restart_phase.name
                except Exception:
                    pass
                finally:
                    restart_session.close()

            # Prepare launch environment
            if restart_wd:
                cli_agent.prepare_working_directory(restart_wd)
            tmux_session = self._prepare_launch_environment(
                new_session_name, restart_wd, env_vars, task, restart_phase_name,
                cli_agent=cli_agent, prewarm_codegraph=False,
            )

            restart_system_prompt = (
                f"\u26a0\ufe0f RESTART: You were restarted because: {reason}. "
                f"Continue working on task {task.id}. "
                f"Do NOT re-read files you already analyzed. Pick up where you left off.\n\n"
                f"{agent.system_prompt}"
            )

            # Session ID for restart (excludes validator/result_validator/diagnostic, NOT arbitration)
            session_id = self._resolve_session_id(
                task, agent.agent_type or "phase", restart_phase_name, model,
                excluded_types=("validator", "result_validator", "diagnostic"),
            )

            restart_message = (
                f"\u26a0\ufe0f You were restarted ({reason}). Your prior work is committed in this "
                f"worktree \u2014 do NOT redo it; run `git log` / `git status` and inspect existing "
                f"files first, then continue toward completion.\n\n"
                + self._format_initial_message(
                    task, agent_id, agent_type=(agent.agent_type or "phase")
                )
            )
            instructions_pointer = ""
            if restart_wd:
                instructions_rel_path = self._write_task_instructions(
                    restart_wd, task.id, restart_message
                )
                instructions_pointer = self._build_instructions_pointer(
                    task.id, instructions_rel_path, restarted=True,
                    agent_name=f"hephaestus-{restart_phase_name.replace('_', '-')}" if restart_phase_name else None,
                )
                if agent.cli_type == "codex" and session_id:
                    instructions_pointer += f"\nHephaestus Session ID: {session_id}"

            # Build and send launch command
            launch_result, pane, cli_launch_started_at = await self._build_and_send_launch_command(
                cli_agent, tmux_session,
                system_prompt=restart_system_prompt, task=task, model=model,
                thinking_level=None, phase_name=restart_phase_name,
                agent_id=agent_id, session_id=session_id,
                working_directory=restart_wd, instructions_pointer=instructions_pointer,
                env_vars=env_vars, label=f"restarted agent {agent_id[:8]}",
            )
            pane.send_keys(launch_result.command, enter=True)

            restart_cli_type = agent.cli_type
            restart_task_id = task.id

            if launch_result.prompt_delivery in (
                LaunchResult.AGENT_FILE, LaunchResult.DEFERRED,
            ) and restart_system_prompt:
                restart_message = restart_system_prompt + "\n\n---\n\n" + restart_message
                if restart_wd:
                    self._write_task_instructions(restart_wd, task.id, restart_message)

            agent.tmux_session_name = new_session_name
            agent.status = "working"
            agent.health_check_failures = 0
            agent.last_activity = datetime.utcnow()
            agent.launched_at = datetime.utcnow()

            log_entry = AgentLog(
                agent_id=agent_id, log_type="restarted",
                message=f"Agent restarted: {reason}",
                details={"new_session": new_session_name},
            )
            session.add(log_entry)
            session.commit()

            try:
                await asyncio.sleep(25)
                term_race_result = await self._check_termination_race(
                    agent_id, restart_task_id, new_session_name,
                    agent_id_to_return=agent_id,
                )
                if term_race_result is not None:
                    return term_race_result
                self._detect_launch_failure(pane, cli_agent, restart_cli_type, new_session_name)
                if self.tmux_server.has_session(new_session_name):
                    await self._deliver_initial_prompt(
                        pane, cli_agent, restart_cli_type,
                        instructions_pointer if restart_wd else restart_message,
                        agent_id, task, agent_type=agent.agent_type or "phase",
                    )
                    await self._record_cli_session(
                        cli_agent, session_id, restart_wd, cli_launch_started_at
                    )
                    if restart_wd:
                        await self._verify_instructions_file_read(
                            pane, instructions_rel_path, agent_id
                        )
                    logger.info(
                        f"[RESTART] Delivered continue-prompt to agent {agent_id} ({restart_cli_type})"
                    )
                else:
                    logger.error(
                        f"[RESTART] Session {new_session_name} died before prompt delivery"
                    )
            except Exception as e:
                logger.error(
                    f"[RESTART] Failed to deliver continue-prompt to agent {agent_id}: {e}"
                )

            logger.info(f"Agent {agent_id} restarted successfully")

        except Exception as e:
            logger.error(f"Failed to restart agent {agent_id}: {e}")
            session.rollback()
        finally:
            session.close()

