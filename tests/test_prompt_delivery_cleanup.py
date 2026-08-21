"""Tests for agent and task cleanup when prompt delivery fails."""

import copy
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from src.agents.manager import AgentManager
from src.core.database import Agent, DatabaseManager, Task
from src.interfaces import LLMProviderInterface


def _disable_cli_fallback(agent_manager):
    """Make a launch failure final instead of retrying on a second CLI.

    These tests stub _send_initial_prompt_with_retry to fail and assert the
    agent/task are cleaned up -- and assert `kill_session` was called
    exactly ONCE, i.e. that a single launch attempt happened. Once a
    default fallback CLI was configured (`agents.default_fallback_cli_tool`),
    a failed primary silently re-dispatched the whole thing on the fallback
    tool, which does not route prompt delivery through the stubbed method at
    all -- so the fallback *succeeded*, create_agent_for_task returned an
    AgentInfo, and `pytest.raises` saw no exception. That is what these
    three tests were failing on.

    Shadow the config on this AgentManager instance only; `self.config` is
    the process-wide get_config() singleton, so mutating it in place would
    leak into every later test in the session.
    """
    cfg = copy.copy(agent_manager.config)
    cfg.agents = copy.copy(agent_manager.config.agents)
    cfg.agents.default_fallback_cli_tool = None
    cfg.agents.default_fallback_cli_model = None
    agent_manager.config = cfg


def _install_session(db_manager, mock_session):
    """Point both session accessors at the same mock session.

    A bare Mock's session_scope() returns another Mock, which doesn't
    support `with ... as` -- launch_pipeline's agent-registration block
    uses the real session_scope() (SOLID review 3.10), so this has to
    actually behave like one. Same underlying session as get_session()
    returns, so assertions against either see the same calls.
    """
    db_manager.get_session = Mock(return_value=mock_session)

    @contextmanager
    def _session_scope():
        yield mock_session

    db_manager.session_scope = _session_scope


def _make_session(agent_record=None, task_record=None):
    """Build a session mock that answers by WHAT is being queried.

    These tests used to drive `.first()` off a positional
    `side_effect=[...]` list tuned to one exact call sequence, so any
    change to how many lookups create_agent_for_task performs silently
    handed the wrong row to the wrong caller. That is what broke them:
    the duplicate-active-agent guard
    (`query(Agent).filter(...).first()`) received the *Task* record,
    saw a truthy "existing agent", and returned early -- so the launch
    never reached the prompt-delivery failure these tests exist to
    exercise, and `pytest.raises` saw no exception at all.

    Answering by model + filter shape instead is stable under any
    call-count change:
      - query(Agent).filter(...)      -> duplicate guard, must be None
      - query(Agent).filter_by(id=..) -> terminate_agent's lookup
      - query(Task).filter_by(...)    -> the task being failed
      - .all()                        -> terminate_agent's stray sweep
    """

    def _query(model):
        q = Mock()
        looked_up_by_id = {"value": False}

        def _filter_by(*_a, **_kw):
            looked_up_by_id["value"] = True
            return q

        def _first():
            if model is Agent:
                return agent_record if looked_up_by_id["value"] else None
            if model is Task:
                return task_record
            return None

        q.filter_by = Mock(side_effect=_filter_by)
        q.filter = Mock(return_value=q)
        q.order_by = Mock(return_value=q)
        q.limit = Mock(return_value=q)
        q.all = Mock(return_value=[])
        q.first = Mock(side_effect=_first)
        return q

    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.add = Mock()
    session.commit = Mock()
    session.rollback = Mock()
    session.close = Mock()
    # merge() returns the instance passed in, so the caller's
    # `agent.id` read after it behaves like the real thing.
    session.merge = Mock(side_effect=lambda obj: obj)
    session.query = Mock(side_effect=_query)
    return session


@pytest.fixture
def mock_db_manager():
    """Create a mock database manager."""
    db_manager = Mock(spec=DatabaseManager)

    # Mock get_session to return a mock session that supports `with`
    mock_session = Mock()
    mock_session.__enter__ = Mock(return_value=mock_session)
    mock_session.__exit__ = Mock(return_value=False)
    mock_session.add = Mock()
    mock_session.commit = Mock()
    mock_session.rollback = Mock()
    mock_session.close = Mock()
    mock_session.query = Mock()

    _install_session(db_manager, mock_session)

    return db_manager


@pytest.fixture
def mock_llm_provider():
    """Create a mock LLM provider."""
    provider = Mock(spec=LLMProviderInterface)
    provider.generate_agent_prompt = AsyncMock(return_value="Test system prompt")
    return provider


@pytest.fixture
def mock_worktree_manager():
    """Create a mock worktree manager."""
    worktree_manager = Mock()
    worktree_manager.create_agent_worktree = Mock(
        return_value={
            "working_directory": "/tmp/test-worktree",
            "branch_name": "agent/test-branch",
        }
    )
    return worktree_manager


@pytest.fixture
def mock_tmux_server():
    """Create a mock tmux server."""
    server = Mock()
    server.has_session = Mock(return_value=True)

    # Mock session and pane
    mock_session = Mock()
    mock_pane = Mock()
    mock_pane.send_keys = Mock()
    mock_pane.cmd = Mock()

    mock_window = Mock()
    mock_window.attached_pane = mock_pane
    mock_session.attached_window = mock_window
    mock_session.kill_session = Mock()

    server.new_session = Mock(return_value=mock_session)

    return server


@pytest.mark.asyncio
async def test_agent_and_task_cleanup_on_prompt_delivery_failure(
    mock_db_manager, mock_llm_provider, mock_worktree_manager, mock_tmux_server
):
    """Test that agent and task are properly cleaned up when prompt delivery fails."""

    # Create agent manager with mocks
    agent_manager = AgentManager(
        db_manager=mock_db_manager, llm_provider=mock_llm_provider
    )

    # Replace worktree manager and tmux server with mocks
    agent_manager.branch_manager = mock_worktree_manager
    agent_manager.tmux_server = mock_tmux_server
    _disable_cli_fallback(agent_manager)

    # Create a mock task
    task = Task(
        id="test-task-123",
        raw_description="Test task",
        enriched_description="Test task enriched",
        done_definition="Complete the test",
        status="pending",
    )

    # Mock _send_initial_prompt_with_retry to always fail
    # Patch the collaborator, not the delegate: create_agent_for_task runs
    # inside LaunchPipeline and calls its own _send_initial_prompt_with_retry
    # (launch_pipeline.py:771), so stubbing the AgentManager attribute never
    # intercepts (Phase 1b decomposition).
    agent_manager._launch._send_initial_prompt_with_retry = AsyncMock(
        side_effect=Exception(
            "Failed to deliver initial prompt to agent test-agent after 3 attempts"
        )
    )

    # Mock database query results
    mock_agent_record = Mock(spec=Agent)
    mock_agent_record.id = "test-agent-id"
    mock_agent_record.status = "working"
    mock_agent_record.tmux_session_name = "test-session"
    mock_task_record = Mock(spec=Task)
    mock_task_record.id = task.id
    mock_task_record.status = "pending"
    # Must be explicit: the post-launch reassignment guard aborts when
    # assigned_agent_id is truthy and != this agent's id, and an
    # unconfigured Mock attribute is truthy -- so the launch bailed out
    # before prompt delivery and the failure under test never happened.
    mock_task_record.assigned_agent_id = None

    mock_session = _make_session(
        agent_record=mock_agent_record, task_record=mock_task_record
    )

    _install_session(mock_db_manager, mock_session)

    # Try to create agent - should fail and clean up
    with pytest.raises(Exception) as exc_info:
        await agent_manager.create_agent_for_task(
            task=task, enriched_data={}, memories=[], project_context="Test context"
        )

    # Verify the exception was raised
    assert "Failed to deliver initial prompt" in str(exc_info.value)

    # Verify tmux session was killed
    tmux_session = mock_tmux_server.new_session.return_value
    tmux_session.kill_session.assert_called_once()

    # Verify database cleanup was attempted
    # Should get a new session for cleanup
    assert (
        mock_db_manager.get_session.call_count >= 2
    )  # Once for agent creation, once for cleanup

    # Verify agent was marked as terminated
    assert mock_agent_record.status == "terminated"

    # Verify task was marked as failed
    assert mock_task_record.status == "failed"
    assert "Agent creation failed" in mock_task_record.failure_reason
    assert mock_task_record.completed_at is not None

    # Verify session was committed and closed
    cleanup_session = mock_db_manager.get_session.return_value
    cleanup_session.commit.assert_called()
    cleanup_session.close.assert_called()


@pytest.mark.asyncio
async def test_cleanup_handles_database_errors_gracefully(
    mock_db_manager, mock_llm_provider, mock_worktree_manager, mock_tmux_server
):
    """Test that cleanup handles database errors gracefully and still raises original exception."""

    # Create agent manager with mocks
    agent_manager = AgentManager(
        db_manager=mock_db_manager, llm_provider=mock_llm_provider
    )

    # Replace worktree manager and tmux server with mocks
    agent_manager.branch_manager = mock_worktree_manager
    agent_manager.tmux_server = mock_tmux_server
    _disable_cli_fallback(agent_manager)

    # Create a mock task
    task = Task(
        id="test-task-123",
        raw_description="Test task",
        enriched_description="Test task enriched",
        done_definition="Complete the test",
        status="pending",
    )

    # Mock _send_initial_prompt_with_retry to always fail
    # Patch the collaborator, not the delegate: create_agent_for_task runs
    # inside LaunchPipeline and calls its own _send_initial_prompt_with_retry
    # (launch_pipeline.py:771), so stubbing the AgentManager attribute never
    # intercepts (Phase 1b decomposition).
    agent_manager._launch._send_initial_prompt_with_retry = AsyncMock(
        side_effect=Exception(
            "Failed to deliver initial prompt to agent test-agent after 3 attempts"
        )
    )

    # Mock database records
    mock_agent_record = Mock(spec=Agent)
    mock_agent_record.id = "test-agent-id"
    mock_agent_record.status = "working"
    mock_agent_record.tmux_session_name = "test-session"
    mock_task_record = Mock(spec=Task)
    mock_task_record.id = task.id
    mock_task_record.status = "pending"
    # Must be explicit: the post-launch reassignment guard aborts when
    # assigned_agent_id is truthy and != this agent's id, and an
    # unconfigured Mock attribute is truthy -- so the launch bailed out
    # before prompt delivery and the failure under test never happened.
    mock_task_record.assigned_agent_id = None

    # Guard session: returns None (no existing agent for this task)
    guard_query = Mock()
    guard_query.filter = Mock(return_value=guard_query)
    guard_query.first = Mock(return_value=None)
    guard_session = Mock()
    guard_session.__enter__ = Mock(return_value=guard_session)
    guard_session.__exit__ = Mock(return_value=False)
    guard_session.query = Mock(return_value=guard_query)

    # Main session for agent/task lookups
    mock_query = Mock()
    mock_query.filter_by = Mock(return_value=mock_query)
    mock_query.filter = Mock(return_value=mock_query)
    # terminate_agent's stray-task sweep calls .filter(...).all(); an
    # unconfigured Mock is not iterable, and the cleanup path swallows
    # that as "Failed to update database during cleanup" -- leaving the
    # task at its old status instead of the "failed" this test asserts.
    mock_query.all = Mock(return_value=[])
    mock_query.first = Mock(return_value=mock_task_record)
    mock_session = Mock()
    mock_session.__enter__ = Mock(return_value=mock_session)
    mock_session.__exit__ = Mock(return_value=False)
    mock_session.add = Mock()
    mock_session.commit = Mock()
    mock_session.rollback = Mock()
    mock_session.close = Mock()
    mock_session.query = Mock(return_value=mock_query)

    # Mock database: guards succeed, main session works, cleanup fails.
    # This list is positional, so every get_session() call in the path must
    # be accounted for -- Phase 2 §4.3 added the phase-sibling guard's own
    # session (launch_pipeline.py:1561), which without an entry here shifted
    # everything down one and raised the cleanup error before the launch had
    # even happened, so no tmux session existed to kill.
    sibling_guard_session = Mock()
    sibling_guard_session.__enter__ = Mock(return_value=sibling_guard_session)
    sibling_guard_session.__exit__ = Mock(return_value=False)
    sibling_guard_session.query = Mock(return_value=guard_query)
    sibling_guard_session.close = Mock()

    mock_db_manager.get_session.side_effect = [
        guard_session,  # duplicate-active-agent guard
        sibling_guard_session,  # phase-sibling guard (§4.3)
        mock_session,  # Main session (agent creation + prompt delivery)
        Exception("Database connection error"),  # Cleanup fails
    ]

    # Try to create agent - should fail and attempt cleanup
    with pytest.raises(Exception) as exc_info:
        await agent_manager.create_agent_for_task(
            task=task, enriched_data={}, memories=[], project_context="Test context"
        )

    # Verify an exception was raised (could be original or cleanup error)
    assert exc_info.value is not None

    # Verify tmux session was still killed despite database error
    tmux_session = mock_tmux_server.new_session.return_value
    tmux_session.kill_session.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_handles_tmux_kill_errors_gracefully(
    mock_db_manager, mock_llm_provider, mock_worktree_manager, mock_tmux_server
):
    """Test that cleanup continues even if tmux session kill fails."""

    # Create agent manager with mocks
    agent_manager = AgentManager(
        db_manager=mock_db_manager, llm_provider=mock_llm_provider
    )

    # Replace worktree manager and tmux server with mocks
    agent_manager.branch_manager = mock_worktree_manager
    agent_manager.tmux_server = mock_tmux_server
    _disable_cli_fallback(agent_manager)

    # Create a mock task
    task = Task(
        id="test-task-123",
        raw_description="Test task",
        enriched_description="Test task enriched",
        done_definition="Complete the test",
        status="pending",
    )

    # Mock _send_initial_prompt_with_retry to always fail
    # Patch the collaborator, not the delegate: create_agent_for_task runs
    # inside LaunchPipeline and calls its own _send_initial_prompt_with_retry
    # (launch_pipeline.py:771), so stubbing the AgentManager attribute never
    # intercepts (Phase 1b decomposition).
    agent_manager._launch._send_initial_prompt_with_retry = AsyncMock(
        side_effect=Exception(
            "Failed to deliver initial prompt to agent test-agent after 3 attempts"
        )
    )

    # Mock tmux session kill to raise an error
    tmux_session = mock_tmux_server.new_session.return_value
    tmux_session.kill_session = Mock(
        side_effect=Exception("Failed to kill tmux session")
    )

    # Guard session: returns None (no existing agent for this task)
    guard_query = Mock()
    guard_query.filter = Mock(return_value=guard_query)
    guard_query.first = Mock(return_value=None)
    guard_session = Mock()
    guard_session.__enter__ = Mock(return_value=guard_session)
    guard_session.__exit__ = Mock(return_value=False)
    guard_session.query = Mock(return_value=guard_query)

    # Main session for agent/task lookups and cleanup
    # first() is called many times — return a usable mock by default, with
    # the specific records for agent and task lookups.
    mock_agent_record = Mock(spec=Agent)
    mock_agent_record.id = "test-agent-id"
    mock_agent_record.status = "working"
    mock_agent_record.tmux_session_name = "test-session"
    mock_task_record = Mock(spec=Task)
    mock_task_record.id = task.id
    mock_task_record.status = "pending"
    # Must be explicit: the post-launch reassignment guard aborts when
    # assigned_agent_id is truthy and != this agent's id, and an
    # unconfigured Mock attribute is truthy -- so the launch bailed out
    # before prompt delivery and the failure under test never happened.
    mock_task_record.assigned_agent_id = None

    mock_query = Mock()
    mock_query.filter_by = Mock(return_value=mock_query)
    mock_query.filter = Mock(return_value=mock_query)
    # terminate_agent's stray-task sweep calls .filter(...).all(); an
    # unconfigured Mock is not iterable, and the cleanup path swallows
    # that as "Failed to update database during cleanup" -- leaving the
    # task at its old status instead of the "failed" this test asserts.
    mock_query.all = Mock(return_value=[])
    mock_query.first = Mock(return_value=mock_task_record)
    mock_session = Mock()
    mock_session.__enter__ = Mock(return_value=mock_session)
    mock_session.__exit__ = Mock(return_value=False)
    mock_session.add = Mock()
    mock_session.commit = Mock()
    mock_session.rollback = Mock()
    mock_session.close = Mock()
    mock_session.query = Mock(return_value=mock_query)

    # Cleanup session (third get_session call)
    cleanup_query = Mock()
    cleanup_query.filter_by = Mock(return_value=cleanup_query)
    cleanup_query.first = Mock(side_effect=[mock_agent_record, mock_task_record])
    cleanup_session = Mock()
    cleanup_session.__enter__ = Mock(return_value=cleanup_session)
    cleanup_session.__exit__ = Mock(return_value=False)
    cleanup_session.commit = Mock()
    cleanup_session.rollback = Mock()
    cleanup_session.close = Mock()
    cleanup_session.query = Mock(return_value=cleanup_query)

    # Use a function-based side_effect: guard_session for the first call
    # (guard check), mock_session for everything else (main + cleanup).
    _call_count = [0]

    def _get_session_side_effect():
        _call_count[0] += 1
        if _call_count[0] == 1:
            return guard_session
        return mock_session

    mock_db_manager.get_session = Mock(side_effect=_get_session_side_effect)

    # The agent-registration block uses session_scope() (SOLID review 3.10),
    # not get_session(), so it needs its own stand-in -- it's the "main"
    # session in the comment above, i.e. mock_session.
    @contextmanager
    def _scope():
        yield mock_session

    mock_db_manager.session_scope = _scope

    # Try to create agent - should fail and attempt cleanup
    with pytest.raises(Exception) as exc_info:
        await agent_manager.create_agent_for_task(
            task=task, enriched_data={}, memories=[], project_context="Test context"
        )

    # Verify the original exception was raised (not the tmux error)
    assert "Failed to deliver initial prompt" in str(exc_info.value)

    # Verify database cleanup still happened despite tmux error.
    # Both queries return mock_task_record (same mock), so task ends up "failed".
    assert mock_task_record.status == "failed"
