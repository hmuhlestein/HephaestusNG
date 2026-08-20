"""Regression test: verify_output_survived_commit must be offloaded to the
executor, not called directly on the event loop.

It runs right after commit_and_link_ticket on every "done" completion
(update_task_status's hottest call site), and its fallback path does real
GitPython history search (repo.iter_commits) when a declared output isn't
found directly in the worktree -- blocking, same class of issue
collect_cost_on_completion (right above it in the same function) was
already fixed for. complete_task_as_user (task_admin_routes.py) has the
identical call and needed the identical fix.
"""

import functools
import os
import tempfile
import uuid
from datetime import datetime
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
    return TestClient(app)


def _seed(test_db, tmp_path):
    session = test_db.get_session()
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    phase_id = f"phase-{uuid.uuid4().hex[:8]}"
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"

    session.add(
        Workflow(
            id=workflow_id, name="t", phases_folder_path="/tmp", status="active",
            working_directory=str(tmp_path),
        )
    )
    session.add(
        Phase(
            id=phase_id, workflow_id=workflow_id, order=6, name="adversarial_review",
            description="d", done_definitions=["done"],
        )
    )
    session.add(
        Agent(id=agent_id, system_prompt="p", status="working", cli_type="claude", agent_type="phase")
    )
    session.add(
        Task(
            id=task_id, raw_description="raw", done_definition="done", status="in_progress",
            workflow_id=workflow_id, phase_id=phase_id, assigned_agent_id=agent_id,
        )
    )
    session.commit()
    return task_id, agent_id


def _fake_offloading_loop():
    """A get_event_loop() stand-in whose run_in_executor is an AsyncMock,
    so every offloaded call in the request resolves immediately instead of
    actually running -- we only care whether verify_output_survived_commit
    was one of the calls routed through it."""
    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=None)
    return fake_loop


def _find_verify_call(run_in_executor_mock):
    for call in run_in_executor_mock.call_args_list:
        for arg in call.args:
            if isinstance(arg, functools.partial) and arg.func.__name__ == "verify_output_survived_commit":
                return arg
    return None


def test_update_task_status_offloads_verify_output_survived_commit(test_db, test_client, tmp_path):
    task_id, agent_id = _seed(test_db, tmp_path)
    fake_loop = _fake_offloading_loop()

    with (
        patch("asyncio.get_event_loop", return_value=fake_loop),
        patch(
            "src.services.task_completion_service.TaskCompletionService.commit_and_link_ticket",
            new=AsyncMock(return_value="deadbeef"),
        ),
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
    partial_arg = _find_verify_call(fake_loop.run_in_executor)
    assert partial_arg is not None, "verify_output_survived_commit must go through run_in_executor"


def test_complete_task_as_user_offloads_verify_output_survived_commit(test_db, test_client, tmp_path):
    task_id, agent_id = _seed(test_db, tmp_path)
    session = test_db.get_session()
    session.query(Task).filter_by(id=task_id).update({"status": "failed"})
    session.commit()
    fake_loop = _fake_offloading_loop()

    with (
        patch("asyncio.get_event_loop", return_value=fake_loop),
        patch(
            "src.services.task_completion_service.TaskCompletionService.commit_and_link_ticket",
            new=AsyncMock(return_value="deadbeef"),
        ),
        patch(
            "src.services.task_completion_service.TaskCompletionService.verify_output_artifact",
            return_value=None,
        ),
        patch(
            "src.services.task_completion_service.TaskCompletionService.verify_gate_result_schema",
            return_value=None,
        ),
        patch(
            "src.services.task_completion_service.TaskCompletionService.verify_no_open_tickets",
            return_value=None,
        ),
    ):
        resp = test_client.post(
            f"/api/tasks/{task_id}/complete",
            json={"summary": "manually completed"},
        )

    assert resp.status_code == 200, resp.text
    partial_arg = _find_verify_call(fake_loop.run_in_executor)
    assert partial_arg is not None, "verify_output_survived_commit must go through run_in_executor"
