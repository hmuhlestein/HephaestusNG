"""Tests for OrphanSessionReaper — the tmux session reconciliation
extracted from MonitoringLoop (SOLID review 3.4).
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest


class TestOrphanSessionReaper:
    """Tests for OrphanSessionReaper.cleanup_orphaned_tmux_sessions."""

    @pytest.fixture
    def reaper(self):
        from src.monitoring.orphan_reaper import OrphanSessionReaper

        db_manager = MagicMock()
        agent_manager = MagicMock()
        return OrphanSessionReaper(db_manager, agent_manager)

    @pytest.mark.asyncio
    async def test_no_agent_sessions_returns_early(self, reaper):
        """When no agent tmux sessions exist, should return early."""
        mock_tmux_sess = MagicMock()
        mock_tmux_sess.name = "other-session"
        reaper.agent_manager.tmux_server.sessions = [mock_tmux_sess]

        await reaper.cleanup_orphaned_tmux_sessions()

        # No DB query needed
        reaper.db_manager.get_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_grace_period_on_first_check(self, reaper):
        """First check with agent sessions should set last_check_time and return."""
        # Agent session exists in tmux
        mock_tmux_sess = MagicMock()
        mock_tmux_sess.name = "agent-test-123"
        reaper.agent_manager.tmux_server.sessions = [mock_tmux_sess]

        # Mock DB: agent exists with matching tmux session name
        mock_agent = MagicMock()
        mock_agent.tmux_session_name = "agent-test-123"
        mock_agent.status = "working"
        mock_agent.current_task_id = None

        mock_db_session = MagicMock()
        reaper.db_manager.get_session.return_value = mock_db_session

        # Set up mock chain for Agent query
        agent_query = MagicMock()
        agent_query.filter.return_value.all.return_value = [mock_agent]

        # Set up mock chain for Workflow query
        wf_query = MagicMock()
        wf_query.filter.return_value.all.return_value = []

        def query_side_effect(model):
            from src.core.database import Agent, Workflow
            if model == Agent:
                return agent_query
            elif model == Workflow:
                return wf_query
            return MagicMock()

        mock_db_session.query.side_effect = query_side_effect

        # First check - last_check_time is None
        assert reaper.last_check_time is None
        await reaper.cleanup_orphaned_tmux_sessions()

        # Should have set last_check_time (grace period)
        assert reaper.last_check_time is not None

    @pytest.mark.asyncio
    async def test_kills_orphaned_tmux_sessions_after_grace_period(self, reaper):
        """Tmux sessions with no corresponding DB agent should be killed
        after the grace period expires."""
        # Agent session exists in tmux but NOT in DB
        orphan_session = MagicMock()
        orphan_session.name = "agent-orphan-999"
        orphan_session.kill_session = MagicMock()
        reaper.agent_manager.tmux_server.sessions = [orphan_session]

        # Set last_check_time to bypass grace period
        reaper.last_check_time = datetime.utcnow() - timedelta(seconds=200)

        # Mock DB session with no active agents
        mock_db_session = MagicMock()
        reaper.db_manager.get_session.return_value = mock_db_session

        # Empty agent and workflow results
        agent_query = MagicMock()
        agent_query.filter.return_value.all.return_value = []

        wf_query = MagicMock()
        wf_query.filter.return_value.all.return_value = []

        def query_side_effect(model):
            from src.core.database import Agent, Workflow
            if model == Agent:
                return agent_query
            elif model == Workflow:
                return wf_query
            return MagicMock()

        mock_db_session.query.side_effect = query_side_effect

        await reaper.cleanup_orphaned_tmux_sessions()

        # Orphan session should be killed
        orphan_session.kill_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_kill_active_sessions(self, reaper):
        """Sessions that have a corresponding active DB agent should NOT be killed."""
        # Agent session exists in tmux AND in DB
        active_session = MagicMock()
        active_session.name = "agent-active-123"
        reaper.agent_manager.tmux_server.sessions = [active_session]

        # Set last_check_time to bypass grace period
        reaper.last_check_time = datetime.utcnow() - timedelta(seconds=200)

        # Mock DB: agent exists with matching session name
        mock_agent = MagicMock()
        mock_agent.tmux_session_name = "agent-active-123"
        mock_agent.status = "working"
        mock_agent.current_task_id = None

        mock_db_session = MagicMock()
        reaper.db_manager.get_session.return_value = mock_db_session

        agent_query = MagicMock()
        agent_query.filter.return_value.all.return_value = [mock_agent]

        wf_query = MagicMock()
        wf_query.filter.return_value.all.return_value = []

        def query_side_effect(model):
            from src.core.database import Agent, Workflow
            if model == Agent:
                return agent_query
            elif model == Workflow:
                return wf_query
            return MagicMock()

        mock_db_session.query.side_effect = query_side_effect

        await reaper.cleanup_orphaned_tmux_sessions()

        # Active session should NOT be killed
        active_session.kill_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminating_agent_for_inactive_workflow_resets_its_task(self, reaper):
        """Regression: this path terminated the agent but never reset the
        Task it was working on -- a Task left "assigned"/"in_progress"
        pointing at a now-terminated agent is indistinguishable from one
        whose agent is still genuinely working, until an unrelated
        periodic sweep (attempt_recovery's stale-assigned-task cleanup)
        eventually notices the mismatch and fails it with a generic
        "terminated unexpectedly" reason instead of resetting it for a
        clean retry."""
        mock_agent = MagicMock()
        mock_agent.id = "agent-orphan-wf"
        mock_agent.tmux_session_name = "agent-orphan-wf"
        mock_agent.status = "working"
        mock_agent.current_task_id = "task-1"
        mock_agent.last_activity = None  # no recent activity -- no grace window

        mock_task = MagicMock()
        mock_task.workflow_id = "wf-gone"
        mock_task.status = "in_progress"

        # Must be non-empty or cleanup_orphaned_tmux_sessions returns
        # before ever reaching the active_agents loop this test exercises.
        unrelated_session = MagicMock()
        unrelated_session.name = "agent-unrelated"
        reaper.agent_manager.tmux_server.sessions = [unrelated_session]

        mock_db_session = MagicMock()
        reaper.db_manager.get_session.return_value = mock_db_session

        agent_query = MagicMock()
        agent_query.filter.return_value.all.return_value = [mock_agent]
        agent_query.filter_by.return_value.first.return_value = mock_agent

        wf_query = MagicMock()
        wf_query.filter.return_value.all.return_value = []  # no active workflows

        task_query = MagicMock()
        # terminate_agent queries stray tasks with filter_by(assigned_agent_id=...)
        stray_query = MagicMock()
        stray_query.filter.return_value.all.return_value = [mock_task]
        stray_query.first.return_value = mock_task
        task_query.filter_by.return_value = stray_query

        def query_side_effect(model):
            from src.core.database import Agent, Task, Workflow
            if model == Agent:
                return agent_query
            elif model == Workflow:
                return wf_query
            elif model == Task:
                return task_query
            return MagicMock()

        mock_db_session.query.side_effect = query_side_effect

        await reaper.cleanup_orphaned_tmux_sessions()

        assert mock_agent.status == "terminated"
        assert mock_task.status == "pending", "task must be reset, not left dangling"
        assert mock_task.assigned_agent_id is None

    @pytest.mark.asyncio
    async def test_grace_period_protects_new_sessions(self, reaper):
        """Sessions created since last check should not be killed."""
        # Agent session in tmux but not in DB
        new_session = MagicMock()
        new_session.name = "agent-new-123"
        reaper.agent_manager.tmux_server.sessions = [new_session]

        # Set last_check_time very recently (within grace period)
        reaper.last_check_time = datetime.utcnow() - timedelta(seconds=10)

        # Mock DB: no active agents
        mock_db_session = MagicMock()
        reaper.db_manager.get_session.return_value = mock_db_session

        agent_query = MagicMock()
        agent_query.filter.return_value.all.return_value = []

        wf_query = MagicMock()
        wf_query.filter.return_value.all.return_value = []

        def query_side_effect(model):
            from src.core.database import Agent, Workflow
            if model == Agent:
                return agent_query
            elif model == Workflow:
                return wf_query
            return MagicMock()

        mock_db_session.query.side_effect = query_side_effect

        await reaper.cleanup_orphaned_tmux_sessions()

        # New session should NOT be killed (within grace period)
        new_session.kill_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_grace_period_uses_utc_not_local_time(self, reaper, monkeypatch):
        """The grace-period clock must be UTC throughout, not the host's
        local time -- CLAUDE.md's utc-only invariant, and the exact bug
        class this reaper's own inline comment documents (west of UTC, a
        local-time cutoff compared against a UTC timestamp silently never
        matches). Simulate a host where local time is wildly different from
        UTC (not just offset by a few hours) and confirm the grace-period
        decision still follows datetime.utcnow(), not datetime.now()."""
        from src.monitoring import orphan_reaper as orphan_reaper_module

        fixed_utc_now = datetime(2026, 1, 1, 12, 0, 10)

        class _FakeDatetime:
            @staticmethod
            def utcnow():
                return fixed_utc_now

            @staticmethod
            def now():
                # A "local" clock wildly different from UTC. If the source
                # used this instead of utcnow(), time_since_last_check would
                # be computed against the wrong epoch entirely.
                return datetime(2000, 1, 1, 0, 0, 0)

        monkeypatch.setattr(orphan_reaper_module, "datetime", _FakeDatetime)

        # 10 seconds before fixed_utc_now -- well within GRACE_PERIOD_SECONDS
        # (120s) if and only if the reaper compares against utcnow().
        reaper.last_check_time = fixed_utc_now - timedelta(seconds=10)

        new_session = MagicMock()
        new_session.name = "agent-new-utc-check"
        reaper.agent_manager.tmux_server.sessions = [new_session]

        mock_db_session = MagicMock()
        reaper.db_manager.get_session.return_value = mock_db_session

        agent_query = MagicMock()
        agent_query.filter.return_value.all.return_value = []
        wf_query = MagicMock()
        wf_query.filter.return_value.all.return_value = []

        def query_side_effect(model):
            from src.core.database import Agent, Workflow
            if model == Agent:
                return agent_query
            elif model == Workflow:
                return wf_query
            return MagicMock()

        mock_db_session.query.side_effect = query_side_effect

        await reaper.cleanup_orphaned_tmux_sessions()

        # Still within the (UTC-computed) grace period -- must not be killed.
        # If the source regressed to datetime.now(), time_since_last_check
        # would be ~26 years, blowing past the grace period, and this
        # assertion would fail.
        new_session.kill_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminates_agent_with_inactive_workflow(self, reaper):
        """Agents whose workflow is no longer active should be terminated."""
        # Agent session in tmux and in DB
        mock_tmux_sess = MagicMock()
        mock_tmux_sess.name = "agent-test-123"
        reaper.agent_manager.tmux_server.sessions = [mock_tmux_sess]

        # Set last_check_time to bypass grace period
        reaper.last_check_time = datetime.utcnow() - timedelta(seconds=200)

        # Mock agent with task in completed workflow
        mock_agent = MagicMock()
        mock_agent.id = "agent-test-123"
        mock_agent.tmux_session_name = "agent-test-123"
        mock_agent.status = "working"
        mock_agent.current_task_id = "task-456"
        # Outside the 30s "recently active" grace window, or the
        # termination path below is skipped.
        # utcnow: OrphanSessionReaper compares last_activity against its own
        # datetime.utcnow() clock. A local-time value is skewed by the host's
        # UTC offset -- east of UTC the delta goes negative, the 30s grace
        # window matches, and the agent is never reaped (verified: passes at
        # UTC-6, fails at UTC+9).
        mock_agent.last_activity = datetime.utcnow() - timedelta(seconds=100)

        mock_task = MagicMock()
        mock_task.workflow_id = "wf-old"

        mock_workflow = MagicMock()
        mock_workflow.id = "wf-active"

        mock_db_session = MagicMock()
        reaper.db_manager.get_session.return_value = mock_db_session

        # Agent query returns our agent
        agent_query = MagicMock()
        agent_query.filter.return_value.all.return_value = [mock_agent]
        agent_query.filter_by.return_value.first.return_value = mock_agent

        # Workflow query returns only wf-active (not wf-old)
        wf_query = MagicMock()
        wf_query.filter.return_value.all.return_value = [mock_workflow]

        # Task query returns task with workflow_id = wf-old
        task_query = MagicMock()
        # terminate_agent queries stray tasks with filter_by(assigned_agent_id=...)
        stray_query = MagicMock()
        stray_query.filter.return_value.all.return_value = [mock_task]
        stray_query.first.return_value = mock_task
        task_query.filter_by.return_value = stray_query

        def query_side_effect(model):
            from src.core.database import Agent, Task, Workflow
            if model == Agent:
                return agent_query
            elif model == Workflow:
                return wf_query
            elif model == Task:
                return task_query
            return MagicMock()

        mock_db_session.query.side_effect = query_side_effect

        # Agent's tmux session matches active agent session name, so it won't be killed as orphan
        # But agent should be terminated due to inactive workflow
        await reaper.cleanup_orphaned_tmux_sessions()

        # Agent should be terminated
        assert mock_agent.status == "terminated"


class TestActiveAgentStatusFilter:
    """Regression: the active-agent query filtered on
    Agent.status.in_(["working", "pending", "assigned"]) -- but
    "pending"/"assigned" are Task.status values, not Agent.status ones
    (Agent.status's CheckConstraint only allows idle/working/stuck/
    terminated), so those two never matched anything. In practice this
    made the filter equivalent to status == "working" only, silently
    excluding "idle"/"stuck" agents from orphaned-workflow cleanup. Uses a
    real sqlite DB (not the MagicMock chain the rest of this file uses)
    because a mocked .filter() can't distinguish the old broken predicate
    from the fixed one -- it returns whatever .all.return_value is set to
    regardless of what was actually passed to filter()."""

    @pytest.fixture
    def reaper(self):
        from src.core.database import DatabaseManager
        from src.monitoring.orphan_reaper import OrphanSessionReaper

        db_manager = DatabaseManager(":memory:")
        db_manager.create_tables()
        agent_manager = MagicMock()
        return OrphanSessionReaper(db_manager, agent_manager), db_manager

    @pytest.mark.asyncio
    async def test_idle_agent_with_inactive_workflow_is_terminated(self, reaper):
        reaper_obj, db_manager = reaper
        from src.core.database import Agent, Task, Workflow

        session = db_manager.get_session()
        session.add(
            Workflow(id="wf-active", name="A", phases_folder_path="/tmp", status="active")
        )
        session.add(
            Workflow(id="wf-old", name="B", phases_folder_path="/tmp", status="completed")
        )
        session.add(
            Task(
                id="task-1",
                raw_description="r",
                done_definition="d",
                status="in_progress",
                workflow_id="wf-old",
            )
        )
        session.commit()  # Task must exist before Agent.current_task_id's FK references it
        session.add(
            Agent(
                id="agent-test-123",
                system_prompt="p",
                status="idle",
                cli_type="pi",
                tmux_session_name="agent-test-123",
                current_task_id="task-1",
                # Outside the 30s "recently active" grace window, or the
                # termination path is skipped.
                # utcnow, not now -- see the note above; production compares
                # this against datetime.utcnow().
                last_activity=datetime.utcnow() - timedelta(seconds=100),
            )
        )
        session.commit()
        session.close()

        mock_tmux_sess = MagicMock()
        mock_tmux_sess.name = "agent-test-123"
        reaper_obj.agent_manager.tmux_server.sessions = [mock_tmux_sess]
        reaper_obj.last_check_time = datetime.utcnow() - timedelta(seconds=200)

        await reaper_obj.cleanup_orphaned_tmux_sessions()

        session = db_manager.get_session()
        agent = session.query(Agent).filter_by(id="agent-test-123").first()
        assert agent.status == "terminated"
        session.close()


class TestOrphanReapFlushesCleanTranscript:
    """An orphaned session has no active Agent row by definition, so
    reaping it bypasses terminate_agent's own clean-shutdown flush of the
    stability-tracked "clean" transcript entirely. Without its own final
    flush here, this abrupt-kill path would lose everything not yet
    confirmed stable (see AgentManager._flush_stable_transcript)."""

    @pytest.fixture
    def reaper(self):
        from src.monitoring.orphan_reaper import OrphanSessionReaper

        db_manager = MagicMock()
        agent_manager = MagicMock()
        return OrphanSessionReaper(db_manager, agent_manager)

    @pytest.mark.asyncio
    async def test_flushes_before_killing_orphaned_session(self, reaper):
        from pathlib import Path

        orphan_session = MagicMock()
        orphan_session.name = "agent-orphan-999"
        call_order = []
        orphan_session.kill_session = MagicMock(
            side_effect=lambda: call_order.append("kill")
        )
        reaper.agent_manager.tmux_server.sessions = [orphan_session]
        reaper.last_check_time = datetime.utcnow() - timedelta(seconds=200)

        mock_db_session = MagicMock()
        reaper.db_manager.get_session.return_value = mock_db_session

        agent_query = MagicMock()
        agent_query.filter.return_value.all.return_value = []
        last_agent = MagicMock()
        agent_query.filter_by.return_value.first.return_value = last_agent

        wf_query = MagicMock()
        wf_query.filter.return_value.all.return_value = []

        def query_side_effect(model):
            from src.core.database import Agent, Workflow
            if model == Agent:
                return agent_query
            elif model == Workflow:
                return wf_query
            return MagicMock()

        mock_db_session.query.side_effect = query_side_effect

        fake_dir = Path("/tmp/fake-transcript-dir")
        reaper.agent_manager._resolve_tmux_transcript_dir = MagicMock(
            return_value=fake_dir,
            side_effect=lambda *a, **k: (call_order.append("flush"), fake_dir)[1],
        )
        reaper.agent_manager._flush_stable_transcript = MagicMock()

        await reaper.cleanup_orphaned_tmux_sessions()

        reaper.agent_manager._resolve_tmux_transcript_dir.assert_called_once_with(last_agent)
        reaper.agent_manager._flush_stable_transcript.assert_called_once_with(
            "agent-orphan-999", fake_dir / "agent-orphan-999.clean.log"
        )
        orphan_session.kill_session.assert_called_once()
        assert call_order == ["flush", "kill"], (
            "the clean transcript must be flushed before the session is "
            "killed -- capture-pane can't see anything once it's gone"
        )
