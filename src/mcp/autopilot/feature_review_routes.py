"""Feature review routes: the review-mode toggle, the Phase-0 decomposition
review, and the main feature review (approve / request-changes) flow. —
split out of feature_routes.py (size budget;
docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md §1)."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.mcp.autopilot._shared import _extract_pr_url, _invalidate
from src.mcp.autopilot.feature_routes import _spawn_agent_for_task

logger = logging.getLogger(__name__)

router = APIRouter()


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
            # If the feature actually completed all phases before the
            # review gate, the derive_workflow_status check further below
            # is what flips wf.status to "completed" -- resume_workflow
            # just set it "active" above, and it's left that way here
            # rather than re-derived from a status/paused_by heuristic
            # that's tautologically true immediately after every approval.
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
