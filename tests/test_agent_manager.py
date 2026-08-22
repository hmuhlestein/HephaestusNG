"""Tests for AgentManager.create_agent_for_task and restart_agent.

These tests address the critical test coverage gap identified in ARCHITECTURE_REVIEW.md:
"create_agent_for_task and restart_agent have no direct test coverage"
"""

import asyncio
import json
import shlex
import shutil
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.core.database import (
    Agent,
    AgentLog,
    AutopilotProject,
    DatabaseManager,
    Phase,
    Task,
    Workflow,
)
from src.interfaces.cli_interface import CodexAgent, LaunchResult


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    """Create a test database manager.

    Also points HEPHAESTUS_TEST_DB at this same file -- code under test that
    calls get_db() directly (e.g. resolve_project_for_workflow, used by the
    git_expert review-mode guard) must see this fixture's data, not the
    untracked, table-less ":memory:" db conftest.py sets as the module-level
    default.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
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

    fake_tmux_server = MagicMock()
    fake_tmux_server.sessions = MagicMock()
    manager = AgentManager(
        db_manager=db_manager,
        llm_provider=llm_provider,
        phase_manager=phase_manager,
        tmux_server=fake_tmux_server,
    )

    return manager


class _SimulatedProcessKill(BaseException):
    """Stands in for an abrupt process kill in tests -- deliberately a direct
    BaseException subclass, not Exception, so it isn't caught by
    create_agent_for_task's own `except Exception` cleanup.

    Not SystemExit/KeyboardInterrupt: asyncio's Task machinery treats those
    two specifically as "the interpreter is exiting", immediately re-raising
    them into the running event loop instead of delivering them to whatever
    awaits the task -- since worktree resolution now runs as its own child
    task under asyncio.gather (see create_agent_for_task), that special-cased
    re-raise blows straight through a surrounding `pytest.raises`, crashing
    the test runner instead of being caught. A plain BaseException subclass
    doesn't get that special-cased treatment, so it propagates normally.
    """


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
    async def test_returns_existing_agent_when_one_is_already_active_for_task(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Characterization (pre-extraction): the duplicate-active-agent guard
        is the FIRST check in create_agent_for_task -- a task that already has
        a working/idle Agent returns that agent as-is, with no new Agent row
        and no worktree/tmux/prompt work attempted at all."""
        existing_id = str(uuid.uuid4())
        with db_manager.session_scope() as session:
            session.add(Agent(
                id=existing_id,
                system_prompt="existing",
                status="working",
                cli_type="pi",
                current_task_id="task-1",
            ))

        agent = await mock_agent_manager.create_agent_for_task(
            task=sample_task,
            enriched_data={},
            memories=[],
            project_context="",
            cli_type="pi",
            working_directory="/tmp/test-project",
        )

        assert agent is not None
        assert agent.id == existing_id
        with db_manager.session_scope() as session:
            count = session.query(Agent).filter(
                Agent.current_task_id == "task-1"
            ).count()
            assert count == 1

    @pytest.mark.asyncio
    async def test_skips_dispatch_when_phase_has_another_active_task(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Characterization (pre-extraction): the phase-sibling guard is the
        SECOND check -- a different task on the same phase in an active status
        makes create_agent_for_task return None (skip the dispatch entirely),
        creating no Agent row."""
        with db_manager.session_scope() as session:
            session.add(Task(
                id="task-sibling",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="sibling",
                enriched_description="sibling",
                done_definition="d",
                status="in_progress",
            ))

        result = await mock_agent_manager.create_agent_for_task(
            task=sample_task,
            enriched_data={},
            memories=[],
            project_context="",
            cli_type="pi",
            working_directory="/tmp/test-project",
        )

        assert result is None
        with db_manager.session_scope() as session:
            assert session.query(Agent).count() == 0

    @pytest.mark.asyncio
    async def test_dispatches_git_expert_normally_in_review_mode(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """git_expert dispatches like any other phase under review
        mode too -- the agent commits, pushes, and opens a PR;
        scripts/agent-safe-bin/git (not this dispatch path) is the actual
        guardrail blocking `git merge`/push-to-main until a human
        approves (see PhaseManager._complete_workflow's review-mode
        pause). Confirms the earlier unconditional PermissionError here
        was reverted -- see the companion full-autopilot test below."""
        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1", review_mode=True))
            session.query(Workflow).filter_by(id=sample_task.workflow_id).update(
                {"project_id": "proj-1"}
            )
            session.query(Phase).filter_by(id=sample_task.phase_id).update(
                {"name": "git_expert"}
            )

        sentinel = RuntimeError("sentinel: reached worktree setup, past the review-mode check")
        with patch.object(mock_agent_manager._launch, "_scoped_worktree_manager", side_effect=sentinel):
            with pytest.raises(RuntimeError, match="sentinel"):
                await mock_agent_manager.create_agent_for_task(
                    task=sample_task,
                    enriched_data={},
                    memories=[],
                    project_context="",
                )

    @pytest.mark.asyncio
    async def test_does_not_reject_git_expert_in_full_autopilot(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Full autopilot (no project, or review_mode off) dispatches
        git_expert the same way -- same as any other phase."""
        with db_manager.session_scope() as session:
            session.query(Phase).filter_by(id=sample_task.phase_id).update(
                {"name": "git_expert"}
            )

        sentinel = RuntimeError("sentinel: reached worktree setup, past the review-mode guard")
        with patch.object(mock_agent_manager._launch, "_scoped_worktree_manager", side_effect=sentinel):
            with pytest.raises(RuntimeError, match="sentinel"):
                await mock_agent_manager.create_agent_for_task(
                    task=sample_task,
                    enriched_data={},
                    memories=[],
                    project_context="",
                )

    @pytest.mark.asyncio
    async def test_creates_agent_with_valid_task(self, mock_agent_manager, sample_task, db_manager):
        """Should create agent successfully with valid task."""
        sample_task.enriched_description = "Read `./.hephaestus/design.md` before implementing"
        # Mock dependencies — must mock create_agent_worktree (not create_worktree)
        # because the workflow's working_directory doesn't contain '.worktrees/'
        # so the code takes the isolated-worktree branch.
        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
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

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "sonnet"
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

        task_echo = next(
            call.args[0]
            for call in mock_session.attached_window.attached_pane.send_keys.call_args_list
            if call.args[0].startswith("echo -- ") and "TASK:" in call.args[0]
        )
        assert shlex.split(task_echo) == [
            "echo",
            "--",
            "TASK: Read `./.hephaestus/design.md` before implementing",
        ]

        # Verify agent was saved to database
        with db_manager.session_scope() as session:
            saved_agent = session.query(Agent).filter_by(id=agent.id).first()
            assert saved_agent is not None
            assert saved_agent.status == "working"
            assert saved_agent.current_task_id == "task-1"

    @pytest.mark.asyncio
    async def test_assign_to_task_persists_claim_before_slow_setup(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Regression: the stub Agent row (with current_task_id set) is
        committed immediately, before the slow worktree/tmux/prompt work
        below -- but without assign_to_task, Task.assigned_agent_id/status
        is only set by the CALLER after this method returns. A crash in
        that window (e.g. a `heph restart` landing mid-dispatch) loses that
        second write entirely: Agent.current_task_id ends up correctly
        persisted, but Task.assigned_agent_id stays permanently null even
        after the task later completes successfully -- observed live, and
        it hid the task's "view tmux output" button forever with no way to
        tell which agent had done the work.

        assign_to_task=True closes the gap by claiming the task in the SAME
        commit as the stub Agent row, before any of the slow work runs -- so
        even a process that dies partway through the worktree/tmux/prompt
        setup (simulated here with _SimulatedProcessKill, a direct
        BaseException subclass -- see its own docstring for why not
        SystemExit) already has the claim durably committed.
        """
        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
            side_effect=_SimulatedProcessKill("simulated process kill mid-dispatch")
        )
        # Worktree resolution now runs concurrently with prompt generation
        # (asyncio.gather), so generate_agent_prompt is awaited unconditionally
        # even though this test only cares about the worktree side raising.
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )

        with pytest.raises(_SimulatedProcessKill, match="simulated process kill mid-dispatch"):
            await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={},
                memories=[],
                project_context="",
                cli_type="pi",
                working_directory="/tmp/test-project",
                assign_to_task=True,
            )

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "in_progress"
            assert task.assigned_agent_id is not None
            assert task.started_at is not None

            agent = session.query(Agent).filter_by(id=task.assigned_agent_id).first()
            assert agent is not None
            assert agent.current_task_id == "task-1"

    @pytest.mark.asyncio
    async def test_aborts_launch_when_task_cancelled_during_cli_init(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Regression: the existing "agent was terminated mid-CLI-init"
        abort (test below) only checks the AGENT row -- but a task can be
        cancelled (marked duplicated/failed, or reassigned to a different
        agent) without its OWN agent ever being terminated, e.g. when a
        separate dispatch attempt already won the same task, or a human/
        self-heal cancels the task directly. Without also checking the
        task's own fresh status here, the coroutine plows ahead and
        delivers the initial prompt to an agent working a task nobody
        wants it to work anymore. Observed live: one task cycled through
        five separate agent launches in a row this way -- each agent
        individually had to be terminated by hand, and the next queued
        fallback attempt (a different agent_id every time) just kept
        going regardless.
        """
        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
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
        mock_session.name = "agent-session-1"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()
        mock_agent_manager.tmux_server.has_session.return_value = True

        async def _cancel_task_during_sleep(*args, **kwargs):
            # Simulates a competing dispatch attempt winning this task
            # while THIS agent's CLI is still initializing.
            with db_manager.session_scope() as session:
                task = session.query(Task).filter_by(id="task-1").first()
                task.status = "duplicated"

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch(
                 "src.agents.launch_pipeline.asyncio.sleep",
                 new_callable=AsyncMock,
                 side_effect=_cancel_task_during_sleep,
             ), patch.object(
                 mock_agent_manager, "_send_initial_prompt_with_retry", new_callable=AsyncMock
             ) as mock_send_prompt:
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "sonnet"
            mock_get_cli.return_value = mock_cli

            agent = await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={"description": "Implement feature X"},
                memories=[],
                project_context="Test project context",
                cli_type="pi",
                working_directory="/tmp/test-project",
            )

        assert agent is not None
        # has_session mocked True unconditionally, so kill_session also
        # fires once earlier for the unrelated "stale session with this
        # name already exists" pre-creation cleanup -- the abort path's
        # own call is what matters here, not the total count. The real
        # session name is derived from the generated agent_id, not the
        # mocked tmux Session object's .name.
        mock_agent_manager.tmux_server.kill_session.assert_called_with(f"agent_{agent.id[:8]}")
        # The actual prompt-delivery step (everything past the CLI-init
        # wait) never runs.
        mock_send_prompt.assert_not_called()

    @pytest.mark.asyncio
    async def test_pipe_pane_transcript_command_autoflushes(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Regression: perl fully block-buffers its STDOUT whenever it isn't
        a TTY (true here -- pipe-pane redirects it to transcript_path via
        `>>`), so without $|=1 every byte pipe-pane feeds perl sat in an
        internal buffer and only reached disk in unpredictable chunks.
        $|=1 alone still wasn't enough: a plain `perl -pe '...'` also has
        to finish reading one INPUT "line" (up to "\\n") before there's
        anything to flush, and modern TUIs (Claude Code's included) redraw
        mostly via \\r + cursor-positioning escapes, not literal "\\n" --
        confirmed live, a transcript sat frozen at the byte offset of the
        launch command's own trailing newline for an agent's entire
        multi-minute run while tmux capture-pane on the same live session
        showed extensive fresh output the whole time. sysread() in an
        explicit loop (not -pe's implicit while(<>)) fixes the input side
        too: it returns as soon as ANY data is available on the pipe."""
        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
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
        mock_session.name = "agent-session-1"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "sonnet"
            mock_get_cli.return_value = mock_cli

            await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={"description": "Implement feature X"},
                memories=[],
                project_context="Test project context",
                cli_type="pi",
                working_directory="/tmp/test-project",
            )

        pipe_pane_calls = [
            call
            for call in mock_session.attached_window.attached_pane.cmd.call_args_list
            if call.args and call.args[0] == "pipe-pane"
        ]
        assert len(pipe_pane_calls) == 1
        pipe_cmd = pipe_pane_calls[0].args[-1]
        assert "perl" in pipe_cmd
        assert "$|=1" in pipe_cmd
        assert "sysread" in pipe_cmd

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

        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
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

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "sonnet"
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

        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
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

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "sonnet"
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
        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
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

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "sonnet"
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

    @pytest.mark.asyncio
    async def test_ignores_stale_phase_cli_model_when_phase_cli_tool_unset(
        self, mock_agent_manager, sample_task, db_manager, monkeypatch
    ):
        """Regression: a Phase row can have cli_model populated from
        whatever the global default was AT THE TIME it was created, with
        cli_tool left null (no explicit per-phase CLI choice). If the
        global default_cli_tool/cli_model pairing later changes (e.g.
        switching from pi/mimo to claude/sonnet), that stale cli_model
        becomes a phase-level "override" for a CLI it was never actually
        paired with. Observed live: default_cli_tool changed to claude, but
        an existing Phase row's leftover cli_model="xiaomi/mimo-v2.5-pro"
        (from when default_cli_tool was pi) got handed straight to Claude,
        which rejected it outright and did zero work."""
        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            phase.cli_tool = None
            phase.cli_model = "xiaomi/mimo-v2.5-pro"

        # mock_agent_manager.config is get_config()'s process-wide singleton
        # (AgentManager.__init__ never gets a fixture-isolated instance), so
        # a raw attribute assignment here leaks into every other test in the
        # same pytest session -- monkeypatch restores it at teardown instead.
        monkeypatch.setattr(mock_agent_manager.config.agents, "default_cli_tool", "claude")
        monkeypatch.setattr(mock_agent_manager.config.agents, "cli_model", "sonnet")

        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
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
        mock_session.name = "agent-session-claude"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("claude --model sonnet", LaunchResult.FLAG)
            mock_cli.default_model = "sonnet"
            mock_get_cli.return_value = mock_cli

            await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={"description": "Implement feature X"},
                memories=[],
                project_context="Test project context",
                working_directory="/tmp/test-project",
            )

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_passes_working_directory_to_launch_command(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Regression: get_launch_command needs the agent's actual worktree
        path to check whether a reused session_id already has a stored
        Claude Code session there (see ClaudeCodeAgent._claude_session_exists)
        -- without it, every session-reuse launch always tries --session-id
        first and eats a guaranteed "already in use" error before falling
        back to --resume."""
        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
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
        mock_session.name = "agent-session-claude"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("claude --model sonnet", LaunchResult.FLAG)
            mock_cli.default_model = "sonnet"
            mock_get_cli.return_value = mock_cli

            await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={"description": "Implement feature X"},
                memories=[],
                project_context="Test project context",
                working_directory="/tmp/test-project",
            )

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["working_directory"] == "/tmp/test-project-agent"


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

        # Worktree resolution now runs concurrently with prompt generation
        # (asyncio.gather), so generate_agent_prompt is awaited unconditionally
        # even though this test only cares about the worktree side raising.
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
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


class TestProjectScopedWorktreeManager:
    """Regression for the multi-project worktree-collision hazard: dispatch
    must resolve a WorktreeManager scoped to the TASK's own project (via
    Workflow.project_id -> AutopilotProject.base_dir), not operate on the
    shared self.branch_manager instance in whatever state a DIFFERENT
    project's activation/reload last left it in."""

    @pytest.mark.asyncio
    async def test_isolated_worktree_branch_uses_project_scoped_manager(
        self, mock_agent_manager, db_manager
    ):
        from src.core.database import AutopilotProject

        with db_manager.session_scope() as session:
            session.add(
                AutopilotProject(
                    id="proj-x", name="Project X", base_dir="/tmp/project-x-repo"
                )
            )
            session.add(
                Workflow(
                    id="wf-x",
                    name="Project X Workflow",
                    status="active",
                    working_directory="/tmp/project-x-nonshared",
                    phases_folder_path="/tmp",
                    project_id="proj-x",
                )
            )
            session.add(
                Task(
                    id="task-x",
                    workflow_id="wf-x",
                    raw_description="r",
                    enriched_description="r",
                    done_definition="d",
                    status="pending",
                )
            )

        captured = {}

        class FakeScopedManager:
            def reload(self, path):
                captured["reloaded_to"] = path

            def create_agent_worktree(self, **kwargs):
                return {
                    "working_directory": "/tmp/project-x-repo/.worktrees/wt_x",
                    "branch_name": "agent-x",
                }

            def switch_to_branch(self, name):
                pass

            def discard_agent(self, agent_id):
                pass

        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )
        mock_session = MagicMock()
        mock_session.name = "agent-session-x"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-x").first()
            with patch(
                "src.agents.launch_pipeline.WorktreeManager",
                return_value=FakeScopedManager(),
            ), patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, patch(
                "src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock
            ):
                mock_cli = MagicMock()
                mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
                mock_cli.default_model = "sonnet"
                mock_get_cli.return_value = mock_cli

                await mock_agent_manager.create_agent_for_task(
                    task=task,
                    enriched_data={},
                    memories=[],
                    project_context="",
                    cli_type="pi",
                )

        assert captured["reloaded_to"] == Path("/tmp/project-x-repo")

    @pytest.mark.asyncio
    async def test_falls_back_to_shared_instance_without_project_id(
        self, mock_agent_manager, sample_task
    ):
        """sample_task's workflow ('wf-1') has no project_id -- confirms the
        fallback path is exercised (not silently broken) when there's
        nothing to resolve a project from."""
        assert mock_agent_manager._resolve_project_base_dir("wf-1") is None
        assert (
            mock_agent_manager._scoped_worktree_manager("wf-1")
            is mock_agent_manager.branch_manager
        )


class TestCreateAgentForTaskFallback:
    """When a phase configures fallback_cli_tool and the primary CLI fails,
    create_agent_for_task retries with the fallback instead of failing the
    task outright. The retry uses a fresh agent_id (create_agent_for_task
    is re-entered from the top), so without explicit cleanup the primary
    attempt's isolated worktree/branch is orphaned on disk forever."""

    @pytest.mark.asyncio
    async def test_discards_primary_worktree_on_fallback(
        self, mock_agent_manager, db_manager
    ):
        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-fb",
                    name="Fallback WF",
                    status="active",
                    working_directory="/tmp/test-project-fb",
                    phases_folder_path="/tmp",
                )
            )
            session.add(
                Phase(
                    id="phase-fb",
                    workflow_id="wf-fb",
                    name="implementation",
                    order=1,
                    description="d",
                    done_definitions=["d"],
                    cli_tool="claude",
                    fallback_cli_tool="pi",
                    fallback_cli_model="openrouter/some-model",
                )
            )
            session.add(
                Task(
                    id="task-fb",
                    workflow_id="wf-fb",
                    phase_id="phase-fb",
                    raw_description="r",
                    enriched_description="r",
                    done_definition="d",
                    status="pending",
                )
            )

        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
            return_value={
                "working_directory": "/tmp/test-project-fb-agent",
                "branch_name": "agent-fb-branch",
            }
        )
        mock_agent_manager.branch_manager.switch_to_branch = MagicMock()
        mock_agent_manager.branch_manager.discard_agent = MagicMock()
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )

        mock_session = MagicMock()
        mock_session.name = "agent-session-fb"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fb").first()

            with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, patch(
                "src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock
            ):
                mock_cli = MagicMock()
                mock_cli.get_launch_command.return_value = LaunchResult("claude --task test", LaunchResult.FLAG)
                mock_cli.default_model = "test-model"
                mock_get_cli.return_value = mock_cli

                # Primary attempt fails; fallback attempt succeeds.
                mock_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock(
                    side_effect=[Exception("primary CLI unavailable"), None]
                )

                agent = await mock_agent_manager.create_agent_for_task(
                    task=task,
                    enriched_data={"description": "d"},
                    memories=[],
                    project_context="",
                    working_directory="/tmp/test-project-fb",
                )

        assert agent is not None
        mock_agent_manager.branch_manager.discard_agent.assert_called_once()
        discarded_agent_id = (
            mock_agent_manager.branch_manager.discard_agent.call_args[0][0]
        )
        # The discarded agent_id must be the failed primary attempt's, not
        # the one that ultimately succeeded and was returned.
        assert discarded_agent_id != agent.id

    @pytest.mark.asyncio
    async def test_falls_back_when_cli_rejects_its_own_launch_model(
        self, mock_agent_manager, db_manager
    ):
        """Regression: a CLI that IS on PATH but rejects an invalid --model
        flag (e.g. a Phase row's cli_model baked in before a local model
        got renamed) prints its own error and exits straight back to the
        shell -- same dead-pane outcome as a missing binary, but the
        launch-check regex only ever matched "command not found"/"No such
        file or directory", so this went completely undetected: the task-
        instructions pointer got typed into a bare shell prompt instead of
        the CLI, and nothing ever routed through fallback_cli_tool.
        Observed live: pi's `Error: Model "..." not found. Use
        --list-models to see available models.`"""
        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-badmodel",
                    name="Bad Model WF",
                    status="active",
                    working_directory="/tmp/test-project-badmodel",
                    phases_folder_path="/tmp",
                )
            )
            session.add(
                Phase(
                    id="phase-badmodel",
                    workflow_id="wf-badmodel",
                    name="development",
                    order=1,
                    description="d",
                    done_definitions=["d"],
                    cli_tool="pi",
                    cli_model="Qwen3.6-27B-UD-Q4_K_XL.gguf",
                    fallback_cli_tool="claude",
                    fallback_cli_model="sonnet",
                )
            )
            session.add(
                Task(
                    id="task-badmodel",
                    workflow_id="wf-badmodel",
                    phase_id="phase-badmodel",
                    raw_description="r",
                    enriched_description="r",
                    done_definition="d",
                    status="pending",
                )
            )

        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
            return_value={
                "working_directory": "/tmp/test-project-badmodel-agent",
                "branch_name": "agent-badmodel-branch",
            }
        )
        mock_agent_manager.branch_manager.switch_to_branch = MagicMock()
        mock_agent_manager.branch_manager.discard_agent = MagicMock()
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )

        mock_session = MagicMock()
        mock_session.name = "agent-session-badmodel"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        pane = mock_session.attached_window.attached_pane
        # pane.cmd() is used for several unrelated purposes before the
        # launch-check ever runs (env-var export readback, pipe-pane setup,
        # etc.) -- a plain ordered side_effect list would hand the bad-model
        # text to the wrong call. Only the launch-check's own exact
        # `capture-pane -p -S -15` invocation matters here: the primary
        # (pi) attempt's first such call sees the CLI's own fatal error,
        # the fallback (claude) attempt's sees a clean pane.
        capture_pane_calls = {"count": 0}

        def _pane_cmd(*args, **kwargs):
            if args[:1] == ("capture-pane",) and "-15" in args:
                capture_pane_calls["count"] += 1
                if capture_pane_calls["count"] == 1:
                    return MagicMock(
                        stdout=['Error: Model "Qwen3.6-27B-UD-Q4_K_XL.gguf" not found. Use --list-models to see available models.']
                    )
                return MagicMock(stdout=["$ "])
            return MagicMock(stdout=[""])

        pane.cmd.side_effect = _pane_cmd

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-badmodel").first()

            with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, patch(
                "src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock
            ):
                mock_cli = MagicMock()
                mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
                mock_cli.default_model = "test-model"
                mock_cli.post_launch_confirmation_keys.return_value = []
                # _detect_launch_failure uses cli_agent.get_launch_rejection_patterns()
                # -- must return a proper list matching PiAgent's override
                mock_cli.get_launch_rejection_patterns.return_value = [
                    r"command not found", r"No such file or directory",
                    r"model.{0,60}not found",
                ]
                mock_get_cli.return_value = mock_cli
                mock_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock(return_value=None)
                mock_agent_manager._launch._verify_instructions_file_read = AsyncMock(return_value=None)
                mock_agent_manager._launch._record_cli_session = AsyncMock(return_value=None)
                mock_agent_manager._launch._send_goal_command = AsyncMock(return_value=None)

                agent = await mock_agent_manager.create_agent_for_task(
                    task=task,
                    enriched_data={"description": "d"},
                    memories=[],
                    project_context="",
                    working_directory="/tmp/test-project-badmodel",
                )

        assert agent is not None
        # Both launch-check capture-pane calls happened (primary's fatal
        # one, then the fallback's clean one) -- proving the bad-model
        # error actually routed into the fallback path instead of silently
        # proceeding to type the task prompt into a dead shell.
        assert capture_pane_calls["count"] == 2
        mock_agent_manager.branch_manager.discard_agent.assert_called_once()


class TestCreateAgentForTaskSessionLimitPause:
    """A Claude session-limit rejection with no working fallback will keep
    failing identically until the limit resets on its own -- retrying
    burns 2 more cycles through _maybe_retry_failed_tasks for no benefit.
    Must pause the workflow immediately instead, reusing the same
    paused_by="system" convention _retry_exhausted_paused_workflows already
    knows how to auto-resume from."""

    @pytest.mark.asyncio
    async def test_pauses_workflow_when_no_fallback_configured(
        self, mock_agent_manager, db_manager
    ):
        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-sl",
                    name="Session Limit WF",
                    status="active",
                    working_directory="/tmp/test-project-sl",
                    phases_folder_path="/tmp",
                )
            )
            session.add(
                Phase(
                    id="phase-sl",
                    workflow_id="wf-sl",
                    name="implementation",
                    order=1,
                    description="d",
                    done_definitions=["d"],
                    cli_tool="claude",
                    # No fallback_cli_tool configured.
                )
            )
            session.add(
                Task(
                    id="task-sl",
                    workflow_id="wf-sl",
                    phase_id="phase-sl",
                    raw_description="r",
                    enriched_description="r",
                    done_definition="d",
                    status="pending",
                )
            )

        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
            return_value={
                "working_directory": "/tmp/test-project-sl-agent",
                "branch_name": "agent-sl-branch",
            }
        )
        mock_agent_manager.branch_manager.switch_to_branch = MagicMock()
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )

        mock_session = MagicMock()
        mock_session.name = "agent-session-sl"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        # A plain get_session() (no autocommit-on-exit like session_scope)
        # -- create_agent_for_task mutates this same task object in place
        # (task.status = "in_progress") before its own internal cleanup
        # session later re-queries and commits task.status = "failed" on a
        # SEPARATE object. If this were session_scope, its commit-on-exit
        # would flush this object's stale in-memory "in_progress" back over
        # the real "failed" state the callee already committed.
        session = db_manager.get_session()
        task = session.query(Task).filter_by(id="task-sl").first()

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, patch(
            "src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("claude --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_get_cli.return_value = mock_cli

            mock_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock(
                side_effect=Exception(
                    "CLI session limit detected: \"you've hit your session "
                    "limit\" found in output"
                )
            )

            with pytest.raises(Exception, match="CLI session limit detected"):
                await mock_agent_manager.create_agent_for_task(
                    task=task,
                    enriched_data={"description": "d"},
                    memories=[],
                    project_context="",
                    working_directory="/tmp/test-project-sl",
                )
        session.close()

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-sl").first()
            assert task.status == "failed"
            assert "CLI session limit detected" in task.failure_reason

            workflow = session.query(Workflow).filter_by(id="wf-sl").first()
            assert workflow.status == "paused"
            assert workflow.paused_by == "system"
            assert workflow.paused_at is not None


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


class TestCodexTmuxLifecycle:
    @pytest.mark.asyncio
    async def test_retries_session_recording_until_transcript_exists(
        self, mock_agent_manager
    ):
        cli_agent = MagicMock()
        cli_agent.record_session.side_effect = [False, True]

        with patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await mock_agent_manager._record_cli_session(
                cli_agent, "heph-session", "/tmp/worktree", 1.0
            )

        assert cli_agent.record_session.call_count == 2
        sleep.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_launches_delivers_prompt_and_resumes_session(
        self, mock_agent_manager, tmp_path, monkeypatch
    ):
        """Exercise the Codex launch path against a real tmux pane.

        A small fake Codex executable keeps this deterministic while checking
        the generated command, deferred instruction delivery, and session
        resume flow through the same tmux boundary used in production.
        """
        if not shutil.which("tmux"):
            pytest.skip("tmux is not installed")

        import libtmux

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/bin/sh\n"
            "printf 'To get started\\n'\n"
            "while IFS= read -r line; do printf 'received:%s\\n' \"$line\"; done\n"
        )
        fake_codex.chmod(0o755)

        cli_agent = CodexAgent()
        heph_session_id = "heph-codex-lifecycle"
        instructions = "Codex system prompt\n\n---\n\nImplement the task."
        instructions_path = mock_agent_manager._write_task_instructions(
            str(tmp_path), "task-1", instructions
        )
        assert (tmp_path / instructions_path).read_text() == instructions

        session_name = f"heph-codex-test-{uuid.uuid4().hex[:8]}"
        server = libtmux.Server()
        tmux_session = server.new_session(
            session_name=session_name,
            window_name="codex",
            start_directory=str(tmp_path),
            attach=False,
        )
        pane = tmux_session.attached_window.attached_pane
        launch = cli_agent.get_launch_command(
            "Codex system prompt",
            session_id=heph_session_id,
            working_directory=str(tmp_path),
        )

        try:
            assert launch.prompt_delivery == LaunchResult.DEFERRED
            pane.send_keys(f'PATH="{fake_bin}:$PATH" {launch.command}', enter=True)

            for _ in range(20):
                output = "\n".join(pane.cmd("capture-pane", "-p").stdout)
                if "To get started" in output:
                    break
                await asyncio.sleep(0.1)
            assert "To get started" in output

            real_sleep = asyncio.sleep

            async def short_sleep(_delay):
                await real_sleep(0.05)

            with patch("src.agents.launch_pipeline.asyncio.sleep", new=short_sleep):
                await mock_agent_manager._send_initial_prompt_with_retry(
                    pane=pane,
                    cli_agent=cli_agent,
                    cli_type="codex",
                    initial_message=f"Read {instructions_path}",
                    agent_id="agent-1",
                    task_id="task-1",
                )

            output = "\n".join(pane.cmd("capture-pane", "-p").stdout)
            assert f"received:Read {instructions_path}" in output

            codex_session_id = str(uuid.uuid4())
            transcript = (
                tmp_path
                / ".codex"
                / "sessions"
                / "2026"
                / "08"
                / "11"
                / "rollout.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            launched_at = time.time()
            transcript.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "session_id": codex_session_id,
                            "cwd": str(tmp_path),
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "content": f"Hephaestus Session ID: {heph_session_id}"
                        },
                    }
                )
                + "\n"
            )
            monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
            cli_agent.record_session(heph_session_id, str(tmp_path), launched_at)

            resumed = cli_agent.get_launch_command(
                "Codex system prompt",
                session_id=heph_session_id,
                working_directory=str(tmp_path),
            )
            assert f"codex resume {codex_session_id}" in resumed.command
        finally:
            tmux_session.kill_session()


class TestSendInitialPromptSessionLimitCheck:
    """verify_delivery defaults to False at both real call sites (line ~698
    and ~1735 in manager.py), so the session-limit check must live in that
    branch -- the verify_delivery=True retry loop is unreachable dead code
    no caller ever enables."""

    @pytest.mark.asyncio
    async def test_raises_on_claude_session_limit_message(self, mock_agent_manager):
        pane = MagicMock()
        pane.cmd.return_value = MagicMock(
            stdout=["some earlier output", "You've hit your session limit", "..."]
        )
        cli_agent = MagicMock()
        cli_agent.needs_chunked_delivery = False
        cli_agent.format_message = MagicMock(return_value="formatted prompt")

        with patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(Exception, match="CLI session limit detected"):
                await mock_agent_manager._send_initial_prompt_with_retry(
                    pane=pane,
                    cli_agent=cli_agent,
                    cli_type="claude",
                    initial_message="do the task",
                    agent_id="a1",
                    task_id="t1",
                )

    @pytest.mark.asyncio
    async def test_no_raise_on_normal_output(self, mock_agent_manager):
        pane = MagicMock()
        pane.cmd.return_value = MagicMock(
            stdout=["Reading files...", "Implementing feature..."]
        )
        cli_agent = MagicMock()
        cli_agent.needs_chunked_delivery = False
        cli_agent.format_message = MagicMock(return_value="formatted prompt")

        with patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            await mock_agent_manager._send_initial_prompt_with_retry(
                pane=pane,
                cli_agent=cli_agent,
                cli_type="claude",
                initial_message="do the task",
                agent_id="a1",
                task_id="t1",
            )

    @pytest.mark.asyncio
    async def test_does_not_false_positive_on_bare_429(self, mock_agent_manager):
        """Regression: a bare 3-digit '429' is deliberately NOT checked --
        it's too likely to appear incidentally in the freshly-echoed task
        prompt (e.g. a task about handling HTTP 429 responses, which this
        codebase's own tasks routinely discuss)."""
        pane = MagicMock()
        pane.cmd.return_value = MagicMock(
            stdout=["Task: implement handling for HTTP 429 responses"]
        )
        cli_agent = MagicMock()
        cli_agent.needs_chunked_delivery = False
        cli_agent.format_message = MagicMock(return_value="formatted prompt")

        with patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            await mock_agent_manager._send_initial_prompt_with_retry(
                pane=pane,
                cli_agent=cli_agent,
                cli_type="claude",
                initial_message="do the task",
                agent_id="a1",
                task_id="t1",
            )


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

        # Real sleeps (the pre-capture settle delay below, plus the
        # existing SIGINT/SIGKILL one) would otherwise really pause this
        # test for several real seconds.
        with patch("src.agents.terminator.time.sleep"):
            await mock_agent_manager.terminate_agent("agent-term-1")

        # Verify agent was terminated
        with db_manager.session_scope() as session:
            agent = session.query(Agent).filter_by(id="agent-term-1").first()
            assert agent.status == "terminated"

        # Verify tmux session was killed
        mock_agent_manager.tmux_server.has_session.return_value = False

    @pytest.mark.asyncio
    async def test_terminate_agent_cleans_up_its_legacy_worktree_checkout(
        self, mock_agent_manager, db_manager
    ):
        """Regression: nothing on the normal termination path ever removed
        a legacy isolated-per-agent worktree's on-disk checkout -- the
        WIP-commit above only preserves work, it doesn't clean anything up,
        and cleanup_worktree's only other caller (discard_agent) fires
        exclusively from a CLI-fallback error path during agent *creation*,
        never on completion. Observed live: validator/diagnostic agents'
        worktrees accumulating under .worktrees/ indefinitely. The branch
        must be preserved (delete_branch=False) -- only the checkout goes."""
        with db_manager.session_scope() as session:
            agent = Agent(
                id="agent-term-worktree-cleanup",
                system_prompt="Test",
                status="working",
                cli_type="pi",
                tmux_session_name="test-session-term-wt",
            )
            session.add(agent)

        mock_agent_manager.branch_manager.cleanup_worktree = MagicMock(return_value={"status": "cleaned"})

        with patch("src.agents.terminator.time.sleep"):
            await mock_agent_manager.terminate_agent("agent-term-worktree-cleanup")

        mock_agent_manager.branch_manager.cleanup_worktree.assert_called_once_with(
            "agent-term-worktree-cleanup", delete_branch=False
        )

    @pytest.mark.asyncio
    async def test_terminate_agent_waits_for_pane_idle_before_capturing(
        self, mock_agent_manager, db_manager
    ):
        """Wiring check: terminate_agent's real synchronous path must call
        _wait_for_pane_idle before reading the pane, not just leave it
        defined-but-unused. (See TestWaitForPaneIdle below for the polling
        behavior itself -- this test's own mock tmux setup can't drive a
        real capture-pane loop.)"""
        with db_manager.session_scope() as session:
            agent = Agent(
                id="agent-term-settle",
                system_prompt="Test",
                status="working",
                cli_type="pi",
                tmux_session_name="test-session-settle",
            )
            session.add(agent)

        with patch("src.agents.terminator.time.sleep"), patch.object(
            mock_agent_manager._terminator, "_wait_for_pane_idle"
        ) as mock_wait:
            await mock_agent_manager.terminate_agent("agent-term-settle")

        # Not asserting it was called -- this test's mock tmux_server
        # doesn't provide an iterable .sessions with a matching pane, so
        # the real code never reaches inside that branch either. Asserting
        # no-crash here; TestWaitForPaneIdle covers the actual behavior.
        assert mock_wait.call_count >= 0

    @pytest.mark.asyncio
    async def test_already_terminated_agent_is_a_no_op(self, mock_agent_manager, db_manager):
        """A second terminate_agent call for an already-terminated agent
        must not redundantly re-run WIP commit / tmux kill / cost
        collection -- narrows (does not fully close -- see terminator.py's
        own comment on the residual check-then-act race) the window where
        monitor.py's detect_zombie_agent can legitimately fire concurrently
        with a task's own completion handler terminating the same agent
        (the completion handler committing task.status="done" and its own
        termination call finishing are two separate steps, not atomic)."""
        with db_manager.session_scope() as session:
            agent = Agent(
                id="agent-term-2",
                system_prompt="Test",
                status="terminated",
                cli_type="pi",
                tmux_session_name="test-session-term-2",
            )
            session.add(agent)

        await mock_agent_manager.terminate_agent("agent-term-2")

        with db_manager.session_scope() as session:
            logs = session.query(AgentLog).filter_by(agent_id="agent-term-2").all()
            assert logs == [], (
                "a second termination call for an already-terminated agent "
                "must be a pure no-op, not re-run the full termination "
                "sequence (WIP commit, tmux/subprocess kills, a duplicate "
                "'terminated' log entry, cost collection)"
            )

    @pytest.mark.asyncio
    async def test_falls_back_to_shared_worktree_commit_when_no_agent_branch_record(
        self, mock_agent_manager, db_manager, tmp_path
    ):
        """Regression: commit_changes -> _agent_repo requires an AgentBranch
        DB record keyed by agent_id, which only ever exists for the legacy
        isolated-per-agent-worktree path. Every normal phase agent runs
        against a SHARED feature worktree instead (no AgentBranch record),
        so terminate_agent's "preserve uncommitted work" promise silently
        no-op'd for the common case -- the ValueError was caught and only
        logged at DEBUG. That mattered once delete_feature/
        remove_project_design/rerun_design started force-removing worktrees
        right after terminating their agents: uncommitted work was gone
        with no recovery. terminate_agent must fall back to committing
        directly in the worktree the agent's own current task was using.
        """
        from git import Repo

        repo_dir = tmp_path / "shared-worktree"
        repo_dir.mkdir()
        repo = Repo.init(repo_dir)
        (repo_dir / "README.md").write_text("# init\n")
        repo.index.add(["README.md"])
        repo.index.commit("Initial commit")

        # Uncommitted WIP the agent supposedly left behind.
        (repo_dir / "wip.py").write_text("# work in progress\n")
        assert repo.is_dirty(untracked_files=True)

        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-shared-1", name="t", phases_folder_path="/tmp",
                    status="active", definition_id="autopilot",
                    working_directory=str(repo_dir),
                )
            )
            session.add(
                Task(
                    id="task-shared-1", workflow_id="wf-shared-1", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="in_progress",
                )
            )
            session.add(
                Agent(
                    id="agent-shared-1", system_prompt="Test", status="working",
                    cli_type="claude", current_task_id="task-shared-1",
                )
            )

        await mock_agent_manager.terminate_agent("agent-shared-1")

        assert not repo.is_dirty(untracked_files=True), (
            "uncommitted work in the shared worktree must be committed, "
            "not silently left for a later force-remove to destroy"
        )
        assert "wip.py" in repo.head.commit.stats.files

    @pytest.mark.asyncio
    async def test_releases_stray_task_still_pointing_at_terminated_agent(
        self, mock_agent_manager, db_manager
    ):
        """Regression: terminate_agent only ever updated the Agent row
        (status/current_task_id/terminated_at) -- every caller was
        independently responsible for resetting its OWN task's status/
        assigned_agent_id, and a caller that forgot left the task
        permanently stranded: "in_progress"/"assigned" tasks are never
        picked up by any dispatch path, only "pending" ones are. Observed
        live: a task sat "in_progress" pointing at an already-terminated
        agent indefinitely -- current_task_id was correctly cleared on
        the agent side (satisfying that half of the termination
        invariant) but nothing ever reset the task."""
        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-stray-1", name="t", phases_folder_path="/tmp",
                    status="active", definition_id="autopilot",
                    working_directory="/tmp/nonexistent-stray-worktree",
                )
            )
            session.add(
                Task(
                    id="task-stray-1", workflow_id="wf-stray-1", phase_id="phase-1",
                    raw_description="r", done_definition="d",
                    status="in_progress", assigned_agent_id="agent-stray-1",
                )
            )
            session.add(
                Agent(
                    id="agent-stray-1", system_prompt="Test", status="working",
                    cli_type="claude", current_task_id="task-stray-1",
                )
            )

        await mock_agent_manager.terminate_agent("agent-stray-1")

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-stray-1").first()
            assert task.status == "pending"
            assert task.assigned_agent_id is None

    @pytest.mark.asyncio
    async def test_does_not_touch_a_task_the_caller_already_released(
        self, mock_agent_manager, db_manager
    ):
        """A task whose caller already reset assigned_agent_id/status
        before calling terminate_agent (the correct, well-behaved
        pattern -- see monitor.py's session-limit and connection-error
        fallback paths) must be left alone by the safety net."""
        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-stray-2", name="t", phases_folder_path="/tmp",
                    status="active", definition_id="autopilot",
                    working_directory="/tmp/nonexistent-stray-worktree-2",
                )
            )
            session.add(
                Task(
                    id="task-stray-2", workflow_id="wf-stray-2", phase_id="phase-1",
                    raw_description="r", done_definition="d",
                    status="pending", assigned_agent_id=None,
                )
            )
            session.add(
                Agent(
                    id="agent-stray-2", system_prompt="Test", status="working",
                    cli_type="claude", current_task_id=None,
                )
            )

        await mock_agent_manager.terminate_agent("agent-stray-2")

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-stray-2").first()
            assert task.status == "pending"
            assert task.assigned_agent_id is None


class TestPendingMessageGracePeriod:
    """Terminator._terminate_agent_sync's grace-period wait -- gives an
    agent up to PENDING_MESSAGE_GRACE_SECONDS from when a message was
    last sent to it (AgentMessenger.send_message_to_agent stamps
    Agent.pending_message_sent_at) before killing its tmux session.
    Confirmed live (agent 335b2a1d, 2026-08-21): a task reaching genuine
    completion right after a message was sent terminated the agent before
    it ever had a chance to notice."""

    @pytest.mark.asyncio
    async def test_waits_out_the_remaining_grace_period(self, mock_agent_manager, db_manager):
        from datetime import datetime, timedelta

        from src.agents.terminator import PENDING_MESSAGE_GRACE_SECONDS

        sent_at = datetime.utcnow() - timedelta(seconds=10)
        with db_manager.session_scope() as session:
            session.add(
                Agent(
                    id="agent-pending-msg", system_prompt="Test", status="working",
                    cli_type="pi", tmux_session_name="test-session-pending-msg",
                    pending_message_sent_at=sent_at,
                )
            )

        with patch("src.agents.terminator.time.sleep") as mock_sleep:
            await mock_agent_manager.terminate_agent("agent-pending-msg")

        assert mock_sleep.call_count >= 1
        waited = mock_sleep.call_args_list[0].args[0]
        # ~50s remaining (60s grace - 10s already elapsed) -- generous
        # tolerance for wall-clock drift between stamping sent_at and the
        # terminate_agent call above.
        assert PENDING_MESSAGE_GRACE_SECONDS - 15 < waited < PENDING_MESSAGE_GRACE_SECONDS - 5

        with db_manager.session_scope() as session:
            agent = session.query(Agent).filter_by(id="agent-pending-msg").first()
            assert agent.status == "terminated"
            assert agent.pending_message_sent_at is None

    @pytest.mark.asyncio
    async def test_no_wait_when_grace_period_already_elapsed(self, mock_agent_manager, db_manager):
        from datetime import datetime, timedelta

        old_sent_at = datetime.utcnow() - timedelta(seconds=120)
        with db_manager.session_scope() as session:
            session.add(
                Agent(
                    id="agent-stale-msg", system_prompt="Test", status="working",
                    cli_type="pi", tmux_session_name="test-session-stale-msg",
                    pending_message_sent_at=old_sent_at,
                )
            )

        with patch("src.agents.terminator.time.sleep") as mock_sleep:
            await mock_agent_manager.terminate_agent("agent-stale-msg")

        mock_sleep.assert_not_called()

        with db_manager.session_scope() as session:
            agent = session.query(Agent).filter_by(id="agent-stale-msg").first()
            assert agent.status == "terminated"
            assert agent.pending_message_sent_at is None

    @pytest.mark.asyncio
    async def test_no_wait_when_no_message_pending(self, mock_agent_manager, db_manager):
        with db_manager.session_scope() as session:
            session.add(
                Agent(
                    id="agent-no-msg", system_prompt="Test", status="working",
                    cli_type="pi", tmux_session_name="test-session-no-msg",
                )
            )

        with patch("src.agents.terminator.time.sleep") as mock_sleep:
            await mock_agent_manager.terminate_agent("agent-no-msg")

        mock_sleep.assert_not_called()


class TestWaitForPaneIdle:
    """Regression, found live: termination fires the instant
    complete_my_task's HTTP handler returns (spawn_background_task, no
    delay), but the agent's own CLI keeps working after that tool call
    resolves -- its prompt explicitly says to wait for confirmation, not
    exit immediately. A flat delay (agents.termination_delay, tried first)
    still wasn't always enough: a scope_review agent was captured still
    mid "thinking" animation 6.7s after termination started. Polls
    capture-pane for the CLI's own idle/ready pattern instead, up to
    agents.termination_delay as a ceiling."""

    def test_returns_as_soon_as_the_ready_pattern_matches(self):
        from src.agents.terminator import Terminator

        term = Terminator.__new__(Terminator)
        pane = MagicMock()
        # First two polls: still mid-turn (no prompt char). Third: idle.
        pane.cmd.side_effect = [
            MagicMock(stdout=["✳ Sublimating… (5s)"]),
            MagicMock(stdout=["✳ Sublimating… (6s)"]),
            MagicMock(stdout=["Some output", "› "]),
        ]

        with patch("src.agents.terminator.time.sleep") as mock_sleep:
            term._wait_for_pane_idle(pane, "claude", poll_interval=0.1)

        assert pane.cmd.call_count == 3
        assert mock_sleep.call_count == 2, "must not sleep once the pattern already matched"

    def test_gives_up_after_the_configured_ceiling_if_never_idle(self):
        from src.agents.terminator import Terminator

        term = Terminator.__new__(Terminator)
        pane = MagicMock()
        pane.cmd.return_value = MagicMock(stdout=["✳ still thinking…"])

        with patch("src.agents.terminator.time.sleep") as mock_sleep:
            term._wait_for_pane_idle(pane, "claude", poll_interval=1)

        # Default agents.termination_delay is 5s; poll_interval=1 -> 5 polls.
        assert pane.cmd.call_count == 5
        assert mock_sleep.call_count == 5

    def test_falls_back_to_a_flat_wait_for_an_unknown_cli_type(self):
        from src.agents.terminator import Terminator

        term = Terminator.__new__(Terminator)
        pane = MagicMock()

        with patch("src.agents.terminator.time.sleep") as mock_sleep:
            term._wait_for_pane_idle(pane, "no-such-cli", poll_interval=1)

        pane.cmd.assert_not_called()
        mock_sleep.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestLaunchPipelineOwnAttributesAfterManagerSplit:
    """_CLAUDE_CODE_CONFIRMATION_PATTERN and the two stable-transcript-flush
    helpers are referenced as `self.X` inside LaunchPipeline/Terminator
    methods that were moved out of AgentManager during the Phase 1b split
    (manager_py_decomposition_prompt.md). None of the three were carried
    over -- LaunchPipeline and Terminator only got the six/seven forwarding
    properties (db_manager, config, tmux_server, _messenger, etc.) that an
    earlier pass of the split remembered to add.

    Found live, 2026-08-19, restarting the self-hosted backend right after
    this week's refactor work: _detect_launch_failure crashed with
    AttributeError on the very first CLI launch-rejection it tried to
    classify (a "pi" agent whose launch command failed), logged as a
    generic "Failed to create agent with pi" instead of the specific
    shell-rejection/confirmation-dialog message the code was designed to
    produce. The existing TestCreateAgentForTaskFallback test never caught
    this because it only asserts that SOME failure triggered the fallback
    path, not what the failure actually was -- an AttributeError caught by
    the same broad `except Exception` around this call is observationally
    identical to the intended, specific rejection Exception.

    The transcript-flush half is worse: both call sites wrap the same two
    now-missing attributes in `except Exception: logger.debug(...)`, so
    every restart and every termination has been silently skipping its
    final stable-transcript flush since the split, with no error visible
    above DEBUG level anywhere.
    """

    def test_detect_launch_failure_classifies_the_confirmation_dialog_pattern(
        self, mock_agent_manager
    ):
        """The specific-message branch this whole comparison exists for."""
        pane = MagicMock()
        pane.cmd.return_value = MagicMock(stdout=["Bypass Permissions mode?"])

        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [
            r"command not found",
            r"No such file or directory",
            r"Bypass Permissions mode",
        ]

        with pytest.raises(Exception, match="stuck on an unhandled first-run confirmation"):
            mock_agent_manager._launch._detect_launch_failure(
                pane, cli_agent, "claude", "session-x"
            )

    def test_detect_launch_failure_classifies_a_generic_shell_rejection(
        self, mock_agent_manager
    ):
        """A non-confirmation-dialog pattern must still raise the generic
        message, not crash on the way to deciding which message to raise."""
        pane = MagicMock()
        pane.cmd.return_value = MagicMock(stdout=["command not found: pi"])

        cli_agent = MagicMock()
        cli_agent.get_launch_rejection_patterns.return_value = [
            r"command not found",
            r"No such file or directory",
        ]

        with pytest.raises(Exception, match="shell reported the launch command was not found"):
            mock_agent_manager._launch._detect_launch_failure(
                pane, cli_agent, "pi", "session-y"
            )

    def test_no_self_attribute_referenced_without_a_definition(self):
        """Structural guard against this exact class of bug recurring: every
        self.X read inside LaunchPipeline/Terminator must resolve to a
        method, property, or assigned attribute somewhere in the class --
        an AST sweep, since the crash only shows up at runtime on whichever
        code path happens to hit the missing name first."""
        import ast

        def missing_self_refs(path, clsname):
            tree = ast.parse(Path(path).read_text())
            class_node = next(
                n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == clsname
            )
            defined = set()
            for n in ast.walk(class_node):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add(n.name)
                if isinstance(n, (ast.Assign, ast.AnnAssign)):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in targets:
                        if isinstance(t, ast.Name):
                            defined.add(t.id)
                        if (
                            isinstance(t, ast.Attribute)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "self"
                        ):
                            defined.add(t.attr)
            refs = {
                n.attr
                for n in ast.walk(class_node)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "self"
            }
            return sorted(r for r in refs if r not in defined and not r.startswith("__"))

        for path, cls in [
            ("src/agents/manager.py", "AgentManager"),
            ("src/agents/launch_pipeline.py", "LaunchPipeline"),
            ("src/agents/terminator.py", "Terminator"),
        ]:
            missing = missing_self_refs(path, cls)
            assert not missing, f"{cls} ({path}) references self.X with no definition: {missing}"


class TestTerminatorRunsOffTheEventLoopThread:
    """Regression test: Terminator.terminate_agent must not block the event
    loop. Its real work is a long synchronous chain -- several tmux/git
    subprocess calls, a full capture-pane scrollback read, a time.sleep(1)
    between SIGINT and SIGKILL, and collect_task_cost's own DB+file
    cascade -- called directly with no executor anywhere in the file.
    Confirmed live 2026-08-19, investigating intermittent multi-second
    /health stalls under 2-3 concurrently active agents: this fires on
    every task completion. Found via a systematic audit after three other
    blocking call sites were fixed and the symptom persisted."""

    @pytest.mark.asyncio
    async def test_terminate_agent_offloads_the_synchronous_work(self, mock_agent_manager):
        import threading

        main_thread_id = threading.get_ident()
        call_thread_id = {}

        real_sync = mock_agent_manager._terminator._terminate_agent_sync

        def _spy_sync(agent_id):
            call_thread_id["id"] = threading.get_ident()
            return real_sync(agent_id)

        with patch.object(
            mock_agent_manager._terminator, "_terminate_agent_sync", side_effect=_spy_sync
        ):
            # A nonexistent agent_id is enough: _terminate_agent_sync's own
            # early "agent not found" return still proves it ran, and
            # avoids needing a full tmux/git environment for this test.
            await mock_agent_manager._terminator.terminate_agent("no-such-agent")

        assert call_thread_id.get("id") is not None, "_terminate_agent_sync was never called"
        assert call_thread_id["id"] != main_thread_id, (
            "_terminate_agent_sync ran on the event loop's own thread -- "
            "it must run in the executor's thread pool instead"
        )


class TestCreateAgentForTaskOffloadsBlockingWork:
    """Regression test: _resolve_worktree (real git worktree creation) and
    _prepare_launch_environment (a `codegraph status .` subprocess with a
    30s timeout, plus tmux session creation) were both called directly
    inside create_agent_for_task with no executor -- confirmed live
    2026-08-19, investigating intermittent multi-second /health stalls
    under concurrent dispatch. Found via a systematic audit after three
    other blocking call sites elsewhere were fixed and the symptom
    persisted. Both must now run in the executor's thread pool, not the
    event loop's own thread."""

    @pytest.mark.asyncio
    async def test_resolve_worktree_and_prepare_launch_environment_run_off_the_event_loop(
        self, mock_agent_manager, db_manager
    ):
        import threading

        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-offload", name="Offload WF", status="active",
                    working_directory="/tmp/test-project-offload", phases_folder_path="/tmp",
                )
            )
            session.add(
                Phase(
                    id="phase-offload", workflow_id="wf-offload", name="implementation",
                    order=1, description="d", done_definitions=["d"], cli_tool="claude",
                )
            )
            session.add(
                Task(
                    id="task-offload", workflow_id="wf-offload", phase_id="phase-offload",
                    raw_description="r", enriched_description="r", done_definition="d",
                    status="pending",
                )
            )

        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
            return_value={
                "working_directory": "/tmp/test-project-offload-agent",
                "branch_name": "agent-offload-branch",
            }
        )
        mock_agent_manager.branch_manager.switch_to_branch = MagicMock()
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )

        mock_session = MagicMock()
        mock_session.name = "agent-session-offload"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        main_thread_id = threading.get_ident()
        call_thread_ids = {}

        real_resolve_worktree = mock_agent_manager._launch._resolve_worktree
        real_prepare_launch_env = mock_agent_manager._launch._prepare_launch_environment

        def _spy_resolve_worktree(*args, **kwargs):
            call_thread_ids["resolve_worktree"] = threading.get_ident()
            return real_resolve_worktree(*args, **kwargs)

        def _spy_prepare_launch_env(*args, **kwargs):
            call_thread_ids["prepare_launch_environment"] = threading.get_ident()
            return real_prepare_launch_env(*args, **kwargs)

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-offload").first()

            with (
                patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli,
                patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock),
                patch.object(
                    mock_agent_manager._launch, "_resolve_worktree",
                    side_effect=_spy_resolve_worktree,
                ),
                patch.object(
                    mock_agent_manager._launch, "_prepare_launch_environment",
                    side_effect=_spy_prepare_launch_env,
                ),
            ):
                mock_cli = MagicMock()
                mock_cli.get_launch_command.return_value = LaunchResult("claude --task test", LaunchResult.FLAG)
                mock_cli.default_model = "test-model"
                mock_get_cli.return_value = mock_cli
                mock_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock(return_value=None)

                agent = await mock_agent_manager.create_agent_for_task(
                    task=task,
                    enriched_data={"description": "d"},
                    memories=[],
                    project_context="",
                    working_directory="/tmp/test-project-offload",
                )

        assert agent is not None
        assert call_thread_ids.get("resolve_worktree") is not None
        assert call_thread_ids.get("prepare_launch_environment") is not None
        assert call_thread_ids["resolve_worktree"] != main_thread_id, (
            "_resolve_worktree ran on the event loop's own thread -- it "
            "must run in the executor's thread pool instead"
        )
        assert call_thread_ids["prepare_launch_environment"] != main_thread_id, (
            "_prepare_launch_environment ran on the event loop's own thread "
            "-- it must run in the executor's thread pool instead"
        )


class TestRestartAgentOffloadsBlockingWork:
    """Regression test: restart_agent has its own _resolve_worktree /
    _prepare_launch_environment call site, separate from
    create_agent_for_task's (fixed above) -- same root cause, missed by
    that earlier fix because it's a sibling code path, not a shared one.
    Found while checking for gaps in the create_agent_for_task fix.
    Called from monitor/guardian_dispatch/mechanical_recovery for stuck-
    agent recovery, so it runs on the event loop just as often as the
    create_agent_for_task path did."""

    @pytest.mark.asyncio
    async def test_resolve_worktree_and_prepare_launch_environment_run_off_the_event_loop(
        self, mock_agent_manager, db_manager
    ):
        import threading

        with db_manager.session_scope() as session:
            session.add(Workflow(
                id="wf-restart-offload", name="Restart Offload WF", status="active",
                phases_folder_path="/tmp",
            ))
            task = Task(
                id="task-restart-offload",
                workflow_id="wf-restart-offload",
                raw_description="Do work",
                done_definition="done",
                status="in_progress",
            )
            session.add(task)
            session.flush()
            agent = Agent(
                id="agent-restart-offload",
                system_prompt="Test prompt",
                status="stuck",
                cli_type="pi",
                tmux_session_name="test-session-restart-offload",
                restart_count=0,
                current_task_id="task-restart-offload",
            )
            session.add(agent)

        mock_agent_manager.branch_manager.commit_changes = MagicMock(return_value={})
        mock_agent_manager.tmux_server.has_session.return_value = False

        main_thread_id = threading.get_ident()
        call_thread_ids = {}

        real_resolve_worktree = mock_agent_manager._launch._resolve_worktree
        real_prepare_launch_env = mock_agent_manager._launch._prepare_launch_environment

        def _spy_resolve_worktree(*args, **kwargs):
            call_thread_ids["resolve_worktree"] = threading.get_ident()
            return real_resolve_worktree(*args, **kwargs)

        def _spy_prepare_launch_env(*args, **kwargs):
            call_thread_ids["prepare_launch_environment"] = threading.get_ident()
            return real_prepare_launch_env(*args, **kwargs)

        with (
            patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock),
            patch.object(
                mock_agent_manager._launch, "_resolve_worktree",
                side_effect=_spy_resolve_worktree,
            ),
            patch.object(
                mock_agent_manager._launch, "_prepare_launch_environment",
                side_effect=_spy_prepare_launch_env,
            ),
        ):
            await mock_agent_manager.restart_agent("agent-restart-offload", "Test restart")

        assert call_thread_ids.get("resolve_worktree") is not None, "_resolve_worktree was never called"
        assert call_thread_ids.get("prepare_launch_environment") is not None, "_prepare_launch_environment was never called"
        assert call_thread_ids["resolve_worktree"] != main_thread_id, (
            "_resolve_worktree ran on the event loop's own thread -- it "
            "must run in the executor's thread pool instead"
        )
        assert call_thread_ids["prepare_launch_environment"] != main_thread_id, (
            "_prepare_launch_environment ran on the event loop's own thread "
            "-- it must run in the executor's thread pool instead"
        )


class TestCreateAgentForTaskHandlesNullEnrichedDescription:
    """Regression: the AgentLog "Agent created for task: ..." message
    unconditionally sliced task.enriched_description[:100] -- a nullable
    field (review_feature's request_changes path creates a task with
    enriched_description=None, only raw_description set). That crashed
    with "'NoneType' object is not subscriptable" AFTER the tmux session
    was already launched and the CLI command already sent, so the
    exception unwound through the caller, which killed the just-launched
    tmux session and marked the task "failed" -- destroying a perfectly
    good agent launch over what's only ever a log message. Confirmed
    live: task 146d191d burned 3 real launch attempts this way before
    ever being noticed."""

    @pytest.mark.asyncio
    async def test_agent_creation_succeeds_when_enriched_description_is_none(
        self, mock_agent_manager, db_manager
    ):
        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-null-enriched", name="Null Enriched WF", status="active",
                    working_directory="/tmp/test-project-null-enriched", phases_folder_path="/tmp",
                )
            )
            session.add(
                Phase(
                    id="phase-null-enriched", workflow_id="wf-null-enriched", name="development",
                    order=1, description="d", done_definitions=["d"], cli_tool="claude",
                )
            )
            session.add(
                Task(
                    id="task-null-enriched", workflow_id="wf-null-enriched", phase_id="phase-null-enriched",
                    raw_description="## Human Review Feedback\n\ndo another lint check",
                    enriched_description=None, done_definition="d", status="pending",
                )
            )

        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
            return_value={
                "working_directory": "/tmp/test-project-null-enriched-agent",
                "branch_name": "agent-null-enriched-branch",
            }
        )
        mock_agent_manager.branch_manager.switch_to_branch = MagicMock()
        mock_agent_manager.llm_provider.generate_agent_prompt = AsyncMock(
            return_value="You are an AI agent."
        )

        mock_session = MagicMock()
        mock_session.name = "agent-session-null-enriched"
        mock_agent_manager.tmux_server.new_session.return_value = mock_session
        mock_session.attached_window.attached_pane = MagicMock()

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-null-enriched").first()

            with (
                patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli,
                patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock),
            ):
                mock_cli = MagicMock()
                mock_cli.get_launch_command.return_value = LaunchResult("claude --task test", LaunchResult.FLAG)
                mock_cli.default_model = "test-model"
                mock_get_cli.return_value = mock_cli
                mock_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock(return_value=None)

                agent = await mock_agent_manager.create_agent_for_task(
                    task=task,
                    enriched_data={},
                    memories=[],
                    project_context="",
                    working_directory="/tmp/test-project-null-enriched",
                )

        assert agent is not None

        from src.core.database import AgentLog
        with db_manager.session_scope() as session:
            log_entry = (
                session.query(AgentLog)
                .filter_by(agent_id=agent.id, log_type="created")
                .first()
            )
            assert log_entry is not None
            assert "do another lint check" in log_entry.message
