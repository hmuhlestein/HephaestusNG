"""Sync local main after a review approval's --auto-armed PR merge lands
asynchronously.

review_feature (feature_review_routes.py) syncs the local main checkout
immediately when a review approval's `gh pr merge` lands right away, but
`--auto` can instead just ARM the merge (required checks still running)
and return before it actually happens -- that request flags the feature
with Feature.auto_merge_sync_pending=True instead. This module's sweep
re-checks those PRs each tick and performs the same local-main sync once
GitHub actually completes the merge.

Mirrors pr_resolution.py's shape deliberately: state resolved later by a
periodic sweep tick rather than spinning up a fresh agent (or blocking
the approval request) just to ask "has it landed yet". Called every
sweep tick -- see background_phase_advancement_sweep.
"""

import logging
from typing import TYPE_CHECKING

from src.core.database import AutopilotProject, Feature, get_db, resolve_project_for_workflow

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)


def _sync_local_main_for_landed_auto_merges(sweep_logger: "OrchestratorLogger") -> None:
    """Feature-table-wide, not scoped to any one workflow -- same reasoning
    as the feature/design-status syncs in features.py: the target set here
    is "every feature with a pending auto-merge sync," found by its own
    DB query, not driven by a per-workflow loop.
    """
    with get_db() as db:
        pending = (
            db.query(Feature)
            .filter(Feature.auto_merge_sync_pending.is_(True), Feature.pr_url.isnot(None))
            .all()
        )
        # Snapshot before the session closes -- mirrors pr_resolution.py's
        # own _resolve_pending_pr_status, which does the same for the
        # identical reason (the gh subprocess call below is slow enough
        # that holding this session open across it risks it going stale).
        candidates = [(f.id, f.pr_url, f.workflow_id) for f in pending]

    if not candidates:
        return

    from src.services.github_pr_status import get_pr_status

    for feature_id, pr_url, workflow_id in candidates:
        pr_status = get_pr_status(pr_url)
        if pr_status is None or pr_status.state == "OPEN":
            continue  # gh unavailable, or genuinely still pending -- check again next tick.

        with get_db() as db:
            feature = db.query(Feature).filter_by(id=feature_id).first()
            if not feature or not feature.auto_merge_sync_pending:
                continue  # Resolved by a concurrent tick already.

            if pr_status.state != "MERGED":
                # CLOSED without merging -- abandoned/superseded. Nothing
                # to sync; stop checking so this doesn't poll gh forever.
                sweep_logger.warning(
                    f"[REVIEW-SYNC] Feature {feature_id[:8]}'s auto-merge PR {pr_url} "
                    f"was closed without merging ({pr_status.state}) -- giving up on the sync"
                )
                feature.auto_merge_sync_pending = False
                db.commit()
                continue

            sync_project_id, _ = resolve_project_for_workflow(workflow_id) if workflow_id else (None, None)
            sync_project = db.query(AutopilotProject).get(sync_project_id) if sync_project_id else None
            sync_base_dir = sync_project.base_dir if sync_project else None

        if sync_base_dir:
            try:
                from src.core.worktree_manager import sync_local_main_checkout

                sync_local_main_checkout(sync_base_dir, f"review-approval-sync:{workflow_id}")
                sweep_logger.info(
                    f"[REVIEW-SYNC] Synced local main checkout at {sync_base_dir} "
                    f"after deferred auto-merge for feature {feature_id[:8]} landed"
                )
            except Exception as e:
                sweep_logger.warning(
                    f"[REVIEW-SYNC] Failed to sync local main for feature {feature_id[:8]}: {e}"
                )
                continue  # Leave the flag set -- retry next tick.

        with get_db() as db:
            feature = db.query(Feature).filter_by(id=feature_id).first()
            if feature:
                feature.auto_merge_sync_pending = False
                db.commit()
