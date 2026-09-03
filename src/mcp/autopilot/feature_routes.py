"""Feature routes: feature list/scan, pause/resume, delete, detail —
extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md
§3.2). Review flow lives in feature_review_routes.py; report/record/docs/
logs endpoints in feature_record_routes.py (size budget;
docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md §1)."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from src.mcp.autopilot import _shared
from src.mcp.autopilot._shared import (
    FeatureDetail,
    FeatureSummary,
    _cached,
    _feature_status,
    _invalidate,
    _read_json,
    _safe_path,
    _store,
)
from src.mcp.autopilot.feature_record_routes import (
    _feature_record_cost,
    _find_archived_feature_report,
    _resolve_feature_docs_base,
    _resolve_feature_record_report,
    _resolve_live_feature_report,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_SUMMARY_FILES = {
    "requirements_summary": "requirements.md",
    "architecture_summary": "architecture.md",
    "security_summary": "security.md",
    "qa_summary": "qa.md",
    "product_validation_summary": "validation.md",
    "forensics_summary": "forensics.md",
}


def _read_summaries(docs_dir: Path) -> Dict[str, str]:
    """First 500 chars of each phase's summary doc, keyed for FeatureDetail."""
    summaries = {}
    for key, fname in _SUMMARY_FILES.items():
        fpath = docs_dir / fname
        if fpath.exists():
            content = fpath.read_text(errors="replace")
            summaries[key] = content[:500] + ("..." if len(content) > 500 else "")
    return summaries


def _list_docs(docs_dir: Path) -> List[Dict[str, Any]]:
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
    return docs


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
            from src.core.database import AutopilotDesign, AutopilotProject, Feature, Workflow
            # Outer join, not inner: a Feature whose design row is missing
            # still belongs in the unscoped list (it just can't be attributed
            # to a project, so no project-scoped view will show it).
            db_features = (
                session.query(
                    Feature,
                    AutopilotDesign.project_id,
                    AutopilotDesign.name,
                    AutopilotDesign.designs_folder,
                )
                .outerjoin(AutopilotDesign, Feature.design_id == AutopilotDesign.id)
                .order_by(Feature.created_at.desc())
                .all()
            )
            for f, feature_project_id, design_name, designs_folder in db_features:
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
                        has_report = _resolve_live_feature_report(wf.working_directory) is not None
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
                if not has_report:
                    # Outside the workflow_id guard on purpose -- doc_review's
                    # report is filed under the design, so it is findable even
                    # for a feature whose workflow row is gone.
                    has_report = _resolve_feature_record_report(
                        designs_folder, f.design_id, f.feature_key
                    ) is not None

                created_at = f.created_at.isoformat() if f.created_at else ""

                features.append({
                    "id": f.id,
                    "project_id": feature_project_id,
                    "design_id": f.design_id,
                    "design_name": design_name,
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
async def list_features(project_id: Optional[str] = None):
    """project_id scopes the list to one project's features -- what the UI's
    Completed tab wants. Omitted, every project's features are returned, which
    other consumers still depend on. Filtering happens after the (shared,
    cached) scan so switching projects doesn't re-query the DB."""
    features = _scan_features()
    if project_id:
        return [f for f in features if f.get("project_id") == project_id]
    return features

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
        from src.core.app_context import get_app_state

        queue_service = get_app_state().queue_service
        for queued_task_id in queued_task_ids:
            queue_service.pause_queued_task(queued_task_id)

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

        # Resume workflow if paused or failed. "paused" goes through the
        # shared primitive (force=True: an explicit Resume click overrides
        # any pause reason, same as the pipeline-level resume endpoint) --
        # cascade_to_feature=False since this endpoint always sets
        # feature.status="active" itself, below, regardless of which
        # feature(s) share this workflow_id. "failed" isn't a pause state
        # at all, so it stays a direct write rather than going through a
        # pause-focused primitive.
        #
        # paused_by="review" is excluded from the force-resume call itself
        # (though the task-recovery below still runs) -- that pause means a
        # human decision (approve/request changes) is outstanding, and
        # review_feature (POST /features/{id}/review) is the only endpoint
        # that's supposed to clear it, since it's also the only one that
        # records feature.review_status/reviewed_at. Force-resuming through
        # it here meant clicking the generic Resume button (e.g. to recover
        # a stuck task while still under review) silently cleared the
        # review gate itself and let the pipeline -- and eventually the
        # design queue's next feature -- proceed with no approval ever
        # recorded. Confirmed live: feature feat-f47c93ba on workflow
        # ca539a75, resumed this way without review_status ever becoming
        # "approved".
        if wf.status == "paused" and wf.paused_by != "review":
            from src.autopilot.orchestrator.engine_client import resume_workflow as _resume_workflow_primitive
            _resume_workflow_primitive(workflow_id, force=True, cascade_to_feature=False, session=db)
        elif wf.status == "failed":
            wf.status = "active"
            wf.paused_by = None
            # Clear a stale arbitration/pause reason -- otherwise it lingers
            # and reads as an ongoing problem even after the user has
            # manually resolved it and resumed.
            wf.status_reason = None
            # The phase whose retry cap failure took the workflow down is
            # itself invisible to every dispatch case once its own
            # PhaseExecution is "failed" (see reset_failed_phase_executions'
            # own docstring) -- reset it or the phase can run to completion
            # again and again without this workflow/feature ever being able
            # to derive "completed".
            from src.autopilot.orchestrator.engine_client import reset_failed_phase_executions
            reset_failed_phase_executions(workflow_id, session=db)

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
        Agent,
        AgentResult,
        BoardConfig,
        CostEntry,
        DiagnosticRun,
        Feature,
        Memory,
        Phase,
        PhaseExecution,
        PhasePromptVersion,
        PromptProposal,
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
        # Captured now, before Phase/Workflow rows are deleted below --
        # used to rotate/invalidate this workflow's CLI sessions after the
        # delete commits (see the cleanup block near the end of this
        # function).
        session_infos: List[dict] = []
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
            from src.autopilot.phases import capture_workflow_session_info

            session_infos = capture_workflow_session_info(db, [workflow_id])

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
                    # AgentResult before ValidationReview -- agent_results.
                    # verified_by_validation_id is an enforced FK to
                    # validation_reviews.id, set by ResultService's normal
                    # task-validation flow for any validated task. Same bug,
                    # same fix, as remove_project_design/delete_project's
                    # identical cascade (design_file_routes.py/project_
                    # routes.py) -- confirmed there via a real FOREIGN KEY
                    # error before those fixes, never propagated here.
                    db.query(AgentResult).filter(AgentResult.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(ValidationReview).filter(ValidationReview.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Memory).filter(Memory.related_task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Ticket).filter(Ticket.task_id.in_(task_ids)).delete(synchronize_session=False)
                    # CostEntry.task_id/workflow_id are also enforced FKs -- a
                    # feature that ever recorded real LLM cost (the common
                    # case, not the exception) would otherwise fail to delete.
                    db.query(CostEntry).filter(CostEntry.task_id.in_(task_ids)).delete(synchronize_session=False)
                    # Agent.current_task_id -> tasks.id is also an enforced
                    # FK -- a belt-and-suspenders null-out alongside the
                    # termination above (repair_service.py's rerun does the
                    # same): an agent that crashed/was killed without going
                    # through the normal terminate path (which clears this)
                    # can leave it dangling at one of these tasks, failing
                    # the Task delete below.
                    db.query(Agent).filter(Agent.current_task_id.in_(task_ids)).update(
                        {"current_task_id": None}, synchronize_session=False
                    )

                db.query(DiagnosticRun).filter(DiagnosticRun.workflow_id == workflow_id).delete(synchronize_session=False)
                # workflows.result_id -> workflow_results.id is also an
                # enforced FK, and WorkflowResultService sets it to a
                # WorkflowResult with the SAME workflow_id (a self-
                # reference), common for has-result pipelines (bugfix/
                # diagnostic). Null the self-reference before deleting
                # WorkflowResult below, or that delete fails -- same bug,
                # same fix, as remove_project_design/delete_project's
                # identical cascade.
                db.query(Workflow).filter_by(id=workflow_id).update(
                    {"result_id": None}, synchronize_session=False
                )
                db.query(WorkflowResult).filter(WorkflowResult.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(BoardConfig).filter(BoardConfig.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(Ticket).filter(Ticket.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(CostEntry).filter(CostEntry.workflow_id == workflow_id).delete(synchronize_session=False)
                # prompt_proposals.workflow_id also FKs to workflows.id, no
                # ondelete clause -- forensics_analysis (a real phase in the
                # standard autopilot workflow) creates these after a
                # pipeline run finishes. Same bug, same fix, as remove_
                # project_design/delete_project's identical cascade.
                db.query(PromptProposal).filter(PromptProposal.workflow_id == workflow_id).delete(synchronize_session=False)

                # tasks.phase_id and tickets.phase_id both FK to phases.id
                # -- Task must be deleted (Ticket already was, above) before
                # Phase, not after, or DELETE FROM phases fails the same
                # FK-violation way the original workflow_execution_id bug did.
                db.query(Task).filter(Task.workflow_id == workflow_id).delete(synchronize_session=False)

                phase_ids = [p.id for p in db.query(Phase.id).filter(Phase.workflow_id == workflow_id).all()]
                if phase_ids:
                    db.query(PhaseExecution).filter(PhaseExecution.phase_id.in_(phase_ids)).delete(synchronize_session=False)
                    db.query(PhasePromptVersion).filter(PhasePromptVersion.phase_id.in_(phase_ids)).delete(synchronize_session=False)
                db.query(Phase).filter(Phase.workflow_id == workflow_id).delete(synchronize_session=False)

            # features.workflow_id also FKs to workflows.id -- the Feature
            # row itself (still pointing at workflow_id here) must be gone
            # before DELETE FROM workflows runs, or that statement fails
            # the same FK-violation way phases/tasks did above. flush()
            # forces the ORM delete to hit the DB now rather than at the
            # transaction's final commit, which would be too late.
            db.delete(feature)
            if workflow_id:
                db.flush()
                db.query(Workflow).filter_by(id=workflow_id).delete(synchronize_session=False)
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

    # Best-effort CLI session cleanup -- see cleanup_workflow_sessions'
    # docstring (src/autopilot/phases.py) for why this is needed even
    # though get_session_id is now workflow-scoped.
    if session_infos:
        try:
            from src.autopilot.phases import cleanup_workflow_sessions

            removed = cleanup_workflow_sessions(session_infos)
            if removed:
                logger.info(f"[DELETE-FEATURE] Removed {removed} orphaned CLI session file(s) for workflow {workflow_id}")
        except Exception as e:
            logger.warning(f"[DELETE-FEATURE] Failed to clean up CLI sessions for {feature_id}: {e}")

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

def _db_feature_detail(feature_id: str) -> Optional[FeatureDetail]:
    """FeatureDetail for a Feature Model row (feat-<uuid>), or None when no
    such row exists.

    Every id /features lists is a Feature row id, but get_feature_detail
    only ever resolved a FEATURES_DIR *directory name* -- the legacy
    single-feature pipeline's id -- so every click through from the gallery
    404'd. Reads through the same _resolve_feature_docs_base chain the
    feature-records endpoints use, so this Overview and the modal's Docs
    tab describe the same directory.
    """
    from src.core.database import AutopilotDesign, AutopilotProject, Feature, Workflow, get_db
    from src.core.status_derivation import derive_feature_status

    with get_db() as db:
        feat = db.query(Feature).filter_by(id=feature_id).first()
        if not feat:
            return None

        wf = db.query(Workflow).filter_by(id=feat.workflow_id).first() if feat.workflow_id else None
        status = feat.status
        if wf:
            derived = derive_feature_status(db, feat.id, write_back=False)
            if derived:
                status = derived

        design = db.query(AutopilotDesign).filter_by(id=feat.design_id).first() if feat.design_id else None
        base_dir = _resolve_feature_docs_base(wf) if wf else None

        # Same project-base derivation as _scan_features: the project row
        # first, launch_params only as a fallback.
        project_base = None
        if wf and wf.project_id:
            proj = db.query(AutopilotProject).filter_by(id=wf.project_id).first()
            project_base = proj.base_dir if proj else None
        if not project_base and wf and isinstance(wf.launch_params, dict):
            project_base = wf.launch_params.get("project_path")

        name = feat.name or feat.feature_key or feat.id
        design_name = design.name if design else name
        design_id = feat.design_id
        designs_folder = design.designs_folder if design else None
        feature_key = feat.feature_key
        workflow_id = feat.workflow_id
        cost_total = feat.cost_total_usd or 0.0
        created_at = feat.created_at.isoformat() if feat.created_at else ""
        # The pipeline never wrote a duration anywhere; the feature row's own
        # start/finish timestamps are the only real source for it.
        duration = (
            int((feat.completed_at - feat.started_at).total_seconds())
            if feat.started_at and feat.completed_at
            else 0
        )

    docs_dir = Path(base_dir) / "docs" if base_dir else None
    metrics = (_read_json(docs_dir / "pipeline_metrics.json") or {}) if docs_dir else {}

    summaries = _read_summaries(docs_dir) if docs_dir else {}

    report = _resolve_live_feature_report(base_dir) if base_dir else None
    if report is None and project_base and workflow_id:
        report = _find_archived_feature_report(project_base, workflow_id)
    if report is None:
        report = _resolve_feature_record_report(designs_folder, design_id, feature_key)

    return FeatureDetail(
        id=feature_id,
        name=name,
        status=status,
        iterations=metrics.get("iterations", 0),
        total_time_seconds=duration,
        stop_reason=metrics.get("stop_reason") or ("completed" if status == "completed" else "unknown"),
        qa_passed=bool(metrics.get("qa_passed")),
        product_validated=bool(metrics.get("product_validated")),
        has_report=report is not None,
        design_name=design_name,
        project_path=project_base or "",
        feature_folder=base_dir or "",
        requirements_summary=summaries.get("requirements_summary", ""),
        architecture_summary=summaries.get("architecture_summary", ""),
        security_summary=summaries.get("security_summary", ""),
        qa_summary=summaries.get("qa_summary", ""),
        product_validation_summary=summaries.get("product_validation_summary", ""),
        forensics_summary=summaries.get("forensics_summary", ""),
        files_created=metrics.get("files_created", []),
        issues_resolved=metrics.get("issues_resolved", []),
        outstanding_issues=metrics.get("outstanding_issues", []),
        cost_total=cost_total,
        cost_breakdown=metrics.get("cost_breakdown", {}),
        cost_currency=metrics.get("cost_currency", "USD"),
        created_at=created_at,
        docs=_list_docs(docs_dir) if docs_dir else [],
    )


@router.get("/features/{feature_id}", response_model=FeatureDetail)
async def get_feature_detail(feature_id: str):
    cache_key = f"feature:{feature_id}"
    cached = _cached(cache_key, ttl=30.0)
    if cached is not None:
        return cached

    db_detail = _db_feature_detail(feature_id)
    if db_detail is not None:
        return _store(cache_key, db_detail)

    # via _shared: FEATURES_DIR is a mutable module global rebound by
    # configure_autopilot_api and by test fixtures; a from-import would bind
    # a stale copy at import time
    feature_dir = _safe_path(_shared.FEATURES_DIR, feature_id)
    if not feature_dir.exists() or not feature_dir.is_dir():
        raise HTTPException(404, f"Feature '{feature_id}' not found")

    report_path = feature_dir / "feature_report.html"
    metrics = _read_json(feature_dir / "docs" / "pipeline_metrics.json") or {}

    docs_dir = feature_dir / "docs"
    docs = _list_docs(docs_dir)
    summaries = _read_summaries(docs_dir)

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
