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

        # Regression: the rejection reason must be persisted on the task,
        # not just returned in the response -- if this agent's session ends
        # before it retries, _clean_stale_assigned_tasks/_maybe_retry_failed_
        # tasks need it to give the next agent specific feedback instead of
        # a generic "agent terminated" message with no memory of why.
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.failure_reason is not None
        assert "features.json" in task.failure_reason

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

    def test_metadata_field_accepted_and_folded_into_summary(
        self, test_db, test_client, tmp_path
    ):
        """Regression: agents naturally want to attach structured verdict
        data (e.g. a scope-review gate's PASS/FAIL + issue counts) to a
        status update. Observed live via the update_task_status MCP tool:
        an agent sent {"metadata": {"verdict": "FAIL", "issues_count": 3}}
        and got "Additional properties are not allowed ('metadata' was
        unexpected)" -- the tool's declared schema didn't have a metadata
        property. There's no dedicated column for this, so the field is
        accepted and folded into the summary text instead of hard-rejected.
        """
        import json

        task_id, agent_id = _seed(test_db, tmp_path, json.dumps([]))

        resp = test_client.post(
            "/update_task_status",
            json={
                "task_id": task_id,
                "status": "done",
                "summary": "Scope review FAILED",
                "key_learnings": [],
                "metadata": {"verdict": "FAIL", "issues_count": 3},
            },
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True

        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert "Scope review FAILED" in task.completion_notes
        assert '"verdict": "FAIL"' in task.completion_notes


def _seed_qa_validation(test_db, tmp_path):
    import json

    session = test_db.get_session()
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    phase_id = f"phase-{uuid.uuid4().hex[:8]}"
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"

    session.add(
        Workflow(
            id=workflow_id, name="t", phases_folder_path="/tmp",
            status="active", working_directory=str(tmp_path),
        )
    )
    session.add(
        Phase(
            id=phase_id, workflow_id=workflow_id, order=8,
            name="qa_validation", description="d", done_definitions=["done"],
            outputs=json.dumps(["qa.md"]),
        )
    )
    session.add(
        Agent(id=agent_id, system_prompt="p", status="working", cli_type="claude", agent_type="phase")
    )
    session.add(
        Task(
            id=task_id, raw_description="raw", done_definition="done",
            status="in_progress", workflow_id=workflow_id, phase_id=phase_id,
            assigned_agent_id=agent_id,
        )
    )
    session.commit()
    return task_id, agent_id


class TestGateResultSchemaFloor:
    """End-to-end regression for the live incident: a QA agent wrote
    docs/qa.md in its own nested shape ({"overall_status": ...,
    "test_results": {"main_suite": {...}}}) instead of the documented flat
    schema. The declared-output floor passed (the file existed) but
    score_qa's field reads all silently defaulted to "everything passed"
    -- this floor catches the shape mismatch at completion time instead."""

    def test_incompatible_qa_shape_is_rejected_not_500(self, test_db, test_client, tmp_path):
        task_id, agent_id = _seed_qa_validation(test_db, tmp_path)
        docs = tmp_path / ".hephaestus" / "qa_validation"
        docs.mkdir(parents=True)
        (docs / "qa.md").write_text(
            "---\n"
            "overall_status: PASS\n"
            "test_results:\n"
            "  main_suite:\n"
            "    total: 1410\n"
            "    passed: 1410\n"
            "---\n\n# QA Report"
        )

        resp = test_client.post(
            "/update_task_status",
            json={"task_id": task_id, "status": "done", "summary": "done", "key_learnings": []},
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is False
        assert "qa_validation" in body["message"]

        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.failure_reason is not None
        assert "qa_validation" in task.failure_reason

    def test_documented_qa_shape_still_succeeds(self, test_db, test_client, tmp_path):
        task_id, agent_id = _seed_qa_validation(test_db, tmp_path)
        docs = tmp_path / ".hephaestus" / "qa_validation"
        docs.mkdir(parents=True)
        (docs / "qa.md").write_text(
            "---\n"
            "type: qa_validation\n"
            "failed_tests: 0\n"
            "passed_tests: 1410\n"
            "critical_issues: 0\n"
            "---\n\n# QA Report"
        )

        resp = test_client.post(
            "/update_task_status",
            json={"task_id": task_id, "status": "done", "summary": "done", "key_learnings": []},
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True
