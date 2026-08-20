"""Regression test: memory_api.submit_result's commit_for_validation call
must not block the event loop.

Found while checking for gaps in the same-day fix to spawn_validation's
own commit_for_validation call (src/services/task_completion/validation.py)
-- this is a second, independent call site for the identical
WorktreeManager.commit_for_validation subprocess work, reached via the
/submit_result HTTP endpoint instead of the task-completion path, and it
was missed by that earlier fix because it's a different caller.
"""

import threading
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from src.mcp.memory_api import SubmitResultRequest, submit_result


@pytest.mark.asyncio
async def test_commit_for_validation_runs_off_the_event_loop_thread():
    main_thread_id = threading.get_ident()
    call_thread_id = {}

    def _fake_commit_for_validation(agent_id, iteration, message):
        call_thread_id["id"] = threading.get_ident()
        return {"commit_sha": "cafefeed" * 5}

    mock_state = MagicMock()
    mock_state.branch_manager.commit_for_validation = MagicMock(
        side_effect=_fake_commit_for_validation
    )
    mock_state.result_validator_service.should_spawn_validator.return_value = (False, None)
    mock_state.broadcast_update = AsyncMock()

    row = MagicMock(id="task-1", workflow_id="wf-1")
    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = row
    mock_state.db_manager.get_session.return_value = mock_session

    submit_result_return = {
        "result_id": "res-1",
        "status": "submitted",
        "created_at": "2026-01-01T00:00:00",
    }

    with (
        patch("src.mcp.memory_api.get_app_state", return_value=mock_state),
        patch(
            "src.services.workflow_result_service.WorkflowResultService.submit_result",
            return_value=submit_result_return,
        ),
        patch("builtins.open", mock_open(read_data="result markdown")),
        patch(
            "src.core.database.resolve_project_for_workflow",
            return_value=(None, None),
        ),
    ):
        response = await submit_result(
            request=SubmitResultRequest(
                markdown_file_path="/tmp/result.md", explanation="Did the thing",
            ),
            agent_id="agent-1",
        )

    assert response.result_id == "res-1"
    assert call_thread_id.get("id") is not None, "commit_for_validation was never called"
    assert call_thread_id["id"] != main_thread_id, (
        "commit_for_validation ran on the event loop's own thread -- it "
        "must run in the executor's thread pool instead"
    )
