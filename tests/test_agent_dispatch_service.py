"""Tests for AgentDispatchService — the agent dispatch context
builder extracted from server.py (SOLID review finding 1.2/1.3).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGetPhaseCliConfig:
    """Tests for AgentDispatchService.get_phase_cli_config."""

    def test_returns_defaults_for_none_phase_id(self):
        from src.services.agent_dispatch_service import AgentDispatchService

        session = MagicMock()
        result = AgentDispatchService.get_phase_cli_config(session, None)

        assert result["cli_tool"] is None
        assert result["cli_model"] is None
        assert result["glm_token_env"] is None
        assert result["thinking_level"] is None
        assert result["working_directory"] is None

    def test_returns_phase_config_when_phase_exists(self):
        from src.services.agent_dispatch_service import AgentDispatchService

        session = MagicMock()
        mock_phase = MagicMock()
        mock_phase.cli_tool = "claude"
        mock_phase.cli_model = "sonnet"
        mock_phase.glm_api_token_env = "GLM_API_TOKEN"
        mock_phase.thinking_level = "medium"
        mock_phase.working_directory = "/tmp/test-project"

        session.query.return_value.filter_by.return_value.first.return_value = mock_phase

        result = AgentDispatchService.get_phase_cli_config(session, "phase-123")

        assert result["cli_tool"] == "claude"
        assert result["cli_model"] == "sonnet"
        assert result["glm_token_env"] == "GLM_API_TOKEN"
        assert result["thinking_level"] == "medium"
        assert result["working_directory"] == "/tmp/test-project"

    def test_returns_defaults_when_phase_not_found(self):
        from src.services.agent_dispatch_service import AgentDispatchService

        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        result = AgentDispatchService.get_phase_cli_config(session, "nonexistent-phase")

        assert result["cli_tool"] is None
        assert result["cli_model"] is None


class TestMarkAssigned:
    """Tests for AgentDispatchService.mark_assigned."""

    @patch("src.core.app_context.get_app_state")
    def test_updates_task_status(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state

        mock_task = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
        mock_state.db_manager.get_session.return_value = mock_session

        AgentDispatchService.mark_assigned("task-123", "agent-456")

        assert mock_task.assigned_agent_id == "agent-456"
        assert mock_task.status == "assigned"
        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

    @patch("src.core.app_context.get_app_state")
    def test_rolls_back_on_commit_failure(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state

        mock_task = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
        mock_session.commit.side_effect = Exception("DB locked")
        mock_state.db_manager.get_session.return_value = mock_session

        with pytest.raises(Exception, match="DB locked"):
            AgentDispatchService.mark_assigned("task-123", "agent-456")

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    @patch("src.core.app_context.get_app_state")
    def test_accepts_external_session(self, mock_get_state):
        """FIX #16: When session is provided, should not open a new one."""
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state

        mock_task = MagicMock()
        external_session = MagicMock()
        external_session.query.return_value.filter_by.return_value.first.return_value = mock_task

        AgentDispatchService.mark_assigned("task-123", "agent-456", session=external_session)

        assert mock_task.assigned_agent_id == "agent-456"
        external_session.commit.assert_called_once()
        # Should NOT have opened a new session
        mock_state.db_manager.get_session.assert_not_called()
        # Should NOT have closed the external session
        external_session.close.assert_not_called()


class TestBuildDispatchContextWorkflowResolution:
    """REQ-17..REQ-21: build_dispatch_context must resolve workflow_id to a
    plain project_id string via resolve_project_for_workflow, not pass its
    (project_id, project_name) tuple straight through (adversarial review
    BLOCKER #1)."""

    @patch("src.core.app_context.get_app_state")
    @patch("src.core.database.resolve_project_for_workflow")
    async def test_unpacks_project_id_from_workflow_resolution_tuple(
        self, mock_resolve, mock_get_state
    ):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_resolve.return_value = ("proj-1", "My Project")

        mock_state = MagicMock()
        mock_state.phase_manager = None
        mock_state.agent_manager.get_project_context = AsyncMock(
            return_value="project context"
        )
        mock_state.rag_system.retrieve_for_task = AsyncMock(return_value=[])
        session_cm = mock_state.db_manager.session_scope.return_value
        session_cm.__enter__.return_value = MagicMock()
        session_cm.__exit__.return_value = False
        mock_get_state.return_value = mock_state

        await AgentDispatchService.build_dispatch_context(
            task_description_for_rag="do the thing",
            phase_id=None,
            workflow_id="wf-1",
            repo_id="repo-1",
        )

        mock_state.agent_manager.get_project_context.assert_awaited_once_with(
            project_id="proj-1", repo_id="repo-1"
        )


class TestResolveTaskProjectContext:
    """REQ-17..21: resolve_task_project_context is the shared helper that
    replaced 5 independent copies of "resolve project_id from
    task.workflow_id, call get_project_context(project_id, task.repo_id)"
    -- each previously hardcoded project_context="" instead
    (create_agent_for_task_direct, /api/create_agent_for_task,
    _spawn_agent_for_task, and 3 mechanical_recovery.py fallback paths)."""

    @patch("src.core.app_context.get_app_state")
    async def test_reuses_caller_session_when_given(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_state.agent_manager.get_project_context = AsyncMock(
            return_value="## PROJECT REPOS\n..."
        )
        mock_get_state.return_value = mock_state

        task = MagicMock(workflow_id="wf-1", repo_id="repo-1")
        fake_session = MagicMock()

        with patch(
            "src.core.database.get_project_info_for_workflow",
            return_value=("proj-1", "My Project"),
        ) as mock_lookup, patch(
            "src.core.database.resolve_project_for_workflow"
        ) as mock_no_session_lookup:
            result = await AgentDispatchService.resolve_task_project_context(
                task, session=fake_session
            )

        mock_lookup.assert_called_once_with(fake_session, "wf-1")
        mock_no_session_lookup.assert_not_called()
        mock_state.agent_manager.get_project_context.assert_awaited_once_with(
            project_id="proj-1", repo_id="repo-1"
        )
        assert result == "## PROJECT REPOS\n..."

    @patch("src.core.app_context.get_app_state")
    async def test_opens_its_own_lookup_when_no_session_given(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_state.agent_manager.get_project_context = AsyncMock(return_value="ctx")
        mock_get_state.return_value = mock_state

        task = MagicMock(workflow_id="wf-2", repo_id=None)

        with patch(
            "src.core.database.resolve_project_for_workflow",
            return_value=("proj-2", "Other Project"),
        ) as mock_no_session_lookup:
            await AgentDispatchService.resolve_task_project_context(task)

        mock_no_session_lookup.assert_called_once_with("wf-2")
        mock_state.agent_manager.get_project_context.assert_awaited_once_with(
            project_id="proj-2", repo_id=None
        )
