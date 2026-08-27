"""Regression tests: give_validation_review's two blocking-call sites must
be offloaded to the executor, not called directly on the event loop.

- The "validation passed" branch commits the agent's worktree via
  GitPython (git add -A, git commit) -- real subprocess work.
- The "validation failed" branch sends feedback to the still-running
  agent via send_feedback_to_agent, which shells out to `tmux send-keys`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.mcp.server  # noqa: F401 -- import once here, at module scope; a

# lazy in-test import (e.g. via `from src.mcp.memory_api import ...`)
# transitively re-triggers this module's own _set_app_state(server_state)
# call, clobbering a fake app state a test just installed (see the
# identical note in tests/test_broadcast_scoping_round2.py).
from src.core.app_context import set_app_state
from src.core.database import Agent, AutopilotProject, DatabaseManager, Task, Workflow


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


class _StubAgentManager:
    async def terminate_agent(self, agent_id):
        return True


class FakeServerState:
    def __init__(self, db_manager, branch_manager=None):
        self.db_manager = db_manager
        self.broadcast_calls = []
        self.agent_manager = _StubAgentManager()
        self.branch_manager = branch_manager

    async def broadcast_update(self, data, project_id=None, project_name=None):
        self.broadcast_calls.append(data)


@pytest.fixture
def fake_state(db_manager):
    import src.core.app_context as app_context

    previous = app_context._app_state
    branch_manager = MagicMock()
    state = FakeServerState(db_manager, branch_manager=branch_manager)
    set_app_state(state)
    try:
        yield state
    finally:
        app_context._app_state = previous


def _seed(db_manager, task_status="under_review"):
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-a", name="Project A", base_dir="/tmp/proj-a"))
        session.add(Workflow(id="wf-1", name="wf-1", status="active", project_id="proj-a", phases_folder_path="/tmp"))
        session.add(Agent(id="validator-1", system_prompt="t", status="working", cli_type="pi", agent_type="validator"))
        session.add(Agent(id="agent-1", system_prompt="t", status="working", cli_type="pi", agent_type="phase"))
        session.add(Task(
            id="task-1", raw_description="d", done_definition="done", status=task_status,
            workflow_id="wf-1", assigned_agent_id="agent-1", validation_iteration=1,
        ))


@pytest.mark.asyncio
async def test_validation_passed_offloads_worktree_commit(db_manager, fake_state, monkeypatch):
    from src.mcp.memory_api import GiveValidationReviewRequest, give_validation_review

    async def noop_process_queue():
        pass

    monkeypatch.setattr("src.mcp.server.background_loops.process_queue", noop_process_queue)
    _seed(db_manager)

    record = MagicMock()
    record.worktree_path = "/tmp/proj-a/.worktrees/agent-1"
    record.branch_name = "agent/agent-1"
    fake_state.branch_manager._agent_record.return_value = record

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=True)

    request = GiveValidationReviewRequest(
        task_id="task-1", validator_agent_id="validator-1",
        validation_passed=True, feedback="looks good",
    )
    with patch("asyncio.get_event_loop", return_value=fake_loop):
        response = await give_validation_review(request=request, agent_id="validator-1")

    assert response.status == "completed"
    fake_loop.run_in_executor.assert_called_once()
    executor_arg, func_arg, worktree_arg, agent_arg = fake_loop.run_in_executor.call_args.args
    assert executor_arg is None
    assert func_arg.__name__ == "_commit_validated_worktree"
    assert worktree_arg == "/tmp/proj-a/.worktrees/agent-1"
    assert agent_arg == "agent-1"


@pytest.mark.asyncio
async def test_validation_failed_offloads_send_feedback_to_agent(db_manager, fake_state, monkeypatch):
    from src.mcp.memory_api import GiveValidationReviewRequest, give_validation_review

    async def noop_process_queue():
        pass

    monkeypatch.setattr("src.mcp.server.background_loops.process_queue", noop_process_queue)
    _seed(db_manager)

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=True)

    request = GiveValidationReviewRequest(
        task_id="task-1", validator_agent_id="validator-1",
        validation_passed=False, feedback="needs work",
    )
    with patch("asyncio.get_event_loop", return_value=fake_loop):
        response = await give_validation_review(request=request, agent_id="validator-1")

    assert response.status == "needs_work"
    fake_loop.run_in_executor.assert_called_once()
    executor_arg, func_arg = fake_loop.run_in_executor.call_args.args[:2]
    assert executor_arg is None
    assert func_arg.func.__name__ == "send_feedback_to_agent"
    assert func_arg.keywords["agent_id"] == "agent-1"
    assert func_arg.keywords["feedback"] == "needs work"


@pytest.mark.asyncio
async def test_validation_passed_shares_session_with_result_service(
    db_manager, fake_state, monkeypatch
):
    """Regression: give_validation_review opened its own session and, when
    a task had results, called ResultService.get_results_for_task/
    verify_result -- each of which opened and committed its OWN
    independent session against AgentResult rows. A failure in the
    caller's own transaction after that point (e.g. the follow-up-task
    creation, or the final session.commit()) couldn't roll back an
    AgentResult already marked "verified" against a ValidationReview that
    was never actually persisted."""
    import contextlib

    from src.core.database import AgentResult
    from src.core.database import get_db as real_get_db
    from src.mcp.memory_api import GiveValidationReviewRequest, give_validation_review

    async def noop_process_queue():
        pass

    monkeypatch.setattr("src.mcp.server.background_loops.process_queue", noop_process_queue)
    _seed(db_manager)

    with db_manager.session_scope() as session:
        task = session.query(Task).filter_by(id="task-1").first()
        task.has_results = True
        session.add(AgentResult(
            id="result-1", agent_id="agent-1", task_id="task-1",
            markdown_content="x", markdown_file_path="/tmp/x.md",
            result_type="implementation", summary="did the thing",
        ))

    call_count = 0

    @contextlib.contextmanager
    def counting_get_db(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        with real_get_db(*args, **kwargs) as db:
            yield db

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=True)

    request = GiveValidationReviewRequest(
        task_id="task-1", validator_agent_id="validator-1",
        validation_passed=True, feedback="looks good",
    )
    with patch("asyncio.get_event_loop", return_value=fake_loop), patch(
        "src.services.result_service.get_db", side_effect=counting_get_db
    ):
        response = await give_validation_review(request=request, agent_id="validator-1")

    assert response.status == "completed"
    assert call_count == 0

    with db_manager.session_scope() as session:
        result = session.query(AgentResult).filter_by(id="result-1").first()
        assert result.verification_status == "verified"
