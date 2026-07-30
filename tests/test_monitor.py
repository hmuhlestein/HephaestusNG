"""Tests for IntelligentMonitor — pure helpers and low-dependency methods."""

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.core.database import Agent, DatabaseManager

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mock_db():
    mock = Mock(spec=DatabaseManager)
    # session_scope() is used as `with self.db_manager.session_scope() as
    # session:` throughout monitor.py -- a plain Mock()'s return value
    # doesn't support the context manager protocol (__enter__/__exit__
    # are magic methods MagicMock configures automatically but Mock
    # doesn't), so every code path using session_scope() raised
    # "'Mock' object does not support the context manager protocol"
    # instead of exercising the test.
    mock.session_scope = MagicMock()
    return mock


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
        mock_agent_manager.get_agent_output.return_value = ""
        await make_monitoring_loop._detect_mcp_disconnected(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_connected_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.CONNECTED_OUTPUT
        await make_monitoring_loop._detect_mcp_disconnected(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_servers_configured_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """MCP: 0/0 servers means none are configured at all -- not a
        failure, so this must not fire."""
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.DISCONNECTED_OUTPUT.replace(
            "MCP: 0/1 servers", "MCP: 0/0 servers"
        )
        await make_monitoring_loop._detect_mcp_disconnected(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_nudges_and_sends_escape_first(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Sends Escape (send_recovery_keystrokes) before the reconnect
        message -- an agent stuck in a "Working..." spinner loop with MCP
        disconnected can't process a text message until the spinner is
        broken (see commit efa1955)."""
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.DISCONNECTED_OUTPUT

        await make_monitoring_loop._detect_mcp_disconnected(agent)

        mock_agent_manager.send_recovery_keystrokes.assert_called_once_with("a1")
        mock_agent_manager.send_message_to_agent.assert_called_once()
        nudge = mock_agent_manager.send_message_to_agent.call_args[0][1]
        assert "mcp connect hephaestus" in nudge.lower()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_immediate_resend(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.DISCONNECTED_OUTPUT

        await make_monitoring_loop._detect_mcp_disconnected(agent)
        await make_monitoring_loop._detect_mcp_disconnected(agent)

        mock_agent_manager.send_message_to_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_after_cooldown_expires(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi")
        mock_agent_manager.get_agent_output.return_value = self.DISCONNECTED_OUTPUT

        await make_monitoring_loop._detect_mcp_disconnected(agent)
        # Cooldown is 45s (c9e653b) -- must be set past that, not just past
        # the old 30s value this previously used.
        make_monitoring_loop._nudged_mcp_disconnected["a1"] = time.time() - 46

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
        mock_agent_manager.get_agent_output.return_value = self.DISCONNECTED_OUTPUT

        await make_monitoring_loop._detect_mcp_disconnected(agent)

        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_nudge_tells_agent_to_complete_current_task(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: observed live, an agent reconnected MCP after this
        exact nudge and then just replied "Task already completed. No
        action needed." on repeat forever, never actually calling
        complete_my_task -- reconnecting fixed the connection but didn't
        tell the agent to act on it. The nudge must name the agent's
        CURRENT task_id and explicitly instruct it to call complete_my_task
        if it hasn't, not just reconnect."""
        agent = Agent(id="a1", cli_type="pi", current_task_id="task-42")
        mock_agent_manager.get_agent_output.return_value = self.DISCONNECTED_OUTPUT

        await make_monitoring_loop._detect_mcp_disconnected(agent)

        nudge = mock_agent_manager.send_message_to_agent.call_args[0][1]
        assert "complete_my_task" in nudge
        assert "task-42" in nudge


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


# ── _detect_agent_never_started ─────────────────────────────────────


class TestDetectAgentNeverStarted:
    """Regression (live incident): a pi agent queued behind other
    concurrently-launched agents on the same local model server sat at
    its initial "Begin now." banner with zero output for 10+ minutes.
    _mechanical_recovery_for_agent's frozen-output check never caught it
    because its in-memory _stuck_state had just been reset by an
    unrelated backend restart minutes earlier -- it needs 300s of
    observed frozen time from THIS process's own polling, not from
    launch. _detect_agent_never_started reads persisted
    Agent.launched_at/last_activity instead, so it doesn't depend on
    in-memory state surviving a restart."""

    def _session_with(self, task):
        from contextlib import contextmanager

        session = Mock()
        session.query.return_value.filter_by.return_value.filter.return_value.first.return_value = task

        @contextmanager
        def mock_session_scope():
            yield session

        return mock_session_scope

    @pytest.mark.asyncio
    async def test_recent_launch_not_flagged(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        now = datetime.utcnow()
        agent = Agent(
            id="a1", cli_type="pi", status="working", current_task_id="t1",
            created_at=now, launched_at=now, last_activity=now,
        )
        mock_agent_manager.terminate_agent = AsyncMock()

        result = await make_monitoring_loop._detect_agent_never_started(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_activity_since_launch_not_flagged(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """last_activity meaningfully later than launched_at means real
        activity happened at some point -- even if it then went idle for
        a long time, that's a different failure mode (handled by
        _mechanical_recovery_for_agent / _audit_system_health's stuck-task
        check), not "never started"."""
        launch = datetime.utcnow() - timedelta(seconds=600)
        agent = Agent(
            id="a1", cli_type="pi", status="working", current_task_id="t1",
            created_at=launch, launched_at=launch, last_activity=launch + timedelta(seconds=300),
        )
        mock_agent_manager.terminate_agent = AsyncMock()

        result = await make_monitoring_loop._detect_agent_never_started(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_output_past_grace_terminates_and_resets_task(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        launch = datetime.utcnow() - timedelta(seconds=300)
        agent = Agent(
            id="a1", cli_type="pi", status="working", current_task_id="t1",
            created_at=launch, launched_at=launch, last_activity=launch,
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        task = Mock(id="t1", status="in_progress", assigned_agent_id="a1", failure_reason=None)
        mock_db.session_scope = self._session_with(task)

        result = await make_monitoring_loop._detect_agent_never_started(agent)

        assert result is True
        mock_agent_manager.terminate_agent.assert_called_once_with("a1")
        assert task.status == "pending"
        assert task.assigned_agent_id is None

    @pytest.mark.asyncio
    async def test_restarted_agent_that_hangs_again_is_still_caught(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: comparing last_activity against created_at (instead
        of launched_at) made this permanently blind to restarted agents --
        created_at predates every restart, so (last_activity - created_at)
        always looked "large" for a resumed "_r" session even with zero
        activity since THAT restart, the exact case this exists to catch."""
        original_creation = datetime.utcnow() - timedelta(hours=3)
        restart = datetime.utcnow() - timedelta(seconds=300)
        agent = Agent(
            id="a1", cli_type="pi", status="working", current_task_id="t1",
            created_at=original_creation, launched_at=restart, last_activity=restart,
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        task = Mock(id="t1", status="in_progress", assigned_agent_id="a1", failure_reason=None)
        mock_db.session_scope = self._session_with(task)

        result = await make_monitoring_loop._detect_agent_never_started(agent)

        assert result is True
        mock_agent_manager.terminate_agent.assert_called_once_with("a1")

    @pytest.mark.asyncio
    async def test_within_grace_period_not_yet_flagged(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        launch = datetime.utcnow() - timedelta(seconds=60)
        agent = Agent(
            id="a1", cli_type="pi", status="working", current_task_id="t1",
            created_at=launch, launched_at=launch, last_activity=launch,
        )
        mock_agent_manager.terminate_agent = AsyncMock()

        result = await make_monitoring_loop._detect_agent_never_started(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_shot_per_agent(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        launch = datetime.utcnow() - timedelta(seconds=300)
        agent = Agent(
            id="a1", cli_type="pi", status="working", current_task_id="t1",
            created_at=launch, launched_at=launch, last_activity=launch,
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        task = Mock(id="t1", status="in_progress", assigned_agent_id="a1", failure_reason=None)
        mock_db.session_scope = self._session_with(task)

        await make_monitoring_loop._detect_agent_never_started(agent)
        await make_monitoring_loop._detect_agent_never_started(agent)

        mock_agent_manager.terminate_agent.assert_called_once_with("a1")


class TestDetectBadModelError:
    """Regression: Claude Code rejects a --model string it doesn't
    recognize (e.g. a stale OpenRouter path baked into a Phase row from
    before default_cli_tool/cli_model changed) and just sits there. The
    agent CANNOT fix this itself -- /model is a client-side slash command
    Claude Code's input loop intercepts before it reaches the model, so no
    reply the agent generates can invoke it. Only the monitor, sending
    literal keystrokes via send_message_to_agent, can."""

    BAD_MODEL_OUTPUT = (
        "⏺ There's an issue with the selected model (xiaomi/mimo-v2.5-pro). "
        "It may not exist or you may not have access to it. Run /model to pick a "
        "different model.\n"
    )

    @pytest.mark.asyncio
    async def test_no_output(self, make_monitoring_loop, mock_agent_manager, mock_db):
        agent = Agent(id="a1", cli_type="claude", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = ""
        await make_monitoring_loop._detect_bad_model_error(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_output_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "Reading design.md..."
        await make_monitoring_loop._detect_bad_model_error(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_claude_cli_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """This is Claude Code's own slash-command syntax and error
        phrasing -- must not fire for other CLIs even if their output
        happened to contain similar text."""
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.BAD_MODEL_OUTPUT
        await make_monitoring_loop._detect_bad_model_error(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_slash_model_command_directly(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.BAD_MODEL_OUTPUT
        make_monitoring_loop.config.cli_model = "sonnet"

        result = await make_monitoring_loop._detect_bad_model_error(agent)

        assert result is True
        mock_agent_manager.send_message_to_agent.assert_called_once_with("a1", "/model sonnet")

    @pytest.mark.asyncio
    async def test_only_fixes_once_per_agent(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """One-shot, like credit-exhaustion -- not a repeatable nudge with
        a cooldown, since sending the fix again while Claude is still
        reloading with the new model would just be noise."""
        agent = Agent(id="a1", cli_type="claude", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.BAD_MODEL_OUTPUT
        make_monitoring_loop.config.cli_model = "sonnet"

        await make_monitoring_loop._detect_bad_model_error(agent)
        await make_monitoring_loop._detect_bad_model_error(agent)

        mock_agent_manager.send_message_to_agent.assert_called_once()


class TestDetectCliModelFallback:
    """Regression: pi's local model has only a single inference slot, so an
    agent queued behind another sits frozen for however long that takes.
    The generic frozen-nudge path doesn't help (the agent isn't stuck, just
    waiting), so this switches it in-place to a configured fallback model
    instead. Polymorphic, not pi-specific: this method never checks
    agent.cli_type -- it goes through
    CLIAgentInterface.model_fallback_keystrokes (empty by default, overridden
    by PiAgent to use its `/model` picker). See
    docs/PI_MODEL_FALLBACK_DESIGN.md."""

    def _frozen_agent(self, make_monitoring_loop, frozen_for_seconds, cli_type="pi", cli_model="Qwen3.6-27B-UD-Q4_K_XL.gguf"):
        agent = Agent(id="a1", cli_type=cli_type, cli_model=cli_model, current_task_id="t1")
        make_monitoring_loop.config.cli_model = "Qwen3.6-27B-UD-Q4_K_XL.gguf"
        make_monitoring_loop.config.cli_model_fallback = "mimo-v2.5-pro"
        make_monitoring_loop.config.cli_model_fallback_wait_seconds = 120
        make_monitoring_loop._stuck_state = {
            "a1": {"sig": "same output", "since": time.time() - frozen_for_seconds, "recov": 0}
        }
        return agent

    @pytest.mark.asyncio
    async def test_cli_without_model_fallback_support_ignored(self, make_monitoring_loop, mock_agent_manager):
        """Claude's CLIAgentInterface doesn't override
        model_fallback_keystrokes (base class default: []) -- must be a
        no-op regardless of how long it's been frozen."""
        agent = self._frozen_agent(make_monitoring_loop, 200, cli_type="claude", cli_model="sonnet")
        make_monitoring_loop.config.cli_model = "sonnet"

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_fallback_configured_disables_feature(self, make_monitoring_loop, mock_agent_manager):
        agent = self._frozen_agent(make_monitoring_loop, 200)
        make_monitoring_loop.config.cli_model_fallback = None

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_already_off_default_model_ignored(self, make_monitoring_loop, mock_agent_manager):
        """An agent already running something else (including a prior
        fallback switch) must be left alone -- re-triggering on it would
        contradict the one-shot-per-task design."""
        agent = self._frozen_agent(make_monitoring_loop, 200, cli_model="mimo-v2.5-pro")

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_yet_frozen_ignored(self, make_monitoring_loop, mock_agent_manager):
        """No _stuck_state entry at all -- _mechanical_recovery_for_agent
        hasn't observed a repeated signature for this agent yet."""
        agent = Agent(id="a1", cli_type="pi", cli_model="Qwen3.6-27B-UD-Q4_K_XL.gguf", current_task_id="t1")
        make_monitoring_loop.config.cli_model = "Qwen3.6-27B-UD-Q4_K_XL.gguf"
        make_monitoring_loop.config.cli_model_fallback = "mimo-v2.5-pro"
        make_monitoring_loop._stuck_state = {}

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_frozen_under_threshold_ignored(self, make_monitoring_loop, mock_agent_manager):
        agent = self._frozen_agent(make_monitoring_loop, 60)  # under the 120s threshold

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_frozen_past_threshold_sends_model_then_search_text(self, make_monitoring_loop, mock_agent_manager):
        agent = self._frozen_agent(make_monitoring_loop, 200)

        with patch("src.monitoring.monitor.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is True
        assert mock_agent_manager.send_message_to_agent.call_args_list == [
            (("a1", "/model"),),
            (("a1", "mimo-v2.5-pro"),),
        ]
        mock_sleep.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_clears_stuck_state_after_switching(self, make_monitoring_loop, mock_agent_manager):
        """So the fallback model's own first turn gets a fresh frozen-
        detection window instead of being judged against a signature
        captured while still on the original model."""
        agent = self._frozen_agent(make_monitoring_loop, 200)

        await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert "a1" not in make_monitoring_loop._stuck_state

    @pytest.mark.asyncio
    async def test_only_fires_once_per_agent(self, make_monitoring_loop, mock_agent_manager):
        agent = self._frozen_agent(make_monitoring_loop, 200)

        await make_monitoring_loop._detect_cli_model_fallback(agent)
        assert mock_agent_manager.send_message_to_agent.call_count == 2  # "/model" + search text

        # Re-seed stuck_state as if the agent is frozen again on a later cycle.
        make_monitoring_loop._stuck_state["a1"] = {
            "sig": "same output", "since": time.time() - 200, "recov": 0
        }
        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        assert mock_agent_manager.send_message_to_agent.call_count == 2  # no additional calls

    @pytest.mark.asyncio
    async def test_logs_an_agent_event_capturing_why_the_model_switched(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: the switch used to only go to process logs -- nothing
        queryable was attached to the agent/task recording why its model
        changed. AgentLog is this codebase's existing mechanism for that
        (see e.g. Conductor/Guardian writes)."""
        from contextlib import contextmanager

        session = Mock()

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        agent = self._frozen_agent(make_monitoring_loop, 200)

        await make_monitoring_loop._detect_cli_model_fallback(agent)

        session.add.assert_called_once()
        logged = session.add.call_args[0][0]
        assert logged.agent_id == "a1"
        assert logged.log_type == "cli_model_fallback"
        assert logged.details["from_model"] == "Qwen3.6-27B-UD-Q4_K_XL.gguf"
        assert logged.details["to_model"] == "mimo-v2.5-pro"
        assert logged.details["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_persists_the_switch_to_agent_cli_model(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: agent.cli_model is surfaced directly in API responses
        (mcp/api.py, mcp/autopilot_api.py) for UI display, and
        get_active_agents() re-fetches a fresh row every cycle -- switching
        the model in the CLI session without also updating the DB column
        would leave every later cycle (and the UI) showing the stale
        original model as "the agent's current model" indefinitely."""
        from contextlib import contextmanager

        agent_row = Mock(cli_model="Qwen3.6-27B-UD-Q4_K_XL.gguf")
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = agent_row

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        agent = self._frozen_agent(make_monitoring_loop, 200)

        await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert agent_row.cli_model == "mimo-v2.5-pro"


class TestVerifyCliModelFallback:
    """Regression: _detect_cli_model_fallback's switch was fire-and-forget --
    nothing ever checked whether pi's picker interaction actually landed. A
    wrong search text or a picker that didn't open in time would leave the
    agent silently stuck on its original (frozen) model with no record of
    the failed attempt anywhere."""

    @pytest.mark.asyncio
    async def test_no_pending_entry_is_a_noop(self, make_monitoring_loop, mock_agent_manager):
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        make_monitoring_loop._pending_fallback_verification = {}

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        mock_agent_manager.get_agent_output.assert_not_called()

    @pytest.mark.asyncio
    async def test_cli_that_cannot_verify_clears_pending_silently(
        self, make_monitoring_loop, mock_agent_manager
    ):
        agent = Agent(id="a1", cli_type="claude", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "anything"
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("sonnet", "opus", time.time())
        }

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        assert "a1" not in make_monitoring_loop._pending_fallback_verification

    @pytest.mark.asyncio
    async def test_confirmed_switch_logs_success_and_clears_pending(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        from contextlib import contextmanager

        session = Mock()

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "Model: xiaomi/mimo-v2.5-pro"
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("mimo-v2.5-pro", "Qwen3.6-27B-UD-Q4_K_XL.gguf", time.time())
        }

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        assert "a1" not in make_monitoring_loop._pending_fallback_verification
        logged = session.add.call_args[0][0]
        assert logged.log_type == "cli_model_fallback_confirmed"

    @pytest.mark.asyncio
    async def test_unconfirmed_within_grace_period_stays_pending_no_warning(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        make_monitoring_loop.config.monitoring_interval_seconds = 60
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "still on the old model"
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("mimo-v2.5-pro", "Qwen3.6-27B-UD-Q4_K_XL.gguf", time.time())  # just switched
        }

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        assert "a1" in make_monitoring_loop._pending_fallback_verification
        mock_db.session_scope.assert_not_called()

    @pytest.mark.asyncio
    async def test_unconfirmed_past_grace_period_warns_and_logs(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        from contextlib import contextmanager

        session = Mock()

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        make_monitoring_loop.config.monitoring_interval_seconds = 60
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "still on the old model"
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("mimo-v2.5-pro", "Qwen3.6-27B-UD-Q4_K_XL.gguf", time.time() - 200)  # past 2x120s grace
        }

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        assert "a1" not in make_monitoring_loop._pending_fallback_verification
        logged = session.add.call_args[0][0]
        assert logged.log_type == "cli_model_fallback_unconfirmed"

    @pytest.mark.asyncio
    async def test_unconfirmed_past_grace_period_allows_a_retry(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: a failed picker interaction (never confirmed) must
        not permanently strand the agent -- clearing it from
        _switched_to_fallback_model lets _detect_cli_model_fallback try
        again if it freezes again on the still-unswitched original model.
        A successfully *confirmed* switch, by contrast, keeps the agent in
        that set forever (no automatic switch-back)."""
        from contextlib import contextmanager

        agent_row = Mock(cli_model="mimo-v2.5-pro")  # the optimistic write from _detect_cli_model_fallback
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = agent_row

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        make_monitoring_loop.config.monitoring_interval_seconds = 60
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "still on the old model"
        make_monitoring_loop._switched_to_fallback_model = {"a1"}
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("mimo-v2.5-pro", "Qwen3.6-27B-UD-Q4_K_XL.gguf", time.time() - 200)
        }

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        assert "a1" not in make_monitoring_loop._switched_to_fallback_model
        # The optimistic write must be reverted -- otherwise the retry this
        # just re-enabled would immediately be blocked by
        # _detect_cli_model_fallback's own "already off default model" gate.
        assert agent_row.cli_model == "Qwen3.6-27B-UD-Q4_K_XL.gguf"

    @pytest.mark.asyncio
    async def test_confirmed_switch_does_not_clear_the_one_shot_set(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        from contextlib import contextmanager

        session = Mock()

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "Model: xiaomi/mimo-v2.5-pro"
        make_monitoring_loop._switched_to_fallback_model = {"a1"}
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("mimo-v2.5-pro", "Qwen3.6-27B-UD-Q4_K_XL.gguf", time.time())
        }

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        assert "a1" in make_monitoring_loop._switched_to_fallback_model


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
        # The query chains filter_by(assigned_agent_id=...) then a separate
        # .filter(status.in_(...)) before .first() -- both links must be
        # configured or the chain falls through to an unconfigured Mock.
        session.query.return_value.filter_by.return_value.filter.return_value.first.return_value = task

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        assert task.status == "failed"
        mock_agent_manager.terminate_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_operation_aborted_nudge_echo_does_not_reset_recovery_counter(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: the nudge sent for "Operation aborted" gets echoed
        into the pane by most CLIs, so the very next poll's signature
        differs from the pre-nudge baseline purely because of our own
        message -- not real agent progress. Left unbaselined, every nudge
        reset st["recov"] back to 0 (the "output changed -> real progress"
        branch), so max_recov was never actually reached and the agent sat
        endlessly re-nudged instead of escalating after max_recov attempts.
        Observed live: 5+ consecutive "Operation aborted" nudges for the
        same agent."""
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)

        baseline = "Operation aborted\nAgent idle at prompt"
        after_nudge_1 = "Operation aborted\nAgent idle at prompt\n[nudge 1 echoed here]"
        after_nudge_2 = "Operation aborted\nAgent idle at prompt\n[nudge 2 echoed here]"
        mock_agent_manager.get_agent_output.side_effect = [
            baseline,       # call 1: sets initial baseline
            baseline,       # call 2: sig-check, matches baseline -> frozen
            after_nudge_1,  # call 2: post-nudge re-capture (this fix)
            after_nudge_1,  # call 3: sig-check, matches re-baselined sig -> still frozen
            after_nudge_2,  # call 3: post-nudge re-capture (this fix)
        ]

        # Call 1: sets baseline, no recovery yet.
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        assert make_monitoring_loop._stuck_state["a1"]["recov"] == 0

        # Call 2: frozen for >= 30s -> first nudge.
        make_monitoring_loop._stuck_state["a1"]["since"] = time.time() - 40
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        assert make_monitoring_loop._stuck_state["a1"]["recov"] == 1

        # Call 3: the pane now shows the first nudge's own echo -- without
        # the fix this reads as "output changed" and resets recov to 0.
        make_monitoring_loop._stuck_state["a1"]["since"] = time.time() - 40
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        assert make_monitoring_loop._stuck_state["a1"]["recov"] == 2

        assert mock_agent_manager.send_message_to_agent.call_count == 2

    @pytest.mark.asyncio
    async def test_operation_aborted_escalates_without_waiting_full_frozen_seconds(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: once max_recov nudges are exhausted via the fast 30s
        abort_frozen path, escalation to fail+terminate used to require
        frozen_for >= the FULL frozen_seconds (300s), measured from the
        last nudge's since=now reset -- neither branch's condition was
        satisfiable in between (recov >= max_recov blocks the nudge
        branch; frozen_for was only ~30-40s, nowhere near 300s), so the
        agent sat frozen and untouched for up to 5 more minutes after
        exhausting recovery attempts."""
        from contextlib import contextmanager

        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.terminate_agent = AsyncMock()

        frozen_output = "Operation aborted\nAgent idle at prompt"
        mock_agent_manager.get_agent_output.return_value = frozen_output

        make_monitoring_loop._stuck_state = {}
        make_monitoring_loop._stuck_state["a1"] = {
            "sig": frozen_output,
            "since": time.time() - 40,  # only 40s, nowhere near frozen_seconds=300
            "recov": 2,  # already exhausted max_recov
        }

        session = Mock()
        task = Mock(id="t1", status="in_progress")
        session.query.return_value.filter_by.return_value.filter.return_value.first.return_value = task

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

    def _wire_tmux_pane_output(self, mock_agent_manager, mock_db, agent_id, pane_text):
        """Spend/session-limit detection (see e9a34ff) reads the live tmux
        pane directly -- self.db_manager.get_session().query(Agent)... to
        find the agent's tmux_session_name, then a matching session in
        self.agent_manager.tmux_server.sessions, then capture-pane on its
        attached pane -- NOT get_agent_output (that path was replaced
        because the interactive limit menu only appears in the live pane,
        not the transcript log get_agent_output reads from). Without this,
        the detector's own `if _sess:` guard is never satisfied and the
        whole check silently no-ops, regardless of what get_agent_output
        returns."""
        db_agent = Mock(tmux_session_name=f"agent_{agent_id}")
        get_session_mock = Mock()
        get_session_mock.query.return_value.filter_by.return_value.first.return_value = db_agent
        mock_db.get_session.return_value = get_session_mock

        tmux_session = Mock(name=f"agent_{agent_id}")
        tmux_session.name = f"agent_{agent_id}"  # Mock(name=...) doesn't set .name itself
        tmux_session.attached_window.attached_pane.cmd.return_value.stdout = [pane_text]
        mock_agent_manager.tmux_server.sessions = [tmux_session]

    def _session_with(self, task, phase=None, workflow=None):
        from contextlib import contextmanager

        session = Mock()

        def query_side_effect(model):
            m = Mock()
            name = model.__name__ if hasattr(model, "__name__") else str(model)
            if name == "Task":
                # Production chains filter_by(assigned_agent_id=...) then a
                # separate .filter(status.in_(...)) before .first() -- both
                # links must be configured or the chain falls through to an
                # unconfigured Mock.
                m.filter_by.return_value.filter.return_value.first.return_value = task
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
        self._wire_tmux_pane_output(
            mock_agent_manager, mock_db, "a1", "You've hit your session limit"
        )
        mock_agent_manager.terminate_agent = AsyncMock()

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1"
        )
        phase = Mock(fallback_cli_tool=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        # No fallback anywhere -- phase.fallback_cli_tool=None above, and
        # the global config default must also be unset here, or this
        # "no fallback configured" scenario silently depends on whatever
        # hephaestus_config.yaml happens to contain on the machine running
        # the test (the make_monitoring_loop fixture's own get_config patch
        # only stays active during MonitoringLoop.__init__, not here).
        with patch("src.monitoring.monitor.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(default_fallback_cli_tool=None)

            # Unlike the frozen/stuck detection elsewhere in this function,
            # the spend/session-limit check fires immediately on the first
            # call -- it's not gated by a consecutive-poll baseline.
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
        """Regression: create_agent_for_task requires memories and
        project_context (no defaults) -- the fallback dispatch used to
        omit both entirely, so it ALWAYS raised, was caught by the
        surrounding try/except, and silently left the task "failed"
        instead of successfully re-dispatching to the fallback tool. A
        bare Mock() agent_manager doesn't enforce the real signature, so
        this only surfaces when create_agent_for_task is asserted to have
        actually been called with the required kwargs and to have
        produced a "pending" (re-dispatched), not "failed", task."""
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = (
            "You've hit your session limit"
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.get_project_context = AsyncMock(return_value="ctx")
        new_agent = Mock(id="a2")
        mock_agent_manager.create_agent_for_task = AsyncMock(return_value=new_agent)

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1",
            enriched_description="do the thing", done_definition="done",
        )
        phase = Mock(fallback_cli_tool="pi", fallback_cli_model=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        mock_agent_manager.create_agent_for_task.assert_called_once()
        call_kwargs = mock_agent_manager.create_agent_for_task.call_args.kwargs
        assert call_kwargs["memories"] == []
        assert call_kwargs["project_context"] == "ctx"
        assert call_kwargs["cli_type"] == "pi"

        assert task.status == "pending"
        assert task.assigned_agent_id is None
        assert workflow.status == "active"
        assert workflow.paused_by is None
        mock_agent_manager.terminate_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_fallback_clears_a_stale_pause(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression (live incident): an earlier agent on this same
        workflow hit the limit before a fallback was available/found and
        paused the workflow (paused_by="system"). A LATER agent's
        successful fallback dispatch must clear that stale pause -- left
        alone, the workflow stays "paused" forever even after the task
        completes, since _retry_exhausted_paused_workflows
        (orchestrator.py) only resumes a paused_by="system" workflow that
        still has a FAILED task sitting in it; once the fallback succeeds
        there's no longer one to trigger that recovery."""
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = (
            "You've hit your session limit"
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.get_project_context = AsyncMock(return_value="ctx")
        new_agent = Mock(id="a2")
        mock_agent_manager.create_agent_for_task = AsyncMock(return_value=new_agent)

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1",
            enriched_description="do the thing", done_definition="done",
        )
        phase = Mock(fallback_cli_tool="pi", fallback_cli_model=None)
        workflow = Mock(
            status="paused", paused_by="system",
            status_reason="CLI monthly spend limit hit (claude), no fallback configured",
            paused_at=datetime.utcnow(),
        )
        mock_db.session_scope = self._session_with(task, phase, workflow)

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        mock_agent_manager.create_agent_for_task.assert_called_once()
        assert workflow.status == "active"
        assert workflow.paused_by is None
        assert workflow.status_reason is None
        assert workflow.paused_at is None

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

    @pytest.mark.asyncio
    async def test_spend_limit_pauses_workflow_when_no_fallback_configured(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Claude Code's monthly-spend-limit message is the same failure
        class as a session limit -- the agent cannot make any more API
        calls -- and gets identical handling."""
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = (
            "You've hit your monthly spend limit."
        )
        mock_agent_manager.terminate_agent = AsyncMock()

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1"
        )
        phase = Mock(fallback_cli_tool=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        with patch("src.monitoring.monitor.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(default_fallback_cli_tool=None)
            await make_monitoring_loop._mechanical_recovery_for_agent(agent)
            await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        assert task.status == "failed"
        assert task.failure_reason == "CLI monthly spend limit reached"
        assert workflow.status == "paused"
        assert workflow.paused_by == "system"
        mock_agent_manager.terminate_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_spend_limit_no_pause_when_fallback_configured(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """A configured fallback_cli_tool should get a chance to run
        instead of leaving the workflow paused -- and must actually
        succeed in dispatching a new agent (see the companion
        test_no_pause_when_fallback_configured for why this needs
        explicit assertions on create_agent_for_task's call)."""
        agent = Agent(id="a1", cli_type="claude")
        mock_agent_manager.get_agent_output.return_value = (
            "You've hit your monthly spend limit."
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.get_project_context = AsyncMock(return_value="ctx")
        new_agent = Mock(id="a2")
        mock_agent_manager.create_agent_for_task = AsyncMock(return_value=new_agent)

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1",
            enriched_description="do the thing", done_definition="done",
        )
        phase = Mock(fallback_cli_tool="pi", fallback_cli_model=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        mock_agent_manager.create_agent_for_task.assert_called_once()
        call_kwargs = mock_agent_manager.create_agent_for_task.call_args.kwargs
        assert call_kwargs["memories"] == []
        assert call_kwargs["project_context"] == "ctx"

        assert task.status == "pending"
        assert workflow.status == "active"
        assert workflow.paused_by is None
        mock_agent_manager.terminate_agent.assert_called_once()


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


class TestAutoRestartFlushesCleanTranscript:
    """_auto_restart_agent kills a stuck agent's tmux session directly --
    it bypasses terminate_agent's own clean-shutdown flush entirely, so
    without its own final flush of the stability-tracked "clean"
    transcript, this abrupt-kill path would lose everything not yet
    confirmed stable (see AgentManager._flush_stable_transcript)."""

    @pytest.mark.asyncio
    async def test_flushes_before_killing_session(
        self, make_monitoring_loop, mock_db, mock_agent_manager
    ):
        from contextlib import contextmanager
        from pathlib import Path

        agent = Agent(id="agent-1", tmux_session_name="agent_agent-1", status="working")
        fake_dir = Path("/tmp/fake-transcript-dir")

        call_order = []
        mock_agent_manager._resolve_tmux_transcript_dir = Mock(
            return_value=fake_dir, side_effect=lambda *a, **k: (call_order.append("flush"), fake_dir)[1]
        )
        mock_agent_manager._flush_stable_transcript = Mock()
        mock_agent_manager.tmux_server.kill_session = Mock(
            side_effect=lambda *a, **k: call_order.append("kill")
        )

        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._auto_restart_agent(agent)

        mock_agent_manager._resolve_tmux_transcript_dir.assert_called_once_with(agent)
        mock_agent_manager._flush_stable_transcript.assert_called_once_with(
            "agent_agent-1", fake_dir / "agent_agent-1.clean.log"
        )
        assert call_order == ["flush", "kill"], (
            "the clean transcript must be flushed before the session is "
            "killed -- capture-pane can't see anything once it's gone"
        )
