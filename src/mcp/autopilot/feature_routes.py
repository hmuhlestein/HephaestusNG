"""Feature routes: reports, review mode, pause/resume, docs, logs. — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from src.core.constants import (
    CONTEXT_DIR_NAME,
    PHASE0_DEFINITION_IDS,
)
from src.mcp.autopilot import _shared

# Import authentication function from server module
from src.mcp.autopilot._shared import FeatureDetail, FeatureSummary, _cached, _extract_pr_url, _feature_status, _get_effective_features_dir, _invalidate, _read_json, _safe_path, _store

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

    if review_path is None and phase0_designs_folder:
        candidate = Path(phase0_designs_folder) / "feature_review.md"
        if not candidate.is_file():
            candidate = Path(phase0_designs_folder) / "review.md"
        if candidate.is_file():
            review_path = candidate

    if review_path is None:
        raise HTTPException(404, "Review not found")
    return {"name": review_path.name, "content": review_path.read_text(errors="replace")}

def _scan_features() -> List[Dict[str, Any]]:
    cached = _cached("features", ttl=30.0)
    if cached is not None:
        return cached

    from src.core.app_context import get_app_state
    from src.core.status_derivation import derive_feature_status

    features = []
    try:
        db_manager = get_app_state().db_manager
        session = db_manager.get_session()
        try:
            from src.core.database import AutopilotProject, Feature, Workflow
            db_features = session.query(Feature).order_by(Feature.created_at.desc()).all()
            for f in db_features:
                status = f.status
                if f.workflow_id:
                    wf = session.query(Workflow).filter_by(id=f.workflow_id).first()
                    if wf:
                        derived = derive_feature_status(session, f.id, write_back=False)
                        if derived:
                            status = derived

                has_report = False
                if f.workflow_id:
                    wf = session.query(Workflow).filter_by(id=f.workflow_id).first()
                    if wf and wf.working_directory:
                        report = Path(wf.working_directory) / CONTEXT_DIR_NAME / "feature_report.html"
                        has_report = report.is_file()
                    if not has_report:
                        project_base = None
                        if wf and wf.project_id:
                            proj = session.query(AutopilotProject).filter_by(id=wf.project_id).first()
                            project_base = proj.base_dir if proj else None
                        if not project_base and wf:
                            lp = wf.launch_params or {}
                            if isinstance(lp, dict):
                                project_base = lp.get("project_path")
                        if project_base:
                            has_report = _find_archived_feature_report(project_base, f.workflow_id) is not None

                created_at = f.created_at.isoformat() if f.created_at else ""

                features.append({
                    "id": f.id,
                    "name": f.name or f.feature_key or f.id,
                    "status": status,
                    "iterations": 0,
                    "total_time_seconds": 0,
                    "stop_reason": "completed" if status == "completed" else "unknown",
                    "cost_total": f.cost_total_usd or 0,
                    "cost_currency": "USD",
                    "created_at": created_at,
                    "has_report": has_report,
                })
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error scanning features from DB: {e}")

    return _store("features", features)

@router.get("/features", response_model=List[FeatureSummary])
async def list_features():
    return _scan_features()

@router.post("/features/{feature_id}/pause")
async def pause_feature(feature_id: str):
    """Pause a feature's workflow and block its in-flight child tasks."""
    from src.core.database import Agent, Feature, Task, Workflow, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(status_code=404, detail="Feature not found")
        if not feature.workflow_id:
            raise HTTPException(status_code=400, detail="Feature has no linked workflow")

        wf = db.query(Workflow).filter_by(id=feature.workflow_id).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if wf.status != "active":
            return {"success": True, "message": f"Workflow already {wf.status}"}

        # Terminate any agent actively working a task on this feature, and mark
        # every not-yet-done task 'blocked' so the UI reflects the pause and the
        # orchestrator will not advance them until resume.
        active_tasks = (
            db.query(Task)
            .filter(
                Task.workflow_id == feature.workflow_id,
                Task.status.in_([
                    "pending", "queued", "assigned", "in_progress",
                    "under_review", "validation_in_progress", "needs_work",
                ]),
            )
            .all()
        )
        from src.autopilot.orchestrator.engine_client import terminate_agent

        # "queued" tasks are handled separately below, through the locked
        # cancel-path's sibling (QueueService.pause_queued_task) -- an
        # unlocked status="blocked" write here could land inside
        # claim_next_queued_task's select-then-dequeue window (running on
        # an executor thread) and let a task this pause just "blocked" get
        # dispatched anyway. Same race class as cancel_workflow/
        # stop_workflow's own queued-task handling.
        queued_task_ids = [t.id for t in active_tasks if t.status == "queued"]
        for task in active_tasks:
            if task.status == "queued":
                continue
            if task.assigned_agent_id:
                agent = db.query(Agent).filter_by(id=task.assigned_agent_id).first()
                if agent and agent.status in ("working", "starting", "idle"):
                    # The primitive also clears this task's stale
                    # assigned_agent_id, which the raw write here left
                    # pointing at the just-terminated agent.
                    terminate_agent(agent.id, session=db)
            task.status = "blocked"

        # cascade_to_feature=False: this endpoint already owns the write
        # for the one feature it was called with, below -- a workflow-wide
        # cascade would be redundant here (and, in the unlikely event
        # multiple features share this workflow_id, would incorrectly
        # pause features this endpoint wasn't asked to touch).
        from src.autopilot.orchestrator.engine_client import pause_workflow
        # Same marker /autopilot/stop sets -- without it, the self-heal
        # sweep's _try_auto_resume_paused_workflow silently un-pauses this
        # feature again within one sweep tick (~20-30s), the same bug the
        # pipeline-level pause button had.
        pause_workflow(feature.workflow_id, reason="user", cascade_to_feature=False, session=db)
        feature.status = "paused"
        db.commit()

        # Each call opens its own locked session -- done after the commit
        # above so it isn't racing this session's own open transaction
        # (same ordering as cancel_workflow/stop_workflow).
        for queued_task_id in queued_task_ids:
            server_state.queue_service.pause_queued_task(queued_task_id)

        return {
            "success": True,
            "message": f"Paused feature {feature.name} ({len(active_tasks)} task(s) blocked)",
        }

@router.post("/features/{feature_id}/resume")
async def resume_feature(feature_id: str):
    """Resume a paused or failed feature: recover blocked, failed, and errored tasks."""
    from src.core.database import Agent, Feature, Phase, Task, Workflow, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(status_code=404, detail="Feature not found")
        if not feature.workflow_id:
            raise HTTPException(status_code=400, detail="Feature has no linked workflow")
        workflow_id = feature.workflow_id
        feature_name = feature.name

        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")

        # Resume workflow if paused or failed. "paused" goes through the
        # shared primitive (force=True: an explicit Resume click overrides
        # any pause reason, same as the pipeline-level resume endpoint) --
        # cascade_to_feature=False since this endpoint always sets
        # feature.status="active" itself, below, regardless of which
        # feature(s) share this workflow_id. "failed" isn't a pause state
        # at all, so it stays a direct write rather than going through a
        # pause-focused primitive.
        if wf.status == "paused":
            from src.autopilot.orchestrator.engine_client import resume_workflow as _resume_workflow_primitive
            _resume_workflow_primitive(workflow_id, force=True, cascade_to_feature=False, session=db)
        elif wf.status == "failed":
            wf.status = "active"
            wf.paused_by = None
            # Clear a stale arbitration/pause reason -- otherwise it lingers
            # and reads as an ongoing problem even after the user has
            # manually resolved it and resumed.
            wf.status_reason = None

        # Recover blocked/failed tasks, plus any task still marked
        # assigned/in_progress whose agent was terminated (errored/orphaned
        # rather than cleanly failed) — pressing resume should retry all of these.
        #
        # "pending" must be included here -- an hours-old, never-dispatched
        # pending task (no assigned_agent_id, nobody working it) is exactly
        # as "restartable" as a failed one. Without it, such a task is
        # invisible to this query, `restartable` looks empty, and a caller
        # that then creates a brand-new task for the same phase (see
        # review_feature's request_changes branch below) strands the old
        # pending task outside its own phase's cycle once the phase gets
        # reopened to "now" -- it later gets swept up by an unrelated
        # staleness check and marked "Orphaned: never dispatched to an
        # agent", even though nothing was ever actually wrong with it
        # beyond this endpoint failing to notice it existed. Confirmed
        # live: task 146d191d.
        candidates_query = db.query(Task).filter(
            Task.workflow_id == workflow_id,
            # needs_work included: set when a validator rejects a task and
            # sends feedback back to the same agent (assigned_agent_id
            # still points at it) -- if that agent then dies before acting
            # on the feedback, the task must still reach the
            # assigned_agent_id/dead-agent check below, or it's invisible
            # to every resume path here.
            Task.status.in_(["blocked", "failed", "assigned", "in_progress", "pending", "needs_work"]),
        )
        candidates = candidates_query.all()
        restartable = []
        for t in candidates:
            if t.status in ("blocked", "failed"):
                restartable.append(t)
            elif t.status == "pending" and not t.assigned_agent_id:
                restartable.append(t)
            elif t.assigned_agent_id:
                agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
                if not agent or agent.status == "terminated":
                    restartable.append(t)

        to_restart = [(t.id, t.phase_id) for t in restartable]
        for t in restartable:
            t.status = "pending"
            t.failure_reason = None
            t.assigned_agent_id = None

        # Always set feature to active on resume
        feature.status = "active"
        db.commit()

    # Spawn a fresh agent for each restarted task. This runs in-process
    # (not a self-HTTP call) and is fired off in the background: agent
    # initialization can legitimately take 25s+, so awaiting it here would
    # block the response and (as a prior version did via a synchronous HTTP
    # call to this same server with a 30s timeout) time out before the agent
    # ever finished starting, leaving the task stuck at 'pending' forever.
    #
    # spawn_background_task, not a bare asyncio.create_task: an unreferenced
    # task can be silently garbage-collected before it runs -- confirmed
    # live elsewhere (c1cc687) to strand a task exactly this same "stuck at
    # pending forever" way, just via a different mechanism than the timeout
    # this comment already guards against.
    from src.mcp.server._shared import spawn_background_task

    for task_id, phase_id in to_restart:
        spawn_background_task(_spawn_agent_for_task(task_id, phase_id))

    return {
        "success": True,
        "message": f"Resumed feature {feature_name} — restarting {len(to_restart)} task(s)",
    }

class ReviewModeUpdate(BaseModel):
    review_mode: bool

class FeatureReviewRequest(BaseModel):
    action: str  # "approve" or "request_changes"
    feedback: Optional[str] = None

@router.patch("/projects/{project_id}/review-mode")
async def set_review_mode(project_id: str, req: ReviewModeUpdate):
    """Toggle review mode for a project. When enabled, the pipeline pauses
    after each feature's deploy phase and waits for explicit approval."""
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        proj.review_mode = req.review_mode
        db.commit()
    _invalidate("status")
    return {"review_mode": req.review_mode}

async def _review_phase0_decomposition(workflow_id: str, req: FeatureReviewRequest):
    """Approve or request changes for a Phase 0 (Feature Architect) decomposition.

    Mirrors review_feature's real-feature flow but operates on the Phase 0
    Workflow directly -- there's no Feature row yet at this point, Phase 0
    is what creates them. Approve clears the review pause the same way the
    "Feature Architect" row's existing Resume action already does (run_phase0's
    own wait loop, _wait_for_phase0_review_clearance, just polls paused_by).
    request_changes creates a new task on the feature_architect phase
    carrying the human feedback and spawns an agent for it directly, the
    same one-off-task pattern review_feature uses for a real feature's
    development phase, leaving the workflow paused for review so the redone
    decomposition gets a second look before it's approved.
    """
    from src.core.database import Phase, Task, TaskPromptOverride, Workflow, get_db

    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if wf.paused_by != "review":
            return {"success": True, "message": "Decomposition was not awaiting review"}

        arch_phase = (
            db.query(Phase)
            .filter(Phase.workflow_id == workflow_id, Phase.name == "feature_architect")
            .first()
        )

        if req.action == "approve":
            # A prior request_changes may still have a redo agent working in
            # this same worktree. Approving now would let run_phase0's wait
            # loop return immediately, create Feature rows from a
            # possibly-half-written features.json, and then delete the
            # worktree out from under the still-running agent (run_phase0's
            # finally block cleans it up once it considers Phase 0 fully
            # succeeded). Block until the redo settles instead.
            if arch_phase:
                in_flight = (
                    db.query(Task)
                    .filter(
                        Task.workflow_id == workflow_id,
                        Task.phase_id == arch_phase.id,
                        Task.status.in_(["pending", "assigned", "in_progress"]),
                    )
                    .first()
                )
                if in_flight:
                    raise HTTPException(
                        status_code=409,
                        detail="A requested-changes redo is still in progress — wait for it to finish before approving.",
                    )
            # No Feature row to cascade to at this point -- Phase 0 is what
            # creates them, same as _pause_phase0_for_review's own reasoning.
            from src.autopilot.orchestrator.engine_client import resume_workflow
            resume_workflow(workflow_id, force=True, cascade_to_feature=False, session=db)
            db.commit()
            _invalidate("status")

            # Normally run_phase0's own wait loop (still polling in-process)
            # notices this clearance and finishes the job itself. But if
            # this pause was set by the out-of-band completion hook
            # (PhaseManager._complete_workflow -> finalize_phase0_workflow,
            # e.g. after a backend restart left run_phase0 with no live
            # waiter), nothing else will ever create the Feature rows.
            # finalize_phase0_workflow and _create_feature_records are both
            # idempotent, so calling it here unconditionally is a safe
            # no-op in the run_phase0-is-still-waiting case.
            from src.autopilot.orchestrator import finalize_phase0_workflow
            finalize_phase0_workflow(workflow_id, logger, skip_review_gate=True)

            return {"success": True, "message": "Feature decomposition approved"}

        # request_changes — re-decompose with the human's feedback.
        if not arch_phase:
            raise HTTPException(status_code=500, detail="feature_architect phase not found")

        import uuid
        # This one-off task redoes both feature_architect's decomposition and
        # feature_review's adversarial pass in a single agent run -- there is
        # no orchestration engine left running to hand off between the two
        # phases the normal way (the workflow already reached "completed"
        # before pausing for review; run_single_workflow's own phase-by-phase
        # loop, which would otherwise sequence this, already returned).
        # Skipping the feature_review.md/feature_report.html rewrite would
        # leave the review modal showing the pre-redo synopsis and findings
        # forever.
        feedback_prompt = (
            f"## Human Review Feedback\n\n{req.feedback.strip()}\n\n"
            "Re-decompose the design taking the above feedback into account. "
            "Update .hephaestus/features.json and each feature's scope.md accordingly.\n\n"
            "Then, in this same task, perform the adversarial feature-review pass "
            "yourself: compare the revised decomposition against the design document "
            "the same way the feature_review phase does, and rewrite "
            ".hephaestus/feature_review/feature_review.md and "
            ".hephaestus/feature_review/feature_report.html so both "
            "reflect the revised decomposition -- they are what the human reviewer "
            "sees next, and must not be left describing the old decomposition."
        )

        # Same restartable-task check as the real-feature request_changes
        # path below: if a prior redo is still blocked/failed/orphaned,
        # restart it instead of piling on a second concurrent agent in the
        # same worktree (which would race on features.json). Scoped to this
        # phase specifically, since the real-feature version's workflow-wide
        # scope has no analogous ambiguity (Phase 0 has two phases sharing
        # one workflow).
        candidates = (
            db.query(Task)
            .filter(
                Task.workflow_id == workflow_id,
                Task.phase_id == arch_phase.id,
                # needs_work included: set when a validator rejects a task
                # and sends feedback back to the same agent
                # (assigned_agent_id still points at it) -- if that agent
                # then dies before acting on the feedback, the task must
                # still reach the assigned_agent_id/dead-agent check
                # below, or it's invisible to every resume path here.
                Task.status.in_(["blocked", "failed", "assigned", "in_progress", "pending", "needs_work"]),
            )
            .all()
        )
        restartable = []
        for t in candidates:
            if t.status in ("blocked", "failed", "pending"):
                restartable.append(t)
            elif t.assigned_agent_id:
                from src.core.database import Agent
                agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
                if not agent or agent.status == "terminated":
                    restartable.append(t)

        if restartable:
            # Mirrors the real-feature restartable-task path below: leave
            # raw_description alone and prefix the new feedback onto the
            # existing override rather than replacing it, so an earlier
            # redo round's feedback isn't silently dropped if it wasn't
            # fully addressed yet.
            reuse_task = restartable[0]
            reuse_task.status = "pending"
            reuse_task.failure_reason = None
            reuse_task.assigned_agent_id = None
            override = db.query(TaskPromptOverride).filter_by(task_id=reuse_task.id).first()
            if override:
                override.user_prompt = feedback_prompt + "\n\n---\n\n" + (override.user_prompt or "")
                override.updated_by = "ui-user"
            else:
                db.add(TaskPromptOverride(
                    task_id=reuse_task.id,
                    user_prompt=feedback_prompt,
                    updated_by="ui-user",
                ))
            task_id, phase_id = reuse_task.id, reuse_task.phase_id
        else:
            new_task = Task(
                id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                phase_id=arch_phase.id,
                raw_description=feedback_prompt,
                enriched_description=None,
                done_definition="Feature decomposition revised per human feedback, feature_review.md and feature_report.html rewritten to match",
                status="pending",
                priority="high",
            )
            db.add(new_task)
            db.flush()
            db.add(TaskPromptOverride(
                task_id=new_task.id,
                user_prompt=feedback_prompt,
                updated_by="ui-user",
            ))
            task_id, phase_id = new_task.id, new_task.phase_id

        # Keep the workflow paused for review — the human must approve
        # again once the redone decomposition is ready.
        db.commit()

    logger.info(f"[REVIEW] Spawning agent for Phase 0 re-decomposition task {task_id}")
    from src.mcp.server._shared import spawn_background_task

    spawn_background_task(_spawn_agent_for_task(task_id, phase_id))

    _invalidate("status")
    return {"success": True, "message": "Changes requested — re-decomposition queued"}

@router.post("/features/{feature_id}/review")
async def review_feature(feature_id: str, req: FeatureReviewRequest):
    """Approve a feature or request changes.

    approve:          clears the review pause, pipeline advances.
    request_changes:  saves feedback, resumes iteration, pipeline advances.
    """
    if req.action not in ("approve", "request_changes"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'request_changes'")
    if req.action == "request_changes" and not (req.feedback or "").strip():
        raise HTTPException(status_code=400, detail="feedback is required when requesting changes")

    if feature_id.startswith("phase0-"):
        return await _review_phase0_decomposition(feature_id[len("phase0-"):], req)

    from src.core.database import Feature, Phase, Task, Workflow, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(status_code=404, detail="Feature not found")
        if not feature.workflow_id:
            raise HTTPException(status_code=400, detail="Feature has no linked workflow")

        wf = db.query(Workflow).filter_by(id=feature.workflow_id).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if wf.paused_by != "review":
            # Idempotent — already cleared (user double-clicked, or pipeline
            # advanced on its own). Return success rather than an error.
            return {"success": True, "message": "Feature was not awaiting review"}

        feature.review_status = "approved" if req.action == "approve" else "changes_requested"
        feature.reviewed_at = datetime.utcnow()
        feature.reviewed_by = "ui-user"

        if req.action == "approve":
            # Clear the review pause — orchestrator's _wait_for_review_clearance
            # polls paused_by; setting it to None unblocks the loop.
            # cascade_to_feature=False: this endpoint already owns the
            # write for `feature` specifically, below.
            from src.autopilot.orchestrator.engine_client import resume_workflow
            resume_workflow(feature.workflow_id, force=True, cascade_to_feature=False, session=db)
            # Restore Feature.status to "active" so derive_feature_status
            # doesn't short-circuit on "paused" forever after approval.
            feature.status = "active"
            db.commit()

            # Create review_approved marker so the safe git wrapper
            # allows git merge. Without this, the agent-safe-bin/git
            # script blocks all merge commands.
            if wf.working_directory:
                from pathlib import Path
                marker_dir = Path(wf.working_directory) / ".hephaestus"
                marker_dir.mkdir(parents=True, exist_ok=True)
                marker = marker_dir / "review_approved"
                marker.write_text(f"Approved at {datetime.utcnow().isoformat()}\n")
                logger.info(f"[REVIEW] Created review_approved marker at {marker}")

            # In review mode, git_expert created a PR but didn't merge.
            # Merge it now that the feature is approved.
            pr_url = feature.pr_url or _extract_pr_url(db, wf.id, {})
            if pr_url:
                import functools
                import subprocess
                try:
                    # Try gh pr merge first -- offloaded, this can take up
                    # to the full 30s timeout and would otherwise block the
                    # event loop (every other request this process is
                    # serving) for that whole window on every approval.
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        None,
                        functools.partial(
                            subprocess.run,
                            ["gh", "pr", "merge", pr_url, "--merge"],
                            capture_output=True, text=True, timeout=30,
                        ),
                    )
                    if result.returncode == 0:
                        logger.info(f"[REVIEW] Merged PR {pr_url} after approval")
                    else:
                        logger.warning(f"[REVIEW] gh pr merge failed: {result.stderr}")
                except Exception as e:
                    logger.warning(f"[REVIEW] Failed to merge PR: {e}")
            elif wf.working_directory:
                # No PR to merge -- git_expert couldn't create one
                # (gh not installed/authenticated, no remote configured,
                # etc; see its own "or local merge if gh unavailable"
                # fallback instruction). The reviewed work already sits
                # committed on the pushed feature branch with
                # review_approved now written -- merge it locally into
                # the project's main branch instead of silently
                # completing the workflow with real, approved work never
                # landing on main.
                #
                # Resolved against THIS workflow's own project root
                # (AutopilotProject.base_dir), not server_state.
                # branch_manager's single global WorktreeManager instance
                # -- that instance is fixed to whichever project it
                # happened to be constructed against and unsafe to assume
                # matches this workflow's project under this app's
                # concurrent-active-projects support (see CLAUDE.md's
                # concurrent-active-projects invariant).
                try:
                    from git import GitCommandError, Repo

                    from src.core.database import AutopilotProject, resolve_project_for_workflow

                    project_id, _ = resolve_project_for_workflow(wf.id)
                    project = db.query(AutopilotProject).get(project_id) if project_id else None
                    if project and project.base_dir:
                        wt_repo = Repo(wf.working_directory)
                        branch_name = wt_repo.active_branch.name
                        merge_message = f"Merge {branch_name} into main after human review approval"

                        def _local_merge():
                            # Mirrors WorktreeManager.merge_shared_branch's
                            # own semantics (no_ff, abort-and-preserve on
                            # conflict -- never auto-resolve), just against
                            # this workflow's own project path instead of
                            # a shared global instance's fixed one.
                            main_repo = Repo(project.base_dir)
                            try:
                                main_repo.git.merge(branch_name, no_ff=True, m=merge_message)
                                return {"action": "merged", "branch": branch_name}
                            except GitCommandError as e:
                                if "CONFLICT" in str(e):
                                    try:
                                        main_repo.git.merge("--abort")
                                    except GitCommandError:
                                        pass
                                    return {"action": "preserved", "branch": branch_name}
                                raise

                        import functools
                        loop = asyncio.get_event_loop()
                        merge_result = await loop.run_in_executor(None, _local_merge)
                        logger.info(f"[REVIEW] Local merge fallback for {branch_name}: {merge_result}")
                except Exception as e:
                    logger.warning(f"[REVIEW] Local merge fallback failed: {e}")

            # Check if all tasks are done — use derive_workflow_status
            # instead of hand-rolling this check. The "all tasks done ≠
            # all phases done" mistake has recurred independently at
            # least four times in this codebase's history.
            from src.core.status_derivation import derive_workflow_status
            derived = derive_workflow_status(db, wf.id, write_back=False)
            if derived == "completed":
                wf.status = "completed"
                feature.status = "completed"
                db.commit()

            _invalidate("status")
            return {"success": True, "message": f"Feature {feature.name} approved"}

        # request_changes path
        feature.review_feedback = req.feedback
        workflow_id = feature.workflow_id
        feature_name = feature.name

        # Keep workflow paused for review - the feature stays yellow
        # until user approves after development fixes are done
        # Don't resume the workflow here, just create the task

        # Find restartable tasks, or create a new one if all are done.
        #
        # "pending" must be included -- an hours-old, never-dispatched
        # pending task (no assigned_agent_id) is exactly as restartable as
        # a failed one. Without it, such a task is invisible here,
        # `restartable` looks empty, and the "create a new development
        # task" branch below fires and creates a SECOND task for the same
        # phase -- stranding the original pending task outside its own
        # phase's cycle once reopen_phase_execution below resets
        # started_at to "now". It's then swept up by an unrelated
        # staleness check and marked "Orphaned: never dispatched to an
        # agent", even though nothing was ever wrong with it beyond this
        # query failing to see it. Confirmed live: task 146d191d.
        candidates_query = db.query(Task).filter(
            Task.workflow_id == workflow_id,
            # needs_work included: set when a validator rejects a task and
            # sends feedback back to the same agent (assigned_agent_id
            # still points at it) -- if that agent then dies before acting
            # on the feedback, the task must still reach the
            # assigned_agent_id/dead-agent check below, or it's invisible
            # to every resume path here.
            Task.status.in_(["blocked", "failed", "assigned", "in_progress", "pending", "needs_work"]),
        )
        candidates = candidates_query.all()
        restartable = []
        for t in candidates:
            if t.status in ("blocked", "failed"):
                restartable.append(t)
            elif t.status == "pending" and not t.assigned_agent_id:
                restartable.append(t)
            elif t.assigned_agent_id:
                from src.core.database import Agent
                agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
                if not agent or agent.status == "terminated":
                    restartable.append(t)

        # If no restartable tasks, create a new development task
        # to address the feedback directly
        if not restartable:
            from src.core.database import Phase
            # Find the development phase
            dev_phase = (
                db.query(Phase)
                .filter(
                    Phase.workflow_id == workflow_id,
                    Phase.name == "development",
                )
                .first()
            )
            if dev_phase:
                import uuid
                # Load feedback prompt template from YAML
                feedback_prompt = f"## Human Review Feedback\n\n{req.feedback.strip()}\n\nRead the feature report for context: .hephaestus/feature_report.html\n\nAddress all feedback items and make the necessary code changes."
                try:
                    from pathlib import Path as _Path

                    import yaml as _yaml
                    prompt_file = _Path(__file__).parent.parent.parent / "config" / "prompts" / "review_feedback.yaml"
                    if prompt_file.exists():
                        with open(prompt_file) as f:
                            prompt_config = _yaml.safe_load(f)
                            feedback_prompt = prompt_config.get("review_feedback_prompt", feedback_prompt).format(feedback=req.feedback.strip())
                except Exception:
                    pass  # Use default prompt

                new_task = Task(
                    id=str(uuid.uuid4()),
                    workflow_id=workflow_id,
                    phase_id=dev_phase.id,
                    raw_description=feedback_prompt,
                    enriched_description=None,
                    done_definition="All review feedback addressed",
                    status="pending",
                    priority="high",
                )
                db.add(new_task)

                # The development phase's own PhaseExecution can be
                # "completed" (or "pending") at this point -- it already
                # ran to completion earlier in the workflow, same as any
                # phase that's since moved on. _create_phase_task's own
                # task-creation path always reopens the PhaseExecution to
                # match a freshly-created task (see reopen_phase_execution);
                # this ad-hoc creation path didn't, leaving a "completed"
                # (or "pending") phase with a live pending task that no
                # dispatch/self-heal case recognizes -- Case 2 only looks
                # at phases already "in_progress", and the two pending-
                # phase self-heals (_release_pending_phases_with_done_
                # tasks/_release_pending_phases_with_orphaned_task) don't
                # match "completed" at all. Confirmed live: task 146d191d
                # sat here, its own phase reading "completed", invisible
                # to every sweep tick.
                from src.autopilot.orchestrator.phase_transitions import reopen_phase_execution
                from src.core.database import PhaseExecution
                dev_execution = db.query(PhaseExecution).filter_by(phase_id=dev_phase.id).first()
                if dev_execution and dev_execution.status != "in_progress":
                    reopen_phase_execution(dev_execution, status="in_progress", started_at="now")

                db.flush()
                restartable.append(new_task)
                logger.info(f"[REVIEW] Created new development task {new_task.id} for feedback")
            else:
                logger.warning(f"[REVIEW] No development phase found for workflow {workflow_id}")

        to_restart = [(t.id, t.phase_id) for t in restartable]
        for t in restartable:
            t.status = "pending"
            t.failure_reason = None
            t.assigned_agent_id = None

        # Inject feedback into each restarted task via TaskPromptOverride
        if req.feedback and to_restart:
            from src.core.database import TaskPromptOverride
            feedback_prefix = (
                f"## Human Review Feedback\n\n{req.feedback.strip()}\n\n"
                "Please address the above feedback in your implementation.\n\n---\n\n"
            )
            for task_id, _ in to_restart:
                existing = db.query(TaskPromptOverride).filter_by(task_id=task_id).first()
                if existing:
                    existing.user_prompt = feedback_prefix + (existing.user_prompt or "")
                else:
                    db.add(TaskPromptOverride(
                        task_id=task_id,
                        user_prompt=feedback_prefix,
                        updated_by="ui-user",
                    ))

        # Keep feature paused for review - user must approve after fixes
        # feature.status stays "paused" and wf.paused_by stays "review"
        db.commit()

    # Spawn agents for restarted tasks (out of DB session, same as resume_feature)
    from src.mcp.server._shared import spawn_background_task

    for task_id, phase_id in to_restart:
        logger.info(f"[REVIEW] Spawning agent for task {task_id} (phase {phase_id})")
        spawn_background_task(_spawn_agent_for_task(task_id, phase_id))

    _invalidate("status")
    return {
        "success": True,
        "message": f"Changes requested for {feature_name} — restarting {len(to_restart)} task(s)",
    }

@router.delete("/features/{feature_id}")
async def delete_feature(feature_id: str):
    """Permanently delete a feature: terminate any agent still working its
    tasks, remove its worktree (if any), and delete the feature, its
    workflow, and every dependent record. For an old/stuck feature run
    that has no path back to "done" and just clutters the queue -- mirrors
    rerun_design's own cleanup (Step 2b above), scoped to one feature
    instead of an entire design.
    """
    from sqlalchemy.exc import IntegrityError

    from src.core.app_context import get_app_state
    from src.core.database import (
        AgentResult,
        BoardConfig,
        CostEntry,
        DiagnosticRun,
        Feature,
        Memory,
        PhaseExecution,
        Task,
        TaskPromptOverride,
        Ticket,
        ValidationReview,
        Workflow,
        WorkflowResult,
        get_db,
    )

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(status_code=404, detail="Feature not found")

        workflow_id = feature.workflow_id
        working_directory = None
        launch_params: dict = {}
        agent_ids_to_terminate: List[str] = []
        if workflow_id:
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            if wf:
                working_directory = wf.working_directory
                launch_params = wf.launch_params if isinstance(wf.launch_params, dict) else {}
            agent_ids_to_terminate = [
                t.assigned_agent_id
                for t in db.query(Task).filter(
                    Task.workflow_id == workflow_id,
                    Task.assigned_agent_id.isnot(None),
                )
                if t.assigned_agent_id
            ]

    # Terminate before deleting: Agent.current_task_id is a foreign key
    # (foreign_keys=ON) and terminate_agent is what clears it, same
    # reasoning as the single-task DELETE endpoint (server.py).
    if agent_ids_to_terminate:
        server_state = get_app_state()
        for agent_id in agent_ids_to_terminate:
            await server_state.agent_manager.terminate_agent(agent_id)

    try:
        with get_db() as db:
            feature = db.query(Feature).filter_by(id=feature_id).first()
            if not feature:
                raise HTTPException(status_code=404, detail="Feature not found")

            if workflow_id:
                task_ids = [t.id for t in db.query(Task).filter(Task.workflow_id == workflow_id).all()]
                if task_ids:
                    db.query(TaskPromptOverride).filter(TaskPromptOverride.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(ValidationReview).filter(ValidationReview.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(AgentResult).filter(AgentResult.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Memory).filter(Memory.related_task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Ticket).filter(Ticket.task_id.in_(task_ids)).delete(synchronize_session=False)
                    # CostEntry.task_id/workflow_id are also enforced FKs -- a
                    # feature that ever recorded real LLM cost (the common
                    # case, not the exception) would otherwise fail to delete.
                    db.query(CostEntry).filter(CostEntry.task_id.in_(task_ids)).delete(synchronize_session=False)

                db.query(DiagnosticRun).filter(DiagnosticRun.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(WorkflowResult).filter(WorkflowResult.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(BoardConfig).filter(BoardConfig.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(Ticket).filter(Ticket.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(CostEntry).filter(CostEntry.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(PhaseExecution).filter(PhaseExecution.workflow_execution_id == workflow_id).delete(synchronize_session=False)
                db.query(Task).filter(Task.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(Workflow).filter_by(id=workflow_id).delete(synchronize_session=False)

            db.delete(feature)
    except IntegrityError as e:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete feature {feature_id}: other records still reference it -- {e}",
        )

    # Best-effort worktree cleanup. Not fatal if it can't be resolved --
    # the startup sweep (sweep_completed_workflow_worktrees) only catches
    # "completed" workflows, and this Workflow row is now gone entirely,
    # so this is the one chance to reclaim the directory.
    if working_directory and ".worktrees/" in working_directory:
        try:
            wt_path = Path(working_directory)
            if (wt_path / ".git").exists():
                project_path_str = launch_params.get("project_path")
                if project_path_str:
                    import git as _git

                    from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

                    try:
                        branch = _git.Repo(wt_path).active_branch.name
                    except Exception:
                        branch = ""
                    # _cleanup_worktree only calls .info/.warning -- this
                    # module's own logger satisfies that without needing
                    # OrchestratorLogger's real log-file machinery here.
                    # Offloaded -- real git/filesystem work, would
                    # otherwise block the event loop.
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None, _cleanup_worktree, wt_path, branch, Path(project_path_str), logger
                    )
                else:
                    logger.warning(
                        f"[DELETE-FEATURE] {feature_id}'s worktree {wt_path} has no "
                        "launch_params.project_path to scope cleanup to -- left in place"
                    )
        except Exception as e:
            logger.warning(f"[DELETE-FEATURE] Failed to clean up worktree for {feature_id}: {e}")

    _invalidate("queue", "features", "status")
    return {"success": True, "feature_id": feature_id}

async def _spawn_agent_for_task(task_id: str, phase_id: Optional[str]) -> None:
    """Create an agent for a task, mirroring /api/create_agent_for_task in server.py."""
    from src.core.app_context import get_app_state
    from src.core.database import Task

    server_state = get_app_state()

    session = server_state.db_manager.get_session()
    try:
        task = session.query(Task).filter_by(id=task_id).first()
        if not task:
            logger.warning(f"[RESUME] Task {task_id} not found, cannot restart")
            return

        enriched_data = {}
        if task.enriched_description:
            enriched_data["enriched_description"] = task.enriched_description

        agent = await server_state.agent_manager.create_agent_for_task(
            task=task,
            enriched_data=enriched_data,
            memories=[],
            project_context="",
            agent_type="phase",
            use_existing_worktree=True,
            # Assign the task in the same commit as the Agent row itself,
            # before the slow worktree/tmux/prompt work -- otherwise a crash
            # in that window (e.g. a backend restart) leaves Agent.current_task_id
            # set but Task.assigned_agent_id permanently null. See
            # create_agent_for_task's assign_to_task docstring for the incident.
            assign_to_task=True,
        )
        logger.info(f"[RESUME] Restarted task {task_id[:8]} with agent {agent.id[:8]}")
    except Exception as e:
        logger.error(f"[RESUME] Failed to restart task {task_id[:8]}: {e}", exc_info=True)
        session.rollback()
        task = session.query(Task).filter_by(id=task_id).first()
        if task:
            task.status = "failed"
            task.failure_reason = f"Resume failed to spawn agent: {e}"
            session.commit()
    finally:
        session.close()

@router.get("/features/{feature_id}", response_model=FeatureDetail)
async def get_feature_detail(feature_id: str):
    cache_key = f"feature:{feature_id}"
    cached = _cached(cache_key, ttl=30.0)
    if cached is not None:
        return cached

    # via _shared: FEATURES_DIR is a mutable module global rebound by
    # configure_autopilot_api and by test fixtures; a from-import would bind
    # a stale copy at import time
    feature_dir = _safe_path(_shared.FEATURES_DIR, feature_id)
    if not feature_dir.exists() or not feature_dir.is_dir():
        raise HTTPException(404, f"Feature '{feature_id}' not found")

    report_path = feature_dir / "feature_report.html"
    metrics = _read_json(feature_dir / "docs" / "pipeline_metrics.json") or {}

    docs_dir = feature_dir / "docs"
    docs = []
    if docs_dir.exists():
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

    summaries = {}
    summary_files = {
        "requirements_summary": "requirements.md",
        "architecture_summary": "architecture.md",
        "security_summary": "security.md",
        "qa_summary": "qa.md",
        "product_validation_summary": "validation.md",
        "forensics_summary": "forensics.md",
    }
    for key, fname in summary_files.items():
        fpath = docs_dir / fname
        if fpath.exists():
            content = fpath.read_text(errors="replace")
            summaries[key] = content[:500] + ("..." if len(content) > 500 else "")

    dir_name = feature_dir.name
    name = dir_name.split("_", 1)[1].replace("_", " ").replace("-", " ").title() if "_" in dir_name else dir_name

    created_at = datetime.fromtimestamp(feature_dir.stat().st_mtime, tz=timezone.utc).isoformat()

    result = FeatureDetail(
        id=feature_dir.name,
        name=name,
        status=_feature_status(metrics),
        iterations=metrics.get("iterations", 0),
        total_time_seconds=metrics.get("total_time_seconds", 0),
        stop_reason=metrics.get("stop_reason", "unknown"),
        qa_passed=metrics.get("qa_passed", False),
        product_validated=metrics.get("product_validated", False),
        has_report=report_path.exists(),
        design_name=metrics.get("design_name", name),
        project_path=metrics.get("project_path", ""),
        feature_folder=metrics.get("feature_folder", str(feature_dir)),
        requirements_summary=summaries.get("requirements_summary", ""),
        architecture_summary=summaries.get("architecture_summary", ""),
        security_summary=summaries.get("security_summary", ""),
        qa_summary=summaries.get("qa_summary", ""),
        product_validation_summary=summaries.get("product_validation_summary", ""),
        forensics_summary=summaries.get("forensics_summary", ""),
        files_created=metrics.get("files_created", []),
        issues_resolved=metrics.get("issues_resolved", []),
        outstanding_issues=metrics.get("outstanding_issues", []),
        # Cost comes from the DB rollup, not the metrics file. No writer has
        # ever put a cost field in pipeline_metrics.json -- its writer emits
        # design_name / workflow_id / project_path / docs_dir / feature_folder
        # / completed_at / stop_reason / qa_passed / product_validated and
        # nothing else -- so metrics.get("cost_total", 0) silently returned 0
        # for every feature ever recorded. Workflow.cost_total_usd is the
        # authoritative value, maintained by cost_derivation from the
        # CostEntry ledger; reading it here fixes the response without
        # creating a second, staleable copy of the number in a JSON file.
        cost_total=_feature_record_cost(metrics.get("workflow_id")),
        cost_breakdown=metrics.get("cost_breakdown", {}),
        cost_currency=metrics.get("cost_currency", "USD"),
        created_at=created_at,
        docs=docs,
    )
    return _store(cache_key, result)

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
        candidate = Path(base_dir) / CONTEXT_DIR_NAME / "feature_report.html"
        if candidate.is_file():
            report_path = candidate
        else:
            candidate = Path(base_dir) / "docs" / "feature_report.html"
            if candidate.is_file():
                report_path = candidate
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
