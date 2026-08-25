"""Design rerun and repair orchestration.

Split out of src/mcp/autopilot/queue_routes.py (SOLID review 1.11): this is
substantive business logic -- stopping/resetting a design's state and
restarting its pipeline, spinning up a recovery review agent for stuck
tasks -- that belongs next to orchestrator.py, not in a route handler.

`load_queue_order`/`save_queue_order`/`invalidate` are injected by the
caller (queue_routes.py's own local helpers + `_shared._invalidate`)
rather than imported here: this module lives in src/autopilot/, and
nothing in src/autopilot/ imports from src/mcp/ anywhere else in this
codebase -- importing them directly would be a new, backwards layering
dependency for the sake of 3 small file-based queue-order helpers that are
also used by 3 unrelated routes (list/reorder/requeue) in that same file.
"""

import asyncio
import json
import logging
import os
import signal
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException

from src.core.constants import (
    AUTOPILOT_STATE_DIR,
    DESIGN_CONTEXT_SUBDIR,
    DESIGN_WORKFLOW_DEFINITION_IDS,
)
from src.prompts.loader import get_prompt

logger = logging.getLogger(__name__)


class RepairService:
    """Rerun and repair a design's pipeline."""

    async def rerun(
        self,
        project_path: str,
        filename: str,
        load_queue_order: Callable[[Optional[str]], List[str]],
        save_queue_order: Callable[[List[str], Optional[str]], None],
        invalidate: Callable[..., None],
    ) -> Dict[str, Any]:
        """Rerun a design: stop everything, move to front, start pipeline."""
        from src.core.database import (
            Agent,
            AutopilotDesign,
            AutopilotProject,
            Feature,
            Task,
            Workflow,
            get_db,
        )

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
                    # await asyncio.sleep, not time.sleep: this is an async
                    # route, and the loop below blocked the entire event loop
                    # for up to 5s (plus 0.5s after SIGKILL) on every rerun --
                    # freezing dashboard reads, agent check-ins, and task
                    # completions server-wide for the duration.
                    for _ in range(10):
                        await asyncio.sleep(0.5)
                        try:
                            os.kill(pid, 0)  # Check if alive
                        except ProcessLookupError:
                            break
                    try:
                        os.kill(pid, signal.SIGKILL)
                        await asyncio.sleep(0.5)  # Give OS time to clean up
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
                        from src.autopilot.orchestrator.engine_client import terminate_agent

                        for agent in db.query(Agent).filter(Agent.id.in_(stuck_agent_ids)).all():
                            terminate_agent(agent.id, session=db)

                    from src.autopilot.orchestrator.engine_client import pause_workflow
                    for wf in db.query(Workflow).filter(Workflow.id.in_(design_wf_ids), Workflow.status == "active").all():
                        # These rows are deleted moments later (Step 2b below),
                        # so this pause is short-lived -- migrated for
                        # consistency with the requeue path above, not because
                        # the auto-resume race is a practical concern here.
                        # Same "user" flavor as requeue_design's above: a
                        # short-lived technical guard, not a standing /stop-
                        # style pause -- see that call site's note.
                        pause_workflow(wf.id, reason="user", session=db)

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
                PhasePromptVersion,
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
                        # Agent.current_task_id -> tasks.id is also an
                        # enforced FK -- an agent that crashed/was killed
                        # without going through the normal terminate path
                        # (which clears this) can leave it dangling at one
                        # of these tasks, failing the Task delete below.
                        db.query(Agent).filter(Agent.current_task_id.in_(task_ids)).update(
                            {"current_task_id": None}, synchronize_session=False
                        )

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
                        db.query(PhasePromptVersion).filter(PhasePromptVersion.phase_id.in_(phase_ids)).delete(synchronize_session=False)

                    # Delete phases -- Phase.workflow_id is a NOT NULL FK to
                    # workflows.id, so leaving these behind (as this always
                    # did) made the Workflow delete below fail with a
                    # FOREIGN KEY constraint error every time.
                    db.query(Phase).filter(Phase.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                    # AutopilotDesign.phase0_workflow_id -> workflows.id is
                    # also an enforced FK. Regression, found live: this was
                    # never cleared here, so the Workflow delete below
                    # failed with a FOREIGN KEY constraint violation --
                    # caught by this function's outer except and logged,
                    # but silently swallowed, so "start pipeline" proceeded
                    # anyway on top of an unrolled-back OLD workflow whose
                    # worktree Step 2's cleanup had already removed. The
                    # orchestrator then got stuck resuming that now-
                    # unrecoverable workflow forever (~3s/cycle, 0 designs
                    # processed), never dispatching anything new -- exactly
                    # what "the Rerun button does nothing" looked like.
                    if design and design.phase0_workflow_id in wf_ids:
                        design.phase0_workflow_id = None

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
                    # _cleanup_worktree does real git/filesystem work --
                    # offloaded so it doesn't block the event loop.
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None, _cleanup_worktree, wt_path, branch, Path(project_path_str), logger
                    )
                except Exception as e:
                    logger.warning(f"[RERUN] Failed to clean up worktree {working_directory}: {e}")
        except Exception as e:
            logger.error(f"Error cleaning up design state for rerun: {e}")

        # Step 3: Clean up branches (non-blocking)
        try:
            from src.core.app_context import get_app_state
            from src.core.worktree_manager import WorktreeManager

            # A fresh WorktreeManager instance is deliberate here (not the
            # shared server_state.branch_manager) -- .reload(project) below
            # points it at an arbitrary project, and reload()ing the shared
            # long-lived instance would race with any other concurrent request
            # relying on it pointing at a different project. Only db_manager
            # itself should be the shared instance (see SOLID review 1.12).
            db_manager = get_app_state().db_manager
            bm = WorktreeManager(db_manager, repo_path=project)
            # Run cleanup in background thread to not block pipeline start
            import threading

            thread = threading.Thread(target=lambda: bm.cleanup_all_stale_branches(), daemon=True)
            thread.start()
        except Exception as e:
            logger.error(f"Error starting branch cleanup: {e}")

        # Step 4: Move design to front of queue
        order = load_queue_order(rerun_start_project_id)
        if filename in order:
            order.remove(filename)
        order.insert(0, filename)
        save_queue_order(order, rerun_start_project_id)
        invalidate("queue", f"queue:{rerun_start_project_id}")

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

        invalidate("status")

        return {
            "rerun": True,
            "filename": filename,
            "workflow_id": new_workflow_id,
            "message": f"Pipeline restarted for {filename}",
        }

    async def repair(self, project_path: str, filename: str) -> Dict[str, Any]:
        """Repair a design: spin up a recovery workflow and a review agent that checks
        and fixes stuck/incomplete tasks. (Branch reconciliation is obsolete under
        per-task worktree isolation — failed worktrees are discarded, never merged.)"""
        logger.info("[REPAIR] Received repair request")

        project = Path(project_path).resolve()
        if not project.exists():
            raise HTTPException(400, f"Project path does not exist: {project_path}")

        # Generate repair ID for tracking
        repair_id = str(uuid.uuid4())[:8]

        # Run repair in background thread pool (not async - uses sync subprocess calls)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, self._run_repair, repair_id, filename, project)

        return {
            "repair_id": repair_id,
            "status": "started",
            "message": f"Repair started for {filename}. Check /api/autopilot/queue/repair/{repair_id} for results.",
        }

    def _spawn_repair_review_agent(self, wf_id: str, filename: str, project: Path, reason: str, actions_taken: list):
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

    def _run_repair(self, repair_id: str, filename: str, project: Path):
        """Background repair task."""
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
            self._spawn_repair_review_agent(wf_id, filename, project, "Repair initiated", actions_taken)
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

    def get_repair_status(self, repair_id: str) -> Dict[str, Any]:
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
            result: Dict[str, Any] = json.loads(result_file.read_text())
            result["status"] = "completed"
            logger.info(f"[REPAIR] {repair_id} completed")
            return result
        except Exception as e:
            logger.error(f"[REPAIR] {repair_id} error reading results: {e}")
            return {"repair_id": repair_id, "status": "error", "message": str(e)}


repair_service = RepairService()
