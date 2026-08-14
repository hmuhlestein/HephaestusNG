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


class TestUpdateTaskStatusClearsStaleFailureReason:
    """Regression, observed live: a task that failed several retries
    (goto/retry reuses the same task row, so failure_reason from an
    earlier attempt sticks around) and then succeeded on a later attempt
    still showed its old failure_reason forever -- update_task_status only
    ever wrote failure_reason when status=="failed", never cleared it on
    status=="done". A task genuinely marked done kept displaying
    "Output validation failed: ..." with no way to tell it had actually
    succeeded."""

    def test_success_clears_prior_failure_reason(self, test_db, test_client, tmp_path):
        task_id, agent_id = _seed(test_db, tmp_path)
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        task.failure_reason = (
            "Output validation failed: not valid OKF: "
            "security_review/security.md (no valid OKF frontmatter block)"
        )
        session.commit()

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
                    "task_id": task_id,
                    "status": "done",
                    "summary": "succeeded on retry",
                    "key_learnings": [],
                },
                headers={"X-Agent-ID": agent_id},
            )

        assert resp.status_code == 200, resp.text
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "done"
        assert task.failure_reason is None

    def test_failure_still_records_failure_reason(self, test_db, test_client, tmp_path):
        task_id, agent_id = _seed(test_db, tmp_path)

        resp = test_client.post(
            "/update_task_status",
            json={
                "task_id": task_id,
                "status": "failed",
                "summary": "s",
                "failure_reason": "real failure",
                "key_learnings": [],
            },
            headers={"X-Agent-ID": agent_id},
        )

        assert resp.status_code == 200, resp.text
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "failed"
        assert task.failure_reason == "real failure"


class TestUpdateTaskStatusIdempotentOnAlreadyTerminalTask:
    """Regression, observed live: a CLI's own auto-compact replayed the
    initial task prompt back to the agent after it had already completed
    the task, which read as a fresh request to redo the work -- the agent
    dutifully called complete_my_task again with the same content,
    repeatedly (four times in one run before it finally stopped). Every
    one of those redundant calls re-ran the full pipeline below --
    record_learnings (duplicate memory writes each time), self-review,
    output-artifact re-verification, cost re-collection -- none of which
    are idempotent. A task already in a terminal state (done/failed/
    duplicated) now short-circuits immediately instead."""

    def test_redundant_done_call_skips_reprocessing(self, test_db, test_client, tmp_path):
        task_id, agent_id = _seed(test_db, tmp_path)
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        task.status = "done"
        task.completion_notes = "original completion"
        session.commit()

        with patch(
            "src.services.task_completion_service.TaskCompletionService.record_learnings",
            new_callable=AsyncMock,
        ) as mock_record_learnings:
            resp = test_client.post(
                "/update_task_status",
                json={
                    "task_id": task_id,
                    "status": "done",
                    "summary": "redundant replay of the same completion",
                    "key_learnings": ["some learning that would otherwise get duplicated"],
                },
                headers={"X-Agent-ID": agent_id},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True
        mock_record_learnings.assert_not_awaited()

        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        # Untouched by the redundant call -- proves it short-circuited
        # rather than reprocessing and overwriting with the new summary.
        assert task.completion_notes == "original completion"

    def test_normal_first_completion_still_processes(self, test_db, test_client, tmp_path):
        """Sanity check the guard doesn't fire for a task's real, first
        completion -- only already-terminal tasks short-circuit."""
        task_id, agent_id = _seed(test_db, tmp_path)

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
                    "task_id": task_id,
                    "status": "done",
                    "summary": "first real completion",
                    "key_learnings": [],
                },
                headers={"X-Agent-ID": agent_id},
            )

        assert resp.status_code == 200, resp.text
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "done"
        assert task.completion_notes == "first real completion"


def _seed_failed(test_db, tmp_path, phase_name="development"):
    """Same shape as _seed, but the task starts 'failed' (the precondition
    for POST /api/tasks/{id}/complete) and has no assigned agent -- this
    endpoint exists specifically for a task whose agent can't report back."""
    session = test_db.get_session()
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    phase_id = f"phase-{uuid.uuid4().hex[:8]}"
    task_id = f"task-{uuid.uuid4().hex[:8]}"

    session.add(
        Workflow(id=workflow_id, name="t", phases_folder_path="/tmp", status="active", working_directory=str(tmp_path))
    )
    session.add(
        Phase(id=phase_id, workflow_id=workflow_id, order=5, name=phase_name, description="d", done_definitions=["done"])
    )
    session.add(
        Task(
            id=task_id, raw_description="raw", done_definition="done", status="failed",
            workflow_id=workflow_id, phase_id=phase_id, failure_reason="agent terminated unexpectedly",
        )
    )
    session.commit()
    return task_id


class TestCompleteTaskAsUserCommitsWorktree:
    """Regression: POST /api/tasks/{id}/complete (human-operator recovery
    for a task whose agent can't report back) skipped commit_and_link_
    ticket/verify_output_survived_commit entirely -- unlike update_task_
    status, which always commits the worktree before advancing. Used for
    any failed/blocked task, not just git_commit_push, a human confirming
    "done" on e.g. a development task could mark it done while the agent's
    actual code changes sat uncommitted in the worktree, and the pipeline
    would advance later phases against a worktree missing that work."""

    def test_commits_worktree_for_non_git_commit_push_phase(self, test_db, test_client, tmp_path):
        task_id = _seed_failed(test_db, tmp_path, phase_name="development")
        tcs = "src.services.task_completion_service.TaskCompletionService"

        with patch(f"{tcs}.verify_output_artifact", return_value=None), \
             patch(f"{tcs}.verify_gate_result_schema", return_value=None), \
             patch(f"{tcs}.verify_no_open_tickets", return_value=None), \
             patch(f"{tcs}.commit_and_link_ticket", new_callable=AsyncMock, return_value="deadbeef") as mock_commit, \
             patch(f"{tcs}.verify_output_survived_commit", return_value=None), \
             patch(f"{tcs}.fire_spec_gate_if_ready", new_callable=AsyncMock):
            resp = test_client.post(
                f"/api/tasks/{task_id}/complete",
                json={"summary": "manually verified and committed by operator"},
            )

        assert resp.status_code == 200, resp.text
        mock_commit.assert_called_once()
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "done"

    def test_skips_commit_for_git_commit_push_phase(self, test_db, test_client, tmp_path):
        """git_commit_push is the one phase whose whole job IS the git
        commit/push -- an operator completing it manually has already done
        that outside Hephaestus, so this must not also try to commit."""
        task_id = _seed_failed(test_db, tmp_path, phase_name="git_commit_push")
        tcs = "src.services.task_completion_service.TaskCompletionService"

        with patch(f"{tcs}.verify_output_artifact", return_value=None), \
             patch(f"{tcs}.verify_gate_result_schema", return_value=None), \
             patch(f"{tcs}.verify_no_open_tickets", return_value=None), \
             patch(f"{tcs}.commit_and_link_ticket", new_callable=AsyncMock, return_value="deadbeef") as mock_commit, \
             patch(f"{tcs}.verify_output_survived_commit", return_value=None), \
             patch(f"{tcs}.fire_spec_gate_if_ready", new_callable=AsyncMock):
            resp = test_client.post(
                f"/api/tasks/{task_id}/complete",
                json={"summary": "committed and pushed manually"},
            )

        assert resp.status_code == 200, resp.text
        mock_commit.assert_not_called()
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "done"

    def test_rejects_completion_when_output_vanished_after_commit(self, test_db, test_client, tmp_path):
        task_id = _seed_failed(test_db, tmp_path, phase_name="development")
        tcs = "src.services.task_completion_service.TaskCompletionService"

        with patch(f"{tcs}.verify_output_artifact", return_value=None), \
             patch(f"{tcs}.verify_gate_result_schema", return_value=None), \
             patch(f"{tcs}.verify_no_open_tickets", return_value=None), \
             patch(f"{tcs}.commit_and_link_ticket", new_callable=AsyncMock, return_value="deadbeef"), \
             patch(f"{tcs}.verify_output_survived_commit", return_value={"message": "output vanished"}), \
             patch(f"{tcs}.fire_spec_gate_if_ready", new_callable=AsyncMock) as mock_gate:
            resp = test_client.post(
                f"/api/tasks/{task_id}/complete",
                json={"summary": "claims done but output is gone"},
            )

        assert resp.status_code == 400, resp.text
        mock_gate.assert_not_called()
        session = test_db.get_session()
        task = session.query(Task).filter_by(id=task_id).first()
        assert task.status == "failed"
        assert task.failure_reason == "output vanished"
