"""SOLID review 1.10: surveying the ~dozen hand-rolled Task/Agent
serialization sites for the proposed TaskSerializer/AgentSerializer found
the duplication itself wasn't the live problem -- inconsistent adoption of
the ALREADY-existing resolve_task_phase() helper was. These are real-DB
regression tests for the sites that were bypassing it (or missing the "Z"
UTC suffix, or reading the wrong source field), none of which had any
prior coverage.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.core.database import Agent, DatabaseManager, Phase, Task, Workflow
from src.mcp.frontend.agent_service import AgentService
from src.mcp.frontend.task_service import TaskService


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def frontend_api(db_manager):
    return TaskService(db_manager=db_manager, agent_manager=None)


@pytest.fixture
def agent_service_instance(db_manager):
    return AgentService(db_manager=db_manager, agent_manager=None)


def _seed_workflow_and_phase(db_manager, workflow_id="wf-1", phase_id="phase-1"):
    with db_manager.session_scope() as session:
        session.add(
            Workflow(
                id=workflow_id,
                name="wf",
                phases_folder_path="/tmp",
                definition_id="autopilot",
            )
        )
        session.add(
            Phase(
                id=phase_id,
                workflow_id=workflow_id,
                name="development",
                order=2,
                description="d",
                done_definitions=["x"],
            )
        )


@pytest.mark.asyncio
async def test_get_task_resolves_phase_name_and_order(db_manager, frontend_api):
    _seed_workflow_and_phase(db_manager)
    with db_manager.session_scope() as session:
        session.add(
            Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="pending",
            )
        )

    result = await frontend_api.get_task("task-1")

    # Before the fix, get_task() never called resolve_task_phase and
    # always returned phase_name/phase_order as None regardless of
    # whether the task had a real phase_id.
    assert result["phase_name"] == "development"
    assert result["phase_order"] == 2


@pytest.mark.asyncio
async def test_get_phase_agents_started_at_prefers_launched_at(db_manager, agent_service_instance):
    _seed_workflow_and_phase(db_manager)
    launched = datetime.utcnow() - timedelta(minutes=2)
    created = datetime.utcnow() - timedelta(minutes=10)
    with db_manager.session_scope() as session:
        session.add(
            Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="in_progress",
            )
        )
        session.add(
            Agent(
                id="agent-1",
                system_prompt="test",
                cli_type="claude",
                status="working",
                current_task_id="task-1",
                created_at=created,
                launched_at=launched,
            )
        )

    result = await agent_service_instance.get_phase_agents("phase-1")

    # Before the fix this always read agent.created_at under the key
    # "started_at" -- launched_at (the field that actually means "when
    # this agent's CLI session started") was ignored entirely.
    assert result["agents"][0]["started_at"] == launched.isoformat() + "Z"


@pytest.mark.asyncio
async def test_get_phase_agents_started_at_falls_back_to_created_at(db_manager, agent_service_instance):
    _seed_workflow_and_phase(db_manager)
    created = datetime.utcnow() - timedelta(minutes=10)
    with db_manager.session_scope() as session:
        session.add(
            Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="in_progress",
            )
        )
        session.add(
            Agent(
                id="agent-1",
                system_prompt="test",
                cli_type="claude",
                status="working",
                current_task_id="task-1",
                created_at=created,
                launched_at=None,
            )
        )

    result = await agent_service_instance.get_phase_agents("phase-1")

    assert result["agents"][0]["started_at"] == created.isoformat() + "Z"


@pytest.mark.asyncio
async def test_get_task_progress_single_task_resolves_phase(db_manager):
    """agents_api.get_task_progress's single-task branch (task_id param
    given) used to bypass resolve_task_phase entirely, doing its own raw
    Phase.filter_by(id=task.phase_id) -- inconsistent with the multi-task
    branch in this same function, which already used resolve_task_phase
    correctly."""
    from src.mcp.agents_api import get_task_progress

    _seed_workflow_and_phase(db_manager)
    with db_manager.session_scope() as session:
        session.add(
            Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="in_progress",
            )
        )

    state = SimpleNamespace(db_manager=db_manager)
    with patch("src.mcp.agents_api._get_server_state", return_value=state):
        result = await get_task_progress(task_id="task-1", requesting_agent_id="a1")

    assert result["phase_name"] == "development"
    assert result["phase_order"] == 2


@pytest.mark.asyncio
async def test_mcp_tool_registry_task_status_resolves_phase_and_z_suffix(db_manager):
    """_tool_get_task_status (the MCP tool backing task-status/ticket
    listing) did its own raw Phase.filter_by lookup (no digit/order or
    workflow scoping, unlike resolve_task_phase) and omitted the "Z" UTC
    suffix every other timestamp in this codebase's responses uses."""
    from src.mcp.server import _mcp_tool_registry

    _seed_workflow_and_phase(db_manager)
    created = datetime.utcnow() - timedelta(minutes=3)
    with db_manager.session_scope() as session:
        session.add(
            Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="pending",
                created_at=created,
            )
        )

    with patch.object(_mcp_tool_registry.server_state, "db_manager", db_manager):
        result = await _mcp_tool_registry._tool_get_task_status({})

    task_result = result["tasks"][0]
    assert task_result["phase_name"] == "development"
    assert task_result["created_at"] == created.isoformat() + "Z"
