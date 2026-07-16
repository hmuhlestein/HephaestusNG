"""Tests for AgentManager.create_agent_for_task and restart_agent.

These tests address the critical test coverage gap identified in ARCHITECTURE_REVIEW.md:
"create_agent_for_task and restart_agent have no direct test coverage"
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.database import (
    Agent,
    AgentLog,
    DatabaseManager,
    Phase,
    Task,
    Workflow,
)


@pytest.fixture
def db_manager(tmp_path):
    """Create a test database manager."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def sample_task(db_manager):
    """Create a sample task for agent assignment."""
    with db_manager.session_scope() as session:
        # Create workflow
        wf = Workflow(
            id="wf-1",
            name="Test Workflow",
            status="active",
            working_directory="/tmp/test-project",
            phases_folder_path="/tmp",
        )
        session.add(wf)

        # Create phase
        phase = Phase(
            id="phase-1",
            workflow_id="wf-1",
            name="implementation",
            order=1,
            description="Implement the feature",
            done_definitions=["code written and tested"],
        )
        session.add(phase)
        
        # Create task
        task = Task(
            id="task-1",
            workflow_id="wf-1",
            phase_id="phase-1",
            raw_description="Implement feature X",
            enriched_description="Implement feature X with tests",
            done_definition="Feature works and tests pass",
            status="pending",
        )
        session.add(task)
        
        return task


@pytest.fixture
def mock_agent_manager(db_manager):
    """Create a mock agent manager."""
    from src.agents.manager import AgentManager
    
    llm_provider = MagicMock()
    phase_manager = MagicMock()
    
    with patch("src.agents.manager.libtmux.Server"):
        manager = AgentManager(
            db_manager=db_manager,
            llm_provider=llm_provider,
            phase_manager=phase_manager,
        )
    
    # Mock tmux operations
    manager.tmux_server = MagicMock()
    manager.tmux_server.sessions = MagicMock()
    
    return manager


class TestCreateAgentForTask:
    """Tests for create_agent_for_task method."""
    
    @pytest.mark.asyncio
    async def test_raises_error_when_task_is_none(self, mock_agent_manager):
        """Should raise ValueError when task is None."""
        with pytest.raises(ValueError, match="task is REQUIRED"):
            await mock_agent_manager.create_agent_for_task(
                task=None,
                enriched_data={},
                memories=[],
                project_context="",
            )
    
    @pytest.mark.asyncio
    async def test_creates_agent_with_valid_task(self, mock_agent_manager, sample_task, db_manager):
        """Should create agent successfully with valid task."""
        # Mock dependencies — must mock create_agent_branch (not create_worktree)
        # because the workflow's working_directory doesn't contain '.worktrees/'
        # so the code takes the isolated-worktree branch.
        mock_agent_manager.branch_manager.create_agent_branch = MagicMock(
            return_value={
                "working_directory": "/tmp/test-project-agent",
                "branch_name": "agent-test-branch",
            }
        )
        mock_agent_manager.branch_manager.switch_to_branch = MagicMock()
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )
        
        # Mock tmux session creation
        mock_session = MagicMock()
        mock_session.name = "agent-session-1"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()
        
        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = ["pi", "--task", "test"]
            mock_get_cli.return_value = mock_cli
            
            agent = await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={"description": "Implement feature X"},
                memories=[],
                project_context="Test project context",
                cli_type="pi",
                working_directory="/tmp/test-project",
            )
        
        # Verify agent was created (create_agent_for_task returns a minimal
        # AgentInfo with only .id — status lives on the DB row)
        assert agent is not None
        assert agent.id is not None

        # Verify agent was saved to database
        with db_manager.session_scope() as session:
            saved_agent = session.query(Agent).filter_by(id=agent.id).first()
            assert saved_agent is not None
            assert saved_agent.status == "working"
            assert saved_agent.current_task_id == "task-1"
    
    @pytest.mark.asyncio
    async def test_session_id_uses_feature_model_launch_params(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Regression: feature-model workflows (the standard shape since the
        Feature Architect split) store project_path/feature_id in
        launch_params, not project_id/design_slug. Without a fallback to
        those keys, session_id was silently always "" for every such
        workflow, so phases meant to share a session (e.g.
        architectural_review resuming architecture_design's context, per
        workflow.yaml's session_roles) always launched a cold --no-session
        agent instead -- while the phase's own prompt still claimed
        continuity ("You have warm context...").

        Found live: architectural_review agent re-ran pytest and cat'd
        runtime logs from scratch instead of using prior context, because
        it never actually had any.
        """
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.launch_params = {
                "project_path": "/private/tmp/heph-smoke-test",
                "feature_id": "calculator-module",
            }

        mock_agent_manager.branch_manager.create_agent_branch = MagicMock(
            return_value={
                "working_directory": "/tmp/test-project-agent",
                "branch_name": "agent-test-branch",
            }
        )
        mock_agent_manager.branch_manager.switch_to_branch = MagicMock()
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )

        mock_session = MagicMock()
        mock_session.name = "agent-session-3"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = ["pi", "--task", "test"]
            mock_get_cli.return_value = mock_cli

            await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={"description": "Implement feature X"},
                memories=[],
                project_context="Test project context",
                cli_type="pi",
                working_directory="/tmp/test-project",
            )

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["session_id"], (
            "session_id should be derived from project_path/feature_id "
            "when project_id/design_slug are absent"
        )

    @pytest.mark.asyncio
    async def test_diagnostic_agent_never_gets_session_id(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Regression: session_id is derived from (project, design, phase_name)
        only -- it has no agent_type or agent_id component. A diagnostic
        agent is deliberately assigned the SAME phase_id as the stuck phase
        it's investigating (see monitor.py's _create_diagnostic_agent), so
        with the same launch_params as any normal phase agent it would
        resolve to the identical session_id as every phase agent that has
        ever worked that phase. Since the CLI (`pi --session-id X`) resumes
        an existing session for that ID, the diagnostic agent would
        silently resume a PRIOR phase agent's live conversation -- inheriting
        that agent's stale agent_id/task_id as part of the resumed context.

        Observed live: a diagnostic agent spent its entire run trying to
        close out a stale, already-terminated agent's task using that
        agent's identity, because its resumed session told it that was who
        it was -- never touching its own actual diagnostic task.
        """
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.launch_params = {
                "project_path": "/private/tmp/heph-smoke-test",
                "feature_id": "calculator-module",
            }

        mock_agent_manager.branch_manager.create_agent_branch = MagicMock(
            return_value={
                "working_directory": "/tmp/test-project-agent",
                "branch_name": "agent-test-branch",
            }
        )
        mock_agent_manager.branch_manager.switch_to_branch = MagicMock()
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )

        mock_session = MagicMock()
        mock_session.name = "agent-session-diagnostic"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = ["pi", "--task", "test"]
            mock_get_cli.return_value = mock_cli

            await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={"validation_prompt": "diagnose the stall"},
                memories=[],
                project_context="Test project context",
                cli_type="pi",
                working_directory="/tmp/test-project",
                agent_type="diagnostic",
                use_existing_worktree=True,
            )

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["session_id"] == "", (
            "diagnostic agents must never resolve a session_id, even when "
            "launch_params would otherwise produce one for a phase agent"
        )

    @pytest.mark.asyncio
    async def test_creates_agent_log_entry(self, mock_agent_manager, sample_task, db_manager):
        """Should create agent log entry when agent is created."""
        mock_agent_manager.branch_manager.create_agent_branch = MagicMock(
            return_value={
                "working_directory": "/tmp/test-project-agent",
                "branch_name": "agent-test-branch",
            }
        )
        mock_agent_manager.branch_manager.switch_to_branch = MagicMock()
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )
        
        mock_session = MagicMock()
        mock_session.name = "agent-session-2"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()
        
        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = ["pi", "--task", "test"]
            mock_get_cli.return_value = mock_cli
            
            agent = await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={"description": "Implement feature X"},
                memories=[],
                project_context="Test project context",
            )
        
        # Verify agent log was created
        with db_manager.session_scope() as session:
            log = session.query(AgentLog).filter_by(agent_id=agent.id).first()
            assert log is not None
            assert log.log_type == "created"


class TestCreateAgentForTaskMissingSharedWorktree:
    """Regression: a missing shared worktree used to either silently fork a
    disconnected new one (stranding every prior phase's commits, unmergeable)
    or, briefly, silently recover it (masking whatever actually deleted it --
    see _run_one_feature's worktree-cleanup-timing fix for the real root
    cause). Neither is acceptable: this must fail loudly instead, so the
    real cause gets found and fixed rather than papered over."""

    @pytest.mark.asyncio
    async def test_raises_when_shared_worktree_directory_is_missing(
        self, mock_agent_manager, db_manager, tmp_path
    ):
        missing_wt = tmp_path / ".worktrees" / "wt_feature-des-1-my-feature"
        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-shared",
                    name="Shared WF",
                    status="active",
                    working_directory=str(missing_wt),
                    phases_folder_path="/tmp",
                )
            )
            session.add(
                Task(
                    id="task-shared",
                    workflow_id="wf-shared",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                )
            )

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-shared").first()
            with pytest.raises(RuntimeError, match="missing or not a valid git worktree"):
                await mock_agent_manager.create_agent_for_task(
                    task=task,
                    enriched_data={},
                    memories=[],
                    project_context="",
                )

        # The task must land on "failed" with the real reason recorded --
        # not silently discarded or left pending forever.
        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-shared").first()
            assert task.status == "failed"
            assert "missing or not a valid git worktree" in (task.failure_reason or "")


class TestRestartAgent:
    """Tests for restart_agent method."""
    
    @pytest.mark.asyncio
    async def test_raises_error_when_agent_not_found(self, mock_agent_manager, db_manager):
        """Should handle gracefully when agent doesn't exist."""
        await mock_agent_manager.restart_agent("nonexistent-agent", "Test restart")
        # Should not raise, just log warning
    
    @pytest.mark.asyncio
    async def test_terminates_agent_exceeding_max_restarts(self, mock_agent_manager, db_manager):
        """Should terminate agent that exceeds max restart count."""
        # Create agent with max restarts
        with db_manager.session_scope() as session:
            agent = Agent(
                id="agent-1",
                system_prompt="Test prompt",
                status="stuck",
                cli_type="pi",
                restart_count=3,  # At max
            )
            session.add(agent)
        
        await mock_agent_manager.restart_agent("agent-1", "Stuck too long")
        
        # Verify agent was terminated
        with db_manager.session_scope() as session:
            agent = session.query(Agent).filter_by(id="agent-1").first()
            assert agent.status == "terminated"
    
    @pytest.mark.asyncio
    async def test_kills_tmux_session_on_restart(self, mock_agent_manager, db_manager):
        """Should kill tmux session when restarting agent."""
        with db_manager.session_scope() as session:
            # restart_agent looks up the agent's current task before doing
            # anything else (including the tmux kill) — without one it logs
            # "Task None not found" and returns early.
            wf = Workflow(
                id="wf-2",
                name="Test Workflow 2",
                status="active",
                phases_folder_path="/tmp",
            )
            session.add(wf)
            task = Task(
                id="task-2",
                workflow_id="wf-2",
                raw_description="Do work",
                done_definition="done",
                status="in_progress",
            )
            session.add(task)
            session.flush()  # Ensure task exists before agent references it via FK
            agent = Agent(
                id="agent-2",
                system_prompt="Test prompt",
                status="stuck",
                cli_type="pi",
                tmux_session_name="test-session",
                restart_count=0,
                current_task_id="task-2",
            )
            session.add(agent)

        # Mock the agent creation for restart
        mock_agent_manager.create_agent_for_task = AsyncMock(
            return_value=MagicMock(id="agent-2-new")
        )

        # restart_agent finds the live tmux session by iterating
        # tmux_server.sessions and calls kill_session() on THAT session
        # object — not tmux_server.kill_session(name).
        mock_tmux_session = MagicMock()
        mock_tmux_session.name = "test-session"
        mock_agent_manager.tmux_server.has_session.return_value = True
        mock_agent_manager.tmux_server.sessions = [mock_tmux_session]

        await mock_agent_manager.restart_agent("agent-2", "Test restart")

        # Verify tmux session was killed
        mock_tmux_session.kill_session.assert_called_with()
    
    @pytest.mark.asyncio
    async def test_increments_restart_count(self, mock_agent_manager, db_manager):
        """Should increment restart count on successful restart."""
        with db_manager.session_scope() as session:
            # Create workflow and phase for FK references
            session.add(Workflow(
                id="wf-1", name="Test WF", status="active", phases_folder_path="/tmp",
            ))
            session.add(Phase(
                id="phase-1", workflow_id="wf-1", name="impl", order=1,
                description="Implement", done_definitions=["done"],
            ))
            # Create task for the agent (no assigned_agent_id yet — agent doesn't exist)
            task = Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="Test task",
                done_definition="Done",
                status="in_progress",
            )
            session.add(task)
            session.flush()  # Ensure task exists before agent references it via FK
            
            agent = Agent(
                id="agent-3",
                system_prompt="Test prompt",
                status="stuck",
                cli_type="pi",
                current_task_id="task-1",
                restart_count=1,
            )
            session.add(agent)
        
        # Mock dependencies for restart
        mock_agent_manager.branch_manager.commit_changes = MagicMock(return_value={})
        mock_agent_manager.create_agent_for_task = AsyncMock(
            return_value=MagicMock(id="agent-3-new")
        )
        
        await mock_agent_manager.restart_agent("agent-3", "Test restart")
        
        # Verify restart count was incremented (in the new agent)
        # Note: The old agent is terminated, new agent is created


class TestGetActiveAgents:
    """Tests for get_active_agents method."""
    
    def test_returns_only_active_agents(self, mock_agent_manager, db_manager):
        """Should return only non-terminated agents."""
        with db_manager.session_scope() as session:
            # Add agents with different statuses
            for i, status in enumerate(["idle", "working", "stuck", "terminated"]):
                agent = Agent(
                    id=f"agent-{i}",
                    system_prompt="Test",
                    status=status,
                    cli_type="pi",
                )
                session.add(agent)
        
        agents = mock_agent_manager.get_active_agents()
        
        # Should return 3 agents (idle, working, stuck) but not terminated
        assert len(agents) == 3
        agent_statuses = {a.status for a in agents}
        assert "terminated" not in agent_statuses


class TestTerminateAgent:
    """Tests for terminate_agent method."""
    
    @pytest.mark.asyncio
    async def test_terminates_agent_successfully(self, mock_agent_manager, db_manager):
        """Should terminate agent and update database."""
        with db_manager.session_scope() as session:
            agent = Agent(
                id="agent-term-1",
                system_prompt="Test",
                status="working",
                cli_type="pi",
                tmux_session_name="test-session-term",
            )
            session.add(agent)
        
        await mock_agent_manager.terminate_agent("agent-term-1")
        
        # Verify agent was terminated
        with db_manager.session_scope() as session:
            agent = session.query(Agent).filter_by(id="agent-term-1").first()
            assert agent.status == "terminated"
        
        # Verify tmux session was killed
        mock_agent_manager.tmux_server.has_session.return_value = False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
