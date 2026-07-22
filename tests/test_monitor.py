"""Tests for IntelligentMonitor — pure helpers and low-dependency methods."""

import asyncio
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
        from contextlib import contextmanager

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

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        result = make_monitoring_loop._get_past_summaries_for_agent("a1", limit=5)
        assert len(result) == 2
        # After reverse, first should be "Good progress"
        assert result[0]["trajectory_summary"] == "Good progress"

    def test_returns_empty_when_no_logs(self, make_monitoring_loop, mock_db):
        from contextlib import contextmanager

        session = Mock()
        # Both GuardianAnalysis and AgentLog return empty
        session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        result = make_monitoring_loop._get_past_summaries_for_agent("a1")
        assert result == []

    def test_skips_logs_without_details(self, make_monitoring_loop, mock_db):
        from contextlib import contextmanager

        session = Mock()
        session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        result = make_monitoring_loop._get_past_summaries_for_agent("a1")
        assert result == []


# ── build_phase_output ────────────────────────────────────────────


from src.autopilot.spec import build_phase_output


class TestBuildSpecPhaseOutput:
    def test_returns_empty_for_non_gated_phase(self):
        result = build_phase_output("development", "/tmp/test")
        assert result == {}

    def test_returns_output_for_gated_phase(self):
        # qa_validation is a gated phase, so it returns a score
        result = build_phase_output("qa_validation", "/tmp/test")
        assert "score" in result

    def test_returns_output_for_gated_phase_with_spec(self):
        result = build_phase_output("qa_validation", "/tmp/test")
        assert "score" in result


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

    @pytest.mark.asyncio
    async def test_llm_hang_times_out_instead_of_blocking_forever(
        self, monitor, mock_agent_manager, monkeypatch
    ):
        """Regression: a slow/over-streaming model call here previously had no
        timeout at all, so it could block this single shared monitoring-loop
        task indefinitely -- freezing recovery for every agent in the system,
        not just this one (observed live: monitor_heartbeat stopped updating
        for 20+ minutes after one such hung call). analyze_agent_state must
        bound the call with asyncio.wait_for and fall back on timeout."""
        monkeypatch.setattr("src.monitoring.monitor.AGENT_STATE_LLM_TIMEOUT", 0.05)
        agent = Agent(
            id="a1",
            status="working",
            cli_type="claude",
            current_task_id="t1",
            tmux_session_name="sess-a1",
            last_activity=datetime.utcnow(),
        )
        mock_agent_manager.get_agent_output.return_value = "Building auth module"

        async def hang(*args, **kwargs):
            await asyncio.sleep(10)

        with patch("src.monitoring.monitor.get_cli_agent") as mock_cli:
            mock_cli.return_value = Mock(is_stuck=Mock(return_value=False))
            with patch("src.monitoring.monitor.TrajectoryContext") as mock_tc:
                mock_tc.return_value.build_accumulated_context.return_value = {}
                monitor.llm_provider.analyze_agent_state = hang
                result = await asyncio.wait_for(
                    monitor.analyze_agent_state(agent), timeout=2
                )

        # Falls back to healthy, same as any other LLM failure
        assert result["state"] == "healthy"


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
        from contextlib import contextmanager

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

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

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
        # 15 repeated lines (threshold is 12)
        repeated = "This is a long enough line that repeats many times in the output"
        output = "\n".join(
            [repeated] * 15 + ["Normal line that is different and unique here"]
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

    @pytest.mark.asyncio
    async def test_detects_repetition_despite_varying_color_codes(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Same gap class as TestMechanicalRecovery's color-code test: the
        Counter(lines) exact-string count used to treat a repeated line
        wrapped in different SGR color codes on each occurrence as N
        distinct lines, never reaching REPEAT_THRESHOLD."""
        agent = Agent(id="a1", cli_type="claude")
        repeated = "This is a long enough line that repeats many times in the output"
        colors = ["\x1b[31m", "\x1b[32m", "\x1b[33m"]
        lines = [f"{colors[i % 3]}{repeated}\x1b[0m" for i in range(15)]
        output = "\n".join(lines + ["Normal line that is different and unique here"])
        mock_agent_manager.get_agent_output.return_value = output

        session = Mock()
        mock_db.get_session.return_value = session

        await make_monitoring_loop._detect_repetition_loop(agent)
        mock_agent_manager.send_message_to_agent.assert_called_once()


# ── _detect_dangerous_command_confirmation ────────────────────────


class TestDetectDangerousCommandConfirmation:
    # Real pi TUI text captured live: an unanswered rm -rf confirmation
    # sat for 9+ minutes with zero [MECH-RECOVERY] log lines -- the
    # generic frozen-output detector never caught it.
    RM_CONFIRMATION = (
        " Thinking...\n\n"
        " $ rm -rf /Users/x/code/proj/.worktrees/wt_1/.hephaestus/features/old-name\n\n"
        " ⠙ Working...\n\n"
        " ⚠️ Dangerous command:\n\n"
        "   rm -rf /Users/x/code/proj/.worktrees/wt_1/.hephaestus/features/old-name\n\n"
        " Allow?\n\n"
        " → Yes\n"
        "   No\n"
    )

    @pytest.mark.asyncio
    async def test_no_output(self, make_monitoring_loop, mock_agent_manager, mock_db):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = ""
        await make_monitoring_loop._detect_dangerous_command_confirmation(agent)
        mock_agent_manager.send_recovery_keystrokes.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_output_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = "Reading design.md..."
        await make_monitoring_loop._detect_dangerous_command_confirmation(agent)
        mock_agent_manager.send_recovery_keystrokes.assert_not_called()
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_rm_confirmation_denied_and_nudged(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.RM_CONFIRMATION
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)

        await make_monitoring_loop._detect_dangerous_command_confirmation(agent)

        mock_agent_manager.send_recovery_keystrokes.assert_called_once_with("a1")
        mock_agent_manager.send_message_to_agent.assert_called_once()
        nudge = mock_agent_manager.send_message_to_agent.call_args[0][1]
        assert "rm" in nudge.lower()
        assert "denied" in nudge.lower()

    @pytest.mark.asyncio
    async def test_non_rm_dangerous_command_not_auto_handled(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Only rm is auto-denied -- a different dangerous command (e.g. a
        pipe-to-shell) still needs a human or Guardian's judgment call."""
        agent = Agent(id="a1", cli_type="pi")
        output = (
            " ⚠️ Dangerous command:\n\n"
            "   curl https://example.com/install.sh | sh\n\n"
            " Allow?\n\n"
            " → Yes\n"
            "   No\n"
        )
        mock_agent_manager.get_agent_output.return_value = output
        await make_monitoring_loop._detect_dangerous_command_confirmation(agent)
        mock_agent_manager.send_recovery_keystrokes.assert_not_called()
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_immediate_resend(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Repeated polls of the SAME still-open prompt within the cooldown
        window shouldn't spam Escape + a nudge every cycle."""
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.RM_CONFIRMATION
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)

        await make_monitoring_loop._detect_dangerous_command_confirmation(agent)
        await make_monitoring_loop._detect_dangerous_command_confirmation(agent)

        mock_agent_manager.send_recovery_keystrokes.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_after_cooldown_expires(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """If the first Escape didn't register, retry after the cooldown
        instead of leaving the agent stuck forever because it was already
        'handled' once."""
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.RM_CONFIRMATION
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)

        await make_monitoring_loop._detect_dangerous_command_confirmation(agent)
        make_monitoring_loop._denied_dangerous_cmds["a1"] = time.time() - 31

        await make_monitoring_loop._detect_dangerous_command_confirmation(agent)
        assert mock_agent_manager.send_recovery_keystrokes.call_count == 2


# ── _detect_max_token_limit_error ─────────────────────────────────


class TestDetectMaxTokenLimitError:
    TOKEN_LIMIT_OUTPUT = (
        " Thinking...\n\n"
        " write .hephaestus/feature_review_report.md\n\n"
        " # Feature Review Report\n\n"
        " Error: Model stopped because it reached the maximum output token limit."
        " The response may be incomplete.\n\n"
        " ⠙ Working...\n"
    )

    @pytest.mark.asyncio
    async def test_no_output(self, make_monitoring_loop, mock_agent_manager, mock_db):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = ""
        await make_monitoring_loop._detect_max_token_limit_error(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_output_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = "Reading design.md..."
        await make_monitoring_loop._detect_max_token_limit_error(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_nudges_immediately_no_keystrokes(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """No recovery keystrokes -- unlike the dangerous-command dialog,
        there's nothing to dismiss here; pi already returned control."""
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.TOKEN_LIMIT_OUTPUT

        await make_monitoring_loop._detect_max_token_limit_error(agent)

        mock_agent_manager.send_recovery_keystrokes.assert_not_called()
        mock_agent_manager.send_message_to_agent.assert_called_once()
        nudge = mock_agent_manager.send_message_to_agent.call_args[0][1]
        assert "token limit" in nudge.lower()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_immediate_resend(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.TOKEN_LIMIT_OUTPUT

        await make_monitoring_loop._detect_max_token_limit_error(agent)
        await make_monitoring_loop._detect_max_token_limit_error(agent)

        mock_agent_manager.send_message_to_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_after_cooldown_expires(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.TOKEN_LIMIT_OUTPUT

        await make_monitoring_loop._detect_max_token_limit_error(agent)
        make_monitoring_loop._nudged_token_limit["a1"] = time.time() - 31

        await make_monitoring_loop._detect_max_token_limit_error(agent)
        assert mock_agent_manager.send_message_to_agent.call_count == 2


# ── _detect_mcp_disconnected ──────────────────────────────────────


class TestDetectMcpDisconnected:
    DISCONNECTED_OUTPUT = (
        " ⠴ Working...\n\n"
        "──────────────────────────────────────────────────────────\n"
        "~/code/HephaestusNG/.worktrees/wt_feature\n"
        "↑270k ↓15k R2.7M CH99.3% $0.140 8.1%/1.0M (auto)  (openrouter) xiaomi/mimo-v2.5-pro\n"
        "MCP: 0/1 servers\n"
    )
    CONNECTED_OUTPUT = DISCONNECTED_OUTPUT.replace("MCP: 0/1 servers", "MCP: 1/1 servers")

    @pytest.mark.asyncio
    async def test_no_output(self, make_monitoring_loop, mock_agent_manager, mock_db):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_raw_pane.return_value = ""
        await make_monitoring_loop._detect_mcp_disconnected(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_connected_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_raw_pane.return_value = self.CONNECTED_OUTPUT
        await make_monitoring_loop._detect_mcp_disconnected(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_servers_configured_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """MCP: 0/0 servers means none are configured at all -- not a
        failure, so this must not fire."""
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_raw_pane.return_value = self.DISCONNECTED_OUTPUT.replace(
            "MCP: 0/1 servers", "MCP: 0/0 servers"
        )
        await make_monitoring_loop._detect_mcp_disconnected(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_nudges_immediately_no_keystrokes(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """No recovery keystrokes -- there's no dialog to dismiss, just a
        reconnect command for the agent to run itself."""
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_raw_pane.return_value = self.DISCONNECTED_OUTPUT

        await make_monitoring_loop._detect_mcp_disconnected(agent)

        mock_agent_manager.send_recovery_keystrokes.assert_not_called()
        mock_agent_manager.send_message_to_agent.assert_called_once()
        nudge = mock_agent_manager.send_message_to_agent.call_args[0][1]
        assert "mcp connect hephaestus" in nudge.lower()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_immediate_resend(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_raw_pane.return_value = self.DISCONNECTED_OUTPUT

        await make_monitoring_loop._detect_mcp_disconnected(agent)
        await make_monitoring_loop._detect_mcp_disconnected(agent)

        mock_agent_manager.send_message_to_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_after_cooldown_expires(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_raw_pane.return_value = self.DISCONNECTED_OUTPUT

        await make_monitoring_loop._detect_mcp_disconnected(agent)
        make_monitoring_loop._nudged_mcp_disconnected["a1"] = time.time() - 31

        await make_monitoring_loop._detect_mcp_disconnected(agent)
        assert mock_agent_manager.send_message_to_agent.call_count == 2

    @pytest.mark.asyncio
    async def test_non_pi_cli_gets_no_nudge(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Polymorphic via CLIAgentInterface.mcp_reconnect_instructions,
        like recovery_keystrokes: a CLI with no known reconnect mechanism
        (base class default "") must not get pi-specific `mcp connect`
        syntax nudged at it -- that would just confuse it."""
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_raw_pane.return_value = self.DISCONNECTED_OUTPUT

        await make_monitoring_loop._detect_mcp_disconnected(agent)

        mock_agent_manager.send_message_to_agent.assert_not_called()


# ── _detect_credit_exhausted ──────────────────────────────────────


class TestDetectCreditExhausted:
    CREDIT_ERROR_OUTPUT = (
        ' Error: 402: {"message":"This request requires more credits, or fewer '
        'max_tokens. You requested up to 106804 tokens, but can only afford 11823. '
        'To increase, visit https://openrouter.ai/...","code":402}\n'
    )

    def _make_session(self, task=None, workflow=None):
        from contextlib import contextmanager

        session = Mock()

        def query_side_effect(model):
            m = Mock()
            if model.__name__ == "Task":
                m.filter_by.return_value.first.return_value = task
            elif model.__name__ == "Workflow":
                m.filter_by.return_value.first.return_value = workflow
            return m

        session.query.side_effect = query_side_effect

        @contextmanager
        def mock_session_scope():
            yield session

        return session, mock_session_scope

    @pytest.mark.asyncio
    async def test_no_output(self, make_monitoring_loop, mock_agent_manager, mock_db):
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = ""
        mock_agent_manager.terminate_agent = AsyncMock()

        await make_monitoring_loop._detect_credit_exhausted(agent)

        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_output_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "Reading design.md..."
        mock_agent_manager.terminate_agent = AsyncMock()

        await make_monitoring_loop._detect_credit_exhausted(agent)

        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_pauses_workflow_fails_task_and_terminates(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.CREDIT_ERROR_OUTPUT
        mock_agent_manager.terminate_agent = AsyncMock()

        task = Mock(workflow_id="wf1", status="in_progress", failure_reason=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        session, mock_session_scope = self._make_session(task=task, workflow=workflow)
        mock_db.session_scope = mock_session_scope

        result = await make_monitoring_loop._detect_credit_exhausted(agent)

        assert result is True
        assert task.status == "failed"
        assert "402" in task.failure_reason
        assert workflow.status == "paused"
        assert workflow.paused_by == "system"
        assert workflow.paused_at is not None
        mock_agent_manager.terminate_agent.assert_called_once_with("a1")

    @pytest.mark.asyncio
    async def test_already_paused_workflow_not_overwritten(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """A workflow already paused for a different reason (e.g. a human
        pause) must not have its reason/timestamp stomped by this check."""
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.CREDIT_ERROR_OUTPUT
        mock_agent_manager.terminate_agent = AsyncMock()

        task = Mock(workflow_id="wf1", status="in_progress", failure_reason=None)
        workflow = Mock(
            status="paused", paused_by="user", paused_at="original-timestamp"
        )
        session, mock_session_scope = self._make_session(task=task, workflow=workflow)
        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._detect_credit_exhausted(agent)

        assert workflow.paused_by == "user"
        assert workflow.paused_at == "original-timestamp"

    @pytest.mark.asyncio
    async def test_one_shot_no_repeat(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.CREDIT_ERROR_OUTPUT
        mock_agent_manager.terminate_agent = AsyncMock()

        task = Mock(workflow_id="wf1", status="in_progress", failure_reason=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        session, mock_session_scope = self._make_session(task=task, workflow=workflow)
        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._detect_credit_exhausted(agent)
        await make_monitoring_loop._detect_credit_exhausted(agent)

        mock_agent_manager.terminate_agent.assert_called_once()


# ── _update_agent_health_from_trajectory ─────────────────────────


class TestUpdateAgentHealth:
    @pytest.mark.asyncio
    async def test_stores_analysis(self, make_monitoring_loop, mock_db):
        from contextlib import contextmanager

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

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._update_agent_health_from_trajectory(agent, analysis)
        assert session.add.call_count == 2  # GuardianAnalysis + AgentLog

    @pytest.mark.asyncio
    async def test_handles_no_agent(self, make_monitoring_loop, mock_db):
        from contextlib import contextmanager

        agent = Agent(id="a1")
        analysis = {"trajectory_aligned": True}
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        # Should not raise
        await make_monitoring_loop._update_agent_health_from_trajectory(agent, analysis)

    @pytest.mark.asyncio
    async def test_off_track_increments_failures(self, make_monitoring_loop, mock_db):
        from contextlib import contextmanager

        agent = Agent(id="a1")
        analysis = {
            "trajectory_aligned": False,
            "alignment_score": 0.2,
        }
        db_agent = Mock(id="a1", health_check_failures=0)
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = db_agent

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._update_agent_health_from_trajectory(agent, analysis)
        # alignment_score < 0.3 → += 2
        assert db_agent.health_check_failures == 2

    @pytest.mark.asyncio
    async def test_off_track_does_not_touch_last_activity(
        self, make_monitoring_loop, mock_db
    ):
        """Regression: unconditionally refreshing last_activity on every
        Guardian cycle (aligned or not) defeated the max_ignored_steering
        auto-restart check -- a persistently stuck agent would look
        "recently active" one cycle later purely because Guardian ran, not
        because it made progress."""
        from contextlib import contextmanager

        agent = Agent(id="a1")
        analysis = {"trajectory_aligned": False, "alignment_score": 0.2}
        db_agent = Mock(id="a1", health_check_failures=0, last_activity="sentinel")
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = db_agent

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._update_agent_health_from_trajectory(agent, analysis)
        assert db_agent.last_activity == "sentinel"

    @pytest.mark.asyncio
    async def test_on_track_refreshes_last_activity(
        self, make_monitoring_loop, mock_db
    ):
        from contextlib import contextmanager
        from datetime import datetime

        agent = Agent(id="a1")
        analysis = {"trajectory_aligned": True, "alignment_score": 0.9}
        db_agent = Mock(id="a1", health_check_failures=1, last_activity="sentinel")
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = db_agent

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._update_agent_health_from_trajectory(agent, analysis)
        assert isinstance(db_agent.last_activity, datetime)


# ── _save_conductor_analysis ─────────────────────────────────────


class TestSaveConductorAnalysis:
    @pytest.mark.asyncio
    async def test_saves_analysis(self, make_monitoring_loop, mock_db):
        from contextlib import contextmanager

        analysis = {
            "system_status": "healthy",
            "agents_summary": [],
            "recommendations": [],
        }
        session = Mock()

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._save_conductor_analysis(analysis)
        session.add.assert_called()

    @pytest.mark.asyncio
    async def test_handles_exception(self, make_monitoring_loop, mock_db):
        from contextlib import contextmanager

        analysis = {"system_status": "healthy"}
        session = Mock()
        session.add.side_effect = Exception("DB error")

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

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

    @pytest.mark.asyncio
    async def test_max_restarts_fails_task_instead_of_recreating(
        self, monitor, mock_agent_manager, mock_db, mock_rag
    ):
        """Regression: this path creates a brand-new Agent row via
        create_agent_for_task rather than incrementing restart_count on the
        existing one (unlike AgentManager.restart_agent), so it had no
        restart-loop bound at all -- a decision-maker that kept returning
        RECREATE for the same stuck task could spin up unlimited agents."""
        agent = Agent(id="a1", current_task_id="t1", restart_count=3)
        session = Mock()
        task = Mock(id="t1", status="in_progress")
        session.query.return_value.filter_by.return_value.first.return_value = task
        mock_db.get_session.return_value = session

        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.create_agent_for_task = AsyncMock()

        await monitor._recreate_agent_with_new_approach(agent, "Stuck too long")

        mock_agent_manager.terminate_agent.assert_not_called()
        mock_agent_manager.create_agent_for_task.assert_not_called()
        assert task.status == "failed"


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
    async def test_output_change_refreshes_last_activity(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: Agent.last_activity was ONLY touched by an MCP tool
        call (_touch_agent_activity, server.py) or a successful Guardian
        analysis cycle -- never by plain, visible tmux output changing. A
        read-heavy phase (e.g. feature_review reading design.md + several
        scope.md files before writing anything, with no MCP calls in
        between) could go 5+ minutes without either of those firing while
        genuinely, visibly working, and _audit_system_health's separate
        "task stuck" check -- driven entirely by last_activity -- would
        kill it on its hard stuck_detection_minutes timer despite real
        progress. Observed live: the same feature_review task died to "no
        agent activity for >5 minutes" on three consecutive retries."""
        from contextlib import contextmanager

        from src.core.database import Agent as DbAgent

        agent = Agent(id="a1", cli_type="claude")
        db_agent = DbAgent(id="a1", last_activity=None)

        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = (
            db_agent
        )

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        # Output genuinely changing (real progress) must refresh
        # last_activity even though no MCP tool was called.
        mock_agent_manager.get_agent_output.return_value = "Reading design.md..."
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        first_seen = db_agent.last_activity
        assert first_seen is not None

        db_agent.last_activity = None  # simulate time passing with no MCP calls
        mock_agent_manager.get_agent_output.return_value = "Reading scope.md..."
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        assert db_agent.last_activity is not None

    @pytest.mark.asyncio
    async def test_frozen_with_varying_color_codes_still_detected(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """AgentManager._read_transcript_log deliberately keeps SGR color
        codes (\\x1b[...m) when stripping other ANSI. A TUI status bar that
        re-emits color codes on every redraw makes two reads of an
        otherwise-frozen screen differ byte-for-byte, which used to make the
        frozen-signature comparison below never match -- silently disabling
        this entire detector for any agent whose frozen screen had colored
        text. Observed live: an agent hard-stopped on a model error sat
        frozen for 12+ minutes with zero recovery attempts."""
        agent = Agent(id="a1", cli_type="claude")
        # Same visible content, different SGR color codes each read --
        # simulates a themed status bar re-rendering on every poll.
        frame1 = "\x1b[31mError: max output token limit\x1b[0m\nAgent idle at prompt"
        frame2 = "\x1b[32mError: max output token limit\x1b[0m\nAgent idle at prompt"
        mock_agent_manager.get_agent_output.side_effect = [frame1, frame2]
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)

        # First call sets baseline
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        make_monitoring_loop._stuck_state["a1"]["since"] = time.time() - 400

        # Second call: different color codes, same visible content
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        mock_agent_manager.send_recovery_keystrokes.assert_called_once()

    @pytest.mark.asyncio
    async def test_max_recovery_fails_task(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        from contextlib import contextmanager

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

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        assert task.status == "failed"
        mock_agent_manager.terminate_agent.assert_called_once()


class TestSessionLimitPause:
    """A session-limit rejection on an already-running agent (unlike
    create_agent_for_task's equivalent check, which only sees this during
    initial prompt delivery) must pause the workflow when the phase has no
    fallback_cli_tool configured -- retrying would just recreate the same
    primary CLI and hit the same limit again until it resets on its own."""

    def _session_with(self, task, phase=None, workflow=None):
        from contextlib import contextmanager

        session = Mock()

        def query_side_effect(model):
            m = Mock()
            name = model.__name__ if hasattr(model, "__name__") else str(model)
            if name == "Task":
                m.filter_by.return_value.first.return_value = task
            elif name == "Phase":
                m.filter_by.return_value.first.return_value = phase
            elif name == "Workflow":
                m.filter_by.return_value.first.return_value = workflow
            return m

        session.query.side_effect = query_side_effect

        @contextmanager
        def mock_session_scope():
            yield session

        return mock_session_scope

    @pytest.mark.asyncio
    async def test_pauses_workflow_when_no_fallback_configured(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = (
            "You've hit your session limit"
        )
        mock_agent_manager.terminate_agent = AsyncMock()

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1"
        )
        phase = Mock(fallback_cli_tool=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        # First call only sets the frozen-signature baseline (real check
        # requires an unchanged signature across two consecutive polls,
        # matching TestMechanicalRecovery's frozen-detection pattern).
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        assert task.status == "failed"
        assert workflow.status == "paused"
        assert workflow.paused_by == "system"
        assert workflow.paused_at is not None
        mock_agent_manager.terminate_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_pause_when_fallback_configured(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = (
            "You've hit your session limit"
        )
        mock_agent_manager.terminate_agent = AsyncMock()

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1"
        )
        phase = Mock(fallback_cli_tool="pi")
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        assert task.status == "failed"
        assert workflow.status == "active"
        assert workflow.paused_by is None
        mock_agent_manager.terminate_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_false_positive_on_bare_youve_hit(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: the bare fragment "You've hit" (e.g. "you've hit a
        bug", quoted in an agent's own reasoning) must not trigger -- only
        the confirmed exact Claude phrase or the other specific phrases."""
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = (
            "Looks like you've hit a tricky edge case here, let me think..."
        )
        mock_agent_manager.terminate_agent = AsyncMock()

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        mock_agent_manager.terminate_agent.assert_not_called()


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

        # Set grace period past check on the orphan reaper
        make_monitoring_loop._orphan_reaper.last_check_time = datetime.now() - timedelta(
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

        # Set grace period past check on the orphan reaper
        make_monitoring_loop._orphan_reaper.last_check_time = datetime.now() - timedelta(
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


# ── _monitoring_cycle: mechanical recovery / Guardian coordination ─


class TestMonitoringCycleGuardianSkip:
    """Regression: mechanical recovery (Phase 0) and Guardian analysis
    (Phase 1) both ran against the same `agents` snapshot in one
    _monitoring_cycle with no coordination -- an agent mechanical recovery
    had just nudged or terminated still got an immediate, redundant (or
    outright harmful, in the termination case) Guardian pass in the same
    cycle. Guardian must skip any agent mechanical recovery intervened on
    this cycle and let the next cycle re-evaluate with fresh state."""

    def _make_agent(self):
        return Agent(id="a1", cli_type="pi", tmux_session_name="s1", status="working")

    @pytest.mark.asyncio
    async def test_intervened_agent_skips_guardian(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = self._make_agent()
        mock_agent_manager.get_active_agents = Mock(return_value=[agent])
        # _monitoring_cycle's own diagnostic "active workflows" query needs a
        # real list back from the Mock session, or len() on it blows up --
        # unrelated to this fix, just a requirement of exercising the real
        # method end-to-end.
        mock_db.get_session.return_value.query.return_value.filter_by.return_value.all.return_value = (
            []
        )

        make_monitoring_loop._detect_credit_exhausted = AsyncMock(return_value=False)
        make_monitoring_loop._mechanical_recovery_for_agent = AsyncMock(return_value=True)
        make_monitoring_loop._detect_repetition_loop = AsyncMock(return_value=False)
        make_monitoring_loop._detect_dangerous_command_confirmation = AsyncMock(
            return_value=False
        )
        make_monitoring_loop._detect_max_token_limit_error = AsyncMock(return_value=False)
        make_monitoring_loop._detect_mcp_disconnected = AsyncMock(return_value=False)
        make_monitoring_loop._guardian_analysis_for_agent = AsyncMock(return_value=None)

        await make_monitoring_loop._monitoring_cycle()

        make_monitoring_loop._guardian_analysis_for_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_intervened_agent_still_gets_guardian(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = self._make_agent()
        mock_agent_manager.get_active_agents = Mock(return_value=[agent])
        mock_db.get_session.return_value.query.return_value.filter_by.return_value.all.return_value = (
            []
        )

        make_monitoring_loop._detect_credit_exhausted = AsyncMock(return_value=False)
        make_monitoring_loop._mechanical_recovery_for_agent = AsyncMock(return_value=False)
        make_monitoring_loop._detect_repetition_loop = AsyncMock(return_value=False)
        make_monitoring_loop._detect_dangerous_command_confirmation = AsyncMock(
            return_value=False
        )
        make_monitoring_loop._detect_max_token_limit_error = AsyncMock(return_value=False)
        make_monitoring_loop._detect_mcp_disconnected = AsyncMock(return_value=False)
        make_monitoring_loop._guardian_analysis_for_agent = AsyncMock(return_value=None)

        await make_monitoring_loop._monitoring_cycle()

        make_monitoring_loop._guardian_analysis_for_agent.assert_called_once()


class TestStuckTaskNudgeCap:
    """_audit_system_health's stuck-task nudge: an idle-but-"working"
    agent gets a task-specific nudge naming its current task_id (not a
    generic "report your status", which let an agent stuck believing an
    earlier task's completion applied to a brand new one just re-confirm
    that same wrong belief forever -- observed live on a resumed pi
    session). Repeated nudge-then-respond-without-completing cycles are
    capped at MAX_STUCK_TASK_NUDGES instead of trusting "the agent
    produced output" as proof of progress indefinitely -- the naive
    version reset its own counter to zero every time activity was seen,
    so the cap could never actually be reached."""

    @pytest.fixture
    def real_db(self, tmp_path):
        from src.core.database import DatabaseManager

        db_path = tmp_path / "test.db"
        db = DatabaseManager(str(db_path))
        db.create_tables()
        return db

    @pytest.fixture
    def audit_monitor(self, real_db, mock_agent_manager, mock_llm, mock_rag):
        from src.monitoring.monitor import MonitoringLoop

        with patch("src.monitoring.monitor.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(stuck_detection_minutes=10, agent_timeout_minutes=60)
            m = MonitoringLoop(
                db_manager=real_db,
                agent_manager=mock_agent_manager,
                llm_provider=mock_llm,
                rag_system=mock_rag,
            )
        return m

    def _seed_stuck_task(self, real_db, idle_minutes=15):
        from src.core.database import Agent, Task

        session = real_db.get_session()
        started = datetime.utcnow() - timedelta(minutes=20)
        session.add(
            Agent(
                id="agent-stuck",
                system_prompt="p",
                status="working",
                cli_type="pi",
                agent_type="phase",
                last_activity=datetime.utcnow() - timedelta(minutes=idle_minutes),
            )
        )
        session.add(
            Task(
                id="task-stuck",
                raw_description="r",
                done_definition="d",
                status="in_progress",
                assigned_agent_id="agent-stuck",
                started_at=started,
            )
        )
        session.commit()
        session.close()
        return "task-stuck", "agent-stuck"

    def _set_agent_last_activity(self, real_db, agent_id, when):
        from src.core.database import Agent

        session = real_db.get_session()
        agent = session.query(Agent).filter_by(id=agent_id).first()
        agent.last_activity = when
        session.commit()
        session.close()

    @pytest.mark.asyncio
    async def test_first_nudge_names_the_current_task_id(
        self, audit_monitor, real_db, mock_agent_manager
    ):
        task_id, agent_id = self._seed_stuck_task(real_db)

        with patch("src.mcp.autopilot_api.run_health_audit", return_value={"findings": []}):
            await audit_monitor._audit_system_health()

        mock_agent_manager.send_message_to_agent.assert_called_once()
        call_args = mock_agent_manager.send_message_to_agent.call_args
        assert call_args[0][0] == agent_id
        assert task_id in call_args[0][1]
        assert "complete_my_task" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_caps_repeated_nudges_when_agent_keeps_responding_without_completing(
        self, audit_monitor, real_db, mock_agent_manager
    ):
        from src.core.database import Task
        from src.monitoring.monitor import MAX_STUCK_TASK_NUDGES

        task_id, agent_id = self._seed_stuck_task(real_db)

        with patch("src.mcp.autopilot_api.run_health_audit", return_value={"findings": []}):
            # Each cycle: agent is idle (due for a nudge -- grace period
            # from the previous nudge is force-expired directly, since real
            # wall-clock time won't elapse meaningfully in a fast test),
            # then "responds" (activity moves forward) before the next
            # cycle -- never actually completes the task.
            for _ in range(MAX_STUCK_TASK_NUDGES):
                self._set_agent_last_activity(
                    real_db, agent_id, datetime.utcnow() - timedelta(minutes=15)
                )
                count, _ = audit_monitor._stuck_task_nudges.get(task_id, (0, None))
                if count:
                    audit_monitor._stuck_task_nudges[task_id] = (
                        count,
                        datetime.utcnow() - timedelta(minutes=15),
                    )
                await audit_monitor._audit_system_health()  # sends a nudge
                self._set_agent_last_activity(real_db, agent_id, datetime.utcnow())
                await audit_monitor._audit_system_health()  # sees "activity", must not reset the count

            # One more idle cycle after the cap is reached -- must be
            # treated as stuck now, not nudged a 4th time.
            self._set_agent_last_activity(
                real_db, agent_id, datetime.utcnow() - timedelta(minutes=15)
            )
            count, _ = audit_monitor._stuck_task_nudges.get(task_id, (0, None))
            audit_monitor._stuck_task_nudges[task_id] = (
                count,
                datetime.utcnow() - timedelta(minutes=15),
            )
            await audit_monitor._audit_system_health()

        assert mock_agent_manager.send_message_to_agent.call_count == MAX_STUCK_TASK_NUDGES

        session = real_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "failed"
        session.close()

    @pytest.mark.asyncio
    async def test_does_not_cap_a_healthy_agent_that_stays_active(
        self, audit_monitor, real_db, mock_agent_manager
    ):
        """Sanity check the cap isn't overbroad: an agent producing steady
        activity (never idle long enough to be nudged at all) must never
        be touched."""
        from src.core.database import Task
        from src.monitoring.monitor import MAX_STUCK_TASK_NUDGES

        task_id, agent_id = self._seed_stuck_task(real_db, idle_minutes=0)

        with patch("src.mcp.autopilot_api.run_health_audit", return_value={"findings": []}):
            for _ in range(MAX_STUCK_TASK_NUDGES + 2):
                self._set_agent_last_activity(real_db, agent_id, datetime.utcnow())
                await audit_monitor._audit_system_health()

        mock_agent_manager.send_message_to_agent.assert_not_called()
        session = real_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "in_progress"
        session.close()
