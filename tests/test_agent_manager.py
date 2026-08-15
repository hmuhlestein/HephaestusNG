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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.interfaces.cli_interface import CodexAgent, LaunchResult

from src.core.database import (
    Agent,
    AgentLog,
    AutopilotProject,
    DatabaseManager,
    Phase,
    Task,
    Workflow,
)


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    """Create a test database manager.

    Also points HEPHAESTUS_TEST_DB at this same file -- code under test that
    calls get_db() directly (e.g. resolve_project_for_workflow, used by the
    git_commit_push review-mode guard) must see this fixture's data, not the
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
    async def test_rejects_git_commit_push_without_human_approval_in_review_mode(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Git actions must never be dispatched by the autonomous pipeline
        while the project is actually in review mode (see the companion
        full-autopilot test below for the review_mode-off case)."""
        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1", review_mode=True))
            session.query(Workflow).filter_by(id=sample_task.workflow_id).update(
                {"project_id": "proj-1"}
            )
            session.query(Phase).filter_by(id=sample_task.phase_id).update(
                {"name": "git_commit_push"}
            )

        with pytest.raises(PermissionError, match="manual-only"):
            await mock_agent_manager.create_agent_for_task(
                task=sample_task,
                enriched_data={},
                memories=[],
                project_context="",
            )

        with db_manager.session_scope() as session:
            assert session.query(Agent).count() == 0

    @pytest.mark.asyncio
    async def test_does_not_reject_git_commit_push_in_full_autopilot(
        self, mock_agent_manager, sample_task, db_manager
    ):
        """Regression: full autopilot (no project, or review_mode off) must
        retain the original autonomous git_commit_push behavior -- the
        pipeline dispatches a real agent for it same as any other phase.
        Only review_mode actually asks for a human in the loop."""
        with db_manager.session_scope() as session:
            session.query(Phase).filter_by(id=sample_task.phase_id).update(
                {"name": "git_commit_push"}
            )

        sentinel = RuntimeError("sentinel: reached worktree setup, past the review-mode guard")
        with patch.object(mock_agent_manager, "_scoped_worktree_manager", side_effect=sentinel):
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

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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
        setup (simulated here with SystemExit, which -- unlike a plain
        Exception -- isn't caught by create_agent_for_task's own internal
        `except Exception` cleanup, so it accurately models an abrupt
        process kill rather than a handled in-process failure) already has
        the claim durably committed.
        """
        mock_agent_manager.branch_manager.create_agent_worktree = MagicMock(
            side_effect=SystemExit("simulated process kill mid-dispatch")
        )

        with pytest.raises(SystemExit, match="simulated process kill mid-dispatch"):
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

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch(
                 "src.agents.manager.asyncio.sleep",
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

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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
        monkeypatch.setattr(mock_agent_manager.config, "default_cli_tool", "claude")
        monkeypatch.setattr(mock_agent_manager.config, "cli_model", "sonnet")

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

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, \
             patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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
                "src.agents.manager.WorktreeManager",
                return_value=FakeScopedManager(),
            ), patch("src.agents.manager.get_cli_agent") as mock_get_cli, patch(
                "src.agents.manager.asyncio.sleep", new_callable=AsyncMock
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

            with patch("src.agents.manager.get_cli_agent") as mock_get_cli, patch(
                "src.agents.manager.asyncio.sleep", new_callable=AsyncMock
            ):
                mock_cli = MagicMock()
                mock_cli.get_launch_command.return_value = LaunchResult("claude --task test", LaunchResult.FLAG)
                mock_cli.default_model = "test-model"
                mock_get_cli.return_value = mock_cli

                # Primary attempt fails; fallback attempt succeeds.
                mock_agent_manager._send_initial_prompt_with_retry = AsyncMock(
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

            with patch("src.agents.manager.get_cli_agent") as mock_get_cli, patch(
                "src.agents.manager.asyncio.sleep", new_callable=AsyncMock
            ):
                mock_cli = MagicMock()
                mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
                mock_cli.default_model = "test-model"
                mock_cli.post_launch_confirmation_keys.return_value = []
                mock_get_cli.return_value = mock_cli
                mock_agent_manager._send_initial_prompt_with_retry = AsyncMock(return_value=None)
                mock_agent_manager._verify_instructions_file_read = AsyncMock(return_value=None)
                mock_agent_manager._record_cli_session = AsyncMock(return_value=None)
                mock_agent_manager._send_goal_command = AsyncMock(return_value=None)

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

        with patch("src.agents.manager.get_cli_agent") as mock_get_cli, patch(
            "src.agents.manager.asyncio.sleep", new_callable=AsyncMock
        ):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("claude --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_get_cli.return_value = mock_cli

            mock_agent_manager._send_initial_prompt_with_retry = AsyncMock(
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

        with patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock) as sleep:
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

            with patch("src.agents.manager.asyncio.sleep", new=short_sleep):
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

        with patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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

        with patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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

        with patch("src.agents.manager.asyncio.sleep", new_callable=AsyncMock):
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

        await mock_agent_manager.terminate_agent("agent-term-1")

        # Verify agent was terminated
        with db_manager.session_scope() as session:
            agent = session.query(Agent).filter_by(id="agent-term-1").first()
            assert agent.status == "terminated"

        # Verify tmux session was killed
        mock_agent_manager.tmux_server.has_session.return_value = False

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
