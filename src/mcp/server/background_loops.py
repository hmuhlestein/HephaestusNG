"""Background queue processor and phase-advancement sweep.

Extracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from src.core.database import (
    Task,
    Workflow,
)
from src.mcp.server._shared import server_state

# Import routers at module level for test compatibility

logger = logging.getLogger("src.mcp.server.background_loops")

async def process_queue(project_id: Optional[str] = None):
    """Process the next queued task by creating an agent for it.

    Only creates an agent if we're under the max concurrent agent limit.

    Args:
        project_id: When given, scopes both the capacity check and the
            next-task selection to this project -- each active project
            gets its own independent max_concurrent_agents budget instead
            of competing for one global slot count, so a busier project
            can't starve a quieter one's queue. When omitted, behaves
            globally (original behavior, used by callers not yet updated
            to pass project_id, e.g. terminate_agents_and_process_queue below).
    """
    from src.services.agent_dispatch_service import AgentDispatchService
    from src.services.task_enrichment_service import TaskEnrichmentService

    # dequeue_task flips the task out of "queued" before enrichment and
    # dispatch -- everything below that point can raise. These two track
    # how far we got so the handler can put the task back (see below).
    dequeued_task_id = None
    agent = None
    # get_next_queued_task takes this reservation, but the release below used
    # to sit in a finally wrapping only the dispatch call -- enrichment and
    # context-building happen in between and can raise, leaking the slot for
    # the process's lifetime. Tracked here so every exit path releases it
    # exactly once (release_cli_model_slot decrements, so a double release
    # would hand out a slot another dispatch is holding).
    reserved_cli_model = None

    # This whole function was doing its DB work (capacity check, dequeue,
    # phase resolution, enrichment write-back, task refresh) directly on the
    # event loop -- plain synchronous SQLAlchemy (DatabaseManager uses a sync
    # create_engine, see database.py), so every call blocks whatever thread
    # runs it. Unlike the already-fixed asyncio.gather() sites (which only
    # delayed THIS function's own completion), a synchronous DB call made
    # directly in an `async def` stalls the entire event loop -- every other
    # HTTP request, WebSocket push, and SSE stream this process is serving --
    # for its duration. process_queue fires on every single task dispatch,
    # far more often than background_phase_advancement_sweep's 20s sweep,
    # which already offloads its own sync work the same way for the same
    # reason (see that function's docstring). Each purely-synchronous segment
    # below is offloaded via run_in_executor; the genuinely async calls
    # (enrich, build_dispatch_context, dispatch, broadcast_update) stay as
    # direct awaits.
    loop = asyncio.get_event_loop()

    try:
        def _dequeue_and_resolve_phase_sync():
            # Check if we should queue (i.e., at capacity)
            if server_state.queue_service.should_queue_task(project_id):
                logger.debug(f"At capacity - not processing queue (project_id={project_id})")
                return None

            # Get next task from queue
            task = server_state.queue_service.get_next_queued_task(project_id)
            if not task:
                logger.debug("No queued tasks to process")
                return None

            logger.info(f"Processing queued task {task.id} (priority={task.priority}, boosted={task.priority_boosted})")

            reservation = getattr(task, "_reserved_cli_model", None)

            # Dequeue the task
            server_state.queue_service.dequeue_task(task.id)

            # Resolve phase_id once up front — reused for both enrichment (if
            # needed) and agent dispatch below. Previously this exact
            # digit-vs-UUID resolution was independently duplicated for each
            # (see docs/SOLID_OO_REVIEW.md findings 1.2/1.3/1.4).
            phase_id = None
            if task.phase_id and server_state.phase_manager:
                phase_id = TaskEnrichmentService.resolve_phase_id(
                    phase_id_raw=task.phase_id,
                    phase_order=None,
                    workflow_id=task.workflow_id,
                    requesting_agent_id="system",
                )
                if phase_id != task.phase_id:
                    task.phase_id = phase_id  # update in-memory object too

            return task, reservation, phase_id

        dequeue_result = await loop.run_in_executor(None, _dequeue_and_resolve_phase_sync)
        if dequeue_result is None:
            return
        next_task, reserved_cli_model, resolved_phase_id = dequeue_result
        dequeued_task_id = next_task.id

        # Tasks created with placeholder "[Processing] ..." (e.g. blocked on
        # creation and enrichment was skipped) need real LLM enrichment.
        needs_enrichment = not next_task.enriched_description or next_task.enriched_description.startswith("[Processing]")
        logger.info(f"[QUEUE_ENRICHMENT] Task {next_task.id} needs_enrichment={needs_enrichment}")

        if needs_enrichment:
            phase_context_str, ctx_workflow_id = await loop.run_in_executor(
                None, TaskEnrichmentService.get_phase_context_str, resolved_phase_id
            )
            workflow_id = ctx_workflow_id or next_task.workflow_id

            enrichment_result = await TaskEnrichmentService.enrich(
                raw_description=next_task.raw_description,
                done_definition=next_task.done_definition,
                phase_context_str=phase_context_str,
                requesting_agent_id="system",
            )
            enriched_task = enrichment_result["enriched_task"]

            # FIX #7: Save enrichment context for dispatch reuse.
            next_task._enrichment_context = {
                "context_memories": enrichment_result["context_memories"],
                "project_context": enrichment_result["project_context"],
            }

            def _write_back_enrichment_sync():
                session = server_state.db_manager.get_session()
                try:
                    task = session.query(Task).filter_by(id=next_task.id).first()
                    if task:
                        task.enriched_description = enriched_task["enriched_description"]
                        task.estimated_complexity = enriched_task.get("estimated_complexity", 5)
                        if resolved_phase_id:
                            task.phase_id = resolved_phase_id
                        if workflow_id:
                            task.workflow_id = workflow_id

                        # Inherit validation from phase, if enabled there
                        if resolved_phase_id:
                            from src.core.database import Phase

                            phase = session.query(Phase).filter_by(id=resolved_phase_id).first()
                            if phase and phase.validation and phase.validation.get("enabled", True):
                                task.validation_enabled = True

                        session.commit()
                        return True
                    else:
                        logger.error(f"[QUEUE_ENRICHMENT] Task {next_task.id} not found in database!")
                        return False
                finally:
                    session.close()

            if await loop.run_in_executor(None, _write_back_enrichment_sync):
                next_task._enriched_task_dict = enriched_task  # for dispatch below
                logger.info(f"[QUEUE_ENRICHMENT] Enrichment complete for task {next_task.id}")
        else:
            logger.info(f"[QUEUE_ENRICHMENT] Task {next_task.id} already enriched - skipping enrichment pipeline")

        def _refresh_task_sync():
            # Refresh task from DB to get post-enrichment data, and build the
            # temp task object used for dispatch (mirrors create_task's pattern).
            session = server_state.db_manager.get_session()
            try:
                refreshed_task = session.query(Task).filter_by(id=next_task.id).first()
                if refreshed_task:
                    agent_task = Task(
                        id=refreshed_task.id,
                        raw_description=refreshed_task.raw_description,
                        enriched_description=refreshed_task.enriched_description,
                        done_definition=refreshed_task.done_definition,
                        phase_id=resolved_phase_id or refreshed_task.phase_id,
                        created_by_agent_id=refreshed_task.created_by_agent_id,
                        workflow_id=refreshed_task.workflow_id,
                    )
                    rag_description = refreshed_task.enriched_description or refreshed_task.raw_description
                else:
                    logger.warning("[QUEUE_AGENT_CREATE] Could not refresh task from DB - using stale task")
                    agent_task = next_task
                    rag_description = next_task.enriched_description or next_task.raw_description
                return agent_task, rag_description
            finally:
                session.close()

        task_for_agent, task_description_for_rag = await loop.run_in_executor(None, _refresh_task_sync)

        # If enrichment just ran, use the full LLM result dict; otherwise
        # (task was already enriched) build a minimal dict.
        if hasattr(next_task, "_enriched_task_dict"):
            enriched_data_for_agent = next_task._enriched_task_dict
        else:
            enriched_data_for_agent = {
                "enriched_description": task_for_agent.enriched_description,
                "estimated_complexity": task_for_agent.estimated_complexity or 5,
            }

        # FIX #7: Reuse enrichment context if available (avoid double-fetch).
        if hasattr(next_task, "_enrichment_context"):
            dispatch_context = await AgentDispatchService.build_dispatch_context_from_existing(
                memories=next_task._enrichment_context["context_memories"],
                project_context=next_task._enrichment_context["project_context"],
                working_directory="",  # Will fall back to phase cwd
                phase_id=task_for_agent.phase_id,
            )
        else:
            dispatch_context = await AgentDispatchService.build_dispatch_context(
                task_description_for_rag=task_description_for_rag,
                phase_id=task_for_agent.phase_id,
                requesting_agent_id="system",
            )

        # QueueService.get_next_queued_task set this when the phase's
        # primary cli/model combo was at its configured concurrency limit
        # (e.g. a local model's single inference slot) -- dispatch on the
        # fallback model it picked instead of the phase's own cli_tool/
        # cli_model. Only overrides those two keys; phase_glm_token_env/
        # phase_thinking_level stay as resolved from the phase.
        if hasattr(next_task, "_dispatch_cli_override"):
            override_cli_tool, override_cli_model = next_task._dispatch_cli_override
            dispatch_context["phase_cli_tool"] = override_cli_tool
            dispatch_context["phase_cli_model"] = override_cli_model

        # get_next_queued_task reserved this combo's slot (if a limit is
        # configured for it) to close the check-then-act race -- must be
        # released once this dispatch attempt finishes, success or not, or
        # the reservation permanently steals a slot from this combo.
        try:
            agent = await AgentDispatchService.dispatch(
                task=task_for_agent,
                enriched_data=enriched_data_for_agent,
                dispatch_context=dispatch_context,
            )
        finally:
            if reserved_cli_model:
                server_state.queue_service.release_cli_model_slot(*reserved_cli_model)
                reserved_cli_model = None
        logger.info(f"Created agent {agent.id} for queued task {next_task.id}")

        def _finalize_dispatch_sync():
            # Agent is now working — "in_progress" (not "assigned" like the
            # other dispatch call sites), matching original process_queue behavior.
            AgentDispatchService.mark_assigned(next_task.id, agent.id, status="in_progress")

            from src.core.database import resolve_project_for_workflow

            return resolve_project_for_workflow(task_for_agent.workflow_id)

        bcast_project_id, bcast_project_name = await loop.run_in_executor(None, _finalize_dispatch_sync)
        await server_state.broadcast_update(
            {
                "type": "task_dequeued",
                "task_id": next_task.id,
                "agent_id": agent.id,
                "description": (next_task.enriched_description or next_task.raw_description)[:200],
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

    except Exception as e:
        logger.error(f"Failed to process queue: {e}")
        import traceback

        logger.error(traceback.format_exc())

        # Failure before the dispatch call never reached the finally that
        # normally releases this.
        if reserved_cli_model:
            server_state.queue_service.release_cli_model_slot(*reserved_cli_model)
            reserved_cli_model = None

        # Compensate for the non-atomic dequeue-then-dispatch above. Without
        # this, a failure between dequeue_task and dispatch strands the task
        # in "assigned" with assigned_agent_id=None: get_next_queued_task
        # only looks at "queued", and every recovery sweep finds its task via
        # filter_by(assigned_agent_id=agent.id) (mechanical_recovery's
        # STUCK_TASK_STATUSES detectors) or requires assigned_agent_id
        # isnot(None) (_clean_stale_assigned_tasks), so nothing can ever see
        # it again -- the workflow waits on that task forever. Observed live:
        # a review task sat "assigned" with no agent while its workflow
        # stayed active for hours.
        # Skipped once dispatch succeeded: an agent is already running on the
        # task, and requeueing would launch a second one for the same work.
        if dequeued_task_id and agent is None:
            try:
                server_state.queue_service.enqueue_task(dequeued_task_id)
                logger.info(f"Requeued task {dequeued_task_id} after failed dispatch")
            except Exception as requeue_error:
                logger.error(
                    f"Failed to requeue task {dequeued_task_id} after failed "
                    f"dispatch -- it is now stranded in 'assigned' with no "
                    f"agent: {requeue_error}"
                )



# FIX #11: Register queue processor with app_context so services can
# trigger queue processing without importing server.py directly.
from src.core.app_context import set_queue_processor as _set_queue_processor  # noqa: E402

_set_queue_processor(process_queue)


async def terminate_agents_and_process_queue(
    agent_manager, agent_ids: List[str], project_id: Optional[str] = None
) -> None:
    """Terminate one or more agents, then advance the queue once.

    Consolidates the 4 near-identical "terminate + advance queue" closures
    that were duplicated across _update_task_status_steps.py/memory_api.py
    (SOLID review 1.17) -- each independently paired agent termination with
    a process_queue() call, so a future edit applied to one copy but not
    the others could silently stall the queue.
    """
    for agent_id in agent_ids:
        await agent_manager.terminate_agent(agent_id)
    await process_queue(project_id)


async def background_queue_processor():
    """Background task that processes the queue every minute.

    This ensures that queued tasks (especially newly unblocked ones)
    don't get stuck waiting for another event to trigger queue processing.
    """
    logger.info("Background queue processor started")

    while not server_state.shutdown_event.is_set():
        try:
            # Scope to every currently-active project (plural, capped at
            # max_concurrent_projects) so one project's queue depth can't
            # starve another's -- mirrors the phase-advancement sweep's own
            # is_active=True scoping fix. Falls back to a single global,
            # unscoped pass when no project is active (fresh install /
            # single-project mode), same as the sweep does.
            from src.core.database import AutopilotProject

            def _get_active_project_ids_sync():
                session = server_state.db_manager.get_session()
                try:
                    return [
                        p.id
                        for p in session.query(AutopilotProject).filter_by(is_active=True).all()
                    ]
                finally:
                    session.close()

            active_project_ids = await asyncio.get_event_loop().run_in_executor(
                None, _get_active_project_ids_sync
            )

            if not active_project_ids:
                queue_status = server_state.queue_service.get_queue_status()
                queued_count = queue_status.get("queued_tasks_count", 0)
                if queued_count > 0:
                    logger.info(f"[BACKGROUND_QUEUE] Found {queued_count} queued task(s), processing queue...")
                    await process_queue()
                else:
                    logger.debug("[BACKGROUND_QUEUE] No queued tasks, skipping")
            else:
                for proj_id in active_project_ids:
                    queue_status = server_state.queue_service.get_queue_status(proj_id)
                    queued_count = queue_status.get("queued_tasks_count", 0)
                    if queued_count > 0:
                        logger.info(
                            f"[BACKGROUND_QUEUE] Found {queued_count} queued task(s) for "
                            f"project {proj_id[:8]}, processing queue..."
                        )
                        await process_queue(proj_id)
                    else:
                        logger.debug(f"[BACKGROUND_QUEUE] No queued tasks for project {proj_id[:8]}, skipping")

        except Exception as e:
            logger.error(f"[BACKGROUND_QUEUE] Error in background queue processor: {e}")
            import traceback

            logger.error(traceback.format_exc())

        # Wait 60 seconds before next check
        try:
            await asyncio.wait_for(server_state.shutdown_event.wait(), timeout=60.0)
            # If we get here, shutdown was signaled
            break
        except asyncio.TimeoutError:
            # Timeout is expected - continue the loop
            pass

    logger.info("Background queue processor stopped")

async def background_phase_advancement_sweep():
    """Background task that re-drives phase advancement for every active
    workflow, independent of any specific run's own polling loop.

    _advance_phases (src/autopilot/orchestrator.py) is the single source of
    truth for firing phase transitions, but historically it was only ever
    called from inside run_single_workflow's own monitor loop -- a loop
    that lives and dies with that specific async call. A backend restart
    kills it, and nothing re-created it for an already-launched workflow:
    the startup resume path (_resume_interrupted_workflows) only restarts
    orphaned AGENTS, on a stale assumption ("a 'done' task advances via the
    monitor's phase-completion check") that no longer holds -- that
    responsibility moved into the orchestrator's per-workflow loop without
    the resume path being updated to compensate.

    Observed live: a workflow's task finished successfully hours before
    this fix, but its phase never advanced past it, because nothing was
    polling _advance_phases for that workflow anymore after a backend
    restart -- it sat "in_progress" indefinitely until manually kicked.

    This sweep is a generic, restart-safe safety net: every workflow with
    status active/paused gets _advance_phases called for it here, on a
    fixed interval, regardless of how it was launched or whether some
    other loop is also driving it. _advance_phases's own claim guards
    (_claim_phase_task_creation) make concurrent calls from multiple
    sources safe by construction -- this doesn't race with
    run_single_workflow's own loop when both are active for the same
    workflow, it just means the workflow is never orphaned from
    advancement again.

    The per-tick work is synchronous, blocking DB I/O (_advance_phases
    itself, and everything it calls, uses plain SQLAlchemy sessions, not
    async ones) -- run via run_in_executor rather than inline, the same way
    AutopilotService._run_pipeline offloads its own synchronous pipeline
    loop. Calling N sequential blocking DB round-trips directly inside this
    coroutine would stall the whole event loop -- every HTTP request,
    WebSocket push, and SSE stream this same process is serving -- for the
    sweep's full duration, every tick, growing with active-workflow count.
    """
    from pathlib import Path

    from src.autopilot.orchestrator import OrchestratorLogger
    from src.core.constants import AUTOPILOT_STATE_DIR

    logger.info("Background phase advancement sweep started")
    sweep_logger = OrchestratorLogger(Path(AUTOPILOT_STATE_DIR) / "phase-advancement-sweep")
    loop = asyncio.get_event_loop()

    while not server_state.shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _run_phase_advancement_sweep_once, sweep_logger, loop),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            logger.error("[PHASE-SWEEP] Tick timed out after 120s — will retry next cycle")
            sweep_logger.warning("[PHASE-SWEEP] Tick timed out after 120s — will retry next cycle")
        except Exception as e:
            logger.error(f"[PHASE-SWEEP] Error in phase advancement sweep: {e}")

        try:
            await asyncio.wait_for(server_state.shutdown_event.wait(), timeout=20.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Background phase advancement sweep stopped")

_LAST_BRANCH_HEAL_TIME: Optional[datetime] = None

_BRANCH_HEAL_INTERVAL_SECONDS = 900  # 15 minutes

def _run_phase_advancement_sweep_once(sweep_logger, loop=None) -> None:
    """Synchronous body of one background_phase_advancement_sweep tick --
    see that function's docstring for why this runs in a thread executor
    rather than inline on the event loop.

    loop: the server's persistent event loop, passed through from
    background_phase_advancement_sweep -- needed by
    _resync_pipeline_registry to schedule AutopilotService.start() back
    onto it (see that function's docstring for why asyncio.run(...) can't
    be used here). Optional/defaulted so direct test calls that don't
    exercise the pipeline-resync path don't need to fake one up.
    """
    from src.autopilot.orchestrator import _resync_pipeline_registry
    from src.autopilot.orchestrator.features import (
        _clean_stale_assigned_tasks,
        _sync_stale_design_statuses,
        _sync_stale_feature_statuses,
    )
    from src.autopilot.orchestrator.phase_transitions import (
        _maybe_resolve_arbitration,
        _retry_exhausted_paused_workflows,
        _retry_failed_tasks,
        _try_advance_phases,
    )
    from src.autopilot.orchestrator.worktree_integration import (
        _recover_abandoned_workflows_missing_worktree,
        _recover_abandoned_workflows_with_completed_phase,
        heal_orphaned_agent_branches,
    )

    # Feature-table-wide, not scoped to any one workflow -- see its own
    # docstring for why this can't just live inside _run_one_feature.
    try:
        _sync_stale_feature_statuses(sweep_logger)
    except Exception as e:
        logger.error(f"[PHASE-SWEEP] Feature-status sync error: {e}")

    # Design-table-wide, same reasoning as the feature-status sync above --
    # a design whose last feature just finished has nothing left to ever
    # call pick_next_design for it again, so its own status sticks "active"
    # without this.
    try:
        _sync_stale_design_statuses(sweep_logger)
    except Exception as e:
        logger.error(f"[PHASE-SWEEP] Design-status sync error: {e}")

    if loop is not None:
        try:
            _resync_pipeline_registry(sweep_logger, loop)
        except Exception as e:
            logger.error(f"[PHASE-SWEEP] Pipeline-registry resync error: {e}")

    # Project-wide, not scoped to any one workflow -- a stranded agent's
    # orphaned branch outlives the workflow it belonged to (that workflow is
    # typically already "completed" by the time this matters). Throttled
    # since it shells out to git per project every time it runs.
    global _LAST_BRANCH_HEAL_TIME
    now = datetime.utcnow()
    if _LAST_BRANCH_HEAL_TIME is None or (now - _LAST_BRANCH_HEAL_TIME).total_seconds() >= _BRANCH_HEAL_INTERVAL_SECONDS:
        _LAST_BRANCH_HEAL_TIME = now
        try:
            heal_orphaned_agent_branches(sweep_logger)
        except Exception as e:
            logger.error(f"[PHASE-SWEEP] Branch-healing sweep error: {e}")

    # Runs before the active/paused workflow snapshot below, so a workflow
    # this just resumed is included in this same tick's per-workflow loop
    # (_retry_failed_tasks etc.) instead of waiting a full tick.
    try:
        _recover_abandoned_workflows_missing_worktree(sweep_logger)
    except Exception as e:
        logger.error(f"[PHASE-SWEEP] Abandoned-workflow recovery error: {e}")

    try:
        _recover_abandoned_workflows_with_completed_phase(sweep_logger)
    except Exception as e:
        logger.error(f"[PHASE-SWEEP] Completed-phase workflow recovery error: {e}")

    try:
        _retry_exhausted_paused_workflows(sweep_logger)
    except Exception as e:
        logger.error(f"[PHASE-SWEEP] Paused-workflow retry error: {e}")

    session = server_state.db_manager.get_session()
    try:
        # Scope sweep to the active projects (plural, capped at
        # max_concurrent_projects) to avoid processing stale workflows
        # from OTHER, non-active projects that are constantly failing and
        # retrying, starving the ones currently in use.
        from src.core.database import AutopilotProject
        active_proj_ids = [
            p.id for p in session.query(AutopilotProject).filter_by(is_active=True).all()
        ]
        query = session.query(Workflow.id, Workflow.status).filter(Workflow.status.in_(["active", "paused"]))
        if active_proj_ids:
            query = query.filter(Workflow.project_id.in_(active_proj_ids))
        workflows = query.all()
    finally:
        session.close()

    for wf_id, wf_status in workflows:
        # Self-healing (dead-agent cleanup + failed-task retry) only while
        # the workflow is actually active, never paused -- these two used
        # to run only once, at pipeline-startup, for whichever single
        # workflow happened to be the last-tracked current_workflow_id (see
        # attempt_recovery's caller in run_continuous_pipeline). Any other
        # in-flight workflow (parallel feature runs, or one resumed outside
        # that one startup check) never got either: a task whose agent died
        # mid-work just sat "assigned"/"in_progress" forever, since nothing
        # else ever notices the agent is dead. Running both here makes it
        # universal instead of tied to one specific caller.
        #
        # _maybe_resolve_arbitration is bundled into this same "active only"
        # guard for the same reason, even though it isn't self-healing: a
        # successful resolution dispatches the next phase's task (see
        # _resolve_arbitration_outcome), which is exactly the "spawn new
        # agent work" side effect a pause is meant to prevent. If the
        # arbitration agent finishes while paused, its decision simply stays
        # unresolved (the claim it holds has no expiry) until the workflow
        # is resumed -- the very next sweep tick after that picks it up and
        # resolves it normally. Not a permanent stall, just deferred.
        # Clean stale tasks for both active AND paused workflows (this
        # loop's own query above is already scoped to status in
        # ["active", "paused"] -- a genuinely completed/failed/cancelled
        # workflow never reaches this loop at all). Paused workflows can
        # still have orphaned tasks (assigned to terminated agents) that
        # were never cleaned up while active.
        from src.core.log_context import set_log_context
        set_log_context(workflow=wf_id)
        try:
            _clean_stale_assigned_tasks(wf_id, sweep_logger)
        except Exception as e:
            logger.error(f"[PHASE-SWEEP] Stale-task cleanup error for {wf_id[:8]}: {e}")

        if wf_status == "active":
            try:
                _retry_failed_tasks(wf_id, sweep_logger)
            except Exception as e:
                logger.error(f"[PHASE-SWEEP] Failed-task retry error for {wf_id[:8]}: {e}")
            try:
                _maybe_resolve_arbitration(wf_id, sweep_logger)
            except Exception as e:
                logger.error(f"[PHASE-SWEEP] Arbitration resolve error for {wf_id[:8]}: {e}")

        try:
            # Skip workflows actively monitored by run_single_workflow as a
            # cheap early-out — its inline _advance_phases is the main
            # path; the sweep is a fallback for workflows that lost their
            # loop. _try_advance_phases (not _advance_phases) is the actual
            # correctness guarantee: it holds a per-workflow lock, so even
            # if this check races a workflow's poll loop just starting up,
            # only one of the two callers actually runs _advance_phases.
            from src.autopilot.orchestrator import _is_workflow_monitored
            if not _is_workflow_monitored(wf_id):
                _try_advance_phases(wf_id, sweep_logger)
        except Exception as e:
            logger.error(f"[PHASE-SWEEP] Error advancing workflow {wf_id[:8]}: {e}")
