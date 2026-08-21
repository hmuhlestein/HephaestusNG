"""Feature-Model DB record bookkeeping."""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional


from src.core.database import (
    Agent,
    Task,
    Workflow,
    get_db,
)
from src.core.simple_config import get_config
from src.core.status_derivation import derive_feature_status

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)


def _create_feature_records(
    design_id: Optional[str],
    features_json: dict,
    designs_folder: Path,
    logger: "OrchestratorLogger",
) -> List[dict]:
    """Create Feature DB records from features.json.

    Args:
        design_id: Design ID
        features_json: Parsed features.json content
        designs_folder: Path to designs folder
        logger: Orchestrator logger

    Returns:
        List of feature records created
    """
    import uuid

    from src.core.database import AutopilotDesign, Feature

    feature_records = []

    with get_db() as db:
        # Denormalized copy of the parent design's workflow_type onto every
        # feature it decomposes into -- see Feature.workflow_type's comment
        # in database.py and docs/BUGFIX_WORKFLOW_TYPE_DESIGN.md.
        parent_design = db.query(AutopilotDesign).filter_by(id=design_id).first()
        design_workflow_type = parent_design.workflow_type if parent_design else "feature"
        # Idempotency guard: finalize_phase0_workflow can now call this from
        # two independent sites for the same design (run_phase0's own
        # synchronous tail, and the generic phase0-completion hook in
        # PhaseManager._complete_workflow / the review-approve endpoint) --
        # both check design_id's Feature rows before calling in, but that
        # check-then-insert is TOCTOU-racy across two separate calls. Guard
        # here too, inside the same transaction as the insert, so a race
        # can't create duplicate Feature rows for one design.
        existing = db.query(Feature).filter_by(design_id=design_id).all()
        if existing:
            logger.info(f"_create_feature_records: {len(existing)} feature(s) already exist for design {design_id} -- skipping")
            return [
                {
                    "id": f.id,
                    "feature_key": f.feature_key,
                    "name": f.name,
                    "scope": f.scope,
                    "files": f.files,
                    "depends_on": f.depends_on,
                    "execution": f.execution,
                    "scope_doc_path": f.scope_doc_path,
                    "feature_record_path": f.feature_record_path,
                }
                for f in existing
            ]

        for feat in features_json.get("features", []):
            feature_id = f"feat-{uuid.uuid4().hex[:8]}"
            feature_key = feat.get("id", "")

            # Create feature record path
            feature_record_path = designs_folder / "features" / feature_key
            feature_record_path.mkdir(parents=True, exist_ok=True)

            # Only store scope_doc_path after the file has been copied by run_phase0.
            # At this point the copy has already happened (run_phase0 copies scope files
            # before calling _create_feature_records), so check for existence.
            scope_doc_path = feature_record_path / "scope.md"
            scope_doc_path_str = str(scope_doc_path) if scope_doc_path.exists() else None

            feature = Feature(
                id=feature_id,
                design_id=design_id,
                feature_key=feature_key,
                name=feat.get("name", feature_key),
                scope=feat.get("scope", ""),
                files=feat.get("files", []),
                depends_on=feat.get("depends_on", []),
                execution=feat.get("execution", "parallel"),
                status="pending",
                scope_doc_path=scope_doc_path_str,
                feature_record_path=str(feature_record_path),
                workflow_type=design_workflow_type,
            )
            db.add(feature)

            feature_records.append(
                {
                    "id": feature_id,
                    "feature_key": feature_key,
                    "name": feat.get("name", feature_key),
                    "scope": feat.get("scope", ""),
                    "files": feat.get("files", []),
                    "depends_on": feat.get("depends_on", []),
                    "execution": feat.get("execution", "parallel"),
                    "scope_doc_path": scope_doc_path_str,
                    "feature_record_path": str(feature_record_path),
                }
            )

            logger.info(f"Created feature record: {feature_key} ({feature_id})")

        db.commit()

    return feature_records


def _update_feature_status(
    feature_id: str,
    design_id: Optional[str],
    status: str,
    error: Optional[str] = None,
    logger: "OrchestratorLogger" = None,
) -> None:
    """Update a feature's status in the database.

    This is the single write path for Feature.status from the orchestrator.

    Args:
        feature_id: Feature ID
        design_id: Design ID
        status: New status (pending, active, completed, failed, skipped)
        error: Error message if status is failed
        logger: Optional logger
    """
    from datetime import datetime

    from src.core.database import Feature

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id, design_id=design_id).first()
        if feature:
            feature.status = status
            if status == "active":
                feature.started_at = datetime.utcnow()
            elif status in ("completed", "failed", "skipped"):
                feature.completed_at = datetime.utcnow()
            if error:
                feature.error = error
            db.commit()
            if logger:
                logger.info(f"Updated feature {feature.feature_key} status to {status}")


def _sync_stale_feature_statuses(logger: "OrchestratorLogger") -> int:
    """Self-heal: flip Feature.status to "completed" for any feature whose
    linked Workflow has already reached "completed", if the Feature row
    itself hasn't caught up.

    _update_feature_status is the normal write path for this, but it only
    ever runs as a side effect of _run_one_feature actually being called
    again for that specific feature -- which only happens on a fresh,
    full re-walk of the whole design (run_single_design ->
    run_feature_pipelines). A backend restart's resume path continues
    whatever workflow was actually in-flight directly (run_single_workflow's
    own poll loop), not a full design re-walk -- so a feature whose
    workflow finished (e.g. via a goto/retry cycle that happened to
    complete on its own after the restart) days ago can have its
    Feature.status stuck "active" indefinitely, with nothing left to ever
    call _run_one_feature for it again. Observed live: a feature's
    workflow status showed "completed" (git_expert had run) while its
    Feature row still showed "active" in the UI, unresolved across
    multiple backend restarts.

    Runs from the same generic, restart-safe background sweep that already
    drives _advance_phases for every workflow (see
    background_phase_advancement_sweep in server.py) -- Feature-table-wide,
    not scoped to a single workflow, since the whole point is to catch
    features no workflow-scoped loop is going to revisit.

    Returns the number of features repaired.
    """
    from src.core.database import Feature

    repaired = 0

    # Re-link any feature whose workflow link was never written (see
    # _relink_features_to_workflows). That function normally runs as a side
    # effect of _run_one_feature/run_single_design re-walking the design --
    # but a design whose pipeline has already fully finished has nothing
    # left to trigger a re-walk, so a feature whose workflow completed
    # without the link ever being written stays workflow_id=None forever.
    # The stale-status join below can't see it either (it requires a
    # linked Workflow row), so without this pass the feature is invisible
    # to every self-heal path and its status sticks indefinitely. Observed
    # live: a feature's workflow reached "completed" but Feature.workflow_id
    # was never set, leaving Feature.status stuck "active" across restarts.
    with get_db() as db:
        orphaned_design_ids = {
            design_id
            for (design_id,) in db.query(Feature.design_id)
            .filter(Feature.workflow_id.is_(None))
            .distinct()
            .all()
        }
    for design_id in orphaned_design_ids:
        _relink_features_to_workflows(design_id, logger)

    with get_db() as db:
        # Feature has a linked workflow that completed. Deliberately NOT
        # also inferring completion for workflow_id-less features from
        # sibling status ("has a completed sibling, no active sibling" was
        # tried here and reverted -- a feature that simply hasn't been
        # dispatched yet is indistinguishable from an orphaned-but-actually-
        # done one by that signal alone: both have workflow_id=None and a
        # non-terminal status. Observed live: the instant a design's last
        # in-flight feature flipped to "completed", every one of its
        # genuinely not-yet-started siblings got marked "completed" too on
        # the very next sweep tick, since none of them were "active" at
        # that moment either -- silently dropping their real work from the
        # pipeline). The actual "workflow completed but link never got
        # written" scenario is handled at the source instead: see
        # _relink_features_to_workflows, called from _run_one_feature
        # after every feature run and from run_single_design before every
        # design reprocessing.
        stale = (
            db.query(Feature)
            .join(Workflow, Feature.workflow_id == Workflow.id)
            .filter(
                Feature.status.notin_(["completed", "failed", "skipped"]),
                Workflow.status == "completed",
            )
            .all()
        )
        for feature in stale:
            old_status = feature.status
            # Route through the single source of truth instead of
            # hand-rolling "workflow completed -> feature completed" --
            # derive_feature_status also handles the edge cases this sweep
            # doesn't (a workflow that's "completed" but has an early
            # superseded-by-retry failed task, PAUSED/SKIPPED preservation,
            # incomplete-phase checks), and does its own write-back/commit.
            derived = derive_feature_status(db, feature.id, write_back=True)
            if derived != old_status:
                logger.info(f"[FEATURE-SYNC] {feature.feature_key}: workflow already completed, Feature.status was {old_status!r} -- derived {derived!r}")
                if derived in ("completed", "failed", "skipped"):
                    feature.completed_at = feature.completed_at or datetime.utcnow()
                repaired += 1

        if repaired:
            db.commit()
    return repaired


def _sync_stale_design_statuses(logger: "OrchestratorLogger") -> int:
    """Self-heal: flip AutopilotDesign.status to "completed" for any
    "active" design whose every Feature has reached completed/skipped.

    Mirrors pick_next_design's own "all features done -> mark completed"
    decision (this file, ~line 2316) -- but that check only ever runs as a
    side effect of the orchestrator picking its NEXT design to work on. A
    design whose last feature finishes without anything else in the
    pipeline ever needing to pick a new design again (the common case once
    every feature has already been queued) has its AutopilotDesign.status
    stuck "active" indefinitely, with nothing left to ever call
    pick_next_design for it again. Observed live: a design's UI showed
    every one of its features as done while the design itself still
    showed "active".

    Deliberately narrower than pick_next_design's full branch: only the
    unambiguous "nothing left to do" case is handled here (and note
    "nothing left to do" already implies no feature is stuck "failed"
    either, since "failed" is neither "completed" nor "skipped" and would
    itself count as incomplete). pick_next_design's failed-workflow
    retry/give-up branches have real side effects (retry-count bookkeeping
    tied to picking the next design) that belong to that call path, not an
    unrelated background sweep.

    Runs from the same generic, restart-safe background sweep as
    _sync_stale_feature_statuses.

    Returns the number of designs repaired.
    """
    from src.core.database import AutopilotDesign, Feature
    from src.core.status_derivation import derive_design_status

    repaired = 0
    with get_db() as db:
        active_designs = db.query(AutopilotDesign).filter_by(status="active").all()
        for design in active_designs:
            total = db.query(Feature).filter(Feature.design_id == design.id).count()
            if total == 0:
                continue  # not decomposed into features yet -- nothing to sync
            incomplete = (
                db.query(Feature)
                .filter(
                    Feature.design_id == design.id,
                    Feature.status.notin_(["completed", "skipped"]),
                )
                .count()
            )
            if incomplete > 0:
                continue
            # This raw-status pre-filter is just a cheap "worth checking"
            # gate -- the actual derivation (and its own write-back/commit)
            # goes through the single source of truth, which re-derives
            # each feature's status fresh rather than trusting the raw
            # Feature.status columns this filter used, and accounts for
            # has_failed_wf/VALIDATED cases this sweep's own simpler
            # "all completed/skipped" check doesn't.
            old_status = design.status
            derived = derive_design_status(db, design.id, write_back=True)
            if derived != old_status:
                logger.info(f"[DESIGN-SYNC] Design {design.id[:8]} ({design.name}) has all {total} feature(s) completed/skipped, status was {old_status!r} -- derived {derived!r}")
                repaired += 1
        if repaired:
            db.commit()
    return repaired


def _relink_features_to_workflows(design_id: str, logger: "OrchestratorLogger") -> None:
    """Re-link features to their workflows if workflow_id is missing.

    Handles pipeline restarts where features exist but their workflow link
    was lost -- and, since run_single_workflow clears
    state.current_workflow_id back to None right before returning
    "completed" (see its final success branch), this is also _run_one_
    feature's ONLY working way to link a just-finished feature's workflow
    (see the call site there). Matches features to workflows by feature_key
    in launch_params.
    """
    import json as _json

    from src.core.database import Feature

    with get_db() as db:
        unlinked = db.query(Feature).filter_by(design_id=design_id, workflow_id=None).all()
        if not unlinked:
            return

        # Scoped to this design_id -- without it, two different designs
        # that happen to share a feature_key (e.g. both have an "auth"
        # feature) could link a feature to the WRONG design's workflow.
        # Matters more now that this runs after every single feature
        # completes, not just once per design reprocessing.
        workflows = db.query(Workflow).filter(
            Workflow.definition_id == "autopilot", Workflow.design_id == design_id
        ).order_by(Workflow.created_at.desc()).all()

        for feat in unlinked:
            for wf in workflows:
                try:
                    params = wf.launch_params if isinstance(wf.launch_params, dict) else _json.loads(wf.launch_params or "{}")
                except Exception:
                    continue
                if params.get("feature_id") == feat.feature_key:
                    feat.workflow_id = wf.id
                    logger.info(f"Re-linked workflow {wf.id[:8]} to feature {feat.id} ({feat.name})")
                    break

        db.commit()


def _clean_stale_assigned_tasks(workflow_id: str, logger: "OrchestratorLogger") -> None:
    """Clean tasks that are 'pending', 'assigned', or 'in_progress' with a
    terminated agent, pending/assigned tasks that belong to already-completed
    workflows, and tasks stranded 'assigned' with no agent at all.

    Called periodically from the polling loop to prevent tasks from hanging
    forever when agents crash or are killed.
    """

    with get_db() as db:
        # 1. Tasks assigned to terminated agents. Includes "pending", not
        # just "assigned"/"in_progress" -- a task can carry assigned_
        # agent_id while still "pending" (e.g. a dispatch loop that sets
        # both fields in memory but only commits after a whole batch).
        # _advance_phases's own phase-scoped sweep already treats this as
        # a live bug (observed: a task stuck "pending" pointing at an
        # agent terminated hours earlier, never self-healed) -- this
        # workflow-wide pass claims the same job and needs the same floor.
        stale_tasks = (
            db.query(Task)
            .filter(
                Task.workflow_id == workflow_id,
                Task.status.in_(["pending", "assigned", "in_progress"]),
                Task.assigned_agent_id.isnot(None),
            )
            .all()
        )
        for task in stale_tasks:
            agent = db.query(Agent).filter_by(id=task.assigned_agent_id).first()
            if agent and agent.status == "terminated":
                logger.info(f"[STALE-TASK] Task {task.id[:8]} assigned to terminated agent {task.assigned_agent_id[:8]} — marking failed")
                task.status = "failed"
                # Don't clobber a real reason: update_task_status already
                # records why a "done" claim was rejected (e.g. a missing
                # output artifact) on this same field before the agent's
                # session ends. Only fall back to the generic message when
                # nothing more specific is already there -- otherwise the
                # retry below loses exactly the feedback it needs to fix.
                if not task.failure_reason:
                    task.failure_reason = f"Agent {task.assigned_agent_id[:8]} terminated unexpectedly"
                db.commit()

        # 2. Pending/assigned tasks in already-completed workflows
        workflow = db.query(Workflow).filter_by(id=workflow_id).first()
        if workflow and workflow.status == "completed":
            orphaned = (
                db.query(Task)
                .filter(
                    Task.workflow_id == workflow_id,
                    Task.status.in_(["pending", "assigned"]),
                )
                .all()
            )
            for task in orphaned:
                logger.info(f"[ORPHAN-TASK] Task {task.id[:8]} ({task.phase_id}) in completed workflow — marking failed")
                task.status = "failed"
                task.failure_reason = "Orphaned: workflow already completed"
                task.assigned_agent_id = None
            if orphaned:
                db.commit()

        # 3. Tasks stranded "assigned" with no agent at all. process_queue and
        # bump_task_priority_endpoint both dequeue ("queued" -> "assigned")
        # before the dispatch that can fail; both now requeue on failure, but
        # a process death in that window runs no handler at all. The result is
        # the one task state nothing else can reclaim: get_next_queued_task
        # reads only "queued", case 1 above requires assigned_agent_id
        # isnot(None), and every mechanical_recovery detector looks its task up
        # by agent. Unlike "pending" (which phase_transitions already retries
        # when unassigned), no sweep covers it -- observed live, a review task
        # sat this way for hours while its workflow stayed active.
        #
        # started_at is NULL only if the task was never dispatched, so this
        # cannot fire on a task an agent is really working. The grace period
        # is what keeps it off a dispatch still in flight: queued_at is
        # refreshed by enqueue_task immediately before every dequeue, so it
        # measures time since this dispatch attempt began, not task age.
        grace_seconds = get_config().monitoring.stranded_task_grace_seconds
        cutoff = datetime.utcnow() - timedelta(seconds=grace_seconds)
        stranded = (
            db.query(Task)
            .filter(
                Task.workflow_id == workflow_id,
                Task.status == "assigned",
                Task.assigned_agent_id.is_(None),
                Task.started_at.is_(None),
                Task.queued_at.isnot(None),
                Task.queued_at < cutoff,
            )
            .all()
        )
        for task in stranded:
            logger.info(
                f"[STRANDED-TASK] Task {task.id[:8]} assigned with no agent since "
                f"{task.queued_at} (>{grace_seconds}s) — returning to queue"
            )
            task.status = "queued"
            task.queue_position = None
        if stranded:
            db.commit()


def _validate_features_json(features_json: dict) -> None:
    """Validate features.json structure.

    Args:
        features_json: Parsed features.json content

    Raises:
        ValueError: If validation fails
    """
    if not isinstance(features_json, dict):
        raise ValueError("features.json must be a JSON object")

    if "design_name" not in features_json:
        raise ValueError("features.json missing 'design_name' field")

    if "features" not in features_json:
        raise ValueError("features.json missing 'features' array")

    features = features_json["features"]
    if not isinstance(features, list):
        raise ValueError("'features' must be an array")

    # The prompt targets "around 5" as a rough guide, but the actual count
    # should follow the design's own structure -- a complex design
    # legitimately needing 6-10 features shouldn't have its entire Phase 0
    # output discarded over a headcount. Observed live: a well-formed
    # 6-feature decomposition for a genuinely multi-concern backend design
    # got rejected outright by a strict 1-5 cap, throwing away real analysis
    # work and forcing a full re-run. Keep only a generous sanity ceiling to
    # catch actual garbage (e.g. one "feature" per file).
    if len(features) < 1:
        raise ValueError("features array must have at least 1 entry, got 0")
    if len(features) > 50:
        raise ValueError(f"features array has {len(features)} entries -- that's not a feature decomposition, it looks like one feature per file")

    # Check for required fields and unique IDs
    ids = set()
    all_files = []

    for i, feat in enumerate(features):
        if "id" not in feat:
            raise ValueError(f"Feature {i} missing 'id' field")
        if "name" not in feat:
            raise ValueError(f"Feature {i} missing 'name' field")
        if "scope" not in feat:
            raise ValueError(f"Feature {i} missing 'scope' field")

        feat_id = feat["id"]
        if feat_id in ids:
            raise ValueError(f"Duplicate feature id: {feat_id}")
        ids.add(feat_id)

        # Check execution field
        execution = feat.get("execution", "parallel")
        if execution not in ("parallel", "sequential"):
            raise ValueError(f"Feature {feat_id} has invalid execution: {execution}")

        # Check depends_on references
        depends_on = feat.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise ValueError(f"Feature {feat_id} depends_on must be an array")

        # Check files for overlaps
        files = feat.get("files", [])
        if not isinstance(files, list):
            raise ValueError(f"Feature {feat_id} files must be an array")

        for f in files:
            # Normalize: strip trailing slashes for comparison
            f_norm = f.rstrip("/")
            for existing in all_files:
                existing_norm = existing.rstrip("/")
                # Check for overlap: exact match or directory containment
                # (e.g. src/ contains src/utils/file.py, but .env does NOT contain .env.example)
                if f_norm == existing_norm or f_norm.startswith(existing_norm + "/") or existing_norm.startswith(f_norm + "/"):
                    raise ValueError(f"File overlap between features: {f} and {existing}")
            all_files.append(f)

    # Validate depends_on references
    for feat in features:
        depends_on = feat.get("depends_on", [])
        for dep in depends_on:
            if dep not in ids:
                raise ValueError(f"Feature {feat['id']} depends on unknown feature: {dep}")

    # Check for cycles
    def has_cycle(graph: dict) -> bool:
        """Check for cycles in dependency graph using DFS."""
        visited = set()
        rec_stack = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)

            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True

            rec_stack.remove(node)
            return False

        for node in graph:
            if node not in visited:
                if dfs(node):
                    return True
        return False

    # Build dependency graph
    dep_graph = {feat["id"]: feat.get("depends_on", []) for feat in features}
    if has_cycle(dep_graph):
        raise ValueError("Dependency cycle detected in features")


def _resolve_execution_order(
    features: List[dict],
    logger: "OrchestratorLogger",
) -> List[List[dict]]:
    """Resolve execution order using Kahn's algorithm with parallel/sequential handling.

    Args:
        features: List of feature dicts from features.json
        logger: Orchestrator logger

    Returns:
        List of execution groups, each group is a list of features
    """
    from collections import defaultdict, deque

    # Build dependency graph
    in_degree = {f["id"]: 0 for f in features}
    adjacency = defaultdict(list)

    for feat in features:
        feat_id = feat["id"]
        depends_on = feat.get("depends_on", [])
        in_degree[feat_id] = len(depends_on)
        for dep in depends_on:
            adjacency[dep].append(feat_id)

    # Kahn's algorithm
    queue = deque([f["id"] for f in features if in_degree[f["id"]] == 0])
    execution_groups = []
    processed = set()

    # Build lookup for quick access
    feat_map = {f["id"]: f for f in features}
    # Architect's original features.json order, for ordering siblings within
    # a dependency layer below -- Kahn's algorithm only guarantees a feature
    # never runs before its dependencies (layer boundaries), it says nothing
    # about the relative order of independent features at the same depth.
    original_index = {f["id"]: i for i, f in enumerate(features)}

    while queue:
        # Collect current layer
        current_layer = []
        while queue:
            feat_id = queue.popleft()
            if feat_id not in processed:
                current_layer.append(feat_id)
                processed.add(feat_id)

        if not current_layer:
            break

        # Group this layer's features in the architect's original list
        # order -- consecutive "parallel" features batch into one group,
        # a "sequential" feature gets its own group in place, rather than
        # unconditionally running every parallel feature in the layer
        # before any sequential one regardless of where it sat in the
        # design's own ordering (observed live: a sequential feature that
        # several other features depend on got pushed to run after two
        # unrelated parallel features listed after it, purely because of
        # the parallel/sequential split, not any real dependency).
        current_layer.sort(key=lambda fid: original_index[fid])
        parallel_batch = []
        for feat_id in current_layer:
            feat = feat_map[feat_id]
            if feat.get("execution", "parallel") == "parallel":
                parallel_batch.append(feat)
                continue
            if parallel_batch:
                execution_groups.append(parallel_batch)
                parallel_batch = []
            execution_groups.append([feat])
        if parallel_batch:
            execution_groups.append(parallel_batch)

        # Reduce in-degrees of dependents
        for feat_id in current_layer:
            for neighbor in adjacency[feat_id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0 and neighbor not in processed:
                    queue.append(neighbor)

    # Check for cycles (unprocessed features)
    unprocessed = [f["id"] for f in features if f["id"] not in processed]
    if unprocessed:
        logger.warning(f"Cycle detected in dependencies among {unprocessed}; appending cyclic features sequentially after already-resolved groups")
        # Preserve groups already resolved by Kahn's algorithm; append the cyclic
        # remainder one-by-one so we don't silently drop any processed features.
        for fid in feat_map:
            if fid not in processed:
                execution_groups.append([feat_map[fid]])

    # Log execution plan
    logger.info("Execution plan:")
    for i, group in enumerate(execution_groups):
        feat_names = [f["name"] for f in group]
        if len(group) > 1:
            logger.info(f"  Group {i + 1}: PARALLEL - {', '.join(feat_names)}")
        else:
            logger.info(f"  Group {i + 1}: SEQUENTIAL - {feat_names[0]}")

    return execution_groups
