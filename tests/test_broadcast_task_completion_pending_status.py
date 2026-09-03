"""Regression: _broadcast_task_completion (_update_task_status_steps.py)
hardcoded "status": "failed" for ANY truthy output_lost_rejection dict --
correct for a real rejection, but §3.3's new "pending" outcome (verify_
git_expert_merged_and_pushed, CI still running) is ALSO a truthy dict,
so it broadcast a misleading "task failed" UI notification for a
completely normal wait. Every existing rejection dict already carries
its own "status" key (all "failed") -- read it instead of assuming.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.mcp.server._shared import UpdateTaskStatusRequest
from src.mcp.server._update_task_status_steps import _broadcast_task_completion


class _FakeTask:
    workflow_id = "wf-1"


@pytest.mark.asyncio
async def test_pending_outcome_broadcasts_pending_not_failed():
    request = UpdateTaskStatusRequest(task_id="task-1", status="done", summary="s")
    pending_dict = {
        "status": "pending",
        "message": "PR is up but CI hasn't finished yet -- nothing more to do this turn.",
    }

    with patch("src.core.database.resolve_project_for_workflow", return_value=("proj-1", "myproject")), \
         patch("src.mcp.server._update_task_status_steps.server_state") as mock_state:
        mock_state.broadcast_update = AsyncMock()
        await _broadcast_task_completion(_FakeTask(), "agent-1", request, pending_dict)

    sent = mock_state.broadcast_update.call_args[0][0]
    assert sent["status"] == "pending"


@pytest.mark.asyncio
async def test_a_real_rejection_still_broadcasts_failed():
    request = UpdateTaskStatusRequest(task_id="task-1", status="done", summary="s")
    rejection_dict = {"status": "failed", "message": "the worktree still has uncommitted changes"}

    with patch("src.core.database.resolve_project_for_workflow", return_value=("proj-1", "myproject")), \
         patch("src.mcp.server._update_task_status_steps.server_state") as mock_state:
        mock_state.broadcast_update = AsyncMock()
        await _broadcast_task_completion(_FakeTask(), "agent-1", request, rejection_dict)

    sent = mock_state.broadcast_update.call_args[0][0]
    assert sent["status"] == "failed"


@pytest.mark.asyncio
async def test_no_rejection_broadcasts_the_requested_status():
    request = UpdateTaskStatusRequest(task_id="task-1", status="done", summary="s")

    with patch("src.core.database.resolve_project_for_workflow", return_value=("proj-1", "myproject")), \
         patch("src.mcp.server._update_task_status_steps.server_state") as mock_state:
        mock_state.broadcast_update = AsyncMock()
        await _broadcast_task_completion(_FakeTask(), "agent-1", request, None)

    sent = mock_state.broadcast_update.call_args[0][0]
    assert sent["status"] == "done"
