"""Design-queue routes: listing, reorder, requeue, rerun, repair, add/remove. — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.autopilot.repair_service import repair_service
from src.core.constants import (
    CONTEXT_DIR_NAME,
    DESIGN_WORKFLOW_DEFINITION_IDS,
)
from src.mcp.autopilot._shared import ALLOWED_EXTENSIONS, DesignQueueAdd, DesignQueueItem, _cached, _get_effective_queue_dir, _invalidate, _safe_path, _store

logger = logging.getLogger(__name__)

router = APIRouter()

def _get_queue_order_path(project_id: Optional[str] = None) -> Optional[Path]:
    try:
        # Write alongside other server state under .hephaestus/, not inside
        # the tracked docs/spec/ directory (which would pollute git status).
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
    all_queued_task_ids = []
    try:
        with get_db() as db:
            # Scoped to the requesting project, mirroring rerun_design's
            # own scoping (9cb947c). Without it this endpoint terminates
            # agents and pauses workflows for ANY project whose design
            # document happens to share this filename -- and design
            # filenames repeat across projects constantly (design.md,
            # feature.md, a doc copied between repos). Same incident shape
            # 9cb947c was root-caused from: a healthy, unrelated agent
            # killed mid-review by another design's queue action.
            wf_query = db.query(Workflow).filter(
                Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                Workflow.status == "active",
            )
            if req_project_id:
                wf_query = wf_query.filter(Workflow.project_id == req_project_id)
            active_workflows = wf_query.all()

            for wf in active_workflows:
                if wf.launch_params:
                    params = json.loads(wf.launch_params) if isinstance(wf.launch_params, str) else wf.launch_params
                    design_doc = params.get("design_document", "")
                    # Basename equality, not `filename in design_doc`: the
                    # substring form also matches any design whose name
                    # merely contains this one (requeuing "api.md" hit
                    # "legacy-api.md"). 533de2a hit the same class of
                    # false match with a prefix compare on project dirs.
                    if design_doc and Path(str(design_doc)).name == filename:
                        # Terminate agents for this workflow
                        # Includes blocked/under_review/validation_in_progress/
                        # needs_work, not just the plainly-active statuses --
                        # a task mid-review/validation still has a live agent
                        # attached; missing it here means that agent survives
                        # this requeue's design-state wipe, left running
                        # against state that no longer exists.
                        matched_tasks = (
                            db.query(Task)
                            .filter(
                                Task.workflow_id == wf.id,
                                Task.status.in_([
                                    "pending", "queued", "assigned", "in_progress",
                                    "blocked", "under_review", "validation_in_progress",
                                    "needs_work",
                                ]),
                            )
                            .all()
                        )
                        task_ids = [t.id for t in matched_tasks]
                        # "queued" tasks are handled separately below, through
                        # the locked QueueService.reset_queued_task_to_pending
                        # -- an unlocked status="pending" write in the same
                        # batch as the other statuses below could land inside
                        # claim_next_queued_task's select-then-dequeue window
                        # (running on an executor thread) and let a task this
                        # requeue just reset get dispatched anyway. Same race
                        # class as stop_workflow/cancel_workflow/pause_feature.
                        queued_task_ids = [t.id for t in matched_tasks if t.status == "queued"]

                        if task_ids:
                            from src.autopilot.orchestrator.engine_client import terminate_agent

                            agents = (
                                db.query(Agent)
                                .filter(
                                    Agent.current_task_id.in_(task_ids),
                                    Agent.status.in_(["working", "starting", "idle"]),
                                )
                                .all()
                            )
                            for agent in agents:
                                terminate_agent(agent.id, session=db)

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
                            for t in matched_tasks:
                                if t.status == "queued":
                                    continue
                                t.status = "pending"
                                t.assigned_agent_id = None

                        # Pause the workflow. Without paused_by set, the
                        # self-heal sweep's _try_auto_resume_paused_workflow
                        # treats "no paused_by" the same as a "system" pause
                        # (both are eligible for auto-resume) -- a requeue
                        # pause could silently get reverted within one
                        # sweep tick the moment a done task shows up in the
                        # workflow's in-progress phase.
                        #
                        # NOTE: this is a different flavor of paused_by=
                        # "user" than /stop's (control_routes.py's
                        # pause_project_workflows, which also clears
                        # AutopilotService's persisted "was running" marker
                        # via .stop()) -- that one means "leave this off
                        # until I say otherwise" and correctly survives a
                        # server restart. This one is a short-lived
                        # technical guard around the reset above; the tasks
                        # it just reset to "pending" are meant to continue
                        # once the pipeline's normal queue processing (or a
                        # restart, via the still-live AutopilotService
                        # marker) re-engages this workflow. Don't make this
                        # pause block restart-resume the way a /stop pause
                        # does -- that would strand every requeued design
                        # paused across the next restart.
                        from src.autopilot.orchestrator.engine_client import pause_workflow
                        pause_workflow(wf.id, reason="user", session=db)
                        paused_count += 1
                        all_queued_task_ids.extend(queued_task_ids)

            db.commit()

        # Each call opens its own locked session -- done after the commit
        # above so it isn't racing this session's own open transaction
        # (same ordering as cancel_workflow/stop_workflow/pause_feature/
        # stop_pipeline).
        from src.core.app_context import get_app_state

        queue_service = get_app_state().queue_service
        for queued_task_id in all_queued_task_ids:
            queue_service.reset_queued_task_to_pending(queued_task_id)
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
    """Rerun a design: stop everything, move to front, start pipeline.

    Keyed by design_id like every other design endpoint -- a directory-backed
    design has no filename to be addressed by.
    """
    design_id = request.get("design_id")
    if not design_id:
        raise HTTPException(400, "design_id is required")

    project_path = request.get("project_path")
    if not project_path:
        raise HTTPException(400, "project_path is required")

    return await repair_service.rerun(
        project_path,
        design_id,
        load_queue_order=_load_queue_order,
        save_queue_order=_save_queue_order,
        invalidate=_invalidate,
    )

@router.post("/queue/repair")
async def repair_design(request: dict):
    """Repair a design: spin up a recovery workflow and a review agent that checks
    and fixes stuck/incomplete tasks. (Branch reconciliation is obsolete under
    per-task worktree isolation — failed worktrees are discarded, never merged.)"""
    design_id = request.get("design_id")
    if not design_id:
        raise HTTPException(400, "design_id is required")

    project_path = request.get("project_path")
    if not project_path:
        raise HTTPException(400, "project_path is required")

    return await repair_service.repair(project_path, design_id)

@router.get("/queue/repair/{repair_id}")
async def get_repair_status(repair_id: str):
    """Get repair status and results."""
    return repair_service.get_repair_status(repair_id)

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
            max_concurrent = get_config().autopilot.max_concurrent_projects
            want_active = active_count < max_concurrent
            if not want_active:
                logger.warning(
                    f"Not auto-activating new project {project_path.name!r}: "
                    f"max_concurrent_projects ({max_concurrent}) already reached"
                )
            from src.services.system_settings import get_default_cost_limit

            # Apply the system default spend cap (settings:default_cost_limit_usd).
            # Passed the in-flight session deliberately: opening a nested get_db()
            # mid-flush is how SQLite deadlocks.
            project = AutopilotProject(
                id=f"proj-{uuid.uuid4().hex[:12]}",
                name=project_path.name,
                base_dir=str(project_path),
                is_active=want_active,
                cost_limit_usd=get_default_cost_limit(db),
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
