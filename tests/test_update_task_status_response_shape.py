"""Regression test: /update_task_status must always return a body matching
UpdateTaskStatusResponse, even when TaskCompletionService.verify_output_artifact
rejects the completion.

Found live during a smoke run: the route returned
verify_output_artifact's plain {"status": "failed", "message": ...} dict
directly, which doesn't have the response_model's required 'success'/
'termination_scheduled' fields. FastAPI's response validation then raised
a 500 ResponseValidationError -- the agent never saw the actual "missing
features.json" message, just a generic Internal Server Error, and kept
blindly retrying update_task_status instead of investigating.
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


def _seed(test_db, tmp_path, outputs):
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
            name="Feature Architect",
            description="d",
            done_definitions=["done"],
            outputs=outputs,
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


class TestUpdateTaskStatusResponseShape:
    def test_output_artifact_rejection_returns_valid_response_not_500(
        self, test_db, test_client, tmp_path
    ):
        import json

        task_id, agent_id = _seed(test_db, tmp_path, json.dumps(["features.json"]))
        # features.json deliberately not written to tmp_path

        resp = test_client.post(
            "/update_task_status",
            json={
                "task_id": task_id,
                "status": "done",
                "summary": "done",
                "key_learnings": [],
            },
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is False
        assert "features.json" in body["message"]
        assert body["termination_scheduled"] is False

    def test_output_artifact_present_still_succeeds(self, test_db, test_client, tmp_path):
        import json

        task_id, agent_id = _seed(test_db, tmp_path, json.dumps(["features.json"]))
        (tmp_path / "features.json").write_text("{}")

        resp = test_client.post(
            "/update_task_status",
            json={
                "task_id": task_id,
                "status": "done",
                "summary": "done",
                "key_learnings": [],
            },
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True
