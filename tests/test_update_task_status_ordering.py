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


def _seed_full_uuid_task(test_db, tmp_path):
    """Same shape as _seed, but with a real 36-char UUID task_id -- this
    class specifically exercises the full-vs-truncated-ID distinction,
    which _seed's short `task-XXXXXXXX` fixture IDs can't."""
    session = test_db.get_session()
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    phase_id = f"phase-{uuid.uuid4().hex[:8]}"
    task_id = str(uuid.uuid4())
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"

    session.add(
        Workflow(
            id=workflow_id, name="t", phases_folder_path="/tmp", status="active",
            working_directory=str(tmp_path),
        )
    )
    session.add(
        Phase(
            id=phase_id, workflow_id=workflow_id, order=7, name="security_review",
            description="d", done_definitions=["done"],
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


class TestUpdateTaskStatusTruncatedIdFallback:
    """Regression, observed live: an agent called update_task_status with
    only the 8-char short form of its task_id (e.g. "e2b0a2fc" instead of
    "e2b0a2fc-7679-49f7-9719-09458a8deae0") on every single retry -- this
    codebase's own logs/transcripts display task IDs that way everywhere
    (task.id[:8]), and the agent had clearly picked up the convention.
    Every attempt hard-failed "Task not found," leaving a task that had
    genuinely finished its work (result submitted, memory saved) stuck
    in_progress with no assigned agent, forever."""

    def test_unambiguous_prefix_resolves(self, test_db, test_client, tmp_path):
        task_id, agent_id = _seed_full_uuid_task(test_db, tmp_path)

        with patch(
            "src.services.task_completion_service.TaskCompletionService.commit_and_link_ticket",
            new_callable=AsyncMock,
        ), patch(
            "src.services.task_completion_service.TaskCompletionService.fire_spec_gate_if_ready",
            new_callable=AsyncMock,
        ):
            resp = test_client.post(
                "/update_task_status",
                json={
                    "task_id": task_id[:8],
                    "status": "done",
                    "summary": "done via truncated id",
                    "key_learnings": [],
                },
                headers={"X-Agent-ID": agent_id},
            )

        assert resp.status_code == 200, resp.text
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "done"

    def test_ambiguous_prefix_is_not_silently_guessed(self, test_db, test_client, tmp_path):
        """Two tasks sharing an 8-char prefix must not resolve to either
        one silently -- fail the same way an unrecognized id would."""
        session = test_db.get_session()
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        phase_id = f"phase-{uuid.uuid4().hex[:8]}"
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        shared_prefix = "aaaaaaaa"
        task_id_1 = f"{shared_prefix}-0000-0000-0000-000000000001"
        task_id_2 = f"{shared_prefix}-0000-0000-0000-000000000002"

        session.add(
            Workflow(id=workflow_id, name="t", phases_folder_path="/tmp", status="active", working_directory=str(tmp_path))
        )
        session.add(
            Phase(id=phase_id, workflow_id=workflow_id, order=7, name="security_review", description="d", done_definitions=["done"])
        )
        session.add(Agent(id=agent_id, system_prompt="p", status="working", cli_type="claude", agent_type="phase"))
        for tid in (task_id_1, task_id_2):
            session.add(
                Task(
                    id=tid, raw_description="raw", done_definition="done", status="in_progress",
                    workflow_id=workflow_id, phase_id=phase_id, assigned_agent_id=agent_id,
                )
            )
        session.commit()

        resp = test_client.post(
            "/update_task_status",
            json={"task_id": shared_prefix, "status": "done", "summary": "s", "key_learnings": []},
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 404
        session = test_db.get_session()
        for tid in (task_id_1, task_id_2):
            assert session.query(Task).filter_by(id=tid).first().status == "in_progress"

    def test_unmatched_prefix_still_404s(self, test_db, test_client, tmp_path):
        _task_id, agent_id = _seed_full_uuid_task(test_db, tmp_path)

        resp = test_client.post(
            "/update_task_status",
            json={"task_id": "ffffffff", "status": "done", "summary": "s", "key_learnings": []},
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 404
