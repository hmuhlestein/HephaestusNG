"""Agent management system for Hephaestus."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import libtmux

from src.core.database import (
    Agent,
    AgentLog,
    DatabaseManager,
    Task,
    utc_now,
)
from src.core.simple_config import get_config
from src.core.worktree_manager import WorktreeManager
from src.interfaces import LLMProviderInterface, get_cli_agent

logger = logging.getLogger(__name__)


class PhaseConfig(NamedTuple):
    """Resolved phase configuration for agent creation."""
    cli_type: str
    # The raw (derived-or-caller) phase tool, before the global-default
    # fallback applied for cli_type. The model-preference gate downstream
    # keys off this value's truthiness, not cli_type's, to match pre-split
    # behavior: a caller passing a model without a tool must NOT get the
    # model applied (HEAD discarded it in that case).
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


class AgentManager:
    """Manages agent lifecycle and tmux sessions."""

    #: Base launch-rejection patterns shared by all CLIs (binary missing from
    #: PATH).  CLI subclasses ADD their own via get_launch_rejection_patterns()
    #: without replacing these.
    _BASE_LAUNCH_REJECTION_PATTERNS = [r"command not found", r"No such file or directory"]
    # The Claude Code pattern that raises the distinct "stuck on confirmation
    # dialog" error wording in _detect_launch_failure (pre-split behavior).
    _CLAUDE_CODE_CONFIRMATION_PATTERN = r"Bypass Permissions mode"

    def __init__(
        self,
        db_manager: DatabaseManager,
        llm_provider: LLMProviderInterface,
        phase_manager=None,
        tmux_server: Optional[libtmux.Server] = None,
    ):
        """Initialize agent manager.

        Args:
            db_manager: Database manager instance
            llm_provider: LLM provider for generating prompts
            phase_manager: Phase manager instance (optional)
            tmux_server: libtmux.Server instance (optional; defaults to a
                real one). Injectable seam (SOLID review 3.10) -- without
                it, AgentManager could only be unit-tested by patching the
                libtmux.Server import itself, then overwriting the instance
                attribute post-construction (see tests/test_agent_manager.py's
                previous mock_agent_manager fixture).
        """
        self.db_manager = db_manager
        self.llm_provider = llm_provider
        self.phase_manager = phase_manager
        self.config = get_config()
        self.tmux_server = tmux_server if tmux_server is not None else libtmux.Server()

        # Branch manager for agent isolation
        self.branch_manager = WorktreeManager(db_manager)

        # Tmux message-delivery collaborator (SOLID review 3.1) — the public
        # send_message_to_agent method below delegates to this.
        from src.agents.messenger import AgentMessenger

        # FIX #2: Pass self (agent_manager) so messenger reads live tmux_server
        self._messenger = AgentMessenger(db_manager, self)

        # Initial-message formatting collaborator (SOLID review 3.1) — the
        # public _format_initial_message method below delegates to this.
        # Some tests mutate self.phase_manager directly on the AgentManager
        # instance after construction, so the delegator re-syncs it onto
        # this builder on every call rather than caching a stale reference.
        from src.agents.prompt_builder import AgentPromptBuilder

        self._prompt_builder = AgentPromptBuilder(self.phase_manager)

        # Launch-pipeline collaborator (decomposition).
        from src.agents.launch_pipeline import LaunchPipeline
        self._launch = LaunchPipeline(self)

        # Terminator collaborator (decomposition).
        from src.agents.terminator import Terminator
        self._terminator = Terminator(self)

        # Output-capture collaborator (decomposition) -- transcript reading,
        # stable-transcript polling, and live capture-pane snapshots.
        from src.agents.output_capture import AgentOutputCapture
        self._output_capture = AgentOutputCapture(self.db_manager, self.tmux_server)

    def _build_glm_env_vars(
        self,
        model: str,
        glm_token_env: Optional[str],
        agent_id: str,
        label: str = "agent",
    ) -> Optional[Dict[str, str]]:
        return self._launch._build_glm_env_vars(model, glm_token_env, agent_id, label)


    def _resolve_mcp_timeout_ms(
        self,
        cli_type: str,
        task_workflow_id: Optional[str],
        label: str = "agent",
    ) -> Optional[int]:
        return self._launch._resolve_mcp_timeout_ms(cli_type, task_workflow_id, label)


    def _resolve_project_base_dir(self, workflow_id: Optional[str]) -> Optional[Path]:
        return self._launch._resolve_project_base_dir(workflow_id)


    def _scoped_worktree_manager(self, workflow_id: Optional[str]) -> WorktreeManager:
        return self._launch._scoped_worktree_manager(workflow_id)


    def _ensure_codegraph_initialized(self, working_directory: str) -> None:
        # The extraction that produced these delegators renamed the real first
        # parameter to `self` and then forwarded `self` as that argument, so
        # this passed an AgentManager where launch_pipeline expects a path
        # string. Currently uncalled, so it never surfaced.
        return self._launch._ensure_codegraph_initialized(working_directory)


    async def _check_termination_race(
        self, agent_id: str, task_id: str, session_name: str, agent_id_to_return: str
    ) -> Optional[object]:
        return await self._launch._check_termination_race(agent_id, task_id, session_name, agent_id_to_return)


    def _detect_launch_failure(
        self, pane, cli_agent, cli_type: str, session_name: str
    ) -> None:
        return self._launch._detect_launch_failure(pane, cli_agent, cli_type, session_name)


    # ── Shared step methods for create_agent_for_task / restart_agent ────

    def _check_duplicate_active_agent(self, task: Task) -> Optional[Agent]:
        return self._launch._check_duplicate_active_agent(task)


    def _resolve_phase_config(
        self,
        task: Task,
        cli_type: Optional[str],
        phase_cli_tool: Optional[str],
        phase_cli_model: Optional[str],
        phase_glm_token_env: Optional[str],
        phase_thinking_level: Optional[str],
    ) -> PhaseConfig:
        return self._launch._resolve_phase_config(task, cli_type, phase_cli_tool, phase_cli_model, phase_glm_token_env, phase_thinking_level)


    def _resolve_worktree(
        self,
        task: Task,
        wt_mgr: WorktreeManager,
        *,
        create_if_missing: bool,
        agent_id: str,
        context_files: Optional[Dict[str, str]] = None,
    ) -> WorktreeResolution:
        # create_if_missing/agent_id are keyword-only on the target.
        return self._launch._resolve_worktree(
            task,
            wt_mgr,
            create_if_missing=create_if_missing,
            agent_id=agent_id,
            context_files=context_files,
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
        return self._launch._resolve_env_and_model(cli_type, task, agent_id, label, phase_cli_model=phase_cli_model, phase_cli_tool=phase_cli_tool, phase_glm_token_env=phase_glm_token_env, agent_cli_model=agent_cli_model)


    def _resolve_phase_name_and_thinking(
        self,
        task: Task,
        phase_thinking_override: Optional[str],
    ) -> Tuple[Optional[str], str, Optional[str]]:
        return self._launch._resolve_phase_name_and_thinking(task, phase_thinking_override)


    def _resolve_session_id(
        self,
        task: Task,
        agent_type: str,
        phase_name: Optional[str],
        model: str,
        *,
        excluded_types: Tuple[str, ...],
    ) -> str:
        # excluded_types is keyword-only on the target.
        return self._launch._resolve_session_id(
            task, agent_type, phase_name, model, excluded_types=excluded_types
        )


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
        return self._launch._prepare_launch_environment(session_name, working_directory, env_vars, task, phase_name, cli_agent, prewarm_codegraph)


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
        # Everything after tmux_session is keyword-only on the target, so the
        # generated positional forwarding here was a guaranteed TypeError.
        # Uncalled, like the other mangled delegators, so it never surfaced;
        # launch_pipeline's own two call sites pass keywords correctly.
        return await self._launch._build_and_send_launch_command(
            cli_agent,
            tmux_session,
            system_prompt=system_prompt,
            task=task,
            model=model,
            thinking_level=thinking_level,
            phase_name=phase_name,
            agent_id=agent_id,
            session_id=session_id,
            working_directory=working_directory,
            instructions_pointer=instructions_pointer,
            env_vars=env_vars,
            label=label,
        )


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
        return await self._launch._deliver_initial_prompt(pane, cli_agent, cli_type, initial_message, agent_id, task, agent_type=agent_type, instructions_rel_path=instructions_rel_path)

        # Note: _record_cli_session and _verify_instructions_file_read are
        # called by the caller (they need caller-specific args like session_id,
        # working_directory, etc.)

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
        return await self._launch.create_agent_for_task(task, enriched_data, memories, project_context, cli_type, working_directory, agent_type, use_existing_worktree, commit_sha, phase_cli_tool, phase_cli_model, phase_glm_token_env, phase_thinking_level, assign_to_task)



    def _wait_for_shell_ready(
        self, pane, timeout: float = 2.0, poll_interval: float = 0.1
    ) -> None:
        return self._launch._wait_for_shell_ready(pane, timeout, poll_interval)


    def _create_tmux_session(
        self,
        session_name: str,
        working_directory: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
    ) -> libtmux.Session:
        return self._launch._create_tmux_session(session_name, working_directory, env_vars)


    async def _export_env_vars_and_verify(
        self, tmux_session, pane, env_vars: Optional[Dict[str, str]], label: str
    ) -> None:
        return await self._launch._export_env_vars_and_verify(tmux_session, pane, env_vars, label)


    def _write_task_instructions(
        self, worktree_path: str, task_id: str, content: str
    ) -> str:
        return self._launch._write_task_instructions(worktree_path, task_id, content)


    def _build_instructions_pointer(
        self, task_id: str, instructions_rel_path: str, restarted: bool = False,
        agent_name: str = None,
    ) -> str:
        # Same mangled extraction as _ensure_codegraph_initialized above:
        # launch_pipeline's first parameter is task_id, not the manager.
        return self._launch._build_instructions_pointer(task_id, instructions_rel_path, restarted, agent_name)


    async def _send_goal_command(
        self, pane, cli_agent, task: Task, agent_type: str
    ) -> None:
        return await self._launch._send_goal_command(pane, cli_agent, task, agent_type)


    async def _verify_instructions_file_read(
        self, pane, instructions_rel_path: str, agent_id: str
    ) -> None:
        return await self._launch._verify_instructions_file_read(pane, instructions_rel_path, agent_id)


    def _gather_worktree_context(self, task: Task) -> Dict[str, str]:
        return self._launch._gather_worktree_context(task)


    def _format_initial_message(
        self,
        task: Task,
        agent_id: str,
        branch_path: str = None,
        agent_type: str = "phase",
        enriched_data: dict = None,
    ) -> str:
        return self._launch._format_initial_message(task, agent_id, branch_path, agent_type, enriched_data)



    async def _verify_prompt_delivery(
        self, pane, verification_string: str, wait_seconds: int = 10
    ) -> bool:
        return await self._launch._verify_prompt_delivery(pane, verification_string, wait_seconds)


    async def _record_cli_session(
        self, cli_agent, session_id: str, working_directory: Optional[str], launched_at: float
    ) -> None:
        return await self._launch._record_cli_session(cli_agent, session_id, working_directory, launched_at)


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
        return await self._launch._send_initial_prompt_with_retry(pane, cli_agent, cli_type, initial_message, agent_id, task_id, max_retries, verify_delivery)


    async def terminate_agent(self, agent_id: str):
        return await self._terminator.terminate_agent(agent_id)


    def _commit_wip_in_shared_worktree(self, agent_id: str, task_id: Optional[str]) -> None:
        return self._terminator._commit_wip_in_shared_worktree(agent_id, task_id)


    async def restart_agent(self, agent_id: str, reason: str = ""):
        return await self._launch.restart_agent(agent_id, reason)



    def get_agent_output(self, agent_id: str, lines: int = 200) -> str:
        return self._output_capture.get_agent_output(agent_id, lines)

    def _resolve_tmux_transcript_dir(self, agent) -> Optional[Path]:
        return self._output_capture._resolve_tmux_transcript_dir(agent)

    def _read_transcript_log(self, agent, lines: int) -> str:
        return self._output_capture._read_transcript_log(agent, lines)

    def _find_tmux_session(self, session_name: str):
        return self._output_capture._find_tmux_session(session_name)

    def is_pane_dead(self, session_name: str) -> bool:
        return self._output_capture.is_pane_dead(session_name)

    def _capture_pane_lines(self, session_name: str) -> Optional[List[str]]:
        return self._output_capture._capture_pane_lines(session_name)

    @staticmethod
    def _append_lines(path: Path, new_lines: List[str]) -> None:
        from src.agents.output_capture import AgentOutputCapture
        AgentOutputCapture._append_lines(path, new_lines)

    def _poll_stable_transcript(self, session_name: str, clean_path: Path) -> None:
        return self._output_capture._poll_stable_transcript(session_name, clean_path)

    def _flush_stable_transcript(self, session_name: str, clean_path: Path) -> None:
        return self._output_capture._flush_stable_transcript(session_name, clean_path)

    def _get_orchestrator_output(self, agent, lines: int) -> str:
        return self._output_capture._get_orchestrator_output(agent, lines)


    def get_active_agents(self) -> List[Agent]:
        """Get all active agents.

        Returns:
            List of active agents

        Note: these Agent objects are returned after their session closes and
        are held by callers (monitor.py) across await points. This is safe
        ONLY because SessionLocal is configured with expire_on_commit=False
        and every attribute callers touch (id, status, cli_type, created_at,
        agent_type, tmux_session_name, current_task_id, ...) is a plain
        Column already eagerly loaded by the .all() query below — not a
        lazy-loaded relationship. If a caller starts accessing a relationship
        (e.g. agent.assigned_tasks) on these objects, that WILL raise
        DetachedInstanceError; extract it to a primitive here instead.
        """
        with self.db_manager.session_scope() as session:
            agents = session.query(Agent).filter(Agent.status != "terminated").all()
            return agents

    # ── Agent messaging / recovery ─────────────────────────────────
    # Restored 2026-08-19. These four implementations plus the
    # send_message_to_agent delegate were dropped from AgentManager
    # during the output_capture extraction -- moved nowhere, just
    # deleted, while three genuine helpers (_frame_kind,
    # _strip_trailing_pad, _norm) moved correctly alongside them.
    # 25 production call sites depend on them (guardian,
    # mechanical_recovery x9, conductor, health_audit, messaging_api,
    # task_enrichment, agent_dispatch), and each would raise
    # AttributeError on first use -- import stays clean, so nothing
    # surfaces until an agent actually needs recovery.
    async def send_recovery_keystrokes(self, agent_id: str) -> bool:
        """Send the CLI's mechanical recovery keystrokes (e.g. Esc for pi) to break a
        stuck/looping TUI. Generic + polymorphic via CLIAgentInterface.recovery_keystrokes()
        — the monitor stays harness-agnostic. Returns True if keys were sent."""
        import functools

        # Every blocking call below (DB query, tmux has_session/send_keys)
        # is individually offloaded via run_in_executor rather than
        # wrapping the whole method in one executor call -- the between-
        # keystrokes pause needs a real, non-blocking asyncio.sleep() so
        # other coroutines keep running while this one waits.
        loop = asyncio.get_event_loop()

        session = self.db_manager.get_session()
        try:
            agent = await loop.run_in_executor(
                None, lambda: session.query(Agent).filter_by(id=agent_id).first()
            )
            if not agent or not agent.tmux_session_name:
                return False
            has_session = await loop.run_in_executor(
                None, self.tmux_server.has_session, agent.tmux_session_name
            )
            if not has_session:
                return False
            # remain-on-exit keeps a crashed pane's session alive for
            # evidence, so has_session alone no longer implies "agent
            # alive" -- without this, recovery keystrokes get silently
            # sent into a dead pane instead of surfacing that the agent
            # already exited and needs a real restart.
            pane_dead = await loop.run_in_executor(
                None, self.is_pane_dead, agent.tmux_session_name
            )
            if pane_dead:
                return False
            keys = get_cli_agent(agent.cli_type).recovery_keystrokes()
            if not keys:
                return False
            tmux_session = next(
                (
                    s
                    for s in self.tmux_server.sessions
                    if s.name == agent.tmux_session_name
                ),
                None,
            )
            if not tmux_session:
                return False
            pane = tmux_session.attached_window.attached_pane
            for k in keys:
                # literal=False so tmux interprets key names like "Escape"
                await loop.run_in_executor(
                    None,
                    functools.partial(pane.send_keys, k, enter=False, literal=False),
                )
                await asyncio.sleep(0.3)
            logger.info(
                f"[RECOVERY] Sent {keys} to agent {agent_id[:8]} ({agent.cli_type}) to break stuck TUI"
            )
            return True
        except Exception as e:
            logger.warning(
                f"[RECOVERY] Failed to send recovery keys to {agent_id[:8]}: {e}"
            )
            return False
        finally:
            await loop.run_in_executor(None, session.close)

    async def send_raw_key(self, agent_id: str, key: str) -> bool:
        """Send a single literal tmux key name (e.g. "Escape") to an
        agent's pane, unlike send_message_to_agent which sends text
        followed by Enter. Same tmux-lookup shape as
        send_recovery_keystrokes above, but user-triggered (tmux viewer's
        Esc button) rather than mechanical-recovery-triggered, and for
        exactly one key rather than a CLI-specific sequence."""
        import functools

        loop = asyncio.get_event_loop()
        session = self.db_manager.get_session()
        try:
            agent = await loop.run_in_executor(
                None, lambda: session.query(Agent).filter_by(id=agent_id).first()
            )
            if not agent or not agent.tmux_session_name:
                return False
            tmux_session = await loop.run_in_executor(
                None, self._find_tmux_session, agent.tmux_session_name
            )
            if not tmux_session:
                return False
            pane = tmux_session.attached_window.attached_pane
            # literal=False so tmux interprets the key name instead of
            # typing it as text (matches send_recovery_keystrokes above).
            await loop.run_in_executor(
                None,
                functools.partial(pane.send_keys, key, enter=False, literal=False),
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to send key {key!r} to agent {agent_id[:8]}: {e}")
            return False
        finally:
            await loop.run_in_executor(None, session.close)

    async def send_message_to_agent(self, agent_id: str, message: str, session=None):
        """Send a message to an agent's tmux session.

        Delegates to AgentMessenger (SOLID review 3.1) — kept as a public
        method here since guardian.py, monitor.py, and others depend on
        AgentManager exposing this directly.
        """
        return await self._messenger.send_message_to_agent(
            agent_id, message, session=session
        )

    async def broadcast_message_to_all_agents(
        self, sender_agent_id: str, message: str
    ) -> int:
        """Broadcast a message to all active agents except the sender.

        Args:
            sender_agent_id: ID of the agent sending the message
            message: Message content to broadcast

        Returns:
            Number of agents the message was sent to

        Note: this stays on AgentManager (rather than delegating to
        AgentMessenger like send_message_to_agent) because it calls
        self.send_message_to_agent — several tests patch that method on the
        AgentManager instance and assert this loop invoked it. Delegating
        the loop itself to AgentMessenger would call AgentMessenger's own
        send_message_to_agent instead, silently bypassing that mock.
        """
        # FIX #3: Restored dropped log line.
        logger.info(f"Broadcasting message from agent {sender_agent_id}")
        session = self.db_manager.get_session()
        try:
            # Get all active agents except the sender
            active_agents = (
                session.query(Agent)
                .filter(Agent.status != "terminated", Agent.id != sender_agent_id)
                .all()
            )

            if not active_agents:
                logger.info(
                    f"No active agents to broadcast to (excluding sender {sender_agent_id})"
                )
                return 0

            # Format message with broadcast prefix
            formatted_message = (
                f"\n[AGENT {sender_agent_id[:8]} BROADCAST]: {message}\n"
            )

            # Send to all active agents
            recipient_count = 0
            for agent in active_agents:
                try:
                    await self.send_message_to_agent(
                        agent.id, formatted_message, session=session
                    )
                    recipient_count += 1

                    # Log the broadcast
                    log_entry = AgentLog(
                        agent_id=agent.id,
                        log_type="agent_communication",
                        message=f"Received broadcast from agent {sender_agent_id[:8]}",
                        details={
                            "sender_id": sender_agent_id,
                            "recipient_id": agent.id,
                            "message_type": "broadcast",
                            "message_content": message[:200],  # Truncate for storage
                            "timestamp": utc_now().isoformat(),
                        },
                    )
                    session.add(log_entry)
                except Exception as e:
                    logger.error(f"Failed to send broadcast to agent {agent.id}: {e}")

            session.commit()
            logger.info(
                f"Broadcast from {sender_agent_id[:8]} sent to {recipient_count} agents"
            )
            return recipient_count

        except Exception as e:
            logger.error(f"Failed to broadcast message: {e}")
            session.rollback()
            return 0
        finally:
            session.close()

    async def send_direct_message(
        self, sender_agent_id: str, recipient_agent_id: str, message: str
    ) -> bool:
        """Send a direct message from one agent to another.

        Args:
            sender_agent_id: ID of the agent sending the message
            recipient_agent_id: ID of the agent receiving the message
            message: Message content

        Returns:
            True if message was sent successfully, False otherwise

        Note: stays on AgentManager rather than delegating to AgentMessenger
        for the same reason as broadcast_message_to_all_agents above — it
        calls self.send_message_to_agent, which tests patch at this level.
        """
        logger.info(
            f"Sending message from agent {sender_agent_id[:8]} to {recipient_agent_id[:8]}"
        )

        session = self.db_manager.get_session()
        try:
            # Verify recipient exists and is active
            recipient = session.query(Agent).filter_by(id=recipient_agent_id).first()
            if not recipient:
                logger.warning(f"Recipient agent {recipient_agent_id} not found")
                return False

            if recipient.status == "terminated":
                logger.warning(f"Recipient agent {recipient_agent_id} is terminated")
                return False

            # Format message with direct message prefix
            formatted_message = f"\n[AGENT {sender_agent_id[:8]} TO AGENT {recipient_agent_id[:8]}]: {message}\n"

            # Send the message
            await self.send_message_to_agent(
                recipient_agent_id, formatted_message, session=session
            )

            # Log the communication
            log_entry = AgentLog(
                agent_id=recipient_agent_id,
                log_type="agent_communication",
                message=f"Received direct message from agent {sender_agent_id[:8]}",
                details={
                    "sender_id": sender_agent_id,
                    "recipient_id": recipient_agent_id,
                    "message_type": "direct",
                    "message_content": message[:200],  # Truncate for storage
                    "timestamp": utc_now().isoformat(),
                },
            )
            session.add(log_entry)
            session.commit()

            logger.info(
                f"Direct message sent from {sender_agent_id[:8]} to {recipient_agent_id[:8]}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to send direct message: {e}")
            session.rollback()
            return False
        finally:
            session.close()

    @staticmethod
    def _build_repo_context(session, project_id: Optional[str], repo_id: Optional[str]) -> str:
        """Multi-repo project context for prompt injection (REQ-17/18).

        Empty string whenever there's nothing to scope a repo list to
        (no project_id) or the common single-repo case (<=1 ProjectRepo
        row) -- REQ-21/NFR-01, no behavior change from before repo
        awareness existed.

        repo_id: the requesting task's own ProjectRepo, if known. When it
        resolves to one of the project's repos, that repo is called out
        as WRITABLE and every sibling as READ-ONLY (REQ-18) -- a stale/
        unmatched repo_id degrades to the REQ-17-only repo list, same as
        not passing one at all.
        """
        from src.core.repo_resolution import get_project_repos

        if not project_id:
            return ""
        repos = get_project_repos(session, project_id)
        if len(repos) <= 1:
            return ""

        context = "\n## PROJECT REPOSITORIES\nThis project spans multiple repos:\n"
        for repo in repos:
            context += f"- {repo.label}: {repo.path}\n"

        if repo_id and any(repo.id == repo_id for repo in repos):
            context += "\n## REPO ACCESS\n"
            for repo in repos:
                access = "WRITABLE" if repo.id == repo_id else "READ-ONLY"
                context += f"- {repo.label}: {access}\n"

        return context

    async def get_project_context(
        self, workflow_id: Optional[str] = None, repo_id: Optional[str] = None
    ) -> str:
        """Get current project context for task enrichment.

        Args:
            workflow_id: Workflow.id, when the caller has one in scope --
                resolved to AutopilotProject.id internally (Workflow.
                project_id) so the multi-repo section (REQ-17/18/21) can be
                scoped to it. When it doesn't resolve to a real workflow
                (or that workflow has no project_id), behavior is
                unchanged from before repo awareness existed -- no new
                text, since there's nothing to scope a repo list to.
            repo_id: the requesting task's own ProjectRepo.id, when known
                -- see _build_repo_context for how it's used (REQ-18).

        Returns:
            Formatted project context string
        """
        # Defensive sanitization: a caller passing a malformed/oversized
        # id (never expected, but these ultimately trace back to
        # user-influenced Task rows) degrades to "no id given" rather than
        # failing a DB query or bloating the prompt.
        if workflow_id and len(workflow_id) > 200:
            workflow_id = None
        if repo_id and len(repo_id) > 200:
            repo_id = None

        session = self.db_manager.get_session()
        try:
            # Get active tasks
            active_tasks = (
                session.query(Task)
                .filter(Task.status.in_(["pending", "assigned", "in_progress"]))
                .all()
            )

            # Get recent completions
            recent_tasks = (
                session.query(Task)
                .filter(Task.status == "done")
                .order_by(Task.completed_at.desc())
                .limit(5)
                .all()
            )

            # Get active agents
            active_agents = (
                session.query(Agent).filter(Agent.status != "terminated").all()
            )

            # Format context
            context = f"""
## PROJECT STATUS
- Active Tasks: {len(active_tasks)}
- Active Agents: {len(active_agents)}
- Recent Completions: {len(recent_tasks)}

## ACTIVE TASKS
"""
            for task in active_tasks[:10]:
                context += f"- {task.id[:8]}: {(task.enriched_description or task.raw_description)[:100]}...\n"

            if recent_tasks:
                context += "\n## RECENT COMPLETIONS\n"
                for task in recent_tasks:
                    context += f"- {(task.enriched_description or task.raw_description)[:100]}...\n"

            # Multi-repo project support (REQ-17/18/21). Resolved from
            # workflow_id, not passed as project_id directly -- the only
            # thing every caller actually has in scope is a workflow or
            # task, never a bare AutopilotProject.id.
            if workflow_id:
                from src.core.database import Workflow

                project = (
                    session.query(Workflow.project_id).filter_by(id=workflow_id).scalar()
                )
            else:
                project = None
            # Isolated: a failure building the repo section (e.g. a
            # transient DB error) must not blow away the PROJECT STATUS/
            # ACTIVE TASKS text already built above.
            try:
                context += self._build_repo_context(session, project, repo_id)
            except Exception as e:
                logger.warning(f"Failed to build repo context: {e}")

            return context

        except Exception as e:
            logger.error(f"Failed to get project context: {e}")
            return "Project context unavailable"
        finally:
            session.close()
