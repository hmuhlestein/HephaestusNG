"""Regression coverage for get_task_instructions's worktree-resolution
fallback (src.mcp.agents_api).

Phase 3 Tier 1 item 7 (docs/AUTOPILOT_REFACTOR_PLAN.md) fixed
WorktreeManager.get_agent_branch_path to return None instead of the main
repo path when no AgentBranch record exists. This is the one production
caller of that function with no pre-existing test coverage at all --
launch_pipeline.py's caller already had characterization tests mocking a
None return (tests/test_restart_agent_characterization.py), and
validator_agent.py's caller's one existing test happened to mock a truthy
path. This file closes that gap: proves get_task_instructions fails loudly
(404) instead of silently resolving to the main repo when
get_agent_branch_path returns None.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.mcp.agents_api import get_task_instructions


def _make_server_state(task, workflow=None):
    state = MagicMock()
    session = MagicMock()

    def query_side_effect(model):
        query = MagicMock()
        name = getattr(model, "__name__", "")
        if name == "Task":
            query.filter_by.return_value.first.return_value = task
        elif name == "Workflow":
            query.filter_by.return_value.first.return_value = workflow
        else:
            query.filter_by.return_value.first.return_value = None
        return query

    session.query.side_effect = query_side_effect
    state.db_manager.get_session.return_value = session
    return state, session


@pytest.mark.asyncio
async def test_raises_404_when_no_worktree_resolvable():
    """No workflow.working_directory AND get_agent_branch_path returns
    None -- must fail loudly (404), not silently fall through to reading
    a task-instructions file out of the main repo."""
    task = MagicMock()
    task.id = "task-1"
    task.workflow_id = None
    task.assigned_agent_id = "agent-1"

    state, _session = _make_server_state(task)
    state.agent_manager.branch_manager.get_agent_branch_path.return_value = None

    with patch("src.mcp.agents_api._get_server_state", return_value=state):
        with pytest.raises(HTTPException) as exc_info:
            await get_task_instructions("task-1", request=None)

    assert exc_info.value.status_code == 404
    assert "Could not resolve a worktree" in exc_info.value.detail


@pytest.mark.asyncio
async def test_uses_agent_branch_path_when_resolvable(tmp_path):
    """Companion to the 404 test above: confirms the normal (non-None)
    path still resolves and reads the instructions file correctly."""
    instructions_dir = tmp_path / ".hephaestus" / "tasks"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "task-1.md").write_text("Do the thing.")

    task = MagicMock()
    task.id = "task-1"
    task.workflow_id = None
    task.assigned_agent_id = "agent-1"

    state, _session = _make_server_state(task)
    state.agent_manager.branch_manager.get_agent_branch_path.return_value = str(tmp_path)

    with patch("src.mcp.agents_api._get_server_state", return_value=state):
        result = await get_task_instructions("task-1", request=None)

    assert result["content"] == "Do the thing."
