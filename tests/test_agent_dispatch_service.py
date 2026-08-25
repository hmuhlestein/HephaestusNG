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


class TestResolveTaskProjectContext:
    """Tests for AgentDispatchService.resolve_task_project_context (REQ-09)."""

    @patch("src.core.app_context.get_app_state")
    async def test_passes_workflow_and_repo_id_from_task(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state
        mock_state.agent_manager.get_project_context = AsyncMock(return_value="repo-aware context")

        mock_task = MagicMock(workflow_id="wf-1", repo_id="repo-be")

        result = await AgentDispatchService.resolve_task_project_context(mock_task)

        assert result == "repo-aware context"
        mock_state.agent_manager.get_project_context.assert_awaited_once_with(workflow_id="wf-1", repo_id="repo-be")

    @patch("src.core.app_context.get_app_state")
    async def test_degrades_to_no_args_call_on_failure(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state
        mock_state.agent_manager.get_project_context = AsyncMock(side_effect=[Exception("db error"), "fallback context"])

        mock_task = MagicMock(workflow_id="wf-1", repo_id="repo-be")

        result = await AgentDispatchService.resolve_task_project_context(mock_task)

        assert result == "fallback context"
        assert mock_state.agent_manager.get_project_context.await_count == 2


class TestBuildDispatchContextRepoAwareness:
    """REQ-09/17/18: build_dispatch_context must route project_context
    through resolve_task_project_context when a task is given, so
    multi-repo context (sibling repos, writable-vs-read-only) reaches the
    agent prompt at the real dispatch site -- it previously called
    get_project_context() bare, so the repo-aware section never appeared
    in production regardless of resolve_task_project_context existing."""

    @patch("src.core.app_context.get_app_state")
    async def test_with_task_uses_resolve_task_project_context(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state
        mock_state.rag_system.retrieve_for_task = AsyncMock(return_value=[])
        mock_state.phase_manager = None
        mock_state.db_manager.session_scope.return_value.__enter__.return_value = MagicMock()

        mock_task = MagicMock(workflow_id="wf-1", repo_id="repo-be")

        with patch.object(
            AgentDispatchService,
            "resolve_task_project_context",
            new=AsyncMock(return_value="## PROJECT REPOS\n- backend (WRITABLE): /code/backend"),
        ) as mock_resolve:
            result = await AgentDispatchService.build_dispatch_context(
                task_description_for_rag="do the thing",
                phase_id=None,
                task=mock_task,
            )

        mock_resolve.assert_awaited_once_with(mock_task)
        assert result["project_context"] == "## PROJECT REPOS\n- backend (WRITABLE): /code/backend"
        mock_state.agent_manager.get_project_context.assert_not_called()

    @patch("src.core.app_context.get_app_state")
    async def test_without_task_falls_back_to_bare_call(self, mock_get_state):
        """Unchanged behavior (no repo section) when no task is given."""
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state
        mock_state.agent_manager.get_project_context = AsyncMock(return_value="plain context")
        mock_state.rag_system.retrieve_for_task = AsyncMock(return_value=[])
        mock_state.phase_manager = None
        mock_state.db_manager.session_scope.return_value.__enter__.return_value = MagicMock()

        result = await AgentDispatchService.build_dispatch_context(
            task_description_for_rag="do the thing",
            phase_id=None,
        )

        mock_state.agent_manager.get_project_context.assert_awaited_once_with()
        assert result["project_context"] == "plain context"
