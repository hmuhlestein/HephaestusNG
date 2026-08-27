"""Project design-file routes: sync/reload, browse, list/add/reorder/remove,
and content/status lookups for a project's design queue.

Split out of project_routes.py (SOLID review: that file mixed project CRUD,
7 cost-accounting endpoints, and design-file browsing/management -- see
docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md's finding on
src/mcp/autopilot/project_routes.py). Mounted alongside project_routes.router
in src/mcp/autopilot/__init__.py. `_design_id`/`_get_project_lock`/
`_sync_project_designs` stay in project_routes.py -- they're shared with its
own CRUD routes (create_project/delete_project also sync/relink designs) --
and are imported from there rather than duplicated.
"""

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from src.core.constants import (
    CONTEXT_DIR_NAME,
    DESIGN_CONTEXT_SUBDIR,
    DESIGN_WORKFLOW_DEFINITION_IDS,
)
from src.mcp.autopilot._shared import ALLOWED_EXTENSIONS, _cached, _invalidate, _safe_path, _store
from src.mcp.autopilot.project_routes import (
    _design_id,
    _get_project_lock,
    _sync_project_designs,
)
from src.mcp.server._shared import verify_agent_authentication
from src.services.design_status_service import get_design_status

logger = logging.getLogger(__name__)

router = APIRouter()


class DesignItem(BaseModel):
    id: str
    filename: str
    name: str
    ordinal: int
    size_bytes: int
    extension: str
    modified_at: Optional[str] = None
    workflow_type: str = "feature"
    archived_at: Optional[str] = None


class DesignReorderRequest(BaseModel):
    design_ids: List[str]


class DesignAddRequest(BaseModel):
    name: str
    content: str
    extension: str = ".md"
    # "queue" (default): .hephaestus/specs/, not git-tracked -- used by
    # "Load from Remote" (the file already lives somewhere in the project,
    # nothing new is being introduced). Any other value is a real,
    # git-tracked folder path relative to the project root -- "docs"
    # (legacy literal), DESIGN_SUBDIR/BUGFIX_SUBDIR (New Feature/Report
    # Bug flow defaults), or an arbitrary folder the user picked.
    # Validated server-side (_safe_path) to stay within the project root.
    destination: str = "queue"
    # "feature" / "bugfix" -- which pipeline this design runs through (see
    # docs/BUGFIX_WORKFLOW_TYPE_DESIGN.md). None (default): auto-detect via
    # detect_workflow_type() at add-time.
    workflow_type: Optional[str] = None
    # Set only when this content was just read from an existing project
    # file via the remote browser (LoadDesignModal's handleSelectRemoteFile)
    # and hasn't been retyped/edited since -- the project-relative path of
    # that file. Lets the "already exists" check below recognize "this is
    # the exact file the user picked, re-submitting it back to itself" and
    # return it as-is instead of erroring, WITHOUT silently swallowing a
    # genuine name collision against some unrelated existing design (e.g.
    # a freshly typed/uploaded name that happens to match one already in
    # the queue) -- that case is still a real error.
    source_remote_path: Optional[str] = None


def _get_design_queue_dir(project_base: str) -> Path:
    """Return the design queue directory (.hephaestus/specs/).

    Designs are stored outside the git repo so commits don't delete them.
    """
    return Path(project_base) / DESIGN_CONTEXT_SUBDIR


def _resolve_design_filepath(file_path: Optional[str], fallback: Path) -> Path:
    """Prefer AutopilotDesign.file_path when set over a queue-dir-relative
    fallback. destination="docs" designs (add_project_design) live under
    docs/, not .hephaestus/specs/ -- without this, content/status/delete
    404 against the wrong directory for every locally-uploaded design
    (LoadDesignModal's default destination), even though the file exists.
    """
    return Path(file_path) if file_path else fallback


@router.post("/projects/{project_id}/sync", response_model=List[DesignItem])
async def sync_project_designs(
    project_id: str,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    # SECURITY: Verify agent authentication before syncing designs
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    from src.core.database import AutopilotProject, get_db

    lock = await _get_project_lock(project_id)
    async with lock:
        with get_db() as db:
            proj = db.query(AutopilotProject).get(project_id)
            if not proj:
                raise HTTPException(404, "Project not found")

            designs = _sync_project_designs(project_id, proj.base_dir, db)

        _invalidate("queue", "status", f"project_designs:{project_id}")
        return [DesignItem(**d) for d in designs]


@router.post("/projects/{project_id}/designs/reload", response_model=List[DesignItem])
async def reload_project_designs(
    project_id: str,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    """Force resync designs from filesystem."""
    # SECURITY: Verify agent authentication before reloading designs
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    from src.core.database import AutopilotProject, get_db

    cache_key = f"project_designs:{project_id}"
    _invalidate(cache_key)
    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        _sync_project_designs(project_id, proj.base_dir, db)
    # Now fetch fresh. Must pass archived explicitly: called directly as a
    # plain function (not via route dispatch), list_project_designs'
    # archived: bool = Query(False) default stays the literal Query(...)
    # sentinel object here -- which is truthy -- instead of being resolved
    # to False the way an actual HTTP request would. Without this, reload
    # returned only archived designs (and dropped every real one).
    return await list_project_designs(project_id, archived=False)


@router.get("/projects/{project_id}/designs", response_model=List[DesignItem])
async def list_project_designs(project_id: str, archived: bool = Query(False)):
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    # Distinct cache key only for the archived view -- the plain
    # f"project_designs:{project_id}" key (unscoped) is what every existing
    # mutation endpoint already invalidates for the active list; reusing it
    # here for archived=False keeps all of them correct unchanged, rather
    # than needing every one of those call sites to invalidate two keys.
    cache_key = f"project_designs_archived:{project_id}" if archived else f"project_designs:{project_id}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        query = db.query(AutopilotDesign).filter_by(project_id=project_id)
        query = query.filter(AutopilotDesign.archived_at.isnot(None)) if archived else query.filter(AutopilotDesign.archived_at.is_(None))
        designs = query.order_by(AutopilotDesign.ordinal).all()
        result = [
            DesignItem(
                id=d.id,
                filename=d.filename,
                name=d.name,
                ordinal=d.ordinal,
                size_bytes=d.size_bytes,
                extension=d.extension,
                modified_at=d.modified_at.isoformat() if d.modified_at else None,
                workflow_type=d.workflow_type,
                archived_at=d.archived_at.isoformat() if d.archived_at else None,
            )
            for d in designs
        ]
        return _store(cache_key, result)


def _set_design_archived(project_id: str, filename: str, archived: bool) -> DesignItem:
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        d = db.query(AutopilotDesign).filter_by(project_id=project_id, filename=filename).first()
        if not d:
            raise HTTPException(404, "Design not found")

        d.archived_at = datetime.utcnow() if archived else None
        db.flush()
        item = DesignItem(
            id=d.id,
            filename=d.filename,
            name=d.name,
            ordinal=d.ordinal,
            size_bytes=d.size_bytes,
            extension=d.extension,
            modified_at=d.modified_at.isoformat() if d.modified_at else None,
            workflow_type=d.workflow_type,
            archived_at=d.archived_at.isoformat() if d.archived_at else None,
        )

    # Moves the design between the active and archived lists -- both caches
    # need invalidating, not just one.
    _invalidate(f"project_designs:{project_id}", f"project_designs_archived:{project_id}")
    return item


@router.post("/projects/{project_id}/designs/{filename}/archive", response_model=DesignItem)
async def archive_project_design(
    project_id: str,
    filename: str,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    """Hide a design from the default queue view without touching its file,
    tasks, workflows, or features -- unlike remove_project_design's
    destructive delete, this is purely a visibility flag."""
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(status_code=401, detail="Agent not authenticated. Provide valid X-Agent-ID header.")
    return _set_design_archived(project_id, filename, True)


@router.post("/projects/{project_id}/designs/{filename}/unarchive", response_model=DesignItem)
async def unarchive_project_design(
    project_id: str,
    filename: str,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(status_code=401, detail="Agent not authenticated. Provide valid X-Agent-ID header.")
    return _set_design_archived(project_id, filename, False)


@router.post("/projects/{project_id}/designs", response_model=DesignItem)
async def add_project_design(
    project_id: str,
    req: DesignAddRequest,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    # SECURITY: Verify agent authentication before adding designs
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    if req.destination == "queue":
        # Store in .hephaestus/specs/ (not git-tracked) so git commits
        # don't delete design files. Stays at the workspace-root (base_dir)
        # level, unaffected by repo count (REQ-13) -- it has no
        # git-tracking requirement to satisfy.
        design_dir = Path(base_dir) / DESIGN_CONTEXT_SUBDIR
    else:
        # Any other destination is a real, git-tracked folder -- "docs"
        # (legacy literal), DESIGN_SUBDIR/BUGFIX_SUBDIR (the New Feature/
        # Report Bug flows' defaults), or an arbitrary folder the user
        # picked via the destination-folder browser. Resolves under the
        # PRIMARY ProjectRepo's path (REQ-12), not the workspace root: a
        # multi-repo project's base_dir need not itself be a git repo, so
        # writing there wouldn't be tracked by anything; single-repo
        # projects resolve to the same base_dir as before (byte-identical).
        # Unlike "queue" above, this value can come from the client (typed
        # or browsed), so it MUST be validated to stay within the resolved
        # repo path -- _safe_path does that (raises 400 on escape), the
        # same check every other browse/content endpoint in this file
        # already applies to user-supplied paths. Resolve the repo path
        # FIRST, then _safe_path against it -- the boundary must be the
        # repo's, not the (wider) workspace root's.
        from src.core.repo_resolution import RepoNotFoundError, resolve_repo_path

        with get_db() as db:
            try:
                repo_path = resolve_repo_path(db, project_id, None)
            except (RepoNotFoundError, ValueError) as e:
                raise HTTPException(400, str(e))
        design_dir = _safe_path(str(repo_path), req.destination)
    design_dir.mkdir(parents=True, exist_ok=True)

    ext = req.extension if req.extension in ALLOWED_EXTENSIONS else ".md"
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in req.name)
    safe_name = safe_name.strip().replace(" ", "_")
    if not safe_name:
        raise HTTPException(400, "Invalid design name")
    filename = f"{safe_name}{ext}"
    filepath = _safe_path(str(design_dir), filename)

    # Selecting an already-queued design via the remote file browser (see
    # LoadDesignModal's handleSelectRemoteFile) re-submits that exact file
    # right back to its own folder -- previously a guaranteed 409 on every
    # such re-submission. Only short-circuit when source_remote_path names
    # THIS SAME file (the client clears it the moment the name/content is
    # edited) -- a name collision with some unrelated existing design (a
    # freshly typed/uploaded name that just happens to match) must still
    # be a real 409, not silently swallowed. If a design row already
    # exists for this project + filename, this is that same design;
    # return it as-is instead of erroring or inserting a duplicate row
    # (which would double its ordinal position in the queue). The file on
    # disk is left untouched either way -- nothing upstream has actually
    # changed it.
    reselected_same_file = (
        req.source_remote_path is not None
        and Path(req.source_remote_path).name == filename
    )
    if filepath.exists():
        if not reselected_same_file:
            raise HTTPException(409, f"Design '{filename}' already exists")
        with get_db() as db:
            existing = (
                db.query(AutopilotDesign)
                .filter_by(project_id=project_id, filename=filename)
                .first()
            )
            if existing:
                return DesignItem(
                    id=existing.id,
                    filename=existing.filename,
                    name=existing.name,
                    ordinal=existing.ordinal,
                    size_bytes=existing.size_bytes,
                    extension=existing.extension,
                    modified_at=existing.modified_at.isoformat() if existing.modified_at else None,
                    workflow_type=existing.workflow_type,
                )
        # File exists on disk but no matching design row (e.g. left over
        # from a previous run outside this endpoint) -- fall through and
        # register it normally; overwriting with the same content it was
        # just read from is a no-op.

    filepath.write_text(req.content)
    stat = filepath.stat()

    design_id = _design_id(project_id, filename)

    if req.workflow_type in ("feature", "bugfix"):
        workflow_type = req.workflow_type
    else:
        from src.services.workflow_type_detection import detect_workflow_type

        workflow_type = detect_workflow_type(req.name, req.content)

    with get_db() as db:
        max_ord = db.query(AutopilotDesign).filter_by(project_id=project_id).count()
        d = AutopilotDesign(
            id=design_id,
            project_id=project_id,
            filename=filename,
            name=req.name,
            ordinal=max_ord + 1,
            # Set for any real (non-"queue") destination so pick_next_design
            # (queue.py) resolves the design from its actual folder instead
            # of falling back to its DESIGN_CONTEXT_SUBDIR-based
            # reconstruction, which would look in the wrong directory. Left
            # unset for destination="queue", unchanged from before.
            file_path=str(filepath) if req.destination != "queue" else None,
            size_bytes=stat.st_size,
            extension=ext,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            workflow_type=workflow_type,
        )
        db.add(d)

    _invalidate("queue", "status", f"project_designs:{project_id}")
    return DesignItem(
        id=design_id,
        filename=filename,
        name=req.name,
        ordinal=max_ord + 1,
        size_bytes=stat.st_size,
        extension=ext,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        workflow_type=workflow_type,
    )


class EnsureFolderRequest(BaseModel):
    path: str


@router.post("/projects/{project_id}/ensure-folder")
async def ensure_project_folder(
    project_id: str,
    req: EnsureFolderRequest,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    """Create a folder (and any missing parents) under the project root if
    it doesn't already exist yet.

    Used by the New Feature/Report Bug destination-folder picker so the
    chosen folder (often a not-yet-existing default like docs/bugfix on a
    project that's never had one) is real the moment it's selected, not
    only once a design is actually submitted (add_project_design's own
    mkdir already handles that case, but leaves the folder invisible to
    a browse/select round-trip in between).
    """
    # SECURITY: Verify agent authentication before creating folders
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    folder = _safe_path(base_dir, req.path)
    folder.mkdir(parents=True, exist_ok=True)
    return {"path": req.path}


class BrowseEntry(BaseModel):
    name: str
    path: str
    type: str  # "dir" or "file"


class BrowseResult(BaseModel):
    path: str
    parent: Optional[str] = None
    entries: List[BrowseEntry]


@router.get("/projects/{project_id}/browse", response_model=BrowseResult)
async def browse_project_files(project_id: str, path: str = Query("")):
    """List directories and .md/.txt files under a project's base_dir.

    `path` is relative to base_dir; traversal above base_dir is rejected
    by `_safe_path`.
    """
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    base_resolved = Path(base_dir).resolve()
    target = _safe_path(base_dir, path) if path else base_resolved
    if not target.is_dir():
        raise HTTPException(400, "Not a directory")

    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name.startswith("."):
            continue
        try:
            rel = str(child.resolve().relative_to(base_resolved))
        except ValueError:
            # Symlink resolves outside base_dir -- skip rather than leak/crash.
            continue
        if child.is_dir():
            entries.append(BrowseEntry(name=child.name, path=rel, type="dir"))
        elif child.suffix in ALLOWED_EXTENSIONS:
            entries.append(BrowseEntry(name=child.name, path=rel, type="file"))

    rel_path = "" if target == base_resolved else str(target.relative_to(base_resolved))
    parent = None
    if rel_path:
        parent_path = str(Path(rel_path).parent)
        parent = "" if parent_path == "." else parent_path

    return BrowseResult(path=rel_path, parent=parent, entries=entries)


@router.get("/projects/{project_id}/speckit/features")
async def list_project_speckit_features(project_id: str):
    """Dashboard picker's data source (REQ-10), project_id-scoped to match
    this file's REST convention -- the dashboard only has projectId on
    hand, not a raw project_path. Thin wrapper around
    speckit.discover_speckit_features; same shape as
    control_routes.list_speckit_features."""
    from src.autopilot.orchestrator.speckit import discover_speckit_features
    from src.core.database import AutopilotProject, ProjectRepo, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        features = discover_speckit_features(db, project_id, proj.base_dir)
        primary_repo = db.query(ProjectRepo).filter_by(project_id=project_id, is_primary=True).first()
        primary_repo_id = primary_repo.id if primary_repo else None

    return [
        {
            "number": f.number,
            "slug": f.slug,
            # None for the project's PRIMARY repo (even once ProjectRepo rows
            # exist, discover_speckit_features sets a real label on every
            # repo including primary -- see repo_id_for_path's own
            # tie-break). The frontend's file-browse endpoints only ever
            # reach base_dir/the primary repo, so this is what the dashboard
            # picker actually needs to decide "selectable here" vs "needs
            # --repo on the CLI" -- not "is this project multi-repo at all."
            "repoLabel": None if f.repo_id == primary_repo_id else f.repo_label,
            "hasPlan": f.plan_path is not None,
            "hasTasks": f.tasks_path is not None,
        }
        for f in features
    ]


@router.get("/projects/{project_id}/browse/content")
async def browse_project_file_content(project_id: str, path: str = Query(...)):
    """Read the content of a .md/.txt file under a project's base_dir."""
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    target = _safe_path(base_dir, path)
    if not target.is_file() or target.suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Invalid file")

    return {
        "name": target.name,
        "content": target.read_text(errors="replace"),
        "size_bytes": target.stat().st_size,
    }


@router.put("/projects/{project_id}/designs/reorder")
async def reorder_project_designs(
    project_id: str,
    req: DesignReorderRequest,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    # SECURITY: Verify agent authentication before reordering designs
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        designs = db.query(AutopilotDesign).filter_by(project_id=project_id).all()
        by_id = {d.id: d for d in designs}

        for i, design_id in enumerate(req.design_ids):
            if design_id not in by_id:
                raise HTTPException(400, f"Unknown design id: {design_id}")
            by_id[design_id].ordinal = i + 1

        # Also save order to file for orchestrator to read
        project = db.query(AutopilotProject).get(project_id)
        if project:
            hephaestus_dir = Path(project.base_dir) / CONTEXT_DIR_NAME
            hephaestus_dir.mkdir(parents=True, exist_ok=True)
            order_file = hephaestus_dir / ".queue_order.json"
            # Map design_ids back to filenames
            ordered_filenames = [by_id[did].filename for did in req.design_ids]
            order_file.write_text(json.dumps(ordered_filenames))

    _invalidate("queue", f"project_designs:{project_id}")
    return {"order": req.design_ids}


@router.delete("/projects/{project_id}/designs/{filename}")
async def remove_project_design(
    project_id: str,
    filename: str,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    # SECURITY: Verify agent authentication before removing designs
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    logger.info(f"[DELETE] remove_project_design called: project={project_id}, file={filename}")
    from src.core.database import (
        Agent,
        AgentResult,
        AutopilotDesign,
        AutopilotProject,
        BoardConfig,
        CostEntry,
        DiagnosticRun,
        Feature,
        Memory,
        Phase,
        PhaseExecution,
        Task,
        TaskPromptOverride,
        Ticket,
        ValidationReview,
        Workflow,
        WorkflowResult,
        get_db,
    )

    # Delete DB record first, then file (atomic rollback if file delete fails)
    found = False
    worktrees_to_clean: List[Tuple[str, dict]] = []
    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

        d = db.query(AutopilotDesign).filter_by(project_id=project_id, filename=filename).first()
        if d:
            # Cascade: terminate agents, delete tasks, workflows, features
            design_features = db.query(Feature).filter_by(design_id=d.id).all()
            wf_ids = []
            for feat in design_features:
                if feat.workflow_id:
                    wf_ids.append(feat.workflow_id)
            # Also get workflows directly linked to the design
            design_wfs = db.query(Workflow).filter_by(design_id=d.id).all()
            for wf in design_wfs:
                if wf.id not in wf_ids:
                    wf_ids.append(wf.id)

            # Fallback: catch orphaned phase0/feature workflows whose design_id
            # link never got set (observed live: Workflow.design_id ended up
            # NULL for a completed Phase 0 run + its first feature workflow,
            # so neither of the two lookups above found them, and they survived
            # a delete of the design that spawned them). Match by launch_params
            # instead, the same way _relink_features_to_workflows already does
            # for Feature.workflow_id.
            orphan_candidates = (
                db.query(Workflow)
                .filter(
                    Workflow.design_id.is_(None),
                    Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                )
                .all()
            )
            for wf in orphan_candidates:
                if wf.id in wf_ids:
                    continue
                try:
                    params = wf.launch_params if isinstance(wf.launch_params, dict) else json.loads(wf.launch_params or "{}")
                except Exception:
                    continue
                if params.get("design_id") == d.id or Path(params.get("design_document", "")).name == filename:
                    wf_ids.append(wf.id)

            if wf_ids:
                # Terminate active agents for these workflows
                tasks = db.query(Task).filter(Task.workflow_id.in_(wf_ids)).all()
                task_ids = [t.id for t in tasks]
                if task_ids:
                    from src.autopilot.orchestrator.engine_client import terminate_agent

                    agents = db.query(Agent).filter(Agent.current_task_id.in_(task_ids)).filter(Agent.status.in_(["working", "starting", "idle"])).all()
                    loop = asyncio.get_event_loop()
                    for agent in agents:
                        try:
                            import functools

                            kill_result = await loop.run_in_executor(
                                None,
                                functools.partial(
                                    subprocess.run,
                                    ["tmux", "kill-session", "-t", agent.tmux_session_name],
                                    capture_output=True,
                                    timeout=3,
                                ),
                            )
                            if kill_result.returncode != 0:
                                logger.warning(
                                    f"tmux kill-session failed for agent {agent.id} "
                                    f"({agent.tmux_session_name}); it may still be running"
                                )
                        except Exception as e:
                            logger.warning(
                                f"tmux kill-session failed for agent {agent.id} "
                                f"({agent.tmux_session_name}): {e}"
                            )
                        terminate_agent(agent.id, session=db)

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

                # Collect worktree info before the Workflow rows are gone --
                # otherwise these directories orphan permanently: they're
                # deterministic per-feature paths (_create_integration_worktree),
                # and nothing else will ever find them once the DB row
                # pointing at one no longer exists -- not even the startup
                # completion-worktree sweep, which only looks at "completed"
                # workflows.
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
                # workflows.id, so leaving these behind (as this function
                # always did) made the Workflow delete below fail with a
                # FOREIGN KEY constraint error every time.
                db.query(Phase).filter(Phase.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete workflows
                db.query(Workflow).filter(Workflow.id.in_(wf_ids)).delete(synchronize_session=False)

            # Delete features
            db.query(Feature).filter_by(design_id=d.id).delete(synchronize_session=False)

            # Delete the design itself
            db.delete(d)
            found = True

    # Best-effort worktree cleanup, now that the DB transaction above has
    # committed -- not fatal if any single one can't be resolved.
    for working_directory, launch_params in worktrees_to_clean:
        try:
            wt_path = Path(working_directory)
            if not (wt_path / ".git").exists():
                continue
            project_path_str = launch_params.get("project_path")
            if not project_path_str:
                logger.warning(
                    f"[DELETE-DESIGN] {wt_path} has no launch_params.project_path "
                    "to scope cleanup to -- left in place"
                )
                continue
            import git as _git

            from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

            try:
                branch = _git.Repo(wt_path).active_branch.name
            except Exception:
                branch = ""
            # _cleanup_worktree does real git/filesystem work
            # (git worktree remove, dirty-check, archiving) -- offloaded
            # so it doesn't block the event loop.
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, _cleanup_worktree, wt_path, branch, Path(project_path_str), logger
            )
        except Exception as e:
            logger.warning(f"[DELETE-DESIGN] Failed to clean up worktree {working_directory}: {e}")

    design_dir = _get_design_queue_dir(base_dir)
    filepath = _resolve_design_filepath(
        d.file_path if d else None, _safe_path(str(design_dir), filename)
    )
    if filepath.exists():
        filepath.unlink()
        found = True

    if not found:
        raise HTTPException(404, f"Design '{filename}' not found")

    _invalidate("queue", "status", f"project_designs:{project_id}")

    # Also remove from the persisted processed-designs set so re-adding
    # triggers reprocessing
    try:
        import hashlib

        from src.autopilot.orchestrator.state import PersistentPipelineState

        # Compute hash of the design file to remove it
        if filepath.exists():
            content = filepath.read_bytes()
        else:
            # File already deleted, try to compute from remaining data
            content = filename.encode()
        h = hashlib.sha256(content).hexdigest()[:16]

        PersistentPipelineState(project_id=project_id).remove_processed_hash(h)
    except Exception:
        pass  # Non-critical

    return {"removed": filename}


@router.get("/projects/{project_id}/designs/{filename}/content")
async def get_project_design_content(project_id: str, filename: str):
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir
        design = db.query(AutopilotDesign).filter_by(project_id=project_id, filename=filename).first()
        file_path_col = design.file_path if design else None

    design_dir = _get_design_queue_dir(base_dir)
    # Validate filename doesn't contain path traversal
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid filename")
    filepath = _resolve_design_filepath(file_path_col, design_dir / filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    return {"filename": filename, "content": filepath.read_text(errors="replace")}


@router.get("/projects/{project_id}/designs/{filename}/status")
async def get_project_design_status(project_id: str, filename: str):
    """Get full status for a design: workflow, tasks, branch, feature folder."""
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir
        design = db.query(AutopilotDesign).filter_by(project_id=project_id, filename=filename).first()
        file_path_col = design.file_path if design else None

    design_dir = _get_design_queue_dir(base_dir)
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid filename")
    filepath = _resolve_design_filepath(file_path_col, design_dir / filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")

    design_content = filepath.read_text(errors="replace")
    design_name = filepath.stem.replace("_", " ").replace("-", " ")

    return await get_design_status(project_id, filename, base_dir, design_content, design_name)

