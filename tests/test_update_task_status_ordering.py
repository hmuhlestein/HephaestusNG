"""Regression test: the worktree commit must happen before the spec gate
fires, not after.

Found live: fire_spec_gate_if_ready ran before commit_and_link_ticket. A
goto decision deletes the gate phase's result files (consume_gate_artifacts,
src/autopilot/spec.py) so a later re-run can't re-score stale ones -- but
with the gate firing first, it deleted a report the agent had just written
in the SAME request, before that report was ever captured in a git commit.
The file (and any findings beyond what's threaded into the corrective
task's description) would be lost outright instead of preserved in history.
"""

import os
import tempfile
import uuid
from unittest.mock import AsyncMock, patch

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
    import src.mcp.server as server_module

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


class TestSpecGateFiresAfterWorktreeCommit:
    def test_commit_precedes_gate_firing(self, test_db, test_client, tmp_path):
        call_order = []

        async def record_commit(*a, **kw):
            call_order.append("commit")
            return "deadbeef"

        async def record_gate(*a, **kw):
            call_order.append("gate")

        task_id, agent_id = _seed(test_db, tmp_path)

        with patch(
            "src.services.task_completion_service.TaskCompletionService.commit_and_link_ticket",
            side_effect=record_commit,
        ), patch(
            "src.services.task_completion_service.TaskCompletionService.fire_spec_gate_if_ready",
            side_effect=record_gate,
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
        assert call_order == ["commit", "gate"], (
            "commit_and_link_ticket must run before fire_spec_gate_if_ready, "
            "or a goto's consume_gate_artifacts can delete a report before "
            "it's ever captured in git history"
        )
