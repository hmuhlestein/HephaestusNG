"""Characterization tests for restart_agent behavior BEFORE any extraction.

These tests pin restart_agent's current behavior so that the shared-step
extraction in Phase 1b Target 4 can be verified as behavior-preserving.
Every test here MUST pass against the pre-extraction code at HEAD.

What is pinned:
  (a) env/model resolution from agent.cli_type/agent.cli_model
  (b) session-id generation (deterministic key from project/design/phase/model;
      excluded agent types: validator, result_validator, diagnostic — note
      "arbitration" is NOT excluded here, per the Phase 3 mismatch)
  (c) prompt delivery through _send_initial_prompt_with_retry
  (d) worktree resolution: existing worktree used, branch-path fallback,
      both-absent -> restart_wd None WITHOUT raising (silent-None)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.database import (
    Agent,
    DatabaseManager,
    Phase,
    Task,
    Workflow,
)
from src.interfaces.cli_interface import LaunchResult

# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def restart_agent_manager(db_manager):
    """AgentManager with only the mocks restart_agent needs."""
    from src.agents.manager import AgentManager

    llm_provider = MagicMock()
    phase_manager = MagicMock()

    manager = AgentManager(
        db_manager=db_manager,
        llm_provider=llm_provider,
        phase_manager=phase_manager,
        tmux_server=MagicMock(),
    )
    return manager


def _setup_restart_prereqs(db_manager, *, cli_type="pi", cli_model="some-model",
                           agent_type="phase", workflow_id="wf-r",
                           task_id="task-r", phase_id="phase-r"):
    """Insert the DB rows restart_agent needs: a Task, a Phase (optional),
    a Workflow, and an Agent in 'stuck' status with restart_count=0."""
    with db_manager.session_scope() as session:
        session.add(Workflow(
            id=workflow_id, name="Test WF", status="active",
            phases_folder_path="/tmp",
        ))
        if phase_id:
            session.add(Phase(
                id=phase_id, workflow_id=workflow_id,
                name="implementation", order=1,
                description="d", done_definitions=["done"],
            ))
        session.add(Task(
            id=task_id, workflow_id=workflow_id, phase_id=phase_id,
            raw_description="r", enriched_description="r",
            done_definition="d", status="in_progress",
        ))
        session.flush()
        agent = Agent(
            id="agent-r1", system_prompt="original-prompt",
            status="stuck", cli_type=cli_type, cli_model=cli_model,
            agent_type=agent_type, current_task_id=task_id,
            restart_count=0, tmux_session_name="old-session",
        )
        session.add(agent)
    return "agent-r1"


# ── (a) env/model resolution from agent.cli_type / agent.cli_model ───────

class TestRestartModelResolution:
    """Pin how restart_agent resolves the model for the relaunch."""

    @pytest.mark.asyncio
    async def test_uses_agent_cli_model_when_set(self, restart_agent_manager, db_manager):
        """When the agent has a cli_model, that value wins (over global config)."""
        agent_id = _setup_restart_prereqs(db_manager, cli_model="openrouter/custom-model")
        restart_agent_manager.tmux_server.has_session.return_value = True

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --model openrouter/custom-model", LaunchResult.FLAG)
            mock_cli.default_model = "fallback-model"
            mock_get_cli.return_value = mock_cli

            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["model"] == "openrouter/custom-model"

    @pytest.mark.asyncio
    async def test_falls_back_to_global_when_agent_cli_model_empty(
        self, restart_agent_manager, db_manager, monkeypatch
    ):
        """When agent.cli_model is falsy, falls back to global config.cli_model
        (only if cli_type matches default_cli_tool)."""
        agent_id = _setup_restart_prereqs(db_manager, cli_model="")
        restart_agent_manager.tmux_server.has_session.return_value = True
        restart_agent_manager.config.agents.default_cli_tool = "pi"
        restart_agent_manager.config.agents.cli_model = "global-pi-model"

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --model global-pi-model", LaunchResult.FLAG)
            mock_cli.default_model = "cli-agent-default"
            mock_get_cli.return_value = mock_cli

            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["model"] == "global-pi-model"

    @pytest.mark.asyncio
    async def test_falls_back_to_cli_default_when_both_agent_model_and_global_absent(
        self, restart_agent_manager, db_manager
    ):
        """When agent.cli_model is falsy AND global doesn't apply,
        the CLI agent's default_model is used."""
        agent_id = _setup_restart_prereqs(db_manager, cli_type="pi", cli_model="")
        restart_agent_manager.tmux_server.has_session.return_value = True
        restart_agent_manager.config.agents.default_cli_tool = "claude"  # different from pi

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "Qwen3.8-27B-UD-Q4_K_XL.gguf"
            mock_get_cli.return_value = mock_cli

            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["model"] == "Qwen3.8-27B-UD-Q4_K_XL.gguf"


# ── (b) session-id generation ─────────────────────────────────────────────

class TestRestartSessionId:
    """Pin session-id generation behavior in restart_agent."""

    @pytest.mark.asyncio
    async def test_session_id_populated_for_phase_agent(self, restart_agent_manager, db_manager):
        """A 'phase' agent with workflow launch_params gets a deterministic session_id."""
        agent_id = _setup_restart_prereqs(db_manager)
        restart_agent_manager.tmux_server.has_session.return_value = True

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.launch_params = {
                "project_path": "/tmp/proj",
                "feature_id": "feat",
            }

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_get_cli.return_value = mock_cli

            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["session_id"] != "", (
            "phase agent should get a non-empty session_id from launch_params"
        )

    @pytest.mark.asyncio
    async def test_session_id_empty_for_validator_agent(self, restart_agent_manager, db_manager):
        """Validator agents must NEVER get a session_id, even with valid
        launch_params, to avoid resuming a prior phase agent's conversation."""
        agent_id = _setup_restart_prereqs(db_manager, agent_type="validator")
        restart_agent_manager.tmux_server.has_session.return_value = True

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.launch_params = {
                "project_path": "/tmp/proj",
                "feature_id": "feat",
            }

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_get_cli.return_value = mock_cli

            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["session_id"] == ""

    @pytest.mark.asyncio
    async def test_session_id_empty_for_result_validator(self, restart_agent_manager, db_manager):
        agent_id = _setup_restart_prereqs(db_manager, agent_type="result_validator")
        restart_agent_manager.tmux_server.has_session.return_value = True

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.launch_params = {"project_path": "/tmp/proj", "feature_id": "feat"}

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_get_cli.return_value = mock_cli

            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["session_id"] == ""

    @pytest.mark.asyncio
    async def test_session_id_empty_for_diagnostic(self, restart_agent_manager, db_manager):
        agent_id = _setup_restart_prereqs(db_manager, agent_type="diagnostic")
        restart_agent_manager.tmux_server.has_session.return_value = True

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.launch_params = {"project_path": "/tmp/proj", "feature_id": "feat"}

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_get_cli.return_value = mock_cli

            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["session_id"] == ""

    @pytest.mark.asyncio
    async def test_arbitration_agent_gets_session_id_in_restart(
        self, restart_agent_manager, db_manager
    ):
        """PHASE 3 MISMATCH PINNED: restart_agent's exclusion list does NOT
        include 'arbitration' (unlike create_agent_for_task which does).
        This test pins that current behavior — it MUST NOT change until
        Phase 3 unifies the exclusion lists."""
        # 'arbitration' is not in the DB's CHECK constraint, so drop it
        # temporarily to allow the raw SQL update. This is a test-only DB
        # so the schema change is harmless.
        agent_id = _setup_restart_prereqs(db_manager, agent_type="phase")
        restart_agent_manager.tmux_server.has_session.return_value = True
        mock_session_obj = MagicMock()
        mock_session_obj.name = "new-session"
        restart_agent_manager.tmux_server.new_session.return_value = mock_session_obj
        restart_agent_manager.tmux_server.sessions = [MagicMock(name="old-session")]

        from sqlalchemy import text as sa_text
        # Temporarily remove the CHECK constraint to allow 'arbitration'
        raw = db_manager.get_session()
        try:
            raw.execute(sa_text("PRAGMA foreign_keys = OFF"))
            raw.execute(sa_text(
                "CREATE TABLE agents_backup AS SELECT * FROM agents"
            ))
            raw.execute(sa_text("DROP TABLE agents"))
            raw.execute(sa_text(
                "CREATE TABLE agents ("
                "  id VARCHAR PRIMARY KEY,"
                "  created_at DATETIME,"
                "  system_prompt TEXT,"
                "  status VARCHAR,"
                "  cli_type VARCHAR,"
                "  tmux_session_name VARCHAR,"
                "  current_task_id VARCHAR,"
                "  last_activity DATETIME,"
                "  launched_at DATETIME,"
                "  health_check_failures INTEGER DEFAULT 0,"
                "  restart_count INTEGER DEFAULT 0,"
                "  cli_model VARCHAR,"
                "  pending_message_sent_at DATETIME,"
                "  agent_type VARCHAR,"
                "  kept_alive_for_validation BOOLEAN DEFAULT 0,"
                "  terminated_at DATETIME"
                ")"
            ))
            raw.execute(sa_text("INSERT INTO agents SELECT * FROM agents_backup"))
            raw.execute(sa_text("DROP TABLE agents_backup"))
            raw.execute(sa_text(
                "UPDATE agents SET agent_type='arbitration' WHERE id=:id"
            ), {"id": agent_id})
            raw.execute(sa_text("PRAGMA foreign_keys = ON"))
            raw.commit()
        finally:
            raw.close()

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.launch_params = {"project_path": "/tmp/proj", "feature_id": "feat"}

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock), \
             patch.object(
                 restart_agent_manager.branch_manager, "get_agent_branch_path",
                 return_value=None,
             ):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_cli.post_launch_confirmation_keys.return_value = []
            mock_get_cli.return_value = mock_cli
            restart_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock()
            restart_agent_manager._send_goal_command = AsyncMock()
            restart_agent_manager._launch._record_cli_session = AsyncMock()
            restart_agent_manager._launch._verify_instructions_file_read = AsyncMock()

            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["session_id"] != "", (
            "arbitration agents currently get session_id in restart_agent "
            "(unlike create_agent_for_task) — this is the Phase 3 mismatch"
        )


# ── (c) prompt delivery ──────────────────────────────────────────────────

class TestRestartPromptDelivery:
    """Pin how restart_agent delivers the resume prompt."""

    @pytest.mark.asyncio
    async def test_delivers_prompt_via_send_initial_prompt_with_retry(
        self, restart_agent_manager, db_manager
    ):
        """restart_agent calls _send_initial_prompt_with_retry with the
        instructions_pointer when restart_wd is available."""
        agent_id = _setup_restart_prereqs(db_manager)
        restart_agent_manager.tmux_server.has_session.return_value = True

        mock_pane = MagicMock()
        mock_pane.cmd.return_value = MagicMock(stdout=[""])

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_cli.post_launch_confirmation_keys.return_value = []
            mock_get_cli.return_value = mock_cli
            restart_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock()
            restart_agent_manager._launch._verify_instructions_file_read = AsyncMock()
            restart_agent_manager._launch._record_cli_session = AsyncMock()
            restart_agent_manager._send_goal_command = AsyncMock()

            await restart_agent_manager.restart_agent(agent_id, "Stuck")

        restart_agent_manager._launch._send_initial_prompt_with_retry.assert_called_once()
        _, call_kwargs = restart_agent_manager._launch._send_initial_prompt_with_retry.call_args
        assert call_kwargs["cli_type"] == "pi"
        assert call_kwargs["agent_id"] == agent_id

    @pytest.mark.asyncio
    async def test_delivers_full_message_when_no_worktree(
        self, restart_agent_manager, db_manager
    ):
        """When no worktree exists (restart_wd is None), restart_agent
        falls back to delivering the full restart_message text instead of
        an instructions_pointer."""
        agent_id = _setup_restart_prereqs(db_manager)
        restart_agent_manager.tmux_server.has_session.return_value = True

        # No workflow working_directory -> restart_wd will be None
        # No agent branch path either -> both absent -> None

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock), \
             patch.object(
                 restart_agent_manager.branch_manager, "get_agent_branch_path",
                 return_value=None,
             ):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_cli.post_launch_confirmation_keys.return_value = []
            mock_get_cli.return_value = mock_cli
            restart_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock()
            restart_agent_manager._send_goal_command = AsyncMock()

            await restart_agent_manager.restart_agent(agent_id, "Stuck")

        restart_agent_manager._launch._send_initial_prompt_with_retry.assert_called_once()
        _, call_kwargs = restart_agent_manager._launch._send_initial_prompt_with_retry.call_args
        # When restart_wd is None, instructions_pointer is "" and the
        # fallback delivers the raw restart_message text.
        assert call_kwargs["initial_message"] != ""


# ── (d) worktree resolution ──────────────────────────────────────────────

class TestRestartWorktreeResolution:
    """Pin restart_agent's worktree-finding behavior."""

    @pytest.mark.asyncio
    async def test_uses_workflow_working_directory_when_present(
        self, restart_agent_manager, db_manager, tmp_path
    ):
        """When workflow.working_directory exists and is on disk, restart_wd
        is set to it."""
        agent_id = _setup_restart_prereqs(db_manager)
        mock_session_obj = MagicMock()
        mock_session_obj.name = "new-session"
        restart_agent_manager.tmux_server.new_session.return_value = mock_session_obj
        restart_agent_manager.tmux_server.sessions = [MagicMock(name="old-session")]

        # Use a real git repo so WorktreeManager.reload() doesn't fail
        import git as _git
        wt_dir = tmp_path / "shared-wt"
        wt_dir.mkdir()
        repo = _git.Repo.init(str(wt_dir))
        (wt_dir / "README.md").write_text("# test")
        repo.index.add(["README.md"])
        repo.index.commit("init")

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.working_directory = str(wt_dir)

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_cli.post_launch_confirmation_keys.return_value = []
            mock_get_cli.return_value = mock_cli
            restart_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock()
            restart_agent_manager._launch._verify_instructions_file_read = AsyncMock()
            restart_agent_manager._launch._record_cli_session = AsyncMock()
            restart_agent_manager._send_goal_command = AsyncMock()

            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["working_directory"] == str(wt_dir)

    @pytest.mark.asyncio
    async def test_silent_none_when_no_workflow_wd_and_no_agent_branch(
        self, restart_agent_manager, db_manager
    ):
        """PHASE 3 MISMATCH PINNED: when workflow.working_directory is absent
        (or the path doesn't exist) AND the agent's own branch path is also
        absent, restart_wd is silently set to None — restart_agent does NOT
        raise an error. The agent launches with working_directory=None. This
        is the silent-None behavior the doc says to preserve."""
        agent_id = _setup_restart_prereqs(db_manager)

        # workflow.working_directory stays None (default from fixture)
        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock), \
             patch.object(
                 restart_agent_manager.branch_manager, "get_agent_branch_path",
                 return_value=None,
             ):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_cli.post_launch_confirmation_keys.return_value = []
            mock_get_cli.return_value = mock_cli
            restart_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock()
            restart_agent_manager._send_goal_command = AsyncMock()

            # Must NOT raise — the silent-None is the current behavior
            await restart_agent_manager.restart_agent(agent_id, "Test")

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["working_directory"] is None, (
            "Both workflow working_directory and agent branch path absent "
            "should produce restart_wd=None without raising"
        )

    @pytest.mark.asyncio
    async def test_session_name_has_r_suffix(self, restart_agent_manager, db_manager, tmp_path):
        """restart_agent creates a tmux session with an _r suffix on the
        session name, to distinguish it from the original."""
        agent_id = _setup_restart_prereqs(db_manager)

        mock_new_session = MagicMock()
        mock_new_session.name = "new-session"
        restart_agent_manager.tmux_server.new_session.return_value = mock_new_session
        restart_agent_manager.tmux_server.sessions = [MagicMock(name="old-session")]

        # Need a real git repo so WorktreeManager.reload() doesn't fail
        import git as _git
        wt_dir = tmp_path / "agent-wt"
        wt_dir.mkdir()
        repo = _git.Repo.init(str(wt_dir))
        (wt_dir / "README.md").write_text("# test")
        repo.index.add(["README.md"])
        repo.index.commit("init")

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.working_directory = str(wt_dir)

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_cli.post_launch_confirmation_keys.return_value = []
            mock_get_cli.return_value = mock_cli
            restart_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock()
            restart_agent_manager._send_goal_command = AsyncMock()
            restart_agent_manager._launch._record_cli_session = AsyncMock()
            restart_agent_manager._launch._verify_instructions_file_read = AsyncMock()

            await restart_agent_manager.restart_agent(agent_id, "Test")

        # The new_session call should use an _r-suffixed name
        new_session_call = restart_agent_manager.tmux_server.new_session.call_args
        session_name_arg = new_session_call.kwargs.get("session_name") or (
            new_session_call.args[0] if new_session_call.args else ""
        )
        assert "_r" in session_name_arg, (
            f"Restart session name should contain _r suffix, got: {session_name_arg}"
        )


# ── restart gap-closings (Step 5 regression tests) ──────────────────────

class TestRestartGapClosings:
    """Regression tests for the two documented restart gap-closings:
    (1) _check_termination_race is now called after the post-launch sleep
    (2) _detect_launch_failure is now called to catch dead-pane launches"""

    @pytest.mark.asyncio
    async def test_restart_calls_termination_race_check_after_sleep(
        self, restart_agent_manager, db_manager, tmp_path
    ):
        """After the post-launch sleep, restart_agent now calls
        _check_termination_race (gap-closing). Verify it is called."""
        agent_id = _setup_restart_prereqs(db_manager)

        mock_session_obj = MagicMock()
        mock_session_obj.name = "new-session"
        restart_agent_manager.tmux_server.new_session.return_value = mock_session_obj
        restart_agent_manager.tmux_server.sessions = [MagicMock(name="old-session")]
        restart_agent_manager.tmux_server.has_session.return_value = True

        import git as _git
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        repo = _git.Repo.init(str(wt_dir))
        (wt_dir / "README.md").write_text("# test")
        repo.index.add(["README.md"])
        repo.index.commit("init")

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.working_directory = str(wt_dir)

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_cli.post_launch_confirmation_keys.return_value = []
            mock_cli.get_launch_rejection_patterns.return_value = [r"command not found"]
            mock_get_cli.return_value = mock_cli
            restart_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock()
            restart_agent_manager._send_goal_command = AsyncMock()
            restart_agent_manager._launch._record_cli_session = AsyncMock()
            restart_agent_manager._launch._verify_instructions_file_read = AsyncMock()
            # Mock _check_termination_race to simulate a detected race
            mock_term_info = MagicMock()
            mock_term_info.id = agent_id
            restart_agent_manager._launch._check_termination_race = AsyncMock(
                return_value=mock_term_info
            )

            await restart_agent_manager.restart_agent(agent_id, "Test")

        # _check_termination_race was called
        restart_agent_manager._launch._check_termination_race.assert_called_once()
        # Prompt was NOT delivered because _check_termination_race aborted
        restart_agent_manager._launch._send_initial_prompt_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_aborts_when_launch_detected_as_failed(
        self, restart_agent_manager, db_manager, tmp_path
    ):
        """restart_agent now calls _detect_launch_failure (gap-closing).
        If it raises, the restart aborts without delivering the prompt."""
        agent_id = _setup_restart_prereqs(db_manager)

        mock_session_obj = MagicMock()
        mock_session_obj.name = "new-session"
        restart_agent_manager.tmux_server.new_session.return_value = mock_session_obj
        restart_agent_manager.tmux_server.sessions = [MagicMock(name="old-session")]
        restart_agent_manager.tmux_server.has_session.return_value = True

        import git as _git
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        repo = _git.Repo.init(str(wt_dir))
        (wt_dir / "README.md").write_text("# test")
        repo.index.add(["README.md"])
        repo.index.commit("init")

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.working_directory = str(wt_dir)

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli, \
             patch("src.agents.launch_pipeline.asyncio.sleep", new_callable=AsyncMock):
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_cli.post_launch_confirmation_keys.return_value = []
            mock_get_cli.return_value = mock_cli
            restart_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock()
            restart_agent_manager._send_goal_command = AsyncMock()
            restart_agent_manager._launch._record_cli_session = AsyncMock()
            restart_agent_manager._launch._verify_instructions_file_read = AsyncMock()
            restart_agent_manager._launch._check_termination_race = AsyncMock(
                return_value=None
            )
            # _detect_launch_failure raises on launch failure
            restart_agent_manager._launch._detect_launch_failure = MagicMock(
                side_effect=Exception("CLI failed to start")
            )

            await restart_agent_manager.restart_agent(agent_id, "Test")

        restart_agent_manager._launch._detect_launch_failure.assert_called_once()
        restart_agent_manager._launch._send_initial_prompt_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_uses_active_readiness_detection_not_flat_sleep(
        self, restart_agent_manager, db_manager, tmp_path
    ):
        """Regression: restart_agent's own launch sequence still used a
        flat `await asyncio.sleep(25)` after the primary create_agent_
        for_task path was already switched to active polling via
        _wait_for_cli_ready (cli_agent.get_health_check_pattern()) --
        same bug, same file, missed in the restart code path."""
        agent_id = _setup_restart_prereqs(db_manager)

        mock_session_obj = MagicMock()
        mock_session_obj.name = "new-session"
        restart_agent_manager.tmux_server.new_session.return_value = mock_session_obj
        restart_agent_manager.tmux_server.sessions = [MagicMock(name="old-session")]
        restart_agent_manager.tmux_server.has_session.return_value = True

        import git as _git
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        repo = _git.Repo.init(str(wt_dir))
        (wt_dir / "README.md").write_text("# test")
        repo.index.add(["README.md"])
        repo.index.commit("init")

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-r").first()
            wf.working_directory = str(wt_dir)

        with patch("src.agents.launch_pipeline.get_cli_agent") as mock_get_cli:
            mock_cli = MagicMock()
            mock_cli.get_launch_command.return_value = LaunchResult("pi --task test", LaunchResult.FLAG)
            mock_cli.default_model = "test-model"
            mock_cli.post_launch_confirmation_keys.return_value = []
            mock_cli.get_launch_rejection_patterns.return_value = [r"command not found"]
            mock_get_cli.return_value = mock_cli
            restart_agent_manager._launch._send_initial_prompt_with_retry = AsyncMock()
            restart_agent_manager._send_goal_command = AsyncMock()
            restart_agent_manager._launch._record_cli_session = AsyncMock()
            restart_agent_manager._launch._verify_instructions_file_read = AsyncMock()
            restart_agent_manager._launch._check_termination_race = AsyncMock(return_value=None)
            restart_agent_manager._launch._wait_for_cli_ready = AsyncMock()

            await restart_agent_manager.restart_agent(agent_id, "Test")

        restart_agent_manager._launch._wait_for_cli_ready.assert_called_once()
