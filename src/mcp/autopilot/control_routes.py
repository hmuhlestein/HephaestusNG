"""Pipeline control routes: status, start, stop, cleanup, health. — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from src.core.constants import (
    DESIGN_WORKFLOW_DEFINITION_IDS,
)

# Import authentication function from server module
from src.mcp.autopilot._shared import (
    ALLOWED_EXTENSIONS,
    PipelineStatus,
    _cached,
    _get_active_project_id,
    _get_effective_queue_dir,
    _invalidate,
    _store,
)

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/status", response_model=PipelineStatus)
async def get_pipeline_status(
    project_id: Optional[str] = None,
    project_path: Optional[str] = None,
):
    import asyncio

    from src.autopilot.service import get_autopilot_service, get_registry

    loop = asyncio.get_event_loop()

    # project_path must be part of the key too, not just project_id: the
    # self-conflict check calls this with project_id=None (global status)
    # but a real project_path, and is_self_conflict depends on it -- without
    # this, two different projects' self-conflict checks within the 2s TTL
    # could get each other's cached result (both fall into the same "status"
    # bucket since project_id is None for both).
    cache_key = f"status:{project_id}:{project_path}" if (project_id or project_path) else "status"
    cached = _cached(cache_key, ttl=2.0)
    if cached is not None:
        return cached

    # AutopilotService is now per-project (see get_registry) -- there's no
    # longer a single global service to ask when project_id isn't given, so
    # the "any project running" fallback used below relies on the DB check,
    # not service_status. When project_id IS given, ask that project's own
    # service directly instead of the DB-workaround this endpoint used
    # before per-project services existed (kept below as a belt-and-
    # suspenders check, not the primary source of truth anymore).
    running_projects_list: List[Dict[str, Any]] = []
    # Which project's DB-backed state/event rows to read below -- when no
    # project_id is given, fall back to the first running project (same
    # "genuinely no multi-project representation in this response shape"
    # convention service_status/current_design already use in that branch).
    effective_project_id = project_id
    if project_id:
        service_status = get_autopilot_service(project_id).status()
    else:
        # current_design/elapsed_seconds/error still only reflect one
        # project (the first running one) -- those genuinely have no
        # multi-project representation in this response shape. But
        # running_projects (below) reports EVERY running project, not just
        # the first, specifically so a caller hitting the concurrency cap
        # can identify and stop exactly the project(s) blocking it instead
        # of resorting to a bare stop-all call.
        running_services = get_registry().running()
        if running_services:
            effective_project_id = getattr(running_services[0], "project_id", None)
            service_status = dict(running_services[0].status())
            for extra in running_services[1:]:
                extra_status = extra.status()
                for key in ("designs_processed", "designs_succeeded", "designs_failed"):
                    service_status[key] = service_status.get(key, 0) + extra_status.get(key, 0)

            for svc in running_services:
                svc_path = svc.status().get("project_path")
                svc_name = None
                if svc_path:
                    try:
                        from src.core.database import AutopilotProject
                        from src.core.database import get_db as _get_db

                        def _lookup_svc_name_sync(svc_path=svc_path):
                            with _get_db() as _db:
                                _rp = _db.query(AutopilotProject).filter_by(base_dir=svc_path).first()
                                return _rp.name if _rp else Path(svc_path).name

                        svc_name = await loop.run_in_executor(None, _lookup_svc_name_sync)
                    except Exception:
                        svc_name = Path(svc_path).name
                running_projects_list.append(
                    {"id": getattr(svc, "project_id", None), "name": svc_name, "base_dir": svc_path}
                )
        else:
            service_status = {}

    running = service_status.get("running", False)

    # When project_id is provided, also check if THIS project has an active
    # workflow OR an active agent -- a belt-and-suspenders promotion for
    # when the service object itself missed something (e.g. it crashed but
    # an agent it spawned is still working). This must only ever promote
    # False -> True, never demote a True from service_status: the pipeline
    # loop is legitimately "running" (alive, watching the queue) between
    # designs or while idling on an empty queue, with zero active
    # workflows/agents at that instant -- demoting to False here used to
    # flip the Play button straight back to "Paused" during any such lull,
    # even though get_autopilot_service(project_id) is already correctly
    # scoped per-project (unlike when this check was first written).
    if project_id and not running:
        try:
            from src.core.database import Agent, Task, Workflow, get_db

            def _check_project_running_sync():
                with get_db() as db:
                    has_active = db.query(Workflow).filter(Workflow.project_id == project_id, Workflow.status == "active").first()
                    if has_active:
                        return True
                    # Also check: are any agents working on tasks in this
                    # project's workflows? A workflow can be "failed" while
                    # an agent is still actively working on it. Excludes
                    # "paused" workflows -- a deliberate pause must not get
                    # reported back as "still running" just because a
                    # straggler agent (e.g. one whose launch was already
                    # mid-flight when the pause hit) hasn't cleaned up yet.
                    project_wf_ids = [w.id for w in db.query(Workflow).filter(Workflow.project_id == project_id, Workflow.status != "paused").all()]
                    if project_wf_ids:
                        active_agent = (
                            db.query(Agent).join(Task, Agent.current_task_id == Task.id).filter(Task.workflow_id.in_(project_wf_ids), Agent.status.in_(["working", "starting", "idle"])).first()
                        )
                        return active_agent is not None
                    return False

            running = await loop.run_in_executor(None, _check_project_running_sync)
        except Exception:
            pass
    elif not running:
        # No project_id specified, fallback to checking any active workflow
        try:
            from src.core.database import Agent, Workflow, get_db

            def _check_any_running_sync():
                with get_db() as db:
                    # Excludes "paused" -- see the project_id-scoped check above,
                    # which deliberately does the same for the same reason: a
                    # user pause must not be reported back as "still running".
                    active_wf = db.query(Workflow).filter(Workflow.status == "active").first()
                    if active_wf:
                        active_agents = (
                            db.query(Agent)
                            .filter(
                                Agent.agent_type == "phase",
                                Agent.status.in_(["working", "idle", "starting"]),
                            )
                            .count()
                        )
                        return active_agents > 0
                    return False

            if await loop.run_in_executor(None, _check_any_running_sync):
                running = True
        except Exception:
            pass

    state = _cached(f"state:{effective_project_id}", ttl=2.0)
    if state is None:
        try:
            import asyncio

            from src.autopilot.orchestrator.state import PersistentPipelineState

            # .load() does two synchronous DB round-trips and deserializes
            # a JSON blob that grows with every design ever processed
            # (838+ processed-design hashes on the live DB) -- called
            # directly here, that blocks the single-threaded event loop on
            # every uncached poll of this endpoint, which the dashboard
            # hits every 3s (see frontend Autopilot.tsx). The 2s cache
            # above limits how often this actually runs, but doesn't make
            # each run free. Confirmed live 2026-08-19: /health -- a bare
            # dict return with zero I/O of its own -- intermittently took
            # several seconds (once the full 8s curl timeout) even after
            # offloading the two other blocking cost-recording call sites
            # found in the same investigation.
            state_obj, _processed = await loop.run_in_executor(
                None, PersistentPipelineState(project_id=effective_project_id).load
            )
            state = state_obj.to_dict()
        except Exception:
            state = {}

        state = _store(f"state:{effective_project_id}", state or {})

    # Count queue depth from DB when project_id is provided (consistent with
    # the queue panel which reads from the DB). Fall back to filesystem count.
    queue_depth = 0
    if project_id:
        from src.core.database import AutopilotDesign, get_db

        def _count_queue_depth_sync():
            with get_db() as db:
                return db.query(AutopilotDesign).filter(AutopilotDesign.project_id == project_id, AutopilotDesign.status.notin_(["completed", "failed", "skipped"])).count()

        try:
            queue_depth = await loop.run_in_executor(None, _count_queue_depth_sync)
        except Exception:
            pass
    else:
        try:
            effective_dir = _get_effective_queue_dir()
            for ext in ALLOWED_EXTENSIONS:
                queue_depth += len(list(Path(effective_dir).glob(f"*{ext}")))
        except (FileNotFoundError, RuntimeError):
            pass  # Queue dir not configured or missing — return queue_depth=0

    last_event = _cached(f"last_event:{effective_project_id}", ttl=5.0)
    if last_event is None:
        last_event = None
        if effective_project_id:
            from src.core.database import AutopilotPipelineEvent
            from src.core.database import get_db as _get_db

            def _fetch_last_event_sync():
                with _get_db() as _db:
                    row = (
                        _db.query(AutopilotPipelineEvent)
                        .filter(AutopilotPipelineEvent.project_id == effective_project_id)
                        .order_by(AutopilotPipelineEvent.created_at.desc())
                        .first()
                    )
                    if row:
                        return {
                            "timestamp": row.created_at.isoformat(),
                            "type": row.event_type,
                            **(row.data or {}),
                        }
                    return None

            try:
                last_event = await loop.run_in_executor(None, _fetch_last_event_sync)
            except Exception:
                pass
        last_event = _store(f"last_event:{effective_project_id}", last_event)

    # Count active agents
    from src.core.database import Agent
    from src.core.database import get_db as _get_db

    def _count_active_agents_sync():
        with _get_db() as _db:
            agent_query = _db.query(Agent).filter(Agent.status.in_(["working", "starting", "idle"]))
            if project_id:
                from src.core.database import Task, Workflow

                wf_ids = [wf.id for wf in _db.query(Workflow).filter_by(project_id=project_id).all()]
                task_ids = [t.id for t in _db.query(Task).filter(Task.workflow_id.in_(wf_ids)).all()]
                agent_query = agent_query.filter(Agent.current_task_id.in_(task_ids))
            return agent_query.count()

    try:
        active_agents = await loop.run_in_executor(None, _count_active_agents_sync)
    except Exception:
        active_agents = 0

    # Resolve which project the (single, global) service is actually running,
    # if any -- so the UI can tell the user what's really running instead of
    # a generic "another project" message that's just as misleading when
    # it's actually the caller's own just-started run.
    running_project_path = service_status.get("project_path")
    running_project_name = None
    if running_project_path:
        try:
            from src.core.database import AutopilotProject
            from src.core.database import get_db as _get_db

            def _lookup_running_project_name_sync():
                with _get_db() as _db:
                    _rp = _db.query(AutopilotProject).filter_by(base_dir=running_project_path).first()
                    return _rp.name if _rp else Path(running_project_path).name

            running_project_name = await loop.run_in_executor(None, _lookup_running_project_name_sync)
        except Exception:
            running_project_name = Path(running_project_path).name

    # Merge service status with file-based state
    # Derive error/reason for why the pipeline stopped
    designs_failed = service_status.get("designs_failed", 0) or state.get("designs_failed", 0)
    last_error = None
    if not running:
        service_error = service_status.get("error")
        if service_error:
            last_error = service_error
        elif last_event and last_event.get("type") == "error":
            last_error = last_event.get("message", "Unknown error")
        elif designs_failed > 0:
            last_error = f"{designs_failed} design(s) failed"

    # Runtime shown on PipelineStatusCard must be actual working time, not
    # wall-clock since the service object's last start() -- the latter
    # (service_status["elapsed_seconds"]) resets on every backend restart
    # and never pauses while the pipeline sits idle between designs.
    # active_elapsed/active_since (PipelineState.mark_working/mark_idle)
    # persist across restarts and only accumulate while a design is
    # actually being worked, so prefer them; fall back to the service's
    # raw elapsed, then the old undifferentiated snapshot, only when
    # they're genuinely unset (0 and no open stretch -- e.g. state.json
    # predates this field, or nothing has ever run).
    active_since = state.get("active_since")
    live_active_elapsed = state.get("active_elapsed", 0) or 0
    if active_since:
        live_active_elapsed += time.time() - active_since

    result = PipelineStatus(
        running=running,
        current_design=service_status.get("current_design") or state.get("current_design"),
        current_workflow_id=state.get("current_workflow_id"),
        designs_processed=service_status.get("designs_processed", 0) or state.get("designs_processed", 0),
        designs_succeeded=service_status.get("designs_succeeded", 0) or state.get("designs_succeeded", 0),
        designs_failed=designs_failed,
        total_elapsed=int(live_active_elapsed) or service_status.get("elapsed_seconds", 0) or state.get("total_elapsed", 0),
        queue_depth=queue_depth,
        last_event=last_event,
        last_error=last_error,
        active_agents=active_agents,
        running_project_path=running_project_path,
        running_project_name=running_project_name,
        # Compute self-conflict server-side using realpath to handle
        # symlink resolution (/tmp -> /private/tmp on macOS). Checks BOTH
        # the single running_project_path (correct when project_id was
        # given above -- that's this project's own service) AND membership
        # in running_projects_list (needed when project_id was omitted --
        # running_project_path there is just running_services[0]'s path,
        # arbitrary order, so a caller whose own project is running but
        # isn't index 0 would otherwise be missed entirely and get told to
        # stop itself to start itself).
        is_self_conflict=(
            project_path is not None
            and (
                (running_project_path is not None and os.path.realpath(running_project_path) == os.path.realpath(project_path))
                or any(
                    p.get("base_dir") and os.path.realpath(p["base_dir"]) == os.path.realpath(project_path)
                    for p in running_projects_list
                )
            )
        ),
        running_projects=running_projects_list,
    )

    # Populate review_mode and features_awaiting_review for the requested project
    if project_id:
        try:
            from src.core.database import AutopilotProject, Feature, Workflow
            from src.core.database import get_db as _get_db

            def _fetch_review_mode_sync():
                with _get_db() as _db:
                    _proj = _db.query(AutopilotProject).get(project_id)
                    review_mode = bool(_proj and getattr(_proj, "review_mode", False))
                    speckit_auto_scan_enabled = bool(
                        _proj and getattr(_proj, "speckit_auto_scan_enabled", False)
                    )
                    # Count features whose workflow is paused_by="review"
                    proj_wf_ids = [
                        wf.id for wf in _db.query(Workflow).filter_by(project_id=project_id).all()
                    ]
                    features_awaiting_review = 0
                    if proj_wf_ids:
                        features_awaiting_review = (
                            _db.query(Feature)
                            .join(Workflow, Feature.workflow_id == Workflow.id)
                            .filter(
                                Feature.workflow_id.in_(proj_wf_ids),
                                Workflow.paused_by == "review",
                            )
                            .count()
                        )
                    return review_mode, speckit_auto_scan_enabled, features_awaiting_review

            (
                result.review_mode,
                result.speckit_auto_scan_enabled,
                result.features_awaiting_review,
            ) = await loop.run_in_executor(None, _fetch_review_mode_sync)
        except Exception:
            pass

    return _store(cache_key, result)

@router.post("/start")
async def start_pipeline(project_path: str, design_queue: str = "", max_iterations: int = 3):
    """Start the autopilot pipeline."""
    from src.autopilot.orchestrator.state import _get_or_create_project_id
    from src.autopilot.service import get_registry

    project_id = _get_or_create_project_id(project_path)

    # Concurrency-cap check, before anything else touches the (possibly
    # already-running) service for this project -- a genuinely new project
    # over the cap should be rejected before the zombie-detection block
    # below does any mutating work (stop()) on a service we're about to
    # refuse anyway. Restarting a project already occupying a slot is
    # always allowed (try_reserve never counts that as a new slot).
    # try_reserve (not can_start) atomically reserves the slot too, closing
    # the race window between two concurrent /start calls both checking the
    # cap before either has actually started -- the reservation MUST be
    # released below once service.start() has resolved either way.
    can_start, cap_message = get_registry().try_reserve(project_id)
    if not can_start:
        raise HTTPException(409, cap_message)

    try:
        return await _start_pipeline_reserved(project_id, project_path, design_queue, max_iterations)
    finally:
        get_registry().release_reservation(project_id)

async def _start_pipeline_reserved(project_id: str, project_path: str, design_queue: str, max_iterations: int):
    """Body of start_pipeline() that runs after the concurrency-cap slot for
    project_id has been reserved -- split out so the reservation can be
    released in a finally regardless of which of the several early-return/
    raise paths below is taken."""
    from src.autopilot.service import get_autopilot_service

    service = get_autopilot_service(project_id)
    # Give a freshly-(re)started pipeline time to actually reach its first
    # workflow check before second-guessing it. Without this, a zombie
    # verdict landing seconds after start cancels run_continuous_pipeline's
    # task -- which resets its in-memory recovery-attempt counter -- before
    # it ever gets a chance to hand off to the per-feature resume path.
    # Observed live: zombie-detected and stopped 8s after auto-resume,
    # trapping a genuinely in-progress workflow in a stop/restart loop that
    # could never escalate past its own recovery counter.
    zombie_check_grace_seconds = 45
    time_since_start = time.time() - service._start_time if service._start_time else None
    if service.running and (time_since_start is None or time_since_start >= zombie_check_grace_seconds):
        # Check for zombie state: service says running but no active agents/workflows.
        # This happens when the pipeline task gets stuck. Auto-stop and restart.
        # BUT: if the queue is legitimately empty (all designs done), the pipeline
        # is correctly idle — not a zombie.
        # Scoped to THIS project's own workflows/agents/designs -- a busy
        # OTHER project must never mask (or falsely trigger) this check.
        try:
            from src.core.database import Agent, AutopilotDesign, Task, Workflow, get_db

            with get_db() as db:
                project_wf_ids = [w.id for w in db.query(Workflow).filter(Workflow.project_id == project_id).all()]
                active_agents = (
                    db.query(Agent)
                    .join(Task, Agent.current_task_id == Task.id)
                    .filter(
                        Task.workflow_id.in_(project_wf_ids),
                        Agent.status.in_(["working", "starting", "idle"]),
                    )
                    .count()
                    if project_wf_ids
                    else 0
                )
                active_wfs = (
                    db.query(Workflow)
                    .filter(
                        Workflow.project_id == project_id,
                        Workflow.status == "active",
                    )
                    .count()
                )

                # Only zombie-detect if there are pending designs that
                # should be getting processed. Empty queue = legitimate idle.
                pending_designs = (
                    db.query(AutopilotDesign)
                    .filter(
                        AutopilotDesign.project_id == project_id,
                        AutopilotDesign.status.in_(["pending", "active"]),
                    )
                    .count()
                )

            if active_agents == 0 and active_wfs == 0 and pending_designs > 0:
                logger.warning(f"[START] Zombie pipeline detected (running=True but {pending_designs} pending/active designs and no agents/workflows) — auto-stopping")
                await service.stop()
            elif active_agents == 0 and active_wfs == 0 and pending_designs == 0:
                logger.info("[START] Pipeline is running but all designs are done — stopping cleanly and restarting")
                await service.stop()
            else:
                raise HTTPException(409, "Pipeline is already running.")
        except HTTPException:
            raise
        except Exception as e:
            # A transient failure of the zombie-detection query itself
            # (e.g. "database is locked") previously fell through to
            # `await service.stop()` -- treating "we couldn't check" the
            # same as "confirmed zombie" and unconditionally stopping a
            # pipeline that, per service.running, is otherwise believed
            # healthy and actively running. Fail conservatively instead,
            # matching the non-zombie branch above (`else: raise
            # HTTPException(409, ...)`): when the check can't run, assume
            # the pipeline is legitimately busy, not that it's safe to
            # kill. The caller can retry the start request later, and a
            # genuine zombie gets caught on a subsequent attempt once the
            # check succeeds.
            logger.error(f"[START] Zombie check failed, treating pipeline as running: {e}")
            raise HTTPException(
                409, "Pipeline is already running (zombie check failed; try again shortly)."
            )

    try:
        result = await service.start(
            project_path=project_path,
            design_queue=design_queue,
            max_iterations=max_iterations,
        )
        _invalidate("status")
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        logger.error(f"Failed to start pipeline: {e}")
        raise HTTPException(500, str(e))

@router.post("/stop")
async def stop_pipeline(clear_state: bool = False, project_id: Optional[str] = None):
    """Stop the autopilot pipeline and all its agents.

    Args:
        clear_state: If True, clear persistent pipeline state (fresh start next time)
        project_id: If provided, only stop workflows for this project
    """
    from src.autopilot.service import get_autopilot_service, get_registry
    from src.core.database import AutopilotProject, get_db

    # Stop the service(s) (this stops the pipeline task). With project_id,
    # stop just that project's service; without one, preserve the old
    # "stop whatever's running" behavior by stopping every running service
    # (there's no longer a single global service to fall back to).
    # stopped_project_ids feeds the clear_state block below -- it must be
    # captured here, not re-derived from get_registry().running() after the
    # fact, since every service in it is no longer "running" once stopped.
    if project_id:
        result = await get_autopilot_service(project_id).stop()
        stopped_project_ids = [project_id]
    else:
        stopped_any = False
        stopped_project_ids = []
        aggregate = {"designs_processed": 0, "designs_succeeded": 0, "designs_failed": 0}
        for running_service in get_registry().running():
            r = await running_service.stop()
            stopped_any = True
            stopped_project_ids.append(running_service.project_id)
            for key in aggregate:
                aggregate[key] += r.get(key, 0)
        result = {"stopped": stopped_any, **aggregate} if stopped_any else {"stopped": True, "message": "Pipeline was not running"}

    # Terminate autopilot agents and pause workflows
    # Uses shared pause_project_workflows which includes Phase 0 workflows
    # (definition_id in ["autopilot", "autopilot-phase0"]).
    terminated_count = 0
    try:
        from src.autopilot.orchestrator.engine_client import pause_project_workflows

        all_queued_task_ids = []
        with get_db() as db:
            for pid in stopped_project_ids:
                paused, queued_task_ids = pause_project_workflows(db, pid, paused_by="user")
                terminated_count += paused
                all_queued_task_ids.extend(queued_task_ids)
                # Deactivate the project so UI no longer shows it as Active
                proj = db.query(AutopilotProject).filter_by(id=pid).first()
                if proj:
                    proj.is_active = False
            db.commit()

        # Each call opens its own locked session -- done after the commit
        # above so it isn't racing this session's own open transaction
        # (same ordering as cancel_workflow/stop_workflow/pause_feature;
        # see pause_project_workflows' docstring for why this can't happen
        # inside that function itself).
        from src.core.app_context import get_app_state

        queue_service = get_app_state().queue_service
        for queued_task_id in all_queued_task_ids:
            queue_service.reset_queued_task_to_pending(queued_task_id)
    except Exception as e:
        logger.error(f"Error cleaning up autopilot agents: {e}")

    # Clear persistent state if requested -- scoped to whichever project(s)
    # this call actually stopped, not the old bare global key, so stopping
    # project A can't wipe project B's still-running pipeline state.
    if clear_state:
        from src.autopilot.orchestrator.state import PersistentPipelineState

        for stopped_project_id in stopped_project_ids:
            PersistentPipelineState(project_id=stopped_project_id).clear()
        logger.info(f"Cleared persistent pipeline state for {stopped_project_ids}")

    _invalidate("status")
    return {
        "stopped": True,
        "agents_terminated": terminated_count,
        "state_cleared": clear_state,
        **result,
    }

@router.post("/cleanup-branches")
async def cleanup_branches(project_path: Optional[str] = None):
    """Clean up all stale agent branches.

    project_path: an explicit single repo path to sweep, taken as-is. Omit
    it to sweep every repo of the active project -- WorktreeManager
    otherwise operates on whatever project happens to be
    config.main_repo_path's current global default, which is wrong as soon
    as more than one project exists (same bug already fixed for the other
    WorktreeManager(...) call sites -- see orchestrator.py), and a single
    base_dir path misses a multi-repo project's non-primary repos entirely
    (REQ-19/REQ-20 -- see resolve_repo_path's own "single choke point"
    docstring; sweeping is a some-repos-not-others gap, not a wrong-repo
    one, since each repo's stale branches just sit unswept rather than
    being swept in the wrong place).
    """
    from src.core.app_context import get_app_state
    from src.core.database import AutopilotProject, get_db
    from src.core.repo_resolution import get_project_repos
    from src.core.worktree_manager import WorktreeManager

    try:
        if project_path:
            repo_paths = [project_path]
        else:
            with get_db() as db:
                active_id = _get_active_project_id()
                proj = (
                    db.query(AutopilotProject).filter_by(id=active_id).first()
                    if active_id
                    else None
                )
                if not proj:
                    raise HTTPException(
                        400,
                        "project_path is required (no active project to default to)",
                    )
                repos = get_project_repos(db, proj.id)
                repo_paths = [repo.path for repo in repos] if repos else [proj.base_dir]
                repo_paths = [p for p in repo_paths if p]

        # A fresh WorktreeManager instance is deliberate here (not the
        # shared server_state.branch_manager) -- .reload(project_path) below
        # points it at an arbitrary project, and reload()ing the shared
        # long-lived instance would race with any other concurrent request
        # relying on it pointing at a different project. Only db_manager
        # itself should be the shared instance (see SOLID review 1.12).
        db_manager = get_app_state().db_manager
        # cleanup_all_stale_branches does real git/filesystem work --
        # blocking, same class of issue as the /health endpoint below
        # (its own comment explains the same offload-at-the-caller
        # pattern). queue_routes.py's rerun flow backgrounds this same
        # call in a fire-and-forget thread, but that path doesn't need
        # the result back; this endpoint's whole contract is returning
        # it to the caller, so it needs an awaited executor call instead.
        import asyncio

        loop = asyncio.get_event_loop()
        totals = {"cleaned": 0, "merged": 0, "failed": 0, "worktrees_cleaned": 0, "branches": [], "repos_swept": []}
        for repo_path in repo_paths:
            branch_manager = WorktreeManager(db_manager, repo_path=repo_path)
            result = await loop.run_in_executor(None, branch_manager.cleanup_all_stale_branches)
            totals["cleaned"] += result.get("cleaned", 0)
            totals["merged"] += result.get("merged", 0)
            totals["failed"] += result.get("failed", 0)
            totals["worktrees_cleaned"] += result.get("worktrees_cleaned", 0)
            totals["branches"].extend(result.get("branches", []))
            totals["repos_swept"].append({"path": repo_path, **result})
        return totals
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cleanup branches: {e}")
        raise HTTPException(500, str(e))

@router.get("/health")
async def get_system_health():
    """Get system health audit results."""
    import asyncio

    loop = asyncio.get_event_loop()
    # run_health_audit stays sync -- it's shared with the Monitor's own
    # background-thread call path (health_audit.py). Offload here, at the
    # async caller, instead of making the shared function itself async.
    return await loop.run_in_executor(None, run_health_audit)

def run_health_audit(db_manager=None):
    """Shared health audit logic used by both Monitor and API endpoint.

    Returns:
        dict with 'findings', 'workflows', 'summary' keys
    """
    from src.core.app_context import get_app_state
    from src.core.database import Agent, Task, Workflow, get_db

    if db_manager is None:
        db_manager = get_app_state().db_manager

    findings = []

    # 1. Orphaned processes
    try:
        result = subprocess.run(
            ["pgrep", "-la", "opencode|claude|pi"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            pids = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split()
                    if len(parts) >= 1:
                        pids.append(parts[0])

            tmux_result = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            tmux_pids = set()
            if tmux_result.returncode == 0:
                for line in tmux_result.stdout.strip().split("\n"):
                    if line:
                        parts = line.split()
                        if len(parts) >= 1:
                            tmux_pids.add(parts[0])

            orphaned = [p for p in pids if p not in tmux_pids]
            if orphaned:
                findings.append(
                    {
                        "type": "orphaned_processes",
                        "severity": "warning",
                        "message": f"{len(orphaned)} orphaned process(es) not in tmux",
                        "pids": orphaned[:10],
                        "action": f"kill -9 {' '.join(orphaned[:5])}",
                    }
                )
    except Exception:
        pass

    # 2. Unmerged branches
    try:
        # Check every active project, not just one -- under the
        # concurrent-active-projects model (max_concurrent_projects), more
        # than one AutopilotProject.is_active row can be True at once, and
        # a .first() here silently skipped unmerged-branch findings for
        # every active project except whichever one the query happened to
        # return.
        with get_db() as _db:
            from src.core.database import AutopilotProject

            active_projects = (
                _db.query(AutopilotProject).filter_by(is_active=True).all()
            )
            project_paths = [p.base_dir for p in active_projects if p.base_dir]
        if not project_paths:
            fallback_path = os.getenv("PROJECT_PATH")
            if fallback_path:
                project_paths = [fallback_path]

        for project_path in project_paths:
            result = subprocess.run(
                ["git", "branch", "--list", "agent-*"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=project_path,
            )
            if result.returncode == 0:
                branches = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n") if b.strip()]
                if branches:
                    findings.append(
                        {
                            "type": "unmerged_branches",
                            "severity": "info",
                            "message": f"{len(branches)} unmerged agent branch(es)",
                            "branches": branches[:10],
                            "action": "heph cleanup branches",
                            "project_path": project_path,
                        }
                    )
    except Exception:
        pass

    # 3. Workflow progress + stuck/failed
    workflows_summary = []
    session = db_manager.get_session()
    try:
        autopilot_wfs = (
            session.query(Workflow)
            .filter(
                Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                Workflow.status.in_(["active", "paused"]),
            )
            .all()
        )

        for wf in autopilot_wfs:
            design_name = "unknown"
            if wf.launch_params:
                try:
                    params = json.loads(wf.launch_params) if isinstance(wf.launch_params, str) else wf.launch_params
                    doc = params.get("design_document", "")
                    design_name = Path(doc).stem.replace("_", " ").replace("-", " ") if doc else "unknown"
                except Exception:
                    pass

            tasks = session.query(Task).filter(Task.workflow_id == wf.id).all()
            status_counts = {}
            for t in tasks:
                status_counts[t.status] = status_counts.get(t.status, 0) + 1

            total = len(tasks)
            done = status_counts.get("done", 0)
            failed = status_counts.get("failed", 0)
            in_progress = status_counts.get("in_progress", 0)
            pending = status_counts.get("pending", 0) + status_counts.get("queued", 0)

            progress = {
                "design": design_name,
                "workflow_id": wf.id[:8],
                "status": wf.status,
                "total_tasks": total,
                "done": done,
                "failed": failed,
                "in_progress": in_progress,
                "pending": pending,
                "progress_pct": round(done / total * 100) if total > 0 else 0,
            }

            if in_progress == 0 and pending > 0 and done < total and wf.status == "active":
                progress["stuck"] = True
                findings.append(
                    {
                        "type": "stuck_design",
                        "severity": "warning",
                        "message": f"Design '{design_name}' stuck: {pending} pending, 0 active",
                        "workflow_id": wf.id[:8],
                        "action": "Relaunch agents or pause workflow",
                    }
                )

            for t in tasks:
                if t.status == "failed":
                    findings.append(
                        {
                            "type": "failed_task",
                            "severity": "error",
                            "message": f"Failed in '{design_name}': {(t.enriched_description or t.raw_description or '')[:80]}",
                            "task_id": t.id[:8],
                            "action": "Review and rerun",
                        }
                    )

            workflows_summary.append(progress)
    finally:
        session.close()

    # 4. Active agents
    try:
        with get_db() as db:
            active = db.query(Agent).filter(Agent.status.in_(["working", "starting", "idle"])).count()
            terminated = db.query(Agent).filter(Agent.status == "terminated").count()
    except Exception:
        active = 0
        terminated = 0

    return {
        "findings": findings,
        "workflows": workflows_summary,
        "active_agents": active,
        "terminated_agents": terminated,
        "summary": {
            "total_findings": len(findings),
            "errors": len([f for f in findings if f["severity"] == "error"]),
            "warnings": len([f for f in findings if f["severity"] == "warning"]),
            "info": len([f for f in findings if f["severity"] == "info"]),
        },
    }
