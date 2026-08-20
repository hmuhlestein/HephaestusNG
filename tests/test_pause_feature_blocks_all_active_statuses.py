"""Regression test: POST /features/{id}/pause must block every non-terminal
task, not just the plainly-active ones.

Found on gap-check sweep: the endpoint's own comment states the intent --
"mark every not-yet-done task 'blocked' so the UI reflects the pause and the
orchestrator will not advance them until resume" -- but its status filter
only covered pending/queued/assigned/in_progress, missing under_review,
validation_in_progress, and needs_work. A task mid-validation (or awaiting
rework after a validator's feedback) when the user hits Pause kept running
unblocked, contradicting the endpoint's own stated contract.
"""

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import Agent, DatabaseManager, Feature, Task, Workflow


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()
    return db_manager


@pytest.fixture
def client(test_db):
    from src.mcp.autopilot import router
    from src.mcp.autopilot import _shared as api_mod

    api_mod._cache.clear()
    app = FastAPI()
    app.include_router(router)
    yield TestClient(app, headers={"X-Agent-ID": "system"})
    api_mod._cache.clear()


@pytest.mark.parametrize("status", ["under_review", "validation_in_progress", "needs_work"])
def test_pause_blocks_task_in_review_or_validation_status(status, test_db, client):
    session = test_db.get_session()
    session.add(
        Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
    )
    session.add(
        Feature(
            id="feat-1", design_id="des-1", feature_key="test-feature", name="Test",
            scope="Build it", workflow_id="wf-1", status="active",
        )
    )
    session.add(
        Agent(id="agent-1", system_prompt="p", status="working", cli_type="pi")
    )
    session.add(
        Task(
            id="task-1", workflow_id="wf-1", raw_description="r", done_definition="d",
            status=status, assigned_agent_id="agent-1",
        )
    )
    session.commit()
    session.close()

    resp = client.post("/api/autopilot/features/feat-1/pause")
    assert resp.status_code == 200, resp.text

    session = test_db.get_session()
    task = session.query(Task).filter_by(id="task-1").first()
    assert task.status == "blocked", (
        f"a task in '{status}' must be blocked on pause, matching this "
        "endpoint's own stated contract of blocking every not-yet-done task"
    )
    session.close()
