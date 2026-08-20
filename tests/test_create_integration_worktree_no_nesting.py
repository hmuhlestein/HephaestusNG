"""Regression: _create_integration_worktree had no no-nested-worktrees guard
(CLAUDE.md invariant: "If project_path contains .worktrees/, use it
directly"). Unlike run_single_workflow's design-worktree setup, which
explicitly checks for this, this function always called
WorktreeManager.reload(project_path) and created a worktree under
project_path/.worktrees/ -- if project_path was itself already a worktree
checkout (e.g. .worktrees/wt_1), that produced a worktree nested inside a
worktree, which gets destroyed when the parent worktree is cleaned up."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.autopilot.orchestrator.worktree_integration import (
    _create_integration_worktree,
)


def test_project_path_already_a_worktree_is_used_directly():
    nested_path = Path("/repo/.worktrees/wt_1")

    with patch(
        "src.core.worktree_manager.WorktreeManager"
    ) as mock_wt_mgr_cls:
        result = _create_integration_worktree(
            project_path=nested_path,
            design_id="design-1",
            branch="feature/x",
            logger=MagicMock(),
        )

    assert result == nested_path
    mock_wt_mgr_cls.assert_not_called()
