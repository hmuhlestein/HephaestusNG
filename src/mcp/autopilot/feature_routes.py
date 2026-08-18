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
    """Serve feature_review's adversarial review.md for a Phase 0 workflow.

    Same live-worktree-then-designs_folder fallback chain as
    get_workflow_feature_report, since review.md is copied to
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
        candidate = Path(working_directory) / CONTEXT_DIR_NAME / "review.md"
        if candidate.is_file():
            review_path = candidate

    if review_path is None and phase0_designs_folder:
        candidate = Path(phase0_designs_folder) / "review.md"
        if candidate.is_file():
            review_path = candidate

    if review_path is None:
        raise HTTPException(404, "Review not found")
    return {"name": "review.md", "content": review_path.read_text(errors="replace")}

def _scan_features() -> List[Dict[str, Any]]:
    cached = _cached("features", ttl=30.0)
    if cached is not None:
        return cached

    from src.core.database import DatabaseManager
    from src.core.status_derivation import derive_feature_status

    features = []
    try:
        db_manager = DatabaseManager(None)
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
                Task.status.in_(["pending", "queued", "assigned", "in_progress"]),
            )
            .all()
        )
        for task in active_tasks:
            if task.assigned_agent_id:
                agent = db.query(Agent).filter_by(id=task.assigned_agent_id).first()
                if agent and agent.status in ("working", "starting", "idle"):
                    agent.status = "terminated"
                    agent.current_task_id = None
                    agent.terminated_at = datetime.utcnow()
                    # Invariant: all three fields together (see terminate_agent).
            task.status = "blocked"

        wf.status = "paused"
        # Same marker /autopilot/stop sets -- without it, the self-heal
        # sweep's _try_auto_resume_paused_workflow silently un-pauses this
        # feature again within one sweep tick (~20-30s), the same bug the
        # pipeline-level pause button had.
        wf.paused_by = "user"
        feature.status = "paused"
        db.commit()
        return {
            "success": True,
            "message": f"Paused feature {feature.name} ({len(active_tasks)} task(s) blocked)",
        }

@router.post("/features/{feature_id}/resume")
async def resume_feature(feature_id: str):
    """Resume a paused or failed feature: recover blocked, failed, and errored tasks."""
    from src.core.database import Agent, Feature, Task, Workflow, get_db

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

        # Resume workflow if paused or failed
        if wf.status in ("paused", "failed"):
            wf.status = "active"
            wf.paused_by = None
            # Clear a stale arbitration/pause reason -- otherwise it lingers
            # and reads as an ongoing problem even after the user has
            # manually resolved it and resumed.
            wf.status_reason = None

        # Recover blocked/failed tasks, plus any task still marked
        # assigned/in_progress whose agent was terminated (errored/orphaned
        # rather than cleanly failed) — pressing resume should retry all of these.
        candidates = (
            db.query(Task)
            .filter(
                Task.workflow_id == workflow_id,
                Task.status.in_(["blocked", "failed", "assigned", "in_progress"]),
            )
            .all()
        )
        restartable = []
        for t in candidates:
            if t.status in ("blocked", "failed"):
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
    for task_id, phase_id in to_restart:
        asyncio.create_task(_spawn_agent_for_task(task_id, phase_id))

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
            wf.status = "active"
            wf.paused_by = None
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
        # Skipping the review.md/feature_report.html rewrite would leave the
        # review modal showing the pre-redo synopsis and findings forever.
        feedback_prompt = (
            f"## Human Review Feedback\n\n{req.feedback.strip()}\n\n"
            "Re-decompose the design taking the above feedback into account. "
            "Update .hephaestus/features.json and each feature's scope.md accordingly.\n\n"
            "Then, in this same task, perform the adversarial feature-review pass "
            "yourself: compare the revised decomposition against the design document "
            "the same way the feature_review phase does, and rewrite "
            ".hephaestus/review.md and .hephaestus/feature_report.html so both "
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
                Task.status.in_(["blocked", "failed", "assigned", "in_progress", "pending"]),
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
                done_definition="Feature decomposition revised per human feedback, review.md and feature_report.html rewritten to match",
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
    asyncio.create_task(_spawn_agent_for_task(task_id, phase_id))

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

    from src.core.database import Feature, Task, Workflow, get_db

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
            wf.status = "active"
            wf.paused_by = None
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

            # In review mode, git_commit_push created a PR but didn't merge.
            # Merge it now that the feature is approved.
            pr_url = feature.pr_url or _extract_pr_url(db, wf.id, {})
            if pr_url:
                import subprocess
                try:
                    # Try gh pr merge first
                    result = subprocess.run(
                        ["gh", "pr", "merge", pr_url, "--merge"],
                        capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode == 0:
                        logger.info(f"[REVIEW] Merged PR {pr_url} after approval")
                    else:
                        logger.warning(f"[REVIEW] gh pr merge failed: {result.stderr}")
                except Exception as e:
                    logger.warning(f"[REVIEW] Failed to merge PR: {e}")

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

        # Find restartable tasks, or create a new one if all are done
        candidates = (
            db.query(Task)
            .filter(
                Task.workflow_id == workflow_id,
                Task.status.in_(["blocked", "failed", "assigned", "in_progress"]),
            )
            .all()
        )
        restartable = []
        for t in candidates:
            if t.status in ("blocked", "failed"):
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
    for task_id, phase_id in to_restart:
        logger.info(f"[REVIEW] Spawning agent for task {task_id} (phase {phase_id})")
        asyncio.create_task(_spawn_agent_for_task(task_id, phase_id))

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
                    _cleanup_worktree(wt_path, branch, Path(project_path_str), logger)
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
        cost_total=metrics.get("cost_total", 0),
        cost_breakdown=metrics.get("cost_breakdown", {}),
        cost_currency=metrics.get("cost_currency", "USD"),
        created_at=created_at,
        docs=docs,
    )
    return _store(cache_key, result)

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
