"""Tests for the one-shot self-review hook in update_task_status
(docs/GAP_CHECK_SELF_LOOP_DESIGN.md).

First "done" from a phase with self_review.enabled=True defers completion,
messages the agent, and sets self_review_done=True (before responding).
Second "done" call falls through to normal completion. A phase without
self_review enabled is unaffected.
"""

import os
import tempfile
import uuid
from unittest.mock import AsyncMock, Mock, patch

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
    mock_send = AsyncMock()
    mock_agent_manager = Mock()
    mock_agent_manager.send_message_to_agent = mock_send
    mock_agent_manager.terminate_agent = AsyncMock(return_value=None)
    monkeypatch.setattr(server_module.server_state, "agent_manager", mock_agent_manager)
    client = TestClient(app)
    client._mock_send_message = mock_send
    yield client


def _seed(test_db, self_review_enabled: bool):
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
        )
    )
    session.add(
        Phase(
            id=phase_id,
            workflow_id=workflow_id,
            order=1,
            name="development",
            description="d",
            done_definitions=["done"],
            self_review={"enabled": True} if self_review_enabled else None,
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


class TestSelfReviewHook:
    def test_first_done_defers_and_messages_agent(self, test_db, test_client, caplog):
        task_id, agent_id = _seed(test_db, self_review_enabled=True)

        with caplog.at_level("INFO"):
            resp = test_client.post(
                "/update_task_status",
                json={
                    "task_id": task_id,
                    "status": "done",
                    "summary": "finished implementing",
                    "key_learnings": [],
                },
                headers={"X-Agent-ID": agent_id},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert "self-review" in body["message"].lower()

        test_client._mock_send_message.assert_called_once()
        called_agent_id = test_client._mock_send_message.call_args[0][0]
        assert called_agent_id == agent_id

        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.self_review_done is True
        assert task.status == "in_progress"  # NOT completed yet
        assert task.self_review_started_at is not None
        session.close()

        fired_logs = [r.message for r in caplog.records if "[SELF-REVIEW]" in r.message]
        assert any("fired" in m for m in fired_logs), fired_logs

    def test_second_done_completes_normally(self, test_db, test_client, caplog):
        task_id, agent_id = _seed(test_db, self_review_enabled=True)

        # First call: deferred by self-review.
        test_client.post(
            "/update_task_status",
            json={
                "task_id": task_id,
                "status": "done",
                "summary": "finished implementing",
                "key_learnings": [],
            },
            headers={"X-Agent-ID": agent_id},
        )

        # Second call: should complete normally now that self_review_done is set.
        with caplog.at_level("INFO"):
            resp = test_client.post(
                "/update_task_status",
                json={
                    "task_id": task_id,
                    "status": "done",
                    "summary": "re-checked, fixed one edge case",
                    "key_learnings": [],
                },
                headers={"X-Agent-ID": agent_id},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True

        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "done"
        # Telemetry fields cleared after logging, so this doesn't re-log later.
        assert task.self_review_started_at is None
        assert task.self_review_started_commit is None
        session.close()

        # Self-review only fires once, not on every subsequent "done".
        assert test_client._mock_send_message.call_count == 1

        completed_logs = [
            r.message for r in caplog.records if "[SELF-REVIEW]" in r.message
        ]
        assert any("completed" in m for m in completed_logs), completed_logs

    def test_phase_without_self_review_completes_immediately(
        self, test_db, test_client
    ):
        task_id, agent_id = _seed(test_db, self_review_enabled=False)

        resp = test_client.post(
            "/update_task_status",
            json={
                "task_id": task_id,
                "status": "done",
                "summary": "finished",
                "key_learnings": [],
            },
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True

        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "done"
        session.close()

        test_client._mock_send_message.assert_not_called()


class TestSelfReviewComposesWithValidatorLoop:
    """Found on adversarial review: a phase can have both self_review and
    validation_enabled True at once. Nothing but block ordering in
    update_task_status keeps them from firing on the same call -- this test
    pins down the intended composition explicitly (self-review first, then
    validation, never both on the same "done" call) so a future reordering
    of the 3a/3b/4 blocks can't silently break it without a test noticing.
    """

    def test_self_review_fires_before_validator_not_alongside_it(
        self, test_db, test_client
    ):
        session = test_db.get_session()
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        phase_id = f"phase-{uuid.uuid4().hex[:8]}"
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"

        session.add(
            Workflow(id=workflow_id, name="t", phases_folder_path="/tmp", status="active")
        )
        session.add(
            Phase(
                id=phase_id,
                workflow_id=workflow_id,
                order=1,
                name="development",
                description="d",
                done_definitions=["done"],
                self_review={"enabled": True},
            )
        )
        session.add(
            Agent(id=agent_id, system_prompt="p", status="working", cli_type="claude", agent_type="phase")
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
                validation_enabled=True,
            )
        )
        session.commit()
        session.close()

        # First "done": self-review must intercept -- task must NOT reach
        # the validator branch (status stays in_progress, not under_review).
        resp1 = test_client.post(
            "/update_task_status",
            json={"task_id": task_id, "status": "done", "summary": "first pass", "key_learnings": []},
            headers={"X-Agent-ID": agent_id},
        )
        assert resp1.status_code == 200, resp1.text
        assert "self-review" in resp1.json()["message"].lower()

        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "in_progress"
        assert task.validation_iteration == 0, "validator must not have fired on the first call"
        session.close()

        # Second "done": self_review_done is now True, so this call must reach
        # the validator branch instead of re-triggering self-review. Mock out
        # the fire-and-forget validator spawn itself: this test's minimal
        # mocks don't provide a full agent_manager/branch_manager, so an
        # unmocked spawn_validation fails asynchronously and its own error
        # path flips task.status to "failed" moments after the response
        # returns -- unrelated to (and would mask) what this test checks.
        with patch(
            "src.services.task_completion_service.TaskCompletionService.spawn_validation",
            AsyncMock(),
        ):
            resp2 = test_client.post(
                "/update_task_status",
                json={"task_id": task_id, "status": "done", "summary": "re-checked", "key_learnings": []},
                headers={"X-Agent-ID": agent_id},
            )
        assert resp2.status_code == 200, resp2.text
        assert "validation" in resp2.json()["message"].lower()

        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "under_review", "second call must reach the validator, not self-review again"
        assert task.validation_iteration == 1
        session.close()

        # Self-review's own message is sent exactly once, not again on the
        # second call.
        assert test_client._mock_send_message.call_count == 1


class TestSelfReviewConfigSurvivesRegistration:
    """Regression test for a real gap found on review: the startup
    registration path (get_all_workflow_definitions -> a hand-whitelisted
    phase_dict in server.py) drops any Phase attribute not explicitly
    listed -- this is *also* why Phase.validation has never actually fired
    for any phase despite the plumbing existing. self_review must be
    explicitly carried through every hop of this chain or development.yaml's
    `self_review: enabled: true` silently never reaches the DB.
    """

    def test_self_review_survives_yaml_loader(self):
        from pathlib import Path

        from src.workflow_engine.yaml_loader import load_full_workflow_definition

        wd = load_full_workflow_definition(
            Path(__file__).parent.parent / "config" / "workflows" / "autopilot"
        )
        dev = next(p for p in wd.phases if p.name == "development")
        assert dev.self_review == {"enabled": True}

    def test_self_review_survives_registration_whitelist(self):
        from src.workflow_registry import get_all_workflow_definitions

        all_defs = get_all_workflow_definitions()
        autopilot = next(d for d in all_defs if d.id == "autopilot")
        dev = next(p for p in autopilot.phases if p.name == "development")

        # Replicate server.py's exact registration whitelist logic (the
        # `phase_dict` block under "Register all workflow definitions").
        phase_dict = {
            "id": dev.id,
            "name": dev.name,
            "description": dev.description,
            "done_definitions": dev.done_definitions,
            "working_directory": dev.working_directory,
        }
        if dev.self_review:
            phase_dict["self_review"] = dev.self_review

        assert phase_dict.get("self_review") == {"enabled": True}
