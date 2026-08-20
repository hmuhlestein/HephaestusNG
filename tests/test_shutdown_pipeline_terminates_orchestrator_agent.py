"""Regression test: _shutdown_pipeline must terminate the orchestrator's
own self-registered Agent row through terminate_agent_direct (the
three-field invariant: status="terminated", current_task_id=None,
terminated_at set), not via a raw _update_orchestrator_status("terminated")
status-only write.

terminate_agent()'s own docstring: "Every raw agent.status = 'terminated'
write site must call this instead of hand-rolling the invariant -- the bug
class it closes has independently recurred eight times in this codebase's
history." This was the 9th instance, for the orchestrator's own agent row
specifically.
"""

from pathlib import Path

import pytest

from src.autopilot import orchestrator
from src.autopilot.orchestrator.state import PersistentPipelineState, PipelineState


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def test_shutdown_pipeline_terminates_orchestrator_agent(db_manager, tmp_path, monkeypatch):
    from src.core.database import Agent

    with db_manager.session_scope() as session:
        session.add(Agent(
            id="orch-agent-1", system_prompt="orchestrator", status="working",
            cli_type="claude", agent_type="orchestrator", current_task_id="stray-task-1",
        ))

    monkeypatch.setattr(orchestrator, "_orchestrator_agent_id", "orch-agent-1")
    monkeypatch.setattr(orchestrator, "get_active_workflows", lambda *a, **k: [])

    log_dir = tmp_path / "logs"
    logger = orchestrator.OrchestratorLogger(log_dir)
    state = PipelineState()
    persistent_state = PersistentPipelineState(project_id="proj-1")

    orchestrator._shutdown_pipeline(
        sdk=None,
        state=state,
        persistent_state=persistent_state,
        processed_hashes=set(),
        project_path=Path(tmp_path / "proj"),
        current_project_id="proj-1",
        log_dir=log_dir,
        logger=logger,
    )

    with db_manager.session_scope() as session:
        agent = session.query(Agent).filter_by(id="orch-agent-1").first()
        assert agent.status == "terminated"
        assert agent.current_task_id is None
        assert agent.terminated_at is not None
