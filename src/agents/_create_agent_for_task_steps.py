"""Named steps for LaunchPipeline.create_agent_for_task -- extracted from
its 534-line body (docs/GOD_FUNCTION_DECOMPOSITION_CANDIDATES.md #1).

Each function below is a verbatim-logic extraction of one section of the
original body -- behavior-preserving, not a rewrite. The only renames are
`self.` -> `pipeline.` (the LaunchPipeline instance, passed explicitly) and
`create_agent_for_task`'s phase-sibling guard, which logged-and-returned-None
in the original but returns the sibling here so the orchestrator owns the
early return (same log, same skip).

The fallback/cleanup failure path deliberately stays in
create_agent_for_task itself: its `"tmux_session" in locals()` /
`"agent_id" in locals()` checks depend on which names are bound in the
orchestrator's OWN scope at the point an exception fires, which only holds
while the try-block's step results are bound there.

Characterization tests pinning the pre-extraction behavior (including the
assign_to_task same-commit race regression and the CLI-fallback flow):
tests/test_agent_manager.py -- TestCreateAgentForTask,
TestCreateAgentForTaskFallback, TestCreateAgentForTaskSessionLimitPause.
"""

import asyncio
import functools
import logging
import shlex
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from src.core.constants import CONTEXT_DIR_NAME, DESIGN_CONTEXT_SUBDIR
from src.core.database import Agent, AgentLog, Task, utc_now
from src.interfaces import LaunchResult

logger = logging.getLogger("src.agents._create_agent_for_task_steps")


class _LaunchPrepResult(NamedTuple):
    branch_path: Optional[str]
    env_vars: Dict[str, str]
    model: Optional[str]
    cli_agent: Any
    tmux_session: Any
    session_name: str
    session_id: Optional[str]
    initial_message: str
    instructions_pointer: str
    instructions_rel_path: str
    agent_type: str


class _LaunchStepResult(NamedTuple):
    launch_result: LaunchResult
    pane: Any
    agent_id_to_return: str
    cli_launch_started_at: Any


def _phase_sibling_guard(pipeline, task: Task) -> Optional[Task]:
    """Phase-sibling guard: don't dispatch if the phase already has
    another active task. Protects against concurrent dispatch from
    different code paths (orchestrator sweep, HTTP route, validator
    spawn) targeting the same phase. Returns the sibling task, or None
    when the phase is clear."""
    from src.autopilot.orchestrator.engine_client import check_phase_sibling_active
    _guard_session = pipeline.db_manager.get_session()
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
            return phase_sibling
    finally:
        _guard_session.close()
    return None


def _insert_stub_agent_row(
    pipeline,
    *,
    agent_id: str,
    cli_type: str,
    agent_type: str,
    task: Task,
    assign_to_task: bool,
) -> Agent:
    # Insert a stub Agent row BEFORE worktree creation so the
    # agent_worktrees.agent_id FK passes.
    # try/except/finally around the whole block: this is a hot,
    # per-dispatch-call path -- a commit() failure (IntegrityError,
    # transient OperationalError) previously propagated with the
    # session never closed or rolled back, leaking a connection on
    # every failure and risking pool exhaustion under repeated retries.
    session = pipeline.db_manager.get_session()
    try:
        agent = Agent(
            id=agent_id,
            system_prompt="(pending: worktree + prompt setup)",
            status="idle",
            cli_type=cli_type,
            agent_type=agent_type,
            current_task_id=task.id,
            last_activity=utc_now(),
            health_check_failures=0,
        )
        session.add(agent)
        if assign_to_task:
            claimed_task = session.query(Task).filter_by(id=task.id).first()
            if claimed_task:
                claimed_task.assigned_agent_id = agent_id
                claimed_task.status = "in_progress"
                claimed_task.started_at = utc_now()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return agent


async def _run_launch_preparations(
    pipeline,
    *,
    task: Task,
    wt_mgr,
    working_directory: Optional[str],
    enriched_data: Dict[str, Any],
    memories: List[Dict[str, Any]],
    project_context: str,
    phase_config,
    agent_id: str,
) -> Tuple:
    """Worktree resolution (git work), system-prompt generation (an LLM
    round-trip), and complexity classification (a second, conditional LLM
    round-trip) -- none reads anything the others produce, they only feed
    steps further below (branch_path, system_prompt, thinking_level
    respectively). Running them concurrently costs max() of the three
    instead of their sum. Confirmed live: a feature_architect launch
    (thinking_level "high", so this DOES run classify_complexity) spent
    ~42s between the stub Agent row committing and the CLI launch command
    actually sending.

    Returns (wt_resolution, system_prompt, thinking_level, phase_name,
    phase_order)."""
    context_files = pipeline._gather_worktree_context(task)
    loop = asyncio.get_event_loop()

    # Resolve phase name/order/thinking BEFORE the parallel block
    # below -- moved up from its old position (after
    # _prepare_launch_environment, near the bottom of this
    # try-block) since it has no actual dependency on the
    # worktree/tmux work that used to sit between here and there;
    # a plain DB lookup + config fallback. Doing it early makes
    # thinking_level available in time for the complexity check to
    # join the same parallel group, and replaces the separate,
    # narrower phase_name-only lookup that used to run right
    # before generate_agent_prompt (same DB info, one query
    # instead of two).
    phase_name, phase_order, thinking_level = pipeline._resolve_phase_name_and_thinking(
        task, phase_config.thinking_level
    )

    async def _resolve_worktree_async():
        # _resolve_worktree does real git work (create_agent_worktree
        # -> git branch + git worktree add, a full checkout) --
        # offloaded to a thread since it's several seconds of
        # blocking subprocess/filesystem work, confirmed live
        # 2026-08-19 investigating intermittent multi-second
        # /health stalls under concurrent dispatch.
        return await loop.run_in_executor(
            None,
            functools.partial(
                pipeline._resolve_worktree,
                task, wt_mgr, create_if_missing=True, agent_id=agent_id,
                context_files=context_files,
            ),
        )

    async def _resolve_complexity_and_thinking():
        """Adaptive reasoning: downgrade thinking_level for a
        workflow whose design turns out simpler than its phase's
        configured budget. Verbatim logic from the original
        sequential block (including the try/except boundaries and
        the log firing on a cache hit too, not just a fresh
        classification) -- only converted to return the resolved
        level instead of mutating thinking_level via closure,
        since it now runs concurrently with the other two below
        rather than after them. Any failure here (LLM error, file
        read error, anything) must never break the launch --
        silently keeps the phase's own thinking_level unchanged,
        exactly like the original.
        """
        local_thinking_level = thinking_level
        try:
            if local_thinking_level in ("high", "medium") and getattr(task, "workflow_id", None):
                if not hasattr(pipeline, "_complexity_cache"):
                    pipeline._complexity_cache = {}
                complexity = pipeline._complexity_cache.get(task.workflow_id)
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
                                wd / CONTEXT_DIR_NAME / "spec.md",
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
                    complexity = await pipeline.llm_provider.classify_complexity(
                        design_text, workflow_id=task.workflow_id
                    )
                    pipeline._complexity_cache[task.workflow_id] = complexity
                if complexity == "low":
                    local_thinking_level = "low"
                elif complexity == "medium" and local_thinking_level == "high":
                    local_thinking_level = "medium"
                logger.info(
                    f"[COMPLEXITY] phase budget {phase_config.thinking_level} → {local_thinking_level} "
                    f"(design complexity={complexity}) for agent {agent_id[:8]}"
                )
        except Exception as e:
            logger.debug(f"[COMPLEXITY] adaptive thinking skipped: {e}")
        return local_thinking_level

    wt_resolution, system_prompt, thinking_level = await asyncio.gather(
        _resolve_worktree_async(),
        pipeline.llm_provider.generate_agent_prompt(
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
        ),
        _resolve_complexity_and_thinking(),
    )
    return wt_resolution, system_prompt, thinking_level, phase_name, phase_order


def _ensure_git_expert_review_approved(pipeline, task: Task, branch_path: str) -> None:
    """For a full-autopilot project (review_mode off), pre-write the same
    .hephaestus/review_approved marker a human's approval would write --
    into both branch_path (the worktree) and the actual repo checkout
    `git rev-parse --git-common-dir` resolves to from it (where git_expert's
    own prompt `cd`s before merging/pushing main) -- so agent-safe-bin/git
    doesn't block a project that has no human review step to wait for.

    _create_integration_worktree does this same write at worktree-creation
    time, but that only covers a feature's FIRST dispatch; a git_expert
    task retried later (the common case, since this same hard floor forces
    a retry) never goes back through that function, re-reading straight
    from Workflow.working_directory instead. Idempotent -- safe to call on
    every git_expert dispatch. Observed live: tasks 03e8b25a and 5d2d8828
    both needed this written by hand after their worktrees already existed.
    """
    try:
        from src.core.database import AutopilotProject, resolve_project_for_workflow

        project_id, _ = resolve_project_for_workflow(task.workflow_id)
        if not project_id:
            return
        with pipeline.db_manager.session_scope() as session:
            project = session.query(AutopilotProject).filter_by(id=project_id).first()
            if not project or project.review_mode:
                return

        import re

        import git as _git

        from src.mcp.autopilot.feature_review_routes import _write_review_approved_marker

        # Mirrors git_expert.yaml's own `git rev-parse --git-common-dir |
        # sed 's|/\.git.*||'` exactly, so this resolves to the identical
        # directory the agent's own merge/push command runs from.
        common_dir = _git.Repo(branch_path).git.rev_parse("--git-common-dir").strip()
        main_repo = re.sub(r"/\.git.*$", "", common_dir)
        _write_review_approved_marker(main_repo)
        _write_review_approved_marker(branch_path)
    except Exception as e:
        logger.warning(f"[GIT-EXPERT] Could not pre-approve non-review-mode worktree {branch_path}: {e}")


async def _prepare_tmux_and_prompt(
    pipeline,
    *,
    cli_type: str,
    task: Task,
    agent_id: str,
    agent_type: str,
    phase_config,
    wt_resolution,
    system_prompt: str,
    enriched_data: Dict[str, Any],
    phase_name: Optional[str],
) -> _LaunchPrepResult:
    branch_path = wt_resolution.branch_path

    if phase_name == "git_expert":
        _ensure_git_expert_review_approved(pipeline, task, branch_path)

    env_vars, model, cli_agent = pipeline._resolve_env_and_model(
        cli_type, task, agent_id, label="agent",
        phase_cli_model=phase_config.cli_model,
        phase_cli_tool=phase_config.phase_cli_tool,
        phase_glm_token_env=phase_config.glm_token_env,
    )

    session_name = f"{pipeline.config.agents.tmux_session_prefix}_{agent_id[:8]}"
    # _prepare_launch_environment's own codegraph pre-warm no longer
    # blocks it (see that method) -- what's left here is
    # _create_tmux_session (several tmux subprocess calls), offloaded
    # to a thread same as _resolve_worktree above.
    loop = asyncio.get_event_loop()
    tmux_session = await loop.run_in_executor(
        None,
        functools.partial(
            pipeline._prepare_launch_environment,
            session_name, branch_path, env_vars, task, phase_name,
            cli_agent=cli_agent,
        ),
    )

    session_id = pipeline._resolve_session_id(
        task, agent_type, phase_name, model,
        excluded_types=("validator", "result_validator", "diagnostic", "arbitration"),
        # feature_review: see _resolve_session_id's own docstring -- a goto
        # re-entry must not echo the earlier reviewer's stale verdict.
        # git_expert: same failure mode via a different trigger -- a hard
        # floor (verify_git_expert_merged_and_pushed) rejects "done" and the
        # task gets retried on the SAME task_id. Resuming the same CLI
        # session let the agent just repeat its prior conclusion ("review
        # mode blocks this locally") without re-testing the actual git
        # command against current on-disk state. Observed live: task
        # 03e8b25a retried twice after the underlying block was fixed, and
        # both retries skipped re-running `git merge` entirely, reusing the
        # resumed session's already-decided answer instead.
        excluded_phases=("feature_review", "git_expert"),
    )

    initial_message = pipeline._format_initial_message(
        task, agent_id, branch_path, agent_type, enriched_data
    )
    instructions_rel_path = pipeline._write_task_instructions(
        branch_path, task.id, initial_message
    )
    instructions_pointer = pipeline._build_instructions_pointer(
        task.id, instructions_rel_path,
        agent_name=f"hephaestus-{phase_name.replace('_', '-')}" if phase_name else None,
    )
    if cli_type == "codex" and session_id:
        instructions_pointer += f"\nHephaestus Session ID: {session_id}"

    return _LaunchPrepResult(
        branch_path=branch_path,
        env_vars=env_vars,
        model=model,
        cli_agent=cli_agent,
        tmux_session=tmux_session,
        session_name=session_name,
        session_id=session_id,
        initial_message=initial_message,
        instructions_pointer=instructions_pointer,
        instructions_rel_path=instructions_rel_path,
        agent_type=agent_type,
    )


async def _send_launch_command_and_record_agent(
    pipeline,
    *,
    prep: _LaunchPrepResult,
    task: Task,
    agent_id: str,
    system_prompt: str,
    cli_type: str,
    thinking_level: Optional[str],
    phase_name: Optional[str],
    phase_order,
) -> _LaunchStepResult:
    # Build and send launch command
    launch_result, pane, cli_launch_started_at = await pipeline._build_and_send_launch_command(
        prep.cli_agent, prep.tmux_session,
        system_prompt=system_prompt, task=task, model=prep.model,
        thinking_level=thinking_level, phase_name=phase_name,
        agent_id=agent_id, session_id=prep.session_id,
        working_directory=prep.branch_path, instructions_pointer=prep.instructions_pointer,
        env_vars=prep.env_vars, label=f"agent {agent_id[:8]}",
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
    # session_scope(), not a manual get_session(): this block had the
    # same three-sequential-statements shape (get_session / commit /
    # close, with no try/finally) that d5fb7f7's audit found and fixed
    # in memory_api.py, but this site was missed by that sweep.
    # Anything raising in between -- session.merge(), the AgentLog
    # construction documented below, session.add(), or a failing
    # commit() itself -- skipped close() entirely, and the enclosing
    # `except Exception` opens its OWN cleanup session rather than
    # closing this one, so the connection leaked outright. Not
    # hypothetical: the comment below records this exact block
    # raising in production.
    with pipeline.db_manager.session_scope() as session:
        agent = session.merge(Agent(
            id=agent_id,
            system_prompt=system_prompt,
            status="working",
            cli_type=cli_type,
            cli_model=prep.model,
            tmux_session_name=prep.session_name,
            working_directory=prep.branch_path,
            current_task_id=task.id,
            last_activity=utc_now(),
            launched_at=utc_now(),
            health_check_failures=0,
            agent_type=prep.agent_type,
        ))
        task.assigned_agent_id = agent_id
        task.status = "in_progress"
        task.started_at = utc_now()
        log_entry = AgentLog(
            agent_id=agent_id, log_type="created",
            # enriched_description is nullable (e.g. a task created
            # directly by review_feature's request_changes path never
            # sets it, only raw_description) -- an unguarded slice here
            # crashed with "'NoneType' object is not subscriptable"
            # AFTER the tmux session was already launched and the CLI
            # command already sent (see pane.send_keys above), so the
            # exception unwound through this function's caller, which
            # then killed the just-launched tmux session and marked the
            # task "failed" -- a perfectly good agent launch destroyed
            # by a crash in what's only ever a log message. Confirmed
            # live: task 146d191d burned 3 real launch attempts (pi,
            # pi fallback, claude fallback) this way, one after another.
            message=f"Agent created for task: {(task.enriched_description or task.raw_description or '')[:100]}",
            details={"cli_type": cli_type, "task_id": task.id},
        )
        session.add(log_entry)
        # Read inside the scope: commit/close now happen on __exit__,
        # preserving the old commit(); agent.id; close() ordering.
        agent_id_to_return = agent.id

    return _LaunchStepResult(
        launch_result=launch_result,
        pane=pane,
        agent_id_to_return=agent_id_to_return,
        cli_launch_started_at=cli_launch_started_at,
    )


async def _deliver_initial_prompt_flow(
    pipeline,
    *,
    prep: _LaunchPrepResult,
    launch: _LaunchStepResult,
    task: Task,
    agent_id: str,
    system_prompt: str,
    cli_type: str,
):
    """Initial-prompt delivery: CLI-ready wait, termination-race check,
    session-death check, launch-failure detection, prompt delivery, and
    the concurrent session-record/instructions-file verification.
    Returns the termination-race result (non-None aborts the launch with
    that value), or None when delivery completed."""
    pane = launch.pane
    launch_result = launch.launch_result
    session_name = prep.session_name

    # Wait for CLI to initialize
    logger.info(f"=== INITIAL PROMPT DELIVERY for agent {agent_id} ===")
    logger.info(f"CLI type: {cli_type}")
    logger.info(f"Tmux session: {session_name}")

    if launch_result.prompt_delivery in (
        LaunchResult.AGENT_FILE, LaunchResult.DEFERRED,
    ) and system_prompt:
        initial_message = system_prompt + "\n\n---\n\n" + prep.initial_message
        pipeline._write_task_instructions(prep.branch_path, task.id, initial_message)
    else:
        initial_message = prep.initial_message

    logger.info(f"Initial message length: {len(initial_message)} characters")
    cli_ready = await pipeline._wait_for_cli_ready(pane, prep.cli_agent, cli_type, agent_id)

    # Termination race check
    term_race_result = await pipeline._check_termination_race(
        agent_id, task.id, session_name, agent_id_to_return=launch.agent_id_to_return,
        task=task,
    )
    if term_race_result is not None:
        return term_race_result

    if not pipeline.tmux_server.has_session(session_name):
        logger.error(f"Tmux session {session_name} died during initialization wait!")
        raise Exception("Tmux session died during initialization wait")

    # Skip once the CLI already confirmed ready -- see
    # _detect_launch_failure's own docstring for why running it anyway
    # risks killing an agent that's already up and doing real work.
    if not cli_ready:
        pipeline._detect_launch_failure(pane, prep.cli_agent, cli_type, session_name)

    # Deliver initial prompt
    await pipeline._deliver_initial_prompt(
        pane, prep.cli_agent, cli_type, prep.instructions_pointer, agent_id, task,
    )
    # Neither reads the other's result: the session-record retry loop
    # only touches cli_agent/session_id, the instructions-file check
    # only reads the tmux pane. Concurrent instead of sequential turns
    # the worst case (~5s record retries + a fixed 15s pane check)
    # into ~15s.
    await asyncio.gather(
        pipeline._record_cli_session(prep.cli_agent, prep.session_id, prep.branch_path, launch.cli_launch_started_at),
        pipeline._verify_instructions_file_read(pane, prep.instructions_rel_path, agent_id),
    )

    logger.info(f"=== END INITIAL PROMPT DELIVERY for agent {agent_id} ===")
    return None
