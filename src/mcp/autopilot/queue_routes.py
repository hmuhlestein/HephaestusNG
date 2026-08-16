"""Design-queue routes: listing, reorder, requeue, rerun, repair, add/remove. — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.core.constants import (
    AUTOPILOT_STATE_DIR,
    CONTEXT_DIR_NAME,
    DESIGN_CONTEXT_SUBDIR,
    DESIGN_WORKFLOW_DEFINITION_IDS,
)

# Import authentication function from server module
from src.prompts.loader import get_prompt

from src.mcp.autopilot._shared import ALLOWED_EXTENSIONS, DesignQueueAdd, DesignQueueItem, _cached, _get_effective_queue_dir, _invalidate, _safe_path, _store

logger = logging.getLogger(__name__)

router = APIRouter()

def _get_queue_order_path(project_id: Optional[str] = None) -> Optional[Path]:
    try:
        # Write alongside other server state under .hephaestus/, not inside
        # the tracked docs/design/ directory (which would pollute git status).
        effective_dir = _get_effective_queue_dir(project_id)
        hephaestus_dir = Path(effective_dir).parent.parent / CONTEXT_DIR_NAME
        hephaestus_dir.mkdir(parents=True, exist_ok=True)
        return hephaestus_dir / ".queue_order.json"
    except (FileNotFoundError, RuntimeError):
        return None

def _load_queue_order(project_id: Optional[str] = None) -> List[str]:
    path = _get_queue_order_path(project_id)
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return []

def _save_queue_order(order: List[str], project_id: Optional[str] = None):
    path = _get_queue_order_path(project_id)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(order))

@router.get("/queue", response_model=List[DesignQueueItem])
async def list_design_queue(project_id: Optional[str] = None):
    cache_key = f"queue:{project_id}" if project_id else "queue"
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    try:
        effective_dir = _get_effective_queue_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))

    queue_path = Path(effective_dir)
    saved_order = _load_queue_order(project_id)

    files_by_name: Dict[str, Path] = {}
    for ext in ALLOWED_EXTENSIONS:
        for f in queue_path.glob(f"*{ext}"):
            files_by_name[f.name] = f

    ordered_names = [n for n in saved_order if n in files_by_name]
    unordered = [n for n in files_by_name if n not in saved_order]
    all_names = ordered_names + sorted(unordered, key=lambda n: files_by_name[n].stat().st_mtime)

    items = []
    for fname in all_names:
        f = files_by_name[fname]
        stat = f.stat()
        name = f.stem.replace("_", " ").replace("-", " ").title()
        items.append(
            DesignQueueItem(
                filename=f.name,
                name=name,
                size_bytes=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                extension=f.suffix,
            )
        )

    return _store(cache_key, items)

class QueueReorderRequest(BaseModel):
    filenames: List[str]
    project_id: Optional[str] = None

@router.post("/queue/reorder")
async def reorder_queue(req: QueueReorderRequest):
    try:
        effective_dir = _get_effective_queue_dir(req.project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))

    queue_path = Path(effective_dir)
    existing = set()
    for ext in ALLOWED_EXTENSIONS:
        for f in queue_path.glob(f"*{ext}"):
            existing.add(f.name)

    for fname in req.filenames:
        if fname not in existing:
            raise HTTPException(400, f"Unknown file: {fname}")

    _save_queue_order(req.filenames, req.project_id)
    _invalidate("queue", f"queue:{req.project_id}" if req.project_id else "queue")
    return {"order": req.filenames}

@router.post("/queue/requeue")
async def requeue_design(request: dict):
    """Move a design to the front of the queue and pause its active workflow."""
    from src.core.database import Agent, Task, Workflow, get_db

    filename = request.get("filename")
    if not filename:
        raise HTTPException(400, "filename is required")
    req_project_id = request.get("project_id")

    # Get the queue order
    order = _load_queue_order(req_project_id)

    # Move to front
    if filename in order:
        order.remove(filename)
    order.insert(0, filename)
    _save_queue_order(order, req_project_id)
    _invalidate("queue", f"queue:{req_project_id}" if req_project_id else "queue")

    # Pause any active workflow processing this design
    paused_count = 0
    try:
        with get_db() as db:
            # Find autopilot workflows that are active
            active_workflows = (
                db.query(Workflow)
                .filter(
                    Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                    Workflow.status.in_(["active", "running"]),
                )
                .all()
            )

            for wf in active_workflows:
                if wf.launch_params:
                    params = json.loads(wf.launch_params) if isinstance(wf.launch_params, str) else wf.launch_params
                    design_doc = params.get("design_document", "")
                    if filename in str(design_doc):
                        # Terminate agents for this workflow
                        task_ids = [
                            t.id
                            for t in db.query(Task)
                            .filter(
                                Task.workflow_id == wf.id,
                                Task.status.in_(["pending", "queued", "assigned", "in_progress"]),
                            )
                            .all()
                        ]

                        if task_ids:
                            agents = (
                                db.query(Agent)
                                .filter(
                                    Agent.current_task_id.in_(task_ids),
                                    Agent.status.in_(["working", "starting", "idle"]),
                                )
                                .all()
                            )
                            for agent in agents:
                                agent.status = "terminated"
                                agent.current_task_id = None  # Clear stale reference
                                agent.terminated_at = datetime.utcnow()

                            # Reset the tasks those agents were working on --
                            # without this, a task left "assigned"/"in_
                            # progress" pointing at a now-terminated agent is
                            # indistinguishable from one whose agent is still
                            # genuinely working, until an unrelated periodic
                            # sweep (attempt_recovery's stale-assigned-task
                            # cleanup) eventually notices the mismatch and
                            # fails it with a generic "terminated
                            # unexpectedly" reason instead of resetting it
                            # for a clean retry once this workflow resumes.
                            for t in db.query(Task).filter(Task.id.in_(task_ids)).all():
                                t.status = "pending"
                                t.assigned_agent_id = None

                        # Pause the workflow
                        wf.status = "paused"
                        paused_count += 1

            db.commit()
    except Exception as e:
        logger.error(f"Error pausing workflows for requeue: {e}")

    _invalidate("status")

    return {
        "requeued": True,
        "filename": filename,
        "paused_workflows": paused_count,
    }

@router.post("/queue/rerun")
async def rerun_design(request: dict):
    """Rerun a design: stop everything, move to front, start pipeline."""
    import signal
    import time
    from pathlib import Path

    from src.core.database import (
        Agent,
        AutopilotDesign,
        AutopilotProject,
        Feature,
        Task,
        Workflow,
        get_db,
    )

    filename = request.get("filename")
    if not filename:
        raise HTTPException(400, "filename is required")

    project_path = request.get("project_path")
    if not project_path:
        raise HTTPException(400, "project_path is required")

    # Validate project path exists
    project = Path(project_path).resolve()
    if not project.exists():
        raise HTTPException(400, f"Project path does not exist: {project_path}")

    # Resolved once and reused for every project-scoped step below (queue
    # order, pipeline state clearing, pipeline start) -- must all scope to
    # the SAME project, not independently-resolved ids that could diverge
    # once more than one project can be active at once.
    from src.autopilot.orchestrator.state import _get_or_create_project_id

    rerun_start_project_id = _get_or_create_project_id(str(project))

    # Validate design exists in queue
    queue_dir = project / DESIGN_CONTEXT_SUBDIR
    queue_dir.mkdir(parents=True, exist_ok=True)
    design_path = queue_dir / filename
    if not design_path.exists():
        raise HTTPException(404, f"Design not found in queue: {filename}")

    # Step 1: Stop the pipeline if running. Uses the in-process AutopilotService
    # (the same one the play/pause button drives) instead of spawning/killing a
    # separate `python -m src.autopilot.orchestrator` subprocess -- that older
    # subprocess path could run concurrently with the in-process service (both
    # calling run_phase0 independently), and was the root cause of design docs
    # ending up copied twice. See docs/MULTI_PROJECT_CONCURRENCY_DESIGN.md and
    # src/autopilot/service.py's module docstring for why the in-process
    # service replaced the subprocess approach in the first place.
    try:
        from src.autopilot.orchestrator.state import _resolve_project_id
        from src.autopilot.service import get_autopilot_service

        rerun_project_id = _resolve_project_id(str(project))
        if rerun_project_id:
            service = get_autopilot_service(rerun_project_id)
            if service.running:
                await service.stop()
    except Exception as e:
        logger.error(f"Error stopping in-process pipeline for rerun: {e}")

    # Defensive cleanup: kill any stray subprocess left over from before this
    # endpoint stopped spawning one (a currently-running old-style process
    # started by a previous backend version). Harmless no-op once nothing
    # writes orchestrator.pid anymore.
    try:
        pid_dir = Path(AUTOPILOT_STATE_DIR)
        pid_file = pid_dir / "orchestrator.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        os.kill(pid, 0)  # Check if alive
                    except ProcessLookupError:
                        break
                try:
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.5)  # Give OS time to clean up
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            pid_file.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Error killing stray orchestrator subprocess: {e}")

    # Step 2: Stop this design's active workflows and agents (Phase 0 AND
    # any feature pipelines already spawned from it -- a fresh
    # decomposition can produce different features, so anything still
    # running under the old one must stop before Step 2b wipes it).
    #
    # SCOPED TO THIS DESIGN ONLY. A prior version of this queried Agent/
    # Workflow with no filter at all -- db.query(Agent).filter(Agent.
    # status.in_(["working", "starting", "idle"])) with nothing narrowing
    # it to this project or design -- terminating every active agent and
    # pausing every active workflow SYSTEM-WIDE, across every other
    # project and design, every time anyone reran any one design. It also
    # never touched the Task rows those agents were working on, only the
    # Agent row -- a Task left "assigned"/"in_progress" pointing at an
    # agent now marked terminated is indistinguishable from one whose
    # agent is still genuinely working, until an unrelated periodic sweep
    # (attempt_recovery's stale-assigned-task cleanup) eventually noticed
    # the mismatch and failed the task with a generic "terminated
    # unexpectedly" reason. Observed live: rerunning one stuck design's
    # Phase 0 silently killed a healthy, unrelated feature's adversarial_
    # review agent mid-review -- it had already written a complete,
    # correct report and just hadn't reported completion yet -- and the
    # feature's workflow burned through its entire retry budget and
    # failed with no visible cause.
    try:
        with get_db() as db:
            proj_for_scope = db.query(AutopilotProject).filter_by(base_dir=str(project)).first()
            design_for_scope = (
                db.query(AutopilotDesign).filter_by(project_id=proj_for_scope.id, filename=filename).first()
                if proj_for_scope else None
            )
            design_wf_ids = (
                [
                    wf.id for wf in db.query(Workflow.id).filter(Workflow.design_id == design_for_scope.id).all()
                ]
                if design_for_scope else []
            )

            if design_wf_ids:
                # Reset tasks before touching their agents -- same ordering
                # used elsewhere to close this exact race (see monitor.py's
                # _auto_restart_agent).
                stuck_tasks = (
                    db.query(Task)
                    .filter(Task.workflow_id.in_(design_wf_ids), Task.status.in_(["assigned", "in_progress"]))
                    .all()
                )
                stuck_agent_ids = [t.assigned_agent_id for t in stuck_tasks if t.assigned_agent_id]
                for t in stuck_tasks:
                    t.status = "pending"
                    t.assigned_agent_id = None
                    t.failure_reason = None

                if stuck_agent_ids:
                    for agent in db.query(Agent).filter(Agent.id.in_(stuck_agent_ids)).all():
                        agent.status = "terminated"
                        agent.current_task_id = None
                        agent.terminated_at = datetime.utcnow()

                for wf in db.query(Workflow).filter(Workflow.id.in_(design_wf_ids), Workflow.status.in_(["active", "running"])).all():
                    wf.status = "paused"

            db.commit()
    except Exception as e:
        logger.error(f"Error stopping workflows for rerun: {e}")

    # Step 2b: Clean slate for this design's workflows, tasks, and features.
    # Rerun means "start fresh" — delete old rows so the orchestrator
    # doesn't see stale Feature rows and skip re-decomposition.
    try:
        from src.core.database import (
            AgentResult,
            BoardConfig,
            CostEntry,
            DiagnosticRun,
            Memory,
            Phase,
            PhaseExecution,
            TaskPromptOverride,
            Ticket,
            ValidationReview,
            WorkflowResult,
        )

        worktrees_to_clean: List[Tuple[str, dict]] = []
        with get_db() as db:
            matching_wfs = (
                db.query(Workflow)
                .filter(
                    Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                    Workflow.launch_params.like(f"%{filename}%"),
                )
                .all()
            )
            wf_ids = [wf.id for wf in matching_wfs]

            # Get design to find features
            proj = db.query(AutopilotProject).filter_by(base_dir=str(project)).first()
            design = db.query(AutopilotDesign).filter_by(project_id=proj.id, filename=filename).first() if proj else None

            if wf_ids:
                # Get task IDs for dependent record cleanup
                task_ids = [t.id for t in db.query(Task).filter(Task.workflow_id.in_(wf_ids)).all()]

                # Delete dependent records (order matters for FK constraints)
                if task_ids:
                    db.query(TaskPromptOverride).filter(TaskPromptOverride.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(ValidationReview).filter(ValidationReview.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(AgentResult).filter(AgentResult.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Memory).filter(Memory.related_task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Ticket).filter(Ticket.task_id.in_(task_ids)).delete(synchronize_session=False)
                    # CostEntry.task_id/workflow_id are also enforced FKs -- a
                    # workflow that ever recorded real LLM cost (the common
                    # case now that cost tracking exists) would otherwise
                    # fail this delete with an IntegrityError.
                    db.query(CostEntry).filter(CostEntry.task_id.in_(task_ids)).delete(synchronize_session=False)

                # Delete workflow-level dependents
                db.query(DiagnosticRun).filter(DiagnosticRun.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(WorkflowResult).filter(WorkflowResult.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(BoardConfig).filter(BoardConfig.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(Ticket).filter(Ticket.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(CostEntry).filter(CostEntry.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Collect worktree info before the Workflow rows are gone.
                # Without this, _create_integration_worktree's deterministic
                # per-design path (design_id-derived, unchanged by rerun)
                # finds the OLD worktree still sitting there and reuses it
                # as-is (it only creates fresh `if not wt_path.exists()`) --
                # "rerun" would silently continue from stale commits instead
                # of actually starting over. Step 2 above already terminated
                # every active agent and paused every active workflow, so
                # nothing is still writing to these worktrees by this point.
                for wf in db.query(Workflow).filter(Workflow.id.in_(wf_ids)).all():
                    if wf.working_directory and ".worktrees/" in wf.working_directory:
                        lp = wf.launch_params if isinstance(wf.launch_params, dict) else {}
                        worktrees_to_clean.append((wf.working_directory, lp))

                # Delete tasks -- must happen before Phase/PhaseExecution
                # below: Task.phase_id is a FK to phases.id, so deleting
                # Phase rows first (as an earlier version of this fix did)
                # fails with the same FOREIGN KEY error, just one table over.
                db.query(Task).filter(Task.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete phase executions -- PhaseExecution links to a
                # workflow via phase_id -> Phase.workflow_id, not the
                # workflow_execution_id column (an unused legacy field
                # that's never actually populated with a workflow id, so
                # filtering on it matched zero rows and left every
                # PhaseExecution -- and the Phase rows below -- behind).
                phase_ids = [p.id for p in db.query(Phase.id).filter(Phase.workflow_id.in_(wf_ids)).all()]
                if phase_ids:
                    db.query(PhaseExecution).filter(PhaseExecution.phase_id.in_(phase_ids)).delete(synchronize_session=False)

                # Delete phases -- Phase.workflow_id is a NOT NULL FK to
                # workflows.id, so leaving these behind (as this always
                # did) made the Workflow delete below fail with a
                # FOREIGN KEY constraint error every time.
                db.query(Phase).filter(Phase.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete workflows
                db.query(Workflow).filter(Workflow.id.in_(wf_ids)).delete(synchronize_session=False)

            # Delete features for this design
            if design:
                db.query(Feature).filter_by(design_id=design.id).delete(synchronize_session=False)
                # Reset design status so orchestrator picks it up fresh
                design.status = "pending"
                # Clear retry counter so fresh retry starts at 0
                from src.autopilot.orchestrator.state import _delete_project_context

                _delete_project_context(db, f"autopilot_retry_{design.id}")

            db.commit()
            logger.info(f"[RERUN] Cleaned up {len(wf_ids)} workflows and features for {filename}")

        # Best-effort worktree cleanup, now that the DB transaction above
        # has committed -- not fatal if any single one can't be resolved.
        for working_directory, launch_params in worktrees_to_clean:
            try:
                wt_path = Path(working_directory)
                if not (wt_path / ".git").exists():
                    continue
                project_path_str = launch_params.get("project_path")
                if not project_path_str:
                    logger.warning(
                        f"[RERUN] {wt_path} has no launch_params.project_path "
                        "to scope cleanup to -- left in place"
                    )
                    continue
                import git as _git

                from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

                try:
                    branch = _git.Repo(wt_path).active_branch.name
                except Exception:
                    branch = ""
                _cleanup_worktree(wt_path, branch, Path(project_path_str), logger)
            except Exception as e:
                logger.warning(f"[RERUN] Failed to clean up worktree {working_directory}: {e}")
    except Exception as e:
        logger.error(f"Error cleaning up design state for rerun: {e}")

    # Step 3: Clean up branches (non-blocking)
    try:
        from src.core.database import DatabaseManager
        from src.core.worktree_manager import WorktreeManager

        db_manager = DatabaseManager(None)
        bm = WorktreeManager(db_manager)
        # Without this, WorktreeManager operates on whatever project happens
        # to be config.main_repo_path's current global default -- wrong
        # project entirely once more than one project exists (see the other
        # WorktreeManager(...).reload(...) call sites in orchestrator.py,
        # which already do this for the same reason).
        bm.reload(project)
        # Run cleanup in background thread to not block pipeline start
        import threading

        thread = threading.Thread(target=lambda: bm.cleanup_all_stale_branches(), daemon=True)
        thread.start()
    except Exception as e:
        logger.error(f"Error starting branch cleanup: {e}")

    # Step 4: Move design to front of queue
    order = _load_queue_order(rerun_start_project_id)
    if filename in order:
        order.remove(filename)
    order.insert(0, filename)
    _save_queue_order(order, rerun_start_project_id)
    _invalidate("queue", f"queue:{rerun_start_project_id}")

    # Step 5: Clear pipeline state so orchestrator starts fresh
    try:
        from src.autopilot.orchestrator.state import PersistentPipelineState

        PersistentPipelineState(project_id=rerun_start_project_id).clear()
    except Exception as e:
        logger.error(f"Error clearing pipeline state: {e}")

    # Step 6: Start pipeline via the in-process AutopilotService (the same
    # singleton the play/pause button drives), not a spawned subprocess.
    try:
        from src.autopilot.service import get_autopilot_service, get_registry

        # Same concurrency-cap check POST /start enforces -- without this,
        # rerun could start a brand-new, not-yet-running project's pipeline
        # even while already at max_concurrent_projects, silently exceeding
        # the cap that starting the identical project via POST /start would
        # have rejected with a 409. try_reserve (not can_start) also closes
        # the TOCTOU race between two concurrent starts both checking the
        # cap before either has actually started -- release it as soon as
        # service.start() resolves, success or not.
        can_start, cap_message = get_registry().try_reserve(rerun_start_project_id)
        if not can_start:
            raise HTTPException(409, cap_message)

        service = get_autopilot_service(rerun_start_project_id)
        try:
            await service.start(
                project_path=str(project),
                design_queue=str(queue_dir),
                max_iterations=3,
            )
        finally:
            get_registry().release_reservation(rerun_start_project_id)

        # Wait for new workflow to be created (up to 15 seconds). asyncio.sleep,
        # not time.sleep -- this is an async route handler, and a blocking
        # sleep here would stall every other request the whole backend is
        # serving for up to 15s, not just this one.
        new_workflow_id = None
        design_name_clean = filename.replace(".md", "").replace("_", " ").lower()
        for _ in range(30):  # 30 * 0.5s = 15s max
            await asyncio.sleep(0.5)
            try:
                with get_db() as db:
                    # Check for new active workflow
                    wf = (
                        db.query(Workflow)
                        .filter(
                            Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                            Workflow.status == "active",
                        )
                        .order_by(Workflow.created_at.desc())
                        .first()
                    )
                    if wf:
                        # Verify it's for this design by checking description
                        desc = (wf.description or "").lower()
                        # Use exact match on design name (without extension)
                        if design_name_clean in desc:
                            new_workflow_id = wf.id
                            break
            except Exception:
                pass
    except HTTPException:
        raise
    except ValueError as e:
        # Matches /start's own convention: bad input (e.g. project path isn't
        # a git repo -- a real check service.start() does that the old
        # subprocess never surfaced clearly).
        logger.error(f"Error starting pipeline for rerun: {e}")
        raise HTTPException(400, f"Failed to start pipeline: {e}")
    except RuntimeError as e:
        # Matches /start's own convention: 409 means "already running" --
        # possible here despite Step 1's stop() if another request raced in
        # and started something else in the meantime.
        logger.error(f"Error starting pipeline for rerun: {e}")
        raise HTTPException(409, f"Failed to start pipeline: {e}")
    except Exception as e:
        logger.error(f"Error starting pipeline for rerun: {e}")
        raise HTTPException(500, f"Failed to start pipeline: {e}")

    _invalidate("status")

    return {
        "rerun": True,
        "filename": filename,
        "workflow_id": new_workflow_id,
        "message": f"Pipeline restarted for {filename}",
    }

@router.post("/queue/repair")
async def repair_design(request: dict):
    """Repair a design: spin up a recovery workflow and a review agent that checks
    and fixes stuck/incomplete tasks. (Branch reconciliation is obsolete under
    per-task worktree isolation — failed worktrees are discarded, never merged.)"""
    import uuid
    from pathlib import Path

    logger.info("[REPAIR] Received repair request")
    filename = request.get("filename")
    if not filename:
        raise HTTPException(400, "filename is required")

    project_path = request.get("project_path")
    if not project_path:
        raise HTTPException(400, "project_path is required")

    project = Path(project_path).resolve()
    if not project.exists():
        raise HTTPException(400, f"Project path does not exist: {project_path}")

    # Generate repair ID for tracking
    repair_id = str(uuid.uuid4())[:8]

    # Run repair in background thread pool (not async - uses sync subprocess calls)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_repair, repair_id, filename, project, logger)

    return {
        "repair_id": repair_id,
        "status": "started",
        "message": f"Repair started for {filename}. Check /api/autopilot/queue/repair/{repair_id} for results.",
    }

def spawn_repair_review_agent(wf_id: str, filename: str, project: Path, reason: str, logger, actions_taken: list):
    """Spawn a review agent that checks each task, acts, and monitors completion."""
    from src.autopilot.orchestrator.engine_client import api_post, get_tasks

    try:
        logger.info(f"[REPAIR-AGENT] Starting for workflow {wf_id[:8]}, design={filename}")

        # Get tasks for this workflow
        failed_tasks = get_tasks(status="failed", workflow_id=wf_id)
        pending_tasks = get_tasks(status="pending", workflow_id=wf_id)
        in_progress_tasks = get_tasks(status="in_progress", workflow_id=wf_id)
        done_tasks = get_tasks(status="done", workflow_id=wf_id)

        logger.info(f"[REPAIR-AGENT] Task counts: done={len(done_tasks)}, failed={len(failed_tasks)}, pending={len(pending_tasks)}, in_progress={len(in_progress_tasks)}")

        # Build task summary for instructions
        task_summary = []
        for t in failed_tasks[:5]:
            desc = (t.get("enriched_description") or t.get("raw_description") or "")[:80]
            task_summary.append(f"  FAILED: {t.get('id', '')[:8]} - {desc}")
        for t in pending_tasks[:5]:
            desc = (t.get("enriched_description") or t.get("raw_description") or "")[:80]
            task_summary.append(f"  PENDING: {t.get('id', '')[:8]} - {desc}")
        for t in in_progress_tasks[:5]:
            desc = (t.get("enriched_description") or t.get("raw_description") or "")[:80]
            task_summary.append(f"  IN_PROGRESS: {t.get('id', '')[:8]} - {desc}")

        review_instructions = get_prompt("repair_agent_instructions", {
            "filename": filename,
            "wf_id_short": wf_id[:8],
            "reason": reason,
            "done_count": len(done_tasks),
            "failed_count": len(failed_tasks),
            "pending_count": len(pending_tasks),
            "in_progress_count": len(in_progress_tasks),
            "task_summary": chr(10).join(task_summary) if task_summary else "No tasks found",
            "design_doc_path": project / DESIGN_CONTEXT_SUBDIR / filename,
        })

        # Create the task
        logger.info(f"[REPAIR-AGENT] Creating task for workflow {wf_id[:8]}")
        task_data = api_post(
            "/create_task",
            {
                "task_description": review_instructions,
                "done_definition": "All tasks resolved, branches merged, repair_report.md written",
                "workflow_id": wf_id,
                "phase_id": "repair-review",
                "priority": "high",
                "ai_agent_id": "sdk-repair-agent",
            },
            headers={"X-Agent-ID": "sdk-repair-agent"},
        )

        if not task_data:
            logger.error("[REPAIR-AGENT] api_post /create_task returned None")
            return

        if "detail" in task_data:
            logger.error(f"[REPAIR-AGENT] /create_task error: {task_data['detail']}")
            return

        task_id = task_data.get("task_id")
        if not task_id:
            logger.error(f"[REPAIR-AGENT] /create_task returned no task_id: {task_data}")
            return

        logger.info(f"[REPAIR-AGENT] Task created: {task_id[:8]}")

        # Create the agent. This runs on a background executor thread (not an
        # awaited request path), so a generous timeout is safe — but it still
        # needs to exceed the agent's own ~25s+ tmux/pi init delay
        # (src/agents/manager.py), otherwise this silently returns None while
        # the agent keeps starting up in the background, leaving the task
        # never linked to it (same failure mode fixed in resume_feature).
        logger.info(f"[REPAIR-AGENT] Creating agent for task {task_id[:8]}")
        agent_data = api_post(
            "/api/create_agent_for_task",
            {"task_id": task_id, "workflow_id": wf_id, "phase_id": "repair-review"},
            timeout=120,
        )

        if not agent_data:
            logger.error("[REPAIR-AGENT] api_post /create_agent_for_task returned None")
            return

        if "detail" in agent_data:
            logger.error(f"[REPAIR-AGENT] /create_agent_for_task error: {agent_data['detail']}")
            return

        agent_id = agent_data.get("agent_id")
        if not agent_id:
            logger.error(f"[REPAIR-AGENT] /create_agent_for_task returned no agent_id: {agent_data}")
            return

        logger.info(f"[REPAIR-AGENT] Agent created: {agent_id[:8]}")
        actions_taken.append(f"Spawned review agent {agent_id[:8]} for workflow {wf_id[:8]}")

    except Exception as e:
        logger.error(f"[REPAIR-AGENT] Exception: {e}", exc_info=True)

def _run_repair(repair_id: str, filename: str, project: Path, logger):
    """Background repair task."""
    import json
    import uuid

    from src.core.database import Workflow, get_db

    logger.info(f"[REPAIR] Starting repair {repair_id} for {filename}")

    findings = []
    actions_taken = []

    try:
        # 1. Create a minimal repair workflow directly in DB
        logger.info("[REPAIR] Step 1: Creating repair workflow")
        wf_id = f"repair-{uuid.uuid4().hex[:8]}"

        with get_db() as db:
            workflow = Workflow(
                id=wf_id,
                name=f"Repair: {filename}",
                definition_id="autopilot",
                description=f"Repair: {filename}",
                phases_folder_path=str(project),
                status="active",
                launch_params=json.dumps(
                    {
                        "design_document": str(project / DESIGN_CONTEXT_SUBDIR / filename),
                        "project_path": str(project),
                        "repair_mode": True,
                    }
                ),
            )
            db.add(workflow)
            db.commit()
            logger.info(f"[REPAIR] Workflow created: {wf_id}")

        actions_taken.append(f"Created repair workflow {wf_id[:8]}")
        findings.append({"type": "info", "message": f"Created repair workflow {wf_id[:8]}"})

        # 2. Spawn review agent on the new workflow
        logger.info("[REPAIR] Step 2: Spawning review agent")
        spawn_repair_review_agent(wf_id, filename, project, "Repair initiated", logger, actions_taken)
        logger.info("[REPAIR] Step 2 complete: spawn_repair_review_agent returned")

        # 3. Find any existing workflows for context
        logger.info("[REPAIR] Step 3: Finding existing workflows for context")
        with get_db() as db:
            workflows = db.query(Workflow).filter(Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS)).all()

            existing_workflow_ids = []
            for wf in workflows:
                if wf.launch_params:
                    try:
                        params = json.loads(wf.launch_params) if isinstance(wf.launch_params, str) else wf.launch_params
                        doc = params.get("design_document", "")
                        if filename in doc:
                            existing_workflow_ids.append(wf.id)
                    except Exception:
                        pass

            logger.info(f"[REPAIR] Found {len(existing_workflow_ids)} existing workflow(s)")
            if existing_workflow_ids:
                findings.append(
                    {
                        "type": "info",
                        "message": f"Found {len(existing_workflow_ids)} existing workflow(s) for context",
                    }
                )

        # NOTE: Repair no longer merges/cleans stray agent branches. Under
        # per-task worktree isolation a failed agent's worktree is discarded and
        # never merged, so there are no half-baked branches to reconcile. Repair
        # is now purely workflow recovery (review agent on the tasks above).

        # 4. Store results
        logger.info("[REPAIR] Step 4: Storing results")
        result = {
            "repair_id": repair_id,
            "filename": filename,
            "findings": findings,
            "actions_taken": actions_taken,
            "summary": {
                "total_findings": len(findings),
                "actions_taken": len(actions_taken),
                "workflows_created": 1,
            },
        }

        result_file = Path(AUTOPILOT_STATE_DIR) / f"repair_{repair_id}.json"
        result_file.write_text(json.dumps(result, indent=2))
        logger.info(f"[REPAIR] Repair {repair_id} complete. Actions: {len(actions_taken)}, Findings: {len(findings)}")

    except Exception as e:
        logger.error(f"[REPAIR] Exception during repair: {e}", exc_info=True)
        findings.append({"type": "error", "message": str(e)})
        result = {
            "repair_id": repair_id,
            "filename": filename,
            "findings": findings,
            "actions_taken": actions_taken,
            "summary": {"error": str(e)},
        }
        result_file = Path(AUTOPILOT_STATE_DIR) / f"repair_{repair_id}.json"
        result_file.write_text(json.dumps(result, indent=2))

@router.get("/queue/repair/{repair_id}")
async def get_repair_status(repair_id: str):
    """Get repair status and results."""
    logger.info(f"[REPAIR] Status check for {repair_id}")
    result_file = Path(AUTOPILOT_STATE_DIR) / f"repair_{repair_id}.json"
    if not result_file.exists():
        logger.info(f"[REPAIR] {repair_id} still running (no result file yet)")
        return {
            "repair_id": repair_id,
            "status": "running",
            "message": "Repair still in progress...",
        }

    try:
        result = json.loads(result_file.read_text())
        result["status"] = "completed"
        logger.info(f"[REPAIR] {repair_id} completed")
        return result
    except Exception as e:
        logger.error(f"[REPAIR] {repair_id} error reading results: {e}")
        return {"repair_id": repair_id, "status": "error", "message": str(e)}

class DesignAddByPath(BaseModel):
    file_path: str
    project_path: str

@router.post("/designs/add")
async def add_design_by_path(req: DesignAddByPath):
    """Add a design document by file path.

    Validates file exists, finds/creates AutopilotProject, checks for duplicates,
    and creates AutopilotDesign record with file_path.

    Returns:
        Design ID, name, and status
    """
    import hashlib
    import uuid

    from src.core.database import AutopilotDesign, AutopilotProject, get_db
    from src.core.simple_config import get_config

    # Validate file exists
    file_path = Path(req.file_path).resolve()
    if not file_path.exists():
        raise HTTPException(400, f"File does not exist: {file_path}")
    if not file_path.is_file():
        raise HTTPException(400, f"Path is not a file: {file_path}")
    if file_path.suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Invalid file extension: {file_path.suffix}. Allowed: {ALLOWED_EXTENSIONS}",
        )

    # Validate project path
    project_path = Path(req.project_path).resolve()
    if not project_path.exists():
        raise HTTPException(400, f"Project path does not exist: {project_path}")

    # Calculate content hash for dedup
    content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()[:16]

    with get_db() as db:
        # Find or create project
        project = db.query(AutopilotProject).filter_by(base_dir=str(project_path)).first()
        if not project:
            # Cap simultaneously-active projects at max_concurrent_projects
            # instead of exclusively clearing every other project's flag --
            # mirrors projects_api.py's create_project/activate_project.
            # Lenient like create_project's own is_first path (not a 409
            # like activate_project): activation here is a side effect of
            # an unrelated "upload a design file" action, so a full project
            # cap shouldn't fail the upload -- create it inactive instead.
            active_count = db.query(AutopilotProject).filter_by(is_active=True).count()
            max_concurrent = get_config().max_concurrent_projects
            want_active = active_count < max_concurrent
            if not want_active:
                logger.warning(
                    f"Not auto-activating new project {project_path.name!r}: "
                    f"max_concurrent_projects ({max_concurrent}) already reached"
                )
            project = AutopilotProject(
                id=f"proj-{uuid.uuid4().hex[:12]}",
                name=project_path.name,
                base_dir=str(project_path),
                is_active=want_active,
            )
            db.add(project)
            db.flush()
            logger.info(f"Created project: {project.name} ({project.id})")

        # Check for duplicate file_path
        existing = (
            db.query(AutopilotDesign)
            .filter_by(
                project_id=project.id,
                file_path=str(file_path),
            )
            .first()
        )

        if existing:
            # Return existing design
            return {
                "id": existing.id,
                "name": existing.name,
                "status": existing.status,
            }

        # Create design record
        design_id = f"des-{uuid.uuid4().hex[:12]}"
        name = file_path.stem.replace("_", " ").replace("-", " ").title()

        # Get ordinal (max ordinal + 1)
        max_ordinal = db.query(AutopilotDesign).filter_by(project_id=project.id).count()

        design = AutopilotDesign(
            id=design_id,
            project_id=project.id,
            filename=file_path.name,
            name=name,
            ordinal=max_ordinal + 1,
            size_bytes=file_path.stat().st_size,
            extension=file_path.suffix,
            content_hash=content_hash,
            status="pending",
            file_path=str(file_path),
        )
        db.add(design)
        db.commit()

        logger.info(f"Added design: {name} ({design_id}) from {file_path}")

        return {
            "id": design_id,
            "name": name,
            "status": "pending",
        }

@router.post("/queue", response_model=DesignQueueItem)
async def add_to_queue(item: DesignQueueAdd):
    try:
        effective_dir = _get_effective_queue_dir(item.project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))

    queue_path = Path(effective_dir)
    queue_path.mkdir(parents=True, exist_ok=True)

    ext = item.extension if item.extension in ALLOWED_EXTENSIONS else ".md"
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in item.name)
    safe_name = safe_name.strip().replace(" ", "_")
    if not safe_name:
        raise HTTPException(400, "Invalid design name")
    filename = f"{safe_name}{ext}"
    filepath = _safe_path(effective_dir, filename)

    if filepath.exists():
        raise HTTPException(409, f"Design '{filename}' already exists in queue")

    filepath.write_text(item.content)
    stat = filepath.stat()

    _invalidate("queue", f"queue:{item.project_id}" if item.project_id else "queue", "status")

    return DesignQueueItem(
        filename=filename,
        name=item.name,
        size_bytes=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        extension=ext,
    )

@router.delete("/queue/{filename}")
async def remove_from_queue(filename: str, project_id: Optional[str] = None):
    try:
        effective_dir = _get_effective_queue_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    filepath = _safe_path(effective_dir, filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    filepath.unlink()
    _invalidate("queue", f"queue:{project_id}" if project_id else "queue", "status")
    return {"removed": filename}

@router.get("/queue/{filename}/content")
async def get_queue_item_content(filename: str, project_id: Optional[str] = None):
    try:
        effective_dir = _get_effective_queue_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    filepath = _safe_path(effective_dir, filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    return {"filename": filename, "content": filepath.read_text(errors="replace")}
