"""Orchestrator state: data classes, project-context persistence, pipeline state."""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


from src.core.database import (
    ProjectContext,
    Workflow,
    get_db,
)


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class StopReason(Enum):
    COMPLETED = "completed"
    HARD_ERROR = "hard_error"
    IMPASSE = "impasse"
    ARCHITECTURAL_ISSUE = "architectural_issue"
    MAX_ITERATIONS = "max_iterations"
    CREDIT_EXHAUSTED = "credit_exhausted"
    USER_INTERRUPT = "user_interrupt"
    USER_SKIP = "user_skip"
    QUEUE_EMPTY = "queue_empty"


class DesignStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class DesignEntry:
    path: Path
    name: str
    content_hash: str
    status: DesignStatus = DesignStatus.PENDING
    db_id: Optional[str] = None  # autopilot_designs.id — links Workflow back to Design (§9.7)
    project_path: Optional[Path] = None
    feature_folder: Optional[Path] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    file_path: Optional[str] = None  # absolute path to design file
    designs_folder: Optional[Path] = None  # path to permanent storage


@dataclass
class IterationResult:
    iteration: int
    status: str
    qa_passed: bool
    product_validated: bool
    issues_found: List[str]
    fixes_applied: List[str]
    elapsed_seconds: int
    stop_reason: Optional[StopReason] = None


@dataclass
class FeatureReport:
    design_name: str
    project_path: str
    feature_folder: str
    design_document: str
    iterations: int
    total_time_seconds: int
    qa_passed: bool
    product_validated: bool
    stop_reason: str
    requirements_summary: str = ""
    architecture_summary: str = ""
    doc_review_summary: str = ""
    security_summary: str = ""
    qa_summary: str = ""
    product_validation_summary: str = ""
    forensics_summary: str = ""
    files_created: List[str] = field(default_factory=list)
    issues_resolved: List[str] = field(default_factory=list)
    outstanding_issues: List[str] = field(default_factory=list)
    cost_total: float = 0.0
    cost_breakdown: Dict[str, float] = field(default_factory=dict)
    cost_currency: str = "USD"


@dataclass
class PipelineState:
    designs_processed: int = 0
    designs_succeeded: int = 0
    designs_failed: int = 0
    total_elapsed: int = 0
    current_design: Optional[str] = None
    current_workflow_id: Optional[str] = None
    current_feature_folder: Optional[str] = None
    current_iteration: int = 0
    queue_status: Dict[str, str] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    run_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "designs_processed": self.designs_processed,
            "designs_succeeded": self.designs_succeeded,
            "designs_failed": self.designs_failed,
            "total_elapsed": self.total_elapsed,
            "current_design": self.current_design,
            "current_workflow_id": self.current_workflow_id,
            "current_feature_folder": self.current_feature_folder,
            "current_iteration": self.current_iteration,
            "queue_status": self.queue_status,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineState":
        state = cls()
        state.designs_processed = data.get("designs_processed", 0)
        state.designs_succeeded = data.get("designs_succeeded", 0)
        state.designs_failed = data.get("designs_failed", 0)
        state.total_elapsed = data.get("total_elapsed", 0)
        state.current_design = data.get("current_design")
        state.current_workflow_id = data.get("current_workflow_id")
        state.current_feature_folder = data.get("current_feature_folder")
        state.current_iteration = data.get("current_iteration", 0)
        state.queue_status = data.get("queue_status", {})
        state.run_id = data.get("run_id")
        return state


def _get_project_context(db, key: str):
    """Read a ProjectContext value by key, or None if unset."""
    row = db.query(ProjectContext).filter_by(key=key).first()
    return row.value if row else None


def _set_project_context(db, key: str, value) -> None:
    """Upsert a ProjectContext value by key.

    Uses SQLite's INSERT ... ON CONFLICT DO UPDATE (an atomic upsert)
    instead of a read-then-write. A naive filter_by(key=key).first() /
    add-or-update sequence has a real TOCTOU window: two callers writing
    the same key for the first time can both see no existing row and both
    attempt to insert, raising IntegrityError on ProjectContext.key's
    unique constraint for whichever commits second.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    now = datetime.utcnow()
    stmt = sqlite_insert(ProjectContext).values(key=key, value=value, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ProjectContext.key],
        set_={"value": value, "updated_at": now},
    )
    db.execute(stmt)


def _delete_project_context(db, key: str) -> None:
    """Remove a ProjectContext row by key, if present."""
    db.query(ProjectContext).filter_by(key=key).delete()


def _get_project_contexts_by_prefix(db, prefix: str) -> Dict[str, Any]:
    """Read every ProjectContext row whose key starts with `prefix`.

    Used for multi-project state that's namespaced per-project by key
    (e.g. _running_state_key(project_id)) rather than looked up by one
    known key -- see AutopilotService.enumerate_persisted_states.
    """
    rows = db.query(ProjectContext).filter(ProjectContext.key.like(f"{prefix}%")).all()
    return {row.key: row.value for row in rows}


def _resolve_project_id(project_path: str) -> Optional[str]:
    """Look up AutopilotProject.id for a project root path, read-only.

    Returns None if no AutopilotProject row exists yet for this path --
    callers that need one to exist should use _get_or_create_project_id
    instead.
    """
    from src.core.database import AutopilotProject

    try:
        with get_db() as db:
            proj = db.query(AutopilotProject).filter_by(base_dir=str(Path(project_path).resolve())).first()
            return proj.id if proj else None
    except Exception:
        return None


def _get_or_create_project_id(project_path: str) -> str:
    """Get-or-create + activate the AutopilotProject row for project_path,
    and resume any workflows the user had explicitly paused for it.

    Single source of truth for logic that used to live inline in
    AutopilotService.start() only -- extracted so callers that need
    project_id BEFORE starting a pipeline (e.g. POST /start's concurrency-
    cap check, AutopilotServiceRegistry.get_or_create) don't need a second,
    divergent copy of the same insert-if-missing/activate logic.
    """
    from src.autopilot.orchestrator.worktree_integration import _ensure_git_excluded
    import uuid as _uuid

    from sqlalchemy.exc import IntegrityError

    from src.core.database import AutopilotProject

    project = Path(project_path).resolve()
    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(base_dir=str(project)).first()
        if not proj:
            proj = AutopilotProject(
                id=f"proj-{_uuid.uuid4().hex[:12]}",
                name=project.name,
                base_dir=str(project),
                is_active=False,
            )
            db.add(proj)
            try:
                db.flush()
            except IntegrityError:
                # Lost a race with a concurrent first-time call for the
                # same brand-new path (base_dir is unique) -- the other
                # caller's row already exists; use it instead of failing.
                db.rollback()
                proj = db.query(AutopilotProject).filter_by(base_dir=str(project)).first()
            else:
                logger.info(f"Auto-created project '{proj.name}' for {project} (none registered)")
            _ensure_git_excluded(
                project,
                {
                    ".worktrees/": "Hephaestus's per-feature worktrees for this project --",
                    ".hephaestus/": "Hephaestus's own orchestration/scratch state for this project --",
                },
                logger,
            )

        if not proj.is_active:
            # Cap at max_concurrent_projects instead of evicting whatever
            # else is active -- this function is the actual pipeline-launch
            # path (POST /start, AutopilotService.start()), called BEFORE
            # /start's own AutopilotServiceRegistry.try_reserve() cap check.
            # The old unconditional eviction here defeated that cap
            # entirely: starting project B would silently deactivate
            # project A's is_active flag (and with it, the phase-
            # advancement sweep's coverage of A's workflows) regardless of
            # whether the cap was actually exceeded. Lenient like
            # projects_api.py's create_project: leave inactive rather than
            # raise here -- try_reserve() immediately after (for the
            # /start path) is the authoritative gate and already returns a
            # proper 409; a caller that isn't /start (e.g. rerun_design)
            # shouldn't hard-fail just because the cap is full elsewhere.
            from src.core.simple_config import get_config

            active_count = db.query(AutopilotProject).filter_by(is_active=True).count()
            max_concurrent = get_config().max_concurrent_projects
            if active_count < max_concurrent:
                proj.is_active = True
                logger.info(f"Activated project '{proj.name}' for pipeline")
            else:
                logger.warning(
                    f"Not activating project '{proj.name}': max_concurrent_projects "
                    f"({max_concurrent}) already reached"
                )

        # Clear the deliberate-pause marker /autopilot/stop sets
        # (Workflow.paused_by="user") for this project's workflows --
        # otherwise every self-heal/retry path that correctly skips
        # user-paused workflows would also skip them here, leaving a
        # workflow permanently stuck even after the user explicitly hits
        # play again.
        #
        # Gated on proj.is_active actually being True (either already was,
        # or was just set above): background_phase_advancement_sweep
        # (server.py) scopes its work to is_active projects only. Flipping
        # a workflow back to "active" while is_active stayed False (cap
        # reached above) would leave it permanently invisible to that
        # sweep -- it looks like it's running but nothing ever advances
        # it. Leave it paused instead; the next successful activation
        # (this function running again with room under the cap) resumes it.
        if proj.is_active:
            from src.autopilot.orchestrator.engine_client import resume_workflow

            user_paused = (
                db.query(Workflow)
                .filter(Workflow.project_id == proj.id, Workflow.paused_by == "user")
                .all()
            )
            # force=True: an explicit project re-activation overrides the
            # user pause, same as this function's pre-existing
            # unconditional clear -- looped per-row (rather than the
            # previous bulk .update()) so paused_at/status_reason clear
            # too (the previous bulk update left paused_at stale,
            # contradicting that column's own documented invariant) and
            # any linked Feature resumes along with its workflow.
            resumed = sum(1 for wf in user_paused if resume_workflow(wf.id, force=True, session=db))
            if resumed:
                logger.info(f"Resumed {resumed} user-paused workflow(s) for '{proj.name}'")

        db.commit()
        return proj.id


_RUNNING_STATE_KEY_PREFIX = "autopilot_running_pipeline_"


_RUNNING_STATE_KEY_LEGACY = "autopilot_running_pipeline"


def _running_state_key(project_id: str) -> str:
    return f"{_RUNNING_STATE_KEY_PREFIX}{project_id}"


class PersistentPipelineState:
    """Manages pipeline state that survives restarts.

    Backed by ProjectContext (a generic key-value table that already existed
    but had no callers) instead of JSON files under AUTOPILOT_STATE_DIR.
    Files were a second, non-transactional source of truth: when the tasks/
    agents/workflows tables got wiped in one DB transaction, these files
    didn't move, and kept pointing at a dead workflow_id until manually
    deleted as a separate step. Storing this in the same database means a
    reset of workflow state naturally carries this along with it.
    """

    STATE_KEY_PREFIX = "autopilot_pipeline_state_"
    PROCESSED_KEY_PREFIX = "autopilot_processed_designs_"
    # Pre-multi-project bare keys, shared by every project under the old
    # single-global scheme. Migrated in place onto this project's namespaced
    # keys the first time a project_id-aware caller loads state and finds
    # nothing under its own key yet -- see _migrate_legacy_state_if_present.
    STATE_KEY_LEGACY = "autopilot_pipeline_state"
    PROCESSED_KEY_LEGACY = "autopilot_processed_designs"

    def __init__(self, project_id: Optional[str] = None):
        # Without project_id, two concurrent run_continuous_pipeline loops
        # (one per project, see AutopilotServiceRegistry) would share these
        # SAME bare keys and clobber each other's processed-design tracking
        # and current-design/workflow pointer. Falls back to the legacy
        # bare keys when project_id isn't given (the standalone CLI path,
        # which only ever runs one project at a time).
        self.project_id = project_id
        if project_id:
            self.STATE_KEY = f"{self.STATE_KEY_PREFIX}{project_id}"
            self.PROCESSED_KEY = f"{self.PROCESSED_KEY_PREFIX}{project_id}"
        else:
            self.STATE_KEY = self.STATE_KEY_LEGACY
            self.PROCESSED_KEY = self.PROCESSED_KEY_LEGACY

    def _migrate_legacy_state_if_present(self) -> None:
        """One-time migration of the pre-multi-project bare keys onto this
        project's namespaced keys. Best-effort: assumes the legacy state
        belongs to whichever project_id-aware caller asks first, which
        holds at the moment this migration ships (only one project's
        pipeline could have been running under the old single-global
        scheme). Idempotent -- a no-op once the namespaced key exists.
        """
        try:
            with get_db() as db:
                if _get_project_context(db, self.STATE_KEY) is not None:
                    return
                legacy_state = _get_project_context(db, self.STATE_KEY_LEGACY)
                legacy_processed = _get_project_context(db, self.PROCESSED_KEY_LEGACY)
                if legacy_state is None and legacy_processed is None:
                    return
                logger.info(f"[MIGRATE] Namespacing legacy pipeline state to project {self.project_id}")
                if legacy_state is not None:
                    _set_project_context(db, self.STATE_KEY, legacy_state)
                    _delete_project_context(db, self.STATE_KEY_LEGACY)
                if legacy_processed is not None:
                    _set_project_context(db, self.PROCESSED_KEY, legacy_processed)
                    _delete_project_context(db, self.PROCESSED_KEY_LEGACY)
        except Exception as e:
            logger.warning(f"Failed to migrate legacy pipeline state: {e}")

    def save(self, state: PipelineState, processed_hashes: Set[str]):
        """Save pipeline state and processed designs to the DB.

        Write order: processed_designs first, in its OWN committed
        transaction, then state in a second one — two separate get_db()
        blocks, not one. This matters: a single shared transaction would be
        atomic (both writes land or neither does), which sounds safer but
        actually reintroduces the failure mode this ordering exists to
        avoid. If the process dies before a single shared commit, NEITHER
        write lands, so the design isn't marked processed and gets
        reprocessed (double-processed) on restart. With two separate
        transactions, a crash between them leaves processed_designs
        durably committed (design safely skipped next time) while state
        merely undercounts by 1 -- worse bookkeeping, not worse work.
        """
        try:
            with get_db() as db:
                _set_project_context(db, self.PROCESSED_KEY, list(processed_hashes))
        except Exception as e:
            logger.warning(f"Failed to save processed designs: {e}")

        self.save_state_only(state)

    def save_state_only(self, state: PipelineState) -> None:
        """Persist just the state half of save(), without touching
        processed_hashes.

        Used for an early, mid-run checkpoint right after
        state.current_workflow_id becomes known (see run_single_workflow),
        rather than only at the end of run_single_design. Without this, the
        status endpoint's current_design/current_workflow_id -- read from
        this same DB-persisted state, see get_autopilot_status -- stayed
        stale for a design's *entire* run (which can take minutes to
        hours): AutopilotService's own live current_design field is never
        updated (this runs in a separate thread with no reference back to
        it), so the status endpoint's fallback to this persisted state was
        the only path, and it wasn't written early enough. Observed live: a
        real agent was actively working (active_agents: 1) right after a
        design was launched, but the dashboard's play button, current
        design, and workflow id all still showed the previous, already-
        completed run -- indistinguishable from clicking play and nothing
        happening. Calling save(...) here instead (the full version) would
        also be safe but wastefully re-writes the unchanged processed_hashes
        set on every mid-run checkpoint.
        """
        state_data = state.to_dict()
        state_data["saved_at"] = datetime.utcnow().isoformat()
        try:
            with get_db() as db:
                _set_project_context(db, self.STATE_KEY, state_data)
        except Exception as e:
            logger.warning(f"Failed to save pipeline state: {e}")

    def load(self) -> Tuple[PipelineState, Set[str]]:
        """Load pipeline state and processed designs from the DB."""
        if self.project_id:
            self._migrate_legacy_state_if_present()

        state = PipelineState()
        processed_hashes: Set[str] = set()

        try:
            with get_db() as db:
                state_data = _get_project_context(db, self.STATE_KEY)
            if state_data:
                state = PipelineState.from_dict(state_data)
                logger.info(f"Loaded pipeline state: {state.designs_processed} designs processed")
        except Exception as e:
            logger.warning(f"Failed to load pipeline state: {e}")

        try:
            with get_db() as db:
                processed_list = _get_project_context(db, self.PROCESSED_KEY)
            if processed_list:
                processed_hashes = set(processed_list)
                logger.info(f"Loaded {len(processed_hashes)} processed designs")
        except Exception as e:
            logger.warning(f"Failed to load processed designs: {e}")

        return state, processed_hashes

    def clear(self):
        """Clear persisted state (for fresh start)."""
        with get_db() as db:
            _delete_project_context(db, self.STATE_KEY)
            _delete_project_context(db, self.PROCESSED_KEY)

    def has_incomplete_work(self) -> bool:
        """Check if there's incomplete work from a previous run."""
        try:
            with get_db() as db:
                state_data = _get_project_context(db, self.STATE_KEY)
            if not state_data:
                return False

            current_design = state_data.get("current_design")
            # `.get("queue_status", {})` only applies its default when the
            # key is ABSENT -- a stored value of `"queue_status": null`
            # (explicit None) returns None here, not {}, and would raise
            # AttributeError on the next .get() call below without the
            # `or {}` guard.
            queue_status = state_data.get("queue_status") or {}
            return current_design is not None or queue_status.get("status") == "processing"
        except Exception as e:
            logger.warning(f"Failed to read state for incomplete work check: {e}")
            return False

    def get_last_run_id(self) -> Optional[str]:
        """Get the run ID from the last persisted state."""
        try:
            with get_db() as db:
                state_data = _get_project_context(db, self.STATE_KEY)
            return state_data.get("run_id") if state_data else None
        except Exception as e:
            logger.warning(f"Failed to read state for last run ID: {e}")
            return None

    def remove_processed_hash(self, design_hash: str) -> None:
        """Remove a single hash from the processed-designs set, touching
        ONLY that key -- not the pipeline state.

        Callers that only need to un-mark one design (e.g. re-adding a
        deleted design so it gets reprocessed) must not go through
        load()+save(): save() rewrites STATE_KEY too, and a load()...save()
        round trip captures a snapshot of pipeline state that a
        concurrently-running pipeline (run_continuous_pipeline saves up to
        6 times per loop iteration) could have already moved past by the
        time this save() lands, silently reverting its progress.
        """
        try:
            with get_db() as db:
                processed_list = _get_project_context(db, self.PROCESSED_KEY) or []
                if design_hash in processed_list:
                    processed_list = [h for h in processed_list if h != design_hash]
                    _set_project_context(db, self.PROCESSED_KEY, processed_list)
        except Exception as e:
            logger.warning(f"Failed to remove processed hash: {e}")


def _workflow_belongs_to_project(
    wf_project_id: Optional[str],
    wf_working_directory: Optional[str],
    current_project_id: Optional[str],
    current_project_path: str,
) -> bool:
    """Whether a workflow belongs to the given project.

    Prefers the authoritative Workflow.project_id FK when both sides have
    one. Falls back to a resolved-path containment check via
    working_directory using Path.is_relative_to() -- NOT a raw
    str.startswith() prefix match, which wrongly matches sibling
    directories that share a name prefix (e.g. "/code/project-a" is a
    string-prefix of "/code/project-ab/.worktrees/wt_1", so a plain
    startswith() would treat project-ab's workflow as belonging to
    project-a). Returns False (does not belong) when neither signal can
    positively confirm membership -- the safe default for every caller of
    this helper: don't block on, force-fail, force-pause, or terminate
    agents for a workflow we can't positively confirm is ours.
    """
    if wf_project_id and current_project_id:
        return wf_project_id == current_project_id
    if not wf_working_directory:
        return False
    try:
        return Path(wf_working_directory).resolve().is_relative_to(Path(current_project_path).resolve())
    except (OSError, ValueError):
        return False
