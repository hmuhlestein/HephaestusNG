"""Startup, shutdown, and restart-notification handlers.

Extracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).
"""

import asyncio
import logging
import os
from typing import Optional

from src.auth.auth_api import router as auth_router
from src.core.database import (
    Agent,
    Task,
    Workflow,
    utc_now,
)
from src.mcp.frontend import create_frontend_routes
from src.mcp.server._shared import _build_phase_dict, _git_expert_already_landed, _tmux_session_alive, app, config, server_state, spawn_background_task
from src.mcp.server.background_loops import background_phase_advancement_sweep, background_queue_processor

# Import routers at module level for test compatibility

logger = logging.getLogger("src.mcp.server.lifecycle")


async def _resume_interrupted_workflows(
    workflow_id: Optional[str] = None,
    project_id: Optional[str] = None,
    reactivate: bool = False,
):
    """Re-drive workflows that were mid-flight when the server last stopped.

    Completed phases are durable (committed to the integration branch) and the DB
    records exactly where each run is. The volatile part is the in-flight agent: its
    tmux session dies with the server. We find phase agents that still think they're
    working but whose tmux is gone, and restart them — restart_agent re-attaches to
    the agent's existing worktree branch (prior commits + context intact) with a
    'continue where you left off' prompt. WIP is preserved because terminate_agent
    auto-commits, and the worktree dir survives a crash regardless.

    Runs on startup (all interrupted workflows) and on demand via the recover
    endpoint: scoped to one workflow_id (a design row's own Resume button), or
    to every workflow in project_id (the project-level Play button's "already
    running" self-conflict path -- see start_pipeline -- cascading into the
    same recovery instead of a bare no-op, since the service loop being up
    doesn't by itself re-drive a workflow stuck on an individually-blocked
    task). reactivate=True flips a paused/failed workflow back to active
    first and resets its failed/blocked tasks too -- the on-demand "Retry"
    behavior, as opposed to the passive startup-wide scan.

    Returns {"resumed": int, "workflows": [ids]}.
    """
    from src.core.database import Feature

    session = server_state.db_manager.get_session()
    result = {"resumed": 0, "workflows": []}
    try:
        if not getattr(server_state, "agent_manager", None):
            logger.warning("[RESUME] agent_manager not ready — skipping resume scan")
            return result

        statuses = ["active", "paused"] + (["failed"] if reactivate else [])
        q = session.query(Workflow).filter(Workflow.status.in_(statuses))
        if workflow_id:
            q = q.filter(Workflow.id == workflow_id)
        elif project_id:
            q = q.filter(Workflow.project_id == project_id)
        active = q.all()
        if not active:
            return result

        # On-demand retry can flip a paused/failed workflow back to active so the
        # monitor re-drives it (and the scan below restarts any orphaned agents).
        if reactivate:
            for wf in active:
                if wf.status == "paused" and wf.paused_by == "review":
                    # A "review" pause means a human decision (approve/
                    # request changes) is outstanding -- only review_feature
                    # (POST /features/{id}/review) may clear it, since it's
                    # the only endpoint that records feature.review_status/
                    # reviewed_at. This on-demand Retry/reactivate path used
                    # to force through it like any other pause, letting a
                    # design's "Resume" click (or the project Play button's
                    # already-running fallback) silently clear a pending
                    # human review with no approval ever recorded. Same bug
                    # class as resume_feature's identical fix.
                    logger.info(f"[RESUME] Workflow {wf.id[:8]} is review-paused — skipping reactivate")
                    continue
                if wf.status in ("paused", "failed"):
                    if wf.status == "paused":
                        # cascade_to_feature=False: the Feature handling a
                        # few lines below already covers both "paused" and
                        # "failed" features uniformly for this function --
                        # a second, narrower cascade here would be redundant.
                        from src.autopilot.orchestrator.engine_client import resume_workflow

                        resume_workflow(wf.id, force=True, cascade_to_feature=False, session=session)
                    else:
                        wf.status = "active"
                        wf.paused_by = None
                        # A manual/on-demand recovery starts a fresh attempt.  If
                        # the exhausted-retry reason is left behind, status
                        # derivation and the phase manager can immediately treat
                        # the reactivated workflow as paused again, even though
                        # its failed tasks were reset and re-dispatched.
                        wf.status_reason = None
                    # A workflow that failed by exhausting max_total_gotos
                    # (or the arbitration cap that follows it) needs a
                    # genuinely fresh budget, not just a status flip -- the
                    # persisted total_gotos counter never decreases on its
                    # own, so without this a retried workflow re-exceeds the
                    # SAME exhausted limit on its very next evaluation,
                    # instantly re-failing with zero real attempt in
                    # between. gotos_reset_at is the cutoff
                    # _trigger_arbitration's own per-phase cap now uses to
                    # stop counting historical (pre-retry) arbitration
                    # attempts against this fresh run.
                    wf.total_gotos = 0
                    wf.gotos_reset_at = utc_now()
                    # Also update the associated feature status
                    feature = session.query(Feature).filter_by(workflow_id=wf.id).first()
                    if feature and feature.status in ("paused", "failed"):
                        feature.status = "active"
            session.commit()

        resumed = 0
        for wf in active:
            # On-demand retry only (never the passive startup-wide scan, which
            # runs with reactivate=False): also reset tasks that outright
            # failed or were individually paused ("blocked", via
            # /api/tasks/{id}/pause), not just ones whose agent process died
            # mid-flight. Without this, clicking Resume/Rerun on a workflow
            # with a genuinely failed or blocked task flips the workflow back
            # to "active" but leaves that task untouched -- status derivation
            # then flips it straight back and nothing appears to have
            # happened (observed live: a task blocked by a per-task pause
            # left the whole workflow re-pausing immediately on every Resume
            # click, since a lone "blocked" task is invisible to both this
            # reset and the orphaned-agent scan below).
            if reactivate:
                stuck_tasks = session.query(Task).filter(Task.workflow_id == wf.id, Task.status.in_(["failed", "blocked"])).all()
                for t in stuck_tasks:
                    t.status = "pending"
                    t.failure_reason = None
                    t.assigned_agent_id = None
                    # This row is reused for the retry -- clear any stale
                    # goto/retry tag from a previous life (see the matching
                    # fix in restart_task_endpoint / orchestrator.py's
                    # per-phase failed-task retry).
                    t.action = ""
                    t.action_target_phase = None
                if stuck_tasks:
                    session.commit()
                    logger.info(f"[RESUME] Workflow {wf.id[:8]}: resetting {len(stuck_tasks)} failed/blocked task(s) for on-demand retry")
                for t in stuck_tasks:
                    try:
                        if server_state.queue_service.should_queue_task():
                            server_state.queue_service.enqueue_task(t.id)
                        else:
                            from src.services.agent_dispatch_service import (
                                AgentDispatchService,
                            )

                            dispatch_context = await AgentDispatchService.build_dispatch_context(
                                task_description_for_rag=t.enriched_description or t.raw_description,
                                phase_id=t.phase_id,
                                workflow_id=t.workflow_id,
                                repo_id=t.repo_id,
                            )
                            agent = await AgentDispatchService.dispatch(
                                task=t,
                                enriched_data={"enriched_description": t.enriched_description},
                                dispatch_context=dispatch_context,
                            )
                            AgentDispatchService.mark_assigned(t.id, agent.id, status="assigned")
                        resumed += 1
                    except Exception as e:
                        logger.warning(f"[RESUME] Failed to restart stuck task {t.id[:8]}: {e}")

            # Only tasks that still need work — a 'done' task advances via the
            # monitor's phase-completion check, not by restarting its old agent.
            task_ids = [
                t.id
                for t in session.query(Task)
                .filter(
                    Task.workflow_id == wf.id,
                    Task.status.in_(["pending", "assigned", "in_progress", "queued"]),
                )
                .all()
            ]
            if not task_ids:
                continue
            orphans = (
                session.query(Agent)
                .filter(
                    Agent.current_task_id.in_(task_ids),
                    Agent.agent_type == "phase",
                    Agent.status.in_(["working", "idle", "starting"]),
                )
                .all()
            )
            # Both _tmux_session_alive and _git_expert_already_landed run
            # real subprocess/git work (up to ~23s combined per orphan,
            # between tmux's 3s and git's two 10s timeouts) -- blocking,
            # same class of issue fixed elsewhere today. This loop runs at
            # startup (blocking every request until it finishes) and on
            # every on-demand Retry click, over however many agents were
            # orphaned by the last restart, so offloaded per-orphan here.
            loop = asyncio.get_event_loop()
            for agent in orphans:
                still_alive = await loop.run_in_executor(None, _tmux_session_alive, agent.tmux_session_name)
                if still_alive:
                    continue  # still alive (e.g., only the monitor restarted) — leave it

                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                landed = task and await loop.run_in_executor(None, _git_expert_already_landed, session, task, config)
                if landed:
                    logger.info(f"[RESUME] Workflow {wf.id[:8]}: orphaned agent {agent.id[:8]}'s git_expert work already landed on {config.git.base_branch} -- marking done instead of redispatching")
                    task.status = "done"
                    task.completed_at = utc_now()
                    task.failure_reason = None
                    task.completion_notes = ((task.completion_notes or "") + "\n[auto-recovered: git work had already landed before the agent's completion call was lost]").strip()
                    from src.autopilot.orchestrator.engine_client import terminate_agent

                    # flush first: sessions are autoflush=False, so the
                    # "done" write above is invisible to terminate_agent's
                    # stray-task query until it hits the DB. Without this
                    # the query still sees "in_progress", matches, and
                    # resets the task to "pending" -- clobbering the
                    # completion this recovery path exists to record.
                    session.flush()
                    terminate_agent(agent.id, session=session)
                    session.commit()
                    # Same as every other "done" transition (see
                    # _complete_task_normally / the human-completion
                    # endpoint) -- a task recovered this way must still
                    # unblock any dependent waiting on it, not just the
                    # normal completion paths.
                    from src.mcp.server._create_task_steps import _dispatch_ready_dependents

                    spawn_background_task(_dispatch_ready_dependents(task.id, task.workflow_id))
                    resumed += 1
                    continue

                logger.info(f"[RESUME] Workflow {wf.id[:8]}: restarting orphaned phase agent {agent.id[:8]} (dead tmux session) to continue from committed state")
                try:
                    await server_state.agent_manager.restart_agent(agent.id, reason="server restarted — resuming interrupted work")
                    resumed += 1
                except Exception as e:
                    logger.warning(f"[RESUME] Failed to restart agent {agent.id[:8]}: {e}")
        result["resumed"] = resumed
        result["workflows"] = [wf.id for wf in active]
        if resumed:
            logger.info(f"[RESUME] Resumed {resumed} interrupted phase agent(s) across {len(active)} workflow(s)")
        return result
    finally:
        session.close()


@app.on_event("startup")
async def startup_event():
    """Initialize server on startup."""
    logger.info("Starting Hephaestus MCP Server...")

    # Several handlers (e.g. autopilot_api.py's repair endpoint) write files
    # under AUTOPILOT_STATE_DIR without their own mkdir guard, previously
    # relying on PersistentPipelineState's constructor having created it as
    # a side effect on first use -- fragile even then, since it depended on
    # a pipeline having started first. Guarantee it exists unconditionally,
    # once, here.
    from pathlib import Path

    from src.core.constants import AUTOPILOT_STATE_DIR

    Path(AUTOPILOT_STATE_DIR).mkdir(parents=True, exist_ok=True)

    await server_state.initialize()

    # Add frontend API routes
    api_router = create_frontend_routes(server_state.db_manager, server_state.agent_manager, server_state.phase_manager)
    app.include_router(api_router)

    # Add authentication routes
    app.include_router(auth_router)

    # Add autopilot routes (configure BEFORE including)
    from src.mcp.autopilot import router as autopilot_router
    from src.mcp.autopilot._shared import configure_autopilot_api

    configure_autopilot_api(
        design_queue_dir=os.environ.get("DESIGN_QUEUE_DIR", ""),
        features_dir=os.environ.get("FEATURES_DIR", ""),
    )
    app.include_router(autopilot_router)

    # Note: tickets_router (M-1: extracted from server.py) is included at
    # module level above, not here — TestClient(app) used without the
    # `with TestClient(app) as client:` context manager never fires this
    # startup event, so including it only here would break those tests.

    # Load phases if folder is specified
    from pathlib import Path

    logger.info("=== PHASE LOADING DEBUG ===")
    logger.info(f"Current working directory: {os.getcwd()}")
    logger.info(f"Environment variables starting with HEPHAESTUS: {[k for k in os.environ.keys() if 'HEPHAESTUS' in k]}")

    phases_folder = os.environ.get("HEPHAESTUS_PHASES_FOLDER")
    logger.info(f"HEPHAESTUS_PHASES_FOLDER value: '{phases_folder}'")

    if phases_folder:
        logger.info(f"Attempting to load workflow phases from: {phases_folder}")

        # Check if folder exists
        full_path = Path(phases_folder)
        if not full_path.is_absolute():
            full_path = Path(os.getcwd()) / phases_folder

        logger.info(f"Full path to phases folder: {full_path}")
        logger.info(f"Folder exists: {full_path.exists()}")
        logger.info(f"Is directory: {full_path.is_dir() if full_path.exists() else 'N/A'}")

        if full_path.exists() and full_path.is_dir():
            # List files in directory
            files = list(full_path.glob("*.yaml"))
            logger.info(f"YAML files found: {len(files)}")
            for f in files:
                logger.info(f"  - {f.name}")

        try:
            from src.phases import PhaseLoader

            logger.info("PhaseLoader imported successfully")

            # Load phases from folder
            logger.info(f"Calling PhaseLoader.load_phases_from_folder('{phases_folder}')")
            workflow_def = PhaseLoader.load_phases_from_folder(phases_folder)
            logger.info(f"Loaded workflow '{workflow_def.name}' with {len(workflow_def.phases)} phases")

            # Load phases configuration (for ticket tracking, result handling, etc.)
            logger.info(f"Loading phases_config.yaml from '{phases_folder}'")
            phases_config = PhaseLoader.load_phases_config(phases_folder)
            logger.info(f"Loaded phases config: enable_tickets={phases_config.enable_tickets}, has_result={phases_config.has_result}")

            # Workflow initialization is handled by SDK's start_workflow() call
            # The phase definitions are loaded but workflow execution is created on-demand
            logger.info("Phases loaded successfully - workflow execution will be created via start_workflow() call")

            # Log phase names
            logger.info("Loaded phases:")
            for phase in workflow_def.phases:
                logger.info(f"  Phase {phase.id}: {phase.name}")
                logger.info(f"    - Description: {phase.description[:100]}...")
                logger.info(f"    - Done definitions: {len(phase.done_definitions)} items")

        except ImportError as e:
            logger.error(f"Failed to import PhaseLoader: {e}")
            import traceback

            logger.error(traceback.format_exc())
        except Exception as e:
            logger.error(f"Failed to load phases: {e}")
            import traceback

            logger.error(f"Full traceback:\n{traceback.format_exc()}")
            # Don't fail server startup, just run without phases
    else:
        logger.info("No phases folder specified - running in standard mode")
        logger.info("To load phases, set HEPHAESTUS_PHASES_FOLDER environment variable")

    logger.info("=== END PHASE LOADING DEBUG ===")

    # Register all workflow definitions
    try:
        from src.core.database import WorkflowDefinition as DBWorkflowDefinition
        from src.workflow_registry import get_all_workflow_definitions

        all_definitions = get_all_workflow_definitions()

        with server_state.db_manager.get_session() as session:
            for defn in all_definitions:
                # Build phases_config from source
                phases_config = [_build_phase_dict(phase) for phase in defn.phases]

                workflow_config = {
                    "has_result": defn.config.has_result,
                    "result_criteria": defn.config.result_criteria,
                    "on_result_found": defn.config.on_result_found,
                    "enable_tickets": defn.config.enable_tickets,
                    "board_config": defn.config.board_config,
                }

                # Include launch_template in workflow_config if present
                if defn.launch_template:
                    from dataclasses import asdict

                    workflow_config["launch_template"] = asdict(defn.launch_template)

                # Get orchestrator_config if present
                orchestrator_config = getattr(defn, "orchestrator_config", None)

                existing = session.query(DBWorkflowDefinition).filter_by(id=defn.id).first()
                if existing:
                    # Update from source files (source of truth)
                    existing.name = defn.name
                    existing.description = defn.description
                    existing.phases_config = phases_config
                    existing.workflow_config = workflow_config
                    existing.orchestrator_config = orchestrator_config
                    logger.info(f"Updated workflow from source: {defn.id}")
                else:
                    db_def = DBWorkflowDefinition(
                        id=defn.id,
                        name=defn.name,
                        description=defn.description,
                        phases_config=phases_config,
                        workflow_config=workflow_config,
                        orchestrator_config=orchestrator_config,
                    )
                    session.add(db_def)
                    logger.info(f"Registered workflow: {defn.id}")
            # Remove stale definitions that no longer exist on disk
            loaded_ids = {d.id for d in all_definitions}
            stale = session.query(DBWorkflowDefinition).filter(DBWorkflowDefinition.id.notin_(loaded_ids)).all()
            for stale_def in stale:
                logger.info(f"Removing stale workflow definition: {stale_def.id}")
                session.delete(stale_def)

            session.commit()
        logger.info(f"Workflow registration complete: {len(all_definitions)} definitions")
    except Exception as e:
        logger.error(f"Failed to register workflows: {e}")
        import traceback

        logger.error(traceback.format_exc())

    # Start background queue processor
    logger.info("Starting background queue processor...")
    server_state.background_queue_processor_task = asyncio.create_task(background_queue_processor())
    logger.info("Background queue processor task created")

    # Start background phase advancement sweep — the generic, restart-safe
    # replacement for relying on a specific run's own polling loop (see its
    # docstring for why that's necessary).
    logger.info("Starting background phase advancement sweep...")
    server_state.phase_advancement_sweep_task = asyncio.create_task(background_phase_advancement_sweep())
    logger.info("Background phase advancement sweep task created")

    # Resume the autopilot pipeline driver itself if it was running when the
    # server last stopped. AutopilotService lives entirely in-process (see
    # src/autopilot/service.py) — its polling loop (which fires phase
    # transitions once a phase's task is marked done) dies with the process
    # and nothing else re-creates it. Without this, a backend restart while a
    # pipeline is active permanently stalls phase advancement: tasks finish,
    # but the next phase's task never gets created, until a much later,
    # cruder fallback (the diagnostic monitor's stuck-workflow detector)
    # eventually notices and manually patches the gap.
    #
    # Done BEFORE _resume_interrupted_workflows below (rather than after, as
    # this used to be ordered) so that if the persisted state says the user
    # last had this running, AutopilotService.running flips true as early as
    # possible in startup -- not after the slower interrupted-workflow scan
    # has already run. Every check elsewhere that reads "is the pipeline
    # active" (status endpoints, the frontend queue page, orphan/recovery
    # logic) should see "active" for as much of the startup window as
    # possible instead of a transient "idle" read.
    try:
        from src.autopilot.service import (
            AutopilotService,
            get_autopilot_service,
            get_registry,
        )

        # Enumerate every project with a persisted "was running" marker, not
        # just one -- multiple projects can each have their own pipeline to
        # resume now (see docs/MULTI_PROJECT_CONCURRENCY_DESIGN.md). This is
        # also the one and only call site of enumerate_persisted_states'
        # legacy-key migration, so a pipeline that was running before this
        # change deployed self-heals onto the namespaced key right here.
        for resume_project_id, persisted in AutopilotService.enumerate_persisted_states():
            if not persisted.get("project_path"):
                continue

            # Same cap POST /start enforces. Without this, a restart with
            # more persisted "was running" projects than max_concurrent_
            # projects (e.g. the cap was lowered, or that many really were
            # running when the backend went down) would silently resume all
            # of them, permanently exceeding the cap until the next manual
            # stop. try_reserve() always allows a project already counted as
            # running, so this only ever rejects the (N+1)th and later
            # resumes within this same loop, not earlier ones. Using
            # try_reserve (not can_start) here too, not just its atomicity:
            # an incoming POST /start could in principle race this loop if
            # the server starts accepting connections before startup_event
            # finishes.
            can_start, cap_message = get_registry().try_reserve(resume_project_id)
            if not can_start:
                logger.warning(f"[RESUME] Skipping auto-resume for project {resume_project_id}: {cap_message}")
                continue

            logger.info(f"[RESUME] Auto-resuming autopilot pipeline for project {resume_project_id} ({persisted['project_path']}) (was running before restart)")
            try:
                await get_autopilot_service(resume_project_id).start(
                    project_path=persisted["project_path"],
                    design_queue=persisted.get("design_queue") or "",
                    max_iterations=persisted.get("max_iterations", 10),
                )
            except Exception as e:
                logger.error(f"[RESUME] Failed to auto-resume project {resume_project_id}: {e}")
            finally:
                get_registry().release_reservation(resume_project_id)
    except Exception as e:
        logger.error(f"[RESUME] Failed to enumerate persisted autopilot state: {e}")

    # Resume any workflows that were mid-flight when the server last stopped
    # (crash / laptop sleep / manual restart), and sweep worktrees for
    # workflows that finished but never got their post-completion cleanup
    # call to run -- backgrounded (not awaited here) so the server starts
    # accepting connections immediately instead of blocking on however many
    # agents were orphaned by the last restart. _resume_interrupted_
    # workflows's own per-orphan loop already offloads the actual
    # subprocess/git work, but the loop itself is sequential and this
    # whole call used to be awaited directly in startup_event -- "blocking
    # every request until it finishes" per that loop's own comment.
    # Confirmed live: several orphaned agents across multiple workflows
    # after a long outage made port 8300 refuse/hang on every connection,
    # including a zero-I/O /health check, for minutes on every restart.
    server_state.startup_recovery_task = asyncio.create_task(_run_startup_recovery())

    logger.info("Server started successfully")


async def _run_startup_recovery() -> None:
    """Backgrounded body of startup_event's post-restart recovery -- see
    its call site's comment for why this isn't awaited inline."""
    try:
        await _resume_interrupted_workflows()
    except Exception as e:
        logger.error(f"[RESUME] resume scan failed: {e}")

    try:
        from src.autopilot.orchestrator.worktree_integration import sweep_completed_workflow_worktrees

        loop = asyncio.get_event_loop()
        swept = await loop.run_in_executor(None, sweep_completed_workflow_worktrees, logger)
        if swept:
            logger.info(f"[SWEEP] Cleaned up {swept} orphaned completed-workflow worktree(s)")
    except Exception as e:
        logger.error(f"[SWEEP] Completed-workflow worktree sweep failed: {e}")


SAFE_RESTART_GRACE_SECONDS = 10


async def _notify_agents_of_restart(project_id: str) -> int:
    """Best-effort checkpoint nudge to every working phase agent in this
    project, sent right before pausing its pipeline for a restart -- see
    docs/SAFE_RESTART_DESIGN.md §3.4.

    Not a guarantee: tmux text injection can't interrupt an agent
    synchronously blocked on its own LLM call. This only helps an agent
    that's between turns notice before its session goes quiet, and
    encourages the save_memory-as-you-go habit the system prompt already
    asks for.
    """

    agent_ids: list = []
    try:
        with server_state.db_manager.session_scope() as session:
            wf_ids = [w.id for w in session.query(Workflow).filter_by(project_id=project_id).all()]
            if not wf_ids:
                return 0
            task_ids = [t.id for t in session.query(Task).filter(Task.workflow_id.in_(wf_ids)).all()]
            agent_ids = [a.id for a in session.query(Agent).filter(Agent.status == "working", Agent.current_task_id.in_(task_ids)).all()]
    except Exception as e:
        logger.warning(f"[SAFE-RESTART] Could not enumerate agents to notify for project {project_id[:8]}: {e}")
        return 0

    notified = 0
    for agent_id in agent_ids:
        try:
            await server_state.agent_manager.send_message_to_agent(
                agent_id,
                "A backend restart is happening shortly. If you're mid-edit, "
                "finish this atomic step (don't start a new multi-file change). "
                "Call hephaestus_save_memory now with anything you don't want "
                "to lose -- your session will resume automatically afterward.",
            )
            notified += 1
        except Exception as e:
            logger.debug(f"[SAFE-RESTART] Could not notify agent {agent_id[:8]}: {e}")
    return notified


async def _notify_and_pause_for_restart(running_services: list) -> None:
    """Notify every in-flight agent across `running_services`' projects,
    give them a grace window to let any already-in-flight call land, then
    pause each service for restart.

    Extracted from shutdown_event as its own function so this specific
    notify -> wait -> pause sequence is unit-testable without exercising
    the rest of shutdown_event's unrelated steps (queue processor
    shutdown, etc).

    Notify in-flight agents first (best-effort), then pause: an agent
    mid-step benefits most from knowing a restart is coming before its
    session goes quiet, not after. pause_for_restart() (unlike stop())
    keeps the persisted "was running" marker intact, so
    _resume_interrupted_workflows still auto-resumes each project on the
    next startup.
    """
    if not running_services:
        return

    logger.info(f"[SAFE-RESTART] Pausing {len(running_services)} running autopilot pipeline(s) for restart...")
    total_notified = 0
    for service in running_services:
        try:
            notified = await _notify_agents_of_restart(service.project_id)
            total_notified += notified
            if notified:
                logger.info(f"[SAFE-RESTART] Notified {notified} agent(s) in project {service.project_id[:8]}")
        except Exception as e:
            logger.warning(f"[SAFE-RESTART] Failed to notify agents for project {service.project_id[:8]}: {e}")

    # Give notified agents a real chance to finish an atomic step before
    # this process actually stops accepting connections, instead of
    # proceeding immediately -- the notification above was previously
    # pure courtesy, with nothing waiting on it. This doesn't confirm any
    # specific agent acted on the message (tmux text injection can't
    # synchronize with a process synchronously blocked on its own LLM
    # call, see _notify_agents_of_restart's docstring), but it does keep
    # this server accepting connections for SAFE_RESTART_GRACE_SECONDS
    # longer -- long enough for an in-flight HTTP call already on the wire
    # (e.g. complete_my_task, bounded by mcp_client.py's own 10s request
    # timeout) to land before the backend actually goes down, instead of
    # racing it. Skipped entirely when nothing was notified -- the common
    # case of no agents actively working shouldn't pay this delay on
    # every restart.
    if total_notified:
        logger.info(f"[SAFE-RESTART] Waiting {SAFE_RESTART_GRACE_SECONDS}s before pausing pipelines, so in-flight agent calls have a chance to land first...")
        await asyncio.sleep(SAFE_RESTART_GRACE_SECONDS)

    await asyncio.gather(
        *(service.pause_for_restart() for service in running_services),
        return_exceptions=True,
    )


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("Shutting down Hephaestus MCP Server...")

    # Set this FIRST, before pausing pipelines below -- it's what stops
    # background_phase_advancement_sweep (server_state.shutdown_event.set()
    # further down is this same event) from starting a NEW tick, including
    # its _resync_pipeline_registry self-heal check (SAFE_RESTART_DESIGN.md
    # §3.5). Without this ordering, that sweep can keep ticking for the
    # entire pause_for_restart() drain window below (up to 45s) and race
    # it: a project mid-pause has its persisted "was running" marker still
    # intact (deliberately, for auto-resume) but its registry entry
    # momentarily not-running, which is exactly _resync_pipeline_registry's
    # own trigger condition -- it could try to restart a pipeline that's
    # still in the middle of winding down. Doesn't fully close the window
    # (a tick already in flight at this exact instant could still race),
    # narrowed further by _resync_pipeline_registry checking _should_stop()
    # itself before restarting anything (orchestrator.py).
    server_state.shutdown_event.set()

    # Pause every running project's autopilot pipeline gracefully, instead
    # of letting asyncio hard-cancel it when the event loop closes later in
    # this shutdown -- see docs/SAFE_RESTART_DESIGN.md §3.1/§3.2.
    try:
        from src.autopilot.service import get_registry

        await _notify_and_pause_for_restart(get_registry().running())
    except Exception as e:
        logger.error(f"[SAFE-RESTART] Graceful pipeline pause failed: {e}")

    # Stop background queue processor (shutdown_event already set above)
    logger.info("Stopping background queue processor...")
    if server_state.background_queue_processor_task:
        try:
            await asyncio.wait_for(server_state.background_queue_processor_task, timeout=5.0)
            logger.info("Background queue processor stopped")
        except asyncio.TimeoutError:
            logger.warning("Background queue processor did not stop gracefully, cancelling...")
            server_state.background_queue_processor_task.cancel()

    # Stop background phase advancement sweep (shares the same shutdown_event,
    # already set above)
    logger.info("Stopping background phase advancement sweep...")
    if server_state.phase_advancement_sweep_task:
        try:
            await asyncio.wait_for(server_state.phase_advancement_sweep_task, timeout=5.0)
            logger.info("Background phase advancement sweep stopped")
        except asyncio.TimeoutError:
            logger.warning("Background phase advancement sweep did not stop gracefully, cancelling...")
            server_state.phase_advancement_sweep_task.cancel()

    # Stop the one-shot startup recovery task if it's still running --
    # unlike the two loops above it doesn't watch shutdown_event (it's not
    # a loop), so a still-running one just gets cancelled outright rather
    # than waited on; _resume_interrupted_workflows's own claim guards make
    # a partial/interrupted pass safe to pick back up on the next restart.
    if server_state.startup_recovery_task and not server_state.startup_recovery_task.done():
        logger.info("Cancelling in-progress startup recovery task...")
        server_state.startup_recovery_task.cancel()

    # Close all WebSocket connections
    for ws in server_state.active_websockets:
        await ws.close()

    # Close any CDP browser sessions the devtools MCP tools opened -- left
    # open otherwise, since nothing else ever calls close_all().
    try:
        from src.mcp.devtools import devtools_manager

        await devtools_manager.close_all()
    except Exception as e:
        logger.error(f"Failed to close devtools browser sessions: {e}")
