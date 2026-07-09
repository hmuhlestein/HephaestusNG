"""Agent management system for Hephaestus."""

import asyncio
import logging
import shlex
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import libtmux

from src.core.constants import AUTOPILOT_STATE_DIR, CONTEXT_DIR_NAME, DESIGN_CONTEXT_SUBDIR, DESIGN_SUBDIR
from src.core.database import (
    Agent,
    AgentLog,
    BoardConfig,
    DatabaseManager,
    Task,
    get_db,
)
from src.core.simple_config import get_config
from src.core.worktree_manager import WorktreeManager
from src.interfaces import LLMProviderInterface, get_cli_agent

logger = logging.getLogger(__name__)


class AgentManager:
    """Manages agent lifecycle and tmux sessions."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        llm_provider: LLMProviderInterface,
        phase_manager=None,
    ):
        """Initialize agent manager.

        Args:
            db_manager: Database manager instance
            llm_provider: LLM provider for generating prompts
            phase_manager: Phase manager instance (optional)
        """
        self.db_manager = db_manager
        self.llm_provider = llm_provider
        self.phase_manager = phase_manager
        self.config = get_config()
        self.tmux_server = libtmux.Server()
        
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

        Returns:
            Created agent

        Raises:
            ValueError: If task is None
        """
        if task is None:
            raise ValueError(
                "task is REQUIRED for create_agent_for_task — cannot create agent without a task"
            )

        agent_id = str(uuid.uuid4())

        # Centralized phase-config fallback: if the caller didn't supply the phase's
        # CLI/thinking config, derive it from task.phase_id here. This guarantees every
        # call site (create_task paths, recovery, API endpoint, monitor transitions)
        # gets the per-phase tool/model/glm/thinking_level without each having to
        # remember to fetch and forward it.
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
                        phase_glm_token_env = _ph.glm_api_token_env
                        phase_thinking_level = _ph.thinking_level
            except Exception as e:
                logger.warning(f"Could not derive phase config for task {task.id}: {e}")

        # Use phase config with fallback to global defaults
        cli_type = phase_cli_tool or cli_type or self.config.default_cli_tool

        logger.info(f"Creating {cli_type} agent {agent_id} for task {task.id}")

        try:
            # Gather inbound context (design doc, qa_spec, project context) to copy
            # into the worktree's git-excluded .hephaestus/ dir, so the agent never
            # has to read out-of-tree paths.
            context_files = self._gather_worktree_context(task)

            # Check if workflow has a shared worktree (all phases use same worktree)
            shared_worktree = None
            if task.workflow_id:
                from src.core.database import Workflow

                with self.db_manager.get_session() as session:
                    wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
                    if wf and wf.working_directory:
                        # If working_directory contains '.worktrees/', it's a shared worktree
                        if ".worktrees/" in wf.working_directory:
                            # Verify the worktree directory still exists before using it
                            wt_path = Path(wf.working_directory)
                            if wt_path.exists() and (wt_path / ".git").exists():
                                shared_worktree = wf.working_directory
                                self.branch_manager.reload(wt_path)
                            else:
                                logger.warning(
                                    f"Workflow working_directory {wf.working_directory} no longer exists "
                                    f"(worktree was likely cleaned up). Falling back to project root."
                                )
                                # Clear the stale working_directory from the workflow
                                wf.working_directory = None
                                session.commit()

            if shared_worktree:
                # Use the shared worktree — all phases commit here
                branch_path = shared_worktree
                branch_name = f"shared-{task.workflow_id[:8]}"
                logger.info(
                    f"Using shared worktree for agent {agent_id} "
                    f"at {branch_path} (all phases commit here)"
                )
            else:
                # Create an isolated worktree for the agent (legacy path)
                branch_info = self.branch_manager.create_agent_branch(
                    agent_id=agent_id,
                    parent_agent_id=getattr(task, "created_by_agent_id", None),
                    context_files=context_files,
                )
                branch_path = branch_info["working_directory"]
                branch_name = branch_info["branch_name"]
                logger.info(
                    f"Created worktree {branch_name} for agent {agent_id} "
                    f"at {branch_path} (context: {sorted(context_files) if context_files else 'none'})"
                )
                self.branch_manager.switch_to_branch(branch_name)

            # 2. Generate system prompt
            # Get phase name for specialized prompts
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

            # 3. Prepare environment variables for GLM if needed
            # Use phase config with fallback to global defaults
            model = phase_cli_model or getattr(self.config, "cli_model", "sonnet")
            env_vars = self._build_glm_env_vars(
                model, phase_glm_token_env, agent_id, label="agent"
            )

            # 3.5. Set MCP_TOOL_TIMEOUT if workflow has human approval enabled
            # (Claude Code agents only.)
            timeout_ms = self._resolve_mcp_timeout_ms(
                cli_type, task.workflow_id, label="agent"
            )
            if timeout_ms is not None:
                env_vars = env_vars or {}
                env_vars["MCP_TOOL_TIMEOUT"] = str(timeout_ms)

            # Fallback for MCP tool calls that omit required ID params —
            # models frequently drop them (e.g. hephaestus_save_memory's
            # agent_id) even when the prompt's own example shows them filled
            # in. mcp/claude_mcp_client.py falls back to these env vars
            # instead of hard-failing the call. workflow_id/phase_id are
            # only set when this task actually has one (standalone tasks
            # have neither).
            env_vars = env_vars or {}
            env_vars["HEPHAESTUS_AGENT_ID"] = agent_id
            env_vars["HEPHAESTUS_TASK_ID"] = task.id
            if task.workflow_id:
                env_vars["HEPHAESTUS_WORKFLOW_ID"] = task.workflow_id
            if task.phase_id:
                env_vars["HEPHAESTUS_PHASE_ID"] = task.phase_id

            # 4. Create tmux session IN THE WORKTREE with env vars
            # Use agent_id for unique session names (not task_id which can be reused on restarts)
            session_name = f"{self.config.tmux_session_prefix}_{agent_id[:8]}"
            tmux_session = self._create_tmux_session(
                session_name, working_directory=branch_path, env_vars=env_vars
            )

            # 5. Launch CLI agent
            cli_agent = get_cli_agent(cli_type)

            # Resolve phase_name before launching so pi can reference the agent file
            phase_name = None
            phase_order = "?"
            if task.phase_id:
                from src.core.database import Phase

                session = self.db_manager.get_session()
                try:
                    if task.phase_id.isdigit():
                        phase = (
                            session.query(Phase)
                            .filter_by(
                                order=int(task.phase_id), workflow_id=task.workflow_id
                            )
                            .first()
                        )
                    else:
                        phase = session.query(Phase).filter_by(id=task.phase_id).first()
                    if phase:
                        phase_name = phase.name
                        phase_order = str(phase.order)
                finally:
                    session.close()

            # Per-turn reasoning budget: per-phase override → global config → "medium"
            thinking_level = phase_thinking_level or getattr(
                self.config, "cli_thinking_level", "medium"
            )

            # Complexity-adaptive reasoning: a phase's base budget expresses INTENT
            # (mechanical=low, reasoning=high). Scale the *reasoning* phases down to the
            # design's actual complexity so a trivial design (a calculator) doesn't get
            # 'high' thinking → over-engineering (e.g. 400+ tickets). Classified once per
            # workflow via a single fast LLM call; mechanical phases keep their low budget.
            try:
                if thinking_level in ("high", "medium") and getattr(
                    task, "workflow_id", None
                ):
                    if not hasattr(self, "_complexity_cache"):
                        self._complexity_cache = {}
                    complexity = self._complexity_cache.get(task.workflow_id)
                    if complexity is None:
                        # Classify from the actual DESIGN, not the phase task prompt.
                        # Source priority (whatever exists in the worktree): the original
                        # design doc → injected design → phase-1 requirements. Fall back to
                        # the task text only if none are present.
                        design_text = ""
                        try:
                            from pathlib import Path as _P

                            if working_directory:
                                wd = _P(working_directory)
                                cands = []
                                dq = wd / DESIGN_CONTEXT_SUBDIR
                                if dq.is_dir():
                                    cands += sorted(dq.glob("*.md"))
                                cands += [
                                    wd / CONTEXT_DIR_NAME / "design.md",
                                    wd / CONTEXT_DIR_NAME / "design_document.md",
                                    wd / "docs" / "requirements_analysis.md",
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
                            design_text
                        )
                        self._complexity_cache[task.workflow_id] = complexity
                    # low complexity → low thinking; medium → medium; high → keep phase base
                    if complexity == "low":
                        thinking_level = "low"
                    elif complexity == "medium" and thinking_level == "high":
                        thinking_level = "medium"
                    logger.info(
                        f"[COMPLEXITY] phase budget {phase_thinking_level} → {thinking_level} "
                        f"(design complexity={complexity}) for agent {agent_id[:8]}"
                    )
            except Exception as e:
                logger.debug(f"[COMPLEXITY] adaptive thinking skipped: {e}")

            # Generate deterministic session ID for persistent agent sessions.
            # Same project + design + role = same session across gotos (§10.1.1).
            #
            # EXCLUDED: validator/result_validator/diagnostic agents. The key is
            # (project_id, design_slug, phase_name) -- it doesn't factor in
            # agent_type or task_id at all. A diagnostic agent is deliberately
            # assigned the SAME phase_id as the stuck phase it's investigating
            # (see monitor.py's _create_diagnostic_agent), so it would compute
            # the identical session_id as every normal phase agent that has
            # ever worked that phase. Since the CLI (`pi --session-id X`)
            # resumes an existing session for that ID rather than starting
            # fresh, the diagnostic agent would silently resume a PRIOR phase
            # agent's live conversation -- inheriting that agent's old
            # "=== TASK ASSIGNMENT ===" header (its agent_id, its task_id) as
            # part of the resumed context, on top of its own fresh
            # --append-system-prompt. Observed live: a diagnostic agent spent
            # its entire run trying to close out a stale, already-terminated
            # agent's task using that agent's identity, because its resumed
            # session told it that was who it was -- never touching its own
            # actual diagnostic task. These agent types are one-shot
            # investigations/verifications, never meant to share warm context
            # across runs, so they must always get a fresh (empty) session_id.
            session_id = ""
            if task.workflow_id and agent_type not in (
                "validator",
                "result_validator",
                "diagnostic",
            ):
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
                            # Feature-model workflows (the standard autopilot
                            # shape since the Feature Architect split) never
                            # populate project_id/design_slug — their
                            # launch_params carry project_path/feature_id
                            # instead. Without this fallback, session_id was
                            # silently always "" for every such workflow,
                            # meaning phases that are supposed to share a
                            # session (e.g. architectural_review resuming
                            # architecture_design's "warm context", per
                            # session_roles in workflow.yaml) always got a
                            # cold --no-session agent instead — while the
                            # phase's own prompt still claimed continuity.
                            _pid = _lp.get("project_id") or _lp.get("project_path", "")
                            _dsl = _lp.get("design_slug") or _lp.get("feature_id", "")
                            if _pid and _dsl and phase_name:
                                from src.autopilot.phases import get_session_id

                                session_id = get_session_id(_pid, _dsl, phase_name)
                    finally:
                        _s.close()
                except Exception as e:
                    logger.debug(f"[SESSION] Could not generate session ID: {e}")

            if session_id:
                logger.info(
                    f"[SESSION] Using session ID: {session_id} for phase {phase_name}"
                )

            launch_command = cli_agent.get_launch_command(
                system_prompt=system_prompt,
                task_id=task.id,
                model=model,  # Pass phase-specific or global model
                thinking_level=thinking_level,
                phase_name=phase_name,
                agent_id=agent_id,
                workflow_id=task.workflow_id,
                phase_id=task.phase_id,
                session_id=session_id,
            )

            # Send launch command to tmux
            pane = tmux_session.attached_window.attached_pane

            # If using GLM, export env vars in the shell first
            if env_vars:
                logger.info(
                    f"Exporting GLM environment variables in shell for agent {agent_id}"
                )
                for key, value in env_vars.items():
                    pane.send_keys(f'export {key}="{value}"', enter=True)
                # Brief pause to ensure exports complete
                await asyncio.sleep(0.5)

            # Echo task info to terminal so we can see what the agent is working on
            task_desc = (task.enriched_description or task.raw_description or "")[:200]
            pane.send_keys('echo "="', enter=True)
            pane.send_keys(f'echo "AGENT: {agent_id[:8]}"', enter=True)
            pane.send_keys(f'echo "PHASE: {phase_order}. {phase_name}"', enter=True)
            pane.send_keys(f'echo "TASK: {task_desc}"', enter=True)
            pane.send_keys('echo "="', enter=True)
            await asyncio.sleep(0.3)

            # Now send the claude launch command
            pane.send_keys(
                launch_command, enter=True
            )  # enter=True sends Enter key after command

            # 6. Register agent in database
            session = self.db_manager.get_session()
            agent = Agent(
                id=agent_id,
                system_prompt=system_prompt,
                status="working",
                cli_type=cli_type,
                cli_model=model,
                tmux_session_name=session_name,
                current_task_id=task.id,
                last_activity=datetime.utcnow(),
                health_check_failures=0,
                agent_type=agent_type,  # Set the agent type
            )
            session.add(agent)
            
            # Assign task to agent
            task.assigned_agent_id = agent_id
            task.status = "in_progress"
            task.started_at = datetime.utcnow()

            # Log agent creation
            log_entry = AgentLog(
                agent_id=agent_id,
                log_type="created",
                message=f"Agent created for task: {task.enriched_description[:100]}",
                details={"cli_type": cli_type, "task_id": task.id},
            )
            session.add(log_entry)

            session.commit()

            # Store the agent ID before closing session (to avoid detached instance issues)
            agent_id_to_return = agent.id

            session.close()

            # 7. Send initial task instructions with verification and retry
            logger.info(f"=== INITIAL PROMPT DELIVERY for agent {agent_id} ===")
            logger.info(f"CLI type: {cli_type}")
            logger.info(f"Tmux session: {session_name}")

            # Get the initial message with worktree path
            initial_message = self._format_initial_message(
                task, agent_id, branch_path, agent_type, enriched_data
            )
            logger.info(f"Initial message length: {len(initial_message)} characters")

            # Save the full prompt to /tmp for debugging
            debug_prompt_path = f"/tmp/hephaestus_debug_prompt_{agent_id}.txt"
            with open(debug_prompt_path, "w") as f:
                f.write("=== FULL INITIAL MESSAGE DEBUG ===\n")
                f.write(f"Agent ID: {agent_id}\n")
                f.write(f"Task ID: {task.id}\n")
                f.write(f"Message length: {len(initial_message)} characters\n")
                f.write(f"Timestamp: {datetime.utcnow()}\n")
                f.write(f"{'=' * 50}\n\n")
                f.write(initial_message)
            logger.info(f"🔍 DEBUG: Full initial message saved to: {debug_prompt_path}")

            # Wait for CLI to initialize first
            wait_time = 25
            logger.info(
                f"Waiting {wait_time} seconds for {cli_type} agent {agent_id} to initialize..."
            )
            await asyncio.sleep(wait_time)

            # Check if tmux session is still alive
            if not self.tmux_server.has_session(session_name):
                logger.error(
                    f"Tmux session {session_name} died during initialization wait!"
                )
                raise Exception("Tmux session died during initialization wait")

            # Send initial prompt (or just Enter for OpenCode)
            await self._send_initial_prompt_with_retry(
                pane=pane,
                cli_agent=cli_agent,
                cli_type=cli_type,
                initial_message=initial_message,
                agent_id=agent_id,
                task_id=task.id,
                max_retries=3,
            )

            logger.info(f"=== END INITIAL PROMPT DELIVERY for agent {agent_id} ===")

            # Return a simple object with just the ID to avoid session issues
            class AgentInfo:
                def __init__(self, id):
                    self.id = id

            return AgentInfo(agent_id_to_return)

        except Exception as e:
            logger.error(f"Failed to create agent: {e}")
            # Clean up on failure
            try:
                # Kill tmux session if it exists
                if "tmux_session" in locals():
                    tmux_session.kill_session()
                    logger.info(f"Killed tmux session {session_name}")
            except Exception as cleanup_error:
                logger.error(
                    f"Failed to kill tmux session during cleanup: {cleanup_error}"
                )

            # Mark agent as terminated and task as failed in database
            try:
                cleanup_session = self.db_manager.get_session()
                try:
                    # Mark agent as terminated if it was created
                    if "agent_id" in locals():
                        agent_record = (
                            cleanup_session.query(Agent).filter_by(id=agent_id).first()
                        )
                        if agent_record:
                            agent_record.status = "terminated"
                            agent_record.current_task_id = None  # Clear stale reference
                            agent_record.terminated_at = datetime.utcnow()
                            logger.info(f"Marked agent {agent_id} as terminated")

                    # Mark task as failed
                    task_record = (
                        cleanup_session.query(Task).filter_by(id=task.id).first()
                    )
                    if task_record:
                        task_record.status = "failed"
                        task_record.failure_reason = f"Agent creation failed: {str(e)}"
                        task_record.completed_at = datetime.utcnow()
                        logger.info(f"Marked task {task.id} as failed")

                    cleanup_session.commit()
                except Exception as db_error:
                    logger.error(
                        f"Failed to update database during cleanup: {db_error}"
                    )
                    cleanup_session.rollback()
                finally:
                    cleanup_session.close()
            except Exception as session_error:
                logger.error(
                    f"Failed to get database session during cleanup: {session_error}"
                )

            raise

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
            existing = self.tmux_server.get_by_id(session_name)
            if existing:
                existing.kill_session()

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
            # Keep: SGR color sequences (\x1b[...m)
            # Strip: everything else aggressively
            _ansi_strip = (
                r"s/\x1b\][^\x07]*\x07//g; "  # OSC with BEL
                r"s/\x1b\][^\x1b]*\x1b\\\\//g; "  # OSC with ST
                r"s/\x1b\[[?]?[0-9;]*[^0-9;m]//g; "  # All CSI/DEC except m (color)
                r"s/\x1b[^\x1b\x5b\x5d]//g; "  # Any other bare ESC sequences
                r"s/\r//g"
            )
            pipe_cmd = f"perl -pe {shlex.quote(_ansi_strip)} >> {shlex.quote(str(transcript_path))}"
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
            # architecture.md and requirements_analysis.md from previous phases.
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
                            req_path = worktree_docs / "requirements_analysis.md"
                            if req_path.exists():
                                try:
                                    context["requirements_analysis.md"] = (
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
                    pane.send_keys(chunk)  # No enter=True, just send the text
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
                    pane.send_keys(chunk)  # No enter=True, just send the text
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

    async def terminate_agent(self, agent_id: str):
        """Terminate an agent and clean up resources.

        Args:
            agent_id: ID of agent to terminate
        """
        logger.info(f"Terminating agent {agent_id}")

        session = self.db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                logger.warning(f"Agent {agent_id} not found")
                return

            # Preserve any uncommitted work before teardown so a kill/restart is
            # non-destructive — a resume then continues from the committed branch
            # state instead of losing in-flight work. No-op if already clean/merged.
            try:
                wip = self.branch_manager.commit_changes(
                    agent_id, f"[WIP] Auto-saved on terminate of agent {agent_id[:8]}"
                )
                if isinstance(wip, dict) and wip.get("files_changed"):
                    logger.info(
                        f"[TERMINATE] Saved WIP for agent {agent_id[:8]}: "
                        f"{wip['commit_sha'][:8]} ({wip['files_changed']} file(s))"
                    )
            except Exception as e:
                logger.debug(f"[TERMINATE] WIP commit skipped for {agent_id[:8]}: {e}")

            # Capture pane PIDs and final output BEFORE killing the tmux session
            pane_pids = []
            final_output = ""
            if agent.tmux_session_name:
                try:
                    import subprocess

                    result = subprocess.run(
                        [
                            "tmux",
                            "list-panes",
                            "-t",
                            agent.tmux_session_name,
                            "-F",
                            "#{pane_pid}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    if result.returncode == 0:
                        pane_pids = [
                            p.strip()
                            for p in result.stdout.strip().split("\n")
                            if p.strip()
                        ]
                except Exception:
                    pass

                try:
                    if self.tmux_server.has_session(agent.tmux_session_name):
                        for tmux_sess in self.tmux_server.sessions:
                            if tmux_sess.name == agent.tmux_session_name:
                                pane = tmux_sess.attached_window.attached_pane
                                # Capture full scrollback for both tmux log and AgentLog.
                                full_scrollback = "\n".join(
                                    pane.cmd("capture-pane", "-p", "-S", "-").stdout
                                )
                                final_output = full_scrollback

                                # Write complete scrollback to .hephaestus/tmux/ (git-excluded run artifact).
                                _task = (
                                    session.query(Task)
                                    .filter_by(id=agent.current_task_id)
                                    .first()
                                )
                                if _task and _task.phase_id and _task.workflow_id:
                                    try:
                                        from pathlib import Path as _P

                                        from src.core.database import (
                                            Phase as _Phase,
                                        )
                                        from src.core.database import (
                                            Workflow as _WF,
                                        )

                                        _phase = (
                                            session.query(_Phase)
                                            .filter_by(id=_task.phase_id)
                                            .first()
                                        )
                                        _wf = (
                                            session.query(_WF)
                                            .filter_by(id=_task.workflow_id)
                                            .first()
                                        )
                                        if _phase and _wf and _wf.working_directory:
                                            tmux_dir = (
                                                _P(_wf.working_directory)
                                                / CONTEXT_DIR_NAME
                                                / "tmux"
                                            )
                                            tmux_dir.mkdir(parents=True, exist_ok=True)
                                            log_file = (
                                                tmux_dir
                                                / f"{_phase.name}_{agent_id[:8]}.log"
                                            )
                                            from src.interfaces.cli_interface import (
                                                get_cli_agent,
                                            )

                                            try:
                                                _cli = get_cli_agent(agent.cli_type)
                                                clean_scrollback = (
                                                    _cli.strip_tui_chrome(
                                                        full_scrollback
                                                    )
                                                )
                                            except Exception:
                                                clean_scrollback = full_scrollback
                                            log_file.write_text(clean_scrollback)
                                            logger.info(
                                                f"[TMUX-LOG] Final capture for "
                                                f"{_phase.name}/{agent_id[:8]}: "
                                                f"{len(clean_scrollback)} chars → {log_file.name}"
                                            )
                                    except Exception as _te:
                                        logger.debug(
                                            f"[TMUX-LOG] Final capture write failed: {_te}"
                                        )
                                break
                except Exception as e:
                    logger.debug(f"Could not capture output before terminate: {e}")

            # Kill tmux session using subprocess (more reliable than libtmux)
            if agent.tmux_session_name:
                try:
                    import subprocess

                    result = subprocess.run(
                        ["tmux", "kill-session", "-t", agent.tmux_session_name],
                        capture_output=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        logger.debug(f"Killed tmux session: {agent.tmux_session_name}")
                    else:
                        logger.debug(
                            f"Tmux session {agent.tmux_session_name} not found or already killed"
                        )
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"Timeout killing tmux session {agent.tmux_session_name}"
                    )
                except FileNotFoundError:
                    logger.debug("tmux not available, skipping session cleanup")
                except Exception as e:
                    logger.error(f"Failed to kill tmux session: {e}")

                # Kill any orphaned child processes (opencode, claude, pi) in the session
                # First try graceful SIGINT, then force SIGKILL
                for pane_pid in pane_pids:
                    try:
                        subprocess.run(["kill", "-2", "--", "-" + pane_pid], timeout=3)
                    except Exception:
                        pass
                if pane_pids:
                    time.sleep(1)
                for pane_pid in pane_pids:
                    try:
                        # Check if still alive before sending SIGKILL
                        result = subprocess.run(
                            ["kill", "-0", "-" + pane_pid],
                            capture_output=True,
                            timeout=3,
                        )
                        if result.returncode == 0:
                            subprocess.run(
                                ["kill", "-9", "--", "-" + pane_pid], timeout=3
                            )
                    except Exception:
                        pass

            # Update agent status
            agent.status = "terminated"
            agent.current_task_id = None  # Clear stale task reference
            agent.terminated_at = datetime.utcnow()

            # Log termination with captured output
            log_entry = AgentLog(
                agent_id=agent_id,
                log_type="terminated",
                message="Agent terminated",
                details={
                    "terminated_at": datetime.utcnow().isoformat(),
                    "final_output": final_output,
                },
            )
            session.add(log_entry)

            session.commit()
            logger.info(f"Agent {agent_id} terminated successfully")

        except Exception as e:
            logger.error(f"Failed to terminate agent {agent_id}: {e}")
            session.rollback()
        finally:
            session.close()

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
                agent.status = "terminated"
                agent.terminated_at = datetime.utcnow()
                # Mark task as failed so pipeline can recover
                task_id = agent.current_task_id
                agent.current_task_id = None  # Clear stale reference
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

            # Get task info
            task = session.query(Task).filter_by(id=agent.current_task_id).first()
            if not task:
                logger.error(f"Task {agent.current_task_id} not found")
                return

            # Kill existing tmux session
            if agent.tmux_session_name:
                try:
                    if self.tmux_server.has_session(agent.tmux_session_name):
                        # Find session by iteration (avoid deprecated get_by_id)
                        tmux_session = None
                        for tmux_sess in self.tmux_server.sessions:
                            if tmux_sess.name == agent.tmux_session_name:
                                tmux_session = tmux_sess
                                break

                        if tmux_session:
                            tmux_session.kill_session()
                except Exception:
                    pass

            # Prepare environment variables for GLM if needed
            model = agent.cli_model or getattr(self.config, "cli_model", "sonnet")
            env_vars = self._build_glm_env_vars(
                model, None, agent_id, label="restarted agent"
            )

            # Set MCP_TOOL_TIMEOUT if workflow has human approval enabled
            # (Claude Code agents only.)
            timeout_ms = self._resolve_mcp_timeout_ms(
                agent.cli_type, task.workflow_id, label="restarted agent"
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

            # Create new tmux session with env vars
            # Use agent_id for unique session names (not task_id which can be reused on restarts)
            new_session_name = f"{self.config.tmux_session_prefix}_{agent_id[:8]}_r"
            # Determine working directory from worktree or workflow
            restart_wd = None
            if task.workflow_id:
                from src.core.database import Workflow

                restart_sess = self.db_manager.get_session()
                try:
                    wf = restart_sess.query(Workflow).filter_by(id=task.workflow_id).first()
                    if wf and wf.working_directory and Path(wf.working_directory).exists():
                        self.branch_manager.reload(Path(wf.working_directory))
                        restart_wd = wf.working_directory
                finally:
                    restart_sess.close()
            tmux_session = self._create_tmux_session(
                new_session_name, working_directory=restart_wd, env_vars=env_vars
            )

            # Relaunch agent
            cli_agent = get_cli_agent(agent.cli_type)

            # Resolve phase_name + per-phase thinking budget for the relaunch
            restart_phase_name = None
            restart_thinking_level = None
            if task.phase_id:
                from src.core.database import Phase

                restart_session = self.db_manager.get_session()
                try:
                    if task.phase_id.isdigit():
                        restart_phase = (
                            restart_session.query(Phase)
                            .filter_by(
                                order=int(task.phase_id), workflow_id=task.workflow_id
                            )
                            .first()
                        )
                    else:
                        restart_phase = (
                            restart_session.query(Phase)
                            .filter_by(id=task.phase_id)
                            .first()
                        )
                    if restart_phase:
                        restart_phase_name = restart_phase.name
                        restart_thinking_level = (
                            restart_phase.thinking_level
                        )  # preserve budget across restart
                finally:
                    restart_session.close()

            # Prepend restart context to system prompt so pi sees it immediately
            restart_system_prompt = (
                f"⚠️ RESTART: You were restarted because: {reason}. "
                f"Continue working on task {task.id}. "
                f"Do NOT re-read files you already analyzed. Pick up where you left off.\n\n"
                f"{agent.system_prompt}"
            )

            # Generate session ID for restart — same session, agent picks up where it left off.
            # Same exclusion as create_agent_for_task above: the session key is
            # (project, design, phase_name) only, with no agent_type or agent_id
            # component. A restarted validator/result_validator/diagnostic agent
            # would otherwise resolve to the SAME session_id as ordinary phase
            # agents on that phase and resume THEIR conversation instead of its
            # own (or start cold with someone else's identity, if none of its
            # own runs ever set a session at all).
            session_id = ""
            if task.workflow_id and agent.agent_type not in (
                "validator",
                "result_validator",
                "diagnostic",
            ):
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
                            # See matching comment in create_agent_for_task above —
                            # feature-model workflows use project_path/feature_id,
                            # not project_id/design_slug.
                            _pid = _lp.get("project_id") or _lp.get("project_path", "")
                            _dsl = _lp.get("design_slug") or _lp.get("feature_id", "")
                            if _pid and _dsl and restart_phase_name:
                                from src.autopilot.phases import get_session_id

                                session_id = get_session_id(
                                    _pid, _dsl, restart_phase_name
                                )
                    finally:
                        _s.close()
                except Exception as e:
                    logger.debug(
                        f"[SESSION] Could not generate session ID for restart: {e}"
                    )

            launch_command = cli_agent.get_launch_command(
                system_prompt=restart_system_prompt,
                task_id=task.id,
                model=model,
                phase_name=restart_phase_name,
                agent_id=agent_id,
                workflow_id=task.workflow_id,
                phase_id=task.phase_id,
                thinking_level=restart_thinking_level,
                session_id=session_id,
            )

            pane = tmux_session.attached_window.attached_pane

            # If using GLM, export env vars in the shell first
            if env_vars:
                logger.info(
                    f"Exporting GLM environment variables in shell for restarted agent {agent_id}"
                )
                for key, value in env_vars.items():
                    pane.send_keys(f'export {key}="{value}"', enter=True)
                # Brief pause to ensure exports complete
                await asyncio.sleep(0.5)

            # Launch the CLI (pi/claude/etc.) in the fresh session
            pane.send_keys(launch_command, enter=True)

            # Build the resume message NOW, while the ORM objects are still attached.
            # It MUST carry the agent-id header (via _format_initial_message → "🔑 Your
            # Agent ID:") — otherwise the agent has no agent_id and uses the task_id in
            # MCP calls, so update_task_status/submit_result fail ("Agent not found").
            restart_cli_type = agent.cli_type
            restart_task_id = task.id
            restart_message = (
                f"⚠️ You were restarted ({reason}). Your prior work is committed in this "
                f"worktree — do NOT redo it; run `git log` / `git status` and inspect existing "
                f"files first, then continue toward completion.\n\n"
                + self._format_initial_message(
                    task, agent_id, agent_type=(agent.agent_type or "phase")
                )
            )

            # Update agent record
            agent.tmux_session_name = new_session_name
            agent.status = "working"
            agent.health_check_failures = 0
            agent.last_activity = datetime.utcnow()

            # Log restart
            log_entry = AgentLog(
                agent_id=agent_id,
                log_type="restarted",
                message=f"Agent restarted: {reason}",
                details={"new_session": new_session_name},
            )
            session.add(log_entry)

            session.commit()

            # Deliver the 'continue' message. Launching the CLI alone leaves pi idle at
            # its welcome screen (the resumed-agent-stuck bug) — mirror the normal create
            # path: wait for the CLI to initialize, then send the task as the first turn.
            try:
                await asyncio.sleep(25)  # let the CLI boot before typing into it
                if self.tmux_server.has_session(new_session_name):
                    await self._send_initial_prompt_with_retry(
                        pane=pane,
                        cli_agent=cli_agent,
                        cli_type=restart_cli_type,
                        initial_message=restart_message,
                        agent_id=agent_id,
                        task_id=restart_task_id,
                        max_retries=3,
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

    def get_agent_output(self, agent_id: str, lines: int = 200) -> str:
        """Get recent output from agent's tmux session or stored output for terminated agents.

        Args:
            agent_id: Agent ID
            lines: Number of lines to retrieve

        Returns:
            Recent output text
        """
        session = self.db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                logger.warning(f"Agent {agent_id} not found")
                return ""

            # Orchestrator agents always read from their log file regardless of status.
            if agent.agent_type == "orchestrator":
                return self._get_orchestrator_output(agent, lines)

            # Check if agent is terminated - try transcript log first, then AgentLog
            if agent.status == "terminated":
                logger.debug(
                    f"Agent {agent_id} is terminated, trying transcript log"
                )
                
                # Try transcript log first (has full history)
                transcript_output = self._read_transcript_log(agent, lines)
                if transcript_output:
                    return transcript_output
                
                # Fall back to stored output in AgentLog
                # Get the most recent termination log with output
                termination_log = (
                    session.query(AgentLog)
                    .filter_by(agent_id=agent_id, log_type="terminated")
                    .order_by(AgentLog.timestamp.desc())
                    .first()
                )

                if termination_log and termination_log.details:
                    final_output = termination_log.details.get("final_output", "")
                    if final_output:
                        logger.debug(
                            f"Retrieved stored output for terminated agent {agent_id}"
                        )
                        # If lines parameter is specified, return only the last N lines
                        if lines and lines > 0:
                            output_lines = final_output.split("\n")
                            return "\n".join(output_lines[-lines:])
                        return final_output

                logger.warning(
                    f"No stored output found for terminated agent {agent_id}"
                )
                # Try to get last logs from agent_logs
                recent_logs = (
                    session.query(AgentLog)
                    .filter_by(agent_id=agent_id)
                    .order_by(AgentLog.timestamp.desc())
                    .limit(100)
                    .all()
                )
                if recent_logs:
                    log_lines = []
                    for log in reversed(recent_logs):
                        msg = log.message or ""
                        if log.details:
                            summary = log.details.get("summary", "") or log.details.get(
                                "trajectory_summary", ""
                            )
                            if summary:
                                msg = f"{msg}: {summary[:200]}"
                        if msg:
                            log_lines.append(f"[{log.log_type}] {msg}")
                    return "\n".join(log_lines)
                return "Agent terminated - no output was captured"

            # For live agents, do a one-time load from transcript log for full
            # history, then use tmux capture-pane for clean live updates.
            if not agent.tmux_session_name:
                logger.warning(f"Agent {agent_id} has no tmux session name")
                return ""

            # Always read from transcript log (has ANSI colors from pipe-pane).
            # capture-pane returns plain text (tmux renders to text), so we
            # prefer the transcript which preserves the original ANSI codes.
            transcript_output = self._read_transcript_log(agent, lines)
            if transcript_output:
                return transcript_output

            # Fallback: capture-pane if transcript is unavailable
            logger.debug(
                f"Attempting to access tmux session: {agent.tmux_session_name}"
            )

            # Use has_session instead of deprecated find_where
            has_session = self.tmux_server.has_session(agent.tmux_session_name)
            logger.debug(f"has_session({agent.tmux_session_name}) = {has_session}")
            if not has_session:
                logger.warning(f"Tmux session {agent.tmux_session_name} not found")
                return ""

            logger.debug(f"Finding session by iteration: {agent.tmux_session_name}")
            tmux_session = None
            for tmux_sess in self.tmux_server.sessions:
                if tmux_sess.name == agent.tmux_session_name:
                    tmux_session = tmux_sess
                    break

            logger.debug(f"Session iteration result: {tmux_session}")
            if not tmux_session:
                logger.warning(f"Could not get tmux session {agent.tmux_session_name}")
                return ""

            logger.debug(f"Successfully got tmux session: {tmux_session}")
            pane = tmux_session.attached_window.attached_pane
            # Capture ALL available scrollback — no fixed line limit.
            # The history-limit is set to 1000 on session creation (pipe-pane
            # saves the full transcript to a file independently).
            output = pane.cmd("capture-pane", "-p", "-S", "-").stdout
            text = "\n".join(output) if output else ""

            from src.interfaces.cli_interface import get_cli_agent

            try:
                text = get_cli_agent(agent.cli_type).strip_tui_chrome(text)
            except Exception:
                pass
            return text

        except Exception as e:
            logger.error(f"Failed to get agent output for {agent_id}: {e}")
            return ""
        finally:
            session.close()

    def _read_transcript_log(self, agent, lines: int) -> str:
        """Read output from the pipe-pane transcript log file.
        
        Returns the last `lines` lines from the transcript, or empty string
        if no transcript is available.
        """
        import re
        try:
            # Get the working directory from the agent's task workflow
            working_dir = None
            project_base = None
            from src.core.database import Task
            session = self.db_manager.get_session()
            try:
                # Try current task first, then most recent task
                task = None
                if agent.current_task_id:
                    task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if not task:
                    task = session.query(Task).filter_by(assigned_agent_id=agent.id).order_by(Task.created_at.desc()).first()
                if task and task.workflow:
                    if task.workflow.working_directory:
                        working_dir = task.workflow.working_directory
                    # Get project base_dir from workflow's design
                    if task.workflow.project_id:
                        from src.core.database import AutopilotProject
                        proj = session.query(AutopilotProject).get(task.workflow.project_id)
                        if proj:
                            project_base = proj.base_dir
            finally:
                session.close()
            
            if not working_dir:
                # Search in common locations using the session name
                import glob
                search_paths = []
                if project_base:
                    search_paths.append(project_base)
                search_paths.append(str(Path.home()))
                
                transcript_path = None
                for base in search_paths:
                    pattern = f"{base}/**/{CONTEXT_DIR_NAME}/tmux/{agent.tmux_session_name}.transcript.log"
                    matches = glob.glob(pattern, recursive=True)
                    if matches:
                        transcript_path = Path(max(matches, key=lambda p: Path(p).stat().st_mtime))
                        break
                
                if not transcript_path:
                    return ""
            else:
                transcript_path = Path(working_dir) / CONTEXT_DIR_NAME / "tmux" / f"{agent.tmux_session_name}.transcript.log"
            
            if not transcript_path.exists() or transcript_path.stat().st_size == 0:
                return ""
            
            # Read ALL lines for terminated agents (full history),
            # or last N lines for live agents
            with open(transcript_path, 'r', errors='replace') as f:
                all_lines = f.readlines()
                # Drop trailing empty lines (partial writes from pipe-pane)
                while all_lines and not all_lines[-1].strip():
                    all_lines.pop()
                
                if lines > 0 and agent.status != 'terminated':
                    # Live agents: return last N lines
                    tail_lines = all_lines[-lines:]
                    text = "".join(tail_lines).rstrip()
                else:
                    # Terminated agents: return ALL lines
                    text = "".join(all_lines).rstrip()
            
            # Strip terminal control sequences that pipe-pane might have missed
            # Keep: SGR color sequences (\x1b[...m)
            # Strip: everything else aggressively
            text = re.sub(r'\x1b\][^\x07]*\x07', '', text)  # OSC with BEL
            text = re.sub(r'\x1b\][^\x1b]*\x1b\\\\', '', text)  # OSC with ST
            text = re.sub(r'\x1b\[[?]?[0-9;]*[^0-9;m]', '', text)  # All CSI/DEC except m
            text = re.sub(r'\x1b[^\x1b\x5b\x5d]', '', text)  # Any other bare ESC
            
            # Collapse carriage-return redraws: TUI spinners redraw the same
            # line using \r. Split on \n first, then for each line, if it
            # contains \r, keep only the last segment (final state).
            collapsed = []
            for line in text.split("\n"):
                if "\r" in line:
                    line = line.rsplit("\r", 1)[-1]
                collapsed.append(line.rstrip())
            text = "\n".join(collapsed)
            
            # Strip TUI chrome (prompts, spinners) that ANSI stripping doesn't catch
            from src.interfaces.cli_interface import get_cli_agent
            try:
                text = get_cli_agent(agent.cli_type).strip_tui_chrome(text)
            except Exception:
                pass
            
            return text
                    
        except Exception as e:
            logger.debug(f"Could not read transcript log: {e}")
            return ""

    def _get_orchestrator_output(self, agent, lines: int) -> str:
        """Return the orchestrator's run log as human-readable text."""
        from pathlib import Path as _P

        # Log dir is stored as "LOG_DIR:<path>" in system_prompt.
        log_dir: _P | None = None
        if agent.system_prompt and agent.system_prompt.startswith("LOG_DIR:"):
            log_dir = _P(agent.system_prompt[len("LOG_DIR:") :].strip())
        if log_dir is None or not log_dir.exists():
            # Fall back: latest run-* directory under ~/.hephaestus/autopilot/
            base = _P(AUTOPILOT_STATE_DIR)
            candidates = sorted(base.glob("run-*"), reverse=True)
            log_dir = candidates[0] if candidates else None
        if log_dir is None:
            return "Orchestrator log not found."
        log_file = log_dir / "orchestrator.log"
        if not log_file.exists():
            return f"Orchestrator log not found at {log_file}."
        text = log_file.read_text(errors="replace")
        if lines and lines > 0:
            text_lines = text.splitlines()
            text = "\n".join(text_lines[-lines:])
        return text

    async def send_recovery_keystrokes(self, agent_id: str) -> bool:
        """Send the CLI's mechanical recovery keystrokes (e.g. Esc for pi) to break a
        stuck/looping TUI. Generic + polymorphic via CLIAgentInterface.recovery_keystrokes()
        — the monitor stays harness-agnostic. Returns True if keys were sent."""
        session = self.db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent or not agent.tmux_session_name:
                return False
            if not self.tmux_server.has_session(agent.tmux_session_name):
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
                pane.send_keys(k, enter=False, literal=False)
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
            session.close()

    async def send_message_to_agent(self, agent_id: str, message: str):
        """Send a message to an agent's tmux session.

        Delegates to AgentMessenger (SOLID review 3.1) — kept as a public
        method here since guardian.py, monitor.py, and others depend on
        AgentManager exposing this directly.
        """
        return await self._messenger.send_message_to_agent(agent_id, message)

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
                    await self.send_message_to_agent(agent.id, formatted_message)
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
                            "timestamp": datetime.utcnow().isoformat(),
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
            await self.send_message_to_agent(recipient_agent_id, formatted_message)

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
                    "timestamp": datetime.utcnow().isoformat(),
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

    async def get_project_context(self) -> str:
        """Get current project context for task enrichment.

        Returns:
            Formatted project context string
        """
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

            return context

        except Exception as e:
            logger.error(f"Failed to get project context: {e}")
            return "Project context unavailable"
        finally:
            session.close()

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
