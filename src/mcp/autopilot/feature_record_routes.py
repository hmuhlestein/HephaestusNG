"""Feature report/record routes: per-workflow feature_report and
decomposition_review HTML, feature-records docs/report browsing, and the
per-feature report/docs/download/logs endpoints. — split out of
feature_routes.py (size budget; docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md
§1)."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from src.core.constants import CONTEXT_DIR_NAME, PHASE0_DEFINITION_IDS
from src.mcp.autopilot._shared import (
    _cached,
    _get_effective_features_dir,
    _safe_path,
    _store,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _find_archived_feature_report(project_base: str, workflow_id: str) -> Optional[Path]:
    """Find a workflow's feature_report.html in the archived features
    gallery, once its worktree (and Workflow.working_directory) is gone.

    PhaseManager._populate_feature_folder archives a durable copy to
    <project_base>/.hephaestus/features/<timestamp>_<design-name>/ at full
    workflow completion, right before _cleanup_worktree removes the
    worktree that would otherwise be the only copy. Folder names are
    timestamp+design-name only, not feature-specific, so a design with
    more than one feature can't be matched by name alone -- match instead
    via the workflow_id each folder's own pipeline_metrics.json records.

    Shared by get_project_design_status's has_report flag and
    get_workflow_feature_report's actual file serving, so both agree on
    exactly the same report once a feature has fully completed.
    """
    features_gallery = Path(project_base) / CONTEXT_DIR_NAME / "features"
    if not features_gallery.is_dir():
        return None
    for gallery_dir in features_gallery.iterdir():
        metrics_path = gallery_dir / "docs" / "pipeline_metrics.json"
        if not metrics_path.is_file():
            continue
        try:
            metrics = json.loads(metrics_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if metrics.get("workflow_id") != workflow_id:
            continue
        for candidate in (
            gallery_dir / "docs" / "doc_review" / "feature_report.html",
            gallery_dir / "docs" / "feature_report.html",
            gallery_dir / "feature_report.html",
        ):
            if candidate.is_file():
                return candidate
        # Continue checking other directories with the same workflow_id
        # (e.g. shared-integrations may lack the report while the main
        # feature gallery folder has it).
    return None

@router.get("/workflows/{workflow_id}/feature_report")
async def get_workflow_feature_report(workflow_id: str):
    """Serve doc_review's HTML feature report, preferring the workflow's
    live worktree and falling back to the archived features gallery copy
    once that worktree is gone.

    Checking the live worktree first is what lets the report show up on
    the feature row right after doc_review itself finishes -- before
    PhaseManager._populate_feature_folder archives a copy to the features
    gallery at FULL workflow completion (2 phases later). But
    _cleanup_worktree removes the worktree (and nulls
    Workflow.working_directory) once the feature is fully done, which is
    exactly when the archived copy becomes the only one left -- must fall
    back to it or a fully-completed feature's report 404s forever, same
    bug class as get_project_design_status's has_report flag, which this
    matches via the same _find_archived_feature_report helper.

    A Phase 0 (Feature Architect) workflow's report is the decomposition
    synopsis feature_review writes -- same filename, same live-worktree
    check above, but archived to the design's own designs_folder (via
    run_phase0's synopsis_src copy) instead of the per-feature features
    gallery, since Phase 0 predates any Feature row existing.
    """
    from src.core.database import AutopilotDesign, AutopilotProject, Workflow, get_db

    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            raise HTTPException(404, "Workflow not found")
        working_directory = wf.working_directory
        project_base_dir = None
        if wf.project_id:
            proj = db.query(AutopilotProject).filter_by(id=wf.project_id).first()
            project_base_dir = proj.base_dir if proj else None
        phase0_designs_folder = None
        if wf.definition_id in PHASE0_DEFINITION_IDS and wf.design_id:
            design = db.query(AutopilotDesign).filter_by(id=wf.design_id).first()
            phase0_designs_folder = design.designs_folder if design else None

    report_path = None
    if working_directory:
        candidate = Path(working_directory) / CONTEXT_DIR_NAME / "doc_review" / "feature_report.html"
        if not candidate.is_file():
            # feature_review's own subdirectory (Phase 0's decomposition
            # synopsis, not doc_review's) -- checked before the flat
            # fallback below since it's this phase's one sanctioned
            # location, same convention every other gated phase uses.
            candidate = Path(working_directory) / CONTEXT_DIR_NAME / "feature_review" / "feature_report.html"
        if not candidate.is_file():
            candidate = Path(working_directory) / CONTEXT_DIR_NAME / "feature_report.html"
        if not candidate.is_file():
            candidate = Path(working_directory) / "docs" / "doc_review" / "feature_report.html"
        if not candidate.is_file():
            candidate = Path(working_directory) / "docs" / "feature_report.html"
        if candidate.is_file():
            report_path = candidate

    if report_path is None and phase0_designs_folder:
        candidate = Path(phase0_designs_folder) / "feature_report.html"
        if candidate.is_file():
            report_path = candidate

    if report_path is None and project_base_dir:
        report_path = _find_archived_feature_report(project_base_dir, workflow_id)

    if report_path is None:
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))

@router.get("/workflows/{workflow_id}/decomposition_review")
async def get_workflow_decomposition_review(workflow_id: str):
    """Serve feature_review's adversarial feature_review.md for a Phase 0
    workflow.

    Same live-worktree-then-designs_folder fallback chain as
    get_workflow_feature_report, since feature_review.md is copied to
    designs_folder by run_phase0 alongside feature_report.html.
    """
    from src.core.database import AutopilotDesign, Workflow, get_db

    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            raise HTTPException(404, "Workflow not found")
        working_directory = wf.working_directory
        phase0_designs_folder = None
        if wf.definition_id in PHASE0_DEFINITION_IDS and wf.design_id:
            design = db.query(AutopilotDesign).filter_by(id=wf.design_id).first()
            phase0_designs_folder = design.designs_folder if design else None

    review_path = None
    if working_directory:
        candidate = Path(working_directory) / CONTEXT_DIR_NAME / "feature_review" / "feature_review.md"
        if not candidate.is_file():
            # TEMPORARY (Phase 2 §4.9 follow-up) -- an in-flight Phase 0
            # run started before feature_review's report moved here may
            # still be writing to the old flat .hephaestus/review.md.
            # Remove once no such run can still be active.
            candidate = Path(working_directory) / CONTEXT_DIR_NAME / "review.md"
        if candidate.is_file():
            review_path = candidate
        else:
            # Agents now write feature_review-<task_id[:8]>.md, not the
            # bare name -- same newest-match fallback read_okf_report uses
            # for scoring, so a human viewing this and the gate agree on
            # which file is "the current one."
            from src.autopilot.spec import _newest_glob_match

            newest = _newest_glob_match(
                Path(working_directory) / CONTEXT_DIR_NAME / "feature_review", "feature_review.md"
            )
            if newest:
                review_path = newest

    if review_path is None and phase0_designs_folder:
        candidate = Path(phase0_designs_folder) / "feature_review.md"
        if not candidate.is_file():
            candidate = Path(phase0_designs_folder) / "review.md"
        if candidate.is_file():
            review_path = candidate

    if review_path is None:
        raise HTTPException(404, "Review not found")
    return {"name": review_path.name, "content": review_path.read_text(errors="replace")}

def _feature_record_cost(workflow_id: Optional[str]) -> float:
    """This feature's total cost, from the authoritative DB rollup.

    Returns 0.0 when the workflow is unknown or has no recorded cost -- an
    archived feature whose Workflow row was pruned still renders, just
    without a figure.
    """
    if not workflow_id:
        return 0.0
    try:
        from src.core.database import Workflow, get_db

        with get_db() as db:
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            return float(wf.cost_total_usd or 0.0) if wf else 0.0
    except Exception as e:
        logger.debug(f"Could not read cost for workflow {workflow_id}: {e}")
        return 0.0


def _resolve_feature_docs_base(wf) -> Optional[str]:
    """Best-known directory to look for a feature's generated docs in.

    working_directory is cleared once a feature's worktree is cleaned up
    after a successful merge (see _cleanup_worktree in orchestrator.py) --
    that's correct, the worktree is genuinely gone, but it means a
    *completed* feature's docs are no longer reachable there. They were
    merged into the project's main repo, so fall back to launch_params'
    project_path (observed live: core-infrastructure showed an empty Docs
    tab despite being done, purely because this fallback was missing).
    """
    if wf.working_directory:
        return wf.working_directory
    launch_params = wf.launch_params or {}
    if isinstance(launch_params, dict):
        return launch_params.get("project_path")
    return None

@router.get("/feature-records/{feature_id}/docs")
async def list_feature_record_docs(feature_id: str):
    """List generated docs for a Feature Model row (Feature DB table).

    Distinct from /features/{feature_id}/docs above -- that endpoint reads
    from FEATURES_DIR (a scanned-directory feature id, legacy single-feature
    pipeline). This one reads from a Feature row's own workflow's
    working_directory/docs -- the storage location every current multi-
    feature design pipeline actually writes to (architecture.md,
    qa.md, etc., same files task_completion_service verifies).
    """
    from src.core.database import AutopilotDesign, Feature, Workflow, get_db

    with get_db() as db:
        feat = db.query(Feature).filter_by(id=feature_id).first()
        if not feat:
            raise HTTPException(404, f"Feature '{feature_id}' not found")

        docs: List[Dict[str, Any]] = []

        # The Feature Architect (Phase 0) writes one scope.md per feature
        # under the design's own storage folder, before the feature's own
        # workflow/worktree even exists -- distinct from (and predates) the
        # docs the feature's own pipeline phases write later. Surfaced here
        # as "architect-scope.md" so it's not confused with -- or clobbered
        # by -- a same-named file the feature's own phases might produce.
        design = db.query(AutopilotDesign).filter_by(id=feat.design_id).first() if feat.design_id else None
        if design and design.designs_folder:
            scope_path = Path(design.designs_folder) / "features" / feat.feature_key / "scope.md"
            if scope_path.is_file():
                stat = scope_path.stat()
                docs.append(
                    {
                        "name": "architect-scope.md",
                        "size_bytes": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        "type": "markdown",
                    }
                )

        if not feat.workflow_id:
            return {"docs": docs}
        wf = db.query(Workflow).filter_by(id=feat.workflow_id).first()
        if not wf:
            return {"docs": docs}
        base_dir = _resolve_feature_docs_base(wf)
        if not base_dir:
            return {"docs": docs}
        docs_dir = Path(base_dir) / "docs"

    if not docs_dir.exists():
        return {"docs": docs}

    for f in sorted(docs_dir.iterdir()):
        if f.is_file():
            stat = f.stat()
            docs.append(
                {
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "type": "markdown" if f.suffix == ".md" else "json" if f.suffix == ".json" else "text" if f.suffix == ".txt" else "other",
                }
            )
    return {"docs": docs}

@router.get("/feature-records/{feature_id}/docs/{doc_name}")
async def get_feature_record_doc(feature_id: str, doc_name: str):
    """Read one generated doc's content for a Feature Model row."""
    from src.core.database import AutopilotDesign, Feature, Workflow, get_db

    with get_db() as db:
        feat = db.query(Feature).filter_by(id=feature_id).first()
        if not feat:
            raise HTTPException(404, f"Feature '{feature_id}' not found")

        if doc_name == "architect-scope.md":
            design = db.query(AutopilotDesign).filter_by(id=feat.design_id).first() if feat.design_id else None
            if not design or not design.designs_folder:
                raise HTTPException(404, "Document 'architect-scope.md' not found")
            scope_dir = str(Path(design.designs_folder) / "features" / feat.feature_key)
            doc_path = _safe_path(scope_dir, "scope.md")
            if not doc_path.exists():
                raise HTTPException(404, "Document 'architect-scope.md' not found")
            return {"name": doc_name, "content": doc_path.read_text(errors="replace")}

        if not feat.workflow_id:
            raise HTTPException(404, f"Document '{doc_name}' not found")
        wf = db.query(Workflow).filter_by(id=feat.workflow_id).first()
        base_dir = _resolve_feature_docs_base(wf) if wf else None
        if not base_dir:
            raise HTTPException(404, "Feature's workflow has no known working directory")
        docs_dir = str(Path(base_dir) / "docs")

    doc_path = _safe_path(docs_dir, doc_name)
    if not doc_path.exists():
        raise HTTPException(404, f"Document '{doc_name}' not found")
    return {"name": doc_name, "content": doc_path.read_text(errors="replace")}

@router.get("/feature-records/{feature_id}/report")
async def get_feature_record_report(feature_id: str):
    """Serve feature_report.html as a real HTML response (not the {name,
    content} JSON shape /docs/{doc_name} above returns) for direct browser
    navigation -- the modal's header "Download Report" link needs raw
    content, not a JSON wrapper. Same live-worktree source as the other
    feature-records endpoints; same underlying file the report icon on
    the feature row (workflow-scoped) also serves, just reachable by the
    Feature DB row's own id instead of needing its workflow_id threaded
    through as a separate prop.
    """
    from src.core.database import Feature, Workflow, get_db

    with get_db() as db:
        feat = db.query(Feature).filter_by(id=feature_id).first()
        if not feat or not feat.workflow_id:
            raise HTTPException(404, f"Feature '{feature_id}' not found")
        wf = db.query(Workflow).filter_by(id=feat.workflow_id).first()
        base_dir = _resolve_feature_docs_base(wf) if wf else None

    report_path = None
    if base_dir:
        # Same candidate order as get_workflow_feature_report -- doc_review
        # is where the report actually lands (doc_review phase writes it
        # there), checked before the flatter fallback locations. This
        # endpoint's own docstring claims it already serves "the same
        # underlying file" as that one; it didn't -- a real feature's
        # report 404s here whenever it's still in the live worktree,
        # even though feature.has_report (design_status_service.py,
        # which does check doc_review/) correctly reports it exists,
        # so the review modal's iframe renders blank instead of the report.
        for rel in (
            Path(CONTEXT_DIR_NAME) / "doc_review" / "feature_report.html",
            Path(CONTEXT_DIR_NAME) / "feature_review" / "feature_report.html",
            Path(CONTEXT_DIR_NAME) / "feature_report.html",
            Path("docs") / "doc_review" / "feature_report.html",
            Path("docs") / "feature_report.html",
        ):
            candidate = Path(base_dir) / rel
            if candidate.is_file():
                report_path = candidate
                break
    if report_path is None:
        # Worktree may have been cleaned up after completion — check the
        # archived features gallery (copied there by PhaseManager before
        # _cleanup_worktree runs).
        project_base = None
        if wf and wf.project_id:
            from src.core.database import AutopilotProject
            with get_db() as _db2:
                proj = _db2.query(AutopilotProject).filter_by(id=wf.project_id).first()
                project_base = proj.base_dir if proj else None
        if not project_base and wf:
            lp = wf.launch_params or {}
            if isinstance(lp, dict):
                project_base = lp.get("project_path")
        if project_base:
            archived = _find_archived_feature_report(project_base, feat.workflow_id)
            if archived:
                report_path = archived
    if report_path is None or not report_path.is_file():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))

@router.get("/features/{feature_id}/report")
async def get_feature_report(feature_id: str, project_id: Optional[str] = None):
    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    report_path = _safe_path(effective_dir, feature_id, "feature_report.html")
    if not report_path.exists():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))

@router.get("/features/{feature_id}/docs/{doc_name}")
async def get_feature_doc(feature_id: str, doc_name: str, project_id: Optional[str] = None):
    # feature_id is globally unique (UUID), so this cache key is already
    # collision-safe across projects without needing project_id in it too.
    cache_key = f"doc:{feature_id}:{doc_name}"
    cached = _cached(cache_key, ttl=60.0)
    if cached is not None:
        return cached

    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    doc_path = _safe_path(effective_dir, feature_id, "docs", doc_name)
    if not doc_path.exists():
        raise HTTPException(404, f"Document '{doc_name}' not found")
    return _store(cache_key, {"name": doc_name, "content": doc_path.read_text(errors="replace")})

@router.get("/features/{feature_id}/download")
async def download_feature_report(feature_id: str, project_id: Optional[str] = None):
    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    report_path = _safe_path(effective_dir, feature_id, "feature_report.html")
    if not report_path.exists():
        raise HTTPException(404, "Report not found")
    return FileResponse(
        path=str(report_path),
        media_type="text/html",
        filename=f"{feature_id}_report.html",
    )

@router.get("/features/{feature_id}/logs")
async def list_feature_logs(feature_id: str, project_id: Optional[str] = None):
    """List available tmux phase logs for a feature run."""
    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    tmux_dir = _safe_path(effective_dir, feature_id, "tmux")
    if not tmux_dir.exists():
        return {"logs": []}
    logs = []
    for f in sorted(tmux_dir.glob("*.log")):
        stat = f.stat()
        logs.append(
            {
                "name": f.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return {"logs": logs}

@router.get("/features/{feature_id}/logs/{log_name}")
async def get_feature_log(feature_id: str, log_name: str, project_id: Optional[str] = None):
    """Return the content of a single tmux phase log."""
    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    log_path = _safe_path(effective_dir, feature_id, "tmux", log_name)
    if not log_path.exists() or log_path.suffix != ".log":
        raise HTTPException(404, f"Log '{log_name}' not found")
    return {"name": log_name, "content": log_path.read_text(errors="replace")}
