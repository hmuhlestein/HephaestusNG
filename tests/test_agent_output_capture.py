"""Tests for agent output capture on termination."""

import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.agents.manager import AgentManager
from src.core.database import Agent, AgentLog, DatabaseManager, Task


class TestAgentOutputCapture:
    """Test suite for agent output capture functionality."""

    @pytest.fixture
    def mock_db_manager(self):
        """Create a mock database manager."""
        db_manager = Mock(spec=DatabaseManager)
        return db_manager

    @pytest.fixture
    def mock_llm_provider(self):
        """Create a mock LLM provider."""
        llm_provider = Mock()
        llm_provider.generate_agent_prompt = AsyncMock(return_value="Test prompt")
        return llm_provider

    @pytest.fixture
    def mock_tmux_server(self):
        """Create a mock tmux server."""
        server = Mock()
        return server

    @pytest.fixture
    def agent_manager(self, mock_db_manager, mock_llm_provider, mock_tmux_server):
        """Create an agent manager with mocked dependencies."""
        return AgentManager(
            mock_db_manager, mock_llm_provider, tmux_server=mock_tmux_server
        )

    @pytest.mark.asyncio
    async def test_terminate_agent_captures_output(
        self, agent_manager, mock_db_manager, mock_tmux_server
    ):
        """Test that terminate_agent captures output before killing the session."""
        # Setup
        agent_id = str(uuid.uuid4())
        session_name = f"test_session_{agent_id[:8]}"
        test_output_lines = [
            "Line 1: Starting task",
            "Line 2: Processing...",
            "Line 3: Task completed successfully",
        ]

        # Create mock agent
        mock_agent = Mock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.tmux_session_name = session_name
        mock_agent.status = "working"
        mock_agent.current_task_id = None
        # Unset on a Mock(spec=Agent) defaults to a truthy Mock, and
        # terminate_agent subtracts it from a real datetime for the
        # message-delivery grace-period check -- TypeError.
        mock_agent.pending_message_sent_at = None

        # Create mock tmux session
        mock_tmux_session = Mock()
        mock_pane = Mock()
        mock_pane.cmd.return_value = Mock(stdout=test_output_lines)
        mock_tmux_session.attached_window.attached_pane = mock_pane

        # Setup database session mock — use a side_effect function so that
        # session.query(Agent) and session.query(Task) each get their own
        # properly-configured mock chain.
        mock_db_session = Mock()

        def _query_dispatch(model):
            q = Mock()
            if model is Agent:
                q.filter_by.return_value.first.return_value = mock_agent
            elif model is Task:
                q.filter_by.return_value.first.return_value = None
                q.filter_by.return_value.filter.return_value.all.return_value = []
            else:
                q.filter_by.return_value.first.return_value = None
                q.filter_by.return_value.filter.return_value.all.return_value = []
            return q

        mock_db_session.query.side_effect = _query_dispatch
        mock_db_session.add = Mock()
        mock_db_session.commit = Mock()
        mock_db_manager.get_session.return_value = mock_db_session

        # Setup tmux server mock
        mock_tmux_server.has_session.return_value = True
        mock_tmux_server.sessions = [mock_tmux_session]
        mock_tmux_session.name = session_name

        # Execute
        await agent_manager.terminate_agent(agent_id)

        # Verify output was captured via cmd("capture-pane", ...)
        mock_pane.cmd.assert_called()

        # Verify agent status was updated
        assert mock_agent.status == "terminated"

        # Verify AgentLog was created with output
        mock_db_session.add.assert_called_once()
        log_entry = mock_db_session.add.call_args[0][0]
        assert isinstance(log_entry, AgentLog)
        assert log_entry.agent_id == agent_id
        assert log_entry.log_type == "terminated"
        assert log_entry.details["final_output"] == "\n".join(test_output_lines)

        # Verify database commit
        mock_db_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminate_agent_handles_no_session(
        self, agent_manager, mock_db_manager, mock_tmux_server
    ):
        """Test that terminate_agent handles missing tmux session gracefully."""
        # Setup
        agent_id = str(uuid.uuid4())
        session_name = f"test_session_{agent_id[:8]}"

        # Create mock agent
        mock_agent = Mock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.tmux_session_name = session_name
        mock_agent.status = "working"
        mock_agent.current_task_id = None
        # Unset on a Mock(spec=Agent) defaults to a truthy Mock, and
        # terminate_agent subtracts it from a real datetime for the
        # message-delivery grace-period check -- TypeError.
        mock_agent.pending_message_sent_at = None

        # Setup database session mock
        mock_db_session = Mock()

        def _query_dispatch(model):
            q = Mock()
            if model is Agent:
                q.filter_by.return_value.first.return_value = mock_agent
            else:
                q.filter_by.return_value.first.return_value = None
                q.filter_by.return_value.filter.return_value.all.return_value = []
            return q

        mock_db_session.query.side_effect = _query_dispatch
        mock_db_session.add = Mock()
        mock_db_session.commit = Mock()
        mock_db_manager.get_session.return_value = mock_db_session

        # Setup tmux server mock - no session exists
        mock_tmux_server.has_session.return_value = False

        # Execute
        await agent_manager.terminate_agent(agent_id)

        # Verify agent status was still updated
        assert mock_agent.status == "terminated"

        # Verify AgentLog was created with empty output
        mock_db_session.add.assert_called_once()
        log_entry = mock_db_session.add.call_args[0][0]
        assert isinstance(log_entry, AgentLog)
        assert log_entry.agent_id == agent_id
        assert log_entry.log_type == "terminated"
        assert log_entry.details["final_output"] == ""

        # Verify database commit
        mock_db_session.commit.assert_called_once()

    def test_get_agent_output_retrieves_from_log_for_terminated(
        self, agent_manager, mock_db_manager
    ):
        """Test that get_agent_output retrieves from AgentLog for terminated agents."""
        # Setup
        agent_id = str(uuid.uuid4())
        stored_output = "This is the stored final output\nLine 2\nLine 3"

        # Create mock agent
        mock_agent = Mock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.status = "terminated"
        mock_agent.current_task_id = None  # No current task
        mock_agent.tmux_session_name = None

        # Create mock AgentLog with stored output
        mock_log = Mock(spec=AgentLog)
        mock_log.details = {
            "final_output": stored_output,
            "captured_at": datetime.utcnow().isoformat(),
        }

        # Setup database session mock
        mock_db_session = Mock()
        # Flow: 1) Agent query, 2) AgentLog query -- tmux_session_name is
        # None, so the clean-transcript/capture-pane check (and the raw
        # transcript's own Task lookup) never run; the termination-time
        # AgentLog is checked directly.
        mock_agent_query = Mock()
        mock_agent_query.filter_by.return_value.first.return_value = mock_agent

        mock_log_query = Mock()
        mock_log_query.filter_by.return_value.order_by.return_value.first.return_value = mock_log

        mock_db_session.query.side_effect = [
            mock_agent_query,  # Agent query
            mock_log_query,    # AgentLog query
        ]
        mock_db_manager.get_session.return_value = mock_db_session

        # Execute
        output = agent_manager.get_agent_output(agent_id)

        # Verify
        assert output == stored_output

    def test_get_agent_output_retrieves_last_n_lines_for_terminated(
        self, agent_manager, mock_db_manager
    ):
        """Test that get_agent_output respects lines parameter for terminated agents."""
        # Setup
        agent_id = str(uuid.uuid4())
        stored_output = "\n".join([f"Line {i}" for i in range(1, 11)])  # 10 lines

        # Create mock agent
        mock_agent = Mock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.status = "terminated"
        mock_agent.current_task_id = None  # No current task
        mock_agent.tmux_session_name = None

        # Create mock AgentLog with stored output
        mock_log = Mock(spec=AgentLog)
        mock_log.details = {
            "final_output": stored_output,
            "captured_at": datetime.utcnow().isoformat(),
        }

        # Setup database session mock
        mock_db_session = Mock()
        # Flow: 1) Agent query, 2) AgentLog query -- tmux_session_name is
        # None, so the clean-transcript/capture-pane check (and the raw
        # transcript's own Task lookup) never run; the termination-time
        # AgentLog is checked directly.
        mock_agent_query = Mock()
        mock_agent_query.filter_by.return_value.first.return_value = mock_agent

        mock_log_query = Mock()
        mock_log_query.filter_by.return_value.order_by.return_value.first.return_value = mock_log

        mock_db_session.query.side_effect = [
            mock_agent_query,  # Agent query
            mock_log_query,    # AgentLog query
        ]
        mock_db_manager.get_session.return_value = mock_db_session

        # Execute - request only last 5 lines
        output = agent_manager.get_agent_output(agent_id, lines=5)

        # Verify - should get last 5 lines
        expected_lines = ["Line 6", "Line 7", "Line 8", "Line 9", "Line 10"]
        assert output == "\n".join(expected_lines)

    def test_terminated_agent_evicts_live_backfill_cache_entry(
        self, agent_manager, mock_db_manager
    ):
        """Adversarial-review BLOCKER: _live_backfill_cache is a process-
        lifetime dict keyed by agent_id with no eviction -- a long-running
        backend accumulates one full transcript string per agent forever
        (textbook slow OOM). Once an agent is terminated its cache entry
        is never read again (only the live path reads it), so
        get_agent_output's terminated branch must evict it."""
        agent_id = str(uuid.uuid4())

        mock_agent = Mock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.status = "terminated"
        mock_agent.current_task_id = None
        mock_agent.tmux_session_name = None

        mock_log_query = Mock()
        mock_log_query.filter_by.return_value.order_by.return_value.first.return_value = None
        mock_recent_logs_query = Mock()
        mock_recent_logs_query.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value = []

        mock_db_session = Mock()
        mock_agent_query = Mock()
        mock_agent_query.filter_by.return_value.first.return_value = mock_agent
        mock_db_session.query.side_effect = [
            mock_agent_query,       # Agent query
            mock_log_query,         # termination AgentLog query
            mock_recent_logs_query,  # recent AgentLog fallback query
        ]
        mock_db_manager.get_session.return_value = mock_db_session

        agent_manager._output_capture._live_backfill_cache[agent_id] = "stale cached transcript"

        agent_manager.get_agent_output(agent_id)

        assert agent_id not in agent_manager._output_capture._live_backfill_cache

    def test_get_agent_output_from_tmux_for_active_agent(
        self, agent_manager, mock_db_manager, mock_tmux_server
    ):
        """Test that get_agent_output retrieves from tmux for active agents."""
        # Setup
        agent_id = str(uuid.uuid4())
        session_name = f"test_session_{agent_id[:8]}"
        test_output_lines = ["Active output line 1", "Active output line 2"]

        # Create mock agent (not terminated)
        mock_agent = Mock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.status = "working"
        mock_agent.tmux_session_name = session_name
        mock_agent.current_task_id = "task1"
        mock_agent.working_directory = None  # exercise the legacy task-lookup path below

        # Create mock tmux session
        mock_tmux_session = Mock()
        mock_pane = Mock()
        mock_pane.cmd.return_value = Mock(stdout=test_output_lines)
        mock_tmux_session.attached_window.attached_pane = mock_pane
        mock_tmux_session.name = session_name

        # Create mock task with workflow for _resolve_tmux_transcript_dir
        mock_task = Mock()
        mock_task.workflow.working_directory = "/tmp/nonexistent"
        mock_task.workflow.project_id = None

        # Setup database session mock
        mock_db_session = Mock()

        def _query_dispatch(model):
            q = Mock()
            if model is Agent:
                q.filter_by.return_value.first.return_value = mock_agent
            elif model is Task:
                q.filter_by.return_value.first.return_value = mock_task
            else:
                q.filter_by.return_value.first.return_value = None
            return q

        mock_db_session.query.side_effect = _query_dispatch
        mock_db_manager.get_session.return_value = mock_db_session

        # Setup tmux server mock
        mock_tmux_server.has_session.return_value = True
        mock_tmux_server.sessions = [mock_tmux_session]

        # Execute
        output = agent_manager.get_agent_output(agent_id, lines=200)

        # Verify output
        assert output == "\n".join(test_output_lines)

    def test_get_agent_output_handles_no_stored_output(
        self, agent_manager, mock_db_manager
    ):
        """Test that get_agent_output handles terminated agents with no stored output."""
        # Setup
        agent_id = str(uuid.uuid4())

        # Create mock agent
        mock_agent = Mock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.status = "terminated"
        mock_agent.current_task_id = None
        mock_agent.tmux_session_name = None

        # No AgentLog found
        mock_db_session = Mock()
        mock_db_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_agent
        )
        mock_db_session.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = None
        mock_db_session.query.return_value.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db_manager.get_session.return_value = mock_db_session

        # Execute
        output = agent_manager.get_agent_output(agent_id)

        # Verify
        assert output == "Agent terminated - no output was captured"

    @pytest.mark.asyncio
    async def test_terminate_agent_handles_output_capture_failure(
        self, agent_manager, mock_db_manager, mock_tmux_server
    ):
        """Test that terminate_agent handles output capture failure gracefully."""
        # Setup
        agent_id = str(uuid.uuid4())
        session_name = f"test_session_{agent_id[:8]}"

        # Create mock agent
        mock_agent = Mock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.tmux_session_name = session_name
        mock_agent.status = "working"
        mock_agent.current_task_id = None
        # Unset on a Mock(spec=Agent) defaults to a truthy Mock, and
        # terminate_agent subtracts it from a real datetime for the
        # message-delivery grace-period check -- TypeError.
        mock_agent.pending_message_sent_at = None

        # Create mock tmux session that fails to capture
        mock_tmux_session = Mock()
        mock_pane = Mock()
        mock_pane.cmd.side_effect = Exception("Failed to capture pane")
        mock_tmux_session.attached_window.attached_pane = mock_pane
        mock_tmux_session.name = session_name

        # Setup database session mock
        mock_db_session = Mock()

        def _query_dispatch(model):
            q = Mock()
            if model is Agent:
                q.filter_by.return_value.first.return_value = mock_agent
            else:
                q.filter_by.return_value.first.return_value = None
                q.filter_by.return_value.filter.return_value.all.return_value = []
            return q

        mock_db_session.query.side_effect = _query_dispatch
        mock_db_session.add = Mock()
        mock_db_session.commit = Mock()
        mock_db_manager.get_session.return_value = mock_db_session

        # Setup tmux server mock
        mock_tmux_server.has_session.return_value = True
        mock_tmux_server.sessions = [mock_tmux_session]

        # Execute
        await agent_manager.terminate_agent(agent_id)

        # Verify agent was still terminated despite capture failure
        assert mock_agent.status == "terminated"

        # Verify AgentLog was created with empty output
        mock_db_session.add.assert_called_once()
        log_entry = mock_db_session.add.call_args[0][0]
        assert log_entry.details["final_output"] == ""

        # Verify database commit
        mock_db_session.commit.assert_called_once()


class TestIsPaneDead:
    """AgentManager.is_pane_dead (delegates to OutputCapture.is_pane_dead) --
    the shared "is this session's pane actually dead" check relocated here
    from guardian_dispatch.py's own private copy so messenger.py and
    manager.py's send_recovery_keystrokes can reuse it too, now that
    remain-on-exit means has_session alone no longer implies "agent alive".
    """

    @pytest.fixture
    def mock_db_manager(self):
        return Mock(spec=DatabaseManager)

    @pytest.fixture
    def mock_llm_provider(self):
        llm_provider = Mock()
        llm_provider.generate_agent_prompt = AsyncMock(return_value="Test prompt")
        return llm_provider

    @pytest.fixture
    def mock_tmux_server(self):
        return Mock()

    @pytest.fixture
    def agent_manager(self, mock_db_manager, mock_llm_provider, mock_tmux_server):
        return AgentManager(
            mock_db_manager, mock_llm_provider, tmux_server=mock_tmux_server
        )

    def test_reads_pane_dead_format_variable(self, agent_manager, mock_tmux_server):
        mock_pane = Mock()
        mock_pane.cmd.return_value.stdout = ["1"]
        mock_window = Mock()
        mock_window.attached_pane = mock_pane
        mock_tmux_session = Mock()
        mock_tmux_session.name = "agent-tmux-1"
        mock_tmux_session.attached_window = mock_window
        mock_tmux_server.has_session.return_value = True
        mock_tmux_server.sessions = [mock_tmux_session]

        assert agent_manager.is_pane_dead("agent-tmux-1") is True
        mock_pane.cmd.assert_called_once_with("display-message", "-p", "#{pane_dead}")

    def test_false_when_session_missing(self, agent_manager, mock_tmux_server):
        mock_tmux_server.has_session.return_value = False

        assert agent_manager.is_pane_dead("agent-tmux-1") is False


class TestResolveTmuxTranscriptDirSurvivesTermination:
    """Regression: termination clears agent.current_task_id AND
    task.assigned_agent_id (the documented Agent.current_task_id
    invariant), which used to be _resolve_tmux_transcript_dir's ONLY way
    to find a terminated agent's workflow.working_directory. The tmux
    viewer showed nothing for every terminated agent as a result.

    The fix stores working_directory directly on the Agent row at
    creation time -- it's never cleared or reassigned, so termination
    can no longer break this lookup at all. The old task-based lookup
    is kept only as a legacy fallback for agents created before this
    column existed."""

    @pytest.fixture
    def db_manager(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
        db = DatabaseManager(str(db_path))
        db.create_tables()
        return db

    def test_reads_working_directory_directly_after_termination(self, db_manager, tmp_path):
        from src.agents.output_capture import AgentOutputCapture

        agent_id = str(uuid.uuid4())
        working_directory = str(tmp_path / "worktree")

        session = db_manager.get_session()
        session.add(Agent(
            id=agent_id, system_prompt="p", status="terminated",
            cli_type="pi", tmux_session_name="agent_test",
            working_directory=working_directory,
            current_task_id=None,  # cleared, as termination does
        ))
        session.commit()
        session.close()

        transcript_dir_on_disk = Path(working_directory) / ".hephaestus" / "tmux"
        transcript_dir_on_disk.mkdir(parents=True)
        (transcript_dir_on_disk / "agent_test.transcript.log").write_text("hi")

        capture = AgentOutputCapture(db_manager, tmux_server=Mock())
        agent = db_manager.get_session().query(Agent).filter_by(id=agent_id).first()

        transcript_dir = capture._resolve_tmux_transcript_dir(agent)

        assert transcript_dir == Path(working_directory) / ".hephaestus" / "tmux"

    def test_legacy_agent_without_working_directory_falls_back_to_task_lookup(
        self, db_manager, tmp_path
    ):
        """An agent created before the working_directory column existed
        has no value to read directly -- falls back to the old
        task->workflow.working_directory resolution."""
        from src.agents.output_capture import AgentOutputCapture
        from src.core.database import Workflow

        agent_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        working_directory = str(tmp_path / "worktree")

        session = db_manager.get_session()
        session.add(Workflow(
            id=workflow_id, name="wf", phases_folder_path="phases",
            working_directory=working_directory, status="active",
        ))
        session.add(Task(
            id=task_id, raw_description="do it", done_definition="done",
            status="in_progress", workflow_id=workflow_id,
            assigned_agent_id=agent_id,
        ))
        session.add(Agent(
            id=agent_id, system_prompt="p", status="working",
            cli_type="pi", tmux_session_name="agent_test",
            working_directory=None,  # pre-migration row
            current_task_id=task_id,
        ))
        session.commit()
        session.close()

        transcript_dir_on_disk = Path(working_directory) / ".hephaestus" / "tmux"
        transcript_dir_on_disk.mkdir(parents=True)
        (transcript_dir_on_disk / "agent_test.transcript.log").write_text("hi")

        capture = AgentOutputCapture(db_manager, tmux_server=Mock())
        agent = db_manager.get_session().query(Agent).filter_by(id=agent_id).first()

        transcript_dir = capture._resolve_tmux_transcript_dir(agent)

        assert transcript_dir == Path(working_directory) / ".hephaestus" / "tmux"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
