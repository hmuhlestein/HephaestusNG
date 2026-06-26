"""Tests for IntelligentMonitor — pure helpers and low-dependency methods."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from src.core.database import DatabaseManager, Agent, Task, AgentLog


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mock_db():
    return Mock(spec=DatabaseManager)


@pytest.fixture
def mock_agent_manager():
    mock = Mock()
    mock.tmux_server = Mock()
    mock.tmux_server.has_session.return_value = True
    mock.get_agent_output = Mock(return_value="Agent working on task...")
    mock.send_message_to_agent = AsyncMock()
    mock.send_recovery_keystrokes = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def mock_rag():
    mock = Mock()
    mock.retrieve_for_task = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def monitor(mock_db, mock_agent_manager, mock_llm, mock_rag):
    from src.monitoring.monitor import IntelligentMonitor
    with patch("src.monitoring.monitor.get_config") as mock_cfg:
        mock_cfg.return_value = Mock(
            stuck_detection_minutes=10,
            agent_timeout_minutes=60,
        )
        m = IntelligentMonitor(
            db_manager=mock_db,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm,
            rag_system=mock_rag,
        )
    return m


@pytest.fixture
def make_monitoring_loop(mock_db, mock_agent_manager, mock_llm, mock_rag):
    from src.monitoring.monitor import MonitoringLoop
    with patch("src.monitoring.monitor.get_config") as mock_cfg:
        mock_cfg.return_value = Mock(
            stuck_detection_minutes=10,
            agent_timeout_minutes=60,
        )
        ml = MonitoringLoop(
            db_manager=mock_db,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm,
            rag_system=mock_rag,
        )
    return ml


# ── _appears_stuck ────────────────────────────────────────────────


class TestAppearsStuck:
    def test_stuck_on_error(self, monitor):
        assert monitor._appears_stuck("Error: something broke") is True

    def test_stuck_on_failed(self, monitor):
        assert monitor._appears_stuck("Task failed to complete") is True

    def test_stuck_on_timeout(self, monitor):
        assert monitor._appears_stuck("Timeout exceeded") is True

    def test_stuck_on_rate_limit(self, monitor):
        assert monitor._appears_stuck("Rate limit hit") is True

    def test_stuck_on_waiting(self, monitor):
        assert monitor._appears_stuck("Waiting for input") is True

    def test_not_stuck_when_working(self, monitor):
        assert monitor._appears_stuck("Building the feature now") is False

    def test_not_stuck_empty_output(self, monitor):
        assert monitor._appears_stuck("") is False

    def test_case_insensitive(self, monitor):
        assert monitor._appears_stuck("ERROR occurred") is True
        assert monitor._appears_stuck("Stuck in loop") is True


# ── _is_agent_responsive ─────────────────────────────────────────


class TestIsAgentResponsive:
    def test_responsive_agent(self, make_monitoring_loop, mock_agent_manager):
        agent = Agent(
            id="a1", status="working", cli_type="claude",
            last_activity=datetime.utcnow() - timedelta(minutes=1),
            tmux_session_name="sess-a1",
        )
        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=False))
            assert make_monitoring_loop._is_agent_responsive(agent) is True

    def test_unresponsive_no_activity(self, make_monitoring_loop, mock_agent_manager):
        agent = Agent(
            id="a1", status="working", cli_type="claude",
            last_activity=datetime.utcnow() - timedelta(minutes=60),
            tmux_session_name="sess-a1",
        )
        assert make_monitoring_loop._is_agent_responsive(agent) is False

    def test_unresponsive_no_tmux_session(self, make_monitoring_loop, mock_agent_manager):
        mock_agent_manager.tmux_server.has_session.return_value = False
        agent = Agent(
            id="a1", status="working", cli_type="claude",
            last_activity=datetime.utcnow(),
            tmux_session_name="sess-a1",
        )
        assert make_monitoring_loop._is_agent_responsive(agent) is False

    def test_unresponsive_stuck_output(self, make_monitoring_loop, mock_agent_manager):
        agent = Agent(
            id="a1", status="working", cli_type="claude",
            last_activity=datetime.utcnow(),
            tmux_session_name="sess-a1",
        )
        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=True))
            assert make_monitoring_loop._is_agent_responsive(agent) is False

    def test_unresponsive_no_output(self, make_monitoring_loop, mock_agent_manager):
        mock_agent_manager.get_agent_output.return_value = ""
        agent = Agent(
            id="a1", status="working", cli_type="claude",
            last_activity=datetime.utcnow(),
            tmux_session_name="sess-a1",
        )
        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=False))
            assert make_monitoring_loop._is_agent_responsive(agent) is False

    def test_no_last_activity(self, make_monitoring_loop, mock_agent_manager):
        agent = Agent(
            id="a1", status="working", cli_type="claude",
            last_activity=None,
            tmux_session_name="sess-a1",
        )
        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=False))
            # No last_activity means the time check is skipped
            assert make_monitoring_loop._is_agent_responsive(agent) is True


# ── _is_task_timed_out ───────────────────────────────────────────


class TestIsTaskTimedOut:
    def test_not_timed_out(self, make_monitoring_loop, mock_db):
        session = Mock()
        task = Mock(started_at=datetime.utcnow() - timedelta(minutes=5), estimated_complexity=5)
        session.query.return_value.filter_by.return_value.first.return_value = task
        mock_db.get_session.return_value = session

        agent = Agent(id="a1", current_task_id="t1")
        assert make_monitoring_loop._is_task_timed_out(agent) is False

    def test_timed_out(self, make_monitoring_loop, mock_db):
        session = Mock()
        # 200 minutes on a complexity-5 task with 60min base timeout = 60*(1+5/10)=90 min
        task = Mock(started_at=datetime.utcnow() - timedelta(minutes=200), estimated_complexity=5)
        session.query.return_value.filter_by.return_value.first.return_value = task
        mock_db.get_session.return_value = session

        agent = Agent(id="a1", current_task_id="t1")
        assert make_monitoring_loop._is_task_timed_out(agent) is True

    def test_no_task(self, make_monitoring_loop, mock_db):
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        agent = Agent(id="a1", current_task_id="missing")
        assert make_monitoring_loop._is_task_timed_out(agent) is False

    def test_no_started_at(self, make_monitoring_loop, mock_db):
        session = Mock()
        task = Mock(started_at=None, estimated_complexity=5)
        session.query.return_value.filter_by.return_value.first.return_value = task
        mock_db.get_session.return_value = session

        agent = Agent(id="a1", current_task_id="t1")
        assert make_monitoring_loop._is_task_timed_out(agent) is False

    def test_high_complexity_longer_timeout(self, make_monitoring_loop, mock_db):
        session = Mock()
        # complexity=10 → timeout = 60*(1+10/10) = 120 min
        task = Mock(started_at=datetime.utcnow() - timedelta(minutes=100), estimated_complexity=10)
        session.query.return_value.filter_by.return_value.first.return_value = task
        mock_db.get_session.return_value = session

        agent = Agent(id="a1", current_task_id="t1")
        assert make_monitoring_loop._is_task_timed_out(agent) is False


# ── _get_past_summaries_for_agent ─────────────────────────────────


class TestGetPastSummaries:
    def test_returns_summaries(self, make_monitoring_loop, mock_db):
        session = Mock()
        # Mock GuardianAnalysis query to return empty (so fallback to AgentLog)
        session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        # Mock AgentLog query with reversed() support
        log1 = Mock(details={"trajectory_summary": "Good progress"})
        log2 = Mock(details={"trajectory_summary": "Almost done"})
        logs_result = [log1, log2]
        logs_result.reverse()  # Simulate reversed() in-place
        session.query.return_value.filter.side_effect = [
            Mock(order_by=Mock(return_value=Mock(limit=Mock(return_value=Mock(all=Mock(return_value=[])))))),  # GuardianAnalysis
            Mock(order_by=Mock(return_value=Mock(limit=Mock(return_value=Mock(all=Mock(return_value=logs_result)))))),  # AgentLog
        ]
        mock_db.get_session.return_value = session

        result = make_monitoring_loop._get_past_summaries_for_agent("a1", limit=5)
        assert len(result) == 2
        # After reverse, first should be "Good progress"
        assert result[0]["trajectory_summary"] == "Good progress"

    def test_returns_empty_when_no_logs(self, make_monitoring_loop, mock_db):
        session = Mock()
        # Both GuardianAnalysis and AgentLog return empty
        session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db.get_session.return_value = session

        result = make_monitoring_loop._get_past_summaries_for_agent("a1")
        assert result == []

    def test_skips_logs_without_details(self, make_monitoring_loop, mock_db):
        session = Mock()
        session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db.get_session.return_value = session

        result = make_monitoring_loop._get_past_summaries_for_agent("a1")
        assert result == []


# ── _build_spec_phase_output ──────────────────────────────────────


class TestBuildSpecPhaseOutput:
    def test_returns_empty_for_non_gated_phase(self, make_monitoring_loop):
        result = make_monitoring_loop._build_spec_phase_output("development")
        assert result == {}

    def test_returns_empty_when_no_workflow(self, make_monitoring_loop, mock_db):
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session
        make_monitoring_loop.phase_manager = Mock(workflow_id="wf-1")

        result = make_monitoring_loop._build_spec_phase_output("qa_validation")
        # workflow not found → wd is None → returns {}
        assert result == {}

    def test_returns_empty_on_exception(self, make_monitoring_loop, mock_db):
        session = Mock()
        session.query.return_value.filter_by.return_value.first.side_effect = Exception("DB error")
        mock_db.get_session.return_value = session
        make_monitoring_loop.phase_manager = Mock(workflow_id="wf-1")

        result = make_monitoring_loop._build_spec_phase_output("qa_validation")
        assert result == {}


# ── analyze_agent_state ───────────────────────────────────────────


class TestAnalyzeAgentState:
    @pytest.mark.asyncio
    async def test_returns_trajectory_analysis(self, monitor, mock_agent_manager):
        agent = Agent(
            id="a1", status="working", cli_type="claude",
            current_task_id="t1", tmux_session_name="sess-a1",
            last_activity=datetime.utcnow(),
        )
        mock_agent_manager.get_agent_output.return_value = "Building auth module"

        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=False))
            with patch("src.monitoring.monitor.TrajectoryContext") as mock_tc:
                mock_tc.return_value.build_accumulated_context.return_value = {
                    "overall_goal": "Build auth",
                    "constraints": [],
                    "current_focus": "implementing",
                }
                monitor.llm_provider.analyze_agent_state = AsyncMock(return_value={
                    "state": "healthy",
                    "decision": "continue",
                    "message": "",
                    "reasoning": "On track",
                    "confidence": 0.9,
                })
                result = await monitor.analyze_agent_state(agent)

        assert result["state"] == "healthy"
        assert result["decision"] == "continue"

    @pytest.mark.asyncio
    async def test_handles_llm_failure(self, monitor, mock_agent_manager):
        agent = Agent(
            id="a1", status="working", cli_type="claude",
            current_task_id="t1", tmux_session_name="sess-a1",
            last_activity=datetime.utcnow(),
        )
        mock_agent_manager.get_agent_output.return_value = "Building auth module"

        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=False))
            with patch("src.monitoring.monitor.TrajectoryContext") as mock_tc:
                mock_tc.return_value.build_accumulated_context.return_value = {}
                monitor.llm_provider.analyze_agent_state = AsyncMock(side_effect=Exception("LLM error"))
                result = await monitor.analyze_agent_state(agent)

        # Falls back to healthy
        assert result["state"] == "healthy"
        assert result["confidence"] == 0.1
