"""Tests for OrphanSessionReaper — the tmux session reconciliation
extracted from MonitoringLoop (SOLID review 3.4).
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, PropertyMock

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
        reaper.last_check_time = datetime.now() - timedelta(seconds=200)

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
        reaper.last_check_time = datetime.now() - timedelta(seconds=200)

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
    async def test_grace_period_protects_new_sessions(self, reaper):
        """Sessions created since last check should not be killed."""
        # Agent session in tmux but not in DB
        new_session = MagicMock()
        new_session.name = "agent-new-123"
        reaper.agent_manager.tmux_server.sessions = [new_session]

        # Set last_check_time very recently (within grace period)
        reaper.last_check_time = datetime.now() - timedelta(seconds=10)

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
    async def test_terminates_agent_with_inactive_workflow(self, reaper):
        """Agents whose workflow is no longer active should be terminated."""
        # Agent session in tmux and in DB
        mock_tmux_sess = MagicMock()
        mock_tmux_sess.name = "agent-test-123"
        reaper.agent_manager.tmux_server.sessions = [mock_tmux_sess]

        # Set last_check_time to bypass grace period
        reaper.last_check_time = datetime.now() - timedelta(seconds=200)

        # Mock agent with task in completed workflow
        mock_agent = MagicMock()
        mock_agent.id = "agent-test-123"
        mock_agent.tmux_session_name = "agent-test-123"
        mock_agent.status = "working"
        mock_agent.current_task_id = "task-456"

        mock_task = MagicMock()
        mock_task.workflow_id = "wf-old"

        mock_workflow = MagicMock()
        mock_workflow.id = "wf-active"

        mock_db_session = MagicMock()
        reaper.db_manager.get_session.return_value = mock_db_session

        # Agent query returns our agent
        agent_query = MagicMock()
        agent_query.filter.return_value.all.return_value = [mock_agent]

        # Workflow query returns only wf-active (not wf-old)
        wf_query = MagicMock()
        wf_query.filter.return_value.all.return_value = [mock_workflow]

        # Task query returns task with workflow_id = wf-old
        task_query = MagicMock()
        task_query.filter_by.return_value.first.return_value = mock_task

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
