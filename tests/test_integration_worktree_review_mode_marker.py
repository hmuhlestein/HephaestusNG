"""Regression: agent-safe-bin/git (scripts/agent-safe-bin/git) unconditionally
blocks `git merge`/`git push` onto main/master for any Hephaestus-managed repo
unless .hephaestus/review_approved exists -- it has no notion of a project's
own review_mode setting. A full-autopilot project (review_mode off, no human
review step to wait for) still got blocked, forcing git_expert to create a PR
it could never get approved, then rejecting update_task_status(done) because
verify_git_expert_merged_and_pushed correctly requires an actual merge to
main in that mode. Observed live: task 03e8b25a on a review_mode=False
project ("this branch's work is not yet merged into main...").

_create_integration_worktree now pre-writes the same review_approved marker
a human's approval would write, whenever the design's project has review_mode
off, so the wrapper's existing check passes immediately.
"""

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.autopilot.orchestrator.worktree_integration import (
    _create_integration_worktree,
)
from src.core.database import AutopilotDesign, AutopilotProject, DatabaseManager


def _seed(db_manager, review_mode: bool):
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    design_id = f"des-{uuid.uuid4().hex[:8]}"
    with db_manager.session_scope() as session:
        session.add(
            AutopilotProject(
                id=project_id,
                name="Test Project",
                base_dir=f"/tmp/{project_id}",
                review_mode=review_mode,
            )
        )
        session.add(
            AutopilotDesign(
                id=design_id,
                project_id=project_id,
                name="Test Design",
                spec_key="test-design.md",
            )
        )
    return design_id


def _run(tmp_path, db_manager, design_id):
    project_path = tmp_path / "project"
    project_path.mkdir()
    wt_base = tmp_path / "worktrees"

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.worktree_base = wt_base
    mock_wt_mgr.main_repo.git.branch.return_value = None
    mock_wt_mgr.main_repo.git.worktree.return_value = None

    with patch(
        "src.core.worktree_manager.WorktreeManager", return_value=mock_wt_mgr
    ):
        wt_path = _create_integration_worktree(
            project_path=project_path,
            design_id=design_id,
            branch="feature/x",
            logger=MagicMock(),
            db_manager=db_manager,
        )
    return project_path, wt_path


class TestReviewApprovedMarkerAtWorktreeCreation:
    def test_full_autopilot_project_gets_pre_approved(self, tmp_path):
        """Marker must land in BOTH the worktree and the actual repo
        checkout (project_path) -- git_expert's own prompt `cd`s into the
        latter (via `git rev-parse --git-common-dir`) before merging/
        pushing main, which is where the real block happened live."""
        db_manager = DatabaseManager(str(tmp_path / "test.db"))
        db_manager.create_tables()
        design_id = _seed(db_manager, review_mode=False)

        project_path, wt_path = _run(tmp_path, db_manager, design_id)

        assert (Path(wt_path) / ".hephaestus" / "review_approved").exists()
        assert (project_path / ".hephaestus" / "review_approved").exists()

    def test_review_mode_project_is_not_pre_approved(self, tmp_path):
        db_manager = DatabaseManager(str(tmp_path / "test.db"))
        db_manager.create_tables()
        design_id = _seed(db_manager, review_mode=True)

        project_path, wt_path = _run(tmp_path, db_manager, design_id)

        assert not (Path(wt_path) / ".hephaestus" / "review_approved").exists()
        assert not (project_path / ".hephaestus" / "review_approved").exists()
