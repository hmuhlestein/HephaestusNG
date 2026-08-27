"""Regression test: the /cleanup-branches endpoint's
cleanup_all_stale_branches() call must be offloaded to the executor.

cleanup_all_stale_branches does real git/filesystem work -- blocking, same
class of issue as this file's own /health endpoint a few lines below (which
already documents and applies this same offload-at-the-caller pattern).
queue_routes.py's rerun flow backgrounds an identical call in a
fire-and-forget thread, but that path doesn't return the result to a
caller; this endpoint's whole contract is returning cleanup results, so it
needs an awaited executor call instead of a direct blocking call.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp.autopilot.control_routes import cleanup_branches


@pytest.mark.asyncio
async def test_cleanup_is_offloaded_to_executor():
    fake_branch_manager = MagicMock()
    fake_branch_manager.cleanup_all_stale_branches.return_value = {"cleaned": 2}

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value={"cleaned": 2})

    with (
        patch("src.core.worktree_manager.WorktreeManager", return_value=fake_branch_manager),
        patch("asyncio.get_event_loop", return_value=fake_loop),
    ):
        result = await cleanup_branches(project_path="/tmp/some-project")

    assert result == {
        "cleaned": 2,
        "merged": 0,
        "failed": 0,
        "worktrees_cleaned": 0,
        "branches": [],
        "repos_swept": [{"path": "/tmp/some-project", "cleaned": 2}],
    }
    fake_loop.run_in_executor.assert_called_once_with(
        None, fake_branch_manager.cleanup_all_stale_branches
    )
    fake_branch_manager.cleanup_all_stale_branches.assert_not_called()
