"""Regression test: spawn_validation must not block the event loop
creating a validation checkpoint commit.

Found live 2026-08-19, continuing the same investigation as
commit_and_link_ticket's own fix (task_completion/git_link.py):
WorktreeManager.commit_for_validation is a second, independent
`git add -A`/`git commit` pair on the same worktree, called directly
inside this async function with no thread-pool offload.
"""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_commit_for_validation_runs_off_the_event_loop_thread(monkeypatch):
    import src.services.task_completion.validation as validation_module

    main_thread_id = threading.get_ident()
    call_thread_id = {}

    def _fake_commit_for_validation(agent_id, iteration):
        call_thread_id["id"] = threading.get_ident()
        return {"commit_sha": "deadbeef" * 5}

    mock_state = MagicMock()
    mock_state.branch_manager.commit_for_validation = MagicMock(
        side_effect=_fake_commit_for_validation
    )
    mock_state.db_manager.session_scope.return_value.__enter__.return_value.query.return_value.filter_by.return_value.first.return_value = None
    mock_state.agent_manager.terminate_agent = AsyncMock()
    mock_state.broadcast_update = AsyncMock()

    monkeypatch.setattr(
        "src.core.app_context.get_app_state", lambda: mock_state
    )

    with patch(
        "src.validation.validator_agent.spawn_validator_agent",
        new_callable=AsyncMock,
        return_value="validator-1",
    ):
        await validation_module.spawn_validation(
            agent_id="agent-1", task_id="task-1", task_workflow_id="wf-1",
            task_validation_iteration=1,
        )

    assert call_thread_id.get("id") is not None, "commit_for_validation was never called"
    assert call_thread_id["id"] != main_thread_id, (
        "commit_for_validation ran on the event loop's own thread -- it "
        "must run in the executor's thread pool instead"
    )
