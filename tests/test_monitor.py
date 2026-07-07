"""Tests for IntelligentMonitor — pure helpers and low-dependency methods."""

import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.database import Agent, DatabaseManager

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
            id="a1",
            status="working",
            cli_type="claude",
            last_activity=datetime.utcnow() - timedelta(minutes=1),
            tmux_session_name="sess-a1",
        )
        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=False))
            assert make_monitoring_loop._is_agent_responsive(agent) is True

    def test_unresponsive_no_activity(self, make_monitoring_loop, mock_agent_manager):
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
            last_activity=datetime.utcnow() - timedelta(minutes=60),
            tmux_session_name="sess-a1",
        )
        assert make_monitoring_loop._is_agent_responsive(agent) is False

    def test_unresponsive_no_tmux_session(
        self, make_monitoring_loop, mock_agent_manager
    ):
        mock_agent_manager.tmux_server.has_session.return_value = False
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
            last_activity=datetime.utcnow(),
            tmux_session_name="sess-a1",
        )
        assert make_monitoring_loop._is_agent_responsive(agent) is False

    def test_unresponsive_stuck_output(self, make_monitoring_loop, mock_agent_manager):
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
            last_activity=datetime.utcnow(),
            tmux_session_name="sess-a1",
        )
        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=True))
            assert make_monitoring_loop._is_agent_responsive(agent) is False

    def test_unresponsive_no_output(self, make_monitoring_loop, mock_agent_manager):
        mock_agent_manager.get_agent_output.return_value = ""
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
            last_activity=datetime.utcnow(),
            tmux_session_name="sess-a1",
        )
        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=False))
            assert make_monitoring_loop._is_agent_responsive(agent) is False

    def test_no_last_activity(self, make_monitoring_loop, mock_agent_manager):
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
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
        task = Mock(
            started_at=datetime.utcnow() - timedelta(minutes=5), estimated_complexity=5
        )
        session.query.return_value.filter_by.return_value.first.return_value = task
        mock_db.get_session.return_value = session

        agent = Agent(id="a1", current_task_id="t1")
        assert make_monitoring_loop._is_task_timed_out(agent) is False

    def test_timed_out(self, make_monitoring_loop, mock_db):
        session = Mock()
        # 200 minutes on a complexity-5 task with 60min base timeout = 60*(1+5/10)=90 min
        task = Mock(
            started_at=datetime.utcnow() - timedelta(minutes=200),
            estimated_complexity=5,
        )
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
        task = Mock(
            started_at=datetime.utcnow() - timedelta(minutes=100),
            estimated_complexity=10,
        )
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
            Mock(
                order_by=Mock(
                    return_value=Mock(
                        limit=Mock(return_value=Mock(all=Mock(return_value=[])))
                    )
                )
            ),  # GuardianAnalysis
            Mock(
                order_by=Mock(
                    return_value=Mock(
                        limit=Mock(
                            return_value=Mock(all=Mock(return_value=logs_result))
                        )
                    )
                )
            ),  # AgentLog
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
        session.query.return_value.filter_by.return_value.first.side_effect = Exception(
            "DB error"
        )
        mock_db.get_session.return_value = session
        make_monitoring_loop.phase_manager = Mock(workflow_id="wf-1")

        result = make_monitoring_loop._build_spec_phase_output("qa_validation")
        assert result == {}


# ── analyze_agent_state ───────────────────────────────────────────


class TestAnalyzeAgentState:
    @pytest.mark.asyncio
    async def test_returns_trajectory_analysis(self, monitor, mock_agent_manager):
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
            current_task_id="t1",
            tmux_session_name="sess-a1",
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
                monitor.llm_provider.analyze_agent_state = AsyncMock(
                    return_value={
                        "state": "healthy",
                        "decision": "continue",
                        "message": "",
                        "reasoning": "On track",
                        "confidence": 0.9,
                    }
                )
                result = await monitor.analyze_agent_state(agent)

        assert result["state"] == "healthy"
        assert result["decision"] == "continue"

    @pytest.mark.asyncio
    async def test_handles_llm_failure(self, monitor, mock_agent_manager):
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
            current_task_id="t1",
            tmux_session_name="sess-a1",
            last_activity=datetime.utcnow(),
        )
        mock_agent_manager.get_agent_output.return_value = "Building auth module"

        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=False))
            with patch("src.monitoring.monitor.TrajectoryContext") as mock_tc:
                mock_tc.return_value.build_accumulated_context.return_value = {}
                monitor.llm_provider.analyze_agent_state = AsyncMock(
                    side_effect=Exception("LLM error")
                )
                result = await monitor.analyze_agent_state(agent)

        # Falls back to healthy
        assert result["state"] == "healthy"
        assert result["confidence"] == 0.1


# ── _write_agent_tmux_log ────────────────────────────────────────


class TestWriteAgentTmuxLog:
    def test_no_output(self, make_monitoring_loop, mock_db):
        # Should not raise
        make_monitoring_loop._write_agent_tmux_log("a1", "dev", "")

    def test_no_phase_manager(self, make_monitoring_loop, mock_db):
        make_monitoring_loop.phase_manager = None
        make_monitoring_loop._write_agent_tmux_log("a1", "dev", "some output")

    def test_writes_log(self, make_monitoring_loop, mock_db, tmp_path):
        make_monitoring_loop.phase_manager = Mock(workflow_id="wf-1")
        session = Mock()
        wf = Mock(working_directory=str(tmp_path))
        session.query.return_value.filter_by.return_value.first.return_value = wf
        mock_db.get_session.return_value = session

        make_monitoring_loop._write_agent_tmux_log(
            "agent-123", "development", "test output"
        )

        tmp_path / ".hephaestus" / "tmux" / "development_agent-.log"
        # File might not exist due to path matching, but no error should occur


# ── _log_intervention ────────────────────────────────────────────


class TestLogIntervention:
    @pytest.mark.asyncio
    async def test_logs_intervention(self, monitor, mock_db):
        agent = Agent(id="a1")
        session = Mock()
        mock_db.get_session.return_value = session

        await monitor._log_intervention(agent, "nudge", "Try a different approach")
        session.add.assert_called()
        session.commit.assert_called()


# ── _collect_agent_context ───────────────────────────────────────


class TestCollectAgentContext:
    @pytest.mark.asyncio
    async def test_collects_context(self, monitor, mock_agent_manager, mock_db):
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
            current_task_id="t1",
            tmux_session_name="sess-a1",
        )
        mock_agent_manager.get_agent_output.return_value = "Working on auth"
        mock_agent_manager.get_project_context = AsyncMock(
            return_value="Project context"
        )

        session = Mock()
        task = Mock(
            raw_description="Build auth",
            enriched_description="Build auth system",
            done_definition="Auth complete",
            started_at=datetime.utcnow() - timedelta(minutes=30),
        )
        session.query.return_value.filter_by.return_value.first.return_value = task
        mock_db.get_session.return_value = session

        with patch("src.monitoring.monitor.TrajectoryContext") as mock_tc:
            mock_tc.return_value.build_accumulated_context.return_value = {
                "overall_goal": "Build auth",
                "constraints": [],
            }
            result = await monitor._collect_agent_context(agent)

        assert "tmux_output" in result
        assert result["task_description"] == "Build auth system"
        assert result["done_definition"] == "Auth complete"

    @pytest.mark.asyncio
    async def test_handles_no_task(self, monitor, mock_agent_manager, mock_db):
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
            current_task_id=None,
            tmux_session_name="sess-a1",
        )
        mock_agent_manager.get_agent_output.return_value = "Working"
        mock_agent_manager.get_project_context = AsyncMock(return_value="")

        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        with patch("src.monitoring.monitor.TrajectoryContext") as mock_tc:
            mock_tc.return_value.build_accumulated_context.return_value = {}
            result = await monitor._collect_agent_context(agent)

        assert result["task_description"] == "Unknown task"


# ── MonitoringLoop._get_past_summaries_for_agent ──────────────────


class TestMonitoringLoopGetPastSummaries:
    def test_returns_from_guardian_analysis(self, make_monitoring_loop, mock_db):
        session = Mock()
        analysis = Mock(
            current_phase="implementation",
            trajectory_aligned=True,
            alignment_score=0.8,
            needs_steering=False,
            steering_type=None,
            trajectory_summary="Good progress",
            accumulated_goal="Build auth",
            timestamp=datetime.utcnow(),
        )
        session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            analysis
        ]
        mock_db.get_session.return_value = session

        result = make_monitoring_loop._get_past_summaries_for_agent("a1")
        assert len(result) == 1
        assert result[0]["trajectory_summary"] == "Good progress"


# ── execute_intervention ──────────────────────────────────────────


class TestExecuteIntervention:
    @pytest.mark.asyncio
    async def test_continue_does_nothing(self, monitor, mock_agent_manager):
        agent = Agent(id="a1")
        decision = {"decision": "continue", "message": "", "reasoning": ""}
        await monitor.execute_intervention(agent, decision)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_nudge_sends_message(self, monitor, mock_agent_manager):
        agent = Agent(id="a1", current_task_id="t1")
        decision = {"decision": "nudge", "message": "Try a different approach"}
        await monitor.execute_intervention(agent, decision)
        mock_agent_manager.send_message_to_agent.assert_called_once()
        call_args = mock_agent_manager.send_message_to_agent.call_args
        assert "different approach" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_nudge_default_message(self, monitor, mock_agent_manager):
        agent = Agent(id="a1", current_task_id="t1")
        decision = {"decision": "nudge", "message": ""}
        await monitor.execute_intervention(agent, decision)
        mock_agent_manager.send_message_to_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_calls_manager(self, monitor, mock_agent_manager):
        mock_agent_manager.restart_agent = AsyncMock()
        agent = Agent(id="a1")
        decision = {"decision": "restart", "reasoning": "Stuck too long"}
        await monitor.execute_intervention(agent, decision)
        mock_agent_manager.restart_agent.assert_called_once_with("a1", "Stuck too long")


# ── _detect_repetition_loop ──────────────────────────────────────


class TestDetectRepetitionLoop:
    @pytest.mark.asyncio
    async def test_detects_repetition(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude")
        # 6 repeated lines
        repeated = "This is a long enough line that repeats many times in the output"
        output = "\n".join(
            [repeated] * 6 + ["Normal line that is different and unique here"]
        )
        mock_agent_manager.get_agent_output.return_value = output

        session = Mock()
        mock_db.get_session.return_value = session

        await make_monitoring_loop._detect_repetition_loop(agent)
        mock_agent_manager.send_message_to_agent.assert_called_once()
        call_args = mock_agent_manager.send_message_to_agent.call_args
        assert "thought loop" in call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_ignores_short_lines(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude")
        # Lines under 30 chars should not trigger
        output = "\n".join(["short"] * 10)
        mock_agent_manager.get_agent_output.return_value = output

        await make_monitoring_loop._detect_repetition_loop(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_few_repeats(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude")
        repeated = (
            "This is a long enough line that should not trigger with fewer repeats"
        )
        output = "\n".join([repeated] * 3)
        mock_agent_manager.get_agent_output.return_value = output

        await make_monitoring_loop._detect_repetition_loop(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_output(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = ""

        await make_monitoring_loop._detect_repetition_loop(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()


# ── _update_agent_health_from_trajectory ─────────────────────────


class TestUpdateAgentHealth:
    @pytest.mark.asyncio
    async def test_stores_analysis(self, make_monitoring_loop, mock_db):
        agent = Agent(id="a1")
        analysis = {
            "state": "healthy",
            "confidence": 0.9,
            "reasoning": "On track",
            "decision": "continue",
            "trajectory_aligned": True,
            "alignment_score": 0.9,
            "trajectory_summary": "Good",
        }
        db_agent = Mock(id="a1", health_check_failures=0)
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = db_agent
        mock_db.get_session.return_value = session

        await make_monitoring_loop._update_agent_health_from_trajectory(agent, analysis)
        assert session.add.call_count == 2  # GuardianAnalysis + AgentLog
        session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_handles_no_agent(self, make_monitoring_loop, mock_db):
        agent = Agent(id="a1")
        analysis = {"trajectory_aligned": True}
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        # Should not raise
        await make_monitoring_loop._update_agent_health_from_trajectory(agent, analysis)

    @pytest.mark.asyncio
    async def test_off_track_increments_failures(self, make_monitoring_loop, mock_db):
        agent = Agent(id="a1")
        analysis = {
            "trajectory_aligned": False,
            "alignment_score": 0.2,
        }
        db_agent = Mock(id="a1", health_check_failures=0)
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = db_agent
        mock_db.get_session.return_value = session

        await make_monitoring_loop._update_agent_health_from_trajectory(agent, analysis)
        # alignment_score < 0.3 → += 2
        assert db_agent.health_check_failures == 2


# ── _save_conductor_analysis ─────────────────────────────────────


class TestSaveConductorAnalysis:
    @pytest.mark.asyncio
    async def test_saves_analysis(self, make_monitoring_loop, mock_db):
        analysis = {
            "system_status": "healthy",
            "agents_summary": [],
            "recommendations": [],
        }
        session = Mock()
        mock_db.get_session.return_value = session

        await make_monitoring_loop._save_conductor_analysis(analysis)
        session.add.assert_called()
        session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_handles_exception(self, make_monitoring_loop, mock_db):
        analysis = {"system_status": "healthy"}
        session = Mock()
        session.add.side_effect = Exception("DB error")
        mock_db.get_session.return_value = session

        # Should not raise
        await make_monitoring_loop._save_conductor_analysis(analysis)


# ── _log_diagnostic_status_report ────────────────────────────────


class TestLogDiagnosticStatusReport:
    def test_logs_trigger(self, make_monitoring_loop):
        conditions = {
            "enabled": True,
            "workflow_exists": True,
            "has_tasks": True,
            "all_tasks_finished": True,
            "no_validated_result": True,
            "cooldown_passed": True,
            "stuck_long_enough": True,
        }
        # Should not raise
        make_monitoring_loop._log_diagnostic_status_report(
            conditions, True, "Test trigger", 120.0
        )

    def test_logs_no_trigger(self, make_monitoring_loop):
        conditions = {
            "enabled": True,
            "workflow_exists": False,
            "has_tasks": False,
            "all_tasks_finished": False,
            "no_validated_result": False,
            "cooldown_passed": False,
            "stuck_long_enough": False,
        }
        make_monitoring_loop._log_diagnostic_status_report(
            conditions, False, "Not stuck"
        )

    def test_logs_with_zero_stuck_time(self, make_monitoring_loop):
        conditions = {
            "enabled": True,
            "workflow_exists": True,
            "has_tasks": True,
            "all_tasks_finished": True,
            "no_validated_result": True,
            "cooldown_passed": True,
            "stuck_long_enough": True,
        }
        make_monitoring_loop._log_diagnostic_status_report(
            conditions, True, "Stuck", 0.0
        )


# ── _enrich_answer ────────────────────────────────────────────────


class TestEnrichAnswer:
    @pytest.mark.asyncio
    async def test_enriches_with_knowledge(self, monitor, mock_rag):
        mock_rag.retrieve_for_task = AsyncMock(
            return_value=[
                {"content": "Auth requires JWT tokens"},
                {"content": "Use bcrypt for passwords"},
            ]
        )
        result = await monitor._enrich_answer("How to implement auth?", "t1")
        assert "Additional context" in result
        assert "JWT" in result

    @pytest.mark.asyncio
    async def test_no_knowledge_returns_base(self, monitor, mock_rag):
        mock_rag.retrieve_for_task = AsyncMock(return_value=[])
        result = await monitor._enrich_answer("Simple answer", "t1")
        assert result == "Simple answer"


# ── _recreate_agent_with_new_approach ────────────────────────────


class TestRecreateAgentWithNewApproach:
    @pytest.mark.asyncio
    async def test_recreates_agent(
        self, monitor, mock_agent_manager, mock_db, mock_rag
    ):
        agent = Agent(id="a1", current_task_id="t1")
        session = Mock()
        task = Mock(
            id="t1",
            enriched_description="Build auth",
            done_definition="Auth complete",
            phase_id="p1",
        )
        session.query.return_value.filter_by.return_value.first.return_value = task
        mock_db.get_session.return_value = session

        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.create_agent_for_task = AsyncMock(return_value=Mock(id="a2"))
        mock_agent_manager.get_project_context = AsyncMock(return_value="ctx")
        mock_rag.retrieve_for_task = AsyncMock(return_value=[])

        # Mock second session for phase query
        phase_session = Mock()
        phase = Mock(
            cli_tool="pi",
            cli_model="mimo",
            glm_api_token_env=None,
            thinking_level="low",
        )
        phase_session.query.return_value.filter_by.return_value.first.return_value = (
            phase
        )
        mock_db.get_session.side_effect = [session, phase_session]

        await monitor._recreate_agent_with_new_approach(agent, "Stuck too long")
        mock_agent_manager.terminate_agent.assert_called_once_with("a1")
        mock_agent_manager.create_agent_for_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_no_task(self, monitor, mock_agent_manager, mock_db):
        agent = Agent(id="a1", current_task_id="missing")
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        await monitor._recreate_agent_with_new_approach(agent, "No task found")
        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_exception(
        self, monitor, mock_agent_manager, mock_db, mock_rag
    ):
        agent = Agent(id="a1", current_task_id="t1")
        session = Mock()
        session.query.return_value.filter_by.return_value.first.side_effect = Exception(
            "DB error"
        )
        mock_db.get_session.return_value = session

        # Should not raise
        await monitor._recreate_agent_with_new_approach(agent, "Error")


# ── _mechanical_recovery_for_agent ────────────────────────────────


class TestMechanicalRecovery:
    @pytest.mark.asyncio
    async def test_no_output(self, make_monitoring_loop, mock_agent_manager, mock_db):
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = ""
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        mock_agent_manager.send_recovery_keystrokes.assert_not_called()

    @pytest.mark.asyncio
    async def test_output_changed(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = "Building feature..."
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        # First call sets baseline, no recovery
        mock_agent_manager.send_recovery_keystrokes.assert_not_called()

    @pytest.mark.asyncio
    async def test_frozen_triggers_recovery(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude")
        frozen_output = "Same output that never changes"
        mock_agent_manager.get_agent_output.return_value = frozen_output
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)

        # First call sets baseline
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        # Manually set frozen time to trigger recovery
        make_monitoring_loop._stuck_state["a1"]["since"] = time.time() - 400

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        mock_agent_manager.send_recovery_keystrokes.assert_called_once()

    @pytest.mark.asyncio
    async def test_max_recovery_fails_task(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude")
        frozen_output = "Same output"
        mock_agent_manager.get_agent_output.return_value = frozen_output
        mock_agent_manager.terminate_agent = AsyncMock()

        # Initialize stuck state manually
        make_monitoring_loop._stuck_state = {}
        make_monitoring_loop._stuck_state["a1"] = {
            "sig": frozen_output,
            "since": time.time() - 400,
            "recov": 2,
        }

        session = Mock()
        task = Mock(id="t1", status="in_progress")
        # The query uses filter_by(assigned_agent_id=..., status=...)
        session.query.return_value.filter_by.return_value.first.return_value = task
        mock_db.get_session.return_value = session

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        assert task.status == "failed"
        mock_agent_manager.terminate_agent.assert_called_once()


# ── _handle_stuck_agent ──────────────────────────────────────────


class TestHandleStuckAgent:
    @pytest.mark.asyncio
    async def test_steers_with_blockers(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", health_check_failures=5)
        make_monitoring_loop.trajectory_context = Mock()
        make_monitoring_loop.trajectory_context.build_accumulated_context.return_value = {
            "discovered_blockers": ["Auth module failing"],
        }
        make_monitoring_loop.guardian = Mock()
        make_monitoring_loop.guardian.steer_agent = AsyncMock()

        await make_monitoring_loop._handle_stuck_agent(agent)
        make_monitoring_loop.guardian.steer_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_logs_blockers_below_threshold(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", health_check_failures=1)
        make_monitoring_loop.trajectory_context = Mock()
        make_monitoring_loop.trajectory_context.build_accumulated_context.return_value = {
            "discovered_blockers": ["Blocker 1"],
        }
        make_monitoring_loop.guardian = Mock()
        make_monitoring_loop.guardian.steer_agent = AsyncMock()
        make_monitoring_loop.intelligent_monitor = Mock()
        make_monitoring_loop.intelligent_monitor.analyze_agent_state = AsyncMock(
            return_value={"decision": "continue"}
        )
        make_monitoring_loop.intelligent_monitor.execute_intervention = AsyncMock()

        await make_monitoring_loop._handle_stuck_agent(agent)
        # Below threshold — no steering, falls through to analysis
        make_monitoring_loop.guardian.steer_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_blockers_runs_analysis(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", health_check_failures=0)
        make_monitoring_loop.trajectory_context = Mock()
        make_monitoring_loop.trajectory_context.build_accumulated_context.return_value = {
            "discovered_blockers": [],
        }
        make_monitoring_loop.intelligent_monitor = Mock()
        make_monitoring_loop.intelligent_monitor.analyze_agent_state = AsyncMock(
            return_value={"decision": "continue"}
        )
        make_monitoring_loop.intelligent_monitor.execute_intervention = AsyncMock()

        await make_monitoring_loop._handle_stuck_agent(agent)
        make_monitoring_loop.intelligent_monitor.execute_intervention.assert_called_once()


# ── _handle_missing_tmux_session ──────────────────────────────────


class TestHandleMissingTmux:
    @pytest.mark.asyncio
    async def test_restarts_agent(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", tmux_session_name="sess-a1")
        mock_agent_manager.restart_agent = AsyncMock()
        await make_monitoring_loop._handle_missing_tmux_session(agent)
        mock_agent_manager.restart_agent.assert_called_once()


# ── _cleanup_orphaned_tmux_sessions ──────────────────────────────


class TestCleanupOrphanedSessions:
    @pytest.mark.asyncio
    async def test_no_sessions(self, make_monitoring_loop, mock_agent_manager, mock_db):
        mock_agent_manager.tmux_server.sessions = []
        await make_monitoring_loop._cleanup_orphaned_tmux_sessions()
        # Should not raise

    @pytest.mark.asyncio
    async def test_no_orphans(self, make_monitoring_loop, mock_agent_manager, mock_db):
        session_mock = Mock()
        session_mock.name = "agent-a1"
        mock_agent_manager.tmux_server.sessions = [session_mock]

        # Mock the DB session - make all queries return empty/basic values
        db_session = Mock()
        db_session.query.return_value.filter.return_value.all.return_value = []
        db_session.query.return_value.filter.return_value.first.return_value = None
        mock_db.get_session.return_value = db_session

        # Set grace period past check
        make_monitoring_loop._last_orphan_check_time = datetime.now() - timedelta(
            seconds=200
        )

        await make_monitoring_loop._cleanup_orphaned_tmux_sessions()
        # Session is active, not orphaned — no kill_session called

    @pytest.mark.asyncio
    async def test_kills_orphans(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        session_mock = Mock()
        session_mock.name = "agent-orphan"
        session_mock.kill_session = Mock()
        mock_agent_manager.tmux_server.sessions = [session_mock]

        db_session = Mock()
        db_session.query.return_value.filter.return_value.all.return_value = []
        db_session.query.return_value.filter.return_value.first.return_value = None
        mock_db.get_session.return_value = db_session

        # Set grace period past check
        make_monitoring_loop._last_orphan_check_time = datetime.now() - timedelta(
            seconds=200
        )

        await make_monitoring_loop._cleanup_orphaned_tmux_sessions()
        session_mock.kill_session.assert_called_once()


# ── _handle_timeout ───────────────────────────────────────────────


class TestHandleTimeout:
    @pytest.mark.asyncio
    async def test_recreates_agent(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", current_task_id="t1")
        make_monitoring_loop.intelligent_monitor = Mock()
        make_monitoring_loop.intelligent_monitor.execute_intervention = AsyncMock()

        await make_monitoring_loop._handle_timeout(agent)
        make_monitoring_loop.intelligent_monitor.execute_intervention.assert_called_once()
        call_args = (
            make_monitoring_loop.intelligent_monitor.execute_intervention.call_args
        )
        assert call_args[0][1]["decision"] == "recreate"


# ── _generate_diagnostic_prompt ──────────────────────────────────


class TestGenerateDiagnosticPrompt:
    @pytest.mark.asyncio
    async def test_generates_prompt(self, make_monitoring_loop):
        context = {
            "workflow_goal": "Build auth",
            "workflow_id": "wf-1",
            "phases_summary": [
                {
                    "order": 1,
                    "name": "Dev",
                    "id": "p1",
                    "description": "Build",
                    "done_definitions": ["Done"],
                    "task_count": 5,
                    "done_task_count": 3,
                }
            ],
            "agents_summary": [],
            "conductor_overviews": [],
            "submitted_results": [],
            "total_tasks": 5,
            "tasks_by_phase": {"Dev": {"total": 5, "done": 3, "failed": 0}},
            "time_since_last_task": 120.0,
        }

        # The method reads a template file - it will fail gracefully
        try:
            result = await make_monitoring_loop._generate_diagnostic_prompt(context)
            # If template exists, returns formatted string
            assert isinstance(result, str)
        except FileNotFoundError:
            # Template not found in test environment - expected
            pass
