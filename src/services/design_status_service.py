"""Design status aggregation.

Extracted verbatim from src/mcp/autopilot/project_routes.py's
get_project_design_status (SOLID review docs/SOLID_OO_REVIEW.md finding 1.8):
the route handler kept request validation (project/filename lookup, 404/400),
this module owns the actual status-aggregation logic so it's usable outside
the HTTP layer.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import or_

from src.core.constants import (
    CONTEXT_DIR_NAME,
    DESIGN_WORKFLOW_DEFINITION_IDS,
    GOTO_REASON_PREFIX,
    PHASE0_DEFINITION_IDS,
)
from src.core.database import (
    Agent,
    AgentBranch,
    AgentLog,
    AutopilotDesign,
    Feature,
    Phase,
    Task,
    Workflow,
    get_db,
)
from src.mcp.autopilot._shared import _extract_pr_url
from src.mcp.autopilot.feature_record_routes import (
    _find_archived_feature_report,
    _resolve_feature_record_report,
    _resolve_live_feature_report,
)


def _resolve_latest_agent_per_task(db, task_ids) -> Dict[str, Agent]:
    """Batch-resolve each task's MOST RECENT agent -- via AgentLog's
    durable "created" record (details.task_id), not Task.assigned_agent_id.

    assigned_agent_id gets cleared on termination/failure (the documented
    invariant -- see database.py's Agent.current_task_id comment) and
    reassigned on every retry, so a task that finished after several CLI
    fallbacks (e.g. claude -> pi -> a local pi model) either shows no cli_type
    at all once it's done/failed, or shows whichever agent happened to be
    assigned at read time rather than the one that actually did the work.
    AgentLog's "created" entries survive every reassignment, so the latest
    one is the correct source for "what CLI actually ran this task."
    """
    task_ids = [t for t in task_ids if t]
    if not task_ids:
        return {}

    logs = (
        db.query(AgentLog)
        .filter(
            AgentLog.log_type == "created",
            AgentLog.details["task_id"].as_string().in_(task_ids),
        )
        .order_by(AgentLog.timestamp)
        .all()
    )
    # SQLite JSON extraction via as_string() can miss rows (see
    # task_service.py's identical fallback for the same query shape) --
    # re-check in Python if the indexed query came up empty.
    if not logs:
        logs = [
            log
            for log in db.query(AgentLog).filter(AgentLog.log_type == "created").all()
            if log.details and log.details.get("task_id") in task_ids
        ]
        logs.sort(key=lambda log: log.timestamp)

    # Ascending timestamp order means the last write for a given task_id
    # wins -- exactly "most recent agent".
    latest_agent_id_by_task: Dict[str, str] = {}
    for log in logs:
        task_id = (log.details or {}).get("task_id")
        if task_id and log.agent_id:
            latest_agent_id_by_task[task_id] = log.agent_id

    agent_ids = list(set(latest_agent_id_by_task.values()))
    agents_by_id = {a.id: a for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all()} if agent_ids else {}

    return {
        task_id: agents_by_id[agent_id]
        for task_id, agent_id in latest_agent_id_by_task.items()
        if agent_id in agents_by_id
    }


def _design_row(db, project_id: str, design_id: Optional[str], filename: Optional[str]):
    """The design row, by id when the caller has one. filename is only a
    fallback for callers that predate id addressing -- it is NULL for a
    directory-sourced design and non-unique as an address in general."""
    if design_id:
        return db.query(AutopilotDesign).filter_by(project_id=project_id, id=design_id).first()
    if filename:
        return db.query(AutopilotDesign).filter_by(project_id=project_id, filename=filename).first()
    return None


async def get_design_status(
    project_id: str,
    filename: Optional[str],
    base_dir: str,
    design_content: str,
    design_name: str,
    design_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate workflow/task/feature status for one design document.

    Caller (the route handler) has already resolved project_id/design_id into
    base_dir/design_content/design_name. filename is whatever the row carries
    -- None for a directory-sourced design -- and is used only for the legacy
    workflow match below and for echoing back.
    """
    # Find all workflows that processed this design. Workflow.design_id is the
    # real link and is what a Spec Kit design matches on: its filename is a
    # synthetic key ("speckit/<repo>/<n>-<slug>.md") that appears nowhere in
    # launch_params, which records the actual spec.md path -- so the LIKE
    # alone found nothing for one, and its status read as "never ran". The
    # LIKE stays as a fallback because design_id was added later and is still
    # NULL on older rows (19 of 53 autopilot workflows here), whose history
    # would otherwise disappear from this view.
    with get_db() as db:
        match_clauses = []
        if design_id:
            match_clauses.append(Workflow.design_id == design_id)
        if filename:
            match_clauses.append(Workflow.launch_params.like(f"%{filename}%"))
        if not match_clauses:
            matching_workflows = []
        else:
            matching_workflows = (
                db.query(Workflow)
                .filter(
                    Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                    or_(*match_clauses),
                )
                .order_by(Workflow.created_at.desc())
                .all()
            )

        # Self-heal each matched workflow's own status before using it below --
        # derive_workflow_status is the centralized "did every phase actually
        # finish" check (unlike the coarse task-status heuristics further
        # down this endpoint), so a workflow that got marked "completed"
        # prematurely (e.g. a goto-limit-exceeded forced "continue" that
        # skipped starting the next phase) gets corrected back to "active"
        # here on every poll, the same way Feature/Design status already
        # self-heal.
        from src.core.status_derivation import derive_workflow_status

        for wf in matching_workflows:
            derive_workflow_status(db, wf.id, write_back=True)

        # Get tasks and agents for all matching workflows
        all_tasks = []
        all_agents = []
        workflow_ids = [wf.id for wf in matching_workflows]

        # Build phase name lookup
        phase_map = {}
        if workflow_ids:
            phases = db.query(Phase).filter(Phase.workflow_id.in_(workflow_ids)).all()
            phase_map = {p.id: p.name for p in phases}

        if workflow_ids:
            tasks = db.query(Task).filter(Task.workflow_id.in_(workflow_ids)).order_by(Task.created_at).all()

            # Bulk-fetch each task's latest agent (not necessarily the
            # currently-assigned one -- see _resolve_latest_agent_per_task).
            latest_agent_by_task = _resolve_latest_agent_per_task(db, [t.id for t in tasks])

            for t in tasks:
                agent = latest_agent_by_task.get(t.id)
                all_tasks.append(
                    {
                        "id": t.id,
                        "description": (t.enriched_description or t.raw_description or "")[:200],
                        "status": t.status,
                        "failure_reason": t.failure_reason,
                        "priority": t.priority,
                        "phase_id": t.phase_id,
                        "phase_name": phase_map.get(t.phase_id),
                        "workflow_id": t.workflow_id,
                        # "Z" suffix required: these are naive datetimes that
                        # ARE utc (see the utc-only invariant), but plain
                        # .isoformat() on a naive datetime carries no
                        # timezone marker at all -- the frontend's
                        # `new Date(iso_string)` then parses it as LOCAL
                        # time, not UTC. On a host whose local timezone
                        # trails UTC, that makes the parsed timestamp look
                        # HOURS in the future relative to real now(),
                        # producing a large negative "elapsed" display.
                        "created_at": t.created_at.isoformat() + "Z" if t.created_at else None,
                        "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
                        "agent_id": t.assigned_agent_id,
                        "agent_status": agent.status if agent else None,
                        "cli_type": agent.cli_type if agent else None,
                        "cost_total_usd": t.cost_total_usd or 0.0,
                    }
                )

            # Get agent IDs for branch info - check both task.assigned_agent_id and agents.current_task_id
            agent_ids = list(set(t.assigned_agent_id for t in tasks if t.assigned_agent_id))
            # Also get agents assigned to these tasks via agents.current_task_id
            task_ids = [t.id for t in tasks]
            if task_ids:
                assigned_agents = db.query(Agent).filter(Agent.current_task_id.in_(task_ids)).all()
                for a in assigned_agents:
                    if a.id not in agent_ids:
                        agent_ids.append(a.id)

            if agent_ids:
                worktrees = db.query(AgentBranch).filter(AgentBranch.agent_id.in_(agent_ids)).all()
                for wt in worktrees:
                    all_agents.append(
                        {
                            "agent_id": wt.agent_id,
                            "branch_name": wt.branch_name,
                            "status": wt.merge_status,
                        }
                    )

            # Also include full agent details (not just branch info)
            agents_by_id = {a.id: a for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all()} if agent_ids else {}
            for agent_id in agent_ids:
                agent = agents_by_id.get(agent_id)
                if agent:
                    # Avoid duplicates
                    if not any(a.get("agent_id") == agent.id for a in all_agents):
                        all_agents.append(
                            {
                                "agent_id": agent.id,
                                "status": agent.status,
                                "current_task_id": agent.current_task_id,
                                "last_activity": agent.last_activity.isoformat() + "Z" if agent.last_activity else None,
                                "cli_model": agent.cli_model,
                                "agent_type": agent.agent_type,
                            }
                        )

        # Determine overall status — prefer the design-level status from
        # autopilot_designs (set by run_design_aggregate / continuous pipeline)
        # over workflow-level heuristics, because workflow statuses may include
        # retries, gotos, or partial failures that don't reflect final outcome.
        _design_id = None
        _design_raw_error = None
        _design_workflow_type = "feature"
        _design_designs_folder = None
        with get_db() as _db:
            _design = _design_row(_db, project_id, design_id, filename)
            if _design:
                from src.core.status_derivation import derive_design_status

                # H-3: use the centralized, self-healing derivation (feature
                # rollup) instead of the raw column, which is only ever
                # written by run_design_aggregate at the very end of a run.
                design_status = derive_design_status(_db, _design.id, write_back=True)
                _design_id = _design.id
                _design_raw_error = _design.error
                _design_workflow_type = _design.workflow_type
                _design_designs_folder = _design.designs_folder
            else:
                design_status = None

        # A live 'active'/'paused' workflow signal must win over the coarser
        # design_status field — that field is only updated by run_design_aggregate
        # at the end of a full pipeline run, so it never reflects a workflow
        # being paused mid-run. Without this, design_status stays 'active'
        # forever after a pause, the pause/resume button never flips to
        # 'resume', and clicking pause looks like it did nothing.
        #
        # BUT matching_workflows is deliberately broad (LIKE-matched on the
        # bare design filename), so it also catches every OTHER feature's
        # workflow that happened to originate from the same design document
        # -- a design gets re-run once per decomposed feature, and each
        # feature's own workflow references the same design_document path
        # in its launch_params. A workflow whose OWN linked Feature has
        # already reached completed/skipped is not a live in-flight run no
        # matter what its own (potentially stale, never-cleaned-up)
        # Workflow.status says -- trusting it here made the WHOLE design
        # look permanently "Active". Observed live: BACKEND_DESIGN.md's
        # Credit Management System feature completed 2026-07-29 but its
        # workflow (f1b3c0e0) never got its status flipped from "active",
        # so every later feature's design-status view showed a permanent
        # spinner even after the design (and every feature) genuinely
        # finished.
        _feature_status_by_wf = {}
        _wf_ids_for_feature_check = [wf.id for wf in matching_workflows]
        if _wf_ids_for_feature_check:
            with get_db() as _db:
                for feat in _db.query(Feature).filter(Feature.workflow_id.in_(_wf_ids_for_feature_check)).all():
                    _feature_status_by_wf[feat.workflow_id] = feat.status
        # A workflow with NO Feature row pointing to it at all is orphaned
        # -- but only once some OTHER workflow for this same design DOES
        # have one: a design's very first workflow (budget-blocked before
        # _create_feature_records ever ran, e.g.) legitimately has no
        # Feature yet and must still count. _any_feature_linked is what
        # distinguishes "superseded by a newer, linked workflow" (a
        # bootstrap-race duplicate -- see _relink_features_to_workflows's
        # own fix for the bugfix-typed-feature version of this) from
        # "hasn't gotten far enough to create one yet". Without this
        # distinction, _feature_status_by_wf.get(wf.id) returns None for
        # BOTH cases, and `None not in ("completed", "skipped")` is True --
        # the opposite of this filter's intent, silently RE-INCLUDING every
        # orphaned duplicate instead of excluding it alongside genuinely-
        # completed ones. Observed live: a feature's canonical workflow
        # completed while two superseded duplicate workflows (created
        # before the bootstrap-race fix, no Feature linking to either)
        # still sat "paused" -- the design's top-level status reported
        # "paused" indefinitely even though the one workflow anything
        # actually routes through was done.
        _any_feature_linked = bool(_feature_status_by_wf)

        def _is_superseded_orphan(wf) -> bool:
            return wf.id not in _feature_status_by_wf and _any_feature_linked

        _wf_statuses = [
            wf.status
            for wf in matching_workflows
            if not _is_superseded_orphan(wf) and _feature_status_by_wf.get(wf.id) not in ("completed", "skipped")
        ]
        _non_orphaned_workflows = [wf for wf in matching_workflows if not _is_superseded_orphan(wf)]
        if any(s == "active" for s in _wf_statuses):
            overall_status = "active"
        elif _wf_statuses and any(s == "paused" for s in _wf_statuses):
            overall_status = "paused"
        elif design_status and design_status not in ("pending", "unknown"):
            overall_status = design_status
        elif not matching_workflows:
            overall_status = "pending"
        else:
            if all(s == "completed" for s in _wf_statuses):
                overall_status = "completed"
            elif any(s == "failed" for s in _wf_statuses):
                overall_status = "failed"
            else:
                overall_status = _wf_statuses[0] if _wf_statuses else "unknown"

        # Only surface the stored error while the design is actually
        # failed -- _design.error isn't cleared when a design is re-run
        # successfully (or reset to pending), so showing it unconditionally
        # would leak a stale message from a previous failed attempt onto a
        # design that's since recovered.
        design_error = _design_raw_error if overall_status == "failed" else None

        # Surface *why* a paused workflow is paused -- "paused" alone is
        # ambiguous between a user-initiated pause and a budget-enforcement
        # pause, and the latter is the one users most need to notice.
        design_paused_by = None
        design_status_reason = None
        if overall_status == "paused":
            # Same orphan exclusion as _wf_statuses above -- otherwise this
            # can surface a superseded duplicate workflow's stale
            # paused_by/status_reason even when the actual, currently-
            # linked workflow is the one legitimately paused (or not
            # paused at all).
            paused_wf = next((wf for wf in _non_orphaned_workflows if wf.status == "paused" and wf.paused_by), None)
            if paused_wf:
                design_paused_by = paused_wf.paused_by
                design_status_reason = paused_wf.status_reason

        # Find feature folder
        feature_folder = None
        for wf in matching_workflows:
            if wf.working_directory:
                features_dir = Path(wf.working_directory) / CONTEXT_DIR_NAME / "features"
                if features_dir.exists():
                    # filename is None for a directory-backed design, which
                    # can reach this endpoint now that it is addressed by id
                    # -- fall back to the design's own name, which is what
                    # the feature folder is named from anyway.
                    needle = (filename or design_name or "").replace(".md", "").lower()
                    for d in sorted(features_dir.iterdir(), reverse=True):
                        if needle and d.is_dir() and needle in d.name.lower():
                            feature_folder = str(d)
                            break
                if feature_folder:
                    break

        # Get branch names
        branch_names = list(set(a["branch_name"] for a in all_agents if a.get("branch_name")))

        # Get features linked to this design's workflows
        workflow_ids = [wf.id for wf in matching_workflows]
        features = []

        # Query decomposed features from the DB (created by Phase 0)
        if _design_id:
            db_features = db.query(Feature).filter_by(design_id=_design_id).all()
        else:
            db_features = []

        for feat in db_features:
            # Get tasks for this feature's workflow
            feat_tasks = []
            feat_wf_id = feat.workflow_id

            # If no workflow_id, try to match by feature_key in launch_params
            if not feat_wf_id and matching_workflows:
                import json as _json

                for wf in matching_workflows:
                    try:
                        params = wf.launch_params if isinstance(wf.launch_params, dict) else _json.loads(wf.launch_params or "{}")
                    except Exception:
                        continue
                    if params.get("feature_id") == feat.feature_key:
                        feat_wf_id = wf.id
                        break

            if feat_wf_id:
                wf_tasks = db.query(Task).filter_by(workflow_id=feat_wf_id).all()
                phase_ids = set(t.phase_id for t in wf_tasks if t.phase_id)
                phases_q = db.query(Phase).filter(Phase.id.in_(phase_ids)).all() if phase_ids else []
                phase_map = {p.id: p.name for p in phases_q}
                # Phase.description is config-sourced (each phase YAML's own
                # `description:`) -- exposed per task so the UI can show what
                # the phase actually does without re-deriving it from the
                # task's own free-text description (which also carries the
                # "Execute {phase}: " label and, for goto/retry tasks, the
                # GOTO_REASON_PREFIX block below).
                phase_description_map = {p.id: p.description for p in phases_q}
                # Bulk-fetch each task's latest agent (not necessarily the
                # currently-assigned one -- see _resolve_latest_agent_per_task).
                latest_agent_by_wf_task = _resolve_latest_agent_per_task(db, [t.id for t in wf_tasks])

                for t in wf_tasks:
                    agent = latest_agent_by_wf_task.get(t.id)
                    agent_status = agent.status if agent else None
                    agent_cli_type = agent.cli_type if agent else None
                    # The full (untruncated) text -- goto_reason is parsed
                    # out of this, not the 200-char-truncated `description`
                    # below, since a long phase description could otherwise
                    # push the reason past the truncation point.
                    full_description = t.enriched_description or t.raw_description or ""
                    goto_reason = None
                    if GOTO_REASON_PREFIX in full_description:
                        goto_reason = full_description.split(GOTO_REASON_PREFIX, 1)[1].split("\n", 1)[0].strip()
                    feat_tasks.append(
                        {
                            "id": t.id,
                            "description": full_description[:200],
                            "phase_description": phase_description_map.get(t.phase_id),
                            "goto_reason": goto_reason,
                            # Once the task is finished, its own outcome is more
                            # useful to show than goto_reason/phase_description
                            # (both describe why the task was dispatched, not
                            # what it actually did) -- the frontend prefers
                            # these when status is done/failed.
                            "completion_notes": t.completion_notes,
                            "failure_reason": t.failure_reason,
                            "status": t.status,
                            "action": t.action or "",
                            "action_target_phase": t.action_target_phase or None,
                            "phase_id": t.phase_id,
                            "phase_name": phase_map.get(t.phase_id),
                            "workflow_id": t.workflow_id,
                            # "Z" suffix required -- see the sibling task-list
                            # builder above for why: a naive-but-UTC datetime
                            # serialized without a timezone marker gets
                            # misparsed as local time by the frontend's
                            # `new Date(...)`, producing a large negative
                            # "elapsed" display on hosts behind UTC.
                            "created_at": t.created_at.isoformat() + "Z" if t.created_at else None,
                            "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
                            "agent_id": t.assigned_agent_id,
                            "agent_status": agent_status,
                            "cli_type": agent_cli_type,
                            "cost_total_usd": t.cost_total_usd or 0.0,
                        }
                    )

            # Use centralized status derivation (H-3 fix)
            from src.core.status_derivation import derive_feature_status

            feat_status = derive_feature_status(db, feat.id, write_back=True)

            # doc_review.yaml's feature_report.html shows up here as soon as
            # that phase writes it -- PhaseManager._populate_feature_folder
            # only archives a copy to the features gallery at FULL workflow
            # completion (2 phases later), so checking the live worktree is
            # what lets the report surface right after doc_review finishes
            # instead of only once the whole 12-phase pipeline is done.
            has_report = False
            if feat_wf_id:
                feat_wf = next((wf for wf in matching_workflows if wf.id == feat_wf_id), None)
                # Only show report if doc_review phase has completed
                # (prevents showing stale reports from previous runs)
                from src.core.database import Phase as _Phase
                doc_review_phase = db.query(_Phase).filter_by(
                    workflow_id=feat_wf_id, name="doc_review"
                ).first()
                if doc_review_phase:
                    doc_review_done = db.query(Task).filter(
                        Task.phase_id == doc_review_phase.id,
                        Task.status == "done",
                    ).first()
                    if doc_review_done:
                        if feat_wf and feat_wf.working_directory:
                            has_report = _resolve_live_feature_report(feat_wf.working_directory) is not None
                        if not has_report:
                            # working_directory is null/gone once the feature's
                            # worktree is cleaned up on full completion (see
                            # _cleanup_worktree) -- the live-worktree checks
                            # above go permanently False at that point even
                            # though PhaseManager._populate_feature_folder
                            # already archived a durable copy to the features
                            # gallery first.
                            has_report = _find_archived_feature_report(base_dir, feat_wf_id) is not None
                        if not has_report:
                            # Neither the live worktree nor the archived
                            # features gallery has it: doc_review may instead
                            # have filed the report under the design's OWN
                            # storage folder (features/<feature_key>/), which
                            # a multi-repo feature (working_directory rooted in
                            # a child repo, never copied into the gallery
                            # PhaseManager writes under the workspace root)
                            # never reaches through either check above. Same
                            # resolver /feature-records/{id}/docs and the
                            # Completed tab's _scan_features already use.
                            has_report = _resolve_feature_record_report(
                                _design_designs_folder, _design_id, feat.feature_key
                            ) is not None

            features.append(
                {
                    "id": feat.id,
                    "name": feat.name,
                    "feature_key": feat.feature_key,
                    "workflow_id": feat.workflow_id,
                    "status": feat_status,
                    "scope": feat.scope or "",
                    "tasks": feat_tasks,
                    "depends_on": feat.depends_on or [],
                    "created_at": feat.created_at.isoformat() + "Z" if feat.created_at else None,
                    "completed_at": feat.completed_at.isoformat() + "Z" if feat.completed_at else None,
                    "has_report": has_report,
                    "cost_total_usd": feat.cost_total_usd or 0.0,
                    "pr_url": _extract_pr_url(db, feat_wf_id, phase_map) if feat_wf_id else None,
                    # Review mode fields
                    "review_pending": (
                        feat_wf_id is not None
                        and any(
                            wf.id == feat_wf_id and wf.paused_by == "review"
                            for wf in matching_workflows
                        )
                    ),
                    "review_status": getattr(feat, "review_status", None),
                    "review_feedback": getattr(feat, "review_feedback", None),
                }
            )

        # Feature Architect (Phase 0) pseudo-feature: it decomposes the design
        # into the Feature rows above, but is itself a separate Workflow (see
        # docs/LOOP_ENGINEERING_REVIEW.md -- a Feature:Workflow is 1:1, so
        # Phase 0 can't be phase order=0 within one of them; it must be its
        # own workflow that runs BEFORE those exist). That made it invisible
        # here: nothing surfaced its live task/agent while it was running, so
        # this list only ever showed a static "pending" placeholder or the
        # real decomposed features, with no way to watch Phase 0 itself.
        # Build a feature-shaped entry from its actual task/agent data (using
        # the same shape as real features above) so FeatureRow renders it
        # identically -- including the clickable agent-id link per task.
        phase0_workflows = [wf for wf in matching_workflows if wf.definition_id in PHASE0_DEFINITION_IDS]
        if phase0_workflows:
            phase0_wf = phase0_workflows[0]  # most recent (matching_workflows is desc-ordered)
            phase0_tasks = [t for t in all_tasks if t["workflow_id"] == phase0_wf.id]
            if phase0_tasks:
                # Paused-for-review wins over the task-derived status: every
                # task is genuinely "done" at this point (decomposition +
                # review both finished), which would otherwise read as
                # "completed" -- indistinguishable from a design that
                # skipped review entirely. Mirrors how a real Feature row's
                # status is set to "paused" by _pause_feature_for_review.
                if phase0_wf.paused_by == "review":
                    phase0_status = "paused"
                else:
                    phase0_status = (
                        "completed"
                        if all(t["status"] == "done" for t in phase0_tasks)
                        else "failed"
                        # "Orphaned:"-tagged failures are self-heal's own
                        # transient artifact (_create_phase_task marks a
                        # stale task failed, then immediately creates a
                        # fresh replacement in the same pass) -- not a
                        # genuine failure. Same class of bug fixed in
                        # status_derivation.py's derive_workflow_status/
                        # derive_feature_status: without this guard, a
                        # status poll landing in that split-second gap
                        # showed this design's Feature Architect card as
                        # "failed" for a task that was already being
                        # replaced.
                        if any(t["status"] == "failed" and not (t.get("failure_reason") or "").startswith("Orphaned:") for t in phase0_tasks)
                        else "active"
                        if any(t["status"] in ("assigned", "in_progress") for t in phase0_tasks)
                        else "pending"
                    )

                # has_report: the feature_review phase's HTML decomposition
                # synopsis. Check the live worktree first (still present
                # while paused for review -- Phase 0's own worktree isn't
                # cleaned up until AFTER the review gate clears), then the
                # design's durably-persisted designs_folder archive (see
                # run_phase0's synopsis_src copy) once it's gone.
                phase0_has_report = False
                if phase0_wf.working_directory:
                    phase0_has_report = (Path(phase0_wf.working_directory) / CONTEXT_DIR_NAME / "doc_review" / "feature_report.html").is_file() or \
                                        (Path(phase0_wf.working_directory) / CONTEXT_DIR_NAME / "feature_report.html").is_file()
                if not phase0_has_report:
                    phase0_design = _design_row(db, project_id, design_id, filename)
                    if phase0_design and phase0_design.designs_folder:
                        phase0_has_report = (Path(phase0_design.designs_folder) / "feature_report.html").is_file()

                features.insert(
                    0,
                    {
                        "id": f"phase0-{phase0_wf.id}",
                        "name": "Feature Architect",
                        "feature_key": "phase-0-decomposition",
                        "workflow_id": phase0_wf.id,
                        "status": phase0_status,
                        "scope": "Decomposes the design into the feature(s) below",
                        "tasks": phase0_tasks,
                        "created_at": phase0_wf.created_at.isoformat() + "Z" if phase0_wf.created_at else None,
                        "completed_at": None,
                        "cost_total_usd": phase0_wf.cost_total_usd or 0.0,
                        "has_report": phase0_has_report,
                        "review_pending": phase0_wf.paused_by == "review",
                        "review_status": None,
                        "review_feedback": None,
                    },
                )

        # Placeholder: if no DB features yet (and no Phase 0 activity to show
        # either), show a single pending feature so the UI has something to
        # display while waiting for Phase 0 to even start.
        if not features:
            features.append(
                {
                    "id": f"placeholder-{design_id or filename}",
                    "name": design_name or (filename or "").replace(".md", ""),
                    "feature_key": "pending-decomposition",
                    "status": "pending",
                    "scope": "Awaiting Phase 0 decomposition",
                    "tasks": [],
                    "created_at": None,
                    "completed_at": None,
                    "cost_total_usd": 0.0,
                }
            )

        # Collect workflow-level errors for failed workflows
        workflow_errors = []
        for wf in matching_workflows:
            if wf.status == "failed":
                wf_tasks = [t for t in all_tasks if t.get("workflow_id") == wf.id]
                failed_tasks = [t for t in wf_tasks if t.get("status") == "failed"]
                diag_failed = [t for t in failed_tasks if t.get("description", "").startswith("DIAGNOSTIC:")]
                real_failed = [t for t in failed_tasks if not t.get("description", "").startswith("DIAGNOSTIC:")]
                if real_failed:
                    workflow_errors.append(f"Workflow {wf.id[:8]}: {len(real_failed)} task(s) failed")
                elif diag_failed:
                    workflow_errors.append(f"Workflow {wf.id[:8]}: diagnostic task failed (all feature work completed)")
                else:
                    workflow_errors.append(f"Workflow {wf.id[:8]}: marked failed")

        # Build warning message for completed designs with failed workflows
        warning = None
        if overall_status == "completed" and workflow_errors:
            warning = f"Design completed but {len(workflow_errors)} workflow(s) had issues. " + "; ".join(workflow_errors)

        return {
            "filename": filename,
            "name": design_name,
            "content": design_content,
            "status": overall_status,
            "error": design_error,
            "warning": warning,
            "paused_by": design_paused_by,
            "status_reason": design_status_reason,
            "workflow_type": _design_workflow_type,
            "workflows": [
                {
                    "id": wf.id,
                    "status": wf.status,
                    "created_at": wf.created_at.isoformat() + "Z" if wf.created_at else None,
                    "error": next((e for e in workflow_errors if wf.id[:8] in e), None) if wf.status == "failed" else None,
                    "paused_by": wf.paused_by,
                    "status_reason": wf.status_reason,
                }
                for wf in matching_workflows
            ],
            "tasks": all_tasks,
            "agents": all_agents,
            "branches": branch_names,
            "feature_folder": feature_folder,
            "features": features,
            "cost_total_usd": sum(f["cost_total_usd"] for f in features),
        }

