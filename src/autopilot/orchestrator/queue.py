"""Design-queue scanning, picking, and status."""

import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

from src.autopilot.orchestrator.engine_client import (
    file_hash,
    get_agents,
    get_tasks,
    get_workflow_status,
)
from src.autopilot.orchestrator.state import (
    DesignEntry,
    _get_project_context,
    _set_project_context,
)
from src.core.constants import (
    CONTEXT_DIR_NAME,
    DESIGN_CONTEXT_SUBDIR,
    DESIGN_QUEUE_FALLBACK_DIR,
    DIAGNOSTIC_TASK_PREFIX,
)
from src.core.database import (
    Phase,
    PhaseExecution,
    Workflow,
    get_db,
)

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)


MAX_DESIGN_RETRIES = 3  # max times a failed design is auto-retried


def is_design_fully_complete(workflow_id: str, logger: "OrchestratorLogger") -> Tuple[bool, str]:
    """Check if a design is fully complete:
    1. Workflow DB status is completed (or no active agents/tasks remain)
    2. No active agents
    3. All agent branches merged to main

    Returns:
        (is_complete, reason) tuple
    """
    # Check workflow status — if the server already marked it completed, trust that.
    # Also use derive_workflow_status for the "all tasks done ≠ all phases done"
    # check — this mistake has recurred independently at least four times.
    wf = get_workflow_status(workflow_id)
    wf_status = wf.get("status", "")
    if wf_status == "completed":
        return True, "Workflow status: completed"
    if wf_status not in ("active", "paused"):
        return False, f"Workflow status: {wf_status}"

    # Use derive_workflow_status to check if the workflow is actually done.
    # This replaces a hand-rolled "all tasks done + all phases completed"
    # check that was missing the phase-completeness gate.
    from src.core.database import get_db
    from src.core.status_derivation import derive_workflow_status
    with get_db() as db:
        derived = derive_workflow_status(db, workflow_id, write_back=False)
    if derived == "completed":
        return True, "All tasks done and all phases completed (derived)"

    # Check task statuses
    pending = get_tasks(status="pending", workflow_id=workflow_id)
    queued = get_tasks(status="queued", workflow_id=workflow_id)
    in_progress = get_tasks(status="in_progress", workflow_id=workflow_id)
    assigned = get_tasks(status="assigned", workflow_id=workflow_id)
    failed = get_tasks(status="failed", workflow_id=workflow_id)
    done = get_tasks(status="done", workflow_id=workflow_id)

    # Pending/active tasks indicate real work remaining.
    # Ignore DIAGNOSTIC tasks (created by the monitor itself when stuck) — they
    # should not block completion detection.
    real_pending = [t for t in (pending + queued + in_progress + assigned) if not (t.get("raw_description") or "").startswith(DIAGNOSTIC_TASK_PREFIX)]
    if real_pending:
        task_ids = [t.get("id", "")[:8] for t in real_pending[:3]]
        return False, f"{len(real_pending)} task(s) still active: {', '.join(task_ids)}"

    # Failed tasks: only block if the same phase has NO subsequent done task
    # (i.e., a retry succeeded → the failure is resolved).
    done_phase_ids = {t.get("phase_id") for t in done if t.get("phase_id")}
    unresolved_failures = [t for t in failed if t.get("phase_id") not in done_phase_ids and not (t.get("raw_description") or "").startswith(DIAGNOSTIC_TASK_PREFIX)]
    if unresolved_failures:
        task_ids = [t.get("id", "")[:8] for t in unresolved_failures[:3]]
        return (
            False,
            f"{len(unresolved_failures)} unresolved failed task(s): {', '.join(task_ids)}",
        )

    # Check for active agents
    agents = get_agents(workflow_id=workflow_id)
    active_agents = [a for a in agents if a.get("status") in ("working", "starting", "idle")]
    if active_agents:
        agent_ids = [a.get("id", "")[:8] for a in active_agents[:3]]
        return (
            False,
            f"{len(active_agents)} agent(s) still active: {', '.join(agent_ids)}",
        )

    # Check for unmerged agent branches
    try:
        from src.core.database import ProjectRepo

        # Get project_id from workflow
        project_id = None
        try:
            from src.core.database import Workflow, get_db

            with get_db() as _db:
                _wf = _db.query(Workflow).filter_by(id=workflow_id).first()
                if _wf:
                    project_id = _wf.project_id
        except Exception as e:
            logger.warning(f"Failed to get workflow for branch check: {e}")

        if not project_id:
            return False, "Cannot determine project for branch check"

        # Iterate all ProjectRepos for the project (BLOCKER-3 fix)
        with get_db() as _db:
            repos = _db.query(ProjectRepo).filter_by(project_id=project_id).all()
            if not repos:
                return False, "No ProjectRepos found for project"

            for repo in repos:
                if not Path(repo.path).exists():
                    continue
                result = subprocess.run(
                    ["git", "branch", "--list", "agent-*"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=repo.path,
                )
                if result.returncode == 0:
                    branches = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n") if b.strip()]
                    if branches:
                        return False, f"{len(branches)} unmerged agent branch(es) in {repo.label}"
    except Exception as e:
        logger.warning(f"Error checking branches: {e}")

    # Check done task count vs actual phase count (not hardcoded 10).
    try:
        from src.core.database import DatabaseManager as _DbM
        from src.core.database import Phase

        _db = _DbM()
        _s = _db.get_session()
        try:
            phase_count = _s.query(Phase).count()
        finally:
            _s.close()
    except Exception:
        phase_count = 11  # fallback for the autopilot pipeline
    if len(done) < phase_count:
        return False, f"Only {len(done)}/{phase_count} phases done"

    return True, "All phases done, branches merged"


def scan_design_queue(queue_dir: Path, processed_hashes: Set[str], extra_dirs: list = None) -> List[DesignEntry]:
    designs = []
    dirs = [queue_dir]
    # Also scan docs/spec-queue if it exists as a sibling of the primary queue.
    # queue_dir is typically <project>/.hephaestus/designs, so .parent.parent is
    # the project root. docs/spec-queue is the conventional fallback location.
    if extra_dirs:
        dirs.extend(extra_dirs)
    elif queue_dir.parent.parent.exists():
        fallback = queue_dir.parent.parent / DESIGN_QUEUE_FALLBACK_DIR
        if fallback != queue_dir and fallback.exists():
            dirs.append(fallback)
    for scan_dir in dirs:
        if not scan_dir.exists():
            continue

    for scan_dir in dirs:
        if not scan_dir.exists():
            continue
        for ext in ("*.md", "*.txt"):
            for filepath in sorted(scan_dir.glob(ext)):
                if filepath.is_dir():
                    continue
                content_hash = file_hash(filepath)
                if content_hash in processed_hashes:
                    # Self-heal: if the design is marked processed but its
                    # features are all pending (e.g. server crashed between
                    # marking processed and creating features), re-queue it.
                    try:
                        from src.core.database import (
                            AutopilotDesign as _AD,
                        )
                        from src.core.database import (
                            Feature as _Feat,
                        )
                        from src.core.database import (
                            get_db as _gdb,
                        )

                        with _gdb() as _db:
                            _des = _db.query(_AD).filter_by(content_hash=content_hash).first()
                            if _des:
                                _feats = _db.query(_Feat).filter_by(design_id=_des.id).all()
                                if not _feats or all(f.status == "pending" for f in _feats):
                                    logger.warning(f"[SELF-HEAL] Design {_des.name} is in processed_hashes but has no features or all pending — re-queuing")
                                    processed_hashes.discard(content_hash)
                                else:
                                    continue
                            else:
                                continue
                    except Exception:
                        continue
                name = filepath.stem.replace("_", " ").replace("-", " ").title()
                designs.append(
                    DesignEntry(
                        path=filepath,
                        name=name,
                        content_hash=content_hash,
                    )
                )

    # Check for manual reorder file — stored in .hephaestus/ (not in docs/spec/)
    order_file = queue_dir.parent.parent / CONTEXT_DIR_NAME / ".queue_order.json"
    if order_file.exists():
        try:
            saved_order = json.loads(order_file.read_text())
            # Create lookup by filename
            by_filename = {d.path.name: d for d in designs}
            # Order by saved order, then append any new files not in saved order
            ordered = []
            for fname in saved_order:
                if fname in by_filename:
                    ordered.append(by_filename.pop(fname))
            # Add remaining files (not in saved order) sorted by name
            ordered.extend(sorted(by_filename.values(), key=lambda d: d.path.name.lower()))
            return ordered
        except (json.JSONDecodeError, KeyError):
            pass  # Fall back to default sort

    designs.sort(key=lambda d: d.path.name.lower())
    return designs


def _has_resumable_active_design(project_id: Optional[str]) -> bool:
    """True if this project has an ACTIVE design with incomplete (not
    completed/skipped) features already on file.

    Used by run_continuous_pipeline's "workflow still active" gate to decide
    whether an unrelated design's still-running workflow should actually
    block picking up new work. It should only block a FRESH design's Phase 0
    -- run_single_workflow's default pause_existing=True terminates every
    other active workflow's agents project-wide (see its docstring), which
    is destructive if another design's agents are still genuinely working.
    Resuming an already-active design never reaches that path: run_phase0
    skips straight past Phase 0 when Feature rows already exist (Tier 1,
    see its docstring), and the feature dispatch that follows always passes
    pause_existing=False. So a design in this state is always safe to
    resume regardless of what else is active in the project.
    """
    try:
        from src.core.database import AutopilotDesign, Feature, get_db

        if not project_id:
            return False
        with get_db() as db:
            active_designs = db.query(AutopilotDesign).filter_by(project_id=project_id, status="active").all()
            for candidate in active_designs:
                incomplete = (
                    db.query(Feature)
                    .filter(Feature.design_id == candidate.id, Feature.status.notin_(["completed", "skipped"]))
                    .count()
                )
                if incomplete > 0:
                    return True
            return False
    except Exception:
        return False


def pick_next_design(
    queue_dir: Path,
    processed_hashes: Set[str],
    logger: "OrchestratorLogger",
    project_id: Optional[str] = None,
) -> Optional[DesignEntry]:
    """Pick the next design to process.

    Reads from DB (autopilot_designs) if available, falls back to file scan.
    Uses file_path column if available, falls back to filename-based path.

    project_id: which project's queue to pick from. Without this, two
    concurrent run_continuous_pipeline loops (one per project, see
    AutopilotServiceRegistry) would both resolve "the project" via the
    single global AutopilotProject.is_active flag -- whichever project
    most recently started/restarted -- and silently steal each other's
    designs the moment a second project starts. Falls back to is_active
    only when project_id isn't supplied (the standalone CLI path, which
    has no per-project registry and only ever runs one project at a time).
    """
    # Try DB-based queue first
    try:
        from src.core.database import (
            AutopilotDesign,
            AutopilotProject,
            Feature,
            Workflow,
            get_db,
        )

        with get_db() as db:
            # Find the target project: by id when given (concurrent,
            # per-project loop), else the single active project (standalone
            # CLI path).
            if project_id:
                project = db.query(AutopilotProject).filter_by(id=project_id).first()
            else:
                project = db.query(AutopilotProject).filter_by(is_active=True).first()
            if not project:
                logger.info("pick_next_design: no active project found")
                return None

            # Budget guard: skip project entirely if over budget
            from src.core.cost_derivation import check_budget_before_new_work

            if not check_budget_before_new_work(db, project.id):
                logger.info(
                    f"pick_next_design: project '{project.name}' ({project.id[:8]}) "
                    f"over budget (${project.cost_total_usd:.2f} >= ${project.cost_limit_usd:.2f}) — skipping"
                )
                return None

            logger.info(
                f"pick_next_design: searching project '{project.name}' ({project.id[:8]})"
            )

            # Resume support: prioritize a design that already finished
            # Phase 0 (status moved to "active") but still has incomplete
            # features over starting a brand new "pending" design. A design
            # in this state was checked FIRST here, before finishing this
            # loop looked at "pending" designs at all -- but the "pending"
            # query below always ran unconditionally FIRST, so any pending
            # design (however low-priority) always won, silently starting
            # a whole new design's Phase 0 while an active design sat with
            # unblocked, ready-to-run features it would never be given a
            # turn to finish. Observed live: a feature whose only blocking
            # dependency had just completed stayed unstarted indefinitely
            # because a second, unrelated design was next in queue-order.
            #
            # Snapshot the pending list BEFORE the active-design loop below
            # runs -- that loop can itself reset a design back to "pending"
            # (candidate.status = "pending", to retry after a failed
            # workflow), and a design reset like that must wait for a
            # FRESH pick_next_design call to be eligible, not be picked
            # right back up by the pending-fallback query at the bottom of
            # this same call as if it had been queued all along.
            pending_designs = db.query(AutopilotDesign).filter_by(project_id=project.id, status="pending").order_by(AutopilotDesign.ordinal, AutopilotDesign.filename).all()

            design = None
            active_designs = db.query(AutopilotDesign).filter_by(project_id=project.id, status="active").order_by(AutopilotDesign.ordinal, AutopilotDesign.filename).all()
            if active_designs:
                logger.info(f"pick_next_design: found {len(active_designs)} active design(s), checking for incomplete work before considering pending designs")
                for candidate in active_designs:
                    incomplete = (
                        db.query(Feature)
                        .filter(
                            Feature.design_id == candidate.id,
                            Feature.status.notin_(["completed", "skipped"]),
                        )
                        .count()
                    )

                    # Check if any associated workflow has failed — only consider
                    # workflows linked to incomplete features. A failed workflow
                    # that's orphaned (no feature links to it) or only linked to
                    # completed features should not block the design.
                    failed_wf = (
                        db.query(Workflow)
                        .join(Feature, Feature.workflow_id == Workflow.id)
                        .filter(
                            Workflow.design_id == candidate.id,
                            Workflow.status == "failed",
                            Feature.status.notin_(["completed", "skipped"]),
                        )
                        .first()
                    )
                    # Fallback: orphaned failed workflow (no feature links to it at all)
                    if not failed_wf:
                        orphaned_failed = (
                            db.query(Workflow)
                            .outerjoin(Feature, Feature.workflow_id == Workflow.id)
                            .filter(
                                Workflow.design_id == candidate.id,
                                Workflow.status == "failed",
                                Feature.id.is_(None),
                            )
                            .first()
                        )
                        if orphaned_failed:
                            logger.info(f"  Ignoring orphaned failed workflow {orphaned_failed.id[:8]} (no features link to it)")
                            # Clear the design_id so it stops showing up
                            orphaned_failed.design_id = None
                            db.commit()

                    logger.info(f"  Active design '{candidate.name}' ({candidate.id[:8]}): incomplete={incomplete}, failed_wf={failed_wf.id[:8] if failed_wf else 'None'}, status={candidate.status}")

                    if incomplete > 0:
                        if failed_wf:
                            # Failed workflow with incomplete features — retry
                            retry_key = f"autopilot_retry_{candidate.id}"
                            retry_count = _get_project_context(db, retry_key) or 0
                            if retry_count >= MAX_DESIGN_RETRIES:
                                logger.info(
                                    f"Design {candidate.name} has failed workflow {failed_wf.id[:8]} and exceeded {MAX_DESIGN_RETRIES} retries ({retry_count}/{MAX_DESIGN_RETRIES}) — marking failed"
                                )
                                candidate.status = "failed"
                                # Surfaced by /autopilot/status's last_error so
                                # the UI can show a real popup instead of this
                                # silently sitting in a log file no one reads
                                # -- the pipeline itself keeps polling "queue
                                # empty" every 60s afterward, looking healthy,
                                # while having permanently given up on the
                                # only design in the queue.
                                candidate.error = f"Gave up after {MAX_DESIGN_RETRIES} retries: workflow {failed_wf.id[:8]} kept failing"
                                db.commit()
                                continue
                            logger.info(f"Design {candidate.name} has failed workflow {failed_wf.id[:8]} — resetting to pending for retry ({retry_count + 1}/{MAX_DESIGN_RETRIES})")
                            _set_project_context(db, retry_key, retry_count + 1)
                            candidate.status = "pending"
                            candidate.error = None  # clear any stale exhausted-retry message
                            db.commit()
                            continue
                        design = candidate
                        logger.info(f"Resuming active design {design.name} ({incomplete} feature(s) not yet complete)")
                        break
                    else:
                        # All features completed/skipped — but check
                        # if any workflow failed (e.g. diagnostic task).
                        # If so, retry instead of marking done.
                        if failed_wf:
                            retry_key = f"autopilot_retry_{candidate.id}"
                            retry_count = _get_project_context(db, retry_key) or 0
                            if retry_count >= MAX_DESIGN_RETRIES:
                                logger.info(
                                    f"Design {candidate.name} has all features done "
                                    f"but failed workflow {failed_wf.id[:8]} and "
                                    f"exceeded {MAX_DESIGN_RETRIES} retries "
                                    f"({retry_count}/{MAX_DESIGN_RETRIES}) — marking done"
                                )
                                candidate.status = "completed"
                                db.commit()
                                continue
                            logger.info(f"Design {candidate.name} has all features done but failed workflow {failed_wf.id[:8]} — retrying ({retry_count + 1}/{MAX_DESIGN_RETRIES})")
                            _set_project_context(db, retry_key, retry_count + 1)
                            candidate.status = "pending"
                            db.commit()
                            continue
                        # All features done, no failed workflows — mark done.
                        candidate.status = "completed"
                        db.commit()
                        logger.info(f"Design {candidate.name} has all features completed/skipped — marking done")

            if design is None:
                # No active design has resumable work -- safe to start the
                # next design that was already pending before this call
                # (the snapshot taken above, not a fresh query -- see its
                # comment for why a design the loop above just reset to
                # "pending" must not be picked up here).
                design = pending_designs[0] if pending_designs else None
                if design:
                    logger.info(f"pick_next_design: found pending design '{design.name}' ({design.id[:8]})")
                else:
                    logger.info("pick_next_design: no designs to process")

            if design:
                # Mark as processing
                design.status = "processing"
                db.commit()

                # Construct DesignEntry from DB record
                # Try file_path first, fall back to filename-based path
                design_path = None
                if design.file_path:
                    # Use file_path if available (absolute path)
                    design_path = Path(design.file_path)
                    if not design_path.exists():
                        logger.warning(f"file_path does not exist: {design_path}")
                        design_path = None

                if design_path is None:
                    # Fall back to filename-based path
                    # REQ-12: Resolve to primary ProjectRepo's path, not workspace root
                    from src.core.database import resolve_project_repo
                    try:
                        primary_repo = resolve_project_repo(db, project.id, None)
                        repo_path = Path(primary_repo.path)
                    except Exception:
                        repo_path = Path(project.base_dir)
                    design_path = repo_path / DESIGN_CONTEXT_SUBDIR / design.filename

                if design_path.exists():
                    entry = DesignEntry(
                        path=design_path,
                        name=design.name,
                        content_hash=design.content_hash or file_hash(design_path),
                        db_id=design.id,
                        file_path=str(design_path),
                    )
                    logger.info(f"Selected from DB: {design.name} (ordinal={design.ordinal})")
                    return entry
                else:
                    logger.warning(f"Design file not found: {design_path}")
    except Exception as e:
        logger.warning(f"DB queue read failed, falling back to file scan: {e}")

    # Fallback: file-based queue
    designs = scan_design_queue(queue_dir, processed_hashes)

    if not designs:
        return None

    logger.info(f"Found {len(designs)} pending design(s) in queue")
    for d in designs:
        logger.info(f"  - {d.name} ({d.path.name})")

    next_design = designs[0]
    logger.info(f"Selected: {next_design.name}")

    # Try to look up DB ID for file-scanned design, creating the row if it
    # doesn't exist yet (e.g. the project itself was just auto-created —
    # see AutopilotService.start — so no AutopilotDesign row was ever
    # created for this file under the new project_id). Leaving db_id as
    # None here isn't a safe no-op: _create_feature_records requires a
    # non-null design_id (NOT NULL constraint), so a design processed via
    # this fallback with no DB row would crash later with an
    # IntegrityError right after Phase 0 completes — observed live during
    # smoke testing.
    try:
        import uuid as _uuid

        from src.core.database import AutopilotDesign, AutopilotProject
        from src.core.database import get_db as _get_db

        with _get_db() as _db:
            if project_id:
                project = _db.query(AutopilotProject).filter_by(id=project_id).first()
            else:
                project = _db.query(AutopilotProject).filter_by(is_active=True).first()
            if project:
                db_design = _db.query(AutopilotDesign).filter_by(project_id=project.id, filename=next_design.path.name).first()
                if not db_design:
                    db_design = AutopilotDesign(
                        id=f"des-{_uuid.uuid4().hex[:12]}",
                        project_id=project.id,
                        filename=next_design.path.name,
                        name=next_design.name,
                        content_hash=next_design.content_hash,
                        file_path=str(next_design.path),
                        status="pending",
                    )
                    _db.add(db_design)
                    _db.flush()
                    logger.info(f"Auto-created AutopilotDesign row for {next_design.path.name} (none existed for project {project.id})")
                next_design.db_id = db_design.id
    except Exception as e:
        logger.warning(f"Could not link/create DB design row: {e}")

    return next_design


def _assess_run_health(
    project_path: Path,
    _exec_id: str,
    orchestrator_log_path: Path,
    logger: "OrchestratorLogger",
) -> dict:
    """Assess run health by checking orchestrator log and tmux logs for problems."""
    health: dict = {
        "clean": True,
        "goto_count": 0,
        "error_count": 0,
        "goto_events": [],
        "tmux_errors": [],
        "warnings": [],
    }

    # Count GOTOs from orchestrator log — informational only, not an error signal
    if orchestrator_log_path and orchestrator_log_path.exists():
        try:
            lines = orchestrator_log_path.read_text(errors="replace").splitlines()
            goto_lines = [line for line in lines if "[GOTO]" in line]
            decision_lines = [line for line in lines if "DECISION POINT" in line]
            health["goto_count"] = len(goto_lines)
            health["goto_events"] = goto_lines[-10:]
            health["decision_points"] = [line.strip() for line in decision_lines]
            # GOTOs are normal iteration — do NOT set clean=False for them
        except Exception as e:
            health["warnings"].append(f"Could not read orchestrator log: {e}")

    # Grep tmux logs for error patterns
    error_patterns = [
        "ERROR",
        "Traceback",
        "FAILED",
        "ModuleNotFoundError",
        "ImportError",
        "AssertionError",
        "pytest.*FAILED",
        "exit code 1",
    ]
    tmux_dir = project_path / CONTEXT_DIR_NAME / "tmux"
    total_errors = 0
    if tmux_dir.is_dir():
        for log_file in sorted(tmux_dir.glob("*.log")):
            try:
                text = log_file.read_text(errors="replace")
                hits = [ln.strip() for ln in text.splitlines() if any(p in ln for p in error_patterns)]
                if hits:
                    total_errors += len(hits)
                    health["tmux_errors"].append(
                        {
                            "file": log_file.name,
                            "count": len(hits),
                            "samples": hits[:3],
                        }
                    )
            except Exception:
                pass
    health["error_count"] = total_errors
    if total_errors > 0:
        health["clean"] = False

    if health["clean"]:
        logger.info("Run health: CLEAN (no GOTOs, no tmux errors)")
    else:
        logger.info(f"Run health: PROBLEMS DETECTED — gotos={health['goto_count']} tmux_errors={health['error_count']}")

    return health


def _update_design_status(
    design_id: Optional[str],
    status: str,
    logger: "OrchestratorLogger" = None,
    **kwargs,
) -> None:
    """Update a design's status in the database.

    Args:
        design_id: Design ID
        status: New status
        logger: Optional logger
        **kwargs: Additional fields to update
    """
    from src.core.database import AutopilotDesign

    with get_db() as db:
        design = db.query(AutopilotDesign).filter_by(id=design_id).first()
        if design:
            design.status = status
            for key, value in kwargs.items():
                if hasattr(design, key):
                    setattr(design, key, value)
                elif logger:
                    logger.warning(f"_update_design_status: unknown field {key!r} for AutopilotDesign")
            db.commit()
            if logger:
                logger.info(f"Updated design {design_id} status to {status}")


def _set_workflow_type(workflow_id: str, workflow_type: str) -> None:
    """Set the workflow type (design or feature).

    Args:
        workflow_id: Workflow ID
        workflow_type: Type of workflow
    """

    with get_db() as db:
        workflow = db.query(Workflow).filter_by(id=workflow_id).first()
        if workflow:
            workflow.workflow_type = workflow_type
            db.commit()


def _get_phase0_completion(design_id: Optional[str]) -> Optional[dict]:
    """Check whether Phase 0's workflow already completed for this design.

    Uses the same status-based idempotency concept PhaseManager.mark_phase_complete
    uses for every other phase (PhaseExecution.status == "completed"), anchored one
    level up at Workflow-existence since Phase 0 is necessarily its own Workflow row
    (it decomposes a design into N features, each of which then gets its own
    separate Workflow — Phase 0 can't be "phase order=0" of any of those, since it
    runs before they exist) rather than a Phase row inside a shared one.

    Checks the LAST phase (by order) rather than the first, since Phase 0 now
    runs two phases (Feature Architect, feature_review) — checking just the
    first Phase's PhaseExecution would report "completed" as soon as Feature
    Architect finished, before feature_review ever ran. Also requires the
    Workflow's own status to be "completed": a couple of generic,
    Phase-0-unaware code paths (WorkflowTerminationHandler.terminate_workflow,
    the admin POST /api/workflow-executions/{id}/complete endpoint) can mark
    a PhaseExecution "completed" as part of a forced/generic teardown without
    the workflow itself having reached a real "completed" state — requiring
    both catches a forced-complete that never actually ran feature_review.

    Returns designs_folder (NOT the workflow's own working_directory/worktree,
    which _cleanup_worktree removes once the workflow finishes) — run_phase0
    already persists AutopilotDesign.designs_folder before calling
    _create_feature_records specifically so this recovery path has somewhere
    durable to read features.json back from if that call never completed.

    Returns:
        {"workflow_id": ..., "designs_folder": ...} if completed, else None.
    """
    if not design_id:
        return None
    from src.core.database import (
        AutopilotDesign,
    )

    with get_db() as db:
        design = db.query(AutopilotDesign).filter_by(id=design_id).first()
        if not design or not design.phase0_workflow_id or not design.designs_folder:
            return None
        wf = db.query(Workflow).filter_by(id=design.phase0_workflow_id).first()
        if not wf or wf.status != "completed":
            return None
        last_phase = db.query(Phase).filter_by(workflow_id=wf.id).order_by(Phase.order.desc()).first()
        execution = db.query(PhaseExecution).filter_by(phase_id=last_phase.id).first() if last_phase else None
        if execution and execution.status == "completed":
            return {"workflow_id": wf.id, "designs_folder": design.designs_folder}
        return None
