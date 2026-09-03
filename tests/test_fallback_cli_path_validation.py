"""Tests for the §7.1 fix: a configured fallback CLI tool (hephaestus_config.
yaml's agents.default_fallback_cli_tool, or a Phase's own fallback_cli_tool)
must be validated against what's actually installed (shutil.which) before an
agent is launched under it -- and mechanical_recovery.py / restart_agent must
re-resolve from CURRENT config rather than trusting a possibly-stale value,
so a config fix actually takes effect on the next attempt.

Covers:
  - is_cli_tool_available: the shutil.which wrapper itself.
  - LaunchPipeline._resolve_phase_config: disables an uninstalled fallback
    rather than handing it back for a caller to launch into a dead pane.
  - LaunchPipeline.restart_agent: re-resolves cli_type from current config
    when the agent's own stored cli_type is no longer installed, instead of
    blindly reusing it; fails the task cleanly (no dead-pane relaunch) when
    nothing usable is configured; a fix made to the config between two
    restart attempts is picked up on the second attempt.
  - MechanicalRecoveryDetector._resolve_fallback_cli: skips a configured
    fallback that isn't installed, the same way callers already treat an
    unset default_fallback_cli_tool.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.core.database import (
    Agent,
    DatabaseManager,
    Phase,
    Task,
    Workflow,
)
from src.interfaces.cli_interface import LaunchResult, is_cli_tool_available


# ── is_cli_tool_available ───────────────────────────────────────────────

class TestIsCliToolAvailable:
    def test_true_for_a_binary_actually_on_path(self):
        assert is_cli_tool_available("python3") is True

    def test_false_for_a_binary_not_on_path(self):
        assert is_cli_tool_available("definitely-not-a-real-cli-binary-xyz123") is False


# ── LaunchPipeline._resolve_phase_config ────────────────────────────────

@pytest.fixture
def launch_pipeline():
    from src.agents.manager import AgentManager

    db_manager = Mock(spec=DatabaseManager)
    llm_provider = Mock()
    agent_manager = AgentManager(db_manager=db_manager, llm_provider=llm_provider)
    return agent_manager


class TestResolvePhaseConfigFallbackValidation:
    def test_disables_global_fallback_when_not_on_path(self, launch_pipeline):
        """agents.default_fallback_cli_tool is set but not installed --
        PhaseConfig must come back with no fallback at all, not a fallback
        a caller would then launch into a dead pane."""
        launch_pipeline.config.agents.default_fallback_cli_tool = "pi"
        launch_pipeline.config.agents.default_fallback_cli_model = "some-model"
        launch_pipeline.config.agents.default_cli_tool = "claude"
        task = Task(id="t1", raw_description="r", enriched_description="r", done_definition="d")

        with patch("src.agents.launch_pipeline.is_cli_tool_available", return_value=False):
            phase_config = launch_pipeline._launch._resolve_phase_config(
                task, cli_type=None, phase_cli_tool=None, phase_cli_model=None,
                phase_glm_token_env=None, phase_thinking_level=None,
            )

        assert phase_config.fallback_cli_tool is None
        assert phase_config.fallback_cli_model is None

    def test_keeps_global_fallback_when_available(self, launch_pipeline):
        """Unchanged behavior: an installed fallback is still resolved."""
        launch_pipeline.config.agents.default_fallback_cli_tool = "pi"
        launch_pipeline.config.agents.default_fallback_cli_model = "some-model"
        launch_pipeline.config.agents.default_cli_tool = "claude"
        task = Task(id="t2", raw_description="r", enriched_description="r", done_definition="d")

        with patch("src.agents.launch_pipeline.is_cli_tool_available", return_value=True):
            phase_config = launch_pipeline._launch._resolve_phase_config(
                task, cli_type=None, phase_cli_tool=None, phase_cli_model=None,
                phase_glm_token_env=None, phase_thinking_level=None,
            )

        assert phase_config.fallback_cli_tool == "pi"
        assert phase_config.fallback_cli_model == "some-model"

    def test_disables_phase_level_fallback_when_not_on_path(self, tmp_path):
        """A Phase row's own fallback_cli_tool (not the global config one)
        must be validated the same way."""
        from src.agents.manager import AgentManager

        db_path = tmp_path / "test.db"
        db = DatabaseManager(str(db_path))
        db.create_tables()
        launch_pipeline = AgentManager(db_manager=db, llm_provider=Mock())
        launch_pipeline.config.agents.default_fallback_cli_tool = None
        launch_pipeline.config.agents.default_cli_tool = "claude"

        with db.session_scope() as session:
            session.add(Workflow(id="wf1", name="W", status="active", phases_folder_path="/tmp"))
            session.add(Phase(
                id="ph1", workflow_id="wf1", name="dev", order=1,
                description="d", done_definitions=["d"],
                cli_tool="claude", fallback_cli_tool="pi", fallback_cli_model="qwen",
            ))
            session.add(Task(
                id="t3", workflow_id="wf1", phase_id="ph1",
                raw_description="r", enriched_description="r", done_definition="d",
            ))

        with db.session_scope() as session:
            task = session.query(Task).filter_by(id="t3").first()

            with patch("src.agents.launch_pipeline.is_cli_tool_available", return_value=False):
                phase_config = launch_pipeline._launch._resolve_phase_config(
                    task, cli_type=None, phase_cli_tool=None, phase_cli_model=None,
                    phase_glm_token_env=None, phase_thinking_level=None,
                )

        assert phase_config.fallback_cli_tool is None
        assert phase_config.fallback_cli_model is None


# ── LaunchPipeline.restart_agent re-resolution ──────────────────────────

@pytest.fixture
def restart_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def restart_manager(restart_db):
    from src.agents.manager import AgentManager

    manager = AgentManager(
        db_manager=restart_db,
        llm_provider=MagicMock(),
        phase_manager=MagicMock(),
        tmux_server=MagicMock(),
    )
    return manager


def _seed_restart_agent(db_manager, *, cli_type="brokencli", cli_model="m", restart_count=0):
    with db_manager.session_scope() as session:
        session.add(Workflow(id="wf-fb", name="W", status="active", phases_folder_path="/tmp"))
        session.add(Task(
            id="task-fb", workflow_id="wf-fb", raw_description="r",
            enriched_description="r", done_definition="d", status="in_progress",
        ))
        session.add(Agent(
            id="agent-fb", system_prompt="p", status="stuck",
            cli_type=cli_type, cli_model=cli_model, agent_type="phase",
            current_task_id="task-fb", restart_count=restart_count,
            tmux_session_name="old-session",
        ))
    return "agent-fb"


def _mock_cli_agent():
    mock_cli = MagicMock()
    mock_cli.get_launch_command.return_value = LaunchResult("cmd", LaunchResult.FLAG)
    mock_cli.default_model = "cli-default-model"
    mock_cli.post_launch_confirmation_keys.return_value = []
    return mock_cli


def _wire_prompt_delivery_mocks(restart_manager):
    """Stub out the post-launch prompt-delivery chain so restart_agent runs
    to completion without touching a real tmux pane."""
    restart_manager._launch._send_initial_prompt_with_retry = AsyncMock()
    restart_manager._launch._record_cli_session = AsyncMock()
    restart_manager._launch._verify_instructions_file_read = AsyncMock()
    restart_manager._send_goal_command = AsyncMock()


class TestRestartAgentReResolvesFallbackCli:
    @pytest.mark.asyncio
    async def test_switches_to_available_default_fallback_when_stored_cli_type_is_broken(
        self, restart_manager, restart_db
    ):
        """agent.cli_type ('brokencli') isn't on PATH; the configured
        default_fallback_cli_tool ('claude') is -- restart_agent must switch
        to it and persist the change, not keep relaunching under 'brokencli'."""
        agent_id = _seed_restart_agent(restart_db, cli_type="brokencli")
        restart_manager.config.agents.default_fallback_cli_tool = "claude"
        restart_manager.config.agents.default_fallback_cli_model = "sonnet"
        restart_manager.tmux_server.has_session.return_value = True
        mock_cli = _mock_cli_agent()

        with patch("src.agents.launch_pipeline.get_cli_agent", return_value=mock_cli), \
             patch("src.agents.launch_pipeline.asyncio.sleep"), \
             patch("src.agents.launch_pipeline.is_cli_tool_available", side_effect=lambda tool: tool == "claude"):
            _wire_prompt_delivery_mocks(restart_manager)
            await restart_manager.restart_agent(agent_id, "Test")

        with restart_db.session_scope() as session:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            assert agent.cli_type == "claude"
            assert agent.cli_model == "sonnet"

        _, call_kwargs = mock_cli.get_launch_command.call_args
        assert call_kwargs["model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_falls_back_to_default_cli_tool_when_fallback_also_unavailable(
        self, restart_manager, restart_db
    ):
        """Neither the stored cli_type nor the configured fallback are
        installed -- restart_agent must still try config.agents.
        default_cli_tool before giving up entirely."""
        agent_id = _seed_restart_agent(restart_db, cli_type="brokencli")
        restart_manager.config.agents.default_fallback_cli_tool = "alsobroken"
        restart_manager.config.agents.default_fallback_cli_model = None
        restart_manager.config.agents.default_cli_tool = "claude"
        restart_manager.config.agents.cli_model = "sonnet"
        restart_manager.tmux_server.has_session.return_value = True
        mock_cli = _mock_cli_agent()

        with patch("src.agents.launch_pipeline.get_cli_agent", return_value=mock_cli), \
             patch("src.agents.launch_pipeline.asyncio.sleep"), \
             patch("src.agents.launch_pipeline.is_cli_tool_available", side_effect=lambda tool: tool == "claude"):
            _wire_prompt_delivery_mocks(restart_manager)
            await restart_manager.restart_agent(agent_id, "Test")

        with restart_db.session_scope() as session:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            assert agent.cli_type == "claude"

    @pytest.mark.asyncio
    async def test_fails_task_cleanly_when_no_cli_is_available(self, restart_manager, restart_db):
        """Stored cli_type, configured fallback, AND the global default are
        all unavailable -- restart_agent must fail the task visibly instead
        of relaunching into another dead pane."""
        agent_id = _seed_restart_agent(restart_db, cli_type="brokencli")
        restart_manager.config.agents.default_fallback_cli_tool = "alsobroken"
        restart_manager.config.agents.default_fallback_cli_model = None
        restart_manager.config.agents.default_cli_tool = "brokencli"  # same as stored -- no real candidate
        restart_manager.tmux_server.has_session.return_value = True
        mock_cli = _mock_cli_agent()

        with patch("src.agents.launch_pipeline.get_cli_agent", return_value=mock_cli), \
             patch("src.agents.launch_pipeline.asyncio.sleep"), \
             patch("src.agents.launch_pipeline.is_cli_tool_available", return_value=False):
            await restart_manager.restart_agent(agent_id, "Test")

        # No launch was attempted.
        mock_cli.get_launch_command.assert_not_called()

        with restart_db.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fb").first()
            agent = session.query(Agent).filter_by(id=agent_id).first()
            assert task.status == "failed"
            assert "not installed" in task.failure_reason
            # cli_type left untouched -- nothing was actually switched to.
            assert agent.cli_type == "brokencli"

    @pytest.mark.asyncio
    async def test_unchanged_cli_type_when_stored_value_is_already_installed(
        self, restart_manager, restart_db
    ):
        """The overwhelming majority case: agent.cli_type is fine. Restart
        must behave exactly as before -- no re-resolution, no config lookup
        beyond what create-time already did."""
        agent_id = _seed_restart_agent(restart_db, cli_type="claude", cli_model="sonnet")
        restart_manager.config.agents.default_fallback_cli_tool = "pi"
        restart_manager.tmux_server.has_session.return_value = True
        mock_cli = _mock_cli_agent()

        with patch("src.agents.launch_pipeline.get_cli_agent", return_value=mock_cli), \
             patch("src.agents.launch_pipeline.asyncio.sleep"), \
             patch("src.agents.launch_pipeline.is_cli_tool_available", return_value=True):
            _wire_prompt_delivery_mocks(restart_manager)
            await restart_manager.restart_agent(agent_id, "Test")

        with restart_db.session_scope() as session:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            assert agent.cli_type == "claude"
            assert agent.cli_model == "sonnet"

    @pytest.mark.asyncio
    async def test_second_restart_attempt_picks_up_a_config_fix_the_first_did_not_have(
        self, restart_manager, restart_db
    ):
        """The exact scenario the fix targets: a first restart attempt has
        no usable CLI and fails cleanly (agent.cli_type left untouched); the
        operator then fixes the config; a SECOND restart attempt against the
        SAME agent must pick up the new config value, not replay the first
        attempt's stale failure."""
        agent_id = _seed_restart_agent(restart_db, cli_type="brokencli", restart_count=0)
        restart_manager.config.agents.default_fallback_cli_tool = "alsobroken"
        restart_manager.config.agents.default_fallback_cli_model = None
        restart_manager.config.agents.default_cli_tool = "brokencli"
        restart_manager.tmux_server.has_session.return_value = True

        # -- Attempt 1: nothing usable, fails cleanly --
        with patch("src.agents.launch_pipeline.get_cli_agent", return_value=_mock_cli_agent()), \
             patch("src.agents.launch_pipeline.asyncio.sleep"), \
             patch("src.agents.launch_pipeline.is_cli_tool_available", return_value=False):
            await restart_manager.restart_agent(agent_id, "Attempt 1")

        with restart_db.session_scope() as session:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            assert agent.cli_type == "brokencli"
            task = session.query(Task).filter_by(id="task-fb").first()
            assert task.status == "failed"

        # -- Operator fixes the config: a fallback is now installed --
        restart_manager.config.agents.default_fallback_cli_tool = "workingfallback"
        restart_manager.config.agents.default_fallback_cli_model = "new-model"

        # -- Attempt 2: picks up the fixed config --
        with patch("src.agents.launch_pipeline.get_cli_agent", return_value=_mock_cli_agent()), \
             patch("src.agents.launch_pipeline.asyncio.sleep"), \
             patch("src.agents.launch_pipeline.is_cli_tool_available", side_effect=lambda tool: tool == "workingfallback"):
            _wire_prompt_delivery_mocks(restart_manager)
            await restart_manager.restart_agent(agent_id, "Attempt 2")

        with restart_db.session_scope() as session:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            assert agent.cli_type == "workingfallback", (
                "second restart attempt must re-resolve from the NOW-fixed "
                "config, not keep reusing the still-broken stored cli_type"
            )
            assert agent.cli_model == "new-model"


# ── MechanicalRecoveryDetector._resolve_fallback_cli ────────────────────

class TestMechanicalRecoveryResolveFallbackCli:
    def _detector(self, db_manager):
        from src.monitoring.mechanical_recovery import MechanicalRecoveryDetector

        return MechanicalRecoveryDetector(
            db_manager=db_manager,
            agent_manager=MagicMock(),
            config=MagicMock(),
            auto_restart=MagicMock(),
        )

    def test_skips_global_fallback_not_on_path(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = DatabaseManager(str(db_path))
        db.create_tables()
        detector = self._detector(db)

        with db.session_scope() as session:
            session.add(Workflow(id="wf-m", name="W", status="active", phases_folder_path="/tmp"))
            session.add(Task(
                id="task-m", workflow_id="wf-m", raw_description="r",
                enriched_description="r", done_definition="d", status="in_progress",
            ))

        agent = Mock(cli_type="claude", cli_model="sonnet")

        with db.session_scope() as session:
            stuck_task = session.query(Task).filter_by(id="task-m").first()
            with patch("src.monitoring.mechanical_recovery.get_config") as mock_cfg, \
                 patch("src.interfaces.cli_interface.is_cli_tool_available", return_value=False):
                mock_cfg.return_value = Mock(agents=Mock(
                    default_fallback_cli_tool="pi",
                    default_fallback_cli_model="qwen",
                ))
                fallback_tool, fallback_model = detector._resolve_fallback_cli(session, agent, stuck_task)

        assert fallback_tool is None
        assert fallback_model is None

    def test_returns_global_fallback_when_available(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = DatabaseManager(str(db_path))
        db.create_tables()
        detector = self._detector(db)

        with db.session_scope() as session:
            session.add(Workflow(id="wf-m2", name="W", status="active", phases_folder_path="/tmp"))
            session.add(Task(
                id="task-m2", workflow_id="wf-m2", raw_description="r",
                enriched_description="r", done_definition="d", status="in_progress",
            ))

        agent = Mock(cli_type="claude", cli_model="sonnet")

        with db.session_scope() as session:
            stuck_task = session.query(Task).filter_by(id="task-m2").first()
            with patch("src.monitoring.mechanical_recovery.get_config") as mock_cfg, \
                 patch("src.interfaces.cli_interface.is_cli_tool_available", return_value=True):
                mock_cfg.return_value = Mock(agents=Mock(
                    default_fallback_cli_tool="pi",
                    default_fallback_cli_model="qwen",
                ))
                fallback_tool, fallback_model = detector._resolve_fallback_cli(session, agent, stuck_task)

        assert fallback_tool == "pi"
        assert fallback_model == "qwen"
