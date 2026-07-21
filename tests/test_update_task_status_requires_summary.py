"""Regression: update_task_status (and complete_my_task, which posts to the
same endpoint) must reject status='done' with no summary instead of silently
storing empty completion_notes.

Previously an empty summary fell through to a file-scraping fallback
(_summary_from_output_artifact) that had never actually worked -- it used
`Path` without importing it, so every real call hit a silently-swallowed
NameError and returned "". Removed in favor of just requiring the agent
provide a real summary at the point it already has the content in hand.
"""

import os
import tempfile
import uuid

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
            order=1,
            name="security_review",
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


class TestUpdateTaskStatusRequiresSummary:
    def test_done_with_empty_summary_is_rejected(self, test_db, test_client, tmp_path):
        task_id, agent_id = _seed(test_db, tmp_path)

        resp = test_client.post(
            "/update_task_status",
            json={"task_id": task_id, "status": "done", "summary": "", "key_learnings": []},
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 400
        assert "summary is required" in resp.json()["detail"]

        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "in_progress"  # untouched, not silently marked done

    def test_done_with_whitespace_only_summary_is_rejected(
        self, test_db, test_client, tmp_path
    ):
        task_id, agent_id = _seed(test_db, tmp_path)

        resp = test_client.post(
            "/update_task_status",
            json={"task_id": task_id, "status": "done", "summary": "   ", "key_learnings": []},
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 400

    def test_failed_with_empty_summary_is_allowed(self, test_db, test_client, tmp_path):
        """The requirement is specific to status='done' -- failure_reason
        already carries the real information for a failed task."""
        task_id, agent_id = _seed(test_db, tmp_path)

        resp = test_client.post(
            "/update_task_status",
            json={
                "task_id": task_id,
                "status": "failed",
                "summary": "",
                "failure_reason": "hit an unrecoverable error",
                "key_learnings": [],
            },
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 200, resp.text
