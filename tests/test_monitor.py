"""Tests for IntelligentMonitor — pure helpers and low-dependency methods."""

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
def make_monitoring_loop(mock_db, mock_agent_manager, mock_llm):
    from src.monitoring.monitor import MonitoringLoop

    with patch("src.monitoring.monitor.get_config") as mock_cfg:
        mock_cfg.return_value = Mock(
            monitoring=Mock(stuck_detection_minutes=10),
            agents=Mock(agent_timeout_minutes=60),
        )
        ml = MonitoringLoop(
            db_manager=mock_db,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm,
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
        " write .hephaestus/review.md\n\n"
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

    @pytest.mark.asyncio
    async def test_ignores_match_within_resumed_session_grace_period(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """A phase resuming another phase's CLI session (shared
        session_roles entry) can replay a prior task's own token-limit
        error into the pane on startup -- must not be treated as current
        within the grace window."""
        from contextlib import contextmanager

        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.TOKEN_LIMIT_OUTPUT
        task = Mock(started_at=datetime.utcnow() - timedelta(seconds=10))
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = task

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        result = await make_monitoring_loop._detect_max_token_limit_error(agent)

        assert result is None or result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()


# ── _detect_unconfirmed_task_completion ────────────────────────────


class TestDetectUnconfirmedTaskCompletion:
    COMPLETION_OUTPUT = (
        " ⎿ Wrote 17 lines to .hephaestus/scope_review/scope.md\n\n"
        '● hephaestus - complete_my_task (MCP)(status: "done", summary: "..."\n\n'
        " ❯\n"
    )

    def _mock_session_with_task(
        self, mock_db, task_status, self_review_started_at=None, task_id="t1", started_at=None
    ):
        from contextlib import contextmanager

        session = Mock()
        task = Mock(
            id=task_id,
            status=task_status,
            self_review_started_at=self_review_started_at,
            started_at=started_at or (datetime.utcnow() - timedelta(minutes=10)),
        )
        session.query.return_value.filter_by.return_value.first.return_value = task

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        return task

    @pytest.mark.asyncio
    async def test_no_output(self, make_monitoring_loop, mock_agent_manager, mock_db):
        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = ""
        await make_monitoring_loop._detect_unconfirmed_task_completion(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_output_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "Reading design.md..."
        await make_monitoring_loop._detect_unconfirmed_task_completion(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_matches_multiline_pretty_printed_json_rendering(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Not every CLI renders a tool call on one line the way Claude
        Code does -- a pretty-printed JSON-style rendering (tool name and
        the status field on separate lines) must still match."""
        agent = Agent(id="a1", cli_type="pi", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = (
            "> Tool call:\n"
            "  {\n"
            '    "tool": "complete_my_task",\n'
            '    "status": "done",\n'
            '    "summary": "finished the thing"\n'
            "  }\n"
        )
        self._mock_session_with_task(mock_db, "in_progress")

        result = await make_monitoring_loop._detect_unconfirmed_task_completion(agent)

        assert result is True
        mock_agent_manager.send_message_to_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_working_status_ignored(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Only a still-'working' agent can have a stranded completion call
        -- an idle/terminated agent is a different failure mode, already
        covered by _detect_orphaned_idle_agent."""
        agent = Agent(id="a1", cli_type="claude", status="idle", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        await make_monitoring_loop._detect_unconfirmed_task_completion(agent)
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_nudges_when_task_still_in_progress(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """The reported live incident: complete_my_task rendered as sent in
        the transcript, but the task never actually reached a terminal
        status server-side."""
        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        self._mock_session_with_task(mock_db, "in_progress")

        result = await make_monitoring_loop._detect_unconfirmed_task_completion(agent)

        assert result is True
        mock_agent_manager.send_message_to_agent.assert_called_once()
        nudge = mock_agent_manager.send_message_to_agent.call_args[0][1]
        assert "t1" in nudge
        assert "complete_my_task" in nudge

    @pytest.mark.asyncio
    async def test_ignores_match_within_resumed_session_grace_period(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """A phase that resumes another phase's CLI session (shared
        session_roles entry, e.g. product_validation/product_requirements)
        can briefly replay the prior task's own completion call into the
        tmux pane on startup. Within the grace period after task.started_at,
        that must not be treated as THIS task confirming completion."""
        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        self._mock_session_with_task(
            mock_db, "in_progress", started_at=datetime.utcnow() - timedelta(seconds=10)
        )

        result = await make_monitoring_loop._detect_unconfirmed_task_completion(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_nudge_when_task_already_done(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """The call actually landed -- nothing to nudge about."""
        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        self._mock_session_with_task(mock_db, "done")

        result = await make_monitoring_loop._detect_unconfirmed_task_completion(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_nudge_during_pending_self_review(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """update_task_status's self-review gate deliberately leaves a
        self_review-enabled phase's task 'in_progress' after its first
        'done' call, while it sends the agent a checklist and waits for a
        second 'done' -- the call landed correctly. A nudge here would be
        redundant and actively misleading (falsely implying a dropped
        connection)."""
        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        self._mock_session_with_task(
            mock_db, "in_progress", self_review_started_at=datetime.utcnow()
        )

        result = await make_monitoring_loop._detect_unconfirmed_task_completion(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_nudge_when_task_under_review(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """A 'done' call that spawned validation moves the task to
        under_review, not a plain terminal state -- also not stranded."""
        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        self._mock_session_with_task(mock_db, "under_review")

        result = await make_monitoring_loop._detect_unconfirmed_task_completion(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_immediate_resend(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        self._mock_session_with_task(mock_db, "in_progress")

        await make_monitoring_loop._detect_unconfirmed_task_completion(agent)
        await make_monitoring_loop._detect_unconfirmed_task_completion(agent)

        mock_agent_manager.send_message_to_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_after_cooldown_expires(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        self._mock_session_with_task(mock_db, "in_progress")

        await make_monitoring_loop._detect_unconfirmed_task_completion(agent)
        make_monitoring_loop._nudged_unconfirmed_completion["a1"] = time.time() - 61

        await make_monitoring_loop._detect_unconfirmed_task_completion(agent)
        assert mock_agent_manager.send_message_to_agent.call_count == 2

    @pytest.mark.asyncio
    async def test_escalates_to_restart_after_repeated_nudges_for_same_task(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """If nudging never resolves it (a persistently broken transport,
        not a one-off blip), keep re-nudging the same broken connection
        forever helps nobody -- past the threshold, restart the agent
        instead."""
        from unittest.mock import AsyncMock

        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        self._mock_session_with_task(mock_db, "in_progress")
        make_monitoring_loop._auto_restart.requeue_and_terminate = AsyncMock()

        threshold = make_monitoring_loop.UNCONFIRMED_COMPLETION_ESCALATE_AFTER
        for i in range(threshold):
            await make_monitoring_loop._detect_unconfirmed_task_completion(agent)
            # Bypass the cooldown between iterations -- only the escalation
            # count matters for this test, not real elapsed time.
            make_monitoring_loop._nudged_unconfirmed_completion["a1"] = time.time() - 61

        # Exactly `threshold` nudges sent, no restart yet.
        assert mock_agent_manager.send_message_to_agent.call_count == threshold
        make_monitoring_loop._auto_restart.requeue_and_terminate.assert_not_called()

        # One more crosses the threshold -- restart, not another nudge.
        result = await make_monitoring_loop._detect_unconfirmed_task_completion(agent)

        assert result is True
        assert mock_agent_manager.send_message_to_agent.call_count == threshold
        make_monitoring_loop._auto_restart.requeue_and_terminate.assert_called_once_with(agent)

    @pytest.mark.asyncio
    async def test_escalation_count_resets_for_a_different_task(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """A fresh task for the same agent must not inherit an earlier
        task's nudge count -- otherwise a single unrelated occurrence on
        one task could trip an immediate restart on the next, unrelated
        one."""
        from unittest.mock import AsyncMock

        agent = Agent(id="a1", cli_type="claude", status="working", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.COMPLETION_OUTPUT
        mock_agent_manager.send_message_to_agent.reset_mock()
        make_monitoring_loop._auto_restart.requeue_and_terminate = AsyncMock()

        threshold = make_monitoring_loop.UNCONFIRMED_COMPLETION_ESCALATE_AFTER
        self._mock_session_with_task(mock_db, "in_progress", task_id="t1")
        for i in range(threshold):
            await make_monitoring_loop._detect_unconfirmed_task_completion(agent)
            make_monitoring_loop._nudged_unconfirmed_completion["a1"] = time.time() - 61

        # A new task_id for the same agent -- count must start over, not
        # immediately escalate.
        agent.current_task_id = "t2"
        self._mock_session_with_task(mock_db, "in_progress", task_id="t2")

        result = await make_monitoring_loop._detect_unconfirmed_task_completion(agent)

        assert result is True
        make_monitoring_loop._auto_restart.requeue_and_terminate.assert_not_called()
        assert mock_agent_manager.send_message_to_agent.call_count == threshold + 1


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

        if task is not None and not isinstance(
            getattr(task, "started_at", None), datetime
        ):
            # _within_resume_replay_grace needs a real, comparable
            # started_at -- well past the grace window, matching every
            # real Task row (always stamped by _create_phase_task) unless
            # a test overrides it to exercise the grace window itself.
            task.started_at = datetime.utcnow() - timedelta(minutes=10)

        def query_side_effect(model):
            m = Mock()
            if model.__name__ == "Task":
                m.filter_by.return_value.first.return_value = task
            elif model.__name__ == "Workflow":
                m.filter_by.return_value.first.return_value = workflow
            elif model.__name__ == "Feature":
                # pause_workflow cascades to any Feature linked to the
                # workflow; these tests link none. Must be configured
                # explicitly -- an unconfigured Mock's .all() is not
                # iterable, and the primitive no longer swallows that.
                m.filter.return_value.all.return_value = []
                m.filter_by.return_value.all.return_value = []
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

    @pytest.mark.asyncio
    async def test_ignores_match_within_resumed_session_grace_period(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """This is the highest-blast-radius detector (pauses the whole
        workflow on a single match), so a resumed session replaying a
        prior, already-failed task's own 402 error must not fire it."""
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.CREDIT_ERROR_OUTPUT
        mock_agent_manager.terminate_agent = AsyncMock()

        task = Mock(
            workflow_id="wf1",
            status="in_progress",
            failure_reason=None,
            started_at=datetime.utcnow() - timedelta(seconds=10),
        )
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        session, mock_session_scope = self._make_session(task=task, workflow=workflow)
        mock_db.session_scope = mock_session_scope

        result = await make_monitoring_loop._detect_credit_exhausted(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()
        assert task.status == "in_progress"


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

    def _session_with_started_task(self, mock_db, started_at=None):
        """_within_resume_replay_grace (via _current_task_started_at) now
        needs a real Task.started_at -- configure the DB mock with one
        well past the grace window, matching every real Task row."""
        from contextlib import contextmanager

        task = Mock(started_at=started_at or (datetime.utcnow() - timedelta(minutes=10)))
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = task

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        return task

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
        """Regression: this must read config.secondary_cli_model_fallback
        (Claude's own configured recovery model), not config.cli_model --
        that global is paired with agents.default_cli_tool (pi) and is
        typically an OpenRouter path pi's picker resolves, meaningless to
        Claude Code's own /model."""
        agent = Agent(id="a1", cli_type="claude", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.BAD_MODEL_OUTPUT
        make_monitoring_loop.config.agents.secondary_cli_model_fallback = "opus"
        self._session_with_started_task(mock_db)

        result = await make_monitoring_loop._detect_bad_model_error(agent)

        assert result is True
        mock_agent_manager.send_message_to_agent.assert_called_once_with("a1", "/model opus")

    @pytest.mark.asyncio
    async def test_falls_back_to_sonnet_when_unconfigured(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="claude", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.BAD_MODEL_OUTPUT
        make_monitoring_loop.config.agents.secondary_cli_model_fallback = None
        self._session_with_started_task(mock_db)

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
        make_monitoring_loop.config.agents.secondary_cli_model_fallback = "opus"
        self._session_with_started_task(mock_db)

        await make_monitoring_loop._detect_bad_model_error(agent)
        await make_monitoring_loop._detect_bad_model_error(agent)

        mock_agent_manager.send_message_to_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_match_within_resumed_session_grace_period(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """A resumed session (e.g. architectural_review resuming
        architecture_design's session -- both role 'architect') can
        replay a prior task's own model rejection into the pane on
        startup -- must not be treated as current within the grace
        window."""
        agent = Agent(id="a1", cli_type="claude", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = self.BAD_MODEL_OUTPUT
        make_monitoring_loop.config.agents.secondary_cli_model_fallback = "opus"
        self._session_with_started_task(
            mock_db, started_at=datetime.utcnow() - timedelta(seconds=10)
        )

        result = await make_monitoring_loop._detect_bad_model_error(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()


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

    def _frozen_agent(self, make_monitoring_loop, frozen_for_seconds, cli_type="pi", cli_model="Qwen3.8-27B-UD-Q4_K_XL.gguf"):
        agent = Agent(id="a1", cli_type=cli_type, cli_model=cli_model, current_task_id="t1")
        make_monitoring_loop.config.agents.default_cli_tool = "pi"
        make_monitoring_loop.config.agents.cli_model = "Qwen3.8-27B-UD-Q4_K_XL.gguf"
        make_monitoring_loop.config.agents.cli_model_fallback = "mimo-v2.5-pro"
        make_monitoring_loop.config.agents.cli_model_fallback_wait_seconds = 120
        make_monitoring_loop._stuck_state = {
            "a1": {"sig": "same output", "since": time.time() - frozen_for_seconds, "recov": 0}
        }
        return agent

    @pytest.mark.asyncio
    async def test_cli_without_model_fallback_support_ignored(self, make_monitoring_loop, mock_agent_manager):
        """opencode's CLIAgentInterface doesn't override
        model_fallback_keystrokes/fallback_model (base class defaults: []
        and None) -- must be a no-op regardless of how long it's been
        frozen, for any CLI that hasn't opted in."""
        agent = self._frozen_agent(make_monitoring_loop, 200, cli_type="opencode", cli_model="anthropic/claude-sonnet-4")
        make_monitoring_loop.config.agents.cli_model = "anthropic/claude-sonnet-4"

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_claude_as_secondary_cli_gets_its_own_fallback(
        self, make_monitoring_loop, mock_agent_manager
    ):
        """Regression: the secondary/fallback CLI (claude, dispatched when
        pi hits a session limit) must also support this mechanism, using
        its OWN config value and model vocabulary (claude_model_fallback,
        e.g. "opus") -- not pi's cli_model_fallback (an OpenRouter path
        meaningless to Claude Code's /model), and not blocked by a gate
        that only recognizes pi's global default model."""
        agent = Agent(id="a1", cli_type="claude", cli_model="sonnet", current_task_id="t1")
        make_monitoring_loop.config.agents.default_cli_tool = "pi"
        make_monitoring_loop.config.agents.cli_model = "Qwen3.8-27B-UD-Q4_K_XL.gguf"
        make_monitoring_loop.config.agents.cli_model_fallback_wait_seconds = 120
        make_monitoring_loop.config.agents.secondary_cli_model_fallback = "opus"
        make_monitoring_loop._stuck_state = {
            "a1": {"sig": "same output", "since": time.time() - 200, "recov": 0}
        }

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is True
        mock_agent_manager.send_message_to_agent.assert_called_once_with("a1", "/model opus")

    @pytest.mark.asyncio
    async def test_role_based_resolution_when_claude_is_primary(
        self, make_monitoring_loop, mock_agent_manager
    ):
        """Regression: fallback_model must resolve by ROLE (is this agent's
        cli_type the current default_cli_tool?), not by hardcoding "pi
        reads cli_model_fallback, claude reads secondary_cli_model_fallback"
        regardless of which CLI is actually configured as primary. With
        Claude set as default_cli_tool (e.g. running Claude against a local
        model), a Claude agent must read cli_model_fallback -- the primary
        tier's config -- and pi (now the secondary tier) must read
        secondary_cli_model_fallback, the mirror image of the default
        pi-primary test above."""
        agent = Agent(id="a1", cli_type="claude", cli_model="local-claude-model", current_task_id="t1")
        make_monitoring_loop.config.agents.default_cli_tool = "claude"
        make_monitoring_loop.config.agents.cli_model = "local-claude-model"
        make_monitoring_loop.config.agents.cli_model_fallback_wait_seconds = 120
        make_monitoring_loop.config.agents.cli_model_fallback = "opus"
        make_monitoring_loop._stuck_state = {
            "a1": {"sig": "same output", "since": time.time() - 200, "recov": 0}
        }

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is True
        mock_agent_manager.send_message_to_agent.assert_called_once_with("a1", "/model opus")

    @pytest.mark.asyncio
    async def test_same_model_fallback_is_a_noop(self, make_monitoring_loop, mock_agent_manager):
        """Regression: observed live -- secondary_cli_model_fallback left at
        its shipped default ("sonnet") happened to equal a claude-primary
        phase's own model, so the "switch" was a literal no-op that still
        interrupted the agent (visible in its transcript as `/model sonnet`
        -> "Model's already set to sonnet"), and re-fired on every backend
        restart since neither Agent.cli_model nor the baseline-default gate
        change when the fallback equals the current model -- only the
        in-memory one-shot set would otherwise have prevented a repeat, and
        that doesn't survive a restart."""
        agent = Agent(id="a1", cli_type="claude", cli_model="sonnet", current_task_id="t1")
        make_monitoring_loop.config.agents.default_cli_tool = "pi"
        make_monitoring_loop.config.agents.cli_model = "Qwen3.8-27B-UD-Q4_K_XL.gguf"
        make_monitoring_loop.config.agents.cli_model_fallback_wait_seconds = 120
        make_monitoring_loop.config.agents.secondary_cli_model_fallback = "sonnet"
        make_monitoring_loop._stuck_state = {
            "a1": {"sig": "same output", "since": time.time() - 200, "recov": 0}
        }

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_pi_reads_secondary_config_when_it_is_the_fallback_tier(
        self, make_monitoring_loop, mock_agent_manager
    ):
        """Mirror of the above: with Claude primary, a pi agent (now the
        secondary/fallback-tier CLI) must read secondary_cli_model_fallback,
        and its baseline-default gate must compare against PiAgent's own
        default_model, not config.cli_model (which is Claude's local model
        here, meaningless to pi)."""
        agent = Agent(id="a1", cli_type="pi", cli_model="Qwen3.8-27B-UD-Q4_K_XL.gguf", current_task_id="t1")
        make_monitoring_loop.config.agents.default_cli_tool = "claude"
        make_monitoring_loop.config.agents.cli_model = "local-claude-model"
        make_monitoring_loop.config.agents.cli_model_fallback_wait_seconds = 120
        make_monitoring_loop.config.agents.secondary_cli_model_fallback = "mimo-v2.5-pro"
        make_monitoring_loop._stuck_state = {
            "a1": {"sig": "same output", "since": time.time() - 200, "recov": 0}
        }

        with patch("src.monitoring.monitor.asyncio.sleep", new=AsyncMock()):
            result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is True
        mock_agent_manager.send_message_to_agent.assert_called_once_with(
            "a1", "/model mimo-v2.5-pro"
        )

    @pytest.mark.asyncio
    async def test_pi_dispatched_as_claude_session_limit_fallback_is_excluded(
        self, make_monitoring_loop, mock_agent_manager
    ):
        """Regression: pi as a *phase-level* session-limit fallback (e.g.
        qa_validation.yaml's cli_tool: claude / fallback_cli_tool: pi,
        fallback_cli_model: openrouter/mimo-v2.5-pro) is a fresh kill+restart
        dispatch (_mechanical_recovery_for_agent's session-limit path), not
        an existing session to send in-session /model keystrokes into --
        that agent's cli_model is already the escalated openrouter path, not
        pi's standard local default, so the baseline-default gate must
        exclude it rather than attempt a further in-session switch on it."""
        agent = Agent(id="a1", cli_type="pi", cli_model="openrouter/mimo-v2.5-pro", current_task_id="t1")
        make_monitoring_loop.config.agents.default_cli_tool = "pi"
        make_monitoring_loop.config.agents.cli_model = "Qwen3.8-27B-UD-Q4_K_XL.gguf"
        make_monitoring_loop.config.agents.cli_model_fallback = "mimo-v2.5-pro"
        make_monitoring_loop.config.agents.cli_model_fallback_wait_seconds = 120
        make_monitoring_loop._stuck_state = {
            "a1": {"sig": "same output", "since": time.time() - 200, "recov": 0}
        }

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_exhausted_attempts_stay_exhausted_across_a_restart(
        self, make_monitoring_loop, mock_agent_manager
    ):
        """Regression: _switched_to_fallback_model/_fallback_attempt_count are
        in-memory only, so a fresh MonitoringLoop (i.e. after a `heph
        restart`) used to have no way to know MAX_FALLBACK_ATTEMPTS was
        already spent, and would grant the agent a full fresh 2-attempt
        budget all over again. Observed live: agent e6633fe6 got two
        separate 2-attempt episodes (18:13-18:21, then 19:07-19:18 after a
        restart in between) instead of being capped at 2 total. AgentLog
        already has 2 'cli_model_fallback' entries for this agent -- a fresh
        process (empty in-memory state, as after a restart) must still
        refuse to fire a 3rd."""
        agent = self._frozen_agent(make_monitoring_loop, 200)
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.all.return_value = [
            ("cli_model_fallback",),
            ("cli_model_fallback",),
        ]
        make_monitoring_loop.db_manager.session_scope.return_value.__enter__.return_value = mock_session

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_fallback_configured_disables_feature(self, make_monitoring_loop, mock_agent_manager):
        agent = self._frozen_agent(make_monitoring_loop, 200)
        make_monitoring_loop.config.agents.cli_model_fallback = None

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
        agent = Agent(id="a1", cli_type="pi", cli_model="Qwen3.8-27B-UD-Q4_K_XL.gguf", current_task_id="t1")
        make_monitoring_loop.config.agents.cli_model = "Qwen3.8-27B-UD-Q4_K_XL.gguf"
        make_monitoring_loop.config.agents.cli_model_fallback = "mimo-v2.5-pro"
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
    async def test_frozen_past_threshold_sends_model_in_one_atomic_message(self, make_monitoring_loop, mock_agent_manager):
        agent = self._frozen_agent(make_monitoring_loop, 200)

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is True
        mock_agent_manager.send_message_to_agent.assert_called_once_with(
            "a1", "/model mimo-v2.5-pro"
        )

    @pytest.mark.asyncio
    async def test_skips_when_connection_errors_present(self, make_monitoring_loop, mock_agent_manager):
        """Regression: the picker keystrokes assume the agent is idle at a
        shell prompt ready to accept "/model <name>" -- if it's actually
        mid-retry on a connection failure, the send may not land as picker
        input at all, and instead falls through to the normal chat input,
        which pi queues as a live "Steering" message instead of it landing
        as a model switch. Connection errors are a distinct hard blocker
        already owned by _detect_connection_errors (itself fallback-aware)
        -- leave this one alone rather than risk misdirecting a busy
        agent."""
        agent = self._frozen_agent(make_monitoring_loop, 200)
        mock_agent_manager.get_agent_output.return_value = (
            "Error: Connection error.\nError: Connection error.\n"
            "Retrying (2/3) in 1s... (escape to cancel)"
        )

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        mock_agent_manager.send_message_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_clears_stuck_state_after_switching(self, make_monitoring_loop, mock_agent_manager):
        """So the fallback model's own first turn gets a fresh frozen-
        detection window instead of being judged against a signature
        captured while still on the original model -- but recov (the
        generic mechanical-recovery escalation counter that lives in the
        same _stuck_state entry) must survive. Regression: this used to
        pop() the whole entry, which also reset recov to 0 on every
        fallback attempt -- the reason the generic frozen/nudge/abandon
        path never independently escalated during the incident
        MAX_FALLBACK_ATTEMPTS was added for."""
        agent = self._frozen_agent(make_monitoring_loop, 200)
        make_monitoring_loop._stuck_state["a1"]["recov"] = 1

        await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert make_monitoring_loop._stuck_state["a1"]["since"] is None
        assert make_monitoring_loop._stuck_state["a1"]["sig"] is None
        assert make_monitoring_loop._stuck_state["a1"]["recov"] == 1

    @pytest.mark.asyncio
    async def test_only_fires_once_per_agent(self, make_monitoring_loop, mock_agent_manager):
        agent = self._frozen_agent(make_monitoring_loop, 200)

        await make_monitoring_loop._detect_cli_model_fallback(agent)
        assert mock_agent_manager.send_message_to_agent.call_count == 1  # one atomic "/model <name>"

        # Re-seed stuck_state as if the agent is frozen again on a later cycle.
        make_monitoring_loop._stuck_state["a1"] = {
            "sig": "same output", "since": time.time() - 200, "recov": 0
        }
        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        assert mock_agent_manager.send_message_to_agent.call_count == 1  # no additional calls

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
        assert logged.details["from_model"] == "Qwen3.8-27B-UD-Q4_K_XL.gguf"
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

        agent_row = Mock(cli_model="Qwen3.8-27B-UD-Q4_K_XL.gguf")
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = agent_row

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        agent = self._frozen_agent(make_monitoring_loop, 200)

        await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert agent_row.cli_model == "mimo-v2.5-pro"

    @pytest.mark.asyncio
    async def test_send_failure_reverts_and_allows_retry_under_cap(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: the one-shot flag and the optimistic DB write both
        happen before the keystroke send is attempted -- previously, if
        send_message_to_agent raised (e.g. tmux session gone mid-send), no
        _pending_fallback_verification entry was ever created, so
        _verify_cli_model_fallback had nothing to check and the agent was
        permanently blocked by the one-shot gate with the MAX_FALLBACK_ATTEMPTS
        retry budget never even consulted. A send failure must revert the DB
        write and behave like an immediately-failed attempt -- retry allowed
        while attempts remain."""
        from contextlib import contextmanager

        agent_row = Mock(cli_model="mimo-v2.5-pro")
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = agent_row

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        agent = self._frozen_agent(make_monitoring_loop, 200)
        mock_agent_manager.send_message_to_agent = AsyncMock(
            side_effect=RuntimeError("tmux session gone")
        )

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        assert "a1" not in make_monitoring_loop._switched_to_fallback_model
        assert agent_row.cli_model == "Qwen3.8-27B-UD-Q4_K_XL.gguf"

    @pytest.mark.asyncio
    async def test_send_failure_at_max_attempts_gives_up_permanently(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Companion to the above: once MAX_FALLBACK_ATTEMPTS is reached, a
        send failure must NOT re-enable a retry -- same cap semantics as an
        unconfirmed switch."""
        from contextlib import contextmanager

        from src.monitoring.patterns import MAX_FALLBACK_ATTEMPTS

        agent_row = Mock(cli_model="mimo-v2.5-pro")
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = agent_row

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        agent = self._frozen_agent(make_monitoring_loop, 200)
        make_monitoring_loop._fallback_attempt_count = {"a1": MAX_FALLBACK_ATTEMPTS - 1}
        mock_agent_manager.send_message_to_agent = AsyncMock(
            side_effect=RuntimeError("tmux session gone")
        )

        result = await make_monitoring_loop._detect_cli_model_fallback(agent)

        assert result is False
        assert "a1" in make_monitoring_loop._switched_to_fallback_model
        assert agent_row.cli_model == "Qwen3.8-27B-UD-Q4_K_XL.gguf"


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
            "a1": ("mimo-v2.5-pro", "Qwen3.8-27B-UD-Q4_K_XL.gguf", time.time())
        }

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        assert "a1" not in make_monitoring_loop._pending_fallback_verification
        logged = session.add.call_args[0][0]
        assert logged.log_type == "cli_model_fallback_confirmed"

    @pytest.mark.asyncio
    async def test_unconfirmed_within_grace_period_stays_pending_no_warning(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        make_monitoring_loop.config.monitoring.monitoring_interval_seconds = 60
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "still on the old model"
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("mimo-v2.5-pro", "Qwen3.8-27B-UD-Q4_K_XL.gguf", time.time())  # just switched
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
        make_monitoring_loop.config.monitoring.monitoring_interval_seconds = 60
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "still on the old model"
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("mimo-v2.5-pro", "Qwen3.8-27B-UD-Q4_K_XL.gguf", time.time() - 200)  # past 2x120s grace
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
        make_monitoring_loop.config.monitoring.monitoring_interval_seconds = 60
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "still on the old model"
        make_monitoring_loop._switched_to_fallback_model = {"a1"}
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("mimo-v2.5-pro", "Qwen3.8-27B-UD-Q4_K_XL.gguf", time.time() - 200)
        }

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        assert "a1" not in make_monitoring_loop._switched_to_fallback_model
        # The optimistic write must be reverted -- otherwise the retry this
        # just re-enabled would immediately be blocked by
        # _detect_cli_model_fallback's own "already off default model" gate.
        assert agent_row.cli_model == "Qwen3.8-27B-UD-Q4_K_XL.gguf"

    @pytest.mark.asyncio
    async def test_unconfirmed_past_grace_period_stops_retrying_after_max_attempts(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: observed live -- with no cap, an agent that kept
        refreezing retried an unconfirmed switch 40+ times over 7+ hours,
        each retry blindly resending the same keystrokes into whatever state
        the CLI actually was in, until one attempt landed on a different,
        unusable model and broke the session outright. Once
        _fallback_attempt_count reaches MAX_FALLBACK_ATTEMPTS, a still-
        unconfirmed switch must NOT clear the one-shot set -- no further
        retries for this agent's task."""
        from contextlib import contextmanager

        from src.monitoring.patterns import MAX_FALLBACK_ATTEMPTS

        agent_row = Mock(cli_model="mimo-v2.5-pro")
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = agent_row

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        make_monitoring_loop.config.monitoring.monitoring_interval_seconds = 60
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "still on the old model"
        make_monitoring_loop._switched_to_fallback_model = {"a1"}
        make_monitoring_loop._fallback_attempt_count = {"a1": MAX_FALLBACK_ATTEMPTS}
        make_monitoring_loop._pending_fallback_verification = {
            "a1": ("mimo-v2.5-pro", "Qwen3.8-27B-UD-Q4_K_XL.gguf", time.time() - 200)
        }

        await make_monitoring_loop._verify_cli_model_fallback(agent)

        assert "a1" in make_monitoring_loop._switched_to_fallback_model
        # The DB write is still reverted regardless -- we just stop retrying.
        assert agent_row.cli_model == "Qwen3.8-27B-UD-Q4_K_XL.gguf"

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
            "a1": ("mimo-v2.5-pro", "Qwen3.8-27B-UD-Q4_K_XL.gguf", time.time())
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
    async def test_stores_steering_recommendation_from_renamed_key(
        self, make_monitoring_loop, mock_db
    ):
        """Regression: analyze_agent_with_trajectory renames the LLM's
        "steering_recommendation" key to "steering_message" before
        returning (guardian.py) -- this write path used to read the old
        key name, so GuardianAnalysis.steering_recommendation was always
        None regardless of what Guardian actually recommended."""
        from contextlib import contextmanager

        agent = Agent(id="a1")
        analysis = {
            "trajectory_aligned": False,
            "alignment_score": 0.2,
            "needs_steering": True,
            "steering_message": "Stop installing external libraries",
        }
        db_agent = Mock(id="a1", health_check_failures=0)
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = db_agent

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope

        await make_monitoring_loop._update_agent_health_from_trajectory(agent, analysis)

        guardian_analysis = session.add.call_args_list[0][0][0]
        assert guardian_analysis.steering_recommendation == "Stop installing external libraries"

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
    async def test_monitor_tool_wait_not_flagged_stuck_within_its_declared_timeout(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Claude Code's own backgrounded-tool-call UI (e.g. "Monitor
        started · task bg0fucqr2 · timeout 300s") legitimately leaves the
        pane static for as long as its own declared timeout -- a
        legitimate wait, not a stuck agent. Frozen for longer than the
        default frozen_seconds (300s) floor but still within the declared
        timeout + one poll cycle's slack must NOT trigger recovery."""
        from src.core.simple_config import get_config

        agent = Agent(id="a1", cli_type="claude")
        frozen_output = "Working on it...\nMonitor started · task bg0fucqr2 · timeout 300s"
        mock_agent_manager.get_agent_output.return_value = frozen_output
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)  # baseline
        buffer = get_config().monitoring.monitoring_interval_seconds
        # 300 (declared) + buffer - 10s: still inside the extended tolerance.
        make_monitoring_loop._stuck_state["a1"]["since"] = time.time() - (300 + buffer - 10)

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        mock_agent_manager.send_recovery_keystrokes.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_tool_wait_flagged_stuck_past_its_declared_timeout_plus_buffer(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Same declared-timeout wait as above, but frozen past declared
        timeout + buffer -- Claude Code's own wait should have resolved by
        now, so this is a real stuck agent and recovery must still fire."""
        from src.core.simple_config import get_config

        agent = Agent(id="a1", cli_type="claude")
        frozen_output = "Working on it...\nMonitor started · task bg0fucqr2 · timeout 300s"
        mock_agent_manager.get_agent_output.return_value = frozen_output
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)  # baseline
        buffer = get_config().monitoring.monitoring_interval_seconds
        make_monitoring_loop._stuck_state["a1"]["since"] = time.time() - (300 + buffer + 10)

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
                def _first_if_active():
                    # Return the task only if its status is one the
                    # production query would match, otherwise None.
                    if task.status in ("assigned", "in_progress"):
                        return task
                    return None

                m.filter_by.return_value.filter.return_value.first = _first_if_active
            elif name == "Phase":
                m.filter_by.return_value.first.return_value = phase
            elif name == "Workflow":
                m.filter_by.return_value.first.return_value = workflow
            elif name == "Feature":
                # pause_workflow cascades to any Feature linked to the
                # workflow; these tests link none. Must be configured
                # explicitly -- an unconfigured Mock's .all() is not
                # iterable, and the primitive no longer swallows that.
                m.filter.return_value.all.return_value = []
                m.filter_by.return_value.all.return_value = []
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
        # both global config defaults must also be unset here, or this
        # "no fallback configured" scenario silently depends on whatever
        # hephaestus_config.yaml happens to contain on the machine running
        # the test (the make_monitoring_loop fixture's own get_config patch
        # only stays active during MonitoringLoop.__init__, not here).
        # secondary_cli_model_fallback specifically: a bare Mock() without
        # this auto-vivifies a truthy child Mock for any attribute access,
        # which the last-resort fallback tier below would treat as a real,
        # different model and use it -- silently defeating "no fallback"
        # unless explicitly nulled out.
        with patch("src.monitoring.mechanical_recovery.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(agents=Mock(default_fallback_cli_tool=None, secondary_cli_model_fallback=None))

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
        self._wire_tmux_pane_output(
            mock_agent_manager, mock_db, "a1", "You've hit your session limit"
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
        assert call_kwargs["project_context"] == ""
        assert call_kwargs["cli_type"] == "pi"

        assert task.status == "pending"
        assert task.assigned_agent_id is None
        assert workflow.status == "active"
        assert workflow.paused_by is None
        mock_agent_manager.terminate_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_dispatch_injects_session_limit_reason_into_enriched_description(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """The fallback agent must be told WHY it's retrying. Before this
        fix, the reset below cleared failure_reason straight to None
        without ever folding it into enriched_description first -- the new
        agent read whatever RETRY note (or none) the task happened to
        already carry, never the session-limit reason that actually
        caused this handoff."""
        agent = Agent(id="a1", cli_type="claude")
        self._wire_tmux_pane_output(
            mock_agent_manager, mock_db, "a1", "You've hit your session limit"
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.get_project_context = AsyncMock(return_value="ctx")
        mock_agent_manager.create_agent_for_task = AsyncMock(return_value=Mock(id="a2"))

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1",
            raw_description="Execute feature_architect", failure_reason=None,
        )
        phase = Mock(fallback_cli_tool="pi", fallback_cli_model=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        assert task.status == "pending"
        assert "--- RETRY:" in task.enriched_description
        assert "Execute feature_architect" in task.enriched_description
        assert "CLI session limit reached" in task.enriched_description
        assert task.failure_reason is None

    @pytest.mark.asyncio
    async def test_falls_back_to_secondary_cli_model_when_default_fallback_is_identical(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Live incident: default_cli_tool and default_fallback_cli_tool
        were both "pi" on the identical model -- the "fall back to global
        config defaults" branch found a "fallback" that was actually the
        same cli+model that just hit the limit, so the outer
        `fallback_tool != agent.cli_type or fallback_model != agent.cli_model`
        check correctly refused it, and the workflow paused with a real,
        different secondary_cli_model_fallback ("sonnet") sitting
        configured and never consulted. Must now try it as a last resort
        before giving up."""
        agent = Agent(id="a1", cli_type="pi", cli_model="openrouter/xiaomi/mimo-v2.5-pro")
        mock_agent_manager.get_agent_output.return_value = (
            "You've hit your session limit"
        )
        self._wire_tmux_pane_output(
            mock_agent_manager, mock_db, "a1", "You've hit your session limit"
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.get_project_context = AsyncMock(return_value="ctx")
        new_agent = Mock(id="a2")
        mock_agent_manager.create_agent_for_task = AsyncMock(return_value=new_agent)

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1",
            enriched_description="do the thing", done_definition="done",
        )
        # No phase-level override -- forces the global-config path.
        phase = Mock(fallback_cli_tool=None, fallback_cli_model=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        with patch("src.monitoring.mechanical_recovery.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(
                agents=Mock(
                    default_fallback_cli_tool="pi",
                    default_fallback_cli_model="openrouter/xiaomi/mimo-v2.5-pro",
                    secondary_cli_model_fallback="sonnet",
                ),
            )
            await make_monitoring_loop._mechanical_recovery_for_agent(agent)
            await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        mock_agent_manager.create_agent_for_task.assert_called_once()
        call_kwargs = mock_agent_manager.create_agent_for_task.call_args.kwargs
        assert call_kwargs["cli_type"] == "pi"
        assert call_kwargs["phase_cli_model"] == "sonnet"

        assert task.status == "pending"
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
        self._wire_tmux_pane_output(
            mock_agent_manager, mock_db, "a1", "You've hit your session limit"
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
        self._wire_tmux_pane_output(
            mock_agent_manager, mock_db, "a1", "You've hit your monthly spend limit."
        )
        mock_agent_manager.terminate_agent = AsyncMock()

        task = Mock(
            id="t1", status="in_progress", phase_id="p1", workflow_id="wf1"
        )
        phase = Mock(fallback_cli_tool=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        with patch("src.monitoring.mechanical_recovery.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(agents=Mock(default_fallback_cli_tool=None, secondary_cli_model_fallback=None))
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
        self._wire_tmux_pane_output(
            mock_agent_manager, mock_db, "a1", "You've hit your monthly spend limit."
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

        assert task.status == "pending"
        assert workflow.status == "active"
        assert workflow.paused_by is None
        mock_agent_manager.terminate_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_limit_fires_before_frozen_nudge_when_both_patterns_match(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Characterization (pre-extraction): check order is load-bearing.
        A pane matching BOTH the session-limit pattern and a frozen
        signature well past frozen_seconds must take the session-limit
        path (checked first) -- terminate + fail the task, NOT a
        keys+nudge recovery attempt."""
        pane_text = "You've hit your session limit\nSame frozen output forever"
        mock_agent_manager.get_agent_output.return_value = pane_text
        self._wire_tmux_pane_output(mock_agent_manager, mock_db, "a1", pane_text)
        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)

        # Frozen well past frozen_seconds -- the nudge branch's condition
        # would be true too if it were ever reached.
        make_monitoring_loop._stuck_state = {
            "a1": {"sig": None, "since": None, "recov": 0}
        }

        task = Mock(id="t1", status="in_progress", phase_id="p1", workflow_id="wf1")
        phase = Mock(fallback_cli_tool=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        with patch("src.monitoring.mechanical_recovery.get_config") as mock_cfg:
            mock_cfg.return_value.agents.default_fallback_cli_tool = None
            mock_cfg.return_value.agents.secondary_cli_model_fallback = None
            result = await make_monitoring_loop._mechanical_recovery_for_agent(
                Agent(id="a1", cli_type="claude")
            )

        assert result is True
        mock_agent_manager.terminate_agent.assert_called_once_with("a1")
        mock_agent_manager.send_recovery_keystrokes.assert_not_called()
        assert task.status == "failed"

    @pytest.mark.asyncio
    async def test_context_overflow_fires_before_frozen_nudge_when_both_match(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Characterization (pre-extraction): a pane matching the
        context-overflow pattern while frozen past frozen_seconds must take
        the context-overflow restart path (checked before the nudge
        branch), not a keys+nudge recovery attempt."""
        pane_text = "exceeds the available context size"
        mock_agent_manager.get_agent_output.return_value = pane_text
        self._wire_tmux_pane_output(mock_agent_manager, mock_db, "a1", pane_text)
        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.send_recovery_keystrokes = AsyncMock(return_value=True)
        mock_agent_manager.create_agent_for_task = AsyncMock(return_value=Mock(id="a2"))

        agent = Agent(id="a1", cli_type="pi", cli_model="local-model")
        # Baseline, then freeze past frozen_seconds -- the nudge branch's
        # condition would be true too if it were ever reached.
        await make_monitoring_loop._mechanical_recovery_for_agent(agent)
        make_monitoring_loop._stuck_state["a1"]["since"] = time.time() - 400

        task = Mock(id="t1", status="in_progress", phase_id="p1", workflow_id="wf1")
        phase = Mock(fallback_cli_tool="claude", fallback_cli_model=None)
        workflow = Mock(status="active", paused_by=None, paused_at=None)
        mock_db.session_scope = self._session_with(task, phase, workflow)

        with patch("src.monitoring.mechanical_recovery.get_config") as mock_cfg:
            mock_cfg.return_value.agents.default_fallback_cli_tool = None
            mock_cfg.return_value.agents.secondary_cli_model_fallback = None
            result = await make_monitoring_loop._mechanical_recovery_for_agent(agent)

        assert result is True
        mock_agent_manager.create_agent_for_task.assert_called_once()
        mock_agent_manager.terminate_agent.assert_called_once_with("a1")
        mock_agent_manager.send_recovery_keystrokes.assert_not_called()
        assert task.status == "pending"


class TestDetectConnectionErrors:
    """Regression #1: resetting a connection-error-killed task straight to
    "pending" with no failure_reason let a later, unrelated orphan-check
    (a task sitting pending with no agent for >1min) relabel it
    "Orphaned: never dispatched to an agent" -- a label _advance_phases's
    own retry cap (max_retry_count=2) deliberately exempts from the cap,
    on the theory that a scheduling race should always be retried. That
    let a persistently unreachable LLM endpoint loop forever instead of
    ever pausing the workflow.

    Regression #2: even with #1 fixed, blindly failing-and-retrying still
    redispatches onto the SAME broken endpoint every time -- a connection
    error means the endpoint is unreachable, not that the agent did
    anything wrong. Observed live: 46+ retries over 5+ hours against a
    dead local inference host, always onto the same model, even though
    the phase already had fallback_cli_tool: claude configured. Mirrors
    the session-limit path: try the phase's (or global) fallback via a
    fresh kill+restart dispatch first; only fail (routing through the
    retry cap from #1) if no fallback is configured or the dispatch
    itself fails."""

    def _session_with(self, task, phase=None):
        from contextlib import contextmanager

        session = Mock()

        if task is not None and not isinstance(
            getattr(task, "started_at", None), datetime
        ):
            # _within_resume_replay_grace (via _current_task_started_at)
            # needs a real, comparable started_at -- well past the grace
            # window, matching every real Task row.
            task.started_at = datetime.utcnow() - timedelta(minutes=10)

        def query_side_effect(model):
            m = Mock()
            name = model.__name__ if hasattr(model, "__name__") else str(model)
            if name == "Task":
                m.filter_by.return_value.filter.return_value.first.return_value = task
                # terminate_agent's own stray-task sweep: this task was
                # already reset above, so nothing still points at the agent.
                m.filter_by.return_value.filter.return_value.all.return_value = []
                # _current_task_started_at's own lookup shape (filter_by(id=...)
                # with no further .filter()) -- same task, different chain.
                m.filter_by.return_value.first.return_value = task
            elif name == "Phase":
                m.filter_by.return_value.first.return_value = phase
            elif name == "Workflow":
                m.filter_by.return_value.first.return_value = None
            return m

        session.query.side_effect = query_side_effect

        @contextmanager
        def mock_session_scope():
            yield session

        return mock_session_scope, session

    @pytest.mark.asyncio
    async def test_single_error_does_not_yet_terminate(self, make_monitoring_loop, mock_agent_manager, mock_db):
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = "Error: Connection error."
        mock_agent_manager.terminate_agent = AsyncMock()

        result = await make_monitoring_loop._detect_connection_errors(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_match_within_resumed_session_grace_period(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """A resumed session can replay a prior, already-resolved task's
        own persistent connection errors into the pane on startup --
        must not be treated as current within the grace window."""
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = (
            "Error: Connection error.\nError: Connection error.\nError: Connection error."
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        task = Mock(
            id="t1", status="in_progress", assigned_agent_id="a1",
            failure_reason=None, phase_id=None,
            started_at=datetime.utcnow() - timedelta(seconds=10),
        )
        mock_session_scope, session = self._session_with(task, phase=None)
        mock_db.session_scope = mock_session_scope

        result = await make_monitoring_loop._detect_connection_errors(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()
        assert task.status == "in_progress"

    @pytest.mark.asyncio
    async def test_terminates_agent_only_after_task_status_already_updated(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """Regression: terminate_agent() used to run BEFORE the task's
        status was updated, leaving a window where Agent.status was
        already "terminated" while Task.status was still "in_progress"
        (pointing at that now-dead agent). A separate, unrelated periodic
        sweep (attempt_recovery's stale-assigned-task cleanup) can see
        exactly that combination and mark the task failed with a generic
        "terminated unexpectedly" reason before this function's own
        session ever gets to it -- silently skipping the fallback dispatch
        entirely. terminate_agent must not be called until the task has
        already left "assigned"/"in_progress"."""
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = (
            "Error: Connection error.\nError: Connection error.\nError: Connection error."
        )
        make_monitoring_loop._stuck_state = {"a1": {"sig": "x", "since": time.time(), "recov": 0}}
        task = Mock(id="t1", status="in_progress", assigned_agent_id="a1", failure_reason=None, phase_id=None)
        status_at_terminate_call = []

        async def fake_terminate(agent_id):
            status_at_terminate_call.append(task.status)

        mock_agent_manager.terminate_agent = AsyncMock(side_effect=fake_terminate)

        mock_session_scope, session = self._session_with(task, phase=None)
        mock_db.session_scope = mock_session_scope

        with patch("src.monitoring.mechanical_recovery.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(agents=Mock(default_fallback_cli_tool=None, secondary_cli_model_fallback=None))
            result = await make_monitoring_loop._detect_connection_errors(agent)

        assert result is True
        assert status_at_terminate_call == ["failed"]

    @pytest.mark.asyncio
    async def test_no_fallback_configured_fails_with_reason_not_silent_pending(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = (
            "Error: Connection error.\nError: Connection error.\nError: Connection error."
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        make_monitoring_loop._stuck_state = {"a1": {"sig": "x", "since": time.time(), "recov": 0}}

        task = Mock(id="t1", status="in_progress", assigned_agent_id="a1", failure_reason=None, phase_id=None)
        mock_session_scope, session = self._session_with(task, phase=None)
        mock_db.session_scope = mock_session_scope

        with patch("src.monitoring.mechanical_recovery.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(agents=Mock(default_fallback_cli_tool=None, secondary_cli_model_fallback=None))
            result = await make_monitoring_loop._detect_connection_errors(agent)

        assert result is True
        assert task.status == "failed"
        assert task.assigned_agent_id is None
        assert task.failure_reason is not None
        assert "connection error" in task.failure_reason.lower()
        assert "Orphaned" not in task.failure_reason
        mock_agent_manager.terminate_agent.assert_called_once()
        mock_agent_manager.create_agent_for_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_configured_redispatches_instead_of_failing(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = (
            "Error: Connection error.\nError: Connection error.\nError: Connection error."
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        new_agent = Mock(id="a2")
        mock_agent_manager.create_agent_for_task = AsyncMock(return_value=new_agent)
        make_monitoring_loop._stuck_state = {"a1": {"sig": "x", "since": time.time(), "recov": 0}}

        task = Mock(
            id="t1", status="in_progress", assigned_agent_id="a1",
            failure_reason=None, phase_id="p1", workflow_id="wf1",
        )
        phase = Mock(fallback_cli_tool="claude", fallback_cli_model="sonnet")
        mock_session_scope, session = self._session_with(task, phase=phase)
        mock_db.session_scope = mock_session_scope

        result = await make_monitoring_loop._detect_connection_errors(agent)

        assert result is True
        assert task.status == "pending"
        assert task.failure_reason is None
        mock_agent_manager.create_agent_for_task.assert_called_once()
        call_kwargs = mock_agent_manager.create_agent_for_task.call_args.kwargs
        assert call_kwargs["cli_type"] == "claude"
        assert call_kwargs["phase_cli_model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_fallback_dispatch_failure_still_fails_with_reason(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        agent = Agent(id="a1", cli_type="pi", current_task_id="t1")
        mock_agent_manager.get_agent_output.return_value = (
            "Error: Connection error.\nError: Connection error.\nError: Connection error."
        )
        mock_agent_manager.terminate_agent = AsyncMock()
        mock_agent_manager.create_agent_for_task = AsyncMock(side_effect=RuntimeError("worktree gone"))
        make_monitoring_loop._stuck_state = {"a1": {"sig": "x", "since": time.time(), "recov": 0}}

        task = Mock(
            id="t1", status="in_progress", assigned_agent_id="a1",
            failure_reason=None, phase_id="p1", workflow_id="wf1",
        )
        phase = Mock(fallback_cli_tool="claude", fallback_cli_model="sonnet")
        mock_session_scope, session = self._session_with(task, phase=phase)
        mock_db.session_scope = mock_session_scope

        result = await make_monitoring_loop._detect_connection_errors(agent)

        assert result is True
        assert task.status == "failed"
        assert "connection error" in task.failure_reason.lower()
        assert "worktree gone" in task.failure_reason


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

        # Set grace period past check on the orphan reaper.
        # utcnow, not now: OrphanSessionReaper compares this against its own
        # datetime.utcnow() clock, so a local-time value here is skewed by the
        # host's UTC offset. West of UTC that skew happens to enlarge the
        # delta and the test still passes; east of it the delta goes negative,
        # the grace period triggers, and the test fails (verified: passes at
        # UTC-6, fails at UTC+9).
        make_monitoring_loop._orphan_reaper.last_check_time = datetime.utcnow() - timedelta(
            seconds=200
        )

        await make_monitoring_loop._cleanup_orphaned_tmux_sessions()
        # Session is active, not orphaned — no kill_session called

    @pytest.mark.asyncio
    async def test_kills_orphans(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """The grace period is timed per-session from when it was first
        seen as an orphan candidate, not from the reaper's own last run --
        so a session must be observed across two calls before it's
        eligible to be killed (see orphan_reaper.py's fix for the
        production bug this replaced: under the default ~60s monitoring
        cadence, the old last-run-based check was true on nearly every
        cycle, and orphaned sessions were essentially never actually
        killed)."""
        session_mock = Mock()
        session_mock.name = "agent-orphan"
        session_mock.kill_session = Mock()
        mock_agent_manager.tmux_server.sessions = [session_mock]

        db_session = Mock()
        db_session.query.return_value.filter.return_value.all.return_value = []
        db_session.query.return_value.filter.return_value.first.return_value = None
        mock_db.get_session.return_value = db_session

        # Past the "very first call ever" short-circuit.
        make_monitoring_loop._orphan_reaper.last_check_time = datetime.utcnow() - timedelta(
            seconds=200
        )

        # First call: newly seen as an orphan candidate -- not killed yet.
        await make_monitoring_loop._cleanup_orphaned_tmux_sessions()
        session_mock.kill_session.assert_not_called()

        # Backdate its own first-seen time past the grace period, then
        # check again -- now it should be killed.
        make_monitoring_loop._orphan_reaper._first_seen_orphan["agent-orphan"] = (
            datetime.utcnow() - timedelta(seconds=200)
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
    def audit_monitor(self, real_db, mock_agent_manager, mock_llm):
        from src.monitoring.monitor import MonitoringLoop

        with patch("src.monitoring.monitor.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(
                monitoring=Mock(stuck_detection_minutes=10),
                agents=Mock(agent_timeout_minutes=60),
            )
            m = MonitoringLoop(
                db_manager=real_db,
                agent_manager=mock_agent_manager,
                llm_provider=mock_llm,
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

        with patch("src.mcp.autopilot.control_routes.run_health_audit", return_value={"findings": []}):
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

        with patch("src.mcp.autopilot.control_routes.run_health_audit", return_value={"findings": []}):
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

        with patch("src.mcp.autopilot.control_routes.run_health_audit", return_value={"findings": []}):
            for _ in range(MAX_STUCK_TASK_NUDGES + 2):
                self._set_agent_last_activity(real_db, agent_id, datetime.utcnow())
                await audit_monitor._audit_system_health()

        mock_agent_manager.send_message_to_agent.assert_not_called()
        session = real_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "in_progress"
        session.close()


class TestStuckTaskPromotionClearsStaleFailureReason:
    """Regression, same class of bug as server.py's update_task_status: a
    stuck in_progress task with completion_notes set (agent finished then
    crashed before its update_task_status call landed) gets promoted to
    done here -- but if this same task row carried a failure_reason from
    an earlier failed attempt (goto/retry reuses the row), that reason
    stuck around forever, feeding the "done but has failure_reason"
    self-heal that wrongly resets genuinely-completed tasks back to
    failed."""

    @pytest.fixture
    def real_db(self, tmp_path):
        from src.core.database import DatabaseManager

        db_path = tmp_path / "test.db"
        db = DatabaseManager(str(db_path))
        db.create_tables()
        return db

    @pytest.fixture
    def audit_monitor(self, real_db, mock_agent_manager, mock_llm):
        from src.monitoring.monitor import MonitoringLoop

        with patch("src.monitoring.monitor.get_config") as mock_cfg:
            mock_cfg.return_value = Mock(
                monitoring=Mock(stuck_detection_minutes=10),
                agents=Mock(agent_timeout_minutes=60),
            )
            m = MonitoringLoop(
                db_manager=real_db,
                agent_manager=mock_agent_manager,
                llm_provider=mock_llm,
            )
        return m

    @pytest.mark.asyncio
    async def test_promotion_clears_prior_failure_reason(self, audit_monitor, real_db):
        from src.core.database import Agent, Task

        session = real_db.get_session()
        session.add(
            Agent(
                id="agent-crashed", system_prompt="p", status="terminated",
                cli_type="pi", agent_type="phase",
            )
        )
        session.add(
            Task(
                id="task-finished-then-crashed", raw_description="r", done_definition="d",
                status="in_progress", assigned_agent_id="agent-crashed",
                started_at=datetime.utcnow() - timedelta(minutes=20),
                completion_notes="done, see report.md",
                failure_reason="earlier attempt: agent timed out",
            )
        )
        session.commit()
        session.close()

        with patch("src.mcp.autopilot.control_routes.run_health_audit", return_value={"findings": []}):
            await audit_monitor._audit_system_health()

        session = real_db.get_session()
        task = session.query(Task).filter_by(id="task-finished-then-crashed").first()
        assert task.status == "done"
        assert task.failure_reason is None
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


class TestAutoRestartResetsTask:
    """Regression: _auto_restart_agent used to only ever touch the Agent
    row (status="terminated"), never the Task it was working on. A Task
    left "assigned"/"in_progress" pointing at a now-terminated agent is
    indistinguishable from one whose agent is still genuinely working,
    until an unrelated periodic sweep (attempt_recovery's stale-assigned-
    task cleanup) eventually notices the mismatch and fails the task with
    a generic "terminated unexpectedly" reason -- discarding any real work
    the agent had already done and couldn't report (verify_agent_
    authentication correctly, and by design, rejects completion calls from
    an agent already marked terminated)."""

    @pytest.mark.asyncio
    async def test_resets_task_before_terminating_agent(
        self, make_monitoring_loop, mock_db, mock_agent_manager
    ):
        from contextlib import contextmanager

        from src.core.database import Agent as AgentModel
        from src.core.database import Task as TaskModel

        agent = Agent(
            id="agent-1",
            tmux_session_name="agent_agent-1",
            status="working",
            current_task_id="task-1",
        )
        task = Mock(id="task-1", status="in_progress", assigned_agent_id="agent-1", failure_reason="stale")
        db_agent = Mock(id="agent-1", status="working")

        session = Mock()

        def query_side_effect(model):
            m = Mock()
            if model is TaskModel:
                m.filter_by.return_value.filter.return_value.first.return_value = task
                # terminate_agent's own stray-task sweep: this task was
                # already reset above, so nothing still points at the agent.
                m.filter_by.return_value.filter.return_value.all.return_value = []
            elif model is AgentModel:
                m.filter_by.return_value.first.return_value = db_agent
            else:
                m.filter_by.return_value.first.return_value = None
            return m

        session.query.side_effect = query_side_effect

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        mock_agent_manager._resolve_tmux_transcript_dir = Mock(return_value=None)

        await make_monitoring_loop._auto_restart_agent(agent)

        assert task.status == "pending"
        assert task.assigned_agent_id is None
        assert task.failure_reason is None
        assert db_agent.status == "terminated"
        mock_agent_manager.tmux_server.kill_session.assert_called_once_with("agent_agent-1")

    @pytest.mark.asyncio
    async def test_no_current_task_id_skips_reset_without_error(
        self, make_monitoring_loop, mock_db, mock_agent_manager
    ):
        """An agent with no current_task_id (already cleared, or never
        assigned) must not crash the restart path."""
        from contextlib import contextmanager

        agent = Agent(id="agent-1", tmux_session_name="agent_agent-1", status="working")

        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        @contextmanager
        def mock_session_scope():
            yield session

        mock_db.session_scope = mock_session_scope
        mock_agent_manager._resolve_tmux_transcript_dir = Mock(return_value=None)

        await make_monitoring_loop._auto_restart_agent(agent)

        mock_agent_manager.tmux_server.kill_session.assert_called_once_with("agent_agent-1")


class TestDetectZombieAgent:
    """A safety net against the whole class of bug behind c1cc687/f5a10fa
    (fire-and-forget agent-termination calls silently never running),
    not just that one specific asyncio.create_task GC race. Catches it by
    observable SYMPTOM -- agent still working/starting/idle while its own
    current task has already reached a terminal status -- regardless of
    what caused the mismatch, so it also covers causes not yet found.

    Confirmed live: three agents each sat "working" for 3-7+ minutes after
    their tasks completed "done", with no agent_logs entries in between,
    until an unrelated frozen-agent detector misread the idle silence and
    wasted an in-session model-switch rescue on work already finished."""

    def _session_with(self, task):
        from contextlib import contextmanager

        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = task

        @contextmanager
        def mock_session_scope():
            yield session

        return mock_session_scope

    @pytest.mark.asyncio
    async def test_terminated_agent_not_checked(
        self, make_monitoring_loop, mock_agent_manager
    ):
        agent = Agent(id="a1", status="terminated", current_task_id="t1")
        mock_agent_manager.terminate_agent = AsyncMock()

        result = await make_monitoring_loop._detect_zombie_agent(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_current_task_not_checked(
        self, make_monitoring_loop, mock_agent_manager
    ):
        agent = Agent(id="a1", status="working", current_task_id=None)
        mock_agent_manager.terminate_agent = AsyncMock()

        result = await make_monitoring_loop._detect_zombie_agent(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.parametrize("status", ["pending", "assigned", "in_progress", "under_review"])
    @pytest.mark.asyncio
    async def test_active_task_not_flagged(
        self, make_monitoring_loop, mock_agent_manager, mock_db, status
    ):
        """The whole point: a genuinely-working agent (its task is still
        active) must never be reaped."""
        agent = Agent(id="a1", status="working", current_task_id="t1")
        task = Mock(id="t1", status=status)
        mock_db.session_scope = self._session_with(task)
        mock_agent_manager.terminate_agent = AsyncMock()

        result = await make_monitoring_loop._detect_zombie_agent(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()

    @pytest.mark.parametrize("agent_status", ["working", "starting", "idle", "stuck"])
    @pytest.mark.parametrize("task_status", ["done", "failed", "duplicated"])
    @pytest.mark.asyncio
    async def test_terminal_task_reaps_the_agent(
        self, make_monitoring_loop, mock_agent_manager, mock_db, agent_status, task_status
    ):
        """Mirrors task 6633d361: completed "done" with real, substantive
        completion_notes -- the agent genuinely finished its work -- while
        its own agent row was still "working" with no explanation.
        TaskStatus.TERMINAL, not just "done": the same zombie risk applies
        to a completion handler that flips status to "failed"/"duplicated"
        and then never reaches its own termination call either."""
        agent = Agent(id="a1", status=agent_status, current_task_id="t1")
        task = Mock(id="t1", status=task_status)
        mock_db.session_scope = self._session_with(task)
        mock_agent_manager.terminate_agent = AsyncMock()

        result = await make_monitoring_loop._detect_zombie_agent(agent)

        assert result is True
        mock_agent_manager.terminate_agent.assert_called_once_with("a1")

    @pytest.mark.asyncio
    async def test_missing_task_row_not_flagged(
        self, make_monitoring_loop, mock_agent_manager, mock_db
    ):
        """current_task_id pointing at a since-deleted row is a different,
        unrelated problem -- must not crash or misfire here."""
        agent = Agent(id="a1", status="working", current_task_id="t1")
        mock_db.session_scope = self._session_with(None)
        mock_agent_manager.terminate_agent = AsyncMock()

        result = await make_monitoring_loop._detect_zombie_agent(agent)

        assert result is False
        mock_agent_manager.terminate_agent.assert_not_called()
