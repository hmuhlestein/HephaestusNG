"""Regression test: a failure in post-completion orchestration (spec-gate
firing, broadcasting) must not turn a genuinely successful task
completion into a reported failure, and must not lose the agent
termination already queued on background_tasks.

Found while closing the "Connection closed" complete_my_task race
(agent_task_routes.py's update_task_status): after switching agent
termination to FastAPI's BackgroundTasks (guaranteed to run only after
the response is sent, instead of a fire-and-forget asyncio.create_task
with no such guarantee), a NEW gap surfaced. Confirmed empirically: if
anything after _complete_task_normally raises an exception that isn't
caught inside update_task_status itself, FastAPI's default exception
handling builds a fresh error response with no `background` attached --
so the already-queued termination task is silently discarded and NEVER
runs, leaving the agent alive as a zombie forever. Worse, a client retry
hits the idempotency short-circuit (task.status already terminal) and
returns immediately WITHOUT ever calling _complete_task_normally again,
so there is no second chance to re-queue termination. The agent would
also see a misleading 500 for a task that had already durably completed.

update_task_status now wraps _maybe_fire_spec_gate and
_broadcast_task_completion in their own try/except (letting
OperationalError through to preserve the existing lock-retry behavior),
so any other exception from that follow-up work is logged but never
threatens the already-committed completion or its queued termination.
"""

import os
import tempfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.core.database import Agent, DatabaseManager, Phase, Task, Workflow
from src.mcp.server import app


@pytest.fixture
def test_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    yield db_manager

    os.unlink(db_path)


@pytest.fixture
def test_client(test_db, monkeypatch):
    import src.mcp.server._shared as server_module

    monkeypatch.setattr(server_module.server_state, "db_manager", test_db)
    monkeypatch.setattr(server_module.server_state, "initialized", True, raising=False)
    mock_agent_manager = MagicMock(terminate_agent=AsyncMock(return_value=None))
    monkeypatch.setattr(server_module.server_state, "agent_manager", mock_agent_manager)
    client = TestClient(app)
    client._mock_agent_manager = mock_agent_manager
    return client


def _seed(test_db, tmp_path):
    session = test_db.get_session()
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    phase_id = f"phase-{uuid.uuid4().hex[:8]}"
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"

    session.add(
        Workflow(
            id=workflow_id,
            name="t",
            phases_folder_path="/tmp",
            status="active",
            working_directory=str(tmp_path),
        )
    )
    session.add(
        Phase(
            id=phase_id,
            workflow_id=workflow_id,
            order=6,
            name="adversarial_review",
            description="d",
            done_definitions=["done"],
        )
    )
    session.add(
        Agent(
            id=agent_id,
            system_prompt="p",
            status="working",
            cli_type="claude",
            agent_type="phase",
        )
    )
    session.add(
        Task(
            id=task_id,
            raw_description="raw",
            done_definition="done",
            status="in_progress",
            workflow_id=workflow_id,
            phase_id=phase_id,
            assigned_agent_id=agent_id,
        )
    )
    session.commit()
    return task_id, agent_id


class TestTerminationSurvivesPostCompletionFailures:
    def test_spec_gate_failure_does_not_lose_termination_or_report_false_failure(
        self, test_db, test_client, tmp_path
    ):
        task_id, agent_id = _seed(test_db, tmp_path)

        with patch(
            "src.autopilot.orchestrator.phase_transitions.fire_spec_gate_if_ready",
            side_effect=RuntimeError("boom in spec gate"),
        ):
            resp = test_client.post(
                "/update_task_status",
                json={
                    "task_id": task_id,
                    "status": "done",
                    "summary": "adversarial review passed",
                    "key_learnings": [],
                },
                headers={"X-Agent-ID": agent_id},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True, (
            "a spec-gate failure must not turn an already-committed "
            "completion into a reported failure"
        )
        test_client._mock_agent_manager.terminate_agent.assert_called_once_with(agent_id)

    def test_broadcast_failure_does_not_lose_termination_or_report_false_failure(
        self, test_db, test_client, tmp_path, monkeypatch
    ):
        import src.mcp.server._shared as server_module

        task_id, agent_id = _seed(test_db, tmp_path)
        monkeypatch.setattr(
            server_module.server_state,
            "broadcast_update",
            AsyncMock(side_effect=RuntimeError("boom in broadcast")),
        )

        resp = test_client.post(
            "/update_task_status",
            json={
                "task_id": task_id,
                "status": "done",
                "summary": "adversarial review passed",
                "key_learnings": [],
            },
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True, (
            "a broadcast failure must not turn an already-committed "
            "completion into a reported failure"
        )
        test_client._mock_agent_manager.terminate_agent.assert_called_once_with(agent_id)
